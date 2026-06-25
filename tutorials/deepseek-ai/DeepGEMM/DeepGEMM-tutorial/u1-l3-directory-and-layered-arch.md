# 目录结构与分层架构

## 1. 本讲目标

通过本讲，你将能够：

- 看懂 DeepGEMM 仓库的整体目录布局，并说清每个顶层目录的职责。
- 理解「Python 包 / 宿主 C++ / 设备侧 CUDA 头文件」三层各自的位置与分工。
- 建立一条**端到端心智模型**：从一行 `deep_gemm.fp8_fp4_gemm_nt(...)` 的 Python 调用，一路追踪到 GPU tensor core 上真正执行的那个 kernel。

本讲是后续所有源码精读讲义的「地图」。地图清楚了，后面进入 JIT、启发式、设备内核等任何主题都不会迷路。

## 2. 前置知识

在开始前，请确认你已建立以下认知（来自 [u1-l1 项目总览与定位](u1-l1-project-overview.md) 与 [u1-l2 环境要求与构建安装](u1-l2-build-and-install.md)）：

- DeepGEMM 是面向 SM90（Hopper）与 SM100（Blackwell）的高性能张量核 kernel 库。
- 它的核心设计哲学之一是**运行时 JIT 编译**：安装时只编译一个很薄的宿主模块 `_C`，所有重型 CUDA kernel 都推迟到「第一次调用某个形状」时才现场编译、缓存、加载。
- 因此 DeepGEMM 的代码天然分成「**宿主侧（host）**」和「**设备侧（device）**」两套，它们通过 JIT 这座「桥」连接。

两个术语先解释清楚：

- **宿主（host）**：指 CPU 上执行的 C++ 代码，负责接收 Python 传来的张量、做形状校验、选配置、生成代码、调用编译器、装载并启动 kernel。
- **设备（device）**：指 GPU 上执行的 CUDA kernel 代码，真正在 tensor core 上做矩阵乘法。

这一讲的关键就是：**这两套代码分别放在仓库的哪些目录里，又如何串起来。**

## 3. 本讲源码地图

本讲主要涉及三个「枢纽」文件，它们恰好位于三层架构的交接点上：

| 文件 | 所属层 | 作用 |
|------|--------|------|
| [deep_gemm/__init__.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py) | Python 包层 | 导出全部 Python API，并在 import 时调用 `_C.init(...)` 完成库初始化 |
| [csrc/python_api.cpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/python_api.cpp) | 宿主 C++ 入口 | 用 pybind11 注册 `_C` 模块，把各 API 命名空间的函数暴露给 Python |
| [csrc/apis/gemm.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp) | 宿主 API 派发层 | 对 GEMM 做形状/类型校验、变换缩放因子，再按 `arch_major` 派发到具体实现 |

辅助理解端到端流程时还会用到：

| 文件 | 作用 |
|------|------|
| [csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp) | 宿主侧 Runtime 类，负责**代码生成**与**启动**，是「桥」的宿主一端 |
| `deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh` | 设备侧 kernel 实现，是「桥」的设备一端（被 JIT 生成出的 `.cu` 源码 `#include`） |

## 4. 核心概念与源码讲解

### 4.1 目录职责划分

#### 4.1.1 概念说明

一个高性能 GPU kernel 库的代码量通常很大，如果都堆在一起会很难维护。DeepGEMM 用一个非常清晰的目录划分，把「不同生命周期、不同运行位置」的代码分开放。我们把仓库看作一棵职责树，先看顶层有哪些目录，再看每个目录管什么。

#### 4.1.2 核心流程

仓库根目录下的文件与目录可以按「角色」分成五类：

```text
DeepGEMM/
├── README.md            # 项目说明、用法示例、环境变量清单
├── LICENSE              # 许可证（MIT）
├── setup.py             # 构建入口：编译 _C 扩展（见 u1-l2）
├── develop.sh           # 开发者本地构建脚本（见 u1-l2）
├── install.sh           # 用户安装脚本（见 u1-l2）
├── build.sh             # 构建辅助
├── CMakeLists.txt       # 仅供 IDE 索引，不参与真实构建（见 u1-l2）
│
├── deep_gemm/           # ① Python 包层（用户直接 import 的部分）
├── csrc/                # ② 宿主 C++ 层（编译进 _C 扩展）
├── tests/               # ③ 测试与输入生成器
├── scripts/             # ④ 辅助脚本（pyi 生成、性能剖析）
└── third-party/         # ⑤ 第三方依赖（git 子模块）
```

