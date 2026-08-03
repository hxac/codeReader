# vLLM-Omni 是什么：全模态推理框架全景

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是让你在不动手写代码、也不深入任何子系统细节的前提下，建立起对 vLLM-Omni 这个项目的「全局认知」。读完本讲，你应该能够：

- 说清楚 vLLM-Omni 相对于上游 vLLM 扩展了什么、为什么需要扩展。
- 识别全模态模型的三种典型组合：DiT 为主、AR 为主、AR+DiT。
- 说出 OmniRouter、EntryPoints、AR、Diffusion、OmniConnector 五大核心组件各自的职责。
- 看懂 `vllm_omni/__init__.py` 中「🟡 修改 / 🔴 新增」的扩展哲学，以及包初始化时各步骤的先后顺序。

本讲不要求你跑通任何模型，只要求你「读懂地图」。从下一讲（u1-l2 安装）开始，我们才会真正动手。

## 2. 前置知识

本讲面向零基础读者，但有两个概念最好先有个模糊印象：

- **LLM（大语言模型）推理**：给模型一段文本输入，它逐字吐出文本输出。这类模型几乎都是「自回归（Autoregressive, AR）」结构——下一个 token 只依赖前面已经生成的 token。
- **vLLM**：一个高吞吐、低延迟的大模型推理与服务框架，它最大的招牌是 **PagedAttention / 高效 KV Cache 管理**。vLLM-Omni 就是在 vLLM 这套底座之上做扩展。

如果下面两个术语你不熟，先记一句话定义即可，后面会结合源码细讲：

| 术语 | 一句话解释 |
| --- | --- |
| AR（Autoregressive，自回归） | 一次生成一个 token，靠「历史 token」逐步往后推，适合文本/语音 token 流。 |
| DiT（Diffusion Transformer，扩散 Transformer） | 从噪声出发，反复去噪若干步得到结果，适合图像/视频/音频的「整块」生成。 |

> 💡 如果你只想看懂本讲，记住「AR 是逐字吐、DiT 是反复去噪」就足够了。

## 3. 本讲源码地图

本讲只读三个文件，它们分别回答「项目是什么、为什么要做、怎么组织」：

| 文件 | 作用 | 本讲用到哪一部分 |
| --- | --- | --- |
| [README.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/README.md) | 项目的门面：定位、最新动态、支持的模型、快速入口 | About 段、Latest News |
| [docs/design/architecture_overview.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/architecture_overview.md) | 官方架构总览文档，是后续所有讲义的「设计依据」 | Goals、Representative models、Key Components |
| [vllm_omni/__init__.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/__init__.py) | 整个 Python 包的入口，定义了扩展哲学与初始化顺序 | 顶部模块注释、import 顺序、`__all__` |

另外会顺带提到 [vllm_omni/version.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/version.py)（版本对齐告警），帮助你理解「vLLM 与 vLLM-Omni 必须版本对齐」这件事在代码层面是怎么落地的。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块，按「定位 → 目标与模型分类 → 五大组件 → 包结构哲学」的顺序推进。

### 4.1 vLLM-Omni 的定位：在 vLLM 之上做增量扩展

#### 4.1.1 概念说明

先看 README 里最关键的一句话定义：

> vLLM was originally designed to support large language models for text-based autoregressive generation tasks. vLLM-Omni is a framework that extends its support for omni-modality model inference and serving.

翻译过来就是：**vLLM 原本是为「文本类、自回归生成」的大模型设计的；vLLM-Omni 把它扩展成支持「全模态（omni-modality）模型」的推理与服务框架。**

这里有两个关键词：

- **omni-modality（全模态）**：不限于文本，还包括图像、音频、视频，甚至机器人动作（action）。
- **extends（扩展）**：vLLM-Omni 不是另起炉灶重写一个推理引擎，而是在 vLLM 之上「打补丁 + 加模块」。这个定位非常重要——它意味着你学过的 vLLM 知识在这里仍然有效。

