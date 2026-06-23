# 仓库目录结构与内容组织

## 1. 本讲目标

本讲承接 [u1-l1 课程定位与学习地图](./u1-l1-course-overview.md)，在已经知道「这是一个 12 周 24 课的 AI 课程仓库」的基础上，进一步把仓库「拆开」给你看。学完本讲，你应当能够：

- 看懂 `lessons/` 目录下「大单元 + 小课程」的两级编号规则，能根据目录名直接判断它属于哪个 AI 主题。
- 区分一节课里的不同文件类型：`README.md`（讲义）、`.ipynb`（可执行 Notebook）、`lab/`（动手实验）、`assignment.md`（写作作业），并知道它们各自的用途。
- 了解 `examples/`、`etc/`、`translations/`、`data/`、`binder/` 等顶层辅助目录分别承担什么职责，知道想找某样东西时该去哪个目录。

换句话说，本讲给你一张「仓库地图」，让你后续阅读任何一节课时都不会迷路。

## 2. 前置知识

本讲假设你已经读过 [u1-l1 课程定位与学习地图](./u1-l1-course-overview.md)，知道：

- 这个仓库的核心产物是 **Markdown 讲义** 和 **可执行 Jupyter Notebook**，而不是传统的源代码。
- 课程采用 **TensorFlow + PyTorch/Keras「双框架并行」**，每节课通常有两个框架版本的 Notebook，理解其中一个即可。

此外需要两个最基础的概念：

- **Markdown（`.md`）**：一种用纯文本写排版的格式，标题用 `#`、列表用 `-`，GitHub 会自动渲染成带样式的网页。本仓库几乎所有文字讲义都是 Markdown。
- **Jupyter Notebook（`.ipynb`）**：一种「代码 + 文字 + 运行结果」混合的文档，可以一格一格地运行 Python 代码并立刻看到输出。它是本课程「边读边跑」的主要载体。

如果你还不熟悉这两者，不用担心——本讲只讲目录约定，不会深入代码细节，具体怎么运行 Notebook 留到 [u1-l3 开发环境搭建](./u1-l3-environment-setup.md)。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md) | 仓库主入口，其中的 `# Content` 内容表是「目录结构」最权威的索引；`Each lesson contains` 段落定义了单课文件类型的官方约定。 |
| [AGENTS.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md) | 给开发/AI 助手的「项目说明书」，含一段 `File Organization` 目录树，是对仓库结构的精炼总结。 |
| [lessons/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/README.md) | `lessons/` 目录的导览页，是一张课程总览的涂鸦图（sketchnote）。 |
| `lessons/3-NeuralNetworks/03-Perceptron/` | 一节「结构最完整」的典型课，含 README、Notebook、lab 三件套，本讲用它做样例剖析。 |

> 说明：本仓库不是软件项目，没有 `src/`、`main()` 这种入口。所谓「源码」主要是 Markdown 和 Notebook，下文统称「课程文件」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **lessons 分级目录结构**——目录是怎么按主题和顺序编号的。
2. **单课文件类型约定**——一节课内部有哪些文件、各干什么。
3. **顶层辅助目录**——`lessons/` 之外那些目录的职责。

### 4.1 lessons 分级目录结构

#### 4.1.1 概念说明

`lessons/` 是整个仓库的「心脏」，所有课程内容都在这里。它采用 **两级编号** 的目录组织方式：

- **第一级（大单元）**：对应 README 内容表里的 **罗马数字大单元**（I、II、III、IV、V、VI、VII、IX），但目录名用「**阿拉伯数字 + 主题名**」表示，例如 `1-Intro`、`4-ComputerVision`。这些大单元就是课程的大主题板块。
- **第二级（小课程）**：每个大单元内部再用「**两位数字 + 主题名**」表示具体的一节课，例如 `03-Perceptron`、`07-ConvNets`。这两位数字大致对应 README 内容表里的课程序号（01–25）。

这种「数字前缀」的好处是：**文件系统里目录会自动按学习顺序排列**，你不用额外看目录就能知道该先学哪个、后学哪个。

#### 4.1.2 核心流程

把 README 内容表的大单元、罗马数字，和磁盘上的目录名对应起来：

