# 从源码构建与运行 CUDA-Q

## 1. 本讲目标

读完本讲后，你应该能够：

- 说出 `scripts/build_cudaq.sh` 的 `-c / -j / -p / -i / -v / -B / -s / -I` 等参数分别做什么，并能用它完成一次本地构建。
- 解释 CUDA-Q 为什么依赖一套自带的 LLVM/MLIR（当前 22.1.4），以及 cuQuantum / cuTensor / CUDA 这三项 GPU 依赖在构建中是「可选的、链接期决定的」。
- 学会用 `nvq++` 把一个 C++ 量子内核编译成可执行程序并运行，并用 `ctest` / `pytest` 触发测试。

本讲是后续所有讲义的「地基」：只有先在本地跑通「编译 → 安装 → 运行」这条主链路，后面阅读 Quake 方言、运行时、后端等源码时才能随时动手验证。

## 2. 前置知识

- **CMake + Ninja**：CUDA-Q 用 CMake 描述构建规则，用 Ninja 作为底层执行器。构建脚本本质上是「准备一堆 CMake 变量 → 调用 `cmake -G Ninja` → 调用 `ninja install`」的薄壳。
- **子模块（git submodule）**：CUDA-Q 把 LLVM、qpp、fmt、spdlog 等第三方代码放在 `tpls/` 下作为子模块管理，其中 `tpls/llvm` 是体量最大、最关键的一个。
- **开发容器（dev container）**：一个预装好全部依赖的 Docker 镜像。NVIDIA 官方推荐在容器里开发，这样不污染本机环境，也能避免「我这里能编译、你那里不行」的差异。
- **链接期后端选择**：承接 u1-l1/u1-l2 的结论——CUDA-Q 不在源码里指定具体模拟器，而是在编译/链接阶段决定链接哪个后端。构建时是否安装了 GPU 库，直接决定了哪些后端会被编进产物里。

如果你对 CMake/Ninja 完全陌生，只要记住一句话即可：**CMake 负责「画施工图」，Ninja 负责「按图施工」**，本讲的构建脚本就是替你把这两步串起来的自动化工具。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [Building.md](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Building.md) | 官方「从源码构建」总说明，给出推荐路径、安装前缀、GPU 依赖、失败清理等。 |
| [Dev_Setup.md](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Dev_Setup.md) | 开发环境说明：开发容器、VS Code、macOS 手动安装步骤。 |
| [scripts/build_cudaq.sh](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_cudaq.sh) | CUDA-Q 主构建脚本：解析参数、检测依赖、生成 CMake 配置、执行 ninja install。 |
| [scripts/build_llvm.sh](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_llvm.sh) | 从 `tpls/llvm` 子模块源码编译 Clang/MLIR/LLVM，必要时由主脚本自动触发。 |
| [scripts/set_env_defaults.sh](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/set_env_defaults.sh) | 各安装前缀的「平台相关默认值」，被主脚本 `source` 引入。 |
| [docs/sphinx/applications/cpp/grover.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/applications/cpp/grover.cpp) | 一个完整的 Grover 算法示例，本讲用它演示 `nvq++` 工作流。 |

## 4. 核心概念与源码讲解

### 4.1 构建脚本 build_cudaq.sh 与环境变量

#### 4.1.1 概念说明

CUDA-Q 的构建入口不是直接敲 `cmake`，而是一个 bash 脚本 `scripts/build_cudaq.sh`。它的存在意义是：**CUDA-Q 的构建涉及太多「条件分支」（有没有 GPU、用哪个链接器、要不要 OpenMP、要不要 Python、要不要 sanitizer……），手动维护一长串 CMake 参数既容易出错也难复现**。脚本把这些分支集中处理，让用户只需关心少数几个开关。

脚本的核心职责有四项：

1. 解析命令行参数（`-c / -j / -p …`）。
2. 探测环境（CUDA 版本、cuQuantum/cuTensor、lld、OpenMP、ccache）。
3. 把探测结果翻译成一串 `-D...=...` 的 CMake 变量，调用 `cmake -G Ninja` 配置工程。
4. 调用 `ninja install` 真正编译并安装到 `CUDAQ_INSTALL_PREFIX`。

