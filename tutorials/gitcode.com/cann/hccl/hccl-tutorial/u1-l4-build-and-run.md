# 构建、安装与运行

## 1. 本讲目标

本讲解决一个最实际的问题：**拿到 HCCL 源码后，如何把它编译成可安装的软件包，并安装到 CANN 环境里运行起来。**

学完本讲，你应该能够：

- 说清 `bash build.sh --pkg` 这一条命令背后到底做了哪几件事；
- 掌握 `build.sh` 的常用编译选项（`--pkg` / `--full` / `--static` / `--asan` / `-u` / `-s`），并能解释每个选项改变了什么构建行为；
- 理解 `CMakeLists.txt`、`version.cmake`、`cmake/config.cmake`、`cmake/package.cmake` 是如何协作把源码组织成工程的；
- 知道编译产物 `.run` 软件包长什么样、放在哪里，以及如何安装与卸载；
- 看懂「源码构建 → 上板测试」的完整流程。

本讲是 Unit 1 的收尾，承接 [u1-l3 源码目录结构剖析](./u1-l3-directory-structure.md)——你已经知道 `src/` 里有什么，现在我们要把这些源码「编出来」。

## 2. 前置知识

在进入源码之前，先用通俗语言建立几个概念。

### 2.1 什么是「编译」

把人写的 C/C++ 源码（`.cc` / `.h`）翻译成机器能执行的二进制，这个过程叫编译（compile）。HCCL 是一个 C++ 工程，体量很大，手写每一条 `g++` 命令不现实，所以用 **CMake** 这个「构建系统生成器」来管理：开发者写一份 `CMakeLists.txt` 描述「要编什么、依赖什么、输出什么」，CMake 会自动生成对应的 Makefile，再用 `make` 真正去编译。

### 2.2 host 侧 与 device 侧

昇腾 NPU 的程序分两侧：

- **host 侧（主机侧）**：运行在服务器的 CPU 上（x86 或 aarch64），负责接收框架的调用、做算法选择、编排任务。HCCL 的绝大部分源码（`src/ops`、`src/common`）都编成 host 侧的动态库 `libhccl.so`。
- **device 侧（设备侧）**：运行在 NPU 芯片上，比如 AICPU kernel、AIV kernel。这些是真正「搬数据」的程序，会被打包成 `aicpu_hccl.tar.gz`，在业务启动时加载到 Device 上。

`bash build.sh --pkg` 默认只编 host 包；加 `--full` 才会同时编 device 包。

### 2.3 CANN 软件包与版本配套

HCCL 不是孤立编译的，它依赖一整套 CANN 软件（runtime、hcomm、metadef 等）。编译前必须先安装 CANN Toolkit，并通过环境变量告诉 `build.sh` 它装在哪里（`ASCEND_CANN_PACKAGE_PATH`）。HCCL 的版本要与 CANN 版本「配套」，这套配套关系就写在 `version.cmake` 里。

### 2.4 `.run` 软件包是什么

CANN 生态里的安装包通常是一个自解压的 `.run` 脚本（用开源工具 makeself 制作）。它本质是一个 shell 脚本，头部是安装逻辑，尾部「贴」着压缩后的二进制文件。直接 `bash xxx.run --full` 就能自解压并安装。

> 小提示：本讲不要求你真的有一台 NPU 机器。**仅编译源码**（不运行）不需要安装驱动固件；只有「上板测试」才需要真实 NPU 设备。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [build.sh](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh) | 一键编译入口脚本：解析命令行选项、组装 CMake 参数、分发到不同的编译函数 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/CMakeLists.txt) | 顶层 CMake 工程文件：声明编译选项、找到 CANN 依赖、引入 `src` 子目录 |
| [version.cmake](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/version.cmake) | 版本与依赖声明：HCCL 自身版本号、构建依赖、运行依赖 |
| [cmake/config.cmake](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/cmake/config.cmake) | CMake 配置细节：默认构建类型、CANN 包路径解析、安装目录、桩库（stub）生成 |
| [cmake/package.cmake](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/cmake/package.cmake) | CPack 打包配置：把编译产物打成 `.run` 软件包 |
| [src/CMakeLists.txt](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/CMakeLists.txt) | src 子目录工程：解析 CANN 版本号、设置头文件包含路径、按 host/device 引入不同 cmake |
| [docs/zh/build/build.md](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/build/build.md) | 官方构建文档：环境准备、编译安装、卸载、上板测试的权威说明 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块拆分：

1. **build.sh 选项解析与编译函数**——脚本入口都做了什么；
2. **顶层 CMakeLists 与 src/CMakeLists 子目录组织**——CMake 工程如何组织；
3. **源码构建与上板测试流程**——对照 build.md 的完整操作链路。

### 4.1 build.sh 选项解析与编译函数

#### 4.1.1 概念说明

`build.sh` 是整个工程对外的「一键编译」入口。它本质上做三件事：

1. **解析命令行选项**：把 `--pkg`、`--full`、`--static` 等参数翻译成一组 CMake 变量；
2. **组装 CMake 参数**：把这些变量拼到 `CUSTOM_OPTION` 这个长字符串里，最终传给 `cmake`；
3. **分发到编译函数**：根据用户意图，调用 `build_hccl`（编 host 包）、`build_static`（编静态库）、`build_ut`/`run_st`（跑测试）等不同函数。