```
README 内容表(罗马数字)   磁盘目录(数字-名称)        主题
I  Introduction to AI  →  lessons/1-Intro/          AI 导论
II Symbolic AI        →  lessons/2-Symbolic/        符号 AI
III Neural Networks    →  lessons/3-NeuralNetworks/  神经网络
IV Computer Vision     →  lessons/4-ComputerVision/  计算机视觉
V  NLP                 →  lessons/5-NLP/             自然语言处理
VI Other AI Techniques →  lessons/6-Other/           其他 AI 技术
VII AI Ethics          →  lessons/7-Ethics/          AI 伦理
IX Extras              →  lessons/X-Extras/          附加内容
```

此外有两个「特殊」目录：

- `lessons/0-course-setup/`：编号 `0`，排在所有主题之前，讲怎么搭建环境（setup.md、how-to-run.md、for-teachers.md）。
- `lessons/sketchnotes/`：存放全课程的手绘涂鸦图（sketchnote），是 README 里那些插图的来源。

第二级小课程示例（以 `4-ComputerVision` 为例）：

```
lessons/4-ComputerVision/
├── README.md            # 本大单元导览
├── 06-IntroCV/          # 第 6 课：CV 入门 + OpenCV
├── 07-ConvNets/         # 第 7 课：卷积神经网络
├── 08-TransferLearning/ # 第 8 课：迁移学习
├── 09-Autoencoders/     # 第 9 课：自编码器 / VAE
├── 10-GANs/             # 第 10 课：生成对抗网络
├── 11-ObjectDetection/  # 第 11 课：目标检测
└── 12-Segmentation/     # 第 12 课：语义分割 / U-Net
```

> 小贴士：注意 `X-Extras` 用的是字母 `X` 而不是数字，因为它是「附加」内容，不占用主干课程序号；其内部的小课程也用 `X1-MultiModal` 这种带字母前缀的命名。

#### 4.1.3 源码精读

**① README 内容表是目录结构最权威的索引。** 它用一张大表把「罗马数字大单元 + 课程序号 + 链接」全部列出，可以直接对照目录名理解：

[README.md:81-90](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L81-L90) —— 这段从 `# Content` 标题开始，表头是「Lesson Link / PyTorch/Keras/TensorFlow / Lab」三列；下面 `I`、`II`、`III` 这些行就是大单元（罗马数字），而 `01`、`02`、`03` 这些行才是具体的小课程，每行的链接路径恰好就是 `lessons/<大单元>/<小课程>/README.md`。

[README.md:86](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L86) 与 [README.md:90](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L90) —— 这两行分别展示了「大单元行」的写法：`| I | **Introduction to AI**` 和 `| III | **Introduction to Neural Networks**`，罗马数字对应到磁盘上的 `1-Intro`、`3-NeuralNetworks`。

**② AGENTS.md 有一段精炼的目录树。** 它把大单元目录一句话概括：

[AGENTS.md:135-154](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L135-L154) —— `### File Organization` 下的目录树，列出了 `0-course-setup/` 到 `X-Extras/` 八个 `lessons/` 子目录，每个都带了一句英文注释（如 `# Computer Vision`），可以当作目录含义的速查表。

**③ `lessons/README.md` 是大单元的总导览。**

