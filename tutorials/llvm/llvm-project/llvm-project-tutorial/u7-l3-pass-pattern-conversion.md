# Pass、Pattern 与 Conversion

## 1. 本讲目标

本讲是 MLIR 单元（u7）的第三篇。在 u7-l1 我们建立了「一切皆 Operation」的核心 IR 抽象，在 u7-l2 讲清了 Operation 的来源——方言（Dialect）。本讲回答下一个问题：**拿到一段 MLIR 之后，用什么机制去变换它、优化它、并把它从一个方言逐步下降（Lowering）到另一个方言？**

MLIR 给出的答案是三件套：

1. **Pass 框架**：以「管理器 + 一组 Pass」的形式，按层次把变换作用到 IR 上。
2. **RewritePattern（模式重写）**：用「匹配 + 改写」的局部图重写规则表达一类优化。
3. **Conversion（方言转换）**：在模式重写之上，加上「合法性目标」，把高层方言系统性地翻译到低层方言。

学完本讲，你应当能够：

- 看懂并拼出一条 MLIR 的 Pass 流水线，说出 `PassManager`、`OpPassManager`、`Pass` 各自的职责。
- 理解 `RewritePattern` / `OpRewritePattern` 的「match + rewrite」模型，以及它如何被 `canonicalizer` 这类贪心驱动器反复应用。
- 说清 `ConversionTarget` + `OpConversionPattern` + `applyPartialConversion` 三者如何协作完成一次方言 Lowering。
- 能够参照 Toy 教程（Ch3/Ch5）读懂一段真实变换代码，并动手观察 IR 的变化。

## 2. 前置知识

本讲假设你已经掌握 u7-l1、u7-l2 的内容。回顾几个关键概念：

- **Operation / Region / Block**：MLIR 的嵌套树（见 u7-l1）。变换的最小处理单元是某个 Operation。
- **Dialect（方言）**：一组 Operation/Type/Attribute 的命名空间（见 u7-l2），操作名形如 `toy.transpose`、`arith.addf`。
- **渐进式下降（Progressive Lowering）**：MLIR 的核心思想——不一次性把高层 IR 翻成机器码，而是让 IR 在多个抽象层级之间逐步下降，每一层都可以做与之匹配的优化。

此外，几个本讲会反复出现的术语，先给一个一句话定义：

| 术语 | 一句话解释 |
| --- | --- |
| Pass | 一次「读 IR → 改 IR」的变换步骤，框架调度的基本单位。 |
| PassManager / OpPassManager | 装着一串 Pass 并按层次驱动它们执行的容器。 |
| RewritePattern | 一条「当 IR 长成某样子时，就把它改写成另一种样子」的局部规则。 |
| PatternRewriter | 改写 IR 时用的「工具箱」，提供 `replaceOp` 等接口。 |
| ConversionTarget | 描述「什么样的 Operation 是合法的终态」的目标集合。 |
| Lowering / Conversion | 把高层方言的 Operation 翻译成低层方言 Operation 的过程。 |

如果你熟悉 LLVM 新 Pass 管理器（u4-l1），会发现 MLIR 的 Pass 框架在思想上非常接近（管理器顺序执行 Pass、有分析缓存、可声明保留分析），但 **MLIR 的 Pass 锚定在某个 Operation 类型上**，因为 MLIR 是嵌套树，而 LLVM IR 是扁平的 Module/Function 结构。

## 3. 本讲源码地图

本讲涉及的关键源码文件如下：

