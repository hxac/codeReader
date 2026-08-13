# 环境准备、源码构建与安装

## 1. 本讲目标

本讲承接 [u1-l1](./u1-l1-project-overview.md) 对 AMCT 的全局认识，把抽象的「浮点模型 → AMCT 量化 → 昇腾 NPU 推理」链路落地成**可在你自己的机器上敲出来的命令**。

学完本讲，你应该能够：

1. 说出 AMCT 的运行依赖（Python / PyTorch / CANN / GCC 等）及其版本要求，并能区分「编译态」与「运行态」。
2. 读懂 `build.sh` 的 `--torch` / `--pkg` / `--experimental` 等构建选项，知道哪个选项产出什么产物。
3. 读懂 `setup.py` 的包发现、版本读取、平台注入逻辑，理解分发包文件名是怎么拼出来的。
4. 读懂 `requirements.txt` 依赖清单，特别是「NPU 场景必须用 CPU 版 PyTorch」这一关键约束。
5. 从源码走通「构建分发包 → pip 安装 → `import amct_pytorch` 验证」的完整闭环。

## 2. 前置知识

本讲面向初学者，但有几个名词需要先建立直觉：

- **构建（build）**：把人类写的源码（Python + C++ + CMake 脚本）转换成机器能用的产物（`.so` 动态库、`.tar.gz` 分发包）的过程。AMCT 用一个 shell 脚本 `build.sh` 来编排这个过程。
- **分发包（distribution / sdist）**：Python 生态里可以 `pip install` 的压缩包，后缀通常是 `.tar.gz`。它内部包含一份按标准目录组织好的 Python 代码。
- **CANN**：昇腾 NPU 的软件栈（含 Toolkit 与 Ops 两部分）。AMCT 的部分 C++ 代码依赖 CANN 提供的头文件和库，所以构建和运行都离不开它。
- **CPU 版 PyTorch**：这是 AMCT 的一个反直觉设计——在 NPU 上跑量化，反而要装**不带 GPU/CUDA 的 CPU 版** `torch`，再配上华为的 `torch_npu` 适配层。本讲后面会解释原因。
- **编译态 vs 运行态**：只想编译 AMCT（不跑）时，只需 CANN Toolkit；想真正运行量化，还需要 NPU 驱动、固件和 CANN Ops 包。

> 提示：如果你手头没有昇腾设备，本讲里「构建」相关的命令在**普通 x86 Linux** 上也能看到选项和脚本逻辑；只有真正「运行量化」才需要 NPU。所以构建这一步大家都能跟做。

## 3. 本讲源码地图

本讲涉及的文件都位于仓库根目录或顶层，是整个项目的「门面」：

| 文件 | 作用 |
|------|------|
| [build.sh](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh) | 工程编译脚本，解析命令行参数、调用 CMake 编译、产出分发包。本讲主角。 |
| [setup.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/setup.py) | Python 打包入口，被 CMake 调用执行 `sdist`，决定打哪些包、版本号、平台后缀。 |
| [requirements.txt](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/requirements.txt) | Python 运行依赖清单，固定 torch / torch_npu / transformers 等版本。 |
| [amct_pytorch/CMakeLists.txt](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/CMakeLists.txt) | 调用 `setup.py sdist` 的 CMake target，负责把平台与试验开关以环境变量传给 setup.py。 |
| [docs/zh/quick_install.md](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/quick_install.md) | 官方环境部署文档，列全前置依赖版本与三种搭建方式。 |
| [AGENTS.md](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/AGENTS.md) | 给开发者的速查手册，含构建命令、安装步骤与常见坑。 |

辅助文件（不在 `source_files` 但本讲会引用）：

- `version.info`：根目录文件，记录 `Version=9.0.0`，给 `--pkg` 全量包命名用。
- `amct_pytorch/.version`：内容是 `1.1.0`，给 `amct_pytorch` 分发包命名用。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先讲清楚**运行依赖**，再依次拆**`build.sh` 构建选项与产物**、**`setup.py` 打包机制**、**`requirements.txt` 依赖清单**。

### 4.1 运行依赖与环境准备

#### 4.1.1 概念说明

