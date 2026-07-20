# 从源码构建与运行 PyTorch

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 PyTorch 为什么采用「pip + CMake」的混合构建入口，以及两者各负责什么。
- 看懂 `setup.py` 的执行流程：从命令行参数解析、环境变量读取，到最终调用 CMake。
- 掌握从源码做一次开发态可编辑安装的完整命令，并理解 `--no-build-isolation`、`-e`、`MAX_JOBS`、`USE_CUDA=0` 等关键开关的含义。
- 区分 PyTorch 构建产出的三类东西：代码生成产物、C++ 编译产物、Python 包。
- 在阅读 `CMakeLists.txt` 与 `Makefile` 时，能快速定位 CUDA / ROCm / XPU 等后端开关，以及 triton 等辅助构建目标。

## 2. 前置知识

在动手之前，先建立两个直觉。

**直觉一：PyTorch 本质上是一个 C++ 项目套了一层 Python 外壳。** 你日常 `import torch` 用到的绝大多数算子（加、乘、卷积、矩阵乘）都是用 C++/CUDA 写的，编译成动态库后再通过 pybind11 暴露给 Python。因此「构建 PyTorch」真正耗时的工作是编译几千个 `.cpp` / `.cu` 文件，这部分由 **CMake** 负责；而 `setup.py`（即 pip）主要负责「调度 CMake、再把产物打包成 Python 包」。

**直觉二：构建系统的三层职责。** 我们可以把一次构建拆成三类产物：

| 类别 | 由谁产出 | 典型产物 | 落在哪里 |
| --- | --- | --- | --- |
| 代码生成（codegen） | `torchgen` 读取 `native_functions.yaml` | 算子的 Python 绑定、C++ 注册、`.pyi` 类型桩 | `torch/`、`build/` |
| C++ 编译 | CMake 调用编译器 | `libtorch_python.so`、`torch._C` 扩展、`torch/lib/*.so` | `torch/lib/`、`torch/_C...so` |
| Python 包 | setuptools（pip） | `torch/`、`torchgen/` 纯 Python 源码 | 通过 `-e` 链接到 site-packages |

理解这张表后，再看源码就不会被 `setup.py` 里大量的「奇怪」逻辑绕晕——它的核心使命就是「让 CMake 把 C++ 编出来，再让 pip 把 Python 包装好」。

如果你还不熟悉 CMake 的基本概念（`CMakeLists.txt`、`-D` 选项、generator、`--build` / `--target`），建议先记住一句话：**CMake 先「配置（configure）」生成原生构建文件（如 Ninja 的 `build.ninja`），再「构建（build）」真正编译代码。** 本讲会把这两步在 PyTorch 里的对应位置一一指出来。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [setup.py](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/setup.py) | 构建的「总入口」。解析参数与环境变量，把 C++ 构建委托给 CMake，再用 setuptools 打包 Python 包。 |
| [CMakeLists.txt](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/CMakeLists.txt) | C++ 构建的「总配置」。声明编译器要求、C++ 标准，以及所有 `USE_CUDA` / `USE_ROCM` / `USE_XPU` 等后端开关。 |
| [Makefile](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/Makefile) | 一层薄包装。把构建委托给 CMake，并提供 `triton`、`clean`、`lint` 等便捷目标。 |
| [tools/build_pytorch_libs.py](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/tools/build_pytorch_libs.py) | `setup.py` 调用的 `build_pytorch()`，串联「CMake 配置 + CMake 构建」两步。 |
| [tools/setup_helpers/cmake.py](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/tools/setup_helpers/cmake.py) | CMake 的 Python 封装。`generate()` 跑配置、`build()` 跑编译，并行度由 `MAX_JOBS` 控制。 |
| [tools/setup_helpers/env.py](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/tools/setup_helpers/env.py) | 平台探测与构建类型（`Release`/`Debug`/`RelWithDebInfo`）判定。 |
| [cmake/EnvVarForwarding.cmake](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/cmake/EnvVarForwarding.cmake) | 把 `USE_*` / `BUILD_*` / `CMAKE_*` 环境变量自动转成 CMake `-D` 选项。 |

