# 构建系统与依赖

## 1. 本讲目标

上一讲（u1-l1）我们建立了 offload 子项目的全局认识：它为加速器/协处理器提供工具、运行时和 API，由绑定 OpenMP 的 `libomptarget`、开发中的通用 `liboffload` 以及共享的设备插件 `plugins-nextgen` 三部分组成。

本讲聚焦一个具体问题：**这个项目怎么构建出来？** 学完后你应当能够：

1. 说出 offload 构建所需的前置条件（CMake 版本、C++17、64 位、平台限制）以及它们写在源码的哪里。
2. 解释 `LIBOMPTARGET_PLUGINS_TO_BUILD` 如何选择要构建的插件（`host` / `cuda` / `amdgpu` / `level_zero`），以及为什么不同平台、不同 CPU 架构上构建出的插件集合不一样。
3. 理解构建期如何用 `offload-arch` 探测本机 GPU，以及构建产物输出到哪个目录、用什么安装目标安装。
4. 看懂调试、OMPT、LTO 等构建开关的作用与默认值。
5. 写出一份针对本机的最小构建命令，并逐条解释每个关键 `-D` 选项。

## 2. 前置知识

本讲会频繁出现 CMake 概念，先用一句话把几个关键术语讲清楚（不熟悉 CMake 也能跟上）：

- **cache 变量（CACHE）**：通过 `-D` 在命令行传入、并被缓存到 `CMakeCache.txt` 的变量。例如 `LIBOMPTARGET_PLUGINS_TO_BUILD`。第一次配置后，再次运行 CMake 时它不会自动重置，除非显式覆盖或清缓存。
- **`option(NAME "描述" 默认值)`**：CMake 提供的、专门给布尔开关用的 cache 变量声明语法，本质就是一个带默认值的 BOOL cache 变量。
- **`add_subdirectory(dir)`**：进入子目录，执行它里面的 `CMakeLists.txt`。offload 顶层文件用一串 `add_subdirectory` 把 `plugins-nextgen`、`libomptarget`、`liboffload`、`test` 等子模块串起来。
- **`find_package(X)`**：在系统中查找某个外部库或工具（如 CUDA Toolkit）。
- **install 组件（COMPONENT）**：CMake 把安装目标分组，安装时用 `--component <名>` 只装某一组。offload 把所有要装的库都归到名为 `offload` 的组件下，对外暴露 `install-offload` 这个高层目标。

还需要两个背景概念：

