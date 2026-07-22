# 把 README 当作导航地图

## 1. 本讲目标

前两讲我们做了两件事：u1-l1 认清了 `sgl-learning-materials` 是一个「资料聚合库」（不含运行时代码），u1-l2 打开了仓库的「抽屉」看清了目录结构。但到现在为止，我们只把 README 当成一个「标题加一堆链接」的文件——**还没有真正学会用它来找资料**。

本讲要做的，是把 README 当成一张**导航地图**来读：先认清地图上的「大区」（Announcement / Slides / Blog / Videos / Paper / Documentation），再看懂每个大区内部的「街道编排方式」（按事件归类），最后学会一个万能技能——**按主题反查**，即「我想了解调度 / MLA / 受限解码，该去 README 的哪一行」。

读完本讲，你应当能够：

1. 说出 README 的六大顶层分区，以及每个分区各自存放什么类型的资料。
2. 看懂 Slides 区段「按事件归类」的子区段组织法（meetup / 学术会议 / biweekly developer sync）。
3. 一眼分辨某条资料是**仓库内链接**（本地 PDF/MD）还是**外部链接**（lmsys.org、pytorch.org 等）。
4. 针对「调度、受限解码、MLA、大规模部署」四个主题，分别说出能从 README 的哪个分区、哪一行找到对应资料。

> 本讲依然不含数学与算法，只练「读地图」的基本功。从 u2 开始，我们才会真正走进这些资料、按主题深入阅读。

## 2. 前置知识

本讲默认你已经读过 u1-l1 与 u1-l2，掌握了下面两点（不重复展开）：

- **判断资料归属看链接是否以 `https://` 开头**：以 `https://` 开头的是外部链接（资料正文不在本仓库），以 `slides/`、`blogs/` 等相对路径开头的是仓库内资产（文件就在本仓库里）。
- **README 是仓库的「目录页」**：它是整个资料库的导航枢纽，几乎所有资料都靠它索引。

在此基础上，本讲再引入四个读 README 时会反复用到的小概念：

| 术语 | 通俗解释 |
| --- | --- |
| 区段（section） | README 里用 `##` 开头划分的大块，如 `## Slides`、`## Blog`，相当于地图上的「大区」。 |
| 子区段（subsection） | 在某个大区内部用 `###` 进一步划分的小块，如 Slides 区段下的 `### AMD SGLang Meetup`，相当于「街道」。 |
| 事件归类 | 把同一场活动（某次 meetup、某次学术会议）的若干张幻灯片归到同一个子区段下，便于成组阅读。 |
| 日期标注 | 每条资料前的 `[YYYY-MM-DD]` 前缀，标注该资料产生的日期，是按时间排序与检索的关键。 |

如果你对「相对路径」这个概念还不熟，请回头读 u1-l2 的 4.2 节（博客如何用相对路径引用配图）——本讲判断「内链」时要用到同样的思路。

## 3. 本讲源码地图

本讲**只读一个文件，但它就是整张地图**：

```
sgl-learning-materials/
└── README.md   ← 本讲的唯一研究对象：整本资料库的导航索引
```

| 路径 | 在本讲中的作用 |
| --- | --- |
| `README.md` | 既是被分析的对象，也是后续所有讲义（u2 起）要反复回头查阅的「索引」。本讲带你把它从上到下读成一张结构化的地图。 |

> 提醒：这和 u1-l2 的视角不同。u1-l2 把 README 当成「根目录三块基石之一」匆匆带过；本讲要**逐区段拆开**它的内部结构。

## 4. 核心概念与源码讲解

### 4.1 README 的整体分区骨架

#### 4.1.1 概念说明

打开 README，你会发现它由**六个顶层区段**（`##` 标题）自上而下排列。把这六个区段记牢，就等于掌握了整张地图的骨架：

| 区段（README 中的标题） | 存放什么 | 资料形态 |
| --- | --- | --- |
| `## Announcement` | 官方重大里程碑公告（版本发布、重要合作、行业采纳） | 短段落 + 链接 |
| `## Slides` | 幻灯片（讲解 SGLang 各项技术的 PDF/PPT） | 仓库内 PDF 为主 + 少量外链 |
| `## Blog` | 技术博客（原理深入、版本说明） | **几乎全是外链**（lmsys.org 等） |
| `## Videos` | 演讲与开发者例会的录像 | **全是外链**（YouTube） |
| `## Paper` | 学术论文 | **全是外链**（arXiv） |
| `## Documentaion` | 官方文档站入口 | 单条外链 |

