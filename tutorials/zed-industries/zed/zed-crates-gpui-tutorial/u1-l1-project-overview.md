# GPUI 是什么：项目定位与整体架构

> 本讲是 zed-crates-gpui 学习手册的第一讲。我们从零开始：不假设你读过 Zed 的任何代码，只要求你对 Rust 有基本了解。读完本讲，你应该能回答三个问题：GPUI 是什么？它对外提供哪几层编程界面？这个 crate 的代码是如何组织的？

## 1. 本讲目标

学完本讲，你应该能够：

1. 用自己的话说出 GPUI 的定位：一个 GPU 加速、混合立即/保留模式的 Rust UI 框架，由 Zed 编辑器团队开发，同时可独立用于其他应用。
2. 说出 GPUI 的三种编程层级（「语域」）：用 `Entity` 管理状态、用视图（`Render`）声明式构建 UI、用 `Element` 命令式控制渲染。
3. 理解 `gpui` 与 `gpui_platform` 两个 crate 的分工：前者是跨平台核心，后者是按操作系统选择后端的「便捷门面」。
4. 读懂 `Cargo.toml` 中的 feature 表，能说出 default features 中每个 feature 在 Linux 上分别启用了什么。
5. 建立 `src/gpui.rs`（crate 注册表）的心智地图：知道如何从 `pub use` 反查任意公开类型的定义文件。

## 2. 前置知识

本讲用到的概念都不难，但如果你对下面几点不熟悉，建议先补一补：

- **UI 框架的两种经典模式**：
  - *保留模式（retained mode）*：框架替你保存一棵 UI 树，你只修改其中的节点，框架负责刷新。典型代表是浏览器 DOM。
  - *立即模式（immediate mode)*：UI 树不保留，每一帧都由你的代码重新描述一遍该画什么。典型代表是 Dear ImGui。
  - GPUI 称自己为「混合模式」：元素树每帧重建（像立即模式），但状态放在实体里跨帧保留（像保留模式）。这个设计是理解后续所有讲义的基础。
- **Rust 基础**：trait 与泛型、`Rc` 引用计数、crate/module/`pub use`、Cargo 的 feature 机制、`#[cfg(...)]` 条件编译。
- **Cargo feature 是什么**：feature 是编译期开关。一个 feature 可以（a）打开源码里的 `#[cfg(feature = "...")]` 分支，（b）通过 `dep:xxx` 引入一个可选依赖，或者（c）联动打开其他 feature。
- **GPU 渲染的粗浅概念**：知道「显卡擅长批量绘制大量矩形/三角形」即可，本讲不需要图形学知识。

## 3. 本讲源码地图

本讲涉及的关键文件如下（路径均相对 `crates/gpui/`）：

| 文件 | 作用 |
| --- | --- |
| `README.md` | GPUI 的官方门面文档：定位、入门代码、三层 API 总览。它同时被 `src/gpui.rs` 第一行 `include_str!` 进 crate 文档。 |
| `Cargo.toml` | crate 元数据、feature 定义、依赖清单、示例注册。本讲的「feature 体系」主要读它。 |
| `src/gpui.rs` | crate 库根（注意 `[lib] path = "src/gpui.rs"`）：声明全部模块、扁平化重导出公开类型，还定义了 `AppContext`/`VisualContext` 等核心 trait。 |
| `../gpui_platform/Cargo.toml` | `gpui_platform` 的 feature 表：把 `wayland`/`x11`/`font-kit` 转发给各平台子 crate。 |
| `../gpui_platform/src/gpui_platform.rs` | `gpui_platform` 的全部实现：约 100 行，按 `cfg` 选择当前操作系统的平台实现。 |
| `build.rs` | 构建脚本：仅在 Windows 上根据 `windows-manifest` feature 嵌入清单资源。 |
| `src/platform.rs` | 平台抽象 trait 所在文件。本讲只看其中 `guess_compositor` 一小段，用于验证 feature 的实际作用。 |

## 4. 核心概念与源码讲解

### 4.1 模块一：README——GPUI 的定位与三种编程层级

#### 4.1.1 概念说明

GPUI 是 Zed 编辑器团队自研的 UI 框架。它的自我定义只有一句话，但信息量很大：

- **GPU accelerated（GPU 加速）**：所有像素最终通过 GPU 渲染，CPU 只负责构建「绘制场景」。
- **hybrid immediate and retained mode（混合立即/保留模式）**：元素树每帧重建，但应用状态放在实体中跨帧保留。
- **for Rust**：为 Rust 的所有权模型量身定做，大量使用 trait 和泛型。
- **designed to support a wide variety of applications**：虽然为 Zed 而生，但它发布在 crates.io 上（`Cargo.toml` 中 `publish = true`），可以被任何应用当作依赖。

README 用了一个社会语言学的术语「register（语域）」来描述 GPUI 的 API 分层：就像同一个人在不同场合会说不同正式程度的话，GPUI 允许你按需选择三档「说话方式」：

