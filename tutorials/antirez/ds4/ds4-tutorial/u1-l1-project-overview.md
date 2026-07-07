# DwarfStar 项目定位与设计哲学

> 本讲是 ds4（DwarfStar）学习手册的第一篇。在进入任何一行 C 代码之前，我们先回答一个最重要的问题：**这个项目为什么存在？它选择做什么、又刻意不做什么？** 理解了定位，后面读源码才不会迷路。

## 1. 本讲目标

学完本讲，你应该能够：

- 用一句话向别人解释 **DwarfStar（ds4）是什么**：它是一个为 DeepSeek V4 Flash / PRO 量身打造的、自包含的原生推理引擎，而不是通用 GGUF 运行器。
- 说清楚 ds4 的三个核心取舍：**窄而精（一次只死磕一个模型）**、**官方向量校验（用官方实现的 logits 做基准）**、**本地高端机器优先（从 96/128GB 内存起步）**。
- 理解贯穿全书的一句口号——**「KV 缓存即一等磁盘公民」（The KV cache is a first-class disk citizen）**——它为什么重要，以及它体现在哪些源码里。
- 知道项目当前的**质量状态（beta，ds4-agent 为 alpha）**，以及出问题时该怎么用 `--trace` 反馈。
- 认识 ds4 与 **llama.cpp / GGML** 的关系：精神与部分量化格式上继承，但**不链接** GGML。

## 2. 前置知识

本讲是纯概念讲，**不要求你懂 C 语言**。下面几个名词用大白话解释一下就够用：

- **大语言模型（LLM）**：一个能根据上文预测下一个 token（可以理解为一个「词片段」）的程序。所谓「推理（inference）」就是反复跑这个预测，把一句话生成出来。
- **权重（weights）/ 模型**：模型里那几十亿、几百亿个浮点数参数。它们才是「模型本体」，体积巨大（几十到上百 GB）。
- **量化（quantization）**：用更少的比特（比如 2 bit）来存权重，牺牲一点点精度换更小的体积、更快的加载。ds4 大量使用 2 bit 量化。
- **KV 缓存（KV cache）**：推理时为了避免重复计算而保存的「中间记忆」。上下文越长，它越大。这是 ds4 最在意的数据结构。
- **GGUF**：一种把模型权重、词表、元数据打包在一起的**文件格式**，由 llama.cpp 社区推广。你可以把它理解成「模型的可执行压缩包」。
- **MoE（混合专家）**：一种模型结构，每一层有多个「专家」子网络，每次只激活其中几个。DeepSeek V4 就是 MoE 模型，这直接影响 ds4 的量化与 SSD 流式设计（后续讲义会展开）。

如果你对上面某些词还一知半解，没关系，本讲只会用到它们的**直觉含义**。

## 3. 本讲源码地图

本讲的关键「源码」其实是两份**说明文档**（它们定义了项目的灵魂），辅以几个 C 文件名作为「这些理念落在代码的哪里」的索引：

| 文件 | 作用 | 本讲如何使用 |
| --- | --- | --- |
| `README.md` | 项目主文档，涵盖定位、动机、状态、用法、文件格式等 | 本讲的主力依据 |
| `AGENT.md` | 给（AI 或人类）贡献者的「设计宪法」：目标、质量规则、安全约束、目录布局、测试要求 | 解释「为什么这么写」 |
| `ds4.c` | 引擎本体（模型加载、分词、CPU 参考路径、会话、序列化），约 **2.7 万行** | 仅指出定位理念的落点，细节留到后续讲义 |
| `ds4_server.c` | OpenAI/Anthropic 兼容的 HTTP 服务器，约 **1.6 万行** | 同上 |
| `ds4_kvstore.c` | 磁盘 KV 缓存的读写与淘汰策略 | 「KV 即磁盘公民」的代码归宿 |

> 提示：本讲只**点名**这些 C 文件，不下钻到具体行号——它们是后续讲义的主角。本讲的源码精读会精确引用 `README.md` 与 `AGENT.md` 的行号。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应 spec 里的三个主题：

