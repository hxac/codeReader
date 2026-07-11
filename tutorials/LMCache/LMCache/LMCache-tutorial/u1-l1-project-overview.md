# LMCache 是什么：KV Cache 管理层定位

## 1. 本讲目标

本讲是 LMCache 学习手册的第一篇，目标是从零开始讲清楚一件事：**LMCache 到底是什么、它解决了什么问题**。

读完本讲，你应该能够：

1. 说出 KV cache 是什么，并解释它为什么会占用大量 GPU 显存。
2. 说明 LMCache 作为「KV cache 管理层」的定位，以及它「vendor-neutral（厂商中立）」的含义。
3. 解释 LMCache 的核心价值：降低 TTFT、提升吞吐、在跨请求 / 跨会话 / 跨引擎之间复用 KV cache。
4. 列举 LMCache 的关键特性（分层存储、可观测性、可插拔后端、非前缀复用、PD 分离、可插拔 KV 变换）。
5. 知道在哪里查找 LMCache 的设计文档（`docs/design/`），并掌握它的目录镜像约定。

本讲**不需要**你已经读过任何 LMCache 源码，也不需要你熟悉 vLLM 等推理引擎的内部实现。

## 2. 前置知识

在进入 LMCache 之前，先用最通俗的方式过一遍几个基础概念。如果你已经熟悉，可以跳到第 3 节。

### 2.1 LLM 推理的两个阶段：prefill 与 decode

当你向大语言模型（LLM）发送一段 prompt 时，推理过程可以粗略地分为两个阶段：

- **prefill（预填充）阶段**：模型一次性处理你输入的全部 token（例如一整段问题 + 检索到的文档），为每一个输入 token 计算 attention 所需的 Key / Value。这一步是**计算密集型**的，往往决定了「收到请求到吐出第一个字」的延迟。
- **decode（解码）阶段**：模型每次只生成一个新 token，每生成一个新 token 都要让它「注意（attend）」前面所有 token。这一步是**访存密集型**的。

工程上常把「从收到请求到第一个 token 输出」的时间称为 **TTFT（Time-To-First-Token）**，把单位时间能处理的请求数称为**吞吐（throughput）**。LMCache 的很多优化都是围绕这两个指标展开的。

### 2.2 什么是 KV cache

Transformer 的核心是 self-attention。为了让后续 token 能快速「回看」前面的 token，模型会把前面每个 token 在每一层算出的 **Key（K）** 和 **Value（V）** 向量缓存下来，这个缓存就叫 **KV cache**。

有了 KV cache，decode 阶段就不必反复重算历史 token 的 K/V，否则计算量会随序列长度二次膨胀。但代价是：**KV cache 非常占显存**。我们会在 4.1 节用公式算清楚它有多大。

> 术语提示：本讲中的「缓存（cache）」有时指 KV cache（模型注意力计算的中间状态），有时指 LMCache 这个「缓存管理层」。语境清楚时不会混淆，关键处我会显式区分。

## 3. 本讲源码地图

本讲只涉及两个「文件级」的最小模块，目的是建立整体认知，不深入任何具体实现。

| 文件 / 目录 | 作用 | 本讲用它做什么 |
|---|---|---|
| `README.md` | 项目的门面文档，给出 LMCache 的定位、关键特性、安装方式、生态 | 作为 LMCache「自我介绍」的一手材料，逐条精读它的 About 与 Key features |
| `docs/design/README.md` | 设计文档体系的说明，规定设计文档的存放约定 | 学习如何用「镜像 `lmcache/` 包树」的方式找到任意模块的设计文档 |

后续讲义才会进入 `lmcache/` 包内部的具体源码（如 `lmcache/v1/cache_engine.py`）。本讲不要求你打开它们。

## 4. 核心概念与源码讲解

### 4.1 KV Cache 是什么：为什么它占用大量显存

#### 4.1.1 概念说明

KV cache 的本质是：**为已经处理过的每一个 token，在每一层都保存一对 Key / Value 向量**。它的存在让 attention 的计算从「每次都重算全部历史」变成「只算新 token + 读取缓存」，是现代 LLM 高效推理的基础。

