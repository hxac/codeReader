# linkerMain：选项解析、配置与文件加载

## 1. 本讲目标

上一讲我们看到 [`elf::link()`](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L118-L140) 只负责"搭舞台"：在堆上创建 `Ctx`、初始化 `ErrorHandler`、装配 `LinkerScript` 与 `SymbolTable`，然后把命令行参数交给 `ctx.driver.linkerMain(args)`。真正"拉开大幕、开始读命令行、读输入文件"的工作，全部发生在 `LinkerDriver::linkerMain` 里。

本讲学完后，你应该能够：

- 说出 `linkerMain` 的整体阶段顺序：**parse → readConfigs → createFiles → inferMachineType → setConfigs → checkOptions → link**，并解释为什么是这个顺序。
- 看懂 `Options.td` 如何用 LLVM 的表格驱动（TableGen）方式声明命令行选项，以及 `ELFOptTable` 如何把 `argv` 解析成 `InputArgList`。
- 解释 `LoadJob` 这层"延迟加载"抽象：为什么 `addFile` 只是把文件分类记录成一个个"作业"，而真正的解析放到 `loadFiles()` 里并行展开。
- 在真实源码中定位 `file_magic` 到 `LoadJob::Kind` 的映射，理解 LLD 如何区分归档、目标文件、bitcode、共享库与二进制文件。

本讲只走到"准备好一个装满 `InputFile` 的 `files` 列表"为止；把这些文件真正解析成符号和段、走完整条链接流水线，是下一讲 `link<ELFT>` 的任务。

## 2. 前置知识

在进入源码前，先建立几个直觉。

### 2.1 命令行选项的"表格驱动"声明

很多 C++ 程序用 `if (arg == "--foo")` 一个个手写选项解析。LLVM 的工具链不用这种方式，而是用一个叫 **OptTable** 的框架：你在一个 `.td`（TableGen 描述）文件里声明每个选项的名字、前缀（`-` 还是 `--`）、是否带参数、帮助文本，构建时 TableGen 会生成一张 `Options.inc` 大表，运行时 `OptTable::ParseArgs` 查这张表完成解析。这样做的好处是：选项声明集中、`--help` 自动生成、还能自动给拼错的选项提示"did you mean"。

LLD 的 ELF 后端用的就是这个框架，封装成 `ELFOptTable`。每个选项在 `.td` 里有个名字（如 `gc_sections`），生成出来的枚举常量是 `OPT_gc_sections`，源码里就用这个常量去取参数。

### 2.2 链接器为什么"延迟加载"文件

一个大型项目可能有上千个输入文件，其中很多是静态库（archive，`.a`）。传统链接器往往一边读命令行一边立刻解析每个文件，串行执行、难以并行。LLD 采用一个更聪明的策略：先把每个输入文件**分类记录**（它是归档吗？是 bitcode 吗？），但不立刻解析；等所有输入都登记完毕，再统一用 `parallelFor` **并行解析**。这样做有两个好处：一是并行加速，二是 LLD 独有的归档处理算法（记住全部符号、按需即时提取）需要在全局视角下工作。

用来"登记但不立刻干活的"数据结构就是 `LoadJob`。本讲会反复提到它。

### 2.3 机器类型推断

ELF 是一种与具体 CPU 架构绑定的格式（x86-64、AArch64、RISC-V……）。LLD 内部用模板 `link<ELFT>` 把"32/64 位"和"大/小端"四种组合各实例化一份代码。到底走哪一份？需要先知道目标机的 `ELFKind` 和 `emachine`。这两者既可以由 `-m` 选项显式指定，也可以从第一个输入的目标文件里**推断**出来——这就是 `inferMachineType` 的工作，也是为什么它必须排在 `createFiles` 之后、`setConfigs` 之前。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`ELF/Driver.cpp`](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp) | `linkerMain`、`createFiles`、`loadFiles`、`addFile`、`inferMachineType`、`readConfigs`、`setConfigs`、`checkOptions` 全部在此 |
| [`ELF/DriverUtils.cpp`](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/DriverUtils.cpp) | `ELFOptTable` 的构造与 `parse` 实现、响应文件展开、`--help` 输出 |
| [`ELF/Driver.h`](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.h) | `ELFOptTable` 类声明、由 `Options.inc` 生成的 `OPT_xxx` 枚举 |
| [`ELF/Options.td`](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Options.td) | 所有命令行选项的 TableGen 声明 |
| [`ELF/Config.h`](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Config.h) | `LoadJob` 结构体与 `LinkerDriver` 类的定义 |
| [`ELF/Target.h`](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Target.h) | `invokeELFT` 宏——根据 `ekind` 选择对应模板实例的"分发器" |

---

## 4. 核心概念与源码讲解

### 4.1 选项解析与配置读取

#### 4.1.1 概念说明

`linkerMain` 是 ELF 链接的"调度中枢"。它做的第一件事不是读文件，而是**解析命令行**。这一步把一串 `const char *`（也就是 `argv`）翻译成一个结构化的 `opt::InputArgList`——之后所有代码都不再面对原始字符串，而是问"用户有没有传 `OPT_o`？它的值是什么？"。

解析完命令行，紧接着是 `readConfigs`：它遍历 `InputArgList`，把每个选项的值写进 `ctx.arg`（即 `Configuration` 结构体，链接全程的"配置账本"）。注意区分两个阶段：

- **`readConfigs`**：只读那些"与目标架构无关、可以立刻确定"的配置，比如 `--gc-sections`、`--shared`、各种 `-z` 选项。它在 `createFiles` **之前**执行，因为加载文件时已经需要用到其中一些配置（例如 `--whole-archive` 的开关、静态/动态模式）。
- **`setConfigs`**：在 `inferMachineType` **之后**执行，用来设置那些"依赖目标架构"的派生配置，比如 32/64 位、大小端、默认入口符号 `_start`、默认输出名 `a.out` 等。

这种拆分是必要的：你无法在知道目标机之前判断 `is64`，也无法在知道 `emachine` 之前给 PPC64 设置 `tocOptimize` 默认值。

