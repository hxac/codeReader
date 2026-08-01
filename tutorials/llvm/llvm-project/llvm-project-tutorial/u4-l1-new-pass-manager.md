# 新 Pass 管理器架构

## 1. 本讲目标

在 u1-l4 我们已经看到：`opt` 这个工具读入一份 LLVM IR，跑一连串「优化工序」，再吐出更优的 IR；`llc` 也是读 IR、跑一连串「代码生成工序」、吐出机器码。这些「工序」在 LLVM 里有一个统一的名字——**pass**，而把成百上千个 pass 按顺序编排、执行、并提供分析缓存的基础设施，就是 **Pass 管理器（Pass Manager）**。

LLVM 目前使用的是**新 Pass 管理器（New Pass Manager，简称 New PM）**，中端优化流水线全部建立在它之上。本讲学完后，你应该能够：

1. 说出新 PM 的两大支柱——`PassManager`（执行变换）与 `AnalysisManager`（缓存分析）——是如何分工与协作的。
2. 理解 IR 的四层单位（Module / CGSCC / Function / Loop）以及「适配器（Adaptor）」如何把高层的 pass 嵌进低层流水线。
3. 掌握 `PassBuilder` 的职责：它如何注册内置 pass、注册扩展点（Extension Point, EP）回调、把 `-passes=...` 文本解析成真正的 pass 流水线。
4. 读懂 `-passes='function(instcombine,gvn)'` 这类文本流水线语法，并知道 `opt` 是怎样驱动整个流程的。

本讲是第 4 单元「Pass 管理器与优化框架」的入口，后续 u4-l2（分析 pass）、u4-l3（经典优化 pass）、u4-l4（自己写一个 pass）都建立在本讲的概念之上。

## 2. 前置知识

在进入源码之前，先用通俗语言澄清几个反复出现的术语。

- **IR 单位（IR Unit）**：LLVM IR 是一棵有层次的树（见 u3-l1）。新 PM 把这棵树的节点抽象成四类管理单位，从大到小分别是：
  - **Module（模块）**：一整个编译单元，对应 `Module` 类。
  - **CGSCC（调用图强连通分量）**：调用图（Call Graph）上的一个强连通分量，用于过程间优化（如内联）。
  - **Function（函数）**：单个函数。
  - **Loop（循环）**：单个自然循环。
  - 层次关系是 `Module -> (CGSCC ->) Function -> Loop`，其中 CGSCC 这一层是可选的。
- **pass（工序）**：一个作用于某种 IR 单位、对 IR 进行变换或分析的算子。每个 pass 必须声明它作用于哪一层（模块级 / 函数级 / ……）。
- **变换 pass（Transformation Pass）**：会改写 IR 的 pass，例如 `instcombine`（指令合并）。它是优化效果的实际来源。
- **分析 pass（Analysis Pass）**：只观察 IR、不改写它、产出一个可被复用的「分析结果」（如支配树 DominatorTree、循环信息 LoopInfo）。分析结果可以被多个变换 pass 共享，避免重复计算。
- **流水线（Pipeline）**：一组按顺序排列的 pass。`opt` 的 `-passes=...` 就是在描述一条流水线。
- **失效（Invalidation）**：当一个变换 pass 改写了 IR，之前缓存的某些分析结果就「过期」了，必须丢弃。pass 通过返回 `PreservedAnalyses` 声明「我保住了哪些分析」。

> 提示：如果你对「pass 之间靠 IR 传递数据」「SSA 与 def-use 链」还不熟，建议先回顾 u3-l1（Module/Function/BasicBlock 层次）与 u3-l2（Value/Use），本讲会直接使用这些概念。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `llvm/include/llvm/IR/PassManager.h` | 新 PM 的核心头文件：定义 `PassManager`、`PassInfoMixin`、`AnalysisManager`、适配器代理等模板。 |
| `llvm/include/llvm/IR/PassManagerImpl.h` | `PassManager::run` 的实现：逐个执行 pass、处理分析与插桩回调。 |
| `llvm/lib/IR/PassManager.cpp` | 模板显式实例化，以及 `ModuleToFunctionPassAdaptor::run`（跨层适配器）的实现。 |
| `llvm/include/llvm/Passes/PassBuilder.h` | `PassBuilder` 类的接口：注册分析、构建默认流水线、解析文本流水线、各类扩展点回调。 |
| `llvm/lib/Passes/PassBuilder.cpp` | `PassBuilder` 的实现，重点是 `parsePassPipeline`（把文本解析成流水线）。 |
| `llvm/tools/opt/NewPMDriver.cpp` | `opt` 工具中驱动新 PM 的代码：`runPassPipeline` 函数，串起分析管理器、PassBuilder 与流水线。 |
| `llvm/docs/NewPassManager.md` | 官方「如何使用新 PM」文档，本讲大量参考它。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 新 PM 架构**：`PassManager` 与 `AnalysisManager` 的分工、四层 IR 单位与适配器、`PreservedAnalyses` 失效机制。
- **4.2 PassBuilder 与扩展点机制**：`PassBuilder` 如何注册内置 pass、注册 EP 回调、构建默认流水线。
- **4.3 `-passes` 文本流水线语法与解析**：文本格式、自动包装规则、`opt` 如何驱动整个流程。

---

### 4.1 新 PM 的整体架构：PassManager 与 AnalysisManager

#### 4.1.1 概念说明

新 PM 的设计可以用一句话概括：**「执行变换」与「缓存分析」严格分离，由两个独立的泛型类分别承担。**

- [`PassManager<IRUnitT>`](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L184-L246) 是一个**泛型容器**，模板参数 `IRUnitT` 决定它管理哪一层的 pass。它内部就是一个 pass 的列表，按顺序跑它们。`PassManager<Module>` 跑模块级 pass，`PassManager<Function>` 跑函数级 pass。为了方便，LLVM 给了别名：

  ```text
  using ModulePassManager   = PassManager<Module>;
  using FunctionPassManager = PassManager<Function>;
  ```
  （定义见 [PassManager.h:L258](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L258) 与 [PassManager.h:L267](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L267)）