但它的副作用是显存消耗随上下文长度**线性增长**。当上下文变长（长文本、多轮对话、RAG 拼接文档）或并发请求变多时，KV cache 会迅速吃光 GPU 显存，成为推理系统的「记忆瓶颈」。

这就引出了 LMCache 要解决的核心矛盾：**KV cache 既宝贵（重算它很贵）、又臃肿（存它很占地方），而且很多时候是可复用的（不同请求会共享相同的前缀或文档）**。

#### 4.1.2 核心流程

先看一条请求里 KV cache 是怎么产生和被使用的：

```text
用户输入 prompt（N 个 token）
        │
        ▼
  prefill 阶段：一次性计算这 N 个 token 在每一层的 K / V
        │
        ▼
  把 K / V 存进 GPU 上的 KV cache（占用显存）
        │
        ▼
  decode 阶段：每生成一个新 token，读取全部历史 K/V 做 attention
        │
        ▼
  请求结束 → 传统实现里 KV cache 随即丢弃（浪费！）
```

传统推理引擎在请求结束后会丢弃 KV cache。但如果下一条请求里有**相同的前缀**（例如固定的系统提示词、同一份检索文档），这些被丢弃的 KV cache 本可以**直接复用**，省掉一整轮 prefill 计算——这正是 LMCache 的切入点。

我们来量化「KV cache 到底多大」。对一个使用 GQA（Grouped-Query Attention）的 Transformer 模型，单条序列的 KV cache 大小约为：

\[
\text{KV 字节数} = 2 \times n_{\text{layers}} \times n_{\text{kv\_heads}} \times d_{\text{head}} \times b \times L
\]

其中：

- 系数 \(2\) 表示 Key 和 Value 两份；
- \(n_{\text{layers}}\) 是 transformer 层数；
- \(n_{\text{kv\_heads}}\) 是 KV 头数（GQA 下通常远小于 query 头数）；
- \(d_{\text{head}}\) 是每个头的维度；
- \(b\) 是单个元素的字节数（bf16 为 \(2\)）；
- \(L\) 是序列长度（token 数）。

代入一组接近真实模型的数字估算「每个 token 的 KV 开销」：

\[
\text{每 token KV} = 2 \times 32 \times 8 \times 128 \times 2 = 131\,072 \text{ 字节} = 128 \text{ KiB}
\]

也就是每存一个 token 的 KV 大约需要 128 KiB。那么一条 8K 上下文（\(L = 8192\)）的请求：

\[
128 \text{ KiB} \times 8192 = 1\,048\,576 \text{ KiB} \approx 1 \text{ GiB}
\]

也就是说，**一条 8K 请求的 KV cache 就要约 1 GiB 显存**。当并发上几十上百、上下文再拉长，GPU 显存会被 KV cache 撑爆。这就解释了为什么需要专门的「KV cache 管理层」来搬运、存放、复用这些数据。

#### 4.1.3 源码精读

README 的 About 段落把 LMCache 与「prefill 重算」的痛点直接关联起来，明确指出 LMCache 的目的是「减少重复的 prefill 计算、改善 TTFT」：

