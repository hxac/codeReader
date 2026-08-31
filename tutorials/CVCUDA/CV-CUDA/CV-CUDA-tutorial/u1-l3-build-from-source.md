# 从源码构建：CMake preset 与 Docker 开发镜像

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `build.sh` 做了哪些事：它如何包装 `cmake` 命令、如何选择编译器/生成器/并行度、如何控制构建目录。
2. 列出 `dev` / `dev-debug` / `dev-py` / `dev-bench` 四个 CMake preset 的差异与各自用途。
3. 读懂根 `CMakeLists.txt` 里 `BUILD_*` 开关矩阵，预测任意参数组合下会构建出哪些东西。
4. 说明 CV-CUDA 的依赖解析方式是「全 `find_package`、无 `FetchContent`」，并据此解释 Docker devel 镜像为什么必须预装 googletest、nvbench、pybind11。
5. 独立完成一次源码构建（或在无 CUDA 环境下产出一份「缺失依赖清单」）。

本讲是第 1 单元的第三讲。前两讲我们用 pip 安装的 wheel「会用」了 CV-CUDA；从这一讲开始，我们为后续「读源码、改源码」做准备 —— 先把项目从源码构建起来。

## 2. 前置知识

### 2.1 CMake 的两阶段模型

CMake 是 C/C++ 项目的「构建系统生成器」，工作分两步：

- **配置阶段（configure）**：`cmake -B <构建目录> <源码目录>`。CMake 读取 `CMakeLists.txt`，检查依赖是否存在，把编译选项写进构建目录的 `CMakeCache.txt`。
- **构建阶段（build）**：`cmake --build <构建目录>`。CMake 调用真正的底层工具（**生成器**，generator）执行编译。CV-CUDA 用 **Ninja**（速度快）或默认的 Unix Makefiles。

配置阶段用 `-D` 传入的变量叫**缓存变量**（如 `-DBUILD_TESTS=OFF`），会持久保存在 `CMakeCache.txt` 中；`option(...)` 定义的开关就是一类典型的缓存变量。

### 2.2 什么是 CMake preset

`CMakePresets.json` 把「一组常用的 `-D` 参数 + 生成器 + 构建目录」打包成一个有名字的预设。之后只需：

```bash
cmake --preset dev           # 等价于带一长串 -D 参数的配置
cmake --build --preset dev   # 构建同一个预设
```

preset 的价值是**固化团队约定**：所有人用同样的开关组合，不用记忆参数。

### 2.3 CUDA 编译基础

- **nvcc**：NVIDIA 的 CUDA 编译器，通常装在 `/usr/local/cuda/bin/nvcc`。
- **SM 架构**：每代 GPU 有一个计算能力编号（如 Turing=7.5、Ampere=8.0/8.6、Hopper=9.0、Blackwell=10.0/12.0）。`CMAKE_CUDA_ARCHITECTURES` 决定为哪些架构生成 GPU 代码；列的架构越多，编译越慢。特殊值 `native` 表示「只编译当前机器上这块 GPU 的架构」，是最快的选择。
- **CUDA Toolkit**：nvcc + 头文件 + 库的合集。`find_package(CUDAToolkit)` 就是找它。

### 2.4 其他小概念

- **动态库命名**：目标 `foo` 构建为 SHARED 库时产物是 `libfoo.so`；Debug 构建可加后缀（本项目用 `_d`）区分。
- **ccache**：编译缓存，重复编译未改动的文件时直接命中缓存，大幅加速增量构建。
- **find_package vs FetchContent**：前者要求依赖**已经在系统里**（找不到就报错）；后者会在配置阶段**自动下载**依赖源码一起编译。CV-CUDA 只用前者 —— 这是本讲的一个重要结论。
- **Docker**：镜像（image）是只读模板，容器（container）是运行实例；`--gpus all` 让容器能用宿主机 GPU；`-v 宿主机路径:/workspace` 把源码挂进容器。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [CMakeLists.txt](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakeLists.txt) | 仓库根构建脚本：定义项目、`BUILD_*` 开关、装配 src/python/tests/bench/samples/docs 子树 |
| [build.sh](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/build.sh) | 官方一键构建脚本：包装 cmake，自动选编译器、Ninja、ccache、并行度 |
| [CMakePresets.json](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakePresets.json) | 四个开发预设 dev / dev-debug / dev-py / dev-bench |
| [docker/README.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docker/README.md) | Docker 基础设施说明：builder 镜像与 devel 镜像两族 |
| [docker/Dockerfile.devel.deps](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docker/Dockerfile.devel.deps) | devel 镜像的构建配方：预装 GTest、pybind11、nvbench 等 |
| [cmake/ConfigCUDA.cmake](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/cmake/ConfigCUDA.cmake) | CUDA 版本检查、架构策略、nvcc 编译flag |
| [cmake/CUDAArchitecturePolicy.cmake](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/cmake/CUDAArchitecturePolicy.cmake) | 决定「为哪些 GPU 架构编译」的策略模块 |
| [cmake/ConfigBuildTree.cmake](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/cmake/ConfigBuildTree.cmake) | 构建产物输出目录（bin/lib）、平台检测、DSO 属性 |
| [src/CMakeLists.txt](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/CMakeLists.txt) | src 子树入口：开 PIC，进入 nvcv 与 cvcuda 两个子目录 |
| [docs/sphinx/installation.rst](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/installation.rst) | 官方安装/构建文档（本讲实践的对齐依据） |

## 4. 核心概念与源码讲解

### 4.1 根 CMakeLists.txt：整个仓库的「总装车间」

#### 4.1.1 概念说明

