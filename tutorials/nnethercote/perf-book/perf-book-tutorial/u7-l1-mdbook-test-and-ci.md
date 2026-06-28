# mdBook 进阶——测试、CI 部署与扩展

## 1. 本讲目标

本讲是「维护与贡献」单元的第一篇，承接 u1-l2（构建与运行）把视角从「能在本地跑起来」提升到「如何保证书稿正确、如何自动部署、以及 mdBook 还能怎么扩展」。读完本讲你应当能够：

- 理解 `mdbook test` 是什么：它能抽出书稿里的 Rust 代码块并真正编译、运行，从而把「书」变成「可测试的文档」。
- 读懂 `.github/workflows/ci.yml` 这条流水线，说清 clone → setup mdbook → build → test → deploy 这五步分别在做什么。
- 解释 deploy 步骤为什么带一道三重条件门控（`push` 到 `master` 且仓库为 `nnethercote/perf-book`）。
- 了解 mdBook 的三类扩展点：preprocessor（预处理）、renderer/backend（输出后端）、theme（主题），并能从 `book.toml` 里认出它们。

本讲依然守 perf-book 的核心约束：**「源码即 Markdown」**——本讲所谓的「源码」是 `book.toml`、`src/` 下的书稿和 CI 配置，而不是某个 Rust 库的代码。

## 2. 前置知识

本讲默认你已经读过 u1-l2 与 u1-l3，掌握了以下事实：

- **mdBook** 是把 `src/` 下的 Markdown 渲染成在线书的工具，三条核心命令是 `mdbook build` / `mdbook serve` / `mdbook test`。
- `book.toml` 是 mdBook 的总配置文件，`src/SUMMARY.md` 是全书目录的单一事实来源。
- `book/` 是 `mdbook build` 的产物，被 `.gitignore` 忽略。

本讲会引入几个工程类概念，先用一句话解释清楚：

- **CI（持续集成，Continuous Integration）**：每次提交或发起 PR 时，由托管平台（这里是 GitHub）在一台干净的服务器上自动跑一遍构建与测试，确保改动没有破坏书稿。
- **CD（持续部署，Continuous Deployment）/ GitHub Pages**：构建产物（`book/` 目录里的 HTML）自动发布成一个公开网站，perf-book 的在线版 https://nnethercote.github.io/perf-book/ 就是这样发布的。
- **GitHub Actions**：GitHub 内置的 CI/CD 引擎，流水线写在 `.github/workflows/*.yml` 里，push 代码时自动触发。
- **文档测试（doctest）**：Rust 工具链里 `rustdoc` 的能力——把文档注释/Markdown 里的代码块抽出来当成测试编译运行。`mdbook test` 正是复用了这一能力。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [.github/workflows/ci.yml](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.github/workflows/ci.yml) | CI/CD 流水线定义：什么时候触发、怎么构建/测试/部署 |
| [README.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/README.md) | 面向贡献者的操作说明：构建、预览、测试、贡献方式 |
| [book.toml](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml) | mdBook 总配置：书名、源目录、构建开关、HTML 输出选项 |
| [src/io.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md) | 第 16 章 I/O，提供「带隐藏行的可测试代码块」实例 |
| [src/build-configuration.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md) | 第 3 章 Build Configuration，提供「`rust,ignore` 跳过测试」实例 |

## 4. 核心概念与源码讲解

### 4.1 mdbook test：把书稿变成可执行的测试

#### 4.1.1 概念说明

perf-book 不是可运行的库，但它的书稿里散布着大量 Rust 代码片段（教程示例、改进前后的对比代码）。这些代码如果只是「写在那里」，就存在一个风险：**随着时间推移、Rust 版本升级，书里的示例可能会悄悄编译不过**，而读者照抄就会踩坑。

`mdbook test` 就是为了消除这个风险。它会扫描全书 Markdown，把每一个被标记为 Rust 的代码块抽出来，逐个编译（必要时运行），只要有一个编译失败或断言失败，整个命令就以非零状态码退出。于是：

- 它把「书稿示例」从「看起来对」升级为「机器保证对」。
- 它是 CI 守门的第二道闸（`mdbook build` 验证结构完整，`mdbook test` 验证代码正确）。

#### 4.1.2 核心流程

`mdbook test` 的执行过程可以概括为：