下面对每一类展开。

**① Python 包层 `deep_gemm/`** —— 用户在 Python 里 `import deep_gemm` 时真正拿到的就是这个包。它包含：

| 子项 | 作用 |
|------|------|
| `__init__.py` | 导出全部 API、初始化 `_C`（见 4.3.3） |
| `_C`（编译产物 `.so`） | 由 `csrc/python_api.cpp` 编译而来，是宿主层的入口 |
| `mega/` | Mega MoE 的 Python 侧封装（`SymmBuffer` 等，见 u8） |
| `testing/` | 基准测试（`bench.py`）与数值误差度量（`numeric.py`）工具 |
| `utils/` | 布局工具（`layout.py`）、数学（`math.py`）、分布式（`dist.py`） |
| `legacy/` | 面向 A100 的老版 Triton kernel（不在本手册主线范围） |
| `include/deep_gemm/` | **设备侧 CUDA 头文件**（见 4.2，是 ② 和设备层之间的桥梁） |

> 注意：`deep_gemm/include/` 虽然放在 Python 包目录下，但它**不是 Python 代码**，而是一堆 `.cuh`（CUDA 设备头文件）。把它放进包里是为了方便随 wheel 一起分发，让运行时 JIT 能找到它们（见 u1-l2）。

**② 宿主 C++ 层 `csrc/`** —— 编译进 `_C` 的全部 C++ 代码。它的内部再细分：

| 子目录 | 作用 |
|------|------|
| `python_api.cpp` | pybind11 模块注册入口（见 4.3.2） |
| `apis/` | API 派发层：`gemm`/`attention`/`mega`/`einsum`/`hyperconnection`/`layout`/`runtime`，每个 `.hpp` 一组算子 |
| `jit/` | JIT 基础设施：编译器、缓存、头文件哈希、cubin 装载（见 u3） |
| `jit_kernels/impls/` | 宿主侧 Runtime 类：**代码生成 + 启动**，与设备侧 `impls/` 一一对应（共 18 个文件） |
| `jit_kernels/heuristics/` | 启发式配置选择：根据形状选出最优 block 尺寸等（见 u5） |
| `utils/` | 宿主工具：异常、哈希、格式化、兼容性判定 |
| `indexing/` | FP4 索引器相关（`main.cu`） |

**③ 测试 `tests/`** —— `generators.py` 负责构造各种形状的输入与参考输出，其余 `test_*.py` 是各算子的 pytest 用例（如 `test_fp8_fp4.py`、`test_mega_moe.py`）。

**④ 脚本 `scripts/`** —— `generate_pyi.py`（生成类型存根）、`run_ncu_mega_moe.sh`（NCU 性能剖析）、`quick_plot_pm.py`（绘图）。

**⑤ 第三方 `third-party/`** —— git 子模块：`cutlass`（NVIDIA 的模板库，仅借用概念与少量头）、`fmt`（格式化库）、`tilelang_ops`。

#### 4.1.3 源码精读

`deep_gemm/__init__.py` 的前半部分集中体现了「Python 包层从 `_C` 重新导出全部算子」这一职责：

[deep_gemm/__init__.py:16-26](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L16-L26) —— 这一段先把宿主模块 `_C` 引入，并从它「重新导出」一批全局配置函数（`set_num_sms`、`set_tc_util`、`set_pdl` 等）。这些函数虽然在 C++ 里实现，但对用户来说是 `deep_gemm.xxx` 的 Python 函数。

紧接着，同一文件按算子家族分批重新导出：

[deep_gemm/__init__.py:36-73](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L36-L73) —— 这里用注释把算子分成若干家族（`# FP8 FP4 GEMMs`、`# FP8 GEMMs`、`# BF16 GEMMs`、`# Einsum kernels`、`# Attention kernels`、`# Hyperconnection kernels`、`# Layout kernels`），每一族都从 `_C` 导入。**这正是「算子家族 ↔ Python 接口」对照表的最佳索引**——你想知道 DeepGEMM 到底提供哪些算子，直接看这段注释即可。

