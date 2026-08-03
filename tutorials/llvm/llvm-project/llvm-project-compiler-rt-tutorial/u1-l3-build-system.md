# 构建系统入门：用 CMake 构建 compiler-rt

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 compiler-rt 顶层 `CMakeLists.txt` 的整体结构与它是如何把「组件开关」翻译成「实际构建哪些库」的。
- 区分三种构建方式：随 LLVM 一起构建（in-tree）、独立构建（standalone）、runtimes 构建，并掌握独立构建的命令步骤。
- 列举最常用的 CMake 选项（`COMPILER_RT_BUILD_*`、安装路径、测试开关等），并知道在哪里查到它们的官方说明。
- 理解 `cmake/` 下 config-ix 系列配置模块在配置阶段探测平台能力、决定「为哪些架构/哪些 sanitizer 生成哪些库」的作用。

本讲是后续所有「读源码、跑测试」讲义的前置：你得先能把项目构建出来，才能观察行为、运行测试。

## 2. 前置知识

在开始前，请先建立这几个概念。如果你已熟悉，可以跳过对应小节。

### 2.1 CMake 的几个基础概念

compiler-rt 完全用 CMake 构建。本讲会反复出现这几个 CMake 原语：

- **`option(NAME "描述" 默认值)`**：声明一个布尔型缓存变量（cache variable）。它既可以在 CMake 内被代码读写，也可以在命令行用 `-DNAME=ON/OFF` 覆盖。compiler-rt 用它来控制「要不要构建某个组件」。
- **`add_subdirectory(dir)`**：进入子目录，处理那里的 `CMakeLists.txt`。compiler-rt 用它把 `lib/` 下每个子库逐个挂进来。
- **`include(file)`**：载入一个 CMake 模块（脚本）。compiler-rt 把大量逻辑放在 `cmake/*.cmake` 里，用 `include` 拼装起来。
- **`set(VAR value CACHE TYPE "doc")`**：定义一个带类型的缓存变量，可被命令行 `-D` 覆盖。

> 提示：本讲提到的所有选项都可以用 `cmake -LH <build_dir>` 在配置完成后查看其当前值与文档字符串。

### 2.2 compiler-rt 的定位回顾（承接 u1-l1、u1-l2）

compiler-rt 是一组约 28 个可裁剪子库（builtins、sanitizer 家族、profile、fuzzer、xray、orc 等）。它**不是一个库，而是一棵组件树**。顶层 `CMakeLists.txt` 的核心职责，就是回答两个问题：

1. 用户想构建**哪些**组件？（由 `COMPILER_RT_BUILD_*` 开关决定）
2. 当前工具链**能**为哪些架构/平台构建这些组件？（由 config-ix 在配置阶段探测决定）

最终构建出的库，是这两个集合的**交集**。

### 2.3 target triple（目标三元组）

一个目标三元组（如 `x86_64-unknown-linux-gnu`）描述「为哪种 CPU、哪种操作系统、哪种 ABI 生成代码」。compiler-rt 需要知道默认目标三元组，才能决定默认构建哪份运行时。第 4.2 节会看到它是如何被推导出来的。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `CMakeLists.txt` | 顶层构建入口：声明组件开关、判断构建模式、设置编译/安装路径、挂载子目录。 |
| `docs/BuildingCompilerRT.md` | 官方构建文档，面向发行版/工具链打包者，列出常用 CMake 变量。 |
| `cmake/base-config-ix.cmake` | 「基础配置」：建立 `compiler-rt` 元目标（metatarget）、区分 in-tree/standalone 的输出与安装路径、定义 `test_targets()` 宏。 |
| `cmake/config-ix.cmake` | 「能力探测」：检测编译器标志/库/架构是否可用，最终算出 `COMPILER_RT_SUPPORTED_ARCH` 和 `COMPILER_RT_HAS_*`。 |
| `lib/CMakeLists.txt` | 把顶层开关翻译成 `add_subdirectory`：决定真正进入哪些子库目录。 |
| `cmake/Modules/CompilerRTUtils.cmake` | 提供 `load_llvm_config()`（独立构建时定位 LLVM）与 `construct_compiler_rt_default_triple()`（推导默认三元组）等工具宏。 |

> 注意：`cmake/` 下还有一个 `builtin-config-ix.cmake` 和 `crt-config-ix.cmake`，它们只在构建 builtins / crt 时被各自包含，本讲聚焦最核心的 `base-config-ix.cmake` 与 `config-ix.cmake`，其余在第 15 单元「跨平台移植」讲义中再深入。

## 4. 核心概念与源码讲解

### 4.1 顶层 CMakeLists 结构与组件开关

#### 4.1.1 概念说明

compiler-rt 的顶层 `CMakeLists.txt` 不是一堆杂乱的脚本，而是一条清晰的「配置流水线」。你可以把它理解成四道工序：

1. **判断构建模式**：我是被当作 LLVM 的一部分构建，还是被单独构建？
2. **声明组件开关**：用一连串 `option()` 告诉 CMake「用户可能想关掉某些组件」。
3. **探测能力**：调用 config-ix，搞清楚当前工具链到底能构建什么。
4. **挂载子目录**：用 `add_subdirectory` 把要构建的子库一个个挂进来。

理解了这条流水线，你以后在源码树里找任何「为什么没构建某个库」的答案都能顺藤摸瓜。

