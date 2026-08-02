# 用 clang 编译第一个程序：命令行与编译阶段

## 1. 本讲目标

在前三讲里，我们知道了 Clang 是什么、怎么构建、源码如何组织。本讲回到最朴素的问题：**在命令行敲下一句 `clang hello.cpp` 之后，到底发生了什么？**

学完本讲，你应当能够：

- 用 `clang` 完成一次完整的编译，并能停在任意一个中间阶段（预处理、编译、汇编）查看产物。
- 说清楚 **driver**（命令行协调者）和 **cc1**（真正的前端）的分工与边界。
- 用 `-###`、`-v`、`-ccc-print-phases` 等选项观察 driver 内部构建出的「编译流水线」。
- 理解 `-E`、`-S`、`-c`、`-emit-llvm`、`-fsyntax-only` 这些高频选项各自让流水线停在哪儿。

本讲是「用户视角」的最后一站；从下一讲（u2）开始，我们将钻进 driver 与 cc1 的源码内部。

## 2. 前置知识

在开始前，请确认你已经理解 u1-l1、u1-l2、u1-l3 建立的几个概念：

- **前端流水线**：Clang 前端把源码加工成 LLVM IR，内部依次是 Lex（词法）→ Parse（语法）→ Sema（语义）→ CodeGen（生成 IR）。LLVM 后端再负责优化与生成机器码。
- **库优先**：Clang 被拆成 `lib/` 下三十多个子库；`tools/driver/` 下的代码只是把这些库组装成可执行的 `clang` 命令。
- **GCC 兼容**：Clang 的命令行设计刻意模仿 GCC，目的是让用户能「直接替换」构建系统里的 `gcc`。

如果你还不熟悉「Token」「AST」「IR」这些词，请先回看 u1-l1 的术语表。本讲会用到，但不会重复展开。

> 小提示：本讲的命令示例假设你已经按 u1-l2 构建出了可用的 `clang`（在 `build/bin` 下）。如果暂时没有构建，也可以用系统自带的 `clang` 跟做大部分实践，只是某些路径与版本号会不同。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [tools/driver/driver.cpp](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp) | `clang` 可执行程序的入口（`clang_main`），是 driver 库的一层薄包装；负责识别 `-cc1`、装配 `Driver` 并执行编译。 |
| [docs/DriverInternals.rst](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst) | 官方 driver 设计文档，讲清楚 driver 的目标与五大内部阶段。 |
| [docs/UsersManual.md](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/UsersManual.md) | 用户手册，定义了 Lexer/Preprocessor/Parser/Sema/Frontend/Backend 等术语，是理解「编译阶段」的权威出处。 |
| [include/clang/Options/Options.td](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Options/Options.td) | （补充）所有命令行选项的 TableGen 定义源头，`-E`/`-S`/`-c` 等选项的帮助文本就写在这里。 |
| [tools/driver/cc1_main.cpp](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/cc1_main.cpp) | （补充）`-cc1` 子工具的入口 `cc1_main`，也就是「真正的编译器前端」的程序入口。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**编译阶段**、**driver 与 cc1 分工**、**常用命令行选项**。

### 4.1 编译阶段

#### 4.1.1 概念说明

把一段 C/C++ 源码变成可执行程序，经典上要经过这几个阶段：

1. **预处理（Preprocess）**：处理 `#include`、`#define`、`#if` 等，做文本级的宏展开和文件包含，产出一个「展开后的纯源码」。
2. **编译（Compile）**：这就是 Clang 前端的核心工作——Lex→Parse→Sema→CodeGen，把源码翻译成 LLVM IR。
3. **汇编（Assemble）**：把 IR（经 LLVM 后端）翻译成机器码，打包成目标文件（`.o`）。
4. **链接（Link）**：把一个或多个目标文件与库合并成最终的可执行映像（如 `a.out`）。

需要特别区分「前端 / 中端 / 后端」这三个常被混用的词。用户手册给了精确定义：

- **Frontend（前端）**：Lexer、Preprocessor、Parser、Sema 以及 LLVM IR 代码生成部分。
- **Middle-end（中端）**：后端中负责（通常与目标无关的）优化的那部分，发生在汇编生成之前。
- **Backend（后端）**：LLVM IR 生成之后运行的部分，包括优化器和汇编代码生成。

