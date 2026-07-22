# SGLang 是什么：定位与核心能力

> 本讲是整个学习手册的第一篇。你不需要任何 SGLang 背景，只要跟着读 README 和依赖清单，就能建立起对「SGLang 是什么、能做什么、由哪些子系统组成」的整体印象。后续每一篇讲义都会在这个骨架上往里填充细节。

## 1. 本讲目标

读完本讲，你应该能够：

- 用一句话说清 SGLang 解决了什么问题，以及它和 vLLM、TensorRT-LLM 这类推理框架的定位差异。
- 复述 README 列出的 5 个以上核心特性，并大致说出每一项的含义。
- 看着 `python/pyproject.toml` 的依赖清单，判断出 SGLang 站在哪些开源项目的肩膀上（attention、grammar、量化、通信等）。
- 知道项目支持的硬件范围与模型范围，明白它为什么能成为业界事实标准之一。
- 在仓库里快速定位「某个特性大概住在哪个目录」，为后续精读源码打基础。

## 2. 前置知识

本讲面向零基础读者，但有几个概念先建立会更容易理解：

- **大语言模型（LLM）推理**：把一个模型权重加载到显存里，接收一段输入文本（prompt），逐个 token 地生成输出。推理框架（serving framework）就是把这件事工程化：管理显存、批处理请求、提供网络接口。
- **吞吐（throughput）与延迟（latency）**：吞吐指单位时间能产出多少 token，延迟指单个请求要等多久。推理框架的一大核心目标就是在两者之间找平衡。
- **前缀缓存（prefix caching）**：很多请求会共享相同的前缀（比如相同的 system prompt）。把这段前缀算出的中间结果（KV cache）缓存起来复用，能大幅加速。
- **批处理（batching）**：把多个请求凑成一批一起算，能更充分利用 GPU。连续批处理（continuous batching）允许新请求随时插入正在跑的批次。
- **并行（parallelism）**：当一个模型放不下一张卡时，需要把模型或数据切分到多张卡上，常见的有张量并行（TP）、数据并行（DP）、流水线并行（PP）、专家并行（EP）。

不熟悉这些术语没关系，本讲只用它们来解释 SGLang 的「卖点」，真正的机制细节会在后续单元展开。

## 3. 本讲源码地图

本讲只读两个文件，但它们是理解整个项目最高效的入口：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/README.md) | 项目的「自我介绍」：About 章节给出定位与核心特性清单，是我们理解 SGLang 卖点的第一手材料。 |
| [python/pyproject.toml](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/pyproject.toml) | Python 打包与依赖清单：声明了项目名、入口命令、运行时依赖、可选依赖（diffusion/ray/tracing/test 等），能反推出技术栈与生态依赖。 |

此外，本讲的实践环节会让你把特性映射到下面的源码目录（后续单元会逐个深入）：

- `python/sglang/srt/managers/` —— 调度器、Tokenizer/Detokenizer 等多进程管理
- `python/sglang/srt/mem_cache/` —— KV 缓存与 RadixAttention 的基数树缓存
- `python/sglang/srt/speculative/` —— 投机解码
- `python/sglang/srt/disaggregation/` —— Prefill-Decode 分离部署
- `python/sglang/srt/distributed/` —— 张量/数据并行
- `python/sglang/srt/constrained/` —— 结构化输出（文法约束）
- `python/sglang/srt/layers/quantization/` —— 量化方案
- `python/sglang/srt/lora/` —— 多 LoRA 批量服务

## 4. 核心概念与源码讲解

### 4.1 SGLang 的定位：高性能推理服务框架

#### 4.1.1 概念说明

SGLang（发音类似「S-G-Lang」）是一个面向**大语言模型与多模态模型**的**高性能推理服务框架（serving framework）**。它要解决的核心问题是：

> 如何在一台机器到大型集群之间，把模型推理做得**又快（低延迟、高吞吐）又省（少占显存）又稳（能服务化、能扩展、能复用）**。

它和 vLLM、TensorRT-LLM 同属一类工具——你把模型路径交给它，它帮你管理显存、批处理、并行、网络接口，让你通过 HTTP（OpenAI 兼容 API）或进程内 Engine 调用模型。SGLang 的差异化在于一套围绕 **RadixAttention 前缀复用 + 零开销调度器**建立的运行时设计，以及对大规模分布式部署（分离式、大规模 EP）的一等支持。

