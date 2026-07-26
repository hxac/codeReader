# 安装、构建与运行环境

## 1. 本讲目标

学完本讲后，你应该能够：

1. 用三种方式（pip、源码构建、nightly）成功安装 tilelang，并通过 `import tilelang` 验证。
2. 理解源码构建背后的依赖链条：为什么需要 TVM 子模块、CUTLASS、Composable Kernel，以及 `USE_CUDA` / `USE_ROCM` / `USE_METAL` / `USE_LLVM` 等 CMake 选项的作用。
3. 看懂 `pyproject.toml` 如何用 scikit-build-core 把 C++ 工程打包成 Python wheel，并把 `3rdparty` 映射进安装目录。
4. **重点**：读懂 `tilelang/env.py`（运行时环境变量中枢）和 `tilelang/libinfo.py`（原生库定位器）这两个最小模块，搞清楚 `import tilelang` 时 `libtilelang.so` 到底是从哪里被找到并加载的。

本讲承接上一讲「项目总览与架构定位」：上一讲建立了「Python DSL → TVM TIR → Pass → 设备代码 → Kernel Adapter」的全景认知；本讲解决的是「我手上这份代码，怎么变成一个能 `import` 的 Python 包，以及 `import` 时那个 C++ 引擎从哪里被加载」。

## 2. 前置知识

在动手之前，用大白话理解几个概念（不熟悉也没关系，本讲会反复用到）：

- **原生库（native / shared library）**：tilelang 不只是纯 Python。它的编译器核心（Pass、代码生成器）是用 C++ 写的，编译后得到一个动态库——Linux 上是 `libtilelang.so`，macOS 上是 `libtilelang.dylib`，Windows 上则被合并进 `tvm_compiler.dll`。Python 层通过 `ctypes` 把这个库加载进进程，才能调用其中的 C++ 函数。
- **TVM**：tilelang 构建在 TVM 之上，把它当作「TIR 中间表示 + 一套基础设施」来用。仓库 `3rdparty/tvm` 里是一个定制版 TVM，源码构建时会和 tilelang 一起编译。
- **CUTLASS / Composable Kernel（CK）**：分别是 NVIDIA 与 AMD 官方的高性能矩阵运算模板库（header-only）。tilelang 生成 GEMM 等算子的设备代码时会 include 这些头文件，所以它们的 include 路径在构建期和运行期都要能被找到。
- **wheel（.whl）**：Python 的标准二进制分发格式。因为 tilelang 含 C++ 代码，它的 wheel 不只是 `.py` 文件，里面还打包了编译好的 `.so`/`.dll`。
- **scikit-build-core**：把 CMake 工程接入 Python 打包（PEP 517）的工具——它驱动 CMake 编译 C++，再把产物塞进 wheel。
- **环境变量**：tilelang 大量用环境变量配置路径与行为，本讲会涉及 `CUDA_HOME`、`TVM_LIBRARY_PATH`、`TL_CUTLASS_PATH` 等，它们都集中定义在 `tilelang/env.py`。

一句话总结 tilelang 的安装本质：**编译一份 C++ 共享库，再配上一堆 Python 文件和第三方头文件，打包成 wheel 供 `pip` 安装；运行时再由 Python 把那份共享库找出来加载。**

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
| --- | --- |
| [docs/get_started/Installation.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/Installation.md) | 官方安装手册，覆盖 pip / 源码 / Docker / nightly 四条路径与构建期/运行期环境变量说明。 |
| [pyproject.toml](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml) | 项目元信息、Python 运行时依赖、构建后端（scikit-build-core）配置、wheel 中 `3rdparty` 的映射规则、cibuildwheel CI 配置。 |
| [CMakeLists.txt](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt) | C++ 侧的 CMake 主入口：后端开关、TVM 子模块加载、`libtilelang.so` 目标定义、stub 库与 rpath。 |
| [cmake/load_tvm.cmake](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/cmake/load_tvm.cmake) | 决定用哪个 TVM 源码（自带子模块，或 `TVM_ROOT` 指向的外部 TVM）。 |
| [version_provider.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/version_provider.py) | 给 scikit-build-core 动态生成版本号，把 SDK 后缀和 git hash 拼进 `tilelang.__version__`。 |
| [tilelang/env.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py) | 运行时环境变量中枢：`EnvVar` 描述符、`Environment` 配置类、CUDA/ROCm 定位、缓存控制、第三方库路径初始化、库搜索目录 `TL_LIBS`。 |
| [tilelang/libinfo.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/libinfo.py) | 原生库定位器：跨平台拼出库文件名并从候选目录中找到 `libtilelang.so`。 |
| [tilelang/__init__.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py) | 包入口：计算 `__version__`、轻量导入模式、用 `libinfo` 加载原生库、导出公共 API。 |

> 上面所有形如 `tile-ai/tilelang/blob/c6294f07.../...` 的链接都是指向当前 HEAD `c6294f07` 的永久链接，点击可直接跳转到对应文件与行。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：4.1 三种安装方式、4.2 源码构建与打包机制、4.3 `tilelang.env` 运行时环境中枢、4.4 `tilelang.libinfo` 原生库定位。其中 **4.3 和 4.4 是本讲指定的两个核心最小模块**，它们共同回答「`import tilelang` 时 C++ 引擎从哪加载」。

### 4.1 三种安装方式：pip / 源码 / nightly

#### 4.1.1 概念说明

tilelang 提供三条主流安装路径，复杂度递增：

| 方式 | 命令 | 适用场景 |
| --- | --- | --- |
| PyPI 稳定版 | `pip install tilelang` | 大多数用户；预编译 wheel，开箱即用 |
| nightly 版 | `pip install tilelang -f https://tile-ai.github.io/whl/nightly` | 想要最新未发布功能/修复 |
| 源码构建 | `git clone --recursive ... && pip install . -v` | 要改 C++/Python 源码、或需要非默认后端（ROCm/Metal/CPU） |

