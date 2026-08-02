# Driver 架构：解析命令行与构建阶段

## 1. 本讲目标

在 u1-l4 中，我们从**用户视角**看到了 `clang` 命令背后有一个 driver（命令行协调者）和一个 cc1（真正的编译器前端），并用 `-###` / `-ccc-print-phases` 观察了它们的边界。本讲我们将**钻进 driver 的源码内部**，搞清楚下面三件事：

1. `Driver` 这个类到底持有哪些状态、对外暴露哪些关键方法（它的“职责清单”）。
2. 一串命令行字符串是怎么被一步步拆成结构化的 `ArgList` 的（OptTable / ArgList / driver mode）。
3. 这些参数又是怎么被翻译成一张“编译阶段图”（Action 列表）的——也就是 `BuildActions` / `getFinalPhase` / `ConstructPhaseAction` 这条主线。

学完本讲，你应该能拿到任意一条 `clang` 命令，沿着源码说出：它会被解析成哪些参数、最终阶段（final phase）是什么、driver 会为它构造出哪些 Action。本讲**只到 Action 图为止**；把 Action 绑定到具体工具并生成可执行命令的 `BuildJobs`，留给下一讲 u2-l2。

## 2. 前置知识

- **driver 与 cc1 的分工**：`clang` 命令里其实住着两个角色。driver 模仿 GCC 的命令行行为，负责解析参数、推断目标平台、规划“要做什么”，但它本身并不真正编译代码；真正干活的编译器前端是 cc1（参见 u1-l4）。
- **编译阶段（phase）**：把一段源代码变成可执行文件，要顺序经过若干阶段。Clang 把它们抽象成 7 个枚举值：`Preprocess`（预处理）→ `Precompile`（预编译，如生成 PCH）→ `Compile`（编译，生成 LLVM bitcode/IR）→ `Backend`（后端，生成汇编）→ `Assemble`（汇编，生成目标文件）→ `Link`（链接，生成映像），外加一个特殊的 `IfsMerge`（接口桩合并）。这些枚举定义在 `Phases.h` 里，本讲会反复用到。
- **gcc 兼容性**：driver 的头号设计目标是“能直接替换 GCC 放进现有构建系统”，所以它要兼容大量 GCC 的命令行习惯（这也是它比“一个简单的命令行解析器”复杂得多的根本原因）。
- **OptTable / Arg / ArgList**：这是 LLVM 的 `llvm/Option` 库提供的通用命令行解析设施。简单说，每个选项（如 `-E`、`-c`）有一个抽象定义 `Option`，解析后得到轻量的 `Arg` 实例，`Arg` 们聚合成 `ArgList`。本讲会从 driver 的角度使用它们，不需要先读 LLVM 那一层。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/clang/Driver/Driver.h](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Driver.h) | `Driver` 类的声明，列出它持有的状态和所有关键方法。是本讲的“接口清单”。 |
| [lib/Driver/Driver.cpp](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp) | `Driver` 的全部实现，包括 `BuildCompilation`、`ParseArgStrings`、`BuildInputs`、`BuildActions`、`ConstructPhaseAction`、`getFinalPhase` 等。本讲的核心战场。 |
| [docs/DriverInternals.rst](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst) | 官方对 driver 内部设计的阐述，把 driver 的工作划分成 Parse / Pipeline / Bind / Translate / Execute 五个阶段。本讲把这份“概念阶段”对应到具体代码。 |
| [include/clang/Driver/Phases.h](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Phases.h) | 7 个编译阶段枚举 `phases::ID` 的定义。 |
| [lib/Driver/Types.cpp](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Types.cpp) 与 [include/clang/Driver/Types.def](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Types.def) | 每种文件类型（C、汇编、目标文件……）绑定到哪些阶段；`getCompilationPhases` 的实现。 |
| [include/clang/Driver/Action.h](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Action.h) | `Action` 类层次：`InputAction`、各种 `JobAction`（`PreprocessJobAction`、`CompileJobAction`、`AssembleJobAction`、`LinkJobAction` 等）。 |
| [include/clang/Options/Options.td](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Options/Options.td) | 用 TableGen 声明所有命令行选项（`-E`、`-S`、`-c`、`--target=` 等），以及每个选项对哪些“driver 模式”可见。 |
| [tools/driver/driver.cpp](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp) | `clang` 可执行程序的入口 `clang_main`，演示 driver 如何被创建、驱动、执行。 |

## 4. 核心概念与源码讲解

### 4.1 Driver 类：编译协调总管

#### 4.1.1 概念说明

`Driver` 是 driver 层的“总管类”。官方文档 [docs/DriverInternals.rst:93-104](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst) 用一张架构图描述了它的位置：driver 被设计成“完全吸收 gcc 可执行程序的功能”，即它不必再委托真正的 gcc 去完成子任务，而是自己就能规划出全部编译步骤，并直接调用 cc1 等子进程。

