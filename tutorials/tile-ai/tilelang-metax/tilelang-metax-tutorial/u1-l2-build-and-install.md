# 环境搭建与编译安装（含 MACA）

## 1. 本讲目标

学完本讲，你应当能够：

- 在一台 Linux 机器上，从源码把 tilelang-metax 编译出可导入的 Python 包（`import tilelang`）。
- 说清楚「通用构建」与「启用 MACA 后端」两条路径各自需要哪些依赖、哪些命令、哪些环境变量。
- 读懂仓库里 `CMakeLists.txt`、`src/maca/CMakeLists.txt`、`cmake/FindMACA.cmake`、`pyproject.toml` 这几个构建关键文件，知道它们各自负责什么。
- 理解一个容易被忽视的细节：**MACA 后端在 CMake 里和 CUDA/ROCm 的接线方式并不完全相同**，它是通过 `USE_MACA` 这个**环境变量**而非标准 `-DUSE_XXX` 选项触发的。
- 写出一条验证安装成功的命令，并知道失败时该去哪里排查。

本讲承接 [u1-l1 项目概览](u1-l1-project-overview.md)：上一讲我们建立了「TileLang 是一个编译器、tilelang-metax 是新增了 MACA 后端的分支」这个全局认知；本讲回答「这个编译器本身怎么在你机器上被编译出来」。

---

## 2. 前置知识

在动手之前，先用大白话建立几个直觉。

**1. 为什么从源码编译？**

TileLang 不是纯 Python 库。它的核心是一个用 C++ 写的「编译器后端」（位于 `src/`），负责把你写的 Tile Program 下译成 CUDA / HIP / **MACA** 等后端的源码，再进一步变成可执行文件。因此安装它，本质上是要先把这个 C++ 编译器本身编译出来，产出动态库（Linux 上是 `libtilelang.so`），再让 Python 前端去加载它。这跟 `pip install numpy` 这种「装个纯 Python 包」是两回事。

**2. 什么是 CMake / pyproject.toml？**

- **CMake** 是 C/C++ 世界的「构建总指挥」。`CMakeLists.txt` 是它的脚本，里面声明「要编译哪些 `.cc` 文件、依赖哪些库、开启哪些功能开关（如 `USE_CUDA`、`USE_MACA`）」。CMake 跑完后会生成 `Makefile` 或 `ninja` 文件，再用 `make` / `ninja` 真正去编译。
- **pyproject.toml** 是 Python 打包的现代标准。它声明「这个 Python 包的依赖、版本、怎么构建 wheel」。在 tilelang-metax 里，它通过 `scikit-build-core` 把「Python 打包」和「CMake 编译 C++」这两步缝在一起。

**3. 什么是 MACA / cu-bridge / tvm-ffi？**

- **MACA**：MetaX（摩尔线程）GPU 的软件栈，类比 NVIDIA 的 CUDA。`mxcc` 是 MACA 的编译器（类比 `nvcc`）。要让 tilelang-metax 给 MetaX GPU 生成代码，就必须装好 MACA SDK。
- **cu-bridge**：MetaX 提供的兼容层，让许多原本写死 `cuda` 的程序（比如 PyTorch）能在 MACA 上跑起来。本讲的 `device="cuda"` 之所以在某些 MACA 环境下也能工作，靠的就是它。
- **tvm-ffi**：TVM 的跨语言函数调用桥（Foreign Function Interface）。TileLang 依赖一个定制版 TVM，而 TVM 又依赖 tvm-ffi，所以构建时需要单独把 tvm-ffi 这个 Python 包装上。

**4. 三类后端开关的统一名字**

TileLang 支持多个硬件后端，构建时用一组统一命名的开关控制是否编译某后端：

| 开关 | 对应后端 | 备注 |
|------|----------|------|
| `USE_CUDA` | NVIDIA CUDA | 默认在 Linux 上若检测到 CUDA 工具链则开启 |
| `USE_ROCM` | AMD ROCm | 默认 OFF |
| `USE_METAL` | Apple Metal | 默认在 macOS 上开启 |
| `USE_LLVM` | CPU（LLVM 后端） | 默认 OFF |
| `USE_MACA` | **MetaX MACA**（本 fork 新增） | 通过环境变量触发 |

> 提示：MACA 是本分支相对上游新增的后端，它的接线方式和前四个略有不同，这是本讲 4.3 节的重点。

---

## 3. 本讲源码地图

本讲涉及的构建关键文件如下：

