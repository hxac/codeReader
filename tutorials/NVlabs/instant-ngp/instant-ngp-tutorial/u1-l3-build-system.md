# 构建与编译：用 CMake 跑通构建

## 1. 本讲目标

读完本讲后，你应当能够：

- 说清「配置（configure）」与「编译（build）」两个阶段各自做了什么，并能逐字解释 README 里那两条 `cmake` 命令。
- 读懂 `CMakeLists.txt` 中 `NGP_BUILD_WITH_*` 系列开关，知道每一个开关会引入或排除哪些依赖与能力。
- 理解 CUDA 架构（compute capability）的作用，会通过 `TCNN_CUDA_ARCHITECTURES` 为自己的显卡指定编译目标。
- 区分本仓库最终产出的两个目标：`instant-ngp` 可执行文件与 `pyngp` Python 模块。

本讲承接 [u1-l2 目录结构与代码组织](u1-l2-directory-structure.md)。上一讲我们已经画出了仓库的目录地图、定位了中枢文件 `testbed.cu`，本讲回答的问题是：**这些 `.cu / .cpp` 源文件如何被组织、编译、链接成一个能跑起来的程序。**

## 2. 前置知识

### 2.1 什么是 CMake

`CMake` 不是编译器，而是一个「构建系统生成器」。你写一份声明式的 `CMakeLists.txt`，CMake 读取它，帮你生成具体平台上的工程文件（Linux 下默认是 Makefile，Windows 下可以是 Visual Studio 工程或 Ninja）。真正把源码变成可执行程序的，还是底层编译器（`g++` / `cl.exe`）和 NVIDIA 的 `nvcc`（CUDA 编译器）。

CMake 的典型用法是两步走：

1. **配置阶段（configure）**：`cmake -B build`。CMake 检查系统环境（CUDA 装了没？Vulkan 装了没？Python 在哪？），解析开关，把结果写进 `build/` 目录里的缓存文件和工程文件。这一步会报「找不到依赖」之类的错误。
2. **编译阶段（build）**：`cmake --build build`。调用上一步选定的底层工具链，真正把源码编译、链接成产物。这一步会报「语法错误、链接错误」之类的错误。

### 2.2 CUDA 与 GPU 架构

CUDA 代码（`.cu` 文件）需要被 `nvcc` 编译成 GPU 能执行的机器码。不同代际的 NVIDIA GPU 有不同的「计算能力」（Compute Capability，比如 RTX 3090 是 8.6，A100 是 8.0）。编译时必须指定目标架构，否则要么生成的代码跑不了，要么为所有架构都生成一遍（编译又慢、产物又大）。

### 2.3 静态库、可执行文件与共享库

- **静态库（static library）**：把一堆 `.o/.obj` 打包成一个 `.a/.lib`，供后续链接。
- **可执行文件（executable）**：最终能直接运行的程序，本项目是 `./instant-ngp`。
- **共享库（shared library）**：`.so/.dll/.dylib`，在运行时被加载，本项目里 `pyngp` 就是一个被 Python 解释器动态加载的共享库。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [CMakeLists.txt](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt) | 整个项目的构建中枢，定义开关、依赖、源码清单与最终目标 |
| [.gitmodules](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/.gitmodules) | 声明 10 个 git 子模块（第三方依赖）的地址与挂载路径 |
| [requirements.txt](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/requirements.txt) | `scripts/` 下 Python 脚本（如 `run.py`）运行所需的 Python 依赖 |
| [README.md](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md) | 给出官方编译命令与 GPU 架构对照表 |
| [cmake/bin2c_wrapper.cmake](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/cmake/bin2c_wrapper.cmake) | 辅助脚本：把 OptiX 编译出的 PTX 文本转成 C 头文件 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 CMake 构建流程**、**4.2 可选功能开关**、**4.3 CUDA 架构选择**。

---

### 4.1 CMake 构建流程

#### 4.1.1 概念说明

