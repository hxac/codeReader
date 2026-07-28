# TinyZero 是什么：复现 DeepSeek R1 Zero

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是让你在「不写一行训练代码」的情况下，先从全局看懂 TinyZero 这个项目到底在做什么。

读完本讲，你应该能够：

- 说清 **TinyZero 与 DeepSeek R1 Zero 的关系**——它是一个轻量化的复现实验。
- 理解 **R1 Zero 的核心思想**：跳过监督微调（SFT），直接用强化学习（RL）训练基座模型，让推理能力「自己涌现」。
- 知道 TinyZero 的 **技术栈**：基于 veRL 框架，使用 Qwen2.5 系列基座模型。
- 看懂 README 里「**under $30**」「**Aha moment**」「**countdown / multiplication 任务**」这几个关键词的真实含义。

本篇不涉及任何算法细节，所有数学和源码实现都留给后续讲义。在这里你只需要建立一个正确的「项目心智模型」。

---

## 2. 前置知识

本讲面向零基础读者，但下面三个名词最好先有个模糊印象，不懂也没关系，正文中会再解释：

- **大语言模型（LLM）**：像 Qwen、GPT 这样的语言模型，输入一段文字，输出一段文字。
- **监督微调（SFT, Supervised Fine-Tuning）**：用「人工写好的标准答案」教模型怎么回答。这是训练对话模型最常见的做法。
- **强化学习（RL, Reinforcement Learning）**：不给人写的标准答案，而是给模型一个「打分器（奖励函数）」，让模型自己不断试错、朝着高分方向调整。AlphaGo 学下棋用的就是这种思路。

一句话区分：SFT 像「老师手把手教」，RL 像「只告诉你得分规则，剩下的你自己摸索」。

> 一个关键概念：**R1 Zero 路线**。它特指「完全跳过 SFT，直接在基座模型上做 RL」。TinyZero 要复现的，正是这条路线在小模型 + 简单任务上是否同样有效。

---

## 3. 本讲源码地图

本讲只涉及两个文件，它们都是文档文件（Markdown），不是代码：

| 文件 | 作用 | 本讲用途 |
| --- | --- | --- |
| `README.md` | TinyZero 当前的项目主页：定位、安装、训练脚本入口、任务说明 | 读懂项目「是什么」、选了哪些任务、为什么强调便宜 |
| `OLD_README.md` | 仓库「原始文档」，内容其实是 **veRL 框架**的 README | 看清 TinyZero 站在谁的肩膀上、底层框架提供哪些能力 |

> 小提示：仓库里还有 `scripts/`、`examples/`、`verl/` 等大量代码目录，本讲先不碰，它们会在 u1-l2（目录结构）和后续讲义中逐一展开。

---

## 4. 核心概念与源码讲解

### 4.1 TinyZero 的项目定位：复现 R1 Zero

#### 4.1.1 概念说明

先看项目主页第一行对自己的一句话定义：

> TinyZero is a reproduction of DeepSeek R1 Zero in countdown and multiplication tasks. We built upon veRL.

这句话信息量很大，拆成三层：

1. **它是一个「复现（reproduction）」**：不是发明新算法，而是把 DeepSeek 公司开源的 **R1 Zero** 训练范式，在更小、更便宜的规模上重新做一遍，验证「这套方法真的能让模型学会推理」。
2. **它在两个具体任务上做**：`countdown`（数字凑数）和 `multiplication`（乘法）。
3. **它构建于 veRL 之上**：底层训练框架用的是开源的 veRL。

**为什么需要这样一个复现？** DeepSeek 的 R1 Zero 用的是几百亿参数的大模型、海量算力，普通人无法复现。TinyZero 的价值在于：用 3B（30 亿）参数的小模型、两个能自动判分的简单任务，证明「纯 RL 涌现推理」这件事在小尺度上也成立、而且人人可复现。

#### 4.1.2 核心流程

从「项目是什么」的角度，TinyZero 的工作可以概括成一条很短的流程：

```
选一个基座模型(Qwen2.5)
        │
        ▼
准备两个能自动判分的任务(countdown / multiply)
        │
        ▼
跳过 SFT，直接用 RL(由 veRL 框架驱动)训练
        │
        ▼
观察基座模型是否「自己学会」推理、自我验证、搜索
```

注意：这里没有「人工标注答案」的环节。模型完全靠奖励函数的打分信号，自己摸索出更好的推理方式。这就是 R1 Zero 路线最反直觉、也最吸引人的地方。

