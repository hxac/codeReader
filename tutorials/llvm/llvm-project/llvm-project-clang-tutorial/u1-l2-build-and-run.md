# 获取、构建与运行 Clang

## 1. 本讲目标

上一讲（u1-l1）我们已经知道：Clang 是 LLVM 中负责 C/C++/Objective-C 的编译器前端，并且它把自己「读懂源码」的能力（词法、AST、语义）封装成了可复用的 C++ 库。本讲要回答的是**下一个最自然的问题**：这套庞大的 C++ 代码，到底怎么把它从源码变成一个可以执行的 `clang` 命令？

学完本讲，你应当能够：

- 看懂 Clang 顶层 `CMakeLists.txt` 里的关键构建逻辑，并说出集成构建（integrated）与独立构建（standalone）的区别。
- 写出一组最小可用的 CMake 配置命令，把 Clang 构建出来。
- 解释 `-DLLVM_ENABLE_ASSERTIONS=ON` 在开发调试中为什么重要。
- 知道构建产物（可执行文件、库、头文件）分别落在构建目录的哪个位置。

本讲是「运行起来」的基础；只有先把 Clang 跑起来，后面几讲讲到的 driver、cc1、Lexer、AST 才有东西可以实际操作。

## 2. 前置知识

在进入源码之前，先用大白话澄清几个本讲会用到的概念。

- **构建系统（Build System）**：把一堆 `.cpp/.h` 源文件编译、链接成可执行文件或库的自动化工具。Clang 用的是 **CMake**——它读取 `CMakeLists.txt`，生成真正的构建脚本（最常用的是 **Ninja** 或 Makefile），再由后者执行实际的 `g++/clang++` 调用。
- **生成器（Generator）**：CMake 本身不直接编译代码，它「生成」构建脚本。`-G Ninja` 表示生成 Ninja 脚本（Clang/LLVM 官方推荐的、最快的生成器）。
- **配置（configure） vs 构建（build）**：配置阶段（`cmake -S … -B …`）读取 `CMakeLists.txt`、探测系统环境、生成 `build.ninja`；构建阶段（`cmake --build …` 或 `ninja`）才真正一行行编译。改了源码只需重新「构建」，改了选项或加了新文件才需要重新「配置」。
- **断言（assertion）**：C++ 里的 `assert(条件)` 宏——条件为假时立刻中止程序并打印位置。LLVM/Clang 用大量断言来检查「内部假设是否成立」。
- **TableGen / llvm-tblgen**：上一讲提到的 TableGen 工具链。Clang 的很多代码（诊断文本、驱动选项等）是用 `.td` 文件描述、再由 `llvm-tblgen` 和自带的 `clang-tblgen` 翻译成 C++ 代码的。所以构建 Clang 离不开 `llvm-tblgen` 这个**宿主工具**。

如果你对 CMake 的 `-D` 选项、cache 变量还不熟，可以把它们简单理解为「带默认值的命令行参数」——`-DXXX=YYY` 就是把变量 `XXX` 设成 `YYY`。

## 3. 本讲源码地图

本讲围绕「构建」这件事，涉及的文件都和 CMake 配置有关：

| 文件 | 作用 |
| --- | --- |
| `CMakeLists.txt` | Clang 顶层的构建脚本，是本讲的主角。它决定 Clang 以独立还是集成方式构建，并集中定义了大量 `CLANG_*` 选项。 |
| `INSTALL.txt` | Clang 自带的安装说明，用最朴素的语言讲清了「Clang 要放在 LLVM 里面一起编」。 |
| `cmake/caches/` | 一组「CMake 缓存脚本」，把常用配置预设好，相当于官方提供的配置模板。 |
| `cmake/caches/README.txt` | 说明这些缓存脚本怎么用。 |
| `lib/CMakeLists.txt` | 列出 `lib/` 下要构建的全部子库（对应上一讲提到的 Lex/Parse/Sema/AST 等）。 |
| `tools/CMakeLists.txt` | 列出 `tools/` 下要构建的全部可执行工具（`clang`、`clang-format` 等）。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**CMake 选项**、**独立 vs 集成构建**、**构建产物**。

