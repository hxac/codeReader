# 顶层目录与代码组织

## 1. 本讲目标

学完本讲，你应该能够：

- 拿到 TensorRT-LLM 仓库后，**不再迷路**——一眼看出每个顶层目录是做什么的。
- 区分「五大块」：Python 包、C++ 核心、示例、文档、测试，以及围绕它们的脚本与 CI。
- 看懂 `examples/` 是如何「按模型」和「按特性」两种方式组织的，并能判断自己该进哪个子目录。
- 看懂 `docs/` 是如何用一个 Sphinx `toctree`（目录树）把上百篇文档串起来的，从而快速定位需要的文档。
- 建立一个可以在日后反复查阅的「仓库目录速查表」。

本讲是 u1（项目定位与首次运行）之后的「地图课」。u1 教你「这是什么、怎么跑」；本讲教你「东西放在哪」。

## 2. 前置知识

本讲默认你已经读过 [u1-l1 项目定位与整体架构](./u1-l1-project-overview.md)，并具备以下心智模型：

- TensorRT-LLM 是一个 **Python + C++/CUDA 混合** 的开源库，口号是「Python 调度、C++ 加速」。
- 它有两条执行路径：**PyTorch（默认）** 与 **AutoDeploy（beta）**，二者共享由 nanobind 暴露的 C++ 核心。
- 请求全链路是：`HF 模型 → LLM API → Executor → Scheduler → 模型前向 → Decoder → Sampling → 生成 Token`。

如果你还不知道上面这条链路在讲什么，建议先回到 u1-l1。本讲不会重复这些概念，而是告诉你「链路上每一段的源码住在仓库的哪个目录」。

几个本讲会用到的小术语：

- **Monorepo（单体仓库）**：把 Python 包、C++ 源码、文档、测试、构建脚本全部放在一个 git 仓库里管理。
- **Sphinx / toctree**：Python 文档生态的事实标准；`toctree` 是「目录树」指令，用一个 `.rst` 文件描述整份文档的章节结构。
- **Pydantic**：Python 的数据校验库，TensorRT-LLM 用它来定义配置类（后续 u4 会详讲）。

## 3. 本讲源码地图

本讲不深入任何模块的内部实现，只建立「文件 / 目录 → 职责」的映射。涉及的关键入口如下：

| 文件 / 目录 | 作用 |
|------|------|
| `README.md` | 项目门面，含技术博客、新闻、总览与快速上手链接 |
| `examples/README.md` | 示例总目录的说明，用一张表说明各子目录职责，并标注遗留工作流 |
| `docs/source/index.rst` | 文档站的总入口，用 toctree 组织全部章节 |
| `docs/source/deployment-guide/index.rst` | 部署指南入口，含「配方选择器」和按模型罗列的部署文档 |
| `tensorrt_llm/` | Python 主包（代码的主体） |
| `cpp/` | 共享 C++ 核心 |
| `examples/`、`docs/`、`tests/`、`scripts/`、`jenkins/` | 示例、文档、测试、构建脚本、CI 流水线 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**顶层目录速查表**、**`examples/` 的组织方式**、**`docs/` 的结构**。

### 4.1 顶层目录速查表

#### 4.1.1 概念说明

TensorRT-LLM 是一个 monorepo。所谓「顶层目录速查表」，就是一张「目录名 → 职责」的对照表。掌握它的最大好处是：以后无论在 GitHub 上浏览代码、看报错堆栈、还是读 PR，你都能瞬间判断「这件事归哪个目录管」。

仓库的定位可以从 README 的标语一句话概括：

> TensorRT LLM optimizes inference for LLMs and Visual Gen models with specialized kernels for common operations, an efficient runtime, and a pythonic framework that enables you to customize and extend the system.

这句话点明了三件事：**专用 kernel**（C++/CUDA）、**高效运行时**、**可扩展的 Python 框架**——这三点恰好对应仓库里 `cpp/`、`tensorrt_llm/` 这两大块代码的存在意义。

#### 4.1.2 核心流程

把仓库想象成一栋大楼，可以这样分层：

