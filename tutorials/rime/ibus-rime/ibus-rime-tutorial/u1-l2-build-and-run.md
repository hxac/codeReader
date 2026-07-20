# 依赖、构建与运行

> 本讲是单元 U1 的第二篇。上一篇 `u1-l1` 已经厘清了 Rime 生态的分工（librime 是核心、plum 管数据、ibus-rime 是「薄前端」）。本篇不再讨论「它是什么」，而是回答「怎么把它编译出来、怎么让它跑起来」。

## 1. 本讲目标

读完本讲，你应当能够：

- 说清 ibus-rime 在编译期和运行期分别依赖哪些库，以及它们各自从哪里来。
- 读懂 [`CMakeLists.txt`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt) 的整体结构：它如何发现依赖、如何把源码编译成可执行文件 `ibus-engine-rime`、如何定义安装规则。
- 理解 `rime_config.h.in` 这个模板文件如何被 CMake 的 `configure_file` 加工成真正的 `rime_config.h`，以及其中三个宏（版本号、图标目录、共享数据目录）的值分别从哪里来。
- 学会用项目根目录的 `Makefile` 包装层触发构建与安装，而不是手敲一长串 `cmake` 命令。
- 完成一次本地构建实践，并准确指出最终可执行文件的生成路径。

## 2. 前置知识

本讲几乎不涉及 C 语言本身的语法，但需要你对下面几个「构建工具链」概念有最基本的认识。如果你已经熟悉，可以直接跳到第 3 节。

### 2.1 为什么需要「构建系统」

C 语言的源码（`.c` 文件）不能直接运行，必须经过 **编译（compile）→ 链接（link）** 才能变成可执行程序。如果项目只有一两个文件、不依赖外部库，手写 `gcc a.c b.c -o app` 就够了。但 ibus-rime 这样要依赖三四个外部库、还要在不同 Linux 发行版上安装到不同目录的项目，就需要一个「构建系统」来：

1. 自动找到系统里这些库的头文件（`.h`）和动态库（`.so`）在哪里。
2. 根据当前平台生成正确的编译命令。
3. 定义「装到哪里去」（安装规则）。

ibus-rime 用的是 **CMake**。

### 2.2 CMake 是什么

CMake 本身不编译代码，它是一个「生成器」：你写一份声明式的 `CMakeLists.txt`，描述「我要编译什么、依赖什么、装到哪里」，CMake 读完后帮你生成传统的 `Makefile`（或 Ninja 等其他后端的文件），然后再由 `make` 真正执行编译。所以常见的工作流是：

```
CMakeLists.txt  ──cmake──▶  Makefile  ──make──▶  可执行文件
```

### 2.3 pkg-config：跨发行版的依赖探测

不同 Linux 发行版（Ubuntu、Fedora、Arch……）把同一个库装到不同的目录。为了不写死路径，库的维护者通常会随库附带一个 `.pc` 文件（pkg-config 文件），里面记录「我的头文件在哪儿、链接库在哪儿、编译参数是什么」。于是项目只需调用 `pkg-config --cflags libibus-1.0`，就能拿到正确的编译参数，而不用关心发行版差异。

### 2.4 模板文件与 `configure_file`

有些值（比如版本号、安装目录）在写源码时还不知道，要等编译时才能确定。C 的源码里又必须有一个具体的宏定义。解决办法是写一个**模板** `xxx.h.in`，里面用 `@变量名@` 占位；编译时 CMake 用真实值替换占位符，生成最终的 `xxx.h`，再交给编译器。这个过程由 CMake 的 `configure_file` 命令完成。

### 2.5 Makefile 包装层

CMake 的命令比较长（要带很多 `-D` 参数）。为了让贡献者「敲一行命令就能构建」，项目通常在最外层放一个极简的 `Makefile`，把那条长 `cmake` 命令封装成 `make`、`make install` 这样的短命令。ibus-rime 就是这么做的。

理解了这五个概念，下面的源码就读得很顺了。

## 3. 本讲源码地图

本讲围绕「构建」这条线，涉及下面这些文件：

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| [`CMakeLists.txt`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt) | 构建脚本的总入口，定义依赖、编译目标、安装规则 | **核心**，本讲绝大部分篇幅都在读它 |
| [`Makefile`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/Makefile) | 对 CMake 命令的薄封装 | 让用户用 `make` 触发构建 |
| [`rime_config.h.in`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_config.h.in) | 配置头文件模板 | 被 `configure_file` 加工成 `rime_config.h` |
| [`cmake/FindRimeData.cmake`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/cmake/FindRimeData.cmake) | 自定义的「查找 rime-data 目录」模块 | 探测输入方案数据装在哪个目录 |
| [`README.md`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/README.md) | 项目说明 | 列出官方的依赖清单 |

一句话概括它们的关系：**`Makefile` 调用 `cmake` 读 `CMakeLists.txt`；`CMakeLists.txt` 用 `pkg-config` 找到三个外部库、用 `FindRimeData.cmake` 找到数据目录、用 `configure_file` 把 `rime_config.h.in` 加工成 `rime_config.h`，最后把所有 `.c` 编译成 `ibus-engine-rime`。**

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **CMake 构建脚本与整体流程**——先鸟瞰 `CMakeLists.txt` 全貌。
2. **pkg-config 依赖发现**——三个外部库是怎么被找到的。
3. **configure_file 模板生成**——`rime_config.h` 里的三个宏从哪来。
4. **Makefile 包装层**——`make` 背后到底跑了什么。

