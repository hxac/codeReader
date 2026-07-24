# 项目定位与多阶段运行时概念

> 本讲是 SGLang-Omni 学习手册的第一篇。你不需要预先看过任何一行源码，读完本讲你会知道这个项目「是什么、为什么长这样、和 SGLang 是什么关系」。

## 1. 本讲目标

学完本讲后，你应该能够：

1. 用一句话向同事说清楚 **SGLang-Omni 是什么**（面向 omni/语音/TTS 模型的多阶段推理服务运行时）。
2. 理解 **多阶段解码（multi-stage decoding）** 的设计动机：为什么要把一次生成拆成预处理、编码器、自回归引擎、talker、解码器、vocoder、聚合器等异构阶段。
3. 识别框架自带的 **五大职责**：管线拓扑、阶段生命周期、跨阶段传输、模型族集成层、OpenAI 兼容 API 面。
4. 画出请求主链路 `HTTP API → Client → Coordinator → Stage → Scheduler → ModelRunner → model forward`，并区分 SGLang-Omni 与上游 SGLang 的 **分工边界**。
5. 从 README 中举出 omni、TTS、ASR 三类被服务模型各一个真实例子。

本讲**只讲概念与定位**，不要求你跑通任何代码。真正动手装环境、起服务是后续讲义（u1-l2、u1-l4）的事。

## 2. 前置知识

如果你对下面这些词不熟，没关系，本节用大白话过一遍。

- **推理（inference）/ 服务（serving）**：训练好的模型，接收用户输入、返回输出的过程叫「推理」；把它包装成一个长期运行、能被很多人同时调用的网络服务，叫「服务」。
- **大语言模型（LLM）/ 多模态（multimodal）**：LLM 只处理文本；多模态模型同时处理文本、图像、音频、视频等「模态（modality）」。
- **omni 模型**：能同时「听懂」多种模态输入、并「说出」多种模态输出的模型，例如既看图又听语音、然后回复文字和语音。
- **自回归（autoregressive, AR）**：「一个词一个词往后生成」的方式。第 N 个 token 依赖前面 N-1 个 token。这是 ChatGPT 这类模型生成文本的核心机制。
- **KV cache**：自回归生成时缓存的历史计算结果，避免每生成一个新词就重算一遍历史。它是 AR 引擎性能的关键。
- **张量（tensor）/ GPU**：模型内部都是多维数组（张量），GPU 是专门高效做张量运算的硬件。
- **TTS / ASR**：TTS（Text-To-Speech）把文字变语音；ASR（Automatic Speech Recognition）把语音转文字。
- **vocoder**：把「语音的数字特征（声学码）」还原成「真正能播放的声音波形」的模块。可以理解为 TTS 的「发声器官」。

只要你知道「模型要变成一个能被网络调用的服务，需要管并发、管缓存、管把不同模块串起来」——本讲就够你读懂了。

## 3. 本讲源码地图

本讲只读两类「说明性」文件，它们是整个项目最权威的定位与架构说明：

| 文件 | 作用 | 本讲用它来 |
| --- | --- | --- |
| [README.md](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/README.md) | 项目的对外名片，给出定位、被服务模型清单、快速入口 | 确认项目定位、五大职责、模型分类 |
| [docs/developer_reference/main.md](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/main.md) | 面向开发者的架构总览，含主链路、分层职责表、目录结构 | 理解请求主链路与分层职责 |

此外，为了让「多阶段」不再抽象，本讲会附带引用一个**真实模型配置**作为例子：

| 文件 | 作用 |
| --- | --- |
| [sglang_omni/models/qwen3_omni/config.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py) | Qwen3-Omni 的管线配置，声明了真实存在的全部 stage，是多阶段概念最生动的实例 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 项目定位：SGLang-Omni 是什么**
- **4.2 多阶段运行时：把一次生成拆成异构阶段**
- **4.3 与上游 SGLang 的关系：分工边界**

---

### 4.1 项目定位：SGLang-Omni 是什么

#### 4.1.1 概念说明

一句话定位：**SGLang-Omni 是一个面向 omni、语音、TTS 模型的「多阶段（multi-stage）推理服务运行时」**。

