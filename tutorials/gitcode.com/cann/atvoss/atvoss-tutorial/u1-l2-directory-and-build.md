# 目录结构与构建运行

## 1. 本讲目标

上一讲（u1-l1）我们已经建立了对 ATVOSS「是什么、为什么、能在哪用」的总体认知。本讲解决的是紧接着的一个问题：**这套代码长什么样、怎么把它编出来、编完之后产物在哪**。

学完本讲，你应当能够：

- 说出 ATVOSS 仓库根目录、`include/`、`examples/`、`tests/` 各目录的职责划分。
- 读懂顶层 `CMakeLists.txt` 是如何把 `examples` 与 `tests` 两个子工程串起来的。
- 熟练使用 `scripts/build.sh` 的三种模式（`--example` / `--host_ut` / `--st`）与 `-DSOC` 参数，编出一个算子。
- 指出编译产物落在 `output/bin/` 下，并知道在真机与仿真两种环境下如何运行它。

本讲不深入任何算子实现细节，只解决「目录 + 构建 + 运行」这条工程链路。

## 2. 前置知识

在开始之前，先建立几个最基础的工程概念。如果你已经熟悉 CMake 与编译流程，可以跳过本节。

- **CMake**：一个跨平台的「构建系统生成器」。它本身不编译代码，而是根据 `CMakeLists.txt` 这个配置文件，生成具体的 Makefile / Ninja 文件，再由后者真正调用编译器。所以 ATVOSS 的编译总是分两步：先 `cmake` 配置（configure），再 `cmake --build` 构建（build）。
- **target（构建目标）**：CMake 里的一个概念，可以理解成「一个要产出的东西」，比如一个可执行文件 `abs`、一个聚合目标 `atvoss_examples`、或者一个测试目标 `host_ut`。我们让 `build.sh` 编译什么，本质就是指定一个 target 名。
- **SOC（System On Chip）**：这里指昇腾 AI 处理器的型号。ATVOSS 目前只支持 `ascend950`，它会被映射成编译器里的 `--npu-arch=dav-3510`。
- **bisheng（毕昇编译器）**：昇腾 CANN 工具链自带的 C/C++ 编译器，对应命令名 `bisheng`。ATVOSS 的算子代码不是用普通 `gcc/g++` 编的，而是用 `bisheng`，它会理解 `--npu-arch`、`-xasc` 这类昇腾专用选项。
- **cannsim（仿真器）**：CANN 提供的指令级仿真工具。没有真实 NPU 硬件时，可以用 `cannsim record ./run.sh -s Ascend950 --gen-report` 来「假装」在芯片上跑一遍算子，验证功能与精度。
- **ASCEND_HOME_PATH**：一个环境变量，指向本机 CANN 工具链的安装目录。它是编译 ATVOSS 的硬性前置条件。

如果上面这些名词暂时觉得抽象，没关系，后面结合源码看一遍就清楚了。

## 3. 本讲源码地图

本讲涉及的文件都以「工程组织 / 构建」为主，不涉及算子计算逻辑本身：

| 文件 / 目录 | 作用 |
|---|---|
| [docs/directory_structure.md](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/directory_structure.md) | 官方目录结构说明文档（本讲的「地图」原文）。 |
| [docs/quick_start.md](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/quick_start.md) | 官方快速入门，含环境、编译、UT/ST 的命令示例。 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/CMakeLists.txt) | 顶层 CMake 配置，串联 `examples` 与 `tests`。 |
| [scripts/build.sh](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh) | 一键构建脚本，封装了 cmake 配置/构建/安装/仿真运行。 |
| [examples/CMakeLists.txt](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/CMakeLists.txt) | 定义「算子样例」如何变成可执行文件。 |
| [examples/abs/CMakeLists.txt](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/CMakeLists.txt) | `abs` 样例的构建声明（仅一行宏调用）。 |
| [tests/CMakeLists.txt](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/CMakeLists.txt) | 测试子工程入口，串联 `ut` 与 `st`。 |
| [tests/st/CMakeLists.txt](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/CMakeLists.txt) | 用 glob 自动把所有 `test_*.cpp` 编成可执行文件。 |
| [cmake/CMakeASCEND.cmake](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/cmake/CMakeASCEND.cmake) | 定位 CANN 工具链与 bisheng 编译器的公共 CMake 片段。 |

## 4. 核心概念与源码讲解

### 4.1 顶层目录结构与各模块职责

#### 4.1.1 概念说明

