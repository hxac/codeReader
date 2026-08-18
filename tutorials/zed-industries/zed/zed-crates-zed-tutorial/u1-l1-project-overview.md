# 第 1 讲：项目定位与 crate 全景

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `crates/zed` 在整个 zed 仓库中的位置：它是一个**组装型二进制 crate**，把 160 多个功能 crate 拼装成一个可运行的编辑器应用。
- 读懂它的 `Cargo.toml`：包信息、两个二进制目标、平台条件依赖、dev-dependencies。
- 理解三个关键 feature 开关（`test-support`、`visual-tests`、`tracy`）各自控制什么。
- 理解 `RELEASE_CHANNEL` 文件（当前内容是 `dev`）与 `release_channel` crate 之间的关系，以及四个 bundle 配置（dev / nightly / preview / stable）的差异。

本讲**不读**任何 `.rs` 文件的主体逻辑——那是后面几讲的事。本讲只解决一个问题：这个 crate 是什么、由什么组成、有哪些开关。

## 2. 前置知识

### 2.1 什么是 crate

在 Rust 中，**crate** 是一次编译的基本单位，分两类：

- **库 crate（library）**：编译成库供别人 `use`，不产生可执行文件。
- **二进制 crate（binary）**：编译成可执行程序。`crates/zed` 就是这一类——它最终产出名为 `zed` 的可执行文件。

### 2.2 什么是 Cargo workspace

一个仓库里可以有很多 crate，通过根目录 `Cargo.toml` 中的 `[workspace]` 组织在一起。workspace 共享一份依赖版本表（`[workspace.dependencies]`），子 crate 用 `foo.workspace = true` 表示「我用 workspace 统一管理的那个版本」。zed 仓库就是一个巨大的 workspace，`crates/` 目录下有上百个成员。

### 2.3 什么是 feature

feature 是 Cargo 提供的**编译期开关**。声明 `[features]` 之后：

- 可以用 `cargo build --features xxx` 打开。
- feature 可以「传递启用」依赖 crate 的 feature，例如 `"visual-tests" = ["gpui/test-support"]` 表示打开 zed 的 `visual-tests` 时，同时打开 `gpui` crate 的 `test-support`。
- 标记为 `optional = true` 的依赖只有被某个 feature 引用时才会编译进来。

### 2.4 Zed 是什么

