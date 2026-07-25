# TileKernels 项目定位与价值

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标不写一行算子代码，而是帮你建立「**这个项目是什么、解决什么问题、技术栈怎么分层**」的心智模型。学完后你应当能够：

- 用一句话说清 TileKernels 是什么、面向 LLM 的哪类工作负载。
- 列出 README 中给出的算子类别，并区分「底层 kernel 家族」和「高层 modeling 封装」。
- 画出 TileKernels → TileLang → CUDA Toolkit → GPU 硬件（SM90/SM100）的依赖层次。
- 看懂 `pyproject.toml` 中声明的依赖与版本要求，并能据此判断自己的环境是否满足运行条件。

掌握这些，后面读任何一篇算子讲义都不会迷路。

## 2. 前置知识

本讲假设你了解以下基础概念（不熟悉也没关系，下面会用通俗的话再点一遍）：

- **LLM（大语言模型）**：像 Transformer 这类模型。LLM 在训练和推理时会大量调用「矩阵乘、归一化、量化、路由」等算子，这些算子的速度直接决定模型快不快。
- **GPU kernel（核函数）**：跑在 GPU 上的一段并行计算程序。一个高效的 kernel 能让同一个数学操作快上几倍到几十倍。
- **算子（operator）**：一个具体的数学运算单元，比如「转置」「量化」「Top-k 选择」。TileKernels 就是一堆高性能算子的集合。
- **Python 包与依赖**：一个 Python 项目通常用 `pyproject.toml` 声明「我叫什么、依赖哪些库、支持哪些 Python 版本」。`pip` 会读它来安装。
- **CUDA / GPU 架构**：CUDA 是 NVIDIA GPU 的并行计算平台；SM（Streaming Multiprocessor，流多处理器）是 GPU 的计算单元。NVIDIA 给每代 GPU 一个「SM 版本号」，例如 SM90（Hopper 架构，如 H100）、SM100（Blackwell 架构）。不同架构的硬件特性（如 TMA、新的数据类型）会影响 kernel 写法。

> 一句话直觉：**TileKernels = 用 TileLang 这门 DSL 写的一堆「逼近硬件极限」的 GPU 算子，专门服务大模型的训练和推理。** 本讲就是要把这个直觉拆开讲清楚。

## 3. 本讲源码地图

本讲只看两个项目级文件，它们决定了「项目长什么样、能不能装、装在什么之上」。

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目的「门面」。说明项目是什么、提供哪些算子、硬件/软件要求、如何安装与测试、目录结构。 |
| `pyproject.toml` | 项目的「身份证 + 依赖清单」。声明包名、版本来源、运行依赖、开发依赖、构建方式、Linter 配置。 |

读完这两个文件，你就能在不看任何 kernel 代码的情况下，向别人介绍这个项目。

## 4. 核心概念与源码讲解

### 4.1 README：项目定位与算子类别

#### 4.1.1 概念说明

README 的开篇一句话给出了项目最核心的定位：

> Optimized GPU kernels for LLM operations, built with TileLang.

翻译过来：**为 LLM 运算优化的 GPU kernel，用 TileLang 构建。** 这里有两个关键信息：

1. **目标场景**：大语言模型（LLM）的训练与推理，而不是通用科学计算。
2. **实现工具**：TileLang —— 一门用 Python 表达高性能 GPU kernel 的领域专用语言（DSL）。

README 还坦率地说明：项目里的大部分 kernel 都接近「计算强度和显存带宽的硬件极限」，部分已经在内部训练/推理中使用，但**不代表最佳实践**，代码质量和文档仍在持续改进。这句话很重要：它告诉我们读这套源码时要以「理解思路」为主，而不是当成不能改的圣经。

#### 4.1.2 核心流程

从「需求 → 算子 → 落地」的视角，TileKernels 的定位可以这样理解：

