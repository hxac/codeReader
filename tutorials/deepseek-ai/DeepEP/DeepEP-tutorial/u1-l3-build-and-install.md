# 环境依赖、安装与构建流程

## 1. 本讲目标

上一讲（u1-l2）我们梳理了 DeepEP 的目录分层，并区分了两条编译路径：`csrc/*` 在 `pip install` 时被编进 `_C.so`，而 `deep_ep/include/impls/*.cuh` 作为 header-only 模板在运行时被 JIT 实例化。本讲要回答的是前一条路径里最自然的几个问题：

- DeepEP 到底依赖哪些硬件和软件？为什么？
- `pip install` 时发生了什么？`setup.py` 是怎么把几十个 `.cu`/`.cpp` 文件、include 目录、链接库和编译标志拼到一起的？
- 为什么有些环境变量（如 `EP_JIT_CACHE_DIR`）要在**构建时**就被“烘焙”进安装包？
- `get_nccl_lib_name` 这种看似琐碎的小函数，到底替 pip wheel 用户解决了什么麻烦？

学完本讲，你应该能够独立读懂 `setup.py` 的每一段，理解 DeepEP 包从源码到可 `import` 的产物之间经历的完整工序，并能解释持久化环境变量的两级默认值机制。

## 2. 前置知识

在进入源码之前，先用通俗语言建立几个概念。

### 2.1 什么是“构建一个 CUDA Python 扩展”

PyTorch 提供了 `torch.utils.cpp_extension.CUDAExtension` 和 `BuildExtension`，它们是 `setuptools` 的帮手。简单说，你只要告诉它：

- **源文件列表 `sources`**：要编译哪些 `.cpp` / `.cu` 文件；
- **头文件目录 `include_dirs`**：编译器去哪里找 `#include`；
- **库目录 `library_dirs`** 与 **链接参数 `extra_link_args`**：链接时去哪里找 `.so`、链哪些库；
- **编译参数 `extra_compile_args`**：分 `cxx`（主机编译器 g++ 的标志）和 `nvcc`（NVCC 的标志）两组。

`BuildExtension` 会替你把这些参数翻译成实际的 `g++` / `nvcc` 命令，最终产出一个形如 `deep_ep/_C.cpython-3x-x86_64-linux-gnu.so` 的共享库，Python 端 `import deep_ep._C` 就能加载它。DeepEP 的 `setup.py` 几乎全部工作就是**为这两组参数填空**。

### 2.2 SO 名称、SONAME 与 pip wheel 的小坑

在 Linux 下，一个动态库通常有三个名字，例如：

- 真实文件：`libnccl.so.2.30.4`
- SONAME（写在文件内部的“我自己叫什么”）：`libnccl.so.2`
- 开发者链接时用的“未版本化符号链接”：`libnccl.so`

当我们用 `-lnccl` 或 `-l:libnccl.so` 链接时，链接器需要找到那个未版本化的 `libnccl.so` 符号链接。问题在于：NVIDIA 通过 PyPI 发布的 pip wheel（如 `nvidia-nvshmem-cu12`、`nvidia-nccl-cu12`）**只打包了带版本的 SONAME 文件**（`libnvshmem_host.so.3`、`libnccl.so.2`），而没有那个未版本化的符号链接。于是 `python setup.py install` 时 `-l:libnvshmem_host.so` 就会报“找不到库”。这是后面 `get_nccl_lib_name` 要解决的核心痛点。

### 2.3 “烘焙”环境变量是什么意思

有些配置（比如 JIT 编译缓存放哪个目录）在**集群部署时是固定的**，每个用户都用同一个路径。如果让每个用户在跑代码前都手动 `export EP_JIT_CACHE_DIR=...`，既容易忘也不统一。

DeepEP 的做法是：在管理员执行 `python setup.py install` 时，把这些变量的**当时取值写死**到一个新生成的 `deep_ep/envs.py` 文件里，随包一起发布。用户 `import deep_ep` 时会先读这个文件把这些值当作默认值注入 `os.environ`，但用户自己当前 `export` 的值优先级更高。这就是“构建期烘焙、运行期可覆盖”的两级默认值机制。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md) | 给出官方的运行依赖列表与安装命令 |
| [setup.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py) | 构建主入口：拼接源文件/标志/链接库，烘焙持久化环境变量 |
| [deep_ep/__init__.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py) | `import deep_ep` 时加载烘焙的默认值、校验 NCCL、初始化 JIT |
| [deep_ep/utils/find_pkgs.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/find_pkgs.py) | 在 Python 环境里自动定位 NCCL / NVSHMEM 的安装根目录 |
| [docs/nvshmem.md](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/docs/nvshmem.md) | NVSHMEM 安装指南（版本、IBGDA、环境变量） |

