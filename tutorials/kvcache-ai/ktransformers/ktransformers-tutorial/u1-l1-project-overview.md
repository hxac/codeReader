# KTransformers 项目定位与整体架构

## 1. 本讲目标

本讲是整个 KTransformers 学习手册的第一篇，目标不涉及任何代码细节，而是帮你建立一张「全局地图」。

读完本讲后，你应该能够：

1. 用一句话说清 KTransformers 是什么、为谁解决什么问题。
2. 理解 **CPU-GPU 异构推理**的核心思想：把 MoE 模型里经常被激活的「热专家」放在 GPU，很少被激活的「冷专家」放在 CPU。
3. 区分项目对外暴露的两条主线能力：**推理（kt-kernel）** 与 **微调（SFT，基于 LLaMA-Factory 集成）**，并知道它们各自的入口文件在哪里。
4. 看懂项目给出的性能示例数字大致代表什么。

本讲对应的最小模块为：**项目概述**、**能力矩阵**、**性能示例**。

---

## 2. 前置知识

在进入源码之前，先用大白话把几个会反复出现的概念讲清楚。如果你已经熟悉，可以跳到第 3 节。

### 2.1 什么是大语言模型（LLM）的推理

「推理（Inference）」就是模型训练好之后，接收用户输入（prompt），逐个生成输出 token 的过程。你调用 ChatGPT 时它「打字」的过程就是推理。与之相对的是「训练（Training）」，本讲的另一条主线 SFT（Supervised Fine-Tuning，监督微调）属于训练范畴。

### 2.2 什么是 MoE（专家混合）

传统 Transformer 的每一层有一个 FFN（前馈网络）。**MoE（Mixture-of-Experts，专家混合）** 把这一层换成若干个「专家」，每个专家都是一个独立的 FFN。对一个 token，模型里会有一个「路由器（router）」算出该 token 最适合哪几个专家，只激活其中 **top-k** 个（比如 8 选 2）。

直觉化的选择过程可以写成：

\[
\text{top-k experts} = \operatorname{TopK}\big(\operatorname{softmax}(x W_g)\big)
\]

其中 \(x\) 是当前 token 的隐状态，\(W_g\) 是路由权重。

这样做的好处是：**参数量可以做得非常大，但每个 token 实际算的量很小**。比如 DeepSeek-V3/R1 有数千亿参数，但每个 token 只激活一小部分，所以推理速度仍可接受。坏处是：**全部参数都要装进显存/内存**，对硬件容量要求极高。

### 2.3 为什么需要 CPU-GPU 异构

MoE 模型动辄几百 GB 甚至上千 GB，单张 GPU（消费级 24GB）根本装不下全部专家权重。但服务器的 **CPU 内存（DRAM）通常很大**（动辄几百 GB 到上 TB）。

关键观察：**并不是每个专家被激活的频率都一样**。有些专家几乎每句话都用（热专家），有些专家很少被用到（冷专家）。于是 KTransformers 的策略是：

- **热专家**：放到 GPU（显存小但快）。
- **冷专家**：放到 CPU（内存大但相对慢），并用专门优化的 CPU 内核来算。

权重总占用可以表示为：

\[
M_{\text{total}} = M_{\text{GPU}}(\text{热专家}) + M_{\text{CPU}}(\text{冷专家})
\]

这样就能在「有限的 GPU 显存 + 大容量 CPU 内存」的机器上，跑起原本装不下的超大 MoE 模型。

### 2.4 什么是量化、AMX、AVX、SGLang、LLaMA-Factory

这些词后面会反复出现，先有个印象即可，后续讲义会深入：

| 术语 | 一句话解释 |
|------|-----------|
| **量化（Quantization）** | 把高精度权重（如 BF16）压成低精度（INT4/INT8），省内存、加速，代价是少量精度损失。 |
| **AMX** | Intel 的高级矩阵扩展指令集（Sapphire Rapids 2023+ CPU 才有），做低精度矩阵乘法很快。 |
| **AVX / AVX2 / AVX512** | Intel/AMD 的向量指令集，比 AMX 老、兼容性更广。 |
| **SGLang** | 一个高性能的 LLM 推理服务框架，KTransformers 推理主线通过它的 fork 版本（`sglang-kt`）来对外提供服务。 |
| **LLaMA-Factory** | 一个流行的开源微调框架，KTransformers 的 SFT 主线与它集成。 |

