# 从源码构建与运行 LLDB

## 1. 本讲目标

前两讲我们建立了对 LLDB 的整体认知（u1-l1）和一张目录心智地图（u1-l2）。本讲要解决一个非常具体的问题：**如何把这份源码变成一个能跑起来的 `lldb` 可执行文件**。

学完本讲，你应当能够：

- 说清 LLDB 的两种 CMake 构建方式（in-tree 随 LLVM 构建、standalone 独立构建）的区别与各自适用场景。
- 看懂 `lldb/CMakeLists.txt` 与 `lldb/cmake/modules/LLDBConfig.cmake` 里那些 `option(...)` / `add_optional_dependency(...)` 到底控制了什么。
- 知道一次构建会产生哪些关键产物：命令行驱动 `lldb`、调试服务端 `lldb-server`、聚合动态库 `liblldb`。
- 理解 `LLDB_INCLUDE_TESTS` / `LLDB_INCLUDE_UNITTESTS` 如何按需打开或关闭测试目标，从而在「只想快速得到一个可用的 lldb」时加速构建。
- 独立完成一次从配置（cmake）到编译（ninja）再到验证（`./bin/lldb --version`）的完整流程。

## 2. 前置知识

在动手之前，先用最朴素的语言解释几个本讲会用到的概念。如果你已经熟悉，可以跳过。

- **CMake**：它本身**不编译代码**，而是一个「构建文件生成器」。你给它一份 `CMakeLists.txt`（用 CMake 自己的脚本语言写的配置），它读取后生成 Ninja / Makefile / Visual Studio 工程等真正的构建文件。所以 LLDB 的构建永远是两步：先 `cmake` 配置，再用对应工具（推荐 Ninja）真正编译。
- **Ninja**：一个极快的构建工具，LLVM/LLDB 官方强烈推荐。它的速度来自「为生成而设计」——`CMakeLists.txt` 生成的 `build.ninja` 文件描述了精确的依赖图，Ninja 只重编译真正受影响的部分。
- **monorepo（单体仓库）**：LLVM 现在把 LLVM、Clang、LLDB、lld 等子项目放在同一个 git 仓库 `llvm-project` 里。LLDB **依赖 Clang 和 LLVM 才能构建**（它内嵌 Clang 来求值表达式），所以「构建 LLDB」本质上离不开这两个邻居。
- **target（构建目标）**：CMake 里的一个可构建单元，比如一个可执行文件 `lldb`、一个动态库 `liblldb`。你可以只构建某个 target：`ninja lldb`，Ninja 就只编译它依赖的内容。
- **构建类型（CMAKE_BUILD_TYPE）**：`Release` 体积小速度快、`Debug` 带调试信息体积大、`RelWithDebInfo` 折中。LLDB 这样的大项目，日常开发常选 `Release` 以节省时间。

> 关于 LLDB 为何离不开 Clang/LLVM，以及它如何被组织成「既是调试器又是可复用库」，参见 u1-l1；关于 `source/`、`tools/`、`include/` 的职责分层，参见 u1-l2。本讲直接站在它们之上。

## 3. 本讲源码地图

本讲围绕「构建系统如何描述自己」展开，涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `lldb/CMakeLists.txt` | LLDB 的顶层构建入口，决定按什么顺序 `add_subdirectory`，以及测试/绑定是否纳入构建。 |
| `lldb/cmake/modules/LLDBConfig.cmake` | 集中定义绝大多数 `LLDB_*` 选项（脚本语言、framework、可选依赖等），是「选项字典」的真身。 |
| `lldb/tools/CMakeLists.txt` | 决定哪些可执行入口（driver、lldb-server、lldb-dap…）被构建。 |
| `lldb/source/API/CMakeLists.txt` | 定义聚合动态库 `liblldb`，列出它链接的全部内部模块。 |
| `lldb/tools/driver/Driver.cpp` | 命令行驱动入口，`--version` 在这里被处理（用于验证产物）。 |
| `lldb/docs/resources/build.md` | 官方构建指南，本讲的命令行示例主要来自这里。 |
| `lldb/docs/resources/test.md` | 官方测试指南，解释 `check-lldb` 等目标。 |