- **LLVM runtimes vs projects**：在 LLVM 单仓库（monorepo）里，`LLVM_ENABLE_PROJECTS` 用来构建编译器本体（如 `clang`、`lld`），而 `LLVM_ENABLE_RUNTIMES` 用来构建依赖该编译器的运行时库（如 `compiler-rt`、`openmp`、`offload`）。offload 通常作为 **runtime** 来构建，这样它能在第一遍编出 `clang` 后，再用 `clang` 把自己编出来。
- **目标三元组（target triple）**：形如 `x86_64-unknown-linux-gnu`、`nvptx64-nvidia-cuda`、`amdgpu-amd-amdhsa` 的字符串，用来描述「CPU 架构-厂商-操作系统-ABI」。设备镜像（device image）就是按目标三元组来区分的，构建系统里维护了一份「支持哪些三元组」的清单。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt) | 顶层构建入口：声明前置条件、选择插件、设置选项、串起所有子目录、定义 `install-offload`。本讲的主线。 |
| [`cmake/Modules/LibomptargetGetDependencies.cmake`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/cmake/Modules/LibomptargetGetDependencies.cmake) | 依赖探测脚本：找 LLVM 头文件目录，用 `offload-arch` 探测本机 NVIDIA/AMD/Intel GPU。 |
| [`cmake/OpenMPTesting.cmake`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/cmake/OpenMPTesting.cmake) | 测试基础设施：定位 `FileCheck`、`not`、`device-info` 等测试工具，登记 lit 测试套件。 |
| [`cmake/caches/Offload.cmake`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/cmake/caches/Offload.cmake) | 官方提供的「一键构建」缓存预设，把 projects/runtimes/目标三元组都配好。 |
| [`include/Shared/Targets.def.in`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/Targets.def.in) | 模板文件：根据插件清单生成 `Targets.def`，供运行时枚举可用插件。 |
| [`plugins-nextgen/CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/CMakeLists.txt) | 插件层入口：遍历 `LIBOMPTARGET_PLUGINS_TO_BUILD`，逐个 `add_subdirectory` 进入对应插件目录。 |

> 说明：讲义规格里把这个依赖探测脚本写成 `cmake/LibomptargetGetDependencies.cmake`，但源码中它实际位于 `cmake/Modules/` 子目录下（顶层 [`CMakeLists.txt:69-75`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L69-L75) 把 `cmake` 和 `cmake/Modules` 都加入了 `CMAKE_MODULE_PATH`，所以 `include(LibomptargetGetDependencies)` 能找到它）。本讲一律使用真实路径。

## 4. 核心概念与源码讲解

### 4.1 构建前置条件与平台限制

#### 4.1.1 概念说明

offload 是一个**主机端运行时库**：它要在主机进程里加载设备插件、和 host 侧的 `libomp`（OpenMP 主机运行时）协作、操作真实的 GPU 驱动。这意味着它对编译器和平台有硬性要求，不像普通库那样「处处可编」。

顶层 `CMakeLists.txt` 一开头就设了几道「关卡」：CMake 版本太老会警告；用 MSVC 编译则跳过 `libomptarget`；在 macOS/WASM/GPU 三元组上、或不支持 C++17、或是 32 位平台，则直接 `return()` 提前结束（即不构建任何东西）。

#### 4.1.2 核心流程

配置期（`cmake` 命令执行时）按顺序做：

1. 声明最低 CMake 版本，并对未来的 3.31 要求给出预警。
2. 设定 CUDA 最低版本常量（供 CUDA 插件与测试引用）。
3. 判断是否在 Windows（MSVC）→ 若是，关闭 `BUILD_LIBOMPTARGET`。
4. 依次检查：macOS/WASM？目标三元组是 GPU？不支持 C++17？32 位？任一命中即 `return()`。
5. 全部通过，才继续往下配置。

#### 4.1.3 源码精读

最低 CMake 版本与 3.31 预警（注释提示 LLVM 24 起将强制 3.31）：

[CMakeLists.txt:4-11](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L4-L11) —— 设 `cmake_minimum_required(VERSION 3.20.0)`，并提醒用户尽早升级。

CUDA 最低版本常量（被 CUDA 插件和测试复用，4.3 节会用到）：

[CMakeLists.txt:16-17](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L16-L17) —— `OFFLOAD_MIN_CUDA_VERSION 11.8.0`，同时存一个数字编码 `11080` 便于源码内比较。

Windows 下关闭 `libomptarget`：

[CMakeLists.txt:20-24](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L20-L24) —— 命中 `MSVC` 时 `BUILD_LIBOMPTARGET` 置 `OFF`（`liboffload` 与插件层在 Windows 上仍可构建）。

四道关卡（任一命中即 `return()`，整个项目不参与构建）：

[CMakeLists.txt:27-40](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L27-L40) —— 依次排除 macOS/WASM、GPU 目标三元组、无 C++17、32 位平台。

C++17 标准的正式设定（关卡通过之后）：

[CMakeLists.txt:108-110](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L108-L110) —— `CMAKE_CXX_STANDARD 17`，且要求 `CMAKE_CXX_EXTENSIONS NO`（用纯标准 C++，不开 GCC 扩展）。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：搞清楚「什么情况下 offload 会完全不构建」。
2. **步骤**：打开 [`CMakeLists.txt:27-40`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L27-L40)，列出所有触发 `return()` 的 `elseif` 分支。
3. **观察**：注意每个分支用的是哪个 CMake 变量（如 `APPLE`、`CMAKE_CXX_COMPILE_FEATURES`、`CMAKE_SIZEOF_VOID_P`）。
4. **预期结果**：应能写出 4 个条件——macOS/WASM、目标三元组为 `amdgcn|nvptx|spirv` 开头、编译器不支持 `cxx_std_17`、`CMAKE_SIZEOF_VOID_P != 8`。
5. 若想验证，可在不同机器上跑 `cmake -S . -B /tmp/ck` 并观察是否打印 `return()` 前的 WARNING——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么目标三元组是 GPU（如 `nvptx64-nvidia-cuda`）时反而不能构建 offload？

> **参考答案**：offload 是**主机**运行时，需要在主机 CPU 上编译并链接主机库。当交叉编译目标本身就是 GPU 时，没有「主机」可言，因此源码在 [`CMakeLists.txt:30-33`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L30-L33) 直接 `return()`。

**练习 2**：`CMAKE_SIZEOF_VOID_P EQUAL 8` 检查的是什么？为什么需要它？

> **参考答案**：检查 `void*` 的字节数是否为 8，即判断是否为 64 位平台。运行时里涉及指针与设备地址的映射，32 位主机未被支持，因此 [`CMakeLists.txt:37-39`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L37-L39) 直接拒绝构建。

### 4.2 依赖探测：LibomptargetGetDependencies

#### 4.2.1 概念说明

构建期除了「能不能编」，还要回答「本机有什么」。offload 用一个独立脚本 `LibomptargetGetDependencies.cmake` 来探测两类依赖：

1. **LLVM 头文件目录**：运行时源码 `#include` 了 LLVM 的 `Support/`、`Object/` 等头文件，必须找到它们。
2. **本机 GPU 架构**：通过 LLVM 工具 `offload-arch` 探测本机是否插了 NVIDIA / AMD / Intel GPU，以及具体架构名（如 `sm_80`、`gfx90a`）。这些架构名会被测试和设备 RTL 用来定向编译。

#### 4.2.2 核心流程

1. 收集 `LLVM_MAIN_INCLUDE_DIR` 与 `LLVM_BINARY_DIR/include` 作为头文件目录。
2. 找到 `offload-arch` 工具（优先用同仓库构建出的 target，否则用 `find_program`）。
3. 若有 `offload-arch`，分别用 `--only=nvptx` / `--only=amdgpu` / `--only=intel` 调用它，把输出按行切成架构列表，并设置 `LIBOMPTARGET_FOUND_*_GPU` 标志。

#### 4.2.3 源码精读

收集 LLVM 头文件目录（顶层会检查它非空，否则报致命错误）：

[cmake/Modules/LibomptargetGetDependencies.cmake:14-18](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/cmake/Modules/LibomptargetGetDependencies.cmake#L14-L18) —— 把 `LLVM_MAIN_INCLUDE_DIR` 等加入 `LIBOMPTARGET_LLVM_INCLUDE_DIRS`。

定位 `offload-arch`：

[cmake/Modules/LibomptargetGetDependencies.cmake:23-28](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/cmake/Modules/LibomptargetGetDependencies.cmake#L23-L28) —— 若存在 target `offload-arch` 取其路径，否则在 `LLVM_TOOLS_BINARY_DIR` 下 `find_program`。

以 NVIDIA GPU 为例的探测（AMD/Intel 同构，只是 `--only=` 参数不同）：

[cmake/Modules/LibomptargetGetDependencies.cmake:33-42](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/cmake/Modules/LibomptargetGetDependencies.cmake#L33-L42) —— `execute_process` 调用 `offload-arch --only=nvptx`，把多行输出 `REPLACE` 成分号分隔列表，命中则置 `LIBOMPTARGET_FOUND_NVIDIA_GPU`。

> AMD 与 Intel 的探测逻辑见 [`cmake/Modules/LibomptargetGetDependencies.cmake:47-70`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/cmake/Modules/LibomptargetGetDependencies.cmake#L47-L70)，结构完全一致。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：跟踪「一次 GPU 架构探测」的完整链路。
2. **步骤**：从 [`CMakeLists.txt:128`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L128) 的 `include(LibomptargetGetDependencies)` 进入脚本，选定 NVIDIA 分支，画出「调用 `offload-arch` → 切分输出 → 设置变量」的流程。
3. **观察**：注意 `string(REPLACE "\n" ";" ...)` 是怎么把多行架构名变成 CMake list 的。
4. **预期结果**：能说清 `LIBOMPTARGET_FOUND_NVIDIA_GPU` 与 `LIBOMPTARGET_NVPTX_DETECTED_ARCH_LIST` 这两个变量分别代表「有没有」和「是哪些」。
5. 如果本机装了 `offload-arch`，可手动运行 `offload-arch --only=nvptx` 对照脚本行为——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果没有 `offload-arch` 工具，GPU 探测会怎样？

> **参考答案**：[`LibomptargetGetDependencies.cmake:33`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/cmake/Modules/LibomptargetGetDependencies.cmake#L33) 的 `if(LIBOMPTARGET_OFFLOAD_ARCH)` 守卫不成立，三个 `execute_process` 都不执行，`LIBOMPTARGET_FOUND_*_GPU` 保持未定义。构建不会失败，但测试/RTL 无法定向到具体 GPU 架构。

**练习 2**：为什么用 `--only=nvptx` / `--only=amdgpu` / `--only=intel` 分别调用，而不是一次调用拿全部？

> **参考答案**：这样每个厂商的探测结果独立存放（如 `LIBOMPTARGET_NVPTX_DETECTED_ARCH_LIST`），下游可以根据「本机到底装了哪家的卡」决定启用哪个插件的测试，互不干扰。

### 4.3 插件选择：LIBOMPTARGET_PLUGINS_TO_BUILD 与平台裁剪

#### 4.3.1 概念说明

上一讲提到 offload 支持多种设备后端，对应四个插件：

- **`host`**：最简单的参考插件，把主机当「设备」，在主机上加载并执行 ELF 镜像（后续 u3-l3 会精读）。
- **`cuda`**：NVIDIA GPU。
- **`amdgpu`**：AMD GPU。
- **`level_zero`**：Intel GPU（通过 Level Zero 运行时）。

用 cache 变量 `LIBOMPTARGET_PLUGINS_TO_BUILD` 选择要构建哪些。它是一个**分号分隔的列表**（CMake list），或写成 `"all"` 表示「本平台支持的全部」。但「全部」到底包含什么，**取决于操作系统和 CPU 架构**——构建系统会按平台自动裁剪。

#### 4.3.2 核心流程

```
读取 LIBOMPTARGET_PLUGINS_TO_BUILD（默认 "all"）
        │
        ▼
按平台决定 "all" 的展开值
   Windows          → [level_zero]
   其他(Linux等)     → [amdgpu, cuda, level_zero]
        │
        ▼
强制追加 "host"，去重           ← host 在所有平台都构建
        │
        ▼
架构裁剪：
   非 (x86_64|ppc64le|aarch64 + Linux) → 移除 amdgpu、cuda
   非 (x86_64|AMD64 + Linux|Windows)   → 移除 level_zero
        │
        ▼
最终插件列表 → 传给 plugins-nextgen/CMakeLists.txt 逐个 add_subdirectory
```

#### 4.3.3 源码精读

按平台定义「全部插件」集合并处理 `"all"`：

[CMakeLists.txt:146-169](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L146-L169) —— Windows 下 `LIBOMPTARGET_ALL_PLUGIN_TARGETS` 只有 `level_zero`，且校验用户请求的插件只能是 `level_zero` 或 `host`；非 Windows 下默认 `all` 展开为 `amdgpu;cuda;level_zero`。

`host` 永远追加并去重：

[CMakeLists.txt:171-172](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L171-L172) —— `list(APPEND ... "host")` 后 `list(REMOVE_DUPLICATES ...)`，保证 `host` 一定在列表里且只出现一次。

按 CPU 架构裁剪 `amdgpu` / `cuda`：

[CMakeLists.txt:174-186](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L174-L186) —— 仅在 `x86_64|ppc64le|aarch64` 且 `Linux` 时才保留这两个 GPU 插件，否则用 `list(REMOVE_ITEM ...)` 移除。

裁剪 `level_zero`：

[CMakeLists.txt:187-194](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L187-L194) —— 仅在 `x86_64|AMD64` 且 `Linux|Windows` 时保留。

> 注意：`LIBOMPTARGET_BUILD_CUDA_PLUGIN` / `LIBOMPTARGET_BUILD_AMDGPU_PLUGIN` 是**已移除**的旧选项，若仍传入会被 [`CMakeLists.txt:141-144`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L141-L144) 警告并要求改用 `LIBOMPTARGET_PLUGINS_TO_BUILD`。

裁剪完成后，最终列表被插件层消费：

[plugins-nextgen/CMakeLists.txt:29-34](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/CMakeLists.txt#L29-L34) —— `foreach(plugin IN LISTS LIBOMPTARGET_PLUGINS_TO_BUILD)` 逐个进入 `${plugin}` 子目录；若目录不存在则 `FATAL_ERROR`。

同时，这份插件清单还被用来生成一个 C++ 头片段 `Targets.def`，供运行时枚举可用插件：

[CMakeLists.txt:201-210](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L201-L210) —— 把每个插件展开成 `PLUGIN_TARGET(name)` 宏调用，写入 `Targets.def`（模板见 [`include/Shared/Targets.def.in`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/Targets.def.in)）。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：能推算「在给定平台上最终构建哪些插件」。
2. **步骤**：
   - 设定场景 A：**Windows + x86_64**，用户传 `-DLIBOMPTARGET_PLUGINS_TO_BUILD=all`。
   - 设定场景 B：**Linux + aarch64**，用户不传该变量（用默认）。
   - 对照 [`CMakeLists.txt:146-194`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L146-L194) 逐步推演。
3. **观察**：注意场景 A 走 `if(WIN32)` 分支，场景 B 走 `else()` 分支；两处架构裁剪的正则不同。
4. **预期结果**：
   - 场景 A：`all` → `[level_zero]`，再 `+host` → `[level_zero, host]`（Windows 下 amdgpu/cuda 根本不在 `ALL_PLUGIN_TARGETS` 里）。
   - 场景 B：`all` → `[amdgpu, cuda, level_zero]`，`+host` → `[amdgpu, cuda, level_zero, host]`；架构裁剪阶段，`aarch64+Linux` 满足 amdgpu/cuda 的条件（保留），但不满足 level_zero 的 `x86_64|AMD64`（移除）→ 最终 `[amdgpu, cuda, host]`。
5. 想验证可在配置后查看 CMake 输出的 `Building the offload library with support for the "..." plugins` 一行（[`CMakeLists.txt:195-196`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L195-L196)）——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：在一台 Linux `riscv64` 主机上，默认会构建哪些插件？

> **参考答案**：`riscv64` 不匹配 `x86_64|ppc64le|aarch64`，故 [`CMakeLists.txt:174-186`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L174-L186) 移除 amdgpu、cuda；也不匹配 level_zero 的 `x86_64|AMD64`（[`CMakeLists.txt:187-194`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L187-L194)）移除 level_zero。最终只剩强制追加的 `host`。

**练习 2**：为什么 `host` 要用 `APPEND + REMOVE_DUPLICATES` 而不是直接写进 `ALL_PLUGIN_TARGETS`？

> **参考答案**：这样无论用户怎么自定义 `LIBOMPTARGET_PLUGINS_TO_BUILD`，`host` 都会被无条件加入（[`CMakeLists.txt:171`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L171)）。它是测试与开发的最小可用后端，必须始终存在；`REMOVE_DUPLICATES` 防止用户已显式写了 `host` 时重复出现。

### 4.4 目标三元组清单与构建产物输出/安装

#### 4.4.1 概念说明

两件看似无关的事放在一起讲，因为它们都回答「产物在哪」：

1. **目标三元组清单**：构建系统维护了一份 `LIBOMPTARGET_ALL_TARGETS`，列举所有受支持/受测试的主机与设备三元组（含 `-LTO` 变体）。它主要用于测试矩阵和文档，告诉你可以为哪些目标生成设备镜像。
2. **输出与安装目录**：编出来的 `.so` 放在构建树的哪里、`install-offload` 把它们装到系统的哪里，取决于 `LLVM_ENABLE_PER_TARGET_RUNTIME_DIR`（是否按目标三元组分目录存放运行时）。

#### 4.4.2 核心流程

- **三元组清单**：用一串 `set(... "${VAR} aarch64-unknown-linux-gnu")` 累加，每个目标再加一个 `-LTO` 变体，最后还有 `nvptx64-nvidia-cuda-JIT-LTO`、`spirv64-intel` 等特殊条目。
- **输出目录**：若开启了 per-target runtime dir（且非 Apple），库输出到 `${LLVM_LIBRARY_OUTPUT_INTDIR}/${目标三元组}`，否则直接输出到 `${LLVM_LIBRARY_OUTPUT_INTDIR}`。
- **安装**：`libomptarget` 与 `libLLVMOffload` 都用 `COMPONENT offload` 安装；对外暴露 `install-offload` 目标，等价于 `cmake --install <build> --component offload`。

#### 4.4.3 源码精读

支持的目标三元组清单：

[CMakeLists.txt:215-233](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L215-L233) —— 涵盖 `aarch64`、`powerpc64(le)`、`x86_64`、`s390x`、`riscv64`、`loongarch64`（各含 `-LTO`），以及设备三元组 `amdgpu-amd-amdhsa`、`nvptx64-nvidia-cuda`（含 `-JIT-LTO`）、`spirv64-intel`。

安装子目录的决策（per-target 时带上目标三元组）：

[CMakeLists.txt:42-57](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L42-L57) —— 根据 `LLVM_ENABLE_PER_TARGET_RUNTIME_DIR` 决定 `OFFLOAD_INSTALL_LIBDIR` 是否追加 `${LLVM_DEFAULT_TARGET_TRIPLE}`。

构建期库的统一输出目录（让测试能找到库）：

[CMakeLists.txt:114-125](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L114-L125) —— 把 `ARCHIVE/LIBRARY/RUNTIME_OUTPUT_DIRECTORY` 都指向 `LIBOMPTARGET_LIBRARY_DIR`，使所有产物集中。

`install-offload` 高层目标（pre-commit CI 用来只装 offload 组件）：

[CMakeLists.txt:363-375](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L363-L375) —— 定义 `install-offload` 与 `install-offload-stripped`，并让它们依赖 `omptarget`、`LLVMOffload` 两个目标。

两个库确实归到 `offload` 组件：

- [libomptarget/CMakeLists.txt:62](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/CMakeLists.txt#L62) —— `install(TARGETS omptarget ... COMPONENT offload ...)`。
- [liboffload/CMakeLists.txt:53-54](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/liboffload/CMakeLists.txt#L53-L54) —— `install(TARGETS LLVMOffload ... COMPONENT offload)`。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：说清「编出来的库叫什么、在哪、怎么装」。
2. **步骤**：
   - 在 [`CMakeLists.txt:331-348`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L331-L348) 找到 `add_subdirectory(libomptarget)` 与 `add_subdirectory(liboffload)`，确认二者都参与构建。
   - 对照上面的安装语句，确认它们都带 `COMPONENT offload`。
   - 跟踪 `LIBOMPTARGET_LIBRARY_DIR`（[`CMakeLists.txt:114-125`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L114-L125)）。
3. **观察**：注意 per-target runtime dir 开关如何改变最终安装路径。
4. **预期结果**：能回答「`libomptarget.so` 与 `libLLVMOffload.so` 在构建树统一输出到 `LIBOMPTARGET_LIBRARY_DIR`；`cmake --build . --target install-offload` 会把它们装到 `OFFLOAD_INSTALL_LIBDIR`」。
5. 实际路径取决于本机是否开启 per-target dir——**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`install-offload` 与直接 `cmake --install` 有什么区别？

> **参考答案**：直接 `cmake --install` 会装整个构建树里所有带 install 规则的目标（含 LLVM 其他组件）；而 [`CMakeLists.txt:363-364`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L363-L364) 的 `install-offload` 加了 `--component offload`，只安装打了 `COMPONENT offload` 标签的目标，即 offload 自己的库与头文件。

**练习 2**：清单里 `nvptx64-nvidia-cuda-JIT-LTO` 与普通 `nvptx64-nvidia-cuda` 有何不同？

> **参考答案**：前者带 `-JIT-LTO` 后缀，表示该设备镜像在加载时走 JIT 编译 + LTO 的路径（u3-l9 会讲 JIT）；普通变体则是预编译好的设备码。清单把它们列为不同条目，便于测试矩阵分别覆盖。

### 4.5 构建选项：调试、OMPT、LTO、编译标志

#### 4.5.1 概念说明

最后一组开关影响**运行时库本身的行为与体积**：

- **调试输出**：是否允许在运行时用环境变量 `LIBOMPTARGET_DEBUG=1` 打印调试信息。
- **OMPT**：是否为工具（如性能分析器）提供目标侧回调（OMPT = OpenMP Tools）。
- **LTO**：是否对运行时库本身做链接期优化。
- **编译标志**：默认关异常、关 RTTI，以匹配 LLVM 整体风格并减小体积。

#### 4.5.2 核心流程

- 调试：若 `CMAKE_BUILD_TYPE` 含 `debug`，默认开启 `LIBOMPTARGET_ENABLE_DEBUG`，否则默认关闭；开启后给源码定义 `OMPTARGET_DEBUG` 宏。
- OMPT：默认跟随主机 `libomp` 的 OMPT 支持情况；满足条件则定义 `OMPT_SUPPORT=1`。
- LTO：`LIBOMPTARGET_USE_LTO` 默认 `FALSE`，开启后把 IPO 选项加到编译与链接标志。
- 编译标志：非 MSVC 用 `-fno-exceptions`；RTTI 跟随 `LLVM_ENABLE_RTTI`。

#### 4.5.3 源码精读

调试开关：

[CMakeLists.txt:243-251](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L243-L251) —— Debug 构建时默认 `LIBOMPTARGET_ENABLE_DEBUG=ON`，并 `add_definitions(-DOMPTARGET_DEBUG)`。

OMPT 目标侧支持：

[CMakeLists.txt:298-316](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L298-L316) —— 仅当主机 `libomp` 实现了 OMPT（`LIBOMP_HAVE_OMPT_SUPPORT`）且被请求（`LIBOMP_OMPT_SUPPORT`）且非 Windows 时，才默认开启 `LIBOMPTARGET_OMPT_SUPPORT` 并定义 `OMPT_SUPPORT=1`。

LTO 开关：

[CMakeLists.txt:282-288](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L282-L288) —— `LIBOMPTARGET_USE_LTO` 默认 `FALSE`；注释解释了为何不默认开（CMake 3.24 前 `CheckIPOSupported` 会忽略 `LLVM_ENABLE_LLD` 等选项，可能误判）。

编译标志（关异常、关 RTTI）：

[CMakeLists.txt:254-270](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L254-L270) —— 非 MSVC 加 `-fno-exceptions`；若 `LLVM_ENABLE_RTTI` 为假则加 `-fno-rtti`。

#### 4.5.4 代码实践（源码阅读型）

1. **目标**：搞清楚「怎么让运行时在运行时打印调试信息」。
2. **步骤**：从 [`CMakeLists.txt:243-251`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L243-L251) 出发，找到 `OMPTARGET_DEBUG` 宏被定义的条件，再在源码里搜索该宏的用法。
3. **观察**：注意它和 `CMAKE_BUILD_TYPE` 的联动——Release 构建默认不定义该宏，即便运行时设了环境变量也不会输出。
4. **预期结果**：应能说出「要么用 Debug 构建，要么显式 `-DLIBOMPTARGET_ENABLE_DEBUG=ON`，才能在运行时用 `LIBOMPTARGET_DEBUG=1` 看到输出」。
5. 想确认环境变量行为可结合后续 u1-l4 讲的 `LIBOMPTARGET_INFO` 一起试——**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 OMPT 目标侧默认依赖主机 `libomp` 的 OMPT 支持？

> **参考答案**：目标侧回调最终要和主机 OMPT 协作（把 target 事件接入工具）。若主机 `libomp` 根本没实现 OMPT（`LIBOMP_HAVE_OMPT_SUPPORT` 为假），单独开启目标侧没有意义，因此 [`CMakeLists.txt:303-306`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L303-L306) 把它作为前置条件。

**练习 2**：`LIBOMPTARGET_USE_LTO` 为什么默认关闭？

> **参考答案**：[`CMakeLists.txt:272-281`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L272-L281) 注释说明：CMake 3.24 之前的 `CheckIPOSupported` 会用默认链接器测试 LTO，忽略 `LLVM_ENABLE_LLD` 等选项，可能出现「测试通过但实际链接失败」（如 in-tree Clang 编译却用 ld.gold 链接、或 gcc 的 LTO 格式 lld 不认识）。为避免误判，默认关闭。

## 5. 综合实践

**任务**：写一份最小构建命令，针对本机选择构建 `host` 插件与某一个 GPU 插件，逐条解释每个关键 `-D` 选项的作用与默认值。

### 推荐做法 A：用官方缓存预设（最省心）

offload 在 `cmake/caches/` 下提供了官方预设 [`Offload.cmake`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/caches/Offload.cmake)（注意：实际路径是 `cmake/caches/Offload.cmake`），它一次性配好了 projects、runtimes 与目标三元组：

[cmake/caches/Offload.cmake:1-5](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/cmake/caches/Offload.cmake#L1-L5) —— 开启 `clang;clang-tools-extra;lld` 三个 project，`compiler-rt;libunwind;libcxx;libcxxabi;openmp;offload` 一组 runtime，并设 `LLVM_ENABLE_PER_TARGET_RUNTIME_DIR ON`。

```bash
# 假设当前在 llvm-project/ 下，build 目录与之同级
cmake -G Ninja -C offload/cmake/caches/Offload.cmake \
  -S llvm -B build \
  -DCMAKE_BUILD_TYPE=Release

cmake --build build --target offload      # 只构建 offload 运行时与插件
# 或
cmake --build build --target install-offload  # 构建并安装（--component offload）
```

该预设不覆盖 `LIBOMPTARGET_PLUGINS_TO_BUILD`，因此走默认 `all`，由本讲 4.3 的平台裁剪逻辑决定实际编哪些。

### 推荐做法 B：显式指定 host + 某个 GPU 插件

若想精确只编 `host` 和 `cuda`（NVIDIA），在 A 的基础上追加：

```bash
cmake -G Ninja -C offload/cmake/caches/Offload.cmake \
  -S llvm -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DLIBOMPTARGET_PLUGINS_TO_BUILD="cuda;host"
```

### 逐条解释关键 `-D` 选项

| 选项 | 作用 | 默认值（非 Windows） | 出处 |
| --- | --- | --- | --- |
| `-G Ninja` | 选择 Ninja 生成器（LLVM 推荐） | — | CMake 通用 |
| `-C offload/cmake/caches/Offload.cmake` | 预加载官方缓存预设 | — | [`Offload.cmake:1-5`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/cmake/caches/Offload.cmake#L1-L5) |
| `-DCMAKE_BUILD_TYPE=Release` | 设构建类型；含 `debug` 时会联动开启调试宏 | 空（单配置生成器需显式设） | [`CMakeLists.txt:243-248`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L243-L248) |
| `-DLIBOMPTARGET_PLUGINS_TO_BUILD="cuda;host"` | 指定要构建的插件列表 | `"all"` | [`CMakeLists.txt:163-168`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L163-L168) |
| `-DLIBOMPTARGET_ENABLE_DEBUG=ON` | 允许运行时 `LIBOMPTARGET_DEBUG=1` 调试输出 | Debug 构建为 ON，否则 OFF | [`CMakeLists.txt:245-248`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L245-L248) |
| `-DLIBOMPTARGET_OMPT_SUPPORT=ON` | 开启目标侧 OMPT 回调 | 跟随主机 libomp 的 OMPT | [`CMakeLists.txt:303-307`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L303-L307) |
| `-DLIBOMPTARGET_USE_LTO=ON` | 对运行时库做 LTO | `FALSE` | [`CMakeLists.txt:282-283`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L282-L283) |
| `-DLIBOMPTARGET_DLOPEN_PLUGINS=...` | 指定哪些插件用 `dlopen` 运行时链接 native 库 | 同 `LIBOMPTARGET_PLUGINS_TO_BUILD` | [`CMakeLists.txt:198-199`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L198-L199) |

### 操作步骤与预期

1. **写命令**：按本机情况选 A 或 B。无 NVIDIA 驱动就只编 `host`（`-DLIBOMPTARGET_PLUGINS_TO_BUILD="host"`），有 AMD 卡改成 `amdgpu;host`，有 Intel 卡改成 `level_zero;host`。
2. **配置**：运行 `cmake`，重点看输出里这一行——
   `Building the offload library with support for the "..." plugins`（[`CMakeLists.txt:195-196`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L195-L196)）。
3. **构建**：`cmake --build build --target offload`。
4. **观察**：在 `build/` 下找到 `libomptarget.so` 与各 `omptarget.rtl.<plugin>.a`、`libLLVMOffload.so`（具体路径取决于 per-target runtime dir，**待本地验证**）。
5. **预期结果**：插件列表与本机平台/架构一致；若在不受支持的架构上请求了 GPU 插件，CMake 会打印「Not building ... plugin」并自动移除（[`CMakeLists.txt:176-194`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L176-L194)），而不是报错。

> 提示：runtimes 构建模式下，向 offload 传 cache 变量的命名空间细节（是否需要 `RUNTIMES_<triple>_` 前缀）依赖 LLVM 版本与构建布局，若发现变量未生效，请以本机 `CMakeCache.txt` 实际值为准——**待确认**。

## 6. 本讲小结

- offload 是主机运行时，有硬性前置条件：CMake ≥ 3.20、C++17、64 位、非 macOS/WASM/GPU 三元组；Windows（MSVC）下不构建 `libomptarget`。
- 依赖探测脚本 `cmake/Modules/LibomptargetGetDependencies.cmake` 收集 LLVM 头文件目录，并用 `offload-arch --only={nvptx|amdgpu|intel}` 探测本机 GPU 架构。
- 插件选择由 `LIBOMPTARGET_PLUGINS_TO_BUILD` 控制（默认 `"all"`），`host` 始终被追加；`cuda`/`amdgpu`/`level_zero` 会按操作系统与 CPU 架构自动裁剪。
- 目标三元组清单 `LIBOMPTARGET_ALL_TARGETS` 列出所有受测试目标；构建产物统一输出到 `LIBOMPTARGET_LIBRARY_DIR`，安装走 `install-offload`（`--component offload`）。
- 调试、OMPT、LTO 三类开关分别绑定 `OMPTARGET_DEBUG`、`OMPT_SUPPORT`、IPO 标志；运行时库默认关异常、关 RTTI。
- 官方缓存预设 `cmake/caches/Offload.cmake` 是最简单的构建入口，配合 `-DLIBOMPTARGET_PLUGINS_TO_BUILD=...` 即可精确定制插件集合。

## 7. 下一步学习建议

- 下一讲 **u1-l3 目录结构与模块全景** 会把本讲涉及的 `libomptarget`、`plugins-nextgen`、`liboffload`、`tools` 等目录的内部职责展开，建议接着读，建立「目录 ↔ 功能」的对应关系。
- 之后 **u1-l4 工具链、编译运行与设备信息** 会用本讲构建出的 `clang`、`llvm-offload-device-info` 真正编译并运行一个卸载程序，并用 `LIBOMPTARGET_INFO` 观察运行时行为——那是验证本讲构建是否成功最直接的方式。
- 想提前了解某个插件的构建细节，可直接看 `plugins-nextgen/<plugin>/CMakeLists.txt`（例如 [`plugins-nextgen/cuda/CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/cuda/CMakeLists.txt) 里对 `OFFLOAD_MIN_CUDA_VERSION` 的使用），这部分会在 u3 单元系统讲解。
