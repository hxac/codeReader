# SGLang 版本演进与里程碑资料

## 1. 本讲目标

本讲是进阶单元的第一篇。学完之后，你应当能够：

- 从 `README.md` 的 **Announcement（公告）** 区段，按时间顺序梳理 SGLang 从 2024 年底到 2025 年中的四个关键里程碑。
- 说清楚 **v0.2 → v0.3 → v0.4 → 大规模 EP** 这条版本演进线上，每个阶段的核心卖点（也就是它解决了什么性能瓶颈）。
- 学会用一种关键技能——**「博客外链追根溯源」**：从 Announcement 里一句简短的里程碑描述，跳到 `## Blog` 区段里对应的 LMSYS 博客，再把博客标题当作「核心卖点清单」来读。
- 理解 **adoption（采纳）** 的含义，知道 SGLang 被哪些公司用在了生产环境里。

> 提醒：本仓库不含 SGLang 运行时源码，它是一个**资料聚合库**。所以本讲的「源码精读」对象是 `README.md` 的文字、仓库内的幻灯片与图片，而不是 `.py` 代码。这一点承接自 [u1-l1](u1-l1-project-overview.md) 的定位结论。

## 2. 前置知识

本讲默认你已经读完 [u1-l3：把 README 当作导航地图](u1-l3-readme-navigation.md)，并掌握以下两件事：

1. **README 的区段结构**：README 由 `## Announcement`、`## Slides`、`## Blog`、`## Videos`、`## Paper`、`## Documentaion` 等顶层区段组成（`## Documentaion` 是仓库原文的拼写，非笔误）。
2. **内链 vs 外链的判断**：以 `slides/`、`blogs/`、`./` 这类相对路径开头的是**仓库内资产**；以 `https://` 开头的是**外部链接**。

本讲还会用到三个新名词，先用大白话解释：

| 名词 | 通俗解释 |
|---|---|
| **版本演进（version evolution）** | 一个软件从早期到成熟，版本号一步步升高的过程，就像一本书的「修订版次」。 |
| **里程碑（milestone）** | 项目发展中值得标记的关键节点，例如「第一次被大公司采纳」「第一次支持某个大特性」。 |
| **追根溯源（trace to the source）** | README 里往往只写一句话，背后通常配有一篇详细博客。沿着链接从「一句话」找到「一篇文章」的过程，就是追根溯源。 |

还要回顾一个来自 [u1-l1](u1-l1-project-overview.md) 的核心认知：**SGLang 是一个开源的 LLM 推理服务引擎（serving engine）**，主打高吞吐、低延迟、低成本。本讲要梳理的版本演进，本质上就是「SGLang 是如何一步步变得更快、更省、被更多人用的」。

## 3. 本讲源码地图

本讲只涉及三个文件，全部是仓库内真实存在的资产：

| 文件 | 类型 | 在本讲的作用 |
|---|---|---|
| [README.md](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md) | Markdown | **核心**。Announcement 区段给出里程碑，Blog 区段给出版本博客，二者构成「一句话 → 一篇文章」的溯源链。 |
| [slides/sglang_v0_2.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang_v0_2.pdf) | PDF 幻灯片 | 仓库内**唯一**与某个具体版本（v0.2）直接对应的幻灯片，是「版本资料」的本地代表。 |
| [slides/adoption.png](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/adoption.png) | PNG 图片 | 一张「采纳 logo 墙」配图，把抽象的「被某某公司采纳」可视化。 |

> 检索命令（来自 [u1-l3](u1-l3-readme-navigation.md) 的工具箱）：`grep -ni '关键词' README.md` 与 `git ls-files slides/`。本讲会反复用到它们。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 Announcement 区段——四个月份里程碑**
- **4.2 v0.2 / v0.3 / v0.4 三大版本博客外链**
- **4.3 adoption 采纳情况（AMD / xAI 等）**

---

### 4.1 Announcement 区段——四个月份里程碑

#### 4.1.1 概念说明