### 4.1 CMake 构建脚本与整体流程

#### 4.1.1 概念说明

`CMakeLists.txt` 是整个构建的「剧本」。它从上到下大致做了五件事：

1. **声明项目**：项目名、版本、最低 CMake 版本。
2. **发现依赖**：找到 IBus、libnotify、librime、rime-data。
3. **生成配置头**：把模板加工成 `rime_config.h`。
4. **编译目标**：把当前目录所有 `.c` 编译成可执行文件 `ibus-engine-rime`。
5. **安装规则**：把可执行文件、组件描述 `rime.xml`、图标、配置文件装到系统目录。

了解这五步的先后顺序，是读懂后面细节的前提。

#### 4.1.2 核心流程

下面是 `CMakeLists.txt` 的执行流程（伪代码）：

```
1. cmake_minimum_required(3.10)        # 要求 CMake ≥ 3.10
2. project(ibus-rime)                  # 项目名
3. set(ibus_rime_version "1.6.1")      # 记录版本号（后面注入头文件）
4. option(BUILD_STATIC "..." OFF)      # 定义一个开关：是否静态链接
5. 发现 IBus          (pkg-config)
6. 发现 libnotify     (pkg-config)
7. 发现 librime       (find_package Rime)
8. 发现 rime-data     (find_package RimeData)
9. configure_file rime_config.h.in → rime_config.h
10. aux_source_directory(.  SRC)       # 收集当前目录所有 .c
11. add_executable(ibus-engine-rime ${SRC})
12. target_link_libraries(...)         # 链接三组库
13. configure_file rime.xml.in → rime.xml
14. install(...)                       # 四条安装规则
```

注意第 9 步（生成 `rime_config.h`）必须在第 11 步（编译）之前完成，因为源码里 `#include "rime_config.h"`。这正是 CMake 顺序敏感的体现。

#### 4.1.3 源码精读

项目声明与版本号：

```cmake
cmake_minimum_required(VERSION 3.10)
project(ibus-rime)

set(ibus_rime_version 1.6.1)
```

这里把版本号单独存进变量 `ibus_rime_version`，稍后会被注入到 `rime_config.h` 里（见 4.3）。这就是为什么 README 要求 `cmake>=3.10`——版本太旧的 CMake 跑不动这份脚本。参见 [`CMakeLists.txt:1-4`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L1-L4)。

一个重要的开关 `BUILD_STATIC`：

```cmake
option(BUILD_STATIC "Build Rime using static libraries" OFF)
```

默认 `OFF`，表示动态链接（依赖系统已装的 librime 等动态库）。若设为 `ON`，则进入静态构建分支，把 boost、leveldb、opencc 等都以 `.a` 静态库形式链接进来（这块属于打包话题，本讲点到为止，详见 [`CMakeLists.txt:44-54`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L44-L54)，深度版本在 `u6-l1`）。

编译目标的定义只有两行，却决定了「最终产物是什么」：

```cmake
aux_source_directory(. IBUS_RIME_SRC)
add_executable(ibus-engine-rime ${IBUS_RIME_SRC})
target_link_libraries(ibus-engine-rime ${IBus_LIBRARIES} ${LIBNOTIFY_LIBRARIES} ${Rime_LIBRARIES} ${RIME_DEPS})
```

- `aux_source_directory(. ...)` 把**当前目录（项目根）下所有 `.c` 文件**收进变量 `IBUS_RIME_SRC`。也就是说 `rime_main.c`、`rime_engine.c`、`rime_settings.c` 都会被一起编译。
- `add_executable(ibus-engine-rime ...)` 把它们编成一个名叫 `ibus-engine-rime` 的可执行文件。这个名字很重要：IBus 正是通过它来启动前端的（见 `rime.xml` 里的 `--ibus` 参数）。
- `target_link_libraries` 把三组库链上去：`${IBus_LIBRARIES}`、`${LIBNOTIFY_LIBRARIES}`、`${Rime_LIBRARIES}`，外加静态构建时的 `${RIME_DEPS}`。

参见 [`CMakeLists.txt:56-58`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L56-L58)。

最后是四条安装规则，决定了「make install 之后东西去了哪」：

```cmake
install(FILES .../rime.xml DESTINATION "${CMAKE_INSTALL_DATADIR}/ibus/component")
install(TARGETS ibus-engine-rime DESTINATION "${CMAKE_INSTALL_LIBEXECDIR}/ibus-rime")
install(DIRECTORY icons DESTINATION "${CMAKE_INSTALL_DATADIR}/ibus-rime" FILES_MATCHING PATTERN "*.png")
install(FILES ibus_rime.yaml DESTINATION "${RIME_DATA_DIR}")
```

| 产物 | 安装目的地（默认 `PREFIX=/usr`） |
| --- | --- |
| 组件描述 `rime.xml` | `/usr/share/ibus/component/rime.xml` |
| 可执行文件 `ibus-engine-rime` | `/usr/lib/ibus-rime/ibus-engine-rime` |
| 图标 `icons/*.png` | `/usr/share/ibus-rime/icons/` |
| 默认配置 `ibus_rime.yaml` | `$RIME_DATA_DIR`（例如 `/usr/share/rime-data`） |

