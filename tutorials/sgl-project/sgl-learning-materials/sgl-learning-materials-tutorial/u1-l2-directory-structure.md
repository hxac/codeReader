# 仓库目录结构与内容类型

## 1. 本讲目标

上一讲（u1-l1）我们已经知道：`sgl-learning-materials` 是一个「资料聚合库」——它本身不含 SGLang 运行时代码，而是把分散在各处的幻灯片、博客、视频、论文汇拢成一张导航地图。

本讲我们要做的，是**打开这个仓库的「抽屉」**，看清每个目录、每个根文件到底装了什么。读完本讲，你应当能够：

1. 画出仓库的整体目录布局（根目录三大文件 + 三个内容目录）。
2. 区分**博客、幻灯片、讲义**三类内容，并说出它们各自的文件格式（`.md` / `.pdf` / `.png` / `.pptx`）。
3. 解释 `.gitignore` 与 `LICENSE` 在这个「纯资料」仓库里各自的作用。
4. 在仓库里快速定位「唯一一篇 `.md` 博客」和「唯一一个 `.pptx` 文件」。

> 本讲不含数学公式与算法，重点是把仓库的物理结构看清楚。后续讲义（u1-l3 会把 README 当导航用）都建立在这张结构地图之上。

## 2. 前置知识

本讲默认你已经读过 u1-l1，知道以下两点（不重复展开）：

- **SGLang** 是开源的大语言模型（LLM）推理服务引擎，主打高吞吐、低延迟、低成本。
- **本仓库是「路标」而非「引擎」**：真正的知识在链接终点，引擎源码在主仓库 `sgl-project/sglang`。

下面补充三个阅读目录结构时要用到的小概念：

| 术语 | 通俗解释 |
| --- | --- |
| 目录（directory / folder） | 仓库里的「文件夹」，用来把文件分类存放。 |
| 文件扩展名 | 文件名中 `.` 之后的部分，如 `report.pdf` 的扩展名是 `pdf`，决定了文件被什么工具打开。 |
| 相对路径 | 相对于「当前文件所在目录」的位置写法，例如博客里写 `docs/figs/team.png`，表示「同目录下的 `docs/figs/` 里的 `team.png`」。 |
| 永久链接（permalink） | 指向某个具体 commit 的 GitHub 文件链接，不会因为后续改动而失效。本讲所有引用都带当前 HEAD 的 commit 号。 |

如果你还不知道「为什么要区分本地文件和外部链接」，请先回顾 u1-l1 中关于「判断资料归属看链接是否以 `https://` 开头」的说明。

## 3. 本讲源码地图

本讲涉及的关键文件与目录如下：

```
sgl-learning-materials/                 ← 仓库根目录
├── README.md                           ← 导航索引，整个仓库的「目录页」
├── LICENSE                             ← MIT 开源许可证
├── .gitignore                          ← 告诉 git 忽略哪些文件
├── blogs/                              ← 长文博客（仓库内唯一一篇 + 配图）
│   ├── Efficient LLM Deployment and Serving.md
│   └── docs/figs/                      ← 博客配图，9 张 PNG
├── slides/                             ← 幻灯片（27 个文件，PDF 为主）
│   ├── *.pdf                           ← 25 个 PDF
│   ├── adoption.png                    ← 1 张 PNG
│   └── sglang_adoption_logo.pptx       ← 1 个 PPTX（仓库唯一的 .pptx）
└── sgl-learning-materials-tutorial/    ← 本讲义所在目录（学习手册）
    ├── manifest.json                   ← 学习手册的大纲清单
    └── u1-l1-*.md / u1-l2-*.md ...     ← 各篇讲义
```

| 路径 | 作用 |
| --- | --- |
| `README.md` | 仓库唯一的核心导航文件，把所有资料按 Slides / Blog / Videos / Paper / Documentation 五大类列出。 |
| `LICENSE` | 声明本仓库内容采用 MIT 许可证，版权方为 `sgl-project`。 |
| `.gitignore` | 只有一行 `.DS_Store`，用于忽略 macOS 自动生成的系统文件。 |
| `blogs/` | 存放**仓库内的长文博客**及其配图，是少数「内容真正写在仓库里」的目录。 |
| `slides/` | 存放**仓库内的幻灯片文件**，是本仓库体量最大的资产目录。 |
| `sgl-learning-materials-tutorial/` | 你正在读的这套学习手册本身，按 `manifest.json` 拆成多篇讲义。 |

