# 构建系统：CMake 构建流程与依赖

> 本讲是「入门单元」的第 3 讲，承接 [u1-l2 仓库结构与四大组件](u1-l2-repository-structure.md)。
> u1-l2 已经告诉我们 `host/` 是运行在主机上的 `libuhd` 库。本讲要回答的问题是：
> **这些源码是怎么变成 `libuhd.so` 的？构建时都检测了哪些依赖？我又该怎么用一条命令看到「构建启用了哪些组件」？**

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 看懂 `host/CMakeLists.txt` 的整体骨架，说出「构建类型 → 项目声明 → 依赖检测 → 组件注册 → 子目录组织 → 汇总打印」这条主线。
2. 说清楚 UHD 的四段式版本号 `MAJOR.API.ABI.PATCH` 是怎么在 CMake 里定义、又被压成一个整数宏 `UHD_VERSION` 的，以及为什么默认构建是 Release。
3. 说出 `libuhd.so`（共享库）、`libuhd.a`（静态库）、CMake 目标 `uhd` 与下游导入目标 `UHD::uhd` 之间的关系——也就是题目里「libuhd / uhdlib」到底指什么。
4. 自己跑一次 `cmake` 配置，并对照 `uhd_config_info --enabled-components` 验证「构建时启用的组件」是从哪里来的。

---

## 2. 前置知识

在进入源码前，先用大白话把几个 CMake 概念过一遍，后面读源码会非常顺。

- **构建系统（build system generator）**：CMake 本身不编译代码，它是一个「构建系统的生成器」。你写 `CMakeLists.txt`，CMake 读完后帮你生成 Makefile / Ninja / VS 工程，再由这些工具真正去 `g++` 编译。所以 UHD 的「构建」分两步：先 `cmake ..`（配置），再 `make`（编译）。
- **配置阶段 vs 编译阶段**：`cmake` 命令运行时叫「配置阶段（configure）」，这一步会检查依赖、算版本号、决定要不要编译某个模块，并把这些决定写进构建目录。`make` 阶段才真正调用编译器。本讲几乎全部内容都发生在「配置阶段」。
- **缓存变量（cache variable）**：形如 `set(VAR "x" CACHE STRING "")`。它会被存到构建目录的 `CMakeCache.txt` 里，用户可以用 `-DVAR=xxx` 在命令行覆盖。UHD 大量使用它来开放开关（比如 `-DENABLE_USB=OFF`）。
- **目标（target）**：CMake 里「一个要构建的产物」就是一个 target，比如一个共享库、一个可执行文件。`add_library(uhd SHARED ...)` 就是声明一个叫 `uhd` 的共享库 target。
- **组件（component）**：UHD 把可选功能（USB、各型号设备、C API、Python 绑定、示例、测试……）抽象成一个个「组件」，每个组件都有「是否启用」的状态。这些状态最终会被打印成一张表，也会被编译进 `libuhd` 供运行时查询。

如果你对 CMake 完全陌生，记住一句话即可：**`CMakeLists.txt` 就是一份用脚本语言写的、描述「要造什么、依赖什么、装到哪里」的说明书。**

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `host/CMakeLists.txt` | `host/` 的顶层构建脚本，是本讲的「主干」。它串起构建类型、依赖检测、组件注册与子目录组织。 |
| `host/lib/CMakeLists.txt` | `libuhd` 库本身的构建脚本：注册各型号设备组件、汇总源码、生成 `uhd` 共享库与 `uhd_static` 静态库。 |
| `host/include/CMakeLists.txt` | 公共头文件侧的构建脚本：生成 `config.h`，进入 `uhd/` 子目录安装头文件。 |
| `host/cmake/Modules/UHDVersion.cmake` | 计算版本号：四段式版本 + git 信息 → 拼成 `UHD_VERSION` 字符串与 `UHD_ABI_VERSION`。 |
| `host/cmake/Modules/UHDMinDepVersions.cmake` | 集中存放所有依赖的最低版本要求（CMake、GCC、Boost、Python…）。 |
| `host/cmake/Modules/UHDGlobalDefs.cmake` | 把四段版本号压成单个整数宏 `UHD_VERSION_ADDED`，写进 `config.h`。 |
| `host/cmake/Modules/UHDComponent.cmake` | 「组件」机制的实现：`LIBUHD_REGISTER_COMPONENT` 宏与汇总打印函数。 |
| `host/cmake/Modules/UHDBuildInfo.cmake` | 采集「构建信息」（编译器、编译选项、构建日期），写进运行时可查的 `build_info`。 |
| `host/lib/build_info.cpp` | 由模板生成的源文件，把 CMake 里算好的组件列表、编译器等暴露成 C++ 函数。 |
| `host/utils/uhd_config_info.cpp` | 命令行工具，把上述 `build_info` 打印出来。实践任务的核心。 |

`host/cmake/Modules/` 下还有很多 `UHDBoost.cmake`、`UHDPython.cmake`、`FindLIBUSB.cmake` 等，本讲会在「依赖检测」一节点到它们的作用。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. **CMake 构建配置**：`host/CMakeLists.txt` 的整体结构。
2. **版本管理**：四段式版本号如何流动。
3. **依赖检测**：编译器、Boost、Python 与硬件传输库。
4. **编译产物**：`libuhd` 的动态库、静态库与 CMake 目标命名。

---

### 4.1 CMake 构建配置：host/CMakeLists.txt 的整体结构

#### 4.1.1 概念说明

`host/CMakeLists.txt` 是整个主机驱动的「总调度」。它要做的事情可以归纳成一条主线：

> **定基调（构建类型）→ 声明项目 → 准备 CMake 模块路径 → 检测依赖 → 注册组件 → 进入子目录 → 打印汇总。**

理解这条主线后，你打开这个 750 多行的文件就不会迷路——每一大段注释（`####...` 包起来的块）就是主线上的一个环节。