> 术语不熟没关系，本讲只需要记住「热专家在 GPU、冷专家在 CPU」这一核心思想。

---

## 3. 本讲源码地图

本讲是总览，源码引用以文档为主，因为项目定位信息主要写在 README 里。

| 文件 | 作用 |
|------|------|
| `README.md` | 项目英文主 README，给出 Overview、两条能力（Inference / SFT）、性能示例、团队信息、归档说明。 |
| `README_ZH.md` | 项目中文主 README，内容与英文版对应，方便中文读者对照。 |
| `kt-kernel/README.md` | 推理主线（kt-kernel）的完整文档：安装、CLI、SGLang 集成、参数表、后端说明。 |
| `ktransformers.py` | 顶层轻量 shim（垫片）模块，把 `ktransformers` 这个名字转发到 `kt-kernel`，并提供 `has_sft_support()`。 |

> 小提示：仓库当前 HEAD 为 `cb9f47d142a507cac5d74450b30463d2e8d1cf58`，本讲所有永久链接都基于这个 commit。

---

## 4. 核心概念与源码讲解

### 4.1 项目概述

#### 4.1.1 概念说明

KTransformers 的官方自我定位写在 README 的 Overview 里。英文版标题下的一句话副标题是：

> A Flexible Framework for Experiencing Cutting-edge LLM Inference/Fine-tune Optimizations

（一个用于体验尖端 LLM 推理/微调优化的灵活框架。）

更具体的定义在 Overview 段落：它是一个**专注于通过 CPU-GPU 异构计算实现大语言模型高效推理和微调的研究项目**，目前对外暴露两条能力，且这两条能力都来自 `kt-kernel` 源码目录。

要理解这个定位，需要抓住三件事：

1. **研究对象是「超大 MoE 模型」**：DeepSeek-V3/R1、Kimi-K2、Qwen3-30B-A3B 等参数量极大、单机 GPU 装不下的模型。
2. **核心手段是「CPU-GPU 异构」**：不是单纯用 GPU，也不是单纯用 CPU，而是按专家热度分工。
3. **产品形态是「框架 + 工具」**：既能在推理侧（SGLang 集成）用，也能在微调侧（LLaMA-Factory 集成）用。

#### 4.1.2 核心流程

从「一个想做 MoE 推理的人」视角，KTransformers 的总体流程可以概括为：

```text
超大 MoE 模型权重
        │
        ├─ GPU 权重（喂给 SGLang，用于 GPU 侧热专家）
        └─ CPU 权重（量化或 GGUF，喂给 kt-kernel，用于 CPU 侧冷专家）
        │
        ▼
  kt-kernel 在 CPU 上跑优化好的 MoE 算子（AMX/AVX）
        │
        ▼
  通过 sglang-kt 把 GPU 与 CPU 结果拼成完整推理
        │
        ▼
  对外提供推理服务（OpenAI 兼容 API 等）
```

微调（SFT）侧则走另一条路：把 MoE 层用 KTransformers 的训练算子包装起来，结合 LLaMA-Factory 跑 LoRA/全量微调。

#### 4.1.3 源码精读

README 开头的副标题点明了项目定位：

[README.md:L10-L10](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L10-L10) — 这是项目对外的"一句话定位"：体验尖端 LLM 推理/微调优化的灵活框架。

更正式的 Overview 定义在下面这段，明确说出"研究项目""CPU-GPU 异构计算""两条能力来自 kt-kernel"：

[README.md:L14-L16](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L14-L16) — KTransformers 的核心定位，并指出 Inference 和 SFT 两个入口都来自 kt-kernel 源码树。

中文版与之完全对应：

