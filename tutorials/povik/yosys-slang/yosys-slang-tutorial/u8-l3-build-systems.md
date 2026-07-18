# 构建系统深入：CMake 与 Bazel

## 1. 本讲目标

学完本讲，你应该能够：

- 读懂 sv-elab 顶层 `CMakeLists.txt` 的整体骨架，说清项目名、C++ 标准、选项与第三方依赖是如何装配起来的。
- 解释 `BUILD_AS_PLUGIN` 这个开关如何决定产物是 **`slang.so`（Yosys 插件，共享库）** 还是 **静态库（`.a`）**，并能列出两者在 target 属性上的关键差异。
- 理解 `cmake/FindYosys.cmake` 如何把外部的 `yosys-config` 工具包装成一个 `yosys::yosys` 的 CMake「接口导入目标（INTERFACE IMPORTED）」。
- 理解 `cmake/GitRevision.cmake` + `src/version.h.in` 如何把 git 提交哈希注入到编译出的 C++ 代码里，供 `slang_version` 命令打印。
- 看懂 Bazel 构建线（`MODULE.bazel`、根 `BUILD.bazel`、`src/yosys_plugin/BUILD.bazel`、`dependency_support/*.bzl`）如何用「另一套思路」产出同样的 `slang.so`。
- 能够独立完成一次本地 CMake 构建（插件与静态库两种），并解释为何 `slang` 与 `fmt` 必须以 **PIC（位置无关代码）** 方式编译。

## 2. 前置知识

在进入源码前，先用通俗语言约定几个术语：

- **构建系统（build system）**：把「源码树」变成「可加载/可链接产物」的自动化工具。sv-elab 同时维护了两套：CMake（主线，绝大多数开发者与 CI 使用）和 Bazel（Google 出品，强调可复现与封闭构建 hermetic build）。
- **Yosys 插件**：一个带 `(* blackbox *)` 风格入口的共享库（`.so`）。用 `yosys -m slang` 或 `plugin -i slang` 加载后，Yosys 会在运行时把它当作自身的一部分，注册其中的 Pass/Frontend（见 u2-l1）。
- **目标（target）**：CMake 里对一个「要构建的东西」的抽象，例如一个库或可执行文件。`add_library` 创建一个 target，再用 `set_target_properties` 给它贴属性（输出名、可见性、PIC 等）。
- **INTERFACE IMPORTED 目标**：CMake 里一种「自己不产出文件、只携带编译/链接参数」的目标。`yosys::yosys` 就是这样——它不真正编译 Yosys，只是把 `yosys-config --cxxflags` 给出的头文件路径与宏打包给依赖它的 target 使用。
- **PIC（Position Independent Code）**：位置无关代码。共享库（`.so`）在被加载到「运行时才确定」的地址时，要求其内部机器码不写死绝对地址，因此组成共享库的每个静态库都必须以 PIC 编译。这正是本讲反复出现的核心约束。
- **FetchContent**：CMake 内置模块，用于「把另一个 CMake 子项目拉进当前构建」。sv-elab 用它把 `third_party/fmt` 编进主构建。
- **module extension / `use_extension`**：Bazel bzl 模块扩展机制，用于在 `MODULE.bazel` 里声明「需要从某个 git commit 或 URL 拉取的第三方仓库」。

如果对 Yosys 插件机制或 `read_slang` 命令还不熟悉，请先回顾 u2-l1。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `CMakeLists.txt` | 顶层 CMake 入口：项目定义、C++ 标准、`BUILD_AS_PLUGIN` 选项、`fmt`/`slang` 子模块装配、PIC 设置、`src` 与 `tests` 子目录挂载。 |
| `src/CMakeLists.txt` | 定义真正的 `yosys-slang` target：源文件清单、链接 `yosys::yosys` 与 `slang::slang`，以及插件/静态库两条 target 属性分支。 |
| `cmake/FindYosys.cmake` | `find_package(Yosys)` 的实现：调用 `yosys-config` 取 `bindir`/`datdir`/`cxxflags`，合成 `yosys::yosys` INTERFACE IMPORTED 目标。 |
| `cmake/GitRevision.cmake` | `git_rev_parse()` 函数：执行 `git rev-parse HEAD`，失败时回退为 `UNKNOWN`。 |
| `src/version.h.in` | `version.h` 的模板：含 `YOSYS_SLANG_REVISION` 与 `SLANG_REVISION` 两个宏占位符。 |
| `BUILD.bazel` | Bazel 根构建文件：生成 `version.h`，定义 `yosys_slang_plugin` 这个 `cc_library`。 |
| `MODULE.bazel` | Bazel 模块声明：模块名/版本、各 `bazel_dep`、以及两个 `use_extension`（slang、tomlplusplus）。 |
| `dependency_support/slang_ext.bzl` | Bazel 扩展：用 `git_repository` 把 slang 从固定 commit 拉下来。 |
| `src/yosys_plugin/BUILD.bazel` | 把 `yosys_slang_plugin` 包成名为 `slang.so` 的 `cc_shared_library`。 |

## 4. 核心概念与源码讲解

本讲按「CMake 主线（4.1–4.4）→ Bazel 对照线（4.5）」组织，最小模块包括：CMake 插件构建、FindYosys/GitRevision、Bazel 依赖。

### 4.1 CMake 顶层骨架：项目、选项与依赖装配

#### 4.1.1 概念说明