这里的 `${CMAKE_INSTALL_DATADIR}`、`${CMAKE_INSTALL_LIBEXECDIR}` 来自第 8 行的 `include(GNUInstallDirs)`，是 GNU 标准的安装目录变量。参见 [`CMakeLists.txt:65-68`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L65-L68)。

#### 4.1.4 代码实践（源码阅读型）

**目标**：不运行任何命令，仅靠阅读 `CMakeLists.txt` 把「五件事」对上号。

**步骤**：

1. 打开 [`CMakeLists.txt`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt)。
2. 用笔把文件按行号划分成五个区段：声明项目、发现依赖、生成配置头、编译目标、安装规则。
3. 回答：为什么 `configure_file(rime_config.h.in ...)`（第 37-40 行）必须出现在 `add_executable`（第 57 行）之前？

**预期结果**：你能说清楚「源码 `#include "rime_config.h"`，所以这个头文件必须先于编译被生成出来」这条因果链。这属于纯阅读，无需本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把第 4 行的版本号改成 `set(ibus_rime_version 1.7.0)`，重新构建后，程序运行时报告的版本会变吗？

**答案**：会。因为 `ibus_rime_version` 会被 `configure_file` 注入 `rime_config.h` 的 `IBUS_RIME_VERSION` 宏（见 4.3），而源码里读取的就是这个宏。

**练习 2**：`aux_source_directory(. IBUS_RIME_SRC)` 用的是 `.`（当前目录）。如果你在项目根新建一个 `subdir/extra.c`，它会被编译进 `ibus-engine-rime` 吗？

**答案**：不会。`aux_source_directory` 只扫描指定目录（这里是项目根）下的源文件，不会递归进子目录。要让子目录的源码参与编译，需要显式列出或改用递归收集。

### 4.2 pkg-config 依赖发现

#### 4.2.1 概念说明

ibus-rime 编译期依赖三组库（与 `u1-l1` 介绍的生态一致）：

- **libibus-1.0**：IBus 框架的 C API，前端用它注册引擎、与 IBus 守护进程通信。
- **libnotify**：桌面通知库，部署完成时弹通知给用户。
- **librime**：Rime 核心引擎，负责真正的按键处理与查词。
- **rime-data**：不是「库」，而是输入方案数据目录（由 plum 产出），运行期需要。

前两个用 `pkg_check_modules`（pkg-config 机制）发现；librime 用 `find_package(Rime)`；rime-data 用项目自定义的 `find_package(RimeData)`。三种方式各有原因，下面逐一拆解。

#### 4.2.2 核心流程

```
IBus:       pkg_check_modules(IBus REQUIRED ibus-1.0)
              └─ 读取系统 /usr/lib/x86_64-linux-gnu/pkgconfig/ibus-1.0.pc
libnotify:  pkg_check_modules(LIBNOTIFY REQUIRED libnotify)
              └─ 读取 libnotify.pc
librime:    find_package(Rime REQUIRED)
              └─ 由 librime 自带的 CMake 配置模块提供（librime 安装时会装 FindRime.cmake）
rime-data:  find_package(RimeData REQUIRED)   ← 项目自定义模块
              └─ 调用 cmake/FindRimeData.cmake，按候选目录清单逐个探测
```

注意一个关键差异：`IBus_FOUND` / `LIBNOTIFY_FOUND` 这种「`<名字>_FOUND`」变量是 `pkg_check_modules` 的命名约定（名字由我们传入的大写参数决定）；而 `Rime_FOUND` 是 `find_package` 的约定。

#### 4.2.3 源码精读

**启用 pkg-config 并发现 IBus：**

```cmake
include(${CMAKE_ROOT}/Modules/FindPkgConfig.cmake)
pkg_check_modules(IBus REQUIRED ibus-1.0)
if(IBus_FOUND)
  include_directories(${IBus_INCLUDE_DIRS})
  link_directories(${IBus_LIBRARY_DIRS})
endif(IBus_FOUND)
```

- 第 12 行手动 `include` 了 CMake 自带的 `FindPkgConfig.cmake`（这一行其实可有可无，CMake 通常自动加载，但写出来更明确）。
- `pkg_check_modules(IBus REQUIRED ibus-1.0)`：第一个参数 `IBus` 是**前缀**，CMake 会据此生成 `IBus_INCLUDE_DIRS`、`IBus_LIBRARY_DIRS`、`IBus_LIBRARIES`、`IBus_FOUND` 等变量；`REQUIRED` 表示找不到就直接报错中止；最后的 `ibus-1.0` 是 `.pc` 文件名。
- 找到后，把头文件目录加进编译搜索路径（`include_directories`），把库目录加进链接搜索路径（`link_directories`）。

参见 [`CMakeLists.txt:12-17`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L12-L17)。libnotify 的处理完全同构，参见 [`CMakeLists.txt:19-23`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L19-L23)。

**librime 的发现方式不同：**

```cmake
find_package(Rime REQUIRED)
if(Rime_FOUND)
  include_directories(${Rime_INCLUDE_DIR})
endif(Rime_FOUND)
```

