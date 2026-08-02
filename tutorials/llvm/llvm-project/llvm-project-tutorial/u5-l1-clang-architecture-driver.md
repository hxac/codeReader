# Clang 架构总览与 Driver 驱动层

## 1. 本讲目标

本讲是「Clang 前端全流程」单元（u5）的第一讲，目标是帮你建立 Clang 的整体地图。学完后你应当能够：

- 理解一条 `clang` 命令是如何被**拆解**成一组具体编译动作（预处理、编译、汇编、链接）的；
- 掌握 **Driver（外层驱动）与 cc1（内层前端）** 这两层的职责划分，知道哪一层做什么；
- 能在源码中精确定位 `clang_main`、`BuildCompilation`、`ExecuteCompilation`、`cc1_main` 这些关键入口；
- 会用 `clang -###`、`-ccc-print-phases` 等开关，亲眼「看见」Driver 的编排过程。

本讲只讲**架构与驱动层**，不深入词法、语法、语义分析等前端内部细节——那些留给 u5-l2 ~ u5-l5。前置知识依赖 u3-l1 的「Module/Function/BasicBlock 层次」，因为本讲会提到「cc1 最终产出 LLVM IR（即内存中的 `Module`）」，那是 Driver 编排的终点之一。

## 2. 前置知识

在进入 Clang 之前，先用三段话建立直觉。

**编译器 vs 编译器驱动。** 我们平时敲的 `clang main.c -o main`，其实是一个「编译器驱动（compiler driver）」，而不是真正的编译器。它像一个**项目经理**：接到用户的命令后，先决定要做哪些工序（要不要预处理、要不要链接、目标是什么平台），再安排具体的「工人」去执行。真正的「工人」——也就是做词法分析、语法分析、生成 IR 的那部分——藏在驱动内部，叫做 **cc1**。

**为什么要把 Driver 和 cc1 分开？** 因为一条用户命令往往对应**一连串**子任务。例如 `clang a.c b.c -o prog` 实际上要：分别预处理+编译 `a.c`、`b.c` 得到两个目标文件，再把它们链接成 `prog`。Driver 负责把这条高层命令「展开」成这条工序流水线，并决定每一道工序用什么工具（Clang 自带的 cc1、系统的汇编器 `as`、链接器 `ld` 等）。cc1 则只关心「给我一份输入和一组选项，我产出对应的输出（IR / 汇编 / 目标文件）」。

**这两层在源码里就是两个入口函数。**
- 外层 Driver 的入口：`clang_main`（`clang/tools/driver/driver.cpp`），它构造一个 `Driver` 对象，编排出整条工序流水线。
- 内层 cc1 的入口：`cc1_main`（`clang/tools/driver/cc1_main.cpp`），它真正跑前端，产出 IR 或目标产物。

> 关键术语：**Driver**（驱动，编排者）、**cc1**（真正的前端）、**Action**（一道抽象工序）、**Tool / ToolChain**（执行工序的工具及其平台环境）、**Command**（最终要执行的命令）、**Compilation**（一次完整编译的上下文对象）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `clang/tools/driver/driver.cpp` | **外层 Driver 的可执行入口**。定义 `clang_main`、`ExecuteCC1Tool`，是 Driver 与 cc1 的派发枢纽。 |
| `clang/tools/driver/cc1_main.cpp` | **内层 cc1 的入口**。定义 `cc1_main`，构造 `CompilerInvocation`/`CompilerInstance`，调用 `ExecuteCompilerInvocation`。 |
| `clang/include/clang/Driver/Driver.h` | `Driver` 类的声明。集中了 `BuildCompilation`、`BuildActions`、`BuildJobs`、`ExecuteCompilation`、`HandleImmediateArgs` 等编排方法。 |
| `clang/lib/Driver/Driver.cpp` | `Driver` 类的实现。`BuildCompilation`（编排总入口）、`ExecuteCompilation`（执行总入口）都在这里。 |
| `clang/include/clang/Driver/Phases.h` | 定义编译「阶段（Phase）」枚举：Preprocess / Compile / Assemble / Link 等。 |
| `clang/lib/Driver/Job.cpp` | `CC1Command::Execute`：Driver 如何在**同一进程内**直接调用 cc1（integrated-cc1）。 |
| `clang/lib/FrontendTool/ExecuteCompilerInvocation.cpp` | cc1 内部：把「程序动作（ProgramAction）」映射成具体的 `FrontendAction` 并执行。 |
| `clang/docs/DriverInternals.rst` | 官方对 Driver 五阶段设计（Parse / Pipeline / Bind / Translate / Execute）的权威文档。 |
| `clang/docs/InternalsManual.rst` | Clang 前端整体手册，含「The Driver Library」「The Frontend Library」两节。 |

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**4.1 Driver 与 cc1 的两层划分**，**4.2 编译动作编排（从命令行到命令流水线）**。

