# 编写一个 Pass

## 1. 本讲目标

上一讲（u3-l1）我们看懂了新 Pass 管理器（New PM）的「骨架」：`PassManager` 跑变换队列、`AnalysisManager` 缓存分析结果、二者按 IR 单元分层。本讲把视角从「管理器」转到「被管理的东西」——**我们自己写一个 pass**。

学完本讲，你应当能够：

1. 继承 `PassInfoMixin`（或 `OptionalPassInfoMixin`）写出一个真正可被 `opt` 调用的 FunctionPass；
2. 说清楚 `run(Function &F, FunctionAnalysisManager &FAM)` 这一行签名里每个部分的作用，并能用 `FAM.getResult<...>(F)` 取分析结果；
3. 用 `registerPipelineParsingCallback` 把自己的 pass 注册成一个名字（如 `-passes=count-inst`），并理解它与扩展点回调（如 `registerPipelineStartEPCallback`）的区别；
4. 用 `STATISTIC` 宏和 `LLVM_DEBUG` / `-debug` 给 pass 加上统计计数与调试输出。

本讲全程围绕 `examples/IRTransforms/SimplifyCFG` 这个真实示例展开，它是一个「长得像 out-of-tree 插件、实际住在源码树里」的完整 pass，几乎涵盖了一个新 pass 需要的所有要素。

## 2. 前置知识

在动手前，请确认你已经理解（这些都在前面讲义建立过）：

- **IR 四层结构**（u2-l1）：`Module → Function → BasicBlock → Instruction`，以及遍历它们的迭代器（`for (BasicBlock &BB : F)`、`instructions(F)`）。
- **新 PM 的 pass/analysis 区分**（u3-l1）：变换 pass 改 IR、返回 `PreservedAnalyses`；分析 pass 只读 IR、通过 `getResult` 取结果。
- **类型识别**（u2-l2）：`isa` / `dyn_cast` 如何判别一条指令是不是 `CondBrInst`。
- **命令行工具**（u1-l3）：`opt` 用 `-passes=...` 描述流水线，输入输出都是 IR。

两个新术语先打个照面：

- **CRTP（Curiously Recurring Template Pattern，奇异递归模板模式）**：把派生类自身作为模板参数传给基类，基类就能「提前知道」派生类的类型并复用代码。`PassInfoMixin<SimplifyCFGPass>` 就是 CRTP。
- **插件（plugin）**：一个 `.so` 动态库，导出一个名为 `llvmGetPassPluginInfo` 的 C 符号；`opt` 用 `-load-pass-plugin=xxx.so` 加载它，从而认识新的 pass 名字。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [examples/IRTransforms/SimplifyCFG.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/IRTransforms/SimplifyCFG.cpp) | 主角：一个教学用 CFG 简化 pass，含三个版本的实现与完整的插件注册 |
| [examples/IRTransforms/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/IRTransforms/CMakeLists.txt) | 用 `add_llvm_pass_plugin` 把上面的 .cpp 构建成可加载的 `.so` |
| [examples/Bye/Bye.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/Bye/Bye.cpp) | 参照对象：同时演示了「按名字注册」和「扩展点注入」两种注册方式 |
| [include/llvm/IR/PassManager.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManager.h) | `PassInfoMixin` / `OptionalPassInfoMixin` / `RequiredPassInfoMixin` 的定义 |
| [include/llvm/Passes/PassBuilder.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Passes/PassBuilder.h) | `registerPipelineParsingCallback`、`registerPipelineStartEPCallback` 等注册接口 |
| [include/llvm/ADT/Statistic.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/ADT/Statistic.h) | `STATISTIC` 宏与统计计数器 |
| [test/Examples/IRTransforms/SimplifyCFG/tut-simplify-cfg1.ll](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/test/Examples/IRTransforms/SimplifyCFG/tut-simplify-cfg1.ll) | 用 `opt %loadexampleirtransforms -passes=tut-simplifycfg` 跑该 pass 的回归测试 |

## 4. 核心概念与源码讲解

本讲的三个最小模块是：**① PassInfoMixin 与 run 签名**、**② 注册到 pipeline**、**③ 统计与调试输出**。三者正好对应「写 pass 的 body」→「让别人能调用它」→「让它能被观测」。

### 4.1 PassInfoMixin 与 run 签名

#### 4.1.1 概念说明

新 PM 有一个反直觉的设计：**它没有统一的 pass 基类**。回忆 u3-l1，任何带 `run` 方法的类都是 pass，管理器靠类型擦除（`PassConcept`/`PassModel`）来调度它。

但 pass 仍然需要一些「元信息」——比如它的名字（用于 `-print-changed`、`-time-passes` 等输出）、它是否可以被跳过。这些元信息由一组 **CRTP mixin** 提供，你只要继承它即可，不必自己写：

```text
PassInfoMixin<DerivedT>           ← 提供 name()、printPipeline()、isRequired()=false 的默认
   ├─ RequiredPassInfoMixin       ← isRequired() = true  （即使 -O0 / 管道要求保留也强制运行）
   └─ OptionalPassInfoMixin       ← isRequired() = false （默认可被跳过）
```

- `RequiredPassInfoMixin`：表示这个 pass 「必须运行」。比如属性 `optnone` 的函数会被默认 pipeline 跳过大部分优化，但标记为 required 的 pass 仍会跑。`VerifierPass` 这类就是 required 的。
- `OptionalPassInfoMixin`：表示这个 pass 可以被跳过，是大多数普通优化的默认选择。

