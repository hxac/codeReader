# 项目总览：slime 是什么

## 1. 本讲目标

本讲是 slime 学习手册的第一讲。读完本讲，你应当能够：

- 用一句话说清 slime 是什么、解决什么问题，以及它和「纯训练框架」的本质区别；
- 说出 slime 的两大核心能力（高性能训练 / 灵活数据生成）是怎么定义的；
- 准确画出 slime 的三大模块——`training (Megatron)` / `rollout (SGLang + router)` / `data buffer`——之间的数据流向，并指出每个模块读什么、写什么；
- 了解 slime 的生产验证背景（GLM 系列等），理解它为什么被当作「基础设施」而非「脚本」来设计。

本讲不涉及任何代码改动，全部围绕 `README.md`、`README_zh.md` 与官方介绍博客 `docs/en/blogs/introducing_slime.md` 展开。目的是为后续所有讲义建立一个清晰的全局认知。

## 2. 前置知识

在开始之前，请确认你对下面几个名词有基本的直觉。不需要精通，知道它们大概做什么即可：

- **大语言模型（LLM）**：像 GPT、Qwen、GLM 这样的语言模型，本质是一个根据上文预测下一个 token 的概率模型。
- **强化学习（RL）**：一种训练范式。模型（称为 policy）与环境交互，产生回答，再根据「奖励（reward）」的好坏来调整自身参数，让「好回答」出现得更频繁。
- **后训练（post-training）**：在模型完成「预训练（pre-training）」之后、正式上线之前的那一段训练。RL 后训练就是这一阶段里用强化学习来对齐和提升模型能力。
- **推理（inference）**：用一个已经训好的（或正在训的）模型生成文本。slime 用 **SGLang** 来做这件事。
- **训练（training）**：根据数据更新模型参数。slime 用 **Megatron-LM** 来做这件事。
- **Ray**：一个分布式计算框架，用来把「训练」和「推理」两类任务调度到一组 GPU 上。

一个关键直觉（后面会反复用到）：**RL 训练和普通监督学习（SFT）最大的不同，在于它需要在训练过程中「实时地、源源不断地」生成新数据。** SFT 的数据是事先准备好的固定文件；RL 的数据是模型自己当下「采样」出来的。这就是为什么 slime 必须把「训练」和「推理」两件事紧紧缝合成一个闭环。

形式化地，RL 的目标是最大化期望奖励：

\[
J(\theta) = \mathbb{E}_{\tau \sim \pi_\theta}\big[\, r(\tau) \,\big]
\]

其中 \(\pi_\theta\) 是当前模型（参数为 \(\theta\)）的采样策略，\(\tau\) 是一次采样轨迹，\(r(\tau)\) 是这次采样的奖励。注意期望是在「当前参数」下取的——所以一旦 \(\theta\) 更新，旧数据就「过期」了，必须用新参数重新采样。这正是闭环必须不断运转的根本原因。

## 3. 本讲源码地图

本讲只读三个文档文件，它们是理解 slime 定位的「第一手资料」：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目主页（英文）。定义了 slime 的两大核心能力、设计取舍、生产验证背景、架构总览与参数分类。 |
| `README_zh.md` | 项目主页（中文），内容与 `README.md` 基本对应，适合中文读者对照阅读。 |
| `docs/en/blogs/introducing_slime.md` | 官方介绍博客。从「愿景」角度解释 slime 为什么这样设计，尤其是「SGLang-native」「单一 rollout backend」「把训练循环写在 train.py 里」这些关键决策。 |

> 提示：本讲引用的所有「源码」其实都是 Markdown 文档。这是合理的——第一讲的目标是建立认知地图，真正的 `.py` 源码从第 2 讲（目录结构）开始才会进入。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：
1. **模块 1：README 项目定位** —— slime 是什么、两大核心能力。
2. **模块 2：架构总览章节** —— 三大模块与闭环数据流。
3. **模块 3：introducing_slime 博客** —— 设计哲学与「SGLang-native」取舍。