```text
┌─────────────────────────────────────────────────────────────┐
│  你看到的「产品」：LLM API / trtllm-serve / VisualGen        │
│  ↓ 入口都在                                                    │
├─────────────────────────────────────────────────────────────┤
│  tensorrt_llm/   ← Python 主包（调度、API、模型、serve…）    │  ★ 主体
│     └─ _torch/   ← PyTorch 后端的「心脏」（默认后端）        │
│  cpp/            ← C++/CUDA 核心（kernel、运行时、绑定）     │  ★ 加速
├─────────────────────────────────────────────────────────────┤
│  examples/  docs/  tests/   ← 示例、文档、测试（围绕主体）   │
├─────────────────────────────────────────────────────────────┤
│  scripts/  jenkins/  docker/  ← 构建、CI、容器（基础设施）   │
└─────────────────────────────────────────────────────────────┘
```

其中 `tensorrt_llm/_torch/` 是最值得记住的子目录，它就是 u1-l1 说的「PyTorch 原生架构」的落点，里面包含了 `pyexecutor/`（执行器与调度）、`models/`（模型定义）、`modules/`（attention、MoE 等模块）、`attention_backend/`、`speculative/`、`distributed/`、`auto_deploy/` 等——后续几乎所有进阶讲义都在这个目录里打转。

#### 4.1.3 源码精读

先看 README 对项目的总览定位，它解释了「为什么会有这些目录」：

