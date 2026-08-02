# 编写你自己的 LLVM Pass（新 PM）

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出新 Pass 管理器（New PM）下一个 pass 的最小骨架由哪几部分组成。
- 用 `OptionalPassInfoMixin` / `RequiredPassInfoMixin` 混入并实现 `run()` 方法，写出自己的 FunctionPass 或 ModulePass。
- 用 `STATISTIC` 宏做计数统计，并用 `-stats` 查看。
- 正确理解并返回 `PreservedAnalyses`（全保住 / 全不保住 / 指定保住）。
- 把一个 pass 注册成可被 `opt -passes=...` 引用：包括「写进树内 `PassRegistry.def`」和「做成插件用 `registerPipelineParsingCallback` 回调注册」两条路径。

本讲是 u4-l1（新 PM 架构）的「动手篇」：上一讲讲了 pass 管理器如何装配与调度，这一讲让你亲自写一个被它调度的小齿轮。

## 2. 前置知识

本讲默认你已经掌握下面三件事（来自依赖讲义）：

1. **新 PM 的四层 IR 单位与 pass 分类**（u4-l1）：新 PM 把 IR 抽象为 Module、CGSCC、Function、Loop 四层；pass 分「变换 pass（改写 IR）」和「分析 pass（只读、产出可复用结果）」两类；`PassManager` 顺序执行变换 pass，`AnalysisManager` 惰性计算并缓存分析结果。
2. **IR 的包含层次**（u3-l1）：`Module ⊃ Function ⊃ BasicBlock ⊃ Instruction`，遍历 API 高度统一（`Function` 的 `begin/end` 遍历基本块，`BasicBlock::size()` 给出指令数，`Function::getInstructionCount()` 累加全部指令）。
3. **`Value` 与 def-use 链**（u3-l2）：`Value` 是 IR 对象的根基类，但本讲只用到「遍历指令计数」，不需要深入改写 use 链。

通俗地说：你已经知道「pass 是流水线上的一道工序」，本讲就教你「如何自己造一道工序，挂到流水线上」。

**一个直觉模型**：把新 PM 想象成一条工厂流水线，IR 是在传送带上流动的工件。你要做的事很简单——写一个「工位」（pass 类），声明这个工位处理什么级别的工件（Function 还是 Module），实现它的动作（`run()`），然后告诉车间主任（`PassBuilder`）这个工位叫什么名字，好让人家能在排程单（`-passes=...`）里点名使用它。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `llvm/docs/WritingAnLLVMNewPMPass.md` | 官方「写一个新 PM pass」手把手教程，本讲的写作蓝本。 |
| `llvm/examples/Bye/Bye.cpp` | 一个完整的 pass **插件**范例：同时含新 PM pass、旧 PM pass、回调注册与插件入口。 |
| `llvm/include/llvm/Transforms/Utils/HelloWorld.h` / `llvm/lib/Transforms/Utils/HelloWorld.cpp` | 官方文档配套的「Hello World」pass：最简的新 PM FunctionPass。 |
| `llvm/include/llvm/IR/PassManager.h` | 定义 `PassInfoMixin` / `RequiredPassInfoMixin` / `OptionalPassInfoMixin` 这套 CRTP 混入。 |
| `llvm/include/llvm/IR/Analysis.h` | 定义 `PreservedAnalyses`（分析保活集合），即 `run()` 的返回类型。 |
| `llvm/include/llvm/ADT/Statistic.h` | 定义 `STATISTIC` 宏与统计计数设施。 |
| `llvm/lib/Passes/PassRegistry.def` | 树内 pass 的「点名册」，`FUNCTION_PASS("name", ...)` 在这里登记。 |
| `llvm/include/llvm/Passes/PassBuilder.h` | `PassBuilder`，提供 `registerPipelineParsingCallback` 等回调注册接口。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 新 PM pass 的骨架**、**4.2 用 STATISTIC 统计与 PreservedAnalyses 返回约定**、**4.3 把 pass 注册进流水线**。

### 4.1 新 PM pass 的骨架：PassInfoMixin 与 run 方法

#### 4.1.1 概念说明

新 PM 下的 pass **不再靠继承某个公共基类接口**来定义，而是采用「基于概念的多态」（concept-based polymorphism）。换句话说：你不需要 `class MyPass : public IPass { void run() override; }` 这样的虚函数接口；你只要让你的类**长得像一个 pass**——具体地说，提供一个 `run()` 方法，并从 `PassInfoMixin` 系列混入一些样板代码即可。

