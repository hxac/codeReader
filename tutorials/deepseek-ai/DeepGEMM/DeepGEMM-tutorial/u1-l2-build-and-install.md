# 环境要求与构建安装

## 1. 本讲目标

上一讲（u1-l1）我们建立了对 DeepGEMM 的整体认知：它是一个面向 SM90/SM100 的统一高性能张量核 kernel 库，核心设计哲学是「安装时不做 CUDA 编译，所有设备 kernel 都在运行时用 JIT 编译」。

本讲要回答一个非常现实的问题：**这么一个库，到底怎么装到我的机器上、怎么让 `import deep_gemm` 跑通？**

学完本讲你将能够：

- 说出 DeepGEMM 的运行时与编译期依赖，并能核对当前环境是否满足。
- 看懂 `setup.py` 如何用 PyTorch 的 `CUDAExtension` 把 `csrc/python_api.cpp` 编译成 `_C` 扩展、如何把 CUTLASS/CuTe 头文件打进包、如何生成 `.pyi` 与默认 `envs.py`。
- 区分 `develop.sh`（原地开发构建 + 软链 `.so`）与 `install.sh`（打 wheel + `pip install`）两条构建入口，理解预编译 wheel 下载与本地源码构建的取舍。
- 理解仓库里那份 `CMakeLists.txt` **并不参与真实编译**，它只是为了让 CLion 等 CMake 类 IDE 能对 CUDA 设备代码做索引。

## 2. 前置知识

在进入源码前，先用通俗语言把几个概念讲清楚：

- **Python C/C++ 扩展（`_C` 模块）**：Python 可以通过 pybind11 等工具调用 C++ 代码。DeepGEMM 在 Python 层 `import deep_gemm` 时，背后其实是 `from . import _C`，这个 `_C` 是一个编译好的动态链接库（`.so` 文件），里面是用 C++ 写的宿主逻辑（参数校验、架构派发、JIT 编译器等）。
- **`setup.py` / `setuptools` / wheel**：`setup.py` 是 Python 传统的打包脚本，`setuptools` 是它依赖的库。`python setup.py build` 只编译、`bdist_wheel` 打出一个 `.whl` 安装包、`pip install xxx.whl` 把包装进 Python 环境里。wheel 本质上是一个 zip，里面既有 Python 代码也有编译好的 `.so`。
- **git submodule（子模块）**：一个 git 仓库可以把另一个仓库作为「子模块」挂进来。DeepGEMM 把 NVIDIA 的 [CUTLASS](https://github.com/NVIDIA/cutlass.git) 和 [fmtlib/fmt](https://github.com/fmtlib/fmt.git) 作为子模块放在 `third-party/` 下，所以克隆时必须加 `--recursive`，否则 `third-party/cutlass`、`third-party/fmt` 是空目录，编译会失败。
- **CUDA Toolkit / nvcc / NVRTC**：CUDA Toolkit 是 NVIDIA 的 GPU 编译与运行时工具集。其中 `nvcc` 是离线编译器（编译 `.cu` 成机器码），`nvrtc`（NVRTC）是运行时编译器（在程序运行过程中把源码编译成 GPU 机器码）。DeepGEMM 的 JIT 正是依赖这两个之一——所以 CUDA Toolkit 既是构建依赖，也是运行时依赖。
- **C++ ABI（cxx11abi）**：C++ 标准库有两套 ABI（`_GLIBCXX_USE_CXX11_ABI` 取 0 或 1）。PyTorch 编译时用了哪套 ABI，所有要链接 PyTorch 的扩展就必须用同一套，否则符号不兼容。`setup.py` 会读取 `torch.compiled_with_cxx11_abi()` 自动对齐。
- **TMA / 张量核**：这些是 GPU 硬件能力，本讲不展开（属于 u1-l1 与后续设备内核章节）。你只需知道：DeepGEMM 的 `_C` 扩展本身**几乎不含 GPU kernel 代码**，真正的 tensor core 计算代码是在调用时由 JIT 临时编译的。

> 一句话承接 u1-l1：DeepGEMM 的「轻量」哲学直接决定了它的构建方式——**安装阶段只编译一个很薄的 C++ 宿主模块 `_C`，所有重型 CUDA kernel 都推迟到运行时 JIT**。本讲讲的正是这个「薄宿主模块」是怎么被构建与安装的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md) | 依赖清单（Requirements）与两种构建入口的官方说明 |
| [setup.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/setup.py) | 真正的构建脚本：定义 `CUDAExtension`、版本号、wheel 下载、`.pyi`/`envs.py` 生成 |
| [develop.sh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/develop.sh) | 开发构建入口：链接头文件 + `setup.py build` + 软链 `.so` |
| [install.sh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/install.sh) | 安装构建入口：`setup.py bdist_wheel` + `pip install` |
| [CMakeLists.txt](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/CMakeLists.txt) | **仅供 CMake 类 IDE（如 CLion）索引设备代码**，不参与真实编译 |
| [deep_gemm/__init__.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py) | `import` 时调用 `_C.init(...)`，衔接构建产物与运行时 |