---

### 4.1 模块：README 项目定位

#### 4.1.1 概念说明

打开任何项目第一件事，都是先搞清楚它「是什么」。slime 的 README 开篇就给出了精确的一句话定位：

> **slime** is an LLM post-training framework for RL scaling, providing two core capabilities.

拆开来理解三个关键词：

- **LLM post-training framework**：这是一个面向大语言模型「后训练」阶段的框架（不是预训练框架）。
- **RL scaling**：重点在「scaling」，即把强化学习训练**大规模、可持续地**跑起来——更多卡、更大模型、更长时间。
- **two core capabilities**：它提供的是两大能力，而不是一个。

这两大能力分别是：

1. **High-Performance Training（高性能训练）**：通过把 Megatron 与 SGLang 连接起来，支持各种模式的高效训练。
2. **Flexible Data Generation（灵活数据生成）**：通过自定义数据生成接口和 server-based engine，实现任意的训练数据生成流程。

这里有一个容易混淆的点要特别强调：slime **不是**一个「训练框架 + 一个推理框架」的简单拼接。它的设计目标是让这两大能力**彼此强化**，并且所有东西——训练、采样、奖励、校验、环境交互——都走**同一条** `training / rollout / Data Buffer` 路径，而不是拼出一堆割裂的 trainer、rollout service 和 agent framework。

#### 4.1.2 核心流程

从「问题 → slime 的解法」的角度，可以这样描述 slime 的存在意义：

```text
问题：RL 训练需要「边训练、边采样」，把训练和推理缝成一个闭环很难，
      还要支持 math / code / tool / sandbox / multi-agent 等各种数据来源。

slime 的解法：
  能力 A（高性能训练）：Megatron 负责训，保证大规模下也快；
  能力 B（灵活数据生成）：SGLang + 自定义接口负责采样，保证数据来源无限自由；
  约束：A 和 B 走同一条数据流，互为表里，不分叉成多套框架。
```

一个贯穿全手册的判断标准：**任何「需要单独 fork 一个框架」的诉求，slime 都希望改造成「在统一数据流里插一段自定义逻辑」来解决。** 这是它和很多其他 RL 框架的根本分歧点，下一讲的博客模块会详细展开。

#### 4.1.3 源码精读

slime 的定位与两大能力定义在 README 的开篇：