`## Announcement` 是 README 的「**头条公告板**」。它的特点是：

- **按月份倒序排列**：最新（2025-05）在最上面，最早（2024-12）在最下面——倒着读就是时间正序。
- **只放最重大的事**：不是每个小版本都上 Announcement，只有「里程碑级别」的事件才会出现。
- **时间敏感**：这是 README 里最容易随版本更新的区段，所以阅读时要注意日期。

#### 4.1.2 核心流程

Announcement 里的四个里程碑，连起来其实是一个完整的故事线——**从「产品成熟」到「被产业采纳」再到「融入生态」最后到「大规模生产」**：

```text
2024-12  三个大版本发布（v0.2 / v0.3 / v0.4）  ← 产品成熟：引擎能力齐备
   ↓
2025-02  多家公司用它跑 DeepSeek V3/R1        ← 产业采纳：被生产环境接受
   ↓
2025-03  加入 PyTorch 生态 + AMD 上 SOTA       ← 融入生态：进入主流工具链
   ↓
2025-05  大规模 EP + PD 分离，成本 $0.20/1M    ← 大规模生产：极致降本
```

这条线解释了为什么 Announcement 要倒着读：**越往下越接近「起点」，越往上越接近「当下成果」**。理解了这条主线，后面看每个版本的技术细节时，就知道它在整条故事线里处于什么位置。

#### 4.1.3 源码精读

先看 Announcement 区段的入口（README 第 5 行）：