| 文件 | 作用 |
|------|------|
| [docs/get_started/Installation.md](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/Installation.md) | **通用安装文档**：pip 安装、从源码构建（CUDA/ROCm）、Docker、构建期/运行期环境变量总览。面向 NVIDIA/AMD 用户。 |
| [docs/get_started/Installation_maca.md](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/Installation_maca.md) | **MACA 专用安装文档**：本分支新增，讲解 MetaX GPU 上从装 MACA SDK 到 `USE_MACA=ON` 构建再到验证的完整流程。本讲主线。 |
| [CMakeLists.txt](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt) | **顶层构建脚本**：声明后端开关、自动选择后端、引入 TVM 子模块、收集源码、产出 `libtilelang.so`。 |
| [src/maca/CMakeLists.txt](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/CMakeLists.txt) | **MACA 后端子构建脚本**：决定哪些 MACA 源码参与编译、在 `USE_MACA` 开启时调用 `find_maca` 定位 SDK。 |
| [cmake/FindMACA.cmake](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/cmake/FindMACA.cmake) | **MACA SDK 探测宏**：按「显式路径 → `MACA_PATH` → `/opt/maca`」顺序找到 SDK，并定位 `mcruntime`、`mxc-runtime64` 两个库。 |
| [pyproject.toml](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/pyproject.toml) | **Python 打包配置**：声明运行期依赖（`apache-tvm-ffi`、`z3-solver`、`torch` 等）、构建后端（`scikit-build-core`）与 wheel 内容映射。 |
| [tilelang/env.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py) | **运行期环境变量与路径检测**：在 `import tilelang` 时自动定位 `CUDA_HOME`/`ROCM_HOME`/`MACA_HOME`，配置缓存目录等。 |

---

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：**构建依赖 → CMake 构建 → MACA SDK → 安装验证**。

### 4.1 构建依赖

#### 4.1.1 概念说明

「从源码编译」之前，必须先把「编译这个编译器」所需要的原料备齐。tilelang-metax 的构建依赖可以分成三层：

1. **OS / 系统级依赖**：编译器（gcc）、CMake、Python 开发头文件等。这些是任何 C++ 项目都需要的。
2. **Python 包依赖**：构建期（cython、scikit-build-core）和运行期（apache-tvm-ffi、z3-solver、torch）的 Python 包。
3. **硬件 SDK 依赖**：取决于你要启用哪个后端——CUDA 工具链、ROCm、或 **MACA SDK**。本讲聚焦 MACA。

为什么需要 z3-solver？因为 TileLang 的部分优化 pass（编译流水线里的一环）用到了 Z3 这个 SMT 求解器。这一点在 `CMakeLists.txt` 里有体现（后文 4.2 会看到 `USE_Z3`/`USE_PYPI_Z3`）。

为什么需要 cython？因为 Python 前端与 C++ 后端之间有一层用 Cython 写的高性能粘合层（`tilelang_cython_wrapper`）。

#### 4.1.2 核心流程

依赖准备的判断流程可以用下面这段伪代码概括：

```
读 Installation.md / Installation_maca.md 的 Prerequisites
  ├─ OS 级：apt-get install python3-dev gcc cmake build-essential ...
  ├─ Python 级：pip install apache-tvm-ffi z3-solver torch cython ...
  └─ SDK 级：
        ├─ 目标是 NVIDIA → 装 CUDA Toolkit
        ├─ 目标是 AMD    → 装 ROCm
        └─ 目标是 MetaX  → 装 MACA SDK + cu-bridge（见 4.3）
```

一个常被忽视的前提：**构建 tilelang-metax 需要一个定制版的 TVM**，它作为 git 子模块（`3rdparty/tvm`）随仓库一起拉取。这也是为什么文档里克隆仓库时要带 `--recursive`。

#### 4.1.3 源码精读

**MACA 文档列出的系统前提条件**：

