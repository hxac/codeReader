# Pass 插件机制（Plugins）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「Pass 插件」要解决的问题：在不重新编译 LLVM 本体的前提下，把自己的 pass 注入 `opt`/`clang` 的优化流水线。
- 描述插件与工具之间的二进制契约：`llvmGetPassPluginInfo` 入口、`PassPluginLibraryInfo` 结构体、以及 API 版本校验。
- 追踪 `PassPlugin::Load` 的动态加载流程（`dlopen` → 查符号 → 调入口 → 校验版本），并定位 `opt` 中 `-load-pass-plugin` 的接线点。
- 区分 `PassBuilder` 提供的两类注册回调：扩展点回调（Extension Point）与流水线解析回调（Pipeline Parsing），并知道何时用哪一种。
- 把仓库自带的 `Bye` 示例构建为插件，用 `opt -load-pass-plugin=...` 加载并运行，观察到它确实被执行。

---

## 2. 前置知识

本讲是 [u4-l4 编写你自己的 LLVM Pass（新 PM）](u4-l4-write-your-own-pass.md) 的直接延续。请先确认你已掌握：

- **新 PM pass 骨架**：通过 CRTP 混入 `OptionalPassInfoMixin<PassT>`、实现 `run(IRUnitT&, AnalysisManager&)`、返回 `PreservedAnalyses`。这是插件里「被注入的 pass」本身的样子。
- **PassBuilder 是装配车间**：它注册内置分析、按优化等级构造默认流水线、用扩展点（EP）回调让外部代码在流水线的固定位置插 pass、用 `registerPipelineParsingCallback` 扩展 `-passes` 词表（见 [u4-l1 新 Pass 管理器架构](u4-l1-new-pass-manager.md)）。
- **两种「让 pass 被认到」的路径**：路径 A 在 `PassRegistry.def` 加宏（需重编 LLVM）；路径 B 做插件用回调认领名字（不重编）。本讲就是把路径 B 彻底讲透。

如果 u4-l4 中你已经在 `PassRegistry.def` 里加过一行 `FUNCTION_PASS("helloworld", HelloWorldPass())`，那么你当时**必须重新编译整个 LLVM** 才能让 `opt -passes=helloworld` 生效。本讲要解决的正是这个痛点。

> 关键直觉：`PassRegistry.def` 是「编译期」注册（改一行代码就要重编）；插件是「运行期」注册（编一个 `.so`，工具运行时再加载）。两者最终都走到同一个 `PassBuilder` 回调机制上，区别只在「注册代码何时被执行」。

此外你需要一点 C/C++ 动态库的常识：操作系统提供 `dlopen`（Windows 上是 `LoadLibrary`）把一个 `.so`/`.dll` 映射进进程地址空间，再用 `dlsym`（Windows 上是 `GetProcAddress`）按名字查找其中的符号。本讲中工具加载插件，底层就是这套机制。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `llvm/include/llvm/Plugins/PassPlugin.h` | 定义插件契约：`PassPluginLibraryInfo` 结构体、`PassPlugin` 加载类、`llvmGetPassPluginInfo` 入口声明、`LLVM_PLUGIN_API_VERSION` 宏。 |
| `llvm/lib/Plugins/PassPlugin.cpp` | `PassPlugin::Load` 的实现：动态打开库、查入口符号、调入口、校验版本。 |
| `llvm/tools/opt/opt.cpp` | `opt` 的极薄外壳：`main` 只是把命令行参数转发给 `optMain`。 |
| `llvm/tools/opt/optdriver.cpp` | `optMain` 实现，定义 `-load-pass-plugin` 选项并在解析时逐个加载插件。 |
| `llvm/tools/opt/NewPMDriver.cpp` | 装配 `PassBuilder` 的核心：把已加载插件、显式回调、静态扩展三类来源的回调都注册到同一个 `PassBuilder` 上。 |
| `llvm/include/llvm/Passes/PassBuilder.h` | 声明两套注册回调：`registerVectorizerStartEPCallback`（扩展点）与 `registerPipelineParsingCallback`（流水线解析）。 |
| `llvm/examples/Bye/Bye.cpp` | 官方完整范本：同时给出新 PM pass、legacy pass、两种回调、pre-codegen 钩子与 C 入口。 |
| `llvm/examples/Bye/CMakeLists.txt` | 用 `add_llvm_pass_plugin` 把 Bye 构建成插件的 CMake 脚本。 |
| `llvm/cmake/modules/AddLLVM.cmake` | `add_llvm_pass_plugin` 宏与 `process_llvm_pass_plugins`，决定动态 `.so` 还是静态链接。 |
| `llvm/test/Feature/load_extension.ll` | 端到端回归测试：`-load-pass-plugin=... -passes=goodbye -wave-goodbye`，断言输出 `Bye`。 |
| `llvm/docs/WritingAnLLVMNewPMPass.md` | 官方文档「Registering passes as plugins」一节，列出两条入口与最小 CMakeLists。 |

