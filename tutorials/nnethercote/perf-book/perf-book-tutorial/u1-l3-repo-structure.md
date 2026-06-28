# 仓库结构与章节的组织方式

## 1. 本讲目标

学完本讲，你应当能够：

- 理解 **mdBook 用 `src/SUMMARY.md` 作为「单一事实来源」决定全书目录与章节顺序**的机制；
- 说出 perf-book 仓库的顶层目录结构与各文件职责，并知道哪些是「书稿」、哪些是「构建产物」；
- 熟悉 `src/` 下 19 个正文章节（外加标题页）的命名约定（kebab-case、显示标题与文件名的对应规则）；
- 给定任意主题，能快速定位它落在哪个章节文件里；
- 动手在 `SUMMARY.md` 里增删一个条目，并解释 `book.toml` 中 `create-missing = false` 在此过程中扮演的角色。

本讲是入门单元的收尾：u1-l1 解决「这本书是什么」，u1-l2 解决「怎么把它跑起来」，本讲解决「它的内容在仓库里是怎么组织的」。掌握目录机制后，后续进阶单元（基准测试、剖析、构建配置……）你就能随时精准定位到对应章节原文。

## 2. 前置知识

在继续前，请确认你已经理解（u1-l1、u1-l2 已建立）：

- **perf-book 是一本用 mdBook 渲染的在线技术书**，它的「源码」就是 `src/` 下的一堆 Markdown 文件，而不是可运行的 Rust 库或二进制。
- **mdBook** 是一个独立的 Rust 命令行工具，命令名是小写的 `mdbook`，用 `cargo install mdbook` 安装。
- 三条核心命令：`mdbook build`（生成静态站点到 `book/`）、`mdbook serve`（本地预览 + 热更新）、`mdbook test`（编译验证书中代码块）。
- **`book.toml` 是 mdBook 的总配置文件**，其中 `[book] src = "src"` 指明书稿源目录，`[build] create-missing = false` 是本讲反复要用到的关键开关。

如果你对「kebab-case」这个词不熟：它是一种命名风格，**全小写、单词之间用连字符 `-` 连接**，例如 `build-configuration`、`bounds-checks`。perf-book 的章节文件名几乎全部遵循这个风格。

> 一句话定位：本讲不写 Rust 代码，而是把 perf-book 当作一个「以 Markdown 为源码」的项目，搞清楚它的文件是如何被组织成一本有目录、有顺序、有编号的书的。

## 3. 本讲源码地图

本讲涉及的关键文件如下（均位于仓库根目录或 `src/` 下）：

| 文件 | 角色 | 本讲用来理解什么 |
| --- | --- | --- |
| `src/SUMMARY.md` | 全书目录 / 章节顺序的「单一事实来源」 | mdBook 如何由它决定目录、编号与导航 |
| `book.toml` | mdBook 构建配置 | `src` 源目录、`create-missing` 开关如何影响构建 |
| `README.md` | 仓库入口说明 | 顶层视角的构建与产物说明 |
| `.gitignore` | 忽略规则 | 印证 `book/` 是构建产物而非源码 |
| `src/title-page.md` | 标题页（封面式首页） | 标题页这种「非编号条目」如何声明 |

辅助理解（非本讲核心，但顺便建立全局观）：仓库顶层还有 `.editorconfig`、`CODE_OF_CONDUCT.md`、`CONTRIBUTING.md`、`LICENSE-APACHE`、`LICENSE-MIT`、`.github/workflows/ci.yml` 等管理类文件，它们会在 u7（维护与贡献）单元详细讲解。

## 4. 核心概念与源码精读

本讲拆成三个最小模块：

1. **4.1 mdBook 的 SUMMARY.md 目录机制** —— 一份 Markdown 如何变成「带编号、有顺序」的目录。
2. **4.2 src/ 目录的章节组织** —— 仓库里到底有哪些文件，它们如何分组。
3. **4.3 标题页与各章的命名约定** —— 显示标题与文件名之间的翻译规则。