```text
大模型训练/推理需要一个算子（如量化、转置、MoE 路由）
        │
        ▼
该算子对延迟/带宽极其敏感，手写 CUDA 太慢、太难迭代
        │
        ▼
用 TileLang DSL 在 Python 里表达 kernel（易写、易调）
        │
        ▼
TileLang 编译成针对 SM90/SM100 优化的 CUDA/PTX 代码
        │
        ▼
在 GPU 上逼近硬件极限运行；再用 PyTorch 封装成可训练/可推理的接口
```

这条链路解释了为什么项目同时依赖 TileLang、PyTorch、CUDA 和特定 GPU：它们分别对应「写 kernel / 用 kernel / 编译运行 / 真正跑」四个层次。

#### 4.1.3 源码精读

**项目定位开篇语**——点明「LLM + TileLang」两个核心：

[README.md:1-5](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/README.md#L1-L5) 这几行给出项目名、一句话定位，并说明「大多数 kernel 接近硬件性能极限，但不代表最佳实践」。

**Features：六大 kernel 家族 + 一层 modeling 封装**——这是项目「提供哪些算子」的最权威清单：

[README.md:7-15](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/README.md#L7-L15) README 用 bullet 列出了项目的功能分类。提取其中的关键描述如下：

```text
- Gating            — Top-k 专家选择与打分（MoE 路由用）
- MoE Routing       — token→专家映射、融合 expand/reduce、权重归一化
- Quantization      — per-token / per-block / per-channel 的 FP8/FP4/E5M6 量化，
                      含融合 SwiGLU+量化算子
- Transpose         — 批量转置
- Engram            — engram 门控 kernel，融合 RMSNorm，含前向/反向与权重梯度归约
- Manifold HyperConnection — 超连接 kernel，含 Sinkhorn 归一化与 mix 拆分/施加
- Modeling          — 高层 torch.autograd.Function 封装，把底层 kernel 组合成可训练层
```

要点说明：

- 前 6 项是**底层 kernel 家族**（Gating 与 MoE Routing 关系紧密，常被合称为「MoE 相关算子」，所以也有「五大类」的说法）。它们各自是一组针对某一类计算的高性能实现。
- 第 7 项 **Modeling** 不是新算子，而是「**封装层**」：用 `torch.autograd.Function` 把前面的底层 kernel 包装成能参与 PyTorch 自动求导、能训练的层（如 engram gate、mHC pipeline）。这解释了「底层 kernel」和「高层可训练层」的分工。

**目录结构**——README 给出的包结构与真实仓库一致（六大算子目录 + modeling/torch/testing）：

[README.md:56-68](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/README.md#L56-L68) 其中 `moe/`、`quant/`、`transpose/`、`engram/`、`mhc/` 放底层 kernel；`modeling/` 放高层 autograd 层；`torch/` 放 PyTorch 参考实现（用来对拍验证）；`testing/` 放测试与基准工具。

> 顺带说明：真实仓库在 `tile_kernels/` 顶层还有两个 README 没画进树里的基础设施文件——`config.py`（探测 SM 数量、共享内存等硬件信息）和 `utils.py`（对齐、整除等小工具）。它们不是某个算子家族，而是所有 kernel 共用的工具，会在后续讲义（如 u1-l3、u10-l1）展开。

#### 4.1.4 代码实践

这是本讲的「源码阅读型实践」，目的是用一张表把 README 的关键信息固化下来。

1. **实践目标**：从 README 提炼出「算子类别清单」和「硬件/软件要求清单」，做成两张表，作为后续阅读的索引。
2. **操作步骤**：
   - 打开 [README.md](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/README.md)。
   - 定位到 `## Features` 段（约第 7–15 行），逐条抄录算子类别。
   - 定位到 `## Requirements` 段（约第 17–23 行），抄录版本要求。
3. **需要观察的现象**：你会得到类似下表的结果（这就是「预期产出」）。
4. **预期结果**：

   **表 A：算子类别**

   | 类别 | 一句话作用 |
   | --- | --- |
   | Gating | MoE 的 Top-k 专家选择与打分 |
   | MoE Routing | token→专家映射、融合 expand/reduce、权重归一化 |
   | Quantization | FP8/FP4/E5M6 量化（per-token/block/channel）+ 融合 SwiGLU |
   | Transpose | 批量转置 |
   | Engram | engram 门控，融合 RMSNorm，含前向/反向 |
   | Manifold HyperConnection | 超连接（Sinkhorn、mix 拆分/施加） |
   | Modeling | 高层 autograd 封装（非新算子） |

5. **待本地验证**：如果你在本地克隆了仓库，可以再对照 `tile_kernels/` 目录确认每个类别都对应一个子目录（modeling/torch/testing 除外）。

#### 4.1.5 小练习与答案

**练习 1**：README 的 Features 里，「Modeling」这一项和其它六项有什么本质不同？

> **参考答案**：其它六项是**底层 kernel 家族**（各自实现一类高性能计算）；Modeling 不是新算子，而是用 `torch.autograd.Function` 把底层 kernel **封装成可训练、可自动求导的 PyTorch 层**，属于「高层封装」而非「底层实现」。

**练习 2**：为什么 README 要强调「不代表最佳实践」？读源码时这提醒了我们什么？

> **参考答案**：因为代码质量和文档仍在持续改进。这提醒我们：读这套源码应以**理解思路与技巧**为目标，不要把它当成不可改的样板；遇到写法时多问「为什么这样写」，而不是直接照抄。

---

### 4.2 pyproject.toml：依赖层次与版本要求

#### 4.2.1 概念说明

`pyproject.toml` 是现代 Python 项目的标准配置文件。它回答几个核心问题：

- **你是谁**：包名（`name`）、描述（`description`）、版本（`version`）。
- **你在谁之上跑**：运行依赖（`dependencies`）、Python 版本（`requires-python`）。
- **怎么构建**：`[build-system]` 指定构建后端（这里是 setuptools）。
- **开发还需要什么**：`[project.optional-dependencies]` 里的 `dev` 组。

对 TileKernels 而言，`pyproject.toml` 揭示了最关键的一点：**运行时只直接依赖 `torch` 和 `tilelang` 两个 Python 库**，而 CUDA Toolkit 与 GPU 硬件并不作为 Python 包出现——它们是 tilelang（以及 torch）的「下游系统依赖」。理解这一层关系，才能解释为什么 README 把 CUDA、SM90/SM100 单独列为要求。

#### 4.2.2 核心流程

TileKernels 的依赖层次是一棵「从应用到硬件」的树：

```text
你的应用 / 测试代码
        │  import tile_kernels
        ▼
tile_kernels（本项目，Python 包）
        │  运行时依赖
        ├──> torch        （PyTorch：张量、autograd、设备管理）
        └──> tilelang     （TileLang DSL：把 kernel 编译成 GPU 代码）
                │
                ▼
        CUDA Toolkit（系统级，如 13.1+）   ──┐
                │                            │ 真正在 GPU 上跑
                ▼                            │
        NVIDIA GPU 硬件：SM90（Hopper）/ SM100（Blackwell）
```

注意：**CUDA Toolkit 和 GPU 架构不在 `dependencies` 列表里**，因为它们是系统/硬件层的依赖，由 tilelang 在编译运行时去使用。这也是为什么 `pip install` 成功 ≠ 一定能跑——还得有对的 GPU 驱动、CUDA Toolkit 和支持的硬件。

#### 4.2.3 源码精读

**包名与动态版本**——版本不是写死的，而是用 setuptools-scm 从 git 标签自动生成：

[pyproject.toml:8-13](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/pyproject.toml#L8-L13) 其中 `dynamic = ["version"]` 表示版本号在构建时动态计算；配合顶部 [tool.setuptools_scm] 把版本写入 `tile_kernels/_version.py`。

**运行依赖与 Python 版本**——这是「能不能装」的硬门槛：

[pyproject.toml:23-27](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/pyproject.toml#L23-L27) 关键两行：

```toml
dependencies = [
    "torch>=2.10",
    "tilelang>=0.1.9"
]
requires-python = ">=3.10"
```

把这四条（含 README 的 TileLang 0.1.9+、PyTorch 2.10+、Python 3.10+）和 README 的硬件要求（SM90/SM100、CUDA 13.1+）拼起来，就得到完整的运行环境画像。

**开发依赖**——只在你需要跑测试时才装：

[pyproject.toml:38-39](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/pyproject.toml#L38-L39) `dev` 组包含 `pytest`、`pytest-xdist`（多 worker 并行）、`pytest-repeat`（重复跑）等。这正好对应 README 里 `pytest ... -n 4`（xdist 的 4 worker）和压力测试 `--count 2`（pytest-repeat）的用法。

> 一个值得注意的细节：`classifiers` 里标的是 `Development Status :: 3 - Alpha`（[pyproject.toml:28-36](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/pyproject.toml#L28-L36)），与 README「不代表最佳实践、仍在改进」的表述一致——项目处于早期活跃阶段。

#### 4.2.4 代码实践

这个实践用 `pip` 的依赖解析能力，验证你（或只读）环境是否满足要求。

1. **实践目标**：在不实际安装的前提下，检查环境的依赖是否齐全，并记录缺失项。
2. **操作步骤**：
   - 在项目根目录执行（`--dry-run` 表示只解析、不真正安装）：
     ```bash
     pip install -e ".[dev]" --dry-run
     ```
   - 如果没有 GPU 或想跳过实际编译，也可只看解析报告里 torch/tilelang 的版本与约束。
3. **需要观察的现象**：终端会打印「计划安装 / 已经满足」的包列表。
4. **预期结果**：
   - 若环境齐全：会看到 `torch>=2.10`、`tilelang>=0.1.9` 及 `pytest`、`pytest-xdist`、`pytest-repeat` 等被解析，且 Python 解释器版本 ≥ 3.10。
   - 若环境缺失：会看到类似 `ERROR: No matching distribution found for tilelang` 或 `torch` 的报错。
5. **待本地验证**：实际输出取决于本机已装的 Python / CUDA / GPU。如果你在无 GPU 的只读沙箱里跑，大概率会在 tilelang 的真实安装环节失败——**这属于正常现象**，因为 tilelang 需要 CUDA Toolkit 和支持的 GPU。请把缺失项记进下面的表。

   **表 B：版本要求与我的环境对照**

   | 项目 | 要求 | 我的版本 | 是否满足 |
   | --- | --- | --- | --- |
   | Python | ≥ 3.10 | _待填_ | _待填_ |
   | PyTorch | ≥ 2.10 | _待填_ | _待填_ |
   | TileLang | ≥ 0.1.9 | _待填_ | _待填_ |
   | CUDA Toolkit | ≥ 13.1（README 要求） | _待填_ | _待填_ |
   | GPU 架构 | SM90 / SM100（README 要求） | _待填_ | _待填_ |

#### 4.2.5 小练习与答案

**练习 1**：为什么 `dependencies` 里只写了 `torch` 和 `tilelang`，却没有 CUDA Toolkit 和 GPU？

> **参考答案**：因为 CUDA Toolkit 是**系统级**依赖、GPU 是**硬件**，它们都不是 PyPI 上的 Python 包，所以不能写进 `dependencies`。`tilelang`（和 `torch`）在运行/编译时会去调用系统的 CUDA Toolkit 并依赖特定 GPU 架构，README 把这些「下游系统/硬件要求」单独列出来提醒用户。

**练习 2**：`pip install -e ".[dev]"` 中的 `-e` 和 `[dev]` 分别是什么意思？

> **参考答案**：`-e` 是「可编辑（editable）安装」，把包链接到源码目录，改源码后无需重装即可生效，适合开发；`[dev]` 表示额外安装 `pyproject.toml` 中 `optional-dependencies.dev` 那一组依赖（pytest、pytest-xdist、pytest-repeat 等）。合起来就是「以开发模式安装本项目，并带上测试工具链」。

**练习 3**：`requires-python = ">=3.10"` 与 README 的「Python 3.10 or higher」是什么关系？为什么 `classifiers` 里只列到 3.12？

> **参考答案**：二者一致，都要求 Python ≥ 3.10。`classifiers` 里列 3.10/3.11/3.12 是项目**声明已测试/支持**的具体版本（PyPI 展示用），并不等于「3.13 一定不行」——`requires-python` 才是硬性的版本下限。

## 5. 综合实践

把本讲两个模块串起来，完成一份「**TileKernels 环境与定位速查卡**」：

1. 读 [README.md](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/README.md)，整理出：项目一句话定位、算子类别清单（区分底层 kernel 与 modeling 封装）、安装与测试命令。
2. 读 [pyproject.toml](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/pyproject.toml)，整理出：包名、运行依赖、Python 版本、dev 依赖、构建后端。
3. 用一张「依赖层次图」把 TileKernels → {torch, tilelang} → CUDA Toolkit → SM90/SM100 GPU 连起来，并口头解释每一层各干什么。
4. （可选）在本地跑 `pip install -e ".[dev]" --dry-run`，把实际缺失的依赖填进表 B，判断本机是否具备运行条件；若不具备，写明卡在哪一层（Python？PyTorch？TileLang？CUDA？GPU？）。

**验收标准**：你能不看任何资料，向一个新同学讲清「TileKernels 是什么、装在什么之上、提供哪几类算子」。

## 6. 本讲小结

- TileKernels 是**面向 LLM 训练/推理的高性能 GPU 算子库**，用 TileLang DSL 编写，目标是逼近硬件的计算与带宽极限。
- README 列出 6 个底层 kernel 家族（Gating、MoE Routing、Quantization、Transpose、Engram、Manifold HyperConnection）外加 1 层 Modeling（`torch.autograd.Function` 封装），共 7 个 Features。
- 运行时只直接依赖 `torch>=2.10` 与 `tilelang>=0.1.9`，Python ≥ 3.10；CUDA Toolkit（13.1+）与 GPU（SM90/SM100）是下游系统/硬件依赖，不在 `dependencies` 中。
- 项目处于 Alpha 阶段、仍在持续改进，读源码应以理解思路为主。
- 包结构为六大算子目录 + `modeling/`（高层封装）+ `torch/`（参考实现）+ `testing/`（测试工具），顶层另有 `config.py`、`utils.py` 基础设施。
- `pip install -e ".[dev]"` 是开发安装命令，`dev` 组含 pytest 全家桶，与 README 的多 worker、重复测试命令对应。

## 7. 下一步学习建议

你已经知道项目「是什么、装在哪」，但还没看过任何具体运行方式。建议按顺序继续：

- **u1-l2《安装、运行与测试工作流》**：动手跑 `pytest`、用 `--run-benchmark` 看延迟/带宽、设置 `TK_PRINT_KERNEL_SOURCE=1` 看生成的 CUDA 源码，把环境真正跑通。
- **u1-l3《目录结构与包入口》**：深入 `tile_kernels/__init__.py`、`config.py`、`utils.py`，理解导出方式与硬件探测基础设施。
- 之后再进入第 2 单元，开始学习 TileLang 算子的标准骨架（`@tilelang.jit` + `@T.prim_func`）。

> 阅读建议：本系列每篇都要求你**打开真实源码对照**阅读，永久链接里的行号基于当前 HEAD，若仓库更新请以最新代码为准。
