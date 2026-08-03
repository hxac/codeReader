# 插件入口与 LLVM Pass 注册

> 前置承接：本讲假设你已读完 [u1-l1 多面体模型入门](u1-l1-project-overview.md)、[u1-l2 构建系统与目录结构](u1-l2-build-and-layout.md)、[u1-l3 通过 clang 与 opt 使用 Polly](u1-l3-using-polly.md)。你已经知道 Polly 编译出一个可加载插件 `LLVMPolly`、入口在 `lib/Plugin/Polly.cpp`，并且 `-polly` / `-passes=polly` 最终都落到同一个 `PollyFunctionPass`。本讲要回答的核心问题是:**这个插件是「怎么被发现、被加载、被挂进 LLVM 默认 `-O3` 流水线」的**。

## 1. 本讲目标

读完本讲，你应当能够:

- 说清 **LLVM Pass Plugin 机制**：一个共享库靠导出一个 C 符号就被编译器识别，并能讲出 Polly 的入口符号。
- 说清 **LLVM New Pass Manager（新 Pass 管理器）** 里「扩展点（Extension Point, EP）回调」的概念，以及 Polly 用了哪两个 EP。
- 把从 `llvmGetPassPluginInfo` 到 `registerPollyPasses`、再到 `buildEarlyPollyPipeline` / `buildLatePollyPipeline` 的整条调用链画出来。
- 解释 `-polly-position=early` 与 `-polly-position=before-vectorizer` 各自注册了哪个 EP 回调，并指出**源码注释与实际默认值不一致**这一真实问题。
- 写出触发 Polly 优化的最小命令行（clang 与 opt 两条路径）。

## 2. 前置知识

在进入源码前，先用最朴素的语言建立三个直觉。

### 2.1 什么是「插件（Plugin）」

很多大型程序都支持插件——主程序在运行时去加载一个外部的共享库（Linux 下是 `.so`），让外部代码「插」进来扩展功能。LLVM 也支持这种方式：`clang`、`opt`、`bugpoint` 这些前端工具可以在启动时加载一个 pass 插件，从而获得原本没有的优化 pass。

插件的核心约定只有一条：**这个共享库必须导出一个名字固定、签名固定的 C 函数**。主程序（LLVM 工具）只要去共享库里找这个符号，找到了就认它是 pass 插件，找不到就当普通库。我们后面会看到这个符号叫 `llvmGetPassPluginInfo`。

### 2.2 什么是「Pass Manager」与「New PM」

LLVM 的优化是以一个一个 **pass（趟）** 的形式组织的，每个 pass 读入 LLVM-IR、做一类变换、再吐出 IR。负责把 pass 按顺序串起来跑的那个调度器就是 **Pass Manager**。

LLVM 目前使用的是 **New Pass Manager（新 PM）**，它有几个本讲要用到的关键设计:

| 概念 | 通俗解释 | 在本讲的角色 |
|------|----------|--------------|
| `PassBuilder` | 「流水线构建器」，负责把 `-passes='...'` 文本解析成 pass 链，并暴露各种「挂钩」 | Polly 注册回调的入口对象 |
| Analysis Manager | 缓存各类分析结果（如别名分析、循环信息） | 决定 Polly 的分析在阶段间能否复用（U2 详述） |
| **Extension Point（EP，扩展点）回调** | 默认 `-O3` 流水线预留的「钩子」，允许外部代码在某些固定位置插入 pass | **Polly 挂进 `-O3` 的关键** |
| `registerPipelineParsingCallback` | 注册「自定义 pass 名」的解析器，让 `-passes=名字` 能被识别 | 让 `-passes='polly'` 能用 |

一句话:**New PM 的扩展性，主要靠「注册各种回调」来实现。Polly 要做的，就是把这些回调注册进去。**

### 2.3 插件与 New PM 怎么接上

插件负责「被加载」，New PM 负责「把 pass 排进流水线」。它们的衔接点是 `PassBuilder`：主程序加载插件、找到入口符号后，会把工具内部的 `PassBuilder` 对象作为参数，交给插件里那个注册函数。插件拿到 `PassBuilder`，就能源源地把自己的一堆回调注册上去。本讲下面两节，就是分别讲「插件怎么被发现」和「拿到 PassBuilder 后怎么挂回调」。

