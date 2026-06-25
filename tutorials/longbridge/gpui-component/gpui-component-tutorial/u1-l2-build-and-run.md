# 构建与运行：Story、Examples 与 Web

> 学习阶段：beginner · 依赖讲义：u1-l1（项目定位与核心特性总览）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `cargo run`（不带任何参数）在这个仓库里到底启动了什么，以及为什么。
2. 会用 `cargo run -- <story_name>` 让 Story Gallery 直接聚焦到某个组件演示。
3. 会用 `cargo run -p <name>` 单独运行 `examples/` 下的某个最小示例。
4. 说出 `crates/story-web` 是如何把整个 Gallery 编译成 WASM 并在浏览器里跑起来的。
5. 理解「workspace / default-members / members」这三个概念如何决定 cargo 的行为。

## 2. 前置知识

在动手之前，先建立三个最基础的概念，它们是理解本讲所有命令的钥匙。

### 2.1 什么是 Cargo Workspace（工作区）

Rust 的一个项目可以由多个 crate（可编译单元）组成。当一个项目里有多个互相依赖的 crate 时，通常会把它们放在一个 **workspace** 里统一管理：

- 一个根 `Cargo.toml` 用 `[workspace]` 声明自己是一个工作区。
- `members` 列出工作区里所有的 crate（包）。
- 所有成员共享同一个 `target/` 编译目录和同一份 `[workspace.dependencies]` 依赖版本声明，避免重复下载和版本冲突。

`gpui-component` 就是一个典型的多 crate workspace：核心库 `crates/ui`、展示程序 `crates/story`、Web 版 `crates/story-web`、宏 `crates/macros` 等都在同一个 workspace 里。

### 2.2 `default-members` 决定了 `cargo run` 启动谁

当你在工作区根目录直接敲 `cargo run`，cargo 需要知道「默认编译并运行哪个 crate」。这个默认目标由 `default-members` 指定。理解这一点，就能解释为什么在本仓库里 `cargo run` 启动的不是核心库 `ui`，而是 `story` 展示程序。

### 2.3 本地运行 GUI 的前提

`gpui-component` 是一个**桌面 GUI 库**，它的程序运行时需要打开真实窗口。这意味着：

- 在本地（你的笔记本 / 台式机）运行时，需要一个图形环境（macOS、带桌面的 Linux、Windows 都可以）。
- 在纯命令行 / 无显示的服务器（例如很多云端 CI 容器）里运行，窗口无法显示，但这不影响我们「读源码、理解构建流程」。
- 因此本讲里凡是「运行后会弹出窗口」的命令，如果你当前没有图形环境，请标注「待本地验证」，先理解原理即可。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [`Cargo.toml`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/Cargo.toml) | 根工作区配置：`members`、`default-members`、共享依赖、编译 profile。 |
| [`crates/story/src/main.rs`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/main.rs) | Story Gallery 的程序入口（`cargo run` 默认启动它）。 |
| [`crates/story/src/lib.rs`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/lib.rs) | 提供 Gallery 用到的 `init`、`create_new_window` 等函数。 |
| [`crates/story/src/gallery.rs`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/gallery.rs) | `Gallery` 结构与「所有组件演示列表」，决定 `cargo run -- <name>` 的可选名字。 |
| [`examples/README.md`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/examples/README.md) | 说明 `examples/` 目录「一个示例只讲一个特性」的约定。 |
| [`examples/hello_world/Cargo.toml`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/examples/hello_world/Cargo.toml) | 最小示例 `hello_world` 的包配置。 |
| [`examples/hello_world/src/main.rs`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/examples/hello_world/src/main.rs) | 最小示例的程序入口。 |
| [`crates/story-web/Cargo.toml`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story-web/Cargo.toml) | Web 版 Gallery 的包配置（WASM）。 |
| [`crates/story-web/Makefile`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story-web/Makefile) | Web 版的构建命令封装（`make dev` / `make build-prod`）。 |
| [`crates/story-web/scripts/build-wasm.sh`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story-web/scripts/build-wasm.sh) | 真正的 WASM 编译脚本（cargo + wasm-bindgen 两步）。 |
| [`crates/story-web/src/lib.rs`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story-web/src/lib.rs) | WASM 入口函数 `run()`，被前端 JS 调用。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 workspace 成员与 default-members**：搞清楚「cargo 到底在编译谁」。
- **4.2 Story Gallery 入口**：`cargo run` 与 `cargo run -- <story_name>`。
- **4.3 独立 example 列表**：`cargo run -p <name>` 跑单个示例。
- **4.4 Web Gallery 的 WASM 构建**：把 Gallery 编译进浏览器。

