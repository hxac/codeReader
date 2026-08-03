# RewriteInstance::run() 全流程总览

## 1. 本讲目标

学完本讲，你应当能够：

- 按执行顺序列出 `RewriteInstance::run()` 调用的全部阶段函数，并说清每个阶段做什么。
- 说清楚 `AggregateOnly`（perf2bolt）、`DiffOnly`（boltdiff）、`BinaryAnalysisMode`（binary-analysis）、正常优化模式这「四种人格」如何复用同一条 `run()` 管线、又分别在哪里提前退出。
- 理解 `NamedRegionTimer` 如何配合 `-time-rewrite` / `-time-build` 选项，把每个阶段的耗时单独计量并打印出来。
- 在脑子里建立一张「输入二进制 → 发现 → 反汇编 → CFG → profile → 优化 → 发射 → 重写输出」的全链路心智地图。

本讲是单元 3 的总纲。后续讲义（u3-l2 发现与 section、u3-l3 反汇编与 CFG、u3-l4 重定位与跳转表）都是把这条链路里的某一段拆开细讲。先把整条线抓住，再读细节才不会迷路。

## 2. 前置知识

本讲承接 u1-l4 与 u2-l1 的认知，复习两个关键点：

- **程序入口与分流（u1-l4）**：`tools/driver/llvm-bolt.cpp` 的 `main()` 用 `argv[0]` 的名字前缀把控制权分发到 `perf2boltMode` / `boltDiffMode` / `boltMode` 三种模式；随后 `createBinary` 打开文件，对 ELF 调用 `RewriteInstance::run()`，对 Mach-O 调用 `MachORewriteInstance::run()`。本讲只看 **ELF** 这条主线，也就是 `RewriteInstance::run()`。
- **BinaryContext（u2-l1）**：`RewriteInstance` 持有一个 `std::unique_ptr<BinaryContext> BC`，几乎所有阶段都是围绕 `BC` 里登记的函数、section、符号、重定位在工作。`run()` 就是「指挥 `BC` 完成读 → 优化 → 写」的调度函数。

还需要两个背景概念：

- **阶段（stage）**：本讲里「阶段」指 `run()` 依次调用的一个成员函数，例如 `disassembleFunctions()`、`buildFunctionsCFG()`。每个阶段职责单一，靠前后顺序保证数据依赖。
- **模式（mode）**：BOLT 的同一个 `llvm-bolt` 二进制要服务「优化」「profile 聚合」「二进制差异对比」「安全分析」四类任务。它们共用 `run()` 管线，但在不同位置提前 `return`（或 `exit`）来缩短流程。模式由命令行选项（一组 `cl::opt<bool>`）控制。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lib/Rewrite/RewriteInstance.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp) | 本讲主角，`run()` 与各阶段函数都在这里 |
| [include/bolt/Rewrite/RewriteInstance.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Rewrite/RewriteInstance.h) | `RewriteInstance` 类声明，列出全部阶段函数签名 |
| [lib/Utils/CommandLineOpts.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Utils/CommandLineOpts.cpp) | 定义 `-time-rewrite`、`-instrument`、`-aggregate-only`、`-diff-only` 等选项；`BinaryAnalysisMode` 全局变量 |
| [tools/driver/llvm-bolt.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp) | `main()`，设置 `AggregateOnly` / `DiffOnly` 并调用 `run()` |
| [tools/binary-analysis/binary-analysis.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/binary-analysis/binary-analysis.cpp) | 独立的 `binary-analysis` 工具，置 `BinaryAnalysisMode = true` 后也调用 `run()` |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **run() 的阶段序列**：主链路总调度。
2. **模式分流**：四种模式如何共用一条管线、各自在哪里提前退出。
3. **NamedRegionTimer 计时**：`-time-rewrite` 如何测量每个阶段。

### 4.1 run()：BOLT 主链路的总调度

#### 4.1.1 概念说明

`RewriteInstance::run()` 是 ELF 重写的「总调度函数」。它的角色类似一条流水线的车间主任：自己不亲手做每一道工序，而是按固定顺序呼叫各个阶段函数（每个阶段是一个成员函数），把输入二进制一步步加工成输出二进制。

`run()` 的设计哲学是「**线性、顺序、单一职责**」：

- 阶段之间是**严格的顺序依赖**——后面的阶段需要前面阶段建立好的数据结构。例如必须先 `disassembleFunctions()`（反汇编出指令）才能 `buildFunctionsCFG()`（在指令上切出基本块）；必须先有 CFG 才能做 `runOptimizationPasses()`（优化要改 CFG）。
- 任何阶段返回 `Error`（`discoverStorage`、`readSpecialSections` 等）都会立刻向上冒泡，整个 `run()` 提前结束。这是用 LLVM 的 `Error` 机制做错误处理。
- `run()` 本身尽量「瘦」，只编排顺序；具体逻辑全在各阶段函数里。

