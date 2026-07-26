# 安装、构建与运行环境

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `pip` 安装 tilelang，并用 `python -c "import tilelang; print(tilelang.__version__)"` 验证安装是否成功。
- 说清楚 tilelang 的三种获取方式（PyPI / 源码构建 / nightly）各自适用场景。
- 理解从源码构建时，TVM 子模块、CUTLASS、Composable Kernel（CK）三者的角色，以及 `USE_CUDA / USE_ROCM / USE_LLVM` 等 CMake 选项的含义。
- 知道 `pyproject.toml` 如何用 `scikit-build-core` 把 C++ 工程打包成 wheel，并把 `3rdparty` 里的 TVM/CUTLASS/CK 映射进安装目录。
- **重点**：讲清楚运行期 `import tilelang` 时，Python 是怎么在文件系统里定位到原生库 `libtilelang.so` 的——也就是 `tilelang.libinfo` 与 `tilelang.env` 这两个最小模块。

本讲承接 [u1-l1 项目总览](./u1-l1-project-overview.md)：上一讲建立了「Python DSL → TIR → Pass → 设备代码 → Kernel Adapter」的全景认知，本讲解决「这套东西先得装得上、跑得起来」。

## 2. 前置知识

在动手之前，先用大白话理解几个概念：

- **原生库（native library / shared library）**：tilelang 不只是纯 Python。它的编译器核心（Pass、代码生成器等）是用 C++ 写的，编译后会得到一个动态库，Linux 上叫 `libtilelang.so`，macOS 上叫 `libtilelang.dylib`，Windows 上则被打进了 `tvm_compiler.dll`。Python 侧通过 `ctypes` 把这个库加载进进程，才能调用里面的 C++ 函数。
- **TVM**：tilelang 建立在 TVM 之上，把 TVM 当作「TIR 中间表示 + 一套基础设施」来用。仓库 `3rdparty/tvm` 里是一个定制版 TVM，源码构建时会和 tilelang 一起编译。
- **CUTLASS / Composable Kernel（CK）**：分别是 NVIDIA 与 AMD 官方的高性能矩阵运算模板库。tilelang 在生成 GEMM 等算子的设备代码时，会 include 这些库的头文件，所以它们的 include 路径在构建期和运行期都要能被找到。
- **wheel**：Python 的二进制安装包格式（`.whl`）。因为 tilelang 含 C++ 代码，所以它的 wheel 不只是 `.py` 文件，里面还打包了编译好的 `.so`/`.dll`。
- **scikit-build-core**：一个把 CMake 工程接入 Python 打包（PEP 517）的工具。tilelang 用它来「先跑 CMake 编译 C++，再把产物打成 wheel」。
- **环境变量**：tilelang 大量使用环境变量来配置路径与行为，本讲会涉及 `CUDA_HOME`、`TVM_LIBRARY_PATH`、`TL_CUTLASS_PATH` 等，它们都集中在 `tilelang/env.py`。

如果你对「Python 如何加载一个 C 动态库」完全陌生，记住一句话即可：**Python 进程启动后，需要知道 `.so` 文件在磁盘上的绝对路径，然后用系统调用把它映射进进程地址空间，之后才能调用里面的函数。** 本讲很大一部分内容就是在回答「路径从哪来」。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [docs/get_started/Installation.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/Installation.md) | 官方安装文档，列出三种安装方式与构建期/运行期环境变量 |
| [pyproject.toml](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e18f2cfd9c218d81d/pyproject.toml) | 项目元信息、运行期依赖、`scikit-build-core` 打包配置、`cibuildwheel` CI 构建配置 |
| [CMakeLists.txt](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt) | C++ 工程的构建脚本：后端开关、加载 TVM、产出 `libtilelang.so`、Cython 封装 |
| [tilelang/env.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py) | 运行期环境的大管家：定位库目录 `TL_LIBS`、第三方头文件路径、`CUDA_HOME/ROCM_HOME`、缓存开关 |
| [tilelang/libinfo.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/libinfo.py) | 在 `TL_LIBS` 候选目录里查找具体的 `.so`/`.dll` 文件名并返回绝对路径 |
| [tilelang/__init__.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py) | 包入口：计算 `__version__`、加载原生库、触发轻量/完整导入分支 |
| [version_provider.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/version_provider.py) | 打包期动态生成版本号（拼接后端标签 + git hash） |
| [cmake/load_tvm.cmake](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/cmake/load_tvm.cmake) | 确定用哪个 TVM 源码（自带子模块或 `TVM_ROOT` 指向的外部 TVM） |

> 说明：上面带 `tile-ai/tilelang/blob/<HEAD>/...` 的链接都是指向当前 HEAD `c6294f07` 的永久链接，点击可直接跳转到对应文件。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. **4.1 三种安装方式与运行期依赖**——怎么装、装进来哪些 Python 依赖。
2. **4.2 wheel 是怎么打包出来的**——`scikit-build-core` 如何把 C++ 产物和 `3rdparty` 一起塞进 `.whl`。
3. **4.3 从源码构建**——`CMakeLists.txt` 如何编译 C++、加载 TVM 子模块、选择后端。
4. **4.4 运行期库定位**（核心）——`import tilelang` 时如何找到 `libtilelang.so`。

### 4.1 三种安装方式与运行期依赖

#### 4.1.1 概念说明

tilelang 提供三种获取方式，适用人群不同：

| 方式 | 命令 | 适用场景 |
| --- | --- | --- |
| PyPI 稳定版 | `pip install tilelang` | 大多数用户；预编译 wheel，开箱即用 |
| nightly 版 | `pip install tilelang -f https://tile-ai.github.io/whl/nightly` | 想用最新特性、尚未发版的修复 |
| 源码构建 | `git clone --recursive ... && pip install . -v` | 需要改 C++/Python 源码、或目标机器没有预编译 wheel |