注意这段被包在 `try/except ImportError` 里（第 79 行 `pass`）：这是因为当 CUDA 运行时版本过旧时，部分 kernel 不可用，包仍应能 import 成功，只是缺一些函数。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立「算子家族 → 源码文件」的对照能力。
2. **操作步骤**：
   - 打开 [deep_gemm/__init__.py:36-73](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L36-L73)，挑出任意 3 个算子名（例如 `fp8_fp4_gemm_nt`、`m_grouped_fp8_fp4_gemm_nt_masked`、`fp8_fp4_mqa_logits`）。
   - 用编辑器在 `csrc/apis/` 下搜索这三个名字，找出它们各自定义在哪个 `.hpp` 文件里。
3. **需要观察的现象**：你会看到 GEMM 类算子集中在 `csrc/apis/gemm.hpp`，注意力类算子在 `csrc/apis/attention.hpp`，分组（`m_grouped_` / `k_grouped_`）算子与普通算子写在同一个文件里。
4. **预期结果**：你能口述「Python 里一个算子名 → 它的宿主派发函数在哪个 `.hpp`」这条映射，为后续精读做准备。

#### 4.1.5 小练习与答案

**练习 1**：`deep_gemm/include/deep_gemm/` 目录里放的是 Python 代码还是 CUDA 代码？为什么放在 Python 包目录下？

> **答案**：放的是 CUDA 设备头文件（`.cuh`），不是 Python。放在 `deep_gemm/` 包里是为了让它随 wheel 一起分发到安装目录，这样运行时 JIT 编译时能在包目录里找到这些头文件（`#include <deep_gemm/...>`）。

**练习 2**：`csrc/jit_kernels/impls/` 里的 `.hpp` 和 `deep_gemm/include/deep_gemm/impls/` 里的 `.cuh`，哪个在 CPU 上跑、哪个在 GPU 上跑？

> **答案**：前者（`.hpp`）是宿主侧 Runtime 类，在 CPU 上执行，负责代码生成与启动；后者（`.cuh`）是设备侧 kernel 模板，会被 JIT 生成的源码 `#include` 后编译到 GPU 上执行。两者名字相似但运行位置不同，这是分层架构的关键。

---

### 4.2 宿主 / 设备分层

#### 4.2.1 概念说明

为什么 DeepGEMM 要把代码分成「宿主」和「设备」两套，而且各自又有一个 `impls/` 目录？

- **宿主侧（`csrc/`）**解决的问题是：用户给我一对张量，我该用什么 tile 尺寸、要不要用 multicast、怎么把缩放因子排好、怎么把这段 kernel 编译出来并启动。这些是 CPU 上的「调度」工作。
- **设备侧（`deep_gemm/include/deep_gemm/`）**解决的问题是：tile 尺寸、TMA 描述符、WGMMA/UMMA 指令、共享内存布局、线程同步——这些是 GPU 上的「计算」工作。

这两套代码不能直接互相调用，因为它们跑在不同的处理器上。连接它们的机制就是 **JIT**：宿主侧在运行时**生成一段 `.cu` 源码**，这段源码 `#include` 了设备侧的 `.cuh` 模板，并把运行时形状填成编译期常量，然后交给编译器，最终编译出一个 GPU kernel。

#### 4.2.2 核心流程

设备侧头文件目录 `deep_gemm/include/deep_gemm/` 按功能再细分，每个子目录管 kernel 的一个「部件」：

```text
deep_gemm/include/deep_gemm/
├── common/     # 公共基础：types/math/exception/tma_copy/utils/compile
├── ptx/        # PTX 内联汇编封装：tma / wgmma(SM90) / tcgen05(SM100) / ld_st
├── mma/        # 矩阵乘抽象：sm90.cuh(WGMMA) / sm100.cuh(UMMA)
├── scheduler/  # 块调度器：gemm.cuh / mega_moe.cuh / paged_mqa_logits...
├── epilogue/   # 输出回写（epilogue）：store_cd / transform
├── layout/     # 内存布局：mega_moe / sym_buffer / mqa_logits
├── comm/       # 多 SM / 多 rank 同步：barrier（cluster_sync / grid_sync）
└── impls/      # ★ 完整 kernel 实现：sm90_fp8_gemm_1d1d.cuh、sm100_fp8_fp4_mega_moe.cuh 等
```