---

## 4. 核心概念与源码讲解

### 4.1 为什么需要 Pass 插件：动机与两种形态

#### 4.1.1 概念说明

假设你写好了一个新的优化 pass `MyFancyPass`。最朴素的让它生效的办法，是把它的注册行写进 `llvm/lib/Passes/PassRegistry.def`，然后**重新编译整个 LLVM**。问题在于 LLVM 是个庞然大物，完整构建动辄几十分钟到数小时；而你只是想验证一下自己的 pass 行为。这显然不划算。

Pass 插件（Pass Plugin）就是为了解开这个死结。它把你的 pass 编译成一个**独立的动态库**（Linux 上是 `.so`、macOS 上是 `.dylib`、Windows 上是 `.dll`），工具（`opt`/`clang`/`llc`/`llvm-lto2` 等）在运行时通过命令行参数把它加载进自己的进程地址空间，于是你的 pass 就出现在了流水线里——**整个 LLVM 不需要重新编译一行**。

插件有两种形态，由一个 CMake 开关控制：

- **动态插件（dynamic plugin）**：默认形态。产物是一个 `.so` 文件，工具用 `-load-pass-plugin=<路径>` 在运行期加载。多个工具、多个版本可以共用同一个 `.so`。
- **静态扩展（statically linked extension）**：把插件直接链接进工具本体。此时不再是「运行期加载」，而是「编译期焊死」。这种形态主要用于树内测试（in-tree testing），保证某些测试不需要依赖动态加载能力也能跑。

这两者的 **C++ 源码几乎完全一样**——同一份 `Bye.cpp` 既能编成 `.so`，也能被静态链接。差别只由一个宏 `LLVM_<NAME>_LINK_INTO_TOOLS` 控制。

#### 4.1.2 核心流程

无论是哪种形态，插件要被工具「认到」，都要经过下面这条链：

```
工具启动
   │
   ├─ [动态] opt 解析到 -load-pass-plugin=libBye.so
   │         → PassPlugin::Load("libBye.so")
   │           → dlopen 打开库
   │           → dlsym 找到符号 llvmGetPassPluginInfo
   │           → 调用它，拿到 PassPluginLibraryInfo 结构体
   │           → 校验 API 版本号
   │
   ├─ [静态] 工具编译时，Extension.def 已把插件焊进二进制
   │         → get<插件>PluginInfo() 直接可调
   │
   ▼
拿到 RegisterPassBuilderCallbacks 函数指针
   │
   ▼
调用该指针，传入工具自己的 PassBuilder
   │
   ▼
插件内部调用 PB.registerXxxCallback(...)
   → 在扩展点插 pass  /  认领一个 -passes 名字
   │
   ▼
工具正常构造流水线时，插件的 pass 自动出现
```

关键在于：工具和插件之间**只约定了一个 C 函数和一个结构体**，没有任何 C++ ABI 耦合。这就是为什么插件能做到「不重编工具也能用」。

#### 4.1.3 源码精读

先看 `opt` 的入口有多薄——这正好印证 u1-l2 讲过的「工具是薄壳，逻辑下沉到 lib」：