#### 4.1.2 核心流程

用伪代码描述 `host/CMakeLists.txt` 的执行顺序：

```text
1. cmake_minimum_required(3.12)         # 声明最低 CMake 版本
2. 若未指定 CMAKE_BUILD_TYPE → 默认 "Release"   # 定基调
3. project(UHD CXX C)                   # 正式声明项目（此时 UHD_SOURCE_DIR 等变量就绪）
4. 把本地 cmake/Modules 插到模块路径最前面      # 让自定义 *.cmake 优先被找到
5. include(UHDMinDepVersions)            # 载入依赖最低版本表
6. 检查编译器版本（GCC/Clang/MSVC）            # 太老就警告或报错
7. set CMAKE_CXX_STANDARD 20             # 要求 C++20
8. 注册打包/安装变量；include(UHDComponent)     # 装上「组件」机制
9. 检测 Boost、Python 模块                 # 依赖检测（见 4.3）
10. LIBUHD_REGISTER_COMPONENT(...)        # 注册顶层组件（LibUHD/C API/Python/Examples/...）
11. add_subdirectory(lib/include/...)      # 进入子目录真正造东西
12. 生成 uhd.pc / UHDConfig.cmake          # 给下游项目用的配置文件
13. UHD_PRINT_COMPONENT_SUMMARY()          # 打印一张「启用/禁用组件」表
```

其中第 2、10、11、13 步是初学者最该记住的——它们决定了「这次构建到底编了什么」。

#### 4.1.3 源码精读

**① 默认 Release 构建类型**

