# 受限解码与结构化输出资料

## 1. 本讲目标

本讲是「按主题绘制资料地图」单元的第三篇，聚焦一个横跨多份资料的核心优化主题：**如何让大模型又快又稳地生成合法的 JSON（或其它结构化文本）**。

读完本讲，你应当能够：

1. 说清「受限解码（constrained decoding）」要解决什么问题，以及它为什么天生就比自由解码慢。
2. 理解 SGLang 用「压缩有限状态机（Compressed FSM）+ 跳跃式解码（jump-forward）」把每一步只能解一个 token 的瓶颈打破的关键思路。
3. 认识 **XGrammar** 这个后来成为主流推理引擎默认结构化生成后端的引擎，以及它和压缩 FSM 的承接关系。
4. 在仓库里精准定位「受限解码 / JSON 加速」相关的幻灯片、博客与 meetup 回顾，组成一个完整的「资料簇」。

> 本讲定位回顾：本仓库 `sgl-learning-materials` 是 SGLang 官方学习资料聚合库，**不含运行时代码**（见 [u1-l1](u1-l1-project-overview.md)）。因此本讲的「源码精读」对象是仓库内的 README 导航文字、meetup 回顾博客，以及 README 指向的 LMSYS 原始博客；幻灯片 PDF 作为配套讲义引用。涉及算法原理的部分，我们以原始博客的描述为准，避免对幻灯片逐页内容做无法核实的臆测。

## 2. 前置知识

在进入源码与资料之前，先用大白话把三个基础概念讲清楚。

**① 什么是「结构化输出」**

当你让大模型「输出一个 JSON」时，模型其实是在一个一个 token（词片）地吐字。它完全可能吐出格式错误的 JSON，比如多一个逗号、少一个引号、把数字写成字符串。很多下游程序（解析器、数据库、API）对格式零容忍，一个不合法的 JSON 就能让整条流水线崩溃。

「结构化输出」就是要求模型生成的文本**始终**满足某个预先定义好的 schema（模式），例如「必须是一个对象，里面有 `name` 字符串和 `age` 整数」。OpenAI 的 JSON mode 就是最常见的产品化例子。

**② 什么是「受限解码」**

模型每一步会输出一个**概率分布**（logits），告诉你下一个 token 可能是什么、各自概率多少。正常情况下我们按概率采样一个 token。

「受限解码」的做法是：在采样之前，**用一个掩码（mask）把所有不合法的 token 概率清零**，让模型只能从「合法的下一步」里挑。比如已经生成了 `{"name":`，下一步按照 JSON 语法只允许出现字符串开头，那么所有非字符串 token 都被屏蔽。

这就是「受限」二字的本意：不是改模型权重，而是在解码时**动态地限制可选 token 集合**。

**③ 为什么受限解码天生就慢**

这是本讲的核心痛点，也是压缩 FSM 要解决的问题。直觉上：受限解码要求「每生成一个 token，就要重新算一次哪些 token 合法、再跑一次模型前向」。换句话说，它被绑死在「**一步一个 token**」的节奏上。而现代推理引擎提速的一大法宝是 **prefill（预填充）/ chunked prefill**——一次性喂进去一长串 token 并行算，远比逐 token 解码快。受限解码享受不到这个红利，所以又慢又贵。

> 一个关键术语：**有限状态机（Finite State Machine, FSM）**。把 JSON schema 先转成正则表达式，再把正则转成一张「状态转移图」——图里每个圆圈是一个状态，每条边标注「读到某个字符就跳到下一个状态」。只要模型每走一步都对照这张图，就能保证输出永远合法。压缩 FSM 就是对这张图做手术。

如果你已经学过 [u1-l3](u1-l3-readme-navigation.md) 的「按主题反查」与「资料簇」概念，本讲就是把这套方法用在「结构化输出」这个主题上。

## 3. 本讲源码地图

本讲涉及的关键文件如下（全部为仓库内真实文件）：

| 文件 | 作用 | 在本讲中的角色 |
| --- | --- | --- |
| [README.md](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md) | 资料导航枢纽 | 定位两份幻灯片、meetup 回顾、压缩 FSM 博客、v0.4 博客的位置 |
| [blogs/Efficient LLM Deployment and Serving.md](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md) | 仓库内唯一一篇长博客（meetup 回顾） | 用文字记录了 XGrammar 的核心卖点（3–5×、token mask cache、30% 端到端提速） |
| [slides/lmsys_1st_meetup_constrained_decoding.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/lmsys_1st_meetup_constrained_decoding.pdf) | 「Faster Constrained Decoding」幻灯片 | 压缩 FSM 思路的会议讲义（PDF，需本地打开阅读） |
| [slides/lmsys_1st_meetup_xgrammar.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/lmsys_1st_meetup_xgrammar.pdf) | 「XGrammar」幻灯片 | XGrammar 引擎的会议讲义（PDF，需本地打开阅读） |

