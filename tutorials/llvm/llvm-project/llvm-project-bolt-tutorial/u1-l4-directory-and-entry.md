# 代码目录结构与程序入口

## 1. 本讲目标

本讲是单元 1 的最后一篇。学完之后，你应该能够：

- 拿着一份 BOLT 源码树，立刻知道每个顶层目录（`lib/`、`include/`、`tools/`、`runtime/`、`test/` 等）在做什么，以及后续讲义要去哪里读源码。
- 看懂唯一的程序入口 `tools/driver/llvm-bolt.cpp` 里的 `main()`：为什么同一个可执行文件，被叫作 `perf2bolt`、`llvm-boltdiff`、`llvm-bolt` 时会表现成三个不同的工具。
- 理解 `main()` 打开一个二进制之后，如何按文件格式（ELF 还是 Mach-O）分流到 `RewriteInstance` 或 `MachORewriteInstance`。
- 理解 `BOLT_TARGET` 宏与 `TargetConfig.def` 是如何在构建期生成、在运行期初始化目标后端的。

本讲不深入任何具体优化逻辑，只建立「地图 + 入口」的心智模型。单元 2 起才会真正进入数据结构。

## 2. 前置知识

阅读本讲前，建议你已经读过本单元前三篇：

- **u1-l1**：知道 BOLT 是 post-link、profile-guided 的二进制优化器，输入是 ELF 二进制，核心是「反汇编 → 重建 CFG → 叠加 profile → 重排 → 重生成」。
- **u1-l2**：知道 BOLT 作为 LLVM 子项目构建，支持 `AArch64;X86;RISCV` 三种目标，`ninja bolt` 一次编出所有工具。
- **u1-l3**：知道端到端四步流水线（emit-relocs → perf 采集 → perf2bolt 转 fdata → llvm-bolt 优化）。

此外需要几个最基础的工程常识：

- **argv[0]**：C/C++ 程序的 `main(int argc, char **argv)` 里，`argv[0]` 是「程序被调用时用的名字」，通常就是可执行文件名。同一个文件被不同名字（例如软链接/符号链接）调用时，`argv[0]` 会不同。BOLT 正是利用这一点实现「一个二进制，三种人格」。
- **ELF 与 Mach-O**：两种二进制文件格式。ELF 用于 Linux（BOLT 的主战场），Mach-O 用于 macOS。BOLT 对两者的处理走两条不同的重写实例类。
- **LLVM 后端初始化**：LLVM 的「目标」（X86、AArch64 等）是运行期注册的，必须调用一系列 `LLVMInitializeXxxTarget()` 函数后才能反汇编/汇编这些架构的代码。

## 3. 本讲源码地图

本讲只围绕「目录 + 入口」，涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [tools/driver/llvm-bolt.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp) | 整个 BOLT **唯一的 `main()`** 所在地，负责模式分发与二进制分流。 |
| [tools/driver/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/CMakeLists.txt) | 声明 `llvm-bolt` 目标，并创建 `perf2bolt`、`llvm-boltdiff` 两个符号链接。 |
| [CMakeLists.txt](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt) | 顶层构建脚本：登记子项目身份、计算目标架构交集、生成 `TargetConfig.def`、构建运行时库。 |
| [include/bolt/Core/TargetConfig.def.in](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/TargetConfig.def.in) | `TargetConfig.def` 的模板，被 `BOLT_TARGET` 宏展开以初始化目标。 |
| [include/bolt/Rewrite/RewriteInstance.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Rewrite/RewriteInstance.h) | ELF 重写实例的接口（`create` / `run`）。 |
| [include/bolt/Rewrite/MachORewriteInstance.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Rewrite/MachORewriteInstance.h) | Mach-O 重写实例的接口（`create` / `run`）。 |
| [README.md](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md) | 项目说明（输入要求、安装、用法）。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先看目录地图（4.1），再看 `main()` 的三种模式分发（4.2），最后看目标初始化与二进制分流（4.3）。

### 4.1 目录结构与职责速查

#### 4.1.1 概念说明

阅读一个大型 C++ 项目，第一步永远是建立「目录 → 职责」的映射。BOLT 是 LLVM monorepo 的一个子项目（顶层是 `llvm-project/`，BOLT 在 `llvm-project/bolt/`），它内部又自成一个相对独立的小项目：有自己的 `CMakeLists.txt`、自己的 `include/` + `lib/` 分层、自己的工具、测试和运行时。