instant-ngp 的构建逻辑全部写在一个文件里：根目录的 `CMakeLists.txt`（共 408 行）。它的总体思路是「**先把所有核心源码编成一个静态库 `ngp`，再从这个库链接出两个产物**」：

- 可执行文件 `instant-ngp`（带 `src/main.cu` 作为入口，给人用的命令行/GUI 程序）。
- 共享库 `pyngp`（给 Python 用的绑定模块）。

这种「中间静态库 + 多前端」的设计很常见：核心逻辑只编译一次，多个前端（CLI、Python）复用同一份机器码，既省编译时间，又保证行为一致。

#### 4.1.2 核心流程

整个构建可以这样用伪代码描述：

```
1. project(instant-ngp, 语言: C / C++ / CUDA)        # 声明项目与启用的语言
2. 检查子模块是否齐全（缺则直接 FATAL_ERROR）
3. 设置 C++14 与 CUDA 14 标准
4. add_subdirectory(dependencies/tiny-cuda-nn)        # 拉入底层神经网络库
5. NGP_SOURCES = [src/testbed.cu, src/testbed_nerf.cu, ... 一堆核心源文件]
6. add_library(ngp STATIC ${NGP_SOURCES})             # 编译成静态库 ngp
7. if (NGP_BUILD_EXECUTABLE):
       add_executable(instant-ngp src/main.cu)         # 从 ngp 链接出可执行文件
   if (Python 找到):
       add_library(pyngp SHARED src/python_api.cu)     # 从 ngp 链接出 Python 模块
```

关键点：**第 2 步的子模块检查发生在配置阶段的最早期**。这就是为什么很多初学者一上来 `cmake -B build` 就报错——因为没加 `--recursive` 克隆，子模块是空的。

#### 4.1.3 源码精读

**项目声明与语言**：第 11–15 行声明项目并启用三种语言，CUDA 是其中之一：

[CMakeLists.txt:11-15](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L11-L15) —— 声明 `instant-ngp` 项目，`LANGUAGES C CXX CUDA` 表示 CMake 会启用 CUDA 编译器检测。

**默认构建类型**：如果用户没指定 `CMAKE_BUILD_TYPE`，第 36–40 行默认设为 `Release`：

[CMakeLists.txt:36-40](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L36-L40) —— 没指定构建类型时强制 `Release`，这会开启 `-O3` 等优化。

**子模块缺失检查（配置阶段最重要的护栏）**：

[CMakeLists.txt:42-48](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L42-L48) —— 检查 `dependencies/glfw/CMakeLists.txt` 是否存在，不存在就直接 `FATAL_ERROR`，并提示运行 `git submodule update --init --recursive`。

**C++ 与 CUDA 标准**：

[CMakeLists.txt:66-67](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L66-L67) —— `CMAKE_CXX_STANDARD 14`，项目用 C++14。

[CMakeLists.txt:73-88](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L73-L88) —— CUDA 标准 14，并追加 `--extended-lambda`、`--expt-relaxed-constexpr`、`--use_fast_math` 等 `nvcc` 编译选项（`--extended-lambda` 是 CUDA 设备端 lambda 的必需开关，本项目大量用到）。

**源码清单 → 静态库 → 两个产物**：

[CMakeLists.txt:266-282](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L266-L282) —— `NGP_SOURCES` 列出所有核心源文件（`testbed.cu`、`testbed_nerf.cu` 等）。

[CMakeLists.txt:347-353](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L347-L353) —— `add_library(ngp STATIC ${NGP_SOURCES})` 把它们编成静态库 `ngp`，并链接 `tiny-cuda-nn`。

[CMakeLists.txt:375-377](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L375-L377) —— `add_executable(instant-ngp src/main.cu)` 从 `ngp` 链接出可执行文件。

[CMakeLists.txt:401-407](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L401-L407) —— `add_library(pyngp SHARED src/python_api.cu)` 链接出 Python 模块（依赖前置找到的 Python 与 pybind11）。

把上述行号串起来，就是「一份 `CMakeLists.txt` 如何组织出整条构建链」的全貌。

#### 4.1.4 代码实践