这里没用 `pkg_check_modules`，而是 `find_package(Rime)`。原因是 **librime 在安装时会自带一个 CMake 查找模块（`FindRime.cmake`）**，专门负责暴露 `Rime_INCLUDE_DIR`、`Rime_LIBRARIES` 等变量。注意它用的是 `Rime_INCLUDE_DIR`（单数 `DIR`），而 pkg-config 那组用的是复数 `IBus_INCLUDE_DIRS`——这是两套机制各自的习惯，容易看走眼。

> 说明：本仓库 `cmake/` 目录下只有 `FindRimeData.cmake`，**没有** `FindRime.cmake`。`FindRime.cmake` 是 librime 这个**外部依赖**自带的，随 librime 开发包一起安装到系统，由 CMake 在标准模块路径里自动找到。如果构建报 `Could NOT find Rime`，通常是系统没装 librime 的开发包。

参见 [`CMakeLists.txt:25-28`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L25-L28)。

**rime-data 的发现，以及一个「覆盖」机制：**

```cmake
if(NOT DEFINED RIME_DATA_DIR)
  find_package(RimeData REQUIRED)
endif(NOT DEFINED RIME_DATA_DIR)
message(STATUS "Precompiler macro RIME_DATA_DIR is set to \"${RIME_DATA_DIR}\"")
add_definitions(-DRIME_DATA_DIR="${RIME_DATA_DIR}")
```

逻辑是：**如果用户没在命令行用 `-DRIME_DATA_DIR=...` 指定数据目录，就调用自定义模块自动探测；否则尊重用户的值。** 这种 `if(NOT DEFINED ...)` 写法是 CMake 里实现「参数可覆盖默认值」的标准套路。探测到的目录随后通过 `add_definitions(-DRIME_DATA_DIR="...")` 变成一个**编译期宏**，源码里可以直接引用 `RIME_DATA_DIR` 字符串字面量。

参见 [`CMakeLists.txt:30-34`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L30-L34)。

那 `find_package(RimeData)` 到底怎么探测？答案在自定义模块 [`cmake/FindRimeData.cmake`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/cmake/FindRimeData.cmake)：

```cmake
set(RIME_DATA_FIND_DIR "${CMAKE_INSTALL_PREFIX}/share/rime-data"
                       "${CMAKE_INSTALL_PREFIX}/share/rime/data"
                       "/usr/share/rime-data"
                       "/usr/share/rime/data")
set(RIME_DATA_FOUND FALSE)
foreach(_RIME_DATA_DIR ${RIME_DATA_FIND_DIR})
    if (IS_DIRECTORY ${_RIME_DATA_DIR})
      set(RIME_DATA_FOUND True)
      set(RIME_DATA_DIR ${_RIME_DATA_DIR})
    endif (IS_DIRECTORY ${_RIME_DATA_DIR})
endforeach(_RIME_DATA_DIR)
```

