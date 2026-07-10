# 安装方式总览

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `pip install kt-kernel`（PyPI 预编译 wheel）和源码编译 `./install.sh` 两条安装路径各自适合什么场景。
- 理解官方 wheel 为什么「一个包内置多个 CPU 变体」、为什么自带「静态 CUDA 运行时」，以及这两点给安装带来的便利。
- 在安装完成后，用 `kt version` 和 `import kt_kernel` 两种方式验证安装是否成功，并看懂 `__cpu_variant__`、`__version__`、CUDA 支持这些关键输出。

本讲是「构建与安装」单元（u2）的第一篇，只讲「怎么装、装完怎么验证」，**不**深入构建选项与 CPU 指令集细节（那是 u2-l2 的主题），也**不**展开运行时变体检测的代码（那是 u2-l3 的主题）。

## 2. 前置知识

在动手之前，先用大白话过一遍本讲会用到的几个概念：

- **pip 与 wheel**：`pip` 是 Python 的包管理器，`wheel`（`.whl`）是 Python 的预编译包格式。一条 `pip install kt-kernel` 的本质是：从 PyPI 下载一个 `.whl`，解压后把里面的 Python 文件和编译好的 C 扩展（`.so`）放到你的环境里。**预编译 wheel 的好处是不需要在你的机器上跑编译器**，安装快、门槛低。
- **CPU 指令集（AMX / AVX512 / AVX2）**：CPU 除了通用指令，还提供「加速指令集」。kt-kernel 的 MoE（专家混合）算子针对不同指令集写了不同版本：AMX 最强（Intel 较新服务器 CPU）、AVX512 次之、AVX2 最普及。你的机器支持哪一档，决定了能跑多快的内核。
- **CUDA 运行时（runtime）vs CUDA 工具链（toolkit）**：跑 GPU 代码需要「CUDA 运行时库」；而「CUDA toolkit」里包含编译 GPU 代码的 `nvcc`。**静态 CUDA 运行时**的意思是：官方 wheel 把运行时库直接打包进 `.so`，所以你**不需要装 CUDA toolkit**，只要有合适的 NVIDIA 驱动就能用 GPU。源码编译则相反，编译时需要 `nvcc`。
- **venv / conda 环境**：建议在独立的 Python 虚拟环境里安装，避免和系统其他包冲突。源码安装时 README 推荐用 conda 建 `python=3.11` 的环境。
- **shell 脚本子命令**：本讲的 `install.sh` 用「子命令 + 选项」的风格，类似 `./install.sh build --manual`。看懂它的 `usage()` 帮助就能掌握绝大部分用法。

## 3. 本讲源码地图

本讲涉及的文件及其职责：

| 文件 | 职责 |
|------|------|
| [kt-kernel/README.md](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/README.md) | 官方安装文档，列出 PyPI 与源码两条路径、验证方法、变体表 |
| [install.sh](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/install.sh) | **仓库根目录**的一键编排脚本：串联 submodule、系统依赖、sglang、kt-kernel |
| [kt-kernel/install.sh](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/install.sh) | kt-kernel 自带的构建/安装脚本：`deps` / `build` / `all`，含 CPU 自动探测 |
| [kt-kernel/autosetup.sh](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/autosetup.sh) | 维护者脚本：批量构建多 Python×多 Torch 的可分发 wheel（理解 wheel 来源） |
| [kt-kernel/python/__init__.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/__init__.py) | 包入口：import 时加载最优 CPU 变体并暴露 `__cpu_variant__`、`__version__` |
| [kt-kernel/python/cli/commands/version.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/version.py) | `kt version` 命令实现：汇总展示各组件版本与安装来源 |

> 回顾 u1-l2、u1-l3：`pip install ktransformers`（顶层 shim）最终会转发安装 `kt-kernel`，所以本讲直接以 `kt-kernel` 为准。

## 4. 核心概念与源码讲解

### 4.1 PyPI 安装

#### 4.1.1 概念说明

对绝大多数用户，官方推荐的第一选择是：

```bash
pip install kt-kernel
```

