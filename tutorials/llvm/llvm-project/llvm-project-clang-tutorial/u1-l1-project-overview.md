# Clang 是什么：项目定位与生态

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是让你在还没有读任何 Clang 源码之前，先建立起对 Clang 的整体认知。读完本讲，你应该能够：

- 用一句话说清 Clang 是什么，以及它在 LLVM 项目里扮演的角色；
- 解释「编译器前端」这个概念，并知道 Clang 之外的另一半（中端/后端）由谁负责；
- 列举出 Clang 除了「编译代码」之外的几个重要用途（静态分析、格式化、索引、LibTooling 工具等），理解它是一个「源码级工具平台」；
- 看懂 Clang 仓库里 `README.md`、`docs/index.rst`、`CMakeLists.txt` 这三个入口文件分别提供了什么信息。

本讲不要求你会编译 Clang，也不要求你懂 C++ 模板或 LLVM IR。我们只做「认路」——为后续讲义里逐层深入源码打下地基。

## 2. 前置知识

本讲面向零基础读者，但有几个通俗概念先铺垫一下：

- **编译器（Compiler）**：把人类写的源代码翻译成机器能执行的程序的工具。比如把 `int main(){}` 翻译成可执行文件。
- **源代码（Source code）**：你用 C/C++/Objective-C 这类语言写出来的、给人读的文本。
- **前端 / 中端 / 后端**：现代编译器常被拆成三段。
  - **前端（Front-end）**：负责「读懂源代码」——做词法分析、语法分析、语义检查，把源码变成一种中间表示。
  - **中端（后端）**：拿走中间表示，做优化，最终生成具体 CPU（x86、ARM 等）上的机器码。
  - Clang 就是 LLVM 这套编译器里的「前端」。
- **C 语言家族（C family）**：指 C、C++、Objective-C（以及 Objective-C++）这一组语法相近、历史悠久、被广泛使用的语言。
- **静态分析（Static Analysis）**：不运行程序，只通过分析源代码来发现潜在 bug（如空指针、内存泄漏）。

后续遇到的专业术语（如 AST、LibTooling、TableGen）都会在第一次出现时解释，现在记不住没关系。

## 3. 本讲源码地图

本讲只读三个「入口型」文件，它们是了解 Clang 全貌的最快路径：

| 文件 | 作用 | 在本讲的角色 |
| --- | --- | --- |
| [README.md](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/README.md) | 项目自述文件 | 用最精炼的话说明 Clang 是什么、它的定位与外链 |
| [docs/index.rst](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/index.rst) | 文档总目录 | 从文档分类一览 Clang 的全部能力面 |
| [CMakeLists.txt](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt) | 构建脚本入口 | 揭示 Clang 作为 LLVM 子项目的依赖关系与组件构成 |

> 提示：`.rst` 是 reStructuredText，一种常用于 Python/LLVM 文档的标记语言，类似 Markdown。Sphinx 工具会把它渲染成 HTML 文档站。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**项目定位**、**与 LLVM 的关系**、**工具生态概览**。

### 4.1 项目定位：Clang 是 C 家族编译器前端

#### 4.1.1 概念说明

先给结论：**Clang 是 LLVM 项目中负责 C/C++/Objective-C 这一类「C 家族语言」的编译器前端。**

它解决的核心问题是——**让计算机「读懂」C 家族源代码**。这件事远比想象中复杂：C++ 的语法（尤其是模板）极其繁琐，历史包袱沉重；为了让编译速度快、报错清晰、便于做工具，Clang 从一开始就被设计成一个「库化的前端」（下面会看到，它的功能几乎都以 C++ 库的形式提供，而不只是一个黑盒命令行程序）。

为什么要强调「前端」？因为一个完整的编译器还要负责优化和生成机器码，但 Clang **不做**这些后半段工作——它把源码翻译成 LLVM IR（一种中间表示），然后把 IR 交给 LLVM 的中端/后端去优化和生成机器码。这种「前端 / 后端分离」的架构，正是 LLVM 生态的基石。

#### 4.1.2 核心流程

从用户视角，Clang 的工作可以概括为一条流水线（本讲只看大图，细节在后续讲义展开）：

```text
源代码(.c/.cpp/.m)
   │  ① 词法分析 Lex     → 切成 Token
   │  ② 语法分析 Parse    → 建成 AST（抽象语法树）
   │  ③ 语义分析 Sema     → 类型检查、名字查找
   │  ④ 代码生成 CodeGen  → 翻译成 LLVM IR
   ▼
LLVM IR
   │  （交给 LLVM 中端/后端继续优化与生成机器码）
   ▼
机器码 / 可执行文件
```

