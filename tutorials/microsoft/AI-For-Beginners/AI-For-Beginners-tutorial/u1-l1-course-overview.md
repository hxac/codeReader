# 课程定位与学习地图

## 1. 本讲目标

本讲是整套学习手册的**第一篇**。读完本讲，你应当能够：

- 用一句话说清楚 **AI-For-Beginners** 这个仓库到底是什么、是给谁用的。
- 看懂 `README.md` 中那张「12 周 24 课」的课程内容表（Content 表），并知道每个编号大致对应哪个 AI 主题。
- 说出本课程**涵盖**哪些技术（TensorFlow / PyTorch / Keras 等）与主题，以及它**刻意不涵盖**哪些内容（比如经典机器学习、云框架）。
- 拿着 `etc/Mindmap.md` 这张思维导图，给自己规划一条个性化的学习路线。

本讲不写任何复杂代码，目标是**建立全局认知**。后面的每一篇讲义都会对应课程里的某一课，在动手之前，先有一张「地图」非常重要。

## 2. 前置知识

这是第 1 篇讲义，**几乎没有前置知识要求**。你只需要：

- 会用浏览器打开网页、点击链接。
- 大致知道「仓库（repository）」「Markdown 文档」是什么——就算不知道也没关系，把它当成一份带格式的说明书即可。
- 对「人工智能（AI）」「神经网络」这些词有最模糊的印象就够了，本课程本来就是从零讲起。

> 小贴士：本仓库的核心「源码」其实不是传统意义上的程序，而是**课程讲义（Markdown）+ 可执行的 Jupyter Notebook**。读这份课程，就像在读一本「能跑起来的教科书」。

## 3. 本讲源码地图

本讲只看仓库里三个最顶层的「导览型」文件，它们共同构成了课程的总入口：

| 文件 | 作用 | 在本讲中的角色 |
| :-- | :-- | :-- |
| `README.md` | 仓库首页文档，包含课程介绍、**课程内容表**、运行方式、社区链接 | 我们主要读懂它的「课程是什么」和「课程内容表」 |
| `AGENTS.md` | 面向开发者/贡献者（包括 AI 编程助手）的项目说明，汇总定位、技术栈、环境、目录结构 | 帮我们快速确认**技术栈**和**目录组织** |
| `etc/Mindmap.md` | 课程思维导图（纯文本大纲形式），把所有课程按主题串成树状结构 | 帮我们建立**全局学习地图** |

> 这三个文件都不需要任何运行环境，用任何编辑器或 GitHub 网页直接阅读即可。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**课程定位与目标读者**、**12 周 24 课内容表**、**技术栈与覆盖范围**。

### 4.1 课程定位与目标读者

#### 4.1.1 概念说明

在正式学习之前，先回答一个最朴素的问题：**这个仓库到底是什么？**

很多开源仓库是「软件项目」（一个能跑的程序），但 **AI-For-Beginners** 是一个**课程仓库（curriculum repository）**——它是微软出品的一套**面向初学者的免费 AI 课程**，内容以 Markdown 讲义和可运行的 Jupyter Notebook 形式组织。它的「产品」是**知识本身**，而不是某个可部署的软件。

这一点非常关键，因为它决定了我们后面「读源码」的方式：我们读的「源码」很大一部分是**教学文档和示例代码**，而不是生产级的业务逻辑。

**目标读者**是 AI 初学者。课程整体定位为「beginner-friendly」（对初学者友好），从最基础的「什么是 AI」讲起，一路讲到 Transformer、GAN 等较现代的主题。

#### 4.1.2 核心流程

理解一个课程仓库的「定位」，可以遵循这个小流程：

1. 看**标题与开场白**：课程叫什么、一句话定位是什么。
2. 看**「你会学到什么 / 你不会学到什么」**：明确边界。
3. 看**目录/内容表**：确认它的实际范围与自己的预期是否一致。
4. 结合**思维导图**与**贡献者说明**，交叉验证定位是否准确。

#### 4.1.3 源码精读

仓库的标题和开场白写在 `README.md` 最上方：

