# 项目定位与工程结构

## 1. 本讲目标

本讲是 typst-cli 学习手册的第一篇。读完后你应当能够：

- 说清楚 **typst-cli 是什么**：它是 Typst 工作区里产出 `typst` 命令行可执行文件的二进制 crate，而不是编译器核心本身。
- 读懂它的 [Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml)：依赖了哪些 crate、有哪些 feature 开关（`embedded-fonts` / `http-server` / `self-update`），以及 `[[bin]]` 如何把产物命名为 `typst`。
- 看懂 [main.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs) 的顶层模块清单，并对照 `dispatch` 函数列出全部子命令。
- 从源码构建出 `typst` 二进制并运行 `--version` / `--help`。

本篇不涉及任何编译流程细节，只建立「这个 crate 在整个项目里处于什么位置、怎么把它跑起来」的全局认知。后续讲义会逐层深入。

## 2. 前置知识

在开始前，建议你了解以下几个概念（不熟悉也没关系，下面会用通俗语言再讲一遍）：

- **Typst**：一个基于标记（markup）的现代排版系统，目标是「像 LaTeX 一样强大，但更容易学」。它可以把 `.typ` 源文件编译成 PDF、PNG、SVG、HTML 等。详见仓库 [README.md](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/README.md)。
- **Rust 工作区（Cargo workspace）**：多个相关 crate 放在同一个仓库里、共享一份 `Cargo.toml`（工作区清单）统一管理依赖版本的方式。每个子目录是一个独立 crate。
- **crate**：Rust 的编译单元。一个 crate 可以是「库」（被别人依赖）或「二进制」（产出可执行文件），typst-cli 就是一个二进制 crate。
- **feature 开关**：Cargo 里用 `[features]` 定义的编译期开关，用于按需启用/禁用某些功能，从而控制产物体积或可选依赖。
- **clap**：Rust 生态里最流行的命令行参数解析库，typst-cli 用它来定义子命令和选项。

