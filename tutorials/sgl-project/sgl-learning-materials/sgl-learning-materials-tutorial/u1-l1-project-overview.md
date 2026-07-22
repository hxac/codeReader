# 项目定位：SGLang 学习资料库是什么

## 1. 本讲目标

本讲是整本学习手册的第一篇，面向**完全没接触过本仓库**的读者。读完本讲后，你应当能够：

- 说清楚 **SGLang** 是什么、它解决什么问题；
- 说清楚**本仓库（sgl-learning-materials）的定位**——它是一个「资料聚合库」，而不是 SGLang 的运行时代码仓库；
- 辨认本仓库收录的**五类资料**（Slides / Blog / Videos / Paper / Documentation），并知道每一类分别指向「仓库内文件」还是「外部链接」；
- 厘清本仓库与 **SGLang 主仓库**、**官方文档站**、**论文**之间的边界与衔接关系。

> 提示：本仓库**不含任何 SGLang 运行时源码**（没有 `.py`/`.ts`/`.rs` 等可执行代码）。本讲所谓的「源码精读」，精读的对象是仓库里真实存在的 `README.md` 与 `LICENSE` 这两个文本文件——它们就是本仓库的「核心资产」。

## 2. 前置知识

本讲是入门第一课，理论上不需要任何先验知识。但下面几个名词会反复出现，先用最通俗的方式解释一下：

| 名词 | 通俗解释 |
| --- | --- |
| **LLM** | 大语言模型，例如 DeepSeek-V3/R1、Llama3 等。 |
| **推理 / 服务（Serving）** | 把训练好的模型部署成「给一句prompt、返回一段回答」的在线服务，关注**吞吐量**和**延迟**。 |
| **SGLang** | 一个**开源的 LLM 推理服务引擎**（serving engine），目标是又快又省地把大模型跑起来。 |
| **仓库（repository）** | 用 Git 管理的一个项目文件夹，GitHub 上的一个项目页面就是一个仓库。 |
| **聚合库** | 自己不生产内容，而是把分散在各处的资料（幻灯片、博客、视频……）汇总、归类、做成索引的仓库。 |

如果你已经知道「GitHub 仓库大概长什么样」「点链接会跳到另一个网页」，那就足够开始本讲了。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 仓库的**门面与导航索引**。整个仓库的「正文」几乎都在这里：项目说明、里程碑公告、五类资料的链接清单。 |
| `LICENSE` | 仓库的**开源许可证**，声明这份资料库以何种协议对外发布。 |
| `slides/`（目录） | 存放**仓库内**的幻灯片文件（`.pdf` / `.pptx` / `.png`）。 |
| `blogs/`（目录） | 存放**仓库内**的少量博客 markdown 与配图。 |

本讲只会精读 `README.md` 和 `LICENSE` 两个文件；`slides/` 与 `blogs/` 的目录结构会在下一讲（u1-l2）详细展开。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- 4.1 SGLang 简介与定位
- 4.2 本仓库的资料类型（Slides / Blog / Videos / Paper / Documentation）
- 4.3 本仓库与 SGLang 主仓库、文档站的关系

---

### 4.1 SGLang 简介与定位

#### 4.1.1 概念说明

**SGLang** 是一个开源的大语言模型（LLM）推理服务引擎。你可以把它理解成「给大模型用的一个高性能 Web 服务器」：你把模型权重交给它，它就能同时接收大量用户的请求，调度 GPU 资源，尽量快、尽量省地把回答吐出来。

它的核心卖点集中在「**快**」和「**省**」两件事上：更高的吞吐（每秒处理更多 token）、更低的延迟、更低的单 token 成本，以及对最新模型（如 DeepSeek 系列）和最新硬件（NVIDIA / AMD）的快速适配。

本仓库**不是 SGLang 引擎本身**，而是 SGLang 官方维护的「**学习资料聚合库**」——它把社区历次分享的幻灯片、博客、视频、论文、文档入口汇总到一处，方便大家系统地学习 SGLang。

#### 4.1.2 核心流程

从「想知道 SGLang 是什么」到「在本仓库里找到资料」，链路是这样的：