---

### 4.1 mdBook 的 SUMMARY.md 目录机制

#### 4.1.1 概念说明

mdBook 与很多静态站点生成器有一个关键区别：**它不会自动扫描 `src/` 下的所有 `.md` 文件来生成目录**。相反，它只认一个文件——`src/SUMMARY.md`。这个文件就是全书的「单一事实来源」（single source of truth）：

- 目录里出现哪些章节、**顺序如何**，完全由 `SUMMARY.md` 里条目的排列决定；
- 每个条目用 Markdown 链接 `- [显示标题](相对路径.md)` 的形式写出来，路径相对于 `book.toml` 里 `[book] src` 指定的源目录；
- 正文章节的**编号（1、2、3……以及子章节的 1.1、1.2）由 mdBook 根据条目在列表中的位置自动生成**，你不需要、也不应该在标题里手写编号；
- 如果 `SUMMARY.md` 引用了一个**不存在的文件**，构建是否报错，取决于 `book.toml` 里的 `create-missing` 开关。

一句话：**改顺序=改 `SUMMARY.md`；加章节=在 `SUMMARY.md` 加条目并（在此项目中）必须同时创建对应文件**。

#### 4.1.2 核心流程

mdBook 在 `mdbook build` 时处理 `SUMMARY.md` 的流程可以用下面这段伪代码描述：

```
读取 src/SUMMARY.md
  ├── 顶部 `# 标题`：作为摘要页标题（不渲染成正文页）
  ├── 不在列表里的独立链接（如 [Title Page](...)）：标记为「前缀章节」，无编号，排最前
  └── `- [标题](路径)` 列表项：按出现顺序依次编号 1, 2, 3, ...
        对每个条目：
          ① 解析出 相对路径 = src目录 + 条目路径
          ② 若该文件存在  → 加入目录与导航
          ③ 若该文件不存在 →
                create-missing = true  (mdBook 默认) → 自动创建空文件
                create-missing = false (perf-book 采用) → 构建报错中止
最终：按编号生成左侧侧边栏 + 页面间的「上一页/下一页」导航
```

关键结论：**在 perf-book 里，`SUMMARY.md` 的每一条都必须有真实文件对应**，否则 `mdbook build` 直接失败。这是作者刻意设置的「防呆」——防止目录指向幽灵文件。

#### 4.1.3 源码精读

先看 `SUMMARY.md` 的开头：第一行是标题，第三行是标题页条目。

[src/SUMMARY.md:1-3](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/SUMMARY.md#L1-L3) —— 第 1 行 `# Summary` 是摘要页标题；第 3 行 `[Title Page](title-page.md)` 是一个**不在编号列表里的独立链接**，mdBook 把它当作「前缀章节」，渲染成无编号的封面式首页（详见 4.3）。

接着是全书唯一的编号章节列表，从 Introduction 到 Compile Times 共 19 条：