## 4. 核心概念与源码讲解

### 4.1 依赖与环境要求

#### 4.1.1 概念说明

一个 CUDA 库的依赖通常分两层：

- **编译期依赖（构建时需要）**：编译器（支持 C++20）、CUDA Toolkit（提供 `nvcc`/头文件）、被链接的第三方库（CUTLASS、fmt）。
- **运行时依赖（运行时需要）**：目标 GPU 硬件、CUDA 驱动、PyTorch，以及 JIT 所需的 `nvcc`/`nvrtc` 与随包分发的设备头文件。

DeepGEMM 的特殊之处在于：因为它是 JIT 编译，**CUDA Toolkit 在运行时也要存在**（否则无法在调用 GEMM 时即时编译 kernel）。此外，设备 kernel 源码（一堆 `.cuh` 头文件）和 CUTLASS/CuTe 头文件必须被打进安装包，供运行时 JIT 读取。

#### 4.1.2 核心流程

下表来自 README 的 Requirements，是核对环境的依据：

| 依赖项 | 要求 |
| --- | --- |
| GPU 架构 | NVIDIA SM90（Hopper）或 SM100（Blackwell） |
| Python | 3.8 或更高 |
| 编译器 | 支持 C++20 |
| CUDA Toolkit | SM90 需 12.3+（**官方推荐 12.9+ 以获得最佳性能**）；SM100 需 12.9+ |
| PyTorch | 2.1 或更高 |
| CUTLASS | 4.0+（git submodule） |
| `{fmt}` | git submodule |

注意一个**容易踩坑的点**：CUTLASS 和 `{fmt}` 是 git 子模块，存放在 `third-party/` 下。普通 `git clone` 不会拉取子模块内容，`third-party/cutlass/`、`third-party/fmt/` 会是空目录，导致编译找不到头文件。所以官方要求用 `git clone --recursive`。

#### 4.1.3 源码精读

依赖清单见 README 的 Requirements 段落：[README.md:27-38](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L27-L38)，这里逐条列出了 GPU、Python、编译器、CUDA、PyTorch、CUTLASS、fmt 的版本要求。

子模块定义在 `.gitmodules` 中，确认了 CUTLASS 与 fmt 的来源：

```
[submodule "third-party/cutlass"]
    path = third-party/cutlass
    url = https://github.com/NVIDIA/cutlass.git
[submodule "third-party/fmt"]
    path = third-party/fmt
    url = https://github.com/fmtlib/fmt.git
```