回顾 u1-l1 的分层图：仓库由 `src`（C++/CUDA 库）、`python`（绑定）、`tests`、`bench`、`samples`、`docs` 几棵子树组成。根 `CMakeLists.txt` 就是「总装车间」：它不编译任何具体文件，只做三件事：

1. 声明项目与语言（C、C++、CUDA）；
2. 定义一组 `BUILD_*` 开关，让用户按需裁剪构建内容；
3. 按开关把各棵子树 `add_subdirectory` 挂进构建树。

理解了这份文件，你就能回答「一条构建命令到底会产出什么」。

#### 4.1.2 核心流程

配置阶段按以下顺序执行（伪代码）：

```text
cmake_minimum_required(3.20.1)
project(cvcuda, C+CXX, 版本 0.17.0)
记录 CMAKE_CUDA_ARCHITECTURES 的来源（显式/环境变量/自动生成）
CUDA host compiler ← 默认等于 C++ 编译器
enable_language(CUDA)
安装前缀默认 → /opt/nvidia/cvcuda<主版本号>
定义 BUILD_* 开关（见下表）
BUILD_TESTS=ON 时连带强制开启三个测试子开关
挂载 cmake/ 模块目录，include 一批 Config*.cmake
检查 CUDA Toolkit 里有 nvtx3 头文件（找不到直接 FATAL_ERROR）
if BUILD_LIB:        进入 src/（→ nvcv + cvcuda）
    if BUILD_PYTHON: 挂载 python 绑定
    if 测试开关:      进入 tests/
    if BUILD_DOCS:   进入 docs/
if BUILD_BENCH:  find_package(nvbench) → 进入 bench/
if BUILD_SAMPLES: 进入 samples/
打印配置摘要（PrintConfig）
```

`BUILD_*` 开关矩阵（默认值来自源码）：

| 开关 | 默认 | 控制内容 |
|------|------|----------|
| `BUILD_LIB` | ON | 是否在本树内构建 cvcuda + nvcv_types 库；OFF 时供下游（bench 等）对已安装的 cvcuda-dev 编译 |
| `BUILD_TESTS` | ON | 总开关；ON 时强制开启下面三个子开关（除非命令行显式关闭） |
| `BUILD_TESTS_CPP` / `BUILD_TESTS_WHEELS` / `BUILD_TESTS_PYTHON` | OFF | C++ 测试 / wheel 测试脚本 / Python 测试 |
| `BUILD_PYTHON` | ON | Python 绑定 |
| `BUILD_PYTHON_WHEEL` | ON | 把绑定打包成 wheel |
| `BUILD_BENCH` | OFF | 基准（需要系统级 nvbench） |
| `BUILD_SAMPLES` | OFF | 示例 |
| `BUILD_DOCS` | OFF | 文档 |
| `ENABLE_SANITIZER` | OFF | 地址消毒器构建 |

注意一个容易踩的坑：**preset `dev` 设置了 `BUILD_TESTS=ON`，而 `BUILD_TESTS=ON` 会连带把 `BUILD_TESTS_CPP` 强制打开**，所以「最快的 dev 预设」也需要系统里装有 GoogleTest —— 这正是 Docker devel 镜像预装 `libgtest-dev` 的原因（见 4.5）。

#### 4.1.3 源码精读