理解这三者的关键在于：**预编译 wheel 已经把 `libtilelang.so` 和 `3rdparty`（TVM/CUTLASS/CK）打包好了**，所以安装后无需编译；而源码构建则要在本机跑 CMake 把 C++ 编出来。无论哪条路径，最终验证命令都一样。

#### 4.1.2 核心流程

```
需要改源码 / 自定义后端？
├── 是 ──> 源码构建（git clone --recursive + pip install . -v）
└── 否 ──> 想要最新未发布功能？
           ├── 是 ──> nightly（pip install -f <nightly 链接>）
           └── 否 ──> pip install tilelang（PyPI 稳定版）
```

三种方式验证安装是否成功的命令完全相同：

```bash
python -c "import tilelang; print(tilelang.__version__)"
```

这一行其实触发两件事：① 导入 Python 包并加载原生库（详见 4.3、4.4）；② 打印版本号（版本号来源见 4.2.3）。

#### 4.1.3 源码精读

**pip 安装的前提条件**，见官方手册 [docs/get_started/Installation.md:5-9](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/Installation.md#L5-L9)：glibc ≥ 2.28（Ubuntu 20.04+）、Python ≥ 3.10、CUDA ≥ 10.0（或用 pip 提供的 CUDA 工具链 ≥ 13.0）。最简安装命令见 [docs/get_started/Installation.md:14-15](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/Installation.md#L14-L15)，验证命令见 [docs/get_started/Installation.md:32-33](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/Installation.md#L32-L33)。

**nightly 安装**见 [docs/get_started/Installation.md:289-298](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/Installation.md#L289-L298)：用 `--find-links`（`-f`）指向专门的 whl 索引页，并提醒 nightly 可能不如正式版稳定。

**pip 安装会顺带装哪些 Python 依赖**，见 [pyproject.toml:29-45](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L29-L45)。几条值得记住：

- `apache-tvm-ffi>=0.1.11,<0.1.12`：TVM 的 Python↔C++ FFI 绑定，Python 调 C++ 编译器靠它（注意有版本上界，因为 ABI 变化快）。
- `torch` 与 `torch-c-dlpack-ext`：tilelang 把 PyTorch tensor 作为主要输入输出载体，并依赖 dlpack 零拷贝交换。
- `z3-solver>=4.13.0,<4.15.5`：SMT 求解器，tilelang 的符号分析（边界证明、索引化简）依赖它（这也是 `import` 时会尝试 `import z3` 的原因，见 4.3.3）。
- `numpy`、`ml-dtypes`（低精度 dtype）、`cloudpickle`、`psutil`、`tqdm`、`typing-extensions` 等常规依赖。

[pyproject.toml:47-63](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L47-L63) 定义可选依赖（`extras`）：`fp4`（启用 fp4 需要更新的 ml-dtypes）、`vis`（布局可视化需要 matplotlib）、`nvcc`（带 NVCC 的构建需要 `nvidia-cuda-nvcc` 等）。安装时用 `pip install "tilelang[nvcc]"` 启用。

#### 4.1.4 代码实践

**实践目标**：在本地环境用 pip 走通最简安装并验证。

**操作步骤**：

1. 确认 Python 版本：`python --version`（需 ≥ 3.10，见 [pyproject.toml:5](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L5)）。
2. 执行安装：`pip install tilelang`。
3. 验证：`python -c "import tilelang; print(tilelang.__version__)"`。

**需要观察的现象**：第 3 步应打印一串版本号，形如 `0.1.12+cu130.gitxxxxxxxx` 或纯 `0.1.12`（取决于 wheel 是否带版本标签，见 4.2.3）。

**预期结果**：命令退出码为 0，无 `ImportError` / `Cannot find libraries` 报错。

**关于无 GPU 环境**：pip wheel 用了 CUDA/HIP stub 库做 lazy load（见 4.2.3），所以即使机器没装 CUDA Toolkit，`import` 本身通常也能成功；只有真正编译/运行 kernel 时才需要 CUDA。

**待本地验证**：记录你机器上实际打印的 `__version__` 字符串，留作后续讲义对比。

#### 4.1.5 小练习与答案

**练习 1**：`pip install tilelang` 之后通常不需要再装 CUDA Toolkit 也能 `import` 成功，为什么？

> **答案**：因为官方 wheel 用了 CUDA/HIP 的 **stub 库**（见 4.2.3）。stub 库在导入时只满足符号链接关系，真正的 `libcuda.so.1` 等运行时是在调用时 lazy-load（dlopen）的。所以「能 import」不等于「能跑 kernel」——后者仍需真实 CUDA/ROCm Toolkit。

**练习 2**：`apache-tvm-ffi` 为什么被钉在 `<0.1.12`？

> **答案**：tvm-ffi 是 tilelang FFI 的基础，小版本之间可能改 ABI 或调用约定；钉死上界避免上游突然发新版破坏已验证的集成。遇到依赖冲突时以 `pyproject.toml` 声明的区间为准。

---

### 4.2 源码构建与打包机制

#### 4.2.1 概念说明

源码构建是理解 tilelang 工程结构的关键，涉及三个层次：

1. **C++ 编译**：用 CMake 把 `src/` 下的 C++ 源码 + TVM 子模块编译成 `libtilelang.so`。
2. **第三方依赖**：TVM（定制版，作为 git 子模块）、CUTLASS、Composable Kernel 作为头文件依赖参与编译。
3. **Python 打包**：scikit-build-core 作为 PEP 517 构建后端，驱动 CMake 编译，再把 `.so`、Python 代码、第三方头文件组装进 wheel。

理解这一层后，你就能解释「为什么 `git clone` 要加 `--recursive`」「为什么 wheel 体积大但开箱即用」「为什么 `TL_CUTLASS_PATH` 这种环境变量存在」。

#### 4.2.2 核心流程

源码构建的端到端流程：

```
git clone --recursive   (拉取 tilelang + TVM/CUTLASS/CK 子模块)
        │
        ▼
pip install . -v        (触发 scikit-build-core)
        │
        ▼
scikit-build-core 调用 CMake (按 pyproject.toml 的 [tool.scikit-build] 配置)
        │
        ├─ include(FindPipCUDAToolkit)       先找主机 CUDA，找不到回退 pip 包
        ├─ 定义 USE_CUDA/ROCM/METAL/LLVM 后端开关
        ├─ include(cmake/load_tvm.cmake)     定位 TVM 源码（自带或 TVM_ROOT）
        ├─ add_subdirectory(TVM)             编译 TVM
        ├─ add_library(tilelang SHARED ...)  链接成 libtilelang.so
        ├─ Cython 编译 cython_wrapper.pyx
        └─ install(... DESTINATION tilelang/lib)
        │
        ▼
version_provider 生成带 git hash 的版本号
        │
        ▼
组装 wheel: Python 包 tilelang/ + libtilelang.so + 3rdparty 头文件
```

后端选择遵循「显式优先」原则：用户在命令行或环境变量里显式指定了 `USE_*`，就用用户指定的；否则按平台默认——macOS 默认 Metal，Linux 在检测到 CUDA Toolkit 时默认 CUDA，否则不开。

#### 4.2.3 源码精读

**Python 侧构建后端声明**，见 [pyproject.toml:75-93](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L75-L93)：

```toml
[build-system]
requires = ["cython>=3.1.0", "scikit-build-core", "z3-solver>=4.13.0,<4.15.5",
            "patchelf>=0.17.2; platform_system == 'Linux'", ...]
build-backend = "scikit_build_core.build"
```

注意 `[build-system].requires`（构建 wheel 时需要的工具）和 `[project].dependencies`（安装后运行时需要的库）是两套——构建期还需要 `cython`、`patchelf` 等。

[pyproject.toml:95-115](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L95-L115) 是 scikit-build 配置：`wheel.py-api = "cp38"` 表示用稳定 ABI（cp38 起的 abi3，一个 wheel 能跨多个 Python 版本）；`cmake.version = ">=3.26.1"`；`build-dir = "build"`；并用 `metadata.version.provider` 指向 `version_provider.py` 动态生成版本号；Windows 下强制 `-G Ninja` 生成器。

**wheel 如何把 3rdparty 打进去（关键设计）**，见 [pyproject.toml:153-169](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L153-L169)：

```toml
[tool.scikit-build.wheel.packages]
tilelang = "tilelang"
# 把 3rdparty 内容放进 wheel 内的 tilelang/3rdparty，运行期才能找到 TVM 共享库
"tilelang/3rdparty/tvm/src" = "3rdparty/tvm/src"
"tilelang/3rdparty/tvm/python" = "3rdparty/tvm/python"
"tilelang/3rdparty/cutlass/include" = "3rdparty/cutlass/include"
"tilelang/3rdparty/composable_kernel/include" = "3rdparty/composable_kernel/include"
```

左侧形如 `"tilelang/3rdparty/tvm/src"` 的键是「**wheel 内的目标路径**」，右侧是「**仓库里的来源路径**」。也就是说，仓库根目录下的 `3rdparty/tvm/...` 会被安装到 wheel 里的 `tilelang/3rdparty/tvm/...`。这一步是运行期能找到 TVM/CUTLASS/CK 的前提——这也是为什么 tilelang wheel 体积较大但开箱即用。

**CI 构建（cibuildwheel）**，见 [pyproject.toml:252-316](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L252-L316)。两条最关键：

```toml
[tool.cibuildwheel.linux]
environment.USE_CUDA = "ON"
environment.USE_ROCM = "ON"
```

官方 Linux wheel 是「CUDA + ROCm 同时开启」的 **fat wheel**——ROCm 侧靠 vendored HIP 头文件 + dlopen stub 编译，运行期仍需真实 ROCm 运行时。cibuildwheel 的导入自检命令正是本讲的验证一行，见 [pyproject.toml:270-272](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L270-L272)。

**C++ 侧的 CMake 主入口**：

[CMakeLists.txt:4](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L4) 要求 CMake ≥ 3.26。注意 [CMakeLists.txt:9](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L9) 在 `project()` **之前** include `FindPipCUDAToolkit`——因为 CUDA 编译器必须在 project() 启用 CUDA 语言前确定好。这个模块先找主机 CUDA Toolkit，找不到回退 pip 装的 `nvidia-cuda-nvcc` 包，对应官方文档「With pip-provided CUDA toolchain (no host CUDA required)」路径（[docs/get_started/Installation.md:66-86](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/Installation.md#L66-L86)）。

[CMakeLists.txt:253](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L253) 定义四个后端 `CUDA ROCM METAL LLVM`，[CMakeLists.txt:264-296](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L264-L296) 用宏 `tilelang_define_backend_option` 为每个后端定义 `USE_*` 选项，并记下用户是否显式设置过。

[CMakeLists.txt:381-445](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L381-L445) 是后端自动选择逻辑：先看用户有没有显式指定；若没有，读环境变量 `USE_CUDA` 等；若都没有，则 macOS 开 Metal、Linux 检测到 CUDA Toolkit 就开 CUDA。这正是 [docs/get_started/Installation.md:123-128](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/Installation.md#L123-L128) 里那些 CMake 选项的来源。

**TVM 源码定位**，见 [cmake/load_tvm.cmake:3-13](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/cmake/load_tvm.cmake#L3-L13)：

```cmake
set(TVM_BUILD_FROM_SOURCE TRUE)
set(TVM_SOURCE ${CMAKE_SOURCE_DIR}/3rdparty/tvm)

if(DEFINED ENV{TVM_ROOT})
  if(EXISTS $ENV{TVM_ROOT}/cmake/config.cmake)
    set(TVM_SOURCE $ENV{TVM_ROOT})
  endif()
endif()
```

默认用 `3rdparty/tvm`；若设置了 `TVM_ROOT` 环境变量且该目录有 `cmake/config.cmake`，则改用外部 TVM。这就是 [docs/get_started/Installation.md:131-139](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/Installation.md#L131-L139)「Building with Customized TVM Path」的实现。

**最终链接出 libtilelang.so**，见 [CMakeLists.txt:549-569](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L549-L569)。要点：

- **POSIX**：`add_library(tilelang SHARED ...)` 编出独立的 `libtilelang.so`，放到 `build/lib/`。
- **Windows**：不单独出 `tilelang.dll`，而是把 tilelang 的目标文件塞进 `tvm_compiler.dll`（因为 Windows DLL 有 65535 符号上限，且 tilelang 需要访问 TVM 未导出的内部符号）。这正是 4.4 里 `libinfo.py` 在 Windows 上要找 `tvm_compiler.dll` 的根因。

**stub 库设计**，见 [CMakeLists.txt:300-324](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L300-L324)：CUDA/HIP 的 stub 库让 wheel 不在二进制里硬编码对 `libcudart.so` 等的依赖，运行时用 `dlopen` 懒加载真实库。这样同一个 wheel 能在不同 CUDA 主版本、甚至纯 CPU 机器上被 `import`。

**安装产物进 wheel**，见 [CMakeLists.txt:713-718](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L713-L718)：`install(TARGETS ... DESTINATION tilelang/lib)` 把 `.so`/`.dll` 装进 wheel 的 `tilelang/lib/`。

**版本号怎么带上 git hash**，见 [version_provider.py:12](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/version_provider.py#L12)（从 `VERSION` 文件读基础版本如 `0.1.12`）和 [version_provider.py:44-96](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/version_provider.py#L44-L96) 的 `dynamic_metadata`。后者根据 `USE_ROCM`/`USE_CUDA`/`CUDA_VERSION` 决定后端标签（`rocm` / `cuda` / `cu<major><minor>` / `cpu`），再用 `git rev-parse HEAD` 取前 8 位拼成 `gitxxxxxxxx`：

```python
if cuda_version := os.environ.get("CUDA_VERSION"):
    major, minor, *_ = cuda_version.split(".")
    backend = f"cu{major}{minor}"
else:
    backend = "cuda"
```

最终形如 `0.1.12+cu130.git0d4a74be`。设置 `NO_VERSION_LABEL=ON` 可关掉（见 [docs/get_started/Installation.md:315-326](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/Installation.md#L315-L326)）。

#### 4.2.4 代码实践

**实践目标**：用一个 CMake 选项改变构建行为并预测影响（源码阅读型实践，无需真编译）。

**操作步骤**：

1. 假设要构建「只支持 CPU、不带 CUDA」的 tilelang。读 [docs/get_started/Installation.md:303-309](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/Installation.md#L303-L309)。
2. 写出配置命令：`cmake .. -DUSE_CUDA=OFF -DUSE_LLVM=ON`。
3. 对照 [CMakeLists.txt:381-445](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L381-L445) 确认：因为你显式传了 `USE_CUDA=OFF`，CMake 不会因为「检测到 CUDA Toolkit」而自动打开。

**需要观察的现象**：构建产物里不会有 CUDA codegen；版本号后缀应变为 `+cpu.gitxxxxxxxx`（参考 [version_provider.py:65-69](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/version_provider.py#L65-L69)）。

**预期结果**：能解释「显式传 `USE_*`」与「让 CMake 自动选」的区别——自动选只在用户**没有**显式指定任何后端时才生效。

**待本地验证**：实际构建需本地编译环境与源码。

#### 4.2.5 小练习与答案

**练习 1**：`git clone` 时忘了加 `--recursive`，构建会怎样？

> **答案**：`3rdparty/tvm` 子模块为空。[CMakeLists.txt:157-179](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L157-L179) 会尝试自动 `git submodule update --init --recursive`；若 git 不可用或失败，则 `FATAL_ERROR`。补救方法是手动 `git submodule update --init --recursive`。

**练习 2**：为什么 `[tool.scikit-build.wheel.packages]` 要把 `3rdparty/tvm` 映射进 wheel？

> **答案**：运行期 `import tilelang` 后需要 `import tvm`，且要加载 TVM 的共享库。把 `3rdparty/tvm` 打进 wheel 的 `tilelang/3rdparty/tvm`，安装后无论用户机器上有没有 TVM，tilelang 都能用自带的配套版本，避免版本错配。

---

### 4.3 tilelang.env：运行时环境变量中枢

> 这是本讲的核心最小模块之一。所有「tilelang 运行时读哪个环境变量」「`.so` 从哪找」「缓存开不开」几乎都汇聚到这里。

#### 4.3.1 概念说明

`tilelang/env.py` 解决的问题是：tilelang 是一个行为高度可配置的系统（编译目标、缓存目录、调试开关、CUDA/ROCm 路径……），如果到处散落 `os.environ.get(...)`，会很难维护和发现。所以 tilelang 设计了两个抽象：

- **`EnvVar` 描述符**：把「环境变量名 + 默认值 + 是否被强制覆盖」封装成描述符，集中定义、动态读取（改 `os.environ` 立即生效）、可被测试强制覆盖。
- **`Environment` 类**：把所有配置项组织成一个对象，并附带一簇「智能默认」方法（根据 CUDA_HOME 算 DLL 搜索目录、解析 target 配置、判断是否处于轻量导入模式等）。

模块底部还会初始化第三方路径（TVM/CUTLASS/Composable Kernel/模板路径）和库搜索路径 `TL_LIBS`——这正是 `libinfo.py` 能找到 `libtilelang.so` 的前提（见 4.4）。

#### 4.3.2 核心流程

`import tilelang` 时 `env.py` 的关键执行顺序：

```
1. 计算 TL_ROOT = tilelang 包所在目录
2. 判断是「安装版」还是「开发版」(DEV)
   ├─ 安装版: TL_LIBS = [TL_ROOT/lib]         (.so 装在包的 lib 子目录)
   └─ 开发版: TL_LIBS = [build/lib, build/tvm] (.so 在仓库 build 目录)
3. 把 TL_LIBS 注入 sys.path 与 Windows %PATH%
4. _find_cuda_home() / _find_rocm_home() 探测 SDK 路径
5. 实例化 Environment（EnvVar 描述符此时只是定义，读取时才查 os.environ）
6. 初始化第三方路径: TVM python 路径、CUTLASS、Composable Kernel、模板路径
7. 导出静态变量: CUDA_HOME / ROCM_HOME / CUTLASS_INCLUDE_DIR ...
```

`TL_LIBS` 是整条加载链的「源头」：它决定了原生库的搜索根目录，`libinfo.py` 直接消费它。dev 模式与安装模式的判定可以用一个存在性检查概括：

\[
\text{DEV} = \neg\; \text{exists}(\text{TL\_ROOT}/3\text{rdparty})
\]

即包目录旁有没有 `3rdparty/` 子目录。

#### 4.3.3 源码精读

**TL_ROOT 与 TL_LIBS 的定义**，见 [tilelang/env.py:47-51](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L47-L51)：定义包根 `TL_ROOT` 和默认库目录 `TL_LIBS = [TL_ROOT/lib]`，并过滤掉不存在的目录。

**开发版 vs 安装版的分叉**，见 [tilelang/env.py:53-75](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L53-L75)：

```python
DEV = False
THIRD_PARTY_ROOT = os.path.join(TL_ROOT, "3rdparty")
if not os.path.exists(THIRD_PARTY_ROOT):
    DEV = True
    dev_lib_root = os.path.join(tl_dev_root, "build")
    TL_LIBS = [os.path.join(dev_lib_root, "lib"), os.path.join(dev_lib_root, "tvm")]
    logger.warning(f"Loading tilelang libs from dev root: {dev_lib_root}")
else:
    try:
        import z3  # noqa: F401
    except ImportError:
        logger.error("Failed to import z3, consider to reinstall tilelang.")
```

这是核心判断：如果包目录下没有 `3rdparty` 子目录（说明不是从 wheel 装的，而是直接跑源码树），就置 `DEV=True`，并把 `TL_LIBS` 改成 `[build/lib, build/tvm]`——即从仓库的 `build` 目录找 `.so`，并打印 `Loading tilelang libs from dev root` 警告。这就是 4.2.4 / 手册 [docs/get_started/Installation.md:373-380](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/Installation.md#L373-L380) 里「开发模式日志」的来源。安装版（有 `3rdparty`）则会尝试 `import z3` 验证依赖（[env.py:66-69](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L66-L69)），缺失只打 `error` 日志不抛异常。最后 [env.py:73-75](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L73-L75) 把 `TL_LIBS` 插入 `sys.path`。

**CUDA 主目录探测（多级回退）**，见 [tilelang/env.py:141-190](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L141-L190)。`_find_cuda_home()` 依次尝试：① 环境变量 `CUDA_HOME`/`CUDA_PATH`；② `which nvcc` 反推；③ pip 包 `nvidia-cuda-nvcc` 的安装位置（仅 ≥13.0 有效）；④ 平台默认路径（Windows 的 `C:/Program Files/...`、Linux 的 `/usr/local/cuda`）。这套多级回退保证了「无论 CUDA 装在哪都能找到」——它在运行期需要调用 `nvcc`/`ptxas` 做 JIT 编译时被用到。

**EnvVar 描述符**，见 [tilelang/env.py:229-309](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L229-L309)。读（`__get__`）时优先返回 `_forced_value`（测试/调试用的强制覆盖），否则查 `os.environ`，最后用默认值——每次读取都动态查，所以改环境变量立即生效。写（`__set__`）只存 `_forced_value`，不污染真实 `os.environ`（除非你取消注释那行）。

**Environment 类与缓存控制**，见 [tilelang/env.py:332-525](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L332-L525)。几个与本讲相关的环境变量：

- `TILELANG_CACHE_DIR`（[env.py:356](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L356)，默认 `~/.tilelang/cache`）：编译缓存目录，对应手册 [Installation.md:339-341](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/Installation.md#L339-L341) 提到的默认缓存路径。
- `TILELANG_DISABLE_CACHE`（[env.py:361-363](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L361-L363)）：高优先级关掉缓存，常用于单测/调试。
- `TILELANG_DEFAULT_TARGET`（[env.py:396](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L396)，默认 `"auto"`）：默认编译目标。
- `SKIP_LOADING_TILELANG_SO`（[env.py:401](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L401)）：跳过加载原生库（`__init__.py` 会读它）。

缓存全局开关由 `CacheState` 类管理（[env.py:208-227](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L208-L227)），`Environment.is_cache_enabled()` 把「环境变量级禁用」和「运行时级禁用」两者合并判断（[env.py:418-419](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L418-L419)）。模块末尾 [env.py:528-530](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L528-L530) 导出顶层函数 `enable_cache` / `disable_cache` / `is_cache_enabled`，这就是 `tilelang.enable_cache()` 这类公共 API 的真正出处。

**第三方路径初始化（运行时）**，见 [tilelang/env.py:597-632](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L597-L632)。导入时设置 TVM Python 路径（[env.py:598-605](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L598-L605) 把 `3rdparty/tvm/python` 加进 `sys.path`，这样 `import tvm` 用的是配套版本）、CUTLASS 头文件路径（`TL_CUTLASS_PATH`）、Composable Kernel 头文件路径（[env.py:619-624](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L619-L624)）、模板路径（[env.py:626-632](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L626-L632)）。找不到时只打 warning 不报错，保证「即使部分依赖缺失也能继续 import」。

> 关于完整运行时环境变量清单，官方手册 [Installation.md:328-330](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/Installation.md#L328-L330) 也明确指引「请参考 `env.py`」——本节即是对该文件的解读。

#### 4.3.4 代码实践

**实践目标**：用 Python 交互式验证 `EnvVar` 的「动态读取」特性。

**操作步骤**（示例代码，非项目原有文件）：

```python
# 示例代码：观察 EnvVar 动态读取
import os
import tilelang  # 若已安装
from tilelang.env import env

# 1. 读当前值（默认）
print("default target =", env.TILELANG_DEFAULT_TARGET)

# 2. 运行时改环境变量，不重启进程
os.environ["TILELANG_DEFAULT_TARGET"] = "cuda"
print("after env set  =", env.TILELANG_DEFAULT_TARGET)

# 3. 用强制覆盖（仅当前进程生效，不污染 os.environ）
env.TILELANG_DEFAULT_TARGET = "hip"
print("after force    =", env.TILELANG_DEFAULT_TARGET)
print("os.environ still =", os.environ.get("TILELANG_DEFAULT_TARGET"))
```

**需要观察的现象**：第 2 步打印 `cuda`（说明改 `os.environ` 立即生效，无需重新 import）；第 3 步打印 `hip`，而最后一行仍是 `cuda`（说明强制覆盖不写回真实环境变量）。

**预期结果**：三行输出依次为 `auto`、`cuda`、`hip`，且 `os.environ` 未被第 3 步污染。

**待本地验证**：在已安装 tilelang 的环境里跑这段代码并记录输出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `import tilelang` 时可能看到 `Failed to import z3` 的 error 日志？这意味着安装失败了吗？

> **答案**：不是安装失败。`env.py` 在「安装版」分支里会尝试 `import z3`（[env.py:66-69](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L66-L69)）做健康检查，z3 缺失只影响依赖符号推理的功能，且日志级别是 `error` 但不会抛异常。正确做法是按提示重装：`pip install z3-solver`。

**练习 2**：开发模式下 `import tilelang` 打印的 `Loading tilelang libs from dev root: .../build` 是哪段代码产生的？为什么 `TL_LIBS` 会变成 `build/lib`？

> **答案**：由 [env.py:64](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L64) 的 `logger.warning` 产生。因为开发模式下包目录里没有 `3rdparty`，触发 [env.py:55-62](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L55-L62) 的 `DEV=True` 分支，把 `TL_LIBS` 指向仓库 `build/lib` 和 `build/tvm`——也就是 CMake 产物所在位置。

---

### 4.4 tilelang.libinfo：定位原生库 libtilelang.so

> 这是本讲第二个核心最小模块。它回答「`import tilelang` 时，那个 C++ 编译器共享库到底从哪个路径被加载」。它消费 4.3 里产生的 `TL_LIBS`。

#### 4.4.1 概念说明

tilelang 的 Python 层本身不能编译 kernel——真正干活的是 C++ 编译出的共享库（Linux: `libtilelang.so`，macOS: `libtilelang.dylib`，Windows: 合并进 `tvm_compiler.dll`）。`libinfo.py` 的职责就是：**给定库名（如 `"tilelang"`），跨平台拼出正确的文件名，并在一组候选目录里找到它，返回绝对路径。**

它非常短（不到 50 行），却是连接 Python 与 C++ 引擎的「最后一公里」。它和 `env.py` 的分工是：**`env.py` 给「目录候选」（`TL_LIBS`），`libinfo.py` 给「文件名匹配」（`find_lib_path`）。**

#### 4.4.2 核心流程

`find_lib_path("tilelang")` 的查找逻辑：

```
输入 name="tilelang"
   │
   ├─ 根据平台拼候选文件名:
   │    Linux:   ["libtilelang.so"]
   │    Windows: ["tvm_compiler.dll"]   ← 特例！
   │    macOS:   ["libtilelang.dylib"]
   │
   ├─ 遍历候选目录 TL_LIBS (来自 env.py)
   │    安装版: [tilelang包/lib]
   │    开发版: [build/lib, build/tvm]
   │
   ├─ 在每个目录下检查候选文件是否存在且是普通文件
   │
   └─ 找到 → 返回绝对路径
      找不到 → 抛 RuntimeError，列出所有尝试过的候选
```

库定位的候选集可以用一个笛卡尔积概括：

\[
\text{candidates} = \{\, d \in \text{TL\_LIBS} \;\mid\; \text{isdir}(d) \,\} \times \{\text{平台对应的库文件名}\}
\]

即在「存在的目录」与「平台对应的文件名」做笛卡尔积，命中第一个就返回。

`get_dll_directories()` 则是把 `TL_LIBS` 与 Windows 上的 CUDA DLL 目录合并，供 `__init__.py` 在 Windows 上注册 DLL 搜索路径。

#### 4.4.3 源码精读

**get_dll_directories：合并库目录**，见 [tilelang/libinfo.py:8-10](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/libinfo.py#L8-L10)：把 `env.TL_LIBS`（原生库根）和 `get_cuda_dll_search_dirs()`（Windows 上 CUDA DLL 子目录，由 [env.py:538-555](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L538-L555) 产生）合并，过滤掉非目录，返回绝对路径列表。

**find_lib_path：跨平台库定位器**，见 [tilelang/libinfo.py:13-47](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/libinfo.py#L13-L47)。平台分派在 [tilelang/libinfo.py:26-38](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/libinfo.py#L26-L38)：

```python
elif sys.platform.startswith("linux") or sys.platform.startswith("freebsd"):
    lib_names = [f"lib{name}.so"]
elif sys.platform.startswith("win32"):
    if name == "tilelang":
        # Windows 把 tilelang 原生注册对象链接进了 tvm_compiler.dll
        lib_names = ["tvm_compiler.dll"]
    else:
        lib_names = [f"{name}.dll"]
elif sys.platform.startswith("darwin"):
    lib_names = [f"lib{name}.dylib"]
```

注意 Windows 特例：当 `name == "tilelang"` 时找的是 `tvm_compiler.dll`（因为 Windows 构建把 tilelang 的目标代码合并进了 `tvm_compiler.dll`，见 [CMakeLists.txt:549-557](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L549-L557) 的注释），没有单独的 `tilelang.dll`。

查找循环在 [tilelang/libinfo.py:40-47](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/libinfo.py#L40-L47)：双层循环——外层遍历 `TL_LIBS`（候选根目录），内层遍历候选文件名，命中即返回；全部 miss 则抛 `RuntimeError`，并把所有候选路径打印出来（这就是你看到 `Cannot find libraries: ... List of candidates: ...` 报错时的来源）。

**调用方：__init__.py 如何加载库**，见 [tilelang/__init__.py:183-190](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L183-L190)：

```python
def _load_tile_lang_lib():
    lib_path = libinfo.find_lib_path("tilelang")
    return ctypes.CDLL(lib_path), lib_path

# only load once here
if env.SKIP_LOADING_TILELANG_SO == "0":
    _LIB, _LIB_PATH = _load_tile_lang_lib()
```

`_load_tile_lang_lib()` 调用 `libinfo.find_lib_path("tilelang")` 拿到路径，再用 `ctypes.CDLL(lib_path)` 加载。注意有开关 `SKIP_LOADING_TILELANG_SO`：只有它为 `"0"`（默认）时才加载。加载发生在 `_lazy_load_lib()` 上下文里（[tilelang/__init__.py:133-155](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L133-L155)），该上下文会预加载 torch 并设置 `RTLD_LAZY` 以避免 dlopen 顺序问题。加载后，C++ 侧通过 tvm-ffi 注册的 `tl.*` 全局函数就可被 Python 调用（注册入口 [tilelang/_ffi_api.py:6](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/_ffi_api.py#L6) 的 `tvm_ffi.init_ffi_api("tl", __name__)`）。

**轻量导入模式（不加载库的捷径）**：[tilelang/__init__.py:100-106](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L100-L106) 与 [tilelang/__init__.py:158-191](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L158-L191) 体现了「轻量导入」：当 `env.is_light_import()` 为真（如运行 `python -m tilelang.autodd`）时，跳过 logger 初始化、跳过加载 `.so`、跳过一堆重型 import，让 CLI 能快速启动。

**版本号的来源**，见 [tilelang/__init__.py:10-44](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L10-L44)：源码检出时（仓库根有 `VERSION` 文件）调用 `version_provider.dynamic_metadata("version")` 动态拼出带 git hash 的版本；pip 安装时走 `importlib.metadata.version("tilelang")` 读 wheel 打包时写死的 metadata。所以同一个 `print(tilelang.__version__)`，源码检出会带 git hash（如 `0.1.12+cuda.gita1b2c3d4`），pip 安装则读定型 metadata。

#### 4.4.4 代码实践

**实践目标**：亲手调用 `libinfo.find_lib_path`，记录你的安装里 `libtilelang.so` 的真实路径，并理解候选目录构造（本讲主任务）。

**操作步骤**（示例代码，非项目原有文件）：

```python
# 示例代码：观察 libinfo 如何定位原生库
import tilelang  # 触发完整导入与原生库加载
from tilelang import env as tenv
from tilelang import libinfo

print("TL_LIBS 候选目录     =", tenv.TL_LIBS)
print("DEV 模式?            =", tenv.DEV)
print("find_lib_path 结果   =", libinfo.find_lib_path("tilelang"))
```

**需要观察的现象**：

- 安装版：`TL_LIBS` 形如 `['/.../site-packages/tilelang/lib']`，`DEV` 为 `False`，`find_lib_path` 返回 `.../tilelang/lib/libtilelang.so`。
- 开发版：`TL_LIBS` 形如 `['/.../tilelang/build/lib', '/.../tilelang/build/tvm']`，`DEV` 为 `True`，且导入时会先打印 `Loading tilelang libs from dev root` 警告；返回 `.../build/lib/libtilelang.so`。

**预期结果**：打印出的路径与 `TL_LIBS` 中的某个根目录 + 平台库名拼接一致；Windows 上返回的是 `tvm_compiler.dll`。

**进一步（可选）**：用 `SKIP_LOADING_TILELANG_SO=1` 跑 `python -c "import tilelang; print('ok')"`，观察 `import` 仍能成功（跳过了 `.so` 加载），但后续真正编译 kernel 会失败——验证了 `.so` 加载是「按需」而非 `import` 必需。

**待本地验证**：记录你机器上的实际路径字符串，它将帮助你后续调试「找不到库」类报错。

#### 4.4.5 小练习与答案

**练习 1**：在 Windows 上 `find_lib_path("tilelang")` 找的是哪个文件？为什么不是 `tilelang.dll`？

> **答案**：找的是 `tvm_compiler.dll`。因为 [libinfo.py:29-32](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/libinfo.py#L29-L32) 对 `name == "tilelang"` 做了特例处理，原因是 Windows 构建把 tilelang 的原生目标代码合并进了 `tvm_compiler.dll`（见 [CMakeLists.txt:549-557](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L549-L557)），没有单独的 `tilelang.dll`。

**练习 2**：如果你看到报错 `Cannot find libraries: libtilelang.so ... List of candidates: ...`，最可能的两个原因是什么？分别该怎么修？

> **答案**：① wheel 安装不完整或被破坏，导致 `tilelang/lib/libtilelang.so` 缺失——重装 `pip install --force-reinstall tilelang`。② 处于开发模式但还没编译 C++，即 `build/lib/libtilelang.so` 不存在——按手册跑一次 `cmake .. -DUSE_CUDA=ON -G Ninja && ninja`（或 `pip install -e . -v`）。`List of candidates` 里列出的就是 `TL_LIBS`，据此可判断当前是安装版还是开发版路径。

**练习 3**：`tilelang.__version__` 在「源码检出」和「pip 安装」下分别从哪来？

> **答案**：源码检出时，`__init__.py` 发现仓库根有 `VERSION` 文件，调用 `version_provider.dynamic_metadata("version")` 动态拼出带 git hash 的版本；pip 安装时没有 `VERSION` 文件（在包外），走 `importlib.metadata.version("tilelang")` 读 wheel 打包时写死的 metadata（见 [tilelang/__init__.py:10-44](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L10-L44)）。

---

## 5. 综合实践

**综合任务**：用一篇「安装诊断报告」把本讲四个模块串起来。假设你帮同事排查「机器 A 上 `pip install tilelang` 跑得好好的，把代码拷到机器 B 却 `import` 失败」的问题，按以下步骤产出报告：

1. **看版本号推断构建**：在机器 A 上记录 `tilelang.__version__` 的完整字符串（含 `+...` 后缀）。根据 [version_provider.py:44-96](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/version_provider.py#L44-L96) 解释后缀含义：是 `cu130`？`rocm`？`cpu`？还是 `cuda`（无具体版本）？这决定了该 wheel 期望的运行时。
2. **看库定位**：在机器 A 上运行 4.4.4 的示例脚本，记录 `TL_LIBS`、`DEV`、`find_lib_path("tilelang")` 的输出，确认 `.so` 真实落在磁盘上的位置。
3. **看依赖目录**：用 `ls` 查看 `tilelang` 安装目录下的 `lib/`、`3rdparty/tvm/python`、`3rdparty/cutlass/include` 是否齐全（对应 [pyproject.toml:153-169](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L153-L169) 的映射）。
4. **复现 B 的失败**：在机器 B 上同样跑该脚本。如果报 `Cannot find libraries`，对照 `TL_LIBS` 看是缺文件还是路径错（4.4）；如果 import 成功但跑 kernel 失败，检查 `env.CUDA_HOME`（参考 [tilelang/env.py:141-190](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L141-L190) 的四级探测）是否为空——很可能 B 没装 CUDA Toolkit，导致运行期 JIT 找不到 `nvcc`。
5. **给出结论**：写一句话诊断，例如「A 装的是 `cu130` wheel，B 没有 CUDA 13 运行时，故 import 后 JIT 失败」。

**核心收获**：区分「能 import」（靠 stub 库）和「能编译运行 kernel」（靠真实 CUDA/ROCm Toolkit）。本环境无法实际部署，上述步骤的运行输出「待本地验证」。

## 6. 本讲小结

- tilelang 提供三条安装路径：`pip install tilelang`（最简）、源码 `pip install . -v`（最灵活）、nightly（最新），三者最终都用 `python -c "import tilelang; print(tilelang.__version__)"` 验证。
- 源码构建由 scikit-build-core 驱动 CMake，需要 TVM 子模块（`--recursive`）、CUTLASS、Composable Kernel；后端由 `USE_CUDA` / `USE_ROCM` / `USE_METAL` / `USE_LLVM` 控制，遵循「显式优先、否则平台默认」。
- wheel 通过 `[tool.scikit-build.wheel.packages]` 把 `3rdparty/tvm` 等映射进 `tilelang/3rdparty`，保证安装后自带 TVM；stub 库让 wheel 能在无 CUDA 的机器上被 import（懒加载）。
- **`tilelang.env`** 是运行时配置中枢：`EnvVar` 描述符集中管理环境变量并动态读取，`TL_LIBS` 决定原生库搜索根，安装版用 `TL_ROOT/lib`、开发版用 `build/lib`。
- **`tilelang.libinfo`** 的 `find_lib_path` 跨平台拼出库名（Linux `libtilelang.so`、Windows 特例 `tvm_compiler.dll`）并在 `TL_LIBS` 中查找；`__init__.py` 用它加载 `.so`，且支持「轻量导入」跳过加载。
- `tilelang.__version__` 由 `version_provider.py` 动态生成，会带上后端标签和 git hash（如 `0.1.12+cu130.git0d4a74be`），可用 `NO_VERSION_LABEL` 关闭；pip 安装版则读 `importlib.metadata`。

## 7. 下一步学习建议

本讲解决了「装好、能 import」的问题。下一讲 **u1-l3 仓库目录结构与包入口** 将带你深入 `tilelang/__init__.py` 的完整导入流程（不只是版本和加载库，还包括 `jit` / `compile` / `language` / `Profiler` 等公共 API 的导出位置），以及 Python 侧子包与 C++ 侧 `src/` 子系统的一一对应关系。

建议你在进入下一讲前，先把本讲的「综合实践」跑一遍——亲手打印一次 `TL_LIBS` 和 `find_lib_path` 的返回值，会让后续阅读包入口代码时事半功倍。如果想提前感受「装好之后能干什么」，可以浏览 [examples/quickstart.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py)，那是下一单元 u1-l4 会实跑的第一个 kernel。