**实践目标**：亲手跑一遍 README 的两条命令，分清配置与编译两个阶段的产物。

**操作步骤**：

1. 确认子模块已初始化（若不确定）：

   ```sh
   git submodule update --init --recursive
   ```

2. 配置阶段：

   ```sh
   cmake . -B build -DCMAKE_BUILD_TYPE=RelWithDebInfo
   ```

   - `.`：源码目录是当前目录。
   - `-B build`：构建目录是 `build/`（out-of-source 构建，不污染源码树）。
   - `-DCMAKE_BUILD_TYPE=RelWithDebInfo`：带调试信息的优化构建。

3. 编译阶段：

   ```sh
   cmake --build build --config RelWithDebInfo -j
   ```

   - `--config RelWithDebInfo`：多配置生成器（如 Visual Studio）需要显式指定。
   - `-j`：并行编译，全部核心用满。

**需要观察的现象**：

- 配置阶段，终端会打印 CMake 检测到的 CUDA 版本、GPU 架构、是否找到 Vulkan / Python 等。
- 配置成功后，`build/` 目录里会出现 `CMakeCache.txt`、`Makefile`（或 ninja 文件）等。
- 编译阶段会看到大量 `nvcc` / `g++` 调用。

**预期结果**：编译结束后，`build/` 目录下（以及源码根目录，因为脚本会建符号链接/拷贝）出现 `instant-ngp` 可执行文件。若开启了 Python 绑定，还会出现 `pyngp.*.so`。

> 待本地验证：在无 GPU 或 CUDA 版本不匹配的机器上，配置阶段即会失败；具体报错文本取决于本机环境。

#### 4.1.5 小练习与答案

**练习 1**：为什么 instant-ngp 要把核心源码先编成静态库 `ngp`，而不是直接把 `src/main.cu` 和所有 `.cu` 一起编成一个可执行文件？

**参考答案**：因为有两个前端（`instant-ngp` 可执行文件和 `pyngp` Python 模块）都要复用同一套核心逻辑。编成静态库后，两个前端各自 `target_link_libraries(... ngp)` 即可，核心代码只编译一次，既省时间又保证行为一致。

**练习 2**：README 推荐用 `RelWithDebInfo` 而不是 `Release`，二者区别是什么？

**参考答案**：`Release` 是 `-O3 -DNDEBUG` 的纯优化构建；`RelWithDebInfo` 是 `-O2 -g -DNDEBUG`，保留调试符号、便于用调试器定位崩溃位置，优化程度略低。对一个交互式实时程序来说，出问题时能调试往往更重要。

---

### 4.2 可选功能开关

#### 4.2.1 概念说明

instant-ngp 有一批「锦上添花」的能力：图形界面 GUI、硬件光线追踪 OptiX、Python 绑定、运行时编译（RTC）、DLSS 超分。这些能力各自依赖额外的库或硬件，并非所有人都需要。CMake 用 `option()` 机制把它们做成可开关，默认开启，但允许用 `-DNGP_BUILD_WITH_XXX=OFF` 关掉。这在两种场景特别有用：

- **环境受限**：服务器没有 Vulkan、没有显示器，只想编译一个无头（headless）版本。
- **加快编译**：GUI 相关源码很多，关掉能显著缩短首次编译时间。

#### 4.2.2 核心流程

6 个开关及其联动关系如下表：

| 开关 | 默认 | 关闭后会排除/改变的能力 | 关键依赖 |
|------|------|------------------------|----------|
| `NGP_BUILD_EXECUTABLE` | ON | 不生成 `instant-ngp` 命令行程序（只编 `pyngp`） | — |
| `NGP_BUILD_WITH_GUI` | ON | 排除窗口、键盘、ImGui 面板、OpenXR、Vulkan、DLSS、GLFW、GLEW | GLFW、GLEW、imgui、OpenXR-SDK、Vulkan、dlss |
| `NGP_BUILD_WITH_OPTIX` | ON | 排除网格 SDF 的硬件加速真值计算 | OptiX SDK |
| `NGP_BUILD_WITH_PYTHON_BINDINGS` | ON | 不生成 `pyngp` 模块 | pybind11、Python≥3.7 |
| `NGP_BUILD_WITH_RTC` | ON | 排除运行时编译的全融合内核（JIT 融合） | NVRTC |
| `NGP_BUILD_WITH_VULKAN` | ON | 排除 DLSS 超分（仅在 GUI 开启时才有意义） | Vulkan、dlss |

