# 仓库结构与 CMake 构建系统

## 1. 本讲目标

在上一讲里，我们已经从「俯瞰」的角度知道了 CUDA Tile IR 是什么、由哪四大组件构成（Dialect / Python Bindings / Bytecode / Conformance Test Suite）。本讲要回答一个更落地的问题：**这套东西在磁盘上长什么样，又怎么把它编译出来。**

学完本讲，你应当能够：

1. 看懂 CUDA Tile 仓库的顶层目录布局，说出 `include` / `lib` / `tools` / `python` / `test` / `cmake` 各自承担什么职责。
2. 读懂顶层 `CMakeLists.txt`，理解项目如何声明 C++ 标准、如何决定构建类型（Release/Debug 等）、以及哪些 `option(...)` 开关控制功能开关。
3. 理解 LLVM/MLIR 配置块：项目为什么必须锁定到具体的 LLVM commit，以及「自动下载 / 本地源码 / 预编译库」三种获取 LLVM 的方式对应哪些 CMake 变量。
4. 理解子目录注册块的顺序，尤其是 **为什么 `cuda-tile-tblgen` 必须最先构建**，以及 `include/lib/test/tools/python` 的注册顺序与依赖关系。
5. 亲手用 README 里的 Quick Start 命令配置并构建一个带 Python 绑定与测试的 Release 版本，并运行 `check-cuda-tile`。

## 2. 前置知识

本讲面向「会敲命令行、但可能没读过大型 C++ 项目构建脚本」的读者。在进入源码前，先用通俗语言铺垫三个概念。

### 2.1 什么是 CMake

C++ 项目源码本身不能直接运行，需要经过「编译 → 链接」变成可执行文件或库。**CMake** 是一个「构建系统的生成器」：你写一份 `CMakeLists.txt` 描述「项目由哪些源文件组成、依赖哪些库、要开哪些功能开关」，CMake 再据此生成真正执行编译的具体命令（比如 Ninja、Makefile、Visual Studio 工程）。

CUDA Tile 用的是 **Ninja** 生成器（命令行 `-G Ninja`），它比传统 Make 并行更快，适合这种源码量大的项目。

几个关键术语：

- **目标（target）**：一个要构建的产物，可以是一个库（如 `CudaTileDialect`）或一个可执行文件（如 `cuda-tile-tblgen`）。`add_subdirectory` 会把另一个目录里的 `CMakeLists.txt` 也纳入构建，从而引入新的 target。
- **option**：CMake 里的「布尔开关」，语法是 `option(NAME "描述" 默认值)`。命令行用 `-DNAME=ON/OFF` 覆盖默认值。CUDA Tile 用它来控制「要不要测试、要不要 Python 绑定、要不要 C API」等。
- **find_package**：让 CMake 去系统里找一个已经装好的第三方库（这里是 MLIR），拿到它的头文件路径和库路径。

### 2.2 MLIR / LLVM 是什么，为什么要锁定 commit

上一讲提到 CUDA Tile 是「基于 MLIR」的。**LLVM** 是一个著名的编译器基础设施，**MLIR**（Multi-Level Intermediate Representation）是 LLVM 子项目，提供了一套「构造编译器中间表示」的框架，让你可以用「方言（dialect）」的方式定义自己的 IR。CUDA Tile 的 `cuda_tile` 方言就是用 MLIR 写出来的。

问题是：LLVM/MLIR 上游一直在演进，经常会引入不兼容的改动（breaking change）。CUDA Tile 的代码依赖 MLIR 的具体 C++ 接口，如果上游改了接口，CUDA Tile 就编不过。所以项目必须在 `cmake/IncludeLLVM.cmake` 里**把 LLVM 锁定到一个具体的 commit hash**，并随上游变化不断发布修复版本——这正是上一讲提到的「兼容区间」机制在构建层面的体现。

### 2.3 TableGen 与代码生成（先建立直觉）

MLIR 用一种叫 **TableGen** 的领域特定语言（`.td` 文件）来声明方言里的操作、类型、属性，然后由一个「代码生成工具」把这些声明翻译成大量 C++ 胶水代码（`.h.inc` / `.cpp.inc`）。CUDA Tile 自己实现了一个增强版的代码生成工具，名字叫 **`cuda-tile-tblgen`**。