### 4.1 Driver 与 cc1：两层职责划分

#### 4.1.1 概念说明

Clang 命令的处理可以画成两层：

```
        用户命令 clang main.c -o main
                   │
        ┌──────────▼──────────┐
        │   外层 Driver         │  解析 GCC 风格命令行
        │  (clang_main / Driver)│  编排工序、选工具、生成命令
        └──────────┬──────────┘
                   │  展开成一组 Command，其中编译工序形如
                   │   clang -cc1 ... main.c ...
        ┌──────────▼──────────┐
        │   内层 cc1           │  真正的前端：词法→语法→语义→CodeGen
        │  (cc1_main)          │  产出 IR / 汇编 / 目标文件
        └──────────────────────┘
```

- **外层 Driver**：兼容 GCC 命令行（这是它的首要目标，见 `DriverInternals.rst` 的 "GCC Compatibility" 一节）。它**不懂** C/C++ 语法，只懂「编译一个 C 文件要先预处理、再编译、可能还要汇编和链接」。它的产出是一组待执行的 `Command`。
- **内层 cc1**：用 `-cc1` 标志触发（`clang -cc1 ...`）。它接收的是**前端专属选项**（与 GCC 风格不同），负责真正解析源码并生成产物。Driver 通常会自动为每个需要编译的输入「拼」出一条 `clang -cc1 ...` 命令。

为什么要分两层？因为 Driver 要同时扮演「GCC 替身」「MSVC cl.exe 替身（clang-cl）」「Fortran 驱动（flang）」等多种角色，命令行兼容性极其复杂；而 cc1 只关心纯粹的编译逻辑。把两者解耦，Driver 的复杂度就不会污染前端。

#### 4.1.2 核心流程

外层入口 `clang_main` 的处理顺序：

1. 收集命令行参数、展开 response file（`@file`）。
2. **判断是否是 `-cc1` 直接调用**：若 `Args[1]` 以 `-cc1` 开头，说明这次进程本身就是 Driver 派生出来的（或用户手敲的）cc1，直接转入 `ExecuteCC1Tool` → `cc1_main`，**不再做任何编排**。
3. 否则，构造 `Driver` 对象，调用 `BuildCompilation` 编排出 `Compilation`，再调用 `ExecuteCompilation` 执行其中的命令。
4. 执行阶段，当某个 `Command` 是一条 cc1 命令时，可以选择**在同一进程内**直接调用 cc1（integrated-cc1，默认），也可以 spawn 子进程（`-fno-integrated-cc1`）。

内层 `cc1_main` 的处理顺序：

1. 把 cc1 参数解析成 `CompilerInvocation`（前端所有选项的容器）。
2. 用它构造 `CompilerInstance`（前端运行时的「大管家」，持有 ASTContext、SourceManager 等）。
3. 调用 `ExecuteCompilerInvocation`，根据 `ProgramAction`（如 `-emit-obj`、`-emit-llvm`、`-fsyntax-only`）选出对应的 `FrontendAction` 并执行，最终产出 IR/目标文件等。

#### 4.1.3 源码精读

**(1) 外层入口 `clang_main`：先判断是不是 cc1 调用。**

[clang/tools/driver/driver.cpp:242-278](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L242-L278)：这是整个 clang 进程的实质入口。关键在 L271-278——如果第二个参数以 `-cc1` 开头，就调用 `ExecuteCC1Tool` 直接走内层，根本不构造 Driver。

```cpp
// Handle -cc1 integrated tools.
if (Args.size() >= 2 && StringRef(Args[1]).starts_with("-cc1")) {
  auto EnableSandbox = llvm::sys::sandbox::scopedEnable();
  return ExecuteCC1Tool(Args, ToolContext, VFS);
}
```