这条命令背后发生的事情比看上去复杂，理解三个关键词就够了：

1. **预编译 wheel**：官方已经替你把 C++/CUDA 代码编译成 `.so`，打包进 `.whl`。你不需要 `cmake`、`nvcc`、编译工具链，只要一台满足最低要求的 Linux x86-64 机器。
2. **一个 wheel 内置多个 CPU 变体**：因为不同机器的 CPU 支持的指令集不同（AMX / AVX512 各种子集 / AVX2），官方没有为每种 CPU 单独发一个包，而是**把多个变体的 `.so` 都塞进同一个 wheel**，真正的选择推迟到「import 时」按你的 CPU 自动挑一个。这就是为什么安装只有一条命令，却能在不同 CPU 上都拿到最优内核。
3. **静态 CUDA 运行时**：GPU 相关的 CUDA 运行时库被静态链接进 `.so`，因此**不需要安装 CUDA toolkit**，只要有支持 CUDA 11.8+ 或 12.x 的 NVIDIA 驱动即可；没有 GPU 的机器上 CUDA 功能会自动关闭。

> 一句话总结 PyPI 路径：**装得快、不挑编译环境、自动适配 CPU、GPU 开箱即用**。代价是：它只覆盖官方预先编译好的组合，无法支持 AMD（BLIS）、ARM（KML）或自定义 CUDA 版本——那些得走源码安装。

#### 4.1.2 核心流程

PyPI 安装的完整流程：

```text
pip install kt-kernel
   │
   ├── 从 PyPI 下载与 (Python版本, OS, 架构) 匹配的 .whl
   │      （manylinux_2_17 兼容，Python 3.10/3.11/3.12）
   │
   ├── 解压到 site-packages，得到 kt_kernel/ 包
   │      包内含多个变体 .so：amx / avx512_* / avx2
   │      以及静态链接的 CUDA 运行时
   │
   └── 安装完成（此刻还没有挑选变体）

import kt_kernel            ← 真正的「选变体」发生在这里
   │
   ├── _cpu_detect 探测本机 CPU 指令集
   ├── 按优先级挑选最优变体 .so 并加载
   └── 暴露 kt_kernel.__cpu_variant__（如 'amx'）
```

关键点：**变体选择是「运行时」行为，不是「安装时」行为**。同一个 wheel 装在不同 CPU 的两台机器上，import 时各自挑不同的 `.so`。

#### 4.1.3 源码精读

**(1) 官方文档把 PyPI 列为推荐路径。** README 的安装章节标题就是「Install from PyPI (Recommended for Most Users)」，并直接给出单行命令：

