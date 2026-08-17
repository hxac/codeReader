# 环境搭建与源码构建：setup.py 驱动的 CMake 构建

## 1. 本讲目标

学完本讲，你应该能够：

1. 根据「本地是否有 NPU 设备」和「使用目标」，从云开发环境、CANN Docker 镜像、手动安装 CANN 包三种方式中选出适合自己的环境准备方案。
2. 理解 pyasc 源码构建的完整链路：`pip install .` 背后其实是 setuptools 的自定义命令 `LocalBuildExt` 拉起 CMake + Ninja，编译出 `libpyasc` 这个 C++ 扩展模块。
3. 掌握 `LLVM_INSTALL_PREFIX`、`PYASC_SETUP_*` 系列环境变量各自控制构建的哪个环节。
4. 在一台**没有 NPU** 的 Linux 机器上完成源码安装，并通过 `pip3 list` 和构建目录结构确认安装成功。

## 2. 前置知识

### 2.1 pip 安装 Python 包的两种姿势

- `pip install 包名`：从 PyPI 下载预编译好的 wheel 文件直接解压安装，**不需要本地编译**。pyasc 提供了 CPython 3.9–3.12 的官方 wheel。
- `pip install .`（在源码目录执行）：本地编译安装。安装后源码的修改**不影响**已安装版本，适合生产环境。
- `pip install -e .`（开发者模式）：只创建一个符号链接指向源码目录，源码修改**实时生效**，适合开发阶段。pyasc 是「Python 前端 + C++ 后端」的混合项目，`-e` 模式下改 Python 代码立即生效，但改 C++ 代码仍需重新触发编译。

### 2.2 setuptools 的 cmdclass 钩子

setuptools（Python 的打包工具）把安装过程拆成一条命令链：`install → build_py → build_ext → ...`。每一步都是一个「命令类」，pyasc 的 `setup.py` 通过 `cmdclass` 参数**替换**了其中若干默认命令（如 `build_ext`），从而把 CMake 构建挂进 pip 安装流程。这是本讲的核心机制。

### 2.3 CMake 与 Ninja

- **CMake**：C/C++ 世界的构建系统生成器。它本身不编译代码，而是读取 `CMakeLists.txt`，生成具体的构建脚本。
- **Ninja**：一个高速构建执行器。CMake 用 `-G Ninja` 生成 `build.ninja` 文件后，交给 Ninja 并行执行编译。pyasc 源码编译**强制要求两者都安装**。

### 2.4 pybind11 与 LLVM/MLIR

- **pybind11**：一个 C++ 头文件库，用来把 C++ 函数/类包装成 Python 可导入的扩展模块（一个 `.so` 文件）。pyasc 用它把 C++ 实现的 MLIR 后端暴露给 Python。
- **LLVM/MLIR**：pyasc 后端的基石。ASC-IR 是基于 MLIR 框架定义的一套方言（Dialect），因此**编译 pyasc 源码必须先有 LLVM/MLIR 的开发环境**（头文件和 CMake 配置）。注意：这里的 LLVM 是**构建 pyasc 工具本身**用的，与运行期用到的昇腾毕昇编译器是两回事。

### 2.5 一个重要区分：构建期依赖 vs 运行期依赖

| 依赖 | 什么时候需要 | 没有 NPU 的机器需要吗 |
| :--- | :--- | :--- |
| LLVM 预编译包、cmake、ninja、pybind11 | 编译 pyasc 源码时 | **需要** |
| CANN 包（toolkit + ops） | 运行算子时（毕昇编译、aclruntime） | 需要（仿真器模式也要，但无需驱动固件） |
| NPU 驱动与固件 | 算子真正上板运行时 | **不需要** |

官方文档明确说明：驱动与固件是运行态依赖，若仅编译本项目源码，可以不安装（见 [docs/quick_start.md:62](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/quick_start.md#L62)）。这就是为什么本讲的实践任务可以在无 NPU 的普通 Linux 机器上完成。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [docs/quick_start.md](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/quick_start.md) | 官方环境准备与安装指引，本讲的「用户手册」 |
| [setup.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py) | 打包入口：定义 `LocalBuildExt` 等自定义命令、环境变量、包结构 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/CMakeLists.txt) | 顶层 CMake 工程（工程名 AscIR）：查找 MLIR、组织子目录 |
| [python/src/CMakeLists.txt](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/CMakeLists.txt) | 定义 `libpyasc` 扩展模块的编译与链接 |
| [requirements-build.txt](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/requirements-build.txt) | 构建期 Python 依赖（cmake、ninja、pybind11 等） |
| [requirements-runtime.txt](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/requirements-runtime.txt) | 运行期 Python 依赖（pytest、psutil 等） |

## 4. 核心概念与源码讲解

### 4.1 环境准备：三种方式与基础依赖

#### 4.1.1 概念说明

pyasc 编写的是运行在昇腾 AI 处理器上的算子，因此「环境准备」主要是在准备 **CANN 软件栈**（含算子编译所需的毕昇编译器与运行时库）。官方按「是否有 NPU 设备」和「使用目标」给出三种方式：