你还需要本机装有 Rust 工具链。本仓库要求 `rust-version = "1.92"`（见工作区 [Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/Cargo.toml)），可用 [rustup](https://rustup.rs/) 安装最新稳定版。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/README.md)（仓库根） | 介绍 Typst 是什么、如何安装与从源码构建。 |
| [Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/Cargo.toml)（仓库根） | 工作区清单：列出所有成员 crate、统一版本号与依赖版本。 |
| [crates/typst-cli/Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml) | typst-cli 自己的清单：依赖、feature 与 `[[bin]]` 配置。本讲的主角。 |
| [crates/typst-cli/src/main.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs) | 二进制入口：声明全部子模块、`main()` 入口与命令分发函数 `dispatch()`。 |
| [crates/typst-cli/src/args.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs) | 用 clap 定义的全部命令行参数与子命令结构（本讲只看子命令清单）。 |
| [crates/typst-cli/src/greet.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/greet.rs) | 首次运行 `typst`（不带子命令）时的欢迎信息。 |

---

## 4. 核心概念与源码讲解

### 4.1 Typst 在 workspace 中的定位

#### 4.1.1 概念说明

很多人第一次看到 `typst-cli` 会以为「这就是 Typst 的全部」。其实不是。

Typst 仓库是一个 **Cargo workspace**，里面分成了十几个 crate，各司其职：

- **编译器核心**：`typst`（纯逻辑的编译与排版引擎，不依赖操作系统）。
- **输出格式 crate**：`typst-pdf`、`typst-render`（位图）、`typst-svg`、`typst-html`、`typst-bundle`（打包导出）。
- **语言能力 crate**：`typst-syntax`（语法）、`typst-eval`（脚本求值）、`typst-layout`（布局）、`typst-library`（标准库）等。
- **操作系统集成「工具箱」**：`typst-kit`（字体发现、包下载、文件监听、诊断打印等，把编译器和真实 OS 连起来）。
- **typst-cli**：上面这一切的「组装车间」，产出最终用户运行的 `typst` 命令。

可以这么理解：**核心 `typst` 是「大脑」，它只懂抽象的文档模型；`typst-cli` 是「身体」，负责读文件、找字体、连网络、把结果写回磁盘。** 两者通过一个叫 `World` 的 trait 沟通（这是下一阶段 u2-l1 的主题，本篇不展开）。

这种「纯核心 + 薄壳 CLI」的分层好处是：同一个编译器核心既能被命令行用，也能被 Web 编辑器、语言服务器（Tinymist）等复用。

#### 4.1.2 核心流程

从用户敲下 `typst compile file.typ` 到产物生成，参与方的协作关系大致是：

```
用户命令行
   │
   ▼
typst-cli (main.rs 入口)        ← 本讲主角：解析参数、分发
   │  依赖
   ├──▶ typst-kit              ← 提供字体/包/下载/监听等 OS 集成
   │        │ 依赖
   │        ▼
   ├──▶ typst (编译器核心)      ← 真正的「排版大脑」
   │        │
   │        ▼
   └──▶ typst-pdf / typst-render / typst-svg / typst-html  ← 各导出格式
```

注意箭头方向：**typst-cli 依赖核心，而不是反过来**。核心对 CLI 一无所知。

#### 4.1.3 源码精读

工作区根 [Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/Cargo.toml) 第 1-4 行声明了成员清单，并把 **默认构建目标设为 typst-cli**：

```toml
[workspace]
members = ["crates/*", "docs", "tests", "tests/fuzz", "tests/wrapper"]
default-members = ["crates/typst-cli"]
```

- [Cargo.toml:L1-L4](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/Cargo.toml#L1-L4)：`members = ["crates/*", ...]` 表示 `crates/` 下每个子目录都是一个 crate；`default-members = ["crates/typst-cli"]` 意味着在工作区根直接 `cargo build` 时，默认只构建 typst-cli。

仓库根 [README.md](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/README.md) 第 23-35 行一句话定位了整个项目，并明确指出本仓库包含「编译器 + CLI」：

> This repository contains the Typst compiler and its CLI, which is everything you need to compile Typst documents locally.

- [README.md:L23-L35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/README.md#L23-L35)：这段同时交代了 Typst 的能力（标记语法、脚本、数学、增量编译、友好报错）与仓库范围。

工作区统一版本号在第 6-9 行，typst-cli 的版本「继承」自这里：

- [Cargo.toml:L6-L9](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/Cargo.toml#L6-L9)：`version = "0.15.1"`、`rust-version = "1.92"`、`edition = "2024"`，子 crate 用 `version = { workspace = true }` 引用。

#### 4.1.4 代码实践

**实践目标**：确认 typst-cli 只是众多 crate 之一，并建立「核心 vs CLI」的直觉。

**操作步骤**：

1. 在仓库根目录浏览 `crates/` 目录，数一下一共有多少个 `Cargo.toml`（每个对应一个 crate）。
2. 找到 `crates/typst/Cargo.toml`（编译器核心）和 `crates/typst-cli/Cargo.toml`（本讲主角）。
3. 对比两个 crate 的 `description` 字段：核心描述自己是「compiler」，CLI 描述自己是「command line interface」。

**需要观察的现象**：`crates/` 下有十多个 crate，typst-cli 只是其中之一。

**预期结果**：你能指出 `typst`（核心）与 `typst-cli`（二进制）是两个不同的 crate。具体数量以本地 `ls crates/` 为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么编译器核心 `typst` 不直接放在 typst-cli 里，而要拆成独立 crate？

<details>
<summary>参考答案</summary>

为了让同一个编译器核心能被多种宿主复用：命令行（typst-cli）、在线编辑器、语言服务器、fuzz 测试等。把纯逻辑的核心与「读写文件、连网络」的 CLI 分离，核心就保持纯净、可测试、可嵌入。
</details>

**练习 2**：在工作区根直接运行 `cargo build`（不指定包名），会构建哪个 crate？为什么？

<details>
<summary>参考答案</summary>

会构建 `crates/typst-cli`，因为工作区 [Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/Cargo.toml) 里设置了 `default-members = ["crates/typst-cli"]`。
</details>

---

### 4.2 Cargo.toml：依赖、feature 开关与 [[bin]] 配置

#### 4.2.1 概念说明

[crates/typst-cli/Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml) 是理解这个 crate 的「身份证」。它回答三个问题：

1. **产物叫什么？** —— 由 `[[bin]]` 段决定。
2. **依赖什么？** —— 由 `[dependencies]` 段决定，体现了「组装车间」的角色。
3. **有哪些可选功能？** —— 由 `[features]` 段决定，控制编译期开关。

#### 4.2.2 核心流程

typst-cli 的依赖可以分成三组：

```
[dependencies]
  ├── 编译器与导出：typst, typst-pdf, typst-render, typst-svg, typst-html,
  │                 typst-bundle, typst-eval, typst-layout, typst-macros, typst-timing
  ├── OS 集成工具箱：typst-kit（带一大串 feature）
  └── 通用工具：clap(参数), codespan-reporting(诊断), rayon(并行),
                 serde(序列化), chrono(时间), self-replace/zip/xz2(自更新, 可选) ...
```

feature 开关则像「出厂配置包」：

- `default = ["embedded-fonts", "http-server"]`：默认开启内嵌字体和内置 HTTP 服务器。
- `self-update`：启用自更新（额外引入 `self-replace`、`xz2`、`zip`）。
- `vendor-openssl`：静态打包 OpenSSL（Linux 上常用，避免系统缺库）。

#### 4.2.3 源码精读

**产物命名**：第 15-18 行的 `[[bin]]` 段决定了最终可执行文件的名字是 `typst`，入口是 `src/main.rs`：

- [Cargo.toml:L15-L18](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml#L15-L18)：`name = "typst"`、`path = "src/main.rs"`、`doc = false`（不生成二进制的文档）。这就是为什么构建出来叫 `typst` 而不是 `typst-cli`。

**对编译器核心与各导出格式的依赖**：第 20-31 行：

- [Cargo.toml:L20-L31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml#L20-L31)：这里 `typst = { workspace = true }` 引入编译器核心，并依次引入 `typst-pdf`/`typst-render`/`typst-svg`/`typst-html`/`typst-bundle` 等导出格式 crate。这正是「CLI 是组装车间」的直接证据。

**对 typst-kit 的依赖与 feature**：第 58-71 行：

- [Cargo.toml:L58-L71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml#L58-L71)：给 `typst-kit` 开启了一长串 feature（`embedded-fonts`、`scan-fonts`、`system-packages`、`universe-packages`、`system-downloader`、`watcher`、`timer` 等）。也就是说，typst-cli 几乎把 typst-kit 的全部 OS 能力都打开了。

**feature 开关**：第 88-101 行：

- [Cargo.toml:L88-L101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml#L88-L101)：
  - `default = ["embedded-fonts", "http-server"]`（第 89 行）—— 默认配置。
  - `embedded-fonts`（第 92 行）—— 内嵌若干字体，离线也能用基础字体。
  - `http-server`（第 95 行）—— 给 `typst watch` 与 HTML 导出提供内置 HTTP 服务器（live reload）。
  - `self-update`（第 98 行）—— 启用自更新，额外依赖 `self-replace`、`xz2`、`zip`（见第 45、55、56 行的 `optional = true`）。
  - `vendor-openssl`（第 101 行）—— 静态打包 OpenSSL（Windows/macOS 不适用）。

注意 `self-update` 的依赖是「按需引入」的：第 45、55、56 行用 `optional = true` 标注，只有开启 `self-update` 时才会拉取。这是 Cargo 的标准 optional-dependency 机制。

#### 4.2.4 代码实践

**实践目标**：用 feature 开关改变编译产物，直观感受 `[features]` 的作用。

**操作步骤**：

1. 先在 `crates/typst-cli` 下默认构建一次（`cargo build`），观察能正常编译。
2. 用 `cargo tree -p typst-cli -e features`（在工作区根执行）查看哪些依赖是被哪个 feature 拉进来的，重点看 `self-update` 相关的 `self-replace`/`zip`/`xz2` 是否出现。
3. 再执行 `cargo build -p typst-cli --no-default-features --features self-update`，观察编译行为变化。

**需要观察的现象**：默认（不开 `self-update`）时，`self-replace`/`zip`/`xz2` 不在依赖树里；显式开启 `self-update` 后才出现。

**预期结果**：你会看到 optional 依赖随 feature 开关进出依赖树。（依赖树的具体输出以本地为准，待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `self-replace`、`xz2`、`zip` 被标为 `optional = true`，而不直接放进默认依赖？

<details>
<summary>参考答案</summary>

因为自更新（`self-update`）是可选功能，很多发行版（如 Linux 包管理器）不允许程序自行替换二进制，它们会用包管理器升级。把这三个依赖设为 optional 并绑定到 `self-update` feature，默认构建就不必拉取它们，缩小了产物体积和攻击面。
</details>

**练习 2**：`[[bin]] name = "typst"` 这一行如果删掉，构建出来的二进制会叫什么？

<details>
<summary>参考答案</summary>

Cargo 会默认用 crate 名作为二进制名，也就是 `typst-cli`。正因为显式写了 `name = "typst"`，用户敲的命令才是 `typst` 而非 `typst-cli`。
</details>

---

### 4.3 main.rs：顶层模块清单与子命令概览

#### 4.3.1 概念说明

[main.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs) 是整个 CLI 的入口。它做三件事：

1. 用一堆 `mod` 声明把所有子模块挂进来（每个子模块对应一类功能）。
2. 提供 `main()` 入口：处理 SIGPIPE、调用分发、打印错误、返回退出码。
3. 提供 `dispatch()`：根据解析出的子命令，调用对应模块的函数。

读懂这三块，你就掌握了 typst-cli 的「目录骨架」。

#### 4.3.2 核心流程

`main()` 的执行流程（伪代码）：

```
main():
    sigpipe::reset()            # 让被管道打断时不要 panic
    res = dispatch()            # 解析参数并执行子命令
    if res 是 Err(msg):
        set_failed()            # 把退出码设为 FAILURE
        print_error(msg)        # 打印 "error: ..."
        for hint in msg.hints:
            print_hint(hint)    # 打印 "hint: ..."
    return EXIT                 # 返回当前退出码（SUCCESS 或 FAILURE）
```

`dispatch()` 的流程就是一张「子命令 → 处理函数」的查找表（见 4.3.3）。

#### 4.3.3 源码精读

**模块清单**：第 1-17 行声明了全部子模块：

- [main.rs:L1-L17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L1-L17)：这一串 `mod args; mod compile; ... mod world;` 就是 typst-cli 的全部源码模块。注意第 14-15 行的 `mod update;` 被 `#[cfg(feature = "self-update")]` 守卫——只有开启 `self-update` feature 时，真正的 `update` 模块才会被编译进来；否则使用第 134-147 行的桩（stub）实现。

**入口 `main()`**：第 49-66 行：

- [main.rs:L49-L66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L49-L66)：`sigpipe::reset()` 处理 SIGPIPE（当输出被管道关闭时避免 Rust 默认 panic）；之后调用 `dispatch()`；若出错则 `set_failed()` 并打印错误与提示；最后通过 `thread_local` 的 `EXIT` 返回退出码。`EXIT` 定义在第 34-37 行，默认 `SUCCESS`，`set_failed()` 把它改成 `FAILURE`。

**命令分发 `dispatch()`**：第 68-82 行是子命令到模块的映射表：

- [main.rs:L68-L82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L68-L82)：这就是全部 9 个子命令的分发逻辑。把它整理成表：

  | 子命令 (`Command`) | 处理函数 | 所在模块 |
  |---|---|---|
  | `Compile`（别名 `c`） | `compile::compile` | `compile.rs` |
  | `Watch`（别名 `w`） | `watch::watch` | `watch.rs` |
  | `Init` | `init::init` | `init.rs` |
  | `Query`（已隐藏/弃用） | `query::query` | `query.rs` |
  | `Eval` | `eval::eval` | `eval.rs` |
  | `Fonts` | `fonts::fonts` | `fonts.rs` |
  | `Update` | `update::update` | `update.rs`（受 feature 控制） |
  | `Completions` | `completions::completions` | `completions.rs` |
  | `Info` | `info::info` | `info.rs` |

**子命令定义源头**：上表的 `Command` 枚举定义在 [args.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs) 第 81-112 行：

- [args.rs:L81-L112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L81-L112)：`pub enum Command` 用 clap 的 `Subcommand` 派生。可以看到 `Compile` 带 `visible_alias = "c"`、`Watch` 带 `visible_alias = "w"`、`Query` 标了 `hide = true`（已弃用）、`Update` 在非 `self-update` feature 下被 `clap(hide = true)` 隐藏。这些注解直接决定了 `typst --help` 里会显示哪些命令。

**首次运行的欢迎信息**：当用户直接敲 `typst` 不带子命令时，[main.rs:L40-L47](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L40-L47) 的 `ARGS` 惰性解析会捕获「缺少子命令」错误，进而调用 `greet::greet()`。欢迎正文见 [greet.rs:L4-L24](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/greet.rs#L4-L24)，它列出了 `typst compile`、`typst watch`、`typst init` 三条最常用命令。

#### 4.3.4 代码实践

**实践目标**：把 `typst --help` 的输出与源码里的子命令定义一一对应。

**操作步骤**：

1. 构建出二进制（见 4.4）后运行 `./target/debug/typst --help`。
2. 数一下帮助里列出的子命令，对照 [args.rs:L81-L112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L81-L112) 的 `Command` 枚举。
3. 注意哪些命令**不**出现在帮助里（`Query` 被 `hide = true`；`Update` 在默认 feature 下也被隐藏）。

**需要观察的现象**：`Query` 和 `Update` 不在默认 `--help` 列表中；`Compile`/`Watch` 会显示别名 `c`/`w`。

**预期结果**：默认构建下 `--help` 列出 `compile`、`watch`、`init`、`eval`、`fonts`、`completions`、`info` 等可见命令，隐藏的 `query`/`update` 不出现。

#### 4.3.5 小练习与答案

**练习 1**：默认 feature 下运行 `typst update` 会发生什么？为什么？

<details>
<summary>参考答案</summary>

会报错 "self-updating is not enabled for this executable..."。因为默认不开 `self-update` feature，真正的 [update.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L134-L147) 不会被编译，取而代之的是 [main.rs:L134-L147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L134-L147) 里的桩函数，它直接 `bail!` 返回这条提示。
</details>

**练习 2**：`main()` 里为什么要 `sigpipe::reset()`？

<details>
<summary>参考答案</summary>

Rust 默认会把 SIGPIPE 转成忽略，导致程序向已关闭的管道（如 `typst ... | head`）写数据时触发 panic 并打印难看的 backtrace。`sigpipe::reset()` 恢复默认的「收到 SIGPIPE 即退出」行为，让 CLI 在管道场景下表现得更像传统 Unix 工具。源码注释也指向了相关讨论（见 [main.rs:L51-L53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L51-L53)）。
</details>

---

### 4.4 如何构建并运行 typst CLI 二进制

#### 4.4.1 概念说明

「从源码构建」是把这份源码变成可执行 `typst` 命令的过程。得益于 Cargo workspace，这一步非常简单——因为工作区把 typst-cli 设为 `default-members`，在工作区根直接 `cargo build` 就行。

#### 4.4.2 核心流程

构建与运行的两种姿势：

```
方式 A（工作区根，推荐）：
    cargo build              # 默认构建 typst-cli（debug）
    cargo build --release    # 优化构建
    → 产物：target/debug/typst  或  target/release/typst

方式 B（在 crates/typst-cli 内）：
    cargo build              # 同样构建本 crate
    → 产物：target/debug/typst

运行：
    ./target/debug/typst --version
    ./target/debug/typst --help
    ./target/debug/typst compile file.typ
```

`--release` 会更慢地编译但运行更快；仓库的 release profile 还开启了 `lto = "thin"`、`codegen-units = 1`、对 typst-cli `strip = true`（见工作区 [Cargo.toml:L163-L168](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/Cargo.toml#L163-L168)）。

> 小贴士：首次 debug 构建会较慢，因为工作区为依赖包设了 `opt-level = 2`（[Cargo.toml:L156-L157](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/Cargo.toml#L156-L157)），换取后续运行/测试的速度。

#### 4.4.3 源码精读

README 的「Contributing」段就给出了官方构建步骤：

- [README.md:L200-L210](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/README.md#L200-L210)：`git clone` → `cd typst` → `cargo build --release`，并说明优化产物在 `target/release/`。

`--version` 输出的内容来自 [args.rs:L49-L63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L49-L63) 对 `CliArguments` 的 clap 注解：

```rust
#[clap(
    name = "typst",
    version = format!("{} ({})", typst_utils::version().raw(),
                      typst_utils::display_commit(typst_utils::version().commit())),
    ...
)]
pub struct CliArguments { ... }
```

- [args.rs:L50-L63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L50-L63)：`name = "typst"` 决定帮助头是 `Typst {version}`；`version` 由 `typst_utils::version()` 的版本号 + commit 哈希拼成。所以 `typst --version` 会形如 `typst 0.15.1 (<commit>)`（具体以本地构建为准）。

#### 4.4.4 代码实践

**实践目标**：亲手把 typst-cli 构建成 `typst` 命令并运行。

**操作步骤**：

1. 确认已安装 Rust（`rustc --version`，应 ≥ 1.92）。
2. 在工作区根执行 `cargo build`（或 `cargo build --release`）。首次会拉取并编译大量依赖，请耐心等待。
3. 构建完成后运行：
   ```sh
   ./target/debug/typst --version
   ./target/debug/typst --help
   ```
4. 记录 `--version` 打印的版本号与 commit，记录 `--help` 列出的子命令。

**需要观察的现象**：

- `--version` 形如 `typst 0.15.1 (<commit-hash>)`。
- `--help` 顶部显示 `Typst <version>` 与 `usage`，随后列出可见子命令，末尾有「Resources」链接（来自 [args.rs:L36-L43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L36-L43) 的 `AFTER_HELP`）。

**预期结果**：你得到一个可用的 `typst` 可执行文件，并能对照 [main.rs:L68-L82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L68-L82) 的 `dispatch()` 确认帮助里每个子命令对应的处理模块。如果本机尚未装好 Rust 或构建超时，相关输出标注为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`./target/debug/typst` 与 `./target/release/typst` 有何区别？什么时候该用哪个？

<details>
<summary>参考答案</summary>

`debug` 是开发构建，编译快但运行慢、体积大；`release` 是优化构建（开启 `lto`、`codegen-units=1`、`strip`），编译慢但运行快、体积小。学习/调试源码用 debug；真正大量编译文档或分发时用 release。
</details>

**练习 2**：`typst --version` 显示的版本号是从哪里来的？

<details>
<summary>参考答案</summary>

来自工作区 [Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/Cargo.toml#L6-L9) 的 `version = "0.15.1"`，经 `typst_utils::version().raw()` 在 [args.rs:L53-L57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L53-L57) 拼上 commit 哈希后，由 clap 注入到 `--version` 输出。
</details>

---

## 5. 综合实践

把本讲四块知识串起来，完成下面的「端到端认知」任务：

1. **定位**：在工作区根列出 `crates/` 目录，指出哪个是编译器核心、哪个是 CLI，并用一句话说明它们的依赖方向（CLI 依赖核心）。
2. **读清单**：打开 [crates/typst-cli/Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml)，回答：
   - 最终二进制为什么叫 `typst`？（指给 `[[bin]]` 行）
   - 默认开启了哪两个 feature？（指给 `default` 行）
   - 哪个 feature 控制自更新？它额外拉入了哪三个可选依赖？
3. **构建运行**：执行 `cargo build`，然后运行 `./target/debug/typst --version` 与 `./target/debug/typst --help`。
4. **对照分发**：把 `--help` 里看到的每个可见子命令，逐一映射到 [main.rs:L68-L82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L68-L82) `dispatch()` 中对应的处理模块；并解释为什么 `query` 和（默认 feature 下的）`update` 没出现在帮助里。
5. **记录**：把版本号、子命令清单、以及「子命令 → 模块」对照表整理成一份笔记，作为后续讲义的参照基线。

> 如果构建受限于网络或机器资源，第 3、4 步的运行结果可标注「待本地验证」，但第 1、2、5 步的源码阅读与整理必须完成。

## 6. 本讲小结

- typst-cli 是 Typst workspace 里的**二进制 crate**，产出用户运行的 `typst` 命令；它依赖编译器核心 `typst` 与各导出格式 crate，本身是「组装车间」。
- 工作区 [Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/Cargo.toml) 把 typst-cli 设为 `default-members`，所以在仓库根直接 `cargo build` 即可构建。
- [Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/Cargo.toml) 的 `[[bin]] name = "typst"` 决定了产物名；`[features]` 用 `default = ["embedded-fonts", "http-server"]` 控制默认能力，`self-update` 按需引入 `self-replace`/`zip`/`xz2`。
- [main.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs) 顶部声明全部子模块；`dispatch()` 是 9 个子命令到处理模块的映射表。
- `main()` 处理 SIGPIPE、错误打印与退出码；首次无参运行会触发 [greet.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/greet.rs) 的欢迎信息。
- 默认 feature 下 `query`（弃用）和 `update`（未启用）被隐藏，不出现在 `--help`。

## 7. 下一步学习建议

本讲建立了「typst-cli 是什么、怎么跑起来」的全局视图。接下来建议：

- **u1-l2《入口与命令分发》**：深入 `main()` 与 `dispatch()` 的细节，理解 `ARGS` 的惰性解析、`set_failed`/`print_error`/`print_hint` 与 `thread_local EXIT` 退出码机制，以及 greet 的「按版本只问候一次」逻辑。
- **u1-l3《命令行参数模型》**：系统学习 [args.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs) 如何用 clap 派生宏定义 `CliArguments`/`Command`/`CompileCommand` 等结构、自定义值解析器，以及 `OutputFormat`/`Pages` 等枚举。
- 在进入进阶层（u2）之前，务必完成本讲的「构建并运行」实践——后续讲义会频繁假设你手头已经有一个可运行的 `typst` 二进制。