#### 4.1.3 源码精读

README 顶部的项目定位、实验现象和成本承诺，集中在开头几行，这是理解整个项目的「纲领性」文字：

- [README.md:L8](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L8) —— 项目一句话定位：在 countdown 和 multiplication 任务上复现 R1 Zero，构建于 veRL。
- [README.md:L10](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L10) —— 核心实验结论：**3B 基座模型通过 RL，自己发展出了 self-verification（自我验证）和 search（搜索）能力**。这正是后续要重点观察的「涌现」现象。
- [README.md:L12](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L12) —— 成本承诺：你可以在 **不到 30 美元** 的花费内亲自体验 Aha moment。

另外，README 第一行有一段重要的项目状态提示，初学者一定要看到：

- [README.md:L3-L4](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L3-L4) —— **弃用说明（Deprecation Notice）**：该仓库已不再积极维护，作者建议直接使用最新的 veRL 库做 RL 实验；原始文档归档在 `OLD_README.md`。

这条提示说明：**学习 TinyZero 的主要价值在于「读懂它的实现与思路」**，而如果要投入生产训练，社区重心已经转移到上游的 veRL。好消息是 TinyZero 的代码本来就是 veRL 的子集，学懂 TinyZero 几乎等于入门 veRL。

#### 4.1.4 代码实践

这是一个「源码阅读型实践」，不需要运行任何代码。

1. **实践目标**：用一句话向一个完全没听过 TinyZero 的人解释它是什么。
2. **操作步骤**：
   - 打开 [README.md:L1-L18](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L1-L18)，只读前 18 行。
   - 找到三处关键信息：项目定位（L8）、实验现象（L10）、成本（L12）。
3. **需要观察的现象**：你会注意到 README 几乎没有讲算法，全是「定位 + 怎么跑」。这说明这个仓库面向「动手复现」而非「理论讲解」。
4. **预期结果**：你能复述出「TinyZero = 在两个简单任务上、用 RL、在 veRL 上复现 R1 Zero」这一定义。

#### 4.1.5 小练习与答案

**练习 1**：README 说 TinyZero 是 R1 Zero 的「reproduction」。reproduction（复现）和 invention（发明）的区别，对这个项目意味着什么？

> **参考答案**：复现意味着算法范式（跳过 SFT 直接 RL）来自 DeepSeek R1 Zero，TinyZero 没有提出新算法；它的贡献是把这套范式搬到「小模型 + 简单任务 + 低成本」的设置下，证明其可复现、可上手。

**练习 2**：为什么 README 要把「countdown 和 multiplication」这两个任务明确写进项目定位里，而不是只说「一个推理任务」？

> **参考答案**：因为这两个任务的「答案对错可以用规则自动判定」（比如乘法结果对不对直接算一下就知道），这正是能用 RL 训练的前提——需要一个廉价、准确、自动的奖励信号，而不依赖人工标注。

---

### 4.2 R1 Zero 的核心思想：纯 RL 让推理「自己涌现」

#### 4.2.1 概念说明

要理解 TinyZero，必须先理解它复现的对象：**DeepSeek R1 Zero**。

R1 Zero 路线有两个反常识的特点：

1. **不要 SFT 冷启动**：传统做法是先用大量「人类示范」微调模型，再做 RL。R1 Zero 直接跳过这一步，拿一个「没学过怎么好好回答问题」的基座模型，硬上 RL。
2. **只靠规则奖励**：不给模型看人类写的参考答案，只给它一个「判分器」，告诉它最终答案对不对。模型为了拿高分，必须自己琢磨出「先思考、再验证、再回答」的策略。

**Aha moment（顿悟时刻）** 指的就是：训练到某个阶段，模型突然「自发地」表现出像人一样的行为——它会停下来重新检查自己的思路、会自我否定、会换一条路再试。这种「反思 / 自我验证 / 搜索」能力不是被教出来的，而是从 RL 的奖励信号里「涌现」出来的。

README 里那句 **"the 3B base LM develops self-verification and search abilities all on its own"** 描述的正是这个现象。

#### 4.2.2 核心流程

把「纯 RL 涌现推理」抽象成最朴素的循环：