```
读者
 │
 │  1. 想了解 SGLang 是什么、能做什么
 ▼
本仓库 README.md（资料聚合库的「目录页」）
 │
 │  2. 通过 README 里的链接，跳到……
 ▼
┌──────────────────────────────────────────────┐
│  仓库内文件（slides/*.pdf）  或  外部站点      │
│  （lmsys.org 博客 / YouTube / arXiv 论文 …）  │
└──────────────────────────────────────────────┘
 │
 │  3. 在那里读到真正的「SGLang 是什么」
 ▼
建立对 SGLang 的理解
```

关键点：**本仓库是「路标」，不是「目的地」**。真正的知识大多在链接指向的地方。

#### 4.1.3 源码精读

先看仓库的「自我介绍」——`README.md` 第一行标题：

[README.md:1](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L1)

```markdown
# Materials for learning SGLang
```

这一行就是仓库的定位宣言：**「用于学习 SGLang 的资料」**。注意它没有写 "SGLang source code" 或 "SGLang runtime"，这正好印证了「资料库 ≠ 运行时代码库」。

再看 README 顶部第二段，交代了社区入口与联系方式：

[README.md:3](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L3)

> Please join our Slack Channel https://slack.sglang.ai. For enterprises interested in adopting or deploying SGLang at scale ... please contact us at sglang@lmsys.org.

这一行说明：本仓库由 SGLang 团队维护，社区入口是 Slack，企业合作邮箱是 `sglang@lmsys.org`（域名 `lmsys.org` 暗示维护团队来自 LMSYS 组织）。

要理解「SGLang 究竟是什么」，最好的位置是 README 的 Announcement 区段里 2025 年 5 月那条里程碑公告：

[README.md:7-9](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L7-L9)

> The SGLang team is delighted to announce that SGLang has become the first fully open-source LLM serving engine to support large-scale Expert-Parallelism (EP) and Prefill-Decode disaggregation ...

这句话给 SGLang 下了一个精确的定义：**「the first fully open-source LLM serving engine」**（第一个完全开源的 LLM 服务引擎），并点出了它当时最前沿的能力——大规模专家并行（Expert-Parallelism, EP）与 Prefill-Decode 分离（disaggregation）。这两个名词现在看不懂没关系，在 u2-l5 会专门讲；这里只需记住：**SGLang 是一个 LLM serving engine**。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（本仓库没有可运行的代码，所以实践以阅读和归纳为主）。

1. **实践目标**：用自己的话把 SGLang 和本仓库分别「是什么」讲清楚。
2. **操作步骤**：
   - 打开仓库根目录的 `README.md`。
   - 阅读第 1 行标题与第 3 行的社区说明。
   - 阅读第 7–9 行的 2025 年 5 月公告。
3. **需要观察的现象**：注意标题里有没有出现 "source code / runtime" 这类词（没有），以及公告里对 SGLang 的定义（"LLM serving engine"）。
4. **预期结果**：你能写出类似下面这样的两句话——
   - 「SGLang 是一个开源的 LLM 推理服务引擎（serving engine），主打高吞吐、低延迟、低成本。」
   - 「本仓库（sgl-learning-materials）是 SGLang 官方的学习资料聚合库，本身不含运行时代码。」

#### 4.1.5 小练习与答案

**练习 1**：本仓库的标题是 "Materials for learning SGLang"。如果有人误以为「clone 这个仓库就能拿到 SGLang 的源码并跑起来」，这个想法对吗？为什么？

> **参考答案**：不对。标题明确写的是 "Materials for learning"（学习资料），README 里也没有任何可执行代码（`.py` 等）。本仓库只聚合资料，SGLang 的运行时代码在另一个主仓库 `sgl-project/sglang`。

**练习 2**：从 README 第 9 行的公告里，找出能用来「定义 SGLang 身份」的那个英文短语。

> **参考答案**："the first fully open-source LLM serving engine"。这表明 SGLang 的身份是 LLM serving engine（LLM 服务引擎）。

---

### 4.2 本仓库的资料类型（Slides / Blog / Videos / Paper / Documentation）

#### 4.2.1 概念说明

本仓库把所有学习资料分成**五大类**，每一类在 `README.md` 里都对应一个二级标题（`##`）。理解这五个分区，就等于拿到了本仓库的「主索引」：

