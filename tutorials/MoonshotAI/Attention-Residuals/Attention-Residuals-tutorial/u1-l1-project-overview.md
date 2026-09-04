# 项目概览：一个论文发布仓库的结构与定位

## 1. 本讲目标

本讲是整套学习手册的第一讲。读完本讲，你应该能够：

1. 说清楚 MoonshotAI/Attention-Residuals 这个仓库**是什么**：它是 Attention Residuals（AttnRes）论文的官方发布仓库，由 README、论文 PDF 和图片资源组成，**没有可直接安装运行的工程代码**。
2. 理解 AttnRes 的定位：它是 Transformer 中**标准残差连接（residual connection）的 drop-in 替换方案**——不改动注意力机制本身，只改"前面各层的输出如何汇入当前层"这一环节。
3. 掌握本仓库的"阅读方法"：核心可读"源码"是 README 中的 PyTorch 风格伪代码，实验细节在论文 PDF 和 assets 图片中。
4. 搭建好 PyTorch 实验环境，用 `torch.stack` 和 `torch.einsum` 跑通一个小型张量实验，为后续讲义（第 2 单元动手实现 Block AttnRes）做好准备。

## 2. 前置知识

本讲是入门第一讲，不要求你已经读过 Transformer 源码，但下面几个概念最好先有个直观印象。不熟悉也没关系，我们用最通俗的方式解释：

- **Transformer**：当前大语言模型（GPT、Kimi 等）的主流网络结构，由很多层（layer）堆叠而成，每层通常包含一个**注意力子层（Attention）**和一个**MLP 子层**。
- **残差连接（residual connection）**：每个子层的输出不是直接替换输入，而是"加"到输入上：\( h_{\text{new}} = h_{\text{old}} + v \)，其中 \( v \) 是本子层的输出。它让梯度可以"抄近道"流回浅层，是训练深层网络的关键技巧。本项目的全部改动都发生在这一环节。
- **PreNorm**：一种把归一化层（Norm）放在子层"入口"的 Transformer 变体，训练稳定，是当前主流选择；但它的残差主干上没有任何缩放，导致隐藏状态（hidden states）的幅度（可以理解为向量的"大小"）会随深度不断增长。这是 AttnRes 要解决的问题，第 2 讲会详细展开。
- **drop-in replacement（直接替换组件）**：指可以"原位替换"现有组件、几乎不需要改动其他部分的方案。AttnRes 替换的就是残差连接这个组件。
- **伪代码（pseudocode）**：不是能直接运行的程序，而是用真实编程语言的语法（这里是 PyTorch 风格的 Python）表达算法逻辑的代码。README 中的伪代码是本仓库最接近"源码"的东西。
- **arXiv**：学术论文的预印本平台。本项目对应的论文编号是 arXiv:2603.15031。
- **scaling law（缩放定律）**：研究"模型损失如何随计算量/参数量增长而下降"的实验方法，用来比较两种结构的性价比。
- **`einsum`（爱因斯坦求和约定）**：PyTorch 中用字符串描述张量运算的接口，例如 `'d, nbtd->nbt'` 表示用一个向量对一批向量做点积。它是理解 AttnRes 伪代码的核心工具，本讲实践会用到。

## 3. 本讲源码地图

整个仓库只有 6 个被 git 跟踪的文件，全部是文档与资源，**没有任何 `.py`/`.json`/构建配置**。这本身就是最重要的信息：

| 文件 | 作用 | 在本讲中的用法 |
|:---|:---|:---|
| `README.md` | 仓库主页：项目定位、方法概述、**PyTorch 风格伪代码**、实验结果 | 本讲的"主源码"，逐段精读 |
| `Attention_Residuals.pdf` | 论文全文（约 932 KB） | 实验设置与公式的权威来源，本讲只做导览 |
| `assets/overview.png` | 总览图：(a) 标准残差、(b) Full AttnRes、(c) Block AttnRes | 理解三种残差方案的直观图示 |
| `assets/scaling_law.png` | Scaling law 实验图 | 第 3 单元第 2 讲精读，本讲先认脸 |
| `assets/training_dynamics.png` | 训练动态图（幅度与梯度范数） | 第 2 单元第 5 讲精读，本讲先认脸 |
| `assets/logo.png` | 仓库标题栏的小图标 | 仅装饰，无需精读 |