**重要的依赖嵌套**：Vulkan/DLSS 块、OpenXR 块都写在 `if (NGP_BUILD_WITH_GUI)` 内部。也就是说，**一旦关闭 GUI，Vulkan、DLSS、OpenXR 会一起被排除**，单独的 `NGP_BUILD_WITH_VULKAN` 只有在 GUI 开启时才生效。这一点直接关系到本讲的实践任务。

#### 4.2.3 源码精读

**6 个开关的定义**：

[CMakeLists.txt:22-27](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L22-L27) —— 6 个 `option()` 声明，全部默认 `ON`。

**GUI 块（最大的开关分支）**：

[CMakeLists.txt:102-207](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L102-L207) —— 整段在 `if (NGP_BUILD_WITH_GUI)` 内，依次处理 Vulkan/DLSS（102–129）、OpenXR（131–157）、OpenGL/GLFW/GLEW（159–193）、imgui（195–204），并在末尾 `list(APPEND NGP_DEFINITIONS -DNGP_GUI)`（206 行）定义编译宏 `NGP_GUI`。源码各处用 `#ifdef NGP_GUI` 来条件编译 GUI 代码。

注意第 104 行的判断：`if (Vulkan_FOUND AND NGP_BUILD_WITH_VULKAN)` —— Vulkan 只有在「找到了」且「开关开着」时才启用；否则第 122 行 `set(NGP_VULKAN OFF)` 并在第 123–128 行给出警告「Vulkan was not found... DLSS will not be supported」。这正是「机器没有 Vulkan 时 DLSS 自动失效」的来源。

**OptiX 块**：

[CMakeLists.txt:223-227](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L223-L227) —— 开启时加入 OptiX 头文件路径并定义 `NGP_OPTIX` 宏。

**Python 绑定块**：

[CMakeLists.txt:229-234](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L229-L234) —— 找到 Python≥3.7 后 `add_subdirectory(dependencies/pybind11)`。

**RTC 块（运行时编译）**：

[CMakeLists.txt:96](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L96) —— `NGP_BUILD_WITH_RTC` 透传给 tiny-cuda-nn 的 `TCNN_BUILD_WITH_RTC`。

[CMakeLists.txt:337-345](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L337-L345) —— RTC 开启时，用 `cmrc`（CMake Resource Compiler）把 `include/neural-graphics-primitives/` 下的所有头文件打包成资源嵌入二进制，供运行时编译内核时读取。

#### 4.2.4 代码实践（本讲指定任务）

**实践目标**：在一台没有 Vulkan 的机器上，编译一个无 GUI 版本；并解释 git 子模块缺失时会发生什么。

**操作步骤**：

由于 Vulkan/DLSS/OpenXR 都嵌套在 GUI 分支内，**关闭 GUI 即可一并禁用 DLSS**，一行命令搞定：

```sh
cmake . -B build -DNGP_BUILD_WITH_GUI=OFF -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build --config RelWithDebInfo -j
```

- `-DNGP_BUILD_WITH_GUI=OFF`：关闭 GUI。由 [CMakeLists.txt:102](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L102) 的 `if (NGP_BUILD_WITH_GUI)` 守卫，整个 GUI 块被跳过，因此 Vulkan、DLSS、OpenXR、GLFW、GLEW、imgui 全部不会被引入。
- 此时再写 `-DNGP_BUILD_WITH_VULKAN=OFF` 是**冗余的**（因为 Vulkan 检测本就在 GUI 块内，GUI 关了它根本不会执行）。但写上也无害，CMake 会忽略它。

**需要观察的现象**：

