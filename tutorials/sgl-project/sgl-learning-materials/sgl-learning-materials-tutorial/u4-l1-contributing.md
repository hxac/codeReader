# 如何向资料库贡献新内容

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `slides/` 与 `blogs/` 两个内容目录的命名与放置惯例，并能据此为一个新文件取名、放到正确位置。
- 按 README 既有格式（`[日期] [标题](链接)`）写出一整条合规的索引条目，并知道该插到 README 的哪个分区、哪一行。
- 理解配图目录 `blogs/docs/figs/` 的组织方式，以及带空格文件名为什么要做 `%20` 转义。
- 描述一次完整的 PR 贡献流程（fork → 分支 → 放资产 → 改 README → 提交 PR），并理解 `.gitignore` 在其中扮演的角色。

本讲是专家层的第一篇。它不再带你"读资料"，而是教你"往这个资料库里加资料"。因为本仓库**没有任何可执行代码**（回顾 [u1-l2](u1-l2-directory-structure.md)），所以"贡献"在这个项目里就等于两件事：**把资产文件放到正确目录** + **在 README 里登记一条索引**。两者缺一不可。

## 2. 前置知识

在动手前，请确认你已经理解以下来自前置讲义的概念：

- **本仓库是"资料聚合库"而非引擎代码库**（[u1-l1](u1-l1-project-overview.md)）：根目录没有 `main.py`、`package.json` 之类的入口，全部知识资产都靠 README 这一张"导航地图"组织。
- **目录结构**（[u1-l2](u1-l2-directory-structure.md)）：根目录三块基石是 `README.md`、`LICENSE`、`.gitignore`；内容只有 `slides/`、`blogs/` 两个目录；`slides/` 里 25 个 PDF + 1 个 PNG + 1 个 PPTX；`blogs/` 里 1 篇 `.md` 加 `docs/figs/` 下 9 张 PNG。
- **README 的分区与内/外链判断**（[u1-l3](u1-l3-readme-navigation.md)）：以 `slides/`、`blogs/`、`./` 开头的是仓库内资产，以 `https://` 开头的是外链。

如果你对 Git 与 GitHub 的基本操作（fork、branch、commit、push、Pull Request）完全不熟悉，建议先补这部分通用知识；本讲会聚焦"这个仓库特有的约定"，而不是重复教 Git 基础。

## 3. 本讲源码地图

本讲涉及的"源码"其实是仓库的**组织约定文件**，而不是可执行代码：

| 文件 / 目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md) | 唯一的导航索引。贡献的最后一步，就是把新条目按格式登记进这里。 |
| [.gitignore](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/.gitignore) | 只有一行 `.DS_Store`，决定了哪些文件**不应该**进版本库。 |
| [LICENSE](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/LICENSE) | MIT 许可证，版权方 sgl-project（2024）。贡献进来的内容默认以同一许可发布。 |
| [slides/](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides) | 幻灯片资产目录，命名遵循"事件前缀"软约定。 |
| [blogs/](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs) | 博客目录，目前只有一篇长文及其 `docs/figs/` 配图。 |
| [slides/sglang_adoption_logo.pptx](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang_adoption_logo.pptx) | 仓库里**唯一**一个 `.pptx`，其余幻灯片都是 PDF。它说明：目录允许混入非 PDF 资产，但 PDF 才是主流。 |

> 提示：本仓库**没有** `CONTRIBUTING.md`、没有 `.github/` 目录、也没有 PR 模板。这意味着没有一份成文的贡献规范——所有"规矩"都隐含在现有文件里，需要你**照着现有文件抄**。这正是本讲要把这些隐性约定"显性化"的原因。

## 4. 核心概念与源码讲解

### 4.1 资产放置：slides/ 与 blogs/ 的命名惯例

#### 4.1.1 概念说明

一个新资料（一份幻灯片、一篇博客）要进入本仓库，第一步是决定它**叫什么名字**、**放在哪个目录**。这两个决定看似琐碎，却直接决定后续 README 条目能否正确链接到它，也决定后来者能否用关键词检索到它。