记忆线索：**`CMakeLists.txt` 管「顺序与开关」，`LLDBConfig.cmake` 管「选项含义」，`tools/` 与 `source/API/` 管「产物是谁」**。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. 两种构建方式：in-tree 与 standalone
2. 关键 CMake 选项与可选依赖
3. 构建产物：lldb / lldb-server / liblldb
4. 测试目标的开关与产物验证

---

### 4.1 两种构建方式：in-tree 与 standalone

#### 4.1.1 概念说明

LLDB 构建有两条路：

- **in-tree（随 LLVM 构建，官方推荐）**：把 CMake 指向 monorepo 的 `llvm` 目录，用 `LLVM_ENABLE_PROJECTS="clang;lldb"` 告诉构建系统「顺带把 Clang 和 LLDB 一起建」。一个构建树搞定 LLVM + Clang + LLDB，最省心。
- **standalone（独立构建）**：先单独构建一份 LLVM+Clang（产物在 `llvm-build`），再把 CMake 指向 `lldb` 目录，通过 `LLVM_DIR` / `Clang_DIR` 把已经建好的 LLVM/Clang「借」过来。需要两个构建树，但可以单独重编 LLDB 而不动 LLVM，迭代 LLDB 时更快。

LLDB 顶层 `CMakeLists.txt` 用一个简单判断来区分这两种情形：**如果 CMake 的源目录就等于当前文件所在目录，说明你是直接对着 `lldb` 跑的 cmake，那就是 standalone**。

#### 4.1.2 核心流程

```
            你运行 cmake 时 -S 指向哪里？
                       │
        ┌──────────────┴───────────────┐
        ▼                              ▼
   指向 llvm/ (monorepo)         指向 lldb/
   → in-tree 构建                 → standalone 构建
   → 一个 build 树                → 需要 LLVM/Clang 已先建好
   → LLVM_ENABLE_PROJECTS         → 通过 LLVM_DIR/Clang_DIR 引入
     带上 "clang;lldb"            → set LLDB_BUILT_STANDALONE TRUE
```

无论哪种方式，最终都会进入同一套后续流程：`include(LLDBConfig)` 读选项 → `add_subdirectory(source)` 建库 → `add_subdirectory(tools)` 建可执行文件 → `check_lldb_plugin_layering()` 校验分层（u1-l2 提到的 `LLDBLayeringCheck.cmake`）。

#### 4.1.3 源码精读

下面这段是 LLDB 判断「我是不是被独立构建」的关键，位于顶层 CMakeLists：