> **重要认知**：因为仓库没有工程代码，这套学习手册的"源码精读"对象是 README 中的伪代码与公式，而"代码实践"则是**动手把伪代码实现出来**。阅读时请始终打开 [README.md](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md) 对照。

## 4. 核心概念与源码讲解

本讲包含三个最小模块：**仓库结构**、**README 导览**、**论文 PDF 与图片资源**。

### 4.1 仓库结构

#### 4.1.1 概念说明

"论文发布仓库（paper release repo）"是 AI 研究团队常见的做法：论文发表后，放出一个仓库存放论文链接、图示、有时附带官方代码。本仓库属于**纯文档型**的发布仓库——只发布 README、PDF 和图片，官方训练代码并未发布。

理解这一点决定了我们的学习策略：

- 不能 `pip install`、不能 `python main.py`，没有 issue 之外的可运行入口；
- 一切"源码级"的细节都以 README 伪代码为唯一锚点，更严谨的推导和实验超参要看论文 PDF；
- 因此**动手复现**是掌握这套方法的唯一路径，这也是整套手册每一讲都配实践任务的原因。

#### 4.1.2 核心流程

用下面的清单确认仓库的真实构成（可在本地克隆后执行）：

```text
git clone https://github.com/MoonshotAI/Attention-Residuals.git
cd Attention-Residuals
git ls-files
```

预期输出（共 6 个文件）：

```text
Attention_Residuals.pdf
README.md
assets/logo.png
assets/overview.png
assets/scaling_law.png
assets/training_dynamics.png
```

仓库的目录树可以画成：

```text
Attention-Residuals/
├── README.md                  # 主文档：定位、公式、伪代码、结果表
├── Attention_Residuals.pdf    # 论文全文
└── assets/                    # README 引用的图片资源
    ├── logo.png               # 标题图标
    ├── overview.png           # 三种残差方案总览图
    ├── scaling_law.png        # 缩放定律实验图
    └── training_dynamics.png  # 训练动态对比图
```

注意：仓库根目录没有 `src/`、`setup.py`、`pyproject.toml`、`requirements.txt`、CI 配置或测试目录——这印证了"无可运行工程代码"的判断。

#### 4.1.3 源码精读

仓库结构本身可以从 git 记录侧面验证。仓库的全部提交历史只有 4 个 commit，从 "initial commit" 到 "Update README.md"，说明这是一个一次性发布、少量维护的文档仓库：

- 提交历史：`85e2231 Update README.md` → `3822cb9 Change citation format in README` → `a422c80 update logo` → `729383a initial commit`（最新在前）。

README 的开头是标准的论文仓库样式——居中标题、logo、导航链接。导航栏给出了全部四个入口：Paper（本地 PDF）、arXiv 页面、Overview、Results、Citation：

[README.md:L14-L20](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L14-L20) —— 这一段是 HTML 锚点导航，`href="Attention_Residuals.pdf"` 指向仓库内的论文文件，`https://arxiv.org/abs/2603.15031` 是论文的 arXiv 编号，`#overview`/`#results`/`#citation` 对应 README 内的三个小节锚点。

整份 README 共 143 行、约 6 KB，是一份"单页论文说明书"，结构为：标题区（L1-L31）→ Overview（L35-L47）→ 伪代码（L49-L93）→ Results（L95-L127）→ Citation（L129-L142）。下一模块按这个顺序逐段走读。

#### 4.1.4 代码实践

**实践一：建立你的资源档案**

1. **实践目标**：把仓库 6 个文件登记成一张"可引用资源表"，后续每一讲引用图片或论文时都从这张表取链接，避免每次翻找。
2. **操作步骤**：
   - 克隆仓库并执行 `git ls-files`，确认输出与 4.1.2 一致；
   - 执行 `wc -l README.md` 确认 README 行数（应为 143 行）；
   - 执行 `du -h Attention_Residuals.pdf assets/*.png` 记录各资源大小；
   - 在自己的笔记里建一张表：文件名、类型（文档/论文/图示）、对应主题（定位/方法/scaling/训练动态）。
