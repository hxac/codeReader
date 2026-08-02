# 源码目录与代码组织全景

## 1. 本讲目标

学完本讲，你应当能够：

- 看懂 Clang 仓库顶层的目录划分，知道每个目录（`lib/`、`include/`、`tools/`、`docs/`、`examples/`、`test/` 等）各自承担什么职责。
- 说出 `lib/` 下各子库（`clangLex`、`clangAST`、`clangSema`、`clangCodeGen`、`clangFrontend`……）分别对应编译流程的哪一段，并能按「基础层 → 前端管线 → 编排层 → 工具扩展 → 静态分析」分层归类。
- 理解 `include/` 与 `lib/` 之间的「镜像」对应关系，知道什么是公共 API、什么是内部实现，并指出两个刻意的例外。
- 识别 `tools/` 中常用工具（`clang`、`clang-format`、`clang-check`、`clang-repl`、`libclang` 等）的用途，以及它们是「可执行程序」而不是「库」。
- 通读 `lib/` 与 `tools/` 目录，亲手制作一张「目录职责 → 代表子目录/工具」的映射表。

本讲承接 [u1-l1](u1-l1-project-overview.md) 建立的「Clang = 库 + 工具平台」认知，以及 [u1-l2](u1-l2-build-and-run.md) 讲过的 CMake 构建方式。我们不深入任何单个源文件，只看「骨架是怎么搭起来的」。

## 2. 前置知识

在开始前，先用通俗语言建立三个直觉：

1. **库（library）vs 可执行程序（executable）**。库是被别人「链接使用」的半成品，本身不能直接运行；可执行程序是能敲命令跑起来的成品。Clang 最大的设计特点，是把整个编译器拆成了一堆**库**，`clang` 命令本身只是一个很薄的**可执行程序**，把这些库组装起来。这正对应 [u1-l1](u1-l1-project-overview.md) 提到的「Clang 是源码级工具平台」——因为能力都在库里，别的工具才能复用。

2. **声明（头文件）vs 实现（源文件）**。C++ 习惯把「这个类/函数长什么样」写在头文件（`.h`），把「具体怎么做」写在源文件（`.cpp`）。Clang 把公共头文件统一放在 `include/`，把实现统一放在 `lib/`，两边几乎一一对应。

3. **构建顺序反映依赖关系**。CMake 用 `add_subdirectory()` 一行一行地把子目录注册进构建系统。在 `lib/CMakeLists.txt` 里，被依赖的库排在前面（比如 `Basic` 第一个），依赖别人的库排在后面。读这张清单的顺序，就是在读 Clang 自下而上的依赖图。

如果你对 CMake 的 `add_subdirectory`、`option`、`add_library` 这些基本命令还陌生，建议先复习 [u1-l2](u1-l2-build-and-run.md)。

## 3. 本讲源码地图

本讲只看「骨架文件」，不读具体算法。关键文件如下：

| 文件 | 作用 |
|------|------|
| `CMakeLists.txt`（仓库根） | 顶层总控：定义开关、决定加入哪些子目录、设置头文件搜索路径、生成 `config.h`。 |
| `lib/CMakeLists.txt` | 用一串 `add_subdirectory()` 列出全部 Clang 子库（依赖顺序）。 |
| `lib/Lex/CMakeLists.txt` | 一个典型子库的构建脚本：`add_clang_library(clangLex ...)`，展示「库是怎么定义和互相依赖」的范式。 |
| `tools/CMakeLists.txt` | 用 `add_clang_subdirectory()` 列出全部可执行工具，并体现条件开关。 |
| `tools/driver/CMakeLists.txt` | `clang` 主程序本身的构建脚本：`add_clang_tool(clang ...)` 并链接各 clang 库。 |
| `cmake/modules/AddClang.cmake` | 定义 `add_clang_library`、`add_clang_tool` 等 Clang 专属 CMake 宏，是上述脚本的「动词来源」。 |

> 术语提示：本讲会反复出现「子库（subdirectory/library）」和「工具（tool）」两个词。子库构建产物是 `libclangX.a`/`.so`，工具构建产物是可执行文件。

## 4. 核心概念与源码讲解

### 4.1 lib 子库职责

#### 4.1.1 概念说明

`lib/` 是 Clang 的「发动机舱」。Clang 的设计哲学是**库优先**：编译器不是一个巨大的 `main()`，而是由几十个职责清晰、可单独链接的库拼成。每个子目录就是一个或一组库：