本仓库的命名是**软约定（convention）而非强制规则**——没有 CI 检查文件名，也没有 linter。但从现有 27 个 `slides/` 文件可以归纳出几条主流模式。

#### 4.1.2 核心流程

为一份新幻灯片命名时，按以下优先级选择：

1. **优先用"事件前缀 + 小写下划线"**：这是最常见、最易检索的模式。例如某次社区聚会的多份幻灯片共享前缀 `lmsys_1st_meetup_`，AMD 聚会共享 `amd_meetup_`。
2. **无明确事件归属时，用 `sglang_` 或主题词开头**：如 `sglang_v0_2.pdf`、`update_weights_from_distributed.pdf`。
3. **命名前先检索是否已存在近重名**：仓库里已经存在仅靠 `_` 与 `-` 区分的两份文件（见 4.1.3），新文件要避免再造一个"差一个字符"的名字。
4. **扩展名默认用 `.pdf`**：25/27 是 PDF；`.pptx` 与 `.png` 属特例，仅当确实需要源格式或位图时才用。

放置规则更简单：

- 幻灯片 → `slides/`
- 博客正文（`.md`）→ `blogs/`，其配图 → `blogs/docs/figs/`

#### 4.1.3 源码精读

先看 `slides/` 目录最主流的命名——事件前缀。下面是 README 里 `## Slides` 区段的开头，链接全部用**仓库内相对路径** `slides/xxx.pdf`：

[README.md:32-36](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L32-L36) — `## Slides` 标题及其下前两条记录，展示"标题用相对路径指向 slides/ 资产"的标准写法。

事件前缀的典型例子是第一次 LMSYS 线上聚会的五份幻灯片，全部以 `lmsys_1st_meetup_` 开头，靠后缀区分主题：

[README.md:76-84](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L76-L84) — 同一次聚会的多条记录共享 `lmsys_1st_meetup_` 前缀（sglang / constrained_decoding / deepseek_mla / mlcengine / xgrammar），这就是"事件前缀"命名法的范本。

**一个真实的"近重名"反例**：仓库里有两份 router 幻灯片，文件名仅靠分隔符 `_` 与 `-` 区分，分属两个不同事件：

[README.md:62](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L62) — `slides/sglang-router.pdf`（连字符），Hyperbolic 线下聚会的「Cache-Aware Load Balancer」。

[README.md:100](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L100) — `slides/sglang_router.pdf`（下划线），Biweekly 例会的「SGLang Router」。

这两个名字极易混淆，是命名不够统一的历史包袱。你贡献新文件时，**不要**再制造类似的一字之差。

至于非 PDF 资产，仓库唯一一个 `.pptx` 说明了目录的宽容度：

[slides/sglang_adoption_logo.pptx](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang_adoption_logo.pptx) — 采纳情况 logo 墙的源文件。它证明 `slides/` 并不强制 PDF，但 PDF 仍是绝对主流，`.pptx`/`.png` 只是少数特例。

#### 4.1.4 代码实践

**实践目标**：用只读命令摸清 `slides/` 的命名全貌，为后续取名做准备。

**操作步骤**：

1. 列出所有 slides 资产并观察命名：`git ls-files slides/`
2. 统计扩展名分布：`git ls-files slides/ | sed 's/.*\.//' | sort | uniq -c`
3. 检查你想用的名字是否已被占用：`git ls-files slides/ | grep -i 你的关键词`

**需要观察的现象**：

- 扩展名分布应为 `25 pdf / 1 png / 1 pptx`，印证 PDF 是主流。
- `grep -i router` 会同时命中 `sglang-router.pdf` 和 `sglang_router.pdf`——亲眼看到那对"一字之差"。

**预期结果**：你拿到一份完整的命名清单，能据此选一个不与现有文件冲突、且风格统一的新名字。

#### 4.1.5 小练习与答案

**练习 1**：假设你要为一次新的 SGLang 线上 meetup 贡献一份关于「调度器调优」的幻灯片，按主流约定，文件名应该长什么样？