## 4. 核心概念与源码讲解

### 4.1 setup.py：pip 与 CMake 的混合入口

#### 4.1.1 概念说明

`setup.py` 是「pip 看得懂」的入口：当你执行 `pip install .` 时，pip 会以 [PEP 517](https://peps.python.org/pep-0517/) 的方式调用 `setup.py`。但 PyTorch 的 C++ 体量太大，setuptools 自带的扩展编译机制（`ext_modules`）根本扛不住，于是 PyTorch 选择**绕开 setuptools 的编译能力**，把 C++ 编译完全交给 CMake，setuptools 只负责打包。

这就解释了一个容易困惑的点：为什么 `setup.py` 里 `ext_modules` 经常是空列表？因为 `torch._C` 这个 C 扩展不是由 setuptools 编译的，而是由 CMake 编译好后，被「假装」当成一个普通扩展塞进包里。

#### 4.1.2 核心流程

`setup.py` 的执行可以概括成下面这条链路：

```
pip install . --no-build-isolation -e
        │
        ▼
setup.py: main()                         # 入口
        │  解析命令行 / 设置 install_requires
        ▼
build_deps()                             # RUN_BUILD_DEPS 为真时执行
        │  ├─ USE_NIGHTLY? → 下载 nightly wheel 直接返回（跳过编译）
        │  ├─ check_pydep("yaml", "pyyaml")
        │  └─ build_pytorch(...)         # 见 tools/build_pytorch_libs.py
        │         ├─ cmake.generate(...) # 第一步：cmake 配置（生成 build.ninja）
        │         └─ cmake.build(...)    # 第二步：cmake --build 编译并 install
        ▼
configure_extension_build()              # 收集 packages / cmdclass / entry_points
        ▼
setup(...)                               # 把 torch/、torchgen/ 打成 Python 包
```

注意第一步（配置）与第二步（编译）是分开的：CMake 会先生成构建文件（默认用 Ninja），再真正调用编译器。两者都失败才会让整个构建失败。

#### 4.1.3 源码精读

**入口与参数重定向。** `setup.py` 一上来就是一段长达两百多行的注释，枚举了所有「你可能感兴趣的环境变量」。这其实就是 PyTorch 构建系统的「使用手册」，本讲的实践任务就是从这里摘取信息，参见 [setup.py:1-243](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/setup.py#L1-L243)。其中关键几条：

- `MAX_JOBS`：最大并行编译数（控制编译吃多少内存/CPU）。
- `USE_CUDA=0`：禁用 CUDA 构建（本讲重点）。
- `DEBUG` / `REL_WITH_DEB_INFO`：控制是否带调试符号与优化等级。
- `CMAKE_FRESH=1`：强制重新跑一次 CMake 配置，丢弃旧缓存。
- `CMAKE_ONLY=1`：只跑 CMake 配置就停，不编译。

进入代码后，`main()` 先设置依赖列表（`filelock`、`sympy`、`jinja2` 等），再调用 `build_deps()`，参见 [setup.py:1159-1198](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/setup.py#L1159-L1198)。

`build_deps()` 是真正的「构建调度器」。它会先检查 `USE_NIGHTLY`（一种跳过本地编译、直接下载官方 nightly wheel 的快捷方式），否则校验 `pyyaml` 后调用 `build_pytorch()`，参见 [setup.py:824-881](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/setup.py#L824-L881)：

```python
check_pydep("yaml", "pyyaml")
build_pytorch(
    version=TORCH_VERSION,
    cmake_python_library=CMAKE_PYTHON_LIBRARY.as_posix(),
    build_python=not BUILD_LIBTORCH_WHL,
    rerun_cmake=RERUN_CMAKE,
    cmake_only=CMAKE_ONLY,
    cmake=cmake,
)
```

**命令行重定向到 pip。** 如果你还习惯写 `python setup.py develop` / `python setup.py install`，`setup.py` 会显式把这些命令重定向到官方推荐的 `pip install -e . --no-build-isolation`，并打印警告，参见 [setup.py:402-431](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/setup.py#L402-L431)。构建结束时还会用一个醒目的方框打印出规范命令 [setup.py:1137-1146](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/setup.py#L1137-L1146)：

```
To install:
  $ python -m pip install --no-build-isolation -v .
To develop locally:
  $ python -m pip install --no-build-isolation -v -e .
To force cmake to re-generate native build files (off by default):
  $ CMAKE_FRESH=1 python -m pip install --no-build-isolation -v -e .
```

**为什么 `torch._C` 不在 `ext_modules` 里？** `configure_extension_build()` 返回的 `ext_modules` 是一个空列表，并配了注释说明 `torch._C` 由 CMake 构建（`torch/CMakeLists.txt`），见 [setup.py:1090-1094](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/setup.py#L1090-L1094)。为了让 wheel 仍被识别为「二进制包」（从而使用 `package_data` 列表而不是把整个 `build/lib` 倒进 wheel），定义了 `BinaryDistribution` 强制 `has_ext_modules()` 返回 `True`，见 [setup.py:1036-1044](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/setup.py#L1036-L1044)。最后通过 `package_data` 精确列出要打包的产物（`_C{EXT_SUFFIX}`、`lib/*.so*`、头文件等），见 [setup.py:1201-1251](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/setup.py#L1201-L1251)。

**布尔环境变量解析。** 注意 `setup.py` 用一个 `str2bool()` 把 `0/1/true/false/on/off/...` 统一解析成布尔值，见 [setup.py:320-358](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/setup.py#L320-L358)。这也是为什么 `USE_CUDA=0`、`USE_CUDA=False`、`USE_CUDA=OFF` 都能生效。

#### 4.1.4 代码实践

**实践目标**：从 `setup.py` 的注释里提取构建开关，并理解 `USE_CUDA=0` 的传导路径。

**操作步骤**：

1. 打开 [setup.py:1-243](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/setup.py#L1-L243)，列出至少 3 个 `USE_*` 开头的环境变量（例如 `USE_CUDA`、`USE_CUDNN`、`BUILD_TEST`、`USE_DISTRIBUTED`、`USE_NUMPY`）。
2. 追踪 `USE_CUDA=0` 的去向：你在 shell 里 `export USE_CUDA=0` 后，这个值会被 `cmake/EnvVarForwarding.cmake` 自动转发成 CMake 变量 `-DUSE_CUDA=0`（见下一节），从而让 `CMakeLists.txt` 里的 `option(USE_CUDA ...)` 取到 `OFF`。
3. 在终端里执行一次「干跑（dry-run）」，只做 CMake 配置、不编译：

   ```bash
   CMAKE_ONLY=1 python -m pip install --no-build-isolation -v .
   ```

**需要观察的现象**：

- 控制台会打印出 `cmake` 的完整命令行，其中 `-D` 参数里能看到 `USE_CUDA`、`BUILD_PYTHON`、`Python_EXECUTABLE` 等。
- 因为 `CMAKE_ONLY=1`，流程会在配置完成后停下并提示你接下来用 `pip install ...` 完成编译（对应 [setup.py:875-881](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/setup.py#L875-L881)）。

**预期结果**：

- `build/` 目录下出现 `CMakeCache.txt` 与 `build.ninja`（如果装了 ninja），但还没有 `.so` 产物。
- `USE_CUDA=0` 时，`CMakeCache.txt` 里 `USE_CUDA:BOOL=OFF`，并且后续编译不会调用 nvcc，构建时间显著缩短。

> 待本地验证：在没有 CUDA 工具链的纯 CPU 机器上，`USE_CUDA=0` 是让构建顺利跑通的必要条件；在有 CUDA 的机器上，它只是「跳过 GPU 后端」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PyTorch 推荐 `--no-build-isolation`，而不是普通的 `pip install .`？

> **参考答案**：默认情况下 pip 会创建一个隔离的临时虚拟环境来运行构建，那个环境里只有 `pyproject.toml` 声明的 `build-system` 依赖，缺少 PyTorch 构建所需的 `pyyaml`、`numpy`、编译器、CMake 等。`--no-build-isolation` 让构建直接使用你当前已激活、已装好这些依赖的环境，这正是 PyTorch 构建脚本的前提（`check_pydep("yaml", "pyyaml")` 才能通过）。

**练习 2**：`-e`（editable）安装与不带 `-e` 的安装，对后续改源码的影响有什么不同？

> **参考答案**：`-e` 会把 `torch/` 以「可编辑」方式链接到 site-packages（通常是一个 `.pth` 指向源码目录）。你修改 `torch/` 下的纯 Python 文件后，无需重装即可立即生效；但修改 C++ 源码仍需重新编译（因为 `.so` 是二进制产物）。

---

### 4.2 CMakeLists.txt：C++ 构建的总配置与后端开关

#### 4.2.1 概念说明

`CMakeLists.txt` 是 C++ 侧的「真理之源」。它做三件事：

1. 声明项目与工具链要求（CMake 版本、编译器版本、C++ 标准）。
2. 用一堆 `option(...)` / `cmake_dependent_option(...)` 定义所有后端与功能开关。
3. 把这些开关交给各子目录的 `CMakeLists.txt` 去决定编译哪些源文件。

对学习者来说，最重要的是第 2 点：`USE_CUDA` / `USE_ROCM` / `USE_XPU` 这三个开关，决定了 PyTorch 会被编译出哪些 GPU 后端。

#### 4.2.2 核心流程

CMake 的执行同样分「配置」与「构建」两阶段。在 PyTorch 里这两步由 `tools/setup_helpers/cmake.py` 封装：

```
cmake.generate():  cmake -GNinja -DCMAKE_INSTALL_PREFIX=.../torch -DBUILD_PYTHON=ON ... <源码根>
        │  → 产出 build/CMakeCache.txt、build/build.ninja
        ▼
cmake.build():     cmake --build . --target install --config Release -j $MAX_JOBS
        │  → 编译 + 把产物 install 到 torch/lib、torch/_C...so
```

并行度（`-j`）的选取有三档优先级，见 [cmake.py:353-376](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/tools/setup_helpers/cmake.py#L353-L376)：

1. 显式设置的 `MAX_JOBS` 环境变量；
2. 若用 Ninja，则交给 Ninja 自己决定；
3. 否则退回 `multiprocessing.cpu_count()`。

构建类型（`Release` / `Debug` / `RelWithDebInfo`）由 `DEBUG` / `REL_WITH_DEB_INFO` 环境变量推导，默认 `Release`，见 [env.py:89-98](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/tools/setup_helpers/env.py#L89-L98)。

#### 4.2.3 源码精读

**工具链与标准。** 顶层要求 CMake ≥ 3.27、C++20、C17，并对编译器版本设了下限（GCC ≥ 11.3、Clang ≥ 16），见 [CMakeLists.txt:1-79](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/CMakeLists.txt#L1-L79)。其中 C++ 标准设置如下：

```cmake
set(CMAKE_CXX_STANDARD 20 CACHE STRING
    "The C++ standard whose features are requested to build this target.")
set(CMAKE_C_STANDARD 17 CACHE STRING
    "The C standard whose features are requested to build this target.")
```

参见 [CMakeLists.txt:49-56](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/CMakeLists.txt#L49-L56)。

**三大 GPU 后端开关。** 这是本节重点。注意 CUDA 与 TSAN 互斥（线程 sanitizer 无法与 CUDA 共存），所以用 `cmake_dependent_option` 表达「当未启用 TSAN 时才默认开 CUDA」，见 [CMakeLists.txt:262-263](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/CMakeLists.txt#L262-L263)：

```cmake
# CUDA is incompatible with TSAN
cmake_dependent_option(USE_CUDA "Use CUDA" ON "NOT USE_TSAN" OFF)
```

XPU（Intel GPU）与 ROCm（AMD GPU）类似，见 [CMakeLists.txt:272](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/CMakeLists.txt#L272) 与 [CMakeLists.txt:277](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/CMakeLists.txt#L277)：

```cmake
option(USE_XPU "Use XPU" ON)
...
cmake_dependent_option(USE_ROCM "Use ROCm" ON "LINUX OR WIN32" OFF)
```

`cmake_dependent_option(NAME "desc" DEFAULT "条件" 否则值)` 的语义是：当「条件」为真时，默认值是 `DEFAULT`；条件为假时，默认值是最后那个 `OFF`。所以 `USE_ROCM` 在 macOS 上默认就是关的。

围绕这三个后端，还有一连串「依赖型」选项，例如 `USE_CUDNN`（依赖 `USE_CUDA`）、`USE_NCCL`（依赖 `USE_DISTRIBUTED` 且要有 CUDA/ROCm）、`USE_XCCL`（依赖 `USE_XPU`）等，见 [CMakeLists.txt:281-313](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/CMakeLists.txt#L281-L313)。例如：

```cmake
cmake_dependent_option(USE_CUDNN "Use cuDNN" ON "USE_CUDA" OFF)
cmake_dependent_option(USE_NCCL "Use NCCL" ON
                       "USE_DISTRIBUTED;USE_CUDA OR USE_ROCM;UNIX;NOT APPLE" OFF)
```

这意味着：如果你设了 `USE_CUDA=0`，那么 `USE_CUDNN`、`USE_CUSPARSELT`、`USE_NCCL` 等都会因为依赖条件不满足而自动关闭——这就是「`USE_CUDA=0` 会带来什么效果」的根因。

**环境变量如何变成 CMake 选项。** 你可能会问：我只是在 shell 里 `export USE_CUDA=0`，CMake 是怎么知道 `USE_CUDA` 这个变量的？答案在 [cmake/EnvVarForwarding.cmake:73-90](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/cmake/EnvVarForwarding.cmake#L73-L90)：它枚举所有环境变量，凡是名字以 `BUILD_` / `USE_` / `CMAKE_` 开头的，都会被自动转成同名 CMake 缓存变量（若未被显式设置）。所以 `USE_CUDA=0` 会变成 `-DUSE_CUDA=0`，进而被 `option(USE_CUDA ...)` 读到。

**配置阶段到底传了哪些 `-D`。** `cmake.generate()` 只显式传几个必须由 Python 侧探测的变量（Python 解释器、NumPy 头文件路径、`CMAKE_INSTALL_PREFIX` 等），其余 `USE_*` 都交给 `EnvVarForwarding.cmake` 处理，见 [cmake.py:284-311](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/tools/setup_helpers/cmake.py#L284-L311)：

```python
build_options: dict[str, CMakeValue] = {
    "CMAKE_INSTALL_PREFIX": install_dir,   # = <源码根>/torch
    "BUILD_PYTHON": build_python,
    "BUILD_TEST": build_test,
}
```

注意 `CMAKE_INSTALL_PREFIX` 指向源码树里的 `torch/`，所以 CMake `--target install` 会把 `.so`、头文件直接铺到 `torch/lib`、`torch/include` 下——这也是为什么 `package_data` 能在 `torch/lib/*.so*` 里找到产物。

#### 4.2.4 代码实践

**实践目标**：通过 CMake 缓存文件，亲眼看见后端开关的取值与依赖关系。

**操作步骤**：

1. 先做一次配置（不编译）：

   ```bash
   CMAKE_ONLY=1 USE_CUDA=0 python -m pip install --no-build-isolation -v .
   ```

2. 在另一个终端查看 `build/CMakeCache.txt`，搜索 `USE_CUDA`、`USE_CUDNN`、`USE_NCCL`、`USE_XPU`。

**需要观察的现象**：

- `USE_CUDA:BOOL=OFF`，并且 `USE_CUDNN`、`USE_CUSPARSELT`、`USE_NCCL` 等也被推断为 `OFF`，体现 `cmake_dependent_option` 的级联关闭。
- 如果你在 Intel GPU 机器上，`USE_XPU:BOOL=ON` 可能仍为真，说明三个后端彼此独立。

**预期结果**：你能够用一张表说明「关掉 `USE_CUDA` 会连带关掉哪些选项」，从而理解为什么纯 CPU 构建既快又不需要 CUDA 工具链。

> 待本地验证：不同平台、不同已装库会导致 `USE_*` 的实际取值不同；以你本机 `CMakeCache.txt` 为准。

#### 4.2.5 小练习与答案

**练习 1**：`option(USE_XPU "Use XPU" ON)` 与 `cmake_dependent_option(USE_ROCM ... "LINUX OR WIN32" OFF)` 写法不同，原因是什么？

> **参考答案**：`option` 是无条件默认值；`cmake_dependent_option` 允许默认值依赖一个条件表达式。ROCm 只在 Linux/Windows 上有意义（macOS 上没有 ROCm），所以用条件选项让它在 macOS 上默认关闭；而 XPU 在支持的两大平台上都可用，故用普通 `option`。

**练习 2**：为什么 `CMAKE_INSTALL_PREFIX` 要指向源码树内的 `torch/`，而不是系统目录？

> **参考答案**：因为接下来 setuptools 打包时，是从源码树里的 `torch/` 收集文件（`package_data`）。把 CMake 的 install 前缀指向 `torch/`，可以让编译产物直接落在打包工具能找到的位置，避免二次拷贝。这也使得 `-e` 可编辑安装后，重新 `cmake --build` 产出的新 `.so` 立刻可用。

---

### 4.3 Makefile：薄包装与 triton / lint 等辅助目标

#### 4.3.1 概念说明

仓库根目录的 `Makefile` **不负责真正的构建逻辑**，它只是把工作转发给 CMake 或 pip。它的价值在于提供几个高频「快捷方式」：`make`（构建）、`make triton`（装匹配版本的 triton）、`make clean`（清构建目录）、`make lint`（跑 lintrunner）。理解它能帮你少记几条长命令。

#### 4.3.2 核心流程

`Makefile` 第一行就声明了意图：把真正的构建委托给 CMake。整体目标如下：

```
make            →  cmake -S . -B build && cmake --build build        # 配置 + 编译
make triton     →  pip uninstall -y triton; ./scripts/install_triton_wheel.sh
make clean      →  rm -r build*/                                       # 删除所有构建目录
make lint       →  lintrunner --all-files                              # 代码检查
make setup-env  →  tools/nightly.py pull                               # 拉取 nightly 依赖
```

注意：`make` 直接调用 CMake，**不经过 setup.py / pip**，所以它产出的只是 `build/` 里的二进制，并不会把 Python 包装到 site-packages。要完成「安装」仍需 `pip install --no-build-isolation -e .`。`make` 更适合「我只改了 C++，想快速重编译看看」的场景。

#### 4.3.3 源码精读

**默认目标：转发给 CMake。** `make`（即 `all`）会先用 `scripts/get_python_cmake_flags.py` 算出 Python 相关的 CMake 标志，再配置并构建，见 [Makefile:9-12](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/Makefile#L9-L12)：

```makefile
.PHONY: all
all:
	@cmake -S . -B build $(shell $(PYTHON) ./scripts/get_python_cmake_flags.py) && \
		cmake --build build --parallel --
```

**triton 目标：装匹配版本的 triton wheel。** `torch.compile` 的默认后端 Inductor 在 CUDA 上依赖 triton。为保证版本兼容，仓库提供了一个脚本来安装「与当前 PyTorch 源码匹配」的 triton，见 [Makefile:14-17](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/Makefile#L14-L17)：

```makefile
.PHONY: triton
triton:
	$(PIP) uninstall -y triton
	@./scripts/install_triton_wheel.sh
```

README 也提示了这一点：若要用 `torch.compile` 的 inductor/triton，运行 `make triton`。

**clean 与 lint。** `clean` 会删除所有 `build*/` 目录（CMake 的输出都在这里），见 [Makefile:19-21](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/Makefile#L19-L21)；`setup-lint` / `lint` 走 lintrunner（这是 PyTorch 仓库统一的 lint 入口，对应 CLAUDE.md 里「只用 `spin lint`/`lintrunner`」的要求），见 [Makefile:48-65](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/Makefile#L48-L65)。

**setup-env 系列。** 还有 `setup-env` / `setup-env-cuda` / `setup-env-rocm`，它们调用 `tools/nightly.py` 拉取一些依赖（如匹配的 triton、编译器工具链提示等），见 [Makefile:36-46](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/Makefile#L36-L46)。这属于可选的环境准备步骤，不影响主构建链路。

#### 4.3.4 代码实践

**实践目标**：用 `make` 走一遍「纯 CMake」路径，对比它与 `pip install` 的差异。

**操作步骤**：

1. 在仓库根目录执行：

   ```bash
   make            # 等价于 cmake -S . -B build && cmake --build build
   ```

2. 构建完成后，尝试 `python -c "import torch"`，观察是否能导入。

**需要观察的现象**：

- `make` 会在 `build/` 下产出二进制并 `install` 到 `torch/lib`，但**不会**把 `torch` 注册到 site-packages。所以如果你之前没装过 torch，`import torch` 很可能失败（除非你的 `PYTHONPATH` 指向仓库根目录）。
- 再执行一次 `pip install --no-build-isolation -e .`，由于 C++ 已经编好，这一步会很快（主要是 setuptools 的打包与 `.pth` 链接）。

**预期结果**：你会直观体会到「`make` = 只编 C++」、「`pip install -e` = 编 C++ + 装 Python 包」的区别。日常开发中，改完 C++ 后用 `make` 增量编译，往往比重新跑一遍 pip 更快。

> 待本地验证：`make` 是否真的只编 C++，取决于你机器上是否已有可用的 CMake 缓存；首次运行 `make` 也会触发完整配置。

#### 4.3.5 小练习与答案

**练习 1**：既然 `pip install -e .` 已经能完成全部构建，为什么还要保留 `make`？

> **参考答案**：`make` 直接调用 CMake，省去了 setuptools/pip 的开销，适合「只改了 C++、想快速增量重编」的循环。它是面向「已经在做开发、只想重编二进制」的场景；而 `pip install` 面向「首次安装或需要更新 Python 包元数据」的场景。

**练习 2**：`make triton` 为什么要先 `pip uninstall -y triton` 再装？

> **参考答案**：为了确保装上的是「与当前源码版本严格匹配」的 triton，避免残留的旧版 triton 干扰 `torch.compile`/Inductor。仓库的 `install_triton_wheel.sh` 会根据当前 PyTorch 版本选择对应的 triton wheel。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「从零到可导入」的 CPU-only 源码构建（或完整 dry-run 描述）。建议步骤如下：

1. **读懂开关**：从 [setup.py:1-243](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/setup.py#L1-L243) 摘出 5 个 `USE_*` 开关，填入下表：

   | 环境变量 | 默认 | 设为 0 时的效果 |
   | --- | --- | --- |
   | `USE_CUDA` | （CMake 推断） | 关闭 CUDA 后端，连带关 cuDNN/NCCL 等 |
   | `USE_DISTRIBUTED` | … | … |
   | `BUILD_TEST` | … | … |
   | `USE_NUMPY` | … | … |
   | `USE_ROCM` / `USE_XPU` | … | … |

2. **配置阶段**：执行 `CMAKE_ONLY=1 USE_CUDA=0 python -m pip install --no-build-isolation -v .`，确认 `build/CMakeCache.txt` 中 `USE_CUDA:BOOL=OFF`，并指出它连带关闭了哪些选项（对应 [CMakeLists.txt:281-313](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/CMakeLists.txt#L281-L313)）。

3. **编译阶段**：去掉 `CMAKE_ONLY`，完整执行 `USE_CUDA=0 python -m pip install --no-build-isolation -v -e .`。在日志中找到 `cmake --build . --target install` 这一行，确认它的 `-j` 并行度（若你设了 `MAX_JOBS` 应该出现，否则交给 Ninja，对应 [cmake.py:353-376](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/tools/setup_helpers/cmake.py#L353-L376)）。

4. **验证产物**：构建完成后执行：

   ```bash
   python -c "import torch; print(torch.__version__); print(torch._C)"
   ```

   确认能成功导入，且 `torch._C` 是一个已加载的编译扩展模块。

5. **反思三类产物**：对照第 2 节的表格，在你的 `torch/` 目录下找到：一个 C++ 产物（`torch/lib/libtorch_python.so` 或 `torch/_C*.so`）、一个 codegen 产物（`torch/_C/__init__.pyi` 之类的类型桩）、以及纯 Python 包本身（`torch/nn/__init__.py` 等）。

> 若没有 GPU 或不希望触发 CUDA，整条路径都应在 `USE_CUDA=0` 下完成；若编译资源紧张，用 `MAX_JOBS=4` 之类限制并行度以免内存爆掉。

## 6. 本讲小结

- PyTorch 的构建是「pip + CMake」混合：`setup.py`（pip）是入口，但 C++ 编译完全交给 CMake，setuptools 只负责打包 Python 包。
- 规范的安装命令是 `python -m pip install --no-build-isolation -v -e .`；`--no-build-isolation` 是为了让构建使用你已装好依赖的当前环境。
- `torch._C` 不在 setuptools 的 `ext_modules` 里，而是由 CMake `--target install` 编译到 `torch/lib` 与 `torch/_C*.so`；`BinaryDistribution` 让 wheel 仍被识别为二进制包。
- 构建产物分三类：codegen（torchgen 生成的绑定与类型桩）、C++（`.so`/`.dylib`/`.dll` 与 `torch._C`）、Python 包（`torch/`、`torchgen/`）。
- 后端开关集中在 `CMakeLists.txt`：`USE_CUDA` / `USE_ROCM` / `USE_XPU` 三大后端，外加 `USE_CUDNN` / `USE_NCCL` 等依赖型选项；`USE_CUDA=0` 会通过 `cmake_dependent_option` 级联关闭一整排 CUDA 相关选项。
- 环境变量靠 `cmake/EnvVarForwarding.cmake` 自动转成 CMake `-D` 选项；`MAX_JOBS` 控制并行度，`DEBUG`/`REL_WITH_DEB_INFO` 控制构建类型。
- `Makefile` 是一层薄包装：`make` 只走 CMake（适合增量重编 C++），`make triton` 装匹配版本 triton，`make clean` 清构建目录。

## 7. 下一步学习建议

下一讲 **u1-l3 仓库目录结构与代码组织** 会带你遍历 `torch/`、`aten/`、`c10/`、`torchgen/`、`torch/csrc/` 各目录的职责，建议结合本讲建立的「三类产物」视角去读：你会看到 codegen 产物落在 `torch/`、C++ 产物落在 `aten/` 与 `c10/`、Python 包主体落在 `torch/`。

之后可以继续阅读：

- `tools/setup_helpers/cmake.py` 全文，理解配置缓存（`CMakeCache.txt`）的复用与失效逻辑；
- `cmake/PreBuildSteps.cmake` 与 `cmake/Dependencies.cmake`，了解子模块（`third_party/`）初始化与第三方库探测；
- 当你开始改 C++ 源码时，回到本讲的「`make` 增量编译 + `pip install -e` 打包」组合，会显著缩短反馈循环。