1. **4.1 项目动机与定位**——ds4 为什么存在、它选择做什么。
2. **4.2 与 llama.cpp/GGML 的关系**——继承什么、不依赖什么。
3. **4.3 beta 质量与状态说明**——项目现在处于什么阶段、出问题怎么办。

---

### 4.1 项目动机与定位

#### 4.1.1 概念说明

市面上能跑本地模型的工具已经很多了（llama.cpp、各种 GGUF runner……），那为什么 antirez 还要另起炉灶写一个 ds4？

答案藏在 README 开头那句自我定义里：

> DwarfStar is a small native inference engine optimized first for **DeepSeek V4 Flash** … It is intentionally **narrow**: **not a generic GGUF runner, not a wrapper** around another runtime: it is **completely self-contained**.

翻译过来就是：ds4 是一个**故意做窄**的引擎。它**不是**通用 GGUF 运行器，**不是**套在别的运行时外面的一层壳，而是**完全自包含**的——专门为 DeepSeek V4 Flash（以及高内存机器上的 PRO）从零写出来的 C 代码。

这种「窄」不是缺点，而是**核心策略**。README 在 Motivations 一节列出了四个现实前提，正是它们让「窄而精」变得值得：

- 又强又便宜的开源权重终于出现了（DeepSeek V4 Flash「感觉接近前沿」，PRO 更强，**两者都能扛住 2bit 量化**）。
- 又强又便宜的本地硬件出现了（MacBook、DGX Spark）。
- DeepSeek V4 的 KV 缓存设计让超大上下文变得**可行**。
- 这种几百 B 的模型，**就算比小模型「跑分低」，实际也更强**。

把这几条串起来：既然「好模型 + 好硬件 + 高效 KV」同时到位了，那就不要写一个「什么模型都能跑但都跑得不精」的工具，而是写一个「就为这一个模型做到端到端好用」的工具。

#### 4.1.2 核心流程：ds4 的「窄赌注」与 A/B/C/D 愿景

ds4 把自己的策略总结成一句话——**一次只赌一个模型**，并用三类校验保证它「真的好用」：

```text
选择一个当前最适合本地高端机的开源模型（现在是 DeepSeek V4 Flash / PRO）
        │
        ├── 官方向量校验：用官方实现跑出的 logits 做基准，本地结果必须对齐
        ├── 长上下文测试：不只测短句，长上下文也要正确
        └── agent 集成验证：接进真实的编码 agent，确认它「真的能干活」
        │
当更好的模型出现 → 可以整体替换（旧模型可能被完全移除）
```

注意「**机会主义（opportunistic）**」这个词：ds4 不承诺永远支持 DeepSeek。如果明天出现一个对 128GB 机器更合适的开源模型，项目**可能整体切换**，旧的模型甚至会被删掉。这种「随时可以换」的底气，恰恰来自它「窄」——因为没有历史包袱。

更进一步，作者把「本地推理应该是什么样」画成了一幅 **A/B/C/D 四件套**愿景（README 原文）：

- **A）带 HTTP API 的推理引擎**
- **B）为这个引擎和这些假设专门打磨过的 GGUF**
- **C）用编码 agent 实现的测试与校验**
- **D）为特定模型和执行环境定制的专用 agent**

ds4 的野心不是「能跑起来」，而是「让一个本地模型感觉**端到端被打磨完成（finished end to end）**」。这也是为什么仓库里同时有引擎、GGUF 生成工具、评测、基准、原生 agent——它们是同一个愿景的不同部件。

#### 4.1.3 源码精读

**定位的自我定义**——开篇就划清边界：「不是通用 GGUF runner、不是壳、完全自包含」：

[README.md:L1-L12](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1-L12)

> 这段同时说明了 ds4 要提供的全部能力：加载、提示渲染、工具调用、KV 状态（内存 + 磁盘）、服务器 API、集成编码 agent，外加 GGUF/imatrix 生成与质量/速度测试工具。注意它不是「一个推理库」，而是一整套。

**动机（Motivations）**——四条现实前提：

[README.md:L31-L36](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L31-L36)

> 其中「Both resist 2 bit quantization very well（两者都很好地扛住了 2bit 量化）」是后续量化讲义（u3-l4）的伏笔——正是因为模型对 2bit 鲁棒，ds4 才敢把 routed MoE 压到 2bit。