也就是说，Clang（这个项目）主要负责「前端」；而第 3、4 步里的汇编与链接，driver 会调用 LLVM 后端能力和系统链接器来完成。

#### 4.1.2 核心流程

driver 文档把 driver 的工作分成 **五个概念阶段**：Parse（解析参数）→ Pipeline（构造 Action）→ Bind（选工具与文件名）→ Translate（翻译成具体命令）→ Execute（执行）。其中 **Pipeline** 阶段正是把「编译阶段」落成一张 Action 图的地方。

driver 提供了 `-ccc-print-phases` 选项，可以直接打印这张 Action 图。文档给出的示例（编译一个 `.c` 和一个 `.s`）如下：

```
$ clang -ccc-print-phases -x c t.c -x assembler t.s
0: input, "t.c", c
1: preprocessor, {0}, cpp-output
2: compiler, {1}, assembler
3: assembler, {2}, object
4: input, "t.s", assembler
5: assembler, {4}, object
6: linker, {3, 5}, image
```

读法：每一行是一个 Action，`{n}` 表示它依赖第 `n` 号 Action 的输出。可以清楚看到 `t.c` 走了「input → preprocess → compile → assemble」四步（0→1→2→3），`t.s` 因为已经是汇编则直接「assemble」（4→5），最后 `linker` 把两个 `.o` 合成映像（6）。这就是「编译阶段」在 driver 内部的真实形态——一张有向无环的 Action 图。

#### 4.1.3 源码精读

**术语定义（前端 / 中端 / 后端）** 出自用户手册的 Terminology 小节：