理解这一层有两个好处：

1. **后续读源码时能直奔目标**：比如讲函数重排算法，你就知道去 `lib/Passes/`；讲 profile 解析，就去 `lib/Profile/`。
2. **理解它的分层哲学**：BOLT 把「目标无关的框架」（`lib/Rewrite`、`lib/Core`）和「目标相关的后端」（`lib/Target/X86|AArch64|RISCV`）分得很干净，这是它能同时支持多架构的关键，也是单元 7 要展开的内容。

#### 4.1.2 核心流程

BOLT 源码树的顶层目录可以这样分类记忆：

```
bolt/
├── CMakeLists.txt     构建入口（顶层脚本）
├── README.md          项目说明
├── include/           公共头文件（按子系统分子目录）
├── lib/               实现代码（按子系统分子目录）
├── tools/             可执行工具（每个工具一个子目录）
├── runtime/           freestanding 运行时库源码（插桩/大页）
├── test/              lit 端到端测试（按架构分类）
├── unittests/         GTest 单元测试
├── docs/              文档
├── cmake/             自定义 CMake 模块（如 AddBOLT.cmake）
└── utils/             杂项辅助脚本（docker 等）
```

其中 `include/` 与 `lib/` 共享同一套子系统划分，下面这张「职责速查表」是贯穿整本手册的索引：

| 子系统 | `lib/` 下目录 | 主要职责 | 典型内容（后续讲义会精读） |
| --- | --- | --- | --- |
| **Core** | `lib/Core` | 核心数据结构与发射 | `BinaryContext`、`BinaryFunction`、`BinaryBasicBlock`、`BinarySection`、`Relocation`、`JumpTable`、`MCPlus`、`BinaryEmitter` |
| **Passes** | `lib/Passes` | 各类优化 pass | `ReorderAlgorithm`、`SplitFunctions`、`Inliner`、`FrameOptimizer` 等（单元 5/6） |
| **Profile** | `lib/Profile` | profile 读取与聚合 | `DataReader`、`DataAggregator`、`BoltAddressTranslation`、`Heatmap`（单元 4） |
| **Rewrite** | `lib/Rewrite` | 二进制重写主管线 | `RewriteInstance`、`DWARFRewriter`、`MetadataManager`、`JITLinkLinker`（单元 3/7/8） |
| **RuntimeLibs** | `lib/RuntimeLibs` | 运行时库注入 | `InstrumentationRuntimeLibrary`、`HugifyRuntimeLibrary`（单元 8） |
| **Target** | `lib/Target/{X86,AArch64,RISCV}` | 目标后端 | 每个架构一套 `MCPlusBuilder.cpp` + `MCSymbolizer.cpp`（单元 7） |
| **Utils** | `lib/Utils` | 通用工具 | `CommandLineOpts`（所有命令行选项的集中定义） |

`tools/` 下则是编出来的可执行文件，每个一个子目录：

| 工具目录 | 产出 | 用途 |
| --- | --- | --- |
| `tools/driver/` | `llvm-bolt` | **主二进制**，凭 `argv[0]` 兼任 `perf2bolt` / `llvm-boltdiff` |
| `tools/merge-fdata/` | `merge-fdata` | 合并多份 fdata |
| `tools/heatmap/` | `llvm-bolt-heatmap` | 热点地图 |
| `tools/bat-dump/` | `llvm-bat-dump` | 打印 BAT section |
| `tools/binary-analysis/` | `llvm-bolt-binary-analysis` | 二进制结构分析 |
| `tools/llvm-bolt-fuzzer/` | fuzzer | 模糊测试 |

> 记住一句话：**真正意义上的「程序入口」只有 `tools/driver/llvm-bolt.cpp` 一个文件**，其它工具目录都是独立的、各自有自己 `main()` 的小程序。

#### 4.1.3 源码精读

顶层 `CMakeLists.txt` 用 `add_subdirectory` 把这些目录串起来。可以看到 `lib/` 与 `tools/` 是无条件加入构建的，而 `unittests/`、`test/`、`docs/` 是有条件加入的：