要理解这句话，先理解它要解决的麻烦。一个 omni 模型（比如 Qwen3-Omni）端到端处理一次「看图 + 听语音 → 回复文字和语音」的请求时，内部并不是一个单一的大网络一跑到底，而是由若干**性质完全不同**的子模块接力完成：

- 有的子模块只做一次**预处理**（把图/音频归一化、切片）；
- 有的子模块是**编码器**（把图、音频编码成特征向量）；
- 有的子模块是**自回归引擎（AR engine）**，一个 token 一个 token 地生成，还要管 KV cache、批处理、并发；
- 有的子模块是 **talker**（负责生成语音的「语义码」）；
- 有的子模块是 **vocoder**（把码还原成可播放的波形）。

这些子模块的「计算模式、依赖结构、资源需求」差别极大。如果你硬要把它们塞进一个统一框架，要么牺牲 AR 引擎的高性能，要么让简单的预处理背负沉重机制。**SGLang-Omni 的设计目标，就是专门为「这种被拆成异构阶段的生成过程」做一个运行时。**

框架自带的 **五大职责**（后面每个模块都会再细化）：

1. **管线拓扑（pipeline topology）**：声明有哪些阶段、阶段之间怎么连。
2. **阶段生命周期（stage lifecycle）**：每个阶段什么时候启动、收到请求、完成、中止。
3. **跨阶段传输（inter-stage transport）**：阶段之间怎么把（往往是 GPU 上的大）张量搬过去。
4. **模型族集成层（model-family integration layer）**：把一类模型（如 Qwen3-Omni、某 TTS）按统一约定接进来。
5. **OpenAI 兼容 API 面（serving surface）**：对外暴露 `/v1/chat/completions` 这类标准接口。

#### 4.1.2 核心流程

从一个外部使用者的视角，SGLang-Omni 的整体形态是这样的：

```text
        用户/客户端（curl, OpenAI SDK, ...）
                     │  HTTP（OpenAI 兼容接口）
                     ▼
        ┌─────────────────────────────────┐
        │     SGLang-Omni 运行时          │
        │  ┌───────────────────────────┐  │
        │  │ HTTP API → Client → ...   │  │  ← 请求主链路（4.3 会展开）
        │  │ → Coordinator → Stage →   │  │
        │  │ Scheduler → ModelRunner   │  │
        │  └───────────────────────────┘  │
        │  跨阶段传输 / 模型族集成 / 配置  │
        └─────────────────────────────────┘
                     │  按需复用
                     ▼
            上游 SGLang（高性能 AR 调度）
```

也就是说：**对外的脸是 OpenAI 兼容接口；内部是「请求主链路 + 多阶段编排」；底层的 AR 高性能调度则交给 SGLang。**

#### 4.1.3 源码精读

项目最权威的一句话定位在 README 的 About 段落：

> SGLang-Omni is a multi-stage serving runtime for omni, speech, and TTS models. Its design target is multi-stage decoding ... SGLang-Omni owns the pipeline topology, stage lifecycle, inter-stage transport, model-family integration layer, and OpenAI-compatible serving surface, while composing with SGLang for high-performance autoregressive scheduling and model execution where applicable.

这一段把「是什么 + 解决什么 + 拥有什么 + 复用什么」全说清了：

