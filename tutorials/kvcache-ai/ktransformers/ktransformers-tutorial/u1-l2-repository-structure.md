# 仓库目录结构与代码组织

## 1. 本讲目标

学完本讲后，你应该能够：

- 画出 KTransformers 仓库顶层到二级的目录树，并标注每个目录的职责。
- 说清 `archive/`（旧一体化框架）和 `kt-kernel/`（新运行时）之间的关系，理解「为什么旧代码要归档、新代码集中在一处」。
- 给定一个需求（找推理 Python 包、找 C++ 算子、找 CUDA 算子、找 CLI、找微调代码、找构建脚本），能立刻定位到正确的目录。
- 看懂 `kt-kernel/pyproject.toml` 如何把源码目录 `python/` 映射成安装后的 Python 包 `kt_kernel`。

本讲承接 [u1-l1](./u1-l1-project-overview.md)：上一讲建立了「KTransformers = CPU-GPU 异构推理 + SFT 微调」的整体认知，本讲带你走进仓库内部，把那张能力地图落到具体的目录上。

## 2. 前置知识

- **仓库（repository）**：一个项目用 git 管理的全部文件。学习新项目第一步永远是「先看目录结构，再读代码」。
- **顶层包 / 子项目**：一个 git 仓库里可以同时存在多个独立的「可安装项目」。KTransformers 仓库的顶层是一个轻量包，真正的运行时在 `kt-kernel/` 子目录里。
- **shim（垫片）**：一个只做「转发」的薄薄一层代码。顶层 `ktransformers.py` 就是 shim，`import ktransformers` 实际加载的是 `kt-kernel` 提供的能力。
- **Python 包目录映射**：源码在磁盘上的文件夹叫法和 `pip install` 之后 `import` 的包名可以不一样，靠 `pyproject.toml` 里的 `package-dir` 配置做映射。例如源码目录 `python/` 安装后变成 `kt_kernel` 包。
- **归档（archive）**：把旧版本代码整体挪到一个独立目录保留下来，方便追溯历史，但不再维护、不再参与新流程。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md) | 仓库顶层主文档，指明两条能力都来自 `kt-kernel` 源码树 |
| [kt-kernel/README.md](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/README.md) | 推理子项目文档，列出支持的后端与安装方式 |
| [kt-kernel/pyproject.toml](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/pyproject.toml) | 定义 `kt-kernel` 包、`kt` 命令入口点、Python 包目录映射 |
| [ktransformers.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/ktransformers.py) | 顶层 shim 模块，转发到 `kt_kernel` |
| [setup.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/setup.py) | 顶层安装脚本，`install_requires` 指向 `kt-kernel` |
| [archive/README.md](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/archive/README.md) | 旧框架文档，记录「kt-kernel + kt-sft 两模块」的历史形态 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**顶层目录**、**kt-kernel 子目录**、**归档目录说明**。

### 4.1 顶层目录

#### 4.1.1 概念说明

站在仓库根目录往下看，KTransformers 仓库的顶层是「一套文档 + 一个轻量 shim + 一个真正的运行时子项目」的组合。它的特殊之处在于：顶层并不直接包含推理/训练的运行时代码，运行时全部集中在 `kt-kernel/` 子目录里。顶层的 `ktransformers.py`、`setup.py`、`pyproject.toml` 只是「门面」，把 `pip install ktransformers` 这个动作转发到 `kt-kernel`。

这种设计的好处是：用户习惯的包名 `ktransformers` 保持不变，但内核可以独立演进、独立发版（`kt-kernel` 在 PyPI 上有自己的 wheel）。

#### 4.1.2 顶层目录树

下面是仓库顶层（到二级）的目录结构，根据实际 `git ls-files` / `ls` 结果整理：

```
ktransformers/                       # 仓库根目录
├── README.md / README_ZH.md         # 项目主文档（英/中）
├── LICENSE                          # Apache-2.0 许可证
├── MAINTAINERS.md                   # 维护者名单
├── MANIFEST.in                      # 打包清单
├── version.py                       # 统一版本号（当前 0.6.3.post1）
├── ktransformers.py                 # 顶层 shim 模块（转发到 kt_kernel）
├── setup.py                         # 顶层安装脚本（install_requires -> kt-kernel）
├── pyproject.toml                   # 顶层包元数据
├── install.sh                       # 顶层一键安装脚本
├── book.toml                        # mdBook 在线文档配置
├── .gitmodules                      # 顶层 git 子模块声明
│
├── kt-kernel/                       # 【核心】推理/训练运行时（本讲重点）
├── archive/                         # 旧一体化框架（已归档，不再演进）
├── doc/                             # 用户文档（en/ zh/ assets/ basic/）
├── docker/                          # 打包/发布用 Dockerfile 与脚本
├── third_party/                     # 顶层 git 子模块依赖
└── .github/                         # CI/CD 工作流、Issue/PR 模板
```