### 4.1 workspace 成员与 default-members

#### 4.1.1 概念说明

我们要回答一个看似简单、却很容易踩坑的问题：**在仓库根目录敲 `cargo run`，到底发生了什么？**

答案是：cargo 会查看根 `Cargo.toml` 里的 `default-members`，找到默认要编译运行的那个（或那些）crate，然后编译并运行它的 `main` 函数。

注意区分三个相关字段：

| 字段 | 含义 |
| --- | --- |
| `members` | 工作区里**所有**可被编译的 crate 清单（含示例）。 |
| `default-members` | 在**不指定 `-p`** 时，`cargo build` / `cargo run` 默认针对的 crate 子集。 |
| `[workspace.dependencies]` | 所有成员共享的依赖版本声明，成员在自己的 `Cargo.toml` 里用 `xxx.workspace = true` 引用。 |

#### 4.1.2 核心流程

```text
你在根目录敲 `cargo run`
        │
        ▼
读取根 Cargo.toml 的 default-members
        │
        ▼
default-members = ["crates/ui", "crates/story", "crates/assets"]
        │
        ▼
发现其中只有 `crates/story` 有 main.rs（可执行目标）
        │
        ▼
编译并运行 crates/story → 弹出 Story Gallery 窗口
```

`crates/ui` 是库（library，没有 `main`），`crates/assets` 是资源库，它们会被一起编译（因为 `story` 依赖它们），但「运行」的是 `story`。

#### 4.1.3 源码精读