#### 4.1.2 核心流程

`linkerMain` 的整体阶段如下（这是本讲最重要的一张图）：

```
linkerMain(argsArr)
  │
  ├─ 1. parser.parse(ctx, args)        // ELFOptTable 解析 argv → InputArgList
  ├─ 2. 提前读取影响诊断的选项         // errorLimit / fatalWarnings / suppressWarnings
  ├─ 3. 处理 --help / -v / --version   // 打印后可能直接 return
  ├─ 4. readConfigs(ctx, args)         // 选项 → ctx.arg（架构无关部分）
  │     checkZOptions(ctx, args)       // 校验 -z 选项组合
  ├─ 5. 初始化 time-trace profiler     // 若开启 --time-trace
  │     ── 进入 TimeTraceScope("ExecuteLinker") ──
  │     6. initLLVM()                  // 注册 LLVM 的 target/asmprinter，为 LTO 准备
  │     7. createFiles(args)           // 遍历 argv，addFile 把输入登记成 LoadJob
  │        if (errCount(ctx)) return;  // ← 检查点
  │     8. inferMachineType()          // 由输入文件推断 ekind/emachine（若未用 -m）
  │     9. setConfigs(ctx, args)       // 设置依赖架构的派生配置
  │    10. checkOptions(ctx)           // 校验选项互斥（如 -shared 与 -pie 不能同用）
  │        if (errCount(ctx)) return;  // ← 检查点
  │    11. invokeELFT(link, args)      // 按 ekind 分发到 link<ELFT>（下一讲主题）
  │     ───────────────────────────────
  └─ 12. waitForLTOCleanup(); 写出 time-trace JSON
```

这里有两个 `if (errCount(ctx)) return;` **检查点**（checkpoint）。回忆上一讲：LLD 的错误处理哲学是"用 `error()` 累积错误，到检查点再统一退出"，这样一次链接能尽可能多地暴露问题。这两个检查点分别保护了"文件加载阶段"和"选项校验阶段"——只有它们都通过，才允许进入昂贵的 `link<ELFT>` 主流水线。

#### 4.1.3 源码精读

先看 `linkerMain` 的全貌：

[ELF/Driver.cpp:633-725](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L633-L725) —— `LinkerDriver::linkerMain` 的完整实现。注意它如何按上面流程图的顺序逐阶段推进，两个 `if (errCount(ctx)) return;` 分别在 `createFiles` 和 `checkOptions` 之后。

第 635 行用 `ELFOptTable` 解析参数：

```cpp
ELFOptTable parser;
opt::InputArgList args = parser.parse(ctx, argsArr.slice(1));
```

注意 `argsArr.slice(1)`：跳过 `argv[0]`（程序名本身），它已经在 `link()` 里被存进 `ctx.arg.progName` 了。

紧接着的几行（638-642 行）会**抢在所有其他处理之前**读取影响诊断行为的选项——`--error-limit`、`--fatal-warnings`、`--no-warnings`。为什么这么急？因为 `Err`/`Warn` 这些诊断流的行为本身就依赖这些配置（上一讲讲过），如果晚读，前面报错时的行为就会不对：

```cpp
ctx.e.errorLimit = args::getInteger(args, OPT_error_limit, 20);
ctx.e.fatalWarnings =
    args.hasFlag(OPT_fatal_warnings, OPT_no_fatal_warnings, false) &&
    !args.hasArg(OPT_no_warnings);
ctx.e.suppressWarnings = args.hasArg(OPT_no_warnings);
```

随后是 `--help` / `-v` / `--version` 的处理（645-692 行）。一个有趣细节：`-v` 单独使用时（没有 `OPT_INPUT`）会打印版本后直接 `return`，这是为了兼容 GNU libtool 的探测脚本（注释 651-663 行有详细说明）。

真正进入链接准备的"重头戏"从第 698 行的 `TimeTraceScope("ExecuteLinker")` 开始，里面依次调用 `initLLVM()`、`createFiles(args)`、`inferMachineType()`、`setConfigs(ctx, args)`、`checkOptions(ctx)`，最后 `invokeELFT(link, args)` 把控制权交给下一讲的 `link<ELFT>`。

**`ELFOptTable::parse` 做了什么？** 它的实现不在 `Driver.cpp`，而在 `DriverUtils.cpp`：