这就能解释本讲反复强调的一点：**`cuda-tile-tblgen` 必须最先构建**。因为编译 `lib/` 和 `include/` 里的 C++ 源码时，需要先由 `cuda-tile-tblgen` 跑一遍，生成那些 `.inc` 文件，C++ 源码 `#include` 它们才能编过。换句话说，`cuda-tile-tblgen` 是「构造其余产物的工具」，是构建链最前端的一环。详细的代码生成机制会在 u2-l3 讲义里展开，本讲你只要记住这个先后顺序即可。

## 3. 本讲源码地图

本讲只盯住「构建骨架」，涉及的文件不多但都很关键。下面这张表把顶层目录和关键文件的作用一次列清。

| 路径 | 类型 | 作用 |
|------|------|------|
| `CMakeLists.txt` | 顶层构建脚本 | 项目入口：声明项目名、C++ 标准、构建类型、所有功能开关，配置 LLVM/MLIR，注册子目录 |
| `cmake/IncludeLLVM.cmake` | CMake 宏库 | 锁定 LLVM commit hash，提供三种获取 LLVM 的宏（自动下载 / 本地源码 / 预编译库） |
| `cmake/IncludeCudaTileUtils.cmake` | CMake 宏库 | `set_cuda_tile_build_type()` 宏：校验并默认化 `CMAKE_BUILD_TYPE` |
| `cmake/IncludeCompilerChecks.cmake` | CMake 宏库 | 编译器特性检查 |
| `README.md` | 文档 | 给出 Quick Start 命令、三种 LLVM 获取方式、测试与集成说明 |
| `include/` | 源码：头文件 | 方言、字节码、属性、接口的 C++ 声明（`.h` / `.td`），以及对外 C API 头 `include/cuda_tile-c/` |
| `lib/` | 源码：实现 | 方言实现 `lib/Dialect`、字节码读写 `lib/Bytecode`、C API 包装 `lib/CAPI` |
| `tools/` | 源码：可执行工具 | `cuda-tile-tblgen`（代码生成）、`cuda-tile-translate`（MLIR↔字节码）、`cuda-tile-opt` / `cuda-tile-optimize`（优化） |
| `python/` | 源码：Python 绑定 | 基于 nanobind 的 C++ 绑定 `SiteInitializer.cpp`、`Dialect/`，以及高层 Python 包 `cuda_tile/` |
| `test/` | 源码：测试 | lit/FileCheck 测试套件（`Bytecode` / `Dialect` / `Transforms` / `CAPI` / `python`） |
| `scripts/` | 辅助脚本 | `get-cuda-tile-version-for-llvm-hash.sh`：根据 LLVM commit 反查应使用的 CUDA Tile 版本 |

一个重要的对应关系（承接上一讲的四大组件）：

- **CUDA Tile Dialect** → `include/cuda_tile/Dialect/` + `lib/Dialect/`
- **Bytecode** → `include/cuda_tile/Bytecode/` + `lib/Bytecode/`
- **Python Bindings** → `python/`
- **Conformance Test Suite** → `test/`

## 4. 核心概念与源码讲解

本讲按三个最小模块拆解：① 顶层 `CMakeLists.txt` 的骨架与构建设置；② LLVM/MLIR 配置块；③ 子目录注册块与构建顺序。

### 4.1 顶层 CMakeLists.txt 的骨架与构建设置

#### 4.1.1 概念说明

一个 C++ 项目的顶层 `CMakeLists.txt` 通常要做四件事：声明项目元信息（名字、语言、标准）、设定构建类型、定义功能开关（`option`）、再把这些信息组织起来驱动后续编译。CUDA Tile 的顶层脚本正是这个套路，只是多了一套「锁定 LLVM」的逻辑（见 4.2）。

理解这一段的关键，是分清两类开关：