| 分区 | 内容形态 | 典型用途 |
| --- | --- | --- |
| **Slides** | 幻灯片（PDF / PPT / 在线幻灯片） | 看社区分享、meetup 演讲 |
| **Blog** | 技术博客文章 | 读版本发布说明、深度技术解读 |
| **Videos** | 视频（主要是 YouTube） | 看演讲录像、开发者例会回放 |
| **Paper** | 学术论文 | 读 SGLang 的学术原理（NeurIPS 论文） |
| **Documentation** | 官方文档站 | 查 API、查使用方法 |

一个**关键细节**：这五类资料里，**有些链接指向仓库内的文件**（例如 `slides/xxx.pdf`），**有些链接指向外部网站**（例如 `https://lmsys.org/...`）。初学者最容易在这里犯晕，所以下一节的实践任务专门来辨析这件事。

#### 4.2.2 核心流程

判断一条资料「在仓库内还是在外部」，可以按下面这个决策流程：

```
看到 README 里一条 [日期] [标题](链接) 条目
            │
            │  看括号里的链接 (...)
            ▼
   链接是否以 http(s):// 开头？
        ┌───────────┴───────────┐
       是                       否
        │                       │
        ▼                       ▼
  外部链接                仓库内相对路径
 （跳到外网）          （如 slides/xxx.pdf，
                         文件就在本仓库里）
```

举个例子对比：

- 仓库内：`[...](slides/sglang_pytorch_china_2025.pdf)` —— 链接以 `slides/` 开头，是**相对路径**，文件物理存在于本仓库的 `slides/` 目录下。
- 外部链接：`[...](https://lmsys.org/blog/2025-05-05-large-scale-ep/)` —— 以 `https://` 开头，跳到 LMSYS 官方博客，内容**不在本仓库**。

#### 4.2.3 源码精读

`README.md` 的五个分区标题分别在这些位置：

- Slides 分区：[README.md:32](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L32) `## Slides`
- Blog 分区：[README.md:112](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L112) `## Blog`
- Videos 分区：[README.md:148](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L148) `## Videos`
- Paper 分区：[README.md:184](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L184) `## Paper`
- Documentation 分区：[README.md:189](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L189) `## Documentaion`

> 小提醒：第 189 行的标题原文是 `## Documentaion`（少了一个 `t`，正确拼写应为 Documentation）。这是 README 里的真实拼写，引用时请照原样，不要「自作主张」改掉——以源文件为准是阅读源码的好习惯。

下面看几条**真实条目**，体会「仓库内 vs 外部」的差别。

**Slides 区——仓库内文件示例**：

[README.md:34](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L34)

```markdown
[2025-10-22] [PyTorch Conference 2025 SGLang](slides/sglang_pytorch_2025.pdf)
```

链接是 `slides/sglang_pytorch_2025.pdf`，相对路径 → **仓库内文件**。

**Slides 区——外部链接示例**：

[README.md:52](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L52)

```markdown
[2025-07-08] [SGLang: An Efficient Open-Source Framework ...](https://gamma.app/docs/...)
```

链接以 `https://` 开头 → **外部站点**（gamma.app）。

**Blog 区——几乎全是外部链接**：以 LMSYS 博客为例，

[README.md:116](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L116)

```markdown
[2025-05-05] [Deploying DeepSeek with PD Disaggregation ...](https://lmsys.org/blog/2025-05-05-large-scale-ep/)
```

注意：`## Blog` 分区下的条目**全部**指向外部站点（lmsys.org / rocm.blogs.amd.com / pytorch.org / techcommunity.microsoft.com），仓库内并不存放这些博客正文。

> 一个容易踩坑的点：仓库里其实**唯一一篇**写在仓库内的 markdown 博客 `blogs/Efficient LLM Deployment and Serving.md`，并没有被放在 `## Blog` 分区，而是被列在了 `## Slides` 分区下作为 meetup 回顾：

[README.md:86](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L86)

```markdown
[2024-10-16] [Review of the first LMSYS online meetup ...](blogs/Efficient%20LLM%20Deployment%20and%20Serving.md)
```

这正说明：分区是「按来源/主题」归类，而不是严格按「文件格式」归类。读到这种例外时不必纠结，记住这一篇的位置即可（u3-l1 会精读它）。

**Videos 区——纯外部（YouTube）**：

[README.md:150](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L150)