它就是维护一张候选目录清单（覆盖了「安装前缀下」和「`/usr/share` 下」两种常见位置，以及 `rime-data` / `rime/data` 两种命名），逐个用 `IS_DIRECTORY` 判断是否存在，**第一个存在的就胜出**。最后用 `find_package_handle_standard_args` 把结果标准化成 `RimeData_FOUND` 变量。参见 [`cmake/FindRimeData.cmake:7-22`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/cmake/FindRimeData.cmake#L7-L22)。

#### 4.2.4 代码实践（源码阅读型 + 可选本地验证）

**目标**：弄清三组依赖分别由哪种机制发现，以及当 rime-data 不在默认位置时怎么办。

**步骤**：

1. 阅读 [`CMakeLists.txt:12-34`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L12-L34)，把「依赖 → 发现机制 → 产生的变量」填进下表：

   | 依赖 | 发现机制 | 产生的关键变量 |
   | --- | --- | --- |
   | IBus | `pkg_check_modules` | `IBus_LIBRARIES` 等 |
   | libnotify | `pkg_check_modules` | `LIBNOTIFY_LIBRARIES` 等 |
   | librime | `find_package(Rime)` | `Rime_INCLUDE_DIR`、`Rime_LIBRARIES` |
   | rime-data | `find_package(RimeData)` | `RIME_DATA_DIR` |

2. 对照官方依赖清单 [`README.md:15-22`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/README.md#L15-L22)，确认 build dependencies 与上面四项一一对应（其中 plum 提供 rime-data）。

3. **可选本地验证**：若你的机器上 rime-data 装在非默认目录（例如 `/opt/rime-data`），可执行：
   ```
   cmake -DRIME_DATA_DIR=/opt/rime-data ..
   ```
   观察 CMake 输出中 `Precompiler macro RIME_DATA_DIR is set to ...` 这一行（由第 33 行的 `message` 打印）是否变成了你指定的值。**若本机未装齐依赖，此步为「待本地验证」。**

**预期结果**：你能解释为什么 IBus/libnotify 用 pkg-config、而 librime 用 `find_package`——前者是标准系统库带 `.pc`，后者是 librime 自己提供了 CMake 查找模块。

#### 4.2.5 小练习与答案

**练习 1**：构建时报错 `Could NOT find Rime`，最可能的原因是什么？

**答案**：系统没有安装 librime 的开发包（devel/dev 包）。`find_package(Rime)` 依赖 librime 安装时自带的 `FindRime.cmake` 及其头文件/库，缺开发包就找不到。解决：安装 `librime-dev`（Debian/Ubuntu 系）或对应发行版的开发包。

**练习 2**：`pkg_check_modules(IBus ...)` 里的 `IBus` 这个词，如果改成小写 `ibus`，会出什么问题？

**答案**：CMake 生成的变量名会随之变成 `ibus_INCLUDE_DIRS`、`ibus_LIBRARIES`、`ibus_FOUND`。而后面 `target_link_libraries` 用的是 `${IBus_LIBRARIES}`（大写），就会引用到一个未定义变量，导致链接时缺少 IBus 库。所以这个前缀必须前后一致。

**练习 3**：为什么 rime-data 的发现要先包一层 `if(NOT DEFINED RIME_DATA_DIR)`？

**答案**：为了让用户能用命令行 `-DRIME_DATA_DIR=...` 覆盖自动探测的结果。只有用户没显式指定时，才调用 `find_package(RimeData)` 去猜。这是 CMake 里实现「有默认、可覆盖」的惯用法。

### 4.3 configure_file 模板生成（rime_config.h 与 rime.xml）

#### 4.3.1 概念说明

C 源码在编译时需要知道一些「安装后才能确定」的值，例如：

- 版本号（写源码时就定了，但要注入到代码里）。
- 图标安装到了哪个绝对路径（取决于 `PREFIX`）。
- 共享数据目录在哪（取决于 rime-data 装在哪）。

这些值不能写死在源码里，因为不同发行版路径不同。解决办法就是：写一个模板 `rime_config.h.in`，用 `@占位符@` 表示待定值；CMake 在配置阶段用真实值替换，生成 `build/rime_config.h`；源码再 `#include` 这个生成出来的头文件。完成这件事的命令就是 `configure_file`。

`rime.xml.in` 同理，只不过它产出的不是头文件，而是给 IBus 看的组件描述文件 `rime.xml`。

#### 4.3.2 核心流程

```
模板 rime_config.h.in            模板 rime.xml.in
   含 @ibus_rime_version@           含 @CMAKE_INSTALL_FULL_LIBEXECDIR@ 等
        │                                │
        │  configure_file(@ONLY)         │  configure_file(@ONLY)
        ▼                                ▼
 build/rime_config.h            build/rime.xml
   含真实宏定义                   含真实安装路径
        │                                │
        ▼                                ▼
 被 rime_*.c #include            被 install 到 ibus/component/
```

`@ONLY` 参数的作用是：**只替换 `@VAR@` 形式的占位符，不碰 `${VAR}` 形式**。这能避免模板里那些本意是字面量的 `${...}` 被误替换，对生成 C 头文件尤其重要。

#### 4.3.3 源码精读

先看模板 [`rime_config.h.in`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_config.h.in) 全文，它只有三个有效宏：

```c
#define IBUS_RIME_VERSION "@ibus_rime_version@"
#define IBUS_RIME_ICONS_DIR "@ibus_rime_icons_dir@"
#define IBUS_RIME_SHARED_DATA_DIR "@RIME_DATA_DIR@"
```

参见 [`rime_config.h.in:4-7`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_config.h.in#L4-L7)。注意三个占位符：

- `@ibus_rime_version@` ← CMakeLists.txt 第 4 行的 `set(ibus_rime_version 1.6.1)`。
- `@ibus_rime_icons_dir@` ← CMakeLists.txt 第 36 行算出来的图标安装目录。
- `@RIME_DATA_DIR@` ← 4.2 节探测到的数据目录（或用户传入的值）。

再看 `CMakeLists.txt` 里是怎么算出图标目录、又怎么触发替换的：

```cmake
set(ibus_rime_icons_dir "${CMAKE_INSTALL_FULL_DATADIR}/ibus-rime/icons")
configure_file(
  "${CMAKE_CURRENT_SOURCE_DIR}/rime_config.h.in"
  "${CMAKE_CURRENT_BINARY_DIR}/rime_config.h"
  @ONLY)
include_directories("${CMAKE_CURRENT_BINARY_DIR}")
```

- 第 36 行：图标目录 = 数据共享目录 + `/ibus-rime/icons`。`${CMAKE_INSTALL_FULL_DATADIR}` 是 `GNUInstallDirs` 提供的**绝对路径**版本（默认 `/usr/share`），所以默认值是 `/usr/share/ibus-rime/icons`。
- `configure_file` 把模板读进来、替换 `@...@`、写到构建目录 `build/rime_config.h`。
- 第 42 行把构建目录加进头文件搜索路径，这样源码里 `#include "rime_config.h"` 才能找到这个**生成出来的**头文件（它不在源码树里，而在 build 树里）。

参见 [`CMakeLists.txt:36-42`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L36-L42)。

> **三个宏的值来源小结（本讲实践任务的核心）：**
>
> | 宏 | 模板占位符 | CMake 变量 | 在哪赋值 | 默认值（PREFIX=/usr） |
> | --- | --- | --- | --- | --- |
> | `IBUS_RIME_VERSION` | `@ibus_rime_version@` | `ibus_rime_version` | `CMakeLists.txt:4` | `1.6.1` |
> | `IBUS_RIME_ICONS_DIR` | `@ibus_rime_icons_dir@` | `ibus_rime_icons_dir` | `CMakeLists.txt:36` | `/usr/share/ibus-rime/icons` |
> | `IBUS_RIME_SHARED_DATA_DIR` | `@RIME_DATA_DIR@` | `RIME_DATA_DIR` | `FindRimeData.cmake` 探测 / 用户 `-D` 覆盖 | `/usr/share/rime-data` 等 |

`rime.xml` 的生成方式完全一样：

```cmake
configure_file(
  "${CMAKE_CURRENT_SOURCE_DIR}/rime.xml.in"
  "${CMAKE_CURRENT_BINARY_DIR}/rime.xml"
  @ONLY)
```

模板 [`rime.xml.in`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime.xml.in) 里用 `@CMAKE_INSTALL_FULL_LIBEXECDIR@`、`@CMAKE_INSTALL_FULL_DATADIR@` 等占位符，生成后得到真实路径，例如可执行文件启动命令会被填成 `/usr/lib/ibus-rime/ibus-engine-rime --ibus`。参见 [`CMakeLists.txt:60-63`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L60-L63) 与 [`rime.xml.in:6`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime.xml.in#L6)。

#### 4.3.4 代码实践（源码阅读型 + 可选本地验证）

**目标**：亲手追踪「三个宏的值来源」这条链路。

**步骤**：

1. 打开 [`rime_config.h.in`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_config.h.in)，记下三个 `@...@` 占位符。
2. 在 [`CMakeLists.txt`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt) 里分别定位这三个占位符对应的 CMake 变量是在哪一行被赋值的（答案见上表）。
3. **可选本地验证**：完成一次构建后，打开 `build/rime_config.h`，对照上表，确认三个宏的值与你预期一致。例如 `IBUS_RIME_VERSION` 应为 `"1.6.1"`。**若本机未装齐依赖无法构建，则此步为「待本地验证」；但你完全可以通过阅读源码推算出预期值。**

**预期结果**：你能不看本讲，仅凭源码说出「`IBUS_RIME_SHARED_DATA_DIR` 的值来自 `FindRimeData.cmake` 的探测结果或命令行 `-DRIME_DATA_DIR`」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `configure_file` 这里要加 `@ONLY`？

**答案**：`@ONLY` 限定只替换 `@VAR@` 形式，不替换 `${VAR}` 形式。在生成 C 头文件时，可以避免把模板里本应作为字面量的 `${...}` 误当作 CMake 变量展开，更安全。

**练习 2**：假设你用 `make PREFIX=/opt/ibus` 重新构建安装，`IBUS_RIME_ICONS_DIR` 会变成什么？

**答案**：会变成 `/opt/ibus/share/ibus-rime/icons`。因为 `ibus_rime_icons_dir = ${CMAKE_INSTALL_FULL_DATADIR}/ibus-rime/icons`，而 `CMAKE_INSTALL_FULL_DATADIR` 受 `PREFIX` 影响（Makefile 把 `PREFIX` 透传成 `CMAKE_INSTALL_DATADIR` 的根）。

### 4.4 Makefile 包装层

#### 4.4.1 概念说明

`CMakeLists.txt` 描述了「怎么构建」，但真正触发它需要在 `build/` 目录里敲一长串 `cmake -D... .. && make`。为了让贡献者一行命令搞定，项目根放了一个极简的 [`Makefile`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/Makefile)。它不是「构建系统」，只是 CMake 的一层「糖衣」。

注意：这个 `Makefile` 与 CMake 生成的 `build/Makefile` 是**两个不同的文件**。前者在项目根，是你手敲 `make` 时用的；后者在 `build/` 里，是 CMake 生成、真正驱动编译的。

#### 4.4.2 核心流程

```
用户: make            ─┐
用户: make install     │   外层 Makefile（项目根）
用户: make uninstall   │     ├─ 设置 PREFIX / builddir 默认值
                       └────▶├─ make all → 进入 build/ 跑 cmake + make
                             ├─ make install → 进入 build/ 跑 make install
                             └─ make uninstall → 按 install_manifest.txt 删除
                                                        │
                                                        ▼
                                              build/Makefile（CMake 生成）
                                              真正编译 / 安装
```

#### 4.4.3 源码精读

先看变量默认值：

```makefile
ifeq (${PREFIX},)
	PREFIX=/usr
endif
sharedir = $(PREFIX)/share
libexecdir = $(PREFIX)/lib

ifeeq (${builddir},)
	builddir=build
endif
```

- `PREFIX` 默认 `/usr`，可以被环境变量或 `make PREFIX=...` 覆盖。
- `sharedir`、`libexecdir` 由 `PREFIX` 派生，对应 CMake 的 `CMAKE_INSTALL_DATADIR`、`CMAKE_INSTALL_LIBEXECDIR`。
- `builddir` 默认 `build`，也就是构建产物所在的目录。

参见 [`Makefile:1-9`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/Makefile#L1-L9)。

核心构建目标 `ibus-engine-rime`（也是 `make`/`make all` 的默认目标）：

```makefile
all: ibus-engine-rime

ibus-engine-rime:
	mkdir -p $(builddir)
	(cd $(builddir); cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_DATADIR=$(sharedir) -DCMAKE_INSTALL_LIBEXECDIR=$(libexecdir) .. && make)
	@echo ':)'
```

解读：

1. `mkdir -p build`——确保构建目录存在。
2. `cd build` 后执行 `cmake .. && make`——在构建目录里「向外」指 `..`（项目根）配置，然后立刻编译。这种「在子目录里配置」是 CMake 推荐的 **out-of-source build** 做法，让源码树保持干净，构建产物全在 `build/`。
3. 透传了三个参数：`CMAKE_BUILD_TYPE=Release`（发布版优化）、`CMAKE_INSTALL_DATADIR`、`CMAKE_INSTALL_LIBEXECDIR`（让安装目录跟 `PREFIX` 走）。
4. 末尾 `@echo ':)'` 是个友好提示，构建成功会打印一个笑脸。

参见 [`Makefile:11-16`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/Makefile#L11-L16)。

静态构建目标只多了一个 `-DBUILD_STATIC=ON`：

```makefile
ibus-engine-rime-static:
	mkdir -p $(builddir)
	(cd $(builddir); cmake ... -DBUILD_STATIC=ON .. && make)
```

这会触发 4.1 里提到的 `BUILD_STATIC` 分支。参见 [`Makefile:18-21`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/Makefile#L18-L21)。

安装与卸载：

```makefile
install:
	(cd $(builddir); make install)

uninstall:
	(cd $(builddir); xargs rm < install_manifest.txt)

clean:
	if  [ -e $(builddir) ]; then rm -R $(builddir); fi
```

- `make install` 进入 `build/` 跑 CMake 生成的 `make install`，执行 `CMakeLists.txt` 第 65-68 行那四条安装规则。
- `make uninstall` 很巧妙：CMake 在安装时会记录一个 `install_manifest.txt`（安装了哪些文件的清单），这里用它喂给 `xargs rm`，把装上去的文件逐一删掉。
- `make clean` 直接删掉整个 `build/` 目录。

参见 [`Makefile:23-30`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/Makefile#L23-L30)。

#### 4.4.4 代码实践（构建型，主线实践）

> 这是本讲的**主线实践**，对应任务里「执行 make 完成 build 目录构建，指出可执行文件生成路径」。

**目标**：用 Makefile 包装层完成一次构建，定位产物，并验证配置头文件。

**前置条件**：已按 [`README.md:15-22`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/README.md#L15-L22) 安装好构建依赖（`pkg-config`、`cmake>=3.10`、librime 开发包、libibus-1.0 开发包、libnotify 开发包，以及 plum 提供的 rime-data）。

**操作步骤**：

1. 在项目根执行：
   ```
   make
   ```
   等价于 Makefile 里 `mkdir -p build && (cd build; cmake -DCMAKE_BUILD_TYPE=Release ... .. && make)`。
2. 观察输出。成功的标志是末尾打印 `:)`，且过程中没有 `Could NOT find ...` 之类的错误。
3. 定位可执行文件：
   ```
   ls -l build/ibus-engine-rime
   ```
4. 检查生成的配置头文件：
   ```
   cat build/rime_config.h
   ```
5. （可选）验证版本号：`build/ibus-engine-rime --version`（行为以实际为准，**待本地验证**该子命令是否被支持；若不支持，直接看 `rime_config.h` 里的 `IBUS_RIME_VERSION` 即可）。

**需要观察的现象**：

- CMake 配置阶段会打印一行 `-- Precompiler macro RIME_DATA_DIR is set to "..."`（来自 `CMakeLists.txt:33`），告诉你数据目录探测结果。
- `build/` 目录下应出现 `ibus-engine-rime`、`rime_config.h`、`rime.xml` 三个产物。

**预期结果**：

- 可执行文件路径：**`build/ibus-engine-rime`**（由 `add_executable` 生成在构建目录根）。注意这是**构建期**位置；执行 `make install` 后，它会被复制到安装期位置 **`/usr/lib/ibus-rime/ibus-engine-rime`**（由 `CMakeLists.txt:66` 的 install 规则决定）。
- `build/rime_config.h` 三个宏的值（以默认 `PREFIX=/usr`、rime-data 在 `/usr/share/rime-data` 为例）：
  - `IBUS_RIME_VERSION` → `"1.6.1"`（来自 `CMakeLists.txt:4`）。
  - `IBUS_RIME_ICONS_DIR` → `"/usr/share/ibus-rime/icons"`（来自 `CMakeLists.txt:36`）。
  - `IBUS_RIME_SHARED_DATA_DIR` → `"/usr/share/rime-data"`（来自 `FindRimeData.cmake` 探测，具体值取决于你机器上 rime-data 实际位置）。

> **若本机缺少依赖**：构建会在 `find_package` 或 `pkg_check_modules` 处失败。这是正常的——本实践需要一台装齐了 Rime 开发依赖的 Linux 环境。在这种情况下，上面的「预期结果」可通过纯源码阅读推得，运行结果标记为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`make` 和 `make ibus-engine-rime` 效果一样吗？为什么？

**答案**：一样。因为 `all: ibus-engine-rime` 把 `all` 设为目标，而 `make` 不带参数时默认构建 Makefile 里的**第一个目标**，这里第一个目标就是 `all`。

**练习 2**：`make uninstall` 是怎么知道要删哪些文件的？

**答案**：CMake 的 `make install` 会把所有被安装文件的完整路径写进 `build/install_manifest.txt`。`make uninstall` 用 `xargs rm < install_manifest.txt` 把这个清单喂给 `rm`，逐一删除。

**练习 3**：为什么 Makefile 要 `cd $(builddir)` 再 `cmake ..`，而不是直接在项目根跑 `cmake`？

**答案**：这是 **out-of-source build**。把构建产物隔离在 `build/` 子目录里，源码树保持干净，`git status` 不会被大量生成文件污染（项目 `.gitignore` 也忽略了 `build/`）。如果直接在项目根配置，`CMakeCache.txt`、`CMakeFiles/` 等会散落在源码树里。

## 5. 综合实践

把本讲四个模块串起来，完成一次「从依赖到产物」的完整追踪。

**任务**：假设你要在一台全新的 Linux 上从源码构建并安装 ibus-rime，请完成下列步骤并记录每一步对应的源码位置。

1. **核对依赖**：参照 [`README.md:15-30`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/README.md#L15-L30)，列出需要安装的构建依赖与运行依赖，标注哪些由 plum 提供。
2. **触发构建**：执行 `make`。在 CMake 输出中找到 `RIME_DATA_DIR` 那行（对应 `CMakeLists.txt:33`），记下你机器上探测到的数据目录。
3. **定位产物**：指出构建期可执行文件路径（`build/ibus-engine-rime`）。
4. **验证配置头**：打开 `build/rime_config.h`，分别说明 `IBUS_RIME_VERSION`、`IBUS_RIME_ICONS_DIR`、`IBUS_RIME_SHARED_DATA_DIR` 三个宏的值及其来源（回溯到 `CMakeLists.txt` 的具体行号）。
5. **（可选）安装**：执行 `make install`，然后确认 `/usr/lib/ibus-rime/ibus-engine-rime`、`/usr/share/ibus/component/rime.xml`、`/usr/share/ibus-rime/icons/` 都已就位（对应 `CMakeLists.txt:65-68` 的四条 install 规则）。

**交付物**：一张表，把「步骤 → 触发的源码位置 → 观察到的现象/产物」对应起来。这张表就是本讲构建链路的「地图」，后面读启动流程（`u2`）时会反复用到其中的路径与产物。

> 若当前环境无法装齐依赖，第 2、3、5 步的运行结果标记为「待本地验证」，但第 1、4 步的「值来源」可完全通过阅读源码完成。

## 6. 本讲小结

- ibus-rime 用 **CMake** 作为构建系统，根目录的 `Makefile` 只是对 `cmake` 命令的薄封装，让用户用 `make` / `make install` 一行命令搞定。
- 四组依赖用**三种不同机制**发现：IBus、libnotify 用 `pkg_check_modules`（pkg-config）；librime 用 `find_package(Rime)`（依赖 librime 自带的查找模块）；rime-data 用项目自定义的 [`cmake/FindRimeData.cmake`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/cmake/FindRimeData.cmake) 按候选目录清单探测，且可被 `-DRIME_DATA_DIR` 覆盖。
- `configure_file(@ONLY)` 把模板 [`rime_config.h.in`](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_config.h.in) 加工成 `build/rime_config.h`：`IBUS_RIME_VERSION` 来自 `CMakeLists.txt:4` 的 `1.6.1`，`IBUS_RIME_ICONS_DIR` 来自第 36 行的 `${CMAKE_INSTALL_FULL_DATADIR}/ibus-rime/icons`，`IBUS_RIME_SHARED_DATA_DIR` 来自 `RIME_DATA_DIR`。
- 最终产物是可执行文件 **`ibus-engine-rime`**，构建期位于 `build/`，安装期位于 `${CMAKE_INSTALL_LIBEXECDIR}/ibus-rime/`（默认 `/usr/lib/ibus-rime/`）。
- 四条 install 规则分别安装：组件描述 `rime.xml`（到 `ibus/component/`）、可执行文件（到 `lib/ibus-rime/`）、图标（到 `share/ibus-rime/icons/`）、默认配置 `ibus_rime.yaml`（到 `RIME_DATA_DIR`）。
- 采用 **out-of-source build**：所有构建产物隔离在 `build/`，源码树保持干净，`.gitignore` 也忽略了 `build/`。

## 7. 下一步学习建议

到这里，你已经知道 ibus-rime **怎么被编译出来**、产物 `ibus-engine-rime` 装在哪里。但它被 IBus 启动后，**程序内部的第一行代码做了什么**？这正是下一单元 U2 的主题。

建议接下来的学习顺序：

1. **`u1-l3` 目录结构与源码地图**：先建立三个 C 源文件（`rime_main.c` / `rime_engine.c` / `rime_settings.c`）的职责地图，以及 `rime.xml` 如何向 IBus 注册组件——这是进入 U2 前的最后一块入门拼图。
2. **`u2-l1` main 入口与进程生命周期**：从 `main()` 出发，看 `ibus-engine-rime` 启动后如何处理信号、获取 librime 的 API、完成初始化。本讲提到的可执行文件路径和 `--ibus` 参数，会在那里被真正「接通」。
3. 如果你对**静态构建与打包**更感兴趣（本讲略过的 `BUILD_STATIC` 分支），可以直接跳到 `u6-l1`，但建议先完成 U2 的启动流程阅读，建立全局观。

阅读源码时，记得随时回到本讲的「值来源小结表」和「install 目的地表」——它们是后续追踪运行时行为（例如程序去哪里找图标、去哪里读 `ibus_rime.yaml`）的索引。
