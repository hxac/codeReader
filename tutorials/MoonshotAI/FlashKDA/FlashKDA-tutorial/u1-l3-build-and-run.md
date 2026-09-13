# 从零构建与运行：submodule、架构选择与测试脚本

## 1. 本讲目标

上一讲（u1-l2）我们已经理解了 KDA 的数学递推。本讲暂时离开算法，解决一个工程问题：**把 FlashKDA 在一台 GPU 机器上从源码构建出来、并验证它能正确运行**。读完本讲，你应该能够：

1. 独立完成 `clone → submodule update → pip install --no-build-isolation` 的完整构建流程，并解释每一步为什么必要。
2. 读懂 `setup.py` 的全部骨架：CUDAExtension、CUTLASS 头文件路径、两个编译单元的组织方式。
3. 理解 `FLASH_KDA_CUDA_ARCHS` 的 `auto / all / 列表` 三种模式，以及 `90a/100a/103a/120a` 这串架构号与 `-gencode` 的关系。
4. 说出每个 nvcc 编译选项的用途（`--use_fast_math`、`--register-usage-level=10`、`-lineinfo` 等），并知道它们分别服务于性能、精度还是剖析。
5. 用 `bash tests/test.sh` 一键跑通正确性测试，并理解这个脚本背后做了什么。
6. 可选：用 `setup_clangd.sh` 配好 clangd，让 IDE 能对 CUDA 源码做跳转和补全。

## 2. 前置知识

本讲是纯工程内容，不涉及新数学，但需要几个打包与 CUDA 编译的基础概念：

- **setuptools / pip 的构建流程**：`pip install .` 会执行仓库根目录的 `setup.py`，把声明的 Python 包（`flash_kda`）和 C/C++ 扩展编译出来并装进当前环境。
- **build isolation（构建隔离）**：现代 pip 默认在一个"干净的"临时环境里装构建依赖再执行构建。但构建 CUDA 扩展时**必须**能 `import torch`（要用 `torch.utils.cpp_extension` 提供的 `CUDAExtension` 类和 `CUDA_HOME` 探测）。隔离环境里没有 torch，构建必然失败——所以 README 强调 `--no-build-isolation`。
- **git submodule（子模块）**：把另一个 git 仓库（这里是 NVIDIA 的 CUTLASS）以"固定在某个 commit"的方式挂载为本仓库的一个子目录。**普通 `git clone` 不会下载 submodule 的内容**，clone 完 `cutlass/` 只是一个空目录。
- **CUTLASS / CuTe**：NVIDIA 官方的高性能 GPU 模板库（header-only，只有头文件、没有要链接的库），CuTe 是其中的布局/张量代数层。FlashKDA 的 kernel 全部用 CuTe 写成。
- **CUDA 架构号与 `-gencode`**：nvcc 用 `-gencode arch=compute_XX,code=sm_XX` 指定目标。`arch=` 是"虚拟架构"（决定语言特性集），`code=` 是"实架构"（决定生成哪份机器码 cubin）。一份 `.so` 里可以嵌多份 cubin（fatbin），运行时由驱动按当前 GPU 挑选。
- **架构号的 `a` 后缀**：如 `90a` 表示 architecture-specific 目标。只有 `compute_90a` 才允许编译器使用 Hopper 专有指令（TMA、wgmma/GMMA、cluster 等）。FlashKDA 重度依赖 TMA 与 GMMA，所以目标是 `90a` 而不是 `90`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [setup.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py) | 构建脚本，全部约 100 行 | submodule 自动拉取、架构 flags、nvcc 选项 |
| [.gitmodules](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/.gitmodules) | submodule 声明 | `cutlass` 指向 NVIDIA/cutlass |
| [README.md](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md) | 官方安装/测试说明 | Installation、Tests、Development 三节 |
| [tests/test.sh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test.sh) | 一键测试入口 | 4 行脚本各自的意义 |
| [tests/run_test_full.sh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/run_test_full.sh) | 全量参数扫描测试 | pytest-xdist 多 GPU 并行 |
| [setup_clangd.sh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup_clangd.sh)、[.clangd.template](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/.clangd.template)、[config.yaml](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/config.yaml) | clangd 开发环境三件套 | 模板替换 + 全局配置 |
| [csrc/smxx/fwd_launch.cu](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu)、[csrc/smxx/utils.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh) | 唯一的 `.cu` 与公共头 | 解释"为什么只有两个编译单元" |

## 4. 核心概念与源码讲解

### 4.1 CUDAExtension 构建骨架与 CUTLASS submodule

#### 4.1.1 概念说明

PyTorch 提供的 `torch.utils.cpp_extension.CUDAExtension` 是一个 setuptools 扩展类：你把 `.cpp`（host 代码）和 `.cu`（device 代码）交给它，它会自动用正确的 nvcc/g++ 参数编译、链接成一个可直接 `import` 的 Python 模块（一个 `.so` 文件）。

FlashKDA 的全部 C++/CUDA 侧就通过**一个** `CUDAExtension` 构建，产物名为 `flash_kda_C`。Python 包 `flash_kda` 在运行时直接从这个编译产物导入函数：

```python
from flash_kda_C import fwd as _fwd_raw, get_workspace_size
```

