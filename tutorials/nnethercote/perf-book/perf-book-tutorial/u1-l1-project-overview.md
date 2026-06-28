# perf-book 是什么——项目定位与这本书的价值

## 1. 本讲目标

学完本讲，你应该能够：

- 用一句话说清楚 perf-book 是什么、它「源码即 Markdown」的特殊之处在哪里。
- 复述这本书面向的目标读者，以及它为什么不适合 Rust 初学者。
- 列出本书关心的四个性能维度（运行速度、内存使用、二进制体积、编译时间）。
- 解释作者「重广度轻深度（breadth over depth）」的写作取向，并说出本书明确不覆盖的内容。
- 从 `README.md` 与 `src/introduction.md` 中独立提取上述信息，而不是依赖记忆。

这是整套学习手册的第一篇。在进入后续「如何构建运行」「如何做基准测试」「如何优化代码」之前，我们必须先把这本书读「对」——理解它的定位、范围与风格，后面的每一篇讲义才有参照系。

## 2. 前置知识

本讲几乎不需要你懂 Rust 细节，但下面几个概念能帮你更快进入状态：

- **什么是「性能（performance）」**：在编程语境下，「性能」是一个统称，至少包含运行速度（程序跑得多快）、内存使用（程序占用多少内存）、二进制体积（编译产物有多大）、编译时间（构建要等多久）这四个方面。它们之间经常相互制约，比如「跑得更快」可能让「二进制更大」。
- **「书」也是一种项目**：大多数软件项目是一堆会被编译/运行的代码；而 perf-book 是一本**用工具渲染成网页的技术书**，它的「源码」就是一篇篇 Markdown 文档，外加少量构建配置。本手册里我们把 Markdown 文档也称作「源码」，因为它们正是被分析的对象。
- **mdBook**：一个用 Rust 写的、把 Markdown 文档渲染成在线书籍的命令行工具。本讲你只需要知道「perf-book 是用 mdBook 写的」即可，具体安装与使用在下一讲（u1-l2）展开。
- **GitHub 仓库**：perf-book 托管在 GitHub 上的 `nnethercote/perf-book` 仓库，本书可以通过网页阅读，也可以 clone 到本地自己构建。
- **issue 与 PR（pull request）**：在 GitHub 上，issue 是「提出问题/建议」的方式，PR 是「直接提交代码改动」的方式。本讲会提到作者对这两种贡献方式的偏好。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md) | 仓库入口说明：如何查看、构建、预览、测试本书，以及贡献与许可证声明。 |
| [src/introduction.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/introduction.md) | 书的「导论」章节，集中说明本书的范围、风格与目标读者。 |
| [src/title-page.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/title-page.md) | 书的标题页：书名、首次发布时间、作者。 |
| [src/SUMMARY.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/SUMMARY.md) | mdBook 的目录文件，列出全部章节（本讲用于佐证「覆盖面广」）。 |
| [book.toml](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml) | mdBook 配置文件，定义书名、作者、源码目录等元信息。 |

> 说明：本讲引用的所有链接都指向当前 HEAD（提交 `a05dd0f`），点击即可在 GitHub 上看到带行号高亮的真实源码。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- 4.1 perf-book 的定位与读者对象
- 4.2 Rust 性能优化的范围与本书风格
- 4.3 README 与 introduction 提供的线索

### 4.1 perf-book 的定位与读者对象

#### 4.1.1 概念说明

perf-book 的正式书名是 **The Rust Performance Book（《Rust 性能手册》）**。它是一本**关于 Rust 程序性能**的在线技术书，由 Nicholas Nethercote 主笔，2020 年 11 月首次发布。

理解 perf-book 时有两个关键认知：

1. **它是一本书，不是一个可运行库**。它的「源码」是 Markdown 文档（`src/` 下的各章）加上构建配置（`book.toml`）。所以「阅读源码」在这里就是「阅读书稿」。
2. **它有明确的读者画像**：面向**中高级（intermediate and advanced）**Rust 用户。作者明确指出本书对初学者是「无益的干扰」。

 Nicholas Nethercote 本人长期从事编译器开发（他是 Rust 编译器性能领域的知名贡献者），所以本书的内容也会带有「偏编译器开发、远离科学计算」的取向——这点会在 4.2 再展开。

#### 4.1.2 核心流程

理解一本书的「定位」可以按这个顺序追问：

```text
这本书叫什么？ → 谁写的、什么时候发布的？ → 写给谁看？ → 不写给谁看？
```

对 perf-book 而言，这条链的答案都集中在两个小文件里：

1. **书名 + 发布时间 + 作者** → `src/title-page.md`
2. **目标读者** → `src/introduction.md` 末尾

#### 4.1.3 源码精读

标题页用一行 HTML `<span>` 控制字号，给出书名、首发时间与作者：