**「窄赌注」与 A/B/C/D 愿景**：

[README.md:L40-L43](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L40-L43)

> 第 40 行点明策略：deliberately narrow bet（刻意的窄赌注）、official-vector validation（官方向量校验）、long-context tests（长上下文测试）。第 43 行画出 A/B/C/D 四件套，并坦承「这是 beta 质量的代码」。

**KV 缓存即一等磁盘公民**——本手册最重要的理念之一：

[README.md:L42](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L42)

> 原文「**The KV cache is actually a first-class disk citizen**」。这句话的意思是：传统观点认为「KV 缓存属于内存」，但 DeepSeek V4 的压缩 KV + 现代 MacBook 的快 SSD 改变了这个观念——KV 缓存值得被当成**磁盘上一等公民**来持久化、复用、淘汰。它的代码归宿是 `ds4_kvstore.c`（磁盘 KV 缓存）与 `ds4.c` 里的 session 序列化，详见第 8 单元。

**AGENT.md 的呼应**——贡献者宪法里同样开宗明义：

[AGENT.md:L3-L5](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L3-L5)

> 「`ds4.c` is a DeepSeek V4 Flash specific inference engine. It is **not a generic GGUF runner**.」——和 README 的定位完全一致，并且补充了代码风格目标：**小巧、可读、高性能**，只在 Metal 必需处用 Objective-C。

#### 4.1.4 代码实践

**实践目标**：亲手从文档里把「窄而精」的定位挖出来，建立「文档说什么 → 代码在哪」的直觉。

**操作步骤**：

1. 打开 [README.md:L1-L12](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1-L12)，找出 ds4 自我定义里出现的**否定句**（「不是……」「不是……」），把它们抄下来。
2. 打开 [AGENT.md:L7-L14](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L7-L14)（Goals），数一数作者列了几条目标，并标记哪几条是在强调「保持窄、保持正确」。
3. 在一张纸上把 README 的 **A/B/C/D 四件套**对应到仓库里的具体产物，例如：
   - A → `ds4`、`ds4-server`
   - B → `gguf-tools/`（GGUF 生成与量化）
   - C → `tests/`、`ds4-eval`
   - D → `ds4-agent`

**需要观察的现象**：你会发现 README 的「否定句」和 AGENT.md 的「目标」是**同一件事的两面**——前者对外说「我们不做什么」，后者对内说「我们要守住什么」。

**预期结果**：你能用一句话回答「ds4 为什么不做成通用 GGUF runner」——因为通用性会稀释它对单一模型的端到端打磨。

> 待本地验证：本实践是纯阅读型，不涉及运行命令，因此无运行结果可验证。

#### 4.1.5 小练习与答案

**练习 1**：ds4 说自己是「opportunistic（机会主义）」的。如果某天出现一个比 DeepSeek V4 Flash 更适合 128GB 机器的开源模型，按 README 的说法，项目会怎么做？

**参考答案**：项目**可能整体切换**到新模型，旧的模型**可能被完全移除、不再支持**，除非两者能力有重叠。这种「随时可换」正是「窄」带来的好处——没有历史包袱。（依据 [README.md:L23-L29](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L23-L29)。）

**练习 2**：A/B/C/D 四件套里，哪一件最直接地体现了「不是只让模型跑起来，而是让它感觉 finished」？为什么？

**参考答案**：**D（专用 agent）** 和 **C（用编码 agent 做测试校验）** 最能体现。因为「能跑」只需要 A（引擎），而「端到端好用」必须接进真实 agent 验证它「真的能干活」（C），并为之定制体验（D）。这正是 README 第 43 行「make one local model feel finished end to end, not just runnable」的含义。

---

### 4.2 与 llama.cpp/GGML 的关系

#### 4.2.1 概念说明

很多人第一次见 ds4 会问：「这跟 llama.cpp 是什么关系？是套壳吗？」

**不是套壳。** README 有一节专门讲这件事，标题就叫 *Acknowledgements to llama.cpp and GGML*。关键的一句话是：