```
repeat（训练很多轮）:
    1. 模型针对一个问题，采样出多个回答
    2. 奖励函数给每个回答打分（规则判分，对=高分）
    3. 用 RL 算法(PPO/GRPO)调整模型，
       让「高分回答」对应的输出概率上升
    4. 模型逐渐学会：先推理、再验证、最后作答
```

从直觉上，RL 的目标可以粗略理解为「最大化期望奖励」：

\[
J(\theta) \;\propto\; \mathbb{E}_{\text{回答}\sim \pi_\theta}\big[\,\text{奖励}(\text{回答})\,\big]
\]

即：调整模型参数 \(\theta\)，使得模型 \(\pi_\theta\) 更倾向于产出高奖励的回答。至于具体怎么算梯度、怎么裁剪、怎么估优势函数（advantage），那是 PPO/GRPO 的细节，会在第 5 单元（u5）的 core_algos 里详细推导，这里只需记住「**调高好答案的概率、调低差答案的概率**」这个直觉即可。

> 为什么 TinyZero 选 countdown / multiply 而不是数学竞赛题？因为这两类任务能给出**精确、即时、自动**的奖励信号——这正是 RL 最需要的「干净反馈」。复杂任务往往很难自动判分。

#### 4.2.3 源码精读

README 开头几行直接给出了「现象 + 证据 + 入口」三件套：

- [README.md:L10](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L10) —— 实验现象：3B 基座模型自行发展出自我验证与搜索能力。
- [README.md:L12](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L12) —— 成本门槛：< $30 即可亲自体验「Aha moment」。
- [README.md:L14](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L14) —— Twitter 线程（作者第一手讲解与现象截图）：`https://x.com/jiayi_pirate/status/1882839370505621655`
- [README.md:L16](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L16) —— 完整实验日志（Weights & Biases）：`https://wandb.ai/jiayipan/TinyZero`

这四行是后续所有「涌现」相关讨论的证据来源。第 7 单元（u7-l6）会专门结合这些日志，解读「Aha moment」与调参的关系。

#### 4.2.4 代码实践

1. **实践目标**：从第一手实验日志里，直观感受「纯 RL 训练」的变化曲线。
2. **操作步骤**：
   - 打开 [README.md:L16](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L16) 给出的 wandb 链接 `https://wandb.ai/jiayipan/TinyZero`（如需登录可免费注册）。
   - 尝试找到与「回答长度（response_length）」「奖励分数（score / reward）」相关的曲线。
3. **需要观察的现象**：训练后期，模型生成的回答往往**变长**（因为它学会了写更多推理与自我验证步骤），同时奖励分数**上升**。
4. **预期结果**：「回答变长 + 分数上升」同时出现，是 R1 Zero 涌现现象在指标上的典型信号。若某些指标因 wandb 权限无法查看，记为 **待本地验证**。
5. **备注**：本步只是「看图建立直觉」，不涉及运行代码；具体的指标定义会在 u7-l5（测试与实验跟踪）讲清。

#### 4.2.5 小练习与答案

**练习 1**：R1 Zero 路线「跳过 SFT」意味着什么？这对数据准备的要求是变高了还是变低了？

> **参考答案**：意味着不依赖人工写「示范回答」来微调。这让数据准备的「人工标注」负担大幅降低——只需要「问题 + 一个自动判分器」，不需要「问题 + 标准示范」。难度转移到了「设计一个靠谱的自动奖励函数」上。

**练习 2**：用自己的话解释「Aha moment」为什么会让人觉得惊讶。

> **参考答案**：因为我们并没有显式地教模型「要先自我验证、要回溯搜索」，奖励函数只看最终答案对不对。但模型为了拿更高分，自己摸索出了这些类似人类反思的高级行为。这种「能力未被直接指定却自然出现」的现象，就是它令人惊讶的原因。

**练习 3**：为什么「回答长度随训练变长」会被当作推理能力涌现的一个信号？

> **参考答案**：因为模型开始产出更多的中间推理、验证和反复尝试步骤，而不是直接蹦出一个答案。回答变长（在奖励也在涨的前提下）说明它在「思考」，而不是在「凑字数」。

---

### 4.3 veRL 框架与 Qwen2.5 基座：TinyZero 站在谁的肩膀上

#### 4.3.1 概念说明

TinyZero 不是从零造轮子。README 明确写了两条「致谢/依赖」：

- 训练框架基于 **veRL**（火山引擎开源的 LLM 强化学习框架）。
- 基座模型用 **Qwen2.5** 系列。

