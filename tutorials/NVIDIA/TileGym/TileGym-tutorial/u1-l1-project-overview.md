# TileGym 是什么：项目定位与核心特性

## 1. 本讲目标

本讲是整本学习手册的第一讲，不要求你写任何 GPU 代码，目标是建立一张"全局地图"。

学完本讲，你应该能够：

- 用自己的话说出 **TileGym 是什么、解决什么问题**；
- 理解 **CUDA Tile / tile 编程模型** 这个核心概念，以及它与传统 CUDA 编程、与 Triton 等的关系；
- 列出 TileGym 的 **四大核心特性**：丰富的内核示例、真实深度学习算子的实现、性能基准、端到端 LLM 集成；
- 知道 TileGym **需要的硬件**（Blackwell / Ampere）和 **软件依赖**（CUDA 13.1+、PyTorch、tileiras 编译器）；
- 知道 TileGym **支持的后端**（cuTile / tilecpp / triton / cutile-rs）和 **已支持的 LLM** 模型清单。

后续每一讲都会往这张地图里添加细节。如果你现在看不懂某个术语，不用紧张，本讲的"前置知识"会先把它们讲清楚。

## 2. 前置知识

本讲面向零基础读者，但有几个概念先讲清楚，理解后面会顺畅得多。

### 2.1 什么是 GPU 内核（kernel）

在深度学习里，绝大多数计算（矩阵乘、注意力、归一化）最终都要交给 GPU 去算。**一段运行在 GPU 上、被大量线程并行执行的函数，就叫 GPU 内核（kernel）**。比如 `softmax`、`matmul`、`attention`，都可以写成内核。

写内核的难点不在于"算什么"，而在于"怎么让数据在 GPU 的存储层级之间高效搬运"。GPU 的计算单元极快，但显存（global memory）相对慢得多，如果每个线程都直接去显存里读写，计算单元就会一直等数据——这就是所谓的 **内存墙（memory wall）**。

### 2.2 什么是 tile（瓦片）编程模型

为了绕开内存墙，GPU 上有一小块非常快的片上存储（共享内存 / 寄存器）。**tile 编程模型** 的核心思想是：把大块数据切成一小块一小块的 **瓦片（tile）**，先把一个瓦片从慢速显存搬到快速片上存储，让计算单元在片上反复使用它，算完再写回。

直观地说，把一个 \( M \times K \) 的大矩阵切成若干个 \( \mathrm{TILE\_M} \times \mathrm{TILE\_K} \) 的小块，每个小块就是一个 tile：

\[
M \times K \;\longrightarrow\; \text{若干个 } \mathrm{TILE\_M} \times \mathrm{TILE\_K} \text{ 的瓦片}
\]

**CUDA Tile** 是 NVIDIA 推出的一套 **基于 tile 的编程抽象 / DSL**：你只需要描述"每次处理一个 tile、tile 内做什么计算、tile 之间怎么搬运"，编译器和运行时帮你把它映射成高效的 GPU 代码。本仓库里你会反复看到的 `cuTile`、`@ct.kernel`、`ct.load`、`ct.store` 都属于这套抽象。

> 小提示：你可以把 tile 编程模型类比成 Triton——两者都是"按 tile 思考"的高层 GPU 编程方式，屏蔽了传统 CUDA 里繁琐的线程块/共享内存手工管理。TileGym 本身也提供了 Triton 后端（见 4.4 节）。

### 2.3 什么是 LLM 算子

大语言模型（LLM，例如 Llama 3.1、DeepSeek V2）的前向推理，本质上是把一批"算子"串起来跑：旋转位置编码（RoPE）、均方根归一化（RMSNorm）、SwiGLU 激活、矩阵乘（GEMM）、多头注意力（Attention）等。TileGym 不只是一个内核教学场，它还把这些优化过的内核 **端到端接入真实 LLM**，让模型推理更快。

---

## 3. 本讲源码地图

本讲主要读文档，不深入代码。涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md) | 项目门面，给出定位、特性、安装、后端、快速上手方式 |
| [ROADMAP.md](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/ROADMAP.md) | 内核 / 模型 / 内核库的支持状态路线图，以及贡献指引 |
| [src/tilegym/\_\_init\_\_.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/__init__.py) | Python 包入口，暴露 `set_backend`、`get_available_backends` 等顶层 API |

顶层目录一览（先混个眼熟，U1-L4 会专门讲）：