一个关键的总体规律：**越靠上的区段越「时间敏感」**（Announcement 讲最近的大事），**越靠下的区段越「稳定长青」**（Paper、Documentation 是长期不变的参考）。所以读 README 的正确姿势是：新人先看 Announcement 了解「最近发生了什么」，再做主题检索时主要翻 Slides 与 Blog。

> 小发现：README 第 189 行的区段标题写成了 `## Documentaion`——这是「Documentation」的拼写错误。它不影响链接功能，但说明这个仓库也像普通项目一样会有笔误；以后你看到这个标题不要以为是另一个词。

#### 4.1.2 核心流程

把六个区段按「使用场景」串起来，阅读流程是这样的：

```text
       我想了解 SGLang？
              │
   ┌──────────┴──────────┐
   ▼                     ▼
想看「最近大事」        想学「某项技术」
   │                     │
   ▼                     ▼
## Announcement       在 ## Slides / ## Blog 里
（里程碑、采纳）        按主题/事件检索
                              │
                              ▼
                    想看录像 → ## Videos
                    想读论文 → ## Paper
                    想查用法 → ## Documentaion
```

换句话说：**Announcement 是「新闻头条」，Slides/Blog 是「主题课本」，Videos/Paper/Documentation 是「延伸出口」**。

#### 4.1.3 源码精读

README 用六个 `##` 标题把骨架立起来。按出现顺序：

- [README.md:L5](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L5) —— `## Announcement`，公告区段起点，下面按月份（`### May 2025`、`### March 2025`…）倒序排列里程碑。
- [README.md:L32](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L32) —— `## Slides`，幻灯片区段起点，是仓库内**体量最大、本地资产最集中**的区段。
- [README.md:L112](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L112) —— `## Blog`，博客区段起点，下面又按来源（LMSYS Org / AMD / Meta PyTorch / Microsoft Azure）分了子区段。
- [README.md:L148](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L148) —— `## Videos`，视频区段起点，全部指向 YouTube。
- [README.md:L184](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L184) —— `## Paper`，论文区段，目前仅一条 NeurIPS 24 论文链接。
- [README.md:L189](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L189) —— `## Documentaion`（原文如此，拼写有误），仅一条指向官方文档站 `https://sgl-project.github.io/` 的链接。

注意一个细节：**Blog 的子区段用的是 `##`（二级标题），与 `## Blog` 同级**（如 L114 的 `## LMSYS Org`）；而 Slides 的子区段用的是 `###`（三级标题），嵌套在 `## Slides` 之下。这是 README 本身层级写法不完全统一的地方——读图时不必纠结，认得「这是某个大区下面再分的一组」即可。

#### 4.1.4 代码实践

这是一个**结构清点型实践**，目标是亲手把六大区段标题抓出来。

1. **实践目标**：列出 README 中所有 `##` 二级标题，确认骨架。
2. **操作步骤**：在仓库根目录执行：
   ```bash
   grep -n '^## ' README.md
   ```
   （命令含义：在 README.md 中找所有以 `## ` 开头的行，并显示行号 `-n`。）
3. **需要观察的现象**：输出大约 9 行（含 Blog 下的几个同级子标题），都带行号。
4. **预期结果**：能看到 `## Announcement`、`## Slides`、`## Blog`、`## LMSYS Org`、`## AMD`、`## Meta PyTorch`、`## Microsoft Azure`、`## Videos`、`## Paper`、`## Documentaion` 等。其中 `## LMSYS Org`、`## AMD`、`## Meta PyTorch`、`## Microsoft Azure` 四个其实是 Blog 大区下的子分组（因写成 `##` 而被一起抓出）。
5. 说明：只读操作，不修改文件。

#### 4.1.5 小练习与答案

**练习 1**：如果你只想看「SGLang 最近有什么大新闻」，应该读 README 的哪个区段？为什么？
> **参考答案**：读 `## Announcement`（约 L5）。它按月份倒序列出里程碑（版本发布、重要合作、行业采纳），是仓库里专门放「时效性大事」的区段。