**veRL 是什么？** 它是一个「面向大语言模型、生产可用的强化学习训练框架」，论文对应的是 **HybridFlow**。它帮你把 RLHF/PPO 训练里最难的部分都做好了：怎么把模型切分到多张 GPU（FSDP / Megatron）、怎么高效生成回答（vLLM）、怎么在「训练态」和「生成态」之间同步权重、怎么把训练流程编排起来。

**为什么这条很重要？** 因为读懂这一点，你就理解了 TinyZero 仓库的本质：**它本身几乎不写训练算法，而是「在 veRL 之上加一层任务数据 + 奖励函数」**。换句话说：

```
TinyZero = veRL 框架(提供 PPO 训练引擎)
         + countdown/multiply 任务数据
         + 对应的规则奖励函数
```

理解了这个「分层」，你就能预测本手册后续的讲义结构：大量篇幅在讲 veRL 的源码（数据协议、调度、训练循环、算法），而真正属于「TinyZero 自己」的代码其实很少——主要集中在数据预处理和奖励打分两处。

#### 4.3.2 核心流程

从「技术栈依赖」的角度，一次 TinyZero 训练涉及的分工是：

| 关注点 | 由谁负责 | 本手册对应单元 |
| --- | --- | --- |
| 任务数据生成（parquet） | TinyZero 自己的 `examples/data_preprocess/` | u2 |
| 规则奖励函数（判分） | TinyZero 自己的 `verl/utils/reward_score/` | u2-l4 |
| PPO/GRPO 训练引擎 | veRL 框架（`verl/trainer/`、`verl/workers/`） | u4、u5、u6 |
| 多卡编排与生成 | veRL 框架（single-controller、vLLM） | u3、u6 |
| 基座模型权重 | 外部下载的 Qwen2.5 | u1-l3 |

这个表格是整本手册的「导航地图」，建议先看一眼，不必现在记住。

#### 4.3.3 源码精读

**关于 veRL 框架的证据**，最有说服力的是 `OLD_README.md`。这个文件名义上是「原始文档」，内容其实就是 veRL 框架自己的 README，直接暴露了 TinyZero 与 veRL 的同源关系：