- [README.md:15](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L15) —— 标题 `# Artificial Intelligence for Beginners - A Curriculum`，直接点明这是「给初学者的 AI 课程」。
- [README.md:21](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L21) —— 一句话定位：「Explore the world of Artificial Intelligence (AI) with our **12-week, 24-lesson** curriculum! … The curriculum is **beginner-friendly** and covers tools like TensorFlow and PyTorch, as well as ethics in AI」。这里同时给出了三个关键信息：**12 周、24 课、对初学者友好**。

`AGENTS.md`（贡献者/AI 助手看的项目说明）对定位做了更结构化的复述，可以和 README 互相印证：

- [AGENTS.md:5](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L5) —— 「AI for Beginners is a comprehensive **12-week, 24-lesson curriculum** covering Artificial Intelligence fundamentals. … practical lessons using Jupyter Notebooks, quizzes, and hands-on labs.」明确这是「课程 + 测验 + 动手实验」的组合。
- [AGENTS.md:16](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L16) —— 「Architecture: **Educational content repository** with Jupyter Notebooks organized by topic areas, supplemented by a Vue.js-based quiz application …」。这句非常关键：它告诉我们仓库的「架构」是**按主题组织的教学内容**，外加一个用 Vue.js 写的测验应用。
- [AGENTS.md:284](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L284) —— 「Estimated time: **12 weeks at 2 lessons per week**」，解释了「12 周 24 课」的由来：每周 2 课。

把这些信息拼起来，课程定位就非常清晰了：**一套 12 周、每周 2 课、共 24 课、对初学者友好的 AI 课程，主体是 Notebook 教学，外加测验应用和多语言支持。**

#### 4.1.4 代码实践

> 本模块的「实践」偏阅读与观察，目的是让你亲手确认上面的定位。

1. **实践目标**：亲眼在源码里确认「12 周 24 课、初学者友好」这句话的出处，而不是只听我转述。
2. **操作步骤**：
   - 在 GitHub 网页打开仓库首页，找到 `README.md`。
   - 定位到第 15 行的标题和第 21 行的开场白。
   - 再打开 `AGENTS.md`，阅读第 3–16 行的 *Project Overview* 小节。
3. **需要观察的现象**：两份文档对「课程是什么」的描述是否一致；它们各自强调的重点有什么不同（README 面向学习者，AGENTS.md 面向开发者）。
4. **预期结果**：你会发现两份文档都强调「12 周 24 课、初学者友好、用 Notebook 教学」，但 `AGENTS.md` 额外给出了目录结构、构建命令等技术细节。
5. 若无法访问网络，可在本地用任意编辑器打开这两个文件，对照上面的行号阅读。

#### 4.1.5 小练习与答案

**练习 1**：仓库里 `AGENTS.md` 主要是写给谁看的？它和 `README.md` 的侧重点有何不同？

> **参考答案**：`AGENTS.md` 主要写给**贡献者和 AI 编程助手**看，侧重技术栈、环境搭建、目录结构、构建与测试方式等「如何维护这个项目」的信息；`README.md` 写给**学习者**看，侧重课程内容表、如何开始学习、社区链接等。两者描述的是同一个项目，只是视角不同。

**练习 2**：为什么说这个仓库的「源码」和普通软件项目不太一样？

> **参考答案**：因为它的核心产物是**教学内容**（Markdown 讲义 + 可执行 Jupyter Notebook），而不是某个可部署的程序。所以我们「读源码」很多时候是在读教学文档和示例代码，外加一个用 Vue.js 写的测验应用。

---

### 4.2 12 周 24 课内容表

#### 4.2.1 概念说明

课程的全部内容被组织在 `README.md` 的 **Content（内容表）** 中。这张表是整个仓库的「目录页」，理解了它，你就掌握了课程的全貌。

内容表有两大特点：

1. **按「大单元（罗马数字）+ 具体课程（阿拉伯数字）」两级编号**。罗马数字（I、II、III…）代表一个大主题单元，阿拉伯数字（01、02…25）代表单元下的具体一课。
2. **每课通常配有三类资源**：Lesson Link（讲义）、PyTorch/Keras/TensorFlow（Notebook 代码）、Lab（动手实验）。

> 注意一个小细节：编号里有 `IX` 这个罗马数字（对应 Extras 附加内容），这是原作者的编号习惯，不影响理解。

#### 4.2.2 核心流程

读懂内容表，可以按这个顺序：