**练习 2**：为什么 `## Paper` 和 `## Documentaion` 几乎不放仓库内文件？
> **参考答案**：因为论文的正文在 arXiv、用法文档在官方文档站 `sgl-project.github.io`，这些是体量大、需要独立平台承载的内容。本仓库只负责**给出指向它们的入口**（外链），不把正文复制进来——这正是「资料聚合库」的定位。

---

### 4.2 Slides 区段的「事件归类」组织法

#### 4.2.1 概念说明

`## Slides` 是仓库里**最值得花时间**的区段：它集中了绝大部分本地 PDF，但条目很多（二十余条）。如果所有幻灯片平铺在一起会很难找，所以 README 用**「事件归类」**来组织——用 `###` 三级标题把同一场活动的若干张幻灯片归到一组。

你可以把 Slides 区段里的事件大致分成三类：

| 事件类型 | 例子（子区段标题） | 特点 |
| --- | --- | --- |
| meetup（社区聚会） | `### AMD SGLang Meetup`、`### The first LMSYS online meetup...`、`### Hyperbolic in-person meetup` | 围绕某次线下/线上聚会，往往一场包含多个主题幻灯片 |
| 学术会议 / Dev Day | `### CUDA Tech Briefing at NVIDIA GTC 2025`、`### AMD Advancing AI 2024`、`### GPU MODE` | 在行业大会上的主题分享 |
| biweekly developer sync（双周开发者例会） | `### SGLang Biweekly Meeting` | 开发者定期同步，主题分散、更新频繁 |

还有一个兜底子区段 `### Other`（L108），放那些不属于某场具体活动、但又值得收录的独立幻灯片（如 `sglang_v0_2.pdf`）。

#### 4.2.2 核心流程

每个子区段内部的条目遵循统一格式，理解了这个格式就能快速扫读：

```text
### <事件名>                    ← 子区段标题（一场活动）
[YYYY-MM-DD] [幻灯片标题](链接)  ← 一条资料：日期 + 标题 + 链接
[YYYY-MM-DD] [幻灯片标题](链接)  ← 同一场活动的另一张
...
```

三个要点：

1. **日期前缀 `[YYYY-MM-DD]`**：标注该幻灯片的演讲日期，便于按时间定位。
2. **同一子区段内的条目通常同属一场活动**：日期相同或相近，主题互补。
3. **条目整体大致倒序排列**：越新的越靠上（区段顶部是 2025-10，底部是 2024）。

#### 4.2.3 源码精读

Slides 区段下用 `###` 划出的事件子区段（节选）：

- [README.md:L38](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L38) —— `### AMD SGLang Meetup`，2025-08-22 的 AMD meetup，下面 5 张幻灯片（roadmap、大规模部署、highlights、wave、AITER/MoRI）。
- [README.md:L74](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L74) —— `### The first LMSYS online meetup: Efficient LLM Deployment and Serving`，2024-10-16 的首届 LMSYS meetup，下面 5 张幻灯片（概览、受限解码、MLA、MLC、XGrammar）外加 1 篇回顾博客。
- [README.md:L92](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L92) —— `### SGLang Biweekly Meeting`，双周开发者例会，主题最杂（RLHF、调度、权重热更新、Router、量化、双稀疏、MLA…），是「按主题翻资料」时最常光顾的子区段。
- [README.md:L108](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L108) —— `### Other`，兜底区，收录独立幻灯片如 `sglang_v0_2.pdf`（L110）。

看一条具体的条目格式——首届 meetup 下的「SGLang Overview & CPU Overhead Hiding」：

- [README.md:L76](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L76) —— `[2024-10-16] [SGLang Overview & CPU Overhead Hiding](slides/lmsys_1st_meetup_sglang.pdf)`。这是「事件归类」的标准写法：日期 `[2024-10-16]` + 标题 `[SGLang Overview & CPU Overhead Hiding]` + 本地链接 `(slides/lmsys_1st_meetup_sglang.pdf)`。文件名前缀 `lmsys_1st_meetup_` 正好和它所在的子区段（首届 LMSYS meetup）对应，命名与归类一致。

