# 项目定位与仓库结构总览

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 rasbt/machine-learning-book 这个仓库是什么：《Machine Learning with PyTorch and Scikit-Learn》一书的官方配套代码库。
- 记住 ch01 到 ch19 共 19 个章节目录的主题脉络：从经典机器学习（感知机、分类器、预处理、降维、评估、集成、文本、回归、聚类）一路走到深度学习（numpy 手写神经网络、PyTorch、CNN、RNN、Transformer、GAN、GNN、强化学习）。
- 知道每一章（绝大多数）都提供 `.ipynb`（Jupyter notebook）与 `.py`（Python 脚本）两种形式的内容等价的代码文件，并理解它们之间的转换关系。
- 学会使用 ERRATA 勘误目录核对书中已发现的错误。
- 掌握「只看目录、不开编辑器也能定位代码」的仓库导航能力，为后续所有讲义打基础。

本讲不运行任何代码，全部任务都是「阅读 + 对照」型的源码阅读实践。

## 2. 前置知识

本讲是整套手册的第一讲，不要求你写过机器学习代码，但需要以下基础概念：

- **代码仓库（repository）**：把一个项目的所有文件（代码、文档、数据）集中存放并做版本管理的地方。本仓库托管在 GitHub 上，用 Git 做版本管理。
- **Jupyter notebook（.ipynb 文件）**：一种「代码 + 文字 + 图表」混合的文档格式，可以在浏览器中逐段运行代码并立即看到输出。它本质上是一个 JSON 文本文件，内部由一个个「单元格（cell）」组成：markdown 单元格放说明文字，code 单元格放可执行代码。
- **Python 脚本（.py 文件）**：普通的纯文本 Python 程序文件，可以用 `python xxx.py` 整体运行。它没有 notebook 的分块输出能力，但更便于用命令行批量执行。
- **勘误（errata）**：图书出版后发现并公开更正的错误清单。读技术书时先扫一眼勘误，可以避免被印刷错误误导。
- **永久链接（permalink）**：本手册引用源码时使用「固定 commit 哈希 + 文件路径 + 行号」构成的 GitHub 链接。即使仓库以后更新，链接指向的仍是当前版本的内容，行号不会漂移。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 仓库首页：书籍信息、19 章目录表、环境指引入口 |
| `ch01/README.md` | 第 1 章专属说明：环境搭建方式、版本检查脚本用法、notebook 与脚本关系的官方说明 |
| `ERRATA/README.md` | 全书勘误清单，按章节组织，附 issue/PR 来源链接 |
| `ch02/ch02.ipynb` | 第 2 章 notebook 形式的代码（感知机、Adaline） |
| `ch02/ch02.py` | 第 2 章脚本形式，与 notebook 内容等价 |
| `ch02/README.md` | 第 2 章目录页：章节大纲 + 指向 ch01 的运行说明 |

辅助文件（后续讲义详讲，本讲只认识）：根目录的 `.convert_notebook_to_script.py`（隐藏文件，把 notebook 转成脚本）、`update_python_from_notebook.sh`、`environment.yml`、`Makefile`、`python_environment_check.py`。

## 4. 核心概念与源码讲解

### 4.1 README 目录表：仓库定位与章节主题脉络

#### 4.1.1 概念说明

打开任何开源项目，第一个该读的文件都是根目录的 `README.md`。它回答三个问题：

1. **这个项目是什么？**——本书配套代码。书籍信息写在最前面：770 页、Packt 出版、ISBN 编号等，还给出了购买链接和内容摘要博客。
2. **里面有什么？**——一张 19 章的目录表，每章一个链接，点开直达对应章节目录。
3. **怎么开始？**——指向 `ch01/README.md` 的安装指引和一份 Google Colab 运行指南 PDF。

需要特别注意仓库首页的一句「免责声明」：这些 notebook 只是书的**配套代码示例**，没有书中的公式推导和文字讲解，单看 notebook 可能不够用。这句话决定了本手册的学习方式：以源码为主线，遇到原理不清时回到书或官方文档。