[clang/tools/driver/driver.cpp:210-240](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L210-L240)：`ExecuteCC1Tool` 根据 `-cc1` / `-cc1as` / `-cc1gen-reproducer` 派发到不同的内层工具。`-cc1` 对应 `cc1_main`（L228-229）：

```cpp
StringRef Tool = ArgV[1];
if (Tool == "-cc1")
  return cc1_main(ArrayRef(ArgV).slice(1), ArgV[0], GetExecutablePathVP);
```

**(2) 外层不是 cc1 时，构造 Driver 并编排执行。**

[clang/tools/driver/driver.cpp:360-363](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L360-L363)：构造 `Driver` 对象，传入可执行文件路径、默认目标 triple、诊断引擎和虚拟文件系统。

```cpp
Driver TheDriver(Path, llvm::sys::getDefaultTargetTriple(), Diags,
                 /*Title=*/"clang LLVM compiler", VFS);
```

[clang/tools/driver/driver.cpp:388-419](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L388-L419)：`BuildCompilation` 编排出 `Compilation`，`ExecuteCompilation` 真正执行。注意 L381-386：默认（`-fintegrated-cc1`）会把 `ExecuteCC1Tool` 作为回调 `TheDriver.CC1Main` 挂上，这样 cc1 工序能在同进程内执行，并配合 `CrashRecoveryContext` 在 cc1 崩溃时不拖垮整个 Driver。

**(3) 同进程 cc1：`CC1Command::Execute` 调用挂载的回调。**