```markdown
Welcome to follow our YouTube [channel](https://www.youtube.com/@lmsys-org).
```

所有视频都在 YouTube，仓库内不存视频文件。

**Paper 区——外部（arXiv）**：

[README.md:186](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L186)

```markdown
[NeurIPS 24] [SGLang: Efficient Execution of Structured Language Model Programs](https://arxiv.org/abs/2312.07104)
```

论文链接指向 arXiv，是 SGLang 的 NeurIPS 2024 论文。

**Documentation 区——外部（文档站）**：

[README.md:191](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L191)

```markdown
[SGLang Documentation](https://sgl-project.github.io/)
```

官方文档站地址是 `https://sgl-project.github.io/`。

#### 4.2.4 代码实践

这是本讲的**主实践任务**（对应任务规格里要求完成的归纳练习）。

1. **实践目标**：用一段话写清本仓库定位，并给出五类资料的「去向表」。
2. **操作步骤**：
   - 通读 `README.md` 的五个分区（行号见 4.2.3）。
   - 在每个分区里各挑 1–2 条条目，判断它的链接是「仓库内相对路径」还是「外部 `https://` 链接」。
   - 按下面的模板填写。
3. **需要观察的现象**：重点留意 Slides 分区「既有仓库内又有外部」的混合特征；Blog / Videos / Paper / Documentation 是否几乎全是外部链接。
4. **预期结果**（请你自己填写，下面是示范格式）：

   > **本仓库定位**：本仓库（sgl-learning-materials）是 SGLang 官方维护的学习资料聚合库，本身不含运行时代码，把分散在各处的幻灯片、博客、视频、论文与文档入口汇总到 README 中做成索引。
   >
   > **五类资料去向表**：
   >
   > | 类型 | 主要去向 | 举例 |
   > | --- | --- | --- |
   > | Slides | **混合**：仓库内 `slides/*.pdf` + 少量外部（gamma.app / docs.google.com） | `slides/sglang_pytorch_2025.pdf`（仓库内）；`https://gamma.app/docs/...`（外部） |
   > | Blog | **外部**（lmsys.org / AMD / PyTorch / Azure 博客）；仅 1 篇回顾写在仓库内 `blogs/` 但列在 Slides 区 | `https://lmsys.org/blog/2025-05-05-large-scale-ep/` |
   > | Videos | **外部**（YouTube `@lmsys-org`） | `https://www.youtube.com/watch?v=...` |
   > | Paper | **外部**（arXiv） | `https://arxiv.org/abs/2312.07104` |
   > | Documentation | **外部**（文档站） | `https://sgl-project.github.io/` |

5. 如果你无法确定某条链接的归属，**明确标注「待本地验证」**，不要瞎猜。

#### 4.2.5 小练习与答案

**练习 1**：在 Slides 分区里，下面两条哪条是仓库内文件、哪条是外部链接？
- a) `[2025-10-22] [PyTorch Conference 2025 SGLang](slides/sglang_pytorch_2025.pdf)`
- b) `[2025-07-08] [SGLang: ...](https://gamma.app/docs/...)`

> **参考答案**：a) 是仓库内文件（相对路径 `slides/...`）；b) 是外部链接（`https://` 开头，指向 gamma.app）。

**练习 2**：`## Blog` 分区里有没有指向仓库内 `.md` 文件的条目？

> **参考答案**：没有。`## Blog` 分区的条目全部指向外部博客站点。仓库内唯一一篇 markdown 博客 `blogs/Efficient LLM Deployment and Serving.md` 被列在 `## Slides` 分区下（README 第 86 行）。

**练习 3**：第 189 行的分区标题实际拼写是什么？正确拼写应该是什么？

> **参考答案**：实际拼写是 `## Documentaion`（少了一个 `t`）；正确拼写是 Documentation。引用源文件时以实际拼写为准。

---

### 4.3 本仓库与 SGLang 主仓库、文档站的关系

#### 4.3.1 概念说明

很多初学者会把「几个名字里带 SGLang 的地方」搞混。本小节帮你把它们一刀切清楚：

