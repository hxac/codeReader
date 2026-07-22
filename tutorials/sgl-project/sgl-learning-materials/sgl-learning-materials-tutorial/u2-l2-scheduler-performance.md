# 调度器与性能优化资料

## 1. 本讲目标

本讲是「按主题绘制资料地图」单元的第二篇。学完之后，你应当能够：

- 在 `README.md` 的 `## Slides` 区段里，**按主题**而不是按日期，定位到三份与「调度 / 性能」直接相关的幻灯片。
- 理解 **CPU Overhead Hiding（CPU 开销隐藏）** 这一思路的直觉，并把它和 v0.4 的核心卖点「Zero-Overhead Batch Scheduler（零开销批调度器）」对应起来——这正是 [u2-l1](u2-l1-version-evolution.md) 留下的「时间线骨架」上的第一块技术血肉。
- 知道 **FLPM** 是 SGLang 的一个「公平且高效」的调度算法，理解调度里「公平 vs 效率」的经典权衡。
- 学会一种实用技巧——**「幻灯片 + 同日期 YouTube 视频」配对阅读**：README 的 `## Slides` 与 `## Videos` 两个区段里，同一天的同名条目往往是「看 PDF 幻灯片」与「看作者视频讲解」的两种载体。

> 提醒（承接 [u1-l1](u1-l1-project-overview.md) 与 [u2-l1](u2-l1-version-evolution.md)）：本仓库是**资料聚合库**，不含运行时代码。所以本讲的「源码精读」对象是 `README.md` 里登记这三份幻灯片的文字条目、它们的 git 历史，以及三份 PDF 幻灯片本身。受运行环境限制，本讲**无法**逐页提取 PDF 内文，凡涉及幻灯片**内部具体页码与措辞**的细节，一律标注「待本地验证」，请你打开 PDF 后自行核对——这本身也是本讲的练习之一。

## 2. 前置知识

本讲默认你已经读完：