顶层的 `CMakeLists.txt` 不直接产出 `slang.so`，它的职责是「搭舞台」：声明项目、设语言标准、定义命令行选项、把两个第三方依赖（`fmt` 与 `slang`）编进当前构建、再交给 `src/CMakeLists.txt` 去定义真正的 target。理解这一层的关键是把握一个贯穿全篇的约束——**`slang` 与 `fmt` 会被静态链接进一个共享库（插件）里，所以它们必须以 PIC 方式构建**。

#### 4.1.2 核心流程

顶层 CMake 的处理顺序大致是：

1. 拒绝 in-tree（源码目录内）构建，要求另起 `build` 目录。
2. 设 CMake 最低版本、C++20 标准、项目名 `sv-elab`。
3. 把 `cmake/` 目录加入模块搜索路径（让 `find_package(Yosys)` 能找到 `FindYosys.cmake`）。
4. 定义核心选项 `BUILD_AS_PLUGIN`（默认 `ON`）。
5. 处理平台特例（macOS 的链接器标志）、可选的 coverage/UBSan/ASan。
6. 用 `FetchContent` 或系统包装配 `fmt`。
7. `add_subdirectory(third_party/slang)` 把 slang 编进来，并强制 PIC。
8. `add_subdirectory(src)` 与 `add_subdirectory(tests)`。

#### 4.1.3 源码精读

开头两条是「防御性」的，第一条禁止在源码目录里直接构建，并给出正确命令提示：