一个设备 kernel（例如 `impls/sm90_fp8_gemm_1d1d.cuh`）会像搭积木一样用到 `ptx/`、`mma/`、`scheduler/`、`comm/` 里的部件。这些部件之间的依赖关系构成了「设备层内部」的分层。

对应地，宿主侧 `csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp` 里的 Runtime 类，**名字和设备侧的 `impls/sm90_fp8_gemm_1d1d.cuh` 一一对应**，只是扩展名从 `.cuh`（设备模板）换成 `.hpp`（宿主 Runtime）。这是命名约定，记住它就能在两层之间快速跳转。

整个分层可以抽象成下图（文字版）：

```text
        Python 层（CPU，用户面对）            deep_gemm/__init__.py
                  │
                  ▼  import / _C.xxx()
        宿主入口（CPU）                       csrc/python_api.cpp  → csrc/apis/*.hpp
                  │
                  ▼  代码生成 + 编译 + 装载
        JIT 桥（CPU↔GPU）                     csrc/jit/*  +  csrc/jit_kernels/*
                  │
                  ▼  cuLaunchKernel
        设备层（GPU）                         deep_gemm/include/deep_gemm/*.cuh
```

#### 4.2.3 源码精读

宿主侧 Runtime 类如何「include」设备侧模板？看 `generate_impl` 函数：

[csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp:33-64](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L33-L64) —— 这段用 `fmt::format` 拼出一段 `.cu` 源码字符串。注意第 35 行：

```cpp
#include <deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh>
```

这就是「桥」的体现——宿主侧 `sm90_fp8_gemm_1d1d.hpp` 在运行时生成了一段源码，而这段源码 `#include` 的正是设备侧的 `deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh`。紧接着的 `__instantiate_kernel` 把运行时的 `BLOCK_M/N/K`、`num_stages`、`num_sms` 等填进模板参数（`{}, {}, {}, ...` 占位符由第 53-63 行的 `args.gemm_config.layout.block_m` 等填入），从而把「运行时形状」固化成「编译期常量」。

而它所在类的定义：

[csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp:15](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L15) —— `class SM90FP8Gemm1D1DRuntime final: public LaunchRuntime<SM90FP8Gemm1D1DRuntime>`。这个 `LaunchRuntime` 基类（定义在 `csrc/jit_kernels/impls/runtime_utils.hpp`）统一了所有 kernel 的「生成 → 缓存查找 → 编译 → 启动」流程，子类只需提供 `generate_impl` 和 `launch_impl` 两个钩子。这是宿主层复用代码的关键抽象。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：验证「宿主 impls ↔ 设备 impls」命名一一对应关系。
2. **操作步骤**：
   - 列出 `csrc/jit_kernels/impls/` 下所有文件名。
   - 列出 `deep_gemm/include/deep_gemm/impls/` 下所有文件名。
   - 找出能配对的（去掉 `.hpp`/`.cuh` 后缀后同名或近名）。
3. **需要观察的现象**：大多数宿主 `.hpp`（如 `sm90_fp8_gemm_1d1d.hpp`、`sm100_fp8_fp4_mega_moe.hpp`）都能在设备侧找到同名的 `.cuh`；也有少数只属于某一层（例如 `smxx_cublaslt.hpp` 走 cuBLASLt，没有自研设备 kernel）。
4. **预期结果**：得出一份「宿主 Runtime ↔ 设备 kernel」对照表，今后看到任意一侧的文件名都能立刻定位到另一侧。

#### 4.2.5 小练习与答案

**练习 1**：为什么设备侧 kernel 用 `.cuh` 模板而不是直接写成 `.cu` 编译成 `.so`？

> **答案**：因为 tile 尺寸、cluster 大小、流水线级数等需要根据具体形状特化才能榨干性能。如果预编译所有组合会组合爆炸。DeepGEMM 选择把 kernel 写成模板，推迟到运行时按需实例化（JIT），既保留了「编译期常量」带来的性能，又避免了安装时海量编译。