[title-page.md:1-5](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/title-page.md#L1-L5) —— 这里写着书名 *The Rust Performance Book*、首次发布于 **2020 年 11 月**、作者为 **Nicholas Nethercote 及其他人**。

仓库根的 `README.md` 第一行也直接点明它是「The Rust Performance Book」：

[README.md:1-3](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md#L1-L3) —— 仓库名 `perf-book`，副标题 *The Rust Performance Book*。

而目标读者这一关键定位，写在导论最后一段，措辞非常直接：

[introduction.md:32-34](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/introduction.md#L32-L34) —— 本书面向**中高级 Rust 用户**；初学者要学的东西已经够多，这些技巧对他们更可能是无益的干扰。

这条信息很重要：它告诉我们**不要**把本书当成 Rust 入门读物，也提示后续讲义里出现的「优化技巧」默认读者已经具备相当的 Rust 经验。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（本书无独立可运行代码，因此采用阅读式实践）。

1. **实践目标**：从源文件中准确定位书的「身份三要素」与「目标读者」原文。
2. **操作步骤**：
   - 打开 `src/title-page.md`，记录书名、首发月份、作者。
   - 打开 `src/introduction.md`，找到写明目标读者的那一段（提示：靠近文末）。
3. **需要观察的现象**：你会看到作者用「intermediate and advanced Rust users」明确圈定读者，并用「unhelpful distraction」解释为何不推荐初学者。
4. **预期结果**：书名 = The Rust Performance Book；首发 = 2020 年 11 月；作者 = Nicholas Nethercote 及其他人；目标读者 = 中高级 Rust 用户。
5. 若你在本地看不到这些文件，可点击上方永久链接在 GitHub 网页查看。

#### 4.1.5 小练习与答案

**练习 1**：这本书首次发布于哪一年、哪一月？主笔是谁？
**答案**：2020 年 11 月；Nicholas Nethercote（及其他人）。

**练习 2**：本书为什么不推荐给 Rust 初学者？用作者的话概括。
**答案**：初学者要学的东西已经够多，这些性能技巧对他们更可能是「无益的干扰（an unhelpful distraction）」。

### 4.2 Rust 性能优化的范围与本书风格

#### 4.2.1 概念说明

知道了「写给谁」，接下来要问「写什么、怎么写」。perf-book 在 `src/introduction.md` 里把这两点说得很清楚：

- **范围（scope）**：本书关心 Rust 程序的四个性能维度——**运行速度（runtime speed）、内存使用（memory usage）、二进制体积（binary size）、编译时间（compile times）**。其中前三个是「运行期」特性，编译时间是「构建期」特性（单独放在 Compile Times 章节）。
- **手段（means）**：有的技巧只改构建配置（build configuration）就能见效，但**更多技巧需要改代码**。
- **风格（style）**：作者**故意写得简洁（deliberately terse），宁可广度优先于深度（favouring breadth over depth）**，目的是让人**快速读完**；需要深度时给出外部链接。

这一点直接决定了我们这套学习手册的读法：本书像一份「性能优化速查地图」，而不是某一项优化的万字深扒。

#### 4.2.2 核心流程

可以用一张「范围—手段—风格」三栏表来组织理解：

| 维度 | perf-book 的立场 |
| --- | --- |
| 关心什么 | 运行速度、内存、二进制体积、编译时间 |
| 怎么做 | 部分靠改构建配置，多数靠改代码 |
| 怎么写 | 简洁、广度优先、快读、需要深度给外链 |

此外，作者还划出了**本书不覆盖的边界**：

- 本书「**不是**一本通用 profiling/优化指南的替代品」——它聚焦 **Rust** 的性能。
- 内容偏向**编译器开发**领域，**远离科学计算**。
- 例子来自**真实世界的 Rust 程序**（很多附有真实 PR 链接），强调「实用且经过验证」。

#### 4.2.3 源码精读

本书的范围在导论开头就讲清了：

[introduction.md:5-9](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/introduction.md#L5-L9) —— 列出运行速度、内存使用、二进制体积三个运行期维度，并说明 Compile Times 章节专门讲编译时间；同时点明「有的只改构建配置，多数要改代码」。

[introduction.md:13-18](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/introduction.md#L13-L18) —— 指出有些技巧是 Rust 特有的，有些可迁移到其他语言；并明确「本书主要讲 Rust 程序的性能，不能替代一本通用的 profiling 与优化指南」。

[introduction.md:22-26](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/introduction.md#L22-L26) —— 强调技巧「实用且经过验证」，常附真实 PR 链接；并坦承作者背景偏向编译器开发、远离科学计算。

本书风格的关键一句：

[introduction.md:28-30](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/introduction.md#L28-L30) —— 本书**故意简洁，重广度轻深度**，便于快速阅读；需要更多深度时给出外部链接。

「广度优先」也可以从目录文件直接印证——`SUMMARY.md` 列出了从 Benchmarking、Profiling 到 Heap Allocations、Iterators、Machine Code 等十几个主题，每个都只用一篇短文覆盖：

[src/SUMMARY.md:3-23](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/SUMMARY.md#L3-L23) —— mdBook 的目录，罗列了全部章节，可见本书覆盖的主题之多、每章之短。

#### 4.2.4 代码实践

1. **实践目标**：用结构化方式归纳本书「覆盖什么 / 不覆盖什么 / 怎么写」。
2. **操作步骤**：
   - 打开 `src/introduction.md`，把第 5–9 行提到的性能维度抄成一个列表。
   - 找到「favouring breadth over depth」与「no substitute for a general purpose guide」两句原文，记下它们的行号。
   - 打开 `src/SUMMARY.md`，数一下本书共有多少个正文章节（不含标题页）。
3. **需要观察的现象**：你会确认范围是「运行速度 / 内存 / 二进制体积 / 编译时间」四项；风格句在第 28 行附近；本书**不**是一本通用 profiling 指南。
4. **预期结果**：四个维度齐全；正文章节约 18 个（以 SUMMARY.md 实际列出的 `-` 条目为准）；广度优先的取向与目录的「多而短」相互印证。
5. 若不确定章节计数，以你实际数到的 `SUMMARY.md` 中 `- [..]` 条目数为准。

#### 4.2.5 小练习与答案

**练习 1**：列出本书关心的全部性能维度。
**答案**：运行速度（runtime speed）、内存使用（memory usage）、二进制体积（binary size）、编译时间（compile times）。

**练习 2**：「favouring breadth over depth」在书中具体指什么？
**答案**：作者故意把书写得简洁，覆盖面广但每个点不深挖，方便快速通读；需要更深入的内容时，靠外部链接补充。

**练习 3**：作者明确说本书不能替代什么？
**答案**：不能替代一本**通用的 profiling 与优化指南**（a general purpose guide to profiling and optimization），因为它聚焦的是 Rust 程序的性能。

### 4.3 README 与 introduction 提供的线索

#### 4.3.1 概念说明

`README.md` 是仓库的「门面」，回答「我怎么用这个仓库」；`src/introduction.md` 是书的「门面」，回答「这本书讲什么」。两者合起来，几乎包含了你判断「这本书适不适合我」所需的全部信息。

从 `README.md` 里，我们能提取四类线索：

1. **在哪看**：本书有在线 HTML 渲染版。
2. **怎么构建/预览/测试**：用 mdBook 的三条命令。
3. **怎么贡献**：作者偏好 issue 而非 PR，且**不接受生成式 AI 产出的内容**。
4. **许可证**：Apache-2.0 或 MIT，二选一。

`src/introduction.md` 则补充了语义层面的线索（范围、风格、读者），我们在 4.1、4.2 已讲过。

#### 4.3.2 核心流程

提取「门面信息」的标准流程：

```text
读 README → 提取「查看/构建/预览/测试」命令 → 提取「贡献与许可」声明
读 introduction → 提取「范围/风格/读者」陈述
两类信息合并 → 得到对本书的完整第一印象
```

伪代码示意（仅描述思路，非项目原有代码，属于示例代码）：

```text
facts = {}
facts += extract(README, sections=["Viewing","Building","Development","Improvements","License"])
facts += extract(introduction, concerns=["scope","style","audience"])
return facts
```

#### 4.3.3 源码精读

查看与构建方式写在 README 的前两节：

[README.md:5-7](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md#L5-L7) —— 在线 HTML 渲染版的地址。

[README.md:21-32](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md#L21-L32) —— 用 `cargo install mdbook` 安装 mdBook，用 `mdbook build` 构建，产物放入 `book/` 目录。

预览与测试写在 Development 一节：

[README.md:34-48](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md#L34-L48) —— `mdbook serve` 启动本地服务器（`localhost:3000` 预览，文件改动自动刷新）；`mdbook test` 测试书中嵌入的代码。

最能体现作者对本书「措辞态度」的两处声明：

[README.md:50-58](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md#L50-L58) —— 改进建议欢迎，但**更希望以 issue 而非 PR 形式提交**，因为作者对书中措辞非常挑剔，收到 PR 通常也会用自己的话重写；并声明**本书不含任何生成式 AI 产出的内容，且不会接受此类内容**。

许可证（双授权）：

[README.md:60-68](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md#L60-L68) —— Apache-2.0 或 MIT，任选其一。

最后，`book.toml` 印证了书的元信息（与标题页一致）：

[book.toml:1-5](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L1-L5) —— 书名 `The Rust Performance Book`、作者 `Nicholas Nethercote`、源码目录 `src`、语言 `en`。

#### 4.3.4 代码实践

1. **实践目标**：把 README 里的「操作类信息」整理成一张可直接照做的命令表，并定位两条「态度声明」。
2. **操作步骤**：
   - 打开 `README.md`，按下表逐行填入命令与用途。
   - 在 `README.md` 中找到「偏好 issue 而非 PR」与「拒绝生成式 AI」两句话，标注行号区间。
3. **需要观察的现象**：你会看到构建/预览/测试三条命令分别对应 `mdbook build` / `mdbook serve` / `mdbook test`；两条态度声明都集中在 Improvements 一节。
4. **预期结果**：命令表大致如下——

   | 用途 | 命令 |
   | --- | --- |
   | 安装 mdBook | `cargo install mdbook` |
   | 构建本书 | `mdbook build`（产物在 `book/`） |
   | 本地预览 | `mdbook serve`（浏览器开 `localhost:3000`） |
   | 测试书中代码 | `mdbook test` |

   两条态度声明位于 `README.md` 第 50–58 行区间。
5. 这些命令本讲**不需要**你真的执行（下一讲 u1-l2 会带你实操），现在只需会从源文件里把它们找出来。

#### 4.3.5 小练习与答案

**练习 1**：哪条命令能在本地启动 Web 服务器预览本书？默认访问地址是什么？
**答案**：`mdbook serve`；默认在 `localhost:3000` 打开。

**练习 2**：作者为什么更希望改进建议以 issue 而非 PR 形式提交？
**答案**：他对书中的措辞非常挑剔（very particular about the wording），即便收到 PR，通常也会把其中的想法用自己的话重写一遍。

**练习 3**：README 对「生成式 AI 生成的内容」持什么态度？
**答案**：本书不含任何生成式 AI 产出的内容，并且**不会接受**此类内容（none will be accepted）。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿任务：

> **任务**：阅读 [README.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md) 与 [src/introduction.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/introduction.md)，用**一段话**（100–200 字）总结本书，要求同时覆盖：
>
> 1. 这本书是什么、由谁主笔、何时首发；
> 2. **目标读者**是谁；
> 3. 覆盖的**四个性能维度**；
> 4. 「**重广度轻深度**」的写作取向；
> 5. 本书**明确不覆盖**的内容（至少一点）。

参考答案（可作为自我对照，不必逐字相同）：

> *The Rust Performance Book* 由 Nicholas Nethercote 主笔、2020 年 11 月首发，是一本用 mdBook 写成的在线技术书，面向中高级 Rust 用户。它关心 Rust 程序的运行速度、内存使用、二进制体积与编译时间四个维度，部分技巧靠改构建配置、多数靠改代码。作者故意把书写得简洁、重广度轻深度，便于快速通读，需要深度时给外部链接。它不是一本通用的 profiling/优化指南，且内容偏向编译器开发、远离科学计算。

完成后再做一个小自检：把你的总结里每一条结论，都回头找到它在源文件中的**具体行号**。如果每条都能对上，说明你真的掌握了从源码提取信息，而不是凭印象复述。

## 6. 本讲小结

- perf-book 的正式书名是 *The Rust Performance Book*，由 Nicholas Nethercote 主笔，2020 年 11 月首发，**用 mdBook 渲染、源码即 Markdown**。
- 本书面向**中高级 Rust 用户**，对初学者是无益的干扰。
- 覆盖四个性能维度：**运行速度、内存使用、二进制体积、编译时间**；手段上「部分改配置、多数改代码」。
- 风格上**故意简洁、重广度轻深度**，便于快读，深度靠外链补充。
- 本书**不是**通用 profiling/优化指南，内容偏向编译器开发、远离科学计算。
- `README.md` 提供查看/构建/预览/测试的命令与「issue 优先、拒收生成式 AI」的态度；`src/introduction.md` 提供范围、风格与读者定位。

## 7. 下一步学习建议

理解了「这本书是什么」之后，建议按以下顺序继续：

- **下一讲 u1-l2《用 mdBook 构建与运行 perf-book》**：动手安装 mdBook，跑通 `mdbook build` / `serve` / `test` 三条命令，把书真正在本机跑起来。
- **u1-l3《仓库结构与章节的组织方式》**：搞清 `SUMMARY.md` 如何决定目录与章节顺序，学会定位任意主题所在的文件。
- 进入第二单元前，建议先回到 [src/introduction.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/introduction.md) 重读一遍——它是后续所有「测量→优化」讲义的总纲。

> 提示：本书强调「先测量、再优化」。所以第二单元会从 Benchmarking、Profiling、Build Configuration、Linting 切入，**这些是后面所有代码级优化的前提**，不要跳过。
