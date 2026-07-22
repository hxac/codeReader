# 资料的边界与延伸资源

## 1. 本讲目标

这是整本手册的**收官篇**。前面 15 讲，你一直在「本仓库内部」读幻灯片、读博客、画资料地图。本讲要做的恰恰相反——**抬起头，看清本仓库的边界在哪里，以及边界之外通向何方**。

学完本讲，你应当能够：

1. **说清本仓库的能力边界**：它聚合资料，但**不含任何运行时代码**；并能用 `git ls-files` 当场证明这一点，而不是凭印象下结论。
2. **掌握全部外部延伸入口**：把 README 末尾的 Documentation / Paper / Videos 与正文里的 Slack、以及博客页脚里更全的链接，整理成一张「延伸资源清单」，知道每个入口适合解决什么问题。
3. **学会跳转到主仓库 `sgl-project/sglang`**：知道哪些需求（看实现、找可配置开关）必须离开本仓库、去哪里找。
4. **贯通「资料 → 用法 → 原理 → 代码」这条递进链路**，理解四个层级各自的角色与衔接方式。

> 本讲承接 u4-l2 的 4.3「资料与外部文档/论文/主仓库的衔接」。u4-l2 在一张表里给了 5 个通道的雏形；本讲是它的**专题展开版**——补上具体 URL、用第二个源文件（博客）交叉验证、并用命令把「边界」坐实。

## 2. 前置知识

本讲默认你已经建立以下认知（若某条陌生，建议先补对应讲义）：

- **本仓库是「资料聚合库」而非运行时代码库**：核心资产是 `README.md` 导航索引 + `slides/` + `blogs/`，许可证为 MIT（详见 u1-l1、u1-l2）。
- **README 是一张导航地图**：分 Announcement / Slides / Blog / Videos / Paper / Documentation 六大区段，越靠下越「稳定长青」（详见 u1-l3）。
- **判断资料归属看链接前缀**：`slides/`、`blogs/`、`./` 开头是仓库内资产，`https://` 开头是外部链接（详见 u1-l3）。
- **四个目标抽屉与外衔接通道雏形**：优化 / 部署 / 硬件 / 安全，以及「跑起来→文档站、懂原理→论文/博客、看实现→主仓库」的分流判据（详见 u4-l2 的 4.1 与 4.3）。

如果以上你都熟悉，本讲会非常顺——它几乎不引入新技术概念，价值在于**把边界画准、把出口标全**。

## 3. 本讲源码地图

本讲深度依赖两个文件，外加一条用来「自证边界」的命令。

| 文件 / 命令 | 作用 |
| --- | --- |
| `README.md` | 外衔接通道的**显式索引**：Documentation / Paper / Videos / Blog 四个区段，外加正文里的 Slack 与联系邮箱 |
| `blogs/Efficient LLM Deployment and Serving.md` | 博客**页脚**藏着比 README 更全的外部链接，是交叉验证、补全清单的第二来源 |
| `git ls-files` | 列出仓库全部已跟踪资产，用来**证明**「无运行时代码」这条边界 |

> 关键观察：README 与博客**不是**同一份链接表的两种排版。它们指向的外部入口有交集也有差异——最显著的差异是「文档站 URL」两者不一致，以及「主仓库链接」只出现在博客、不在 README。这条差异正是 4.2 与 4.3 的核心切入点。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应一条递进链路：

> **资料（本仓库）→ 用法（文档站）→ 原理（论文 / 博客）→ 代码（主仓库）**

- **4.1** 守住左端点：本仓库「只是资料」的边界。
- **4.2** 打开中间通道：文档站、论文、录像、社区。
- **4.3** 抵达右端点：主仓库 `sgl-project/sglang` 的衔接。

---

### 4.1 本仓库的边界：聚合资料、不含运行时代码

#### 4.1.1 概念说明

很多读者第一次打开本仓库，会下意识去找「`src/` 在哪」「`python -m sglang` 怎么跑」「入口文件是哪个」——**这些在本仓库都不存在**。本仓库的角色是**路标**，不是引擎本身：

- 引擎的**源代码**在主仓库 `sgl-project/sglang`；
- 引擎的**用法**在文档站；
- 本仓库只**收录讲解这些内容的幻灯片、博客、视频与论文链接**。

u1-l1 已经提出过这条边界。本模块要做的是**把它从「说法」升级为「可验证的事实」**——给你一条命令，任何人都能当场复核。