[CMakeLists.txt:1-3](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/CMakeLists.txt#L1-L3) —— 拒绝 in-tree 构建，引导用户用 `cmake . -B build` 另起构建目录。

接着是语言标准与项目名。注意 C++20 是硬要求（sv-elab 大量使用 C++20 特性）：

[CMakeLists.txt:8-14](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/CMakeLists.txt#L8-L14) —— 设 C++20、关闭编译器扩展、声明项目名 `sv-elab`，并把 `cmake/` 加入模块搜索路径（第 14 行的 `CMAKE_MODULE_PATH` 是后面 `find_package(Yosys)` 能命中 `cmake/FindYosys.cmake` 的关键）。

本讲最核心的选项在这里——默认 `ON`，即默认产物是插件：

[CMakeLists.txt:16-17](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/CMakeLists.txt#L16-L17) —— `option(BUILD_AS_PLUGIN ... ON)`，`mark_as_advanced` 把它从图形界面的常用项里隐藏（它对绝大多数用户都该保持默认）。

`fmt` 的装配体现了「优先用系统的、否则用子模块」的渐进策略：先尝试 `find_package(fmt CONFIG)`，找到就用系统的；否则用 `FetchContent` 把 `third_party/fmt` 编进来：

[CMakeLists.txt:48-65](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/CMakeLists.txt#L48-L65) —— `USE_EXTERNAL_FMT` 控制 `find_package`；`fmt_FOUND` 为假时用 `FetchContent_Declare(... SOURCE_DIR third_party/fmt)` 把子模块拉入构建。

然后是 slang 子模块与 PIC 设置。注释把「为什么需要 PIC」解释得很清楚：

[CMakeLists.txt:67-75](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/CMakeLists.txt#L67-L75) —— 第 68 行 `add_subdirectory(third_party/slang)` 把 slang 编进来；第 70-71 行注释说明：插件是共享库，slang 与 fmt 被静态链接进去，若不以 PIC 构建会链接失败；第 72-75 行据此对 `fmt`（若存在）与 `slang_slang`（slang 的真实 target 名）设置 `POSITION_INDEPENDENT_CODE`，且值绑定为 `BUILD_AS_PLUGIN`——即只在构建插件时强制 PIC。

> 说明：`slang_slang` 是 slang 项目内部 `add_library` 的真实目标名，`slang::slang` 是它的带命名空间别名（alias）。sv-elab 在此处改 `slang_slang` 的属性、在链接处用 `slang::slang` 名字，两者指向同一组对象。

最后挂载 `src` 与 `tests`：

[CMakeLists.txt:77-80](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/CMakeLists.txt#L77-L80) —— `enable_testing()` 在 `add_subdirectory(tests)` 之前调用，使 `tests/CMakeLists.txt` 里的 `add_test` 能注册进 CTest。

#### 4.1.4 代码实践

**实践目标**：验证顶层 CMake 的几个关键事实。

**操作步骤**：

1. 在仓库根目录运行（注意必须另起 build 目录）：
   ```
   cmake -B build .
   ```
2. 观察配置阶段的 `message(STATUS ...)` 输出，找到「Using bundled third_party/fmt」与「Got YOSYS_SLANG_REVISION: ...」两行。
3. 在生成的 `build/CMakeCache.txt` 里搜索 `BUILD_AS_PLUGIN`，确认其值为 `ON`。

**预期现象**：配置成功，`BUILD_AS_PLUGIN` 默认为 `ON`，构建产物目录里最终会出现 `build/slang.so`。

**预期结果**：`build/slang.so` 存在；若你在源码目录内运行 `cmake .`（不另起目录），会立刻报 `In-tree builds are not supported` 并中止。

> 若本地无 Yosys 与完整子模块，配置阶段可能提前失败——属正常，重点观察上面列出的 `STATUS` 信息与 in-tree 报错即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `POSITION_INDEPENDENT_CODE` 的值要绑定成 `BUILD_AS_PLUGIN` 而不是恒为 `ON`？

**答案**：只有在构建共享库（插件）时才强制要求 PIC。若把 `BUILD_AS_PLUGIN` 关掉、只构建普通静态库，PIC 不是必需的；把值绑成开关可以让非插件构建沿用各依赖自身的默认（通常非 PIC），避免不必要的开销。绑成开关也让「系统已安装的非 PIC 版 fmt」在静态库模式下有机会被直接复用。

**练习 2**：如果 `CMAKE_MODULE_PATH`（第 14 行）被删掉，`find_package(Yosys)` 会发生什么？

**答案**：CMake 找不到 `cmake/FindYosys.cmake`，`find_package(Yosys)` 会回退到默认查找逻辑，最终因找不到 `YosysConfig.cmake` 或内置 `FindYosys` 而失败，导致后续 `src/CMakeLists.txt` 中的 `yosys::yosys` 目标无法定义、构建中止。

### 4.2 FindYosys：把 yosys-config 包装成 INTERFACE IMPORTED 目标

#### 4.2.1 概念说明

Yosys 是一个独立的外部可执行程序，自带一个查询脚本 `yosys-config`，能告诉你它的二进制目录、数据目录、编译所需的头文件路径与宏。sv-elab 不去 hardcode 这些路径，而是在 `cmake/FindYosys.cmake` 里调用 `yosys-config`，把结果封装成一个「干净的 CMake 目标」`yosys::yosys`。这样做的好处是：`src/CMakeLists.txt` 只需写 `target_link_libraries(... yosys::yosys)`，无需关心 Yosys 装在哪里。

#### 4.2.2 核心流程

1. 读 `YOSYS_CONFIG`（默认 `yosys-config`，可被缓存变量覆盖）。
2. 依次执行 `yosys-config --bindir`、`--datdir`、`--cxxflags`，捕获输出。
3. 对 `--cxxflags` 做字符串切分并过滤，只保留以 `-I`/`-D` 开头的项（头文件路径与宏）。
4. 创建 `IMPORTED` 的 `INTERFACE` 目标 `yosys::yosys`，把过滤后的编译选项挂上去。
5. 在 Windows（`WIN32`）下额外取 `--linkflags`/`--ldlibs` 并挂链接选项。
6. 把 `YOSYS_BINDIR`/`YOSYS_DATDIR` 通过 `PARENT_SCOPE` 上抛，供 `tests` 子目录在运行测试时定位 `yosys` 可执行文件。

#### 4.2.3 源码精读

入口与缓存变量：

[cmake/FindYosys.cmake:1-2](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/cmake/FindYosys.cmake#L1-L2) —— `YOSYS_CONFIG` 是一个 `CACHE STRING`，默认值 `yosys-config`，允许用户用 `-DYOSYS_CONFIG=/path/to/yosys-config` 指向特定版本的 Yosys。

典型的 `execute_process` 调用模式（取 bindir）：

[cmake/FindYosys.cmake:4-9](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/cmake/FindYosys.cmake#L4-L9) —— 运行 `yosys-config --bindir`，`COMMAND_ERROR_IS_FATAL ANY` 表示只要命令失败就立刻致命报错（Yosys 没装就别往下走）。

对 cxxflags 的过滤是关键一步——只留 `-I`/`-D`：

[cmake/FindYosys.cmake:20-28](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/cmake/FindYosys.cmake#L20-L28) —— 第 26 行把连续空格切成分号列表，第 27 行用 `list(FILTER ... INCLUDE REGEX "^-[ID]")` 只保留 `-I`（头文件路径）和 `-D`（宏），丢弃 `-std=`、`-O2` 等可能干扰 sv-elab 自身编译选项的标志。

合成 INTERFACE IMPORTED 目标：

[cmake/FindYosys.cmake:30-31](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/cmake/FindYosys.cmake#L30-L31) —— `add_library(yosys::yosys INTERFACE IMPORTED)` 创建一个不产出文件的接口目标，`target_compile_options` 把过滤后的头文件路径/宏作为「接口编译选项」挂上去；任何 `target_link_libraries(x yosys::yosys)` 的 target 都会自动继承这些选项。

Windows 额外处理与变量上抛：

[cmake/FindYosys.cmake:33-57](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/cmake/FindYosys.cmake#L33-L57) —— `WIN32` 下额外取链接选项与依赖库；第 56-57 行把 `YOSYS_BINDIR`/`YOSYS_DATDIR` 设为 `PARENT_SCOPE`，让 `tests/CMakeLists.txt` 能用 `${YOSYS_BINDIR}/yosys` 定位可执行文件、用 `${YOSYS_DATDIR}` 决定插件安装目录。

#### 4.2.4 代码实践

**实践目标**：亲手「扮演」一次 `FindYosys.cmake`，理解它从 `yosys-config` 取到了什么。

**操作步骤**：

1. 在装好 Yosys 的机器上直接运行：
   ```
   yosys-config --bindir
   yosys-config --datdir
   yosys-config --cxxflags
   ```
2. 对照 `cmake/FindYosys.cmake:20-28` 的过滤规则，手动把 `--cxxflags` 的输出里 `-I`/`-D` 之外的部分剔除，看看 `yosys::yosys` 最终继承到的编译选项是什么。

**预期现象**：`--bindir`/`--datdir` 分别给出 Yosys 可执行文件与数据目录的绝对路径；`--cxxflags` 里包含若干 `-I/.../share/yosys/include` 与 `-DYOSYS_*` 宏，以及一些可能干扰的 `-std=`/优化标志。

**预期结果**：过滤后只剩头文件路径与宏，正是 `yosys::yosys` 暴露给 sv-elab 的全部接口信息。

> 若本地无 Yosys，本步骤可跳过；理解过滤逻辑即可（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么第 27 行只保留 `-I`/`-D`，而不是把 `--cxxflags` 的全部输出都传给 `yosys::yosys`？

**答案**：`--cxxflags` 可能含 `-std=c++...`、`-O2`、`-Wall` 等 Yosys 自己构建时用的标志，直接透传会与 sv-elab 的 C++20 标准（顶层第 9 行硬定）和自身的 `-Wall -Wextra`（`src/CMakeLists.txt:40`）冲突。只保留头文件路径与宏，既能拿到 Yosys 的 API 头文件，又不污染 sv-elab 自己的编译选项。

### 4.3 GitRevision 与 version.h.in：把 git 哈希注入 C++

#### 4.3.1 概念说明

sv-elab 的 `slang_version` 命令会打印两条 git 修订号（见 u2-l1）。这两条字符串不是手写的常量，而是在配置阶段由 CMake 调用 `git rev-parse` 取到、再通过模板文件 `src/version.h.in` 注入到编译产物里的。理解这条链路，就理解了「构建期常量」是如何生成的。

#### 4.3.2 核心流程

1. `src/CMakeLists.txt` 调用 `find_package(Yosys)` 与 `include(GitRevision)`。
2. 两次调用 `git_rev_parse`：一次取 sv-elab 自身的 HEAD，一次取 `third_party/slang` 子模块的 HEAD。
3. `configure_file` 把 `src/version.h.in` 渲染成 `build/src/version.h`，把占位符 `@YOSYS_SLANG_REVISION@` / `@SLANG_REVISION@` 替换成真实哈希。
4. C++ 源码 `#include "version.h"`，使用其中的宏。

#### 4.3.3 源码精读

`git_rev_parse` 函数定义：

[cmake/GitRevision.cmake:1-24](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/cmake/GitRevision.cmake#L1-L24) —— 第 4 行构造 `git -C <dir> rev-parse HEAD`；第 6-11 行执行并捕获输出；第 13-17 行是失败兜底：`git` 不可用或目录不是 git 仓库时，发一条 `WARNING` 并把值设为 `UNKNOWN`，而不是让整个配置崩溃（这对从 tar 包解压的源码很重要）。

调用点与模板渲染：

[src/CMakeLists.txt:1-6](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/CMakeLists.txt#L1-L6) —— 第 4-5 行分别对仓库根与 `third_party/slang` 取 HEAD；第 6 行 `configure_file` 把模板渲染到 `${CMAKE_CURRENT_BINARY_DIR}/version.h`（即 `build/src/version.h`）。

模板本身只有两行宏定义：

[src/version.h.in:1-4](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/version.h.in#L1-L4) —— 两个 `@...@` 占位符会被 `configure_file` 替换成 `git_rev_parse` 取到的哈希字符串。

C++ 端的使用处（`slang_version` Pass 的 `execute`）：

[src/slang_frontend.cc:3469-3470](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3469-L3470) —— 直接 `log` 出两个宏 `YOSYS_SLANG_REVISION` 与 `SLANG_REVISION`，它们来自 `#include "version.h"`（见 [src/slang_frontend.cc:79](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L79)）。

#### 4.3.4 代码实践

**实践目标**：观察 `version.h` 是如何被生成的，以及它在运行时的体现。

**操作步骤**：

1. 完成 4.1.4 的 `cmake -B build .` 后，打开生成的 `build/src/version.h`，确认两个宏的值确实是哈希（或 `UNKNOWN`）。
2. 构建 `slang.so` 后，运行：
   ```
   yosys -m build/slang.so -p "slang_version"
   ```
3. 对照 `src/slang_frontend.cc:3469-3470` 的输出格式，确认打印的哈希与 `build/src/version.h` 中的宏一致。

**预期结果**：`slang_version` 输出形如 `sv-elab revision 3dddccd...` 与 `slang revision 8acc660...`（具体哈希取决于你检出的 commit）；若是从非 git 的 tar 包构建，则两者都为 `UNKNOWN`。

> 若本地无 Yosys，可只完成步骤 1（查看生成文件），运行验证待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果从 GitHub 下载的 release 源码 tar 包（不带 `.git` 目录）构建，`slang_version` 会打印什么？为什么不会构建失败？

**答案**：会打印 `UNKNOWN`。因为 `cmake/GitRevision.cmake:13-17` 在 `git rev-parse` 失败时只发 `WARNING` 并把值设为 `UNKNOWN`，而不是致命错误，所以配置与构建仍能完成，只是版本号丢失了精确性。

### 4.4 src/CMakeLists：BUILD_AS_PLUGIN 的两种产物

#### 4.4.1 概念说明

这是本讲最核心的一节。`src/CMakeLists.txt` 定义唯一的 target `yosys-slang`，但根据 `BUILD_AS_PLUGIN` 的取值，它的「形状」截然不同：

- **`BUILD_AS_PLUGIN=ON`（默认）**：`SHARED` 库，产物是 `slang.so`，输出到构建根目录，设 `PREFIX ""` + `SUFFIX ".so"` + `OUTPUT_NAME "slang"`，并把符号可见性设为 `hidden`（隐藏插件内部细节）。
- **`BUILD_AS_PLUGIN=OFF`**：`STATIC` 库，产物是 `libyosys-slang.a`，并把 `fmt`/`slang` 的对象文件归档进去，得到一个自包含的静态库。

两种形态共享同一份源文件清单与同一组链接依赖。

#### 4.4.2 核心流程

1. `find_package(Yosys)` + `include(GitRevision)` + 渲染 `version.h`。
2. 据 `BUILD_AS_PLUGIN` 选 `LIBRARY_TYPE`：`SHARED` 或 `STATIC`。
3. `add_library(yosys-slang ${LIBRARY_TYPE} <全部源文件>)`，含生成的 `version.h`。
4. 公共部分：include 目录、链接 `yosys::yosys` 与 `slang::slang`、加 `-Wall -Wextra` 等警告。
5. 分叉：
   - 插件分支：设输出目录/输出名/后缀/可见性，并 `install` 到 `${YOSYS_DATDIR}/plugins`。
   - 静态库分支：设 `ARCHIVE_OUTPUT_DIRECTORY`，并用 `STATIC_LIBRARY_OPTIONS` 把 fmt/slang 的对象文件归档进来。

#### 4.4.3 源码精读

选择库类型：

[src/CMakeLists.txt:8-12](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/CMakeLists.txt#L8-L12) —— `BUILD_AS_PLUGIN` 决定 `LIBRARY_TYPE` 是 `SHARED` 还是 `STATIC`，直接喂给下面的 `add_library`。

定义 target 与公共属性：

[src/CMakeLists.txt:14-46](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/CMakeLists.txt#L14-L46) —— 第 14-37 行列出全部源文件（含生成的 `version.h`）；第 38 行把构建目录加入 include 路径（让 `#include "version.h"` 找得到）；第 39 行链接 `yosys::yosys`（来自 FindYosys）与 `slang::slang`（来自子模块）；第 40-46 行开启严格警告，并把未使用变量/参数/函数升级为错误。

**插件分支**的 target 属性（重点）：

[src/CMakeLists.txt:48-68](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/CMakeLists.txt#L48-L68) —— 第 49-52 行把 `RUNTIME_OUTPUT_DIRECTORY`/`LIBRARY_OUTPUT_DIRECTORY` 都设为构建根目录（保证 `build/slang.so`）；第 53-56 行的三条属性合起来把产物名钉死成 `slang.so`：`PREFIX ""` 去掉默认的 `lib` 前缀、`OUTPUT_NAME "slang"` 设名字、`SUFFIX ".so"` 设后缀——注释解释 Yosys 在所有平台都按 `.so` 加载插件，所以 macOS 上也必须是 `.so`；第 58-59 行把符号可见性设为 `hidden`，隐藏插件内部符号；第 62-68 行按平台 `install` 到 `${YOSYS_DATDIR}/plugins`。

**静态库分支**的 target 属性（重点）：

[src/CMakeLists.txt:69-74](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/CMakeLists.txt#L69-L74) —— 第 71 行设 `ARCHIVE_OUTPUT_DIRECTORY` 到构建根目录；第 72 行用 `STATIC_LIBRARY_OPTIONS` 配合生成器表达式 `$<TARGET_OBJECTS:fmt::fmt>` 与 `$<TARGET_OBJECTS:slang::slang>`，把 fmt 与 slang 的对象文件一并归档进 `libyosys-slang.a`，使这个静态库自包含——下游只要链接它，就能拿到 slang 与 fmt 的全部实现，无需再单独链接两者。

> 对比记忆：插件分支靠 `target_link_libraries` 在链接期把 slang/fmt 拉进共享库；静态库分支因为没有「链接期合并」的概念，改用 `STATIC_LIBRARY_OPTIONS` 在归档期把对象文件直接打进 `.a`。两者的目的都是「让最终产物包含 slang 与 fmt 的实现」。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：分别用插件与静态库两种配置构建，观察 target 属性差异，并解释 PIC 的必要性。这正是本讲规格里要求的实践任务。

**操作步骤**：

1. **插件构建**（默认）：
   ```
   cmake -B build_plugin . -DBUILD_AS_PLUGIN=ON
   make -C build_plugin -j$(nproc)
   ls -la build_plugin/slang.so
   ```
2. **静态库构建**：
   ```
   cmake -B build_static . -DBUILD_AS_PLUGIN=OFF
   make -C build_static -j$(nproc)
   ls -la build_static/libyosys-slang.a
   ```
3. 在两个 `build` 目录里分别用 `make help` 或查看生成的 `build.ninja`/`Makefile`，确认 target 名都是 `yosys-slang`，但类型与输出文件不同。
4. 用 `nm build_static/libyosys-slang.a | grep -c ' T '` 大致观察静态库里包含了大量符号（含 slang/fmt 的实现），印证「自包含」。

**需要观察的现象与对照表**：

| 维度 | `BUILD_AS_PLUGIN=ON` | `BUILD_AS_PLUGIN=OFF` |
| --- | --- | --- |
| `LIBRARY_TYPE` | `SHARED`（[src/CMakeLists.txt:9](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/CMakeLists.txt#L9)） | `STATIC`（[src/CMakeLists.txt:11](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/CMakeLists.txt#L11)） |
| 产物文件 | `build/slang.so` | `build/libyosys-slang.a` |
| 输出名控制 | `PREFIX ""` + `OUTPUT_NAME "slang"` + `SUFFIX ".so"`（[L53-L56](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/CMakeLists.txt#L53-L56)） | 默认（`libyosys-slang.a`），靠 `ARCHIVE_OUTPUT_DIRECTORY` 定目录（[L71](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/CMakeLists.txt#L71)） |
| 符号可见性 | `CXX_VISIBILITY_PRESET hidden` + `VISIBILITY_INLINES_HIDDEN YES`（[L58-L59](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/CMakeLists.txt#L58-L59)） | 不设置（静态库不做符号可见性裁剪） |
| slang/fmt 如何进入产物 | 链接期由 `target_link_libraries` 合并进共享库（[L39](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/CMakeLists.txt#L39)） | 归档期由 `STATIC_LIBRARY_OPTIONS` 打进 `.a`（[L72](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/CMakeLists.txt#L72)） |
| 安装 | `install` 到 `${YOSYS_DATDIR}/plugins`（[L62-L68](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/CMakeLists.txt#L62-L68)） | 无 `install` 规则 |

**关于「slang 与 fmt 为何需要以 PIC 方式构建」的解释**（实践任务后半部分）：

- 当 `BUILD_AS_PLUGIN=ON` 时，`yosys-slang` 是一个**共享库**（`slang.so`）。共享库会被动态加载器映射到运行时才确定的地址，其机器码不能含写死的绝对地址，因此组成它的所有代码都必须以 PIC 编译。
- 而 slang 与 fmt 被**静态链接**进这个共享库（不是作为独立 `.so` 存在）。把一个非 PIC 的静态归档链接进共享库，链接器会报「recompile with `-fPIC`」之类的错误。
- 所以顶层 [CMakeLists.txt:72-75](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/CMakeLists.txt#L72-L75) 在 `BUILD_AS_PLUGIN=ON` 时强制把 `slang_slang` 与 `fmt` 的 `POSITION_INDEPENDENT_CODE` 设为 `ON`。
- 当 `BUILD_AS_PLUGIN=OFF`（静态库）时，产物是普通 `.a`，不涉及动态加载，PIC 非必需，于是该属性保持默认（通常关闭），避免无谓的 PIC 开销，也给「系统已安装的非 PIC 版 fmt」留出复用空间。

**预期结果**：两种配置都能成功构建，分别得到 `slang.so` 与 `libyosys-slang.a`；插件构建里能看到对 slang/fmt 启用了 PIC，静态库构建则不会强制 PIC。

> 若本地缺 Yosys 或子模块未初始化，构建会失败——此时重点理解上面的对照表与 PIC 解释即可（运行验证待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么插件分支要把 `PREFIX` 设为空字符串？

**答案**：CMake 的 `SHARED` 库默认产物名是 `lib<name>.so`（带 `lib` 前缀）。但 Yosys 用 `yosys -m slang` 加载插件时按名字 `slang` 去找 `slang.so`，不接受 `libslang.so`。所以必须 `PREFIX ""` 去掉 `lib`，配合 `OUTPUT_NAME "slang"` 与 `SUFFIX ".so"`，把产物名精确钉死成 `slang.so`（见 `src/CMakeLists.txt:53-56` 的注释）。

**练习 2**：静态库分支用 `STATIC_LIBRARY_OPTIONS` 把 fmt/slang 的对象文件打进 `.a`，这样做相对「让下游自己链接 fmt 与 slang」有什么好处？

**答案**：得到一个**自包含**的静态库：下游只要链接 `libyosys-slang.a` 一个文件，就能拿到 sv-elab + slang + fmt 的全部实现，不必再分别找到并链接 slang 与 fmt。这降低了把 sv-elab 作为静态库嵌入更大构建（例如把 sv-elab 直接编进另一个 C++ 程序）的集成成本。

**练习 3**：插件分支设了 `CXX_VISIBILITY_PRESET hidden`，但 `slang_version`/`read_slang` 这些命令为什么仍能被 Yosys 调用到？

**答案**：Yosys 插件机制不是靠「导出符号」被 Yosys 按名字查找的，而是靠 C++ 全局对象的**自我注册**（见 u2-l1：`SlangFrontend` 的全局静态实例在 `.so` 加载时构造，构造函数里把自己登记进 Yosys 的 Pass/Frontend 表）。只要这个全局对象的构造未被裁剪（它不属于普通函数符号），`hidden` 可见性就不影响插件工作；`hidden` 反而能避免插件内部符号污染 Yosys 的符号空间、减少符号冲突。

### 4.5 Bazel 构建线：MODULE、dependency_support 与 slang.so

#### 4.5.1 概念说明

除 CMake 外，sv-elab 还维护了一条完整的 Bazel 构建线（见 `.github/workflows/bazel.yaml`，CI 会跑 `bazel build //...`）。Bazel 与 CMake 的思路不同：CMake 靠 `yosys-config` 探测「本机已装的 Yosys」、靠 git 子模块拉 slang；Bazel 强调**封闭、可复现**——所有依赖都从 `MODULE.bazel` 声明的固定版本/哈希拉取，包括 Yosys 的头文件（通过 BCR 上的 `yosys` 模块的 `:hdrs`）和 slang（通过固定 commit 的 `git_repository`）。注意：**Bazel 线目前只构建插件（`slang.so`），不提供静态库选项**——这是它和 CMake 主线的一个关键差异。

#### 4.5.2 核心流程

Bazel 线的装配顺序：

1. `MODULE.bazel` 声明模块 `sv-elab`（版本 `2026-07-07`）、最低 Bazel 版本，以及各 `bazel_dep`（yosys、rules_cc、fmt、boost.regex 等）。
2. 用 `use_extension` 引入两个模块扩展：`slang_ext`（拉 slang）与 `tomlplusplus_ext`（拉 tomlplusplus），再用 `use_repo` 把它们具名为仓库 `vendored-slang` 与 `tomlplusplus`。
3. 根 `BUILD.bazel` 用 `expand_template` 从 `src/version.h.in` 生成 `version.h`（注意：Bazel 的版本号取自 `module_version()`，而非 git 哈希）。
4. 根 `BUILD.bazel` 定义 `yosys_slang_plugin` 这个 `cc_library`：`-fPIC`、C++20、依赖 `@vendored-slang//:slang` 与 `@yosys//:hdrs`、`alwayslink=True`。
5. `src/yosys_plugin/BUILD.bazel` 把它包成 `cc_shared_library`，产物名 `slang.so`。

#### 4.5.3 源码精读

模块声明与依赖：

[MODULE.bazel:3-16](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/MODULE.bazel#L3-L16) —— 模块名 `sv-elab`、版本 `2026-07-07`、要求 Bazel ≥ 7.2.1；第 10 行声明 `yosys 0.62.bcr.2`——注释说这是「首个为插件导出 `:hdrs` 的发布」，即 Bazel 侧从 BCR 拿 Yosys 的头文件，而非本机 `yosys-config`；`fmt`、`boost.regex` 也都从 BCR 拉固定版本。

两个模块扩展的引入：

[MODULE.bazel:17-20](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/MODULE.bazel#L17-L20) —— `use_extension` 加载扩展、`use_repo` 把扩展产出的仓库具名，使后续 `BUILD.bazel` 能用 `@vendored-slang` 与 `@tomlplusplus` 引用。

slang 的拉取逻辑（固定 commit，不用子模块）：

[dependency_support/slang_ext.bzl:8-23](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/dependency_support/slang_ext.bzl#L8-L23) —— 第 8-11 行硬编码 slang 的版本号与 commit 哈希（注释说「子模块更新时一并 bump」）；第 13-19 行用 `git_repository` 从 slang 官方仓库的该 commit 拉取，并用 `build_file` 指向 `dependency_support/slang.BUILD`。这与 CMake 的「`add_subdirectory(third_party/slang)`」形成鲜明对照：Bazel 不依赖本地检出的子模块，而是按哈希现拉，保证可复现。

根 `BUILD.bazel` 的版本头生成：

[BUILD.bazel:31-43](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/BUILD.bazel#L31-L43) —— 第 29 行 `SV_ELAB_REVISION = module_version()`，即 sv-elab 的「修订号」在 Bazel 线取自 `MODULE.bazel` 的 `version` 字段（由 BCR 提交者在发布时填，见第 27-28 行注释），而非 git 哈希；`expand_template` 据此渲染 `src/version.h.in`。这是 Bazel 线与 CMake 线生成 `version.h` 的一个细微但重要的差别。

核心 `cc_library`：

[BUILD.bazel:47-64](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/BUILD.bazel#L47-L64) —— `srcs` 用 `glob` 收 `src/*.cc` 与 `src/*.h`，再加生成的 `:version.h`；`copts` 显式给 `-fPIC` 与 `-std=c++20`（对应 CMake 线的 PIC 与 C++20）；`deps` 依赖 `@vendored-slang//:slang` 与 `@yosys//:hdrs`（对应 CMake 线的 `slang::slang` 与 `yosys::yosys`）；`alwayslink = True` 保证该库的对象文件即使在看似「无引用」时也被链入最终共享库——这对靠全局静态对象自我注册的插件机制至关重要（对应 4.4.5 练习 3 提到的注册原理）。

包成共享库：

[src/yosys_plugin/BUILD.bazel:1-8](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/yosys_plugin/BUILD.bazel#L1-L8) —— `cc_shared_library` 把 `//:yosys_slang_plugin` 打包成名为 `slang.so` 的共享库，产物名与 CMake 线的 `slang.so` 对齐。

> **CMake vs Bazel 速查**：① CMake 支持插件（`SHARED`）与静态库（`STATIC`）两种产物，Bazel 只构建插件；② CMake 靠 `yosys-config` 探测本机 Yosys，Bazel 靠 BCR 的 `@yosys//:hdrs`；③ CMake 靠 git 子模块取 slang，Bazel 靠固定 commit 的 `git_repository`；④ 两者都从 `src/version.h.in` 生成 `version.h`，但 sv-elab 修订号来源不同（CMake=git 哈希，Bazel=`module_version()`）。

#### 4.5.4 代码实践

**实践目标**：用 Bazel 复现插件构建，并与 CMake 产物对照。

**操作步骤**：

1. 安装 Bazel（或 bazelisk），初始化子模块（`git submodule update --init --recursive`）后运行：
   ```
   bazel build //src/yosys_plugin:slang.so
   ```
2. 在 `bazel-bin/src/yosys_plugin/` 下找到 `slang.so`，与 CMake 产出的 `build/slang.so` 比较：两者都应是可被 `yosys -m` 加载的共享库。
3. 对照 4.5.3 的速查表，分别在 CMake 与 Bazel 产物上运行 `yosys -m <path>/slang.so -p "slang_version"`，观察 sv-elab 修订号的差异（CMake 给 git 哈希，Bazel 给 `2026-07-07` 之类的模块版本）。

**预期结果**：两条线都能产出可加载的 `slang.so`；`slang_version` 的第一行（sv-elab revision）在两种构建下格式不同，印证了 4.5.3 的差异点。

> Bazel 构建需要联网拉取 slang/git 仓库，且首次较慢；若环境受限，理解速查表即可（运行验证待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 Bazel 线在 `cc_library` 上要设 `alwayslink = True`？

**答案**：sv-elab 的 Pass/Frontend（如 `SlangFrontend`、`SlangVersionPass`）靠**全局静态对象的构造**自我注册进 Yosys（见 u2-l1）。链接器在做共享库时，若发现某个对象文件里「没有看似被引用的符号」，可能不把它链入，从而丢掉那段注册代码。`alwayslink = True` 强制把该 `cc_library` 的对象文件无条件链入最终 `slang.so`，确保注册构造得以执行。

**练习 2**：Bazel 线不提供静态库选项，这从工程上意味着什么？

**答案**：意味着 Bazel 构建主要面向「产出可加载的 Yosys 插件」这一单一目标（服务于 BCR 发布与封闭可复现构建）。若你想把 sv-elab 作为静态库嵌入别的 C++ 项目，目前应使用 CMake 的 `BUILD_AS_PLUGIN=OFF` 路径，而非 Bazel。

## 5. 综合实践

把本讲的知识串起来，完成一次「双构建系统对照实验」：

1. **准备**：初始化子模块（`git submodule update --init --recursive`），确保本机有 Yosys（CMake 线需要）。
2. **CMake 插件构建**：
   ```
   cmake -B build . && make -C build -j$(nproc)
   ```
   记录产物路径 `build/slang.so`，运行 `yosys -m build/slang.so -p "slang_version"`，抄下两条 revision。
3. **CMake 静态库构建**（另起目录避免污染）：
   ```
   cmake -B build_static . -DBUILD_AS_PLUGIN=OFF && make -C build_static -j$(nproc)
   ```
   得到 `build_static/libyosys-slang.a`，用 `nm` 确认内含 slang/fmt 符号。
4. **回答三个问题**（用本讲源码佐证）：
   - 插件与静态库两种 target，分别由 `src/CMakeLists.txt` 的哪几行属性决定？（答：[L48-L68](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/CMakeLists.txt#L48-L68) 与 [L69-L74](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/CMakeLists.txt#L69-L74)。）
   - slang 与 fmt 为什么必须以 PIC 构建？（答：见 4.4.4 的 PIC 解释与 [CMakeLists.txt:70-75](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/CMakeLists.txt#L70-L75)。）
   - `version.h` 的两个宏在 CMake 与 Bazel 线下分别来自哪里？（答：CMake=git rev-parse；Bazel=`module_version()` + slang 扩展里的固定版本号。）
5. **（可选）Bazel 对照**：若装有 Bazel，运行 `bazel build //src/yosys_plugin:slang.so`，对照 4.5 的速查表，体会两条线的依赖管理差异。

> 若环境不允许完整构建，至少完成「阅读源码回答三个问题」部分——这是本讲的核心产出。

## 6. 本讲小结

- 顶层 `CMakeLists.txt` 负责「搭舞台」：C++20、`BUILD_AS_PLUGIN` 选项、`fmt`/`slang` 子模块装配，并在 `BUILD_AS_PLUGIN=ON` 时强制 slang 与 fmt 以 **PIC** 构建（因为它们要被静态链接进共享库插件）。
- `cmake/FindYosys.cmake` 把外部的 `yosys-config` 包装成一个干净的 `yosys::yosys` INTERFACE IMPORTED 目标，只透传 `-I`/`-D`，避免污染 sv-elab 自身的编译选项。
- `cmake/GitRevision.cmake` + `src/version.h.in` 在配置阶段把 git 哈希注入 `version.h`，供 C++ 的 `slang_version` 命令打印；`git` 不可用时优雅回退为 `UNKNOWN`。
- `src/CMakeLists.txt` 用 `BUILD_AS_PLUGIN` 分叉出两种产物：插件（`SHARED` → `slang.so`，隐藏符号可见性、安装到 `${YOSYS_DATDIR}/plugins`）与静态库（`STATIC` → 自包含的 `libyosys-slang.a`，靠 `STATIC_LIBRARY_OPTIONS` 打入 fmt/slang 对象）。
- Bazel 线（`MODULE.bazel` + `dependency_support/*.bzl` + 根 `BUILD.bazel` + `src/yosys_plugin/BUILD.bazel`）用「封闭可复现」思路产出同一个 `slang.so`：依赖全部固定版本/哈希拉取，但**只构建插件、不提供静态库**。
- 两套构建系统都从 `src/version.h.in` 生成 `version.h`，但 sv-elab 修订号来源不同：CMake 取 git 哈希，Bazel 取 `module_version()`。

## 7. 下一步学习建议

- 本讲聚焦「如何把源码变成产物」。若你想理解产物内部如何工作，建议回到 u2-l1 复习 `SlangFrontend` 的自我注册机制，它正是 `slang.so` 加载后立刻生效的根因。
- 若你想了解构建产物如何被验证，接续 u8-l1（等价性测试体系）与 u8-l2（croc_boot 端到端集成测试），它们正好运行在本讲产出的 `slang.so` 之上。
- 若你打算为 sv-elab 添加新的源文件，本讲的「源文件清单」分布在两处：CMake 的 `src/CMakeLists.txt:14-37` 与 Bazel 的 `BUILD.bazel:49-52`（`glob`）。改 CMake 时需手动增列，改 Bazel 时若文件在 `src/` 下会被 `glob` 自动收入——这是二次开发时容易踩到的差异点，详见 u8-l4（扩展开发）。