**参考答案**：沿用 meetup 前缀 + 小写下划线，例如 `sglang_meetup_scheduler_tuning.pdf`，放进 `slides/`。关键是带上事件/主题前缀、用小写、用下划线，并先 `git ls-files slides/ | grep -i scheduler` 确认无近重名。

**练习 2**：为什么不应该把文件命名为 `sglang-router2.pdf`？

**参考答案**：因为已存在 `sglang-router.pdf` 与 `sglang_router.pdf` 一对近重名，再加一个 `2` 后缀会让三方更难区分。正确做法是换一个有语义、带事件前缀的新名字。

---

### 4.2 README 条目格式：[日期] [标题](链接)

#### 4.2.1 概念说明

资产放进目录只是"半步贡献"。真正的最后一步，是在 `README.md` 里**登记一条索引**，否则没有任何人能从 README 找到这个文件——它就等于"不存在"。本仓库甚至已经有一个真实案例：`slides/meetup_shenzhen.pdf` 已经进库，却没有任何 README 条目指向它（见 4.2.4）。

本仓库的索引条目格式高度统一，核心模板是：

```
[日期] [标题](链接)
```

其中：

- **日期**：幻灯片/博客的发表或演讲日期，格式 `YYYY-MM-DD`。
- **标题**：人类可读的资料标题。
- **链接**：仓库内资产用相对路径（如 `slides/xxx.pdf`），外部资料用完整 URL。

#### 4.2.2 核心流程

往 README 加一条新条目，按下面顺序决策：

1. **定分区**：幻灯片进 `## Slides`，博客进 `## Blog`，视频进 `## Videos`，论文进 `## Paper`。
2. **定子区段（仅 Slides/Blog/Videos 需要）**：在 Slides 里，按"事件"用 `### 事件名` 归类（如 `### AMD SGLang Meetup`）；找不到匹配事件就放 `### Other`。
3. **定位置**：同一子区段内，按日期**从新到旧**排列（最新在最上）。
4. **写条目**：套用 `[日期] [标题](链接)` 模板；若链接含空格，必须把空格写成 `%20`。
5. **写完核对**：点击链接确认能跳到目标文件。

事件归类规则（来自 [u1-l3](u1-l3-readme-navigation.md)）回顾：事件分 meetup（社区聚会）、学术会议 / Dev Day、biweekly developer sync（双周例会）三类，外加 `### Other` 兜底。

#### 4.2.3 源码精读

**标准条目模板**——一条典型的仓库内幻灯片索引：

[README.md:34](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L34) — `[2025-10-22] [PyTorch Conference 2025 SGLang](slides/sglang_pytorch_2025.pdf)`，这是 `[日期] [标题](相对路径)` 的教科书式范例。

**事件子区段的写法**——用 `###` 给一组同事件条目归类：

[README.md:38-48](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L38-L48) — `### AMD SGLang Meetup` 子区段，下面 5 条同日期（2025-08-22）记录共用一个事件标题，这正是"按事件归类"的范本。

**带空格链接必须转义**——博客正文文件名含空格，README 与博客内部都把空格写成 `%20`：

[README.md:86](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L86) — `[Review of the first LMSYS online meetup...](blogs/Efficient%20LLM%20Deployment%20and%20Serving.md)`，注意 `Efficient%20LLM%20...` 里的 `%20`，不转义则 markdown 链接会在第一个空格处截断。

**两条格式"例外"，贡献时要心里有数**：

[README.md:94-96](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L94-L96) — Biweekly 例会里出现非零填充日期 `[2025-4-22]`、`[2025-1-25]`（而非 `2025-04-22`）。这是历史遗留的不一致；**新条目仍应坚持零填充 `YYYY-MM-DD`**，不要模仿这个例外。

[README.md:108-110](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L108-L110) — `### Other` 兜底区段，其下的 v0.2 条目**完全没有日期前缀**。`### Other` 专门收纳那些"不属于任何已知事件、且无明确日期"的零散资料；新条目若确有日期，就别放这里。

#### 4.2.4 代码实践

**实践目标**：亲手验证"放了文件却没登记"会导致资料"查不到"，体会 README 索引的不可或缺。