为什么要较这个真？因为混淆边界会直接导致学习卡壳：你若以为本仓库能跑代码，就会在 `slides/` 里徒劳地找安装命令；你若以为本仓库有论文全文，就会在 `blogs/` 里找不到 arXiv 链接而困惑。**边界清晰，出口才找得准。**

#### 4.1.2 核心流程：用 `git ls-files` 自证边界

证明一个仓库「没有某类文件」，比证明它「有」更难——你得把全部文件都看一遍。Git 提供了一条精确的命令：

```
1. git ls-files          列出仓库全部已跟踪文件（一行一个）；
2. 按扩展名归类          统计 .py/.ts/.rs/.go/.js 等源码扩展名出现次数；
3. 若源码扩展名计数为 0  → 边界成立：本仓库不含运行时代码。
```

这比「我翻了翻没看到代码」要可靠得多——`git ls-files` 覆盖**每一个已提交文件**，无遗漏。

#### 4.1.3 源码精读：本仓库到底装了什么

下面是当前 HEAD 下 `git ls-files` 的**真实产出**，按扩展名归类：

| 扩展名 | 数量 | 说明 |
| --- | --- | --- |
| `.pdf` | 25 | 幻灯片（`slides/` 主体） |
| `.png` | 10 | 博客配图（`blogs/docs/figs/`） |
| `.md` | 2 | `README.md` + 唯一长博客 `blogs/Efficient LLM Deployment and Serving.md` |
| `.pptx` | 1 | `slides/sglang_adoption_logo.pptx`（仓库唯一的 PowerPoint） |
| `.gitignore` | 1 | 仅忽略 `.DS_Store` |
| （无后缀）`LICENSE` | 1 | MIT 许可证，版权 sgl-project |

把这个表和「源码扩展名」对照：**`.py` / `.ts` / `.js` / `.rs` / `.go` / `.java` / `.c` / `.cpp` / `.sh` 的计数全部为 0**。这就是「无运行时代码」的硬证据。

进一步看仓库根目录，已跟踪的根级文件只有三个——正是 u1-l2 讲过的「三块基石」：

- [`README.md`](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md)（导航枢纽）
- [`LICENSE`](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/LICENSE)（MIT 许可）
- [`.gitignore`](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/.gitignore)（仅忽略 `.DS_Store`）

没有任何 `main.*`、`index.*`、`setup.py`、`package.json`、`Cargo.toml`、`go.mod` 之类的入口或构建文件。**本仓库连「构建」这个词都不适用——它没有东西可构建。**

#### 4.1.4 代码实践：亲手复核边界

1. **实践目标**：用一条命令证明本仓库不含源代码，把「边界」从讲义结论变成你自己验证过的事实。
2. **操作步骤**（在仓库根目录执行）：
   ```bash
   # 列出全部已跟踪文件并按扩展名计数
   git ls-files | sed 's/.*\.//' | sort | uniq -c | sort -rn

   # 直接搜常见源码扩展名（若为空即不存在）
   git ls-files '*.py' '*.ts' '*.js' '*.rs' '*.go' '*.java' '*.c' '*.cpp' '*.sh'
   ```
3. **需要观察的现象**：第一条命令输出一张「扩展名 → 数量」表（见 4.1.3）；第二条命令**没有任何输出**（即「空」）。
4. **预期结果**：扩展名表里只有 `pdf / png / md / pptx / gitignore / LICENSE`；源码搜索为空。两者共同坐实边界。
5. **若结果不一致**：说明 HEAD 已变化（例如仓库新接入了代码），请以你本地 `git ls-files` 的真实输出为准，并重新评估本仓库定位——这是**待本地验证**的活结论，不要迷信讲义里的固定数字。

#### 4.1.5 小练习与答案

- **练习 1**：为什么用 `git ls-files` 而不是 `ls` 来证明「没有源码」？
  - **答案**：`ls` 只看当前目录、且受隐藏文件与 `.gitignore` 影响；`git ls-files` 列出**全部已跟踪文件**（含子目录深处），覆盖完整、无遗漏，是证明「不存在某类文件」的正确工具。
- **练习 2**：本仓库有 25 个 PDF 却没有任何 `.py`，这说明什么？
  - **答案**：本仓库的内容载体是「讲解型资料」（幻灯片），不是「可执行实现」。引擎实现在主仓库，本仓库只负责把它们讲清楚、索引起来。
