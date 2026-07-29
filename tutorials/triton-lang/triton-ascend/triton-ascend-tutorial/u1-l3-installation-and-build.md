# 环境准备、安装与构建

## 1. 本讲目标

本讲是「从零跑起来」的关键一步。读完本讲，你应当能够：

1. 说清楚 Triton-Ascend 为什么依赖 **CANN 工具链**，它在编译期和运行期分别扮演什么角色。
2. 根据 usage 场景，在 **whl 安装 / 源码编译 / Docker 镜像** 三种方式中做出正确选择。
3. 看懂 `setup.py` 这个构建入口：它如何驱动 CMake 构建 C++/MLIR 扩展，又如何把 `ascend` 后端「挂载」到 `triton` 命名空间。
4. 理解 `CMakeLists.txt` 与 **LLVM** 构建链路的关系，知道 LLVM 是从哪里来的（预编译下载还是源码自建）。
5. 独立完成一次安装，并能记录、解释过程中遇到的关键环境变量（尤其是 CANN 的 `set_env.sh`）。

> 本讲承接 [u1-l1](u1-l1-project-overview-and-architecture.md)（已建立「compiler / driver / 语言扩展」三大组件与 `TTIR → Linalg → AscendNPU IR → .o` 主链路的地图），把那张地图「落到地面」——告诉你这些东西是怎么装进机器里的。

## 2. 前置知识

- **Triton**：一种用 Python 写 GPU/NPU kernel 的语言 + 编译器。社区版 Triton 默认面向 NVIDIA/AMD GPU，Triton-Ascend 是它针对华为昇腾 NPU 的后端实现（见 [u1-l1](u1-l1-project-overview-and-architecture.md)）。
- **NPU（神经网络处理单元）**：昇腾系列加速卡（如 Atlas A2/A3/950）。和 GPU 一样需要「驱动 + 运行时库 + 编译器」三层软件栈才能用。
- **CANN（Compute Architecture for Neural Networks）**：华为为昇腾 NPU 提供的整套软件栈，包含驱动接口、运行时库（ACL）、以及把中间表示编译成 NPU 二进制的 **BiSheng 编译器**。你可以把它类比为「NPU 版的 CUDA Toolkit」。
- **torch_npu**：让 PyTorch 能把张量放到 `npu:0` 设备上、并调用 CANN 运行时的适配库。它之于 NPU，就像 CUDA 之于 GPU 上的 `torch.cuda`。
- **setuptools / pip / CMake / Ninja / LLVM**：Python 打包与 C++ 构建的常规工具链。本讲会用到，但不要求精通。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `README.md` | 项目门面，含「快速安装 / 源码安装 / Docker」三段速查 | 版本要求与三种安装命令 |
| `docs/en/quick_start.md` | 快速开始，以 vector-add 为例验证环境 | 软件依赖版本、`set_env.sh`、运行示例 |
| `docs/en/installation_guide.md` | 最完整的安装手册 | 版本矩阵表、源码/镜像安装细节、FAQ |
| `setup.py` | **源码安装的构建入口**（`pip install -e .` 触发） | 默认构建环境变量、后端发现与挂载、驱动 CMake、LLVM 下载 |
| `CMakeLists.txt` | 顶层 CMake 构建脚本 | MLIR/LLVM 依赖、ascend 后端子目录、产物清单 |
| `python/build_helpers.py` | 计算 CMake 构建目录 | `TRITON_BUILD_DIR` 覆盖构建路径 |
| `docker/Dockerfile` | 镜像构建脚本 | CANN 基础镜像、构建依赖安装 |
| `version.txt` / `cmake/llvm-hash.txt` / `third_party/ascend/patch/llvm_patch_*.patch` | 版本与 LLVM 补丁 | 确定要拉的 LLVM 版本与补丁 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 三种安装方式的定位与选择**（建立全局选择直觉）
- **4.2 CANN 工具链与软件依赖**（最小模块：CANN 工具链）
- **4.3 setup.py：构建入口与后端打包机制**（最小模块：setup.py 构建入口）
- **4.4 CMakeLists.txt 与 LLVM 构建链路**（最小模块：CMakeLists.txt）

---

### 4.1 三种安装方式的定位与选择

#### 4.1.1 概念说明

Triton-Ascend 提供三种安装方式，**它们的产物几乎一样**（都是 `triton` 这个 Python 包 + 一堆 `.so`/可执行工具），区别在于「谁来准备 CANN、谁来编译 C++」：

1. **whl 包安装**：直接 `pip install triton-ascend==...`。官方已经把 C++/MLIR 编译好的二进制打进 wheel，你只负责准备好 CANN 与 torch_npu。适合生产环境、快速上手。
2. **源码编译安装**：`git clone` 后 `pip install -e .`，在本机现场编译 C++/MLIR 扩展。适合二次开发、改源码、用最新 main 分支特性。
3. **镜像安装**：用官方 Dockerfile / 预制镜像，CANN 已经装好，开箱即用。适合快速体验、容器化部署、多机一致环境。

> 重要事实：Triton-Ascend **不是新语言**，它和社区 Triton 共用同一个安装目录名 `triton`。3.2.1 起官方把社区 Triton 声明为依赖，安装时会先装社区 Triton、再用 Triton-Ascend 覆盖同名目录，以缓解「后装的 triton 把 ascend 覆盖掉」的问题（详见 `docs/en/installation_guide.md` 的 FAQ）。