注意：`deep_ep/envs.py` **不存在于源码仓库**，它是构建时由 `setup.py` 动态生成的，所以你在 `git ls-files` 里找不到它——这是本讲的一个重要伏笔。

---

## 4. 核心概念与源码讲解

### 4.1 运行依赖：硬件与软件栈

#### 4.1.1 概念说明

DeepEP 是一个面向 Hopper（SM90）架构 GPU 的极低 SM 占用通信库，它大量使用了只有 SM90 PTX ISA 才支持的指令（如 TMA、mbarrier、FP8）。因此它的依赖比一般 Python 包要“重”得多：既要求特定的 GPU 架构，也要求特定版本的 CUDA / PyTorch / NCCL，再加上为遗留 V1 接口服务的 NVSHMEM。

理解依赖的关键是分清**节点内（intranode）**和**节点间（internode）**两条物理链路：

- 节点内用 **NVLink** 连接同一台机器上的多张 GPU；
- 节点间用 **RDMA**（如 InfiniBand + CX7 网卡）连接不同机器。

这两条链路对应了上一讲提到的 `num_nvlink_ranks` 与 `num_rdma_ranks` 两个物理域。

#### 4.1.2 核心流程

依赖确认的顺序大致是：

1. 确认 GPU 是 Hopper（SM90）或支持 SM90 PTX ISA 的其它架构；
2. 确认 CUDA ≥ 12.3、PyTorch ≥ 2.10；
3. `pip install nvidia-nccl-cu13>=2.30.4`（NCCL ≥ 2.30.4）；
4. 安装 NVSHMEM ≥ 3.3.9（仅遗留方法需要）；
5. `python setup.py install`。

#### 4.1.3 源码精读

官方依赖清单写在 README 的 Requirements 一节：

[README.md:65-72](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L65-L72) — 列出 Hopper / Python 3.8+ / CUDA 12.3+ / PyTorch 2.10+ / NCCL 2.30.4+，以及 NVLink（节点内）和 RDMA（节点间）两条网络要求。

NCCL 的安装方式很有讲究，README 推荐**用 pip 装 NCCL**，这样 DeepEP 能直接在 Python 环境里自动定位它，而无需在系统层面装一份：

[README.md:74-80](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L74-L80) — 推荐命令 `pip install "nvidia-nccl-cu13>=2.30.4" --no-deps`。`--no-deps` 表示不连带安装依赖，避免 pip 自作主张拉一堆东西。

NVSHMEM 的安装则交给了专门的文档：

[README.md:82-84](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L82-L84) — 说明 NVSHMEM 仅为遗留方法所需，并指向安装指南。

在 [docs/nvshmem.md](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/docs/nvshmem.md) 里能看到硬件要求（节点内 NVLink、节点间 RDMA、IBGDA 支持）和软件要求（NVSHMEM ≥ 3.3.9）：

