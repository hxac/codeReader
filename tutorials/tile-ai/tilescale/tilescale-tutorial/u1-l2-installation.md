# 环境搭建与安装

## 1. 本讲目标

上一讲（u1-l1）我们建立了全局认知：TileScale 是 TileLang 的分布式扩展，建立在 TVM 之上，**Python 包名是 `tilelang`**，单机能力由 `tilelang` 提供，分布式能力由 `tilescale_ext` + NVSHMEM 提供。

本讲的目标是让你把项目真正「跑起来」。读完本讲，你应该能够：

1. 从源码或 pip 安装 `tilelang`，并理解每一步命令在做什么。
2. 看懂 `pyproject.toml` 里的依赖清单，说清 `apache-tvm-ffi`、`torch`、`z3-solver`、`nvidia-nvshmem-cu12` 这些依赖各自的作用。
3. 根据 CUDA 版本选择正确的 Docker 镜像 / Dockerfile。
4. 用一句话讲清 `import tilelang` 时底层那个 `.so` 是怎么被找到并加载的，并能据此诊断「找不到库」这类常见报错。

> 与 u1-l1 一致，本讲也会区分「文档宣称的」和「代码实际的」。你会在后面看到：安装文档里写的 Python 版本要求和包元数据里写的不完全一致——这种细节正是诊断环境问题的关键。

## 2. 前置知识

本讲是纯环境搭建，不要求你会写 CUDA。但有几个名词先解释清楚：

| 名词 | 通俗解释 |
| --- | --- |
| `.so` 文件 | Linux 上的「共享库」（shared object），相当于 Windows 的 `.dll`。`tilelang` 的 C++ 编译器被编译成一个 `.so`，Python 通过它调用底层。 |
| ctypes | Python 标准库里用来加载 `.so` / `.dll` 的模块。`tilelang` 用 `ctypes.CDLL(...)` 加载自己的底层库。 |
| NVCC | NVIDIA 的 CUDA 编译器（`nvcc` 命令）。编译 `tilelang` 的 C++ 部分需要它，所以安装时要保证 `nvcc` 在 `PATH` 里。 |
| CUDA 版本 | 比如 12.1、12.4、12.8。驱动、`nvcc`、CUDA 运行时三者的大版本要对齐，否则编译或运行会出问题。 |
| HIP / ROCm | AMD GPU 的「CUDA 对应物」。TileScale 同时支持 NVIDIA（CUDA）和 AMD（HIP/ROCm）。 |
| 构建隔离（build isolation） | `pip install` 默认在一个隔离的临时环境里编译项目。`tilelang` 编译时需要 `torch`、`scikit-build-core` 等已就绪，所以源码安装要用 `--no-build-isolation`。 |

如果你不确定自己的 CUDA 版本，可以先跳到本讲 4.2 节，那里讲了怎么选镜像。

## 3. 本讲源码地图

本讲涉及的文件不多，但每个都直接关系到「能不能装上、能不能 import」：

| 文件 | 作用 |
| --- | --- |
| [docs/get_started/Installation.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/get_started/Installation.md) | 官方安装指南：前置条件、容器准备、源码安装三步、验证命令、NVSHMEM 构建。 |
| [pyproject.toml](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/pyproject.toml) | 项目元数据与运行时依赖清单（Python 版本、`apache-tvm-ffi`、`torch`、`z3-solver`、`nvidia-nvshmem-cu12` 等），以及 scikit-build 构建配置。 |
| [requirements.txt](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/requirements.txt) | 运行时依赖的精简列表（与 `pyproject.toml` 的 `dependencies` 基本一致，少了 NVSHMEM 项）。 |
| [requirements-dev.txt](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/requirements-dev.txt) | 源码安装（`--no-build-isolation`）所需的构建工具：`scikit-build-core`、`cmake`、`ninja`、`cython`、`cuda-python` 等。 |
| [docker/README.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docker/README.md) | Docker 镜像构建与运行说明（含 cu118~cu128 多个 Dockerfile 和 rocm 版）。 |
| [tilelang/libinfo.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/libinfo.py) | 库查找逻辑：`find_lib_path()` 决定 `tilelang` 去哪些目录找 `.so`。 |
| [tilelang/env.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/env.py) | 运行时环境：`TL_LIBS` 搜索路径、开发模式回退路径、CUDA/ROCm 探测、各种环境变量开关。 |
| [tilelang/__init__.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/__init__.py) | 包入口：计算 `__version__`、`import tvm`、`_load_tile_lang_lib()` 加载 `.so`、可选加载 `tilescale_ext`。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，对应规格里的三条主线：依赖与版本要求、CUDA/Docker 选择、底层 `.so` 的加载机制。