[ bolt/CMakeLists.txt:L198-L207 ](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L198-L207) —— 这段把 `lib` 和 `tools` 加入构建；`unittests` 与 `test` 仅在 `BOLT_INCLUDE_TESTS` 打开时才加入（而后者又依赖 `clang` 与 `lld` 已启用）。

顶层 `CMakeLists.txt` 的第一行实质性内容是登记身份：

[ bolt/CMakeLists.txt:L10 ](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L10) —— `set(LLVM_SUBPROJECT_TITLE "BOLT")`。如 u1-l2 所述，它只是给 IDE 分组用，不是构建开关。

#### 4.1.4 代码实践

**实践目标**：用一次目录遍历，把「职责速查表」内化为肌肉记忆。

**操作步骤**：

1. 在 `bolt/` 根目录下，分别进入 `lib/Core`、`lib/Passes`、`lib/Profile`、`lib/Rewrite`，看一眼文件名清单（例如 `ls lib/Core`）。
2. 对照上表，给每个目录挑一个你猜得到含义的文件名（比如 `lib/Core/BinaryFunction.cpp`），不必打开，只记名字。
3. 进入 `tools/`，确认 `driver/` 是唯一含 `main()` 的「主」工具目录。

**需要观察的现象**：

- `lib/Passes` 是文件最多的目录（约 50 个），印证「优化 pass 是 BOLT 价值的核心、也是代码量的大头」。
- `lib/Target` 下只有三个架构子目录 `X86/AArch64/RISCV`，每个都含一个 `*MCPlusBuilder.cpp`，印证「后端按架构隔离」。

**预期结果**：你能不看本讲，凭目录名说出 `lib/Rewrite` 与 `lib/Passes` 的区别（前者是「读-优化-写」的框架与编排，后者是「具体做哪一项优化」）。

#### 4.1.5 小练习与答案

**练习 1**：BOLT 的命令行选项定义（如 `-reorder-blocks`）分散在各 pass 文件里，还是集中在一处？如果集中，在哪个目录？

> **答案**：大量「全局/通用」选项集中在 `lib/Utils/CommandLineOpts.cpp`（及对应头 `include/bolt/Utils/CommandLineOpts.h`）。各 pass 也会在本地定义自己的专属选项，但跨模块共用的分类（`BoltCategory`、`BoltOptCategory` 等）和通用开关放在 Utils。

**练习 2**：为什么 `lib/Target` 下没有「通用」的后端文件，而是严格按 `X86/AArch64/RISCV` 分目录？

> **答案**：因为目标相关逻辑（指令判定、分支范围、放松策略）天然与具体架构绑定。把通用接口抽到 `include/bolt/Core/MCPlusBuilder.h`（目标无关），把实现塞进各架构目录，是「公共接口 + 特化实现」的经典分层（单元 7 详讲）。

### 4.2 main() 的三种模式分发

#### 4.2.1 概念说明

一个非常实用的工程问题：BOLT 其实内含三套功能——

- **优化器（bolt）**：读二进制 + profile，输出优化后的二进制。
- **profile 聚合器（perf2bolt）**：读 `perf.data`，聚合输出 fdata。
- **二进制差异对比器（llvm-boltdiff）**：比较两个二进制 + 两份 profile。

这三套功能要复用大量相同的代码（解析 ELF、读符号表、反汇编……）。如果把它们做成三个独立可执行文件，会有三份几乎一样大的二进制；BOLT 的选择是——**编出一份 `llvm-bolt`，然后用符号链接改个名字**，运行时靠 `argv[0]`（即「我被叫什么名字」）来决定走哪条分支。

这就是「一个二进制，三种人格（personality）」的设计。

#### 4.2.2 核心流程

分发逻辑的伪代码：

```
ToolName = argv[0] 取文件名部分
if ToolName 以 "perf2bolt" 开头:     → perf2boltMode()   # 设置 AggregateOnly, ShowDensity
else if ToolName 以 "llvm-boltdiff" 开头: → boltDiffMode()  # 设置 DiffOnly
else:                                 → boltMode()        # 普通优化模式
```

三个模式函数各自做三件事：