[docs/nvshmem.md:9-18](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/docs/nvshmem.md#L9-L18) — 硬件需要 NVLink + RDMA + IBGDA；软件需要 NVSHMEM v3.3.9 或更高。

NVSHMEM 同样支持 pip wheel 安装（`pip install nvidia-nvshmem-cu12`），这正是后面 `get_nvshmem_host_lib_name` 必须处理 SONAME-only 问题的根源：

[docs/nvshmem.md:24-29](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/docs/nvshmem.md#L24-L29) — NVSHMEM 3.3.9 提供 tarball / RPM / deb / conda / pip wheel 多种格式。

#### 4.1.4 代码实践

**实践目标**：核对运行环境是否满足 DeepEP 的依赖。

**操作步骤**：

1. 用 `nvidia-smi` 查看本机 GPU 型号与 CUDA Driver 版本；
2. 用 `python -c "import torch; print(torch.__version__, torch.version.cuda)"` 查看 PyTorch 与打包的 CUDA 版本；
3. 用 `pip show nvidia-nccl-cu13 2>/dev/null || pip show nvidia-nccl-cu12` 查看 pip 安装的 NCCL 版本；
4. 对照 README 第 65–72 行的四条要求逐项确认。

**需要观察的现象**：GPU 名字应包含 Hopper 系列（如 H800 / H100），PyTorch 版本 ≥ 2.10，NCCL 版本 ≥ 2.30.4。

**预期结果**：在达标机器上，四项均满足；若某项不满足，则 `import deep_ep` 后续会报错或性能严重下降。**待本地验证**（本讲不假定你已经跑过这些命令）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 DeepEP 推荐 `pip install nvidia-nccl-cu13 --no-deps` 而不是用系统包管理器装 NCCL？

**参考答案**：用 pip 装可以让 NCCL 落在 Python 环境里，`deep_ep/utils/find_pkgs.py` 能通过 Python 包元数据自动定位它（见 4.4 节），无需额外配置 `EP_NCCL_ROOT_DIR`；`--no-deps` 避免连带安装可能引起冲突的其它 nvidia 包。

**练习 2**：如果只使用 V2（NCCL Gin 后端），是否还需要安装 NVSHMEM？

**参考答案**：仍需要。当前 `setup.py` 默认会把 NVSHMEM 相关源文件和链接库一起编进 `_C.so`（见 4.2 节），即使用户只用 V2 接口，链接阶段也需要 NVSHMEM 库存在。源码里有 `# TODO: make NVSHMEM and legacy optional` 的备注，说明未来才可能解耦。

---

### 4.2 setup.py 的整体结构与构建拼装

#### 4.2.1 概念说明

`setup.py` 是 DeepEP 的构建主入口（注意：仓库**没有** `CMakeLists.txt`，构建全靠它）。它的核心职责是把“源文件 + 头文件目录 + 编译标志 + 链接库”组装成一次 `setuptools.setup(...)` 调用，由 `BuildExtension` 真正去调用 `g++` 和 `nvcc`。

它还承担两件“副业”：

1. 在构建时把持久化环境变量烘焙成 `envs.py`（4.3 节）；
2. 解决 pip wheel 的 SONAME 问题（4.4 节）。

#### 4.2.2 核心流程

`setup.py` 执行时（即 `python setup.py install`）的流程如下（伪代码）：

```text
1. 定义 persistent_env_names（哪些变量要烘焙）
2. 动态加载 find_pkgs 模块（注意：不能触发 deep_ep.__init__）
3. nvshmem_root_dir = find_nvshmem_root()
   nccl_root_dir  = find_nccl_root()
4. 准备 cxx_flags / nvcc_flags
5. sources = [python_api.cpp, legacy/*.cu ...]
   include_dirs / library_dirs / extra_link_args 各就各位
6. 追加 NVSHMEM 源文件、include、链接库
7. 追加 NCCL 源文件、include、链接库（用 get_nccl_lib_name 解析真实 SO 名）
8. 处理 TORCH_CUDA_ARCH_LIST / DISABLE_SM90_FEATURES / DISABLE_AGGRESSIVE_PTX_INSTRS
9. 处理 EP_NUM_TOPK_IDX_BITS（影响 topk_idx 的位宽）
10. CustomBuildPy.generate_default_envs() 生成 envs.py
11. setuptools.setup(ext_modules=[CUDAExtension(name='deep_ep._C', ...)], ...)
```

关键在于：被编进 `_C.so` 的只有 `csrc/*` 下的源文件，而 `deep_ep/include/impls/*.cuh` 是作为 `package_data` 随包发布的**头文件**，运行时才被 JIT 编译——这与上一讲的结论完全一致。

#### 4.2.3 源码精读

**入口与版本号**。`setup.py` 先定义持久化变量名单，并定位仓库根目录：

[setup.py:12-13](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L12-L13) — `current_dir` 是仓库根，`persistent_env_names` 列出 4 个要在构建期烘焙的变量：`EP_JIT_CACHE_DIR`、`EP_JIT_PRINT_COMPILER_COMMAND`、`EP_NUM_TOPK_IDX_BITS`、`EP_NCCL_ROOT_DIR`。

版本号不是写死的，而是**从 `deep_ep/__init__.py` 里读 `__version__`，再拼上 git 短哈希**：

[setup.py:50-67](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L50-L67) — 用正则从 `__init__.py` 抓出 `__version__`；若 git 工作区干净，则追加 `+<short HEAD>`，否则追加 `+local`。所以安装包版本形如 `2.1.0+099d5f2`。

**源文件拼装**。最关键的“编进 `_C.so` 的源文件清单”是这样累加起来的：

[setup.py:100-106](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L100-L106) — 起始 `sources` 只有三件：`csrc/python_api.cpp`（pybind11 入口）、`csrc/kernels/legacy/layout.cu`、`csrc/kernels/legacy/intranode.cu`；`include_dirs` 包含 `deep_ep/include`（这就是 JIT 头文件也参与安装期编译的 include 路径）、`third-party/fmt/include` 和 `/usr/local/cuda/include/cccl`；`extra_link_args` 起步需要 `-lcuda`（CUDA driver API）。

接着追加 NVSHMEM 与 NCCL：

[setup.py:108-124](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L108-L124) — NVSHMEM 段加入 `internode.cu`、`internode_ll.cu`、`backend/nvshmem.cu`，并设置 `-dlink`（device link，NVSHMEM 的设备端库需要单独 device-link）、`-lnvshmem_device` 与 host 库；NCCL 段加入 `backend/nccl.cu`，并用 `get_nccl_lib_name` 解析真实 SO 文件名。两段都通过 `-Wl,-rpath,...` 把库路径写进 rpath，运行时无需手动设置 `LD_LIBRARY_PATH`。

最后追加 CUDA driver 源文件：

[setup.py:126-127](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L126-L127) — 加入 `csrc/kernels/backend/cuda_driver.cu`，这是 AGRS 等特性用到的 CUDA driver API 封装（如 `cudaMemcpyBatchAsync`）。

**架构与编译标志**。DeepEP 默认按 H800（SM90 = compute capability 9.0）编译：

[setup.py:140-145](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L140-L145) — `TORCH_CUDA_ARCH_LIST` 默认 `9.0`；NVCC 加上 `-rdc=true`（可重定位设备代码，配合 NVSHMEM device link）和 `--ptxas-options=--register-usage-level=10`（压低寄存器用量以让更多 warp 驻留）。

同时有一个 `DISABLE_AGGRESSIVE_PTX_INSTRS` 开关，默认开启，用来禁用某些 CUDA 版本不支持的 PTX 指令（如 `.L1::no_allocate`）：

[setup.py:147-155](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L147-L155) — 仅当 `TORCH_CUDA_ARCH_LIST` 严格等于 `9.0` 时才允许放开激进 PTX 指令，其它架构一律禁用并通过宏 `-DDISABLE_AGGRESSIVE_PTX_INSTRS` 传到源码里。

**topk_idx 位宽**。`topk_idx` 是路由表里“每个 token 选了哪些专家”的索引张量，默认 32 位，也可以 64 位（大规模 EP 时专家编号可能很大）。这个选择必须在**编译期**确定，因为 `csrc/python_api.cpp` 里用了一个固定的 C++ 类型别名 `topk_idx_t`：

[setup.py:157-166](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L157-L166) — 兼容旧的 `TOPK_IDX_BITS` 环境变量名（重命名为 `EP_NUM_TOPK_IDX_BITS`），并通过 `-DEP_NUM_TOPK_IDX_BITS=N` 宏把位宽烘焙进 C++ 源码。

**组装与最终调用**：

[setup.py:168-174](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L168-L174) — 把 `cxx` / `nvcc` / 可选的 `nvcc_dlink` 三组标志装进 `extra_compile_args`。

[setup.py:197-218](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L197-L218) — 最终的 `setuptools.setup`：扩展模块名为 `deep_ep._C`（即产出的 `.so`），`package_data` 把 `deep_ep/include/deep_ep/**/*`（JIT 头文件）随包发布，`cmdclass` 注册了两个自定义命令——`build_ext=BuildExtension`（PyTorch 的 CUDA 编译器）和 `build_py=CustomBuildPy`（用来生成 `envs.py`）。

注意 `find_packages(include=['deep_ep', 'deep_ep.*'])`：`deep_ep` 既是 Python 包名，也是 C++ include 前缀（`#include <deep_ep/...>`），这是上一讲提到的同名易混淆点在构建层面的体现。

#### 4.2.4 代码实践

**实践目标**：在不实际编译的前提下，预演 `setup.py` 会拼出怎样的编译命令。

**操作步骤**：

1. 在 `setup.py` 第 197 行 `setuptools.setup(...)` 之前已经有一段 `print('Build summary:')`（第 177–195 行）通篇打印 `sources / includes / libraries / flags / arch / NVSHMEM path / NCCL path / persistent envs`；
2. 想象你设置了 `EP_NCCL_ROOT_DIR=/opt/nccl EP_NUM_TOPK_IDX_BITS=64` 后运行 `python setup.py build`；
3. 对照源码，推导“Build summary”里 `Sources`、`Arch list`、`Persistent envs` 三行分别会打印什么。

**需要观察的现象**：`Sources` 里包含 `csrc/kernels/elastic/dispatch.hpp` 吗？`Persistent envs` 里会出现哪几个键？

**预期结果**：`Sources` **不会**包含 `dispatch.hpp`——它是 header-only 模板，运行时 JIT 才实例化（验证上一讲的结论）；`Persistent envs` 会出现 `EP_NCCL_ROOT_DIR` 和 `EP_NUM_TOPK_IDX_BITS` 两行（因为它们出现在环境里且属于 `persistent_env_names`）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `extra_link_args` 起步就要 `-lcuda`？

**参考答案**：`-lcuda` 链接的是 CUDA **driver** API（`libcuda.so`），区别于运行时 API（`libcudart`）。DeepEP 在 `cuda_driver.cu` 里直接调用了 driver API（如 `cuMemCreate`、`cudaMemcpyBatchAsync`），所以必须链接 driver 库。

**练习 2**：`--ptxas-options=--register-usage-level=10` 这个标志的意图是什么？

**参考答案**：它告诉 PTXAS 主动压低每个线程的寄存器用量（10 是较激进档位）。寄存器用得越少，单个 SM 能驻留的 warp 越多，从而提升 occupancy——这对 DeepEP“用少量 SM 跑通信内核”的设计目标尤其重要。

---

### 4.3 持久化环境变量烘焙机制

#### 4.3.1 概念说明

这是本讲最巧妙的设计。问题背景是：JIT 编译缓存目录、NCCL 根目录、topk 位宽等配置在**一个集群内通常是固定的**，但它们同时又必须能被用户在运行时覆盖。

DeepEP 的解决方案是“两级默认值”：

- **构建期默认值**：管理员 `python setup.py install` 时，这些变量的取值被写进一个动态生成的 `deep_ep/envs.py`，随包发布；
- **运行期覆盖**：用户 `import deep_ep` 时，先把 `envs.py` 里的值注入 `os.environ`（仅当该变量当前不存在时），用户当前 `export` 的值优先级最高。

注意 `envs.py` 是**生成物**，不在 git 里，所以你看不到它的源码版本——这是初学者最容易困惑的一点。

#### 4.3.2 核心流程

烘焙与加载的流程：

```text
构建期（setup.py）:
  for name in persistent_env_names:
      if name in os.environ:
          写一行 persistent_envs['name'] = 'value' 到 envs.py

运行期（import deep_ep）:
  from .envs import persistent_envs        # 可能 ImportError（未烘焙）
  for k, v in persistent_envs.items():
      if k not in os.environ:              # 关键：不覆盖已有值
          os.environ[k] = v
```

关键就是运行期那一句 `if k not in os.environ`——它保证了用户运行时 `export` 的值永远赢。

#### 4.3.3 源码精读

**构建期：生成 envs.py**。这件事由一个自定义的 `build_py` 子类完成：

[setup.py:70-89](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L70-L89) — `CustomBuildPy` 重写了 `run()`：先调 `generate_default_envs()` 生成 `envs.py`，再调用父类 `build_py.run()` 完成常规构建。`generate_default_envs` 遍历 `persistent_env_names`，对每个**当前在 `os.environ` 里**的变量写一行 `persistent_envs['NAME'] = 'value'`，写到 `build_lib/deep_ep/envs.py`。注意：未在环境里设置的变量**不会**被写进去，从而保持“未设置”语义。

**运行期：加载 envs.py**。`import deep_ep` 时，`__init__.py` 最开头就尝试加载这份默认值：

[deep_ep/__init__.py:10-18](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L10-L18) — `try: from .envs import persistent_envs`，逐项写入 `os.environ`（仅当键不存在）；`except ImportError: pass` 兜底——如果包是从源码直接跑（没经过 `setup.py` 烘焙），`envs.py` 不存在，就静默跳过，不会报错。

这就是为什么 README 里专门有一段说明：

[README.md:367](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L367) — 明确列出四个持久化变量：`EP_JIT_CACHE_DIR`、`EP_JIT_PRINT_COMPILER_COMMAND`、`EP_NUM_TOPK_IDX_BITS`、`EP_NCCL_ROOT_DIR`，并说明“构建期捕获、import 时作为默认值应用、除非被当前环境变量覆盖”。

**为什么这四个变量要烘焙？**

- `EP_JIT_CACHE_DIR`：JIT 编译的 cubin 缓存放哪。集群里通常希望所有用户共享同一个 NFS 上的缓存目录，避免每个用户重复编译；
- `EP_NCCL_ROOT_DIR`：NCCL 安装路径。安装机器确定了，路径就固定了；
- `EP_NUM_TOPK_IDX_BITS`：topk 位宽。注意它在 `setup.py` 里**同时**通过 `-DEP_NUM_TOPK_IDX_BITS=N` 宏影响 C++ 编译，又作为持久化变量影响运行期 Python 端逻辑，必须两边一致；
- `EP_JIT_PRINT_COMPILER_COMMAND`：调试用，是否打印 JIT 编译命令。

#### 4.3.4 代码实践

**实践目标**：亲手验证烘焙机制的两级默认值行为。

**操作步骤**（源码阅读 + 思维实验，不需要真装）：

1. 在 [setup.py:13](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L13) 找到 `persistent_env_names` 的定义；
2. 假设你在构建前 `export EP_JIT_CACHE_DIR=/shared/ep_cache`，然后 `python setup.py install`；
3. 阅读 [setup.py:78-89](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L78-L89) 推断生成的 `envs.py` 内容（应包含一行 `persistent_envs['EP_JIT_CACHE_DIR'] = '/shared/ep_cache'`）；
4. 阅读 [deep_ep/__init__.py:14-16](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L14-L16) 推断：用户运行时再 `export EP_JIT_CACHE_DIR=/local/x`，最终 `os.environ['EP_JIT_CACHE_DIR']` 是哪个值？

**需要观察的现象**：`envs.py` 是否只包含构建期实际设置了的那几个变量？运行期覆盖是否生效？

**预期结果**：`envs.py` 只包含构建期存在于 `os.environ` 的变量；运行期用户 `export` 的 `/local/x` 会赢，因为加载逻辑里有 `if key not in os.environ` 守卫。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果你直接从 git clone 的源码 `python -c "import deep_ep"`（没有先 `setup.py install`），加载持久化变量这一步会发生什么？

**参考答案**：`from .envs import persistent_envs` 会抛 `ImportError`（因为 `envs.py` 不存在），被第 17 行的 `except ImportError: pass` 静默吞掉。不会报错，但持久化默认值不会生效——所有持久化变量都必须用户自己 `export`。这也提醒我们：开发模式下（`python setup.py build` + 软链 SO）同样没有烘焙的 `envs.py`。

**练习 2**：为什么 `EP_NUM_TOPK_IDX_BITS` 既要在 `setup.py` 里通过宏传给 C++，又要作为持久化变量烘焙？

**参考答案**：topk 位宽同时影响两个层面——C++ 端 `topk_idx_t` 的类型定义（编译期，靠 `-DEP_NUM_TOPK_IDX_BITS` 宏）和 Python 端某些运行期逻辑（靠持久化变量）。两者必须取同一个值，烘焙机制保证了“构建期定下的值在运行期自动复现”，避免用户运行时设错导致 Python 与 C++ 不一致。

---

### 4.4 SO 名称解析与 NCCL/NVSHMEM 自动定位

#### 4.4.1 概念说明

本模块解决两个紧密相关的问题：

1. **定位**：构建时去哪里找 NCCL / NVSHMEM 的安装目录？（`find_pkgs.py`）
2. **链接**：找到目录后，真实的 `.so` 文件叫什么名字？（`_find_versioned_so` / `get_nccl_lib_name`）

第二个问题就是本讲前面提到的 pip wheel 的 SONAME-only 坑。DeepEP 用一个小函数优雅地解决了它。

#### 4.4.2 核心流程

```text
find_nccl_root():
  1. 先看环境变量 EP_NCCL_ROOT_DIR / NCCL_DIR
  2. 否则扫描 Python 包元数据，找 nvidia-nccl-* 里含 libnccl.so 的安装位置
  3. 返回根目录（其下有 include/ 和 lib/）

get_nccl_lib_name(root):
  1. 优先找未版本化符号链接 libnccl.so（tarball 安装时存在）
  2. 否则取 libnccl.so.* 中字典序最小的那个（即 SONAME，pip wheel 提供）
  → 链接时用 -l:<真实文件名> 精确指定
```

#### 4.4.3 源码精读

**自动定位**。`find_pkg_root` 是通用逻辑，NCCL 和 NVSHMEM 都复用它：

[deep_ep/utils/find_pkgs.py:8-54](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/find_pkgs.py#L8-L54) — 先按 `EP_{NAME}_ROOT_DIR` / `{NAME}_DIR` 的优先级查环境变量（第 22–24 行）；否则遍历 `importlib.metadata.distributions()`，匹配包名含 `nvidia-{name}` 的发行版，在它的 `files` 列表里找包含 `lib_name` 的文件，反推出根目录。它还按 `sys.path` 顺序挑选优先级最高的匹配，避免多虚拟环境并存时找错。

NCCL 与 NVSHMEM 的封装就是一行：

[deep_ep/utils/find_pkgs.py:57-82](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/find_pkgs.py#L57-L82) — `find_nccl_root` 用 `lib_name='libnccl.so'` 定位；`find_nvshmem_root` 不传 `lib_name`（走目录判定）。两者都用 `@functools.lru_cache()` 缓存，因为一次会话里路径不会变。

**注意一个微妙之处**：`setup.py` 不能直接 `import deep_ep.utils.find_pkgs`，因为那会触发 `deep_ep/__init__.py` 全部副作用（`check_nccl_so` / `init_jit`），而构建时 `_C.so` 还没编出来。所以它用 `importlib.util` 手动加载这个模块文件，绕过包初始化：

[setup.py:15-18](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L15-L18) — 用 `spec_from_file_location` 直接从文件路径加载 `find_pkgs.py`，注释明确写“Load discover module without triggering `deep_ep.__init__`”。

**SO 名称解析**。这是本模块的点睛之笔：

[setup.py:26-39](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L26-L39) — `_find_versioned_so(base_dir, prefix)` 在 `base_dir/lib` 下先找未版本化的 `{prefix}.so`（tarball 安装会有，行为与原来一致）；找不到就退回到 `{prefix}.so.*` 里字典序最小的那个（pip wheel 只提供这种 SONAME 文件）；都找不到就抛 `ModuleNotFoundError`。文件开头的长注释把 pip wheel 的 SONAME-only 现象解释得清清楚楚。

[setup.py:42-47](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L42-L47) — `get_nvshmem_host_lib_name` 和 `get_nccl_lib_name` 都是薄封装。

**怎么用上这个真实名字**：

[setup.py:116-124](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L116-L124) — NVSHMEM 用 `-l:{nvshmem_host_lib}`（注意是 `-l:` 加完整文件名，而不是 `-l` 加库名），NCCL 同理用 `-l:{nccl_lib}`。`-l:NAME` 是链接器的“精确文件名链接”语法，它会直接找叫 `NAME` 的文件而不是去猜 `libNAME.so`。这样无论真实文件是 `libnccl.so`（tarball）还是 `libnccl.so.2`（pip wheel），都能正确链接。

值得对比的是：NVSHMEM 的 **device 静态库** `libnvshmem_device.a` 仍用硬编码名字，因为 pip wheel 里它确实按规范名发布（见第 115、117 行）。

#### 4.4.4 代码实践

**实践目标**：亲手体验 `get_nccl_lib_name` 解决的问题。

**操作步骤**（源码阅读型）：

1. 假设你的 NCCL 来自 pip wheel，`find_nccl_root()` 返回的 `lib/` 目录下只有 `libnccl.so.2`，没有 `libnccl.so`；
2. 阅读 [setup.py:33-38](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L33-L38)，推断 `_find_versioned_so` 会返回什么；
3. 追踪这个返回值如何流到 [setup.py:123-124](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L123-L124) 的 `extra_link_args`；
4. 思考：如果没有 `get_nccl_lib_name` 而是写死 `-l:libnccl.so`，pip wheel 用户会得到什么错误？

**需要观察的现象**：返回值应是 `libnccl.so.2`；最终链接参数应是 `-l:libnccl.so.2`。

**预期结果**：写死 `-l:libnccl.so` 时，pip wheel 用户构建会报 `cannot find -l:libnccl.so`（因为文件不存在），这正是 `get_nccl_lib_name` 要规避的。**待本地验证**（若有 pip wheel 环境）。

#### 4.4.5 小练习与答案

**练习 1**：`-lnccl` 和 `-l:libnccl.so.2` 在链接器行为上有什么区别？

**参考答案**：`-lnccl` 让链接器去搜索 `libnccl.so` / `libnccl.so.*` 并按 SONAME 规则选择，依赖未版本化符号链接存在；`-l:libnccl.so.2` 是精确文件名链接，链接器直接找名为 `libnccl.so.2` 的文件，不需要符号链接。后者对 pip wheel 这种“只有 SONAME 文件”的安装更稳健。

**练习 2**：为什么 `setup.py` 用 `importlib.util.spec_from_file_location` 加载 `find_pkgs.py`，而不是 `from deep_ep.utils import find_pkgs`？

**参考答案**：`import deep_ep.utils.find_pkgs` 会先执行父包 `deep_ep/__init__.py`，而它会调用 `check_nccl_so()` 和 `init_jit()`，后者需要 `deep_ep._C`——但构建时 `_C.so` 还没编出来，必然失败。直接按文件路径加载模块可以彻底绕过 `deep_ep` 包的初始化。

---

## 5. 综合实践

把本讲的四个最小模块串起来，做一次“构建流程全景追踪”。

**任务**：模拟一次 `python setup.py install`，写出每一阶段的关键产物。

**步骤**：

1. **依赖准备**：按 4.1 节确认 Hopper / CUDA 12.3+ / PyTorch 2.10+ / NCCL 2.30.4+，并 `pip install nvidia-nccl-cu13>=2.30.4 --no-deps` 与 NVSHMEM；
2. **环境变量预设**（可选）：`export EP_JIT_CACHE_DIR=/shared/ep_cache EP_NUM_TOPK_IDX_BITS=32`；
3. **追踪定位**：阅读 [find_pkgs.py:8-54](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/find_pkgs.py#L8-L54)，说明 `find_nccl_root()` 在 pip wheel 安装下如何返回 NCCL 根目录；
4. **追踪链接名**：阅读 [setup.py:26-47](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L26-L47)，推断 `get_nccl_lib_name` 与 `get_nvshmem_host_lib_name` 的返回值；
5. **追踪烘焙**：阅读 [setup.py:70-89](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L70-L89)，写出本次构建生成的 `envs.py` 完整内容；
6. **追踪运行期**：阅读 [deep_ep/__init__.py:10-18](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L10-L18)，说明 `import deep_ep` 时这些默认值如何被加载，以及用户运行时覆盖的优先级。

**预期产物**：一份包含“定位结果 / 链接参数 / envs.py 内容 / 运行期加载顺序”的四段式报告。如果有真实 8 卡 Hopper 环境，可在第 5 步后真的跑一次 `python setup.py build` 并查看 `build/lib.*/deep_ep/envs.py` 的实际内容来核对。**待本地验证**。

## 6. 本讲小结

- DeepEP 的运行依赖较重：Hopper（SM90）GPU、CUDA 12.3+、PyTorch 2.10+、NCCL 2.30.4+、NVLink（节点内）、RDMA（节点间），以及仅为遗留方法的 NVSHMEM ≥ 3.3.9。
- `setup.py` 是唯一的构建入口（无 CMake），它把 `csrc/*` 源文件、include 目录、NVSHMEM/NCCL 链接库和 cxx/nvcc 标志拼成一次 `CUDAExtension(name='deep_ep._C')` 调用；`deep_ep/include/impls/*.cuh` 仅作为 `package_data` 随包发布，留给运行时 JIT。
- 持久化环境变量（`EP_JIT_CACHE_DIR` 等 4 个）在构建期被烘焙进动态生成的 `envs.py`，`import deep_ep` 时作为默认值注入 `os.environ`，但运行期用户 `export` 的值优先级更高——即“构建期烘焙、运行期可覆盖”。
- `get_nccl_lib_name` / `get_nvshmem_host_lib_name` 通过优先取未版本化符号链接、否则回退到 SONAME 文件的方式，解决了 NVIDIA pip wheel 只发布带版本 SO、缺少未版本化符号链接导致 `-l:` 链接失败的问题。
- `find_pkgs.py` 通过环境变量优先、再扫描 Python 包元数据的方式自动定位 NCCL/NVSHMEM 根目录；`setup.py` 用 `importlib.util` 加载它以避免触发尚未就绪的 `deep_ep.__init__`。

## 7. 下一步学习建议

本讲只覆盖了**安装期编译**这一条路径，把 `_C.so` 编了出来。下一步应该沿着上一讲提到的另一条路径——**运行时 JIT 编译**——继续深入：

- 先读 [deep_ep/__init__.py:71-84](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L71-L84) 里的 `init_jit()`，理解它如何把库根路径、CUDA home、NCCL root 注入 JIT 编译器；这会自然过渡到 **u2-l1（import deep_ep 背后做了什么）**。
- 如果你想先看到 DeepEP 真正跑起来，可以跳到 **u1-l4（快速上手：跑通 test_ep.py）**，用本讲确认好的环境实际执行一次 dispatch+combine。
- 对构建工程细节感兴趣的同学，可以再读 `csrc/python_api.cpp` 的 `register_apis`，看看 `_C` 模块对外暴露了哪些函数，为后续 Python 接口层（U2）做铺垫。