### 4.1 依赖与 Python 版本要求（pyproject.toml）

#### 4.1.1 概念说明

`tilelang` 不是一个纯 Python 包。它有大量 C++ 代码（编译器 pass、codegen、CUDA 模板），这些 C++ 在安装时被编译成共享库，再由 Python 调用。因此它的依赖分两层：

- **运行时依赖**（装好后 `import tilelang` 需要的）：定义在 `pyproject.toml` 的 `[project].dependencies` 里。
- **构建时依赖**（从源码编译 C++ 需要的）：定义在 `requirements-dev.txt` 里，源码安装时必须先用 `--no-build-isolation` 让它们可见。

理解这两层的区别，是排查「pip install 报错」的第一步。

#### 4.1.2 核心流程

源码安装的标准流程（来自 [Installation.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/get_started/Installation.md)）：

```
1. git clone --recursive ...        # 递归拉取，3rdparty/tvm 等子模块必须到位
2. pip install cuda-python==12.9    # 与你机器的 nvcc 版本对齐
3. pip install scikit-build-core CMake torch ninja Cython   # 装构建工具
4. pip install -e . --no-build-isolation   # 关闭隔离，用上一步装的工具编译本包
5. python -c "import tilelang; print(tilelang.__version__)"  # 验证
```

关键点：

- `--recursive` 不能少：TileLang 依赖 `3rdparty/tvm` 子模块（ vendored TVM），少拉了会编译失败或 `import` 失败。
- `--no-build-isolation` 不能少：因为 C++ 编译需要 `torch`（构建 `tilescale_ext._C` 要 libtorch）、`scikit-build-core`、`cython` 等，必须让 pip 看到当前环境里已装好的它们。

#### 4.1.3 源码精读