AMCT 不是「装一个 pip 包就完事」的纯 Python 项目——它的底层有 C++ 代码（量化算子、图压缩），需要编译；运行时又要调用昇腾 NPU 硬件，需要 CANN 软件栈。所以环境准备分成两层：

- **系统级依赖**：bash、GCC、CMake、patch、Python 解释器。这些是「能编译」的前提。
- **昇腾依赖**：CANN（Toolkit + Ops）、NPU 驱动与固件、`torch_npu`。这些是「能在 NPU 上运行」的前提。

官方文档把使用场景分成「编译态」和「运行态」，初学者容易混淆：

| 场景 | 需要装什么 | 对应动作 |
|------|-----------|---------|
| 编译态 | 仅 CANN Toolkit | 只想把 AMCT 编出分发包，不跑 |
| 运行态 | 驱动 + 固件 + Toolkit + Ops | 真正跑量化 / 跑部署模型 |

#### 4.1.2 核心流程

环境准备的标准顺序：

1. 装好 NPU 驱动与固件（仅运行态需要）。
2. 装 CANN Toolkit（编译态起就必需），可选装 CANN Ops（运行态必需）。
3. 配置环境变量 `source .../cann/set_env.sh`。
4. 安装 CPU 版 PyTorch + `torch_npu` + 其余 Python 依赖（`pip install -r requirements.txt`）。
5. 用 `build.sh` 构建分发包，再 pip 安装产物。

#### 4.1.3 源码精读