| 文件 | 作用 |
| --- | --- |
| [`mlir/include/mlir/Pass/Pass.h`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/Pass/Pass.h) | `Pass`、`OperationPass`、`PassWrapper` 等核心类的定义。 |
| [`mlir/lib/Pass/Pass.cpp`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Pass/Pass.cpp) | Pass 管理器的执行引擎：`PassManager::run`、`OpToOpPassAdaptor` 的调度循环。 |
| [`mlir/lib/Pass/PassRegistry.cpp`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Pass/PassRegistry.cpp) | Pass 注册表与 `-passes=...` 文本流水线解析器。 |
| [`mlir/include/mlir/IR/PatternMatch.h`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/PatternMatch.h) | `RewritePattern`、`OpRewritePattern`、`RewritePatternSet`、`PatternRewriter` 的定义。 |
| [`mlir/include/mlir/Transforms/DialectConversion.h`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/Transforms/DialectConversion.h) | `ConversionTarget`、`OpConversionPattern`、`applyPartialConversion` 等转换框架的定义。 |
| [`mlir/examples/toy/Ch3/mlir/ToyCombine.cpp`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch3/mlir/ToyCombine.cpp) | Toy 方言的模式重写优化（C++ 与 DRR 两种写法）。 |
| [`mlir/examples/toy/Ch3/mlir/ToyCombine.td`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch3/mlir/ToyCombine.td) | 用 TableGen DRR（声明式重写规则）描述的同一些优化。 |
| [`mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp) | 把 Toy 方言 Lowering 到 affine/arith/memref 方言的完整 Conversion Pass。 |
| [`mlir/examples/toy/Ch3/toyc.cpp`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch3/toyc.cpp) 与 [`Ch5/toyc.cpp`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/toyc.cpp) | Toy 编译器入口，演示如何用代码拼装并运行 Pass 流水线。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 MLIR Pass 框架**、**4.2 RewritePattern 与模式重写**、**4.3 Conversion / Lowering**。三者是层层叠加的关系——Pass 是「外壳与调度」，RewritePattern 是「局部规则」，Conversion 是「带合法性目标的成批改写」。

### 4.1 MLIR Pass 框架

#### 4.1.1 概念说明

**Pass 是 MLIR 变换的基本单位**：它读入一个 Operation，对它（及其内部嵌套的内容）做一些变换，写回 IR。和 LLVM 一样，MLIR 用一个「Pass 管理器」来组织一串 Pass 并依次执行它们。

MLIR 与 LLVM 在这里的最大差别来自 IR 形态：

- LLVM IR 是**扁平**的 `Module ⊃ Function`，所以 LLVM 的 Pass 层次是固定的（Module / CGSCC / Function / Loop）。
- MLIR IR 是**任意嵌套的树**（`Operation ⊃ Region ⊃ Block ⊃ Operation`），所以 MLIR 的 Pass 管理器必须能「锚定到某一种 Operation 类型」再下钻。

因此 MLIR 引入两个容器类：

- `PassManager`：顶层管理器，锚定在一个根 Operation（通常是 `builtin.module`）上。
- `OpPassManager`：可嵌套的管理器，锚定在某种具体 Operation（如 `toy.func`、`func.func`）上。

而一个 Pass 自身通过继承 `OperationPass<OpT>` 声明「我只处理 `OpT` 这种 Operation」。框架在执行时，会遍历 IR 树，把匹配类型的 Operation 喂给对应的 Pass。

#### 4.1.2 核心流程

一次 `PassManager::run(op)` 的执行可以概括为：

```text
PassManager::run(moduleOp)
  │
  ├─ 1. 收集所有 Pass 声明的「依赖方言」并加载到 context
  ├─ 2. finalizePassList：合并相邻的 adaptor、校验可调度性
  ├─ 3. initialize：用新的一代（generation）初始化每个 Pass
  ├─ 4. 构造顶层 AnalysisManager（分析缓存）
  └─ 5. OpToOpPassAdaptor::runPipeline → 逐个 Pass 执行：
         对每个 Pass：
           a. 检查目标 Operation 是否注册、是否 IsolatedFromAbove
           b. 运行 before-pass 插桩
           c. 调用 pass->runOnOperation()
           d. 按 Pass 声明「保留的分析」做分析失效
           e. （可选）运行 verifier 校验 IR 合法性
           f. 运行 after-pass 插桩
```

关键约束：**只有带 `IsolatedFromAbove` 特征的 Operation 才能被 Pass 调度**。这个特征表示该 Operation「不从外层引用任何值」，从而保证 Pass 在多线程下安全地改写它内部的内容而不会波及外部。这是 MLIR 并行执行 Pass 的前提。

当 Pass 锚定的 Operation 类型与当前管理器不同时，框架会自动**嵌套**（nest）一个新的 `OpPassManager`，并通过 `OpToOpPassAdaptor`（适配器）这个特殊 Pass 把「外层管理器」与「内层管理器」粘起来——它的工作就是遍历嵌套 Operation，为每个找到合适的内层管理器并下钻执行。

#### 4.1.3 源码精读

**Pass 基类**的核心虚函数定义在 [`mlir/include/mlir/Pass/Pass.h:52-84`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/Pass/Pass.h#L52-L84)：

```cpp
class Pass {
public:
  virtual StringRef getName() const = 0;
  // 注册本 Pass 会「新建」哪些方言的实体（类型/操作/属性）
  virtual void getDependentDialects(DialectRegistry &registry) const {}
  // 命令行参数名，用于 -passes=... 文本流水线
  virtual StringRef getArgument() const { return ""; }
  virtual StringRef getDescription() const { return ""; }
  std::optional<StringRef> getOpName() const { return opName; }
  ...
};
```

注意 `getDependentDialects`（[Pass.h:72](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/Pass/Pass.h#L72)）是个钩子：一个把 Toy 翻成 affine 的 Pass，需要声明它会创建 `affine`/`func`/`memref` 方言的操作，框架据此提前把这些方言加载进 context。

**OperationPass 模板**（[`mlir/include/mlir/Pass/Pass.h:366-397`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/Pass/Pass.h#L366-L397)）把 Pass 锚定到具体 Operation 类型：

```cpp
template <typename OpT = void>
class OperationPass : public Pass {
protected:
  OperationPass(TypeID passID) : Pass(passID, OpT::getOperationName()) {}
  // 最终判定：只能调度在 OpT 这种操作上
  bool canScheduleOn(RegisteredOperationName opName) const final {
    return opName.getStringRef() == getOpName();
  }
  OpT getOperation() { return cast<OpT>(Pass::getOperation()); }
};
```

而用户写 Pass 时通常不直接继承 `OperationPass`，而是套一层 CRTP 的 **PassWrapper**（[`mlir/include/mlir/Pass/Pass.h:470-492`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/Pass/Pass.h#L470-L492)），它会自动帮你实现 `getName()` 和 `clonePass()`（Pass 必须可克隆，因为多线程下每个线程需要一份副本）：

```cpp
template <typename PassT, typename BaseT>
class PassWrapper : public BaseT {
protected:
  PassWrapper() : BaseT(TypeID::get<PassT>()) {}
  StringRef getName() const override { return llvm::getTypeName<PassT>(); }
  std::unique_ptr<Pass> clonePass() const override {
    return std::make_unique<PassT>(*static_cast<const PassT *>(this));
  }
};
```

LowerToAffineLoops 里的真实 Pass 就是这样写的（[`mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp:311-323`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp#L311-L323)）：

```cpp
struct ToyToAffineLoweringPass
    : public PassWrapper<ToyToAffineLoweringPass, OperationPass<ModuleOp>> {
  StringRef getArgument() const override { return "toy-to-affine"; }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<affine::AffineDialect, func::FuncDialect,
                    memref::MemRefDialect>();
  }
  void runOnOperation() final;
};
```

这是一切「手写 Pass」的标准骨架：继承 `PassWrapper<自己, OperationPass<锚点操作>>`，可选地提供 `getArgument()`（这样它就能出现在文本流水线里）、`getDependentDialects()`，以及真正干活的 `runOnOperation()`。

**执行引擎** `OpToOpPassAdaptor::run`（[`mlir/lib/Pass/Pass.cpp:546-657`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Pass/Pass.cpp#L546-L657)）是整个框架的心脏。它会先做合法性检查（要求 Operation 注册过、且带 `IsolatedFromAbove`，见 [Pass.cpp:558-561](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Pass/Pass.cpp#L558-L561)），然后真正调用 `runOnOperation`（[Pass.cpp:609-613](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Pass/Pass.cpp#L609-L613)）：

```cpp
op->getContext()->executeAction<PassExecutionAction>(
    [&]() {
      if (auto *adaptor = dyn_cast<OpToOpPassAdaptor>(pass))
        adaptor->runOnOperation(verifyPasses);
      else
        pass->runOnOperation();          // ← 你的 Pass 在这里被调用
      passFailed = pass->passState->irAndPassFailed.getInt();
    },
    {op}, *pass);