> `ds4.c` does **not link against GGML**, but it **exists thanks to the path opened by the llama.cpp project** …

也就是说：

- **不链接**：ds4 的可执行文件里**没有** GGML 库的代码，它从零用自己的 C 代码实现了 DeepSeek V4 的推理路径。
- **精神继承**：但 ds4 之所以能写出来，是因为 llama.cpp 已经「趟出了路」——量化格式、GGUF 生态、各种工程经验。
- **部分源码级保留/改写**：ds4 在 MIT 协议下**保留或改写**了一些来自 llama.cpp/GGML 的源码片段，主要是 **GGUF 量化布局与表、CPU 量化/点积逻辑、部分内核**。

所以更准确的比喻是：llama.cpp 是「开路先驱」，ds4 是「沿着这条路、为一辆特定的车（DeepSeek V4）专门修的一条赛道」。正因如此，ds4 在 `LICENSE` 里**保留了 GGML 作者的版权声明**——既是法律要求，也是真诚致谢。

理解这层关系很重要，因为它解释了 ds4 代码里你会看到的两个现象：
1. 一些量化结构（如 `block_q8_K`、`q4_K`、`q2_K`、`IQ2_XXS`）和 llama.cpp 高度相似——因为就是从那里改写来的（详见 `gguf-tools/quants.c`，第 u3-l4 讲）。
2. 但整个推理图、调度、KV 缓存、服务器都是 ds4 自己写的，没有 GGML 的 `ggml_backend` 抽象。

#### 4.2.2 核心流程：ds4 从 llama.cpp「继承」了什么、「自建」了什么

```text
                llama.cpp / GGML 生态
                        │
        ┌───────────────┼───────────────────────┐
        │ 继承（源码级改写）                      │ 自建（从零）
        │                                       │
   • GGUF 量化布局/表                     • DeepSeek V4 专用推理图
   • CPU 量化/点积参考逻辑                • MLA 注意力 + MoE 调度
   • 部分内核                             • 压缩 KV / indexer
   • 工程经验与设计参考                   • Metal/CUDA/ROCm 后端
                                          • HTTP 服务器 / 原生 agent
                                          • 磁盘 KV 缓存格式
                        │
                  ds4 不链接 GGML
```

一句话总结：**格式和数学借鉴，架构和工程自建。**

#### 4.2.3 源码精读

**致谢与法律关系**——明确「不链接」+「保留版权」：

[README.md:L46-L57](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L46-L57)

> 注意三个层次：(1) `ds4.c` does **not link against** GGML；(2) 仍保留/改写了 GGUF 量化布局、CPU 点积逻辑、某些内核；(3) 因此在 `LICENSE` 里保留 GGML 作者版权。这是「站在巨人肩膀上」的标准且诚实的做法。

**README 开头的即时致谢**——在介绍后端之后立刻点名感谢：

[README.md:L19-L21](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L19-L21)

> 「This project would not exist without llama.cpp and GGML」——并特别感谢 Georgi Gerganov（llama.cpp/GGML 作者）。

**AGENT.md 的呼应**——贡献者宪法里强调「不要引入 C++」（GGML/llama.cpp 是 C++，而 ds4 刻意保持纯 C）：

[AGENT.md:L24](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L24)

> 「Do not introduce C++.」——这是 ds4 与 llama.cpp 在技术选型上一个微妙但重要的区别：ds4 只在 Metal 必需处用 Objective-C，其余坚持纯 C。

#### 4.2.4 代码实践

**实践目标**：用证据区分「ds4 自建」与「ds4 从 llama.cpp 继承」的部分。

**操作步骤**：

1. 打开仓库根目录，找到 `LICENSE` 文件，看看里面是否真的保留了 GGML/llama.cpp 作者的版权声明（这印证了 README 第 57 行的说法）。
2. 打开 [gguf-tools/quants.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.h)（先只看结构体名，不纠结实现），找一找 `block_q8_K`、`block_q4_K`、`block_q2_K` 之类的名字——这些就是「继承自 llama.cpp」的量化块结构。
3. 对比 [AGENT.md:L32-L43](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L32-L43) 的目录布局，确认 `ds4.c`/`ds4_metal.m`/`metal/*.metal` 这些是 ds4 **自建**的推理路径，与 GGML 无链接关系。