#### 4.2.4 代码实践

这是一个**子区段清点型实践**，目标是看清 Slides 区段的「街道划分」。

1. **实践目标**：列出 Slides 区段下所有 `###` 事件子区段及其行号。
2. **操作步骤**：在仓库根目录执行：
   ```bash
   awk '/^## Slides/{f=1} f&&/^### /{print NR": "$0} /^## Blog/{f=0}' README.md
   ```
   （命令含义：从 `## Slides` 行开始打印所有 `### ` 开头的行，直到遇到 `## Blog` 为止。）
3. **需要观察的现象**：输出约 10 行 `###` 子区段标题，每行带行号。
4. **预期结果**：能看到 `### AMD SGLang Meetup`、`### AWS AI Hours Singapore`、`### CUDA Tech Briefing at NVIDIA GTC 2025`、`### Hyperbolic in-person meetup`、`### CAMEL-AI Hackathon...`、`### GPU MODE`、`### The first LMSYS online meetup...`、`### AMD Advancing AI 2024`、`### SGLang Biweekly Meeting`、`### Other`。它们正好分别属于「meetup / 学术会议 / biweekly」三类事件。
5. 说明：只读统计，不修改文件。若不熟悉 `awk`，可改用 `grep -n '^### ' README.md` 人工筛掉非 Slides 部分。

#### 4.2.5 小练习与答案

**练习 1**：`### SGLang Biweekly Meeting`（L92）下面的条目，日期跨度从 2024-09 到 2025-04。这说明它和 `### AMD SGLang Meetup`（L38）在「归类逻辑」上有什么不同？
> **参考答案**：Biweekly Meeting 是**周期性、长期持续**的开发者例会，所以同一子区段下会累积跨越数月、主题各异的多条记录；而 AMD SGLang Meetup 是**单次活动**，其下条目日期基本相同（都是 2025-08-22），主题围绕那一场聚会。前者是「按系列归类」，后者是「按单场活动归类」。

**练习 2**：一张名为 `lmsys_1st_meetup_mlcengine.pdf` 的幻灯片，最可能出现在 README 的哪个子区段？为什么？
> **参考答案**：最可能出现在 `### The first LMSYS online meetup` 子区段（约 L74 起）。因为文件名前缀 `lmsys_1st_meetup_` 与该子区段对应的事件一致。事实上它就在 L82。

---

### 4.3 内链与外链的辨别

#### 4.3.1 概念说明

u1-l1 已经教过一条总原则：**看链接是否以 `https://` 开头**。本讲把它落到 README 的具体场景里，并强调一点——这条规则**在每个区段都适用**，不只是 Slides。

把链接分成两类，读图时心里就有数：

| 类型 | 链接形态 | 资料在哪 | 例子 |
| --- | --- | --- | --- |
| 内链（仓库内） | `slides/xxx.pdf`、`blogs/xxx.md`、`./slides/xxx.pdf` | 文件就在本仓库，可离线打开 | `(slides/lmsys_1st_meetup_sglang.pdf)` |
| 外链（外部） | `https://...` | 正文在外部站点，需联网 | `(https://lmsys.org/blog/...)` |

一个区段里两种链接常常**混在一起**。例如 Slides 区段大部分是内链 PDF，但也夹杂指向 `gamma.app`、`docs.google.com` 的外链——那些幻灯片的正文并不在仓库里。

#### 4.3.2 核心流程

判断一条资料归属的决策流程：

```text
        看到一条 README 链接 (...)
                 │
   ┌─────────────┴─────────────┐
   ▼                           ▼
以 slides/ blogs/ ./ 开头？     以 https:// 开头？
   │                           │
   ▼                           ▼
 内链：仓库内资产              外链：正文在外部站点
 （可在本地 git ls-files       （需联网访问；slides/ 里
   找到同名文件）                不会有对应文件）
```

> 实用小窍门：当一条 Slides 记录是外链（如指向 `gamma.app`），你想确认仓库里到底有没有这份幻灯片的本地副本时，最快的办法是 `git ls-files slides/ | grep 关键词`——若搜不到，就证明它确实是纯外链。

#### 4.3.3 源码精读

下面用**三组对比**把内链与外链看清楚。