#### 4.1.2 核心流程

使用 README 导航仓库的标准流程：

1. 读 `README.md` 顶部的书籍信息，确认仓库与书的对应关系（书名、作者、出版年份）。
2. 浏览目录表（共 19 行，ch01 → ch19），对主题脉络建立整体印象。
3. 点击某一章的 `open dir` 链接（或直接进入本地 `chXX/` 目录）。
4. 遇到环境问题 → 回到 `ch01/README.md`；发现书中内容可疑 → 去 `ERRATA/` 查勘误。

19 章的主题脉络可以概括为四段：

| 段落 | 章节 | 主题 |
| --- | --- | --- |
| 起步 | ch01 | 机器学习概念与环境搭建（无实质代码） |
| 经典机器学习 | ch02–ch10 | 感知机/Adaline → scikit-learn 分类器 → 预处理 → 降维 → 模型评估 → 集成 → 文本情感 → 回归 → 聚类 |
| 从 numpy 到 PyTorch | ch11–ch13 | 手写多层神经网络 → PyTorch 张量与训练 → PyTorch 核心机制与高层接口 |
| 深度学习前沿 | ch14–ch19 | CNN → RNN → Transformer → GAN → GNN → 强化学习 |

#### 4.1.3 源码精读

书籍定位与基本信息，可以在这里确认作者与出版信息：