## 4. 核心概念与源码讲解

### 4.1 根目录的三块基石：README / LICENSE / .gitignore

#### 4.1.1 概念说明

仓库根目录下只有三个文件，它们构成了整个项目的「地基」：

- **`README.md`**：仓库的「门面」与「目录页」。GitHub 打开仓库时默认展示的就是它。在本仓库里，它几乎是**唯一需要人读的入口**——所有资料都被它索引。
- **`LICENSE`**：开源许可证。本仓库用 MIT，表示「欢迎复制、修改、再分发，但请保留版权声明，且不承担任何担保责任」。
- **`.gitignore`**：一个纯文本清单，告诉 git「这些文件不要纳入版本管理」。它让仓库保持干净，不被操作系统自动生成的临时文件污染。

注意一个重要事实：**这个仓库的根目录没有任何可执行代码**（没有 `main.py`、`package.json`、`Cargo.toml` 等）。这印证了 u1-l1 的结论——它是资料库，不是运行时。

#### 4.1.2 核心流程

这三块基石如何协同支撑一个「资料仓库」：

```text
1. 贡献者新增一份资料（如一张幻灯片 new.pdf）
       │
       ├── 文件本体放进 slides/ 或 blogs/
       └── 在 README.md 对应分区追加一行链接
2. .gitignore 负责把 .DS_Store 等垃圾文件挡在 git 之外
3. LICENSE 声明整个资料库（含 README、slides、blogs）都遵循 MIT
4. 访问者只需读 README.md，即可顺着链接找到所有资料
```

换句话说：**README 是枢纽，LICENSE 管法务，.gitignore 管卫生**。

#### 4.1.3 源码精读

`README.md` 第一行就是仓库标题，点明「这是学习 SGLang 的资料集合」：

- [README.md:L1](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L1) —— 仓库标题 `# Materials for learning SGLang`，是 GitHub 仓库主页默认展示的首行。

`.gitignore` 全文只有一行，忽略 macOS 在文件夹里自动生成的 `.DS_Store`：