理解 `build.sh` 的关键，是把它看成一个「参数翻译器 + 调度器」，它本身不编译任何代码，真正的编译交给 CMake。

#### 4.1.2 核心流程

`build.sh` 的执行流程可以概括为下图：

```text
bash build.sh [选项]
        │
        ▼
 ① 读取 build_config.sh（可选覆盖）   ← 比如 ENABLE_910_96
        │
        ▼
 ② 解析命令行选项（while 循环）       ← --pkg / --full / --static / --asan / -u / -s ...
        │
        ▼
 ③ 把选项拼成 CUSTOM_OPTION           ← -DSTATIC_MODE=ON -DENABLE_ASAN=ON ...
        │
        ▼
 ④ 定位 ASCEND_CANN_PACKAGE_PATH       ← 找 CANN 装在哪
        │
        ▼
 ⑤ clean + set_env                     ← 清理 build/、build_out/，source set_env.sh
        │
        ▼
 ⑥ 按优先级分发（if/elif 链）          ← UT > ST > TEST > CUSTOM > CB_TEST > STATIC > build_hccl
        │
        ▼
       build_hccl()  ← 默认（--pkg 走这里）
        │
        ▼
  cmake 配置 → cmake --build → make package  →  build_out/*.run
```

注意最后一步的分发是 **if/elif 互斥**的，优先级从高到低：先看是否跑单元测试（UT），再看系统测试（ST），再看自定义算子（CUSTOM）……只有都不命中时，才走默认的 `build_hccl`。所以 `bash build.sh --pkg` 会落到 `build_hccl` 这条路径。

#### 4.1.3 源码精读

**(1) 脚本开头的默认变量**

脚本一开始定义了一组默认值，这些是后续选项要修改的「开关」：

[build.sh:23-49](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L23-L49) 定义了 `ASAN="false"`、`STATIC_MODE="false"`、`ENABLE_BUILD_DEVICE="OFF"`、`ENABLE_UT="off"`、`ENABLE_ST="off"` 等默认开关，以及 `OUTPUT_DIR=${CURRENT_DIR}/build_out`（产物输出目录）和 `BUILD_DIR=${CURRENT_DIR}/build`（中间构建目录）。

其中第 21 行用 `cat /proc/cpuinfo` 数出 CPU 核数并乘以 2 作为并行编译线程数 `JOB_NUM`，第 39 行从 `version.cmake` 里抓取版本号作为 `VERSION_INFO` 默认值：

[build.sh:39](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L39) 用 `grep -oP 'VERSION "\K[0-9.]+'` 从 `version.cmake` 提取版本字符串，抓不到则回退到 `8.5.0`。

还有一段值得注意的设计——脚本会尝试 source 同目录下的 `build_config.sh`：

[build.sh:33-35](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L33-L35) 如果存在 `build_config.sh` 就 source 它。这意味着你想改某些默认开关（例如把 910_96 的 AIV kernel 编译开关 `ENABLE_910_96` 从 `OFF` 改成 `ON`），**不需要修改 `build.sh` 本身**，建一个 `build_config.sh` 写入覆盖值即可。这是一种常见的「不改主干、本地覆盖」的工程实践。

**(2) 选项解析：while 循环**

选项解析集中在一个 `while [[ $# -gt 0 ]]` 循环里，用 `case` 逐个匹配：

[build.sh:741-930](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L741-L930) 是整个选项解析循环。几个关键选项的映射如下：

| 命令行选项 | 作用 | 改变的变量 |
| --- | --- | --- |
| `--pkg` | 主编译动作标识（仅占位，被跳过） | 无（第 764-767 行直接 `shift`） |
| `--full` | 同时编译 host + device 包 | `ENABLE_BUILD_DEVICE="ON"`（第 825-828 行） |
| `--static` | 编静态库模式 | `STATIC_MODE="true"`（第 829-832 行） |
| `--asan` | 开启 AddressSanitizer 内存检测 | `ASAN="true"`（第 837-840 行） |
| `-u` / `--ut` | 编译并运行单元测试（UT） | `ENABLE_TEST="on"`、`ENABLE_UT="on"`（第 773-777 行） |
| `-s` / `--st` | 编译并运行系统测试（ST） | `ENABLE_TEST="on"`、`ENABLE_ST="on"`（第 778-785 行） |
| `--experimental` | 启用 experimental 实验特性 | `ENABLE_EXPERIMENTAL="true"`（第 865-868 行） |

`--pkg` 本身只是一个「占位」动作标记——它在解析时被直接跳过（第 765-767 行注释明确写了「跳过 --pkg，不做处理」），真正的编译由最后分发逻辑里的 `else → build_hccl` 默认分支承担。

**(3) 把选项拼成 CMake 参数**

解析完后，脚本把这些开关翻译成 CMake 的 `-D` 参数，追加到 `CUSTOM_OPTION`：