[README.md:L1-L14](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/README.md#L1-L14)
这段是仓库标题、页数、出版社、ISBN 以及封面图链接，说明本仓库是《Machine Learning with PyTorch and Scikit-Learn》（Raschka、Liu、Mirjalili 合著，Packt 2022）的代码库。

目录表入口与两个重要指引：

[README.md:L25-L31](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/README.md#L25-L31)
这段做了三件事：第 27 行把安装与环境搭建指引指到 `ch01/README.md`；第 29 行给出一份社区贡献的 Google Colab 运行指南（`supplementary/running-on-colab.pdf`），适合没有本地 GPU 的读者；第 31 行明确提醒：notebook 只是代码示例，缺少书中的公式与文字说明。

19 章目录表本体（截取前 4 章为例）：

[README.md:L34-L52](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/README.md#L34-L52)
完整的 19 章目录表，每一行格式为「章节序号 + 英文标题 + `[open dir](chXX)` 目录链接」。例如第 35 行的 `Training Machine Learning Algorithms for Classification` 对应 `ch02/`，正是后面单元 2 要精读的感知机与 Adaline 实现；表末第 52 行是第 19 章 `Reinforcement Learning for Decision Making in Complex Environments`。这张表就是整个仓库的「地图」。

文末还提供了标准 BibTeX 引用格式，写论文引用本书时可直接复制：

[README.md:L60-L69](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/README.md#L60-L69)
BibTeX 条目包含三位作者、ISBN、出版社与年份，用于学术引用。

#### 4.1.4 代码实践

**实践目标**：不看任何代码，仅凭根 `README.md` 的目录表说出每章主题，并在本地文件系统中找到对应目录。

**操作步骤**：

1. 打开仓库根目录的 `README.md`，定位到第 34–52 行的目录表。
2. 逐行阅读 19 个章节标题，把英文标题在心里翻成中文（例如 `Compressing Data via Dimensionality Reduction` = 「通过降维压缩数据」）。
3. 在本地仓库根目录执行 `ls`，确认存在 `ch01` 到 `ch19` 共 19 个文件夹。
4. 任选一章（如 ch05），进入该目录确认里面确实有代码文件。

**需要观察的现象**：

- 根目录下除 19 个章节目录外，还有 `ERRATA/`、`supplementary/`、`environment.yml`、`Makefile`、`python_environment_check.py` 等辅助文件。
- 目录表行号与文件夹编号一一对应（README 第 35 行的第 2 章 ↔ `ch02/` 目录）。

**预期结果**：你能不看资料说出「ch02 讲感知机、ch14 讲 CNN、ch19 讲强化学习」这类映射，并且在文件系统中 10 秒内定位到任意一章。

**待本地验证**：`ls` 输出的完整文件清单请以你本地仓库为准。

#### 4.1.5 小练习与答案

**练习 1**：仓库首页提醒「notebook 可能单独不够用」，原因是什么？

答案：README 第 31 行说明这些 notebook 只是随书代码示例，不包含书中的公式推导和描述性文字，因此最好与书配合阅读。

**练习 2**：如果本地没有 GPU，README 提供了什么替代方案？

答案：第 29 行指向社区贡献的 Google Colab 指南（`supplementary/running-on-colab.pdf`），可以在云端免费运行代码。

**练习 3**：ch12 的标题是 `Parallelizing Neural Network Training with PyTorch`，它处于四段主题脉络的哪一段？

答案：属于第三段「从 numpy 到 PyTorch」（ch11–ch13），这一章正式引入 PyTorch，讲解张量、Dataset/DataLoader 与用 GPU 并行训练。

### 4.2 章节双文件组织：.ipynb 与 .py 的等价关系

#### 4.2.1 概念说明

本仓库最特别的组织约定是：**每章同一份代码有两种载体**。

- `chXX.ipynb`：Jupyter notebook，markdown 单元格放讲解，code 单元格放代码，可以交互式逐段运行，适合边学边试。
- `chXX.py`：纯 Python 脚本，**由 notebook 自动转换生成**，代码内容与 notebook 完全一致，适合命令行运行和纯源码阅读。

这个约定不是口头承诺，而是有官方说明和自动转换脚本支撑的：

- `ch01/README.md` 第 130 行明确写道：书中所有代码示例同时以 `.py` 脚本文件提供，这些脚本**从 Jupyter notebook 转换而来以保证代码完全相同**。
- 根目录的隐藏脚本 `.convert_notebook_to_script.py` 负责转换；`update_python_from_notebook.sh` 用于批量更新。
- 每个 notebook 的最后一个单元格就是转换命令本身（后面源码精读会看到）。

理解这一点的实际价值：**读代码时选 `.py`（行号稳定、结构干净、便于全文搜索），做实验时选 `.ipynb`（分块运行、即时输出）**。本手册后续讲义引用行号时基本都指向 `.py` 文件。

此外还要知道两个「例外情况」（读目录时会遇到）：

- `ch01/` 没有 `ch01.py`，因为第 1 章没有实质代码示例，只有环境搭建说明。
- `ch19/` 顶层只有 `ch19.ipynb`，它的 Python 代码放在 `gridworld/`（3 个模块文件）和 `cartpole/`（1 个文件）两个子目录里。
- 章节变长后会拆成多个 part：如 ch12 是 `ch12_part1`、`ch12_part2`；ch13 拆成 4 个 part；ch16 的文件名用连字符（`ch16-part1-self-attention.py`）而不是下划线，查找时别写错。

#### 4.2.2 核心流程

notebook → 脚本的生成与同步流程：

```text
作者编写/修改 chXX.ipynb
        │
        ▼
notebook 末尾单元格执行:
python ../.convert_notebook_to_script.py --input chXX.ipynb --output chXX.py
        │
        ▼
转换脚本把 markdown 单元格变成 # 注释、code 单元格变成代码
        │
        ▼
得到内容等价的 chXX.py，与 notebook 一起提交到仓库
```

对应地，`.py` 文件的结构规律是：

1. 文件头部：`# coding: utf-8` 与 import 语句。
2. 包版本检查块（由 notebook 开头的检查单元格转换而来）。
3. 若干 `#` 开头的注释块 = notebook 中的 markdown 标题/说明。
4. 顶层代码与类定义 = notebook 中的 code 单元格，顺序完全一致。

#### 4.2.3 源码精读

官方对双文件关系的说明（最权威的依据）：

[ch01/README.md:L102-L130](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch01/README.md#L102-L130)
这一节先解释什么是 Jupyter notebook（代码、运行结果、图表、Markdown/LaTeX 文档集中在一份文件里），推荐安装 JupyterLab；最后第 130 行点明关键：notebook 是可选但强烈推荐的，**所有代码示例也都以 .py 脚本形式提供，这些脚本由 notebook 转换而来以确保代码一致**。

看一个真实的 `.py` 文件头部，验证「markdown → 注释」的转换规律：

[ch02/ch02.py:L1-L35](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L1-L35)
第 1 行是编码声明，第 4–10 行是 import；第 12–15 行的 `# # Machine Learning with PyTorch and Scikit-Learn`、`# ## Package version checks` 正是 notebook 中 markdown 单元格的标题（转换规则：一级标题 `#` 变成 `# #`，两级标题变成 `# ##`）；第 21 行 `sys.path.insert(0, '..')` 把上级目录加入模块搜索路径，以便第 5 行 `from python_environment_check import check_packages` 能找到仓库根目录的检查脚本；第 30–35 行用字典声明推荐版本并调用 `check_packages(d)`。

`.py` 中的章节大纲注释（对应 notebook 的目录单元格）：

[ch02/ch02.py:L38-L54](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L38-L54)
第 38 行 `# # Chapter 2 - Training Machine Learning Algorithms for Classification` 是章标题，第 43–54 行是带锚点链接的小节目录（感知机历史 → 感知机实现 → Adaline 与梯度下降 → 随机梯度下降）。读任何一章的 `.py` 前，先扫这段注释就能知道全章脉络。

转换之后再往下就是真正的类定义，例如第 92 行开始是 `class Perceptron:`（单元 2 第 1 讲会逐行精读）：

[ch02/ch02.py:L92-L118](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L92-L118)
这是感知机类的定义与构造函数，docstring 说明了 `eta`（学习率）、`n_iter`（迭代轮数）、`random_state`（随机种子）三个参数。现在只需认识它，不必理解算法。

再回到 notebook 一侧，看它末尾的「转换证据」：

[ch02/ch02.ipynb:L1307-L1307](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.ipynb#L1307-L1307)
这是 ch02.ipynb 最后一个 code 单元格（在原始 JSON 文件的第 1307 行）：`! python ../.convert_notebook_to_script.py --input ch02.ipynb --output ch02.py`，即每次更新 notebook 后运行它来重新生成 `ch02.py`。它上面的 markdown 单元格写着「Readers may ignore the following cell」（读者可忽略此单元格）。

每个章节目录还有一份自己的 `README.md`，起「章节目录页」作用：

[ch02/README.md:L1-L21](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/README.md#L1-L21)
列出第 2 章的小节大纲，并在第 20 行提醒：运行说明统一见 `../ch01/README.md`。也就是说环境问题只在 ch01 讲一次，各章不重复。

#### 4.2.4 代码实践

**实践目标**：用纯阅读方式验证 ch02 与 ch03 的双文件对应关系。

**操作步骤**：

1. 在仓库根目录执行 `ls ch02` 和 `ls ch03`，分别记录输出。
2. 打开 `ch02/ch02.py`，找到第 92 行的 `class Perceptron:`。
3. 打开 `ch02/ch02.ipynb`（用文本编辑器或 GitHub 页面均可），搜索字符串 `class Perceptron`。
4. 对比两处 `Perceptron` 类的 `__init__` 参数签名是否一致（`eta`、`n_iter`、`random_state`）。

**需要观察的现象**：

- `ch02/` 下应同时存在 `ch02.ipynb` 与 `ch02.py`；`ch03/` 下应同时存在 `ch03.ipynb` 与 `ch03.py`。
- 同一个 `Perceptron` 类在两种文件中代码相同：notebook 里它是某个 code 单元格的全部内容，脚本里它从第 92 行开始。
- notebook 中 markdown 单元格的文字（如 "Package version checks"）在 `.py` 里变成了 `# ## Package version checks` 注释。

**预期结果**：确认 ch02 与 ch03 均为「notebook + 脚本」双文件组织，且两份内容等价；能说出为什么引用行号时优先用 `.py`（notebook 是 JSON，原始行号随单元格内容变化，且不便人类阅读）。

**待本地验证**：如果你的仓库版本不是本手册使用的 commit，行号可能略有偏移。

#### 4.2.5 小练习与答案

**练习 1**：为什么仓库要同时维护 `.ipynb` 和 `.py` 两种文件？

答案：notebook 适合交互式学习（分块运行、即时看输出和图），脚本适合命令行运行、全文搜索与稳定引用。官方说明（ch01/README.md 第 130 行）指出 `.py` 由 notebook 转换生成以保证代码一致，两种形式服务不同使用场景。

**练习 2**：`ch01/` 目录下为什么找不到 `ch01.py`？

答案：第 1 章没有实质代码示例。`ch01/README.md` 第 28 行写明「This chapter does not contain any code examples」，该章的任务是环境搭建与检查，所以没有生成对应脚本。

**练习 3**：如果你想在终端里用 `grep` 搜索 `AdalineSGD` 类的定义，应该搜 `.ipynb` 还是 `.py`？为什么？

答案：搜 `.py`。notebook 是 JSON 格式，源代码被包在引号和转义字符里（例如换行是 `\n` 字符串），直接 grep 结果难读；`.py` 是纯文本，行号干净、结构清晰。

### 4.3 ERRATA 勘误目录：读书与读码的纠错通道

#### 4.3.1 概念说明

`ERRATA/` 是全书公开勘误的存放目录，入口是 `ERRATA/README.md`。技术书印刷后发现的错误（公式抄写错误、代码笔误、表述不严谨等）会按章节记录在这里，每条通常包含：

- **位置**：章节 + 页码；
- **问题与更正**：原文哪里错、应改成什么；
- **来源**：发现该错误的 GitHub issue 或 PR 链接，可追溯讨论过程。

对学习者的意义：**当你在书里看到一个可疑的公式或代码，先查勘误再怀疑自己**。仓库代码通常是按勘误更新过的，因此「书上的错误」可能已在代码中修正——这也解释了为什么偶尔会出现「代码和书对不上」的情况。

目录里还有一个 `old-errata/` 子目录，存放 2022 年 11 月 16 日之前印刷版本的旧勘误；当前 `README.md` 只列最新印刷批次的错误。

#### 4.3.2 核心流程

使用勘误目录的流程：

1. 学习中遇到疑似错误（公式、参数说明、代码行为与书不一致）。
2. 打开 `ERRATA/README.md`，按章节标题跳到对应小节。
3. 用页码或关键词定位条目，确认是否为已知错误。
4. 如需了解背景，点击条目附带的 issue/PR 链接。

#### 4.3.3 源码精读

勘误文件的总体结构（标题 + 按章分节）：

[ERRATA/README.md:L1-L9](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ERRATA/README.md#L1-L9)
第 1 行 `# Current Errata` 表明这是「当前印刷批次」的勘误；紧接着第 4 行开始就是 `## Chapter 1` 小节。第 6–15 行的第一条其实标注为「不是错误，而是改进建议」（关于向量/矩阵记号约定的表述优化），可见勘误目录也收录表述类改进。

看一个真实的代码相关勘误条目（第 2 章）：

[ERRATA/README.md:L52-L74](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ERRATA/README.md#L52-L74)
「Chapter 2」小节包含两条：**Page 23** 指出感知机学习规则示例中预测标签 \(\hat{y}^{(i)}\) 与真实标签 \(y^{(i)}\) 写反了，应为 \(\hat{y}^{(i)} = 0,\ y^{(i)} = 1\)，并附 issue 链接 #225；**Page 38** 指出 MSE 求导的最终求和应对特征下标 \(j\) 而不是样本下标 \(i\) 进行（issue #64）。这两处都直接影响单元 2 将要学习的内容，读第 2 章书稿时务必带上这两条更正。

再看第 3 章小节的开头，了解条目如何与代码修复关联：

[ERRATA/README.md:L78-L90](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ERRATA/README.md#L78-L90)
Page 66 条目指出 `LogisticRegressionGD` 的 docstring 误写成 "Mean squared error loss"，应为 "Log log"（对数损失），来源是 PR #98；Page 67 条目指出损失计算中除以 `X.shape[0]` 应作用于整个损失表达式，来源 PR #104 与 #241。注意这些是通过 **PR** 修复的——也就是说仓库代码已改正，而早期印刷的书还留着旧写法。

文件末尾是新旧勘误的分界说明：

[ERRATA/README.md:L351-L351](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ERRATA/README.md#L351-L351)
「2022 年 11 月 16 日之前印刷的书请查看 Old Errata」——如果你手上是较早的印刷版本，还要对照 `ERRATA/old-errata/` 里的历史清单。

#### 4.3.4 代码实践

**实践目标**：建立「先查勘误」的习惯，并核对一条勘误是否已反映在仓库代码中。

**操作步骤**：

1. 打开 `ERRATA/README.md`，通读各章小节的标题与页码，感受勘误密度（第 1–17 章均有涉及）。
2. 找到 Chapter 3 小节 Page 66 的条目（docstring 错误）。
3. 打开 `ch03/ch03.py`，搜索 `LogisticRegressionGD` 类的 docstring。
4. 观察其中对损失函数的描述用词。

**需要观察的现象**：

- 勘误条目结构统一：页码 → 问题描述 → 更正 → 来源链接。
- `ch03.py` 中 `LogisticRegressionGD` 的 docstring 对损失函数的描述与勘误后的正确说法（对数损失，log loss）一致，而不是错误版本 "Mean squared error loss"。

**预期结果**：验证「勘误 + PR 已同步修复仓库代码」这一工作流，理解为什么本手册强调以仓库代码为准。

**待本地验证**：docstring 的具体措辞请以你本地 `ch03/ch03.py` 为准。

#### 4.3.5 小练习与答案

**练习 1**：勘误条目里的 `Source: [#225](...)` 链接有什么用？

答案：它指向发现或讨论该错误的 GitHub issue/PR，点击可以看到错误被发现的经过、讨论与修复代码，便于深入理解。

**练习 2**：为什么读这本书时「以仓库代码为准」通常是对的？

答案：因为很多勘误是通过 PR 直接修进仓库代码的（如 ch03 的 PR #98、#104、#241），代码更新比已印刷书籍快，书上的旧错误往往在代码里已经改正。

**练习 3**：`ERRATA/old-errata/` 子目录保存什么？

答案：保存 2022 年 11 月 16 日之前印刷批次适用的历史勘误；当前 `ERRATA/README.md` 只覆盖最新批次。

## 5. 综合实践

**任务**：不运行任何代码，仅凭 `README.md` 的目录表和各章文件夹的内容清单，亲手整理一张**「章节号 - 主题 - 关键源码文件」对照表**。这张表将是你后续学习全部 19 个单元的私人导航图。

**操作步骤**：

1. 打开根 `README.md` 第 34–52 行的目录表，把 19 个英文标题译成中文主题。
2. 依次 `ls chXX`（或用文件管理器），记录每章的代码文件名，特别注意三种特殊形态：
   - 单文件章节（ch02–ch11）：`chXX.ipynb` + `chXX.py`；
   - 多 part 章节（ch12–ch18）：文件名形如 `ch13_part1.py`，ch16 用连字符（`ch16-part2-gpt2.py`）；
   - 特殊章节：`ch01/` 只有 notebook 和 README；`ch19/` 顶层只有 `ch19.ipynb`，代码在 `gridworld/`、`cartpole/` 子目录。
3. 确认 ch02 的 notebook 与脚本分别是 `ch02/ch02.ipynb`、`ch02/ch02.py`；ch03 的是 `ch03/ch03.ipynb`、`ch03/ch03.py`。
4. 完成下表（前 5 行为示例，请补全到 ch19；数据文件一栏可选）。

示例表（读者需自行补全）：

| 章节 | 主题 | 关键源码文件（已验证存在） |
| --- | --- | --- |
| ch01 | 机器学习概述与环境搭建（无代码示例） | `ch01/README.md`、`ch01/ch01.ipynb` |
| ch02 | 从零实现感知机与 Adaline | `ch02/ch02.py`、`ch02/ch02.ipynb`、数据 `ch02/iris.data` |
| ch03 | scikit-learn 分类器：逻辑回归、SVM、树、KNN | `ch03/ch03.py`、`ch03/ch03.ipynb` |
| ch04 | 数据预处理与特征选择 | `ch04/ch04.py`、数据 `ch04/wine.data` |
| ch05 | 降维：PCA、LDA、核 PCA | `ch05/ch05.py`、数据 `ch05/wine.data` |
| ch06 | （待补全） | （待补全） |
| ... | ... | ... |
| ch19 | 强化学习：GridWorld 与 CartPole | `ch19/ch19.ipynb`、`ch19/gridworld/*.py`、`ch19/cartpole/main.py` |

**需要观察的现象**：绝大多数章节严格遵循「同名 ipynb + py 成对出现」的规律；偏离规律的地方（ch01、ch12 起的 part 拆分、ch16 的连字符、ch19 的子目录）恰好反映了章节内容的组织差异（无代码、篇幅长、多主题、多环境）。

**预期结果**：一张 19 行的完整对照表。以后任何讲义提到「ch15_part2」，你都能立刻知道它是第 15 章第 2 部分（LSTM 情感分类）、该去哪个文件找代码。

**待本地验证**：表中文件名以你本地 `ls` 结果为准；本讲义中的文件清单基于 commit `63bee95` 验证。

## 6. 本讲小结

- 本仓库是《Machine Learning with PyTorch and Scikit-Learn》的官方配套代码，根 `README.md` 的目录表（L34–L52）是全仓库的导航地图，19 章覆盖「经典机器学习 → numpy 手写神经网络 → PyTorch → 六大深度学习架构」。
- 每章代码有 `.ipynb` 与 `.py` 两种等价载体：`.py` 由 notebook 经根目录 `.convert_notebook_to_script.py` 自动转换生成，markdown 单元格变成 `#` 注释；读码引用行号用 `.py`，交互实验用 `.ipynb`。
- 例外形态要记牢：ch01 无 `.py`（无代码示例）、ch12 起拆成多个 part、ch16 用连字符命名、ch19 代码在 `gridworld/` 与 `cartpole/` 子目录。
- `ERRATA/README.md` 按章维护全书勘误并附 issue/PR 来源；许多错误已通过 PR 修进代码，遇到「书与代码不一致」先查勘误、以代码为准。
- 学习任何一章前，先读该章 `.py` 文件头部的大纲注释（如 `ch02/ch02.py` L38–L54）和该章 `README.md`，可以快速建立章节地图。

## 7. 下一步学习建议

下一讲（u1-l2《搭建 Python 环境并验证可运行性》）将正式动手：按 `ch01/README.md` 用 conda 或 pip 创建 `pyml-book` 虚拟环境，运行 `python_environment_check.py` 验证 numpy/scipy/scikit-learn 等包版本，并了解 `Makefile` 与 `environment.yml` 的一键建环境方式。

在进入下一讲之前，建议先自行浏览这些文件建立印象：

- [ch01/README.md](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch01/README.md)：环境搭建的完整说明（下一讲主线）。
- [python_environment_check.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/python_environment_check.py)：版本检查脚本的实现。
- [ch02/ch02.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py)：完整通读一遍，感受「注释块 + 类定义」的章节结构，为单元 2 的感知机精读做准备。