1. 用 `cl::HideUnrelatedOptions` 隐藏与本模式无关的命令行选项分类（所以 `perf2bolt -help` 看到的选项和 `llvm-bolt -help` 不同）。
2. 用 `cl::ParseCommandLineOptions` 解析参数，并做必填项校验（如 `-o` 是否给出）。
3. 设置一两个**全局开关**（`opts::AggregateOnly`、`opts::DiffOnly` 等），让后续共同的代码路径据此分叉。

关键点是：**三个模式函数本身不真正干活，它们只是「选择并配置」**；真正打开二进制、跑重写管线的代码是后面 `main()` 里共享的同一段。

#### 4.2.3 源码精读

先看符号链接是怎么在构建期造出来的：

[ bolt/tools/driver/CMakeLists.txt:L30-L31 ](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/CMakeLists.txt#L30-L31) —— `add_bolt_tool_symlink(perf2bolt llvm-bolt)` 与 `add_bolt_tool_symlink(llvm-boltdiff llvm-bolt)`：这两个调用让构建产物里多出两个指向 `llvm-bolt` 的符号链接。这就是「同名源码、异名入口」的物理基础。

再看运行期的分发：

[tools/driver/llvm-bolt.cpp:L187-L192](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp#L187-L192) —— `main()` 取 `argv[0]` 的文件名，用 `starts_with` 判断走哪个模式。注意它取的是文件名部分（`llvm::sys::path::filename`），所以无论符号链接放在哪个目录、叫什么后缀，只要名字前缀对得上即可。

三个模式函数对全局开关的赋值是「分流」的关键：

[tools/driver/llvm-bolt.cpp:L105-L122](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp#L105-L122) —— `perf2boltMode()`，末尾 `opts::AggregateOnly = true; opts::ShowDensity = true;`。

[tools/driver/llvm-bolt.cpp:L124-L148](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp#L124-L148) —— `boltDiffMode()`，末尾 `opts::DiffOnly = true;`。

[tools/driver/llvm-bolt.cpp:L150-L163](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp#L150-L163) —— `boltMode()`，普通优化模式，不设这两个聚合/对比开关。

#### 4.2.4 代码实践

**实践目标**：亲手验证「argv[0] 决定人格」，并理解三模式只配置不干活。

**操作步骤**：

1. 在构建产物目录（`build/bin/`）确认存在三个名字指向同一文件：
   ```bash
   ls -l build/bin/llvm-bolt build/bin/perf2bolt build/bin/llvm-boltdiff
   ```
   预期看到 `perf2bolt` 和 `llvm-boltdiff` 是符号链接（或同一 inode 的硬链接）。
2. 分别跑 `--version`（或 `--help`）对比：
   ```bash
   build/bin/llvm-bolt --version
   build/bin/perf2bolt --help 2>&1 | head
   build/bin/llvm-boltdiff --help 2>&1 | head
   ```
3. 阅读 [tools/driver/llvm-bolt.cpp:L187-L192](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp#L187-L192)，确认三个分支只是调用不同的 `xxxMode()` 函数。

**需要观察的现象**：

- `perf2bolt --help` 与 `llvm-bolt --help` 列出的选项分类不同（前者围绕 `AggregatorCategory`，后者围绕 `BoltCategory/BoltOptCategory` 等）。这正是 `cl::HideUnrelatedOptions` 的效果。
- 三个二进制其实是同一份机器码，只是入口名字不同。

**预期结果**：你能用一句话解释「为什么 `perf2bolt` 和 `llvm-bolt` 是同一个文件却表现不同」——因为 `main()` 读 `argv[0]` 并据此走不同分支。

> 如果当前没有可运行的构建产物，本步骤标注「待本地验证」，可改为纯源码阅读：直接对照上面三处源码链接理解。

#### 4.2.5 小练习与答案

**练习 1**：如果你把 `llvm-bolt` 复制（不是软链接）一份并命名为 `myperf2bolt`，再用它跑，会发生什么？

> **答案**：`main()` 用 `starts_with("perf2bolt")` 判断，`myperf2bolt` 不匹配该前缀，会落到 `else` 分支按普通 `boltMode()` 处理。也就是说，**决定人格的是名字前缀，不是文件实体**。要让复制版也进入聚合模式，名字得以 `perf2bolt` 开头（如 `perf2bolt2`）。

**练习 2**：为什么三个模式函数都调用 `cl::HideUnrelatedOptions`？

> **答案**：因为三个模式共用同一个二进制、同一套已注册的全部命令行选项。若不隐藏无关选项，`perf2bolt` 的帮助里就会出现一堆只在优化模式才有意义的选项，既误导用户也容易引发歧义。隐藏后，每个名字下的帮助只展示与该人格相关的分类。

### 4.3 目标初始化宏 BOLT_TARGET 与 createBinary 后的分流

#### 4.3.1 概念说明

模式分发之后，`main()` 还要解决两个问题：

1. **初始化目标后端**：BOLT 要反汇编/汇编 X86、AArch64、RISCV 的代码，必须先把对应 LLVM 后端「注册」进运行期。但到底注册哪几个架构，是构建期决定的（取决于 `LLVM_TARGETS_TO_BUILD` 与 BOLT 支持架构的交集）。BOLT 用一个宏 `BOLT_TARGET` + 一个构建期生成的 `.def` 文件，把「构建期选择」和「运行期初始化」干净地连起来。

2. **按二进制格式分流**：BOLT 主要吃 ELF（Linux），但也支持 Mach-O（macOS）。`main()` 用 LLVM 的 `createBinary` 打开文件，拿到一个通用的 `Binary`，再用 `dyn_cast` 判断它到底是 ELF 还是 Mach-O，分别交给 `RewriteInstance`（ELF）或 `MachORewriteInstance`（Mach-O）。

这两个机制共同决定了「这段代码能处理哪些架构、哪些格式的二进制」。

#### 4.3.2 核心流程

**目标初始化**的生成与展开链路：

```
构建期（CMake）:
  CMakeLists.txt 计算 BOLT_TARGETS_TO_BUILD = {AArch64,X86,RISCV} ∩ LLVM_TARGETS_TO_BUILD
  → configure_file(TargetConfig.def.in → TargetConfig.def)
     把每个目标展开成一行 BOLT_TARGET(TargetName)

运行期（main 启动）:
  #define BOLT_TARGET(target)  LLVMInitialize##target##TargetInfo/TargetMC/AsmParser/Disassembler/Target/AsmPrinter
  #include "bolt/Core/TargetConfig.def"      ← 每个目标展开成一组初始化调用
```

**二进制分流**的伪代码：

```
if 不是 DiffOnly 模式:                      # 优化 或 perf2bolt 聚合
    binary = createBinary(InputFilename)
    if dyn_cast<ELFObjectFileBase>(binary) 成功:
        RI = RewriteInstance::create(elf, argc, argv, ToolPath, ...)
        RI.setProfile(...)                  # 若提供了 -data / -perfdata
        RI.run()                            # ← ELF 主路径：读、优化、重写
    else if dyn_cast<MachOObjectFile>(binary) 成功:
        MachORI = MachORewriteInstance::create(macho, ToolPath)
        MachORI.setProfile(...)
        MachORI.run()                       # ← Mach-O 路径
    else:
        报错 invalid_file_type
else:                                        # boltdiff 对比模式
    打开两个 ELF → 两个 RewriteInstance → 各自 run() → RI1.compare(RI2)
```

#### 4.3.3 源码精读

先看构建期如何决定支持哪些目标。顶层 `CMakeLists.txt` 把「BOLT 支持的全部目标」与「LLVM 实际构建的目标」求交集：

[ bolt/CMakeLists.txt:L72-L92 ](https://github.com/llvm-project/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L72-L92) —— `BOLT_TARGETS_TO_BUILD_all` 写死为 `"AArch64;X86;RISCV"`；逐个检查它是否也在 `LLVM_TARGETS_TO_BUILD` 里，求交集得到默认值；若最终为空则 `FATAL_ERROR`。这正是 u1-l2 提到的「两者不匹配会配置报错」的代码出处。

然后把每个目标渲染进 `TargetConfig.def`：

[ bolt/CMakeLists.txt:L218-L224 ](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L218-L224) —— 用 `foreach` 把每个目标拼成一行 `BOLT_TARGET(TargetName)`，再 `configure_file` 把 `TargetConfig.def.in` 里的 `@BOLT_ENUM_TARGETS@` 占位符替换掉，生成最终的 `TargetConfig.def`。

模板文件本身只是一个「被宏展开」的骨架：

[include/bolt/Core/TargetConfig.def.in:L17-L23](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/TargetConfig.def.in#L17-L23) —— 它要求外层先 `#define BOLT_TARGET(...)`，再 `#include` 本文件，于是 `@BOLT_ENUM_TARGETS@` 处的那几行 `BOLT_TARGET(X86)` 等就会被外层宏展开。

运行期，`main()` 正是这么做的：

[tools/driver/llvm-bolt.cpp:L175-L183](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp#L175-L183) —— `#define BOLT_TARGET(target)` 把宏定义为针对该 target 的六类初始化调用（TargetInfo/TargetMC/AsmParser/Disassembler/Target/AsmPrinter），紧接着 `#include "bolt/Core/TargetConfig.def"`。include 之后，每个构建期选中的目标都展开成一串 `LLVMInitializeXxxTarget...()` 调用，完成运行期注册——之后 BOLT 才能反汇编/汇编这些架构。

目标初始化完成后，`main()` 打开二进制并分流。先看入口与文件存在性检查：

[tools/driver/llvm-bolt.cpp:L172-L195](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp#L172-L195) —— `getMainExecutable` 解析 `argv[0]` 得到 `ToolPath`（供运行时库定位自身）；随后是上面讲过的模式分发；最后检查输入文件存在。

ELF 主路径——`createBinary` 后 `dyn_cast` 到 `ELFObjectFileBase`，创建 `RewriteInstance` 并 `run()`：

[tools/driver/llvm-bolt.cpp:L216-L250](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp#L216-L250) —— `createBinary` 打开文件；`dyn_cast<ELFObjectFileBase>` 命中则 `RewriteInstance::create(...)`，再用 `RI.setProfile(...)` 叠加 profile（`-perfdata` / `-data`），最后 `RI.run()` 执行「读-优化-写」。注意 `run()` 返回 `Error`，出错会经 `report_error` 打印并退出。

Mach-O 路径——`dyn_cast` 到 `MachOObjectFile`，交给 `MachORewriteInstance`：

[tools/driver/llvm-bolt.cpp:L251-L264](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp#L251-L264) —— 命中 `MachOObjectFile` 则 `MachORewriteInstance::create(O, ToolPath)`，`setProfile` 后 `MachORI.run()`。注意两个细节：(1) 两个 `run()` 签名不同，ELF 的返回 `Error`、Mach-O 的返回 `void`；(2) 都不是「报错就退出」的同一种写法。其它格式直接 `report_error(..., invalid_file_type)`。

最后是 `boltdiff` 对比路径（`DiffOnly` 为真时）：

[tools/driver/llvm-bolt.cpp:L269-L312](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp#L269-L312) —— 打开两个二进制、创建两个 `RewriteInstance`、各自 `setProfile` 与 `run()`，最后 `RI1.compare(RI2)` 做对比。

两个重写实例的对外接口可以对照头文件确认：

[include/bolt/Rewrite/RewriteInstance.h:L54-L64](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Rewrite/RewriteInstance.h#L54-L64) —— ELF 侧 `create(...)` 接收 `ELFObjectFileBase*` 及 argc/argv/ToolPath/日志流；`Error run()` 执行全部「读、优化、重写」。

[ bolt/include/bolt/Rewrite/MachORewriteInstance.h:L71-L82 ](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Rewrite/MachORewriteInstance.h#L71-L82) —— Mach-O 侧 `create(...)` 只接收 `MachOObjectFile*` 与 `ToolPath`；`void run()` 不返回 `Error`。

#### 4.3.4 代码实践

**实践目标**：画出 `main()` 从「打开二进制」到「调用 `run()`」的完整调用流程图，并标注分流条件。这是本讲的核心实践任务。

**操作步骤**：

1. 打开 [tools/driver/llvm-bolt.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp)，定位 `main()`（L165 起）。
2. 用纸笔或文本，按执行顺序把以下节点串起来：`main` → 信号/关闭初始化 → `getMainExecutable` → `BOLT_TARGET` 宏展开初始化目标 → 模式分发（L187-L192）→ 文件存在检查 → 日志流初始化 → `if (!DiffOnly)` 分支 → `createBinary` → `dyn_cast<ELFObjectFileBase>` / `dyn_cast<MachOObjectFile>` → `RewriteInstance::create` 或 `MachORewriteInstance::create` → `setProfile` → `run()`。
3. 在图上用不同颜色/记号标出三个「分流点」：① 模式分发（按 `argv[0]`）；② `DiffOnly` 与否；③ 二进制格式（ELF/Mach-O）。

**需要观察的现象**：

- `RewriteInstance::run()` 与 `MachORewriteInstance::run()` 处于「互斥分支」上，永远不会同时执行。
- `setProfile` 在 `run()` **之前**调用——profile 必须先挂上去，`run()` 内部才能用它做重排（呼应 u1-l3：没有 profile 就无法优化）。

**预期结果**：你得到一张类似下面的流程图（这是本任务要求画出的图）：

```
main(argv)
  │
  ├─ sys::PrintStackTraceOnErrorSignal / PrettyStackTraceProgram / llvm_shutdown_obj   (信号与退出清理)
  ├─ getMainExecutable(argv[0]) ──► ToolPath
  ├─ #define BOLT_TARGET + #include TargetConfig.def ──► 初始化各目标后端
  │      (TargetInfo/TargetMC/AsmParser/Disassembler/Target/AsmPrinter)
  ├─ ToolName = argv[0]
  │
  ├─【分流点① 模式】按 ToolName 文件名分发:
  │      starts_with("perf2bolt")     → perf2boltMode()   (置 AggregateOnly=ShowDensity=true)
  │      starts_with("llvm-boltdiff") → boltDiffMode()    (置 DiffOnly=true)
  │      否则                          → boltMode()
  │
  ├─ 检查 InputFilename 存在 / 初始化日志流 BOLTJournalOut/Err
  │
  ├─【分流点② DiffOnly?】
  │
  ├─ if (!DiffOnly):                        ── 优化 / perf2bolt 聚合 路径
  │     ├─ createBinary(InputFilename) ──► Binary
  │     ├─【分流点③ 格式】
  │     │     ├─ dyn_cast<ELFObjectFileBase> 命中:
  │     │     │     RewriteInstance::create(e, argc, argv, ToolPath, out, err)
  │     │     │       → setProfile(-perfdata/-data)  → RI.run()        ★ ELF 主路径
  │     │     │
  │     │     └─ dyn_cast<MachOObjectFile> 命中:
  │     │           MachORewriteInstance::create(O, ToolPath)
  │     │             → setProfile(-data) → MachORI.run()               ★ Mach-O 路径
  │     │
  │     └─ 都不命中 → report_error(invalid_file_type)
  │     return EXIT_SUCCESS
  │
  └─ else (DiffOnly):                      ── boltdiff 对比 路径
        ├─ createBinary(InputFilename)  + createBinary(InputFilename2)
        ├─ 两个 ELF → RewriteInstance::create × 2，各 setProfile
        ├─ RI1.run()  (打印 "Analyzing binary 1")
        ├─ RI2.run()  (打印 "Analyzing binary 2")
        └─ RI1.compare(RI2)
        return EXIT_SUCCESS
```

> 这张图就是「打开二进制 → 调用 `run()`」的全貌。后续单元 3 会放大 `RewriteInstance::run()` 内部那一段，讲清楚它到底怎么「读-优化-写」。

#### 4.3.5 小练习与答案

**练习 1**：如果只给 BOLT 构建了 `X86` 一个目标（`-DLLVM_TARGETS_TO_BUILD="X86"`），然后用它处理一个 AArch64 的 ELF，会发生什么？错误是在构建期还是运行期暴露？

> **答案**：构建期不会报错（X86 确实在 BOLT 支持列表里且与 LLVM 目标相交不为空）。但运行期 `TargetConfig.def` 只展开了 `BOLT_TARGET(X86)`，AArch64 后端没有被 `LLVMInitialize` 注册，因此 `RewriteInstance::create` 在为 AArch64 二进制初始化反汇编器时会失败/报错。结论：**支持哪些架构由构建期决定，但「不支持某架构」的后果通常在运行期才体现**。

**练习 2**：`RewriteInstance::run()` 返回 `Error`，而 `MachORewriteInstance::run()` 返回 `void`。这种不一致会带来什么影响？

> **答案**：ELF 路径可以用 `if (Error E = RI.run()) report_error(...)` 把错误统一收敛到入口的报错逻辑；Mach-O 路径的 `run()` 内部只能自行处理/直接退出，调用方（`main()`）无法用同样的 `Error` 机制接管。这反映了 ELF 是主战场、错误处理更完善，Mach-O 是较简化的二级支持（单元 7/8 主要围绕 ELF 的 `RewriteInstance` 展开）。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「入口追踪」小任务：

1. **目录层**：在 `bolt/` 下新建一份只有你自己看的「速查便签」，用 6 行以内写清 `lib/Core`、`lib/Passes`、`lib/Profile`、`lib/Rewrite`、`lib/Target`、`tools/driver` 各自的一句话职责。
2. **入口层**：阅读 [tools/driver/llvm-bolt.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp) 的 `main()`，回答三个问题：
   - 三种模式分别由哪一行判定（给出行号）？
   - `perf2bolt` 模式设置了哪两个全局开关，使后续共用代码走「聚合而非优化」？
   - ELF 与 Mach-O 分别由哪个类的 `run()` 收尾？
3. **生成层**：在 `build/` 下找到构建生成的 `include/bolt/Core/TargetConfig.def`（注意不是 `.def.in`），打开它，数一下里面有几行 `BOLT_TARGET(...)`，与你 `LLVM_TARGETS_TO_BUILD` 的设置对照，解释为什么是这些目标。
   - 若没有可用构建目录，本步标注「待本地验证」，改为阅读 `CMakeLists.txt` 的 L72–L92 与 L218–L224，推断出生成的 `.def` 内容。

完成上述三步后，你应该能合上文档、对着 `main()` 把「从 `argv[0]` 到 `run()`」的整条链路完整复述一遍——这正是进入单元 2（核心数据结构）和单元 3（`RewriteInstance::run()` 主链路）之前的必备地图。

## 6. 本讲小结

- BOLT 源码树按「子系统」分层：`lib/Core`（数据结构）、`lib/Passes`（优化）、`lib/Profile`（profile）、`lib/Rewrite`（重写管线）、`lib/Target`（后端）、`lib/Utils`（选项/工具）；`include/` 与之对应。
- 真正的程序入口只有一个：`tools/driver/llvm-bolt.cpp` 的 `main()`；其余 `tools/` 子目录都是各自独立的轻量小工具。
- `perf2bolt` 与 `llvm-boltdiff` 是 `llvm-bolt` 的符号链接，`main()` 靠 `argv[0]` 的名字前缀分发到 `perf2boltMode` / `boltDiffMode` / `boltMode`——「一个二进制，三种人格」。
- 三个模式函数只做「配置」（隐藏无关选项、校验参数、置全局开关如 `AggregateOnly`/`DiffOnly`），不真正干活；真正打开二进制并跑管线的代码是共享的。
- `BOLT_TARGET` 宏 + 构建期生成的 `TargetConfig.def`，把「构建期选择的目标架构」转化为运行期的 `LLVMInitialize*` 注册调用。
- `createBinary` 之后，`main()` 用 `dyn_cast` 按格式分流：ELF → `RewriteInstance::run()`（返回 `Error`），Mach-O → `MachORewriteInstance::run()`（返回 `void`），其余报 `invalid_file_type`。

## 7. 下一步学习建议

本讲只建立了「地图 + 入口」。建议接下来：

- **单元 2（u2-l1 起）**：进入 `lib/Core`，从 `BinaryContext` 开始读核心数据结构——它是后续所有源码阅读的基础。
- **单元 3（u3-l1）**：放大本讲图里那个 `RI.run()` 节点，逐阶段拆解 `RewriteInstance::run()` 的「发现 → 反汇编 → CFG → 优化 → 发射 → 重写文件」管线。
- 如果你想立刻看到「入口→真实优化」的端到端效果，可以回头做 u1-l3 的四步流水线实践，把本讲的入口知识与实际命令对照起来。

阅读时记住一条主线：**`main()` 负责把控制权交给 `RewriteInstance::run()`，之后几乎所有有意思的事都发生在 `run()` 内部**。本讲到此为止，单元 2/3 再深入。
