# 构建系统与目录结构

## 1. 本讲目标

上一篇（u1-l1）我们建立了对 Polly 的整体认知：它是 LLVM 的多面体循环优化器，吃 LLVM-IR、吐优化后的 LLVM-IR。本篇聚焦「工程层面」——**Polly 的代码长什么样、怎么构建出来、目录是怎么组织的**。

学完本讲，你应当能够：

- 说清楚 Polly 的两种构建模式（树内构建 in-tree / 独立构建 standalone）以及 `POLLY_BUNDLED_ISL` 开关的作用。
- 看懂 `CMakeLists.txt` 与 `lib/CMakeLists.txt` 是如何把 Polly 组织成「可链接库 + 可加载插件」两种产物的。
- 默写出 `include/polly`、`lib`、`test`、`unittests` 四大目录的职责，并能在 `lib` 下快速定位「检测 / 代码生成 / 调度优化」等功能对应的源码位置。

本讲不涉及任何算法细节，目标是让你拿到 Polly 源码后「不迷路」。

## 2. 前置知识

在阅读本讲前，你需要以下基础（都很轻量）：

- **CMake 是什么**：C/C++ 项目常用的构建系统生成器。你写一份 `CMakeLists.txt`，CMake 据此生成 Makefile 或 Ninja 文件，再编译。本讲会出现的关键概念：
  - `option(NAME "描述" 默认值)`：定义一个可由命令行 `-DNAME=ON/OFF` 切换的开关。
  - `add_subdirectory(dir)`：进入子目录继续解析它的 `CMakeLists.txt`。
  - `find_package(X)`：在系统中查找已安装的第三方库 X。
  - `target_link_libraries(A PUBLIC B)`：让目标 A 链接库 B。
- **LLVM 子项目（subproject）**：`llvm-project` 是一个包含 `llvm`、`clang`、`polly`、`compiler-rt` 等多个子项目的巨型仓库。每个子项目都能「跟随 LLVM 一起编译」（树内）或「单独编译、链接已安装的 LLVM」（独立）。Polly 的 `CMakeLists.txt` 第一步就是判断自己处于哪种模式。
- **LLVM Pass 与插件（plugin）**：LLVM 的优化以「Pass」为单位。Polly 整体可以作为一个 Pass 集合挂进 LLVM 的 Pass 管线；它既能编译进 LLVM 工具（如 `opt`/`clang`），也能编译成一个可被 `-load` 加载的动态库 `LLVMPolly`。这些概念在 u1-l4 会展开，本讲你只需知道「Polly 有两种打包形态」即可。
- **ISL（Integer Set Library）**：u1-l1 已介绍，它是多面体运算的数学引擎。Polly 既可以用仓库内自带的 ISL 源码（bundled），也可以用系统里已安装的 ISL（external）——这正是本讲要讲的构建选项之一。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [CMakeLists.txt](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/CMakeLists.txt) | Polly 顶层构建脚本：判断构建模式、处理 ISL、注册子目录 |
| [lib/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/CMakeLists.txt) | 把所有 `.cpp` 源文件组织成 `Polly` 库与 `LLVMPolly` 插件 |
| [lib/External/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/External/CMakeLists.txt) | 捆绑版 ISL 的编译脚本（生成 `PollyISL` 库） |
| [cmake/FindISL.cmake](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/cmake/FindISL.cmake) | 外部 ISL 的查找脚本（用 pkg-config 定位系统 ISL） |
| [include/polly/Config/config.h.cmake](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Config/config.h.cmake) | 由 CMake 配置生成的编译期配置头 |

> 提示：本仓库实际存在这些文件，行号均以当前 HEAD `abe5aa5cb` 为准。

## 4. 核心概念与源码讲解

### 4.1 CMake 构建基础：树内、独立与捆绑 ISL

#### 4.1.1 概念说明

Polly 的构建有两条「正交」的选择轴，理解它们就读懂了顶层 `CMakeLists.txt` 的一大半：