- [u1-l3：把 README 当作导航地图](u1-l3-readme-navigation.md)——知道 `## Slides` 区段用 `###` 子标题做**事件归类**（meetup / 学术会议 / biweekly developer sync / Other），每条记录格式为 `[日期] [标题](链接)`。
- [u2-l1：版本演进与里程碑](u2-l1-version-evolution.md)——知道 v0.4 的博客标题（[README.md#L118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118)）里写着「Zero-Overhead Batch Scheduler, Cache-Aware Load Balancer」。本讲就是把这两个词拆开来讲。

本讲用到几个新名词，先用大白话解释：

| 名词 | 通俗解释 |
|---|---|
| **调度器（scheduler）** | 服务引擎里「决定下一步让哪些请求进 GPU 算」的那个部件，就像十字路口的红绿灯指挥官。 |
| **CPU 开销隐藏（CPU overhead hiding）** | 让 CPU「排班」的同时，GPU 不闲着继续算，把 CPU 的排队时间「藏」到 GPU 的计算时间里，省得 GPU 干等。 |
| **公平调度（fair scheduling）** | 在多个用户/请求共享 GPU 时，尽量让每个人都有公平的机会，避免有人一直排不上队（饿死）。 |
| **吞吐（throughput）** | 单位时间能处理多少 token 或多少请求，是衡量「快不快、省不省」的核心指标。 |

还要回顾一条来自 [u1-l3](u1-l3-readme-navigation.md) 的检索技能：判断一份资料是仓库内资产还是外链，看链接是否以 `https://` 开头。本讲三份幻灯片**都是仓库内 PDF**（链接以 `slides/` 开头），这是它们能被「逐页打开」的前提。

## 3. 本讲源码地图

本讲涉及一个文字文件加三份 PDF 幻灯片，全部是仓库内真实存在的资产：

| 文件 | 类型 | 在本讲的作用 |
|---|---|---|
| [README.md](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md) | Markdown | **索引核心**。`## Slides` 与 `## Videos` 两个区段里登记了这三份幻灯片的标题、日期、所属事件，以及配套的 YouTube 视频。本讲的事实性结论几乎全部从 README 文字得出。 |
| [slides/lmsys_1st_meetup_sglang.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/lmsys_1st_meetup_sglang.pdf) | PDF 幻灯片 | 第一届 LMSYS 线上 meetup 的 SGLang 主题幻灯片，标题为「SGLang Overview & CPU Overhead Hiding」。对应 **4.1 模块**。 |
| [slides/sglang-FLPM.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang-FLPM.pdf) | PDF 幻灯片 | SGLang 双周开发者例会（biweekly）上的一场分享，标题为「A fair and efficient scheduling algorithm」。对应 **4.2 模块**。 |
| [slides/SGLang-Performance-Optimization-YinengZhang.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/SGLang-Performance-Optimization-YinengZhang.pdf) | PDF 幻灯片 | GPU MODE 社区的一场性能优化分享，演讲者 Yineng Zhang，标题为「SGLang Performance Optimization」。对应 **4.3 模块**。 |

> 工具箱（承接 [u1-l3](u1-l3-readme-navigation.md)）：本讲反复使用 `grep -n '关键词' README.md` 定位行号、`git ls-files slides/` 列出全部幻灯片、`git log --oneline -- <文件>` 追溯提交历史。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，每个模块对应一份幻灯片：

- **4.1 SGLang 概览与 CPU 开销隐藏**（`lmsys_1st_meetup_sglang.pdf`）
- **4.2 FLPM 公平调度算法**（`sglang-FLPM.pdf`）
- **4.3 GPU MODE 性能优化分享**（`SGLang-Performance-Optimization-YinengZhang.pdf`）

三份幻灯片分别落在 README 的三个**不同事件子区段**里（meetup / biweekly / GPU MODE），这正好示范了 [u1-l3](u1-l3-readme-navigation.md) 强调的「同一主题会横跨多个事件、构成资料簇」——「调度与性能」这一主题就是靠把它们三个串起来，才拼出全貌。

> 本讲下面的「概念说明 / 核心流程」是帮助你看懂幻灯片的**通用背景知识**；凡是幻灯片内部具体讲了哪些页、哪些公式，都以打开 PDF 后看到的原文为准（标注「待本地验证」）。

---

### 4.1 SGLang 概览与 CPU 开销隐藏

#### 4.1.1 概念说明

这份幻灯片的标题「SGLang Overview & CPU Overhead Hiding」由两部分组成：

1. **SGLang Overview（概览）**：介绍 SGLang 是什么、整体架构如何。
2. **CPU Overhead Hiding（CPU 开销隐藏）**：这是性能部分的重头戏。

要理解「CPU 开销隐藏」，先看一个 LLM 推理服务在每一步（生成一个 token）时，CPU 和 GPU 各自要做什么：

- **CPU 的活**：调度——决定下一步这一批里放哪些请求、管理 KV Cache 显存、做准入控制、拼好 batch。
- **GPU 的活**：跑模型的前向计算（attention、FFN 等）。

如果这两件事**串行**做（CPU 先排完班，GPU 再算，算完 CPU 再排下一班……），GPU 在 CPU 排班时就**干等**。这段干等时间就是「CPU 开销」。**CPU 开销隐藏**的核心想法是让两者**重叠（overlap）**：当 GPU 正在跑第 N 步的前向计算时，CPU 同时就在排第 N+1 步的班。于是 CPU 的排队时间被「藏」到了 GPU 的计算时间里，GPU 几乎不再空转。

> 这个思路并非 SGLang 独有，而是高性能 LLM serving 的通用优化方向。把它当背景知识看，去幻灯片里核对 SGLang 具体是怎么实现的。

#### 4.1.2 核心流程

用一张时间轴说明「隐藏」前后的差别（示意，具体实现以幻灯片原文为准——待本地验证）：

```text
【不隐藏：串行】
  CPU: [排班 N]            [排班 N+1]            [排班 N+2]
  GPU:          [算 N]              [算 N+1]            [算 N+2]
       ↑ GPU 在 CPU 排班时空等，利用率低

【隐藏：重叠】
  CPU: [排班 N ] [排班 N+1   ] [排班 N+2   ] ...   ← 排班与上一步的 GPU 计算并行
  GPU:           [算 N       ] [算 N+1     ] [算 N+2] ...
       ↑ CPU 排班被「藏」进 GPU 计算时间，GPU 持续忙碌
```

可以粗略地把 GPU 利用率写成（\(T_{\text{cpu}}\) 为排班耗时，\(T_{\text{gpu}}\) 为计算耗时）：

- 串行：GPU 利用率 \(\approx \dfrac{T_{\text{gpu}}}{T_{\text{cpu}}+T_{\text{gpu}}}\)
- 重叠隐藏：GPU 利用率 \(\approx \dfrac{T_{\text{gpu}}}{\max(T_{\text{cpu}},\,T_{\text{gpu}})}\)

当 \(T_{\text{gpu}} \ge T_{\text{cpu}}\) 时，分母里的 \(T_{\text{cpu}}\) 被「吸收」，利用率趋近 100%。这正是 v0.4 把它命名为 **Zero-Overhead Batch Scheduler（零开销批调度器）** 的来历——CPU 的开销被消成了「零」（相对 GPU 而言）。

#### 4.1.3 源码精读

**幻灯片登记位置**，见 [README.md#L76](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L76)。这一行是：

```text
[2024-10-16] [SGLang Overview & CPU Overhead Hiding](slides/lmsys_1st_meetup_sglang.pdf)
```

它被归在 `### The first LMSYS online meetup: Efficient LLM Deployment and Serving` 这个事件子区段下，区段标题见 [README.md#L74](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L74)。也就是说，这是 2024 年 10 月 16 日**第一届 LMSYS 线上 meetup**上的一场分享。同一场 meetup 还有受限解码、DeepSeek MLA、XGrammar、MLC-LLM 等多场分享（[README.md#L78-L84](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L78-L84)），本讲的这份是其中的「SGLang 本体」专场。

**和 v0.4 卖点的呼应**。这场 meetup 在 2024-10，而 [u2-l1](u2-l1-version-evolution.md) 讲过的 v0.4 发布在 2024-12，其博客标题（[README.md#L118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118)）写着「Zero-Overhead Batch Scheduler」。把这两条对照起来，就能看出一条清晰的技术演进：

```text
2024-10 meetup：CPU Overhead Hiding（思路命名：把 CPU 开销“藏”起来）
        ↓
2024-12 v0.4：Zero-Overhead Batch Scheduler（产品命名：CPU 开销相对 GPU 已可忽略）
```

**配套资料（资料簇）**。这场 meetup 还有两份配套资料可一起读：(1) meetup 回顾博客，仓库内唯一的长 `.md`，登记在 [README.md#L86](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L86)；(2) 整场 meetup 的 YouTube 录像，登记在 `## Videos` 区段下（见 [README.md#L156-L158](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L156-L158)）。

**git 溯源**。这份 PDF 是在一次「从 PPT 改用 PDF」的整理提交里定型的，提交信息为 `93a5f2f upload LMSYS first meetup slides and use pdf instead of ppt`。可用以下命令核对（4.1.4 会用到）：

```bash
git show --stat 93a5f2f
```

#### 4.1.4 代码实践

1. **实践目标**：用一条命令确认这份幻灯片的「标题 + 日期 + 所属事件」，并追溯它的提交历史。
2. **操作步骤**：

   ```bash
   grep -n 'CPU Overhead' README.md
   git log --oneline -- slides/lmsys_1st_meetup_sglang.pdf
   ```

3. **需要观察的现象**：
   - 第一条命令打印出第 **76** 行，标题里包含 `CPU Overhead Hiding`，日期 `2024-10-16`。
   - 第二条命令至少能看到提交 `93a5f2f`（PDF 化）以及更早的 `58cfa27`（首次上传该 meetup 幻灯片）。
4. **预期结果**：行号 = 76；提交历史里出现 `93a5f2f`。与 4.1.3 的引用一致。
5. **待本地验证**：`git log` 的具体提交条数与文案以你本地实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：这份幻灯片标题的两部分分别讲什么？它们和 v0.4 的哪个卖点直接对应？

> **参考答案**：两部分是「SGLang Overview（概览）」与「CPU Overhead Hiding（CPU 开销隐藏）」。后者直接对应 v0.4 的 **Zero-Overhead Batch Scheduler** 卖点（[README.md#L118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118)）：meetup（2024-10）提出「隐藏」思路，v0.4（2024-12）把它做成「零开销」产品特性。

**练习 2**：用一句话解释，为什么「把 CPU 排班藏到 GPU 计算时间里」能提升吞吐？

> **参考答案**：因为这样 GPU 不再在 CPU 排班时空等，GPU 利用率从 \(\frac{T_{\text{gpu}}}{T_{\text{cpu}}+T_{\text{gpu}}}\) 提升到接近 \(\frac{T_{\text{gpu}}}{\max(T_{\text{cpu}},T_{\text{gpu}})}\)，单位时间内能算更多 token，吞吐自然上升。

---

### 4.2 FLPM 公平调度算法

#### 4.2.1 概念说明

第二份幻灯片的标题是「A fair and efficient scheduling algorithm（一个公平且高效的调度算法）」，文件名里的 **FLPM** 就是这个算法的名字。它登记在 `### SGLang Biweekly Meeting`（SGLang 双周开发者例会）子区段下——「双周例会」通常是偏深入、偏研究的话题分享，定位上比 meetup 更「硬核」。

这份资料要回答的核心问题是：**当多个用户/请求共享同一批 GPU 时，调度器怎么做到既「公平」又「高效」？** 这是一个经典的两难：

- **只追求效率（吞吐最大化）**：调度器会优先挑「容易算、能凑满 batch」的请求，结果可能是某些「难算」或「排得靠后」的请求长时间得不到服务，即**饿死（starvation）**。
- **只追求公平**：严格轮流服务每个请求，可能凑不出好 batch，GPU 利用率下降，整体**吞吐降低**。

FLPM 的目标就是在这两端之间找平衡。它具体是怎么定义「公平」、用什么数据结构或策略来兼顾效率的——**这部分必须以幻灯片原文为准**（待本地验证），本讲只提供背景框架帮你读得进去。

> 关于「FLPM」这个缩写的全称，本仓库 README 与本讲运行环境均无法确认，标注**待确认**。读幻灯片时请留意它的展开写法。

#### 4.2.2 核心流程

「公平调度」类算法的通用思考框架通常包含三步（示意，FLPM 的具体设计以原文为准——待本地验证）：

```text
第 1 步：度量公平性
        —— 给每个用户/请求定义一个“已用资源”或“等待时间”的度量
第 2 步：在“公平”与“效率”之间权衡
        —— 既要照顾落后的请求（保公平），又要尽量凑出高利用率的 batch（保吞吐）
第 3 步：给出调度决策
        —— 输出“下一步 batch 里放进哪些请求”
```

可以把「公平 vs 效率」的权衡想成一条权衡曲线：一个调度算法的价值，就是在这条曲线上找到一个既不太偏向吞吐、也不太偏向严格公平的**工作点**。FLPM 正是 SGLang 给出的这个工作点的具体算法。

#### 4.2.3 源码精读

**幻灯片登记位置**，见 [README.md#L96](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L96)。这一行是：

```text
[2025-1-25] [A fair and efficient scheduling algorithm](slides/sglang-FLPM.pdf)
```

它归在 `### SGLang Biweekly Meeting` 子区段下（区段标题见 [README.md#L92](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L92)）。注意这里的日期写法是 `[2025-1-25]`——月份和日**没有补零**，这是 biweekly 子区段里常见的非标准写法（[u1-l3](u1-l3-readme-navigation.md) 已提醒过按关键词检索更稳妥）。

**和 4.1 的分工**。如果把 4.1 的「CPU Overhead Hiding」理解为「让调度器跑得**不拖累 GPU**」（纵向：CPU↔GPU 重叠），那么 4.2 的「公平调度」就是解决「调度器在**众多请求之间**怎么分配 GPU」（横向：请求↔请求公平）。两者合起来，才是一个完整调度器的两个面：

| 维度 | 模块 | 关心的对象 | 标题关键词 |
|---|---|---|---|
| 纵向（CPU↔GPU） | 4.1 | 让 CPU 排班不拖累 GPU | CPU Overhead Hiding |
| 横向（请求↔请求） | 4.2 | 让多个请求公平又高效地分到 GPU | fair and efficient |

**配套视频**。这场 biweekly 分享也有同日期的 YouTube 录像：「SGLang Developer Sync 20250125」，登记在 `## Videos` 区段的 `### SGLang Biweekly Meeting` 下（[README.md#L164-L166](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L164-L166)）。日期 `2025-01-25` 与幻灯片的 `2025-1-25` 是同一天（仅补零写法不同）——这是「PDF 幻灯片 + YouTube 讲解」配对阅读的好例子。

**git 溯源**。这份 PDF 的引入提交是 `21df9d1 add FLPM talk (#13)`，可用以下命令核对：

```bash
git show --stat 21df9d1
```

#### 4.2.4 代码实践

1. **实践目标**：用命令验证「幻灯片条目」与「同日期 YouTube 视频」是同一场分享的两种载体。
2. **操作步骤**：

   ```bash
   grep -n 'FLPM\|fair and efficient' README.md
   grep -n '2025-01-25\|2025-1-25\|Developer Sync 20250125' README.md
   ```

3. **需要观察的现象**：
   - 第一条命中第 **96** 行（FLPM 幻灯片）。
   - 第二条会同时命中 Slides 区段的 `[2025-1-25]` 行与 Videos 区段的 `[2025-01-25] SGLang Developer Sync 20250125` 行。
4. **预期结果**：两条命令交叉印证「2025-01-25 这天有一场 biweekly，既有 PDF 幻灯片、又有 YouTube 录像」。
5. **待本地验证**：grep 的精确命中行号以本地 README 为准。

#### 4.2.5 小练习与答案

**练习 1**：FLPM 幻灯片登记在哪个事件子区段？它的标题里强调了哪两个目标？

> **参考答案**：登记在 `### SGLang Biweekly Meeting`（[README.md#L92](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L92)），具体在 [README.md#L96](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L96)。标题强调**公平（fair）**与**高效（efficient）**两个目标。

**练习 2**：「CPU Overhead Hiding」和「FLPM 公平调度」分别解决调度器的哪个面？为什么说它们是互补的？

> **参考答案**：前者解决**纵向**——CPU 排班与 GPU 计算的重叠，让 CPU 不拖累 GPU；后者解决**横向**——多个请求之间如何公平又高效地分到 GPU。一个关心「调度别卡 GPU」，一个关心「调度别偏心」，合起来才是完整调度器，故互补。

**练习 3**（开放题，待本地验证）：打开 [slides/sglang-FLPM.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang-FLPM.pdf)，找出 FLPM 缩写的全称，并记下它用什么度量来衡量「公平」。

> **参考答案**（框架）：以幻灯片原文为准。请把查到的全称与度量方式填回此处——这是本讲特意留给你的「打开 PDF 核对」练习。

---

### 4.3 GPU MODE 性能优化分享

#### 4.3.1 概念说明

第三份幻灯片是 GPU MODE 社区的一场分享，标题「SGLang Performance Optimization」，从文件名可知演讲者是 **Yineng Zhang**。**GPU MODE**（原 CUDA MODE）是一个面向 GPU / CUDA 开发者的技术社区，分享内容通常偏底层、偏「工程实操」，比如怎么定位瓶颈、怎么用 profiling 工具、怎么改 kernel。

与前两份相比，它的定位很不一样：

| 幻灯片 | 来源事件 | 偏重 |
|---|---|---|
| 4.1 `lmsys_1st_meetup_sglang.pdf` | LMSYS meetup（官方主场） | 体系级概述 + 核心思路 |
| 4.2 `sglang-FLPM.pdf` | SGLang biweekly（开发者例会） | 单个算法深入 |
| 4.3 `SGLang-Performance-Optimization-YinengZhang.pdf` | GPU MODE（外部技术社区） | 工程实操 / 性能优化经验 |

所以这份幻灯片最适合回答「**我想知道 SGLang 性能优化具体都做了哪些事、怎么调**」的问题——这是它的独特价值。它内部具体列了哪些优化点，仍以 PDF 原文为准（待本地验证）。

> 一个有意思的旁证：这份 PDF 在 git 里有 **41 MB** 之大（提交 `5a576f9`，`41134242 bytes`），远大于前两份（FLPM 约 1.8 MB）。体积大通常意味着页数多、配图/截图多，侧面说明这是一份「图很多、很实操」的分享——但这只是推断，具体以打开后所见为准。

#### 4.3.2 核心流程

阅读一份「性能优化」类分享，通用套路是「**找瓶颈 → 改优化 → 再测**」的循环：

```text
第 1 步：测量（profile）
        —— 用 profiling 工具找出“GPU 时间花在哪一段”
第 2 步：定位瓶颈
        —— 是 attention 慢？是调度慢？是显存搬运慢？
第 3 步：施加优化
        —— kernel 优化 / 重叠 / 调度改进 / 显存复用 …
第 4 步：复测对比
        —— 看吞吐 / 延迟是否真的改善，再回到第 1 步
```

带着这个框架去看 [slides/SGLang-Performance-Optimization-YinengZhang.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/SGLang-Performance-Optimization-YinengZhang.pdf)，就能把里面的各种优化点对号入座。它具体讲了哪些优化（哪些对应 4.1 的调度重叠、哪些是别的话题），需打开 PDF 核对（待本地验证）。

#### 4.3.3 源码精读

**幻灯片登记位置**，见 [README.md#L72](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L72)。这一行是：

```text
[2024-11-10] [SGLang Performance Optimization](slides/SGLang-Performance-Optimization-YinengZhang.pdf)
```

它归在 `### GPU MODE` 子区段下（[README.md#L70](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L70)）。注意：README 里**同时存在** `### GPU MODE`（Slides 区段，第 70 行）和 `### GPU MODE`（Videos 区段，第 152 行）两个同名子标题——它们分属两个顶层区段，不是重复。

**「幻灯片 + 视频」配对（本模块的核心技巧）**。GPU MODE 这场分享在 `## Videos` 区段里也有同名条目，且**日期完全相同**，见 [README.md#L154](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L154)：

```text
[2024-11-10] [SGLang Performance Optimization](https://www.youtube.com/watch?v=XQylGyG7yp8)
```

两者日期都是 `2024-11-10`、标题都是 `SGLang Performance Optimization`，显然是同一场分享的「PDF 幻灯片」与「YouTube 录像」两种载体。这是本讲最值得记住的阅读技巧：**遇到一份 PDF 幻灯片，先去 `## Videos` 区段按同日期/同名找有没有配套视频**——有视频时，先听作者讲一遍，再回头看 PDF，效率高得多。三份幻灯片的配套视频对照如下：

| 幻灯片（Slides 区段） | 日期 | 配套视频（Videos 区段） |
|---|---|---|
| 4.1 `lmsys_1st_meetup_sglang.pdf`（第 76 行） | 2024-10-16 | 整场 meetup 录像（[README.md#L158](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L158)） |
| 4.3 `SGLang-Performance-Optimization-YinengZhang.pdf`（第 72 行） | 2024-11-10 | 同名 GPU MODE 录像（[README.md#L154](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L154)） |
| 4.2 `sglang-FLPM.pdf`（第 96 行） | 2025-1-25 | Developer Sync 20250125（[README.md#L166](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L166)） |

**git 溯源**。引入提交为 `5a676f9 docs: add SGLang Performance Optimization GPU MODE talk slide`，可用以下命令核对（含体积）：

```bash
git show --stat 5a676f9
```

#### 4.3.4 代码实践

1. **实践目标**：用命令把「GPU MODE 幻灯片」与「GPU MODE 视频」两条记录一起打印出来，亲手验证配对关系。
2. **操作步骤**：

   ```bash
   grep -n 'GPU MODE' README.md
   grep -n 'SGLang Performance Optimization' README.md
   git show --stat 5a676f9 | head
   ```

3. **需要观察的现象**：
   - 第一条会命中**两处** `### GPU MODE`：第 **70** 行（Slides）与第 **152** 行（Videos）。
   - 第二条会命中**两行**同名条目：第 **72** 行（PDF）与第 **154** 行（YouTube），日期都是 `2024-11-10`。
   - 第三条会显示该 PDF 体积约 `41134242 bytes`（约 41 MB）。
4. **预期结果**：两个同名 `### GPU MODE` 子标题分属 Slides / Videos 两个区段；同名条目行号 72 与 154 配对成功。
5. **待本地验证**：具体输出以本地为准。

#### 4.3.5 小练习与答案

**练习 1**：README 里出现两次 `### GPU MODE`，分别在哪些顶层区段下？它们是重复的吗？

> **参考答案**：一次在 `## Slides` 下（[README.md#L70](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L70)），一次在 `## Videos` 下（[README.md#L152](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L152)）。不是重复——前者登记 PDF 幻灯片，后者登记 YouTube 录像，是同一场分享的两种载体。

**练习 2**：你想最快地搞懂 GPU MODE 这场性能优化分享，应该怎么读？

> **参考答案**：先看 `## Videos` 区段第 154 行的 YouTube 录像（[README.md#L154](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L154)）听作者讲一遍，再回到第 72 行的 PDF（[README.md#L72](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L72)）细看具体页面与数据。

---

## 5. 综合实践

把本讲三份幻灯片串起来，完成本讲指定的实践任务：**阅读三份幻灯片，用要点列表写出 SGLang 提升吞吐的 3 个关键机制，并标注每条分别出自哪一份幻灯片。**

### 第一步：先用「标题」建立假设

在打开 PDF 之前，仅凭 README 登记的标题，就能为每个机制锁定来源（这一步完全可由 README 验证）：

| 候选机制 | 来源幻灯片 | 锁定依据（README 行号） |
|---|---|---|
| CPU 开销隐藏 / 零开销批调度（CPU↔GPU 重叠） | 4.1 `lmsys_1st_meetup_sglang.pdf` | 标题含 `CPU Overhead Hiding`（[README.md#L76](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L76)），呼应 v0.4 `Zero-Overhead Batch Scheduler`（[README.md#L118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118)） |
| 公平且高效的调度（请求↔请求 分配） | 4.2 `sglang-FLPM.pdf` | 标题 `A fair and efficient scheduling algorithm`（[README.md#L96](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L96)） |
| 一组工程级性能优化（profile→定位→优化→复测） | 4.3 `SGLang-Performance-Optimization-YinengZhang.pdf` | 标题 `SGLang Performance Optimization`（[README.md#L72](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L72)） |

### 第二步：打开 PDF 填充细节（待本地验证）

逐份打开三份 PDF（建议先看配套 YouTube 视频），把每个机制下的**具体做法**补充进去。下面给出一个填写模板，把「待本地验证」处替换成你在 PDF 里看到的真实内容：

```text
机制 1（出处：4.1 lmsys_1st_meetup_sglang.pdf / CPU Overhead Hiding）
  - 做法：把 CPU 排班与 GPU 计算重叠，使 CPU 开销相对 GPU 可忽略
  - PDF 中的具体图示/数据：待本地验证（记录页码与数值）

机制 2（出处：4.2 sglang-FLPM.pdf / FLPM）
  - 做法：在请求间做公平且高效的调度
  - FLPM 全称与公平度量：待确认 / 待本地验证

机制 3（出处：4.3 SGLang-Performance-Optimization-YinengZhang.pdf）
  - 做法：profile → 定位瓶颈 → 优化 → 复测 的工程循环
  - PDF 中列举的具体优化点：待本地验证（逐条记录）
```

### 预期结果

完成后，你得到一份「**机制 → 出处 → 具体做法**」三列的要点清单，既能回答「SGLang 提升吞吐靠什么」，又能说清「这些信息去仓库的哪一份幻灯片里查」。这也直接为 [u2-l1](u2-l1-version-evolution.md) 的 v0.4 卖点（Zero-Overhead Batch Scheduler、Cache-Aware Load Balancer）补上了资料落点。

## 6. 本讲小结

- 「调度与性能」是一个**横跨多事件**的资料簇：4.1 在 LMSYS meetup、4.2 在 biweekly 例会、4.3 在 GPU MODE 社区，分别登记在 `## Slides` 的三个不同 `###` 子区段（[README.md#L74](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L74)、[L92](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L92)、[L70](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L70)）。
- **CPU Overhead Hiding**（[README.md#L76](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L76)）解决「调度纵向不拖累 GPU」，是 v0.4 **Zero-Overhead Batch Scheduler**（[README.md#L118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118)）的前身思路。
- **FLPM**（[README.md#L96](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L96)）解决「调度横向在请求间公平又高效」，标题强调 fair + efficient；其缩写全称**待确认**。
- **GPU MODE 性能优化分享**（[README.md#L72](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L72)）偏工程实操，回答「具体做了哪些优化、怎么调」。
- 关键阅读技巧：**每份 PDF 幻灯片都去 `## Videos` 区段按同日期/同名找配套录像**——4.1 对应整场 meetup 录像（[L158](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L158)）、4.3 对应同名 GPU MODE 视频（[L154](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L154)）、4.2 对应 Developer Sync 20250125（[L166](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L166)）。
- 三份幻灯片的 git 来源：4.1 定型于 `93a5f2f`、4.2 引入于 `21df9d1`、4.3 引入于 `5a676f9`。
- 受运行环境限制，本讲未逐页提取 PDF 内文；幻灯片内部具体页面、公式、措辞均标注「待本地验证」，请打开 PDF 核对。

## 7. 下一步学习建议

本讲把「调度与性能」这条主题线讲清了，建议按以下顺序继续：

1. **[u2-l3：受限解码与结构化输出资料](u2-l3-constrained-decoding.md)**：转到 v0.4 的另一个卖点「Faster Structured Outputs」，去看受限解码与 XGrammar / Compressed FSM 资料。
2. **[u2-l4：DeepSeek MLA 与模型优化资料](u2-l4-deepseek-mla.md)**：对应 v0.3 的「7x Faster DeepSeek MLA」卖点，是模型层面的优化（与本讲调度层面的优化互补）。
3. **主仓库代码衔接（[u4-l3](u4-l3-external-resources.md) 会详讲）**：如果想看调度器的**真实实现**而不是幻灯片，需要跳到引擎主仓库 `sgl-project/sglang`（本仓库不含运行时代码，[u1-l1](u1-l1-project-overview.md) 已明确这一边界）。
4. **想听作者讲调度**：直接看本讲 4.1 / 4.2 对应的两段 YouTube 录像（[L158](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L158)、[L166](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L166)），比单看 PDF 更直观。

读完上述资料，你就能把 v0.4「Zero-Overhead Batch Scheduler + Cache-Aware Load Balancer」这句话里的每个词，都找到对应的幻灯片与视频落点。