[llvm/tools/opt/opt.cpp:23-27](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/tools/opt/opt.cpp#L23-L27) —— `opt` 的 `main` 仅把 `argc/argv` 转发给 `optMain`，自己什么都不做。真正的命令行解析和插件加载都在 `optdriver.cpp` 里。

再看官方文档对两种形态与两条入口的概括：

[llvm/docs/WritingAnLLVMNewPMPass.md:246-256](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/docs/WritingAnLLVMNewPMPass.md#L246-L256) —— 文档明确：插件至少要提供两条入口之一——`get##Name##PluginInfo()`（静态注册用）和 `extern "C" llvmGetPassPluginInfo()`（动态加载用）；并指出设 `LLVM_${NAME}_LINK_INTO_TOOLS=ON` 可把它变成静态扩展。

#### 4.1.4 代码实践

**实践目标**：理解「重编 LLVM」与「插件」的代价差异，并在仓库里找到插件范本。

**操作步骤**：
1. 打开 [llvm/examples/Bye/Bye.cpp](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/examples/Bye/Bye.cpp)，确认它是一个独立源文件，不依赖 `PassRegistry.def`。
2. 打开 `llvm/lib/Passes/PassRegistry.def`，搜索 `goodbye`，确认它**没有**出现在那里（说明 Bye 不走重编路径）。
3. 思考：如果用 `PassRegistry.def` 方式，你要改 `PassBuilder.cpp` 和 `PassRegistry.def` 两个文件并重编；用插件方式，你只编一个 `.so`。

**需要观察的现象**：Bye 的 pass 名字 `goodbye` 在仓库的 `PassRegistry.def` 里查不到，却仍能被 `opt -passes=goodbye` 使用——这正是插件机制的价值。

**预期结果**：确认 Bye 完全通过插件机制注册，与 LLVM 本体解耦。本步骤无需运行任何命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么静态扩展也要保留同一份 `Bye.cpp` 源码，而不是另写一个版本？

<details>
<summary>参考答案</summary>

因为静态扩展和动态插件只是「链接时机」不同——一个是运行期 `dlopen`，一个是编译期焊进二进制。两者的注册逻辑（`registerPassBuilderCallbacks`）完全一致，复用同一份源码可避免维护两套。源码里用 `#ifndef LLVM_BYE_LINK_INTO_TOOLS` 守卫 C 入口：动态时导出 `llvmGetPassPluginInfo`，静态时由 `Extension.def` 的 `getByePluginInfo()` 直接调用。
</details>

**练习 2**：在不重新编译 LLVM 的前提下，要让 `clang` 也用上你的 pass，需要满足什么前提？

<details>
<summary>参考答案</summary>

需要 `clang` 在构建时开启了动态插件支持（`LLVM_ENABLE_PLUGINS=ON`，默认在能支持的平台上是开的），并且 `clang` 在内部装配 `PassBuilder` 时也调用了和 `opt` 一样的「加载插件 → 注册回调」流程。`clang` 通过 `-fpass-plugin=libBye.so` 加载插件，机制与 `opt -load-pass-plugin` 同源。
</details>

---

### 4.2 插件契约与 PassPlugin 动态加载（最小模块一）

#### 4.2.1 概念说明

工具和插件之间是纯粹的 **C ABI 契约**，不涉及任何 C++ 名字修饰（name mangling）。这个契约由三样东西定义：

1. **一个入口函数**：`extern "C" llvm::PassPluginLibraryInfo llvmGetPassPluginInfo()`。它是插件对外暴露的唯一符号，工具靠它拿到插件的信息。
2. **一个信息结构体**：`PassPluginLibraryInfo`，装着插件的名字、版本、API 版本号，以及最重要的——一个「注册回调」函数指针。
3. **一个版本号常量**：`LLVM_PLUGIN_API_VERSION`。工具和插件各自持有一个版本号，加载时必须相等，否则拒绝。这是为了在契约结构体发生 ABI 破坏性变更（增删/重排字段）时强制双方重新编译。

> 为什么是 C 链接（`extern "C"`）？因为 C++ 的符号名会被编译器修饰成 `_ZN...` 之类乱码，不同编译器、不同版本修饰规则不同，插件就无法跨工具复用。C 链接保证符号名就是 `llvmGetPassPluginInfo`，任何人都能找到。

`PassPlugin` 类是工具这一侧用来「持有一个已加载插件」的句柄。它包装了动态库句柄和那次加载拿到的 `PassPluginLibraryInfo`，并提供调用其中回调的便捷方法。注意 `PassPlugin` 只负责**加载和持有**，真正「把 pass 装进流水线」的工作发生在 `PassBuilder` 的回调里（见 4.3）。

#### 4.2.2 核心流程

`PassPlugin::Load(Filename)` 是整个加载流程的核心，可以分成四步：

```
① 打开动态库
   sys::DynamicLibrary::getPermanentLibrary(Filename)
   → 失败则报错 "Could not load library"

② 查找入口符号
   Library.getAddressOfSymbol("llvmGetPassPluginInfo")
   → 找不到则报错 "Plugin entry point not found ... Is this a legacy plugin?"

③ 调用入口，取回信息结构体
   P.Info = llvmGetPassPluginInfo()
   → 得到 APIVersion / PluginName / PluginVersion / 回调指针

④ 校验 API 版本
   if (P.Info.APIVersion != LLVM_PLUGIN_API_VERSION) 报错 "Wrong API version"
```

四步任一失败都返回 `Error`，工具据此决定是否中止。成功则返回一个填充好的 `PassPlugin` 对象。

注意第②步的报错信息很关键：如果一个 `.so` 里没有 `llvmGetPassPluginInfo` 符号，工具会怀疑它是不是「旧式（legacy）插件」——legacy pass manager 时代用的是另一种 `-load` 机制（`RegisterPass` + `opt -load`），与新 PM 的 `-load-pass-plugin` 是两套东西。这条报错是给用户的提示。

#### 4.2.3 源码精读

先看契约结构体本身。它是 `extern "C"` 的 POD，所有回调字段都带默认 `nullptr`，未用到的可以不填：

[llvm/include/llvm/Plugins/PassPlugin.h:45-64](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/Plugins/PassPlugin.h#L45-L64) —— `PassPluginLibraryInfo`：前三个字段是身份信息（`APIVersion`/`PluginName`/`PluginVersion`），后两个是钩子函数指针——`RegisterPassBuilderCallbacks`（向 `PassBuilder` 注册 pass）与 `PreCodeGenCallback`（在后端代码生成前介入，甚至可以自己接管输出）。

版本号常量定义在同一个头里。**当结构体字段增删或重排时，这个数字必须加 1**：

[llvm/include/llvm/Plugins/PassPlugin.h:36](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/Plugins/PassPlugin.h#L36) —— `LLVM_PLUGIN_API_VERSION` 当前为 `2`。

`PassPlugin` 类只持有三样东西（文件名、动态库句柄、信息结构体），并提供调用回调的转发方法：

[llvm/include/llvm/Plugins/PassPlugin.h:93-96](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/Plugins/PassPlugin.h#L93-L96) —— `registerPassBuilderCallbacks` 只是把结构体里的函数指针取出来调用，并在指针为空时安全跳过。

再看 `Load` 的四步实现，完全对应上面的流程：

[llvm/lib/Plugins/PassPlugin.cpp:16-49](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Plugins/PassPlugin.cpp#L16-L49) —— 注意第 39 行用 `reinterpret_cast` 把符号地址转成函数指针并调用，第 41-46 行做版本校验。整个函数是静态工厂方法，返回 `Expected<PassPlugin>`（见 u3 系列讲过的 `Error`/`Expected` 错误处理）。

工具侧的入口在 `opt`。先看命令行选项定义：

[llvm/tools/opt/optdriver.cpp:285-287](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/tools/opt/optdriver.cpp#L285-L287) —— `-load-pass-plugin` 是一个字符串列表（`cl::list`），意味着可以同时加载多个插件。

再看 `optMain` 里实际触发加载的代码。它用一个回调来处理 `-load-pass-plugin` 的每个值：

[llvm/tools/opt/optdriver.cpp:449-455](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/tools/opt/optdriver.cpp#L449-L455) —— 对每个插件路径调用 `PassPlugin::Load`，失败则 `reportFatalUsageError`，成功则 `emplace_back` 进 `PluginList`。注意它使用 `setCallback`——即「解析到这个选项就执行」，因此加载发生在命令行解析期间。

#### 4.2.4 代码实践

**实践目标**：亲手把 Bye 构建为动态插件，并用 `opt` 加载它，验证 `PassPlugin::Load` 的四步流程真的发生了。

**操作步骤**（需要你已按 u1-l3 配置好一个 LLVM 构建目录，且构建时开启了 `LLVM_BUILD_EXAMPLES=ON`、`LLVM_ENABLE_PLUGINS=ON`）：

1. 构建插件（在构建目录下）：
   ```bash
   ninja -C build Bye
   ```
   产物位于 `build/lib/Bye.so`（macOS 上是 `Bye.dylib`）。
2. 准备一段最简单的 IR（存为 `/tmp/a.ll`）：
   ```llvm
   define i32 @foo() {
     %a = add i32 2, 3
     ret i32 %a
   }
   ```
3. 用 `opt` 加载插件并运行 `goodbye` pass，开启 `-wave-goodbye` 让它打印：
   ```bash
   opt -load-pass-plugin=build/lib/Bye.so \
       -passes=goodbye -wave-goodbye \
       -disable-output /tmp/a.ll
   ```

**需要观察的现象**：标准错误应打印 `Bye: foo`（因为 Bye 的 `runBye` 在 `-wave-goodbye` 时会打印 `Bye: <函数名>`）。这证明：插件被 `dlopen`、`llvmGetPassPluginInfo` 被找到并调用、`goodbye` 名字被流水线解析回调认领、`Bye` pass 真的跑在了 `foo` 上。

**预期结果**：输出 `Bye: foo`。仓库自带的回归测试 [llvm/test/Feature/load_extension.ll:1-11](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/test/Feature/load_extension.ll#L1-L11) 正是用 `CHECK: Bye` 来断言这条路径。如果实际未能运行，标注「待本地验证」并检查：插件是否真的编出来了、路径是否正确、平台是否支持动态插件（Windows 上该示例被跳过，见下文 CMakeLists）。

> 如果你的环境无法构建，可改为「源码阅读型实践」：在 [llvm/lib/Plugins/PassPlugin.cpp:16-49](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Plugins/PassPlugin.cpp#L16-L49) 旁注上四步注释（① 打开库 ② 查符号 ③ 调入口 ④ 校验版本），并在每一步对应行号处标出它返回哪种 `StringError`。

#### 4.2.5 小练习与答案

**练习 1**：如果有人用旧版 LLVM（API 版本为 1）编出的插件，加载到新版工具（API 版本为 2）里，会发生什么？在哪一行被拒？

<details>
<summary>参考答案</summary>

会在 [llvm/lib/Plugins/PassPlugin.cpp:41-46](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Plugins/PassPlugin.cpp#L41-L46) 的版本校验处被拒，返回 `StringError("Wrong API version ...")`，提示 `Got version 1, supported version is 2`。这正是 API 版本号存在的意义：契约结构体发生 ABI 破坏性变更时，宁可拒绝加载也不要产生内存错乱。
</details>

**练习 2**：为什么 `PassPluginLibraryInfo` 里 `RegisterPassBuilderCallbacks` 字段默认是 `nullptr`？

<details>
<summary>参考答案</summary>

因为不是所有插件都需要注册 pass。一个插件可能只想挂 `PreCodeGenCallback`（在后端发射前做点事），而不需要往优化流水线加 pass；或者反过来。给字段默认 `nullptr`，调用方（[PassPlugin.h:93-96](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/Plugins/PassPlugin.h#L93-L96) 的 `registerPassBuilderCallbacks` 包装方法）就能在为空时安全跳过。这样新增字段不会破坏老插件。
</details>

---

### 4.3 PassBuilder 回调注册（最小模块二）

#### 4.3.1 概念说明

`PassPlugin::Load` 只解决了「拿到插件信息」。真正让 pass 出现在流水线里的，是插件提供的 `RegisterPassBuilderCallbacks` 函数——它接受工具的 `PassBuilder &PB`，然后在上面挂回调。

`PassBuilder`（见 u4-l1）为插件开放了两类钩子，对应两种「你想让 pass 怎么被触发」的需求：

| 类型 | 回调 | 何时触发 | 典型用法 |
| --- | --- | --- | --- |
| **扩展点回调（EP）** | `registerVectorizerStartEPCallback`、`registerPipelineStartEPCallback` 等十余种 | 当工具构造**默认优化流水线**（如 `-O2`、`-O3`）到某个固定位置时 | 想让你的 pass 在每次 `-O2` 编译时**自动**插入到向量化之前 |
| **流水线解析回调** | `registerPipelineParsingCallback`（按 IR 层级有 5 个重载） | 当 `-passes=...` 文本里出现一个工具**不认识的名字**时 | 想让你的 pass 可以用 `-passes=goodbye` **手动**调用 |

一句话区分：

- 扩展点 = 「默认流水线里给我留个位置」→ 自动、被动。
- 流水线解析 = 「我的 pass 有自己的名字，可以被 `-passes` 显式引用」→ 手动、主动。

`Bye` 示例同时注册了这两类，所以它既能被 `-O2` 自动触发（向量化前），也能被 `-passes=goodbye` 手动调用。这种「双注册」是真实插件的常见写法。

此外还有第三类来源——**静态扩展**。当插件被静态链接进工具时，没有 `dlopen`，回调注册靠一个名为 `Extension.def` 的「X 宏（X-Macro）」文件完成：构建系统把所有静态插件的名字生成成一串 `HANDLE_EXTENSION(名字)` 宏调用，工具源码里 `#include` 这个文件两次，分别展开成「函数声明」和「调用注册」。

#### 4.3.2 核心流程

插件内部的注册函数 `registerPassBuilderCallbacks(PB)` 是一切的中枢。以 Bye 为例：

```
registerPassBuilderCallbacks(PB)
   │
   ├─ PB.registerVectorizerStartEPCallback(...)
   │     → 注册一个 lambda：当默认流水线到「向量化起点」时，
   │       把 Bye() 加进 FunctionPassManager
   │
   └─ PB.registerPipelineParsingCallback(...)
         → 注册一个 lambda：当 -passes 解析到 "goodbye" 时，
           把 Bye() 加进 FunctionPassManager，返回 true 表示「我认领了」
```

而工具侧（`opt`）在装配 `PassBuilder` 时，要确保**三类来源**的回调都被注册到同一个 `PB` 上：

```
NewPMDriver::runPassPipeline 里：
   ① registerEPCallbacks(PB)              ← 命令行 -passes-ep-* 文本形式的 EP
   ② for (已加载的动态插件)                ← -load-pass-plugin 加载进来的
        plugin.registerPassBuilderCallbacks(PB)
   ③ for (显式 PassBuilderCallbacks)      ← 工具内嵌调用方传入的
        callback(PB)
   ④ #include Extension.def              ← 静态扩展（HANDLE_EXTENSION 宏展开）
```

四类回调最后都落到同一个 `PassBuilder` 上，因此无论是动态加载、静态焊接还是命令行文本，最终对流水线的影响是一致的——这正是插件机制「无侵入」的关键。

#### 4.3.3 源码精读

先看 Bye 的注册函数全貌，它同时挂了两类回调：

[llvm/examples/Bye/Bye.cpp:41-55](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/examples/Bye/Bye.cpp#L41-L55) —— 第 42-45 行注册扩展点回调（向量化起点），第 46-54 行注册流水线解析回调：当 `Name == "goodbye"` 时把 `Bye()` 加进函数流水线并返回 `true` 认领这个名字。

对照 `PassBuilder` 这两个回调的声明，确认参数签名：

[llvm/include/llvm/Passes/PassBuilder.h:480-483](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/Passes/PassBuilder.h#L480-L483) —— `registerVectorizerStartEPCallback` 接受一个签名为 `void(FunctionPassManager&, OptimizationLevel)` 的回调，在默认流水线「向量化之前」被调用。

[llvm/include/llvm/Passes/PassBuilder.h:595-599](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/Passes/PassBuilder.h#L595-L599) —— `registerPipelineParsingCallback`（Function 重载）接受 `bool(StringRef Name, FunctionPassManager&, ArrayRef<PipelineElement>)`：返回 `true` 表示「这个名字我处理了」，`false` 表示「不是我，交给下一个回调」。

再看 Bye 的 pass 本体与 C 入口，这是「被注入的东西」长什么样：

[llvm/examples/Bye/Bye.cpp:33-39](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/examples/Bye/Bye.cpp#L33-L39) —— `Bye` 是一个标准的可选新 PM 函数 pass（`OptionalPassInfoMixin<Bye>`），`run` 里调 `runBye`，不改 IR 时返回 `PreservedAnalyses::all()`。这正是 u4-l4 教过的骨架。

[llvm/examples/Bye/Bye.cpp:81-91](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/examples/Bye/Bye.cpp#L81-L91) —— `getByePluginInfo()` 填好结构体（API 版本、名字、版本、回调指针），`llvmGetPassPluginInfo()` 用 `#ifndef LLVM_BYE_LINK_INTO_TOOLS` 守卫：动态插件时导出此 C 入口，静态链接时被 `getByePluginInfo()` 直接调用而绕过它。

现在看工具侧如何把已加载插件接到 `PassBuilder` 上：

[llvm/tools/opt/NewPMDriver.cpp:464-470](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/tools/opt/NewPMDriver.cpp#L464-L470) —— 在 `runPassPipeline` 里，先遍历所有「已加载的动态插件」调它们的 `registerPassBuilderCallbacks(PB)`，再遍历「显式传入的 `PassBuilderCallbacks`」。这两段正是 4.3.2 流程图里的 ② 和 ③。

最后看静态扩展的注册（来源 ④），用的是 X 宏技巧：

[llvm/tools/opt/NewPMDriver.cpp:472-475](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/tools/opt/NewPMDriver.cpp#L472-L475) —— `#define HANDLE_EXTENSION(Ext) get##Ext##PluginInfo().RegisterPassBuilderCallbacks(PB);` 然后展开 `Extension.def`。文件顶部还有对应的声明版本：

[llvm/tools/opt/NewPMDriver.cpp:350-353](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/tools/opt/NewPMDriver.cpp#L350-L353) —— 声明版本的 `HANDLE_EXTENSION(Ext)` 展开成 `PassPluginLibraryInfo get##Ext##PluginInfo();`。`Extension.def` 是构建期由 `process_llvm_pass_plugins` 生成的（见 4.3.4），每行一个 `HANDLE_EXTENSION(插件名)`。

静态与动态的分叉点在 CMake。`add_llvm_pass_plugin` 根据开关走两条路：

[llvm/cmake/modules/AddLLVM.cmake:1323-1344](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/cmake/modules/AddLLVM.cmake#L1323-L1344) —— 当 `LLVM_<NAME>_LINK_INTO_TOOLS` 为真时，建一个对象库（OBJECT）、追加到全局 `LLVM_STATIC_EXTENSIONS` 属性、并定义 `LLVM_<NAME>_LINK_INTO_TOOLS` 宏（让 Bye 的 `#ifndef` 生效，关闭 C 入口导出）；否则就建一个 MODULE 库（即 `.so`）。第 1339 行的 `set_property(GLOBAL APPEND PROPERTY LLVM_STATIC_EXTENSIONS ${name})` 正是静态扩展清单的来源。

Bye 的 CMakeLists 直接调用了这个宏，并解释了为什么不在 Windows 上构建：

[llvm/examples/Bye/CMakeLists.txt:9-15](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/examples/Bye/CMakeLists.txt#L9-L15) —— 注释说明：插件**故意不**链接 Support/Core 库，而是期望加载它的进程里有这些符号；但 Windows 的 DLL 不允许有未解析符号，所以跳过。`BUILDTREE_ONLY` 表示只在构建树里用、不参与安装测试。

#### 4.3.4 代码实践：观察静态扩展的生成

**实践目标**：理解 X 宏 `Extension.def` 如何把静态插件焊进工具，体会「动态加载」与「静态焊接」殊途同归。

**操作步骤**：

1. 在仓库源码里搜 `Extension.def`，你会发现它**不在源码树里**（搜索 `llvm/include/llvm/Support/Extension.def` 会找不到），因为它由构建系统生成。
2. 阅读 [llvm/cmake/modules/AddLLVM.cmake:1396-1401](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/cmake/modules/AddLLVM.cmake#L1396-L1401)：`process_llvm_pass_plugins(GEN_CONFIG)` 把 `LLVM_STATIC_EXTENSIONS` 清单里的每个插件写成一行 `HANDLE_EXTENSION(插件名)`，落盘成 `Extension.def`。
3. 做一个思想实验：若你把 Bye 用 `-DLLVM_BYE_LINK_INTO_TOOLS=ON` 静态链接，那么：
   - `Bye.cpp` 里 `#ifndef LLVM_BYE_LINK_INTO_TOOLS` 为假 → `llvmGetPassPluginInfo` **不**导出。
   - `LLVM_STATIC_EXTENSIONS` 里多了 `Bye` → `Extension.def` 多一行 `HANDLE_EXTENSION(Bye)`。
   - NewPMDriver.cpp 第 472-475 行展开后变成 `getByePluginInfo().RegisterPassBuilderCallbacks(PB);`，于是静态注册生效，无需 `-load-pass-plugin`。

**需要观察的现象**：静态形态下 `opt -passes=goodbye` **不**需要 `-load-pass-plugin` 也能工作，因为 Bye 已经在二进制里了。

**预期结果**：能在脑子里讲清「同一个 `Bye.cpp`，靠一个 CMake 开关和一个 `#ifndef`，在动态 `.so` 与静态焊接之间切换」。本步骤为源码阅读型实践，标注「待本地验证」构建部分。

#### 4.3.5 小练习与答案

**练习 1**：扩展点回调和流水线解析回调，分别适合什么场景？请各举一个真实需求。

<details>
<summary>参考答案</summary>

- 扩展点回调适合「希望 pass 在用户写 `-O2` 时自动生效、无需用户记得写 pass 名」的场景。例如一个 instrumentation（插桩）pass，想在每个模块优化开始时自动插计数器，用 `registerPipelineStartEPCallback`。
- 流水线解析回调适合「希望 pass 有独立名字、按需手动调用」的场景。例如 Bye 用 `registerPipelineParsingCallback` 认领 `goodbye`，用户写 `opt -passes=goodbye` 才会跑。两者可以同时注册（像 Bye 那样）。
</details>

**练习 2**：`registerPipelineParsingCallback` 的 lambda 返回 `false` 表示什么？为什么 Bye 在名字不匹配时必须返回 `false`？

<details>
<summary>参考答案</summary>

返回 `false` 表示「这个名字不是我处理的」。`PassBuilder` 会把 `-passes` 里每个不认识的名字依次喂给所有已注册的解析回调，谁返回 `true` 就算谁认领；全都返回 `false` 才报「unknown pass name」错误。Bye 在 `Name != "goodbye"` 时返回 `false`（[Bye.cpp:46-54](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/examples/Bye/Bye.cpp#L46-L54)），这样别的插件还有机会认领同一个流水线里的其他名字，多个插件才能共存。
</details>

**练习 3**：为什么 `opt` 里要同时支持「动态插件」「显式 PassBuilderCallbacks」「静态扩展 Extension.def」三套来源？

<details>
<summary>参考答案</summary>

它们服务于不同的扩展方：动态插件面向**外部开发者**（编个 `.so` 即可）；显式回调面向**工具内嵌调用方**（如 `lli`、`clang` 等其他工具复用 `runPassPipeline` 时以代码方式注入）；静态扩展面向**树内测试或随工具一起发布的官方 pass**（不依赖运行期 `dlopen` 能力）。三套来源最终都汇入同一个 `PassBuilder`，保证了行为一致与机制统一（见 [NewPMDriver.cpp:464-475](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/tools/opt/NewPMDriver.cpp#L464-L475)）。
</details>

---

## 5. 综合实践

把本讲知识串起来，完成一个「**从零写一个最小插件**」的端到端任务。这个任务结合了 u4-l4（写 pass 骨架）和本讲（把 pass 打包成插件）。

### 任务描述

编写一个名为 `CountInst` 的插件，它提供一个 `countinst` pass：对每个函数，统计其中指令条数并打印到 `errs()`，然后能用 `opt -load-pass-plugin=... -passes=countinst` 运行。

### 操作步骤

1. **写 pass**：仿照 [Bye.cpp:33-39](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/examples/Bye/Bye.cpp#L33-L39) 的骨架，定义
   ```cpp
   struct CountInst : OptionalPassInfoMixin<CountInst> {
     PreservedAnalyses run(Function &F, FunctionAnalysisManager &) {
       unsigned N = 0;
       for (auto &BB : F)
         for (auto &I : BB)
           ++N;
       errs() << "countinst: " << F.getName() << " has " << N << " instrs\n";
       return PreservedAnalyses::all();
     }
   };
   ```
   > 示例代码，非项目原有代码。

2. **写注册回调**：仿照 [Bye.cpp:46-54](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/examples/Bye/Bye.cpp#L46-L54)，把名字 `countinst` 认领下来。

3. **写 C 入口与信息结构体**：仿照 [Bye.cpp:81-91](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/examples/Bye/Bye.cpp#L81-L91)，提供 `getCountInstPluginInfo()` 与 `llvmGetPassPluginInfo()`。

4. **写 CMakeLists.txt**：照 [Bye/CMakeLists.txt:10-15](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/examples/Bye/CMakeLists.txt#L10-L15) 用 `add_llvm_pass_plugin(CountInst CountInst.cpp ...)`。

5. **构建并运行**：
   ```bash
   ninja -C build CountInst
   opt -load-pass-plugin=build/lib/CountInst.so \
       -passes=countinst -disable-output /tmp/a.ll
   ```

### 需要观察的现象

- 每个函数打印一行指令计数。
- 删掉 `-load-pass-plugin` 后再跑 `-passes=countinst`，应报 `unknown function pass 'countinst'`——证明该名字确实只通过插件注册。
- 在注册回调里再追加一行 `PB.registerPipelineStartEPCallback(...)`，然后改用 `opt -default-pipeline -O2`（或对应默认流水线写法），观察 `countinst` 是否被自动插入。

### 预期结果

构建产物为 `lib/CountInst.so`，运行后输出函数指令计数。若你的环境无法构建，请把整个流程在源码层面走读一遍，并标注「待本地验证」。重点是验证你对「契约 → 加载 → 回调注册 → 流水线触发」这条链的理解。

---

## 6. 本讲小结

- **插件的本质动机**：在不重编 LLVM 的前提下，把自己的 pass 注入 `opt`/`clang` 等工具的流水线，把「改注册表 + 全量重编」降为「编一个 `.so` + 运行期加载」。
- **契约是纯 C ABI**：插件只导出一个 `extern "C" llvmGetPassPluginInfo()` 符号，返回 `PassPluginLibraryInfo` 结构体（API 版本 + 名字 + 版本 + 注册回调指针 + 可选的 pre-codegen 钩子），从而摆脱 C++ 名字修饰与 ABI 耦合。
- **`PassPlugin::Load` 四步走**：`dlopen` 打开库 → `dlsym` 找入口符号 → 调入口取结构体 → 校验 `LLVM_PLUGIN_API_VERSION`，任一步失败即拒绝加载。
- **`PassBuilder` 提供两类注册回调**：扩展点回调（如 `registerVectorizerStartEPCallback`）让 pass 在默认 `-O2/-O3` 流水线的固定位置自动插入；流水线解析回调（`registerPipelineParsingCallback`）让 pass 拥有可被 `-passes=` 显式引用的名字。两者常同时注册。
- **三种来源殊途同归**：动态插件（`-load-pass-plugin`）、显式 `PassBuilderCallbacks`、静态扩展（`Extension.def` 的 X 宏）最终都汇入同一个 `PassBuilder`，行为一致。
- **动态/静态由一个开关切换**：`LLVM_<NAME>_LINK_INTO_TOOLS` 控制是建 MODULE 库（`.so`）还是 OBJECT 库（焊进工具），同一份源码靠 `#ifndef` 守卫 C 入口的导出与否。

---

## 7. 下一步学习建议

- **回到测试**：本讲的插件需要测试。下一讲 [u9-l1 测试体系：lit、FileCheck 与单元测试](u9-l1-testing-infra.md) 已讲过 lit/FileCheck，你可以仿照 [load_extension.ll](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/test/Feature/load_extension.ll) 为你的插件写一个 `; RUN:` + `; CHECK:` 回归测试。
- **深入 `PassBuilder` 扩展点全貌**：本讲只用了 `VectorizerStart` 与流水线解析两种回调。建议通读 [PassBuilder.h](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/Passes/PassBuilder.h) 中所有 `register...EPCallback`，理解默认流水线的各个插入点（PipelineStart、OptimizerEarly/Last、FullLinkTimeOptimization 等），这对写出能在正确时机触发的插件至关重要。
- **进阶阅读 `clang` 与 `llvm-lto2` 的插件入口**：`clang -fpass-plugin` 与 `llvm-lto2 -load-pass-plugin` 走的是同一套契约，但触发时机不同（前端 vs 链接时优化）。结合 [u8-l2 LTO](u8-l2-lto.md) 理解插件在 LTO 场景下的接入点。
- **动手扩展**：尝试写一个真正改写 IR 的插件 pass（比如简单的统计 + 删除死代码），用 `PreservedAnalyses::none()` 正确声明副作用，并用 u4-l4 的 `STATISTIC` 宏配合 `-stats` 观察计数。