**操作步骤**：

1. 确认文件存在：`git ls-files slides/ | grep shenzhen`（应命中 `slides/meetup_shenzhen.pdf`）。
2. 在 README 里找它的索引：`grep -ni shenzhen README.md`。
3. 看该文件的来历：`git log --oneline -- slides/meetup_shenzhen.pdf`。

**需要观察的现象**：

- 第 1 步命中文件，说明资产**已进库**。
- 第 2 步**没有任何输出**，说明 README 里**没有**指向它的条目——这就是"半步贡献"。
- 第 3 步显示它是最新提交 `160433e add shenzhen meetup slides (#24)` 加进来的。

**预期结果**：你亲眼看到一个真实的缺口。修复方法就是按 4.2 的格式，在 `## Slides` 下补一条形如 `[日期] [Shenzhen Meetup - ...](slides/meetup_shenzhen.pdf)` 的条目。这正是本讲综合实践（第 5 节）要你模拟的事情。

> 待本地验证：补登条目时的确切日期与标题需参照该聚会的真实信息；本仓库 HEAD（`160433e`）下此文件确属"已入库未登记"状态。

#### 4.2.5 小练习与答案

**练习 1**：把下面这条原始信息改写成合规的 README 条目——标题「SGLang Router」，日期 2024 年 11 月 16 日，文件 `slides/sglang_router.pdf`。

**参考答案**：

```markdown
[2024-11-16] [SGLang Router](slides/sglang_router.pdf)
```

**练习 2**：你想给一篇名为 `my meetup notes.md`（含空格）的博客登记条目，文件已放进 `blogs/`。链接该怎么写？

**参考答案**：空格必须转义为 `%20`，写成 `blogs/my%20meetup%20notes.md`，否则 markdown 解析时链接会在空格处截断。完整条目示例：`[2025-07-01] [My Meetup Notes](blogs/my%20meetup%20notes.md)`。

**练习 3**：一份新幻灯片不属于任何已知事件，也没有明确演讲日期，该放 README 哪里？

**参考答案**：放进 `## Slides` 下的 `### Other` 子区段（参见 [README.md:108](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L108)），并参照 v0.2 那条省略日期前缀的写法。但只要你有确切日期，就应优先放进匹配的事件子区段并用 `YYYY-MM-DD`。

---

### 4.3 配图目录：docs/figs 与带空格文件名的处理

#### 4.3.1 概念说明

博客正文常常需要配图（架构图、benchmark 图、路线图等）。本仓库的约定是：博客正文（`.md`）放在 `blogs/`，它引用的配图统一放在 `blogs/docs/figs/` 子目录下。配图文件名同样允许含空格，引用时同样要把空格转义成 `%20`。

这套"正文 + 独立配图目录"的结构，让一篇长博客可以携带多张图而不污染正文目录。

#### 4.3.2 核心流程

为博客新增一张配图：

1. 把图片（PNG/JPG）放进 `blogs/docs/figs/`。
2. 文件名建议带"事件或日期前缀"，便于和正文对应（现有 9 张图都以 `1016 meetup - ` 前缀开头，对应 10 月 16 日聚会）。
3. 在博客 `.md` 里用 markdown 图片语法 `![替代文本](docs/figs/文件名)` 引用它——注意路径是**相对于博客 `.md` 所在目录**的，所以从 `blogs/` 看，路径就是 `docs/figs/xxx.png`。
4. 文件名里的空格一律写成 `%20`。

> 关键点：图片链接的相对路径起点是**引用它的 `.md` 文件所在目录**（这里是 `blogs/`），而不是仓库根目录。这与 README 里的 `slides/xxx.pdf`（起点是仓库根）不同，务必区分。

#### 4.3.3 源码精读

看博客正文如何引用配图——每一行都是 `![alt](docs/figs/带%20的文件名)`：

[blogs/Efficient LLM Deployment and Serving.md:3](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L3) — `![Meetup Team](docs/figs/1016%20meetup%20-%20team.png)`：替代文本是 `Meetup Team`，路径相对于 `blogs/` 写成 `docs/figs/...`，空格全部转义为 `%20`。

