# Analysis 分析 pass 与 AnalysisManager

## 1. 本讲目标

在 u4-l1 里我们已经把新 Pass 管理器的骨架搭好了：`PassManager` 负责按顺序执行「变换 pass」改写 IR，`AnalysisManager` 负责把「分析 pass」的结果缓存起来供大家复用。但当时我们只是把分析 pass 当作「一类特殊的 pass」一笔带过，并没有真正打开它看里面是什么。本讲就把这个黑盒拆开。

变换 pass 是优化效果的来源（它真的改 IR），但绝大多数变换 pass 在动手之前，必须先「看清」IR 的某种结构——循环长什么样？这两条 load/store 访问的内存会不会重叠？这条 store 到底 clobber（覆盖）了哪些之前的内存写？这些「看清结构」的工作，就是**分析 pass**的职责。分析 pass 不改 IR，只产出一个可被反复查询的「分析结果对象」，因此可以被同一条流水线里的多个变换 pass 共享，避免重复计算。

本讲学完后，你应该能够：

1. 准确区分**分析 pass**与**变换 pass**，并说出 `AnalysisManager` 是如何用「按需计算 + 缓存 + 失效」三件套来管理分析结果的。
2. 读懂 **LoopInfo**（循环信息）分析：它如何依赖支配树（DominatorTree），以及循环不变量（loop-invariant）这个核心查询的含义。
3. 读懂**别名分析（Alias Analysis）**：`AAManager` 如何把多个 AA 提供者（如 BasicAA）组合成一个 `AAResults`，以及 `NoAlias/MayAlias/MustAlias` 等查询结果的语义。
4. 读懂 **MemorySSA**：它如何把「内存访问」建模成一棵 SSA 形式的 def-use 图，并用 `getClobberingMemoryAccess` 回答「这条 load 真正读到的是哪条 store」。
5. 能用 `opt` 的 `print<loops>`、`aa-eval`、`print<memoryssa>` 把这三类分析的结果打印出来，并对照源码理解输出。

本讲是 u4-l3（经典优化 pass）的直接前提——`InstCombine`、`GVN`、`LICM` 全都重度依赖本讲讲到的分析。它也承接 u3-l1/u3-l2 的 IR 对象模型。

## 2. 前置知识

本讲假设你已经掌握 u4-l1 的概念：四层 IR 单位（Module/CGSCC/Function/Loop）、`PassManager`/`AnalysisManager` 的分工、`PreservedAnalyses` 失效机制。这里再用通俗语言补两个本讲反复用到的术语。

- **支配（dominate）与支配树（Dominator Tree）**：在一个函数的控制流图（CFG）里，如果从入口基本块到基本块 B 的**所有**路径都必定经过基本块 A，就说「A 支配 B」（记作 A dom B）。每个函数都有这样一棵支配树。支配关系是 LLVM 里最基础的分析之一，循环检测、SSA 构造、支配边界都建立在它之上。它本身也是一个分析 pass（`DominatorTreeAnalysis`），对应 `-passes` 里的名字 `domtree`。本讲的 LoopInfo 和 MemorySSA 都依赖它。
- **别名（alias）**：两条内存访问指令，如果它们访问的内存区间可能重叠，就说它们「别名」。别名信息越精确（能证明不重叠），优化器就越敢做激进变换（如重排、删除冗余 load）。反之，若什么都证明不了，优化器只能保守假设「可能重叠」。

> 提示：如果你对「基本块、支配、CFG」还生疏，可回顾 u2-l2 与 u3-l1。本讲会直接用「支配」「自然循环」「内存 def-use」这些词。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `llvm/include/llvm/IR/PassManager.h` | 新 PM 核心：定义 `AnalysisManager` 模板、`getResult`/`getCachedResult`/`invalidate`、以及跨分析失效用的 `Invalidator`。 |
| `llvm/include/llvm/Analysis/LoopInfo.h` / `llvm/lib/Analysis/LoopInfo.cpp` | 循环信息分析 `LoopAnalysis` 与 `Loop`/`LoopInfo` 类、`LoopPrinterPass`。 |
| `llvm/include/llvm/Analysis/AliasAnalysis.h` / `llvm/lib/Analysis/AliasAnalysis.cpp` | 别名分析的统一接口：`AliasResult`、聚合器 `AAResults`、流水线管理器 `AAManager`。 |
| `llvm/include/llvm/Analysis/BasicAliasAnalysis.h` / `llvm/lib/Analysis/BasicAliasAnalysis.cpp` | 默认的、无状态的基础 AA 提供者 `BasicAA`/`BasicAAResult`。 |
| `llvm/include/llvm/Analysis/MemorySSA.h` / `llvm/lib/Analysis/MemorySSA.cpp` | 内存 SSA：`MemoryAccess`/`MemoryUse`/`MemoryDef`/`MemoryPhi`、`MemorySSAAnalysis`、`MemorySSAWalker`。 |
| `llvm/lib/Transforms/Scalar/LICM.cpp` | 循环不变量外提 pass，是这三类分析的「真实消费者」范例。 |
| `llvm/lib/Passes/PassBuilder.cpp` / `llvm/lib/Passes/PassRegistry.def` | 注册默认 AA 流水线、把分析 pass 绑定到 `-passes` 文本名字。 |

## 4. 核心概念与源码讲解

### 4.1 分析 pass 与 AnalysisManager 的惰性缓存

#### 4.1.1 概念说明

回到 u4-l1 的二分法：新 PM 把 pass 分成两类——

- **变换 pass（Transformation Pass）**：读 IR、改 IR，返回 `PreservedAnalyses` 声明保住了哪些分析。它是优化效果的实际来源。
- **分析 pass（Analysis Pass）**：只读 IR、不改 IR，产出一个**分析结果对象（Result）**。这个对象里封装了对 IR 某种结构的「认识」，可以被后续任意多个 pass 反复查询。

分析 pass 的价值在于**复用**：计算一次支配树很贵，但如果同一条流水线里有 10 个 pass 都要查支配树，我们显然希望它只算一次。`AnalysisManager`（下称 AM）就是干这件事的容器——它给每个 IR 单位（这里是 Function）维护一张「分析 ID → 结果」的缓存表，按需计算、命中即返回。

理解 AM 的关键是三件套：**按需计算（lazy）、缓存（cache）、失效（invalidate）**。