1. 先看**罗马数字大单元**，建立「7 大主题板块」的印象。
2. 再看每个单元下的**阿拉伯数字课程**，知道每课叫什么。
3. 看每行最右侧是否有 **Lab** 链接——有 Lab 的课意味着有动手作业，通常更值得重点投入。
4. 把这张表和 `etc/Mindmap.md` 对照，确认主题归类一致。

下面这张表是依据 README 内容表（[README.md:81-118](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L81-L118)）整理的**主题地图**，方便你一眼看清结构：

| 大单元（罗马数字） | 主题 | 课程编号与名称 | 对应目录 |
| :-- | :-- | :-- | :-- |
| 0 | 课程准备 | Course Setup | `lessons/0-course-setup/` |
| I | AI 导论 | 01 Introduction and History of AI | `lessons/1-Intro/` |
| II | 符号 AI | 02 Knowledge Representation & Expert Systems | `lessons/2-Symbolic/` |
| III | 神经网络基础 | 03 Perceptron / 04 OwnFramework / 05 Frameworks | `lessons/3-NeuralNetworks/` |
| IV | 计算机视觉 | 06 IntroCV / 07 ConvNets / 08 TransferLearning / 09 Autoencoders / 10 GANs / 11 ObjectDetection / 12 Segmentation | `lessons/4-ComputerVision/` |
| V | 自然语言处理 | 13 TextRep / 14 Embeddings / 15 LanguageModeling / 16 RNN / 17 GenerativeNetworks / 18 Transformers / 19 NER / 20 LangModels | `lessons/5-NLP/` |
| VI | 其他 AI 技术 | 21 GeneticAlgorithms / 22 DeepRL / 23 MultiagentSystems | `lessons/6-Other/` |
| VII | AI 伦理 | 24 AI Ethics | `lessons/7-Ethics/` |
| IX | 附加内容 | 25 MultiModal (CLIP/VQGAN) | `lessons/X-Extras/` |

> 一个观察：编号目录名（如 `3-NeuralNetworks`、`4-ComputerVision`）和上面的大单元**一一对应**，目录名前面的数字就代表学习顺序。这说明仓库的目录结构本身就是课程顺序的体现。

#### 4.2.3 源码精读

内容表的起点在 `README.md`：

- [README.md:81](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L81) —— 一级标题 `# Content`，标志着内容表开始。
- [README.md:82-84](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L82-L84) —— 内容表的表头：四列分别是序号、Lesson Link（讲义）、PyTorch/Keras/TensorFlow（Notebook）、Lab（实验）。
- [README.md:86-87](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L86-L87) —— 第 I 大单元「Introduction to AI」及其下的第 01 课。
- [README.md:90-93](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L90-L93) —— 第 III 大单元「神经网络」，含 03 Perceptron、04 OwnFramework、05 Frameworks 三课，并且每课都带有 Notebook 与 Lab 链接——这就是「为什么说本课程很注重动手」的直观证据。
- [README.md:94-101](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L94-L101) —— 第 IV 大单元「计算机视觉」，从 06 IntroCV 一直到 12 Segmentation，是全课程**篇幅最大**的板块。
- [README.md:102-110](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L102-L110) —— 第 V 大单元「自然语言处理」，从 13 文本表示一路到 20 大语言模型，覆盖了从词袋到 GPT 的完整脉络。
- [README.md:115-118](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L115-L118) —— 伦理（VII / 24）与附加内容（IX / 25 MultiModal）。

每课都「包含什么」，在内容表之后有专门说明：

- [README.md:120-125](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L120-L125) —— 「Each lesson contains」列出一课的标配：前置阅读材料、可执行的 Jupyter Notebook（常分 PyTorch / TensorFlow 两版）、部分主题的 Lab、以及指向 MS Learn 的拓展模块。**关键提示**：作者强调 Notebook 本身也包含大量理论，所以「理解一个主题至少要完整跑完一版 Notebook」。

课程还特别为零基础读者准备了「最简单的入门示例」，不依赖深度学习框架：

- [README.md:129-138](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L129-L138) —— 「🎯 New to AI? Start Here!」，指向 `examples/` 目录里的 Hello AI World、Simple Neural Network、Image Classifier、Text Sentiment 等极简示例。这是全课程**最友好的起点**（本手册第 4 篇 `u1-l4` 会专门讲它）。

#### 4.2.4 代码实践

