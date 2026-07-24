# 项目定位与整体架构

## 1. 本讲目标

本讲是 TensorRT-LLM 学习手册的第一讲。读完本讲，你应该能够：

- 用一两句话说清 **TensorRT-LLM 是什么、为什么需要它**（专用 kernel、高效运行时、可扩展的 Python 框架）。
- 理解它的 **PyTorch 原生架构（PyTorch-native）** 是什么含义，以及一条请求是如何从 HuggingFace 模型走到「生成 token」的。
- 区分两条执行后端：**PyTorch（默认）** 与 **AutoDeploy（Beta）**，并知道它们在入口上的差别。
- 认识三条产品线：**LLM API**（离线推理）、**trtllm-serve**（在线服务）、**VisualGen**（图像/视频生成）。
- 对仓库的顶层结构建立一个初步的心智模型，为后续讲义打好基础。

本讲不要求你懂 C++ 或 GPU 编程，只需要对大语言模型（LLM）推理有最基础的概念即可。

---

## 2. 前置知识

在看源码之前，先用最直白的话建立两个直觉。

### 2.1 什么是「LLM 推理」

把一个训练好的大语言模型（比如 Llama、DeepSeek）拿来用，给它一段文字（prompt），让它一个 token 一个 token 地「吐」出回答，这个过程就叫**推理（inference）**。推理和训练不同：训练关心「学得好不好」，推理关心「又快又省地给出结果」。

一次推理可以粗略分成两个阶段：

- **Prefill（预填）阶段**：一次性吃掉整段 prompt，算出每个位置的中间状态（后面会讲的 KV cache）。这一步是「算力密集型」。
- **Decode（解码）阶段**：每次只生成一个新 token，反复进行直到结束。这一步是「显存带宽密集型」。

TensorRT-LLM 的几乎所有优化，最终都服务于「让这两个阶段在 NVIDIA GPU 上跑得更快、更省显存」。

### 2.2 为什么不直接用 PyTorch 跑推理

原生 PyTorch 灵活、易调试，但它为「通用」牺牲了「极致」。LLM 推理有一些特有的瓶颈：

- **注意力（attention）** 计算随序列变长急剧变慢。
- **KV cache** 会吃掉大量显存，需要精细管理。
- 大量请求一起跑时，需要**动态拼批（in-flight batching）** 才能压满 GPU。

TensorRT-LLM 用三类手段解决这些问题：**专用 kernel（attention、GEMM、MoE 等）**、**高效运行时（调度、批管理、KV cache）**、以及一个**可改可扩展的 Python 框架**。这三点正是它的核心价值。

### 2.3 一句话定位

> TensorRT-LLM 是 NVIDIA 的开源库，用专用 kernel、高效运行时和可扩展的 Python 框架，优化 LLM 与 Visual Gen 模型在 NVIDIA GPU 上的推理。

这句话直接来自仓库的「门面」，下一节我们会到源码里验证它。

---

## 3. 本讲源码地图

本讲只读「项目门面 + 架构总览」级别的文档与文件，不会进入具体实现。涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `README.md` | 项目门面：定位、技术栈、技术博客、Getting Started、三条产品线线索 |
| `AGENTS.md` | 面向贡献者的架构速查：后端矩阵、共享 C++ 核心、请求流、关键文件表 |
| `docs/source/overview.md` | 官方产品总览：核心能力、性能、模型支持、高级特性清单 |
| `docs/source/torch/arch_overview.md` | PyTorch 后端架构总览：LLM API、PyExecutor、ModelEngine、Scheduler |
| `.github/tava_architecture_diagram.md` | 全局架构 Mermaid 图（看大图用） |

> 约定：本讲所有源码引用都附永久链接，指向当前 HEAD `f5c2a07052`。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **README 与技术博客**——项目定位、价值与技术栈。
2. **架构总览**——PyTorch 原生架构与请求全链路。
3. **后端矩阵**——PyTorch 与 AutoDeploy 双后端、C++ 共享核心，以及 LLM API / Serving / VisualGen 三条产品线。

---

### 4.1 README 与技术博客：项目定位、价值与技术栈

#### 4.1.1 概念说明