要点：Clang 负责 ① 到 ④，产出是 **LLVM IR**；之后的优化和机器码生成由 LLVM 负责。这就是「前端」二字的含义。

#### 4.1.3 源码精读

最权威的一句话定位，就在 README 的开头：

```text
# C language Family Front-end
```

[README.md:1](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/README.md#L1) —— 标题直接点明：**C 语言家族的前端**。

紧接着正文给出完整定义：

```text
This is a compiler front-end for the C family of languages
(C, C++ and Objective-C) which is built as part of the LLVM
compiler infrastructure project.
```

[README.md:5](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/README.md#L5) —— 这一句包含两个关键信息：
1. 它是 C、C++、Objective-C 的**前端**；
2. 它**作为 LLVM 编译器基础设施项目的一部分**而构建（"built as part of the LLVM compiler infrastructure project"）。

> 注意：README 把这一段写在了独立的一行里，行内带有较多空格（原文件排版所致），但语义清晰。我们引用时只看含义，不必纠结多余空白。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，无需编译。

1. **实践目标**：通过原文确认 Clang 的定位，建立第一印象。
2. **操作步骤**：
   - 打开 [README.md](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/README.md)。
   - 阅读第 1–9 行。
3. **需要观察的现象**：注意标题用 "Front-end"（前端）而非 "Compiler"（编译器）——这个用词差别正是 Clang 与 LLVM 分工的体现。
4. **预期结果**：你能向别人解释「为什么 README 不直接叫 Clang a compiler，而叫 front-end」。
5. 结论：**待本地验证**（阅读体会因人而异，重点是理解「前端」一词）。

#### 4.1.5 小练习与答案

**练习 1**：Clang 支持哪三种语言？
**答案**：C、C++、Objective-C（见 [README.md:5](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/README.md#L5)）。

**练习 2**：如果把 Clang 比作一条流水线上的工人，它的「上游」和「下游」分别是谁？
**答案**：上游是**用户写的源代码**；下游是 **LLVM（接收 Clang 产出的 IR，继续优化与生成机器码）**。Clang 自己不直接产出最终机器码。

---

### 4.2 与 LLVM 的关系：作为子项目的协同

#### 4.2.1 概念说明

很多人会把 Clang 和 LLVM 混为一谈，其实它们是**两个层次**的东西：

- **LLVM**：一套编译器基础设施，提供中间表示（LLVM IR）、优化器（一系列 Pass）和针对各种 CPU 的代码生成后端。它本身**不认识 C++ 语法**。
- **Clang**：LLVM 项目里的一个**子项目（subproject）**，专门负责「把 C 家族源码翻译成 LLVM IR」。

打个比方：LLVM 是一座大型加工厂的「通用加工车间」，能把半成品原料加工成各种成品；而 Clang 是工厂门口的「原料预处理车间」，负责把原始矿石（源代码）初步提炼成车间能用的标准原料（LLVM IR）。两者缺一不可，但职责分明。

Clang 还有一个常被忽视的身份：它是 **C++ 标准库实现（libc++）和 LLVM 自身的「自举编译器」**——LLVM 项目的 C++ 代码本身就是用 Clang 编译的。

#### 4.2.2 核心流程

从工程角度看，Clang 与 LLVM 的依赖是**单向**的：

```text
┌─────────────┐   依赖（构建期链接 LLVM 库、调用 llvm-tblgen）
│   Clang     │ ───────────────────────────────────────────────►  ┌──────────┐
│  (子项目)   │                                                   │   LLVM   │
└─────────────┘   产出（LLVM IR）                                 │ (基础设施)│
       │         ─────────────────────────────────────────────►  └──────────┘
       │                                                          优化 + 机器码
```

也就是说：Clang 在**构建时**需要 LLVM 已就绪（链接 LLVM 的库、用它的 tblgen 工具生成代码）；在**运行时**把生成的 IR 交给 LLVM 处理。

#### 4.2.3 源码精读

证据全部写在构建脚本 [CMakeLists.txt](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt) 里。

**证据一：Clang 自报家门是「LLVM 子项目」。**

```cmake
set(LLVM_SUBPROJECT_TITLE "Clang")
```

[CMakeLists.txt:10](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L10) —— 这一行设置子项目标题为 "Clang"，表明它运行在 LLVM 的整体构建框架内。

**证据二：Clang 区分「作为 LLVM 一部分构建」和「独立构建」两种模式。**

```cmake
# If we are not building as a part of LLVM, build Clang as a
# standalone project, using LLVM as an external library:
if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
  project(Clang)
  set(CLANG_BUILT_STANDALONE TRUE)
endif()
```

[CMakeLists.txt:18-23](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L18-L23) —— 当 Clang 不在 LLVM 大构建里时，就把它当作独立项目，**把 LLVM 当作外部库**来用。

**证据三：构建期对 LLVM 的硬依赖。**

```cmake
find_package(LLVM REQUIRED HINTS "${LLVM_CMAKE_DIR}")
```

[CMakeLists.txt:48](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L48) —— `find_package(LLVM REQUIRED)` 说明：**没有 LLVM，就构建不出 Clang**。

```cmake
find_program(LLVM_TABLEGEN_EXE "llvm-tblgen" ${LLVM_TOOLS_BINARY_DIR}
  NO_DEFAULT_PATH)
```

[CMakeLists.txt:58-59](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L58-L59) —— Clang 还要借用 LLVM 自带的代码生成工具 `llvm-tblgen`（TableGen 体系，后续讲义会专门讲）。这也印证了「构建期单向依赖 LLVM」。

**证据四：Clang 用 C++17 标准编写（独立构建时显式声明）。**

```cmake
set(CMAKE_CXX_STANDARD 17 CACHE STRING "C++ standard to conform to")
set(CMAKE_CXX_STANDARD_REQUIRED YES)
set(CMAKE_CXX_EXTENSIONS NO)
```

[CMakeLists.txt:37-39](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L37-L39) —— 读 Clang 源码请备好 C++17 知识。

#### 4.2.4 代码实践

1. **实践目标**：从构建脚本里找出「Clang 依赖 LLVM」的全部证据。
2. **操作步骤**：
   - 打开 [CMakeLists.txt](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt)。
   - 搜索关键词 `LLVM`，统计它出现的次数与上下文。
3. **需要观察的现象**：你会看到 `find_package(LLVM …)`、`LLVM_INCLUDE_DIRS`、`LLVM_TABLEGEN_EXE`、`LLVM_ENABLE_PROJECTS` 等大量对 LLVM 的引用。
4. **预期结果**：得出结论——Clang 把 LLVM 当作「前提条件」而非「同级伙伴」。这与「前端 / 后端」的分工完全吻合。
5. 结论：**待本地验证**（可用编辑器的查找功能计数）。

#### 4.2.5 小练习与答案

**练习 1**：Clang 能脱离 LLVM 单独存在并完整编译出机器码吗？为什么？
**答案**：不能。Clang 只产出 LLVM IR，后续优化和机器码生成都依赖 LLVM；构建时也需要链接 LLVM 的库（见 [CMakeLists.txt:48](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L48)）。

**练习 2**：`CLANG_BUILT_STANDALONE` 这个变量在什么情况下为真？
**答案**：当 Clang 不是作为 LLVM 整体构建的一部分、而是被单独配置（`CMAKE_SOURCE_DIR` 等于 `CMAKE_CURRENT_SOURCE_DIR`）时为真（见 [CMakeLists.txt:20-23](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L20-L23)）。

---

### 4.3 工具生态概览：不只是编译器

#### 4.3.1 概念说明

这是本讲最重要也最容易被忽略的一点：**Clang 不只是一个编译器，它还是一个「源码级工具平台」**。

很多编译器是「黑盒」——你给它源码，它吐出可执行文件，中间过程对外不可见。Clang 反其道而行：它把词法分析、语法树（AST）、语义信息等能力**封装成一组 C++ 库**，并提供稳定的 C 语言接口（libclang）。于是别人就能复用这些能力，写出各种各样的「源码级工具」：

- **静态分析器（Static Analyzer）**：找空指针、内存泄漏等 bug；
- **clang-format**：自动格式化代码风格；
- **clang-tidy**：lint 式的代码检查与现代化改写；
- **代码索引 / 跳转**：IDE 里的「跳转到定义」「重命名」常基于 Clang；
- **重构工具**：批量、安全地修改代码（Clang Transformer）。

README 里把这一点说得很直白。

#### 4.3.2 核心流程

Clang 的能力以「库」的形式分层暴露，工具站在它的肩膀上：

```text
                    ┌─────────────────────────────────────────┐
   源码级工具 ──►   │  Clang 库（lib/Lex, lib/AST, lib/Sema …）│  ◄── 共享同一套前端能力
 (clang-format,     │  稳定 C 接口：libclang (CXTranslationUnit)│
  clang-tidy, …)    └─────────────────────────────────────────┘
                              │ 产出
                              ▼
                          LLVM IR ──► LLVM 后端 ──► 机器码
```

关键理念：**「编译」只是 Clang 的一种用法；它的内部能力（AST、词法、语义）被抽成库，供无数工具复用。** 这也是为什么本手册后续会花大量篇幅讲 AST、LibTooling、ASTMatchers、插件——它们都是这个平台的组成部分。

#### 4.3.3 源码精读

**证据一：README 明确声明 Clang 的工具平台定位。**

```text
Unlike many other compiler frontends, Clang is useful for a number
of things beyond just compiling code: we intend for Clang to be host
to a number of different source-level tools. One example of this is
the Clang Static Analyzer.
```

[README.md:7](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/README.md#L7) —— 「与许多其他前端不同，Clang 在编译代码之外还有诸多用途：我们打算让 Clang 成为大量源码级工具的宿主（host）。」这是 Clang 区别于普通编译器的**设计宣言**。

README 还专门给了静态分析器的链接：

```text
* Clang Static Analyzer:    http://clang-analyzer.llvm.org/
```

[README.md:15](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/README.md#L15)。

**证据二：文档总目录把 Clang 的能力分成「四大块」，其中三块都不是单纯编译。**

打开 [docs/index.rst](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/index.rst)，可以看到文档被组织成四大部分：

| 章节 | 含义 | 典型条目 |
| --- | --- | --- |
| `Using Clang as a Compiler`（[L13-14](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/index.rst#L13-L14)） | 把 Clang 当**编译器**用 | UsersManual、各类 Sanitizer、Modules |
| `Using Clang as a Library`（[L77-78](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/index.rst#L77-L78)） | 把 Clang 当**库**用 | LibTooling、LibClang、LibFormat、ClangPlugins、LibASTMatchers |
| `Using Clang Tools`（[L99-100](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/index.rst#L99-L100)） | 使用 Clang 生态的**现成工具** | ClangCheck、ClangFormat、ClangRepl |
| `Design Documents`（[L115-116](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/index.rst#L115-L116)） | 内部**设计文档** | InternalsManual、DriverInternals、PCHInternals |

注意「Using Clang as a Library」这一节，它列出的正是构成工具平台的核心库（节选）：

```text
   Tooling
   LibTooling
   LibClang
   LibFormat
   ClangPlugins
   RAVFrontendAction
   LibASTMatchersTutorial
   LibASTMatchers
   ClangTransformerTutorial
```

[docs/index.rst:83-93](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/index.rst#L83-L93) —— 这些名字（LibTooling、ASTMatchers、Transformer、Plugins）都是本手册进阶篇要讲的「工具平台积木」。

**证据三：构建脚本里的 `add_subdirectory` 串起了庞大的库与工具集合。**

```cmake
add_subdirectory(utils/TableGen)
...
add_subdirectory(include)
...
add_subdirectory(lib)
add_subdirectory(tools)
add_subdirectory(runtime)
```

[CMakeLists.txt:534-560](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L534-L560)（节选关键几行）—— 这串调用把 Clang 的各大组件挂进构建系统。其中 `lib/` 下挂载了 30 多个子库，`tools/` 下挂载了几十个可执行工具，正是「平台」二字的实物证据。

例如 [lib/CMakeLists.txt:1-20](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CMakeLists.txt#L1-L20) 列出的子库（节选）：

```cmake
add_subdirectory(Basic)      # 基础设施：源码位置、诊断、Token
add_subdirectory(Lex)        # 词法分析 / 预处理
add_subdirectory(Parse)      # 语法分析
add_subdirectory(AST)        # 抽象语法树
add_subdirectory(Sema)       # 语义分析
add_subdirectory(CodeGen)    # 代码生成（→ LLVM IR）
add_subdirectory(StaticAnalyzer)  # 静态分析器
add_subdirectory(Format)     # clang-format 背后的库
add_subdirectory(Tooling)    # LibTooling / ASTMatchers / Transformer
```

以及 [CMakeLists.txt:498-499](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L498-L499) 表明静态分析器是 Clang 的内置可选组件：

```cmake
option(CLANG_ENABLE_STATIC_ANALYZER
  "Include static analyzer in clang binary." ON)
```

> 术语速查：上面出现的 `AST`（Abstract Syntax Tree，抽象语法树）是 Clang 解析源码后得到的树状中间结构，是几乎所有工具的「数据底座」。后续讲义会反复用到。

#### 4.3.4 代码实践

1. **实践目标**：动手数一数 Clang 到底提供了多少「非编译」类工具与库，直观感受平台的规模。
2. **操作步骤**：
   - 打开 [tools/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/CMakeLists.txt)，逐条看 `add_clang_subdirectory(...)` 列出了哪些工具。
   - 打开 [lib/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CMakeLists.txt)，数一数有多少个子库。
3. **需要观察的现象**：你会看到 `clang-format`、`clang-check`、`clang-repl`、`clang-scan-deps`、`libclang`、`scan-build` 等大量工具，它们大多不直接「编译生成可执行文件」，而是在做格式化、检查、索引、依赖扫描等事。
4. **预期结果**：你列出的工具里，至少有 5 个属于「源码级工具」而非传统意义上的编译器。
5. 结论：**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：下面哪些是 Clang 生态里的「源码级工具」？（多选）
a) clang-format　b) clang-tidy　c) Clang Static Analyzer　d) 一个把 `.cpp` 链接成可执行程序的链接器后端
**答案**：a、b、c 都是 Clang 生态的源码级工具；d 属于系统链接器（虽然 Clang 会调用它，但它本身不是 Clang 的源码分析工具）。

**练习 2**：为什么 Clang 能成为「工具平台」，而很多传统编译器做不到？
**答案**：因为 Clang 把词法、AST、语义等能力**封装成了可复用的 C++ 库**（见 [lib/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CMakeLists.txt) 下的 Lex/AST/Sema/Tooling 等子库），并提供稳定 C 接口 libclang；工具可以直接站在这些库上工作，而不必各自重新实现一套前端。这正是 [README.md:7](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/README.md#L7) 所说的设计意图。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿性任务（这也是本讲的 `practice_task`）：

> **任务**：阅读 [README.md](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/README.md) 与 [docs/index.rst](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/index.rst)，用自己的话写出 **Clang 解决的三个主要问题**，并列举 **两个除「编译代码」之外的用途**。

**建议步骤**：

1. 重读 [README.md:5-7](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/README.md#L5-L7)，提取「前端」「LLVM 一部分」「工具宿主」三个关键词。
2. 浏览 [docs/index.rst](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/index.rst) 的四个大标题，理解 Clang 文档为什么分成「编译器 / 库 / 工具 / 设计」四类。
3. 写出你的答案。

**参考答题方向**（先自己写，再对照）：

- **三个主要问题**：
  1. 让计算机「读懂」C/C++/Objective-C 源代码（前端职责）；
  2. 把源码翻译成与具体 CPU 无关的 LLVM IR，交给 LLVM 做后端优化与代码生成；
  3. 把「理解源码」的能力封装成库，支撑各类源码级工具。
- **两个非编译用途**：例如「用 Clang Static Analyzer 做 bug 静态检查」「用 clang-format 自动排版代码风格」「用 LibTooling/ASTMatchers 写自定义代码分析或重构工具」。

## 6. 本讲小结

- **Clang 是什么**：LLVM 项目里负责 C/C++/Objective-C 的**编译器前端**，产出 LLVM IR（见 [README.md:5](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/README.md#L5)）。
- **与 LLVM 的关系**：Clang 是 LLVM 的**子项目**，构建期单向依赖 LLVM 的库与 `llvm-tblgen`，运行期把 IR 交给 LLVM 处理（见 [CMakeLists.txt:10](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L10)、[L48](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L48)）。
- **不只是编译器**：Clang 自我定位为「源码级工具平台」，静态分析器就是其一（见 [README.md:7](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/README.md#L7)）。
- **能力以库形式暴露**：文档分为「编译器 / 库 / 工具 / 设计」四类（见 [docs/index.rst](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/index.rst)），`lib/` 下 30 多个子库、`tools/` 下数十个工具共同构成这个平台。
- **前端流水线**：Lex（词法）→ Parse（语法/AST）→ Sema（语义）→ CodeGen（生成 IR），这是后续讲义的主干。
- **读源码的准备**：Clang 用 C++17 编写（见 [CMakeLists.txt:37](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L37)），请备好相应语言基础。

## 7. 下一步学习建议

本讲只做了「认路」。接下来建议：

1. **动手把 Clang 跑起来**——这是下一讲 [u1-l2 获取、构建与运行 Clang](u1-l2-build-and-run.md) 的主题，会讲 CMake 构建方式与构建产物。
2. **摸清目录结构**——[u1-l3 源码目录与代码组织全景](u1-l3-directory-layout.md) 会带你逐个认识 `lib/`、`include/`、`tools/` 的职责。
3. **亲自编译一个程序**——[u1-l4 用 clang 编译第一个程序](u1-l4-first-compile.md) 会从用户视角体验 `clang` 命令，并初步揭开 driver 与 cc1 的分工。

如果想现在就深入，可以提前翻阅 [docs/index.rst](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/docs/index.rst) 中的 `InternalsManual` 与 `IntroductionToTheClangAST`，但本手册会按顺序带你读完它们，不必急于一时。