[README_ZH.md:L14-L16](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README_ZH.md#L14-L16) — 中文版概览，同样的定位表述。

关于"谁在维护"：README 的 Contributors & Team 段落列出了维护方，这对判断项目的长期可信度很有帮助：

[README.md:L142-L150](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L142-L150) — 维护方为清华大学 MADSys 实验室、Approaching.AI、9#AISoft 及社区贡献者。

最后，理解仓库当前布局很重要：旧的"一体化"KTransformers 框架已被归档到 `archive/`，新代码集中在 `kt-kernel/`：

[README.md:L157-L163](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L157-L163) — 说明原始框架已归档到 `archive/`，项目现在围绕 kt-kernel 组织两条能力。

> 这也解释了为什么本学习手册（以及后续讲义）会以 `kt-kernel/` 为核心，而不是读根目录的旧代码。

#### 4.1.4 代码实践

**实践目标**：通过阅读 README，确认你对项目定位的理解，而不是凭记忆复述。

**操作步骤**：

1. 打开仓库根目录的 `README.md`。
2. 找到 Overview 段落（约第 14 行）。
3. 用你自己的话，在笔记里写出 KTransformers 的"一句话定义"，不超过 30 个字。
4. 把你写的定义和 README 副标题、Overview 原文对照，看是否抓住了"MoE / 异构 / 推理与微调"这三个关键词。

**需要观察的现象**：你会发现只要漏掉"CPU-GPU 异构"或"MoE"中任何一个词，定义就不再准确——这正说明这两个词是项目定位的支柱。

**预期结果**：得到一句类似"KTransformers 是用 CPU-GPU 异构计算来高效推理和微调超大 MoE 模型的框架"的描述。

> 说明：本实践为源码/文档阅读型实践，不需要运行命令，因此无"待本地验证"项。

#### 4.1.5 小练习与答案

**练习 1**：KTransformers 解决的核心硬件矛盾是什么？
**参考答案**：超大 MoE 模型的参数量远超单张 GPU 显存，但服务器 CPU 内存容量很大。矛盾在于"算力在 GPU、容量在 CPU"。KTransformers 用"热专家放 GPU、冷专家放 CPU"来化解。

**练习 2**：为什么仓库里有一个 `archive/` 目录，和一个 `kt-kernel/` 目录？
**参考答案**：`archive/` 是早期一体化的 KTransformers 框架，已被归档仅供参考；当前真正运行时和对外能力都集中在 `kt-kernel/` 源码树。这是项目结构重组的结果。

---

### 4.2 能力矩阵

#### 4.2.1 概念说明

KTransformers 对外暴露两条相对独立的能力。理解它们的区别，是后续选学路线的基础：

| 能力 | 中文名 | 目标 | 入口文件 | 典型用户 |
|------|--------|------|----------|----------|
| **Inference** | 推理 | 把已训练好的超大 MoE 模型部署成可用的推理服务 | `kt-kernel/README.md` | 想在有限硬件上跑 DeepSeek/Kimi/Qwen 等大模型的人 |
| **SFT** | 监督微调 | 在有限 GPU 显存下，对超大 MoE 模型做微调（含 LoRA） | `doc/en/SFT/KTransformers-Fine-Tuning_Quick-Start.md` | 想用自有数据微调大模型的人，配合 LLaMA-Factory |

两条能力都建立在 `kt-kernel` 的底层算子之上：推理直接用 CPU 优化算子 + GPU 热专家；微调则把 MoE 层包装成可训练的层，再接 PyTorch autograd。所以它们共享底层，但面向不同任务。

#### 4.2.2 核心流程

两条能力的"对外入口 → 内部路径"对比：

```text
Inference（推理）:
  kt-kernel/README.md
    → pip install kt-kernel（或源码 ./install.sh）
    → kt CLI（kt run ...）或直接 SGLang 启动
    → sglang-kt 调用 kt-kernel 的 CPU MoE 算子 + GPU 热专家
    → 推理服务

SFT（微调）:
  doc/en/SFT/KTransformers-Fine-Tuning_Quick-Start.md
    → 进入 LLaMA-Factory
    → 安装 requirements/ktransformers.txt
    → 用 accelerate launch 跑 src/train.py
    → KTransformers 把 MoE 层包装成可训练层（INT8/INT4 量化训练）
    → 得到微调后的权重 / LoRA
```

注意一个细节：`pip install ktransformers` 实际安装的是 `kt-kernel`；是否支持 SFT 取决于是否额外装了 SFT 依赖。顶层 shim `ktransformers.py` 里的 `has_sft_support()` 就是通过尝试 `import kt_kernel.sft` 来检测的。

#### 4.2.3 源码精读

README 的 Capabilities 段落把两条能力并列摆出：

[README.md:L58-L60](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L58-L60) — Inference 能力小节标题，链接指向 `kt-kernel/README.md`。

Inference 的"使用场景"段落直接点明了异构推理的核心（热专家 / 冷专家）：

[README.md:L79-L83](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L79-L83) — Use Cases 明确写出"Heterogeneous expert placement (hot experts on GPU, cold experts on CPU)"。

SFT 能力小节标题：

[README.md:L94-L94](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L94-L94) — SFT 能力小节，链接指向 `doc/en/SFT/KTransformers-Fine-Tuning_Quick-Start.md`。

SFT 的关键特性清单（量化训练、超大 MoE、比 ZeRO-Offload 快、内存更低、LLaMA-Factory 集成）：

[README.md:L100-L105](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L100-L105) — SFT 主要特性，强调与 LLaMA-Factory 集成及量化微调。

在 kt-kernel 的 README 里，推理主线的定位也很清晰——"高性能内核操作 + CPU 优化的 MoE 推理"：

[kt-kernel/README.md:L1-L3](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/README.md#L1-L3) — kt-kernel 自我介绍，强调 AMX/AVX/KML/blis 等 CPU 优化。

SGLang 集成段落里有一句对"异构推理"最直白的描述：

[kt-kernel/README.md:L257-L259](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/README.md#L257-L259) — 明确说"hot experts run on GPU and cold experts run on CPU"。

最后，顶层 shim 模块 `ktransformers.py` 说明了 SFT 支持是可选的、按需安装的：

[ktransformers.py:L1-L6](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/ktransformers.py#L1-L6) — docstring 说明运行时内核在 kt-kernel，SFT 通过 `pip install "ktransformers[sft]"` 激活。

[ktransformers.py:L27-L32](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/ktransformers.py#L27-L32) — `has_sft_support()` 通过尝试导入 `kt_kernel.sft` 来判断环境是否具备 SFT 能力。

#### 4.2.4 代码实践

**实践目标**：把两条能力"入口文件 → 安装命令 → 触发方式"串起来，形成一张能力对照表。

**操作步骤**：

1. 打开 `README.md` 的 Capabilities 段落，分别找到 Inference 和 SFT 的 Quick Start 代码块。
2. 打开 `ktransformers.py`，阅读 `has_sft_support()` 的实现，理解它如何"检测"而不是"安装"SFT。
3. 在笔记里填写下面这张表：

| 能力 | 入口文件 | 安装命令（来自 README） | 是否默认开启 |
|------|----------|--------------------------|--------------|
| Inference | ？ | ？ | ？ |
| SFT | ？ | ？ | ？ |

**需要观察的现象**：两条能力的安装路径不同——推理主线主要装 `kt-kernel`（含 CPU 内核），SFT 主线则需要进入 LLaMA-Factory 并安装 `requirements/ktransformers.txt`。

**预期结果**：

| 能力 | 入口文件 | 安装命令 | 是否默认开启 |
|------|----------|----------|--------------|
| Inference | `kt-kernel/README.md` | `cd kt-kernel && pip install .`（或 `pip install kt-kernel`） | 是（装 ktransformers 即装 kt-kernel） |
| SFT | `doc/en/SFT/KTransformers-Fine-Tuning_Quick-Start.md` | 进 LLaMA-Factory，`pip install -r requirements/ktransformers.txt` | 否（需额外 `[sft]` 依赖，`has_sft_support()` 才为 True） |

> 说明：以上命令均直接抄自 README，本实践为阅读核对型，未实际执行，故无"待本地验证"项。

#### 4.2.5 小练习与答案

**练习 1**：为什么推理主线必须用 `sglang-kt`（kvcache-ai 的 fork），而不是官方 `sglang`？
**参考答案**：因为 CPU-GPU 异构推理需要 sglang 支持 `--kt-method`、`--kt-weight-path` 等 kt-kernel 专属参数，并把部分专家计算卸载到 CPU。官方 sglang 没有这些能力，必须用 fork 版本。

**练习 2**：`has_sft_support()` 返回 `True` 需要满足什么条件？
**参考答案**：需要成功 `import kt_kernel.sft`，也就是环境里已经装好了 SFT 相关依赖（如通过 `pip install "ktransformers[sft]"` 或 `requirements/ktransformers.txt` 安装）。否则会进入 `except` 分支返回 `False`。

**练习 3**：一个只想做推理的用户，需要安装 LLaMA-Factory 吗？
**参考答案**：不需要。推理只需要 kt-kernel（及可选的 sglang-kt）。LLaMA-Factory 是 SFT 主线才用到的。

---

### 4.3 性能示例

#### 4.3.1 概念说明

性能示例的作用不是让你复刻数字，而是帮你判断"KTransformers 大致处在什么水平、解决什么量级的问题"。README 里给出了两条能力各自的代表性数字：

- **推理侧**：DeepSeek-R1-0528（FP8）在 8×L20 GPU + Xeon Gold 6454S 上，总吞吐 227.85 tokens/s，8 路并发下输出吞吐 87.58 tokens/s。
- **微调侧**：DeepSeek-V3/R1 在 4×RTX 4090（约 80GB 总显存）上以 3.7 it/s 训练；Qwen3-30B-A3B 在单张 RTX 4090（约 24GB 总显存）上 8+ it/s。

读这些数字时要关注两点：**硬件门槛**和**任务规模**。例如"在 4 张消费级 4090 上微调 DeepSeek-V3"——这本身就是 KTransformers 价值的体现，因为传统做法需要远超这个规模的显存。

#### 4.3.2 核心流程

如何"读懂"一个性能数字：

```text
1. 看模型规模（DeepSeek-V3/R1 是数百 B 级 MoE）
2. 看硬件配置（GPU 数量 + CPU 型号）
3. 看指标含义（tokens/s = 吞吐；it/s = 训练每秒迭代次数）
4. 看是否有并发/特殊条件（如 8-way concurrency）
5. 对照"如果没有 KTransformers，这个任务需要什么硬件"
```

以推理为例，吞吐的两个口径要分清：

- **Total Throughput（总吞吐）**：所有并发请求合并起来的生成速度。
- **Output Throughput（输出吞吐）**：通常指稳定输出阶段的有效速度。

#### 4.3.3 源码精读

推理能力的性能示例表只有一行，但极具代表性：

[README.md:L85-L89](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L85-L89) — 推理性能示例：DeepSeek-R1-0528 (FP8) 在 8×L20 + Xeon Gold 6454S 上 227.85/87.58 tokens/s。

微调能力的性能示例表，三行覆盖了 V3/R1 与 Qwen3：

[README.md:L107-L111](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L107-L111) — SFT 性能示例：DeepSeek-V3/R1 在 4×4090 上 3.7 it/s，Qwen3-30B-A3B 在单卡 4090 上 8+ it/s。

中文版给出同样的 SFT 数字，方便对照：

[README_ZH.md:L96-L100](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README_ZH.md#L96-L100) — 中文版 SFT 性能示例表。

SFT 关键特性里还有两个"相对优势"数字（比 ZeRO-Offload 快 6-12 倍、CPU 内存约降到 1/2）：

[README.md:L100-L105](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L100-L105) — 这里同时包含性能优势描述与特性清单。

> 提示：这些数字依赖具体硬件、模型版本和参数配置，属于"参考量级"，不要当作通用承诺。

#### 4.3.4 代码实践

**实践目标**：学会从性能示例里提炼出"硬件门槛 + 任务规模"，而不是只记一个速度数字。

**操作步骤**：

1. 读 `README.md` 推理性能表（第 85-89 行）和 SFT 性能表（第 107-111 行）。
2. 为每一行填写下面四栏：**模型 / 硬件 / 指标 / 含义解读**。
3. 思考一个问题：如果没有 KTransformers，把 DeepSeek-V3 塞进 GPU 做推理/微调，大概需要多少显存？这能帮你体会性能表背后的价值。

**需要观察的现象**：你会发现每一行都同时绑定"一个大模型 + 一套相对亲民的硬件"。这正是 KTransformers 异构方案要突出的对比。

**预期结果**（示例解读，数字均来自上述 README 行）：

| 模型 | 硬件 | 指标 | 含义解读 |
|------|------|------|----------|
| DeepSeek-R1-0528 (FP8) | 8×L20 + Xeon Gold 6454S | 总 227.85 / 输出 87.58 tokens/s（8 路并发） | 数百 B 级 MoE 可在服务器级 CPU+多卡 GPU 上跑出可用吞吐 |
| DeepSeek-V3 | 4×RTX 4090（~80GB） | 3.7 it/s | 用消费级显卡微调数百 B MoE |
| DeepSeek-R1 | 4×RTX 4090（~80GB） | 3.7 it/s | 同上 |
| Qwen3-30B-A3B | 1×RTX 4090（~24GB） | 8+ it/s | 单卡即可微调中等 MoE |

> 说明：以上为基于 README 的解读练习，未在本机复现，属于阅读型实践，故无"待本地验证"项。

#### 4.3.5 小练习与答案

**练习 1**：推理性能表里"Total Throughput"和"Output Throughput"为什么不相等？
**参考答案**：它们是两个口径。Total Throughput 通常统计所有并发请求的合计生成速度；Output Throughput 更接近稳定输出阶段的有效速度。在多路并发（如 8-way）下，两者数值会不同。

**练习 2**：SFT 性能表里写"约 80GB 总计"显存，指的是单卡显存吗？
**参考答案**：不是。它指的是整套配置（4×RTX 4090）的总显存上限量级，而非单卡。4×24GB ≈ 96GB，扣除开销后有效量级在 80GB 左右。这体现了"多卡分担 + 异构"的思路。

---

## 5. 综合实践

设计一个小任务，把本讲三个模块（项目概述、能力矩阵、性能示例）串起来。

**任务**：为 KTransformers 写一张「一页纸项目简介」。

要求包含以下五部分，全部基于本讲引用的真实 README 内容：

1. **一句话定位**：用你自己的话写，必须包含"MoE"和"CPU-GPU 异构"两个关键词。
2. **核心思想图**：画一个简单的示意图，标出"热专家 → GPU""冷专家 → CPU"，并写出权重总占用公式 \(M_{\text{total}} = M_{\text{GPU}} + M_{\text{CPU}}\)。
3. **两条能力对照表**：列出 Inference 与 SFT 的入口文件、安装命令、是否默认开启。
4. **一个性能亮点**：从性能示例里挑一个你最感兴趣的数字，写出它的模型、硬件、指标，并解释为什么这个数字"值得一提"。
5. **一个你想继续追问的问题**：例如"热专家和冷专家到底是怎么判定和调度的？"——把这个问题记下来，它会成为你读后续讲义的动力。

**操作建议**：

- 定位与能力部分，对照 [README.md:L14-L16](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L14-L16) 和 [README.md:L58-L60](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L58-L60)。
- 性能数字对照 [README.md:L85-L89](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L85-L89) 与 [README.md:L107-L111](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/README.md#L107-L111)。
- 完成后把这张简介保存下来，作为本手册的"开篇笔记"。

**预期结果**：一张能发给完全没接触过 KTransformers 的同事、让他 3 分钟看懂项目是干嘛的简介。如果你发现自己写不出某一部分，说明需要回头重读对应模块。

---

## 6. 本讲小结

- KTransformers 是一个**用 CPU-GPU 异构计算来高效推理和微调超大 MoE 模型**的研究框架，由清华 MADSys 实验室、Approaching.AI、9#AISoft 等维护。
- 核心思想是**按专家热度分工**：热专家放 GPU、冷专家放 CPU，从而在有限显存 + 大容量内存的机器上跑起原本装不下的模型。
- 对外有两条独立能力：**推理（kt-kernel，常配合 sglang-kt）** 与 **微调（SFT，配合 LLaMA-Factory）**，共享底层 kt-kernel 算子。
- 仓库已重组：旧一体化框架归档到 `archive/`，新运行时集中在 `kt-kernel/`；顶层 `ktransformers.py` 是转发到 kt-kernel 的轻量 shim。
- SFT 是可选能力，是否可用由 `ktransformers.has_sft_support()` 通过尝试导入 `kt_kernel.sft` 来检测。
- 性能示例（如 DeepSeek-R1 在 8×L20 上推理、DeepSeek-V3 在 4×4090 上微调）展示了"在亲民硬件上跑超大 MoE"这一核心价值。

---

## 7. 下一步学习建议

本讲建立了全局认知，下一讲建议继续入门层，把"地图"画得更细：

1. **`u1-l2` 仓库目录结构与代码组织**：深入 `kt-kernel/` 内部，搞清 `python/`、`operators/`、`cpu_backend/`、`cuda/`、`scripts/` 各自的职责，以及 `archive/` 与 `kt-kernel/` 的边界。
2. **`u1-l3` 顶层 shim 包与安装入口**：精读 `ktransformers.py`、`setup.py`、`pyproject.toml`、`version.py`，搞清 `pip install ktransformers` 实际装了什么、`[sft]`/`[sglang]` 两个 extras 的作用。

如果你想直接进入动手环节，也可以跳到单元 2（`u2-l1` 安装方式总览），按 README 用 `pip install kt-kernel` 装好，并运行 `kt version` 验证；但建议先读完单元 1 的三篇，避免对仓库结构产生误解。