**需要观察的现象**：你会看到量化结构「长得很 llama.cpp」，但整个目录布局里**找不到** `ggml.c`/`ggml.h` 或任何 GGML 后端抽象。

**预期结果**：你能指着具体的文件说「这部分是继承的（`gguf-tools/quants.*`），那部分是自建的（`ds4.c`、`ds4_metal.m`）」。

> 待本地验证：第 2、3 步若你尚未搭建阅读环境，可只做「文件存在性 + 结构体名」级别的浏览；量化语义留到 u3-l4 讲义深入。

#### 4.2.5 小练习与答案

**练习 1**：有人说「ds4 就是 llama.cpp 换皮」，请用 README 里的证据反驳。

**参考答案**：反驳点是「**不链接 GGML**」（[README.md:L48](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L48)）。ds4 没有把 GGML 编进可执行文件，推理图、KV 缓存、服务器、agent 全是从零自写的 C/Objective-C/CUDA 代码。llama.cpp 只是「开路先驱」，ds4 借鉴了量化格式与点积逻辑，但不是套壳。

**练习 2**：为什么 ds4 的 `gguf-tools/quants.c` 里会出现和 llama.cpp 几乎一样的量化结构？

**参考答案**：因为 ds4 在 MIT 协议下**保留/改写**了来自 llama.cpp/GGML 的「GGUF 量化布局与表、CPU 量化/点积逻辑、某些内核」（[README.md:L53-L57](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L53-L57)）。复用成熟的量化格式既保证与官方 GGUF 兼容，又避免重复造轮子。

---

### 4.3 beta 质量与状态说明

#### 4.3.1 概念说明

读完前两节，你可能会很兴奋地想去跑 ds4。但在动手前，必须先建立一个**正确的心理预期**：**这是 beta 质量的代码**。

README 的 Status 一节说得很直白：

> The code and GGUF files are to be considered of **beta quality** …

理由也很诚实：推理和模型服务「是个复杂的事」，而这一切才存在了**几天**，需要**几个月**才能更稳定。

具体到组件：

- **引擎 + 服务器 + GGUF**：**beta**。最近还刚加了几个大功能（分布式推理、SSD 流式等），所以尤其需要小心。
- **`ds4-agent`（原生编码 agent）**：**alpha**，因为它是后来才加的。

这不是「免责声明」式的水词，而是真正影响你怎么用 ds4：
- 遇到不对的生成，先用调试工具（`--trace`、`--dump-tokens`、`--dump-logprobs`）收集证据，再提 issue。
- 不要在生产环境里强依赖它，尤其是分布式/SSD 这些新路径。
- 协议（如分布式协议）**尚未 release-stable**，协调器和 worker 应当用**同一个 commit** 构建。

#### 4.3.2 核心流程：出问题时该怎么做

```text
生成结果看起来不对
        │
        ├── 先用三个小工具自检（README "Debugging Notes"）：
        │     • ./ds4 --dump-tokens -p "..."          → 看分词对不对
        │     • ./ds4 --dump-logprobs ... -p "..."    → 区分是采样问题还是 logit 问题
        │     • ./ds4-server --trace /tmp/trace.txt   → 看渲染/缓存/工具解析全过程
        │
        ├── 仍无法定位 → 开 issue，附上完整 --trace 日志
        │
        └── 心理预期：这是 beta，尤其是分布式 / SSD / agent 路径
```

与此配套的，是 **AGENT.md 里对「正确性优先」的铁律**：在贡献者宪法中明确写着「**Preserve correctness before speed**（正确性优先于速度）」——不允许保留一条「更快但有未解释的 attention/KV/logits 漂移」的路径。这与「beta 但严肃对待正确性」的态度是一致的。

此外，AGENT.md 列出了**四条必须保护的推理路径**，任何改动都得确认没有误伤它们：

1. **Metal 默认推理**（生产主路径）
2. **SSD 流式**
3. **分布式推理**
4. **CUDA**

这四条路径也是本手册后续多个单元的主角，记住它们，有助于你理解为什么很多代码写得「啰嗦」——为了同时维护多条路径的正确性。

