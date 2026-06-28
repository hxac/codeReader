# 用 mdBook 构建与运行 perf-book

## 1. 本讲目标

上一篇讲义我们弄清楚了 perf-book「是什么」：一本用 Markdown 写成的《Rust 性能手册》，它的源码就是书稿本身。本篇要解决「怎么把它跑起来」。

读完本讲，你应当能够：

1. 用 `cargo install mdbook` 安装 mdBook，并验证安装成功。
2. 熟练使用 `mdbook build` / `mdbook serve` / `mdbook test` 这三条核心命令，并清楚它们各自的产物。
3. 逐字段读懂 `book.toml`，知道书名、作者、源目录、HTML 输出等配置如何影响构建。
4. 读懂 GitHub Actions（`.github/workflows/ci.yml`）是如何在 CI 里自动构建、测试并部署本书的。

## 2. 前置知识

在动手之前，先建立三个直觉。

**什么是 mdBook。** mdBook 是 Rust 官方生态里的一个命令行工具，作用是把一堆 Markdown 文件渲染成一本带目录、带搜索、可切换主题的在线书（类似 GitBook）。它本身也是用 Rust 写的。perf-book 之所以选择 mdBook，正是因为「写书 = 写 Markdown」，作者只需要关心内容，排版、目录、网页骨架都由 mdBook 负责。需要特别强调：mdBook 是一个**独立的可执行程序**，不是 perf-book 仓库里的一部分，所以「运行本书」的第一步就是先把它装上。

**Cargo 是什么。** Cargo 是 Rust 的包管理器与构建工具。`cargo install <包名>` 会从 crates.io（Rust 的官方包仓库）下载某个包，编译它，并把它的可执行文件放到 `~/.cargo/bin/` 下。mdBook 在 crates.io 上的包名就叫 `mdbook`，所以安装命令是 `cargo install mdbook`。

**「构建」对一本 mdBook 书意味着什么。** 对于普通 Rust 项目，「构建」是编译出二进制；对于 mdBook 书，「构建」是把 `src/` 下的 Markdown 转换成一堆 HTML、CSS、JS 文件，输出到 `book/` 目录，可以直接用浏览器打开。理解这一点，就不会把「跑 perf-book」误以为「跑某个 Rust 程序」——本书里讨论的那些 Rust 性能优化技巧，是书**内容**，而不是仓库要**运行**的代码。

## 3. 本讲源码地图

本讲只涉及三个文件，但它们正好覆盖了「安装 → 命令 → 配置 → 自动化」整条链路。

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目说明。告诉读者本书用 mdBook 构建，并列出安装、构建、预览、测试四类操作的命令。 |
| `book.toml` | mdBook 的配置文件。定义书名、作者、源目录、HTML 输出选项等，是控制「书长什么样」的总开关。 |
| `.github/workflows/ci.yml` | GitHub Actions 的 CI 配置。定义了在 push / pull request 时如何自动构建、测试本书，并在合并到 master 时部署到 GitHub Pages。 |

## 4. 核心概念与源码讲解

### 4.1 安装 mdBook 与依赖

#### 4.1.1 概念说明

mdBook 不在 perf-book 仓库内，它是外部工具。要「运行本书」，第一步必须先把它安装到本机。mdBook 本身是 Rust 写的，最通用的安装方式就是用 Cargo 从源码编译安装。装好之后，你会得到一个名为 `mdbook` 的命令行可执行文件，本书后续所有操作都靠它。

需要澄清一个常见误解：**包名和命令名都是 `mdbook`（全小写）**。`cargo install mdbook` 里的 `mdbook` 是 crates.io 上的包名，安装后产生的命令也叫 `mdbook`。不要写成 `mdBook`（中间大写 B 那个只是项目/产品的展示名）。

#### 4.1.2 核心流程

安装与验证的流程：