理解 `Driver` 类要抓住三个要点：

1. **它是“无状态的总管”**：官方文档明确指出，“Driver 本身在构造 Compilation 的过程中应保持不变（invariant）；一个 IDE 完全可以长期持有一个 Driver 实例，反复用它处理整个构建过程”（见 [docs/DriverInternals.rst:295-302](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst)）。真正“每次编译各不相同”的信息被装进另一个对象 `Compilation`。
2. **它持有配置型状态**：诸如被调用时的名字、目标三元组（target triple）、sysroot、resource 目录、各种 `CC_PRINT_*` 开关等，这些在一次构建里基本不变。
3. **它的对外能力是一组方法**：`BuildCompilation`（总入口）、`ParseArgStrings`/`BuildInputs`/`BuildActions`/`BuildJobs`（管线各阶段）、`ExecuteCompilation`（执行）、`getFinalPhase`/`ConstructPhaseAction`（阶段判定与构造）。

#### 4.1.2 核心流程

driver 的完整工作被官方文档划成五个阶段（[docs/DriverInternals.rst:106-291](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst)）。本讲对应到代码，整体流程是：

```text
clang_main(args)
   └── Driver 构造（设置名字、目标、模式）
   └── Driver::BuildCompilation(args)        ← driver 的总入口，内部依次：
         1. ParseArgStrings        ── Parse 阶段：字符串 → ArgList
         2. getToolChain           ── 选定目标平台的工具链
         3. new Compilation(...)   ── 创建“本次编译”对象
         4. HandleImmediateArgs    ── 处理 -v / --help / --version 等即时参数
         5. BuildInputs            ── 确定“有哪些输入文件、各是什么类型”
         6. BuildActions           ── Pipeline 阶段：构造 Action 图（本讲重点）
         7. BuildJobs              ── Bind+Translate 阶段：Action → 具体命令（下一讲）
   └── Driver::ExecuteCompilation(C)         ── Execute 阶段：真正跑命令
```

本讲聚焦 1、5、6 三步，即从“字符串”到“Action 图”。第 7 步（Bind/Translate）和 Execute 留给 u2-l2。

#### 4.1.3 源码精读