3. **需要观察的现象**：`git ls-files` 输出里**没有任何代码文件**；README 是唯一的 `.md` 文档。
4. **预期结果**：得到与 4.1.2 目录树完全一致的资源清单。若你看到的文件多于 6 个，说明可能看错了仓库（例如 fork 中加了第三方复现）。
5. 数值型输出（如文件大小）随平台显示略有差异，具体数值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么说本仓库"没有工程代码"是学习路线上必须最先确认的事实？

**参考答案**：因为它决定了学习方式——没有可运行的入口，就不能靠"跑起来打断点"来学习，只能以 README 伪代码为阅读锚点、以论文 PDF 为细节依据，并通过自己动手实现来验证理解。整套手册的实践任务都围绕"把伪代码变成真代码"设计。

**练习 2**：如果想知道某个实验的具体超参数（如学习率、训练 token 数），应该去仓库的哪个文件找？为什么？

**参考答案**：优先查 `Attention_Residuals.pdf`（论文正文与附录）。README 是概述性文档，只给出结论级信息（如"1.4T tokens"出现在结果表标题中）；训练超参这类细节通常在论文实验部分。仓库未附带官方代码，所以论文是唯一权威来源，具体页码待你翻阅 PDF 确认。

### 4.2 README 导览

#### 4.2.1 概念说明

README 是本仓库的"主源码"。它用最短的篇幅回答了四个问题：

1. **是什么**：AttnRes，标准残差连接的 drop-in 替换；
2. **怎么工作**：一个公式 + 一段 PyTorch 风格伪代码；
3. **效果如何**：scaling law 与 9 项下游基准的结论；
4. **如何引用**：BibTeX 条目。

本模块带你把这四个部分的位置和内容摸清楚，形成"以后随手就能定位"的地图。公式的数学细节留给第 2、3 讲，本讲只建立框架。

#### 4.2.2 核心流程

README 的信息流可以概括为：

```text
问题（标准残差会稀释与膨胀）
   ↓
方案（h_l = Σ α_{i→l} · v_i，跨深度 softmax 注意力）
   ↓
工程化（Block AttnRes：O(Ld) → O(Nd) 内存）
   ↓
伪代码（block_attn_res 函数 + forward 调度）
   ↓
证据（scaling law 图 + Kimi Linear 48B 结果表 + 训练动态图）
```

这正好也是整套学习手册的展开顺序：第 1 单元讲问题与方案，第 2 单元实现伪代码，第 3 单元解读证据并做二次开发。

#### 4.2.3 源码精读

**第一站：项目定位声明。**

[README.md:L33](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33) —— 这一句是全仓库最重要的一句话：本仓库是 Attention Residuals（AttnRes）的官方仓库，AttnRes 是 Transformer 中标准残差连接的 drop-in replacement，让每一层可以通过"学到的、依赖输入的跨深度注意力"**选择性地**聚合更早层的表示。注意两个关键词：*selectively*（有选择，而不是一律等权重相加）和 *over depth*（注意力作用在"深度"这个维度上，不是 token 维度）。

**第二站：问题与核心公式（Overview 小节）。**

[README.md:L35-L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L35-L43) —— 这一段先陈述问题：标准残差以**固定的单位权重**累加所有层的输出，深度增大后，这种均匀聚合会**稀释（dilute）每一层的贡献**，并让隐藏状态幅度**无界增长**——这是 PreNorm 的一个著名问题。随后给出 AttnRes 的替换公式：

\[
\mathbf{h}_l = \sum_{i=0}^{l-1} \alpha_{i \to l} \cdot \mathbf{v}_i
\]