Zed 是一个用 Rust 编写的高性能协作代码编辑器。仓库里的功能（编辑器、终端、LSP、AI 助手、Git 集成……）分别实现在独立的 crate 中，而 `crates/zed` 负责「临门一脚」：初始化全局状态、创建窗口、注册 action，把它们变成一个能双击打开的应用。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [Cargo.toml](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml) | zed crate 的「身份证 + 装配清单」：包信息、feature、依赖、二进制目标、打包元数据，共 317 行 |
| [RELEASE_CHANNEL:L1](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/RELEASE_CHANNEL#L1) | 只有一个单词 `dev` 的文本文件，由 `release_channel` crate 在编译期读取 |
| [src/main.rs:L359-L386](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L359-L386) | 程序入口。本讲只看它**使用** release channel 的两处（单实例检查、崩溃处理器），不看启动流程 |
| [../release_channel/src/lib.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/release_channel/src/lib.rs) | （仓库内其他 crate）定义 `ReleaseChannel` 枚举，并在编译期 `include_str!` 读取上面的 RELEASE_CHANNEL 文件 |
| [../../Cargo.toml:L265](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/Cargo.toml#L265) | （仓库根）workspace 定义，注意 `default-members = ["crates/zed"]` |

> 提示：后两个链接在 `crates/zed` 之外，但属于同一仓库同一提交，链接同样有效。

## 4. 核心概念与源码讲解

### 4.1 crate 定位与依赖全景

#### 4.1.1 概念说明

把 zed 仓库想象成一家大型工厂：

- 每个 `crates/<名字>` 车间生产一个**零件**：`editor` 生产编辑器、`project` 管理工程与文件树、`theme` 管理主题、`terminal_view` 提供终端界面……
- `crates/zed` 是**总装车间**：它几乎没有「业务逻辑」，而是做三件事——
  1. 在 `main()` 里完成进程级启动（初始化、日志、会话恢复）；
  2. 在 `zed.rs` 里把窗口、状态栏、面板、action 装配起来；
  3. 通过打开协议（`zed://` URL、CLI）接收外部请求。

一个直接证据是它的依赖数量：`[dependencies]` 段有 160 多条，几乎每个功能 crate 都出现在这里。另一个证据是 workspace 根的配置——`default-members = ["crates/zed"]`，意味着在仓库根直接执行 `cargo build` / `cargo run`，默认构建的就是这个总装 crate。

#### 4.1.2 核心流程

从「克隆仓库」到「得到可执行文件」的过程：

1. Cargo 读取仓库根 `Cargo.toml` 的 `[workspace]`，解析全部成员及统一依赖版本表。
2. 因为 `default-members = ["crates/zed"]`，默认目标就是 `crates/zed`。
3. Cargo 读取 `crates/zed/Cargo.toml`：
   - `[package]` 提供包名与版本；
   - `[features]` 决定哪些可选代码参与编译；
   - `[[bin]]` 声明「从哪个源文件生成哪个可执行文件」。
4. 编译 `src/main.rs` → 产出 `zed` 可执行文件（`default-run = "zed"` 保证 `cargo run` 运行的是它）。

#### 4.1.3 源码精读

**包基本信息**——注意 `default-run`，它决定了 `cargo run -p zed` 启动哪个二进制：

[Cargo.toml:L1-L9](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L1-L9)

```toml
[package]
description = "The fast, collaborative code editor."
edition.workspace = true
name = "zed"
version = "1.17.0"
publish.workspace = true
license = "GPL-3.0-or-later"
authors = ["Zed Team <hi@zed.dev>"]
default-run = "zed"
```

这段声明了：包名 `zed`、版本 `1.17.0`、GPL-3.0-or-later 许可证。

**两个二进制目标**：

[Cargo.toml:L56-L63](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L56-L63)

```toml
[[bin]]
name = "zed"
path = "src/main.rs"

[[bin]]
name = "zed_visual_test_runner"
path = "src/visual_test_runner.rs"
required-features = ["visual-tests"]
```

- `zed`：主程序，无条件构建。
- `zed_visual_test_runner`：可视化（截图）测试运行器，`required-features = ["visual-tests"]` 表示**只有**打开 `visual-tests` feature 时它才会被构建（详见 4.2）。

**依赖全景**：`[dependencies]` 段（[Cargo.toml:L65-L231](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L65-L231)）约 166 条，几乎全部使用 `.workspace = true` 继承 workspace 统一版本。按职责可以粗略归类：

| 类别 | 代表依赖（均真实存在于 Cargo.toml） | 大致职责 |
| --- | --- | --- |
| UI 框架 | `gpui`、`gpui_platform`、`ui`、`component` | 自研 UI 框架与组件库 |
| 工作区 | `workspace`、`sidebar`、`project_panel`、`title_bar` | 窗口/工作区/侧栏结构 |
| 编辑体验 | `editor`、`language`、`search`、`outline`、`vim` | 编辑器核心、语法、搜索、VIM 模式 |
| 工程与文件 | `project`、`fs`、`paths`、`remote`、`dev_container` | 工程模型、文件系统、远程开发 |
| AI 能力 | `agent`、`agent_ui`、`language_models`、`copilot`、`edit_prediction` | 助手、语言模型、补全 |
| 终端与运行 | `terminal_view`、`repl`、`task`、`tasks_ui`、`node_runtime` | 终端、REPL、任务系统 |
| 基础设施 | `settings`、`theme`、`client`、`telemetry`、`release_channel`、`zlog` | 配置、主题、网络、遥测、日志 |
| 打开协议 | `cli`、`install_cli`、`zed_actions` | CLI 与 zed:// URL 协作 |

其中几条带有额外 feature，值得留意：

- [Cargo.toml:L123-L124](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L123-L124)：`gpui` 打开了 `profiler`，`gpui_platform` 打开了 `screen-capture`、`font-kit`、`wayland`、`x11`——总装 crate 按最终产品的需要为零件 crate 「加装选配」。
- [Cargo.toml:L152](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L152)：`languages` 打开了 `load-grammars`，因为发布的应用需要内建语法。

**平台条件依赖**（同一份 Cargo.toml 按操作系统裁剪）：

- Windows 段 [Cargo.toml:L233-L239](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L233-L239)：额外依赖 `etw_tracing`（Windows 事件跟踪）、`windows`，并给 `gpui` 加 `windows-manifest` feature。
- Linux/FreeBSD 段 [Cargo.toml:L244-L251](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L244-L251)：额外依赖 `ashpd`（桌面门户，用于系统对话框等）与 `image`，并给 `gpui` 加 `wayland`、`x11`。

#### 4.1.4 代码实践

**实践目标**：亲手完成一次「crate 体检」，建立自己的依赖地图，供后续讲义验证。

**操作步骤**：

1. 打开 [Cargo.toml](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml)，从头到尾通读一遍（只有 317 行，其中近一半是依赖清单）。
2. 在笔记中列出两个二进制目标：

   | 二进制名 | 源文件 | 构建条件 |
   | --- | --- | --- |
   | `zed` | `src/main.rs` | 无条件 |
   | `zed_visual_test_runner` | `src/visual_test_runner.rs` | 需要 `visual-tests` feature |

3. 从 `[dependencies]` 中挑选 3 个你感兴趣的 crate（建议选 `workspace`、`gpui`、`project`），为每个写下一句话猜测：「我猜它负责______，依据是名字/我在 UI 里见过的功能」。

**需要观察的现象**：依赖清单的规模与分类——你会发现几乎每条 `.workspace = true` 都对应仓库 `crates/` 下一个同名目录。

**预期结果**：得到一张类似下表的猜测表（示例答案见 4.1.5）：

| 依赖 crate | 我的猜测 |
| --- | --- |
| `workspace` | ？ |
| `gpui` | ？ |
| `project` | ？ |

本实践纯源码阅读，无需编译，可直接完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cargo run` 在仓库根目录就能启动 Zed 主程序？

**参考答案**：仓库根 `Cargo.toml` 中 `default-members = ["crates/zed"]`（[根 Cargo.toml:L265](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/Cargo.toml#L265)）使 `crates/zed` 成为默认构建目标；同时该 crate 的 `[package]` 里 `default-run = "zed"`（[Cargo.toml:L9](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L9)）指定 `cargo run` 运行 `zed` 这个二进制目标而不是 `zed_visual_test_runner`。

**练习 2**：`zed` crate 自己实现了语法高亮吗？

**参考答案**：没有。语法能力在 `language` / `languages` 等 crate 中实现，zed 只是依赖它们（并给 `languages` 打开 `load-grammars` feature，[Cargo.toml:L152](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L152)）。这体现了「总装 crate」的定位：业务在零件 crate，装配在 zed。

**练习 3**：`[[bin]]` 与 `[bin]`（单括号）有什么区别？

**参考答案**：双括号 `[[bin]]` 是 TOML 的「数组中的表」语法，表示可以声明**多个**二进制目标（zed 就有两个）；单括号只能声明一个。与之类似的有 `[[test]]`、`[[example]]`。

### 4.2 feature 开关：test-support、visual-tests 与 tracy

#### 4.2.1 概念说明

`crates/zed` 声明了 4 个 feature：`tracy`、`track-project-leak`、`test-support`、`visual-tests`。它们解决的问题是：**同一份代码，要在「正式产品」「性能剖析」「自动化测试」「截图测试」四种形态之间切换，又不想让正式版本背上这些包袱**。

两类机制要分清：

- **传递型 feature**（`test-support`、`visual-tests`）：自己不对应 zed 内部多少代码，主要作用是给一串依赖 crate 打开同名 feature，让那些 crate 编入测试辅助代码。
- **选配型 feature**（`tracy`、`track-project-leak`）：打开额外的剖析/诊断能力。

#### 4.2.2 核心流程

feature 生效的链路：

1. `cargo build/test --features xxx`（或由其他 feature 传递）打开 zed 的某 feature。
2. 该 feature 的声明列表被 Cargo 展开：
   - `"dep:image"` 这类条目把对应的 `optional = true` 依赖真正纳入编译；
   - `"workspace/test-support"` 这类条目同时启用依赖 crate 的 feature。
3. 源码中 `#[cfg(feature = "...")]` 标注的代码块参与或退出编译。
4. `required-features` 的二进制目标：feature 未开 → 该目标**根本不会构建**。

#### 4.2.3 源码精读

**完整的 feature 声明**：

[Cargo.toml:L14-L17](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L14-L17)

```toml
[features]
tracy = ["ztracing/tracy"]
# LEAK_BACKTRACE=1 cargo run --features zed/track-project-leak --profile release-fast
track-project-leak = ["gpui/leak-detection"]
```

- `tracy`：接入 [Tracy](https://github.com/wolfpld/tracy) 性能剖析器的开关，实际打开的是 `ztracing` crate 的 `tracy` feature。
- `track-project-leak`：泄漏检测开关。注意它**自带使用示例注释**——`LEAK_BACKTRACE=1 cargo run --features zed/track-project-leak --profile release-fast`，这是仓库里罕见的「feature 用法文档」。

**test-support**（传递给 11 个依赖 crate）：

[Cargo.toml:L18-L30](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L18-L30)

```toml
test-support = [
    "gpui/test-support",
    "gpui_platform/screen-capture",
    "dep:image",
    "workspace/test-support",
    "project/test-support",
    "editor/test-support",
    ...
]
```

它把 `gpui`、`workspace`、`project`、`editor` 等一批 crate 的测试辅助代码打开。这些辅助代码支撑了 zed 源码里大量 `#[gpui::test]` 标注的测试（例如 [src/zed.rs:L2983](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L2983) 起的测试模块）。

**visual-tests**（列表更长，含可选依赖）：

[Cargo.toml:L31-L54](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L31-L54)

```toml
visual-tests = [
    "gpui/test-support",
    "gpui_platform/screen-capture",
    "gpui_platform/test-support",
    "dep:image",
    "dep:tempfile",
    "dep:action_log",
    "dep:agent_servers",
    ...
]
```

注意 `dep:image`、`dep:tempfile`、`dep:action_log`、`dep:agent_servers`——这些依赖在 `[dependencies]` 里标记了 `optional = true`（例如 [Cargo.toml:L133](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L133) 的 `tempfile`），只有开 feature 才编译。`screen-capture` 是截图测试的前提——要对比截图，先得能截图。

**feature 与二进制目标的联动**：`required-features = ["visual-tests"]`（[Cargo.toml:L63](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L63)）保证 `zed_visual_test_runner` 只在截图测试形态下存在。而 zed 自己源码中对 feature 的直接 `cfg` 引用非常少，目前可见的一处是：

[src/visual_test_runner.rs:L502](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/visual_test_runner.rs#L502)

```rust
#[cfg(feature = "visual-tests")]
```

这印证了 4.2.1 的判断：zed 的 feature 绝大多数是「给依赖 crate 递开关」，而不是给自己内部的代码分区。

#### 4.2.4 代码实践

**实践目标**：亲眼验证 `visual-tests` feature 对构建产物的影响。

**操作步骤**：

1. 执行 `cargo check -p zed`（不带 feature），观察输出中出现的二进制目标。
2. 再执行 `cargo check -p zed --features visual-tests`。
3. 对比两次输出中 `zed_visual_test_runner` 是否出现。
4. 想看依赖如何被点亮，可执行 `cargo tree -p zed -e features -i tempfile`，观察 `tempfile` 是被哪个 feature 引入的。

**需要观察的现象**：不带 feature 时只有 `zed` 一个目标被检查；带 `visual-tests` 时多出 `zed_visual_test_runner`；`cargo tree` 显示 `tempfile` 的引入路径包含 `visual-tests`。

**预期结果**：与上述现象一致。注意：zed 依赖树庞大，`cargo check` 首次可能耗时很久（磁盘与网络取决于环境），**待本地验证**；若机器性能有限，步骤 1-2 可只执行到解析依赖阶段观察目标列表即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `image`、`tempfile` 这些依赖要标 `optional = true`？

**参考答案**：因为只有测试/截图测试形态需要它们。声明为可选后，正式发布的 `zed` 二进制完全不会编译这些 crate，缩短编译时间、减小产物体积；它们只在 `test-support` / `visual-tests` feature 打开时通过 `dep:image` 等条目激活。

**练习 2**：`"gpui/test-support"` 和 `"dep:image"` 两种写法的区别是什么？

**参考答案**：`"gpui/test-support"` 表示「启用依赖 `gpui` 并打开它的 `test-support` feature」；`"dep:image"` 只把可选依赖 `image` 纳入编译而不打开它的任何 feature。`dep:` 前缀是较新的 Cargo 语法，用于显式指名「依赖本身」，避免与同名隐式 feature 混淆。

**练习 3**：如果你想在 release 版本里排查内存泄漏，应该怎么构建？

**参考答案**：按 [Cargo.toml:L16](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L16) 的注释：`LEAK_BACKTRACE=1 cargo run --features zed/track-project-leak --profile release-fast`。该 feature 打开 `gpui/leak-detection`，环境变量 `LEAK_BACKTRACE=1` 控制是否输出泄漏处的回溯。

### 4.3 release channel 与 bundle 元数据

#### 4.3.1 概念说明

Zed 有四条并行的**发布通道（release channel）**：

| channel | 用途 |
| --- | --- |
| `dev` | 本地开发自建版本（**当前仓库默认**） |
| `nightly` | 每日自动构建的尝鲜版 |
| `preview` | 即将进入稳定版的新功能预览 |
| `stable` | 正式稳定版 |

为什么要分通道？因为它们要能**在同一台机器上共存**：图标不同、应用标识不同、数据目录不同，互不干扰。你甚至能同时装着 Zed Dev 调试自己的改动，同时用 Zed Stable 干活。

支撑这个体系的三个角色：

1. **`RELEASE_CHANNEL` 文件**：一个单词的文本文件，声明「这棵源码树默认属于哪个通道」。
2. **`release_channel` crate**：在**编译期**读入该文件，换算成 `ReleaseChannel` 枚举，供运行时查询。
3. **`[package.metadata.bundle-*]`**：四个打包配置段，告诉打包工具每个通道用什么图标、应用标识和名称。

#### 4.3.2 核心流程

通道值的确定与消费链路：

```text
crates/zed/RELEASE_CHANNEL 文件（内容 "dev"）
        │  编译期 include_str! 嵌入
        ▼
release_channel::RELEASE_CHANNEL_NAME（字符串，LazyLock）
        │  ReleaseChannel::from_str 解析
        ▼
release_channel::RELEASE_CHANNEL（枚举：Dev/Nightly/Preview/Stable）
        │  运行时被各处消费
        ├── 单实例检查是否执行（Dev 通道跳过，可多开调试）
        ├── 崩溃处理器是否安装、上报时携带通道名
        ├── poll_for_updates：Dev 不检查更新
        └── docs_url / app_id / display_name 等展示差异
```

细节：**debug 构建**（`cfg!(debug_assertions)`）下还可以用环境变量 `ZED_RELEASE_CHANNEL` 临时覆盖通道名；release 构建则完全以编译期嵌入的值为准。另外，构建脚本（`build.rs`，下一讲精读）也监听 `RELEASE_CHANNEL` 环境变量变化（[build.rs:L205](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/build.rs#L205)），CI 会用它配合 `bundle-*` 元数据产出不同通道的安装包。

#### 4.3.3 源码精读

**一切的原点**——这个文件只有一行：

[RELEASE_CHANNEL:L1](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/RELEASE_CHANNEL#L1)

```text
dev
```

**编译期读取**（注意 `include_str!` 的相对路径从 `crates/release_channel/src/` 出发，`../../zed/RELEASE_CHANNEL` 正好落在 zed crate 的这个文件上）：

[../release_channel/src/lib.rs:L13-L19](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/release_channel/src/lib.rs#L13-L19)

```rust
pub static RELEASE_CHANNEL_NAME: LazyLock<String> = LazyLock::new(|| {
    if cfg!(debug_assertions) {
        env::var("ZED_RELEASE_CHANNEL").unwrap_or_else(|_| compile_time_release_channel_name())
    } else {
        compile_time_release_channel_name()
    }
});
```

这段代码定义通道名的取值规则：debug 构建先看环境变量 `ZED_RELEASE_CHANNEL`，没有再用编译期嵌入的值。

[../release_channel/src/lib.rs:L21-L34](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/release_channel/src/lib.rs#L21-L34)

```rust
#[cfg(__do_not_set_zed_release_channel)]
fn compile_time_release_channel_name() -> String {
    env!("ZED_RELEASE_CHANNEL").trim().to_string()
}

#[cfg(not(__do_not_set_zed_release_channel))]
fn compile_time_release_channel_name() -> String {
    include_str!("../../zed/RELEASE_CHANNEL").trim().to_string()
}
```

两个分支的区别是为 nix `crane` 单独 vendored 每个 crate 的构建场景准备的特殊路径（此时 `include_str!` 找不到文件，改由构建脚本通过 `cfg` 开关注入环境变量值）；常规构建永远走 `include_str!` 那一支。

**字符串 → 枚举**：

[../release_channel/src/lib.rs:L37-L41](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/release_channel/src/lib.rs#L37-L41)

```rust
#[doc(hidden)]
pub static RELEASE_CHANNEL: LazyLock<ReleaseChannel> =
    LazyLock::new(|| match ReleaseChannel::from_str(&RELEASE_CHANNEL_NAME) {
        Ok(channel) => channel,
        _ => panic!("invalid release channel {}", *RELEASE_CHANNEL_NAME),
    });
```

枚举本体是四值 `ReleaseChannel`（[../release_channel/src/lib.rs:L139-L154](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/release_channel/src/lib.rs#L139-L154)），且 `Dev` 是 `#[default]`——即使文件缺失/解析失败也有安全默认值（不过这里 `from_str` 失败会直接 panic，`default` 更多服务于直接构造场景）。

**运行时消费点**（zed 主程序里）：

- **单实例检查**：

[src/main.rs:L359-L379](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L359-L379)

```rust
let failed_single_instance_check = if *zed_env_vars::ZED_STATELESS
    || *release_channel::RELEASE_CHANNEL == ReleaseChannel::Dev
{
    false
} else {
    // Linux/FreeBSD: unix socket；Windows: 单实例事件；macOS: ensure_only_instance
    ...
};
```

Dev 通道**跳过**单实例检查——开发者经常需要同时跑多个实例对比行为，这个分支就是给他们的。

- **崩溃处理**：[src/main.rs:L385-L386](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L385-L386) 用 `should_install_crash_handler(*release_channel::RELEASE_CHANNEL)` 决定是否安装崩溃处理器，[src/main.rs:L401](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L401) 把 `RELEASE_CHANNEL_NAME` 一并塞进崩溃上报数据。

**四个 bundle 配置对比**（打包元数据，每个通道一段）：

[Cargo.toml:L284-L314](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L284-L314)

| 字段 | bundle-dev | bundle-nightly | bundle-preview | bundle-stable |
| --- | --- | --- | --- | --- |
| `name` | Zed Dev | Zed Nightly | Zed Preview | Zed |
| `identifier` | `dev.zed.Zed-Dev` | `dev.zed.Zed-Nightly` | `dev.zed.Zed-Preview` | `dev.zed.Zed` |
| `icon` | `app-icon-dev*.png` | `app-icon-nightly*.png` | `app-icon-preview*.png` | `app-icon*.png` |
| `osx_url_schemes` | `zed` | `zed` | `zed` | `zed` |
| `osx_minimum_system_version` | 10.15.7 | 10.15.7 | 10.15.7 | 10.15.7 |

四个通道的 `identifier` 与 `release_channel` crate 中 `app_id()` 的返回值（[../release_channel/src/lib.rs:L228-L235](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/release_channel/src/lib.rs#L228-L235)）一一对应——这就是「同一台机器共存」的机制：标识不同，操作系统就把它们当成四个不同的应用；数据目录也按通道区分（例如 Linux 下的 unix socket 路径带通道名，见 `open_listener.rs` 中 `zed-{}.sock` 的用法，第 3 单元会详讲）。

目录中还有一批与通道对应的资源可作为旁证：`resources/` 下的 `app-icon-dev.png`、`app-icon-nightly.png`、`app-icon-preview.png`、`app-icon.png`（stable 不带后缀），以及 macOS 签名用的 `contents/dev/`、`contents/nightly/`、`contents/preview/`、`contents/stable/` 四个 `embedded.provisionprofile`。

#### 4.3.4 代码实践

**实践目标**：亲手验证「RELEASE_CHANNEL 文件 → 编译期嵌入 → 运行时展示」这条链路。

**操作步骤**：

1. **验证 include_str! 路径**：`release_channel` crate 的源码位于 `crates/release_channel/src/lib.rs`，其中 `include_str!("../../zed/RELEASE_CHANNEL")` 的相对路径基于**该源文件所在目录**。请在仓库中手动走一遍：`crates/release_channel/src/` → 上两级 → `crates/release_channel/` 的兄弟目录中的 `zed/RELEASE_CHANNEL`。确认它指向的就是我们读过的那个只含 `dev` 的文件。
2. **验证 debug 覆盖**：阅读 [../release_channel/src/lib.rs:L13-L19](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/release_channel/src/lib.rs#L13-L19)，回答：在 debug 构建下设置 `ZED_RELEASE_CHANNEL=nightly` 会不会改变通道？（答案见练习 1。）
3. **（可选，需编译）**：执行 `cargo run -p zed -- --system-specs`，观察输出中的 release channel 字段；再试 `ZED_RELEASE_CHANNEL=nightly cargo run -p zed -- --system-specs` 对比差异。`--system-specs` 模式的输出由 [src/main.rs:L311-L321](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L311-L321) 打印，其中传入了 `*release_channel::RELEASE_CHANNEL`。

**需要观察的现象**：步骤 1-2 是纯阅读，立即可做；步骤 3 中两次输出的 channel 字段应分别为 `dev` 与 `nightly`。

**预期结果**：链路与 4.3.2 的流程图一致。步骤 3 依赖完整编译 zed（耗时可能很长），**待本地验证**；若无法编译，用步骤 1-2 的静态推导代替即可。

#### 4.3.5 小练习与答案

**练习 1**：debug 构建下 `ZED_RELEASE_CHANNEL=nightly` 会改变通道吗？release 构建呢？

**参考答案**：会/不会。`RELEASE_CHANNEL_NAME` 的构造逻辑（[../release_channel/src/lib.rs:L13-L19](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/release_channel/src/lib.rs#L13-L19)）在 `cfg!(debug_assertions)` 为真时优先读环境变量；release 构建直接用编译期 `include_str!` 嵌入的值（即 RELEASE_CHANNEL 文件内容），环境变量无效。

**练习 2**：为什么 Dev 通道要跳过单实例检查？

**参考答案**：见 [src/main.rs:L359-L363](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L359-L363)。开发者常常需要同时启动多个 Zed 实例（对比改动、多窗口调试），单实例检查会让第二个进程直接退出，妨碍开发。正式通道保持单实例是为了点击 dock 图标/打开文件时聚焦既有窗口，而不是不断开新进程。

**练习 3**：如果把 RELEASE_CHANNEL 文件内容改成 `banana`，会发生什么？

**参考答案**：`ReleaseChannel::from_str` 只接受 `dev`/`nightly`/`preview`/`stable`（[../release_channel/src/lib.rs:L269-L281](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/release_channel/src/lib.rs#L269-L281)），`banana` 解析失败，`RELEASE_CHANNEL` 这个 `LazyLock` 首次被访问时 panic：`invalid release channel banana`（[../release_channel/src/lib.rs:L37-L41](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/release_channel/src/lib.rs#L37-L41)）。编译能通过（`include_str!` 只要求文件存在），问题在运行时暴露。

## 5. 综合实践

**任务：为 `crates/zed` 建立一份「crate 档案卡」**，把本讲三个模块的产出合并成一份可长期维护的笔记（建议存为 `zed-crates-zed-tutorial/my-notes/u1-l1-crate-profile.md`，与讲义分开管理）。

档案卡必须包含四部分：

1. **基本信息**：包名、版本、许可证、`default-run`、workspace 中的 `default-members` 地位。
2. **二进制目标表**：两个 `[[bin]]` 的名称、源文件、构建条件。
3. **feature 表**：4 个 feature 各自的作用、典型命令（如 `--features visual-tests`）、涉及的 `optional` 依赖。
4. **通道矩阵**：四个通道的 `name`/`identifier`/`icon` 对照表，以及「RELEASE_CHANNEL 文件 → `include_str!` → `RELEASE_CHANNEL_NAME` → `RELEASE_CHANNEL` 枚举 → main.rs 消费点」的链路图（手画或文字版均可）。

完成标准：

- 表格内容全部来自 `Cargo.toml` 与 `release_channel/src/lib.rs` 的真实条目，不凭记忆填写。
- 在依赖猜测表（4.1.4 步骤 3）旁留一列「验证结果」，标记「待第 X 讲验证」——后续学习对应模块时回填，让这份档案成为贯穿整本手册的索引。

## 6. 本讲小结

- `crates/zed` 是 zed 仓库这个大 workspace 的**总装二进制 crate**：160 多个依赖、`default-run = "zed"`、workspace 根的 `default-members` 都指向它。
- 它声明了**两个二进制目标**：主程序 `zed`（无条件）和截图测试运行器 `zed_visual_test_runner`（需要 `visual-tests` feature）。
- 四个 feature 中，`test-support` / `visual-tests` 主要是**给依赖 crate 传递测试开关并激活可选依赖**，`tracy` / `track-project-leak` 是性能与诊断选配。
- 发布通道体系由三件套构成：`RELEASE_CHANNEL` 文件（当前 `dev`）→ `release_channel` crate 编译期 `include_str!` 嵌入 → `ReleaseChannel` 枚举驱动运行时行为（Dev 跳过单实例检查、Dev 不轮询更新等）。
- 四个 `[package.metadata.bundle-*]` 段通过不同的 `identifier` / 图标 / 名称让四条通道在同一台机器上共存。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：构建与运行方式——精读 `build.rs` 看构建期发生了什么（Linux 图标解码、`rerun-if-env-changed`），了解 `resources/` 下各平台资源的作用，并动手编译运行 Zed。
- **延伸阅读**：仓库根 `Cargo.toml` 的 `[workspace.dependencies]` 段（用 Grep 抽查几个本讲出现的依赖名，感受统一版本管理）；`crates/release_channel/src/lib.rs` 全文（只有 300 行，是学习 LazyLock + 全局状态模式的好样本）。
- **回填提醒**：把 4.1.4 的依赖猜测表带在身边——`workspace` 在 u4 验证、`gpui` 贯穿 u4/u6、`project` 在 u2/u3 的打开链路中反复出现。
