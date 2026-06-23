# AI 简史与两种 AI 范式

## 1. 本讲目标

本讲是「符号 AI 与神经网络基础」单元（第 II 单元）的第一讲，承接上一单元的 `examples/01-hello-ai-world.py` 中「从数据学权重」的直觉，正式进入 AI 学科的理论框架。

学完后你应当能够：

- 说出 AI 学科的几个关键历史节点（专家系统、AI 寒冬、ImageNet、人类平价）。
- 区分**符号主义（GOFAI / 自上而下）**与**连接主义（神经网络 / 自下而上）**两种核心范式，并解释它们各自擅长与不擅长的任务。
- 用「弱 AI / 强 AI」「图灵测试」等术语准确描述 AI 系统的能力边界。
- 把本讲的范式划分对应回课程地图，知道后续每一课属于哪条路线。

> 说明：本课 `lessons/1-Intro/` 是一节**纯概念课**，没有可执行的 `.ipynb` 或 `.py`。因此本讲的「代码实践」以**源码阅读 + 写作实践**为主，这是课程本身的设计意图，并非偷懒。

## 2. 前置知识

- **算法（algorithm）**：一组明确的、可一步步执行的步骤。计算机最初被发明出来就是为了「按算法算数」。
- **权重（weight）**：在上一讲 `examples/01-hello-ai-world.py` 中，模型靠学习一个权重 \(w\) 来拟合 \( \hat{y} = w \cdot x \)。权重是「连接主义」的最小单元，本讲会把它放大成一整张网络。
- **范式（paradigm）**：做一类问题的根本思路与方法论。本讲的核心就是把 AI 归纳为两种范式。
- **GOFAI**：Good Old-Fashioned AI，即「老派 AI」，指符号主义路线的昵称。
- 建议先完成上一讲（u1-l4）的 `examples` 两个脚本，建立「训练 = 从数据中调权重」的体感。

## 3. 本讲源码地图

本讲几乎全部内容都来自一节课的 README，外加它的作业文件：

| 文件 | 作用 |
| --- | --- |
| [lessons/1-Intro/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/README.md) | 本讲主源码。一节完整的 AI 导论：定义、弱/强 AI、图灵测试、两种范式、AI 简史、近年研究。 |
| [lessons/1-Intro/assignment.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/assignment.md) | 本课官方作业「Game Jam」：写一篇短文，讨论某个游戏在 AI 演进下的过去、现在与未来。 |
| [lessons/1-Intro/images/history-of-ai.png](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/images/history-of-ai.png) | README 中的「AI 简史」时间线插图，是理解历史脉络的最佳视觉入口。 |
| [README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md) | 课程总览，用于「课程地图衔接」一节，把范式对应到具体单元。 |

> 提醒：本课目录下还有一个 `translations/README.ja.md`（日文镜像），以及一组 `images/` 配图。本课程每个 README 顶部几乎都有一幅「sketchnote（手绘笔记）」，是绝佳的速览材料。

## 4. 核心概念与源码讲解

### 4.1 AI 历史脉络

#### 4.1.1 概念说明

要理解今天为什么是「神经网络的天下」，必须先看懂 AI 的兴衰周期。`1-Intro` 的 README 用一句话点题：AI 最初以**符号推理**为主，曾靠「专家系统」取得一批成功，但因为「难以规模化」而在 1970 年代跌入 **AI 寒冬（AI Winter）**；随后算力变便宜、数据变多，神经网络路线才重新崛起，并在过去十年几乎成为 AI 的代名词。

这条曲线最关键的启示是：**一种范式的兴衰，往往取决于它能不能规模化（scale）**，而不只是它「对不对」。

#### 4.1.2 核心流程

把 README 叙述的历史压缩成一条时间线：

```text
1950s  AI 学科诞生，符号推理（自上而下）占主导
  │
  ▼
~1970s 符号路线的「专家系统」取得局部成功
  │     瓶颈：从专家脑子里抽取知识、维护知识库 → 太贵、不通用
  ▼
1970s  AI Winter（AI 寒冬）：投入与产出失衡，资金退潮
  │
  ▼
算力↑ + 数据↑（尤其 ImageNet，2010 起）
  │
  ▼
2012  CNN 用于图像分类，错误率从 ~30% 降到 16.4%
2015  ResNet 在图像分类上达到人类水平
  │
  ▼
近十年  「AI」≈「神经网络」，BERT/GPT 等大模型涌现
```

README 还用「下棋程序」和「对话程序」两条具体线索，展示了**同一个任务如何从符号路线一步步迁移到神经路线**——这是理解范式更替最好的微观案例，我们在 4.2 节细讲。