**对比一：同为幻灯片，一内一外。**

- [README.md:L76](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L76) —— `(slides/lmsys_1st_meetup_sglang.pdf)`，相对路径 → **内链**，`slides/` 下有同名文件。
- [README.md:L52](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L52) —— `(https://gamma.app/docs/SGLang-AWS-AI-Hours-...)`，`https://` 开头 → **外链**，幻灯片正文在 gamma.app，仓库内无对应文件。

**对比二：同为「SGLang DeepSeek MLA」，一次内链一次外链。** 这是「同主题跨事件」的好例子：

- [README.md:L80](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L80) —— `[SGLang DeepSeek MLA](slides/lmsys_1st_meetup_deepseek_mla.pdf)`，内链，首届 meetup 的版本。
- [README.md:L106](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L106) —— `[SGLang DeepSeek MLA](https://docs.google.com/presentation/...)`，外链，Biweekly Meeting 的版本（Google Slides）。

**对比三：Blog/Videos/Paper 区段几乎全是外链。**

- [README.md:L116](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L116) —— `(https://lmsys.org/blog/2025-05-05-large-scale-ep/)`，博客正文在 lmsys.org，外链。
- [README.md:L158](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L158) —— `(https://www.youtube.com/watch?v=_mzKptPj0hE)`，YouTube 视频，外链。
- [README.md:L186](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674f67a85bc8915d/README.md#L186) —— `(https://arxiv.org/abs/2312.07104)`，arXiv 论文，外链。

唯一一个例外（仓库内 `.md` 博客）也很有辨识度：

- [README.md:L86](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L86) —— `(blogs/Efficient%20LLM%20Deployment%20and%20Serving.md)`，相对路径（`blogs/` 开头）→ **内链**，是 Blog/Slides 里少有的「正文真在仓库内」的资料（u3-l1 会专门精读它）。

#### 4.3.4 代码实践

这是一个**主题反查型实践**（本讲的核心实践任务），目标是亲手做一张「主题 → README 位置」速查表。

1. **实践目标**：针对「调度、受限解码、MLA、大规模部署」四个主题，分别在 README 中找到对应资料，记录它所在的**区段 / 子区段 / 行号 / 内外链**。
2. **操作步骤**：
   - 在仓库根目录用关键词检索，例如：
     ```bash
     grep -ni 'schedul\|FLPM\|overhead' README.md
     grep -ni 'constrained\|xgrammar\|structured\|JSON' README.md
     grep -ni 'MLA\|DeepSeek' README.md
     grep -ni 'expert parallel\|PD disagg\|large-scale\|large scale' README.md
     ```
   - 对每条命中结果，向上找到它属于哪个 `##` 区段和哪个 `###` 子区段。
   - 判断链接是内链还是外链。
3. **需要观察的现象**：每个主题都能命中多条记录，分布在 Slides 与 Blog 等不同区段。
4. **预期结果**：完成下表（参考答案见 5. 综合实践的骨架，此处先自己填）：

   | 主题 | 代表资料 | 所在区段 / 子区段 | 行号 | 内/外链 |
   | --- | --- | --- | --- | --- |
   | 调度 | SGLang Overview & CPU Overhead Hiding | Slides / 首届 LMSYS meetup | L76 | 内链 |
   | 调度 | A fair and efficient scheduling algorithm (FLPM) | Slides / Biweekly Meeting | L96 | 内链 |
   | 受限解码 | Faster Constrained Decoding | Slides / 首届 LMSYS meetup | L78 | 内链 |
   | 受限解码 | XGrammar 结构化生成 | Slides / 首届 LMSYS meetup | L84 | 内链 |
   | 受限解码 | Fast JSON Decoding / Compressed FSM | Blog / LMSYS Org | L124 | 外链 |
   | MLA | SGLang DeepSeek MLA（首届 meetup 版） | Slides / 首届 LMSYS meetup | L80 | 内链 |
   | MLA | SGLang DeepSeek MLA（Biweekly 版） | Slides / Biweekly Meeting | L106 | 外链 |
   | MLA | SGLang v0.3 Release: 7x Faster DeepSeek MLA | Blog / LMSYS Org | L120 | 外链 |
   | 大规模部署 | May 2025 EP + PD 公告（含 PyTorch Day China 幻灯片） | Announcement | L9 | 内外链混合 |
   | 大规模部署 | Large-scale Deployment of Emerging LLMs | Slides / AMD SGLang Meetup | L42 | 内链 |
   | 大规模部署 | Deploying DeepSeek with PD Disaggregation … 96 H100 | Blog / LMSYS Org | L116 | 外链 |

5. 说明：`grep -i` 忽略大小写；若命中行在某个 `###` 之下，则它属于该子区段，直到出现下一个同级标题为止。这是只读检索，不修改任何文件。

#### 4.3.5 小练习与答案

**练习 1**：README 里有一条 `[2024-10-05] [SGLang Double Sparsity](https://docs.google.com/presentation/...)`（L104）。它在仓库的 `slides/` 目录里有本地副本吗？怎么最快确认？
> **参考答案**：从链接是 `https://docs.google.com/...`（外链）判断，**大概率没有本地副本**。最快确认方法是执行 `git ls-files slides/ | grep -i sparsity`——若无输出，则证实它只是外链，仓库内不存在该文件。

**练习 2**：为什么说 `blogs/Efficient LLM Deployment and Serving.md`（L86）是 README 里「辨识度很高」的一条？
> **参考答案**：因为它是 README 中**极少数正文真正写在仓库内**的资料——链接以 `blogs/` 相对路径开头（内链），文件就在 `blogs/` 目录下。其余 Blog/Videos/Paper 条目几乎全是 `https://` 外链。所以只要看到 `blogs/` 开头，就知道这是一份「本地长文」。

---

### 4.4 日期标注惯例与「同主题跨事件」检索

#### 4.4.1 概念说明

README 的资料条目几乎都带 `[YYYY-MM-DD]` 日期前缀。这个看似不起眼的标注有两个作用：

1. **按时间排序**：同一区段/子区段内，条目大致按日期**倒序**排列（新的在上），便于先看最新进展。
2. **同名资料消歧**：同一主题（如「SGLang DeepSeek MLA」）会在不同时间、不同活动里被反复讲解。日期前缀帮你区分「这是哪一版、哪一场」。

第二点尤其重要：**SGLang 的很多技术主题不是一次讲完的**，而是随版本迭代在多次 meetup、多次 Biweekly 例会里逐步深入。所以「按主题检索」时，**不要只看一条**——同一个关键词可能横跨 Slides 的多个子区段，甚至同时出现在 Blog 里。

#### 4.4.2 核心流程

「按主题检索」的正确姿势：

```text
  想学某个主题（如 MLA）
         │
         ▼
  在 README 用关键词检索（grep -i）
         │
         ▼
  得到多条命中，分布在不同区段/子区段/日期
         │
         ├── 内链 PDF（仓库内）→ 可直接下载精读
         └── 外链（博客/Google Slides）→ 联网阅读，往往讲得更系统
         │
         ▼
  按「由浅入深」挑选阅读顺序：先看概览，再看版本博客，再看专题幻灯片
```

要点：**把同一主题的多条记录当成一个「资料簇」**，而不是各自孤立的一条。

#### 4.4.3 源码精读

以 **MLA** 为例，看同一个主题如何在 README 里多处出现：

- [README.md:L80](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L80) —— `[2024-10-16] [SGLang DeepSeek MLA]`（Slides / 首届 LMSYS meetup，内链）。首届 meetup 上的 MLA 专题分享。
- [README.md:L106](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L106) —— `[2024-09-21] [SGLang DeepSeek MLA]`（Slides / Biweekly Meeting，外链 Google Slides）。更早一次 Biweekly 例会上的 MLA 分享。
- [README.md:L64](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L64) —— `[2025-01-15] [SGLang DeepSeek Model Optimizations]`（Slides / Hyperbolic meetup，内链）。主题更宽泛的 DeepSeek 模型优化（含 MLA 相关内容）。
- [README.md:L120](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L120) —— `[2024-09-04] [SGLang v0.3 Release: 7x Faster DeepSeek MLA, ...]`（Blog / LMSYS Org，外链）。v0.3 发布博客，讲 MLA 带来 7 倍提速的原理与数据。

再看**日期格式的一个小坑**：大多数日期是规范的 `YYYY-MM-DD`（如 `[2024-10-16]`），但 Biweekly Meeting 下有几条用了**非零填充**写法，如 `[2025-1-25]`（L96）、`[2025-4-22]`（L94）——月份/日没补零。检索时若用严格的 `YYYY-MM-DD` 正则可能漏掉它们，所以用 `grep -i` 按关键词（如 `FLPM`、`RLHF`）搜更稳妥：

- [README.md:L96](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L96) —— `[2025-1-25] [A fair and efficient scheduling algorithm](slides/sglang-FLPM.pdf)`，日期写法不规范但内容是 FLPM 公平调度。

#### 4.4.4 代码实践

这是一个**主题簇检索型实践**，目标是体会「同主题跨事件」。

1. **实践目标**：在 README 中检索 `MLA` 与 `DeepSeek`，统计命中的条目数，并按区段归类。
2. **操作步骤**：
   ```bash
   grep -ni 'MLA\|DeepSeek' README.md
   ```
3. **需要观察的现象**：命中结果散落在 Announcement、Slides（多个子区段）、Blog 等多处，日期从 2024-09 跨到 2025。
4. **预期结果**：你会看到 MLA/DeepSeek 相关条目至少出现在 L9（Announcement，DeepSeek V3/R1 采纳）、L64（DeepSeek 模型优化幻灯片）、L80 与 L106（两版 MLA 幻灯片）、L116/L120/L130 等（博客与 AMD 文章）。这说明 MLA 是一个**横跨多区段、多次活动**的持续主题，单一一条记录不足以覆盖全貌。
5. 说明：这是只读检索命令；若想精确统计行数可加 `grep -ci`。

#### 4.4.5 小练习与答案

**练习 1**：为什么「按事件浏览」和「按主题检索」要结合使用？
> **参考答案**：按事件浏览（如通读 `### The first LMSYS online meetup` 下全部 5 张幻灯片）能让你看到**一场活动里多个主题如何配合**；按主题检索（如搜 `MLA`）能让你看到**同一主题在不同时间、不同活动里如何演进**。前者是「横切」，后者是「纵切」，两者结合才能既见森林又见树木。

**练习 2**：`[2025-1-25]`（L96）这种日期写法，在用脚本严格匹配时会带来什么小麻烦？如何规避？
> **参考答案**：它没有把月、日补零成 `2025-01-25`，所以用形如 `[0-9]{4}-[0-9]{2}-[0-9]{2}` 的正则会漏掉它。规避办法是**不按日期格式检索，而按关键词检索**（如 `grep -i 'FLPM'`），或用更宽松的日期正则 `[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}`。

---

## 5. 综合实践

把本讲学到的「分区骨架 + 事件归类 + 内外链辨别 + 主题检索」全部用上，完成下面这张**主题 → README 位置速查表**。这是本讲的核心交付物。

**任务**：为「调度、受限解码、MLA、大规模部署」四个主题，各找至少 2 条 README 资料，填出它们所在的**区段 / 子区段 / 行号 / 内外链**，并为每个主题写一句「推荐阅读顺序」。

**操作步骤**：

1. 用 4.3.4 与 4.4.4 给出的 `grep -ni` 命令在 README 中检索四个主题。
2. 对每条命中，向上找到所属的 `##` 区段与 `###` 子区段。
3. 判断内链 / 外链，并给出永久链接（用当前 HEAD 的行号锚点）。
4. 为每个主题排出「由浅入深」的阅读顺序。

**参考骨架**（请你自己补全「推荐阅读顺序」一列）：

| 主题 | 资料 | 区段 / 子区段 | 行号 | 内/外链 | 推荐阅读顺序 |
| --- | --- | --- | --- | --- | --- |
| 调度 | SGLang Overview & CPU Overhead Hiding | Slides / 首届 LMSYS meetup | L76 | 内链 | ____________ |
| 调度 | A fair and efficient scheduling algorithm (FLPM) | Slides / Biweekly Meeting | L96 | 内链 | ____________ |
| 调度 | SGLang v0.4: Zero-Overhead Batch Scheduler | Blog / LMSYS Org | L118 | 外链 | ____________ |
| 受限解码 | Faster Constrained Decoding | Slides / 首届 LMSYS meetup | L78 | 内链 | ____________ |
| 受限解码 | XGrammar 结构化生成 | Slides / 首届 LMSYS meetup | L84 | 内链 | ____________ |
| 受限解码 | Fast JSON Decoding / Compressed FSM | Blog / LMSYS Org | L124 | 外链 | ____________ |
| MLA | SGLang DeepSeek MLA（meetup 版） | Slides / 首届 LMSYS meetup | L80 | 内链 | ____________ |
| MLA | SGLang DeepSeek MLA（Biweekly 版） | Slides / Biweekly Meeting | L106 | 外链 | ____________ |
| MLA | v0.3 Release: 7x Faster DeepSeek MLA | Blog / LMSYS Org | L120 | 外链 | ____________ |
| 大规模部署 | May 2025 EP + PD 公告 | Announcement | L9 | 混合 | ____________ |
| 大规模部署 | Large-scale Deployment of Emerging LLMs | Slides / AMD SGLang Meetup | L42 | 内链 | ____________ |
| 大规模部署 | Deploying DeepSeek with PD Disaggregation … 96 H100 | Blog / LMSYS Org | L116 | 外链 | ____________ |

**预期结果**（自查要点）：

- 四个主题都能在 README 里找到**至少 2 条**、且**横跨 Slides 与 Blog 两个区段**的记录——这证明 README 的资料是「按事件归档」的，但可以「按主题检索」重新组织。
- 「大规模部署」主题的入口最特别：它最早出现在 `## Announcement`（L9），因为这是一次官方里程碑公告，而不只是普通幻灯片。
- 你给出的阅读顺序应当遵循「先概览（Overview/Announcement）→ 再专题幻灯片 → 最后深度博客」的由浅入深原则。

> 这张表也可以保存下来，作为你后续学习 u2（按主题绘制资料地图）时的「种子索引」。

## 6. 本讲小结

- README 由**六大顶层区段**构成：`## Announcement`（里程碑公告）、`## Slides`（幻灯片，本地资产最集中）、`## Blog`（博客，多为外链）、`## Videos`（YouTube 录像）、`## Paper`（arXiv 论文）、`## Documentaion`（文档站入口，注意原文有拼写错误）。
- `## Slides` 用 `###` 子区段做**事件归类**，事件分三类：meetup（社区聚会）、学术会议/Dev Day、biweekly developer sync（双周例会）；另有 `### Other` 兜底。每条记录格式为 `[YYYY-MM-DD] [标题](链接)`。
- **内链 vs 外链**的判断在每个区段都适用：以 `slides/`、`blogs/`、`./slides/` 等相对路径开头的是仓库内资产；以 `https://` 开头的是外部链接。Blog/Videos/Paper 几乎全是外链，唯一的本地长文是 `blogs/Efficient LLM Deployment and Serving.md`（L86）。
- **日期前缀** `[YYYY-MM-DD]` 既用于倒序排列，也用于同名资料消歧；注意 Biweekly Meeting 下有 `[2025-1-25]` 这类非零填充写法，按关键词检索更稳妥。
- **同一主题常横跨多个区段与事件**（如 MLA 出现在 L64/L80/L106/L120），应把同主题的多条记录当成一个「资料簇」整体阅读——这正是后续 u2「按主题绘制资料地图」的出发点。

## 7. 下一步学习建议

- **下一步进入 u2《按主题绘制资料地图》**：本讲你已经学会在 README 里**按主题反查**资料位置；u2 会反过来，**先定主题**（调度、受限解码、MLA、大规模部署、路由与权重热更新），把散落在各处的幻灯片组织成系统的主题地图并深入阅读。本讲的速查表就是 u2 的直接输入。
- **建议先动手熟悉两个检索命令**：`grep -ni '关键词' README.md`（按主题反查）与 `git ls-files slides/`（核对本地资产）。这两个命令会贯穿后续所有讲义。
- **可选预习**：直接点开 Slides 区段里你最感兴趣的一场 meetup（例如 `### The first LMSYS online meetup`，L74 起），把它下面 5 张幻灯片的标题读一遍，感受「一场活动覆盖多个主题」的编排——这能帮你在 u2 更快进入主题深读的状态。