- [README.md:L34-L36](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/README.md#L34-L36) — About 标题与一句话定位，明确「multi-stage serving runtime」。
- [README.md:L38-L41](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/README.md#L38-L41) — 四个特性要点，分别对应：多阶段运行时、阶段特化调度、传输感知执行、API 面。注意这四条正是上文「五大职责」的浓缩版（拓扑/生命周期没单列条目，而是隐含在 multi-stage 与 scheduling 里）。

开发者文档 main.md 开头再次强调同样定位，并把 omni 模型定义为「混合输入、多种输出」的模型：

- [docs/developer_reference/main.md:L3-L5](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/main.md#L3-L5) — 「SGLang-Omni is the multi-stage runtime for omni models: models that accept mixed text, image, audio, and video inputs and may emit text, audio, or other modalities.」

> 小贴士：README 是对外名片（说「能干嘛」），main.md 是对内架构（说「怎么搭的」）。后续读源码遇到「这玩意到底算哪一层」的疑问，回 main.md 的分层表查最准。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：把 README 的「特性要点」逐条对应到框架职责，证明你能从源码文档里「读出」项目结构。

**操作步骤**：

1. 打开 [README.md:L38-L41](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/README.md#L38-L41)。
2. 为四个要点各抄一句中文，并填进下表第三列。

| README 要点 | 对应框架职责 | 用你自己的话解释（待你填写） |
| --- | --- | --- |
| Multi-stage runtime | 拓扑 + 生命周期 | ______ |
| Stage-specialized scheduling | 阶段生命周期 / 执行 | ______ |
| Transport-aware execution | 跨阶段传输 | ______ |
| API surface | OpenAI 兼容 API 面 | ______ |

**需要观察的现象**：你会发现 README 的四个要点和「五大职责」并非一一对应——「模型族集成层」没有被单独列成一条要点。这正是想让你注意的：**有些职责藏在描述里，需要你结合 main.md 才能补全。**

**预期结果**：你能写出 4 条通顺的中文解释，并指出「模型族集成层」对应 README 里 `models/` 那一类「模型相关代码」的存在（具体见 4.2 与 u1-l3）。

#### 4.1.5 小练习与答案

**练习 1**：如果只看 README 的 About 段落，SGLang-Omni 一共「拥有」哪些职责？请全部列出。

**参考答案**：pipeline topology（管线拓扑）、stage lifecycle（阶段生命周期）、inter-stage transport（跨阶段传输）、model-family integration layer（模型族集成层）、OpenAI-compatible serving surface（OpenAI 兼容 API 面），共五项。

**练习 2**：SGLang-Omni 对外暴露的接口风格是什么？为什么这很重要？

**参考答案**：OpenAI 兼容接口。重要是因为大量现有客户端（OpenAI SDK、各种 Agent 框架）可以几乎零改动地接进来，降低迁移成本。

---

### 4.2 多阶段运行时：把一次生成拆成异构阶段

#### 4.2.1 概念说明

「多阶段」是整个项目的灵魂。README 把它描述成：

> SGLang-Omni models generation as coordinated stages: preprocessing, encoders, autoregressive engines, talkers, decoders, vocoders, and aggregators.

翻译过来，一次生成被建模成下面这些**协同（coordinated）的阶段**：

| 阶段类别 | 大白话 | 典型计算特点 |
| --- | --- | --- |
| preprocessing（预处理） | 把用户的图/音/视频/文本规范化 | 轻量、CPU 友好、一次一条 |
| encoders（编码器） | 把图/音频编码成特征 | 一次前向，非 AR |
| autoregressive engines（AR 引擎） | 一个 token 一个 token 生成 | 重型、需 KV cache、批处理、并发 |
| talkers | 生成语音的「语义码」 | AR，但目标是语音码本 |
| decoders（解码器） | 把生成的 token 流变成最终文本 | 轻量、流式友好 |
| vocoders | 把语音码还原成可播放波形 | 流式、按 chunk 出声 |
| aggregators（聚合器） | 把多路输入汇成一份给下游 | fan-in，等齐多源 |

**为什么要拆？** 因为不同阶段的「最优运行方式」差别太大：

- AR 引擎要追求极致吞吐，需要 continuous batching、KV cache、radix tree 复用——这是上游 SGLang 的强项。
- 预处理则完全不需要这些重型机制，套上 AR 调度器反而是累赘。
- vocoder 要「边算边出声」（流式），逻辑和 AR 引擎又不一样。

把它们拆成独立阶段、各自接上**最适合的调度器**，是这个运行时相对「单一大模型框架」的核心价值。

#### 4.2.2 核心流程

多阶段之间不是简单的「一条直线」，而是带 **fan-out（扇出）/ fan-in（扇入）/ stream（流式）** 的有向图。用 Qwen3-Omni（带语音）的真实拓扑举例：

```text
                preprocessing
                 /     |      \        ← fan-out：一路请求拆成三条
                v      v       v
        image_encoder  audio_encoder
                 \      |       /
                  v     v       v
                    mm_aggregate            ← fan-in：等齐三路并 merge
                     /        \
                    v          v
                thinker   ──stream──► talker_ar   ← thinker 把 hidden state 流式喂给 talker
                    |                       |
                    v                       v
                 decode(text)            code2wav(vocoder, audio)
                 (terminal)              (terminal)
```

两个概念解释一下：

- **fan-out（扇出）**：一个阶段的输出要发给多个下游（如 preprocessing → image/audio/aggregate）。
- **fan-in（扇入）**：一个阶段要等齐多个上游才动手（如 mm_aggregate 要等 preprocessing + image_encoder + audio_encoder）。
- **stream（流式）**：thinker 还没生成完，就把它已生成的 hidden state 一路流给 talker_ar，让 talker 提前开工。

**一个直觉性的延迟公式**（仅用于理解，不是精确模型）：端到端延迟大致等于「各阶段计算时间」+「跨阶段传输时间」+「等齐上游的等待时间」之和：

\[
T_{\text{e2e}} \approx \sum_{i} T_{\text{stage}_i}^{\text{compute}} \;+\; \sum_{e} T_{\text{transport}_e} \;+\; \sum_{j} T_{\text{wait}_j}
\]

多阶段运行时存在的意义，正是要在「拆得够细以各取所长」和「别让传输/等待把收益吃光」之间找到平衡——这就是为什么跨阶段传输（transport）会单独成为一个核心职责。

#### 4.2.3 源码精读

README 用一句话列出了这些阶段类别：

- [README.md:L38](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/README.md#L38) — 「preprocessing, encoders, autoregressive engines, talkers, decoders, vocoders, and aggregators」。

光看分类还是抽象。下面看 Qwen3-Omni 配置里**真实存在的 stage**，你会发现抽象分类几乎能一一对应。`config.py` 用 `_text_stages()` 构造纯文本管线、用 `_speech_stages()` 构造带语音管线：

- [sglang_omni/models/qwen3_omni/config.py:L35-L58](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L35-L58) — `preprocessing` 阶段，`next=["image_encoder", "audio_encoder", "mm_aggregate"]`，这就是上图的 **fan-out**。
- [sglang_omni/models/qwen3_omni/config.py:L89-L111](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L89-L111) — `mm_aggregate` 阶段，`wait_for=["preprocessing", "image_encoder", "audio_encoder"]` 配 `merge_fn=...`，这就是 **fan-in + 合并**。
- [sglang_omni/models/qwen3_omni/config.py:L125-L152](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L125-L152) — `thinker` 阶段（AR 引擎），`stream_to=["talker_ar", "decode"]`，这就是把 hidden state **流式**喂给下游。
- [sglang_omni/models/qwen3_omni/config.py:L155-L162](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L155-L162) — `decode` 阶段，`terminal=True`，是**终态**（文本输出在这里收口）。
- [sglang_omni/models/qwen3_omni/config.py:L200-L209](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L200-L209) — `code2wav` 阶段（**vocoder**），同样是 `terminal=True`（音频输出在这里收口）。

把真实 stage 映射回 README 的抽象分类：

| README 抽象分类 | Qwen3-Omni 真实 stage |
| --- | --- |
| preprocessing | `preprocessing` |
| encoders | `image_encoder`、`audio_encoder` |
| aggregators | `mm_aggregate` |
| autoregressive engines | `thinker` |
| talkers | `talker_ar` |
| decoders | `decode` |
| vocoders | `code2wav` |

> 注意：`terminal=True` 是一个关键标志，表示「这个阶段的产出就是最终结果之一」。一条管线可以有**多个终态**（这里文本走 `decode`、语音走 `code2wav`），结果会在更上层被合并——这是后续 Coordinator（u2-l4）的核心职责。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：亲手从真实配置里把多阶段 DAG 画出来，建立「抽象分类 ↔ 真实代码」的肌肉记忆。

**操作步骤**：

1. 打开 [sglang_omni/models/qwen3_omni/config.py:L212-L220](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L212-L220) 的 `_text_stages()`（纯文本管线，最简单）。
2. 列出它包含的 stage 名字：`preprocessing / image_encoder / audio_encoder / mm_aggregate / thinker / decode`。
3. 仿照本节 4.2.2 的拓扑图，画出纯文本管线的 DAG（提示：纯文本时 `thinker` 没有 `stream_to`，`decode` 是唯一终态）。

**需要观察的现象**：

- 纯文本管线只有 **一个** `terminal=True` 的阶段（`decode`），而带语音管线有两个（`decode` + `code2wav`）。
- `preprocessing` 的 `next` 是个列表（多目标），`mm_aggregate` 有 `wait_for`（多源）。

**预期结果**：你画出的纯文本 DAG 大致是

```text
preprocessing ──fan-out──► image_encoder ──┐
        │────────────────► audio_encoder ──┼──fan-in──► mm_aggregate ──► thinker ──► decode(terminal)
        └─────────────────────────────────────┘
```

> 待本地验证：如果你之后（u1-l2）装好环境，可以用 `sgl-omni config export` 把这份配置导出成 YAML，对照本图检查 stage 名是否一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 preprocessing 不直接套用 AR 引擎那套调度器？

**参考答案**：preprocessing 一次只处理一条提示、是 CPU 友好的轻量操作，不需要 KV cache / continuous batching 这些为 AR 吞吐设计的重型机制；套上反而增加开销和复杂度。多阶段的意义就是让每个阶段用「最适合自己的调度器」。

**练习 2**：`terminal=True` 表示什么？一条管线能有几个 terminal 阶段？

**参考答案**：表示该阶段的产出是最终结果之一（不再有下游）。一条管线可以有多个 terminal 阶段，例如 Qwen3-Omni 带语音时，文本收口在 `decode`、语音收口在 `code2wav`，都是 terminal。多个终态的结果会在上层（Coordinator）被合并。

**练习 3**：thinker 用 `stream_to` 把数据喂给 talker_ar，而不是等 thinker 全部生成完再一次性传——这样设计的主要好处是什么？

**参考答案**：让 talker（乃至 vocoder）能在 thinker 还没结束时就提前开工，从而降低端到端首字/首音延迟（TTFT），实现「边想边说」的流式体验。这正是多阶段 + 流式传输的价值。

---

### 4.3 与上游 SGLang 的关系：分工边界

#### 4.3.1 概念说明

很多人第一反应是：「SGLang 已经是一个很强的 LLM 推理引擎，SGLang-Omni 是不是重复造轮子？」——不是。两者分工明确：

- **上游 SGLang**：擅长**高性能的自回归（AR）调度与模型执行**——continuous batching、KV cache 管理、radix tree 复用、张量并行等。它面向的是「一个或一类 AR 模型的高吞吐服务」。
- **SGLang-Omni**：擅长**把异构的多阶段串起来**——管线拓扑、阶段生命周期、跨阶段传输、模型族集成、OpenAI 兼容多模态 API。

关键关系词是 README 里的 **composing with**（组合/复用）：SGLang-Omni 在「需要高性能 AR」的地方**复用** SGLang，自己并不重写一套 AR 引擎；而在「SGLang 不关心」的地方（跨阶段搬张量、多模态输入、流式语音、多终态合并），由 Omni 自己负责。

打个比方：SGLang 是一台顶尖的「发动机」，SGLang-Omni 是一辆为多模态/语音定制的「整车」，它把多台不同型号的发动机和部件（编码器、AR 引擎、vocoder……）组装在一起、接好油路（跨阶段传输）、装上方向盘和仪表盘（OpenAI API、Profiler），并在需要大马力的位置直接装上 SGLang 这台发动机。

#### 4.3.2 核心流程

从「一次请求」看分工，主链路如下（main.md 的权威描述）：

```text
HTTP API → Client → Coordinator → Stage → Scheduler → ModelRunner → model forward
```

- `HTTP API`：OpenAI 兼容接口（Omni 负责）。
- `Client`：把 `GenerateRequest` 转成内部 `OmniRequest`、聚合结果（Omni 负责）。
- `Coordinator`：全局请求路由，送到入口阶段、收终态结果（Omni 负责）。
- `Stage`：阶段的 IO 外壳，桥接 scheduler（Omni 负责）。
- `Scheduler`：每阶段的执行循环（Omni 负责；其中 AR 阶段的调度会**复用 SGLang**）。
- `ModelRunner`：AR 前向准备与模型前向分发（Omni 负责，底层执行可借助 SGLang）。

注意边界：**链路的「形状」由 Omni 决定，链路中「AR 阶段的高性能执行」由 SGLang 提供。** 这就是「Omni 拥有拓扑与生命周期，SGLang 提供高性能 AR」的落地方式。

#### 4.3.3 源码精读

主链路是 main.md 给出的最权威一行：

- [docs/developer_reference/main.md:L8-L11](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/main.md#L8-L11) — System Overview 图示 `HTTP API -> Client -> Coordinator -> Stage -> Scheduler -> ModelRunner -> model forward`。

分层职责表把每一层「只做什么」说得很清楚：

- [docs/developer_reference/main.md:L14-L24](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/main.md#L14-L24) — 分层职责表。重点看：HTTP API（OpenAI 兼容 + SSE）、Client（`GenerateRequest`→`OmniRequest` + 音频编码）、Coordinator（请求生命周期 + 中止广播）、Stage（控制面/数据面 IO + fan-in + stream 路由 + scheduler inbox/outbox 桥接）、Scheduler（每阶段执行循环）、ModelRunner（AR 前向准备 + 输出抽取）、Communication（控制面消息 + relay 数据传输）。

README 的 About 段落则用一句话锁定与 SGLang 的边界：

- [README.md:L36](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/README.md#L36) — 「…while composing with SGLang for high-performance autoregressive scheduling and model execution where applicable.」「where applicable」很重要——Omni 只在**适用处**复用 SGLang，非 AR 阶段（预处理、聚合、流式 vocoder）并不需要它。

> 边界速记：**拓扑/生命周期/传输/集成/API 归 Omni；AR 高性能调度与执行归 SGLang。**

#### 4.3.4 代码实践（本讲核心实践任务）

> 这正是本讲规格里指定的实践任务。

**实践目标**：用一段话向同事讲清「SGLang-Omni 与 SGLang 各自负责什么」，并从 README 举出三类被服务模型的真实例子，证明你理解了定位与分工。

**操作步骤**：

1. 打开 [README.md:L43-L50](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/README.md#L43-L50) 的「What SGLang-Omni Serves」一节。
2. 写一段**不超过 4 句**的话，向同事解释分工。要求覆盖：① Omni 是多阶段运行时；② Omni 拥有哪些职责；③ 在何处复用 SGLang；④ 举一个非 AR、不需要 SGLang 的阶段例子。
3. 从 README 中分别挑出 **omni / TTS / ASR** 三类模型各一个具体名字，填进下表。

**参考分工话术**（你可以改写）：
> 「SGLang-Omni 是为 omni/语音/TTS 模型设计的多阶段推理服务运行时，它负责管线拓扑、阶段生命周期、跨阶段传输、模型族集成和 OpenAI 兼容 API；而真正吃性能的自回归（AR）调度与执行，它直接复用上游 SGLang。比如预处理、聚合、流式 vocoder 这些非 AR 阶段，就由 Omni 自己用更轻量的调度器跑，不需要 SGLang。」

**模型分类表（待你填写，参考答案在下方）**：

| 模型类别 | 含义 | 从 README 挑一个例子 |
| --- | --- | --- |
| omni | 多模态输入 + 多模态输出（thinker-talker 管线） | ______ |
| TTS | 文本转语音 | ______ |
| ASR | 语音转文字（转录 / 说话人分离） | ______ |

**需要观察的现象**：README 把这三类模型分别放在三个不同的 bullet 里，并且 ASR 通过 `response_format=verbose_json` 还能做说话人标注与时间戳——这说明同一套运行时服务了形态差异很大的模型。

**预期结果（参考答案）**：

| 模型类别 | 含义 | 例子（取自 README L45-L47） |
| --- | --- | --- |
| omni | 多模态输入输出、thinker-talker 管线 | **Qwen3-Omni**（或 Ming-Omni） |
| TTS | 文本转语音 | **Higgs Audio v3**（或 MOSS-TTS、Qwen3-TTS 等） |
| ASR | 语音转文字 / 说话人分离 | **Qwen3-ASR**（或 MOSS-Transcribe-Diarize） |

> 对应源码：[README.md:L45](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/README.md#L45)（omni）、[README.md:L46](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/README.md#L46)（TTS）、[README.md:L47](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/README.md#L47)（ASR）。

#### 4.3.5 小练习与答案

**练习 1**：判断题——「SGLang-Omni 自己重新实现了一套 AR 引擎来替代 SGLang。」对还是错？

**参考答案**：错。SGLang-Omni 通过 composing with SGLang，在需要高性能 AR 调度的地方**复用** SGLang，而不是重写。

**练习 2**：下面哪些是 SGLang-Omni **自己**的职责（而非 SGLang 的）？多选：A) continuous batching；B) 跨阶段把 GPU 张量从 thinker 搬到 talker；C) OpenAI 兼容 `/v1/chat/completions` 接口；D) 维护 KV cache radix tree。

**参考答案**：B、C。A、D 属于 AR 引擎的高性能调度/缓存，由 SGLang 提供；B 是跨阶段传输（Omni 职责）；C 是 API 面（Omni 职责）。

**练习 3**：为什么 README 说复用 SGLang 是「where applicable」（适用处）？

**参考答案**：因为不是所有阶段都需要 AR 调度。预处理、聚合、流式 vocoder 等阶段用更轻量的调度器即可，套用 SGLang 反而是负担；只有 thinker、talker 这类 AR 阶段才值得接入 SGLang 的高性能调度。

---

## 5. 综合实践

**任务：制作一页「SGLang-Omni 速查卡」，把本讲全部内容串起来。**

请用一张图/一张表完成以下四件事，**所有结论必须能在本讲引用的源码里找到出处**：

1. **画主链路**：抄下 `HTTP API → Client → Coordinator → Stage → Scheduler → ModelRunner → model forward`，并给每层写一句「只负责什么」（参考 [docs/developer_reference/main.md:L14-L24](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/main.md#L14-L24)）。
2. **标分工**：在主链路上用两种颜色/记号区分「Omni 负责」与「（AR 处）复用 SGLang」。
3. **画阶段 DAG**：用 Qwen3-Omni 带语音的拓扑（参考 4.2.2），标出 fan-out / fan-in / stream，并圈出所有 `terminal=True` 的阶段。
4. **列模型清单**：从 README 列出 omni / TTS / ASR 各一个真实模型名。

**验收标准**：

- 速查卡上没有任何「编造」内容——每条结论都能点到本讲给出的某个永久链接。
- 你能用 1 分钟对着速查卡向一个完全没接触过本项目的人讲清「它是什么、怎么分层、和 SGLang 什么关系」。

> 提示：这张速查卡建议保留下来，后续每一讲都会在它的某一层「打洞深入」——u2 讲 HTTP/Client/Coordinator，u3 讲 Stage/传输/进程，u4 讲 Scheduler/ModelRunner。

## 6. 本讲小结

- **SGLang-Omni 是什么**：面向 omni、语音、TTS 模型的**多阶段推理服务运行时**。
- **核心动机**：一次生成由性质迥异的子模块接力完成，强行统一会牺牲性能或徒增复杂度，故拆成异构阶段。
- **七大阶段类别**：preprocessing / encoders / autoregressive engines / talkers / decoders / vocoders / aggregators，Qwen3-Omni 的真实 stage 与之一一对应。
- **五大职责**：管线拓扑、阶段生命周期、跨阶段传输、模型族集成层、OpenAI 兼容 API 面。
- **请求主链路**：`HTTP API → Client → Coordinator → Stage → Scheduler → ModelRunner → model forward`。
- **与 SGLang 的边界**：Omni 拥有拓扑/生命周期/传输/集成/API；高性能 AR 调度与执行在「适用处」复用上游 SGLang。

## 7. 下一步学习建议

你现在知道了「是什么」，接下来要解决「怎么跑起来」。建议按顺序往下学：

1. **u1-l2 环境搭建与安装**：学会用 Docker / `uv` 装好 SGLang-Omni，跑通 `sgl-omni --help`。
2. **u1-l3 目录结构与代码组织**：把 main.md 的 Directory Layout 对应到 `sglang_omni/` 的真实子目录，建立「框架层 vs 模型层」的地图感。
3. **u1-l4 启动 API Server 与第一次请求**：亲手起一个服务，发第一条 `/v1/chat/completions`，把本讲的主链路「跑通」一遍。

> 进阶提示：当你能跑通服务后，**u2-l1（请求主链路总览）** 会把本讲的 `HTTP→...→ModelRunner` 这条链路逐层拆开讲透，建议把它作为本讲的自然延续。