[README.md:L9-L16](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/README.md#L9-L16)

> 这段话先给出「两大核心能力」，再用一句话点出 slime 的核心设计哲学：让两种能力彼此强化，而**不是**变成割裂的 trainer / rollout service / agent framework。

slime 的生产验证背景（这是判断一个 RL 框架是否可靠的重要依据）在「Why This Design Matters」和「Production Validation」两节：

[README.md:L20-L34](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/README.md#L20-L34)

> 这里明确：slime 是 GLM-5.2 / GLM-5.1 / GLM-5 / GLM-4.7 / GLM-4.6 / GLM-4.5 背后的 RL 训练框架，并额外支持 Qwen 系列（Qwen3.6、Qwen3.5、Qwen3Next、Qwen3MoE、Qwen3、Qwen2.5）、DeepSeek V3 系列（V3、V3.1、R1）和 Llama 3。**验证的是完整的 post-training loop，而不是孤立的小例子。**

slime 把参数分为三类（这一点在第 4 讲「运行第一个训练」会展开，这里先建立印象）：

[README.md:L162-L168](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/README.md#L162-L168)

> 三类参数：Megatron 参数（直接读取）、SGLang 参数（加 `--sglang-` 前缀透传）、slime 自身参数（见 `slime/utils/arguments.py`）。这种「透传」设计是 slime 的招牌特性，模块 3 会详细解释。

#### 4.1.4 代码实践

**实践目标**：通过精读 README 的开篇定义，确认你已经抓住了 slime 的「一句话定位」。

**操作步骤**：

1. 打开 `README.md`，只读第 9 行到第 16 行。
2. 打开 `README_zh.md`，读第 9 行到第 16 行（中文版），对照理解。
3. 在你的笔记里，用**自己的话**（不要复制原文）补全下面这个填空：
   - 「slime 是一个为 ___ 设计的 ___ 框架，它提供 ___ 和 ___ 两大核心能力。」
4. 思考并写下：为什么 README 强调这两大能力要「彼此强化」，而不是做成两个独立模块？

**需要观察的现象**：你会注意到中英文两版 README 在「核心能力」这一段的措辞几乎完全对应，说明这是项目最想让你记住的定位。

**预期结果**：你能不看原文，用自己的话把 slime 是什么讲给一个完全没听过它的人听。

> 待本地验证：本实践无需运行任何命令，属于「阅读型实践」。

#### 4.1.5 小练习与答案

**练习 1**：slime 是预训练框架还是后训练框架？

> **参考答案**：是**后训练（post-training）框架**。它的定位是「LLM post-training framework for RL scaling」，作用在预训练完成之后的阶段，用强化学习来对齐和提升模型。

**练习 2**：slime 的两大核心能力分别是什么？它们是割裂的两套工具吗？

> **参考答案**：两大能力是「高性能训练（Megatron + SGLang 连接）」和「灵活数据生成（自定义接口 + server-based engine）」。它们**不是**割裂的——README 明确说要让二者「彼此强化，避免变成一组割裂的 trainer、rollout service 和 agent framework」，所有流程都走同一条 `training / rollout / Data Buffer` 路径。

---

### 4.2 模块：架构总览章节（三大模块）

#### 4.2.1 概念说明

理解了「slime 是什么」之后，第二件最重要的事就是理解它的**三大模块架构**。这是后续所有源码阅读的总框架——之后你读到的每一个 `.py` 文件，都可以归到这三个模块之一。

slime 把整个系统抽象成三个模块：

1. **training（Megatron）**：训练模块。负责主训练流程。
2. **rollout（SGLang + router）**：采样/生成模块。负责生成新数据。
3. **data buffer**：数据缓冲区。是连接前两者的「桥梁」。

这三个模块通过 **Data Buffer** 串成一个**闭环**，这也是 slime 区别于普通训练框架的核心。

#### 4.2.2 核心流程

用「数据流向」来描述这个闭环（请配合理解，后续每一讲都在细化这张图）：

```text
        ┌──────────────────────────────────────────────┐
        │                                                │
        ▼                                                │
  ┌───────────┐  写入新数据(Sample)   ┌────────────┐     │
  │  rollout  │ ───────────────────▶ │ data buffer │     │
  │ (SGLang+  │                       │  (桥梁)     │     │
  │  router)  │ ◀───────────────────  │             │     │
  └───────────┘   读取最新权重          └─────┬──────┘     │
        ▲                                  │ 读取数据      │
        │ 更新权重(train→rollout, 单向)     ▼              │
        │                            ┌────────────┐       │
        └────────────────────────────│  training  │───────┘
                  同步参数            │ (Megatron) │ 训练完触发下一轮采样
                                     └────────────┘
```

关键要点（初学者最常搞错的几点）：

- **rollout 写数据、training 读数据**：rollout 把生成的新数据（包含 reward / verifier 结果）**写入** Data Buffer；training 从 Data Buffer **读取**数据来训练。
- **权重同步是单向的：training → rollout**：训练完之后，training 把更新好的参数同步给 rollout，让 rollout 用新权重去生成下一批数据。**rollout 永远不会反向修改 training 的参数。** 这就是为什么 README 把 training 描述为「train 后把参数同步到 rollout」。
- **Data Buffer 是桥梁**：它管理 prompt 初始化、自定义数据，以及 rollout 的生成方法（包括 agentic workflow 也是以同一套接口产出 sample）。没有它，rollout 和 training 就无法解耦。
- **闭环的驱动力是训练步**：每完成一次训练，就触发一次权重同步和新一轮采样，循环往复。

#### 4.2.3 源码精读

三大模块的官方定义在 README 的「Architecture Overview」章节：

[README.md:L84-L93](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/README.md#L84-L93)

> 三段「Module Descriptions」精确给出了每个模块的职责。请逐条对照：
> - **training (Megatron)**：「reads data from the Data Buffer, and synchronizes parameters to the rollout module after training」——读 buffer，训完同步权重给 rollout。
> - **rollout (SGLang + router)**：「Generates new data (including rewards/verifier outputs) and stores it in the Data Buffer」——生成数据（含 reward），写入 buffer。
> - **data buffer**：「A bridge module that manages prompt initialization, custom data, and rollout generation methods」——桥梁，管理 prompt / 自定义数据 / 生成方法。

中文版表述一致，可对照阅读：

[README_zh.md:L85-L93](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/README_zh.md#L85-L93)

> 中文版特别点明 training「训练完后将参数同步至 rollout 模块」，强化了权重同步的方向性。

#### 4.2.4 代码实践

**实践目标**：把三大模块的「读 / 写」关系彻底弄清楚，这是理解整个框架的关键。

**操作步骤**：

1. 重新读 [README.md:L88-L93](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/README.md#L88-L93) 的三段模块说明。
2. 在笔记里画一张表，列出三大模块各自**读取**什么、**写入**什么：

   | 模块 | 读取（输入） | 写入（输出） |
   | --- | --- | --- |
   | rollout | 最新权重（来自 training） | 新数据 Sample（→ data buffer） |
   | data buffer | （管理）prompt / 自定义数据 / rollout 产出 | 提供给 training 的训练数据 |
   | training | data buffer 里的数据 | 更新后的权重（→ rollout） |

3. 思考：为什么权重同步必须是 **training → rollout 单向**？如果反过来（rollout 改 training 的参数）会出什么问题？

**需要观察的现象**：你会发现三个模块形成了一个清晰的有向环：rollout → buffer → training → rollout。权重只在一个方向上流动。

**预期结果**：你能不查资料，画出这张闭环图，并标注每个箭头上的数据载体（Sample / 权重 / 指标）。这正是本讲「综合实践」要交付的成果。

> 待本地验证：本实践为「画图型实践」，不涉及命令运行。

#### 4.2.5 小练习与答案

**练习 1**：哪个模块负责把 reward / verifier 的结果写进 Data Buffer？

> **参考答案**：**rollout（SGLang + router）**。README 明确：rollout「Generates new data (including rewards/verifier outputs) and stores it in the Data Buffer」。也就是说，奖励计算的结果是和采样数据一起，由 rollout 写入 buffer 的。

**练习 2**：training 训练完之后，参数会同步给谁？这个方向能反过来吗？

> **参考答案**：同步给 **rollout** 模块，方向是 **training → rollout 单向**。不能反过来：rollout 只是推理，不更新参数；如果允许 rollout 修改 training 的参数，就无法保证训练梯度的正确性和一致性。这也是 README 把 training 描述为「synchronizes parameters to the rollout module after training」的原因。

**练习 3**：Data Buffer 在三个模块中扮演什么角色？

> **参考答案**：它是**桥梁模块**。一方面管理 prompt 初始化和自定义数据，另一方面承载 rollout 的生成方法（包括 agentic workflow），并把 rollout 产出的数据交给 training 消费。没有它，rollout 和 training 就无法解耦地协作。

---

### 4.3 模块：introducing_slime 博客（设计哲学）

#### 4.3.1 概念说明

前两个模块讲了 slime「是什么」和「怎么组成」。这个模块要回答一个更深的问题：**slime 为什么这样设计？** 理解了设计哲学，你才能预测 slime 在遇到新需求时会怎么做选择。

官方介绍博客 `docs/en/blogs/introducing_slime.md` 用三个词概括 slime 的设计目标：

- **Versatile（灵活）**：完全可定制的 rollout 接口 + 灵活的训练设置（colocated / decoupled、同步 / 异步、RL / SFT 冷启动）。
- **Performant（高性能）**：原生集成 SGLang（推理）和 Megatron-LM（训练）。
- **Maintainable（可维护）**：轻量代码库，从 Megatron 预训练到 SGLang 部署的平滑过渡。

这篇博客里最核心的一个反直觉观点是：**不应该为不同任务维护不同的 RL 框架**。社区里常见一种误区——做数学用一个框架、做多轮工具调用用另一个、做异步训练再换一个、做 agent 又换一个。slime 认为这种「每来一个新场景就 fork 一个框架」的做法是灾难性的（会导致无止境的 cherry-pick 补丁、甚至因为漏掉补丁而训练崩溃）。

slime 的解法是：**不规定你怎么构建应用，而是给你一个可注入自定义逻辑的数据生成接口。** 用 sgl-router 统一管理所有 SGLang 服务器，把数据生成做成「用户注入自定义逻辑、自由地与 SGLang 服务器交互」的接口。

#### 4.3.2 核心流程

博客用几个关键决策勾勒出 slime 的设计取舍，可以归纳成「四条主线」：

```text
主线 1：可定制性带来自由
  - 用 sgl-router 管所有 SGLang server，单一 HTTP 端点；
  - 用户在数据生成接口里注入自定义逻辑（multi-turn / tool / sandbox / verifier）；
  - 训练设置用 Ray，一个 --colocate 标志切换 colocated / decoupled；
  - 训练循环直接写在 train.py 里，不用 trainer 类包裹。

主线 2：为性能而生（SGLang-native）
  - 用 server-based 模式启动 SGLang；
  - 所有 SGLang 参数 --sglang- 前缀透传；
  - 提供 --debug-rollout-only 只跑推理的调试模式。

主线 3：为性能而生（Megatron-native）
  - 所有 Megatron 参数透传；
  - 支持 TP/PP/EP/CP 全部并行；
  - 提供 --debug-train-only 只跑训练的调试模式。

主线 4：轻量且可扩展
  - 复杂度从「框架」转移到「用户自定义 pipeline + 核心库(SGLang/Megatron)」；
  - 因此能自然延伸到 SFT、Rejection Sampling 等其他后训练流程。
```

这里要特别理解一个名词：**SGLang-native（SGLang 原生）**。它的意思是——在 slime 里用 SGLang，和单独用 SGLang 几乎没有区别，你能用上 SGLang 的全部优化。要做到这点，slime 选择了「参数透传」而不是「重新抽象一层」。

#### 4.3.3 源码精读

slime 的三大设计目标（Versatile / Performant / Maintainable）定义在博客开头：

[docs/en/blogs/introducing_slime.md:L15-L21](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/blogs/introducing_slime.md#L15-L21)

> 这一段是理解 slime 所有后续行为的「总纲」。比如「不用 trainer 类包裹、把训练循环写在 train.py」——这解释了为什么 slime 的入口是一个清晰的脚本而不是一个庞大的类层级（第 6 讲会精读 `train.py`）。

slime 对「多任务不应多框架」的反驳，以及「数据生成可注入自定义逻辑」的核心观点：

[docs/en/blogs/introducing_slime.md:L36-L45](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/blogs/introducing_slime.md#L36-L45)

> 注意几个关键设计决策都在这里：
> - 用 sgl-router 统一管理所有 SGLang server，用户只需向**单一端点**发 HTTP 请求；
> - 复杂的 agent 环境可以通过 OpenAI 兼容 API 直接与 slime 交互，**无需改动环境**，保持训练-部署一致性；
> - 用 Ray 做资源管理，一个 `--colocate` 标志切换共卡 / 分卡；
> - **没有用 trainer 类包裹代码**，而是把训练循环直接暴露在入口 `train.py` 里。

「SGLang-native」的具体含义（server-based 模式 + 参数透传 + debug 模式）：

[docs/en/blogs/introducing_slime.md:L53-L61](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/blogs/introducing_slime.md#L53-L61)

> 这一段解释了为什么 slime 把 SGLang 作为**单一** rollout backend——不是为了排他，而是为了避免「把多个推理引擎抽象成最小公共子集」从而丢掉各自的强项。这是 slime 最重要的架构取舍之一。

「轻量且可扩展」的四点总结（可定制接口 / Ray / SGLang+Megatron / 权重更新）：

[docs/en/blogs/introducing_slime.md:L90-L99](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/blogs/introducing_slime.md#L90-L99)

> slime 把复杂度从「框架内部」转移到了「用户自定义 pipeline」和「核心库（SGLang / Megatron）」，因此代码库轻量、易维护。这四点也预告了后续讲义的主线：rollout 接口（U3/U6）、Ray 编排（U2）、权重同步（U5）。

#### 4.3.4 代码实践

**实践目标**：体会「参数透传」这条设计主线，建立对 SGLang-native 的直觉。

**操作步骤**：

1. 读 [docs/en/blogs/introducing_slime.md:L53-L61](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/blogs/introducing_slime.md#L53-L61)。
2. 结合 [README.md:L162-L168](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/README.md#L162-L168) 的「Arguments Walkthrough」，回答：
   - 如果 SGLang 原生有一个参数叫 `--mem-fraction-static`，在 slime 里应该怎么传？
   - 如果 Megatron 原生有一个参数叫 `--tensor-model-parallel-size`，在 slime 里应该怎么传？
   - 这两类参数的「透传规则」有什么不同？
3. 在笔记里写下你的发现：**为什么 slime 选择「透传」而不是「重新封装一套参数」？** 提示：从「上游框架升级时 slime 要不要跟着改代码」的角度想。

**需要观察的现象**：你会看到 SGLang 参数需要加 `--sglang-` 前缀，而 Megatron 参数是「直接读取」、无需前缀。这个差别会贯穿整个参数体系（U8 第 3 讲会详细讲）。

**预期结果**：你能说出 slime 选择透传的核心收益——上游 SGLang / Megatron 升级时，slime 几乎不用改代码就能用上新优化。

> 待本地验证：本实践为「阅读 + 推理型实践」，不涉及命令运行。

#### 4.3.5 小练习与答案

**练习 1**：博客用哪三个词概括 slime 的设计目标？

> **参考答案**：**Versatile（灵活）、Performant（高性能）、Maintainable（可维护）**。详见 [docs/en/blogs/introducing_slime.md:L15-L21](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/blogs/introducing_slime.md#L15-L21)。

**练习 2**：slime 为什么反对「为每个任务维护一个独立 RL 框架」？

> **参考答案**：因为 fork 多个框架会导致无止境的补丁 cherry-pick，甚至因为漏掉补丁而训练崩溃。slime 认为问题根源在于「试图规定用户该怎么构建应用」——一旦为每种 rollout 场景定义通用模板，就必然只能满足少部分真实需求。slime 的解法是：不规定，而是提供一个可注入自定义逻辑的数据生成接口，让用户自由地与 SGLang 服务器交互。

**练习 3**：什么是「SGLang-native」？它和「支持多个推理后端」的做法有什么取舍？

> **参考答案**：SGLang-native 指「在 slime 里用 SGLang，和单独用 SGLang 几乎一样，能用上全部优化」（server-based 模式启动、`--sglang-` 前缀参数透传、`--debug-rollout-only` 调试模式）。取舍是：slime **只选 SGLang 一个 rollout backend**，而不是兼容多个推理引擎。博客指出，多 backend 框架往往要把多个引擎抽象成「最小公共子集」，反而会遮住每个 backend 的强项；slime 深度优化 SGLang，可直接用它的 serving / routing / caching / disaggregation / weight-sync 能力。

---

## 5. 综合实践

本讲的综合实践，是把三个最小模块串起来的一个小任务。

**任务**：精读 `README.md` 的「Architecture Overview」章节与 `introducing_slime.md` 博客后，完成下面三件事：

1. **画一张 slime 闭环图**：包含 training / rollout / data buffer 三个模块，标注它们之间的箭头，并在每个箭头上写明「数据载体」是什么——是 Sample（采样数据）、权重（模型参数），还是指标（metrics）。
2. **写一段约 200 字的说明**：用自己的话解释「slime 和单一训练框架（比如纯 Megatron）有什么区别」。要求必须包含：
   - slime 多出来的「采样闭环」是什么；
   - 三大模块各自**读取**和**写入**的对象分别是什么；
   - 权重同步为什么是 training → rollout 单向。
3. **一个反思问题**：如果你要训练一个「会调用搜索工具的多轮 agent」，按照 slime 的设计哲学，你应该去 fork 一个新框架，还是在统一数据流里插一段自定义逻辑？引用博客里的相关原话来支持你的判断。

**交付物**：一张闭环图（手绘或工具画均可）+ 一段 200 字文字 + 一句带引用的反思。

**评判标准**（自查）：
- 闭环图里，rollout → buffer 的箭头标的是「Sample（含 reward）」；
- buffer → training 的箭头标的是「训练数据」；
- training → rollout 的箭头标的是「权重」，且是单向；
- 文字里能说清「纯 Megatron 只管训，没有实时采样闭环；slime 把采样和训练缝成闭环」。

> 待本地验证：本实践为纯阅读与画图任务，不涉及任何命令运行，无需 GPU 环境。

## 6. 本讲小结

- **slime 是什么**：一个为 RL scaling 设计的 LLM 后训练框架，提供「高性能训练」和「灵活数据生成」两大核心能力。
- **三大模块**：`training (Megatron)` 负责训练、`rollout (SGLang + router)` 负责采样生成、`data buffer` 是连接两者的桥梁。
- **闭环数据流**：rollout 写 Sample（含 reward）→ data buffer → training 读取训练 → 训练后把权重**单向**同步回 rollout。
- **核心设计哲学**：不为每种任务维护独立框架，而是在统一数据流里注入自定义逻辑；不规定用户怎么构建应用。
- **SGLang-native + 透传**：slime 只选 SGLang 作为 rollout backend，并用 `--sglang-` 前缀透传所有 SGLang 参数、直接读取 Megatron 参数，让上游升级几乎零成本。
- **生产验证**：slime 是 GLM-5.x 系列背后的 RL 框架，并支持 Qwen / DeepSeek V3 / Llama 3 等，验证的是完整闭环而非孤立 demo。

## 7. 下一步学习建议

本讲建立了「全局认知地图」，但还没有进入任何 Python 源码。下一讲建议学习：

- **u1-l2《目录结构与代码地图》**：把 slime 仓库的顶层目录和 `slime/` 包的子模块（`ray/`、`backends/`、`rollout/`、`utils/`、`agent/`）梳理清楚，知道本讲说的「三大模块」分别对应磁盘上的哪些文件夹。这是读源码前的导航准备。

在进入第 2 讲之前，建议你再做一件事：**把本讲的闭环图贴在显眼处**。后面每一讲（无论讲 rollout、training 还是 weight sync）都可以对照这张图，问自己「这一讲的内容对应图上的哪一段」，这样就不会在大量源码里迷路。

如果想提前感受代码，也可以在学完第 2 讲（目录结构）后，跳读 `train.py` 的主循环——你会立刻看到本讲描述的「采样 → 训练 → 保存 → 同步权重 → 评估」闭环在代码里是怎么写的（这是 u1-l6 的内容）。