官方在 [quick_install.md](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/quick_install.md) 里列出了手动安装的前置依赖版本（[docs/zh/quick_install.md:L88-L100](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/quick_install.md#L88-L100)），这段代码（其实是文档里的依赖清单）说明了系统能编译的最低门槛：

- bash ≥ 5.1.16
- GCC ≥ 7.3.x
- CMake ≥ 3.16.0（建议 3.20.0）
- patch ≥ 2.7
- Python ≥ 3.9.x
- PyTorch：2.7.1 或 2.1.0

注意 PyTorch 有两个可选版本，要和 CANN / `torch_npu` 配套，**不能随意升级**。

关于「NPU 场景必须用 CPU 版 PyTorch」这条反直觉约束，[quick_install.md 的 PyTorch + torch_npu 配套安装一节](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/quick_install.md#L102-L121) 给出了解释与验证命令。核心原因是：`torch_npu` 作为一个「后端适配层」要接管 PyTorch 的设备调度，如果同时存在带 CUDA 的 `torch`，会在运行期发生加速器冲突。所以推荐路径是先卸掉非 CPU 版 torch，再装 CPU 版：

```bash
pip3 uninstall -y torch
pip3 install torch==2.7.1+cpu --index-url https://download.pytorch.org/whl/cpu
pip3 install torch_npu==2.7.1.post4
```

装完后用一行 assert 确认装的是 CPU 版（[docs/zh/quick_install.md:L119-L121](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/quick_install.md#L119-L121)）：

```bash
python3 -c "import torch; assert '+cpu' in torch.__version__, 'must use CPU torch for NPU path'"
```

最后，CANN 装好后要让环境变量生效（[docs/zh/quick_install.md:L190-L199](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/quick_install.md#L190-L199)）：

```bash
source /usr/local/Ascend/cann/set_env.sh
```

> 文档还提供 WebIDE 与 Docker 两种「免手动装 CANN」的捷径（[docs/zh/quick_install.md:L13-L18](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/quick_install.md#L13-L18)），没有昇腾设备的读者可以从 WebIDE 一站式平台起步。

#### 4.1.4 代码实践

- **实践目标**：确认本机满足编译态前置依赖。
- **操作步骤**：依次执行下列命令，查看各版本号。
  ```bash
  bash --version | head -1
  g++ --version | head -1
  cmake --version | head -1
  python3 --version
  ```
- **需要观察的现象**：每个命令都应打印出一行版本号。
- **预期结果**：bash ≥ 5.1.16、g++ ≥ 7.3、cmake ≥ 3.16、python3 ≥ 3.9。若某项低于要求，需先升级。
- **说明**：本机若无 NPU，可跳过 CANN 与 torch_npu 相关步骤，只验证上面四项即可跟做后续「构建」实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 AMCT 在 NPU 场景下要求安装 CPU 版 PyTorch，而不是带 CUDA 的版本？

> **参考答案**：因为 `torch_npu` 是 PyTorch 的 NPU 后端适配层，需要独占设备调度；若同环境存在带 CUDA 的 torch，会在运行期产生加速器冲突。CPU 版 torch 不带任何加速后端，正好把调度让给 `torch_npu`。

**练习 2**：「编译态」和「运行态」对 CANN 的要求有何不同？

> **参考答案**：编译态只需 CANN Toolkit（提供编译期头文件/库）；运行态还需要 NPU 驱动、固件和 CANN Ops 算子包（提供运行期算子实现）。

### 4.2 build.sh 构建选项与产物

#### 4.2.1 概念说明

`build.sh` 是整个项目的「总调度脚本」。它做三件事：解析命令行参数 → 拼装 CMake 参数并调用 `cmake` + `make` 编译 → 把编译产物打包成分发包。

初学者最容易踩的第一个坑：**不带任何参数直接跑 `bash build.sh` 不会产出任何分发包**。这一点官方在 [AGENTS.md](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/AGENTS.md) 里特意用警告框标了出来（[AGENTS.md:L23-L25](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/AGENTS.md#L23-L25)）：脚本只在指定 `--torch`、`--pkg` 或 `-u/--utest` 时才真正构建。

#### 4.2.2 核心流程

`build.sh` 的执行流程（伪代码）：

```
main():
    checkopts(参数)            # 解析 --torch/--pkg/--experimental 等
    g++ -v                     # 打印编译器版本（顺便确认 g++ 可用）
    assemble_cmake_args()      # 把开关拼成 CMAKE_ARGS
    if ENABLE_PYTORCH_PACKAGE: build_pytorch_package()   # --torch
    elif ENABLE_PACKAGE:       build_package()           # --pkg
    if ENABLE_TEST:            build_ut()                # -u/--utest
```

关键分发逻辑在 `main()` 里（[build.sh:L313-L330](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh#L313-L330)）：三个开关是互斥 + 叠加关系——`--torch` 和 `--pkg` 二选一（torch 优先），而 `-u/--utest` 可以和它们叠加。

两条产物路径的区别：

| 选项 | 产物 | 命名来源 |
|------|------|---------|
| `--torch` | `amct_pytorch-<version>-py3-none-<platform>.tar.gz` | `amct_pytorch/.version`（1.1.0）|
| `--pkg` | 上面那个 + 外层再包一层 `cann-amct_<version>_<os>-<arch>.tar.gz`（含 graph）| `version.info`（9.0.0）|

`--experimental` 是个「修饰开关」，必须配合 `--torch` 或 `--pkg` 使用，作用是把 `amct_pytorch/experimental/` 试验特性也打进包里。

#### 4.2.3 源码精读

**(1) 所有构建选项**

`usage()` 函数列出了全部选项（[build.sh:L26-L48](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh#L26-L48)），初学者重点关注这几个：

| 选项 | 含义 |
|------|------|
| `--torch` | 只构建 amct_pytorch 包（最常用） |
| `--pkg` | 构建完整 amct 包（amct_pytorch + graph） |
| `--experimental` | 把试验特性打进包（需配合上两者） |
| `--build-type=<TYPE>` | Release / Debug，默认 Release |
| `-j<N>` | 编译线程数 |
| `-u, --utest` | 构建并跑单元测试 |
| `--output_path=<PATH>` | 产物输出目录，默认 `./build_out` |
| `--cann_3rd_lib_path=<PATH>` | CANN 第三方库路径，默认 `./third_party` |

对应的参数解析在 `checkopts()` 里，以三个打包开关为例（[build.sh:L89-L100](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh#L89-L100)）：每遇到一个 `--pkg` / `--torch` / `--experimental`，就把对应的 `ENABLE_*` 变量置为 `TRUE`。

> 小细节：usage 里写 `-j<N>` 默认 8，但实际代码读的是 CPU 核数（[build.sh:L53](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh#L53) `THREAD_NUM=$(grep -c ^processor /proc/cpuinfo)`）。文档与代码略有出入，以代码为准。

**(2) 编译主循环：cmake + make**

`build()` 函数是真正干活的地方（[build.sh:L139-L171](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh#L139-L171)）。它先检查 `build/CMakeCache.txt`：如果构建类型、试验开关、ASAN 开关任一发生变化，就清掉缓存重配（[build.sh:L141-L153](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh#L141-L153)）。然后执行编译两件套：

```bash
cmake ${CMAKE_ARGS} ..
make all ${VERBOSE} -j${THREAD_NUM}
```

这里 `cmake ..` 触发的 CMake target 最终会调用 `setup.py sdist`（详见 4.3 节），生成 `dist/*.tar.gz`。

**(3) `--torch` 路径：只拷 tar 包**

`--torch` 模式下，`build()` 在 `make` 完成后**提前 return**，把打包交给上层 `build_pytorch_package()`（[build.sh:L173-L176](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh#L173-L176)）。上层函数做的事很简单（[build.sh:L265-L286](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh#L265-L286)）：清掉旧的 `build_out/`，把 `dist/*.tar.gz` 原样拷过去。所以 `--torch` 产物就是 setup.py 生成的那个 `amct_pytorch-*.tar.gz`。

**(4) `--pkg` 路径：多套一层 graph**

`--pkg` 模式不提前 return，继续走 `build()` 的后半段（[build.sh:L184-L196](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh#L184-L196)）：除了拷 `dist/*.tar.gz`，还把 `amctgraph/*`（图压缩组件，由 `install_graph.sh` 下载）拷进来，最后用 `tar -czf` 打成一个 `cann-amct_<VERSION>_<os>-<arch>.tar.gz`。这里的 `<VERSION>` 读自 `version.info`（[build.sh:L189](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh#L189)），即 `9.0.0`，与 amct_pytorch 的 `1.1.0` 不是一回事。

**(5) CMake 参数拼装**

`assemble_cmake_args()` 把所有开关转成 `-D` 参数传给 CMake（[build.sh:L229-L240](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh#L229-L240)），例如 `-DENABLE_EXPERIMENTAL=...`、`-DCMAKE_BUILD_TYPE=...`、`-DCANN_3RD_LIB_PATH=...`。这些 `-D` 最终会被 CMake 转成环境变量传给 setup.py（见 4.3 节）。

#### 4.2.4 代码实践

- **实践目标**：看清 `build.sh` 的全部选项，并尝试构建 amct_pytorch 分发包、记录产物文件名。
- **操作步骤**：
  1. 查看选项：
     ```bash
     bash build.sh --help
     ```
  2. 构建 amct_pytorch 分发包（编译态即可，但需要 CANN Toolkit 与 g++ 可用）：
     ```bash
     bash build.sh --torch
     ```
  3. 在产物目录里找 tar.gz：
     ```bash
     ls build_out/
     ```
- **需要观察的现象**：步骤 1 打印出 usage；步骤 2 末尾打印 `package amct_pytorch run success`；步骤 3 列出一个 `.tar.gz` 文件。
- **预期结果**：产物文件名形如 `amct_pytorch-1.1.0-py3-none-linux_x86_64.tar.gz`（aarch64 机器则是 `linux_aarch64`）。其中：
  - **version 字段** = `1.1.0`（来自 `amct_pytorch/.version`）
  - **arch 字段** = `x86_64` 或 `aarch64`（来自 `uname -m`，由 CMake 注入为 `linux_<arch>`）
- **说明**：实际文件名与本机架构、当前 `.version` 内容有关，若与示例不同以本机实际为准；完整构建需要 CANN 环境，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么「直接 `bash build.sh`（不带参数）」不会产出分发包？

> **参考答案**：因为 `main()` 里的打包动作被三个开关守卫：只有 `ENABLE_PYTORCH_PACKAGE`、`ENABLE_PACKAGE`、`ENABLE_TEST` 任一为 TRUE 才会触发构建（[build.sh:L321-L328](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh#L321-L328)）。不带参数时三者都是空，脚本只打印几行环境信息就结束了。

**练习 2**：`--torch` 与 `--pkg` 的产物文件名分别从哪个文件读版本号？为什么不一样？

> **参考答案**：`--torch` 产物版本号来自 `amct_pytorch/.version`（`1.1.0`，Python 包自身版本）；`--pkg` 外层全量包版本号来自根目录 `version.info`（`9.0.0`，随 CANN 大版本走的发布版本）。两者口径不同，所以数字不一样。

**练习 3**：改了 `--build-type` 从 Release 到 Debug 后，为什么 `build()` 会主动清缓存？

> **参考答案**：`build()` 会比对 `CMakeCache.txt` 里缓存的构建类型/试验开关/ASAN 开关与当前请求是否一致，不一致就 `rm -rf build/`（[build.sh:L147-L152](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh#L147-L152)），避免旧缓存污染新配置。

### 4.3 setup.py 打包机制与版本/平台设置

#### 4.3.1 概念说明

`build.sh` 负责「编排」，真正生成 Python 分发包的是 `setup.py`。它用标准的 `setuptools`，但做了三处定制：

1. **按需排除试验特性**：默认不打 `amct_pytorch/experimental/`，只有设了环境变量才打。
2. **版本号外置**：版本不写死在 setup.py，而是从 `amct_pytorch/.version` 文件读。
3. **平台后缀注入**：`sdist` 产物会被重命名，加上 `py3-none-<platform>` 后缀，让不同架构的包不冲突。

#### 4.3.2 核心流程

setup.py 的执行流程（由 CMake 触发 `python3 setup.py sdist --formats=gztar`）：

```
SetupTool.__init__():
    set_packages()     # 读 AMCT_EXPERIMENTAL 决定是否排除 experimental
    set_version()      # 读 amct_pytorch/.version
    set_platform()     # 仅 sdist 时读 AMCT_PYTORCH_PLATFORM

setuptools.setup(name='amct_pytorch', version=..., packages=..., ...)

# sdist 结束后：把 amct_pytorch-<ver>.tar.gz 重命名为 amct_pytorch-<ver>-py3-none-<platform>.tar.gz
```

#### 4.3.3 源码精读

**(1) 包发现与试验开关**

`set_packages()` 用 `find_packages` 发现 `amct_pytorch` 下的子包，并根据环境变量 `AMCT_EXPERIMENTAL` 决定是否排除试验目录（[setup.py:L44-L55](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/setup.py#L44-L55)）：

```python
enable_experimental = os.getenv('AMCT_EXPERIMENTAL', '').upper() == 'TRUE'
if enable_experimental:
    self.packages = setuptools.find_packages(include=['amct_pytorch', 'amct_pytorch.*'])
else:
    self.packages = setuptools.find_packages(
        include=['amct_pytorch', 'amct_pytorch.*'],
        exclude=['amct_pytorch.experimental', 'amct_pytorch.experimental.*'])
```

注意这里读的是**环境变量**，不是命令行参数。这个环境变量由 CMake 在调用 sdist 前注入。

**(2) 环境变量是谁注入的？**

答案在 [amct_pytorch/CMakeLists.txt](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/CMakeLists.txt) 的 `amct_pytorch_package` target（[amct_pytorch/CMakeLists.txt:L31-L42](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/CMakeLists.txt#L31-L42)）里：

```cmake
export AMCT_PYTORCH_PLATFORM=linux_${CMAKE_HOST_SYSTEM_PROCESSOR} &&
export AMCT_EXPERIMENTAL=${ENABLE_EXPERIMENTAL} &&
${HI_PYTHON} setup.py sdist --formats=gztar
```

这就把 4.2 节里 `build.sh` 传给 CMake 的 `-DENABLE_EXPERIMENTAL=...` 接力成了 setup.py 能读到的环境变量。**这条链路是理解「`--experimental` 命令行开关如何最终影响打进去的代码」的关键**：

```
build.sh --experimental
  → ENABLE_EXPERIMENTAL=TRUE
  → cmake -DENABLE_EXPERIMENTAL=TRUE
  → export AMCT_EXPERIMENTAL=TRUE
  → setup.py: include experimental 包
```

**(3) 版本号读取**

`set_version()` 从 `amct_pytorch/.version` 读版本（[setup.py:L57-L62](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/setup.py#L57-L62)）。这个文件当前内容是 `1.1.0`，所以包名里会出现 `1.1.0`。

**(4) 平台后缀注入**

`set_platform()` 只在 `sdist` 子命令下生效，读取 `AMCT_PYTORCH_PLATFORM`（[setup.py:L64-L68](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/setup.py#L64-L68)）。sdist 跑完后，脚本把默认产物重命名，加上平台后缀（[setup.py:L108-L119](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/setup.py#L108-L119)）：

```
amct_pytorch-1.1.0.tar.gz
  → amct_pytorch-1.1.0-py3-none-linux_x86_64.tar.gz
```

这就是 4.2 节实践任务里「arch 字段」的来源——它本质是 `AMCT_PYTORCH_PLATFORM=linux_${CMAKE_HOST_SYSTEM_PROCESSOR}`。

**(5) 打包元数据**

最终的 `setuptools.setup(...)` 调用声明了包名、版本、描述、URL、classifiers 等（[setup.py:L86-L106](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/setup.py#L86-L106)）。其中 `package_data` 还把 `.version`、graph_based 下的 proto/csv/`.so` 一并打进包（[setup.py:L74-L83](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/setup.py#L74-L83)）。

#### 4.3.4 代码实践

- **实践目标**：理解 setup.py 的包发现逻辑，验证「试验目录默认不打包」。
- **操作步骤**：
  1. 阅读 [setup.py:L44-L55](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/setup.py#L44-L55)，确认默认 `exclude` 了 `amct_pytorch.experimental.*`。
  2. 在仓库根目录用 Python 模拟「默认」与「开启试验」两种情况下的包发现结果（示例代码，仅本地演示，不会改动源码）：
     ```bash
     # 默认（排除 experimental）
     AMCT_EXPERIMENTAL= python3 -c "import setuptools,os; \
       print([p for p in setuptools.find_packages(include=['amct_pytorch','amct_pytorch.*'], \
       exclude=['amct_pytorch.experimental','amct_pytorch.experimental.*']) \
       if 'experimental' in p])"
     # 开启试验
     AMCT_EXPERIMENTAL=TRUE python3 -c "import setuptools; \
       print([p for p in setuptools.find_packages(include=['amct_pytorch','amct_pytorch.*']) \
       if 'experimental' in p])"
     ```
- **需要观察的现象**：第一条命令打印空列表 `[]`；第二条命令打印含 `experimental` 的包列表。
- **预期结果**：默认模式下试验子包被排除；`AMCT_EXPERIMENTAL=TRUE` 下被纳入。
- **说明**：示例代码仅为演示包发现逻辑，**待本地验证**具体输出（取决于仓库当前 experimental 子包数量）。

#### 4.3.5 小练习与答案

**练习 1**：`build.sh --experimental` 这个命令行开关，经过了哪几跳才最终影响 setup.py 打包的内容？

> **参考答案**：`build.sh` 设 `ENABLE_EXPERIMENTAL=TRUE` → `assemble_cmake_args` 拼成 `-DENABLE_EXPERIMENTAL=TRUE` → CMake 在 `amct_pytorch_package` target 里 `export AMCT_EXPERIMENTAL=${ENABLE_EXPERIMENTAL}` → setup.py 的 `set_packages()` 读到 `AMCT_EXPERIMENTAL=TRUE`，从而不再 exclude `experimental` 子包。

**练习 2**：为什么 setup.py 要在 sdist 后把产物重命名加 `py3-none-<platform>` 后缀？

> **参考答案**：为了让不同 CPU 架构（x86_64 / aarch64）编译出的分发包文件名不冲突，便于在同一处目录区分、分发与归档。平台串由 CMake 的 `CMAKE_HOST_SYSTEM_PROCESSOR` 注入。

### 4.4 requirements.txt 依赖清单

#### 4.4.1 概念说明

`requirements.txt` 列出 AMCT 运行所需的全部 Python 第三方库及版本。它和 setup.py 的分工是：setup.py 负责「打包」，requirements.txt 负责「描述运行环境」。官方推荐的安装方式是 `pip install -r requirements.txt`（[docs/zh/quick_install.md:L123-L127](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/quick_install.md#L123-L127)）。

#### 4.4.2 核心流程

依赖可按用途分成几组来理解：

| 分组 | 代表依赖 | 用途 |
|------|---------|------|
| 框架核心 | `torch==2.7.1+cpu`、`torch_npu==2.7.1.post4` | 量化算法的张量计算与 NPU 后端 |
| 模型加载 | `transformers==5.12.1`、`sentencepiece`、`accelerate` | 加载 LLM 与分词 |
| 量化元数据 | `compressed_tensors==0.15.0.1`、`torchao==0.12.0` | 读写 compressed-tensors 量化配置 |
| 数据处理 | `datasets==4.8.4`、`numpy`、`scipy`、`einops` | 校准数据加载与数值运算 |
| 图/序列化 | `onnx==1.18.0`、`onnxruntime==1.20.0`、`protobuf`、`pyyaml`、`zstandard` | 图压缩与配置/权重序列化 |
| 工程 | `setuptools`、`loguru` | 打包与日志 |

#### 4.4.3 源码精读

整份清单见 [requirements.txt:L1-L21](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/requirements.txt#L1-L21)，初学者最该关注两处：

**(1) CPU 版 torch 的固定**

文件头两行先给了一句重要提示（[requirements.txt:L1-L2](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/requirements.txt#L1-L2)），随后把 torch 钉死在 CPU 版（[requirements.txt:L3-L4](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/requirements.txt#L3-L4)）：

```
# NPU 场景需使用 CPU 版 PyTorch 与 torch_npu 配套。
# 如已安装非 CPU 版 torch，请先执行：pip3 uninstall -y torch
torch==2.7.1+cpu
torch_npu==2.7.1.post4
```

注意 `torch==2.7.1+cpu` 这个 `+cpu` 本地版本标签——它不是 PyPI 默认的那个 torch，必须走 CPU wheel 索引才能装上（见 4.1.3 节的安装命令）。

**(2) 关键库的版本锁定**

很多库用 `==` 精确锁定版本，例如 `transformers==5.12.1`、`compressed_tensors==0.15.0.1`、`torchao==0.12.0`、`datasets==4.8.4`（[requirements.txt:L11-L21](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/requirements.txt#L11-L21)）。这是因为 LLM 量化对这些库的 API 行为很敏感（比如 `compressed_tensors` 的量化配置 schema、`transformers` 的模型加载接口），版本漂移可能导致量化结果或导出格式出错。`numpy` 则用区间约束 `>=1.26.4,<2.7`（[requirements.txt:L9](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/requirements.txt#L9)），兼顾兼容性与上限。

> 历史小注：近期的提交里 `torchao` 曾因「未指定版本约束」被修复（见 git log 中 `requirements.txt 中 torchao 未指定版本约束 (#170)`），现在已固定为 `torchao==0.12.0`；另有提交移除了不必要的 `ml_dtypes` 依赖（#171）。这说明依赖清单是持续维护的。

#### 4.4.4 代码实践

- **实践目标**：验证当前环境是否满足 AMCT 的核心依赖约束。
- **操作步骤**：
  ```bash
  python3 -c "import torch, torch_npu, transformers, compressed_tensors, numpy; \
    print('torch', torch.__version__); \
    print('torch_npu', torch_npu.__version__); \
    print('transformers', transformers.__version__); \
    print('compressed_tensors', compressed_tensors.__version__); \
    print('numpy', numpy.__version__)"
  ```
- **需要观察的现象**：打印出五个库的版本号。
- **预期结果**：torch 应为 `2.7.1+cpu`（含 `+cpu`），torch_npu 为 `2.7.1.post4`，transformers 为 `5.12.1`，compressed_tensors 为 `0.15.0.1`，numpy 在 `[1.26.4, 2.7)` 区间。
- **说明**：若未安装这些库会报 `ModuleNotFoundError`，此时应先 `pip install -r requirements.txt`；本机若无 NPU，`torch_npu` 可能未装，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `torch` 要写成 `torch==2.7.1+cpu` 而不是普通的 `torch==2.7.1`？

> **参考答案**：普通的 `torch==2.7.1` 默认从 PyPI 拉，可能带 CUDA 后端，与 `torch_npu` 冲突；`+cpu` 是本地版本标签，明确指向不带任何加速后端的 CPU wheel，给 `torch_npu` 让出设备调度。

**练习 2**：`numpy>=1.26.4,<2.7` 这种区间约束相比 `numpy==1.26.4` 有什么好处？

> **参考答案**：允许安装 1.26.4 及以上、但低于 2.7 的任意 numpy 版本，在保证下限兼容（含已知的 API/行为）的同时，给上限封顶（避开 numpy 2.7 可能的破坏性变更），兼顾安全与灵活性。

## 5. 综合实践

把本讲四个模块串起来，完成一次「从依赖检查到安装验证」的完整闭环。这是一个**源码阅读 + 命令实操**的混合任务。

**任务**：在本机走通 AMCT 的构建与安装，并解释每一步对应的源码逻辑。

**步骤**：

1. **依赖自检**：执行 4.1.4 的四条命令，确认 bash / g++ / cmake / python3 满足版本。
2. **阅读构建入口**：打开 [build.sh](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh)，定位 `usage()`（[L26-L48](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh#L26-L48)）与 `main()`（[L313-L330](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh#L313-L330)），用自己的话画出 `--torch` 选项从命令行到产物文件的完整链路图（提示：要经过 `checkopts` → `build_pytorch_package` → `build` → `cmake/make` → CMake 的 `amct_pytorch_package` target → `setup.py sdist` → 重命名）。
3. **构建分发包**（需 CANN Toolkit）：
   ```bash
   bash build.sh --torch
   ```
4. **定位产物并解读文件名**：
   ```bash
   ls build_out/
   ```
   把文件名拆成 `<name>-<version>-<python_tag>-<abi_tag>-<platform_tag>.tar.gz` 五段，指出 version 来自哪个文件、platform_tag 来自哪个 CMake 变量。
5. **pip 安装并验证**（参考 [AGENTS.md:L75-L95](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/AGENTS.md#L75-L95)）：
   ```bash
   # 若 pip > 25.2，需追加 --no-build-isolation
   pip3 install build_out/amct_pytorch-<version>-py3-none-linux_<arch>.tar.gz --user
   python3 -c "import amct_pytorch as amct; print('successfully installed AMCT')"
   ```

**验收标准**：

- 能画出 `--torch` 从命令行到 `.tar.gz` 的链路图，并标出 setup.py 在哪一步被调用、`AMCT_EXPERIMENTAL` 在哪一步被注入。
- 第 5 步打印出 `successfully installed AMCT`。

> 若本机没有 CANN / NPU，第 3、5 步无法实跑，可改为：对照源码写出每一步**应该**产出的文件名与日志关键字（如 `package amct_pytorch run success`），并标注「待本地验证」。

## 6. 本讲小结

- AMCT 的环境分两层：系统级（bash/GCC/CMake/Python）与昇腾级（CANN Toolkit + Ops、驱动固件）；「编译态」只需 Toolkit，「运行态」三者都要。
- NPU 场景必须装 **CPU 版** `torch==2.7.1+cpu` + `torch_npu`，否则会加速器冲突；这是初学者最反直觉的约束。
- `bash build.sh` **必须带** `--torch` / `--pkg` / `-u` 之一才会真正构建；裸跑不产出任何分发包。
- `--torch` 产物是 `amct_pytorch-<version>-py3-none-<platform>.tar.gz`，version 来自 `amct_pytorch/.version`（1.1.0），platform 来自 CMake 的 `CMAKE_HOST_SYSTEM_PROCESSOR`。
- `--experimental` 开关经 `build.sh → cmake -D → export AMCT_PYTORCH_PLATFORM/AMCT_EXPERIMENTAL → setup.py` 四跳，最终决定是否把 `experimental/` 打进包。
- 构建出的分发包要用 `pip install` 安装后，`import amct_pytorch` 才可用（pip > 25.2 需加 `--no-build-isolation`）。

## 7. 下一步学习建议

本讲解决了「怎么把 AMCT 装起来」，下一步建议：

1. **先读 [u1-l3 仓库目录结构与代码组织](./u1-l3-directory-structure.md)**：建立代码地图，知道 `amct_pytorch/` 下各子目录的职责，为后续读源码做准备。
2. **再读 [u1-l4 一站式量化初体验：四条 CLI 命令](./u1-l4-first-quant-cli.md)**：用 `eval / extract_ptq_data / ptq / deploy` 四条命令跑通一次端到端量化，把本讲装好的工具真正用起来。
3. **想深入打包机制的读者**：可以继续看 [amct_pytorch/CMakeLists.txt](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/CMakeLists.txt) 与 `cmake/config.cmake`，理解 C++ 扩展（`.so`）是如何与 Python 包一起编译进分发包的。