#### 4.1.2 核心流程

选择决策可以这样走：

```text
是否要改 C++/MLIR 源码？
├─ 是 ──→ 源码编译安装（pip install -e .）
└─ 否 ──→ 是否需要容器化/多机一致？
          ├─ 是 ──→ 镜像安装（Docker）
          └─ 否 ──→ whl 包安装（pip install triton-ascend==x.y.z）
```

无论哪条路，**第一步都是先装好 NPU 驱动 + CANN + Python + torch_npu**，这是绕不开的前置依赖（见 4.2）。

#### 4.1.3 源码精读

三种方式的命令分别出自以下位置：

- whl 包安装命令（README 的 Quick Installation）：

[README.md:77-80](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/README.md#L77-L80) —— 以 `pip install triton-ascend==3.2.1 --extra-index-url=...` 为例，`--extra-index-url` 指向华为云镜像源拉取昇腾相关的 wheel。

- 源码安装命令（README 的 Source Installation）：

[README.md:101-105](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/README.md#L101-L105) —— `git clone ... && cd triton-ascend` 后 `git checkout main`，再 `pip install -e .`（`-e` 是 editable/开发模式，改完 Python 代码即时生效，C++ 改动需重新编译）。

- 镜像安装（README 的 Docker Image Usage）：

[README.md:164-169](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/README.md#L164-L169) —— `docker build --build-arg CANN_BASE_IMAGE=... -f ./docker/Dockerfile .`，通过 `--build-arg CANN_BASE_IMAGE` 选择与你的芯片匹配的 CANN 基础镜像。

镜像为什么快？因为 Dockerfile 直接以「已经装好 CANN」的 `quay.io/ascend/cann` 镜像为基底，省掉了最耗时的 CANN 安装：

[docker/Dockerfile:1-2](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docker/Dockerfile#L1-L2) —— `ARG CANN_BASE_IMAGE=quay.io/ascend/cann:8.5.0-a3-ubuntu22.04-py3.10`，`FROM ${CANN_BASE_IMAGE}`。

随后 Dockerfile 安装编译所需系统库（clang-15、lld-15、cmake、ninja、ccache、zlib1g-dev 等）并安装 Python 依赖：

[docker/Dockerfile:13-36](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docker/Dockerfile#L13-L36) —— `apt install ... clang-15 lld-15 cmake ccache ninja-build zlib1g-dev ...`，并通过 `update-alternatives` 把 `clang/clang++/lld` 指向 `-15` 版本。

[docker/Dockerfile:41-45](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docker/Dockerfile#L41-L45) —— `pip install -r requirements_dev.txt` 与 `pip install -r requirements.txt` 安装运行期与构建期 Python 依赖。

> 注意：Dockerfile 里 `WORKDIR /home/triton` 之后并没有直接 `pip install -e .`，它只把「环境」备好；真正的 Triton-Ascend 源码是在容器启动后由你 `git clone` 进来再装的（见 installation_guide 的镜像使用步骤）。

#### 4.1.4 代码实践

**实践目标**：根据场景选对安装方式，建立「为什么这么选」的判断。

**操作步骤**：

1. 阅读 `docs/en/installation_guide.md` 的「Installation Method Selection」对比表（[installation_guide.md:115-141](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/installation_guide.md#L115-L141)）。
2. 为下面三个场景各选一种方式并写出一句理由：
   - (a) 你要在 Atlas 800T A2 上跑别人的 Triton kernel，不改源码；
   - (b) 你想给 Triton-Ascend 新增一个 MLIR pass；
   - (c) 你的公司要求所有服务跑在 Kubernetes 容器里。

**预期结果**：(a) whl 包安装；(b) 源码编译安装；(c) 镜像安装。理由应分别对应「稳定快速」「可改源码」「容器隔离/一致」。

#### 4.1.5 小练习与答案

- **Q1**：源码安装命令里 `-e`（editable）的作用是什么？
  - **答**：以「开发模式」安装，Python 端的修改即时生效而无需重装；但 C++/MLIR（如 `setup.py` 驱动 CMake 编译出的 `.so`）改动后仍需重新执行编译。
- **Q2**：为什么 Docker 构建要传 `CANN_BASE_IMAGE` 参数？
  - **答**：不同芯片（910b/A3/950）需要不同 CANN 版本与内核驱动匹配；用 `--build-arg CANN_BASE_IMAGE=<tag>` 选择匹配的基础镜像，避免在镜像里重新装 CANN。

---

### 4.2 CANN 工具链与软件依赖

#### 4.2.1 概念说明

CANN 是 Triton-Ascend 一切行为的「地基」。它在两个阶段都不可或缺：

- **编译期**：Triton-Ascend 把 TTIR 一路降到 AscendNPU IR，随后由 CANN 里的 **BiSheng 编译器**把它编译成 NPU 可执行的 `triton_xxx_kernel.o`（这正是 [u1-l1](u1-l1-project-overview-and-architecture.md) 主链路最右端的那一步）。
- **运行期**：driver（见 [u1-l1](u1-l1-project-overview-and-architecture.md) 的 driver 组件）通过 CANN 的运行时 API（如 `rtKernelLaunch`）在 stream 上启动内核、管理 workspace 与显存。

CANN 装好后，必须 `source <安装路径>/ascend-toolkit/set_env.sh` 来导出环境变量（典型包含 `LD_LIBRARY_PATH`、`PATH`、`ASCEND_HOME_PATH` 等，具体以本地 `source` 后的 `env` 为准），否则 Triton-Ascend 找不到 BiSheng 编译器和运行时库。

除 CANN 外，软件依赖还包含：

- **Python**：构建入口 `setup.py` 在当前 HEAD 上声明的支持区间。
- **PyTorch / torch_npu**：torch_npu 是 PyTorch 与 NPU 的桥梁，提供 `device='npu'`、`torch.npu.*` 等接口（见 [u1-l4](u1-l4-first-kernel-vector-add.md) 与迁移相关讲义）。

#### 4.2.2 核心流程

环境准备的顺序（**whl 安装与源码安装都需要先做**）：

```text
1. 系统层：安装 NPU 驱动（厂商提供，通常已预装在 Atlas 整机上）
2. CANN 层：安装 CANN toolkit（推荐 9.0.0），并 source set_env.sh
3. Python 层：安装合适版本的 Python，再装 torch + torch_npu
4. Triton-Ascend 层：whl 安装 或 源码安装
5. 验证：npu-smi info 看芯片型号；跑 vector-add 看精度
```

版本匹配关系（以官方 3.2.1 版本矩阵为准）：CANN 推荐 9.0.0，torch_npu 推荐 2.7.1.post4，Python 在文档里写的是 3.9–3.11，但**构建入口 `setup.py` 才是源码安装的权威**（见 4.3，当前 HEAD 声明 `>=3.10,<3.15`）。文档与 `setup.py` 之间存在滞后，以 `setup.py` 为准。

#### 4.2.3 源码精读

软件依赖版本声明（README 的 Environment Preparation）：

[README.md:61-69](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/README.md#L61-L69) —— 明确「Python py3.9-py3.11 / CANN 推荐 9.0.0 / TorchNPU 当前为 2.7.1.post4」。注意这是 README 的口径，偏保守。

最权威的版本矩阵在安装手册里（含 Python 3.9–3.13、CANN 9.0.0、torch_npu 多版本）：

[docs/en/installation_guide.md:168-190](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/installation_guide.md#L168-L190) —— 3.2.1 行：支持 Python 3.9–3.13、CANN 9.0.0、torch_npu 2.7.1.post4 / 2.8.0.post4 / 2.9.0.post2 / 2.10.0；备注「Python3.9.x 不支持 aarch64」。

CANN 环境变量的加载与示例运行（quick_start）：

[docs/en/quick_start.md:45-52](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/quick_start.md#L45-L52) —— `source /usr/local/Ascend/ascend-toolkit/set_env.sh` 后运行 `python3 ./third_party/ascend/tutorials/01-vector-add.py`，看到「The maximum difference between torch and triton is 0.0」即环境正确。

如何确认芯片型号（FAQ 里给的 `npu-smi info` 示例）：

[docs/en/installation_guide.md:577-596](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/installation_guide.md#L577-L596) —— 例如输出里 `910B4` 对应 A2（Ascend 910b 系列），据此选择对应的 CANN 镜像/版本。

#### 4.2.4 代码实践

**实践目标**：亲手加载 CANN 环境变量并观察它的「足迹」，理解为什么 `set_env.sh` 不可省略。

**操作步骤**：

1. 在已装好 CANN 的机器上执行：

   ```bash
   env | grep -iE 'ASCEND|LD_LIBRARY_PATH|PATH' | sort > /tmp/before.txt
   source /usr/local/Ascend/ascend-toolkit/set_env.sh
   env | grep -iE 'ASCEND|LD_LIBRARY_PATH|PATH' | sort > /tmp/after.txt
   diff /tmp/before.txt /tmp/after.txt
   ```

2. 执行 `npu-smi info`，记下芯片型号（如 910B4 / A3 / 950）。

**需要观察的现象**：diff 会列出 `set_env.sh` 新增/修改的环境变量（典型会看到 `ASCEND_HOME_PATH`、`LD_LIBRARY_PATH` 里追加的 `lib64` 路径、`PATH` 里的 `compiler/ccec_compiler` 等 BiSheng 相关路径）。

**预期结果**：能解释「如果跳过 `source set_env.sh`，Triton-Ascend 在编译/运行时会因为找不到 BiSheng 与运行时库而报错」。

> 如果当前环境没有 NPU/CANN，这一步标注为「待本地验证」——你可以在任意 Linux 上先跑 `diff` 部分理解脚本逻辑，但真实环境变量需在装好 CANN 的机器上观察。

#### 4.2.5 小练习与答案

- **Q1**：CANN 在「编译期」和「运行期」分别提供什么？
  - **答**：编译期提供 BiSheng 编译器（把 AscendNPU IR 编译成 `.o`）；运行期提供 ACL/runtime 库（如 `rtKernelLaunch`，负责在 stream 上启动内核、管理显存/workspace）。
- **Q2**：README 写 Python 支持 3.9–3.11，但你想用 Python 3.12 做源码安装，该信谁？
  - **答**：源码安装以 `setup.py` 的 `python_requires` 为权威（当前 HEAD 为 `>=3.10,<3.15`，3.12 在支持范围内）；README 的措辞相对滞后。安装手册的 3.2.1 矩阵也列出支持 3.12/3.13，可相互印证。
- **Q3**：为什么 `source set_env.sh` 之后，还要单独装 torch_npu？
  - **答**：CANN 是底层 NPU 软件栈；torch_npu 是 PyTorch 与 CANN 之间的适配层，提供 `device='npu'`、`torch.npu.*` 等高层接口，二者职责不同、缺一不可。

---

### 4.3 setup.py：构建入口与后端打包机制

#### 4.3.1 概念说明

`setup.py` 是**源码安装的核心入口**。当你执行 `pip install -e .`，setuptools 会调用它。它干两件事：

1. **驱动 CMake 构建 C++/MLIR 扩展**：通过自定义的 `CMakeBuild`（继承 `build_ext`）调用 `cmake` + `ninja`，编译出 `libtriton`、`triton-mlir-opt`、`triton-opt`、`FileCheck`、`entryC` 等产物。
2. **打包 Python 包并把 ascend 后端「挂载」到 triton 命名空间**：把 `third_party/ascend/language` 装到 `triton.language.extra.*`、把 `third_party/ascend/backend` 装到 `triton.backends.ascend`，并通过 entry points 让 core 在运行时自动发现 ascend 后端。

> 这一步对应 [u1-l2](u1-l2-directory-structure-and-layering.md) 讲过的「分层由安装期机制强制」——`setup.py` 就是那个「安装期机制」的具体实现。读不懂它，就理解不了「为什么 `import triton.language.extra.cann` 能用」。

#### 4.3.2 核心流程

`pip install -e .` 触发后的关键步骤：

```text
A. 加载 setup.py
   └─ 顶部 os.environ.setdefault(...) 写入默认构建环境变量
   └─ backends = BackendInstaller.copy(["ascend","nvidia","amd"])  # 发现三个内置后端
B. 自定义命令钩子
   ├─ plugin_editable_wheel.run → add_links(external_only=False)
   │     └─ 为每个后端创建符号链接：backend→python/triton/backends/<name>,
   │        language→python/triton/language/extra/<x>
   └─ build_ext (CMakeBuild).run → 下载依赖 → cmake configure → cmake build → 拷贝工具
C. setup() 注册
   ├─ packages / package_dir：把 python/ 与各后端目录映射进 wheel
   ├─ entry_points["triton.backends"]：注册 ascend/nvidia/amd，供 core 自动发现
   └─ install_requires：声明运行期依赖（numpy/scipy/...）+ 架构相关的 triton==<版本>
D. 安装完成后，import triton 时 core 按 entry_points 加载 ascend 后端
```

#### 4.3.3 源码精读

**(1) 默认构建环境变量**——这是源码安装「开箱默认行为」的源头：

[setup.py:52-56](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L52-L56) —— 用 `setdefault` 设定：`TRITON_BUILD_WITH_CCACHE=true`（启用 ccache 加速重编）、`TRITON_BUILD_WITH_CLANG_LLD=true`（用 clang/lld 而非 gcc）、`TRITON_BUILD_PROTON=OFF`（不构建 proton profiler）、`TRITON_WHEEL_NAME="triton-ascend"`（包名）、`TRITON_APPEND_CMAKE_ARGS="-DTRITON_BUILD_UT=OFF"`（默认不编 C++ 单测以省时）。这些就是 README 里源码安装命令所设的那些变量，`setup.py` 已替你设好默认值。

**(2) 后端发现**——ascend 只是三个内置后端之一：

[setup.py:764](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L764) —— `backends = [*BackendInstaller.copy(["ascend", "nvidia", "amd"]), *BackendInstaller.copy_externals()]`，把 ascend/nvidia/amd 都当成 in-tree 后端处理。

[setup.py:78-113](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L78-L113) —— `BackendInstaller.prepare` 探测每个后端的 `backend/language/tools` 子目录，**断言 `backend` 下必须存在 `compiler.py` 和 `driver.py`**（这正是 [u1-l1](u1-l1-project-overview-and-architecture.md) 讲的 compiler/driver 两大组件），并算出安装目标 `install_dir = python/triton/backends/<name>`。

**(3) 打包映射**——把 ascend 的 language 挂到 `triton.language.extra`：

[setup.py:767-791](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L767-L791) —— `get_package_dirs` 先 `yield ("", "python")`，再对每个后端 `yield (f"triton.language.extra.{x}", ...)`，从而把 `third_party/ascend/language/cann` 映射成 `triton.language.extra.cann`（即 `tl` 之外的 cann 扩展，见 [u1-l1](u1-l1-project-overview-and-architecture.md)）。

**(4) editable 模式的符号链接**——开发模式下用软链而非拷贝，改源码即时生效：

[setup.py:816-841](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L816-L841) —— `add_link_to_backends` 对每个后端调用 `update_symlink(backend.install_dir, backend.backend_dir)`，把 ascend 的 `backend`/`language` 软链到 `python/triton/...` 下。

[setup.py:849-852](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L849-L852) —— `add_links(external_only=False)` 是 `plugin_develop` / `plugin_editable_wheel` 的统一入口；而 `plugin_bdist_wheel` / `plugin_install` 走 `external_only=True`（正式安装只链接外部插件，内置后端走 package 数据）。

**(5) entry points——core 自动发现后端的钥匙**：

[setup.py:927-935](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L927-L935) —— `get_entry_points` 生成 `entry_points["triton.backends"] = ["ascend = triton.backends.ascend", ...]`。core 在 `import triton` 时按这个入口点找到 ascend 后端并加载其 `compiler.py`。

**(6) 版本与依赖**：

[setup.py:976-982](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L976-L982) —— `TRITON_VERSION = "3.6.0" + ...`（与 `version.txt` 的 `3.6.0` 对应），`MIN_PYTHON=(3,10)`、`MAX_PYTHON=(3,14)`，故 `PYTHON_REQUIRES=">=3.10,<3.15"`，这是源码安装 Python 版本的权威。

[setup.py:1014-1033](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L1014-L1033) —— `get_package_name` 返回 `triton_ascend`（或 `TRITON_WHEEL_NAME`）；`ARCHITECTURE_DEPENDENCIES` 对 x86/arm 都依赖 `triton==3.6.0`，即「先装社区 Triton 再被 ascend 覆盖」机制里锁定的社区 Triton 版本。

[setup.py:1044-1059](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L1044-L1059) —— `get_install_requirements` 列出 `attrs/numpy/scipy/decorator/psutil/pytest/pyyaml/pybind11/pandas` 等运行期依赖。

[setup.py:1069-1099](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L1069-L1099) —— 最终 `setup(...)` 调用：`ext_modules=[CMakeExtension("triton", "triton/_C/")]` 把 CMake 构建挂进来，`cmdclass` 把各命令替换成自定义的 `CMakeBuild/BuildWheel/plugin_develop/...`。

> 一个易错点（来自 [u1-l2](u1-l2-directory-structure-and-layering.md)）：`third_party/ascend/include` 与顶层 `include` 同名但完全不同；`setup.py` 这里处理的是 Python 侧的 `backends/language` 映射，C++ 侧的 `include/lib` 是由 CMake 负责的（见 4.4）。

#### 4.3.4 代码实践

**实践目标**：不动手装，仅靠读 `setup.py` 预测「editable 安装后，ascend 的 language 会出现在哪个 Python 路径」，再在有环境时验证。

**操作步骤**：

1. 在仓库根目录，仅用源码追踪符号链接目标：

   ```bash
   # 只读分析：找出 setup.py 会为 ascend 创建的软链目标
   python3 - <<'PY'
   # 示意代码：模拟 get_package_dirs 对 ascend 的映射
   backend_name = "ascend"
   # language 下的每个子目录（如 cann）会映射到这里：
   print("language 映射目标前缀:", f"triton.language.extra.<x>")
   print("backend  映射目标    :", f"python/triton/backends/{backend_name}")
   PY
   ```

   上述为**示例代码**，仅演示映射逻辑；真实路径由 `setup.py:767-791` 与 `setup.py:816-841` 决定。

2. （本地有环境时验证）完成 `pip install -e .` 后执行：

   ```bash
   python3 -c "import triton.language.extra.cann as c; print(c.__file__)"
   python3 -c "import importlib.metadata as m; print(m.get_entry_points('triton.backends'))"
   ```

**需要观察的现象**：第 2 步应显示 `triton.language.extra.cann` 指向 `third_party/ascend/language/cann` 的软链；entry_points 列出 `ascend = triton.backends.ascend`。

**预期结果**：能说清「`import triton.language.extra.cann` 之所以可用，是因为 `setup.py` 在安装期建立了这条映射/软链」。若无 NPU 环境，第 2 步标注「待本地验证」。

#### 4.3.5 小练习与答案

- **Q1**：`pip install -e .` 与 `pip install .`（非 editable）对 ascend 后端代码的处理有何不同？
  - **答**：editable（`-e`）走 `plugin_editable_wheel`/`plugin_develop`，用**符号链接**把 ascend 的 backend/language 链到 `python/triton/...`，改源码即时生效；非 editable 走 `plugin_install`/`BuildWheel`，内置后端以**包数据**形式拷贝进 wheel，改动需重装。
- **Q2**：为什么 `setup.py:107-108` 要断言 backend 下存在 `compiler.py` 和 `driver.py`？
  - **答**：这是 Triton 后端插件的契约——core 加载后端时需要这两个文件（compiler 提供编译阶段，driver 提供运行时启动），缺失则后端不完整，故在打包期就断言拦截。
- **Q3**：默认 `TRITON_BUILD_PROTON=OFF`、`-DTRITON_BUILD_UT=OFF` 带来的好处是什么？
  - **答**：跳过 proton profiler 与 C++ 单测的构建，显著缩短首次编译时间；需要时可通过环境变量重新打开。

---

### 4.4 CMakeLists.txt 与 LLVM 构建链路

#### 4.4.1 概念说明

`CMakeLists.txt` 才是「真正编译 C++/MLIR」的脚本，由 `setup.py` 的 `CMakeBuild` 调用。它的核心职责：

1. 找到 **MLIR/LLVM** 依赖（Triton 的 IR 基础设施基于 MLIR）。
2. 编译 **ascend 后端插件**（`third_party/ascend` 下的 C++/MLIR pass、`triton-mlir-opt` 工具等）。
3. 链接产出 `libtriton`（Python 扩展 `triton._C`）、`entryC`、以及运行时用到的 `triton-mlir-opt` / `triton-opt` / `FileCheck`。

**LLVM 从哪来？** 这是新手最容易卡住的地方。Triton-Ascend 用 LLVM 22 系列。它有两种来源：

- **预编译下载（默认）**：`setup.py` 根据当前平台和 `cmake/llvm-hash.txt` + 补丁哈希，拼出一个预编译 LLVM 包名，从华为云 OBS 下载到 `~/.triton/llvm`。
- **源码自建（离线/定制）**：设置 `LLVM_SYSPATH` 指向本地已装好的 LLVM，跳过下载。源码安装文档里的「Offline Installation」就是这条路。

#### 4.4.2 核心流程

CMake 配置阶段的关键决策链：

```text
setup.py: CMakeBuild.build_extension
  ├─ get_thirdparty_packages([get_llvm_package_info()])
  │     ├─ 若设了 LLVM_SYSPATH        → 用本地 LLVM（离线）
  │     ├─ 若 TRITON_OFFLINE_BUILD=true → 强制 syspath，禁止下载
  │     └─ 否则                        → 按 llvm-hash + patch_hash + OS 拼名 → 下载预编译包
  ├─ cmake -G Ninja <cmake_args>
  │     ├─ -DTRITON_BUILD_PYTHON_MODULE=ON
  │     ├─ -DTRITON_CODEGEN_BACKENDS="ascend;nvidia;amd"   # 决定 add_subdirectory 哪些后端
  │     ├─ -DLLVM_MAJOR_VERSION_22_COMPATIBLE=ON
  │     └─ -DLLVM_ENABLE_WERROR=ON ...
  └─ cmake --build . -j<N>
        → libtriton.so / entryC.so / triton-mlir-opt / triton-opt / FileCheck
```

预编译 LLVM 包名的组成（理解它就理解了「为什么换 patch 会触发重新下载」）：

\[ \texttt{name} = \texttt{llvm-}\,\underbrace{\texttt{f6ded0b}}_{\text{llvm-hash.txt 前 8 位}}\texttt{-}\,\underbrace{\texttt{xxxxxxxx}}_{\text{patch 哈希前 8 位}}\texttt{-}\,\underbrace{\texttt{ubuntu-x64}}_{\text{系统后缀}} \]

其中 patch 哈希由 `get_llvm_patch_hash` 对 `third_party/ascend/patch/llvm_patch_*.patch` 求 SHA256 得到；一旦 patch 内容变化，哈希变化，包名变化，就会触发重新下载——这套机制保证了「下载到的 LLVM 一定带着正确的 Ascend 补丁」。

#### 4.4.3 源码精读

**(1) CMake 最低版本与构建选项**：

[CMakeLists.txt:1](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/CMakeLists.txt#L1) —— `cmake_minimum_required(VERSION 3.20)`，与 `setup.py` 里 `CMake >= 3.20 is required` 的检查一致（[setup.py:504-505](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L504-L505)）。

[CMakeLists.txt:22-26](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/CMakeLists.txt#L22-L26) —— 关键 option：`TRITON_BUILD_PYTHON_MODULE`（构建 Python 绑定）、`TRITON_BUILD_PROTON`、`TRITON_BUILD_UT`、`TRITON_BUILD_WITH_CCACHE`，以及 `TRITON_CODEGEN_BACKENDS`（决定编译哪些后端，默认空、由 `setup.py` 传入 `ascend;nvidia;amd`）。

**(2) 查找 MLIR/LLVM 并按平台追加 codegen 库**：

[CMakeLists.txt:102](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/CMakeLists.txt#L102) —— `find_package(MLIR REQUIRED CONFIG PATHS ${MLIR_DIR})`，MLIR 目录由 `LLVM_LIBRARY_DIR/cmake/mlir` 推得（[CMakeLists.txt:93-99](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/CMakeLists.txt#L93-L99)）。

[CMakeLists.txt:287-306](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/CMakeLists.txt#L287-L306) —— 根据系统架构（aarch64 / x86_64 / ppc64le）追加对应的 LLVM codegen/ASM 解析库；昇腾整机常见为 aarch64 或 x86_64。

**(3) 编译 ascend 后端——`add_subdirectory` 的关键循环**：

[CMakeLists.txt:204-221](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/CMakeLists.txt#L204-L221) —— 当 `TRITON_BUILD_PYTHON_MODULE` 打开时，对 `TRITON_CODEGEN_BACKENDS` 里每个后端 `add_subdirectory(third_party/${CODEGEN_BACKEND})`。也就是说 ascend 后端的 C++/MLIR pass（`third_party/ascend/lib/...`）正是在这里被纳入编译的（后续 [u4](u4-l1-ttir-to-linalg-pipeline-overview.md) 系列会深入这些 pass）。

**(4) LLVM 版本兼容性开关**：

[CMakeLists.txt:186-199](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/CMakeLists.txt#L186-L199) —— 定义 `LLVM_MAJOR_VERSION_21_COMPATIBLE` / `LLVM_MAJOR_VERSION_22_COMPATIBLE` 两个 option，用于让 AscendNPU IR 适配不同 LLVM 大版本；`setup.py` 默认传 `-DLLVM_MAJOR_VERSION_22_COMPATIBLE=ON`（[setup.py:569](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L569)）。

**(5) LLVM_SYSPATH 与 FileCheck**：

[CMakeLists.txt:341-352](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/CMakeLists.txt#L341-L352) —— 断言 `LLVM_SYSPATH` 必须设置（由 `setup.py` 的 `get_thirdparty_packages` 注入，指向下载或本地的 LLVM），并把 `FileCheck` 从 LLVM 拷贝到 wheel 目录（FileCheck 是后续 MLIR conversion 测试要用的工具，见 [u10-l4](u10-l4-writing-tests.md)）。

**(6) LLVM 预编译包下载逻辑（在 setup.py 中）**：

[setup.py:225-273](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L225-L273) —— `get_llvm_package_info`：读取 `cmake/llvm-hash.txt` 前 8 位（`f6ded0b`）作 `rev`，调 `get_llvm_patch_hash` 得 `patch_hash`，按平台选 `system_suffix`，拼出 `llvm-{rev}-{patch_hash}-{system_suffix}`，URL 指向 `https://triton-ascend-artifacts.obs.myhuaweicloud.com/llvm-builds/...`。

[setup.py:206-222](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L206-L222) —— `get_llvm_patch_hash`：对 `third_party/ascend/patch/llvm_patch_*.patch`（当前为 `llvm_patch_f6ded0b.patch`）求 SHA256，取前 8 位。

**(7) CMake 构建与产物拷贝（在 setup.py 的 CMakeBuild 中）**：

[setup.py:636-638](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L636-L638) —— `cmake` 配置与 `cmake --build` 的实际调用。

[setup.py:641-657](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L641-L657) —— 把构建出的 `triton-mlir-opt`（位于 `cmake_dir/third_party/ascend/bin/`）拷贝到扩展目录并 `strip` 减小体积——这个工具正是 [u4](u4-l1-ttir-to-linalg-pipeline-overview.md) 里手动跑 pass 流水线要用的 `triton-mlir-opt`。

> 离线自建 LLVM 的命令在安装手册里（[docs/en/installation_guide.md:297-369](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/installation_guide.md#L297-L369)）：`git checkout f6ded0be...` 后 `git apply llvm_patch_f6ded0b.patch`，再用 clang-15/lld-15 编 LLVM，最后 `LLVM_SYSPATH=... python3 setup.py install`。注意它 checkout 的 LLVM commit 与 `cmake/llvm-hash.txt` 一致（`f6ded0be897e2878612dd903f7e8bb85448269e5`）。

#### 4.4.4 代码实践

**实践目标**：在不下载、不编译的前提下，亲手「算」出当前 HEAD 在 x86_64 Ubuntu 上会去下载的预编译 LLVM 包名，理解版本与补丁如何参与命名。

**操作步骤**：

1. 读取两个输入：

   ```bash
   cat cmake/llvm-hash.txt        # → f6ded0be897e2878612dd903f7e8bb85448269e5，取前 8 位 = f6ded0b
   ls third_party/ascend/patch/   # → llvm_patch_f6ded0b.patch
   ```

2. 计算 patch 哈希前 8 位（与 `setup.py:206-222` 的逻辑一致）：

   ```bash
   python3 - <<'PY'
   import hashlib, os, glob
   patch_dir = "third_party/ascend/patch"
   files = sorted(f for f in os.listdir(patch_dir)
                  if f.startswith("llvm_patch_") and f.endswith(".patch"))
   h = hashlib.sha256()
   for pf in files:
       h.update(open(os.path.join(patch_dir, pf), "rb").read())
   print("patch_hash[:8] =", h.hexdigest()[:8])
   PY
   ```

3. 按 `setup.py:265-273` 的拼接规则，x86_64 + 较新 glibc 的系统后缀为 `ubuntu-x64`，于是包名为：

   ```text
   llvm-f6ded0b-<patch_hash>-ubuntu-x64
   ```

4. 在仓库源码里反查这个逻辑：

   ```bash
   python3 -c "import setup_helper_llvm as _" 2>/dev/null || true   # 仅提示：真实逻辑在 setup.py:get_llvm_package_info
   ```

**需要观察的现象**：第 2 步打印出一个 8 位哈希；第 3 步得到完整包名；包名里同时包含 `rev` 和 `patch_hash`。

**预期结果**：能解释「如果我修改了 `llvm_patch_*.patch`，`patch_hash` 改变，`setup.py` 会判定本地缓存失效并重新下载对应 LLVM」——这正是 `setup.py:320-324` 用 `version.txt == p.url` 做兼容性检查的意义。

> 若想真实触发下载，执行 `pip install -e .` 并观察日志里的 `downloading and extracting https://triton-ascend-artifacts.obs.myhuaweicloud.com/llvm-builds/...`（需联网，标注「待本地验证」）。

#### 4.4.5 小练习与答案

- **Q1**：`CMakeLists.txt` 中 `TRITON_CODEGEN_BACKENDS` 为空时会发生什么？
  - **答**：`add_subdirectory(third_party/${CODEGEN_BACKEND})` 循环不执行，不会编译任何后端的 C++/MLIR。`setup.py` 会把它填成 `ascend;nvidia;amd`，所以正常构建会编译全部三个后端。
- **Q2**：为什么 `CMakeLists.txt:341` 断言 `LLVM_SYSPATH` 必须设置？它由谁注入？
  - **答**：MLIR/LLVM 的 include/lib 路径必须显式提供，不能凭空找到。`LLVM_SYSPATH` 由 `setup.py` 的 `get_thirdparty_packages`（基于 `get_llvm_package_info`）注入——要么指向下载到 `~/.triton/llvm` 的预编译包，要么指向用户 `LLVM_SYSPATH` 环境变量指定的本地 LLVM。
- **Q3**：离线构建（`TRITON_OFFLINE_BUILD=true`）与设置 `LLVM_SYSPATH` 有何区别？
  - **答**：`LLVM_SYSPATH` 单纯指定本地 LLVM 路径，但仍允许其他依赖联网；`TRITON_OFFLINE_BUILD=true` 是更强的开关，会禁止任何联网下载（包括 LLVM 与其他第三方包），强制所有依赖都通过各自 `syspath_var_name` 显式提供，适合无网沙箱（见 [setup.py:162-175](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L162-L175)）。

---

## 5. 综合实践

**任务**：在一个真实（或容器内的）昇腾环境上，完整走一遍「环境准备 → 安装 → 验证」流程，并产出一份《环境与安装记录》。

**要求完成的事**：

1. **选型**：根据你是否要改 C++ 源码，选择 whl 或源码安装，写出选择理由（参考 4.1）。
2. **依赖准备**：
   - `source <CANN>/ascend-toolkit/set_env.sh`，用 4.2.4 的 diff 方法记录 `set_env.sh` 导出的关键变量。
   - `npu-smi info` 记录芯片型号。
3. **安装**：
   - whl：`pip install triton-ascend==3.2.1 --extra-index-url=https://mirrors.huaweicloud.com/ascend/repos/pypi`
   - 或源码：`git clone ... && cd triton-ascend && pip install -e .`，并记录首次编译耗时与是否下载了预编译 LLVM。
4. **验证**：运行 `python3 ./third_party/ascend/tutorials/01-vector-add.py`，确认输出 `The maximum difference between torch and triton is 0.0`（命令见 [docs/en/quick_start.md:45-52](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/quick_start.md#L45-L52)）。
5. **溯源**（源码安装者额外做）：用 4.3.4 的方法确认 `triton.language.extra.cann` 的软链目标，用 4.4.4 的方法算出本次下载的 LLVM 包名。

**交付物**：一份 markdown 记录，包含选型理由、芯片型号、`set_env.sh` 影响的环境变量清单、安装耗时、vector-add 验证截图/输出、（可选）LLVM 包名。

> 如果手头没有 NPU：可退化为「源码阅读型实践」——只完成第 5 步的溯源分析，并对照本讲给出的源码链接，说明每一步在 `setup.py`/`CMakeLists.txt` 中的依据。真机运行部分标注「待本地验证」。

## 6. 本讲小结

- Triton-Ascend 有 **whl / 源码 / 镜像** 三种安装方式，产物相同，区别在于「谁来准备 CANN、谁来现场编译 C++」。
- **CANN 是地基**：编译期提供 BiSheng（生成 `.o`），运行期提供 ACL/runtime（`rtKernelLaunch`）；`source set_env.sh` 不可省略，否则找不到编译器与运行时库。
- 软件版本以 **`setup.py` 为源码安装权威**（当前 HEAD：Python `>=3.10,<3.15`，依赖 `triton==3.6.0`，与 `version.txt` 一致），README/quick_start 的措辞相对滞后。
- **`setup.py` 是源码安装入口**：它驱动 CMake 构建 C++/MLIR 扩展，又通过 package 映射 + 符号链接 + entry points 把 `ascend` 后端挂载进 `triton` 命名空间（这是 [u1-l2](u1-l2-directory-structure-and-layering.md) 分层原则的落地机制）。
- **`CMakeLists.txt` 负责真正的 C++ 编译**：查找 MLIR/LLVM、`add_subdirectory(third_party/ascend)` 编译后端 pass、产出 `libtriton`/`triton-mlir-opt`/`triton-opt`/`FileCheck`。
- **LLVM 来自预编译下载或源码自建**：包名由 `llvm-hash.txt` + patch 哈希 + 平台后缀拼成，patch 变化会触发重新下载；离线场景用 `LLVM_SYSPATH` 或 `TRITON_OFFLINE_BUILD`。

## 7. 下一步学习建议

- 装好后，立刻进入 [u1-l4 跑通第一个 kernel：vector-add 教程](u1-l4-first-kernel-vector-add.md)，用 `01-vector-add.py` 验证环境并建立对 `@triton.jit` / `tl.load` / `tl.store` 的第一印象。
- 想理解安装后 `import triton` 是怎么找到 ascend 后端的，可先读 [u3-l1 @triton.jit 与编译入口](u3-l1-jit-and-compile-entry.md)，再回看本讲的 entry points 机制。
- 对「为什么 ascend 是 in-tree 后端、安装期如何分层」想更系统了解，复习 [u1-l2 代码结构与「核心 vs Ascend」分层原则](u1-l2-directory-structure-and-layering.md)。
- 后续若要给 Triton-Ascend 新增 C++/MLIR pass，本讲的 `CMakeLists.txt` 后端编译与 `setup.py` 默认构建变量（如 `TRITON_APPEND_CMAKE_ARGS`）会成为你的日常工具，届时可结合 [u10-l5 扩展 C++ pass：二次开发实战](u10-l5-extending-cpp-pass.md) 一起看。