[src/SUMMARY.md:5-23](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/SUMMARY.md#L5-L23) —— 每一行形如 `- [Benchmarking](benchmarking.md)`：方括号里是**显示标题**（带空格、首字母大写），圆括号里是**相对 `src/` 的文件路径**。mdBook 按这 19 行的**出现顺序**自动编号，所以这里的第 1 条 `Introduction` 就是侧边栏里的「1. Introduction」，最后一条 `Compile Times` 就是「19. Compile Times」。

注意一个重要事实：**章节在书里的编号，完全等于它在 `SUMMARY.md` 里的行序**，与文件名字母序无关。例如 `profiling.md` 字母序靠后，但在书里是第 5 章，因为它在 `SUMMARY.md` 第 9 行（列表第 5 条）。

再看 `create-missing` 这个决定性开关：

[book.toml:7-8](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L7-L8) —— `[build]` 段下 `create-missing = false`。mdBook 的默认值其实是 `true`（缺文件会自动补一个空文件让构建继续），但 perf-book **显式关掉了它**。后果是：一旦你在 `SUMMARY.md` 写了条目却忘了创建对应 `.md`，`mdbook build` 会立即报错，而不是悄悄生成一个空白页。这条配置是本讲综合实践的核心。

> 提示：mdBook 官方约定摘要文件必须叫 `SUMMARY.md`（大写），且必须放在 `src` 目录下。它不会被渲染成一个可点击的正文页面，而是被解析为「目录元数据」。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「`SUMMARY.md` 条目 ↔ 真实文件 ↔ 构建成败」三者之间的因果关系。

**操作步骤**（在你的本地克隆里进行；本实践会临时改动 `SUMMARY.md`，最后会用 git 还原）：

1. 先确认基线能构建成功：`mdbook build`（应正常生成 `book/`）。
2. 打开 `src/SUMMARY.md`，在最后一行 `- [Compile Times](compile-times.md)` 下面新增一行：
   ```
   - [Experiments](experiments.md)
   ```
   **先不要**创建 `src/experiments.md`。
3. 再次运行 `mdbook build`。

**需要观察的现象**：

- 第 3 步应当**构建失败**，mdBook 报告 `experiments.md` 不存在（因为 `create-missing = false`）。
- 此时 `book/` 不会更新出新章节。

**预期结果**：构建报错、提示缺少 `experiments.md`；这正是 `create-missing = false` 在「挡住」不完整的目录改动。

**关于具体报错文案**：不同 mdBook 版本措辞略有差异（典型形如 *“failed to load book”* 并指出找不到对应章节文件），**具体措辞待本地验证**；但「构建失败、缺文件被拦截」这一行为是确定的。

> 还原：实践结束后运行 `git checkout src/SUMMARY.md` 撤销改动，保持书稿干净。（下一节的综合实践会接着把缺失文件补上，体验「补齐后即构建成功」。）

#### 4.1.5 小练习与答案

**练习 1**：如果作者想把「Profiling」从第 5 章挪到第 1 章（紧跟 Introduction 之后），需要修改哪些地方？要不要改文件名？

> **答案**：只需要在 `src/SUMMARY.md` 里把 `- [Profiling](profiling.md)` 这一整行**剪切、粘贴**到 `- [Introduction](introduction.md)` 之后。编号会自动重排为 2。**不需要改文件名**，也不需要改 `profiling.md` 内部任何内容——顺序完全由 `SUMMARY.md` 决定。

**练习 2**：`book.toml` 里把 `create-missing` 改回 mdBook 默认的 `true`，会对 4.1.4 的实践产生什么影响？

> **答案**：第 3 步不再报错——mdBook 会**自动创建一个空的 `src/experiments.md`** 让构建通过。这听起来「方便」，但风险是：你可能根本没写内容，书里却悄悄多出一个空白章节。perf-book 选择 `false` 就是为了让这种疏漏立刻暴露。

---

### 4.2 src/ 目录的章节组织

#### 4.2.1 概念说明

`src/` 是 perf-book 真正的「书稿源码」所在。理解它的组织，要分清两类东西：

- **被 `SUMMARY.md` 引用的正文页**：标题页 + 19 个章节；
- **`SUMMARY.md` 本身**：是目录元数据，不作为正文页渲染。

此外要建立「仓库全局观」：`src/` 之外还有构建配置（`book.toml`）、说明（`README.md`）、CI（`.github/workflows/ci.yml`）、许可与行为准则等管理文件；而 `book/` 目录是 `mdbook build` 的**产物**，被 `.gitignore` 忽略，**不属于源码**。

#### 4.2.2 核心流程

仓库从「源文件」到「成书」的数据流：

```
源码（纳入 git）                       构建产物（被忽略，不入 git）
─────────────────                     ─────────────────────────
book.toml        ┐
src/SUMMARY.md   ├── mdbook build ──> book/   (HTML 静态站点)
src/*.md         ┘                      └─ .gitignore 第 1 行忽略 "book"
README.md 等管理文件（不参与成书内容）
```

也就是说：你阅读、修改的对象是 `src/` 下的 Markdown 与 `book.toml`；`book/` 只是每次构建临时生成、用完即弃的输出。

#### 4.2.3 源码精读

先确认 `book/` 是产物而非源码——`.gitignore` 第一行就忽略了它：

[.gitignore:1-1](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.gitignore#L1-L1) —— `book` 这一行让 git 忽略整个 `book/` 目录。所以你在仓库里**看不到 `book/`**，它只在你本地运行 `mdbook build` 后出现。这也解释了为什么 `git ls-files` 列出的全是源码与配置。

`book.toml` 指明源目录就是 `src`：

[book.toml:1-5](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L1-L5) —— `[book]` 段里 `src = "src"`，告诉 mdBook「书稿都在 `src/` 下」。因此 `SUMMARY.md` 里的相对路径（如 `benchmarking.md`）会被解析成 `src/benchmarking.md`。

`README.md` 同样说明了产物的去向：

[README.md:28-32](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md#L28-L32) —— 运行 `mdbook build` 后，生成的文件放在 `book/` 目录。这句话把「源码在 `src/`、产物在 `book/`」的边界讲得很清楚。

至于 `src/` 内部，用 `git ls-files src/` 可数出**共 21 个 `.md` 文件**，构成为：1 个 `SUMMARY.md`（目录）+ 1 个 `title-page.md`（标题页）+ 19 个正文章节。这 19 个正文条目与 `SUMMARY.md` 第 5–23 行一一对应，完整对照见下表（4.2.4 之后的总表）。

> 旁注：`SUMMARY.md` 的条目顺序并不严格等于字母序，也不等于「学习先后」的唯一解，而是作者编排的**阅读顺序**。从主题上大致可看出三组（这是为方便记忆的归纳，书里并无正式的「Part」分节）：
> - **测量/工作流组**（第 2–5 章）：Benchmarking、Build Configuration、Linting、Profiling——「先测量再优化」的准备工作；
> - **代码级优化技巧组**（第 6–17 章）：Inlining、Hashing、Heap Allocations、Type Sizes、Standard Library Types、Iterators、Bounds Checks、I/O、Logging and Debugging、Wrapper Types、Machine Code、Parallelism；
> - **通用与编译组**（第 18–19 章）：General Tips、Compile Times。
>
> 这与本学习手册后续单元（u2 测量 → u3/u4/u5 代码优化 → u6 通用原则）的拆分思路是一致的。

#### 4.2.4 代码实践

**实践目标**：用一条只读命令把「书里的章节」和「磁盘上的文件」对齐核对，体会「目录即文件清单」。

**操作步骤**：

1. 在仓库根目录运行：`git ls-files src/`，观察列出的 21 个 `.md` 文件。
2. 打开 `src/SUMMARY.md`，逐行把每个 `- [标题](文件名)` 与第 1 步的文件列表比对。

**需要观察的现象**：

- `SUMMARY.md` 里出现的每一个文件名，都能在 `git ls-files src/` 的输出里找到；
- 反过来，`src/` 下除了 `SUMMARY.md` 之外，每个 `.md` 都被 `SUMMARY.md` 引用（没有「孤儿文件」）。

**预期结果**：二者完全吻合——这正是 `create-missing = false` 长期约束出来的结果：目录与文件不会失同步。

#### 4.2.5 小练习与答案

**练习 1**：某天有人在 `src/` 下新建了一个 `notes.md` 但没在 `SUMMARY.md` 里登记，运行 `mdbook build` 会发生什么？这个文件会出现在书里吗？

> **答案**：构建**不会报错**（因为 `create-missing` 管的是「目录里有、磁盘上没有」，而这里是反过来的「磁盘上有、目录里没有」），但 `notes.md` **不会出现在书里**——mdBook 只渲染 `SUMMARY.md` 登记过的页面。它成了一个对成书「不可见」的孤儿文件。

**练习 2**：为什么仓库里看不到 `book/` 目录？

> **答案**：因为 `.gitignore` 第 1 行忽略了 `book`。`book/` 是 `mdbook build` 生成的 HTML 产物，属于可重新生成的派生物，所以不纳入版本控制；你只有在本地构建后才能看到它。

---

### 4.3 标题页与各章的命名约定

#### 4.3.1 概念说明

perf-book 的章节有一套稳定的命名约定，掌握它就能在「显示标题」和「文件名」之间互相翻译：

- **文件名**：kebab-case、全小写、`.md` 结尾；多词用连字符连接，例如 `build-configuration.md`、`logging-and-debugging.md`。
- **显示标题**（`SUMMARY.md` 方括号里的文字）：英文 Title Case，单词间用空格，例如 "Build Configuration"、"Logging and Debugging"。
- **特殊字符处理**：文件名不能含 `/`，所以显示标题里的斜杠会被去掉——最典型的就是 "I/O" → 文件名 `io.md`。
- **标题页**用专门的 `title-page.md`，并在 `SUMMARY.md` 里以**独立链接**（非编号列表项）声明，渲染为无编号的封面式首页。

#### 4.3.2 核心流程

从显示标题推导文件名的「翻译规则」：

```
显示标题 "Logging and Debugging"
   │  ① 转小写
   ▼
"logging and debugging"
   │  ② 空格 → 连字符
   ▼
"logging-and-debugging"
   │  ③ 追加 .md
   ▼
logging-and-debugging.md

特例：显示标题含 "/"（如 "I/O"）
   ① 转小写 → "i/o"
   ② 删除斜杠、空格→连字符（此处无空格）→ "io"
   ③ 追加 .md → io.md
```

反过来看 `SUMMARY.md` 时，看到方括号里的标题，你基本能直接猜出对应文件名。

#### 4.3.3 源码精读

先看标题页这条特殊条目——它是**独立链接、不在编号列表里**：

[src/SUMMARY.md:3-3](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/SUMMARY.md#L3-L3) —— `[Title Page](title-page.md)` 写在编号列表之前、且不是 `- ` 列表项。mdBook 把它识别为「前缀章节」，渲染时**不带编号**，作为全书第一页。

标题页的内容本身只是一张封面：

[src/title-page.md:1-7](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/title-page.md#L1-L7) —— 用内联 HTML 调大字号显示书名 *The Rust Performance Book*、首次发布时间（2020 年 11 月）、作者，以及一个指向源码仓库的链接。它没有实质正文，纯粹是「书的门面」。

再看命名约定的几条「规则样本」——注意单词数与连字符的对应，以及 "I/O" 的特例：

[src/SUMMARY.md:7-17](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/SUMMARY.md#L7-L17) —— 这里能同时看到三类样本：
- 单词主题 → 单个小写词：`Linting`→`linting.md`、`Profiling`→`profiling.md`、`Inlining`→`inlining.md`、`Hashing`→`hashing.md`、`Iterators`→`iterators.md`；
- 多词主题 → kebab-case：`Build Configuration`→`build-configuration.md`、`Heap Allocations`→`heap-allocations.md`、`Type Sizes`→`type-sizes.md`、`Standard Library Types`→`standard-library-types.md`、`Bounds Checks`→`bounds-checks.md`；
- 含斜杠特例：`I/O`→`io.md`（斜杠被删除）。

为方便查阅，下面给出**全部 19 个正文章节 + 标题页**的完整对照表（编号 = 在 `SUMMARY.md` 编号列表中的位置）：

| 编号 | 显示标题（`SUMMARY.md`） | 文件名 | 命名类型 |
| --- | --- | --- | --- |
| 前缀 | Title Page | `title-page.md` | 前缀章节（无编号） |
| 1 | Introduction | `introduction.md` | 单词 |
| 2 | Benchmarking | `benchmarking.md` | 单词 |
| 3 | Build Configuration | `build-configuration.md` | kebab-case |
| 4 | Linting | `linting.md` | 单词 |
| 5 | Profiling | `profiling.md` | 单词 |
| 6 | Inlining | `inlining.md` | 单词 |
| 7 | Hashing | `hashing.md` | 单词 |
| 8 | Heap Allocations | `heap-allocations.md` | kebab-case |
| 9 | Type Sizes | `type-sizes.md` | kebab-case |
| 10 | Standard Library Types | `standard-library-types.md` | kebab-case |
| 11 | Iterators | `iterators.md` | 单词 |
| 12 | Bounds Checks | `bounds-checks.md` | kebab-case |
| 13 | I/O | `io.md` | 含斜杠特例 |
| 14 | Logging and Debugging | `logging-and-debugging.md` | kebab-case |
| 15 | Wrapper Types | `wrapper-types.md` | kebab-case |
| 16 | Machine Code | `machine-code.md` | kebab-case |
| 17 | Parallelism | `parallelism.md` | 单词 |
| 18 | General Tips | `general-tips.md` | kebab-case |
| 19 | Compile Times | `compile-times.md` | kebab-case |

> 这张表是你后续「按主题找原文」的索引。例如想看「替换默认哈希算法」，直接打开 `hashing.md`；想看「边界检查」，打开 `bounds-checks.md`。

#### 4.3.4 代码实践

**实践目标**：亲手走一遍「加一个新章节」的完整落地流程，把命名约定和 `create-missing` 串起来验证。

**操作步骤**（本地克隆中进行，结束后用 git 还原）：

1. 在 `src/` 下新建文件 `src/experiments.md`，写入最小内容：
   ```markdown
   # Experiments

   This is a scratch page for performance experiments.
   ```
2. 在 `src/SUMMARY.md` 的最后一行 `- [Compile Times](compile-times.md)` 之后新增：
   ```
   - [Experiments](experiments.md)
   ```
3. 运行 `mdbook build`，再 `mdbook serve` 打开 `localhost:3000`。

**需要观察的现象**：

- 这次构建**成功**（因为文件已存在，满足 `create-missing = false`）。
- 侧边栏末尾出现新条目，**自动编号为 20**（标题 "Experiments"，无需手写编号）。
- 点击它能正常打开你写的那页内容。

**预期结果**：文件 + 目录条目都齐备时，新章节顺利加入成书，编号自动续接。结合 4.1.4 的失败案例，你就完整体验了 `create-missing = false` 的「双面性」：它拦住残缺改动，放行完整改动。

> 还原：`git checkout src/SUMMARY.md` 并 `rm src/experiments.md`（该文件是你新建的，未被跟踪，需手动删除）。

#### 4.3.5 小练习与答案

**练习 1**：假设要新增一章叫 "Cache Friendliness"，按本书命名约定，文件名应该是什么？`SUMMARY.md` 里那一行该怎么写？

> **答案**：文件名 `cache-friendliness.md`（转小写、空格→连字符、加 `.md`）；`SUMMARY.md` 里写 `- [Cache Friendliness](cache-friendliness.md)`，并需同时创建 `src/cache-friendliness.md`，否则因 `create-missing = false` 构建失败。

**练习 2**：为什么 "I/O" 这一章的文件名是 `io.md` 而不是 `i-o.md` 或 `io-something.md`？

> **答案**：因为文件名里不允许出现路径分隔符 `/`，所以显示标题 "I/O" 里的斜杠被直接删除，得到 `io`，再追加 `.md`。这里没有空格需要转连字符，所以结果是 `io.md` 而非 `i-o.md`。

**练习 3**：标题页 `title-page.md` 和正文 19 章在 `SUMMARY.md` 里的写法有什么本质区别？这个区别带来什么效果？

> **答案**：标题页写成**独立链接** `[Title Page](title-page.md)`（非 `- ` 列表项、位于编号列表之前），而正文 19 章都是 `- [标题](路径)` **列表项**。区别带来的效果是：标题页被当作「前缀章节」**不参与自动编号**、作为封面式首页渲染；正文条目则按位置自动获得 1–19 的编号。

## 5. 综合实践

把本讲三个模块串起来，完成一个「读懂目录 → 验证机制 → 体验增删」的小任务。

**任务**：在不破坏书稿的前提下，验证「`SUMMARY.md` 是全书组织的唯一入口」这一论断。

**操作步骤**：

1. **盘点**：对照 `src/SUMMARY.md`，手写或在文本里列出全部 19 个正文章节及其对应文件名（可直接参考 4.3.3 的总表自测遮住答案）。
2. **核对**：运行 `git ls-files src/`，确认 `SUMMARY.md` 的每一条都有真实文件、且 `src/` 下没有未被引用的孤儿 `.md`。
3. **正向实验（完整改动）**：新建 `src/experiments.md`（写一行标题）+ 在 `SUMMARY.md` 末尾加 `- [Experiments](experiments.md)` → `mdbook build` 应成功，新章节自动编号为 20。
4. **反向实验（残缺改动）**：删掉刚建的 `src/experiments.md` 但保留 `SUMMARY.md` 里的条目 → 再次 `mdbook build` 应**失败**，因为 `create-missing = false`。
5. **还原**：`git checkout src/SUMMARY.md`，并删除任何你新建的临时文件，确保 `git status` 干净。

**需要观察并记录的现象**：

- 第 3 步：构建成功、侧边栏多出编号 20 的条目；
- 第 4 步：构建失败、报缺文件；
- 全程：**章节顺序与编号只随 `SUMMARY.md` 改变**，文件名内部内容不影响排序。

**预期结果**：你将直观验证三条核心规律——① 目录顺序 = `SUMMARY.md` 行序；② 编号由位置自动生成；③ `create-missing = false` 强制「目录条目 ↔ 真实文件」严格同步。具体报错文案与侧边栏样式**待本地验证**（随 mdBook 版本略有差异）。

## 6. 本讲小结

- **`src/SUMMARY.md` 是全书目录的单一事实来源**：章节是否出现、顺序如何、编号多少，全部由它决定；mdBook 不会自动扫描 `src/`。
- **章节编号由位置自动生成**：`SUMMARY.md` 编号列表（第 5–23 行）从上到下依次是第 1–19 章；标题页作为前缀章节不带编号。
- **`src/` 共 21 个 `.md` 文件**：1 个 `SUMMARY.md` + 1 个 `title-page.md` + 19 个正文章节，二者（目录条目与磁盘文件）严格一一对应。
- **命名约定**：文件名 kebab-case 全小写（`build-configuration.md`），显示标题 Title Case（"Build Configuration"）；"I/O" 这类含斜杠的标题，斜杠被删除得 `io.md`。
- **`book.toml` 的 `create-missing = false`** 是关键防呆：`SUMMARY.md` 引用了不存在的文件时构建直接报错，杜绝幽灵条目。
- **`book/` 是构建产物而非源码**：被 `.gitignore` 忽略，源码在 `src/`、配置在 `book.toml`。

## 7. 下一步学习建议

入门单元到此结束，你已经能把 perf-book「读对、跑起来、找得到」。接下来进入**第二单元：性能优化工作流（先测量再优化）**，建议按以下顺序：

- **u2-l1 Benchmarking**：先学会建立可比较的性能基线——这是后续一切代码优化的前提。对应原文 [src/benchmarking.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md)（`SUMMARY.md` 第 2 章）。
- **u2-l3 Build Configuration**：系统学习 `book.toml` 背后真正的 Cargo/rustc 构建选项（`codegen-units`、LTO、`panic`、`opt-level` 等），对应 [src/build-configuration.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md)。
- 如果你更想先了解「整本书的部署与协作机制」，也可以跳到 **u7-l1 mdBook 进阶** 与 **u7-l2 贡献内容**，那里会讲解 `.github/workflows/ci.yml` 的 build/test/deploy 流程与新增章节的协作规范。

> 一句话衔接：本讲让你「找得到章节」，下一阶段让你「用得上工具」——先用基准测试和剖析把性能问题量化，再去读代码级优化章节才有意义。