[README.md:66](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/README.md#L66-L66) —— 关键特性「Persistent, tiered KV cache offloading and reuse」原文提到：把 KV cache 从 GPU 搬到分层存储里，以**跨请求 / 跨会话 / 跨引擎实例复用**，从而 **reduce repeated prefill computation and improve TTFT（减少重复 prefill 计算、改善 TTFT）**。

这一句既是 4.1 节「传统实现丢弃 KV cache 是浪费」的官方表述，也预告了后面 4.3 节要讲的核心价值。

#### 4.1.4 代码实践

1. **实践目标**：用一个公式直观体会 KV cache 的显存压力，建立「为什么需要 LMCache」的直觉。
2. **操作步骤**：
   - 打开一个 Python 交互环境（或心算）。
   - 任选一组你熟悉的模型参数（层数、KV 头数、头维度、dtype）。
   - 用本节的公式算出「每 token KV 字节数」和「一条 8K 请求的总 KV」。
3. **需要观察的现象**：上下文长度 \(L\) 翻倍时，KV cache 字节数也大致翻倍（线性关系）。
4. **预期结果**：对于 4.1.2 节的参数，每 token ≈ 128 KiB，一条 8K 请求 ≈ 1 GiB。
5. 如果你用的是另一种模型（例如不同的 KV 头数），数值会不同，但「线性增长 + 量级可观」的结论不变。具体到你机器上的真实数字属「待本地验证」。

> 这是一个「纸笔 / 计算器型」实践，本讲不要求你启动任何服务。后续讲义（如 u1-l6）会有真正调用代码的实践。

#### 4.1.5 小练习与答案

**练习 1**：如果把序列长度从 8K 提到 32K，单条请求的 KV cache 大约变成多少？

**参考答案**：因为 KV cache 随 \(L\) 线性增长，\(32\text{K} / 8\text{K} = 4\)，所以约 \(1\text{ GiB} \times 4 = 4\text{ GiB}\)。

**练习 2**：GQA（减少 KV 头数）相比传统 MHA，对 KV cache 大小有什么影响？

**参考答案**：KV cache 字节数与 \(n_{\text{kv\_heads}}\) 成正比。GQA 把 KV 头数压小，等比例缩小了 KV cache，这就是 GQA 在长上下文下省显存的原因。

---

### 4.2 LMCache 的定位：KV Cache 管理层

#### 4.2.1 概念说明

LMCache 把自己定义为 **「a KV cache management layer for LLM inference」（LLM 推理的 KV cache 管理层）**。注意「层（layer）」这个词：它不是一个完整的推理引擎，而是一个**横跨在推理引擎与存储之间**的中间层，专门负责 KV cache 的存放、搬运、复用与监控。

类比一下：操作系统里有「内存管理子系统」专门管内存页的换入换出；LMCache 之于 LLM 推理，就像一个专门管 KV cache 的「换入换出 + 缓存复用」子系统。

LMCache 还强调自己是 **vendor-neutral（厂商中立）**：它不绑死某一家推理引擎、某一种硬件或某一种存储。用户可以在不同推理引擎、不同存储供应商之间自由切换，并继续复用已经存下来的 KV cache。

#### 4.2.2 核心流程

把 LMCache 放进整个推理栈里看它的位置：

```text
┌──────────────────────────────────────────────┐
│  应用 / 业务（多轮对话、RAG、Agent……）         │
└──────────────────────────────────────────────┘
                  │  请求 / prompt
                  ▼
┌──────────────────────────────────────────────┐
│  推理引擎（vLLM / SGLang / TensorRT-LLM …）   │  ← 可替换
└──────────────────────────────────────────────┘
        │                          ▲
   存 KV / 取 KV               返回命中的 KV
        ▼                          │
┌──────────────────────────────────────────────┐
│           LMCache（KV cache 管理层）          │  ← 本项目
│  分层存储 · 复用 · 可观测 · 可插拔后端 ……      │
└──────────────────────────────────────────────┘
        │                          ▲
   搬运 / 持久化                远端取回
        ▼                          │
┌──────────────────────────────────────────────┐
│  CPU 内存 / 本地 SSD / Redis / S3 / NIXL ……   │  ← 可替换
└──────────────────────────────────────────────┘
```

关键点：上下两端（推理引擎、存储后端）都是「可替换」的，LMCache 是中间那层稳定的契约。

#### 4.2.3 源码精读

README 的 About 段落一句话定调，这是理解整个项目最重要的一句话：

[README.md:50](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/README.md#L50-L50) —— 「LMCache is a **KV cache management layer** for LLM inference. It turns KV cache from a temporary state into reusable *AI-native knowledge* that can be *stored* persistently, *reused* across multiple serving engines, *monitored* with an observability stack, and *transformed* for better generation quality.」

这句话信息密度很高，把它拆开看就是本讲后续几节的提纲：

- *stored* persistently → 4.4 分层存储
- *reused* across multiple serving engines → 4.3 跨引擎复用
- *monitored* → 4.4 可观测性
- *transformed* → 4.4 可插拔 KV 变换

紧接的下一句点出厂商中立：

[README.md:52](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/README.md#L52-L52) —— 「LMCache is **vendor-neutral**.」并说明用户可以在不同推理引擎与存储供应商之间自由切换而仍能复用已存的 KV cache。

#### 4.2.4 代码实践

1. **实践目标**：从一手文档里确认 LMCache 的定位，并把它和「推理引擎」区分开。
2. **操作步骤**：
   - 打开仓库根目录的 `README.md`，找到 `## About` 段落（约第 48 行起）。
   - 圈出定义 LMCache 身份的那一句话（即 4.2.3 节引用的第 50 行）。
   - 在该段落里找到列出「vendor-neutral」的句子，记下它列举了哪几类「可替换对象」（推理引擎 / 硬件 / 存储 / 基础设施提供商）。
3. **需要观察的现象**：你会看到 LMCache 明确把自己描述成「层」而不是「引擎」。
4. **预期结果**：你能在 About 段落里同时找到「KV cache management layer」与「vendor-neutral」两处表述。
5. 本实践无需运行命令。

#### 4.2.5 小练习与答案

**练习 1**：LMCache 是不是一种推理引擎？为什么？

**参考答案**：不是。它是「KV cache 管理层」，作用在推理引擎之下 / 旁路，负责 KV cache 的存储与复用；真正的 token 生成仍由 vLLM 等推理引擎完成。

**练习 2**：「vendor-neutral」对用户意味着什么实际好处？

**参考答案**：意味着用户不会被某一家引擎或存储锁死——可以从 vLLM 换到别的引擎，或从本地磁盘换到 Redis / S3，而之前积累的 KV cache 仍可复用，避免重算。

---

### 4.3 核心价值：降低 TTFT、提升吞吐、跨请求/会话/引擎复用

#### 4.3.1 概念说明

LMCache 的核心价值可以浓缩成一句话：**把「一次算完就扔」的 KV cache，变成可以反复取用的持久资产**。具体表现为三个收益：

1. **降低 TTFT**：当请求的前缀已经在缓存里命中，引擎不必再做那一长段 prefill，第一个字能更快吐出。
2. **提升吞吐**：省下的 prefill 算力可以服务更多请求，单位时间处理的请求数提高。
3. **跨范围复用**：复用不限于单条请求内部，而是可以跨越——
   - **跨请求（request）**：不同用户问了相同系统提示词 / 文档；
   - **跨会话（session）**：同一个多轮对话的上下文延续；
   - **跨引擎实例（engine instance）**：A 实例算出的 KV，B 实例也能用（借助远端共享存储）。

#### 4.3.2 核心流程

下面这条数据流是本讲最关键的一张图，也是本讲综合实践要你亲手画出来的：

```text
请求 1：[系统提示 + 文档 + 问题]
        │  prefill
        ▼
   计算出 KV cache
        │  LMCache.store(...)
        ▼
   存入分层存储（GPU → CPU → 磁盘 → 远端）
        │
        … 一段时间后 …
        │
请求 2：[系统提示 + 文档 + 另一个问题]   （前缀与请求 1 相同）
        │  LMCache.lookup(...)  → 命中！
        ▼
   直接取回已存的 KV，跳过该段 prefill
        │
        ▼
   更快的 TTFT + 更高的吞吐
```

注意三个动作：**store（存）→ lookup（查是否命中）→ retrieve（取回）**。这三者正是 LMCache 对外暴露的核心 API，会在后续 u1-l6 讲义里以 `LMCacheEngine` 的 `store / retrieve / lookup` 方法正式讲解。本讲你只需要建立这条「存 → 查 → 取」的直觉。

#### 4.3.3 源码精读

README 用一段话把「分层存储 + 跨范围复用 + 降低 TTFT」三者打包描述：

[README.md:66](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/README.md#L66-L66) —— 「Persistent, tiered KV cache offloading and reuse」明确写到 reuse across **requests, sessions, and engine instances**，并直接点出目的是 reduce repeated prefill computation and improve **TTFT**。

这里第一次出现了 LMCache 的几个关键术语，建议现在就记住它们的对应关系：

| README 原词 | 含义 | 对应收益 |
|---|---|---|
| offloading | 把 KV 从 GPU 搬到更便宜更大的存储 | 省显存 |
| tiered storage | 分层（CPU / 磁盘 / 远端）存储 | 容量与速度的折中 |
| reuse | 复用已存的 KV | 省算力 |
| requests / sessions / engine instances | 复用的三个跨度范围 | 跨请求 / 跨会话 / 跨引擎 |

#### 4.3.4 代码实践

1. **实践目标**：在示例目录里找到「KV cache 复用」的演示，建立直观印象。
2. **操作步骤**：
   - 在仓库根目录列出 `examples/` 目录，会看到名为 `kv_cache_reuse` 的示例。
   - 阅读该示例目录下的说明（`README` 或脚本头部注释），观察它演示的「第一次算、第二次命中」流程。
   - 同时留意 `examples/online_session`（端到端在线会话演示），后续 u4-l8 会用到。
3. **需要观察的现象**：示例会展示「相同前缀第二次请求时命中缓存、跳过 prefill」这一行为。
4. **预期结果**：你能用自己的话说出该示例里「store → 第二次 lookup 命中」发生在哪一步。
5. 是否能在你的机器上真正跑起来取决于 GPU 与依赖；若暂不具备环境，以「阅读示例代码 + 理解断言」的方式完成即可，运行结果属「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么「跨引擎实例复用」对多副本部署很重要？

**参考答案**：多副本部署里，请求可能被负载均衡到不同实例。若 KV cache 能共享（借助远端存储），那么在 A 实例算过的前缀，请求落到 B 实例时也能命中，避免每个副本都各自重算一遍。

**练习 2**：TTFT 和吞吐这两个指标，KV cache 复用分别主要改善哪一个？

**参考答案**：直接看，复用省掉了 prefill 这段等待，主要降低单条请求的 **TTFT**；同时省下的 prefill 算力让 GPU 能服务更多请求，从而**提升吞吐**。两者都会受益，但 TTFT 的改善对「首字延迟」最直观。

---

### 4.4 关键特性一览

#### 4.4.1 概念说明

README 的 `### Key features` 列出了 LMCache 的七大关键特性。本节只做「特性 → 解决什么问题」的概览，每一项的具体实现都会在后续讲义里展开。

#### 4.4.2 核心流程

下表把 README 列出的特性逐条映射到「它解决的问题」和「后续在哪一篇讲义深入」：

| 特性（README 原文简称） | 解决的问题 | 后续讲义 |
|---|---|---|
| Engine-independent deployment（独立 daemon） | 引擎崩溃时 KV cache 不丢失（no fate-sharing） | u3-1（MP 架构） |
| Persistent, tiered offloading | KV 太大，需分层搬到 CPU/磁盘/远端 | u2-3 / u4-2 |
| Production-level observability | 生产环境需要命中率、用量等指标 | u3-5 |
| Pluggable storage/transport backends | 可插拔接入 Redis、S3、NIXL、GDS 等 | u4-3 |
| Non-prefix KV reuse（CacheBlend） | 非前缀位置也能复用 KV | u2-6 |
| PD disaggregation & KV transfer | prefill 与 decode 分离时传 KV | u4-7 |
| Pluggable KV transformation（SERDE） | 可插拔压缩 / 量化 / 自定义序列化 | u4-4 / u2-7 |

#### 4.4.3 源码精读

下面把七条特性的原文逐条列出来，便于你回查（行号与 README 一一对应）：

- [README.md:64](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/README.md#L64-L64) —— **Engine-independent deployment**：LMCache 作为独立 daemon 进程管理 KV cache，**不与引擎共命运（no fate-sharing）**，引擎崩溃也不会丢缓存。
- [README.md:66](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/README.md#L66-L66) —— **Persistent, tiered KV cache offloading and reuse**：分层存储 + 跨范围复用，减少重复 prefill、改善 TTFT。
- [README.md:68](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/README.md#L68-L68) —— **Production-level KV cache observability**：提供健康监控、请求级 / token 级 prefix 命中、生命周期、用户级用量等指标。
- [README.md:70](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/README.md#L70-L70) —— **Pluggable storage and transport backends**：统一接口接入 CPU RAM、本地 SSD、Redis/Valkey、Mooncake、InfiniStore、S3 兼容对象存储、NIXL、GDS。
- [README.md:72](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/README.md#L72-L72) —— **Non-prefix KV reuse**：借助 CacheBlend，复用不局限于前缀，可在 prompt 任意位置复用 KV 块，并选择性重算少量 token 以恢复质量。
- [README.md:74](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/README.md#L74-L74) —— **PD disaggregation and KV transfer**：支持 prefill worker 到 decode worker 的 KV 传输（NVLink / RDMA / TCP，传输层如 NIXL）。
- [README.md:76](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/README.md#L76-L76) —— **Pluggable KV transformation**：给研究者的 SERDE 接口，可写压缩、token dropping、自定义序列化。

其中「no fate-sharing」与「non-prefix reuse」是两个最容易被新手忽略、但最能体现 LMCache 设计野心的点：

- **no fate-sharing** 意味着 LMCache 倾向于把缓存管理**独立成进程**（最新的多进程架构更是如此），这正是 u3 单元的主题。
- **non-prefix reuse** 意味着 LMCache 不止是「前缀缓存」，它用 CacheBlend 把复用范围扩展到任意位置——这对 RAG 这类「文档插在 prompt 中间」的场景至关重要。

#### 4.4.4 代码实践

1. **实践目标**：把 LMCache 装上，并对照 README 确认它的入口形态。
2. **操作步骤**：
   - 按 README 的 Getting Started 用 pip 安装：

     [README.md:86-89](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/README.md#L86-L89) —— 安装命令 `pip install lmcache` 与官方文档链接（Installation / Quickstart / Recipes / CLI / Benchmarking / Production Deployment）。
   - 安装后运行 `lmcache --help`，浏览它的子命令（具体子命令框架在 u1-l4 / u4-l1 详讲）。
   - 回到本节表格，给 README 列出的每条特性在 `examples/` 目录里找**一个**对应的示例目录（例如 observability → `examples/observability`，PD 分离 → `examples/disagg_prefill` 或 `examples/disagg_prefill_mp`）。
3. **需要观察的现象**：`lmcache --help` 能列出若干子命令，说明它已作为命令行工具安装成功。
4. **预期结果**：你能把「七条特性」与「至少三四个 examples 子目录」粗略对应起来。
5. 若主机无 GPU，安装可能受限（可参考后续 u1-l2 的 slim 安装）；`lmcache --help` 在纯 CPU / CLI-only 下一般仍可用，具体「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：「Engine-independent deployment」为什么能避免「引擎崩溃丢缓存」？

**参考答案**：因为 KV cache 由独立的 LMCache daemon 进程管理，与推理引擎进程是分离的（no fate-sharing）。引擎进程崩溃时，daemon 里已持久化的 KV cache 不受影响，引擎重启后仍可继续复用。

**练习 2**：举一个「纯前缀缓存会失效、但 CacheBlend 仍能部分命中」的例子。

**参考答案**：RAG 场景里，检索到的文档往往拼接在 prompt **中间**（系统提示 + 文档 + 用户问题）。当换了一份文档时，用户问题那段不再是「前缀」，纯前缀缓存命中不到；但 CacheBlend 可以复用那些仍匹配的 KV 块，只选择性重算变化的部分。

---

### 4.5 设计文档体系：docs/design 镜像约定

#### 4.5.1 概念说明

LMCache 是一个体量很大、模块众多的项目（既有 legacy 的 `storage_backend/`，也有新的 `v1/` 架构）。光看代码容易迷失，因此项目维护了一套**设计文档体系**，位于 `docs/design/`。

它最重要的规则是：**`docs/design/` 的目录结构镜像（mirror）`lmcache/` 包树**。也就是说，源码在 `lmcache/<路径>/`，对应的设计文档就在 `docs/design/<路径>/`。这条约定让你能像「查字典」一样定位任意模块的设计说明。

> 提示：并非每个模块都有设计文档——只有那些「设计值得用文字阐述」的模块才有。某个目录在 `docs/design/` 下不存在，只意味着「还没有独立的设计文档」，不代表该模块不重要。

#### 4.5.2 核心流程

当你想了解某个模块的设计时，按下面三步走：

```text
1. 拿到模块的源码路径，例如 lmcache/v1/distributed/l2_adapters/
                        └────────── 相对路径 ──────────┘
        │
        ▼
2. 在 docs/design/ 下拼接同样的相对路径 → docs/design/v1/distributed/l2_adapters/
        │
        ▼
3. 阅读该目录下的 .md 文件，获取设计契约、动机、扩展指南
```

#### 4.5.3 源码精读

这条镜像约定写在 `docs/design/README.md` 的开头，是阅读整个设计文档体系的「钥匙」：

- [docs/design/README.md:3](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/README.md#L3-L3) —— 说明这个目录存放 LMCache 各模块的设计文档。
- [docs/design/README.md:7](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/README.md#L7-L7) —— 核心约定：「**`docs/design/` mirrors the `lmcache/` package tree.**」

紧接着的表格给出了几个具体对照例子，用来说明如何把源码路径翻译成设计文档路径：

[docs/design/README.md:13-20](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/README.md#L13-L20) —— 对照表，例如 `lmcache/cli/` → `docs/design/cli/`、`lmcache/v1/distributed/l2_adapters/` → `docs/design/v1/distributed/l2_adapters/`。

另外，本项目的 `CLAUDE.md`（仓库根的开发指引）也明确建议：调研某个模块时，**先看 `docs/design/<对应路径>/`** 拿到设计契约与扩展指南。这说明设计文档是项目里一等公民，值得从一开始就养成「读代码前先读对应 design doc」的习惯。

#### 4.5.4 代码实践

1. **实践目标**：掌握「源码路径 → 设计文档路径」的镜像查找法。
2. **操作步骤**：
   - 在仓库里列出 `docs/design/` 的一级子目录，你会看到 `cli / integration / observability / sdk / v1` 等，它们正好对应 `lmcache/` 下的同名模块。
   - 任选一个本讲后续会涉及的模块，例如 `lmcache/v1/distributed/l2_adapters/`，去 `docs/design/v1/distributed/l2_adapters/` 下阅读 `overall.md`（该目录确实存在）。
   - 再挑一个：`lmcache/v1/mp_coordinator/` → `docs/design/v1/mp_coordinator/README.md`。
3. **需要观察的现象**：设计文档目录结构与源码目录结构高度一致（镜像关系成立）。
4. **预期结果**：对任意给定的 `lmcache/<路径>`，你能立刻说出对应设计文档应该在 `docs/design/<路径>` 下。
5. 本实践为「目录浏览 + 文档阅读」型，不涉及运行。

#### 4.5.5 小练习与答案

**练习 1**：源码 `lmcache/v1/multiprocess/` 对应的设计文档应在哪个目录？

**参考答案**：按镜像约定，应在 `docs/design/v1/multiprocess/`（仓库里该目录确实存在，例如里面有 `mp_runtime_plugin.md`）。

**练习 2**：如果某个源码模块在 `docs/design/` 下找不到对应文档，能说明什么？

**参考答案**：只说明「该模块暂时没有独立的设计文档」，并不说明它不重要或不存在。代码本身、docstring、commit 历史仍是理解它的依据。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个贯穿性小任务。这是本讲的核心实践。

### 任务：画出 LMCache 的数据流草图，并匹配一个真实业务场景

**实践目标**：用自己的话把「请求 → prefill → KV cache → LMCache 存储 → 复用」这条主线讲清楚，并判断哪类业务最适合用 LMCache 加速。

**操作步骤**：

1. **画数据流草图**。在纸或文本编辑器里，画出包含以下要素的流程图（可参考 4.3.2 节，但请用你自己的措辞）：
   - 请求 1 如何产生 KV cache；
   - LMCache 用什么动作把 KV 存进分层存储（写出 store 这个动词）；
   - 请求 2（与前缀有重叠）如何通过 lookup 发现命中、再用 retrieve 取回，从而跳过 prefill。
2. **标注分层存储**。在草图里画出 KV cache 可能经过的存储层级（GPU → CPU 内存 → 本地磁盘 → 远端），并用箭头表示 offload 方向。
3. **匹配业务场景**。从下面任选一个真实场景，说明它为什么适合用 LMCache（重点说清「可复用的前缀 / 文档」是什么）：
   - **多轮对话**：同一会话里历史消息作为前缀复用；
   - **RAG（检索增强）**：系统提示 + 检索文档作为可复用片段（注意结合 4.4 的 CacheBlend，思考文档在中间时的情况）；
   - **Agent / 长上下文**：固定的工具说明、长系统提示在每次调用中重复。
4. **回查文档**。把你画的草图里的关键词（offload、reuse、TTFT、tiered）逐一在 README 里找到出处（对照 4.3.3 / 4.4.3 的行号），确认你的理解与官方表述一致。

**需要观察的现象**：画完之后，你应该能一眼看出「哪里省了 prefill、哪里因此降低了 TTFT」。

**预期结果**：产出一份（1）数据流草图 +（2）一段 3–5 句的场景说明，说明该场景里「被复用的前缀/文档」具体是什么、复用带来了 TTFT/吞吐的什么改善。

**关于运行**：本综合实践以「阅读 README + 画图 + 推理」为主，不需要启动服务；如想在机器上亲眼看「第二次命中」的效果，可结合 `examples/kv_cache_reuse` 尝试，运行结果「待本地验证」。

## 6. 本讲小结

- **KV cache** 是 attention 机制为历史 token 缓存的 Key/Value 向量，它让 decode 高效，但显存消耗随上下文线性增长（一条 8K 请求的 KV 可达约 1 GiB）。
- **LMCache 是「KV cache 管理层」**，不是推理引擎；它位于推理引擎与存储之间，是 **vendor-neutral** 的中间层。
- LMCache 的核心价值是：把「算完即扔」的 KV cache 变成**持久、可复用**的资产，从而**降低 TTFT、提升吞吐**，并支持**跨请求 / 跨会话 / 跨引擎实例**复用。
- 主线数据流是 **store（存）→ lookup（查命中）→ retrieve（取回）**，对应后续 `LMCacheEngine` 的三大 API。
- 七大关键特性：引擎独立部署、分层 offload、可观测性、可插拔后端、非前缀复用（CacheBlend）、PD 分离与 KV 传输、可插拔 KV 变换（SERDE）。
- 设计文档位于 `docs/design/`，**目录结构与 `lmcache/` 包树镜像**——读代码前先读对应 design doc 是项目推荐的习惯。

## 7. 下一步学习建议

本讲只建立了「LMCache 是什么」的整体认知，还没有进入任何代码。建议接下来按顺序学习：

1. **u1-l2 安装、构建与运行方式**：亲手把 LMCache 装上，弄清 `lmcache` / `lmcache_server` / `lmcache_controller` 三个命令行入口。
2. **u1-l3 代码目录结构与组织**：对照本讲的「镜像约定」，正式走进 `lmcache/` 包，区分 `v1/` 新架构与 legacy `storage_backend/`。
3. **u1-l4 进程入口与启动方式**：理解 LMCache 作为独立 daemon 是怎么启动的（呼应本讲的「no fate-sharing」）。
4. 之后再进入 **u1-l5 配置系统** 与 **u1-l6 LMCacheEngine 公共 API**，亲手看到 `store / retrieve / lookup` 的真面目。

如果在本讲里你对某个特性（例如 CacheBlend 或 PD 分离）特别好奇，也可以先跳到对应的设计文档（`docs/design/v1/` 下）浏览动机，再按学习路线回头补基础。