- [`AnalysisManager<IRUnitT>`](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L274) 同样是泛型容器，但它装的是**分析 pass**，并且负责**惰性运行 + 缓存**它们的分析结果。一个变换 pass 在运行时会收到一个 `AnalysisManager` 引用，需要某个分析结果时就去 `getResult<某分析>()` 查询——查不到才真正运行该分析，算完缓存起来供后续 pass 复用。

> 直觉：`PassManager` 是「指挥官」，喊「下一位 pass 上场」；`AnalysisManager` 是「资料室」，谁需要分析结果都来找它要，资料过期了它会按规则清理。

一个关键点是：**「pass 管理器」本身也是一个 pass。** 一个 `FunctionPassManager` 既然能处理 Function，那它就可以被当作一个函数级 pass，嵌进 `ModulePassManager`（外面包一个适配器）。这种「管理器即 pass」的特性让流水线可以任意嵌套。

#### 4.1.2 核心流程

一条流水线从「被构造」到「被执行」的过程，可以用下面的伪代码描述：

```text
PassManager::run(IR, AM):
    PA = PreservedAnalyses::all()          # 初始：假设一切都被保住
    PI = AM.getResult<PassInstrumentationAnalysis>()   # 拿到插桩回调
    for Pass in Passes:                    # 逐个执行 pass
        if not PI.runBeforePass(Pass, IR): # 插桩：允许跳过该 pass
            continue
        PassPA = Pass.run(IR, AM)          # 运行 pass，它声明保住了哪些分析
        AM.invalidate(IR, PassPA)          # 据此丢弃过期分析
        PI.runAfterPass(Pass, IR, PassPA)
        PA.intersect(PassPA)               # 累计：取交集
    return PA
```

这段伪代码几乎一一对应 `PassManager::run` 的真实实现（见下文源码精读）。

#### 4.1.3 源码精读

**(1) pass 的混入基类 `PassInfoMixin`。** 新 PM 的 pass 不再继承一个庞大的虚基类，而是用 **CRTP（Curiously Recurring Template Pattern，奇异递归模板）** 混入元信息：通过模板自动提供 `name()`（pass 名字）和 `isRequired()`（是否不可跳过）等方法。

```cpp
template <typename DerivedT>
struct PassInfoMixin : detail::InfoMixin<DerivedT> {
  void printPipeline(raw_ostream &OS, ...);
  // TODO: remove once out of tree users are updated.
  static bool isRequired() { return false; }
};
```
这段定义见 [PassManager.h:L88-L99](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L88-L99)。它有两个子混入：`RequiredPassInfoMixin`（`isRequired()` 返回 `true`，pass 永不跳过）与 `OptionalPassInfoMixin`（可被跳过），见 [PassManager.h:L102-L111](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L102-L111)。一个典型的新 PM 变换 pass 只需 `struct MyPass : PassInfoMixin<MyPass> { PreservedAnalyses run(Function &F, FunctionAnalysisManager &); }`，这正是 u4-l4 会的写法。

**(2) `PassManager` 容器本身。** 它的核心字段就一个 `Passes` 向量：

```cpp
template <typename IRUnitT, typename AnalysisManagerT = AnalysisManager<IRUnitT>,
          typename... ExtraArgTs>
class PassManager : public RequiredPassInfoMixin<...> {
  PreservedAnalyses run(IRUnitT &IR, AnalysisManagerT &AM, ExtraArgTs... ExtraArgs);
  template <typename PassT>
  void addPass(PassT &&Pass) { ... Passes.push_back(...); }
  bool isEmpty() const { return Passes.empty(); }
protected:
  std::vector<typename PassConceptT::unique_ptr> Passes;   // pass 列表
};
```
见 [PassManager.h:L184-L246](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L184-L246)。

注意两个细节：

- **类型擦除（Type Erasure）**：`Passes` 存的是 `PassConcept` 的 `unique_ptr`（一种「概念+模型」的设计模式），所以一个 `PassManager` 可以装**不同具体类型**的 pass——只要它们都作用于同一层 IR。这正是「流水线里能混入各种 pass」的根基。
- **`addPass` 的特例**：当你把一个 `PassManager` 加进另一个**同类型**的 `PassManager` 时，它会把内层的 pass 全部「拍平」搬进外层，而不是真正嵌套执行（见 [PassManager.h:L231-L236](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L231-L236)）。这简化了实现、避免了重复失效。

**(3) `PassManager::run` 的运行主循环。** 实现 `PassManager.h` 中声明、`PassManagerImpl.h` 中定义，逐行对应 4.1.2 的伪代码：