此外，本讲会多次引用 README 指向的**外部**资料（不在本仓库内，但与本主题强相关）：

- LMSYS 博客《Fast JSON Decoding for Local LLMs with Compressed Finite State Machine》（压缩 FSM 的权威原理说明）。
- LMSYS 博客《SGLang v0.4: ... Faster Structured Outputs》（XGrammar 集成后的产品化里程碑）。
- XGrammar 项目主页 `github.com/mlc-ai/xgrammar`（引擎能力与生态集成情况）。

> 这是 u1-l1 强调过的「路标」特征：本仓库是导航，真正知识在链接终点。本讲的深度内容主要落在 README 指向的 LMSYS 博客里。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，对应大纲指定的「受限解码与 Compressed FSM / XGrammar 结构化生成 / JSON 解码加速」。三者是一条递进的故事线：**痛点（受限解码慢）→ SGLang 的解法（压缩 FSM）→ 工程化引擎（XGrammar）→ 落地效果（JSON 加速）**。

### 4.1 受限解码与 Compressed FSM

#### 4.1.1 概念说明

「压缩有限状态机（Compressed Finite State Machine, Compressed FSM）」是 SGLang 在 2024 年初提出的一种加速受限解码的算法，配合的解码动作叫 **jump-forward（向前跳跃）解码**。

它要解决的，正是第 2 节里讲到的痛点：传统基于 FSM 的受限解码**每一步只能推进一个 token**，因为 FSM 是在 token 层面构建的——每个状态只能根据「下一个 token」转移一次，于是整条生成被切成无数个「单 token 解码步」，无法享受 prefill 的并行红利。

压缩 FSM 的核心直觉是：

> 在很多位置，schema 是**完全确定**的。比如刚生成了 `{"name":`，按 JSON 语法，接下来必然是某个字符串——这段路径上没有分支，只有一个走向。既然下一步是确定的，为什么还要一个 token 一个 token 地让模型「猜」？直接把这一整段确定的内容**一次性补进去**（prefill/extend）就好了。

「压缩」指的是：在 FSM 的状态转移图里，把那些**只有一个出口的连续边**（singular transition edges）合并成一条「单值路径（singular path）」。这些路径不需要模型逐步决策，可以直接跳过，直到遇见下一个真正有分支的决策点。

#### 4.1.2 核心流程

压缩 FSM + jump-forward 的工作流程可以概括为下面几步：

```
1. JSON schema  →  正则表达式  →  普通 FSM（状态转移图）
2. 扫描 FSM，识别所有「只有一个出口」的边（singular edges）
3. 把连续的 singular edges 压缩成一条 singular path
4. 进入解码循环：
     a. 若当前位置在一个 singular path 上：
        - 不再逐 token 解码
        - 直接把这条确定路径对应的字符串「补」进序列（extend / prefill）
        - 一直跳到下一个有分支的决策点
     b. 若到达分支点：
        - 像普通受限解码一样，用 mask 选出合法 token，采样一个
5. 利用 RadixAttention + 高效 extend 原语，复用已算过的 KV cache，避免重复计算
```

用一个博客里的例子帮助理解。当生成一个角色信息、模型在 `house` 字段吐出字母 `G` 时，按 schema 几乎可以肯定接下来是 `ryffindor`（拼成 `Gryffindor`）。传统做法要把 `r`、`y`、`f`、`f`、`e`、`n`、`d`、`o`、`r` 每个字符都走一次完整解码步；jump-forward 则直接把 `ryffindor` 整段补进去，一次 extend 搞定。

**为什么能省这么多？** 从「步数」的角度看，普通受限解码的步数等于输出 token 总数 \(N\)。引入 jump-forward 后，那些落在 singular path 上的 token 不再各自占用一个解码步，而是被折叠进少数几次 extend 操作。若用 \(\rho\) 表示输出中「确定路径 token」所占的比例，那么真正需要逐 token 决策的步数大约降为 \((1-\rho)\,N\) 量级，其余部分被廉价的 extend 吸收：