- [OLD_README.md:L1](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/OLD_README.md#L1) —— 标题赫然写着「veRL: Volcano Engine Reinforcement Learning for LLM」，说明仓库根目录沿用了 veRL 的文档骨架。
- [OLD_README.md:L3-L5](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/OLD_README.md#L3-L5) —— veRL 自我介绍：面向 LLM 的、灵活、高效、生产可用的 RL 训练框架，是 HybridFlow 论文的开源版本。
- [OLD_README.md:L11](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/OLD_README.md#L11) —— 关键能力：解耦计算与数据依赖，无缝集成 PyTorch FSDP、Megatron-LM、vLLM 等已有 LLM 基础设施。这正是 u6（Worker 与混合引擎）要讲的内容。
- [OLD_README.md:L38-L46](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/OLD_README.md#L38-L46) —— veRL 的 Key Features 清单：FSDP/Megatron 训练、vLLM/TGI 生成、HF 模型支持、PPO、flash-attention、扩展到 70B 与数百卡、wandb/mlflow 跟踪。

**关于「基于 veRL + 用 Qwen2.5」的明确声明**，则在新 README 的致谢段：

- [README.md:L105](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L105) —— 实验基于 veRL。
- [README.md:L106](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L106) —— 使用 Qwen2.5 系列基座模型。

#### 4.3.4 代码实践

1. **实践目标**：确认 veRL 的核心能力清单，并把它和后续讲义对应起来。
2. **操作步骤**：
   - 打开 [OLD_README.md:L36-L46](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/OLD_README.md#L36-L46)，阅读 Key Features 列表。
   - 逐一标出：哪些是「训练」（FSDP/Megatron/PPO/SFT）、哪些是「生成」（vLLM/TGI）、哪些是「工程」（flash-attention/sequence packing/wandb）。
3. **需要观察的现象**：你会发现 veRL 的功能远超 TinyZero 实际用到的部分——TinyZero 只用了其中「PPO + FSDP + vLLM + Qwen2.5」这一小撮。
4. **预期结果**：你能说出「TinyZero 用到了 veRL 的 PPO 训练和 vLLM 生成，但没用上 RM 训练、DPO、Megatron 等其他能力」。

#### 4.3.5 小练习与答案

**练习 1**：为什么说 `OLD_README.md` 这个文件名有「迷惑性」？它的真实内容是什么？

> **参考答案**：文件名像是「TinyZero 的旧版说明」，但真实内容是 **veRL 框架的 README**。这暴露了一个事实：TinyZero 仓库是从 veRL fork 而来的，作者把 veRL 的原始 README 归档为 `OLD_README.md`，再用自己的 `README.md` 覆盖了项目主页。

**练习 2**：基座模型选 Qwen2.5 而不是自己训练一个模型，这对「复现 R1 Zero」的结论有什么影响？

> **参考答案**：R1 Zero 的要点是「在已有基座模型上做纯 RL」。选一个成熟的公开基座（Qwen2.5）能排除「基座本身能力差异」的干扰，让结论更聚焦于「RL 是否带来推理涌现」，也更方便他人复现。

**练习 3**：如果 veRL 已经提供了完整的 PPO 训练能力，那 TinyZero 这个仓库「自己写了什么」？

> **参考答案**：主要写了两部分——(1) countdown / multiply 的**任务数据生成脚本**；(2) 这两个任务的**规则奖励函数**（判分逻辑）。训练引擎本身几乎全是 veRL 的代码。

---

## 5. 综合实践

本讲的核心实践任务是下面这道「阅读理解 + 总结」题，它把本讲三个模块串在一起。

**任务**：阅读 [README.md](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md)（重点 L8–L16）与实验日志链接 [README.md:L14-L16](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L14-L16)，用你自己的话写一段 **不超过 200 字** 的中文说明，要求同时覆盖以下三点：

1. TinyZero 与 DeepSeek R1 Zero 的**关系**（复现 / 基于谁 / 跳过 SFT）。
2. 它选用的**两个任务**是什么，以及为什么选它们（能自动判分）。
3. 为什么 README 要强调 **「under $30」**（小模型、低成本、人人可复现）。

**写作提示**：

- 不要照抄英文原文，用中文重新组织。
- 第 2 点可以顺带提一句「奖励函数自动判分」是 RL 训练的前提。
- 第 3 点要和「DeepSeek 原版用大模型、高算力」做对比，才能凸显 TinyZero 的意义。

**自检标准**：写完后，找一个没接触过这个项目的朋友（或假装是）读一遍——如果对方读完能回答「TinyZero 在干嘛、用什么技术栈、为什么值得看」，这段总结就算合格。

> 本实践为「源码阅读 / 文档阅读型实践」，不需要运行任何命令，没有需要「待本地验证」的运行结果。

---

## 6. 本讲小结

- **TinyZero 是 DeepSeek R1 Zero 的轻量化复现**：在 countdown 和 multiplication 两个任务上，用 3B 基座模型验证「纯 RL 涌现推理」。
- **R1 Zero 的核心思想是「跳过 SFT、直接 RL」**：只靠规则奖励信号，让模型自己学会推理、自我验证与搜索，即所谓「Aha moment」。
- **技术栈很清晰**：训练框架基于 veRL（HybridFlow 的开源版），基座模型用 Qwen2.5。
- **仓库本质是「veRL + 任务数据 + 奖励函数」**：真正属于 TinyZero 自己的代码很少，主要在数据预处理和奖励打分两处。
- **项目已标注弃用**：作者建议生产环境直接用上游 veRL；学懂 TinyZero 几乎等于入门 veRL。
- **关键证据都在 README 开头**：项目定位、实验现象、< $30 成本、Twitter 与 wandb 日志链接，构成了后续所有讨论的基础。

---

## 7. 下一步学习建议

本讲只建立了「项目是什么」的心智模型，还没有碰任何代码。建议按顺序继续：

1. **u1-l2（环境安装与目录结构）**：先把仓库目录看懂，知道 `scripts/`、`examples/`、`verl/` 各自装着什么，为后面读源码做准备。
2. **u1-l3（跑通第一次训练）**：亲手把 countdown 任务跑起来（或至少读懂 `train_tiny_zero.sh`），把「项目定位」从纸面落到可运行的命令上。
3. **u2-l1（Countdown 数据生成）**：开始接触第一份真正属于 TinyZero 的代码——数据预处理脚本，理解任务数据长什么样。

> 阅读建议：在读后续讲义前，先把本讲的「综合实践」做完。能用 200 字讲清一个项目，说明你已经具备读懂它源码的前提了。