#### 4.1.2 核心要点

从「用户视角」看，SGLang 提供两个抽象层：

1. **前端语言（frontend / `sglang.lang`）**：一套 Python DSL，用 `gen`、`select`、`function` 等原语描述结构化生成程序，并自动抽取共享前缀以提升缓存命中。
2. **后端运行时（runtime / `sglang.srt`）**：真正干活的服务进程，负责分词、调度、前向计算、采样、解码、流式返回。

这「一前一后」就是 `python/sglang/` 下的两大子包。后续讲义中你会反复看到 `lang/` 和 `srt/` 这两个名字。

#### 4.1.3 源码精读

README 的 About 章节用一句话点题。下面是定位的原始描述：

README.md:61-63 —— About 开篇给出项目定位：

[README.md:61-63](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/README.md#L61-L63)

```markdown
## About
SGLang is a high-performance serving framework for large language models and multimodal models.
It is designed to deliver low-latency and high-throughput inference across a wide range of setups, from a single GPU to large distributed clusters.
```

这段话有三个关键词：**high-performance**（高性能）、**low-latency / high-throughput**（低延迟高吞吐）、**single GPU to large distributed clusters**（从单卡到大规模集群）。它圈定了 SGLang 的野心——不只是「能跑模型」，而是「能在任意规模上跑得快」。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：用一个工程化句子锁定 SGLang 的定位。
2. **操作步骤**：打开 README，阅读 `## About` 标题下的第一段（约两行），以及 `## Adoption and Sponsorship` 中提到「trillions of tokens in production each day」「over 400,000 GPUs worldwide」的句子。
3. **观察现象**：注意它强调的是「serving framework」而不是「training framework」，这决定了它的设计取舍（一切为推理时延和吞吐服务）。
4. **预期结果**：你能写出类似「SGLang 是一个把 LLM/多模态模型从单卡到集群都跑得又快又稳的开源推理服务框架」的一句话定义。

> 本实践不需要运行命令，属于源码阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：SGLang 是训练框架还是推理框架？为什么 README 这样定位？
**参考答案**：是推理（服务）框架。README 用的是 serving framework，强调 low-latency / high-throughput inference，目标是在生产中「提供推理服务」，而不是训练模型。

**练习 2**：「from a single GPU to large distributed clusters」这句话对理解 SGLang 有什么意义？
**参考答案**：它说明 SGLang 不是只做小模型本地推理，也不是只做超大规模部署，而是要在「单卡」到「大规模分布式集群」这个连续谱上都可用，因此它的并行、分离式部署等能力是核心特性而非附属功能。

---

### 4.2 核心能力清单：Fast Runtime 的特性矩阵

#### 4.2.1 概念说明

README 的核心特性集中在 About 的一段 `Fast Runtime` 列表里。这一段几乎就是 SGLang 的「特性总目录」，后续每一篇讲义都对应其中的一两项。理解这一段，等于拿到了整本学习手册的目录。

#### 4.2.2 特性到子系统的映射（核心流程）

把一条请求送进 SGLang 运行时，它会依次经过这些子系统，每个子系统对应一个或多个特性：

```text
请求进入
  │
  ▼
[分词/路由]  TokenizerManager / DataParallelController   ← continuous batching、cache-aware load balancer
  │
  ▼
[调度]      Scheduler (zero-overhead scheduler)          ← chunked prefill、continuous batching
  │
  ▼
[缓存命中]  RadixCache (RadixAttention prefix cache)      ← RadixAttention、paged attention
  │
  ▼
[前向计算]  ModelRunner + attention backend               ← quantization、TP/PP/EP/DP
  │           （可选）speculative draft+verify             ← speculative decoding
  │           （可选）多 LoRA 注入                          ← multi-LoRA batching
  │           （可选）grammar mask                          ← structured outputs
  ▼
[采样解码]  Sampler / Detokenizer                         ← structured outputs
  │
  ▼
流式返回
```

这张图不用背，它的作用是让你明白：README 里那一长串特性，其实分布在请求生命周期的不同阶段。本手册 U2～U6 就是按这条流水线展开的。

#### 4.2.3 源码精读

README.md:64-66 —— About 给出核心特性清单，其中 `Fast Runtime` 这一项一口气列出了运行时的所有能力：

[README.md:64-66](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/README.md#L64-L66)

```markdown
Its core features include:

- **Fast Runtime**: Provides efficient serving with RadixAttention for prefix caching, a zero-overhead CPU scheduler, prefill-decode disaggregation, speculative decoding, continuous batching, paged attention, tensor/pipeline/expert/data parallelism, structured outputs, chunked prefill, quantization (FP4/FP8/INT4/AWQ/GPTQ), and multi-LoRA batching.
```

把这一行拆开，就是下面的特性—目录对照表（路径均相对仓库根目录，已在本讲编写时核对存在）：

| 特性 | 含义（一句话） | 对应源码目录 |
| --- | --- | --- |
| RadixAttention (prefix caching) | 用基数树自动复用公共前缀的 KV 缓存 | `python/sglang/srt/mem_cache/radix_cache.py`、`python/sglang/srt/layers/radix_attention.py` |
| zero-overhead CPU scheduler | 让 CPU 调度与 GPU 计算重叠，调度几乎零开销 | `python/sglang/srt/managers/scheduler.py`、`python/sglang/srt/managers/overlap_utils.py` |
| prefill-decode disaggregation | 把长 prefill 与 decode 拆到不同 worker | `python/sglang/srt/disaggregation/` |
| speculative decoding | 草稿模型/语料预测候选 token，主模型批量验证 | `python/sglang/srt/speculative/` |
| continuous batching | 新请求可随时插入正在跑的批次 | `python/sglang/srt/managers/scheduler.py`、`schedule_policy.py` |
| paged attention | 分块（paged）管理 KV 显存，减少碎片 | `python/sglang/srt/mem_cache/memory_pool.py` |
| tensor/pipeline/expert/data parallelism | TP/PP/EP/DP 多种切分方式 | `python/sglang/srt/distributed/`、`python/sglang/srt/layers/moe/` |
| structured outputs | 用文法约束保证输出符合 JSON/正则/Schema | `python/sglang/srt/constrained/` |
| chunked prefill | 把长 prefill 切块与 decode 混批 | `python/sglang/srt/managers/scheduler.py` |
| quantization (FP4/FP8/INT4/AWQ/GPTQ) | 多种量化降低显存与提升速度 | `python/sglang/srt/layers/quantization/` |
| multi-LoRA batching | 同一批服务多个 LoRA 适配器 | `python/sglang/srt/lora/` |

> 说明：上表的「对应源码目录」是本讲作者在仓库中实际核对存在的路径，用于帮你建立「特性住在哪」的直觉；每个目录的具体机制会在后续对应单元精讲。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：把 README 的特性列表与仓库目录对应起来。
2. **操作步骤**：
   - 在 README 中找到 4.2.3 引用的那一行 `Fast Runtime`。
   - 任选 3 个特性（例如 RadixAttention、speculative decoding、quantization）。
   - 用编辑器或 `git ls-files | grep <关键字>` 在仓库里确认它们对应的目录确实存在。
3. **观察现象**：你会发现几乎每个特性名都能在 `python/sglang/srt/` 下找到一个同名或近名的目录/文件。
4. **预期结果**：列出一张「特性 → 目录」对照表（3 条即可），确认它们真实存在于源码树中。

> 本实践属于源码阅读型，不涉及运行推理。

#### 4.2.5 小练习与答案

**练习 1**：`continuous batching` 和 `chunked prefill` 都和「批」有关，它们解决的问题有何不同？
**参考答案**：continuous batching 解决的是「新请求能否随时加入正在跑的批次」以提高并发；chunked prefill 解决的是「一个很长的 prefill 会不会独占 GPU、阻塞 decode」——把长 prefill 切成小块，让 prefill 和 decode 能混批，兼顾吞吐与延迟。

**练习 2**：为什么 `structured outputs`（结构化输出）会被列为运行时核心特性，而不是「随便用个后处理就行」？
**参考答案**：因为结构化输出需要在**采样阶段**就对词表做掩码（mask），保证每一步只能采样出符合文法（JSON/正则/Schema）的 token，这必须在推理框架内部完成。SGLang 在 `constrained/` 下集成了 xgrammar、outlines、llguidance 等文法后端，所以它是运行时能力而非外挂工具。

---

### 4.3 模型、硬件与生态定位

#### 4.3.1 概念说明

光跑得快还不够，一个推理框架要成为「事实标准」，还得做到两点：**支持的模型够多**（开箱即用），**支持的硬件够广**（不绑定单一厂商）。SGLang 在这两点上都下足了功夫，并把自己定位为 **RL（强化学习）与后训练的 rollout 后端骨干**——这是它区别于「只做 API 服务」的框架的重要身份。

#### 4.3.2 核心要点

SGLang 的「广度」可以从三个维度看：

1. **模型广度**：覆盖主流开源 LLM（Llama/Qwen/DeepSeek/Kimi/GLM/GPT/Gemma/Mistral 等）、embedding 模型、reward 模型，乃至扩散模型（WAN/Qwen-Image），并兼容大多数 Hugging Face 模型与 OpenAI API。
2. **硬件广度**：NVIDIA（GB200/B300/H100/A100/Spark/5090）、AMD（MI355/MI300）、Intel Xeon CPU、Google TPU、Ascend NPU 等。
3. **生态身份**：作为 RL rollout 后端被 AReaL、Miles、slime、Tunix、verl 等后训练框架采用。

#### 4.3.3 源码精读

README.md:67-70 —— 三条特性分别对应模型、硬件、社区/RL 后端定位：

[README.md:67-70](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/README.md#L67-L70)

```markdown
- **Broad Model Support**: Supports a wide range of language models (Llama, Qwen, DeepSeek, Kimi, GLM, GPT, Gemma, Mistral, etc.), embedding models (e5-mistral, gte, mcdse), reward models (Skywork), and diffusion models (WAN, Qwen-Image), with easy extensibility for adding new models. Compatible with most Hugging Face models and OpenAI APIs.
- **Extensive Hardware Support**: Runs on NVIDIA GPUs (GB200/B300/H100/A100/Spark/5090), AMD GPUs (MI355/MI300), Intel Xeon CPUs, Google TPUs, Ascend NPUs, and more.
- **Active Community**: ... powering over 400,000 GPUs worldwide.
- **RL & Post-Training Backbone**: SGLang is a proven rollout backend used for training many frontier models, with native RL integrations and adoption by well-known post-training frameworks such as AReaL, Miles, slime, Tunix, verl and more.
```

注意 `RL & Post-Training Backbone` 这一条——它解释了为什么 SGLang 会内置 `update_weights`（权重热更新）、异步批量推理等接口：这些是 RL 训练循环里「rollout 一批、再更新策略权重」所必需的能力。相关的进程内入口在 `python/sglang/srt/entrypoints/engine.py`（后续 U1-L4、U12-L3 会讲）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：感受「模型广度」在源码里的物理体现。
2. **操作步骤**：
   - 在仓库中定位模型实现目录 `python/sglang/srt/models/`（本手册 U5-L5 会精读其中的 `llama.py`）。
   - 用 `git ls-files 'python/sglang/srt/models/*.py' | head -30` 列出部分模型文件。
3. **观察现象**：你会看到一系列以模型家族命名的文件（llama、qwen、deepseek 等），印证 README 说的 broad model support。
4. **预期结果**：记录你看到的前 5 个模型文件名，并和 README 列出的模型清单做对照。

> 本实践属于源码阅读型；若本地没有 clone 完整仓库，可改为在 GitHub 网页上浏览 `python/sglang/srt/models/` 目录。

#### 4.3.5 小练习与答案

**练习 1**：SGLang 把自己定位为「RL & Post-Training Backbone」，这对它的接口设计有什么影响？
**参考答案**：它必须支持「在训练循环中反复更新模型权重」「高效跑大量并发 rollout 请求」「与训练框架交换状态」等能力，因此会内置权重热更新、异步批量推理、Engine 进程内接口等，这些都是纯 API 服务框架不一定需要的。

**练习 2**：README 说 SGLang「Compatible with most Hugging Face models and OpenAI APIs」，这两层兼容分别意味着什么？
**参考答案**：「兼容 HF 模型」指可以直接加载 HuggingFace 上的模型权重与配置；「兼容 OpenAI API」指对外提供 `/v1/chat/completions` 等与 OpenAI 一致的 HTTP 接口，使得原本调用 OpenAI 的客户端可以几乎无缝切换到自部署的 SGLang 服务。

---

### 4.4 技术栈与依赖概览

#### 4.4.1 概念说明

看一个项目「站在谁的肩膀上」，最直接的方式就是读它的依赖清单。SGLang 的 `python/pyproject.toml` 里列出了运行时依赖（dependencies）和若干可选依赖（optional-dependencies）。这些依赖名字会反复出现在后续源码里——提前认识它们，能大大降低阅读源码的陌生感。

#### 4.4.2 核心要点（依赖与能力的关系）

把关键依赖按「能力域」分组，就能看出 SGLang 的技术选型：

| 能力域 | 代表依赖 | 在 SGLang 里的角色 |
| --- | --- | --- |
| 深度学习后端 | `torch`、`torchvision`、`torchao` | 模型计算与张量运算的基石 |
| 注意力/算子 | `flashinfer_python`、`flash-attn-4`、`sglang-kernel` | 高效 attention 与自定义 CUDA/Triton 算子 |
| 文法/结构化输出 | `xgrammar`、`outlines`、`llguidance`、`interegular` | `constrained/` 结构化解码的多种后端 |
| 量化/压缩 | `compressed-tensors`、`gguf` | 解析各种量化权重格式 |
| 服务/Web | `fastapi`、`uvicorn`、`uvloop`、`aiohttp` | HTTP 服务与 OpenAI 兼容 API |
| 进程通信 | `pyzmq`、`msgspec` | 多进程（Tokenizer/Scheduler/Detokenizer）之间的 ZMQ 消息 |
| 可观测性 | `prometheus-client`、`py-spy` | 指标导出与性能剖析 |
| 分布式 | `ray`（可选） | 大规模分布式调度后端 |

#### 4.4.3 源码精读

python/pyproject.toml:5-10 —— 项目元信息：包名 `sglang`，描述「fast serving framework for large language models and vision language models」，要求 Python ≥ 3.10：

[python/pyproject.toml:5-10](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/pyproject.toml#L5-L10)

```toml
[project]
name = "sglang"
dynamic = ["version"]
description = "SGLang is a fast serving framework for large language models and vision language models."
requires-python = ">=3.10"
```

python/pyproject.toml:18-91 —— 运行时核心依赖（节选关键几行），从中可见上面表格里的分组：

[python/pyproject.toml:18-91](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/pyproject.toml#L18-L91)

```toml
dependencies = [
  "aiohttp",
  ...
  "flashinfer_python[cu13]==0.6.14",
  ...
  "msgspec",
  ...
  "pyzmq>=25.1.2",
  ...
  "torch==2.11.0",
  ...
  "transformers==5.12.1",
  "xgrammar==0.2.1",
  ...
]
```

> 注意：依赖版本会随项目演进变化（例如这里的 `torch==2.11.0`、`transformers==5.12.1` 是当前 HEAD 的快照）。学习时重点是「依赖名 → 能力域」的映射，而不是死记版本号。

python/pyproject.toml:188-190 —— 入口命令：装好 sglang 后，`sglang` 和 `killall_sglang` 两个命令行工具就来自这里。后续 U1-L2 会用到 `sglang serve`：

[python/pyproject.toml:188-190](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/pyproject.toml#L188-L190)

```toml
[project.scripts]
sglang = "sglang.cli.main:main"
killall_sglang = "sglang.cli.killall:main"
```

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：通过依赖清单反推 SGLang 的技术栈边界。
2. **操作步骤**：打开 `python/pyproject.toml`，浏览 `[project] dependencies` 与 `[project.optional-dependencies]`（注意里面有 `diffusion`、`ray`、`tracing`、`test` 等多个可选组）。
3. **观察现象**：注意 `diffusion`、`ray`、`tracing` 是「可选依赖」，说明扩散模型、Ray 分布式、链路追踪是「按需启用」的能力，不是核心运行时必需。
4. **预期结果**：把依赖分成「核心运行时」与「可选扩展」两组，每组各写出 3 个包名。

> 本实践属于源码阅读型，无需安装或运行。

#### 4.4.5 小练习与答案

**练习 1**：`pyzmq` 和 `msgspec` 这两个依赖放在一起，能猜出 SGLang 运行时的什么架构特征？
**参考答案**：`pyzmq` 是 ZMQ 消息库，`msgspec` 是高性能的结构体序列化库。两者一起出现，强烈暗示 SGLang 运行时是**多进程架构**——进程之间通过 ZMQ 传递 `msgspec` 结构化消息来通信（这正是后续 U2-L1 要讲的 io_struct 多进程协议）。

**练习 2**：为什么 `diffusion` 被放在 `optional-dependencies` 而不是 `dependencies`？
**参考答案**：因为扩散模型（图像/视频生成）是一类特定用途，多数 LLM 推理用户用不到它。把它设为可选依赖，可以让核心安装包更轻、依赖更少，需要用扩散能力的人再额外安装 `sglang[diffusion]`。

---

## 5. 综合实践

> 这是本讲的主实践，对应学习目标里「理解 SGLang 与其他框架的差异」。

**实践目标**：把本讲读到的「定位 + 特性 + 目录」串成你自己的认知。

**操作步骤**：

1. 重读 README 的 `## About` 章节（[README.md:61-71](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/README.md#L61-L71)）。
2. 选一个你最熟悉的对比对象（vLLM、TensorRT-LLM、TGI、Ollama 等任选其一）。
3. 写一段话（150 字以内），给出 SGLang 相比它的 **三个差异化优势**。
4. 对这三个优势中的每一项，标注它对应的**源码目录位置**（参考 4.2.3 的对照表）。

**需要观察的现象**：写完之后检查——你标注的每个目录是否真的存在于 `python/sglang/srt/` 下？你的三个优势是否能各对应到一个不同的子系统？

**预期结果**：一段对比文字 + 一张「优势 → 源码目录」对照表。例如（仅供参考格式，内容因人而异）：

```text
优势 1：RadixAttention 自动复用公共前缀 KV → python/sglang/srt/mem_cache/radix_cache.py
优势 2：零开销调度器让 CPU/GPU 重叠 → python/sglang/srt/managers/scheduler.py
优势 3：原生 Prefill-Decode 分离部署 → python/sglang/srt/disaggregation/
```

> 说明：本实践不要求运行推理命令，重点是建立「特性 ↔ 源码位置」的映射。如果你已有 GPU 环境，也可以在完成本讲后进入 U1-L2 真正启动一次服务。

## 6. 本讲小结

- SGLang 是一个面向 LLM/多模态模型的**高性能推理服务框架**，目标是从单卡到大规模集群都做到低延迟、高吞吐。
- README 的 `Fast Runtime` 一行就是 SGLang 的特性总目录：RadixAttention、零开销调度器、Prefill-Decode 分离、投机解码、连续批处理、paged attention、TP/PP/EP/DP、结构化输出、chunked prefill、量化、多 LoRA。
- 每个特性几乎都能在 `python/sglang/srt/` 下找到一个对应目录——后续讲义就是逐个深入这些目录。
- SGLang 的差异化身份之一是 **RL/后训练 rollout 后端**，这决定了它会内置权重热更新、异步批量推理等接口。
- `python/pyproject.toml` 的依赖清单揭示了技术栈：`pyzmq + msgspec`（多进程通信）、`flashinfer/flash-attn/sglang-kernel`（算子）、`xgrammar/outlines/llguidance`（结构化输出）、`fastapi/uvicorn`（HTTP 服务）。
- 入口命令 `sglang`（即 `sglang serve`）来自 `[project.scripts]`，是下一讲启动服务的起点。

## 7. 下一步学习建议

本讲建立了「SGLang 是什么、特性住哪」的全局视图，但还没真正跑起来。建议下一步：

- **必读下一篇**：[U1-L2 安装与首次运行](u1-l2-install-and-first-run.md)——动手用 `sglang serve` 启动一个小模型服务，并发送第一个请求，让抽象的「serving framework」变成你能摸到的进程。
- 之后顺着 U1（导览）→ U2（架构与请求生命周期）→ U3-U6（引擎四大支柱）的顺序往下读。
- 如果你已经熟悉推理基础，想直接看源码，可以从 4.2.3 的对照表里挑一个你最感兴趣的目录，先浏览它的文件名建立印象，再等对应单元精读。