1. **云开发环境（CANNLab）**：网页/VSCode 在线提供的昇腾 ARM 环境，驱动固件、CANN 包都预装好了，适合**没有 NPU 设备**的用户。
2. **CANN 官方 Docker 镜像**：本机有 NPU 时，拉取预集成 CANN 的镜像，以特定参数把宿主机 NPU 设备透传进容器。
3. **手动下载安装 CANN 包**：从昇腾社区或 CANN master obs 镜像站下载 `.run` 安装包自己安装，适合贡献者或只想在**仿真器模式**下体验的用户。

#### 4.1.2 核心流程

选择环境的决策流程：

```text
你的机器有 NPU 吗？
├── 没有 ──→ 只想体验编译安装 + 仿真运行算子
│             └─→ 手动安装 CANN 包（跳过驱动固件），或直接用云开发环境
├── 有 ────→ 想快速体验
│             └─→ CANN 官方 Docker 镜像（透传 /dev/davinci0 等设备）
└── 有 ────→ 想为 CANN master 做贡献
              └─→ 手动安装 CANN master 包
```

无论哪种方式，最后都要通过「开发环境验证」：`npu-smi info` 检查驱动、`cat ascend_toolkit_install.info` / `cat ascend_ops_install.info` 检查 CANN toolkit 与 ops 包版本（见 [docs/quick_start.md:187-208](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/quick_start.md#L187-L208)）。

#### 4.1.3 源码精读

官方用一个表格概括了三种方式的选择逻辑：

- [docs/quick_start.md:6-25](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/quick_start.md#L6-L25)：按「无 NPU / 有 NPU」×「社区体验 / 生态贡献」给出环境准备方式的选择矩阵，并建议优先容器化。

CANN 包分 toolkit 和 ops 两种，手动安装命令在：

- [docs/quick_start.md:170-185](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/quick_start.md#L170-L185)：分别安装 toolkit 包与 ops 包的 `chmod +x` + `./xxx.run --install --install-path=...` 命令。注意 ops 包按芯片型号命名（如 910B 对应 `Ascend-cann-910b-ops_...`，A3 对应 `Ascend-cann-A3-ops_...`）。

源码编译的基础依赖清单：

- [docs/quick_start.md:234-239](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/quick_start.md#L234-L239)：Python 3.9–3.12、gcc/g++ ≥ 9.4（两者版本必须一致）、GLIBC ≥ 2.31、cmake ≥ 3.20。

Python 依赖分两份文件安装：

- [docs/quick_start.md:224-227](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/quick_start.md#L224-L227)：先 `pip install -r requirements-build.txt`（构建期依赖），再 `pip install -r requirements-runtime.txt`（运行期依赖）。
- [requirements-build.txt:1-8](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/requirements-build.txt#L1-L8)：构建期依赖包括 `cmake>=3.20,<4.0`、`ninja>=1.11.1`、`pybind11==2.10.3`、`setuptools-scm`、限定版本的 `numpy` 等——**cmake 和 ninja 也在其中**，所以即使系统没装，pip 也会装出 Python 版的 cmake/ninja 可执行文件供构建使用。
- [requirements-runtime.txt:1-8](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/requirements-runtime.txt#L1-L8)：运行期依赖是 `attrs`、`scipy`、`psutil`、`pytest`、`pytest-xdist` 等，与编译无关。

#### 4.1.4 代码实践

**实践目标**：在动手编译之前，先确认你的机器满足源码编译的基础依赖。

**操作步骤**：

```bash
python3 --version      # 应输出 3.9 ~ 3.12 之间的版本
cmake --version        # 应 >= 3.20
gcc --version && g++ --version   # 应 >= 9.4，且两者版本一致
ldd --version          # GLIBC 应 >= 2.31
```

再执行 LLVM 相关系统库检查（官方给出的命令）：

```bash
test -f /usr/lib/$(uname -m)-linux-gnu/libz.so && echo "libz.so: [OK]" || echo "libz.so: [MISSING]"
test -f /usr/lib/$(uname -m)-linux-gnu/libzstd.so && echo "libzstd.so: [OK]" || echo "libzstd.so: [MISSING]"
# 若缺失：sudo apt-get install zlib1g-dev libzstd-dev
```

最后安装两份 Python 依赖：

```bash
cd pyasc   # 进入克隆下来的源码根目录
python3 -m pip install -r requirements-build.txt
python3 -m pip install -r requirements-runtime.txt
```

**需要观察的现象**：每条命令的版本号输出；两个 `test -f` 检查是否都 `[OK]`；pip 安装是否无报错。

**预期结果**：所有版本满足区间要求、`libz.so` 与 `libzstd.so` 均存在。若某项 MISSING，按注释中的 apt 命令补装后再继续。本实践命令来自官方文档，具体输出因机器而异，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `requirements-build.txt` 和 `requirements-runtime.txt` 要分成两个文件，而不是合成一个？

**答案**：两者的使用场景不同。构建期依赖（cmake、ninja、pybind11、setuptools-scm）只在**从源码编译**时需要——通过 `pip install pyasc` 装官方 wheel 的用户完全用不到它们；而运行期依赖（pytest、psutil 等）是装好之后**运行和测试**需要的。分开后，二进制包用户不必安装一堆编译工具。这也解释了 `setup.py` 中 `install_requires` 只列了 `pybind11/numpy/typing_extensions` 三个真正的运行必需项（见 [setup.py:403-407](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L403-L407)）。

**练习 2**：你的笔记本没有 NPU，也想编译 pyasc 并在仿真器模式跑算子，需要安装 NPU 驱动固件吗？需要装 CANN 包吗？

**答案**：不需要驱动固件——驱动是运行态（上板）依赖，仅编译源码和仿真运行可以不装；但 CANN 包仍然需要安装（仿真器模式也要用到其中的组件），可参考手动安装 CANN 包的章节，并按运行环境变量配置一节设置 `LD_LIBRARY_PATH` 指向仿真器库。

### 4.2 LLVM 依赖与 LLVM_INSTALL_PREFIX

#### 4.2.1 概念说明

pyasc 的 ASC-IR 基于 MLIR 构建，编译 pyasc 源码必须找到一套带 MLIR 的 LLVM 安装。`setup.py` 里存在**两条获取 LLVM 的路径**：

1. **手动路径（官方文档推荐）**：用户自己下载 LLVM 预编译包，解压后把路径写进环境变量 `LLVM_INSTALL_PREFIX`。
2. **自动下载路径**：`setup.py` 根据 CPU 架构、glibc 版本自动推断包名，从内部存储服务下载并缓存到用户主目录。

#### 4.2.2 核心流程

`LocalBuildExt.build_extension` 中获取 LLVM 的判断逻辑：

```text
build_extension 开始
└── llvm_dir = get_llvm_install_prefix()   # 读环境变量 LLVM_INSTALL_PREFIX
    ├── 非空 → 直接使用用户指定的路径（手动路径，官方推荐）
    └── 为空 → download_llvm_package()     # 自动下载
                ├── get_storage_url()        # 必须已设置 PYASC_SETUP_STORAGE_URL，否则报错
                ├── get_llvm_package_info()  # 拼 URL：需要读取源码根的 llvm-commit.txt
                ├── 缓存目录 ~/.pyasc/llvm/<full_name>/ 下检查 version.txt
                └── 未命中缓存则下载 tar.gz 并解压、创建符号链接
```

#### 4.2.3 源码精读

手动路径的实现非常简洁：

- [setup.py:89-92](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L89-L92)：`get_llvm_install_prefix()` 读取环境变量 `LLVM_INSTALL_PREFIX`，非空（且非纯空白）就返回该路径，否则返回 `None`——返回 `None` 时才会走自动下载。

- [setup.py:210-214](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L210-L214)：`LocalBuildExt.build_extension` 中的分流点：`llvm_dir = get_llvm_install_prefix()`，为 `None` 则调用 `download_llvm_package()`。

自动下载路径的两个前提条件：

- [setup.py:81-86](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L81-L86)：`get_storage_url()` 要求环境变量 `PYASC_SETUP_STORAGE_URL` 已设置，否则直接 `RuntimeError`。
- [setup.py:95-120](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L95-L120)：`get_llvm_package_info()` 按 `平台-架构-glibc版本` 推断 `ubuntu-x64 / almalinux-x64 / centos-x64 / ubuntu-arm64` 后缀，并读取源码根目录 `llvm-commit.txt` 文件的前 8 个字符拼出包名（如 `llvm-xxxxxxxx-ubuntu-x64.tar.gz`）。

> **读源码发现的关键事实**：当前仓库中**并不包含** `llvm-commit.txt` 文件（可用 `git ls-files | grep llvm` 验证），也没有设置 `PYASC_SETUP_STORAGE_URL` 的说明。因此自动下载链路主要面向内部构建/发布环境；**本地开发者请一律走手动路径**——下载预编译包并 `export LLVM_INSTALL_PREFIX`，这也正是 [docs/quick_start.md:254-282](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/quick_start.md#L254-L282) 给出的官方步骤。这是一个「读源码可以看清文档背后机制边界」的好例子。

下载缓存机制：

- [setup.py:140-160](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L140-L160)：`download_llvm_package()` 把包缓存在 `~/.pyasc/llvm/<full_name>/`（缓存根目录由 [setup.py:45-51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L45-L51) 的 `get_cache_dir()` 决定，可用 `PYASC_HOME` 重定向），以 `version.txt` 内容是否等于下载 URL 判断缓存是否命中，并维护一个 `llvm-<system_suffix>` 的符号链接指向具体版本目录。

LLVM 路径最终如何传给 CMake：

- [setup.py:237](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L237)：配置参数中追加 `-DLLVM_PREFIX_PATH=<llvm_dir>`。
- [CMakeLists.txt:41-49](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/CMakeLists.txt#L41-L49)：顶层 CMake 把 `LLVM_PREFIX_PATH` 加入 `CMAKE_PREFIX_PATH`，进而在 `MLIR_DIR` 下 `find_package(MLIR REQUIRED CONFIG)` 找到 MLIR——这就是 LLVM 与整个 CMake 工程的接合点。

#### 4.2.4 代码实践

**实践目标**：安装 LLVM 预编译包并验证 `LLVM_INSTALL_PREFIX` 生效。

**操作步骤**（以 x86_64 机器为例，ARM 机器把 `x86_64` 换成 `aarch64`）：

```bash
wget https://cann-ai.obs.cn-north-4.myhuaweicloud.com/llvm/llvm-19.1.7-x86_64.tar.xz
tar -xJf llvm-19.1.7-x86_64.tar.xz
export LLVM_INSTALL_PREFIX=$PWD/llvm-19.1.7-x86_64
```

验证安装：

```bash
${LLVM_INSTALL_PREFIX}/bin/llvm-config --version
echo ${LLVM_INSTALL_PREFIX}
```

**需要观察的现象**：`llvm-config --version` 是否输出版本号（如 `19.1.7`）；`echo` 是否打印出绝对路径且末尾不带多余斜杠或空白。

**预期结果**：输出版本信息即安装成功。注意 `export` 只对当前终端会话有效，若新开终端执行 `pip install`，需要重新 `export`。命令本身来自官方文档（[docs/quick_start.md:259-272](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/quick_start.md#L259-L272)），实际下载速度与输出待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果不设置 `LLVM_INSTALL_PREFIX` 直接 `pip install .`，会发生什么？

**答案**：`get_llvm_install_prefix()` 返回 `None`，构建转入 `download_llvm_package()` 自动下载路径；该路径要求 `PYASC_SETUP_STORAGE_URL` 已设置且源码根存在 `llvm-commit.txt`——两者在普通本地环境通常都不满足，会分别在 `get_storage_url()` 抛出 `RuntimeError: PYASC_SETUP_STORAGE_URL must be set`（[setup.py:81-86](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L81-L86)）或打开 `llvm-commit.txt` 时抛出 `FileNotFoundError`（[setup.py:116-118](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L116-L118)）。所以本地构建前务必设置 `LLVM_INSTALL_PREFIX`。

**练习 2**：自动下载的 LLVM 包缓存在哪里？如何让多台机器共享缓存？

**答案**：缓存在 `~/.pyasc/llvm/<包全名>/` 目录下（`get_cache_dir()` 优先读 `PYASC_HOME` 环境变量，其次用 `HOME`，见 [setup.py:45-51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L45-L51)）。把 `PYASC_HOME` 指向一个共享文件系统上的目录（如 NFS 挂载点），即可让多台机器复用同一份下载缓存。

### 4.3 LocalBuildExt/LocalBuildPy：pip 如何拉起 CMake+Ninja

#### 4.3.1 概念说明

pyasc 的核心后端是 C++ 写的 MLIR Dialect、Pass 和代码发射器，必须编译成 Python 扩展模块 `asc._C.libpyasc`（一个 `.so` 文件）才能被 `import asc` 使用。`setup.py` 声明了一个**空的** `LocalExtension`（源码列表为空），然后重写 `build_ext` 命令，把真正的编译工作整体外包给 CMake。这种「setuptools 只做壳、CMake 做实」的模式让同一个 CMake 工程既能被 pip 使用，也能被开发者直接手动调用。

#### 4.3.2 核心流程

`pip install .` 触发的完整构建链：

```text
pip install .
└── setuptools 命令链（cmdclass 替换后）
    ├── LocalBuildPy.run()          # 先强制执行 build_ext，再拷贝纯 Python 包
    ├── LocalBuildExt.run()          # 逐个构建 extension
    │   └── build_extension()
    │       ├── require_tools("cmake", "ninja")     # 检查两个工具存在
    │       ├── 解析 LLVM 路径（见 4.2）
    │       ├── cmake -S <源码根> -B build/cmake.<平台后缀> -G Ninja \
    │       │     -DCMAKE_BUILD_TYPE=... -DLLVM_PREFIX_PATH=... \
    │       │     -DPython3_EXECUTABLE=... -Dpybind11_DIR=...      # 配置阶段
    │       └── cmake --build ... --target libpyasc --parallel      # 编译阶段
    └── 产物 libpyasc*.so 落入 build/lib.<平台后缀>/asc/_C/
```

其中「平台后缀」由 `get_platform_suffix()` 生成，格式为 `<平台>-<实现名>-<Python版本>`，例如 `linux-x86_64-cpython-3.10`。

#### 4.3.3 源码精读

**空壳扩展的声明**：

- [setup.py:181-184](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L181-L184)：`LocalExtension` 继承 `setuptools.Extension`，`sources=[]`——不交给 setuptools 任何源码。
- [setup.py:396-398](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L396-L398)：`ext_modules=[LocalExtension("asc._C.libpyasc")]`，声明最终产物是这个扩展模块，但怎么编由重写的 `build_ext` 决定。

**命令类的注册**：

- [setup.py:409-416](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L409-L416)：`cmdclass` 把 `build_ext/build_py/bdist_wheel/clean/egg_info/install` 六个命令全部替换为 `Local*` 版本。

**LocalBuildPy：保证先编译再拷包**：

- [setup.py:187-195](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L187-L195)：`LocalBuildPy.run()` 先 `run_command("build_ext")` 再执行默认逻辑，并把 `build_lib` 重定向到 `build/lib.<平台后缀>`。若不这么做，纯 Python 包拷贝可能先于 C++ 编译完成，`.so` 就来不及进入包里。

**LocalBuildExt：本讲的主角**：

- [setup.py:198-208](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L198-L208)：`initialize_options` 设置 `build_lib`（`build/lib.<后缀>`）与 `build_temp`（`build/temp.<后缀>`）两个目录；`run()` 遍历所有 extension 调用 `build_extension`。
- [setup.py:211](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L211)：`require_tools("cmake", "ninja")` 用 `shutil.which` 确认两个工具可用（实现见 [setup.py:163-171](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L163-L171)），缺失时报 `xxx is required`。
- [setup.py:221-238](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L221-L238)：CMake 配置参数逐项组装：`-S` 源码根、`-B` 构建目录、`-G Ninja`、`CMAKE_BUILD_TYPE`（默认 `Release`，读 `PYASC_SETUP_CONFIG`，见 [setup.py:220](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L220)）、`CMAKE_LIBRARY_OUTPUT_DIRECTORY` 指向扩展输出目录、`Python3_EXECUTABLE/INCLUDE_DIR`、`pybind11` 的两个路径、`LLVM_PREFIX_PATH`。
- [setup.py:239-253](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L239-L253)：四个可选开关，按环境变量追加 CMake 参数——`PYASC_SETUP_CCACHE`→`-DASCIR_CCACHE=ON`、`PYASC_SETUP_CLANG_LLD`→改用 clang/clang++/lld、`PYASC_SETUP_COVERAGE`→`-DASCIR_COVERAGE=ON`、`PYASC_SETUP_ASAN`→`-DASCIR_ASAN=ON`。这些布尔环境变量统一由 [setup.py:41-42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L41-L42) 的 `check_env_bool` 解析（取值 `1/true/ON` 为真）。
- [setup.py:254-266](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L254-L266)：先 `subprocess.check_call(configure_args)` 执行配置，再以 `--target libpyasc --parallel` 执行编译。`PYASC_SETUP_DEVTOOLS=1` 时 targets 会追加 `ascir-lsp/ascir-opt/ascir-translate` 三个开发工具（[setup.py:174-178](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L174-L178)），`PYASC_SETUP_DOCS=1` 时追加 `mlir-doc`。

**CMake 侧接应**：

- [CMakeLists.txt:9-17](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/CMakeLists.txt#L9-L17)：要求 cmake ≥ 3.20，工程名为 `AscIR`，语言 C/C++。
- [CMakeLists.txt:54-56](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/CMakeLists.txt#L54-L56)：引入 MLIR 的 `TableGen/AddLLVM/AddMLIR` 模块——这是整个 Dialect 代码生成体系的入口。
- [CMakeLists.txt:111-120](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/CMakeLists.txt#L111-L120)：五个子目录依次加入：`lib/TableGen`（代码生成器）、`include`（td 定义）、`lib`（Dialect/Pass/Target 实现）、`bin`（命令行工具）、`python/src`（pybind 桥接）。
- [python/src/CMakeLists.txt:9-45](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/CMakeLists.txt#L9-L45)：`libpyasc` 目标由 `pybind11_add_module` 创建，编译 `IR.cpp/Module.cpp/OpBuilder.cpp/Passes.cpp/Translation.cpp` 五个文件，链接 `MLIRAsc`、`MLIRAscTransforms`、`MLIRTargetAsc` 等库，并依赖 `AscPybindGen/AscTypesPybindGen` 两个 TableGen 生成目标。

**PYASC_SETUP_* 环境变量速查表**（均出自 [setup.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py)）：

| 环境变量 | 作用 | 默认行为 |
| :--- | :--- | :--- |
| `LLVM_INSTALL_PREFIX` | 手动指定 LLVM 安装路径 | 未设置则尝试自动下载（本地通常不可用） |
| `PYASC_SETUP_BUILD_DIR` | 覆盖构建根目录 | 源码根下的 `build/` |
| `PYASC_SETUP_CONFIG` | CMake 构建类型 | `Release` |
| `PYASC_SETUP_CCACHE` | 启用 ccache 编译缓存 | 关 |
| `PYASC_SETUP_CLANG_LLD` | 用 clang+lld 替代 gcc | 关 |
| `PYASC_SETUP_DEVTOOLS` | 额外构建 ascir-lsp/opt/translate | 关 |
| `PYASC_SETUP_DOCS` | 额外构建 mlir-doc | 关 |
| `PYASC_SETUP_COVERAGE` | 开启覆盖率编译 | 关 |
| `PYASC_SETUP_ASAN` | 开启 AddressSanitizer | 关 |
| `PYASC_SETUP_NAME` / `PYASC_SETUP_VERSION` / `PYASC_SETUP_VERSION_SUFFIX` | 覆盖包名/版本/版本后缀 | `pyasc` / `1.1.1` / 无 |
| `PYASC_HOME` | LLVM 下载缓存根目录 | 用户主目录 |
| `PYASC_SETUP_STORAGE_URL` | 自动下载 LLVM 的存储源（内部环境用） | 未设置则报错 |

#### 4.3.4 代码实践

**实践目标**：通过改变 `PYASC_SETUP_BUILD_DIR` 和 `PYASC_SETUP_CONFIG`，观察 setup.py 对构建行为的影响，加深对环境变量机制的理解（不必完整编译成功，观察 CMake 配置阶段的输出即可）。

**操作步骤**：

1. 阅读并确认工具就绪：`which cmake ninja`。
2. 在源码根执行（先设置好 `LLVM_INSTALL_PREFIX`）：

```bash
export PYASC_SETUP_BUILD_DIR=/tmp/pyasc-build
export PYASC_SETUP_CONFIG=Debug
python3 -m pip install . 2>&1 | tee /tmp/build-log.txt
```

3. 构建启动后立即在另一个终端查看：`ls /tmp/pyasc-build`。
4. 在日志中搜索 `cmake` 开头的命令行，确认 `-DCMAKE_BUILD_TYPE=Debug`、`-DLLVM_PREFIX_PATH=...` 是否出现。

**需要观察的现象**：构建目录出现在 `/tmp/pyasc-build`（而不是源码根的 `build/`）下，且包含 `cmake.linux-xxx-cpython-XXX` 子目录；日志里能看到完整的 cmake 配置命令与 `--target libpyasc` 编译命令。

**预期结果**：目录与参数均按环境变量变化。注意 `get_build_dir()` 等函数用了 `@functools.lru_cache`（[setup.py:59-64](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L59-L64)），环境变量在**同一次** setup.py 执行内不会被重复读取，但每次 pip 调用都是新进程，不受影响。完整编译耗时较长（C++ 后端文件多），待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`LocalBuildPy` 为什么要先 `run_command("build_ext")` 再执行默认的 `build_py` 逻辑？

**答案**：pip 安装时会按依赖顺序执行命令，默认的 `build_py` 只负责把纯 Python 文件拷贝到 `build_lib`，不知道 C++ 扩展的存在。若 `build_ext` 在拷贝之后才完成，`.so` 文件可能没有进入最终安装的包里。`LocalBuildPy` 强制先编译扩展、再拷包，保证 `asc/_C/` 下既有 Python 入口又有编译产物（[setup.py:193-195](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L193-L195)）。

**练习 2**：为什么 `LocalExtension` 的 `sources` 是空列表，构建却依然能产出 `.so`？

**答案**：`sources` 只对 setuptools 默认的 `build_ext`（直接调编译器编 .c/.cpp）有意义。pyasc 重写了 `build_ext.run/build_extension`，把工作转交给 CMake 子进程（[setup.py:206-208](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L206-L208)），真正的源文件清单在 `python/src/CMakeLists.txt` 里。`LocalExtension` 只是向 setuptools 声明「存在一个叫 `asc._C.libpyasc` 的扩展模块」，让安装流程给它预留目录与命名。

**练习 3**：想给 C++ 后端排查内存问题，应该设置哪个环境变量？它最终传给 CMake 什么？

**答案**：`PYASC_SETUP_ASAN=1`。它使 `LocalBuildExt` 追加 `-DASCIR_ASAN=ON`（[setup.py:252-253](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L252-L253)），顶层 CMakeLists 据此加上 `-fsanitize=address` 等编译/链接选项（[CMakeLists.txt:103-108](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/CMakeLists.txt#L103-L108)）。

### 4.4 构建产物与安装验证

#### 4.4.1 概念说明

构建结束后，需要回答三个问题：**东西装到哪了**、**装了什么**、**怎么确认装对了**。pyasc 的安装产物分两部分：纯 Python 前端（`python/asc/` 目录，含 runtime/codegen/language/lib 子包）和 C++ 扩展模块（`libpyasc*.so`）。理解构建目录的布局，对后续调试（找到编译产物、清理重编、查看 compile_commands.json）非常有用。

#### 4.4.2 核心流程

安装后的包结构与构建目录布局：

```text
源码根 pyasc/
├── python/asc/...              # 纯 Python 前端（package_dir 把 "python" 映射为包根）
└── build/                      # get_build_dir()，可被 PYASC_SETUP_BUILD_DIR 覆盖
    ├── cmake.linux-x86_64-cpython-3.10/   # get_cmake_dir()：CMake/Ninja 构建树
    │   ├── CMakeCache.txt       #   配置缓存（含 LLVM_PREFIX_PATH 等全部变量）
    │   ├── build.ninja          #   Ninja 构建脚本
    │   ├── compile_commands.json#   每个编译单元的完整命令（给 clangd 用）
    │   └── bin/                 #   PYASC_SETUP_DEVTOOLS=1 时的 ascir-* 工具
    ├── lib.linux-x86_64-cpython-3.10/    # build_lib：组装出的完整 Python 包
    │   └── asc/
    │       ├── _C/libpyasc*.so  #   C++ 扩展模块最终落点
    │       ├── runtime/ codegen/ language/ ...
    ├── temp.linux-x86_64-cpython-3.10/   # build_temp：setuptools 临时目录
    └── bdist.linux-x86_64-cpython-3.10/  # bdist_dir：打 wheel 的工作目录
```

`pip install -e .` 之后再 `import asc`，Python 加载的就是源码目录（或符号链接）下的 `asc`，其中 `asc/_C/__init__.py` 的 `from .libpyasc import ir, passes, translation` 会加载编译出的 `.so`——这是 Python 前端与 C++ 后端在运行时的接合点（见 [python/asc/_C/__init__.py:10](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/_C/__init__.py#L10)，该文件导出 `ir/passes/translation` 三个子模块）。

#### 4.4.3 源码精读

**包结构声明**：

- [setup.py:333-347](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L333-L347)：`packages` 列出 `asc` 及其全部子包（codegen/common/language/adv/basic/core/fwk/lib/host/runtime/_C），配合 [setup.py:394](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L394) 的 `package_dir={"": "python"}`，说明 Python 源码根在仓库的 `python/` 目录。
- [setup.py:399-402](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L399-L402)：`package_data` 把 `rt_wrapper.cpp/npu_utils.cpp/print_utils.cpp` 和 `bindings/*.cpp` 这些 **C++ 源文件当作包数据**随包分发——它们会在用户机器上被在线编译成运行时辅助库（后续 u3-l7 讲解）。

**构建目录的生成逻辑**：

- [setup.py:67-78](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L67-L78)：`get_platform_suffix()` 用 `sysconfig.get_platform()` + `sys.implementation.name` + Python 版本拼出后缀；`get_cmake_dir()` 返回 `build/cmake.<后缀>` 并自动创建目录。
- [setup.py:269-299](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L269-L299)：`LocalBdistWheel`、`LocalInstall` 分别把 `bdist_dir`、`build_lib` 也重定向到 `build/` 下的同名后缀目录，保证所有中间产物集中在统一布局里。

**版本号与安装验证**：

- [setup.py:322-330](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L322-L330)：`get_project_version()` 优先用 setuptools-scm 从 git 标签推版本，失败时退回 `DEFAULT_VERSION = "1.1.1"`（[setup.py:30](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L30)）。开发者模式下 `pip3 list` 看到的版本常带 `+g<commit>` 与 `.d<日期>`（脏工作区标记）后缀，来自 [setup.py:306-319](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L306-L319) 的 `wheel_local_version`。
- [docs/quick_start.md:300-307](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/quick_start.md#L300-L307)：官方安装验证命令就是 `pip3 list | grep -w "pyasc"`，能显示包名与版本即安装成功。

**构建后别忘了运行环境变量**：

- [docs/quick_start.md:309-338](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/quick_start.md#L309-L338)：安装成功只是第一步，运行算子前还要 `source <CANN路径>/set_env.sh`；仿真器模式需把模拟器库路径加入 `LD_LIBRARY_PATH`。

#### 4.4.4 代码实践

**实践目标**：验证 pyasc 安装成功，并亲手确认 C++ 扩展模块的存在。

**操作步骤**：

```bash
# 1. 官方验证方式
pip3 list | grep -w "pyasc"

# 2. 定位安装位置（开发者模式下应指向源码目录）
python3 -c "import asc; print(asc.__file__)"

# 3. 确认 C++ 扩展模块存在（路径中的平台后缀按你的环境调整）
ls build/lib.linux-x86_64-cpython-3.10/asc/_C/ | grep libpyasc
```

**需要观察的现象**：第 1 步输出 `pyasc` 及版本号（`-e` 安装时版本形如 `1.1.1.dev日期+g提交号`）；第 2 步打印 `asc` 包的 `__init__.py` 路径；第 3 步列出一个形如 `libpyasc.cpython-310-x86_64-linux-gnu.so` 的文件。

**预期结果**：三步都符合预期即安装完整。其中第 2 步 `import asc` 会连带触发前端各模块的导入，在只有 LLVM、未装 CANN 的机器上能否顺利执行取决于运行期依赖就绪程度——若报与 aclruntime/CANN 相关的错误，说明**构建安装本身成功**（以第 1、3 步为准），只是运行环境未配好，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`build/cmake.<平台后缀>/compile_commands.json` 是什么？有什么用？

**答案**：它是 `LocalBuildExt` 通过 `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`（[setup.py:231](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L231)）让 CMake 生成的「编译数据库」，记录每个 C++ 翻译单元的完整编译命令（含所有头文件搜索路径与宏）。把它软链到源码根即可让 clangd/clang-tidy 等工具理解 MLIR 头文件的位置，是阅读 C++ 后端源码前配置 IDE 的关键一步。

**练习 2**：`pip install -e .` 之后修改了 `python/asc/runtime/jit.py`，需要重新安装吗？修改了 `python/src/OpBuilder.cpp` 呢？

**答案**：改 Python 文件不需要——`-e` 模式下安装的就是指向源码的符号链接，改动即时生效。改 C++ 文件需要重新触发编译：重跑 `python3 -m pip install -e .`（CMake 有增量构建缓存，只会重编受影响的目标），因为 `.so` 是编译产物，不会自动更新。

**练习 3**：为什么 `setup.py` 要把若干 `.cpp` 文件通过 `package_data` 打进 Python 包？

**答案**：pyasc 的部分运行时辅助库（如 `rt_wrapper.cpp`、`npu_utils.cpp`）需要在**目标机器上**针对本地环境在线编译加载，而不是在打包机上预编译。把它们作为包数据随 wheel 分发（[setup.py:399-402](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L399-L402)），保证任何机器安装 pyasc 后都能拿到这些源码完成本地编译。

## 5. 综合实践

**任务**：在一台不带 NPU 的 Linux 机器上，从零完成 pyasc 源码安装，并记录构建过程证据。

**步骤**：

1. **检查基础依赖**（参照 4.1.4）：Python 3.9–3.12、cmake ≥ 3.20、gcc/g++ ≥ 9.4、GLIBC ≥ 2.31、`libz.so` 与 `libzstd.so` 存在。
2. **安装 Python 依赖**：

   ```bash
   python3 -m pip install -r requirements-build.txt
   python3 -m pip install -r requirements-runtime.txt
   ```

3. **下载并解压 LLVM 预编译包**（按架构选择，参照 4.2.4）：

   ```bash
   wget https://cann-ai.obs.cn-north-4.myhuaweicloud.com/llvm/llvm-19.1.7-x86_64.tar.xz
   tar -xJf llvm-19.1.7-x86_64.tar.xz
   export LLVM_INSTALL_PREFIX=$PWD/llvm-19.1.7-x86_64
   ${LLVM_INSTALL_PREFIX}/bin/llvm-config --version   # 确认输出版本号
   ```

4. **克隆源码并执行开发者模式安装**：

   ```bash
   git clone https://gitcode.com/cann/pyasc.git
   cd pyasc
   python3 -m pip install -e .
   ```

5. **验证安装**：

   ```bash
   pip3 list | grep pyasc
   ```

6. **记录构建目录结构**：

   ```bash
   find build -maxdepth 2 -type d | sort
   ls build/cmake.*/CMakeCache.txt build/cmake.*/build.ninja build/cmake.*/compile_commands.json
   ls build/lib.*/asc/_C/ | grep libpyasc
   grep -m1 "LLVM_PREFIX_PATH" build/cmake.*/CMakeCache.txt
   ```

**预期产物（一份安装报告）**，包含：

- `pip3 list | grep pyasc` 的输出（包名 + 版本号）；
- `find build` 输出的目录树，对照 4.4.2 的布局图，标出 `cmake.*`、`lib.*`、`temp.*`、`bdist.*` 四类目录；
- `libpyasc*.so` 的完整文件名；
- `CMakeCache.txt` 中 `LLVM_PREFIX_PATH` 一行，确认它与你 `export` 的路径一致。

**预期结果**：全部符合即安装成功。C++ 后端编译较慢（视机器性能需要几分钟到更久），首次构建耗时与是否启用 ccache（`PYASC_SETUP_CCACHE=1`）差异明显，可记录对比。本实践各命令均出自官方文档与源码逻辑，实际输出待本地验证。

## 6. 本讲小结

- pyasc 环境准备有三条路：无 NPU 用云开发环境或手动装 CANN 包（免驱动固件），有 NPU 用 CANN 官方 Docker 镜像；**仅编译源码**只需要 LLVM + cmake/ninja + Python 依赖，不需要 NPU 和驱动。
- `setup.py` 用「空壳 `LocalExtension` + 重写 `build_ext`」的方式，把 C++ 后端的编译整体交给 CMake+Ninja：`pip install .` 的本质是执行 `cmake -S 源码根 -B build/cmake.<后缀> -G Ninja` 配置，再 `cmake --build --target libpyasc` 编译。
- LLVM 有两条获取路径：官方推荐的手动 `LLVM_INSTALL_PREFIX`，和依赖 `PYASC_SETUP_STORAGE_URL` + `llvm-commit.txt`（仓库未包含）的自动下载——本地开发务必走前者。
- `PYASC_SETUP_*` 环境变量覆盖构建的方方面面：目录（`BUILD_DIR`）、类型（`CONFIG`）、加速（`CCACHE`/`CLANG_LLD`）、附加目标（`DEVTOOLS`/`DOCS`）、诊断（`COVERAGE`/`ASAN`）。
- 构建产物集中在 `build/` 下按 `cmake.*/lib.*/temp.*/bdist.*` 四类平台后缀目录组织，最终 `libpyasc*.so` 落在 `lib.*/asc/_C/`，通过 `asc/_C/__init__.py` 的 `from .libpyasc import ir, passes, translation` 与 Python 前端会合。
- 安装验证用 `pip3 list | grep -w "pyasc"`；开发者模式 `-e` 下改 Python 即时生效、改 C++ 需重编。

## 7. 下一步学习建议

- **下一讲（u1-l3）**将带你看懂仓库目录地图：`python/asc` 各子包与 `include/lib` 的 C++ 目录如何一一对应，本讲的 `packages` 列表（[setup.py:333-347](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L333-L347)）就是那张地图的索引。
- 装好环境后，建议直接进入 **u1-l4** 运行第一个 Add 算子示例，把本讲构建出的 `libpyasc` 用起来；运行前记得 `source` CANN 的 `set_env.sh`（[docs/quick_start.md:316-323](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/quick_start.md#L316-L323)）。
- 想深入构建系统的读者可以阅读 `pyproject.toml`（PEP 517 构建后端声明）与 [python/src/CMakeLists.txt](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/CMakeLists.txt)，思考「为什么 pybind11_ADD_module 需要链接那批 MLIR 库」——答案会在单元 5 展开。