[kt-kernel/README.md:49-55](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/README.md#L49-L55) —— 这段是 PyPI 安装的入口说明，强调「single command」即可安装最新版。

**(2) wheel 的「特性清单」解释了它为什么省心。** README 紧接着列出 PyPI wheel 自带的能力：

[kt-kernel/README.md:59-71](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/README.md#L59-L71) —— 注意这几条：`Automatic CPU detection`（自动选变体）、`CPU multi-variant support`（一个 wheel 含 AMX/AVX512 各子集/AVX2）、`Static CUDA runtime`（无需 CUDA toolkit）、`Works on CPU-only systems`（无 GPU 自动禁用 CUDA）。这正是本模块三个关键词的官方出处。运行需求也写在这里：Python 3.10/3.11/3.12、Linux x86-64、CPU 至少支持 AVX2、可选 NVIDIA GPU（算力 8.0+）。

**(3) 「一个 wheel 含 6 个变体」的明细表。** README 给出了变体与自动选择条件的对照：

[kt-kernel/README.md:110-122](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/README.md#L110-L122) —— 这张表说明同一个 wheel 里打包了 AMX、AVX512+BF16、AVX512+VBMI、AVX512+VNNI、AVX512 Base、AVX2 共 6 个变体，并标明各自在「检测到什么指令集时被自动选中」。它把「多变体」从抽象概念落成了具体清单。

**(4) 这些 wheel 是怎么造出来的？** 看 `autosetup.sh`（这是**维护者**用的批量构建脚本，普通用户不会运行，但读它能理解 wheel 的来源）。脚本顶部定义了要构建哪些 Python 和 Torch 版本的组合：

[kt-kernel/autosetup.sh:5-13](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/autosetup.sh#L5-L13) —— `PY_LIST`（Python 版本列表）和 `TORCH_LIST`（Torch 版本列表）决定了矩阵的维度；`WHEELS_DIR` 是产物输出目录。维护者就是用这套矩阵批量产出上传到 PyPI 的 wheel。

脚本还内置了产物自检函数，确保每个 wheel 都包含关键文件：

[kt-kernel/autosetup.sh:98-118](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/autosetup.sh#L98-L118) —— `verify_wheel_contents` 会打开 `.whl`（本质是 zip），检查里面一定有 `kt_kernel/kt_kernel_ext*.so`（编译出的扩展）以及 `kt_kernel/sft/__init__.py` 等必要条目，缺了就直接报错退出。这解释了为什么你 `pip install` 拿到的东西一定是完整的。

**(5) import 时挑变体的入口。** 在包的 `__init__.py` 里，第一件事就是探测 CPU 并加载扩展：

[kt-kernel/python/__init__.py:38-50](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/__init__.py#L38-L50) —— `_initialize_cpu()` 返回 `(扩展模块, 变体名)`，于是 `__cpu_variant__` 就是 import 时动态决定的（具体探测逻辑在 `_cpu_detect.py`，是 u2-l3 的内容）。这里只需记住：**`__cpu_variant__` 不是写死的，而是你这台机器 import 的那一刻算出来的**。

#### 4.1.4 代码实践

**实践目标**：用 PyPI 路径安装 kt-kernel，并确认它能 import、能告诉你选了哪个变体。

**操作步骤**：

1. 准备一个干净的 Python 3.11 环境（推荐 conda 或 venv）。
2. 安装：
   ```bash
   pip install kt-kernel
   ```
3. 进入 Python，打印关键信息：
   ```python
   import kt_kernel
   print(kt_kernel.__version__)      # 包版本，如 0.6.3.post1
   print(kt_kernel.__cpu_variant__)  # 本机选中的变体，如 'amx' / 'avx512' / 'avx2'
   ```

**需要观察的现象**：

- 安装过程**不应**出现 `cmake`、`nvcc`、`Building wheel for kt-kernel` 的本地编译日志（因为是预编译 wheel，直接下载解压）。
- `__cpu_variant__` 的值取决于你的 CPU：Intel Sapphire Rapids 及以上通常是 `amx`；较新的 AVX512 服务器可能是 `avx512_bf16` 之类；老旧机器回退到 `avx2`。

**预期结果**：两条 `print` 都能正常输出，无 `ImportError`。如果机器没有 GPU，import 同样应该成功（CUDA 功能自动关闭）。

> 若无合适硬件，可只执行到「能联网下载并看到安装日志」为止；import 阶段标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么官方不为 AMX、AVX512、AVX2 分别发布三个独立的包，而是塞进一个 wheel？

> **参考答案**：因为「选哪个变体」取决于运行机器的 CPU，而安装时未必知道最终在哪台机器上跑。把多变体打进同一个 wheel、把选择推迟到 import 时的自动探测，既让用户只需记一条 `pip install kt-kernel`，又能让同一份安装在不同 CPU 上都拿到最优内核。

**练习 2**：一台没有 NVIDIA GPU 的机器上 `pip install kt-kernel` 会失败吗？

> **参考答案**：不会失败。wheel 的 CUDA 运行时是静态链接的，且包在设计上支持「无 GPU 时自动禁用 CUDA 功能」（README 特性清单中的 `Works on CPU-only systems`）。import 与 CPU 推理照常工作，只是 GPU 加速不可用。

---

### 4.2 源码安装

#### 4.2.1 概念说明

当 PyPI wheel 不能满足需求时，就要从源码编译。典型场景有四类：

1. **AMD CPU（BLIS 后端）或 ARM CPU（KML 后端）**：官方 wheel 只内置 Intel/通用 x86 变体，AMD/ARM 优化需要本地编译接入对应矩阵库。
2. **自定义 CUDA 版本**：需要链接特定版本的 CUDA。
3. **可分发的可移植二进制**：要构建一份能在多种 CPU 上运行的产物（PyPI wheel 本身就是这么造的，但你也可能想自己造一份）。
4. **本地开发 / 想要针对本机 CPU 极致优化**：用 `-march=native` 编出只在这台机器上最快、但换台机器可能跑不了的二进制。

源码安装有两层脚本，分工很清楚：

- **根目录 `install.sh`**：是「编排者」，把整个 KTransformers（含 sglang 子模块 + kt-kernel）的安装串起来。
- **`kt-kernel/install.sh`**：是 kt-kernel 自己的「构建者」，负责装系统依赖、探测 CPU、编译安装 kt-kernel。

#### 4.2.2 核心流程

**根目录 `install.sh` 的编排顺序**（默认子命令 `all`）：

```text
./install.sh            # 默认 = all
   │
   ├── 1. init_submodules     # git submodule update --init --recursive（拉 sglang-kt）
   ├── 2. install_deps        # 调 kt-kernel/install.sh deps 装系统依赖
   ├── 3. read_kt_version     # 从 version.py 读版本号，导出给 sglang-kt
   ├── 4. install_sglang      # pip install sglang-kt（kvcache-ai fork）
   └── 5. install_kt_kernel   # 调 kt-kernel/install.sh build 编译安装 kt-kernel
```

它支持子命令拆步：`./install.sh sglang`（只装 sglang）、`./install.sh kt-kernel`（只装 kt-kernel）、`./install.sh deps`（只装系统依赖）。

**`kt-kernel/install.sh` 的两种模式**：

- **自动探测模式（默认）**：脚本读 `/proc/cpuinfo` 探测 AMX / AVX512 各子集，自动决定开哪些指令集，并用 `-march=native` 编出**只针对本机**的二进制——最快但不可移植。
- **手动模式（`--manual`）**：跳过自动探测，由你用环境变量 `CPUINFER_CPU_INSTRUCT`（`NATIVE`/`AVX512`/`AVX2`/`FANCY`）和 `CPUINFER_ENABLE_AMX`（`ON`/`OFF`）显式指定，用于造可分发、可移植的二进制。

#### 4.2.3 源码精读

**(1) 根目录 `install.sh` 的帮助与子命令。** 脚本顶部的 `usage()` 把它的全貌讲清楚了：

[install.sh:7-45](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/install.sh#L7-L45) —— 可以看到子命令 `all`/`sglang`/`kt-kernel`/`deps`，以及选项 `--skip-sglang`、`--skip-kt-kernel`、`--editable`、`--manual`、`--no-clean`。其中 `--manual` 和 `--no-clean` 会被「透传」给 `kt-kernel/install.sh`。

**(2) 默认 `all` 子命令的完整编排。** `install_all` 函数把上面流程图里的 5 步串起来：

[install.sh:163-210](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/install.sh#L163-L210) —— 注意它先 `init_submodules`、再 `install_deps`、再 `read_kt_version`（读 `version.py` 并导出 `SGLANG_KT_VERSION`）、再装 sglang、最后调 `install_kt_kernel`。结尾提示用 `kt doctor` 验证。

**(3) 版本号如何从根目录传递给 sglang-kt。** `read_kt_version` 读取唯一的版本源 `version.py`：

[install.sh:70-79](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/install.sh#L70-L79) —— 它 `exec` 执行 `version.py` 拿到 `__version__`，再 `export SGLANG_KT_VERSION`，让 sglang-kt 安装时与 kt-kernel 版本对齐。这呼应了 u1-l3 讲过的「单源真相」：全仓库只有 `version.py` 一处定义版本。

**(4) kt-kernel 自己的脚本：帮助与两种模式。** `kt-kernel/install.sh` 的 `usage()` 详解了自动探测 vs 手动配置：

[kt-kernel/install.sh:4-87](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/install.sh#L4-L87) —— 自动模式用 `NATIVE`（`-march=native`），并按探测结果决定 `CPUINFER_ENABLE_AMX` 等；手动模式则要求你显式设置环境变量，并给出「通用分发（AVX512+AMX=OFF）」「最大兼容（AVX2+AMX=OFF）」等推荐组合。还说明了软件回退（VNNI/BF16 缺失时有较慢的替代实现）。

**(5) CPU 特性探测函数。** 自动模式依赖 `detect_cpu_features`，它直接读 `/proc/cpuinfo`：

[kt-kernel/install.sh:149-196](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/install.sh#L149-L196) —— 函数从 `flags` 行里 grep `amx_tile`/`amx_int8`/`amx_bf16`、`avx512f`、`avx512_vnni`、`avx512_bf16`、`avx512_vbmi`，返回 5 个 0/1 标志。macOS 上这些一律置 0（无 AMX）。这正是「自动探测」的实现核心。

**(6) 构建步骤：从探测到 `pip install .`。** `build_step` 把探测结果导出为环境变量，再交给 pip+CMake 编译：

[kt-kernel/install.sh:198-243](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/install.sh#L198-L243) —— 这里能看到它先清理 `build/`（除非 `--no-clean`），自动模式下 `export CPUINFER_CPU_INSTRUCT=NATIVE`，再根据 `HAS_AMX` 设 `CPUINFER_ENABLE_AMX=ON/OFF`，并对 VNNI/BF16/VBMI 做细粒度探测（可被同名环境变量覆盖）。所有 `CPUINFER_*` 变量最终被 `python3 -m pip install .` 在底层传给 CMake（具体构建选项见 u2-l2）。

#### 4.2.4 代码实践

**实践目标**：走一遍「分步」源码安装，看清每一步在做什么（不追求完整编译大模型所需权重）。

**操作步骤**：

1. 克隆仓库（含子模块）：
   ```bash
   git clone --recursive https://github.com/kvcache-ai/ktransformers.git
   cd ktransformers
   ```
2. 建环境并查看脚本帮助（先不执行）：
   ```bash
   conda create -n kt-kernel python=3.11 -y && conda activate kt-kernel
   ./install.sh --help           # 看根脚本的子命令
   bash kt-kernel/install.sh --help   # 看 kt-kernel 脚本的两种模式
   ```
3. 只装系统依赖（最快、最安全的第一步）：
   ```bash
   ./install.sh deps
   ```
   观察它调用了 `kt-kernel/install.sh deps`，按你的发行版（Debian/RHEL/Arch/openSUSE）装 `libhwloc-dev`/`hwloc-devel`、`pkg-config` 等。
4. （可选）分步编译 kt-kernel：
   ```bash
   ./install.sh kt-kernel        # 默认自动探测本机 CPU
   ```

**需要观察的现象**：

- `./install.sh deps` 会检测发行版并选用 `apt`/`dnf`/`pacman`/`zypper` 之一。
- 自动模式会打印探测到的 AMX/VNNI/BF16/VBMI 结果，并提示「此二进制仅针对本机 CPU」。
- 若想造可移植二进制，改用：
  ```bash
  export CPUINFER_CPU_INSTRUCT=AVX512
  export CPUINFER_ENABLE_AMX=OFF
  ./install.sh kt-kernel --manual
  ```

**预期结果**：`deps` 子命令成功装上系统库；`kt-kernel` 子命令完成 `pip install .` 编译并安装。完整编译耗时较长（取决于 `CPUINFER_PARALLEL`），属于「待本地验证」的重型步骤。

> 提示：如果你只想理解流程而不真编译，重点跑 `--help` 和 `deps` 两步即可。

#### 4.2.5 小练习与答案

**练习 1**：根目录 `install.sh` 和 `kt-kernel/install.sh` 是什么关系？

> **参考答案**：根目录 `install.sh` 是「编排者」，负责拉 submodule、装系统依赖、装 sglang-kt、然后把剩余参数透传给 `kt-kernel/install.sh`；后者是「构建者」，只负责探测 CPU 并编译安装 kt-kernel 这一个包。前者在 `install_kt_kernel` 里以 `bash ./install.sh build ...` 的方式调用后者。

**练习 2**：自动模式（默认）和 `--manual` 模式分别适合什么场景？为什么自动模式会警告「此二进制仅针对本机 CPU」？

> **参考答案**：自动模式用 `-march=native`，会启用本机 CPU 的全部特性，性能最好但产物的 `.so` 可能含其他 CPU 没有的指令，换台机器可能直接 illegal instruction，所以只适合「本地使用」。`--manual` 模式让你显式指定一个保守的指令集基线（如 AVX512 或 AVX2）并关掉 AMX，产物可在该基线以上的任意 CPU 上运行，适合「分发、Docker 镜像、PyPI 打包、集群部署」。

---

### 4.3 验证步骤

#### 4.3.1 概念说明

装完之后必须验证，因为 kt-kernel 的安装结果依赖运行环境（CPU 变体、CUDA、sglang fork）。验证要回答三个问题：

1. **装上没有？** 版本号能读出来吗？
2. **选了哪个 CPU 变体？** 是不是你这台机器能拿到的最优解？
3. **CUDA 支持有没有？** GPU 路径通不通？装的是不是 kvcache-ai 的 sglang-kt fork（而不是官方 sglang）？

官方提供两条互补的验证途径：命令行 `kt version`（人看的汇总报告）和 Python 里 `import kt_kernel`（程序可读取的属性）。

#### 4.3.2 核心流程

```text
方式 A：命令行汇总
   kt version
     → 打印 kt-cli 版本、Python、平台、CUDA 版本
     → 打印 kt-kernel 版本、sglang-kt 版本及安装来源
     → （--verbose 追加 torch/transformers/llamafactory 等）

方式 B：Python 属性
   import kt_kernel
     → __version__        包版本
     → __cpu_variant__    选中的 CPU 变体
   from kt_kernel import kt_kernel_ext
     → CPUInfer 是否有 submit_with_cuda_stream  → 判断 CUDA 支持
```

#### 4.3.3 源码精读

**(1) README 给出的验证命令与预期输出。** 官方文档先用 `kt version` 验证 CLI：

[kt-kernel/README.md:204-227](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/README.md#L204-L227) —— 这里给出 `kt version` 的示例输出（Python、Platform、CUDA、kt-kernel 版本、sglang 版本），以及一条额外的 Python 验证 `from kt_kernel import KTMoEWrapper`。

> 注意：README 示例里 `kt-kernel: 0.x.x (amx)` 中的 `(amx)` 是示意性写法；实际的 `kt version` 命令（见下）展示的是 `kt-kernel` 的**包版本**，变体名要通过 `import kt_kernel; kt_kernel.__cpu_variant__` 单独读取。两者结合才是完整画像。

**(2) `kt version` 命令的真实实现。** 该命令汇总各组件版本与安装来源：

[kt-kernel/python/cli/commands/version.py:52-79](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/version.py#L52-L79) —— 它收集 Python 版本、平台、CUDA 版本（`detect_cuda_version()`），再单独列出 Packages 区块：`kt-kernel` 版本和 `sglang` 信息。其中 sglang 信息由 `_get_sglang_info()` 探测，会标注来源是「sglang-kt」「editable」「source: <git remote>」还是「PyPI」——这正好用来确认你装的是不是 kvcache-ai fork。

[kt-kernel/python/cli/commands/version.py:18-49](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/version.py#L18-L49) —— `_get_sglang_info` 优先取 `sglang-kt` 的版本，并通过 `is_kvcache_fork` 判断是否为官方推荐的 fork；若没装还会触发安装提示。

**(3) `__version__` 与 `__cpu_variant__` 的来源。** 在包入口 `__init__.py` 中：

[kt-kernel/python/__init__.py:66-80](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/__init__.py#L66-L80) —— `__version__` 优先用 `importlib.metadata.version("kt-kernel")`（已安装环境），找不到时回退读仓库根的 `version.py`（源码环境）。这与 u1-l3 讲的「单源真相」一致。

[kt-kernel/python/__init__.py:38-50](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/__init__.py#L38-L50) —— `__cpu_variant__` 来自 `_initialize_cpu()`，是 import 时探测并挑选变体的产物。

**(4) README 的 Python 验证片段（含 CUDA 判断）。** 官方还给了直接读属性、判断 CUDA 的范例：

[kt-kernel/README.md:123-138](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/README.md#L123-L138) —— 打印 `__cpu_variant__`、`__version__`，并用 `hasattr(cpu_infer, 'submit_with_cuda_stream')` 判断是否带 CUDA 支持。这段就是本模块验证实践 4.3.4 的官方出处。

#### 4.3.4 代码实践

**实践目标**：用两种方式完整验证一次安装，拿到版本、变体、CUDA、sglang 四项信息。

**操作步骤**：

1. 命令行验证：
   ```bash
   kt version              # 汇总报告
   kt version --verbose    # 追加 torch/transformers 等版本
   ```
2. Python 验证（与 README 一致）：
   ```python
   import kt_kernel

   print(f"CPU variant: {kt_kernel.__cpu_variant__}")
   print(f"Version:     {kt_kernel.__version__}")

   from kt_kernel import kt_kernel_ext
   cpu_infer = kt_kernel_ext.CPUInfer(4)
   has_cuda = hasattr(cpu_infer, "submit_with_cuda_stream")
   print(f"CUDA support: {has_cuda}")
   ```
3. 若要做 CPU-GPU 异构推理，确认 sglang 是 kvcache-ai fork：
   ```bash
   pip uninstall sglang -y        # 若误装了官方 sglang，先卸载
   pip install sglang-kt
   kt version                     # 看 sglang 行是否标注 (sglang-kt)
   ```

**需要观察的现象**：

- `kt version` 输出里 `kt-kernel` 行有版本号；sglang 行标注来源为 `sglang-kt` / `editable` / `source: ...` 之一。
- `__cpu_variant__` 与你的 CPU 匹配（AMX 机器得 `amx`）。
- 有 GPU 时 `CUDA support: True`；无 GPU 或装的是纯 CPU wheel 时为 `False`。

**预期结果**：四项信息（版本、变体、CUDA、sglang fork）都能正确读出。若 `kt version` 报 sglang 未安装，说明你只装了推理内核、还没装推理引擎——可按提示补装 `sglang-kt`。

> 排查技巧（来自 README）：用 `export KT_KERNEL_DEBUG=1` 后 `import kt_kernel` 可看到变体探测与加载的详细过程；用 `export KT_KERNEL_CPU_VARIANT=avx2` 可强制指定变体做对比测试。这两条命令的原理在 u2-l3 详述。

#### 4.3.5 小练习与答案

**练习 1**：`kt version` 输出里看到 sglang 行显示 `(PyPI)` 而不是 `(sglang-kt)`，可能意味着什么？该怎么办？

> **参考答案**：说明当前环境装的是**官方 sglang** 而非 kvcache-ai 的 `sglang-kt` fork，kt-kernel 的异构推理需要后者。应先 `pip uninstall sglang -y` 卸载官方版，再 `pip install sglang-kt`，然后重新 `kt version` 确认来源变为 `sglang-kt`。

**练习 2**：同一台机器上，源码环境（未 `pip install`）和已安装环境里 `kt_kernel.__version__` 分别从哪里读到？

> **参考答案**：已安装环境优先用 `importlib.metadata.version("kt-kernel")` 从包元数据读；若包未安装（源码环境、包元数据找不到），则回退读取仓库根目录的 `version.py` 里的 `__version__`。两种途径最终都指向同一个版本源，保证一致。

---

## 5. 综合实践

**任务**：为一个「双路 Intel Sapphire Rapids + 1 张 RTX 4090」的目标机器，规划一次完整的安装与验证，并解释每一步的选择。

**步骤**：

1. **选路径**：该机器是标准 Intel x86 + NVIDIA GPU，且不需要 AMD/ARM 后端 → 选择 **PyPI 路径**（最省事）。
2. **安装**：
   ```bash
   conda create -n kt-kernel python=3.11 -y && conda activate kt-kernel
   pip install kt-kernel sglang-kt
   ```
3. **验证四件套**：
   - `kt version` → 确认 kt-kernel 版本、sglang 来源为 `sglang-kt`。
   - `python -c "import kt_kernel; print(kt_kernel.__cpu_variant__)"` → 期望 `amx`（Sapphire Rapids 支持 AMX）。
   - `python -c "from kt_kernel import kt_kernel_ext; print(hasattr(kt_kernel_ext.CPUInfer(4), 'submit_with_cuda_stream'))"` → 期望 `True`（有 4090）。
4. **反思题**：如果同一台机器你要为「一批型号不统一的服务器」造一份可分发的二进制，应该改走哪条路径、用什么参数？
   > 参考答案：改走**源码安装 + 手动模式**，例如 `export CPUINFER_CPU_INSTRUCT=AVX512 && export CPUINFER_ENABLE_AMX=OFF && ./install.sh kt-kernel --manual`，造出可在任意 AVX512（2017+）CPU 上运行的二进制；这正是官方用 `autosetup.sh` 造 PyPI wheel 的思路。

> 这个综合实践把「选路径 → 安装 → 验证 → 可移植构建取舍」串起来，正好覆盖本讲的三个最小模块。

## 6. 本讲小结

- kt-kernel 有两条安装路径：**PyPI 预编译 wheel**（`pip install kt-kernel`，推荐大多数用户）和**源码编译**（`./install.sh`，用于 AMD/ARM、自定义 CUDA、可移植二进制、本机极致优化）。
- 官方 wheel 的两大省心特性：**一个包内置多个 CPU 变体**（AMX/AVX512 各子集/AVX2），变体选择推迟到 import 时自动完成；**静态 CUDA 运行时**，无需安装 CUDA toolkit，无 GPU 时自动禁用 CUDA。
- 源码安装分两层：根目录 `install.sh` 是编排者（submodule→deps→sglang→kt-kernel），`kt-kernel/install.sh` 是构建者（`deps`/`build`/`all`，自动探测 vs `--manual`）。
- 安装后用 **`kt version`**（命令行汇总）和 **`import kt_kernel`**（读 `__version__`、`__cpu_variant__`、判断 CUDA）两条途径验证；做异构推理还要确认装的是 `sglang-kt` fork 而非官方 sglang。
- `__cpu_variant__` 是 import 时动态探测的、`__version__` 优先取包元数据再回退 `version.py`——两者都体现了「单源真相 + 运行时自适应」的设计。

## 7. 下一步学习建议

- 下一讲 **u2-l2 构建系统与 CPU 指令集配置** 会深入 `CMakeLists.txt` 的构建选项（`KTRANSFORMERS_USE_CUDA`、`KTRANSFORMERS_CPU_USE_AMX` 等）、`CPUINFER_*` 环境变量与 `cmake/DetectCPU.cmake` 的指令集检测——如果你选了源码安装，这是必读。
- 之后 **u2-l3 运行时 CPU 变体检测与加载** 会拆开 `_cpu_detect.py`，讲清多变体 `.so` 的优先级、回退链与 `KT_KERNEL_CPU_VARIANT`/`KT_KERNEL_DEBUG` 的用法，承接本讲验证步骤里提到的两个排查技巧。
- 如果你想跳过构建细节、直接用起来，可先去 **u3 单元（KT CLI 入门）** 学 `kt run` / `kt model` 等命令，等需要定制构建时再回来看 u2-l2、u2-l3。
- 建议继续阅读的源码：`kt-kernel/README.md` 的 *Installation* 与 *Verification* 小节（权威安装文档）、`install.sh` 与 `kt-kernel/install.sh` 的 `usage()`（最准的用法说明）。