- 配置阶段不会再去找 Vulkan、GLFW、GLEW，也不会打印「Vulkan was not found」警告。
- 编译产物里没有 `src/dlss.cu`、`src/openxr_hmd.cu`（它们被排除在 `GUI_SOURCES` 之外）。
- 最终得到的 `instant-ngp` 只能以 `--no-gui` 无头方式运行（GUI 代码已被条件编译剔除）。

**git 子模块缺失时会报什么错**：

如 [CMakeLists.txt:42-48](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L42-L48) 所示，配置阶段会直接抛出 `FATAL_ERROR`，报错信息大致为：

> Some instant-ngp dependencies are missing. If you forgot the "--recursive" flag when cloning this project, this can be fixed by calling "git submodule update --init --recursive".

修复办法就是按提示执行 `git submodule update --init --recursive`。注意这个检查只看 `dependencies/glfw/CMakeLists.txt` 这一个标志文件，所以报错文本提到的是「依赖缺失」而非具体某个库。

#### 4.2.5 小练习与答案

**练习 1**：在一台只有 CPU、完全不想碰 GPU 编译的机器上，能通过关开关把 instant-ngp 编出来吗？

**参考答案**：不能。CUDA 是项目的核心语言（`LANGUAGES C CXX CUDA`，见 [CMakeLists.txt:14](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L14)），且关键算法都在 `.cu` 文件里，没有 GPU / CUDA 工具链无法编译。`NGP_BUILD_WITH_*` 只能关掉 GUI/OptiX/Python/RTC/Vulkan 这些「附加能力」，关不掉 CUDA 本身。

**练习 2**：`NGP_BUILD_WITH_RTC=OFF` 会带来什么后果？

**参考答案**：RTC（运行时编译）被关闭，意味着 JIT 全融合内核（把编码+MLP 融合成单个 GPU 内核）不可用，项目只能回退到非融合的执行路径，性能会下降。同时 [CMakeLists.txt:96](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L96) 会把 `TCNN_BUILD_WITH_RTC` 也设为 OFF，tiny-cuda-nn 内部同样禁用 RTC。

---

### 4.3 CUDA 架构选择

#### 4.3.1 概念说明

每张 NVIDIA GPU 都有一个「计算能力」编号（major.minor），它决定了 GPU 支持的指令集特性。例如：

- Pascal（GTX 10X0）：6.1
- Volta（V100 / TITAN V）：7.0
- Turing（RTX 20X0）：7.5
- Ampere（RTX 30X0 / A100）：8.6 / 8.0
- Ada（RTX 40X0）：8.9
- Hopper（H100）：9.0

`nvcc` 编译时需要知道目标架构，才能生成对应的 GPU 机器码（SASS）和中间表示（PTX）。如果为单一架构编译，产物小、编译快；如果为多架构编译（fat binary），兼容性好但编译时间长。instant-ngp 的做法是**把架构检测完全委托给 tiny-cuda-nn**，再从它那里「抄」结果。

#### 4.3.2 核心流程

```
1. add_subdirectory(dependencies/tiny-cuda-nn)
2. tiny-cuda-nn 内部检测本机 GPU，或读取环境变量 TCNN_CUDA_ARCHITECTURES
3. CMakeLists.txt 第 100 行：CMAKE_CUDA_ARCHITECTURES = TCNN_CUDA_ARCHITECTURES
4. 后续所有 CUDA 目标（ngp、pyngp、optix_program）都用这个架构编译
```

也就是说，**设置 CUDA 架构的正确入口是环境变量 `TCNN_CUDA_ARCHITECTURES`**，而不是 CMake 的原生变量。这是因为检测逻辑（枚举本机显卡、查表）写在 tiny-cuda-nn 里，instant-ngp 只是消费它的结果。

当自动检测失败时（比如一台机器插了多块不同代际的 GPU），就需要手动指定。README 提供了对照表。

#### 4.3.3 源码精读

**架构委托**：