- **按需**：分析不会被预先全部跑一遍。只有当某个 pass 调用 `AM.getResult<XxxAnalysis>(F)` 时，AM 才第一次计算它。
- **缓存**：算完的结果按「分析类型 + IR 单位」存起来。下一次同一 IR 单位再请求同一分析，直接返回缓存对象，不重算。
- **失效**：一旦某个变换 pass 改写了 IR，之前缓存的部分分析结果就「过期」了。变换 pass 通过返回 `PreservedAnalyses` 显式声明「我保住了哪些分析」；AM 据此丢弃那些没被保住的分析。这就是 u4-l1 讲过的失效机制，本讲看它的真实代码。

#### 4.1.2 核心流程

AM 对分析结果的生命周期管理可以概括成下面这条主线：

```
某 pass 调用 AM.getResult<LoopAnalysis>(F)
        │
        ▼
在 AnalysisResults 表里查 {LoopAnalysis::ID(), &F}
   ├── 命中 → 直接返回缓存结果（不重算）
   └── 未命中 → 用注册过的 LoopAnalysis::run(F, AM) 计算结果
                 存入缓存，返回
        │
   ……若干变换 pass 改写了 F，返回 PreservedAnalyses PA……
        │
        ▼
AM.invalidate(F, PA)：遍历 F 上所有缓存的分析
   对每个分析，调它的 Result::invalidate(F, PA, Inv)
   ├── 分析声明自己被保住 且 无依赖被破坏 → 保留
   └── 否则 → 从缓存删除，下次再被请求时重算
```

注意两件事。第一，AM 内部用「分析 ID（一个 `AnalysisKey*`）+ IR 单位指针」作为缓存的复合键，类型擦除后统一存一个 `std::list`，再用一个 `DenseMap` 做索引。第二，**分析之间会互相依赖**：LoopInfo 依赖支配树，MemorySSA 依赖支配树 + 别名分析。如果支配树失效了，那 LoopInfo 即使自己「被保住」也跟着失效。这个「连带失效」由一个叫 `Invalidator` 的对象负责，它允许某个分析结果在判断自己是否失效时，再去反问「我依赖的那些分析失效了吗」。

#### 4.1.3 源码精读

先看 `AnalysisManager` 类本身和它的两个核心查询入口。AM 是个模板，对函数层做了个常用别名：

[llvm/include/llvm/IR/PassManager.h:574-L583](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L574-L583) — 把 `AnalysisManager<Module>` 命名为 `ModuleAnalysisManager`、`AnalysisManager<Function>` 命名为 `FunctionAnalysisManager`。本讲的 LoopInfo/AA/MemorySSA 都是函数级分析，挂在 `FunctionAnalysisManager` 上。

`getResult` 是「按需计算 + 缓存」的对外入口，注释明确写了「如果缓存里没有就跑这个分析」：

```cpp
// 427-442
template <typename PassT>
typename PassT::Result &getResult(IRUnitT &IR, ExtraArgTs... ExtraArgs) {
  assert(AnalysisPasses.count(PassT::ID()) &&
         "This analysis pass was not registered prior to being queried");
  ResultConceptT &ResultConcept =
      getResultImpl(PassT::ID(), IR, ExtraArgs...);   // 真正查缓存 / 触发 run
  ...
  return static_cast<ResultModelT &>(ResultConcept).Result;
}
```
[llvm/include/llvm/IR/PassManager.h:L427-L442](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L427-L442) — `getResult<LoopAnalysis>(F)`：若 `AnalysisResults` 里没有该分析的结果，则调用其 `run` 计算并存缓存；否则直接返回缓存。注意开头的断言：分析必须先被注册（由 `PassBuilder::registerFunctionAnalyses` 完成），否则直接查会触发断言失败。

与它相对的是 `getCachedResult`——**只查不跑**：

```cpp
// 444-463
template <typename PassT>
typename PassT::Result *getCachedResult(IRUnitT &IR) const {
  ...
  ResultConceptT *ResultConcept = getCachedResultImpl(PassT::ID(), IR);
  if (!ResultConcept)
    return nullptr;          // 缓存里没有就返回空，绝不触发计算
  ...
}
```
[llvm/include/llvm/IR/PassManager.h:L444-L463](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L444-L463) — `getCachedResult` 用于「分析可能还没被任何 pass 触发」的场景（例如代理类判断模块级分析是否已就绪），它保证不会因为查询而产生副作用。

失效入口在 AM 这一侧只是一个声明，具体遍历逻辑在 `.cpp`：

[llvm/include/llvm/IR/PassManager.h:L507-L511](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L507-L511) — `AnalysisManager::invalidate(IR, PA)`：遍历该 IR 单位上所有缓存分析，除非被 `PreservedAnalyses` 保住，否则删除。

真正的「连带失效」机制在嵌套类 `Invalidator` 里。它是 AM 在失效遍历时传给每个分析结果的一个工具对象，分析结果用它来反问「我依赖的某某分析是否也已失效」：

```cpp
// 352-381（节选）
template <typename ResultT = ResultConceptT>
bool invalidateImpl(AnalysisKey *ID, IRUnitT &IR, const PreservedAnalyses &PA) {
  auto IMapI = IsResultInvalidated.find(ID);
  if (IMapI != IsResultInvalidated.end())
    return IMapI->second;                       // 同一次失效遍历里，问过的直接复用
  ...
  auto RI = Results.find({ID, &IR});
  ...
  std::tie(IMapI, Inserted) = IsResultInvalidated.insert(
      {ID, Result.invalidate(IR, PA, *this)});  // 回调该分析自己的 invalidate
  ...
  return IMapI->second;
}
```
[llvm/include/llvm/IR/PassManager.h:L352-L381](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L352-L381) — `Invalidator::invalidateImpl`：递归地（深度优先）判定一个分析及其依赖是否需要失效，并用 `IsResultInvalidated` 这个小 map 避免重复判定与循环依赖。这正是「支配树失效 → LoopInfo/​MemorySSA 跟着失效」得以实现的底层通道。

> 一句话总结：`getResult` 触发计算、`getCachedResult` 只读缓存、`invalidate` + `Invalidator` 负责连带失效。这三者就是 AM 的全部对外语义。

#### 4.1.4 代码实践

**实践目标**：亲手验证「分析结果会被缓存、且能被 `print<...>` 这类打印 pass 触发计算」。

**操作步骤**：

1. 把下面这段含循环和内存访问的 IR 存成 `demo.ll`（本讲后续小节会反复用到它）：