#### 4.1.2 核心流程

顶层配置的执行顺序可以画成下面这条链：

```text
cmake_minimum_required(3.20)
   │
   ├─ 判断 standalone？ ──► 是 ──► project(CompilerRT)、load_llvm_config()
   │
   ├─ include(base-config-ix)   # 元目标、输出/安装路径、test_targets() 宏
   ├─ include(CompilerRTUtils)
   │
   ├─ 一长串 option(COMPILER_RT_BUILD_* ...)   # 组件开关
   │
   ├─ construct_compiler_rt_default_triple()   # 推导默认三元组
   │
   ├─ include(config-ix)        # 探测能力 → COMPILER_RT_HAS_*、SUPPORTED_ARCH
   │
   ├─ 设置 SANITIZER_COMMON_CFLAGS 等编译/链接标志
   │
   └─ add_subdirectory(include / lib / unittests / test / tools)
```

关键点：**组件开关只表达「意图」，能力探测才决定「现实」**。比如 `COMPILER_RT_BUILD_SANITIZERS=ON` 表示「我想构建 sanitizer」，但最终是否构建 ASan，还要看 `COMPILER_RT_HAS_ASAN` 在 config-ix 里是否被算成 `TRUE`（见 4.4 节）。

#### 4.1.3 源码精读

**最低 CMake 版本与子项目标题**。文件开头声明了最低版本要求，并把本项目在 LLVM 多项目构建里的标题设为 `Compiler-RT`：