[CMakeLists.txt:25-31](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/CMakeLists.txt#L25-L31) — 当源目录等于当前目录时，声明 `project(lldb)`、置 `LLDB_BUILT_STANDALONE TRUE`，并把测试默认打开。

含义：standalone 模式下，`LLDB_BUILT_STANDALONE` 这个变量会触发后续的 `include(LLDBStandalone)`（负责找到并引入已建好的 LLVM/Clang）：

[CMakeLists.txt:39-46](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/CMakeLists.txt#L39-L46) — 仅 standalone 时设置 C++17 标准、并 `include(LLDBStandalone)` 与 `HandleDoxygen`。

不管哪种模式，紧接着都会执行这三行，它们是 LLDB 构建的「骨架」：

[CMakeLists.txt:48-50](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/CMakeLists.txt#L48-L50) — `LLDBConfig` 装载选项、`AddLLDB` 提供 `add_lldb_library/tool` 宏、`LLDBLayeringCheck` 准备分层校验。

对应到命令行，官方文档给出的两种典型配置如下（注意源目录参数的不同）：

[docs/resources/build.md:170](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/build.md#L170) — in-tree：cmake 指向 `llvm-project/llvm`，用 `LLVM_ENABLE_PROJECTS="clang;lldb"` 带上 LLDB。

[docs/resources/build.md:221-225](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/build.md#L221-L225) — standalone：cmake 指向 `llvm-project/lldb`，用 `LLVM_DIR` 指向已建好的 LLVM 构建树。

#### 4.1.4 代码实践

1. **实践目标**：能从源码判断一次构建属于哪种模式，并写出对应的 cmake 命令。
2. **操作步骤**：
   - 打开 `lldb/CMakeLists.txt`，定位到第 27 行的 `if (CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)`。
   - 思考：如果你执行 `cmake -S lldb -B build-lldb`，`CMAKE_SOURCE_DIR`（顶层源目录）和 `CMAKE_CURRENT_SOURCE_DIR`（本文件目录）分别是什么？二者相等吗？
   - 写出一条 in-tree 配置命令（指向 `llvm-project/llvm`）和一条 standalone 配置命令（指向 `llvm-project/lldb` 并带 `LLVM_DIR`）。
3. **需要观察的现象**：standalone 命令里若漏掉 `LLVM_DIR`，cmake 会因为找不到 LLVM 的 CMake 模块而报错。
4. **预期结果**：你能口述「指向 `llvm` 就是 in-tree、指向 `lldb` 就是 standalone」这条判据，并能解释它来自第 27 行那个比较。
5. 若本地没有完整 LLVM 源码可构建，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`LLDB_BUILT_STANDALONE` 这个变量在哪一行被设置为 `TRUE`？它随后触发了哪个 `include`？

**答案**：在 [CMakeLists.txt:29](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/CMakeLists.txt#L29) 被置为 `TRUE`；随后在 [CMakeLists.txt:44](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/CMakeLists.txt#L44) 触发 `include(LLDBStandalone)`（连同 `HandleDoxygen`）。

**练习 2**：为什么 in-tree 构建不需要 `LLVM_DIR`，而 standalone 需要？

**答案**：in-tree 时 LLDB 与 LLVM/Clang 在同一个 CMake 工程里，目标直接可见；standalone 时 LLDB 是独立工程，必须通过 `LLVM_DIR`（指向已建好的 `lib/cmake/llvm`）告诉 CMake 去哪儿找 LLVM 的导入模块。

---

### 4.2 关键 CMake 选项与可选依赖

#### 4.2.1 概念说明

LLDB 的能力是「可裁剪」的：Python/Lua 脚本、行编辑（editline）、终端界面（curses）、压缩（LZMA）、XML、语法高亮（Tree-sitter）等都是**可选依赖**。CMake 用一个统一的 `add_optional_dependency` 宏来处理它们——默认 `Auto`（能找到就用），你也可以强制 `On`/`Off`。

除了可选依赖，还有一组真正的 `option(...)`：如 `LLDB_BUILD_FRAMEWORK`（macOS 打包成 framework）、`LLDB_ENABLE_PYTHON_LIMITED_API`（用 Python 稳定 ABI）、`LLDB_ENABLE_DYNAMIC_SCRIPTINTERPRETERS`（脚本插件编译成动态库）等。

理解这些选项的意义在于：**关掉用不到的依赖能显著加速构建、减小产物，也是交叉编译时的常用手段**（目标机上没有 Python 开发头文件时就得 `-DLLDB_ENABLE_PYTHON=0`）。

#### 4.2.2 核心流程

```
add_optional_dependency(LLDB_ENABLE_PYTHON ...)
        │  默认 "Auto"
        ├─ Auto → find_package(Python) ，找到→启用，找不到→关闭
        ├─ On   → find_package(... REQUIRED) ，找不到就报错
        └─ Off  → 直接关闭，不查找
        │
        ▼
   写入 LLDB_ENABLE_PYTHON 这个 CMake 变量
        │
        ▼
   顶层 CMakeLists 据 if(LLDB_ENABLE_PYTHON) 决定是否 add_subdirectory(bindings)
```

#### 4.2.3 源码精读

可选依赖的「三态」逻辑集中在这个宏里：

[cmake/modules/LLDBConfig.cmake:21-52](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/cmake/modules/LLDBConfig.cmake#L21-L52) — `add_optional_dependency` 宏：把变量默认设为 `Auto`，根据 `Auto/On/Off` 决定是否 `find_package` 以及是否 `REQUIRED`。

随后一连串调用声明了 LLDB 的全部可选依赖：

[cmake/modules/LLDBConfig.cmake:59-66](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/cmake/modules/LLDBConfig.cmake#L59-L66) — 声明 SWIG、LibEdit、Curses、LZMA、Lua、Python、LibXml2、Tree-sitter 八个可选依赖。

注意 Python 与 Lua 都隐含「需要 SWIG」——它们通过 `FindPythonAndSwig` / `FindLuaAndSwig` 模块同时查找解释器和 SWIG。

真正的 `option(...)`（非 Auto，纯开关）在这里：

[cmake/modules/LLDBConfig.cmake:68-76](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/cmake/modules/LLDBConfig.cmake#L68-L76) — `LLDB_BUILD_FRAMEWORK`、`LLDB_USE_SYSTEM_DEBUGSERVER`、`LLDB_ENFORCE_STRICT_TEST_REQUIREMENTS` 等选项。

其中 `LLDB_BUILD_FRAMEWORK` 只在 Apple 平台有效：

[cmake/modules/LLDBConfig.cmake:96-98](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/cmake/modules/LLDBConfig.cmake#L96-L98) — 非 Apple 平台开启 framework 会直接 `FATAL_ERROR`。

顶层 `CMakeLists.txt` 则根据 `LLDB_ENABLE_PYTHON`/`LLDB_ENABLE_LUA` 决定是否构建 `bindings/`（SWIG 绑定，u1-l2 提到过）：

[CMakeLists.txt:141-143](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/CMakeLists.txt#L141-L143) — 任一脚本语言启用时才 `add_subdirectory(bindings)`。

可选依赖与 CMake flag 的速查表（来自官方文档）：

[docs/resources/build.md:49-57](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/build.md#L49-L57)

| 功能 | CMake flag | 说明 |
| --- | --- | --- |
| Python 脚本 | `LLDB_ENABLE_PYTHON` | 需 Python 3.8+（Windows 3.11+），跑测试套件必须有 |
| Lua 脚本 | `LLDB_ENABLE_LUA` | 支持 Lua 5.3 / 5.4 |
| 行编辑 | `LLDB_ENABLE_LIBEDIT` | 命令行历史、Emacs/Vi 模式 |
| 终端 UI | `LLDB_ENABLE_CURSES` | `gui` 命令的 TUI |
| 压缩 | `LLDB_ENABLE_LZMA` | 解压压缩的调试信息 |
| XML | `LLDB_ENABLE_LIBXML2` |  plist 等 |
| SWIG | （由 Python/Lua 触发） | 生成脚本绑定，需 4.0+ |

#### 4.2.4 代码实践

1. **实践目标**：学会用 CMake 状态信息确认某个可选依赖是否被启用。
2. **操作步骤**：
   - 执行一次配置（in-tree 或 standalone 均可），在 cmake 输出里搜索形如 `Enable Python scripting support in LLDB: TRUE/FALSE` 的状态行（由 `message(STATUS ...)` 打印，见宏的最后一行）。
   - 重新配置并强制关闭 Python：追加 `-DLLDB_ENABLE_PYTHON=Off`，观察该状态行变为 `FALSE`，且 `bindings` 不再被构建。
   - 再试 `-DLLDB_ENABLE_PYTHON=On` 但不装 Python 开发包，观察 cmake 因 `REQUIRED` 而报错。
3. **需要观察的现象**：`Auto` 时自动探测；`On` + 缺失时报错；`Off` 时静默关闭。
4. **预期结果**：你能解释「为什么没装 SWIG 时即便 `Auto` 也会关掉 Python」——因为 `FindPythonAndSwig` 同时要求两者。
5. 若本地环境受限，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：把 `LLDB_ENABLE_PYTHON` 设成 `On` 但系统没装 SWIG，会发生什么？

**答案**：因为 `add_optional_dependency` 在 `On` 时会带 `REQUIRED` 调用 `find_package(PythonAndSwig)`，而该模块同时查找 SWIG，找不到就会让 cmake 配置失败报错。

**练习 2**：`LLDB_BUILD_FRAMEWORK` 在 Linux 上开启会怎样？为什么？

**答案**：会触发 `FATAL_ERROR`，因为 framework 是 Apple 专属产物，代码在 [LLDBConfig.cmake:97-98](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/cmake/modules/LLDBConfig.cmake#L97-L98) 显式要求 `APPLE`。

**练习 3**：交叉编译只想要 `lldb-server` 时，通常会关掉哪三个选项？

**答案**：`LLDB_ENABLE_PYTHON`、`LLDB_ENABLE_LIBEDIT`、`LLDB_ENABLE_CURSES`（服务端不需要脚本与交互式 UI），见 [docs/resources/build.md:473-477](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/build.md#L473-L477)。

---

### 4.3 构建产物：lldb / lldb-server / liblldb

#### 4.3.1 概念说明

一次完整的 LLDB 构建会产出三类核心产物，对应 u1-l2 里讲的「薄壳入口」与「可复用库」：

- **`lldb`**：命令行驱动（driver），位于 `tools/driver/`。它是个很薄的壳，真正干活的是 `liblldb`。
- **`lldb-server`**：调试服务端，位于 `tools/lldb-server/`。远程调试时跑在目标机上，**它刻意不链接 `liblldb`**，而是直接用内部库与插件（u1-l2 提到的前后端分离）。
- **`liblldb`**：把几乎所有内部模块（Core、Target、Symbol、Expression、Breakpoint、各插件…）聚合成的单一动态库，对外暴露稳定的 SB API。

理解产物的关键在于：**`tools/CMakeLists.txt` 决定「建哪些可执行文件」，`source/API/CMakeLists.txt` 决定「liblldb 由哪些内部库拼成」**。

#### 4.3.2 核心流程

```
add_subdirectory(source)   ← 先建一堆内部静态库（lldbCore, lldbTarget, ...）
        │
add_subdirectory(tools)    ← 再建可执行入口
        │
        ├─ driver       → lldb        （链接 liblldb）
        ├─ lldb-server  → lldb-server （直接链接内部库 + 插件）
        ├─ lldb-dap     → lldb-dap    （链接 liblldb，IDE 适配器）
        └─ lldb-test … （测试用工具，默认 EXCLUDE_FROM_ALL）

其中 liblldb 由 source/API/CMakeLists.txt 定义：
   add_lldb_library(liblldb SHARED … LINK_LIBS lldbCore lldbTarget … ${LLDB_ALL_PLUGINS})
```

`lldb-server` 是否构建由平台决定：

[cmake/modules/LLDBConfig.cmake:406-410](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/cmake/modules/LLDBConfig.cmake#L406-L410) — 仅当目标系统是 AIX/Android/Darwin/FreeBSD/Linux/NetBSD/OpenBSD/Windows 之一时，`LLDB_CAN_USE_LLDB_SERVER` 才为 `ON`。

#### 4.3.3 源码精读

`tools/CMakeLists.txt` 列出了所有可执行入口，注意 `lldb-test` 被标记为按需构建：

[tools/CMakeLists.txt:1-12](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/tools/CMakeLists.txt#L1-L12) — `argdumper`、`driver`（即 `lldb`）总是构建；`lldb-test`/`lldb-fuzzer` 用 `EXCLUDE_FROM_ALL`（仅被依赖时才建）；`lldb-dap`、`lldb-mcp` 等常态构建。

`lldb-server` 受平台开关控制：

[tools/CMakeLists.txt:31-33](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/tools/CMakeLists.txt#L31-L33) — 仅 `LLDB_CAN_USE_LLDB_SERVER` 为真时才加入 `lldb-server`。

`liblldb` 的定义与它的「聚合」本质（注意 `LINK_LIBS` 那一长串和 `${LLDB_ALL_PLUGINS}`）：

[source/API/CMakeLists.txt:36](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/CMakeLists.txt#L36) — `add_lldb_library(liblldb SHARED …)` 起始处，这是 SB API 全部 `SB*.cpp` 的归宿。

它链接的内部模块清单（这就是 u1-l2 说的「API 模块最后构建，因为它要链接几乎所有其他模块」的直接证据）：

[source/API/CMakeLists.txt:128-141](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/CMakeLists.txt#L128-L141) — `LINK_LIBS` 包含 `lldbBreakpoint / lldbCore / lldbDataFormatters / lldbExpression / lldbHost / lldbInterpreter / lldbSymbol / lldbTarget / lldbUtility / lldbValueObject / lldbVersion` 以及 `${LLDB_ALL_PLUGINS}`（全部插件）。

在非 Windows 平台，这个动态库的输出名被设为 `lldb`（于是文件是 `liblldb.so`）：

[source/API/CMakeLists.txt:252-257](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/CMakeLists.txt#L252-L257) — `OUTPUT_NAME lldb`，配合 Unix 的 `lib` 前缀得到 `liblldb.so`。

命令行驱动里 `--version` 的处理（本讲验证产物就靠它）：

[tools/driver/Driver.cpp:214-216](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/tools/driver/Driver.cpp#L214-L216) — 解析到 `--version` 时置 `m_print_version`。

[tools/driver/Driver.cpp:402-405](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/tools/driver/Driver.cpp#L402-L405) — 打印 `SBDebugger::GetVersionString()` 后退出。版本号字符串本身来自 [LLDBConfig.cmake:334-347](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/cmake/modules/LLDBConfig.cmake#L334-L347)（默认沿用 LLVM 版本）。

构建命令上，官方建议显式指定要编的目标：

[docs/resources/build.md:180-181](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/build.md#L180-L181) — `ninja lldb lldb-server`。

[docs/resources/build.md:188](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/build.md#L188) — 平台不支持 lldb-server 时只 `ninja lldb`。

#### 4.3.4 代码实践

1. **实践目标**：构建完成后能定位三类产物并理解它们的来源。
2. **操作步骤**：
   - 完成 `cmake` 配置后执行 `ninja lldb`（若平台支持再加 `lldb-server`）。
   - 在构建目录的 `bin/` 下找到 `lldb`（以及可能的 `lldb-server`）；在 `lib/` 下找到 `liblldb.so`（macOS 为 `liblldb.dylib`，Windows 为 `liblldb.dll`）。
   - 用 `ldd bin/lldb`（Linux）或 `otool -L bin/lldb`（macOS）观察 `lldb` 是否链接了 `liblldb`。
   - 运行 `./bin/lldb --version`，对照 [Driver.cpp:403](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/tools/driver/Driver.cpp#L403) 理解这行输出来自 `SBDebugger::GetVersionString()`。
3. **需要观察的现象**：`lldb` 体积很小，因为它只是壳；真正庞大的是 `liblldb`。
4. **预期结果**：`./bin/lldb --version` 打印出形如 `lldb-version-X.Y.Z` 的版本串。
5. 若未实际构建，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `lldb-server` 不像 `lldb` 那样链接 `liblldb`？

**答案**：`lldb-server` 是部署到目标机的精简服务端，为了减小体积与依赖、并避免与前端共享同一份动态库 ABI，它直接静态链接所需的内部库与插件（u1-l2 的前后端分离设计）。

**练习 2**：`liblldb` 的 `LINK_LIBS` 里出现的 `${LLDB_ALL_PLUGINS}` 是什么？

**答案**：它是全局属性 `LLDB_PLUGINS` 里收集的全部插件目标（由各插件的 `add_lldb_plugin` 注册），意味着 liblldb 把所有插件都静态聚合进来了，所以一个 `liblldb.so` 就能提供完整能力。

**练习 3**：`lldb-test` 为什么默认不构建？

**答案**：它在 [tools/CMakeLists.txt:8](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/tools/CMakeLists.txt#L8) 用了 `EXCLUDE_FROM_ALL`，只在被依赖（典型是被 `check-lldb` 依赖）时才构建，避免普通构建拉入测试工具。

---

### 4.4 测试目标的开关与产物验证

#### 4.4.1 概念说明

LLDB 的测试套件很大（API 测试要编译大量小程序），如果你只是想得到一个可用的 `lldb`，**关掉测试能极大加速构建**。控制它的就是两个变量：

- `LLDB_INCLUDE_TESTS`：总开关。关掉后 `test/`、`unittests/`、`utils/` 都不纳入构建。
- `LLDB_INCLUDE_UNITTESTS`：细粒度开关，仅控制 C++ 单元测试（基于 gtest）；当找不到 `llvm_gtest` 目标时自动关闭。

此外，运行测试还需要 Python 脚本支持——这也是为什么「想跑测试就必须开 Python」。

#### 4.4.2 核心流程

```
LLDB_INCLUDE_TESTS  (默认 = LLVM_INCLUDE_TESTS)
        │
        ├─ ON  → add_subdirectory(test)
        │        └─ if LLDB_INCLUDE_UNITTESTS → add_subdirectory(unittests)
        │        └─ add_subdirectory(utils)
        └─ OFF → 三者都不建，构建变快，但没有 check-lldb 目标

LLDB_INCLUDE_UNITTESTS
        ├─ ON  (默认) ，但前提是存在 llvm_gtest 目标
        └─ 若 NOT TARGET llvm_gtest → 强制 OFF
```

测试运行则交给 lit 驱动与若干目标：

| 目标 | 作用 |
| --- | --- |
| `check-lldb` | 跑全部三类测试 |
| `check-lldb-unit` | 仅 C++ 单元测试 |
| `check-lldb-api` | 仅 SB API（Python）测试 |
| `check-lldb-shell` | 仅 Shell（lit/FileCheck）测试 |

#### 4.4.3 源码精读

顶层 CMakeLists 里测试开关的定义与使用：

[CMakeLists.txt:36](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/CMakeLists.txt#L36) — `option(LLDB_INCLUDE_TESTS ... ${LLVM_INCLUDE_TESTS})`，默认跟随 LLVM 总开关（standalone 时被第 30 行设为 `ON`）。

[CMakeLists.txt:196-199](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/CMakeLists.txt#L196-L199) — `LLDB_INCLUDE_UNITTESTS` 默认 `ON`，但若不存在 `llvm_gtest` 目标就置 `OFF`。

[CMakeLists.txt:201-207](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/CMakeLists.txt#L201-L207) — `LLDB_INCLUDE_TESTS` 为真时才 `add_subdirectory(test)`，并在单元测试开关为真时加 `unittests`，再加 `utils`。

测试目标的含义与运行方式，官方文档给了清晰说明：

[docs/resources/test.md:5-17](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/test.md#L5-L17) — 三类测试：单元测试（gtest）、Shell 测试（lit + FileCheck）、API 测试（Python + dotest.py）。

[docs/resources/test.md:529](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/test.md#L529) — `ninja check-lldb` 跑全套。

[docs/resources/test.md:566-568](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/test.md#L566-L568) — `check-lldb-unit` / `check-lldb-api` / `check-lldb-shell` 分别跑子集。

#### 4.4.4 代码实践

1. **实践目标**：用关闭测试的方式做一次「快速构建」，并验证产物可用。
2. **操作步骤**：
   - 配置时加 `-DLLDB_INCLUDE_TESTS=OFF`（若想顺带确认 Python，可保留 `LLDB_ENABLE_PYTHON=Auto`）。
   - `ninja lldb`（或 `ninja lldb lldb-server`）。
   - 验证：`./bin/lldb --version`。
   - 可选验证 Python：`./bin/lldb -P` 打印 Python 模块路径，按 [docs/resources/build.md:688](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/build.md#L688) 设置 `PYTHONPATH` 后 `python -c 'import lldb'`。
3. **需要观察的现象**：关闭测试后，cmake 不会 `add_subdirectory(test)`，构建目录里没有 `check-lldb` 目标；编译时间明显缩短。
4. **预期结果**：`--version` 正常输出版本串；若开了 Python，`import lldb` 成功。
5. 若环境无法构建，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`LLDB_INCLUDE_UNITTESTS` 在什么情况下会被自动关闭？

**答案**：当构建树里不存在 `llvm_gtest` 目标时（见 [CMakeLists.txt:197-199](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/CMakeLists.txt#L197-L199)），因为单元测试依赖 googletest。

**练习 2**：在 Windows 上想跳过测试，官方建议用什么选项？为什么？

**答案**：用 `LLDB_INCLUDE_TESTS=OFF`（见 [docs/resources/build.md:286-288](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/build.md#L286-L288)），因为 Windows 上测试套件还需要 lld，若不想引入 lld 就直接关掉测试。

**练习 3**：跑 API 测试为什么必须有 Python？

**答案**：API 测试用 Python 写、基于 `dotest.py` 框架（见 [docs/resources/test.md:15-17](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/test.md#L15-L17)），它通过 SB API 与调试器交互，而 SB API 的 Python 绑定依赖 `LLDB_ENABLE_PYTHON`。

---

## 5. 综合实践

把四个模块串起来，完成一次「从零到验证」的最小构建。

**任务**：在本地或容器中，用 CMake + Ninja 从源码构建一个能跑的 `lldb`，要求**关闭测试以加速**，并记录你启用的全部选项，最后用 `--version` 验证。

**步骤（以 in-tree 为例；standalone 同理，只需改源目录与加 `LLVM_DIR`）**：

1. 准备依赖（Ubuntu 示例，摘自 [docs/resources/build.md:64](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/build.md#L64)）：
   ```bash
   sudo apt-get install build-essential cmake ninja-build python3-dev swig libedit-dev libncurses5-dev libxml2-dev
   ```

2. 配置（关闭测试、保留 Python 与常用可选依赖、用 Release 加速）：
   ```bash
   cmake -G Ninja -S llvm-project/llvm -B build-lldb \
       -DCMAKE_BUILD_TYPE=Release \
       -DLLVM_ENABLE_PROJECTS="clang;lldb" \
       -DLLDB_INCLUDE_TESTS=OFF \
       -DLLDB_ENABLE_PYTHON=ON
   ```
   记录：你启用了 `LLVM_ENABLE_PROJECTS`、`CMAKE_BUILD_TYPE`、`LLDB_INCLUDE_TESTS=OFF`、`LLDB_ENABLE_PYTHON=ON`。对照 4.2，能解释每一项的含义。

3. 编译（只编需要的 target）：
   ```bash
   cmake --build build-lldb --target lldb      # Linux 还可加 lldb-server
   ```

4. 验证产物（对应 4.3、4.4）：
   ```bash
   ./build-lldb/bin/lldb --version             # 应打印版本串
   ./build-lldb/bin/lldb -P                    # 打印 Python 模块路径
   PYTHONPATH=$(./build-lldb/bin/lldb -P) python3 -c 'import lldb; print("ok")'
   ```

**观察清单**：
- `bin/lldb` 存在；`lib/liblldb.so`（或 `.dylib`/`.dll`）存在。
- `lldb --version` 输出来自 [Driver.cpp:403](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/tools/driver/Driver.cpp#L403) 的 `SBDebugger::GetVersionString()`。
- 因为 `LLDB_INCLUDE_TESTS=OFF`，`build-lldb/` 里没有 `check-lldb` 目标——这正是「加速」的体现。

**进阶（可选）**：再建一个 standalone 构建树对照：先用 `ninja` 建好 `llvm-build`，再 `cmake -S llvm-project/lldb -B build-lldb-standalone -DLLVM_DIR=$(pwd)/llvm-build/lib/cmake/llvm`，体会两者差异。

> 若本地/容器没有足够资源或完整 LLVM 源码，将命令行视为待验证方案，先确保你能读懂每一步对应的源码位置即可。

## 6. 本讲小结

- LLDB 有两种构建方式：**in-tree**（指向 `llvm`，一个树搞定，官方推荐）与 **standalone**（指向 `lldb`，借已建好的 LLVM/Clang，迭代 LLDB 更快），判据在顶层 [CMakeLists.txt:27](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/CMakeLists.txt#L27)。
- 选项的「真身」在 `cmake/modules/LLDBConfig.cmake`：可选依赖用 `add_optional_dependency` 三态（Auto/On/Off），纯开关用 `option(...)`；`LLDB_ENABLE_PYTHON/LUA` 触发 `bindings/` 的构建。
- 三类核心产物：命令行驱动 `lldb`（壳）、调试服务端 `lldb-server`（平台相关，不链接 liblldb）、聚合动态库 `liblldb`（在 [source/API/CMakeLists.txt:36](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/CMakeLists.txt#L36) 定义，链接几乎全部内部模块与插件）。
- `LLDB_INCLUDE_TESTS`（总开关）与 `LLDB_INCLUDE_UNITTESTS`（gtest 细分开关）控制测试目标；只想快速得到可用 `lldb` 时设 `OFF` 可大幅加速。
- 验证产物最简方式：`./bin/lldb --version`，其输出来自 `SBDebugger::GetVersionString()`，版本号默认沿用 LLVM 版本。
- 构建永远是两步：`cmake` 配置（生成 `build.ninja`）→ `ninja <target>` 编译；可只编 `lldb` / `lldb lldb-server` 等目标以省时。

## 7. 下一步学习建议

- **下一讲 u1-l4（第一次运行：lldb CLI 驱动入口）** 将带你在刚构建出的 `lldb` 之上，跟随 `tools/driver/Driver.cpp` 的 `main()` 走通从参数解析到 `SBDebugger::Initialize` 再到 `RunCommandInterpreter` 的启动主链路——本讲的 `--version` 正是这条链路上的一个早退分支，正好承上启下。
- 想深入构建细节，可读 `cmake/modules/AddLLDB.cmake`（`add_lldb_library` / `add_lldb_tool` 宏的实现）与 `cmake/modules/LLDBStandalone.cmake`（standalone 如何引入 LLVM）。
- 计划跑测试的读者，直接精读 [docs/resources/test.md](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/test.md)，并尝试 `ninja check-lldb-shell` 跑一组最轻量的 Shell 测试（届时需把 `LLDB_INCLUDE_TESTS` 打开并确保 Python 可用）。