1. **实践目标**：把 README 的内容表变成**属于你自己的学习清单**。
2. **操作步骤**：
   - 打开 `README.md` 的 Content 表（第 81–118 行）。
   - 逐行浏览，对照上面那张「主题地图」表，把 24 课在脑海里归到 7 大板块。
   - 用自己的话写一段 **100 字以内** 的课程简介。
   - 在内容表里圈出**你最想学的 3 个单元**（可以按编号或主题名记录），并各写一句「为什么想学」。
3. **需要观察的现象**：哪些课带有 Lab（动手实验），哪些没有；计算机视觉和 NLP 两个板块各有多少课。
4. **预期结果**：你得到一份个性化的「前三优先级」学习清单，例如「我最想学：07 ConvNets（想理解 CNN）、18 Transformers（想懂大模型原理）、24 AI Ethics（想了解负责任的 AI）」。
5. 简介示例（仅供参考，请用自己的话改写）：「这是微软出品的 12 周 24 课 AI 课程，从符号 AI、神经网络讲到计算机视觉、NLP 与 AI 伦理，配合可运行 Notebook 与动手实验，适合零基础入门。」

> 这是本讲的**主实践任务**，它贯穿了「读懂内容表 → 建立全局观 → 制定个人路线」这一完整过程。

#### 4.2.5 小练习与答案

**练习 1**：内容表里，第 III 大单元「神经网络」包含哪三课？它们之间是什么递进关系？

> **参考答案**：包含 03 Perceptron（感知机）、04 OwnFramework（自研迷你框架）、05 Frameworks（PyTorch/TensorFlow 工业框架）。递进关系是：先用最简单的单个神经元入门 → 再用 NumPy 从零搭建自己的框架理解原理 → 最后过渡到工业级框架并讨论过拟合。

**练习 2**：如果你完全零基础、只想先「玩一下」AI，README 推荐你从哪里开始？

> **参考答案**：README 第 129–138 行的「🎯 New to AI? Start Here!」推荐先看 `examples/` 目录里的极简示例（如 Hello AI World、Simple Neural Network），这些示例不依赖深度学习框架，适合在进入正式课程前建立直觉。

---

### 4.3 技术栈与覆盖范围

#### 4.3.1 概念说明

知道「课程讲什么」之后，还要知道「**用什么工具讲**」以及「**讲到什么程度 / 不讲什么**」。这就是本模块要解决的两个问题：

- **技术栈**：课程用到的编程语言、深度学习框架、配套工具。
- **覆盖范围**：课程**刻意不涵盖**哪些内容——这一点和「涵盖什么」同样重要，因为它帮你判断本课程是否适合你、以及哪些主题需要另找资料补齐。

#### 4.3.2 核心流程

梳理技术栈与边界，可以这样做：

1. 从 `AGENTS.md` 的 *Key Technologies* 直接拿到「官方技术栈清单」。
2. 从 `README.md` 的 *What you will learn / What we will not cover* 拿到「学 / 不学」的边界。
3. 把两者对照，得出一张「学什么 + 用什么 + 不学什么」的清单，作为后续选课依据。

#### 4.3.3 源码精读

**技术栈**最权威的一句话来自 `AGENTS.md`：

- [AGENTS.md:14](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L14) —— 「**Key Technologies**: Python 3, Jupyter Notebooks, TensorFlow, PyTorch, Keras, OpenCV, Vue.js (for quiz app)」。一句话点明：主力语言是 **Python 3**，教学载体是 **Jupyter Notebook**，深度学习用 **TensorFlow + PyTorch + Keras**（很多课同时提供两版 Notebook），图像处理用 **OpenCV**，测验应用用 **Vue.js**。

更细的依赖版本在 `AGENTS.md` 的环境配置一节：

- [AGENTS.md:217-229](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L217-L229) —— 列出核心依赖：`tensorflow==2.17.0`、`torch`/`torchvision`（经 conda 安装）、`keras==3.5.0`、`opencv`、`scikit-learn`、`numpy==1.26`、`pandas==2.2.2`、`matplotlib==3.9`、`jupyter`。这进一步印证「双框架并行」的教学风格。

**覆盖范围**写在 `README.md` 的 *What you will learn* / *What we will not cover* 两个小节：