// 失效未被保留的分析
am.invalidate(pass->passState->preservedAnalyses);
// 可选：跑 verifier 校验 IR
```

而**适配器如何下钻**到嵌套 Operation，看 `runOnOperationImpl`（[`mlir/lib/Pass/Pass.cpp:855-901`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Pass/Pass.cpp#L855-L901)）：它遍历当前 Operation 的 region/block，为每个嵌套 Operation 找到合适的内层 Pass 管理器（`findPassManagerFor`），再递归执行该管线。

最顶层的 `PassManager::run`（[`mlir/lib/Pass/Pass.cpp:1035-1116`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Pass/Pass.cpp#L1035-L1116)）负责加载依赖方言（[Pass.cpp:1052-1061](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Pass/Pass.cpp#L1052-L1061)）、终结 Pass 列表、初始化 Pass、构造分析管理器，最后把整条管线交给 `OpToOpPassAdaptor::runPipeline` 执行。

**注册与文本流水线**：要让一个 Pass 能用 `getArgument()` 标识并出现在 `-passes=` 文本里，需要把它注册到全局注册表。`registerPass`（[`mlir/lib/Pass/PassRegistry.cpp:149-169`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Pass/PassRegistry.cpp#L149-L169)）把 PassInfo 以 `argument` 为键存入 `passRegistry`。`-passes=` 文本（形如 `builtin.module(func.func(cse,canonicalize))`）由文本流水线解析器 `parsePipelineText`（[`mlir/lib/Pass/PassRegistry.cpp:623-709`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Pass/PassRegistry.cpp#L623-L709)）按 `,(){` 切分，再由 `resolvePipelineElement`（[PassRegistry.cpp:722-744](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Pass/PassRegistry.cpp#L722-L744)）把名字解析成注册表里的 Pass 或 pipeline。外层入口是 `parsePassPipeline`（[PassRegistry.cpp:780-798](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Pass/PassRegistry.cpp#L780-L798)）。

#### 4.1.4 代码实践

**实践目标**：读懂 Ch3 中 `toyc.cpp` 是如何用代码拼装并运行一条最简单的 Pass 流水线的。

**操作步骤**：

1. 打开 [`mlir/examples/toy/Ch3/toyc.cpp`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch3/toyc.cpp)，定位到 `dumpMLIR()` 函数（[Ch3/toyc.cpp:111-136](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch3/toyc.cpp#L111-L136)）。
2. 阅读这段核心代码（[Ch3/toyc.cpp:122-132](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch3/toyc.cpp#L122-L132)）：

   ```cpp
   if (enableOpt) {
     mlir::PassManager pm(module.get()->getName());
     if (mlir::failed(mlir::applyPassManagerCLOptions(pm)))
       return 4;
     // 在每个 toy.func 内运行 canonicalizer
     pm.addNestedPass<mlir::toy::FuncOp>(mlir::createCanonicalizerPass());
     if (mlir::failed(pm.run(*module)))
       return 4;
   }
   ```

3. 解释这条流水线：顶层管理器锚定 `builtin.module`（`module->getName()`）；`addNestedPass<toy::FuncOp>(...)` 表示「下钻到每个 `toy.func` 上跑 canonicalizer」——框架会自动插入一个适配器来处理这层嵌套；`pm.run(*module)` 启动执行。

**需要观察的现象**：

- `addNestedPass<OpT>` 是 `pm.nest<OpT>().addPass(...)` 的简写，它隐式建立了一个嵌套的 `OpPassManager`。
- 如果把 `addNestedPass` 换成 `addPass`，而 Pass 又是 FunctionPass，会触发「隐式嵌套」（见 [Pass.cpp:220-235](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Pass/Pass.cpp#L220-L235) 的 `addPass` 逻辑）。

**预期结果**：你能在脑中画出「module 管理器 →(adaptor)→ 每个 toy.func → canonicalizer」的执行结构。实际运行结果**待本地验证**（需先按 Ch3 CMake 构建 `toyc-ch3`，见 4.3.4 与第 5 节的构建说明）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 MLIR 要求被 Pass 调度的 Operation 必须带 `IsolatedFromAbove` 特征？

> **参考答案**：因为 MLIR 支持多线程并行执行 Pass。如果一个 Operation 内部引用了外层的值，改写它就可能影响外部状态，无法保证线程安全。`IsolatedFromAbove` 承诺「内部不依赖外部」，使得框架可以放心地只在该 Operation 的子树上操作，进而并行处理兄弟 Operation。源码强制见 [Pass.cpp:558-561](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Pass/Pass.cpp#L558-L561)。

**练习 2**：`OperationPass<ModuleOp>` 与 `OperationPass<>`（泛型版本）在 `canScheduleOn` 上有何区别？

> **参考答案**：特化版（[Pass.h:384-386](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/Pass/Pass.h#L384-L386)）`canScheduleOn` 是 `final`，严格要求操作名等于 `OpT::getOperationName()`，只能跑在某一种操作上；泛型版 `OperationPass<void>`（[Pass.h:425-427](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/Pass/Pass.h#L425-L427)）默认返回 `true`，可调度在任何带 `IsolatedFromAbove` 的操作上。

---

### 4.2 RewritePattern 与模式重写

#### 4.2.1 概念说明

Pass 给出了「变换的外壳与调度」，但很多优化本质上是**局部的图重写**：当 IR 出现某种可识别的形状时，就把它等价地替换成更优的形状。比如：

- `transpose(transpose(x))` ⟶ `x`（连续两次转置抵消）
- `reshape(reshape(x))` ⟶ `reshape(x)`（连续重塑可合并）

MLIR 把这类规则抽象成 **RewritePattern（重写模式）**。每条模式做两件事：

1. **match（匹配）**：判断当前 Operation 是否符合要优化的形状。
2. **rewrite（改写）**：若匹配，用 `PatternRewriter` 工具箱把 IR 改写成目标形状。

一个 Pass（如 `canonicalizer`）通常会**收集一批 RewritePattern**，然后贪心地、反复地把它们应用到 IR 上，直到没有任何模式还能匹配（不动点）。这就是「贪心模式重写驱动器」的工作方式。

#### 4.2.2 核心流程

一条 RewritePattern 的生命周期：

```text
定义：struct MyPattern : OpRewritePattern<SomeOp> {
        matchAndRewrite(SomeOp op, PatternRewriter &rewriter) const;
      }
       │
收集：RewritePatternSet patterns(ctx);
      patterns.add<MyPattern, OtherPattern>(ctx);   // 把若干模式装进集合
       │
驱动：canonicalizer / applyPatternsAndFoldGreedily：
      反复遍历 IR，对每个 Operation 尝试所有 pattern ——
        若某 pattern 的 matchAndRewrite 返回 success：
            应用改写、并「折叠」（fold）能折叠的操作
            被改写影响的 Operation 重新入队，继续尝试
      直到没有 pattern 能再匹配（到达不动点）
```

两个设计要点：

- **模式带 benefit（收益）**：构造模式时可给一个收益值，框架在多条模式都能匹配同一个 Operation 时，优先尝试收益高的。
- **改写必须通过 rewriter**：模式**不能**直接修改 IR，必须调用 `rewriter.replaceOp(...)`、`rewriter.eraseOp(...)` 等接口。这样驱动器才能维护工作列表、正确处理失效引用。

#### 4.2.3 源码精读

**RewritePattern** 是所有模式的抽象基类（[`mlir/include/mlir/IR/PatternMatch.h:238-286`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/PatternMatch.h#L238-L286)），核心是一个纯虚函数：

```cpp
class RewritePattern : public Pattern {
public:
  // 匹配成功且改写了 IR 时返回 success；否则返回 failure
  virtual LogicalResult matchAndRewrite(Operation *op,
                                        PatternRewriter &rewriter) const = 0;
};
```

直接继承 `RewritePattern` 要自己处理 `Operation*` 的类型转换，太繁琐。于是有了 **OpRewritePattern**（[`PatternMatch.h:309-326`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/PatternMatch.h#L309-L326)），它自动把 `Operation*` 转成具体的 `SourceOp`：

```cpp
template <typename SourceOp>
struct OpRewritePattern
    : public mlir::detail::OpOrInterfaceRewritePatternBase<SourceOp> {
  OpRewritePattern(MLIRContext *context, PatternBenefit benefit = 1,
                   ArrayRef<StringRef> generatedNames = {})
      : ...(SourceOp::getOperationName(), benefit, context, generatedNames) {}
};
```

它内部靠 `OpOrInterfaceRewritePatternBase`（[PatternMatch.h:292-306](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/PatternMatch.h#L292-L306)）把 `Operation*` 版本的 `matchAndRewrite` 做了 `cast<SourceOp>` 后转发到类型安全的版本。

**PatternRewriter**（[`PatternMatch.h:799-809`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/PatternMatch.h#L799-L809)）继承自 `RewriterBase`，提供了改写 IR 的全部接口。最常用的是 `replaceOp`（定义于基类 [`PatternMatch.h:515-523`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/PatternMatch.h#L515-L523)）：

```cpp
/// 用 newValues 替换 op 的所有结果，并擦除原 op
virtual void replaceOp(Operation *op, ValueRange newValues);
/// 用另一个新 op 替换 op
virtual void replaceOp(Operation *op, Operation *newOp);
```

`replaceOp` 的语义很重要：它把原 Operation 的结果引用**全部改写**为新值，然后**删除**原 Operation——这正是「用更优形状等价替换」的标准动作。

**RewritePatternSet**（[`PatternMatch.h:822-889`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/PatternMatch.h#L822-L889)）是模式的集合容器，提供链式 `add<Ts...>(args...)`（[PatternMatch.h:858-868](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/PatternMatch.h#L858-L868)）一次性塞入多种模式。

来看 Toy 的真实例子。**SimplifyRedundantTranspose**（[`mlir/examples/toy/Ch3/mlir/ToyCombine.cpp:28-53`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch3/mlir/ToyCombine.cpp#L28-L53)）实现了 `transpose(transpose(x)) → x`：

```cpp
struct SimplifyRedundantTranspose : public mlir::OpRewritePattern<TransposeOp> {
  SimplifyRedundantTranspose(mlir::MLIRContext *context)
      : OpRewritePattern<TransposeOp>(context, /*benefit=*/1) {}

  llvm::LogicalResult
  matchAndRewrite(TransposeOp op,
                  mlir::PatternRewriter &rewriter) const override {
    // 1. 看 transpose 的输入是否又是一个 transpose
    mlir::Value transposeInput = op.getOperand();
    TransposeOp transposeInputOp = transposeInput.getDefiningOp<TransposeOp>();
    if (!transposeInputOp)
      return failure();                  // 不是 → 不匹配

    // 2. 匹配！用内层 transpose 的输入替换当前 op
    rewriter.replaceOp(op, {transposeInputOp.getOperand()});
    return success();
  }
};
```

这段代码浓缩了 RewritePattern 的全部要素：继承 `OpRewritePattern<TransposeOp>` 只关心 `toy.transpose`；`matchAndRewrite` 里先判断形状，不符合就 `return failure()`（**且不能修改 IR**）；符合则用 `rewriter.replaceOp` 完成改写。

**如何让模式被驱动器用到？** Toy 选择把它们注册为对应操作的「规范化模式」（[`ToyCombine.cpp:57-60`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch3/mlir/ToyCombine.cpp#L57-L60)）：

```cpp
void TransposeOp::getCanonicalizationPatterns(RewritePatternSet &results,
                                              MLIRContext *context) {
  results.add<SimplifyRedundantTranspose>(context);
}
```

`getCanonicalizationPatterns` 是每个操作都能提供的钩子。`canonicalizer` 这个内置 Pass 在运行时，会向 IR 中每种操作索要它的规范化模式，把这些模式连同可「折叠」（fold）的规则一起喂给贪心驱动器，反复应用到不动点。这样 `toyc -opt` 里的 `createCanonicalizerPass()`（见 4.1.4）就能自动用上 `SimplifyRedundantTranspose`。

**声明式写法（DRR）**：除了用 C++ 写模式，还能用 TableGen 的 DRR（Declarative Rewrite Rules）声明式地描述同样的规则。[`ToyCombine.td`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch3/mlir/ToyCombine.td) 给了三种典型写法：

```tablegen
// 1. 基本：Reshape(Reshape(x)) = Reshape(x)
def ReshapeReshapeOptPattern : Pat<(ReshapeOp(ReshapeOp $arg)),
                                   (ReshapeOp $arg)>;

// 2. 带内联 C++（NativeCodeCall）处理更复杂变换
def ReshapeConstant :
  NativeCodeCall<"$0.reshape(::llvm::cast<ShapedType>($1.getType()))">;

// 3. 带约束（Constraint）：仅当输入输出类型相同时 Reshape(x)=x
def TypesAreIdentical : Constraint<CPred<"$0.getType() == $1.getType()">>;
def RedundantReshapeOptPattern : Pat<
  (ReshapeOp:$res $arg), (replaceWithValue $arg),
  [(TypesAreIdentical $res, $arg)]>;
```

这些 `.td` 在构建期由 `mlir-tblgen -gen-rewriters` 编译成等价的 C++ `RewritePattern`，输出到 `ToyCombine.inc`，再被 `#include` 进来（[`ToyCombine.cpp:21-24`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch3/mlir/ToyCombine.cpp#L21-L24)）。最终它们和 C++ 版的模式一样，被注册到 `ReshapeOp::getCanonicalizationPatterns`（[ToyCombine.cpp:64-68](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch3/mlir/ToyCombine.cpp#L64-L68)）：

```cpp
void ReshapeOp::getCanonicalizationPatterns(RewritePatternSet &results,
                                            MLIRContext *context) {
  results.add<ReshapeReshapeOptPattern, RedundantReshapeOptPattern,
              FoldConstantReshapeOptPattern>(context);  // ← 来自 ToyCombine.inc
}
```

> 这是 u6-l5 TableGen 思想在 MLIR 的延续：声明式描述 `.td`，构建期生成大量样板 C++，让人只写「做什么」而不写「怎么调度」。

#### 4.2.4 代码实践

**实践目标**：通过阅读 Ch3 的源码，理解「一条 RewritePattern 如何从定义走到被执行」。

**操作步骤**：

1. 在 [`ToyCombine.cpp:28-53`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch3/mlir/ToyCombine.cpp#L28-L53) 读 `SimplifyRedundantTranspose`，确认它只在「输入也是 transpose」时返回 `success()`。
2. 在 [`ToyCombine.td:34-62`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch3/mlir/ToyCombine.td#L34-L62) 对比 DRR 的三种写法：纯 dag 重写、`NativeCodeCall`、`Constraint`。
3. 在 [`ToyCombine.cpp:57-68`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch3/mlir/ToyCombine.cpp#L57-L68) 看它们如何挂到 `getCanonicalizationPatterns`。
4. 在 [`Ch3/toyc.cpp:122-132`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch3/toyc.cpp#L122-L132) 看 `createCanonicalizerPass()` 如何被加入流水线。

**需要观察的现象**：追踪这条链——「C++/DRR 定义模式 → 注册为规范化模式 → canonicalizer 收集并贪心应用」。注意 `matchAndRewrite` 在 `return failure()` 前绝不能改动 IR，否则会破坏驱动器的工作列表假设。

**预期结果**：你能复述「一条 `toy.transpose(toy.transpose(x))` 是如何被 `SimplifyRedundantTranspose` 化简成 `x`」的完整过程。实际运行 `toyc-ch3 ... -opt` 的输出**待本地验证**（见第 5 节构建说明）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `matchAndRewrite` 必须在返回 `failure()` 之前不修改任何 IR？

> **参考答案**：因为驱动器会尝试多条模式、并对同一 Operation 反复尝试。如果一条模式「改了一半又失败」，IR 就处于半改写的非法状态，后续模式与工作列表都会错乱。约定「只有返回 `success()` 才视为改写生效」让框架可以把多次尝试做成事务式的。改写必须经由 `PatternRewriter`，也是为了让驱动器统一记账。

**练习 2**：DRR 的 `Pat<(ReshapeOp(ReshapeOp $arg)), (ReshapeOp $arg)>` 对应怎样的 C++ 语义？

> **参考答案**：它声明了一条模式——源形状是「对一个 `ReshapeOp($arg)` 再套一层 `ReshapeOp`」，目标形状是「只保留一层 `ReshapeOp($arg)`」。`$arg` 是绑定到操作数的变量名。`mlir-tblgen` 会把它编译成一个等价于「检测嵌套 reshape、用内层替换外层」的 C++ `RewritePattern`，即 `ReshapeReshapeOptPattern`。

---

### 4.3 Conversion / Lowering：方言间转换

#### 4.3.1 概念说明

RewritePattern 擅长「同方言内的局部优化」，但 MLIR 的核心能力是**渐进式下降**：把高层方言（如 `toy`）的操作，整批翻译成低层方言（如 `affine` + `arith` + `memref`）的操作。这种「跨方言、成批、且要求最终全部合法」的改写，用普通 RewritePattern 表达起来很笨拙，于是 MLIR 提供了专门的 **Conversion（方言转换）框架**。

Conversion 框架在 RewritePattern 之上多加了两个概念：

1. **ConversionTarget（转换目标）**：声明「什么样的 Operation 在终态是合法的 / 非法的 / 动态判定的」。它回答的是「我要把 IR 变成什么样才算完成」。
2. **OpConversionPattern（转换模式）**：一种特殊的 RewritePattern，它的 `matchAndRewrite` 收到一个 `OpAdaptor`——里面装的是**已经被转换过**的操作数，省去你手动逐个替换的麻烦。

驱动器（`applyPartialConversion` 等）的工作循环大致是：找出所有「非法」操作 → 用模式把它们改写成「合法」操作 → 重复，直到没有非法操作（或确认无法转换而失败）。

> 「Lowering（下降）」与「Conversion（转换）」在日常用语里常混用：Lowering 通常指抽象层级从高到低的 Conversion，但技术上它们用同一套框架。

#### 4.3.2 核心流程

一次完整的 Conversion（以 Toy → affine 为例）：

```text
runOnOperation():
  1. 定义 ConversionTarget target(ctx)
       target.addLegalDialect<affine, arith, func, memref, builtin>()  // 合法终态
       target.addIllegalDialect<toy::ToyDialect>()                      // toy 全部非法
       target.addDynamicallyLegalOp<toy::PrintOp>(...)                  // toy.print 例外
  2. 收集 Conversion Pattern：
       RewritePatternSet patterns(ctx)
       patterns.add<AddOpLowering, MulOpLowering, ConstantOpLowering,
                    FuncOpLowering, ReturnOpLowering, TransposeOpLowering,
                    PrintOpLowering>(ctx)
  3. applyPartialConversion(getOperation(), target, std::move(patterns))
       驱动器反复：
         对每个「非法」op，找一条能匹配它的 ConversionPattern；
         用 rewriter 改写（op 的操作数已自动替换为已转换版本）；
         重新评估合法性；
       直到没有非法 op（partial：允许个别 op 没有对应模式而被忽略，
                        但只要有「显式非法」op 残留就报失败）
```

三种驱动模式的区别（见 [`DialectConversion.h:1512-1536`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/Transforms/DialectConversion.h#L1512-L1536) 的文档注释）：

| 函数 | 失败条件 | 适用场景 |
| --- | --- | --- |
| `applyPartialConversion` | 仅当存在「显式标记为 Illegal」且未被转换的操作 | 部分下降：允许保留一些本就合法的操作（如 `toy.print`） |
| `applyFullConversion` | 任何一个操作未被转换到「合法」 | 要求全部操作都变成合法终态 |
| `applyAnalysisConversion` | 只分析、不真正改写 | 试探「哪些操作能被转换」 |

#### 4.3.3 源码精读

**ConversionTarget**（[`mlir/include/mlir/Transforms/DialectConversion.h:1078-1170`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/Transforms/DialectConversion.h#L1078-L1170)）用一个枚举描述每种操作的处置：

```cpp
class ConversionTarget {
public:
  enum class LegalizationAction {
    Legal,    // 目标支持该操作
    Dynamic,  // 需运行回调动态判定是否合法
    Illegal,  // 目标明确不支持该操作
  };
  using DynamicLegalityCallbackFn =
      std::function<std::optional<bool>(Operation *)>;
  ...
  void addLegalOp() / addIllegalOp() / addDynamicallyLegalOp(callback);
};
```

`addDynamicallyLegalOp`（[DialectConversion.h:1140-1160](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/Transforms/DialectConversion.h#L1140-L1160)）特别有用：它让你用一个回调精细判定某种操作「在什么条件下才算合法」。

**OpConversionPattern**（[`DialectConversion.h:680-732`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/Transforms/DialectConversion.h#L680-L732)）是 Conversion 版的 `OpRewritePattern`，关键差别是它收到一个 `OpAdaptor`：

```cpp
template <typename SourceOp>
class OpConversionPattern : public ConversionPattern {
public:
  using OpAdaptor = typename SourceOp::Adaptor;
  // 框架把「已转换的操作数」打包成 OpAdaptor 传给你
  LogicalResult matchAndRewrite(Operation *op, ArrayRef<Value> operands,
                                ConversionPatternRewriter &rewriter) const final {
    auto sourceOp = cast<SourceOp>(op);
    return matchAndRewrite(sourceOp, OpAdaptor(operands, sourceOp), rewriter);
  }
  virtual LogicalResult matchAndRewrite(SourceOp op, OpAdaptor adaptor,
                  ConversionPatternRewriter &rewriter) const { ... }
};
```

`OpAdaptor` 的意义：当你在改写 `toy.add` 时，它的两个操作数（可能是别的 `toy` 操作的结果）**已经被各自的模式转换过了**，`adaptor.getLhs()/getRhs()` 直接给你转换后的新值，你无需关心前驱怎么变。

改写用的是 **ConversionPatternRewriter**（[`DialectConversion.h:839`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/Transforms/DialectConversion.h#L839)），它继承自 `PatternRewriter` 但增加了一些转换专用接口（如 `applySignatureConversion` 改块参数、`notifyMatchFailure` 报告为何不匹配），并且其改写是「事务式」的——一次模式失败时其所有改动会被回滚。

现在看 Toy 的完整 Conversion Pass。**总入口 `ToyToAffineLoweringPass::runOnOperation`**（[`mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp:325-362`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp#L325-L362)）正是上面流程的代码化：

```cpp
void ToyToAffineLoweringPass::runOnOperation() {
  ConversionTarget target(getContext());
  // 终态合法方言
  target.addLegalDialect<affine::AffineDialect, BuiltinDialect,
                         arith::ArithDialect, func::FuncDialect,
                         memref::MemRefDialect>();
  // toy 方言全部非法（必须被转换掉）
  target.addIllegalDialect<toy::ToyDialect>();
  // 但 toy.print 在「操作数已非 tensor」时算合法（部分下降）
  target.addDynamicallyLegalOp<toy::PrintOp>([](toy::PrintOp op) {
    return llvm::none_of(op->getOperandTypes(),
                         [](Type type) { return llvm::isa<TensorType>(type); });
  });

  RewritePatternSet patterns(&getContext());
  patterns.add<AddOpLowering, ConstantOpLowering, FuncOpLowering, MulOpLowering,
               PrintOpLowering, ReturnOpLowering, TransposeOpLowering>(
      &getContext());

  if (failed(applyPartialConversion(getOperation(), target, std::move(patterns))))
    signalPassFailure();
}
```

这段代码是理解整个 Conversion 框架的最佳样本：**目标（target）定义「要变成什么」，模式（patterns）定义「怎么变」，`applyPartialConversion` 负责「反复变到满足目标」**。

接下来看几条具体模式。**二元运算的下降**（[`LowerToAffineLoops.cpp:113-138`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp#L113-L138)）是一个模板，把 `toy.add`/`toy.mul` 下降为「逐元素循环 + affine load + 低层算术 + affine store」：

```cpp
template <typename BinaryOp, typename LoweredBinaryOp>
struct BinaryOpLowering : public OpConversionPattern<BinaryOp> {
  using OpAdaptor = typename OpConversionPattern<BinaryOp>::OpAdaptor;
  LogicalResult matchAndRewrite(BinaryOp op, OpAdaptor adaptor,
                  ConversionPatternRewriter &rewriter) const final {
    auto loc = op->getLoc();
    lowerOpToLoops(op, rewriter, [&](OpBuilder &builder, ValueRange loopIvs) {
      auto loadedLhs = affine::AffineLoadOp::create(builder, loc, adaptor.getLhs(), loopIvs);
      auto loadedRhs = affine::AffineLoadOp::create(builder, loc, adaptor.getRhs(), loopIvs);
      return LoweredBinaryOp::create(builder, loc, loadedLhs, loadedRhs);  // arith.addf/mulf
    });
    return success();
  }
};
using AddOpLowering = BinaryOpLowering<toy::AddOp, arith::AddFOp>;
using MulOpLowering = BinaryOpLowering<toy::MulOp, arith::MulFOp>;
```

注意 `adaptor.getLhs()` 给出的就是已经被转换（从 tensor 变成 memref）的操作数——这就是 `OpAdaptor` 的便利。

`lowerOpToLoops`（[`LowerToAffineLoops.cpp:78-106`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp#L78-L106)）是公共辅助：为结果分配一个 memref，用 `affine::buildAffineLoopNest` 按每个维度生成一层循环嵌套，在最内层调用回调算出元素值并 `AffineStoreOp` 存回，最后用 `rewriter.replaceOp(op, alloc)` 用这块 memref 替换原操作。

另两类模式展示了 Conversion 的其它常见手法：

- **FuncOpLowering**（[`LowerToAffineLoops.cpp:213-238`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp#L213-L238)）：只下降 `main` 函数；若 `main` 不满足「无参无返回」则用 `rewriter.notifyMatchFailure`（[行 226](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp#L226)）说明原因并放弃；否则用 `inlineRegionBefore` 把函数体搬到新的 `func.func` 里。
- **ReturnOpLowering**（[`LowerToAffineLoops.cpp:262-277`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp#L262-L277)）：直接把 `toy.return` 换成 `func.return`（用 `replaceOpWithNewOp`）。

**它在流水线里的位置**：Ch5 的 `toyc.cpp` 给出了一条完整的下降流水线（[`Ch5/toyc.cpp:132-168`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/toyc.cpp#L132-L168)）：

```cpp
mlir::PassManager pm(module.get()->getName());
if (enableOpt || isLoweringToAffine) {
  pm.addPass(mlir::createInlinerPass());                  // 1. 内联所有函数到 main
  mlir::OpPassManager &optPM = pm.nest<mlir::toy::FuncOp>();
  optPM.addPass(mlir::toy::createShapeInferencePass());   // 2. 形状推断
  optPM.addPass(mlir::createCanonicalizerPass());         // 3. 规范化（用上 RewritePattern）
  optPM.addPass(mlir::createCSEPass());                   // 4. 公共子表达式消除
}
if (isLoweringToAffine) {
  pm.addPass(mlir::toy::createLowerToAffinePass());       // 5. 本讲的 Conversion！
  mlir::OpPassManager &optPM = pm.nest<mlir::func::FuncOp>();
  optPM.addPass(mlir::createCanonicalizerPass());         // 6. 下降后清理
  optPM.addPass(mlir::createCSEPass());
  if (enableOpt) {
    optPM.addPass(mlir::affine::createLoopFusionPass());   // 7. 循环融合
    optPM.addPass(mlir::affine::createAffineScalarReplacementPass());
  }
}
```

这条流水线是「渐进式下降」的教科书范例：先在高层（toy）做内联与优化，再用 Conversion 降到 affine，最后在低层（affine）继续优化。每一步都只在一个抽象层级上工作，用的都是本讲讲的三件套。

#### 4.3.4 代码实践

**实践目标**：亲手构建并运行 `toyc-ch5`，观察同一段 Toy 代码在「仅优化」与「下降到 affine」两步下的 IR 差异。

**操作步骤**：

1. 配置并构建（需启用 MLIR，详见 u1-l3）。示例命令（具体路径按你的环境调整，**构建耗时较长**）：

   ```bash
   cmake -G Ninja -S llvm -B build \
         -DLLVM_ENABLE_PROJECTS=mlir \
         -DCMAKE_BUILD_TYPE=Release \
         -DLLVM_TARGETS_TO_BUILD=X86
   cmake --build build --target toyc-ch3 toyc-ch5
   ```

   这会产出 `build/bin/toyc-ch3` 与 `build/bin/toyc-ch5`（目标名见 [`Ch5/CMakeLists.txt:13`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/CMakeLists.txt#L13) 的 `add_toy_chapter(toyc-ch5 ...)`，宏定义见 [mlir/examples/toy/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/CMakeLists.txt)）。

2. 准备一段含冗余 transpose 的 Toy 源码，存为 `example.toy`（示例代码，语法见 Toy 教程）：

   ```text
   def transpose_transpose(x) {
     return transpose(transpose(x));
   }
   ```

3. 仅做规范化优化，观察 RewritePattern 的效果：

   ```bash
   ./build/bin/toyc-ch3 example.toy -emit=mlir -opt
   ```

   预期 `transpose(transpose(x))` 被 `SimplifyRedundantTranspose` 化简。

4. 下降到 affine，观察 Conversion 的效果：

   ```bash
   ./build/bin/toyc-ch5 example.toy -emit=mlir-affine -opt
   ```

   预期 `toy.*` 操作被替换为 `affine.for` / `affine.load` / `affine.store` / `arith.addf` 等。

**需要观察的现象**：

- 第 3 步：toy 方言的操作还在，但冗余结构消失了（RewritePattern 的作用域是「同方言局部优化」）。
- 第 4 步：toy 方言的计算操作几乎消失，取而代之的是 affine 循环与 memref/arith 操作；`toy.print` 可能保留（因为它是 `addDynamicallyLegalOp` 的例外）。

**预期结果**：你能直观看到「渐进式下降」——同一份输入在不同阶段呈现完全不同的抽象层级。若你尚未构建环境，以上命令的精确输出**待本地验证**；你也可以退而用第 4.3.3 节的源码阅读方式，根据 `BinaryOpLowering` 等模式推断下降后的 IR 形态。

#### 4.3.5 小练习与答案

**练习 1**：`applyPartialConversion` 与 `applyFullConversion` 在「失败条件」上有何不同？为什么 Toy 选前者？

> **参考答案**：`applyFullConversion` 要求**每一个**操作最终都处于合法态，否则失败；`applyPartialConversion` 只在存在「显式标记 Illegal 且未被转换」的操作时才失败，对「既没匹配上、也没被显式禁止」的操作会容忍。Toy 选 `applyPartialConversion`（[LowerToAffineLoops.cpp:359](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp#L359)）是因为它是「部分下降」——`toy.print` 故意保留不降，用 `addDynamicallyLegalOp` 让它在操作数合法时视为合法（[行 344-347](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp#L344-L347)）。若用 Full Conversion，这些保留的 `toy.print` 会导致失败。

**练习 2**：`OpConversionPattern` 传进来的 `OpAdaptor` 和直接用 `op.getLhs()` 取操作数有什么区别？

> **参考答案**：`op.getLhs()` 取的是**原始**操作数（可能还是 toy 方言的 tensor 值），而 `adaptor.getLhs()` 取的是**已被前驱模式转换后**的操作数（已是 memref 值）。Conversion 是成批、有依赖的：当你处理 `toy.add` 时，产生它操作数的那些操作可能已经被别的模式改写，`OpAdaptor` 让你直接拿到改写后的正确值，无需自己手动跟踪替换，这正是 Conversion 框架相对于裸 RewritePattern 的核心便利之一。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「阅读 + 推演」型综合任务。

**任务**：以 [`mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp) 为对象，回答以下问题，并画出一张「给定 toy 操作 → 最终 affine/arith/memref 操作」的对应表。

1. **Pass 框架层**：`ToyToAffineLoweringPass` 继承自什么？它的 `getDependentDialects` 注册了哪些方言？为什么必须注册？（提示：见 4.1.3 与 [LowerToAffineLoops.cpp:312-320](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp#L312-L320)。）
2. **RewritePattern 层**：本文件里的 `*Lowering` 结构体继承的是 `OpConversionPattern`（它是 `RewritePattern` 的子类的子类）。请确认「Conversion 模式也是一种 RewritePattern」，并指出它们都实现的关键方法是哪个。
3. **Conversion 层**：列出每种 toy 操作对应的「下降目标」并填表：

   | toy 操作 | 下降后主要产生 | 对应模式（源码行） |
   | --- | --- | --- |
   | `toy.add` | `affine.for` + `affine.load/store` + `arith.addf` | `AddOpLowering` [行 137](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp#L137) |
   | `toy.mul` | …（自行补全） | … |
   | `toy.constant` | …（提示 [行 144](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp#L144)） | … |
   | `toy.transpose` | …（提示 [行 283](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp#L283)，注意它用「逆序下标」） | … |
   | `toy.func(main)` | `func.func` | `FuncOpLowering` [行 213](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp#L213) |
   | `toy.return` | `func.return` | `ReturnOpLowering` [行 262](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/mlir/LowerToAffineLoops.cpp#L262) |

4. **流水线层**：回到 [`Ch5/toyc.cpp:140-166`](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch5/toyc.cpp#L140-L166)，解释为什么 `createLowerToAffinePass` 之前要先跑 `createInlinerPass` + `createShapeInferencePass`（提示：注释里说「所有调用已被内联、所有形状已解析」是下降的前提）。

完成本任务后，你应当能独立读懂任意一个 MLIR Conversion Pass，并说清它在三件套中各自扮演的角色。

## 6. 本讲小结

- **Pass 框架**是 MLIR 变换的调度外壳：`PassManager` / `OpPassManager` 按 Operation 类型分层嵌套，由 `OpToOpPassAdaptor` 下钻执行；Pass 锚定具体操作类型（`OperationPass<OpT>`），且要求被调度操作带 `IsolatedFromAbove` 以支持多线程。
- **RewritePattern** 表达「match + rewrite」的局部图重写规则：`OpRewritePattern<SourceOp>` 提供类型安全接口，改写必须经 `PatternRewriter::replaceOp` 等完成；`canonicalizer` 等贪心驱动器把一批模式反复应用到不动点。模式可用 C++ 或 TableGen DRR 声明式描述。
- **Conversion / Lowering** 在 RewritePattern 之上增加 `ConversionTarget`（合法性目标）与 `OpConversionPattern`（带已转换操作数的 `OpAdaptor`），由 `applyPartialConversion` / `applyFullConversion` 驱动整批跨方言改写，是渐进式下降的核心机制。
- 三者是**层层叠加**的关系：Pass 是外壳与调度，RewritePattern 是局部规则，Conversion 是「带目标的成批改写」；Toy 教程（Ch3 演示 RewritePattern，Ch5 演示 Conversion）把它们串联成一条完整的下降流水线。
- 文本流水线（`-passes=...`）由 `PassRegistry.cpp` 的解析器把名字解析到注册表里的 Pass，命令行与代码拼装两种方式等价。

## 7. 下一步学习建议

- **下一讲 u7-l4（Toy 教程：从语言到 MLIR 到 LLVM IR）** 会把本讲的 Pass/Conversion 放进 Toy 的完整旅程：从 AST 经 MLIRGen 生成 MLIR、一路 Lowering 到 LLVM 方言、最终翻译成 LLVM IR。本讲是其中「变换与下降」环节的直接前置。
- **延伸阅读源码**：想看更多 Conversion 实例，可浏览 `mlir/lib/Conversion/` 下各 `*To*.cpp`（如 `VectorToLLVM`、`SCFToControlFlow`），它们的结构与本讲的 `LowerToAffineLoops.cpp` 完全一致。
- **回看 u4-l1/u4-l4**：对照 LLVM 新 Pass 管理器，体会「扁平 IR 的固定层次 Pass」与「嵌套 IR 的 Operation 锚定 Pass」的设计差异。
- **动手建议**：仿照 `SimplifyRedundantTranspose`，为某个 toy 操作（或你熟悉方言的操作）写一条新的 `OpRewritePattern`，挂到 `getCanonicalizationPatterns`，用 `toyc-ch3 -opt` 验证它是否被触发。