另外还有「Docker 镜像」「外部 TVM」等变体，本质都是源码构建的封装或特例。

理解这三种方式的关键在于：**预编译 wheel 已经把 `libtilelang.so` 和 `3rdparty` 打包好了**，所以安装后无需编译；而源码构建则要在本机跑 CMake 把 C++ 编出来。

#### 4.1.2 核心流程

安装后验证的正确姿势只有一行：

```bash
python -c "import tilelang; print(tilelang.__version__)"
```

这一行其实触发了两件事：

1. **导入 Python 包**：解析 `tilelang` 包，执行 `tilelang/__init__.py`，过程中加载原生库（详见 4.4）。
2. **打印版本号**：`__version__` 的来源取决于「源码检出」还是「已安装的 wheel」（详见 4.4.1）。

tilelang 的运行期 Python 依赖在 [pyproject.toml](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml) 里声明，其中几条值得记住：

- `apache-tvm-ffi`：tvm-ffi 的 Python 绑定，tilelang 的 FFI 调用基于它。
- `torch`：tilelang 把 PyTorch tensor 作为主要的输入输出载体。
- `z3-solver`：Z3 SMT 求解器，tilelang 用它做符号推理（如索引边界分析）。
- `ml-dtypes`：提供 `float8` 等低精度 dtype。
- `numpy`、`psutil`、`cloudpickle`、`tqdm`、`typing-extensions`：常用工具库。

#### 4.1.3 源码精读

Python 版本与项目定位写在 [pyproject.toml:1-9](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L1-L9)：

```toml
name = "tilelang"
description = "A tile level programming language to generate high performance code."
requires-python = ">=3.10"
```

注意 `requires-python = ">=3.10"`——这是 tilelang 支持的最低 Python 版本，对应 classifiers 里列出的 3.10 ~ 3.14。

运行期依赖列表见 [pyproject.toml:29-45](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L29-L45)（节选）：

```toml
dependencies = [
    "apache-tvm-ffi>=0.1.11,<0.1.12",
    "torch-c-dlpack-ext; python_version < '3.14'",
    "cloudpickle",
    "ml-dtypes",
    "numpy>=1.23.5",
    "psutil",
    "torch",
    ...
    "z3-solver>=4.13.0,<4.15.5",
]
```

注意几条依赖带了**版本上界**（如 `apache-tvm-ffi>=0.1.11,<0.1.12`、`z3-solver>=4.13.0,<4.15.5`）。这是因为这些库的 ABI/行为变化较快，tilelang 需要把它钉在经过验证的区间内。如果安装时报依赖冲突，多半是这些上界在起作用。