**项目声明与最小 CMake 版本**。[CMakeLists.txt:L16-L22](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakeLists.txt#L16-L22)：项目名为 `cvcuda`，版本 0.17.0，先只启用 C/C++ 两种语言（CUDA 稍后手动启用，为的是先记录架构来源）。

**架构来源探测**。[CMakeLists.txt:L28-L29](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakeLists.txt#L28-L29) 在启用 CUDA 语言**之前**调用 `cvcuda_detect_cuda_architecture_source()`，区分「用户显式指定 / 环境变量 `CUDAARCHS` / CMake 自动生成」三种来源，供后面的策略模块使用。

**开关定义**。[CMakeLists.txt:L52-L62](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakeLists.txt#L52-L62) 定义上表全部 `option`。读 `BUILD_LIB` 的描述可知它的第二用途：OFF 时本树只编译「下游消费者」，CI 上用它对着已安装的 cvcuda-dev 编译 bench。

**BUILD_TESTS 的传染逻辑**。[CMakeLists.txt:L74-L84](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakeLists.txt#L74-L84)：`BUILD_TESTS=ON` 时，只要子开关没被命令行显式设为假值，就 `FORCE` 成 ON。这就是「preset dev 也会构建 C++ 测试」的机制来源。

**NVTX 头文件检查**。[CMakeLists.txt:L114-L125](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakeLists.txt#L114-L125) 定义接口目标 `cvcuda_nvtx_config`，并用 `find_path` 在 CUDA Toolkit 的 include 路径里找 `nvtx3/nvToolsExt.h`，找不到就 `FATAL_ERROR`。这是一个常见报错点：装了「CUDA 运行时」而非完整 toolkit 的机器会在这里失败。

**构建树装配**。[CMakeLists.txt:L141-L168](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakeLists.txt#L141-L168)：`BUILD_LIB` 时进入 `src`，再按 `BUILD_PYTHON` / 测试开关 / `BUILD_DOCS` 决定挂载哪些子树；`BUILD_BENCH` 时先 `find_package(nvbench REQUIRED)` 再进入 `bench`；`BUILD_SAMPLES` 进入 `samples`。

**src 子树入口**。[src/CMakeLists.txt:L16-L28](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/CMakeLists.txt#L16-L28)：因为产物是共享库，全树开位置无关代码（PIC），然后进入 `nvcv` 与 `cvcuda` 两个子目录。

**两个最终库目标**。[src/nvcv/src/CMakeLists.txt:L19-L36](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/CMakeLists.txt#L19-L36) 定义 `add_library(nvcv_types ...)`（u1-l1 讲过的 nvcv 类型层），[src/cvcuda/CMakeLists.txt:L106-L112](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/CMakeLists.txt#L106-L112) 定义 `add_library(cvcuda SHARED ...)` 并静态链接 `CUDA::cudart_static`。由于没有设置 `OUTPUT_NAME`，产物文件名就是目标名：**`libnvcv_types.so` 与 `libcvcuda.so`**（注意：不是 `libnvcv.so`）。

#### 4.1.4 代码实践

**实践目标**：不运行任何命令，仅凭根 `CMakeLists.txt` 预测两种配置分别会构建什么，锻炼「读构建脚本推产物」的能力。

**操作步骤**：

1. 通读 [CMakeLists.txt:L52-L62](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakeLists.txt#L52-L62) 的开关默认值。
2. 场景 A：`bash build.sh`（不带任何额外参数）；场景 B：`cmake --preset dev`。分别列出会被 `add_subdirectory` 的目录清单。
3. 对照 [CMakeLists.txt:L141-L168](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakeLists.txt#L141-L168) 逐个核对。

**需要观察的现象**：场景 A 使用全部默认值（`BUILD_PYTHON=ON`、`BUILD_TESTS=ON`），会构建库 + Python 绑定 + wheel + 测试；场景 B（preset `dev`）显式把 `BUILD_PYTHON` 设为 OFF，只构建库 + 测试。

**预期结果**：两张目录清单：A 包含 `src`、`python`、`tests`；B 只包含 `src`、`tests`。若想验证，可在有 CUDA 的机器上分别配置后查看配置末尾的 `PrintConfig` 摘要输出。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cmake --preset dev` 的描述说是 "Fast build"，它快在哪里？

**答案**：两个原因：一是 `CMAKE_CUDA_ARCHITECTURES=native`，只为当前机器的 GPU 编译一份架构代码（默认策略要为多代架构编译，见 4.4.3）；二是它关掉了 Python 绑定、wheel 打包、bench、samples、docs，编译目标数量大幅减少（但注意它保留 `BUILD_TESTS=ON`，仍会编译 C++ 测试）。

**练习 2**：CI 想在一个只装了 `cvcuda-dev` 包（库已预装）的 pod 里只编译 bench，应该设什么开关？

**答案**：`BUILD_LIB=OFF` + `BUILD_BENCH=ON`。`BUILD_LIB=OFF` 跳过库本身的构建，bench 子树会通过 `find_package(cvcuda CONFIG REQUIRED)` 找到已安装的库（见根 CMakeLists.txt L157-L159 的注释）。

**练习 3**：`BUILD_TESTS=OFF` 但 `BUILD_TESTS_CPP=ON` 同时给出时，会发生什么？

**答案**：会构建 C++ 测试。[CMakeLists.txt:L87-L88](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakeLists.txt#L87-L88) 的逻辑是：总开关 OFF 时，子开关只要在命令行显式给了真值就仍然强制 ON —— 显式指定优先于总开关默认。

### 4.2 build.sh：官方一键构建脚本

#### 4.2.1 概念说明

`build.sh` 是仓库官方的构建入口（[docs/sphinx/installation.rst:L249-L265](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/installation.rst#L249-L265) 称之为 "central build.sh script"）。它**不使用 preset**，而是一个薄包装：把「环境探测 + 常用默认值」翻译成一长串 `-D` 参数和命令行选项后调用原生 cmake。它解决的是裸用 cmake 时的几个烦人问题：

- 装了多个 gcc 版本时选哪个；
- 有没有 Ninja / ccache 可用；
- 控制并行度避免内存爆掉；
- 先生成 requirements 文件再配置。

#### 4.2.2 核心流程

```text
解析命令行: build.sh [debug|release] [构建目录] [额外 cmake 参数]
  → 默认 release；目录默认 build-rel / build-deb
并行度 num_jobs = 环境变量 CVCUDA_BUILD_JOBS 或 nproc
选择编译器: CC/CXX = 环境变量，否则 /usr/bin 下最新的 gcc/g++
有 ninja → 追加 -G Ninja
有 ccache → 记录统计日志路径
存在 /usr/local/cuda/bin/nvcc → 指定 CUDA 编译器
运行 generate_requirements.sh 生成各 requirements.txt
cmake -B 构建目录 源码目录 <全部参数> -DCVCUDA_BUILD_JOBS=<num_jobs>
cmake --build 构建目录 -- -j<num_jobs>
```

#### 4.2.3 源码精读

**默认值与并行度**。[build.sh:L31-L40](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/build.sh#L31-L40)：构建类型默认 `release`；并行度取环境变量 `CVCUDA_BUILD_JOBS`，未设置则 `nproc`，且校验必须是正整数，非法时回退并告警。注释说明这个预算会一路传给 Python wheel 的子构建，限制总扇出。

**命令行解析**。[build.sh:L43-L62](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/build.sh#L43-L62)：第一个参数若是 `debug`/`release` 就当作构建类型，第二个参数当作构建目录；否则第一个参数直接是构建目录。目录未指定时用 `build-${build_type:0:3}` 截前三个字符 —— 这就是 `build-rel` 与 `build-deb` 的由来。

**编译器与工具探测**。[build.sh:L80-L100](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/build.sh#L80-L100) 做四件事：用 `find /usr/bin/gcc-1* | sort -rV | head -n 1` 挑最新版 gcc（可用 `CC`/`CXX` 环境变量覆盖）；有 `ninja` 就追加 `-G Ninja` 并设置状态输出格式；有 `ccache` 就把统计日志路径传给构建系统；若 `/usr/local/cuda/bin/nvcc` 存在则显式指定 CUDA 编译器。

**最后的执行**。[build.sh:L103-L108](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/build.sh#L103-L108)：先运行 `generate_requirements.sh`（从 `versions.env` 模板渲染出 tests/bench/samples 等目录的 requirements.txt —— 这些文件是生成物，不要手改，见仓库规范），然后 `cmake -B ... -DCVCUDA_BUILD_JOBS=...` 配置、`cmake --build ... -- -j<num_jobs>` 构建。

顺带一提，[cmake/ConfigBuildTree.cmake:L22-L23](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/cmake/ConfigBuildTree.cmake#L22-L23) 把所有库产物指到 `<构建目录>/lib`、可执行文件指到 `<构建目录>/bin`；[L16](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/cmake/ConfigBuildTree.cmake#L16) 设置 Debug 后缀 `_d`，让 Debug 产物（如 `libcvcuda_d.so`）与 Release 区分开。

#### 4.2.4 代码实践

**实践目标**：把 `build.sh` 当作一份「参数 → 行为」规格来读，能对任意调用方式预测其行为。

**操作步骤**：

1. 通读脚本，填写下表（示例第一行已给出）：

| 调用方式 | 构建类型 | 构建目录 | 并行度 |
|----------|----------|----------|--------|
| `bash build.sh` | release | build-rel | nproc |
| `bash build.sh debug` | ？ | ？ | ？ |
| `CVCUDA_BUILD_JOBS=8 bash build.sh mydir -DBUILD_TESTS=0` | ？ | ？ | ？ |

2. 在有 CUDA 与 Docker 的机器上，按 [docs/sphinx/installation.rst:L303-L315](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/installation.rst#L303-L315) 的示例执行 `bash build.sh`，观察输出里 CMake 打印的配置摘要。

**需要观察的现象**：配置摘要中各 `BUILD_*` 开关的取值；构建结束时 `<构建目录>/lib` 下出现的 `.so` 文件。

**预期结果**：表格答案依次为 `(debug, build-deb, nproc)`、`(release, mydir, 8)`；`build-rel/lib` 下至少出现 `libnvcv_types.so` 与 `libcvcuda.so`。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么构建脚本要专门设 `CVCUDA_BUILD_JOBS`，而不是直接让用户传 `-j`？

**答案**：因为 CV-CUDA 构建会派生 Python wheel 的**子构建**（见 build.sh L105-L107 的注释），外层 `-j` 管不到子构建的扇出；把作业预算作为 CMake 变量传入（`-DCVCUDA_BUILD_JOBS`），子构建才能读到并限制合并后的总并行度，避免小内存机器上 nvcc 多进程并发把内存打爆。

**练习 2**：`build.sh` 和 `cmake --preset dev` 都能构建项目，两者在**编译器选择**上的行为差异是什么？

**答案**：`build.sh` 会自动挑选 `/usr/bin` 下版本号最高的 gcc/g++（除非 `CC`/`CXX` 环境变量已设置）；preset 完全不管编译器，用 CMake 默认（通常是 PATH 里第一个 gcc）。所以在装了多个 gcc 的机器上，两条路线可能用不同编译器。

**练习 3**：脚本为什么在 cmake 之前先运行 `generate_requirements.sh`？

**答案**：tests/bench/samples/docs/docker 目录下的 requirements.txt 是由 `.template` 文件加根目录 `versions.env` 渲染出来的生成物。构建含 Python 绑定/测试/wheel 的配置需要这些文件存在，脚本把它们统一渲染一次，保证后续子构建能找到依赖清单。

### 4.3 CMakePresets.json：四个开发预设

#### 4.3.1 概念说明

[CMakePresets.json](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakePresets.json) 是给**本仓库开发者日常迭代**用的快速入口，与 `build.sh`（面向完整构建/打包）互补。它定义四个配置预设，全部以 `dev` 为基座通过 `inherits` 派生：

| 预设 | 构建类型 | 相对 dev 的差异 | 构建目录 | 用途 |
|------|----------|-----------------|----------|------|
| `dev` | Release | —— | `build-dev` | 日常本地开发：只编译本机 GPU 架构 + C++ 测试 |
| `dev-debug` | Debug | 类型改 Debug | `build-dev-dbg` | 需要断点/调试信息的场景 |
| `dev-py` | Release | `BUILD_PYTHON=ON` | `build-dev-py` | 开发 Python 绑定时用 |
| `dev-bench` | Release | `BUILD_BENCH=ON` | `build-dev-bench` | 跑基准；**要求系统已安装 nvbench** |

四个预设有共同基线：生成器固定 Ninja（因此 preset 路线**硬性依赖 ninja**）、`CMAKE_CUDA_ARCHITECTURES=native`、`BUILD_TESTS=ON`、Python/bench/samples/docs 全 OFF。

#### 4.3.2 核心流程

```text
$ cmake --preset dev
  → 读 CMakePresets.json 中 name="dev" 的 configurePreset
  → 在 ${sourceDir}/build-dev 生成构建树（Ninja）
  → 注入 cacheVariables: Release / native / TESTS=ON / 其他=OFF
$ cmake --build --preset dev
  → 读同名 buildPreset → 对 build-dev 执行实际编译
```

#### 4.3.3 源码精读

**文件头与版本声明**。[CMakePresets.json:L2-L7](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakePresets.json#L2-L7)：preset 格式 `version: 6`，并声明 CMake 最低 3.24。

**基座预设 dev**。[CMakePresets.json:L10-L25](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakePresets.json#L10-L25)：描述明确写着 "detects the installed GPU and compiles only for that architecture"，对应 `CMAKE_CUDA_ARCHITECTURES: "native"`；构建目录 `${sourceDir}/build-dev`；除 `BUILD_TESTS=ON` 外其余全 OFF。这就是「快速本地迭代」配置的全部秘密。

**三个派生预设**。[CMakePresets.json:L27-L55](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakePresets.json#L27-L55)：`dev-debug` 只改 `CMAKE_BUILD_TYPE`；`dev-py` 只把 `BUILD_PYTHON` 翻成 ON；`dev-bench` 只把 `BUILD_BENCH` 翻成 ON，且描述里注明 "Requires nvbench installed system-wide"（因为根 CMakeLists 对 nvbench 是 `find_package(... REQUIRED)`，见 4.4）。

**构建预设**。[CMakePresets.json:L57-L74](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakePresets.json#L57-L74)：每个 configurePreset 都配一个同名 buildPreset，让 `cmake --build --preset dev` 知道去哪个目录构建。

**preset 与架构默认策略的关系**。[cmake/CUDAArchitecturePolicy.cmake:L8-L21](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/cmake/CUDAArchitecturePolicy.cmake#L8-L21) 先探测架构列表的来源（显式缓存值 / `CUDAARCHS` 环境变量 / 未指定）。preset 显式给了 `native`，属于「用户显式指定」，策略模块就不再注入默认列表；若什么都不给（`build.sh` 路线默认如此），[cmake/CUDAArchitecturePolicy.cmake:L28-L43](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/cmake/CUDAArchitecturePolicy.cmake#L28-L43) 会在 x86_64 上生成多架构列表（80-real、90-real，CUDA ≥ 12.8 再加 100-real、120-real，外加基线 75-real），编译时间成倍增加 —— 这就是发布构建慢、dev 构建快的根源。

#### 4.3.4 代码实践

**实践目标**：体会「同一份 CMakeLists，两种入口，产物目录与内容不同」。

**操作步骤**：

1. 在有 CUDA 环境的机器上执行：

```bash
cmake --preset dev          # 配置到 build-dev/
cmake --build --preset dev  # 编译
ls build-dev/lib build-dev/bin
```

2. 如果机器没装 GoogleTest，配置会在 `tests/CMakeLists.txt` 的 `find_package(GTest REQUIRED)` 处失败；用下面的方式绕过再试一次：

```bash
cmake --preset dev -DBUILD_TESTS=OFF   # preset 可与额外 -D 参数叠加
```

3. 对比 `build.sh release build-rel`（如果也执行了）与 `build-dev` 两个目录里的产物差异。

**需要观察的现象**：`build-dev/lib` 下的 `.so` 文件名；`build-dev/bin` 下的测试可执行文件与 `run_tests.sh`；无 GTest 时的报错信息。

**预期结果**：`build-dev/lib` 出现 `libnvcv_types.so`、`libcvcuda.so`；`build-dev/bin` 出现测试驱动脚本（[tests/CMakeLists.txt:L50-L55](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/CMakeLists.txt#L50-L55) 会把 `run_tests.sh.in` 渲染到 bin 目录）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：preset `dev-py` 相比 `dev` 多构建什么？它为什么单独成一个预设而不是默认打开？

**答案**：多构建 Python 绑定（`BUILD_PYTHON=ON`，连带 wheel 打包默认也是 ON）。Python 绑定要为每个 Python 版本各编译一份 `_cvcuda` 扩展模块，构建时间和依赖（pybind11、对应版本的 Python 头文件）都显著增加；只在开发绑定时才值得付出。

**练习 2**：为什么 `dev-bench` 的描述特别强调需要系统级 nvbench，而其他预设不用？

**答案**：因为根 [CMakeLists.txt:L160-L163](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakeLists.txt#L160-L163) 对 nvbench 使用 `find_package(nvbench REQUIRED)`，且仓库不用 FetchContent 自动下载，找不到就直接配置失败。dev/dev-py 不开 `BUILD_BENCH`，自然碰不到这个依赖。

**练习 3**：`cmake --preset dev` 之后能否再用 `cmake --preset dev-py`？会发生什么？

**答案**：不推荐。两个预设的 `binaryDir` 不同（`build-dev` vs `build-dev-py`），第二次配置会在新目录生成一棵独立的构建树，互不干扰但占磁盘；若想改当前树的开关，应对同一目录用 `cmake -B build-dev -DBUILD_PYTHON=ON` 这类增量配置，而不是换预设。

### 4.4 依赖解析与构建产物：全 find_package、无 FetchContent

#### 4.4.1 概念说明

现代 CMake 项目获取第三方依赖有两条主流路线：

- **FetchContent**：配置阶段自动 clone/下载依赖源码并一起编译。优点是开箱即用，缺点是构建时间变长、版本锁定在 CMake 脚本里、离线环境麻烦。
- **find_package**：假设依赖**已经装在系统里**，只负责定位。找不到就报错，由用户/镜像负责准备环境。

**CV-CUDA 全仓库没有任何 `FetchContent` 调用**（可用 `grep -r FetchContent` 验证），所有第三方依赖一律 `find_package`。这个设计决策直接解释了两件事：一是为什么构建前要装一堆系统依赖；二是为什么官方要维护一个预装好一切的 Docker devel 镜像（4.5）。

#### 4.4.2 核心流程

配置阶段的依赖检查链条（按触发顺序）：

```text
enable_language(CUDA)
  → 需要 nvcc 在 PATH 或 CMAKE_CUDA_COMPILER 指定
ConfigBuildTree/ConfigCUDA: find_package(CUDAToolkit REQUIRED)
  → 需要 CUDA Toolkit（含 nvtx3 头文件，根 CMakeLists 再查一次）
根 CMakeLists: find_path(nvtx3/nvToolsExt.h)
BUILD_TESTS_CPP=ON → tests/: find_package(GTest REQUIRED)
BUILD_PYTHON=ON    → python/: find_package(pybind11 REQUIRED CONFIG)
                     python/mod_cvcuda/: find_package(dlpack CONFIG REQUIRED)
BUILD_BENCH=ON     → 根: find_package(nvbench REQUIRED)
任何一个找不到 → 配置立即失败（REQUIRED）
```

#### 4.4.3 源码精读

**CUDA 版本与工具包**。[cmake/ConfigCUDA.cmake:L21-L28](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/cmake/ConfigCUDA.cmake#L21-L28)：按 nvcc 版本 `find_package(CUDAToolkit x.y REQUIRED)`，且低于 **CUDA 12.2** 直接 `FATAL_ERROR` —— 这就是 u1-l1 兼容矩阵里 "CUDA 12.2+" 的代码出处。

**nvcc 编译效率选项**。[cmake/ConfigCUDA.cmake:L34-L45](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/cmake/ConfigCUDA.cmake#L34-L45)：压缩 fatbin 减小产物体积；每个 nvcc 进程内部开 `--threads 4` 并行编译多架构（上限设为 4 是避免与外层构建并行度叠加把机器打满）。

**测试依赖 GTest**。[tests/CMakeLists.txt:L29-L34](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/CMakeLists.txt#L29-L34)：`enable_testing()` 后 `find_package(GTest REQUIRED)`。GCC-10 配系统 libgtest 可能有 ABI 问题，只有 GCC-11+ 才有完整测试覆盖（L31-L33 的警告）。

**Python 侧依赖**。[python/CMakeLists.txt:L40-L42](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/CMakeLists.txt#L40-L42) 要求 `pybind11`（CONFIG 模式）；[python/mod_cvcuda/CMakeLists.txt:L16-L19](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/CMakeLists.txt#L16-L19) 额外要求 `dlpack`（u2-l4 会讲 DLPack 互操作，头文件就来自这里）。

**产物落位与库属性**。[cmake/ConfigBuildTree.cmake:L22-L28](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/cmake/ConfigBuildTree.cmake#L22-L28)：库进 `<build>/lib`、可执行文件进 `<build>/bin`；安装时库装到 `lib/<架构三元组>`（如 `lib/x86_64-linux-gnu`）并写 RPATH。[cmake/ConfigBuildTree.cmake:L93-L107](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/cmake/ConfigBuildTree.cmake#L93-L107) 的 `setup_dso()` 给两个 `.so` 设置 SONAME 版本、裁剪未用符号、静态链接 C++ 标准库 —— 让发布的二进制更自包含。

#### 4.4.4 代码实践

**实践目标**：亲手产出一份「本机构建 CV-CUDA 的缺失依赖清单」—— 这在接手任何新机器时都适用，而且**不需要 GPU 也能做**。

**操作步骤**：

1. 逐项填写下表（把「要求它的位置」一列填完整）：

| 依赖 | find_package 名称 | 要求它的位置 |
|------|-------------------|--------------|
| CUDA Toolkit ≥ 12.2 | `CUDAToolkit` | cmake/ConfigCUDA.cmake:21 |
| NVTX3 头文件 | （`find_path` nvtx3/nvToolsExt.h） | ？ |
| GoogleTest | `GTest` | ？ |
| pybind11 | `pybind11` (CONFIG) | ？ |
| DLPack 头文件 | `dlpack` (CONFIG) | ？ |
| nvbench | `nvbench` | ？ |

2. 在当前机器（很可能没有 CUDA）执行 `cmake --preset dev`，记录**第一个** FATAL_ERROR 报错。
3. 每解决一个依赖就重新配置，直到成功或列完清单。

**需要观察的现象**：报错出现在哪一行 CMake 脚本；报错信息里提到的 find_package 名。

**预期结果**：无 CUDA 机器上第一个失败点通常是 CUDA 语言启用或 `find_package(CUDAToolkit)`；表内答案依次为根 CMakeLists.txt:115、tests/CMakeLists.txt:34、python/CMakeLists.txt:40、python/mod_cvcuda/CMakeLists.txt:19、根 CMakeLists.txt:161。**待本地验证**（不同机器报错顺序可能不同，取决于最先缺失的依赖）。

#### 4.4.5 小练习与答案

**练习 1**：CV-CUDA 为什么选 find_package 而不是 FetchContent？

**答案**：CV-CUDA 是 NVIDIA 官方维护的库，依赖（CUDA Toolkit、pybind11、nvbench 等）都有明确的版本矩阵，并通过 Docker 镜像与 `versions.env` 统一固化。find_package 让构建可复现、离线可用、不把第三方源码混进构建树；代价是环境准备成本 —— 官方用预装好的 devel 镜像（4.5）来偿付这笔成本。

**练习 2**：配置时报错 `nvtx3/nvToolsExt.h not found`，但 `nvcc --version` 正常。可能是什么问题？

**答案**：机器装的是精简版/仅运行时组件的 CUDA，缺少 NVTX 头文件。根 [CMakeLists.txt:L114-L124](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakeLists.txt#L114-L124) 只在 CUDA Toolkit 的 include 路径里找这个头文件（`NO_DEFAULT_PATH`），装完整 toolkit 或补上 NVTX 组件即可。

**练习 3**：构建完成后去哪里找 `libcvcuda.so`？Debug 构建里它叫什么？

**答案**：`<构建目录>/lib/libcvcuda.so`（如 `build-dev/lib/`）。Debug 构建因 `CMAKE_DEBUG_POSTFIX "_d"`（cmake/ConfigBuildTree.cmake:16）带后缀，形如 `libcvcuda_d.so`。

### 4.5 Docker 开发镜像：为什么预装 googletest、nvbench、pybind11

#### 4.5.1 概念说明

[docker/README.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docker/README.md) 描述了两族镜像：

- **Builder 镜像**：manylinux 底子，用于构建**可再分发的包**（wheel/deb）。锁定 gcc-10 与特定 CUDA 版本（12.2/12.5/13.0/13.3），保证产物的 glibc 兼容性。
- **Development（devel）镜像**：Ubuntu 底子（基于 NVIDIA 官方 `nvidia/cuda:*-devel-ubuntu*`），装满开发/测试工具链，面向日常开发。

因为 4.4 的结论 —— 依赖全靠 `find_package`、构建过程**一个依赖都不下载** —— devel 镜像的本质就是「把 4.4 依赖清单一次性装好的环境」：`libgtest-dev`（GTest）、系统级 nvbench、每个 Python 版本的 pybind11、dlpack、Sphinx 文档链、PyTorch/CuPy/NumPy（跑测试用）等。镜像把「装环境」这件事从每个开发者的待办里永久删除了。

#### 4.5.2 核心流程

```text
docker/versions.env（所有 Python 包版本的唯一事实来源）
        │ generate_requirements.sh
        ▼
各目录 requirements.txt（生成物，勿手改）
        │ Dockerfile.devel.deps（apt 装系统包 + pip 装 requirements + 源码编译 nvbench）
        ▼
devel 镜像（如 devel_u22.04_cu12.5.0_num1:v9）
        │ docker run -it --gpus all -v $PWD:/workspace <镜像>
        ▼
容器内直接 cmake --preset dev / bash build.sh —— 依赖全部就绪
```

#### 4.5.3 源码精读

**两族镜像的定位**。[docker/README.md:L21-L29](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docker/README.md#L21-L29)：明确分为 Builder 与 Development 两类，且都是 x86_64 + aarch64 多架构 manifest，`docker pull` 自动选对架构。

**builder 镜像矩阵**。[docker/README.md:L35-L40](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docker/README.md#L35-L40)：四个 builder 分别对应 CUDA 12.2.0/12.5.0/13.0.1/13.3.0，全部 gcc-10、manylinux_2_28 底。[docker/README.md:L76-L86](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docker/README.md#L76-L86) 列出 builder 内置：CMake 3.24.3、多版本 Python（3.10–3.14）、**pybind11（为 CMake find_package 而装）**、**dlpack（从源码安装的头文件）**、Sphinx 文档链、patchelf 等 —— 与 4.4 的依赖清单一一对应。

**devel 镜像矩阵**。[docker/README.md:L96-L103](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docker/README.md#L96-L103)：如 `devel_u22.04_cu12.5.0_num1`（Python 3.10、NumPy 1.26、PyTorch 2.9.1）与 `devel_u26.04_py310-314_cu13.3.0_num2`（Python 3.10–3.14、CUDA 13.3）。[docker/README.md:L116-L124](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docker/README.md#L116-L124) 列出关键内容：CMake 3.31.1、ninja-build、多版本 GCC、**Google Test/Mock + pytest**、PyTorch/CuPy、Doxygen/Sphinx、git-lfs 与 pre-commit。

**devel 镜像的安装配方**。[docker/Dockerfile.devel.deps:L123](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docker/Dockerfile.devel.deps#L123) 用 apt 安装 `libgtest-dev`；[L254](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docker/Dockerfile.devel.deps#L254) 附近为每个 Python 版本安装 pybind11（注释明说是 "needed for CMake find_package"）；[L303-L336](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docker/Dockerfile.devel.deps#L303-L336) 从 GitHub clone 固定 commit（[L28](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docker/Dockerfile.devel.deps#L28) 的 `NVBENCH_COMMIT`）的 nvbench，`cmake --install` 到系统路径 —— 因为根 CMakeLists 用 `find_package(nvbench REQUIRED)` 找它，必须装成系统级包。builder 侧同理，[docker/Dockerfile.builder.deps:L108-L114](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docker/Dockerfile.builder.deps#L108-L114) 从源码编译安装 googletest v1.14.0（manylinux 仓库里没有足够新的包）。

**版本管理与使用方式**。[docker/README.md:L126-L137](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docker/README.md#L126-L137)：所有 Python 包版本钉在根目录 `versions.env`，改版本要「编辑 → `generate_requirements.sh` → 一起提交」。[docker/README.md:L169-L186](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docker/README.md#L169-L186) 给出用法：`docker run -it --gpus all -v /path/to/cvcuda:/workspace devel_u22.04_cu12.5.0_num1:v9`，源码挂载进容器即可开发。

#### 4.5.4 代码实践

**实践目标**：用 devel 镜像把「装环境」从构建流程中彻底剥离，完成一次容器内构建。

**操作步骤**：

1. 拉取并运行 devel 镜像，把仓库挂进容器（按 [docker/README.md:L171-L177](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docker/README.md#L171-L177)）：

```bash
docker run -it --gpus all \
  -v $PWD:/workspace \
  devel_u22.04_cu12.5.0_num1:v9
```

2. 容器内执行：

```bash
cd /workspace
cmake --preset dev && cmake --build --preset dev
find build-dev -name 'lib*.so' | sort
```

3. 容器内执行 `docker` 之外的两个验证命令：`cmake --version`（应约 3.31）与 `ls /usr/local/lib | grep -i nvbench`（确认系统级 nvbench 存在）。

**需要观察的现象**：配置阶段没有任何 `find_package` 报错（依赖全就绪）；`find` 输出的库文件路径。

**预期结果**：`build-dev/lib/libnvcv_types.so` 与 `build-dev/lib/libcvcuda.so` 出现在输出里。**待本地验证**（需要装有 Docker 与 NVIDIA Container Toolkit 的机器）。

#### 4.5.5 小练习与答案

**练习 1**：builder 镜像与 devel 镜像的客户分别是谁？

**答案**：builder 镜像面向「发布流程」—— manylinux 底 + 锁死 gcc-10 + 特定 CUDA 版本，产出能在老 glibc 发行版上运行的 wheel/deb 包；devel 镜像面向「开发者」—— Ubuntu 底 + 最新工具链 + 测试/文档/ML 框架全套，用于日常编码、跑测试与基准。

**练习 2**：为什么 nvbench 在 devel 镜像里要用源码编译安装（clone 固定 commit + cmake --install），而不是 pip 安装？

**答案**：因为根 CMakeLists 对 nvbench 的消费方式是 C++ 的 `find_package(nvbench REQUIRED)`，它找的是装在系统 CMake 路径下的 C++ 库与配置文件，pip 管不到这一层。钉死 commit 则保证所有开发者的 nvbench 行为完全一致（基准数字可比较）。

**练习 3**：想给 devel 镜像升级某个 Python 依赖（比如 NumPy），正确流程是什么？

**答案**：编辑根目录 `versions.env`（版本唯一事实来源），运行 `bash generate_requirements.sh` 重新渲染各 requirements.txt，然后把两个文件一起提交。直接手改任何 `AUTO-GENERATED` 的 requirements.txt 都会被仓库规范拒绝（`generate_requirements.sh --check` 会拦截）。

## 5. 综合实践

**任务：完成一次真实构建（或产出一份合格的缺失依赖报告）。**

这是本讲的收官实践，对应学习手册指定的实践任务：

1. **准备**：选择你的路线 ——
   - 路线 A（本机）：Linux + CUDA Toolkit ≥ 12.2 + Ninja + GTest 的机器；
   - 路线 B（容器）：任意装有 Docker 与 NVIDIA Container Toolkit 的机器，用 devel 镜像。
2. **构建**：执行 `cmake --preset dev && cmake --build --preset dev`（路线 A），或按 4.5.4 在容器内执行同样命令（路线 B）。
3. **记录产物**：填写下表（注意：nvcv 层库的实际文件名是 `libnvcv_types.so`，因为 CMake 目标名为 `nvcv_types`）：

| 产物 | 完整路径 | 大小 |
|------|----------|------|
| nvcv 类型层共享库 | `build-dev/lib/libnvcv_types.so` | ？ |
| cvcuda 算子库 | `build-dev/lib/libcvcuda.so` | ？ |
| 测试驱动脚本 | `build-dev/bin/run_tests.sh` | ？ |

4. **若无法构建**：改为执行 4.4.4 的依赖清单实践，产出一份报告，格式为「缺失依赖 → 对应 find_package 名称 → 要求它的文件:行号 → 安装方式（apt/pip/源码）」。
5. **对比思考**：再用 `bash build.sh release build-rel -DBUILD_TESTS=1`（仓库验证梯子中的命令，见 AGENTS.md）构建一次，记录两条路线在构建目录、编译的架构数量、编译耗时上的差异，用一句话解释差异来源（提示：`native` vs 多架构默认策略）。

预期结果：两条路线都能产出 `libnvcv_types.so` 与 `libcvcuda.so`；preset 路线明显更快（单架构 + 无 Python 绑定）。**待本地验证**。

## 6. 本讲小结

- 根 `CMakeLists.txt` 是总装车间：`BUILD_LIB/BUILD_PYTHON/BUILD_TESTS/BUILD_BENCH/...` 开关矩阵决定哪些子树进入构建；`BUILD_TESTS=ON` 会传染性地强制开启三个测试子开关。
- `build.sh` 是包装层：自动挑最新 gcc、启用 Ninja/ccache、以 `CVCUDA_BUILD_JOBS` 控制总并行度、先渲染 requirements 再调用原生 cmake；默认目录 `build-rel`/`build-deb`。
- `CMakePresets.json` 提供四个以 `dev` 为基座的开发预设（dev / dev-debug / dev-py / dev-bench），核心加速手段是 `CMAKE_CUDA_ARCHITECTURES=native` 只编译本机 GPU 架构。
- 依赖解析是「全 find_package、零 FetchContent」：CUDAToolkit（≥12.2）、GTest、pybind11、dlpack、nvbench、nvtx3 头文件都必须预先存在，否则配置直接失败。
- 最终产物落在 `<构建目录>/lib`：`libnvcv_types.so`（注意不是 libnvcv.so）与 `libcvcuda.so`；Debug 构建带 `_d` 后缀。
- Docker devel 镜像 = 把依赖清单一次装好的环境（libgtest-dev、系统级 nvbench、多版本 pybind11、dlpack、PyTorch/CuPy），版本统一由 `versions.env` + `generate_requirements.sh` 管理。

## 7. 下一步学习建议

构建跑通后，你已经具备了「改一行代码 → 重新编译 → 看结果」的闭环能力。建议：

1. **下一讲（u1-l4）「仓库代码地图」**：学习按命名规律定位任意算子的 C API、C++ 类、priv 实现、kernel、Python 绑定、测试与基准文件 —— 这是读源码前的最后一项基本功。
2. **提前浏览** [tests/README.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/README.md) 与 `build-dev/bin/run_tests.sh` 的用法，第 7 单元会深入测试体系。
3. 若你对构建系统本身感兴趣，可通读 `cmake/` 目录下的 `ConfigPython.cmake` 与 `BuildPython.cmake`，看多版本 Python 的 wheel 子构建是如何被编排的（u8-l2 Python 绑定解剖会用到）。