其中 \( \mathbf{v}_i \) 是第 \( i \) 层的输出，权重 \( \alpha_{i \to l} \) 由该层一个可学习的**伪查询（pseudo-query）** \( \mathbf{w}_l \in \mathbb{R}^d \) 经 softmax 计算得到。直觉上：标准残差是"前面每层都按权重 1 加进来"，AttnRes 是"本层自己学会按需分配权重，想要哪层多要一点就给大权重"。

**第三站：Block 变体（工程化）。**

[README.md:L45-L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L45-L47) —— Full AttnRes 直观但需要 O(Ld) 内存（L 是层数，d 是隐藏维度，因为要保留所有前层输出）；**Block AttnRes** 把层划分成 N 个块，块内仍用标准残差累加，只在块与块之间做注意力，内存降到 O(Nd)。README 明确给出经验结论：**约 8 个块即可恢复 Full 版本的大部分收益**。

**第四站：伪代码（本仓库的"源码主体"）。**

[README.md:L52-L91](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L52-L91) —— 一个 `<details>` 折叠块里包含两个函数。`block_attn_res`（L53-L65）实现跨块注意力，其核心只有 4 行（L61-L64）：`torch.stack` 把 N 个已完成块加上当前部分和堆成 `[N+1, B, T, D]` 的 V，对 K 做 RMSNorm，用 `einsum` 算出逐 token 的深度 logits，`softmax(0)` 沿深度归一化后再用 `einsum` 加权求和。`forward`（L67-L90）实现层间调度：块内标准残差累加、按 `layer_number % (block_size // 2)` 判断块边界、在注意力前和 MLP 前各应用一次 attn_res。**本讲只需要记住这 4 行的形状变化**（下一模块实践会亲手跑通），逐行解析分别在第 2 单元第 1、2 讲。

**第五站：结果证据。**

[README.md:L95-L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L95-L99) —— Scaling Laws 小节：AttnRes 在所有计算预算上都优于基线，**Block AttnRes 达到了用 1.25 倍计算量训练的基线的损失水平**。

[README.md:L105-L119](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105-L119) —— 下游性能表：在 Kimi Linear 48B（3B 激活参数、1.4T tokens 训练）上，9 项基准全面领先，提升最大的是 GPQA-Diamond（36.9 → 44.4，+7.5）与 HumanEval（59.1 → 62.2，+3.1）。这张表的解读放在第 3 单元第 3 讲。

[README.md:L121-L127](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L127) —— 训练动态小节：AttnRes 缓解 PreNorm 稀释，输出幅度随深度保持有界、梯度范数在各层间分布更均匀。第 2 单元第 5 讲会教你用 hook 亲自复测。

[README.md:L129-L142](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L129-L142) —— Citation 小节：BibTeX 条目 `chen2026attnres`，作者为 Kimi Team 等，年份 2026，arXiv 编号 2603.15031。

#### 4.2.4 代码实践

**实践二：给 README 做"定位打卡"**

1. **实践目标**：不看讲义，能在 30 秒内定位 README 中公式、伪代码、结果表的行号区间。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -n "def block_attn_res" README.md`；
   - 执行 `grep -n "## " README.md` 列出所有章节标题及行号；
   - 执行 `grep -n "einsum" README.md`，确认两处 `einsum` 都在伪代码内。
3. **需要观察的现象**：`grep -n` 输出的行号应与本讲 4.2.3 给出的行号一致（`def block_attn_res` 在第 53 行附近）。
4. **预期结果**：章节锚点 `## Overview`、`## Results`、`## Citation` 与导航栏 `#overview` 等锚点一一对应。若行号有偏差，说明 README 版本与本讲所基于的 HEAD（`85e2231`）不同。
5. 本实践只涉及 grep，行为确定，可直接本地验证。

#### 4.2.5 小练习与答案

**练习 1**：README 里说标准残差"accumulate all layer outputs with fixed unit weights"（用固定单位权重累加所有层输出）。请用自己的话说出这句话对应的数学形式。

**参考答案**：标准残差下 \( \mathbf{h}_l = \mathbf{h}_{l-1} + \mathbf{v}_l = \sum_{i=0}^{l} 1 \cdot \mathbf{v}_i \)，即第 \( l \) 层的隐藏状态是之前所有层输出的**等权求和**，每个 \( \mathbf{v}_i \) 的权重恒为 1，与内容无关。AttnRes 把这个恒为 1 的权重换成 softmax 产生的 \( \alpha_{i \to l} \)。