参见 [Installation_maca.md:9-22](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/Installation_maca.md#L9-L22)。这段明确写出：操作系统必须是 Linux、Python >= 3.10、MACA 版本 >= 3.3.0，并给出 Ubuntu 上的 apt 安装命令：

```bash
apt-get install -y python3 python3-dev python3-setuptools gcc zlib1g-dev \
  build-essential cmake libedit-dev git
```

**Python 运行期依赖**（来自打包配置）：

参见 [pyproject.toml:29-45](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/pyproject.toml#L29-L45)。关键几行（节选）：

```toml
dependencies = [
    "apache-tvm-ffi>=0.1.10,<=0.1.12",
    "torch-c-dlpack-ext; python_version < '3.14'",
    "cloudpickle", "ml-dtypes", "numpy>=1.23.5", "psutil",
    "torch", "tqdm>=4.62.3", "typing-extensions>=4.10.0",
    "z3-solver>=4.13.0,<4.15.5",
]
```

注意 `z3-solver` 有严格版本区间（`>=4.13.0,<4.15.5`），MACA 文档也特别强调「z3-solver 版本应 >= 4.13.0，且安装路径下要能找到 include 和 lib 目录」。

**构建期依赖**（CMake/打包）：

参见 [pyproject.toml:75-93](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/pyproject.toml#L75-L93)。这里声明构建后端是 `scikit-build-core`，构建期需要 `cython>=3.1.0`、`z3-solver`、Linux 上的 `patchelf`。CMake 版本要求 `>=3.26.1`（见 [pyproject.toml:95-98](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/pyproject.toml#L95-L98)）。

> 对比：通用安装文档 [Installation.md:37-50](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/Installation.md#L37-L50) 同样要求 Python>=3.10、CUDA>=10.0，并允许用 Windows；而 MACA 文档只支持 Linux。这正呼应了上一讲「MACA 是本分支新增后端」的定位。

#### 4.1.4 代码实践

**实践目标**：把本模块所需的依赖项整理成一张可执行的「环境准备清单」。

**操作步骤**：

1. 打开终端，确认操作系统与 Python 版本：`uname -a` 与 `python3 --version`。
2. 阅读 [Installation_maca.md:19-22](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/Installation_maca.md#L19-L22) 的 apt 命令，逐条核对哪些包你还没装。
3. 阅读上述 `pyproject.toml` 依赖块，记录 `apache-tvm-ffi`、`z3-solver`、`torch` 的版本约束。

**需要观察的现象**：把每条依赖记成「名字 / 版本约束 / 用途」三列的表格。

**预期结果**：得到一张类似下面的表（节选）：

| 依赖 | 版本约束 | 用途 |
|------|----------|------|
| cmake | >= 3.26.1 | C++ 构建系统 |
| cython | >= 3.1.0 | 生成 Python↔C++ 粘合层 |
| apache-tvm-ffi | 0.1.10–0.1.12 | TVM 跨语言调用 |
| z3-solver | 4.13.0–<4.15.5 | 编译优化用的 SMT 求解器 |
| torch | （无上限） | 测试与数据搬运 |
| MACA SDK | >= 3.3.0 | MetaX GPU 编译器（mxcc） |

> 这些命令是否能在你的机器上一键跑通，**待本地验证**（取决于你是否已有 root 权限与正确的发行版）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 MACA 构建路径要求 `z3-solver` 的安装路径下能找到 `include` 和 `lib` 两个目录？

**参考答案**：因为 z3 既要被 Python 导入（运行期），又要被 CMake 当作原生库链接进 `libtilelang.so`（`find_package(Z3)` 需要头文件和 `.so`）。`Installation_maca.md` 的注释正是为此提醒。

**练习 2**：克隆仓库时为什么要加 `--recursive`？如果忘了加会怎样？

**参考答案**：tilelang-metax 依赖一个定制版 TVM 子模块（`3rdparty/tvm`，见 `.gitmodules`）。不加 `--recursive` 子模块为空，CMake 配置阶段会在 [CMakeLists.txt:340-344](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt#L340-L344) 报错 "Nor tvm provided or submodule checkout-ed"。补救办法是再跑一次 `git submodule update --init --recursive`（顶层 CMake 其实也会在 [CMakeLists.txt:160-182](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt#L160-L182) 自动尝试一次）。

---

### 4.2 CMake 构建

#### 4.2.1 概念说明

依赖备齐后，真正「编译」由 CMake 主导。本模块要搞清楚三件事：

1. **顶层 `CMakeLists.txt` 如何收集源码、产出 `libtilelang.so`**；
2. **后端开关（`USE_CUDA`/`USE_MACA` 等）如何被识别和自动选择**；
3. **MACA 后端为什么接线方式特殊**。

理解第 3 点是本讲的核心收获：它揭示了「fork 新增一个后端」时，构建系统是怎么被增量地修改的。

#### 4.2.2 核心流程

顶层 CMake 的执行流程可概括为：

```
1. project() / 检测编译器 / ccache
2. 定义后端开关集合 TILELANG_BACKENDS = {CUDA, ROCM, METAL, LLVM}
3. 用 option() 给每个后端建 -DUSE_XXX 选项
4. load_tvm.cmake 引入 TVM 子模块
5. 收集 TileLang 源码 (TILE_LANG_SRCS)
6. 自动选择后端：
     - 若用户用 -DUSE_XXX 显式指定 → 用指定值
     - 否则读 USE_CUDA/USE_ROCM/USE_MACA/USE_METAL/USE_LLVM 环境变量
     - 都没给 → 按平台默认（macOS→Metal，Linux 有 CUDA 工具链→CUDA）
7. include 各后端的子 CMakeLists（cuda/rocm/metal/maca/webgpu/cpu）
8. add_library(tilelang SHARED ...) 产出 libtilelang.so
9. 编译 cython wrapper
```

注意第 2 步：`TILELANG_BACKENDS` 这个集合里**没有 MACA**。这就是 MACA 接线方式不同的根源。

#### 4.2.3 源码精读

**后端开关集合（关键！MACA 不在其中）**：

参见 [CMakeLists.txt:256](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt#L256)：

```cmake
set(TILELANG_BACKENDS CUDA ROCM METAL LLVM)
```

随后 [CMakeLists.txt:297-299](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt#L297-L299) 只对这个集合里的后端调用 `tilelang_define_backend_option`，也就是只给 CUDA/ROCm/METAL/LLVM 建立标准的 `-DUSE_XXX` cache 选项。**MACA 没有走这条路**。

**MACA 通过环境变量被识别**：

参见 [CMakeLists.txt:416-423](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt#L416-L423)：

```cmake
if(DEFINED ENV{USE_MACA})
  set(_tilelang_backend_env_selected ON)
  if($ENV{USE_MACA})
    set(USE_MACA ON)
  else()
    set(USE_MACA OFF)
  endif()
endif()
```

注意这里是 `DEFINED ENV{USE_MACA}`——它检查的是**环境变量** `USE_MACA`，而不是 CMake 的 `-D` cache 变量。这正是 MACA 文档把构建命令写成 `USE_MACA=ON cmake -B build`（shell 环境变量前缀）而不是 `cmake -DUSE_MACA=ON ..` 的原因。

**MACA 后端源码始终被 include**：

参见 [CMakeLists.txt:460-466](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt#L460-L466)：

```cmake
include("${CMAKE_CURRENT_SOURCE_DIR}/src/cpu/CMakeLists.txt")
include("${CMAKE_CURRENT_SOURCE_DIR}/src/cuda/CMakeLists.txt")
include("${CMAKE_CURRENT_SOURCE_DIR}/src/rocm/CMakeLists.txt")
include("${CMAKE_CURRENT_SOURCE_DIR}/src/metal/CMakeLists.txt")
include("${CMAKE_CURRENT_SOURCE_DIR}/src/maca/CMakeLists.txt")
include("${CMAKE_CURRENT_SOURCE_DIR}/src/webgpu/CMakeLists.txt")
```

也就是说 `src/maca/CMakeLists.txt` **无条件**会被执行；它内部再根据 `USE_MACA` 决定要不要真正去链接 MACA SDK。这点马上在 4.3 详述。

**产物是 `libtilelang.so`**：

参见 [CMakeLists.txt:572-582](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt#L572-L582)。在 POSIX 上，TileLang 的对象文件被组装成一个共享库 `tilelang`，并链接到 `tvm_compiler`，输出到 `build/lib/`。Python 前端在运行时通过 `dlopen` 加载它。

> 旁注：构建会自动启用 `ccache`/`sccache`（见 [CMakeLists.txt:184-253](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt#L184-L253)），二次编译会快很多。

#### 4.2.4 代码实践

**实践目标**：在不真正编译的前提下，读懂「MACA 后端开关」在 CMake 中的完整传播链路。

**操作步骤**：

1. 在仓库根目录打开 `CMakeLists.txt`，定位到第 256 行，确认 `TILELANG_BACKENDS` 不含 MACA。
2. 跳到第 416–423 行，确认 `USE_MACA` 是从 `$ENV{USE_MACA}` 读出来的。
3. 打开 `src/maca/CMakeLists.txt` 第 10–12 行，确认它用 `if(NOT USE_MACA) return() endif()` 做门禁。
4. 把这条链路画成时序图：`shell: USE_MACA=ON` → `CMake: $ENV{USE_MACA}` → `set(USE_MACA ON)` → `src/maca/CMakeLists.txt` 放行 → `find_maca()`。

**需要观察的现象**：链路上每一步「变量名」是什么、在哪一行被设置。

**预期结果**：得到一张类似下面的传播表：

| 步骤 | 位置 | 变量/动作 |
|------|------|-----------|
| 1 | shell 命令行 | 环境变量 `USE_MACA=ON` |
| 2 | CMakeLists.txt:416 | `DEFINED ENV{USE_MACA}` 为真 |
| 3 | CMakeLists.txt:419 | `set(USE_MACA ON)`（CMake 变量） |
| 4 | src/maca/CMakeLists.txt:10 | `if(NOT USE_MACA)` 为假，不 return |
| 5 | src/maca/CMakeLists.txt:14-15 | 调用 `find_maca(${USE_MACA})` |

#### 4.2.5 小练习与答案

**练习 1**：假如你误把命令写成 `cmake -DUSE_MACA=ON -B build`（而不是 `USE_MACA=ON cmake -B build`），会发生什么？

**参考答案**：`-DUSE_MACA=ON` 会建立一个 CMake cache 变量，但 [CMakeLists.txt:416](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt#L416) 检查的是 `ENV{USE_MACA}`（环境变量），不是 cache 变量，所以这条 `if` 不成立，`USE_MACA` 不会被设为 ON。结果 [src/maca/CMakeLists.txt:10](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/CMakeLists.txt#L10) 处 `if(NOT USE_MACA)` 为真、提前 `return()`，MACA SDK 不会被链接。**因此必须用环境变量前缀的写法**，这也是文档强调 `USE_MACA=ON cmake` 的原因。

**练习 2**：为什么 `src/maca/CMakeLists.txt` 要「无条件被 include，内部再门禁」，而不是像 CUDA 那样由顶层条件控制？

**参考答案**：因为 MACA 有一部分源码（[src/maca/CMakeLists.txt:2-8](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/CMakeLists.txt#L2-L8) 里的 `intrin_rule_maca.cc`、`lower_maca_intrin.cc` 等）即使不启用 MACA 后端也需要参与编译（用于代码生成阶段的注册/符号），所以它被设计成「常驻 + 门禁」两层结构。

---

### 4.3 MACA SDK

#### 4.3.1 概念说明

启用了 `USE_MACA` 之后，CMake 还需要能在你机器上**找到 MACA SDK**（头文件和库），才能真正编译 MACA 后端代码。本模块讲两件事：

1. **怎么安装 MACA SDK 与 cu-bridge**（面向 MetaX 硬件用户）。
2. **CMake 是怎么定位 SDK 的**（`find_maca` 宏的搜索顺序）。

即使你没有 MetaX 显卡，第 2 点也值得读懂——它展示了 TileLang「为某个后端写一个 Find 模块」的标准模式，将来你要加自己的后端时（见 u9-l1）会用到。

#### 4.3.2 核心流程

**SDK 安装流程**（来自 MACA 文档）：

```
1. 添加 MetaX 软件源 + 导入 GPG key
2. apt 安装 maca-sdk 与 metax-driver
3. usermod -aG video（获取设备访问权限）→ reboot
4. （可选）源码编译安装 cu-bridge 到 /opt/maca/tools/cu-bridge
5. （可选）从 MetaX pypi 装 PyTorch 全家桶
```

**SDK 定位流程**（`find_maca` 宏的搜索优先级）：

```
find_maca(use_maca):
  if use_maca 是一个目录     → 用它作为 SDK 路径        # 即 USE_MACA=/path/to/sdk
  elif $MACA_PATH 是目录     → 用 $MACA_PATH
  elif /opt/maca 是目录      → 用 /opt/maca
  else                       → 空（找不到）
  在 SDK 下找 include/ 与 lib/mcruntime、lib/mxc-runtime64
```

#### 4.3.3 源码精读

**安装 MACA SDK 与驱动**：

参见 [Installation_maca.md:30-44](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/Installation_maca.md#L30-L44)。核心命令（节选）：

```bash
curl -fsSL https://repos.metax-tech.com/public.gpg.key | apt-key add -
echo "deb ... https://repos.metax-tech.com/r/maca-sdk-deb/ stable main" \
  | tee /etc/apt/sources.list.d/maca-sdk-deb.list
apt-get update
apt-get install maca-sdk metax-driver
usermod -aG video ${USER}
reboot
```

**安装 cu-bridge**：

参见 [Installation_maca.md:48-56](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/Installation_maca.md#L48-L56)。cu-bridge 用 CMake 编译安装到 `/opt/maca/tools/cu-bridge`：

```bash
export MACA_PATH=/opt/maca
git clone https://gitee.com/metax-maca/cu-bridge.git
cd cu-bridge && mkdir build && cd ./build
cmake -DCMAKE_INSTALL_PREFIX=/opt/maca/tools/cu-bridge ../
make && make install
```

**MACA 后端构建入口**：

参见 [src/maca/CMakeLists.txt:10-30](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/CMakeLists.txt#L10-L30)。门禁通过后，调用 `find_maca` 并把 MACA 相关源码（`codegen_maca.cc`、`maca_target_kind.cc`、`maca_device_api.cc` 等）加入编译列表，再把 SDK 的 include 目录加入头文件搜索路径：

```cmake
if (NOT USE_MACA)
  return()
endif()

include(cmake/FindMACA.cmake)
find_maca(${USE_MACA})

file(GLOB TILE_LANG_MACA_SRCS
  src/maca/op/*.cc
  src/maca/codegen/codegen_maca.cc
  src/maca/runtime/maca_target_kind.cc
  src/maca/runtime/maca_device_api.cc
  ...
)
list(APPEND TILE_LANG_SRCS ${TILE_LANG_MACA_SRCS})
list(APPEND TILE_LANG_INCLUDES ${MACA_INCLUDE_DIRS})
```

**SDK 探测宏的搜索顺序**：

参见 [cmake/FindMACA.cmake:33-53](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/cmake/FindMACA.cmake#L33-L53)。这是本模块最值得精读的片段（节选）：

```cmake
macro(find_maca use_maca)
  if(IS_DIRECTORY ${__use_maca})            # 显式 SDK 路径
    set(__maca_sdk ${__use_maca})
  elseif(IS_DIRECTORY $ENV{MACA_PATH})      # MACA_PATH 环境变量
    set(__maca_sdk $ENV{MACA_PATH})
  elseif(IS_DIRECTORY /opt/maca)            # 默认安装位置
    set(__maca_sdk /opt/maca)
  endif()
  if(__maca_sdk)
    set(MACA_INCLUDE_DIRS ${__maca_sdk}/include)
    find_library(MACA_MACAMCC_LIBRARY mcruntime ${__maca_sdk}/lib)
    find_library(MACA_HCA_LIBRARY mxc-runtime64 ${__maca_sdk}/lib)
    if(MACA_MACAMCC_LIBRARY)
      set(MACA_FOUND TRUE)
    endif()
  endif()
endmacro()
```

这段决定了：只要你在 `USE_MACA=ON` 的同时设好 `MACA_PATH`（或装在默认的 `/opt/maca`），CMake 就能自动找到 SDK。它要找的两个原生库是 `mcruntime`（MACA 运行时）和 `mxc-runtime64`。

**Python 侧的 SDK 路径检测（运行期）**：

构建期用 CMake 找 SDK，运行期 `import tilelang` 时还会再用 Python 找一次。参见 [tilelang/env.py:188-198](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L188-L198)：

```python
def _find_maca_home() -> str:
    maca_home = os.environ.get("MACA_PATH") or os.environ.get("MACA_HOME")
    if maca_home is None:
        mxcc_path = shutil.which("mxcc")
        if mxcc_path is not None:
            maca_home = os.path.dirname(os.path.dirname(mxcc_path))
        else:
            maca_home = "/opt/maca"
            ...
    return maca_home ...
```

它的优先级是 `MACA_PATH` / `MACA_HOME` 环境变量 → PATH 上的 `mxcc` → `/opt/maca`。这与 CMake 侧的搜索顺序基本一致，说明「`MACA_PATH=/opt/maca` 是约定俗成的默认值」。

#### 4.3.4 代码实践

**实践目标**：理清 MACA SDK 在「构建期」和「运行期」分别是如何被定位的，并写出对应的环境变量清单。

**操作步骤**：

1. 阅读 [cmake/FindMACA.cmake:33-44](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/cmake/FindMACA.cmake#L33-L44)，记录 CMake 侧的三级搜索顺序。
2. 阅读 [tilelang/env.py:188-198](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L188-L198)，记录 Python 侧的三级搜索顺序。
3. 对比两者，找出共同约定的「默认路径」和「首选环境变量」。

**需要观察的现象**：两侧搜索顺序是否完全一致？哪个环境变量名是双方都认的？

**预期结果**：

| 阶段 | 首选来源 | 次选 | 默认 |
|------|----------|------|------|
| 构建期（CMake） | `USE_MACA` 传入的显式路径 | `$ENV{MACA_PATH}` | `/opt/maca` |
| 运行期（Python） | `MACA_PATH` / `MACA_HOME` | PATH 上的 `mxcc` | `/opt/maca` |

共同结论：把 SDK 装在 `/opt/maca`，并 `export MACA_PATH=/opt/maca`，是两边都最省心的做法。

> 本模块涉及的 SDK 安装命令是否能在你的机器上跑通，**待本地验证**（需要 MetaX 硬件、root 权限及可访问的 MetaX 软件源）。没有 MetaX 显卡时，你仍可完成上面的「源码阅读型实践」。

#### 4.3.5 小练习与答案

**练习 1**：如果你把 MACA SDK 装在了非标准位置（比如 `/home/me/my-maca`），构建期和运行期分别要怎么告诉 tilelang-metax？

**参考答案**：
- 构建期有两种写法：`USE_MACA=/home/me/my-maca cmake -B build`（让 `find_maca` 把它当显式路径，见 [FindMACA.cmake:35-37](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/cmake/FindMACA.cmake#L35-L37)），或 `export MACA_PATH=/home/me/my-maca` 后再 `USE_MACA=ON cmake -B build`。
- 运行期：`export MACA_PATH=/home/me/my-maca`，Python 侧 [env.py:189](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L189) 会读到。

**练习 2**：`find_maca` 找到 SDK 后，`MACA_FOUND` 为真的判据是什么？

**参考答案**：是成功找到 `mcruntime` 库（`MACA_MACAMCC_LIBRARY`），见 [FindMACA.cmake:48-53](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/cmake/FindMACA.cmake#L48-L53)。`mxc-runtime64` 即便没找到也不会让 `MACA_FOUND` 为假（但链接阶段可能会出问题）。

---

### 4.4 安装验证

#### 4.4.1 概念说明

构建完成不等于「能用」。验证要做两件事：

1. **Python 能 import**：说明动态库 `libtilelang.so` 被正确加载、依赖（tvm-ffi、z3）就位。
2. **能跑一个最小 kernel**：说明编译器后端真的能工作（生成代码并执行）。

此外，MACA 运行期还需要一组**环境变量**让动态链接器找到 MACA 的库，这是 NVIDIA 路径上不会遇到的额外步骤。

#### 4.4.2 核心流程

```
1. （MACA 专属）导出运行期环境变量：
     MACA_PATH, LD_LIBRARY_PATH（含 lib 与 mxgpu_llvm/lib）, PATH（含 mxgpu_llvm/bin）
2. 设置 PYTHONPATH 指向源码根（开发模式下）
3. python -c "import tilelang; print(tilelang.__version__)"   ← 最低门槛
4. python examples/quickstart.py                              ← 跑通一个 GEMM
```

#### 4.4.3 源码精读

**MACA 文档给出的构建命令**：

参见 [Installation_maca.md:91-108](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/Installation_maca.md#L91-L108)。注意它要求先配置 git 提交者身份（因为版本号里会嵌入 git hash），再用环境变量触发 MACA：

```bash
git config --global user.email "you@example.com"
git config --global user.name "Your Name"

cd tilelang-metax
USE_MACA=ON cmake -B build
make -C build -j 32
```

> 文档写的是 `make -C build -j 32`，其中的 `32` 是并行度，按你的 CPU 核数调整即可。

**单独安装 tvm-ffi**：

参见 [Installation_maca.md:110-114](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/Installation_maca.md#L110-L114)。MACA 路径上需要手工把 tvm-ffi 装进 Python 环境：

```bash
cd 3rdparty/tvm/3rdparty/tvm-ffi && pip install . && cd -
```

**MACA 文档给出的验证命令**：

参见 [Installation_maca.md:116-127](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/Installation_maca.md#L116-L127)。这是本模块最关键的一段（节选）：

```bash
export MACA_PATH=/opt/maca
export LD_LIBRARY_PATH=${MACA_PATH}/lib:${MACA_PATH}/mxgpu_llvm/lib:$LD_LIBRARY_PATH
export PATH=${MACA_PATH}/mxgpu_llvm/bin:${PATH}
export PYTHONPATH=/path/to/tilelang-metax:$PYTHONPATH

python -c "import tilelang; print(tilelang.__version__)"
python path/to/tilelang-metax/examples/quickstart.py
```

为什么 `LD_LIBRARY_PATH` 要同时包含 `${MACA_PATH}/lib` 和 `${MACA_PATH}/mxgpu_llvm/lib`？因为 MACA 的运行时库（`mcruntime` 等）和它的 LLVM 工具链库分别放在这两个目录；缺了任何一个，`import tilelang` 或运行 kernel 时都会报「找不到 `.so`」。

**版本号是怎么来的**：

参见 [tilelang/__init__.py:10-47](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py#L10-L47)。`tilelang.__version__` 在源码检出时优先读仓库根的 `VERSION` 文件（当前内容为 `0.1.12`），并通过 `version_provider` 注入 git 信息。这也是上面为什么要先 `git config` 的原因——版本号依赖 git 元数据。

**开发模式下的库加载提示**：

参见 [tilelang/env.py:34-52](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L34-L52)。当没有打包成 wheel（即 `3rdparty/` 不存在）时，tilelang 进入「dev 模式」，从 `build/lib` 加载库并打印一条警告：

```
Loading tilelang libs from dev root: /path/to/tilelang-metax/build
```

看到这条日志，说明你的 `build/lib/libtilelang.so` 正在被加载——这是验证「C++ 部分确实编译出来了」的直观信号。

#### 4.4.4 代码实践

**实践目标**：把 MACA 安装文档里的「构建 + 验证」整理成一份可复制的 runbook，并标注每个环境变量的作用。

**操作步骤**：

1. 从 [Installation_maca.md:101-108](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/Installation_maca.md#L101-L108) 抄出构建命令，记下 `USE_MACA=ON` 与 `MACA_PATH`。
2. 从 [Installation_maca.md:116-122](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/Installation_maca.md#L116-L122) 抄出验证用的 4 个 `export`。
3. 为每个环境变量写一句话作用说明。

**需要观察的现象**：导出变量前后，分别运行 `python -c "import tilelang"`，观察报错信息的变化。

**预期结果**：得到如下 runbook（变量作用表）：

| 变量 | 作用 | 缺失时的可能现象 |
|------|------|------------------|
| `USE_MACA=ON` | 构建期启用 MACA 后端（环境变量） | `libtilelang.so` 不含 MACA codegen |
| `MACA_PATH` | 指定 MACA SDK 根目录（构建期+运行期） | `find_maca` 找不到 SDK / 运行期定位失败 |
| `LD_LIBRARY_PATH` | 让动态链接器找到 MACA 的 `.so` | `import tilelang` 报 `libmcruntime.so` 找不到 |
| `PATH`（含 mxgpu_llvm/bin） | 让 `mxcc` 等工具可被发现 | 运行期找不到 MACA 编译器 |
| `PYTHONPATH` | 让 Python 找到源码根（开发模式） | `ModuleNotFoundError: No module named 'tilelang'` |

> 验证命令的实际输出（`__version__` 的值、quickstart 是否跑通）**待本地验证**，取决于你是否具备 MetaX 硬件与完整 MACA 环境。在不具备硬件时，最低限度的验证是 `python -c "import tilelang"` 不报错——即便没有 MACA，只要构建时没强制只编 MACA，这个导入通常也能成功（会加载默认/可用的后端）。

#### 4.4.5 小练习与答案

**练习 1**：构建时为什么要先 `git config --global user.email/name`？

**参考答案**：因为 `tilelang.__version__`（通过 `version_provider`）会把 git 提交者信息和 commit hash 嵌入版本号；若 git 没有配置提交者身份，某些版本生成步骤可能失败或告警。见 [tilelang/__init__.py:20-29](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py#L20-L29)。

**练习 2**：如果你只运行了 `python -c "import tilelang"` 成功，能说明 MACA 后端可用吗？

**参考答案**：不能。`import tilelang` 成功只说明动态库和 Python 依赖就位；MACA 后端是否真正可用，还需要 (a) 构建时确实用了 `USE_MACA=ON`，(b) 运行时 `MACA_PATH`/`LD_LIBRARY_PATH` 正确，(c) 能用 `target={"kind":"maca"}` 编译并跑通一个 kernel（这会在 [u3-l3 在 Metax GPU 上运行](u3-l3-running-on-metax-maca.md) 详细讲）。完整自检应跑 `examples/quickstart.py`。

---

## 5. 综合实践

**综合任务**：为 tilelang-metax 的 MACA 构建编写一份「从零到验证」的 runbook 脚本（`setup_maca.sh`，仅作为练习产物，不要提交到仓库），把本讲四个模块串起来。

要求脚本（示例代码，非项目原有文件）包含以下分段，并在每段开头用注释说明「对应本讲哪个模块 / 引用了哪个源码文件」：

```bash
#!/usr/bin/env bash
# 示例代码：MACA 构建 runbook（练习产物，非仓库文件）
set -euo pipefail

# === 4.1 构建依赖（参考 Installation_maca.md:19-22 / pyproject.toml:29-45）===
apt-get update
apt-get install -y python3 python3-dev python3-setuptools gcc zlib1g-dev \
    build-essential cmake libedit-dev git
pip install z3-solver cython psutil cloudpickle tqdm torch-c-dlpack-ext

# === 4.3 MACA SDK（参考 Installation_maca.md:30-56）===
export MACA_PATH=/opt/maca
# （此处省略 maca-sdk / metax-driver / cu-bridge 的安装，需 MetaX 硬件与 root）

# === 4.2 CMake 构建（参考 Installation_maca.md:101-108）===
git clone --recursive https://github.com/tile-ai/tilelang-metax.git
cd tilelang-metax
git config --global user.email "you@example.com"
git config --global user.name "Your Name"
USE_MACA=ON cmake -B build
make -C build -j "$(nproc)"

# tvm-ffi（参考 Installation_maca.md:110-114）
( cd 3rdparty/tvm/3rdparty/tvm-ffi && pip install . )

# === 4.4 验证（参考 Installation_maca.md:116-127）===
export LD_LIBRARY_PATH=${MACA_PATH}/lib:${MACA_PATH}/mxgpu_llvm/lib:$LD_LIBRARY_PATH
export PATH=${MACA_PATH}/mxgpu_llvm/bin:${PATH}
export PYTHONPATH="$(pwd):$PYTHONPATH"

python -c "import tilelang; print(tilelang.__version__)"
python examples/quickstart.py
```

**完成标准**：

1. 脚本能体现「依赖 → SDK → 构建 → 验证」四段顺序。
2. 每段注释里准确指明了对应的源码文件/行号。
3. 你能口头解释：为什么 `USE_MACA=ON` 必须放在 `cmake` 前面（环境变量），而 `MACA_PATH` 既要在构建期也要在运行期导出。
4. 你能指出脚本中哪一步在没有 MetaX 硬件时会失败、为什么——并知道此时退而求其次的验证方式是 `import tilelang`。

> 整个脚本能否端到端跑通**待本地验证**；本练习的重点是「把构建链路讲清楚」，而非真的在一台机器上装好 MetaX 全家桶。

---

## 6. 本讲小结

- tilelang-metax 是「C++ 编译器 + Python 前端」，安装的本质是用 CMake 把 `libtilelang.so` 编译出来再让 Python 加载。
- 构建依赖分三层：OS 级（gcc/cmake/python-dev）、Python 级（cython/apache-tvm-ffi/z3-solver/torch）、硬件 SDK 级（CUDA/ROCm/**MACA**）。
- **MACA 后端的接线方式与 CUDA/ROCm 不同**：它不在 `TILELANG_BACKENDS`（[CMakeLists.txt:256](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt#L256)）里，而是通过 `USE_MACA` **环境变量**触发（[CMakeLists.txt:416-423](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt#L416-L423)），因此构建命令是 `USE_MACA=ON cmake -B build` 而非 `cmake -DUSE_MACA=ON`。
- MACA SDK 由 `cmake/FindMACA.cmake` 按「显式路径 → `MACA_PATH` → `/opt/maca`」三级搜索定位，运行期 Python 侧（[env.py:188-198](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L188-L198)）用几乎相同的顺序再找一次。
- 验证分两级：最低门槛 `python -c "import tilelang"`；完整验证跑 `examples/quickstart.py`。MACA 运行期还需导出 `MACA_PATH`/`LD_LIBRARY_PATH`/`PATH`。
- 构建出的库放在 `build/lib/`，开发模式下 `import tilelang` 会打印 `Loading tilelang libs from dev root: .../build` 的提示。

---

## 7. 下一步学习建议

安装好 tilelang-metax 后，建议按以下顺序继续：

1. **[u1-l3 仓库目录结构与代码组织](u1-l3-repo-layout.md)**：本讲我们聚焦了构建相关文件，下一讲会把整个仓库的目录布局讲清楚，帮你建立「改某个功能要去哪个目录」的全局地图。
2. **[u1-l4 第一个 kernel：跑通 GEMM](u1-l4-first-gemm-kernel.md)**：用本讲验证时提到的 `examples/quickstart.py`，正式写出并运行第一个 GEMM kernel。
3. **[u3-l3 在 Metax GPU 上运行（MACA target）](u3-l3-running-on-metax-maca.md)**：本讲你装好了 MACA 后端，但还没真正用 `target={"kind":"maca"}` 编译过一个 kernel——那是 u3-l3 的主题。
4. 想深入了解构建系统细节的读者，可以直接精读 [CMakeLists.txt](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt) 中后端自动选择（[L384-L457](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt#L384-L457)）和 cython wrapper 编译（[L595-L640](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt#L595-L640)）两段。