**`Driver` 类的声明**从 [include/clang/Driver/Driver.h:95](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Driver.h#L95) 开始，注释一句话点明它的定位：“封装从一组类 gcc 命令行参数构造编译过程的逻辑”。

它内部用一个枚举记录自己的**驱动模式**（[include/clang/Driver/Driver.h:100-107](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Driver.h#L100-L107)）：

```cpp
enum DriverMode { GCCMode, GXXMode, CPPMode, CLMode, FlangMode, DXCMode } Mode;
```

这个 `Mode` 决定了 driver 模仿谁：`gcc`、`g++`、`cpp`、MSVC 的 `cl.exe`、`flang` 还是 `dxc.exe`。`clang`、`clang++`、`clang-cl` 这些不同的程序名，本质上就是同一个可执行文件以不同 `Mode` 运行（参见 u1-l3 提到的符号链接）。模式查询函数如 `CCCIsCXX()`、`IsCLMode()` 都只是判断 `Mode`（[include/clang/Driver/Driver.h:222-238](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Driver.h#L222-L238)）。

**构造函数**在 [lib/Driver/Driver.cpp:200-247](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L200-L247)，它接收“可执行程序路径、目标三元组、诊断引擎”三件套，初始化默认状态（默认 `GCCMode`、默认 sysroot、默认链接器 `CLANG_DEFAULT_LINKER` 等），并从可执行程序路径推算出名字、所在目录和 resource 目录。注意：构造时**并不解析命令行**——命令行要到 `BuildCompilation` 里才处理。这正是“Driver 无状态、可复用”设计的体现。

**模式是怎么从程序名确定的**？入口函数 `getDriverMode`（[lib/Driver/Driver.cpp:7531-7544](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L7531-L7544)）先找命令行里有没有显式的 `--driver-mode=X`，没有就从程序名里解析（比如 `clang++` 推出 `g++` 模式）。结果通过 `setDriverMode`（[lib/Driver/Driver.cpp:249-259](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L249-L259)）写回 `Mode`。

**总入口 `BuildCompilation`** 声明在 [include/clang/Driver/Driver.h:472](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Driver.h#L472)，实现是本讲最长的一个函数，[lib/Driver/Driver.cpp:1463-1879](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L1463-L1879)。它把上面流程图里的 1～7 步串起来，是追踪 driver 内部执行的“主路径”。它的返回值是一个 `Compilation *`，driver 后续就用这个对象来执行。

**它在 `clang_main` 中的调用**见 [tools/driver/driver.cpp:388](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L388) 与 [tools/driver/driver.cpp:419](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L419)：先 `BuildCompilation` 得到 `Compilation`，再 `ExecuteCompilation` 执行。这一对调用就是 driver 在主程序里的全部“出场”。

#### 4.1.4 代码实践

**实践目标**：在源码里定位 driver 的执行主路径，建立“函数→职责”的映射。

**操作步骤**：

1. 打开 [tools/driver/driver.cpp:388](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L388)，确认 `clang_main` 用 `TheDriver.BuildCompilation(Args)` 构造编译对象。
2. 跳进 [lib/Driver/Driver.cpp:1463](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L1463) 的 `BuildCompilation`，依次找到下面这些调用并记录它们的行号：
   - `ParseArgStrings(...)`（参数解析）
   - `getToolChain(...)`（选工具链）
   - `new Compilation(...)`（创建编译对象）
   - `HandleImmediateArgs(*C)`（即时参数）
   - `BuildInputs(...)`（输入识别）
   - `BuildActions(...)` / `BuildUniversalActions(...)`（构造 Action 图）
   - `BuildJobs(*C)`（绑定工具，下一讲）
3. 对照本讲 4.1.2 的流程图，给每个调用标注它属于官方五阶段（Parse/Pipeline/Bind/Translate/Execute）中的哪一段。

**需要观察的现象**：你会发现 `BuildCompilation` 几乎是“线性脚本式”地把五阶段串起来；`BuildJobs` 是它在本讲范围内调用的**最后一个**实质性步骤，再往后就只是 `return C`。

**预期结果**：你能画出一张“`BuildCompilation` 内部调用序列”的时序表，并能解释为什么文档说 driver 适合被 IDE 长期复用（因为这些状态都进了 `Compilation`，而不是污染 `Driver`）。

#### 4.1.5 小练习与答案

**练习 1**：`Driver` 构造函数里为什么不解析命令行？这种设计带来什么好处？

> 参考答案：构造函数只设置与“被谁调用”有关的不变状态（名字、目录、目标、模式、各种默认值）。命令行属于“这一次具体要编译什么”，每次都不同，所以放到 `BuildCompilation` 里处理，并把每次的结果装入独立的 `Compilation` 对象。好处是同一个 `Driver` 实例可以被反复调用 `BuildCompilation` 来处理多个不同的编译任务，适合 IDE 长期持有。

**练习 2**：`Driver` 内部的 `Mode` 枚举有 6 个值。`clang-cl` 运行时 `Mode` 是哪个？它和普通 `clang` 的行为差异主要由哪个方法体现？

> 参考答案：是 `CLMode`。行为差异主要由 `getOptionVisibilityMask`（见 4.2.3）体现——不同模式下，driver 只“看得见”属于自己的那部分选项（CL 模式只看 `CLOption`，普通 gcc 模式看 `ClangOption`）。

---

### 4.2 参数解析：OptTable、ArgList 与 driver mode

#### 4.2.1 概念说明

这一步对应官方文档的 **Parse 阶段**（[docs/DriverInternals.rst:111-156](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst)）。目标很简单：把一串命令行**字符串**（如 `["-E", "-I", "foo", "t.c"]`）分解成一组结构化的 `Arg` 对象，每个 `Arg` 都对应一个抽象的 `Option` 定义。

几个关键设计（来自文档）：

- **每个 `Arg` 对应唯一的 `Option`**：例如 `-Ifoo`（ JoinedArg）和 `-I foo`（SeparateArg）是两个不同的 `Arg` 实例，但都指向同一个 `OPT_I` 选项定义。
- **`Arg` 很轻量**：它一般**不拷贝参数字符串**，而是在外层 `ArgList`（持有原始字符串向量）里存一个索引。这避免了大量字符串复制，是文档强调的“低开销”原则。
- **后续阶段几乎不再做字符串处理**：解析完之后，大家都用选项 ID（如 `options::OPT_E`）来查询参数。
- **未识别参数会被报告**：文档专门讨论了“未使用参数警告”（[docs/DriverInternals.rst:337-358](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst)），driver 会给拼错的选项提示“did you mean”。

#### 4.2.2 核心流程

Clang 的选项不是手写代码维护的，而是用 **TableGen** 在 `Options.td` 里声明，再由 `llvm-tblgen` 生成 C++ 代码（参见 u1-l2 提到的 llvm-tblgen 依赖）。每个选项除了名字，还带两类元数据：

- **Visibility（可见性）**：标记这个选项在哪些“driver 模式”下可见。例如 `ClangOption`（默认，gcc 风格 driver 可见）、`CLOption`（仅 clang-cl）、`CC1Option`（仅 `-cc1` 前端）、`FlangOption`、`DXCOption` 等（[include/clang/Options/Options.td:77-98](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Options/Options.td#L77-L98)）。
- **Flags**：如 `NoXarchOption`（不能被 `-Xarch_` 透传）、`Group<Action_Group>`（归类）等。

解析的核心流程：

```text
命令行字符串 argv
   │
   ▼
getDriverMode(程序名, argv)        ── 先定 driver 模式（影响可见性掩码）
   │
   ▼
getOptionVisibilityMask(模式)      ── 算出本模式“看得见”哪些选项
   │
   ▼
OptTable::ParseArgs(argv, 掩码)    ── 用掩码过滤，把字符串切成 Arg 列表
   │
   ▼
InputArgList                       ── 结构化参数（后续都用选项 ID 查询）
```

#### 4.2.3 源码精读

**选项的声明（示例）**：以 `-E` 为例（[include/clang/Options/Options.td:766-769](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Options/Options.td#L766-L769)）：

```cpp
def E : Flag<["-"], "E">, Flags<[NoXarchOption]>,
  Visibility<[ClangOption, CC1Option, FlangOption, FC1Option]>,
  Group<Action_Group>,
    HelpText<"Only run the preprocessor">;
```

这说明 `-E` 是一个无参 `Flag`，对 gcc 风格 driver（`ClangOption`）和 `-cc1`（`CC1Option`）等可见，归入 `Action_Group`（注意：u1-l4 提到 `Action_Group` 正是控制流水线停站的选项组）。类似地，`-S`（[Options.td:876](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Options/Options.td#L876)）、`-c`（[Options.td:1243](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Options/Options.td#L1243)）、`-emit-llvm`（[Options.td:1611](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Options/Options.td#L1611)）、`--target=`（[Options.td:6814](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Options/Options.td#L6814)）都在这里声明。

**可见性掩码的计算**：`getOptionVisibilityMask`（[lib/Driver/Driver.cpp:7473-7483](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L7473-L7483)）根据当前 `Mode` 返回不同的可见性集合——CL 模式返回 `CLOption`，DXC 模式返回 `DXCOption`，flang 模式返回 `FlangOption`，否则返回 `ClangOption`。这就是“不同程序名 = 不同选项集”的实现机制。

**解析主函数 `ParseArgStrings`**（[lib/Driver/Driver.cpp:265-351](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L265-L351)）做了三件事：

1. **真正解析**：调用底层 `getOpts().ParseArgs(...)`，把字符串切成 `InputArgList`（[Driver.cpp:273-274](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L273-L274)）。
2. **检查缺参数**：对需要值却没给值的选项报 `err_drv_missing_argument`（[Driver.cpp:277-283](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L277-L283)）。
3. **处理未知选项 + “did you mean”**：对 `OPT_UNKNOWN` 的参数，用 `findNearest` 找最接近的合法选项并给出建议（[Driver.cpp:304-336](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L304-L336)）。这里有个很实用的细节：如果用户写了一个只有 `-cc1` 才认的选项，driver 会建议用 `-Xclang <选项>` 透传给前端（[Driver.cpp:318-322](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L318-L322)）。

**它在 `BuildCompilation` 里的调用位置**：`BuildCompilation` 一开头就先用 `getDriverMode` 定模式（[Driver.cpp:1472-1474](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L1472-L1474)），然后才调 `ParseArgStrings`（[Driver.cpp:1480-1481](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L1480-L1481)）。顺序很重要：模式必须先定，因为模式决定了可见性掩码，进而决定哪些字符串被认作合法选项。

**从 `InputArgList` 到 `DerivedArgList`**：解析得到的是“原始参数” `InputArgList`；driver 还会通过 `TranslateInputArgs`（[Driver.cpp:1740](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L1740)，实现见 [Driver.cpp:462](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L462) 起）做“标准参数翻译”，产出 `DerivedArgList`。文档专门提到 ToolChain 也可能再做一次翻译（[docs/DriverInternals.rst:317-335](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst)），但那是下一讲 ToolChain 的范畴。

#### 4.2.4 代码实践

**实践目标**：用 `-###` 观察 Parse 阶段的产物，并把观察到的输出对应到源码。

**操作步骤**：

1. 写一个最小 C 文件 `t.c`（内容随意，如 `int main(void){return 0;}`）。
2. 运行（待本地验证）：

   ```bash
   clang -### -Xarch_i386 -fomit-frame-pointer -Wa,-fast -Ifoo -I foo t.c
   ```

3. 对照文档给出的预期输出（[docs/DriverInternals.rst:144-152](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst)），其中 `-Ifoo` 和 `-I foo` 被解析成两个 `Arg`，但都指向同一个 `OPT_I` 选项。
4. 故意拼错一个选项，例如 `clang -### -fsyntax-onl t.c`（少了个 `y`），观察 driver 是否给出 “did you mean” 建议，并把它对应到 [Driver.cpp:304-336](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L304-L336) 的 `findNearest` 逻辑。

**需要观察的现象**：`-###` 会让 driver 打印它“打算执行”的完整命令（但不真正执行）；其中能看到参数已被规范化。拼错选项时应看到建议性诊断。

**预期结果**：你能解释“为什么 `-Ifoo` 和 `-I foo` 最终等价”——它们解析成不同形态的 `Arg`，但引用同一个 `Option`；后续阶段一律用 `options::OPT_I` 查询，所以书写形式不同不影响语义。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `BuildCompilation` 要在 `ParseArgStrings` **之前**先调 `getDriverMode`？

> 参考答案：因为 driver 模式决定了 `getOptionVisibilityMask`，进而决定 `OptTable::ParseArgs` 用哪个可见性掩码来识别选项。如果先解析再定模式，clang-cl 专属的 `/` 风格选项或 dxc 专属选项就会被当成未知参数报错。

**练习 2**：用户在普通 `clang`（gcc 模式）下写了一个只有 `-cc1` 才认识的选项，driver 会怎么处理？对应哪段代码？

> 参考答案：driver 会在 `ParseArgStrings` 里把它判为 `OPT_UNKNOWN`，然后由于 `findExact(... CC1Option)` 命中，报 `err_drv_unknown_argument_with_suggestion` 并建议用 `-Xclang <选项>` 透传（[Driver.cpp:318-322](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L318-L322)）。

---

### 4.3 BuildActions 与阶段管线：从输入到 Action 图

#### 4.3.1 概念说明

这一步对应官方文档的 **Pipeline 阶段**（[docs/DriverInternals.rst:158-219](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst)）。目标是：给定已识别的输入文件和参数，构造出一棵“要做哪些事”的 **Action 图**。每个 `Action` 代表图中的一个节点（一条边），通常表示“用某个工具把输入变换成输出”。

`Action` 的种类在 [include/clang/Driver/Action.h:56-84](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Action.h#L56-L84) 列出，分两大类：

- **特殊 Action**：`InputAction`（把一个输入参数适配成 Action 的起点）、`BindArchAction`（为子树绑定一个目标架构，用于通用二进制/universal build）。
- **JobAction**（真正对应一个任务的）：`PreprocessJobAction`、`PrecompileJobAction`、`CompileJobAction`、`BackendJobAction`、`AssembleJobAction`、`LinkJobAction` 等。

Pipeline 阶段要回答两个核心问题：

1. **最终走到哪个阶段（final phase）？**——由 `-E`/`-S`/`-c`/`-fsyntax-only` 等选项决定。
2. **对每个输入文件，要走哪些阶段？**——由文件类型绑定到的“阶段集合”与 final phase 取交集决定。

#### 4.3.2 核心流程

**先看阶段枚举与类型绑定**。7 个阶段定义在 [include/clang/Driver/Phases.h:17-25](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Phases.h#L17-L25)：`Preprocess, Precompile, Compile, Backend, Assemble, Link, IfsMerge`（顺序就是编译的自然顺序）。

每种文件类型绑定到“可能经历的阶段集合”，用位集 `PhasesBitSet` 表示（[lib/Driver/Types.cpp:20-34](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Types.cpp#L20-L34)），具体值在 `Types.def` 里随类型声明。几个关键例子：

| 类型 | 绑定阶段（来自 Types.def） |
| --- | --- |
| `c`（`.c` 源文件） | Preprocess, Compile, Backend, Assemble, Link |
| `cpp-output`（`.i` 预处理过的 C） | Compile, Backend, Assemble, Link |
| `assembler`（`.s` 汇编） | Assemble, Link |
| `object`（`.o` 目标文件） | Link |

（见 [include/clang/Driver/Types.def:38-39](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Types.def#L38-L39) 的 `c`/`cpp-output`、[Types.def:83](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Types.def#L83) 的 `assembler`、[Types.def:118](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Types.def#L118) 的 `object`。）

**final phase 的确定**：`getFinalPhase`（[lib/Driver/Driver.cpp:356-412](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L356-L412)）按“从最早阶段往后匹配”的优先级判定：

```text
-E / -M / -MM / CPP 模式      → Preprocess
--precompile / -extract-api   → Precompile
-fsyntax-only / --analyze     → Compile
-S                            → Backend
-c                            → Assemble
-emit-interface-stubs         → IfsMerge
（都没有）                      → Link      ← 默认：一路做到链接
```

**实际要走的阶段 = 该类型的阶段集合 ∩ [起点..final phase]**。这件事由 `getCompilationPhases` 完成（[lib/Driver/Types.cpp:418-427](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Types.cpp#L418-L427)）：遍历从 0 到 `LastPhase` 的每个阶段，若该类型的位集包含它，就加入列表。

```text
对每个输入文件 I：
  PL = getCompilationPhases(I 的类型, finalPhase)   ← 该文件实际要走的有序阶段
  current = new InputAction(I)                       ← Action 链的起点
  for Phase in PL:
      current = ConstructPhaseAction(Phase, current) ← 把 current 包成“做 Phase 的 JobAction”
  顶层 Action = current                              ← 一条链的终点
```

多条链的最后，如果有多个目标文件需要链接，还会再追加一个 `LinkJobAction`（或相关链接动作），把各链的终点作为它的输入——于是若干条线性的 Action 链在链接处汇合成一张图。

#### 4.3.3 源码精读

**输入识别 `BuildInputs`**（[lib/Driver/Driver.cpp:3126](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L3126) 起）负责把命令行里的“输入类参数”（`OPT_INPUT`，即没有 `-` 前缀的文件名）识别成 `(类型, Arg)` 列表。类型推断规则（[Driver.cpp:3169-3225](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L3169-L3225)）：优先用 `-x` 显式指定的类型，否则按扩展名查（`TC.LookupTypeForExtension`），查不到则根据模式兜底（C/C++/Object）。`clang++` 还会把某些 C 文件当 C++ 处理（[Driver.cpp:3223](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L3223)）。

**Action 图构造主函数 `BuildActions`**（[lib/Driver/Driver.cpp:4530](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L4530) 起）是本讲的核心。关键片段：

1. 先调 `handleArguments` 处理一批影响管线开关的参数（[Driver.cpp:4539](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L4539)，实现在 [Driver.cpp:4322](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L4322)）。
2. 对每个输入，算出它要走的阶段列表 `PL`（[Driver.cpp:4568](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L4568)），并创建链的起点 `InputAction`（[Driver.cpp:4575](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L4575)）。
3. 进入阶段循环 `for (phases::ID Phase : PL)`（[Driver.cpp:4597](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L4597)），其中：
   - 若到了 `Link` 阶段，把这个输入的终点收进 `LinkerInputs`（[Driver.cpp:4607-4617](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L4607-L4617)）——链接是“多入一处”的特殊阶段。
   - 否则用 `ConstructPhaseAction` 把当前 Action 包成对应阶段的 JobAction（[Driver.cpp:4640-4642](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L4640-L4642)）。
4. 所有输入处理完后，若 `LinkerInputs` 非空，再创建一个总的链接动作 `LinkJobAction`（[Driver.cpp:4699-4727](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L4699-L4727)）。

**逐阶段构造 `ConstructPhaseAction`**（[lib/Driver/Driver.cpp:5278-5448](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L5278-L5448)）是一个大的 `switch (Phase)`，把抽象阶段翻译成具体的 `JobAction` 子类：

| `Phase` 分支 | 关键产物 | 行号 |
| --- | --- | --- |
| `Preprocess` | `PreprocessJobAction`（输出预处理后的类型） | [5300-5323](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L5300-L5323) |
| `Precompile` | `PrecompileJobAction`（PCH/模块） | [5324-5369](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L5324-L5369) |
| `Compile` | `CompileJobAction` / `AnalyzeJobAction`（依 `-fsyntax-only`/`--analyze`/`-emit-ast`/`-emit-cir` 等决定输出类型） | [5370-5391](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L5370-L5391) |
| `Backend` | `BackendJobAction`（依 `-emit-llvm`/LTO 决定输出 IR/汇编） | [5392-5431](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L5392-L5431) |
| `Assemble` | `AssembleJobAction`（输出 `TY_Object`） | [5432-5445](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L5432-L5445) |

这里有个细节能解释 u1-l4 里“`-fsyntax-only`/`-emit-llvm` 控制流水线停站”的底层原因：在 `Compile` 分支里，`-fsyntax-only` 会让输出类型变成 `TY_Nothing`（[Driver.cpp:5371-5372](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L5371-L5372)）；在 `Backend` 分支里，`-emit-llvm` 会让输出类型变成 `TY_LLVM_IR`/`TY_LLVM_BC`（[Driver.cpp:5423-5428](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L5423-L5428)）。输出类型决定了后续阶段是否能继续，从而实现“在某个阶段停住”。

**`BuildCompilation` 把这些串起来**（[Driver.cpp:1861-1873](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L1861-L1873)）：先按是否 MachO 目标选择 `BuildUniversalActions`（多架构）或 `BuildActions`（单架构），随后若用户传了 `-ccc-print-phases` 就调 `PrintActions` 打印整张图并提前返回，否则继续 `BuildJobs`。`PrintActions`（[Driver.cpp:2924-2928](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L2924-L2928)）正是我们在实践里要观察的 Action 图的“出口”。

#### 4.3.4 代码实践

**实践目标**：用 `-ccc-print-phases` 看到 driver 为一条命令生成的 Action 列表，并把它**逐条对应回源码**（`getFinalPhase` → `getCompilationPhases` → `ConstructPhaseAction`）。这是本讲的主实践，直接落实规格里的实践任务。

**操作步骤**：

1. 准备三个小文件（待本地验证）：

   ```bash
   echo 'int main(void){return 0;}' > t.c
   echo '   .globl _start' > t.s        # 一个最小汇编文件
   ```

2. 运行下面四条命令，分别记录每条的 Action 图输出：

   ```bash
   clang -ccc-print-phases t.c                      # 默认：做到 Link
   clang -ccc-print-phases -c t.c                   # -c：做到 Assemble
   clang -ccc-print-phases -S t.c                   # -S：做到 Backend
   clang -ccc-print-phases -fsyntax-only t.c        # 只做 Compile
   clang -ccc-print-phases -x c t.c -x assembler t.s # 混合输入，看链接汇合
   ```

3. 对第 5 条混合输入的输出（文档里有完全对应的示例，[docs/DriverInternals.rst:179-190](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst)），逐一回答：
   - `t.c` 那条链有几个 Action？分别对应 `ConstructPhaseAction` 的哪个 `case`？
   - `t.s` 那条链为什么没有 `preprocessor`/`compiler`？（提示：看 [Types.def:83](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Types.def#L83) 的 `assembler` 绑定了哪些阶段。）
   - 最后一行的 `linker, {3, 5}, image` 是怎么来的？（提示：对应 [Driver.cpp:4699-4727](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L4699-L4727) 的 `LinkJobAction` 创建，输入是前面各链的终点。）

4. 切换 final phase 再验证：用 `-c` 时，`getFinalPhase` 返回 `Assemble`（[Driver.cpp:398-399](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L398-L399)），于是 `t.c` 的链应在 `assembler, {...}, object` 处终止，**不再有 `linker`**——确认你的输出与此一致。

**需要观察的现象**：同一份 `t.c`，随着 `-c` / `-S` / `-fsyntax-only` 的切换，Action 链的长度（终点）不同；混合输入时两条链在 `linker` 处汇合。这些正是 `getFinalPhase` 与 `getCompilationPhases` 取交集的结果。

**预期结果**：你能拿着任意一条命令的 `-ccc-print-phases` 输出，指出每个 Action 由 `ConstructPhaseAction` 的哪个 `case` 生成、为什么链在某处终止。这就完成了“给定一条命令，描述它如何生成 Action 列表”的实践目标。

> 说明：以上命令的行为以你本地构建的 clang 为准；若某些输出的行号编号略有不同（例如 `0:` `1:` 的具体编号），不影响对结构的理解。如果暂时没有可用 clang，可改为“源码阅读型实践”：对照 [docs/DriverInternals.rst:179-213](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst) 给出的两份示例输出，逐行标注它们对应的代码位置。

#### 4.3.5 小练习与答案

**练习 1**：对 `t.c` 执行 `clang -ccc-print-phases -fsyntax-only t.c`，Action 链应该到哪一步停止？为什么不会再有 `backend`/`assembler`/`linker`？

> 参考答案：链应到 `compiler` 停止（输出类型为 `TY_Nothing`）。原因有二：一是 `getFinalPhase` 因 `-fsyntax-only` 返回 `Compile`（[Driver.cpp:380-391](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L380-L391)），所以 `getCompilationPhases` 只会枚举到 `Compile` 为止；二是即便到了 `Compile`，`ConstructPhaseAction` 也把输出类型设成 `TY_Nothing`（[Driver.cpp:5371-5372](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L5371-L5372)），后续阶段无事可做。

**练习 2**：为什么 `assembler`（`.s`）输入的链里没有 `preprocessor` 和 `compiler`？

> 参考答案：因为 `Types.def` 中 `assembler` 类型只绑定了 `Assemble` 和 `Link` 两个阶段（[Types.def:83](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/Types.def#L83)）。`getCompilationPhases` 用该类型的 `PhasesBitSet` 与 `[0..finalPhase]` 取交集，自然不含 `Preprocess`/`Compile`。

**练习 3**：`ConstructPhaseAction` 的 `Backend` 分支里，`-emit-llvm` 会把输出类型设成 `TY_LLVM_IR` 或 `TY_LLVM_BC`。这对 Action 图后续阶段有什么影响？

> 参考答案：它让 `BackendJobAction` 的产出是 LLVM IR/bitcode 而不是汇编。而 `Assemble` 分支开头有一处特判：`if (Phase == phases::Assemble && Input->getType() != types::TY_PP_Asm) return Input;`（[Driver.cpp:5286-5287](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L5286-L5287)）——当输入不是汇编时直接跳过汇编阶段。所以 `-emit-llvm` 实际上让流水线停在 IR，不再走汇编。

## 5. 综合实践

把本讲三个模块串起来，做一次“命令→源码”的完整追踪。

**任务**：解释下面这条命令从字符串到 Action 图的全过程。

```bash
clang -ccc-print-phases --target=aarch64-linux -c -O2 foo.c bar.c
```

**要求**：

1. **Parse**：指出 driver 会先调哪个函数定模式、再调哪个函数解析参数（对应 4.2.3）。说明 `-c`、`--target=`、`-O2` 分别对应 Options.td 里的哪个选项定义（`OPT_c`、`OPT_target`，`-O2` 属于 `OPT_O_Group`）。
2. **final phase**：根据 `getFinalPhase`（[Driver.cpp:356-412](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L356-L412)）判断最终阶段是哪个（提示：`-c` → `Assemble`）。
3. **Pipeline**：在 `BuildActions`（[Driver.cpp:4530](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L4530)）里，两个 `.c` 输入会各自走 `input → preprocessor → compiler → backend → assembler → object` 的链；因为 final phase 是 `Assemble`，所以**不会有 `linker`**。画出这张 Action 图。
4. **验证**：用 `clang -ccc-print-phases -c foo.c bar.c` 实际运行（待本地验证），与你画的图对照。

**预期产出**：一份包含四段的简短报告，证明你能把“用户敲的一行命令”沿着 `clang_main → BuildCompilation → ParseArgStrings → getFinalPhase → BuildInputs → BuildActions → ConstructPhaseAction → PrintActions` 这条主线，一路讲清楚到 Action 图为止。

## 6. 本讲小结

- `Driver` 是 driver 层的无状态总管：构造期只设不变状态（名字、目标、模式、目录），命令行解析与每次编译的细节都放进独立的 `Compilation` 对象，因此可被 IDE 长期复用。它的总入口是 `BuildCompilation`。
- 官方把 driver 工作划成 Parse / Pipeline / Bind / Translate / Execute 五段；本讲覆盖 **Parse**（`ParseArgStrings`）与 **Pipeline**（`BuildInputs` + `BuildActions`），Bind/Translate/Execute 留给 u2-l2。
- 命令行解析基于 TableGen 声明的 `Options.td`：每个选项带 Visibility 元数据，`getOptionVisibilityMask` 按 driver 模式（gcc/cl/flang/dxc）选出可见选项集，`OptTable::ParseArgs` 据此把字符串切成 `InputArgList`，并对未知选项给出 “did you mean” 建议。
- 最终阶段由 `getFinalPhase` 按 `-E`/`-S`/`-c`/`-fsyntax-only` 等优先级判定；每个文件实际要走的阶段 = 该类型在 `Types.def` 里绑定的阶段集合 与 `[0..finalPhase]` 的交集，由 `getCompilationPhases` 计算。
- `BuildActions` 为每个输入建一条 Action 链（起点 `InputAction`），逐阶段用 `ConstructPhaseAction` 包成对应的 `JobAction`；多个目标的链在链接处汇合成 `LinkJobAction`。`-ccc-print-phases` 是观察这张图的官方出口。
- `-fsyntax-only`/`-emit-llvm` 之所以能让流水线“停站”，本质是 `ConstructPhaseAction` 把输出类型设成 `TY_Nothing`/`TY_LLVM_*`，使后续阶段无事可做。

## 7. 下一步学习建议

本讲到 **Action 图**为止，这些 Action 还只是“要做哪些事”的抽象描述，并未绑定到具体工具（比如“编译”到底是调 clang 的 cc1 还是外部 gcc？）。下一讲 **u2-l2《ToolChain、Action、Job 与 Compilation》** 将进入 Bind 与 Translate 阶段：

- 阅读 [include/clang/Driver/ToolChain.h](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Driver/ToolChain.h)，看 ToolChain 如何根据目标三元组选出具体工具；
- 阅读 [lib/Driver/Driver.cpp:5511](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Driver/Driver.cpp#L5511) 起的 `BuildJobs`，看 Action 如何被绑定成可执行的 `Command`；
- 之后 **u2-l3** 会跨过 driver 与 cc1 的边界，进入真正的编译器前端入口 `cc1_main`。

建议在进入下一讲前，先把本讲的 `-ccc-print-phases` 实践跑通，确保你能熟练地把一条命令映射成 Action 图——这是理解后续“Action→Job→执行”的前提。