**练习 2**：Full AttnRes 和 Block AttnRes 的差别是什么？为什么论文主推 Block 版本？

**参考答案**：Full 版本让每层对所有更早层的输出做注意力，需要把每层输出都保存下来，内存随层数 L 线性增长（O(Ld)）；Block 版本把层分组，组内仍用标准残差（只维护一个部分和），注意力只在 N 个块级表示之间进行，内存为 O(Nd)。主推 Block 是因为它以约 8 个块就恢复 Full 的大部分收益，内存开销小，才能作为大规模模型上"边际开销"的 drop-in 替换。

### 4.3 论文 PDF 与图片资源

#### 4.3.1 概念说明

仓库的另外两类资源是**论文 PDF**（细节的权威来源）和 **assets 图片**（结论的可视化证据）。它们与本手册的分工：

- `overview.png`：一图看懂三种残差方案的差别，是理解方法的最佳起点；
- `scaling_law.png`：支撑"1.25 倍计算等效应"这一核心卖点；
- `training_dynamics.png`：支撑"幅度有界、梯度更均匀"这一机理解释；
- `Attention_Residuals.pdf`：以上都只是结论，公式推导、消融实验、训练细节都在论文里。

一个重要的学习方法提示：图片是"结论的快照"，论文是"过程的记录"。看图能快速建立直觉，但做复现时必须回到论文核对设置。

#### 4.3.2 核心流程

建议的阅读顺序（也是从"直觉"到"证据"的顺序）：

```text
overview.png（三种方案对比图，建立直觉）
   ↓
README Overview 小节（公式 h_l = Σ α·v_i，形式化直觉）
   ↓
README 伪代码（工程实现的样子）
   ↓
scaling_law.png + 结果表（它真的有效吗？）
   ↓
training_dynamics.png（它为什么有效？）
   ↓
Attention_Residuals.pdf（严格版本的全部细节）
```

#### 4.3.3 源码精读

**总览图的三联结构。**

[README.md:L22-L29](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L22-L29) —— README 用 `<img>` 标签嵌入 `assets/overview.png`，并配了三段式图注：**(a)** 标准残差——均匀的加法累加，每层输出以固定权重汇入主干；**(b)** Full AttnRes——每层对之前**所有**层的输出做注意力，权重不再均匀；**(c)** Block AttnRes——层先分组进块，注意力只在块级表示之间进行，把内存从 O(Ld) 降到 O(Nd)。打开图片可以看到三个子图分别画出了从 \( \mathbf{v}_i \) 到 \( \mathbf{h}_l \) 的连接方式：(a) 全连接且权重为 1，(b) 全连接但权重是学出来的 \( \alpha \)，(c) 只连到块级表示。

**两张实验图与两个结论。**

[README.md:L101-L103](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L101-L103) —— 嵌入 `scaling_law.png`，对应结论"各计算预算下一致占优，Block AttnRes 匹配 1.25 倍计算的基线"。

[README.md:L125-L127](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L125-L127) —— 嵌入 `training_dynamics.png`，对应结论"输出幅度随深度有界、梯度范数跨层分布更均匀"。

**论文 PDF。**

[Attention_Residuals.pdf](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/Attention_Residuals.pdf) —— 约 932 KB 的论文全文。README 导航栏 L15 的 `Paper` 链接即指向它，arXiv 页面（编号 2603.15031，见 [README.md:L16](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L16)）是同一篇论文的在线版本。论文包含仓库未覆盖的细节：完整推导、消融实验、训练超参数等（具体章节结构待你翻阅 PDF 确认；本套手册第 3 单元第 4 讲会带读）。

#### 4.3.4 代码实践

**实践三：跑通 torch.stack 与 torch.einsum 小实验（本讲主实践）**