1. 确认本机已有 Rust 工具链（`cargo` 命令可用）。
2. 运行 `cargo install mdbook`，Cargo 会下载 mdBook 源码、编译并把 `mdbook` 二进制放进 `~/.cargo/bin/`。
3. 确保 `~/.cargo/bin` 在你的 `PATH` 中（Rust 安装时通常会自动配置）。
4. 运行 `mdbook --version` 验证安装成功。

#### 4.1.3 源码精读

README 的「Building」一节明确指出了工具来源与安装命令：

[README.md:23-27](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md#L23-L27)

这一段做了两件事：第一，说明本书用 [`mdbook`](https://github.com/rust-lang/mdBook) 构建，并指向了 mdBook 的官方仓库；第二，给出安装命令 `cargo install mdbook`。这是本讲一切操作的起点。

#### 4.1.4 代码实践

1. **实践目标**：在本机安装 mdBook 并确认可用。
2. **操作步骤**：
   - 运行 `cargo install mdbook`（首次安装会编译，耗时几分钟，请耐心等待）。
   - 安装完成后运行 `mdbook --version`。
3. **需要观察的现象**：终端逐行打印编译进度；最后 `mdbook --version` 输出形如 `mdbook vX.Y.Z` 的版本号。
4. **预期结果**：能看到版本号即说明安装成功，`mdbook` 命令已进入 `PATH`。
5. 若提示 `command not found`，检查 `~/.cargo/bin` 是否在 `PATH` 中，必要时手动 `export PATH="$HOME/.cargo/bin:$PATH"`。

> 注：`cargo install mdbook` 需要联网从 crates.io 拉取并本地编译，是否能在你本机顺利完成取决于网络与工具链环境，请以本地实际结果为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 perf-book 仓库本身不包含 mdBook，而要让你单独安装？

**参考答案**：mdBook 是构建工具，不是本书内容的一部分。把它作为外部依赖安装，可以保持仓库只存放书稿（Markdown），并且让所有人用统一、可升级的工具来渲染。

**练习 2**：安装命令里写的是 `mdbook` 而不是 `mdBook`，这两个名字分别指什么？

**参考答案**：`mdBook`（大写 B）是项目/产品的展示名称；`mdbook`（全小写）既是 crates.io 上的包名，也是安装后生成的命令名。`cargo install` 接收的是包名 `mdbook`。

---

### 4.2 mdbook build / serve / test 三个命令

#### 4.2.1 概念说明

装好 mdBook 后，操作本书主要靠三条命令，分别对应三种使用场景：

- **`mdbook build`**：一次性构建。读取 `src/` 下的 Markdown，渲染成完整的静态网站，输出到 `book/` 目录。
- **`mdbook serve`**：边写边看。启动一个本地 Web 服务器，在浏览器里实时预览本书，并且**监听文件变化**——你一改 Markdown，网页自动刷新。
- **`mdbook test`**：验证书中的代码。mdBook 会抽取书稿里嵌在代码块中的 Rust 代码并编译运行，确保示例不会写错。

这三条命令覆盖了「出成品」「本地预览」「保证示例正确」三个核心需求。

#### 4.2.2 核心流程

三条命令的关系：

```
mdbook build   →  读 src/*.md  →  生成 book/（静态 HTML 站点）
mdbook serve   →  内部先 build →  起本地服务器(默认 :3000) →  监听变更自动重建
mdbook test    →  扫描书稿代码块 →  编译/运行 Rust 示例 →  报告失败样例
```

注意 `mdbook serve` 实际上也做了 build 的工作，区别只是它额外起服务器并监听文件。所以日常写书时通常只用 `serve`；要交付静态文件或部署时才用 `build`。

#### 4.2.3 源码精读

README 的「Building」一节说明了 build 命令及其产物位置：

[README.md:28-32](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md#L28-L32)

这里明确：构建命令是 `mdbook build`，生成的文件放在 `book/` 目录。

「Development」一节说明了 serve 命令、访问地址以及热更新特性：

[README.md:36-43](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md#L36-L43)

关键信息有三：① `mdbook serve` 启动本地服务器；② 在浏览器访问 `localhost:3000` 查看；③ 文件改动时网页会自动更新。

紧随其后是 test 命令：

[README.md:45-48](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md#L45-L48)

这条命令用来测试书中的代码，是保证书稿里 Rust 示例始终可编译的护栏。

#### 4.2.4 代码实践

1. **实践目标**：完成本书的构建、本地预览，并跑一次代码测试。
2. **操作步骤**：
   - 在仓库根目录运行 `mdbook build`，构建完成后查看是否生成了 `book/` 目录（里面有 `index.html` 等）。
   - 运行 `mdbook serve`，然后在浏览器打开 `http://localhost:3000`。
   - 保持 `serve` 运行，随便打开 `src/` 下任一 `.md` 文件改动一个字保存，回到浏览器观察是否自动刷新。
   - 另开一个终端运行 `mdbook test`，观察它编译书稿代码块的过程。
3. **需要观察的现象**：`build` 后 `book/` 出现；`serve` 后浏览器显示完整带目录的网页；改文件后网页自动刷新；`test` 逐个编译代码块并最终无报错。
4. **预期结果**：三条命令均正常执行，浏览器能看到渲染好的《Rust Performance Book》。
5. 若 `serve` 提示端口占用，可用 `mdbook serve -p <其他端口>` 指定端口；具体行为待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`mdbook build` 和 `mdbook serve` 都会生成网页，它们的核心区别是什么？

**参考答案**：`build` 只生成静态文件到 `book/` 然后退出；`serve` 在生成后还启动一个本地 Web 服务器并持续监听 `src/` 变化，文件一改就自动重建并刷新浏览器，适合边写边看。

**练习 2**：既然书稿都是 Markdown，`mdbook test` 到底在测什么？

**参考答案**：它抽取书稿代码块中标记为 Rust 的代码，编译并（在有 `assert` 等可执行内容时）运行它们，确保书里给出的 Rust 示例能够通过编译、行为正确，防止示例随 Rust 版本演进而失效。

---

### 4.3 book.toml 的关键配置项

#### 4.3.1 概念说明

`book.toml` 是 mdBook 的配置文件，位于仓库根目录，是控制「这本书叫什么、内容在哪、网页长什么样」的总开关。mdBook 在 `build`/`serve` 时会读取它。它用 TOML 格式（键值对 + 分节），即使你没学过 TOML 也能一眼看懂。

理解 `book.toml` 的关键，是把它分成几个「节（section）」来看：

- `[book]`：书的元信息（书名、作者、源目录、语言）。
- `[build]`：构建行为开关。
- `[rust]`：书中代码示例的 Rust 设置。
- `[output.html]`：HTML 输出的外观与功能选项。

#### 4.3.2 核心流程

mdBook 构建时读取配置的逻辑大致如下：

```
读取 book.toml
  ├─ [book].src        → 确定源 Markdown 在哪个目录（默认 src）
  ├─ [build]           → 决定是否自动创建缺失文件等行为
  ├─ [rust]            → 决定书稿代码块按哪个 edition 编译
  └─ [output.html]     → 决定网页主题、编辑链接、站点地址等
        ↓
渲染 src/SUMMARY.md 决定的章节  →  输出到 book/
```

其中 `src` 指向的内容目录、`SUMMARY.md` 决定的章节顺序，是「书的内容从哪来」；`[output.*]` 决定「输出成什么格式、长什么样」。

#### 4.3.3 源码精读

`[book]` 节定义了书的基本身份与源目录：

[book.toml:1-5](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L1-L5)

逐项含义：`title` 是书名 *The Rust Performance Book*；`authors` 是作者 Nicholas Nethercote；`src = "src"` 告诉 mdBook 从 `src/` 目录读取 Markdown；`language = "en"` 是内容语言。

`[build]` 节控制构建行为：

[book.toml:7-8](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L7-L8)

`create-missing = false` 很关键：当 `SUMMARY.md` 引用了某个尚不存在的 `.md` 文件时，mdBook **不会**自动创建空文件，而是报错。这能避免误把空章节混进书里。

`[rust]` 节设置书稿代码示例的编译选项：

[book.toml:10-11](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L10-L11)

`edition = "2018"` 指明书中的 Rust 代码示例按 2018 edition 编译（`mdbook test` 时生效）。

`[output.html]` 节是网页外观与功能的核心配置：

[book.toml:13-18](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L13-L18)

逐项含义：`smart-punctuation = true` 开启智能标点（自动把直引号转成弯引号等）；`default-theme = "rust"` 设置默认主题为「rust」配色；`git-repository-url` 让网页右上角显示指向 GitHub 仓库的链接；`edit-url-template` 提供「编辑此页」的链接模板（点开后直接跳到 GitHub 编辑界面）；`site-url = "https://nnethercote.github.io/perf-book/"` 是部署后的站点地址，mdBook 用它来生成正确的相对/绝对链接。

配置文件末尾还保留了被注释掉的 ePub 输出配置：

[book.toml:20-24](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L20-L24)

这段 `#[output.epub]` 被注释掉了，说明电子书（ePub）输出当前处于**禁用**状态，原因是 README 中提到的 ePub 生成存在格式问题。

#### 4.3.4 代码实践

1. **实践目标**：逐字段理解 `book.toml`，并通过改动观察效果。
2. **操作步骤**：
   - 对照上面的源码精读，在 `book.toml` 里找到书名、作者、源目录、默认主题这四项。
   - 运行 `mdbook serve` 预览本书。
   - 把 `default-theme` 的值从 `"rust"` 临时改成 `"light"`（或 `"ayu"`），保存，观察浏览器主题是否变化（页面顶部主题选择器/默认配色应随之改变）。
   - 改完**务必还原**，不要污染仓库。
3. **需要观察的现象**：修改 `default-theme` 后，网页刷新，整体配色变化。
4. **预期结果**：确认 `[output.html].default-theme` 确实控制默认外观；还原后一切如初。
5. 若想验证 `create-missing = false`，可在 `src/SUMMARY.md` 里临时加一条指向不存在文件的目录项再 `mdbook build`，应看到报错而非自动建文件；实验后还原。

#### 4.3.5 小练习与答案

**练习 1**：如果你在 `SUMMARY.md` 里写了一个指向 `src/new-chapter.md` 的条目，但忘了创建该文件，运行 `mdbook build` 会发生什么？为什么？

**参考答案**：会报错，而不是自动创建空文件。因为 `book.toml` 里 `[build].create-missing = false` 关闭了「自动补建缺失文件」的行为，mdBook 会把缺失文件当作错误抛出，避免空章节混入书中。

**练习 2**：`[output.html]` 里的 `edit-url-template` 对读者有什么实际用处？

**参考答案**：它在每个页面生成一个「编辑此页」链接，点开后跳转到 GitHub 上对应 Markdown 文件的编辑界面，方便读者发现笔误时快速提议修改（结合本书「偏好 issue 而非 PR」的协作方式）。

**练习 3**：`[rust].edition = "2018"` 主要影响哪条命令的效果？

**参考答案**：主要影响 `mdbook test`——它决定书稿中 Rust 代码块按哪个 edition 编译；同时也影响书中可运行的代码示例如何被理解。

---

### 4.4 CI 如何构建与部署

#### 4.4.1 概念说明

perf-book 的线上版本（GitHub Pages 上的网页）不是手动发布的，而是由 **GitHub Actions** 在每次代码变更时自动构建并部署的。GitHub Actions 是 GitHub 内置的持续集成（CI）服务，配置文件放在仓库的 `.github/workflows/` 目录下，用 YAML 描述「在什么时机、跑哪些步骤」。

perf-book 的 CI 文件叫 `.github/workflows/ci.yml`，它定义了一个名为 `CI` 的工作流，核心做三件事：构建（`mdbook build`）、测试（`mdbook test`）、部署（deploy 到 GitHub Pages）。理解它能帮你看懂「我改了书稿后，线上是怎么自动更新的」。

#### 4.4.2 核心流程

CI 工作流的执行逻辑：

```
触发：pull_request（任意分支） 或 push 到 master
        ↓
单 job：test_and_maybe_deploy（ubuntu-latest）
        ↓
步骤：
  1. checkout 拉取仓库
  2. 用 peaceiris/actions-mdbook 安装 mdbook
  3. mdbook build     ← 构建
  4. mdbook test      ← 测试书稿代码
  5. 部署到 GitHub Pages  ← 仅当：push 到 master 且仓库为 nnethercote/perf-book
```

关键点在于第 5 步的**条件判断**：构建和测试在 pull request 时也会跑（用来把关 PR 质量），但「部署到线上」只在 push 到 master 且确为本仓库时才执行——这样既能保护发布通道，又不会在 fork 或 PR 上误发。

#### 4.4.3 源码精读

工作流的触发条件定义了「什么时候跑」：

[.github/workflows/ci.yml:1-7](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.github/workflows/ci.yml#L1-L7)

工作流名 `CI`；`on` 块说明它在两种情况触发：一是任意 `pull_request`，二是向 `master` 分支的 `push`。

构建与测试之前，CI 先用第三方 Action 安装 mdBook：

[.github/workflows/ci.yml:16-19](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.github/workflows/ci.yml#L16-L19)

这里用 `peaceiris/actions-mdbook@v2` 这个社区 Action 来安装 mdBook（而不是本机 `cargo install`），并指定 `mdbook-version: "latest"`。这正是 CI 与本地「安装」方式的不同之处——CI 在干净的虚拟环境里跑，需要一个独立的安装步骤。

随后是构建与测试两个核心步骤：

[.github/workflows/ci.yml:27-31](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.github/workflows/ci.yml#L27-L31)

可以看到 CI 里执行的命令和本地完全一致：`mdbook build` 与 `mdbook test`。这说明本书的「正确性标准」是统一的——本地能过，CI 也用同样命令把关。

最后是部署步骤及其触发条件：

[.github/workflows/ci.yml:37-44](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.github/workflows/ci.yml#L37-L44)

部署用 `peaceiris/actions-gh-pages@v4` 把 `./book` 目录发布到 GitHub Pages。注意末尾的 `if:` 条件：`github.event_name == 'push' && github.ref == 'refs/heads/master' && github.repository == 'nnethercote/perf-book'`。这三个条件「同时」满足才部署——必须是 push（而非 PR）、必须是 master 分支、必须是本仓库（防止 fork 误发）。`publish_dir: ./book` 也呼应了 README 里「构建产物放在 `book/`」的说法。

#### 4.4.4 代码实践

1. **实践目标**：看懂 CI 是如何把本地命令搬上自动化流水线的。
2. **操作步骤**：
   - 打开 `.github/workflows/ci.yml`，对照上面的源码精读，把 5 个步骤（checkout → setup mdbook → build → test → deploy）依次标注出来。
   - 在本地分别运行 `mdbook build` 和 `mdbook test`，对比它们与 CI 第 3、4 步是否完全一致。
   - 思考：为什么 deploy 步骤要加 `github.repository == 'nnethercote/perf-book'` 这一条件？
3. **需要观察的现象**：本地命令与 CI 命令逐字对应；deploy 条件三要素清晰可辨。
4. **预期结果**：你能用自己的话解释「PR 时会构建+测试但不会部署，合并到 master 才会部署」这一流程。
5. 若想观察真实运行，可在 GitHub 仓库的 **Actions** 标签页查看历史 CI 运行日志（需要仓库访问权限），具体可见性待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 CI 里要先有一个「Setup mdbook」步骤，而本地只需装一次？

**参考答案**：CI 每次运行都在全新的虚拟环境里，没有预装 mdBook，所以每次都必须重新安装；本地机器装一次后 `mdbook` 一直在 `PATH` 里，能反复使用。

**练习 2**：如果有人 fork 了 perf-book 并向自己 fork 的 master 分支推送改动，CI 会把书部署到 fork 的 GitHub Pages 吗？为什么？

**参考答案**：不会。deploy 步骤的 `if` 条件里有 `github.repository == 'nnethercote/perf-book'`，fork 的仓库名不匹配，部署被跳过，从而避免 fork 误用本仓库的发布通道。

**练习 3**：CI 的 build/test 步骤用的是 `mdbook build` / `mdbook test`，这与 README 给本地用户的命令是否一致？这反映了什么设计思想？

**参考答案**：完全一致。这反映「本地与 CI 用同一套命令、同一套正确性标准」的思想——本地能跑通的构建，CI 用同样的方式把关，保证结果可复现。

---

## 5. 综合实践

**任务**：从零把 perf-book 在本地「跑通」，并对照配置解释每一处行为。

1. 运行 `cargo install mdbook` 安装工具，用 `mdbook --version` 确认。
2. 在仓库根目录依次运行 `mdbook build`、`mdbook serve`、`mdbook test`，记录每条命令的输出与产物：
   - `build` 产生了 `book/` 目录里的哪些文件？
   - `serve` 在 `localhost:3000` 显示了什么？改一处 Markdown 后是否自动刷新？
   - `test` 编译了哪些代码块，有没有失败？
3. 打开 `book.toml`，对照本讲 4.3 节，逐字段写出 `[book]`、`[build]`、`[rust]`、`[output.html]` 各项的作用。
4. 打开 `.github/workflows/ci.yml`，画出「触发 → checkout → setup mdbook → build → test → deploy（有条件）」的流程图，并解释 deploy 的三个 `if` 条件。
5. **进阶**：临时把 `book.toml` 的 `create-missing` 改为 `true`，在 `SUMMARY.md` 加一条指向不存在文件的条目，运行 `mdbook build`，观察 mdBook 是否自动创建了空文件；实验后**务必还原所有改动**。

完成这个综合实践后，你就掌握了「安装工具 → 三条命令 → 配置文件 → CI 自动化」这条完整的构建链路。

## 6. 本讲小结

- mdBook 是把 Markdown 渲染成在线书的外部工具，用 `cargo install mdbook` 安装，命令名是全小写 `mdbook`。
- 三条核心命令分工明确：`mdbook build` 出静态成品到 `book/`，`mdbook serve` 起本地服务器（`localhost:3000`）并热更新，`mdbook test` 验证书中 Rust 代码。
- `book.toml` 是总配置：`[book]` 管元信息与源目录、`[build]` 管 `create-missing` 等行为、`[rust]` 管代码 edition、`[output.html]` 管网页外观与编辑链接。
- CI（`.github/workflows/ci.yml`）用与本地完全相同的 `mdbook build`/`mdbook test` 把关质量，并在 push 到 master 且为本仓库时才部署到 GitHub Pages。
- 「运行本书」不是「运行 Rust 程序」，而是「构建并预览一本 Markdown 书」——本书里的 Rust 性能技巧是内容，不是要跑的代码。

## 7. 下一步学习建议

到这里你已经能让 perf-book 在本地跑起来，也理解了它的构建与发布机制。接下来建议：

1. **下一篇（u1-l3）仓库结构与章节组织**：深入 `src/SUMMARY.md` 如何决定目录顺序、`src/` 下各章节文件分别讲什么，学会快速定位任意性能主题所在的文件。
2. **动手扩充本书**：尝试按 `edit-url-template` 的指引，理解一次「新增章节」需要改动 `SUMMARY.md` 与新增 `.md` 文件（注意 `create-missing = false`）。
3. **进入性能内容**：在熟悉构建流程后，第二单元将从「基准测试（benchmarking）」开始，正式进入「先测量、再优化」的性能优化工作流，那时 mdBook 只是你阅读《Rust 性能手册》的工具，注意力将转向 Rust 代码本身。