1. 读取 `src/SUMMARY.md`，按目录顺序收集所有章节 `.md` 文件。
2. 对每个文件，抽取所有 ` ```rust `（或带属性）的代码块。
3. 把这些代码块交给 Rust 工具链以「文档测试（doctest）」的方式编译、运行——这与 `rustdoc --test` 处理代码块的机制一致。
4. 任何一个代码块编译/运行失败，命令失败；全部通过则成功。

代码块的「属性」沿用 rustdoc 的约定，perf-book 里实际用到的有两种：

- **` ```rust `**：会被编译（必要时运行）。这是默认形态。
- **` ```rust,ignore `**：显式跳过，不参与测试。用于「单独看是对的、但抽出来无法独立编译」的片段（例如需要一个外部 crate 依赖、或只是某个函数内部的片段）。

此外有一个关键的语法糖：**以 `# ` 开头的行会被渲染时隐藏，但仍会参与编译**。这让你能在书里展示「干净的核心代码」，同时在背后补上让它真正能编译的脚手架（变量声明、函数包裹等）。mdBook 复用了 rustdoc 的这一行为。

#### 4.1.3 源码精读

先看一个**会被测试**的代码块，来自第 16 章 I/O「Locking」一节：

[代码块 src/io.md:L12-L17](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L12-L17) —— 一个带 `# ` 隐藏行的 Rust 代码块。

```rust
# let lines = vec!["one", "two", "three"];
for line in lines {
    println!("{}", line);
}
```

这里的第 13 行 `# let lines = ...` 以 `# ` 开头：

- **渲染成网页时**它被隐藏，读者只看到干净的 `for line in lines { println!(...) }`。
- **`mdbook test` 时**它参与编译——正是这行让片段拥有可遍历的 `lines`，从而能通过编译。没有它，`lines` 就是未定义的，测试会失败。

紧接着的对比代码块同样用了这个手法，而且隐藏行更多，把片段完整包进了一个函数：

[代码块 src/io.md:L19-L31](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L19-L31) —— 用 `# ` 把片段包进 `fn blah() -> Result<(), std::io::Error> { ... }`。

```rust
# fn blah() -> Result<(), std::io::Error> {
# let lines = vec!["one", "two", "three"];
use std::io::Write;
let mut stdout = std::io::stdout();
let mut lock = stdout.lock();
for line in lines {
    writeln!(lock, "{}", line)?;
}
// stdout is unlocked when `lock` is dropped
# Ok(())
# }
```

读者在网页上只看到中间那几行核心代码，但 `mdbook test` 看到的是一个带 `?` 的、必须放进 `-> Result<...>` 函数体里才能编译的完整片段——隐藏行恰好提供了这个函数外壳。

再看一个**显式跳过测试**的例子，来自第 3 章 Build Configuration「jemalloc」一节：

[代码块 src/build-configuration.md:L153-L156](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L153-L156) —— 标了 `ignore`、不参与测试的片段。

```rust,ignore
#[global_allocator]
static GLOBAL: tikv_jemallocator::Jemalloc = tikv_jemallocator::Jemalloc;
```

为什么这段要 `ignore`？因为它用到了 `tikv_jemallocator` 这个外部 crate——而 perf-book 的书稿不是 Cargo 项目，`mdbook test` 在编译这段时根本找不到这个依赖，必然失败。作者用 `rust,ignore` 如实告诉读者「这段只是示意，请在你自己的 `Cargo.toml` 加依赖后再用」，同时让 CI 不必为它报错。同章 mimalloc 的片段也是同样处理：

[代码块 src/build-configuration.md:L185-L188](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L185-L188) —— 同样标 `ignore` 的 mimalloc 全局分配器片段。

> 小结：写书稿示例时有一条不成文的纪律——**能独立编译的片段用 ` ```rust ` 让 CI 替你把关，需要外部依赖或只是局部片段的用 ` ```rust,ignore ` 显式豁免**。不要让一段「看起来完整、实则编译不过」的代码裸奔进 ` ```rust `。

#### 4.1.4 代码实践

**实践目标**：亲手感受 `mdbook test` 如何编译验证书中代码，并观察 `# ` 隐藏行与 `ignore` 的区别。

**操作步骤**：

1. 按本书 u1-l2 的方式安装 mdBook（`cargo install mdbook`），并确认 `mdbook --version` 可用。
2. 在仓库根目录运行：

   ```bash
   mdbook test
   ```

3. 观察输出：它会逐章报告代码块的编译情况。
4. 做一个小破坏实验：临时把 `src/io.md` 第 13 行的 `# ` 前缀去掉，让那行变成普通可见代码（注意此时它仍是合法的 `let`，单独看仍能编译，所以测试可能仍通过）。再做一个更明显的破坏：在任意一个 ` ```rust ` 代码块里加一行语法错误的代码，例如 `let x = ;`，再次运行 `mdbook test`。

**需要观察的现象**：

- 正常运行时，命令应以「成功/无错误」结束。
- 引入语法错误后，`mdbook test` 应当报错，并指出出错的是哪个文件的哪一段代码，命令以非零状态码退出。

**预期结果**：

- `mdbook test` 能精确定位到出错的代码块，这正是 CI 里用它守门的价值——书稿示例一旦写错，CI 立刻拦下。

> 注意：本实践会临时改动书稿源码。完成后请用 `git checkout src/io.md` 等命令还原，不要把破坏性改动留下。**待本地验证**：具体输出文案随 mdBook 版本略有差异，但「报错并定位文件」的行为是稳定的。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 Rust 代码片段需要一个外部 crate 才能编译，作者应该把它写成 ` ```rust ` 还是 ` ```rust,ignore `？为什么？

> **参考答案**：写成 ` ```rust,ignore `。因为 perf-book 的书稿不是 Cargo 项目，`mdbook test` 编译该片段时找不到那个 crate 会失败。标 `ignore` 既如实告知读者「这需要自行加依赖」，又让 CI 不必为它报错。

