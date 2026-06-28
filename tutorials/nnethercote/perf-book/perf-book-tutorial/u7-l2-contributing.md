# 为 perf-book 贡献内容——风格与协作

## 1. 本讲目标

本讲是全套手册的收官篇，把前 6 个单元学到的「读懂」能力升级为「贡献」能力。学完后你应该能够：

- 说清楚 perf-book 为什么「偏好 issue 而非 PR」，以及作者对生成式 AI 内容的态度，从而选对协作入口。
- 复述 `CONTRIBUTING.md` 里的四条写作规范（行长、示例、标题大小写、外链风格），并能照此风格改写一段文字。
- 独立完成「为本书新增一章」的全部步骤：新建 `.md`、改 `SUMMARY.md`、用 `mdbook build/test` 验证，并理解 `create-missing=false` 的防呆作用。
- 区分「内容贡献」与「mdBook 扩展/自定义」，知道哪些改动要碰 `book.toml`、哪些完全不用。

## 2. 前置知识

本讲承接两篇讲义，关键认知不再重复：

- **u1-l3 仓库结构与章节组织**：本讲反复用到「`SUMMARY.md` 是目录单一事实来源」「文件名 kebab-case」「`create-missing=false`」这套认知。
- **u7-l1 mdBook 进阶**：本讲的「mdBook 扩展与自定义」模块建立在 u7-l1 讲过的 preprocessor / renderer / theme 管线、`mdbook build/test` 闸门、以及 deploy 条件门控之上，只从贡献者视角收口。

如果你跳过了这两篇，至少需要知道：

- 这本书的「源码」就是 `src/` 下的 Markdown，外加 `book.toml` 与 `.github/workflows/ci.yml`。
- mdBook **不会**自动扫描 `src/`，章节顺序完全由 `src/SUMMARY.md` 决定。
- `book/` 是 `mdbook build` 的产物，被 `.gitignore` 忽略，不属于源码。

需要熟悉的术语：issue / PR（Pull Request）、mdBook、`SUMMARY.md`、`create-missing`、kebab-case、reference link（引用式链接）、inline link（行内链接）、Title Case。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `README.md` | 给出贡献态度：偏好 issue、拒收生成式 AI 内容 |
| `CONTRIBUTING.md` | 本书的风格指南（Style Guide）：行长、示例、标题、外链四条规范 |
| `src/SUMMARY.md` | 目录单一事实来源，新增章节必须在此登记 |
| `book.toml` | mdBook 总配置；`create-missing`、`edit-url-template`、输出后端都在这里 |
| `.editorconfig` | 编辑器层面的行长规范（79 字符） |
| `src/general-tips.md` | terse 风格的最佳范本：短句 + Example 链接 |
| `src/introduction.md` | 作者亲口说明本书的读者定位与 terse 取向 |
| `.github/workflows/ci.yml` | CI 如何把关与部署，决定你的改动何时上线 |

## 4. 核心概念与源码讲解

### 4.1 贡献流程：issue 优先

#### 4.1.1 概念说明

很多开源项目鼓励直接提 PR——「Talk is cheap, show me the code」。perf-book 反其道而行：作者**明确偏好 issue 而非 PR**。这不是不欢迎贡献，而是因为本书的**措辞（wording）本身就是内容的核心资产**。

理解这一点对贡献者至关重要：如果你花一晚写好一章直接开 PR，很可能被作者「取其思想、弃其文字」后用他自己的话重写。所以最经济的协作姿势是——**先用 issue 提出想法与依据，让作者认可方向，再（可选地）动手**。

#### 4.1.2 核心流程

推荐的贡献路径：

1. **想清楚要补什么**：修正错误、补充一个性能技巧，还是新增一章？
2. **开 issue**：描述问题/想法，最好附上「为什么这是性能坑」「是否有真实 PR 可佐证」。
3. **等作者反馈**：作者可能采纳、改写或拒绝。
4. **若仍想提 PR**：明白作者会以自己的措辞重写，把 PR 当作「带证据的建议」而非「定稿」。

此外，本书有一条硬性红线：**不接受任何生成式 AI 产生的内容**。

#### 4.1.3 源码精读