```
TileGym/
├── README.md            # 项目说明（本讲重点）
├── ROADMAP.md           # 内核与模型路线图（本讲重点）
├── pyproject.toml / setup.py / requirements.txt   # 安装与依赖
├── src/tilegym/         # 核心 Python 包
│   ├── ops/             # 所有算子接口 + 各后端实现（cutile/tilecpp/triton/cutile_rs）
│   ├── backend/         # 后端选择与分发
│   ├── transformers/    # 把内核接入 HuggingFace transformers
│   └── suites/          # liger/flashinfer 等扩展套件
├── tests/               # 正确性测试 + benchmark
├── modeling/transformers/  # 端到端 LLM 推理基准
├── julia/               # cuTile.jl（Julia 版内核，可选）
└── skills/              # 贡献新内核的指引
```

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：项目定位与价值、核心特性概览、目标硬件与依赖要求、支持的后端与 LLM。

### 4.1 项目定位与价值

#### 4.1.1 概念说明

开源世界从来不缺"内核集合"，那 TileGym 的独特价值是什么？它同时扮演两个角色：

1. **cuTile 的教学场（playground）**：用大量由浅入深的内核示例，教你怎么用 CUDA Tile / cuTile 这套 tile DSL 写 GPU 内核——从最简单的 `softmax`，一直到很复杂的 `Flash Attention`、`MLA`（多潜注意力）。
2. **LLM 加速示例库**：把这些优化过的内核，真正接入 Llama 3.1、DeepSeek V2 等真实大模型，跑通端到端推理，并给出性能对比。

这两个角色合起来，意味着你既能"学怎么写内核"，又能"看到内核在真实模型里怎么发光发热"。这是它和纯教程仓库、纯算子库最大的区别。

#### 4.1.2 核心流程

从使用者的视角，TileGym 的价值链是：

```text
学 tile 编程模型
      │
      ▼
阅读 / 运行 cuTile 内核示例（softmax / matmul / attention ...）
      │
      ▼
用 benchmark 量化内核性能
      │
      ▼
通过 monkey-patch 把内核接入 HuggingFace 模型
      │
      ▼
端到端 LLM 推理加速（Llama 3.1 / DeepSeek V2 / Qwen / Gemma ...）
```

#### 4.1.3 源码精读

README 开篇一句话定义了 TileGym：