\[
\text{逐步解码步数} \;\approx\; \underbrace{(1-\rho)\,N}_{\text{分支点决策}} \;+\; \underbrace{\#\text{singular paths}}_{\text{extend 操作}}
\]

注意这只是用来建立直觉的近似关系，并非博客给出的精确公式；但可以看出：JSON 里**确定字符越多**（引号、冒号、逗号、花括号、固定键名等），\(\rho\) 越大，jump-forward 的收益越显著。

**一个不能忽略的细节——tokenization 边界。** LLM 的分词器常常把多个字符合并成一个 token（例如把 `"` 和 `,` 合并成 `",`）。这在 jump-forward 时会带来麻烦：被补进去的字符串到底怎么切分 token，会影响后续 token 的概率分布。博客给出的应对是「**重新分词（re-tokenization）**」——补进字符串而非 token，然后对整段文本重新分词。这带来约 **4%** 的额外开销，但换来了正确性。

#### 4.1.3 源码精读

压缩 FSM 的**原理**权威说明不在本仓库内，而在 README 指向的 LMSYS 博客里。我们先看仓库内能定位到它的两条线索。

**线索一：README 在 Slides 区段登记了对应幻灯片。**

[README.md:L74-L78](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L74-L78) 把这份幻灯片归在「The first LMSYS online meetup」事件下，标题为 **Faster Constrained Decoding**，日期 2024-10-16：

> `[2024-10-16] [Faster Constrained Decoding](slides/lmsys_1st_meetup_constrained_decoding.pdf)`

这一行的作用是**登记 + 定位**：它告诉我们这份 PDF 的文件名、所属活动、日期。文件本身是二进制 PDF，需在本地打开阅读其逐页内容（待本地验证）。

**线索二：README 在 Blog 区段登记了原理博客。**

[README.md:L124-L124](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L124-L124) 指向 LMSYS 博客：

> `[2024-02-05] [Fast JSON Decoding for Local LLMs with Compressed Finite State Machine](https://lmsys.org/blog/2024-02-05-compressed-fsm/)`

注意日期：博客发表于 **2024-02-05**，幻灯片讲于 **2024-10-16**。也就是说，幻灯片是对八个月前那篇博客工作的**会议回顾讲解**。这一点很重要——它解释了为什么我们读原理要去博客，而不是期望幻灯片 PDF 里有完整文字推导。

**仓库内的直接文字佐证。** meetup 回顾博客里，介绍 SGLang 时明确把它列为运行时的一项核心能力：

[blogs/Efficient LLM Deployment and Serving.md:L13-L14](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L13-L14) 写道，SGLang 的 runtime 通过「RadixAttention 用于 KV cache 复用」与「**compressed finite state machines for faster decoding**（压缩有限状态机以加速解码）」来提速。这一句是仓库内对「压缩 FSM」概念最直接的文字记录，也把压缩 FSM 和 RadixAttention 绑在了一起——正是 4.1.2 流程里第 5 步「复用 KV cache」的来源。

> 阅读提示：本仓库不含算法实现代码。如果你想看压缩 FSM 的**代码实现**，需要跳到主仓库 `sgl-project/sglang`（博客末尾也提供了 benchmark 代码路径 `benchmark/json_jump_forward`）。本讲只负责把「资料在哪里、讲了什么」讲清楚。

#### 4.1.4 代码实践

**实践类型：源码阅读型实践（资料梳理）。**

1. **实践目标**：把压缩 FSM 博客里的「两种旧方法 → SGLang 新方法」梳理成一张对照表，建立对「为什么慢、怎么变快」的完整理解。
2. **操作步骤**：
   - 打开 README 第 124 行登记的博客《Fast JSON Decoding for Local LLMs with Compressed Finite State Machine》。
   - 在博客里定位三个小节：Method 1（FSM-Based）、Method 2（Interleaved-Based）、Our Method（Jump-Forward with Compressed FSM）。
   - 用下表模板填写（博客原文已给出每种方法的原理与 limitations）：

   | 方法 | 核心做法 | 主要局限 |
   | --- | --- | --- |
   | Method 1：FSM + Logits Mask | （待你填写） | （待你填写） |
   | Method 2：Interleaved（guidance + llama.cpp） | （待你填写） | （待你填写） |
   | SGLang：Compressed FSM + Jump-Forward | （待你填写） | 克服了上述局限 |

3. **需要观察的现象**：注意 SGLang 方法是如何同时拿到「Method 1 的通用性（任意正则）」和「Method 2 的速度（一次处理多 token）」两者的优点。
4. **预期结果**：你能用一句话讲清 jump-forward 的本质——「在确定路径上不逐 token 解码，而是直接 prefill 整段，跳到下一个分支点」。
5. **若无法联网阅读博客**：标注「待本地验证」，仅依据本讲 4.1.1–4.1.2 的描述完成对照表的「SGLang 方法」一列。

#### 4.1.5 小练习与答案

**练习 1**：为什么说传统 FSM 受限解码「享受不到 prefill 的红利」？

> **参考答案**：因为 FSM 是在 token 层面构建的，每个状态只能根据「下一个 token」转移一次，所以每生成一个 token 就必须重新跑一次模型前向并重新计算 mask。整条生成被切成 N 个「单 token 解码步」，无法像 prefill 那样把一长串 token 一次性并行算完。

**练习 2**：jump-forward 在「补进确定字符串」时，为什么要重新分词（re-tokenization）而不是直接补 token？

> **参考答案**：因为分词器可能把多个字符合并成一个 token（如 `",`），直接补 token 会与「整段重新分词」的结果不一致，进而改变后续 token 的概率分布，产生意外行为。重新分词以约 4% 的开销换取正确性。

**练习 3**：在仓库里，你能用哪一行 README 同时定位到「压缩 FSM 的原理博客」和「Faster Constrained Decoding 幻灯片」？它们分别在 README 的哪个区段？

> **参考答案**：原理博客在 `## Blog` 区段的第 124 行；幻灯片在 `## Slides` → `### The first LMSYS online meetup` 子区段的第 78 行。两者日期不同（2024-02-05 vs 2024-10-16），说明幻灯片是对早期博客工作的回顾讲解。

---

### 4.2 XGrammar 结构化生成

#### 4.2.1 概念说明

如果说压缩 FSM 是 SGLang 在 2024 年初的「自研解法」，那么 **XGrammar** 就是这条路线后来演化出的、更具通用性的**独立结构化生成引擎**。它的全称出现在幻灯片标题里：「XGrammar: Flexible And Efficient Structured Generation Engine for Large Language Models」。

XGrammar 的定位可以这样概括（基于其项目主页与 meetup 回顾博客）：

- 它是一个**独立开源库**（项目地址 `github.com/mlc-ai/xgrammar`），不绑定单一推理引擎。
- 它用受限解码保证输出 **100% 结构正确**（即生成的 JSON 一定合法）。
- 它支持**通用上下文无关文法（CFG）**，因此不止 JSON，还能处理正则、自定义文法等更广的结构。
- 通过精心优化，它在 JSON 生成上达到了**近零开销（near-zero overhead）**。
- 它强调**通用部署**：跨平台（Linux/macOS/Windows）、跨硬件（CPU / NVIDIA GPU / AMD GPU / Apple Silicon / TPU）、多语言绑定（Python/C++/JavaScript/Swift）。

正因为又快又通用，XGrammar 后来成了 vLLM、SGLang、TensorRT-LLM、MLC-LLM 等主流推理引擎的**默认结构化生成后端**。

**和压缩 FSM 的关系（重要）。** 两者是承接关系，不是并列关系：压缩 FSM 解决了「确定路径可以 prefill」这一关键直觉；XGrammar 把这类思路工程化成一个**可被任意引擎调用的、跨硬件的、支持通用文法**的引擎。在本讲的资料簇里，XGrammar 幻灯片（2024-10-16）排在压缩 FSM 博客（2024-02-05）之后，正反映了这条演进线。

#### 4.2.2 核心流程

从「用户视角」看，XGrammar 的工作流程是：

```
1. 用户给定一个结构规范
     - JSON schema，或正则，或自定义 CFG
2. XGrammar 把规范编译成内部文法表示（含 FSM / 掩码计算所需信息）
3. 推理引擎每一步前向后，把 logits 交给 XGrammar
4. XGrammar 根据当前文法状态，计算出「合法 token 掩码」
     - 非法 token 概率被清零
5. 引擎在掩码后的分布上采样，得到下一个 token
6. 循环 3-5，直到生成结束 → 输出 100% 合规的结构化文本
```

XGrammar 的工程亮点（来自 meetup 回顾博客与项目主页）集中在「**如何让第 4 步极快**」上：

- **Token mask cache（token 掩码缓存）**：预先计算并缓存每个 token 在不同文法状态下的合法性，避免每步重算。
- **CPU 开销管理**：把文法相关的计算合理地放在 CPU 上与 GPU 前向重叠，隐藏开销。
- **跨硬件可移植**：同一套文法逻辑能在 NVIDIA/AMD/Apple Silicon 等多种硬件上跑。

这些手段合起来，让结构化生成的额外开销小到「几乎可以忽略」，这也是它被广泛集成的根本原因。

#### 4.2.3 源码精读

**幻灯片登记行。**

[README.md:L84-L84](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L84-L84) 在同一个 meetup 事件子区段登记了 XGrammar 幻灯片：

> `[2024-10-16] [XGrammar: Flexible And Efficient Structured Generation Engine for Large Language Models](slides/lmsys_1st_meetup_xgrammar.pdf)`

注意它与 4.1.3 的受限解码幻灯片**同属一个事件**（`### The first LMSYS online meetup`，README 第 74 行起），日期都是 2024-10-16。这正是 u1-l3 所讲的「资料簇」：同一个活动里，受限解码和 XGrammar 被放在一起讲，说明它们本就是同一条技术线。

**meetup 回顾博客里的文字佐证。**

仓库内的 meetup 回顾博客专门有一节讲 XGrammar，并给出了量化指标。这是仓库内对 XGrammar 卖点最集中的文字记录：

[blogs/Efficient LLM Deployment and Serving.md:L38-L48](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L38-L48) 记录了三条要点（原文用「Jason」是「JSON」的笔误）：

- **解码速度**：相比已有后端快 **3 到 5 倍**，靠的是 CPU 开销管理与 **token mask cache**。
- **端到端速度**：即使在常量字符串很少的情况下，端到端也有约 **30%** 的提升。
- **文法引导生成（Grammar-Guided Generation）**：达到当时最好的效率。

博客还在 [blogs/Efficient LLM Deployment and Serving.md:L44-L44](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L44-L44) 处配了一张 `1016 meetup - Xgrammer benchmark.png` 的基准图（位于 `blogs/docs/figs/`），用以展示这组数字。

**产品化里程碑。**

XGrammar 从「会议讲义」变成「默认后端」的时间线，可以从 README 的 Blog 区段读到：

[README.md:L118-L118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118-L118) 登记的 v0.4 博客标题里直接出现了 **Faster Structured Outputs**：

> `[2024-12-04] [SGLang v0.4: Zero-Overhead Batch Scheduler, Cache-Aware Load Balancer, Faster Structured Outputs](https://lmsys.org/blog/2024-12-04-sglang-v0-4/)`

结合 XGrammar 项目主页的记录（2024/11 正式集成进 SGLang 与 MLC-LLM，2024/12 集成进 vLLM），可以把演进串成一条线：

```
2024-02  压缩 FSM 博客（SGLang 自研解法）
   ↓
2024-10  meetup：Faster Constrained Decoding + XGrammar 两份幻灯片
   ↓
2024-11  XGrammar 正式集成进 SGLang、MLC-LLM
   ↓
2024-12  v0.4 发布，结构化输出（Faster Structured Outputs）成为主打卖点之一
```

#### 4.2.4 代码实践

**实践类型：资料对照型实践。**

1. **实践目标**：把 XGrammar 的「卖点数字」与「它在资料里的出处」一一对应，练就「每个结论都能追溯到具体文件」的习惯。
2. **操作步骤**：
   - 打开仓库内博客 [blogs/Efficient LLM Deployment and Serving.md](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md)，定位第 38–48 行的 XGrammar 小节。
   - 填写下面的「数字 → 出处」表：

   | 卖点数字 | 含义 | 出处（文件 + 行号 / 配图） |
   | --- | --- | --- |
   | 3–5× | 解码速度相对已有后端 | 博客第 42 行，配图 `1016 meetup - Xgrammer benchmark.png`（第 44 行） |
   | 30% | 端到端提速 | （待你填写） |
   | Grammar-Guided | 文法引导生成达 SOTA | （待你填写） |

3. **需要观察的现象**：注意这些数字都附在文字段落后紧跟的配图上——这是 meetup 回顾博客的写作惯例（一段说明 + 一张图）。
4. **预期结果**：你能指出每个量化结论分别对应博客的哪一行、哪张配图，而不是笼统地说「博客里提到 XGrammar 很快」。
5. **延伸（可选，待本地验证）**：访问 XGrammar 项目主页，记录它当前是哪些推理引擎的默认结构化生成后端，并注明这些集成时间都不在本仓库内（属外部资料）。

#### 4.2.5 小练习与答案

**练习 1**：XGrammar 和压缩 FSM 是什么关系？

> **参考答案**：承接关系。压缩 FSM（2024-02）给出了「确定路径可直接 prefill」这一关键直觉；XGrammar（2024-10 meetup 讲解，2024-11 集成进 SGLang）把这类思路工程化为一个独立、跨硬件、支持通用文法、近零开销的结构化生成引擎，并被主流推理引擎采纳为默认后端。

**练习 2**：仓库内 meetup 回顾博客里，XGrammar 的「3–5×」提速主要归因于哪两个工程手段？

> **参考答案**：CPU 开销管理（把文法计算与 GPU 前向重叠以隐藏开销）与 token mask cache（预先缓存各 token 在不同文法状态下的合法性，避免每步重算）。

**练习 3**：为什么说 XGrammar 的「通用部署」能力对它的广泛集成很重要？

> **参考答案**：因为它跨平台（Linux/macOS/Windows）、跨硬件（CPU/NVIDIA/AMD/Apple Silicon/TPU）、多语言绑定（Python/C++/JS/Swift），任何推理引擎都能较容易地接入它，不必为每种硬件重写结构化生成逻辑，因而能成为 vLLM、SGLang、TensorRT-LLM、MLC-LLM 等的默认后端。

---

### 4.3 JSON 解码加速

#### 4.3.1 概念说明

第三个最小模块把前两个模块的效果「落」到一个具体场景上：**让本地大模型生成 JSON 更快**。这是受限解码技术最具代表性的用例，也是 README 里出现「Fast JSON Decoding」字样的原因。

需要强调的是：JSON 解码加速不是一项独立的新技术，而是**压缩 FSM + XGrammar 这一整条技术线在 JSON 场景下的综合收益**。它的价值在于：

1. **保证合法**：模型生成的 JSON 一定符合给定 schema，下游可以直接解析，不用写容错逻辑。
2. **更快**：通过 jump-forward / 掩码缓存等手段，把受限解码的开销压到极低，甚至**比不受限的普通解码还快**（博客原文的原话）。
3. **更省**：在服务场景下，更低的延迟与更高的吞吐直接转化为更低的单 token 成本。

「比普通解码还快」这一点乍听反直觉——加了限制反而更快？原因是：jump-forward 把大量「确定字符」折叠进了廉价的 prefill 操作，反而减少了昂贵的逐 token 解码步。当 JSON 里确定字符占比很高（键名、标点、固定结构）时，这个收益尤为明显。

#### 4.3.2 核心流程

把 JSON 解码加速拆成「能省多少、靠什么省」，可以用下面这张因果图表示：

```
JSON 输出里大量字符是「确定的」
        │
        ├── 键名、引号、冒号、逗号、花括号 ……
        │   （schema 决定，无需模型决策）
        │
        ▼
压缩 FSM 把这些「确定路径」识别出来
        │
        ▼
jump-forward：直接 prefill 整段确定路径，跳到下一个分支点
        │
        ├── 只有「真正需要模型决策」的位置才逐 token 解码
        │
        ▼
RadixAttention 复用 KV cache + 重新分词保证正确（~4% 开销）
        │
        ▼
XGrammar 把整套逻辑工程化：token mask cache + CPU/GPU 重叠
        │
        ▼
结果：延迟降最多 ~2×、吞吐升最多 ~2.5×，甚至快过普通解码
```

注意数字的出处区分：

- 「最多 ~2× 延迟降低 / ~2.5× 吞吐提升」出自**压缩 FSM 博客**（4.1 节），测试在 llama-7B + NVIDIA A10 GPU 上。
- 「3–5× 解码提速 / 30% 端到端提速」出自**仓库内 meetup 回顾博客**（4.2 节），描述的是 XGrammar 相对已有后端的表现。

两组数字口径不同（前者是与 outlines+vLLM、guidance+llama.cpp 比的受限解码延迟/吞吐；后者是 XGrammar 解码后端的相对提速），不能直接混为一谈——这也是读资料时要留意的细节。

#### 4.3.3 源码精读

**「Fast JSON Decoding」在 README 里的两处出现。**

第一处是原理博客，已在 4.1.3 引用过：

[README.md:L124-L124](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L124-L124) ——标题里就有 **Fast JSON Decoding ... Compressed Finite State Machine**，是 JSON 加速原理的权威出处。

第二处是产品化里程碑：

[README.md:L118-L118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118-L118) ——v0.4 博客标题里的 **Faster Structured Outputs**，是 XGrammar 集成后 JSON/结构化输出的产品级提速。

把这两行放在一起读，就能看到「JSON 解码加速」在本仓库资料里的完整脉络：**从 2024-02 的原理博客，到 2024-12 的产品化卖点**。

**配套视频（可选）。** 本主题在 README 的 Videos 区段也有配套录像：

[README.md:L156-L158](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L156-L158) 登记了「The first LMSYS online meetup」的整场 YouTube 录像（2024-10-16）。受限解码与 XGrammar 的两份幻灯片正是这场 meetup 的两个议题，因此看这场录像可以听到作者本人的口头讲解——这正是 [u2-l2](u2-l2-scheduler-performance.md) 提到的阅读技巧：**先听讲解，再看 PDF**。

#### 4.3.4 代码实践（本讲的主实践任务）

**实践类型：原理消化型实践（写作输出）+ 可选的运行验证。**

这是大纲指定的本讲实践任务：**结合幻灯片与 README 指向的「Fast JSON Decoding ... Compressed Finite State Machine」博客，写一段说明：为什么压缩 FSM 能加速本地 LLM 的 JSON 解码。**

1. **实践目标**：用自己的话把「压缩 FSM 加速 JSON 解码」的因果关系讲清楚，做到不堆砌术语、能让一个没读过博客的人看懂。
2. **操作步骤**：
   - 第一步（资料）：打开 README 第 124 行的压缩 FSM 博客，重点读「Our Method: Jump-Forward Decoding With a Compressed Finite State Machine」与「Benchmark Results」两节。
   - 第二步（幻灯片，可选）：本地打开 [slides/lmsys_1st_meetup_constrained_decoding.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/lmsys_1st_meetup_constrained_decoding.pdf)，对照博客看作者在幻灯片里如何图示化这套流程（待本地验证）。
   - 第三步（写作）：写一段 200–300 字的说明，至少覆盖以下四个要点。
3. **你的说明里应当包含的要点**：
   - **痛点**：传统 FSM 受限解码每步只能解一个 token，慢。
   - **观察**：JSON 里大量字符（引号、冒号、键名等）是 schema 决定的、确定的。
   - **做法**：压缩 FSM 把这些「确定路径」合并，用 jump-forward 直接 prefill 整段，跳到下一个分支点。
   - **配套**：RadixAttention 复用 KV cache、重新分词保证正确（约 4% 开销）；效果是延迟最多降 ~2×、吞吐最多升 ~2.5×，甚至快过普通解码。
4. **需要观察的现象**：写完后自查——你是否混淆了「压缩 FSM 博客的 2×/2.5×」与「XGrammar 的 3–5×/30%」这两组口径不同的数字？如果混了，改正。
5. **预期结果**：得到一段逻辑自洽、数字口径正确、可追溯回具体博客的说明文字。
6. **可选的运行验证（待本地验证，需另行准备环境）**：
   - 压缩 FSM 博客末尾给出了 SGLang 的 JSON 解码入口与 benchmark 代码路径 `benchmark/json_jump_forward`（位于主仓库 `sgl-project/sglang`，不在本资料库内）。
   - 若你已在本地部署 SGLang，可尝试对一个固定 JSON schema 分别开启/关闭结构化输出，对比生成耗时；若没有环境，此项标注「待本地验证」，不编造结果。
   - 这一步**不在本仓库内完成**，仅作为延伸——本仓库不含运行时代码，无法直接运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么「加了限制的受限解码」反而可能比「不受限的普通解码」还快？

> **参考答案**：因为 jump-forward 把 JSON 中大量由 schema 决定的「确定字符」折叠进了廉价的 prefill/extend 操作，减少了昂贵的逐 token 解码步。当确定字符占比高时，省下的解码步代价超过了加掩码的代价，于是整体更快。

**练习 2**：本仓库里有两组关于「结构化输出提速」的数字（2×/2.5× 与 3–5×/30%），它们分别出自哪里？口径有何不同？

> **参考答案**：2× 延迟降低 / 2.5× 吞吐提升出自压缩 FSM 博客（README 第 124 行），是与 outlines+vLLM、guidance+llama.cpp 等受限解码系统比的延迟与吞吐；3–5× / 30% 出自仓库内 meetup 回顾博客（第 42、46 行），描述的是 XGrammar 解码后端相对已有后端的提速。两者比较对象不同，不能直接混用。

**练习 3**：如果你只想看一场视频快速了解本讲的两个议题（受限解码 + XGrammar），应该看 README 的哪一行？

> **参考答案**：看 README 第 158 行登记的「The First SGLang Online Meetup」YouTube 录像（2024-10-16）。两份幻灯片都是这场 meetup 的议题，录像里有作者口头讲解。

## 5. 综合实践

把本讲三个最小模块串起来，完成一个「**结构化输出资料簇全景图**」任务。

**任务背景**：假设你要给团队新人做一次 15 分钟的分享，主题是「SGLang 怎么把 JSON 生成做快」。你需要从本仓库里挑出最相关的资料，并按一条清晰的故事线组织它们。

**要求产出一份 Markdown 文档，包含以下三部分**：

1. **资料清单表**：列出你会用到的全部仓库内资料（至少包括两份幻灯片、meetup 回顾博客、以及 README 里登记的两个博客外链），每条标注「文件名/链接 + README 行号 + 一句话作用」。

   参考起手式（请补全）：

   | 资料 | README 位置 | 在分享中的作用 |
   | --- | --- | --- |
   | `slides/lmsys_1st_meetup_constrained_decoding.pdf` | 第 78 行 | 讲「压缩 FSM」思路 |
   | `slides/lmsys_1st_meetup_xgrammar.pdf` | （待补全） | （待补全） |
   | `blogs/Efficient LLM Deployment and Serving.md` | （待补全） | （待补全） |
   | 压缩 FSM 博客（外链） | 第 124 行 | 讲原理与 2×/2.5× 数字 |
   | v0.4 博客（外链） | （待补全） | （待补全） |

2. **故事线大纲**：按「痛点 → 原理（压缩 FSM）→ 工程化（XGrammar）→ 落地（JSON 加速 + 数字）」四段，每段写 2–3 句串场词，并标注每段引用哪份资料。

3. **口径提醒**：用一句话提醒听众，分享里出现的两组提速数字（2×/2.5× 与 3–5×/30%）口径不同、不要混用。

**验收标准**：

- 清单里每条资料都能在仓库里找到（文件存在或 README 有登记行）。
- 故事线四段都引用了具体资料，没有空泛的「SGLang 很强」式总结。
- 口径提醒这一项不能漏。

> 这个任务综合考查了 u1-l3 的「按主题反查」、本讲的「资料簇」组织能力，以及把技术原理讲清楚的表达能力。完成后，你就拥有了一份可以直接拿去分享的素材包。

## 6. 本讲小结

- **受限解码**通过在每步用掩码屏蔽非法 token，保证输出永远符合 schema，但传统 FSM 做法被「一步一 token」绑死，享受不到 prefill 的加速红利。
- **压缩 FSM + jump-forward** 是 SGLang 的破局点：把 FSM 里只有一个出口的连续边压缩成「确定路径」，直接 prefill 整段、跳到下一个分支点，从而一次处理多个 token。
- 这套机制依赖 **RadixAttention 复用 KV cache**，并用**重新分词（约 4% 开销）**处理 tokenization 边界问题；效果是延迟最多降 ~2×、吞吐最多升 ~2.5×，甚至快过普通解码。
- **XGrammar** 是这条路线工程化后的独立引擎，支持通用文法、跨硬件、近零开销，已成为 vLLM/SGLang/TensorRT-LLM/MLC-LLM 的默认结构化生成后端。
- 在本仓库里，这一主题构成一个**资料簇**：两份 meetup 幻灯片（README 第 78、84 行）+ meetup 回顾博客（仓库内）+ 两篇 LMSYS 博客外链（README 第 118、124 行）+ 一场 YouTube 录像（README 第 158 行）。
- 读资料时要**区分数字口径**：压缩 FSM 博客的 2×/2.5× 与 XGrammar 的 3–5×/30% 比较对象不同，不可混用。

## 7. 下一步学习建议

1. **横向扩展主题地图**：本讲完成了「结构化输出」这一簇资料的梳理。下一讲 [u2-l4 DeepSeek MLA 与模型优化资料](u2-l4-deepseek-mla.md) 会换一个主题（注意力机制优化），建议用本讲学到的方法——「先在 README 里圈出同主题的多条记录，组成资料簇，再按时间线串起原理博客 → 幻灯片 → 产品化里程碑」——去梳理 MLA 资料。
2. **补全压缩 FSM 的代码视角**：本仓库不含运行时代码。若你想看 jump-forward 的**实现**，请跳到主仓库 `sgl-project/sglang`，并参考压缩 FSM 博客末尾给出的 benchmark 路径 `benchmark/json_jump_forward`。这是「资料 → 文档 → 代码」递进链路的最后一环。
3. **对比阅读 XGrammar 项目**：访问 XGrammar 项目主页（`github.com/mlc-ai/xgrammar`），把它「支持的结构类型、跨硬件能力、被哪些引擎集成」整理成一张表，作为本讲 4.2 节的外部延伸。
4. **回顾本单元方法论**：学完 u2-l1～u2-l3 后，你应该能独立完成「任选一个 SGLang 优化主题 → 在 README 里画出资料簇 → 写出原理与数字口径说明」这个闭环。可以用 [u2-l2](u2-l2-scheduler-performance.md) 的调度主题和本讲的结构化输出主题做对比，体会不同主题资料簇的共性与差异。