- [README.md:57](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L57) —— 标题 `## What you will learn`，下面列出本课程**会教**的内容。
- [README.md:63](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L63) —— 第一条：不同的 AI 路径，包括「经典符号主义」方法（GOFAI），即知识表示与推理。
- [README.md:64](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L64) —— 第二条：**神经网络与深度学习**，这是现代 AI 的核心，用 TensorFlow 和 PyTorch 两个框架讲解。
- [README.md:65](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L65) —— 第三条：处理**图像和文本**的神经网络架构（计算机视觉 + NLP），但坦承「可能略落后于最前沿（state-of-the-art）」。
- [README.md:66](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L66) —— 第四条：较冷门的 AI 方法，如**遗传算法**和**多智能体系统**。

**不涵盖的内容**同样重要：

- [README.md:68](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L68) —— 标题 `What we will not cover in this curriculum:`，下面列出本课程**不教**的内容，并贴心地给出了替代学习资源。
- [README.md:73](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L73) —— 明确指出**经典机器学习**不在本课程范围，并推荐姊妹课程 `ML-for-Beginners`。这是一个非常重要的边界：本课程聚焦**深度学习 / 神经网络**，传统 ML（决策树、SVM 等）需要另学。
- [README.md:74-76](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L74-L76) —— 还不涵盖：基于认知服务的实战应用、具体云框架（Azure ML 等）、对话式 AI / 聊天机器人。
- [README.md:77](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L77) —— 也不涵盖深度学习背后的**深层数学**，并推荐 Goodfellow 的《Deep Learning》经典教材。

把上面这些信息汇总，技术栈与范围就一目了然了：

| 维度 | 本课程的情况 |
| :-- | :-- |
| 编程语言 | Python 3 |
| 教学载体 | Jupyter Notebook（每课常分 PyTorch / TensorFlow 两版） |
| 深度学习框架 | TensorFlow、PyTorch、Keras |
| 图像处理 | OpenCV |
| 测验应用 | Vue.js |
| **会教** | 符号 AI、神经网络/深度学习、计算机视觉、NLP、遗传算法、多智能体、强化学习、AI 伦理 |
| **不教** | 经典机器学习（→ ML-for-Beginners）、认知服务实战、云框架、聊天机器人、深层数学 |

#### 4.3.4 代码实践

1. **实践目标**：根据技术栈与边界，判断「这门课是否适合现在的我」，并找出需要补的前置/外围资源。
2. **操作步骤**：
   - 打开 `AGENTS.md` 第 14 行，记录官方技术栈。
   - 打开 `README.md` 第 57–77 行，分别列出「会学」和「不会学」的内容。
   - 对照自己的背景打勾：Python 基础是否具备？是否接受「双框架并行」？对「不教的经典 ML」是否需要另补？
3. **需要观察的现象**：课程几乎每课都同时给 PyTorch 和 TensorFlow 两版 Notebook；以及「不涵盖」清单里推荐的外部资源（如 ML-for-Beginners、《Deep Learning》）。
4. **预期结果**：得到一张「我的准备度清单」，例如：「Python 基础已具备 ✓；优先跟 PyTorch 版 Notebook；经典 ML 暂不补，后续需要时再学 ML-for-Beginners」。
5. 如果你对某个「不教」的主题（如经典 ML）确实有需求，把 README 第 73 行给的替代资源链接记下来，作为后续学习计划的一部分。

#### 4.3.5 小练习与答案

**练习 1**：本课程在深度学习上「双框架并行」是什么意思？为什么要这样做？

> **参考答案**：指很多课程的 Notebook 同时提供 **PyTorch** 和 **TensorFlow/Keras** 两个版本。这样做的目的是让读者可以**任选自己熟悉或偏好的框架**学习；作者也强调，要理解一个主题，至少完整跑通其中一版即可，不必两版都做。

**练习 2**：一个想学「传统决策树、支持向量机」的同学，本课程适合他吗？为什么？

> **参考答案**：不完全适合。本课程聚焦神经网络/深度学习，**经典机器学习被明确列为「不涵盖」**（README 第 73 行），并推荐改学姊妹课程 `ML-for-Beginners`。不过如果他同时也想建立深度学习基础，本课程仍是很好的选择。

---

## 5. 综合实践