这行 [flash_kda/__init__.py:1-2](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py#L1-L2) 说明：装好之后，Python 层与 CUDA 层的边界就是这个 `flash_kda_C` 模块——这也是 u1-l4 画调用链的起点。

而 CUTLASS 以 **git submodule** 的形式提供。`.gitmodules` 只有三行：

- [.gitmodules:1-3](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/.gitmodules#L1-L3)：声明子模块 `cutlass`，挂载路径 `cutlass/`，上游是 `https://github.com/NVIDIA/cutlass.git`。

为什么用 submodule 而不是 pip 依赖？因为 CUTLASS 是 header-only 的 C++ 模板库，**不在 PyPI 上发布**，编译期只需要它的头文件出现在 include path 里。submodule 把它钉在一个已知 commit 上，保证所有人编出一致的结果。

#### 4.1.2 核心流程

`pip install .` 执行 `setup.py` 时，按顺序发生：

1. **submodule 兜底拉取**：脚本第一件事就是执行 `git submodule update --init cutlass`——即使你忘了拉 submodule，构建也能自愈（前提是网络可达、且目录仍在 git 仓库内）。
2. **计算平台相关参数**：读取环境变量（`NVCC_THREADS`、`FLASH_KDA_CUDA_ARCHS`），探测当前 GPU，生成 `-gencode` 列表（见 4.2）。
3. **编译两个编译单元**：
   - `csrc/flash_kda.cpp`（host 侧：pybind 绑定、输入校验、模板分发）由 g++ 编译；
   - `csrc/smxx/fwd_launch.cu`（唯一的 `.cu`）由 nvcc 编译——它 `#include` 了两个 kernel 实现头 `fwd_kernel1.cuh` / `fwd_kernel2.cuh`，后者又引入 `utils.cuh`，`utils.cuh` 再引入大量 `<cute/...>`、`<cutlass/...>` 头文件。**整个 CUDA 侧是一个编译单元。**
4. **链接**成 `flash_kda_C.cpython-3xx-...so`，与 Python 包 `flash_kda` 一起安装。
5. 版本号追加 git 短 hash，形如 `0.0.1+7afb9f4`。

#### 4.1.3 源码精读

**（a）submodule 自动拉取。** [setup.py:1-7](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L1-L7)：先 import，然后第 7 行无条件执行 `subprocess.run(["git", "submodule", "update", "--init", "cutlass"])`。注意它**不带 `--recursive`**，而 README 的安装命令带（见下面 README 引文）；对当前的 cutlass 上游而言两者效果相同（没有必须递归拉取的嵌套依赖时）。已经初始化过的仓库重复执行这条命令是幂等的、几乎零开销。

**（b）README 的标准安装流程。** [README.md:14-20](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L14-L20)：

```bash
git clone https://github.com/MoonshotAI/FlashKDA.git flash-kda
cd flash-kda
git submodule update --init --recursive
pip install -v --no-build-isolation .
```

**（c）CUDAExtension 的骨架。** [setup.py:55-86](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L55-L86) 声明了：

- `name='flash_kda_C'`：编译产物模块名（Python 里 `import flash_kda_C`）。
- `sources=['csrc/flash_kda.cpp', 'csrc/smxx/fwd_launch.cu']`：**只有两个源文件**。为什么 2400 行 CUDA 代码只算一个 `.cu`？看 [csrc/smxx/fwd_launch.cu:1-3](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L1-L3)：它依次 include `fwd.h`、`fwd_kernel1.cuh`、`fwd_kernel2.cuh`——两个 kernel（准备阶段 K1 与递推阶段 K2）以头文件形式被拉进同一个翻译单元。`.cuh` 后缀在这里是"实现头"（被 include 的实现代码），不是公共接口。
- `include_dirs` 四项：`cutlass/include`、`cutlass/examples/common`、`cutlass/tools/util/include`、`csrc`。前三项全部指向 **submodule 内部**——这就是"不拉 submodule 就必然编译失败"的直接原因：`utils.cuh` 里第一条 `#include <cute/tensor.hpp>`（[csrc/smxx/utils.cuh:9-22](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L9-L22) 就找不到。第四项 `csrc` 使 `#include "smxx/fwd_kernel1.cuh"` 这类仓库内相对 include 可解析。
- `extra_compile_args`：host 与 nvcc 的选项，逐项讲解见 4.3。

**（d）版本号。** [setup.py:89-95](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L89-L95)：优先取环境变量 `FLASH_KDA_VERSION_SUFFIX`，否则追加 `git rev-parse --short HEAD`。所以本地构建出的包版本能唯一对应到源码 commit，方便 CI 与复现。

**（e）为什么必须 `--no-build-isolation`。** [setup.py:4](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L4) 从 `torch.utils.cpp_extension` 导入 `CUDAExtension`、`BuildExtension`、`CUDA_HOME`——构建一开始就需要 `import torch` 成功。项目根目录没有 `pyproject.toml`，pip 走 legacy 构建路径、直接使用当前环境；`--no-build-isolation` 把这一行为显式固定下来，避免未来 pip 策略变化导致隔离环境里找不到 torch。同时 [setup.py:33](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L33) 断言 `CUDA_HOME is not None`——必须装的是 **CUDA 版 PyTorch**。

#### 4.1.4 代码实践：亲手验证 submodule 的必要性

1. **实践目标**：确认"空 `cutlass/` 目录 → 构建失败；拉取后 → 头文件就位"的因果关系，把抽象的 submodule 概念落到磁盘上。
2. **操作步骤**：
   ```bash
   git clone https://github.com/MoonshotAI/FlashKDA.git flash-kda
   cd flash-kda
   ls cutlass/          # 观察：目录存在但为空
   ls cutlass/include/cute/tensor.hpp   # 观察：文件不存在
   ```
   然后执行 `git submodule update --init cutlass`，再重复后两条命令。
3. **需要观察的现象**：拉取前 `cutlass/` 为空目录；拉取后出现完整的 CUTLASS 源码树，`cutlass/include/cute/tensor.hpp` 存在。
4. **预期结果**：拉取 submodule 之前直接 `pip install -v --no-build-isolation .` 会在编译 `fwd_launch.cu` 时报 `cute/tensor.hpp: No such file or directory` 之类的致命错误（实际上 `setup.py` 第 7 行会先自动拉取，只有该自动拉取失败——例如断网或目录脱离了 git 仓库——才会走到这一步；具体报错文本**待本地验证**）。
5. 第二个观察点：`git submodule status` 输出的 commit hash 前若带空格表示已初始化、带 `-` 表示未初始化。

#### 4.1.5 小练习与答案

**练习 1**：`sources` 里只有两个文件，但仓库里 CUDA 相关代码有 5 个文件（`flash_kda.cpp`、`fwd_launch.cu`、`fwd_kernel1.cuh`、`fwd_kernel2.cuh`、`utils.cuh`）。其余 3 个文件以什么方式参与编译？

**答案**：`fwd_launch.cu` 在文件头部 `#include "fwd_kernel1.cuh"` 与 `"fwd_kernel2.cuh"`（[csrc/smxx/fwd_launch.cu:1-3](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L1-L3)），两个 kernel 头又各自 `#include "utils.cuh"`。它们是"实现头"，全部在 `fwd_launch.cu` 这一个翻译单元里展开。`flash_kda.cpp` 则是独立的 host 编译单元。

**练习 2**：`setup.py` 里的自动拉取和 README 的 `git submodule update --init --recursive` 有什么差别？为什么两个写法都可行？

**答案**：差别是 `--recursive`（递归初始化嵌套子模块）以及是否限定路径（`setup.py` 限定只拉 `cutlass`）。当 cutlass 上游没有必须的嵌套子模块时，两者效果一致；`setup.py` 的版本是"忘了拉也能构建"的兜底。

**练习 3**：为什么必须加 `--no-build-isolation`？

**答案**：构建脚本第一行就要 `import torch`（拿 `CUDAExtension` 与 `CUDA_HOME`）。构建隔离环境是全新的、没有安装 torch 的临时环境，会直接 `ModuleNotFoundError`。`--no-build-isolation` 让 pip 用当前环境执行构建。

### 4.2 架构探测与 FLASH_KDA_CUDA_ARCHS

#### 4.2.1 概念说明

GPU 架构（compute capability）决定可用的指令集。FlashKDA 用到 TMA、GMMA 等 SM90 专有特性，**不能**在旧架构上运行。构建脚本必须回答："这次编译要为哪些架构生成机器码？"

`FLASH_KDA_CUDA_ARCHS` 环境变量就是这个开关，共三种取值：

| 取值 | 行为 | 适用场景 |
|---|---|---|
| `auto`（默认） | 探测**当前可见 GPU** 的 compute capability，只编这一个架构 | 本地开发，编译最快 |
| `all` | 编译全部支持架构：`90a,100a,103a,120a` | 打 wheel、CI、要在多机部署 |
| 逗号分隔列表，如 `90a,100a` | 只编列出的架构 | 指定目标机型 |

#### 4.2.2 核心流程

```
FLASH_KDA_CUDA_ARCHS ──┬── "auto"（默认）── 探测 GPU → [f"{major}{minor}a"]（如 "90a"）
                       │                      └─ 探测不到 GPU → RuntimeError
                       ├── "all" ────────── ["90a","100a","103a","120a"]
                       └── "90a,100a" ───── ["90a","100a"]（逐项 strip、跳过空项）
                                        │
                                        ▼
             对每个 arch 生成一对参数：-gencode arch=compute_{arch},code=sm_{arch}
```

要点：

- **`a` 后缀是硬性要求**。`compute_90a`/`sm_90a` 才启用 Hopper 的 TMA、wgmma、cluster 指令；不带 `a` 的目标无法支撑本项目的 kernel。仓库里实测过的两类硬件（见 `BENCHMARK_H20.md` 与 `BENCHMARK_GB200.md`）分别对应 `90a`（H20）与 `100a`（GB200）。`103a`、`120a` 是更晚的架构家族成员，具体芯片与架构号的对应关系建议以 NVIDIA 官方文档为准。
- **只嵌 cubin，不嵌 PTX**：`code=sm_XX` 生成的 fatbin 里没有可 JIT 的 PTX。这意味着即使在 `all` 模式下，产物也**只能**在这 4 个架构上运行，无法在列表之外的未来 GPU 上即时编译（运行会报 no kernel image 一类错误）。这也与 README "SM90 and above" 的要求一致。
- **`auto` 需要能看到 GPU**：在无 GPU 的构建容器里 `auto` 会直接抛 `RuntimeError`，报错信息明确建议改用 `all`（[setup.py:38-42](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L38-L42)）。
- 架构越多编译越久：`all` 是 4 份 cubin，nvcc 需要对每个目标各做一轮代码生成与 ptxas。

#### 4.2.3 源码精读

**（a）支持列表。** [setup.py:19](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L19)：

```python
SUPPORTED_CUDA_ARCHS = ["90a", "100a", "103a", "120a"]
```

**（b）GPU 探测。** [setup.py:22-29](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L22-L29)：`detect_cuda_arch()` 延迟 `import torch`，用 `torch.cuda.is_available()` 判断有无设备，再用 `torch.cuda.get_device_capability()` 拿 `(major, minor)` 拼成 `f"{major}{minor}a"`——例如 H100/H20 返回 `(9, 0)` → `"90a"`。注意**只探测 `current_device()` 一块**：多卡异构机器上 `auto` 只迁就当前可见的第一块。

**（c）三分支与 `-gencode` 生成。** [setup.py:32-52](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L32-L52)：先断言 `CUDA_HOME` 非空（"PyTorch must be compiled with CUDA support"）；然后按上面流程图三分支得到 `archs` 列表；最后循环拼接：

```python
flags.extend(["-gencode", f"arch=compute_{arch},code=sm_{arch}"])
```

这一对参数会原样进入 nvcc 命令行（拼接点在 [setup.py:70-83](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L70-L83) 的 `*get_arch_flags()`）。

**（d）README 的对应说明。** [README.md:22-28](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L22-L28)：默认探测当前设备；打 wheel 或 CI 用 `FLASH_KDA_CUDA_ARCHS=all`；也支持 `90a,100a` 形式的列表。

#### 4.2.4 代码实践：看见真实的 `-gencode`

1. **实践目标**：把 4.2.2 的流程图与真实编译命令对上号。
2. **操作步骤**（在一台装好 CUDA 版 PyTorch 的机器上）：
   ```bash
   cd flash-kda
   # 列表模式：从安装日志里抓出 nvcc 实际收到的 -gencode
   FLASH_KDA_CUDA_ARCHS=90a,100a pip install -v --no-build-isolation . 2>&1 \
       | tee /tmp/build_list.log | grep -o -- '-gencode arch=compute_[0-9a-z]*,code=sm_[0-9a-z]*'
   ```
   再在**无 GPU** 环境（如 CPU-only 的 docker 容器）里执行默认 `auto` 的安装，观察报错。
3. **需要观察的现象**：列表模式下 grep 应打出 `arch=compute_90a,code=sm_90a` 与 `arch=compute_100a,code=sm_100a` 各一行；无 GPU 时 `auto` 触发 `RuntimeError: FLASH_KDA_CUDA_ARCHS=auto requires a visible CUDA device...`。
4. **预期结果**：见上；具体日志措辞随 setuptools/nvcc 版本略有差异，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么架构号都带 `a` 后缀？如果改成 `90` 会怎样？

**答案**：`a` 表示 architecture-specific 目标，只有它才暴露 TMA/wgmma/cluster 等 SM90 专有指令。FlashKDA 的 kernel 直接使用这些指令，面向不带 `a` 的目标编译无法得到可用的代码。

**练习 2**：在无 GPU 的 CI 容器里构建，应该设什么？

**答案**：`FLASH_KDA_CUDA_ARCHS=all`（或显式列表）。`auto` 依赖 `torch.cuda.is_available()`，无设备时直接抛错（[setup.py:38-42](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L38-L42)）。

**练习 3**：`FLASH_KDA_CUDA_ARCHS=all` 编出来的包能在一块 sm_80 的 A100 上 `import flash_kda` 并调用 `fwd` 吗？

**答案**：能 import，但调用会失败。fatbin 里只有 `sm_90a/100a/103a/120a` 的 cubin，且没有嵌入 PTX，无法 JIT 到 sm_80；README 也明确要求 SM90 及以上。

### 4.3 nvcc 编译选项逐项精读

#### 4.3.1 概念说明

`extra_compile_args` 分成 `'cxx'`（host 编译器，只有 `-O3` 和 `-Wno-psabi`）与 `'nvcc'` 两组。nvcc 这组选项不是随手抄来的模板——其中几项与本项目的**数值设计**（bit-exact 测试）和**剖析工作流**（ncu）直接绑定，后续单元会反复回来引用。

#### 4.3.2 核心流程

完整选项清单在 [setup.py:70-83](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L70-L83)，按下表分类理解：

| 选项 | 类别 | 作用 |
|---|---|---|
| `-O3` | 优化 | host/device 都开最高级别优化 |
| `-U__CUDA_NO_HALF_OPERATORS__`、`-U__CUDA_NO_HALF_CONVERSIONS__`、`-U__CUDA_NO_HALF2_OPERATORS__`、`-U__CUDA_NO_BFLOAT16_CONVERSIONS__` | 语言开关 | PyTorch 的 cpp_extension 默认给 nvcc 注入 `-D__CUDA_NO_HALF_OPERATORS__` 等宏，**禁用** `cuda_fp16.h`/`cuda_bf16.h` 的运算符与转换（避免与 torch C++ API 的历史冲突）。本项目直接使用 CUTLASS/CuTe 的原生 half/bf16 类型，必须用 `-U` 取消这些默认定义 |
| `--expt-relaxed-constexpr` | 语言开关 | 允许 device 代码调用未标 `__device__` 的 `constexpr` host 函数。CuTe 的 layout 元编程几乎全是 constexpr 模板函数，没有它编译不过 |
| `--expt-extended-lambda` | 语言开关 | 允许 `__device__` lambda 表达式 |
| `--use_fast_math` | 精度 | 启用快速数学（近似除法/开方/超越函数、FTZ）。与 kernel 里 `ex2.approx.ftz`、`tanh.approx.f32` 的数值设计配套；exact-match 测试隐含这一假设（u3-l8 展开） |
| `--ptxas-options=-v,--register-usage-level=10,--warn-on-spills` | 性能/可观测 | 见下方逐项说明 |
| `-lineinfo` | 剖析 | 嵌入行号表，几乎不影响代码生成；ncu / Nsight 按源码行归档指标必需（`benchmarks/ncu.sh` 依赖它） |
| `--threads N`（`NVCC_THREADS`，默认 32） | 构建速度 | 让 nvcc 用多线程驱动编译子过程；`all` 模式下要为 4 个架构各生成一份代码，收益更明显 |
| `-gencode ...` | 目标架构 | 见 4.2 |

`--ptxas-options` 三件套：

- **`-v`**：编译日志打印每个 kernel 的寄存器数、栈用量、smem 用量。这是读 CUDA kernel 的第一手工具——不运行任何东西就能知道 K1/K2 各占多少资源。
- **`--register-usage-level=10`**：让 ptxas 倾向"用满寄存器换更少指令"的调度策略。性能优先，但寄存器用得越多，单个 SM 能同时驻留的 block 越少（occupancy 下降），是一种显式取舍。
- **`--warn-on-spills`**：一旦寄存器溢出到 local memory 就告警——对 warp 专用化、寄存器预取环（K2）这类写法，溢出往往是性能悬崖的信号。

occupancy 与寄存器的量化关系（Hopper 每 SM 寄存器文件 65536 个 32 位寄存器、最多 2048 线程）：若想让一个 SM 同时驻留 8 个 256 线程的 block，则每线程寄存器数 \(R\) 需满足

\[
8 \times 256 \times R \le 65536 \quad\Longrightarrow\quad R \le 32 .
\]

K1 正是以 `__launch_bounds__(256, 8)` 表达这个意图（在 u2-l8 展开），而 `--register-usage-level=10` 则是同一权衡在天平另一端的注脚。

#### 4.3.3 源码精读

**（a）host 侧只有两个选项。** [setup.py:69](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L69)：`'cxx': ['-O3', '-Wno-psabi']`。`-Wno-psabi` 压制 GCC 关于 C++ ABI 参数传递变化产生的 note（torch 扩展编译的常见噪音）。

**（b）nvcc 选项与两个动态拼接点。** [setup.py:70-83](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L70-L83)：静态选项之后，`*get_nvcc_thread_args()`（定义在 [setup.py:14-16](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L14-L16)，读 `NVCC_THREADS`，默认 `"32"`）与 `*get_arch_flags()`（4.2 已精读）在**构建时**动态展开——也就是说架构相关选项不在源码里写死，而是每次构建根据环境变量现算。

**（c）最终 `setup()` 调用。** [setup.py:97-105](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L97-L105)：把 `ext_modules`、Python 包 `flash_kda`、`cmdclass={"build_ext": BuildExtension}` 组装起来。`BuildExtension` 负责处理 nvcc 与 host 编译器的参数分发、`-isystem` 修正、 Ninja/Make 的选择等脏活。

#### 4.3.4 代码实践：从编译日志读出两个 kernel 的资源占用

1. **实践目标**：利用 `--ptxas-options=-v` 在不运行代码的情况下，拿到 K1/K2 两个 kernel 的寄存器与 smem 占用，为后续读 kernel 建立资源直觉。
2. **操作步骤**：
   ```bash
   FLASH_KDA_CUDA_ARCHS=auto pip install -v --no-build-isolation . 2>&1 | tee /tmp/build.log
   grep -nE 'ptxas info|Used [0-9]+ registers|_flash_kda_fwd_(prepare|recurrence)' /tmp/build.log | head -40
   ```
3. **需要观察的现象**：日志中应出现 `ptxas info` 段落，含 `_flash_kda_fwd_prepare` 与 `_flash_kda_fwd_recurrence` 两个入口名，各自带 `Used N registers`、smem 字节数等信息。
4. **预期结果**：能摘出两个 kernel 的寄存器数与 smem 字节数并记入笔记（具体数值取决于编译器版本与架构，**待本地验证**）。若出现 `spill` 告警，说明该配置下寄存器吃紧。

#### 4.3.5 小练习与答案

**练习 1**：想让 ncu 剖析时把耗时对应到源码行，依赖哪个编译选项？

**答案**：`-lineinfo`（[setup.py:80](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L80)）。它嵌入行号表且基本不影响代码生成；真正会改变优化的调试选项（如 `-G`）项目并未使用。

**练习 2**：为什么需要那四个 `-U__CUDA_NO_*` 宏？

**答案**：PyTorch 的 cpp_extension 默认给 nvcc 定义这些宏，禁用 CUDA 原生 half/bf16 的运算符与转换。FlashKDA 通过 CUTLASS/CuTe 大量直接使用 `__nv_bfloat16` 运算，所以要 `-U` 取消默认定义后才能编译。

**练习 3**：`--use_fast_math` 与测试有什么隐藏关系？

**答案**：fast math 使超越函数走近似指令（如 `ex2.approx.ftz`），而 kernel 的门控计算正是用这类近似实现的；`tests/torch_ref.py` 要 bit-exact 复刻 kernel 数值，其参考实现同样模拟这些近似。编译选项与参考实现、kernel 三者是一个整体（详见 u3-l8）。

### 4.4 测试入口：tests/test.sh

#### 4.4.1 概念说明

FlashKDA 的正确性标准非常严格：不是"误差小于某个阈值"，而是与逐 FMA 复刻 kernel 数值行为的 torch 参考实现 **bit-exact（`torch.equal`）**（u1-l1 已介绍这一哲学，u3-l9 展开）。`tests/test.sh` 就是把"装好依赖 + 跑正确性测试"压缩成 4 行的一键入口。

#### 4.4.2 核心流程

```bash
set -e                        # 任一步失败立即退出
pip install -e .              # ① 可编辑安装本包（触发编译）
pip install "flash-linear-attention>=0.5.0" matplotlib   # ② 装测试依赖
python tests/test_fwd.py      # ③ 以脚本方式运行正确性测试
```

三个步骤各自的含义：

- **① 可编辑安装（editable）**：`pip install -e .` 同样会走 `setup.py` 编译 CUDA 扩展，但把 `flash_kda` Python 包指向源码目录——之后改 Python 代码立即生效，不需要重装。注意 **CUDA/C++ 部分不会自动重编**：改了 `csrc/` 下的代码后需要重新执行这条命令（或先 `rm -rf build` 清掉缓存再装，否则 setuptools 可能因源文件未变而跳过重编——尤其当你只改了环境变量如 `FLASH_KDA_CUDA_ARCHS` 时，务必清缓存）。
- **② 两个测试依赖**：`flash-linear-attention`（fla）提供 `fused_recurrent_kda` 的 **fp64 金标**与 Triton 版 `chunk_kda` 对比（用于和被替代的 Triton 实现横向比较）；`matplotlib` 用于误差可视化。fla 的导入写在 [tests/test_fwd.py:75](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L75) 的函数体内（`run_fla_gold_reference`），只有真正用到金标时才 import。
- **③ 为什么 `python tests/test_fwd.py` 能直接跑**：脚本第一方导入 `from torch_ref import torch_ref`（[tests/test_fwd.py:6](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L6)）引用的是**同目录**的 `tests/torch_ref.py`。以 `python 路径/脚本.py` 方式运行时，Python 会把脚本所在目录（`tests/`）放进 `sys.path[0]`，因此这个"裸模块名"导入可以解析。如果换成在仓库根目录跑 `pytest tests/test_fwd.py`，`tests/` 不一定在 `sys.path` 里，导入方式就可能不同——这正是 test.sh 选择 `python tests/...` 这种朴素运行方式的原因之一。

#### 4.4.3 源码精读

**（a）一键测试脚本全文。** [tests/test.sh:1-4](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test.sh#L1-L4)：即上面流程图的三步，`set -e` 保证失败即停。

**（b）README 的 Tests 节。** [README.md:70-76](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L70-L76)：入口就是 `bash tests/test.sh`，并注明 `tests/test_fwd.py` 是"对照 torch 参考的 exact match 正确性测试，并与 flash-linear-attention 对比"。

**（c）全量扫描的姊妹脚本。** [tests/run_test_full.sh:1-4](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/run_test_full.sh#L1-L4)：`cd tests` 后用 pytest + xdist 以 `-n 16` 起 16 个并行 worker 跑 `test_fwd_full.py`，并设 `FLASH_KDA_DIST_GPU=1`——这个环境变量由 `tests/conftest.py` 用来给每个 worker 分配不同 GPU（多卡回归的正确姿势，u3-l9 精读）。注意它先 `pip install pytest pytest-xdist`，说明全量脚本假定你已经用 `test.sh` 或手动装好了本包。

#### 4.4.4 代码实践：跑通第一次一键测试

1. **实践目标**：完成从源码到"正确性验证通过"的闭环，拿到第一份 exact match 输出。
2. **操作步骤**：
   ```bash
   cd flash-kda
   bash tests/test.sh 2>&1 | tee /tmp/test.log
   tail -30 /tmp/test.log
   ```
3. **需要观察的现象**：日志依次出现 ①扩展编译与安装（可看到 nvcc 命令行）、②fla 与 matplotlib 安装、③`test_fwd.py` 的各测试用例输出（exact match 的 PASS、与 fla 对比的误差统计等）。
4. **预期结果**：所有 exact match 用例通过（`torch.equal` 为 True）；与 fla 的对比输出相对误差统计（fla 走的是不同数值路径，允许有非零小误差）。具体输出格式与用例数量**待本地验证**。
5. 若在无 GPU 机器上执行，会在导入/运行阶段因 `torch.cuda.is_available()` 为 False 而失败——构建（编译）本身不需要 GPU，但运行测试需要。

#### 4.4.5 小练习与答案

**练习 1**：`test.sh` 为什么要安装 `flash-linear-attention` 和 `matplotlib`？

**答案**：fla 提供 fp64 的 `fused_recurrent_kda` 金标参考和被替代的 Triton `chunk_kda`，用于横向对比（[tests/test_fwd.py:75](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L75)）；matplotlib 用于绘制误差曲线。它们只在测试里用，所以不放进 `setup.py` 的运行时依赖。

**练习 2**：`pip install -e .` 之后，改了 `flash_kda/__init__.py` 要重装吗？改了 `csrc/smxx/fwd_kernel2.cuh` 呢？

**答案**：改 Python 文件不需要重装（editable 安装直接指向源码目录）；改 `csrc/` 下的 CUDA 代码需要重新执行 `pip install -e .` 触发重编（建议先 `rm -rf build` 清缓存）。

**练习 3**：`test.sh` 用 `python tests/test_fwd.py` 而不是 `pytest`，为什么 `from torch_ref import torch_ref` 能成功？

**答案**：`python 脚本.py` 会把脚本所在目录 `tests/` 作为 `sys.path[0]`，同目录模块 `torch_ref` 可以按裸名导入（[tests/test_fwd.py:6](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L6)）。`run_test_full.sh` 则通过先 `cd tests` 再跑 pytest 解决同一问题。

### 4.5 clangd 开发环境：setup_clangd.sh

#### 4.5.1 概念说明

CuTe 代码里类型名动辄几百个字符，没有 IDE 跳转几乎没法读。clangd 是 C/C++ 语言服务器，但它用的是 **clang** 的编译器语法，不认识 nvcc 专属选项（`-gencode`、`-Xcudafe` 等），也不知道 CUDA 头文件在哪。本仓库提供三件套解决这件事：

- `.clangd.template`：项目级配置**模板**，含占位符 `__REPO_ROOT__`；
- `setup_clangd.sh`：把模板中的占位符替换成仓库绝对路径，生成 `.clangd`；再把 `config.yaml` 拷到 `~/.config/clangd/` 作为全局配置；
- `config.yaml`：告诉 clangd 如何"伪装成 nvcc"编译 `.cu`/`.cuh`。

#### 4.5.2 核心流程

```
setup_clangd.sh
  ├─ sed 's|__REPO_ROOT__|<绝对路径>|g' .clangd.template > .clangd   （项目内生效）
  └─ cp config.yaml ~/.config/clangd/                                 （用户级全局生效）

clangd 打开 csrc/**/*.cuh
  └─ 项目 .clangd 注入 4 个 -I（与 setup.py 的 include_dirs 一致）
  └─ 全局 config.yaml 的 PathMatch: .*\.cuh 段追加 --cuda-path、--cuda-gpu-arch=sm_90、
     -D__CUDA_ARCH__=900 等 clang 风格 CUDA 编译选项，并 Remove nvcc 专属 flags
```

#### 4.5.3 源码精读

**（a）生成脚本。** [setup_clangd.sh:1-9](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup_clangd.sh#L1-L9)：`REPO_ROOT` 用 `BASH_SOURCE` 定位自身目录（因此从任何 cwd 运行都正确）；`sed` 替换生成 `.clangd`；最后把 `config.yaml` 复制到 `~/.config/clangd/`。

**（b）模板与 include 目录的对应。** [.clangd.template:1-6](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/.clangd.template#L1-L6) 注入的 4 个 include 路径（`cutlass/include`、`cutlass/tools/util/include`、`cutlass/examples/common`、`csrc`）与 [setup.py:62-67](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L62-L67) 的 `include_dirs` **一一对应**——真实编译和 IDE 解析看到同一套头文件。

**（c）全局配置的三段结构。** [config.yaml:1-13](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/config.yaml#L1-L13) 是全局段：取消错误数上限、关闭 Hover 的 AKA、抑制 `variadic_device_fn` 等误报。[config.yaml:14-45](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/config.yaml#L14-L45) 是 `PathMatch: .*\.cuh` 段（`.cu` 段结构相同）：追加 `--cuda-path=/usr/local/cuda`、`--cuda-gpu-arch=sm_90`、`-xcuda`，并**伪造**一批宏让 CUTLASS 的条件编译走到 SM90 分支——`-D__INTELLISENSE__`、`-D__CLANGD__`（让库内"编辑器模式"分支生效）、`-D__CUDA_ARCH__=900`、`-DCUTLASS_ARCH_MMA_SM90_SUPPORTED=1` 等；同时 `Remove` 掉 `-gencode*`、`-Xcudafe*`、`--expt-*` 等 clangd 无法理解的 nvcc 选项。注意该配置假定 CUDA 装在 `/usr/local/cuda` 且目标是 sm_90——如果本机路径不同需要相应修改。

README 的 Development 节（[README.md:111-119](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L111-L119)）说明的正是这套流程。

#### 4.5.4 代码实践：配好 IDE 再读源码（无需 GPU）

1. **实践目标**：让编辑器能对 `csrc/` 下的 CUDA 源码做定义跳转，为后续单元的源码精读准备工具。
2. **操作步骤**：
   ```bash
   cd flash-kda
   bash setup_clangd.sh
   cat .clangd        # 确认 __REPO_ROOT__ 已被替换为绝对路径
   ls ~/.config/clangd/config.yaml
   ```
   然后用支持 clangd 的编辑器（VS Code + clangd 插件等）打开仓库，跳到 `csrc/smxx/fwd_kernel1.cuh` 里任意一个 CuTe 类型上尝试"转到定义"。
3. **需要观察的现象**：`cat .clangd` 输出中的 include 路径是本机绝对路径；编辑器中 `cute::Tensor` 等符号能跳到 `cutlass/include/cute/` 下的定义；无大量 unknown type 报错。
4. **预期结果**：跳转可用。若全部标红，最常见原因是 submodule 未拉取（头文件不存在）或 CUDA 不在 `/usr/local/cuda`。此步骤纯本地配置，不依赖 GPU。

#### 4.5.5 小练习与答案

**练习 1**：`.clangd.template` 的 4 个 `-I` 和 `setup.py` 的什么部分对应？为什么要保持一致？

**答案**：与 `CUDAExtension` 的 `include_dirs`（[setup.py:62-67](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L62-L67)）一一对应。一致才能保证 IDE 解析到的头文件与真实编译用的是同一套，跳转和补全才可信。

**练习 2**：`config.yaml` 为什么要 `Remove: -gencode*` 这一批选项？

**答案**：clangd 后端是 clang，不认识 nvcc 专属选项；不删除会报 unknown argument，导致整个编译数据库解析失败。删除后再以 clang 风格补上 `--cuda-gpu-arch=sm_90` 等价物。

**练习 3**：`__INTELLISENSE__` / `__CLANGD__` 这类伪造宏的作用是什么？

**答案**：许多 CUDA 库（含 CUTLASS）会用这类宏区分"被真实编译器编译"与"被编辑器分析"，从而在编辑器路径上绕过编译器特有的扩展。定义它们能让 clangd 走到可解析的分支，减少误报。

## 5. 综合实践

把本讲三个核心模块（构建骨架、架构选择、测试入口）串成一次完整的从零到验证流程。**需要一台 SM90 及以上、装有 CUDA 版 PyTorch（≥2.4）与 CUDA 12.9+ 的机器**：

1. **从零构建（auto 模式）**：
   ```bash
   git clone https://github.com/MoonshotAI/FlashKDA.git flash-kda && cd flash-kda
   git submodule update --init --recursive
   pip install -v --no-build-isolation . 2>&1 | tee /tmp/build_auto.log   # FLASH_KDA_CUDA_ARCHS 未设置，等效 auto
   ```
2. **跑通一键测试**：`bash tests/test.sh 2>&1 | tee /tmp/test.log`，确认 exact match 用例全部通过。
3. **改用 all 模式重编**（先清掉产物与缓存，确保真的重编）：
   ```bash
   pip uninstall -y flash_kda
   rm -rf build
   FLASH_KDA_CUDA_ARCHS=all pip install -v --no-build-isolation . 2>&1 | tee /tmp/build_all.log
   ```
4. **对比两种产物的目标架构**：
   ```bash
   SO=$(find . -name 'flash_kda_C*.so' | head -1)     # editable/常规安装后一般位于 build/ 产物目录下，路径待本地确认
   ${CUDA_HOME:-/usr/local/cuda}/bin/cuobjdump --list-elf "$SO" | sort | uniq -c
   ```
5. **记录一张对照表**（模板）：

   | 构建模式 | 编译耗时（约） | cuobjdump 中的 ELF 架构 | 测试结果 |
   |---|---|---|---|
   | `auto`（本机 SM90） |  | 应只出现 `.elf.sm_90a` |  |
   | `all` |  | 应出现 `sm_90a / sm_100a / sm_103a / sm_120a` 各一份 |  |

6. **思考题**（写在同一份笔记里）：为什么 `all` 模式编译时间大约是 `auto` 的数倍？如果本机是 H20（sm_90a），`all` 产物运行时驱动加载的是哪一份 cubin？

预期结果：auto 产物只含本机架构的 cubin；all 产物含全部四个架构；两种模式下 `tests/test_fwd.py` 均应通过（`all` 模式验证了多架构编译路径没有破坏本机行为）。以上命令输出**待本地验证**。

## 6. 本讲小结

- FlashKDA 的构建完全由一个约 100 行的 `setup.py` 驱动：第 7 行自动拉取 CUTLASS submodule，`CUDAExtension('flash_kda_C', ...)` 只编译 `csrc/flash_kda.cpp` 与 `csrc/smxx/fwd_launch.cu` 两个单元——所有 kernel 以 `.cuh` 实现头的形式汇入后者。
- `FLASH_KDA_CUDA_ARCHS` 支持 `auto / all / 列表` 三种模式；`auto` 探测当前 GPU，无 GPU 时报错并提示用 `all`；目标架构必须是带 `a` 后缀的 `90a/100a/103a/120a`，因为 TMA/GMMA 等指令只在 arch-specific 目标下可用；产物只嵌 cubin、不嵌 PTX。
- nvcc 选项各有分工：四个 `-U__CUDA_NO_*` 恢复原生 half/bf16 运算符；`--expt-relaxed-constexpr` 支撑 CuTe 元编程；`--use_fast_math` 与 kernel 的近似指令数值设计绑定；`--ptxas-options=-v` 提供免费的资源占用报告；`-lineinfo` 服务 ncu 剖析。
- `bash tests/test.sh` 三步走：可编辑安装 → 装 fla 与 matplotlib → 以脚本方式运行 exact match 测试；改 C++ 代码后需重装（必要时先 `rm -rf build`）。
- `setup_clangd.sh` 通过"模板替换项目路径 + 全局 config.yaml 伪装 nvcc"两步，让 clangd 能解析 CuTe 代码，其 include 路径与 `setup.py` 严格一致。

## 7. 下一步学习建议

构建环境已经就绪，下一讲 **u1-l4「项目地图」**将带你画出从 `flash_kda.fwd` 到两个 CUDA kernel 的完整调用链与数据流图，弄清 `flash_kda.cpp`、`fwd_launch.cu`、`fwd_kernel1.cuh`、`fwd_kernel2.cuh`、`utils.cuh` 各自的职责边界。之后再进入 **u1-l5**亲手完成第一次 `flash_kda.fwd` 调用。如果你打算精读源码，建议现在就先跑一遍 `bash setup_clangd.sh` 把跳转配好；阅读时可以随时回看本讲 4.3 的资源占用实践——用 `--ptxas-options=-v` 的日志数据（寄存器数、smem 字节数）为每一讲的 kernel 建立一张"资源档案"。