CRTP 的好处在这里很直接：`name()` 需要知道派生类的真实类型名（靠 `getTypeName<DerivedT>()` 取 RTTI 名字），而把派生类当模板参数传进来，基类就能拿到。你只需写 `struct MyPass : OptionalPassInfoMixin<MyPass>`。

#### 4.1.2 核心流程

一个 FunctionPass 的最小骨架是这样的：

```text
struct MyPass : OptionalPassInfoMixin<MyPass> {
    PreservedAnalyses run(Function &F, FunctionAnalysisManager &FAM) {
        // 1.（可选）用 FAM.getResult<某分析>(F) 取分析结果
        // 2. 读/改 F（IR 变换）
        // 3. 返回 PreservedAnalyses，声明保住了哪些分析
    }
};
```

要点逐条解释：

- **签名 `run(Function &F, ...)` 决定 IR 单元**。第一个参数是 `Function &`，所以这是 FunctionPass，它会被 `FunctionPassManager` 调度，每个函数跑一次。如果是 `Module &` 就是 ModulePass。**管理器靠参数类型来归类**，没有继承基类来声明。
- **第二个参数 `FunctionAnalysisManager &FAM`** 是「取分析的入口」。注意 u3-l1 讲过：在新 PM 里，pass 不像老 PM 那样用 `getAnalysis<...>()`，而是 `FAM.getResult<DominatorTreeAnalysis>(F)`——传入 IR 单元，返回该单元上该分析的结果。
- **返回值 `PreservedAnalyses`** 是 pass 与管理器之间的「契约」。如果你什么都没改，返回 `PreservedAnalyses::all()`，所有分析都保住、不必重算；如果你改了 IR 又懒得细说，返回 `PreservedAnalyses::none()`，保守地把所有分析都作废。

关于「取分析」与「失效」的关系（承接 u3-l1）：

\[ \text{分析结果是否过期} \;=\; \neg\,(\text{pass 声明保留了它}) \]

也就是说，管理器只在你**没有**声明保留某个分析时，才把它从缓存里清掉。`PreservedAnalyses::none()` 等于「一个都没保留」。

#### 4.1.3 源码精读

`SimplifyCFG` 的核心 pass 类只有十几行，先看它如何继承与声明 `run`：