配图目录本身有 9 张 PNG，命名都带 `1016 meetup - ` 前缀，与正文一一对应（详见 [u3-l1](u3-l1-meetup-blog-reading.md)）：

[blogs/docs/figs/](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/docs/figs) — 配图目录，内含 `1016 meetup - SGLANG scheduler.png`、`1016 meetup - Xgrammer benchmark.png` 等 9 张图，文件名含空格、含大写，引用时都需 `%20` 转义。

而 README 反过来登记这篇博客时，因为起点是仓库根，路径写成 `blogs/Efficient%20LLM%20Deployment%20and%20Serving.md`（已在 4.2.3 引用，[README.md:86](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L86)）。两个 `%20` 出现在不同层级，正说明"相对路径起点决定写法"。

#### 4.3.4 代码实践

**实践目标**：验证"相对路径起点"对链接写法的影响。

**操作步骤**：

1. 看博客如何引用图：`grep -n 'docs/figs' "blogs/Efficient LLM Deployment and Serving.md"`
2. 看 README 如何引用同一篇博客：`grep -n 'Efficient' README.md`

**需要观察的现象**：

- 第 1 步：链接前缀是 `docs/figs/...`（起点是 `blogs/`）。
- 第 2 步：链接前缀是 `blogs/Efficient%20...`（起点是仓库根）。

**预期结果**：同一个"含空格文件名"在两处出现，路径前缀不同，但都遵守"含空格必 `%20`"的规则。这就回答了"为什么不能无脑照抄前缀"——要先想清楚链接的起点在哪。

#### 4.3.5 小练习与答案

**练习 1**：一篇放在 `blogs/`、名为 `sglang notes.md` 的博客要引用 `blogs/docs/figs/bench result.png`，正确的图片 markdown 是什么？

**参考答案**：

```markdown
![bench result](docs/figs/bench%20result.png)
```

因为链接起点是 `blogs/`，所以前缀是 `docs/figs/`，空格转义为 `%20`。

**练习 2**：如果是在**仓库根**的 `README.md` 里直接引用同一张图，路径该怎么写？

**参考答案**：起点变成仓库根，所以前缀要带上 `blogs/`，写成 `blogs/docs/figs/bench%20result.png`。

---

### 4.4 PR 提交流程与 .gitignore

#### 4.4.1 概念说明

前面三节讲的都是"在本地把内容准备对"。这一节讲"如何把它提交进仓库"——也就是 GitHub 的 Pull Request（PR）流程，以及本仓库特有的两个注意点：**没有成文规范**（全靠照抄现有约定），以及 `.gitignore` 只忽略了 `.DS_Store`。

#### 4.4.2 核心流程

一次完整贡献的标准步骤：

```
1. fork 仓库到自己的 GitHub 账号
2. 克隆 fork，新建分支（如 add-new-topic-slides）
3. 把资产文件放进正确目录（slides/ 或 blogs/ + docs/figs/）
4. 在 README.md 登记索引条目（按 4.2 格式）
5. git add 资产文件 + README.md（切勿 add 系统垃圾文件）
6. commit，写清提交信息（如 "add <主题> slides"）
7. push 到自己的 fork
8. 在 GitHub 上向 sgl-project/sgl-learning-materials 发起 PR
```

关于 `.gitignore` 的作用：它告诉 Git **哪些文件不要进版本库**。本仓库的 `.gitignore` 只忽略 `.DS_Store`（macOS 访达自动生成的文件夹元数据）。这有两个含义：

- **只此一项**，意味着仓库对"该忽略什么"几乎没有额外保护——你要自觉不提交编辑器临时文件、`.DS_Store`、大型本地缓存等。
- **它不忽略** `sgl-learning-materials-tutorial/`（本学习手册目录）等内容目录，说明这些目录是仓库的一部分。

许可证层面，仓库采用 MIT：

[LICENSE](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/LICENSE) — `MIT License, Copyright (c) 2024 sgl-project`。你贡献进来的资料会以同一 MIT 许可发布；若资料本身有更严格的版权（如他人幻灯片），需先确认可否以 MIT 再发布。