- 每个子目录有自己的 `CMakeLists.txt`。
- 通过 `add_clang_library(clangX ...)` 产出一个名为 `clangX` 的库。
- 库与库之间用 `LINK_LIBS` 声明依赖，形成自下而上的分层。

这样做的回报就是 [u1-l1](u1-l1-project-overview.md) 强调的「平台属性」：`clang-format` 只链接 `clangFormat`、`clangLex`、`clangBasic` 等少数库，而无需拖进整个编译器；静态分析器、索引工具同理。库的拆分粒度直接决定了工具的复用边界。

#### 4.1.2 核心流程：自下而上的构建清单

`lib/CMakeLists.txt` 本质就是一张「按依赖顺序排列的构建清单」。把它读下来，就等于读到了 Clang 的依赖骨架：

```
Basic → Lex → Parse → AST → Sema → CodeGen → ... → Frontend → FrontendTool → Tooling → ...
```

伪代码描述这条流水线对应的库层关系：

```text
clangBasic        (源码位置、诊断、Token 种类 等最底层抽象)
   │ 被 几乎所有 库依赖
   ▼
clangLex          (词法/预处理) ──┐
clangParse        (语法分析)    ──┤ 都依赖 clangBasic、clangAST
clangAST          (AST 节点)    ──┤
clangSema         (语义分析)    ──┘
   ▼
clangCodeGen      (AST → LLVM IR)
   ▼
clangFrontend / clangFrontendTool  (装配并驱动上述管线)
   ▼
clangTooling / clangStaticAnalyzer / clangFormat ...  (在此之上构建工具)
```

实际清单里每个 `add_subdirectory(X)` 一行；理解时不必死记顺序，记住「`Basic` 最底层、`Frontend`/`Tooling` 最顶层」即可。

#### 4.1.3 源码精读

`lib/CMakeLists.txt` 全文就是一串按依赖顺序排好的 `add_subdirectory()`：