[lessons/README.md:1-5](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/README.md#L1-L5) —— 内容很短，主要是一张 `ai-overview.png` 总览涂鸦图，作为进入各单元之前的「全貌图」。

#### 4.1.4 代码实践

本模块的实践是「阅读 + 画图」，不需要运行任何代码：

1. **实践目标**：把 README 的内容表和磁盘目录一一对应，建立「看到目录名就知道主题」的直觉。
2. **操作步骤**：
   - 打开 [README.md 的 Content 表](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L81-L118)，从上到下读一遍，注意罗马数字行（大单元）和数字行（小课程）的区别。
   - 在本地仓库根目录执行 `ls lessons/`，对照 README 表，确认每个目录名对应哪个罗马数字大单元。
3. **需要观察的现象**：磁盘目录的排序（`0-course-setup`、`1-Intro`、`2-Symbolic` ……）与 README 表的顺序完全一致，印证了「数字前缀 = 学习顺序」。
4. **预期结果**：你应能不查表就说出 `5-NLP` 是第 V 大单元（自然语言处理）、`X-Extras` 是附加内容。

> 本实践为源码阅读型实践，无需运行命令即可完成；如本地尚未克隆仓库，可只看 GitHub 网页上的目录结构。

#### 4.1.5 小练习与答案

**练习 1**：README 内容表里课程序号 `08` 对应的目录是哪个？它属于哪个大单元（罗马数字）？

> **答案**：`lessons/4-ComputerVision/08-TransferLearning/`，属于第 IV 大单元「Computer Vision（计算机视觉）」。

**练习 2**：为什么 `lessons/X-Extras/` 用字母 `X` 而不是数字 `8`？

> **答案**：因为它是「附加（Extras）」内容，不占用主干课程的连续编号；用 `X` 表示「非主线」，其内部小课程也用 `X1-MultiModal` 这种命名，与主干 `NN-Name` 区分开。

### 4.2 单课文件类型约定

#### 4.2.1 概念说明

进入任意一节具体的小课程（比如 `03-Perceptron`），你会发现里面不是只有一个文件，而是一组「各司其职」的文件。README 的官方约定说明了每节课包含哪些东西：

- **`README.md`**：讲义正文，包含理论讲解、公式、配图，开头通常有一个「课前测验（Pre-lecture quiz）」链接。
- **可执行 Jupyter Notebook（`*.ipynb`）**：把理论转成可运行代码，常分 **PyTorch** 和 **TensorFlow** 两个框架版本，看其中一个即可。
- **`lab/` 子目录**：动手实验，提供一个具体问题让你应用所学；内含 `lab/README.md`（任务 + 提示）和一个起始 Notebook。
- **`assignment.md`**：写作/思考型作业（比如写一篇短文），不是每节课都有。
- **`images/`**：本课用到的插图。

关键认知：**并不是每节课都集齐上述所有类型**。有的课只有 README + Notebook，有的多了 lab，有的用 assignment 代替 lab。这也是为什么 README 内容表里 `Lab` 列有的行是空的。

#### 4.2.2 核心流程

以「结构最完整」的 `03-Perceptron` 为例，一节课的典型文件组成如下：

```
lessons/3-NeuralNetworks/03-Perceptron/
├── README.md              # 讲义正文（理论 + 课前测验链接）
├── Perceptron.ipynb       # 可执行 Notebook（课堂示例代码）
├── images/                # 讲义配图（Rosenblatt 照片、激活函数图等）
└── lab/
    ├── README.md          # 实验任务说明 + 提示
    └── PerceptronMultiClass.ipynb  # 实验起始 Notebook
```

阅读一节课的推荐顺序：

1. 先读 `README.md` 建立概念（注意开头的课前测验链接，可去在线测验应用做一遍）。
2. 再打开 `.ipynb` Notebook，逐格运行代码、对照理论。
3. 若有 `lab/`，作为「实战检验」完成它；若是 `assignment.md`，作为开放式思考题。

#### 4.2.3 源码精读

**① README 官方定义了「每节课包含什么」。** 这是对文件类型最权威的说明：

[README.md:120-125](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L120-L125) —— `## Each lesson contains` 段落列出四类内容：Pre-reading material（预读）、Executable Jupyter Notebooks（可执行 Notebook，强调常分 PyTorch/TensorFlow 两个版本）、Labs（部分主题才有）、以及 MS Learn 模块链接。这段文字就是单课文件约定的「合同」。

**② 讲义 README 的开头通常有课前测验链接。**

[lessons/3-NeuralNetworks/03-Perceptron/README.md:1-5](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/README.md#L1-L5) —— 标题 `# Introduction to Neural Networks: Perceptron` 之后紧接着 `## [Pre-lecture quiz](https://ff-quizzes.netlify.app/en/ai/quiz/5)`，这就是「课前测验」入口，它指向 `etc/quiz-app` 那个测验应用（第 4.3 节会讲）。

**③ `lab/README.md` 用「Task + Hints + Starting Notebook」三段式描述实验。**

[lessons/3-NeuralNetworks/03-Perceptron/lab/README.md:1-22](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/lab/README.md#L1-L22) —— 先给 `## Task`（任务：把二分类感知机扩展成多分类），再给 `## Hints`（提示：训练 10 个感知机再用 argmax），最后 `## Starting Notebook` 指向 `PerceptronMultiClass.ipynb`。这是所有 `lab/README.md` 的通用写法。

**④ `assignment.md` 是写作型作业，与 `lab/` 互补。**

[lessons/1-Intro/assignment.md:1-3](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/assignment.md#L1-L3) —— 标题 `# Game Jam`，要求写一篇关于「受 AI 影响的游戏」的短文。可以看到它没有代码、没有起始 Notebook，纯粹是开放式思考题，风格与 `lab/` 截然不同。

**⑤ 哪些课有 lab、哪些有 assignment？** 通过 `git ls-files` 可以精确统计（数据来自当前 HEAD）：

- 有 `lab/README.md` 的课共 12 节（如 `03-Perceptron`、`07-ConvNets`、`22-DeepRL` 等），偏重代码实战。
- 有 `assignment.md` 的课共 7 节（如 `1-Intro`、`2-Symbolic`、`18-Transformers` 等），偏重思考与写作。
- 可见「lab」与「assignment」是两种互补的练习形式，并非每课都有，也并非互斥。

#### 4.2.4 代码实践

1. **实践目标**：用命令精确统计每种文件类型的分布，验证「并非每课都有 lab/assignment」。
2. **操作步骤**（在仓库根目录执行）：
   ```bash
   # 统计有多少节课配了动手实验
   git ls-files | grep "lab/README.md" | grep -v "translations/" | wc -l
   # 统计有多少节课配了写作作业
   git ls-files | grep "assignment.md" | grep -v "translations/" | wc -l
   # 看看每节小课程里都有哪些文件类型
   ls lessons/3-NeuralNetworks/03-Perceptron/
   ```
3. **需要观察的现象**：`lab/README.md` 与 `assignment.md` 的数量都明显少于 24（课程总数），说明它们是「部分课程才有」的可选项。
4. **预期结果**：`lab/README.md` 约 12 个，`assignment.md` 约 7 个；`03-Perceptron` 目录下能看到 `README.md`、`Perceptron.ipynb`、`images/`、`lab/` 四项。

> 如本地无 git 仓库，可直接在 GitHub 网页上进入各课程目录人工统计；命令中的 `grep -v "translations/"` 是为了排除多语言翻译副本的干扰（见 4.3 节）。

#### 4.2.5 小练习与答案

**练习 1**：一节课的 `README.md` 和 `.ipynb` Notebook，应该先读哪个？为什么？

> **答案**：先读 `README.md`。它提供理论框架和概念直觉，建立认知后再用 Notebook 跑代码、对照验证，效果更好；直接啃 Notebook 容易被代码细节淹没而忽略原理。

**练习 2**：`lab/README.md` 和 `assignment.md` 的主要区别是什么？

> **答案**：`lab/` 偏重**代码实战**，通常给「Task + Hints + 起始 Notebook」让你动手实现；`assignment.md` 偏重**思考与写作**，多是开放式问题（如写短文、建本体），通常不含起始代码。

### 4.3 顶层辅助目录

#### 4.3.1 概念说明

`lessons/` 之外，仓库根目录还有一组「辅助目录」，它们不直接是课程内容，但支撑着课程的运行、分发和本地化。理解它们的职责，能让你在找东西时少走弯路。

| 目录 / 文件 | 职责 |
| --- | --- |
| `examples/` | 给纯新手的「最小可运行示例」Python 脚本，无需深度学习框架即可跑。 |
| `etc/` | 工具集中地：测验应用 `quiz-app`（Vue.js）、题库生成 `quiz-src`、课程思维导图、PDF、贡献指南等。 |
| `translations/` | 50+ 种语言的翻译副本，由自动化机器人（co-op-translator）维护，结构镜像 `lessons/`。 |
| `translated_images/` | 翻译版用到的图片副本。 |
| `data/` | 课程示例数据集（如 MNIST 的 `mnist.pkl.gz`）。 |
| `binder/` | 在线云端运行环境 Binder 的配置（`apt.txt`、`environment.yml`、`postBuild.sh`）。 |
| `images/` | 仓库根级插图（README 徽章、封面等用图）。 |
| `.github/`、`.devcontainer/` | CI 工作流、VS Code 开发容器配置。 |

#### 4.3.2 核心流程

这些目录的协作关系可以这样理解：

- **学习主线**：读者主要在 `lessons/` 活动；若完全零基础，先去 `examples/` 热身。
- **测验闭环**：每节课 README 里的「课前测验」链接 → 指向 `etc/quiz-app` 这个 Vue 应用；而题库则由 `etc/quiz-src` 用脚本从纯文本生成。
- **多语言**：英文源在 `lessons/`，自动翻译结果镜像在 `translations/<语言码>/` 下（结构完全一致），配套图片在 `translated_images/`。
- **云端运行**：不想本地装环境的读者，靠 `binder/` 配置一键在浏览器里启动 Jupyter。

> 关键点：`translations/` 体积很大（50+ 语言），是仓库「下载慢」的主因。README 专门给了「稀疏克隆」命令来跳过它，这部分会在 [u6-l4 多语言翻译机制](./u6-l4-translations-i18n.md) 详细讲。

#### 4.3.3 源码精读

**① AGENTS.md 的目录树也概括了顶层辅助目录。**

[AGENTS.md:149-153](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L149-L153) —— 在 `File Organization` 树里，`etc/` 下挂了 `quiz-app/`（Vue 测验应用）和 `quiz-src/`（题库源文件），并单独列出 `translations/`（多语言翻译）。这是对顶层辅助目录最精炼的官方描述。

**② `examples/` 是面向零基础的独立入口。** README 在「Getting Started」里专门引导新手去这里：

[README.md:131-138](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L131-L138) —— `### 🎯 New to AI? Start Here!` 段落推荐先做 `examples/` 里的 4 个最小示例（Hello AI World、Simple Neural Network、Image Classifier、Text Sentiment），它们是「理解概念之后再进入完整课程」的过渡桥梁，对应讲义 [u1-l4 从 examples 开始](./u1-l4-first-ai-program.md)。实际目录里能看到 `examples/01-hello-ai-world.py` 等独立 `.py` / `.ipynb` 文件。

**③ `etc/` 承载测验与导览工具。** 它内部既有代码（`quiz-app/`、`quiz-src/`），也有文档资源：

- `etc/quiz-app/`：Vue 2 测验应用（含 `package.json`、`src/`、`public/`），对应讲义 [u6-l1 测验应用架构](./u6-l1-quiz-app.md)。
- `etc/quiz-src/`：题库源（`questions-en.txt` + `qzmkjson.py` 生成脚本），对应讲义 [u6-l2 测验生成流水线](./u6-l2-quiz-generation.md)。
- `etc/Mindmap.md` / `Mindmap.svg`：课程思维导图。
- `etc/CONTRIBUTING.md`、`etc/CODE_OF_CONDUCT.md`：贡献与行为规范。

**④ `translations/` 镜像源结构，且体积巨大。** 它按语言码分子目录（如 `zh-CN`、`ja`、`fr`），每种语言内部目录结构与 `lessons/` 完全一致。README 给出了跳过它的稀疏克隆命令：

[README.md:33-49](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L33-L49) —— 这段说明仓库含 50+ 语言翻译导致下载量激增，并提供 `git sparse-checkout` 命令排除 `translations` 与 `translated_images`，从而大幅减少下载体积。

#### 4.3.4 代码实践

1. **实践目标**：亲手摸一遍顶层辅助目录，建立「要找 X 就去 Y」的映射。
2. **操作步骤**（在仓库根目录执行）：
   ```bash
   # 看 examples 里有哪些新手示例
   ls examples/
   # 看 etc 里有哪些工具
   ls etc/
   # 数一下 translations 支持多少种语言
   ls translations/ | wc -l
   ```
3. **需要观察的现象**：
   - `examples/` 下有 4 个示例文件（`01-hello-ai-world.py` 等）加一个 README。
   - `etc/` 下能看到 `quiz-app/`、`quiz-src/`、`pdf/`、`Mindmap.*`、`CONTRIBUTING.md` 等。
   - `translations/` 子目录数量在 50 以上（语言码 + 少量文件）。
4. **预期结果**：你能说出「想跑新手示例去 `examples/`」「想看测验代码去 `etc/quiz-app/`」「想要中文版去 `translations/zh-CN/`」。

> 如本地用稀疏克隆排除了 `translations/`，则 `ls translations/` 可能为空——这正是 4.3.1 提到的「跳过翻译」效果，属正常现象。

#### 4.3.5 小练习与答案

**练习 1**：某节课的 README 里「课前测验」链接打不开本地怎么办？相关的题库源代码在哪个目录？

> **答案**：在线版可访问 README 给的 `ff-quizzes.netlify.app` 链接；本地版题库与生成脚本在 `etc/quiz-src/`（`questions-en.txt` + `qzmkjson.py`），前端应用在 `etc/quiz-app/`。

**练习 2**：为什么克隆这个仓库会很慢？有什么办法缓解？

> **答案**：因为 `translations/`（50+ 语言）和 `translated_images/` 体积巨大。可用 README 提供的 `git sparse-checkout` 稀疏克隆命令排除这两个目录，只下载英文源码，大幅减小下载量。

## 5. 综合实践

把本讲三个模块串起来，完成下面这张「仓库地图」任务：

**任务**：画出 `lessons/` 目录的 **二层树状图**，并为每个编号大单元（从 `0-course-setup` 到 `X-Extras`）标注其主题与对应的 README 罗马数字；再从树中挑一节结构完整的小课程，列出它的全部文件并标注每个文件的类型（讲义 / Notebook / 实验起点 / 配图）。

**建议步骤**：

1. 在仓库根目录执行 `ls lessons/`，记录所有一级目录。
2. 对照 [README.md 的 Content 表](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L81-L118)，为每个一级目录标注罗马数字与主题（如 `4-ComputerVision` → IV / 计算机视觉）。
3. 任选一个大单元（推荐 `4-ComputerVision` 或 `5-NLP`，子课较多），执行 `ls lessons/4-ComputerVision/` 展开第二层。
4. 选一节有 `lab/` 的小课程（如 `03-Perceptron`），用 `ls -R` 列出它的全部文件，按下表标注类型。

**交付示例**（可仿照这个格式写在自己的笔记里）：

```
lessons/
├── 0-course-setup/   # 第 0 单元：环境搭建（setup.md / how-to-run.md）
├── 1-Intro/          # I  AI 导论（README.md + assignment.md）
├── 2-Symbolic/       # II 符号 AI（README + 3 个 Notebook + assignment）
├── 3-NeuralNetworks/ # III 神经网络（README + 03/04/05 三节）
│   └── 03-Perceptron/
│       ├── README.md            → 讲义正文
│       ├── Perceptron.ipynb     → 可执行 Notebook
│       ├── images/              → 配图
│       └── lab/
│           ├── README.md                 → 实验任务说明
│           └── PerceptronMultiClass.ipynb → 实验起始 Notebook
├── 4-ComputerVision/ # IV 计算机视觉
├── 5-NLP/            # V  自然语言处理
├── 6-Other/          # VI 其他 AI 技术
├── 7-Ethics/         # VII AI 伦理
└── X-Extras/         # IX 附加内容（多模态）
```

> 本实践为源码阅读型实践，全程不需要运行 Notebook，只需 `ls` 系列命令即可完成。

## 6. 本讲小结

- `lessons/` 采用 **两级编号**：一级是「数字-主题」大单元（对应 README 罗马数字 I–IX），二级是「两位数字-主题」小课程；**数字前缀即学习顺序**。
- 一节课典型含 **`README.md`（讲义）+ `.ipynb`（Notebook）+ 可选的 `lab/`（实验）或 `assignment.md`（写作作业）+ `images/`**；`lab` 偏代码实战、`assignment` 偏思考写作，两者都「部分课程才有」。
- 官方对单课文件类型的定义见 [README.md:120-125](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L120-L125)，强调 Notebook 常分 PyTorch / TensorFlow 双版本。
- 顶层辅助目录各司其职：`examples/`（新手示例）、`etc/`（测验与导览工具）、`translations/`（50+ 语言镜像）、`data/`（数据集）、`binder/`（云端运行配置）。
- `translations/` 与 `translated_images/` 体积巨大，是下载慢的主因，可用 `git sparse-checkout` 稀疏克隆跳过（见 [README.md:33-49](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L33-L49)）。
- 找东西的口诀：**学课程 → `lessons/`；跑新手示例 → `examples/`；看测验 → `etc/quiz-app`；要中文 → `translations/zh-CN`**。

## 7. 下一步学习建议

现在你已经有了完整的「仓库地图」，下一步建议：

- **立即衔接 [u1-l3 开发环境搭建与多种运行方式](./u1-l3-environment-setup.md)**：学会创建 `ai4beg` conda 环境并启动 Jupyter，这样你才能真正「打开」本讲提到的那些 `.ipynb` Notebook。
- 环境就绪后，可先做 [u1-l4 从 examples 开始：第一个 AI 程序](./u1-l4-first-ai-program.md)，跑通 `examples/01-hello-ai-world.py`，获得第一次「从数据中学习」的直观体验。
- 如果你对仓库的「工具链」更感兴趣（而非 AI 内容本身），也可以跳到第 6 单元，先读 [u6-l1 测验应用架构](./u6-l1-quiz-app.md) 了解 `etc/quiz-app` 这个 Vue 应用如何工作。
- 推荐继续精读源码：通读一遍 [AGENTS.md 的 File Organization](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L135-L154) 与 [README 的 Content 表](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L81-L118)，把目录结构彻底印在脑子里，后续每一讲都会轻松很多。