#### 4.3.3 源码精读

**Status（beta / alpha 声明）**：

[README.md:L59-L68](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L59-L68)

> 注意第 65 行：出问题请用 `--trace` 记录会话，并在 issue 里附上完整 trace。第 68 行：`ds4-agent` 是 **alpha** 质量。这两条决定了你使用 ds4 的「安全边界」。

**正确性优先于速度的铁律**：

[AGENT.md:L13](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L13)

> 「Preserve correctness before speed. Do not keep a faster path with unexplained attention, KV cache, or logits drift.」——这是整个项目的质量底线。也正因为这条，ds4 才敢自称「官方向量校验」：任何速度优化都不能引入未解释的数值漂移。

**CPU 路径只是参考/调试**——理解为什么不要拿 CPU 当生产目标：

[AGENT.md:L12](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L12)

> 「Keep the CPU backend CPU-only and use it only as reference/debug code.」README 也警告：**当前 macOS 的虚拟内存实现有 bug，跑 CPU 路径可能导致内核崩溃**（[README.md:L44](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L44)）。所以 CPU 路径只用来对答案、调试分词，不是性能路径。

**四条必须保护的路径（贡献与 QA 的核心）**：

[AGENT.md:L51-L56](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L51-L56)

> 每次大改都要确认：Metal 主路径且速度没退化、SSD 流式、分布式（需先问用户）、CUDA（需用户提供机器）。这是 ds4 的「四大命脉」，也是后续讲义 u5/u6/u9 的主线。

#### 4.3.4 代码实践

**实践目标**：把「beta 状态」从一句口号，变成你可以操作的两件事——(a) 知道怎么报 bug，(b) 知道哪些路径最脆弱。

**操作步骤**：

1. 打开 [README.md:L1239-L1259](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1239-L1259)（Debugging Notes），把三个调试命令抄进你的笔记：
   - `./ds4 --dump-tokens -p "..."`
   - `./ds4 --dump-logprobs /tmp/out.json --logprobs-top-k 20 --temp 0 -p "..."`
   - `./ds4-server --trace /tmp/ds4-trace.txt ...`
2. 打开 [AGENT.md:L51-L56](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L51-L56)，把「四条命脉路径」抄下来，并标注每一条对应本手册的哪个单元（提示：Metal→u5、SSD→u9、分布式→u9、CUDA→u5）。
3. 在仓库里找到 `CONTRIBUTING.md` 与 `QA_BEFORE_RELEASES.md`，浏览它们的标题/小标题，感受「beta 但严肃 QA」的态度（细节留到 u11-l4）。

**需要观察的现象**：你会看到 ds4 提供了一套**自下而上的自检工具链**——从分词、logprob 到全量 trace，覆盖了「错在哪里」的各个层次。

**预期结果**：当别人问你「ds4 跑出来不对怎么办」时，你能立刻给出三件套调试命令，并提醒他「分布式/SSD/agent 是新路径，要格外小心」。

> 待本地验证：调试命令需要先编译 ds4（见 u1-l4）。如果你现在还没编译，第 1 步可只做「记录命令与用途」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 AGENT.md 说「**正确性优先于速度**」，而 README 同时又大篇幅讲速度优化（速度表、SSD 流式、分布式加速 prefill）？这两者矛盾吗？

**参考答案**：不矛盾。原则是「**不允许保留一条更快但有未解释的数值漂移的路径**」（[AGENT.md:L13](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L13)）。也就是说：速度优化 welcome，但必须**可解释、可校验**（通常靠官方向量）。一旦某条快路径的 attention/KV/logits 漂移无法解释，就必须让步于正确性。

**练习 2**：README 警告「macOS 上跑 CPU 路径可能让内核崩溃」。结合 AGENT.md，CPU 路径的正确用途是什么？

**参考答案**：CPU 路径**只用于参考/调试（reference/debug）**，比如核对分词、对比数值、诊断问题（[AGENT.md:L12](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L12)）。它**不是生产目标**，正常推理应当走 Metal 或 CUDA（[README.md:L44](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L44)）。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**贯穿性任务**（这也是本讲的官方 practice_task）：

