# 本地构建与预览：跑起来整个书库

## 1. 本讲目标

上一讲我们看清了仓库的目录结构：七个书籍目录（内容层）加上 xtask、docker、CI（基础设施层）。本讲要让你亲手把整个书库在本地跑起来。学完本讲，你应该能够：

1. 安装正确版本的 `mdbook` 与 `mdbook-mermaid`，并理解为什么两个都要装。
2. 解释 `cargo xtask build` 这条命令背后发生了什么——`cargo` 并没有内置 `xtask` 子命令，它靠的是一个两行的配置文件。
3. 说出 `build` / `serve` / `deploy` / `clean` 四个子命令各自做什么、写入哪个目录（`site/` 还是 `docs/`）、分别给谁用。
4. 独立完成一次完整的「构建 → 浏览器预览 → 清理」循环。

## 2. 前置知识

本讲是全书第一次真正运行命令，先把几个基础概念用通俗语言过一遍：

- **静态站点生成器（SSG）**：把一批 Markdown 文件转换成一堆 HTML/CSS/JS 文件的工具。转换是一次性的，产物可以直接由任何静态服务器（甚至文件浏览器）提供服务，不需要数据库或后端程序。本仓库用的是 Rust 生态中最流行的 SSG——[mdBook](https://rust-lang.github.io/mdBook/)。
- **预处理器（preprocessor）**：mdBook 允许在「读入 Markdown → 输出 HTML」的流水线上插入自定义处理步骤。本仓库的每本书都启用了 mermaid 预处理器，用来把 ```` ```mermaid ```` 代码块渲染成流程图/时序图。这个预处理器是一个**独立的可执行程序** `mdbook-mermaid`，所以也必须安装。
- **`cargo install` 与 PATH**：`cargo install xxx` 会把 crate 编译成二进制文件，放进 `~/.cargo/bin/`。这个目录通常已在你的 PATH 里，所以装完后在任何终端都能直接敲 `mdbook` 运行。PATH 是操作系统查找可执行程序的目录列表——这个概念马上会在 xtask 探测 `mdbook` 时用到。
- **退出码（exit code）**：每个进程结束时返回一个整数，0 表示成功，非 0 表示失败。脚本和父进程靠它判断命令是否成功，本讲会实际观察它。
- **stdout 与 stderr**：程序有两个输出流——stdout 通常放正常结果，stderr 放错误和诊断信息。两者默认都打印到终端，但可以用重定向分开，xtask 的 usage 输出会刻意区分这两者。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [README.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md) | 项目首页文档 | 快速开始与维护者指南中的安装、构建命令 |
| [.cargo/config.toml](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.cargo/config.toml) | cargo 的项目级配置 | 两行的 `[alias]` 定义，让 `cargo xtask` 变成合法命令 |
| [xtask/src/main.rs](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs) | xtask 构建工具的全部源码（单文件） | `main` 的命令分发、四个 `cmd_*` 函数、`check_mdbook` 探测 |
| [async-book/book.toml](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/book.toml) | async-book 的 mdBook 配置 | `[preprocessor.mermaid]` 一节，说明为什么必须装 `mdbook-mermaid` |
| [.github/workflows/pages.yml](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml) | GitHub Pages 部署流水线 | CI 调用的是 `deploy`、上传的是 `docs/`（佐证两个目录的分工） |

## 4. 核心概念与源码讲解

本讲按你实际操作的顺序拆成三个最小模块：先装工具（4.1），再理解 `cargo xtask` 这条命令为什么存在（4.2），最后逐个吃透四个子命令（4.3）。

### 4.1 mdbook 工具链安装

#### 4.1.1 概念说明

本仓库的「源码」是 Markdown 章节文件，要把它们变成浏览器可读的网站，需要两个外部工具：

- **`mdbook`**：核心构建器。读取每本书的 `book.toml` 与 `src/SUMMARY.md`，把章节 Markdown 编译成带侧边栏、搜索框、可编辑 playground 的 HTML 站点。
- **`mdbook-mermaid`**：预处理器。书中大量图表用 mermaid 语法写成（```` ```mermaid ```` 代码块），`mdbook` 本体不认识它，需要这个预处理器把代码块转换成浏览器端可渲染的形式。

为什么 `mdbook-mermaid` 是**必需**的而不是可选的？看一本书的配置就知道：

#### 4.1.2 核心流程

安装流程：

```
安装 rustup（Rust 工具链）
        │
        ▼
cargo install mdbook@0.4.52 mdbook-mermaid@0.14.0
        │  （编译若干分钟，产物进入 ~/.cargo/bin）
        ▼
mdbook --version / mdbook-mermaid --version   ← 验证两个二进制都在 PATH 上
```

之后每次 `mdbook build` 执行时，mdBook 读到配置里的 `[preprocessor.mermaid]`，就会作为**子进程**去启动 `mdbook-mermaid` 命令——找不到就构建失败。这就是「工具链」的含义：一个由多个独立程序协作的流水线。

#### 4.1.3 源码精读

仓库在维护者指南里给出了**钉住版本**的安装命令：

- [README.md:L80-L82](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L80-L82)：前置条件一节，`cargo install mdbook@0.4.52 mdbook-mermaid@0.14.0`。用 `@版本号` 钉住版本，保证所有维护者产出一致的 HTML，避免新版本 mdBook 引入渲染差异。
- [README.md:L61-L67](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L61-L67)：面向普通读者的快速开始，给出的是不钉版本的 `cargo install mdbook mdbook-mermaid`，随后直接 `cargo xtask serve`。两条路径的差别只在版本是否锁定。

而 `mdbook-mermaid` 必须安装的依据在每本书的 `book.toml` 里：

- [async-book/book.toml:L16-L17](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/book.toml#L16-L17)：`[preprocessor.mermaid]` 节声明 `command = "mdbook-mermaid"`——构建时 mdBook 会按这个名字在 PATH 上查找并启动该程序。其余六本书的 `book.toml` 结构相同。

CI 里也能看到同样的双工具安装（作为交叉印证）：

- [pages.yml:L43-L46](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml#L43-L46)：`which mdbook || cargo install mdbook`、`which mdbook-mermaid || cargo install mdbook-mermaid`——先探测缓存里有没有，没有再装。

#### 4.1.4 代码实践

1. **实践目标**：装好并验证 mdBook 工具链。
2. **操作步骤**：
   ```bash
   cargo install mdbook@0.4.52 mdbook-mermaid@0.14.0
   mdbook --version
   mdbook-mermaid --version
   ```
3. **需要观察的现象**：`cargo install` 会编译这两个工具（可能需要几分钟）；两条 `--version` 命令的输出。
4. **预期结果**：`--version` 输出中分别包含 `0.4.52` 和 `0.14.0`。如果提示 `command not found`，说明 `~/.cargo/bin` 不在 PATH 里（检查 shell 配置中的 PATH 环境变量）。

> 本讲义只描述预期行为，以上命令由读者本地执行验证。

#### 4.1.5 小练习与答案

**练习 1**：只装 `mdbook`、不装 `mdbook-mermaid`，直接跑 `cargo xtask build` 会发生什么？
**答案**：`mdbook` 本体能被 xtask 探测到（下文 `check_mdbook` 只检查 `mdbook`），但构建某一本书时，mdBook 启动 `mdbook-mermaid` 预处理器会失败，该书构建报错，xtask 打印 `✗ <slug> FAILED`。这说明「依赖是否齐全」分两层：xtask 只探测 `mdbook`，预处理器缺失要等到 mdBook 构建阶段才暴露。

**练习 2**：为什么 README 给维护者的命令钉版本（`mdbook@0.4.52`），给普通读者的快速开始却不钉？
**答案**：维护者要产出将被部署上线、被多人协作审阅的站点，版本一致能避免「我这儿渲染正常、你那儿不一样」的问题；普通读者只是本地预览，装最新版通常兼容，门槛也更低。这是「可复现构建」与「低使用门槛」之间的典型取舍。

### 4.2 cargo alias 机制：`cargo xtask` 为什么是一条合法命令

#### 4.2.1 概念说明

敲 `cargo xtask build` 时，`cargo` 并不认识 `xtask`——它的内置子命令只有 `build`、`run`、`test` 这些。魔法来自 cargo 的**别名（alias）机制**：项目可以在 `.cargo/config.toml` 的 `[alias]` 表里把一个新名字展开成任意 cargo 子命令序列。

本仓库的别名定义只有两行，却把「构建工具」变成了「cargo 的原生子命令」：

```toml
[alias]
xtask = "run --package xtask --"
```

于是：

```
cargo xtask build
   └─ 展开为 ─→ cargo run --package xtask -- build
                     │            │            └─ 传给 xtask 程序的参数
                     │            └─ 第一个 `--`：cargo 自己参数到此结束
                     └─ 编译并运行 workspace 里的 xtask 包
```

**`--` 的作用**：它把「cargo 自己的参数」和「转发给目标程序的参数」分开。`cargo xtask serve` 中的 `serve` 会原样交给 xtask 的 `main` 函数，而不是被 cargo 当成 `cargo run` 的选项吞掉。

这种做法（社区称为 **xtask 模式**）的好处：不用安装任何额外任务运行器，构建逻辑用 Rust 写、享受类型检查和依赖管理，且 `.cargo/config.toml` 随仓库提交，**所有克隆者自动获得这个别名**。它的更多工程细节（与 Makefile 的对比、workspace 组织）在 u2-l1 展开。

#### 4.2.2 核心流程

```
用户敲 cargo xtask serve
        │
        ▼
cargo 读取 <仓库>/.cargo/config.toml，发现 [alias] xtask
        │
        ▼
展开为 cargo run --package xtask -- serve
        │
        ▼
cargo 编译（有缓存则跳过）并运行 xtask 二进制，
    argv = ["xtask", "serve"]
        │
        ▼
xtask 的 main() 里 env::args().skip(1) 取出 "serve"，进入 match 分发
```

#### 4.2.3 源码精读

- [.cargo/config.toml:L1-L2](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.cargo/config.toml#L1-L2)：整个文件就这两行——`[alias]` 表声明别名 `xtask = "run --package xtask --"`。注意 `.cargo/` 是**仓库级**配置：cargo 会从当前目录向上查找 `.cargo/config.toml`，所以在仓库内任何位置执行 `cargo xtask` 都生效。
- [xtask/src/main.rs:L61-L62](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L61-L62)：`main` 的第一行 `let args: Vec<String> = env::args().skip(1).collect();`——`skip(1)` 跳过 argv[0]（程序自身路径），只留用户传入的子命令名。这正是别名里那个 `--` 送进来的参数。

再看参数合法/非法时程序怎么表现：

- [xtask/src/main.rs:L71-L76](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L71-L76)：`help`/`-h`/`--help` 或**不传任何参数**时 `print_usage(0)`（成功退出）；未知命令先 `eprintln!` 报错再 `print_usage(1)`（失败退出）。
- [xtask/src/main.rs:L79-L97](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L79-L97)：`print_usage` 按 `code == 0` 与否选择把 usage 写到 **stdout** 还是 **stderr**，最后 `std::process::exit(code)`。退出码和输出流的区分是标准 CLI 惯例——脚本可以只读 stdout 拿帮助文本，也可以只查退出码判断成败。

#### 4.2.4 代码实践

1. **实践目标**：亲眼验证别名展开与 usage 的流/退出码行为。
2. **操作步骤**：
   ```bash
   cargo xtask                 # 不带参数
   echo "exit=$?"              # 查看退出码
   cargo xtask bogus           # 故意敲一个不存在的子命令
   echo "exit=$?"
   cargo xtask bogus 2>/dev/null   # 丢弃 stderr
   cargo run --package xtask -- help   # 手动做一次别名展开
   ```
3. **需要观察的现象**：前两条命令各打印什么、退出码是多少；第三条丢弃 stderr 后还剩什么输出；最后一条与 `cargo xtask help` 的输出是否一致。
4. **预期结果**：
   - `cargo xtask` 打印 usage，`exit=0`；
   - `cargo xtask bogus` 打印 `Unknown command: bogus` 加 usage，`exit=1`；
   - `2>/dev/null` 后**没有任何输出**——证明错误信息全走了 stderr（`eprintln!` 与 `print_usage(1)` 都写 stderr；cargo 自己那行 `Running ...` 也走 stderr）；
   - 最后一条手动展开的输出与 `cargo xtask help` 相同，证明别名只是字符串展开。

> 待本地验证：不同 cargo 版本打印的 `Running \`target/debug/xtask ...\` 前缀行可能略有差异，但 usage 正文与退出码行为稳定。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `.cargo/config.toml` 里的 `--` 去掉，`cargo xtask build` 会怎样？
**答案**：别 名 变 成 `run --package xtask`，`build` 会被 cargo 当作传给 `cargo run` 的参数处理——`cargo run` 没有名为 `build` 的合法参数，cargo 会报参数错误，xtask 根本收不到 `build` 这个词。`--` 是参数归属的分界线。

**练习 2**：为什么别名定义放在仓库的 `.cargo/config.toml`，而不是每个开发者的全局 `~/.cargo/config.toml`？
**答案**：放在仓库里并提交进版本库，任何克隆者无需手工配置就获得 `cargo xtask` 命令——工具入口随代码分发。放全局配置则每个新贡献者都要手动设置一遍，容易漏。

### 4.3 xtask 四个子命令：build / serve / deploy / clean

#### 4.3.1 概念说明

xtask 的全部行为就是一个围绕四个子命令的命令分发器。先给结论表（与 README 维护者指南一一对应）：

| 命令 | 行为 | 输出目录 | 给谁用 |
|------|------|---------|--------|
| `cargo xtask build` | 构建全部七本书 + 生成落地页 | `site/` | 本地预览 |
| `cargo xtask serve` | 先执行一次 build，再把 `site/` 跑在 `http://localhost:3000` | `site/` | 本地阅读/检查 |
| `cargo xtask deploy` | 同样的构建，但输出到 `docs/`，并提示如何发布 | `docs/` | GitHub Pages 部署 |
| `cargo xtask clean` | 删除 `site/` 和 `docs/` | （删除） | 清理工作区 |

`site/` 与 `docs/` 内容几乎相同，分开存在的**唯一原因是用途不同**：`site/` 是本地预览的临时产物，`docs/` 是面向 GitHub Pages「从分支部署」传统的发布目录。CI 印证了这个分工——[pages.yml:L48-L54](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/pages.yml#L48-L54) 中构建步骤跑的正是 `cargo xtask deploy`，上传的正是 `./docs`。两个目录都不入库（属于构建产物），细节在 u2-l3 展开。

还有一个容易踩的点：`serve` 与 mdBook 自带的 `mdbook serve` 行为不同——xtask 的 `serve` 是「构建一次 + 起一个静态服务器」，**不会监视文件变化**；改了章节要重新运行才有效果。写作时想实时预览，应该对单本书用 `mdbook serve --open`（它带 live-reload）：

- [README.md:L100-L104](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L100-L104)：README 明确给出「构建/预览单本书」的替代做法 `cd c-cpp-book && mdbook serve --open`。

#### 4.3.2 核心流程

`main` 的分发逻辑（伪代码）：

```
match 第一个参数:
    "build"   → cmd_build()                      # 探测 mdbook → build_to("site")
    "serve"   → cmd_build(); cmd_serve()         # 先构建，再起服务器
    "deploy"  → cmd_deploy()                     # 探测 mdbook → build_to("docs") + 发布提示
    "clean"   → cmd_clean()                      # 删除 site/ 与 docs/
    help/无参  → print_usage(0)
    其他      → 报错 + print_usage(1)
```

`build_to(dir)` 的内部流程（build 与 deploy 共用）：

```
1. 定位项目根（CARGO_MANIFEST_DIR 的父目录）
2. 删除已存在的输出目录，重建空目录     ← 每次都是干净构建
3. 遍历 BOOKS 注册表：
     目录存在 → mdbook build --dest-dir <输出>/<slug> → 统计成功数
     目录缺失 → 打印 ✗ 并跳过
4. 生成落地页 index.html（卡片式导航）
5. 写入空的 .nojekyll（阻止 GitHub Pages 用 Jekyll 再加工产物）
```

#### 4.3.3 源码精读

命令分发总入口：

- [xtask/src/main.rs:L61-L77](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L61-L77)：`main` 用 `match args.first().map(|s| s.as_str())` 分发四个子命令。注意 `serve` 分支（L65-L68）是 `cmd_build(); cmd_serve();` 两条语句——**serve 隐含一次完整构建**。

build 与 deploy 的对照：

- [xtask/src/main.rs:L101-L107](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L101-L107)：`cmd_build` 先用 `check_mdbook()` 探测工具，再把目标目录 `"site"` 传给 `build_to`。
- [xtask/src/main.rs:L109-L116](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L109-L116)：`cmd_deploy` 结构相同，只是目标换成 `"docs"`，结束后多打一行发布指引（commit `docs/` 并在 GitHub Pages 设置里选 "Deploy from a branch" → `/docs`）。
- [xtask/src/main.rs:L118-L126](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L118-L126)：`check_mdbook` 的探测手法——`Command::new("mdbook").arg("--version")`，把 stdout/stderr 都重定向到 `Stdio::null()`（静默），只看子进程退出状态是否成功。`unwrap_or(false)` 兜底「连程序都启动不了」的情况。找不到时 `cmd_build` 打印的报错（L103）直接给出安装文档链接并以退出码 1 结束。

批量构建主体：

- [xtask/src/main.rs:L128-L168](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L128-L168)：`build_to`。L132-L135 先删后建输出目录（保证干净构建）；L140-L160 遍历上一讲认识的 `BOOKS` 注册表，对每本书以书籍目录为工作目录运行 `mdbook build --dest-dir <输出>/<slug>`，逐书打印 `✓/✗`；L161 汇总 `N/7 books built`；L163 生成落地页；L166 写入 `.nojekyll`。

serve 的启动段：

- [xtask/src/main.rs:L406-L417](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L406-L417)：`cmd_serve` 一进来就 `fs::canonicalize(site)`——`site/` 不存在直接 panic，报错文案贴心地提示「先跑 build，或直接用 serve（它会自动先 build）」；随后绑定 `127.0.0.1:3000`（L411-L412，只监听本机回环地址，不对外暴露），打印 `Serving at http://localhost:3000 (Ctrl+C to stop)`。服务器内部的多层安全设计属于 u2-l5/u2-l6 的内容，本讲只需会启动它。

clean：

- [xtask/src/main.rs:L487-L496](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L487-L496)：`cmd_clean` 对 `["site", "docs"]` 逐个判断存在即 `remove_dir_all`。注意它**同时清理两个目录**，不只是 `site/`。

README 中四条命令的官方说明（与人读文档对齐）：

- [README.md:L93-L98](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L93-L98)：build/serve/deploy/clean 四行注释，与本节表格同义。

#### 4.3.4 代码实践

1. **实践目标**：完成一次批量构建，并解剖 `site/` 的产物结构。
2. **操作步骤**：
   ```bash
   cargo xtask build
   ls site/
   ls -a site/ | head -20      # -a 才能看到 .nojekyll
   ls site/async-book | head   # 看单本书的产物
   ```
3. **需要观察的现象**：终端逐行打印每本书的 `✓ <slug>`、最后的 `N/7 books built`、`✓ index.html` 与 `Done! Output in site/`；`site/` 下有哪些条目。
4. **预期结果**：七本书全部 `✓`，汇总 `7/7 books built`；`site/` 包含七个书籍子目录、一个 `index.html`（落地页）和一个空的 `.nojekyll` 文件；`site/async-book/` 里是 mdBook 输出的 `index.html`、`book/` 等静态文件。

> 待本地验证：如果某本书构建失败，汇总行会显示 `6/7` 之类——通常意味着 mdbook-mermaid 未安装（回到 4.1）。

#### 4.3.5 小练习与答案

**练习 1**：`cargo xtask serve` 和 `cd async-book && mdbook serve --open` 都能在 3000 端口看内容，它们的核心区别是什么？各适合什么场景？
**答案**：xtask 的 `serve` 先构建**全部七本书**再起服务器，看到的是落地页 + 整个书库，但不会监视文件变化；`mdbook serve --open` 只服务**单本书**，且带 live-reload——保存 Markdown 即自动重建刷新。前者适合最终检查整体效果，后者适合写作时的实时预览。

**练习 2**：为什么 `build_to` 每次都先 `remove_dir_all` 删掉整个输出目录，而不是增量更新？
**答案**：删除被删掉的书、改名/移动过的章节不会在增量构建中自动清掉旧文件，残留会让线上站点出现「幽灵页面」；全量重建以一点构建时间换取产物 = 源码的确定性映射。对七本书体量的文档项目，这是简单可靠的取舍。

**练习 3**：`cargo xtask clean` 之后马上运行 `cargo xtask serve`（而不是 build），会发生什么？
**答案**：什么都不会坏——`serve` 分支先执行 `cmd_build()` 重建 `site/`，再进入 `cmd_serve()`。这正是 `cmd_serve` 里 canonicalize 报错文案所说的「serve 会自动先 build」的含义。

## 5. 综合实践

把三个模块串成一次完整循环——这也是本讲规格中指定的实践任务：

1. **实践目标**：独立完成「安装 → 构建 → 预览 → 清理」全流程，并能在产物目录中定位每个命令留下的痕迹。
2. **操作步骤**：
   ```bash
   # ① 确认工具链（4.1）
   mdbook --version && mdbook-mermaid --version

   # ② 构建全部书籍（4.3）
   cargo xtask build
   ls -a site/          # 应看到 7 个书籍目录 + index.html + .nojekyll

   # ③ 起服务器预览（4.3）
   cargo xtask serve
   # 浏览器打开 http://localhost:3000
   #   - 确认落地页列出七张带分类色条的书籍卡片
   #   - 点进任意一本书（如 async-book），确认侧边栏、Mermaid 图、playground 正常
   #   - 按 Ctrl+C 停止（进程以退出码 0 干净退出）

   # ④ 清理（4.3）
   cargo xtask clean
   ls site/ 2>&1        # 应提示目录不存在
   ```
3. **需要观察的现象**：②中逐书 `✓` 与 `7/7 books built` 汇总；③中落地页与书籍页面均可正常打开，Ctrl+C 后终端无报错；④后 `site/` 消失。
4. **预期结果**：全流程无错误完成；你能回答「落地页的 `index.html` 是谁写的」（xtask 的 `write_landing_page`，见 [xtask/src/main.rs:L163](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L163) 的调用点）以及「`site/` 和 `docs/` 分别给谁用」（本地预览 / Pages 部署）。

> 待本地验证：若端口 3000 被占用，`cmd_serve` 的 `TcpListener::bind` 会 panic 并提示 `failed to bind port 3000`，需先释放端口再试。

## 6. 本讲小结

- 构建书库需要两个外部工具：`mdbook`（核心构建器）与 `mdbook-mermaid`（每本 `book.toml` 中 `[preprocessor.mermaid]` 声明的预处理器）；维护者路径钉版本 `mdbook@0.4.52 mdbook-mermaid@0.14.0` 以保证可复现。
- `cargo xtask` 不是魔法：`.cargo/config.toml` 里两行 `[alias]` 把它展开成 `cargo run --package xtask --`，别名随仓库分发，参数经 `--` 原样传给 xtask 的 `main`。
- 四个子命令一句话：`build` 全量构建到 `site/`；`serve` = 先 build 再在 `127.0.0.1:3000` 起静态服务器（不监视变化）；`deploy` 构建到 `docs/` 供 GitHub Pages 使用（CI 跑的就是它）；`clean` 删除 `site/` 与 `docs/` 两个目录。
- `site/` 与 `docs/` 内容几乎相同，分开只为区分「本地预览产物」与「部署产物」两种用途；每次构建都先删后建，保证产物干净。
- 工具探测用的是「静默运行 `mdbook --version` 看退出状态」的子进程手法；usage 输出按退出码分流 stdout/stderr，这是值得模仿的 CLI 惯例。

## 7. 下一步学习建议

到这里你已经能跑起整个书库，接下来的两条路：

1. **想先「用」起来**：进入 u1-l4「一本书的解剖：book.toml 与 SUMMARY.md」，学习 mdBook 的配置节与章节导航语法，试着改一章标题并预览——那正是 `mdbook serve --open` 发挥 live-reload 威力的场景。
2. **想读懂今天用到的构建工具**：进入 u2 单元。u2-l1 会从 workspace 与别名机制的系统讲解开始，u2-l2 精读 `main` 分发与 `check_mdbook`，u2-l3 深挖 `build_to` 的清理-构建-收尾细节，u2-l5/u2-l6 拆解你今天在 3000 端口用到的那个自研静态服务器。