- [README.md:265-267](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/README.md#L265-L267) — README 的「Overview」段落，说明本库提供专用 kernel（对应 `cpp/`）、运行时与算法优化（对应 `tensorrt_llm/_torch/`）。
- [README.md:271](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/README.md#L271) — 强调「模块化、易修改、PyTorch 原生」，并举了 `tensorrt_llm/_torch/models/modeling_deepseekv3.py` 作为「用原生 PyTorch 代码自定义模型」的范例。这条链接直接把你从 README 引到了 `_torch/models/` 这个核心目录。

下表是本讲的核心产出——**顶层目录速查表**（基于实际 `ls` 结果整理）：

| 顶层目录 / 文件 | 类别 | 职责 |
|---|---|---|
| `tensorrt_llm/` | 代码（主体） | Python 主包。`llmapi/`（LLM API）、`_torch/`（PyTorch 后端心脏）、`serve/`（在线服务）、`commands/`（CLI 入口）、`executor/`、`models/`、`runtime/`、`quantization/`、`visual_gen/`、`bench/` 等都在这里 |
| `cpp/` | 代码（加速） | 共享 C++/CUDA 核心：`kernels/`（专用 kernel）、`runtime/`、`batch_manager/`、`executor/`、`layers/`、`nanobind/`（Python 绑定）、`deep_gemm/`、`deep_ep/`、`flash_mla/` 等 |
| `examples/` | 示例 | 按模型 / 按特性组织的可运行示例与配置（详见 4.2） |
| `docs/` | 文档 | Sphinx 文档源码，构建后即官网 nvidia.github.io/TensorRT-LLM（详见 4.3） |
| `tests/` | 测试 | `unittest/`、`integration/`、`torch/`、`microbenchmarks/` 等（u12-l4 详讲） |
| `scripts/` | 基础设施 | 构建与工具脚本，如 `build_wheel.py`（构建 wheel）、`test_to_stage_mapping.py`（测试→CI 阶段映射）、`generate_llm_args_golden_manifest.py` 等 |
| `jenkins/` | CI | Jenkins 流水线定义（`.groovy`），如 `Build.groovy`、`L0_MergeRequest.groovy`、`L0_Test.groovy` |
| `docker/` | 基础设施 | 构建/发布用的 Dockerfile 与镜像脚本 |
| `triton_kernels/` | 代码（加速） | Triton 实现的 kernel 集（`matmul_ogs`、`compaction`、`reduce`、`distributed`、`swiglu` 等） |
| `triton_backend/` | 集成 | 与 Triton Inference Server 集成相关的后端代码 |
| `3rdparty/` | 依赖 | 第三方依赖 |
| `enroot/` | 基础设施 | 集群（Slurm）场景下用 enroot 拉起容器镜的相关文件 |
| `security_scanning/` | 基础设施 | 安全扫描配置 |

此外，仓库根有一批重要的「治理类」文件，初学者尤其要留意：

| 文件 | 作用 |
|---|---|
| `README.md` | 项目门面 |
| `AGENTS.md` / `CLAUDE.md` | 给贡献者/智能体的仓库导航与规则（`CLAUDE.md` 直接 `@AGENTS.md`） |
| `CODING_GUIDELINES.md` | C++/Python 编码规范，改代码前必读 |
| `CONTRIBUTING.md` | 贡献流程（DCO 签名、PR 规范等） |
| `pyproject.toml` / `setup.py` / `requirements.txt` | 构建与依赖（u1-l2 详讲） |

> 记忆口诀：**「主体 `tensorrt_llm`，加速 `cpp`，外围 examples/docs/tests，地基 scripts/jenkins/docker」**。

#### 4.1.4 代码实践

**实践目标**：亲手把「顶层目录速查表」从源码里挖出来，而不是死记本讲给的表。

**操作步骤**：

1. 在仓库根执行（只读命令，安全）：
   ```bash
   ls -1 --group-directories-first
   ```
2. 对你感兴趣的目录，再 `ls -1 <目录>` 看一层，例如 `ls -1 tensorrt_llm/_torch`。
3. 用 `git ls-files <目录> | head` 看该目录下实际被 git 跟踪的文件类型。

**需要观察的现象**：

- 顶层会同时出现目录（`tensorrt_llm`、`cpp`、`examples` …）和文件（`README.md`、`setup.py` …）。
- `tensorrt_llm/_torch/` 下会出现 `pyexecutor/`、`models/`、`modules/`、`attention_backend/` 等子目录——它们就是后续讲义的主战场。

**预期结果**：你得到一份与本讲 4.1.3 表格一致的目录清单，并确认 `tensorrt_llm/_torch` 确实是 PyTorch 后端的核心落点。

#### 4.1.5 小练习与答案

**练习 1**：如果有人问你「TensorRT-LLM 的专用 GPU kernel 写在哪」，你会指向哪个顶层目录？为什么？

> **参考答案**：主要指向 `cpp/`（尤其 `cpp/tensorrt_llm/kernels/`），那里是 C++/CUDA kernel；此外 `triton_kernels/` 存放 Triton 实现的 kernel，`tensorrt_llm/_torch/custom_ops/` 则是把这些 kernel 汇总暴露给 Python 的「算子层」。三者构成了「kernel 实现 → Python 调用」的链路。

**练习 2**：`scripts/` 和 `jenkins/` 都是「基础设施」，它们的分工是什么？

> **参考答案**：`scripts/` 存放**人也会手动跑**的工具脚本（构建 wheel、生成 manifest、测试映射等）；`jenkins/` 存放 **CI 流水线自动跑**的 Groovy 定义（构建、合并门禁、L0 测试等）。简言之：scripts 是「工具」，jenkins 是「编排这些工具的流水线」。

---

### 4.2 `examples/` 的组织方式

#### 4.2.1 概念说明

`examples/` 是「能跑给你看」的目录。它解决两个问题：

1. **按模型**：我想跑某个具体模型（DeepSeek、Llama4、Qwen…），入口在哪？
2. **按特性**：我想试某个功能（投机解码、分离式服务、量化、多模态…），示例在哪？

TensorRT-LLM 用一张表把「按特性」的子目录讲清楚了，而「按模型」的指南则集中放在 `examples/models/` 下。

#### 4.2.2 核心流程

`examples/` 的组织可以用下面这张「双轴」图理解：

```text
examples/
├── 按特性（feature-oriented）                ── 想试某个「能力」
│   ├── llm-api/      离线 LLM API 用法（最常进）
│   ├── serve/        trtllm-serve 客户端 / 部署示例
│   ├── configs/      预调优的服务配置（curated + database）
│   ├── quantization/ 用 Model Optimizer 做量化
│   ├── auto_deploy/  AutoDeploy（beta）示例与模型注册
│   ├── disaggregated/ dwdp/ ngram/ wide_ep/ sparse_attention/ …
│   └── apps/ visual_gen/ opentelemetry/ ray_orchestrator/ …
│
└── 按模型（model-oriented）                  ── 想跑某个「模型」
    └── models/
        ├── core/     官方维护的主流模型指南（当前推荐工作流）
        └── contrib/  社区/实验性贡献（如 hyperclovax）
```

一个关键认知：**进入 `examples/models/core/<模型>/` 的 README，你会看到当前推荐的 `trtllm-serve` / LLM API 工作流**（例如 `deepseek_v3/README.md` 通篇用 `trtllm-serve` 和 `quickstart_advanced.py`）。而 `examples/README.md` 同时给出了一条重要提醒：旧的 `convert_checkpoint.py → trtllm-build → run.py` 引擎构建工作流已是 **legacy（遗留）**，新项目应改用 `trtllm-serve` 或 LLM API。

#### 4.2.3 源码精读

- [examples/README.md:29-37](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/examples/README.md#L29-L37) — 「Examples Directory」表格，用六行说清了 `llm-api/`、`apps/`、`configs/`、`auto_deploy/`、`serve/`、`quantization/` 各自的职责。这是进入 examples 时第一个该读的地方。
- [examples/README.md:40-47](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/examples/README.md#L40-L47) — 说明 `configs/` 目录下分 `curated/`（人工挑选的快速上手配置）与 `database/`（按 GPU/ISL/OSL/并发穷举的 Pareto 配置库）。一句 `trtllm-serve <model> --config configs/curated/<xxx>.yaml` 就是生产部署的常见起点。
- [examples/README.md:63-78](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/examples/README.md#L63-L78) — 「Legacy Engine-Build Workflow」警告，明确 `convert_checkpoint.py → trtllm-build → run.py` 已不推荐，新项目走 `trtllm-serve` / LLM API。**初学者要记住：凡是看到老教程让你 `trtllm-build`，都应回到 Quick Start 用新流程。**

`examples/models/core/` 下当前收录的模型指南目录（实际 `ls` 结果）：

`deepseek_v3`、`deepseek_v4`、`exaone`、`gemma`、`gpt_oss`、`kimi_k2`、`llama4`、`mistral_large_3`、`multimodal`、`nemotron`、`nemotron_nas`、`phi`、`qwen`；`contrib/` 下有 `hyperclovax`。

以 DeepSeek 为例：[examples/models/core/deepseek_v3/README.md](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/examples/models/core/deepseek_v3/README.md) 通篇演示的就是新工作流——从 `quickstart_advanced.py` 单卡推理、`trtllm-serve` 在线服务，到分离式服务、多节点（mpirun/Slurm）基准，覆盖了「一个模型从跑到调优」的完整链路。

#### 4.2.4 代码实践

**实践目标**：建立「我要做 X，进哪个子目录」的直觉。

**操作步骤**：

1. 列出按特性的入口：`ls -1 examples`。
2. 列出按模型的入口：`ls -1 examples/models/core`。
3. 打开 `examples/llm-api/quickstart_example.py`（u1-l3 用过的最小示例），确认它属于「按特性」轴里最常进的 `llm-api/`。
4. 打开任一 `examples/models/core/<模型>/README.md`，确认它属于「按模型」轴。

**需要观察的现象**：

- `examples/llm-api/` 里既有 `quickstart_example.py`（入门），也有 `llm_speculative_decoding.py`、`llm_multilora.py`、`llm_kv_cache_connector.py` 等「按特性」命名的示例。
- `examples/models/core/` 下的每个目录通常只有一个 `README.md`，即「模型运行指南」本身。

**预期结果**：你能回答「我想试投机解码」→进 `examples/llm-api/llm_speculative_decoding.py`；「我想跑 Qwen」→进 `examples/models/core/qwen/README.md`。

#### 4.2.5 小练习与答案

**练习 1**：同事给你一段命令 `trtllm-build ... && python run.py`，说「按这个老博客跑」。根据本讲，你应该怎么做？

> **参考答案**：先看 [examples/README.md:63-78](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/examples/README.md#L63-L78) 的 legacy 提醒，确认这是已不推荐的引擎构建工作流；然后改用 `trtllm-serve <model>` 或 LLM API（`examples/llm-api/`），并参考 `examples/models/core/<对应模型>/README.md` 的当前命令。

**练习 2**：`examples/configs/curated/` 和 `examples/configs/database/` 有何区别？什么时候用哪个？

> **参考答案**：`curated/` 是人工挑选的「快速上手」配置，适合第一次部署照抄；`database/` 是按 GPU 型号、输入/输出长度、并发等维度穷举的 Pareto 优化配置库，适合在确定硬件与流量画像后做精细调优。两者都通过 `trtllm-serve --config <file>.yaml` 消费。

---

### 4.3 `docs/` 的结构

#### 4.3.1 概念说明

`docs/source/` 是文档站的源码，构建后就是官网 `nvidia.github.io/TensorRT-LLM`。TensorRT-LLM 的文档量很大（特性文档几十篇、部署指南十多篇、博客二十多篇），靠一个 **Sphinx `toctree`** 把它们组织成树状导航。掌握 `index.rst` 的 toctree，就等于拿到了整份文档的「目录页」。

#### 4.3.2 核心流程

`docs/source/index.rst` 用若干个带 `:caption:` 的 toctree 把文档分成若干大章，读者顺着章节标题就能定位：

```text
docs/source/
├── index.rst              ← 总目录（toctree 入口）
├── overview.md            ← Getting Started：架构总览
├── quick-start-guide.md   ← Getting Started：快速上手
├── installation/          ← Getting Started：安装
├── deployment-guide/      ← Deployment Guide：按模型的部署指南 + 配方选择器
├── models/                ← Models：支持矩阵、加新模型、VisualGen
├── commands/              ← CLI Reference：trtllm-bench / eval / serve
├── llm-api/               ← API Reference
├── features/              ← Features：attention / kvcache / quantization / sampling …
├── developer-guide/       ← Developer Guide：架构、性能、CI、API 变更 …
├── torch/                 ← PyTorch 后端专题（arch_overview / adding_new_model …）
├── visual-gen/            ← VisualGen 专题
├── blogs/                 ← 技术博客
└── legacy/                ← 迁移/遗留说明
```

一个特别实用的子目录是 `deployment-guide/`：它既有 `index.rst` 里的「配方选择器（recipe selector）」，也用 toctree 罗列了每个模型的部署文档。这意味着「按模型查部署」有固定入口。

#### 4.3.3 源码精读

- [docs/source/index.rst:9-17](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/index.rst#L9-L17) — 「Getting Started」toctree，含 `overview.md`、`quick-start-guide.md`、`installation/`、`supported-hardware.md`。这是新人最先该读的章节。
- [docs/source/index.rst:22-30](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/index.rst#L22-L30) — 「Deployment Guide」toctree，把示例与部署指南收拢在一起，末尾指向 `deployment-guide/index.rst`。
- [docs/source/index.rst:61-91](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/index.rst#L61-L91) — 「Features」toctree，列出了 attention、disagg-serving、kvcache、parallel-strategy、quantization、sampling、speculative-decoding、auto_deploy 等几乎所有特性文档。后续每一篇特性讲义都能在这里找到对应原文。
- [docs/source/deployment-guide/index.rst:22-41](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/deployment-guide/index.rst#L22-L41) — 「Model-Specific Deployment Guides」toctree，按模型罗列部署文档（DeepSeek-R1、Llama3.3-70B、Llama4-Scout、GPT-OSS、Qwen3/3.5、Kimi-K2、GLM-5、MiniMax-M3、Nemotron-3 等）。这正是 4.2 里「按模型」示例要对接的目标。

> 阅读技巧：在 GitHub 上直接打开 `docs/source/index.rst`，沿着 toctree 的 `:caption:` 一层层点下去，比在官网搜索框里盲搜更高效。

#### 4.3.4 代码实践

**实践目标**：学会用 toctree 而非搜索引擎来定位文档。

**操作步骤**：

1. 打开 [docs/source/index.rst](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/index.rst)，找到 `:caption: Features` 与 `:caption: Developer Guide` 两段 toctree。
2. 在 Features 里找出「KV cache」「投机解码」「量化」分别对应哪个 `.md` 文件。
3. 在 Developer Guide 里找出「CI overview」「架构总览（overview）」分别对应哪个文件。
4. 打开 `docs/source/deployment-guide/index.rst`，确认它确实按模型列出了部署文档。

**需要观察的现象**：每个 toctree 条目的路径就是 `docs/source/` 下的相对路径，可直接在仓库里打开对应 Markdown。

**预期结果**：你能在不联网的情况下，仅凭仓库里的 `index.rst` 找到任意一篇文档的源文件。

**待本地验证**：如果你本地装了 Sphinx（`pip install sphinx`），可在 `docs/` 下尝试构建（具体命令见 `docs/` 内的 `conf.py` 与 README，本讲不假定你已构建）。

#### 4.3.5 小练习与答案

**练习 1**：你想了解「如何添加一个新模型」和「PyTorch 后端架构总览」，应该分别读哪两个文档？

> **参考答案**：两者都在 `docs/source/torch/` 下——`docs/source/torch/adding_new_model.md`（加新模型）和 `docs/source/torch/arch_overview.md`（架构总览）。注意它们不在顶层 `features/` 或 `models/`，而在 `torch/` 专题子目录里；`docs/source/models/adding-new-model.md` 也是相关入口。

**练习 2**：`deployment-guide/index.rst` 里有一段「Recipe selector」，它和「Model-Specific Deployment Guides」是什么关系？

> **参考答案**：「Recipe selector」是交互式的「聚合服务（in-flight batching）配置选择器」，适合单机同卡做 prefill+decode 的常见场景；而「Model-Specific Deployment Guides」是按模型逐篇写的详细部署手册，覆盖分离式服务等更复杂场景。二者互补：先查配方选择器拿默认配置，再查对应模型指南看进阶与分离式部署。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「仓库侦察」小任务。

### 任务一：制作你的「仓库目录速查表」

1. 在仓库根执行 `ls -1 --group-directories-first`，把结果抄进一张表。
2. 为每个顶层目录写一句话职责（不要照抄本讲，用你自己的话）。
3. 用三种标记区分：**★主体代码**、**◆外围（示例/文档/测试）**、**◇基础设施**。

**验收标准**：表里至少覆盖 `tensorrt_llm`、`cpp`、`examples`、`docs`、`tests`、`scripts`、`jenkins`、`triton_kernels` 八项，并正确标出 `tensorrt_llm` 和 `cpp` 为主体代码。

### 任务二：建立「模型示例 ↔ 部署文档」的映射

在 `examples/models/core/` 下任选 **3 个**模型目录，找到它们各自对应的部署文档，并说明对应关系。下面是一份已核对的参考映射（你应自己打开文件确认）：

| 模型示例目录 | 对应部署文档（`docs/source/deployment-guide/`） |
|---|---|
| `examples/models/core/deepseek_v3/` | `deployment-guide-for-deepseek-r1-on-trtllm.md` |
| `examples/models/core/llama4/` | `deployment-guide-for-llama4-scout-on-trtllm.md` |
| `examples/models/core/gpt_oss/` | `deployment-guide-for-gpt-oss-on-trtllm.md` |
| `examples/models/core/qwen/` | `deployment-guide-for-qwen3-on-trtllm.md`（另有 `qwen3.5`） |
| `examples/models/core/kimi_k2/` | `deployment-guide-for-kimi-k2-thinking-on-trtllm.md` |
| `examples/models/core/nemotron/` | `deployment-guide-for-nemotron-3-on-trtllm.md` |

**操作步骤**：

1. `ls -1 examples/models/core` 选 3 个模型。
2. 打开对应 `examples/models/core/<模型>/README.md` 的前几段，确认它讲的就是该模型。
3. 打开 [docs/source/deployment-guide/index.rst:22-41](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/deployment-guide/index.rst#L22-L41)，在上面的 toctree 里找到同名部署文档。
4. 用一句话写出二者关系，例如：*「`examples/models/core/deepseek_v3/README.md` 给出跑 DeepSeek 的可执行命令；`deployment-guide-for-deepseek-r1-on-trtllm.md` 给出更系统的部署与调优说明。」*

**预期结果**：你得到一份「示例 ↔ 文档」对照表，日后遇到任何模型都能在两个地方互补查找。

## 6. 本讲小结

- TensorRT-LLM 是 monorepo：**主体是 `tensorrt_llm/`（Python）与 `cpp/`（C++/CUDA 加速）**，外围是 `examples/`、`docs/`、`tests/`，地基是 `scripts/`、`jenkins/`、`docker/`。
- `tensorrt_llm/_torch/` 是 PyTorch 后端的心脏，后续绝大多数进阶讲义都在这里展开。
- `examples/` 按「特性」和「模型」两条轴组织：试功能进 `examples/llm-api/` 等，跑模型进 `examples/models/core/<模型>/README.md`。
- `examples/README.md` 明确标注旧的 `convert_checkpoint.py → trtllm-build → run.py` 是 legacy，新项目应走 `trtllm-serve` 或 LLM API。
- `docs/source/index.rst` 的 toctree 是整份文档的目录页；`deployment-guide/index.rst` 则是「按模型查部署」的固定入口。
- 治理类文件（`AGENTS.md`、`CODING_GUIDELINES.md`、`CONTRIBUTING.md`）是改代码前必读的规则来源。

## 7. 下一步学习建议

本讲只建立了「目录 → 职责」的静态地图，还没有真正进入代码。建议：

- **下一讲 [u2-l2 tensorrt_llm Python 包与公共 API](./u2-l2-python-package-public-api.md)**：钻进 `tensorrt_llm/`，看清 `LLM`、`SamplingParams`、`Mapping` 等公共对象从哪里导出。
- **再下一讲 [u2-l3 C++ 核心与 Python↔C++ 绑定](./u2-l3-cpp-core-and-nanobind.md)**：钻进 `cpp/`，理解 nanobind 如何把 C++ 运行时暴露给 Python。
- 在进入这两讲前，可以先把本讲「综合实践」的速查表做出来，作为日后阅读源码时的随身地图。
- 想提前了解某个特性的，可直接顺着 [docs/source/index.rst](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/index.rst) 的 Features toctree 去读对应的官方文档。