| 层级 | 名称 | 一句话概括 | 适合场景 |
| --- | --- | --- | --- |
| 第 1 层 | 实体状态管理（Entity） | 把应用状态放进由 GPUI 拥有的「实体」里，通过类似 `Rc` 的智能指针访问 | 任何需要跨模块共享、通信的状态 |
| 第 2 层 | 声明式视图（View / `Render`） | 实现 `Render` trait 的实体就是视图；每帧 GPUI 调用 `render` 方法，你用 `div` 等元素描述「应该长什么样」 | 绝大多数常规 UI |
| 第 3 层 | 命令式元素（Element） | 直接实现 `Element` trait，自己控制布局与绘制 | 性能敏感的巨型列表、代码编辑器这类需要完全掌控渲染的场景 |

关键在于：这三层不是三选一，而是**同一次开发里按需混用**。第 2 讲（u1-l2）你会看到 hello_world 只用到第 2 层；等到学习虚拟化列表时你会降到第 3 层。

#### 4.1.2 核心流程

一个 GPUI 独立应用的最小启动流程（伪代码）：

```text
gpui_platform::application()          # 1. 为当前操作系统选择平台后端，构造 Application
    .run(|cx: &mut App| {             # 2. 把控制权交给平台事件循环，run 的回调在启动时执行一次
        cx.open_window(               # 3. 打开一个窗口
            WindowOptions::default(),
            |window, cx| {            # 4. 在窗口里创建根视图（一个实现了 Render 的实体）
                cx.new(|_| HelloWorld)
            },
        )
    })

此后每一帧：
GPUI 调用根视图的 render()
  → 得到一棵元素树（div、text、img……）
  → 布局（taffy 引擎）与样式计算
  → 交给 GPU 变成像素
```

注意分工：`application()` 来自 `gpui_platform`，而 `App`、`open_window`、`cx.new`、`Render` 都来自 `gpui` 核心。这就是两个 crate 的第一次「露面」。

#### 4.1.3 源码精读

**① 定位宣言**