- **练习 3**：仓库里唯一的 `.pptx` 文件是什么？它的存在是否削弱「无运行时代码」的结论？
  - **答案**：是 `slides/sglang_adoption_logo.pptx`（采纳 logo 墙，详见 u1-l2）。它只是**演示文稿**，不是可执行代码，因此不削弱边界结论。

---

### 4.2 外部延伸：文档站 / 论文 / YouTube / Slack 与社区

#### 4.2.1 概念说明

边界之内是资料，边界之外是「真正能让你把 SGLang **跑起来、读懂、改对**」的地方。本模块把所有外部入口系统化。这些入口按用途分成五类：

1. **用法（Documentation）**——怎么安装、怎么调参数、怎么起服务。
2. **原理（Paper / Blog）**——设计依据、实验数据、版本发布详解。
3. **讲解（Videos）**——作者亲口讲，幻灯片的「配音版」。
4. **社区（Slack）**——提问、追进展、找贡献入口。
5. **组织（LMSYS / GitHub）**——上游项目与姊妹项目的代码仓库。

一个常被忽略的要点：**README 的外链并不全**。本仓库里还有第二个外部链接来源——博客 `blogs/Efficient LLM Deployment and Serving.md` 的**页脚**（第 102–111 行）。那里藏着 README 没有的入口（如主仓库 GitHub 链接）。所以做「延伸资源清单」时，**README 与博客页脚要交叉合并**，不能只看一个。

#### 4.2.2 核心流程：从两个来源合并出完整清单

```
1. 从 README 提取显式区段：Documentation / Paper / Videos / Blog；
2. 从 README 正文提取 Slack 与联系邮箱（第 3 行）；
3. 从博客页脚（第 102–111 行）补充 README 未列出的入口；
4. 把两份来源的入口按「用法/原理/讲解/社区/组织」五类归并；
5. 对每个入口标注「适合解决什么问题」。
```

第 4 步会发现两处**值得标记的差异**（详见 4.2.3）：
- 文档站 URL 在 README 与博客里**不一致**（两个不同域名）；
- 主仓库链接**只**出现在博客页脚，README 通篇没有。

#### 4.2.3 源码精读：两类来源的外部入口

**(A) README 的四个显式区段 + 正文 Slack**

- **Documentation（用法）**——注意 README 原文标题拼写为 `Documentaion`（漏了一个 `t`），指向文档站：[README.md:189-191](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L189-L191)。
  ```markdown
  ## Documentaion
  [SGLang Documentation](https://sgl-project.github.io/)
  ```
  > 文档站域名为 `https://sgl-project.github.io/`。请记住它——下面会和博客里的另一个域名撞车。

- **Paper（原理 / 学术）**——NeurIPS 24 论文，RadixAttention 的学术出处：[README.md:184-186](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L184-L186)。
  ```markdown
  ## Paper
  [NeurIPS 24] [SGLang: Efficient Execution of Structured Language Model Programs](https://arxiv.org/abs/2312.07104)
  ```

- **Videos（讲解）**——按事件分组的 YouTube 录像，频道入口在第 150 行：[README.md:148-150](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L148-L150)。
  ```markdown
  ## Videos
  Welcome to follow our YouTube [channel](https://www.youtube.com/@lmsys-org).
  ```

- **Blog（原理 / 深度）**——分 LMSYS Org / AMD / Meta PyTorch / Microsoft Azure 四个子区段：[README.md:112-146](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L112-L146)。其中最有分量的几篇深度博客：大规模 EP（[README.md:116](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L116)）、v0.4（[:118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118)）、v0.3（[:120](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L120)）、RadixAttention 原始博客（[:126](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L126)）。

- **Slack 与联系邮箱（正文，非区段）**——README 开篇第 3 行就给出社区入口与企业联系邮箱：[README.md:3](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L3)。
  > Slack 频道 `https://slack.sglang.ai`；企业合作邮箱 `sglang@lmsys.org`。

**(B) 博客页脚的补充入口（README 未列）**

博客 `Efficient LLM Deployment and Serving.md` 的结尾有一段「For more details」，列出了比 README 更全的外部链接：[blogs/Efficient LLM Deployment and Serving.md:102-111](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L102-L111)。