`README.md` 的 `## Improvements` 一节是这条流程的权威出处：

[README.md:50-58](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md#L50-L58) — 作者声明欢迎改进建议，但「prefer them to be filed as issues rather than pull requests」，理由是他对书里的措辞极其讲究；并在结尾声明本书不含、也不会接受任何生成式 AI 内容。

把关键两句拆开看：

[README.md:52-55](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md#L52-L55) — 偏好 issue 的理由：措辞是核心资产，PR 的文字会被「take the underlying idea ... and rewrite it into my own words」。

[README.md:57-58](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md#L57-L58) — 生成式 AI 红线：`This book contains no material produced by generative AI, and none will be accepted.`

`README.md` 末尾还有一段标准贡献条款（DCO 式双许可证默认）：

[README.md:70-74](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md#L70-L74) — 你提交的内容默认按 Apache-2.0 / MIT 双许可证纳入，除非另行声明；这也呼应了 README `## License` 段的双许可证声明。

#### 4.1.4 代码实践

源码阅读型实践，目标是把「issue 优先」内化为肌肉记忆。

1. 实践目标：用作者自己的话复述贡献流程，并能向同事解释「为什么不建议直接开 PR」。
2. 操作步骤：打开 `README.md` 的 `## Improvements`，逐句翻译；再到 GitHub 仓库 `nnethercote/perf-book` 的 Issues 列表，找 1–2 个已被采纳的 issue，对比 issue 文字与最终落书的文字是否一致（验证「重写」现象）。
3. 需要观察的现象：被采纳的 issue，其核心想法进了书，但措辞往往与 issue 原文不同。
4. 预期结果：能写出一句不超过 30 字的总结，例如「提 issue 给想法，作者用自己的话重写，不接受 AI 内容」。
5. 待本地验证：GitHub Issues 的具体内容需联网查看，本地仓库不含历史 issue。

#### 4.1.5 小练习与答案

**练习 1**：某贡献者写了 200 行新章节直接开 PR。按本书流程，更合理的做法是什么？

> **答案**：先开 issue 说明动机与依据（最好附真实 PR 佐证），等作者认可方向；即便之后提 PR，也应预期文字会被重写。直接提长 PR 既可能白费排版精力，也与作者「issue 优先」的偏好相左。

**练习 2**：作者为什么会「rewrite a pull request into my own words」？这反映了本书哪一特性？

> **答案**：因为本书的措辞（wording）是核心内容资产，作者对用词非常讲究（very particular about the wording）。这反映了本书「terse、重广度」的风格——每个字都为「快读」服务，不能有冗余，故作者要亲自把控文字。

---

### 4.2 本书简洁（terse）的写作风格

#### 4.2.1 概念说明

「terse」意为「言简意赅」。`introduction.md` 明说本书「deliberately terse, favouring breadth over depth」。这是贯穿全书、也贯穿 `CONTRIBUTING.md` 的写作纪律：**宁可短、宁可外链给深度，也要让书能被快速读完**。

对贡献者来说，terse 不是「写得少」，而是「每句都要有用、每段都要能站住」。`CONTRIBUTING.md` 把这种纪律具体化为四条可目检的规范。

#### 4.2.2 核心流程：四条风格规范

`CONTRIBUTING.md` 给出的四条规范：

1. **行长（Line Lengths）**：正文每行不超过 79 字符，由 `.editorconfig` 强制；含链接等非文本元素的行可超长。
2. **示例（Examples）**：鼓励链接到真实程序上演示该技巧的 PR / 博客。单个示例写 `[**Example**](url)`，多个写 `[**Example 1**](url), [**Example 2**](url)`，每个独占一行。
3. **标题大小写（Title Style）**：节标题用 Title Case，仅连词等「小词」不大写，如 `Using an Alternative Allocator`。
4. **外链风格（External Link Style）**：指向书外的链接优先用**引用式链接**而非行内链接，因为长 URL 行内会难看地折行；**Example 链接是唯一例外**。

一个直观对照：

- ❌ 行内：`The book's title is [The Rust Performance Book](https://...)`
- ✅ 引用式：正文写 `The book's title is [The Rust Performance Book].`，文末再写 `[The Rust Performance Book]: https://...`

#### 4.2.3 源码精读

`CONTRIBUTING.md` 标题就是 "Style Guide"：

[CONTRIBUTING.md:1-3](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/CONTRIBUTING.md#L1-L3) — 文件自述「These style guidelines are used for the book」，即这是全书的风格硬规范。

四条规范逐段：

[CONTRIBUTING.md:5-9](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/CONTRIBUTING.md#L5-L9) — Line Lengths：正文限 79 字符，由 `.editorconfig` 指定，含链接的行可超长。

[CONTRIBUTING.md:11-26](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/CONTRIBUTING.md#L11-L26) — Examples：鼓励真实程序示例；给出单个示例与多个示例的写法模板。

[CONTRIBUTING.md:28-32](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/CONTRIBUTING.md#L28-L32) — Title Style：节标题 Title Case，给出 `Using an Alternative Allocator` 正例。

[CONTRIBUTING.md:34-51](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/CONTRIBUTING.md#L34-L51) — External Link Style：外链优先引用式，理由是长行内链接折行难看；Example 链接例外。

`.editorconfig` 的实际内容只有一行规则，印证第 1 条：

[.editorconfig:1-2](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.editorconfig#L1-L2) — `[*.md]` 下 `max_line_length = 79`。

terse 风格的最佳范本是 `src/general-tips.md`——整章是「一句原则 + Example 链接」的循环：

[src/general-tips.md:16-19](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md#L16-L19) — 一句「最大改进常来自算法/数据结构而非低层优化」+ 两个 Example 链接，几乎不展开。

[src/general-tips.md:67-70](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md#L67-L70) — terse 不等于无解释：优化代码结构「non-obvious」时，要加引用剖析数据的注释，如「99% 的时间这个 Vec 只有 0 或 1 个元素」。这是 terse 与「可维护性」的平衡点。

作者在 `introduction.md` 里亲口交代 terse 的动机与读者定位：

[src/introduction.md:28-30](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/introduction.md#L28-L30) — 「deliberately terse, favouring breadth over depth, so that it is quick to read」，深度靠外链补。

[src/introduction.md:22-26](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/introduction.md#L22-L26) — 本书聚焦「practical and proven」的技巧，大量附真实 PR 链接，反映作者偏编译器开发、远离科学计算的背景——这正是为什么第 2 条「Example 链接」如此重要。

最短的几章也是 terse 的活教材：`parallelism.md` 仅用几句话点出「safe parallelism 能带来大提升，但深入超出本书范围」，把深度全部外链给 rayon / crossbeam / 一篇 SIMD 综述：

[src/parallelism.md:8-9](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/parallelism.md#L8-L9) — 「an in-depth treatment of parallelism is beyond the scope of this book」——典型 terse：点到为止，深度外链。

#### 4.2.4 代码实践

动手把一段「啰嗦」的文字改写成本书风格。

1. 实践目标：用四条风格规范改写一段不符合规范的 Markdown。
2. 操作步骤：取下面这段（**示例代码**，非书中原文）：
   ```text
   This is a really excellent and very detailed paragraph that goes on at considerable length to explain that you should avoid allocating memory in hot loops because it is slow, see https://github.com/rust-lang/rust/pull/12345.
   ```
   - 把行长压到 ≤79 字符（按语义换行）。
   - 把长行内 URL 改成引用式链接，文末给出定义。
   - 若想加佐证，改成 `[**Example**](url)` 独占一行。
3. 需要观察的现象：改写后字数变少、每行都不超长、链接不再折行。
4. 预期结果：得到一段 terse、符合 `CONTRIBUTING.md` 的文字。
5. 待本地验证：可在编辑器开启 79 列标尺自检，或用命令 `awk '{ if (length($0) > 79) print NR": "length($0) }' file.md` 找出超长行。

#### 4.2.5 小练习与答案

**练习 1**：下面哪种外链写法符合本书规范？为什么。
- (a) `see [the book](https://nnethercote.github.io/perf-book/)`
- (b) `see [the book][book]` 配文末 `[book]: https://...`
- (c) `[**Example**](https://...)` 独占一行

> **答案**：(b) 与 (c) 都符合。(b) 是普通外链的默认偏好（引用式）；(c) 是 Example 链接，按 `CONTRIBUTING.md` 的例外规则必须行内且独占一行。(a) 的行内长链接会折行难看，不推荐用于普通外链。

**练习 2**：节标题 `hashing of integers` 哪里不符合规范？正确写法是？

> **答案**：没有用 Title Case。正确写法是 `Hashing of Integers`（仅 of 这类小词不大写），与书中 `Type Sizes`、`Standard Library Types` 一致。

**练习 3**：terse 是不是意味着「不写注释」？请用 `general-tips.md` 的一句话反驳。

> **答案**：不是。`general-tips.md` 指出优化后代码结构「non-obvious」，此时「explanatory comments are valuable, particularly those that reference profiling measurements」。terse 针对的是冗余叙述，不是要删掉解释非显然优化的高价值注释。

---

### 4.3 新增章节的完整步骤

#### 4.3.1 概念说明

「为本书新增一章」是本讲最硬核的实践，它把 u1-l3 学的 `SUMMARY.md` 机制、`create-missing` 防呆，以及 u7-l1 学的 build/test 闸门全部串起来。

核心事实（来自 u1-l3）：mdBook **不扫描** `src/`，章节是否存在、顺序如何，完全由 `src/SUMMARY.md` 的条目决定。又因为 `book.toml` 里 `create-missing = false`，`SUMMARY` 指向一个**不存在**的文件会直接让 `mdbook build` 失败。这两条合起来决定了「新增章节」必须同时改两个地方，且两边要同步。

#### 4.3.2 核心流程：三步落地 + 一步验证

完整步骤（伪流程）：

```text
1. 选定主题，确认符合本书范围
   （Rust 性能、实用且经实证、有 Example 佐证）
2. 在 src/ 下新建 kebab-case 的 .md 文件，按 terse 风格写
   - 第一行是 # Title（Title Case）
   - 短句 + Example 链接；深度靠外链
3. 在 src/SUMMARY.md 里加一行 "- [Title](filename.md)"
   - 位置决定章节编号（mdBook 按出现顺序自动编号）
4. 本地验证
   - mdbook build   # create-missing=false 会拦下漏登记/漏建文件
   - mdbook serve   # 浏览器 localhost:3000 看效果
   - mdbook test    # 若有 rust 代码块，验证可编译
```

关键约束与坑：

- **文件名 ↔ 标题约定**：文件名 kebab-case 全小写（如 `type-sizes.md`），显示标题 Title Case（如 `Type Sizes`）。含斜杠的标题（"I/O"）斜杠被删，得 `io.md`。
- **`create-missing=false` 是单向防呆**：你登记了却没建文件 → build 直接失败；但你建了文件却没登记 → build 不会报错，只是该文件**不显示**（mdBook 不扫描 `src/`）。所以「登记」与「建文件」必须同步。
- **章节顺序由 `SUMMARY` 行序决定**：想插在 `Parallelism` 与 `General Tips` 之间，就把新行插在那两行之间。
- **新增章节通常不碰 `book.toml`**：纯内容贡献只需 `src/` 下的两个动作。

#### 4.3.3 源码精读

`SUMMARY.md` 全文就是一张「目录清单」，每行一个章节：

[src/SUMMARY.md:1-23](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/SUMMARY.md#L1-L23) — 第 1 行 `# Summary`；第 3 行是无编号前缀章节「Title Page」；第 5–23 行是 19 个正文章节，按出现顺序自动编号。例如第 22 行 `- [General Tips](general-tips.md)`。新章节插在哪，看的就是这 23 行的相对位置。

`book.toml` 的防呆开关是新增章节流程里最关键的一行：

[book.toml:7-8](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L7-L8) — `create-missing = false`。这行让 mdBook 在 `SUMMARY` 引用不存在文件时**报错而非补建**，杜绝幽灵条目，也意味着你必须手动建文件。

其余配置对「新增内容章节」基本透明，但有一处值得知道：编辑链接模板。

[book.toml:13-18](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L13-L18) — `[output.html]` 段的 `edit-url-template` 会给每页生成「编辑此页」链接，指向 `github.com/.../edit/master/{path}`——这正是读者从书上**一键进入贡献流程**的入口，与 4.1 的「issue/PR」流程首尾呼应。

#### 4.3.4 代码实践

完整跑一遍「新增一章」。

1. 实践目标：亲手新增一个占位章节，走完建文件 → 登记 → 验证全流程，并感受 `create-missing` 的防呆。
2. 操作步骤：
   - 在 `src/` 下新建 `my-test-chapter.md`，内容只有一行 `# My Test Chapter`（**示例内容**，正式贡献请按 terse 风格写实）。
   - 在 `src/SUMMARY.md` 第 23 行 `Compile Times` 之后新增一行 `- [My Test Chapter](my-test-chapter.md)`。
   - 运行 `mdbook build`，应成功，并能在 `book/` 里看到新页。
   - 运行 `mdbook serve`，浏览器打开 `localhost:3000`，确认新章节出现在侧边栏末尾。
   - **反向验证 create-missing**：把 `SUMMARY` 里的文件名改成 `does-not-exist.md`（但不建该文件），再 `mdbook build`，应报错。
3. 需要观察的现象：正向时侧边栏出现新章节；反向时 build 因找不到文件而失败。
4. 预期结果：理解「`SUMMARY` 登记 + 文件存在」两者必须同步。
5. 待本地验证：需要本机已 `cargo install mdbook`（见 u1-l2）。**做完后请把测试改动还原**，本讲只读源码、不污染仓库。

#### 4.3.5 小练习与答案

**练习 1**：你在 `src/` 下建了 `locking.md`，但忘了改 `SUMMARY`。`mdbook build` 会成功还是失败？读者能在书里看到吗？

> **答案**：build 会**成功**（文件存在，不触发 `create-missing`），但读者**看不到**这一章——因为 mdBook 不扫描 `src/`，没在 `SUMMARY` 登记的文件不会进书。这正是 `create-missing` 防呆的「单向」盲区：它只拦「登记了没建」，不拦「建了没登记」。

**练习 2**：新章节标题想叫 "Avoiding Lock Overhead"，文件名应该是什么？

> **答案**：kebab-case 全小写，得 `avoiding-lock-overhead.md`。注意是 Title Case 标题、kebab-case 文件名，二者通过 `SUMMARY` 的 `- [Avoiding Lock Overhead](avoiding-lock-overhead.md)` 对应起来。

**练习 3**：为什么新增一个纯内容章节通常不需要改 `book.toml`？

> **答案**：`book.toml` 管的是「构建行为与输出形态」（书名、src 目录、`create-missing`、html 主题、输出后端、编辑链接模板等），而新增章节只动 `src/` 下的内容与 `SUMMARY` 的目录登记。除非要启用新输出后端（如 epub）或改主题，否则 `book.toml` 无需改动。

---

### 4.4 mdBook 扩展与自定义（贡献者视角）

#### 4.4.1 概念说明

「mdBook 扩展与自定义」这一模块在 u7-l1 已深入讲过 preprocessor → renderer（输出后端）→ theme 的可插拔管线。本讲只从**贡献者视角**收口：当你贡献内容时，mdBook 的「可扩展面」长什么样、什么时候你才需要碰它。

一句话定位：**绝大多数内容贡献只动 `src/`，根本不碰 mdBook 扩展**；只有当你想改「书怎么被渲染/输出」时，才会进入这一层。把「内容贡献」与「工具链扩展」分清，是本模块的目标。

#### 4.4.2 核心流程

贡献者可能接触的 mdBook 自定义点，按频率从高到低：

| 自定义点 | 在哪改 | 何时需要 | 是否影响内容 |
|----------|--------|----------|--------------|
| 章节内容/目录 | `src/*.md`、`src/SUMMARY.md` | 任何内容贡献 | 是 |
| 行长等编辑规范 | `.editorconfig` | 想统一编辑器换行 | 否（仅编辑体验）|
| 书名/作者/src/edition | `book.toml [book]` | 极少（元信息）| 否 |
| HTML 主题/编辑链接/site-url | `book.toml [output.html]` | 想改外观或部署地址 | 否 |
| 新增输出后端（epub 等）| `book.toml` + 装 `mdbook-xxx` + CI | 想出新格式 | 否 |
| preprocessor / 自定义 theme 文件 | `book.toml` + 新目录 | 高级定制 | 否 |

关键结论：**新增章节 ≠ 扩展 mdBook**。前者是内容，后者是工具链。多数贡献者不会碰后三行。

#### 4.4.3 源码精读

perf-book 是极简配置的典型：整个 `book.toml` 只有 25 行，且只启用内置 HTML renderer。

[book.toml:1-5](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L1-L5) — `[book]` 段：书名、作者、src 目录、语言——元信息，贡献内容时基本不动。

[book.toml:13-18](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L13-L18) — `[output.html]` 段：`smart-punctuation`、`default-theme`、`git-repository-url`、`edit-url-template`、`site-url`。`edit-url-template` 决定每页「编辑此页」链接，是贡献流程的入口。

注释掉的 epub 后端是「启用一个新输出后端要改几处」的活教材：

[book.toml:20-24](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L20-L24) — 注释掉的 `[output.epub]` + `optional = true`。要真正启用 epub（u7-l1 详述），需要：①在 `book.toml` 取消注释；②`cargo install mdbook-epub`；③在 `ci.yml` 里加安装与产物拷贝步骤。**这三处联动改动正是「mdBook 扩展」的典型工作量**——也是为什么它不属于「内容贡献」。

CI 的 deploy 条件则决定了你的改动**何时上线**（u7-l1 详述）：

[.github/workflows/ci.yml:37-44](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.github/workflows/ci.yml#L37-L44) — deploy 步骤仅在 `push` + `refs/heads/master` + 仓库为 `nnethercote/perf-book` 时触发。对贡献者意味着：你在 fork/PR 上的改动只会被 build/test 验证，不会部署；只有作者在正本 master 推送才上线。这与 4.1「issue 优先、作者重写」的流程是一致的设计。

#### 4.4.4 代码实践

源码阅读型实践：盘点「新增一章」到底要碰哪些文件。

1. 实践目标：清晰区分内容贡献与 mdBook 扩展，列出两者的文件改动边界。
2. 操作步骤：假设要新增一章「Avoiding Lock Overhead」。在纸上列「需要改的文件」与「不需要改的文件」两栏，再对照本模块表格核对。
3. 需要观察的现象：你会得出需要改的只有 `src/avoiding-lock-overhead.md`（新建）与 `src/SUMMARY.md`（加一行）；`book.toml`、`ci.yml`、`.editorconfig` 都不动。
4. 预期结果：得出「新增纯内容章节 = 2 个文件改动」的结论。
5. 待本地验证：无（纯清单梳理）。

#### 4.4.5 小练习与答案

**练习 1**：你想让本书多出一个 EPUB 版本。这属于「内容贡献」还是「mdBook 扩展」？至少要改哪几处？

> **答案**：属于 mdBook 扩展（输出后端），不是内容贡献。至少三处：①`book.toml` 启用 `[output.epub]`（可设 `optional = true`）；②本机/CI 安装 `mdbook-epub`；③CI（`ci.yml`）里加 epub 安装与产物拷贝步骤（`README`/`ci.yml` 中注释掉的 EPUB 块即此意图）。

**练习 2**：`edit-url-template` 与本讲 4.1 的贡献流程有什么关系？

> **答案**：`edit-url-template` 给每页生成「编辑此页」链接，直接指向 `github.com/.../edit/master/{path}`，是读者从书页一键进入「改这一页」的入口——即 PR 流程的起点。它与「issue 优先」并不矛盾：读者可借此快速定位要改的文件，但按本书约定，仍应先开 issue 让作者把关措辞。

---

## 5. 综合实践

把四个模块串成一个完整任务：**按本书风格，设计并落地一个新章节《Avoiding Lock Overhead》（锁的开销与避免）**。

### 任务一：草拟一页 terse 大纲

阅读 `CONTRIBUTING.md` 与 `README.md` 后，按本书风格写一页（约 15–25 行）大纲，要求：

1. 第一行 `# Avoiding Lock Overhead`（Title Case）。
2. 用 2–4 句 terse 短句点出核心：锁的固定访问开销（承接 u5-l3 包装类型）、何时该减竞争、常见手段（合并锁、归约、无锁原子）。
3. 至少附 2 个 `[**Example**](url)` 占位（可填你熟悉的 rustc PR 链接，无则标注「待补真实 PR」）。
4. 深度内容用引用式外链（如指向 rayon / Rust Atomics and Locks）。
5. 全文每行 ≤79 字符。

参考骨架（**示例代码**，非书中原文）：

```markdown
# Avoiding Lock Overhead

Locks have a fixed access cost; under contention this cost is amplified
into forced serialization that eats your speedup. Prefer reducing
contention over micro-tuning a single lock.

Merge locks that are always accessed together, use reductions to avoid
shared state, or replace a hot counter with an atomic.
[**Example 1**](TODO-real-pr),
[**Example 2**](TODO-real-pr).

See [Rust Atomics and Locks][Atomics] for depth.

[Atomics]: https://marabos.nl/atomics/
```

### 任务二：列出落地所需的文件改动清单

写出把这一章真正加入书里所需的全部改动：

| 文件 | 改动类型 | 具体内容 |
|------|----------|----------|
| `src/avoiding-lock-overhead.md` | 新建 | 任务一的 terse 大纲 |
| `src/SUMMARY.md` | 修改 | 在合适位置（如 `Wrapper Types` 之后）加 `- [Avoiding Lock Overhead](avoiding-lock-overhead.md)` |
| `book.toml` | 不改 | 纯内容章节无需动配置 |
| `.github/workflows/ci.yml` | 不改 | build/test 闸门自动覆盖新章节 |

### 任务三（可选，需本地 mdBook）：验证

- 运行 `mdbook build` 与 `mdbook test`，确认无幽灵条目、代码块可编译。
- 运行 `mdbook serve`，在 `localhost:3000` 确认新章节出现在侧边栏。
- 反向验证：故意把 `SUMMARY` 文件名写错，观察 build 是否因 `create-missing` 失败。

> 提醒：本任务仅用于学习。按本书「issue 优先」流程，正式贡献应先把这份大纲作为 issue 提交给 `nnethercote/perf-book`，让作者认可方向与措辞，而非直接开 PR。完成后请把本地测试改动还原，避免污染仓库。

## 6. 本讲小结

- perf-book 偏好 **issue 而非 PR**：作者对措辞极其讲究，PR 通常只取思想、重写文字；且**不接受生成式 AI 内容**。
- 写作风格四条硬规范：行长 ≤79 字符、附 `[**Example**]` 真实 PR 链接、节标题 Title Case、外链优先引用式（Example 例外）。
- 「新增一章」= 在 `src/` 新建 kebab-case 文件 + 在 `src/SUMMARY.md` 登记一行，两步必须同步；`create-missing=false` 拦「登记没建」，但「建了没登记」不会报错、只是不显示。
- 章节顺序由 `SUMMARY` 行序决定，新增纯内容章节**不碰 `book.toml`**。
- 「内容贡献」与「mdBook 扩展（新输出后端/主题/preprocessor）」是两件事；后者要联动改 `book.toml` + 装工具 + CI，属于工具链工作。
- CI 仅在正本 master 推送时部署，fork/PR 只验证不部署——与「issue 优先、作者把关」一致。

## 7. 下一步学习建议

本讲是全套手册收官篇，你已具备从「读懂」到「贡献」的全套能力。建议：

1. **回到源码做一次风格审计**：挑一篇最短的章节（如 `src/machine-code.md`、`src/parallelism.md`），逐条核对其是否满足 `CONTRIBUTING.md` 的四条规范——这是巩固风格感最快的方式。
2. **开一个真实 issue**：如果你在工作中遇到一个本书没覆盖、且有真实 PR 佐证的性能技巧，按本讲流程开一个 issue 给作者。
3. **横向阅读**：把 u7-l1（mdBook 测试/CI/扩展）与本讲对照，理解「书稿正确性」「自动部署」「内容贡献」三者如何由同一套 build/test/deploy 流水线串起来。
4. **进阶**：若想深入 mdBook 工具链本身（写一个 preprocessor 或自定义 renderer），可阅读 mdBook 官方文档；本书仓库的极简 `book.toml` 已是好范本。