- [.gitignore:L1](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/.gitignore#L1) —— 内容为 `.DS_Store`，含义是「不要把 macOS 的目录元数据文件提交进仓库」。

`LICENSE` 前三行声明许可证类型与版权方：

- [LICENSE:L1-L3](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/LICENSE#L1-L3) —— 标注 `MIT License` 与 `Copyright (c) 2024 sgl-project`，即版权归 sgl-project 组织，采用 MIT 许可。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是亲眼确认根目录「只有三个文件、且无代码」。

1. **实践目标**：确认根目录文件构成，理解仓库是「文档优先」。
2. **操作步骤**：在仓库根目录执行下面的命令，列出根目录所有条目（含隐藏文件）。
   ```bash
   ls -la
   ```
3. **需要观察的现象**：输出里应当只有 `.git/`、`.gitignore`、`LICENSE`、`README.md`、`blogs/`、`slides/`、`sgl-learning-materials-tutorial/` 这几项。
4. **预期结果**：**看不到任何 `.py` / `.js` / `.ts` / `.go` 等源代码文件，也看不到 `package.json` / `requirements.txt` 等构建文件**。这印证了「资料库无运行时代码」。
5. 说明：本实践为只读操作，不会修改任何文件。

#### 4.1.5 小练习与答案

**练习 1**：为什么一个「没有代码」的仓库仍然需要 `.gitignore`？
> **参考答案**：因为贡献者在本地用 macOS、编辑器等工具时，系统会生成 `.DS_Store`、`.swp` 等临时文件。`.gitignore` 把它们挡在版本管理之外，避免污染提交历史。本仓库的 `.gitignore` 至少忽略了 `.DS_Store`。

**练习 2**：有人想把这仓库的内容搬进自己的产品里商用，`LICENSE` 允许吗？需要履行什么义务？
> **参考答案**：MIT 许可证允许商用、修改、再分发。义务只有一条：在副本中保留版权声明与本许可声明（即 `Copyright (c) 2024 sgl-project` 那段）。MIT 不要求开源衍生代码，也不提供任何担保。

---

### 4.2 blogs 目录：长文博客与它的配图

#### 4.2.1 概念说明

`blogs/` 目录存放**真正写在仓库里的长文博客**。这与 README 中「Blog」区段里大量指向 `lmsys.org`、`pytorch.org` 的**外链博客**不同——那些博客的正文不在本仓库，只有这里的博客是「本地资产」。

事实上，整个 `blogs/` 目录里只有一篇 Markdown 正文：

- `blogs/Efficient LLM Deployment and Serving.md` —— 这是**仓库里唯一一篇 `.md` 博客**，回顾了 2024 年 10 月 16 日的首届 LMSYS 线上 meetup。

此外还有一个子目录 `blogs/docs/figs/`，里面是这篇博客用到的 **9 张 PNG 配图**。博客正文通过「相对路径」引用这些图，所以图必须和博客放在同一个 `blogs/` 树下。

#### 4.2.2 核心流程

博客与配图的依赖关系如下：

```text
blogs/Efficient LLM Deployment and Serving.md   （正文，.md）
       │
       │  正文里写：![说明](docs/figs/xxx.png)
       │           ↑ 相对路径，从 blogs/ 出发
       ▼
blogs/docs/figs/*.png   （9 张配图）
       ├── 1016 meetup - team.png
       ├── 1016 meetup - SGLANG scheduler.png
       ├── 1016 meetup - Xgrammer benchmark.png
       └── ...（共 9 张）
```

关键点：**配图的路径是相对博客文件计算的**。博客在 `blogs/` 下，写 `docs/figs/team.png` 就指 `blogs/docs/figs/team.png`。这也是为什么图片必须和博客「绑在一起」放在同一个目录树里，而不能随意挪动。

#### 4.2.3 源码精读

博客正文第一行是标题，点明这是一篇 meetup 回顾：

- [blogs/Efficient LLM Deployment and Serving.md:L1](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L1) —— 标题 `A Look Back at the Efficient LLM Deployment and Serving Meetup ...`，说明本文回顾的是 2024-10-16 的首届 meetup。

紧接着第 3 行就用相对路径插入了第一张配图（团队合影）：

- [blogs/Efficient LLM Deployment and Serving.md:L3](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L3) —— Markdown 图片语法 `![Meetup Team](docs/figs/1016%20meetup%20-%20team.png)`。注意路径里的 `%20` 是空格的 URL 编码（文件名 `1016 meetup - team.png` 含空格）。

README 中也正好有一行链接指向这篇博客，并标注了日期：

- [README.md:L86](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L86) —— `[2024-10-16] [Review of the first LMSYS online meetup...](blogs/Efficient%20LLM%20Deployment%20and%20Serving.md)`。这是 README 里少数指向**仓库内 `.md` 文件**的链接（不是外链），证实它确实是本地资产。

> 提示：文件名里的空格在链接中要编码成 `%20`。如果你在本地用 `git ls-files` 看到的是带空格的原名，而在 README/链接里看到 `%20`，指的其实是同一个文件。

#### 4.2.4 代码实践

这是一个**目录清点型实践**，目标是确认「博客只有一篇、配图有 9 张」。

1. **实践目标**：列出 `blogs/` 下所有文件并按扩展名归类。
2. **操作步骤**：在仓库根目录执行：
   ```bash
   git ls-files blogs/
   ```
3. **需要观察的现象**：输出应包含 1 个 `.md` 文件（`Efficient LLM Deployment and Serving.md`）和 9 个 `.png` 文件（都在 `blogs/docs/figs/` 下）。
4. **预期结果**：`.md` 正文恰好 1 篇，`.png` 配图恰好 9 张，共 10 个文件。如果你再执行 `git ls-files blogs/ | grep -c '\.png$'`，应得到 `9`。
5. 说明：这些都是 git 跟踪的真实文件；本实践只读不写。

#### 4.2.5 小练习与答案

**练习 1**：博客引用 `docs/figs/team.png` 时，这个路径的起点是仓库根目录还是博客所在目录？
> **参考答案**：是**博客所在目录**（即 `blogs/`）。相对路径从引用它的文件算起，所以 `docs/figs/team.png` 实际指向 `blogs/docs/figs/team.png`。

**练习 2**：如果把 `blogs/docs/figs/` 整个目录移动到仓库根目录改名为 `figs/`，博客里的图片还能正常显示吗？
> **参考答案**：不能。因为博客正文写死的是 `docs/figs/...`，目录移动后相对路径失效。要让图片恢复显示，要么把目录移回去，要么修改博客正文里所有的图片路径。这说明「正文与配图必须一起搬动」。

---

### 4.3 slides 目录：多格式幻灯片资产

#### 4.3.1 概念说明

`slides/` 是本仓库**体量最大**的目录，共 27 个文件，集中了大部分仓库内的可见资料。它的文件格式是混合的：

| 扩展名 | 数量 | 含义 |
| --- | --- | --- |
| `.pdf` | 25 | 可移植文档格式，幻灯片的主要载体，任何系统都能打开。 |
| `.png` | 1 | 位图图片，即 `adoption.png`（SGLang 被采纳的示意图）。 |
| `.pptx` | 1 | PowerPoint 源文件，即 `sglang_adoption_logo.pptx`——**仓库里唯一的 `.pptx` 文件**。 |

文件命名遵循「**事件前缀 + 主题**」的惯例，便于按活动归类，例如：

- `lmsys_1st_meetup_*.pdf` —— 第一届 LMSYS meetup 的若干主题。
- `amd_meetup_*.pdf` —— AMD SGLang meetup 的若干主题。
- `sglang_*.pdf` / `sglang-*.pdf` —— 各种独立的 SGLang 主题分享。

#### 4.3.2 核心流程

幻灯片如何与 README 联动：

```text
slides/xxx.pdf  （文件本体存放在此）
       ▲
       │  README 在 Slides 区段里写相对路径
       │  例：(slides/sglang_pytorch_2025.pdf)
       │
README.md 的 ## Slides 分区   ← 访问者点击即下载/预览本地 PDF
```

也就是说：**`slides/` 存「文件」，README 存「索引」**。README 的 Slides 区段用相对路径（`slides/...`）指向本地文件，而 Videos、Paper 等区段则多指向外部 `https://` 链接。这正好对应 u1-l1 提到的「看链接是否以 `https://` 开头来判断资料归属」。

#### 4.3.3 源码精读

README 用 `## Slides` 一行开启幻灯片区段：

- [README.md:L32](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L32) —— 区段标题 `## Slides`。

区段内每条记录的格式是 `[日期] [标题](本地相对路径)`，例如第一条：

- [README.md:L34](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L34) —— `[2025-10-22] [PyTorch Conference 2025 SGLang](slides/sglang_pytorch_2025.pdf)`。链接以 `slides/` 开头（不是 `https://`），说明这是一份**仓库内 PDF**。

对比来看，同一区段里也有外链条目，例如指向 `gamma.app` 的分享：

- [README.md:L52](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L52) —— `[2025-07-08] [SGLang: An Efficient Open-Source Framework...](https://gamma.app/docs/...)`。链接以 `https://` 开头，说明这份幻灯片的**正文不在本仓库**，`slides/` 里不会有对应文件。

> 这就是判断「这份幻灯片在不在 `slides/` 目录里」的最快方法：看 README 里它对应的链接是 `slides/xxx.pdf` 还是 `https://...`。

#### 4.3.4 代码实践

这是一个**格式统计型实践**，目标是亲手验证 `slides/` 的格式分布。

1. **实践目标**：统计 `slides/` 下各扩展名的文件数量。
2. **操作步骤**：在仓库根目录执行：
   ```bash
   git ls-files slides/ | sed 's/.*\.//' | sort | uniq -c
   ```
   （命令含义：列出 `slides/` 下所有文件 → 取最后一个 `.` 之后的扩展名 → 排序 → 去重计数。）
3. **需要观察的现象**：输出三行，分别是 `pdf`、`png`、`pptx` 各自的计数。
4. **预期结果**：`25 pdf`、`1 png`、`1 pptx`，合计 27。其中唯一的 `.pptx` 是 `sglang_adoption_logo.pptx`，唯一的 `.png` 是 `adoption.png`。
5. 说明：这是只读统计命令，不修改任何文件。如果你在 Windows PowerShell 下，可改用 `git ls-files slides/` 后人工数扩展名。

#### 4.3.5 小练习与答案

**练习 1**：README 的 Slides 区段里有一条 `[Cache-Aware Load Balancer in SGLang](slides/sglang-router.pdf)`（约第 62 行）。请判断：这份幻灯片的文件本体在不在本仓库的 `slides/` 目录里？
> **参考答案**：**在**。因为链接写的是 `slides/sglang-router.pdf`（相对路径，不以 `https://` 开头），说明它是仓库内资产，可以在 `slides/` 目录下找到同名文件。

**练习 2**：仓库里唯一的 `.pptx` 文件叫什么？它与 `slides/adoption.png` 是什么关系？
> **参考答案**：唯一的 `.pptx` 是 `slides/sglang_adoption_logo.pptx`。从命名（都含 `adoption`）推测，`adoption.png` 很可能是从这个 PowerPoint 源文件导出的一张图片，用于 README 或文档中展示「SGLang 被哪些组织采纳」。

---

### 4.4 sgl-learning-materials-tutorial 目录：本手册的家

#### 4.4.1 概念说明

`sgl-learning-materials-tutorial/` 是一个**特殊的目录——它存放的不是 SGLang 的原始资料，而是你正在读的这套「学习手册」本身**。换句话说，前三类内容（README、blogs、slides）是 SGLang 团队贡献的原始学习资料，而这个目录是围绕这些资料整理出来的**教学讲义**。

这个目录与前面三个目录有一个本质区别：

| 目录 | 内容性质 | 谁产生 |
| --- | --- | --- |
| `blogs/` `slides/` | SGLang 原始资料 | SGLang 团队 / meetup 演讲者 |
| `sgl-learning-materials-tutorial/` | 围绕原始资料整理的讲义 | 本手册的作者（按大纲生成） |

#### 4.4.2 核心流程

讲义目录由一份「大纲清单」驱动，拆分成多篇讲义：

```text
sgl-learning-materials-tutorial/
├── manifest.json          ← 大纲：定义所有单元与讲义的结构
├── u1-l1-project-overview.md        ← 第 1 单元第 1 讲（已存在）
├── u1-l2-directory-structure.md     ← 第 1 单元第 2 讲（就是本文件）
└── ...后续讲义...
```

`manifest.json` 是整本手册的「目录页」：它规定每篇讲义的编号（如 `u1-l2`）、标题、文件名、依赖关系（`depends_on`）以及要覆盖的最小模块。每篇讲义文件名都以编号开头（`u1-l1-...`、`u1-l2-...`），便于排序与交叉引用。

> 概念辨析：仓库根目录的 `README.md` 是 **SGLang 资料**的导航，而本目录的 `manifest.json` 是 **学习讲义**的大纲——两者层级不同，不要混淆。

#### 4.4.3 源码精读

本目录没有「源码」可读，但你可以直接观察它的物理结构。一个值得注意的细节是：**讲义之间的依赖关系是显式的**。例如本讲 `u1-l2` 在大纲里声明 `depends_on: ["u1-l1"]`，表示「先读 u1-l1，再读 u1-l2」。这也解释了为什么本讲开头可以直接引用上一讲建立的「路标仓库」概念，而不必重新解释 SGLang 是什么。

- 关于本目录的 `manifest.json`：它定义了 `u1`（入门）到 `u4`（专家）四个单元，每篇讲义都对应一个 `uN-lM-*.md` 文件。本文件 `u1-l2-directory-structure.md` 即出自该大纲。（`manifest.json` 为本手册生成物，请直接在本目录下打开查看，不单独给出永久链接。）

#### 4.4.4 代码实践

这是一个**结构自查型实践**，目标是确认本讲义在目录中的位置。

1. **实践目标**：列出讲义目录的所有 `.md` 文件，找到本讲及其前序讲义。
2. **操作步骤**：在仓库根目录执行：
   ```bash
   git ls-files sgl-learning-materials-tutorial/ | grep '\.md$'
   ```
   （若该目录尚未被 git 跟踪，可改用 `find sgl-learning-materials-tutorial/ -name '*.md'`。）
3. **需要观察的现象**：输出里应能看到 `u1-l1-project-overview.md` 与本讲 `u1-l2-directory-structure.md`，以及一份 `manifest.json`。
4. **预期结果**：讲义按 `uN-lM` 编号命名，编号越小越靠前。本讲 `u1-l2` 紧接在 `u1-l1` 之后，符合「先认识项目定位（u1-l1），再看目录结构（u1-l2）」的学习顺序。
5. 说明：仅列出文件，不修改任何内容。

#### 4.4.5 小练习与答案

**练习 1**：本目录的 `manifest.json` 与仓库根目录的 `README.md`，哪一个才是「SGLang 资料」的导航？
> **参考答案**：是**根目录的 `README.md`**。`manifest.json` 只是这套学习讲义的内部大纲，导航的是「讲义」，而不是 SGLang 的原始资料。原始资料的导航始终是 README。

**练习 2**：为什么讲义文件名都以 `u1-l2-...` 这样的编号开头，而不是用中文标题做文件名？
> **参考答案**：编号前缀有两个好处——一是保证文件在目录里按学习顺序自然排序（`u1-l1` 在 `u1-l2` 之前）；二是全小写英文 + 连字符的命名跨平台安全、便于在永久链接和命令行中引用。中文标题只用在讲义正文里展示。

## 5. 综合实践

把本讲全部内容串起来，完成下面这个「仓库结构速写」任务。

**任务**：为 `sgl-learning-materials` 仓库画一张带注释的目录树，并为每个目录写一句话说明它存放的文件类型；最后回答两个「唯一」问题。

**操作步骤**：

1. 在仓库根目录执行 `ls -la` 与 `git ls-files`，对照真实文件补全下面这棵树。
2. 在每个目录后面用一句话写明它存放的文件类型与格式。
3. 回答：仓库里**唯一一篇 `.md` 博客**是什么？**唯一一个 `.pptx` 文件**又是什么？

**参考骨架**（请你自己补全注释）：

```text
sgl-learning-materials/
├── README.md          # ____________________________
├── LICENSE            # ____________________________
├── .gitignore         # ____________________________
├── blogs/             # ____________________________
│   └── docs/figs/     # ____________________________
├── slides/            # ____________________________
└── sgl-learning-materials-tutorial/  # _____________
```

**预期结果**（用于自查）：

- 根目录三文件分别是：导航索引（`README.md`）、MIT 许可证（`LICENSE`）、忽略 `.DS_Store` 的清单（`.gitignore`）。
- `blogs/`：1 篇 `.md` 长文博客 + `docs/figs/` 下 9 张 PNG 配图。
- `slides/`：27 个文件（25 PDF + 1 PNG + 1 PPTX）。
- `sgl-learning-materials-tutorial/`：本学习手册（`manifest.json` + 各篇 `uN-lM-*.md` 讲义）。
- **唯一一篇 `.md` 博客**：`blogs/Efficient LLM Deployment and Serving.md`。
- **唯一一个 `.pptx` 文件**：`slides/sglang_adoption_logo.pptx`。

## 6. 本讲小结

- 仓库根目录只有三块基石：`README.md`（导航枢纽）、`LICENSE`（MIT 许可，版权 sgl-project）、`.gitignore`（仅忽略 `.DS_Store`），且**不含任何可执行代码**。
- `blogs/` 存放**仓库内长文博客**，仅 1 篇 `Efficient LLM Deployment and Serving.md`，外加 `docs/figs/` 下 9 张 PNG 配图；博客用相对路径引用配图，二者必须绑定。
- `slides/` 是体量最大的目录，27 个文件 = 25 PDF + 1 PNG（`adoption.png`）+ 1 PPTX（`sglang_adoption_logo.pptx`，仓库唯一 `.pptx`），命名按「事件前缀」归类。
- 判断一份幻灯片是否在仓库内：看 README 里它的链接是 `slides/xxx.pdf`（本地）还是 `https://...`（外链）。
- `sgl-learning-materials-tutorial/` 是本学习手册的目录，由 `manifest.json` 驱动，讲义以 `uN-lM-*.md` 编号命名；它与根目录 `README.md` 是两个不同层级的「目录页」。

## 7. 下一步学习建议

- **下一步必读 u1-l3《把 README 当作导航地图》**：本讲只看了 README 的「分区标题」（Slides / Blog / ...），下一讲会深入每个分区，教你按主题（调度、受限解码、MLA、大规模部署）在 README 里快速定位资料。
- **动手熟悉命令**：把本讲出现的 `git ls-files`、`ls -la` 多用几次，建立「看一眼目录就知道里面装什么」的直觉，这对阅读后续所有讲义都有帮助。
- **延伸阅读（可选）**：直接打开 `blogs/Efficient LLM Deployment and Serving.md` 通读一遍，它是仓库里少数「正文完全在本地」的资料，u3-l1 会专门精读它；现在先混个脸熟即可。