### 4.1 CMake 选项

#### 4.1.1 概念说明

Clang 的构建行为高度可定制。顶层 `CMakeLists.txt` 用 `option(...)` 或 `set(... CACHE ...)` 定义了一大批变量，覆盖「要不要带静态分析器」「默认链接器是什么」「编译产物装到哪」等问题。理解这些选项，是看懂「别人给的一长串 `cmake -D...` 命令在做什么」的前提。

这些选项大致分三类：

1. **能力开关**：决定某个子系统编不编，如 `CLANG_ENABLE_STATIC_ANALYZER`、`CLANG_BUILD_TOOLS`、`CLANG_INCLUDE_TESTS`。
2. **默认行为**：影响编译出来的 `clang` 在用户那里的默认行为，如 `CLANG_DEFAULT_LINKER`、`CLANG_DEFAULT_CXX_STDLIB`。
3. **构建/安装位置**：产物输出与安装路径，如 `CMAKE_INSTALL_PREFIX`、`CLANG_TOOLS_INSTALL_DIR`。

#### 4.1.2 核心流程

CMake 选项的处理流程可以概括为：

```text
命令行 -DXXX=YYY
   │
   ▼
写入 CMakeCache.txt（变量有了「记忆」，下次配置仍生效）
   │
   ▼
CMakeLists.txt 里的 option()/set(... CACHE ...) 读取它
   │
   ▼
据此决定 add_subdirectory 哪些目录、给编译器加哪些宏
   │
   ▼
生成 build.ninja / Makefile
```

关键点：`option(NAME "描述" 默认值)` 是「布尔开关」的惯用写法；`set(NAME 值 CACHE 类型 "描述")` 则可以带默认值地定义任意类型变量。命令行 `-D` 给定的值会**覆盖**这些默认值。

#### 4.1.3 源码精读

**能力开关示例**：是否把静态分析器编进 `clang`。默认开启。

[CLANG_ENABLE_STATIC_ANALYZER 的定义](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L498-L499) 把它声明为默认 `ON` 的开关；关掉后，下面会看到的 `tools/CMakeLists.txt` 里 `add_clang_subdirectory(clang-check)` 等静态分析相关工具就不会被加入构建。

**工具构建开关**：`CLANG_BUILD_TOOLS` 控制 Clang 的可执行工具是否真正生成构建目标。

[CLANG_BUILD_TOOLS 的定义](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L477-L478)：当它为 `OFF` 时，只生成库目标、不生成最终可执行文件——这在只想复用 Clang 的库（比如做工具开发）时有用。

**插件支持开关**：`CLANG_PLUGIN_SUPPORT` 决定编译出来的 `clang` 能否动态加载插件（`-fplugin`）。

[CLANG_PLUGIN_SUPPORT 的条件定义](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L480-L487)：它依赖于 LLVM 是否启用了插件导出符号（`LLVM_ENABLE_PLUGINS`）。这会在 u11-l2「编写 Clang 插件」一讲再次出现。

**默认行为示例**：`clang` 默认用哪个链接器、哪个 C++ 标准库。

[CLANG_DEFAULT_LINKER / CLANG_DEFAULT_CXX_STDLIB 的定义](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L291-L295)：这两个变量分别控制「不带 `-fuse-ld=` 时默认的链接器」和「默认链接 libstdc++ 还是 libc++」。注意它们改的是**编译产物的内置默认值**，不是构建 Clang 本身用的链接器。

**cc1 进程模型开关**：`CLANG_SPAWN_CC1` 决定 `clang` 是在当前进程内直接调用前端（cc1），还是 fork 一个新进程来跑。

[CLANG_SPAWN_CC1 的定义](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L286-L287)：默认 `OFF`（同进程）。这与上一讲提到的 driver / cc1 分工直接相关，后续 u2 单元会深入。

> 提示：CMake 最低版本也有约束。[CMakeLists.txt:1](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L1) 要求 `cmake_minimum_required(VERSION 3.20.0)`，并且 [CMakeLists.txt:2-8](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L2-L8) 提醒：从 LLVM 24 起最低版本会升到 3.31.0。配置时报这类版本错误，先检查 CMake 版本。