## 3. 本讲源码地图

本讲只涉及 4 个源码文件，按「由外到内」的调用顺序列出:

| 文件 | 行数级别 | 作用 |
|------|----------|------|
| [lib/Plugin/Polly.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Plugin/Polly.cpp) | 极短（~20 行） | 导出 C 符号 `llvmGetPassPluginInfo`，是**整个插件对外的唯一入口** |
| [include/polly/RegisterPasses.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/RegisterPasses.h) | 头文件 | 声明 `registerPollyPasses(PassBuilder&)` 与 `getPollyPluginInfo()` |
| [lib/Support/RegisterPasses.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp) | 本讲主力（~700 行） | 实现 `getPollyPluginInfo()`、`registerPollyPasses()`、两个位置流水线构建、选项解析 |
| [lib/Support/PollyPasses.def](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/PollyPasses.def) | 极短（~20 行） | 用 X-Macro 登记所有 pass 名（`polly`、`polly-custom`、`polly-inline`） |

> 还会顺带引用 [include/polly/Pass/PollyFunctionPass.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Pass/PollyFunctionPass.h) 作为「Polly 的 pass 真身」，但它属于 U2 主题，本讲只点到为止。

---

## 4. 核心概念与源码讲解

本讲拆成两个最小模块:

- **4.1 Pass Plugin API**：Polly 如何被发现与加载。
- **4.2 LLVM New Pass Manager**：`registerPollyPasses` 如何把 Polly 挂进 `-O3`（含两个位置与 `-passes=` 名称）。

### 4.1 Pass Plugin API：Polly 如何被发现与加载

#### 4.1.1 概念说明

LLVM 的 pass 插件协议规定:一个共享库只要导出下面这个 C 函数，就被认定为 pass 插件:

```c
extern "C" LLVM_ATTRIBUTE_WEAK
llvm::PassPluginLibraryInfo llvmGetPassPluginInfo();
```

它返回一个结构体 `PassPluginLibraryInfo`，里面装着四样东西:

1. **API 版本号**（`LLVM_PLUGIN_API_VERSION`）——主程序用它判断插件是否兼容。
2. **插件名字**（这里是字符串 `"Polly"`）。
3. **编译期 LLVM 版本字符串**（`LLVM_VERSION_STRING`）——这就是为什么 UsingPollyWithClang 文档反复警告「clang/LLVM/Polly 必须用同一份源码编译」，版本对不上插件就会被拒绝。
4. **一个注册回调函数指针**——签名是 `void(llvm::PassBuilder&)`。主程序加载插件后，正是调用这个函数指针，把 `PassBuilder` 交给插件。

`extern "C"` 保证符号名不被 C++ 「名字修饰（name mangling）」改写，这样主程序能用纯 C 的方式按名字找到它；`LLVM_ATTRIBUTE_WEAK`（弱符号）使得该符号在静态链接场景下即使没被引用也不报错。

#### 4.1.2 核心流程

Polly 的「被发现 → 被加载 → 被调用」过程可以这样描述:

```
┌─────────────────────────────────────────────────────────────┐
│  clang / opt  启动                                          │
│        │                                                    │
│        ▼                                                    │
│  ① 加载 LLVMPolly 共享库                                    │
│     · 树内构建(in-tree): Polly 已链入, 自动可用             │
│     · 独立构建: 用 -fpass-plugin= / -load-pass-plugin 加载  │
│        │                                                    │
│        ▼                                                    │
│  ② 在库里查找符号 llvmGetPassPluginInfo                     │
│        │  找到 → 认定是 pass 插件                           │
│        ▼                                                    │
│  ③ 调用 llvmGetPassPluginInfo()                            │
│        │  返回 PassPluginLibraryInfo{版本, "Polly",          │
│        │                          LLVM_VERSION_STRING,       │
│        │                          registerPollyPasses}       │
│        ▼                                                    │
│  ④ 校验 API 版本                                            │
│        ▼                                                    │
│  ⑤ 调用结构体里的回调: registerPollyPasses(PassBuilder&)    │
│        │  (进入 4.2 节)                                     │
│        ▼                                                    │
│  Polly 的所有回调被注册进 PassBuilder                        │
└─────────────────────────────────────────────────────────────┘
```