[host/CMakeLists.txt:21-25](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/CMakeLists.txt#L21-L25) 在 `project()` 之前就先把构建类型定成 `Release`，目的是「让 UHD 依赖的库也用 Release 的优化选项去查找/编译」。这就是为什么你什么都不传、直接 `cmake ..` 也能拿到带 `-O3` 优化的库：

```cmake
if(NOT CMAKE_BUILD_TYPE)
   set(CMAKE_BUILD_TYPE "Release")
   message(STATUS "Build type not specified: defaulting to release.")
endif(NOT CMAKE_BUILD_TYPE)
```

注释里明确写了「Use release build type by default to get optimization flags」。

**② 项目声明与本地模块路径**

[host/CMakeLists.txt:43](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/CMakeLists.txt#L43) 是 `project(UHD CXX C)`。这一行之后，CMake 才会建立 `UHD_SOURCE_DIR`、`UHD_BINARY_DIR` 等变量。紧接着 [host/CMakeLists.txt:53](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/CMakeLists.txt#L53) 把本仓库自带的 `cmake/Modules/` 插到模块搜索路径最前面，保证后面的 `include(UHDBoost)`、`include(UHDComponent)` 能优先找到 UHD 自己的版本，而不是系统里同名的模块：

```cmake
list(INSERT CMAKE_MODULE_PATH 0 ${UHD_SOURCE_DIR}/cmake/Modules)
```

**③ 装上「组件」机制并检测依赖**

[host/CMakeLists.txt:144-145](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/CMakeLists.txt#L144-L145) 载入了两个关键模块：`UHDComponent` 提供 `LIBUHD_REGISTER_COMPONENT` 宏（见 4.1 的组件机制细节在 4.4 节展开），`UHDPackage` 负责 CPack 打包。

```cmake
include(UHDComponent) #enable components
include(UHDPackage)   #setup cpack
```

**④ 注册顶层组件**

[host/CMakeLists.txt:478-516](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/CMakeLists.txt#L478-L516) 用一串 `LIBUHD_REGISTER_COMPONENT` 登记顶层可选功能。第一个就是核心库本身：

```cmake
LIBUHD_REGISTER_COMPONENT("LibUHD" ENABLE_LIBUHD ON "Boost_FOUND;HAVE_PYTHON_MODULE_MAKO" OFF ON)
LIBUHD_REGISTER_COMPONENT("LibUHD - C API" ENABLE_C_API ON "ENABLE_LIBUHD" OFF OFF)
```

这一行的含义是：注册一个名叫 `"LibUHD"` 的组件，对应开关变量 `ENABLE_LIBUHD`，默认 `ON`；它的依赖是 `Boost_FOUND` 和 `HAVE_PYTHON_MODULE_MAKO` 两个变量都为真；最后一个参数 `ON` 表示「这是必需组件，依赖不满足就报错」。这正是「组件」机制的精髓——把「功能 + 开关 + 依赖 + 是否必需」四件事打包。

**⑤ 进入子目录造东西**

[host/CMakeLists.txt:522-544](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/CMakeLists.txt#L522-L544) 根据组件开关决定要不要 `add_subdirectory`：

```cmake
if(ENABLE_LIBUHD)
    add_subdirectory(lib)
endif(ENABLE_LIBUHD)
add_subdirectory(include)
if(ENABLE_EXAMPLES)
    add_subdirectory(examples)
endif(ENABLE_EXAMPLES)
```

也就是说：`lib/` 只有在 `LibUHD` 组件启用时才会被编译；`examples/`、`tests/`、`utils/`、`python/` 都各自受对应组件控制。这解释了为什么关掉某个组件后，相关源码根本不会被编译。

**⑥ 打印汇总**

最后 [host/CMakeLists.txt:735](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/CMakeLists.txt#L735) 调用 `UHD_PRINT_COMPONENT_SUMMARY()` 输出那张「启用/禁用组件」表，[host/CMakeLists.txt:750-751](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/CMakeLists.txt#L750-L751) 再补上版本号和安装前缀。你每次 `cmake ..` 滚动到最后看到的那段文字，就是这两行打印的。

#### 4.1.4 代码实践

**实践目标**：亲手跑一次配置，看到 4.1.3 里讲的那条主线在真实输出里如何体现。

**操作步骤**：

1. 进入 `host/`，建一个独立的构建目录（CMake 强烈建议「外置构建」out-of-source）：

   ```bash
   cd host
   mkdir build && cd build
   cmake .. 2>&1 | tee cmake_config.log
   ```

2. 在 `cmake_config.log` 里依次找下面 4 段输出，它们分别对应主线上的环节：
   - `Build type not specified: defaulting to release.` → 对应 4.1.3 ①。
   - `Configuring LibUHD support...` / `Enabling LibUHD support.` 一长串 → 对应 4.1.3 ④。
   - `# UHD enabled components` / `# UHD disabled components` 两张表 → 对应 4.1.3 ⑥。
   - `Building version: ...` 与 `Using install prefix: ...` → 对应 4.1.3 ⑥。

**需要观察的现象**：

- 即使你不传任何 `-D` 参数，构建类型也是 `Release`。
- 表里每个组件后面都有提示 `Override with -DENABLE_xxx=ON/OFF`，说明每个组件都能在命令行单独开关。

**预期结果**：你会看到类似这样的列表（具体启用项取决于本机是否装了 libusb、Python 依赖等）：

```text
# UHD enabled components
  * LibUHD
  * LibUHD - C API
  * Examples
  * Utils
  ...
```

> 待本地验证：在没有硬件、且未安装 libusb 的最小环境里，`USB` 及依赖它的 `B100/B200/USRP1` 等组件会出现在「disabled」表中；若装了 libusb，则会出现在「enabled」表中。具体结果以你本机为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `project(UHD CXX C)` 这一行删掉，前面提到的 `UHD_SOURCE_DIR` 还能用吗？为什么？

> **参考答案**：不能正常使用。`UHD_SOURCE_DIR`、`UHD_BINARY_DIR` 是 `project()` 调用后由 CMake 自动建立的变量。删掉 `project()` 后这些变量未定义，第 53 行的 `list(INSERT CMAKE_MODULE_PATH 0 ${UHD_SOURCE_DIR}/cmake/Modules)` 会插入一个空路径，导致后续 `include(UHDBoost)` 等找不到 UHD 自带的模块。

**练习 2**：为什么 UHD 要在 `project()` **之前**就设置 `CMAKE_BUILD_TYPE`，而不是在 `project()` 之后？

> **参考答案**：因为 `project()` 会触发对依赖库的查找；在查找之前把构建类型定为 `Release`，能让「查找/链接依赖库」时也采用 Release 的库搜索行为与优化选项。注释里明确写了「Do this before project setup to ensure dependend libraries use the correct build type」。

---

### 4.2 版本管理：从四个数字到 UHD_VERSION 宏

#### 4.2.1 概念说明

u1-l1 已经讲过 UHD 版本号是四段式 `MAJOR.API.ABI.PATCH`，当前是 `4.10.0.0`，并且会被压成一个整数宏 `UHD_VERSION` 供编译期判断。本节回答：**这四段数字在 CMake 里是怎么定义的？又是怎么变成「字符串版本号」和「整数宏」两套东西的？**

UHD 的版本管理集中在 `host/cmake/Modules/UHDVersion.cmake`，它会同时产出三类产物：

- 一个人类可读的字符串 `UHD_VERSION`（形如 `4.10.0.0-xxx-gHASH`），用于打印和打包文件名。
- 一个 ABI 字符串 `UHD_ABI_VERSION`（形如 `4.10.0`），决定 `libuhd.so` 的 `SOVERSION`，是「二进制兼容性」的契约。
- 一个整数 `UHD_VERSION_ADDED`，写进 `config.h` 后成为 C++ 里的 `UHD_VERSION` 宏，用于 `#if UHD_VERSION >= ...` 的编译期判断。

#### 4.2.2 核心流程

```text
四段数字 (MAJOR/API/ABI/PATCH)        ← UHDVersion.cmake 顶部硬编码
        │
        ├──(拼字符串)──► UHD_VERSION = "4.10.0.0-<git计数>-<git哈希>"
        │                  （开发分支会带上 git 信息）
        │
        ├──(取前三段)──► UHD_ABI_VERSION = "4.10.0"  → libuhd.so 的 SOVERSION
        │
        └──(压缩成整数)► UHD_VERSION_ADDED
                          = MAJOR*1e6 + API*1e4 + ABI*1e2 + PATCH
                          （UHDGlobalDefs.cmake）
                              │
                              ▼  configure_file
                          version.hpp.in → version.hpp 里的 #define UHD_VERSION
```

整数压缩公式为：

\[
\text{UHD\_VERSION\_ADDED} = \text{MAJOR}\times 10^{6} + \text{API}\times 10^{4} + \text{ABI}\times 10^{2} + \text{PATCH}
\]

举例：对 `4.10.0.0`，该值为 \(4\times10^6 + 10\times10^4 + 0\times10^2 + 0 = 4\,100\,000\)。这样设计的好处是：版本越高，整数越大，于是下游代码可以直接用整数比较来判断「当前 UHD 是否够新」。

> 小细节：开发分支（`UHD_VERSION_DEVEL` 为真）时，PATCH 位会被替换成 `99`，表示「比该 PATCH 的任何正式发布都新」。见 [host/cmake/Modules/UHDGlobalDefs.cmake:13-17](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/cmake/Modules/UHDGlobalDefs.cmake#L13-L17)。

#### 4.2.3 源码精读

**① 四段版本号的来源**

[host/cmake/Modules/UHDVersion.cmake:22-29](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/cmake/Modules/UHDVersion.cmake#L22-L29) 硬编码了四段数字，并把 `UHD_VERSION_DEVEL` 设为 `TRUE`（因为当前 HEAD 在 `master` 开发分支上）：

```cmake
set(UHD_VERSION_MAJOR      4)
set(UHD_VERSION_API        10)
set(UHD_VERSION_ABI        0)
set(UHD_VERSION_PATCH      0)
set(UHD_VERSION_DEVEL      TRUE)
```

**② 开发分支会让版本带上 git 信息**

[host/cmake/Modules/UHDVersion.cmake:64-66](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/cmake/Modules/UHDVersion.cmake#L64-L66) 通过 `git rev-parse`/`git describe` 判断当前分支。在 `master` 上会把 `UHD_VERSION_DEVEL` 置真，并随后把 git 提交计数和哈希拼进版本字符串：

```cmake
elseif(UHD_GIT_BRANCH STREQUAL "master")
    message(STATUS "Operating on master branch.")
    set(UHD_VERSION_DEVEL TRUE)
```

**③ 拼成字符串与 ABI 版本**

[host/cmake/Modules/UHDVersion.cmake:161-175](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/cmake/Modules/UHDVersion.cmake#L161-L175) 把四段数字拼成最终的两个字符串变量：

```cmake
set(UHD_VERSION "${UHD_VERSION_MAJOR}.${UHD_VERSION_API}.${UHD_VERSION_ABI}.${UHD_VERSION_PATCH}-${UHD_GIT_COUNT}-${UHD_GIT_HASH}")
...
set(UHD_ABI_VERSION "${UHD_VERSION_MAJOR}.${UHD_VERSION_API}.${UHD_VERSION_ABI}")
```

注意 `UHD_ABI_VERSION` 只取前三段（`4.10.0`），它就是后面 `libuhd.so` 的 `SOVERSION`，代表「二进制兼容性窗口」。

**④ 压成整数宏**

[host/cmake/Modules/UHDGlobalDefs.cmake:13-20](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/cmake/Modules/UHDGlobalDefs.cmake#L13-L20) 用 `math(EXPR ...)` 把四段数字算成一个整数 `UHD_VERSION_ADDED`：

```cmake
if(UHD_VERSION_DEVEL)
    math(EXPR UHD_VERSION_ADDED "1000000 * ${UHD_VERSION_MAJOR} + 10000 * ${UHD_VERSION_API} + 100 * ${UHD_VERSION_ABI} + 99")
else()
    math(EXPR UHD_VERSION_ADDED "1000000 * ${UHD_VERSION_MAJOR} + 10000 * ${UHD_VERSION_API} + 100 * ${UHD_VERSION_ABI} + ${UHD_VERSION_PATCH}")
endif(UHD_VERSION_DEVEL)
add_definitions(-DHAVE_CONFIG_H)
```

**⑤ 这个整数如何进入 C++ 头文件**

[host/include/uhd/version.hpp.in:21-24](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/version.hpp.in#L21-L24) 是一个模板文件，`@UHD_VERSION_ADDED@` 会在配置阶段被替换成上面的整数，最终生成 `version.hpp` 里的 `#define UHD_VERSION`：

```cpp
#define UHD_VERSION @UHD_VERSION_ADDED@
```

于是下游 C++ 代码就能写 `#if UHD_VERSION >= 4100000` 这样的编译期判断。

#### 4.2.4 代码实践

**实践目标**：验证「同一个版本号」在不同形态下的取值。

**操作步骤**（源码阅读型，不需要硬件）：

1. 打开 `host/cmake/Modules/UHDVersion.cmake`，确认四段数字是 `4/10/0/0`。
2. 用本节公式手算 `UHD_VERSION_ADDED`（开发分支 PATCH 取 99）：
   \(4\times10^6 + 10\times10^4 + 0\times10^2 + 99 = 4\,100\,099\)。
3. 配置构建后，去构建目录查看生成的 `include/uhd/version.hpp`（由 `version.hpp.in` 生成），确认 `#define UHD_VERSION` 的值与你手算一致。

**需要观察的现象**：模板文件 `version.hpp.in` 里的 `@UHD_VERSION_ADDED@` 占位符，在生成后被替换成了真实整数。

**预期结果**：在 `master` 开发分支上，`UHD_VERSION` 宏约为 `4100099`（待本地验证：正式 release 分支因为 `UHD_VERSION_DEVEL=FALSE`，PATCH 用真实值 `0`，则宏为 `4100000`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `UHD_ABI_VERSION` 只取前三段，而不含 PATCH？

> **参考答案**：ABI 版本代表「二进制兼容性窗口」。前三段 `MAJOR.API.ABI` 决定了 `libuhd.so` 的 `SOVERSION`；PATCH 是纯 bugfix，不破坏二进制兼容，所以同一个 ABI 版本下不同 PATCH 的库可以互换。把 PATCH 排除在外，才能让小版本升级不必重新编译所有下游程序。

**练习 2**：写一个表达式，让下游代码在「UHD 不低于 4.10.0.0」时才编译某段代码。

> **参考答案**：`#if UHD_VERSION >= 4100000`。因为 `4.10.0.0` 对应整数 `4*1e6 + 10*1e4 + 0*1e2 + 0 = 4100000`。注意开发分支会因 PATCH=99 而略大，比较结果依然成立。

---

### 4.3 依赖检测：编译器、Boost、Python 与硬件传输库

#### 4.3.1 概念说明

UHD 不是「裸 C++」就能编译的，它在配置阶段要确认三类依赖：

1. **编译器**：UHD 用了 C++20 特性，编译器太老会直接报错或警告。
2. **Boost**：`libuhd` 强依赖 Boost 的若干子库（`chrono`、`thread`、`filesystem` 等）。
3. **Python 与 Python 模块**：构建期（注意不是运行期）需要 Python 来跑各种代码生成脚本，还需要 `mako`、`numpy`、`ruamel.yaml` 等模块；这些模块是否齐全，会决定 Python API 等组件能否启用。
4. **硬件传输库**：`libusb`（USB 类设备）、`DPDK`（高性能以太网）等是可选的，装了才启用对应传输。

所有依赖的「最低版本要求」被集中放在 [host/cmake/Modules/UHDMinDepVersions.cmake](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/cmake/Modules/UHDMinDepVersions.cmake#L11-L28)，方便维护。

#### 4.3.2 核心流程

```text
include(UHDMinDepVersions)            # 载入最低版本表
        │
        ├─► 检查 CMAKE / 编译器版本（GCC≥7.3 / Clang≥6 / MSVC≥16.0）
        │       太老 → WARNING 或 FATAL_ERROR
        │
        ├─► set(CMAKE_CXX_STANDARD 20)  # 要求 C++20
        │
        ├─► include(UHDBoost)           # 查找 Boost 子库（chrono/thread/...）
        │       结果写入 Boost_FOUND
        │
        ├─► include(UHDPython) + 一串 UHD_PYTHON_CHECK_MODULE_VERSION
        │       逐个检测 mako/numpy/ruamel.yaml/requests，结果写入 HAVE_PYTHON_MODULE_*
        │
        └─► find_package(LIBUSB) / find_package(DPDK)   # 在 lib/CMakeLists.txt 里
                结果用于 LIBUHD_REGISTER_COMPONENT("USB" ...) / ("DPDK" ...)
```

关键点：**依赖检测的结果（如 `Boost_FOUND`、`HAVE_PYTHON_MODULE_MAKO`）会直接喂给组件注册**。这就是为什么「装不装某个依赖」会自动决定「某个组件启不启用」。

#### 4.3.3 源码精读

**① 集中的最低版本表**

[host/cmake/Modules/UHDMinDepVersions.cmake:11-28](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/cmake/Modules/UHDMinDepVersions.cmake#L11-L28) 把所有最低版本放在一起：

```cmake
set(UHD_CMAKE_MIN_VERSION           "3.12"     )
set(UHD_GCC_MIN_VERSION             "7.3.0"   )
set(UHD_CLANG_MIN_VERSION           "6.0.0"   )
...
set(UHD_PYTHON_MIN_VERSION          "3.7"     )
set(UHD_BOOST_MIN_VERSION           "1.71"    )
```

文件顶部注释说明：这些变量故意**不带** `UHD_` 前缀，是为了方便别的项目复用来做版本检查。

**② 编译器版本检查**

[host/CMakeLists.txt:65-127](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/CMakeLists.txt#L65-L127) 分三种编译器分别处理：GCC/Clang 太老只发 `WARNING`（「This build may or not work」），而 MSVC 太老直接 `FATAL_ERROR`。紧接着 [host/CMakeLists.txt:129](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/CMakeLists.txt#L129) 设定 C++ 标准为 20：

```cmake
set(CMAKE_CXX_STANDARD 20)
```

**③ Boost 检测**

[host/CMakeLists.txt:318-338](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/CMakeLists.txt#L318-L338) 先列出必需的 Boost 子库，再 `include(UHDBoost)` 真正去查找。注意注释特意说明 `system`、`unit_test_framework` 不在这里显式列出（在某些系统/静态链接下显式列出会出错），由 `UHDBoost.cmake` 特殊处理：

```cmake
set(UHD_BOOST_REQUIRED_COMPONENTS
    chrono date_time filesystem program_options serialization thread
)
...
include(UHDBoost)
```

**④ Python 模块检测**

[host/CMakeLists.txt:354-394](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/CMakeLists.txt#L354-L394) 用一串 `UHD_PYTHON_CHECK_MODULE_VERSION` 逐个检查 Python 模块版本。每个检查都会把结果写进一个 `HAVE_PYTHON_MODULE_*` 变量，例如：

```cmake
UHD_PYTHON_CHECK_MODULE_VERSION(
    "numpy module" "numpy" "numpy.__version__"
    ${UHD_NUMPY_MIN_VERSION} HAVE_PYTHON_MODULE_NUMPY)
```

这个 `HAVE_PYTHON_MODULE_NUMPY` 之后会被用在 Python API 组件的依赖里（见 4.1.3 ④）。

**⑤ 硬件传输库检测（在 lib/CMakeLists.txt 里）**

[host/lib/CMakeLists.txt:62-63](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/CMakeLists.txt#L62-L63) 用 `find_package` 找可选的 libusb 和 DPDK，然后 [host/lib/CMakeLists.txt:64-79](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/CMakeLists.txt#L64-L79) 把它们的检测结果作为组件依赖：

```cmake
find_package(LIBUSB)
find_package(DPDK)
LIBUHD_REGISTER_COMPONENT("USB" ENABLE_USB ON "ENABLE_LIBUHD;LIBUSB_FOUND" OFF OFF)
...
LIBUHD_REGISTER_COMPONENT("DPDK" ENABLE_DPDK ON "ENABLE_MPMD;DPDK_FOUND" OFF OFF)
```

也就是说：没装 libusb → `LIBUSB_FOUND` 为假 → `USB` 组件自动禁用 → 依赖 USB 的 `B100/B200/USRP1` 也会跟着禁用。整条依赖链是自动级联的。

#### 4.3.4 代码实践

**实践目标**：在配置输出里定位「每个依赖的检测结果」。

**操作步骤**：

1. 跑一次 `cmake ..` 并保存输出（同 4.1.4）。
2. 在日志里搜索下面这些关键字，记录它们的值：
   - `Dependency LIBUSB_FOUND =`（在 `Configuring USB support...` 段下）
   - `Dependency DPDK_FOUND =`
   - `Boost_FOUND`（顶层组件 `LibUHD` 的依赖）
   - `Dependency HAVE_PYTHON_MODULE_NUMPY =`（在 Python API 组件段下）

**需要观察的现象**：每个组件配置时，CMake 会逐行打印它每个依赖变量的当前值（这正是 [host/cmake/Modules/UHDComponent.cmake:28-30](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/cmake/Modules/UHDComponent.cmake#L28-L30) 的行为）。

**预期结果**：你能看到类似 `Dependency LIBUSB_FOUND = TRUE/FALSE` 的行，并能据此解释为什么某个组件被启用或禁用。待本地验证：本机未装 libusb 时该项为 `FALSE`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Boost 的 `system` 子库没有被写进 `UHD_BOOST_REQUIRED_COMPONENTS` 列表？

> **参考答案**：注释说明，在某些系统上显式列出 `system` 会引发错误；同时静态链接（Windows 默认）下列出 `unit_test_framework` 也会出问题。因此这两个被放到 `UHDBoost.cmake` 里特殊处理，而不是与其他子库一起常规查找。

**练习 2**：如果你忘了装 Python 的 `mako` 模块，哪个**必需**组件会因此失败？

> **参考答案**：`LibUHD` 组件。它的依赖里含 `HAVE_PYTHON_MODULE_MAKO`（见 [host/CMakeLists.txt:478](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/CMakeLists.txt#L478)），且该组件最后一个参数为 `ON`（必需）。依赖不满足且未显式禁用，会触发 `FATAL_ERROR`。所以构建期必须有 `mako`。

---

### 4.4 编译产物：libuhd 的动态库、静态库与 CMake 目标命名

#### 4.4.1 概念说明

题目里提到「libuhd / uhdlib」，这里需要先厘清一个容易混淆的命名层次。在本仓库里：

- **并没有一个叫 `uhdlib` 的独立 target**（用 `grep` 在 `host/` 下搜索 `uhdlib` 没有任何命中）。日常说的「uhd 库」其实就是 **`libuhd`**。
- 真正存在的 CMake 目标只有两个，且都产出名叫 `libuhd` 的文件：
  - 目标 **`uhd`** → 产物 `libuhd.so`（Linux）/ `uhd.dll`（Windows），**共享库**，是绝大多数人用的那个。
  - 目标 **`uhd_static`** → 产物 `libuhd.a`，**静态库**，由 `ENABLE_STATIC_LIBS=ON` 启用。
- 下游项目通过 CMake 导入目标 **`UHD::uhd`** 来链接（见 4.1.3 ⑥ 生成的 `UHDTargets.cmake`）。

所以「libuhd 与 uhdlib 的区别」更准确的理解是：**操作系统层面的库文件叫 `libuhd`（`libuhd.so` / `libuhd.a`），而 CMake 内部构建它用的 target 名叫 `uhd`，对下游导出时又叫 `UHD::uhd`**——是同一个东西在「文件名 / target 名 / 导入名」三个层面的不同称呼，而非两个不同的库。

`libuhd` 本身的构建脚本在 [host/lib/CMakeLists.txt](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/CMakeLists.txt)。它干三件事：注册各型号设备组件、把所有子目录的源码汇拢、最后 `add_library` 造出库。

#### 4.4.2 核心流程

```text
lib/CMakeLists.txt
   │
   ├─① 注册设备组件（USB/B100/.../MPMD/X400/OctoClock/DPDK）   ← 决定编进哪些设备驱动
   │
   ├─② INCLUDE_SUBDIRECTORY(include/cal/types/convert/rfnoc/usrp/...)
   │       每个子目录用 LIBUHD_APPEND_SOURCES(...) 往 libuhd_sources 列表里塞源文件
   │
   ├─③ configure_file(build_info.cpp / version.cpp)             ← 把版本/构建信息烤进源文件
   │
   ├─④ add_library(uhd SHARED ${libuhd_sources})                ← 造共享库
   │       target_link_libraries → Boost + 可选 rpclib/DPDK/Python
   │       SOVERSION = UHD_ABI_VERSION（如 "4.10.0"）
   │
   └─⑤ 若 ENABLE_STATIC_LIBS：
           add_library(uhd_static STATIC ...) OUTPUT_NAME uhd   ← 造静态库 libuhd.a
```

#### 4.4.3 源码精读

**① 注册设备组件**

[host/lib/CMakeLists.txt:64-79](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/CMakeLists.txt#L64-L79) 注册了所有具体型号设备的组件。注意依赖的级联：`B100/B200/USRP1` 依赖 `ENABLE_USB`，而 `N300/N320/E300/E320/X400/SIM` 等现代设备都依赖 `ENABLE_MPMD`：

```cmake
LIBUHD_REGISTER_COMPONENT("B200" ENABLE_B200 ON "ENABLE_LIBUHD;ENABLE_USB" OFF OFF)
LIBUHD_REGISTER_COMPONENT("MPMD" ENABLE_MPMD ON "ENABLE_LIBUHD" OFF OFF)
LIBUHD_REGISTER_COMPONENT("X400" ENABLE_X400 ON "ENABLE_LIBUHD;ENABLE_MPMD" OFF OFF)
```

**② 把子目录源码汇拢进 `libuhd_sources`**

[host/lib/CMakeLists.txt:84-96](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/CMakeLists.txt#L84-L96) 通过自定义的 `INCLUDE_SUBDIRECTORY` 宏依次处理各子目录。每个子目录（`convert/`、`rfnoc/`、`usrp/`、`transport/`…）会调用 `LIBUHD_APPEND_SOURCES` 把自己的 `.cpp` 追加到全局 `libuhd_sources` 列表里：

```cmake
INCLUDE_SUBDIRECTORY(include)
INCLUDE_SUBDIRECTORY(convert)
INCLUDE_SUBDIRECTORY(rfnoc)
INCLUDE_SUBDIRECTORY(usrp)
INCLUDE_SUBDIRECTORY(transport)
...
```

`LIBUHD_APPEND_SOURCES` 宏定义在 [host/lib/CMakeLists.txt:11-13](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/CMakeLists.txt#L11-L13)，本质就是 `list(APPEND ...)`。这种「全局累加源文件列表、最后一次性 `add_library`」的做法，是 UHD 把分散在十几个子目录的代码编译进单一 `libuhd.so` 的关键。

**③ 烤入构建信息**

[host/lib/CMakeLists.txt:103-114](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/CMakeLists.txt#L103-L114) 用 `configure_file` 把 `build_info.cpp` 和 `version.cpp` 模板里的 `@...@` 占位符替换成配置阶段算好的值（编译器、组件列表、版本号等），生成真正的 `.cpp`。其中 `build_info.cpp` 模板的 `enabled_components()` 函数把 CMake 变量 `_uhd_enabled_components`（分号分隔的列表）转成逗号分隔字符串——这正是运行时查询组件列表的数据来源：

```cmake
configure_file(
    ${CMAKE_CURRENT_SOURCE_DIR}/build_info.cpp
    ${CMAKE_CURRENT_BINARY_DIR}/build_info.cpp
    @ONLY)
```

[host/lib/build_info.cpp:65-69](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/build_info.cpp#L65-L69) 的实现：

```cpp
const std::string enabled_components()
{
    return boost::algorithm::replace_all_copy(
        std::string("@_uhd_enabled_components@"), std::string(";"), std::string(", "));
}
```

**④ 造共享库 `libuhd.so`**

[host/lib/CMakeLists.txt:177](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/CMakeLists.txt#L177) 是造库的核心一行；[host/lib/CMakeLists.txt:187-198](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/CMakeLists.txt#L187-L198) 把 Boost 各子库链进来；[host/lib/CMakeLists.txt:230-234](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/CMakeLists.txt#L230-L234) 设定 `SOVERSION`/`VERSION` 为 `UHD_ABI_VERSION`（即 `4.10.0`），并设 `DEFINE_SYMBOL UHDDLL_EXPORTS`（控制 Windows 下导出符号）：

```cmake
add_library(uhd SHARED ${libuhd_sources})
...
set_target_properties(uhd PROPERTIES DEFINE_SYMBOL "UHDDLL_EXPORTS")
set_target_properties(uhd PROPERTIES SOVERSION "${UHD_ABI_VERSION}")
set_target_properties(uhd PROPERTIES VERSION "${UHD_ABI_VERSION}")
```

> 在 Linux 上，这会产生 `libuhd.so.4.10.0` 以及指向它的符号链接 `libuhd.so.4.10` 和 `libuhd.so`——`SOVERSION` 决定了中间那个数字链接。

**⑤ 造静态库 `libuhd.a`**

[host/lib/CMakeLists.txt:288-290](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/CMakeLists.txt#L288-L290) 在 `ENABLE_STATIC_LIBS` 时造静态库，注意它的 `OUTPUT_NAME` 也是 `uhd`，所以产物文件名是 `libuhd.a`，并定义宏 `UHD_STATIC_LIB`（让头文件知道现在是静态链接、不要走 dllimport）：

```cmake
add_library(uhd_static STATIC ${libuhd_sources} $<TARGET_OBJECTS:uhd_rc>)
set_target_properties(uhd_static PROPERTIES OUTPUT_NAME uhd)
set_target_properties(uhd_static PROPERTIES COMPILE_DEFINITIONS UHD_STATIC_LIB)
```

启用开关在 [host/lib/CMakeLists.txt:319-321](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/CMakeLists.txt#L319-L321)。

**⑥ 公共头文件侧：生成 `config.h`**

[host/include/CMakeLists.txt:8-11](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/CMakeLists.txt#L8-L11) 由 `config.h.in` 生成 `config.h`，里面就包含上一节 `UHD_VERSION_ADDED` 等「编译期常量」；[host/include/CMakeLists.txt:20](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/CMakeLists.txt#L20) 再进入 `uhd/` 子目录安装所有公共头文件：

```cmake
configure_file(
    ${CMAKE_CURRENT_SOURCE_DIR}/config.h.in
    ${CMAKE_CURRENT_BINARY_DIR}/config.h
)
...
add_subdirectory(uhd)
```

#### 4.4.4 代码实践

**实践目标**：把「构建时启用的组件」和「运行时查到的组件」对上号，验证它们来自同一条数据链。

**操作步骤**：

1. 按本讲前面步骤完成 `cmake ..` 和 `make`，并 `make install`（或直接用构建产物）。
2. 运行配置信息工具：

   ```bash
   uhd_config_info --print-all
   # 或只看组件：
   uhd_config_info --enabled-components
   ```

3. 对照 [host/utils/uhd_config_info.cpp:27](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_config_info.cpp#L27) 与 [host/utils/uhd_config_info.cpp:73-76](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_config_info.cpp#L73-L76)，确认 `--enabled-components` 打印的内容就是 `build_info::enabled_components()`。

**需要观察的现象**：`uhd_config_info --enabled-components` 输出的列表，应当与你 4.1.4 在 `cmake` 日志末尾看到的「`# UHD enabled components`」那张表**完全一致**。这证明了一条数据链：

```text
LIBUHD_REGISTER_COMPONENT 填充 _uhd_enabled_components
   → configure_file 烤进 build_info.cpp
   → 编进 libuhd.so
   → uhd_config_info 运行时读出并打印
```

**预期结果**：两边组件列表一致。例如都包含 `LibUHD, LibUHD - C API, Examples, Utils, ...`。

> 待本地验证：若没有硬件/未装 libusb，`USB` 等组件不会出现在该列表中；若曾用 `-DENABLE_EXAMPLES=OFF` 配置，则 `Examples` 也不会出现。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `uhd_static` 的 `OUTPUT_NAME` 要设成 `uhd`，而不是 `uhd_static`？

> **参考答案**：为了让静态库产物文件名是 `libuhd.a`，与共享库 `libuhd.so` 的「basename」一致。这样链接器命令行 `-luhd` 在共享/静态两种场景下都能工作；同时定义 `UHD_STATIC_LIB` 宏让头文件切换到静态链接的符号声明。注意 CMake target 名仍叫 `uhd_static`，只是产出文件名被改成了 `uhd`。

**练习 2**：`uhd_config_info --enabled-components` 显示的列表，是在 **配置阶段** 还是 **运行阶段** 决定的？

> **参考答案**：内容在**配置阶段**决定（由 `LIBUHD_REGISTER_COMPONENT` 填入 `_uhd_enabled_components`），但**在运行阶段**才能被读取（值被 `configure_file` 烤进 `build_info.cpp`，编译进 `libuhd.so`，最终由 `uhd_config_info` 调用 `enabled_components()` 打印）。所以同一份 `libuhd.so`，无论拷到哪台机器，查到的组件列表都反映它**被构建时**的配置。

---

## 5. 综合实践

把本讲 4 个模块串起来，完成下面这个「构建可观测性」小任务。

**任务**：用一条 `cmake` 命令，刻意改变组件组合，并验证改动同时反映在「配置日志」「生成的源文件」「运行时工具」三处。

**步骤**：

1. 先做一次默认配置作为对照：

   ```bash
   cd host && mkdir -p build && cd build
   cmake .. 2>&1 | tee config_default.log
   ```

   记下日志末尾 `# UHD enabled components` 表的内容（对应 4.1 的主线、4.4 的数据链源头）。

2. 清掉缓存，做一次「关掉示例和测试、关掉 USB」的精简配置：

   ```bash
   rm -rf *
   cmake .. -DENABLE_EXAMPLES=OFF -DENABLE_TESTS=OFF -DENABLE_USB=OFF 2>&1 | tee config_trim.log
   ```

3. 在 `config_trim.log` 里确认：`Examples`/`Tests`/`USB`（以及依赖 USB 的 `B100/B200/USRP1`）出现在 **disabled** 表中；同时 `Building version:` 行里的版本号符合 4.2 讲的格式。

4. `make` 并运行 `uhd_config_info --enabled-components`（对应 4.4.4），确认运行时输出的列表与第 3 步日志里的 enabled 表一致、且不再含被关掉的组件。

**验收标准**：

- 能指出 `host/CMakeLists.txt` 里哪几行负责「默认 Release」「注册顶层组件」「进入子目录」「打印汇总」。
- 能解释 `UHD_VERSION` 整数宏的算法，并说出 `UHD_ABI_VERSION` 为何只取前三段。
- 能说清 `libuhd.so` / `libuhd.a` / target `uhd` / 导入目标 `UHD::uhd` 的关系。
- 能把 `uhd_config_info --enabled-components` 的输出回溯到 `LIBUHD_REGISTER_COMPONENT` 这条数据链。

> 待本地验证：若环境无法完整 `make`（缺依赖或无编译器），可只完成第 1–3 步的配置与日志分析部分，第 4 步标注为「待本地验证」。

---

## 6. 本讲小结

- `host/CMakeLists.txt` 的主线是：**默认 Release → `project()` → 载入自定义模块 → 检测依赖 → `LIBUHD_REGISTER_COMPONENT` 注册组件 → `add_subdirectory` → 打印汇总**。
- UHD 版本是四段式 `MAJOR.API.ABI.PATCH`（当前 `4.10.0.0`），在 CMake 里同时生成字符串 `UHD_VERSION`、ABI 串 `UHD_ABI_VERSION`（取前三段，决定 `libuhd.so` 的 `SOVERSION`）和整数宏 `UHD_VERSION`（公式 \( \text{MAJOR}\cdot10^6+\text{API}\cdot10^4+\text{ABI}\cdot10^2+\text{PATCH} \)）。
- 依赖检测分四类：**编译器版本（要求 C++20）、Boost 子库、Python 模块（mako/numpy/ruamel.yaml 等）、硬件传输库（libusb/DPDK）**；检测结果直接作为组件依赖，从而自动级联决定组件是否启用。
- 依赖的最低版本集中在 `UHDMinDepVersions.cmake`，方便维护与下游复用。
- `libuhd` 由 `lib/CMakeLists.txt` 构建：它把各子目录源码汇拢进 `libuhd_sources`，再用 `add_library(uhd SHARED ...)` 造共享库；`ENABLE_STATIC_LIBS` 时另造 `uhd_static`（产物 `libuhd.a`）。「libuhd」是文件名层面的称呼，CMake target 名是 `uhd`，下游导入名是 `UHD::uhd`，并非两个不同的库。
- 组件列表通过 `configure_file` 被烤进 `build_info.cpp`、编译进 `libuhd`，最终由 `uhd_config_info --enabled-components` 在运行时读出——这就是「构建时启用组件」可被查询的完整链路。

---

## 7. 下一步学习建议

本讲聚焦「怎么把源码编成库」。接下来建议：

1. **先认识 API 入口**：进入 [u1-l4 公共 API 头文件全景](u1-l4-public-api-overview.md)，看看 `libuhd.so` 对外暴露了哪些头文件（`config.hpp`、`version.hpp`、`device.hpp` 等），把「构建产物」和「使用入口」连起来。
2. **再看一个示例**：[u1-l6 第一个示例 rx_samples_to_file](u1-l6-first-example-rx-to-file.md) 会展示如何链接 `libuhd` 写出第一个能跑的程序。
3. **想深入构建可继续读**：`host/cmake/Modules/UHDBoost.cmake`、`UHDPython.cmake` 看依赖查找细节；`UHDPackage.cmake` 看 CPack 打包；`host/docs/CMakeLists.txt` 看文档/手册页组件如何复用同一套 `LIBUHD_REGISTER_COMPONENT` 机制。