```cpp
PreservedAnalyses PA = PreservedAnalyses::all();
PassInstrumentation PI = detail::getAnalysisResult<PassInstrumentationAnalysis>(AM, IR, ...);
for (auto &Pass : Passes) {
  if (!PI.runBeforePass<IRUnitT>(*Pass, IR))   // 插桩：可跳过
    continue;
  PreservedAnalyses PassPA = Pass->run(IR, AM, ExtraArgs...);  // 运行 pass
  AM.invalidate(IR, PassPA);                   // 丢弃过期分析
  PI.runAfterPass<IRUnitT>(*Pass, IR, PassPA);
  PA.intersect(std::move(PassPA));             // 累计保住的分析
}
```
见 [PassManagerImpl.h:L28-L89](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManagerImpl.h#L28-L89)。其中 `PI.runBeforePass` / `PI.runAfterPass` 是**插桩回调（Pass Instrumentation）**——用来支持 `-print-after-all`、计时、验证、`-debug-pass-manager` 等功能，由 `PassInstrumentationCallbacks` 注册，本讲后面在 `opt` 一节会看到。

**(4) `AnalysisManager` 的惰性查询与缓存。** 它对外暴露两个核心方法：

```cpp
// 取分析结果；没有缓存就立即运行分析并缓存
template <typename PassT>
typename PassT::Result &getResult(IRUnitT &IR, ExtraArgTs... ExtraArgs);
// 只取已缓存的结果，从不触发运行；没有就返回 nullptr
template <typename PassT>
typename PassT::Result *getCachedResult(IRUnitT &IR) const;
```
见 [PassManager.h:L430-L442](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L430-L442)（`getResult`）与 [PassManager.h:L449-L463](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L449-L463)（`getCachedResult`）。注册分析用 `registerPass`，传入一个返回分析对象的 lambda（见 [PassManager.h:L491-L505](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L491-L505)）。

> 关键结论：分析是「按需计算 + 全程缓存」，谁先要谁就先算，后面的人免费复用。这就是 LLVM 能把昂贵的分析（支配树、别名分析等）控制在可接受编译时间内的原因。

**(5) `PreservedAnalyses` 与失效机制。** 每个 pass 的 `run` 返回一个 `PreservedAnalyses`，告诉管理器「我改完之后哪些分析还成立」。常见写法（摘自 [NewPassManager.md:L284-L300](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/docs/NewPassManager.md#L284-L300)）：

```cpp
return PreservedAnalyses::all();        // 没动任何会影响分析的东西
return PreservedAnalyses::none();       // 改了 IR，懒得维护，全失效
PreservedAnalyses PA;
PA.preserve<DominatorAnalysis>();       // 我顺手维护了支配树，其余失效
PA.preserveSet<CFGAnalyses>();          // 没改控制流，所有只关心 CFG 的分析仍有效
return PA;
```

`PassManager` 拿到 `PassPA` 后调用 `AM.invalidate(IR, PassPA)`（声明在 [PassManager.h:L511](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L511)）。失效的规则可以精确到「某一项分析」或「某一层全部分析」甚至「所有只依赖 CFG 的分析」，从而**尽可能少地丢弃缓存**，降低编译时间。失效逻辑里有个集合运算：若把「保住的分析」看作集合，多个 pass 累计保住的就是逐次取交集（`PA.intersect(PassPA)`）。

\[ \mathrm{Preserved}_{\text{总}} = \bigcap_{i} \mathrm{Preserved}_{\text{pass}_i} \]

也就是说，一条流水线最终保住的分析，是其中每个 pass 保住分析的交集——只要有任何一个 pass 没保住某分析，它就会被失效。

**(6) 跨层适配器：把高层 pass 嵌进低层流水线。** 由于 `instcombine` 是**函数级** pass，而 `opt` 的顶层 `PassManager` 是**模块级**的，直接把 `instcombine` 塞进模块管理器是行不通的——需要一个「适配器（Adaptor）」：它本身是一个**模块级 pass**，运行时遍历模块里所有函数，对每个函数调用内层那个函数级 pass。`ModuleToFunctionPassAdaptor` 就是这样的适配器，它的 `run` 实现：

```cpp
PreservedAnalyses ModuleToFunctionPassAdaptor::run(Module &M, ModuleAnalysisManager &AM) {
  FunctionAnalysisManager &FAM =
      AM.getResult<FunctionAnalysisManagerModuleProxy>(M).getManager();  // 取出函数级分析管理器
  PassInstrumentation PI = AM.getResult<PassInstrumentationAnalysis>(M);
  PreservedAnalyses PA = PreservedAnalyses::all();
  for (Function &F : M) {            // 遍历模块里的每个函数
    if (F.isDeclaration()) continue; // 跳过纯声明
    if (!PI.runBeforePass<Function>(*Pass, F)) continue;
    PreservedAnalyses PassPA = Pass->run(F, FAM);  // 对该函数跑内层 pass
    FAM.invalidate(F, EagerlyInvalidate ? PreservedAnalyses::none() : PassPA);
    PI.runAfterPass(*Pass, F, PassPA);
    PA.intersect(std::move(PassPA));
  }
  PA.preserveSet<AllAnalysesOn<Function>>();   // 我们手动做完了函数级失效
  PA.preserve<FunctionAnalysisManagerModuleProxy>();
  return PA;
}
```
见 [PassManager.cpp:L107-L149](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/PassManager.cpp#L107-L149)。

> 这段代码揭示了三件事：
> 1. **适配器实现了「跨层下钻」**：模块级 pass → 内层函数级 pass。同理还有 `ModuleToPostOrderCGSCCPassAdaptor`、`CGSCCToFunctionPassAdaptor`、`createFunctionToLoopPassAdaptor` 等，构成完整的层次嵌套链（见 [NewPassManager.md:L69-L93](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/docs/NewPassManager.md#L69-L93)）。
> 2. **跨层分析靠「代理（Proxy）」**：`FunctionAnalysisManagerModuleProxy` 是一个模块级分析，它的「结果」就是把函数级 `AnalysisManager` 暴露出来，让模块层的代码能拿到函数级管理器。这一机制由 `crossRegisterProxies` 统一接线（见下文 4.2）。
> 3. **失效被逐层精确处理**：函数级 pass 改写某个函数后，只会失效该函数的分析，不会波及其他函数。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：通过追踪 `PassManager::run` 的主循环，建立「pass 一个接一个执行、每次都更新分析缓存」的直觉。
2. **操作步骤**：
   - 打开 [PassManagerImpl.h:L56-L89](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManagerImpl.h#L56-L89)，找到 `PA = PreservedAnalyses::all()` 这行。
   - 顺着 `for (auto &Pass : Passes)` 把循环体读一遍，确认它与 4.1.2 的伪代码一致。
   - 再打开 [PassManager.cpp:L107-L149](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/PassManager.cpp#L107-L149) 的 `ModuleToFunctionPassAdaptor::run`，对比两者结构——你会发现适配器的内层循环和 `PassManager::run` 几乎是同构的。
3. **需要观察的现象**：两段「运行主循环」在结构上的高度相似——`runBeforePass` → `Pass->run` → `invalidate` → `runAfterPass` → `intersect`。
4. **预期结果**：你能用自己的话解释「为什么适配器在 `run` 末尾要 `preserveSet<AllAnalysesOn<Function>>()`」——因为它在循环里已经手动对每个函数调用了 `FAM.invalidate`，外层管理器无需再对函数级分析做任何事。
5. 待本地验证（无需运行命令，纯阅读）。

#### 4.1.5 小练习与答案

**练习 1**：一个变换 pass 改写了某个函数的控制流（删了一个基本块），但没有删任何指令的运算语义。它应当返回哪种 `PreservedAnalyses` 最合适？

> **参考答案**：它改了 CFG，所以支配树、循环信息等「依赖 CFG 的分析」会失效；但它没有改变内存访问语义。最合适的是 `PreservedAnalyses PA; PA.preserveSet<CFGAnalyses>();` 的反面——即不保住 CFG 类分析，但可以用 `PA.preserve<某具体分析>()` 单独保住确实仍有效的分析。最保守省事的是返回 `PreservedAnalyses::none()`（全失效，正确但牺牲性能）。

**练习 2**：为什么新 PM 要把 `PassManager` 和 `AnalysisManager` 设计成两个独立的类，而不是合并？

> **参考答案**：因为「执行变换」与「缓存分析」的生命周期与访问模式不同——变换 pass 是一次性顺序执行，而分析结果要被多个 pass 共享、要支持惰性计算与按规则失效。分离后，`AnalysisManager` 可以专注于「按需计算 + 缓存 + 精确失效」这一件事，避免每个 pass 自己重复管理分析；同时也为未来可能的并发（在不同函数上并行跑 pass）留出空间。

---

### 4.2 PassBuilder 与扩展点机制

#### 4.2.1 概念说明

如果说 `PassManager` 是「执行引擎」，那 [`PassBuilder`](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Passes/PassBuilder.h#L114) 就是「装配车间」。它解决三个问题：

1. **注册**：把 LLVM 自带的上百个分析 pass「装填」进各个 `AnalysisManager`。
2. **构建默认流水线**：根据优化等级（`-O1/-O2/-O3`）拼出那条长长的高质量默认流水线，省去人手写几百个 pass 的麻烦。
3. **扩展点（Extension Point, EP）**：允许前端（如 Clang）、后端（如 AMDGPU）、或 pass 插件，在不修改 LLVM 源码的前提下，往默认流水线的特定位置**注入自己的 pass**。

`PassBuilder` 还持有「装配 pass 时可用的基础状态」，见其私有成员（[PassBuilder.h:L115-L119](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Passes/PassBuilder.h#L115-L119)）：目标机 `TargetMachine *TM`、流水线调参 `PipelineTuningOptions PTO`、PGO 选项、插桩回调 `PIC`、虚拟文件系统 `FS`。

#### 4.2.2 核心流程

用 `PassBuilder` 跑一遍默认 `-O2` 流水线的「标准五步」如下（摘自官方文档 [NewPassManager.md:L12-L40](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/docs/NewPassManager.md#L12-L40)）：

```cpp
// 1) 创建四层分析管理器（声明顺序 = 析构顺序，因存在跨层引用）
LoopAnalysisManager LAM;
FunctionAnalysisManager FAM;
CGSCCAnalysisManager CGAM;
ModuleAnalysisManager MAM;

PassBuilder PB;                                  // 2) 建 PassBuilder

// 3) 把内置分析装进各层管理器
PB.registerModuleAnalyses(MAM);
PB.registerCGSCCAnalyses(CGAM);
PB.registerFunctionAnalyses(FAM);
PB.registerLoopAnalyses(LAM);
PB.crossRegisterProxies(LAM, FAM, CGAM, MAM);    // 4) 跨层接线（注册代理）

// 5) 构造 -O2 默认流水线并执行
ModulePassManager MPM = PB.buildPerModuleDefaultPipeline(OptimizationLevel::O2);
MPM.run(MyModule, MAM);
```

第 4 步 `crossRegisterProxies` 是「跨层分析」的关键接线，它的实现把各层代理分析注册进对应管理器：

```cpp
MAM.registerPass([&] { return FunctionAnalysisManagerModuleProxy(FAM); }); // 模块层能看到函数层
MAM.registerPass([&] { return CGSCCAnalysisManagerModuleProxy(CGAM); });
CGAM.registerPass([&] { return ModuleAnalysisManagerCGSCCProxy(MAM); });   // CGSCC 层能看到模块层
FAM.registerPass([&] { return CGSCCAnalysisManagerFunctionProxy(CGAM); });
FAM.registerPass([&] { return ModuleAnalysisManagerFunctionProxy(MAM); });
FAM.registerPass([&] { return LoopAnalysisManagerFunctionProxy(LAM); });   // 函数层能看到循环层
LAM.registerPass([&] { return FunctionAnalysisManagerLoopProxy(FAM); });
```
见 [PassBuilder.cpp:L2675-L2697](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Passes/PassBuilder.cpp#L2675-L2697)，接口声明在 [PassBuilder.h:L146-L149](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Passes/PassBuilder.h#L146-L149)。

> 直觉：跨层「看到」是**单向受限**的。内层 pass（如函数级）只能**读取**外层（如模块级）的**已缓存且不可变**分析（用 `getCachedResult`），不能触发外层分析重新计算——这是为了防止编译时间二次方爆炸，也为未来并发留余地（详见 [NewPassManager.md:L220-L247](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/docs/NewPassManager.md#L220-L247)）。

#### 4.2.3 源码精读

**(1) 一组 `buildXxxPipeline` 方法。** `PassBuilder` 用一系列方法构造不同场景的默认流水线，例如：

```cpp
// 单模块、非 LTO 的默认流水线，对应前端 -O1/-O2/-O3
ModulePassManager buildPerModuleDefaultPipeline(OptimizationLevel Level,
        ThinOrFullLTOPhase Phase = ThinOrFullLTOPhase::None);
```
见 [PassBuilder.h:L258-L260](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Passes/PassBuilder.h#L258-L260)。同类还有 `buildModuleOptimizationPipeline`、`buildThinLTODefaultPipeline`、`buildLTOPreLinkDefaultPipeline`、`buildO0DefaultPipeline` 等（见 [PassBuilder.h:L249-L315](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Passes/PassBuilder.h#L249-L315)）。这些方法内部都会在固定位置触发扩展点回调。

**(2) 扩展点回调（EP Callbacks）。** `PassBuilder` 暴露了一大批 `registerXxxEPCallback` 方法，每个对应流水线里的一个「插槽」。注册的回调会在 `buildXxxPipeline` 拼装到该位置时被调用，调用者就能往里 `addPass`。例如：

```cpp
// 在默认流水线的最开头插入（LTO/ThinLTO 链接期不适用）
void registerPipelineStartEPCallback(
    const std::function<void(ModulePassManager &, OptimizationLevel)> &C);

// 在函数优化流水线的最末尾插入
void registerOptimizerLastEPCallback(
    const std::function<void(ModulePassManager &, OptimizationLevel,
                             ThinOrFullLTOPhase Phase)> &C);
```
见 [PassBuilder.h:L501-L504](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Passes/PassBuilder.h#L501-L504) 与 [PassBuilder.h:L530-L534](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Passes/PassBuilder.h#L530-L534)。还有针对函数级的 `registerPeepholeEPCallback`（[L424-L427](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Passes/PassBuilder.h#L424-L427)）、循环级的 `registerLateLoopOptimizationsEPCallback`（[L438-L441](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Passes/PassBuilder.h#L438-L441)）、向量化前后的 EP 等等，覆盖了流水线的各个阶段。

官方文档给了一个最小用法（[NewPassManager.md:L154-L160](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/docs/NewPassManager.md#L154-L160)）：

```cpp
PassBuilder PB;
PB.registerPipelineStartEPCallback(
    [&](ModulePassManager &MPM, PassBuilder::OptimizationLevel Level) {
      MPM.addPass(FooPass());
    });
```
此后该 `PB` 构造出的任何默认流水线，开头都会插一个 `FooPass`。

> EP 是「二次开发」的核心入口：前端用它注入 sanitizer（Clang 的 `BackendUtil.cpp`）、后端用它注入目标专属 pass（如 `AMDGPUTargetMachine::registerPassBuilderCallbacks()`）、pass 插件用它在不重编 LLVM 的情况下扩展流水线（见 u9-l2）。文档指出：若 `PassBuilder` 持有 `TargetMachine`，会自动调用 `TargetMachine::registerPassBuilderCallbacks()` 让后端注册 EP（[NewPassManager.md:L166-L168](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/docs/NewPassManager.md#L166-L168)）。

**(3) 流水线解析回调。** 除了 EP，`PassBuilder` 还有 `registerPipelineParsingCallback`，用于**让插件/外部代码注册自定义 pass 名字**，使 `-passes=my-custom-pass` 能被解析。它对每一层都有重载：

```cpp
void registerPipelineParsingCallback(
    const std::function<bool(StringRef Name, ModulePassManager &,
                             ArrayRef<PipelineElement>)> &C);
// 同样有 Function / CGSCC / Loop / MachineFunction 版本
```
见 [PassBuilder.h:L590-L614](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Passes/PassBuilder.h#L590-L614)。这正是 u4-l4（自己写 pass 并注册到 `-passes`）和 u9-l2（pass 插件）会用到的钩子。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：理解「默认流水线 + EP 回调」是如何组装出来的，建立一个 EP 的心智坐标。
2. **操作步骤**：
   - 打开 [PassBuilder.h:L424-L552](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Passes/PassBuilder.h#L424-L552)，把所有 `register...EPCallback` 方法的注释读一遍，注意每个 EP 注释里写明「在流水线哪个位置插入」「插入的 pass 必须是哪一层」。
   - 列一张表：`PipelineStartEP`（模块级，流水线开头）、`OptimizerEarlyEP`（模块级，函数优化之前）、`PeepholeEP`（函数级，每次指令合并之后）、`VectorizerStartEP`（函数级，向量化之前）……
3. **需要观察的现象**：每个 EP 都明确绑定了**一层 IR 单位**——回调签名里第二个参数要么是 `ModulePassManager&`，要么是 `FunctionPassManager&` 或 `LoopPassManager&`。
4. **预期结果**：你能回答「我想在向量化之前插入一个函数级 pass，该用哪个 EP」——答案是 `registerVectorizerStartEPCallback`。
5. 待本地验证（纯阅读）。

#### 4.2.5 小练习与答案

**练习 1**：`crossRegisterProxies` 注册的那些 `XxxProxy` 分析，本质上是干什么的？

> **参考答案**：它们是「跨层分析管理器的代理」。例如 `FunctionAnalysisManagerModuleProxy` 是一个**模块级分析**，其「结果」把函数级 `AnalysisManager` 的引用暴露出来；这样模块层的适配器在遍历函数时，就能拿到对应的函数级管理器去运行/失效函数级分析。它们同时承担「把外层失效传播到内层」的职责（见 [PassManager.cpp:L31-L94](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/PassManager.cpp#L31-L94) 的特化 `invalidate`）。

**练习 2**：扩展点（EP）与流水线解析回调（`registerPipelineParsingCallback`）有何区别？

> **参考答案**：EP 用于往**默认流水线**（`buildPerModuleDefaultPipeline` 等构造的）的固定插槽插入 pass，调用时机是「拼装默认流水线时」；而解析回调用于**让 `-passes` 文本里的某个名字被识别为一个合法 pass**，调用时机是「解析用户写的文本流水线时」。前者扩展的是「默认行为」，后者扩展的是「文本词表」。

---

### 4.3 `-passes` 文本流水线语法与解析

#### 4.3.1 概念说明

前面两节讲的都是「编程式」用法（写 C++ 拼 pass）。而 `opt` 命令行的 `-passes=...` 提供了一种**声明式**用法：用一段文本描述流水线，由 `PassBuilder::parsePassPipeline` 解析成真正的 `PassManager`。这是调试单个 pass、写测试用例时最常用的方式。

文本流水线的设计围绕一个递归结构 `PipelineElement`（[PassBuilder.h:L130-L133](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Passes/PassBuilder.h#L130-L133)）：

```cpp
struct PipelineElement {
  StringRef Name;                       // 一个 pass 名 或 一个流水线类型名
  std::vector<PipelineElement> InnerPipeline;  // 它内部的嵌套流水线（pass 则为空）
};
```

也就是说，一条流水线就是「一串名字」，每个名字可能自己又「包含」一条子流水线。

#### 4.3.2 核心流程：文本格式

官方文档（[NewPassManager.md:L412-L483](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/docs/NewPassManager.md#L412-L483)）把语法总结为几条规则：

```text
# 顶层用逗号分隔 pass；圆括号表示嵌套
opt -passes='pass1,pass2' a.ll -S
# -p 是 -passes 的别名
opt -p pass1,pass2 a.ll -S
```

**规则一：显式嵌套。** 新 PM 通常要求显式写出层级嵌套。例如先跑一个函数级 pass、再跑一个模块级 pass：

```text
opt -passes='function(no-op-function),no-op-module' a.ll -S
```

层级顺序是 `module (-> cgscc) -> function -> loop`，CGSCC 可选。一个完整而啰嗦的例子：

```text
opt -passes='no-op-module,cgscc(no-op-cgscc,function(no-op-function,loop(no-op-loop))),function(no-op-function,loop(no-op-loop))' a.ll -S -debug-pass-manager
```

**规则二：自动包装（隐式嵌套）。** 为了方便调试，文档规定了两类简化（[NewPassManager.md:L444-L464](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/docs/NewPassManager.md#L444-L464)）：

- 若**第一个 pass 不是模块级**，会自动套一层对应类型的 pass 管理器：
  ```text
  opt -passes='no-op-function,no-op-function'   # 等价于
  opt -passes='function(no-op-function,no-op-function)'
  ```
- 若某个 pass 存在「让它能塞进上一个 pass 管理器」的适配器，也会自动创建：
  ```text
  opt -passes='no-op-function,no-op-loop'   # 等价于
  opt -passes='no-op-function,loop(no-op-loop)'
  ```

**规则三：可用名字查询。** 用 `opt --print-passes` 可列出全部可用 pass 与分析及其所属层级；权威名单在 `PassRegistry.def` 文件（[NewPassManager.md:L466-L473](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/docs/NewPassManager.md#L466-L473)）。

**规则四：强制预先计算分析。** `require<分析名>` 会插入一个「仅请求运行该分析」的 pass，用于保证后续 pass 能直接拿到缓存。它同样受嵌套规则约束（[NewPassManager.md:L475-L483](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/docs/NewPassManager.md#L475-L483)）。

> 错误示范：层级写错会得到清晰报错，例如 `opt -passes='no-op-function,no-op-module'` 会报 `unknown function pass 'no-op-module'`——因为第一个 pass 是函数级，于是整条流水线被自动包进 `function(...)`，而 `no-op-module` 不是函数级 pass（[NewPassManager.md:L435-L440](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/docs/NewPassManager.md#L435-L440)）。

#### 4.3.3 源码精读

**(1) `parsePassPipeline` 的自动包装逻辑。** 入口是针对 `ModulePassManager` 的重载：

```cpp
Error PassBuilder::parsePassPipeline(ModulePassManager &MPM, StringRef PipelineText) {
  auto Pipeline = parsePipelineText(PipelineText);   // 先把文本切成 PipelineElement 树
  if (!Pipeline || Pipeline->empty())
    return make_error<StringError>(formatv("invalid pipeline '{}'", PipelineText)...);

  StringRef FirstName = Pipeline->front().Name;
  // 若第一个名字不是模块级 pass，自动包一层
  if (!isModulePassName(FirstName, ModulePipelineParsingCallbacks)) {
    if (isCGSCCPassName(FirstName, ...))        Pipeline = {{"cgscc", std::move(*Pipeline)}};
    else if (isFunctionPassName(FirstName, ...)) Pipeline = {{"function", std::move(*Pipeline)}};
    else if (isLoopNestPassName(...))           Pipeline = {{"function",{{"loop(-mssa)",...}}}};
    else if (isLoopPassName(...))               Pipeline = {{"function",{{"loop(-mssa)",...}}}};
    else if (isMachineFunctionPassName(...))    Pipeline = {{"function",{{"machine-function",...}}}};
    else { /* 回退到顶层解析回调，再不行就报 unknown name */ }
  }
  return parseModulePassPipeline(MPM, *Pipeline);
}
```
见 [PassBuilder.cpp:L2711-L2759](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Passes/PassBuilder.cpp#L2711-L2759)。这段代码精确对应 4.3.2 的「规则二」：`instcombine,gvn` 这种纯函数级写法，正是被 `isFunctionPassName` 命中、自动包成 `function(instcombine,gvn)`。注意它还会查询 `isFunctionPassName` 等「is…Name」函数，这些函数内部会同时检查内置名字表和用户注册的 `XxxPipelineParsingCallbacks`——这就是 4.2 提到的「解析回调」发挥作用的地方。

解析好层级后，逐层调用 `parseModulePassPipeline` / `parseFunctionPassPipeline` 把名字翻译成真正的 pass 对象（见 [PassBuilder.cpp:L2657-L2664](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Passes/PassBuilder.cpp#L2657-L2664) 的函数层版本）。

**(2) `opt` 如何驱动这一切：`runPassPipeline`。** 这是把本讲三节串起来的「总装函数」。它的主干（省略 PGO 等细节）正是 4.2.2 的「标准五步」加上「解析 + 运行」：

```cpp
bool llvm::runPassPipeline(StringRef Arg0, Module &M, ..., StringRef PassPipeline, ...) {
  // 1) 创建四层分析管理器
  LoopAnalysisManager LAM;
  FunctionAnalysisManager FAM;
  CGSCCAnalysisManager CGAM;
  ModuleAnalysisManager MAM;
  ...
  PassInstrumentationCallbacks PIC;
  StandardInstrumentations SI(M.getContext(), DebugPM != DebugLogging::None, ...);
  SI.registerCallbacks(PIC, &MAM);                  // 注册计时/打印/验证等插桩

  PipelineTuningOptions PTO;
  PassBuilder PB(TM, PTO, P, &PIC);                 // 2) 建 PassBuilder
  registerEPCallbacks(PB);                          //    把 -passes-ep-* 文本注册为 EP
  for (auto &PassPlugin : PassPlugins)              //    让插件注册回调
    PassPlugin.registerPassBuilderCallbacks(PB);

  // 3) 注册内置分析 + 跨层接线
  PB.registerModuleAnalyses(MAM);
  PB.registerCGSCCAnalyses(CGAM);
  PB.registerFunctionAnalyses(FAM);
  PB.registerLoopAnalyses(LAM);
  PB.crossRegisterProxies(LAM, FAM, CGAM, MAM);

  ModulePassManager MPM;
  // 4) 解析 -passes 文本
  if (!PassPipeline.empty())
    if (auto Err = PB.parsePassPipeline(MPM, PassPipeline)) { ... return false; }
  ...
  // 5) 运行
  MPM.run(M, MAM);
}
```
见 [NewPMDriver.cpp:L416-L511](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/tools/opt/NewPMDriver.cpp#L416-L511)（四层管理器创建 L416-L419，PassBuilder 构造与回调 L461-L475，分析注册与接线 L491-L495，解析流水线 L506-L511）。

其中 `-debug-pass-manager` 选项定义在 [NewPMDriver.cpp:L78-L88](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/tools/opt/NewPMDriver.cpp#L78-L88)，它会打开插桩里的「打印 pass 执行」回调，让你实时看到「正在函数 f 上运行 instcombine」这类信息，是观察流水线行为的第一利器。

> 一个值得注意的细节：`runPassPipeline` 还支持 `-print-pipeline`，它会把构造好的 `MPM` 反向序列化回 `-passes` 文本（[NewPMDriver.cpp:L555-L563](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/tools/opt/NewPMDriver.cpp#L555-L563)），并自检这段文本能否被 `parsePassPipeline` 重新解析（[NewPMDriver.cpp:L565-L573](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/tools/opt/NewPMDriver.cpp#L565-L573)）。这就是「`-O2` 到底跑了哪些 pass」的标准查法：`opt -passes='default<O2>' -print-pipeline`。

#### 4.3.4 代码实践（命令行型，可操作）

这是本讲的主实践，目标是用 `opt -passes='instcombine,gvn'` 处理一段 IR，并对照源码理解这条流水线如何被 `PassBuilder` 解析与执行。

1. **实践目标**：验证「`instcombine,gvn` 会被自动包成 `function(instcombine,gvn)`」，并观察 `instcombine`（指令合并）与 `gvn`（全局值编号）的优化效果。
2. **操作步骤**：

   准备一份带冗余的 IR，存为 `t.ll`（**示例代码**）：

   ```ll
   define i32 @test(i32 %x) {
   entry:
     %a = add i32 %x, 0          ; 加 0，instcombine 可化简为 %x
     %cond = icmp sgt i32 %x, 0
     br i1 %cond, label %then, label %else

   then:
     %t = mul i32 %x, 1          ; 乘 1，instcombine 可化简为 %x
     br label %merge

   else:
     %e = mul i32 %x, 1          ; 与 %t 完全相同的计算
     br label %merge

   merge:
     %r = phi i32 [ %t, %then ], [ %e, %else ]
     %s = add i32 %r, %a
     ret i32 %s
   }
   ```

   然后依次执行（**假设你已按 u1-l3 构建出 `opt`**）：

   ```bash
   # (a) 只看流水线结构：把 -O2 默认流水线反序列化成文本（对照理解 EP 在哪里）
   opt -passes='default<O2>' -print-pipeline -disable-output t.ll

   # (b) 观察执行顺序：-debug-pass-manager 会打印每个 pass 的运行
   opt -passes='instcombine,gvn' -debug-pass-manager -S t.ll -o t.opt.ll

   # (c) 看优化前后对比
   cat t.ll
   cat t.opt.ll
   ```

3. **需要观察的现象**：
   - 在 (b) 的 `-debug-pass-manager` 输出里，应能看到形如 `Running pass: InstCombinePass on function: test` 与 `Running pass: GVN on function: test`，且它们都在一个 `FunctionPassManager` 之下——这印证了 `instcombine,gvn` 被自动包成了 `function(instcombine,gvn)`。
   - 在 (c) 的 `t.opt.ll` 里，`add i32 %x, 0`、`mul i32 %x, 1` 这些冗余应被 `instcombine` 化简；`%t` 与 `%e` 两处相同计算经 `gvn` 后应合并为一处，`phi` 可能随之简化或消失。
4. **预期结果**：`t.opt.ll` 中 `@test` 的指令数明显减少，语义保持等价（仍计算 `(x>0 ? x : x) + x + 0`，即 `2*x`，但优化器是否收敛到最简形式取决于 pass 组合，**精确的化简结果待本地验证**）。
5. 对照源码：在 (b) 看到的「自动包成 function(...)」行为，对应 [PassBuilder.cpp:L2719-L2754](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Passes/PassBuilder.cpp#L2719-L2754) 的 `isFunctionPassName` 分支；而 pass 被逐个执行的过程，对应 4.1.3 讲的 [PassManagerImpl.h:L56-L89](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManagerImpl.h#L56-L89) 主循环。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `opt -passes='instcombine,gvn'` 能跑通，而 `opt -passes='instcombine,no-op-module'` 会报错？

> **参考答案**：第一个 pass `instcombine` 是函数级，于是 `parsePassPipeline` 把整条流水线自动包进 `function(...)`。在函数级管理器里，`instcombine` 合法，但 `no-op-module` 是模块级 pass，不属于函数级，于是 `parseFunctionPass` 找不到它而报 `unknown function pass 'no-op-module'`。要让两者共存，需显式分层：`opt -passes='function(instcombine),no-op-module'`。

**练习 2**：`require<loops>` 写在 `-passes` 里有什么用？

> **参考答案**：它插入一个「只请求运行 `LoopAnalysis`」的 pass，强制在该位置把循环信息算出来并缓存。这样其后紧跟的、需要循环信息的 pass 就能直接从 `AnalysisManager` 拿到结果，不必各自触发计算。它同样受嵌套规则约束，例如要为所有函数预先算循环信息应写 `function(require<loops>),my-module-pass`。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「全景追踪」小任务：

1. 用一段最简 C 代码生成 IR 作为输入：
   ```bash
   echo 'int f(int x){ return x*1 + x*1; }' | clang -x c - -S -emit-llvm -o big.ll
   ```
   （若没有 clang，手写等价 `.ll` 亦可。）
2. 运行 `opt -passes='default<O2>' -print-pipeline -disable-output big.ll`，把输出的那条长流水线文本保存下来。在这条文本里**圈出**至少三处你能辨认的结构：一处 `function(...)` 适配器、一处 `cgscc(...)`（内联相关）、一处循环 pass（如 `loop(...)`）。
3. 选其中两个函数级 pass（如 `instcombine`、`gvn` 或 `simplifycfg`），单独组成 `opt -passes='A,B'` 跑一遍 `big.ll`，用 `-debug-pass-manager` 观察执行顺序，并用 `-S` 对比前后 IR。
4. 回答：在步骤 2 的默认流水线里，`instcombine` 出现了多次——结合 4.2 讲的扩展点，解释「为什么要在 `PeepholeEP`（每次指令合并之后）反复跑」（提示：因为其他 pass 会不断制造新的可化简模式，instcombine 起到「规范化/兜底」作用）。

**交付物**：一张标注好的 `-print-pipeline` 输出截图/文本，加一段说明，指出你识别出的 `function(...)`、`cgscc(...)`、`loop(...)` 各一段，以及你对「instcombine 反复出现」的解释。

> 说明：若当前环境没有可运行的 `opt`/`clang`，步骤 2 的等价做法是阅读 [PassBuilder.cpp](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Passes/PassBuilder.cpp) 中 `buildModuleOptimizationPipeline` 的实现，从源码里找出 `addPass` 的调用顺序，这同样能回答「默认流水线里有哪些 pass、大致什么顺序」——只是具体运行现象「待本地验证」。

## 6. 本讲小结

- 新 PM 的两大支柱是 [`PassManager`](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L184-L246)（顺序执行变换 pass）与 [`AnalysisManager`](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManager.h#L274)（惰性计算并缓存分析、按 `PreservedAnalyses` 精确失效）；二者靠 `PassManager::run` 主循环协作（[PassManagerImpl.h:L28-L89](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/PassManagerImpl.h#L28-L89)）。
- IR 分四层 `Module -> (CGSCC ->) Function -> Loop`；跨层由**适配器**（如 `ModuleToFunctionPassAdaptor`）下钻、由**代理分析**（经 `crossRegisterProxies` 接线）跨层共享分析。
- 每个 pass 用 CRTP 混入 `PassInfoMixin` 提供元信息，返回 `PreservedAnalyses` 声明保住了哪些分析；累计保住的是所有 pass 的**交集**。
- [`PassBuilder`](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Passes/PassBuilder.h#L114) 是「装配车间」：注册内置分析、`buildXxxPipeline` 构造默认流水线、`registerXxxEPCallback` 提供扩展点、`registerPipelineParsingCallback` 扩展 `-passes` 词表。
- `-passes` 文本流水线用「逗号分隔 + 圆括号嵌套」描述层级；`parsePassPipeline` 会在首 pass 非模块级时**自动包一层**对应管理器（[PassBuilder.cpp:L2711-L2759](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Passes/PassBuilder.cpp#L2711-L2759)）。
- `opt` 的 `runPassPipeline`（[NewPMDriver.cpp:L355](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/tools/opt/NewPMDriver.cpp#L355)）把以上全部串起来：建四层管理器 → 建 `PassBuilder` → 注册分析/接线 → 解析 `-passes` → `MPM.run`。`-debug-pass-manager` 与 `-print-pipeline` 是观察流水线的两大工具。

## 7. 下一步学习建议

本讲只讲了「框架」——怎么注册、怎么编排、怎么跑。接下来：

- **u4-l2 分析 pass 与 AnalysisManager**：深入「分析」这一侧，看 `LoopInfo`、别名分析、`MemorySSA` 等具体分析如何被定义、缓存、失效，补全 4.1 里被略过的「分析结果自身如何实现 `invalidate()`」。
- **u4-l3 经典优化 pass 巡礼**：进 `lib/Transforms` 看 `instcombine`、`gvn`、`licm`、`Inliner` 这些本讲反复举例的 pass 到底做了什么，理解 pass 之间的顺序依赖。
- **u4-l4 编写你自己的 LLVM Pass**：动手写一个继承 `PassInfoMixin` 的新 PM pass，并用本讲的 `registerPipelineParsingCallback` 把它注册进 `-passes`，把「框架」与「自己写 pass」打通。
- **延伸阅读**：官方博客 [The New Pass Manager](https://blog.llvm.org/posts/2021-03-26-the-new-pass-manager/)（[NewPassManager.md:L6-L8](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/docs/NewPassManager.md#L6-L8) 提到的链接）给出了架构动机；`WritingAnLLVMNewPMPass.md` 则是写 pass 的官方教程，u4-l4 会基于它展开。