- **构建类型（build type）**：决定编译器优化等级与是否带调试信息，例如 `Release`（优化、无调试信息）、`Debug`（带断言、带调试信息）。
- **功能开关（option）**：决定「要不要把某个组件编进来」，例如要不要测试、要不要 Python 绑定。这类开关默认值的设计很有讲究——**默认 OFF 的功能在最小构建下不会被拉进来**，从而让快速体验构建尽可能轻量。

#### 4.1.2 核心流程

顶层脚本的前半段执行顺序如下（伪代码）：

```
cmake_minimum_required(3.20)
project(CUDA_TILE)                  # 声明项目，启用 C/C++ 语言
set(CMAKE_CXX_STANDARD 17)          # 强制 C++17
include(编译器检查 / 工具宏)
set_cuda_tile_build_type()          # 默认/校验构建类型
定义一组 option 开关                  # TESTING / CCACHE / BUILD_IN_LLVM ...
（可选）配置 ccache
设置位置无关代码 PIC、Windows /bigobj
---- 进入 LLVM/MLIR 配置块（见 4.2）----
定义 CAPI / TOOLS / BINDINGS_PYTHON option
（可选）配置 Python 开发包
include LLVM/MLIR 头文件目录
---- 进入子目录注册块（见 4.3）----
```

#### 4.1.3 源码精读

项目入口与 C++ 标准声明。这里要求 CMake ≥ 3.20，项目名 `CUDA_TILE`，并强制使用 C++17（`CMAKE_CXX_STANDARD_REQUIRED` 保证不能用降级的标准编译）：

[CMakeLists.txt:4-9](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L4-L9) — 声明 CMake 最低版本、项目名与 C++17 标准。

随后脚本 include 两个工具宏库，并调用 `set_cuda_tile_build_type()` 来处理构建类型：

[CMakeLists.txt:11-14](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L11-L14) — 引入编译器检查与 CUDA Tile 工具宏，并设置构建类型。

构建类型的默认/校验逻辑在工具宏库里。如果用户没指定 `CMAKE_BUILD_TYPE`，就默认 `Release`；如果指定了不支持的值，就报错列出可选项：