打开一个开源项目，第一件事永远是看 `README.md`。TensorRT-LLM 的 README 在最顶端用一句话点明了项目本质：

> TensorRT LLM optimizes inference for LLMs and Visual Gen models with specialized kernels for common operations, an efficient runtime, and a pythonic framework that enables you to customize and extend the system.

[README.md:L5](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/README.md#L5) —— 项目门面一句话定位（专用 kernel + 高效运行时 + 可扩展 Python 框架）。

这句话把 TensorRT-LLM 的三大价值说全了，和我们在前置知识里讲的「三类手段」完全对应。注意它特别强调了 **Visual Gen**（视觉生成），这说明这个项目已经不只是「LLM 推理库」，而是覆盖 LLM 和扩散模型（Diffusion）生成两条线。

#### 4.1.2 核心流程：从 README 能读出什么

README 本身不是「执行流程」，而是「项目地图」。但它有几个区域特别值得初学者关注：

```text
README 顶部徽章        → 技术栈与版本（Python / CUDA / PyTorch / 发布版本）
Tech Blogs 区块        → 官方深度技术文章（理解优化方向的最佳入口）
TensorRT LLM Overview  → 定位与「PyTorch-native」关键词
Getting Started        → 快速上手入口（Quick Start / 安装 / 支持矩阵 / 基准）
Useful Links           → 生态与衍生（量化模型、Dynamo、AutoDeploy）
```

这个阅读顺序，正好对应「先认人、再看本事、最后找入口」。

#### 4.1.3 源码精读

**① 技术栈徽章**——README 顶部一排徽章直接告诉你依赖的版本：

[README.md:L9-L13](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/README.md#L9-L13) —— 这段徽章说明项目支持 **Python 3.12 / 3.10**、**CUDA 13.2.1**、**PyTorch 2.11.0**，当前发布版本为 **1.3.0rc23**。版本组合在安装时非常重要（下一讲 u1-l2 会专门讲）。

**② 技术博客（Tech Blogs）**——这是理解项目「在优化什么」最省力的入口。最新的几篇：

[README.md:L21-L25](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/README.md#L21-L25) —— Tech Blogs 区块，最新一篇是关于 DeepSeek-V4 在 Blackwell 上的优化。

从博客标题就能看出项目当前的重点方向，例如：MoE 通信优化（One-Sided AlltoAll）、专家并行（Expert Parallelism）、稀疏注意力（Sparse Attention）、CUDA Graph 调优、分离式服务（Disaggregated Serving）、投机解码（Speculative Decoding）。**这些标题就是后续高级讲义的预告片**。

**③ Overview 小节**——README 中段的正式定位：

[README.md:L265-L271](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/README.md#L265-L271) —— 「TensorRT LLM Overview」小节，定义了项目是优化 LLM 与 Visual Gen 推理的开源库，并强调 **Architected on PyTorch** 与 **PyTorch-native architecture**。

这里有两个关键词要记住：

- **Architected on PyTorch**（基于 PyTorch 架构）：模型用原生 PyTorch 代码写，不再是黑盒的编译图。
- **PyTorch-native architecture**（PyTorch 原生架构）：开发者可以用熟悉的 PyTorch 代码直接改运行时、扩展功能。

[README.md:L267](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/README.md#L267) —— 这一句明确列出优化手段：custom kernels for common inference operations（attention, GEMMs, MoE, ...）和 algorithmic runtime optimizations（Prefill-Decode disaggregation, Wide Expert Parallelism, Speculative Decoding 等）。

**④ Getting Started 入口**——告诉你下一步去哪：

[README.md:L274-L283](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/README.md#L274-L283) —— Getting Start 清单：Quick Start Guide（含 Running DeepSeek 示例）、Installation Guide、支持矩阵、基准方法、Release Notes。

**⑤ Useful Links 与 AutoDeploy**——README 底部把 AutoDeploy 列为 beta 后端：

[README.md:L341-L343](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/README.md#L341-L343) —— Useful Links，其中 AutoDeploy 被描述为「A beta backend for TensorRT LLM to simplify and accelerate the deployment of PyTorch models」。这就是我们要区分的「第二个后端」。

> 旁注：`docs/source/overview.md` 给出了更结构化的产品总览，把核心能力归纳为四大块——[Architected on Pytorch](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/overview.md#L11-L15)、State-of-the-Art Performance、Comprehensive Model Support、Advanced Optimization。其中模型支持明确包含三类：语言模型、多模态模型、视觉生成模型（[overview.md:L24-L32](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/overview.md#L24-L32)）。

#### 4.1.4 代码实践

**实践目标**：用「门面 + 博客标题」建立对项目优化方向的全局印象，不写代码、不装环境。

**操作步骤**：

1. 打开 [README.md](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/README.md)，记录顶部徽章里的 Python / CUDA / PyTorch 版本。
2. 滚动到 **Tech Blogs** 区块，把最近 6 篇博客的标题抄下来。
3. 把这些标题按你自己的理解分三类：「与计算 kernel 相关」「与运行时/调度相关」「与服务部署相关」。

**需要观察的现象**：

- 你会发现博客标题高度集中在 **MoE、专家并行、CUDA Graph、分离式服务、投机解码、稀疏注意力** 这几个主题上。

**预期结果**：

- 得到一张「当前优化重点」分类表。这张表里的每一个词，几乎都对应本手册后面的一篇讲义（u10 高级优化、u11 服务部署）。

> 本实践为「源码阅读型实践」，无需 GPU、无需运行，可直接完成。

#### 4.1.5 小练习与答案

**练习 1**：README 顶部徽章显示的发布版本和 PyTorch 版本分别是什么？

**参考答案**：发布版本 1.3.0rc23，PyTorch 2.11.0（见 README.md:L9-L13）。

**练习 2**：用一句话概括 TensorRT-LLM 的核心价值（不能照抄，要用自己的话）。

**参考答案**：它把 NVIDIA GPU 上跑 LLM/视觉生成推理时最耗时的部分（attention、GEMM、MoE 等）换成专用 kernel，再用一个高效又可改的 PyTorch 运行时把它们组织起来，从而又快又灵活。

---

### 4.2 架构总览：PyTorch 原生架构与请求全链路

#### 4.2.1 概念说明

理解完「是什么」，接着要理解「怎么跑」。TensorRT-LLM 的整体架构可以概括为一句话：

> **Python 来调度，C++ 来加速；模型用原生 PyTorch 写。**

官方架构文档 [arch_overview.md](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/torch/arch_overview.md) 开篇就说得很清楚：

[arch_overview.md:L3-L4](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/torch/arch_overview.md#L3-L4) —— 说明 TensorRT LLM 是为 LLM 推理创建优化方案的工具包，PyTorch 也可以作为它的后端（"Besides TensorRT, PyTorch can also serve as the backend"）。

这句话点出了关键：**PyTorch 后端** 是当前主力，用户最高层的入口是 `tensorrt_llm.LLM`。

#### 4.2.2 核心流程：一条请求的生命周期

`AGENTS.md` 用一段极简的伪流程概括了请求从输入到输出的全过程：

[AGENTS.md:L65-L69](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/AGENTS.md#L65-L69) —— 请求流（Request Flow）：

```text
HuggingFace Model → LLM API → Executor (PyTorch/AutoDeploy)
    → Scheduler → Model Forward → Decoder → Sampling → Generated Tokens
```

这条链路是**整个学习手册的主干**，后续每一篇进阶讲义都在展开其中的某一环。把它拆开看：

| 环节 | 含义 | 谁负责 |
|------|------|--------|
| HuggingFace Model | 用户提供模型 checkpoint | HF 生态 |
| LLM API | 用户调用的 Python 入口 `LLM(...)` | Python |
| Executor | 把请求变成可执行的批次 | Python + C++ |
| Scheduler | 决定哪些请求、哪些资源参与本步 | Python 接口 + C++ 实现 |
| Model Forward | 跑一次模型前向，算出 logits | PyTorch 模型 + 专用 kernel |
| Decoder | 把 logits 组织成候选 token | Python |
| Sampling | 从 logits 里采样出最终 token | Python + kernel |
| Generated Tokens | 返回给用户 | Python |

而这一切的「发动机」是一个叫 **PyExecutor** 的单步循环（详见 u3-l2）。架构文档对它的描述：

[arch_overview.md:L17-L31](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/torch/arch_overview.md#L17-L31) —— PyExecutor 的三个关键组件（Model Engine / Decoder / Scheduler）与单步流程。

其中单步流程（每一步都做这些事）是：

1. 从请求队列里取出新请求（如果有）。
2. **调度（schedule）** 一些请求。
3. 为被调度的请求**跑模型前向（forward）**。
4. 用前向输出跑 **decoder**。
5. 为每个请求追加输出 token，并处理已完成的请求。

这种「一步一步（step）」推进的方式，正是**在线/动态批（in-flight batching）** 得以实现的基础：每一步都可以动态地把新请求加进来、把完成的请求踢出去。

#### 4.2.3 源码精读

**① 顶层入口 LLM**：

[arch_overview.md:L6-L15](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/torch/arch_overview.md#L6-L15) —— PyTorch 后端的接口是 `tensorrt_llm.LLM`，并且 LLM 还托管了 **tokenization（分词）** 和 **detokenization（反分词）** 过程。

```python
from tensorrt_llm import LLM
llm = LLM(model=<path_to_llama_from_hf>)
```

注意：用户不需要自己处理分词，`LLM` 把 tokenizer / detokenizer 都接管了——这是它「高级 API」的体现。

**② 两步调度器**：

[arch_overview.md:L43-L52](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/torch/arch_overview.md#L43-L52) —— 调度器分两步：**CapacityScheduler**（判断资源够不够接纳一个请求）+ **MicroBatchScheduler**（挑哪些请求参与本步前向）。文档明确说二者「目前用 C++ 绑定，但接口是 Python 实现的，因此可定制」。

这是 TensorRT-LLM 一个反复出现的设计哲学：**实现可以在 C++（追求性能），接口一定暴露在 Python（追求可改）**。

**③ 资源管理器与 KV cache**：

[arch_overview.md:L54-L69](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/torch/arch_overview.md#L54-L69) —— `ResourceManager` 是多种资源的容器，每个资源继承自 `BaseResourceManager`，有三个关键接口：`prepare_resources`（每步前向前调用）、`update_resources`（每步结束时调用）、`free_resources`（请求结束时调用）。最重要的资源就是 KV cache，对应的 `BaseResourceManager` 是 `KVCacheManager`。

这三个接口（prepare / update / free）会在后续讲 KV cache（u7）和调度（u8）时反复出现，现在先记住这个「三段式生命周期」。

> 想看全局大图，可以打开仓库的 Mermaid 架构图 [.github/tava_architecture_diagram.md](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/.github/tava_architecture_diagram.md)。

#### 4.2.4 代码实践

**实践目标**：把「请求全链路」从抽象文字变成一张你自己的流程图。

**操作步骤**：

1. 阅读 [AGENTS.md:L65-L69](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/AGENTS.md#L65-L69) 的请求流。
2. 阅读 [arch_overview.md:L17-L31](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/docs/source/torch/arch_overview.md#L17-L31) 的 PyExecutor 单步流程。
3. 在纸上（或任何画图工具）画出一张图：从 `LLM.generate()` 出发，经过 Scheduler → Model Forward → Decoder → Sampling → Tokens，并在每个节点旁标注「Python / C++ / PyTorch kernel」。

**需要观察的现象**：

- 你会发现同一条链路里，**调度**偏 Python、**前向计算**偏 PyTorch+kernel、**KV cache 管理底层**偏 C++。这正是「Python 调度 + C++ 加速」的体现。

**预期结果**：

- 得到一张带「责任方」标注的请求时序图。这张图将作为 u3（端到端流程总览）的起点。

> 本实践为「源码阅读 + 画图型实践」，无需 GPU。如果你愿意，可以在图上把 `prepare_resources / update_resources / free_resources` 三个 KV cache 接口标到 forward 的前、后、请求结束处，提前预热 u7 的内容。

#### 4.2.5 小练习与答案

**练习 1**：PyExecutor 的单步流程包含哪 5 个动作？

**参考答案**：① 取新请求；② 调度部分请求；③ 跑模型前向；④ 跑 decoder；⑤ 追加 token 并处理完成的请求（见 arch_overview.md:L25-L31）。

**练习 2**：调度器的两步分别叫什么，各管什么？

**参考答案**：CapacityScheduler 判断资源是否够接纳请求；MicroBatchScheduler 挑选本步参与前向的请求（见 arch_overview.md:L45-L48）。

**练习 3**：`BaseResourceManager` 的三个接口分别在什么时机被调用？

**参考答案**：`prepare_resources` 在每步前向前、`update_resources` 在每步结束、`free_resources` 在请求完成时（见 arch_overview.md:L60-L62）。

---

### 4.3 后端矩阵：双后端、C++ 共享核心与三条产品线

#### 4.3.1 概念说明

前两节我们都在讲「PyTorch 后端」，但其实 TensorRT-LLM 有**两条执行后端**，以及**一个共享的 C++ 核心**把它们托住。`AGENTS.md` 的开头一句话就把这件事说清了：

[AGENTS.md:L3-L4](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/AGENTS.md#L3-L4) —— 「TensorRT-LLM: open-source library for optimized LLM inference on NVIDIA GPUs. Python and C++ codebase with PyTorch and AutoDeploy execution paths.」

也就是说：代码库是「Python + C++」，执行路径有「PyTorch」和「AutoDeploy」两条。理解这一点，是分清整个仓库结构的关键。

#### 4.3.2 核心流程：后端矩阵 + 共享核心

`AGENTS.md` 用一张表概括两条后端的差异：

[AGENTS.md:L52-L57](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/AGENTS.md#L52-L57) —— Backends 表，整理如下：

| 后端 | 状态 | 入口 | 关键路径 |
|------|------|------|----------|
| **PyTorch** | 默认（Default） | `TorchLlmArgs` | `_torch/pyexecutor/` → `PyExecutor` → PyTorch Engine |
| **AutoDeploy** | Beta | `_torch/auto_deploy/` 的 shim | `ad_executor.py` → 适配 `PyExecutor` → 图变换 + torch.export |

要点：

- **PyTorch 是默认后端**：前面讲的 `LLM`、`PyExecutor` 都属于它。
- **AutoDeploy 是 Beta 后端**：它通过一个 **shim（垫片）** 适配 `PyExecutor`，核心做法是对 PyTorch 模型做 **FX 图变换（graph transforms）** 和 **torch.export**，目标是「简化并加速 PyTorch 模型的部署」（见 README.md:L343）。
- **两者共享同一个 C++ 核心**：

[AGENTS.md:L59-L63](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/AGENTS.md#L59-L63) —— 经 **nanobind** 暴露的共享 C++ 组件，包括「调度流水线（Scheduler → BatchManager → KV Cache Manager）」和「解码流水线（Decoder → Sampling）」。

> 术语解释：**nanobind** 是一个把 C++ 代码绑定到 Python 的工具（类似更轻量的 pybind11）。TensorRT-LLM 用它把高性能 C++ 实现（调度、批管理、KV cache、采样）暴露给 Python 调用。这也是为什么我们在 4.2 里反复看到「接口在 Python、实现在 C++」。

把后端矩阵和共享核心画成图就是：

```text
                    ┌─────────────────────────────────┐
   用户 LLM API ───▶ │  两条执行后端                    │
                    │  ① PyTorch（默认）               │
                    │  ② AutoDeploy（Beta，图变换）    │
                    └───────────────┬─────────────────┘
                                    │ 都跑在同一个 ↓
                    ┌─────────────────────────────────┐
                    │  共享 C++ 核心（nanobind 暴露）   │
                    │  · 调度流水线                     │
                    │  · 解码流水线                     │
                    └─────────────────────────────────┘
```

#### 4.3.3 源码精读

**① 服务（Serving）产品线**：

[AGENTS.md:L71-L74](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/AGENTS.md#L71-L74) —— `trtllm-serve` 是 OpenAI 兼容的 REST + gRPC 服务，支持所有后端；**分离式服务（Disaggregated serving）** 把 prefill 与 decode 分到不同 GPU，KV cache 通过 NIXL（默认）/ UCX / MPI 交换。

这就引出了**第二条产品线：在线服务**。和离线的 `LLM.generate()`（第一条产品线：LLM API）相对，`trtllm-serve` 把推理包装成一个符合 OpenAI 协议的网络服务，可以直接对接上层应用。

**② VisualGen 产品线**：

[AGENTS.md:L102-L116](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/AGENTS.md#L102-L116) —— VisualGen 是与 LLM 并列的一条产品线，面向基于 DiT（Diffusion-Transformer）的图像/视频生成。它**不是 LLM 后端**，有自己的引擎、参数和输出，但会复用 PyTorch 后端的算子与 kernel（attention、量化、并行）。

其中关键入口是（[AGENTS.md:L109-L111](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/AGENTS.md#L109-L111)）：

- 公共 Python API：`from tensorrt_llm import VisualGen, VisualGenArgs, VisualGenParams`
- 服务 CLI：`trtllm-serve --model <HF id> --visual_gen_args <YAML path>`

我们可以在顶层包里验证这个导出确实存在：

[\_\_init\_\_.py:L127-L129](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/tensorrt_llm/__init__.py#L127-L129) —— `tensorrt_llm` 顶层包确实导出了 `VisualGen / VisualGenArgs / VisualGenParams / VisualGenResult` 等类，说明 VisualGen 是与 LLM 平级的一等公民产品线。

至此，三条产品线齐了：

| 产品线 | 入口 | 用途 | 对应产品 |
|--------|------|------|----------|
| **LLM API** | `from tensorrt_llm import LLM` | 离线批量推理 | 语言/多模态模型 |
| **trtllm-serve** | `trtllm-serve <model> --port 8000` | 在线 OpenAI 兼容服务 | 语言/多模态/视觉生成 |
| **VisualGen** | `from tensorrt_llm import VisualGen` | 图像/视频生成（DiT） | FLUX、Wan 等 |

#### 4.3.4 代码实践

**实践目标**：分清两条后端入口，并验证三条产品线的入口符号。

**操作步骤**：

1. 打开 [AGENTS.md:L52-L57](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/AGENTS.md#L52-L57)，把两条后端的「入口」和「关键路径」抄到一张对比表。
2. 用文字回答两个问题：
   - PyTorch 后端的入口符号是什么？（提示：`TorchLlmArgs`）
   - AutoDeploy 后端通过什么机制适配 `PyExecutor`？（提示：shim + 图变换 + torch.export）
3. 打开 [\_\_init\_\_.py:L127-L129](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/tensorrt_llm/__init__.py#L127-L129)，确认 VisualGen 系列类确实从顶层包导出。

**需要观察的现象**：

- AutoDeploy 的入口路径里出现了 `shim/ad_executor.py`，说明它不是「另起炉灶」，而是「套在 PyExecutor 外面的一层」。

**预期结果**：

- 一张「双后端入口对比表」+ 一句关于 shim 机制的说明。这两点将在 u12-l1（AutoDeploy）深入展开。

> 本实践为「源码阅读型实践」，无需 GPU。若你的环境已安装 tensorrt_llm，可在 Python 里 `import tensorrt_llm; print([n for n in dir(tensorrt_llm) if n.startswith(('LLM','VisualGen'))])` 验证导出符号（若 import 失败，属正常，标注「待本地验证」即可，不要假装运行成功）。

#### 4.3.5 小练习与答案

**练习 1**：PyTorch 后端和 AutoDeploy 后端各自的入口是什么？哪个是默认后端？

**参考答案**：PyTorch 后端入口是 `TorchLlmArgs`，是默认后端；AutoDeploy 后端入口是 `_torch/auto_deploy/` 的 shim（`ad_executor.py`），状态为 Beta（见 AGENTS.md:L54-L57）。

**练习 2**：两条后端共享的 C++ 核心包含哪两条「流水线」？通过什么工具暴露给 Python？

**参考答案**：调度流水线（Scheduler → BatchManager → KV Cache Manager）和解码流水线（Decoder → Sampling）；通过 nanobind 暴露给 Python（见 AGENTS.md:L59-L63）。

**练习 3**：TensorRT-LLM 有哪三条产品线？分别对应什么场景？

**参考答案**：LLM API（离线推理）、trtllm-serve（OpenAI 兼容在线服务）、VisualGen（图像/视频生成，非 LLM 后端）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「项目定位速览卡」小任务：

**任务**：假设你要向一个完全没听过 TensorRT-LLM 的同事用 5 分钟介绍这个项目，请基于本讲源码产出一份「一页速览」，包含以下要素：

1. **一句话定位**（必须包含：专用 kernel、高效运行时、可扩展 Python 框架，引用 [README.md:L5](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/README.md#L5)）。
2. **一张请求全链路图**（HF Model → LLM API → Executor → Scheduler → Model Forward → Decoder → Sampling → Tokens，引用 [AGENTS.md:L65-L69](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/AGENTS.md#L65-L69)），并标注每个环节由 Python 还是 C++/kernel 负责。
3. **一张双后端 + 共享核心的架构小图**（引用 [AGENTS.md:L52-L63](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/AGENTS.md#L52-L63)），注明 PyTorch 为默认、AutoDeploy 为 Beta、二者共享 nanobind 暴露的 C++ 核心。
4. **三条产品线一行总结**（LLM API / trtllm-serve / VisualGen），各给一个入口符号。
5. **三个值得继续深入的方向**（从 Tech Blogs 标题里挑，引用 [README.md:L21-L25](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/README.md#L21-L25)）。

**验收标准**：同事看完这一页，能回答出「TensorRT-LLM 是什么、请求怎么走、有几个后端、有几条产品线」这四个问题。

> 这是一个纯文档/画图型综合实践，全程不需要 GPU 或运行环境，但它输出的「速览卡」会作为你后续阅读每一篇讲义时的「目录页」。

---

## 6. 本讲小结

- **项目定位**：TensorRT-LLM 是 NVIDIA 开源的 LLM/Visual Gen 推理优化库，三大价值是专用 kernel、高效运行时、可扩展的 Python 框架（[README.md:L5](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/README.md#L5)）。
- **架构哲学**：PyTorch 原生——模型用原生 PyTorch 写，运行时可改可扩展（[README.md:L265-L271](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/README.md#L265-L271)）。
- **请求全链路**：HF Model → LLM API → Executor → Scheduler → Model Forward → Decoder → Sampling → Tokens（[AGENTS.md:L65-L69](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/AGENTS.md#L65-L69)），由 PyExecutor 一步步推进。
- **双后端**：PyTorch（默认，入口 `TorchLlmArgs`）与 AutoDeploy（Beta，通过 shim 适配 PyExecutor 并做图变换 + torch.export），二者共享 nanobind 暴露的 C++ 核心（[AGENTS.md:L52-L63](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/AGENTS.md#L52-L63)）。
- **三条产品线**：LLM API（离线推理）、trtllm-serve（OpenAI 兼容在线服务，支持分离式部署）、VisualGen（图像/视频生成，非 LLM 后端）。
- **设计口诀**：实现可在 C++（追求性能），接口必在 Python（追求可改）——这一点贯穿 KV cache、调度器等所有子系统。

---

## 7. 下一步学习建议

本讲只建立了「高空视图」，还没有真正运行任何东西。建议按以下顺序继续：

1. **u1-l2 安装、容器与从源码构建**：动手把环境搭起来，确认 CUDA / PyTorch / Python 版本组合（呼应本讲 4.1.3 的徽章）。
2. **u1-l3 首次运行：LLM API 与 trtllm-serve**：真正跑通第一个模型，把本讲的「请求全链路」在真实运行中看一遍。
3. **u2 仓库结构与代码地图**：从「门面」下钻到 `tensorrt_llm` 包、`cpp` 核心等具体目录，建立完整心智模型。
4. 想提前感受高级主题，可先挑一篇 Tech Blog（[README.md:L21-L25](https://github.com/NVIDIA/TensorRT-LLM/blob/f5c2a07052f2324c23b3235a09a3a72120bc68a5/README.md#L21-L25)）泛读，建立对「优化方向」的直觉，等到 u10/u11 时再回来精读。