#### 4.1.4 代码实践

**实践目标**：通过对比，直观感受一个 CMake 选项对「会构建哪些东西」的影响。

**操作步骤**：

1. 先用「默认选项」配置一次（命令见 4.2 节，这里只关注对比），构建后到 `build/bin/` 下确认 `clang-check` 是否生成。
2. 重新配置，加上 `-DCLANG_ENABLE_STATIC_ANALYZER=OFF`：

   ```bash
   cmake -S llvm -B build-nosa -G Ninja \
     -DCMAKE_BUILD_TYPE=Release \
     -DLLVM_ENABLE_PROJECTS=clang \
     -DCLANG_ENABLE_STATIC_ANALYZER=OFF
   cmake --build build-nosa --target clang-check
   ```

**需要观察的现象**：第二次构建 `clang-check` 目标时，CMake/Ninja 应当报告找不到该目标，因为它所属的 `tools/clang-check` 子目录在 [tools/CMakeLists.txt:40-46](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/CMakeLists.txt#L40-L46) 里被 `if(CLANG_ENABLE_STATIC_ANALYZER)` 包住了，关掉后就不会 `add_clang_subdirectory`。

**预期结果**：开启时能构建出 `clang-check`；关闭时报「unknown target」之类的错。**待本地验证**（受机器环境与已装依赖影响）。

#### 4.1.5 小练习与答案

**练习 1**：`option()` 和 `set(... CACHE BOOL ...)` 在「定义一个布尔开关」时效果几乎一样，但官方更常用 `option()`。请说出二者一个明显区别。

> **答案**：`option(NAME "doc" 默认值)` 写法更简洁、语义更明确（一眼看出是布尔开关），且文档字符串紧跟其后；`set(... CACHE ...)` 则适用于非布尔类型（PATH/STRING/FILEPATH）或需要在别处 `FORCE` 覆盖的场景。

**练习 2**：想把编译出的 `clang` 默认链接器设为 `lld`，应该用哪个选项？它和 `CMAKE_LINKER` 有什么不同？

> **答案**：用 [CLANG_DEFAULT_LINKER](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L291-L292)（设成 `"lld"`）。区别：`CLANG_DEFAULT_LINKER` 改的是**编译产物 `clang` 给用户代码链接时**的默认链接器；`CMAKE_LINKER` 改的是**构建 Clang 自己时**用的链接器，二者作用对象不同。

### 4.2 独立 vs 集成构建

#### 4.2.1 概念说明

Clang 既可以「跟 LLVM 一起编」，也可以「单独编」。这是初学者最容易混淆的一点，本模块专门讲清楚。

- **集成构建（integrated build）**：把 Clang 当作 LLVM 的一个子项目，在 LLVM 顶层一次性配置、一起构建。这是**官方推荐、最常用**的方式，对应 LLVM 单仓库（monorepo）的工作流。
- **独立构建（standalone build）**：Clang 自己当顶层项目来配置（`cmake -S clang`），把已构建好的 LLVM 当作**外部依赖**来链接。

为什么要支持两种方式？集成构建省事、依赖关系自动处理；独立构建适合「只想改 Clang、复用别人编译好的 LLVM」的场景，或者当你把 Clang 源码单独 checkout 出来时。

无论哪种方式，Clang 都**必须**依赖 LLVM 的库和一个叫 `llvm-tblgen` 的宿主工具——这印证了上一讲说的「构建期单向依赖 LLVM」。

#### 4.2.2 核心流程

CMake 用一个简单的判断来区分这两种模式：

```text
cmake 第一次处理 Clang/CMakeLists.txt
   │
   ├── CMAKE_SOURCE_DIR == CMAKE_CURRENT_SOURCE_DIR ?
   │        （顶层源码目录 == 本 CMakeLists 所在目录？）
   │
   ├── 是 → 我是顶层项目 → 独立构建
   │        set(CLANG_BUILT_STANDALONE TRUE)
   │        进入「自己找 LLVM、找 llvm-tblgen」的分支
   │
   └── 否 → 我是 LLVM 的子目录 → 集成构建
            LLVM 的变量直接可见，跳过那段独立逻辑
```

判定关键：你 `cmake -S` 指向的是 `llvm`（集成）还是 `clang`（独立）。源码里通过比较「整个构建的根源目录」与「本文件所在目录」是否相等来区分。

#### 4.2.3 源码精读

**模式判定**：这段是本模块的核心。

[独立构建的判定与标记](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L18-L23)：当 `CMAKE_SOURCE_DIR`（构建根）等于 `CMAKE_CURRENT_SOURCE_DIR`（Clang 目录）时，说明 Clang 就是这次构建的顶层，于是 `set(CLANG_BUILT_STANDALONE TRUE)`。整个独立构建的「找依赖」逻辑都包在 [CMakeLists.txt:36-174](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L36-L174) 这个 `if(CLANG_BUILT_STANDALONE) ... endif()` 大块里——集成构建会直接跳过它。

**C++17 标准**：独立构建时显式指定 C++ 标准。

[独立构建设置 C++17](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L37-L39)：`set(CMAKE_CXX_STANDARD 17 ...)` 且 `CMAKE_CXX_STANDARD_REQUIRED YES`。这与上一讲「Clang 用 C++17 编写」一致。集成构建时这一项由 LLVM 顶层统一设置，不需要 Clang 自己操心。

**找 LLVM 与 llvm-tblgen**：这是独立构建最关键的依赖处理。

[find_package(LLVM) 与 find_program(llvm-tblgen)](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L48-L59)：`find_package(LLVM REQUIRED HINTS "${LLVM_CMAKE_DIR}")` 去找一个**已构建好**的 LLVM；紧接着 `find_program(LLVM_TABLEGEN_EXE "llvm-tblgen" ...)` 在 LLVM 的 bin 目录里找 `llvm-tblgen` 可执行文件。两个都不可或缺——前者提供库，后者把 `.td` 翻译成 C++。注意 [CMakeLists.txt:54](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L54) 还把 `LLVM_MAIN_SRC_DIR` 指向了 `../llvm`，说明独立构建也假定 LLVM 源码就在隔壁目录。

**禁止源码内构建**：一个常见的「新手坑」防护。

[禁止 in-source build](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L373-L379)：如果你直接在 Clang 源码目录里跑 `cmake .`，会得到 `FATAL_ERROR`。这是有意的——源码内构建会把生成文件和源码混在一起，极难清理。**永远在单独的 build 目录里配置**。

**集成构建时的依赖方式**：集成构建不需要上面那些 `find_package`，因为 LLVM 顶层已经把所有变量、`llvm-tblgen` 目标都准备好了，Clang 的 `CMakeLists.txt` 直接就能用。这正是集成构建「省事」的根源。

**官方缓存脚本**：`cmake/caches/` 提供了一批预设配置，等于官方替你写好了一组 `-D` 选项。

[如何使用缓存脚本](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/cmake/caches/README.txt#L7-L14)：用 `cmake -C <缓存文件>` 加载，例如 `cmake -G Ninja -C clang/cmake/caches/Apple-stage1.cmake <llvm 源码>`。命令行上再写的 `-D` 会覆盖缓存里的值。以 [Apple-stage1.cmake](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/cmake/caches/Apple-stage1.cmake#L10-L23) 为例，它预设了 `LLVM_TARGETS_TO_BUILD=X86`、`CLANG_VENDOR=Apple`、`CLANG_SPAWN_CC1=ON` 等，模拟 Apple 版 Clang 的构建。另一个例子 [DistributionExample.cmake](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/cmake/caches/DistributionExample.cmake#L4-L11) 则展示了集成构建常用的「打开哪些子项目、哪些 runtime」：`LLVM_ENABLE_PROJECTS="clang;clang-tools-extra;lld"`、`LLVM_TARGETS_TO_BUILD=Native`、`CMAKE_BUILD_TYPE=Release`。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：写出一组最小可用的 CMake 配置命令构建 Clang，并解释 `-DLLVM_ENABLE_ASSERTIONS=ON` 对开发调试的意义。

**操作步骤**（集成构建，官方推荐路线）：

```bash
# 1. 拉取 LLVM 单仓库（Clang 在 llvm-project/clang 下）
git clone https://github.com/llvm/llvm-project.git
cd llvm-project

# 2. 配置：把 Clang 加入要构建的子项目，开启断言
cmake -S llvm -B build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_PROJECTS=clang \
  -DLLVM_ENABLE_ASSERTIONS=ON

# 3. 构建（只先构建 clang 主程序，省时间）
cmake --build build --target clang

# 4. 验证
./build/bin/clang --version
```

> 说明：`-S llvm` 指向 LLVM 顶层，所以这是**集成构建**——`clang` 是通过 `LLVM_ENABLE_PROJECTS=clang` 被 LLVM 顶层「发现」并纳入构建的，不会走 4.2.3 里那段独立构建分支。如果你想让 Clang 走独立构建，则应 `cmake -S llvm-project/clang -B build`，并先确保已有可用的 LLVM 构建供 `find_package(LLVM)` 找到。

**需要观察的现象**：构建完成后，`./build/bin/clang --version` 应打印出 Clang 版本号（该版本号来自 [CMakeLists.txt:382-396](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L382-L396)，缺省沿用 `LLVM_VERSION_*`）。

**为什么 `-DLLVM_ENABLE_ASSERTIONS=ON` 对调试很重要**：

1. LLVM/Clang 源码里散布着大量 `assert(条件)`，用来检查内部不变量（指针非空、枚举值合法、类型符合预期等）。这些是「自检」逻辑，不是给最终用户看的。
2. 默认的 `Release` 构建会定义 `NDEBUG` 宏，从而**禁用** `assert()`。于是 release 版 Clang 在遇到内部 bug 时，会「静默地」生成错误代码，或在一个莫名其妙的地方崩溃，难定位。
3. 加上 `-DLLVM_ENABLE_ASSERTIONS=ON` 后，即使构建类型是 `Release`，断言也保持开启。一旦某个内部假设被违反，Clang 会**立刻中止**并打印 `Assertion failed: ...` 外加调用栈，直接指向出问题的源码行——这正是改 Clang 源码时最想要的反馈。
4. 代价：断言会让运行变慢，所以发行版通常关掉断言换性能；但**开发/学习阶段强烈建议开**。
5. 一致性约束：独立构建时 [CMakeLists.txt:42-45](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L42-L45) 会从 llvm-config 读取断言设置并 `mark_as_advanced`，注释里写明「Assertions should follow llvm-config's」——即 Clang 的断言开关要和它链接的那个 LLVM 保持一致，不能随意单独改。

**预期结果**：能跑出 `clang --version`；并且在人为制造一个违反断言的场景时（比如改坏某段代码），会看到清晰的断言失败信息而非神秘崩溃。**待本地验证**（完整构建耗时较长，且依赖机器内存与 Ninja 并发）。

#### 4.2.5 小练习与答案

**练习 1**：判断对错：「集成构建时，Clang 也需要自己调用 `find_package(LLVM)` 去找 LLVM。」请结合源码说明理由。

> **答案**：错。`find_package(LLVM)` 只在 [CMakeLists.txt:48](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L48) 的 `if(CLANG_BUILT_STANDALONE)` 块里。集成构建时 `CLANG_BUILT_STANDALONE` 为假，整块被跳过——LLVM 的库、变量、`llvm-tblgen` 目标由顶层 CMake 直接提供。

**练习 2**：你执行 `cmake -S llvm -B build -DLLVM_ENABLE_PROJECTS=clang` 后报错说找不到 `llvm-tblgen`，最可能的原因是什么？

> **答案**：`llvm-tblgen` 是构建过程中由 LLVM 自己生成、再用作工具的「宿主工具」。若配置阶段就找不到，通常意味着这是独立构建（`-S` 指向了 `clang` 而非 `llvm`），或之前没有先构建出 LLVM 的宿主工具。集成构建（`-S llvm`）下，CMake/Ninja 会自动先编 `llvm-tblgen` 再编 Clang，不应出现此错误。

**练习 3**：为什么官方禁止「源码内构建」？

> **答案**：见 [CMakeLists.txt:373-379](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L373-L379)。源码内构建会在源码树里生成 `CMakeCache.txt`、`CMakeFiles/` 等，和源码混在一起，难以清理、容易污染版本控制。正确做法是另建 `build/` 目录，在它里面配置。

### 4.3 构建产物

#### 4.3.1 概念说明

构建完成后，磁盘上多出来一大堆文件。本模块帮你建立「哪个东西在哪儿、它对应什么」的地图，避免在茫茫产物里迷路。

产物主要分三类：

1. **可执行文件**：放在构建目录的 `bin/` 下，最核心的是 `clang`、`clang++`；还有 `clang-format`、`clang-check`、`c-index-test` 等工具。
2. **库**：放在 `lib/` 下，包括 Clang 自身的几十个子库（`libclangFrontend`、`libclangAST` 等），以及可选的动态库 `libclang-cpp.so` 和稳定 C 接口库 `libclang`。
3. **生成文件**：由 TableGen 或 `configure_file` 生成的头文件，如 `Version.inc`、`config.h`，落在构建目录的 `include/clang/...` 下。

#### 4.3.2 核心流程

产物的「出生地」由 CMake 的几个输出目录变量决定（独立构建时显式设定）：

```text
源码 ──构建──► <build>/bin/     ← 可执行文件（CMAKE_RUNTIME_OUTPUT_DIRECTORY）
            ├─ <build>/lib/     ← 库（CMAKE_LIBRARY_OUTPUT_DIRECTORY / ARCHIVE）
            └─ <build>/include/clang/... ← 生成的 .inc / config.h

执行 install 目标后，再按 CMAKE_INSTALL_PREFIX 拷贝到系统位置：
            <prefix>/bin、<prefix>/lib、<prefix>/include
```

集成构建时这些输出目录由 LLVM 顶层统一规定（同样是 `build/bin`、`build/lib`），Clang 的工具和库会落到同一组目录里。

#### 4.3.3 源码精读

**输出目录设定**（独立构建分支里）：

[设置 bin/lib 输出目录](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L108-L110)：`CMAKE_RUNTIME_OUTPUT_DIRECTORY = ${CMAKE_BINARY_DIR}/bin`，库输出到 `${CMAKE_BINARY_DIR}/lib`。这就是「可执行文件在 `build/bin`、库在 `build/lib`」的来源。

**生成版本头文件**：

[用 configure_file 生成 Version.inc](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L399-L401)：把模板 `Version.inc.in` 与上面算出的 `CLANG_VERSION` 结合，生成 `Version.inc`——`clang --version` 打印的版本号最终来自这里。

**生成 config.h**：

[生成 config.h](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L976-L978)：依据探测结果（如 [CMakeLists.txt:204-221](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L204-L221) 的 `CLANG_HAVE_RLIMITS`、`CLANG_HAVE_DLADDR`）填入 `config.h.cmake` 模板，反映宿主平台能力的宏会被编译进 Clang。

**子库清单**：`lib/CMakeLists.txt` 列出 Clang 全部子库，正好对应上一讲提到的流水线。

[lib/ 下的全部子库](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CMakeLists.txt#L1-L38)：可以清楚看到 `Lex`、`Parse`、`AST`、`Sema`、`CodeGen`、`Frontend`、`Driver`、`Serialization`、`Tooling`、`StaticAnalyzer`、`Format`、`Interpreter` 等几十个 `add_subdirectory`——每一个基本对应一个 `libclang*` 库。这正是上一讲「lib/ 下 30+ 子库」的实证。注意末尾 [lib/CMakeLists.txt:36-38](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CMakeLists.txt#L36-L38) 的 `CLANG_ENABLE_CIR` 控制是否编译 CIR（u14-l2 会讲）。

**工具清单**：`tools/CMakeLists.txt` 列出最终可执行的工具。

[tools/ 下的全部工具](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/CMakeLists.txt#L1-L24)：`driver`（产出 `clang`/`clang++`）、`clang-format`、`clang-check`、`clang-scan-deps`、`clang-repl` 等都在这里。其中 `clang-check`、`scan-build` 等还被 [tools/CMakeLists.txt:40-46](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/CMakeLists.txt#L40-L46) 的 `CLANG_ENABLE_STATIC_ANALYZER` 守卫。`libclang` 由 [tools/CMakeLists.txt:56](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/CMakeLists.txt#L56) 的 `add_clang_subdirectory(libclang)` 提供，对应上一讲提到的稳定 C 接口。

**安装目标**：构建只是「生成」，`install` 才把产物拷到系统位置。

[头文件的 install 规则](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L436-L453) 把 `include/clang`、`include/clang-c` 下的 `.h/.def` 以及生成的 `.inc/.h` 安装到 `${CMAKE_INSTALL_INCLUDEDIR}`。此外 [clang-libraries 目标](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L618) 汇总了所有库，方便一次性安装。`INSTALL.txt` 里的说明虽然老（仍写 `make install`），但流程一致：[INSTALL.txt:42-48](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/INSTALL.txt#L42-L48) 指出安装后 `clang`/`clang++` 出现在选定的 prefix 下。

**动态库开关**：`CLANG_LINK_CLANG_DYLIB` 决定工具链接成单个大动态库 `libclang-cpp` 还是众多静态库。

[CLANG_LINK_CLANG_DYLIB 的定义](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L355-L361)：它跟随 `LLVM_LINK_LLVM_DYLIB`，二者必须一致。开启后产物里会出现 `libclang-cpp.so`。

#### 4.3.4 代码实践

**实践目标**：亲手核对构建产物落点与「子库/工具」清单的对应关系。

**操作步骤**：

1. 完成 4.2.4 的构建后，列出 `build/bin/` 下的可执行文件：

   ```bash
   ls build/bin/ | grep -E '^clang' | head
   ```

2. 列出 `build/lib/` 下的 Clang 库：

   ```bash
   ls build/lib/ | grep -i clang | head
   ```

3. 查看生成的版本头文件内容（确认它由 `configure_file` 产生）：

   ```bash
   cat build/tools/clang/include/clang/Basic/Version.inc 2>/dev/null \
     || find build -name Version.inc -path '*clang*'
   ```

**需要观察的现象**：

- `build/bin/` 下应能看到 `clang`、`clang++`（来自 `tools/driver`），可能还有 `clang-format`、`clang-check` 等。
- `build/lib/` 下应能看到形如 `libclangFrontend.a`、`libclangAST.a`、`libclangSema.a` 等若干 `libclang*.a`（或 `.so`），正好对应 [lib/CMakeLists.txt:1-38](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CMakeLists.txt#L1-L38) 的子目录名。
- `Version.inc` 里应有 `CLANG_VERSION "x.y.z..."` 字样。

**预期结果**：产物清单与上面源码列出的「子库/工具」一一对应。**待本地验证**（实际生成的工具列表取决于你启用了哪些 `CLANG_*` 选项，如关掉静态分析器则不会有 `clang-check`）。

#### 4.3.5 小练习与答案

**练习 1**：构建产物里的可执行文件默认放在哪个目录？这个位置由哪一行 CMake 决定？

> **答案**：放在 `<build>/bin/`。独立构建时由 [CMakeLists.txt:108](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L108) 的 `set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)` 决定；集成构建时由 LLVM 顶层统一规定（同样是 `build/bin`）。

**练习 2**：你想让工具链链接成单个 `libclang-cpp.so` 而不是一堆静态库，需要打开哪两个互相约束的选项？

> **答案**：`LLVM_LINK_LLVM_DYLIB=ON` 与 `CLANG_LINK_CLANG_DYLIB=ON`。见 [CMakeLists.txt:355-361](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L355-L361)，二者必须同时为 ON，否则 CMake 会 `FATAL_ERROR`。

**练习 3**：`Version.inc` 是手写的还是构建时生成的？它的版本号从哪来？

> **答案**：构建时由 `configure_file` 从 `Version.inc.in` 模板生成（[CMakeLists.txt:399-401](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L399-L401)）。版本号取自 `CLANG_VERSION`，而 [CMakeLists.txt:382-394](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L382-L394) 在未显式指定时让它沿用 `LLVM_VERSION_*`。

## 5. 综合实践

把三个模块串起来，完成一次「带自定义选项的构建 + 产物自查」。

**任务**：用「集成构建」方式构建 Clang，并刻意改变两个默认选项，然后验证产物符合预期。

1. 配置（关掉静态分析器、打开动态库链接，并保留断言）：

   ```bash
   cmake -S llvm -B build -G Ninja \
     -DCMAKE_BUILD_TYPE=Release \
     -DLLVM_ENABLE_PROJECTS=clang \
     -DLLVM_ENABLE_ASSERTIONS=ON \
     -DCLANG_ENABLE_STATIC_ANALYZER=OFF \
     -DLLVM_LINK_LLVM_DYLIB=ON \
     -DCLANG_LINK_CLANG_DYLIB=ON
   ```

2. 构建 `clang`：

   ```bash
   cmake --build build --target clang
   ```

3. 自查清单（逐条解释你观察到的现象，并与源码对应）：
   - `./build/bin/clang --version` 是否正常？版本号从哪个生成文件来？
   - `build/bin/` 下**有没有** `clang-check`？为什么？（联系 [tools/CMakeLists.txt:40-46](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/CMakeLists.txt#L40-L46)）
   - `build/lib/` 下**有没有** `libclang-cpp.so`？为什么？（联系 [CMakeLists.txt:355-361](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/CMakeLists.txt#L355-L361)）
   - 把 `-DLLVM_ENABLE_ASSERTIONS=ON` 换成 `OFF` 重新构建并对比，说明断言开关对开发反馈的影响。

**预期结果**：`clang-check` 因静态分析器被关闭而**不生成**；`libclang-cpp.so` 因动态库选项打开而**生成**；版本号来自 `Version.inc`。断言开启时，对内部错误的反馈更直接。**完整构建耗时较长，结果待本地验证。**

## 6. 本讲小结

- Clang 的构建行为由顶层 `CMakeLists.txt` 里一大批 `CLANG_*` 选项控制，分「能力开关 / 默认行为 / 构建位置」三类。
- 构建 Clang 有两种方式：**集成构建**（作为 LLVM 子项目一起编，官方推荐）和**独立构建**（Clang 当顶层、把已构建的 LLVM 当外部依赖）；二者由 `CMAKE_SOURCE_DIR` 是否等于 Clang 目录来区分。
- 独立构建必须自己 `find_package(LLVM)` 并找到 `llvm-tblgen`；集成构建则由 LLVM 顶层直接提供这些，省去麻烦。
- `-DLLVM_ENABLE_ASSERTIONS=ON` 让内部断言在 Release 构建中也保持开启，违反不变量时立刻报错并给出调用栈，是开发调试的利器；且 Clang 的断言设置要与所链接的 LLVM 一致。
- 构建产物落在 `build/bin`（可执行文件）、`build/lib`（库）、`build/.../include`（生成的头文件）；子库与工具分别由 `lib/CMakeLists.txt`、`tools/CMakeLists.txt` 列出。
- 永远不要在源码目录内构建——CMake 会直接报错拒绝；请始终另建 `build/` 目录。

## 7. 下一步学习建议

到这里，你已经能从源码把 `clang` 跑起来。接下来的 u1-l3「源码目录与代码组织全景」会带你系统梳理 `lib/`、`include/`、`tools/` 的职责划分——正好和本讲看到的「子库清单 / 工具清单」对上。之后再进入 u1-l4，用刚编译出来的 `clang` 实际编译一个程序，并第一次观察 driver 与 cc1 的分工。

建议你顺手做两件事，为后续做准备：

- 读一遍 [lib/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CMakeLists.txt#L1-L38) 和 [tools/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/tools/CMakeLists.txt#L1-L58)，把子目录名和「它们大概管什么」在脑子里建个索引。
- 把本讲的构建命令存成脚本（带 `-DLLVM_ENABLE_ASSERTIONS=ON`），后续每讲做实践都会用到这个自建的 `clang`。