[build.sh:936-990](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L936-L990) 逐段追加 `-DSTATIC_MODE=ON/OFF`、`-DENABLE_EXPERIMENTAL=ON`、`-DENABLE_ASAN=ON`、`-DENABLE_GCOV=ON` 等，并在最后统一加上 `ASCEND_CANN_PACKAGE_PATH`、`CANN_3RD_LIB_PATH`、`VERSION_INFO` 等基础变量。这一段就是「bash 变量 → CMake 变量」的翻译层。

例如 `--static` 的翻译：

[build.sh:936-940](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L936-L940) 把 `STATIC_MODE` 翻译成 `-DSTATIC_MODE=ON` 传给 CMake。

**(4) 分发逻辑与 build_hccl**

脚本的最后是分发逻辑：

[build.sh:998-1033](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L998-L1033) 按优先级 `if/elif` 判断：UT > ST > TEST > CUSTOM > CB_TEST > STATIC > else(build_hccl)。`bash build.sh --pkg` 不命中前面任何分支，落到最后的 `else`，调用 `build_hccl`。

`build_hccl` 是默认编译路径，它就是经典的「cmake 配置 → cmake 构建 → make 打包」三步：

[build.sh:658-686](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L658-L686) 定义了 `build_hccl` 函数：先 `cmake -S ../ -B . ${CUSTOM_OPTION}` 配置，再 `cmake --build . -j${CPU_NUM}` 编译，最后 `make package -j${CPU_NUM}` 打包。每一步都用 `if [ $? -ne 0 ]` 检查返回值，失败则报错返回。**这一步的 `make package` 就是产出 `.run` 软件包的关键**（打包逻辑见 4.3 节）。

值得注意的是，无论走哪条分支，脚本在分发前都会先执行两步准备工作：

[build.sh:992-996](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L992-L996) 调用 `set_env`（source CANN 的 `set_env.sh`）和 `clean`（删除并重建 `build/` 目录），然后 `cd ${BUILD_DIR}` 进入构建目录。`clean` 函数（第 99-110 行）会删除 `build/` 和 `build_out/`，所以**每次 `build.sh` 都是全量重新编译**，不会残留旧产物。

#### 4.1.4 代码实践

**实践目标**：不实际运行，仅通过阅读源码，理清 `--asan` 与 `--static` 两个选项各自从「命令行」走到「最终产物」的完整路径，从而体会 `build.sh` 的「翻译 + 分发」机制。

**操作步骤**：

1. 打开 [build.sh](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh)。
2. 对于 `--asan`：先看第 837-840 行（设 `ASAN="true"`）→ 再看第 946-948 行（追加 `-DENABLE_ASAN=ON`）→ 最后看 `build_test` 函数第 136-147 行（根据架构拼出 `libasan.so` 的 `LD_PRELOAD` 路径，并设 `ASAN_OPT="detect_leaks=0"`）。
3. 对于 `--static`：先看第 829-832 行（设 `STATIC_MODE="true"`）→ 再看第 936-940 行（追加 `-DSTATIC_MODE=ON`）→ 最后看第 1022-1024 行（分发到 `build_static` + `package_static_tar`）。

**需要观察的现象（代码层面）**：

- `--asan` 只往 CMake 加了一个 `-DENABLE_ASAN=ON`，**没有改变分发分支**（仍可能走 `build_hccl`），它的主要效果体现在编译选项注入和测试运行时的 `LD_PRELOAD`。
- `--static` 则**改变了分发分支**——它让脚本走 `build_static`（第 212-359 行），产出的是静态库 `libhccl_static.a` 和 `cann-hccl-static_*.tar.gz`（第 361-427 行），**而不是 `.run` 包**。两者产物形态完全不同。

**预期结果**：你能在一张纸上画出两条路径，并标注「选项解析 → CUSTOM_OPTION 拼接 → 分发分支 → 最终产物」四个节点。