**练习 2**：`# let lines = vec![...];` 这行以 `# ` 开头，它在「渲染网页」和「mdbook test」两种语境下分别如何被对待？

> **参考答案**：渲染网页时它被**隐藏**（读者看不到）；`mdbook test` 时它**照常参与编译**。这个语法糖让作者既能展示干净的核心代码，又能补上让片段真正可编译的脚手架。

**练习 3**：为什么 perf-book 选择用 `mdbook test` 而不是单独写一套单元测试来验证示例？

> **参考答案**：示例直接写在书稿里，`mdbook test` 复用 rustdoc 的文档测试能力，就地抽取代码块编译，保证「读者看到的代码」与「被验证的代码」是同一份，避免了示例与测试两套代码不同步的风险。

---

### 4.2 CI 流水线解析：build / test / deploy

#### 4.2.1 概念说明

CI 流水线（pipeline）是一份「清单」：告诉 GitHub Actions 在什么时机、按什么顺序、在什么环境里执行哪些步骤。perf-book 的流水线极简但结构完整——它把「本地三件套 `mdbook build/serve/test`」里的两条（build、test）搬上 CI，再额外加一个条件式的 deploy 步骤。理解它的价值在于：

- **本地与 CI 同构**：CI 跑的命令和你在本地跑的一模一样，这意味着「在我电脑上能过」与「在 CI 上能过」基本等价，不会出现环境差异的惊喜。
- **两道闸门**：`build` 守结构（SUMMARY.md 引用的文件都在、Markdown 合法），`test` 守正确性（代码块能编译）。

#### 4.2.2 核心流程

整条流水线的逻辑流程：

```
触发（pull_request 或 push 到 master）
        │
        ▼
  job: test_and_maybe_deploy（ubuntu-latest）
        │
        ├─ 1. checkout 仓库代码
        ├─ 2. 安装 mdBook（用第三方 action peaceiris/actions-mdbook）
        ├─ 3. mdbook build   ← 结构闸门（总是运行）
        ├─ 4. mdbook test    ← 正确性闸门（总是运行）
        └─ 5. 部署到 GitHub Pages ← 仅满足三重条件才运行
```

注意第 5 步与前三步的关系：**build 和 test 总是运行（无论 PR 还是 push），deploy 带条件**。这一点在 4.3 节展开。

#### 4.2.3 源码精读

先看触发条件：

