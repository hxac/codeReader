# 构建与运行 CUTLASS

## 1. 本讲目标

本讲承接上一讲「CUTLASS 项目总览与定位」，把读者从「知道 CUTLASS 是什么」推进到「能在自己机器上把它编译出来、跑起来」。

读完本讲，你应当能够：

- 理解 CUTLASS 作为 header-only 库的「包含即使用」特性，以及它在 CMake 里如何被声明成一个 `INTERFACE` 库。
- 用 CMake 从零构建 CUTLASS，并知道哪些 CMake 变量（如 `CUTLASS_NVCC_ARCHS`、`CUTLASS_ENABLE_TESTS`）会改变构建结果。
- 理解 `CUTLASS_NVCC_ARCHS` 的含义，尤其是 `90a`/`100a` 这类带 `a` 后缀的「架构加速特性」标记为什么不能省。
- 知道 CUTLASS 对 CUDA Toolkit 版本、编译器、C++ 标准的具体要求。
- 编译并运行单元测试目标 `test_unit`，以及一个最小示例 `00_basic_gemm`。

> 本讲不要求你已经写过 CUDA 代码，但需要你装好 NVIDIA 驱动、CUDA Toolkit，以及一个能联网拉取 CMake/Make 的 Linux 环境。本讲里的命令以「在仓库根目录下」为前提。

## 2. 前置知识

在动手之前，先建立 3 个直觉。

**第一，CUTLASS 是 header-only（仅头文件）库。** 这意味着它的「源码」几乎全是 `.h`/`.hpp` 模板文件，没有需要预先编译链接的 `.so`/`.dll`。你只要把 `include/` 目录加进编译器的头文件搜索路径，再 `#include` 相应头文件，编译器就会在你自己的 `.cu`/`.cpp` 里把用到的模板「实例化」出来。这带来一个直接后果：**CUTLASS 本身的编译时间几乎为零，真正的编译耗时发生在「你实例化了哪些内核」上**。这也是后面 `CUTLASS_NVCC_ARCHS` 和 `CUTLASS_LIBRARY_KERNELS` 如此重要的原因。

**第二，CUDA 程序要为「具体的 GPU 架构」编译。** 每一代 NVIDIA GPU 有一个「计算能力」（compute capability），例如 V100 是 7.0、A100 是 8.0、H100 是 9.0、B200 是 10.0。`nvcc` 需要知道你要为哪些架构生成代码（SASS/PTX）。给错架构，要么编译失败，要么运行时内核启动报错。

**第三，架构号有「纯净版」和「加速版」之分。** `90` 和 `90a` 不是一回事：`90a` 表示「允许使用 SM90 上需要架构加速特性（如 Hopper Tensor Core 的 WGMMA 指令）的代码」。如果你的内核用到了这类指令，却用 `90`（不带 `a`）编译，运行时会失败。这一点在 [README.md](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md) 里有明确警告。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [CMakeLists.txt](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt) | 顶层构建脚本，声明 CUTLASS 为 `INTERFACE` 库，定义所有 `CUTLASS_*` 缓存变量、架构支持表、子目录与测试目标 |
| [CUDA.cmake](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CUDA.cmake) | 被 CMakeLists.txt `include` 进来，负责启用 CUDA 语言、查找 CUDA Toolkit、提供 `cutlass_add_executable` 等函数 |
| [README.md](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md) | 官方构建说明，包含 `CUTLASS_NVCC_ARCHS` 用法、`test_unit` 运行示例与各 GPU 的最低 CUDA 版本表 |
| [examples/CMakeLists.txt](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/CMakeLists.txt) | 遍历所有 example 子目录并注册可执行目标的脚本，定义 `cutlass_example_add_executable` |
| [examples/00_basic_gemm/CMakeLists.txt](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/CMakeLists.txt) | 最小 GEMM 示例的构建脚本，仅一行 `cutlass_example_add_executable` |
| [examples/00_basic_gemm/basic_gemm.cu](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu) | 用 `cutlass::gemm::device::Gemm` 跑一个 SGEMM 并自校验的示例源码 |

## 4. 核心概念与源码讲解

### 4.1 header-only 库的包含方式

#### 4.1.1 概念说明

「header-only」是说 CUTLASS 把全部能力写在头文件模板里，使用者只要做到两件事就能用：