[lib/CMakeLists.txt:1-38](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CMakeLists.txt#L1-L38) —— 这就是 Clang 全部子库的「构建清单与依赖顺序」。注意几个要点：

- 第 2 行 `Basic` 排在最前，因为它是地基；
- 第 4–10 行是前端管线核心：`Lex → Parse → AST → ... → Sema → CodeGen`；
- 第 17–19 行 `Serialization → Frontend → FrontendTool` 是装配层；
- 第 36–38 行 `CIR` 被 `if(CLANG_ENABLE_CIR)` 包住，说明它是可选的实验特性（参见 [u14-l2](u14-2-cir.md)）。

要理解「一个库到底怎么定义」，看 `clangLex` 这个典型样本：

[lib/Lex/CMakeLists.txt:8-38](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/CMakeLists.txt#L8-L38) —— `add_clang_library(clangLex ...)` 把一组 `.cpp` 源文件编译成名为 `clangLex` 的库，并通过 `LINK_LIBS clangBasic` 声明它依赖 `clangBasic`。这正是「库优先 + 显式依赖」的范式：源文件列表即库的内部实现，`LINK_LIBS` 即对外的依赖边。

`add_clang_library` 这个「动词」本身定义在 Clang 自己的 CMake 模块里：

[cmake/modules/AddClang.cmake:48-157](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/cmake/modules/AddClang.cmake#L48-L157) —— `add_clang_library` 宏的定义。它在 LLVM 原生 `add_llvm_library` 之上包了一层，统一处理 Clang 库的命名（`clang` 前缀）、安装、符号导出等。第 44 行还有一个 `add_clang_subdirectory` 宏，是 `tools/CMakeLists.txt` 里逐行注册工具时用的。

下表把 `lib/` 的子库按职责分层归类（构建产物名 = `clang` + 目录名，如 `clangLex`）：

| 层 | 子库 | 职责（一句话） |
|------|------|----------------|
| 基础层 | `Basic` | 最底层抽象：源码位置、诊断、Token 种类、目标信息 |
| 基础层 | `Support` | Clang 内部通用工具（内存、字符串、错误适配等） |
| 基础层 | `Lex` | 词法分析与预处理（Lexer、Preprocessor、HeaderSearch） |
| 基础层 | `Headers` | 编译器**自带的 C/C++ 运行时头文件**（如 `stdio.h`），不是 Clang 自己的源码头 |
| 前端管线 | `Parse` | 语法分析（Parser、ParseAST） |
| 前端管线 | `AST` | AST 节点与上下文（ASTContext、Decl、Stmt、Expr、Type） |
| 前端管线 | `ASTMatchers` | 声明式 AST 匹配器 |
| 前端管线 | `Sema` | 语义分析（名字查找、类型检查、模板实例化） |
| 前端管线 | `CodeGen` | 把 AST 翻译成 LLVM IR |
| 前端管线 | `Analysis` | 控制流图（CFG）与数据流分析基础 |
| 编排层 | `Driver` | 命令行驱动（解析参数、构建 Action/Job） |
| 编排层 | `Options` | TableGen 生成的命令行选项表（OptTable） |
| 编排层 | `Frontend` | 前端大总管（CompilerInstance、CompilerInvocation、FrontendAction） |
| 编排层 | `FrontendTool` | 前端执行入口（ExecuteCompilerInvocation） |
| 编排层 | `Serialization` | AST 的序列化/反序列化（PCH/PCM） |
| 工具与扩展 | `Tooling` | LibTooling 框架（ClangTool、CommonOptionsParser、CompilationDatabase） |
| 工具与扩展 | `Index` / `IndexSerialization` | 符号索引 |
| 工具与扩展 | `ExtractAPI` | 为库生成接口摘要 |
| 工具与扩展 | `InstallAPI` | 动态库符号安装清单生成 |
| 工具与扩展 | `Format` | clang-format 背后的排版库 |
| 工具与扩展 | `Edit` / `Rewrite` | 源码编辑与重写 |
| 工具与扩展 | `CrossTU` | 跨翻译单元分析支持 |
| 工具与扩展 | `Interpreter` | 增量编译/REPL（clang-repl 背后） |
| 工具与扩展 | `DependencyScanning` | 依赖扫描（clang-scan-deps 背后） |
| 工具与扩展 | `DirectoryWatcher` | 目录变化监听（供 IDE 用） |
| 工具与扩展 | `UnifiedSymbolResolution` | 统一符号解析 |
| 静态分析 | `StaticAnalyzer` | 路径敏感的静态分析器（Checkers、ExplodedGraph） |
| 静态分析 | `ScalableStaticAnalysis` | 可扩展（SSAA）静态分析 |
| 条件构建 | `Testing` | 测试辅助库（仅测试时构建） |
| 条件构建 | `CIR` | ClangIR（仅 `CLANG_ENABLE_CIR` 时构建） |

#### 4.1.4 代码实践

**实践目标**：亲手把 `lib/` 的子库按依赖层归类，验证「构建顺序 = 依赖顺序」这个判断。

**操作步骤**：

1. 打开 [lib/CMakeLists.txt:1-38](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CMakeLists.txt#L1-L38)，把每行 `add_subdirectory(X)` 的名字抄下来。
2. 按本讲 4.1.3 的分层表，给每个库标注它属于哪一层（基础层 / 前端管线 / 编排层 / 工具扩展 / 静态分析 / 条件）。
3. 挑一个库（例如 `Lex`），打开它的 `lib/Lex/CMakeLists.txt`，看它 `LINK_LIBS` 了谁；再打开被它依赖的那个库的 `CMakeLists.txt`，确认后者在主清单里**排在更前面**。

**需要观察的现象**：

- `clangLex` 只 `LINK_LIBS clangBasic`（见 4.1.3 的链接），而 `clangBasic` 排在主清单第 2 行——确实在 `Lex`（第 4 行）之前。
- 高层库（如 `Frontend`、`Tooling`）的 `LINK_LIBS` 列表会更长，因为它们聚合了大量底层库。

**预期结果**：你会得到一张「库 → 依赖的库 → 在主清单中的行号」表，且「被依赖者行号 < 依赖者行号」始终成立。

**待本地验证**：若想亲眼确认依赖边，可在构建目录用 `cmake --graphviz=dep.dot` 生成依赖图（可选）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `lib/CMakeLists.txt` 里 `Basic` 必须排在 `Lex` 之前，而不能随便排？

**参考答案**：因为 `clangLex` 通过 `LINK_LIBS clangBasic` 显式依赖 `clangBasic`。虽然现代 CMake 用 target 依赖能自动处理顺序，但 Clang 这张清单本身就是按依赖拓扑序写的，把它当作文档来读时，排在前面的就是「更底层、被更多人依赖」的库，便于读者建立分层直觉。

**练习 2**：`lib/Headers` 这个子库「特殊」在哪里？它编译出来的 `clangX` 库和别的库一样吗？

**参考答案**：它特殊在于装的不是 Clang 自己的 C++ 源码实现，而是**编译器随附给用户程序使用的 C/C++ 头文件**（如 `stdio.h`、`__clang_cuda_*.h`，共 246 个文件）。它不以 `.cpp` 为主体，本质上是一组「资源文件」，会被安装为编译器的 resource headers。

---

### 4.2 include 与 lib 对应关系

#### 4.2.1 概念说明

Clang 严格遵循一条「头文件镜像」约定：

- **`include/clang/<模块>/`** 存放**公共 API 的头文件**（`.h`），告诉外部「我能提供哪些类和函数」。
- **`lib/<模块>/`** 存放**这些类的具体实现**（`.cpp`），是内部细节。

二者几乎一一对应：`include/clang/Lex/Lexer.h` 声明 `Lexer` 类，`lib/Lex/Lexer.cpp` 实现它。这条约定让你只需扫一眼 `include/clang/` 的目录名，就能猜到 `lib/` 里有哪些库；反之亦然。它也是 [u3](u3-l1-source-manager.md) 及以后讲义中「读头文件理解接口、读源文件理解实现」这一阅读方法的基础。

#### 4.2.2 核心流程：两个刻意的例外

镜像关系有两个例外，理解它们恰恰能加深对约定的认识：

1. **`lib/Headers` 没有 `include/clang/Headers`**。如 4.1.5 所述，`Headers` 是给用户程序的 C/C++ 头文件，不属于 Clang 自身的 C++ 源码，自然不需要在 `include/clang/` 下声明。
2. **`include/clang/Config` 没有 `lib/Config`**。`Config` 目录里只有一个 `config.h`，它是**构建时生成的**——由顶层 `CMakeLists.txt` 用 `configure_file` 从模板 `config.h.cmake` 渲染出来，记录诸如是否启用 libxml2、平台特性等编译期开关，所以它没有对应的 `.cpp` 实现。

除这两处外，`include/clang/` 的每个子目录都对应 `lib/` 的一个同名子目录。

此外还有一处独立的公共头目录：`include/clang-c/`。它是 **libclang 的稳定 C ABI**（如 `Index.h`、`CXDiagnostic.h`），与 `include/clang/`（内部 C++ API）刻意分开，目的是给第三方（IDE、绑定语言）一个不会随版本频繁变动的接口。

#### 4.2.3 源码精读

顶层 `CMakeLists.txt` 先把 `include` 加入构建，再构建 `lib`——这正是「头文件是 API、库是实现」在构建顺序上的体现：

[CMakeLists.txt:539-560](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L539-L560) —— 先 `add_subdirectory(include)`（第 539 行），后 `add_subdirectory(lib)`（第 558 行）。中间第 542–548 行收集所有 TableGen 生成头文件的目标，因为库的编译依赖这些生成的头。

为了让所有 `.cpp` 都能 `#include "clang/..."`，源码头目录被加进全局搜索路径，且排在最前：

[CMakeLists.txt:431-434](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L431-L434) —— `include_directories(BEFORE ...)` 把「生成的头目录」和「源码头目录」放在搜索路径最前面，所以代码里 `#include "clang/Lex/Lexer.h"` 永远能命中仓库内的 `include/clang/Lex/Lexer.h`。

`include/clang/Config` 的「生成而非实现」证据在这一行：

[CMakeLists.txt:976-978](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L976-L978) —— `configure_file(... config.h.cmake ... config.h)` 把模板渲染成 `config.h`，这就是 `include/clang/Config` 目录里唯一文件的来源。

最后，公共头文件还是 Clang 的「可安装产物」——安装时只装头文件，不装 `.cpp`：

[CMakeLists.txt:436-454](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L436-L454) —— `install(DIRECTORY include/clang include/clang-c ...)` 把 `*.h`/`*.def` 安装出去，作为对外 API；注意它 `EXCLUDE` 掉了 `config.h`（因为那是本机构建产物）。

#### 4.2.4 代码实践

**实践目标**：亲手验证「`include/clang/X` ↔ `lib/X`」的镜像关系，并定位两个例外。

**操作步骤**：

1. 列出 `include/clang/` 的全部子目录名，再列出 `lib/` 的全部子目录名。
2. 逐个比对，找出：
   - 在 `lib/` 但不在 `include/clang/` 的目录 → 应该只有 `Headers`；
   - 在 `include/clang/` 但不在 `lib/` 的目录 → 应该只有 `Config`。
3. 任选一个类，例如 `Lexer`：打开 `include/clang/Lex/Lexer.h`（看声明）与 `lib/Lex/Lexer.cpp`（看实现），确认二者描述的是同一个类。

**需要观察的现象**：

- 两个目录列表「几乎完全相同」，差异恰好是 `Headers` 与 `Config` 两个特例。
- `include/clang/Config/` 下只有 `config.h.cmake`（模板）及构建后生成的 `config.h`，没有 `.cpp`。

**预期结果**：你确认了「公共 API（`include`）与实现（`lib`）镜像对应」这条约定，并能解释为什么这两个目录是例外。

**待本地验证**：`config.h` 要等构建后才会出现在 `build/include/clang/Config/config.h`，源码树里只能看到模板 `config.h.cmake`。

#### 4.2.5 小练习与答案

**练习 1**：`include/clang-c/` 和 `include/clang/` 为什么是两个并列目录，而不是把 C 头文件也塞进 `include/clang/`？

**参考答案**：二者面向不同受众、稳定性要求不同。`include/clang/` 是内部 C++ API，会随版本重构；`include/clang-c/` 是 libclang 的稳定 C ABI，承诺跨版本兼容，供 IDE、Python/Go 绑定等外部消费者使用。把它们物理分开，能时刻提醒维护者「改动 `clang-c` 要顾及 ABI 兼容」。

**练习 2**：如果我新建一个库 `lib/Foo`，按约定应该在 `include/` 下放什么？

**参考答案**：应在 `include/clang/Foo/` 下放该库的公共头文件（`.h`/`.def`），并保证 `lib/Foo/CMakeLists.txt` 用 `add_clang_library(clangFoo ...)` 把对应 `.cpp` 编译成 `clangFoo` 库；同时在 `lib/CMakeLists.txt` 里加一行 `add_subdirectory(Foo)`（位置取决于它依赖谁）。这样镜像约定就保持了。

---

### 4.3 tools 工具一览

#### 4.3.1 概念说明

如果说 `lib/` 是发动机舱，`tools/` 就是「装好方向盘、能开走的整车」。这里每个子目录产出一个**可执行程序**。其中最核心的是 `tools/driver`——它构建出的就是你在命令行敲的 `clang`。

工具和库的关键区别：

- 工具用 `add_clang_tool(name ...)` 定义（而非 `add_clang_library`），产物是可执行文件。
- 工具会 `clang_target_link_libraries` 链接它需要的那些 `clangX` 库——这正是 [u1-l1](u1-l1-project-overview.md)「库优先」的兑现：同一个 `clangFrontend`/`clangAST` 库，被 `clang`、`clang-check`、`clang-format` 等多个工具复用。
- 很多工具被构建开关控制，例如静态分析相关工具受 `CLANG_ENABLE_STATIC_ANALYZER` 控制。

#### 4.3.2 核心流程：工具注册与开关

`tools/CMakeLists.txt` 用 `add_clang_subdirectory(name)` 一行一个地注册工具。这个宏（定义在 `AddClang.cmake:44`）会尊重 `CLANG_BUILD_TOOLS` 开关：当开关关闭时只生成构建目标、不真正产出可执行文件。

伪代码描述一个工具的诞生：

```text
tools/<name>/CMakeLists.txt:
    add_clang_tool(<name>            # 声明这是一个可执行工具
        a.cpp b.cpp
        ...)
    clang_target_link_libraries(<name>  # 链接所需的 clang 库
        PRIVATE clangBasic clangFrontend ...)
```

条件开关的判定流程：

```text
CLANG_BUILD_TOOLS = ON ?         ── 否 ──> 只生成目标，不产出二进制
   │ 是
   ▼
逐个 add_clang_subdirectory(...)：
   if CLANG_ENABLE_STATIC_ANALYZER → clang-check, scan-build, ...
   if CLANG_ENABLE_CIR             → cir-opt, cir-translate, ...
   if HAVE_CLANG_REPL_SUPPORT      → clang-repl
   if CLANG_INCLUDE_TESTS          → c-index-test, apinotes-test
```

#### 4.3.3 源码精读

[tools/CMakeLists.txt:1-58](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/CMakeLists.txt#L1-L58) —— 全部工具的注册清单。注意三个细节：

- 第 5–9 行、第 23–25 行、第 27–30 行、第 40–46 行都是 `if(...)` 包裹的「条件工具」，对应上面的开关判定。
- 第 56 行 `add_clang_subdirectory(libclang)` 注册的是那个稳定 C ABI 库（见 4.2）。
- 第 53 行 `add_llvm_external_project(clang-tools-extra extra)` 把另一个仓库 `clang-tools-extra`（含 `clang-tidy` 等更多工具）作为外部项目挂进来——这就是为什么 `clang-tidy` 不在当前 `tools/` 列表里。

最关键的 `clang` 主程序本身，在 `tools/driver` 里定义：

[tools/driver/CMakeLists.txt:42-53](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/CMakeLists.txt#L42-L53) —— `add_clang_tool(clang driver.cpp cc1_main.cpp cc1as_main.cpp cc1gen_reproducer_main.cpp ...)`。也就是说，`clang` 这个可执行文件由 4 个源文件构成（参见 [u2-l3](u2-l3-cc1-entry.md)）：`driver.cpp` 是 driver 入口，三个 `cc1*_main.cpp` 分别是前端、汇编器、reproducer 的 cc1 入口。

它链接了哪些 clang 库，一目了然：

[tools/driver/CMakeLists.txt:59-67](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/CMakeLists.txt#L59-L67) —— `clang_target_link_libraries(clang PRIVATE clangBasic clangCodeGen clangDriver clangFrontend clangFrontendTool clangOptions clangSerialization)`。这一行就是「`clang` = 这些库的组装」的直接证据，呼应 4.1 讲的库分层。

`clang` 还会派生出几个常用「别名」可执行文件（其实是符号链接）：

[tools/driver/CMakeLists.txt:91-93](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/CMakeLists.txt#L91-L93) —— `add_clang_symlink(clang++ clang)` 等。于是 `clang++`、`clang-cl`、`clang-cpp` 都指向同一个 `clang` 二进制，运行时根据被调用的名字切换行为（C++ 模式、MSVC 兼容模式等）。

控制「是否真的产出工具二进制」的开关在顶层：

[CMakeLists.txt:477-478](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L477-L478) —— `option(CLANG_BUILD_TOOLS "Build the Clang tools. If OFF, just generate build targets." ON)`。默认 ON；关掉后 `add_clang_subdirectory` 只建目标、不产二进制。

[CMakeLists.txt:498-499](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L498-L499) —— `option(CLANG_ENABLE_STATIC_ANALYZER "Include static analyzer in clang binary." ON)`，这个开关会连带决定 `clang-check`、`scan-build` 等工具是否构建（见 `tools/CMakeLists.txt:40-46`）。

常用工具速查表：

| 工具目录 | 产物 | 用途 |
|----------|------|------|
| `driver` | `clang`（及 `clang++`/`clang-cl`/`clang-cpp` 链接） | 编译器主程序，含 cc1/cc1as 入口 |
| `diagtool` | `diagtool` | 查看内置诊断、选项等信息 |
| `clang-format` | `clang-format` | 代码格式化 |
| `clang-check` | `clang-check` | 基于 LibTooling 的语法检查/AST 打印（[u10-l4](u10-l4-clang-check.md)） |
| `clang-refactor` | `clang-refactor` | 重构引擎命令行 |
| `clang-repl` | `clang-repl` | 交互式 C++ 解释器（[u14-l1](u14-l1-clang-repl.md)） |
| `clang-scan-deps` | `clang-scan-deps` | 扫描编译依赖（供构建系统） |
| `clang-installapi` | `clang-installapi` | 生成动态库 API 安装清单 |
| `clang-diff` | `clang-diff` | AST 差异比较 |
| `clang-fuzzer` | `clang-fuzzer` | 模糊测试 |
| `clang-shlib` | `libclang-cpp.so` | 把全部 clang 库打包成单个共享库 |
| `libclang` | `libclang.so` | 稳定 C ABI 库（[u11-l1](u11-l1-libclang.md)） |
| `scan-build` / `scan-build-py` / `scan-view` | 脚本/查看器 | 静态分析器封装与结果查看 |
| `clang-linker-wrapper` / `clang-nvlink-wrapper` / `clang-offload-bundler` / `clang-sycl-linker` / `offload-arch` | 各类卸载/链接工具 | 异构计算的链接与打包 |
| `cir-opt` / `cir-translate` / `cir-lsp-server` | CIR 工具 | ClangIR 相关（仅 `CLANG_ENABLE_CIR`，[u14-l2](u14-2-cir.md)） |
| `c-index-test` / `apinotes-test` | 测试辅助 | 仅 `CLANG_INCLUDE_TESTS` 时构建 |

> 提示：`clang-tidy`、`clang-include-fixer`、`clang-move` 等更多工具不在本仓库 `tools/`，而在 `clang-tools-extra`（由 `tools/CMakeLists.txt:53` 作为外部项目挂入）。

#### 4.3.4 代码实践

**实践目标**：通过源码确认「工具 = 链接若干 clang 库的薄可执行文件」。

**操作步骤**：

1. 打开 [tools/driver/CMakeLists.txt:42-67](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/CMakeLists.txt#L42-L67)，记录 `add_clang_tool(clang ...)` 用了哪些源文件、`clang_target_link_libraries` 链了哪些库。
2. 对比一个更轻量的工具，例如 `tools/diagtool/CMakeLists.txt`，看它链接的库是不是少很多（因为它不需要 CodeGen/AST 等重型能力）。
3. 在 `tools/CMakeLists.txt` 里找出「受 `CLANG_ENABLE_STATIC_ANALYZER` 控制的工具有哪几个」。

**需要观察的现象**：

- `clang` 主程序链接的库最多（`clangBasic clangCodeGen clangDriver clangFrontend clangFrontendTool clangOptions clangSerialization`），因为它要驱动完整管线。
- 功能单一的工具链接的库明显更少，这直观体现「按需取用库」的好处。

**预期结果**：你能写出一张「工具 → 链接的 clang 库集合」表，并理解工具的「胖瘦」取决于它复用了多少编译能力。

**待本地验证**：在构建目录运行 `cmake --build . --target help | grep clang` 可看到全部 clang 相关目标；其中带 `clang-` 前缀的可执行目标对应本节表格里的工具（可选）。

#### 4.3.5 小练习与答案

**练习 1**：`clang++`、`clang-cl`、`clang-cpp` 是三个独立的可执行文件吗？

**参考答案**：不是。由 [tools/driver/CMakeLists.txt:91-93](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/driver/CMakeLists.txt#L91-L93) 可见，它们都是用 `add_clang_symlink` 指向同一个 `clang` 二进制的符号链接。`clang` 启动时会检查自己被以什么名字调用，从而切换为 C++、MSVC 兼容等模式。

**练习 2**：为什么 `clang-tidy` 不在 `tools/` 目录里？

**参考答案**：因为 `clang-tidy` 属于独立仓库 `clang-tools-extra`。`tools/CMakeLists.txt:53` 的 `add_llvm_external_project(clang-tools-extra extra)` 会在构建时把它作为外部项目挂载进来。这样设计是为了「保持主 clang 仓库小而聚焦」（该行注释也这么说）。

---

## 5. 综合实践

把三个模块串起来，完成本讲规格里要求的那张**「目录职责 → 代表子目录/工具」映射表**。这是一个贯穿 `lib/` 与 `tools/` 的小任务。

**任务**：为 Clang 仓库制作一张三列映射表，覆盖 `lib/`（库）、`include/clang/`（API 头）、`tools/`（可执行程序）三层。

**建议步骤**：

1. 从 [lib/CMakeLists.txt:1-38](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CMakeLists.txt#L1-L38) 抄出全部子库，按 4.1.3 的分层归类，填入「库层」列。
2. 对每个库，在 `include/clang/` 下找到同名头目录（注意 `Headers`、`Config` 两个例外，参见 4.2），填入「公共 API 头」列。
3. 从 [tools/CMakeLists.txt:1-58](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/CMakeLists.txt#L1-L58) 抄出工具，按 4.3.3 表格归类，并标注每个工具「主要复用了哪个库」，填入「工具」列。

**产出模板**（节选，请自行补全）：

| 职责 | 代表 lib 子库 | 公共 API 头（include/clang/） | 代表 tools 工具 |
|------|---------------|-------------------------------|-----------------|
| 词法/预处理 | `Lex` | `Lex/` | （被 `clang` 内部使用） |
| AST 节点 | `AST` | `AST/` | `clang-check`、`clang-diff` |
| 语义分析 | `Sema` | `Sema/` | `clang` |
| 生成 IR | `CodeGen` | `CodeGen/` | `clang`（`-emit-llvm`） |
| 命令行驱动 | `Driver` | `Driver/` | `clang` |
| 前端装配 | `Frontend`/`FrontendTool` | `Frontend/` | `clang` |
| 格式化 | `Format` | `Format/` | `clang-format` |
| LibTooling 框架 | `Tooling` | `Tooling/` | `clang-check`、`clang-refactor` |
| 稳定 C ABI | （由 `libclang` 工具封装多个库） | `clang-c/` | `libclang` |
| 静态分析 | `StaticAnalyzer` | `StaticAnalyzer/` | `scan-build`、`clang-check` |
| 增量编译/REPL | `Interpreter` | `Interpreter/` | `clang-repl` |
| 编译器自带头文件 | `Headers`（特例：无对应 include） | — | 安装为 resource headers |
| 构建期配置 | — | `Config/`（特例：生成 `config.h`） | — |

**自检问题**（完成表格后回答）：

- 哪些工具只复用很少的库、因而很「轻」？（如 `diagtool`）
- `clang` 为什么链接的库最多？它链接的 7 个库分别对应哪段管线？
- 两个「镜像例外」是哪两个？为什么是例外？

完成这张表，你就把 Clang「库优先 + 工具复用 + API/实现分离」的整体骨架装进了脑子，后续每一讲都只是钻进其中某一格。

## 6. 本讲小结

- Clang 是**库优先**的：`lib/` 下 30+ 个子库（`clangBasic`、`clangLex`、`clangAST`、`clangSema`、`clangCodeGen`、`clangFrontend`…）按依赖顺序在 [lib/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CMakeLists.txt) 中注册，可分层为「基础层 / 前端管线 / 编排层 / 工具扩展 / 静态分析」。
- `include/clang/` 与 `lib/` 几乎一一镜像：头文件是公共 API、`lib` 是实现；两个例外是 `lib/Headers`（编译器自带 C/C++ 头，无对应 `include`）和 `include/clang/Config`（构建期生成的 `config.h`，无对应 `lib`）。
- `include/clang-c/` 是 libclang 的稳定 C ABI，与内部 C++ API（`include/clang/`）刻意分开。
- `tools/` 产出**可执行程序**，其中 `tools/driver` 构建出 `clang` 主程序，并通过 `add_clang_symlink` 派生出 `clang++`/`clang-cl`/`clang-cpp`。
- 工具的「胖瘦」取决于它复用了多少 clang 库：`clang` 链接 7 个库驱动全管线，`diagtool` 等只链接少量库。
- 许多工具受构建开关控制（`CLANG_BUILD_TOOLS`、`CLANG_ENABLE_STATIC_ANALYZER`、`CLANG_ENABLE_CIR`、`HAVE_CLANG_REPL_SUPPORT`、`CLANG_INCLUDE_TESTS`），`clang-tidy` 等更多工具位于外部仓库 `clang-tools-extra`。

## 7. 下一步学习建议

本讲只看了骨架。接下来建议：

- **进入驱动层**：直接读 [u2-l1 Driver 架构](u2-l1-driver-architecture.md)，从 `tools/driver/driver.cpp` 出发，理解 `clang` 命令如何被解析、分阶段。你会用上本讲认得的 `clangDriver`、`clangFrontend` 库。
- **若想先体验编译流程**：读 [u1-l4 用 clang 编译第一个程序](u1-l4-first-compile.md)，从用户视角跑一遍 `clang -###`，把命令行行为与 4.3 讲的 `clang` 工具对应起来。
- **补充阅读**：浏览顶层 [CMakeLists.txt](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt) 中 `add_subdirectory` 一节（第 534–614 行），那里还列出了 `utils/`、`unittests/`、`test/`、`docs/`、`examples/` 等目录是如何被挂入构建的——本讲聚焦 `lib/`/`include/`/`tools/`，其余目录留待后续用到时再细看。