根 [`Cargo.toml:L1-L22`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/Cargo.toml#L1-L22) 定义了 default-members 与 members：

```toml
[workspace]
default-members = ["crates/ui", "crates/story", "crates/assets"]
members = [
    "crates/macros",
    "crates/story",
    "crates/story-web",
    "crates/ui",
    "crates/assets",
    "crates/webview",
    "examples/app_assets",
    "examples/hello_world",
    # ... 其余 examples
]
```

这段代码说明：默认 `cargo run` 只针对 `crates/ui`、`crates/story`、`crates/assets` 三个，而真正可执行的是 `crates/story`。

另外，workspace 用一份 [`[workspace.dependencies]`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/Cargo.toml#L29-L41) 统一锁定关键依赖版本，其中最重要的是 GPUI 来自 Zed 仓库的某个 git 提交：

```toml
gpui = { git = "https://github.com/zed-industries/zed", rev = "1d217ee..." }
```

> 这一行解释了为什么第一次 `cargo build` 会比较慢：cargo 要从 Zed 的 git 仓库拉取 GPUI 源码并编译。这是正常的，不是出错。

还有一个对编译体验很重要的配置 [`[profile.dev.package]`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/Cargo.toml#L101-L116)。它对一批「重计算」的依赖（如 `gpui`、`tree-sitter`、`ropey`、`resvg`、`taffy` 等）单独开启 `opt-level = 3`。目的是：即使整体是 debug 构建，也把这些热点库以优化方式编译，让运行时（尤其是滚动、渲染）不至于太卡。这是这个项目「debug 模式也能用」的关键技巧之一。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认 `default-members` 的作用。
2. **操作步骤**：
   - 在仓库根目录执行 `cargo run`（首次会编译较久）。
3. **需要观察的现象**：
   - 终端先出现大量编译日志，最后弹出标题类似 `GPUI Component` 的窗口，里面是一个带左侧导航的组件画廊。
4. **预期结果**：弹出 Story Gallery 窗口。
5. **待本地验证**：如果你在无图形界面的服务器上，窗口无法显示，命令可能在尝试初始化显示时失败，此时请切到有桌面环境的机器验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `default-members` 改成只包含 `["crates/ui"]`，再敲 `cargo run` 会怎样？

> **答案**：`crates/ui` 是库，没有可执行目标，cargo 会报错提示「no bin target」，因为没有任何带 `main` 的 crate 在默认成员里。这正说明 default-members 决定了 `cargo run` 的目标。

**练习 2**：为什么 `examples/hello_world` 不在 `default-members` 里，却能被编译？

> **答案**：因为它在 `members` 列表里。`members` 决定「能否被编译」，`default-members` 只决定「不指定 `-p` 时默认编译谁」。所以你可以用 `cargo run -p hello_world` 显式运行它（见 4.3）。

---

### 4.2 Story Gallery 入口：main.rs 与 `cargo run -- <story>`

#### 4.2.1 概念说明

`crates/story` 是一个「组件画廊（Gallery）」程序：它把库里 60+ 组件各做一个演示页，集中在一个带左侧导航的窗口里，方便你像翻目录一样浏览和调试组件。这就是你运行 `cargo run` 看到的界面。

它还有一个很实用的隐藏能力：**可以通过命令行参数直接打开某个组件的演示页**。例如 `cargo run -- button` 会让 Gallery 一启动就聚焦到 Button 的演示。这个参数的「可选名字」就来自 Gallery 内部的组件列表。

#### 4.2.2 核心流程

```text
cargo run -- <story_name>
        │
        ▼
main() 读取 std::env::args().nth(1)  得到 <story_name>
        │
        ▼
application().with_assets(Assets)        // 加载图标/字体等内置资源
        │
        ▼
app.run 中调用 init(cx)                   // 初始化主题、字体等（u1-l4 会细讲）
        │
        ▼
create_new_window("GPUI Component", |w, cx| Gallery::view(name, w, cx), cx)
        │
        ▼
Gallery::view 把 name 传给 Gallery::new → set_active_story
        │
        ▼
把 name 写进左侧搜索框 → Gallery 自动滚动/过滤到对应组件演示
```

#### 4.2.3 源码精读

入口 [`crates/story/src/main.rs:L1-L20`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/main.rs#L1-L20) 全文很短，是理解整个 Gallery 启动的最佳起点：

```rust
use gpui_component_assets::Assets;
use gpui_component_story::{Gallery, init, create_new_window};

fn main() {
    let app = gpui_platform::application().with_assets(Assets);

    // Parse `cargo run -- <story_name>`
    let name = std::env::args().nth(1);

    app.run(move |cx| {
        init(cx);
        cx.activate(true);

        create_new_window(
            "GPUI Component",
            move |window, cx| Gallery::view(name.as_deref(), window, cx),
            cx,
        );
    });
}
```

逐行解读：

- [`let name = std::env::args().nth(1);`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/main.rs#L7-L8)：读取命令行第一个参数（`nth(0)` 是程序自身路径）。这就是 `cargo run -- button` 里那个 `button` 的来源。注意 `--` 是必须的，它把后面的参数交给程序而不是 cargo。
- [`create_new_window(...)`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/main.rs#L14-L18)：创建主窗口，窗口内容由闭包 `Gallery::view(name.as_deref(), ...)` 提供。

`Gallery::view` 的定义在 [`crates/story/src/gallery.rs:L132-L134`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/gallery.rs#L132-L134)，它把可选的名字传给 `new`：

```rust
pub fn view(init_story: Option<&str>, window: &mut Window, cx: &mut App) -> Entity<Self> {
    cx.new(|cx| Self::new(init_story, window, cx))
}
```

而 `new` 在 [`gallery.rs:L118-L120`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/gallery.rs#L118-L120) 里，当传入了名字就调用 `set_active_story`，本质上等价于把名字填进画廊的搜索框，让界面定位到对应组件：

```rust
if let Some(init_story) = init_story {
    this.set_active_story(init_story, window, cx);
}
```

那么「可选名字」到底有哪些？它们就来自 Gallery 内部硬编码的演示列表，例如 [`gallery.rs:L40-L52`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/gallery.rs#L40-L52) 列出了 "Components" 分组下的前几个：

```rust
(
    "Components",
    vec![
        StoryContainer::panel::<AccordionStory>(window, cx),
        StoryContainer::panel::<AlertStory>(window, cx),
        StoryContainer::panel::<AlertDialogStory>(window, cx),
        StoryContainer::panel::<AvatarStory>(window, cx),
        StoryContainer::panel::<BadgeStory>(window, cx),
        StoryContainer::panel::<BreadcrumbStory>(window, cx),
        StoryContainer::panel::<ButtonStory>(window, cx),
        // ...
    ],
),
```

可见 `Accordion`、`Alert`、`Avatar`、`Badge`、`Button` 等都是合法的 `-- <name>` 候选名。完整列表一直延伸到 [`gallery.rs:L107`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/gallery.rs#L107)。

> 注意：这里说的 `editor`、`markdown`、`html`、`dock` 等，**都是 Gallery 内部的演示页（Story），不是独立的 example 程序**。下一节的 `examples/` 才是独立可执行程序。两者容易混淆，务必区分。

#### 4.2.4 代码实践

1. **实践目标**：用命令行参数直接打开某个组件演示。
2. **操作步骤**：
   - 执行 `cargo run -- button`（注意 `--` 不能少）。
   - 再试一次 `cargo run -- markdown`。
3. **需要观察的现象**：
   - 窗口启动后，左侧导航自动选中对应组件，主区域直接显示它的演示，而不是停留在默认首页。
4. **预期结果**：分别看到 Button 演示页、Markdown 渲染演示页。
5. **待本地验证**：若当前无图形环境，请记录命令并留到本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么命令里必须写 `--`，直接 `cargo run button` 会失败？

> **答案**：`--` 之后的内容才会被传递给被运行的程序。如果不写 `--`，`button` 会被 cargo 当成自己的参数（如 `cargo run --release` 那种），导致解析失败或被忽略。

**练习 2**：如果传一个列表里不存在的名字，比如 `cargo run -- notexist`，会发生什么？

> **答案**：程序不会崩溃。`set_active_story` 只是把字符串填进搜索框；搜索没有匹配项时，画廊主区域会显示「无匹配」或保持空白，不会报错。

---

### 4.3 独立 example 列表：`cargo run -p <name>`

#### 4.3.1 概念说明

除了内容丰富的 Gallery，仓库还有一个 `examples/` 目录，里面是一组**「一个示例只讲一个特性」的最小程序**。它们和 Gallery 的定位不同：

- **Gallery（story）**：大而全，所有组件集中演示，适合浏览整体能力。
- **examples/ 下的示例**：小而专，每个都是独立的、可以单独编译运行的小程序，适合当作「如何从零搭一个最小应用」的模板。

最容易踩的坑是运行命令。这个仓库里 `examples/` 下的每个示例**都是一个独立的 workspace 成员包**（各自有 `Cargo.toml` 和包名），**不是**某个 crate 的 `[[example]]` 目标。因此：

- ❌ `cargo run --example hello_world`（在本仓库**不适用**，因为根本没有 `[[example]]` 目标）。
- ✅ `cargo run -p hello_world`（正确，`-p` 指定包名）。

> 提示：你可能在项目的 `CLAUDE.md` 里见过 `cargo run --example` 的写法，那是通用的 Rust 习惯说法；但本仓库的真实布局是「示例即独立包」，请以 `-p <name>` 为准。

#### 4.3.2 核心流程

```text
你想运行某个最小示例
        │
        ▼
查看 examples/<name>/Cargo.toml 里的 name = "<包名>"
        │
        ▼
cargo run -p <包名>
        │
        ▼
cargo 在 workspace members 里定位该包 → 编译它的 main.rs → 运行
```

#### 4.3.3 源码精读

先看示例目录的约定，[`examples/README.md:L3-L5`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/examples/README.md#L3-L5) 写得很清楚：

```
This folder contains basic examples of how to use the GPUI Component library.
Unlike the examples in the `story` folder, these examples focus on 1 example
for 1 feature, making it easier to understand and implement specific
functionalities in your own projects.
```

也就是说，「1 个示例 = 1 个特性」是这个目录的核心约定。

当前 `examples/` 下共有 12 个独立示例包（在根 [`Cargo.toml` 的 members L10-L22](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/Cargo.toml#L10-L22) 中逐一列出），它们的包名（即 `-p` 要用的名字）如下：

| 包名 | 大致用途 |
| --- | --- |
| `hello_world` | 最小应用：一个标题 + 一个按钮（本节精读）。 |
| `app_assets` | 演示如何加载自定义应用资源。 |
| `input` | 文本输入相关最小示例。 |
| `window_title` | 自定义窗口标题栏。 |
| `dialog_overlay` | 对话框 / 遮罩层用法。 |
| `webview` | 内嵌 WebView。 |
| `system_monitor` | 系统监控（数据展示类）。 |
| `focus_trap` | 焦点陷阱（模态焦点循环）。 |
| `tooltip_top_edge` | Tooltip 贴边定位。 |
| `sidebar` | 侧边栏导航。 |
| `text_selection` | 文本选择。 |
| `root_borderless` | 无边框窗口 + Root。 |

以 `hello_world` 为例，它的包配置 [`examples/hello_world/Cargo.toml:L1-L16`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/examples/hello_world/Cargo.toml#L1-L16) 非常精简：

```toml
[package]
name = "hello_world"
description = "A minimal example of application development with GPUI Component."
version = "0.5.1"
publish = false
edition.workspace = true

[dependencies]
anyhow.workspace = true
gpui.workspace = true
gpui_platform.workspace = true
gpui-component = { workspace = true }

[lints]
workspace = true
```

两点值得注意：

- `edition.workspace = true` 与 `xxx.workspace = true`：说明示例复用 workspace 统一配置，自己不写死版本号。
- 包名是 `hello_world`，所以运行命令是 `cargo run -p hello_world`。

它的入口 [`examples/hello_world/src/main.rs:L23-L41`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/examples/hello_world/src/main.rs#L23-L41) 给出了「从零搭一个最小 GPUI Component 应用」的标准骨架（具体每行的含义是 u1-l4 的主题，这里只需建立整体印象）：

```rust
fn main() {
    gpui_platform::application().run(move |cx| {
        // This must be called before using any GPUI Component features.
        gpui_component::init(cx);

        cx.spawn(async move |cx| {
            cx.open_window(WindowOptions::default(), |window, cx| {
                let view = cx.new(|_| Example);
                // This first level on the window, should be a Root.
                cx.new(|cx| {
                    Root::new(view, window, cx).bg(cx.theme().background)
                })
            })
            .expect("Failed to open window");
        })
        .detach();
    });
}
```

可以看出，它和 Story 的 `main.rs` 共享同一套套路：`application().run(...)` → `init(cx)` → `open_window(...)` → 用 `Root` 包裹第一个视图。

#### 4.3.4 代码实践

1. **实践目标**：单独运行一个最小示例，并与 Gallery 对比。
2. **操作步骤**：
   - 执行 `cargo run -p hello_world`。
3. **需要观察的现象**：
   - 弹出一个非常简洁的窗口，正中是 `Hello, World!` 文字和一个 `Let's Go!` 按钮；点击按钮在终端打印 `Clicked!`。
4. **预期结果**：一个只有一个按钮的最小窗口。
5. **待本地验证**：无图形环境时留到本地验证。

#### 4.3.5 小练习与答案

**练习 1**：怎样知道某个示例的「正确包名」是什么？

> **答案**：打开 `examples/<目录>/Cargo.toml`，看 `[package]` 段里的 `name = "..."`，那个值就是 `cargo run -p` 后面要写的包名。目录名和包名在本仓库恰好一致，但以 `name` 字段为准更稳妥。

**练习 2**：为什么不能把 `examples/hello_world` 从 `members` 里删掉？（思考题）

> **答案**：删掉后它就不再是 workspace 成员，`cargo run -p hello_world` 将无法在 workspace 内找到该包；同时它用到的 `gpui.workspace = true` 等共享依赖也就无法解析。`members` 是「能否编译」的总开关。

---

### 4.4 Web Gallery：WASM 构建流程

#### 4.4.1 概念说明

除了原生桌面程序，`gpui-component` 还能**整个编译成 WebAssembly（WASM）跑在浏览器里**。对应的 crate 是 `crates/story-web`，它把同一套 Gallery 搬到了网页上。项目还提供了在线 Demo（见其 [`README.md`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story-web/README.md) 里的 Live Demo 链接）。

WASM 构建的核心思路只有两步：

1. 用 `cargo build --target wasm32-unknown-unknown` 把 Rust 代码编译成 `.wasm`。
2. 用 `wasm-bindgen` 把 `.wasm` 转换成前端 JS 可以直接 `import` 的绑定文件。

之后再由一个前端工程（这里用 Bun + Vite）加载这些绑定、启动 WASM。

#### 4.4.2 核心流程

```text
make dev   （或 make build-prod）
        │
        ▼
scripts/build-wasm.sh
   ├─ Step1: cargo build --target wasm32-unknown-unknown [--release]
   │           → 产物 gpui_component_story_web.wasm
   └─ Step2: wasm-bindgen <wasm> --out-dir www/src/wasm --target web
              → 生成供前端 import 的 JS 绑定
        │
        ▼
前端 (Bun + Vite, www/) 加载绑定 → 调用 run() → 浏览器里渲染 Gallery
        │
        ▼
访问 http://localhost:3000 看到 Web 版组件画廊
```

#### 4.4.3 源码精读

先看 Web 包的配置 [`crates/story-web/Cargo.toml:L7-L15`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story-web/Cargo.toml#L7-L15)：

```toml
[lib]
crate-type = ["cdylib", "rlib"]

[dependencies]
gpui.workspace = true
gpui_platform.workspace = true
gpui-component = { path = "../ui", default-features = false }
gpui-component-assets.workspace = true
gpui-component-story = { path = "../story", default-features = false }
```

关键点：

- `crate-type = ["cdylib", ...]`：`cdylib` 是 WASM / 动态库需要的目标类型，没有它就编译不出 `.wasm`。
- `default-features = false`：因为浏览器里拿不到文件系统、原生网络等能力，所以要关掉桌面端默认特性，只用 WASM 兼容的子集。

真正的构建命令被封装在 [`crates/story-web/Makefile`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story-web/Makefile)，常用入口有：

```makefile
install:       ## 安装 WASM target + wasm-bindgen-cli + 前端依赖
build-wasm:    ## 用 release 模式编译 WASM
dev:           ## debug 编译 WASM 并启动 Vite 开发服务器
build-prod:    ## 完整生产构建（release WASM + 生产前端）
```

而 `make build-wasm` 背后调用的就是脚本 [`scripts/build-wasm.sh`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story-web/scripts/build-wasm.sh)。它分两步，[`L24-L26`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story-web/scripts/build-wasm.sh#L24-L26) 先编译成 WASM：

```bash
# Step 1: Build WASM
cd "$PROJECT_ROOT"
cargo build --target wasm32-unknown-unknown $RELEASE_FLAG
```

然后 [`L47-L50`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story-web/scripts/build-wasm.sh#L47-L50) 生成 JS 绑定：

```bash
# Step 2: Generate JavaScript bindings
wasm-bindgen "$WASM_PATH" \
    --out-dir "$PROJECT_ROOT/www/src/wasm" \
    --target web \
    --no-typescript
```

WASM 侧的入口函数是 [`crates/story-web/src/lib.rs:L9-L10`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story-web/src/lib.rs#L9-L10) 暴露给 JS 的 `run()`：

```rust
#[wasm_bindgen]
pub fn run() -> Result<(), JsValue> {
```

它的内部 [`lib.rs:L37-L68`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story-web/src/lib.rs#L37-L68) 做了三件浏览器特有的事：先 `web_init()` 初始化 WASM 下的 GPUI 平台（[`lib.rs:L19-L20`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story-web/src/lib.rs#L19-L20)），再 `include_bytes!` 内嵌 CJK / Emoji / 等宽字体（因为浏览器拿不到系统字体），最后 `cx.open_window(...)` 用同样的 `Gallery::view` 渲染出和桌面版一致的画廊。

> 注意：WASM 入口与桌面 `main.rs` 共用了同一个 `Gallery`，所以你在浏览器里看到的组件和本地运行 `cargo run` 看到的是**同一套演示**，只是渲染后端不同。这正是「一次编写，桌面/Web 通用」的体现。

补充：仓库根目录还有一个 [`Makefile:L1-L2`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/Makefile#L1-L2) 提供快捷入口，效果等同进到 `crates/story-web` 再 `make dev`：

```makefile
dev-web:
	cd crates/story-web && make dev
```

#### 4.4.4 代码实践

1. **实践目标**：在浏览器里跑起 Web 版 Gallery。
2. **操作步骤**（需先装好 Rust、[Bun](https://bun.sh)，并 `rustup target add wasm32-unknown-unknown`、`cargo install wasm-bindgen-cli`）：
   - 在仓库根目录执行 `make dev-web`（首次编译 WASM 较慢）。
3. **需要观察的现象**：
   - 终端提示 Vite 开发服务器启动，并在 `http://localhost:3000` 提供服务。
   - 用浏览器打开该地址，看到与桌面版一致的组件画廊。
4. **预期结果**：浏览器里出现可交互的 Gallery。
5. **待本地验证**：WASM 首次编译耗时较长，且依赖 Bun / wasm-bindgen-cli 工具链，请按 README 的 Prerequisites 准备后本地验证；若环境受限，可只阅读脚本理解流程。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `story-web` 要用 `crate-type = ["cdylib", ...]`？

> **答案**：`cdylib`（C 兼容动态库）是产生 `.wasm` 这种可被外部（JS/浏览器）加载的二进制所必需的目标类型。普通的 `bin` / `rlib` 无法被 `wasm-bindgen` 当作可调用的动态库处理。

**练习 2**：为什么 Web 版要把字体用 `include_bytes!` 内嵌进去？

> **答案**：浏览器沙箱里**无法访问操作系统字体**，若不内嵌字体，中文和等宽代码字体将无法显示。所以 Web 版自带了 Noto Sans SC（中文）、Noto Emoji、JetBrains Mono（代码）三套字体的子集。

---

## 5. 综合实践

**任务：把三种运行方式串起来，建立对「如何运行 gpui-component」的完整肌肉记忆。**

1. **桌面 Gallery**：在仓库根目录执行 `cargo run`，等编译完成后，浏览左侧导航，截图记录任意 3 个组件演示页面（例如 Button、Markdown、Table）。
2. **参数直达**：关掉窗口，改用 `cargo run -- button`，确认这次一启动就直接停在 Button 演示页。
3. **最小示例**：另起一个终端，执行 `cargo run -p hello_world`，对比它与 Gallery 的「重量级 vs 最小化」差异，并尝试再运行 `cargo run -p sidebar`。
4. **（可选，需 Web 工具链）**：执行 `make dev-web`，在浏览器中打开 `http://localhost:3000`，确认 Web 版 Gallery 与桌面版是同一套演示。

完成后，你应该能用一句话回答：**「在这个仓库里，分别用什么命令跑 Gallery、跑单个组件演示、跑最小示例、跑 Web 版？」**

> 答案速查：`cargo run`（Gallery）/ `cargo run -- <story>`（聚焦某组件）/ `cargo run -p <name>`（最小示例）/ `make dev-web`（Web 版）。

---

## 6. 本讲小结

- `cargo run`（不带参数）之所以启动 Story Gallery，是因为根 `Cargo.toml` 的 `default-members` 把 `crates/story` 列为默认可执行目标。
- `members` 列出了工作区**所有**可编译 crate（含 12 个示例）；`default-members` 只决定不指定 `-p` 时默认编译谁。
- Story Gallery 支持 `cargo run -- <story_name>` 直接聚焦某个组件演示，可选名字来自 `gallery.rs` 里的 Story 列表。
- `examples/` 下的每个示例都是**独立的 workspace 成员包**（非 `[[example]]`），用 `cargo run -p <包名>` 运行，而不是 `--example`。
- Web 版 Gallery 通过 `crates/story-web` 编译成 WASM：`cargo build --target wasm32-unknown-unknown` + `wasm-bindgen` 两步，再用 Bun/Vite 前端加载。
- 桌面版与 Web 版共用同一个 `Gallery`，只是渲染后端不同。

## 7. 下一步学习建议

- 本讲聚焦「怎么运行」，但运行时弹出的窗口、`init(cx)`、`Root::new` 等还没展开——这些正是下一讲 **u1-l3（仓库结构与 crate 划分）** 和 **u1-l4（应用入口、init 初始化与 Root）** 的主题。
- 在进入 u1-l4 之前，建议先做 **u1-l3**：对照 `crates/ui/src/lib.rs` 的 `pub mod` 列表，亲手画一张「核心库 / 展示库 / 宏库 / Web 库」的模块分类图，把本讲看到的 `ui`、`story`、`story-web`、`macros`、`assets` 在脑子里的位置摆清楚。
- 如果你对某个组件特别感兴趣，可以直接去 `gallery.rs` 的 Story 列表里找到对应的 `XxxStory`，再到 `crates/story/src/stories/` 下读它的源码——这是把「运行」和「源码」对应起来的最快路径。