关键点:**Polly 插件本身不直接跑任何优化**。它只负责「把回调注册好」。真正跑优化，是等到 `-O3` 流水线执行到某个扩展点（EP）、或解析到 `-passes='polly'` 时，由 LLVM 主动回调 Polly 注册的函数。这是典型的「控制反转（Inversion of Control）」:LLVM 掌握调度，Polly 只是预先「报名」。

#### 4.1.3 源码精读

**① 插件入口符号** —— [lib/Plugin/Polly.cpp:17-20](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Plugin/Polly.cpp#L17-L20)

```cpp
extern "C" LLVM_ATTRIBUTE_WEAK ::llvm::PassPluginLibraryInfo
llvmGetPassPluginInfo() {
  return getPollyPluginInfo();
}
```

整个文件就这一个有意义的函数。它把活儿全转交给 `getPollyPluginInfo()`。注意它甚至没有自己构造结构体——真正的结构体内容在 `RegisterPasses.cpp` 里。

**② 入口符号的声明** —— [include/polly/RegisterPasses.h:25-28](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/RegisterPasses.h#L25-L28)

```cpp
namespace polly {
void registerPollyPasses(llvm::PassBuilder &PB);
} // namespace polly

llvm::PassPluginLibraryInfo getPollyPluginInfo();
```

这里声明了两件事：注册回调 `registerPollyPasses`（4.2 节主角）和 `getPollyPluginInfo`（结构体工厂）。

**③ 结构体的真正内容** —— [lib/Support/RegisterPasses.cpp:693-696](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L693-L696)

```cpp
llvm::PassPluginLibraryInfo getPollyPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "Polly", LLVM_VERSION_STRING,
          polly::registerPollyPasses};
}
```

这就是 `PassPluginLibraryInfo` 四个字段的实参：API 版本、名字 `"Polly"`、LLVM 版本字符串，以及最关键的**第 4 个字段——回调函数指针 `polly::registerPollyPasses`**。主程序最终调用的就是这个函数。一句话总结 4.1:**入口符号 → 结构体 → 回调函数指针**，三层间接，把「外部插件」与「内部 PassBuilder」解耦。

#### 4.1.4 代码实践

**实践目标**：亲手验证「插件入口符号」的存在与签名，理解插件协议的物理形态。

**操作步骤**:

1. 找到你构建产物里的 `LLVMPolly` 共享库（通常在 `<build>/lib/LLVMPolly.so`，具体路径**待本地确认**，取决于你的 CMake 构建目录）。
2. 用 `nm` / `objdump` 查看它导出的符号，过滤 `PassPluginInfo`:

   ```bash
   nm -D --defined-only <build>/lib/LLVMPolly.so | grep llvmGetPassPluginInfo
   # 或
   objdump -T <build>/lib/LLVMPolly.so | grep llvmGetPassPluginInfo
   ```

**需要观察的现象**:

- 输出里应能看到一个名为 `llvmGetPassPluginInfo` 的符号，且类型标记为 `T`（text 段，已定义）或 `W`（weak，弱符号，对应 `LLVM_ATTRIBUTE_WEAK`）。
- 注意符号是**未修饰**的（纯 `llvmGetPassPluginInfo`，没有 C++ mangling 后缀），这正是 `extern "C"` 的效果。

**预期结果**：能直观看到「一个 `.so` 文件里就靠这么一个符号被 LLVM 认作插件」。如果 `nm -D` 看不到，可改用 `nm <lib> | grep PassPluginInfo`（去掉 `-D` 看普通符号表）。**待本地验证**：不同构建模式（`-DLLVM_${target}_LINK_INTO_TOOLS=ON` 把 Polly 静态链入 vs. 独立 `.so`）下，符号可见性会不同；静态链入时插件协议仍然走通，但不再以独立 `.so` 形式存在。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `llvmGetPassPluginInfo` 必须用 `extern "C"`？如果去掉会怎样？

> **参考答案**：C++ 会对函数名做「名字修饰」，把签名信息编码进符号名（如 `_Z25llvmGetPassPluginInfov`）。LLVM 主程序是用固定的纯 C 符号名 `llvmGetPassPluginInfo` 去 `dlsym` 查找的，一旦被 mangle 就找不到了，插件不会被识别。`extern "C"` 强制使用 C 链接约定，保持符号名不变。

**练习 2**：`getPollyPluginInfo()` 返回的结构体里，第 4 个字段（回调）的类型是什么？它和 `registerPollyPasses` 的签名有什么关系？

> **参考答案**：第 4 个字段是一个函数指针，类型为 `void(*)(llvm::PassBuilder&)`（即 `RegisterPassPluginCallbacks`）。`registerPollyPasses` 的声明正是 `void registerPollyPasses(llvm::PassBuilder &PB)`，签名完全匹配，所以可以直接作为该字段的实参传入。

**练习 3**：为什么 UsingPollyWithClang 文档警告「clang/LLVM/Polly 必须用同一份源码编译」？这与插件结构体的哪个字段有关？

> **参考答案**：结构体里同时带了 `LLVM_PLUGIN_API_VERSION` 和 `LLVM_VERSION_STRING`，主程序会据此校验插件是否兼容。如果 clang（主程序）与 Polly 用不同版本编译，ABI/版本号对不上，插件会被拒绝加载，行为不可预期。

---

### 4.2 LLVM New Pass Manager：registerPollyPasses 如何挂入流水线

#### 4.2.1 概念说明

上一节里主程序最终调用了 `registerPollyPasses(PassBuilder &PB)`。本节就讲它**拿到 `PassBuilder` 之后做了什么**。它会注册三类回调:

1. **pass 名 → pass 类的映射**（`addClassToPassName`）：让 LLVM 知道 `polly`、`polly-custom`、`polly-inline` 这些名字对应哪些 C++ 类，便于时间统计、pass 名打印等。
2. **流水线解析回调**（`registerPipelineParsingCallback`，三个：Function / CGSCC / Module）：让用户能在 `-passes='...'` 文本里写 `polly` 并被正确解析成真正的 pass 对象。
3. **扩展点（EP）回调**（根据 `-polly-position` 二选一）：让 Polly **自动**出现在默认 `-O3` 流水线的固定位置，而不需要用户手写 `-passes='polly'`。

第 3 类是重中之重，因为它决定了「普通用户只要写 `clang -O3 -mllvm -polly file.c`，Polly 就会自己跑起来」——这正是 u1-l3 里那条最简命令背后的机制。

#### 4.2.2 核心流程

Polly 在 New PM 里只有**两个可选挂载点**，由命令行选项 `-polly-position` 决定:

```
-polly-position=early              -polly-position=before-vectorizer  (默认!)
        │                                  │
        ▼                                  ▼
registerPipelineStartEPCallback    registerVectorizerStartEPCallback
 (模块级, 流水线最起点)              (函数级, 就在向量化器之前)
        │                                  │
        ▼                                  ▼
buildEarlyPollyPipeline(MPM,...)   buildLatePollyPipeline(FPM,...)
        │                                  │
        └──────────────┬───────────────────┘
                       ▼
            buildCommonPollyPipeline(FPM,...)
                       │
                       ▼
              addPass(PollyFunctionPass(Opts))   ← Polly 真身
                       +
            buildFunctionSimplificationPipeline  ← 跑完后的清理
```

两个位置都最终汇入 `buildCommonPollyPipeline`，差别只在「挂在哪一级 PM、前面跑了多少 LLVM 自带 pass」:

| 维度 | early（PipelineStartEP） | before-vectorizer（VectorizerStartEP） |
|------|--------------------------|----------------------------------------|
| EP 名 | `registerPipelineStartEPCallback` | `registerVectorizerStartEPCallback` |
| PM 级别 | `ModulePassManager` | `FunctionPassManager` |
| 流水线位置 | `-O3` 流水线**最起点** | `-O3` 流水线**向量化器之前** |
| 前置规范化 | 需自己跑更多 canonicalization（`buildCanonicalicationPassesForNPM`） | LLVM 已完成大部分内联/规范化 |
| 是否支持 `-polly-dump-before-file` | ✅ 支持 | ❌ 报致命错误 |
| 默认？ | ❌ 否 | ✅ **是（代码默认）** |

> ⚠️ **真实源码的「注释 vs 代码」冲突**（重要的学习点）:
> `RegisterPasses.cpp` 第 607–614 行的大段注释写着 *“The default is currently a), to register Polly such that it runs as early as possible”*，声称默认是 early。但**实际代码**第 89 行是 `cl::init(POSITION_BEFORE_VECTORIZER)`——**真正默认是 before-vectorizer**。这段注释已过时（Polly 默认行为已迁移到 before-vectorizer）。教训:**永远以代码为准，注释只能当线索**。这与 u1-l3 的结论一致。

#### 4.2.3 源码精读

**① `registerPollyPasses` 主体** —— [lib/Support/RegisterPasses.cpp:633-690](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L633-L690)

这个函数先做 pass 名注册（`addClassToPassName`，由 `PollyPasses.def` 展开）、再注册三类流水线解析回调，**最后**根据 `PassPosition` 选择 EP。我们把最关键的 EP 选择单独拎出来:

**② 两个 EP 的二选一** —— [lib/Support/RegisterPasses.cpp:676-689](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L676-L689)

```cpp
switch (PassPosition) {
case POSITION_EARLY:
  PB.registerPipelineStartEPCallback(
      [FS](ModulePassManager &MPM, OptimizationLevel Level) {
        buildEarlyPollyPipeline(MPM, Level, FS);
      });
  break;
case POSITION_BEFORE_VECTORIZER:
  PB.registerVectorizerStartEPCallback(
      [FS](FunctionPassManager &FPM, OptimizationLevel Level) {
        buildLatePollyPipeline(FPM, Level, FS);
      });
  break;
}
```

注意两个回调的参数类型不同：early 拿到的是 `ModulePassManager`，before-vectorizer 拿到的是 `FunctionPassManager`——这正对应「模块级起点」与「函数级向量化前」两个不同的流水线层。

**③ `-polly-position` 选项与真实默认** —— [lib/Support/RegisterPasses.cpp:80-89](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L80-L89)

```cpp
enum PassPositionChoice { POSITION_EARLY, POSITION_BEFORE_VECTORIZER };
...
static cl::opt<PassPositionChoice> PassPosition(
    "polly-position", cl::desc("Where to run polly in the pass pipeline"),
    cl::values(clEnumValN(POSITION_EARLY, "early", "Before everything"),
               clEnumValN(POSITION_BEFORE_VECTORIZER, "before-vectorizer",
                          "Right before the vectorizer")),
    cl::Hidden, cl::init(POSITION_BEFORE_VECTORIZER), cl::cat(PollyCategory));
```

`cl::init(POSITION_BEFORE_VECTORIZER)` 就是「代码默认 before-vectorizer」的铁证（`cl::Hidden` 表示该选项不在 `-help` 默认输出里，但仍可用）。

**④ early 位置流水线** —— [lib/Support/RegisterPasses.cpp:482-510](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L482-L510)

```cpp
static void buildEarlyPollyPipeline(llvm::ModulePassManager &MPM, ...) {
  ...
  FunctionPassManager FPM = buildCanonicalicationPassesForNPM(MPM, Level); // ① 先规范化
  ...
  buildCommonPollyPipeline(FPM, Level, std::move(FS), EnableForOpt);       // ② Polly
  MPM.addPass(createModuleToFunctionPassAdaptor(std::move(FPM)));
  ...
}
```

early 位置的特点是「前面啥都没有」，所以必须自己先跑一整套规范化（`buildCanonicalicationPassesForNPM`，详见 u2-l3），否则 IR 太「原始」Polly 根本看不懂。

**⑤ before-vectorizer（late）位置流水线** —— [lib/Support/RegisterPasses.cpp:512-537](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L512-L537)

```cpp
static void buildLatePollyPipeline(FunctionPassManager &PM, ...) {
  ...
  if (!DumpBeforeFile.empty())
    llvm::report_fatal_error(
        "Option -polly-dump-before-file at -polly-position=late "
        "not supported with NPM", false);   // ← 呼应 u1-l3 的报错结论
  buildCommonPollyPipeline(PM, Level, std::move(FS), EnableForOpt);
  ...
}
```

注意这里的致命错误信息正是 u1-l3 讲过的「`-polly-dump-before-file` 必须配 early」的来源。late 位置前面 LLVM 已做完内联和大部分规范化，所以不需要再补 canonicalization。

**⑥ 两个位置的共同终点** —— [lib/Support/RegisterPasses.cpp:460-480](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L460-L480)

```cpp
static void buildCommonPollyPipeline(FunctionPassManager &PM, ...) {
  ...
  PollyPassOptions &&Opts =
      Err(parsePollyOptions(StringRef(), /*IsCustom=*/false));
  PM.addPass(PollyFunctionPass(Opts));                                    // Polly 真身
  PM.addPass(PB.buildFunctionSimplificationPipeline(...));                // 清理
  ...
}
```

无论 early 还是 late，最终都汇入这里：**加一个 `PollyFunctionPass`，再跟一段函数简化流水线做收尾清理**。`PollyFunctionPass` 就是 u1-l3 反复提到的「所有路径最终落到的同一个 pass」，它内部才真正跑那条 17 阶段流水线（U2 主题）。

**⑦ `-passes=` 名字的登记** —— [lib/Support/PollyPasses.def:4-5,17-18](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/PollyPasses.def#L4-L5)

```cpp
MODULE_PASS("polly", createModuleToFunctionPassAdaptor(PollyFunctionPass(Opts)), parsePollyDefaultOptions)
MODULE_PASS("polly-custom", createModuleToFunctionPassAdaptor(PollyFunctionPass(Opts)), parsePollyCustomOptions)
...
FUNCTION_PASS("polly", PollyFunctionPass(Opts), parsePollyDefaultOptions)
FUNCTION_PASS("polly-custom", PollyFunctionPass(Opts), parsePollyCustomOptions)
```

这是 **X-Macro 技巧**：同一个 `.def` 文件在不同地方被 `#include`，外层先 `#define MODULE_PASS(...)` 决定怎么用每一条记录，再 include。这里登记了 `polly` 与 `polly-custom` 两个名字，各自配一个选项解析器:

- `-passes='polly'` → `parsePollyDefaultOptions` → 启用全部默认优化（`enableDefaultOpts` + `enableEnd2End`）。
- `-passes='polly-custom<...>'` → `parsePollyCustomOptions` → 只跑用户在 `<...>` 里显式点的阶段。

这正好呼应 u1-l3 提到的「`PollyPasses.def` 登记了 `-passes=` 识别的 `polly`/`polly-custom` 名」。

**⑧ Polly pass 真身** —— [include/polly/Pass/PollyFunctionPass.h:19-30](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Pass/PollyFunctionPass.h#L19-L30)

```cpp
class PollyFunctionPass : public llvm::OptionalPassInfoMixin<PollyFunctionPass> {
public:
  PollyFunctionPass() {}
  PollyFunctionPass(PollyPassOptions Opts) : Opts(std::move(Opts)) {}
  llvm::PreservedAnalyses run(llvm::Function &F, llvm::FunctionAnalysisManager &);
private:
  PollyPassOptions Opts;
};
```

`PollyFunctionPass` 继承 `OptionalPassInfoMixin`（让它可被 New PM 当 pass 用），持有一份 `PollyPassOptions`（哪些阶段开/关），真正的活儿在 `run(Function&, FunctionAnalysisManager&)` 里——这便是 U2 的 `PhaseManager::run()`。本讲到此为止，不再下钻。

#### 4.2.4 代码实践

**实践目标**：完成规格里要求的「追踪从 `llvmGetPassPluginInfo` 到 `registerPollyPasses` 的调用链，说明两个 EP 各注册了哪个回调，并写出最小命令行」。

**操作步骤**（纯源码阅读型，无需构建）:

1. 打开 [lib/Plugin/Polly.cpp:17-20](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Plugin/Polly.cpp#L17-L20)，确认入口符号调用 `getPollyPluginInfo()`。
2. 跳到 [lib/Support/RegisterPasses.cpp:693-696](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L693-L696)，确认结构体第 4 字段是 `polly::registerPollyPasses`。
3. 跳到 [lib/Support/RegisterPasses.cpp:633-690](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L633-L690)，定位末尾的 `switch (PassPosition)`，填出下表:

   | `-polly-position` 值 | 注册的 EP API | 回调函数 | PM 级别 |
   |---|---|---|---|
   | `early` | `PB.registerPipelineStartEPCallback` | `buildEarlyPollyPipeline` | ModulePassManager |
   | `before-vectorizer` | `PB.registerVectorizerStartEPCallback` | `buildLatePollyPipeline` | FunctionPassManager |

4. 写出触发 Polly 优化的最小命令行（两条路径，对照 UsingPollyWithClang 文档验证）:

   ```bash
   # 路径 A：clang 驱动（-mllvm 仅是把后续参数转发给 LLVM 后端）
   clang -O3 -mllvm -polly file.c

   # 路径 B：opt 驱动（直接走 New PM 流水线文本）
   opt -passes='polly' file.ll -S
   ```

   > 加载插件本身：树内构建时 Polly 已随工具链可用（文档原话“if Polly was checked out into tools/polly before compilation. No further configuration is needed”）；独立构建时需用标准 LLVM 插件加载方式 `clang -fpass-plugin=<path>/LLVMPolly.so ...` / `opt -load-pass-plugin <path>/LLVMPolly.so ...`，具体路径**待本地确认**。

**需要观察的现象**:在步骤 3 的表里，两个分支的 EP API 名字、回调函数名、PM 类型都应与源码逐字对应；步骤 4 命令行应能反映「`-mllvm -polly` 只是打开 `PollyEnabled` 开关，真正挂载由 EP 回调完成」。

**预期结果**:你能用自己的话讲清——**「写 `-mllvm -polly` 并不会直接调用 Polly，它只是让 `shouldEnablePollyForOptimization()` 返回 true；Polly 之所以会跑，是因为 `registerPollyPasses` 早已把 EP 回调注册好，`-O3` 流水线走到 before-vectorizer 这个点时，LLVM 主动回调了 `buildLatePollyPipeline`。」**

#### 4.2.5 小练习与答案

**练习 1**：`-mllvm -polly` 这个开关本身并没有「调用」Polly 的任何 pass。那 Polly 为什么会在 `-O3` 下自动跑起来？

> **参考答案**：`-polly` 只是让 `PollyEnabled` 为 true，进而使 `shouldEnablePollyForOptimization()` 返回 true。真正驱动 Polly 运行的是 `registerPollyPasses` 在插件加载时注册的 EP 回调（默认 `registerVectorizerStartEPCallback`）。当 `-O3` 流水线执行到「向量化器之前」这个扩展点时，LLVM 回调 `buildLatePollyPipeline`，其中检查到 `EnableForOpt` 为 true 才真正把 `PollyFunctionPass` 加进流水线。这是「注册时报名、运行时回调」的控制反转。

**练习 2**：为什么 `early` 位置需要 `buildCanonicalicationPassesForNPM`，而 `before-vectorizer` 位置不需要？

> **参考答案**：early 位置挂在 `-O3` 流水线最起点，前面没有跑过任何 LLVM 自带的规范化 pass，IR 还很「原始」（可能还有 mem2reg 没做、循环未规范化等），Polly 无法理解。所以必须自己先补一整套 canonicalization（见 u2-l3）。而 before-vectorizer 位置前面 LLVM 已完成内联、循环规范化、SCEV 友好化等大部分工作，IR 已是 Polly 喜欢的形态，故无需再补。

**练习 3**：`-passes='polly'` 和 `-passes='polly-custom<no-delicm;stopafter=ast>'` 走的是同一个选项解析器吗？区别在哪？

> **参考答案**：不是。前者用 `parsePollyDefaultOptions`（`IsCustom=false`），后者用 `parsePollyCustomOptions`（`IsCustom=true`）。两者都进入 `parsePollyOptions`，但 `IsCustom` 控制是否启用默认优化:默认模式下 `EnableDefaultOpts` 与 `EnableEnd2End` 都为 true（跑全套），custom 模式下都为 false（只跑用户在 `<...>` 里显式点的阶段）。详细解析逻辑（位集、阶段依赖、`checkConsistency`）属于 u2-l2 主题。

---

## 5. 综合实践

**任务：画出 Polly「从被加载到跑起来」的完整时序，并标注每一步对应的源码位置。**

要求:

1. 在一张图（或编号列表）里画出以下角色与消息顺序:`clang/opt 启动` → `加载 LLVMPolly` → `查找 llvmGetPassPluginInfo` → `getPollyPluginInfo` → `registerPollyPasses(PB)` → `注册 3 类回调` → `用户写 -O3 -mllvm -polly` → `流水线走到 before-vectorizer EP` → `buildLatePollyPipeline` → `buildCommonPollyPipeline` → `PollyFunctionPass::run`。
2. 在每个箭头旁标注它对应的**文件:行号**（用本讲给出的永久链接）。
3. 在图上特别标出**两处「陷阱」**:
   - 注释 vs 代码默认值冲突（第 607–614 行注释说默认 early，实际第 89 行是 before-vectorizer）。
   - `-polly-dump-before-file` 在 late 位置会致命报错（第 523–526 行）。
4. 最后用一句话回答:如果用户既不写 `-passes='polly'`、也不写 `-mllvm -polly`，Polly 还会跑吗？为什么？

**参考要点（不直接给完整答案，鼓励自己查源码）**:
- 若没有 `-polly`，则 `shouldEnablePollyForOptimization()` 与 `shouldEnablePollyForDiagnostic()` 都可能为 false，此时 `buildEarlyPollyPipeline` / `buildLatePollyPipeline` 在开头就会 `return`（见 [RegisterPasses.cpp:487-488](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L487-L488) 与 [517-518](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L517-L518)）——**回调被调用了，但什么也不做就返回**。这正是「默认行为不变」的设计:插件加载不等于插件生效。

## 6. 本讲小结

- Polly 的插件入口是 [lib/Plugin/Polly.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Plugin/Polly.cpp) 里唯一的 `extern "C"` 符号 `llvmGetPassPluginInfo`，它返回一个 `PassPluginLibraryInfo` 结构体，其第 4 字段是回调 `registerPollyPasses`。
- `registerPollyPasses(PassBuilder&)` 注册三类回调:**pass 名映射**、**流水线解析**（让 `-passes='polly'` 可用）、**扩展点 EP**（让 Polly 自动进 `-O3`）。
- Polly 只有两个挂载点:`early`（`registerPipelineStartEPCallback`，模块级）与 `before-vectorizer`（`registerVectorizerStartEPCallback`，函数级），由 `-polly-position` 控制。
- **代码默认是 `before-vectorizer`**（`cl::init(POSITION_BEFORE_VECTORIZER)`），尽管第 607–614 行的注释已过时地声称默认是 early——这是「信代码不信注释」的活教材。
- 两个位置最终都汇入 `buildCommonPollyPipeline`，加一个 `PollyFunctionPass` + 一段函数简化清理；`PollyFunctionPass` 才是 Polly 的 pass 真身，其 `run()` 即 U2 的 `PhaseManager::run()`。
- `PollyPasses.def` 用 X-Macro 登记了 `polly` / `polly-custom` / `polly-inline` 三个名字，分别配默认/自定义/无选项解析器。

## 7. 下一步学习建议

本讲把「Polly 是怎么被挂进 LLVM 的」讲完了，但**还没讲它挂进去之后到底跑了什么**。自然的下一步:

1. **【强烈推荐下一步】[u2-l1 PhaseManager 阶段流水线全景](u2-l1-phase-manager-pipeline.md)**：打开 `PollyFunctionPass::run()` 背后的 `PhaseManager::run()`，看那 17 个阶段（prepare→detect→…→codegen）是怎么按顺序跑的。这是整本手册的「枢纽讲义」。
2. **[u2-l2 PollyFunctionPass 与阶段选项解析](u2-l2-function-pass-and-options.md)**：深入 `PollyPassOptions` 的位集模型与 `parsePollyOptions` 如何解析 `-passes='polly-custom<...>'`，承接本讲 4.2.5 练习 3 留下的问题。
3. **[u2-l3 规范化与代码准备阶段](u2-l3-canonicalization-and-preparation.md)**：搞懂本讲反复出现的 `buildCanonicalicationPassesForNPM` 到底注册了哪些规范化 pass、为什么 Polly 必须吃规范化后的 IR。

如果你更想先看看「Polly 跑出来的东西长什么样」，也可以先跳到 [u3 SCoP 检测](u3-l1-scop-detection-design.md) 系列再回头读 U2——两条路径都成立。