[docs/UsersManual.md:91-97](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/UsersManual.md#L91-L97) —— 这里用列表明确写出 Frontend / Middle-end / Backend 各自涵盖的范围，是判断「某一步算前端还是后端」的权威依据。

**Pipeline 阶段与 Action 图** 出自 driver 设计文档：

[docs/DriverInternals.rst:158-190](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst#L158-L190) —— 这段说明：参数解析完成后，driver 会确定输入文件及其类型、要对它们做的工作（preprocess、compile、assemble、link 等），并为每个任务构造一个 `Action` 实例；结果是一棵「若干顶层 Action」的树，每个顶层 Action 通常对应一个输出（目标文件或可执行映像）。其中 `InputAction` 用来把输入参数适配成 Action 的输入。

[docs/DriverInternals.rst:106-118](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst#L106-L118) —— 概括了五大阶段（Parse / Pipeline / Bind / Translate / Execute）的划分。

> 注意：这一节我们只看「文档怎么描述阶段」。真正的 `BuildActions` 等代码在 `lib/Driver/Driver.cpp`，属于下一讲 u2-l1 的内容，本讲不展开。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 driver 为一段代码构建出的 Action 图。

**操作步骤**：

1. 准备一个最小文件 `hello.cpp`：

   ```cpp
   // 示例代码
   #include <cstdio>
   int main() { return 0; }
   ```

2. 运行（注意 `-ccc-print-phases` 必须放在前面）：

   ```bash
   clang -ccc-print-phases hello.cpp
   ```

**需要观察的现象**：终端会打印出形如 `0: input, ...`、`1: preprocessor, {0}, ...`、`2: compiler, {1}, ...`、`3: assembler, {2}, ...`、`4: linker, {3}, image` 的列表。

**预期结果**：你会看到一条从 `input` 一路到 `linker` 的依赖链，与本节文档示例结构一致。

**待本地验证**：不同平台/目标下，`linker` 之前可能多出 `bind-arch`（多架构）或 `offload`（GPU 卸载）等额外 Action，具体以你机器上的输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果用 `clang -ccc-print-phases -c hello.cpp`（加上 `-c`），Action 图的结尾会有什么变化？
**答案**：流水线会在 `assembler → object` 处终止，不再出现 `linker` 这一步，因为 `-c` 表示「只编译和汇编，不链接」。

**练习 2**：为什么一个 `.s` 汇编文件不会经过 `preprocessor` 和 `compiler` 这两个 Action？
**答案**：driver 会根据输入文件的类型（`.s` 被识别为 assembler 输入）决定起点。`.s` 已经是汇编文本，所以只需 `assembler` 一步即可得到目标文件，不必再走预处理与编译。

---

### 4.2 driver 与 cc1 分工

#### 4.2.1 概念说明

很多人以为 `clang` 命令本身就是「编译器」。更准确的画面是：`clang` 命令里其实住着 **两个角色**：

- **driver**：面向用户的「命令行协调者」。它模仿 GCC 的命令行接口，负责解析参数、推断目标平台、构建上面那张 Action 图、挑选工具（汇编器、链接器等），并最终**安排一系列子任务去执行**。它本身不做词法/语法/语义分析。
- **cc1**：真正的「编译器前端」。它接收一组细粒度的参数，执行 Lex→Parse→Sema→CodeGen，产出 IR 或目标文件。

driver 文档把这个关系说得很直白：driver 要「完全吞下 gcc 可执行程序的功能」，意味着它**不需要委托给 gcc** 来完成子任务；并且它要能**直接调用 cc1**，因此必须掌握足够的信息把命令行参数正确地转发给子进程。

为什么这么分？因为 driver 要同时兼容 GCC/MSVC 的命令行习惯、处理多架构、多语言、链接外部工具——这些「调度」逻辑和「真正编译」逻辑完全不同，分开后两边都更清晰、更可复用（driver 库可以被其他工具复用来实现 GCC 风格的接口）。

#### 4.2.2 核心流程

当你输入 `clang hello.cpp` 时，入口函数 `clang_main` 的逻辑大致是：

```text
clang_main(Args)
 ├─ 若 Args[1] 以 "-cc1" 开头？
 │    └─ 是：说明是被 driver 派发来的（或用户直接调 -cc1）
 │         → ExecuteCC1Tool() → cc1_main()  直接运行前端，结束
 ├─ 否（普通 clang 调用）：
 │    ├─ 构造 Driver TheDriver(可执行路径, 默认目标三元组, Diags)
 │    ├─ C = TheDriver.BuildCompilation(Args)   # Parse→Pipeline→Bind→Translate
 │    └─ TheDriver.ExecuteCompilation(*C, ...)  # Execute：真正跑起来
 │           └─ 对每个 Job：调用对应 Tool
 │                 └─ 编译类 Tool 最终调用 cc1（进程内或子进程）
```

这里有一个关键设计点：**cc1 是「进程内」跑还是「另起子进程」跑**？由 `CLANG_SPAWN_CC1` 编译期开关和 `-fintegrated-cc1` / `-fno-integrated-cc1` 运行期选项控制。默认是「进程内」（integrated），即 driver 在同一个进程里直接调用 `cc1_main` 回调，省去启动新进程的开销，也便于调试与性能分析。

#### 4.2.3 源码精读

**入口与薄包装定位** —— 文件开头的注释直接点明 driver.cpp 的角色：

[tools/driver/driver.cpp:9-11](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L9-L11) —— 注释写道：「这是 clang driver 的入口；它是对 Driver（clang 库）功能的一层薄包装」。

**三个 cc1 系子工具的声明** —— driver 通过这三个外部函数分别进入前端、汇编器、复现文件生成器：

[tools/driver/driver.cpp:87-93](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L87-L93) —— 声明了 `cc1_main`、`cc1as_main`、`cc1gen_reproducer_main` 三个入口，分别对应 `-cc1`、`-cc1as`、`-cc1gen-reproducer` 三种子工具。

**`-cc1` 分发逻辑** —— `ExecuteCC1Tool` 根据第二个参数决定调用哪个子工具：

[tools/driver/driver.cpp:226-239](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L226-L239) —— 取 `ArgV[1]` 作为工具名：等于 `"-cc1"` 则调 `cc1_main`；`"-cc1as"` 则调 `cc1as_main`；`"-cc1gen-reproducer"` 则调 `cc1gen_reproducer_main`；其余报「未知集成工具」错误。注意传给 `cc1_main` 的是 `slice(1)`，即去掉了程序名本身。

**主入口 `clang_main` 与 `-cc1` 早期拦截**：

[tools/driver/driver.cpp:271-278](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L271-L278) —— 在正式参数解析之前，先判断 `Args[1]` 是否以 `"-cc1"` 开头；若是，则启用沙箱并直接走 `ExecuteCC1Tool`，根本不构造 `Driver`。这就是「直接以 `-cc1` 调用」与「普通 clang 调用」的分岔点。

**普通调用路径：构造 Driver、BuildCompilation、ExecuteCompilation**：

[tools/driver/driver.cpp:360-361](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L360-L361) —— 用可执行路径、默认目标三元组（`llvm::sys::getDefaultTargetTriple()`）和诊断引擎构造 `Driver TheDriver`。

[tools/driver/driver.cpp:388](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L388) —— `TheDriver.BuildCompilation(Args)` 一次性完成 Parse→Pipeline→Bind→Translate，返回一个 `Compilation` 对象（里面装着待执行的 Job 列表）。

[tools/driver/driver.cpp:419](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L419) —— `TheDriver.ExecuteCompilation(*C, FailingCommands)` 真正执行这些 Job，并收集失败命令。

**进程内 cc1 的回调注入**：

[tools/driver/driver.cpp:381-386](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L381-L386) —— 当 `UseNewCC1Process` 为假（默认、即 `-fintegrated-cc1`）时，把 `ExecuteCC1WithContext` 这个回调赋给 `TheDriver.CC1Main`，并启用 `CrashRecoveryContext` 以便在 cc1 崩溃时能捕获并生成诊断。换言之，「进程内 cc1」就是通过这个回调函数指针实现的。

[tools/driver/driver.cpp:331-336](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L331-L336) —— `UseNewCC1Process` 的初值取自编译期 `CLANG_SPAWN_CC1`，并允许 `-fno-integrated-cc1`（置真，另起进程）和 `-fintegrated-cc1`（置假，进程内）在命令行覆盖。

**「driver 直接调用 cc1」的设计意图** 出自文档：

[docs/DriverInternals.rst:80-91](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst#L80-L91) —— 说明 driver 要完全吞下 gcc 的功能、不委托 gcc；并且要能直接调用语言专用编译器（如 cc1），因此必须掌握足够信息以正确转发命令行参数。

**cc1 自己的入口** 在另一个文件里：

[tools/driver/cc1_main.cpp:219](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/cc1_main.cpp#L219) —— `int cc1_main(...)` 的定义；其文件头注释（[cc1_main.cpp:9-12](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/cc1_main.cpp#L9-L12)）写道：这是 clang `-cc1` 功能的入口，实现了核心编译器功能以及若干用于演示和测试的附加工具。下一讲 u2-l3 会深入这里。

#### 4.2.4 代码实践

**实践目标**：用 `-###` 让 driver「只排程、不执行」，观察它如何调用 cc1。

**操作步骤**：

1. 对同一个 `hello.cpp`，运行：

   ```bash
   clang -### hello.cpp
   ```

2. 注意输出全部打印在 **标准错误（stderr）** 上，且以 `"` 包裹的命令行形式出现。

**需要观察的现象**：你会看到一行形如 `"/abs/path/to/clang" -cc1 ... hello.cpp ...` 的长命令，其后通常还跟着链接器调用。这就是 driver 在 Translate/Execute 阶段为 cc1 拼好的「真实参数」。

**预期结果**：因为 `-###`，driver 不会真正编译，只是把「将要执行的命令」打印出来并立即返回（退出码通常为 0）。这正是阅读「driver↔cc1 边界」的最佳窗口——你能看到 driver 把高层选项（如 `hello.cpp`）翻译成了 cc1 需要的一长串细粒度参数。

**待本地验证**：cc1 那一行的具体参数（目标三元组、内部 include 路径、`-fn` 类选项）随你的安装与平台而变；如果你看到的是「进程内」模式，命令里出现的可执行路径就是当前 `clang` 自身。

**对比实践（选做）**：再跑一次 `clang -### -fno-integrated-cc1 hello.cpp`，观察 cc1 是否被安排成一个新的子进程调用（与默认的 `-fintegrated-cc1` 对照），从而体会 4.2.3 里 `UseNewCC1Process` 的含义。

#### 4.2.5 小练习与答案

**练习 1**：既然 driver 不做词法/语法分析，那为什么把源码文件名交给 driver，最终却能完成编译？
**答案**：driver 在 Execute 阶段会把编译类 Action 绑定到 cc1（进程内回调或子进程），由 cc1 完成真正的 Lex/Parse/Sema/CodeGen。driver 只负责「排程」，cc1 负责「干活」。

**练习 2**：直接执行 `clang -cc1 hello.cpp`（不加任何其他参数）通常会发生什么？为什么？
**答案**：很可能会报错退出。因为 `-cc1` 绕过了 driver 的高层推理，不会再自动补目标三元组、include 路径等信息；这些在普通 `clang` 调用里是由 driver 翻译并补齐后传给 cc1 的。所以 `-cc1` 需要用户显式提供大量参数，通常只用于调试或测试（见 u2-l3）。

---

### 4.3 常用命令行选项

#### 4.3.1 概念说明

上一节我们看到 driver 会构建一张「跑到哪儿」的 Action 图。本节关心的是：**用户用哪些选项来控制这张图停在哪个阶段、产出什么、当作什么语言处理**。

最常用的一组选项可以分成三类：

| 类别 | 代表选项 | 作用 |
| --- | --- | --- |
| 控制阶段（停在哪儿） | `-E` `-S` `-c` `-fsyntax-only` `-emit-llvm` | 让流水线在预处理/编译/汇编/语法检查/IR 处停下 |
| 输出与语言 | `-o <file>` `-x <language>` | 指定输出文件名；指定后续输入文件的语言类型 |
| 观察与调试 | `-###` `-v` `-ccc-print-phases` | 打印将要执行的命令、显示详细过程、打印 Action 图 |

这些选项在源码里都集中在 `include/clang/Options/Options.td` 这一个 TableGen 文件中定义，并被归到 `Action_Group`（动作选项组）等分组里，driver 据此决定如何构造 Action。

#### 4.3.2 核心流程

「控制阶段」类选项的本质，是告诉 driver：**Pipeline 阶段构造 Action 图时，从哪里开始、到哪里结束**。可以记成下面这张「停站表」：

| 选项 | 流水线走到 | 产物（默认） |
| --- | --- | --- |
| `-fsyntax-only` | Lex → Parse → Sema（不做 CodeGen） | 无文件输出，仅诊断 |
| `-E` | 只到预处理 | 展开后的源码（stdout） |
| `-emit-llvm`（配合 `-S`/`-c`） | 走完前端到 IR | 可读 IR（`.ll`）或 bitcode（`.bc`） |
| `-S` | 预处理 + 编译 | 汇编文本（`.s`） |
| `-c` | 预处理 + 编译 + 汇编 | 目标文件（`.o`） |
| （都不加） | 一直走到链接 | 可执行映像（如 `a.out`） |

`-o` 改的是输出文件名；`-x` 改的是「后续输入被当作什么语言」，例如 `-x c++` 会把后续文件都按 C++ 处理（即便扩展名不是 `.cpp`）。

`-###` 的官方定位见 driver 文档：它用来「dump Parse 阶段的结果」。在现代 clang 里，它的实际效果更接近「打印将要执行的全部命令行（含 cc1 调用），但不真正执行」——是阅读 driver 排程结果最方便的开关。

#### 4.3.3 源码精读

下列选项全部定义在 Options.td 中，且大多挂在 `Action_Group` 下：

`-E`：[include/clang/Options/Options.td:766-769](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Options/Options.td#L766-L769) —— 帮助文本「Only run the preprocessor」（只运行预处理）。

`-S`：[include/clang/Options/Options.td:876-879](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Options/Options.td#L876-L879) —— 帮助文本「Only run preprocess and compilation steps」（只运行预处理和编译步骤）。

`-c`：[include/clang/Options/Options.td:1243-1245](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Options/Options.td#L1243-L1245) —— 帮助文本「Only run preprocess, compile, and assemble steps」（只运行预处理、编译、汇编步骤）。

`-emit-llvm`：[include/clang/Options/Options.td:1611-1614](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Options/Options.td#L1611-L1614) —— 帮助文本「Use the LLVM representation for assembler and object files」（让汇编/目标文件阶段产出 LLVM 表示，即 IR/bitcode）。

`-fsyntax-only`：[include/clang/Options/Options.td:4616-4620](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Options/Options.td#L4616-L4620) —— 帮助文本「Run the preprocessor, parser and semantic analysis stages」（运行预处理、解析和语义分析阶段，不做代码生成）。

`-o`：[include/clang/Options/Options.td:6575](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Options/Options.td#L6575) —— 定义为 `JoinedOrSeparate`，即可写成 `-ofoo` 或 `-o foo`。

`-x`：[include/clang/Options/Options.td:6897-6901](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Options/Options.td#L6897-L6901) —— 帮助文本「Treat subsequent input files as having type \<language\>」（把后续输入文件当作指定语言类型处理）。

`-###` 的文档出处：[docs/DriverInternals.rst:140-156](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst#L140-L156) —— 文档说明 driver 可以用 `-###` 标志 dump 出 Parse 阶段的结果（参数被分解为 `Arg` 实例后的样子），并给出示例。

> 小知识：每个选项都带有 `Visibility<[ClangOption, CC1Option, ...]>` 这样的标注，表示该选项对 driver 层（`ClangOption`）和 cc1 层（`CC1Option`）是否可见。这就是为什么有些选项只能在 `clang` 层用、有些也能在 `-cc1` 层用。

#### 4.3.4 代码实践

**实践目标**：用一组选项，把同一段代码分别停在每个中间阶段，体会「停站表」。

**操作步骤**：对 `hello.cpp`（内容见 4.1.4）依次执行：

```bash
clang -fsyntax-only   hello.cpp        # 只做语法/语义检查，不产文件
clang -E              hello.cpp        # 预处理，输出到终端
clang -S              hello.cpp -o hello.s   # 编译到汇编
clang -c              hello.cpp -o hello.o   # 编译+汇编到目标文件
clang -S -emit-llvm   hello.cpp -o hello.ll  # 编译到可读 LLVM IR
clang                 hello.cpp -o hello     # 完整编译+链接
```

**需要观察的现象**：

- `-fsyntax-only`：若有语法/语义错误会打印诊断；正常时几乎无输出，且不生成文件。
- `-E`：会把 `#include` 的内容展开进来，输出会非常长。
- `-S`：生成 `hello.s`，里面是汇编文本。
- `-c`：生成 `hello.o`，是二进制目标文件。
- `-S -emit-llvm`：生成 `hello.ll`，里面是形如 `define ...` 的 LLVM IR。
- 完整编译：生成可执行的 `hello`，可 `./hello` 运行。

**预期结果**：每条命令的产物对应「停站表」一行；`-o` 成功改写了输出文件名。

**待本地验证**：IR 文件里的具体函数名、属性（如 `noundef`、`!dbg` 行号）会因 clang 版本与默认目标而略有不同；目标文件格式（ELF/Mach-O/COFF）取决于你的操作系统。

**进阶观察**：对上面任一条命令再加 `-###`（如 `clang -### -S hello.cpp`），对比 driver 给 cc1 拼出的参数，验证「`-S` 让流水线停在编译步」这件事是如何被翻译成具体 cc1 选项的。

#### 4.3.5 小练习与答案

**练习 1**：想得到一份「可读的 LLVM IR」该用哪个选项组合？为什么不能只用 `-emit-llvm`？
**答案**：用 `-S -emit-llvm`（产出 `.ll` 可读文本）。单独 `-emit-llvm` 配合默认或 `-c` 时倾向于产出 bitcode（`.bc`，二进制）；`-S` 强制以文本形式输出，二者组合才得到人类可读的 IR。

**练习 2**：`-x c++ hello.txt` 中 `-x` 的作用是什么？如果省略 `-x`，driver 会如何判断 `hello.txt` 的语言？
**答案**：`-x c++` 让 driver 把**后续**输入文件都当作 C++ 处理，无论扩展名。如果省略 `-x`，driver 会依据文件扩展名推断语言类型（如 `.cpp`→C++、`.c`→C）；`hello.txt` 这种无法识别的扩展名通常会被当作链接器输入或报错。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿任务：

> **任务**：对一段简单 C++ 代码运行 `clang -### hello.cpp`，逐条解释 driver 触发的每个 Action 与工具调用，并用源码出处佐证你的解释。

**建议步骤**：

1. 准备 `hello.cpp`：

   ```cpp
   // 示例代码
   #include <cstdio>
   int main() { std::printf("hi\n"); return 0; }
   ```

2. 先看「Action 视图」：

   ```bash
   clang -ccc-print-phases hello.cpp
   ```

   按 4.1 的读法，把每行 Action（input / preprocessor / compiler / assembler / linker）抄下来，标注 `{n}` 依赖关系，画出一张 Action 图。

3. 再看「命令视图」：

   ```bash
   clang -### hello.cpp 2>cmd.txt
   clang -### -v hello.cpp 2>>cmd.txt
   ```

   （`-###` 输出在 stderr，故用 `2>` 重定向。）打开 `cmd.txt`，找到以 `"-cc1"` 结尾的那条长命令。

4. **逐条解释**（写成你的学习笔记）：
   - driver 先做什么？（对应 4.2.2 的 `BuildCompilation`，源码 [driver.cpp:388](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L388)）。
   - `-cc1` 那条命令里：哪个参数指明了目标三元组？哪个指明了输入文件？哪个指明了要包含的系统头文件目录？（这些正是 4.2.5 练习 2 里说「直接 `-cc1` 会缺」的信息。）
   - `-cc1` 之后那条命令是什么？（通常是系统链接器，如 `ld` / `lld`，把 `.o` 链成可执行文件。）
   - `-v` 比 `-###` 多显示了什么？（通常是搜索到的头文件、链接器详细信息。）

5. **交叉验证**：把 `-###` 里 cc1 的目标三元组、include 路径，与你机器上的 `clang -print-target-triple`、`clang -E -v hello.cpp` 输出对照，确认理解无误。

**验收标准**：你能指着 `-###` 的输出说清楚「这一长串是 driver 替 cc1 翻译好的参数，对应 Action 图里的 compiler 这一步；其后那条是链接器，对应 linker 这一步」。

> 待本地验证：步骤 3、4 的确切内容依赖你的操作系统、clang 版本与安装路径；本任务重在「读懂结构与对应关系」，而非记住具体字符串。

## 6. 本讲小结

- 一次 `clang` 编译在概念上经过 **预处理 → 编译 → 汇编 → 链接** 四个阶段；Clang 项目主要负责其中的「前端」（到 LLVM IR 为止），汇编/链接由 LLVM 后端与系统链接器完成。
- driver 内部把这过程建模成一张 **Action 图**，可用 `-ccc-print-phases` 直接打印；这张图由 Parse→Pipeline→Bind→Translate→Execute 五个阶段构造并执行。
- `clang` 命令里住着两个角色：**driver**（命令行协调者，模仿 GCC）和 **cc1**（真正的编译器前端）。普通调用时 driver 在 Execute 阶段通过进程内回调（`-fintegrated-cc1`，默认）或子进程调用 cc1。
- 入口 `clang_main` 会在正式解析前先拦截 `-cc1`（[driver.cpp:271-278](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/driver.cpp#L271-L278)），由此区分「直接调 -cc1」与「普通 clang 调用」两条路径。
- `-E`/`-S`/`-c`/`-fsyntax-only`/`-emit-llvm` 控制流水线停在哪一站；它们都定义在 [Options.td](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Options/Options.td) 的 `Action_Group` 下。
- `-###` 是阅读 driver↔cc1 边界的最佳工具：它打印将要执行的全部命令（含 cc1 的细粒度参数）但不真正执行。

## 7. 下一步学习建议

本讲止步于「用户视角」与源码入口的指认。接下来建议：

- **u2-l1（Driver 架构）**：进入 `lib/Driver/Driver.cpp`，看 `BuildCompilation` 内部是如何从参数解析一步步构造出 Action 列表的——本讲里那个 `-ccc-print-phases` 的图，就是在那里被画出来的。
- **u2-l2（ToolChain / Action / Job / Compilation）**：搞清楚 Action 图如何被绑定到具体 Tool、翻译成 Job 并执行，理解本讲 `-###` 输出里那一条条命令是怎么来的。
- **u2-l3（cc1 入口）**：深入 `cc1_main.cpp`，理解「直接 `-cc1`」时需要显式提供哪些参数，以及 `ExecuteCompilerInvocation` 如何驱动前端。
- 顺手阅读：[docs/DriverInternals.rst](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/DriverInternals.rst) 的「Additional Notes」一节，了解 `Compilation` 对象与「未使用参数警告」机制，为 u2 打底。