[ci.yml:L1-L7 — 流水线名与触发时机](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.github/workflows/ci.yml#L1-L7)

```yaml
name: CI

on:
  pull_request:
  push:
    branches:
      - master
```

这段定义了两类触发：`pull_request`（有人发起 PR 时）和 `push` 到 `master` 分支（合入主干时）。也就是说，**任何 PR 和任何对 master 的 push 都会跑 build/test**——前者用于在合并前把关，后者用于在合并后持续验证并触发部署。

再看 job 与运行环境：

[ci.yml:L9-L14 — 单个 job 与第一步 checkout](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.github/workflows/ci.yml#L9-L14)

```yaml
jobs:
  test_and_maybe_deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Clone repository
        uses: actions/checkout@v4
```

整条流水线只有一个 job `test_and_maybe_deploy`，跑在最新的 Ubuntu 镜像上。第一步用官方的 `actions/checkout@v4` 把仓库代码克隆到 CI 机器——没有代码，后续什么都做不了。

接着是安装 mdBook：

[ci.yml:L16-L19 — 用第三方 action 安装 mdBook](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.github/workflows/ci.yml#L16-L19)

```yaml
      - name: Setup mdbook
        uses: peaceiris/actions-mdbook@v2
        with:
          mdbook-version: "latest"
```

这里**没有**用 README 里推荐的 `cargo install mdbook`，而是用了社区维护的 [`peaceiris/actions-mdbook`](https://github.com/peaceiris/actions-mdbook) 这个 action。原因很实际：`cargo install` 要从源码编译 mdBook，较慢；而这个 action 直接下载预编译二进制，更快、更省 CI 额度。注意它固定用 `mdbook-version: "latest"`——好处是总能用上新版，代价是构建不完全可复现（某次 mdBook 升级若引入不兼容，CI 可能突然失败）。

然后是两道核心闸门：

[ci.yml:L27-L31 — build 与 test 两个步骤](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.github/workflows/ci.yml#L27-L31)

```yaml
      - name: Build
        run: mdbook build

      - name: Test
        run: mdbook test
```

两条命令与本地完全一致。`mdbook build` 会校验整本书的结构（例如 `book.toml` 里 `create-missing = false` 决定了 SUMMARY.md 引用不存在的文件时直接报错，参见 u1-l3），并生成 `book/` 目录；`mdbook test` 则在 build 成功后校验所有 Rust 代码块（参见 4.1 节）。两者**总是运行**，不分 PR 还是 push。

最后看 deploy 步骤的「外壳」：

[ci.yml:L37-L42 — deploy 步骤用第三方 action 发布](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.github/workflows/ci.yml#L37-L42)

```yaml
      - name: Deploy
        uses: peaceiris/actions-gh-pages@v4
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          #publish_dir: ./book/html  # use if EPUB is enabled
          publish_dir: ./book # use if EPUB is disabled
```

这一步用 [`peaceiris/actions-gh-pages`](https://github.com/peaceiris/actions-gh-pages) 把 `publish_dir: ./book` 里的产物推送到一个 `gh-pages` 分支，GitHub Pages 再从这个分支发布成网站。`${{ secrets.GITHUB_TOKEN }}` 是 GitHub 自动注入的令牌，让 action 有权往仓库推分支。注意 `publish_dir` 那行注释：当前 EPUB 后端被禁用（见 4.4 节），所以直接发布整个 `book/`；若启用 EPUB，则要改成只发布 `./book/html`。

#### 4.2.4 代码实践

**实践目标**：把 ci.yml 的每一步与本地命令对应起来，建立「CI 就是自动化的本地流程」的直觉。

**操作步骤**：

1. 在仓库根目录依次手动执行 ci.yml 里的核心命令，模拟 CI：

   ```bash
   mdbook build
   mdbook test
   ls book/        # 查看构建产物
   ```
2. 对照 `.github/workflows/ci.yml` 第 27–31 行，确认你本地跑的就是 CI 的第 3、4 步。
3. 故意制造一个 CI 会拦下的错误：在 `src/SUMMARY.md` 临时加一行指向一个**不存在**的文件（例如 `- [不存在](nonexistent.md)`），然后跑 `mdbook build`。

**需要观察的现象**：

- 第 3 步因 `book.toml` 的 `create-missing = false`（[book.toml:L7-L8](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L7-L8)）应当直接报错——这正是 CI 的 `mdbook build` 步骤会拦截的同类问题。

**预期结果**：

- 你能复现 CI 的 build 闸门所拦截的「幽灵条目」错误，从而理解为何作者要把 `create-missing` 设为 `false`：让错误在本地/CI 当场暴露，而不是悄悄生成空页面。

> 完成后请 `git checkout src/SUMMARY.md` 还原。**待本地验证**：错误信息的具体措辞以本地 mdBook 版本为准。

#### 4.2.5 小练习与答案

**练习 1**：CI 安装 mdBook 用的是 `peaceiris/actions-mdbook` 而不是 `cargo install mdbook`，主要好处是什么？

> **参考答案**：下载预编译二进制比从源码 `cargo install` 编译更快、更省 CI 时间与额度；同时把「安装某工具」封装成一个可复用的 action，配置更简洁。

**练习 2**：流水线里 `mdbook build` 和 `mdbook test` 谁先跑？如果 build 失败，test 还会跑吗？

> **参考答案**：build 在前、test 在后。在 GitHub Actions 的同一 job 内，默认前面的 step 失败后后面的 step 不会执行——所以 build 失败时 test 不会跑。这个顺序合理：连书都构建不出来，谈代码块编译为时尚早。

**练习 3**：为什么说「CI 跑的命令和本地一样」是一个值得珍惜的性质？

> **参考答案**：它消除了「本地能过、CI 不能过」的环境差异盲区，让作者在本地复现 CI 行为成为可能，调试成本低、信任度高。perf-book 正是这么做的——CI 用的就是 README 推荐的那两条 `mdbook` 命令。

---

### 4.3 GitHub Pages 部署的条件门控

#### 4.3.1 概念说明

`deploy` 是整条流水线里唯一「有副作用、面向外」的步骤——它会把产物推成公开网站。与 build/test 不同，部署是不可随意触发的：你不希望别人提的 PR 就把网站改掉，也不希望 fork 出去的副本意外部署到作者名下。因此 deploy 步骤挂了一道**条件门控（conditional gate）**：只有三个条件同时满足才执行。这体现了一条通用工程原则——**对有外部副作用的动作，默认关闭、显式开启**。

#### 4.3.2 核心流程

deploy 的执行判定可以写成伪代码：

```
if ( 事件类型 == push            // 不是 PR
     and 目标分支 == master       // 推到主干
     and 仓库全名 == nnethercote/perf-book )  // 是正本仓库
then
    执行 deploy
else
    跳过 deploy（但 build/test 仍照常跑过）
end
```

三个条件缺一不可，合在一起表达的意思是：**「只有正本仓库的 master 分支被直接推送时，才发布网站。」**

#### 4.3.3 源码精读

条件门控就挂在 deploy 步骤的 `if` 字段上，与步骤本体同属一个 YAML 块：

[ci.yml:L37-L44 — deploy 步骤及其三重 if 条件](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.github/workflows/ci.yml#L37-L44)

```yaml
      - name: Deploy
        uses: peaceiris/actions-gh-pages@v4
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          #publish_dir: ./book/html  # use if EPUB is enabled
          publish_dir: ./book # use if EPUB is disabled
        # Only deploy on a push to master, not on a pull request.
        if: github.event_name == 'push' && github.ref == 'refs/heads/master' && github.repository == 'nnethercote/perf-book'
```

注意这里的细节：

- `github.event_name == 'push'` —— 排除 `pull_request`。这样别人发 PR 时 CI 仍会 build/test 验证，但**不会部署**，网站不会被 PR 改动。
- `github.ref == 'refs/heads/master'` —— 只在推到 master 时部署。其他分支的 push 不部署。
- `github.repository == 'nnethercote/perf-book'` —— 只在正本仓库部署。这一点对公开模板/可 fork 项目尤为重要：任何人都可以 fork 这本书到自己的账号，但 fork 的 CI 即使满足前两条也不应往作者控制的 GitHub Pages 推——这条把部署权限锁死在正本仓库。

> 顺带一提：`deploy` 这个 step 的 `if` 是**步骤级**条件，而非 job 级。也就是说，build 和 test 这两个 step **没有** `if`，所以它们在每次触发（含 PR）时都会跑；只有 deploy 受门控约束。这正好实现了「PR 验证但不发布、master 推送才发布」的分工。

这套部署最终落到哪里？`book.toml` 里的 `site-url` 给出了发布地址：

[book.toml:L13-L18 — HTML 输出配置含 site-url](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L13-L18)

```toml
[output.html]
smart-punctuation = true
default-theme = "rust"
git-repository-url = "https://github.com/nnethercote/perf-book"
edit-url-template = "https://github.com/nnethercote/perf-book/edit/master/{path}"
site-url = "https://nnethercote.github.io/perf-book/"
```

`site-url = "https://nnethercote.github.io/perf-book/"` 告诉 mdBook「这本书最终会托管在这个 URL 下」，从而让生成的 HTML 里的资源路径、sitemap 等使用正确的前缀。它与 ci.yml 里 `peaceiris/actions-gh-pages` 推到 `gh-pages` 分支、GitHub Pages 自动发布这一链条配合，共同构成「push 到 master → 自动出现在该 URL」的闭环。

#### 4.3.4 代码实践

**实践目标**：通过推理（无需真去 fork）理解三重条件的每一条在防什么。

**操作步骤**：

1. 重新阅读 [ci.yml:L44](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.github/workflows/ci.yml#L44) 的 `if` 表达式，把它拆成三个布尔子条件。
2. 针对下面三种「假想场景」，判断 deploy 是否会执行（build/test 不受影响，始终会跑）：
   - **场景 A**：有人在 `nnethercote/perf-book` 发起一个 PR。
   - **场景 B**：作者直接 push 到 `nnethercote/perf-book` 的 master 分支。
   - **场景 C**：某人 fork 了仓库到 `someone/perf-book`，并 push 到自己 fork 的 master 分支。

**需要观察的现象 / 预期结果**：

| 场景 | event_name | ref | repository | deploy？ |
|------|------------|-----|------------|----------|
| A：正本的 PR | `pull_request` | — | 正本 | ❌（被 `event_name` 拦） |
| B：正本 push master | `push` | `refs/heads/master` | 正本 | ✅ |
| C：fork push master | `push` | `refs/heads/master` | `someone/perf-book` | ❌（被 `repository` 拦） |

结论：只有场景 B 会真正发布网站。

> **说明**：本实践为「源码阅读 + 推理」型，无需运行命令；若想实地验证，最安全的做法是在自己 fork 的仓库上开启 GitHub Pages 并观察——你会看到 build/test 通过但 deploy 被跳过（因 `repository` 条件不满足）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：去掉 `github.repository == 'nnethercote/perf-book'` 这个条件会有什么潜在问题？

> **参考答案**：任何 fork 本仓库的人在 push 到自己 fork 的 master 时，其 CI 都会尝试部署——虽然部署到的是 fork 自己的 GitHub Pages、不会污染正本，但会浪费 fork 用户的 CI 额度，也可能在缺少权限时报错噪音。保留这条把部署行为严格限定在正本仓库。

**练习 2**：为什么 build 和 test 步骤**不**加这个 `if`？

> **参考答案**：build/test 是「验证」动作，对 PR 也应当跑——这正是不合并坏代码的保障。只有 deploy（有外部副作用的发布动作）才需要门控。把验证与发布解耦，是 CI 设计的常见模式。

**练习 3**：`site-url` 配置错了（比如少写了路径前缀），CI 会报错吗？

> **参考答案**：通常不会在 build/test 阶段报错——`site-url` 主要影响生成 HTML 的资源路径与 sitemap。错误的结果是网站能部署成功，但页面里的资源链接/绝对路径不对，表现为「部署了但样式或链接错乱」。这是「部署成功 ≠ 表现正确」的一个例子。

---

### 4.4 mdBook 的扩展机制：preprocessor / renderer / theme

#### 4.4.1 概念说明

mdBook 并不是一个写死的「Markdown→HTML」转换器，而是一条可插拔的**处理管线**。理解这条管线，你就能看懂 `book.toml` 里那些 `[output.xxx]` 段在做什么，也能理解 perf-book 里被注释掉的 EPUB 后端意味着什么。mdBook 的扩展点主要有三类：

- **preprocessor（预处理器）**：在渲染前对 mdBook 的「书籍表达」（ chapters + Markdown 文本）做变换，例如自动给标题加编号、展开自定义语法、做数学公式替换等。输入是 Markdown，输出仍是（变换后的）Markdown/书籍结构。
- **renderer / output backend（输出后端）**：决定产物形态。内置主要是 HTML 渲染器；通过外部二进制（如 `mdbook-epub`、`mdbook-pdf`）可以增加 EPUB、PDF 等后端。`book.toml` 里每个 `[output.xxx]` 段对应一个后端。
- **theme（主题）**：针对 HTML 渲染器，覆盖默认的 CSS、字体、Handlebars 模板、图标等，改变书籍的外观。

perf-book 本身**很克制**：它只用内置的 HTML 渲染器，没有自定义 preprocessor，也没有覆盖主题（只用了 `default-theme = "rust"` 这类开关）。但它的 `book.toml` 和 ci.yml 里恰好留有「扩展点」的痕迹，非常适合作为认识这些机制的入口。

#### 4.4.2 核心流程

mdBook 一次 `build` 的概念性管线：

```
src/*.md + SUMMARY.md
        │
        ▼
  preprocessors（预处理，可选多个，按顺序变换文本/结构）
        │
        ▼
  renderers（输出后端，可并行：HTML、EPUB、PDF…）
        │
        ▼
  book/ 下的产物（index.html、…；或 .epub、.pdf）
```

每个 renderer 是否启用、如何配置，由 `book.toml` 里对应的 `[output.xxx]` 段决定；每个 preprocessor 是否启用，由 `[preprocessor.xxx]` 段决定。perf-book 只启用了 `[output.html]`，所以管线里实际只有 HTML 这一条渲染分支。

#### 4.4.3 源码精读

先看 perf-book 实际启用的 HTML 后端配置：

[book.toml:L13-L18 — 启用并配置 HTML 输出后端](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L13-L18)

```toml
[output.html]
smart-punctuation = true
default-theme = "rust"
git-repository-url = "https://github.com/nnethercote/perf-book"
edit-url-template = "https://github.com/nnethercote/perf-book/edit/master/{path}"
site-url = "https://nnethercote.github.io/perf-book/"
```

`[output.html]` 这一段就是在配置 HTML renderer。其中几项分别属于不同扩展维度：

- `default-theme = "rust"` —— 属于**主题**维度：让读者打开网页时默认使用 mdBook 内置的 "rust"（浅色）主题。mdBook 内置 light/rust/ayu/dark 等主题，无需自定义 CSS 即可切换。
- `edit-url-template = ".../edit/master/{path}"` —— 让网页每页右上角出现「编辑」按钮，点进去直接跳转到 GitHub 上对应文件的在线编辑器。这是 HTML renderer 提供的「协作」特性，配合 README 里「欢迎以 issue 形式提建议」的态度，降低了读者顺手纠错小错误的门槛。
- `site-url` —— 见 4.3 节，配合部署。

再看「本可启用、但被注释」的 EPUB 后端，这是认识 renderer 扩展点最好的例子：

[book.toml:L20-L24 — 被注释的 EPUB 输出后端](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L20-L24)

```toml
# EPUB
# Currently disabled due to
# https://github.com/nnethercote/perf-book/actions/runs/6358429874/job/17270643057
#[output.epub]
#optional = true  # So epub generation is skipped if mdbook-epub isn't installed.
```

这段揭示了增加一个输出后端的方式：**在 `book.toml` 加一个 `[output.epub]` 段，并安装对应的外部二进制 `mdbook-epub`**。`optional = true` 的含义也很关键——它告诉 mdBook「如果找不到 `mdbook-epub` 这个后端程序，就跳过它而不是报错」。作者之所以把整段注释掉，并附上一条指向某次失败 CI run 的链接，是因为那个 EPUB 后端当时出了问题（参见 README 与 ci.yml 里同样被注释的 epub 相关步骤）。换句话说：

- ci.yml 里也有一串被注释的 EPUB 步骤，与这里一一对应：

[ci.yml:L21-L25 与 L33-L35 — CI 里被注释的 epub 安装与拷贝步骤](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.github/workflows/ci.yml#L21-L35)

```yaml
      # EPUB
      # Currently disabled due to
      # https://github.com/nnethercote/perf-book/actions/runs/6358429874/job/17270643057
      #- name: Setup mdbook-epub
      #  run: cargo install mdbook-epub
      ...
      # EPUB
      #- name: Copy ePub
      #  run: cp book/epub/The\ Rust\ Performance\ Book.epub book/html
```

可以看到「启用一个新后端」在两处都要联动改：`book.toml` 加 `[output.xxx]`，CI 安装对应二进制（`cargo install mdbook-epub`），并在 deploy 时把额外产物（`.epub`）拷进发布目录。这三处目前都被注释，构成一组「待恢复」的功能开关。

> 关于 preprocessor：perf-book 没有使用任何自定义 preprocessor，`book.toml` 里也没有 `[preprocessor.xxx]` 段。这一节对 preprocessor 只做概念介绍，目的有二——一是让你知道 mdBook 有这一层扩展能力（适合做自动编号、公式、自定义语法等），二是说明 perf-book **刻意保持极简**：能用内置能力解决的就绝不引入外部扩展，这与全书「简洁（terse）、广度优先」的风格一脉相承。

#### 4.4.4 代码实践

**实践目标**：通过阅读 `book.toml` 与 ci.yml 中的注释痕迹，复原「给 perf-book 加一个输出后端」需要改动哪些地方。

**操作步骤（源码阅读型）**：

1. 打开 [book.toml:L20-L24](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml#L20-L24)，记录下「启用 EPUB 后端」需要在配置里写什么。
2. 打开 [ci.yml:L21-L35](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.github/workflows/ci.yml#L21-L35)，记录下 CI 里对应的「安装二进制」「拷贝产物」「调整 publish_dir」三处改动。
3. 把这两份清单合并，画出一张「启用 EPUB 的改动清单」。
4. （可选，**待本地验证**）如果你想真正试验：在自己的 fork 上取消 book.toml 里 `[output.epub]` 的注释，本地 `cargo install mdbook-epub` 后运行 `mdbook build`，观察 `book/epub/` 下是否生成 `.epub` 文件。注意这会修改 `book.toml`，做完请还原，且不要提交到正本仓库。

**需要观察的现象 / 预期结果**：

- 你应当能得出类似这样的改动清单：
  1. `book.toml`：取消 `[output.epub]` 与 `optional = true` 的注释。
  2. ci.yml：取消 `Setup mdbook-epub`（`cargo install mdbook-epub`）的注释。
  3. ci.yml：取消 `Copy ePub`（把 `.epub` 拷进发布目录）的注释。
  4. ci.yml：把 deploy 的 `publish_dir` 从 `./book` 改回 `./book/html`（对应注释里的提示）。
- 这张清单正好解释了为什么作者选择把 EPUB 全部注释掉而不是删除：**它是一组关联改动，保留注释便于将来一次性恢复**。

#### 4.4.5 小练习与答案

**练习 1**：`[output.html]` 和 `[output.epub]` 在 mdBook 的管线里分别属于哪一类扩展点？

> **参考答案**：两者都属于 **renderer / output backend（输出后端）**。前者是内置的 HTML 渲染器，后者是通过外部二进制 `mdbook-epub` 提供的 EPUB 渲染器。`book.toml` 里每一个 `[output.xxx]` 段对应一个后端。

**练习 2**：`optional = true`（注释里的 `[output.epub]` 那段）解决的是什么问题？

> **参考答案**：它告诉 mdBook「如果机器上没装 `mdbook-epub` 这个后端程序，就跳过 EPUB 渲染、不要报错」。这让 EPUB 成为「装了才有、没装也不影响 HTML 构建」的可选功能，便于在不同环境（本地缺工具 vs CI 已装）下都能正常 build。

**练习 3**：如果有人想给 perf-book 加「自动给二级标题编号」的功能，应该用 preprocessor 还是 renderer？为什么？

> **参考答案**：用 **preprocessor**。自动编号是对「文本/结构」的变换，发生在渲染之前，属于预处理层的职责；renderer（如 HTML）只负责把（已编号的）内容渲染成产物形态。perf-book 目前没有 preprocessor，引入它需要新增 `[preprocessor.xxx]` 段并安装对应二进制——这也正是 perf-book 至今没有这么做、保持极简的原因。

---

## 5. 综合实践

**综合任务**：模拟一次「给 perf-book 贡献一个修复并走完 CI 闭环」的全流程，把本讲四个模块串起来。

**背景**：假设你发现 `src/io.md` 里某个 Rust 代码块写错了一个变量名，你想修正它，并确保修正后既能在本地通过、也能被 CI 守住。

**操作步骤**：

1. **本地构建与测试（对应 4.1、4.2）**：
   ```bash
   mdbook build      # 结构闸门
   mdbook test       # 正确性闸门：编译所有 Rust 代码块
   mdbook serve      # 浏览器 localhost:3000 看渲染效果
   ```
   确认这三条都通过。

2. **理解你触发的 CI（对应 4.2）**：阅读 [.github/workflows/ci.yml](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.github/workflows/ci.yml)，明确：当你为这次改动发起 PR 时，CI 会跑 `mdbook build` 与 `mdbook test`（deploy 因 `if` 条件被跳过，见 4.3）。

3. **构造一个会被 `mdbook test` 拦下的错误（对应 4.1）**：在 `src/io.md` 某个 ` ```rust ` 代码块里临时引入一个未定义变量，运行 `mdbook test`，观察报错并定位到具体文件与代码块——这正是 CI 的 test 步骤会替你拦截的错误类型。然后用 `git checkout src/io.md` 还原。

4. **推理部署条件（对应 4.3）**：写下一句话，说明「你的这个改动在什么时机才会真正出现在 https://nnethercote.github.io/perf-book/ 上」。
   > 参考答案：当改动以 push 形式合入 `nnethercote/perf-book` 的 `master` 分支时——此时 deploy 的三重 `if` 条件全部满足，CI 把新 `book/` 推到 `gh-pages`，GitHub Pages 随之更新。仅发 PR 不会部署。

5. **检视扩展点（对应 4.4）**：打开 `book.toml`，指出 perf-book 用了哪一类扩展点（HTML renderer），并说明它「刻意没用」的另一类（preprocessor / 额外 renderer 如 EPUB）体现在文件的哪里。

**预期结果**：你能用本讲的语言，完整描述「一次改动从本地验证 → PR 触发 build/test → 合入 master 触发 deploy → 上线 GitHub Pages」的整条链路，并能指出链路上每个环节由哪个文件、哪一行守护。

## 6. 本讲小结

- `mdbook test` 把书稿里的 Rust 代码块抽出来编译/运行，让「书」成为「可测试的文档」；` ```rust ` 块会被测试，` ```rust,ignore ` 显式跳过，`# ` 开头的行渲染时隐藏但仍参与编译。
- perf-book 的 CI 是一条单 job 流水线：checkout → 安装 mdBook → `mdbook build`（结构闸门）→ `mdbook test`（正确性闸门）→ deploy；build/test 与本地命令完全一致。
- deploy 步骤挂了三重 `if` 门控（`push` + `refs/heads/master` + 仓库为 `nnethercote/perf-book`），把发布严格限定在「正本仓库的 master 推送」，PR 与 fork 都不部署。
- mdBook 是一条可插拔管线：preprocessor（预处理）→ renderer/output（HTML/EPUB/PDF…）→ theme（主题）；perf-book 极简，只启用内置 HTML renderer，被注释的 `[output.epub]` 与 ci.yml 里对应的 epub 步骤正好展示了「增加一个输出后端」所需的联动改动。
- `book.toml` 的 `[output.html]` 里 `default-theme`、`edit-url-template`、`site-url` 分别对应主题、协作、部署三个维度的配置。
- 贯穿全讲的仍然是 perf-book 的核心约束：源码即 Markdown，维护书稿 = 维护内容与构建配置的正确性。

## 7. 下一步学习建议

- 下一讲 **u7-l2「为 perf-book 贡献内容——风格与协作」** 会承接本讲的 CI/部署视角，转向「人」的维度：贡献流程（作者偏好 issue 而非 PR）、本书简洁的写作风格，以及如何在 `SUMMARY.md` 与 `src/` 下落地一个新章节。
- 想深入了解 mdBook 本身的扩展机制，可阅读 mdBook 官方文档的 *For Developers* 章节（Preprocessors / Backends），理解如何用 Rust 写一个自定义 preprocessor 或 renderer。
- 想验证你对 CI 的理解，可在自己 fork 的 perf-book 上观察一次 push：你会看到 build/test 通过、deploy 因 `repository` 条件被跳过——这是对 4.3 节条件门控最直观的印证。
- 继续阅读源码：把 [book.toml](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/book.toml) 与 [.github/workflows/ci.yml](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/.github/workflows/ci.yml) 对照阅读，体会「配置文件与 CI 定义如何共同描述一本书的构建与发布」。