| 名称 | 是什么 | 在哪 | 有没有运行时代码 |
| --- | --- | --- | --- |
| **本仓库** `sgl-project/sgl-learning-materials` | 学习**资料**聚合库 | GitHub | **没有**，只有 README / LICENSE / 幻灯片 / 博客 |
| **SGLang 主仓库** `sgl-project/sglang` | SGLang **引擎本体**（源代码） | GitHub（另一个仓库） | **有**，这是真正的代码 |
| **官方文档站** `sgl-project.github.io` | 在线**使用文档**（API、教程） | 网站 | 没有，是渲染好的文档网页 |
| **论文**（arXiv 2312.07104） | SGLang 的**学术原理**阐述 | arXiv | 没有，是学术论文 |

一句话总结关系：

> **本仓库（资料）→ 文档站（怎么用）→ 论文（为什么）→ 主仓库（真代码）** 是一条层层递进的链路。本仓库站在最上游，负责「把你领进门」。

#### 4.3.2 核心流程

当你想从本仓库「跳」到更深的资源时，路径如下：

```
本仓库 README
   │
   ├──(Documentation 区)──► https://sgl-project.github.io/   （查怎么用）
   │
   ├──(Paper 区)──────────► https://arxiv.org/abs/2312.07104 （读原理）
   │
   ├──(Videos 区)─────────► https://www.youtube.com/@lmsys-org （看演讲）
   │
   └──(README 顶部)──────► https://slack.sglang.ai            （进社区问问题）
                              │
                              └─► 想看/改引擎代码？去 GitHub 的 sgl-project/sglang
```

注意：**主仓库 `sgl-project/sglang` 并没有直接出现在 README 的链接里**——README 主要链接的是资料、博客、视频、论文和文档站。引擎代码仓库需要你自己到 GitHub 上搜索 `sgl-project/sglang` 进入。这正是本仓库「资料库」边界的体现：它只负责资料，不负责代码分发。

#### 4.3.3 源码精读

社区入口（Slack）在 README 第 3 行：

[README.md:3](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L3)

> Please join our Slack Channel https://slack.sglang.ai.

论文入口在 Paper 分区，第 186 行：

[README.md:186](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L186)

> `[NeurIPS 24] [SGLang: Efficient Execution of Structured Language Model Programs](https://arxiv.org/abs/2312.07104)`

文档站入口在 Documentation 分区，第 191 行：

[README.md:191](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L191)

> `[SGLang Documentation](https://sgl-project.github.io/)`

注意文档站的域名 `sgl-project.github.io`——它的组织前缀 `sgl-project` 正是 GitHub 上 SGLang 的组织名（本仓库与主仓库都在这个组织下：`sgl-project/sgl-learning-materials` 与 `sgl-project/sglang`）。这就把「资料库 → 文档站 → 主仓库」三者用同一个 GitHub 组织串了起来。

最后看 `LICENSE`，它从法律层面确认了本仓库「资料库」的身份：