理解了这四步，整段脚本就读懂了八成。

#### 4.1.2 核心流程

用伪代码描述 `build_cudaq.sh` 的主流程：

```text
source set_env_defaults.sh          # 设置各 *_INSTALL_PREFIX 的默认值
解析命令行 getopts                   # -c -t -j -v -B -i -s -p -I
mkdir build && cd build              # 默认在当前目录下建 build/
if 安装前驱 (-p):
    source install_prerequisites.sh
检测 CUDA 版本 (需要 >= 12.0)        # 找不到则跳过 GPU 组件
检测 cuQuantum / cuTensor
选择链接器 (lld 优先, 否则系统链接器)
检测 OpenMP、ccache
组装 cmake_args = -G Ninja + 一堆 -D...
cmake $cmake_args                    # 生成 Ninja 构建文件
ninja install                        # 编译 + 安装
拷贝 LICENSE/NOTICE/set_env.sh, 写 build_config.xml
```

整个流程里有两个目录最值得记住：

- **build 目录**：脚本默认在「你运行脚本时的当前工作目录」下建一个 `build/`（可用 `-B` 改名）。所有 CMake 缓存、日志都在这里。
- **安装目录 `CUDAQ_INSTALL_PREFIX`**：默认是 `$HOME/.cudaq`，最终产物（`nvq++`、`cudaq-quake`、Python 包等）都装到这里。

#### 4.1.3 源码精读