[examples/IRTransforms/SimplifyCFG.cpp:370-392](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/IRTransforms/SimplifyCFG.cpp#L370-L392) —— pass 类定义。`SimplifyCFGPass` 继承 `OptionalPassInfoMixin<SimplifyCFGPass>`，并提供 `run(Function &, FunctionAnalysisManager &)`。这正是上面骨架的真实落地。

```cpp
namespace {
struct SimplifyCFGPass : public OptionalPassInfoMixin<SimplifyCFGPass> {
  PreservedAnalyses run(Function &F, FunctionAnalysisManager &FAM) {
    switch (Version) {
    case V1:
      doSimplify_v1(F);
      break;
    case V2: {
      DominatorTree &DT = FAM.getResult<DominatorTreeAnalysis>(F);  // 取分析
      doSimplify_v2(F, DT);
      break;
    }
    ...
    }
    return PreservedAnalyses::none();   // 改了 CFG，保守地全部作废
  }
};
} // namespace
```

这段代码做了三件本模块关心的事：

1. **继承 `OptionalPassInfoMixin`**：表示这是个「可被跳过」的普通 pass，并自动获得 `name()` 等元信息。
2. **`FAM.getResult<DominatorTreeAnalysis>(F)`**（[第 378 行](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/IRTransforms/SimplifyCFG.cpp#L378)）：取出支配树（DominatorTree）。这是新 PM 取分析的标准姿势——`getResult<分析类型>(IR 单元)`。注意 v2/v3 版本会保留支配树（用 `DomTreeUpdater` 增量更新），v1 不保留。
3. **`return PreservedAnalyses::none()`**：声明没保住任何分析。这是教学示例为了简单而采用的保守做法；生产级 pass 通常会精确声明保留集（如 `PA.preserve<DominatorTreeAnalysis>()`）。

再看 mixin 本体的定义，理解 `OptionalPassInfoMixin` 到底提供了什么：

[include/llvm/IR/PassManager.h:88-111](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManager.h#L88-L111) —— `PassInfoMixin` 提供 `printPipeline` 和默认的 `isRequired()=false`；`RequiredPassInfoMixin` 把它改成 `true`，`OptionalPassInfoMixin` 保持 `false`。CRTP 让 `name()`（在 `InfoMixin` 里）能拿到派生类类型名。

```cpp
template <typename DerivedT>
struct PassInfoMixin : detail::InfoMixin<DerivedT> {
  void printPipeline(raw_ostream &OS, ...);
  static bool isRequired() { return false; }   // 默认可跳过
};
template <typename DerivedT>
struct RequiredPassInfoMixin : PassInfoMixin<DerivedT> {
  static bool isRequired() { return true; }    // 强制运行
};
template <typename DerivedT>
struct OptionalPassInfoMixin : PassInfoMixin<DerivedT> {
  static bool isRequired() { return false; }   // 可跳过（同默认）
};
```

`SimplifyCFGPass` 选 `OptionalPassInfoMixin`，意味着它不会强行闯进 `optnone` 函数的优化——这对一个教学 pass 是合适的。

> 小贴士：如果你以后写一个必须运行的 pass（比如校验器、必须插桩的逻辑），就继承 `RequiredPassInfoMixin`。

#### 4.1.4 代码实践

**目标**：动手写一个最小 pass 的 body，先不注册、先不编译，只把「遍历函数 + 取分析」的肌肉记忆建立起来。

**步骤**：

1. 在 SimplifyCFG.cpp 的 `run` 方法里，找到 `switch (Version)` 之前的位置（[第 372-373 行附近](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/IRTransforms/SimplifyCFG.cpp#L372-L373)），假装在脑子里加一行：用 `FAM.getResult<DominatorTreeAnalysis>(F)` 取出支配树，再把 `DT` 打印到 `errs()`。

2. 练习只读不改。回答：如果改成 ModulePass，`run` 的签名应该是什么？第二个参数应该变成哪个类型？

**需要观察的现象**：你会注意到「取分析」这一步本身**不会**把 IR 弄脏——它只是触发 `AnalysisManager` 在需要时计算并缓存支配树。这正是 u3-l1 讲的「分析只读 IR」。

**预期结果**（待本地验证）：
- 答案：ModulePass 的签名是 `PreservedAnalyses run(Module &M, ModuleAnalysisManager &AM)`，第二个参数对应换成 `ModuleAnalysisManager`。这印证了「参数类型决定 IR 单元与配套的分析管理器」。

#### 4.1.5 小练习与答案

**练习 1**：`SimplifyCFGPass` 返回 `PreservedAnalyses::none()`，这对随后想用支配树的 pass 有什么影响？

> **答案**：`none()` 表示「没有任何分析被保留」，于是 `AnalysisManager` 会把缓存的支配树作废；下一个要支配树的 pass 必须重新计算一遍。v2/v3 版本用 `DomTreeUpdater` 增量维护支配树，就是为了避免这种浪费，本可以返回更精确的保留集。

**练习 2**：为什么 `SimplifyCFGPass` 用 `OptionalPassInfoMixin` 而不是 `RequiredPassInfoMixin`？

> **答案**：它是一个普通优化，没必要在 `optnone` / 强制保留的场景里强行运行；用 `OptionalPassInfoMixin` 让它服从管理器的跳过策略即可。只有像 `VerifierPass` 这种「校验合法性、不可省略」的 pass 才需要 `isRequired()=true`。

---

### 4.2 注册到 pipeline

#### 4.2.1 概念说明

写完 pass 类，`opt` 还不认识它。你需要告诉 `PassBuilder`：当用户在 `-passes=...` 里写下某个名字时，把它翻译成「往流水线里加一个 `SimplifyCFGPass()`」。

新 PM 提供两条注册路线，用途完全不同：

| 路线 | 注册接口 | 效果 | 何时触发 |
|------|----------|------|----------|
| **按名字注册** | `registerPipelineParsingCallback` | 用户写 `-passes=tut-simplifycfg` 才跑 | 显式出现在 `-passes` 文本里 |
| **扩展点注入** | `registerPipelineStartEPCallback` 等 | 自动塞进默认 `-O2` 流水线的某个位置 | 跑 `default<O2>` 之类的默认流水线 |

SimplifyCFG 用的是第一条（按名字注册）；第二条由 `examples/Bye/Bye.cpp` 同时演示。两者最终都活在同一个函数里，由 `PassPluginLibraryInfo` 打包，再通过 `llvmGetPassPluginInfo()` 暴露给 `opt`。

#### 4.2.2 核心流程

一个 pass 插件从「源码」到「被 opt 调用」的完整链路：

```text
.cpp 源码
   │  (1) 写 pass 类
   │  (2) 写 get...PluginInfo()，返回 PassPluginLibraryInfo
   │      内含一个 lambda，对传入的 PassBuilder 注册回调
   │  (3) 写 extern "C" llvmGetPassPluginInfo() 暴露该 info
   ▼
add_llvm_pass_plugin → 编译为 .so（MODULE 库）
   ▼
opt -load-pass-plugin=xxx.so
   │  opt 用 dlopen 打开 .so，dlsym 找到 llvmGetPassPluginInfo
   │  拿到 lambda，把它作用于自己的 PassBuilder
   ▼
opt -passes=tut-simplifycfg
   │  PassBuilder 解析流水线文本，遇到 "tut-simplifycfg"
   │  调用你注册的 callback；callback 返回 true 并 addPass(SimplifyCFGPass())
   ▼
SimplifyCFGPass::run 在每个函数上运行
```

`PassPluginLibraryInfo` 是个简单结构体，关键字段是第三项「一个接收 `PassBuilder &` 的回调」——这就是你注册 pass 的唯一入口。它的四个字段分别是：插件 API 版本号、插件名字、LLVM 版本字符串、注册回调。

按名字注册的回调签名（FunctionPass 版本）需要你返回 `bool`：**如果你认领了这个名字就 `addPass` 并返回 `true`，否则返回 `false` 让别的回调继续尝试**。这是一种「责任链」模式。

#### 4.2.3 源码精读

先看 SimplifyCFG 的注册全貌：

[examples/IRTransforms/SimplifyCFG.cpp:394-415](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/IRTransforms/SimplifyCFG.cpp#L394-L415) —— `getExampleIRTransformsPluginInfo()` 把 pass 注册成名字 `"tut-simplifycfg"`；`#ifndef` 守卫下的 `llvmGetPassPluginInfo()` 是 `opt` 通过 `dlopen`/`dlsym` 查找的 C 入口。

```cpp
llvm::PassPluginLibraryInfo getExampleIRTransformsPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "SimplifyCFG", LLVM_VERSION_STRING,
          [](PassBuilder &PB) {
            PB.registerPipelineParsingCallback(
                [](StringRef Name, llvm::FunctionPassManager &PM,
                   ArrayRef<llvm::PassBuilder::PipelineElement>) {
                  if (Name == "tut-simplifycfg") {   // 认领这个名字
                    PM.addPass(SimplifyCFGPass());
                    return true;
                  }
                  return false;                       // 别的名字交给别人
                });
          }};
}

#ifndef LLVM_EXAMPLEIRTRANSFORMS_LINK_INTO_TOOLS
extern "C" LLVM_ATTRIBUTE_WEAK ::llvm::PassPluginLibraryInfo
llvmGetPassPluginInfo() {
  return getExampleIRTransformsPluginInfo();
}
#endif
```

几个要点：

- **`PB.registerPipelineParsingCallback(...)`**：注意它有多个重载，分别对应不同 IR 单元。这里第二个参数是 `FunctionPassManager &PM`，所以这是「Function 层的名字注册」——和 pass 本身是 FunctionPass 一致。如果你写的是 ModulePass，要换成接收 `ModulePassManager &` 的重载。
- **`PM.addPass(SimplifyCFGPass())`**：把一个 pass 实例塞进正在构建的 Function 流水线。`addPass` 按值接收，pass 对象通常是轻量的、无状态的。
- **`extern "C"`**：必须用 C 链接，因为 `opt` 是用 `dlsym("llvmGetPassPluginInfo")` 按名字找符号的，C++ 的 name mangling 会让名字变得不可预测。
- **`LLVM_ATTRIBUTE_WEAK`**：弱符号，允许在「静态链接进工具」时被覆盖。下面的 `#ifndef LLVM_EXAMPLEIRTRANSFORMS_LINK_INTO_TOOLS` 守卫与此呼应。

为什么有那个 `#ifndef` 守卫？看构建脚本就明白了：

[examples/IRTransforms/CMakeLists.txt:9-15](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/IRTransforms/CMakeLists.txt#L9-L15) —— 用 `add_llvm_pass_plugin` 把源文件构造成一个可加载的 MODULE 库（即 `.so`）。

```cmake
if (NOT WIN32 AND NOT CYGWIN)
  add_llvm_pass_plugin(ExampleIRTransforms
    SimplifyCFG.cpp
    DEPENDS intrinsics_gen
    BUILDTREE_ONLY)
  ...
endif()
```

`add_llvm_pass_plugin` 内部有两种模式（参见 [cmake/modules/AddLLVM.cmake:1301-1347](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/cmake/modules/AddLLVM.cmake#L1301-L1347)）：

- 默认（`LLVM_EXAMPLEIRTRANSFORMS_LINK_INTO_TOOLS=OFF`）：构建为 `MODULE` 库，即一个 `libExampleIRTransforms.so`，靠 `opt -load-pass-plugin` 加载。此时 `llvmGetPassPluginInfo` 正常导出。
- 若把 `LLVM_EXAMPLEIRTRANSFORMS_LINK_INTO_TOOLS` 设为 `ON`：构建为 OBJECT 库并静态链接进 opt，编译期会定义宏 `LLVM_EXAMPLEIRTRANSFORMS_LINK_INTO_TOOLS`，于是上面的 `#ifndef` 生效、**不导出** `llvmGetPassPluginInfo`（因为不必再靠 `dlopen` 加载，它在编译期就注册了）。这就是 `#ifndef` 守卫存在的原因。

验证它确实被按名字调用，看回归测试最直接：

[test/Examples/IRTransforms/SimplifyCFG/tut-simplify-cfg1.ll:2-4](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/test/Examples/IRTransforms/SimplifyCFG/tut-simplify-cfg1.ll#L2-L4) —— 用 `-passes=tut-simplifycfg` 调用刚注册的 pass，并对 v1/v2/v3 三个版本各跑一次。

```ll
; RUN: opt %loadexampleirtransforms -passes=tut-simplifycfg -tut-simplifycfg-version=v1 -S < %s | FileCheck %s
; RUN: opt %loadexampleirtransforms -passes=tut-simplifycfg -tut-simplifycfg-version=v2 -S < %s | FileCheck %s
; RUN: opt %loadexampleirtransforms -passes=tut-simplifycfg -tut-simplifycfg-version=v3 -S < %s | FileCheck %s
```

其中 `%loadexampleirtransforms` 是一个 lit 替换，它在 [test/lit.cfg.py:558-566](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/test/lit.cfg.py#L558-L566) 被展开成 `-load-pass-plugin=<shlib>/ExampleIRTransforms.so`（静态链接模式则是空字符串）。这正好串起了「`.so` → `-load-pass-plugin` → 注册名字 → `-passes=tut-simplifycfg`」整条链。

接着看扩展点注入这条路线，对照 `Bye` 示例。它**同时**注册了两种方式：

[examples/Bye/Bye.cpp:41-55](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/Bye/Bye.cpp#L41-L55) —— 先用扩展点回调把 `Bye` 塞进「向量化开始」这个默认流水线位置，再用按名字注册支持 `-passes=goodbye`。

```cpp
void registerPassBuilderCallbacks(PassBuilder &PB) {
  PB.registerVectorizerStartEPCallback(           // ① 扩展点：自动注入到默认流水线
      [](llvm::FunctionPassManager &PM, OptimizationLevel Level) {
        PM.addPass(Bye());
      });
  PB.registerPipelineParsingCallback(             // ② 按名字：-passes=goodbye
      [](StringRef Name, llvm::FunctionPassManager &PM,
         ArrayRef<llvm::PassBuilder::PipelineElement>) {
        if (Name == "goodbye") { PM.addPass(Bye()); return true; }
        return false;
      });
}
```

注意 ① 的回调签名比 ② 多一个 `OptimizationLevel Level` 参数——因为扩展点是「默认流水线的一部分」，管理器需要告诉你当前在跑 `-O1`/`-O2`/`-O3`，你可以据此决定要不要插。

规格里提到的 `registerPipelineStartEPCallback` 与此同类，只是位置不同：

[include/llvm/Passes/PassBuilder.h:501-504](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Passes/PassBuilder.h#L501-L504) —— 在默认优化流水线的**最开头**插入，操作的是 `ModulePassManager`（注意是 Module 层，不是 Function 层）。

```cpp
void registerPipelineStartEPCallback(
    const std::function<void(ModulePassManager &, OptimizationLevel)> &C);
```

这里有个**层级错配**的小坑：`registerPipelineStartEPCallback` 给你的是 `ModulePassManager`，但 SimplifyCFG/Bye 是 FunctionPass。要在 Module 层加一个 FunctionPass，必须用「适配器」把它包一层（如 `createModuleToFunctionPassAdaptor(SimplifyCFGPass())`），否则类型对不上。这就是为什么教程示例优先用按名字注册——它直接在对应的 `FunctionPassManager` 上 `addPass`，没有层级问题。`registerPipelineParsingCallback` 的全部重载见 [PassBuilder.h:590-614](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Passes/PassBuilder.h#L590-L614)，每种 IR 单元（Module/Function/CGSCC/Loop/MachineFunction）各一个。

> 小结两条路线：**想让用户显式调用** → `registerPipelineParsingCallback`；**想自动混进 `-O2` 等默认流水线** → 扩展点回调（注意 IR 单元层级与 `OptimizationLevel` 参数）。

#### 4.2.4 代码实践

**目标**：亲眼看到「注册名字 → 被调用」这条链是通的，不动 C++，只用命令行。

**步骤**：

1. 确认你构建 LLVM 时启用了 examples（默认开）。在构建目录下确认产物存在：`libExampleIRTransforms.so`（或 `.dylib`/`.a`，取决于平台与 `LINK_INTO_TOOLS`）。
2. 准备一个最小 IR 文件 `t.ll`：

   ```ll
   define i32 @simp1() {
   entry:
     br i1 true, label %t, label %f
   t:
     ret i32 10
   f:
     ret i32 12
   }
   ```

3. 运行（请把 `<build>` 换成你的构建目录，`.so` 后缀按平台调整）：

   ```bash
   <build>/bin/opt \
     -load-pass-plugin=<build>/lib/libExampleIRTransforms.so \
     -passes=tut-simplifycfg -tut-simplifycfg-version=v1 -S t.ll
   ```

**需要观察的现象**：`entry` 里的常量条件分支 `br i1 true, ...` 被 v1 的 `eliminateCondBranches_v1` 替换成了无条件分支，且死块 `f` 被删掉。最终 `simp1` 应只剩一个块、一条 `ret i32 10`。

**预期结果**（待本地验证，输出大致如下）：

```ll
define i32 @simp1() {
entry:
  br label %t
t:
  ret i32 10
}
```

> 注意：上面是「观察教学 pass 行为」；第 5 节的综合实践会让你注册一个**自己命名**的 pass。

#### 4.2.5 小练习与答案

**练习 1**：如果你把 `if (Name == "tut-simplifycfg")` 改成 `if (Name == "tut-simplify-cfg")`，需要同步改动哪里才能让 `-passes=tut-simplify-cfg` 生效？

> **答案**：只需要在运行命令里把 `-passes=tut-simplifycfg` 改成 `-passes=tut-simplify-cfg` 即可。名字纯粹是字符串约定，注册回调里的 `Name` 和命令行 `-passes=` 两边对上就行；C++ 类名 `SimplifyCFGPass` 与之无关。

**练习 2**：为什么 `llvmGetPassPluginInfo` 必须是 `extern "C"`，而 `getExampleIRTransformsPluginInfo` 不是？

> **答案**：`opt` 通过 `dlsym` 按符号名 `llvmGetPassPluginInfo` 查找入口，必须保证符号名不被 C++ name mangling 改写，所以要 `extern "C"`。而 `getExampleIRTransformsPluginInfo` 只在 .cpp 内部被调用，不跨链接边界，用正常 C++ 链接即可。

---

### 4.3 统计与调试输出

#### 4.3.1 概念说明

一个 pass 写出来之后，你会反复想问两个问题：**「它到底跑了多少次/改了多少东西？」** 和 **「它内部发生了什么？」**。LLVM 为这两件事分别提供了官方设施：

- **Statistics（统计）**：用 `STATISTIC` 宏声明一个计数器，pass 里 `++Count`；用 `-stats`（或 `-stats-json`）在结束时打印汇总。常用来统计「合并了多少条指令」「删了多少个块」。
- **Debug output（调试输出）**：用 `LLVM_DEBUG(...)` 包住一段打印，只有当用户传 `-debug`（或 `-debug-only=某类型`）时才真正输出。常用来打印 pass 处理每个函数时的中间状态。

这两个设施都依赖同一个文件级宏 **`DEBUG_TYPE`**——一个字符串标签，用来给本文件的统计和调试输出归类。SimplifyCFG 已经定义了它：

[examples/IRTransforms/SimplifyCFG.cpp:57](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/IRTransforms/SimplifyCFG.cpp#L57) —— 文件级标签 `"tut-simplifycfg"`，后续的统计和 `LLVM_DEBUG` 都归到这个类型下。

```cpp
#define DEBUG_TYPE "tut-simplifycfg"
```

> 注意：SimplifyCFG 示例**定义了** `DEBUG_TYPE`，但**并没有真正使用** `STATISTIC` 或 `LLVM_DEBUG`。也就是说它「预留了归类标签，却没有计数/调试输出」。本模块讲清这两个设施的用法，让你能补上。

#### 4.3.2 核心流程

**Statistics 的数据流**：

```text
在文件顶部：#define DEBUG_TYPE "tut-simplifycfg"
声明计数器：STATISTIC(NumDeadBlocks, "Number of dead blocks removed");
在 pass 里：++NumDeadBlocks;
opt 结束时：-stats 触发打印
            ===-------------------------------------------------------------------------===
                                 ... Statistics Collected ...
            3 tut-simplifycfg - Number of dead blocks removed
```

`STATISTIC(VARNAME, DESC)` 实际上展开成一个 `static llvm::Statistic` 对象，里面绑定了三个信息：所属的 `DEBUG_TYPE`、变量名字符串、描述字符串。这三个字符串只会在 `-stats` 打印时才真正拼装，零开销是该设施的设计目标之一。

**Debug output 的数据流**：

```text
#define DEBUG_TYPE "tut-simplifycfg"
LLVM_DEBUG(dbgs() << "Visiting " << F.getName() << "\n");
opt -debug            ← 打印所有类型的调试输出（噪音很大）
opt -debug-only=tut-simplifycfg   ← 只打印本类型
```

`LLVM_DEBUG(...)` 是个宏，在非调试构建里它会被完全编译掉（连字符串字面量都不进二进制），所以可以放心地写详细的调试打印，不会影响 release 性能。

两者共享 `DEBUG_TYPE` 的好处：你用 `-debug-only=tut-simplifycfg` 能同时、且只同时打开「本 pass 的统计与调试」。

#### 4.3.3 源码精读

先看 `STATISTIC` 宏到底展开成什么：

[include/llvm/ADT/Statistic.h:159-173](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/ADT/Statistic.h#L159-L173) —— `STATISTIC(VARNAME, DESC)` 展开成 `static llvm::Statistic VARNAME = {DEBUG_TYPE, #VARNAME, DESC}`，把文件级 `DEBUG_TYPE` 自动绑进计数器。

```cpp
using Statistic = TrackingStatistic;   // （LLVM_ENABLE_STATS 开启时）

#define STATISTIC(VARNAME, DESC)                                               \
  static llvm::Statistic VARNAME = {DEBUG_TYPE, #VARNAME, DESC}
```

三个字段：`DEBUG_TYPE`（归类）、`#VARNAME`（变量名，`#` 是字符串化）、`DESC`（人类可读描述）。当 `LLVM_ENABLE_STATS=OFF` 时，`Statistic` 是 `NoopStatistic`，`++` 变成空操作——这是「零开销」的实现方式。

再看一个**真实**使用了 `STATISTIC` 的生产级 pass，作为正确写法的样板：

[lib/Transforms/Utils/SimplifyCFG.cpp:213-217](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Transforms/Utils/SimplifyCFG.cpp#L213-L217) —— 标准库版 SimplifyCFG 用 `STATISTIC` 统计「把 switch 转成了多少查找表」等指标。

```cpp
STATISTIC(NumBitMaps, "Number of switch instructions turned into bitmaps");
STATISTIC(NumLookupTables, "Number of switch instructions turned into lookup tables");
```

这些计数器随后在该 pass 把 switch 转成查找表时 `++NumLookupTables`。对比教学版 SimplifyCFG——它本可以照着写 `STATISTIC(NumDeadBlocks, "...")` 然后在 `removeDeadBlocks_v1` 删块处自增，但作者为了让初学者聚焦 CFG 变换逻辑而省略了。这就是「示例定义了 `DEBUG_TYPE` 却没用统计」的原因。

至于 `LLVM_DEBUG`，典型用法是：

```cpp
LLVM_DEBUG(dbgs() << "Processing function " << F.getName()
                  << ", blocks=" << F.size() << "\n");
```

它和 `STATISTIC` 一样以 `DEBUG_TYPE` 归类。**两者一起使用**是日常调试 pass 的标配：用统计看「全局改了多少」，用调试看「每个函数上发生了什么」。

#### 4.3.4 代码实践

**目标**：给教学版 SimplifyCFG 补上它「定义了标签却没用」的统计，亲眼看到 `-stats` 输出。

**步骤**（在本地的副本上修改，不改原仓库）：

1. 复制 `examples/IRTransforms/SimplifyCFG.cpp` 到一个 out-of-tree 工程或一个新的 in-tree 副本，命名为 `SimplifyCFGCount.cpp`。
2. 在 `#define DEBUG_TYPE "tut-simplifycfg"` 之后加一行：

   ```cpp
   STATISTIC(NumCondBranches, "Number of constant conditional branches eliminated");
   ```

3. 在 `eliminateCondBranches_v1` 里，每次替换成功后（[第 163 行](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/IRTransforms/SimplifyCFG.cpp#L163) `Changed = true;` 附近）加 `++NumCondBranches;`。
4. 重新构建插件。
5. 运行时加 `-stats`（注意：`-stats` 需要 `LLVM_ENABLE_STATS=ON`，通常 Release 默认开启，Asserts 构建也开启）：

   ```bash
   opt -load-pass-plugin=.../libSimplifyCFGCount.so \
       -passes=tut-simplifycfg -stats -disable-output t.ll
   ```

**需要观察的现象**：标准错误会多出一段 `===----------------- Statistics Collected -------------------===`，其中有一行类似：

```text
1 tut-simplifycfg - Number of constant conditional branches eliminated
```

数字对应 `t.ll` 里被消掉的常量条件分支个数。

**预期结果**（待本地验证）：若 `t.ll` 含 1 个 `br i1 true/false`，则该行显示 `1`；若你关闭统计（`LLVM_ENABLE_STATS=OFF`），则该计数器变成 `NoopStatistic`，`-stats` 不输出任何内容——这正好验证了「零开销」机制。

#### 4.3.5 小练习与答案

**练习 1**：`-stats` 和 `-stats-json` 有什么区别？

> **答案**：`-stats` 打印人类可读的文本汇总（带 `DEBUG_TYPE` 前缀和描述）；`-stats-json` 输出 JSON 格式，便于脚本/CI 解析。两者读的是同一批 `Statistic` 计数器。

**练习 2**：为什么 `LLVM_DEBUG` 在 release 构建里几乎没有性能代价？

> **答案**：`LLVM_DEBUG(...)` 在非 `NDEBUG`（即非 release）构建里才展开成实际打印；在 release 构建里它被编译成空，连 `dbgs() << ...` 里的字符串字面量都不会进入二进制。因此可以放心写详尽的调试输出而不用担心拖慢生产性能。

---

## 5. 综合实践

把三个最小模块串起来，写一个**你自己命名**的 pass：`CountInstPass`，它统计并打印每个函数的指令数。这正好落实规格里的实践任务。

**步骤**：

1. 新建 `CountInst.cpp`，套用本讲学到的「pass 类 + 注册 + 入口」三件套。下面是完整骨架（示例代码，非项目原有文件）：

   ```cpp
   // CountInst.cpp —— 示例代码
   #include "llvm/ADT/Statistic.h"
   #include "llvm/IR/Function.h"
   #include "llvm/IR/InstIterator.h"
   #include "llvm/IR/PassManager.h"
   #include "llvm/Passes/PassBuilder.h"
   #include "llvm/Support/raw_ostream.h"

   using namespace llvm;

   #define DEBUG_TYPE "count-inst"
   STATISTIC(TotalInsts, "Total number of instructions seen");

   namespace {
   struct CountInstPass : PassInfoMixin<CountInstPass> {
     PreservedAnalyses run(Function &F, FunctionAnalysisManager &) {
       unsigned Count = 0;
       for (Instruction &I : instructions(F))   // 跨块扁平遍历（见 u2-l1）
         ++Count;
       TotalInsts += Count;                      // 统计：累加到全局计数器
       errs() << "[count-inst] " << F.getName() << " : " << Count << " instrs\n";
       return PreservedAnalyses::all();          // 只读不改，全保留
     }
   };
   } // namespace

   llvm::PassPluginLibraryInfo getCountInstPluginInfo() {
     return {LLVM_PLUGIN_API_VERSION, "CountInst", LLVM_VERSION_STRING,
             [](PassBuilder &PB) {
               PB.registerPipelineParsingCallback(
                   [](StringRef Name, FunctionPassManager &PM,
                      ArrayRef<PassBuilder::PipelineElement>) {
                     if (Name == "count-inst") {
                       PM.addPass(CountInstPass());
                       return true;
                     }
                     return false;
                   });
             }};
   }

   extern "C" LLVM_ATTRIBUTE_WEAK ::llvm::PassPluginLibraryInfo
   llvmGetPassPluginInfo() {
     return getCountInstPluginInfo();
   }
   ```

   对照本讲要点自检：继承 `PassInfoMixin`（① 4.1）；`run` 返回 `all()` 因为不改 IR；用 `registerPipelineParsingCallback` 注册成 `count-inst`（② 4.2）；用 `STATISTIC` + `DEBUG_TYPE` 加统计（③ 4.3）。

2. 用一个最小 CMakeLists.txt（示例代码）把它构造成插件（请把 `<LLVM build>` 换成你的 LLVM 构建目录）：

   ```cmake
   # CMakeLists.txt —— 示例代码（out-of-tree 插件）
   cmake_minimum_required(VERSION 3.20)
   project(CountInst LANGUAGES CXX)
   find_package(LLVM REQUIRED CONFIG)
   message(STATUS "Using LLVMConfig.cmake in: ${LLVM_DIR}")
   list(APPEND CMAKE_MODULE_PATH "${LLVM_CMAKE_DIR}")
   include(AddLLVM)
   include_directories(SYSTEM ${LLVM_INCLUDE_DIRS})
   add_definitions(${LLVM_DEFINITIONS})
   add_llvm_pass_plugin(CountInst CountInst.cpp)
   ```

3. 配置并构建（`-DLLVM_DIR=<LLVM build>/lib/cmake/llvm`）得到 `libCountInst.so`。

4. 准备输入 `a.ll`：

   ```ll
   define i32 @f(i32 %a, i32 %b) {
   entry:
     %s = add i32 %a, %b
     %m = mul i32 %s, 2
     ret i32 %m
   }
   ```

5. 运行：

   ```bash
   opt -load-pass-plugin=./libCountInst.so \
       -passes=count-inst -stats -disable-output a.ll
   ```

**需要观察的现象**：标准错误先打印 `[count-inst] f : 3 instrs`（add/mul/ret 共 3 条），随后 `-stats` 汇总里出现 `3 count-inst - Total number of instructions seen`。

**预期结果**（待本地验证）：

```text
[count-inst] f : 3 instrs
===-------------------------------------------------------------------------===
                      ... Statistics Collected ...
3 count-inst - Total number of instructions seen
```

指令数的计算就是个简单求和：对一个有 \( n \) 个基本块、第 \( i \) 个块含 \( c_i \) 条指令的函数，指令总数为

\[
\text{Count} \;=\; \sum_{i=1}^{n} c_i
\]

`instructions(F)` 正是按这个扁平方式遍历所有块的所有指令（参见 u2-l1 的 `inst_iterator`）。

> 进阶：把 `run` 里的 `errs()` 打印换成 `LLVM_DEBUG(dbgs() << ...)`，于是只有 `opt -debug-only=count-inst` 才打印每个函数；而 `-stats` 的汇总照常输出。这正好体会「详细日志走 `LLVM_DEBUG`、汇总数字走 `STATISTIC`」的分工。

## 6. 本讲小结

- 一个新 PM pass **没有统一基类**：继承 `PassInfoMixin`（或 `Optional/RequiredPassInfoMixin`）即可，CRTP 让基类拿到派生类的元信息（`name()`、`isRequired()`）。
- **`run` 的签名决定一切**：第一个参数是 `Function &` 就是 FunctionPass，配套的第二个参数是 `FunctionAnalysisManager &`，用 `FAM.getResult<分析>(F)` 取分析结果，返回 `PreservedAnalyses` 声明保留集。
- **让 opt 认识你的 pass** 有两条路：`registerPipelineParsingCallback`（按名字，`-passes=xxx` 显式调用）和扩展点回调如 `registerPipelineStartEPCallback`（自动塞进默认 `-O2` 流水线）；二者都封装在 `PassPluginLibraryInfo` 里，由 `extern "C" llvmGetPassPluginInfo()` 暴露给 `-load-pass-plugin`。
- **`add_llvm_pass_plugin`** 默认把源文件构造成 `.so`（MODULE 库）；设 `LINK_INTO_TOOLS=ON` 则静态链接进 opt，对应 .cpp 里的 `#ifndef` 守卫。
- **统计与调试**：`STATISTIC` 宏 + `-stats` 看「全局改了多少」，`LLVM_DEBUG` + `-debug-only=<DEBUG_TYPE>` 看「每个函数上发生了什么」，二者共享文件级 `DEBUG_TYPE` 标签。
- 教学版 SimplifyCFG 定义了 `DEBUG_TYPE` 却没用统计，是一个让你「补全」的好切入点；生产级 pass（如 `lib/Transforms/Utils/SimplifyCFG.cpp`）才是 `STATISTIC` 的标准样板。

## 7. 下一步学习建议

本讲你已经能写出一个独立、可注册、可观测的 pass。接下来：

1. **u3-l3（Pass 流水线与 PassBuilder）**：本讲的「按名字注册」只是 `PassBuilder` 的一小块能力。下一讲会讲它如何把 `-passes='function(...)'` 这样的**文本流水线**整体解析成 pass 树，以及 `default<O2>` 默认流水线是怎么拼出来的——届时你会更清楚扩展点回调到底插在流水线的哪一格。
2. **u3-l4（Pass 插件机制）**：本讲的 `extern "C" llvmGetPassPluginInfo` 只讲了「写法」，下一讲用 `examples/Bye` 把「`opt` 如何 `dlopen` 插件、如何调用回调」的**加载流程**讲透，并对比静态/动态两种链接方式。
3. **动手延伸**：把综合实践里的 `CountInstPass` 改造成「真的改 IR」——比如删除所有空的、无副作用的非终结指令，并返回精确的 `PreservedAnalyses`（尝试 `PA.preserve<DominatorTreeAnalysis>()`）。这会逼你直面「改了 IR 该作废哪些分析」这一 u3-l1 的核心命题。
4. **阅读推荐**：通读 [examples/IRTransforms/SimplifyCFG.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/IRTransforms/SimplifyCFG.cpp) 的 v2/v3 版本，重点看它如何用 `DomTreeUpdater` 增量维护支配树、如何用 `PatternMatch.h` 的 `match()` 简化判别逻辑——这是从「能跑的 pass」迈向「保住分析的高质量 pass」的关键一步。