```
For more details about SGLang!
Visit Documentation: https://sglang.readthedocs.io/en/latest/#
Visit Github: https://github.com/sgl-project/sglang
Join Slack Channel: https://app.slack.com/client/T0652SSCVMG/C064NB2TAP9

For more details about LMSYS!
Visit Github: https://github.com/lm-sys
Visit Blogs: https://lmsys.org/blog/
HuggingFace: https://huggingface.co/lmsys
Visit YouTube Channel: @lmsys Official
```

这里有**三个 README 没有的发现**：

1. **文档站有第二个域名**：博客第 103 行写的是 `https://sglang.readthedocs.io/en/latest/`，与 README 的 `sgl-project.github.io` **不一致**。两者大概率是文档站在迁移前后的两个地址（readthedocs 为旧、github.io 为新），但本仓库无运行时代码无法佐证迁移时间——**待本地验证哪个为现行主站**，使用时以能打开、且内容最新者为准。
2. **主仓库 GitHub 链接只在这里**：第 104 行 `https://github.com/sgl-project/sglang` 是**本仓库里唯一一处**显式指向主仓库的地方，而且它在博客里，不在 README。这一点至关重要，是 4.3 的全部依据。
3. **Slack 的另一个入口**：第 105 行给了一个直连的 Slack 客户端链接（`app.slack.com/client/...`），与 README 顶部的 `slack.sglang.ai` 是同一社区的两个门——前者是邀请/落地页，后者是已加入后的直跳。

此外，博客还点出了上游组织 **LMSYS** 的入口（第 108–111 行）：LMSYS GitHub `https://github.com/lm-sys`、博客 `https://lmsys.org/blog/`、HuggingFace `https://huggingface.co/lmsys`、YouTube `@lmsys Official`。这些帮助你理解 SGLang 背后的组织生态（LMSYS 还维护 Vicuna、Chatbot Arena 等项目，见博客第 100 行）。

> **阅读提示**：把 README 与博客页脚当作「同一份外链清单的两个版本」。README 版更权威、更新（区段化、有日期）；博客版更全（多了主仓库与 LMSYS 入口），但成文于 2024-10，**个别链接可能过时**（如文档站域名）。合并取用时，以 README 为准、博客补缺，并对任何「两个域名」类差异保持警觉。

#### 4.2.4 代码实践：把 README 的全部外链域名挖出来

1. **实践目标**：用一条命令把 README 引用的所有外部域名提取出来，为「延伸资源清单」打底。
2. **操作步骤**：
   ```bash
   # 提取 README 中所有 https 链接的「域名」部分，去重排序
   grep -oE 'https?://[^/)]+' README.md | sort -u
   ```
3. **需要观察的现象**：输出一张域名清单，包含 `arxiv.org`、`lmsys.org`、`pytorch.org`、`rocm.blogs.amd.com`、`www.youtube.com`、`sgl-project.github.io`、`slack.sglang.ai`、`gamma.app`、`docs.google.com` 等。
4. **预期结果**：你会注意到清单里**没有任何 `github.com` 域名**——这从侧面印证了「README 不直接链接代码仓库」。要找主仓库，得去博客页脚（4.3）。
5. **延伸观察**：把同样的命令套到博客上 `grep -oE 'https?://[^/ )\\]+' "blogs/Efficient LLM Deployment and Serving.md" | sort -u`，你会看到 `github.com/sgl-project/sglang` 与 `github.com/lm-sys`——正好补上 README 的缺口。

#### 4.2.5 小练习与答案

- **练习 1**：README 的 `## Documentaion` 为什么只有一行？
  - **答案**：因为文档站是「用法」的**统一入口**，所有安装/参数/接口细节都在 `sgl-project.github.io`，本仓库只负责指路，不重复收录用法文档。
- **练习 2**：博客第 103 行的文档站域名与 README 不一致，你该怎么办？
  - **答案**：把它当作「文档站可能有两个地址（迁移前后）」的信号，**待本地验证**：两个都试，以能打开且内容更新者为主站；不要认定某一个一定是错的。
- **练习 3**：想看作者现场讲解「受限解码」，应走哪个外衔接通道？
  - **答案**：走 Videos 通道，在 `## Videos`（[README.md:148-182](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L148-L182)）按 2024-10-16 找第一次 meetup 录像（[:158](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L158)），配合 `slides/lmsys_1st_meetup_constrained_decoding.pdf` 一起看（u4-l2 原则 6：录像优先于幻灯片）。

---

### 4.3 主仓库 sgl-project/sglang 的衔接