**练习 2**：`scheduler/gemm.cuh`、`mma/sm90.cuh`、`ptx/tma.cuh` 三个文件，按「被依赖 → 依赖」的从底到顶顺序排列。

> **答案**：`ptx/tma.cuh`（最底层，封装硬件 PTX 指令）→ `mma/sm90.cuh`（在其上封装 WGMMA 矩阵乘）→ `scheduler/gemm.cuh`（最高层，负责把输出块分配给各 SM）。一个完整 kernel 会同时 include 这三者。

---

### 4.3 端到端调用数据流

#### 4.3.1 概念说明

把前两节合起来，我们要回答一个完整问题：**当用户写下 `deep_gemm.fp8_fp4_gemm_nt(a, b, d, ...)`，这行代码到底经过了哪些目录、哪些文件，最后才在 GPU 上算出结果？**

这条路径会穿过三层、跨越宿主/设备边界，是理解整个项目最核心的一条「主线」。把它走通，你就掌握了 DeepGEMM 的骨架。

#### 4.3.2 核心流程

下面是一次 `fp8_fp4_gemm_nt` 调用的端到端流程（数字对应下方源码精读的步骤）：

```text
①  Python:  deep_gemm.fp8_fp4_gemm_nt(...)
        │   （函数来自 deep_gemm/__init__.py 的 from ._C import ...）
        ▼
②  宿主入口:  csrc/python_api.cpp 的 PYBIND11_MODULE
        │   调用 deep_gemm::gemm::register_apis(m)
        ▼
③  API 派发:  csrc/apis/gemm.hpp::fp8_fp4_gemm_nt
        │   - 形状/类型/dtype 校验（early_return / check_*）
        │   - 变换缩放因子 SF（layout::transform_sf_pair_into_required_layout）
        │   - 读 device_runtime->get_arch_major() 判断架构
        ▼
④  架构派发:  arch_major == 9  → sm90_fp8_gemm_1d1d(...)
             arch_major == 10 → sm100_fp8_fp4_gemm_1d1d(...)
        ▼
⑤  宿主 Runtime:  csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp
        │   - generate_impl() 生成含 #include <deep_gemm/impls/...cuh> 的 .cu 源码
        │   - JIT 编译 / 缓存 / 装载（csrc/jit/*）
        ▼
⑥  设备 kernel:  deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh
        │   在 GPU tensor core 上执行真正的 FP8 矩阵乘
        ▼
        结果写回张量 d
```

其中 **③ 的架构派发**是全库的总开关：`device_runtime->get_arch_major()` 返回 `9` 还是 `10`，决定了走 SM90 还是 SM100 的一整套实现。这一点 [u1-l1](u1-l1-project-overview.md) 已强调，这里再次看到它的具体落点。

#### 4.3.3 源码精读

**① Python 入口与 `_C` 初始化。**

[deep_gemm/__init__.py:122-125](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L122-L125) —— 这段是 import 阶段的「总装」：调用 `_C.init(...)`，把「库根目录路径」（用于 JIT 找设备头文件）和「CUDA home」（用于 JIT 调用 nvcc/nvrtc）传给宿主层。没有这一步，后面的 JIT 编译就找不到头文件和编译器。函数 `fp8_fp4_gemm_nt` 本身则是第 38 行 `from ._C import ( ... fp8_fp4_gemm_nt ... )` 导出的，对 Python 用户而言它就是一个普通函数。

**② 宿主 pybind 注册。**