把三个模块串起来，完成下面这个**贯穿性小任务**——产出一份「我的 AI 学习路线 v1.0」。

**任务背景**：你已经读完了课程定位、内容表和技术栈，现在要像项目经理一样，给自己排一份学习计划。

**步骤**：

1. **写课程简介（对应 4.1）**：用 100 字以内概括 AI-For-Beginners 是什么。
2. **画主题地图（对应 4.2）**：参照 `etc/Mindmap.md`（[etc/Mindmap.md:1](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/Mindmap.md#L1) 起），把 7 大板块画成一张树状图，标注每板块的代表课程。
3. **选定前 3 优先级（对应 4.2）**：从内容表中选出你最想学的 3 个单元，各写一句理由。
4. **评估准备度（对应 4.3）**：对照技术栈清单，列出你已经具备的技能、还需要补的技能（如 Python 基础、是否需要 GPU）。
5. **标注边界（对应 4.3）**：记录本课程「不教」但你可能需要的主题及其替代资源（如经典 ML → ML-for-Beginners）。

**交付物**：一份一页纸的 Markdown 或纯文本笔记，包含上述 5 项。

**预期结果示例（节选）**：

```text
# 我的 AI 学习路线 v1.0

## 1. 课程简介
AI-For-Beginners 是微软 12 周 24 课的初学者 AI 课程，以 Notebook 教学为主，
覆盖符号 AI、神经网络、CV、NLP、伦理等。

## 2. 主题地图
- I 导论 → II 符号AI → III 神经网络 → IV 计算机视觉 → V NLP → VI 其他 → VII 伦理

## 3. 前 3 优先级
- 07 ConvNets：想理解 CNN 原理
- 18 Transformers：想懂大模型
- 24 AI Ethics：想做负责任的 AI

## 4. 准备度
- 已具备：Python 基础
- 待补：暂无 GPU，优先选轻量实验 / Colab 运行

## 5. 边界
- 经典 ML 不在本课，需要时学 ML-for-Beginners
```

> 这个综合实践没有「标准答案」，它的价值在于逼你把读到的信息**主动重组**成自己的计划——这比单纯读完文档有效得多。

## 6. 本讲小结

- AI-For-Beginners 是微软出品的**课程仓库**（不是传统软件项目），核心产物是 Markdown 讲义 + 可执行 Jupyter Notebook，定位为「12 周、24 课、对初学者友好」。
- 仓库的「目录名编号」与 README 的「内容表编号」一一对应，**目录结构本身就是学习顺序**：从 1-Intro 到 7-Ethics，外加 X-Extras 附加内容。
- 课程分 **7 大板块**：AI 导论、符号 AI、神经网络、计算机视觉、NLP、其他 AI 技术、AI 伦理（外加多模态附加内容）；其中计算机视觉和 NLP 是篇幅最大的两块。
- 技术栈以 **Python 3 + Jupyter** 为载体，深度学习**双框架并行**（TensorFlow + PyTorch/Keras），图像处理用 OpenCV，测验应用用 Vue.js。
- 课程**聚焦神经网络/深度学习**，明确**不涵盖**经典机器学习、云框架、聊天机器人等，并贴心给出替代资源。
- `README.md`、`AGENTS.md`、`etc/Mindmap.md` 三个文件共同构成「课程总入口」，是建立全局认知的最佳起点。

## 7. 下一步学习建议

本讲建立了全局认知，接下来建议按手册顺序继续：

1. **下一篇 `u1-l2`（仓库目录结构与内容组织）**：深入 `lessons/` 目录，搞清楚每课的 README / Notebook / lab / assignment 等文件类型约定，为你「打开任意一课都能看懂结构」打基础。
2. **随后 `u1-l3`（开发环境搭建）**：按 `environment.yml` / `setup.md` 搭建 `ai4beg` conda 环境，让你能真正跑起 Notebook。
3. **然后 `u1-l4`（从 examples 开始：第一个 AI 程序）**：运行 `examples/01-hello-ai-world.py`，亲手感受「从数据中学习权重」这一 AI 核心思想——这是整个课程最温柔的起点。

> 阅读建议：在进入下一篇之前，先完成本讲的「综合实践」，确保你已经拥有一张属于自己的学习地图。后续每一篇讲义都会对应内容表里的某一课，有了这张地图，你随时知道自己身处何处。