1. **构建模式（谁来提供 LLVM）**
   - **树内构建（in-tree）**：Polly 作为 `llvm-project` 的一部分被一起编译。此时 LLVM 的源码与构建变量已经就绪，CMake 变量 `LLVM_MAIN_SRC_DIR` 已定义。
   - **独立构建（standalone）**：单独进入 `polly/` 目录编译，通过 `find_package(LLVM)` 去找系统里已安装的 LLVM。此时 `LLVM_MAIN_SRC_DIR` 未定义，需要 `project(Polly)` 自己立起来。

2. **ISL 来源（谁来提供多面体数学库）**
   - **捆绑 ISL（bundled，默认）**：使用仓库内 `lib/External/isl/` 自带的 ISL 源码，编译成 Polly 私有的 `PollyISL` 库。
   - **外部 ISL（external）**：用系统已安装的 libisl（通过 pkg-config 查找），链接目标名为 `ISL`。

这两条轴由两个独立的判断控制，所以「树内 + 捆绑」「树内 + 外部」「独立 + 捆绑」「独立 + 外部」四种组合都合法。

#### 4.1.2 核心流程

顶层 `CMakeLists.txt` 的执行顺序可以概括为：

```text
1. 是否定义了 LLVM_MAIN_SRC_DIR？
     否 → 这是独立构建：project(Polly)、find_package(LLVM)、设置 POLLY_STANDALONE_BUILD
     是 → 这是树内构建：直接复用 LLVM 提供的变量
2. 把 cmake/ 与公共 cmake 工具加入模块搜索路径，include("polly_macros")
3. 处理 ISL 来源：
     option(POLLY_BUNDLED_ISL ... ON)
     ON  → ISL_TARGET = PollyISL（捆绑）
     OFF → find_package(ISL)、ISL_TARGET = ISL（外部）
4. include_directories 把 include/ 与 ISL 头文件加入搜索路径
5. 注册子目录：docs / lib / test / unittests / cmake
6. 由 config.h.cmake 生成 include/polly/Config/config.h
7. 定义 clang-format 检查/格式化目标
```

#### 4.1.3 源码精读