[CMakeLists.txt:L6-L15](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/CMakeLists.txt#L6-L15) —— 第 6 行 `cmake_minimum_required(VERSION 3.20.0)`，第 7–13 行给出一个预警：从 LLVM 24 起最低要求将升到 3.31.0。第 15 行 `set(LLVM_SUBPROJECT_TITLE "Compiler-RT")`。

**组件开关（最核心的一段）**。下面这一串 `option()` 就是「组件总开关」，每个对应一类子库，默认基本都是 `ON`：

[CMakeLists.txt:L79-L110](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/CMakeLists.txt#L79-L110) —— 这里的关键开关含义如下：

| 选项 | 默认 | 控制什么 |
|------|------|----------|
| `COMPILER_RT_BUILD_BUILTINS` | ON | builtins 库（libgcc 替代品） |
| `COMPILER_RT_BUILD_SANITIZERS` | ON | 整个 sanitizer 家族（ASan/MSan/TSan/UBSan…及其公共设施） |
| `COMPILER_RT_BUILD_XRAY` | ON | XRay 函数追踪运行时 |
| `COMPILER_RT_BUILD_LIBFUZZER` | ON | libFuzzer 模糊测试引擎 |
| `COMPILER_RT_BUILD_PROFILE` | ON | profile 剖析运行时 |
| `COMPILER_RT_BUILD_CTX_PROFILE` | ON | 上下文敏感剖析 |
| `COMPILER_RT_BUILD_MEMPROF` | ON | MemProf 内存剖析 |
| `COMPILER_RT_BUILD_ORC` | ON | ORC JIT 运行时 |
| `COMPILER_RT_BUILD_GWP_ASAN` | ON | GWP-ASan 采样守卫分配器（并链入 Scudo） |

后面紧跟的 `mark_as_advanced(...)` 表示这些选项在 `cmake-gui` 里默认折叠为「高级」，普通用户一般不碰。

> 术语：`option()` 声明的变量是「缓存变量」，第一次配置时写入 CMakeCache.txt；若想强制改值，命令行加 `-D` 即可，例如 `-DCOMPILER_RT_BUILD_SANITIZERS=OFF`。

**从开关到子目录的翻译**。顶层在第 911 行 `add_subdirectory(lib)` 后，真正把开关翻译成「进入哪些子库」的工作发生在 `lib/CMakeLists.txt`。以 sanitizer 为例：

[lib/CMakeLists.txt:L38-L55](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/CMakeLists.txt#L38-L55) —— 第 38 行 `if(COMPILER_RT_BUILD_SANITIZERS)` 是第一道闸门（意图）；第 52–54 行的 `foreach` 遍历 `COMPILER_RT_SANITIZERS_TO_BUILD`，对每个 sanitizer 调用 `compiler_rt_build_runtime()`。

而 `compiler_rt_build_runtime()` 内部还有第二道闸门（现实）：

[lib/CMakeLists.txt:L20-L32](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/CMakeLists.txt#L20-L32) —— 第 22 行 `if(COMPILER_RT_HAS_${runtime_uppercase})` 正是 config-ix 算出来的「能力」标志。只有**意图 + 能力同时为真**，第 29 行的 `add_subdirectory(${runtime})` 才会执行。

**顶层挂载的全部子目录**。最后，顶层按固定顺序挂载四大子目录：

[CMakeLists.txt:L841-L936](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/CMakeLists.txt#L841-L936) —— 第 841 行 `add_subdirectory(include)`（安装公共头）、第 911 行 `add_subdirectory(lib)`（构建库本体）、第 913–934 行在 `COMPILER_RT_INCLUDE_TESTS` 为真时挂载 `unittests` 和 `test`、第 936 行 `add_subdirectory(tools)`。

#### 4.1.4 代码实践

**实践目标**：亲手体会「组件开关如何影响最终构建范围」。

**操作步骤**（独立构建，详见 4.2 节）：

1. 进入 compiler-rt 源码目录，配置一个**只构建 builtins、关闭 sanitizer** 的构建：

   ```bash
   cmake -S . -B build-min \
     -DCOMPILER_RT_STANDALONE_BUILD=ON \
     -DLLVM_CMAKE_DIR=/path/to/llvm/install/lib/cmake/llvm \
     -DCOMPILER_RT_BUILD_BUILTINS=ON \
     -DCOMPILER_RT_BUILD_SANITIZERS=OFF \
     -DCOMPILER_RT_BUILD_XRAY=OFF \
     -DCOMPILER_RT_BUILD_LIBFUZZER=OFF \
     -DCOMPILER_RT_BUILD_ORC=OFF \
     -DCOMPILER_RT_BUILD_PROFILE=OFF \
     -DCOMPILER_RT_BUILD_MEMPROF=OFF \
     -DCOMPILER_RT_BUILD_CTX_PROFILE=OFF \
     -DCOMPILER_RT_BUILD_GWP_ASAN=OFF
   ```

   > 说明：`LLVM_CMAKE_DIR` 指向你已安装的 LLVM 的 `lib/cmake/llvm` 目录（独立构建需要它，原因见 4.2 节）。

2. 配置完成后，查看缓存里这些开关的实际值：

   ```bash
   cmake -LH build-min | grep COMPILER_RT_BUILD
   ```

3. 列出实际会构建的目标，观察是否真的只剩 builtins：

   ```bash
   cmake --build build-min --target help 2>/dev/null | grep -i clang_rt
   ```

**需要观察的现象**：第 2 步应显示 `SANITIZERS/XRAY/...` 等都被设成 `OFF`；第 3 步列出的 `clang_rt.*` 目标应明显变少（只剩 `clang_rt.builtins` 之类）。

**预期结果**：通过关掉开关，`lib/CMakeLists.txt` 里的 `if(COMPILER_RT_BUILD_*)` 闸门不再进入对应 `add_subdirectory`，因此那些子库根本不会被纳入构建系统。

> 待本地验证：具体能列出多少个 `clang_rt.*` 目标取决于你的平台与工具链能力（见 4.4 节）。

#### 4.1.5 小练习与答案

**练习 1**：如果只设置 `-DCOMPILER_RT_BUILD_SANITIZERS=OFF`，但忘了关 `COMPILER_RT_BUILD_XRAY`，`sanitizer_common` 还会被构建吗？

**参考答案**：仍可能被构建。因为 `lib/CMakeLists.txt` 第 11–14 行的条件是「sanitizer **或** xray **或** memprof **或** ctx_profile」任一为真且 `COMPILER_RT_HAS_SANITIZER_COMMON` 为真就会构建 `sanitizer_common`——XRay 等组件复用了 sanitizer_common 的公共设施。

**练习 2**：为什么所有 `option()` 默认都是 `ON`？如果发行版想缩小体积，应该怎么做？

**参考答案**：默认全开符合「开箱即用」的体验。发行版打包时按需用 `-D` 关闭不想要的组件（例如嵌入式系统常关掉 sanitizer/xray/fuzzer），这正是 `option()` 作为缓存变量的设计意图。

### 4.2 独立构建 vs runtimes 构建

#### 4.2.1 概念说明

compiler-rt 支持三种「被构建」的姿态，理解它们的区别是本节重点：

1. **随 LLVM 一起构建（in-tree）**：把 compiler-rt 作为 `llvm-project/llvm` 构建树的一部分。这时 LLVM 的构建系统会把 `LLVM_LIBRARY_OUTPUT_INTDIR` 等变量传给 compiler-rt，让它「假装」自己在 LLVM 树内。
2. **独立构建（standalone）**：直接对 compiler-rt 目录跑 `cmake`。这时它需要自己去定位一个**已安装**的 LLVM（通过 `find_package(LLVM)`），用来获取工具、头文件和输出路径。
3. **runtimes 构建**：LLVM 较新的「runtimes」基础设施，由顶层 `runtimes/` 目录统一调度 compiler-rt、libcxx、libunwind 等运行时，用「刚构建好的 Clang」去交叉编译各目标的运行时。compiler-rt 通过 `LLVM_RUNTIMES_BUILD` 变量识别这一模式。

> 给初学者的直觉：in-tree 是「我和 LLVM 同生共死」；standalone 是「我先有了一个 LLVM，再单独编译我这块」；runtimes 是「LLVM 用一个总指挥统一编排所有运行时，常用于交叉编译」。

#### 4.2.2 核心流程

三种模式的判别与分流集中在顶层前半段：

```text
CMAKE_SOURCE_DIR == CMAKE_CURRENT_SOURCE_DIR  或  COMPILER_RT_STANDALONE_BUILD?
        │ 是 ──► COMPILER_RT_STANDALONE_BUILD = TRUE
        │         project(CompilerRT)
        │
LLVM_RUNTIMES_BUILD?  ──► 是 ──► 走 runtimes 路径（含 HandleLibC）
        │
否则若 LLVM_LIBRARY_OUTPUT_INTDIR 等已就绪 ──► LLVM_TREE_AVAILABLE = On（in-tree）
```

随后 `base-config-ix.cmake` 根据「是否 `LLVM_TREE_AVAILABLE`」选择**不同的输出路径与测试编译器**。

#### 4.2.3 源码精读

**standalone 的判别**。顶层用一行 `if` 判定是否独立构建：

[CMakeLists.txt:L28-L33](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/CMakeLists.txt#L28-L33) —— 当 `CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR`（即 compiler-rt 就是构建根）或显式传了 `COMPILER_RT_STANDALONE_BUILD` 时，进入 standalone 分支：调用 `project(CompilerRT ...)`、置标志位、开启 IDE 文件夹分组。

**standalone 时的特殊设置**。在 standalone 分支里，会设置 C++ 标准、调用 `load_llvm_config()` 定位 LLVM、查找 Python：

[CMakeLists.txt:L122-L163](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/CMakeLists.txt#L122-L163) —— 第 123–125 行设置 `CMAKE_CXX_STANDARD 17`；第 127–129 行在**非 runtimes 构建**时调用 `load_llvm_config()`（注意：runtimes 模式不调用，因为它已由上层提供 LLVM）。

`load_llvm_config()` 的核心是用 `find_package(LLVM)` 找到已安装的 LLVM CMake 包：

[cmake/Modules/CompilerRTUtils.cmake:L325-L336](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/Modules/CompilerRTUtils.cmake#L325-L336) —— 第 325 行 `find_package(LLVM HINTS "${LLVM_CMAKE_DIR}")`。若找不到，第 326–329 行会给出明确警告，提示用 `-DLLVM_CMAKE_DIR=...` 重新配置。这就是为什么独立构建必须提供 `LLVM_CMAKE_DIR`。

**in-tree 的判别（LLVM_TREE_AVAILABLE）**。`base-config-ix.cmake` 用三个变量判断「我是否在 LLVM 树内」：

[cmake/base-config-ix.cmake:L53-L57](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/base-config-ix.cmake#L53-L57) —— 当 LLVM 构建系统注入了 `LLVM_LIBRARY_OUTPUT_INTDIR`、`LLVM_RUNTIME_OUTPUT_INTDIR` 和 `PACKAGE_VERSION` 三个变量时，认为「LLVM 树可用」，于是 compiler-rt 可以据此构造输出路径，表现得像在树内一样。

**两套不同的输出路径与测试编译器**。这是 in-tree 与 standalone 最大的可观察差异：

[cmake/base-config-ix.cmake:L59-L96](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/base-config-ix.cmake#L59-L96) —— 在 `LLVM_TREE_AVAILABLE` 分支（第 59–82 行），输出目录取自 Clang 资源目录、测试用「刚构建好的 Clang」；在 `else`（standalone）分支（第 83–96 行），输出目录默认是构建目录本身、测试用宿主编译器 `CMAKE_C_COMPILER`，且 `COMPILER_RT_INCLUDE_TESTS` 默认 `OFF`。

> 术语：**Clang 资源目录（resource dir）**是 Clang 存放运行时库与头文件的目录，通常形如 `lib/clang/<version>`。compiler-rt 的产物默认就安装到这里，Clang 链接 sanitizer 时会自动到资源目录里找 `clang_rt.asan-*.a`。

**默认三元组的推导**。无论哪种模式，都需要一个默认三元组，由 `construct_compiler_rt_default_triple()` 推导：

[CMakeLists.txt:L165](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/CMakeLists.txt#L165) 调用该宏；其内部逻辑见 [cmake/Modules/CompilerRTUtils.cmake:L382-L422](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/Modules/CompilerRTUtils.cmake#L382-L422) —— 第 393 行从 `LLVM_TARGET_TRIPLE` 取初值；第 402–419 行对 Clang 编译器额外执行 `-print-target-triple` 做归一化（因为 Clang 的规范三元组可能与配置传入的不完全一致）；第 421–422 行把三元组按 `-` 拆分，取出第一段作为 `COMPILER_RT_DEFAULT_TARGET_ARCH`。

**runtimes 构建的入口**。runtimes 模式通过 `LLVM_RUNTIMES_BUILD` 识别：

[CMakeLists.txt:L45-L49](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/CMakeLists.txt#L45-L49) —— runtimes 模式会把 `../runtimes/cmake/Modules` 加入模块路径并 `include(HandleLibC)`，使 compiler-rt 子组件能共享 `runtimes-libc-*` 等目标（与 libcxx/libunwind 一致）。

#### 4.2.4 代码实践

**实践目标**：用一条命令体验 in-tree 构建，并与 standalone 对比输出路径差异。

**操作步骤**：

1. 假设你已 checkout 整个 `llvm-project` monorepo。用 LLVM 作为构建根、启用 compiler-rt 项目：

   ```bash
   cmake -S llvm -B build-llvm \
     -DCMAKE_BUILD_TYPE=Release \
     -DLLVM_ENABLE_PROJECTS="clang;compiler-rt"
   cmake --build build-llvm --target clang_rt.builtins-x86_64 2>/dev/null || true
   ```

2. 观察产物落在哪。in-tree 模式下应出现在 Clang 资源目录下，例如：

   ```bash
   ls build-llvm/lib/clang/*/lib/linux/ | head
   ```

**需要观察的现象**：步骤 2 能看到 `clang_rt.builtins-x86_64.a` 等文件位于 `lib/clang/<version>/lib/<os>/`，这正是 in-tree 模式 `base-config-ix.cmake` 第 61 行 `get_clang_resource_dir(...)` 决定的路径。

**预期结果**：in-tree 把库放进 Clang 资源目录，方便刚构建好的 Clang 直接使用；而 4.1.4 节的 standalone 构建默认把库放在 `build-min/` 下（除非另行设置 `COMPILER_RT_INSTALL_PATH` 后 `make install`）。

> 待本地验证：资源目录的确切版本号 `<version>` 与 OS 子目录名取决于你的配置。

#### 4.2.5 小练习与答案

**练习 1**：standalone 构建时，如果不提供 `LLVM_CMAKE_DIR` 会发生什么？

**参考答案**：`load_llvm_config()` 里的 `find_package(LLVM)` 找不到 LLVM CMake 包，打印一条「UNSUPPORTED COMPILER-RT CONFIGURATION DETECTED」警告（见 [CompilerRTUtils.cmake:L326-L329](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/Modules/CompilerRTUtils.cmake#L326-L329)），随后退化为一个仅用于生成 lit 测试套件、几乎不能真正构建运行时的 mock 配置（第 376–377 行）。

**练习 2**：为什么 standalone 默认 `COMPILER_RT_INCLUDE_TESTS=OFF`，而 in-tree 默认跟随 `LLVM_INCLUDE_TESTS`？

**参考答案**：standalone 环境常常没有完整的 LLVM 构建树与 `llvm-lit`，跑测试链路不全；in-tree 则天然具备这些工具，所以默认开启测试。见 [base-config-ix.cmake:L64-L65](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/base-config-ix.cmake#L64-L65) 与第 91 行。

### 4.3 关键 CMake 选项与安装路径

#### 4.3.1 概念说明

除了「构建哪些组件」，用户最常调的另一类选项是**输出与安装路径**，以及**测试相关开关**。官方文档 `docs/BuildingCompilerRT.md` 把它们集中列了出来。本节带你对照源码读懂这些选项。

> 重要前提：该文档第 11–15 行明确说明，本页内容面向「把 Compiler-RT 作为发行版/工具链一部分打包」的厂商；普通用户通常只需用厂商提供的构建，不必自己配。

#### 4.3.2 核心流程

安装路径的推导逻辑可以概括为：

```text
COMPILER_RT_INSTALL_PATH  （用户给定前缀，可相对 CMAKE_INSTALL_PREFIX）
   │
   ├── + COMPILER_RT_INSTALL_LIBRARY_DIR  （默认 lib 或 lib/<os>）
   ├── + COMPILER_RT_INSTALL_BINARY_DIR   （默认 bin）
   ├── + COMPILER_RT_INSTALL_INCLUDE_DIR  （默认 include）
   └── + COMPILER_RT_INSTALL_DATA_DIR     （默认 share）
```

而 `lib/<os>` 中的 `<os>`（如 `linux`）由 `COMPILER_RT_OS_DIR` 决定。

#### 4.3.3 源码精读

**OS 子目录的推导**。安装库目录默认带一个 OS 段，由下面这段决定：

[cmake/base-config-ix.cmake:L115-L123](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/base-config-ix.cmake#L115-L123) —— 把 `CMAKE_SYSTEM_NAME` 转小写作为 OS 目录名（Android 例外，映射为 `linux`，因为驱动在 `linux` 目录下找库）。

**库输出/安装目录的拼装**。结合是否启用 per-target runtime dir，拼出最终的库目录：

[cmake/base-config-ix.cmake:L125-L138](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/base-config-ix.cmake#L125-L138) —— 第 132–137 行是常见情况：输出到 `<output_dir>/lib/<os>`、安装到 `<install_path>/lib/<os>`。`extend_path()` 负责把「可能为空的前缀」与「相对段」安全拼接。

**其余安装子目录**。bin/include/share 目录用同样方式拼装：

[cmake/base-config-ix.cmake:L139-L147](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/base-config-ix.cmake#L139-L147)。

**文档对安装选项的说明**。`BuildingCompilerRT.md` 给出了这些选项的官方释义与一个常见陷阱：

[docs/BuildingCompilerRT.md:L42-L85](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/docs/BuildingCompilerRT.md#L42-L85) —— 注意第 47–52 行的提醒：设置相对路径的 `COMPILER_RT_INSTALL_PATH` 时必须带 `:PATH` 类型标注（`-DCOMPILER_RT_INSTALL_PATH:PATH=...`），否则 CMake 会把它转成绝对路径。

**测试相关开关**。文档还列出了两个测试选项：

[docs/BuildingCompilerRT.md:L87-L102](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/docs/BuildingCompilerRT.md#L87-L102) —— `COMPILER_RT_INCLUDE_TESTS`（默认：in-tree 跟随 `LLVM_INCLUDE_TESTS`，standalone 为 `OFF`）控制是否生成测试；`COMPILER_RT_ENABLE_TEST_SUITES`（默认 `all`）可细粒度选择只启用某些套件，如 `"-DCOMPILER_RT_ENABLE_TEST_SUITES=asan;ubsan;lsan"`。

**LLVM 通用变量**。还有一个常被遗忘的 `LLVM_LIBDIR_SUFFIX`：

[docs/BuildingCompilerRT.md:L106-L113](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/docs/BuildingCompilerRT.md#L106-L113) —— 例如 64 位系统加 `-DLLVM_LIBDIR_SUFFIX=64` 可把库装到 `/usr/lib64`。

#### 4.3.4 代码实践

**实践目标**：定制安装路径并把库装到一个独立目录，验证 OS 段是否出现。

**操作步骤**：

1. 在 4.1.4 的配置基础上加自定义安装路径，重新配置并安装：

   ```bash
   cmake -S . -B build-min \
     -DCOMPILER_RT_STANDALONE_BUILD=ON \
     -DLLVM_CMAKE_DIR=/path/to/llvm/install/lib/cmake/llvm \
     -DCMAKE_INSTALL_PREFIX=/tmp/crt-install \
     -DCOMPILER_RT_BUILD_BUILTINS=ON \
     -DCOMPILER_RT_BUILD_SANITIZERS=OFF
   cmake --build build-min
   cmake --install build-min
   ```

2. 查看安装结果：

   ```bash
   find /tmp/crt-install -name 'clang_rt*' -o -name '*.h' | sort
   ```

**需要观察的现象**：库文件应出现在 `/tmp/crt-install/lib/<os>/`（Linux 上是 `lib/linux/`），头文件出现在 `include/`。

**预期结果**：印证 `base-config-ix.cmake` 第 132–137 行的拼装规则——库目录带 OS 段，头文件目录不带。

> 待本地验证：安装出的具体文件清单取决于平台与启用的组件。

#### 4.3.5 小练习与答案

**练习 1**：在 Android 上构建时，库会装到 `lib/android/` 还是 `lib/linux/`？

**参考答案**：装到 `lib/linux/`。因为 `base-config-ix.cmake` 第 116–119 行专门把 Android 的 `COMPILER_RT_OS_DIR` 设为 `linux`，与 Clang 驱动搜索库的目录保持一致。

**练习 2**：为什么相对路径的 `COMPILER_RT_INSTALL_PATH` 必须用 `:PATH` 标注？

**参考答案**：见 [docs/BuildingCompilerRT.md:L47-L52](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/docs/BuildingCompilerRT.md#L47-L52)。若不带类型标注，CMake 会把相对路径当成普通字符串并在内部转成绝对路径，导致它不再相对 `CMAKE_INSTALL_PREFIX`，安装位置与预期不符。

### 4.4 config-ix：平台与能力探测

#### 4.4.1 概念说明

前面反复提到的「能力探测」由 `cmake/config-ix.cmake` 完成（顶层第 346 行 `include(config-ix)` 触发）。它的任务是：在配置阶段，用一系列「试着编译/链接一小段代码」的检查，回答三个问题：

1. 编译器/链接器支持哪些标志？（`COMPILER_RT_HAS_*`）
2. 系统有哪些库？（libc、libdl、libpthread…）
3. 当前工具链**能交叉编译哪些架构**？（`COMPILER_RT_SUPPORTED_ARCH`）

进而为每个 sanitizer 算出一个 `COMPILER_RT_HAS_<SAN>` 布尔值，供 `lib/CMakeLists.txt` 的第二道闸门使用。

#### 4.4.2 核心流程

config-ix 的整体流程：

```text
检查编译标志  (check_cxx_compiler_flag → COMPILER_RT_HAS_*_FLAG)
检查系统库    (check_library_exists → COMPILER_RT_HAS_LIBC/LIBDL/...)
写一个 hello world 测试源
   └─ test_targets()  逐个架构试着编译 → COMPILER_RT_SUPPORTED_ARCH
        └─ 求交集     ASAN_SUPPORTED_ARCH ∩ SUPPORTED_ARCH
按 arch + OS 组合    设置 COMPILER_RT_HAS_ASAN/MSAN/TSAN/...
```

数学上，某个 sanitizer 被启用的条件可写成：

\[ \text{HAS\_X} = \text{HAS\_SANITIZER\_COMMON} \;\land\; (X_{\text{arch}} \cap \text{SUPPORTED\_ARCH} \neq \emptyset) \;\land\; (\text{OS} \in X_{\text{supported OS}}) \]

即「公共设施可用」∧「目标架构在支持列表里」∧「操作系统在支持列表里」三者同时成立。

#### 4.4.3 源码精读

**编译标志探测（节选）**。config-ix 用 `check_cxx_compiler_flag` 探测几十个标志，例如：

[cmake/config-ix.cmake:L90-L121](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/config-ix.cmake#L90-L121) —— 每一行形如 `check_cxx_compiler_flag(-fPIC COMPILER_RT_HAS_FPIC_FLAG)`，把「能否用 `-fPIC`」的结果存进 `COMPILER_RT_HAS_FPIC_FLAG`。顶层后面那段（第 404–421 行）就是根据这些结果决定把哪些标志真正加进 `SANITIZER_COMMON_CFLAGS`。

**架构可编译性探测**。config-ix 先写一个最简 hello world 源，再调用 `test_targets()` 逐个架构「试编译」：

[cmake/config-ix.cmake:L248-L260](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/config-ix.cmake#L248-L260) —— 第 248–249 行写测试源；第 260 行 `test_targets()`（定义在 `base-config-ix.cmake`）根据默认架构，对 x86_64/i386/aarch64/riscv64 等逐一用 `test_target_arch()` 试编译，成功者加入 `COMPILER_RT_SUPPORTED_ARCH`。

> `test_targets()` 宏内部会针对不同架构传不同 flag（如 x86 用 `-m64`/`-m32`），见 [cmake/base-config-ix.cmake:L203-L331](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/base-config-ix.cmake#L203-L331)。

**最终打印支持架构**。配置完成时会打印一行非常关键的状态信息：

[cmake/config-ix.cmake:L763-L766](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/config-ix.cmake#L763-L766) —— `message(STATUS "Compiler-RT supported architectures: ${COMPILER_RT_SUPPORTED_ARCH}")`。排查「为什么没构建某架构」时，先看这一行。

**sanitizer 清单与「要构建哪些」**。第 768 行定义全部可选 sanitizer，第 769–771 行处理 `COMPILER_RT_SANITIZERS_TO_BUILD`（默认 `all`）：

[cmake/config-ix.cmake:L768-L771](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/config-ix.cmake#L768-L771)。

**「能力」标志的最终计算（以 ASan 为例）**。这是把架构、OS、公共设施三者合起来下结论的地方：

[cmake/config-ix.cmake:L793-L797](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/config-ix.cmake#L793-L797) —— `COMPILER_RT_HAS_ASAN` 为真当且仅当 `COMPILER_RT_HAS_SANITIZER_COMMON` 且 `ASAN_SUPPORTED_ARCH` 同时成立。其余 sanitizer（TSan、MSan、HWASan…）在第 799–975 行用同样的「arch ∩ OS」模式逐一计算。这正是 4.1.2 节所说「意图 + 现实取交集」中的「现实」来源。

> 补充：`COMPILER_RT_HAS_SANITIZER_COMMON` 本身也要 arch 与 OS 都达标，见 [cmake/config-ix.cmake:L773-L780](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/config-ix.cmake#L773-L780)。它是几乎所有 sanitizer 的前置条件，所以 sanitizer_common 在第 3 单元被定位为「公共地基」。

#### 4.4.4 代码实践

**实践目标**：从配置日志中读出「能力探测」的结果，并解释某个 sanitizer 为何被启用或禁用。

**操作步骤**：

1. 重新跑一次 4.1.4 的配置，但这次把配置过程的输出重定向保存：

   ```bash
   cmake -S . -B build-min \
     -DCOMPILER_RT_STANDALONE_BUILD=ON \
     -DLLVM_CMAKE_DIR=/path/to/llvm/install/lib/cmake/llvm \
     -DCOMPILER_RT_BUILD_BUILTINS=ON \
     -DCOMPILER_RT_BUILD_SANITIZERS=ON \
     2>&1 | tee configure.log
   ```

2. 在日志里搜索关键状态行：

   ```bash
   grep -E "supported architectures|Compiler-RT supported" configure.log
   ```

3. 查询某个能力标志的最终值：

   ```bash
   cmake -LH build-min | grep -E "COMPILER_RT_HAS_ASAN|COMPILER_RT_HAS_TSAN"
   ```

**需要观察的现象**：第 2 步能看到形如 `-- Compiler-RT supported architectures: x86_64` 的行；第 3 步能看到 `COMPILER_RT_HAS_ASAN` 之类的内部变量值。

**预期结果**：若你的平台是 x86_64 Linux，`COMPILER_RT_HAS_ASAN` 应为 `ON/TRUE`；`COMPILER_RT_HAS_TSAN` 同样为真（TSan 支持 x86_64 Linux）。把这一结果与 [config-ix.cmake:L793-L797](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/config-ix.cmake#L793-L797) 和 [L871-L881](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/config-ix.cmake#L871-L881) 对照，验证「arch ∩ OS」逻辑。

> 待本地验证：能力标志是 CMake 内部变量，部分不会进缓存；以配置日志的 `-- ...` 行与 `CMakeCache.txt` 为准。

#### 4.4.5 小练习与答案

**练习 1**：在 32 位 ARM Linux 上，TSan 通常不会被构建。请根据源码解释原因。

**参考答案**：TSan 需要架构在 `TSAN_SUPPORTED_ARCH`（典型为 x86_64/aarch64/mips64/powerpc64 等位宽较大的架构）且与 `COMPILER_RT_SUPPORTED_ARCH` 求交集后非空。32 位 ARM 一般不在 `TSAN_SUPPORTED_ARCH` 列表中，于是 [config-ix.cmake:L871-L881](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/config-ix.cmake#L871-L881) 中 `TSAN_SUPPORTED_ARCH` 为空，`COMPILER_RT_HAS_TSAN` 被设为 `FALSE`，`lib/CMakeLists.txt` 第二道闸门 `if(COMPILER_RT_HAS_TSAN)` 不通过。

**练习 2**：`COMPILER_RT_HAS_SANITIZER_COMMON` 依赖哪些条件？为什么它对几乎所有 sanitizer 都至关重要？

**参考答案**：依赖 `SANITIZER_COMMON_SUPPORTED_ARCH` 非空、未启用 `LLVM_USE_SANITIZER`（即不是在用 sanitizer 构建 sanitizer 自身），且 OS 在支持列表里（见 [config-ix.cmake:L773-L780](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/config-ix.cmake#L773-L780)）。它至关重要，是因为 ASan/MSan/TSan/UBSan/LSan 等的 `COMPILER_RT_HAS_*` 计算都以它为前置 `AND` 条件——没有公共设施，任何具体 sanitizer 都无法独立构建。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**「最小自定义构建 + 诊断」**任务：

1. **规划**：假设你要为一台 x86_64 Linux 服务器打包一个**只含 builtins 和 ASan** 的 compiler-rt，其余组件都不要。写出你需要传给 `cmake` 的全部关键 `-D` 选项（构建模式、定位 LLVM、组件开关、安装路径）。

2. **执行**：用你写出的选项配置并构建：

   ```bash
   cmake -S . -B build-asan \
     -DCOMPILER_RT_STANDALONE_BUILD=ON \
     -DLLVM_CMAKE_DIR=/path/to/llvm/install/lib/cmake/llvm \
     -DCMAKE_INSTALL_PREFIX=/tmp/crt-asan \
     -DCOMPILER_RT_BUILD_BUILTINS=ON \
     -DCOMPILER_RT_BUILD_SANITIZERS=ON \
     -DCOMPILER_RT_BUILD_XRAY=OFF \
     -DCOMPILER_RT_BUILD_LIBFUZZER=OFF \
     -DCOMPILER_RT_BUILD_ORC=OFF \
     -DCOMPILER_RT_BUILD_PROFILE=OFF \
     -DCOMPILER_RT_BUILD_MEMPROF=OFF \
     -DCOMPILER_RT_BUILD_CTX_PROFILE=OFF \
     -DCOMPILER_RT_BUILD_GWP_ASAN=OFF
   cmake --build build-asan
   ```

   注意：`COMPILER_RT_BUILD_SANITIZERS=ON` 会让「所有支持的 sanitizer」都尝试构建。若想**只**构建 ASan，需要额外用 `-DCOMPILER_RT_SANITIZERS_TO_BUILD=asan`（见 4.4 节 sanitizer 清单）。

3. **诊断**：从配置日志里找出并解释三件事：
   - 当前 `Compiler-RT supported architectures` 是什么？（→ config-ix 探测结果）
   - `COMPILER_RT_HAS_ASAN` 最终是否为真？（→ arch ∩ OS 交集）
   - 实际构建出了哪些 `clang_rt.*` 库？（→ 意图 ∩ 现实的最终产物）

4. **验证**：把构建出的 `clang_rt.asan-x86_64.so`/`.a` 路径，对照 4.3 节的安装路径规则，解释它为什么出现在 `lib/linux/` 下。

> 待本地验证：以上命令的具体输出与产物清单依赖本机工具链；若某步报错，请回到对应模块对照源码排查。

## 6. 本讲小结

- compiler-rt 顶层 `CMakeLists.txt` 是一条「判别模式 → 声明开关 → 探测能力 → 挂载子目录」的配置流水线；组件开关只表达**意图**，能力探测才决定**现实**，最终构建范围是两者交集。
- `COMPILER_RT_BUILD_*` 是一组默认全开的布尔开关，被 `lib/CMakeLists.txt` 翻译成 `add_subdirectory`；`compiler_rt_build_runtime()` 内的 `if(COMPILER_RT_HAS_*)` 是第二道闸门。
- 三种构建模式各有判据：in-tree 靠 `LLVM_TREE_AVAILABLE`、standalone 靠 `CMAKE_SOURCE_DIR` 判定并需 `load_llvm_config()` 定位已安装 LLVM、runtimes 靠 `LLVM_RUNTIMES_BUILD` 识别。
- 输出与安装路径由 `base-config-ix.cmake` 拼装，库目录默认带 OS 段（`lib/<os>`，Android 映射为 `linux`）；相对路径的 `COMPILER_RT_INSTALL_PATH` 必须带 `:PATH` 标注。
- `config-ix.cmake` 用「试编译/试链接」探测编译器标志、系统库与可目标架构，最终为每个 sanitizer 计算 `COMPILER_RT_HAS_*`；排查构建问题先看 `Compiler-RT supported architectures` 这行状态输出。
- 官方文档 `docs/BuildingCompilerRT.md` 是面向打包者的权威选项参考，遇到不确定的选项应回这里查证。

## 7. 下一步学习建议

- **想验证构建产物能否工作**：进入下一讲 [u1-l4 测试基础设施：lit 与 lit.cfg](u1-l4-testing-infrastructure.md)，学习如何运行 compiler-rt 的测试套件（如 `check-asan`），本讲的 `COMPILER_RT_INCLUDE_TESTS` 正是开启它的开关。
- **想深入 builtins**：构建成功后，可直接进入第 2 单元 [u2-l1 builtins 概览](u2-l1-builtins-overview.md)，用刚构建出的 `clang_rt.builtins` 观察编译器对 `__udivdi3` 等函数的调用。
- **想理解 config-ix 的全部细节**：本讲只讲了最核心的 `base-config-ix.cmake` 与 `config-ix.cmake`；`builtin-config-ix.cmake`、`crt-config-ix.cmake` 的分工留到第 15 单元 [u15-l1 跨平台移植](u15-l1-platform-portability.md) 系统讲解。
- **想自己加/裁运行时**：第 15 单元 [u15-l2 扩展 compiler-rt](u15-l2-extend-runtime-cmake.md) 会讲 `AddCompilerRT.cmake` 等宏，是本讲组件开关机制的高级应用。
