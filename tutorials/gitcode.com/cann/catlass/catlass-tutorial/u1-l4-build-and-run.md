# 环境搭建与编译运行首个样例

## 1. 本讲目标

学完本讲，你应当能够：

- 理解 CANN（昇腾异构计算架构）工具包在 CATLASS 中的角色，并能正确使能 CANN 环境（`source set_env.sh`），看懂 `ASCEND_HOME_PATH` 这个关键环境变量。
- 读懂 CATLASS 顶层 `CMakeLists.txt` 的构建骨架，理解 `find_package(ASC)`、ASC 编译器与 `CATLASS_ARCH`（`2201`/`3510`）的含义与作用范围。
- 掌握 `scripts/build.sh` 的用法（选项与 target），独立完成 `00_basic_matmul` 样例的编译与运行，并看到 `Compare success.` 验证精度。

本讲是「跑通」环节：在 [u1-l1](./u1-l1-project-overview.md)（项目定位）和 [u1-l3](./u1-l3-directory-structure.md)（目录结构）之后，亲手把第一个算子在真实（或仿真）NPU 上跑起来，为 [u2](./u2-l1-host-acl-runtime.md) 起逐层精读源码打下可运行的基础。

## 2. 前置知识

在动手前，先建立三个通俗概念：

- **CANN 是什么**：CANN（Compute Architecture for Neural Networks）是昇腾 NPU 的异构计算软件栈，类比 NVIDIA 那边的 CUDA Toolkit。它提供了编译 NPU kernel 的编译器（ASC 编译器）、运行时库（ACL，Ascend Computing Language）和各种算子库。CATLASS 是一套「纯头文件模板库」，它本身不自带编译器和运行时，必须依赖 CANN 才能把模板实例化、编译成能在 NPU 上跑的可执行文件。
- **Host 与 Device**：一段 CATLASS 程序分两部分——跑在 CPU 上的 Host 代码（负责分配显存、启动 kernel、对比精度）和跑在 NPU 上的 Device/Kernel 代码（真正做矩阵乘）。CANN 同时为这两侧提供工具：ASC 编译器把 kernel 源码编成设备码，ACL 运行时让 Host 把设备码加载到 NPU 执行。
- **CMake 与编译器语言**：CATLASS 用 CMake 组织构建。特别之处在于 `CMakeLists.txt` 里声明了两种语言：`ASC`（编译 kernel 的昇腾编译器语言）和 `CXX`（普通主机端 C++）。`catlass_example_add_executable` 把一个 `.cpp` 标记为 `ASC` 语言交给昇腾编译器处理，这就是模板能被实例化成设备可执行文件的关键。

> 提示：本讲不要求你已读懂任何 kernel 代码。`00_basic_matmul` 的源码精读在 [u2](./u2-l1-host-acl-runtime.md)，本讲只关心「装好依赖、编得出来、跑得起来」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 仓库主入口，给出软硬件配套要求（gcc/cmake/python 版本、CANN 最低版本）。 |
| `docs/zh/1_Practice/01_quick_start.md` | 官方快速上手文档，本讲的「人话版」操作手册。 |
| `CMakeLists.txt` | 顶层 CMake 配置：声明 ASC/CXX、校验 `ASCEND_HOME_PATH`、设定 `CATLASS_ARCH` 默认值。 |
| `examples/CMakeLists.txt` | 样例层 CMake：把 `CATLASS_ARCH` 翻译成编译选项 `--npu-arch=dav-XXX`、定义宏、按架构挑选样例清单、定义 `catlass_example_add_executable`。 |
| `examples/00_basic_matmul/CMakeLists.txt` | 单个样例的 CMake：声明把 `basic_matmul.cpp` 编成名为 `00_basic_matmul` 的可执行文件。 |
| `scripts/build.sh` | 编译入口脚本：解析选项/target、调用 cmake 构建、把产物安装到 `output/bin`。 |
| `include/catlass/catlass.hpp` | 演示 `CATLASS_ARCH` 宏如何穿透到 C++ 源码内部，驱动架构特化常量。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先准备 CANN 环境，再理解 CMake 与 `CATLASS_ARCH`，最后用 `build.sh` 编译并运行。