这和旧 PM（legacy pass manager）最大的区别：旧 PM 用纯虚基类 `FunctionPass` 强制接口，新 PM 用 CRTP（Curiously Recurring Template Pattern，奇异递归模板）混入（mixin）来注入样板。

- **CRTP（奇异递归模板）**：`class Derived : public Base<Derived>`，把派生类自己作为模板参数传给基类，基类即可在编译期为派生类生成专属代码，零虚函数开销。
- **mixin（混入）**：一个小工具类，专管提供一组样板方法（如 `name()`、`isRequired()`、`printPipeline()`），让你不必手写。

新 PM 提供三个混入，层层递进：

| 混入 | 含义 | 典型用途 |
|------|------|----------|
| `PassInfoMixin<DerivedT>` | 最底层，提供 `printPipeline`、`name` 等元信息；`isRequired()` 默认 `false`。 | 一般不直接用。 |
| `RequiredPassInfoMixin<DerivedT>` | 继承 `PassInfoMixin`，把 `isRequired()` 设为 `true`。 | 不可被跳过的 pass，如 `AlwaysInlinerPass`。 |
| `OptionalPassInfoMixin<DerivedT>` | 继承 `PassInfoMixin`，把 `isRequired()` 设为 `false`。 | 大多数普通优化 pass，可被 `optnone` 跳过。 |

> 一个 pass 是「必需（required）」还是「可选（optional）」会影响它是否在标注了 `optnone` 的函数上运行——必需 pass（如必须执行的强制内联）仍会跑，可选优化 pass 会被跳过。

一个 pass 的 `run()` 方法签名由它处理的 **IR 层级**决定：

- Function pass：`PreservedAnalyses run(Function &F, FunctionAnalysisManager &AM);`
- Module pass：`PreservedAnalyses run(Module &M, ModuleAnalysisManager &AM);`
- （CGSCC / Loop pass 同理，换掉 IR 单位与管理器类型即可。）

pass 管理器会保证：一个 FunctionPass 会被自动应用到模块里的每一个函数上——你只写「处理一个函数」的逻辑，调度由管理器负责。

#### 4.1.2 核心流程

写一个新 PM pass 的最小步骤：

```
1. 定义类 MyPass，继承 OptionalPassInfoMixin<MyPass>（CRTP）。
2. 声明并实现 run(IRUnit &U, AnalysisManager &AM)。
3. 在 run 里做你想做的事（读、统计、改写 IR）。
4. 根据是否改写了 IR，返回合适的 PreservedAnalyses。
5.（注册留到 4.3）
```

调用方（pass 管理器）视角：

```
PassManager.run(Module, AM)
  └─ 逐个 Function 调用 MyPass.run(Function, AM)
        └─ 你的 run() 被执行
        └─ 返回 PreservedAnalyses
        └─ 管理器据此失效对应的缓存分析
```

注意第 5 步之前，`MyPass` 只是一个普通 C++ 类；它要能被 `opt -passes=myname` 引用，必须经过「注册」，这是 4.3 的内容。

#### 4.1.3 源码精读

**官方最简范例 HelloWorld**——先看头文件，定义 pass 类并声明 `run()`：