[CMakeLists.txt:94-100](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L94-L100) —— 第 98 行 `add_subdirectory(dependencies/tiny-cuda-nn)` 拉入底层库；第 100 行 `set(CMAKE_CUDA_ARCHITECTURES ${TCNN_CUDA_ARCHITECTURES})` 把 tiny-cuda-nn 算出来的架构赋给本项目所有 CUDA 目标。

**GPU 架构对照表**：

README 在 [README.md:209-213](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L209-L213) 给出了常见 GPU 的计算能力表（H100→90、40X0→89、30X0→86、A100→80、20X0→75、V100→70、10X0→61、9X0→52、K80→37）。设置 `TCNN_CUDA_ARCHITECTURES` 时就用这些数字。

**编译期常量传递**：架构信息除了控制 `nvcc` 生成代码，还会变成编译期宏影响代码路径。例如 [CMakeLists.txt:302](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L302) 给 OptiX 程序传入 `-DTCNN_MIN_GPU_ARCH=0`、`-DTCNN_HALF_PRECISION=...`，让头文件里的条件编译（如是否启用半精度）与目标架构一致。

#### 4.3.4 代码实践

**实践目标**：为一张 RTX 3090（计算能力 8.6）单独指定架构，加速首次编译并观察 `nvcc` 命令。

**操作步骤**：

1. 清理旧构建目录，避免缓存干扰：

   ```sh
   rm -rf build
   ```

2. 设置架构环境变量后再配置：

   ```sh
   TCNN_CUDA_ARCHITECTURES=86 cmake . -B build -DCMAKE_BUILD_TYPE=RelWithDebInfo
   ```

3. 编译时观察底层命令：

   ```sh
   cmake --build build --config RelWithDebInfo -j -- VERBOSE=1
   ```

   （`VERBOSE=1` 让 Makefile 打印实际命令；用 Ninja 生成器则默认就打印。）

**需要观察的现象**：

- 配置阶段日志里会出现形如 `CMAKE_CUDA_ARCHITECTURES = 86` 的信息。
- 编译阶段，`nvcc` 命令行里会带 `-arch=sm_86`（或 `gencode arch=compute_86,code=sm_86`），说明只为一套架构生成代码。
- 相比为多架构编译，本次编译时间明显变短。

**预期结果**：编出的 `instant-ngp` 只能在 sm_86 及更新、且二进制兼容的 GPU 上运行；换到一张更老的 GTX 1080（sm_61）上会因「无可用 GPU 代码」而无法运行。

> 待本地验证：`nvcc` 实际生成的 `gencode` 参数文本依 CUDA 版本而略有差异，以本机日志为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接 `set(CMAKE_CUDA_ARCHITECTURES 86)`，而要走 `TCNN_CUDA_ARCHITECTURES` 这个环境变量？

**参考答案**：因为 instant-ngp 把架构检测逻辑放在了 tiny-cuda-nn 里。tiny-cuda-nn 会综合「自动枚举本机 GPU」与「读取 `TCNN_CUDA_ARCHITECTURES` 环境变量」两路信号来决定最终架构，然后本项目第 100 行再从它那里取值。统一从环境变量入口设置，能保证 instant-ngp 和它依赖的 tiny-cuda-nn 用同一套架构，避免不一致。

**练习 2**：README 的 FAQ 说「多 GPU 只在 VR 渲染时每只眼一块」，这和 `TCNN_CUDA_ARCHITECTURES` 有关吗？

**参考答案**：两者是不同层面的事。运行时多 GPU 选择用 `CUDA_VISIBLE_DEVICES` 环境变量（运行期）；而 `TCNN_CUDA_ARCHITECTURES` 是编译期决定「为哪些架构生成代码」。FAQ 的建议是：当机器装了多块不同代际 GPU 时，自动检测可能选错，此时用 `TCNN_CUDA_ARCHITECTURES` 针对你想跑的那块卡专门编译（见 [README.md:209](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L209)）。

---

## 5. 综合实践

**任务**：为「一台服务器，插了一张 RTX 3090（sm_86），没有显示器、没有 Vulkan、不需要 Python 绑定，但需要 OptiX 加速」设计一组合适的构建命令，并说明每个开关对最终产物的影响。