#### 4.3.1 概念说明

递进链路的最右端是**代码**。SGLang 引擎真正的源代码、可配置开关、调度器实现，都在主仓库 [`sgl-project/sglang`](https://github.com/sgl-project/sglang)。本仓库与主仓库同属 `sgl-project` 组织，但二者分工截然不同：

| | 本仓库 `sgl-learning-materials` | 主仓库 `sgl-project/sglang` |
| --- | --- | --- |
| 角色 | 学习资料聚合 | 引擎实现 |
| 内容 | 幻灯片 / 博客 / 链接 | Python/CUDA 源码、文档、issue |
| 你来这为 | 读懂「为什么、怎么做」 | 跑代码、改参数、读实现、提 bug |

一个反直觉但极其重要的结论：**README 通篇没有出现主仓库的代码链接**。这不是疏漏，而是定位使然——本仓库刻意只做「资料层」，不直接把你导去代码。因此，**「跳主仓库」这一步需要你主动迈出**，而本讲就是给你指路牌。

#### 4.3.2 核心流程：什么时候必须跳主仓库

判据很简单——看你**下一步要什么**：

```
若需求是「读懂概念 / 看幻灯片」      → 留在本仓库；
若需求是「跑起来 / 查用法参数」      → 去文档站（4.2 Documentation）；
若需求是「看实验数据 / 设计依据」     → 去论文或 LMSYS 博客（4.2 Paper/Blog）；
若需求是「读实现 / 找可配置开关 / 提 bug」 → 跳主仓库 sgl-project/sglang（本模块）。
```

典型「必须跳主仓库」的场景：

- **找可配置开关**：例如 u3-l3 讲过「关闭跨租户 KV Cache 复用」以缓解侧信道——这个开关的开关名、默认值、配置位置都在主仓库的 server 参数里，本仓库没有。
- **看调度器实现**：u2-l2 讲的 CPU Overhead Hiding、FLPM，原理在幻灯片，**代码**在主仓库的 scheduler。
- **复现与提 bug**：任何「我跑出来不对」的问题，都要带着复现代码去主仓库提 issue，本仓库的 issue 区不适合技术排障。

#### 4.3.3 源码精读：「README 不含主仓库链接」的反向证据 + 唯一锚点

**反向证据**：在 README 里检索主仓库地址，**零命中**：

```bash
$ grep -n 'github.com/sgl-project/sglang' README.md
（无输出）
```

把 4.2.4 提取的 README 域名清单再扫一遍，里面**没有任何 `github.com`**。也就是说，README 把你导向博客、论文、文档站、录像，却**唯独不直接导向代码**。这正是「本仓库只聚合资料」最干净的反向佐证。

**唯一正向锚点**：主仓库链接**只**出现在博客页脚第 104 行——[blogs/Efficient LLM Deployment and Serving.md:104](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L104)：

```
Visit Github: https://github.com/sgl-project/sglang
```

所以，如果你**只在 README 里找**主仓库，会找不到；必须翻到博客页脚，或直接记住这个地址。这条「藏在博客、不在 README」的细节，是本讲最有操作价值的一个结论。

> **衔接链路全景**（把 u4-l2 的雏形补全为带 URL 的完整版）：
>
> | 层级 | 角色 | 入口 |
> | --- | --- | --- |
> | 资料 | 讲解型内容 | 本仓库 README + `slides/` + `blogs/` |
> | 用法 | 安装 / 参数 / 接口 | 文档站 `https://sgl-project.github.io/`（README [:191](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L191)）；博客另记 `https://sglang.readthedocs.io/`（[:103](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L103)，待本地验证） |
> | 原理 | 设计 / 实验 | NeurIPS 论文 `https://arxiv.org/abs/2312.07104`（README [:186](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L186)）+ LMSYS/AMD 博客（README [:112-146](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L112-L146)） |
> | 代码 | 实现 / 开关 | 主仓库 `https://github.com/sgl-project/sglang`（博客 [:104](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L104)） |

#### 4.3.4 代码实践：亲手找到「跳主仓库」的唯一锚点

1. **实践目标**：验证「主仓库链接不在 README、只在博客」这条结论，并定位它的精确行号。
2. **操作步骤**：
   ```bash
   # 1) 确认 README 不含主仓库链接（预期：无输出）
   grep -n 'github.com/sgl-project/sglang' README.md

   # 2) 在博客里定位主仓库链接（预期：命中第 104 行）
   grep -n 'github.com/sgl-project/sglang' "blogs/Efficient LLM Deployment and Serving.md"

   # 3) 顺带把仓库内所有指向 sgl-project/sglang 的位置都找出来
   grep -rn 'sgl-project/sglang' README.md blogs/
   ```
3. **需要观察的现象**：第 1 步无输出；第 2 步命中第 104 行；第 3 步只显示博客那一处（README 无命中）。
4. **预期结果**：你亲眼确认了「主仓库链接是本仓库里的稀缺资源——只有博客页脚一处」。今后需要跳主仓库时，直接用 `https://github.com/sgl-project/sglang`，不必再翻 README。
5. **若结果不一致**：若 README 也命中了，说明仓库已在更新版本里补上了主仓库链接（好事），请以你本地最新输出为准更新本结论。

#### 4.3.5 小练习与答案

- **练习 1**：u3-l3 提到「关闭跨租户 KV Cache 复用」可缓解侧信道，这个开关在本仓库能找到吗？
  - **答案**：不能。本仓库无运行时代码（4.1 已证），可配置开关在主仓库 `sgl-project/sglang` 的 server 参数里。这正是「必须跳主仓库」的典型场景。
- **练习 2**：为什么 README 不直接放主仓库链接？
  - **答案**：定位使然。本仓库是「资料层」，刻意只做讲解与索引，把「用法」导给文档站、「代码」留给主仓库；主仓库链接仅作为博客页脚的补充信息出现，避免资料库与代码库职责混淆。
- **练习 3**：把「资料→用法→原理→代码」四层与「本仓库→文档站→论文/博客→主仓库」对应起来。
  - **答案**：资料=本仓库；用法=文档站（`sgl-project.github.io`）；原理=NeurIPS 论文 + LMSYS/AMD 博客；代码=主仓库 `sgl-project/sglang`。四层依次递进，每层解决不同问题。

---

## 5. 综合实践

本讲的交付物，正是规格里要求的**「延伸资源清单」**：把 README 末尾的 Documentation、Paper、Videos、Slack 等入口**分类**，并写明**每个入口适合解决什么问题**。下面给出一份基于真实源码整理的示例清单，你可以照它改造为自己的版本。

### 实践目标

产出一张「入口分类表」，要求：

1. 覆盖**用法 / 原理 / 讲解 / 社区 / 组织 / 代码**六类；
2. 每类给出**具体 URL**与本仓库内的**出处行号**（README 或博客）；
3. 每个入口写一句「**适合解决什么问题**」；
4. 标注任何「两个来源不一致」的入口（如文档站双域名）。

### 示例：延伸资源清单

| 分类 | 入口 | URL | 出处 | 适合解决什么问题 |
| --- | --- | --- | --- | --- |
| **用法** | SGLang 文档站 | `https://sgl-project.github.io/` | [README.md:191](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L191) | 安装、起服务、查参数与接口用法 |
| 用法（备用域名） | 文档站（readthedocs） | `https://sglang.readthedocs.io/en/latest/` | [博客:103](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L103) | 同上；与上一行**域名不一致**，待本地验证哪个为现行主站 |
| **原理** | NeurIPS 24 论文 | `https://arxiv.org/abs/2312.07104` | [README.md:186](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L186) | 看设计依据、RadixAttention 学术出处、实验对照 |
| 原理 | LMSYS 深度博客 | `https://lmsys.org/blog/` | [README.md:114](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L114) | 版本发布详解（v0.2–v0.4、大规模 EP）、提速数字 |
| 原理 | AMD ROCm 博客 | `https://rocm.blogs.amd.com/...` | [README.md:128-136](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L128-L136) | AMD MI300X 上的 DeepSeek 加速、量化细节 |
| **讲解** | YouTube 频道 | `https://www.youtube.com/@lmsys-org` | [README.md:150](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L150) | 听作者亲口讲，幻灯片的「配音版」；按日期配 PDF |
| **社区** | Slack 频道 | `https://slack.sglang.ai` | [README.md:3](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L3) | 提问、追进展、找贡献入口 |
| 社区（直跳） | Slack 客户端直链 | `https://app.slack.com/client/T0652SSCVMG/C064NB2TAP9` | [博客:105](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L105) | 已加入后直跳频道（与上一行同社区，两个门） |
| 社区 | 企业合作邮箱 | `sglang@lmsys.org` | [README.md:3](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L3) | 企业级采纳 / 部署咨询 / 赞助 / 合作 |
| **组织** | LMSYS GitHub | `https://github.com/lm-sys` | [博客:108](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L108) | 了解上游组织（Vicuna、Chatbot Arena 等姊妹项目） |
| 组织 | LMSYS HuggingFace | `https://huggingface.co/lmsys` | [博客:110](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L110) | 找模型权重、数据集 |
| **代码** | 主仓库 | `https://github.com/sgl-project/sglang` | [博客:104](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/blogs/Efficient%20LLM%20Deployment%20and%20Serving.md#L104) | 读实现、找可配置开关、提 bug（**README 无此链接**） |

### 操作步骤（你也可以照此定制自己的清单）

1. 运行 4.2.4 与 4.3.4 的 `grep` 命令，把 README 与博客的全部外链抓出来。
2. 按本表的六类（用法 / 原理 / 讲解 / 社区 / 组织 / 代码）归并。
3. 对每个入口补一句「适合解决什么问题」。
4. 单独标出「两个来源不一致」或「仅博客有」的入口（文档站双域名、主仓库链接、Slack 双入口）。
5. 把成品存进个人笔记——**不要**写进本讲义目录 `sgl-learning-materials-tutorial/`（那是讲义区，详见 u4-l1、u4-l2）。

### 需要观察的现象 / 预期结果

- 你的清单应同时覆盖 README 与博客两个来源，且能指出「主仓库链接只在博客」这一关键缺口。
- 文档站、Slack 两处应标注「双入口」，并注明待本地验证。
- 整张表应当能回答「我想做 X，该去哪个入口」——这是本讲全部价值的落点。

## 6. 本讲小结

- **本仓库的边界是「聚合资料、不含运行时代码」**：`git ls-files` 显示全部资产为 25 PDF + 10 PNG + 2 MD + 1 PPTX + LICENSE + .gitignore，**零**源码文件——这条边界可被任何人当场复核。
- **外部入口要合并 README 与博客页脚两个来源**：README 给出 Documentation / Paper / Videos / Blog 四个显式区段 + 正文 Slack；博客页脚（第 102–111 行）补上了 README 缺的主仓库、LMSYS、HuggingFace 等入口。
- **文档站存在双域名**：README 用 `sgl-project.github.io`，博客用 `sglang.readthedocs.io`，二者不一致，**待本地验证**现行主站。
- **主仓库 `sgl-project/sglang` 是递进链路的终点**：README 通篇**不含** `github.com` 链接，主仓库地址**只**出现在博客第 104 行——找代码必须主动跳这一步。
- **「资料 → 用法 → 原理 → 代码」四层递进**：资料=本仓库、用法=文档站、原理=论文/博客、代码=主仓库；每层解决不同问题，入口不可混用。
- **最终交付物是「延伸资源清单」**：六类入口 + URL + 出处行号 + 「适合解决什么问题」，外加对双入口/缺口的标注。

## 7. 下一步学习建议

- **从本仓库毕业**：你已读完全部 16 讲，本仓库的资料地图、边界与出口都已摸清。下一步是**真正跳出本仓库**——带着你的「延伸资源清单」和 u4-l2 的 4 周计划，去文档站把 SGLang 跑起来，去主仓库读一段调度器代码。
- **优先衔接顺序**：先文档站（用法，把环境跑通）→ 再 NeurIPS 论文（原理，对照 u2/u3 已学）→ 最后主仓库（代码，挑一个你最有兴趣的机制读实现，如 RadixAttention 或 scheduler）。
- **回看巩固**：若在主仓库读到某段代码看不懂，回到对应主题讲义——调度（u2-l2）、受限解码（u2-l3）、MLA（u2-l4）、EP/PD（u2-l5）、Router（u2-l6）、量化/硬件（u3-l2）、安全（u3-l3）——把「原理」当「代码」的注解。
- **反哺社区**：当你在主仓库或文档站发现值得收录的新资料，按 **u4-l1** 的格式规范登记进本仓库 README 并提 PR，把「资料层」维护得更好——这也让本仓库的边界与出口对下一位学习者更清晰。
- **手册到此完结**：本讲是收官篇，没有后续讲义。你的学习路线从此由「读手册」转入「读代码与论文」，本仓库将作为你随时回查的**资料索引**而长期有用。