[README.md#L5](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L5) —— `## Announcement` 顶层区段标题，下面四个 `### 月份` 子区段就是四个里程碑。

**里程碑一：2025 年 5 月（大规模 EP）**，见 [README.md#L7-L9](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L7-L9)。原文要点：SGLang 成为**第一个**完全开源、支持大规模 Expert-Parallelism（EP）与 Prefill-Decode 分离（disaggregation）的 LLM serving 引擎，吞吐追平 DeepSeek 官方博客披露的水平，成本降到 **$0.20 / 1M output tokens**。注意它同时给了两个链接：一个 LMSYS 博客（外链）、一个本仓库幻灯片 `./slides/sglang_pytorch_china_2025.pdf`（内链）。

**里程碑二：2025 年 3 月（加入 PyTorch 生态）**，见 [README.md#L11-L17](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L11-L17)。要点：正式加入 PyTorch 生态，并在 AMD nightly 镜像上达到 SOTA。这里给了两条**外链**博客：一条 pytorch.org、一条 AMD ROCm 博客。

**里程碑三：2025 年 2 月（公司采纳名单）**，见 [README.md#L19-L21](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L19-L21)。要点：列出一批用 SGLang 跑 DeepSeek V3/R1 的公司，包括 AMD、NVIDIA、Microsoft Azure、Baseten、Novita AI、ByteDance Volcengine、DataCrunch、Hyperbolic、Vultr、RunPod 等。这条清单是纯外链集合。

**里程碑四：2024 年 12 月（三版本 + AMD/xAI）**，见 [README.md#L23-L30](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L23-L30)。这是本讲的「枢纽里程碑」，它一口气交代了两件事——**三个大版本**（下一节细讲）和**两个标志性采纳**（4.3 节细讲）。

#### 4.1.4 代码实践

1. **实践目标**：用命令快速把四个里程碑从 README 里「抓」出来，验证倒序排列。
2. **操作步骤**：在仓库根目录执行

   ```bash
   grep -n '^### ' README.md | head -n 20
   ```

   重点观察 `## Announcement` 之下、`## Slides` 之前的四个 `### 月份` 行。
3. **需要观察的现象**：你会看到 `### May 2025`、`### March 2025`、`### February 2025`、`### December 2024` 依次出现，顺序从新到旧。
4. **预期结果**：四个里程碑的行号应分别落在第 **7、11、19、23** 行（与 4.1.3 的引用一致）。

#### 4.1.5 小练习与答案

**练习 1**：Announcement 区段里，最早和最晚的里程碑分别是哪个月？为什么 README 要这样排？

> **参考答案**：最早是 December 2024（第 23 行，最靠下），最晚是 May 2025（第 7 行，最靠上）。README 采用倒序，把最新的成果放最前，方便读者第一眼看到项目当下状态。

**练习 2**：May 2025 里程碑同时给出了一条外链和一条内链，分别指向什么？

> **参考答案**：外链是 LMSYS 博客 `https://lmsys.org/blog/2025-05-05-large-scale-ep/`（详细技术解读），内链是仓库内幻灯片 `./slides/sglang_pytorch_china_2025.pdf`（PyTorch Day China 现场幻灯片）。这正是「资料簇」的典型形态：一个主题同时有外链深度文 + 内链幻灯片。

---

### 4.2 v0.2 / v0.3 / v0.4 三大版本博客外链

#### 4.2.1 概念说明

2024 年 12 月的里程碑里提到，团队在「2024 年 7 月到 12 月」期间完成了三个大版本：**v0.2、v0.3、v0.4**。每个版本都有一篇 LMSYS 官方博客做详细说明。

这里要掌握一个阅读技巧：**博客标题本身就是该版本的「核心卖点清单」**。例如 v0.3 的博客标题里直接写着「7x Faster DeepSeek MLA」「1.5x Faster torch.compile」——这些短语就是 v0.3 最想让你记住的改进。所以哪怕你不点开博客，光读标题就能抓到卖点。

#### 4.2.2 核心流程

**「追根溯源」的标准动作**分三步：

```text
第 1 步：在 Announcement 里看到一句简短描述（往往只有一个版本号 + 一个链接）
第 2 步：顺着链接，或去 ## Blog 区段找同日期/同版本号的条目
第 3 步：读博客标题 → 当成「核心卖点清单」提炼要点
```

为什么强调「去 Blog 区段找同名条目」？因为 Announcement 里的链接和 Blog 区段里的链接指向的是**同一篇博客**。Announcement 是「新闻稿式的一句话」，Blog 区段则是「按日期整齐归档的索引」。两边互相对照，能确认你找对了文章。

#### 4.2.3 源码精读

**第 1 步——Announcement 里的「一句话」**，见 [README.md#L24](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L24)。这一行同时列出三个版本及其博客外链：

- v0.2 → `https://lmsys.org/blog/2024-07-25-sglang-llama3/`
- v0.3 → `https://lmsys.org/blog/2024-09-04-sglang-v0-3/`
- v0.4 → `https://lmsys.org/blog/2024-12-04-sglang-v0-4/`

**第 2 步——到 Blog 区段找同名条目**。Blog 区段的入口是 [README.md#L112-L114](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L112-L114)（`## Blog` 与 `## LMSYS Org` 子标题）。三个版本的归档条目如下，**日期前缀正好和上面 Announcement 的链接一一对应**：

| 版本 | Blog 区段条目（行号） | 博客标题 = 核心卖点 |
|---|---|---|
| v0.2 | [README.md#L122](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L122) | Achieving Faster Open-Source **Llama3 Serving**（对标 TensorRT-LLM、vLLM） |
| v0.3 | [README.md#L120](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L120) | **7x Faster DeepSeek MLA**、**1.5x Faster torch.compile**、多图/视频 LLaVA-OneVision |
| v0.4 | [README.md#L118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118) | **Zero-Overhead Batch Scheduler**、**Cache-Aware Load Balancer**、更快的结构化输出 |

> 注意 v0.2 的博客标题聚焦「Llama3 Serving 速度」，而仓库内那份 v0.2 幻灯片的标题则是「**Faster Interface and Runtime** for LLM Inference」（见 4.2.3 末）。同一个版本，博客和幻灯片切入点略有不同：博客强调「跑 Llama3 有多快」，幻灯片强调「接口和运行时都更快了」。

**仓库内的 v0.2 幻灯片**（本讲 source_files 之一）登记在 Slides 区段的 `### Other` 兜底子区段下，见 [README.md#L108-L110](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L108-L110)。文件本体是 [slides/sglang_v0_2.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang_v0_2.pdf)。它是仓库内**唯一**以版本号直接命名的幻灯片——其他版本的资料几乎都以**外链**形式存在（例如 v0.4 优化在外链 gamma.app，见 README 第 68 行）。

补充一条「**版本演进线的延伸**」：2025 年 5 月的大规模 EP 虽然不叫 v0.5，但它显然是 v0.2→v0.3→v0.4 之后的下一个大节点。它的详细博客归档在 [README.md#L116](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L116)（标题：「Deploying DeepSeek with PD Disaggregation and Large-Scale Expert Parallelism on 96 H100 GPUs」）。所以完整的演进线是：**v0.2 → v0.3 → v0.4 → 大规模 EP**。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证「Announcement 的链接」与「Blog 区段的链接」指向同一篇博客（追根溯源成立）。
2. **操作步骤**：

   ```bash
   # 取出 Announcement 里三个版本的链接
   sed -n '24p' README.md
   # 取出 Blog 区段里三个版本的链接
   sed -n '118p;120p;122p' README.md
   ```

   （`sed -n '24p'` 表示只打印第 24 行，依此类推。）
3. **需要观察的现象**：两组命令打印出来的 URL 应当两两相同——v0.2 都是 `.../2024-07-25-sglang-llama3/`，v0.3 都是 `.../2024-09-04-sglang-v0-3/`，v0.4 都是 `.../2024-12-04-sglang-v0-4/`。
4. **预期结果**：三对链接完全一致，证明 Announcement 与 Blog 区段是同一批博客的两种视图。
5. **若无法运行 `sed`**：用任意编辑器打开 README，人工跳到第 24 行与第 118/120/122 行对照即可，结论相同。

#### 4.2.5 小练习与答案

**练习 1**：仅凭博客标题，v0.4 的三个核心卖点是什么？

> **参考答案**：Zero-Overhead Batch Scheduler（零开销批调度器）、Cache-Aware Load Balancer（缓存感知负载均衡器）、Faster Structured Outputs（更快的结构化输出）。来源：[README.md#L118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118) 的博客标题。

**练习 2**：仓库内为什么只有 v0.2 有一份本地 PDF 幻灯片，v0.3/v0.4 却没有？

> **参考答案**：因为 v0.3/v0.4 的配套幻灯片是以**外链**形式收录的（例如 v0.4 优化见 README 第 68 行的 gamma.app 外链），而 v0.2 的幻灯片 [slides/sglang_v0_2.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang_v0_2.pdf) 被作为本地资产上传进了仓库，登记在 `### Other` 子区段（第 110 行）。这体现了本仓库「内链 + 外链混合」的资料收录习惯（承接 [u1-l3](u1-l3-readme-navigation.md)）。

---

### 4.3 adoption 采纳情况（AMD / xAI 等）

#### 4.3.1 概念说明

**adoption（采纳）** 指的是一个软件被哪些团队/公司真正用在生产环境里。对开源项目而言，adoption 是衡量「是否被业界认可」的硬指标——比任何技术 benchmark 都更能说明问题。

在 SGLang 的 Announcement 里，adoption 分两层来表达：

- **标志性采纳**（December 2024）：AMD 把 SGLang 作为「dominant LLM engine」（主力 LLM 引擎），xAI 把它作为「default LLM engine」（默认 LLM 引擎）。这两个是**定性**的最高评价。
- **群体性采纳**（February 2025）：再列出十家左右用 SGLang 跑 DeepSeek 的公司，是**定量**的背书。

#### 4.3.2 核心流程

adoption 资料在本仓库里构成一个小「簇」：

```text
December 2024 公告（AMD 主力 / xAI 默认）   ← 文字定性
        +
February 2025 名单（十家以上公司）           ← 文字定量
        +
slides/adoption.png（采纳 logo 墙）          ← 可视化
        +
slides/sglang_adoption_logo.pptx（可编辑版） ← 素材
```

读 adoption 资料时，把这几样当成同一个主题的不同表现形式一起看，印象会完整得多。

#### 4.3.3 源码精读

**标志性采纳**，见 [README.md#L26-L30](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L26-L30)。原文（第 27–28 行）给出两条最有分量的背书：

- 第 27 行：`- The dominant LLM engine by AMD`
- 第 28 行：`- The default LLM engine for xAI`

第 30 行还追加了 AMD ROCm 6.3 官方公告与 xAI 在 AMD Advancing AI 2024 大会演讲的外链，方便进一步溯源。

**群体性采纳名单**，见 [README.md#L21](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L21)。这一行集中列出 AMD、NVIDIA、Microsoft Azure、Baseten、Novita AI、ByteDance Volcengine、DataCrunch、Hyperbolic、Vultr、RunPod 等，并各自带了指向其公告/博客的外链。

**采纳 logo 墙配图**，文件本体是 [slides/adoption.png](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/adoption.png)。这张图是一个**公司 logo 网格拼贴**，把上述文字名单可视化为「一面采纳墙」。仓库里还有一份可编辑的同主题素材 [slides/sglang_adoption_logo.pptx](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang_adoption_logo.pptx)（仓库内唯一的 `.pptx`，承接 [u1-l2](u1-l2-directory-structure.md)）。

> 小提示：`adoption.png` 并没有出现在 README 的 Slides 列表里——它是一张「游离」的配图资产，需要靠 `git ls-files slides/` 才能发现。这正是 [u1-l2](u1-l2-directory-structure.md) 提到的「用 git ls-files 兜底查找」的真实用例。

#### 4.3.4 代码实践

1. **实践目标**：用 git 历史确认 `adoption.png` 是一份被多次维护的「采纳墙」配图。
2. **操作步骤**：

   ```bash
   git log --oneline -- slides/adoption.png
   git ls-files slides/ | grep -i adoption
   ```

3. **需要观察的现象**：第一条命令会列出多次提交（如 `Add files via upload`、`adoption (#23)`、`Upd adortption (#21)` 等），说明这张图随着采纳名单扩大被反复更新；第二条命令会同时显示 `slides/adoption.png` 与 `slides/sglang_adoption_logo.pptx` 两份同主题资产。
4. **预期结果**：能看到至少数条与 adoption 相关的提交记录；能确认存在 png + pptx 两份资产。
5. **待本地验证**：`git log` 的具体提交条数取决于本地克隆的完整历史，以你本地实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：「dominant LLM engine by AMD」和「default LLM engine for xAI」分别出现在第几行？两者在措辞上有何不同？

> **参考答案**：分别在第 27、28 行（[README.md#L26-L30](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L26-L30)）。措辞上 AMD 用 **dominant**（主力/占主导），强调 SGLang 在 AMD 平台是占主导地位的引擎；xAI 用 **default**（默认），强调 SGLang 是 xAI 默认选用的引擎。两个词都表示「首选」，但视角不同。

**练习 2**：如果要向同事介绍「SGLang 被业界采纳」的证据，本仓库能提供哪三样材料？

> **参考答案**：(1) December 2024 公告里的 AMD/xAI 标志性采纳（第 27–28 行）；(2) February 2025 的公司名单（第 21 行）；(3) 可视化配图 [slides/adoption.png](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/adoption.png)。

---

## 5. 综合实践

把本讲三个模块串起来，完成一份**《SGLang 版本演进时间线表》**。这是本讲的核心交付物。

**任务**：依据 `README.md` 的 Announcement 与 Blog 区段，整理出下表（这里给出表头与第一行作为示范，其余请你自己补全）：

| 版本 / 里程碑 | 日期 | 核心改进（读博客标题提炼） | 对应博客链接（外链） | 是否有仓库内幻灯片 |
|---|---|---|---|---|
| v0.2 | 2024-07-25 | 更快的 Llama3 serving（对标 TensorRT-LLM、vLLM） | https://lmsys.org/blog/2024-07-25-sglang-llama3/ | 是：`slides/sglang_v0_2.pdf` |
| v0.3 | _请补全_ | _请补全（提示：MLA / torch.compile）_ | _请补全_ | _请补全_ |
| v0.4 | _请补全_ | _请补全_ | _请补全_ | _请补全（提示：看第 68 行外链）_ |
| 大规模 EP | 2025-05-05 | _请补全_ | _请补全_ | _请补全（提示：`./slides/sglang_pytorch_china_2025.pdf`）_ |

**操作建议**：

1. 先用 `grep -n 'lmsys.org/blog' README.md` 一次性把所有 LMSYS 博客外链及其行号打印出来，再按日期排序。
2. 对每个版本，回到 [4.2.3](#423-源码精读) 的方法，把「博客标题」拆成「核心改进」要点。
3. 对「是否有仓库内幻灯片」一列，用 `git ls-files slides/` 核对——多数版本没有本地幻灯片，只有外链，请如实填「否（仅外链）」。

**预期结果**：你得到一张 4 行（v0.2 / v0.3 / v0.4 / 大规模 EP）的完整时间线表，能够一眼看清 SGLang「**做快 Llama3 → 做快 DeepSeek MLA → 零开销调度与负载均衡 → 大规模 EP 降本**」这条演进主线。

## 6. 本讲小结

- `## Announcement` 是 README 的「头条公告板」，按月份**倒序**排列了 2024-12 / 2025-02 / 2025-03 / 2025-05 四个里程碑，构成「产品成熟 → 产业采纳 → 融入生态 → 大规模生产」的故事线。
- 2024-12 里程碑一口气交代了 **v0.2 / v0.3 / v0.4** 三个大版本（[README.md#L24](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L24)），它们的详细博客归档在 `## Blog` 区段（第 118/120/122 行）。
- **追根溯源**的关键动作：从 Announcement 的「一句话 + 链接」跳到 Blog 区段的同名条目，再把**博客标题当作核心卖点清单**来读。
- 完整的版本演进线是 **v0.2（Llama3 serving）→ v0.3（7x DeepSeek MLA）→ v0.4（零开销调度 + 负载均衡）→ 大规模 EP（$0.20/1M tokens）**。
- adoption 资料：AMD（dominant）、xAI（default）是标志性采纳（第 27–28 行），February 2025 名单是群体性背书（第 21 行），配图 [slides/adoption.png](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/adoption.png) 是可视化 logo 墙。
- 仓库内**唯一**以版本号命名的幻灯片是 [slides/sglang_v0_2.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang_v0_2.pdf)，登记在 Slides 区段的 `### Other`（第 110 行）。

## 7. 下一步学习建议

本讲把「版本演进」的骨架搭好了，但每个版本背后的**技术机制**还没展开。建议按以下顺序继续：

1. **[u2-l2：调度器与性能优化资料](u2-l2-scheduler-performance.md)**：v0.4 的核心卖点之一是「Zero-Overhead Batch Scheduler」，下一讲会带你找到并阅读调度器、CPU 开销隐藏、FLPM 公平调度相关的幻灯片。
2. **[u2-l3：受限解码与结构化输出资料](u2-l3-constrained-decoding.md)**：对应 v0.4 的「Faster Structured Outputs」卖点，去看 XGrammar 与 Compressed FSM 资料。
3. **[u2-l4：DeepSeek MLA 与模型优化资料](u2-l4-deepseek-mla.md)**：对应 v0.3 的「7x Faster DeepSeek MLA」卖点。
4. **[u2-l5：大规模部署：EP 与 PD 分离资料](u2-l5-large-scale-ep.md)**：对应 2025-05 的大规模 EP 里程碑，是本讲演进线的终点站。

读完这几篇，你就能把本讲的「时间线骨架」逐条填上具体的技术血肉。