[ELF/DriverUtils.cpp:110-140](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/DriverUtils.cpp#L110-L140) —— `ELFOptTable::parse`。它做了三件关键的事：

1. 先调用 `ParseArgs` 做一次预解析，只为了拿到 `--rsp-quoting`（响应文件的引号风格）。
2. 调用 `cl::ExpandResponseFiles` 把形如 `@args.txt` 的响应文件就地展开（这样 LLD 就支持把超长命令行写进文件再用 `@` 引用），再 `ParseArgs` 一次得到最终结果。
3. 对所有 `OPT_UNKNOWN`（未识别）选项，调用 `findNearest` 算编辑距离，给出"did you mean"提示——这是 `OptTable` 框架送的福利。

`ELFOptTable` 的构造则极简：

[ELF/DriverUtils.cpp:52-53](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/DriverUtils.cpp#L52-L53) —— 构造函数把 TableGen 生成的三张表（字符串表、前缀表、选项信息表 `optInfo`）传给基类 `GenericOptTable`。这三张表都来自 `#include "Options.inc"`（36-50 行），而 `Options.inc` 是构建时由 `Options.td` 生成的。

**`Options.td` 长什么样？** 看几个真实例子：

[ELF/Options.td:364-365](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Options.td#L364-L365) —— `-o` 选项，`JoinedOrSeparate` 表示既可写成 `-oout` 也可写成 `-o out`：

```cpp
def o: JoinedOrSeparate<["-"], "o">, MetaVarName<"<path>">,
  HelpText<"Path to file to write output">;
```

[ELF/Options.td:320-321](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Options.td#L320-L321) —— `-l` 选项，用于搜索库：

```cpp
def library: JoinedOrSeparate<["-"], "l">, MetaVarName<"<libname>">,
  HelpText<"Search for library <libname>">;
```

[ELF/Options.td:283-285](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Options.td#L283-L285) —— `--gc-sections`，用 `multiclass B` 一次同时定义 `--gc-sections` 和 `--no-gc-sections`（这是 LLD 表达"开关对"的惯用法，见 `.td` 文件 32-35 行的 `multiclass B`）：

```cpp
defm gc_sections: B<"gc-sections",
    "Enable garbage collection of unused sections",
    "Disable garbage collection of unused sections (default)">;
```

[ELF/Options.td:233-234](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Options.td#L233-L234) —— `--error-limit`，用 `multiclass EEq` 同时定义 `--error-limit N` 与 `--error-limit=N` 两种写法，并互为别名。

[ELF/Options.td:595-596](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Options.td#L595-L596) —— `-z`，一个"通用扩展槽"，所有 `-z xxx` 都解析成同一个 `OPT_z`，具体 `xxx` 是什么由 `readConfigs` 里的 `hasZOption`/`getZFlag` 二次判断。

声明在 `.td` 里的名字（如 `gc_sections`）会被 TableGen 转成枚举常量 `OPT_gc_sections`：

[ELF/Driver.h:21-33](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.h#L21-L33) —— `ELFOptTable` 类声明，以及由 `Options.inc` 生成的 `OPT_xxx` 枚举。源码各处就是用这些常量向 `InputArgList` 查询选项的。

**`readConfigs` 与 `setConfigs` 的分工**：

[ELF/Driver.cpp:1378-1437](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L1378-L1437)（节选开头）—— `readConfigs` 一开头就把 `--verbose`、各种 `-z muldefs`/`-z memtag-heap`、`--gc-sections` 之类的**架构无关**选项写进 `ctx.arg`。这个函数很长（数百行），但模式单一：每行都是"从 `args` 取一个选项，赋给 `ctx.arg` 的某个字段"。

[ELF/Driver.cpp:2024-2108](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L2024-L2108) —— `setConfigs`。注意它的前两行就揭示了为什么它必须排在 `inferMachineType` 之后：

```cpp
ELFKind k = ctx.arg.ekind;       // 由 -m 或 inferMachineType 填好
uint16_t m = ctx.arg.emachine;
ctx.arg.is64 = (k == ELF64LEKind || k == ELF64BEKind);
ctx.arg.isLE = (k == ELF32LEKind || k == ELF64LEKind);
```

`is64`/`isLE`/`wordsize`/`endianness` 这些派生量都依赖已经确定的目标架构。`setConfigs` 还会设置默认值：若用户没指定入口符号，默认 `_start`（MIPS 用 `__start`）；若没指定 `-o`，默认输出 `a.out`（2086-2091 行）。它还**提前尝试创建输出文件**（2096-2107 行），目的是在跑完漫长的链接（尤其 LTO）之前就发现"输出路径不可写"这种低级错误，免得白等。

#### 4.1.4 代码实践

**实践目标**：把 `--help` 输出、`.td` 声明、源码里使用的 `OPT_xxx` 常量三者对应起来，验证 TableGen 这条链路。

**操作步骤**：

1. 运行 `ld.lld --help > help.txt`，导出完整选项列表（若本机没有 `ld.lld`，可用 `clang -fuse-ld=lld` 间接调用，或参考构建产物）。
2. 在 `help.txt` 里挑三个选项，例如 `--gc-sections`、`--error-limit`、`-o <path>`。
3. 打开 [ELF/Options.td](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Options.td)，分别找到它们的 `def`/`defm`（提示：`--gc-sections` 在 283 行、`--error-limit` 在 233 行、`-o` 在 364 行）。
4. 然后在 `ELF/Driver.cpp` 里搜索对应的 `OPT_gc_sections`、`OPT_error_limit`、`OPT_o`，看它们分别在哪里被消费。

**需要观察的现象**：

- `--help` 里显示的帮助文本，与你 `.td` 中写的 `HelpText<"...">` 完全一致——证明 `--help` 是 TableGen 自动生成的，而非手写。
- `--gc-sections` 这种"开关对"会同时在帮助里出现 `--gc-sections` 和 `--no-gc-sections` 两条，分别对应 `multiclass B` 生成的两个 `def`。

**预期结果**：你能在 `.td` 声明、`--help` 输出、源码中的 `OPT_xxx` 使用点之间建立一一对应。**若本机无法运行 `ld.lld`，可用"`grep OPT_gc_sections ELF/*.cpp` 找到所有使用点"替代，并标注"待本地验证 `--help` 输出"。**

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--error-limit`、`--fatal-warnings` 必须在 `readConfigs` **之前**就抢着读取，而不能放进 `readConfigs` 一起处理？

> **参考答案**：因为它们决定的是 `ErrorHandler` 的行为（最多报多少错、警告是否升级为致命），而 `readConfigs` 本身、以及它之前的 `-v`/`--version`/`--reproduce` 处理过程中都可能触发诊断。若晚读，这些早期诊断的行为就会出错。

**练习 2**：`-z now`、`-z relro`、`-z muldefs` 在 `Options.td` 里只有**一个** `def z`，为什么源码里却能区分它们？

> **参考答案**：`-z` 是一个通用扩展槽，所有 `-z xxx` 都被解析成同一个 `OPT_z`，其值 `xxx` 作为该选项的字符串参数保存。具体语义由 `readConfigs` 里的 `hasZOption(args, "muldefs")`、`getZFlag(args, "now", "lazy", ...)` 等辅助函数再次判断（参见 [ELF/Driver.cpp:450-473](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L450-L473)）。

**练习 3**：`setConfigs` 里有一段"提前创建输出文件"的代码（2096-2107 行）。假如删掉它、改为在 `Writer::run` 里才创建输出文件，会有什么坏处？

> **参考答案**：会浪费用户时间。一个大型 LTO 链接可能跑几十分钟，如果最后才发现"输出路径不可写"，用户相当于白等。提前探测能在 `checkOptions` 检查点就把这种低级错误暴露出来，符合 LLD"尽早失败"的工程哲学。

---

### 4.2 createFiles/addFile 的 LoadJob 分类

#### 4.2.1 概念说明

`createFiles` 的任务是：遍历已经解析好的 `InputArgList`，对每一个输入项（`OPT_INPUT` 即位置参数、`OPT_library` 即 `-l`、链接脚本、`--defsym` 等）做出处理。其中真正涉及"读文件"的，会落到 `addFile`。

但 `addFile` **不立刻解析文件**。它的核心动作是：读取文件的前几个字节，用 `llvm::sys::fs::identify_magic` 判断文件类型（即 `file_magic` 枚举），然后据此构造一个 `LoadJob` 结构体，**塞进 `loadJobs` 队列**就返回。真正的解析被推迟到 `loadFiles()`（下一节）。

`LoadJob::Kind` 有五种取值，正好对应五类输入：

| `file_magic` | `LoadJob::Kind` | 含义 |
|--------------|-----------------|------|
| `elf_relocatable` | `Obj` | 可重定位目标文件（`.o`） |
| `bitcode` | `Bitcode` | LLVM bitcode（`-flto` 产物） |
| `archive` | `Archive` | 静态库（`.a`，可能含多个成员） |
| `elf_shared_object` | `Shared` | 动态共享库（`.so`） |
| （`--format binary`） | `Binary` | 原始二进制（强制） |
| `unknown` | （不建 LoadJob） | 当作链接脚本处理 |

这个分类是后续并行加载、归档展开、bitcode 延迟编译的基础。

#### 4.2.2 核心流程

```
createFiles(args)
  │ SaveAndRestore 把 deferLoad 置为 true   ← 关键：addFile 期间不立刻 loadFiles()
  │
  ├─ for (每个 arg in args):
  │     switch (arg 的 OPT_xxx):
  │       OPT_INPUT     → addFile(path, withLOption=false)
  │       OPT_library   → addLibrary(name)            // -lfoo，先搜索路径再 addFile
  │       OPT_script    → readLinkerScript(...)        // 链接脚本直接当场解析
  │       OPT_defsym    → readDefsym(...)
  │       OPT_whole_archive / OPT_as_needed / OPT_Bstatic / ...  → 改变状态开关
  │       OPT_start_group / OPT_start_lib              → 进/出"组"状态
  │       OPT_push_state / OPT_pop_state               → 保存/恢复状态栈
  │
  └─ loadFiles()           // 队列攒齐了，统一并行展开
     if (files.empty() && 无输入)  → ErrAlways "no input files"
```

`addFile` 内部的判断：

```
addFile(path, withLOption):
  buffer = readFile(path)             // 读全文进内存，得到 MemoryBufferRef
  if --format binary:
      push LoadJob{kind=Binary}
  else:
      magic = identify_magic(buffer)  // 看文件头几个字节
      if magic == unknown:
          readLinkerScript(buffer); return     // 当链接脚本处理，不建 LoadJob
      switch (magic):               // 决定 LoadJob::Kind
        archive            → Archive
        elf_relocatable    → Obj
        bitcode            → Bitcode
        elf_shared_object  → Shared（静态链接下报错并返回）
      push LoadJob{kind, inWholeArchive, inLib, asNeeded, groupId, ...}
  if not deferLoad: loadFiles()       // createFiles 之外（如 addDependentLibrary）才立即加载
```

这里有一个精妙的设计：`deferLoad` 标志。`createFiles` 进入时用 `SaveAndRestore` 把它置为 `true`，于是整个 `createFiles` 期间所有 `addFile` 都只入队、不展开；等循环结束，`createFiles` 显式调用一次 `loadFiles()` 统一展开。而在 `createFiles` 之外（例如后续 `parseFiles` 阶段遇到 `DEPENDENT_LIBRARY` 注释要补拉依赖库），`deferLoad` 是 `false`，此时 `addFile` 会**立刻**调用 `loadFiles()` 展开单个作业。同一个 `addFile` 函数，靠这个标志同时服务两种调用场景。

#### 4.2.3 源码精读

先看 `createFiles` 的开头，注意 `SaveAndRestore`：

[ELF/Driver.cpp:2222-2235](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L2222-L2235) —— `createFiles` 入口。第 2224 行 `SaveAndRestore saveDefer(deferLoad, true)` 是延迟加载的关键：它把 `deferLoad` 临时设为 `true`，函数返回时自动恢复。随后是一个大 `for` 循环，逐个 `arg` 处理。

循环里的几个关键分支：

[ELF/Driver.cpp:2236-2245](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L2236-L2245) —— `OPT_INPUT`（位置参数）调 `addFile`，`OPT_library`（`-l`）调 `addLibrary`。`addLibrary` 会先用 `searchLibrary` 在 `-L` 路径里找库，找到再委托 `addFile`。

[ELF/Driver.cpp:2284-2295](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L2284-L2295) —— 一组"状态开关"分支：`--whole-archive`、`--as-needed`、`-Bstatic`/`-Bdynamic` 只是把 `LinkerDriver` 的成员变量（`inWholeArchive`、`ctx.arg.asNeeded`、`ctx.arg.isStatic`）翻转，**它们本身不读文件**，但会影响后续 `addFile` 构造的 `LoadJob` 字段。

[ELF/Driver.cpp:2303-2328](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L2303-L2328) —— `--start-group`/`--end-group`/`--start-lib`/`--end-lib`。注意 LLD 对 `--start-group` 基本是 no-op（只维护 `isInGroup` 用于分组编号），这是因为 LLD 的归档算法天然支持循环依赖，不需要传统链接器那种反复扫描（参见第一单元讲过的"高效静态库处理"设计原则）。

循环结束后，[ELF/Driver.cpp:2346-2348](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L2346-L2348) 显式调用 `loadFiles()` 展开队列，并在没有任何输入时报"no input files"。

**`addFile` 的分类逻辑**是本节核心：

[ELF/Driver.cpp:229-293](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L229-L293) —— `LinkerDriver::addFile`。注释（224-228 行）说得很清楚：每个常规输入都被记录为 `LoadJob`，在 `createFiles()` 内部攒批、在末尾并行展开；在 `createFiles()` 之外则立即展开单个作业。

第 250-254 行先判 `unknown`：文件头不像任何已知格式，就当作链接脚本交给 `readLinkerScript` 直接处理、不入队：

```cpp
auto magic = identify_magic(mbref.getBuffer());
if (magic == file_magic::unknown) {
  readLinkerScript(ctx, mbref);
  return;
}
```

第 255-276 行就是 `file_magic` → `LoadJob::Kind` 的核心映射（本节最该记住的一段）：

```cpp
LoadJob::Kind kind;
switch (magic) {
case file_magic::archive:          kind = LoadJob::Archive;   break;
case file_magic::elf_relocatable:  kind = LoadJob::Obj;       break;
case file_magic::bitcode:          kind = LoadJob::Bitcode;   break;
case file_magic::elf_shared_object:
  if (ctx.arg.isStatic) { Err(ctx) << "attempted static link of dynamic object " << path; return; }
  kind = LoadJob::Shared;
  break;
default:  Err(ctx) << path << ": unknown file type"; return;
}
```

注意 `elf_shared_object` 在静态链接（`-static`/`-Bstatic`）下会直接报错返回——这是"静态链接不允许引入动态库"的合理校验。

第 277-288 行把决定好的 `kind` 连同当时的各项状态（`inWholeArchive`/`inLib`/`asNeeded`/`withLOption`/`nextGroupId`）打包成一个 `LoadJob` 入队。这些状态字段会被 `loadFiles` 原封不动地用到。最后第 291-292 行：

```cpp
if (!deferLoad)
  loadFiles();
```

正是前面说的"延迟加载开关"——`createFiles` 内 `deferLoad=true`，所以不触发；外部调用 `addFile` 时 `deferLoad=false`，立即触发。

`LoadJob` 结构体本身定义在 `Config.h`：

[ELF/Config.h:189-203](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Config.h#L189-L203) —— `LoadJob`。`enum Kind : uint8_t { Obj, Bitcode, Archive, Shared, Binary };` 五种取值；字段还包括文件缓冲 `mbref`、路径、各种状态标志、`groupId`，以及输出用的 `out`（展开后产生的 `InputFile` 列表）、`thinBufs`（thin archive 的成员缓冲所有权）、`tarEntries`（`--reproduce` 用）。

[ELF/Config.h:205-240](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Config.h#L205-L240) —— `LinkerDriver` 类。可以看到 `loadJobs`（229 行）、`files`（232 行）两个核心容器，以及 `deferLoad`、`inWholeArchive`、`inLib` 等状态成员。`files` 就是 `loadFiles` 展开后最终交付给 `link<ELFT>` 的输入文件列表。

#### 4.2.4 代码实践

**实践目标**：亲手在 `addFile` 的源码里走一遍 `file_magic` → `LoadJob::Kind` 的分支，理解每种输入如何被归类。

**操作步骤**：

1. 打开 [ELF/Driver.cpp:255-276](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L255-L276)，对照下面这张表，把每个 `case` 对应的 `Kind` 和"会被谁处理"填进去：

   | 输入 | `file_magic` | `LoadJob::Kind` | `loadFiles` 里交给谁构造 |
   |------|--------------|-----------------|--------------------------|
   | `main.o` | `elf_relocatable` | `Obj` | `createObjFile` → `ObjFile` |
   | `libfoo.a` | `archive` | `Archive` | `getArchiveMembers` 逐个展开 |
   | `foo.o.bc` | `bitcode` | `Bitcode` | `BitcodeFile` 构造 |
   | `libfoo.so` | `elf_shared_object` | `Shared` | `SharedFile` 构造 + `init()` |
   | `data.bin`（`--format binary`） | （跳过 magic） | `Binary` | `BinaryFile` 构造 |
   | `link.script` | `unknown` | （不建 LoadJob） | `readLinkerScript` 直接解析 |

2. **思考实验**（源码阅读型）：假设你给 `ld.lld` 传了一个扩展名是 `.a`、但内容其实是个普通 `.o` 的文件。`addFile` 会怎么分类？为什么 LLD 不看扩展名而看文件头？

**需要观察的现象 / 预期结果**：你会发现 LLD **完全不依赖文件扩展名**，只相信 `identify_magic` 探测到的文件头魔数（magic bytes）。这就是为什么把一个 `.so` 改名成 `.o` 传给 LLD，它依然按共享库处理。第 2 步的"伪 `.a`"会被识别成 `elf_relocatable` → `Obj`，而不是 `Archive`——因为归档的魔数是 `!<arch>\n`，与 ELF 目标文件不同。

**若无法本地运行**：直接通读源码完成上表，标注"待本地验证"。

#### 4.2.5 小练习与答案

**练习 1**：`createFiles` 用 `SaveAndRestore saveDefer(deferLoad, true)` 把 `deferLoad` 临时设为 `true`。如果不这么做（即 `deferLoad` 在 `createFiles` 期间保持 `false`），会出什么问题？

> **参考答案**：那么每 `addFile` 一个输入就会**立刻**调用一次 `loadFiles()`，文件加载退化成完全串行、无法并行；而且每个归档会被单独展开一次，失去批量并行优化的机会。延迟加载的意义就在于"攒一批一起并行处理"。

**练习 2**：`addFile` 把 `inWholeArchive`、`asNeeded`、`nextGroupId` 这些**当下**的状态快照写进 `LoadJob`。为什么要在入队时就拍快照，而不是等 `loadFiles` 时再去读这些变量？

> **参考答案**：因为这些状态会随后续命令行参数变化（例如 `--whole-archive foo.a --no-whole-archive bar.a`，两个库的 `inWholeArchive` 不同）。`loadFiles` 在所有输入都处理完之后才统一执行，那时这些变量已经是"最终状态"，无法还原每个文件被加入时的状态。入队时拍快照，保证了每个文件按它**被声明时**的语义处理。

**练习 3**：`elf_shared_object` 在 `ctx.arg.isStatic` 为真时报错返回。那 `ctx.arg.isStatic` 是在哪里被设置的？

> **参考答案**：在 `createFiles` 开头（[ELF/Driver.cpp:2229](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L2229)）`ctx.arg.isStatic = ctx.arg.relocatable;`（`-r` 隐含静态），以及循环里遇到 `-Bstatic`/`--omagic`/`--nmagic` 时置 `true`、遇到 `-Bdynamic` 时置 `false`（228-2283 行）。所以 `isStatic` 的值取决于命令行上 `-Bstatic`/`-Bdynamic` 出现的顺序——这也是为什么 `addFile` 必须按命令行顺序逐个处理。

---

### 4.3 loadFiles 的并行文件加载

#### 4.3.1 概念说明

`loadFiles` 是"延迟加载"队列的兑现点。它把 `loadJobs` 里攒下的所有 `LoadJob` 用 `llvm::parallelFor` **并行**展开：每个 `LoadJob` 根据自己的 `kind` 构造出对应的 `InputFile`（`ObjFile`/`BitcodeFile`/`SharedFile`/`BinaryFile`），归档则先展开成成员再逐个构造。展开产物被收进 `LinkerDriver::files`，最终交给 `link<ELFT>`。

这里有一个并发的关键细节：`BitcodeFile` 和"fat LTO 对象"的构造会调用 `ctx.saver`（一个 `StringSaver`，内部不是线程安全的），而 `ObjFile`/`SharedFile` 的构造是线程安全的。LLD 的做法不是"全部加锁"，而是**只对需要 `saver` 的那两种加锁**，其余放手并行。这是"少加锁、细粒度"的典型范例——回忆第一单元讲过的"Speed by design：少做事胜过高效做事"。

#### 4.3.2 核心流程

```
loadFiles():
  mu = std::mutex                                    // 仅给 bitcode/fatLTO 用
  makeFile(mb, magic, arPath, offset, lazy) -> InputFile:
      if magic == bitcode:           锁内构造 BitcodeFile
      elif fatLTO 且缓冲含 bitcode:   锁内构造 BitcodeFile（标记 fatLTOObject）
      else:                           无锁构造 createObjFile（→ ObjFile）

  parallelFor(0, loadJobs.size(), [&](i):            // 并行展开每个作业
    switch (job.kind):
      Obj / Bitcode:  job.out += makeFile(job.mbref, ...)
      Archive:        members = getArchiveMembers(job)   // 取出全部成员
                        for mb, offset in members:
                          mm = identify_magic(mb)
                          if mm 是 elf_relocatable/bitcode 或 inWholeArchive:
                              job.out += makeFile(mb, mm, job.path, offset, lazy)
                          else 警告"非 ET_REL 非 bitcode"
      Shared:         构造 SharedFile；调用 f->init()；设置 isNeeded
      Binary:         构造 BinaryFile
      for m in job.out: m->groupId = job.groupId        // 染上传阅组号
  )

  // 串行收尾：把每个 job.out 追加进 files
  for job in loadJobs:
      files.append(job.out ...)
      memoryBuffers.append(job.thinBufs ...)            // thin archive 缓冲所有权转移
  loadJobs.clear()
```

几个要点：

- **归档的展开策略**：LLD **不**用归档自带的符号索引来按需提取，而是把归档里**所有**成员都取出来（`getArchiveMembers`），其中 `ET_REL` 和 bitcode 成员构造为对象文件并标记 `lazy`（除非 `--whole-archive`）。"是否真的需要这个成员"的判断推迟到后续符号解析阶段（`handleUndefined`）。这正是 LLD"记住全部符号、即时提取"算法的体现。
- **`groupId` 的作用**：同一个归档的所有成员共享一个 `groupId`，用于支持 `--warn-backrefs`（检测"后引用先定义"的反向依赖）。
- **thin archive 的缓冲所有权**：thin archive 只记录成员的路径而非内容，`getArchiveMembers` 会持有这些成员的 `MemoryBuffer`，通过 `job.thinBufs = file->takeThinBuffers()` 转移所有权，最后追加进 `ctx.memoryBuffers` 保活。

#### 4.3.3 源码精读

[ELF/Driver.cpp:2123-2220](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L2123-L2220) —— `LinkerDriver::loadFiles` 全貌。

开头注释（2124-2126 行）一语道破并发策略：

```cpp
// BitcodeFile / fatLTO constructors call ctx.saver which is not thread-safe.
// SharedFile and ObjFile constructors are safe without the mutex.
std::mutex mu;
```

随后的 lambda `makeFile`（2127-2146 行）封装了"按 magic 构造 InputFile"的逻辑，只在 bitcode/fatLTO 两条路径上加锁：

[ELF/Driver.cpp:2127-2146](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L2127-L2146) —— `makeFile` lambda。注意 fat LTO 路径用 `IRObjectFile::findBitcodeInMemBuf` 在普通对象里"嗅探"隐藏的 bitcode，找到则按 bitcode 处理并打 `fatLTOObject(true)` 标记。

主体在 `TimeTraceScope("Parallel load")` 下的 `parallelFor`（2148-2202 行）：

[ELF/Driver.cpp:2148-2202](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L2148-L2202) —— 并行展开主体。`parallelFor(0, loadJobs.size(), ...)` 把每个 `LoadJob` 的处理当成一个并行任务。

`Obj`/`Bitcode` 分支（2153-2160 行）最简单：直接 `makeFile` 构造一个对象，塞进 `job.out`。

`Archive` 分支（2161-2181 行）最有意思——它揭示了 LLD 与传统链接器的根本差异：

[ELF/Driver.cpp:2161-2181](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L2161-L2181) —— 归档处理。注释 2162-2166 行明确说"扫描归档全部成员而非用符号索引"，"同一归档所有文件共享 groupId 以支持 `--warn-backrefs`"。`lazy = !job.inWholeArchive`：非 whole-archive 时成员标记为惰性，等到符号解析时按需激活。循环里对每个成员再次 `identify_magic`，只接受 `elf_relocatable`/`bitcode`（或 `--whole-archive` 强制），否则警告。

`Shared` 分支（2182-2194 行）：

[ELF/Driver.cpp:2182-2194](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L2182-L2194) —— 共享库用 soname 标识：若由 `-lfoo` 引入则只取文件名（忽略目录部分），否则用完整路径。`isNeeded = !asNeeded` 记录"是否真正被需要"（`--as-needed` 下未引用的共享库不进 DT_NEEDED）。

`Binary` 分支（2195-2197 行）直接构造 `BinaryFile`。

每个作业处理完后，2199-2200 行统一给产物染上 `groupId`。

最后是**串行收尾**（2204-2219 行）：

[ELF/Driver.cpp:2204-2219](https://github.com/llvm/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L2204-L2219) —— 因为并行阶段各任务只写自己的 `job.out`（无共享写竞争），所以可以放心并行；收尾阶段才串行地把所有 `job.out` 追加进 `files`、把 thin archive 缓冲追加进 `ctx.memoryBuffers`，最后 `loadJobs.clear()`。这种"并行计算 + 串行收集"是 LLD 常用的并行模式。

**关联：`inferMachineType` 在 `loadFiles` 之后登场**

`createFiles` 返回后（即 `loadFiles` 跑完、`files` 已填满），`linkerMain` 调用 `inferMachineType()`：

[ELF/Driver.cpp:2352-2373](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L2352-L2373) —— 若用户没传 `-m`（`ekind == ELFNoneKind`），就从 `files` 里第一个有效目标文件借它的 `ekind`/`emachine`/`osabi` 过来；若一个能推断架构的文件都没有，就报"target emulation unknown"。

随后 `setConfigs` 才能根据这些值算出 `is64`/`isLE` 等，`checkOptions` 才能校验"这个选项是否适用于该架构"，最后 `invokeELFT(link, args)` 按 `ekind` 分发：

[ELF/Target.h:362-380](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Target.h#L362-L380) —— `invokeELFT` 宏。它是个 `switch(ctx.arg.ekind)`，把 `link` 这个模板函数实例化成 `link<ELF32LE>`/`link<ELF32BE>`/`link<ELF64LE>`/`link<ELF64BE>` 四选一。这就是为什么 `linkerMain` 自己不模板化、却能把控制权交给类型正确的 `link<ELFT>`——架构信息是在运行时由 `inferMachineType` 确定的，而模板实例化在编译期就已全部生成。

#### 4.3.4 代码实践

**实践目标**：用一个真实的归档库，观察 `loadFiles` 里"归档全部成员展开 + lazy 标记"的行为，理解 LLD 的归档处理为什么不需要 `--start-group`。

**操作步骤**：

1. 准备两个互相依赖的小目标文件（示例代码，需要本机有 `clang`/`gcc` 与 `ld.lld`）：

   ```c
   // a.c —— 调用 b()，被 B 库定义
   extern int b(void);
   int a(void) { return b(); }
   // b.c —— 调用 a()，被 A 库定义；构造循环依赖 a.a ↔ b.a
   extern int a(void);
   int b(void) { return a(); }
   // main.c —— 入口
   extern int a(void);
   int main(void) { return a(); }
   ```

2. 编译并各自打成静态库（**故意制造 a 依赖 b、b 依赖 a 的循环**）：

   ```bash
   clang -c -ffunction-sections a.c b.c main.c
   llvm-ar rcs a.a a.o
   llvm-ar rcs b.a b.o
   ```

3. 用 LLD 链接，**不**加 `--start-group`：

   ```bash
   ld.lld main.o a.a b.a -o test.elf
   # 或：clang -fuse-ld=lld main.o a.a b.a -o test.elf
   ```

4. 阅读源码 [ELF/Driver.cpp:2161-2181](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L2161-L2181)，结合第一单元讲过的"LLD 记住全部符号、按需即时提取"，解释为什么第 3 步能成功（传统链接器在 `ld main.o a.a b.a` 顺序下，处理完 a.a 后 b.a 里的 a() 已无法再回头从 a.a 提取，需要 `--start-group` 或调整顺序）。

**需要观察的现象**：

- 第 3 步链接成功，输出 `test.elf`。
- `ld.lld main.o a.a b.a a.a`（重复写 a.a）也能成功，因为 LLD 的符号解析是全局的。

**预期结果**：你亲眼看到 LLD 不需要 `--start-group` 就能处理循环依赖的归档。再对照 `loadFiles` 源码：归档成员被全部展开并标记 lazy，真正"是否提取"推迟到 `link<ELFT>` 里的 `handleUndefined`（下一讲）按符号表全局状态决定，因此命令行顺序和分组对 LLD 基本无影响。

**若本机无法编译/链接**：跳到源码阅读——在 [ELF/Driver.cpp:2161-2181](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L2161-L2181) 确认"全部成员展开 + lazy 标记"的事实，并标注"待本地验证循环依赖用例"。

#### 4.3.5 小练习与答案

**练习 1**：`loadFiles` 里只有 `BitcodeFile` 和 fat LTO 对象的构造加了锁，`ObjFile`/`SharedFile` 的构造不加锁。假如为了"保险"给所有构造都套上同一把 `mu`，会有什么代价？

> **参考答案**：会把原本可以并行的对象文件构造强制串行化，大幅拖慢大型项目（往往成百上千个 `.o`）的加载阶段。LLD 的做法是**只锁真正不线程安全的部分**（访问 `ctx.saver` 的 bitcode 路径），让绝大多数普通目标文件构造完全并行，这正是高性能的关键。

**练习 2**：`Shared` 分支里 `f->withLOption ? path::filename(bufPath) : bufPath` 是什么意思？为什么用 `-l` 引入和直接写路径要区别对待？

> **参考答案**：共享库用 soname（`DT_SONAME`，回退到文件名）标识。当用户写 `-lfoo` 时，库可能在 `./debug/libfoo.so` 等任意目录，但其他 `.so` 对它的 `DT_NEEDED` 只写 `libfoo.so`（不带目录），所以 LLD 也只取文件名部分作为标识，保证匹配。直接写路径 `./libfoo.so` 时则保留完整路径，因为那是用户的明确意图。

**练习 3**：`inferMachineType` 取的是 `files` 里**第一个**有效目标文件的架构。如果你把一个 x86-64 的 `.o` 和一个 AArch64 的 `.o` 一起传给 `ld.lld`，会发生什么？

> **参考答案**：`inferMachineType` 会以第一个 `.o`（x86-64）的 `ekind`/`emachine` 为准，`setConfigs` 据此设置 64 位小端。之后进入 `link<ELF64LE>`，那个 AArch64 的 `.o` 会在解析阶段（`ObjFile` 的 `initializeSymbols`/重定位处理）因为架构不匹配而被报错。LLD 不做"多架构混合链接"，跨架构输入只会推迟报错，而不会静默产出错误结果。

---

## 5. 综合实践

把本讲三个模块串起来，做一次"命令行到 `files` 列表"的全程追踪。

**任务**：给定下面这条命令（假设本机有相应工具链；若没有，改为纯源码追踪）：

```bash
ld.lld -m elf_x86_64 -o test.elf --gc-sections main.o -lfoo ./libbar.so @args.rsp
```

请按 `linkerMain` 的阶段顺序，回答下列问题，**每个答案都要给出对应的源码行号或永久链接**：

1. **解析阶段**：`@args.rsp` 在哪一行、被哪个函数展开成实际参数？`-m elf_x86_64` 被解析成哪个 `OPT_xxx` 常量？它在哪里被翻译成 `(ELF64LEKind, EM_X86_64)`？（提示：`parseEmulation`，[ELF/Driver.cpp:145-190](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L145-L190)）

2. **配置阶段**：`--gc-sections` 是在 `readConfigs` 还是 `setConfigs` 里被写进 `ctx.arg`？为什么？（提示：它与架构无关，所以应该早。在 `readConfigs` 里搜 `OPT_gc_sections`。）

3. **分类阶段**：`main.o`、`-lfoo`（假设解析到 `libfoo.a`）、`./libbar.so` 分别会被 `addFile` 归类成哪三种 `LoadJob::Kind`？`-lfoo` 在变成 `libfoo.a` 之前，多走了哪一步（哪个函数）？

4. **并行加载阶段**：`libfoo.a` 在 `loadFiles` 里走哪个 `case`？它的成员是按"符号索引按需提取"还是"全部展开"？给出源码行号。

5. **架构推断阶段**：本例显式传了 `-m elf_x86_64`，`inferMachineType` 会在第几行提前 `return`？如果不传 `-m`，它会从哪里取架构？

6. **分发阶段**：`invokeELFT(link, args)` 最终调用的是 `link<ELF64LE>` 还是 `link<ELF64BE>`？依据是 `ctx.arg.ekind` 的哪个值？

**完成标志**：你能画出一张从 `argv` 到 `files`（`SmallVector<unique_ptr<InputFile>>`）的数据流图，标出每一步经过的函数与源码位置。这张图就是本讲的"知识总账"，下一讲 `link<ELFT>` 会从 `files` 接着往下走。

---

## 6. 本讲小结

- `linkerMain` 是 ELF 链接的调度中枢，严格按 **parse → readConfigs → createFiles → inferMachineType → setConfigs → checkOptions → link** 七步推进，两处 `if (errCount(ctx)) return;` 检查点分别守护文件加载与选项校验。
- 命令行解析建立在 LLVM 的 `OptTable` 框架上：`Options.td` 声明选项 → TableGen 生成 `Options.inc` → `ELFOptTable::parse` 把 `argv` 解析成 `InputArgList`，并自动支持响应文件 `@file`、`--help`、拼写纠错。
- `readConfigs` 与 `setConfigs` 的分工是"架构无关 vs 架构相关"——前者在 `createFiles` 前执行，后者在 `inferMachineType` 后执行；这种拆分源于"有些配置必须等知道目标机后才能确定"。
- LLD 用 `LoadJob` 实现**延迟加载**：`addFile` 只用 `identify_magic` 把文件分类入队（`Obj`/`Bitcode`/`Archive`/`Shared`/`Binary`），真正的解析推迟到 `loadFiles()` 统一并行展开；`deferLoad` 标志让同一函数既服务批量加载（`createFiles` 内）又服务即时加载（外部依赖库）。
- `loadFiles` 用 `parallelFor` 并行展开队列，对线程不安全的 bitcode/fatLTO 路径细粒度加锁，对象文件构造完全并行；归档成员被**全部展开**并标记 lazy，这就是 LLD 无需 `--start-group` 即可处理循环依赖归档的根因。
- `inferMachineType` + `invokeELFT` 完成运行时架构分发：从首个目标文件推断 `ekind`，再用 `switch(ekind)` 选择正确的 `link<ELFT>` 模板实例——架构信息运行时确定，模板实例编译期就已全部生成。

## 7. 下一步学习建议

本讲到 `invokeELFT(link, args)` 就戛然而止——`files` 已经装满 `InputFile`，但它们还没被解析成符号、还没聚合成段、更没写盘。下一讲 **`u2-l3 link<ELFT> 核心流水线全景`** 会接过控制权，沿 `link<ELFT>` 走完：`parseFiles`（把 `files` 真正解析进符号表）→ `compileBitcodeFiles`（LTO）→ 段聚合 → `markLive`（GC）→ 合成段 → ICF → `writeResult`。

建议在进入下一讲前，先做两件准备：

1. **通读** [`link<ELFT>` 的开头](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L3245-L3289)，重点看 `parseFiles(ctx, files)` 这一行——它消费的正是本讲产出的 `files` 列表。
2. **回顾**本讲的 `LoadJob` 与延迟加载设计，因为下一讲里"惰性求值"的思想会以更强大的形式（重定位扫描两阶段、地址分配迭代收敛）反复出现。

如果对符号解析的具体规则（`Defined`/`Undefined`/`Lazy` 如何冲突解决）感兴趣，可以在学完下一讲后跳到第四单元（`u4`）深入。