1. 让编译器能找到 `include/` 目录（即 `-I<path>/include`）。
2. 在源文件里 `#include` 对应头文件。

CUTLASS 的总入口头文件是 `include/cutlass/cutlass.h`（上一讲已介绍，它定义了贯穿全库的 `cutlass::Status`、warp 常量等）。示例 `00_basic_gemm` 里真正用到的是 GEMM 设备层模板，所以它 include 的是更具体的 [cutlass/gemm/device/gemm.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu#L69)。

由于没有「要先编译出库文件」这一步，CUTLASS 在 CMake 里被声明成一个 **`INTERFACE` 库**——一种特殊的 CMake 目标，它本身不产生任何产物，只用来「携带」编译选项、头文件路径等接口信息，供链接它的可执行目标继承。

#### 4.1.2 核心流程

`INTERFACE` 库的工作流程：

1. `add_library(CUTLASS INTERFACE)` 声明一个不产物的目标。
2. `target_include_directories(CUTLASS INTERFACE ...)` 把 `include/` 路径挂在它的「接口」上。
3. 任何用 `target_link_libraries(my_app PRIVATE CUTLASS)` 链接它的目标，都会自动继承这些头文件路径与编译宏。
4. 当 `my_app` 被编译时，它 include 的 CUTLASS 模板在此时被实例化——CUTLASS 的「编译」其实发生在这一刻。

#### 4.1.3 源码精读

顶层 CMakeLists.txt 里，CUTLASS 库目标就这样被创建（无任何源文件参数，说明是纯接口）：

[CMakeLists.txt:731-733](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L731-L733) 创建名为 `CUTLASS` 的 `INTERFACE` 库，并起别名 `nvidia::cutlass::cutlass`，便于被外部项目以命名空间方式引用。

紧接着，头文件目录被挂到接口上（`$<BUILD_INTERFACE:...>` 表示仅在构建本仓库时生效，`$<INSTALL_INTERFACE:include>` 表示安装后被下游使用时生效）：

[CMakeLists.txt:755-761](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L755-L761) 把 `include/` 与构建期生成的 `include/cutlass/version_extended.h` 目录加入 CUTLASS 的接口包含路径。

示例侧只需要一行就能「链接 + 自动拿到头文件路径」。[examples/CMakeLists.txt](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/CMakeLists.txt#L50-L57) 里的 `cutlass_example_add_executable` 函数对每个示例目标都执行了 `target_link_libraries(${NAME} PRIVATE CUTLASS ...)`，所以示例源码里可以直接写 `#include "cutlass/gemm/device/gemm.h"` 而无需关心路径。

> 小知识：`target_compile_features(CUTLASS INTERFACE cxx_std_11)` 在 [CMakeLists.txt:744](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L744) 声明库接口「至少需要 C++11」——这是对「仅头文件、最小依赖」使用者而言的最低线；但 CUTLASS 3.x 的 GEMM/CuTe 代码本身要求 C++17，这一点会在 4.2 节看到。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：确认「CUTLASS 没有预编译库产物，能力全在头文件里」。

**操作步骤**：

1. 在仓库根目录执行只读命令 `git ls-files include/cutlass | head` 与 `git ls-files '*.cu' include | head`，观察 `include/` 下几乎全是 `.h`/`.hpp`/`.inl`。
2. 打开 `examples/00_basic_gemm/CMakeLists.txt`，确认它只有一行 `cutlass_example_add_executable(00_basic_gemm basic_gemm.cu)`，没有任何「链接到 libcutlass.so」的语句。

**需要观察的现象**：`include/cutlass` 下找不到任何 `.so`/`.a`/`.o`；示例构建脚本里只出现源文件名，没有预编译库。

**预期结果**：验证 CUTLASS 是 header-only，编译产物只来自你自己实例化的内核。

#### 4.1.5 小练习与答案

**练习 1**：如果把 CUTLASS 当作子目录被另一个顶层项目 `add_subdirectory()` 进来，`CUTLASS` 这个 INTERFACE 目标会出现在那个项目里吗？示例还能正常编译吗？

> **参考答案**：会。`add_library(CUTLASS INTERFACE)` 创建的是普通目标，进入上层项目的目标命名空间；示例通过 `target_link_libraries(... PRIVATE CUTLASS)` 继承其接口包含路径，因此仍能编译。不过顶层 CMakeLists.txt 里有专门判断：当 CUTLASS 不是顶层项目时（`CMAKE_PROJECT_NAME` 不等于 `PROJECT_NAME`），默认不开启单元测试（见 4.4 节），避免污染宿主项目。

**练习 2**：为什么 header-only 库仍然需要 `target_include_directories`？

> **参考答案**：因为编译器要能找到 `#include "cutlass/..."` 里的头文件。`INTERFACE` 库不产生二进制，但它「携带」的头文件搜索路径会通过 `target_link_libraries` 传递给使用者，省去你手动写 `-I`。

---

### 4.2 CMake 构建流程与版本/C++17 要求

#### 4.2.1 概念说明

CUTLASS 用 CMake 管理构建。构建分三步：① 配置（`cmake ..`，CMake 读取 `CMakeLists.txt`、检测工具链、生成 Makefile）；② 编译（`make`）；③ 运行。配置阶段会检测 CUDA Toolkit 版本、编译器版本、C++ 标准，并把一堆 `CUTLASS_*` 选项写入缓存（可被命令行 `-D` 覆盖）。

#### 4.2.2 核心流程

```
export CUDACXX=<toolkit>/bin/nvcc          # 让 CMake 找到正确的 nvcc
mkdir build && cd build
cmake .. -DCUTLASS_NVCC_ARCHS=80           # 配置：生成 Makefile
make test_unit -j                           # 编译：构建单元测试目标
./test/unit/cutlass/test_unit               # 运行（路径见 4.4）
```

配置阶段，CMakeLists.txt 依次做这些事：

1. 设定 CMake 最低版本与项目版本（版本号从 `include/cutlass/version.h` 解析）。
2. `include(CUDA.cmake)`：启用 CUDA 语言、`find_package(CUDAToolkit)`。
3. 检测 CUDA 版本 / 编译器版本是否达标，否则告警或致命错误。
4. 强制 C++17 / CUDA 17 标准。
5. 计算当前 CUDA 版本下「受支持的架构列表」并写入 `CUTLASS_NVCC_ARCHS`。
6. 创建 `CUTLASS` INTERFACE 库，`add_subdirectory` 进入 `tools/`、`examples/`、`test/`。

#### 4.2.3 源码精读

**最低 CMake 版本**：[CMakeLists.txt:29-30](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L29-L30) 要求 CMake ≥ 3.19。注意 `project(...)` 只声明了 `LANGUAGES CXX`——CUDA 语言是稍后由 [CUDA.cmake:40](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CUDA.cmake#L40) 的 `enable_language(CUDA)` 启用的，这是一种常见做法，便于让 `CUDACXX` 环境变量决定使用哪个 nvcc。

**版本号解析**：CUTLASS 不在多个地方重复写版本号，而是直接从 `version.h` 里抓取，避免不一致：

[CMakeLists.txt:46-54](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L46-L54) 读取 `include/cutlass/version.h`，用正则解析 `CUTLASS_MAJOR/MINOR/PATCH` 三个宏，再据此设定 `project()` 版本。

**CUDA 版本门槛**：[CMakeLists.txt:86-90](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L86-L90) 规定：CUDA < 11.3 时打印「需要 11.4+，推荐 11.8+」的警告；11.3 ≤ CUDA < 11.4 时提示「已弃用」。结合 README 的说法，CUTLASS 3.x 的最低要求是 CUDA 11.4，性能最佳的是 12.8。

**编译器门槛**：[CMakeLists.txt:92-94](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L92-L94) 要求 GCC ≥ 7.3（否则致命错误）。

**C++17 强制**：CUTLASS 3.x 的 CuTe 与 GEMM 模板大量使用 C++17 特性，因此全局强制标准：

[CMakeLists.txt:104-109](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L104-L109) 同时把主机 C++ 与 CUDA 设为 C++17 且 `STANDARD_REQUIRED ON`。这就是为什么即便 `CUTLASS` INTERFACE 目标只声明了 `cxx_std_11`（4.1 节），实际构建仍以 C++17 进行——构建标准由顶层强制设定，覆盖了库接口的最低线。

随后挂上两个对 CUTLASS 模板很关键的 nvcc 选项：[CMakeLists.txt:111-113](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L111-L113) 启用 `--expt-relaxed-constexpr`（让 `__device__` 常量表达式更宽松）与 `-ftemplate-backtrace-limit=0`（出错时打印完整模板实例化链，对调试 CUTLASS 深层模板必不可少）。

**默认 Release 构建**：[CMakeLists.txt:242-246](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L242-L246) 在未指定 `CMAKE_BUILD_TYPE` 时强制 `Release`，以保证拿到最佳性能（去掉调试符号、开启优化）。

#### 4.2.4 代码实践

**实践目标**：走一遍「配置 → 看关键变量」的过程，确认你的工具链达标。

**操作步骤**：

```bash
export CUDACXX=/usr/local/cuda/bin/nvcc      # 按你的实际 toolkit 路径
mkdir build && cd build
cmake .. -DCUTLASS_NVCC_ARCHS=80
```

观察配置阶段的终端输出。

**需要观察的现象**：CMake 打印的几行 `STATUS`，至少包括：
- `CUTLASS <major>.<minor>.<patch>`（版本号，来自 version.h）；
- `CUDA Compilation Architectures: 80`（你指定的架构）；
- 一段 `Using the following NVCC flags:`（含 `--expt-relaxed-constexpr` 等）。

**预期结果**：配置成功生成 `build/Makefile`，且没有任何 `FATAL_ERROR`。若 CUDA < 11.4，会出现告警但仍可能继续（取决于具体版本）。

> 若你当前环境没有 GPU/nvcc，无法实际执行，请标注「待本地验证」，但你可以先通读 CMakeLists.txt 的 L86–L113，对照上面的现象列表。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `project()` 不直接写 `LANGUAGES CXX CUDA`，而要把 CUDA 放到 `CUDA.cmake` 里 `enable_language(CUDA)`？

> **参考答案**：把 CUDA 启用推迟到 `enable_language(CUDA)` 之前，CMake 会优先尊重 `CUDACXX` 环境变量来决定使用哪个 nvcc；如果在 `project()` 里就声明 CUDA，CMake 可能在你设好 `CUDACXX` 之前就选定了编译器，导致用错 toolkit。

**练习 2**：把 `CMAKE_BUILD_TYPE` 显式设为 `Debug` 重新配置，[CMakeLists.txt:552-555](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L552-L555) 会额外加什么标志？为什么？

> **参考答案**：非 Release 模式下会加上 `-lineinfo`（nvcc）和 `-gmlt`（clang）。`-lineinfo` 保留源码行号信息，便于用 `cuobjdump`/Nsight Compute 做性能剖析，但不含完整调试符号，是「剖析友好且体积可控」的折中。

---

### 4.3 CMake 与 CUTLASS_NVCC_ARCHS

#### 4.3.1 概念说明

`CUTLASS_NVCC_ARCHS` 是本讲最重要的变量。它告诉 CUTLASS「为哪些 SM 架构生成 GPU 代码」。它的取值是一组数字（可带后缀），如 `80`、`'75;80'`、`90a`、`100a`。架构清单不是随便写的，顶层 CMakeLists.txt 会根据**你当前的 CUDA Toolkit 版本**动态计算「受支持的架构集合」，你不应填入 toolkit 不认识的架构。

带 `a` 后缀（如 `90a`/`100a`）表示启用该架构的「架构加速特性」（architecture-accelerated features），即允许生成使用 Tensor Core 等专用指令的代码。这对 CUTLASS 的 GEMM 性能是决定性的。

#### 4.3.2 核心流程

受支持架构的确定过程（在配置阶段一次性算好）：

```
读取 CUDA_VERSION
  ├─ ≥ 11.4        → 追加 75 80 86 87
  ├─ ≥ 11.4 且 < 13.0 → 追加 70 72
  ├─ ≥ 11.8        → 追加 89 90
  ├─ ≥ 12.0        → 追加 90a
  ├─ ≥ 12.8        → 追加 100 100a 120 120a 121 121a（及 101/110 等视版本）
  └─ ≥ 12.9 / 13.0 → 追加更多 10x/110/带 f 后缀的变体
把「受支持集合」设为 CUTLASS_NVCC_ARCHS 的默认值
用户可用 -DCUTLASS_NVCC_ARCHS=... 覆盖
```

之后，函数 `cutlass_apply_cuda_gencode_flags` 把每个架构翻译成 CMake 的 `CUDA_ARCHITECTURES` 目标属性（`<arch>-real` 生成 SASS，`<arch>-virtual` 生成 PTX），从而驱动 nvcc 的 `-gencode`。

> 一个常被忽略的点：填了 toolkit 不支持的架构时，CUTLASS 不会致命错误，而是给出 `WARNING` 提示「使用了不支持的架构，未来版本可能移除支持」。

#### 4.3.3 源码精读

架构支持表的核心就是下面这段条件追加（节选关键分支）：

[CMakeLists.txt:174-195](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L174-L195) 按 CUDA 版本逐档把架构加入 `CUTLASS_NVCC_ARCHS_SUPPORTED`。注意 `90a` 只有在 CUDA ≥ 12.0 时才出现（[L184-L186](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L184-L186)），而 Blackwell 的 `100/100a/120/120a/121/121a` 需要 CUDA ≥ 12.8（[L188-L195](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L188-L195)）。

然后把「受支持集合」作为默认值写入缓存变量：

[CMakeLists.txt:210-211](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L210-L211) `CUTLASS_NVCC_ARCHS` 是用户可覆盖的输入变量；`CUTLASS_NVCC_ARCHS_ENABLED` 是实际参与构建的架构集合（二者默认相同，便于在某些场景解耦）。

[CMakeLists.txt:214-222](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L214-L222) 检查用户填写的架构是否落在受支持集合内；若不在，打印 `WARNING`（注意不是 `FATAL_ERROR`，所以填错也能继续，但运行时很可能失败）。

把架构号真正翻译成 nvcc 代码生成选项的是这个函数：

[CMakeLists.txt:639-667](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L639-L667) `cutlass_apply_cuda_gencode_flags` 遍历每个架构，依据 `CUTLASS_NVCC_EMBED_CUBIN`/`CUTLASS_NVCC_EMBED_PTX` 生成 `<arch>-real`（cubin）与 `<arch>-virtual`（PTX），最后写入目标的 `CUDA_ARCHITECTURES` 属性。

README 里对 `a` 后缀有专门警告（强调 Hopper 必须用 `90a` 才能拿到 WGMMA）：

[README.md:186-201](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L186-L201) 给出 `cmake .. -DCUTLASS_NVCC_ARCHS="90a"` 与 `="100a"` 两个推荐写法。

#### 4.3.4 代码实践

**实践目标**：亲手改变目标架构，并确认它影响了构建配置。

**操作步骤**：

```bash
cd build
cmake .. -DCUTLASS_NVCC_ARCHS=80          # 命令 A：只编译 Ampere
cmake .. -DCUTLASS_NVCC_ARCHS="90a"        # 命令 B：改为 Hopper 加速
```

每次配置后，查看输出里的 `CUDA Compilation Architectures:` 那一行。

**需要观察的现象**：命令 A 显示 `80`，命令 B 显示 `90a`；随后编译同一示例时，命令 B 会编译出使用 WGMMA 的内核（编译时间通常更长）。

**预期结果**：架构字符串随 `-DCUTLASS_NVCC_ARCHS` 改变而改变，验证了「CUTLASS 为你指定的架构生成代码」。若你填了 toolkit 不支持的值（例如 CUDA 11.4 下填 `90a`），会出现 `WARNING: Using unsupported or deprecated compute capabilities ...`。

> 待本地验证：实际编译耗时与产物大小取决于你的硬件与 toolkit。

#### 4.3.5 小练习与答案

**练习 1**：在 CUDA 11.4 上配置 `-DCUTLASS_NVCC_ARCHS=90a` 会发生什么？为什么？

> **参考答案**：会出现「使用了不支持的架构」的 `WARNING`。因为 [CMakeLists.txt:184-186](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L184-L186) 把 `90a` 列入受支持集合的前提是 CUDA ≥ 12.0；11.4 时 `90a` 不在 `CUTLASS_NVCC_ARCHS_SUPPORTED` 里，于是落入 L214–L222 的告警分支。更根本的原因是 `90a` 对应的 WGMMA PTX 需要 12.0+ 的 nvcc 才能生成。

**练习 2**：`CUTLASS_NVCC_ARCHS` 与 nvcc 原生的 `-arch=sm_80` 是什么关系？

> **参考答案**：`CUTLASS_NVCC_ARCHS` 是 CUTLASS 的 CMake 抽象，由 `cutlass_apply_cuda_gencode_flags` 翻译成 CMake 的 `CUDA_ARCHITECTURES` 目标属性，CMake 再据此生成等价于 `-gencode arch=compute_80,code=sm_80`（及 PTX）的 nvcc 命令行。所以它是「更上层、跨编译器（nvcc/clang-cuda）友好」的封装。

---

### 4.4 构建并运行 test_unit 与示例

#### 4.4.1 概念说明

CUTLASS 提供两类「能跑给你看」的产物：

- **单元测试 `test_unit`**：一组基于 GoogleTest 的二进制，覆盖 CUTLASS 各命名空间的核心功能。README 用它作为「环境是否正常」的标准体检。
- **示例 `examples/`**：每个子目录是一个独立可执行程序，演示一类用法；`00_basic_gemm` 是其中最简单的——用 `cutlass::gemm::device::Gemm` 跑一次 SGEMM 并与朴素参考实现逐位比对。

这些是否参与构建，由一组 `CUTLASS_ENABLE_*` 缓存变量控制。

#### 4.4.2 核心流程

构建开关的确定逻辑（顶层 CMakeLists.txt）：

```
若 CUTLASS_ENABLE_HEADERS_ONLY：
    examples = OFF, tools = ON, library = OFF, tests = OFF
否则：
    examples = ON, tools = ON, library = ON
    tests = ON 仅当 CUTLASS 是顶层项目（CMAKE_PROJECT_NAME == PROJECT_NAME）
```

随后：

```
add_subdirectory(tools)      → cutlass_profiler 等
add_subdirectory(examples)   → 遍历每个 example 子目录
add_subdirectory(test)       → test_unit 等
test_all 聚合 test_examples / test_unit / test_profiler
```

示例本身注册极其简洁：`examples/CMakeLists.txt` 用一个 `foreach` 遍历所有示例名（含 `00_basic_gemm`），对每个调用 `cutlass_example_add_executable`，再 `add_subdirectory` 进入其目录。

#### 4.4.3 源码精读

**构建开关的确定**：[CMakeLists.txt:134-150](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L134-L150) 定义 `CUTLASS_ENABLE_HEADERS_ONLY` 及其对 examples/tools/library/tests 初始值的影响。关键点在 L145–L149：只有当 CUTLASS 是顶层项目时，测试默认才开启——这就是「作为子项目被引入时不自动构建一堆测试」的机制。

**对外暴露的开关**：[CMakeLists.txt:154-161](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L154-L161) 把 examples/tools/library/tests（及基于 GTest 的单元测试）正式声明为可被命令行覆盖的缓存变量，如 `-DCUTLASS_ENABLE_TESTS=OFF`。

**子目录与测试聚合**：[CMakeLists.txt:1136-1161](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L1136-L1161) 依次 `add_subdirectory(tools/examples/test)`，并把 `test_examples`/`test_unit` 挂到聚合目标 `test_all` 上。`test_all` 在 [CMakeLists.txt:819-821](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L819-L821) 创建，并配合 [CMakeLists.txt:808-809](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L808-L809) 的 `include(CTest)`/`enable_testing()` 启用 `ctest`。

**示例注册**：[examples/CMakeLists.txt:83-183](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/CMakeLists.txt#L83-L183) 用 `foreach` 遍历所有示例（`00_basic_gemm` 在 [L84](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/CMakeLists.txt#L84)），逐个 `add_subdirectory`。函数 `cutlass_example_add_executable`（[L35-L80](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/CMakeLists.txt#L35-L80)）封装了「链接 CUTLASS、链接 cuda、注册到 test_examples」等通用步骤。

**00_basic_gemm 的构建**：[examples/00_basic_gemm/CMakeLists.txt:31-34](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/CMakeLists.txt#L31-L34) 只有一行——`cutlass_example_add_executable(00_basic_gemm basic_gemm.cu)`，目标名就叫 `00_basic_gemm`，源文件是 `basic_gemm.cu`。

**00_basic_gemm 的运行入口**：[examples/00_basic_gemm/basic_gemm.cu:451-494](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu#L451-L494) 是 `main`：默认问题规模 `128x128x128`、`alpha=1`、`beta=0`，也支持命令行 `00_basic_gemm <M> <N> <K> <alpha> <beta>`；成功时打印 `Passed.`（[L489-L491](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu#L489-L491)）。它内部用 `cutlass::gemm::device::Gemm` 实例化一个列主序 SGEMM（[L103-L108](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu#L103-L108)），构造 `Arguments`（[L122-L127](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu#L122-L127)），再 `gemm_operator(args)` 启动内核（[L133](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/00_basic_gemm/basic_gemm.cu#L133)）。这些细节属于后续讲义（u1-l6），本讲只需知道它「编译完能跑、会自校验」即可。

**README 的官方流程**：[README.md:260-287](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L260-L287) 给出完整的 `mkdir build` → `cmake .. -DCUTLASS_NVCC_ARCHS=80` → `make test_unit -j` 流程，并展示典型输出 `[  PASSED  ] 946 tests.`（具体用例数随版本变化）。

#### 4.4.4 代码实践（本讲核心实践）

**实践目标**：从零配置、编译并运行 `00_basic_gemm`，再尝试 `test_unit`。

**操作步骤**：

```bash
# 0. 指向正确的 nvcc（按你的实际路径）
export CUDACXX=/usr/local/cuda/bin/nvcc

# 1. 在仓库根目录创建并进入 build 目录
mkdir build && cd build

# 2. 配置：只编译 Ampere (SM80)
cmake .. -DCUTLASS_NVCC_ARCHS=80

# 3. 只编译最小示例（比编译整个 test_unit 快得多）
make 00_basic_gemm -j

# 4. 运行（二进制位于 build/examples/00_basic_gemm/ 下）
./examples/00_basic_gemm/00_basic_gemm

# 5. （可选）编译并运行单元测试体检
make test_unit -j
ctest -R cutlass_test_unit_gemmsm80    # 按需过滤
```

**需要观察的现象**：

- 步骤 3：`make` 成功生成可执行文件 `examples/00_basic_gemm/00_basic_gemm`。
- 步骤 4：终端打印一行 `Passed.`，退出码为 0。若改用 `./examples/00_basic_gemm/00_basic_gemm 256 256 256 1 0`，应同样打印 `Passed.`（自校验通过）。
- 步骤 5：`make test_unit` 耗时显著长于示例；运行后输出形如 `[==========] N tests ... [  PASSED  ] N tests.`。

**预期结果**：示例打印 `Passed.`，证明你的工具链、架构设置、CUDA runtime 全部正常；`test_unit` 全绿即代表环境完整可用。

> 待本地验证：本实践需要真实 GPU 与 CUDA Toolkit。当前代码阅读环境无 GPU，无法替你运行；若你机器上 `make 00_basic_gemm` 报「架构不支持」，请回到 4.3 节核对 `CUTLASS_NVCC_ARCHS` 是否与你的 GPU 计算能力一致（用 `nvidia-smi` 或 `deviceQuery` 查看）。

> 小贴士：若只想快速验证「能编译」，可加 `-DCUTLASS_ENABLE_TESTS=OFF -DCUTLASS_ENABLE_LIBRARY=OFF`，显著缩短配置与编译时间。

#### 4.4.5 小练习与答案

**练习 1**：把 CUTLASS 作为子项目 `add_subdirectory()` 引入另一个顶层工程后，为什么默认没有 `test_unit`？

> **参考答案**：因为 [CMakeLists.txt:145-149](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt#L145-L149) 判断 `CMAKE_PROJECT_NAME` 是否等于 `PROJECT_NAME`：作为子项目时两者不同，于是 `CUTLASS_ENABLE_TESTS_INIT` 被设为 `OFF`，`test/` 不会被 `add_subdirectory`。这是为了避免在被依赖时拖慢宿主项目的构建。如确需测试，可显式 `-DCUTLASS_ENABLE_TESTS=ON`。

**练习 2**：`make 00_basic_gemm` 与 `make test_examples_00_basic_gemm` 有何区别？

> **参考答案**：前者只构建可执行目标 `00_basic_gemm`；后者在 `cutlass_example_add_executable` 里通过 `cutlass_add_executable_tests` 额外注册了一个「构建并运行该示例」的测试目标（见 [examples/CMakeLists.txt:71-78](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/CMakeLists.txt#L71-L78)），它会被聚合进 `test_examples`/`test_all`，可通过 `make test_all` 或 `ctest` 一次性跑全部示例。

---

## 5. 综合实践

把本讲的 4 个模块串起来，完成一次「最小化、可控」的 CUTLASS 构建实验。

**任务**：在 `build/` 目录里，用**两种不同的 `CUTLASS_NVCC_ARCHS`** 各配置一次，对比它们的「配置阶段输出」与「编译产物」，并跑通 `00_basic_gemm`。

**建议步骤**：

1. 准备两个独立构建目录以避免互相污染：
   ```bash
   mkdir build_ampere build_hopper
   ```
2. 在 `build_ampere` 里：`cmake .. -DCUTLASS_NVCC_ARCHS=80 -DCUTLASS_ENABLE_TESTS=OFF -DCUTLASS_ENABLE_LIBRARY=OFF`，记录输出里的 `CUDA Compilation Architectures:` 行。
3. 在 `build_hopper` 里（需 CUDA ≥ 12.0 与 Hopper GPU）：`cmake .. -DCUTLASS_NVCC_ARCHS="90a" -DCUTLASS_ENABLE_TESTS=OFF -DCUTLASS_ENABLE_LIBRARY=OFF`，同样记录该行，并留意是否有额外 `-DCUTE_SM90_*` 之类的定义被启用。
4. 两个目录都执行 `make 00_basic_gemm -j` 并运行，确认都打印 `Passed.`。
5. 用 `du -sh build_ampere build_hopper` 对比两个构建目录的体积，思考「为什么架构越多、加速特性越多，构建就越慢越大」（提示：每种架构 + 每种加速特性都要单独实例化内核代码）。

**验收标准**：你能向别人解释「`CUTLASS_NVCC_ARCHS` 改变的是 nvcc 为哪些架构生成代码」，并能独立判断自己的 GPU 该填哪个值（带不带 `a`）。

> 待本地验证：体积与耗时数据取决于硬件；本实践在无 GPU 环境下只能完成到「阅读配置输出」这一步。

## 6. 本讲小结

- CUTLASS 是 **header-only** 库，在 CMake 里被声明为 `INTERFACE` 目标（`add_library(CUTLASS INTERFACE)`），能力全在 `include/` 头文件模板里，编译发生在「使用者实例化内核」时。
- 顶层 [CMakeLists.txt](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CMakeLists.txt) 要求 CMake ≥ 3.19、CUDA ≥ 11.4（推荐 12.8）、GCC ≥ 7.3，并全局强制 **C++17/CUDA 17** 与 `Release` 默认构建。
- `CUTLASS_NVCC_ARCHS` 决定为哪些 SM 架构生成代码；**带 `a` 后缀（`90a`/`100a`）才允许使用 Tensor Core 等加速指令**，填错会运行时失败。受支持架构随 CUDA 版本动态计算。
- 构建/测试是否参与由 `CUTLASS_ENABLE_HEADERS_ONLY`、`CUTLASS_ENABLE_TESTS` 等开关控制；**作为子项目引入时单元测试默认关闭**。
- 标准流程是 `mkdir build && cd build && cmake .. -DCUTLASS_NVCC_ARCHS=<sm> && make <target> -j`；`make test_unit` 是环境体检，`make 00_basic_gemm` 是最小冒烟测试（成功打印 `Passed.`）。
- 想缩短构建时间，可用 `-DCUTLASS_ENABLE_TESTS=OFF -DCUTLASS_ENABLE_LIBRARY=OFF` 关掉非必需子项目，并只指定一个目标架构。

## 7. 下一步学习建议

本讲让你能「编译并运行」。接下来建议：

- **u1-l3 目录结构与源码组织**：在你会构建之后，系统了解 `include/cutlass` 与 `include/cute` 两套核心库的职责划分，知道「要改某个功能该去哪个目录」。
- **u1-l4 数值类型与基础容器**、**u1-l5 矩阵布局基础**：这两讲为理解 GEMM 源码做铺垫——你已经在本讲看到 `00_basic_gemm` 用到了 `cutlass::layout::ColumnMajor`。
- **u1-l6 第一个 GEMM：2.x device API**：精读本讲只是「跑起来」的 `basic_gemm.cu`，搞懂 `cutlass::gemm::device::Gemm` 的模板参数与 `Arguments` 对象到底意味着什么。
- 进阶后，可阅读官方 [Quickstart](https://docs.nvidia.com/cutlass/latest/media/docs/cpp/quickstart.html)（README 多次引用）获取更多选择性编译内核的 CMake 示例（与 u3-l8 Profiler 讲义呼应）。