这个实验复刻伪代码 L61-L64 的形状流程，验证你的环境足以支撑后续所有实践。以下为**示例代码**（仓库本身无 Python 代码，此为按伪代码形状编写的练习）：

```python
# env_check.py —— 环境自检：复刻 README 伪代码的形状流程（示例代码）
import torch

print("torch version:", torch.__version__)
torch.manual_seed(0)

B, T, D, N = 2, 4, 8, 3          # batch、序列长度、隐藏维度、已完成块数

# 模拟 N 个已完成块的表示 + 当前块内部分和（对应伪代码 L53-L60 的输入）
blocks = [torch.randn(B, T, D) for _ in range(N)]
partial_block = torch.randn(B, T, D)

# 对应伪代码 L61：堆叠成 V，形状 [N+1, B, T, D]
V = torch.stack(blocks + [partial_block])
print("V.shape =", V.shape)                  # 预期 torch.Size([4, 2, 4, 8])

# 对应伪代码 L63：用一个 D 维“伪查询”角色向量算逐 token 深度 logits
w = torch.randn(D)
logits = torch.einsum('d, n b t d -> n b t', w, V)
print("logits.shape =", logits.shape)        # 预期 torch.Size([4, 2, 4])

# 对应伪代码 L64：沿深度(第 0 维) softmax 后加权求和
alpha = logits.softmax(0)
print("alpha.sum(0) 前几个值 =", alpha.sum(0).flatten()[:3])   # 预期全为 1.0

h = torch.einsum('n b t, n b t d -> b t d', alpha, V)
print("h.shape =", h.shape)                  # 预期 torch.Size([2, 4, 8])
```

1. **实践目标**：确认 PyTorch 环境可用，并提前熟悉 AttnRes 伪代码里出现的三个关键操作——`torch.stack`（堆叠深度维）、`einsum`（点积打分）、`softmax(0)`（沿深度归一化）。
2. **操作步骤**：
   - 安装 PyTorch（`pip install torch`，或按官网选择对应 CUDA 版本；本实验 CPU 即可）；
   - 将上面代码保存为 `env_check.py`（放在仓库**外**的练习目录，不要放进仓库）；
   - 运行 `python env_check.py`。
3. **需要观察的现象**：三个形状打印值，以及 `alpha.sum(0)` 是否全为 1。
4. **预期结果**：`V.shape=[4, 2, 4, 8]`、`logits.shape=[4, 2, 4]`、`h.shape=[2, 4, 8]`（这些由张量构造直接决定）；`alpha.sum(0)` 的每个元素都应等于 1.0（softmax 沿第 0 维归一化的数学性质，浮点误差在 1e-6 量级内即可视为 1）。打印的具体浮点数值依赖随机种子与版本，待本地验证。
5. 若 `einsum` 报维度错误，请检查字符串里的空格：`'d, n b t d -> n b t'` 中下标顺序必须与张量实际维度一致。

#### 4.3.5 小练习与答案

**练习 1**：`torch.einsum('d, n b t d -> n b t', w, V)` 这一行在做什么？用自然语言描述。

**参考答案**：把 D 维向量 `w`（在完整 AttnRes 中这个角色由可学习伪查询经投影得到）与 V 中每个位置 `[n, b, t]` 处的 D 维向量做**点积**，得到形状 `[N+1, B, T]` 的打分矩阵——即对每个 batch 里的每个 token，给"深度方向上每一个候选块表示"打一个分。这就是跨深度注意力的 logits。

**练习 2**：为什么 `logits.softmax(0)` 是沿第 0 维（深度维）做 softmax，而不是沿 token 维？

**参考答案**：因为 AttnRes 的注意力是"**跨深度**"的：对每个 token 独立地决定"从深度方向的 N+1 个候选表示中各取多少"。第 0 维正是 `torch.stack` 拼出来的深度维；沿它归一化保证每个 token 的深度权重 \( \alpha_{i \to l} \) 加和为 1（这正是实践三中 `alpha.sum(0)` 全 1 的原因）。token 维（t）与 batch 维（b）各自独立，互不影响。

**练习 3**：如果你想核对论文里的消融实验（例如块数 N 取多少合适），应该读仓库哪个文件？README 能回答吗？