#### 4.4.3 源码精读

[.gitignore:1](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/.gitignore#L1) — 全文件只有一行 `.DS_Store`。这就是本仓库全部的"忽略规则"，别期待它会替你挡住其他垃圾文件。

仓库顶部还给出对外联系方式，PR 之外的大事（企业采纳、合作）走邮件：

[README.md:1-3](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L1-L3) — 项目简介与联系邮箱 `sglang@lmsys.org`，并指向 Slack 频道。普通资料贡献走 PR 即可，无需邮件。

最后再强调一次"无成文规范"这一事实：仓库里**没有** `CONTRIBUTING.md`、没有 `.github/`、没有 PR 模板（你可用 `git ls-files | grep -iE 'contribut|\.github|template'` 自行验证，结果为空）。所以本讲归纳的四条约定，就是你目前能依赖的最接近"规范"的东西。

#### 4.4.4 代码实践

**实践目标**：在不真的发 PR 的前提下，把一次贡献的本地动作完整演练一遍（只读 + 在本地工作区模拟）。

**操作步骤**：

1. 确认没有成文规范：`git ls-files | grep -iE 'contribut|\.github|template'`（预期无输出）。
2. 确认忽略规则：`cat .gitignore`（预期只有 `.DS_Store`）。
3. 模拟新增资产：在工作区建一个空文件 `slides/new_topic.pdf`（如 `touch slides/new_topic.pdf`，仅本地演练，**不要提交**）。
4. 模拟 README 登记：按 4.2 格式在 `## Slides` 下补一行（见下方"预期结果"）。
5. 检查改动范围：`git status`，确认只有 `slides/new_topic.pdf` 与 `README.md` 两个变更，**没有**混入 `.DS_Store` 等垃圾。

**需要观察的现象**：

- `git status` 应只列出你新增的资产文件和修改过的 `README.md`。
- 若 `git status` 里出现 `.DS_Store` 或其他系统文件，说明你需要在 `git add` 时**只显式 add 那两个目标文件**，而不是 `git add .`。

**预期结果**：演示条目（插在 `## Slides` 下合适的事件子区段或 `### Other`，按日期从新到旧）：

```markdown
[2025-08-22] [SGLang New Topic Deep Dive](slides/new_topic.pdf)
```

演练完成后，请**删掉**本地模拟文件（`rm slides/new_topic.pdf`）并还原 README，避免污染工作区——因为本任务只要求你"写出条目 + 说明目录"，不要求真正提交。

> 待本地验证：`new_topic.pdf` 的日期与标题是模拟值；真实贡献时需替换为该资料的实际信息。本仓库无 CI 校验格式，所以 PR 评审人会人工核对你的条目是否符合 4.2 的约定。

#### 4.4.5 小练习与答案

**练习 1**：为什么在本仓库应该用 `git add slides/new_topic.pdf README.md`，而不是 `git add .`？

**参考答案**：因为 `.gitignore` 只忽略 `.DS_Store`，`git add .` 会把工作区里其他未忽略的系统文件、临时文件一并加入。显式 add 目标文件能确保提交只含资产 + README 两个变更，保持提交干净。

**练习 2**：你想贡献一份他人署名的幻灯片，需要注意什么？

**参考答案**：仓库是 MIT 许可（[LICENSE](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/LICENSE)），贡献内容会以 MIT 再发布。若该幻灯片有更严的版权，需先取得作者同意或确认其许可兼容 MIT，再提交 PR。

---

## 5. 综合实践

把四节串起来，模拟一次完整的"真贡献"（仅本地演练，不真正提交）：

**任务背景**：你参加了一场假想的「SGLang Community Meetup」，做了一场关于「New Topic」的分享，产出一份幻灯片 `new_topic.pdf`。请把它"贡献"进本仓库。

**要求完成以下全部步骤**：

1. **命名与放置**：为文件选定一个符合 4.1 约定的名字（带事件前缀、小写、下划线），并说明它应放在哪个目录。
2. **README 登记**：在 `README.md` 的 `## Slides` 下，**选择正确的子区段**（是新建一个 `### SGLang Community Meetup`，还是放进 `### Other`？说明你的判断理由），并写出**整行**符合 4.2 格式的 markdown 条目。
3. **配图（可选加分）**：如果该分享还有一张架构图 `new_topic arch.png`，说明它该放哪个目录、博客/正文该如何用相对路径引用它（注意 `%20`）。
4. **提交清单**：写出本次 PR 的 `git add` 命令，并说明为什么不用 `git add .`。
5. **自查**：列出本次贡献需要满足的 3 个关键约定（命名、格式、忽略规则）。

**参考答案要点**：

1. 命名如 `sglang_meetup_new_topic.pdf`（或带日期 `sglang_meetup_2025_new_topic.pdf`），放进 `slides/`。先 `git ls-files slides/ | grep -i new_topic` 确认无重名。
2. 若该聚会是新的、且预计还会有后续条目，建议新建子区段 `### SGLang Community Meetup`（参照 [README.md:38](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L38) 的 `### AMD SGLang Meetup` 写法）；若只是一次性零散资料且无明确事件序列，则放 `### Other`。条目示例：

   ```markdown
   [2025-08-22] [SGLang New Topic Deep Dive](slides/sglang_meetup_new_topic.pdf)
   ```

3. 配图放 `blogs/docs/figs/new_topic%20arch.png`（若配套博客在 `blogs/`），正文引用 `![new topic arch](docs/figs/new_topic%20arch.png)`，空格转义 `%20`。
4. `git add slides/sglang_meetup_new_topic.pdf README.md`；不用 `git add .` 是因为 `.gitignore` 只忽略 `.DS_Store`，`git add .` 易带入其他未忽略的系统/临时文件。
5. 三条关键约定：(a) 文件名小写、带事件前缀、无近重名；(b) README 条目用 `[YYYY-MM-DD] [标题](相对路径)`、含空格用 `%20`、按日期从新到旧；(c) 只 `git add` 目标资产与 README，不提交 `.DS_Store` 等垃圾。

## 6. 本讲小结

- **贡献 = 放资产 + 登记 README**，两步缺一不可；仓库里 `slides/meetup_shenzhen.pdf` 就是"只放未登记"的真实反例。
- **命名是软约定**：`slides/` 主流是小写 + 事件前缀 + 下划线，PDF 为主；`sglang-router.pdf` 与 `sglang_router.pdf` 这对近重名提醒你取名前先 `grep`。
- **README 条目统一格式** `[日期] [标题](链接)`：仓库内资产用相对路径，外链用 URL；按事件用 `###` 归类，同段内日期从新到旧；`### Other` 收纳零散无日期资料。
- **含空格的链接必须 `%20` 转义**，且相对路径起点决定前缀（博客正文从 `blogs/` 起写 `docs/figs/...`，README 从仓库根起写 `blogs/...`）。
- **`.gitignore` 只忽略 `.DS_Store`**，且仓库**无 `CONTRIBUTING.md` / PR 模板**，所以一切约定靠"照抄现有文件"。
- **PR 流程**是标准 fork→分支→放资产→改 README→显式 `git add`→commit→push→发 PR；贡献内容按 MIT 许可发布。

## 7. 下一步学习建议

- **[u4-l2 设计个人 SGLang 学习路线](u4-l2-study-plan.md)**：学会反过来用本仓库资料为不同目标（部署 / 优化 / 硬件 / 安全）规划学习顺序——贡献者往往也是最懂资料分类的人。
- **[u4-l3 资料的边界与延伸资源](u4-l3-external-resources.md)**：厘清本仓库"只聚合、不含代码"的边界，并在贡献时正确衔接 SGLang 文档站、论文与 `sgl-project/sglang` 主仓库。
- **动手前的热身**：重读 [u1-l3 把 README 当作导航地图](u1-l3-readme-navigation.md) 的"主题→README 位置"速查表，它能帮你在贡献时快速判断一条新资料该归入哪个事件子区段。