#### 4.1.2 核心流程

用一句话概括 vLLM-Omni 的工作方式：

```
用户请求（文本/图/音/视频）
        │
        ▼
   vLLM-Omni 运行时（在 vLLM 核心之上扩展）
        │
        ▼
全模态输出（文本/图像/音频/视频/动作）
```

它的价值在于：**你不用为了跑一个「文生图」模型去学一套全新框架，也不用为了跑一个「能听能说」的对话模型再学一套——vLLM-Omni 想用同一套抽象把它们都装下。**

#### 4.1.3 源码精读

README 的 About 段直接列出了 vLLM-Omni 扩展的三个方向：

- [README.md:33-37](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/README.md#L33-L37)：说明 vLLM 的原始定位，以及 vLLM-Omni 的三条扩展——**Omni-modality（全模态）、Non-autoregressive Architectures（非自回归结构）、Heterogeneous outputs（异构输出）**。

紧接着 README 还强调了「快」和「易用」的来源，这三点其实就是 vLLM-Omni 的技术卖点，后续进阶讲义会逐一拆解：

- [README.md:45-49](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/README.md#L45-L49)：列出 vLLM-Omni 的三个性能来源——**复用 vLLM 的高效 KV Cache、流水线化的阶段执行重叠（pipelined stage execution overlapping）、基于 OmniConnector 的全解耦与跨阶段动态资源分配**。

包入口文件的顶部注释，则用一句话总结了它的身份：

- [vllm_omni/__init__.py:1-13](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/__init__.py#L1-L13)：模块文档字符串写明——「vLLM-Omni：带非自回归结构的多模态模型推理与服务，本包把 vLLM 从传统的文本自回归生成，扩展到支持多模态、非自回归结构和非文本输出」。

#### 4.1.4 代码实践

**实践目标**：通过 README 的 Latest News，建立「版本节奏」的直觉。

**操作步骤**：

1. 打开 [README.md 的 Latest News 段（第 18-27 行）](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/README.md#L18-L27)。
2. 找到最新一个版本（写作本讲时为 `0.24.0`，发布于 2026/07）。
3. 注意它的措辞：「aligned with the vLLM 0.24 release line」——即 vLLM-Omni `0.24.0` 与 vLLM `0.24.x` 对齐。

**需要观察的现象**：几乎每条 release 都会写明它「对齐了哪个 vLLM 版本」。

**预期结果**：你会得出一条结论——**vLLM-Omni 的版本号紧跟 vLLM 的主次版本（major.minor）**。这不是巧合，而是强约束（下一模块会看到对应代码）。

#### 4.1.5 小练习与答案

**练习 1**：vLLM-Omni 是「重写一个推理引擎」还是「在 vLLM 上做扩展」？请用一句话说明依据。

> **参考答案**：是「在 vLLM 上做扩展」。依据是 README 第 33 行的措辞「vLLM-Omni is a framework that **extends** its support」，以及包注释里「This package **extends** vLLM」的表述。

**练习 2**：README 提到 vLLM-Omni 「快」的三个来源是什么？

> **参考答案**：① 复用 vLLM 的高效 KV Cache 管理；② 流水线化的阶段执行重叠（pipelined stage execution overlapping）；③ 基于 OmniConnector 的全解耦与跨阶段动态资源分配。

---

### 4.2 三大扩展目标与代表模型分类

#### 4.2.1 概念说明

架构文档把 vLLM-Omni 的目标拆成 4 条。其中前 3 条直接对应 README 的「三大扩展」，第 4 条是设计层面的「可扩展性」：

1. **Non-textual Output（非文本输出）**：能高效地处理和输出图像、音频、视频、动作轨迹等，而不只是文本。
2. **Non-Autoregressive Structure（非自回归结构）**：支持自回归之外的模型结构，尤其是扩散 Transformer（DiT），它广泛用于视觉和音频生成。
3. **Integration with vLLM Core（与 vLLM 核心集成）**：保持兼容，并在合适的地方复用 vLLM 已有的关键模块和优化。
4. **Extensibility（可扩展性）**：架构要模块化、灵活，方便接入新的模态、模型结构和输出格式。

要理解第 2 条为什么重要，需要先弄清 AR 和 DiT 的本质区别：

- **AR（自回归）**：建模的是「给定历史，预测下一个 token」的条件概率。文本生成本质上是一个序列决策过程：

  \[ P(x_1, x_2, \dots, x_T) = \prod_{t=1}^{T} P(x_t \mid x_1, \dots, x_{t-1}) \]

  它「逐个」产出，天然适合流式。

- **DiT（扩散）**：不预测「下一个」，而是从一个随机噪声 \( z_T \) 出发，学习一系列去噪步骤，逐步得到干净样本 \( z_0 \)。每一步去噪可以写成：

  \[ z_{t-1} = \text{Step}\bigl(z_t,\ t,\ \text{模型预测的噪声或速度}\bigr),\quad t = T, T\!-\!1, \dots, 1 \]

  它是「整块」地、经过固定步数去噪后一次成型，无法像 AR 那样边算边吐单个 token。

正是因为 DiT 的执行模式（多步迭代、整块输出）和 vLLM 原生为 AR 设计的调度（逐 token、连续批处理）完全不同，才需要 vLLM-Omni 单独搞一套「非自回归」的支持。

#### 4.2.2 核心流程

官方把当前主流的开源全模态模型，归纳成 **AR 与 DiT 的三种组合**。理解这三种组合，能帮你预判一个新模型在 vLLM-Omni 里会怎么跑：

```
┌─────────────────────────────────────────────────────────┐
│ 组合 A：DiT 为主，AR 当文本编码器      典型：Qwen-Image │
│   文本 prompt ──AR(编码)──► 条件 ──DiT(去噪)──► 图像   │
├─────────────────────────────────────────────────────────┤
│ 组合 B：AR 为主，DiT 当多模态生成器    典型：BAGEL      │
│   输入 ──AR(理解+CoT)──► 指令 ──DiT(生成)──► 视觉      │
├─────────────────────────────────────────────────────────┤
│ 组合 C：AR + DiT 联合                 典型：Qwen-Omni   │
│   多模态输入 ──AR(理解)──► 隐状态 ──DiT/声码器──► 音/视 │
└─────────────────────────────────────────────────────────┘
```

关键直觉：**一个模型里同时出现 AR 和 DiT，就必然存在「阶段（stage）」之间的数据流转问题**——这正是后续 u3（多阶段运行时）要解决的核心。

#### 4.2.3 源码精读

架构文档专门用一节讲「目标」和「代表模型」，是我们理解项目定位的最权威依据：

- [architecture_overview.md:12-19](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/architecture_overview.md#L12-L19)：**Goals 段**，列出上面 4 条目标，其中前 3 条（Non-textual Output / Non-Autoregressive Structure / Integration with vLLM Core）与 README 的三大扩展一一对应。

- [architecture_overview.md:22-54](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/architecture_overview.md#L22-L54)：**Representative omni-modality models 段**，把主流模型分成三类，并各配一张架构图：
  - 第 26-27 行：**DiT 为主 + AR 文本编码器**（例：Qwen-Image），强调复杂文字渲染和精细图像编辑。
  - 第 36-37 行：**AR 为主 + DiT 多模态生成器**（例：BAGEL），统一理解与生成，输出 CoT 文本和视觉生成。
  - 第 46-47 行：**AR + DiT**（例：Qwen-Omni），端到端的全模态 LLM，多模态输入、文本/音频输出。

> 📌 记住这三个典型模型（Qwen-Image / BAGEL / Qwen-Omni），它们会在后续 u5（Diffusion）、u4（AR）、u3（多阶段）讲义里反复作为例子出现。

#### 4.2.4 代码实践

**实践目标**：用三个真实模型，验证「AR + DiT 组合」的分类。

**操作步骤**：

1. 打开 README 的 [支持模型清单（第 59-64 行）](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/README.md#L59-L64)。
2. 找到这三类模型各一个例子：`Qwen-Image`（Diffusion 段）、`BAGEL`（Omni-modality 段）、`Qwen3-Omni`（Omni-modality 段）。
3. 对照架构文档的三类分类，给每个模型标注它属于「DiT 为主 / AR 为主 / AR+DiT」中的哪一类。

**预期结果**：你能把 README 提到的模型归到架构文档的三个类别里，并说出它的「主结构」是什么。

**待本地验证**：如果你想确认某个具体模型的内部结构（比如它到底有没有 DiT 分支），需要去 HuggingFace 上看该模型的实现，本讲不展开。

#### 4.2.5 小练习与答案

**练习 1**：为什么 vLLM 原生不能直接跑 DiT 模型？请用 AR 和 DiT 的执行模式差异来解释。

> **参考答案**：vLLM 的调度是围绕「逐 token 自回归 + 连续批处理」设计的；而 DiT 是「从噪声出发、固定步数去噪、整块输出」，既没有「下一个 token」的概念，也不适合逐 token 的连续批处理。两者的调度模型根本不同，所以需要 vLLM-Omni 额外提供「非自回归结构」的支持。

**练习 2**：Qwen-Image、BAGEL、Qwen-Omni 分别属于哪类组合？它们的「主结构」分别是什么？

> **参考答案**：Qwen-Image = DiT 为主（AR 当文本编码器）；BAGEL = AR 为主（DiT 当多模态生成器）；Qwen-Omni = AR + DiT 联合（端到端全模态）。

---

### 4.3 五大核心组件：从架构图读懂请求流转

#### 4.3.1 概念说明

架构文档把 vLLM-Omni 的主架构拆成 5 个核心组件。这 5 个组件的名字，你在后续几乎每一篇讲义里都会再见到，所以现在先把它们的「一句话职责」记牢：

| 组件 | 一句话职责 |
| --- | --- |
| **OmniRouter** | 面向全模态请求的智能路由，负责把请求派发到正确的处理路径。 |
| **EntryPoints** | 定义离线/在线服务的 API 入口（APIServer、Omni / AsyncOmni），并由 `AsyncOmniEngine` + `Orchestrator` 协调多阶段的 AR/DiT 执行。 |
| **AR** | 适配全模态模型的自回归执行层，同时继承 vLLM 的高效特性（如 KV Cache 管理）。 |
| **Diffusion** | 原生实现并经过加速（缓存、并行、注意力、量化）的扩散执行层。 |
| **OmniConnector** | 支持阶段间的「全解耦（disaggregation）」，按 E/P/D/G（编码/处理/解码/生成）把阶段拆开，跨阶段传递数据。 |

「全解耦」是 vLLM-Omni 最有特色的设计，值得单独点一句：**它允许把一个模型的不同阶段（比如 AR 理解阶段、DiT 生成阶段）拆成独立运行的进程/副本，彼此通过 OmniConnector 通信，从而可以给不同阶段分配不同的 GPU 资源。**

#### 4.3.2 核心流程

以 **Qwen3-Omni**（AR+DiT 联合的典型）为例，架构文档明确指出它的三个阶段会被声明成独立的配置阶段，并由 Orchestrator 在运行时路由：

```
                  多模态请求（文本/视频/音频）
                          │
                          ▼
                   ┌──────────────┐
                   │  EntryPoints │  Omni / AsyncOmni / APIServer
                   └──────┬───────┘
                          │  （AsyncOmniEngine 协调）
                          ▼
                   ┌──────────────┐
                   │ Orchestrator │  跨阶段路由
                   └──┬───┬───┬───┘
            ┌─────────┘   │   └──────────┐
            ▼             ▼              ▼
       ┌─────────┐   ┌─────────┐   ┌───────────┐
       │ Thinker │──►│ Talker  │──►│ Code2wav  │
       │ (AR阶段)│   │ (AR阶段)│   │ (音频生成)│
       └─────────┘   └─────────┘   └───────────┘
            │             │              │
            └──阶段间数据经 OmniConnector 流转──►
```

这里的 **Thinker / Talker / Code2wav** 是 Qwen3-Omni 内部的三个阶段：Thinker 负责「理解」多模态输入，Talker 负责「思考并产出文本 token」，Code2wav 负责把 token 变成最终的音频波形。它们在 vLLM-Omni 里被当成三个独立 stage 来调度，阶段之间由 Orchestrator 负责前推、由 OmniConnector 负责传数据。

#### 4.3.3 源码精读

官方架构文档用一张表精确定义了 5 个组件，并紧接着用 Qwen3-Omni 给出了具体的阶段示例：

- [architecture_overview.md:65-75](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/architecture_overview.md#L65-L75)：**Key Components 表格**。逐行定义了 OmniRouter（智能路由）、EntryPoints（离线/在线 API + `AsyncOmniEngine`/`Orchestrator` 协调多阶段）、AR（继承 vLLM 的 cache 管理）、Diffusion（原生加速实现）、OmniConnector（基于 E/P/D/G 的全解耦）。

- [architecture_overview.md:75](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/architecture_overview.md#L75)：紧接的一句话非常关键——「解耦的阶段通过 stage configuration 管理；在 Qwen3-Omni 中，**Thinker/Talker/Code2wav 被声明为各自独立的已配置阶段，运行时路由由 `Orchestrator` 经由 `StageEngineCoreClient` / `StageDiffusionClient` 完成**」。这句话把「配置声明（stage config）」和「运行时路由（Orchestrator + Client）」两件事点透了。

#### 4.3.4 代码实践

**实践目标**：把 5 个抽象组件和它们的「源码落点」对上号，为后续阅读建好索引。

**操作步骤**：

1. 先记住上面表格里 5 个组件的职责。
2. 打开包目录浏览（本机执行 `ls vllm_omni/`，或直接看仓库 [vllm_omni/](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/__init__.py)），你会看到这些一级目录：`entrypoints`、`engine`、`diffusion`、`distributed`、`worker`、`core`、`config`、`platforms`、`quantization` 等。
3. 尝试把每个组件映射到目录（先自己猜，再看下表对答案）。

**预期结果**：你能大致建立下面的映射（细节在后续讲义展开，本讲只求「眼熟」）：

| 组件 | 大致对应的源码目录（仅供建立印象） |
| --- | --- |
| OmniRouter / EntryPoints | `vllm_omni/entrypoints/`（含 `omni.py`、`async_omni.py`、`openai/`、`cli/`） |
| Orchestrator（运行时协调） | `vllm_omni/engine/`（含 `orchestrator.py`、`async_omni_engine.py`） |
| AR | `vllm_omni/worker/`、`vllm_omni/core/sched/` |
| Diffusion | `vllm_omni/diffusion/`（含 `engine`、`sched`、`worker`、`models`、`attention`、`cache`） |
| OmniConnector | `vllm_omni/distributed/omni_connectors/` |

> ⚠️ 上表是「建立印象」用的粗映射，不是精确边界。例如 AR 的调度器在 `core/sched/` 下、Orchestrator 在 `engine/` 下，二者会协同。精确的代码落点会在 u3/u4/u5 讲义里逐一给出。

#### 4.3.5 小练习与答案

**练习 1**：5 大组件里，哪一个负责「阶段之间的全解耦与数据传递」？

> **参考答案**：**OmniConnector**。它支持基于 E/P/D/G 的全解耦，负责跨阶段的数据流转。

**练习 2**：在 Qwen3-Omni 中，Thinker/Talker/Code2wav 是怎么被组织起来的？谁负责在它们之间路由？

> **参考答案**：它们被声明为三个独立的「已配置阶段（configured stages）」，由 stage configuration 管理；运行时的跨阶段路由由 **Orchestrator** 经由 `StageEngineCoreClient` / `StageDiffusionClient` 完成。

**练习 3**：AR 组件和 Diffusion 组件在「是否复用 vLLM」上的策略有什么不同？

> **参考答案**：**AR** 是「适配全模态的同时，尽量继承 vLLM 已有的高效特性（如 KV Cache 管理）」；**Diffusion** 则是「原生实现并自研加速（缓存/并行/注意力/量化）」，因为它无法直接复用 vLLM 为 AR 设计的那套机制。

---

### 4.4 包结构与扩展哲学：🟡 修改 / 🔴 新增

#### 4.4.1 概念说明

vLLM-Omni 既然是「在 vLLM 之上扩展」，那么它到底是怎么改的？包入口文件的顶部注释给出了最精炼的答案——它把所有扩展分成两类：

- 🟡 **Modified（修改）**：对 vLLM 已有组件做改造，让它支持多模态。
- 🔴 **Added（新增）**：新增 vLLM 里根本没有的组件，用来处理多模态和非自回归逻辑。

这种「二分法」是理解整个项目代码组织的钥匙：你在仓库里看到的每一处改动，基本都能归到这两类之一。

#### 4.4.2 核心流程

理解这套哲学，最直接的方式是看「`import vllm_omni` 时到底发生了什么」。包入口 `__init__.py` 用一连串 `try/except import` 严格规定了初始化顺序，大致是：

```
1. 先加载 version（并触发 vLLM 版本对齐检查）
   └─ 若 vLLM 与 vLLM-Omni 主次版本不一致 → 发出 RuntimeWarning
2. 应用 patch（monkey-patch，改写 vLLM 的若干行为）   ← 🟡 Modified 的总入口
3. 注册自定义 configs（AutoConfig / AutoTokenizer）   ← 让 HF 模型能被识别
4. 暴露 OmniModelConfig                              ← 配置入口
5. 懒加载 Omni / AsyncOmni（用到才 import，避免拖慢启动）
```

为什么要这么讲究顺序？注释里写得很明白：**版本检查必须在打 patch 之前完成**，否则当 vLLM 与 vLLM-Omni 版本不一致时，patch 阶段导入 vLLM 可能直接抛错。这是一种典型的「先体检、再动手」的防御式初始化。

#### 4.4.3 源码精读

包入口的每一段都对应上面流程的一步，下面给出精确行号：

- [vllm_omni/__init__.py:9-13](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/__init__.py#L9-L13)：**扩展哲学的官方表述**——「🟡 Modified: 为支持多模态而修改的 vLLM 组件；🔴 Added: 为多模态和非自回归处理新增的组件」。

- [vllm_omni/__init__.py:15-19](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/__init__.py#L15-L19)：**第 1 步——尽早导入 version**。注释说明：之所以要先导入 version，是因为它会在「vLLM / vLLM-Omni 主次版本不一致」时发出告警；而这件事必须在打 patch 之前做，因为版本不一致时 patch 里的 vLLM 导入可能抛错。

- [vllm_omni/__init__.py:21-27](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/__init__.py#L21-L27)：**第 2 步——应用 patch**。注意它用 `try/except ModuleNotFoundError` 包起来：如果当前环境没装 vLLM（比如只是构建文档），就允许 `patch = None` 而不报错。

- [vllm_omni/__init__.py:29-37](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/__init__.py#L29-L37)：**第 3 步——尽早注册自定义 configs/parsers**，让 HuggingFace 的 `AutoConfig` / `AutoTokenizer` 能识别 vLLM-Omni 关心的模型。

- [vllm_omni/__init__.py:39](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/__init__.py#L39)：**第 4 步——暴露 `OmniModelConfig`**，这是后续 u2-l2（配置体系）的主角。

- [vllm_omni/__init__.py:42-56](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/__init__.py#L42-L56)：**第 5 步——`__getattr__` 懒加载**。`Omni` / `AsyncOmni` 不会在 `import vllm_omni` 时就加载，而是在你真正访问它们时才 import。注释解释了原因：避免在包导入时就拉入重量级依赖（vllm model_loader → fused_moe → pynvml），从而防止那些没有 CUDA 上下文的轻量子进程崩溃（参见 issue #1793）。

版本对齐的告警逻辑则在另一个文件里：

- [vllm_omni/version.py:27-58](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/version.py#L27-L58)：`warn_if_misaligned_vllm_version()` 取 vLLM 与 vLLM-Omni 各自版本的前两位（major.minor）做比较，不一致就发出 `RuntimeWarning`，提示「这很可能导致兼容性问题」。这个函数在该模块被导入时自动执行——这就是为什么 `__init__.py` 要「先 version、再 patch」。

> 📌 这一小节其实是 **u2-l1（patch 机制）** 和 **u2-l2（配置体系）** 的预告。如果你现在还看不懂 patch 具体改了什么，完全没关系——本讲只要知道「patch 是 🟡 Modified 的总入口」即可。

#### 4.4.4 代码实践

**实践目标**：在本地亲手验证「import 顺序」与「版本对齐告警」。

**操作步骤**（属于「源码阅读型实践」，不一定需要 GPU）：

1. 在已安装 vLLM-Omni 的环境里，执行：
   ```bash
   python -c "import vllm_omni; print(vllm_omni.__version__)"
   ```
2. 再执行下面这条，观察是否出现版本对齐告警：
   ```bash
   python -W all -c "import vllm_omni"
   ```
3. 阅读 [vllm_omni/version.py:27-58](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/version.py#L27-L58)，找到「比较 major.minor」的那两行代码。

**需要观察的现象**：

- 若 vLLM 与 vLLM-Omni 的主次版本一致，则无告警。
- 若不一致，会看到一条 `RuntimeWarning: vLLM and vLLM-Omni appear to have mismatched major/minor versions ...`。

**预期结果**：你能在 `version.py` 第 40-50 行附近，看到「取 `__version_tuple__[:2]` 做比较、不一致则 warn」的逻辑，从而印证「vLLM-Omni 强约束主次版本对齐」这件事在代码里是真实存在的。

**待本地验证**：是否真的触发告警，取决于你本地安装的 vLLM 版本；如果两者恰好对齐，你只会看到「无输出」，这也是正确现象。

#### 4.4.5 小练习与答案

**练习 1**：vLLM-Omni 把对 vLLM 的扩展分成哪两类？各举一个（不需要具体函数名）。

> **参考答案**：分两类——🟡 **Modified（修改）**：改造 vLLM 已有组件以支持多模态（如对某些配置/行为的 patch）；🔴 **Added（新增）**：新增 vLLM 原本没有的组件（如扩散执行层、跨阶段连接器）。

**练习 2**：为什么 `__init__.py` 要「先 import version、再 apply patch」？

> **参考答案**：因为 version 的导入会触发「vLLM 与 vLLM-Omni 主次版本是否一致」的检查；而一旦版本不一致，patch 阶段导入 vLLM 就可能抛错。所以必须先体检（版本检查）、再动手（打 patch），让用户能在出问题前就看到告警。

**练习 3**：`Omni` 和 `AsyncOmni` 为什么不在 `import vllm_omni` 时就加载？

> **参考答案**：为了避免在包导入阶段就拉入重量级依赖（vllm model_loader → fused_moe → pynvml），从而防止那些没有 CUDA 上下文的轻量子进程（如模型结构检查）崩溃。因此用 `__getattr__` 实现「用到时才 import」的懒加载。

---

## 5. 综合实践

本讲的综合实践，是把前面 4 个模块串成一张图。**这是一道「源码阅读 + 画图」型任务**，不需要运行任何模型。

**任务**：阅读 [docs/design/architecture_overview.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/architecture_overview.md) 后，用自己的话画出一张 **Qwen3-Omni（AR+DiT 联合）在 vLLM-Omni 中的请求流转草图**。

**要求**：

1. 草图里必须出现这些节点：`EntryPoints`、`AsyncOmniEngine`、`Orchestrator`、`Thinker`、`Talker`、`Code2wav`、`OmniConnector`。
2. 用箭头标出「一个多模态请求（例如「看一段视频并用语音回答」）从进入到产出音频」的流转方向。
3. 在 `Thinker → Talker → Code2wav` 三个阶段之间，标注「阶段间数据由 OmniConnector 流转」。
4. 在图旁用一句话注明：这些阶段是「由 stage configuration 声明、由 Orchestrator 在运行时路由」的（依据 [architecture_overview.md:75](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/architecture_overview.md#L75)）。

**参考画法**（你可以用自己的工具重画，文字版示意如下）：

```
多模态请求(视频+音频+文本)
        │
        ▼
  EntryPoints (Omni / AsyncOmni / APIServer)
        │
        ▼
  AsyncOmniEngine  ──协调──►  Orchestrator (跨阶段路由)
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
    Thinker (AR)  ──OmniConnector──►  Talker (AR)  ──OmniConnector──►  Code2wav (音频生成)
   理解多模态输入                    产出文本/隐状态                合成音频波形
        │                          │                          │
        └──────────────最终输出：文本 + 音频──────────────────┘
```

**自我检查清单**：

- [ ] 我能在图里指出「谁是 AR 阶段、谁是生成阶段」。
- [ ] 我能说出 Orchestrator 的作用是「运行时跨阶段路由」。
- [ ] 我能说出 OmniConnector 的作用是「阶段间数据流转 / 全解耦」。
- [ ] 我知道这些阶段是「配置声明 + 运行时路由」两件事配合的结果。

> 完成后，把这张图保存好——后续 u3（多阶段运行时与编排）会把它逐步细化为真实的类、队列和进程。

## 6. 本讲小结

- **vLLM-Omni 是在 vLLM 之上做「增量扩展」的全模态推理与服务框架**，不是重写；它复用 vLLM 的高效 KV Cache，并新增了流水线阶段重叠和基于 OmniConnector 的全解耦。
- 它的**三大扩展目标**是：非文本输出、非自回归结构（尤其是 DiT）、与 vLLM 核心保持兼容（外加「可扩展性」这条设计目标）。
- 主流全模态模型可归为 **AR 与 DiT 的三种组合**：DiT 为主（Qwen-Image）、AR 为主（BAGEL）、AR+DiT（Qwen-Omni）。
- 五大核心组件各司其职：**OmniRouter 路由、EntryPoints 提供入口并协调、AR 继承 vLLM、Diffusion 原生加速、OmniConnector 做全解耦**。
- 在 Qwen3-Omni 中，**Thinker/Talker/Code2wav 被声明为独立 stage，由 Orchestrator 经由 Stage 客户端在运行时路由**。
- 包入口用 **「🟡 修改 / 🔴 新增」** 二分法组织所有扩展，并严格规定 `version → patch → 注册 configs → 暴露 config → 懒加载 Omni/AsyncOmni` 的初始化顺序。

## 7. 下一步学习建议

本讲只建立了「全局认知」，还没有动手。建议按下面的顺序继续：

1. **u1-l2 安装与环境**：先把 vLLM-Omni 在本地装起来，亲手触发一次版本对齐告警，把本讲 4.4 节的理论变成体验。
2. **u1-l3 源码地图**：系统梳理 `vllm_omni/` 下的一级目录，把本讲 4.3.4 节那张「粗映射表」升级成精确的目录职责表。
3. **u1-l4 / u1-l5**：分别跑通一次离线推理（`Omni.generate`）和在线服务（`vllm serve --omni`），让本讲的「请求流转草图」有真实的输入输出可对照。
4. 想深入「为什么这么设计」，可以直接精读 [docs/design/architecture_overview.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/architecture_overview.md) 的 Main features 与 CFG Companion Flow 两段——它们是后续 u3/u7 的设计源头。