[csrc/python_api.cpp:17-28](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/python_api.cpp#L17-L28) —— `PYBIND11_MODULE` 定义了 `_C` 模块。注意第 21-27 行的七行 `deep_gemm::xxx::register_apis(m);`：每行对应 `csrc/apis/` 里的一个 `.hpp`（attention/einsum/hyperconnection/gemm/layout/mega/runtime）。**这就是「Python 函数名 → 宿主命名空间」的接线板**。整个 `_C` 模块只是把这七个命名空间的函数一次性注册出来，不含任何算法逻辑——逻辑都在各自的 `.hpp` 里。

**③+④ API 派发与架构分支。**

[csrc/apis/gemm.hpp:73-124](https://github.com/deepseek-ai-DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L73-L124) —— 这是 `fp8_fp4_gemm_nt` 的完整派发逻辑，可分三段读：

- **校验段**（第 82-103 行）：断言形状是 `[M,K] @ [N,K].T`、C/D 是 N-major、dtype 合法，并调用 `early_return(...)` 处理空问题 / 累加等平凡情形。
- **SF 变换段**（第 106-107 行）：`layout::transform_sf_pair_into_required_layout(...)` 把用户给的缩放因子排成 kernel 所需的 TMA 对齐布局（SM90 是 FP32、SM100 是打包 UE8M0，详见 [u2-l2](u2-l2-scaling-factor-recipe-ue8m0.md)）。
- **架构派发段**（第 110-123 行）：读 `arch_major = device_runtime->get_arch_major()`，`9` 配 FP32 SF 走 `sm90_fp8_gemm_1d1d`，`10` 配打包 SF 走 `sm100_fp8_fp4_gemm_1d1d`，否则 `DG_HOST_UNREACHABLE`。**这正是全库架构开关的落点。**

此外，同一个文件还体现了「转置复用」的派生关系：

[csrc/apis/gemm.hpp:135-136](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L135-L136) —— `fp8_fp4_gemm_nn` 并非独立实现，而是把 `b` 转置后直接调用 `fp8_fp4_gemm_nt`。`tn`/`tt` 同理（见第 148-163 行）。所以 SM90 上即便只实现了 NT 路径，也能通过转置支持全部四种布局（详见 [u2-l1](u2-l1-gemm-naming-and-nt-layout.md)）。**这一点解释了为什么「一个 `nt` 实现撑起四种布局」**。

**⑤→⑥ 宿主 Runtime 接到设备 kernel。**

[csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp:66-67](https://github.com/deepseek-ai-DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L66-L67) —— `launch_impl` 调用 `launch_kernel(kernel, config, ...)`，把前面编译好的 kernel 句柄和参数一起发到 GPU。至此，控制权从 CPU 交到 GPU，设备侧 `deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh` 里的 kernel 开始在 tensor core 上执行（具体内核结构见 [u6-l1](u6-l1-sm90-fp8-gemm-1d1d-entry.md)）。

#### 4.3.4 代码实践（绘图型，本讲核心实践）

这是本讲的主实践任务。

1. **实践目标**：亲手画出 DeepGEMM 的分层架构图，标注 `fp8_fp4_gemm_nt` 从 Python 调用到 GPU kernel 的完整路径。
2. **操作步骤**：
   - 准备纸笔或任意绘图工具。
   - 画三个纵向分区的泳道，分别标注 **Python 层 / 宿主 C++ 层 / 设备 CUDA 层**。
   - 按 4.3.2 的 6 个步骤，在每个泳道里填入对应的**文件路径**（不是函数名）：
     - Python 层：`deep_gemm/__init__.py`
     - 宿主层：`csrc/python_api.cpp` → `csrc/apis/gemm.hpp` → `csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp` →（经 `csrc/jit/`）→
     - 设备层：`deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh`
   - 在跨泳道的「桥」上标注：Python→宿主 是 `pybind11`；宿主→设备 是 `JIT 代码生成 + 编译`。
   - 在宿主层内部用一条注释标出 `get_arch_major()` 在 `gemm.hpp` 里做架构分支的位置。
3. **需要观察的现象**：你会清楚看到 `#include <deep_gemm/impls/...>` 这一行跨越了宿主 `.hpp` 与设备 `.cuh` 的边界——这就是 JIT「桥」在文件层面的体现。
4. **预期结果**：得到一张可长期保存的「DeepGEMM 调用链地图」。今后阅读任何一篇进阶讲义，都能在这张图上定位「我现在在哪一层」。
5. **说明**：本实践为绘图/源码阅读型，无需运行 GPU；若想配合真实运行观察，可在设 `DG_JIT_DEBUG=1` 时运行一次（见 [u3-l1](u3-l1-jit-arch-overview.md)），对照控制台输出的编译信息验证你的路径图。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `csrc/apis/gemm.hpp::fp8_fp4_gemm_nt` 里的 `arch_major == 10` 分支删掉，会发生什么？

> **答案**：在 SM100（Blackwell）上调用时会落入 `else` 分支，触发 `DG_HOST_UNREACHABLE("Unsupported architecture or scaling factor types")` 报错。SM90 路径不受影响。这印证了 `get_arch_major()` 是派发的唯一依据。

**练习 2**：`_C.init(...)`（[deep_gemm/__init__.py:122](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L122)）传进去的两个参数分别给谁用？

> **答案**：第一个是「库根目录路径」，供 JIT 在运行时定位 `deep_gemm/include/` 下的设备头文件（`#include <deep_gemm/...>` 的搜索根）；第二个是「CUDA home」，供 JIT 定位 `nvcc`/`nvrtc` 编译器。两者都是 JIT 桥正常工作的前提。

## 5. 综合实践

把本讲三个模块串成一个综合任务：**为 DeepGEMM 写一份一页纸「架构速查卡」**。

要求包含三部分：

1. **目录职责表**：用一张表列出 `deep_gemm/`、`csrc/`、`tests/`、`scripts/`、`third-party/` 五个顶层目录及其一句话职责，并在 `csrc/` 和 `deep_gemm/include/deep_gemm/` 下各展开一层子目录说明（参考 4.1.2 与 4.2.2）。
2. **三层对照**：画一张「Python 层 / 宿主层 / 设备层」三栏对照表，每栏填入该层最具代表性的一个文件，并写明层与层之间的连接机制（`pybind11` 与 `JIT`）。
3. **调用链追踪**：选 `fp8_fp4_gemm_nt` 这个算子，按「① Python → ② python_api.cpp → ③ apis/gemm.hpp → ⑤ jit_kernels/impls/…hpp → ⑥ include/…/impls/…cuh」写出每一步对应的文件路径与行号引用（直接引用本讲给出的永久链接）。

完成这张速查卡后，你应能不查资料就答出：DeepGEMM 有几层、每层放什么、`arch_major` 在哪一层做开关、JIT 这座「桥」连接了哪两个文件。

## 6. 本讲小结

- DeepGEMM 仓库顶层分为 `deep_gemm/`（Python 包）、`csrc/`（宿主 C++）、`tests/`、`scripts/`、`third-party/` 五类，职责清晰。
- 代码天然分为**三层**：Python 层（用户面对）、宿主 C++ 层（派发 + JIT 基础设施）、设备 CUDA 层（真正的 kernel 模板）。
- `deep_gemm/include/deep_gemm/` 虽在 Python 包下，实为随包分发的设备头文件，按 `common/ptx/mma/scheduler/epilogue/layout/comm/impls` 再细分。
- `csrc/jit_kernels/impls/*.hpp`（宿主 Runtime）与 `deep_gemm/include/deep_gemm/impls/*.cuh`（设备 kernel）**命名一一对应**，是跨层跳转的关键约定。
- 端到端调用链：`__init__.py` → `python_api.cpp`（pybind 注册）→ `apis/gemm.hpp`（校验 + SF 变换 + `get_arch_major()` 派发）→ `jit_kernels/impls/…hpp`（代码生成 + 启动）→ `include/…/impls/…cuh`（GPU 执行）。
- 连接三层与跨宿主/设备边界的两座「桥」分别是 **pybind11**（Python↔C++）和 **JIT 运行时编译**（宿主↔设备）。

## 7. 下一步学习建议

本讲建立了地图，接下来建议按调用链自上而下深入：

- 先读 [u1-l4 Python 接口全貌与第一次调用](u1-l4-python-api-and-first-call.md)，亲手跑通一次最小 GEMM 调用并校验数值。
- 再读 [u2-l3 C++ 绑定与 API 派发层](u2-l3-cpp-binding-and-dispatch.md)，把本讲 4.3 节的 `apis/gemm.hpp` 派发逻辑与 `python_api.cpp` 的 pybind 注册彻底吃透。
- 之后进入 [u3 JIT 编译系统](u3-l1-jit-arch-overview.md)，搞清楚本讲反复提到的「JIT 这座桥」内部是如何编译、缓存、装载的。
- 想直接看 GPU kernel 内部，可跳到 [u6-l1 内核入口：SM90 FP8 GEMM 1D1D](u6-l1-sm90-fp8-gemm-1d1d-entry.md)，但要先有 [u2](u2-l1-gemm-naming-and-nt-layout.md)（布局/缩放因子）与 [u3](u3-l1-jit-arch-overview.md) 的基础。