**待本地验证**：如需确认实际编译产物，需在装好 CANN Toolkit 的环境执行（见 4.3 节实践）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bash build.sh --pkg` 不需要再加别的选项就能编译 host 包？`--pkg` 这个参数本身做了什么？

> **参考答案**：因为最后的分发逻辑是 `if/elif` 互斥链，`--pkg` 不命中任何特殊分支，自然落到 `else → build_hccl` 默认路径。`--pkg` 在选项解析时（第 764-767 行）只是被 `shift` 跳过，本身不设置任何变量，它更像一个「我要编译」的语义标识，便于用户书写和文档统一。

**练习 2**：如果我只想编译单元测试而不编译 host 包，该用哪个选项？它会落到分发逻辑的哪个分支？

> **参考答案**：用 `-u` 或 `--ut`。它会把 `ENABLE_UT="on"`，从而命中分发逻辑的第一个分支（第 998-1000 行 `if [ "${ENABLE_UT}" == "on" ]`），调用 `build_ut` + `run_ut`，不会走 `build_hccl`。

---

### 4.2 顶层 CMakeLists 与 src/CMakeLists 子目录组织

#### 4.2.1 概念说明

`build.sh` 把参数翻译完后，真正干活的是 CMake。HCCL 的 CMake 工程是「顶层 + 子目录」的分层结构：

- **顶层 [CMakeLists.txt](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/CMakeLists.txt)**：声明全局选项（静态/动态、是否编 device、是否启用 experimental）、找到所有 CANN 依赖、决定走哪种构建模式，最后 `add_subdirectory(src)` 进入子目录。
- **[src/CMakeLists.txt](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/CMakeLists.txt)**：负责具体的源码组织——解析 CANN 版本号做兼容判断、设置庞大的头文件包含路径列表、按 host/device 引入不同的 `.cmake` 文件。
- **[version.cmake](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/version.cmake)**：独立的版本与依赖声明文件，被多处引用（`build.sh` 抓版本号、CMake 做版本兼容判断、打包脚本命名）。

#### 4.2.2 核心流程

顶层 CMakeLists 根据几个开关选择不同的构建模式，可以用下面的决策图概括：

```text
顶层 CMakeLists.txt
        │
        ├── option: BUILD_OPEN_PROJECT (默认 ON)   ← 开源仓构建
        ├── option: STATIC_MODE (默认 OFF)
        ├── option: ENABLE_EXPERIMENTAL (默认 OFF)
        ├── option: ENABLE_BUILD_DEVICE (默认 OFF)
        │
        ▼
  if(ENABLE_TEST)        → 只编译 test（UT）
  elseif(ENABLE_CUSTOM)  → 自定义算子打包
  elseif(BUILD_OPEN_PROJECT)  ← 默认走这里
        │
        ├── find_cann_package(hcomm / runtime / graph ...)  ← 找 CANN 依赖
        ├── check_pkg_build_deps("hccl")                    ← 检查构建依赖
        ├── pack_built_in()                                 ← 配置 CPack 打包
        ├── add_subdirectory(src)                           ← 进入子目录编译
        ├── add_subdirectory(experimental/ops/)             ← 若 ENABLE_EXPERIMENTAL
        ├── add_cann_device_project(hccl)                   ← 若 ENABLE_DEVICE
        └── install(include/hccl.h  include/hccl_mc2.h)      ← 安装对外头文件
```

#### 4.2.3 源码精读

**(1) 顶层选项声明**

[CMakeLists.txt:11-19](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/CMakeLists.txt#L11-L19) 用 `option()` 声明了 `BUILD_OPEN_PROJECT`、`STATIC_MODE`、`ENABLE_EXPERIMENTAL`、`ENABLE_BUILD_DEVICE`、`ENABLE_BUILD_AARCH`、`HCCL_ENABLE_910_96` 等选项。这些 `option` 的默认值正是 `build.sh` 通过 `-D` 参数来覆盖的——例如 `build.sh --static` 传 `-DSTATIC_MODE=ON`，就把这里的 `OFF` 默认值翻转了。

第 21-23 行有一个隐藏联动：开静态模式时自动连带打开 device 构建：

[CMakeLists.txt:21-23](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/CMakeLists.txt#L21-L23) 如果 `STATIC_MODE` 且未显式关 device，则 `set(ENABLE_BUILD_DEVICE ON)`。这解释了为什么 `--static` 路径会先去编 device 端 AICPU 包（见 4.1 节 `build_static` 第一步）。

**(2) host/device 默认与 C++ 标准**

[CMakeLists.txt:26-28](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/CMakeLists.txt#L26-L28) 默认 `PRODUCT_SIDE="host"`，即默认只编译 host 侧。HCCL 全工程采用 C++17 标准：

[CMakeLists.txt:67-69](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/CMakeLists.txt#L67-L69) 设 `CMAKE_CXX_STANDARD 17` 且 `CMAKE_CXX_STANDARD_REQUIRED ON`，并清空了 Release 模式的默认编译 flags，以便由 CANN cmake 框架统一注入。

**(3) 开源仓构建主分支：找依赖 + 进子目录**

开源仓编译（`BUILD_OPEN_PROJECT=ON`，默认）走的是 `elseif(BUILD_OPEN_PROJECT)` 分支：

[CMakeLists.txt:127-159](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/CMakeLists.txt#L127-L159) 这一段是默认编译的核心：先 `find_cann_package` 依次找齐 `acl_rt`、`runtime`、`hcomm`、`graph` 等十余个 CANN 组件依赖，调用 `check_pkg_build_deps("hccl")` 校验构建依赖是否满足，再 `pack_built_in()` 配置打包（见 4.3），然后 `add_subdirectory(src)` 真正进入源码编译。若开了 `ENABLE_EXPERIMENTAL` 则额外 `add_subdirectory(experimental/ops/)`；若开了 `ENABLE_DEVICE` 则 `add_cann_device_project(hccl)` 编 device。

注意第 143 行 `find_cann_package(hcomm MODULE REQUIRED)`——这里**编译期依赖的是 hcomm 的桩库（stub）**，而非 hcomm 私有头。这正是 [u1-l1](./u1-l1-project-overview.md) 讲过的「两仓解耦」硬约束在构建层面的体现：HCCL 编译时不耦合 HCOMM 内部实现，运行时再通过 dlsym 动态加载 `libhcomm.so`。

**(4) 安装对外头文件**

[CMakeLists.txt:161-176](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/CMakeLists.txt#L161-L176) 定义了 `HCCL_HEAD` 仅包含 `include/hccl.h` 与 `include/hccl_mc2.h` 两个头文件，并 `install` 到 `${INSTALL_INCLUDE_DIR}/hccl/`。这印证了 [u1-l3](./u1-l3-directory-structure.md) 的结论：对外只暴露这两个稳定头文件。

**(5) version.cmake：版本与依赖**

[version.cmake:11-21](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/version.cmake#L11-L21) 声明 HCCL 自身版本 `9.2.0`，并列出构建依赖（`hcomm`、`runtime`、`metadef`、`bisheng-compiler`、`asc-devkit` 均 `>=8.5`）与运行依赖（`hcomm`、`runtime`、`metadef` 均 `>=8.5`）。`set_cann_build_dependencies` 和 `set_cann_run_dependencies` 是 CANN cmake 框架提供的宏，`check_pkg_build_deps` 会据此校验环境是否满足。

**(6) src/CMakeLists.txt：版本兼容与头文件路径**

进入 `src` 子目录后，首先要做版本兼容判断：

[src/CMakeLists.txt:14-65](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/CMakeLists.txt#L14-L65) 开源仓编译时（`BUILD_OPEN_PROJECT`）从 CANN 包的 `include/version/cann_version.h` 解析出 `CANN_VERSION_NUM`，再据此设置 `HCCL_CANN_COMPAT_850` 兼容开关（版本号 `< 90000000` 时为 `ON`）。这个开关会被 `hccl_apply_cann_compat` 函数（第 67-72 行）以 `target_compile_definitions` 注入到各编译目标，让代码里能用 `#ifdef HCCL_CANN_COMPAT_850` 做版本分支。**这就是为什么同一份 HCCL 源码能适配多个 CANN 版本**。