**参考命令**：

```sh
# 1. 确保子模块齐全
git submodule update --init --recursive

# 2. 配置：关 GUI（顺带关掉 Vulkan/DLSS/OpenXR），关 Python 绑定，指定架构 86
TCNN_CUDA_ARCHITECTURES=86 cmake . -B build \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    -DNGP_BUILD_WITH_GUI=OFF \
    -DNGP_BUILD_WITH_PYTHON_BINDINGS=OFF

# 3. 编译
cmake --build build --config RelWithDebInfo -j
```

**逐项解释**：

- `TCNN_CUDA_ARCHITECTURES=86`：只为 sm_86 生成代码，编译最快，产物最小。
- `-DNGP_BUILD_WITH_GUI=OFF`：排除窗口、ImGui、GLFW、GLEW、OpenXR、Vulkan、DLSS；服务器无显示器，这些用不上，且能省下大量编译时间。`NGP_BUILD_WITH_VULKAN` 不用单独写，因为它嵌在 GUI 块里（[CMakeLists.txt:104](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L104)）。
- `-DNGP_BUILD_WITH_PYTHON_BINDINGS=OFF`：不生成 `pyngp`，[CMakeLists.txt:229-234](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L229-L234) 的 pybind11 块被跳过。
- `NGP_BUILD_WITH_OPTIX` 与 `NGP_BUILD_WITH_RTC` 保持默认 ON：保留硬件光线追踪真值与运行时编译融合，性能最优。

**预期产物**：`build/instant-ngp` 一个可执行文件，无 `pyngp`，无 GUI，只能 `./instant-ngp --no-gui` 无头运行；由于关了 GUI，`--no-gui` 已是唯一运行方式。

**验证要点**：配置日志中应能看到 CUDA 架构为 86、未检测 Vulkan、未生成 pyngp 目标；编译产物列表里不含 `dlss.cu`、`openxr_hmd.cu`、`python_api.cu` 对应的目标。

> 待本地验证：以上行为需在本机实际执行配置/编译后，核对日志与产物目录确认。

## 6. 本讲小结

- CMake 构建分**配置**与**编译**两阶段：`cmake -B build` 检测环境、生成工程文件，`cmake --build build` 真正编译；README 的两条命令就是这两步。
- 整个项目的构建链是「核心源码 → 静态库 `ngp` → 两个产物（`instant-ngp` 可执行文件 + `pyngp` Python 模块）」，核心逻辑只编译一次。
- 子模块缺失会在配置阶段最早期触发 `FATAL_ERROR`，修复办法是 `git submodule update --init --recursive`。
- 6 个 `NGP_BUILD_WITH_*` 开关控制 GUI / OptiX / Python / RTC / Vulkan 等附加能力；**Vulkan、DLSS、OpenXR 都嵌套在 GUI 分支内**，关 GUI 即一并关闭。
- CUDA 架构通过环境变量 `TCNN_CUDA_ARCHITECTURES` 指定（委托给 tiny-cuda-nn 检测），值是 GPU 计算能力数字（如 RTX 3090 = 86）。
- `requirements.txt` 是 `scripts/` 下 Python 脚本的运行依赖（numpy、opencv 等），与 C++ 编译本身无关。

## 7. 下一步学习建议

下一讲 [u1-l4 命令行运行与示例场景](u1-l4-cli-and-scenes.md) 将进入 `src/main.cu`，看编译出的 `instant-ngp` 可执行文件如何解析命令行参数（`--scene`、`--network`、`--no-gui` 等）、加载文件并进入 `frame()` 主循环。

进阶方向：当你以后想理解 OptiX 的 PTX 是如何被打包进头文件、或 RTC 如何把 `include/` 下的头文件嵌入二进制时，可以回看本讲引用的 [CMakeLists.txt:294-335](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L294-L335)（OptiX PTX 打包）与 [CMakeLists.txt:337-345](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L337-L345)（cmrc 资源嵌入），它们对应 u8（专家层）的 JIT 融合与 OptiX 讲义。