> 提示：类头文件顶部的注释把这个职责写得很清楚——「encapsulates all data necessary to carry on binary reading, disassembly, CFG building, BB reordering ... and rewriting. It also has the logic to coordinate such events.」（见 [RewriteInstance.h:40-43](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Rewrite/RewriteInstance.h#L40-L43)）。

#### 4.1.2 核心流程

把 `run()` 的正常（优化）模式展开成一条流水线，可以分成 **7 个大块**：

```text
┌─────────────────────────────────────────────────────────────────┐
│ A. 发现（Discovery）                                            │
│    selectFunctionsToPrint → discoverStorage → readSpecialSections│
│    → adjustCommandLineOptions → discoverFileObjects             │
│    （+ 插桩模式额外：discoverRtInitAddress/FiniAddress）          │
├─────────────────────────────────────────────────────────────────┤
│ B. profile/调试预处理                                          │
│    preprocessProfileData → selectFunctionsToProcess             │
│    → readDebugInfo                                              │
├─────────────────────────────────────────────────────────────────┤
│ C. 反汇编 + CFG                                                │
│    disassembleFunctions → processMetadataPreCFG                 │
│    → buildFunctionsCFG → processProfileData                     │
│    （+ EnableBAT 时保存原始元数据 BAT->saveMetadata）            │
│    → postProcessFunctions → processMetadataPostCFG             │
├─────────────────────────────────────────────────────────────────┤
│   ★ 模式分流点：DiffOnly / BinaryAnalysisMode 在此 return ★     │
├─────────────────────────────────────────────────────────────────┤
│ D. 优化                                                        │
│    preregisterSections → runOptimizationPasses                  │
├─────────────────────────────────────────────────────────────────┤
│ E. 发射与链接                                                  │
│    finalizeMetadataPreEmit → emitAndLink → updateMetadata       │
│    （+ 插桩模式额外：updateRtInitReloc/FiniReloc）               │
├─────────────────────────────────────────────────────────────────┤
│ F. 落盘                                                        │
│    （-o /dev/null 时跳过）→ rewriteFile                          │
└─────────────────────────────────────────────────────────────────┘
```

这条流水线对应了 BOLT 的本质：**读（A/B/C）→ 优化（D）→ 写（E/F）**。profile（运行时热冷信息）在 C 块的 `processProfileData` 里叠加到 CFG 上，是后续 D 块布局优化的输入。

#### 4.1.3 源码精读

`run()` 的完整实现见 [RewriteInstance.cpp:784-866](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L784-L866)。下面按顺序分段引用。

**开头：打印架构与版本，然后做发现阶段。**

[lib/Rewrite/RewriteInstance.cpp:784-807](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L784-L807)：

```cpp
Error RewriteInstance::run() {
  assert(BC && "failed to create a binary context");

  BC->outs() << "BOLT-INFO: Target architecture: " << ... << "\n";
  BC->outs() << "BOLT-INFO: BOLT version: " << BoltRevision << "\n";

  selectFunctionsToPrint();

  if (Error E = discoverStorage())
    return E;
  if (Error E = readSpecialSections())
    return E;
  adjustCommandLineOptions();
  discoverFileObjects();

  if (opts::Instrument && !BC->IsStaticExecutable) {
    if (Error E = discoverRtInitAddress())
      return E;
    if (Error E = discoverRtFiniAddress())
      return E;
  }
```

- `selectFunctionsToPrint()`：根据 `-print-only=/-print-all?` 等选项标记哪些函数要后续打印。
- `discoverStorage()`：在 ELF 里找可用于分配新 section 的空闲地址区间（带 `Error` 返回，失败即终止）。
- `readSpecialSections()`：解析 `.eh_frame`、`.gcc_except_table` 等异常/栈展开特殊 section。
- `adjustCommandLineOptions()`：根据输入二进制的实际情况微调命令行选项（例如自动判断是否处于重定位模式）。
- `discoverFileObjects()`：**核心阶段**，从符号表发现函数，建立 `BinaryFunction` 对象（详见 u3-l2）。
- 插桩分支：只有 `-instrument` 且非静态可执行文件时，才额外去发现运行时库的 init/fini 入口。

**中段：profile 预处理、调试信息、反汇编、CFG 构建、profile 叠加。**

[lib/Rewrite/RewriteInstance.cpp:809-829](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L809-L829)：

```cpp
  preprocessProfileData();

  selectFunctionsToProcess();

  readDebugInfo();

  disassembleFunctions();

  processMetadataPreCFG();

  buildFunctionsCFG();

  processProfileData();

  // Save input binary metadata if BAT section needs to be emitted
  if (opts::EnableBAT)
    BAT->saveMetadata(*BC);

  postProcessFunctions();

  processMetadataPostCFG();
```

- `preprocessProfileData()`：在还没反汇编时先做一轮 profile 预读（例如把 profile 里的地址先索引起来）。
- `selectFunctionsToProcess()`：标记 `-funcs=` 限定处理范围、把不该处理的函数标为 ignored。
- `readDebugInfo()`：读取 DWARF 调试信息（行号、范围等），供后续 `-update-debug-sections` 用。
- `disassembleFunctions()`：逐函数把字节流解码成 `MCInst`（详见 u3-l3）。
- `processMetadataPreCFG()` / `processMetadataPostCFG()`：在 CFG 构建前后处理元数据（详见 u8-l3）。
- `buildFunctionsCFG()`：在指令上切分基本块、连边，得到 CFG（详见 u3-l3）。
- `processProfileData()`：把 profile 计数叠加到函数/基本块/边上。**注意：`AggregateOnly`（perf2bolt）模式在这里提前 `exit(0)`，见 4.2.3。**
- `EnableBAT` 分支：若开启 BAT，在函数地址尚未被优化改写前保存原始元数据，供 BAT 表反向翻译。
- `postProcessFunctions()`：CFG 建好后的收尾（注册函数分片、整理 layout 等）。

**模式分流点（关键！）。**

[lib/Rewrite/RewriteInstance.cpp:831-837](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L831-L837)：

```cpp
  if (opts::DiffOnly)
    return Error::success();

  if (opts::BinaryAnalysisMode) {
    runBinaryAnalyses();
    return Error::success();
  }
```

- `DiffOnly`（boltdiff 模式）：到这一步已经够做二进制对比，直接 `return`。
- `BinaryAnalysisMode`（binary-analysis 工具）：跑完安全分析就 `return`。
- 这两个 `return` 之后的阶段（优化、发射、重写）在这两种模式下**根本不执行**。

**后段：优化、发射、链接、重写落盘。**

[lib/Rewrite/RewriteInstance.cpp:839-866](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L839-L866)：

```cpp
  preregisterSections();

  runOptimizationPasses();

  finalizeMetadataPreEmit();

  emitAndLink();

  updateMetadata();

  if (opts::Instrument && !BC->IsStaticExecutable) {
    if (Error E = updateRtInitReloc())
      return E;
    if (Error E = updateRtFiniReloc())
      return E;
  }

  if (opts::OutputFilename == "/dev/null") {
    BC->outs() << "BOLT-INFO: skipping writing final binary to disk\n";
    return Error::success();
  } else if (BC->IsLinuxKernel) {
    BC->errs() << "BOLT-WARNING: Linux kernel support is experimental\n";
  }

  // Rewrite allocatable contents and copy non-allocatable parts with mods.
  rewriteFile();
  return Error::success();
}
```

- `preregisterSections()`：预先登记输出需要的 section。
- `runOptimizationPasses()`：**核心阶段**，跑布局/分裂/调用等全部优化 pass（详见单元 5、6）。它把控制权交给 `BinaryFunctionPassManager::runAllPasses`。
- `finalizeMetadataPreEmit()` / `updateMetadata()`：发射前后的元数据收尾（详见 u8-l3）。
- `emitAndLink()`：用 `MCStreamer` 把优化后的函数重新发射成可链接对象，再用基于 ORC 的 `JITLinkLinker` 链接回内存，得到最终地址（详见 u7-l2）。
- 插桩后处理分支：更新 init/fini 数组的重定位，指向插桩运行时库。
- `-o /dev/null` 分支：跳过写盘，常用于只看 `-dyno-stats` 统计而不产出二进制的场景。
- `rewriteFile()`：把优化后的可分配内容写回，并带上修改后的非可分配部分（如符号表、调试段），得到最终输出文件。

#### 4.1.4 代码实践

**实践目标**：亲手把 `run()` 的阶段顺序梳理成一张可核对的清单。

**操作步骤**：

1. 打开 [RewriteInstance.cpp:784-866](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L784-L866)。
2. 从第 784 行往下读，每遇到一个「形如 `xxxYyy()` 或 `if (Error E = xxxYyy())`」的调用，就记一行：函数名 + 一句话职责。
3. 对照下面的「标准答案表」核对你有没有漏。

**标准答案（正常优化模式下实际执行的阶段，按顺序）**：

| # | 阶段函数 | 一句话职责 |
| --- | --- | --- |
| 1 | `selectFunctionsToPrint()` | 标记要打印的函数 |
| 2 | `discoverStorage()` | 找可用于新 section 的地址空间 |
| 3 | `readSpecialSections()` | 解析 `.eh_frame` 等特殊 section |
| 4 | `adjustCommandLineOptions()` | 按输入微调选项 |
| 5 | `discoverFileObjects()` | 从符号表发现并创建 `BinaryFunction` |
| 6 | `preprocessProfileData()` | 反汇编前的 profile 预读 |
| 7 | `selectFunctionsToProcess()` | 标记要处理/忽略的函数 |
| 8 | `readDebugInfo()` | 读 DWARF 调试信息 |
| 9 | `disassembleFunctions()` | 字节流 → `MCInst` |
| 10 | `processMetadataPreCFG()` | CFG 前的元数据处理 |
| 11 | `buildFunctionsCFG()` | 切基本块、连边得到 CFG |
| 12 | `processProfileData()` | 把 profile 叠加到 CFG |
| 13 | `postProcessFunctions()` | CFG 后的收尾（分片、layout） |
| 14 | `processMetadataPostCFG()` | CFG 后的元数据处理 |
| 15 | `preregisterSections()` | 预登记输出 section |
| 16 | `runOptimizationPasses()` | 跑全部优化 pass |
| 17 | `finalizeMetadataPreEmit()` | 发射前元数据收尾 |
| 18 | `emitAndLink()` | 重新发射 + ORC 链接得最终地址 |
| 19 | `updateMetadata()` | 发射后元数据更新 |
| 20 | `rewriteFile()` | 写回最终输出二进制 |

> 注：`discoverRtInitAddress/FiniAddress`（步骤 5 之后）和 `updateRtInitReloc/FiniReloc`（步骤 19 之前）只在 `-instrument` 且非静态可执行时执行；`BAT->saveMetadata`（步骤 12 之后）只在 `-enable-bat` 时执行。它们是条件分支，不算「主干」的 20 步。

**需要观察的现象 / 预期结果**：你的清单应与上表 20 行一一对应；如果你把条件分支也算进去，会发现主干之外还有 2 处插桩分支和 1 处 BAT 分支。

> 待本地验证：上表职责描述基于源码阅读，未实际运行确认每个阶段的内部行为；阶段函数的内部细节在后续讲义展开。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `disassembleFunctions()` 必须在 `buildFunctionsCFG()` 之前？反过来行不行？

**参考答案**：`buildFunctionsCFG()` 的输入是已经解码好的 `MCInst` 指令序列，它要在指令边界上切基本块、识别分支并连边。而 `disassembleFunctions()` 才是把原始字节解码成 `MCInst` 的步骤。没有指令就没有切分依据，所以必须先反汇编。反过来会导致 CFG 构建时拿不到任何指令。

**练习 2**：`run()` 用什么机制在某个阶段失败时提前终止？

**参考答案**：用 LLVM 的 `Error` 返回值。凡是可能失败的阶段（如 `discoverStorage`、`readSpecialSections`、`discoverRtInitAddress` 等）都返回 `Error`，`run()` 用 `if (Error E = xxx()) return E;` 的惯用法把错误向上冒泡，整个 `run()` 立刻返回，跳过后续阶段。

---

### 4.2 模式分流：四种「人格」共用一条管线

#### 4.2.1 概念说明

`llvm-bolt` 这一个二进制要干四类活，靠 `argv[0]` 名字和命令行选项区分（见 u1-l4）。这四类活分别是：

| 模式 | 触发方式 | 干什么 | 在 `run()` 里走到哪 |
| --- | --- | --- | --- |
| **正常优化** | `boltMode`（默认） | 反汇编 + 叠 profile + 优化 + 重写，产出优化二进制 | 走完全程（步骤 1–20） |
| **perf2bolt 聚合** | `perf2boltMode`，置 `AggregateOnly=true` | 把 `perf.data` 聚合成 fdata | 走到步骤 12 `processProfileData` 内部 `exit(0)` |
| **boltdiff 对比** | `boltDiffMode`，置 `DiffOnly=true` | 对比两个二进制的优化差异 | 走到步骤 14 后 `return`（步骤 831–832） |
| **binary-analysis 分析** | 独立工具，置 `BinaryAnalysisMode=true` | 跑安全分析（如 PAC gadget 扫描） | 走到步骤 14 后 `runBinaryAnalyses()` 再 `return`（步骤 834–837） |

关键洞察：**四种模式共用同一条 `run()` 管线**，区别只在于「在哪里提前退出」。前 14 个阶段（发现→反汇编→CFG→profile 叠加→元数据）是几乎所有模式都要走的公共前置，因为不论后面干什么，都得先有「函数 + CFG + profile」这张底图。

#### 4.2.2 核心流程

模式开关的「设置点」和「检查点」分布在三个文件里：

```text
设置点（在 run() 之前）：
  perf2boltMode()           → opts::AggregateOnly = true   [llvm-bolt.cpp:120]
  boltDiffMode()            → opts::DiffOnly = true        [llvm-bolt.cpp:147]
  binary-analysis main()    → opts::BinaryAnalysisMode = true  [binary-analysis.cpp:103]
  （正常模式什么都不设）

检查点（在 run() 内部）：
  AggregateOnly  → 在 processProfileData() 内部 exit(0)   [RewriteInstance.cpp:3893]
  DiffOnly       → 在 run() 第 831 行 return              [RewriteInstance.cpp:831]
  BinaryAnalysis → 在 run() 第 834 行 runBinaryAnalyses() + return
```

注意三种提前退出的「力度」不同：

- `AggregateOnly` 用的是 `exit(0)`（进程级退出），因为它发生在成员函数 `processProfileData` 内部，需要直接结束整个程序来跳过后续所有阶段和写盘。
- `DiffOnly` 和 `BinaryAnalysisMode` 用的是 `return Error::success()`（函数级返回），因为它们正好在 `run()` 主体里，可以直接返回。

#### 4.2.3 源码精读

**模式开关的定义。**

[lib/Utils/CommandLineOpts.cpp:33](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Utils/CommandLineOpts.cpp#L33)：`BinaryAnalysisMode` 是一个普通全局 `bool`（不是 `cl::opt`，因为由独立工具直接赋值）：

```cpp
bool BinaryAnalysisMode = false;
```

[lib/Utils/CommandLineOpts.cpp:97-100](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Utils/CommandLineOpts.cpp#L97-L100)：`AggregateOnly` 是个 `Hidden` 的 `cl::opt<bool>`（用户不直接传，由 `perf2boltMode` 程序内设置）：

```cpp
cl::opt<bool>
AggregateOnly("aggregate-only",
  cl::desc("exit after writing aggregated data file"),
  cl::Hidden,
  cl::cat(AggregatorCategory));
```

[lib/Utils/CommandLineOpts.cpp:113-116](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Utils/CommandLineOpts.cpp#L113-L116)：`DiffOnly` 同样是 `Hidden`：

```cpp
cl::opt<bool>
DiffOnly("diff-only",
  cl::desc("stop processing once we have enough to compare two binaries"),
  cl::Hidden,
  cl::cat(BoltDiffCategory));
```

**设置点。**

[tools/driver/llvm-bolt.cpp:120](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp#L120)：`perf2boltMode` 末尾置 `AggregateOnly=true`（同时置 `ShowDensity=true`）。

[tools/driver/llvm-bolt.cpp:147](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp#L147)：`boltDiffMode` 末尾置 `DiffOnly=true`。

[tools/binary-analysis/binary-analysis.cpp:103](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/binary-analysis/binary-analysis.cpp#L103)：`binary-analysis` 工具的 `main` 里置 `BinaryAnalysisMode = true`，随后照样 `create` 一个 `RewriteInstance` 并调用 `run()`（[binary-analysis.cpp:115-120](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/binary-analysis/binary-analysis.cpp#L115-L120)）。这印证了「四种人格共用 `run()`」。

**检查点（提前退出）。**

`DiffOnly` 与 `BinaryAnalysisMode` 的退出在 4.1.3 已引用（[RewriteInstance.cpp:831-837](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L831-L837)）。

`AggregateOnly` 的退出藏在 `processProfileData()` 内部。注意它不在 `run()` 主体里，所以不能用 `return`，只能 `exit(0)`。[lib/Rewrite/RewriteInstance.cpp:3883-3898](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L3883-L3898)：

```cpp
  if (opts::AggregateOnly &&
      opts::ProfileFormat == opts::ProfileFormatKind::PF_YAML &&
      !BAT->enabledFor(InputFile)) {
    YAMLProfileWriter PW(opts::OutputFilename);
    PW.writeProfile(*this);
  }

  // Release memory used by profile reader.
  ProfileReader.reset();

  if (opts::AggregateOnly) {
    PrintProgramStats PPS(&*BAT);
    BC->logBOLTErrorsAndQuitOnFatal(PPS.runOnFunctions(*BC));
    TimerGroup::printAll(outs());
    exit(0);
  }
```

也就是说：perf2bolt 模式下，`run()` 一路走到 `processProfileData`（步骤 12），在里面把聚合后的 fdata 写出、打印统计、打印计时报告，然后 `exit(0)`——后面所有阶段（13–20）一概不执行。

> 为什么 perf2bolt 也要走反汇编+CFG？因为要把 LBR 分支采样里的原始地址翻译成「函数名/块」语义，需要先发现并理解函数边界与 CFG。聚合的本质就是「按已识别的函数/边把原始分支计数归类」。

#### 4.2.4 代码实践

**实践目标**：搞清楚在 `DiffOnly`（boltdiff）模式下，哪些阶段被执行、哪些被跳过。

**操作步骤**：

1. 读 [RewriteInstance.cpp:831-832](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L831-L832)，定位 `DiffOnly` 的 `return`。
2. 以这个 `return` 为界，把 4.1.4 的 20 步清单切成「**会执行**」和「**被跳过**」两组。
3. 验证你的判断：boltdiff 的真实流程是「`RI1.run()` 提前 return → `RI2.run()` 提前 return → `RI1.compare(RI2)`」。可在 [tools/driver/llvm-bolt.cpp:298](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp#L298) 附近确认 `run()` 之后还有 `compare()` 调用。

**需要观察的现象 / 预期结果**：

- **会执行**：步骤 1–14（`selectFunctionsToPrint` 到 `processMetadataPostCFG`）。
- **被跳过**：步骤 15–20（`preregisterSections`、`runOptimizationPasses`、`finalizeMetadataPreEmit`、`emitAndLink`、`updateMetadata`、`rewriteFile`）。

也就是说，boltdiff 模式**不优化、不发射、不写盘**——它只需要「两个二进制各自的函数 + CFG + profile」这张底图，足够 `compare()` 做差异对比即可。

> 待本地验证：跳过结论基于源码分支逻辑，未实际运行 boltdiff 确认。

#### 4.2.5 小练习与答案

**练习 1**：`AggregateOnly`、`DiffOnly`、`BinaryAnalysisMode` 三种提前退出，为什么 `AggregateOnly` 用 `exit(0)` 而另外两个用 `return`？

**参考答案**：因为检查点位置不同。`DiffOnly` 和 `BinaryAnalysisMode` 的检查点在 `run()` 主体里（第 831、834 行），直接 `return Error::success()` 就能退出 `run()`。而 `AggregateOnly` 的检查点在成员函数 `processProfileData()` 内部（第 3893 行），处于更深的调用栈，`return` 只能退出 `processProfileData`、无法退出 `run()`，所以必须用进程级 `exit(0)` 直接结束程序。

**练习 2**：为什么 `binary-analysis` 是一个**独立可执行文件**，却和 `llvm-bolt` 共用 `RewriteInstance::run()`？

**参考答案**：因为它需要的工作完全在 `run()` 管线的公共前置阶段之内——发现函数、反汇编、建 CFG。安全分析（如 PAC gadget 扫描）只需要「函数 + CFG + 指令」这张底图，不需要优化和重写。所以它置 `BinaryAnalysisMode = true` 复用 `run()`，到步骤 14 后转去 `runBinaryAnalyses()` 再 `return`。把它做成独立工具是为了简化命令行（只暴露分析相关选项）。

---

### 4.3 NamedRegionTimer：用 -time-rewrite 测量每个阶段

#### 4.3.1 概念说明

BOLT 处理一个大二进制可能耗时几十秒到几分钟，想知道「时间花在哪」就需要逐阶段计时。BOLT 用的是 LLVM 通用的计时设施：`NamedRegionTimer`。

- **`NamedRegionTimer`** 是一个 RAII 对象：构造时记下当前时间戳并「登记」到一个 `TimerGroup`，析构时（离开作用域）计算耗时并累计到该 `TimerGroup`。因此只要在某个阶段函数开头创建一个 `NamedRegionTimer` 局部变量，它就自动覆盖整个函数体的耗时。
- **`TimerGroup`** 是一组相关计时器的集合，最终统一打印成一张表。BOLT 用静态字符串 `TimerGroupName = "rewrite"` / `TimerGroupDesc = "Rewrite passes"` 作为组名（[RewriteInstance.cpp:349-350](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L349-L350)）。
- **启用开关**：`NamedRegionTimer` 的构造函数最后一个参数是「是否启用」。BOLT 传的是 `opts::TimeRewrite`，即 `-time-rewrite` 选项。不传该选项时，计时器虽仍构造但实质不计时（开销可忽略），不会打印报告。

BOLT 实际上有**两个**计时选项：

| 选项 | 控制的计时器组 | 覆盖范围 |
| --- | --- | --- |
| `-time-rewrite`（`opts::TimeRewrite`） | 组 `rewrite` | 大多数重写阶段（发现、反汇编、profile、优化、发射等） |
| `-time-build`（`opts::TimeBuild`） | 组 `buildfuncs` | CFG 构建相关（`disassembleFunctions` 内部的 scan、`buildFunctionsCFG`） |

这点容易踩坑：`buildFunctionsCFG()` 用的是 `-time-build` 而不是 `-time-rewrite`（见 [RewriteInstance.cpp:4032-4033](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L4032-L4033)）。所以要看到完整报告，两个选项都得开。

#### 4.3.2 核心流程

一个阶段函数配合计时的标准写法（伪代码）：

```text
void RewriteInstance::某阶段() {
  NamedRegionTimer T("阶段名", "可读描述",
                     TimerGroupName, TimerGroupDesc, opts::TimeRewrite);
  // ... 阶段实际工作 ...
}   // ← T 在此析构，耗时累计到 TimerGroup "rewrite"
```

`-time-rewrite` 如何变成报告：

```text
命令行带 -time-rewrite
  → opts::TimeRewrite == true
  → 每个阶段的 NamedRegionTimer 真正计时，累计到 TimerGroup "rewrite"
  → 程序结束时（或 AggregateOnly 模式下显式 TimerGroup::printAll(outs())）
    打印一张 "===-------------------------------------------------------------------------===\n ... Final Time report"
```

每个计时条目形如：

```text
===-------------------------------------------------------------------------===
                      ... Final Time report ...
===-------------------------------------------------------------------------===
  Total Execution Time: 12.3405 seconds (12.3405 wall clock)

  ---User Time--- --System Time-- --User+System-- ---Wall Time---  --- Name ---
    8.1234 ( 65.8%)   0.1234 ( 1.0%)   8.2468 ( 66.8%)   8.2468 ( 66.8%)  runOptimizationPasses
    2.0011 ( 16.2%)   0.0000 ( 0.0%)   2.0011 ( 16.2%)   2.0011 ( 16.2%)  emitAndLink
    ...
```

（以上数字为示意，待本地验证实际输出。）

#### 4.3.3 源码精读

**计时组名定义。**

[lib/Rewrite/RewriteInstance.cpp:349-350](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L349-L350)：

```cpp
const char RewriteInstance::TimerGroupName[] = "rewrite";
const char RewriteInstance::TimerGroupDesc[] = "Rewrite passes";
```

**`-time-rewrite` 选项定义。**

[lib/Utils/CommandLineOpts.cpp:349-351](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Utils/CommandLineOpts.cpp#L349-L351)：

```cpp
cl::opt<bool> TimeRewrite("time-rewrite",
                          cl::desc("print time spent in rewriting passes"),
                          cl::Hidden, cl::cat(BoltCategory));
```

注意它标记为 `cl::Hidden`——不会出现在 `--help` 里，但在源码注释和调试场景里常用。

**各阶段计时器的统一写法（节选）。**

`-time-rewrite` 控制的计时器（组 `rewrite`），列举几个代表性阶段：

| 阶段函数 | 计时器名 | 行号 |
| --- | --- | --- |
| `discoverStorage` | `discoverStorage` | [RewriteInstance.cpp:611-612](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L611-L612) |
| `discoverFileObjects` | `discoverFileObjects` | [RewriteInstance.cpp:869-870](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L869-L870) |
| `readSpecialSections` | `readSpecialSections` | [RewriteInstance.cpp:2383-2384](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L2383-L2384) |
| `readDebugInfo` | `readDebugInfo` | [RewriteInstance.cpp:3766-3767](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L3766-L3767) |
| `preprocessProfileData` | `preprocessprofile` | [RewriteInstance.cpp:3784-3785](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L3784-L3785) |
| `processMetadataPreCFG` | `processmetadata-precfg` | [RewriteInstance.cpp:3835-3836](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L3835-L3836) |
| `processMetadataPostCFG` | `processmetadata-postcfg` | [RewriteInstance.cpp:3843-3844](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L3843-L3844) |
| `processProfileData` | `processprofile` | [RewriteInstance.cpp:3863-3864](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L3863-L3864) |
| `disassembleFunctions` | `disassembleFunctions` | [RewriteInstance.cpp:3902-3903](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L3902-L3903) |
| `runOptimizationPasses` | `runOptimizationPasses` | [RewriteInstance.cpp:4115-4116](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L4115-L4116) |
| `runBinaryAnalyses` | `runBinaryAnalyses` | [RewriteInstance.cpp:4121-4122](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L4121-L4122) |
| `emitAndLink` | `emitAndLink` | [RewriteInstance.cpp:4186-4187](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L4186-L4187) |
| `finalizeMetadataPreEmit` | `finalizemetadata-preemit` | [RewriteInstance.cpp:4299-4300](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L4299-L4300) |
| `updateMetadata` | `updatemetadata-postemit` | [RewriteInstance.cpp:4305-4306](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L4305-L4306) |

`runOptimizationPasses` 的例子最能说明模式——[lib/Rewrite/RewriteInstance.cpp:4114-4118](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L4114-L4118)：

```cpp
void RewriteInstance::runOptimizationPasses() {
  NamedRegionTimer T("runOptimizationPasses", "run optimization passes",
                     TimerGroupName, TimerGroupDesc, opts::TimeRewrite);
  BC->logBOLTErrorsAndQuitOnFatal(BinaryFunctionPassManager::runAllPasses(*BC));
}
```

**CFG 构建用的是另一个选项 `-time-build`。**

`disassembleFunctions` 内部的「scan」和 `buildFunctionsCFG` 用的是 `opts::TimeBuild`、组名 `buildfuncs`——[lib/Rewrite/RewriteInstance.cpp:4032-4033](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L4032-L4033)：

```cpp
  NamedRegionTimer T("buildCFG", "buildCFG", "buildfuncs",
                     "Build Binary Functions", opts::TimeBuild);
```

`TimeBuild` 定义在 [lib/Core/BinaryFunction.cpp:134](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L134)（`-time-build`）。

**报告何时打印。**

在 `AggregateOnly` 模式下，`processProfileData()` 里有**显式**的 `TimerGroup::printAll(outs())`——[RewriteInstance.cpp:3896](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L3896)。正常优化模式下，则由 LLVM 计时设施在程序退出时统一打印所有登记过的 `TimerGroup`（即 `-time-rewrite` 和 `-time-build` 两组都会出现）。

#### 4.3.4 代码实践

**实践目标**：用 `-time-rewrite` 实际测量一次 BOLT 运行，看清各阶段耗时占比。

**操作步骤**：

1. 准备一个满足 BOLT 输入要求的二进制（未 strip、`-Wl,-q` 链接，见 u1-l3）和一份 fdata profile。
2. 运行（命令本身在 u1-l3 已讲）：

   ```bash
   llvm-bolt 输入二进制 -o 输出二进制 \
     -data=profile.fdata \
     -reorder-blocks=ext-tsp -reorder-functions \
     -split-functions -time-rewrite -time-build
   ```

3. 在 stdout 末尾寻找 `=== ... Final Time report ... ===` 表格。

**需要观察的现象**：

- 不加 `-time-rewrite` 时：**没有**时间报告输出。
- 加 `-time-rewrite` 时：出现一张表，列出 `runOptimizationPasses`、`emitAndLink`、`discoverFileObjects` 等条目及其 `Wall Time` 与百分比。
- 通常 `runOptimizationPasses` 和 `emitAndLink` 占大头。

**预期结果**：你能从报告里读出「哪个阶段最慢」，从而判断是该调优 pass 数量、还是该关注发射/链接环节。

> 待本地验证：上述命令的实际耗时数字与表格格式取决于本地二进制规模与机器，本讲不提供具体数字。若手头没有可用二进制，可退化为「源码阅读型实践」：对照 4.3.3 的表格，逐个打开行号链接，确认每个阶段的计时器名与所用选项（`TimeRewrite` 还是 `TimeBuild`）。

#### 4.3.5 小练习与答案

**练习 1**：你跑了 `-time-rewrite`，却发现报告里**没有** `buildCFG` 这一条。为什么？怎么办？

**参考答案**：因为 `buildFunctionsCFG()` 用的是 `-time-build`（组 `buildfuncs`）而非 `-time-rewrite`（组 `rewrite`）。`-time-rewrite` 只开启组 `rewrite` 的计时器。解决办法是同时加 `-time-build`，这样 `buildfuncs` 组也会被启用并打印。

**练习 2**：`NamedRegionTimer` 为什么不需要手动「停止计时」？

**参考答案**：因为它是 RAII 对象。构造时启动计时并登记到 `TimerGroup`，析构时（函数返回、离开作用域）自动停止计时并累计。把局部变量声明在阶段函数开头，它的作用域就正好覆盖整个函数体，无需手动 stop。

---

## 5. 综合实践

**任务**：用本讲建立的全链路心智模型，给一次真实的 `llvm-bolt` 运行「标注进度」。

**步骤**：

1. 选一个能用 BOLT 跑通的小二进制（参照 u1-l3 的 OptimizingClang 流程，或一个 hello world）。
2. 用 `-v=1`（提高日志级别）和 `-time-rewrite -time-build` 运行优化：

   ```bash
   llvm-bolt input.bin -o output.bin -data=p.fdata \
     -reorder-blocks=ext-tsp -split-functions \
     -v=1 -time-rewrite -time-build 2>&1 | tee bolt.log
   ```

3. 在 `bolt.log` 里，按时间顺序找出每条 `BOLT-INFO` / `BOLT-WARNING` 日志，把它对应到本讲 4.1.4 表格里的某个阶段（例如看到 disassembly 相关日志对应步骤 9、看到 reorder 相关日志对应步骤 16）。
4. 末尾找到 `Final Time report`，把耗时最高的 3 个阶段写下来。
5. **串起来**：写一段话，用「输入二进制 →（发现）→ 函数对象 →（反汇编）→ 指令 →（CFG）→ 基本块与边 →（profile）→ 带计数的 CFG →（优化）→ 重排后的 CFG →（发射+链接）→ 最终地址 →（重写）→ 输出二进制」这条主线，把你日志里观察到的关键事件穿成一串。

**预期结果**：你能把一条命令的日志输出，对回到 `run()` 的阶段序列上，从而真正「看懂」BOLT 在干什么，而不是面对一堆日志发懵。

> 待本地验证：日志的具体文本随 BOLT 版本和二进制而变；若无法运行，可退化为纯阅读实践——只做步骤 5，基于本讲源码梳理写出主线即可。

## 6. 本讲小结

- `RewriteInstance::run()` 是 ELF 重写的总调度函数，把输入二进制加工成输出二进制，分**发现 → profile/调试预处理 → 反汇编+CFG → 优化 → 发射链接 → 落盘**几大块，共 20 个主干阶段。
- 阶段之间是严格的顺序依赖；返回 `Error` 的阶段失败会立刻终止整个 `run()`。
- **四种模式共用一条管线**：正常模式走全程；`AggregateOnly`（perf2bolt）在 `processProfileData` 内 `exit(0)`；`DiffOnly`（boltdiff）和 `BinaryAnalysisMode`（binary-analysis）在 `run()` 主体第 831/834 行 `return`。前 14 个阶段是几乎所有模式的公共前置。
- 每个阶段开头放一个 `NamedRegionTimer` RAII 对象做计时，靠 `-time-rewrite`（组 `rewrite`）启用；CFG 构建另用 `-time-build`（组 `buildfuncs`），想看全报告两个都要开。
- 计时报告在程序退出时打印（`AggregateOnly` 模式下在 `processProfileData` 里显式 `TimerGroup::printAll`）。
- 本讲是单元 3 的总纲，后续 u3-l2/u3-l3/u3-l4 把「发现」「反汇编+CFG」「重定位+跳转表」三段分别展开。

## 7. 下一步学习建议

- **下一步读 u3-l2**（二进制发现与特殊 section 解析）：深入本讲的 `discoverStorage` / `readSpecialSections` / `discoverFileObjects` 三个阶段，搞清函数是如何从符号表被发现的。
- **再读 u3-l3**（反汇编与 CFG 重建）：深入 `disassembleFunctions` / `buildFunctionsCFG`，重点看间接分支的启发式判定。
- **再读 u3-l4**（重定位与 JumpTable）：理解 `--emit-relocs` 为什么是 BOLT 能移动代码的前提。
- 想提前了解优化阶段，可跳到单元 5 的 u5-l1（pass 框架），那里讲 `runOptimizationPasses` 背后的 `BinaryFunctionPassManager`。
- 建议同时打开 [RewriteInstance.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Rewrite/RewriteInstance.h) 的私有成员函数列表对照阅读——它把 `run()` 调用的所有阶段函数（及更多辅助函数）都声明在一起，是一份很好的「目录」。