#### 4.1.3 源码精读

顶层 [README.md](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L14-L16) 的 Overview 一段点明了仓库的定位与代码来源：

> KTransformers is a research project focused on efficient inference and fine-tuning of large language models through CPU-GPU heterogeneous computing. The project now exposes two user-facing capabilities **from the kt-kernel source tree**: [Inference](./kt-kernel/README.md) and [SFT](./doc/en/SFT/KTransformers-Fine-Tuning_Quick-Start.md).

[README.md:14-16](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L14-L16) 说明：推理（Inference）和微调（SFT）这两条面向用户的能力，**都来自 `kt-kernel` 源码树**。这正是「运行时集中在 `kt-kernel/`」的最权威依据。

顶层的门面由三个小文件构成。首先 [setup.py:15-28](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/setup.py#L15-L28) 把顶层包的安装依赖直接指向 `kt-kernel`，并提供两个可选 extras：

```python
setup(
    version=_v,
    install_requires=[
        f"kt-kernel=={_v}",          # 装顶层包 = 装 kt-kernel
    ],
    extras_require={
        "sft": ["transformers-kt==...", "accelerate-kt==..."],
        "sglang": [f"sglang-kt=={_v}"],
    },
)
```

其次 [ktransformers.py:27-32](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/ktransformers.py#L27-L32) 里的 `has_sft_support()` 通过尝试 `import kt_kernel.sft` 来探测微调能力是否可用：

```python
def has_sft_support() -> bool:
    try:
        import kt_kernel.sft  # noqa: F401
    except Exception:
        return False
    return True
```

注意它 import 的是 `kt_kernel`（带下划线），这正是 `kt-kernel` 安装后注册的 Python 包名。版本号则统一写在 [version.py:6](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/version.py#L6)，顶层包与 `kt-kernel` 共享同一个 `__version__ = "0.6.3.post1"`。

> **小提示**：`kt-kernel`（包发布名，带连字符）和 `kt_kernel`（Python 导入名，带下划线）是同一个东西的两种写法，PyPI 上用连字符，`import` 时用下划线，别被绕晕。

### 4.2 kt-kernel 子目录

#### 4.2.1 概念说明

`kt-kernel/` 是整个仓库的「心脏」，所有推理与训练的运行时代码都在这里。它本身是一个独立可安装的 Python 包（`pip install kt-kernel` 或 `cd kt-kernel && pip install .`），同时也是一个包含 C++/CUDA 源码的混合项目。理解 `kt-kernel` 的内部目录，就等于拿到了整个项目的导航图。

它的内部可以按「语言层」和「职责层」两条线划分：

- **按语言层**：Python 代码在 `python/`，C++ CPU 算子在 `operators/`，CUDA GPU 算子在 `cuda/`，CPU 线程运行时在 `cpu_backend/`，两者之间的桥梁是 `ext_bindings.cpp`。
- **按职责层**：推理 API、CLI、微调（SFT）、权重转换脚本、基准、示例、测试各占一个目录。

#### 4.2.2 kt-kernel 目录树

下面是 `kt-kernel/` 内部到二级的目录结构（依据实际文件清单整理）：

```
kt-kernel/
├── README.md / README_zh.md        # 推理子项目文档（英/中）
├── pyproject.toml                  # kt-kernel 包定义 + kt 命令入口点
├── setup.py                        # kt-kernel 安装脚本
├── requirements.txt                # Python 依赖清单
├── pytest.ini                      # 测试配置
│
├── CMakeLists.txt                  # C++/CUDA 构建主文件
├── CMakePresets.json               # CMake 预设（多变体构建）
├── cmake/                          # DetectCPU.cmake / FindSIMD.cmake（指令集探测）
├── install.sh / autosetup.sh       # 源码安装脚本
├── .clang-format                   # C++ 代码格式化配置
│
├── ext_bindings.cpp                # 【桥梁】pybind11：把 C++ 算子暴露给 Python
│
├── python/                         # 【Python 包】源码目录，安装后映射为 kt_kernel
│   ├── __init__.py / _cpu_detect.py / experts.py / experts_base.py
│   ├── utils/                      # 各推理后端封装（amx / llamafile / moe_kernel / loader）
│   ├── sft/                        # 微调子系统（wrapper / arch / amx / lora / autograd ...）
│   └── cli/                        # kt 命令行（commands / config / utils / completions）
│
├── operators/                      # 【C++ CPU 算子】MoE / MLA / rope / kvcache 实现
│   ├── amx/                        # AMX 后端（含 la/ 低层 kernel、test/ 验证）
│   ├── avx2/                       # AVX2 回退后端
│   ├── llamafile/                  # Llamafile 通用 CPU 后端（moe/mlp/linear/mla）
│   ├── moe_kernel/                 # 通用矩阵库后端（api/ la/ mat_kernel/）
│   ├── kvcache/                    # 分页 KV 缓存算子
│   └── *.hpp                       # 公共头：rope / softmax / rms-norm / tp / reduce ...
│
├── cpu_backend/                    # CPU 运行时（线程池/任务队列）
│   └── worker_pool.{h,cpp} / task_queue.{h,cpp} / cpuinfer.h
│
├── cuda/                           # 【CUDA GPU 算子】MoE 路由 / 权重反量化
│   ├── binding.cpp                 # CUDA pybind 绑定（KTransformersOps 模块）
│   ├── moe/                        # top-k softmax 专家路由核
│   ├── gptq_marlin/                # GPTQ-Marlin 反量化核（GPU 热专家）
│   └── custom_gguf/                # GGUF（Q2_K~Q8_K/IQ）反量化核
│
├── scripts/                        # 权重转换/量化脚本（convert_cpu_weights.py 等）
├── bench/                          # Python 性能基准（bench_moe*.py）
├── examples/                       # 端到端用法示例（test_moe_amx.py 等）
├── demo/                           # C++ 单 kernel 验证程序（simple_test.cpp）
├── test/                           # 测试套件（per_commit/ + ci/ + run_suite.py）
└── third_party/                    # kt-kernel 本地子模块（pybind11 / llama.cpp）
```

#### 4.2.3 关键定位速查表

| 你想找的东西 | 去哪个目录 |
|--------------|-----------|
| 推理 Python 包（`kt_kernel`） | `kt-kernel/python/`（安装后映射为 `kt_kernel`） |
| 推理后端封装（AMX/Llamafile/通用 MoE） | `kt-kernel/python/utils/` |
| 微调（SFT）代码 | `kt-kernel/python/sft/` |
| `kt` 命令行 | `kt-kernel/python/cli/` |
| C++ CPU 算子 | `kt-kernel/operators/`（按指令集分 `amx/avx2/llamafile/...`） |
| CUDA GPU 算子 | `kt-kernel/cuda/`（`moe/`、`gptq_marlin/`、`custom_gguf/`） |
| C++↔Python 桥梁 | `kt-kernel/ext_bindings.cpp` |
| CPU 线程池运行时 | `kt-kernel/cpu_backend/` |
| 构建脚本 | `kt-kernel/CMakeLists.txt`、`cmake/`、`install.sh` |
| 权重量化脚本 | `kt-kernel/scripts/` |
| 基准/示例/测试 | `kt-kernel/bench/`、`examples/`、`test/` |

#### 4.2.4 源码精读：python/ 如何变成 kt_kernel

`kt-kernel/python/` 在磁盘上叫 `python`，但 `import` 时却要写成 `kt_kernel`，这个映射在 [kt-kernel/pyproject.toml](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/pyproject.toml) 里完成。包名与描述见 [pyproject.toml:6-12](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/pyproject.toml#L6-L12)：

```toml
[project]
name = "kt-kernel"
description = "KT-Kernel: High-performance kernel operations for KTransformers (AMX/AVX/KML optimizations)"
```

`kt` 命令的入口点定义在 [pyproject.toml:49-50](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/pyproject.toml#L49-L50)，它指向 `kt_kernel.cli.main:main`：

```toml
[project.scripts]
kt = "kt_kernel.cli.main:main"
```

源码目录到 Python 包名的映射在 [pyproject.toml:55-76](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/pyproject.toml#L55-L76)，这里同时声明了「有哪些包」和「每个包对应磁盘哪个目录」：

```toml
[tool.setuptools]
packages = [
  "kt_kernel",                 # 对应 python/
  "kt_kernel.utils",           # 对应 python/utils/
  "kt_kernel.sft",             # 对应 python/sft/   ← 微调就在这
  "kt_kernel.cli",             # 对应 python/cli/   ← 命令行在这
  "kt_kernel.cli.commands",
  "kt_kernel.cli.config",
  "kt_kernel.cli.utils",
  "kt_kernel.cli.completions",
]

[tool.setuptools.package-dir]
kt_kernel = "python"           # 源码目录 -> 包名 的核心映射
"kt_kernel.utils" = "python/utils"
"kt_kernel.sft" = "python/sft"
"kt_kernel.cli" = "python/cli"
...
```

所以结论很清晰：

- `import kt_kernel` → 来自 `kt-kernel/python/__init__.py`
- `import kt_kernel.sft` → 来自 `kt-kernel/python/sft/`（这也是 `has_sft_support()` 探测的目标）
- `kt` 命令 → 来自 `kt-kernel/python/cli/main.py`

推理子项目自身的能力描述，见 [kt-kernel/README.md:1-3](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/README.md#L1-L3)：

> # KT-Kernel
>
> High-performance kernel operations for KTransformers, featuring CPU-optimized MoE inference with AMX, AVX, KML and blis (amd library) support.

它列出的「AMX / AVX / KML / blis」四类 CPU 后端，分别对应 `operators/amx`、`operators/avx2`、`operators/moe_kernel`（KML/矩阵库）、`operators/llamafile`（含 blis 路径）这四个目录。

### 4.3 归档目录说明

#### 4.3.1 概念说明

`archive/` 里是 KTransformers 早期「一体化框架」的全部代码——那时推理、服务、模型定义、SFT 都揉在一个巨大的 `ktransformers/` 包里，根目录还带 `Makefile`、`Dockerfile`、`install.sh` 等一整套。随着项目演进，团队把运行时精简重写成了现在的 `kt-kernel`，于是把旧框架整体挪到 `archive/` 保留下来，便于追溯历史，但**它不再参与新流程、不再被顶层安装**。

理解 `archive/` 的存在有两个作用：第一，解释了仓库「为什么顶层看起来这么干净」——因为旧的庞杂结构被搬走了；第二，当你看到网上较早的 KTransformers 教程引用 `from ktransformers import ...` 或 `ktransformers/operators/...` 这类路径时，要意识到那些代码现在大多在 `archive/` 里，新代码请到 `kt-kernel/` 找。

#### 4.3.2 archive 目录概览

`archive/` 自带一个完整的旧项目骨架（仅列关键部分）：

```
archive/
├── README.md / README_LEGACY.md   # 旧版文档（标注 Legacy）
├── ktransformers/                 # 【旧的】一体化 Python 包（models / operators / server ...）
├── kt-sft/                        # 旧的独立微调子项目（csrc/ + ktransformers/）
├── csrc/                          # 旧的 C++ 扩展源码（balance_serve / custom_marlin / ktransformers_ext）
├── setup.py / pyproject.toml      # 旧的安装脚本
├── Dockerfile / Makefile          # 旧的构建/镜像
└── third_party/                   # 旧的依赖
```

注意 `archive/` 里也有一个 `ktransformers/` 目录和一个 `kt-sft/`，它们和根目录、`kt-kernel/` **不是**一回事——是历史遗留的同名目录，仅供查阅。

#### 4.3.3 源码精读：归档点的历史证据

`archive/` 自己的 [README.md:10-12](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/archive/README.md#L10-L12) 记录了仓库曾经的形态：

> The project has evolved into **two core modules**: kt-kernel and kt-sft.

这说明旧仓库曾经把 `kt-kernel`（推理）和 `kt-sft`（微调）当作两个并列模块；而现在的根目录 [README.md:14-16](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L14-L16) 已经把两条能力都收敛进单一的 `kt-kernel` 源码树。两段 README 的措辞差异，正好见证了「旧一体化框架 → 归档，新运行时集中到 `kt-kernel`」这一结构重组。

> **判别口诀**：看到代码路径以 `archive/` 开头 → 历史遗留，谨慎参考；看到以 `kt-kernel/` 开头 → 当前运行时，放心学习。

### 4.4 代码实践

#### 4.4.1 实践目标

亲手把仓库目录结构「走一遍」，绘制一张到二级的目录树，并验证三个关键定位：推理 Python 包、C++ 算子、CUDA 算子分别在哪儿。

#### 4.4.2 操作步骤

1. **列出顶层结构**。在仓库根目录执行只读 git 命令，看顶层有哪些条目（避免被 `archive/` 里上千个文件刷屏）：

   ```bash
   # 只看顶层目录与文件
   git ls-files | cut -d/ -f1 | sort -u
   ```

   > 若 `cut`/`sort` 不可用，可直接 `ls -F` 查看顶层。

2. **列出 kt-kernel 内部结构**：

   ```bash
   ls -F kt-kernel
   ```

   对照本讲 4.2.2 的目录树，确认 `python/`、`operators/`、`cuda/`、`cpu_backend/`、`scripts/` 等目录都在。

3. **验证 Python 包映射**。打开 [kt-kernel/pyproject.toml](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/pyproject.toml#L68-L76)，找到 `package-dir` 段，确认 `kt_kernel = "python"`。

4. **绘制目录树并标注**。在笔记里画出本讲 4.1.2 与 4.2.2 的两棵树，在每个目录旁用一句话写清职责。

5. **定位三类代码**（这是本实践的核心交付）：
   - 推理 Python 包：`kt-kernel/python/`（`import` 名 `kt_kernel`）。
   - C++ 算子：`kt-kernel/operators/`（再按指令集进 `amx/`、`avx2/`、`llamafile/`、`moe_kernel/`、`kvcache/`）。
   - CUDA 算子：`kt-kernel/cuda/`（`moe/`、`gptq_marlin/`、`custom_gguf/`）。

#### 4.4.3 需要观察的现象

- 顶层 `git ls-files | cut -d/ -f1 | sort -u` 的结果中，应该看到 `archive`、`doc`、`docker`、`kt-kernel`、`third_party`、`.github` 这几个目录，以及若干顶层 `.py`/`.md` 文件——**看不到任何运行时业务代码直接堆在顶层**。
- `kt-kernel/python/` 下确实存在 `__init__.py`、`experts.py`、`experts_base.py`、`_cpu_detect.py` 以及 `utils/`、`sft/`、`cli/` 三个子包。
- `pyproject.toml` 里 `package-dir` 把 `kt_kernel` 指向 `python`，与步骤 3 一致。

#### 4.4.4 预期结果

你应当得到一张清晰的目录树，并能在不翻代码的前提下回答：

| 问题 | 答案 |
|------|------|
| 推理 Python 包在哪个目录？ | `kt-kernel/python/`（包名 `kt_kernel`） |
| C++ CPU 算子在哪个目录？ | `kt-kernel/operators/` |
| CUDA GPU 算子在哪个目录？ | `kt-kernel/cuda/` |
| 微调代码在哪个目录？ | `kt-kernel/python/sft/` |
| `kt` 命令行在哪个目录？ | `kt-kernel/python/cli/` |
| 旧一体化框架在哪？ | `archive/`（已归档，不参与新流程） |

#### 4.4.5 进阶验证（可选）

如果你已按 [u2-l1](./u2-l1-installation-overview.md) 装好 `kt-kernel`，可执行下面的命令验证「源码目录 → 包名」的映射在安装后确实生效：

```bash
python -c "import kt_kernel, kt_kernel.sft; print(kt_kernel.__file__)"
```

预期打印的路径会落在 site-packages 下的 `kt_kernel/__init__.py`，证明 `python/` 目录最终被安装成了 `kt_kernel` 包。

> 若尚未安装环境，本步骤标注为「待本地验证」，仅做源码阅读即可。

### 4.5 小练习与答案

**练习 1**：有人说「`pip install ktransformers` 装的是顶层 `ktransformers.py` 这个文件」，这句话对吗？为什么？

> **答案**：不对。顶层 `ktransformers.py` 只是个 shim，真正的安装依赖在 [setup.py:17-19](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/setup.py#L17-L19) 里写成 `kt-kernel==<version>`，所以装顶层包实际会拉取并安装 `kt-kernel`，运行时来自 `kt-kernel/` 子目录。

**练习 2**：要在代码里 `import` 微调（SFT）相关模块，应该写 `import ktransformers.sft` 还是 `import kt_kernel.sft`？依据是什么？

> **答案**：写 `import kt_kernel.sft`。依据是 [ktransformers.py:29](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/ktransformers.py#L29) 里 `has_sft_support()` 探测的就是 `import kt_kernel.sft`；同时 [pyproject.toml:59,71](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/pyproject.toml#L71) 把 `kt_kernel.sft` 映射到源码目录 `python/sft/`。微调代码物理位置在 `kt-kernel/python/sft/`。

**练习 3**：你想研究 GPU 上「选 top-k 专家」的 CUDA 核函数，应该去哪个目录？为什么不是 `operators/`？

> **答案**：去 `kt-kernel/cuda/moe/`（如 `moe_topk_softmax_kernels.cu`）。`operators/` 是 **CPU 端**算子（AMX/AVX2/llamafile 等），而 GPU/CUDA 算子统一放在 `kt-kernel/cuda/` 下，二者按「CPU vs GPU」分目录隔离。

## 5. 综合实践

**任务：为一位新同事写一份「KTransformers 仓库导航卡」**。

要求：

1. 用你自己的话，画一棵从仓库根目录到 `kt-kernel/` 二级的目录树（可以精简，但必须覆盖 `kt-kernel/` 下的 `python/`、`operators/`、`cuda/`、`cpu_backend/`、`scripts/`、`test/`）。
2. 在树旁边标注：当同事分别问以下 5 件事时，应直接指向哪个目录或文件：
   - 「推理的 Python 入口包在哪？」
   - 「CPU 上 AMX 的 MoE 算子实现在哪？」
   - 「GPU 上 GPTQ 反量化核在哪？」
   - 「`kt run` 命令的实现在哪？」
   - 「CPU 权重量化脚本在哪？」
3. 附一句「避坑提示」：说明遇到 `archive/` 开头的路径该怎么对待。
4. 最后一行写明：顶层 `ktransformers` 与子项目 `kt-kernel` 是什么关系（用 `setup.py` 的 `install_requires` 作为证据）。

完成这张导航卡后，你相当于已经把整个仓库的「骨架」装进了脑子里——后续每读一篇讲义，都可以把新知识挂到这副骨架对应的目录节点上。

## 6. 本讲小结

- KTransformers 仓库顶层是「文档 + shim + 运行时子项目」的组合，运行时代码全部集中在 `kt-kernel/`，顶层 `ktransformers.py`/`setup.py` 只做转发。
- `kt-kernel/` 内部按语言分层：Python 在 `python/`、C++ CPU 算子在 `operators/`、CUDA GPU 算子在 `cuda/`、CPU 线程运行时在 `cpu_backend/`，C++↔Python 桥梁是 `ext_bindings.cpp`。
- 源码目录 `kt-kernel/python/` 经 `pyproject.toml` 的 `package-dir` 映射成安装后的 Python 包 `kt_kernel`，`kt` 命令入口在 `kt_kernel.cli.main:main`。
- 旧的一体化框架整体归档到 `archive/`，不再参与新流程；看到 `archive/` 路径要当作历史遗留谨慎参考。
- 三类代码的快速定位：推理 Python 包 → `kt-kernel/python/`；C++ 算子 → `kt-kernel/operators/`；CUDA 算子 → `kt-kernel/cuda/`。

## 7. 下一步学习建议

- 下一讲 [u1-l3：顶层 shim 包与安装入口](./u1-l3-top-level-package.md) 会深入 `ktransformers.py`、`setup.py` 的 extras（`[sft]`、`[sglang]`）以及 `has_sft_support()` 的检测细节，把本讲的「门面」讲透。
- 之后进入单元 2「构建与安装」，建议先读 [kt-kernel/README.md](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/README.md) 的 Installation 段，对照本讲的 `kt-kernel/CMakeLists.txt` 与 `cmake/` 目录，理解这副骨架如何被编译出来。
- 想提前感受 `kt-kernel/python/` 的代码风格，可以打开 `kt-kernel/python/__init__.py` 和 `experts.py` 浏览，那是后续单元 4「Python 推理 API」的起点。