[README.md:L1-L4](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/README.md#L1-L4) 开门见山：「GPUI 是一个混合立即与保留模式、GPU 加速的 Rust UI 框架，旨在支持各种各样的应用」。这句是整个 crate 的定位句，也是本讲的标题来源。

**② 入门依赖与最小应用**

[README.md:L8-L13](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/README.md#L8-L13) 说明 GPUI 处于 pre-1.0、常有破坏性变更，并且引入依赖时需要同时加 `gpui` 与可选的 `gpui_platform`。这里第一次出现「一个框架拆成两个 crate」的迹象，4.3 节会解释原因。

[README.md:L15-L25](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/README.md#L15-L25) 给出最小应用：`gpui_platform::application().run(|cx: &mut App| { ... })`。注意这段代码是 README 的原文（属于项目自带示例），它演示了 4.1.2 流程图的第 1、2 步。

**③ gpui_platform 的平台差异说明**

[README.md:L27-L43](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/README.md#L27-L43) 逐平台说明 feature 选择：macOS 只需 `font-kit`（Metal 渲染永远可用）；Linux/FreeBSD 至少开 `wayland`、`x11` 之一（这些 feature 同时编译渲染器与文本系统）；Windows 无需任何 feature（Win32 窗口 + DirectWrite 文本）。这份说明是 4.2 节 feature 分析的官方依据。

**④ The Big Picture：三种「语域」**

[README.md:L74-L84](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/README.md#L74-L84) 是 README 最核心的一段，逐条对应 4.1.1 表格的三层：

- [README.md:L78](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/README.md#L78) 第 1 层：实体由 GPUI 拥有，只能通过「类似 `Rc` 的智能指针」访问——这句话预告了 u2 单元的所有权模型。
- [README.md:L80](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/README.md#L80) 第 2 层：视图就是实现了 `Render` 的实体；每帧开始时 GPUI 调用根视图的 render 方法；视图构建元素树、用「Tailwind 风格的 API」布局上色，再交给 GPUI 变成像素。
- [README.md:L82](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/README.md#L82) 第 3 层：元素是 UI 的积木，包装了一套命令式 API，对自身和子元素的渲染有完全控制权，适合做高效大列表、自定义布局的编辑器等。
- [README.md:L84](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/README.md#L84) 收尾一句很重要：**每一层都有对应的 context（上下文），context 是你与 GPUI 各项服务交互的主接口**。这预告了 u2-l3 的 Context 家族。

**⑤ 其他资源**

[README.md:L86-L96](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/README.md#L86-L96) 列出框架的其余服务：Action（把按键翻译成逻辑操作）、平台服务（退出应用、打开 URL 等都是 `App` 上的方法）、与平台事件循环集成的异步执行器，以及 `#[gpui::test]` 测试宏与 `TestAppContext`。这些分别是 u5、u7 单元的主角，本讲只需混个脸熟。

#### 4.1.4 代码实践

**实践目标**：亲手从 README 原文中提取三层 API 的定义，而不是听讲义转述。

**操作步骤**：

1. 打开 `README.md`，定位到 `## The Big Picture` 一节（约第 74 行起）。
2. 准备一张三行表格（纸或笔记软件均可），列头为「层级 / 关键词 / README 原句 / 对应的 trait 或类型」。
3. 逐条填写：
   - 第 1 层：从 L78 提取，关键词 `Entity`；
   - 第 2 层：从 L80 提取，关键词 `Render`、`div`；
   - 第 3 层：从 L82 提取，关键词 `element`。
4. 再读 [README.md:L17-L25](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/README.md#L17-L25) 的最小应用，把其中出现的 `application()`、`run`、`App` 三个名字标注到你的表格里（它们属于第 1 层的入口设施）。

**需要观察的现象**：你会发现在最小应用代码里完全看不到第 2、3 层的身影——因为视图创建被放在 `open_window` 的回调里，README 为了简洁省略了。

**预期结果**：得到一张三行四列的表格，例如第 2 行是「第 2 层 / 声明式视图 / "A view is simply an Entity that can be rendered…" / `Render` trait + `div()` 函数」。

**待本地验证**：无。本实践是纯阅读任务，不依赖运行环境。

#### 4.1.5 小练习与答案

**练习 1**：README 说 GPUI 是「混合立即与保留模式」。结合 4.1.1 的解释，说出「立即」的部分和「保留」的部分分别是什么。

<details>
<summary>参考答案</summary>

「立即」指元素树：每帧都从根视图的 `render()` 重新构建，元素本身不跨帧保存。「保留」指应用状态：状态存放在 GPUI 拥有的实体（`Entity<T>`）中，跨帧存活，元素树只是状态的瞬时投影。
</details>

**练习 2**：如果你想给 Zed 写一个代码编辑器风格的巨型虚拟列表，应该主要用哪一层 API？为什么？

<details>
<summary>参考答案</summary>

主要用第 3 层（命令式 Element）。README L82 明确说元素「对自身和子元素的渲染有完全控制权，可用于做出高效的大列表视图、为代码编辑器实现自定义布局」。第 2 层的 `div` 声明式方式在条目数量巨大时会一次性构建整棵元素树，需要靠 `uniform_list`/`list` 这类基于第 3 层实现的元素来虚拟化（u6 单元会专门讲）。
</details>

**练习 3**：README L8 说 GPUI「pre-1.0、版本间常有不兼容变更」。这对你在自己的项目里引用 GPUI 意味着什么？

<details>
<summary>参考答案</summary>

意味着升级版本可能需要改代码，应把 GPUI 版本锁定（例如 `version = "=0.2.2"` 或固定某个 commit），并在升级时留意破坏性变更；同时需要使用最新稳定版 Rust（README 原文要求）。
</details>

### 4.2 模块二：Cargo.toml——feature 体系与平台依赖

#### 4.2.1 概念说明

`Cargo.toml` 是 crate 的「户口本」。对 `gpui` 来说，它回答三件事：

1. **这个 crate 是谁**：[Cargo.toml:L1-L13](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L1-L13) 里，包名 `gpui`、版本 `0.2.2`、描述「Zed's GPU-accelerated UI framework」、主页 `https://gpui.rs`、许可证 Apache-2.0，并且 `publish = true`——它是一个正式发布到 crates.io 的开源库，不是 Zed 的私有内部件。
2. **库根在哪里**：[Cargo.toml:L42-L44](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L42-L44) 写着 `[lib] path = "src/gpui.rs"`。这也是本仓库的惯例：不用默认的 `lib.rs`，而用与 crate 同名的文件（Zed 的 CLAUDE.md 也明确要求新 crate 这样做）。
3. **哪些能力可以被开关**：`[features]` 段。UI 框架必须跑在四种操作系统（macOS/Windows/Linux 系/wasm）上，不同系统的窗口、渲染、文本后端完全不同，feature 就是这些差异的「总开关面板」。

#### 4.2.2 核心流程

feature 生效的两条路径：

```text
路径 A（cfg 开关）：feature "x11" 被启用
  → 源码里所有 #[cfg(feature = "x11")] 的分支参与编译
  → 例如 guess_compositor() 才会去读 DISPLAY 环境变量

路径 B（可选依赖）：feature "windows-manifest" = ["dep:embed-resource"]
  → 构建脚本获得 embed_resource 这个工具
  → 但 build.rs 里还套了一层 target_os == "windows" 判断，Linux 上照样不干活
```

default features 一共四个，在 Linux 桌面上的实际效果如下（这张表就是本讲实践任务的答案底稿，请你自己推导一遍后再对照）：

| default feature | 在 `gpui` crate 内、Linux 上的直接效果 | 证据 |
| --- | --- | --- |
| `font-kit` | **没有效果**。它启用的可选依赖 `zed-font-kit` 只声明在 `target_os = "macos"` 的依赖段里，Linux 目标根本不会解析这个依赖；gpui 源码中也不存在 `cfg(feature = "font-kit")`。Linux 的文本系统走的是 `gpui_linux → gpui_wgpu` 路径，`gpui_wgpu` 的 `font-kit` feature 由 `gpui_linux` 无条件带上。 | [Cargo.toml:L114-L124](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L114-L124)、[../gpui_linux/Cargo.toml:L63](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_linux/Cargo.toml#L63) |
| `wayland` | 打开源码中的 Wayland 分支。`wayland = []` 是空 feature，本身不引入依赖，纯粹是 cfg 开关；例如运行时才会读取 `WAYLAND_DISPLAY` 环境变量来判断合成器。 | [Cargo.toml:L32](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L32)、[src/platform.rs:L102-L105](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/platform.rs#L102-L105) |
| `x11` | 打开 X11 分支（读取 `DISPLAY`）；若同时启用 `screen-capture`，还会联动给 `scap`（屏幕捕获库）打开它的 x11 支持（`"scap?/x11"` 里的 `?` 表示「scap 存在才联动」）。 | [Cargo.toml:L33-L35](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L33-L35)、[src/platform.rs:L107-L110](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/platform.rs#L107-L110) |
| `windows-manifest` | **没有效果**。它只引入可选构建依赖 `embed-resource`，而 `build.rs` 里 `embed_resource()` 仅在 `target_os == "windows"` 时调用，作用是把 Windows 清单资源嵌进二进制。 | [Cargo.toml:L39](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L39)、[build.rs:L8-L11](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/build.rs#L8-L11) |

其余 feature（非 default）速览：`test-support`（测试基建，连带 leak 检测与 wayland/x11）、`bench`（基准测试，加 criterion）、`inspector`（元素检查器）、`leak-detection`（实体泄漏检测）、`screen-capture`（屏幕捕获，引入 scap）、`profiler`（性能剖析，引入 hdrhistogram 并编译 `debug_overlay` 模块）。见 [Cargo.toml:L21-L40](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L21-L40)。

**为什么 default 要塞四个平台的开关？** 因为 `gpui` 发布在 crates.io，`gpui = { version = "*" }` 一行就该在三大桌面系统上开箱即用。真正的重活由 `gpui_platform` 承担：它的 feature 表（[../gpui_platform/Cargo.toml:L14-L21](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_platform/Cargo.toml#L14-L21)）把 `wayland`/`x11` 转发给 `gpui_linux`，`font-kit` 转发给 `gpui_macos`；而按目标平台引入哪个平台 crate，则由 target 配置段决定（[../gpui_platform/Cargo.toml:L26-L37](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_platform/Cargo.toml#L26-L37)）：macOS 用 `gpui_macos`，Windows 用 `gpui_windows`，Linux/FreeBSD 用 `gpui_linux`，wasm 用 `gpui_web`。`gpui_linux` 的 feature 再拉入真正的窗口库：`wayland` 一路启用 `wayland-client`、`wayland-protocols` 等，`x11` 一路启用 `x11rb`、`xim`、`x11-clipboard` 等（[../gpui_linux/Cargo.toml:L17-L46](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_linux/Cargo.toml#L17-L46)）。

**依赖体系**（学习目标之三）中值得记名字的几个：

| 依赖 | 作用 | 位置 |
| --- | --- | --- |
| `taffy = "=0.13.0"` | Rust 编写的 flexbox/grid 布局引擎，GPUI 布局阶段的心脏（u4-l2 专讲） | [Cargo.toml:L93](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L93) |
| `accesskit` | 无障碍访问抽象层，让屏幕阅读器可以遍历 UI（u6-l8 专讲） | [Cargo.toml:L47](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L47) |
| `lyon = "1.0"` | 2D 路径曲线细分（tessellation），`Path` 图元渲染的基础（u4-l4 专讲） | [Cargo.toml:L99](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L99) |
| `refineable` | 样式「细化叠加」过程的宏支持，`StyleRefinement` 的根基（u3-l3 专讲） | [Cargo.toml:L77](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L77) |
| `resvg` / `usvg` | SVG 解析与渲染 | [Cargo.toml:L79-L80](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L79-L80) |
| `slotmap` | 带稳定键的容器，`EntityMap` 用它存实体（u2-l2 专讲） | [Cargo.toml:L87](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L87) |
| `etagere` | 纹理图集装箱分配器（sprite atlas） | [Cargo.toml:L57](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L57) |
| `ctor` + `inventory` | 链接期收集与构造函数注册，`actions!` 宏自动注册 Action 靠它们（u5-l3 专讲） | [Cargo.toml:L55](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L55)、[Cargo.toml:L64](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L64) |
| `gpui_macros` | 过程宏 crate：`Render`、`IntoElement`、`test`、`bench` 等 derive/属性宏 | [Cargo.toml:L60](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L60) |

此外 [Cargo.toml:L177-L268](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L177-L268) 用 23 个 `[[example]]` 段显式注册了示例（首尾分别是 `hello_world` 和 `view_example`）；`examples/` 目录下共有 40 多个示例，配套说明见 [examples/README.md](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/README.md)（u1-l4 会专门导览）。

#### 4.2.3 源码精读（feature 定义逐行）

[Cargo.toml:L19-L40](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L19-L40) 是完整的 `[features]` 段，逐行读：

```toml
default = ["font-kit", "wayland", "x11", "windows-manifest"]   # L20 四平台开箱即用
test-support = [ "leak-detection", ..., "wayland", "x11", "proptest" ]  # L21-28 测试需要窗口后端
bench = ["test-support", "profiler", "dep:criterion"]          # L29 基准测试
inspector = ["gpui_macros/inspector"]                          # L30 联动宏 crate
leak-detection = ["backtrace"]                                 # L31
wayland = []                                                   # L32 空 feature：纯 cfg 开关
x11 = ["scap?/x11"]                                            # L33-35 scap 存在时联动
screen-capture = ["scap"]                                      # L36-38
windows-manifest = ["dep:embed-resource"]                      # L39 可选构建依赖
profiler = ["dep:hdrhistogram"]                                # L40
```

再看 feature 的「消费端」，两处对照：

- [src/platform.rs:L95-L119](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/platform.rs#L95-L119) 的 `guess_compositor()`：只有开启 `wayland` feature 才会读取 `WAYLAND_DISPLAY`（L102-105），只有开启 `x11` 才会读取 `DISPLAY`（L107-110）。这是「feature → cfg → 运行时行为」的完整链路实例。
- [build.rs:L8-L23](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/build.rs#L8-L23) 的构建脚本：`target_os == "windows"` 且 feature `windows-manifest` 开启时才调用 `embed_resource::compile`，把 `resources/windows/gpui.manifest.xml` 嵌入产物。这就是「windows-manifest 在 Linux 上无效果」的直接证据。

#### 4.2.4 代码实践

**实践目标**：验证（而不只是相信）default features 在 Linux 上的实际效果，形成一份可复查的笔记。

**操作步骤**：

1. 在 Zed 仓库根目录执行（只构建 gpui 库本身，不牵扯示例的 dev-dependencies）：

   ```sh
   cargo tree -p gpui -f "{p} [{f}]"
   ```

   这会打印依赖树中每个包及其启用的 feature 列表（`-f` 是 cargo-tree 的自定义格式参数）。
2. 在输出中搜索 `taffy`、`accesskit`、`lyon`，确认核心依赖已被拉入。
3. 再执行：

   ```sh
   cargo tree -p gpui -i font-kit
   ```

   `-i`（inverse）反查谁依赖 `font-kit`。在 Linux 上预期显示该包未被解析（因为可选依赖只在 macOS target 声明），这正好印证 4.2.2 表格的第一行。
4. 把 4.2.2 的四行表格抄进笔记，每行在「证据」列补上你自己的观察（命令输出摘录或源码行号）。

**需要观察的现象**：`cargo tree` 的输出里能看到 `gpui` 节点自带的 feature 标签；`-i font-kit` 的结果与平台相关。

**预期结果**：Linux 上（待本地验证——具体输出格式随 cargo 版本略有差异）：步骤 3 应报告找不到 `font-kit` 包或显示无反向依赖，说明该 feature 在本平台是空操作；步骤 2 能看到 taffy/accesskit/lyon 出现在树中。

**重要提醒**：不要试图用 `cargo run -p gpui --example hello_world --no-default-features` 来做这个实验——示例的 dev-dependencies 里 `gpui_platform` 带了 `font-kit`/`wayland`/`x11` feature（[Cargo.toml:L151](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L151)），cargo 的 feature 合并会把它们加回来，你会得到误导性的结果。用 `cargo tree` 观察是最干净的。

#### 4.2.5 小练习与答案

**练习 1**：`wayland = []` 是一个空 feature。既然它不引入任何依赖，为什么还要存在？

<details>
<summary>参考答案</summary>

它是纯编译期开关（cfg 开关面板）。gpui 核心源码中有大量 `#[cfg(feature = "wayland")]` 分支（如 `platform.rs` 中读取 `WAYLAND_DISPLAY`），同时它还能被下游 crate（`gpui_platform` → `gpui_linux`）用 `gpui/wayland` 语法反向联动，让 `gpui_linux` 在启用 Wayland 后端时自动打开核心 crate 里对应的 cfg 分支。空 feature 就是「让别人开我源码里的开关」的钩子。
</details>

**练习 2**：`x11 = ["scap?/x11"]` 里的 `?` 是什么意思？

<details>
<summary>参考答案</summary>

`?` 表示「可选依赖联动」：只有当 `scap` 这个依赖本身被启用（例如同时开了 `screen-capture` feature）时，才把 scap 的 `x11` feature 一并打开；如果 scap 没被引入，这条联动静默跳过，不会报错。这样屏幕捕获功能才能在 X11 会话下正确工作。
</details>

**练习 3**：如果你要写一个只在 Windows 上运行的 GPUI 应用，依赖应该怎么写最精简？

<details>
<summary>参考答案</summary>

参考 README L43 与 gpui_platform 的 Cargo.toml：Windows 上窗口用 Win32、文本用 DirectWrite，`font-kit` 无效、`wayland`/`x11` 无意义。但注意 `gpui_platform` 在 Windows target 上会自动带上 `gpui/windows-manifest`（[../gpui_platform/Cargo.toml:L29-L31](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_platform/Cargo.toml#L29-L31)），所以最省心的写法是 `gpui_platform = { version = "*", default-features = false }`（Windows 无需任何 feature），再配 `gpui = { version = "*", default-features = false }`。
</details>

### 4.3 模块三：crate 注册表——gpui.rs 模块树与 gpui/gpui_platform 分工

#### 4.3.1 概念说明

`src/gpui.rs` 只有 352 行，却是整个 crate 的「注册表」：所有模块在这里登记（`mod`），所有公开类型在这里扁平化导出（`pub use ... *`）。读懂它，你就能在 40 多个模块、近 7 万行代码里快速定位任何东西。

它有四个值得注意的设计：

1. **README 即文档**：[src/gpui.rs:L1](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L1) 用 `#![doc = include_str!("../README.md")]` 把 README 直接当作 crate 级文档——所以 docs.rs 上 gpui 的首页就是这份 README。
2. **自我别名**：[src/gpui.rs:L7](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L7) 的 `extern crate self as gpui;` 让 crate 内部代码可以用 `gpui::xxx` 的绝对路径引用自己，子模块里的路径写法与外部用户完全一致。
3. **扁平化导出**：`pub use app::*;`、`pub use elements::*;` 等把各模块的类型全部倒进 crate 根，用户只需 `use gpui::*`（或 `use gpui::prelude::*`，见 L38 的 `pub mod prelude`）就能拿到 `Entity`、`div`、`Window` 等一切。
4. **文档专用模块**：[src/gpui.rs:L69-L72](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L69-L72) 用 `#[cfg(doc)]` 声明了 `_accessibility` 和 `_ownership_and_data_flow` 两个「只在生成文档时存在」的模块，对应 `src/_accessibility.rs` 与 `src/_ownership_and_data_flow.rs` 两份官方指南（后者是 u2 单元的重要读物）。

而 **`gpui` 与 `gpui_platform` 的分工**可以这样理解：

- `gpui`：跨平台的核心——状态模型、元素系统、布局、样式、输入抽象、`Platform` trait 的定义。它自己不知道怎么开窗口。
- `gpui_platform`：100 行左右的「便捷门面」，用 `#[cfg(target_os = ...)]` 在编译期挑好平台实现（`MacPlatform` / `WindowsPlatform` / `gpui_linux::current_platform` / `WebPlatform`），替你省掉自己写 cfg 的麻烦。它的自述就说得明白：「re-exports GPUI's platform traits and the `current_platform` constructor so consumers don't need `#[cfg]` gating」。

#### 4.3.2 核心流程

**如何从 `gpui.rs` 找到任意公开类型的定义文件**（这是本模块要练成的手艺）：

```text
想知道 gpui::div 是什么
  1. 在 src/gpui.rs 的 pub use 区找 div 可能来自哪一行
     → L104: pub use elements::*;      （elements 模块的导出）
  2. 打开 src/elements/mod.rs，找 div 的 re-export
     → 它来自 src/elements/div.rs
  3. 阅读真正实现

同法：
  Entity  → L95  pub use app::*;        → src/app/ 下的模块
  Render  → L109-111 从 gpui_macros 重导出的 derive 宏（trait 本体在 src/element.rs）
  Window  → L166 pub use window::*;     → src/window.rs
```

模块树按职能分组（登记处在 [src/gpui.rs:L10-L64](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L10-L64)）：

| 分组 | 模块（行号为登记处） | 职责 |
| --- | --- | --- |
| 状态与上下文 | `app` (L12)、`executor` (L25)、`global` (L30)、`subscription` (L55)、`view` (L63) | 实体、上下文、全局状态、异步任务——u2 单元 |
| 声明式 UI | `element` (L23)、`elements` (L24)、`style` (L53)、`styled` (L54)、`color` (L18)、`colors` (L20)、`geometry` (L28) | 元素 trait、div 等内置元素、样式——u3 单元 |
| 交互 | `action` (L11)、`input` (L31)、`interactive` (L33)、`key_dispatch` (L34)、`keymap` (L35)、`gestures` (L29)、`tab_stop` (L57) | 事件、动作、键位、焦点导航——u5 单元 |
| 渲染底层 | `scene` (L50)、`path_builder` (L36)、`taffy` (L58)、`arena` (L14)、`bounds_tree` (L16)、`spring` (L52) | 绘制场景、路径、布局引擎、每帧状态存续——u4 单元 |
| 平台与服务 | `platform` (L37)、`platform_scheduler` (L26)、`text_system` (L61)、`assets` (L16)、`asset_cache` (L15)、`svg_renderer` (L56)、`shared_uri` (L51) | 平台抽象 trait、文本系统、资源——u6/u7 单元 |
| 调试与测试 | `inspector` (L32)、`profiler` (L40)、`debug_overlay` (L21-22，仅 profiler feature)、`test` (L59-60，仅 test)、`queue` (L41-49，仅部分平台/test) | 工程化工具——u7 单元 |

注意这张表里也体现了条件编译：`debug_overlay` 只在 `profiler` feature 下编译，`test` 只在 `cfg(test)` 或 `test-support` 下编译，`queue` 模块只在 Windows/Linux/wasm/test 下导出（L148-149 的 `pub use queue::...` 同样带条件）。

#### 4.3.3 源码精读

**① 模块登记与条件编译**

[src/gpui.rs:L10-L64](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L10-L64) 这 50 多行是整个 crate 的目录页。注意第 10-11 行 `#[macro_use] mod action;`——`actions!` 宏通过 `macro_use` 对全 crate 可见，这是老式宏的声明方式；而现代的过程宏则从 L109-111 重导出：`pub use gpui_macros::{AppContext, IntoElement, Render, VisualContext, bench, property_test, register_action, test};`——你在 Zed 代码里常见的 `#[derive(IntoElement)]`、`#[gpui::test]` 都来自这里。

**② 扁平化导出区**

[src/gpui.rs:L90-L166](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L90-L166) 是「注册表」的主体：90 行 `pub use accesskit;` 把无障碍库原样转手给用户；95 行 `pub use app::*;`、103 行 `pub use element::*;`、104 行 `pub use elements::*;`、166 行 `pub use window::*;` 等把各模块类型倒进根命名空间。也有只对内开放的：96 行 `pub(crate) use arena::*;`、158 行 `pub(crate) use tab_stop::*;`——每帧元素状态存续的 arena 和 tab 停靠元素是内部设施，不对外承诺 API。

**③ 注册表里也住着核心 trait**

[src/gpui.rs:L170-L245](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L170-L245) 定义了 `AppContext` trait——「让 GPUI 的各种 context 可以互换使用」的公共接口：`new`/`reserve_entity`/`insert_entity`/`update_entity`/`read_entity`/`update_window`/`background_spawn`/`read_global` 等方法全在这 75 行里。这是「crate 注册表」文件的一个反直觉之处：它不只是转发类型，还承载了上下文体系的最底层抽象。

[src/gpui.rs:L258-L292](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L258-L292) 紧接着定义 `VisualContext` trait（需要窗口存在的视觉上下文），[src/gpui.rs:L294-L311](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L294-L311) 定义 `EventEmitter` 与 `BorrowAppContext`，[src/gpui.rs:L341-L352](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L341-L352) 是 `GpuSpecs`（运行时查询 GPU 信息，如是否软件渲染 llvmpipe）。本讲只需知道它们的存在与位置，u2 单元再逐个精读。

**④ gpui_platform：100 行的门面**

[../gpui_platform/src/gpui_platform.rs:L1-L2](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_platform/src/gpui_platform.rs#L1-L2) 的模块注释直说了它的使命。核心函数有两个：

- [../gpui_platform/src/gpui_platform.rs:L13-L21](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_platform/src/gpui_platform.rs#L13-L21) `application()`：非 wasm 时就是 `gpui::Application::with_platform(current_platform(false))`——把「挑平台」这件事封装成一行。
- [../gpui_platform/src/gpui_platform.rs:L57-L81](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_platform/src/gpui_platform.rs#L57-L81) `current_platform(headless: bool)`：四段 `#[cfg]` 分别返回 `gpui_macos::MacPlatform`、`gpui_windows::WindowsPlatform`、`gpui_linux::current_platform(...)`、`gpui_web::WebPlatform`。这就是「gpui 核心定义 `Platform` trait、平台 crate 实现 trait、gpui_platform 负责挑选」的三层结构的最后一环。

顺带一提，这个 crate 家族在仓库里还有更多成员：`gpui_macros`（过程宏）、`gpui_macos`/`gpui_windows`/`gpui_linux`/`gpui_web`（平台实现）、`gpui_wgpu`（基于 wgpu 的渲染器，被 Linux/Windows/web 复用）、`gpui_apple`、`gpui_shared_string`、`gpui_util`、`gpui_tokio`（支撑设施）。本手册的主角是 `gpui` 核心，平台 crate 留到 u7-l1。

#### 4.3.4 代码实践

**实践目标**：练成「从注册表反查定义文件」的手艺，并亲手验证 gpui/gpui_platform 的分工。

**操作步骤**：

1. 打开 `src/gpui.rs`，对下面 5 个类型各做一次反查，把结果记成「类型 → pub use 行 → 最终定义文件」三列表：
   - `div`（提示：从 L104 的 `pub use elements::*;` 进入 `src/elements/mod.rs`）
   - `Entity`（提示：L95 的 `pub use app::*;`）
   - `App`（同上）
   - `Window`（提示：L166）
   - `actions!` 宏（提示：L10-11 的 `macro_use` 与 `src/action.rs`）
2. 用编辑器全局搜索或 ripgrep 验证你的答案，例如：

   ```sh
   rg -n "pub fn div" crates/gpui/src/elements/
   rg -n "pub struct Entity" crates/gpui/src/app/
   ```

3. 阅读 [../gpui_platform/src/gpui_platform.rs:L57-L81](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_platform/src/gpui_platform.rs#L57-L81)，确认在你的操作系统上 `current_platform` 会走到哪一个分支（Linux CI 上是 `gpui_linux::current_platform`）。

**需要观察的现象**：`pub use` 链有时一跳就到（`Window` → `window.rs`），有时要两跳（`div` → `elements/mod.rs` → `div.rs`）；`Entity` 定义在 `src/app/` 目录下的某个文件中，而不是单个 `entity.rs`。

**预期结果**：得到一张 5 行反查表。可对照两个已验证的锚点：`Entity` 定义在 [src/app/entity_map.rs:L414](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/entity_map.rs#L414)，`div` 函数定义在 [src/elements/div.rs:L1689](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/elements/div.rs#L1689)。其余三个（`App`、`Window`、`actions!` 宏，最后一个在 [src/action.rs:L24](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/action.rs#L24)）由你的 grep 完成反查。

**待本地验证**：步骤 2 的 grep 命令需要在 Zed 仓库根目录执行；如果你在 `crates/gpui` 目录内，请把路径改为 `src/elements/`、`src/app/`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `src/gpui.rs` 要用 `extern crate self as gpui;`（L7）？

<details>
<summary>参考答案</summary>

这样 crate 内部（包括各子模块）可以统一用 `gpui::Entity`、`gpui::div` 这样的绝对路径引用自己 crate 的项，和外部用户写出的代码完全一致。这避免了子模块里相对路径的混乱，也让宏生成的代码可以放心展开成 `gpui::xxx` 形式。
</details>

**练习 2**：`#[cfg(doc)] pub mod _ownership_and_data_flow;` 中的 `#[cfg(doc)]` 起什么作用？

<details>
<summary>参考答案</summary>

`doc` cfg 只在 rustdoc 生成文档时为真，正常编译（以及测试）时这个模块不存在。GPUI 团队把《Ownership and data flow》《Accessibility》两份指南写成 `.rs` 文件放进 `src/`，让它们既能在 docs.rs 上以「模块文档」的形式出现，又不参与实际编译、不影响构建产物。`_` 前缀也是同样的意图：排在文档列表最前面/表达「这不是代码模块」。
</details>

**练习 3**：如果你新写了一个平台（比如某个 hypothetical OS），按当前架构需要动哪些 crate？

<details>
<summary>参考答案</summary>

至少三处：① 新建 `gpui_<os>` crate，实现 `gpui` 中定义的 `Platform`/`PlatformWindow` 等平台 trait（参考 `gpui_linux` 的结构）；② 在 `gpui_platform` 里加对应的 target 依赖段与 `current_platform` 分支；③ 如需特殊 feature，在 `gpui_platform/Cargo.toml` 的 `[features]` 里加转发项。`gpui` 核心本身通常不用改——这正是「核心定义 trait、平台实现 trait、门面挑选实现」分层的好处（细节在 u7-l1 展开）。
</details>

## 5. 综合实践

**任务：制作你的《GPUI 学习手册首页笔记》**——一页纸串联本讲三个模块，作为后续所有讲义的参考资料。

具体要求：

1. **定位总结**（对应模块 4.1）：用一段话（3-5 句）概括 GPUI 是什么，必须涵盖「GPU 加速」「混合立即/保留模式」「三种编程层级」「为 Zed 而生但独立发布」四个要点，并注明出处行号（README L3-4、L74-84、Cargo.toml L6-L8）。
2. **三层 API 速查卡**（对应模块 4.1）：一张三行表格：层级 / 解决什么问题 / 关键 trait 或类型 / 典型使用场景。
3. **Linux feature 分析表**（对应模块 4.2）：把 4.2.2 的四行表格独立推导一遍——先只看 [Cargo.toml:L19-L40](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L19-L40) 和 `build.rs`，自己判断每个 feature 在 Linux 上是否有效、为什么，再对照讲义答案，最后附上 `cargo tree` 的观察结果。
4. **crate 家族图**（对应模块 4.3）：画一张以 `gpui` 为中心的依赖草图：`gpui_macros`、`gpui_platform` 及其按操作系统分派的 `gpui_macos`/`gpui_windows`/`gpui_linux`/`gpui_web`、被复用的 `gpui_wgpu`，标注每条边的含义（「提供过程宏」「按 cfg 挑选」「提供平台实现」）。

完成后，这份笔记应该能让一个没读过本讲的人用 5 分钟了解 GPUI 的全貌。

## 6. 本讲小结

- GPUI 是 Zed 编辑器团队开发的 GPU 加速、混合立即/保留模式 Rust UI 框架，`publish = true` 独立发布在 crates.io，版本 0.2.2、pre-1.0。
- 它提供三种「语域」：`Entity` 状态管理（GPUI 拥有一切实体）、`Render` 声明式视图（每帧由根视图构建元素树）、`Element` 命令式元素（完全掌控布局与绘制），三者在同一应用中按需混用。
- `gpui` 是跨平台核心（状态、元素、布局、样式、`Platform` trait 定义），`gpui_platform` 是按 `cfg(target_os)` 挑选平台实现的约 100 行门面，真正开窗口的是 `gpui_macos`/`gpui_windows`/`gpui_linux`/`gpui_web` 这些平台 crate。
- default features = `font-kit` + `wayland` + `x11` + `windows-manifest`，为的是三大桌面开箱即用；在 Linux 上只有 `wayland`/`x11` 有实际效果（作为 cfg 开关），`font-kit` 与 `windows-manifest` 分别是 macOS/Windows 专属。
- 核心依赖各有来头：`taffy`（布局引擎）、`accesskit`（无障碍）、`lyon`（路径细分）、`refineable`（样式叠加）、`slotmap`（实体存储）、`resvg`（SVG）、`ctor`+`inventory`（Action 自动注册）。
- `src/gpui.rs` 是 crate 的注册表：`mod` 登记全部模块、`pub use` 扁平化导出、README 被 `include_str!` 成 crate 文档，同时还定义了 `AppContext`/`VisualContext` 等 trait——掌握「从 `pub use` 反查定义文件」的路径是后续阅读源码的基本功。

## 7. 下一步学习建议

下一讲（u1-l2《运行第一个 GPUI 应用：hello_world 逐行解读》）将把本讲的三层 API 第一次落到可运行的代码上：运行 `cargo run -p gpui --example hello_world`，逐行拆解 `Application::run`、`App::open_window`、`cx.new` 与 `Render` 四个步骤。建议在进入下一讲前：

1. 通读 [examples/README.md](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/README.md)，它把 40 多个示例按「入门/布局样式/交互/图片动画/窗口行为」分好类，是后续整个手册的示例索引。
2. 浏览 `src/gpui.rs` 的模块登记区（L10-64），对照 4.3.2 的分组表混个脸熟，不必逐个深入。
3. 若你对「实体所有权」好奇，可以提前翻阅文档专用模块 `src/_ownership_and_data_flow.rs`（README L47 给出的官方链接的同名文件），它是 u2 单元《Entity 与所有权模型》的官方教材。