#### 4.1.3 源码精读

**① 学科起点与「无法显式编程」的任务**。README 开篇先给 AI 下定义，并指出它的研究对象是那些「我们自己也说不清怎么做到」的任务（典型例子：看照片猜年龄）：

[lessons/1-Intro/README.md:L9-L21](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/README.md#L9-L21) —— 定义 AI，并给出「看照片判断年龄」这个无法显式编程的例子，作为整门课的问题动机。

> 这段直接回答了「为什么需要 AI」：因为有些任务（感知、识别、翻译）我们做得到、却写不出步骤。

**② AI 简史与 AI 寒冬**。这是本模块最核心的一段：

[lessons/1-Intro/README.md:L93-L101](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/README.md#L93-L101) —— 讲符号推理为何先成功后衰退、引出 AI Winter，并说明「算力+数据」如何让神经网络反超，最终使 AI 几乎成了神经网络的同义词。

**③ 配套时间线插图**。文字旁边有一张 `history-of-ai.png`，把上述脉络画成一条时间轴，强烈建议打开对照阅读：

[lessons/1-Intro/README.md:L97-L99](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/README.md#L97-L99) —— 「Brief History of AI」配图，是历史脉络的视觉总纲。

**④ 近年研究的人类平价里程碑**。README 用一张表总结神经网络在哪里追上了人类：

[lessons/1-Intro/README.md:L135-L142](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/README.md#L135-L142) —— 列出 2015 图像分类、2016 语音识别、2018 机器翻译、2020 图像描述等「达到人类水平」的年份，并以 BERT/GPT-3 收尾，预告后续 NLP 单元。

#### 4.1.4 代码实践（源码阅读型）

本课无可执行代码，故采用**源码阅读 + 整理**实践。

1. **实践目标**：把 README 里散落的历史信息，整理成一张属于自己的「AI 大事年表」。
2. **操作步骤**：
   - 打开 [lessons/1-Intro/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/README.md)。
   - 重点阅读 L93–L101（简史）、L121–L142（近年研究）。
   - 再打开插图 [images/history-of-ai.png](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/images/history-of-ai.png)。
   - 用表格整理出「年份 / 事件 / 属于哪种范式（符号 or 神经）」三列。
3. **需要观察的现象**：你会发现 1970s 前的事件多归「符号」，2010 后的事件几乎全归「神经」，中间有一段空白（寒冬）。
4. **预期结果**：得到一张 6–8 行的年表，并能用一句话解释「为什么中间会有空白」。
5. 待本地验证：图片中的具体年份标注以你本地打开 `history-of-ai.png` 看到的为准。

#### 4.1.5 小练习与答案

**练习 1**：README 把哪一年附近的事件称为「AI Winter」？给出的根本原因是什么？

> **答案**：1970 年代。原因是符号路线的专家系统「难以规模化」——从专家抽取知识、在计算机里表示并维护知识库，代价太高、不实用（见 README L95）。

**练习 2**：README 说「如今 AI 几乎成了神经网络的同义词」，它给出的两个外部条件是什么？

> **答案**：算力变得更便宜（computing resources became cheaper）、可用数据变得更多（more data has become available）。二者共同让神经网络重新崛起。

**练习 3**：在「人类平价」年表里，最早达到人类水平的任务是哪一个、哪一年？

> **答案**：图像分类（Image Classification），2015 年（对应 ResNet，见 README L137）。

---

### 4.2 符号 vs 连接范式

#### 4.2.1 概念说明

这是本讲最核心的一节。README 把「让计算机像人一样智能」分成两条根本不同的路线：

- **自上而下（Top-down）/ 符号推理（Symbolic Reasoning）/ GOFAI**：模仿人的**推理过程**。先把人类知识抽取出来、用计算机能读的形式表示，再写一套推理规则让机器套用。代表是「专家系统」。
- **自下而上（Bottom-up）/ 神经网络（Neural Networks）/ 连接主义**：模仿人脑的**结构**。用大量简单单元（神经元）组成网络，靠**训练数据**让网络自己学会解决问题。

一句话区分：**符号派「教机器规则」，连接派「喂机器数据」**。

此外 README 还点了两种「其他路线」，本课程后面会专门讲：

- **涌现 / 多智能体（Emergent / Multi-agent）**：大量简单个体交互产生复杂智能行为（对应第 23 课）。
- **进化 / 遗传算法（Evolutionary / Genetic Algorithm）**：模仿自然选择的优化过程（对应第 21 课）。

#### 4.2.2 核心流程

两种范式在「如何得到一个能用的系统」上流程截然不同：

```text
【符号派 / Top-down】
  人类专家头脑中的规则
      │  （知识抽取，最难的环节）
      ▼
  知识表示（本体、规则库、概念图）
      │
      ▼
  推理引擎套用规则  →  得到结论（如医生诊断）

【连接派 / Bottom-up】
  收集大量带标注的训练数据
      │
      ▼
  构造人工神经元 y = f(Σ wᵢ·xᵢ + b)
      │  （上一讲 ŷ = w·x 是它的最简一维特例）
      ▼
  用数据反复调整权重 w、偏置 b  →  网络学会任务
```

连接派单个神经元的数学形式（README 原文说每个神经元「acts like a weighted average of its inputs」）：

\[
y = f\!\left(\sum_{i} w_i x_i + b\right)
\]

其中 \(x_i\) 是输入，\(w_i\) 是权重，\(b\) 是偏置，\(f\) 是激活函数。当只剩一个输入、去掉激活函数时，它就退化成上一讲的 \( \hat{y} = w \cdot x \)。

README 还用两个真实任务展示了「同一任务从符号迁移到神经」的过程，这是理解范式之争最直观的材料：

- **下棋**：早期靠搜索（alpha-beta 剪枝）→ 中期靠从人类棋谱学习（case-based reasoning）→ 现代靠神经网络 + 强化学习自我对弈。
- **对话程序**：早期 Eliza 靠简单语法规则改写句子 → 现代 Siri/Cortana 是「语音转文本(神经)+意图识别(神经)+显式推理」的混合体 → 未来趋向纯神经端到端（GPT/Turing-NLG）。

#### 4.2.3 源码精读

**① 两种范式的对照表（本节灵魂）**：

[lessons/1-Intro/README.md:L63-L73](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/README.md#L63-L73) —— 用一张并列表给出 Top-down（符号推理，建模推理、需知识表示）与 Bottom-up（神经网络，建模大脑、靠训练数据）的根本差异，并顺势点名「涌现」与「进化」两条支线。

**② 符号派详解**：

[lessons/1-Intro/README.md:L75-L81](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/README.md#L75-L81) —— 解释符号推理靠「知识表示 + 推理」，以医生诊断为例；并指出它最大软肋：专家自己都说不清「为什么」，有些任务（如看照片猜年龄）根本无法化为知识操作。

**③ 连接派详解**：

[lessons/1-Intro/README.md:L83-L87](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/README.md#L83-L87) —— 解释自下而上是建模最简单的「神经元」，构造人工神经网络，靠给例子来教，类比婴儿观察世界学习。

**④ 「ML 去哪了」的重要边界声明**：

[lessons/1-Intro/README.md:L89-L91](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/README.md#L89-L91) —— 明确声明**本课程不讲经典机器学习**，并指向姊妹课程 *Machine Learning for Beginners*。这是本课程「聚焦神经网络/深度学习」的关键定位，务必记住。

**⑤ 范式迁移的两个微观案例（下棋与对话）**：

[lessons/1-Intro/README.md:L103-L119](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/README.md#L103-L119) —— 用下棋程序（搜索→棋谱学习→神经+强化学习）和对话程序（Eliza→混合系统→纯神经）两个例子，展示同一任务如何随时间从符号范式迁移到神经范式。

> 这一段非常重要：它把抽象的「范式之争」落到了你能看懂的具体演进上。后续单元里你遇到的每一个模型，都可以追问一句「它的前一代符号版本长什么样？」

#### 4.2.4 代码实践（写作型，对齐官方作业思路）

本课的官方作业是「Game Jam」（写一篇游戏受 AI 演进影响的短文）。下面给一个更聚焦「范式对比」的写作实践，二者可合并完成。

1. **实践目标**：用自己的话讲清「符号 AI 与神经网络各自擅长什么任务」，并对照 README 的案例。
2. **操作步骤**：
   - 重读 [README.md:L75-L87](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/README.md#L75-L87)（两种范式详解）。
   - 写一段 150–250 字的对比，**必须**各举一个任务作为例子：符号派擅长什么（提示：规则清晰、可解释，如医生诊断）、神经网络擅长什么（提示：感知类、说不清步骤的，如看照片猜年龄、图像分类）。
   - 进阶：挑一个你熟悉的游戏（象棋 / 围棋 / Pac-Man），参考 [README.md:L103-L107](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/README.md#L103-L107) 的三阶段框架，写出它的「符号期 → 过渡期 → 神经期」——这正好就是官方 `assignment.md` 要求的 Game Jam。
3. **需要观察的现象**：你会发现「规则可显式表达」的任务倾向符号派，「靠感觉/经验」的任务倾向神经网络。
4. **预期结果**：产出一段对比文字（+ 可选的游戏短文），能清楚说出两种范式的「擅长领域」与「软肋」。
5. 待本地验证：写作类实践无固定运行结果，以你能否自圆其说为准。

> 与官方作业的关系：[lessons/1-Intro/assignment.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/assignment.md) 的「Game Jam」要求讨论某游戏在过去、现在、AI 未来三个时期的变化——这本质上就是在复述 4.2 节的「范式迁移」。完成上面的进阶步骤即等于完成官方作业。

#### 4.2.5 小练习与答案

**练习 1**：用一句话分别概括符号派和连接派「教机器」的方式。

> **答案**：符号派把人类专家的规则显式地写进机器（知识表示 + 推理）；连接派构造神经元网络，靠喂训练数据让网络自己调权重学会任务。

**练习 2**：README 说符号派「最难的环节」是什么？为什么？

> **答案**：从人类专家头脑中抽取知识（knowledge extraction）。因为很多专家自己也说不清为什么得出某个结论——解决方案常常是「直接冒出来」的，无法显式表达（见 README L81）。

**练习 3**：下棋程序的演进依次经历了哪三种技术路线？

> **答案**：① 基于搜索（alpha-beta 剪枝）；② 基于人类棋谱的 case-based reasoning；③ 神经网络 + 强化学习自我对弈（见 README L105–L107）。

**练习 4**：本课程**不**讲哪一部分 AI？它指向了哪个姊妹课程？

> **答案**：不讲经典机器学习（classical machine learning），指向 *Machine Learning for Beginners*（aka.ms/ml-beginners，见 README L89–L91）。

---

### 4.3 课程地图衔接

#### 4.3.1 概念说明

学完两种范式后，一个自然的问题是：**课程后面的每一课，分别属于哪条路线？** 本模块帮你把抽象范式映射到具体单元，建立「全局导航」。结论很简单：本课程**以连接主义为主线**（神经网络 → CNN → RNN → Transformer → 大模型），符号主义只在第 2 课集中出现一次，另有多智能体、遗传算法两条「其他路线」支线。

#### 4.3.2 核心流程

把范式对应到课程单元的速查表（单元编号沿用 README 的罗马数字 / 目录前缀）：

| 课程单元 | 主题 | 主要范式 |
| --- | --- | --- |
| I · 1-Intro | AI 导论（**本讲**） | 总览：符号 + 连接 |
| I · 2-Symbolic | 知识表示与专家系统 | **符号主义**（唯一集中的一课） |
| II · 3~5-NeuralNetworks | 感知机→自研框架→PyTorch/Keras | **连接主义** |
| III · 4-ComputerVision | CNN/迁移学习/GAN/检测/分割 | **连接主义** |
| IV · 5-NLP | 词向量/RNN/Transformer/BERT/GPT | **连接主义** |
| V · 6-Other | 遗传算法 / 深度强化学习 / 多智能体 | 进化 + 强化学习 + **涌现/多智能体** |
| VI · 7-Ethics | AI 伦理 | 跨范式 |
| 附加 · X-Extras | 多模态 CLIP | **连接主义** |

一句话记忆：**「第 2 课讲符号，其余几乎全是神经网络；第 6 单元是三支奇兵。」**

#### 4.3.3 源码精读

README 在「近年研究」一节里，已经埋下了通往后续 CV、NLP 单元的两条超链接，正好印证「连接主义是主线」：

[lessons/1-Intro/README.md:L129-L142](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/README.md#L129-L142) —— 提到 2012 年 CNN 用于图像分类、2015 年 ResNet 达到人类水平，并链接到后续的计算机视觉单元；结尾以 BERT/GPT-3 收束，链接到 NLP 单元。这两条超链接就是「连接主义主线」在课程结构上的具象化。

同时在「其他路线」处，README 也预先点名了第 6 单元的两门支线课：

[lessons/1-Intro/README.md:L67-L73](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/README.md#L67-L73) —— 点名「涌现/多智能体」与「进化/遗传算法」两种支线，并说明「后续会讲，本课先聚焦 top-down 与 bottom-up 两大主线」。这正好预告了第 21、23 课。

> 想看完整单元划分，回到课程总览 [README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md) 的课程内容表（上一讲 u1-l1 已详细讲过）。

#### 4.3.4 代码实践（导航型）

1. **实践目标**：亲手把本课的范式划分「画」到课程地图上，形成一张可长期使用的导航图。
2. **操作步骤**：
   - 打开课程总览 [README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md) 的课程表。
   - 用三种颜色给每个单元标注：红=符号、蓝=连接、绿=其他（进化/强化/多智能体）。
   - 在每个单元旁写一句「它对应 1-Intro 里的哪个概念」（例如 7-ConvNets 对应 L129 的 CNN；21-GeneticAlgorithms 对应 L71 的进化路线）。
3. **需要观察的现象**：你会直观看到「蓝色（连接）」几乎铺满整张图。
4. **预期结果**：得到一张三色标注的课程地图，作为后续学习的索引页。
5. 待本地验证：无运行结果，以地图清晰可用为准。

#### 4.3.5 小练习与答案

**练习 1**：课程里「唯一」集中讲符号主义的单元是哪一个？

> **答案**：第 I 单元下的 **2-Symbolic**（知识表示与专家系统）。其余单元以连接主义为主。

**练习 2**：README 在「近年研究」里，分别用哪两个模型/架构代表 CV 与 NLP 的连接主义突破？

> **答案**：CV 是 **CNN/ResNet**（2012/2015，见 L129）；NLP 是 **BERT / GPT-3**（见 L142）。

**练习 3**：README「其他路线」点名的两个支线，分别对应课程的哪两课？

> **答案**：「进化/遗传算法」对应 **21-GeneticAlgorithms**；「涌现/多智能体」对应 **23-MultiagentSystems**（见 L67–L73）。

## 5. 综合实践

**任务：写一篇「范式之争」微型报告，并给出你自己的判断。**

把本讲三个模块串起来，完成下面这份不超过 400 字的小报告：

1. **历史**：用 4–5 个时间节点，复述 AI 从符号主导到神经网络主导的兴衰（依据 4.1，引用 README L93–L101）。
2. **范式**：用一组的对照，说明符号派与连接派「教机器」的方式差异，各举一个 README 中的任务例子（依据 4.2，引用 L75–L87）。
3. **迁移**：挑一个 README 提到的任务（下棋或对话程序），写出它从符号到神经的三阶段演变（依据 L103–L119）。
4. **地图**：用一句话说明这门课为什么「几乎全是神经网络」，并指出唯一的符号主义单元（依据 4.3）。
5. **你的判断**：基于以上，你认为未来 5 年，符号主义会彻底消失，还是会与神经网络融合？给出一条理由。

> 评估标准：能否准确引用 README 真实内容、能否区分两种范式、能否把历史—范式—地图三者打通。完成后，你就为下一讲（u2-l2 符号 AI：知识表示与专家系统）做好了准备。

## 6. 本讲小结

- AI 学科兴起于符号推理，因专家系统「难规模化」而在 1970s 陷入 AI 寒冬，随后靠「算力+数据」由神经网络重新主导。
- 两大范式：**自上而下=符号推理（GOFAI）**，靠知识表示与推理；**自下而上=神经网络（连接主义）**，靠训练数据调权重。
- 单个神经元 \( y = f(\sum w_i x_i + b) \) 是连接主义的最小单元，上一讲的 \( \hat{y}=w\cdot x \) 是它的一维退化。
- 下棋、对话等任务的演进，是观察「范式迁移」的最佳案例：搜索/规则 → 混合 → 纯神经。
- 本课程明确**不讲经典 ML**（指向 ML-for-Beginners），且**以连接主义为主线**，唯一集中讲符号的是第 2 课。
- 两条支线（遗传算法、多智能体）在第 6 单元展开；CV/NLP 突破由 CNN/ResNet 与 BERT/GPT 代表。

## 7. 下一步学习建议

- **下一讲 u2-l2（符号 AI：知识表示与专家系统）**：本讲只讲了符号派「是什么」，下一讲会用 `lessons/2-Symbolic/` 的 `FamilyOntology.ipynb`、`Animals.ipynb` 真正跑一次规则推理，是符号路线唯一的动手课，不要跳过。
- **横向对比**：学完 u2-l2 后，建议立刻跳到 u2-l3（感知机），亲手实现一个最简神经元——这样你能在同一周内「同时摸到两种范式」，体感最强。
- **延伸阅读**：README 的「Review & Self Study」指向姊妹课程 ML-For-Beginners 的 [history-of-ML](https://github.com/microsoft/ML-For-Beginners/tree/main/1-Introduction/2-history-of-ML) 一课，可作为本讲历史的补充视角。
- **源码建议继续阅读**：[lessons/1-Intro/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/README.md) 的「弱 AI vs 强 AI」(L25–L34) 与「图灵测试」(L35–L53) 两节本讲未展开，建议自行阅读，建立完整概念地图。