**参考答案**：读 `Attention_Residuals.pdf` 的实验/消融部分。README 只在 [L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47) 给出一句结论级信息（约 8 个块恢复大部分收益），没有给出不同 N 的对比数据。

## 5. 综合实践

**综合任务：产出一份《AttnRes 学习资源档案 + 环境验收单》**，把本讲三个模块串起来：

1. **建档案**：完成实践一，把 6 个仓库文件登记成资源表（文件、类型、主题、一句话摘要），并在表后抄录 README 的四个关键定位：定位声明（L33）、核心公式（L41）、Block 内存结论（L47）、1.25 倍计算结论（L99），各配一行中文注解。
2. **验环境**：完成实践三的 `env_check.py`，把四行打印输出粘贴到验收单里，并回答一个问题：如果把 `N` 从 3 改成 7，`V.shape`、`logits.shape`、`h.shape` 各变成什么？（答案：`[8,2,4,8]`、`[8,2,4]`、`[2,4,8]`——输出形状不随 N 变，这正是 drop-in 替换"不改变接口"的体现。）
3. **做预告**：打开 `overview.png`，用 3 句话分别向一个没读过论文的同学解释 (a)(b)(c) 三个子图的差别，写进档案。这 3 句话就是你下一讲（标准残差的问题）和第三讲（AttnRes 核心思想）的提纲。

完成本综合实践后，你不仅拥有了全套手册的引用基础，还提前跑通了 Block AttnRes 伪代码中全部三个张量操作——第 2 单元实现完整函数时，唯一的新内容就只剩 RMSNorm 与投影层了。

## 6. 本讲小结

- 本仓库是 Attention Residuals（AttnRes）论文的**官方发布仓库**，仅含 6 个文件（README、论文 PDF、4 张图片），**没有任何可运行工程代码**；README 中的 PyTorch 风格伪代码是唯一可精读的"源码"。
- AttnRes 是标准残差连接的 **drop-in 替换**：把"所有前层输出按固定权重 1 累加"改为 \( \mathbf{h}_l = \sum_{i=0}^{l-1} \alpha_{i \to l} \mathbf{v}_i \)，权重由每层一个可学习伪查询 \( \mathbf{w}_l \in \mathbb{R}^d \) 经 softmax 产生。
- **Block AttnRes** 把层分组、块内标准残差、块间注意力，将内存从 O(Ld) 降到 O(Nd)，约 8 块即可恢复大部分收益，是实际可用的版本。
- README 的证据链有三条：scaling law（匹配 1.25 倍计算基线）、Kimi Linear 48B 上 9 项基准全面领先（GPQA +7.5 最大）、训练动态（幅度有界、梯度更均匀）。
- 仓库阅读方法：**直觉看 overview.png → 形式看 README 公式 → 实现看伪代码 → 证据看实验图 → 细节回论文 PDF**。
- 学习策略：因为无官方代码，**动手复现伪代码**是掌握 AttnRes 的主线路；本讲的 `torch.stack` / `einsum` / `softmax(0)` 实验就是这条主线路的起点。

## 7. 下一步学习建议

下一讲（`u1-l2-standard-residual-problem.md`：背景知识：标准残差连接为何会稀释与膨胀）将深入本讲埋下的第一个伏笔：**标准残差到底出了什么问题**。你会：

- 推导 PreNorm 下隐藏状态幅度 \( \|\mathbf{h}_l\| \) 随深度增长的直观原因；
- 理解"每个 token 的表示被越来越多的层输出平均"如何**稀释**单层贡献；
- 动手实现一个 12 层 PreNorm 前向骨架，用 hook 亲自画出范数-深度曲线（对应 `training_dynamics.png` 的左半部分）。

在进入下一讲前，建议先自行完成两件事：一是把本讲实践三中 `alpha` 的语义（每个 token 沿深度的权重分布）想清楚；二是预习性地浏览 [README.md:L35-L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L35-L47)，带着"稀释与膨胀具体指什么"的问题去读，下一讲会逐句拆解。