ATVOSS 作为一个库（library），它的核心代码几乎全是 C++ 头文件（header-only 风格），放在 `include/` 下；而「怎么用这个库」则通过 `examples/` 与 `tests/` 里的可执行样例来演示。所以理解目录结构，本质上就是理解「**核心库代码在哪、使用样例在哪、构建脚本在哪、文档在哪**」这四件事。

官方在 [docs/directory_structure.md](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/directory_structure.md) 给出了一份目录说明。下面我们以**仓库真实结构**为准（该文档略滞后于代码，少数子目录未列出），逐一拆解。

#### 4.1.2 根目录布局

仓库根目录的关键内容如下（实测，节选）：

```
atvoss/
├── CMakeLists.txt        # 顶层 CMake 配置
├── README.md             # 项目说明
├── version.info          # 版本信息（Version=9.0.0）
├── cmake/                # 公共 CMake 片段（CMakeASCEND.cmake、third_party/）
├── docs/                 # 文档（quick_start / summary / api / tutorials）
├── examples/             # 算子样例（abs / muls / rms_norm / python_extension）
├── include/              # 核心头文件库（框架本体）
├── scripts/              # 构建脚本（build.sh、oat_check.sh）
└── tests/                # 测试工程（ut/ 单测、st/ 系统测试）
```