> **阅读 README，用一段话写下 ds4 不同于通用 GGUF runner 的三个设计取舍，并指出它们分别体现在哪个源码文件或文档中。**

建议按以下流程做：

1. **先列候选**：从本讲已引用的 README 段落里，挑出 3 个「ds4 刻意和通用 runner 不一样」的点。参考方向（不要照抄，用自己的话组织）：
   - 取舍 A：**一次只支持一个模型 + 官方向量校验**（窄而精）。
   - 取舍 B：**只用自己的 GGUF，不做通用 GGUF loader**（专用量化组合）。
   - 取舍 C：**KV 缓存即磁盘一等公民**（而非纯内存）。
2. **再定位**：为每个取舍指出「它体现在哪里」。你可以借助下表起步（请自行补全/修正）：

   | 取舍 | 体现在哪里（文档 / 源码） |
   | --- | --- |
   | 窄而精 + 官方向量校验 | `README.md` Motivations / Status；`AGENT.md` Goals |
   | 只用自己的 GGUF | `README.md` Model Weights；量化格式见 `gguf-tools/quants.*` |
   | KV 即磁盘公民 | `README.md` Disk KV Cache；代码在 `ds4_kvstore.c`、`ds4.c` |

3. **写一段话**：把三个取舍和你找到的证据，写成一段连贯的中文（建议 150~250 字），要求每个取舍后用括号注明出处文件。
4. **自检**：回头对照 [README.md:L1-L12](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1-L12) 的「not a generic GGUF runner, not a wrapper」——你写的三个取舍，是否都能呼应这句自我定义？

**交付物**：一段话 + 一张「取舍 → 出处」对照表。

> 这一步是纯阅读与写作，不涉及编译运行，因此无运行结果可验证；重点是训练「文档主张 ↔ 代码落点」的对应能力，这是后续读源码的基本功。

## 6. 本讲小结

- **ds4 是什么**：一个为 DeepSeek V4 Flash / PRO 量身打造的、**自包含**的原生推理引擎，**不是**通用 GGUF runner、**不是**套壳。
- **核心取舍**：**窄而精**（一次只死磕一个模型，可整体替换）、**官方向量校验**（用官方 logits 做基准）、**本地高端机器优先**（96/128GB 起步）。
- **核心愿景**：本地推理 = A 引擎+API ＋ B 专用 GGUF ＋ C agent 测试校验 ＋ D 专用 agent，目标是让一个本地模型「端到端被打磨完成」。
- **核心理念**：**「KV 缓存即一等磁盘公民」**——KV 不只属于内存，它值得被磁盘持久化、复用、淘汰（落点：`ds4_kvstore.c`）。
- **与 llama.cpp 的关系**：**不链接** GGML，但源码级继承量化格式/点积逻辑，并在 `LICENSE` 保留 GGML 版权；架构与工程全自建。
- **当前状态**：引擎/服务器/GGUF 为 **beta**，`ds4-agent` 为 **alpha**；出问题用 `--trace` 等三件套自检；贡献底线是「正确性优先于速度」，并须保护 Metal/SSD/分布式/CUDA 四条路径。

## 7. 下一步学习建议

本讲建立的是「**为什么**」，下一讲开始进入「**是什么和怎么跑**」。建议按本手册的顺序继续：

1. **u1-l2（DeepSeek V4 模型背景与量化策略）**：先认识 ds4 服务的那两个模型——Flash 与 PRO 的区别、为什么只量化 routed MoE、MTP 与长上下文的意义。
2. **u1-l3（目录结构与源码地图）**：建立「哪个文件负责什么」的心智地图，这是后续所有源码讲义的导航基础。
3. **u1-l4（构建系统与多后端编译）**与 **u1-l5（下载模型与首次运行）**：亲手把 ds4 跑起来，让本讲的抽象理念变成屏幕上真实的 token。

> 在进入 u1-l2 之前，你可以再读一遍 [README.md:L31-L44](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L31-L44)，确认自己能脱口而出「窄赌注」「官方向量校验」「KV 即磁盘公民」这三句话——它们会贯穿整本手册。