[cmake/IncludeCudaTileUtils.cmake:7-28](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeCudaTileUtils.cmake#L7-L28) — `set_cuda_tile_build_type` 宏：默认 Release，校验只允许 Release/Debug/RelWithDebInfo/MinSizeRel。

接下来是第一批功能开关。注意这三个默认都是 `OFF`：

[CMakeLists.txt:22-24](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L22-L24) — 三个 option：测试、ccache、是否作为 LLVM 外部项目构建。

ccache 是一个 C/C++ 编译缓存工具，能大幅加快重复构建。开启时脚本会去找 `ccache` 可执行文件，并把它设为编译器的前置启动器：

[CMakeLists.txt:33-42](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L33-L42) — 开启 ccache 时把它挂到编译器 launcher 上。

> 注意第 56 行有一处 `add_definitions(-DUSE_12_9_COMPATIBLE_LLVM=0)`，注释明确写着 `TEMPORARY HACKS!`。这是项目历史遗留的临时编译期开关，阅读时知道它存在即可，不要在新代码里依赖它。

#### 4.1.4 代码实践

**目标**：用只读方式感受构建类型默认值与 option 的作用，不真正编译。

**操作步骤**：

1. 打开 `cmake/IncludeCudaTileUtils.cmake`，确认 `DEFAULT_BUILD_TYPE` 的值。
2. 打开 `CMakeLists.txt`，数一下第 22-24、96-97、104 行一共定义了几个 `option(...)`，分别记下它们的默认值（ON 还是 OFF）。
3. 在 README 的 Quick Start 命令里，找出哪几个 `-D` 旗标是用来翻转这些 option 默认值的。

**需要观察的现象**：你会看到 TESTING / CCACHE / BINDINGS_PYTHON 默认都是 `OFF`，而 CAPI / TOOLS 默认是 `ON`。这说明「最小可用构建」默认就会编出方言库和工具，但不会自动开测试和 Python 绑定——后者需要你显式打开。

**预期结果**：你能口头回答「为什么 README 的 Quick Start 要显式加 `-DCUDA_TILE_ENABLE_BINDINGS_PYTHON=ON -DCUDA_TILE_ENABLE_TESTING=ON`」——因为这两个默认是关的。

**待本地验证**：如果环境允许，可执行 `cmake -G Ninja -S . -B build-only-check -DCMAKE_BUILD_TYPE=Release`（不开任何额外 option），观察 CMake 配置阶段打印的 `CUDA Tile testing: OFF`、`CUDA Tile Python bindings: OFF` 等状态行，验证上面的判断。

#### 4.1.5 小练习与答案

**练习 1**：如果不传 `-DCMAKE_BUILD_TYPE=...`，CUDA Tile 会用什么构建类型？依据是哪一行？

**参考答案**：会用 `Release`。依据是 `cmake/IncludeCudaTileUtils.cmake` 中 `DEFAULT_BUILD_TYPE` 设为 `"Release"`，当 `CMAKE_BUILD_TYPE` 与 `CMAKE_CONFIGURATION_TYPES` 都未设置时强制默认为 Release。

**练习 2**：把 `-DCMAKE_BUILD_TYPE=Fast` 传给 cmake 会发生什么？

**参考答案**：配置阶段直接 `FATAL_ERROR` 退出，并列出仅允许的四种构建类型（Release / Debug / RelWithDebInfo / MinSizeRel），因为 `set_cuda_tile_build_type` 会校验 `CMAKE_BUILD_TYPE` 是否在白名单里。

---

### 4.2 LLVM/MLIR 配置块

#### 4.2.1 概念说明

CUDA Tile 是「宿主项目」，它依赖「客人」MLIR/LLVM。配置块要解决三件事：

1. **拿到一份 LLVM/MLIR**：可以是让 CMake 自动从 GitHub 下载并编译，也可以用你机器上已有的 LLVM 源码或预编译库。
2. **保证 commit 兼容**：必须用项目锁定的那个 commit，否则接口对不上。
3. **让后续编译能找到 MLIR 的头文件和 CMake 模块**：通过 `find_package(MLIR)` 和 `include_directories` 完成。

理解这一块的最大收益，是看懂「为什么有时候构建特别慢」——默认方式会**把整个 LLVM/MLIR 从源码编译一遍**，这是首次构建耗时的主因（通常以小时计）。如果你已经在别处编过 LLVM，用预编译库方式会快得多。

#### 4.2.2 核心流程

配置块的决策树（伪代码）：

```
if (CUDA_TILE_BUILD_IN_LLVM)            # 特殊模式：作为 LLVM 内部外部项目，跳过本块
    # 由 LLVM 主构建接管，不做独立 LLVM 配置
else
    include(cmake/IncludeLLVM.cmake)     # 引入锁定 commit 与三个配置宏
    互斥检查：INSTALL_DIR 与 SOURCE_DIR 不能同时设
    if (CUDA_TILE_USE_LLVM_INSTALL_DIR)
        configure_pre_installed_llvm()   # 方式 3：预编译库
    else
        configure_llvm_from_sources()    # 方式 1/2：下载或本地源码
    find_package(MLIR REQUIRED ...)      # 找到 MLIR 的 CMake 配置
    include(TableGen / AddLLVM / AddMLIR)
    print_llvm_config()                  # 打印 LLVM/MLIR 环境摘要
```

注意「方式 1（自动下载）」和「方式 2（本地源码）」走的是同一个宏 `configure_llvm_from_sources()`，区别只在于源码是 CMake 帮你下载，还是你用 `CUDA_TILE_USE_LLVM_SOURCE_DIR` 指定本地路径。

锁定的 LLVM commit 写在工具宏库里：

[cmake/IncludeLLVM.cmake:28-30](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake#L28-L30) — `LLVM_BUILD_COMMIT_HASH` 锁定到具体 commit，自动下载时用这个 tag 拉 llvm-project。

> 本 HEAD 对应的锁定 commit 是 `57109befac92811d2253109242ca6fa69c961fb2`（即近期提交信息 `[LLVM-FIX] Breaking commit 57109befac92` 所指的那次上游 breaking）。这正是上一讲「兼容区间」机制在构建层落地的地方。

#### 4.2.3 源码精读

整个 LLVM/MLIR 配置块被包在 `if(NOT CUDA_TILE_BUILD_IN_LLVM)` 里。普通用户构建时这个条件为真，会执行 include + 互斥检查 + 分派：

[CMakeLists.txt:61-78](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L61-L78) — 引入 `IncludeLLVM.cmake`，校验 INSTALL_DIR 与 SOURCE_DIR 互斥，二选一调用预编译库或源码构建宏。

随后脚本把 MLIR 的 CMake 目录加入模块搜索路径，并 `find_package(MLIR)`。这一步把 MLIR 的头文件目录、库、CMake 辅助宏都暴露给 CUDA Tile：

[CMakeLists.txt:80-93](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L80-L93) — 设定模块路径、`find_package(MLIR)`，并引入 TableGen/AddLLVM/AddMLIR 三个 LLVM/MLIR 辅助模块。

LLVM 配置之后才定义 C API 和工具的两个开关，默认都是 `ON`：

[CMakeLists.txt:95-100](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L95-L100) — `CUDA_TILE_ENABLE_CAPI`、`CUDA_TILE_ENABLE_TOOLS` 默认 ON。

Python 绑定的开关定义更靠后，并且**必须在 `find_package(MLIR)` 之后**——因为脚本要检查拿到的 MLIR 是否本身开启了 Python 绑定（`MLIR_ENABLE_BINDINGS_PYTHON`）。如果你用预编译库方式，必须保证那份库当初是用 `-DMLIR_ENABLE_BINDINGS_PYTHON=ON` 编的，否则这里会 `FATAL_ERROR`：

[CMakeLists.txt:102-118](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L102-L118) — Python 绑定开关与对 MLIR Python 绑定的依赖校验。

最后把 LLVM/MLIR 的头文件目录加入全局 include 路径，后续所有 C++ 编译都能找到 MLIR 头文件：

[CMakeLists.txt:120-122](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L120-L122) — 把 `LLVM_INCLUDE_DIRS` / `MLIR_INCLUDE_DIRS` 加入 include 路径。

README 里把三种 LLVM 获取方式讲得很清楚，可作为本模块的对照阅读材料：自动下载（默认、最慢）、本地源码（`CUDA_TILE_USE_LLVM_SOURCE_DIR`）、预编译库（`CUDA_TILE_USE_LLVM_INSTALL_DIR`）。

#### 4.2.4 代码实践

**目标**：弄清三种 LLVM 获取方式分别对应哪个 CMake 变量，并定位锁定的 commit。

**操作步骤**：

1. 在 `cmake/IncludeLLVM.cmake` 中找到 `LLVM_BUILD_COMMIT_HASH` 那一行，记录它锁定的 commit。
2. 对照 README「Build Configuration Options → MLIR/LLVM Build Configuration」一节，把「自动下载 / 本地源码 / 预编译库」三段示例命令里的关键 `-D` 变量分别摘出来。
3. 解释：为什么 README 强调使用预编译库时，「库的 commit hash 必须与 `IncludeLLVM.cmake` 指定的兼容」？

**需要观察的现象**：三种方式的差异只在「源码从哪来 / 要不要现编 LLVM」，CUDA Tile 自身的代码与功能开关完全一致。

**预期结果**：你能写出一张三行的小表——方式 1（默认，无额外变量）、方式 2（`-DCUDA_TILE_USE_LLVM_SOURCE_DIR=...`）、方式 3（`-DCUDA_TILE_USE_LLVM_INSTALL_DIR=...`）。

**待本地验证**：实际下载并编译 LLVM 耗时极长，本步骤以源码阅读为准；如确需构建，建议优先用方式 3（预编译库）以节省时间。

#### 4.2.5 小练习与答案

**练习 1**：同时设置 `CUDA_TILE_USE_LLVM_INSTALL_DIR` 和 `CUDA_TILE_USE_LLVM_SOURCE_DIR` 会怎样？

**参考答案**：配置阶段 `FATAL_ERROR`，提示「二者只能设其一，不能同时设」。见 `CMakeLists.txt` 第 66-70 行的互斥检查。

**练习 2**：为什么 `CUDA_TILE_ENABLE_BINDINGS_PYTHON` 的校验逻辑放在 `find_package(MLIR)` 之后？

**参考答案**：因为校验依赖 `MLIR_ENABLE_BINDINGS_PYTHON` 这个 MLIR 侧变量，而该变量只有在 `find_package(MLIR)` 之后才会被正确填充。脚本注释也提示：如果校验失败且你确实事先打开了 MLIR 的 Python 绑定，多半是 `find_package(MLIR)` 抓到了错误位置的 MLIR。

---

### 4.3 子目录注册块与构建顺序

#### 4.3.1 概念说明

`add_subdirectory(dir)` 的作用是「把 `dir/CMakeLists.txt` 也纳入构建」。对一个分模块的大项目，顶层脚本通常用一连串 `add_subdirectory` 把各模块挂进来。这里最关键的设计是 **注册顺序**——因为模块之间存在依赖，顺序错了就编不过。

CUDA Tile 的核心依赖链是：

```
cuda-tile-tblgen (代码生成工具)
        │  生成 .inc 文件
        ▼
include / lib  (C++ 源码 #include 那些 .inc)
        │
        ▼
tools / python / test  (依赖 lib 里的库)
```

所以 `cuda-tile-tblgen` 必须第一个挂进来，单独写在 `tools/cuda-tile-tblgen`，先于 `lib` / `include`。这条顺序是理解整个构建的「钥匙」。

#### 4.3.2 核心流程

子目录注册块的执行顺序（伪代码）：

```
add_subdirectory(tools/cuda-tile-tblgen)      # 第 1 步：必须最先
解析出 CUDA_TILE_TABLEGEN_EXE 的完整路径      # 给后续 TableGen 调用用
(若开启测试) 给 TableGen 与 C++ 加 -DTILE_IR_INCLUDE_TESTS
add_subdirectory(include)                      # 第 2 步：头文件 + .td → .inc
add_subdirectory(lib)                          # 第 3 步：方言/字节码/C API 实现
if (CUDA_TILE_ENABLE_TESTING)  add_subdirectory(test)
if (CUDA_TILE_ENABLE_TOOLS)    add_subdirectory(tools)     # 其余工具（translate/opt/optimize）
if (CUDA_TILE_ENABLE_BINDINGS_PYTHON) add_subdirectory(python)
install(头文件)
```

注意一个细节：`tools` 里的 `cuda-tile-tblgen` 被单独提前注册，而 `tools/` 整体（含 `cuda-tile-translate`、`cuda-tile-opt`、`cuda-tile-optimize`）在后面才注册。也就是说同一个 `tools/` 目录被分两次挂入：先挂它的 `tblgen` 子目录，后挂整个目录。这是因为 tblgen 是「前置依赖」，其余工具是「后置产物」。

还有一段「测试标志传播」逻辑：当 `CUDA_TILE_ENABLE_TESTING=ON` 时，会给 TableGen 和 C++ 编译都加上 `TILE_IR_INCLUDE_TESTS` 宏定义，从而让 `.td` 与 C++ 在测试构建下多生成一些「仅测试用」的操作/代码。这就是为什么很多测试用例（如 `testing` 组操作）只有在开启测试时才存在。

#### 4.3.3 源码精读

先把 `cuda-tile-tblgen` 单独提前注册。注释说得很明白：它是构建流程的一部分，必须先于其它一切构建：

[CMakeLists.txt:167-169](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L167-L169) — 注释强调 `cuda-tile-tblgen` 必须最先构建，随后 `add_subdirectory(tools/cuda-tile-tblgen)`。

接下来解析出 host 上 `cuda-tile-tblgen` 可执行文件的完整路径，存到 `CUDA_TILE_TABLEGEN_EXE`，后续 `lib`/`include` 里的 TableGen 规则就能调用它生成 `.inc`。注意它还区分了交叉编译场景（跨平台构建时要用本机 native 工具）：

[CMakeLists.txt:171-181](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L171-L181) — 设置 `CUDA_TILE_TABLEGEN_EXE` 完整路径，交叉编译时改取 native 工具路径。

测试标志的传播——同时写入 TableGen 旗标和 C++ 编译宏：

[CMakeLists.txt:183-187](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L183-L187) — 开启测试时给 TableGen 与 C++ 都加 `TILE_IR_INCLUDE_TESTS`，用于条件化生成测试专用操作。

核心源码子目录注册，顺序为 `include` → `lib`，再按开关注册 `test` / `tools` / `python`：

[CMakeLists.txt:189-201](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L189-L201) — 注册 include、lib，并按 TESTING/TOOLS/BINDINGS_PYTHON 开关条件注册 test/tools/python。

最后是头文件安装规则，把 `include/` 下的 `.def` / `.h` / `.inc` / `.td` 文件安装到 `CUDA_TILE_INSTALL_DIR/include`，供「Option 1：使用预编译库」方式集成时取用：

[CMakeLists.txt:203-215](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L203-L215) — 安装头文件，匹配 `.def/.h/.inc/.td`。

#### 4.3.4 代码实践

**目标**：用「源码阅读 + 解释」的方式验证构建顺序的合理性，不真正编译。

**操作步骤**：

1. 在顶层 `CMakeLists.txt` 中数出所有 `add_subdirectory(...)` 调用，按出现顺序列出。
2. 解释：为什么 `add_subdirectory(tools/cuda-tile-tblgen)` 出现在 `add_subdirectory(include)` 之前？
3. 解释：为什么 `add_subdirectory(test)` 被包在 `if(CUDA_TILE_ENABLE_TESTING)` 里，而 `add_subdirectory(include)` 和 `add_subdirectory(lib)` 没有任何条件包裹？

**需要观察的现象**：你会看到「无条件的核心源码目录（include/lib）」与「有条件的外围目录（test/tools/python）」的清晰分层；`tblgen` 作为特例被无条件提前。

**预期结果**：你能画出一张从 `cuda-tile-tblgen` 到 `include/lib` 再到 `tools/python/test` 的有向依赖图，并说明为什么 `tblgen` 必须第一个。

**待本地验证**：若环境允许完整构建，可在构建后到 `build/bin/` 查看 `cuda-tile-tblgen` 是否早于其它工具产物生成；或在 `build/include/.../` 下确认存在由 tblgen 生成的 `*.inc` 文件。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `add_subdirectory(tools/cuda-tile-tblgen)` 移到 `add_subdirectory(lib)` 之后，会发生什么？

**参考答案**：`lib` 的 C++ 源码在编译时会找不到由 `cuda-tile-tblgen` 生成的 `.inc` 文件（因为它们还没被生成），导致 `#include` 失败、构建失败。这正是 tblgen 必须最先注册的原因。

**练习 2**：为什么 `test` 目录的注册有 `if(CUDA_TILE_ENABLE_TESTING)` 保护，而 `tools` 目录用 `if(CUDA_TILE_ENABLE_TOOLS)` 保护？

**参考答案**：因为测试和工具都是「可选组件」，由对应的 option 控制；用户可以选择只构建核心库（方言 + 字节码）而不编测试或工具，从而缩短构建时间和减小产物体积。这两个 option 默认分别是 OFF 和 ON，所以最小快速构建仍会带上工具但不带测试。

## 5. 综合实践

把本讲三个模块串起来：执行一次「带 Python 绑定与测试的 Release 构建」并解释每一步背后对应顶层脚本的哪一段。

**任务**：

1. 在仓库根目录执行 README 的 Quick Start 配置命令：

   ```bash
   cmake -G Ninja -S . -B build \
     -DCMAKE_BUILD_TYPE=Release \
     -DLLVM_ENABLE_ASSERTIONS=OFF \
     -DCUDA_TILE_ENABLE_BINDINGS_PYTHON=ON \
     -DCUDA_TILE_ENABLE_TESTING=ON
   ```

2. 在 CMake 配置阶段打印的大量 `-- ...` 状态行里，定位并记录以下信息（它们分别对应顶层脚本里哪一条 `message(STATUS ...)`）：
   - `CUDA Tile testing:` 的值
   - `CUDA Tile Python bindings:` 的值
   - `CUDA_TILE_ENABLE_CAPI:` 的值
   - `CUDA_TILE_TABLEGEN_EXE:` 的路径
   - `MLIR_ENABLE_BINDINGS_PYTHON:` 的值

3. 执行构建：

   ```bash
   cmake --build build
   ```

4. 运行测试套件并记录通过数量：

   ```bash
   cmake --build build --target check-cuda-tile
   ```

**需要观察的现象**：

- 首次构建会先编译 LLVM/MLIR（因为默认走「自动下载」方式），耗时很长——对应 4.2 讲的「方式 1 最慢」。
- `build/bin/` 下会先出现 `cuda-tile-tblgen`，随后才是 `cuda-tile-translate`、`cuda-tile-opt` 等——对应 4.3 讲的注册顺序。
- `check-cuda-tile` 会用 LLVM 的 lit 框架跑 `test/` 下的用例，最后打印类似 `Testing: N tests, M failures` 的汇总。

**预期结果**：配置阶段状态行显示 `testing: ON`、`Python bindings: ON`、`CAPI: ON`；构建产物齐全；`check-cuda-tile` 全部通过（`M = 0`）。把你观察到的测试总数 `N` 记下来，作为后续讲义（u10-l3 测试基础设施）的参照基线。

**待本地验证**：完整构建依赖网络下载 LLVM 与充足的编译资源，本实践无法在不能联网或资源受限的环境完成。若环境不具备，请退回到各模块的「源码阅读型实践」，重点把三个 `add_subdirectory` 的顺序与 LLVM commit 锁定讲清楚即可。

## 6. 本讲小结

- CUDA Tile 顶层目录分工清晰：`include/lib` 是核心源码（方言与字节码），`tools` 是命令行工具，`python` 是绑定，`test` 是测试，`cmake` 是构建辅助宏库——它们与上一讲的四大组件一一对应。
- 顶层 `CMakeLists.txt` 用 `set_cuda_tile_build_type()` 把构建类型默认为 `Release` 并做白名单校验；功能开关靠 `option(...)` 控制，其中 TESTING / BINDINGS_PYTHON 默认 OFF，CAPI / TOOLS 默认 ON。
- LLVM/MLIR 配置块是首次构建耗时的主因：默认「自动下载并编译」锁定 commit，也支持本地源码（`CUDA_TILE_USE_LLVM_SOURCE_DIR`）和预编译库（`CUDA_TILE_USE_LLVM_INSTALL_DIR`）两种加速方式，二者互斥。
- Python 绑定的校验必须放在 `find_package(MLIR)` 之后，因为它依赖 MLIR 侧的 `MLIR_ENABLE_BINDINGS_PYTHON`。
- 子目录注册顺序是构建的关键：`cuda-tile-tblgen` 必须最先（它生成 `.inc` 胶水代码），然后才是 `include/lib`，最后按开关注册 `test/tools/python`。
- 开启测试时 `TILE_IR_INCLUDE_TESTS` 宏会同时传给 TableGen 与 C++，用于条件化生成仅测试用的操作。

## 7. 下一步学习建议

本讲把「构建骨架」讲清楚了，但还有两个自然延伸的方向：

1. **如果你想真正跑通一个内核**：直接进入 u1-l3（工具链与端到端示例：从 MLIR 到 cubin），它会用 `cuda-tile-translate` 和 `tileiras` 把一段 Tile IR 从文本跑到 GPU 输出。本讲你建立的「tblgen 先于一切、lib 依赖 tblgen」的心智模型，能帮你理解那条工具链里各工具的来源。
2. **如果你更关心 LLVM 依赖的细节**：进入 u2-l1（MLIR/LLVM 依赖与构建配置），它会深入 `cmake/IncludeLLVM.cmake` 的三种配置宏内部，并讲解跨编译、兼容区间脚本 `scripts/get-cuda-tile-version-for-llvm-hash.sh` 的用法。

后续当你开始读 `include/cuda_tile/Dialect/` 里的 `.td` 文件时（u2-l2 起），记得回过头来对照本讲的 4.3：你会真切看到「这些 `.td` 是怎么被 `cuda-tile-tblgen` 变成 `.inc`、又被 `lib/` 里的 C++ `#include` 进去的」——本讲埋下的这条线届时会闭合。
