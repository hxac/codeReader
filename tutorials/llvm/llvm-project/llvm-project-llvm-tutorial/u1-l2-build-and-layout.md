# 构建系统与目录结构

## 1. 本讲目标

本讲承接 [u1-l1 项目总览与定位](u1-l1-project-overview.md)，把"地图"从概念落到地面：LLVM 这套庞大工具箱，到底用什么方式从几万个 `.cpp` 文件变成可执行的 `opt`、`llc`、`lli`？

学完本讲你应当能够：

- 说清 CMake 在 LLVM 中扮演的角色，以及"配置（configure）→ 构建（build）→ 安装（install）"三步分别做什么。
- 看懂顶层 [`CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt) 如何用 `add_subdirectory` 把 `lib/`、`include/`、`tools/`、`examples/`、`test/` 串成一棵构建树。
- 掌握最常用的几个构建变量（`CMAKE_BUILD_TYPE`、`LLVM_TARGETS_TO_BUILD`、`LLVM_ENABLE_PROJECTS`、`LLVM_ENABLE_ASSERTIONS` 等）和 `CMakePresets.json` 预设的用法。
- 在本地跑通一次最小 CMake 配置，并能预测产物（库与可执行文件）会落在构建目录的哪个子路径下。

本讲只讲"怎么把它建起来、目录怎么组织"，**不**深入任何 C++ 代码逻辑——那是后续 IR、Pass、后端各讲的任务。

## 2. 前置知识

在开始前，先用最简单的话建立几个概念。如果你已经熟悉，可以跳过本节。

- **源码树（source tree）与构建树（build tree）**：你下载下来的源代码目录叫"源码树"，里面全是 `.cpp`/`.h`/`CMakeLists.txt`。CMake 生成的中间文件（`.o`、`.a`、可执行文件、`CMakeCache.txt`）应该放进一个单独的"构建树"目录。**LLVM 不允许在源码树里直接构建**（俗称 in-source build），后面我们会看到源码里专门有一段代码拦截这种行为。
- **CMake 是"构建生成器"而不是"构建器"**：CMake 自己不编译任何东西。它读你的 `CMakeLists.txt`，然后为 Ninja、GNU make、Visual Studio、Xcode 等真正的构建工具生成对应文件（如 `build.ninja`、`Makefile`）。你最终是调用 Ninja/make 去编译的。LLVM 官方推荐 **Ninja**，因为它快、且支持并发链接限流。
- **目标（target）**：在 CMake 里，一个"目标"可以是一个可执行文件（如 `opt`）、一个静态库（如 `LLVMCore`）、或一个自定义任务（如 `install`）。`add_llvm_tool(opt ...)` 就定义了一个可执行文件目标。
- **构建类型（CMAKE_BUILD_TYPE）**：单配置生成器（Ninja、Make）用这个变量决定优化与调试信息的组合，常见取值是 `Debug`、`Release`、`RelWithDebInfo`、`MinSizeRel`。LLVM 在不指定时默认 `Release`。
- **组件（component）**：LLVM 把功能拆成大量小组件库（`Core`、`Support`、`Analysis`、`CodeGen`、`X86` …）。一个工具通过 `LLVM_LINK_COMPONENTS` 声明自己依赖哪些组件，CMake 会自动把对应的库链接进来。`llvm-config` 工具就是用来查询这些组件依赖关系的。

如果你对 CMake 语法本身完全陌生，LLVM 官方建议先读 [`docs/CMakePrimer.rst`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/CMakePrimer.rst)，它有最小化的 CMake 语言速览。

## 3. 本讲源码地图

本讲涉及的关键文件如下，全部位于 `llvm/` 子目录下：

| 文件 | 作用 |
|------|------|
| [`CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt) | 顶层构建脚本：定义项目、版本、所有构建选项（cache 变量）、输出目录，并用 `add_subdirectory` 把各子目录挂进构建树。 |
| [`docs/CMake.md`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/CMake.md) | 官方"如何用 CMake 构建 LLVM"文档，含 Quick start 与全部变量参考。 |
| [`CMakePresets.json`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakePresets.json) | CMake 预设：把常用的几组 `-D` 变量打包成命名预设，便于复用。 |
| [`lib/CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/CMakeLists.txt) | 库构建入口：为 `lib/` 下每个子目录（IR、Analysis、Transforms、CodeGen…）调用 `add_subdirectory`，生成全部 `LLVM*` 组件库。 |
| [`include/llvm/CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/CMakeLists.txt) | 头文件构建入口：为需要生成 TableGen 头的子目录调用 `add_subdirectory`。 |
| [`tools/CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/CMakeLists.txt) | 工具构建入口：遍历 `tools/` 下各工具目录，并把 clang/lld 等外部子项目挂进来。 |
| [`examples/CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/CMakeLists.txt) | 示例构建入口：列出每个示例子目录。 |
| [`tools/opt/CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/opt/CMakeLists.txt) | 单个工具（`opt`）的构建脚本，展示 `add_llvm_tool` 与 `LLVM_LINK_COMPONENTS` 的典型写法。 |
| [`cmake/modules/AddLLVM.cmake`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/cmake/modules/AddLLVM.cmake) | LLVM 自定义 CMake 宏的定义库，`add_llvm_tool`、`add_llvm_example` 等都在这里。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **CMake 顶层与 lib/include/tools 分层**——理解构建树的骨架。
2. **CMake 预设与常用构建变量**——学会用最少的参数得到想要的构建。
3. **从源码到可执行文件的整体流程**——把配置、编译、链接、产物路径串起来。

### 4.1 CMake 顶层与 lib/include/tools 分层

#### 4.1.1 概念说明

LLVM 的构建系统采用"**一个顶层 `CMakeLists.txt` + 多层 `add_subdirectory` 递归**"的经典结构。可以这样理解：

- 顶层 `CMakeLists.txt` 负责"全局事务"：声明项目名与版本、设置 C++ 标准、定义所有构建选项（写成 CMake **cache 变量**，可用 `-D` 覆盖）、计算各类输出目录、最后用一串 `add_subdirectory(...)` 把各个子目录挂进构建树。
- 每个子目录的 `CMakeLists.txt` 只管自己那一层。`lib/` 负责把各个库挂进来，`tools/` 负责把各个可执行工具挂进来，`examples/` 负责示例。
- 这种分层使得"加一个新工具"或"加一个新库"只需要在对应目录加一个 `CMakeLists.txt` 并登记一行 `add_subdirectory`，几乎不动顶层。

为什么强调分层？因为 LLVM 仓库有上万个源文件、上百个库、几十个工具。如果没有清晰的目录约定，依赖关系会很快变成一团乱麻。LLVM 的约定是：**实现进 `lib/`，对外头文件进 `include/`，命令行驱动进 `tools/`，教学示例进 `examples/`**。

#### 4.1.2 核心流程

顶层脚本的大致执行顺序如下（伪代码）：

```
1. cmake_minimum_required(VERSION 3.20.0)        # 声明最低 CMake 版本
2. project(LLVM VERSION <major>.<minor>.<patch>) # 命名项目、启用 C/CXX/ASM 语言
3. 设置 C++ 标准 = 17                            # LLVM 强制要求 C++17
4. 定义大量 option(...) / set(... CACHE ...)      # 全部构建选项（可被 -D 覆盖）
5. 计算 LLVM_TOOLS_BINARY_DIR / LLVM_LIBRARY_DIR # 各类输出路径
6. 处理 LLVM_TARGETS_TO_BUILD / LLVM_ENABLE_PROJECTS  # 决定要建哪些后端/子项目
7. configure_file(...)                           # 生成 config.h、Targets.def 等配置头
8. add_subdirectory(lib/Demangle|Support|TableGen)# 先建三个最底层的库
9. add_subdirectory(utils/TableGen)              # 建 llvm-tblgen（代码生成器）
10. add_subdirectory(include)                    # 头文件/TableGen 头
11. add_subdirectory(lib)                        # 全部组件库
12. add_subdirectory(tools)                      # 全部命令行工具
13. add_subdirectory(runtimes / examples / test) # 运行时、示例、测试
```

注意第 8–11 步的顺序很重要：`Demangle/Support/TableGen` 是最底层的依赖，必须先建；`llvm-tblgen` 要在建 `lib` 之前就绪，因为很多库的头文件（如内建函数表 `intrinsics_gen`）是由 `llvm-tblgen` 生成的。

#### 4.1.3 源码精读

**① 项目声明与 C++ 标准**

[CMakeLists.txt:1-2](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt#L1-L2) 第 1 行注释指向官方文档，第 2 行声明最低 CMake 版本 3.20.0：

```cmake
# See docs/CMake.html for instructions about how to build LLVM with CMake.
cmake_minimum_required(VERSION 3.20.0)
```

[CMakeLists.txt:77-79](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt#L77-L79) 正式声明项目 `LLVM` 并启用 C、C++、ASM 三种语言——版本号来自前面对 `LLVMVersion.cmake` 的 include：

```cmake
project(LLVM
  VERSION ${LLVM_VERSION_MAJOR}.${LLVM_VERSION_MINOR}.${LLVM_VERSION_PATCH}
  LANGUAGES C CXX ASM)
```

[CMakeLists.txt:92-110](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt#L92-L110) 强制 C++17，并拒绝更老的标准（设小了直接 `FATAL_ERROR`）：

```cmake
set(LLVM_REQUIRED_CXX_STANDARD 17)
...
set(CMAKE_CXX_STANDARD ${LLVM_REQUIRED_CXX_STANDARD} CACHE STRING "...")
set(CMAKE_CXX_STANDARD_REQUIRED YES)
```

**② 默认构建类型 = Release**

[CMakeLists.txt:112-117](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt#L112-L117) 如果你没指定 `CMAKE_BUILD_TYPE`，LLVM 会默认填 `Release` 并打印一条警告：

```cmake
if (NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  message(WARNING "No build type selected. Defaulted CMAKE_BUILD_TYPE=Release. ...")
  set(CMAKE_BUILD_TYPE Release CACHE STRING "..." FORCE)
endif()
```

> 这就是为什么很多人"啥都不带跑 `cmake ..`"也能拿到一个能用的优化版 LLVM。

**③ 输出目录：可执行文件去 `bin/`，库去 `lib/`**

[CMakeLists.txt:548-563](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt#L548-L563) 定义了所有产物的落点。记住这一段，你就能预测构建后去哪里找 `opt`：

```cmake
set(LLVM_RUNTIME_OUTPUT_INTDIR ${CMAKE_CURRENT_BINARY_DIR}/${CMAKE_CFG_INTDIR}/bin)
set(LLVM_LIBRARY_OUTPUT_INTDIR ${CMAKE_CURRENT_BINARY_DIR}/${CMAKE_CFG_INTDIR}/lib${LLVM_LIBDIR_SUFFIX})
...
set(LLVM_TOOLS_BINARY_DIR ${LLVM_RUNTIME_OUTPUT_INTDIR}) # --bindir
set(LLVM_LIBRARY_DIR      ${LLVM_LIBRARY_OUTPUT_INTDIR}) # --libdir
set(LLVM_MAIN_SRC_DIR     ${CMAKE_CURRENT_SOURCE_DIR}  ) # --src-root
set(LLVM_MAIN_INCLUDE_DIR ${LLVM_MAIN_SRC_DIR}/include ) # --includedir
set(LLVM_BINARY_DIR       ${CMAKE_CURRENT_BINARY_DIR}  ) # --prefix
```

对于 Ninja 这类**单配置**生成器，`${CMAKE_CFG_INTDIR}` 取值为 `.`，因此：

- 可执行工具落在 `<build>/bin/`（如 `build/bin/opt`）
- 静态库落在 `<build>/lib/`（如 `build/lib/libLLVMCore.a`）

> 小提示：这些变量同时对应 `llvm-config` 工具的 `--bindir`、`--libdir`、`--src-root`、`--includedir`、`--prefix` 选项，注释里也写明了。

**④ 拦截 in-source 构建**

[CMakeLists.txt:509-515](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt#L509-L515) 如果发现你把构建目录选在了源码目录里，会直接报错并提示删除 `CMakeCache.txt`：

```cmake
if( CMAKE_CURRENT_SOURCE_DIR STREQUAL CMAKE_CURRENT_BINARY_DIR AND NOT MSVC_IDE )
  message(FATAL_ERROR "In-source builds are not allowed. ...")
endif()
```

这正是"必须用单独构建目录"的强制保证。

**⑤ 构建树的"骨架"：那一串 add_subdirectory**

[CMakeLists.txt:1376-1441](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt#L1376-L1441) 是本讲最关键的一段——它定义了整个构建树的形状。摘录关键行：

```cmake
# 先建三个最底层库
add_subdirectory(lib/Demangle)
add_subdirectory(lib/Support)
add_subdirectory(lib/TableGen)

add_subdirectory(utils/TableGen)   # 建 llvm-tblgen
add_subdirectory(include)          # 头文件 + TableGen 生成的头
add_subdirectory(lib)              # 全部组件库（IR/Analysis/Transforms/CodeGen/...）
...
if( LLVM_INCLUDE_TOOLS )
  add_subdirectory(tools)          # 全部命令行工具
endif()
...
if( LLVM_INCLUDE_RUNTIMES )
  add_subdirectory(runtimes)
endif()

if( LLVM_INCLUDE_EXAMPLES )
  add_subdirectory(examples)       # 教学示例
endif()
```

可以看到三个开关 `LLVM_INCLUDE_TOOLS`、`LLVM_INCLUDE_RUNTIMES`、`LLVM_INCLUDE_EXAMPLES` 直接决定对应子目录是否进入构建树——这些就是后面要讲的"常用变量"。

**⑥ lib/ 这一层：把每个组件库挂进来**

[lib/CMakeLists.txt:1-52](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/CMakeLists.txt#L1-L52) 用一长串 `add_subdirectory` 列出所有库子目录。这一段几乎就是 LLVM 模块清单的"目录"：

```cmake
include(LLVM-Build)
...
add_subdirectory(IR)
add_subdirectory(Bitcode)
add_subdirectory(CodeGen)
add_subdirectory(Transforms)
add_subdirectory(Analysis)
add_subdirectory(MC)
add_subdirectory(Target)
add_subdirectory(ExecutionEngine)
...
```

这里的每个子目录（如 `lib/IR`、`lib/Analysis`）各自又有 `CMakeLists.txt`，用 `add_llvm_component_library(...)` 定义出 `LLVMCore`、`LLVMAnalysis` 这样的组件库。后续讲义会逐一深入这些目录。

**⑦ include/ 这一层：只处理需要生成头的子目录**

[include/llvm/CMakeLists.txt:14-19](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/CMakeLists.txt#L14-L19) 只为少数几个需要跑 TableGen 的头文件子目录调用 `add_subdirectory`：

```cmake
add_subdirectory(Analysis)
add_subdirectory(Codegen)
add_subdirectory(IR)
add_subdirectory(Support)
add_subdirectory(Frontend)
add_subdirectory(TargetParser)
```

注意：`include/` 下绝大多数 `.h` 是直接随源码存在的普通头文件，不需要 CMake 处理；只有那些**由 `.td` 自动生成**的头（如内建函数表 `Intrinsics.gen`）才需要走 TableGen，所以这里只列了 6 个。

**⑧ tools/ 这一层：工具与外部子项目**

[tools/CMakeLists.txt:1-10](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/CMakeLists.txt#L1-L10) 会自动遍历子目录、为每个工具生成一个开关；开头注释说明了这一点：

```cmake
# This file will recurse into all subdirectories that contain CMakeLists.txt
# Setting variables that match the pattern LLVM_TOOL_{NAME}_BUILD to Off will
# prevent traversing into a directory.
create_llvm_tool_options()
```

也就是说，`tools/` 下每个子目录（`opt/`、`llc/`、`lli/`、`llvm-as/`…）默认都会被构建，除非你用 `-DLLVM_TOOL_<NAME>_BUILD=Off` 关掉。

[tools/CMakeLists.txt:31-50](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/CMakeLists.txt#L31-L50) 还展示了如何把 clang/lld 等同仓库的**外部子项目**挂进来：

```cmake
add_llvm_external_project(lld)
add_llvm_external_project(mlir)
add_llvm_external_project(clang)
add_llvm_external_project(flang)
add_llvm_external_project(lldb)
```

这就是为什么"想在 LLVM 里同时编译 clang"，需要从 `llvm/` 目录配置并在 `LLVM_ENABLE_PROJECTS` 里写 `clang`——clang 是作为外部项目从 `tools/` 这一层接入的。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：亲手把"构建树骨架"和真实目录对应起来，建立空间感。

**操作步骤**：

1. 在仓库里打开 [CMakeLists.txt 第 1376–1441 行](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt#L1376-L1441)。
2. 准备一张三列表格：`add_subdirectory 的参数` | `源码里对应的真实目录` | `它产出什么`。
3. 逐行填表，例如：
   - `lib/Support` → `lib/Support/` → 基础工具库 `LLVMSupport`
   - `utils/TableGen` → `utils/TableGen/` → 可执行文件 `llvm-tblgen`
   - `tools` → `tools/` → 全部命令行工具（`opt`、`llc`…）
   - `examples` → `examples/` → 教学示例（默认不编译，需 `LLVM_BUILD_EXAMPLES=ON`）
4. 打开 [lib/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/CMakeLists.txt)，对照 `ls lib/` 的实际目录，核对哪些 `add_subdirectory` 有对应真实目录。

**需要观察的现象**：`add_subdirectory` 的参数与磁盘上的目录是**一一对应**的；任何多出来的真实目录如果没被登记，就不会进入默认构建。

**预期结果**：得到一张"构建树骨架表"，能清楚说出 `lib/`、`include/`、`tools/`、`examples/`、`test/` 分别由哪一行 `add_subdirectory` 挂进构建树。无需运行任何命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么顶层 `CMakeLists.txt` 要先 `add_subdirectory(lib/Demangle/Support/TableGen)`，再 `add_subdirectory(utils/TableGen)`，最后才 `add_subdirectory(lib)`？顺序能调换吗？

> **答案**：因为存在依赖。`Demangle/Support/TableGen` 是最底层、不依赖其它 LLVM 库的基础设施；`utils/TableGen` 构建出的 `llvm-tblgen` 可执行文件，被 `lib/` 里大量组件用来生成 `.gen`/`.inc` 头文件（如 `Intrinsics.gen`）。如果先建 `lib/` 再建 `llvm-tblgen`，那些生成步骤就拿不到工具，构建会失败。顺序不能随意调换。

**练习 2**：`include/llvm/CMakeLists.txt` 只列了 6 个 `add_subdirectory`，但 `include/llvm/` 下有几十个子目录。其它头文件是怎么被构建系统"处理"的？

> **答案**：大多数头文件就是普通的 `.h`，由编译器在编译 `.cpp` 时按头文件搜索路径直接包含，不需要 CMake 单独处理。只有那些**由 TableGen 生成**的头（`.td → .gen/.inc`）才需要在 `include/llvm/CMakeLists.txt` 里登记，以便在构建时跑 `llvm-tblgen`。其余头文件的"安装"则由顶层文件末尾的 `install(DIRECTORY include/llvm ...)` 一并完成。

---

### 4.2 CMake 预设与常用构建变量

#### 4.2.1 概念说明

LLVM 有上百个构建变量（绝大多数以 `LLVM_` 开头）。你不需要全记住，日常只要掌握两类：

1. **常用变量（高频）**：决定"建什么、用什么优化、给多少资源"。例如 `CMAKE_BUILD_TYPE`、`LLVM_TARGETS_TO_BUILD`、`LLVM_ENABLE_PROJECTS`、`LLVM_ENABLE_RUNTIMES`、`LLVM_ENABLE_ASSERTIONS`。
2. **CMake 预设（preset）**：把几组常用的 `-D` 组合预先写进 [`CMakePresets.json`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakePresets.json)，用 `--preset` 名字一次带出，省得手敲一长串参数。

预设的价值在于**可复现**：团队成员只要用同一个 preset 名字，就能拿到完全相同的配置。

#### 4.2.2 核心流程

使用预设与变量的流程：

```
方式 A：手敲变量
  cmake -B build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DLLVM_ENABLE_ASSERTIONS=ON \
        -DLLVM_TARGETS_TO_BUILD=X86

方式 B：用预设组合
  cmake --preset llvm-enable-assertions ...   # LLVM 仓库目前只有"隐藏"预设，见下文说明
```

> 重要提醒：本仓库的 `CMakePresets.json` 里所有预设都标记了 `"hidden": true`（详见 4.2.3）。**隐藏预设不能直接被 `--preset` 调用**，它们的用途是被其它（将来定义在用户本地 `CMakeUserPresets.json` 里的）预设 `"inherits"` 继承复用。所以当前阶段，**直接用 `-D` 手敲变量**是最稳妥的方式。

#### 4.2.3 源码精读

**① 官方推荐的 Quick start 步骤**

[docs/CMake.md:26-86](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/CMake.md#L26-L86) 给出了标准的"建目录 → 配置 → 构建 → 安装"四步。核心命令：

```console
$ cmake path/to/llvm/source/root      # 配置（生成 build.ninja / Makefile）
$ cmake --build .                      # 构建（调用底层 ninja/make）
$ cmake --build . --target install     # 安装到 CMAKE_INSTALL_PREFIX
```

[docs/CMake.md:6-8](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/CMake.md#L6-L8) 一句话点明 CMake 的定位，值得记住：

> CMake is a cross-platform build-generator tool. CMake does not build the project; it generates the files needed by your build tool.

[docs/CMake.md:108-118](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/CMake.md#L108-L118) 说明如何用 `-G` 指定生成器（如 `-G Ninja`），以及用 `cmake --help` 查看本机可用生成器列表。

**② 最常用的几个 LLVM 变量**

[docs/CMake.md:234-262](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/CMake.md#L234-L262) 列出"Frequently Used LLVM-related variables"，最关键的两个：

```text
LLVM_ENABLE_PROJECTS:STRING
  例：-DLLVM_ENABLE_PROJECTS="clang;lldb"   # 同时构建 clang、lldb

LLVM_TARGETS_TO_BUILD:STRING
  例：-DLLVM_TARGETS_TO_BUILD=X86            # 只构建 X86 后端（默认 "all" = 全部后端）
```

回到顶层脚本看它们的定义：

- [CMakeLists.txt:138](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt#L138) 定义 `LLVM_ALL_PROJECTS`（可被 `"all"` 展开）。
- [CMakeLists.txt:147-148](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt#L147-L148) 把 `LLVM_ENABLE_PROJECTS` 声明为 cache 变量，默认空。
- [CMakeLists.txt:173-176](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt#L173-L176) 同样定义 `LLVM_ENABLE_RUNTIMES`（控制 libc++、compiler-rt 等运行时）。
- [CMakeLists.txt:572-607](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt#L572-L607) 列出全部后端 `LLVM_ALL_TARGETS`（X86、AArch64、RISCV、AMDGPU…），并把 `LLVM_TARGETS_TO_BUILD` 默认设为 `"all"`。

**③ 控制是否构建工具/示例/测试的开关**

[CMakeLists.txt:893-913](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt#L893-L913) 定义了几个成对的开关，理解"`INCLUDE_` vs `BUILD_`"的区别很有用：

```cmake
option(LLVM_INCLUDE_TOOLS "Generate build targets for the LLVM tools." ON)
option(LLVM_BUILD_TOOLS  "Build the LLVM tools. If OFF, just generate build targets." ON)
...
option(LLVM_BUILD_EXAMPLES "Build the LLVM example programs. ..." OFF)
option(LLVM_INCLUDE_EXAMPLES "Generate build targets for the LLVM examples" ON)

option(LLVM_BUILD_TESTS  "Build LLVM unit tests. If OFF, just generate build targets." OFF)
option(LLVM_INCLUDE_TESTS "Generate build targets for the LLVM unit tests." ON)
```

规律：`INCLUDE_*` 决定"生成不生成 target"（即这个目录进不进构建树），`BUILD_*` 决定"默认 `cmake --build` 时编不编它"。所以示例默认 `LLVM_BUILD_EXAMPLES=OFF`——你配置后得显式 `cmake --build build --target ModuleMaker` 才会编译示例。

**④ CMakePresets.json：把变量打包成预设**

[CMakePresets.json:1-8](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakePresets.json#L1-L8) 声明预设文件版本与最低 CMake：

```json
{
  "version": 6,
  "cmakeMinimumRequired": { "major": 3, "minor": 20, "patch": 0 }
}
```

每个预设就是把一组 `-D` 变量起个名字。例如启用断言的预设：

[CMakePresets.json:25-32](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakePresets.json#L25-L32)

```json
{
  "name": "llvm-enable-assertions",
  "hidden": true,
  "description": "Enable runtime assertions",
  "cacheVariables": { "LLVM_ENABLE_ASSERTIONS": true }
}
```

只建 X86 后端的预设：

[CMakePresets.json:73-80](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakePresets.json#L73-L80)

```json
{
  "name": "llvm-target-x86",
  "hidden": true,
  "cacheVariables": { "LLVM_TARGETS_TO_BUILD": "X86" }
}
```

> 注意它们都带 `"hidden": true`。隐藏预设的作用是"被继承"——你可以在本地新建 `CMakeUserPresets.json`，用 `"inherits": ["llvm-enable-assertions", "llvm-target-x86", "llvm-export-compile-commands"]` 把多个预设组合成一个自己的命名预设。这正是预设机制的设计意图：把仓库提供的基础积木组合成团队/个人配置。

#### 4.2.4 代码实践（源码阅读 + 轻量配置型）

**实践目标**：理解"变量 → cache → 构建行为"的传导链。

**操作步骤**：

1. 读 [CMakeLists.txt:572-611](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt#L572-L611)，找到 `LLVM_ALL_TARGETS` 列表与 `LLVM_TARGETS_TO_BUILD` 的默认值。
2. 读 [CMakePresets.json:73-80](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakePresets.json#L73-L80)，确认 `llvm-target-x86` 预设做了什么。
3. （可选，本地有 CMake 时）执行一次最小配置，**只配不编**，以节省时间：
   ```bash
   cmake -S . -B build -G Ninja \
         -DCMAKE_BUILD_TYPE=Release \
         -DLLVM_TARGETS_TO_BUILD=X86 \
         -DLLVM_ENABLE_ASSERTIONS=ON
   ```
4. 配置完成后打开 `build/CMakeCache.txt`，搜索 `LLVM_TARGETS_TO_BUILD:` 和 `LLVM_ENABLE_ASSERTIONS:`，确认你传的值被写进了 cache。

**需要观察的现象**：你传的 `-D` 值会原样出现在 `CMakeCache.txt` 对应行；未传的变量保持脚本里的默认值（如 `LLVM_TARGETS_TO_BUILD` 不传则是 `all` 展开后的完整列表）。

**预期结果**：能在 `CMakeCache.txt` 中看到 `LLVM_TARGETS_TO_BUILD:STRING=X86` 与 `LLVM_ENABLE_ASSERTIONS:BOOL=ON`。若本地无法安装 CMake/无源码构建环境，则标注「待本地验证」并只完成第 1、2 步的阅读即可。

#### 4.2.5 小练习与答案

**练习 1**：`LLVM_INCLUDE_EXAMPLES=ON` 但 `LLVM_BUILD_EXAMPLES=OFF` 时，执行 `cmake --build build` 会编译示例吗？为什么？

> **答案**：不会。`INCLUDE_EXAMPLES=ON` 只让示例目录进入构建树、生成对应的 target（如 `ModuleMaker`），但因为 `BUILD_EXAMPLES=OFF`，这些 target 被标记为 `EXCLUDE_FROM_ALL`（见 `add_llvm_example` 宏的实现），不会出现在默认的 `all` 目标里。你需要显式 `cmake --build build --target ModuleMaker` 才会编译它。

**练习 2**：仓库里的 `llvm-target-x86` 预设为什么不能直接 `cmake --preset llvm-target-x86` 使用？

> **答案**：因为它在 `CMakePresets.json` 里被标记了 `"hidden": true`。隐藏预设不会出现在 `cmake --list-presets` 中，也不能被 `--preset` 直接调用；它的设计用途是被其它预设通过 `"inherits"` 继承。要直接用，需在本地 `CMakeUserPresets.json` 里定义一个非隐藏预设并 `inherits` 它。

---

### 4.3 从源码到可执行文件：工具与示例的构建链路

#### 4.3.1 概念说明

前两节讲了"骨架"和"参数"。这一节回答最具体的问题：**当我 `cmake --build build` 之后，那些 `bin/opt`、`bin/llc` 是怎么从 `.cpp` 变出来的？**

答案的核心是一个宏 **`add_llvm_tool`**（定义工具可执行文件）和一份声明 **`LLVM_LINK_COMPONENTS`**（声明这个工具要链接哪些组件库）。示例用的是姐妹宏 `add_llvm_example`。理解了这三个东西，你就掌握了"加一个新工具/新示例"的全部构建知识。

#### 4.3.2 核心流程

一个工具（以 `opt` 为例）从源码到可执行文件的链路：

```
tools/opt/CMakeLists.txt
  ├─ set(LLVM_LINK_COMPONENTS Core Analysis Passes ...)   # 声明依赖的组件库
  └─ add_llvm_tool(opt opt.cpp ...)                        # 定义可执行目标 opt
        │
        │  AddLLVM.cmake 的 llvm_add_tool 内部会：
        │   1. add_llvm_executable(opt ...)                # 创建可执行 target
        │   2. 根据 LLVM_LINK_COMPONENTS 解析出要链接的 LLVM*.a
        │   3. target_link_libraries(opt PRIVATE <那些库>)
        ▼
  ninja/make 编译 opt.cpp → opt.o
  链接 opt.o + libLLVMCore.a + libLLVMPasses.a + ... → build/bin/opt
```

组件库本身则由 `lib/` 下各子目录用 `add_llvm_component_library` 定义（如 `lib/IR` 定义 `LLVMCore`），那是第 4.1 节"lib 分层"里每个子目录自己的事。

#### 4.3.3 源码精读

**① 一个工具的完整 CMakeLists：以 opt 为例**

[tools/opt/CMakeLists.txt:1-31](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/opt/CMakeLists.txt#L1-L31) 先声明它依赖的全部组件库——这正是 `opt` 之所以"能跑优化流水线"的原因：它链接了 `Passes`、`ScalarOpts`、`InstCombine`、`Analysis` 等：

```cmake
set(LLVM_LINK_COMPONENTS
  Analysis
  BitWriter
  CodeGen
  Core
  IPO
  InstCombine
  Passes
  ScalarOpts
  Support
  ...
  )
```

[tools/opt/CMakeLists.txt:43-54](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/opt/CMakeLists.txt#L43-L54) 定义可执行目标，并启用了插件支持：

```cmake
add_llvm_tool(opt
  PARTIAL_SOURCES_INTENDED
  opt.cpp
  DEPENDS
  intrinsics_gen
  SUPPORT_PLUGINS
  EXPORT_SYMBOLS
  )
target_link_libraries(opt PRIVATE LLVMOptDriver)
```

`DEPENDS intrinsics_gen` 表示先跑 TableGen 生成内建函数表头；`SUPPORT_PLUGINS` 让 `opt` 支持用 `-load-pass-plugin` 动态加载 pass 插件（这是 [u3-l4 pass 插件机制](u3-l4-pass-plugins.md) 的基础）。

**② 一个更小的工具：llvm-as**

[tools/llvm-as/CMakeLists.txt:1-15](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llvm-as/CMakeLists.txt#L1-L15) 是最简洁的范本，把 IR 文本（`.ll`）转成位码（`.bc`），只依赖 4 个组件：

```cmake
set(LLVM_LINK_COMPONENTS
  AsmParser
  BitWriter
  Core
  Support
  )

add_llvm_tool(llvm-as
  llvm-as.cpp
  DEPENDS
  intrinsics_gen
  )
```

`add_llvm_tool` 第一个参数 `llvm-as` 既是 target 名也是最终可执行文件名，所以构建后会得到 `build/bin/llvm-as`。

**③ add_llvm_tool / add_llvm_example 的定义**

[cmake/modules/AddLLVM.cmake:1654-1656](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/cmake/modules/AddLLVM.cmake#L1654-L1656) `add_llvm_tool` 只是把工作转交给 `llvm_add_tool`：

```cmake
macro(add_llvm_tool name)
  llvm_add_tool(LLVM ${ARGV})
endmacro()
```

[cmake/modules/AddLLVM.cmake:1659-1674](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/cmake/modules/AddLLVM.cmake#L1659-L1674) `add_llvm_example` 与之类似，但多了一个关键差异——当 `LLVM_BUILD_EXAMPLES` 关闭时，把目标设为 `EXCLUDE_FROM_ALL`（即不进入默认构建）：

```cmake
macro(add_llvm_example name)
  if( NOT LLVM_BUILD_EXAMPLES )
    set(EXCLUDE_FROM_ALL ON)
  endif()
  add_llvm_executable(${name} EXPORT_SYMBOLS ${ARGN})
  ...
endmacro(add_llvm_example name)
```

这就从源码层面印证了 4.2 节"示例默认不编译"的结论。

**④ 一个示例的 CMakeLists：ModuleMaker**

[examples/ModuleMaker/CMakeLists.txt:1-9](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/ModuleMaker/CMakeLists.txt#L1-L9) 声明依赖并用 `add_llvm_example` 注册：

```cmake
set(LLVM_LINK_COMPONENTS
  BitWriter
  Core
  Support
  )

add_llvm_example(ModuleMaker
  ModuleMaker.cpp
  )
```

[examples/CMakeLists.txt:1-11](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/CMakeLists.txt#L1-L11) 用一串 `add_subdirectory` 把所有示例挂进来，`ModuleMaker` 就在其中：

```cmake
add_subdirectory(BrainF)
add_subdirectory(Fibonacci)
add_subdirectory(HowToUseLLJIT)
add_subdirectory(IRTransforms)
add_subdirectory(Kaleidoscope)
add_subdirectory(ModuleMaker)
...
```

#### 4.3.4 代码实践（轻量构建型）

**实践目标**：构建出第一个可执行文件，验证"产物落在 `build/bin/`"的结论。

**操作步骤**（**只构建一个最小工具**，避免全量编译耗时数小时）：

1. 在 `llvm/` 目录配置（只选 X86 后端，加速配置与依赖生成）：
   ```bash
   cmake -S . -B build -G Ninja \
         -DCMAKE_BUILD_TYPE=Release \
         -DLLVM_TARGETS_TO_BUILD=X86 \
         -DLLVM_BUILD_EXAMPLES=ON
   ```
2. 只构建 `llvm-as` 这一个工具（它会自动带上它依赖的 `LLVMCore`/`LLVMSupport` 等库，但不会构建其它无关工具）：
   ```bash
   cmake --build build --target llvm-as
   ```
3. 同样方式构建示例 `ModuleMaker`：
   ```bash
   cmake --build build --target ModuleMaker
   ```
4. 列出生成的可执行文件路径：
   ```bash
   ls -l build/bin/llvm-as build/bin/ModuleMaker
   ```

**需要观察的现象**：两个可执行文件都出现在 `build/bin/` 下；`llvm-as` 能把 `.ll` 转成 `.bc`，`ModuleMaker` 运行后会在当前目录生成一个 `foo.bc`（见示例源码逻辑）。

**预期结果**：
- `build/bin/llvm-as`（来自 [tools/llvm-as/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llvm-as/CMakeLists.txt)）
- `build/bin/ModuleMaker`（来自 [examples/ModuleMaker/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/ModuleMaker/CMakeLists.txt)）

若本地无编译环境或全量构建耗时过长，可只执行第 1 步配置并跳过 2–4，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`tools/llvm-as/CMakeLists.txt` 里 `LLVM_LINK_COMPONENTS` 写了 `AsmParser / BitWriter / Core / Support`。如果漏写 `BitWriter`，构建 `llvm-as` 时会发生什么？

> **答案**：`llvm-as` 的源码调用位码写入 API（属于 `BitWriter`/`LLVMBitWriter` 组件）。漏写后，链接阶段会报"未定义引用（undefined reference）"错误，因为没把 `libLLVMBitWriter.a` 链接进来。`LLVM_LINK_COMPONENTS` 就是告诉构建系统"请把这些组件库链接给我"。

**练习 2**：为什么 `add_llvm_example(ModuleMaker ...)` 默认不编译，而 `add_llvm_tool(llvm-as ...)` 默认会编译？

> **答案**：见 [AddLLVM.cmake:1659-1674](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/cmake/modules/AddLLVM.cmake#L1659-L1674)：当 `LLVM_BUILD_EXAMPLES=OFF`（默认）时，`add_llvm_example` 会给目标设置 `EXCLUDE_FROM_ALL`，使其不进入默认 `all` 目标。而 `add_llvm_tool` 走 `llvm_add_tool`，没有这种排除逻辑，故默认参与构建。所以示例需要 `LLVM_BUILD_EXAMPLES=ON` 或显式 `--target <example>` 才会编译。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个"**最小可用 LLVM 工具链**"的构建与验证任务。

**任务**：配置一个 Release + 断言开启 + 仅 X86 后端 + 含示例的构建，单独构建 `llvm-as`、`llvm-dis`、`ModuleMaker` 三个目标，并用它们完成一次"IR 文本 → 位码 → IR 文本"的往返转换。

**步骤**：

1. **配置**（对应模块 4.2）：
   ```bash
   cmake -S . -B build -G Ninja \
         -DCMAKE_BUILD_TYPE=Release \
         -DLLVM_ENABLE_ASSERTIONS=ON \
         -DLLVM_TARGETS_TO_BUILD=X86 \
         -DLLVM_BUILD_EXAMPLES=ON
   ```
   配置后，用 `grep` 在 `build/CMakeCache.txt` 里核对你传入的 4 个变量值是否被正确记录。

2. **构建三个目标**（对应模块 4.3，避免全量编译）：
   ```bash
   cmake --build build --target llvm-as llvm-dis ModuleMaker
   ```
   确认产物都在 `build/bin/` 下（对应模块 4.1 讲的输出目录约定）。

3. **IR 往返转换**（衔接 [u1-l1](u1-l1-project-overview.md) 学过的 `.ll` 与 `.bc` 互转）：
   ```bash
   printf 'define i32 @main(){ ret i32 42 }\n' > t.ll
   build/bin/llvm-as t.ll -o t.bc      # 文本 → 位码
   build/bin/llvm-dis t.bc -o t2.ll     # 位码 → 文本
   diff t.ll t2.ll && echo "round-trip ok"
   ```

4. **画出构建树骨架**（对应模块 4.1）：把顶层 [CMakeLists.txt:1376-1441](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt#L1376-L1441) 中那一串 `add_subdirectory` 整理成一张树状图，标注 `lib/Support`、`utils/TableGen`、`include`、`lib`、`tools`、`examples` 各自产出什么。

**预期结果**：`diff` 报告 `round-trip ok`（允许格式化导致的细微空白差异，重点是语义一致）；得到一张构建树骨架图。若无本地构建环境，至少完成第 4 步的源码阅读与画图，并将 1–3 标注「待本地验证」。

## 6. 本讲小结

- LLVM 用 **CMake 作为构建生成器**：它不直接编译，而是为 Ninja/make/MSVS 生成构建文件；官方推荐 Ninja。
- 顶层 [`CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakeLists.txt) 负责"全局事务"（项目、版本、C++17、全部选项、输出目录），再用一串 `add_subdirectory` 把 `lib/`、`include/`、`tools/`、`examples/`、`test/` 挂成构建树。
- 目录分层有清晰约定：**实现进 `lib/`、头文件进 `include/`、命令行驱动进 `tools/`、示例进 `examples/`**；`lib/` 的每个子目录对应一个组件库（如 `LLVMCore`、`LLVMAnalysis`）。
- 常用变量记住五个就够日常使用：`CMAKE_BUILD_TYPE`（默认 Release）、`LLVM_TARGETS_TO_BUILD`（默认 all）、`LLVM_ENABLE_PROJECTS`、`LLVM_ENABLE_RUNTIMES`、`LLVM_ENABLE_ASSERTIONS`；`LLVM_INCLUDE_*` 控制"生成不生成 target"，`LLVM_BUILD_*` 控制"默认编不编"。
- [`CMakePresets.json`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/CMakePresets.json) 把常用 `-D` 组合打包成预设，但本仓库的预设目前都是 `hidden`，需在本地 `CMakeUserPresets.json` 中继承使用。
- 工具与示例分别用 `add_llvm_tool` / `add_llvm_example` 宏定义，靠 `LLVM_LINK_COMPONENTS` 声明组件库依赖；产物统一落在 `<build>/bin/`（可执行）与 `<build>/lib/`（库）。

## 7. 下一步学习建议

到这里，你已经能从源码构建出 LLVM 工具，并理解了目录分层。建议接下来：

- 进入 [u1-l3 命令行工具入口](u1-l3-cli-tools.md)：亲手使用 `opt`、`llc`、`lli`、`llvm-as`、`llvm-dis`，把本讲"构建产物"变成"会用的工作"。
- 之后进入 [u1-l4 第一个 IR 程序：ModuleMaker](u1-l4-module-maker.md)，用本讲构建出的 `ModuleMaker` 示例，开始从 C++ 侧操作 LLVM IR。
- 想深入构建系统本身，可阅读 [`docs/CMake.md`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/CMake.md) 的"LLVM-related variables"完整参考，以及 [`cmake/modules/AddLLVM.cmake`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/cmake/modules/AddLLVM.cmake) 中 `add_llvm_component_library`、`llvm_update_compile_flags` 等宏，理解组件库是如何被自动加上依赖与编译选项的。