```llvm
; demo.ll
define void @foo(ptr %p, ptr %q, i32 %n) {
entry:
  br label %loop

loop:
  %i = phi i32 [0, %entry], [%next, %loop]
  %pi = getelementptr i32, ptr %p, i32 %i
  store i32 %i, ptr %pi        ; 写 %p
  %v = load i32, ptr %q        ; 读 %q
  %next = add i32 %i, 1
  %cond = icmp slt i32 %next, %n
  br i1 %cond, label %loop, label %exit

exit:
  ret void
}
```

2. 运行 `print<loops>`（它是一个要求 LoopAnalysis 的「打印 pass」），观察 LoopInfo 被计算并打印：

```bash
opt -passes='print<loops>' demo.ll -disable-output
```

**需要观察的现象**：终端会打印出 `Loop info for function 'foo':` 并列出检测到的循环（含 header、preheader 等），而 `demo.ll` 文件本身不变（`-disable-output` 表示不写回 IR）。

**预期结果**：你能看到 `print<loops>` 触发了一次 LoopAnalysis 计算。这就是「分析被按需触发」的直观体现。精确输出格式见 4.2.4。如果手头没有可运行的 `opt`，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 AM 不在流水线开始前把所有注册过的分析都预先算一遍？

**参考答案**：因为绝大多数分析在一个典型流水线里根本用不到，预计算会浪费大量 CPU 与内存。AM 采用「按需计算 + 缓存」：谁用 `getResult` 算谁，算完缓存给后续复用。这把「必要性」交给了实际的 pass 去决定。

**练习 2**：`getResult` 和 `getCachedResult` 在「是否触发计算」上有什么本质区别？各自适合什么场景？

**参考答案**：`getResult` 命中缓存就返回、未命中就跑分析；`getCachedResult` 永远只读缓存、未命中返回 `nullptr`。前者用于「我确实需要这个分析，没有就替我算」；后者用于「我只是想看看它是否已经就绪、绝不能因为查询而引入副作用」（典型如跨层代理 `ModuleAnalysisManagerFunctionProxy` 判断模块级分析是否已被物化）。

---

### 4.2 LoopInfo：循环信息分析

#### 4.2.1 概念说明

**LoopInfo** 回答的是「这个函数里有哪些自然循环（natural loop）」。所谓自然循环，是指存在一个回边（backedge）和被该回边「圈起来」的一组基本块。LoopInfo 把它们组织成「循环森林」：外层循环包含内层循环，最外层循环挂在函数上。每个 `Loop` 对象暴露大量查询：它的 header、latch、preheader、出口块、所有成员块，以及最重要的——某个值是否**循环不变（loop-invariant）**。

循环不变量是循环优化的核心判据。比如 LICM（循环不变量外提）想做的事就是：如果一条指令的计算结果在整个循环里不变，就把它搬到循环外面只算一次。判断「不变」就靠 `Loop::isLoopInvariant(V)`。而 LoopInfo 自身的计算又依赖支配树——回边的定义就建立在支配关系上（回边的目标 header 必须支配回边的源 latch）。所以这是一个「分析依赖分析」的典型例子。

#### 4.2.2 核心流程

```
LoopAnalysis::run(F, AM)
   │
   ├── （按需）AM.getResult<DominatorTreeAnalysis>(F)   ← 依赖支配树
   │
   └── LoopInfo.analyze(F, ...支配树...)
          │
          ├── 找出所有回边
          ├── 对每条回边构造自然循环（header + body）
          └── 嵌套成循环森林，返回 LoopInfo
```

`LoopInfo` 结果被缓存后，`LICM`、`LoopRotate`、`LoopUnroll`、`IndVarSimplify` 等一堆循环 pass 都通过 `AM.getResult<LoopAnalysis>(F)` 拿到它，无需各自重算。

#### 4.2.3 源码精读

先看分析 pass 的声明。每个分析 pass 都经 CRTP 混入 `AnalysisInfoMixin` 拿到一个全局唯一的 `AnalysisKey`（这就是缓存的 ID），并声明 `Result` 类型和 `run` 方法：