`setup.py` 则把上述依赖「翻译」成了编译器能用的参数。构建时需要的头文件目录与链接库都在这里声明：[setup.py:35-48](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/setup.py#L35-L48)。关键几行（说明）：

- `sources = ['csrc/python_api.cpp']`：**整个 `_C` 扩展只编译这一个 C++ 文件**，体量很小。
- `build_include_dirs` 包含 `CUDA_HOME/include`、`deep_gemm/include`、`third-party/cutlass/include`、`third-party/fmt/include`——这就是为什么子模块必须存在。
- `build_libraries = ['cudart', 'nvrtc']`：链接 CUDA 运行时库与 NVRTC 库，后者正是 JIT 编译器在运行时要调用的。

#### 4.1.4 代码实践

**实践目标**：在动手编译前，先核对当前环境是否满足 Requirements。

**操作步骤**：

1. 查看 GPU 架构：`nvidia-smi --query-gpu=compute_cap --format=csv`（SM90 → 9.0，SM100 → 10.0）。
2. 查看 CUDA 版本：`nvcc --version`。
3. 查看 Python 与 PyTorch：
   ```bash
   python --version
   python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
   ```
4. 确认子模块已拉取：`ls third-party/cutlass/include` 应能看到 `cutlass/`、`cute/` 等目录，而不是空的。

**需要观察的现象 / 预期结果**：GPU 计算能力为 9.0 或 10.0；CUDA 版本满足上表；PyTorch ≥ 2.1 且 `torch.cuda.is_available()` 为 True；`third-party/cutlass/include` 非空。

> 待本地验证：本讲义编写环境无 GPU、子模块未拉取，上述命令的实际输出需在你自己的机器或容器中确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `git clone`（不加 `--recursive`）之后直接 `./develop.sh` 大概率会失败？
**参考答案**：因为 CUTLASS 与 `{fmt}` 是子模块，不加 `--recursive` 时 `third-party/cutlass`、`third-party/fmt` 为空目录，`setup.py` 的 `build_include_dirs` 指向 `third-party/cutlass/include` 会找不到头文件，编译报错。补救办法是 `git submodule update --init --recursive`。

**练习 2**：为什么说 CUDA Toolkit 对 DeepGEMM 既是编译期依赖又是运行时依赖？
**参考答案**：编译期需要 `nvcc` 和头文件来编译 `_C` 宿主模块；运行时因为采用 JIT，需要 `nvcc` 或 `nvrtc` 来即时编译设备 kernel，还需要随包分发的设备头文件。

---

### 4.2 CUDAExtension 构建：从源码到 `_C` 扩展

#### 4.2.1 概念说明

`CUDAExtension` 是 PyTorch（`torch.utils.cpp_extension`）提供的一个便利封装，用来把 C++/CUDA 源码编译成可被 Python `import` 的扩展模块。DeepGEMM 用它把 `csrc/python_api.cpp` 编译成名叫 `deep_gemm._C` 的 pybind11 模块。

最关键的一点（再次呼应 JIT 哲学）：**构建阶段只编译这一个薄薄的宿主 C++ 文件**。那些动辄上千行的 tensor core kernel（`.cuh`）在安装时**一个都不编译**——它们以头文件形式随包分发，等到你在 Python 里真正调用 GEMM 时才被 JIT 即时编译。这就是为什么 DeepGEMM 「安装时不需要 CUDA 编译」却又性能极高的原因。

#### 4.2.2 核心流程

构建 `_C` 的核心流程可以用伪代码表示：

```
get_ext_modules()
  └─ 若 DG_SKIP_CUDA_BUILD=1 → 返回 []（跳过 CUDA 编译）
  └─ 否则 → CUDAExtension(
        name='deep_gemm._C',
        sources=['csrc/python_api.cpp'],
        include_dirs=[CUDA/include, deep_gemm/include, cutlass/include, fmt/include],
        libraries=['cudart', 'nvrtc'],
        extra_compile_args=[-std=c++17, -O3, -fPIC, ...])
```

除了编译扩展本身，`setup.py` 还自定义了一个 `build_py` 子类 `CustomBuildPy`，在常规构建之前做三件准备工作：

1. `prepare_includes()`：把 `cute`、`cutlass` 两个头文件目录复制到构建目录 `build_lib/deep_gemm/include/` 下，这样它们能随包分发，供运行时 JIT 使用。
2. `generate_default_envs()`：把构建时存在的 `DG_JIT_CACHE_DIR`、`DG_JIT_PRINT_COMPILER_COMMAND`、`DG_JIT_CPP_STANDARD` 三个环境变量「烧」进一个 `envs.py`，作为包的默认配置。
3. `generate_pyi_file()`：用 `scripts/generate_pyi.py` 为 `_C` 生成类型存根 `_C.pyi`，方便 IDE 自动补全。

#### 4.2.3 源码精读

`get_ext_modules()` 是扩展的入口，`DG_SKIP_CUDA_BUILD` 可完全跳过编译：[setup.py:102-111](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/setup.py#L102-L111)。注意 `name='deep_gemm._C'` 决定了最终在 Python 里写 `from deep_gemm import _C`。

编译选项与 ABI 对齐在这里，`-D_GLIBCXX_USE_CXX11_ABI` 的值直接取自当前 PyTorch，保证与 PyTorch 同 ABI：[setup.py:28-31](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/setup.py#L28-L31)。

`CustomBuildPy.run()` 串起了上述三步准备工作再走标准 `build_py`：[setup.py:114-126](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/setup.py#L114-L126)。其中：

- 复制 CUTLASS/CuTe 头文件进构建目录（让它们随 wheel 分发）：[setup.py:149-165](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/setup.py#L149-L165)。
- 生成默认 `envs.py`（烧入构建期环境变量）：[setup.py:140-147](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/setup.py#L140-L147)。
- 生成并复制 `_C.pyi` 类型存根：[setup.py:128-138](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/setup.py#L128-L138)。

最后，`setup()` 的 `package_data` 明确把这些随包分发的数据文件（设备头文件、CUTLASS、CuTe）写进 wheel，运行时 JIT 就靠它们：[setup.py:201-207](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/setup.py#L201-L207)。

#### 4.2.4 代码实践

**实践目标**：直观体会「构建阶段只编译一个 C++ 文件」，并验证 `DG_SKIP_CUDA_BUILD` 的作用。

**操作步骤**：

1. 正常开发构建（见 4.3），构建完成后查看产物：
   ```bash
   find build -name "*.so" -type f
   ```
   预期只看到一个形如 `build/.../deep_gemm/_C.cpython-xx-...so` 的文件，且 `build/.../deep_gemm/include/` 下能看到被复制进来的 `cutlass/`、`cute/`、`deep_gemm/` 头文件。
2. 对比跳过 CUDA 编译的情形：
   ```bash
   DG_SKIP_CUDA_BUILD=1 python setup.py build
   ```
   此时 `get_ext_modules()` 返回 `[]`，不会编译 `.so`。

**需要观察的现象 / 预期结果**：正常构建产出一个 `.so` 且 include 目录被填充；`DG_SKIP_CUDA_BUILD=1` 时无 `.so` 产出。

> 待本地验证：构建命令需在具备 GPU 工具链的环境运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 DeepGEMM 安装很快，而第一次调用某个形状的 GEMM 时会有一次明显延迟？
**参考答案**：安装只编译了薄宿主模块 `_C`，所以快；第一次调用时 JIT 才针对该形状即时编译设备 kernel（生成 cubin 并缓存），所以有首次延迟，后续相同形状命中缓存即快。

**练习 2**：`generate_default_envs()` 生成的 `envs.py` 在运行时如何被使用？
**参考答案**：`deep_gemm/__init__.py` 在 import 时会 `from .envs import persistent_envs`，并把其中每个键「仅在未设置时」写入 `os.environ`，从而把构建期的 JIT 默认配置（缓存目录、CPP 标准、是否打印编译命令）变成运行时默认值。

---

### 4.3 两种构建入口：develop.sh、install.sh 与 wheel 下载

#### 4.3.1 概念说明

DeepGEMM 提供两个 shell 入口，对应两种使用场景：

- **`develop.sh`（开发模式）**：面向库的开发者。它链接 CUTLASS 头文件、执行 `python setup.py build`，然后把编译出的 `.so` **软链**到 `deep_gemm/` 目录下。因为是软链，你修改 Python 代码或重新 build 后，`import deep_gemm` 立刻反映最新改动，无需反复 `pip install`。
- **`install.sh`（安装模式）**：面向使用者。它执行 `python setup.py bdist_wheel` 打出一个 wheel，再 `pip install dist/*.whl --force-reinstall` 装进当前 Python 环境。

此外，`setup.py` 还内置了**预编译 wheel 下载**机制：`bdist_wheel` 被替换成 `CachedWheelsCommand`，在满足条件时会直接从 GitHub Releases 下载官方预编译好的 wheel，省去本地编译；下载失败则回退到本地源码构建。

#### 4.3.2 核心流程

`develop.sh` 的流程：

```
cd 项目根目录
ln -sf cutlass/include/cutlass  deep_gemm/include   # 链接头文件
ln -sf cutlass/include/cute      deep_gemm/include
rm -rf build dist *.egg-info
python setup.py build                              # 只编译，原地
找到 build 下的 *.so，软链到 deep_gemm/            # 供原地 import
```

`install.sh` 的流程：

```
cd 项目根目录
rm -rf build dist *.egg-info
python setup.py bdist_wheel                        # 触发 CachedWheelsCommand
pip install dist/*.whl --force-reinstall           # 装进 site-packages
```

`CachedWheelsCommand`（即 `bdist_wheel` 的真实行为）的决策树：

```
if DG_FORCE_BUILD 或 DG_USE_LOCAL_VERSION:
    本地源码构建（super().run()）
else:
    尝试从 GitHub Releases 下载预编译 wheel
    成功 → 直接用下载的 wheel
    失败(HTTPError/URLError) → 回退本地源码构建
```

**一个重要细节**：`DG_USE_LOCAL_VERSION` 的默认值是 `'1'`（即开启）。也就是说，在默认配置下，即使跑 `install.sh` 也会走**本地源码构建**而非下载预编译 wheel。若想真正触发预编译 wheel 下载，需要显式 `DG_USE_LOCAL_VERSION=0`（并可配合 `DG_FORCE_BUILD=0`）。

#### 4.3.3 源码精读

三个控制构建行为的开关，注意 `DG_USE_LOCAL_VERSION` 默认为 `1`：[setup.py:22-25](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/setup.py#L22-L25)。

`develop.sh` 全文很短，核心是链接头文件、`setup.py build`、软链 `.so`：[develop.sh:1-26](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/develop.sh#L1-L26)。其中链接 CUTLASS 头文件的两行 [develop.sh:7-8](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/develop.sh#L7-L8)、`setup.py build` [develop.sh:13](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/develop.sh#L13)、把 `.so` 软链进 `deep_gemm/` [develop.sh:15-18](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/develop.sh#L15-L18)。

`install.sh` 同样简短，`bdist_wheel` + `pip install`：[install.sh:1-13](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/install.sh#L1-L13)，关键是 [install.sh:9-10](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/install.sh#L9-L10)。

预编译 wheel 下载逻辑 `CachedWheelsCommand`：先判断是否强制本地构建，否则尝试 `urllib` 下载，失败回退源码构建：[setup.py:168-192](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/setup.py#L168-L192)。

下载的 wheel 文件名由 `get_wheel_url()` 拼接，把 CUDA 大版本、torch 版本、Python 版本、cxx11abi、平台都编码进文件名，确保下载到与本环境匹配的包：[setup.py:83-99](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/setup.py#L83-L99)。下载基址 `base_wheel_url` 指向 GitHub Releases：[setup.py:51](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/setup.py#L51)。

版本号 `get_package_version()` 从 `deep_gemm/__init__.py` 读取 `__version__`，并在 `DG_USE_LOCAL_VERSION` 开启时追加 git 短 SHA（要求工作区干净，否则退化为 `+local`）：[setup.py:54-73](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/setup.py#L54-L73)。例如当前版本 `2.6.1` 在干净工作区下会变成 `2.6.1+<短SHA>`。

#### 4.3.4 代码实践

**实践目标**：完成一次本地开发构建，记录产物路径，并理解 wheel 下载开关。

**操作步骤**：

1. 确保已 `git clone --recursive`（或 `git submodule update --init --recursive`）。
2. 运行开发构建并观察产物：
   ```bash
   ./develop.sh
   ls -l deep_gemm/*.so          # 应看到一个指向 build/.../deep_gemm/_C...so 的软链
   ```
3. 对照预编译 wheel 路径（可选）：先看会拼出什么 URL，再决定是否下载：
   ```bash
   # 仅用于理解 get_wheel_url 拼出的文件名，不会真的安装
   python -c "import setup" 2>/dev/null || true
   ```
   实际想触发下载需：`DG_USE_LOCAL_VERSION=0 python setup.py bdist_wheel`，观察日志中的 `Try to download wheel from URL: ...`。

**需要观察的现象 / 预期结果**：`develop.sh` 后 `deep_gemm/` 下出现 `_C...so` 软链；`DG_USE_LOCAL_VERSION=0` 时 `bdist_wheel` 日志会出现下载尝试，断网或无对应 wheel 时回退 `Building from source...`。

> 待本地验证：上述命令需在具备 GPU 工具链与联网的环境中运行。

#### 4.3.5 小练习与答案

**练习 1**：`develop.sh` 用软链 `.so` 而不是 `pip install`，这样做的好处是什么？
**参考答案**：软链是原地（in-place）开发模式，修改 Python 代码或重新 `setup.py build` 后，`import deep_gemm` 立即生效，不必反复卸载/安装；适合库自身的迭代开发。

**练习 2**：默认配置下直接运行 `install.sh`，会下载预编译 wheel 吗？
**参考答案**：不会。因为 `DG_USE_LOCAL_VERSION` 默认为 `1`，`CachedWheelsCommand` 会直接走 `super().run()` 本地源码构建。只有显式设置 `DG_USE_LOCAL_VERSION=0`（且不设 `DG_FORCE_BUILD=1`）时才会尝试下载预编译 wheel。

---

### 4.4 CMakeLists.txt：仅为 IDE 索引存在

#### 4.4.1 概念说明

仓库根目录有一份 `CMakeLists.txt`，但**它不参与 DeepGEMM 的真实构建**——真实构建完全由 `setup.py` 驱动。它存在的唯一目的，是让 CLion、VSCode 等「基于 CMake 的 IDE」能够对 CUDA 设备代码（那些 `.cuh`/`.cu`）做**代码索引、跳转和补全**。否则 IDE 看不懂这些只有 JIT 才编译的设备头文件，开发体验很差。

#### 4.4.2 核心流程

这份 CMake 脚本做了几件服务于「索引」的事：

1. 声明工程语言为 CXX + CUDA，设置一些示例编译选项（如 `-O3`、`--register-usage-level=10`）——这些只是为了让 IDE 分析时参数齐全，并非运行时真正用的参数。
2. `find_package(CUDAToolkit / pybind11 / Torch)`：让 IDE 解析这些依赖的头文件与库。
3. `pybind11_add_module(_C csrc/python_api.cpp)`：声明主 Python API 入口，与 `setup.py` 里编译的是同一文件。
4. `cuda_add_library(deep_gemm_indexing_cuda STATIC csrc/indexing/main.cu)`：额外编译一个静态库目标，纯粹为了「让设备 kernel 代码也能被 IDE 索引到」。

注意 `CUDA_ARCH_LIST` 被设为 `"9.0"`，这只是给索引用的默认架构。

#### 4.4.3 源码精读

文件第一行注释就把定位讲透了：**只为 CMake 类 IDE 索引，真实编译走 JIT**：[CMakeLists.txt:1](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/CMakeLists.txt#L1)。

依赖与索引目标：`find_package` 三连 [CMakeLists.txt:16-18](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/CMakeLists.txt#L16-L18)、主入口 `pybind11_add_module` [CMakeLists.txt:28](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/CMakeLists.txt#L28)、为索引而建的静态库 [CMakeLists.txt:32](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/CMakeLists.txt#L32)。

> 验证其「非真实构建」最直接的方法：`./develop.sh` 与 `./install.sh` 全程都不会调用 `cmake`；CMake 只在你用 IDE 主动 configure 项目时才运行。

#### 4.4.4 代码实践

**实践目标**：确认 `CMakeLists.txt` 与真实构建无关，并理解它对 IDE 的价值。

**操作步骤**：

1. 通读 `./develop.sh` 与 `./install.sh`，确认其中没有任何 `cmake` 调用。
2.（可选）在 CLion 中打开本仓库，让 IDE 用 `CMakeLists.txt` 做 configure，验证它可以对 `deep_gemm/include/deep_gemm/impls/` 下的设备 kernel 头文件做跳转与补全。

**需要观察的现象 / 预期结果**：两个 shell 脚本里都没有 cmake；IDE configure 后能正常索引 CUDA 设备代码。

#### 4.4.5 小练习与答案

**练习 1**：既然 `CMakeLists.txt` 不参与真实构建，删掉它会影响 DeepGEMM 的运行吗？
**参考答案**：不影响运行（真实构建与运行完全依赖 `setup.py` + JIT）。但会损失 IDE 对 CUDA 设备代码的索引/跳转能力，降低开发体验，因此仓库保留它。

**练习 2**：`CMakeLists.txt` 里 `pybind11_add_module(_C csrc/python_api.cpp)` 与 `setup.py` 的 `CUDAExtension` 入口源文件一致，这说明什么？
**参考答案**：两者指向同一个宿主入口文件 `csrc/python_api.cpp`。CMake 版是为了让 IDE 能索引/编译它做语法分析；`setup.py` 版才是真正产出运行时 `.so` 的那条路径。

---

## 5. 综合实践

把本讲的四个模块串起来，完成一次「从零构建到 import 验证」的完整链路：

1. **准备环境**：确认 GPU 为 SM90/SM100、CUDA 满足版本、PyTorch ≥ 2.1（参考 4.1 的命令）。
2. **拉取子模块**：`git clone --recursive <repo>` 或 `git submodule update --init --recursive`，确认 `third-party/cutlass/include` 非空。
3. **开发构建**：运行 `./develop.sh`，观察它「链接头文件 → `setup.py build` → 软链 `.so`」三步。
4. **记录产物**：用 `find build -name "*.so"` 与 `ls -l deep_gemm/*.so` 记录编译产物路径；查看 `build/.../deep_gemm/include/` 下随包分发的 `cutlass/`、`cute/`、`deep_gemm/` 头文件（理解这些是 JIT 运行时需要的）。
5. **验证 import**：
   ```bash
   python -c "import deep_gemm; print(deep_gemm.__version__)"
   ```
   预期打印版本号（如 `2.6.1`）且无报错。
6. **（进阶）定位 JIT 衔接点**：阅读 [deep_gemm/__init__.py:122-125](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L122-L125)，理解 `import` 时 `_C.init(库根目录, CUDA_HOME)` 把「随包分发的 include 根目录」与「CUDA 主目录」交给 C++ 侧——这正是构建产物与运行时 JIT 的衔接点：JIT 后续会在这两个路径下找设备头文件与 `nvcc`/`nvrtc`。

> 待本地验证：本综合实践需要真实 GPU 与 CUDA 工具链，请在你自己的环境完成并记录每一步输出。

## 6. 本讲小结

- DeepGEMM 的依赖分编译期（C++20 编译器、CUDA Toolkit、CUTLASS/fmt 子模块）与运行时（SM90/SM100 GPU、PyTorch、JIT 所需的 `nvcc`/`nvrtc` 与随包头文件）两层。
- 构建阶段**只编译薄宿主模块 `_C`（单文件 `csrc/python_api.cpp`）**，所有重型设备 kernel 推迟到运行时 JIT——这是「安装免 CUDA 编译」哲学的落地。
- `setup.py` 用 `CUDAExtension` 编译 `_C`，并通过 `CustomBuildPy` 完成「复制 CUTLASS/CuTe 头文件进包、生成默认 `envs.py`、生成 `_C.pyi`」三件准备工作。
- `develop.sh`（原地构建 + 软链 `.so`）面向开发者；`install.sh`（`bdist_wheel` + `pip install`）面向使用者；`CachedWheelsCommand` 在 `DG_USE_LOCAL_VERSION=0` 时可下载官方预编译 wheel，失败回退源码构建（默认 `DG_USE_LOCAL_VERSION=1`，即默认本地构建）。
- 仓库根目录的 `CMakeLists.txt` **仅为 CMake 类 IDE 索引设备代码**，不参与真实构建。
- 版本号由 `__version__`（当前 `2.6.1`）追加 git 短 SHA 组成，并被编码进 wheel 文件名以保证下载到与本环境匹配的包。

## 7. 下一步学习建议

到这里，你应该已经能装好 DeepGEMM 并 `import` 成功。建议接下来：

- 阅读讲义 **u1-l3（目录结构与分层架构）**，建立从 `import deep_gemm` 到 GPU kernel 执行的端到端数据流心智模型，理解 `csrc/apis`、`csrc/jit`、`deep_gemm/include` 的分层关系。
- 进而阅读 **u1-l4（Python 接口全貌与第一次调用）**，亲手调用一次 `fp8_gemm_nt` 并触发 JIT，观察首次编译延迟与缓存——那会与本讲「构建薄、运行时厚」的认知完美闭环。
- 如果你更关心 JIT 本身，可以直接跳到 Unit 3（JIT 编译系统），从 [csrc/jit/compiler.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp) 看构建产物如何被运行时驱动。