[clang/lib/Driver/Job.cpp:408-447](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Job.cpp#L408-L447)：当一条命令是 cc1 命令、且启用了 integrated-cc1（`InProcess == true`）时，**不 spawn 子进程**，而是在 `CrashRecoveryContext` 保护下直接调用 `D.CC1Main(Argv)`（L442），进入 `cc1_main`。这就是「外层 Driver 调用内层 cc1」在同一进程里的衔接点。

```cpp
// Enter ExecuteCC1Tool() instead of starting up a new process
if (!CRC.RunSafely([&]() { R = D.CC1Main(Argv); })) {
  llvm::RestorePrettyStackState(PrettyState);
  return CRC.RetCode;
}
```

**(4) 内层入口 `cc1_main`：构造前端并执行。**

[clang/tools/driver/cc1_main.cpp:219-251](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/cc1_main.cpp#L219-L251)：`cc1_main` 用 `CompilerInvocation::CreateFromArgs` 把 cc1 参数解析成选项容器（L246-248），再用它构造 `CompilerInstance`（L250-251）。

```cpp
auto Invocation = std::make_shared<CompilerInvocation>();
bool Success =
    CompilerInvocation::CreateFromArgs(*Invocation, Argv, Diags, Argv0);
auto Clang = std::make_unique<CompilerInstance>(std::move(Invocation),
                                                std::move(PCHOps));
```

[clang/tools/driver/cc1_main.cpp:296-296](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/cc1_main.cpp#L296)：最终通过 `ExecuteCompilerInvocation(Clang.get())` 启动前端动作。

[clang/lib/FrontendTool/ExecuteCompilerInvocation.cpp:232-335](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/FrontendTool/ExecuteCompilerInvocation.cpp#L232-L335)：`ExecuteCompilerInvocation` 根据 `ProgramAction` 创建 `FrontendAction`（L328），然后 `Clang->ExecuteAction(*Act)`（L331）真正运行前端——这部分是 u5-l2 ~ u5-l5 的主角，本讲只需知道它是 cc1 的「实际干活者」。

**(5) Driver 与 cc1 的「桥梁字段」CC1Main。**

[clang/include/clang/Driver/Driver.h:285-291](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Driver.h#L285-L291)：`Driver` 类持有 `CC1Main` 函数指针（注释说明：当通过 clang.exe 使用 clangDriver 库时，它提供「在同进程内直接执行 `-cc1` 命令行」的捷径）。这正是上面 `CC1Command::Execute` 里 `D.CC1Main(Argv)` 调用的那个字段。

> 小结：`clang_main`（Driver 入口）→ 编排出 `Compilation` → 执行其中的 cc1 命令 →（同进程）`CC1Main` 回调 → `ExecuteCC1Tool` → `cc1_main`（前端入口）→ `ExecuteCompilerInvocation` → `FrontendAction`。

#### 4.1.4 代码实践

**实践目标：** 亲眼确认 Driver 在执行编译工序时会构造一条 `clang -cc1 ...` 命令，从而验证「Driver 把 cc1 当作内部工具调用」。

**操作步骤：**

1. 准备一个最小源文件 `hello.c`：
   ```c
   int main(void) { return 42; }
   ```
2. 运行（`-###` 表示「只打印将要执行的命令，不真正执行」）：
   ```bash
   clang -### hello.c -o hello
   ```
3. 在输出里找到以 `"-cc1"` 开头的那一长串参数。

**需要观察的现象：** 输出会显示一条形如 `clang -cc1 -triple ... -emit-obj ... hello.c -o ...` 的命令——这正是 Driver 为「编译 hello.c」这道工序自动拼出来的 cc1 命令。注意它的选项风格（`-triple`、`-emit-obj`、`-disable-free` 等）与你直接敲的 GCC 风格（`clang hello.c -o hello`）完全不同，这印证了 cc1 有一套独立的命令行接口。

**预期结果（基于官方文档与源码逻辑，具体参数值待本地验证）：** 你会看到至少两条命令——一条 `clang -cc1` 编译生成临时目标文件，一条链接命令（可能是 `ld` 或 `clang` 再调一次）把它链接成 `hello`。如果你只编译不链接（`clang -### -c hello.c`），则只会看到那条 `-cc1 ... -emit-obj` 命令。

#### 4.1.5 小练习与答案

**练习 1：** 为什么用户敲 `clang hello.c` 时，进程会先经过 Driver、再进入 cc1，而不是直接进入 cc1？

**参考答案：** 因为 `Args[1]` 是 `hello.c`（不以 `-cc1` 开头），`clang_main` 走的是 Driver 分支，由 Driver 编排后再（在同进程内或 spawn 子进程）以 `-cc1` 形式调用 cc1。只有用户显式敲 `clang -cc1 ...` 才会跳过 Driver 直接进入 `cc1_main`。

**练习 2：** `-fintegrated-cc1` 和 `-fno-integrated-cc1` 的区别是什么？

**参考答案：** 前者（默认）让 cc1 工序在**同一进程**内通过 `CC1Command::Execute` → `D.CC1Main` 回调执行，省去启动子进程的开销，便于调试与剖析；后者则每次 cc1 都 spawn 一个新的 clang 子进程（见 [driver.cpp:331-336](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L331-L336) 中 `UseNewCC1Process` 的判定）。

---

### 4.2 编译动作编排：从命令行到命令流水线

#### 4.2.1 概念说明

Driver 最核心的能力是「编排」。官方文档 `DriverInternals.rst` 把这个过程概念上分成**五个阶段**：

1. **Parse（解析）**：把命令行字符串拆成 `Arg` 对象。
2. **Pipeline（管线/动作构建）**：根据输入文件类型和选项，构造一棵 **Action（动作）树**——描述「这个文件要先预处理、再编译、再汇编」这样的工序依赖。
3. **Bind（绑定）**：给每个 Action 选定一个具体的 **Tool**（由 **ToolChain** 根据目标平台决定）。
4. **Translate（翻译）**：把 GCC 风格的参数翻译成具体工具接受的参数，生成最终的可执行 **Command**。
5. **Execute（执行）**：依次执行这些 Command。

这里有三个递进的数据结构，理解它们的区别是关键：

| 数据结构 | 含义 | 由哪个阶段产生 | 可观测开关 |
| --- | --- | --- | --- |
| `Action` | 抽象工序（如 preprocess/compile/assemble），多个 Action 组成一棵树 | Pipeline | `-ccc-print-phases` |
| `Tool` 绑定 | 把工序绑定到某平台的具体工具（如 `clang`、`as`、`ld`） | Bind | `-ccc-print-bindings` |
| `Command` | 最终要执行的命令（可执行路径 + 参数串） | Translate | `-###` |

另一个关键概念是 **Compilation** 对象：它是「一次完整编译」的上下文，持有输入参数、Action 列表、Job 列表、临时文件清单等。Driver 本身设计成可在多次编译间复用，而每次调用 `BuildCompilation` 才生成一个新的 `Compilation`（见 `DriverInternals.rst` 的 "The Compilation Object" 一节）。

#### 4.2.2 核心流程

「阶段（Phase）」的枚举定义在 [clang/include/clang/Driver/Phases.h:14-33](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Phases.h#L14-L33)：

```cpp
namespace phases {
  enum ID {
    Preprocess,   // 预处理（展开宏、#include）
    Precompile,   // 预编译（生成 PCH / 模块）
    Compile,      // 编译（→ IR，再 → 汇编）
    Backend,      // 后端（IR → 机器码层）
    Assemble,     // 汇编（→ 目标文件 .o）
    Link,         // 链接（→ 可执行文件 / 库）
    IfsMerge,     // 接口合并（与 .ifc / 模块相关）
  };
}
```

对一份普通 C 源码 `t.c`，默认（编译+链接）会经过 `Preprocess → Compile → Assemble → Link` 这条流水线。每个阶段对应一个 `Action`，前一个的输出是后一个的输入，构成一棵线性的动作树；多个输入最终在 `Link` 节点汇合。

`BuildCompilation` 的内部正是按「Parse → 选 ToolChain → 处理立即参数 → BuildInputs → BuildActions → BuildJobs」这条主线推进的：

```
BuildCompilation(Args)
  ├─ ParseArgStrings / loadConfigFiles         (Parse：解析参数)
  ├─ getToolChain(...)                          (据目标 triple 选工具链)
  ├─ new Compilation(...)                       (创建编译上下文)
  ├─ HandleImmediateArgs(*C)                    (处理 --help / --version 等可早退选项)
  ├─ BuildInputs(...)                           (确定输入文件及其类型)
  ├─ BuildUniversalActions / BuildActions       (Pipeline：构造 Action 树)
  └─ BuildJobs(*C)                              (Bind + Translate：生成 Command)
```

随后 `ExecuteCompilation` 负责第 5 阶段 Execute：若有 `-###` 则只打印不执行，否则真正 `ExecuteJobs`。

#### 4.2.3 源码精读

**(1) `BuildCompilation`：编排总入口。**

[clang/lib/Driver/Driver.cpp:1463-1463](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L1463)：方法签名。它的函数体很长，但骨架就是上面那张图。

[clang/lib/Driver/Driver.cpp:1718-1719](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L1718-L1719)：根据目标 triple 选定 ToolChain——这是「Bind」阶段能选对工具的前提。

[clang/lib/Driver/Driver.cpp:1795-1799](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L1795-L1799)：创建 `Compilation` 对象，紧接着调用 `HandleImmediateArgs`。后者返回 `false` 表示这是一个「只看参数就够」的调用（如 `--help`），可直接返回，不再构造任何工序。

```cpp
Compilation *C = new Compilation(*this, TC, UArgs.release(), TranslatedArgs,
                                 ContainsError);
if (!HandleImmediateArgs(*C))
  return C;
```

[clang/lib/Driver/Driver.cpp:1803-1803](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L1803)：`BuildInputs` 推断每个输入文件的类型（C / C++ / 汇编 / 目标文件……）。

[clang/lib/Driver/Driver.cpp:1863-1864](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L1863-L1864)：`BuildUniversalActions`（Mach-O 平台需要多架构合并）或 `BuildActions`，构造出 Action 树。这正是「Pipeline」阶段。

[clang/lib/Driver/Driver.cpp:1873-1873](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L1873)：`BuildJobs` 完成「Bind + Translate」，把每个 Action 绑定到具体 Tool 并翻译出 `Command`。这些 `Command` 最终挂在 `Compilation::getJobs()` 上。

**(2) `HandleImmediateArgs`：哪些选项会「早退」。**

[clang/include/clang/Driver/Driver.h:641-647](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Driver.h#L641-L647)：注释明确——它处理那些「在构造动作或绑定工具之前就该处理」的参数（如 `--help`、`--version`、`--print-supported-cpus`），返回 `false` 时 `BuildCompilation` 直接返回，跳过整条流水线。

**(3) `ExecuteCompilation`：执行总入口。**

[clang/lib/Driver/Driver.cpp:2357-2387](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L2357-L2387)：三段逻辑——
- `-fdriver-only`：只记录命令不执行（L2360-2371）；
- `-###`：只打印命令、不执行（L2373-2377）；
- 否则：`C.ExecuteJobs(C.getJobs(), FailingCommands)` 真正执行（L2387）。

```cpp
// Just print if -### was present.
if (C.getArgs().hasArg(options::OPT__Hash_Hash_Hash)) {
  C.getJobs().Print(llvm::errs(), "\n", true);
  return Diags.hasErrorOccurred() ? 1 : 0;
}
...
C.ExecuteJobs(C.getJobs(), FailingCommands);
```

**(4) Driver 类的编排方法一览。**

[clang/include/clang/Driver/Driver.h:472-551](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Driver.h#L472-L551)：集中声明了 `BuildCompilation`（L472）、`BuildInputs`（L487）、`BuildActions`（L496）、`BuildUniversalActions`（L504）、`BuildJobs`（L542）、`ExecuteCompilation`（L550）。这张方法清单就是 Driver 编排能力的「目录」，对照上面的流程图阅读即可建立全局观。

**(5) 官方文档对五阶段与 `-###` 的说明。**

[clang/docs/DriverInternals.rst:106-291](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst#L106-L291)：详细描述了 Parse / Pipeline / Bind / Translate / Execute 五阶段，并给出 `clang -###`、`-ccc-print-phases`、`-ccc-print-bindings` 三个开关各自的输出样例——这是理解编排过程最权威的参考。

> 三个观测开关的关系：`-ccc-print-phases` 看 Action 树（Pipeline 结果），`-ccc-print-bindings` 看工具绑定（Bind 结果），`-###` 看最终命令（Translate 结果）。从「抽象」到「具体」，三者层层细化。

#### 4.2.4 代码实践

**实践目标：** 用三个不同的观测开关，分别看到 Action 树、工具绑定、最终命令，从而完整理解 Driver 的编排全过程。

**操作步骤：**

1. 仍用上面的 `hello.c`，依次运行：

   ```bash
   # (a) 看 Action 树（Pipeline 阶段产物）
   clang -ccc-print-phases hello.c

   # (b) 看工具绑定（Bind 阶段产物）
   clang -ccc-print-bindings hello.c

   # (c) 看最终命令（Translate 阶段产物）
   clang -### hello.c
   ```

2. 对比三者的「粒度」：`(a)` 是抽象工序编号，`(b)` 多出了工具名和临时文件路径，`(c)` 是完整可执行命令。

**需要观察的现象：**
- `(a)` 会输出类似 `0: input, "hello.c", c` → `1: preprocessor, {0}, cpp-output` → `2: compiler, {1}, assembler` → `3: assembler, {2}, object` → `4: linker, {3}, image` 的树状结构（数字是 Action 编号，`{n}` 表示依赖第 n 个 Action）。
- `(b)` 会显示每个 Action 绑定到的工具链与工具（如 `clang`、`::Assemble`、`::Link`）以及中间临时文件名。
- `(c)` 是最终命令行，含一条 `-cc1` 编译命令和一条链接命令。

**预期结果：** 三个输出逐步具体化，恰好对应 Pipeline → Bind → Translate 三个阶段。（输出样式参考 `DriverInternals.rst` 第 144-265 行的样例；具体平台、临时文件名、链接器选择会因你的系统而异，**待本地验证**。）

**源码对照：** 看到 `(a)` 的 Action 树时，回头读 [Driver.cpp:1863-1864](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L1863-L1864) 的 `BuildActions`，理解每个编号背后对应一个 `Action` 对象；看到 `(c)` 的最终命令时，对照 [Driver.cpp:1873](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L1873) 的 `BuildJobs` 与 [Driver.cpp:2374-2376](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L2374-L2376) 的 `-###` 打印逻辑。

#### 4.2.5 小练习与答案

**练习 1：** `-###` 和 `-ccc-print-phases` 看到的东西有何本质区别？

**参考答案：** `-ccc-print-phases` 打印的是 **Action 树**（Pipeline 阶段产物，抽象工序，平台无关）；`-###` 打印的是 **最终 Command 列表**（Translate 阶段产物，含具体可执行路径、翻译后的参数、临时文件名），已经绑定到具体平台工具。前者抽象、后者具体。

**练习 2：** 如果用户运行 `clang --version`，Driver 会构造 Action 树吗？

**参考答案：** 不会。[Driver.cpp:1798-1799](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L1798-L1799) 中 `HandleImmediateArgs` 处理 `--version` 这类「立即参数」并返回 `false`，`BuildCompilation` 直接返回 `Compilation`，跳过 `BuildInputs`/`BuildActions`/`BuildJobs`，根本不构造任何工序。

**练习 3：** 为什么 Driver 要把「编译」单独做成一个 `-cc1` 命令，而不是直接在 Driver 里调前端函数？

**参考答案：** ①职责分离：Driver 关注命令行兼容与工序编排，cc1 关注纯粹的编译逻辑，分开后复杂度不互相污染；②可独立调用与测试：可以脱离 Driver 直接用 `clang -cc1 ...` 调前端，便于调试、复现问题；③进程模型灵活：可在同进程（integrated-cc1）或子进程中执行 cc1，兼顾性能与崩溃隔离。

## 5. 综合实践

**任务：用 `clang -###` 完整追踪一次多输入编译，并画出 Driver 的编排结果。**

1. 准备两个源文件：
   ```c
   // a.c
   int shared(void);
   int main(void) { return shared(); }
   ```
   ```c
   // b.c
   int shared(void) { return 7; }
   ```
2. 运行并记录输出：
   ```bash
   clang -### a.c b.c -o app
   clang -ccc-print-phases a.c b.c -o app
   ```
3. 完成以下分析（写在本子上即可）：
   - 列出 `-###` 输出中**所有**命令，标注每条属于哪个阶段（预处理/编译/汇编/链接）。
   - 指出哪几条命令是 `clang -cc1`，分别对应哪个输入文件。
   - 在 `-ccc-print-phases` 输出中，找到两条编译流水线在哪个 Action 编号处「汇合」（应该是 `linker` 节点，形如 `linker, {3, X}, image`）。
   - 用一句话说明：为什么有两个 `.c` 文件，`-cc1` 命令会出现两次，而链接命令只有一次？

**预期结论：** Driver 为每个 `.c` 输入独立构造一条 `Preprocess→Compile→Assemble` 的 Action 链（因此两条 `-cc1` 命令），最后用一条 `Link` 命令把两个 `.o` 合并成 `app`。这正体现了 Driver「按输入展开流水线、在链接处汇合」的编排本质。

## 6. 本讲小结

- Clang 命令分两层处理：**外层 Driver**（`clang_main` → `Driver`）负责命令行兼容与工序编排，**内层 cc1**（`cc1_main`）负责真正的前端编译，二者靠 `-cc1` 标志与 `Driver::CC1Main` 回调衔接。
- `clang_main` 在 [driver.cpp:271-278](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L271-L278) 判断：若 `Args[1]` 以 `-cc1` 开头则直接进 `ExecuteCC1Tool`→`cc1_main`，否则构造 Driver 编排执行。
- Driver 的编排分五阶段：**Parse → Pipeline（构造 Action）→ Bind（选 Tool）→ Translate（生成 Command）→ Execute**，对应三个递进数据结构 `Action` / `Tool 绑定` / `Command`。
- `BuildCompilation` 是编排总入口（[Driver.cpp:1463](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L1463)），内部依次做选 ToolChain、`HandleImmediateArgs`、`BuildInputs`、`BuildActions`、`BuildJobs`；`ExecuteCompilation` 是执行总入口（[Driver.cpp:2357](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L2357)）。
- 阶段枚举定义在 [Phases.h:17-25](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Phases.h#L17-L25)：`Preprocess / Precompile / Compile / Backend / Assemble / Link / IfsMerge`。
- 三个观测开关：`-ccc-print-phases`（Action 树）、`-ccc-print-bindings`（工具绑定）、`-###`（最终命令），从抽象到具体。

## 7. 下一步学习建议

本讲只到「Driver 把编译工序交给 cc1」为止，**没有进入 cc1 内部**。接下来按 u5 的顺序逐层深入前端：

- **u5-l2 词法分析 Lex 与预处理**：进入 cc1 的第一道工序——Lexer 如何切 Token、Preprocessor 如何展开宏与 `#include`（对应 `clang/lib/Lex/`）。
- **u5-l3 语法分析 Parse 与 AST 构建**：Parser 如何把 Token 流组织成 AST，认识 `Decl`/`Stmt`/`ASTContext`。
- **u5-l4 语义分析 Sema**：类型检查、名字查找、重载决议。
- **u5-l5 CodeGen：从 AST 到 LLVM IR**：cc1 的出口，把 AST 翻译成 `Module`（与 u3-l1 的 IR 层次结构接上）。

建议在进入 u5-l2 之前，先把本讲的「综合实践」做完——亲眼看一次 `-###` 输出，会对后续每一讲「cc1 内部某一道工序」在整体中的位置有更清晰的坐标感。