### 4.1 CANN 环境准备

#### 4.1.1 概念说明

CATLASS 是模板库，不是自包含的可执行工程。要把它实例化、编译、运行，需要三件来自 CANN 的东西：

1. **ASC 编译器**：负责把 kernel 源码（含 CATLASS 模板）编译成 NPU 设备码。
2. **ACL 运行时库**：Host 侧调用 `aclInit`、`aclrtMalloc` 等接口与 NPU 交互。
3. **AscendC 算子库 / 平台库**：kernel 内部使用的 `AscendC::Mmad`、`platform_ascendc::PlatformAscendCManager` 等底层能力。

这三者都装在 CANN toolkit 安装目录下。安装完成后，关键动作是「使能环境」——执行 `set_env.sh`，它会把编译器、库路径等写入环境变量。其中最重要的一个变量是 **`ASCEND_HOME_PATH`**：它指向 CANN 的安装根目录，是 CMake 找到头文件（`include`）和链接库（`lib64`）的「锚点」。没有它，构建直接失败。

#### 4.1.2 核心流程

环境准备的完整流程：

```text
安装 NPU 驱动/固件
      │
      ▼
安装 CANN toolkit（Ascend-cann-toolkit_*.run）
      │
      ▼
source <install_path>/ascend-toolkit/set_env.sh   ← 关键：使能环境，写入 ASCEND_HOME_PATH 等
      │
      ▼
git clone https://gitcode.com/cann/catlass.git
      │
      ▼
进入仓库根目录，准备用 build.sh 编译
```

之后每次打开新终端，都要重新 `source` 一次 `set_env.sh`，否则 `ASCEND_HOME_PATH` 不存在，编译会报错。

#### 4.1.3 源码精读

先看官方对环境使能的说明：