紧接着是一份极长的 `INCLUDE_LIST`：

[src/CMakeLists.txt:74-208](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/CMakeLists.txt#L74-L208) 列出了所有头文件包含路径。你可以从这份列表里清晰地看到 `ops/op_common` 的四大组件目录（`executor`、`selector`、`template`、`topo`）以及每个算子（`all_reduce`、`all_gather`、`reduce_scatter`……）都按 `selector/executor/template/[aicpu|aiv|ccu]` 的固定结构组织——这与 [u1-l3](./u1-l3-directory-structure.md) 介绍的目录结构完全对应。

最后按 `PRODUCT_SIDE` 引入不同的编译描述：

[src/CMakeLists.txt:256-265](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/CMakeLists.txt#L256-L265) host 侧 `include(hccl.cmake)` 并 `add_subdirectory(ops)`；device 侧则 `include(scatter_aicpu_kernel.cmake)`。第 269-377 行还定义了一个聚合目标 `aiv_all_targets`，把几十个 AIV kernel 目标（如 `hccl_aiv_all_reduce_op_910_95`、各种 `superkernel`）汇总成一个可一次性构建的集合——这正对应 `build_static` 里 `build aiv_all_targets` 那一步。

#### 4.2.4 代码实践

**实践目标**：通过阅读 CMake 源码，验证「`build.sh` 的命令行选项」与「CMakeLists 的 `option`」之间的对应关系。

**操作步骤**：

1. 打开 [CMakeLists.txt 第 11-19 行](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/CMakeLists.txt#L11-L19)，记下每个 `option` 的名字和默认值。
2. 打开 [build.sh 第 936-990 行](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L936-L990)，找出每个 `-DXXX=ON/OFF` 对应哪个 `option`。
3. 制作一张对照表。

**需要观察的现象**：

| build.sh 命令行 | build.sh 内部变量 | 拼成的 CMake 参数 | CMakeLists option（默认值） |
| --- | --- | --- | --- |
| `--static` | `STATIC_MODE="true"` | `-DSTATIC_MODE=ON` | `STATIC_MODE (OFF)` |
| `--asan` | `ASAN="true"` | `-DENABLE_ASAN=ON` | （由框架处理） |
| `--experimental` | `ENABLE_EXPERIMENTAL="true"` | `-DENABLE_EXPERIMENTAL=ON` | `ENABLE_EXPERIMENTAL (OFF)` |
| `--full` | `ENABLE_BUILD_DEVICE="ON"` | `-DENABLE_BUILD_DEVICE=ON` | `ENABLE_BUILD_DEVICE (OFF)` |

**预期结果**：你能清楚地看到，`build.sh` 的命令行选项 → bash 变量 → CMake `-D` 参数 → CMakeLists `option` 默认值，是一条完整的「四级翻译链」。

**待本地验证**：可在编译时加 `--rule_launch` 或观察 cmake 配置阶段打印的 `STATIC_MODE=...`、`ENABLE_BUILD_DEVICE=...` 状态行确认。

#### 4.2.5 小练习与答案

**练习 1**：顶层 CMakeLists 里有 `option(BUILD_OPEN_PROJECT ... ON)`，但 `build.sh` 从来没有传 `-DBUILD_OPEN_PROJECT=ON`，为什么它还是 `ON`？

> **参考答案**：因为 `option()` 的默认值就是 `ON`，而 `build.sh` 没有显式覆盖它，CMake 会沿用默认值。开源仓编译场景下这个开关始终为 `ON`，它只在「大工程内部编译」（`BUILD_OPEN_PROJECT=OFF`）时才会被外部环境显式关闭。

**练习 2**：[src/CMakeLists.txt:54-65](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/CMakeLists.txt#L54-L65) 里如果 `HCCL_CANN_VERSION_NUM` 解析为 0，会怎样？

> **参考答案**：会先打印一条 `WARNING`（第 55-57 行）提示版本号解析失败、兼容分支会回退到 `9.0.0+` 路径；随后由于 0 不满足 `GREATER 0 AND LESS 90000000`，`HCCL_CANN_COMPAT_850` 保持 `OFF`，即按非 8.5 兼容（新版）路径处理。

---

### 4.3 源码构建与上板测试流程（build.md）

#### 4.3.1 概念说明

前两节讲的是「机制」，这一节讲「操作」。官方文档 [build.md](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/build/build.md) 是构建流程的权威说明，它把整个过程分成四个阶段：

1. **环境准备**：装编译依赖、装 CANN 软件、配环境变量；
2. **编译源码**：`bash build.sh --pkg`，产出 `.run` 包；
3. **安装 / 卸载**：`bash xxx.run --full` / `--uninstall`；
4. **测试**：LLT（`build.sh --ut`）和上板测试（HCCL Test 工具）。

这一节还会把 4.1、4.2 讲过的机制串起来，解释 `.run` 包到底是怎么从 `make package` 一步步产出的。

#### 4.3.2 核心流程

完整的「源码 → 产物 → 安装 → 测试」链路如下：

```text
[环境准备]
  安装 CANN Toolkit → source set_env.sh → 设置 ASCEND_CANN_PACKAGE_PATH
        │
        ▼
[编译]  bash build.sh --pkg
        │  build.sh 解析选项 → CUSTOM_OPTION → cmake 配置 → cmake --build
        │  → make package （触发 CPack / makeself）
        ▼
  build_out/cann-hccl_<version>_linux-<arch>.run   ← 自解压安装包
        │
        ▼
[安装]  bash ./build_out/cann-hccl_<ver>_linux-<arch>.run --full
        │  自解压，替换 CANN Toolkit 里的 HCCL 相关软件
        ▼
[卸载]  bash ./build_out/cann-hccl_<ver>_linux-<arch>.run --uninstall
        │  恢复到安装 CANN Toolkit 后的初始状态
        ▼
[测试]
  ├─ LLT：bash build.sh --ut           （不需要 NPU，编宿主机单元测试）
  └─ 上板：HCCL Test 工具 + mpirun      （需要真实 NPU + 关闭验签）
```

#### 4.3.3 源码精读

**(1) 前置依赖与 CANN 安装**

[build.md:9-17](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/build/build.md#L9-L17) 列出编译依赖：`python>=3.7.0`、`pip3>=20.3.0`、`gcc/g++ 7.3.0~13.3.x`、`cmake>=3.16.0`、可选 `ccache`，以及仅 UT 时依赖的 `googletest`（建议 release-1.14.0）。其中 `cmake>=3.16.0` 正好对应顶层 [CMakeLists.txt:11](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/CMakeLists.txt#L11) 的 `cmake_minimum_required(VERSION 3.16.0)`。

[build.md:79-88](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/build/build.md#L79-L88) 说明装完 CANN 后要 `source /usr/local/Ascend/cann/set_env.sh` 让环境变量生效。这个 `set_env.sh` 正是 `build.sh` 第 72 行 `set_env` 函数里 source 的同一个文件。

**(2) 一键编译与产物**

[build.md:101-121](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/build/build.md#L101-L121) 给出核心命令：

```shell
# 编译 host 包
bash build.sh --pkg
# 编译 host + device 包
bash build.sh --pkg --full
```

并说明编译完成后会在 `./build_out` 目录下生成 `cann-hccl_<version>_linux-<arch>.run`，其中 `<arch>` 取 `x86_64` 或 `aarch64`。这个产物路径 `build_out` 就是 [build.sh:16](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L16) 定义的 `OUTPUT_DIR`，`.run` 包则由下面要讲的 CPack 配置产出。

**(3) `.run` 包是怎么产出的：CPack + makeself**

`build_hccl` 最后的 `make package` 会触发 CPack 打包。打包配置在 `cmake/package.cmake` 的 `pack_built_in` 函数里：

[cmake/package.cmake:79-166](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/cmake/package.cmake#L79-L166) `pack_built_in` 先探测架构（`x86_64` / `aarch64`，第 81-90 行），把安装脚本（`scripts/package/hccl/scripts`）、版本信息文件（`version.hccl.info` → `version.info`）、CANN 安装框架脚本等都 `install` 进打包目录，最后调用 CANN cmake 框架的 `set_cann_cpack_config(hccl ...)`（第 164 行）完成 CPack 配置。这个框架函数内部会用 makeself 工具（第 13 行 `add_cann_third_party(makeself-fetch)` 下载）把整个 staging 目录打包成自解压的 `.run` 脚本。所以 `cann-hccl_*.run` 本质是「安装脚本 + makeself 自解压壳 + 压缩的二进制产物」。

**(4) 安装与卸载**

[build.md:123-143](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/build/build.md#L123-L143) 给出安装与卸载命令：

```shell
# 安装（替换 CANN Toolkit 中的 HCCL 相关软件）
bash ./build_out/cann-hccl_<version>_linux-<arch>.run --full
# 卸载（恢复到安装 CANN Toolkit 后的状态）
bash ./build_out/cann-hccl_<version>_linux-<arch>.run --uninstall
```

关键点：安装是**覆盖式**的——「用户编译生成的 HCCL 软件包会替换已安装 CANN Toolkit 开发套件包中的 HCCL 相关软件」。卸载则能回滚到 Toolkit 初始状态。

**(5) 测试：LLT 与上板测试**

[build.md:147-153](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/build/build.md#L147-L153) 说明 LLT（单元测试）只需 `bash build.sh --ut`，**不需要 NPU 设备**。这对应 4.1 节分发逻辑里 `ENABLE_UT==on` 的分支。

上板测试则需要真实 NPU，流程更复杂（[build.md:155-204](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/build/build.md#L155-L204)）：编译 HCCL Test 工具 → **关闭驱动安全验签**（因为源码编出的 `aicpu_hccl.tar.gz` 不含签名头，见 [build.md:166-179](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/build/build.md#L166-L179)）→ 用 `mpirun -n 8 ./bin/all_reduce_test ...` 执行。这里有个重要知识点：**源码自编译的 AICPU 算子包没有签名**，所以上板前必须用 `npu-smi` 关闭客户自定义验签，否则驱动会拒绝加载。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：在有 CANN Toolkit 的环境中，亲手执行一次 `bash build.sh --pkg`，定位 `.run` 产物，并对照源码解释 `--static` 与 `--asan` 分别改变了什么构建行为。

> 注意：本实践需要已安装 CANN Toolkit 的编译环境（不一定需要 NPU 设备，编译 host 包本身不依赖 NPU）。若你当前环境没有 CANN，请把下面的「操作步骤」当作**阅读型实践**来完成——重点理解每一步对应的源码位置。

**操作步骤**：

1. **准备环境**：确保已安装 CANN Toolkit 并执行了 `source <安装路径>/set_env.sh`。可参考 [build.md:59-88](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/build/build.md#L59-L88) 验证环境。
2. **编译 host 包**：

   ```shell
   bash build.sh --pkg
   ```

   对照 [build.sh:998-1033](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L998-L1033)，确认它落到 `else → build_hccl`。
3. **定位产物**：在 `build_out/` 下找到形如 `cann-hccl_<version>_linux-<arch>.run` 的文件。版本号 `<version>` 来自 [version.cmake:11](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/version.cmake#L11)（当前为 `9.2.0`）。
4. **解释 `--static` 改变了什么**：对照 [build.sh:1022-1024](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L1022-L1024) 和 `build_static`（[build.sh:212-359](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L212-L359)）：
   - 它改变了**分发分支**，从 `build_hccl` 切到 `build_static`；
   - 产物从可安装的 `.run` 动态包，变成静态库 `libhccl_static.a` + `cann-hccl-static_*.tar.gz`（[build.sh:361-427](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L361-L427)）；
   - 它会连带打开 device 构建（[CMakeLists.txt:21-23](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/CMakeLists.txt#L21-L23)），先编出 device 端 AICPU 包，再把 `.o`、AICPU tar、AIV kernel 全部嵌入最终静态库。
5. **解释 `--asan` 改变了什么**：对照 [build.sh:946-948](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L946-L948) 与 `build_test` 里的 [build.sh:136-147](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L136-L147)：
   - 它往 CMake 注入 `-DENABLE_ASAN=ON`，让编译器在产物里插入 AddressSanitizer 内存检测插桩；
   - 它**不改变分发分支**（通常仍配合 `-u`/`-t` 用于测试），在测试运行时自动设置 `LD_PRELOAD` 指向 `libasan.so`，从而在跑测试时检测内存越界、释放后使用等错误。

**需要观察的现象**：

- `build.sh --pkg` 会在终端打印 `STATIC_MODE=OFF`、`PRODUCT_SIDE=host` 等状态行（来自 [CMakeLists.txt:18-19](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/CMakeLists.txt#L18-L19) 等处的 `message(STATUS ...)`）；
- 最终在 `build_out/` 出现 `.run` 文件，且文件名包含 `9.2.0` 和你的架构（`x86_64` 或 `aarch64`）。

**预期结果**：

- 你能指出 `.run` 产物的确切路径与命名规则；
- 你能用一句话区分：`--static` 改变的是「产物形态（动态包 → 静态库）+ 分发分支」，`--asan` 改变的是「编译插桩 + 测试运行时的内存检测」，两者作用层面完全不同。

**待本地验证**：若环境无 CANN Toolkit，上述命令无法实际执行；请改为阅读型实践，重点核对源码行号与逻辑。

#### 4.3.5 小练习与答案

**练习 1**：为什么「仅编译源码」可以不安装驱动固件，而「上板测试」必须装？

> **参考答案**：编译 host 包只是把 C++ 源码翻译成 `libhccl.so` 等二进制，这个过程在 CPU 上完成，不需要 NPU 硬件（见 [build.md:20-22](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/build/build.md#L20-L22)，驱动固件是「运行态依赖」）。而上板测试要让通信算子真正在 NPU 上跑起来、在设备间搬数据，必须有真实的驱动固件和设备。

**练习 2**：源码自编译的 `aicpu_hccl.tar.gz` 为什么上板前要关闭验签？这反映了 `--pkg` 与 `--full` 的什么区别？

> **参考答案**：源码仓自编译的 AICPU 算子包不含驱动要求的安全签名头，加载到 Device 时会被驱动拒绝（[build.md:166-179](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/build/build.md#L166-L179)）。这正说明：`bash build.sh --pkg`（只编 host）产出的包里**不含** device 端 AICPU 包；只有 `--full`（或 `--static`）才会编译并带上 `aicpu_hccl.tar.gz`，从而需要面对验签问题。

---

## 5. 综合实践

**任务：为一次「带 ASan 的 host 包编译」写出完整的预期行为报告。**

假设你要在已经装好 CANN Toolkit 的环境里执行：

```shell
bash build.sh --pkg --asan
```

请完成以下工作（可不实际运行，纯源码分析）：

1. **画出执行流程图**：从命令行解析开始，标出 `--pkg`、`--asan` 各自经过的源码位置，直到最终产物。
2. **预测分发分支**：这条命令会落到 [build.sh:998-1033](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/build.sh#L998-L1033) 的哪个分支？为什么？（提示：注意 `--asan` 没有设置 `ENABLE_TEST`/`ENABLE_UT`/`ENABLE_ST`，而 `ENABLE_ASAN` 是否在分发条件里。）
3. **列出 CMake 参数**：推算最终 `CUSTOM_OPTION` 里会包含哪些与本次选项相关的 `-D` 参数。
4. **指出产物**：最终在 `build_out/` 下会出现什么文件？它和「不带 `--asan`」的产物在文件名上有没有区别？

**参考思路**：

- 流程图应包含：`--pkg`（第 764-767 行跳过）→ `--asan`（第 837-840 行设 `ASAN="true"`）→ 第 946-948 行追加 `-DENABLE_ASAN=ON` → 第 992-996 行 `set_env` + `clean` → 分发到 `else → build_hccl`（因为 `--asan` 不触发任何测试开关，仍走默认分支）→ `cmake 配置 → cmake --build → make package` → `build_out/cann-hccl_*.run`。
- 关键结论：`--asan` 单独使用时**不改变分发分支**，仍编出 `.run` host 包，只是产物内部带上了 ASan 插桩；要真正发挥 ASan 作用，通常配合 `-u` 或 `-t` 跑测试（此时走 `build_test`，会自动设置 `LD_PRELOAD`）。

## 6. 本讲小结

- `build.sh` 是「参数翻译器 + 调度器」：它把命令行选项翻译成 CMake `-D` 参数（拼到 `CUSTOM_OPTION`），再按 `if/elif` 优先级分发到不同编译函数；`bash build.sh --pkg` 走默认分支 `build_hccl`。
- `build_hccl` 是经典三步：`cmake 配置 → cmake --build → make package`，其中 `make package` 通过 CPack + makeself 产出 `build_out/cann-hccl_<version>_linux-<arch>.run` 自解压安装包。
- `--static` 与 `--asan` 作用层面不同：前者改变**分发分支和产物形态**（`.run` 动态包 → `libhccl_static.a` 静态库 + tar.gz），后者只注入**编译插桩和测试期内存检测**，不改变默认分发分支。
- 顶层 `CMakeLists.txt` 用 `option()` 声明开关、`find_cann_package` 找依赖、`add_subdirectory(src)` 进子目录；编译期对 hcomm 只依赖桩库，体现「两仓解耦」硬约束。
- `version.cmake` 声明 HCCL 版本（当前 `9.2.0`）与构建/运行依赖（`>=8.5`）；`src/CMakeLists.txt` 解析 CANN 版本号设 `HCCL_CANN_COMPAT_850` 兼容开关，使一份源码适配多版本 CANN。
- 完整流程是「环境准备 → `build.sh --pkg` → 安装 `.run` → 卸载 / 测试」；仅编译不需 NPU，上板测试需装驱动固件并关闭对自编 AICPU 包的验签。

## 7. 下一步学习建议

本讲让你具备了「把 HCCL 编出来并安装运行」的能力，Unit 1 至此结束。接下来建议：

- 进入 **Unit 2（对外 API 与单算子执行主链路）**，从 [u2-l1 对外 API 与通信算子接口](./u2-l1-public-api-surface.md) 开始，学习 [include/hccl.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h) 暴露的全部算子，并跑通 [examples](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/examples) 里的第一个集合通信样例。
- 如果你更关心工程构建细节，可以继续精读 [cmake/config.cmake](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/cmake/config.cmake) 里的桩库（stub）生成逻辑 `generate_stub`，理解「编译期不依赖 hcomm 实现」是如何在 CMake 层面落实的。
- 如果你想做测试驱动开发，可以先跳到 [u7-l4 测试体系——UT 与 ST](./u7-l4-testing.md)，对照本讲的 `-u`/`-s` 选项理解 `build_ut`、`run_st` 背后的测试组织。