这与官方 [docs/directory_structure.md:9-19](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/directory_structure.md#L9-L19) 给出的根目录树一致。一句话概括每个顶层目录的职责：

| 目录 | 职责 |
|---|---|
| `include/` | ATVOSS 框架本体（头文件库），开发者 `#include "atvoss.h"` 就是用它。 |
| `examples/` | 教你怎么调用框架写算子并跑起来，是最佳学习材料。 |
| `tests/` | UT（主机侧单测）与 ST（仿真侧系统测试），用来回归验证框架。 |
| `cmake/` | 复用的 CMake 片段，比如定位 CANN 工具链。 |
| `scripts/` | 一键构建脚本 `build.sh`，本讲的主角。 |
| `docs/` | 全部文档：快速入门、分层介绍、API、开发指南。 |

#### 4.1.3 include/：框架本体的职责划分

`include/` 是 ATVOSS 最核心的部分。`directory_structure.md` 对它有一段较完整的说明（[docs/directory_structure.md:21-65](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/directory_structure.md#L21-L65)），但官方树略滞后，下面给出与仓库一致的实测划分：

| 子目录 | 职责 | 对应学习阶段 |
|---|---|---|
| `atvoss.h` | **主入口头文件**，用户只需 include 它一个。 | 入门 |
| `common/` | 公共类型与硬件信息：`type_def.h`、`arch.h`、`platform_info.h`。 | 进阶 |
| `expression/` | 表达式模板系统（`expr_template.h`），把计算编码进类型。 | 进阶 |
| `operators/` | 算子声明：`math_expression.h`、`tensor_expression.h`、各类 `*_evaluator.h`。 | 进阶 |
| `evaluator/` | 求值器基类（`eval_base.h`），把表达式真正执行出来。 | 专家 |
| `elewise/block/` `kernel/` `device/` `tile/` | 五层架构里的四层实现（Block / Kernel / Device / Tile）。 | 进阶→专家 |
| `elewise/graph/` | 逐元素算子的计算图构建（dag / bind / node / buffer）。 | 专家 |
| `reduce/graph/` | 归约/广播算子专用的图处理。 | 专家 |
| `graph/` | 与图无关的通用图优化 Pass（线性化、展平、Cast 消除等）。 | 专家 |
| `utils/` | 工具：`tensor.h`、`arguments/`、`buf_pool/`、`layout/` 等。 | 进阶 |

> 说明：五层架构（Device > Kernel > Block > Tile > Basic）的整体概念已在 u1-l1 提及，本讲只需知道「`include/elewise/` 下的 `device/`、`kernel/`、`block/`、`tile/` 正好对应其中四层」即可，逐层细节留待 u1-l3 与进阶篇。

#### 4.1.4 examples/、tests/、docs/

- **examples/**（[docs/directory_structure.md:67-86](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/directory_structure.md#L67-L86)）：目前提供四个样例目录——`abs`、`muls`、`rms_norm`、`python_extension`，外加一个公共头 `common/example_common.h`。前三个是 C++ 单算子样例，`python_extension` 演示如何把算子接入 PyTorch。每个样例目录里通常有「算子源码 `.cpp` + 自己的 `CMakeLists.txt` + `README.md`」三件套，例如 `examples/abs/` 下就是 `abs.cpp`、`CMakeLists.txt`、`README.md`。

- **tests/**（[docs/directory_structure.md:101-119](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/directory_structure.md#L101-L119)）：分为 `ut/`（单元测试，主机侧）与 `st/`（系统测试，仿真侧）。`ut/` 下又分 `host/`（如 `test_arguments.cpp`）、`builtin_kernel/`、`builtin_tiling/`、`compile_perf/`（编译性能）；`st/` 下是大量 `test_*.cpp`，覆盖单算子功能、Compute 结构、Tile/Block、Cast 消除等维度。注意官方文档只列了两个 `host/` 文件，实测有四个（`test_arguments.cpp`、`test_elewise_tiling.cpp`、`test_expr_linearizer.cpp`、`test_utility.cpp`）。

- **docs/**（[docs/directory_structure.md:88-99](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/directory_structure.md#L88-L99)）：`quick_start.md`（快速入门）、`summary.md`（分层介绍）、`directory_structure.md`（本讲原文）、`tutorials/developer_guide.md`（开发指南）、`api/`（每个 API 一篇小文档）、`images/`。

- **scripts/**（[docs/directory_structure.md:121-128](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/directory_structure.md#L121-L128)）：核心是 `build.sh`，此外还有 `oat_check.sh`（开源合规检查）。

#### 4.1.5 代码实践：用目录树自检

**实践目标**：亲手把上面这张「目录 → 职责」表与真实仓库对上号，建立空间感。

**操作步骤**：

1. 在仓库根目录执行 `find include -maxdepth 2 -type d | sort`，确认 4.1.3 表里列出的子目录都真实存在。
2. 执行 `ls examples/`，确认样例目录是 `abs / muls / rms_norm / python_extension / common`。
3. 执行 `find tests -maxdepth 2`，对比 `ut/` 与 `st/` 的划分，数一数 `st/` 下有多少个 `test_*.cpp`。

**需要观察的现象**：`include/elewise/` 下应当正好有 `block/`、`kernel/`、`device/`、`tile/`、`graph/` 五个子目录，与「五层架构占四层 + 图」的说法吻合。

**预期结果**：你能不查文档，直接说出「我想看 Device 层实现要去 `include/elewise/device/`」「我想看 abs 怎么调用要去 `examples/abs/abs.cpp`」。

### 4.2 顶层 CMakeLists.txt 如何串联 examples 与 tests

#### 4.2.1 概念说明

ATVOSS 用 CMake 组织工程。顶层 `CMakeLists.txt` 不写具体怎么编译某个算子，而是做两件事：**声明全局编译环境**（C++ 标准、SOC 默认值、引入 CANN 工具链片段），**再 `add_subdirectory` 把 `examples` 与 `tests` 两个子工程挂进来**。具体怎么把 `abs.cpp` 变成可执行文件，是子目录 `CMakeLists.txt` 的职责。这种「顶层定环境、子目录定产物」的分层，是 CMake 工程的常见写法。

#### 4.2.2 核心流程

顶层 CMake 的执行流程可以用下面伪代码概括：

```
1. cmake_minimum_required(3.16) + project(ATVOSS 1.0.0)
2. 设置全局属性：C++17、PIC、导出 compile_commands.json
3. include(cmake/third_party/gtest.cmake)        # 引入 googletest（UT 用）
4. include(cmake/CMakeASCEND.cmake)               # 定位 CANN 工具链与 bisheng
5. 若未指定 SOC，则默认 SOC=ascend950
6. add_subdirectory(examples)                     # 进入 examples/CMakeLists.txt
7. add_subdirectory(tests)                        # 进入 tests/CMakeLists.txt
```

#### 4.2.3 源码精读

顶层配置非常短，关键几行如下：

[CMakeLists.txt:11-13](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/CMakeLists.txt#L11-L13) —— 声明工程名 `ATVOSS`、版本 `1.0.0`：

```cmake
cmake_minimum_required(VERSION 3.16)
set(PKG_NAME ATVOSS)
project(${PKG_NAME} VERSION 1.0.0)
```

[CMakeLists.txt:17-19](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/CMakeLists.txt#L17-L19) —— 强制使用 C++17 标准（ATVOSS 大量使用模板与 `if constexpr`，依赖 C++17）：

```cmake
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
```

[CMakeLists.txt:21-22](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/CMakeLists.txt#L21-L22) —— 引入两段公共 CMake 片段：`gtest.cmake`（给 UT 提供 GoogleTest）与 `CMakeASCEND.cmake`（定位 CANN 工具链）：

```cmake
include(cmake/third_party/gtest.cmake)
include(cmake/CMakeASCEND.cmake)
```

其中 `CMakeASCEND.cmake` 会先从环境变量 `ASCEND_HOME_PATH`（或若干默认路径）找到 CANN 安装目录，再用 `find_program` 找到 bisheng 编译器，并把它设为 C/C++ 编译器与链接器（[cmake/CMakeASCEND.cmake:13-49](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/cmake/CMakeASCEND.cmake#L13-L49)）。它还组装了 `ASCEND_INCLUDE_DIRS`，把 Ascend C 的头文件路径全部加进来（[cmake/CMakeASCEND.cmake:52-60](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/cmake/CMakeASCEND.cmake#L52-L60)）。**这就是为什么编译前必须设置 `ASCEND_HOME_PATH`。**

[CMakeLists.txt:24-27](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/CMakeLists.txt#L24-L27) —— SOC 默认值：如果调用时没传 `-DSOC=...`，就给出警告并默认用 `ascend950`：

```cmake
if(NOT DEFINED SOC OR NOT SOC)
    message(WARNING "SOC is not defined, use default value \"ascend950\"")
    set(SOC ascend950)
endif()
```

[CMakeLists.txt:29-31](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/CMakeLists.txt#L29-L31) —— 把两个子工程挂进来：

```cmake
set(ATVOSS_DIR ${CMAKE_CURRENT_SOURCE_DIR})
add_subdirectory(examples)
add_subdirectory(tests)
```

`tests/CMakeLists.txt` 同样只是个「中转站」，继续往下挂 `ut` 与 `st`（[tests/CMakeLists.txt:10-12](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/CMakeLists.txt#L10-L12)）：

```cmake
include(../cmake/third_party/gtest.cmake)
add_subdirectory(ut)
add_subdirectory(st)
```

#### 4.2.4 代码实践：追踪 abs 的「注册链路」

**实践目标**：从顶层一路追到 `abs` 这个可执行目标是在哪里、被哪一行声明的。

**操作步骤**：

1. 打开 `CMakeLists.txt` 第 31 行 `add_subdirectory(examples)`。
2. 打开 `examples/CMakeLists.txt`，定位到末尾的循环（[examples/CMakeLists.txt:98-104](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/CMakeLists.txt#L98-L104)），可以看到 `abs` 在 `foreach(EXAMPLE abs rms_norm muls)` 里被 `add_subdirectory`。

   ```cmake
   foreach(EXAMPLE
       abs
       rms_norm
       muls
   )
       add_subdirectory(${EXAMPLE})
   endforeach()
   ```
3. 打开 `examples/abs/CMakeLists.txt`，整文件只有一行有效代码（[examples/abs/CMakeLists.txt:11](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/CMakeLists.txt#L11)）：

   ```cmake
   atvoss_example_add_executable(abs abs.cpp)
   ```

   这个宏 `atvoss_example_add_executable` 就定义在 `examples/CMakeLists.txt` 里（[examples/CMakeLists.txt:54-95](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/CMakeLists.txt#L54-L95)），它负责把 `bisheng` 设为编译器、加上 `--npu-arch=dav-3510 -xasc` 编译选项、链接 `ascendcl` 等库，并把产物安装到 `bin/` 目录。

**需要观察的现象**：新增一个样例目录后，必须同时满足两个条件才会被编译——① 在 `examples/CMakeLists.txt` 的 `foreach` 列表里加上它的名字；② 它自己目录里有 `CMakeLists.txt` 并调用了 `atvoss_example_add_executable`。

**预期结果**：你能解释「为什么 `bash scripts/build.sh -DSOC=ascend950 abs` 里的 `abs` 是一个合法 target」——因为它正是 `add_executable(abs abs.cpp)` 产生的目标名。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `python_extension` 没有出现在 `examples/CMakeLists.txt` 的 `foreach` 里？

**参考答案**：因为 `python_extension` 不是通过 CMake 直接编成单算子可执行文件，而是通过 `setup.py` + CMake 构建成一个 Python wheel 扩展（PyTorch C++ Extension）。它有自己的构建链路，不走 `atvoss_example_add_executable` 宏，所以不需要也不应该被加进这个 `foreach`。

**练习 2**：如果不传 `-DSOC`，编译会发生什么？

**参考答案**：顶层 `CMakeLists.txt` 会打印一条 `WARNING`，然后把 `SOC` 默认设为 `ascend950`（[CMakeLists.txt:24-27](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/CMakeLists.txt#L24-L27)），编译继续进行。

### 4.3 scripts/build.sh：MODE、-DSOC 与编译流程

#### 4.3.1 概念说明

直接手敲 `cmake -S . -B build ... && cmake --build build --target abs && cmake --install build --component abs` 太长也太容易出错。`scripts/build.sh` 就是这套流程的封装：它帮你解析参数、决定要编哪个 target、调用 cmake 配置/构建/安装，甚至还能在编完后自动跑一遍仿真。理解 `build.sh`，核心是理解它的 **MODE（模式）** 与 **target（目标）** 两个概念。

#### 4.3.2 核心流程

`build.sh` 的整体流程如下：

```
1. 校验 ASCEND_HOME_PATH 是否设置（未设置直接报错退出）
2. 解析命令行参数：
   - --clean              清理 build/ 与 output/
   - -D*                  收集为 CMake 选项（如 -DSOC=ascend950）
   - --host_ut/--example/--st [name]   互斥的三种模式
   - 裸 target 名          进入 legacy 模式（直接按 target 名编）
3. 根据 MODE 决定 CMake target 名（有默认值）
4. 若无 build/CMakeCache.txt，先 cmake 配置（abs/atvoss_examples 额外加 COMPILE_DYNAMIC_OPTIMIZED_MATMUL=ON）
5. cmake --build build --target <target>
6. cmake --install build --component <target>   → 产物落到 output/bin/
7. 按 MODE 做后处理：host_ut 编完即跑；atvoss_examples / st 默认跑 cannsim 仿真
```

#### 4.3.3 源码精读

**前置校验**：脚本一上来就检查 `ASCEND_HOME_PATH`，未设置则直接退出（[scripts/build.sh:63-67](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L63-L67)）：

```bash
if [[ ! -v ASCEND_HOME_PATH ]]; then
    echo -e "${ERROR}ASCEND_HOME_PATH environment variable is not set!${NC}"
    ...
    exit 1
fi
```

**MODE 取值**：脚本用变量 `MODE` 区分模式，取值见 [scripts/build.sh:31](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L31) 的注释 `"host_ut", "example", "st", or "legacy"`。`show_help` 里给出了最权威的用法说明（[scripts/build.sh:44-56](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L44-L56)）：

```
--host_ut [name]     Build unit test(s) in host/ (default: host_ut)
--example [name]     Build example(s) (default: atvoss_examples)
--st [name]          Build system test (default: st)
<target>             Directly build CMake target (e.g., abs, host_ut)   ← legacy
```

四种模式的含义对照：

| MODE | 触发写法 | 默认 target | 用途 |
|---|---|---|---|
| `example` | `--example [name]` | `atvoss_examples` | 编译样例；用 `atvoss_examples` 时会顺带把 abs/muls/rms_norm 跑一遍仿真 |
| `host_ut` | `--host_ut [name]` | `host_ut` | 编译并运行主机侧单元测试（不需芯片） |
| `st` | `--st [name]` | `st` | 编译系统测试；默认 `st` 时会跑 3 个代表性用例的仿真 |
| `legacy` | 直接给 target 名 | 无（即所给名字） | 直接按 CMake target 名编译，最灵活 |

**参数解析**：`-D*` 开头的参数会被收集进 `CMAKE_OPTIONS` 数组，原样传给 cmake（[scripts/build.sh:78-81](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L78-L81)）；遇到 `--host_ut` / `--example` / `--st` 则记录 MODE 并吃掉紧跟的可选 target 名（[scripts/build.sh:82-93](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L82-L93)）；一个不以 `-` 开头的裸名则进入 `legacy` 模式（[scripts/build.sh:99-107](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L99-L107)）。

**MODE → CMake target 映射**（[scripts/build.sh:131-144](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L131-L144)）：

```bash
case "$MODE" in
    host_ut)  CMAKE_TARGET=${TARGET_NAME:-host_ut} ;;
    example)  CMAKE_TARGET=${TARGET_NAME:-atvoss_examples} ;;
    st)       CMAKE_TARGET=${TARGET_NAME:-st} ;;
    legacy)   CMAKE_TARGET="$TARGET_NAME" ;;
esac
```

**cmake 配置**：仅当 `build/CMakeCache.txt` 不存在时才配置一次（避免重复 configure）。当 target 是 `abs` 或 `atvoss_examples` 时，会额外加 `-DCOMPILE_DYNAMIC_OPTIMIZED_MATMUL=ON`（[scripts/build.sh:154-159](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L154-L159)）：

```bash
if [[ "$CMAKE_TARGET" == "abs" ]] || [[ "$CMAKE_TARGET" == "atvoss_examples" ]]; then
    cmake -S "$CMAKE_SOURCE_DIR" -B "$BUILD_DIR" \
        -DCMAKE_BUILD_TYPE="$CMAKE_BUILD_TYPE" \
        -DCMAKE_INSTALL_PREFIX="$OUTPUT_DIR" \
        -DCOMPILE_DYNAMIC_OPTIMIZED_MATMUL=ON \
        "${CMAKE_OPTIONS[@]}"
else
    ...   # 不带 COMPILE_DYNAMIC_OPTIMIZED_MATMUL
fi
```

**构建 + 安装**：先 `cmake --build`，再 `cmake --install --component <target>`（[scripts/build.sh:171-180](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L171-L180)）。`--component` 是关键：因为 `examples/CMakeLists.txt` 里把每个样例都登记了 `install(... COMPONENT ${NAME})`（[examples/CMakeLists.txt:93-94](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/CMakeLists.txt#L93-L94)），所以「按 component 安装」就能只把你要的那一个样例拷到 `output/bin/`。

**后处理（仿真运行）**：

- `host_ut` 模式下，若指定了单个名字，编完会立刻运行该 UT 可执行文件（[scripts/build.sh:187-193](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L187-L193)）。
- 当 target 是 `atvoss_examples` 时，脚本会对 `abs / muls / rms_norm` 三个样例依次跑 `cannsim`，并用 `--shape=32` 喂数据，最后 grep `Accuracy verification passed` 判定通过（[scripts/build.sh:200-237](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L200-L237)）。
- `st` 模式默认（不给 name）会跑 `test_block_cast12`、`test_tile_rms_norm_14`、`test_compute_buffer_reuse` 三个代表性用例（[scripts/build.sh:242-274](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L242-L274)）。

#### 4.3.4 代码实践：解读 `build.sh -DSOC=ascend950 abs` 这条命令

**实践目标**：把本节的命令实践任务「`bash scripts/build.sh -DSOC=ascend950 abs`」逐参数解读清楚，不靠运行只靠读脚本，推断它会做什么。

**操作步骤**：

1. 把命令拆成三个 token：`-DSOC=ascend950`、`abs`。
2. 对照参数解析逻辑（[scripts/build.sh:72-109](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L72-L109)）推断：
   - `-DSOC=ascend950` 命中 `-D*` 分支 → 进入 `CMAKE_OPTIONS`。
   - `abs` 是裸名、且此前 MODE 为空 → `TARGET_NAME=abs`、`MODE=legacy`。
3. 对照 MODE→target 映射：`legacy` ⇒ `CMAKE_TARGET=abs`。
4. 因为 `CMAKE_TARGET == abs`，configure 阶段会带上 `-DCOMPILE_DYNAMIC_OPTIMIZED_MATMUL=ON` 和 `-DSOC=ascend950`。
5. 执行 `cmake --build build --target abs`，再 `cmake --install build --component abs`。

**需要观察的现象**：注意「`bash scripts/build.sh -DSOC=ascend950 abs`」走的是 **legacy 模式**（裸名）；而「`bash scripts/build.sh -DSOC=ascend950 --example abs`」走的是 **example 模式**。两者最终 `CMAKE_TARGET` 都是 `abs`，编译结果一样，但语义上后者更显式。

**预期结果**：你能在不运行的情况下说出——这条命令会把 `abs.cpp` 编成可执行文件，并安装到 `output/bin/abs`。

> ⚠️ **待本地验证**：实际运行需要在已安装 CANN 工具链、且 `ASCEND_HOME_PATH` 已设置、`cannsim` 可用的机器上执行。本环境无 NPU 工具链，无法实跑；以上为基于源码静态推断的结果。

#### 4.3.5 小练习与答案

**练习 1**：`--example`、`--host_ut`、`--st` 能否同时使用？为什么？

**参考答案**：不能。脚本在解析时一旦发现 `MODE` 已被设置而又遇到第二个模式 flag，就会报错 `Only one mode (--host_ut/--example/--st) allowed.` 并退出（[scripts/build.sh:82-87](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L82-L87)）。三种模式互斥。

**练习 2**：`-DSOC=ascend950` 里的 SOC 能填别的型号吗（比如 ascend910）？

**参考答案**：不能。`examples/CMakeLists.txt` 与 `tests/st/CMakeLists.txt` 都有断言：只有 `SOC STREQUAL "ascend950"` 时才设置 `NPU_ARCH=dav-3510`，否则 `message(FATAL_ERROR "SOC only supports ascend950, but get ${SOC}")` 直接终止配置（见 [examples/CMakeLists.txt:17-24](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/CMakeLists.txt#L17-L24)）。这与 quick_start 里「支持的产品：Ascend 950PR/Ascend 950DT」一致（[docs/quick_start.md:41](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/quick_start.md#L41)）。

### 4.4 编译产物路径与运行方式

#### 4.4.1 概念说明

`build.sh` 把所有产物分两处放：**中间产物**（构建缓存、对象文件）放在仓库根的 `build/`，**最终可执行文件**通过 `cmake --install` 拷到 `output/bin/`。我们日常只关心 `output/bin/`：编出来的每个算子可执行文件都在这里，文件名就是 target 名。

#### 4.4.2 核心流程

产物路径由脚本顶部两个变量决定（[scripts/build.sh:28-29](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L28-L29)）：

```bash
BUILD_DIR="$CMAKE_SOURCE_DIR/build"
OUTPUT_DIR="$CMAKE_SOURCE_DIR/output"
```

安装时 `CMAKE_INSTALL_PREFIX` 指向 `output/`，而 `examples/CMakeLists.txt` 里 `set(EXAMPLE_DESTINATION bin)`，所以最终落在 `output/bin/<name>`（[examples/CMakeLists.txt:66](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/CMakeLists.txt#L66)、[examples/CMakeLists.txt:93-94](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/CMakeLists.txt#L93-L94)）。`st/` 测试同理（[tests/st/CMakeLists.txt:93-94](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/CMakeLists.txt#L93-L94)）。

#### 4.4.3 源码精读

`install` 那几行是产物落地的关键。以样例为例（[examples/CMakeLists.txt:93-94](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/CMakeLists.txt#L93-L94)）：

```cmake
install(TARGETS ${NAME} DESTINATION ${EXAMPLE_DESTINATION} COMPONENT ${NAME})
install(TARGETS ${NAME} DESTINATION ${EXAMPLE_DESTINATION} COMPONENT atvoss_examples)
```

每个样例被登记了两个 component：一个与自身同名（供「只编它」用），一个叫 `atvoss_examples`（供「编全部」用）。所以 `cmake --install --component abs` 只装 `abs`，`--component atvoss_examples` 装全部样例。

运行方式分两种（[docs/quick_start.md:97-127](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/quick_start.md#L97-L127)）：

- **真机执行**（依赖 Device）：
  ```
  ./output/bin/abs --shape=32
  ```
- **仿真执行**（不依赖 Device，用 cannsim）：把命令写进 `run.sh`，再
  ```
  cannsim record ./run.sh -s Ascend950 --gen-report
  ```
  运行通过时日志里会出现 `Accuracy verification passed.`（[docs/quick_start.md:124-127](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/quick_start.md#L124-L127)）。

> 小提示：`build.sh` 在编 `atvoss_examples` 或默认 `st` 时，已经把 `cannsim record` 这一步自动化了（见 4.3.3），所以日常回归直接用 `--example` / `--st` 即可，不必手写 `run.sh`。

#### 4.4.4 代码实践：定位产物并设计运行脚本

**实践目标**：编出 `abs` 之后，准确说出可执行文件路径，并设计真机/仿真两种运行方式。

**操作步骤**：

1. （本地）执行 `bash scripts/build.sh -DSOC=ascend950 abs`。
2. 编译成功后，执行 `ls -l output/bin/`，确认存在 `output/bin/abs`。
3. 真机运行：`./output/bin/abs --shape=32`。
4. 仿真运行：新建 `run.sh`，内容为 `./output/bin/abs --shape=32`，`chmod +x run.sh`，再 `cannsim record ./run.sh -s Ascend950 --gen-report`。

**需要观察的现象**：`output/bin/` 下只会出现「你这次 `--component` 指定的那些可执行文件」；之前编过但这次没指定的不会重复出现（除非你用了聚合 component）。仿真会在当前目录生成一个 `cannsim_xxx_abs_xxx/` 结果目录。

**预期结果**：日志末尾出现 `Accuracy verification passed.`，说明 abs 算子功能与精度正确。

> ⚠️ **待本地验证**：本环境无 CANN 工具链与 NPU/仿真器，上述运行结果需在合规机器上验证。

#### 4.4.5 小练习与答案

**练习 1**：`build/` 和 `output/` 这两个目录，哪一个适合加入 `.gitignore`？为什么？

**参考答案**：两者都应加入 `.gitignore`。`build/` 是 CMake 的中间产物（缓存、对象文件），`output/` 是安装产物（可执行文件），它们都能由源码 + 构建脚本重新生成，不应入库。`build.sh --clean` 正是同时删除这两个目录（[scripts/build.sh:114-118](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L114-L118)）。

**练习 2**：为什么 `--component abs` 只装 abs，而不装 muls、rms_norm？

**参考答案**：因为安装是按 CMake **component** 过滤的。每个样例被登记了两个 component：自身同名 component 与 `atvoss_examples`（[examples/CMakeLists.txt:93-94](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/CMakeLists.txt#L93-L94)）。`--component abs` 只命中 abs 自己那条 install 规则，muls/rms_norm 属于各自同名 component 或 `atvoss_examples`，不会被装入。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「**清理 → 编 abs → 定位产物 → 仿真运行 → 对比 host_ut 模式**」的完整闭环。所有命令在仓库根目录执行。

1. **清理旧产物**（对应 4.4.5 练习 1）：
   ```bash
   bash scripts/build.sh --clean
   ```
   预期：`build/` 与 `output/` 被删除。

2. **编译 abs 样例**（对应 4.3.4）：
   ```bash
   bash scripts/build.sh -DSOC=ascend950 abs
   ```
   预期：configure 阶段带上 `-DCOMPILE_DYNAMIC_OPTIMIZED_MATMUL=ON`，构建并安装到 `output/bin/abs`。

3. **定位并运行产物**（对应 4.4.4）：在真机上 `./output/bin/abs --shape=32`，或用 cannsim 仿真，直到看到 `Accuracy verification passed.`。

4. **换一种模式编单元测试**（对应 4.3 的 MODE 对照）：
   ```bash
   bash scripts/build.sh -DSOC=ascend950 --host_ut test_arguments
   ```
   预期：编完 `tests/ut/host/test_arguments` 后，脚本会**立刻运行**它并打印 UT 结果（[scripts/build.sh:187-193](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/scripts/build.sh#L187-L193)）。注意它跑在主机侧、不需要芯片，这与 `abs` 必须真机/仿真运行形成对比。

5. **写一段总结**：用自己的话说明——为什么第 2 步用 `legacy` 模式（裸名 `abs`）能编出算子可执行文件，而第 4 步要改用 `--host_ut` 模式？两种模式的产物路径和运行依赖有什么不同？

> ⚠️ **待本地验证**：第 2~4 步的实跑依赖 CANN 工具链与（部分步骤依赖）NPU/仿真器，本环境无法执行；请按 [docs/quick_start.md](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/quick_start.md) 准备环境后验证。

## 6. 本讲小结

- ATVOSS 仓库以 `include/`（框架本体）、`examples/`（使用样例）、`tests/`（UT/ST）、`scripts/build.sh`（一键构建）、`docs/`（文档）为骨架，`cmake/` 提供公共 CMake 片段。
- 顶层 `CMakeLists.txt` 只负责「定环境 + 挂子工程」：C++17、默认 SOC=ascend950、引入 CANN 工具链，再 `add_subdirectory(examples)` 与 `add_subdirectory(tests)`。
- 编译算子必须用 bisheng 编译器，依赖环境变量 `ASCEND_HOME_PATH`；SOC 目前只支持 `ascend950`（映射 `--npu-arch=dav-3510`）。
- `scripts/build.sh` 用 MODE 组织行为：`--example` 编样例、`--host_ut` 编并跑主机侧单测、`--st` 编并跑系统测试、裸名走 `legacy` 直接编指定 target。
- 产物分两层：中间产物在 `build/`，最终可执行文件通过 `cmake --install --component <name>` 落到 `output/bin/<name>`。
- 运行分两种：真机 `./output/bin/<name> --shape=...`，或无芯片时用 `cannsim record` 仿真；成功标志是 `Accuracy verification passed.`。

## 7. 下一步学习建议

现在你已经能把 ATVOSS 编出来并跑通一个样例，但对「`abs.cpp` 里到底写了什么、`atvoss.h` 把哪几层串起来」还一无所知。建议按以下顺序继续：

1. **下一讲 u1-l3（五层架构总览）**：从整体上认识 Device > Kernel > Block > Tile > Basic 五层架构，理解 `include/atvoss.h` 这个主入口头文件如何把各层拼起来——这是后续所有源码阅读的「骨架图」。
2. **再下一讲 u1-l4（从 abs 样例看用户编程模型）**：回到 `examples/abs/abs.cpp`，逐行读懂一个 ATVOSS 算子的标准骨架（Config / Compute / PlaceHolder / 三级 Builder），把本讲「能编能跑」升级为「能读懂、能改」。
3. 顺手通读一遍 [docs/quick_start.md](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/quick_start.md) 与 [examples/README.md](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/README.md)，确认环境与样例调用方式，为后续动手实验打底。