**① 判断构建模式。** 文件开头用 `LLVM_MAIN_SRC_DIR` 是否定义来区分两种模式；独立构建时会调用 `project(Polly)` 并 `find_package(LLVM CONFIG REQUIRED)`：[CMakeLists.txt:2-14](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/CMakeLists.txt#L2-L14) 中 `set(POLLY_STANDALONE_BUILD TRUE)` 标记独立构建，而树内构建则在 `else()` 分支里复用 `LLVM_MAIN_SRC_DIR`。

**② ISL 来源开关。** 这是本模块最关键的一段。[CMakeLists.txt:91-102](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/CMakeLists.txt#L91-L102) 定义了 `option(POLLY_BUNDLED_ISL "Use the bundled version of libisl included in Polly" ON)`——默认开启捆绑。当关闭时（`NOT POLLY_BUNDLED_ISL`）调用 `find_package(ISL MODULE REQUIRED)` 找外部 ISL 并令 `ISL_TARGET = ISL`；否则令 `ISL_TARGET = PollyISL`，指向捆绑源码。

> 关键点：变量 `ISL_TARGET` 是一个「抽象别名」，下游只需 `target_link_libraries(... ${ISL_TARGET})`，无需关心 ISL 来自哪里。这就是切换 ISL 来源能在不改动其它代码的情况下生效的原因。

**③ 头文件搜索路径。** [CMakeLists.txt:104-111](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/CMakeLists.txt#L104-L111) 把 `include/`、ISL 头文件、`lib/External` 等加入搜索路径（`BEFORE` 表示优先于系统路径）。

**④ 注册子目录。** [CMakeLists.txt:138-144](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/CMakeLists.txt#L138-L144) 依次 `add_subdirectory(docs)`、`add_subdirectory(lib)`、`add_subdirectory(test)`，并在 `POLLY_GTEST_AVAIL` 为真时才 `add_subdirectory(unittests)`。这就是「include/lib/test/unittests 四大目录」的来源。

**⑤ 捆绑 ISL 的实际编译。** 当 `POLLY_BUNDLED_ISL` 为真时，`lib/External/CMakeLists.txt` 把上百个 `.c` 文件编进 `PollyISL` 库：[lib/External/CMakeLists.txt:17](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/External/CMakeLists.txt#L17) 进入捆绑分支，最终在 [lib/External/CMakeLists.txt:286-288](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/External/CMakeLists.txt#L286-L288) 用 `add_polly_library(PollyISL ${ISL_FILES})` 产出该库。

**⑥ 外部 ISL 的查找逻辑。** 若选择外部 ISL，则走 [cmake/FindISL.cmake:1-5](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/cmake/FindISL.cmake#L1-L5)，它用 `pkg_search_module(ISL isl)` 通过 pkg-config 在系统中定位 libisl，找不到则 `FATAL_ERROR`。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（不需要真正编译，重点在读懂构建脚本）。

1. **实践目标**：搞清楚切换 ISL 来源时，到底有哪些变量和目标会改变。
2. **操作步骤**：
   - 打开 [CMakeLists.txt:91-102](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/CMakeLists.txt#L91-L102)，记录 `POLLY_BUNDLED_ISL=ON` 与 `=OFF` 两种情况下 `ISL_TARGET`、`ISL_INCLUDE_DIRS` 分别是什么。
   - 打开 [lib/CMakeLists.txt:117-119](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/CMakeLists.txt#L117-L119)，确认 `target_link_libraries(Polly PUBLIC ${ISL_TARGET})` 这一行是如何在两种来源下都能正确链接的。
3. **需要观察的现象**：你会发现 `lib/CMakeLists.txt` 里没有任何 `if(POLLY_BUNDLED_ISL)`，它只依赖 `ISL_TARGET` 这个别名——这就是「来源切换对上层透明」的设计。
4. **预期结果**：你能用一句话总结——「捆绑时链接 `PollyISL`（仓库内编译），外部时链接 `ISL`（系统库），二者通过 `ISL_TARGET` 别名统一」。
5. 如果你本地有 LLVM 构建环境，可进一步尝试：树内构建时在 cmake 命令行加 `-DLLVM_ENABLE_PROJECTS=polly`（这是启用子项目的标准方式，本环境无法运行验证，标注为**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：为什么独立构建时需要 `find_package(LLVM CONFIG REQUIRED)`，而树内构建不需要？
> **答案**：树内构建时 Polly 已经在 LLVM 的构建树里，`LLVM_MAIN_SRC_DIR` 等 LLVM 变量早已被父级 CMake 定义好，可直接复用；独立构建时 LLVM 是「外部依赖」，必须通过 `find_package` 在系统中定位已安装的 LLVM 配置（`LLVM_DIR`/`LLVM_CMAKE_DIR` 等）才能拿到头文件路径与链接库。

**练习 2**：`ISL_TARGET` 这个变量的设计目的是什么？
> **答案**：它是一个「抽象别名」，把「捆绑 `PollyISL`」和「外部 `ISL`」两种来源统一成一个名字。下游（如 `lib/CMakeLists.txt` 的 `target_link_libraries`）只引用 `ISL_TARGET`，因此切换来源时上层脚本完全不用改。

---

### 4.2 顶层目录与 include/lib 的 LLVM 子项目约定

#### 4.2.1 概念说明

Polly 遵循 LLVM 子项目的「标准目录约定」，掌握这套约定后，你看任何 LLVM 子项目（clang、lldb 等）都能快速定位代码：

- **`include/`**：公开头文件。Polly 的头文件统一放在 `include/polly/` 下，安装时会被拷到系统的 `include/polly/`。
- **`lib/`**：实现代码（`.cpp`）。`lib` 内部再按功能分子目录。
- **`test/`**：基于 `lit` + `FileCheck` 的回归测试，以 `.ll`（LLVM IR 文本）为主。
- **`unittests/`**：基于 `gtest` 的 C++ 单元测试。
- **`docs/`**：Sphinx 文档（`.rst`）。
- **`cmake/`**：自定义 CMake 模块（如 `FindISL.cmake`、`PollyConfig.cmake.in`）。
- **`utils/`**：辅助脚本。

「头文件在 `include/polly`、实现在 `lib`」的对应关系，是阅读 Polly 源码最基础的一条线索。

#### 4.2.2 核心流程

一个典型的「类」在代码里出现在两处：

```text
声明： include/polly/<某模块>/<XxxYyy>.h   （或 include/polly/<XxxYyy>.h）
实现： lib/<某模块>/XxxYyy.cpp
```

例如：

| 公开头文件 | 实现 | 含义 |
|-----------|------|------|
| `include/polly/ScopDetection.h` | `lib/Analysis/ScopDetection.cpp` | SCoP 检测 |
| `include/polly/ScopInfo.h` | `lib/Analysis/ScopInfo.cpp` | 多面体模型核心数据结构 |
| `include/polly/ScheduleOptimizer.h` | `lib/Transform/ScheduleOptimizer.cpp` | 调度优化 |
| `include/polly/CodeGen/CodeGeneration.h` | `lib/CodeGen/CodeGeneration.cpp` | 代码生成 |
| `include/polly/Pass/PhaseManager.h` | `lib/Pass/PhaseManager.cpp` | 阶段流水线管理 |

注意：头文件有的直接放在 `include/polly/` 下（如 `ScopInfo.h`），有的放在子目录（如 `include/polly/CodeGen/`、`include/polly/Support/`、`include/polly/Pass/`）。`include/polly/Config/` 比较特殊——它只有一个 `config.h.cmake` 模板，由 CMake 在构建时生成真正的 `config.h`：[CMakeLists.txt:148-149](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/CMakeLists.txt#L148-L149)。

#### 4.2.3 源码精读

**① 顶层安装头文件。** [CMakeLists.txt:113-126](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/CMakeLists.txt#L113-L126) 通过 `install(DIRECTORY include/ ... PATTERN "*.h")` 把所有 `.h` 安装到系统 include 目录——这印证了「`include/polly/` 即为公开 API」。

**② 测试与单测目录的按需启用。** [CMakeLists.txt:140-143](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/CMakeLists.txt#L140-L143) 中，`test` 总是被加入，而 `unittests` 仅在 `POLLY_GTEST_AVAIL` 为真（即 LLVM 提供了 gtest）时才加入。这也解释了为什么独立构建可能没有单测目标——独立构建开头 `set(POLLY_GTEST_AVAIL 0)`，只有检测到 `llvm_gtest` target 才置 1（见 [CMakeLists.txt:44-47](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/CMakeLists.txt#L44-L47)）。

**③ config.h 的生成。** `include/polly/Config/config.h.cmake` 模板内容非常简短（见 [include/polly/Config/config.h.cmake](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Config/config.h.cmake)），目前只是一个空的 include guard。它是 CMake `configure_file` 的产物入口，未来若需要编译期配置宏（如版本、特性开关），会写在这里再由 [CMakeLists.txt:148-149](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/CMakeLists.txt#L148-L149) 渲染成 `${POLLY_BINARY_DIR}/include/polly/Config/config.h`。

#### 4.2.4 代码实践

1. **实践目标**：建立「头文件 ↔ 实现」的肌肉记忆。
2. **操作步骤**：
   - 在 `include/polly/` 与 `include/polly/` 的子目录下列出所有 `.h`。
   - 对以下 5 个公开头文件，找出它们对应的 `lib/` 实现：
     - `include/polly/DependenceInfo`（依赖分析）
     - `include/polly/Simplify.h`（冗余访问消除）
     - `include/polly/DeLICM.h`
     - `include/polly/CodeGen/IslAst.h`
     - `include/polly/JSONExporter.h`
3. **需要观察的现象**：注意有的头文件在 `include/polly/` 根下，有的在 `include/polly/CodeGen/`、`include/polly/Support/`、`include/polly/Pass/` 子目录下，但实现统一在 `lib/<模块>/`。
4. **预期结果**：你能填出这样一张表——

   | 头文件 | 实现 |
   |--------|------|
   | `include/polly/DependenceInfo.h` | `lib/Analysis/DependenceInfo.cpp` |
   | `include/polly/Simplify.h` | `lib/Transform/Simplify.cpp` |
   | `include/polly/DeLICM.h` | `lib/Transform/DeLICM.cpp` |
   | `include/polly/CodeGen/IslAst.h` | `lib/CodeGen/IslAst.cpp` |
   | `include/polly/JSONExporter.h` | `lib/Exchange/JSONExporter.cpp` |

5. 本实践为纯源码阅读，可在本仓库内直接完成，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `test` 目录总是被加入构建，而 `unittests` 目录有条件加入？
> **答案**：`test` 是基于 `lit` 的回归测试，依赖很轻（只需要已编译的 `opt`/`FileCheck` 等工具），所以默认启用；`unittests` 依赖 gtest 框架，需要 LLVM 构建树提供 `llvm_gtest` target，独立构建时可能拿不到，故用 `POLLY_GTEST_AVAIL` 守卫。

**练习 2**：`include/polly/Config/config.h` 这个文件为什么在源码树里找不到，只能在构建树里找到？
> **答案**：源码树里只有模板 `config.h.cmake`；真正的 `config.h` 是 CMake 在配置阶段用 `configure_file` 把模板渲染到 `${POLLY_BINARY_DIR}/include/polly/Config/config.h` 生成的（见 [CMakeLists.txt:148-149](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/CMakeLists.txt#L148-L149)），属于「生成文件」而非「源文件」。

---

### 4.3 lib 子目录职责地图：从「检测」到「代码生成」

#### 4.3.1 概念说明

`lib/` 是 Polly 真正干活的地方，它按 u1-l1 介绍的那条主线「检测 → 建模 → 依赖 → 变换 → 代码生成」拆成了若干子目录。掌握这张地图，你就能在看到任何一个 Polly pass 名字时，立刻知道它的源码在哪个目录。

`lib/` 下的子目录及其职责：

| 子目录 | 职责 | 代表文件 |
|--------|------|----------|
| `lib/Analysis/` | SCoP 检测与多面体建模、依赖分析 | `ScopDetection.cpp`、`ScopInfo.cpp`、`ScopBuilder.cpp`、`DependenceInfo.cpp` |
| `lib/Transform/` | 循环变换（调度优化、Simplify、DeLICM 等） | `ScheduleOptimizer.cpp`、`Simplify.cpp`、`DeLICM.cpp` |
| `lib/CodeGen/` | IslAst 生成与回写 LLVM-IR、OpenMP 循环生成 | `IslAst.cpp`、`CodeGeneration.cpp`、`BlockGenerators.cpp`、`LoopGeneratorsGOMP.cpp` |
| `lib/Support/` | SCEV↔ISL 转换、ISL 工具、pass 注册、调试辅助 | `SCEVAffinator.cpp`、`GICHelper.cpp`、`RegisterPasses.cpp`、`PollyPasses.def` |
| `lib/Exchange/` | JScop（JSON）导入导出 | `JSONExporter.cpp` |
| `lib/Pass/` | Polly 作为单个 Pass 的封装与阶段流水线编排 | `PhaseManager.cpp`、`PollyFunctionPass.cpp`、`PollyModulePass.cpp` |
| `lib/Plugin/` | LLVM Pass Plugin 入口 | `Polly.cpp` |
| `lib/External/` | 捆绑的第三方库（ISL 及其 imath） | `isl/`、`isl/imath/` |

这里特别要记住三个「路标目录」——它们正好对应 u1-l1 主线上的三个关键阶段：

- **检测** → `lib/Analysis/`（`ScopDetection.cpp`）
- **调度优化** → `lib/Transform/`（`ScheduleOptimizer.cpp`）
- **代码生成** → `lib/CodeGen/`（`CodeGeneration.cpp`）

#### 4.3.2 核心流程

`lib/CMakeLists.txt` 用一个 `add_llvm_pass_plugin(Polly ...)` 把几乎所有 `.cpp` 源文件一次性列出来，编进同一个 `Polly` 目标；再用一个 `add_polly_loadable_module(LLVMPolly ...)` 产出可动态加载的插件。简化后的结构是：

```text
lib/CMakeLists.txt
├── add_subdirectory(External)        # 先编译捆绑 ISL → PollyISL
├── add_llvm_pass_plugin(Polly ...)   # 列出 Analysis/Transform/CodeGen/... 全部 .cpp → 库 Polly
│     └── LINK_COMPONENTS ${POLLY_COMPONENTS}   # 声明依赖的 LLVM 库（Core/Analysis/...）
├── target_link_libraries(Polly PUBLIC ${ISL_TARGET})   # Polly 链接 ISL
└── add_polly_loadable_module(LLVMPolly Plugin/Polly.cpp ...)  # 可加载插件，复用 obj.Polly
```

也就是说：**所有功能源码编进同一个 `Polly` 库**，并不按子目录拆成多个库；子目录只是源码的组织方式，不是库的边界。

#### 4.3.3 源码精读

**① 把全部源码列进 `Polly` 目标。** [lib/CMakeLists.txt:43-100](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/CMakeLists.txt#L43-L100) 这一大段 `add_llvm_pass_plugin(Polly ...)` 按子目录顺序列出了 Analysis、CodeGen、Exchange、Pass、Support、Transform 下的全部 `.cpp`。可以看到「检测」类的 `Analysis/ScopDetection.cpp`、「调度优化」类的 `Transform/ScheduleOptimizer.cpp`、「代码生成」类的 `CodeGen/CodeGeneration.cpp` 都在这里被一起编译。

**② 先编译捆绑 ISL。** [lib/CMakeLists.txt:10](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/CMakeLists.txt#L10) 的 `add_subdirectory(External)` 会触发前面讲过的 `PollyISL` 构建——它必须先就绪，因为 `Polly` 要链接它。

**③ 声明对 LLVM 库的依赖。** [lib/CMakeLists.txt:17-39](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/CMakeLists.txt#L17-L39) 的 `POLLY_COMPONENTS` 列出了 `Core`、`Analysis`、`ScalarOpts`、`TransformUtils`、`Vectorize` 等 LLVM 组件，通过 `LINK_COMPONENTS ${POLLY_COMPONENTS}`（见 [lib/CMakeLists.txt:98-100](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/CMakeLists.txt#L98-L100)）告知 Polly 依赖 LLVM 的哪些库。这正反映了 Polly 的本质——它是一个深度使用 LLVM 分析与变换基础设施的 pass。

**④ 链接 ISL。** [lib/CMakeLists.txt:117-119](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/CMakeLists.txt#L117-L119) 用 `target_link_libraries(Polly PUBLIC ${ISL_TARGET})` 把 Polly 与 ISL 绑定。`PUBLIC` 表示依赖会向下游传递。

**⑤ 可加载插件 `LLVMPolly`。** [lib/CMakeLists.txt:121-144](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/CMakeLists.txt#L121-L144) 用 `add_polly_loadable_module(LLVMPolly Plugin/Polly.cpp $<TARGET_OBJECTS:obj.Polly>)` 产出动态库。注意它复用了 `$<TARGET_OBJECTS:obj.Polly>`——即 `Polly` 库的目标文件，再加上唯一的入口 `Plugin/Polly.cpp`（这是 Pass Plugin 的注册入口，u1-l4 详述）。这样一份源码同时产出「可链接库」和「可加载插件」两种形态。Windows 或未启用 PIC 的情况会退化为一个 dummy target（见 [lib/CMakeLists.txt:123-127](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/CMakeLists.txt#L123-L127)）。

#### 4.3.4 代码实践

这是本讲的**主实践任务**（对应规格要求的练习）。

1. **实践目标**：在 `lib/` 下定位「检测 / 代码生成 / 调度优化」三大功能的子目录与代表文件，并整理出头文件与实现的对应关系表。
2. **操作步骤**：
   - 在本仓库 `polly/` 目录下，分别进入 `lib/Analysis`、`lib/CodeGen`、`lib/Transform` 列出文件。
   - 找到三类功能的代表文件：
     - **检测**：`lib/Analysis/ScopDetection.cpp`（配合 `include/polly/ScopDetection.h`）
     - **代码生成**：`lib/CodeGen/CodeGeneration.cpp`（配合 `include/polly/CodeGen/CodeGeneration.h`）
     - **调度优化**：`lib/Transform/ScheduleOptimizer.cpp`（配合 `include/polly/ScheduleOptimizer.h`）
   - 在 [lib/CMakeLists.txt:43-100](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/CMakeLists.txt#L43-L100) 里确认这三个文件确实都出现在 `add_llvm_pass_plugin` 的源码列表中。
3. **需要观察的现象**：
   - 三个功能分散在三个不同的 `lib/` 子目录，但最终都编进同一个 `Polly` 库。
   - 头文件位置不统一：`ScopDetection.h` 和 `ScheduleOptimizer.h` 在 `include/polly/` 根下，`CodeGeneration.h` 在 `include/polly/CodeGen/` 子目录下。
4. **预期结果**：填写下面的对应关系表（这是本实践的产出物）——

   | 功能 | 子目录 | 实现文件 | 公开头文件 |
   |------|--------|----------|-----------|
   | SCoP 检测 | `lib/Analysis/` | `ScopDetection.cpp` | `include/polly/ScopDetection.h` |
   | 代码生成 | `lib/CodeGen/` | `CodeGeneration.cpp` | `include/polly/CodeGen/CodeGeneration.h` |
   | 调度优化 | `lib/Transform/` | `ScheduleOptimizer.cpp` | `include/polly/ScheduleOptimizer.h` |

5. 本实践为纯源码阅读，无需编译即可在本仓库内完成；如果你本地已构建 Polly，可额外用 `llvm-nm` 或 `llvm-readobj` 查看 `Polly` 库里是否含这三个文件里的符号（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：Polly 的功能按子目录组织，为什么不在 `lib/CMakeLists.txt` 里为每个子目录建一个独立的库？
> **答案**：因为 Polly 内部各模块耦合紧密（例如变换 pass 直接读写 `Scop` 对象，代码生成依赖变换后的调度），且整体只作为「一个 pass 集合」对外暴露。把它们编进单个 `Polly` 库可以简化依赖管理、加快链接，也方便再用同一份目标文件生成可加载插件 `LLVMPolly`（见 `$<TARGET_OBJECTS:obj.Polly>`）。

**练习 2**：`Plugin/Polly.cpp` 这个文件为什么单独出现在 `add_polly_loadable_module(LLVMPolly ...)` 里，而不在 `add_llvm_pass_plugin(Polly ...)` 的列表中？
> **答案**：`Plugin/Polly.cpp` 实现的是 LLVM Pass Plugin 的入口函数（如 `llvmGetPassPluginInfo`），它只在「作为动态库被 `-load` 加载」时才需要；而静态链接进 LLVM 工具时（`LLVM_POLLY_LINK_INTO_TOOLS=ON`）走的是另一套注册路径（`RegisterPasses.cpp`）。因此它只属于插件目标，不属于基础 `Polly` 库。这部分细节会在 u1-l4 详述。

**练习 3**：如果一个新功能「既不属于检测、也不属于代码生成或调度优化」，比如「把 SCoP 信息导出为 JSON」，你会把它的源码放在哪个子目录？为什么？
> **答案**：放在 `lib/Exchange/`。事实上 `JSONExporter.cpp` 就在这里——`Exchange` 子目录专门负责 Polly 与外部（如 JScop 工具）的导入导出交换格式，与「分析/变换/生成」的主线并列。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「构建脚本 → 目录结构 → 功能定位」的完整阅读：

1. **阅读构建脚本**：打开 [CMakeLists.txt:91-102](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/CMakeLists.txt#L91-L102)，向自己解释一遍：默认情况下 Polly 用的是哪种 ISL？为什么？
2. **追踪产物**：打开 [lib/CMakeLists.txt:43-100](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/CMakeLists.txt#L43-L100) 与 [lib/CMakeLists.txt:121-144](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/CMakeLists.txt#L121-L144)，回答：构建后会得到哪两个主要目标？它们各自如何获得同一份 `.cpp` 源码的编译产物？
3. **画出主线地图**：结合 u1-l1 讲的「检测 → 建模 → 依赖 → 变换 → 代码生成」主线，在 `lib/` 下为每个阶段各挑一个代表文件，整理成一张表（阶段 / 子目录 / 文件 / 公开头文件）。
4. **产出物**：一张三列表格——「构建选项轴」「顶层目录职责」「lib 子目录与主线阶段的映射」。完成后，你应该能凭这张表，在 Polly 源码里「按图索骥」而不迷路。

> 提示：这一步不需要真正编译。如果你后续要本地构建，标准做法是在 LLVM 构建中用 `-DLLVM_ENABLE_PROJECTS=polly` 把 Polly 一起带上（具体命令随 LLVM 版本和平台而异，**待本地验证**）。

## 6. 本讲小结

- Polly 的构建有两条正交的选择轴：**构建模式**（树内 `in-tree` / 独立 `standalone`，由 `LLVM_MAIN_SRC_DIR` 是否定义来区分）与 **ISL 来源**（捆绑 `PollyISL` / 外部 `ISL`，由 `POLLY_BUNDLED_ISL` 开关控制）。
- `ISL_TARGET` 是一个抽象别名，让上层脚本无需关心 ISL 来自哪里，是来源切换对上层透明的关键设计。
- Polly 遵循 LLVM 子项目的标准目录约定：`include/polly/` 放公开头文件、`lib/` 放实现、`test/` 放 lit 回归测试、`unittests/` 放 gtest 单测（后者仅在 `POLLY_GTEST_AVAIL` 为真时启用）。
- `lib/` 内部按功能分子目录，三大路标是：检测 `lib/Analysis/`、调度优化 `lib/Transform/`、代码生成 `lib/CodeGen/`。
- 所有 `lib/` 下的 `.cpp` 都被 [lib/CMakeLists.txt:43-100](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/CMakeLists.txt#L43-L100) 编进同一个 `Polly` 库，子目录只是源码组织方式，不是库的边界。
- 同一份目标文件同时产出「可链接库 `Polly`」和「可加载插件 `LLVMPolly`」（入口为 `Plugin/Polly.cpp`），实现了两种打包形态。

## 7. 下一步学习建议

- 下一讲 **u1-l3「通过 clang 与 opt 使用 Polly」** 会让你真正跑起来 Polly——学完本讲的目录结构后，你将知道那些 `-mllvm -polly` 开关背后调用的源码分别在哪个子目录。
- 如果你想提前感受「Polly 是怎么挂进 LLVM 的」，可以先扫一眼 [lib/Plugin/Polly.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Plugin/Polly.cpp) 和 [lib/Support/RegisterPasses.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp)，这正是 **u1-l4「插件入口与 LLVM Pass 注册」** 的主题。
- 想了解构建产出的两个目标如何对应到 Polly 内部的阶段流水线，可预习 [lib/Pass/PhaseManager.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp)，这是 **u2-l1** 的核心。