[LICENSE:1-3](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/LICENSE#L1-L3)

```
MIT License

Copyright (c) 2024 sgl-project
```

这是标准的 **MIT 许可证**，版权方是 `sgl-project` 组织。MIT 是一种非常宽松的开源协议，意味着你可以自由复制、修改、再分发这份资料库的内容（只要保留版权声明）。这也侧面说明：本仓库的「资产」就是这些资料文本本身，所以许可证保护的也是资料，而不是某个软件的二进制或运行时。

#### 4.3.4 代码实践

1. **实践目标**：把本仓库的「外部出口」梳理成一张清单，并验证它们的归属组织。
2. **操作步骤**：
   - 在 `README.md` 中找到 Slack、文档站、论文、YouTube 四个入口（行号见 4.3.3 与 4.2.3）。
   - 把每个 URL 的「主域名」抽出来，判断它属于哪个组织（例如 `sgl-project.github.io` 属于 sgl-project，`lmsys.org` 属于 LMSYS）。
3. **需要观察的现象**：注意哪些入口共享 `sgl-project` 这个组织前缀，哪些指向 LMSYS / arXiv / YouTube 等第三方。
4. **预期结果**：得到一张类似下表的清单——
   - `https://slack.sglang.ai` → SGLang 社区 Slack
   - `https://sgl-project.github.io/` → sgl-project 组织的文档站
   - `https://arxiv.org/abs/2312.07104` → arXiv（第三方学术论文库）
   - `https://www.youtube.com/@lmsys-org` → LMSYS 的 YouTube 频道
5. 结论（预期）：本仓库是 `sgl-project` 组织下的资料库；要找引擎源码，应去同组织的另一个仓库 `sgl-project/sglang`（**待本地验证**：可到 GitHub 搜索确认该仓库存在）。

#### 4.3.5 小练习与答案

**练习 1**：如果你想**查看 SGLang 的 Python 源代码**，应该 clone 本仓库吗？

> **参考答案**：不应该。本仓库只有资料。应去 GitHub 上的 `sgl-project/sglang` 主仓库。

**练习 2**：本仓库、文档站、主仓库三者共享哪个 GitHub 组织名？

> **参考答案**：`sgl-project`。本仓库是 `sgl-project/sgl-learning-materials`，文档站域名前缀是 `sgl-project.github.io`，主仓库是 `sgl-project/sglang`。

**练习 3**：本仓库用的是什么开源许可证？版权方是谁？

> **参考答案**：MIT License，版权方是 `sgl-project`（Copyright (c) 2024 sgl-project，见 LICENSE 第 1–3 行）。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「**仓库画像**」小任务。这是一次纯阅读与归纳的综合练习，不需要运行任何代码。

**任务**：假设你要向一位完全没听过 SGLang 的同事用 5 分钟介绍这个资料库。请产出一份一页纸的「画像」，包含以下四个部分：

1. **一句话定位**：用一句话说清「sgl-learning-materials 是什么、不是什么」。（依据 4.1）
2. **资料分类表**：列出 README 的五个分区，并各举一个「仓库内 / 外部」的例子。（依据 4.2）
3. **边界说明**：明确写出本仓库里**找不到**什么（运行时代码），以及要找代码/文档/原理分别该去哪里。（依据 4.3）
4. **入口清单**：列出 Slack、文档站、论文、YouTube 四个外部入口及其用途。

**验收标准**：

- 第 1 点不能把本仓库和 SGLang 主仓库混淆；
- 第 2 点的「仓库内 vs 外部」判断必须正确（可对照 4.2.4 的示范表）；
- 第 3 点必须提到 `sgl-project/sglang` 主仓库与 `sgl-project.github.io` 文档站；
- 所有引用的链接/文件名都必须是 README/LICENSE 里真实存在的，不得编造。

> 提示：完成这个任务的过程中，你会自然地把 README 当成「导航地图」来用——这正是下一讲（u1-l3）的主题。

## 6. 本讲小结

- **SGLang** 是一个开源的 LLM 推理服务引擎（serving engine），主打高吞吐、低延迟、低成本。
- **本仓库（sgl-learning-materials）是 SGLang 官方的「学习资料聚合库」**，本身**不含任何运行时代码**，核心资产就是 `README.md` 这份导航索引，外加 `slides/`、`blogs/` 里的少量仓库内文件。
- README 把资料分成 **Slides / Blog / Videos / Paper / Documentation** 五大类，其中 **Slides 是「仓库内 + 外部」混合**，其余四类主要指向外部站点。
- 判断一条资料在仓库内还是外部，看链接是否以 `https://` 开头：是则为外部链接，否则为仓库内相对路径。
- 本仓库与 **主仓库 `sgl-project/sglang`（代码）**、**文档站 `sgl-project.github.io`（用法）**、**arXiv 论文（原理）** 共同构成一条递进链路；三者同属 `sgl-project` 组织。
- 仓库采用 **MIT 许可证**，版权方为 `sgl-project`。

## 7. 下一步学习建议

接下来建议按以下顺序继续：

1. **u1-l2 仓库目录结构与内容类型**：深入 `slides/`、`blogs/` 目录，看清仓库内到底有哪些文件、什么格式，巩固本讲对「仓库内文件」的直觉。
2. **u1-l3 把 README 当作导航地图**：学会按「事件 / 主题」在 README 里快速定位资料，为后面进阶单元打基础。
3. 之后再进入 **u2（按主题绘制资料地图）**，系统学习调度、受限解码、MLA、大规模部署等核心主题的资料。

> 如果你已经迫不及待想看「真东西」，可以现在就点开 README 第 9 行指向的 LMSYS large-scale-ep 博客（外部链接），感受一下本仓库「路标」最终把你带到的地方长什么样——但记得读完后回到本手册，按顺序打好基础。