```cpp
// 587-595
class LoopAnalysis : public AnalysisInfoMixin<LoopAnalysis> {
  friend AnalysisInfoMixin<LoopAnalysis>;
  LLVM_ABI static AnalysisKey Key;
public:
  typedef LoopInfo Result;
  LLVM_ABI LoopInfo run(Function &F, FunctionAnalysisManager &AM);
};
```
[llvm/include/llvm/Analysis/LoopInfo.h:L587-L595](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Analysis/LoopInfo.h#L587-L595) — `LoopAnalysis`：结果类型是 `LoopInfo`，`Key` 是它的缓存身份。`Result` 可以是值（这里是 `LoopInfo`），也可以是更复杂的句柄对象（MemorySSA 那种，见 4.4.3）。

`run` 的实现清晰地展示了「分析依赖分析」——它通过传入的 `AM` 按需取得支配树：

```cpp
// 1006-1019
LoopInfo LoopAnalysis::run(Function &F, FunctionAnalysisManager &AM) {
  // FIXME: Currently we create a LoopInfo from scratch for every function.
  ...
  LoopInfo LI;
  // The dominator tree is needed only for an irreducible CFG.
  LI.analyze(&F, [&]() -> const DominatorTree & {
    return AM.getResult<DominatorTreeAnalysis>(F);   // ← 按需取支配树
  });
  return LI;
}
```
[llvm/lib/Analysis/LoopInfo.cpp:L1006-L1019](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Analysis/LoopInfo.cpp#L1006-L1019) — `LoopAnalysis::run`：构造一个 `LoopInfo`，再调用 `LI.analyze`，其间通过 lambda **惰性地** `getResult<DominatorTreeAnalysis>`。这里的惰性很巧妙：只有当 CFG 不可规约（irreducible）时才真的需要支配树，规约 CFG 走的是另一套 SCC 算法。这正是 AM「按需计算」哲学在分析内部再一次体现。

`Loop` 对象上最被频繁查询的莫过于循环不变性：

[llvm/include/llvm/Analysis/LoopInfo.h:L62](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Analysis/LoopInfo.h#L62) — `bool Loop::isLoopInvariant(const Value *V) const;`：判断值 `V` 在本循环内是否不变。LICM 等变换 pass 用它来决定能否把某条指令搬到循环外。

最后看打印 pass，它正是 `print<loops>` 的本体——一个要求 LoopAnalysis、把结果打印出来、自身不改 IR（返回 `PreservedAnalyses::all()`）的 pass：

```cpp
// 1021-1027
PreservedAnalyses LoopPrinterPass::run(Function &F,
                                       FunctionAnalysisManager &AM) {
  auto &LI = AM.getResult<LoopAnalysis>(F);          // ← 触发/复用分析
  OS << "Loop info for function '" << F.getName() << "':\n";
  LI.print(OS);
  return PreservedAnalyses::all();                   // 不改 IR，全部保住
}
```
[llvm/lib/Analysis/LoopInfo.cpp:L1021-L1027](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Analysis/LoopInfo.cpp#L1021-L1027) — `LoopPrinterPass::run`：`AM.getResult<LoopAnalysis>(F)` 这一句既是「消费者请求分析」的范本，也是 `print<loops>` 之所以能打出循环信息的根源。

#### 4.2.4 代码实践

**实践目标**：在真实循环 IR 上观察 LoopInfo 输出，并理解它和支配树的依赖。

**操作步骤**：

```bash
opt -passes='print<loops>' demo.ll -disable-output
```

**需要观察的现象**：输出形如

```
Loop info for function 'foo':
Loop at depth 1 containing: %loop<header><exiting>,%entry,%exit
```

（实际字段以本地版本为准，待本地验证。）它会标注每个循环的嵌套深度 `depth`、header 块、是否为 exiting 块等。

**预期结果**：你能确认 LoopInfo 正确识别出 `loop` 基本块构成的单层循环。若想顺带看支配树，可换成 `opt -passes='print<domtree>' demo.ll -disable-output`，体会「LoopInfo 依赖 DominatorTree」这条依赖链。

#### 4.2.5 小练习与答案

**练习 1**：`LoopAnalysis::run` 里取支配树用的是 `getResult` 还是 `getCachedResult`？为什么？

**参考答案**：用的是 `getResult`。因为 LoopInfo 的正确性必须建立在支配树之上，支配树不存在就得马上算出来；用只读缓存的 `getCachedResult` 拿到 `nullptr` 就没法继续分析了。

**练习 2**：`LoopPrinterPass` 返回 `PreservedAnalyses::all()`，意味着什么？

**参考答案**：意味着它声明「我没有改 IR，所有分析结果都仍然有效」。因此它跑完后，LoopInfo 以及其它分析都继续留在缓存里，无需重算。打印类 pass 通常都是这样「只读」的。

---

### 4.3 别名分析：AAManager / AAResults / BasicAA

#### 4.3.1 概念说明

**别名分析（Alias Analysis，AA）**回答的核心问题是：两条内存访问指令，它们操作的内存区间**会不会重叠**。这个答案直接决定优化器敢多激进——比如要删除一个冗余 load，必须确认没有别的 store 在中间改写了它读的地址。

AA 的查询结果用一个四值枚举 `AliasResult` 表示，按「精确度」从粗到细：

- `MayAlias`：可能重叠（最保守，什么都没证明，可当作「默认值」）。
- `NoAlias`：证明**不**重叠（最有用的「好消息」，允许激进优化）。
- `PartialAlias`：重叠，但只是部分重叠（附带可计算的偏移）。
- `MustAlias`：精确重叠，起点与大小完全一致。

LLVM 的 AA 不是「一个大算法」，而是**一条由多个 AA 提供者（provider）组成的流水线**。每个提供者用不同依据给答案：`BasicAA` 用数据布局、捕获分析、支配关系等本地推理；`ScopedNoAliasAA`/`TypeBasedAA` 用 `!noalias`/`!tbaa` 元数据；目标后端还能插入自己的 AA。它们被 `AAManager` 组合进一个聚合器 `AAResults`，查询时依次问，**第一个给出确定性答案（非 MayAlias）者胜出**。这种「可组合、首次确定即停」的设计让 AA 既灵活又高效。

#### 4.3.2 核心流程

```
AAManager::run(F, AM)                      ← 一次性组装聚合器
   ├──getResult<TargetLibraryAnalysis>(F)  ← AAResults 总要带上 TLI
   ├── 对每个已注册的 AA 提供者（如 BasicAA）
   │     └─ getResult<该AA>(F) → AAResults.addAAResult(...) + 记录依赖ID
   └── 得到一个组合好的 AAResults，缓存为 AAManager 的结果

某 pass 调用 AAResults.alias(LocA, LocB)
   └── for 每个 provider：result = provider.alias(...)
         若 result != MayAlias → 立即返回（首个确定性答案胜出）
```

`AAResults` 是分析结果，被缓存在 `AAManager` 名下；但它内部又依赖若干子分析（BasicAA、TLI 等），所以它的 `invalidate` 要「连带」检查这些子分析（见 4.1 的 `Invalidator`）。

#### 4.3.3 源码精读

先看结果枚举 `AliasResult`，注释把四种语义讲得很清楚：

```cpp
// 92-106
enum Kind : uint8_t {
  NoAlias = 0,   // 两处完全不重叠（被刻意排成 0，便于在布尔语境里判「有无别名」）
  MayAlias,      // 可能也可能不重叠，最不精确
  PartialAlias,  // 重叠，但只是部分
  MustAlias,     // 精确重叠
};
```
[llvm/include/llvm/Analysis/AliasAnalysis.h:L92-L106](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Analysis/AliasAnalysis.h#L92-L106) — `AliasResult::Kind`。注意 `NoAlias = 0` 的安排：它转换成 `bool` 时为 `false`（无别名），其余为 `true`（存在别名的可能），方便一句 `if (AA.alias(...))` 判断。

聚合器 `AAResults` 的「组装口」和「查询口」：

```cpp
// 324-336
template <typename AAResultT> void addAAResult(AAResultT &AAResult) {
  AAs.emplace_back(new Model<AAResultT>(AAResult, *this));   // 把一个 provider 挂进链表
}
void addAADependencyID(AnalysisKey *ID) { AADeps.push_back(ID); }  // 记下「我依赖它」
```
[llvm/include/llvm/Analysis/AliasAnalysis.h:L324-L336](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Analysis/AliasAnalysis.h#L324-L336) — `addAAResult` 装一个 AA 提供者进 `AAs` 链表；`addAADependencyID` 把该提供者的分析 ID 记进 `AADeps`，供失效时连带检查。

查询时的「逐个问、首个确定即停」逻辑，是整条 AA 流水线的核心：

```cpp
// 110-130（节选）
AliasResult AAResults::alias(const MemoryLocation &LocA,
                             const MemoryLocation &LocB, AAQueryInfo &AAQI,
                             const Instruction *CtxI) {
  ...
  AliasResult Result = AliasResult::MayAlias;
  ...
  AAQI.Depth++;
  for (const auto &AA : AAs) {
    Result = AA->alias(LocA, LocB, AAQI, CtxI);
    if (Result != AliasResult::MayAlias)
      break;                  // ← 第一个给出确定答案者胜出
  }
  AAQI.Depth--;
  ...
}
```
[llvm/lib/Analysis/AliasAnalysis.cpp:L110-L130](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Analysis/AliasAnalysis.cpp#L110-L130) — `AAResults::alias`：遍历所有已注册的 AA 提供者，谁先给出非 `MayAlias` 的确定答案就立刻返回。`MayAlias` 是「我没结论」的占位，所以遇到任何确定结论即可终止。

谁负责把 provider 一个个装进 `AAResults`？是 `AAManager::run`，它先把 `TargetLibraryAnalysis` 塞进去，再逐个调用注册过的「结果 getter」：

```cpp
// 890-895
AAManager::Result AAManager::run(Function &F, FunctionAnalysisManager &AM) {
  Result R(AM.getResult<TargetLibraryAnalysis>(F));      // AAResults 必带 TLI
  for (auto &Getter : ResultGetters)
    (*Getter)(F, AM, R);                                 // 每个 getter 把一个 provider 加进 R
  return R;
}
```
[llvm/lib/Analysis/AliasAnalysis.cpp:L890-L895](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Analysis/AliasAnalysis.cpp#L890-L895) — `AAManager::run`：`ResultGetters` 是 `registerFunctionAnalysis<BasicAA>()` 等调用预先登记好的函数指针列表，每个 getter 内部 `getResult<某AA>(F)` 并 `addAAResult`。这样 `AAManager` 这个分析的结果（一个 `AAResults`）就聚拢了整条 AA 流水线。

`AAManager` 是如何注册 provider 的？看模板：

```cpp
// 1012-1019
class AAManager : public AnalysisInfoMixin<AAManager> {
public:
  using Result = AAResults;
  template <typename AnalysisT> void registerFunctionAnalysis() {
    ResultGetters.push_back(&getFunctionAAResultImpl<AnalysisT>);
  }
  ...
};
```
[llvm/include/llvm/Analysis/AliasAnalysis.h:L1012-L1019](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Analysis/AliasAnalysis.h#L1012-L1019) — `AAManager::registerFunctionAnalysis<BasicAA>()` 把 BasicAA 注册为一个函数级 AA 提供者。`getFunctionAAResultImpl`（同文件 1037–1043 行）内部正是 `addAAResult(getResult<AnalysisT>(F))` + `addAADependencyID(AnalysisT::ID())`。

默认流水线里到底装了哪些 AA？由 `PassBuilder` 决定：

```cpp
// 737-741
void PassBuilder::registerFunctionAnalyses(FunctionAnalysisManager &FAM) {
  // We almost always want the default alias analysis pipeline.
  // If a user wants a different one, they can register their own before calling
  // registerFunctionAnalyses().
  FAM.registerPass([&] { return buildDefaultAAPipeline(); });
  ...
}
```
[llvm/lib/Passes/PassBuilder.cpp:L737-L741](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Passes/PassBuilder.cpp#L737-L741) — `registerFunctionAnalyses` 在注册函数级分析时，默认就用 `buildDefaultAAPipeline()` 装配一个 `AAManager`（内含 BasicAA、TypeBasedAA、ScopedNoAliasAA 等）。这就是为什么你什么都不写也有 AA 可用。

默认流水线的「主力」是 BasicAA，它是无状态的本地推理 AA，结果依赖 `DataLayout`/`Function`/`TLI`/`AssumptionCache`/`DominatorTree`：

```cpp
// 2060-2065
BasicAAResult BasicAA::run(Function &F, FunctionAnalysisManager &AM) {
  auto &TLI = AM.getResult<TargetLibraryAnalysis>(F);
  auto &AC  = AM.getResult<AssumptionAnalysis>(F);
  auto *DT  = &AM.getResult<DominatorTreeAnalysis>(F);
  return BasicAAResult(F.getDataLayout(), F, TLI, AC, DT);
}
```
[llvm/lib/Analysis/BasicAliasAnalysis.cpp:L2060-L2065](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Analysis/BasicAliasAnalysis.cpp#L2060-L2065) — `BasicAA::run`：把 BasicAA 所需的几项子分析（TLI、AC、DT）按需取来，构造一个 `BasicAAResult`。注意 `BasicAAResult` 持有引用而不持有 IR 所有权——这正是 `AAResults::invalidate` 注释里说的「AA 的无状态特性」。

最后回到「连带失效」。`AAResults` 自身的 `invalidate` 是 `Invalidator` 的教科书级用例——它默认自认被保住（因为 AA 无状态），但若它依赖的任何子分析失效了，它也得跟着失效：

```cpp
// 80-98
bool AAResults::invalidate(Function &F, const PreservedAnalyses &PA,
                           FunctionAnalysisManager::Invalidator &Inv) {
  // AAResults preserves the AAManager by default, due to the stateless nature
  // of AliasAnalysis. ...
  auto PAC = PA.getChecker<AAManager>();
  if (!PAC.preservedWhenStateless())
    return true;
  // Check if any of the function dependencies were invalidated, and invalidate
  // ourselves in that case.
  for (AnalysisKey *ID : AADeps)
    if (Inv.invalidate(ID, F, PA))      // ← 反问依赖的子分析是否失效
      return true;
  return false;
}
```
[llvm/lib/Analysis/AliasAnalysis.cpp:L80-L98](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Analysis/AliasAnalysis.cpp#L80-L98) — `AAResults::invalidate`：先用 `preservedWhenStateless()` 处理模块级依赖；再用 `for (ID : AADeps) Inv.invalidate(ID, F, PA)` 逐个反问函数级依赖（即 `addAADependencyID` 登记的那些 provider）。这就是 4.1 里 `Invalidator` 的真实落地。

#### 4.3.4 代码实践

**实践目标**：用 `aa-eval`（一个会调用 `AAResults` 做大量别名查询并统计的 pass）观察 AA 对 `demo.ll` 里 `%p` 与 `%q` 这两个指针的判定。

**操作步骤**：

```bash
opt -passes='aa-eval' demo.ll -disable-output
```

**需要观察的现象**：`aa-eval` 会打印一段统计，形如

```
===== Alias Analysis Evaluator Report =====
  ... Total Alias Queries: ...
  ... NoAlias: ... (..%)
  ... MayAlias: ...
  ... MustAlias: ...
```

（具体数字以本地为准，待本地验证。）

**预期结果**：因为 `%p` 和 `%q` 是两个不同的函数参数、且没有证据表明它们指向同一对象，BasicAA 会判定它们 `NoAlias`——这正是循环里「写 `%p`、读 `%q`」互不干扰、可以被优化器自由重排的依据。`aa-eval` 的 NoAlias 计数应明显大于 0。

#### 4.3.5 小练习与答案

**练习 1**：在 `AAResults::alias` 的循环里，为什么遇到 `NoAlias` 就 `break`，但遇到 `MayAlias` 却继续问下一个 provider？

**参考答案**：`MayAlias` 的语义是「我没结论」，并不等于「真的别名」，所以不能据此停止——下一个 provider 可能给出更精确的 `NoAlias`。而 `NoAlias`/`MustAlias`/`PartialAlias` 是确定结论，第一个确定结论即为最终答案，所以立即停止。这就是「首个确定性答案胜出」。

**练习 2**：`AAResults` 的 `invalidate` 里 `AADeps` 这个列表是谁填的？它解决什么问题？

**参考答案**：由 `AAManager` 在 `getFunctionAAResultImpl` 里调用 `addAADependencyID(AnalysisT::ID())` 填入，记录「我聚合了哪些子 AA 分析」。它解决「连带失效」问题：即便 `AAResults` 自己被声明保住，只要它聚合的任一子分析（如 BasicAA 依赖的支配树被破坏）失效了，`AAResults` 也得跟着失效，否则会用到过期的底层结果。

---

### 4.4 MemorySSA：把内存访问建模为 SSA

#### 4.4.1 概念说明

别名分析回答「两个指针是否重叠」，是**成对、按需**的查询。但很多优化想知道的是一种**结构化**的问题：「在这个程序点，内存的『当前版本』是什么？这条 load 到底是被哪条 store 喂的？这条 store 又 clobber 了哪些后续访问？」——这类问题用 MemorySSA 回答更高效。

**MemorySSA** 把函数里所有的内存访问组织成一棵**内存上的 SSA 树**，仿照普通值的 SSA（回顾 u3-l2 的 def-use 链）。它给每条可能访问内存的指令挂一个 `MemoryAccess` 节点，节点之间用「定义—使用」边连起来，共三类：

- **`MemoryDef`**：一条写（store、可能写的 call 等）。它有一个 `DefiningAccess`，指向「在我之前、我能看到的最新的内存版本」。
- **`MemoryUse`**：一条读（load）。它的 `DefiningAccess` 指向「真正喂给我数据的那个 MemoryDef」。
- **`MemoryPhi`**：控制流汇合点上的内存版本合并，作用和普通 SSA 的 `phi` 一样。

还有一个特殊的哨兵 **`liveOnEntry`**（`getLiveOnEntryDef()`），代表「函数入口处的内存版本」，即「在函数开始前就存在的、未被本函数任何 store 定义过的内存」。所有未被本函数定义的读，其 clobber 最终都会落到它身上。

MemorySSA 的强大之处在于：一旦建好，回答「这条 load 读到的是哪条 store」只需沿 def-use 链走（由 `MemorySSAWalker` 高效完成，典型 `getClobberingMemoryAccess`），而不必对每对指针重跑别名分析。这使得 GVN、LICM、DeadStoreElimination 等在大函数里也能保持线性级别的开销。它的代价是构造较重，且维护成本高，所以它不是默认开启的——需要流水线显式声明 `loop-mssa`/`memoryssa`。

#### 4.4.2 核心流程

```
MemorySSAAnalysis::run(F, AM)
   ├──getResult<DominatorTreeAnalysis>(F)   ← 依赖支配树
   ├──getResult<AAManager>(F)               ← 依赖别名分析
   └── new MemorySSA(F, &AA, &DT)
          ├── 给每个基本块建 MemoryPhi（控制流汇合处）
          ├── 给每条 load 建 MemoryUse、每条 store 建 MemoryDef
          ├── 把 DefiningAccess 初步连成链
          └── （可选）optimize uses：用 walker 把 use 的定义精确化

某 pass 调用 MSSA.getWalker()->getClobberingMemoryAccess(I)
   └── 沿 MemorySSA 的 def-use 链 + 必要时调用 AA，找出 I 真正的 clobber
```

同样地，MemorySSA 依赖支配树与 AA，因此它的失效也要「连带」检查这两者。

#### 4.4.3 源码精读

`MemorySSAAnalysis` 的声明里，`Result` 是一个**包装类**而非裸 `MemorySSA`——注释点明了原因：要保证 MemorySSA 内部指针的地址稳定性：

```cpp
// 922-943（节选）
class MemorySSAAnalysis : public AnalysisInfoMixin<MemorySSAAnalysis> {
  ...
  struct Result {
    Result(std::unique_ptr<MemorySSA> &&MSSA) : MSSA(std::move(MSSA)) {}
    MemorySSA &getMSSA() { return *MSSA; }
    std::unique_ptr<MemorySSA> MSSA;
    LLVM_ABI bool invalidate(Function &F, const PreservedAnalyses &PA,
                             FunctionAnalysisManager::Invalidator &Inv);
  };
  LLVM_ABI Result run(Function &F, FunctionAnalysisManager &AM);
};
```
[llvm/include/llvm/Analysis/MemorySSA.h:L922-L943](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Analysis/MemorySSA.h#L922-L943) — `MemorySSAAnalysis`：`Result` 内含 `unique_ptr<MemorySSA>`。注意 `Result` 自己带了一个 `invalidate` 方法——这就是 AM 在失效遍历时会回调的对象方法（见 4.1 的 `Invalidator`）。这是「分析结果自定义失效逻辑」的标准写法。

`run` 实现展现了 MemorySSA 对 DT 与 AA 的双重依赖：

```cpp
// 2369-2374
MemorySSAAnalysis::Result MemorySSAAnalysis::run(Function &F,
                                                 FunctionAnalysisManager &AM) {
  auto &DT = AM.getResult<DominatorTreeAnalysis>(F);
  auto &AA = AM.getResult<AAManager>(F);
  return MemorySSAAnalysis::Result(std::make_unique<MemorySSA>(F, &AA, &DT));
}
```
[llvm/lib/Analysis/MemorySSA.cpp:L2369-L2374](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Analysis/MemorySSA.cpp#L2369-L2374) — `MemorySSAAnalysis::run`：分别 `getResult` 拿到支配树与 `AAManager`（聚合的别名分析），再用它们构造 `MemorySSA`。MemorySSA 构造期间会用 AA 来判定「这条 def 是否真的 clobber 了那条 use」。

它的 `invalidate` 是「连带失效」最直白的范例——三行就把依赖图说清楚了：

```cpp
// 2376-2383
bool MemorySSAAnalysis::Result::invalidate(
    Function &F, const PreservedAnalyses &PA,
    FunctionAnalysisManager::Invalidator &Inv) {
  auto PAC = PA.getChecker<MemorySSAAnalysis>();
  return !(PAC.preserved() || PAC.preservedSet<AllAnalysesOn<Function>>()) ||
         Inv.invalidate<AAManager>(F, PA) ||              // ← AA 失效我也失效
         Inv.invalidate<DominatorTreeAnalysis>(F, PA);    // ← DT 失效我也失效
}
```
[llvm/lib/Analysis/MemorySSA.cpp:L2376-L2383](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Analysis/MemorySSA.cpp#L2376-L2383) — `MemorySSAAnalysis::Result::invalidate`：哪怕有人声明保住了 MemorySSA，只要它依赖的 `AAManager` 或 `DominatorTreeAnalysis` 被失效，MemorySSA 就必须失效。`Inv.invalidate<...>` 正是 4.1 那个 `Invalidator` 的对外模板入口。

三种 `MemoryAccess` 的类层次：`MemoryUseOrDef` 是 `MemoryUse`/`MemoryDef` 的公共基，挂一个「定义访问」；`MemoryPhi` 用于控制流汇合。`getDefiningAccess` 取「我看见的内存版本」：

[llvm/include/llvm/Analysis/MemorySSA.h:L260](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Analysis/MemorySSA.h#L260) — `MemoryAccess *getDefiningAccess() const { return getOperand(0); }`：MemoryUse/Def 的第 0 个操作数就是它的定义访问。这与 u3-l2 讲的「`User` 的操作数数组」一脉相承——MemoryAccess 本身也是一种 `User`（`DerivedUser`）。

入口哨兵与核心查询：

[llvm/include/llvm/Analysis/MemorySSA.h:L744](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Analysis/MemorySSA.h#L744) — `inline MemoryAccess *getLiveOnEntryDef() const;`：返回代表「函数入口内存版本」的哨兵 def。

[llvm/include/llvm/Analysis/MemorySSA.h:L1035-L1039](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Analysis/MemorySSA.h#L1035-L1039) — `MemoryAccess *getClobberingMemoryAccess(const Instruction *I, ...)`：`MemorySSAWalker` 的核心查询，回答「指令 `I` 真正读到/被覆盖的那个内存访问是谁」，是 GVN/LICM 高效判定内存依赖的利器。

打印 pass 把整棵内存 SSA 树吐出来，正是 `print<memoryssa>` 的本体：

```cpp
// 2385-2399（节选）
PreservedAnalyses MemorySSAPrinterPass::run(Function &F,
                                            FunctionAnalysisManager &AM) {
  auto &MSSA = AM.getResult<MemorySSAAnalysis>(F).getMSSA();
  if (EnsureOptimizedUses)
    MSSA.ensureOptimizedUses();
  ...
  OS << "MemorySSA for function: " << F.getName() << "\n";
  MSSA.print(OS);
  return PreservedAnalyses::all();
}
```
[llvm/lib/Analysis/MemorySSA.cpp:L2385-L2399](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Analysis/MemorySSA.cpp#L2385-L2399) — `MemorySSAPrinterPass::run`：`getResult<MemorySSAAnalysis>(F)` 拿到结果，调用 `ensureOptimizedUses()` 把 use 的定义精确化后，再 `print`。注意它对 `AAManager` 的依赖是**间接**的（MemorySSAAnalysis 内部去取 AA），打印 pass 自己只声明对 MemorySSAAnalysis 的依赖。

最后看一个真实消费者——LICM。新 PM 的 `LICMPass` 通过 `LoopStandardAnalysisResults`（一个把 LoopInfo/DT/AA/MemorySSA 打包的结构）一次性拿到这些分析：

```cpp
// 309-326（节选）
PreservedAnalyses LICMPass::run(Loop &L, LoopAnalysisManager &AM,
                                LoopStandardAnalysisResults &AR, LPMUpdater &) {
  if (!AR.MSSA)
    reportFatalUsageError("LICM requires MemorySSA (loop-mssa)");
  ...
  LoopInvariantCodeMotion LICM(...);
  if (!LICM.runOnLoop(&L, &AR.AA, &AR.LI, &AR.DT, &AR.AC, &AR.TLI, &AR.TTI,
                      &AR.SE, AR.MSSA, &ORE))
    return PreservedAnalyses::all();
  auto PA = getLoopPassPreservedAnalyses();
  PA.preserve<MemorySSAAnalysis>();
  ...
}
```
[llvm/lib/Transforms/Scalar/LICM.cpp:L309-L326](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Transforms/Scalar/LICM.cpp#L309-L326) — `LICMPass::run`：开头断言「没有 MemorySSA 就直接报错」——这正是「MemorySSA 非默认、需 `loop-mssa` 声明」的体现。随后把 `AR.AA`/`AR.LI`/`AR.DT`/`AR.MSSA` 等分析一起喂给核心逻辑。结尾 `PA.preserve<MemorySSAAnalysis>()` 表示 LICM 会维护 MemorySSA 的一致性，所以可以保住它（搭配 `MemorySSAUpdater` 增量更新）。这是「变换 pass 消费 + 维护分析」的完整闭环。

#### 4.4.4 代码实践

**实践目标**：把 `demo.ll` 的内存访问建成 MemorySSA 并打印，辨认 `MemoryDef`/`MemoryUse`/`liveOnEntry`。

**操作步骤**：

```bash
opt -passes='print<memoryssa>' demo.ll -disable-output
```

**需要观察的现象**：输出形如

```
MemorySSA for function: foo
...
  %pi = ...: 1 = MemoryDef(liveOnEntry)   ; store i32 %i, ptr %pi 对应的 def
...
  %v = load i32, ptr %q: MemoryUse(liveOnEntry)   ; 这条 load 的定义落在入口
```

（实际编号与是否 optimize 以本地为准，待本地验证。）

**预期结果**：你能看到 `store ... %pi` 对应一个 `MemoryDef`，`load ... %q` 对应一个 `MemoryUse`。由于 `%p` 与 `%q` 被判为 `NoAlias`，那条 `%q` 的 load 的 clobber 会落在 `liveOnEntry`（本函数没有任何 store 写过 `%q`）。试着把 `load` 改成读 `%pi`，再跑一次，观察 `MemoryUse` 的定义是否变成了那条 `store`——这就直观展示了 MemorySSA 如何随别名关系而变。

#### 4.4.5 小练习与答案

**练习 1**：`MemoryUse` 和 `MemoryDef` 分别对应哪类内存指令？它们的 `getDefiningAccess()` 各自语义是什么？

**参考答案**：`MemoryUse` 对应读（load），`MemoryDef` 对应写（store、可能写的 call）。对 `MemoryUse`，`getDefiningAccess()` 指向「真正喂给它数据的那个 MemoryDef」（即它读到的最新版本）；对 `MemoryDef`，`getDefiningAccess()` 指向「在我执行之前、我能看到的最新内存版本」（即我建立在哪个版本之上）。

**练习 2**：MemorySSA 的 `invalidate` 为什么必须检查 `AAManager` 和 `DominatorTreeAnalysis` 两者，即使有人声明 `preserve<MemorySSAAnalysis>()`？

**参考答案**：因为 MemorySSA 的构造与精确化（optimize uses）在结构上依赖支配树（决定哪些块可达、phi 放在哪）和别名分析（决定某条 def 是否真的 clobber 了某条 use）。如果支配树或 AA 因 IR 改动而失效，缓存里的 MemorySSA 就可能建立在过期结构之上，必须一起失效重算，否则优化器会拿到错误的内存依赖。这正是「连带失效」存在的根本理由。

---

## 5. 综合实践

把本讲的三类分析串起来，做一次「分析依赖图」的阅读 + 观察任务。

**任务**：以 `demo.ll` 为对象，依次完成：

1. **画依赖图**。根据本讲源码，画出下面这条分析依赖链，标注每条边来自哪个 `getResult`/`invalidate`：

   ```
   DominatorTreeAnalysis
        ▲            ▲
        │            │
   LoopAnalysis    MemorySSAAnalysis ──▶ AAManager ──▶ BasicAA
                                                   ──▶ TargetLibraryAnalysis
                                                   ──▶ AssumptionAnalysis
                                                   ──▶ DominatorTreeAnalysis（又指回来）
   ```

   要求：每条「A 依赖 B」的边，写出对应的源码位置（如 LoopInfo.cpp:1015 的 `getResult<DominatorTreeAnalysis>`，MemorySSA.cpp:2381-2382 的 `Inv.invalidate<...>`）。

2. **跑三条打印 pass**，把结果存档：

   ```bash
   opt -passes='print<loops>'       demo.ll -disable-output  > loops.txt
   opt -passes='aa-eval'            demo.ll -disable-output  > aa.txt
   opt -passes='print<memoryssa>'   demo.ll -disable-output  > mssa.txt
   ```

   在 `loops.txt` 里确认循环结构；在 `aa.txt` 里确认 `%p`/`%q` 的 `NoAlias`；在 `mssa.txt` 里确认 `store %pi` 是 `MemoryDef`、`load %q` 是 `MemoryUse` 且 clobber 落在 `liveOnEntry`。

3. **制造一次失效**。把 `-passes` 改成会改 IR 的流水线，例如：

   ```bash
   opt -passes='loop-rotate,print<memoryssa>' demo.ll -disable-output
   ```

   `loop-rotate` 会改写循环结构（旋转），它**不会**保住 MemorySSA/LoopInfo。观察 `print<memoryssa>` 在 `loop-rotate` 之后是否仍能打印——它必须重新计算一份 MemorySSA（因为上一份已失效）。可在两条 pass 之间加 `-debug`（需断言构建）观察是否出现重新构造的痕迹。若无法运行，则据源码推断并标注「待本地验证」。

**验收标准**：你能用一句话解释「为什么 `loop-rotate` 之后 MemorySSA 会被重算」——因为 `loop-rotate` 改变了 CFG，支配树随之失效，而 MemorySSA 的 `invalidate` 会连带失效（MemorySSA.cpp:2381-2382）。

> 参考文档：别名的语义与使用方式可进一步阅读 [llvm/docs/AliasAnalysis.md](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/docs/AliasAnalysis.md)。

## 6. 本讲小结

- **分析 pass 只读不改、产出可复用结果**；它与变换 pass 的区别是理解整个优化框架的第一条线。
- **`AnalysisManager` 靠「按需计算 + 缓存 + 失效」三件套**运作：`getResult` 未命中才跑、`getCachedResult` 只读不跑、`invalidate` 据 `PreservedAnalyses` 丢弃过期结果。
- **`Invalidator` 实现「连带失效」**：分析结果可在自己的 `invalidate` 里反问依赖的分析是否失效，从而保证依赖图一致性（支配树坏 → LoopInfo/MemorySSA 跟着坏）。
- **LoopInfo 依赖支配树**，回答「有哪些循环」「某值是否循环不变」，是 LICM/Unroll 等循环优化的基石。
- **别名分析是「可组合流水线」**：`AAManager` 把 BasicAA 等多个 provider 装进 `AAResults`，查询时「首个确定性答案（非 MayAlias）胜出」，结果为 `NoAlias/MayAlias/PartialAlias/MustAlias`。
- **MemorySSA 把内存访问建模成 SSA**（`MemoryUse`/`MemoryDef`/`MemoryPhi` + `liveOnEntry`），依赖支配树与 AA，用 walker 高效回答 clobber 查询；它非默认开启，需 `loop-mssa`/`memoryssa` 声明。

## 7. 下一步学习建议

- **u4-l3 经典优化 pass**：直接承接本讲。`LICM` 会用 LoopInfo + MemorySSA 做循环外提；`GVN` 会用 MemorySSA + AA 做冗余 load 消除；`InstCombine` 虽不依赖这三者但会触发大量失效。学完 u4-l3 你能看到「分析如何被真正用起来」。
- **u4-l4 编写你自己的 pass**：你会亲手写一个 `getResult<...>` 的消费者，并决定返回什么 `PreservedAnalyses`——本讲的失效机制在那里变成你必须正确处理的约定。
- **延伸阅读**：`llvm/docs/AliasAnalysis.md`（AA 的完整语义、如何写一个自定义 AA provider）；`llvm/docs/MemorySSA.rst` 若仓库内存在则可参考，否则以 `MemorySSA.h` 顶部注释与 `MemorySSA.cpp` 的 `MemorySSA::build` 为准；支配关系可参考任意编译原理教材的「支配树 / 支配边界」章节。