[README.md:9-L9](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md#L9-L9) —— 明确把 TileGym 定位为"基于 CUDA Tile 的内核库，提供丰富的 tile 编程教学与示例"。

紧接着的 Overview 段落点明了"双重角色"：

[README.md:20-L20](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md#L20-L20) —— 把 TileGym 描述为"实验 CUDA Tile 的 playground"，并提到可以探索内核如何接入 Llama 3.1、DeepSeek V2 等真实 LLM。

#### 4.1.4 代码实践

这是一个 **源码阅读型实践**（本讲不要求运行代码）。

1. **实践目标**：用自己的话，而不是照抄，复述 TileGym 的定位。
2. **操作步骤**：
   - 打开 [README.md](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md)，只读 `Overview` 这一节（约第 18–21 行）。
   - 合上文档，用中文写 3 句话回答：TileGym 是什么？它能帮我学什么？它和真实 LLM 有什么关系？
3. **需要观察的现象**：你会发现自己能否在不看原文的情况下，准确说出"tile 编程""内核示例""LLM 集成"这三个关键词。
4. **预期结果**：3 句话里至少覆盖"教学场"和"LLM 加速"这两个角色。如果只能写出一面，回头再读一遍第 20 行。

#### 4.1.5 小练习与答案

**练习 1**：TileGym 和一个"只提供算子 API 的库"相比，多提供了什么？
> **参考答案**：它还提供了 tile 编程的教学示例（让人学会写内核本身），以及把内核接入真实 LLM 做端到端推理的示例，而不只是封装好的黑盒 API。

**练习 2**：为什么 TileGym 既适合"学 GPU 编程的初学者"，也适合"想优化 LLM 的工程师"？
> **参考答案**：前者可以从最简单的 `softmax` 等内核示例入门 tile 编程；后者可以直接复用现成的优化内核，并通过 transformer 集成获得 LLM 推理加速。

---

### 4.2 核心特性概览

#### 4.2.1 概念说明

README 的 Features 列表把 TileGym 的卖点压缩成四条。理解这四条，你就理解了整个仓库的组织逻辑——后续每一个目录、每一篇讲义，基本都是围绕这四条展开的。

#### 4.2.2 核心流程

四大特性 → 仓库落地的对应关系：

| 核心特性 | 在仓库里的落地点 |
|----------|------------------|
| 丰富的 CUDA Tile 内核示例 | `src/tilegym/ops/cutile/` 下逐个内核（softmax、matmul、attention 等） |
| 常见深度学习算子的实用实现 | 同上，覆盖 Linear Algebra / Attention / Normalization / Activation / MoE / RoPE 等 |
| 性能基准 | `tests/benchmark/`（`run_all.sh`） |
| 端到端 LLM 集成 | `modeling/transformers/`（HF 推理基准） |

#### 4.2.3 源码精读

README 的 Features 节直接列出了这四条：

[README.md:25-L28](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md#L25-L28) —— 四条特性分别对应：内核示例、实用算子实现、性能基准、端到端 LLM 集成。

而 [ROADMAP.md](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/ROADMAP.md) 的算子支持表，给出了"实用算子实现"到底有多丰富。它按类别罗列了算子的前向 / 反向支持状态，类别包括 Linear Algebra（MatMul / BMM / Grouped GEMM）、Attention（Attention / Flash Decode / MLA / MLA Decoding）、Normalization（RMSNorm / LayerNorm）、Activation（SiLU and Mul / SwiGLU / Softmax）、Mixture of Experts（MoE）、Positional Encoding（RoPE）等：

[ROADMAP.md:15-L54](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/ROADMAP.md#L15-L54) —— 算子支持状态表，每个算子标注前向 / 反向的支持状态（✅ Available / 🧪 Experimental / 🚧 WIP / 📅 Planned）。

状态图例说明（值得记住，读路线图时常用）：

[ROADMAP.md:85-L90](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/ROADMAP.md#L85-L90) —— `✅ Available` 表示已充分测试、性能优化、可用于生产；`🧪 Experimental` 表示功能可用但带 `@experimental_kernel` 标签、尚未完全验证性能。

> 小提示：状态表里的"反向（Backward）"列表示该算子是否支持 autograd 反向传播。能反向的算子才能用于训练，本讲你只需知道"前向 / 反向"这个区分即可，U4 会专门讲 autograd 集成。

#### 4.2.4 代码实践

1. **实践目标**：把"四大特性"和"仓库目录"对应起来，建立空间感。
2. **操作步骤**：
   - 在 [ROADMAP.md](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/ROADMAP.md) 的算子表里，数一数 `✅ Available`（前向）的算子大概有多少个。
   - 回到本讲第 3 节的目录树，确认这四个目录存在：`src/tilegym/ops/`（特性 1、2）、`tests/benchmark/`（特性 3）、`modeling/transformers/`（特性 4）。
3. **需要观察的现象**：你会看到算子覆盖面相当广，且按类别组织得很清楚。
4. **预期结果**：能说出"想看某类算子去 `ops/cutile/`，想看性能去 `tests/benchmark/`，想看 LLM 集成去 `modeling/transformers/`"。
5. 本步骤不运行任何命令，属于阅读型实践。

#### 4.2.5 小练习与答案

**练习 1**：四大特性里，"性能基准"对应的目录是哪个？
> **参考答案**：`tests/benchmark/`，README 的 Quick Start 里给出的入口命令是 `cd tests/benchmark && bash run_all.sh`。

**练习 2**：ROADMAP 里 `🧪 Experimental` 和 `✅ Available` 的核心区别是什么？
> **参考答案**：`✅ Available` 已充分测试且性能优化、可上生产；`🧪 Experimental` 功能可用但带实验标签、性能尚未完全验证，欢迎社区反馈。

**练习 3**：如果某个算子的 Backward 列是 `📅 Planned`，意味着什么？
> **参考答案**：意味着该算子目前只有前向实现，反向（autograd）还没做，在路线图上计划未来开发——这样的算子暂时不适合直接用于训练。

---

### 4.3 目标硬件与依赖要求

#### 4.3.1 概念说明

TileGym 是面向较新 NVIDIA 硬件的高性能内核库，**不是随便一台机器都能跑**。在动手安装前，必须先确认两件事：

- **硬件**：需要 Blackwell 或 Ampere 架构的 GPU；
- **软件**：需要 CUDA 13.1+、PyTorch（自带 Triton）、以及运行时编译器 `tileiras`。

理解依赖关系的关键在于：cuTile（`cuda-tile`）是 TileGym 写内核用的 DSL，而 `tileiras` 是这个 DSL 在运行时把内核编译成 GPU 代码所依赖的编译器。没有 `tileiras`，cuTile 内核跑不起来。

#### 4.3.2 核心流程

安装与运行的依赖链：

```text
PyTorch (cu130) + Triton     ← 基础数值库 / 内核运行环境
        │
        ▼
pip install tilegym[tileiras]  ← 装 TileGym + cuda-tile + tileiras 编译器
        │
        ▼
需要 Blackwell (CUDA 13.1+) 或 Ampere (CUDA 13.2+) GPU
        │
        ▼
即可调用 / 运行 TileGym 内核
```

#### 4.3.3 源码精读

README 的 Prerequisites 明确写出了硬件门槛：

[README.md:34-L34](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md#L34-L34) —— 要求 **Blackwell GPU**（如 B200、RTX 5080、RTX 5090）配 **CUDA 13.1+**；**Ampere**（如 A100）也支持，但需要 **CUDA 13.2+**。所有发布的内核都在这两种架构上验证过。

软件依赖三条：

[README.md:36-L38](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md#L36-L38) —— PyTorch（2.9.1 或兼容版本）、CUDA 13.1+、Triton（随 PyTorch 一起装）。

`tileiras` 编译器依赖的说明：

[README.md:54-L54](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md#L54-L54) —— TileGym 用 `cuda-tile`（≥ 1.3.0）编写 GPU 内核，而它在运行时依赖 `tileiras` 编译器。这也是为什么推荐用 `pip install tilegym[tileiras]`——这个 extra 会把 `tileiras` 一起装进 Python 环境。

#### 4.3.4 代码实践

1. **实践目标**：在动手安装前，先自查硬件 / 软件是否达标，避免白忙。
2. **操作步骤**：
   - 确认 GPU 型号：如果是 B200 / RTX 5080 / RTX 5090（Blackwell），需要 CUDA 13.1+；如果是 A100（Ampere），需要 CUDA 13.2+。把结论写下来。
   - （**可选，待本地验证**）如果你已经有 CUDA，运行 `nvidia-smi` 或 `nvcc --version` 查看 CUDA 版本是否满足上面要求。
   - （**可选，待本地验证**）如果你已装好 PyTorch，运行 `python -c "import torch; print(torch.__version__)"` 看 torch 是否 ≥ 2.9.1 或兼容版本。
3. **需要观察的现象**：你应当能填出"我的 GPU 是 ____ 架构，需要 CUDA ____ 以上"。
4. **预期结果**：硬件 / CUDA / torch 三项至少心里有数；不满足的话，先解决环境再继续（U1-L2 会专门讲安装）。
5. 如果当前环境没有 GPU，明确写"待本地验证"，不要假装跑过。

#### 4.3.5 小练习与答案

**练习 1**：A100（Ampere）用户需要的 CUDA 版本和 B200（Blackwell）用户一样吗？
> **参考答案**：不一样。Blackwell 需要 CUDA 13.1+；Ampere 需要 **更高的** CUDA 13.2+。

**练习 2**：`pip install tilegym[tileiras]` 里的 `[tileiras]` 是干什么的？什么情况下可以省略它？
> **参考答案**：`[tileiras]` 这个 extra 会把 `cuda-tile[tileiras]` 连同 `tileiras` 运行时编译器一起装进 Python 环境。如果你的系统里已经装好了 `tileiras`（例如来自 CUDA Toolkit 13.1+），就可以省略它，直接 `pip install tilegym`。

**练习 3**：`tileiras` 和 `cuda-tile` 是什么关系？
> **参考答案**：`cuda-tile`（cuTile）是 TileGym 用来编写 tile 内核的 DSL；`tileiras` 是它在运行时把内核代码编译成可执行 GPU 代码所依赖的编译器。前者负责"怎么写"，后者负责"编译运行"。

---

### 4.4 支持的后端与 LLM

#### 4.4.1 概念说明

这是 TileGym 最有特色的设计之一：**同一个算子名，可以用多种"后端（backend）"实现**。比如 `softmax` 这个算子，cuTile 后端有一份实现，tilecpp 后端也有一份实现——它们注册到同一个名字下，运行时由 TileGym 的分发机制根据你选中的后端决定真正跑哪一份。

TileGym 目前涉及 **4 种后端**：

| 后端名 | 语言 / 形式 | 一句话说明 |
|--------|-------------|-----------|
| **cuTile**（默认） | Python DSL（`@ct.kernel`） | 基于 cuda-tile 的 tile DSL，仓库主力教学后端 |
| **CUDA Tile C++（tilecpp）** | C++（`.cuh`） | 用 CUDA Tile C++ 写的内核，需 nvcc ≥ 13.3 |
| **Triton CUDA Tile IR（triton）** | Triton | Triton-to-tile-IR 后端，常作为 cuTile 缺失时的 fallback |
| **cuTile-rs** | Rust（FFI `.so`） | 用 Rust 写内核、经 C-ABI 加载的可选后端 |

在 LLM 一侧，TileGym 已经把内核接入了相当多真实模型。

#### 4.4.2 核心流程

多后端 + 多模型的整体形态：

```text
统一算子名（softmax / matmul / fmha ...）
        │
        ├── cuTile 实现      ┐
        ├── tilecpp 实现     ├── 由后端分发机制按"当前后端"二选一/多选一
        ├── triton 实现      │
        └── cutile-rs 实现   ┘
        │
        ▼
通过 monkey-patch 接入 HuggingFace transformers
        │
        ▼
Llama 3.1 / DeepSeek V2 / Qwen2 / Qwen3.5 / Gemma-3 / GPT-OSS / Mistral / Phi-3 / OLMo-3 ...
```

> 多后端的"分发机制"是本仓库的核心抽象，U2 会整章讲它。本讲你只要知道"有多个后端、可以切换"即可。

#### 4.4.3 源码精读

README 的 Backends 节列出了三种主要后端，各自放在 `src/tilegym/ops/` 下独立目录：

[README.md:86-L90](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md#L86-L90) —— cuTile（默认，`ops/cutile`）、CUDA Tile C++（`ops/tilecpp`）、Triton CUDA Tile IR（`ops/triton`），并注明 triton 后端用 `ENABLE_TILE=1` 在运行时选择。

第四种可选后端 cuTile-rs 单独一节介绍：

[README.md:159-L165](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md#L159-L165) —— cuTile-rs 是用 Rust 写内核、通过 C-ABI `libcutile_kernels.so` 加载的可选后端，代码在 `src/tilegym/ops/cutile_rs`，需 Rust 1.89+ 与带 headers 的 CUDA toolkit。

切换后端的顶层 API 在包入口里导出。`src/tilegym/__init__.py` 从 `backend` 子模块导出了一批后端管理函数：

[\_\_init\_\_.py:34-L40](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/__init__.py#L34-L40) —— 导出 `get_available_backends`、`get_current_backend`、`set_backend`、`is_backend_available` 等，这就是你之后用来查询 / 切换后端的入口。

在 LLM 一侧，ROADMAP 的 E2E 模型表给出了已支持的真实模型清单：

[ROADMAP.md:60-L71](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/ROADMAP.md#L60-L71) —— 已支持的模型包括 LLaMA-3.1-8B、DeepSeek-V2-Lite-Chat、Qwen2-7B、Qwen3.5-7B、Gemma-3-4B-IT、GPT-OSS、Mistral-7B-Instruct-v0.3、Phi-3-mini-4k-instruct、OLMo-3-1025-7B，均在 B200 上测试过。

#### 4.4.4 代码实践

1. **实践目标**：建立"4 后端 + 已支持 LLM"的准确清单。
2. **操作步骤**：
   - 打开 [README.md 的 Backends 节](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md#L84-L90)，把 cuTile / tilecpp / triton 三个后端对应的源码目录填进一张表。
   - 打开 [README.md 的 cuTile-rs 节](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md#L159-L165)，把第四个后端 cutile-rs 补进表里。
   - 打开 [ROADMAP 的模型表](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/ROADMAP.md#L60-L71)，抄一份当前已支持的 LLM 清单。
3. **需要观察的现象**：你会注意到 cutile-rs 被标注为"可选（Optional）"，而 cuTile 是默认后端。
4. **预期结果**：得到一张"后端名 → 目录 → 是否默认/可选"的表，以及一份 LLM 清单。
5. **（可选，待本地验证）** 如果你已经按 U1-L2 装好了 tilegym，可以运行 `python -c "import tilegym; print(tilegym.get_available_backends())"` 看当前环境实际可用的后端（不同机器结果会不同）。没有环境就跳过，不要假装运行。

#### 4.4.5 小练习与答案

**练习 1**：cuTile、tilecpp、triton、cutile-rs 四个后端，哪个是默认后端？哪个是"可选"的？
> **参考答案**：cuTile 是默认后端；cutile-rs 是可选（Optional）后端，只覆盖部分算子，且只在源码检出时可用。

**练习 2**：如果当前机器没装 cuTile 所需的 `tileiras`，会发生什么？（提示：看 triton 后端的角色）
> **参考答案**：cuTile 后端会不可用。README/设计里 triton 后端常作为 fallback（多个算子的 `fallback_backend` 设为 `"triton"`），这样在 cuTile 缺失时仍能退而用 triton 实现跑通（U2 会详讲 fallback 机制）。

**练习 3**：triton 后端和普通的 Triton 有什么不同？
> **参考答案**：TileGym 的 triton 后端指的是 **Triton-to-tile-IR**（CUDA Tile IR）实现，需要用 `ENABLE_TILE=1` 在运行时选择；它不是默认 PyTorch 自带的那个普通 Triton，而是把 Triton 编译路径走到 tile IR 上。

---

## 5. 综合实践

把本讲四个模块串起来，完成一份属于你自己的「TileGym 速写卡」。

1. **实践目标**：把项目定位、特性、硬件依赖、后端与模型整合到一页文档里，作为后续学习的随身参考。
2. **操作步骤**：
   - 用中文写一段 **3 句话的 TileGym 简介**（覆盖"是什么 / 学什么 / 和 LLM 的关系"）。
   - 画一张表，列出 **4 个后端**：cuTile、tilecpp、triton、cutile-rs，分别写一句话定位 + 源码目录。
   - 写出 **目标硬件**：Blackwell（CUDA 13.1+）、Ampere（CUDA 13.2+）。
   - 写出至少 **5 个已支持的 LLM**（从 ROADMAP 模型表里挑）。
3. **需要观察的现象**：写完后，你能否不看任何资料，口头回答"TileGym 是什么、要什么硬件、有哪些后端"。
4. **预期结果**：一张完整的速写卡，包含 3 句话简介 + 后端表 + 硬件要求 + 模型清单。
5. 本综合实践为阅读 / 写作型，不涉及运行命令；若你的简介里没有出现"tile 编程模型"和"LLM"这两个关键词，建议重读 4.1。

> 这张速写卡建议保存下来，每学完一讲回头补一行——到手册结束时，它就是你自己的 TileGym 知识索引。

## 6. 本讲小结

- **TileGym** 是基于 CUDA Tile 的 GPU 内核库，同时是 cuTile 教学场和 LLM 加速示例库。
- 核心概念是 **tile 编程模型**：把大数据切成瓦片，搬到快速片上存储再计算，绕开内存墙。
- 四大核心特性：丰富的内核示例、实用算子实现、性能基准、端到端 LLM 集成。
- 硬件门槛：**Blackwell（CUDA 13.1+）** 或 **Ampere（CUDA 13.2+）**；软件依赖 PyTorch、Triton、`tileiras` 运行时编译器。
- 支持 **4 个后端**：cuTile（默认）、tilecpp、triton、cutile-rs（可选）。
- 已接入大量真实 LLM：Llama 3.1、DeepSeek V2、Qwen2 / Qwen3.5、Gemma-3、GPT-OSS、Mistral、Phi-3、OLMo-3 等。

## 7. 下一步学习建议

本讲只建立了全局认知，还没真正碰过代码。建议按这个顺序继续：

1. **U1-L2 环境准备与安装**：动手把 TileGym 装起来，确认 `tilegym.get_available_backends()` 能跑。
2. **U1-L3 第一次调用 TileGym 算子**：写一个最小脚本调用 `softmax`，和 PyTorch 参考对比——这是你第一次真正"用"TileGym。
3. **U1-L4 仓库目录结构导览**：把本讲第 3 节的目录树细化成完整的模块地图。

如果你想跳着看，也可以先去 **U2（统一算子接口与后端调度）** 理解"多后端分发"这个核心抽象，但建议先把 U1 走完，确保环境和地图都就位。