[llvm/include/llvm/Transforms/Utils/HelloWorld.h:16-19](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/Transforms/Utils/HelloWorld.h#L16-L19)：`HelloWorldPass` 继承 `OptionalPassInfoMixin<HelloWorldPass>`，声明一个针对 `Function` 的 `run()`。`LLVM_ABI` 宏与 ABI 导出有关，初学可忽略。

再看实现，整个 pass 的逻辑只有一行——打印函数名：

[llvm/lib/Transforms/Utils/HelloWorld.cpp:14-18](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/lib/Transforms/Utils/HelloWorld.cpp#L14-L18)：`errs() << F.getName()` 把函数名打到标准错误流；返回 `PreservedAnalyses::all()` 表示「我没改任何 IR，所有分析都还成立」。

**混入的定义**——看 `PassInfoMixin` 系列如何注入样板：

[llvm/include/llvm/IR/PassManager.h:88-111](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/IR/PassManager.h#L88-L111)：`PassInfoMixin<DerivedT>` 提供 `printPipeline`（用于 `-print-pipeline-passes` 打印流水线文本）和默认的 `isRequired(){return false;}`；`RequiredPassInfoMixin` 把它改写为 `true`，`OptionalPassInfoMixin` 显式保持 `false`。注意注释里那句「Actual passes should inherit from RequiredPassInfoMixin or OptionalPassInfoMixin」——说明直接继承底层的 `PassInfoMixin` 不是推荐做法。

**Bye 示例里的新 PM pass**（更接近实战，因为它带注册）：

[llvm/examples/Bye/Bye.cpp:33-39](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/examples/Bye/Bye.cpp#L33-L39)：`struct Bye : OptionalPassInfoMixin<Bye>` 的 `run()` 调用 `runBye(F)`，根据返回值决定保活分析：没改就 `PreservedAnalyses::all()`，改了就 `PreservedAnalyses::none()`。这演示了「按是否改写返回不同保活集合」的标准写法。

> 提示：`struct` 与 `class` 在 C++ 里仅默认访问性不同；LLVM 的 pass 类常用 `struct` 以省去 `public:`。

#### 4.1.4 代码实践

**实践目标**：照着 HelloWorld 的形状，徒手写出一个 FunctionPass 骨架（暂不注册，先确认它能编译）。

**操作步骤**：

1. 在你已构建的 LLVM 源码树里新建头文件 `llvm/include/llvm/Transforms/Utils/InstrCount.h`，内容（示例代码，非项目原有）：

   ```cpp
   #ifndef LLVM_TRANSFORMS_UTILS_INSTRCOUNT_H
   #define LLVM_TRANSFORMS_UTILS_INSTRCOUNT_H

   #include "llvm/IR/PassManager.h"

   namespace llvm {
   class InstrCountPass : public OptionalPassInfoMixin<InstrCountPass> {
   public:
     PreservedAnalyses run(Function &F, FunctionAnalysisManager &AM);
   };
   } // namespace llvm
   #endif
   ```

2. 新建 `llvm/lib/Transforms/Utils/InstrCount.cpp`（示例代码）：

   ```cpp
   #include "llvm/Transforms/Utils/InstrCount.h"
   #include "llvm/IR/Function.h"
   using namespace llvm;

   PreservedAnalyses InstrCountPass::run(Function &F, FunctionAnalysisManager &AM) {
     errs() << "function " << F.getName() << " has "
            << F.getInstructionCount() << " instructions\n";
     return PreservedAnalyses::all();
   }
   ```

3. 把这个 `.cpp` 加进 `llvm/lib/Transforms/Utils/CMakeLists.txt`（在现有源文件列表里追加一行 `InstrCount.cpp`），然后 `ninja -C build/`（或你的构建命令）。

**需要观察的现象**：能成功编译，不报「未定义的 `run`」之类链接错误；此时还没有注册，所以 `opt -passes=instrcount` 会报 `unknown pass name`，这是预期的（4.3 会解决）。

**预期结果**：编译通过；`F.getInstructionCount()` 返回该函数所有基本块指令数之和（见 4.2 的源码）。

**待本地验证**：实际编译行为取决于你的构建配置；若 `getInstructionCount` 报弃用警告，改用遍历 `BB.size()` 亦可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 HelloWorld 继承的是 `OptionalPassInfoMixin` 而不是 `RequiredPassInfoMixin`？

**参考答案**：HelloWorld 只打印函数名、不改任何 IR，属于可有可无的观察型 pass。用 `OptionalPassInfoMixin` 表示「可被跳过」，这样当某函数带 `optnone` 属性时它不会被强行运行；而 `RequiredPassInfoMixin` 留给「不可跳过」的 pass（如 `AlwaysInlinerPass` 必须执行以保住 `alwaysinline` 语义）。

**练习 2**：把 HelloWorld 改成一个 **Module pass**（处理整个模块而非单个函数），需要改哪些地方？

**参考答案**：① `run()` 签名改成 `PreservedAnalyses run(Module &M, ModuleAnalysisManager &AM);`；② 头文件 include 不变（仍是 `PassManager.h`）；③ 实现里遍历 `for (Function &F : M)` 自行逐个处理（因为 Module pass 不会被自动下钻到每个函数）。注册时的宏也要从 `FUNCTION_PASS` 改成 `MODULE_PASS`（见 4.3）。

---

### 4.2 用 STATISTIC 统计与 PreservedAnalyses 返回约定

#### 4.2.1 概念说明

写出能跑的 pass 之后，两件最常做的事是「**统计**」和「**正确声明副作用**」。本模块讲清楚这两点。

**STATISTIC 宏**：LLVM 提供一套轻量的全局计数设施。你写一行

```cpp
STATISTIC(NumInstrs, "Number of instructions counted");
```

就在该编译单元里声明了一个 `static` 计数器 `NumInstrs`，运行时用 `++NumInstrs` 累加，最后由 `opt -stats` 汇总打印。它比手搓 `errs() <<` 更规整：多个 pass 的计数会按 `DEBUG_TYPE` 分组、统一格式输出，且默认是线程安全的原子计数。

- 关键前提：计数器归属到某个 `DEBUG_TYPE`。每个 `.cpp` 文件顶部用 `#define DEBUG_TYPE "yourpass"` 给整个文件命名，`STATISTIC` 宏会自动把计数器挂到这个 `DEBUG_TYPE` 下。
- 计数是否生效取决于 CMake 选项 `LLVM_ENABLE_STATS`（断言构建通常默认开启）；若关闭，`STATISTIC` 会退化成「空操作（noop）」计数器，`++` 不产生任何代码——这就是 `Statistic.h` 里 `TrackingStatistic` 与 `NoopStatistic` 的区分。

**PreservedAnalyses 返回约定**：`run()` 必须返回一个 `PreservedAnalyses`，它告诉管理器「我跑完之后，哪些分析结果还成立、可以继续用缓存」。这是新 PM 分析缓存**失效（invalidation）**机制的核心输入（u4-l1、u4-l2 已铺垫）。

三种常用取值：

| 写法 | 含义 | 何时用 |
|------|------|--------|
| `PreservedAnalyses::all()` | 全部分析都保活 | 你**没改** IR（只读、统计、打印）。 |
| `PreservedAnalyses::none()` | 全部分析都失效 | 你做了**大改写**，保险起见丢弃所有缓存。 |
| `PA.preserve<AnalysisT>()` / `preserveSet<...>()` | 精确保住某(几)类分析 | 你改了 IR 但知道某分析（如支配树）仍成立——更精细、更高效。 |

#### 4.2.2 核心流程

**统计流程**：

```
.cpp 顶部：#define DEBUG_TYPE "instrcount"
声明：STATISTIC(NumInstrs, "...")
run() 内：每遇一条指令 ++NumInstrs;
运行：opt -stats -passes=instrcount 输入.ll
  └─ 程序结束时打印：
       ===-------------------------------------------------------------------------===
                                ... Statistics ...
       "instrcount" pass:
        12 NumInstrs - Number of instructions counted
```

**失效流程（PreservedAnalyses 的去向）**：

```
Pass 返回 PA
  └─ PassManager 把 PA 喂给 AnalysisManager.invalidate(IRUnit, PA)
        └─ 对每个缓存的分析结果：
              - 若 PA 保住了它 → 留着
              - 否则 → 丢弃（连带失效下游，见 u4-l2 的 Invalidator）
```

多个 pass 串行时，流水线累计保活的是各 pass 保活集合的**交集**：

\[
P_{\text{累积}} = P_1 \cap P_2 \cap \dots \cap P_n
\]

直觉解释：只要有一条工序声称「某个分析现在不可信了」，那这个分析在后续就必须重算——「最严苛的那次」决定结果。这正是 `PreservedAnalyses::intersect()` 的语义。

#### 4.2.3 源码精读

**STATISTIC 宏的定义**：

[llvm/include/llvm/ADT/Statistic.h:165-173](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/ADT/Statistic.h#L165-L173)：`#define DEBUG_TYPE` 决定计数器归属；`STATISTIC(VARNAME, DESC)` 展开为一个 `static llvm::Statistic` 变量，带 `DEBUG_TYPE`、变量名、描述三项。当 `LLVM_ENABLE_STATS` 关闭时，`Statistic` 类型别名为 `NoopStatistic`（[Statistic.h:159-163](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/ADT/Statistic.h#L159-L163)），其 `operator++` 是空操作。

**真实 pass 里 STATISTIC 的用法**——以 InstSimplify 为例：

[llvm/lib/Transforms/Scalar/InstSimplifyPass.cpp:24-26](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/lib/Transforms/Scalar/InstSimplifyPass.cpp#L24-L26)：先 `#define DEBUG_TYPE "instsimplify"`，再 `STATISTIC(NumSimplified, "Number of redundant instructions removed")`。这是「计数 + DEBUG_TYPE」的标准三件套写法。

**PreservedAnalyses 的两个静态工厂**：

[llvm/include/llvm/IR/Analysis.h:115-121](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/IR/Analysis.h#L115-L121)：`none()` 返回空集合（什么都不保活），`all()` 向集合插入特殊标记 `&AllAnalysesKey` 表示「全部保活」。

**交集运算**（即上面公式的实现）：

[llvm/include/llvm/IR/Analysis.h:193-214](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/IR/Analysis.h#L193-L214)：`intersect(const PreservedAnalyses &Arg)` 实现「取交集」——若对方全部保活则保留己方集合，否则逐项求交。

**我们要统计的指令数从哪来**：

[llvm/lib/IR/Function.cpp:361-366](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/lib/IR/Function.cpp#L361-L366)：`Function::getInstructionCount()` 遍历所有基本块，把每个块的 `size()`（指令数）累加。它对应头文件声明 [llvm/include/llvm/IR/Function.h:208](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/IR/Function.h#L208)。所以 4.1 里 `F.getInstructionCount()` 拿到的就是「该函数全部指令条数」。

> 修正说明：本模块引用的 `Analysis.h` 路径为 `llvm/include/llvm/IR/Analysis.h`，行号以本仓库当前 HEAD 为准。

#### 4.2.4 代码实践

**实践目标**：给 4.1 的 `InstrCountPass` 加上 `STATISTIC`，统计所有函数的总指令数，并用 `-stats` 查看。

**操作步骤**：

1. 修改 `llvm/lib/Transforms/Utils/InstrCount.cpp`（示例代码）：

   ```cpp
   #include "llvm/Transforms/Utils/InstrCount.h"
   #include "llvm/ADT/Statistic.h"   // STATISTIC 宏
   #include "llvm/IR/Function.h"

   #define DEBUG_TYPE "instrcount"   // 必须在使用 STATISTIC 之前
   STATISTIC(NumInstrs, "Number of instructions counted");

   using namespace llvm;

   PreservedAnalyses InstrCountPass::run(Function &F, FunctionAnalysisManager &AM) {
     NumInstrs += F.getInstructionCount();
     return PreservedAnalyses::all();
   }
   ```

2. 重新编译（4.3 注册完成后才能用 `opt -passes=instrcount`）。

3. 准备输入 `a.ll`：

   ```llvm
   define i32 @foo() {
     %a = add i32 2, 3
     %b = mul i32 %a, 2
     ret i32 %b
   }
   define void @bar() {
     ret void
   }
   ```

4. 运行（注册后）：

   ```console
   $ opt -disable-output -stats -passes=instrcount a.ll
   ```

**需要观察的现象**：程序结束时打印一段 `=== ... Statistics ... ===`，其中 `instrcount` 分组下出现 `NumInstrs` 计数。

**预期结果**：`@foo` 有 3 条指令（add/mul/ret），`@bar` 有 1 条（ret），总计 4 条，因此 `NumInstrs` 应显示为 4。

**待本地验证**：`-stats` 是否有输出取决于 LLVM 是否以 `LLVM_ENABLE_STATS=ON` 构建；若无输出，在 CMake 配置时显式加 `-DLLVM_ENABLE_STATS=ON` 重新构建。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `run()` 的返回值从 `PreservedAnalyses::all()` 改成 `PreservedAnalyses::none()`，会发生什么？

**参考答案**：本 pass 只读不改 IR，正确做法是 `all()`。若错误地返回 `none()`，管理器会认为「所有分析都失效了」而清空 `AnalysisManager` 缓存——下游 pass 再要这些分析就得重算，导致不必要的重复计算与性能损失；功能上不会出错，但白白浪费了缓存。

**练习 2**：`#define DEBUG_TYPE "instrcount"` 这行如果漏写会怎样？

**参考答案**：`STATISTIC` 宏内部依赖 `DEBUG_TYPE` 来标记计数器归属（见 [Statistic.h:168](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/ADT/Statistic.h#L168)）。若漏写，`DEBUG_TYPE` 会被某个前置头文件默认定义为 `"nullptr"` 或空，导致统计计数被归到错误的分组名下，输出里看不到 `"instrcount"` 分组。务必在引用 `STATISTIC` **之前**定义 `DEBUG_TYPE`。

---

### 4.3 把 pass 注册进流水线

#### 4.3.1 概念说明

到目前为止，`InstrCountPass` 还只是个孤立类，`opt` 不认识它。注册就是「给 pass 取个名字，让 `PassBuilder` 在解析 `-passes=名字` 时能找到它」。新 PM 提供两条注册路径：

| 路径 | 做法 | 是否需重编 LLVM | 适用场景 |
|------|------|-----------------|----------|
| **A. 树内静态注册** | 在 `PassRegistry.def` 里加一行宏，并在 `PassBuilder.cpp` 里 `#include` 头文件 | 是（与 LLVM 一起重编） | 学习、或你本来就在改 LLVM。 |
| **B. 插件回调注册** | 写一个动态库（`.so`），用 `registerPipelineParsingCallback` 注册名字 | 否（独立编译，`opt -load-pass-plugin` 加载） | 不想重编 LLVM、分发第三方 pass。 |

路径 B 的完整机制（`PassPluginLibraryInfo`、`llvmGetPassPluginInfo` 入口）是下一单元 u9-l2「Pass 插件机制」的主题；本讲先用它（Bye 范例）让你建立直觉，深入留到后面。

**PassRegistry.def 的巧妙设计**：它不是普通源文件，而是一个被 `PassBuilder.cpp` **多次 `#include`** 的「宏展开表」。同一段 `FUNCTION_PASS("name", SomePass())` 会被不同的外层宏（如 `FUNCTION_PASS`、`FUNCTION_PASS_WITH_PARAMS`）以不同方式展开——既用来注册 `-passes` 文本解析，也用来注册默认流水线。所以你只需在 `FUNCTION_PASS` 区段加一行，就同时打通了「文本点名」和「默认装配」两个入口。

#### 4.3.2 核心流程

**路径 A（树内）**：

```
1. 在 PassRegistry.def 的 FUNCTION_PASS 区段加：
     FUNCTION_PASS("instrcount", InstrCountPass())
2. 在 PassBuilder.cpp 顶部加：
     #include "llvm/Transforms/Utils/InstrCount.h"
3. 重编 LLVM，得到新的 opt。
4. opt -passes=instrcount a.ll   ← 现在 opt 认识这个名字了
```

**路径 B（插件）**：

```
1. 写 Bye.cpp 式的源文件，提供：
   - registerPassBuilderCallbacks(PassBuilder &PB) 内调用
       PB.registerPipelineParsingCallback(...)
       把 "goodbye" 这个名字映射到 Bye()
2. 提供 getByePluginInfo() 返回 PassPluginLibraryInfo
3. 提供 extern "C" llvmGetPassPluginInfo() 入口
4. 用 add_llvm_pass_plugin() 编成动态库
5. opt -load-pass-plugin=./Bye.so -passes=goodbye a.ll
```

`registerPipelineParsingCallback` 的回调签名决定了它能在哪一层注册名字——有 Function/Module/CGSCC/Loop/MachineFunction 五个重载，分别对应五层 pass 管理器。

#### 4.3.3 源码精读

**路径 A 的点名册**——HelloWorld 的注册：

[llvm/lib/Passes/PassRegistry.def:463](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/lib/Passes/PassRegistry.def#L463)：`FUNCTION_PASS("helloworld", HelloWorldPass())` 一行，左边是 `-passes` 里用的文本名，右边是 pass 类的无参构造。同区段上下都是其它内置 FunctionPass（如 `gvn-hoist`、`indirectbr-expand`），可见这是所有树内 FunctionPass 的统一登记处。

官方文档对这套注册步骤的原文说明见 [llvm/docs/WritingAnLLVMNewPMPass.md:131-151](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/docs/WritingAnLLVMNewPMPass.md#L131-L151)（强调 `PassRegistry.def` 被 `#include` 多次、需同步 `#include` 头文件）。运行方式见 [llvm/docs/WritingAnLLVMNewPMPass.md:153-177](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/docs/WritingAnLLVMNewPMPass.md#L153-L177)：`opt -disable-output /tmp/a.ll -passes=helloworld` 会打印出 `foo`、`bar`。

**路径 B 的注册回调**——Bye 如何把 `goodbye` 挂上去：

[llvm/examples/Bye/Bye.cpp:41-55](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/examples/Bye/Bye.cpp#L41-L55)：`registerPassBuilderCallbacks` 做了两件事——① `registerVectorizerStartEPCallback` 把 Bye 插到默认流水线的「向量化起点」扩展点（EP）；② `registerPipelineParsingCallback` 注册函数级名字：当 `-passes` 里出现 `goodbye` 时，向 `FunctionPassManager` 添加 `Bye()` 并返回 `true`（表示「这个名字我认领了」）。

`registerPipelineParsingCallback` 的回调约定：返回 `true` 表示成功解析、`false` 表示「不是我负责的名字」让其它回调继续尝试。这正是 [llvm/include/llvm/Passes/PassBuilder.h:590-614](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/Passes/PassBuilder.h#L590-L614) 里那五个重载（CGSCC/Function/Loop/Module/MachineFunction）的统一形态。

**插件入口**——Bye 的导出符号：

[llvm/examples/Bye/Bye.cpp:81-91](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/examples/Bye/Bye.cpp#L81-L91)：`getByePluginInfo()` 构造一个 `PassPluginLibraryInfo`（含 API 版本、名字、版本号、注册回调、preCodeGen 回调）；`extern "C" LLVM_ATTRIBUTE_WEAK llvmGetPassPluginInfo()` 是 `opt -load-pass-plugin` 加载时查找的动态库入口符号。`#ifndef LLVM_BYE_LINK_INTO_TOOLS` 控制是「做成插件」还是「静态链进工具」。

**插件的 CMake**：

[llvm/examples/Bye/CMakeLists.txt:9-16](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/examples/Bye/CMakeLists.txt#L9-L16)：`add_llvm_pass_plugin(Bye Bye.cpp ...)` 是构建 pass 插件的标准 CMake 函数，注释说明了插件「不在自身链接 Support/Core 库、而指望宿主进程已加载它们」的设计。

#### 4.3.4 代码实践

**实践目标**：用 **路径 A（树内静态注册）** 让 `InstrCountPass` 可被 `opt -passes=instrcount` 引用，并跑通 4.2 的统计。

**操作步骤**：

1. 在 `llvm/lib/Passes/PassRegistry.def` 的 `FUNCTION_PASS` 区段（与 `helloworld` 相邻处）加一行：

   ```cpp
   FUNCTION_PASS("instrcount", InstrCountPass())
   ```

2. 在 `llvm/lib/Passes/PassBuilder.cpp` 的 include 区加：

   ```cpp
   #include "llvm/Transforms/Utils/InstrCount.h"
   ```

3. 重编：`ninja -C build/ opt`。

4. 跑：

   ```console
   $ cat a.ll
   define i32 @foo() {
     %a = add i32 2, 3
     %b = mul i32 %a, 2
     ret i32 %b
   }
   define void @bar() {
     ret void
   }
   $ opt -disable-output -stats -passes=instrcount a.ll
   ```

**需要观察的现象**：命令不再报 `unknown pass name`；结尾打印 `instrcount` 分组的 `NumInstrs` 计数。

**预期结果**：`NumInstrs` 为 4（foo 的 3 条 + bar 的 1 条）。若去掉 `-stats`，则计数不打印（但 pass 仍运行）。

**扩展（路径 B，选做）**：参照 `Bye.cpp`，把同样的 pass 写成插件 `libInstrCount.so`，用 `PB.registerPipelineParsingCallback(...)` 注册 `"instrcount"`，再 `opt -load-pass-plugin=./libInstrCount.so -passes=instrcount a.ll` 运行。注意 Bye 同时注册了「文本名」和「扩展点」两种入口，初学可只保留文本名回调。

**待本地验证**：路径 A 需要重编整个 `opt`；若不想重编，可直接走路径 B 的插件方式。

#### 4.3.5 小练习与答案

**练习 1**：为什么改了 `PassRegistry.def` 之后，还要在 `PassBuilder.cpp` 里加 `#include`？

**参考答案**：`PassRegistry.def` 里写的是 `InstrCountPass()`，需要知道这个类的完整定义才能调用其构造。`PassRegistry.def` 本身被 `PassBuilder.cpp` 多次 `#include`，因此 `PassBuilder.cpp` 必须在 `#include "PassRegistry.def"` 之前先 `#include "llvm/Transforms/Utils/InstrCount.h"`，否则编译器找不到 `InstrCountPass` 的定义而报错。

**练习 2**：`registerPipelineParsingCallback` 返回 `false` 和返回 `true` 各代表什么？

**参考答案**：回调被调用时传入「待解析的名字」`Name`。返回 `true` 表示「这个名字是我的，我已经把它加进了 `PassManager`」；返回 `false` 表示「这不是我负责的名字，请继续问其它回调」。Bye 的实现里，只有当 `Name == "goodbye"` 才 `addPass` 并返回 `true`，其余情况返回 `false`。这种「逐个询问、认领即止」的设计让多个插件能共存而不冲突。

---

## 5. 综合实践

把三个模块串起来，完成一个「带统计的指令计数 pass」并注册运行。这是本讲的收口任务。

**任务**：写一个新 PM FunctionPass `InstrCountPass`，要求：

1. 用 `OptionalPassInfoMixin` 混入，`run(Function &F, FunctionAnalysisManager &AM)` 实现。
2. 用 `STATISTIC(NumInstrs, ...)` 累计每个函数的指令数（用 `F.getInstructionCount()`）。
3. 用 `errs()` 额外打印每个函数名与它的指令数。
4. 正确返回 `PreservedAnalyses::all()`。
5. 经 **路径 A** 注册为 `instrcount`，用 `opt -stats -passes=instrcount` 运行 `a.ll`。

**参考答案（示例代码，非项目原有）**：

```cpp
// llvm/include/llvm/Transforms/Utils/InstrCount.h
#ifndef LLVM_TRANSFORMS_UTILS_INSTRCOUNT_H
#define LLVM_TRANSFORMS_UTILS_INSTRCOUNT_H
#include "llvm/IR/PassManager.h"
namespace llvm {
class InstrCountPass : public OptionalPassInfoMixin<InstrCountPass> {
public:
  PreservedAnalyses run(Function &F, FunctionAnalysisManager &AM);
};
} // namespace llvm
#endif
```

```cpp
// llvm/lib/Transforms/Utils/InstrCount.cpp
#include "llvm/Transforms/Utils/InstrCount.h"
#include "llvm/ADT/Statistic.h"
#include "llvm/IR/Function.h"
using namespace llvm;

#define DEBUG_TYPE "instrcount"
STATISTIC(NumInstrs, "Number of instructions counted");

PreservedAnalyses InstrCountPass::run(Function &F, FunctionAnalysisManager &AM) {
  unsigned N = F.getInstructionCount();
  errs() << "function " << F.getName() << " has " << N << " instructions\n";
  NumInstrs += N;
  return PreservedAnalyses::all();
}
```

注册两处（路径 A）：`PassRegistry.def` 加 `FUNCTION_PASS("instrcount", InstrCountPass())`；`PassBuilder.cpp` 加 `#include "llvm/Transforms/Utils/InstrCount.h"`。

**自检清单**：

- [ ] `-passes=instrcount` 不再报 `unknown pass name`。
- [ ] 终端逐行打印每个函数名 + 指令数。
- [ ] `-stats` 末尾出现 `instrcount` 分组，`NumInstrs` 等于所有函数指令数之和。
- [ ] 去掉 `-stats` 后 pass 仍正常运行、只是不打印计数。

**进阶**：把同样的逻辑用 **路径 B（插件）** 实现一遍（参照 `Bye.cpp`），用 `-load-pass-plugin` 加载，体会「不重编 LLVM 也能扩展 opt」的便利——这正好为 u9-l2「Pass 插件机制」热身。

## 6. 本讲小结

- 新 PM pass 不靠虚函数接口，而靠 **CRTP 混入**（`OptionalPassInfoMixin` / `RequiredPassInfoMixin`）+ 一个 `run(IRUnit&, AnalysisManager&)` 方法来定义；管理器会自动把 FunctionPass 应用到每个函数。
- `STATISTIC` 宏配合 `#define DEBUG_TYPE` 提供规整的全局计数，用 `opt -stats` 查看；是否生效取决于 `LLVM_ENABLE_STATS`。
- `run()` 必须返回 `PreservedAnalyses`：只读返回 `all()`，大改返回 `none()`，精细控制用 `preserve<T>()`；流水线累计保活为各 pass 保活集合的**交集**。
- 注册让 pass 拥有 `-passes` 里的名字：**路径 A** 改 `PassRegistry.def`（需重编），**路径 B** 做插件用 `registerPipelineParsingCallback`（不重编，详见 u9-l2）。
- `PassRegistry.def` 是被 `PassBuilder.cpp` 多次 `#include` 的宏展开表，加一行即可同时打通「文本点名」与「默认装配」。
- HelloWorld 是最简骨架，Bye 是带注册与插件入口的完整范例，二者是本讲的两块「参照石」。

## 7. 下一步学习建议

- **紧接着**：学习 u9-l1「测试体系：lit、FileCheck 与单元测试」，为你刚写的 pass 配一个 `instrcount.ll` + FileCheck 的回归测试（参照 HelloWorld 的 [helloworld.ll 测试模式](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/docs/WritingAnLLVMNewPMPass.md#L185-L202)），让 pass 的行为可被持续验证。
- **深化插件**：u9-l2「Pass 插件机制」会展开本讲路径 B 的全部细节（`PassPluginLibraryInfo`、扩展点 EP、`-load-pass-plugin`），建议紧接着读。
- **写会改写 IR 的 pass**：本讲的 pass 是只读的；想练习真正「改 IR」并正确返回 `PreservedAnalyses::none()` / `preserve<>()`，可阅读 `llvm/lib/Transforms/InstCombine/InstructionCombining.cpp`、`llvm/lib/Transforms/Scalar/GVN.cpp`（u4-l3）。
- **阅读源码顺序**：`WritingAnLLVMNewPMPass.md`（手把手）→ `HelloWorld.{h,cpp}`（最简）→ `Bye.cpp`（带注册）→ `PassManager.h` 的 mixin 定义 → `PassRegistry.def` 的宏展开。