官方文档里的安装命令见 [docs/get_started/Installation.md:11-33](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/Installation.md#L11-L33)，其中验证步骤正是上面那一行 `import`。

#### 4.1.4 代码实践

1. **实践目标**：用 pip 安装 tilelang 并确认能导入、能拿到版本号。
2. **操作步骤**：
   - 创建一个干净的虚拟环境（推荐 Python ≥ 3.10）。
   - 执行 `pip install tilelang`。
   - 执行 `python -c "import tilelang; print(tilelang.__version__)"`。
3. **需要观察的现象**：终端打印一串版本号，例如形如 `0.1.12+cuda.gitxxxxxxxx`（具体后缀取决于你装的 wheel 是哪个后端构建的，详见 4.2）。
4. **预期结果**：命令退出码为 0，且无 `ImportError` / `Cannot find libraries` 报错。
5. 若手头没有 GPU 也无妨——只要 wheel 里带了 stub 库，`import` 本身不需要真实 GPU；但真正编译/运行 kernel 时仍需对应运行时（CUDA/ROCm）。

> 本环境无 GPU 与网络，无法替你执行安装，具体输出「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pip install tilelang` 之后通常不需要再装 CUDA Toolkit 也能 `import` 成功？

> **答案**：因为官方 wheel 用了 CUDA/HIP 的 **stub 库**（见 4.3.3 的 `TILELANG_USE_CUDA_STUBS` / `TILELANG_USE_HIP_STUBS`）。stub 库在导入时只满足符号链接关系，真正的 `libcuda.so.1` 等运行时是在调用时 lazy-load（dlopen）的。所以「能 import」不等于「能跑 kernel」。

**练习 2**：`apache-tvm-ffi` 为什么被钉在 `<0.1.12`？

> **答案**：tvm-ffi 是 tilelang FFI 的基础，小版本之间可能改 ABI 或调用约定；钉死上界可以避免上游突然发新版把已验证的集成破坏掉。遇到冲突时以 `requirements.txt`/`pyproject.toml` 声明的区间为准。

### 4.2 wheel 是怎么打包出来的：scikit-build-core 与 3rdparty 映射

#### 4.2.1 概念说明

tilelang 是「C++ + Python」混合工程。纯 Python 包用 `setuptools` 打包即可，但有 C++ 的包需要：**先编译 C++，再把编译产物连同 Python 文件一起塞进 wheel**。`scikit-build-core` 就是干这个的——它把 CMake 作为构建后端，在打包时自动跑一遍 CMake，收集产物。

理解 wheel 的内部结构很重要，因为运行期「去哪找 `.so`」直接由 wheel 里文件的摆放位置决定（见 4.4）。简单说：

- Python 代码放在 `tilelang/`。
- 编译出的原生库放在 `tilelang/lib/`。
- 第三方头文件（TVM/CUTLASS/CK）放在 `tilelang/3rdparty/`，因为运行期生成 kernel 时还要 include 它们。

#### 4.2.2 核心流程

打包流程可以概括为：

```text
pip install / python -m build
        │
        ▼
scikit-build-core 启动（PEP 517 build backend）
        │
        ├── 读 [tool.scikit-build] 配置
        ├── 调用 CMake: cmake -S . -B build  →  生成 build 系统
        ├── 调用编译: cmake --build build    →  产出 libtilelang.so / cython 包装
        ├── 按 [tool.scikit-build.wheel.packages] 收集文件
        │     · tilelang/        → wheel/tilelang/
        │     · 3rdparty/tvm/... → wheel/tilelang/3rdparty/...
        │     · build/lib/*.so   → wheel/tilelang/lib/*.so
        └── 打成 .whl
```

版本号是「动态生成」的：`scikit-build-core` 会在打包期调用 `version_provider.py` 的 `dynamic_metadata("version")`，把后端标签和 git hash 拼到基础版本后面。

#### 4.2.3 源码精读

构建后端声明见 [pyproject.toml:75-93](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L75-L93)：

```toml
[build-system]
requires = ["cython>=3.1.0", "scikit-build-core", "z3-solver>=4.13.0,<4.15.5", ...]
build-backend = "scikit_build_core.build"

[tool.scikit-build]
wheel.py-api = "cp38"
cmake.version = ">=3.26.1"
build-dir = "build"
metadata.version.provider = "version_provider"
metadata.version.provider-path = "."
```

关键点：

- `build-backend = "scikit_build_core.build"` 指明用 scikit-build-core 打包。
- `cmake.version = ">=3.26.1"` 要求构建期有 CMake ≥ 3.26。
- `metadata.version.provider = "version_provider"` 把版本号交给同目录下的 `version_provider.py` 动态生成（对应 `dynamic_metadata` 函数）。

把 `3rdparty` 打进 wheel 的映射见 [pyproject.toml:153-169](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L153-L169)（节选）：

```toml
[tool.scikit-build.wheel.packages]
tilelang = "tilelang"
"tilelang/src" = "src"
# 把 3rdparty 的内容放进 wheel 内的 tilelang/3rdparty，运行期才能找到 TVM 共享库
"tilelang/3rdparty/tvm/src" = "3rdparty/tvm/src"
"tilelang/3rdparty/tvm/python" = "3rdparty/tvm/python"
"tilelang/3rdparty/tvm/include" = "3rdparty/tvm/include"
# CUTLASS
"tilelang/3rdparty/cutlass/include" = "3rdparty/cutlass/include"
# Composable Kernel
"tilelang/3rdparty/composable_kernel/include" = "3rdparty/composable_kernel/include"
```

注意左侧形如 `"tilelang/3rdparty/tvm/src"` 的键是「**wheel 内的目标路径**」，右侧 `"3rdparty/tvm/src"` 是「**源码仓库里的来源路径**」。也就是说，仓库根目录下的 `3rdparty/tvm/...` 会被安装到 wheel 里的 `tilelang/3rdparty/tvm/...`。这一步是运行期能找到 TVM/CUTLASS/CK 的前提。

CI 构建配置（cibuildwheel）见 [pyproject.toml:252-316](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L252-L316)，其中两条最关键：

```toml
[tool.cibuildwheel.linux]
environment.USE_CUDA = "ON"
environment.USE_ROCM = "ON"
```

这表示官方 Linux wheel 是「CUDA + ROCm 同时开启」的 **fat wheel**——同一个 wheel 既能跑 CUDA 也能跑 ROCm（ROCm 侧靠 vendored HIP 头文件 + dlopen stub 编译，运行期仍需真实 ROCm 运行时）。而 cibuildwheel 的导入自检命令正是本讲的验证一行：

```toml
test-command = ['python -c "import tilelang; print(tilelang.__version__)"']
```

见 [pyproject.toml:270-272](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L270-L272)。

版本号的动态拼接逻辑在 [version_provider.py:44-96](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/version_provider.py#L44-L96)，它读取 `USE_CUDA` / `USE_ROCM` / `CUDA_VERSION` 等环境变量决定后缀：

```python
elif _read_cmake_bool(os.environ.get("USE_ROCM", "")) and not _read_cmake_bool(os.environ.get("USE_CUDA", "")):
    backend = "rocm"
elif "USE_CUDA" in os.environ and not _read_cmake_bool(os.environ.get("USE_CUDA")):
    backend = "cpu"
else:  # cuda
    if cuda_version := os.environ.get("CUDA_VERSION"):
        major, minor, *_ = cuda_version.split(".")
        backend = f"cu{major}{minor}"
    else:
        backend = "cuda"
```

所以你会看到形如 `0.1.12+cu130.gita1b2c3d4` 这样的版本号——`cu130` 表示用 CUDA 13.0 构建，`gita1b2c3d4` 是 8 位 git hash。

#### 4.2.4 代码实践

1. **实践目标**：在不真的构建的前提下，通过阅读配置「预测」一个官方 Linux wheel 里会包含哪些目录。
2. **操作步骤**：
   - 读 [pyproject.toml:153-169](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L153-L169) 的 `wheel.packages` 映射。
   - 读 [pyproject.toml:117-143](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L117-L143) 的 sdist include 列表作对照。
   - 在纸上列出 wheel 内 `tilelang/` 下应出现的子目录：`lib/`、`3rdparty/tvm/...`、`3rdparty/cutlass/include`、`3rdparty/composable_kernel/include`、`src/` 等。
3. **需要观察的现象**：`lib/` 目录（放 `.so`）不在 `wheel.packages` 列表里——它是由 CMake 的 `install(TARGETS ... DESTINATION tilelang/lib)` 单独装进去的（见 4.3.3）。
4. **预期结果**：你能说出「Python 源码靠 `wheel.packages` 收，原生库靠 CMake `install` 收」这条分工。
5. 若本地已装好 tilelang，可用 `python -c "import tilelang, os; print(os.path.dirname(tilelang.__file__))"` 找到安装目录，再 `ls` 其下的 `lib/` 与 `3rdparty/` 验证（「待本地验证」）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `3rdparty/tvm/python` 要被打进 wheel？

> **答案**：tilelang 运行期需要 `import tvm`，而它用的是仓库自带的定制版 TVM 的 Python 绑定。把 `3rdparty/tvm/python` 装进 wheel 并在导入期把它加入 `sys.path`（见 4.4.3 的 `env.py`），就保证了「装的 tilelang 用的是配套版本的 TVM」，而不是系统里碰巧存在的另一个 tvm。

**练习 2**：版本号 `0.1.12+cuda.gitxxxxxxxx` 里 `+` 后面的部分叫什么？去掉它会发生什么？

> **答案**：`+` 后面是 **local version label**（PEP 440）。`version_provider.py` 用 `NO_VERSION_LABEL=ON` 可以关掉它；构建 sdist 时也会关掉，以保证 sdist 与 wheel 的版本字符串一致，避免 pip 抱怨版本不匹配（见 [version_provider.py:14-18](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/version_provider.py#L14-L18) 的注释）。

### 4.3 从源码构建：CMakeLists 与 TVM 子模块

#### 4.3.1 概念说明

当你需要修改 tilelang 的 C++ 源码，或者官方 wheel 不覆盖你的平台/后端时，就要从源码构建。源码构建的本质是：**让 CMake 调用编译器，把 `src/` 下的 C++ 和 `3rdparty/tvm` 一起编成 `libtilelang.so`**，再由 scikit-build-core 把它打包（或直接用 PYTHONPATH 引用）。

这里有几个关键角色：

- **TVM 子模块**：`3rdparty/tvm` 是定制版 TVM。`git clone --recursive` 会把它拉下来；构建时和 tilelang 一起编译。
- **后端开关 `USE_CUDA / USE_ROCM / USE_METAL / USE_LLVM`**：决定编译哪些硬件后端的代码生成器。
- **CUTLASS / CK**：作为头文件库被 include，构建期需要它们的 include 路径。
- **Z3**：tilelang 编译器用 Z3 做符号推理，构建期通过 PyPI 的 `z3-solver` 提供。

#### 4.3.2 核心流程

最小化的源码构建流程（官方「Working from Source via PYTHONPATH」路径）：

```bash
git clone --recursive https://github.com/tile-ai/tilelang.git
cd tilelang
mkdir -p build && cd build
cmake .. -DUSE_CUDA=ON        # 配置，生成 Makefile/Ninja
make -j                       # 编译，产出 build/lib/libtilelang.so
cd ..
export PYTHONPATH=$PWD:$PYTHONPATH
python -c "import tilelang; print(tilelang.__version__)"
```

CMake 阶段做的事：

```text
1. include(FindPipCUDAToolkit)        # 先找主机 CUDA，找不到回退 pip 包
2. project(TILE_LANG C CXX)
3. 定义后端开关 USE_CUDA/ROCM/METAL/LLVM
4. include(cmake/load_tvm.cmake)      # 定位 TVM 源码（自带或 TVM_ROOT）
5. include(TVM 的 config.cmake)
6. add_subdirectory(TVM)              # 把 TVM 编进来
7. add_library(tilelang SHARED ...)   # 编译 tilelang 自己的 C++，链接 tvm_compiler
8. Cython 化 cython_wrapper.pyx       # 生成 Python↔C++ 桥接
9. install(... DESTINATION tilelang/lib)
```

#### 4.3.3 源码精读

CMake 起点见 [CMakeLists.txt:4-18](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L4-L18)：

```cmake
cmake_minimum_required(VERSION 3.26)
# 必须在 project() 之前 include，以便设置 CMAKE_CUDA_COMPILER
include(${CMAKE_CURRENT_LIST_DIR}/cmake/FindPipCUDAToolkit.cmake)
project(TILE_LANG C CXX)
```

注意 `FindPipCUDAToolkit` 在 `project()` **之前** include——因为 CUDA 编译器必须在 project() 启用 CUDA 语言前确定好。这个模块的逻辑是「先找主机 CUDA Toolkit，找不到就回退到 pip 装的 `nvidia-cuda-nvcc` 包」，对应官方文档里「With pip-provided CUDA toolchain (no host CUDA required)」那条路径。

后端开关定义见 [CMakeLists.txt:253-296](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L253-L296)：

```cmake
set(TILELANG_BACKENDS CUDA ROCM METAL LLVM)
...
foreach(BACKEND IN LISTS TILELANG_BACKENDS)
  tilelang_define_backend_option(${BACKEND})
endforeach()
```

四个后端 `USE_CUDA / USE_ROCM / USE_METAL / USE_LLVM`，默认值由平台决定（macOS 默认开 Metal，有 CUDA Toolkit 时默认开 CUDA，否则不开）。

加载 TVM 见 [cmake/load_tvm.cmake:3-13](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/cmake/load_tvm.cmake#L3-L13)：

```cmake
set(TVM_BUILD_FROM_SOURCE TRUE)
set(TVM_SOURCE ${CMAKE_SOURCE_DIR}/3rdparty/tvm)

if(DEFINED ENV{TVM_ROOT})
  if(EXISTS $ENV{TVM_ROOT}/cmake/config.cmake)
    set(TVM_SOURCE $ENV{TVM_ROOT})
  endif()
endif()
```

默认用 `3rdparty/tvm`；若设置了 `TVM_ROOT` 环境变量且该目录有 `cmake/config.cmake`，则改用外部 TVM。这就是官方文档「Building with Customized TVM Path」的实现。

产出原生库见 [CMakeLists.txt:549-569](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L549-L569)：

```cmake
if(WIN32)
  # Windows 把 tilelang 的原生代码链接进 tvm_compiler.dll
  target_sources(tvm_compiler PRIVATE $<TARGET_OBJECTS:tilelang_objs>)
else()
  add_library(tilelang SHARED $<TARGET_OBJECTS:tilelang_objs>)
  target_link_libraries(tilelang PUBLIC tvm_compiler)
  set_target_properties(tilelang PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib"
    ...)
endif()
```

两个要点：

1. **POSIX**：编出独立的 `libtilelang.so`，放到 `build/lib/`。
2. **Windows**：不单独出 `tilelang.dll`，而是把 tilelang 的目标文件塞进 `tvm_compiler.dll`（因为 Windows DLL 有 65535 符号上限，且 tilelang 需要调用 TVM 未导出的内部符号）。这正是 4.4 里 `libinfo.py` 在 Windows 上要找 `tvm_compiler.dll` 的原因。

安装产物进 wheel 见 [CMakeLists.txt:713-718](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L713-L718)：

```cmake
install(
  TARGETS ${TILELANG_OUTPUT_TARGETS}
  LIBRARY DESTINATION tilelang/lib
  RUNTIME DESTINATION tilelang/lib
  ARCHIVE DESTINATION tilelang/lib)
```

无论走 `pip install` 还是 PYTHONPATH，原生库最终都落在 `tilelang/lib/`（或 dev 模式的 `build/lib/`）。

stub 库选项见 [CMakeLists.txt:300-324](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L300-L324)：

```cmake
option(TILELANG_USE_CUDA_STUBS
       "Use stub libraries (cuda/cudart/nvrtc) for portable wheels" ON)
...
option(TILELANG_USE_HIP_STUBS
       "Use POSIX dlopen-based HIP stub libraries (hip/hiprtc) for portable wheels" ON)
```

stub 库让 wheel 在「没有 CUDA/ROCm 运行时的机器上也能 import」，真实运行时按需 dlopen 加载——这是官方 wheel 能做到「开箱 import」的关键。

#### 4.3.4 代码实践

1. **实践目标**：用一个 CMake 选项改变构建行为，并预测它对 wheel 的影响。
2. **操作步骤**：
   - 假设你要构建一个「只支持 CPU、不带 CUDA」的 tilelang。阅读 [docs/get_started/Installation.md:303-309](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/get_started/Installation.md#L303-L309)。
   - 写出配置命令：`cmake .. -DUSE_CUDA=OFF -DUSE_LLVM=ON`。
   - 对照 [CMakeLists.txt:383-445](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L383-L445) 的自动后端选择逻辑，确认你显式传了 `USE_CUDA=OFF`，所以不会因为「检测到 CUDA Toolkit」而被自动打开。
3. **需要观察的现象**：构建产物里不会有 CUDA 相关 codegen；版本号后缀应变为 `+cpu.gitxxxxxxxx`（参考 [version_provider.py:68-69](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/version_provider.py#L68-L69)）。
4. **预期结果**：你能解释「显式传 `USE_*`」与「让 CMake 自动选」的区别：自动选只在用户**没有**显式指定任何后端时才生效。
5. 实际构建需本地有编译环境与源码，本环境无法执行，「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`git clone` 时忘了加 `--recursive`，构建会怎样？

> **答案**：`3rdparty/tvm` 子模块为空。CMakeLists.txt 在 [CMakeLists.txt:157-179](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L157-L179) 会尝试自动 `git submodule update --init --recursive`；若 git 不可用或失败，则直接 `FATAL_ERROR`。补救方法是手动 `git submodule update --init --recursive`。

**练习 2**：为什么 tilelang 默认把 `HIDE_PRIVATE_SYMBOLS` 设为 `OFF`？

> **答案**：见 [CMakeLists.txt:354-356](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L354-L356) 的注释：tilelang 作为独立共享库，会跨 DSO 调用/继承 TVM 的 C++ 内部符号；TVM 默认隐藏私有符号会让这些引用在 `libtilelang.so` 被 dlopen 时无法解析，所以必须关掉。

### 4.4 运行期库定位：tilelang.libinfo 与 tilelang.env（核心）

> 这是本讲最重要的模块。前面三节都在讲「怎么把 `.so` 装到磁盘上」，这一节讲「`import tilelang` 时，Python 怎么把它找出来加载」。涉及的两个最小模块就是 `tilelang.libinfo` 与 `tilelang.env`。

#### 4.4.1 概念说明

当你执行 `import tilelang`，Python 解释器会运行 `tilelang/__init__.py`。这个文件干了很多事，但其中和「装在哪」最相关的是：

1. **计算版本号** `__version__`——源码检出和已安装两种情况来源不同。
2. **确定「库搜索目录」** `TL_LIBS`——一个候选目录列表，原生库只会在这些目录里找。
3. **配置第三方路径**——TVM Python 路径、CUTLASS/CK 头文件路径、模板路径。
4. **真正加载** `libtilelang.so`——调用 `libinfo.find_lib_path("tilelang")` 拿到绝对路径，再 `ctypes.CDLL` 加载。

`tilelang.env` 负责 1~3（**去哪些目录找**），`tilelang.libinfo` 负责 4（**在这些目录里挑出具体文件**）。两者分工明确：env 给「目录候选」，libinfo 给「文件名匹配」。

一个关键概念是 **dev 模式 vs 安装模式**：

- **安装模式（wheel/pip）**：包目录里有 `3rdparty/` 子目录。`TL_LIBS` 指向 `tilelang/lib/`。
- **dev 模式（源码 + PYTHONPATH）**：包目录里**没有** `3rdparty/`（它在仓库根的 `3rdparty/`）。`TL_LIBS` 改指向 `build/lib/` 和 `build/tvm/`，并打印一条 `Loading tilelang libs from dev root` 警告。

区分这两种模式靠一个简单的存在性检查：包目录旁边有没有 `3rdparty/`。

#### 4.4.2 核心流程

`import tilelang` 的库定位流程（聚焦 `env.py` + `libinfo.py` + `__init__.py`）：

```text
import tilelang
   │
   ├─ tilelang/__init__.py
   │     ├─ _compute_version()       # 决定 __version__
   │     └─ from .env import env     # ← 关键：导入 env.py（模块级副作用在这里执行）
   │
   ├─ tilelang/env.py（模块加载时执行）
   │     ├─ TL_ROOT = tilelang 包所在目录
   │     ├─ THIRD_PARTY_ROOT = TL_ROOT/3rdparty
   │     ├─ if 存在 3rdparty/:  安装模式
   │     │       TL_LIBS = [TL_ROOT/lib]
   │     ├─ else:                dev 模式
   │     │       TL_LIBS = [build/lib, build/tvm]
   │     ├─ 设置 TVM/CUTLASS/CK/Template 路径环境变量
   │     └─ 探测 CUDA_HOME / ROCM_HOME
   │
   ├─ tilelang/__init__.py（继续）
   │     ├─ from . import libinfo
   │     └─ _load_tile_lang_lib():
   │           lib_path = libinfo.find_lib_path("tilelang")
   │           ctypes.CDLL(lib_path)        # 真正加载
   │
   └─ tilelang/libinfo.py
         └─ find_lib_path("tilelang"):
               在 TL_LIBS 里找 libtilelang.so / tvm_compiler.dll / libtilelang.dylib
               找到 → 返回绝对路径
               找不到 → raise RuntimeError("Cannot find libraries ...")
```

库定位的目标函数可以用一个简单式子概括其候选集：

\[
\text{candidates} = \{\, d \in \text{TL\_LIBS} \;\mid\; \text{isdir}(d) \,\} \times \{\text{libtilelang.so}, \ldots\}
\]

即在「存在的目录」与「平台对应的文件名」做笛卡尔积，命中第一个就返回。

#### 4.4.3 源码精读

**第一步：`TL_LIBS` 与 dev/安装模式判定**，见 [tilelang/env.py:47-64](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L47-L64)：

```python
TL_ROOT = os.path.dirname(os.path.abspath(__file__))
TL_LIBS = [os.path.join(TL_ROOT, "lib")]
TL_LIBS = [i for i in TL_LIBS if os.path.exists(i)]

DEV = False
THIRD_PARTY_ROOT = os.path.join(TL_ROOT, "3rdparty")
if not os.path.exists(THIRD_PARTY_ROOT):
    DEV = True
    tl_dev_root = os.path.dirname(TL_ROOT)
    dev_lib_root = os.path.join(tl_dev_root, "build")
    TL_LIBS = [os.path.join(dev_lib_root, "lib"), os.path.join(dev_lib_root, "tvm")]
    THIRD_PARTY_ROOT = os.path.join(tl_dev_root, "3rdparty")
    logger.warning(f"Loading tilelang libs from dev root: {dev_lib_root}")
```

读法：

- `TL_ROOT` 是 `env.py` 所在目录，即 `tilelang` 包目录。
- 安装模式下 `TL_LIBS = [tilelang/lib]`（因为 wheel 把 `.so` 装到了 `tilelang/lib`，见 4.3.3）。
- dev 模式下 `TL_LIBS = [build/lib, build/tvm]`——也就是你 `make` 之后产物所在的位置。
- `THIRD_PARTY_ROOT` 也随之切换：安装模式在包内，dev 模式在仓库根。

**第二步：第三方路径初始化**，见 [tilelang/env.py:597-632](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L597-L632)（节选 TVM 与 CUTLASS）：

```python
# 初始化 TVM Python 路径
if env.TVM_IMPORT_PYTHON_PATH is not None:
    prepend_pythonpath(env.TVM_IMPORT_PYTHON_PATH)
else:
    tvm_path = os.path.join(THIRD_PARTY_ROOT, "tvm", "python")
    assert os.path.exists(tvm_path), tvm_path
    prepend_pythonpath(tvm_path)

# 初始化 CUTLASS 路径
if os.environ.get("TL_CUTLASS_PATH", None) is None:
    cutlass_inc_path = os.path.join(THIRD_PARTY_ROOT, "cutlass", "include")
    if os.path.exists(cutlass_inc_path):
        os.environ["TL_CUTLASS_PATH"] = env.CUTLASS_INCLUDE_DIR = cutlass_inc_path
```

这里把 `3rdparty/tvm/python` 加入 `sys.path`（这样 `import tvm` 用的是配套版本），并把 CUTLASS/CK 的 include 路径写进 `TL_CUTLASS_PATH` / `TL_COMPOSABLE_KERNEL_PATH`。注意 CK（Composable Kernel）路径在 [tilelang/env.py:619-624](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L619-L624)，模板路径在 [tilelang/env.py:626-632](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L626-L632)。这些环境变量后续会被代码生成阶段读取（在后续讲义展开）。

**第三步：CUDA/ROCm 主目录探测**，见 [tilelang/env.py:141-190](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L141-L190)。`_find_cuda_home()` 按 4 个优先级查找：

```python
# Guess #1
cuda_home = os.environ.get("CUDA_HOME") or os.environ.get("CUDA_PATH")
# Guess #2
if cuda_home is None:
    nvcc_path = shutil.which("nvcc")
    ...
# Guess #3
elif _get_package_version("nvidia-cuda-nvcc") is not None:
    # 从 pip 包 nvidia-cuda-nvcc 里找
    ...
# Guess #4
else:
    # Linux/macOS 默认路径 /usr/local/cuda 等
    ...
```

这套探测在「运行期需要调用 `nvcc`/`ptxas` 做 JIT 编译」时被用到——所以即使 wheel 自带 stub，要在运行期把生成的 CUDA 源码编成 cubin，仍然需要找到真实的 CUDA Toolkit（主机安装或 pip 包）。

**第四步：`libinfo.find_lib_path` 真正定位文件**，见 [tilelang/libinfo.py:13-47](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/libinfo.py#L13-L47)：

```python
def find_lib_path(name: str, py_ext=False):
    if py_ext:
        lib_names = [f"{name}{suffix}" for suffix in importlib.machinery.EXTENSION_SUFFIXES]
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

    for lib_root in TL_LIBS:
        for lib_name in lib_names:
            lib_dll_path = os.path.join(lib_root, lib_name)
            if os.path.exists(lib_dll_path) and os.path.isfile(lib_dll_path):
                return lib_dll_path
    else:
        raise RuntimeError(f"Cannot find libraries: {', '.join(lib_names)}\n..."
                           + "\n".join(TL_LIBS))
```

这段非常关键，逐行理解：

- 按平台拼出候选文件名：Linux 是 `lib{name}.so`，macOS 是 `lib{name}.dylib`。
- **Windows 例外**：当 `name == "tilelang"` 时，找的不是 `tilelang.dll` 而是 `tvm_compiler.dll`——原因就是 4.3.3 提到的「Windows 把 tilelang 编进了 tvm_compiler.dll」。
- 双层循环：遍历 `TL_LIBS` 的每个候选目录 × 每个候选文件名，命中即返回绝对路径。
- 全部找不到：抛 `RuntimeError`，并把搜索过的 `TL_LIBS` 列出来辅助排查。

`get_dll_directories()` 是给 Windows 用的辅助函数，见 [tilelang/libinfo.py:8-10](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/libinfo.py#L8-L10)，它把 `TL_LIBS` 与 `get_cuda_dll_search_dirs()` 合并，用于向 Windows 的安全 DLL 加载器注册目录（见 [tilelang/__init__.py:170-177](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L170-L177) 的 `os.add_dll_directory`）。

**第五步：调用方**，见 [tilelang/__init__.py:183-190](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L183-L190)：

```python
def _load_tile_lang_lib():
    lib_path = libinfo.find_lib_path("tilelang")
    return ctypes.CDLL(lib_path), lib_path

if env.SKIP_LOADING_TILELANG_SO == "0":
    _LIB, _LIB_PATH = _load_tile_lang_lib()
```

也就是说，只要环境变量 `SKIP_LOADING_TILELANG_SO` 不为 `"0"` 之外的真值，就会真正加载库。加载后，C++ 侧通过 tvm-ffi 注册的 `tl.*` 全局函数就可被 Python 调用（注册入口在 [tilelang/_ffi_api.py:6](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/_ffi_api.py#L6) 的 `tvm_ffi.init_ffi_api("tl", __name__)`）。

**版本号的来源**，见 [tilelang/__init__.py:10-44](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L10-L44)：

```python
def _compute_version() -> str:
    repo_root = Path(__file__).resolve().parent.parent
    version_file = repo_root / "VERSION"
    if version_file.is_file():
        # 源码检出：用 version_provider 动态算
        from version_provider import dynamic_metadata
        return dynamic_metadata("version")
    # 已安装：用 importlib.metadata
    from importlib.metadata import version as _dist_version
    return _dist_version("tilelang")
```

所以同一个 `print(tilelang.__version__)`：

- 在源码检出里会拼上 git hash（如 `0.1.12+cuda.gita1b2c3d4`）。
- 在 pip 安装里读到的是 wheel 打包时定型、写进 metadata 的版本字符串。

**缓存目录** 也属于运行期环境，见 [tilelang/env.py:356](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L356)：

```python
TILELANG_CACHE_DIR = EnvVar("TILELANG_CACHE_DIR", os.path.expanduser("~/.tilelang/cache"))
```

默认编译缓存目录是 `~/.tilelang/cache`（与官方文档「Compile Cache」一致）。

#### 4.4.4 代码实践

本实践是本讲的主任务：**亲手观察 libinfo 如何定位 `libtilelang.so`**。

1. **实践目标**：在不破坏安装的前提下，让 tilelang 告诉你它加载的是哪个 `.so`、在哪个目录找到的。
2. **操作步骤**：
   - 先确认正常导入：`python -c "import tilelang; print(tilelang.__version__)"`。
   - 打印库搜索目录与加载路径。新建 `trace_lib.py`（**示例代码**，非项目原有文件）：

     ```python
     # 示例代码：仅用于观察 tilelang 的库定位过程
     import tilelang                       # 触发完整导入与原生库加载
     from tilelang import env as tenv
     from tilelang import libinfo

     print("TL_ROOT 候选目录 TL_LIBS =", tenv.TL_LIBS)
     print("DEV 模式? =", tenv.DEV)
     print("THIRD_PARTY_ROOT =", tenv.THIRD_PARTY_ROOT)
     print("CUDA_HOME =", tenv.CUDA_HOME)
     print("CUTLASS path =", tenv.CUTLASS_INCLUDE_DIR)

     # 直接问 libinfo 它会选哪个文件
     print("find_lib_path('tilelang') =", libinfo.find_lib_path("tilelang"))
     ```

   - 运行 `python trace_lib.py`。
3. **需要观察的现象**：
   - `TL_LIBS` 应是一个目录列表（pip 安装时通常只有一项，指向 `site-packages/tilelang/lib`；dev 模式指向 `build/lib` 与 `build/tvm`）。
   - `DEV` 在 pip 安装下为 `False`，在 PYTHONPATH 源码运行下为 `True`，且会打印 `Loading tilelang libs from dev root` 警告。
   - `find_lib_path("tilelang")` 返回的绝对路径就是被 `ctypes.CDLL` 加载的那个文件。
4. **预期结果**：你能把「`TL_LIBS` 目录」与「最终加载的 `.so` 文件」一一对应起来，并说清是哪条 `find_lib_path` 分支命中的（Linux 命中 `libtilelang.so`，Windows 命中 `tvm_compiler.dll`）。
5. **进阶**：设置 `SKIP_LOADING_TILELANG_SO=1` 再导入，观察是否跳过 `_load_tile_lang_lib()`（此时调用任何需要原生库的 API 会报错，但 `env` 模块本身的属性仍可读，可用于排障）。本环境无法执行，「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：导入时报错 `Cannot find libraries: libtilelang.so`，列出 `TL_LIBS` 里只有一个错误的目录。可能的原因有哪些？

> **答案**：常见原因：(a) wheel 安装不完整或被破坏，`tilelang/lib/` 下没有 `.so`；(b) 你在源码目录用 PYTHONPATH 运行（dev 模式），但还没执行 `make`，`build/lib/` 不存在或没产物；(c) 手动改过 `TL_LIBS` 或 `TVM_LIBRARY_PATH` 指错了路径。排查方法是按报错里列出的 `TL_LIBS` 逐个 `ls`，确认目标 `.so` 是否真的在。

**练习 2**：为什么在 Windows 上 `find_lib_path("tilelang")` 找的是 `tvm_compiler.dll` 而不是 `tilelang.dll`？

> **答案**：因为 Windows 构建把 tilelang 的原生目标文件链接进了 `tvm_compiler.dll`（见 [CMakeLists.txt:550-557](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L550-L557)），原因是 Windows DLL 有 65535 符号上限且 tilelang 需要访问 TVM 未导出的内部符号。`libinfo.py` 的 Windows 分支用 `if name == "tilelang"` 专门处理了这个特例。

**练习 3**：`tilelang.__version__` 在「源码检出」和「pip 安装」下分别从哪来？

> **答案**：源码检出时，`__init__.py` 发现仓库根有 `VERSION` 文件，调用 `version_provider.dynamic_metadata("version")` 动态拼出带 git hash 的版本；pip 安装时没有 `VERSION` 文件（在包外），走 `importlib.metadata.version("tilelang")` 读 wheel 打包时写死的 metadata（见 [tilelang/__init__.py:10-44](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L10-L44)）。

## 5. 综合实践

把本讲的知识串起来，做一个「安装取证」小任务。

**背景**：你的同事在机器 A 上 `pip install tilelang` 跑得好好的，把代码拷到机器 B 却 `import` 失败。请你用本讲学到的方法定位问题。

**任务步骤**：

1. **看版本号推断构建**：在机器 A 上记录 `tilelang.__version__` 的完整字符串（含 `+...` 后缀）。根据 [version_provider.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/version_provider.py) 解释后缀含义：是 `cu130`？`rocm`？`cpu`？还是 `cuda`（无具体版本）？这决定了该 wheel 期望的运行时。
2. **看库定位**：在机器 A 上运行 4.4.4 的 `trace_lib.py`，记录 `TL_LIBS`、`find_lib_path("tilelang")` 的输出。确认 `.so` 真实落在磁盘上的位置。
3. **看依赖目录**：用 `ls` 查看 `tilelang` 安装目录下的 `lib/`、`3rdparty/tvm/python`、`3rdparty/cutlass/include` 是否齐全（对应 [pyproject.toml:153-169](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L153-L169) 的映射）。
4. **复现 B 的失败**：在机器 B 上同样跑 `trace_lib.py`。如果报 `Cannot find libraries`，对照 `TL_LIBS` 看是缺文件还是路径错；如果 import 成功但跑 kernel 失败，检查 `env.CUDA_HOME`（参考 [tilelang/env.py:141-190](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L141-L190) 的四级探测）是否为空——很可能 B 没装 CUDA Toolkit，导致运行期 JIT 找不到 `nvcc`。
5. **给出结论**：写一句话诊断，例如「A 装的是 `cu130` wheel，B 没有 CUDA 13 运行时，故 import 后 JIT 失败」。

> 提示：区分「能 import」（靠 stub 库）和「能编译运行 kernel」（靠真实 CUDA/ROCm Toolkit）是本任务的核心收获。本环境无法实际部署，上述步骤的运行输出「待本地验证」。

## 6. 本讲小结

- tilelang 有三种获取方式：`pip`（PyPI 稳定版）、`-f nightly`（每日构建）、源码构建；验证安装统一用 `python -c "import tilelang; print(tilelang.__version__)"`。
- tilelang 是 C++ + Python 混合工程，用 `scikit-build-core` 把 CMake 构建产物打进 wheel；wheel 内 `tilelang/lib/` 放原生库，`tilelang/3rdparty/` 放 TVM/CUTLASS/CK。
- 源码构建的关键是 `CMakeLists.txt`：`USE_CUDA/ROCM/METAL/LLVM` 选后端，`3rdparty/tvm` 子模块提供 TVM，POSIX 产出 `libtilelang.so`，Windows 则把 tilelang 编进 `tvm_compiler.dll`。
- **运行期库定位** 由 `tilelang.env`（给目录候选 `TL_LIBS`）和 `tilelang.libinfo`（在目录里匹配文件名 `find_lib_path`）协作完成；dev 模式与安装模式靠「包内有无 `3rdparty/`」自动切换。
- 版本号在源码检出下由 `version_provider.py` 动态拼接（含 git hash），在 pip 安装下读 `importlib.metadata`。
- wheel 靠 CUDA/HIP **stub 库**实现「无 GPU 也能 import」，但真正 JIT 编译 kernel 仍需 `_find_cuda_home()` 探测到的真实 Toolkit。

## 7. 下一步学习建议

装好之后，下一步自然是「看懂仓库结构、找到入口」。建议进入下一讲 [u1-l3 仓库目录结构与包入口](./u1-l3-repo-layout-and-entry.md)，在那里你会：

- 梳理 `tilelang/`（Python）与 `src/`（C++）的子系统对应关系。
- 看 `tilelang/__init__.py` 暴露的公共 API（`jit / compile / language / Profiler / autotune`）。

如果想先「跑起来一个 kernel」再回头读结构，也可以跳到 [u1-l4 第一个 Kernel：Quickstart GEMM 实跑](./u1-l4-quickstart-gemm.md)。后续涉及编译流水线的讲义（第 4 单元）会反复用到本讲提到的 `env.py` 路径变量（如 `TL_CUTLASS_PATH`、`TVM_LIBRARY_PATH`、缓存目录），届时可回看 4.4.3 巩固。