先看运行时依赖清单（[pyproject.toml:29-45](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/pyproject.toml#L29-L45)）：

```toml
requires-python = ">=3.9"
dependencies = [
    "apache-tvm-ffi~=0.1.0,>=0.1.6",   # TVM 的 FFI（外部函数接口）绑定
    "torch-c-dlpack-ext",               # 预编译的 torch 扩展，避免首次 import 时 JIT
    "cloudpickle",
    "ml-dtypes",
    "numpy>=1.23.5",
    "psutil",
    "torch",
    "torch>=2.7; platform_system == 'Darwin'",   # macOS 上要求更新的 torch
    "tqdm>=4.62.3",
    "typing-extensions>=4.10.0",
    "z3-solver>=4.13.0",                # SMT 求解器，用于自动调优/合法化推理
    "nvidia-nvshmem-cu12; platform_system == 'Linux'",  # NVSHMEM 设备库（仅 Linux）
]
```

每个核心依赖的作用：

| 依赖 | 作用 |
| --- | --- |
| `apache-tvm-ffi` | TVM 的「外部函数接口」。`tilelang` 建立在 vendored TVM 之上，Python 与 C++ 之间的函数调用、张量传递都靠它。 |
| `torch` | 既做张量容器（kernel 的输入输出用 `torch.Tensor`），构建 `tilescale_ext._C` 时还需要 libtorch。 |
| `z3-solver` | 微软的 SMT 求解器。TVM/TileLang 在做形状推导、边界检查、合法化时会用它做约束求解。 |
| `nvidia-nvshmem-cu12` | NVSHMEM 的设备端头/库（pip 包形式）。这是 u1-l1 讲到的「真正落地的分布式路线」的依赖——只有 Linux 上装。 |

再看构建时依赖（[requirements-dev.txt](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/requirements-dev.txt)），其中关键几行：

```text
cmake>=3.26
ninja
cython>=3.0.0
scikit-build-core
cuda-python>=12.0.0
auditwheel; platform_system == 'Linux'   # wheel 依赖修复
patchelf; platform_system == 'Linux'     # 修复 libz3.so 的 rpath
```

这些就是 `pip install -e . --no-build-isolation` 背后真正用到的工具链。

> **一个值得注意的版本不一致**：[Installation.md:9](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/get_started/Installation.md#L9) 写「Python Version: >= 3.7」，但 [pyproject.toml:5](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/pyproject.toml#L5) 实际是 `requires-python = ">=3.9"`，classifiers 也只列了 3.9–3.14。**以 `pyproject.toml` 为准**：用 3.7/3.8 装不上。这是「文档宣称 vs 包元数据」的典型差异，遇到报错时优先信元数据。

> 另一个容易踩的点：[Installation.md:10](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/get_started/Installation.md#L10) 提到「LLVM < 20 if you are using the bundled TVM submodule」——用 vendored TVM 编译时，LLVM 版本不能太新。

#### 4.1.4 代码实践

**实践目标**：搞清运行时依赖和构建依赖的区别，并发现文档与元数据的版本差异。

**操作步骤**：

1. 打开 [pyproject.toml](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/pyproject.toml)，记录 `requires-python` 的值。
2. 打开 [docs/get_started/Installation.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/get_started/Installation.md)，记录它写的 Python 版本要求。
3. 打开 [requirements-dev.txt](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/requirements-dev.txt)，找出源码安装时 `--no-build-isolation` 所需要的工具链（至少列出 5 个）。

**需要观察的现象**：三处对「Python 版本」的表述是否一致；`requirements-dev.txt` 与运行时 `requirements.txt` 有哪些不同。

**预期结果**：`pyproject.toml` 要求 `>=3.9`，而 `Installation.md` 写 `>=3.7`；`requirements-dev.txt` 比 `requirements.txt` 多了 `cmake`/`ninja`/`cython`/`scikit-build-core`/`cuda-python` 等构建工具。

**待本地验证**：是否在你机器上真能用 3.8 安装失败、用 3.10 安装成功（取决于具体环境，但元数据会直接拒绝 3.8）。

#### 4.1.5 小练习与答案

**练习 1**：为什么源码安装必须加 `--no-build-isolation`？

> **参考答案**：`tilelang` 编译 C++ 时需要 `torch`（构建 `tilescale_ext._C`）、`scikit-build-core`、`cython`、`cmake` 等已经装在当前环境里。pip 默认的构建隔离会把项目放进一个空临时环境编译，那里找不到这些工具，导致编译失败。`--no-build-isolation` 让 pip 直接用当前环境。

**练习 2**：`requirements.txt` 和 `pyproject.toml` 的 `dependencies` 几乎一样，但 `pyproject.toml` 多了一项，是哪一项？为什么？

> **参考答案**：多了 `nvidia-nvshmem-cu12; platform_system == 'Linux'`。它是 NVSHMEM 设备库，只有 Linux 才需要（分布式路线），且带平台标记。`requirements.txt` 是更精简的运行时列表，没有把它列进去。

### 4.2 CUDA 版本与 Docker 镜像对应关系

#### 4.2.1 概念说明

GPU 项目最大的环境痛点是「CUDA 版本对齐」：你的 NVIDIA 驱动、`nvcc` 编译器、CUDA 运行时库三者的大版本要能配套。对新手来说，最省心的方式是直接用官方容器（NVIDIA 的 `pytorch` 镜像已经装好驱动 + CUDA + torch），再在里面装 `tilelang`。

仓库提供了两条 Docker 路线：

1. **官方指南推荐的「基础容器」**：直接 `docker pull nvcr.io/nvidia/pytorch:25.03-py3`，在里面手动装 `tilelang`。这是 [Installation.md:14-22](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/get_started/Installation.md#L14-L22) 的做法。
2. **仓库自带 Dockerfile**：`docker/` 目录下有一整套按 CUDA 版本命名的 Dockerfile，一条 `docker build` 全部装好。这是 [docker/README.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docker/README.md) 的做法。

#### 4.2.2 核心流程

**路线 A（官方指南，灵活）**：

```
docker pull nvcr.io/nvidia/pytorch:25.03-py3
docker run --gpus=all --shm-size=10g --ipc=host ... -it nvcr.io/nvidia/pytorch:25.03-py3
# 容器内：装 conda、libstdcxx-ng，然后走 4.1 节的源码安装三步
```

**路线 B（仓库 Dockerfile，省心）**：

```
cd docker
docker build -t tilelang_workspace -f Dockerfile.cu124 .   # 按你的 CUDA 版本选
docker run --gpus all --shm-size=4G ... tilelang_workspace bash
```

#### 4.2.3 源码精读

`docker/` 目录下的 Dockerfile 按 CUDA 版本命名（[docker/README.md:6-9](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docker/README.md#L6-L9)）：

```
Dockerfile.cu118   Dockerfile.cu120   Dockerfile.cu121   Dockerfile.cu123
Dockerfile.cu124   Dockerfile.cu125   Dockerfile.cu126   Dockerfile.cu128
Dockerfile.rocm    (AMD GPU 用)
```

文件名里的数字就是 CUDA 版本，比如 `cu124` = CUDA 12.4、`cu128` = CUDA 12.8。AMD 显卡用 `Dockerfile.rocm`。

[docker/README.md:1-2](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docker/README.md#L1-L2) 说明这些镜像基于 Ubuntu 20.04，包含运行实验所需的全部依赖，只提供 NVIDIA 版本（AMD 版需另行索取）。

运行容器时的关键参数（[docker/README.md:11-12](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docker/README.md#L11-L12)）：

| 参数 | 作用 |
| --- | --- |
| `--gpus all` | 把宿主机 GPU 透传给容器（NVIDIA）。AMD 用 `--device=/dev/kfd --device=/dev/dri`。 |
| `--shm-size=4G`（或 10g） | 扩大 `/dev/shm`。多进程数据加载、NCCL/NVSHMEM 通信都需要足够大的共享内存。 |
| `--ipc=host` | 共享宿主机 IPC 命名空间，多进程训练/分布式常需要。 |
| `--cap-add=SYS_PTRACE` | 允许容器内调试（ptrace），便于排查。 |

> 选哪个 CUDA 版本？看你的驱动支持到多少。原则：**容器 CUDA 版本 ≤ 驱动支持的最大版本**。新驱动可跑旧 CUDA，反之不行。

#### 4.2.4 代码实践

**实践目标**：根据本机 CUDA 版本选对 Dockerfile，并理解运行参数。

**操作步骤**：

1. 在宿主机执行 `nvidia-smi`，看右上角 `CUDA Version:`（这是驱动支持的最高 CUDA 版本）。
2. 在 `docker/` 目录下挑一个 `Dockerfile.cuXXX`，使 `XXX` ≤ 上一步看到的版本（没有精确匹配就选最接近且更低的）。
3. 阅读该 Dockerfile（可选），确认它装了 `nvcc`、`torch`、`cmake` 等。
4. 按 [docker/README.md:9-12](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docker/README.md#L9-L12) 构建（`docker build -t tilelang_workspace -f Dockerfile.cuXXX .`）并运行。

**需要观察的现象**：`nvidia-smi` 报告的驱动 CUDA 版本；`docker build` 是否顺利完成（约 10+ 分钟）。

**预期结果**：进入容器后 `nvcc --version` 与你选的 `cuXXX` 一致；`python --version` ≥ 3.9。

**待本地验证**：实际构建耗时、是否能进入容器，取决于你机器的 GPU 驱动与网络。

#### 4.2.5 小练习与答案

**练习 1**：你的驱动显示 `CUDA Version: 12.6`，能直接用 `Dockerfile.cu128` 吗？

> **参考答案**：不能（很可能跑不起来）。12.8 > 12.6，容器里的 CUDA 运行时版本不能超过驱动支持的最高版本。应选 `cu126` 或更低的 Dockerfile。

**练习 2**：为什么分布式示例的容器启动要加 `--shm-size`？

> **参考答案**：NVSHMEM / NCCL 等多 GPU 通信库和 PyTorch 的多进程数据加载会大量使用 `/dev/shm`（共享内存）。默认的 64MB 太小，容易 OOM 或报错，所以要显式调大（4G 甚至 10G）。这和 [Installation.md:18](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/get_started/Installation.md#L18) 用 `--shm-size=10g` 是一个道理。

### 4.3 tilelang .so 的加载（libinfo.find_lib_path）

#### 4.3.1 概念说明

`import tilelang` 不是单纯加载 Python 代码。它会做一件关键的事：找到并加载一个 C++ 共享库（`libtilelang.so` 或 `libtilelang_module.so`）。这个 `.so` 里就是 TileLang 的编译器核心（pass、codegen）。

如果这一步失败，你会看到类似 `Cannot find libraries: libtilelang.so ...` 的报错。理解加载路径是怎么决定的，就能自己诊断这类问题。

#### 4.3.2 核心流程

`import tilelang` 时的加载链：

```
tilelang/__init__.py
  ├─ import .env          → 计算 TL_LIBS（去哪些目录找库）
  ├─ import tvm           → 先把 vendored TVM 拉进 sys.path 并 import
  ├─ from . import libinfo → 拿到 find_lib_path 工具
  └─ _load_tile_lang_lib()
        ├─ 选库名: "tilelang" (runtime-only) 或 "tilelang_module" (完整)
        ├─ libinfo.find_lib_path(name) → 在 TL_LIBS 里拼出 lib<name>.so
        └─ ctypes.CDLL(path) → 真正加载 .so
```

整个过程由环境变量 `SKIP_LOADING_TILELANG_SO` 控制：只有当它不等于 `"0"` 时才跳过加载（用于特殊调试场景）。

#### 4.3.3 源码精读

**第一步：搜索路径 `TL_LIBS` 怎么来**（[tilelang/env.py:21-44](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/env.py#L21-L44)）：

```python
TL_ROOT = os.path.dirname(os.path.abspath(__file__))
TL_LIBS = [os.path.join(TL_ROOT, "lib")]          # 正常安装：包目录下的 lib/
TL_LIBS = [i for i in TL_LIBS if os.path.exists(i)]

if not os.path.exists(THIRD_PARTY_ROOT):           # 开发模式：3rdparty 不在包里
    DEV = True
    dev_lib_root = os.path.join(tl_dev_root, "build")
    TL_LIBS = [os.path.join(dev_lib_root, "lib"),   # build/lib
               os.path.join(dev_lib_root, "tvm")]   # build/tvm
```

也就是说：
- **pip 安装**的 `tilelang`，库在 `tilelang/lib/` 下。
- **源码 `pip install -e`**（开发模式）的，库在仓库根目录的 `build/lib` 和 `build/tvm` 下。这也是为什么 `pip install -e .` 之后 `import tilelang` 能从 `build/` 找到 `.so`。

**第二步：`find_lib_path` 怎么拼文件名**（[tilelang/libinfo.py:7-35](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/libinfo.py#L7-L35)）：

```python
def find_lib_path(name: str, py_ext=False):
    if py_ext:
        lib_name = f"{name}.abi3.so"
    elif sys.platform.startswith("linux"):       # Linux
        lib_name = f"lib{name}.so"
    elif sys.platform.startswith("win32"):       # Windows
        lib_name = f"{name}.dll"
    elif sys.platform.startswith("darwin"):      # macOS
        lib_name = f"lib{name}.dylib"
    ...
    for lib_root in TL_LIBS:                      # 逐个候选目录找
        lib_dll_path = os.path.join(lib_root, lib_name)
        if os.path.exists(lib_dll_path) and os.path.isfile(lib_dll_path):
            return lib_dll_path
    else:
        raise RuntimeError(f"Cannot find libraries: {lib_name}\n"
                           + "List of candidates:\n" + "\n".join(TL_LIBS))
```

注意它**按平台拼出不同后缀**（Linux `.so` / Windows `.dll` / macOS `.dylib`），并在找不到时抛出 `RuntimeError` 并列出所有候选目录——这正是你排查「找不到库」时的第一手信息。

**第三步：入口里真正调用加载**（[tilelang/__init__.py:126-140](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/__init__.py#L126-L140)）：

```python
def _load_tile_lang_lib():
    if sys.platform.startswith("win32") and sys.version_info >= (3, 8):
        for path in libinfo.get_dll_directories():
            os.add_dll_directory(path)
    lib_name = "tilelang" if tvm.base._RUNTIME_ONLY else "tilelang_module"
    lib_path = libinfo.find_lib_path(lib_name)
    return ctypes.CDLL(lib_path), lib_path

# only load once here
if env.SKIP_LOADING_TILELANG_SO == "0":
    _LIB, _LIB_PATH = _load_tile_lang_lib()
```

两个要点：

1. **库名取决于 TVM 模式**：`tvm.base._RUNTIME_ONLY` 为真时加载精简的 `libtilelang.so`，否则加载带编译能力的 `libtilelang_module.so`。
2. **`SKIP_LOADING_TILELANG_SO`**（[env.py:255](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/env.py#L255)）等于 `"0"` 才加载。设成 `1` 可以在「只想用 Python 侧、不调底层」的特殊场景跳过加载。

加载 `.so` 之后，`__init__.py` 才继续 `import` 各个子模块（`jit`、`profiler`、`language`、`engine` 等），并**可选地**加载分布式扩展 `tilescale_ext`（[tilelang/__init__.py:151-158](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/__init__.py#L151-L158)）：

```python
# TileScale distributed extensions (optional - only available when tilescale_ext is installed)
try:
    from .utils.tensor import tensor
    from .utils.allocator import get_allocator
except ImportError:
    # tilescale_ext not installed - distributed features unavailable
    tensor = None
    get_allocator = None
```

这条 `try/except` 正是 u1-l1 讲到的「单机 `tilelang` + 可选分布式 `tilescale_ext`」在代码里的体现：分布式扩展没装时，`import tilelang` 仍然成功，只是 `tensor`/`get_allocator` 为 `None`。

> 版本号怎么来的？[tilelang/__init__.py:11-48](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/__init__.py#L11-L48) 的 `_compute_version()` 优先读仓库根目录的 `VERSION` 文件（源码检出），否则用 `importlib.metadata` 取已安装发行版版本。当前仓库 `VERSION` 内容为 `0.1.7.post1`。

#### 4.3.4 代码实践

**实践目标**：亲手验证 `.so` 加载机制，并学会读 `find_lib_path` 的报错。

**操作步骤**（在装好 `tilelang` 的环境里）：

1. 正常验证：`python -c "import tilelang; print(tilelang.__version__)"`。
2. 找到 `.so` 的实际位置：`python -c "import tilelang; from tilelang import libinfo; print(libinfo.find_lib_path('tilelang'))"`（开发模式会指向 `build/lib/...`）。
3. 故意跳过加载：`SKIP_LOADING_TILELANG_SO=1 python -c "import tilelang; print('imported but so skipped')"`，观察是否还能 import（底层 C++ 调用会失败）。
4. （进阶）在 `python -c "import tilelang"` 时临时把 `TL_LIBS` 指向的目录改名，复现 `RuntimeError: Cannot find libraries: libtilelang.so ...`，阅读它列出的候选目录。

**需要观察的现象**：
- 步骤 1 应打印版本号（如 `0.1.7.post1`）。
- 步骤 2 应返回一个 `.so` 的绝对路径。
- 步骤 3：`import` 本身可能仍成功（因为加载被跳过），但任何真正调用 C++ 的操作（如 `tilelang.lower`）会报错。
- 步骤 4：报错信息会列出所有它找过的目录。

**预期结果**：你能用一句话解释「`tilelang` 去 `TL_LIBS` 里找 `libtilelang.so`」，并知道报错信息里的候选目录从哪来。

**待本地验证**：步骤 3、4 的确切报错信息取决于环境与 TVM 模式，建议本地实际跑一遍记录。

#### 4.3.5 小练习与答案

**练习 1**：源码 `pip install -e .` 之后，`import tilelang` 从哪里找 `.so`？为什么不在 `tilelang/lib/`？

> **参考答案**：从仓库根目录的 `build/lib/`（和 `build/tvm/`）找。因为开发模式下 `3rdparty` 不在包目录里，`tilelang/env.py` 检测到 `THIRD_PARTY_ROOT` 不存在，就把 `TL_LIBS` 回退到 `build/lib`、`build/tvm`（CMake 的产物输出目录）。`tilelang/lib/` 是 pip wheel 安装时才有的布局。

**练习 2**：报错 `Cannot find libraries: libtilelang.so` 时，应该看报错里的什么信息来定位？

> **参考答案**：看报错末尾「List of candidates:」列出的候选目录（即 `TL_LIBS`）。如果候选目录里确实没有 `libtilelang.so`，说明 C++ 编译没产出（`pip install -e .` 失败或 `build/` 被清理）；如果候选目录本身不对，说明 `TL_LIBS` 计算异常（比如误进了开发模式）。

## 5. 综合实践

把本讲三个模块串起来，完成一次「从零安装到验证」的全流程：

1. **选环境**：按 4.2 节确认本机 CUDA 版本，挑一个 `Dockerfile.cuXXX`（或用官方 `pytorch` 镜像），`docker run` 进入容器，确认 `nvcc --version` 与 `python --version`。
2. **装构建工具**：在容器内 `pip install scikit-build-core CMake torch ninja Cython`（对应 4.1 节的构建依赖）。
3. **装 tilelang**：`git clone --recursive` 拉仓库后，`pip install -e . --no-build-isolation`（注意 `--recursive` 和 `--no-build-isolation` 都不能少）。
4. **验证版本**：运行规格要求的命令，确认能 import 并打印版本：
   ```bash
   python -c "import tilelang; print(tilelang.__version__)"
   ```
5. **理解依赖**：对照 [pyproject.toml:29-45](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/pyproject.toml#L29-L45)，记录四个依赖的作用：
   - `apache-tvm-ffi`：TVM 的 Python↔C++ 外部函数接口，`tilelang` 建在 vendored TVM 之上。
   - `torch`：张量容器（kernel I/O）+ 构建 `tilescale_ext._C` 所需的 libtorch。
   - `z3-solver`：SMT 求解器，供编译期形状/边界推理与合法化使用。
   - `nvidia-nvshmem-cu12`：NVSHMEM 设备库（仅 Linux），是已落地的分布式路线的依赖。
6. **追踪加载**（衔接 4.3 节）：用 `SKIP_LOADING_TILELANG_SO=1` 跑一次，对比正常 import 的差异，确认 `.so` 是在 `import` 阶段被 `ctypes.CDLL` 加载的。

**验收标准**：版本号能打印；你能口头说清「`.so` 来自 `TL_LIBS`、库名取决于 TVM 运行时模式、分布式扩展是可选的」。

> 如果只关心单机 kernel（Unit 2 的内容），到第 5 步就够了。要用 NVSHMEM 分布式能力，还需额外按 [Installation.md:58-79](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/get_started/Installation.md#L58-L79) 构建 NVSHMEM 库并安装 `pynvshmem`——这属于 Unit 6 的范畴，本讲先不展开。

## 6. 本讲小结

- `tilelang` 不是纯 Python 包，安装时要把 C++ 编译成 `.so`；源码安装必须 `--recursive` 拉子模块、用 `--no-build-isolation` 让构建工具可见。
- 运行时依赖清单在 [pyproject.toml:29-45](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/pyproject.toml#L29-L45)：`apache-tvm-ffi`（FFI）、`torch`（张量+libtorch）、`z3-solver`（约束求解）、`nvidia-nvshmem-cu12`（分布式，仅 Linux）。
- **Python 版本以 `pyproject.toml` 的 `>=3.9` 为准**，安装文档里写的 `>=3.7` 已过时；用 vendored TVM 时 LLVM 要 < 20。
- CUDA 版本对齐是头号痛点：最省心是走 Docker（`docker/` 下有 cu118~cu128 和 rocm 版 Dockerfile），选 `cuXXX` 时「容器 CUDA ≤ 驱动 CUDA」。
- `import tilelang` 会通过 `tilelang/env.py` 算出 `TL_LIBS`，再用 `tilelang/libinfo.py:find_lib_path()` 找到 `libtilelang.so` / `libtilelang_module.so` 并 `ctypes.CDLL` 加载；找不到时报 `RuntimeError` 并列出候选目录。
- 开发模式（`pip install -e`）下库在 `build/lib`，pip 安装下在 `tilelang/lib`；`SKIP_LOADING_TILELANG_SO=1` 可跳过加载用于调试。
- 分布式扩展 `tilescale_ext` 是**可选**的：没装时 `import tilelang` 仍成功，只是 `tensor`/`get_allocator` 为 `None`。

## 7. 下一步学习建议

环境就绪后，下一步是「跑通第一个 kernel 并看懂它」。建议：

1. 先读 **u1-l3《第一个 kernel：quickstart 详解》**，通过 `examples/quickstart.py` 的 matmul+relu 端到端理解一个 TileLang 程序（`T.Kernel`、`T.copy`、`T.gemm`、`T.Pipelined`、`@tilelang.jit`）。
2. 再读 **u1-l4《仓库结构与入口文件地图》**，把本讲零散提到的目录（`src/`、`tilelang/`、`tilescale_ext/`、`3rdparty/`）串成一张完整地图。
3. 如果你想先验证「能不能编译出一个真 kernel」，可以现在就试着 `python examples/quickstart.py`，看它是否顺利跑出与 torch 对齐的结果（需要本机有 NVIDIA GPU）。

后续 Unit 2 会系统讲 `T.*` 编程原语；Unit 3 进入编译流水线（本讲 4.3 节加载的那个 `.so` 里就是编译器）。分布式相关（NVSHMEM 构建、`pynvshmem`、`tilescale_ext`）则在 Unit 6 展开。