**默认安装前缀**：脚本第一行业务代码就把安装目录定下来了——[scripts/build_cudaq.sh:56](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_cudaq.sh#L56) 设 `CUDAQ_INSTALL_PREFIX` 默认为 `$HOME/.cudaq`。如果你想装到别处，要么 export 这个变量，要么……注意它**没有**等价的命令行开关（脚本没暴露 `-prefix`），所以「改安装位置」只能靠环境变量。

**参数解析**：脚本用 `getopts` 处理选项，定义在 [scripts/build_cudaq.sh:94](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_cudaq.sh#L94)。各开关含义在文件顶部的 Usage 注释里列得很清楚，关键几个：

| 开关 | 作用 | 典型用法 |
| --- | --- | --- |
| `-c <类型>` | 构建类型，默认 `Release` | `-c Debug` 调试 |
| `-j <N>` | 限制并行任务数（缓解内存不足） | `-j 4` |
| `-p` | 先安装前置依赖再构建 | 容器外/macOS 首次必加 |
| `-i` | 增量构建，不 `rm -rf build/*` | 反复改代码时用 |
| `-v` | 详细输出（不重定向到日志） | 排查问题时用 |
| `-B <目录>` | 指定 build 目录名 | `-B build-debug` |
| `-s` | 开启 ASan/UBSan | 查内存错误 |
| `-I` | 只安装（跳过 configure/build） | 已配置后重装 |
| `--` | 之后的参数透传给 cmake | `-- -DCUDAQ_LIT_JOBS=2` |

**关于 `-j` 的设计直觉**：Building.md 专门强调过——[Building.md:22-25](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Building.md#L22-L25) 说明如果构建时内存吃紧，可以用 `-j N` 限制并行度，`N` 越小越不易 OOM 但越慢。这是因为 LLVM/CUDA-Q 都是 C++ 重型项目，并行编译会同时驻留大量内存。

**build 目录与日志**：[scripts/build_cudaq.sh:121-128](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_cudaq.sh#L121-L128) 建 `build/` 并清空 `build/logs/`。除非用了 `-i`（增量），否则会 `rm -rf *` 全清——所以**不要把你重要的东西放在 `build/` 里**。

**安装后产物**：[scripts/build_cudaq.sh:341-352](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_cudaq.sh#L341-L352) 把 LICENSE、NOTICE、`set_env.sh` 拷进安装目录，并写一份 `build_config.xml`，记录构建时用到的 `LLVM_INSTALL_PREFIX` 等路径。这份 XML 很有用——它解释了「为什么 CUDA-Q 安装目录不是自包含的」：运行时仍然依赖构建时位置固定的那些 LLVM 工具。

**安装前缀的平台默认值**：`set_env_defaults.sh` 区分 macOS / Linux 给出不同默认值。Linux 下默认走系统级路径，见 [scripts/set_env_defaults.sh:56-68](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/set_env_defaults.sh#L56-L68)，例如 `LLVM_INSTALL_PREFIX` 默认 `/opt/llvm`、cuQuantum 默认 `/opt/nvidia/cuquantum`；macOS 下则统一放 `~/.local/`，见 [scripts/set_env_defaults.sh:23-55](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/set_env_defaults.sh#L23-L55)。所有变量都是 `${VAR:-默认}` 形式——**你提前 export 过的值不会被覆盖**。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：在不真正运行构建的前提下，仅通过阅读脚本，画出 `build_cudaq.sh` 注入给 CMake 的「变量清单」。

**操作步骤**：

1. 打开 [scripts/build_cudaq.sh:264-295](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_cudaq.sh#L264-L295)（`cmake_args` 的组装段）。
2. 列出其中所有 `-DXXX=...` 形式的 CMake 变量，逐个标注「值来自哪个环境变量或探测结果」。
3. 特别留意几个布尔变量：`CUDAQ_ENABLE_PYTHON`（默认 `TRUE`）、`CUDAQ_BUILD_TESTS`（默认 `TRUE`）、`CMAKE_COMPILE_WARNING_AS_ERROR`（默认 `ON`）。

**需要观察的现象 / 预期结果**：

- 你会发现 `CMAKE_COMPILE_WARNING_AS_ERROR` 取自 `CUDAQ_WERROR`，默认 `ON`——也就是说**默认构建里警告即报错**。如果你只是想临时绕过某个警告，应 `export CUDAQ_WERROR=OFF` 后再跑脚本（这一点脚本顶部注释 [scripts/build_cudaq.sh:48-50](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_cudaq.sh#L48-L50) 也写了）。

本实践为「源码阅读型」，不要求实际编译；其结果（变量清单）可直接复用到下一节的真机构建。

#### 4.1.5 小练习与答案

**练习 1**：你想把 CUDA-Q 装到 `/opt/cudaq` 而不是默认的 `$HOME/.cudaq`，应该怎么做？为什么不能用 `-c` 之类的命令行开关？

> **答案**：在运行脚本前 `export CUDAQ_INSTALL_PREFIX=/opt/cudaq`。脚本没有为安装目录提供命令行开关（见 `getopts` 定义里没有对应选项），只能通过环境变量覆盖；安装完成后记得按 [Building.md:32-35](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Building.md#L32-L35) 把 `${CUDAQ_INSTALL_PREFIX}/bin` 加进 `PATH`、把 `${CUDAQ_INSTALL_PREFIX}` 加进 `PYTHONPATH`。

**练习 2**：连续改一个 `.cpp` 文件做实验时，每次都用默认方式跑脚本会发生什么？怎样改进？

> **答案**：默认 `clean_build=true`，脚本会 `rm -rf build/*` 后从头配置（[scripts/build_cudaq.sh:125-127](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_cudaq.sh#L125-L127)），每次都全量重配，很慢。反复迭代时应加 `-i`（增量构建，不清空），或干脆直接进 `build/` 跑 `cmake .. && ninja install`（Building.md 的「Manual/Incremental Builds」段也是这么建议的）。

### 4.2 LLVM/MLIR 与 GPU 工具链依赖

#### 4.2.1 概念说明

CUDA-Q 的编译器是基于 **MLIR** 实现的（Quake、CC 都是自定义方言），而 MLIR 是 LLVM 项目的一部分。因此 CUDA-Q 必须依赖一套**特定版本**的 LLVM/MLIR——当前 pin 在 **LLVM 22.1.4**（见 [Building.md:72](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Building.md#L72) 与 [Building.md:126](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Building.md#L126)）。系统自带的 LLVM 版本通常对不上，所以 CUDA-Q 走「自带 LLVM 子模块 + 自行编译」的路线。

除了 LLVM，还有一组 **GPU 依赖**：

- **CUDA Toolkit**（`nvcc`）：构建 GPU 设备代码需要，版本要求 **>= 12.0**。
- **cuQuantum**：GPU 状态向量 / 张量网络模拟后端的底层库。
- **cuTensor**：张量运算库。

关键设计：这三项 GPU 依赖**全部是可选的**。脚本会探测它们的存在，找不到就**静默跳过对应组件**，而不是报错退出——这正是 u1-l1 讲过的「链接期决定后端」在构建侧的体现。

#### 4.2.2 核心流程

构建期对依赖的处理可以分成两条独立的链路：

```text
链路 A：LLVM/MLIR（必需）
  开发容器 → 已预装 LLVM（直接用）
  非容器   → build_cudaq.sh 检测到没有 llvm-config
            → 自动调用 build_llvm.sh
            → 从 tpls/llvm 子模块 checkout 指定 commit
            → 打补丁 → cmake + ninja 编译 Clang/MLIR/nanobind

链路 B：GPU 三件套（可选）
  nvcc --version → 取主版本号
   ├─ < 12.0 或缺失 → unset cuda_driver, GPU 组件被省略
   └─ >= 12.0    → 继续探测 cuQuantum / cuTensor
                   ├─ 找到 → 编进对应后端
                   └─ 没找到 → 提示「Some backends will be omitted」
```

记忆要点：**链路 A 是硬依赖（没有 LLVM 编不动），链路 B 是软依赖（没有 GPU 也能编译，只是少几个后端）**。

#### 4.2.3 源码精读

**LLVM 版本与子模块**：`.gitmodules` 把 LLVM 注册为子模块 `tpls/llvm`，地址是官方仓库——见 [.gitmodules:7-11](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/.gitmodules#L7-L11)。`build_llvm.sh` 不会盲目拉 `main`，而是 checkout 子模块 pin 的具体 commit，参见 [scripts/build_llvm.sh:88-96](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_llvm.sh#L88-L96)。Building.md 把这一点讲得很直白：CUDA-Q 就是要用「子模块当前 pin 的那个 LLVM 提交」，目前是 22.1.4——[Building.md:123-128](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Building.md#L123-L128)。

**build_cudaq.sh 何时触发 build_llvm.sh**：[Building.md:143-145](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Building.md#L143-L145) 解释——主脚本去 `LLVM_INSTALL_PREFIX/bin` 下找 `llvm-config`，找不到就自动调用 `build_llvm.sh`。所以在开发容器里（LLVM 已预装）你几乎察觉不到 `build_llvm.sh` 的存在；在裸机或 macOS 上首次构建，它会先花很长时间编译 LLVM。Building.md 提醒：**编译 LLVM 大约需要 64GB 内存**——[Building.md:147-151](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Building.md#L147-L151)。

**build_llvm.sh 编译什么**：默认工程列表 `LLVM_PROJECTS='clang;lld;mlir;python-bindings'`（[scripts/build_llvm.sh:35-37](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_llvm.sh#L35-L37)），即 Clang 编译器、lld 链接器、MLIR 框架、MLIR 的 Python 绑定。如果带了 `python-bindings`，还会顺带编译 nanobind（CUDA-Q Python 前端的绑定工具）——[scripts/build_llvm.sh:71-86](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_llvm.sh#L71-L86)。脚本还会对 LLVM 打补丁，补丁放在 `tpls/customizations/llvm`，见 [scripts/build_llvm.sh:98-123](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_llvm.sh#L98-L123)（且会跳过已应用过的补丁，幂等可重入）。

**CUDA 版本探测**：[scripts/build_cudaq.sh:163-171](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_cudaq.sh#L163-L171) 跑 `nvcc --version` 抓版本号，主版本 `< 12`（或根本没 nvcc）就打印「GPU-accelerated components will be omitted」并 `unset cuda_driver`。注意这是「降级」不是「失败」——构建照常继续。

**cuQuantum / cuTensor 探测**：紧接着的一段，[scripts/build_cudaq.sh:174-192](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_cudaq.sh#L174-L192)，脚本会先尝试从 pip 安装的 `cuquantum-python-cu*` / `cutensor-cu*` 包里推断安装路径；找不到则提示设置 `CUQUANTUM_INSTALL_PREFIX` / `CUTENSOR_INSTALL_PREFIX`，同样「省略部分后端」而非报错。Building.md 也指向 `install_prerequisites.sh` 看这些库怎么装——[Building.md:46-64](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Building.md#L46-L64)。

**开发容器：把链路 A 一次性解决**：Dev_Setup.md 推荐「在容器里开发」，因为容器预装了匹配版本的 LLVM，免去本机编译——[Dev_Setup.md:6-14](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Dev_Setup.md#L6-L14)。VS Code 的 Dev Containers 扩展能一键 `Open Folder in Container`，左下角会出现 `Development Container: cudaq-dev`——[Dev_Setup.md:43-74](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Dev_Setup.md#L43-L74)。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：理解「为什么换一台没装 GPU 的机器，CUDA-Q 仍然能编译」。

**操作步骤**：

1. 阅读 [scripts/build_cudaq.sh:163-193](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_cudaq.sh#L163-L193) 的 CUDA / cuQuantum / cuTensor 探测段。
2. 追踪 `cuda_driver` 这个变量：当 CUDA 缺失时它被 `unset`，然后看 [scripts/build_cudaq.sh:289-295](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_cudaq.sh#L289-L295) 里 `CMAKE_CUDA_COMPILER` 是否还会被加进 `cmake_args`（注意该段被 `if [ "$(uname)" != "Darwin" ]` 包裹，且引用了 `$cuda_driver`）。
3. 对照 Building.md 关于「Developing code in this repository does not require you to have a GPU」的论述（[Building.md:55-60](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Building.md#L55-L60)）。

**需要观察的现象 / 预期结果**：

- 你应当能解释清楚：没有 GPU 时，`CMAKE_CUDA_COMPILER` 这一行的值变成空字符串，CMake 便不会启用 CUDA 语言，下游所有 `.cu` 编译目标被自然跳过——这就是「GPU 是软依赖」在脚本层的实现机制。
- 进一步可以推断：在一台纯 CPU 机器上构建出来的 CUDA-Q，运行时只能用 `qpp` 这类 CPU 模拟器，`custatevec`/`cutensornet` 等后端根本不会被链接进来。

本实践为源码阅读型，结论可在第 5 节综合实践中用 `nvidia-smi` 是否可用来佐证。

#### 4.2.5 小练习与答案

**练习 1**：在开发容器里第一次跑 `./scripts/build_cudaq.sh`（不带 `-p`），它会去编译 LLVM 吗？为什么？

> **答案**：不会。容器里已经预装了与 pin commit 匹配的 LLVM，`LLVM_INSTALL_PREFIX/bin/llvm-config` 存在，主脚本检测到后就直接复用，不会触发 `build_llvm.sh`。只有「不在容器里」或「`LLVM_INSTALL_PREFIX` 指向一个没装好的目录」时才会自动编 LLVM（依据 [Building.md:37-40](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Building.md#L37-L40) 与 [Building.md:143-145](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Building.md#L143-L145)）。

**练习 2**：你想用一个更新的 LLVM 主线版本来构建 CUDA-Q，需要改哪些地方？

> **答案**：先把 `tpls/llvm` 子模块切到目标 commit，再 `export LLVM_INSTALL_PREFIX=<新安装路径>`，然后（如果是首次）跑 `build_llvm.sh` 或直接 `build_cudaq.sh -p`。注意 `LLVM_INSTALL_PREFIX` 只在首次 CMake configure 需要，之后会缓存进 `CMakeCache.txt`（依据 [Building.md:130-141](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Building.md#L130-L141)）。当然，换非官方支持版本的 LLVM 可能导致编译失败。

### 4.3 nvq++ 工作流与测试入口

#### 4.3.1 概念说明

构建完成后，`CUDAQ_INSTALL_PREFIX/bin` 下会出现一整套工具，其中用户最常用的是 **`nvq++`**。它是一个 bash 编排脚本（详见 u4-l5），对外表现得像「专门编译量子内核的 C++ 编译器」：你把一个含 `__qpu__` 内核的 `.cpp` 文件交给它，它替你完成「翻译成 Quake → 注册到运行时 → 降低到 QIR → 链接」的全套流程，产出可执行文件。

> 提醒：`nvq++` 的内部编排逻辑不是本讲重点（u4-l5 会逐段拆解），本讲只把它当作「黑盒命令」来用，重点是跑通端到端流程。

至于**测试入口**，CUDA-Q 主要有三类测试（u8-l1 会专门讲），本讲只需掌握「怎么触发」：

- 编译器回归测试：用 `lit` / FileCheck，位于 `cudaq/test/`。
- 运行时单元测试：GoogleTest，在 `build/` 目录下用 `ctest` 跑。
- Python 端测试：用 `pytest`。

#### 4.3.2 核心流程

一次典型的「编译 → 运行」操作：

```text
nvq++ grover.cpp -o grover.x     # 编排四步编译流水线，产出 grover.x
./grover.x [可选的目标比特串]     # 默认目标 0b1011，打印最可能的测量结果
```

测试侧则分两步：

```text
cd build
ctest                            # 跑所有启用的测试（也可 -R <正则> 筛选）
pytest <某个 python 测试目录>     # 跑 Python 测试
```

**前置条件**：`nvq++` 和测试工具必须能被 shell 找到。在容器里或按默认路径安装时通常已就绪；自定义安装路径时则需先 `source` 安装目录里的 `set_env.sh`，或手动 export `PATH`/`PYTHONPATH`——[Building.md:32-35](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Building.md#L32-L35)。

#### 4.3.3 源码精读

**示例文件自带的编译指令**：Grover 示例文件头部的注释直接给出了最简用法——[docs/sphinx/applications/cpp/grover.cpp:1-4](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/applications/cpp/grover.cpp#L1-L4)：

```cpp
// nvq++ grover.cpp -o grover.x && ./grover.x
```

注意它**没有指定任何后端/目标**——默认就会用本机 CPU 的 `qpp` 模拟器，这正是 u1-l1 讲的「源码不指定具体模拟器」的体现。

**示例里的 CUDA-Q 元素**：这个文件虽然小，却几乎涵盖了 u1、u2 的大部分核心 API，值得先扫一眼：

- `__qpu__` 标注的量子内核结构体——[docs/sphinx/applications/cpp/grover.cpp:23-37](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/applications/cpp/grover.cpp#L23-L37)。
- `cudaq::qvector` 分配比特、`h`/`x`/`z<cudaq::ctrl>` 等门、`mz` 测量——[docs/sphinx/applications/cpp/grover.cpp:29-35](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/applications/cpp/grover.cpp#L29-L35)。
- `cudaq::compute_action` 实现「计算-作用-反计算」模式——[docs/sphinx/applications/cpp/grover.cpp:15-21](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/applications/cpp/grover.cpp#L15-L21)。
- 在 `main` 里用 `cudaq::sample(内核{}, 参数...)` 触发执行并取 `most_probable()`——[docs/sphinx/applications/cpp/grover.cpp:56-60](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/applications/cpp/grover.cpp#L56-L60)。这些 API 的细节会在 u1-l4、u2 系列讲义里展开，本讲只需知道「这套源码能被 nvq++ 编译并运行」即可。

**测试入口**：Building.md 在结尾明确给出运行测试的方法——构建成功后进入 `build` 目录执行 `ctest`，全部通过即可开始开发，见 [Building.md:42-44](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Building.md#L42-L44)。注意 `build_cudaq.sh` 默认 `CUDAQ_BUILD_TESTS=TRUE`（[scripts/build_cudaq.sh:281](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_cudaq.sh#L281)），所以只要你没显式关掉，`build/` 里就会有 CTest 注册的测试目标。

#### 4.3.4 代码实践（运行型）

**实践目标**：用 `nvq++` 编译并运行 Grover 示例，验证端到端工具链可用。

**操作步骤**：

1. 确认 `nvq++` 可用：`which nvq++`（若失败，先 `source $CUDAQ_INSTALL_PREFIX/set_env.sh` 或按 Building.md 配好 PATH）。
2. 进入示例目录并编译：
   ```bash
   cd docs/sphinx/applications/cpp
   nvq++ grover.cpp -o grover.x
   ```
3. 运行默认目标（默认 `0b1011`）：
   ```bash
   ./grover.x
   ```
4. 也可传入自定义目标比特串（命令行第一个参数按二进制解析，见 [docs/sphinx/applications/cpp/grover.cpp:57](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/applications/cpp/grover.cpp#L57)），例如 `./grover.x 1100`。

**需要观察的现象 / 预期结果**：

- `nvq++` 会打印若干编译阶段的日志（取决于日志级别），最后生成 `grover.x`。
- 运行后应输出类似 `Found string 1011`（默认目标）的行——Grover 算法放大量了目标态的概率，使它成为最可能的测量结果。
- 由于是采样算法，结果在统计上是稳定的，但严格说每次运行基于伪随机采样；如果输出与预期不符（比如出现别的串），多为采样次数不足或编译/后端问题。

**待本地验证**：具体打印格式与 `nvq++` 的中间日志文本可能随版本变化，请在本地以实际输出为准。本实践不依赖 GPU，纯 CPU（`qpp` 后端）即可完成。

#### 4.3.5 小练习与答案

**练习 1**：上面的编译命令没有 `-target xxx`，它是怎么决定用哪个模拟器的？

> **答案**：CUDA-Q 有一个默认目标（由安装时的默认 YAML 平台配置决定，通常落到本机 CPU 的 `qpp` 后端）。源码里不写死模拟器，运行时由「平台/目标」抽象按链接期可用的后端来选择——这正是 u1-l1 强调的「后端在链接期切换」。如何显式指定目标，会在 u6-l4（平台与目标配置）讲。

**练习 2**：构建成功后想只跑名字里含 `sample` 的测试，应该用什么命令？

> **答案**：进入 `build/` 目录后用 `ctest -R sample`。`-R` 接正则，只运行匹配的测试名；想看有哪些测试可先 `ctest -N` 列清单（依据 [Building.md:42-44](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Building.md#L42-L44) 的 `ctest` 入口）。Python 测试则用 `pytest -k sample`。

## 5. 综合实践

**任务**：在开发容器中跑通「构建 → 安装 → 编译示例 → 运行 → 测试」的完整闭环，并记录每个阶段的产物位置与关键日志。这是后续所有讲义动手环节的共同前提。

**操作步骤**：

1. **准备环境**：按 [Dev_Setup.md:43-74](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Dev_Setup.md#L43-L74) 用 VS Code 在容器里打开仓库；确认容器里 `llvm-config` 可用、`nvidia-smi` 是否可用（决定 GPU 组件是否会被编入）。
2. **构建**：在仓库根目录运行
   ```bash
   ./scripts/build_cudaq.sh
   ```
   （若内存吃紧加 `-j 4`；若不在容器或 macOS 首次构建加 `-p`）。构建日志在 `build/logs/` 下的 `cmake_output.txt`、`ninja_output.txt` 等。
3. **验证安装**：检查 `CUDAQ_INSTALL_PREFIX`（默认 `$HOME/.cudaq`）下应出现 `bin/nvq++`、`bin/cudaq-quake` 以及 `build_config.xml`；可 `cat build_config.xml` 看构建时记录的各依赖路径。
4. **编译并运行 Grover**：
   ```bash
   cd docs/sphinx/applications/cpp
   nvq++ grover.cpp -o grover.x && ./grover.x
   ```
   记录终端输出的 `Found string ...`。
5. **跑测试**：
   ```bash
   cd build
   ctest -R sample      # 跑一个含 sample 的测试，确认通过
   ```

**需要观察与记录的关键现象**：

- 步骤 2 是否触发了 `build_llvm.sh`（容器里通常不会）；`build/logs/ninja_output.txt` 的尾部是否报告 `Installed CUDA-Q in directory: ...`。
- 步骤 3 `build_config.xml` 里 `LLVM_INSTALL_PREFIX` 的实际值；GPU 是否被探测到（看构建日志里是否出现 `CUDA version ... detected` 还是 `GPU-accelerated components will be omitted`）。
- 步骤 4 Grover 输出的目标串。
- 步骤 5 测试是否全部通过（`100% tests passed`）。

**预期结果**：构建成功安装到 `$HOME/.cudaq`，Grover 打印出默认目标 `1011` 的串，筛选出的测试通过。

**待本地验证**：本综合实践涉及一次较重的完整构建（容器内不含 LLVM 编译时通常数十分钟级，裸机首次构建 LLVM 可能更久且需大内存）。具体耗时与日志文本以本地实测为准；若构建中途失败，请按 [Building.md:155-169](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Building.md#L155-L169) 的清理建议（`rm -rf build` 或重置对应的 `*_INSTALL_PREFIX` 目录）后重试。**本讲义未在撰写时实际执行这些命令，所有结果均待本地验证。**

## 6. 本讲小结

- CUDA-Q 的构建入口是 `scripts/build_cudaq.sh`：它解析 `-c/-j/-p/-i/-v/-B/-s/-I` 等参数，探测环境后生成 CMake 配置并执行 `ninja install`，默认安装到 `$HOME/.cudaq`、build 目录默认在当前目录的 `build/`。
- 安装路径只能用环境变量 `CUDAQ_INSTALL_PREFIX` 覆盖（脚本无对应命令行开关）；所有 `*_INSTALL_PREFIX` 的平台默认值集中在 `set_env_defaults.sh`，且不会覆盖你已 export 的值。
- CUDA-Q 硬依赖一套自带版本的 LLVM/MLIR（当前 pin 在 **22.1.4**），开发容器里已预装，裸机/macOS 首次构建会自动调用 `build_llvm.sh` 从 `tpls/llvm` 子模块源码编译（约需 64GB 内存）。
- CUDA Toolkit（>=12.0）、cuQuantum、cuTensor 三项 GPU 依赖**全部可选**：脚本探测不到就静默省略对应后端，不会让构建失败——这是「链接期决定后端」在构建侧的直接体现。
- 用户侧的命令行入口是 `nvq++`，它把含 `__qpu__` 内核的 `.cpp` 一键编译成可执行文件；Grover 示例 `nvq++ grover.cpp -o grover.x && ./grover.x` 可在纯 CPU 上跑通。
- 测试入口：`cd build && ctest`（默认 `CUDAQ_BUILD_TESTS=TRUE`），Python 测试用 `pytest`；细节分类留待 u8-l1。

## 7. 下一步学习建议

- 想真正写出自己的第一个内核，进入 **u1-l4（第一个 C++ 量子内核）**，它会把本讲里「黑盒使用」的 `__qpu__`、`cudaq::qvector`、`cudaq::sample` 拆开讲。
- 更偏好 Python 的读者可以在学完 u1-l4 后跳到 **u1-l5（第一个 Python 量子内核）**。
- 对 `nvq++` 内部到底跑了哪四步编译流水线感兴趣，可以先记下疑问，等学到 **u4-l5（nvq++ 驱动脚本）** 时再回来对照本讲的 `nvq++ grover.cpp` 体验。
- 如果你想在构建侧做更多实验（比如开 sanitizer、关 warnings-as-errors），建议结合 [scripts/build_cudaq.sh](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/scripts/build_cudaq.sh) 顶部注释，把每个开关都试一遍并观察 `build/logs/` 的变化。
