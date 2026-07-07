# 环境准备与源码构建安装

> 承接上一讲：你已经知道 FlashMLA 是一组面向 DeepSeek-V3/V3.2 的注意力 CUDA kernel，只支持 SM90（Hopper）与 SM100（Blackwell）。本讲解决下一个问题——**怎么把它在你自己的机器上编译装起来**。

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚「安装 FlashMLA」到底装了哪两样东西（Python 包 + CUDA 扩展）。
- 按正确顺序完成：克隆仓库 → 初始化 CUTLASS 子模块 → `pip install` 编译。
- 读懂 `setup.py`，理解 `CUDAExtension` 的源文件清单、`include` 目录和编译参数是怎么组织的。
- 解释 `get_arch_flags` 为什么在 NVCC < 12.9 时强制要求 `FLASH_MLA_DISABLE_SM100`，以及 `sm_90a` / `sm_100f` 这两条 `-gencode` 的含义。
- 掌握 `FLASH_MLA_DISABLE_SM90 / SM100 / FP16` 这一组「编译期 feature flag」的作用与生效方式。

## 2. 前置知识

本讲是构建安装篇，不涉及 kernel 内部数学。你只需要以下基础：

- **pip + setuptools**：Python 生态最常见的打包/安装方式。`pip install .` 会读取项目根目录的 `setup.py`，执行里面的 `setup(...)` 调用完成安装。
- **git submodule（子模块）**：一个 Git 仓库可以把另一个 Git 仓库「挂」到自己的某个子目录下，叫子模块。子模块的内容不会随主仓库一起 clone 下来，需要单独「初始化」。
- **NVCC**：NVIDIA CUDA C++ 编译器，随 CUDA Toolkit 一起安装。它把 `.cu` 文件编译成 GPU 可执行代码（PTX/SASS）。
- **PyTorch C++ 扩展（`torch.utils.cpp_extension`）**：PyTorch 提供的工具，让你用 C++/CUDA 写一段高性能代码，编译成 `.so` 动态库后，像普通 Python 模块一样 `import` 使用。FlashMLA 的 GPU kernel 就是通过这种方式暴露给 Python 的。
- **gencode（`-gencode arch=...,code=...`）**：告诉 NVCC「为哪种 GPU 架构生成代码」。`sm_90a` 对应 Hopper、`sm_100f` 对应 Blackwell，后缀 `a`/`f` 表示要启用该架构的特定硬件特性（如 WGMMA、TMA）。

> 如果上述概念里有陌生的，本讲会在用到时再结合源码解释一遍，不影响阅读。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| `setup.py` | 唯一的构建入口：声明要安装的 Python 包与 CUDA 扩展、检测 NVCC 版本、生成 gencode 与编译参数 |
| `.gitmodules` | 声明 CUTLASS 子模块（挂载在 `csrc/cutlass`），是编译期的关键依赖 |
| `README.md` | 给出克隆/子模块/安装三步命令，以及硬件与 CUDA 版本要求 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：先看**整体构建链路与 CUTLASS 子模块**（4.1），再钻进 `setup.py` 看 **CUDAExtension 构建入口**（4.2），接着单独讲最关键的 **NVCC 版本检测与 arch gencode**（4.3，本讲的核心实践也在这里），最后是 **feature flag 机制**（4.4）。

### 4.1 整体构建链路与 CUTLASS 子模块

#### 4.1.1 概念说明

「安装 FlashMLA」其实一次性装了**两个东西**：

1. **Python 包 `flash_mla`**：纯 Python 代码，位于 `flash_mla/` 目录（`__init__.py`、`flash_mla_interface.py`），负责对外暴露 `flash_mla_with_kvcache` 等函数、做张量形状校验和参数拼装。
2. **CUDA 扩展 `flash_mla.cuda`**：用 C++/CUDA 写的 GPU kernel，位于 `csrc/` 目录，编译成一个 `.so` 动态库，作为 `flash_mla` 包下的一个子模块被 Python 调用。

这两者通过 PyTorch 的 **pybind** 桥接：Python 端调用 `flash_mla.cuda.xxx`，实际进入 C++ 实现再启动 CUDA kernel。

此外，FlashMLA 的 SM100 dense prefill/backward kernel 大量使用了 NVIDIA 的 **CUTLASS** 模板库。CUTLASS 不是 pip 装的，而是作为 **git 子模块**直接嵌在仓库里（`csrc/cutlass`），编译时以头文件形式 `#include`。所以「初始化子模块」这一步是硬性前提——如果 `csrc/cutlass` 是空的，编译会因为找不到头文件而失败。