[docs/zh/1_Practice/01_quick_start.md:26-35](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/1_Practice/01_quick_start.md#L26-L35) —— 安装完成后执行 `source set_env.sh` 即完成 CANN 环境使能，默认路径以 `/usr/local/Ascend/ascend-toolkit/set_env.sh` 为例。

[README.md:101-116](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/README.md#L101-L116) —— 软硬件配套说明：列出支持的昇腾产品（Atlas A2/A3、Ascend 950PR/DT）与软件依赖版本（`gcc >= 7.5 < 13.0`、`cmake >= 3.16`、`python >= 3.8 < 3.12`）。当前主线对应 CANN 8.5.0，Ascend 950 系列样例需要 CANN 9.0.0。

接下来看 `ASCEND_HOME_PATH` 在构建链路里被如何「强校验」。

顶层 CMake 第一时间检查它，缺失就直接 `FATAL_ERROR`：

[CMakeLists.txt:18-22](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/CMakeLists.txt#L18-L22) —— 若 `ASCEND_HOME_PATH` 未定义，报致命错误提示「请运行 set_env.sh」；否则把它从环境变量读进 CMake 变量，供后续找头文件与库使用。

`build.sh` 在解析任何参数之前也做同样的校验，给出一行更友好的报错：

[scripts/build.sh:72-76](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/scripts/build.sh#L72-L76) —— 检测到 `ASCEND_HOME_PATH` 未设置时打印错误并 `exit 1`，避免在错误环境下继续构建。

> 结论：`source set_env.sh`（产生 `ASCEND_HOME_PATH`）是编译 CATLASS 的**第一道闸门**，CMake 和 `build.sh` 两处都拦它。

#### 4.1.4 代码实践

- **实践目标**：确认 CANN 环境已正确使能。
- **操作步骤**：
  1. 打开终端，执行 `source /usr/local/Ascend/ascend-toolkit/set_env.sh`（按你的实际安装路径替换）。
  2. 执行 `echo $ASCEND_HOME_PATH`。
  3. 执行 `which cmake` 和 `cmake --version`，确认 cmake 可见且版本 ≥ 3.16。
- **需要观察的现象**：`echo` 应输出一个非空路径（如 `/usr/local/Ascend/ascend-toolkit/latest`）；cmake 版本号 ≥ 3.16。
- **预期结果**：`ASCEND_HOME_PATH` 有值。若为空，说明没有 `source` 成功，后续 `build.sh` 会在 [scripts/build.sh:72-76](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/scripts/build.sh#L72-L76) 处直接退出。
- 若你当前没有 NPU/CANN 环境：这一步待本地验证，可先只做「读源码」部分。

#### 4.1.5 小练习与答案

1. **练习**：为什么每次开新终端都要重新 `source set_env.sh`，而不能一劳永逸？
   **参考答案**：`source` 是在当前 shell 进程里设置环境变量，进程结束（关掉终端）后变量就消失。如果想一劳永逸，可以把该 `source` 命令写入 `~/.bashrc` 等启动脚本。

2. **练习**：如果忘了 `source`，直接运行 `bash scripts/build.sh 00_basic_matmul`，会在哪一步、由谁报错？
   **参考答案**：会在 `build.sh` 开头的 [scripts/build.sh:72-76](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/scripts/build.sh#L72-L76) 处被脚本自身拦截，打印「ASCEND_HOME_PATH environment variable is not set!」并 `exit 1`，根本进不到 cmake。

### 4.2 CMake 与 CATLASS_ARCH

#### 4.2.1 概念说明

CATLASS 的构建系统是 CMake，但它和普通 C++ 工程最大的不同是：**一套源码要服务两代硬件**——Atlas A2/A3（代号 `2201`）与 Ascend 950PR/DT（代号 `3510`）。这通过一个贯穿全工程的开关变量 `CATLASS_ARCH` 实现。

`CATLASS_ARCH` 的作用有两层：

- **编译器层**：它被拼成 ASC 编译器选项 `--npu-arch=dav-${CATLASS_ARCH}`（如 `dav-2201`），告诉编译器为哪代 NPU 的指令集生成设备码。
- **源码层**：它被定义成同名的 C++ 宏 `CATLASS_ARCH`，CATLASS 模板内部用 `#if CATLASS_ARCH == 2201 / 3510` 选择不同的常量与特化实现——这就是 [u1-l1](./u1-l1-project-overview.md) 提到的「上层逻辑共享、底层硬件差异特化」的落地方式之一。

此外，构建里还区分两种编译器语言：`ASC`（昇腾 kernel 编译器，处理跑在 NPU 上的 `.cpp`）与 `CXX`（主机端 g++）。`project(Catlass LANGUAGES ASC CXX)` 同时声明两者；样例则用 `set_source_files_properties(... PROPERTIES LANGUAGE ASC)` 把 `basic_matmul.cpp` 显式交给 ASC 编译器。

#### 4.2.2 核心流程

`CATLASS_ARCH` 从命令行一路穿透到 C++ 源码的链路：

```text
命令行:  bash scripts/build.sh -DCATLASS_ARCH=2201 00_basic_matmul
              │ (build.sh 把 -D* 原样收集进 CMAKE_OPTIONS)
              ▼
顶层 CMake:  set(CATLASS_ARCH 2201)   （未指定则默认 2201）
              │
              ▼
样例 CMake:  add_compile_options(--npu-arch=dav-2201)   → ASC 编译器目标架构
             add_compile_definitions(CATLASS_ARCH=2201)  → 注入 C++ 宏
              │
              ├── 按架构挑选样例清单 (EXAMPLE_ATLASA2 / EXAMPLE_ASCEND950)
              ▼
C++ 源码:    #if CATLASS_ARCH == 2201 ... #elif CATLASS_ARCH == 3510 ...
             （选择 BYTE_PER_BLK_FP 等架构常量、Tile 组件路由）
```

#### 4.2.3 源码精读

**第一站：顶层 CMake 骨架。**

[CMakeLists.txt:11-16](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/CMakeLists.txt#L11-L16) —— `find_package(ASC REQUIRED)` 找到昇腾 ASC 编译器工具；`project(Catlass LANGUAGES ASC CXX)` 同时启用 ASC 与 CXX 两种语言；C++ 标准设为 17。`find_package(ASC)` 能成功，正是因为 `ASCEND_HOME_PATH` 指向了装好 ASC 编译器的 CANN 目录。

[CMakeLists.txt:46-49](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/CMakeLists.txt#L46-L49) —— 若用户没传 `CATLASS_ARCH`，给出告警并默认 `2201`（对应 Atlas A2/A3）。

**第二站：样例层 CMake 把架构变量翻译成编译选项与宏。**

[examples/CMakeLists.txt:15-16](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/CMakeLists.txt#L15-L16) —— 第 15 行把 `--npu-arch=dav-${CATLASS_ARCH}` 作为 ASC 语言（`$<COMPILE_LANGUAGE:ASC>`）的编译选项，决定设备码目标架构；第 16 行定义同名 C++ 宏 `CATLASS_ARCH`，让模板源码能据此分支。

[examples/CMakeLists.txt:19-24](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/CMakeLists.txt#L19-L24) —— `2201` 架构专属的编译选项（溢出记录、dcci 插入等 llvm 级开关），只在 Atlas A2/A3 上启用。

[examples/CMakeLists.txt:176-184](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/CMakeLists.txt#L176-L184) —— 根据 `CATLASS_ARCH` 选择要编译的样例清单：`2201` 用 `EXAMPLE_ATLASA2`（见 [examples/CMakeLists.txt:100-146](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/CMakeLists.txt#L100-L146)，含 `00_basic_matmul`），`3510` 用 `EXAMPLE_ASCEND950`（见 [examples/CMakeLists.txt:148-174](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/CMakeLists.txt#L148-L174)，样例名带 `ascend950_` 前缀）。这就是为什么 `00_basic_matmul` 在 2201 下能编、而 Ascend950 样例必须配 `-DCATLASS_ARCH=3510`。

**第三站：宏穿透进 C++ 源码。**

[include/catlass/catlass.hpp:38-42](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/catlass.hpp#L38-L42) —— 模板源码里直接用宏分支：`2201`（或未定义）时 `BYTE_PER_BLK_FP = 128`，`3510` 时为 `64`。同一个 `catlass.hpp` 头文件，因 `CATLASS_ARCH` 宏不同而实例化出不同的架构常量。

**第四站：单个样例如何被声明为一个可执行目标。**

[examples/00_basic_matmul/CMakeLists.txt:11-12](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/CMakeLists.txt#L11-L12) —— 第 11 行把 `basic_matmul.cpp` 的语言属性设为 `ASC`（交给昇腾编译器）；第 12 行调用 `catlass_example_add_executable(00_basic_matmul cube basic_matmul.cpp)` 生成名为 `00_basic_matmul` 的可执行文件，`cube` 表示算子类型为 cube（矩阵乘类）。该宏定义在 [examples/CMakeLists.txt:59-70](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/CMakeLists.txt#L59-L70)，负责创建 target、加入头文件目录、并把它 install 到 `bin` 目录。

#### 4.2.4 代码实践

- **实践目标**：理解 `CATLASS_ARCH` 如何决定「能编哪些样例」。
- **操作步骤**：
  1. 打开 [examples/CMakeLists.txt](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/CMakeLists.txt)，对照 `EXAMPLE_ATLASA2`（第 100-146 行）与 `EXAMPLE_ASCEND950`（第 148-174 行）两个清单。
  2. 回答：`00_basic_matmul` 在哪个清单里？它在 `3510` 下会被编译吗？
  3. （可选，待本地验证）若环境是 Ascend 950，尝试 `bash scripts/build.sh -DCATLASS_ARCH=3510 43_ascend950_basic_matmul`，观察它在 2201 默认下会不会因不在清单而报「target not found」。
- **需要观察的现象**：两份清单互不重叠，样例名前缀（`ascend950_`）与架构一一对应。
- **预期结果**：`00_basic_matmul` 属于 `EXAMPLE_ATLASA2`，仅 `2201` 会编译；`43_ascend950_basic_matmul` 属于 `EXAMPLE_ASCEND950`，仅 `3510` 会编译。

#### 4.2.5 小练习与答案

1. **练习**：`--npu-arch=dav-2201`（编译器选项）和 `CATLASS_ARCH=2201`（C++ 宏）分别解决什么问题？
   **参考答案**：前者告诉 ASC 编译器「为 2201 这代硬件的指令集生成设备码」（编译器行为）；后者注入到 C++ 预处理器，让模板源码在 `#if` 分支里选择对应的常量与特化实现（源码行为）。两者同源（都来自 CMake 变量 `CATLASS_ARCH`），但作用域不同。

2. **练习**：不传 `-DCATLASS_ARCH` 时默认值是多少？在哪行设定？
   **参考答案**：默认 `2201`，由 [CMakeLists.txt:46-49](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/CMakeLists.txt#L46-L49) 设定并给出告警。

### 4.3 build.sh 编译运行

#### 4.3.1 概念说明

`scripts/build.sh` 是 CATLASS 的统一编译入口，本质是一层对 `cmake` 的「友好封装」：它帮你处理选项解析、环境校验、`configure`/`build`/`install` 三步调用，并把最终可执行文件统一安装到 `output/bin`。你不需要手敲 cmake 命令。

它接受两类参数：

- **options**：以 `--` 或 `-D` 开头的编译选项，如 `--clean`、`--debug`、`--simulator`、`--enable_print`、`-DCATLASS_ARCH=3510` 等。
- **target**：要编译的目标，可以是一个具体样例名（如 `00_basic_matmul`），也可以是聚合目标（如 `catlass_examples` 全量编译、`python_extension`、`torch_library`、`mstuner_catlass`）。

编译成功后，样例可执行文件落在仓库的 `output/bin/` 下，直接运行即可；程序内部会自行生成随机数据、跑 kernel、与 CPU 真值对比，最终打印 `Compare success.` 表示精度通过。

#### 4.3.2 核心流程

`build.sh` 处理一次编译请求的流程：

```text
解析命令行 (options + target)
      │
      ▼
校验 ASCEND_HOME_PATH（缺失则退出）         [第 72-76 行]
      │
      ▼
--clean? → rm -rf build/ output/            [第 137-144 行]
      │
      ▼
cmake -S . -B build -DCMAKE_INSTALL_PREFIX=output <options>   [第 214-216 行]
cmake --build build --target <target> -j                      [第 219 行]
cmake --install build --component <target>                    [第 220 行]
      │  (install 把可执行文件拷到 output/bin)
      ▼
打印 [INFO] Target '<target>' built successfully              [第 221 行]
```

随后用户进入 `output/bin` 运行样例：

```text
cd output/bin
./00_basic_matmul 256 512 1024 0
      │  (Host: aclInit → 分配/拷贝显存 → 启动 kernel → 拷回 → 与 CPU 真值对比)
      ▼
打印 Compare success.
```

#### 4.3.3 源码精读

先看官方文档对用法与成功标志的描述：

[docs/zh/1_Practice/01_quick_start.md:57-59](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/1_Practice/01_quick_start.md#L57-L57) —— 编译入口 `bash scripts/build.sh [options] <target>` 的语法；紧随其后第 61-76 行枚举了全部 `options`，第 72-76 行说明了 target 的取值。

[docs/zh/1_Practice/01_quick_start.md:80-83](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/1_Practice/01_quick_start.md#L80-L83) —— 以 `00_basic_matmul` 为例的编译命令。

[docs/zh/1_Practice/01_quick_start.md:91-110](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/1_Practice/01_quick_start.md#L91-L110) —— 运行方式：`cd output/bin` 后 `./00_basic_matmul 256 512 1024 0`，三个数分别为 m/n/k 维度，第 4 个为可选 deviceId（默认 0）；出现 `Compare success.` 即符合精度预期。

再看脚本本体。输出目录定义：

[scripts/build.sh:28-29](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/scripts/build.sh#L28-L29) —— `BUILD_DIR=仓库根/build`、`OUTPUT_DIR=仓库根/output`，分别存放中间构建产物与最终安装产物（可执行文件在 `output/bin`）。

选项/target 解析（核心循环）：

[scripts/build.sh:78-122](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/scripts/build.sh#L78-L122) —— 用 `while/case` 逐个吃参数：`--clean/--debug/--simulator/--enable_print` 等映射成 `CMAKE_OPTIONS` 数组里的 `-D...`；`-D*` 原样追加；其它非选项参数当作 `target`，出现第二个 target 即报错。

实际执行构建的分支：

[scripts/build.sh:199-222](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/scripts/build.sh#L199-L222) —— 对普通样例 target：先 `cmake -S -B` 配置（`-DCMAKE_INSTALL_PREFIX=$OUTPUT_DIR`），再 `cmake --build build --target <target> -j`，最后 `cmake --install build --component <target>` 把产物装到 `output/bin`，结尾打印成功信息。第 221 行的成功提示与官方文档一致。

最后，确认 `Compare success.` 来自哪里——它在样例 Host 代码的精度对比段：

[examples/00_basic_matmul/basic_matmul.cpp:127-132](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L127-L132) —— `golden::CompareData` 把 device 拷回的 `hostC` 与 CPU 真值 `hostGolden` 逐元素比对；无误差时打印 `Compare success.`，否则打印失败与错误计数。这就是你运行样例后期望看到的那行输出。

> 命令行参数 `m n k [deviceId]` 的解析由 `examples/common/options.hpp` 里的 `GemmOptions` 完成（见 [examples/common/options.hpp:30-37](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/common/options.hpp#L30-L37)），`basic_matmul.cpp` 的 `main` 调 `options.Parse(argc, argv)` 读取它们。

#### 4.3.4 代码实践（本讲核心实践）

- **实践目标**：完整跑通「编译 → 运行 → 精度验证」全流程，看到 `Compare success.`。
- **操作步骤**：
  1. 确认已 `source set_env.sh`（参考 4.1.4）。
  2. 进入仓库根目录，编译样例：
     ```bash
     bash scripts/build.sh 00_basic_matmul
     ```
  3. 编译成功后进入产物目录并运行：
     ```bash
     cd output/bin
     ./00_basic_matmul 256 512 1024 0
     ```
- **需要观察的现象**：
  - 编译阶段：脚本先打印 ASCII Logo，随后执行 cmake configure/build/install，最后输出形如 `[INFO]Target '00_basic_matmul' built successfully`。
  - 运行阶段：程序无报错地跑完，标准输出打印 `Compare success.`。
- **预期结果**：终端出现 `Compare success.`，表明 NPU 上 kernel 计算结果与 CPU 真值在允许误差内一致。
- **若没有 NPU**：可用 `--simulator` 在仿真器上跑（需额外设置 `NPU_MODEL` 与 `LD_LIBRARY_PATH`，详见 [docs/zh/1_Practice/01_quick_start.md:65-65](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/1_Practice/01_quick_start.md#L65-L65) 引用的仿真文档），否则本步待本地验证。

#### 4.3.5 小练习与答案

1. **练习**：编译产物最终落在哪个目录？由 CMake 的哪个机制放过去的？
   **参考答案**：落在 `output/bin`。由 [scripts/build.sh:220](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/scripts/build.sh#L220-L220) 的 `cmake --install --component <target>` 触发，而 install 目标 `bin` 目录在 `catlass_example_add_executable` 宏里声明（[examples/CMakeLists.txt:60-69](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/CMakeLists.txt#L60-L69)）。

2. **练习**：运行时三个数字 `256 512 1024` 分别代表什么？第 4 个参数能否省略？
   **参考答案**：分别代表矩阵乘的 m、n、k 维度（左矩阵 256×1024、右矩阵 1024×512、结果 256×512）。第 4 个参数是 deviceId，可省略，默认 0（见 [examples/00_basic_matmul/README.md:21-26](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/README.md#L21-L26)）。

3. **练习**：想从头干净编译，应加哪个选项？它会删哪些目录？
   **参考答案**：加 `--clean`。它会删除 `build/` 与 `output/`（以及 `examples/python_extension/build`），见 [scripts/build.sh:137-144](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/scripts/build.sh#L137-L144)。

## 5. 综合实践

把本讲三个模块串起来，完成一次「换架构、换尺寸」的完整编译运行小任务：

1. **环境自检**：`source set_env.sh` 后执行 `echo $ASCEND_HOME_PATH`，确认非空；再确认 `cmake --version` ≥ 3.16、`gcc --version` 符合 README 要求。
2. **读懂架构开关**：对照 [examples/CMakeLists.txt:176-184](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/CMakeLists.txt#L176-L184)，确认你当前的硬件属于 2201（Atlas A2/A3）还是 3510（Ascend 950），据此决定编译命令是否需要 `-DCATLASS_ARCH=3510`。
3. **干净编译**：执行
   ```bash
   bash scripts/build.sh --clean 00_basic_matmul
   ```
   观察 `--clean` 先清理、再重新 configure/build/install 的全过程，确认看到成功提示。
4. **运行验证**：
   ```bash
   cd output/bin
   ./00_basic_matmul 256 512 1024 0
   ```
   确认输出 `Compare success.`。
5. **换尺寸复跑**：再试一组尺寸，例如 `./00_basic_matmul 512 512 512 0`，预期仍然 `Compare success.`。思考：m/n/k 会不会影响「能否跑通」？（本阶段只需跑通；性能与 TileShape 的关系是 [u8](./u8-l1-matmul-theory-templates.md) 的主题。）

> 完成后，你就拥有了一个可重复编译运行的 CATLASS 工作环境，这是后续逐层精读源码、动手改模板的前提。

## 6. 本讲小结

- CANN 是 CATLASS 的「编译器 + 运行时」依赖，`source set_env.sh` 产生 `ASCEND_HOME_PATH` 是编译的第一道闸门，CMake 与 `build.sh` 两处都强校验它。
- 顶层 `CMakeLists.txt` 通过 `find_package(ASC)` 启用 ASC 编译器、声明 `ASC`/`CXX` 双语言，并用 `CATLASS_ARCH`（默认 `2201`）统一描述目标硬件。
- `CATLASS_ARCH` 一条链路贯通三层：编译器选项 `--npu-arch=dav-XXX`、C++ 宏（驱动模板内 `#if` 特化，如 `BYTE_PER_BLK_FP`）、按架构挑选样例清单（`EXAMPLE_ATLASA2` vs `EXAMPLE_ASCEND950`）。
- `scripts/build.sh` 是 cmake 的友好封装：解析 options/target → cmake configure/build/install → 产物落到 `output/bin`，成功后打印 `[INFO]Target '<target>' built successfully`。
- 运行 `./00_basic_matmul 256 512 1024 0` 后看到 `Compare success.`，即证明 kernel 计算结果与 CPU 真值一致、环境与构建链路全通。
- 没有 NPU 时可用 `--simulator` 走仿真路径。

## 7. 下一步学习建议

环境跑通后，建议紧接着进入：

- **[u2-l1 Host 侧代码、ACL 运行时与精度验证](./u2-l1-host-acl-runtime.md)**：逐段精读 `00_basic_matmul.cpp` 的 Host 部分，搞清楚 `Compare success.` 背后的 `aclInit/aclrtMalloc/aclrtMemcpy` 与 `golden.hpp` 真值对比是怎么写的。
- **[u2-l2 四层组装范式总览](./u2-l2-four-layer-assembly.md)**：从 `basic_matmul.cpp` 的「五步组装」开始，第一次从源码层面接触 Device→Kernel→Block→Tile 分层。

阅读源码时，可随时回到本讲确认构建方式——后续讲义中出现的样例编号（如 `06_optimized_matmul`、`13_basic_matmul_tla`）都可用同样的 `bash scripts/build.sh <样例名>` 编译运行。