#### 4.1.2 核心流程

从零安装的完整链路（与 README 的 Installation 一致）：

```text
git clone <repo>                          # 1. 拉源码（此时 csrc/cutlass 是空目录）
  └─ git submodule update --init --recursive   # 2. 初始化 CUTLASS 子模块
       └─ pip install -v .                 # 3. 触发 setup.py
            ├─ setup.py 顶部自动再跑一次 submodule init（双保险）
            ├─ get_arch_flags()  → 探测 NVCC，生成 -gencode
            ├─ get_features_args() → 读取 feature flag，生成 -D...
            └─ BuildExtension 编译所有 .cu/.cpp → 链接成 flash_mla.cuda.so
                 └─ 安装 flash_mla (Python 包) + flash_mla.cuda (扩展)
```

注意第 1 步 clone 之后，`csrc/cutlass` 默认是**空目录**（这是 git submodule 的标准行为）。必须执行第 2 步才能真正拿到 CUTLASS 源码。

#### 4.1.3 源码精读

子模块的声明在 `.gitmodules`：

[.gitmodules:1-3](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/.gitmodules#L1-L3)
「挂载路径 `csrc/cutlass`，来自 NVIDIA 官方 CUTLASS 仓库。」——这就是为什么必须先初始化子模块。

`setup.py` 在**文件顶部、定义任何函数之外**就直接执行了一次子模块初始化，作为「忘了手动 init」的双保险：

[setup.py:52](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L52)
「`pip install` 一开始就跑 `git submodule update --init csrc/cutlass`。」——这意味着即使你只执行了 `pip install .` 而忘了手动 init 子模块，构建过程也会帮你补上。当然，前提是当前目录是个 git 仓库且能联网拉取子模块。

最终的 `setup(...)` 调用同时声明了上面提到的「两个东西」：

[setup.py:145-151](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L145-L151)
「`packages=find_packages(include=['flash_mla'])` 负责安装纯 Python 包 `flash_mla`；`ext_modules=ext_modules` 负责安装编译出的 CUDA 扩展；`cmdclass={"build_ext": BuildExtension}` 告诉 setuptools 用 PyTorch 的 `BuildExtension` 来编译扩展。」

版本号也值得一提：默认是 `1.0.0` 拼上当前 git 短 hash（如 `1.0.0+9241ae3`），如果取不到 hash 就退化为时间戳，方便你区分本地多次构建的产物。

[setup.py:136-147](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L136-L147)
「用 `git rev-parse --short HEAD` 取短 hash 拼 `+` 后缀；失败则用 `datetime` 时间戳兜底。」

#### 4.1.4 代码实践

**实践目标**：确认你本地的 CUTLASS 子模块状态，理解「clone 后子模块是空的」这一现象。

**操作步骤**（在仓库根目录执行）：

1. 查看子模块登记状态：
   ```bash
   git submodule status
   ```
   开头如果有 `-` 号，表示该子模块尚未初始化；如果是空格或 `+`，表示已检出。
2. 查看子模块目录是否真的有内容：
   ```bash
   ls csrc/cutlass/include/cutlass 2>/dev/null | head
   ```
3. 如果上一步无输出，执行初始化后重看：
   ```bash
   git submodule update --init --recursive
   ls csrc/cutlass/include/cutlass 2>/dev/null | head
   ```

**需要观察的现象**：

- 初始化前：`git submodule status` 行首是 `-`，`ls csrc/cutlass/include/...` 几乎为空。
- 初始化后：行首变成空格/`+`，能看到大量 CUTLASS 头文件（如 `arch/`、`gemm/` 等目录）。

**预期结果**：`csrc/cutlass/include/cutlass/` 下出现真实头文件目录，说明编译期的 `#include <cutlass/...>` 才能被找到。

> 如果你的环境没有 git 或无法联网拉取子模块，此步骤无法完成，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：README 让你执行 `git submodule update --init --recursive`，但 `setup.py` 第 52 行又自动跑了一次 `--init`。这两者重复了吗？为什么不冲突？

> **答案**：不冲突。`git submodule update --init` 对已初始化的子模块是幂等的（相当于 no-op）。手动执行是为了在 `pip install` 之前就保证子模块就绪；setup.py 里那次是「双保险」，针对的是直接 `pip install` 而没手动 init 的用户。

**练习 2**：为什么 FlashMLA 选择把 CUTLASS 作为子模块，而不是写进 `requirements.txt` 让 pip 装？

> **答案**：CUTLASS 是**纯头文件模板库（header-only）**，发布形态就是一堆 `.hpp`，并不存在标准的 pip 包；FlashMLA 还需要**锁定一个特定版本/commit**以保证模板 API 兼容。子模块正好满足「把固定版本的源码嵌进项目、编译期 include」的需求。

---

### 4.2 CUDAExtension 构建入口：源文件清单与编译参数

#### 4.2.1 概念说明

`CUDAExtension` 是 `torch.utils.cpp_extension` 提供的便利类，本质上是给 setuptools 增加一个「把 `.cu`/`.cpp` 用 NVCC/宿主编译器编译并链接成一个 `.so`」的扩展模块。FlashMLA 把它命名为 `flash_mla.cuda`，意味着编译完成后你可以 `from flash_mla.cuda import xxx`（或 `import flash_mla.cuda`）来访问里面的 pybind 绑定。

构造一个 `CUDAExtension` 主要给三样信息：

- `name`：扩展的完整模块名。
- `sources`：参与编译的源文件清单。
- `extra_compile_args`：分 `cxx`（宿主 C++ 编译器）和 `nvcc`（GPU 编译器）两套参数。
- `include_dirs`：头文件搜索路径。

#### 4.2.2 核心流程

```text
sources (一批 .cpp/.cu)
   │
   ├─ *.cpp ──> 宿主 C++ 编译器，用 cxx 参数（-O3 -std=c++20 ...）
   ├─ *.cu  ──> NVCC，用 nvcc 参数（-O3 --use_fast_math -gencode ... ）
   │
   └─ BuildExtension 把所有目标文件链接成 flash_mla.cuda.{cpython-XX-...}.so
        └─ 放进 site-packages，Python 端可 import
```

注意 `cxx` 和 `nvcc` 是两套独立的参数：前者管「与 PyTorch/宿主对接的胶水代码」（如 `api.cpp` 里的 pybind），后者管「真正跑在 GPU 上的 kernel」。

#### 4.2.3 源码精读

整个扩展的构造在一段 `CUDAExtension(...)` 调用里：

[setup.py:62-64](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L62-L64)
「扩展名 `flash_mla.cuda`——这是 Python 端访问 C++ 实现的入口名。」

`sources` 是一份**按 kernel 家族分组、带注释**的清单，覆盖了上一讲提到的四类 kernel 的全部实例化文件：

[setup.py:65-105](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L65-L105)
「从上到下依次是：API 层（`csrc/api/api.cpp`）、decode 辅助 kernel（调度元数据 `get_decoding_sched_meta.cu`、split-KV 归并 `combine.cu`）、SM90 dense decode、SM90 sparse decode（FP8）、SM90 sparse prefill、SM100 dense prefill/backward、SM100 sparse prefill、SM100 sparse decode。」——你会发现源文件路径本身就编码了「架构（sm90/sm100/smxx）× 阶段（decode/prefill）× 稀疏性（dense/sparse）」的分类，这正是上一讲支持矩阵在代码里的落点。

`include_dirs` 决定了 `#include` 能找到哪些头文件：

[setup.py:126-132](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L126-L132)
「包含 `csrc`（自有头文件）、`csrc/kerutils/include`（公共工具，注释标注 `TODO Remove me` 说明正在迁移）、`csrc/sm90`、以及 CUTLASS 的 `include` 和 `tools/util/include`。」

NVCC 编译参数分两部分：一组「通用 GPU 编译选项」 + 由函数动态生成的「feature/arch/threads 参数」：

[setup.py:108-124](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L108-L124)
「静态部分包括 `-O3`、`-std=c++20`、几个 `-U__CUDA_NO_HALF*`（取消禁用，从而启用 fp16/bf16 运算符）、`--use_fast_math`、一组 `--ptxas-options`（控制寄存器用量与告警）以及 `-lineinfo`/`--source-in-ptx`（便于 profiling 定位源码行）；动态部分是 `get_features_args() + get_arch_flags() + get_nvcc_thread_args()`，分别在 4.4 和 4.3 讲。」

其中 `--ptxas-options` 里的 `--register-usage-level=10`、`--warn-on-spills`、`--warn-on-local-memory-usage` 是性能调优关键（后面 u8-l3 会专门讲），现在只需知道：它们让编译器**尽量少用寄存器**（避免 spills 到 local memory 拖慢 kernel），并在出现 spill 时告警。

C++ 宿主侧参数在 `cxx_args`，按平台区分：

[setup.py:56-59](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L56-L59)
「Linux 用 `-O3 -std=c++20 -DNDEBUG -Wno-deprecated-declarations`，Windows 用 MSVC 等价参数。」

#### 4.2.4 代码实践

**实践目标**：通过把 `sources` 清单映射到 kernel 家族，验证你对仓库结构的理解（为下一讲 u1-l3 的目录结构打基础）。

**操作步骤**：

1. 打开 [setup.py:65-105](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L65-L105)。
2. 为每一条 `.cu`/`.cpp` 来源，按下表填写「架构 / 阶段 / 稀疏性」三列。第一行作为示例已填好：

   | 源文件 | 架构 | 阶段 | 稀疏性 |
   | :--- | :---: | :---: | :---: |
   | `csrc/api/api.cpp` | 通用 | 通用 | 通用（pybind 入口） |
   | `csrc/sm90/decode/dense/instantiations/bf16.cu` | ? | ? | ? |
   | `csrc/sm90/decode/sparse_fp8/instantiations/v32_persistent_h128.cu` | ? | ? | ? |
   | `csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu` | ? | ? | ? |
   | `csrc/sm100/decode/head64/instantiations/model1.cu` | ? | ? | ? |

3. 对照 README 的支持矩阵核对你的判断。

**需要观察的现象**：路径里的 `sm90`/`sm100`、`decode`/`prefill`、`dense`/`sparse(_fp8)`/`sparse` 段落与三列是否一一对应。

**预期结果**（参考答案）：

| 源文件 | 架构 | 阶段 | 稀疏性 |
   | :--- | :---: | :---: | :---: |
   | `csrc/sm90/decode/dense/instantiations/bf16.cu` | SM90 | decode | dense |
   | `csrc/sm90/decode/sparse_fp8/instantiations/v32_persistent_h128.cu` | SM90 | decode | sparse(FP8) |
   | `csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu` | SM100 | prefill | dense(反向) |
   | `csrc/sm100/decode/head64/instantiations/model1.cu` | SM100 | decode | sparse(FP8) |

#### 4.2.5 小练习与答案

**练习 1**：扩展名为什么是 `flash_mla.cuda` 而不是 `flash_mla_cuda`？

> **答案**：`.` 在 Python 的模块系统里表示「包的子模块」。`flash_mla.cuda` 表示它属于 `flash_mla` 这个包，安装后可以用 `from flash_mla.cuda import ...` 访问，命名空间整洁。下划线版本则是一个独立顶层模块，不符合 FlashMLA 的包组织方式。

**练习 2**：为什么 `extra_compile_args` 要把 `cxx` 和 `nvcc` 分开？

> **答案**：`.cpp` 由宿主编译器（g++/MSVC）编译，参数如 `-O3 -std=c++20`；`.cu` 由 NVCC 编译，需要额外的 GPU 专用参数（`--use_fast_math`、`-gencode`、`--ptxas-options` 等）。分开传才能让各自的编译器拿到合法、匹配的选项。

---

### 4.3 NVCC 版本检测与 arch gencode

> 这是本讲的核心模块，也是规格指定的主实践任务所在。

#### 4.3.1 概念说明

`-gencode arch=compute_XX,code=sm_XX` 告诉 NVCC「为哪一代 GPU 生成可执行代码」。FlashMLA 只关心两条：

| gencode | 含义 | 对应 GPU | 需要 NVCC |
| :--- | :--- | :--- | :--- |
| `arch=compute_90a,code=sm_90a` | Hopper 架构，启用架构特定指令（WGMMA、TMA） | H100 / H800 | CUDA 12.x |
| `arch=compute_100f,code=sm_100f` | Blackwell 架构 | B200 | **CUDA 12.9+** |

后缀 `a`（Hopper）表示「architecture-specific」，即必须用 `sm_90a` 才能发射 WGMMA/TMA 这些 Hopper 专有指令；`sm_90`（无 a）只能用普通指令，对 FlashMLA 这种榨干 Tensor Core 的 kernel 来说是不够的。

**关键约束**：Blackwell 的 `sm_100f` 目标**只在 CUDA 12.9 及以上的 NVCC 里才被支持**。CUDA 12.8 的 NVCC 根本不认识 `sm_100f`，强行编译会直接报错。这就是为什么 `setup.py` 要先探测 NVCC 版本。

#### 4.3.2 核心流程

`get_arch_flags()` 的执行流程：

```text
1. 取 CUDA_HOME（断言非空，否则说明 PyTorch 没带 CUDA 支持）
2. 跑 `nvcc --version`，解析出 NVCC 的 major.minor
3. 读环境变量 FLASH_MLA_DISABLE_SM100 / FLASH_MLA_DISABLE_SM90
4. 若 NVCC <= 12.8：
       assert DISABLE_SM100 == True   ← 强制要求关掉 SM100，否则直接报错中止
5. 根据 flag 拼装 arch_flags：
       若未 DISABLE_SM100 → 追加 -gencode arch=compute_100f,code=sm_100f
       若未 DISABLE_SM90  → 追加 -gencode arch=compute_90a,code=sm_90a
6. 返回 arch_flags（注入到 nvcc 编译参数）
```

第 4 步是核心：**NVCC 版本不够时，不是「自动跳过 SM100」，而是「强制你显式声明放弃 SM100」**。这是一种「把环境不匹配暴露为显式错误」的设计——逼着用户意识到自己只能编译 SM90 那部分。

#### 4.3.3 源码精读

[setup.py:25-46](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L25-L46)
「`get_arch_flags()` 全貌：探测 NVCC 版本 → 读 flag → 断言 → 拼 gencode。」

逐段看：

[setup.py:28-34](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L28-L34)
「断言 `CUDA_HOME` 非空；执行 `nvcc --version`，从形如 `release 12.8,` 的输出里解析出 `major=12, minor=8`。」——注意这里用的 `CUDA_HOME` 来自 `torch.utils.cpp_extension` 的同名变量，注释提醒它**不一定**来自你设置的 `CUDA_HOME` 环境变量，而是 PyTorch 内部解析得到的值。

[setup.py:36-39](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L36-L39)
「读两个 DISABLE flag；若 NVCC 版本满足 `major < 12 or (major == 12 and minor <= 8)`（即 ≤ 12.8），则 `assert DISABLE_SM100`，必须设了 `FLASH_MLA_DISABLE_SM100` 才能继续，否则抛出明确错误信息。」

注意边界：条件是 `minor <= 8`，所以 **12.8 也会触发断言**——只有 12.9 及以上才能免除断言、自动启用 SM100。这正好对应 README 说的「CUDA 12.8 及以上是基线，但 SM100 kernel 需要 CUDA 12.9+」。

[setup.py:41-46](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L41-L46)
「按 flag 拼 gencode：未禁用 SM100 则加 `compute_100f/sm_100f`，未禁用 SM90 则加 `compute_90a/sm_90a`。」——两条 gencode 互相独立，因此同一份编译产物可以**同时**包含 SM90 与 SM100 两套代码，由运行时架构检测决定调哪一套（这部分在 u2-l3 讲）。

#### 4.3.4 代码实践

**实践目标**（本讲主任务）：读懂 `get_arch_flags`，解释「NVCC < 12.9 时为何必须 `FLASH_MLA_DISABLE_SM100`」，并针对**你当前环境**的 NVCC 版本列出会启用的 `-gencode`。

**操作步骤**：

1. **查你本机的 NVCC 版本**：
   ```bash
   nvcc --version
   ```
   关注输出里 `release X.Y,` 这一行，记下 `X.Y`。
2. **解读断言**：打开 [setup.py:38-39](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L38-L39)，结合步骤 1 的版本回答：
   - 你的 NVCC 是否触发断言（`X.Y <= 12.8`）？
   - 如果触发，你必须 `export FLASH_MLA_DISABLE_SM100=1` 才能编译；否则 `pip install` 会在这一行直接 `AssertionError`。
3. **列出会启用的 `-gencode`**：根据 [setup.py:41-46](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L41-L46) 与你设置的 flag，写出最终 `arch_flags` 列表。

**需要观察的现象 / 预期结果**：分三种典型情况（用你的实测版本对号入座）：

| 你的 NVCC | `FLASH_MLA_DISABLE_SM100` | 结果 |
| :---: | :---: | :--- |
| **12.9 及以上**（如 12.9、13.0） | 未设置 | 不触发断言；启用 **两条** gencode：`compute_100f/sm_100f` 与 `compute_90a/sm_90a` |
| **12.9 及以上** | 设为 `1` | 不触发断言；只启用 `compute_90a/sm_90a` |
| **12.8 及以下**（如 12.8、12.6） | 未设置 | **触发断言，编译中止**，报错要求你设 `FLASH_MLA_DISABLE_SM100=1` |
| **12.8 及以下** | 设为 `1` | 通过断言；只启用 `compute_90a/sm_90a` |

> **为什么 NVCC < 12.9 必须禁用 SM100？** 因为 `sm_100f`（Blackwell）的代码生成目标直到 **CUDA 12.9** 才被加入 NVCC。12.8 及更早的 NVCC 编译器根本不认识 `arch=compute_100f`，强行传入会直接报「unsupported architecture」。FlashMLA 用 `assert` 把这个「必然失败」提前成一条清晰、可操作的人话错误，而不是让用户在一堆 NVCC 原始报错里挣扎。

**关于「当前环境」的说明**：本讲义生成环境未实际执行 `nvcc --version`，无法给出确定版本号——请你以步骤 1 的实测结果为准。如果你所在的机器**没有 GPU 或没装 CUDA Toolkit**（`nvcc: command not found`），则本实践无法完整运行，标注为**待本地验证**。此时可退化为「源码阅读型实践」：仅依据 [setup.py:38-46](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L38-L46) 推演「给定 NVCC=X.Y + 某 flag，会启用哪些 gencode」即可。

#### 4.3.5 小练习与答案

**练习 1**：假设你的 NVCC 是 12.8、GPU 是 B200（SM100）。你能编译出能用的 SM100 kernel 吗？

> **答案**：不能。NVCC 12.8 不支持 `sm_100f` 目标，你必须升级到 CUDA 12.9+，否则即使设了 `FLASH_MLA_DISABLE_SM100=1` 也只是「跳过 SM100、只编译 SM90」，得到的产物在 B200 上跑不了 SM100 kernel（只能 fallback 或报错）。换言之，这个 flag 解决的是「让旧 NVCC 也能成功编译 SM90 部分」，而不是「让旧 NVCC 也能编译 SM100」。

**练习 2**：`sm_90a` 末尾的 `a` 去掉写成 `sm_90` 会怎样？

> **答案**：`sm_90`（无 `a`）不启用 Hopper 架构特定指令，WGMMA（warpgroup MMA）和 TMA 等 Hopper 专有特性都用不了。FlashMLA 的 SM90 kernel 高度依赖这些指令（seesaw 调度、TMA 流水都建立在 WGMMA/TMA 之上），所以必须用 `sm_90a` 才能正确编译并发挥性能。

---

### 4.4 feature flag 机制

#### 4.4.1 概念说明

除了架构开关，`setup.py` 还提供了一组**编译期 feature flag**——通过环境变量在编译时打开/关闭某些功能。它的原理很简单：把一个 `-DXXX` 宏定义传给编译器，C++ 源码里的 `#ifdef XXX` / CMake 式的条件编译就会走不同分支。

目前定义的 flag 有：

| 环境变量 | 作用 | 生成的宏 |
| :--- | :--- | :--- |
| `FLASH_MLA_DISABLE_FP16` | 编译时去掉 fp16 路径（只保留 bf16） | `-DFLASH_MLA_DISABLE_FP16` |
| `FLASH_MLA_DISABLE_SM90` | 不编译 SM90 gencode（见 4.3） | （控制 gencode，不生成宏） |
| `FLASH_MLA_DISABLE_SM100` | 不编译 SM100 gencode（见 4.3） | （控制 gencode，不生成宏） |
| `NVCC_THREADS` | NVCC 并行编译线程数，默认 32 | （转为 `--threads N`） |

注意区分两类机制：

- `FLASH_MLA_DISABLE_FP16` 是**纯 `-D` 宏**，影响源码内部的 `#ifdef`。
- `FLASH_MLA_DISABLE_SM90/SM100` 是**架构开关**，影响 4.3 的 `arch_flags`，并不产生宏。
- `NVCC_THREADS` 是**编译速度调优**，影响 NVCC 并行编译源文件的线程数。

#### 4.4.2 核心流程

```text
is_flag_set(name)            # 读环境变量，TRUE/1/y/yes 视为真（不区分大小写）
        │
        ▼
get_features_args()          # 把为真的 FP16 之类的开关拼成 ["-DFLASH_MLA_DISABLE_FP16", ...]
        │
        ▼
注入 extra_compile_args
   ├─ cxx  = cxx_args + get_features_args()
   └─ nvcc = [静态选项...] + get_features_args() + get_arch_flags() + get_nvcc_thread_args()
```

注意 `get_features_args()` 的结果**同时**注入到 `cxx` 和 `nvcc` 两套参数——因为同一个宏可能既在宿主 C++ 代码、又在 GPU kernel 代码里被 `#ifdef`。

#### 4.4.3 源码精读

`is_flag_set` 是所有 flag 读取的底层：

[setup.py:16-17](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L16-L17)
「读环境变量，默认 `FALSE`；取值（小写）落在 `true/1/y/yes` 之一即为真。」——所以 `FLASH_MLA_DISABLE_SM100=1`、`=yes`、`=TRUE` 都等价。

`get_features_args` 目前只处理 FP16 一个宏开关：

[setup.py:19-23](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L19-L23)
「若 `FLASH_MLA_DISABLE_FP16` 为真，追加 `-DFLASH_MLA_DISABLE_FP16`。」——这个宏会被 `csrc/sm90/decode/dense/instantiations/fp16.cu` 之类的实例化文件用 `#ifdef` 保护，从而在编译产物里排除 fp16 路径、缩短编译时间/减小产物体积。

SM90/SM100 的开关在 `get_arch_flags` 里读取（4.3 已讲）：

[setup.py:36-37](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L36-L37)
「读两个架构 DISABLE flag。」

`NVCC_THREADS` 控制编译并行度：

[setup.py:48-50](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L48-L50)
「读 `NVCC_THREADS`，默认 32，转为 NVCC 的 `--threads N`。」

最后，三个动态函数一起拼进 nvcc 参数：

[setup.py:124](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L124)
「`+ get_features_args() + get_arch_flags() + get_nvcc_thread_args()`——feature 宏、架构 gencode、编译线程数三者拼接进 NVCC 调用。」

#### 4.4.4 代码实践

**实践目标**：给定一组环境变量，预测 `setup.py` 会生成怎样的编译参数（无需真编译，纯源码推演）。

**操作步骤**：

1. 假设你执行：
   ```bash
   export FLASH_MLA_DISABLE_SM100=1
   export FLASH_MLA_DISABLE_FP16=yes
   pip install -v .
   ```
2. 回答以下三问（依据 [setup.py:16-50](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L16-L50)）：
   - `get_arch_flags()` 会返回什么？（是否会触发断言取决于你的 NVCC 版本，分 NVCC≥12.9 与 NVCC≤12.8 两种情况回答）
   - `get_features_args()` 会返回什么？
   - 最终 nvcc 参数里会出现哪些 `-D` 和 `-gencode`？

**需要观察的现象**：把三问的答案写成最终的「nvcc 关键参数片段」。

**预期结果**：

- `get_features_args()` → `["-DFLASH_MLA_DISABLE_FP16"]`（因为 `yes` 为真）。
- `get_arch_flags()`：
  - 若 NVCC ≤ 12.8：`DISABLE_SM100` 已设、断言通过；`DISABLE_SM90` 未设 → 返回 `["-gencode", "arch=compute_90a,code=sm_90a"]`。
  - 若 NVCC ≥ 12.9：同样 `DISABLE_SM100` 为真 → 不加 sm_100f；`DISABLE_SM90` 未设 → 返回 `["-gencode", "arch=compute_90a,code=sm_90a"]`。（两种情况结果一致，因为这里主动禁了 SM100。）
- 最终 nvcc 参数包含：`-DFLASH_MLA_DISABLE_FP16` 和 `-gencode arch=compute_90a,code=sm_90a`，**没有** `sm_100f`。

> 若你无法运行 `pip install`（无 GPU/CUDA），本实践退化为「读源码推演」即可，结论同样有效——**待本地验证**的是「实际编译是否成功」，而非推演逻辑。

#### 4.4.5 小练习与答案

**练习 1**：`FLASH_MLA_DISABLE_FP16=True`（首字母大写 T）能生效吗？

> **答案**：能。`is_flag_set` 会 `.lower()` 后再比较，`True` → `true`，命中真值集合。所以 `TRUE/True/true/1/y/yes/YES`（任意大小写）都生效。

**练习 2**：为什么 `get_features_args()` 同时被加进 `cxx` 和 `nvcc` 两套参数，而 `get_arch_flags()` 只进 `nvcc`？

> **答案**：feature 宏（`-D...`）可能同时被宿主 C++ 代码和 GPU kernel 代码 `#ifdef`，所以两边都要；而 `-gencode` 是「告诉 NVCC 为哪种 GPU 生成代码」，只对 GPU 编译器（NVCC）有意义，宿主 C++ 编译器不认这个参数，所以只进 `nvcc`。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「为指定环境规划一次 FlashMLA 构建」的纸上演练。

**场景**：你拿到一台机器，已知：

- GPU：H800（SM90），**没有** Blackwell。
- 已安装 CUDA Toolkit，但版本未知。
- 你只关心 dense decode（bf16）和 sparse decode（FP8），完全不需要 fp16 dense decode。

**任务**：写出你将执行的完整命令序列，并预测编译产物包含哪些 gencode / 宏。步骤：

1. 查 NVCC 版本：`nvcc --version`。假设结果是 `release 12.8,`。
2. 决定是否设 `FLASH_MLA_DISABLE_SM100`：依据 [setup.py:38-39](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L38-L39)，NVCC 12.8 必须禁用 SM100。
3. 决定是否设 `FLASH_MLA_DISABLE_FP16`：你不需要 fp16，可以设以加速编译。
4. 写出命令并预测 gencode/宏。

**参考答案**：

```bash
git clone https://github.com/deepseek-ai/FlashMLA.git flash-mla
cd flash-mla
git submodule update --init --recursive          # 拿到 CUTLASS
export FLASH_MLA_DISABLE_SM100=1                  # NVCC 12.8 不支持 sm_100f
export FLASH_MLA_DISABLE_FP16=1                   # 不需要 fp16 dense decode
pip install -v .
```

预测：

- `get_arch_flags()` 通过断言（因为 `DISABLE_SM100` 为真），返回 `["-gencode", "arch=compute_90a,code=sm_90a"]`——**只有 SM90**，正好匹配你的 H800。
- `get_features_args()` 返回 `["-DFLASH_MLA_DISABLE_FP16"]`——编译时跳过 fp16 路径。
- 编译产物 `flash_mla.cuda.so` 只含 SM90 代码、不含 fp16 dense decode，体积更小、编译更快，但功能上完全满足 dense decode(bf16) + sparse decode(FP8) 的需求。

> 若本机无 CUDA/GPU，上述命令无法实际执行，**待本地验证**；但「版本→flag→gencode」的推演链是确定的，可作为你拿到真实机器时的操作清单。

## 6. 本讲小结

- 安装 FlashMLA 会同时产出**两个东西**：纯 Python 包 `flash_mla` 与 CUDA 扩展 `flash_mla.cuda`，后者由 `setup.py` 里的 `CUDAExtension` 编译。
- 编译前必须初始化 **CUTLASS 子模块**（`csrc/cutlass`）；`setup.py` 顶部会自动再 `--init` 一次作为双保险。
- `setup.py` 用 `nvcc --version` 探测版本；**NVCC ≤ 12.8 时必须设 `FLASH_MLA_DISABLE_SM100`**，否则在断言处中止——因为 `sm_100f`（Blackwell）目标只在 CUDA 12.9+ 的 NVCC 里才被支持。
- 架构 gencode：`sm_90a`（Hopper，含 WGMMA/TMA 专有指令）与 `sm_100f`（Blackwell）互相独立，可同时编译进同一个产物。
- `FLASH_MLA_DISABLE_FP16` 是 `-D` 宏开关（同时进 cxx/nvcc）；`SM90/SM100` 是 gencode 开关（只进 nvcc）；`NVCC_THREADS` 控制编译并行度。

## 7. 下一步学习建议

本讲让你能「装起来」。下一步：

- **u1-l3 仓库目录结构与代码组织**：把本讲 `sources` 清单里看到的 `sm90/sm100/smxx × decode/prefill × dense/sparse` 路径规律，与真实目录树对应起来，建立全局代码空间感。
- **u1-l4 Python 接口与最小运行示例**：装好后第一次 `import flash_mla` 并跑通最小解码示例，验证你的构建产物可用。
- 想深入构建细节的话，可以继续阅读 `setup.py` 里 `--ptxas-options`、`--use_fast_math` 等 NVCC 性能 flag 的作用（u8-l3 会系统讲解它们对 kernel 性能与稳定性的影响）。
