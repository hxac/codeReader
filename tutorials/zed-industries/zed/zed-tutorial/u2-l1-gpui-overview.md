# GPUI 总览:Zed 的 UI 与状态框架

## 1. 本讲目标

本讲是第二单元「GPUI」的第一篇。学完后你应该能够:

1. 说出 gpui crate 内部包含哪几大类模块,以及每类模块的职责。
2. 区分 gpui(框架本体)、gpui_platform(平台分发)、gpui_macos / gpui_linux / gpui_windows / gpui_web(具体平台实现)、gpui_apple / gpui_wgpu(渲染后端)这几层 crate 的边界与依赖方向。
3. 理解 GPUI 的双重身份:它既是 GPU 加速的 UI 框架,又是自带执行器、状态容器、键位分发和测试基座的应用运行时。
4. 会运行 gpui 自带的示例程序,并具备阅读其模块清单回答架构问题的能力。

本讲只建立「地图」,不深入任何单个 API。Entity、Render、Action 等机制分别由本单元后续讲义展开。

## 2. 前置知识

- **立即模式与保留模式(hybrid immediate and retained mode)**:gpui 官方 README 对自己的定位是「混合立即与保留模式」。保留模式指框架替你保存一棵长期存活的对象树(如传统桌面控件);立即模式指每帧根据当前状态重新构建要画什么(如游戏引擎)。GPUI 的做法是:**状态长期保留在 Entity 里,而描述界面的元素树每帧重建**。本讲先记住这句话,细节留到 u2-l3。
- **GPU 渲染**:界面不是逐个控件让操作系统绘制,而是把文本、矩形、图片统一变成 GPU 图元(顶点、图集、着色器)一次画出来。这就是 Cargo.toml 里 `description = "Zed's GPU-accelerated UI framework"` 的含义。
- **trait object(`Rc<dyn Platform>`)**:Rust 中用 trait 加 `dyn` 表示「一组满足同一接口的类型」。GPUI 用 `Platform` trait 把 macOS / Linux / Windows / Web 的差异藏在这个接口后面。
- **条件编译(`#[cfg]`)与 Cargo feature**:承接 u1-l2 讲过的知识——同一份代码可以按编译目标或开关包含/排除某些模块,这是理解平台分层的钥匙。
- **依赖方向**:承接 u1-l3 的结论——Zed 工作区的 crate 依赖关系是一张 DAG。本讲会看到 gpui 家族内部同样严格遵守「平台 crate 依赖框架本体,框架本体不依赖任何平台 crate」的单向箭头。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/gpui/src/gpui.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/gpui.rs) | gpui 的库根:声明全部子模块并重导出公共 API,是「框架地图」的入口 |
| [crates/gpui/Cargo.toml](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/Cargo.toml) | 包元信息、feature 开关、各操作系统专属依赖、示例清单 |
| [crates/gpui/src/element.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/element.rs) | Element trait 与渲染管线的官方文档,讲清「元素树如何变成像素」 |
| [crates/gpui/src/app.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs) | `Application` 与 `App` 两个核心类型的定义,状态与能力的集大成者 |
| [crates/gpui/src/executor.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/executor.rs) | 前台/后台执行器,GPUI 并发模型的心脏 |
| [crates/gpui/src/platform.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/platform.rs) | `Platform` trait:操作系统差异的抽象接口 |
| [crates/gpui_platform/src/gpui_platform.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui_platform/src/gpui_platform.rs) | 便利层:提供 `application()` 与 `current_platform()`,免去使用者手写 `#[cfg]` |
| [crates/gpui_platform/Cargo.toml](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui_platform/Cargo.toml) | 展示「按编译目标选择平台 crate」的依赖写法 |
| [crates/zed/src/main.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs) | Zed 主程序接入 GPUI 的位置,承接 u1-l4 讲过的 `build_application` |
| [crates/gpui/examples/README.md](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/README.md) | 官方示例目录导读,是动手实践的教材 |

## 4. 核心概念与源码讲解

### 4.1 gpui crate 结构浏览:一个 crate 装下了六层能力

#### 4.1.1 概念说明

很多 UI 库只负责「画界面」,应用的其他部分(状态怎么存、任务怎么调度、按键怎么路由、测试怎么写)要你自己拼装 tokio + 十几个第三方库。GPUI 走的是另一条路:**把编辑器应用需要的整套基础设施都收进一个框架**。

因此读 gpui 的模块清单时,不要用「UI 库」的眼光看,而要用「应用运行时」的眼光看。它至少同时提供了:

1. **UI 构建层**:元素、样式、布局。
2. **状态管理层**:Entity 容器与上下文。
3. **并发运行时层**:前台/后台执行器与 Task。
4. **输入分发层**:Action、keymap、按键匹配。
5. **平台抽象层**:窗口、字体、文本排版。
6. **工程质量层**:测试上下文、性能剖析器、界面检查器。

这就是本讲学习目标里「双重身份」的含义——GPUI 既是 UI 框架,又是并发与状态运行时。后面几讲会逐层展开,本讲先把这张地图画准。

#### 4.1.2 核心流程

先用一句话概括 GPUI 应用的运行骨架(细节在 u2-l2、u2-l3 展开):

```text
main()
 └─ gpui_platform::application()      # 选定平台实现,构造 Application
     └─ app.run(|cx| { ... })          # 进入平台事件循环
         └─ cx.open_window(...)         # 开窗,注册根视图(Entity)
             └─ 每帧:Render::render() → 元素树 → taffy 布局 → paint → GPU 上屏
```

一帧的循环,官方在 element.rs 模块文档里说得很清楚:调用根视图的 `Render::render()` 递归构建元素树 → 交给 Taffy 做 web 风格布局 → 各元素按自己的 `Element::paint()` 实现 painted 上屏 → **下一帧开始前整棵元素树连同其回调被丢弃**,过程周而复始。状态不在元素树里,而在 Entity 里——这就是「状态保留、视图立即」的混合模式。

#### 4.1.3 源码精读

**(1) 库根:模块清单就是框架地图**

gpui 的库根文件第一行就把 README 嵌入为 crate 文档,并声明 `extern crate self as gpui`,让宏里能用 `gpui::` 路径引用自身:

[crates/gpui/src/gpui.rs:L1-L9](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/gpui.rs#L1-L9) —— 引入 README 作为文档、开启 `missing_docs` 警告、把自身注册为 `gpui` 供宏使用。

紧接着是一长串 `mod` 声明,这就是我们要的模块清单:

[crates/gpui/src/gpui.rs:L11-L64](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/gpui.rs#L11-L64) —— 声明全部子模块。注意 `test` 模块被 `#[cfg(any(test, feature = "test-support"))]` 门控,`profiler` 有专门文档注释,`keymap`、`platform`、`elements` 是目录模块。

我把这份清单按 4.1.1 的六层归类如下(只列代表性模块,完整清单请对照源码):

| 分层 | 模块 | 职责 |
| --- | --- | --- |
| UI 构建 | `element`、`elements`(div/text/img/svg/list…)、`style`、`styled`、`taffy`、`color`、`colors`、`scene`、`path_builder`、`spring` | 元素树、Tailwind 风格样式、flexbox 布局、绘制场景 |
| 状态管理 | `app`(含 `entity_map`、`context` 等)、`view`、`global`、`subscription`、`arena` | Entity 容器、上下文类型、全局状态、订阅 |
| 并发运行时 | `executor`、`platform_scheduler` | 前台/后台执行器、Task、定时器 |
| 输入分发 | `action`、`input`、`keymap`、`key_dispatch`、`gestures`、`interactive`、`tab_stop` | 动作定义、键位匹配、鼠标手势 |
| 平台抽象 | `platform`、`text_system`、`window`、`shared_uri` | Platform trait、字体与排版、窗口 |
| 工程质量 | `test`、`queue`、`inspector`、`profiler`、`debug_overlay`、`asset_cache`、`assets`、`svg_renderer`、`util` | 测试基座、剖析、检查器、资源缓存 |

其中约三分之二的模块与「画像素」没有直接关系——这就是「双重身份」最直观的证据。

清单之后是重导出区,把各模块的公共类型平铺到 crate 顶层,所以使用时写 `gpui::div()` 而不是 `gpui::elements::div::div()`:

[crates/gpui/src/gpui.rs:L93-L111](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/gpui.rs#L93-L111) —— `pub use` 把 `app::*`、`element::*`、`executor::*` 等全部摊平,并从 `gpui_macros` 引入 `Render`、`IntoElement`、`test`、`bench` 等派生宏与工具宏。

**(2) Cargo.toml:包定位与 feature 开关**

[crates/gpui/Cargo.toml:L1-L13](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/Cargo.toml#L1-L13) —— 包描述 `Zed's GPU-accelerated UI framework`、主页 `gpui.rs`、`publish = true`(GPUI 已发布到 crates.io,可作为独立库使用)、关键词 `desktop/gui/immediate`。

[crates/gpui/Cargo.toml:L19-L44](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/Cargo.toml#L19-L44) —— `[features]` 定义了 `test-support`、`wayland`、`x11`、`profiler` 等开关;`[lib] path = "src/gpui.rs"` 正是 u1-l3 提过的仓库命名规范(库根与 crate 同名而非 `lib.rs`)。

**(3) element.rs:渲染管线的官方自述**

[crates/gpui/src/element.rs:L1-L32](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/element.rs#L1-L32) —— 模块文档讲清三件事:元素树由 `Render::render()` 构建、按 taffy 的 web 布局标准排版、每帧结束即丢弃;大多数时候你不需要手写 Element,而是用 `RenderOnce` + `#[derive(IntoElement)]` 组合现成元素,只有需要自控布局/绘制(比如渲染代码编辑器)时才实现底层 Element。

[crates/gpui/src/element.rs:L51-L104](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/element.rs#L51-L104) —— `Element` trait 的核心:三个阶段方法 `request_layout`(向 Taffy 申请布局)→ `prepaint`(提交边界、生成命中盒)→ `paint`(绘制上屏),以及两个跨帧状态类型 `RequestLayoutState`/`PrepaintState`。u5-l4 讲编辑器渲染时会回到这里。

**(4) app.rs 与 executor.rs:UI 之外的能力证据**

[crates/gpui/src/app.rs:L143-L182](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L143-L182) —— `Application` 是 main 函数里最先拿到的类型,`with_platform(platform: Rc<dyn Platform>)` 说明应用由「平台实现」注入构造。

[crates/gpui/src/app.rs:L692-L719](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L692-L719) —— `App` 结构体的字段表是「应用运行时」的最佳证词:既有 `platform`、`text_system`(平台层),又有 `background_executor`、`foreground_executor`(并发层)、`entities: EntityMap`(状态容器)、`windows`、`keymap`、各类 `observers`(事件订阅)。一个结构体同时装着 UI、并发、状态、键位四类设施。

[crates/gpui/src/executor.rs:L11-L26](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/executor.rs#L11-L26) —— `BackgroundExecutor`(后台线程池执行)与 `ForegroundExecutor`(主线程执行,用 `PhantomData<Rc<()>>` 标记自身非 `Send`)两个执行器,内部复用外部 `scheduler` crate。GPUI 不依赖 tokio 就拥有完整的 async 执行能力,u2-l6 详述。

#### 4.1.4 代码实践

**实践目标**:亲手把 gpui.rs 的模块清单分类,验证本讲给出的六层表格,并获得「GPUI 提供了大量 UI 之外能力」的第一手证据。

**操作步骤**:

1. 打开 [crates/gpui/src/gpui.rs:L11-L64](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/gpui.rs#L11-L64),把 40 多个 `mod` 声明逐个抄到你的笔记里。
2. 对照 4.1.3 的分类表,给每个模块标注所属层级;遇到不确定的(例如 `bounds_tree`、`svg_renderer`),点进对应文件看开头的 `//!` 文档注释再判断。
3. 运行官方示例直观感受「一个 Application + 一个窗口」的形态:

   ```sh
   cargo run -p gpui --example hello_world
   ```

   该命令出自 [crates/gpui/examples/README.md:L1-L5](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/README.md#L1-L5)。

4. 对照 [crates/gpui/examples/hello_world.rs:L1-L20](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/hello_world.rs#L1-L20):注意它 `use gpui_platform::application`——示例程序同样通过平台分发层拿到 Application,这正是 4.2 要讲的内容。

**需要观察的现象**:

- 模块总数远超一个纯 UI 库的预期;你能找出至少 10 个与「画界面」无关的模块。
- hello_world 窗口中出现一个带边框、阴影、居中布局的彩色方块组合——这些都是 `div()` 方法链的产物。
- (运行结果待本地验证:示例需要在有图形环境的机器上运行,无显示器时可能报平台错误;Linux 无头环境下可观察到的现象是窗口创建失败而非编译失败。)

**预期结果**:你笔记里出现一张完整的「模块 → 层级」对照表,并能指出 `executor`、`subscription`、`keymap`、`test` 这类模块在 egui/iced 这类纯 UI 库中通常需要另外找库拼装。

#### 4.1.5 小练习与答案

**练习 1**:gpui.rs 里 `mod test` 前面的 `#[cfg(...)]` 写的是什么?为什么测试模块要被门控?

**参考答案**:`#[cfg(any(test, feature = "test-support"))]`(见 gpui.rs L59-L60)。测试基础设施(如 `TestAppContext`)只在两种情况下编译:crate 自身跑 `cargo test`,或下游开启 `test-support` feature(工作区里大量业务 crate 的测试都依赖它)。门控保证普通用户编译产物不包含测试代码。

**练习 2**:`App` 结构体里 `entities: EntityMap`、`keymap`、`background_executor` 三个字段分别对应 GPUI 的哪层能力?

**参考答案**:`entities` 是状态管理层(所有 Entity 的中央登记表,u2-l2 展开);`keymap` 是输入分发层(键位绑定表,u2-l5 展开);`background_executor` 是并发运行时层(后台任务执行器,u2-l6 展开)。三者共存在同一个结构体里,正说明 App 是「应用运行时」而非单纯的界面对象。

**练习 3**:为什么说 GPUI 是「混合立即与保留模式」?保留的是什么,立即的是什么?

**参考答案**:保留的是**状态**——Entity 长期存活在 `EntityMap` 中,跨帧存在;立即的是**视图描述**——元素树每帧由 `Render::render()` 重建、画完即丢弃。这兼顾了保留模式的可推理状态与立即模式的简洁渲染模型。

### 4.2 框架/平台分层理解:gpui 家族的洋葱结构

#### 4.2.1 概念说明

u1-l3 讲过 Zed 工作区里有 11 个 gpui 开头的 crate。本讲把它们的关系理清,画成一张洋葱图(由内向外):

```text
┌─ gpui ───────────────────────── 框架本体:类型、trait、执行器、元素、样式(平台无关)
│  ┌─ gpui_macros ─────────────── 过程宏:Render、IntoElement、test、bench(编译期支援)
│  ┌─ gpui_shared_string / gpui_util ── 字符串与工具函数(底层支援)
│  ┌─ 渲染后端 ────────────────── gpui_apple(Apple/Metal)、gpui_wgpu(跨平台 wgpu + cosmic-text)
│  ┌─ 平台实现 ────────────────── gpui_macos、gpui_linux、gpui_windows、gpui_web
│  ┌─ gpui_platform ───────────── 便利层:current_platform() 按 cfg 选出上面的实现
└─ 应用(zed、gpui examples、你的程序)── 调用 gpui_platform::application()
```

几个 crate 的自我说明(来自各自库根的文档注释或模块结构):

- **gpui_apple**:「Shared Apple platform support for GPUI. This crate contains the Metal renderer and GPU resource management shared by GPUI's Apple platform backends.」——Apple 系共享的 Metal 渲染器与 GPU 资源管理。
- **gpui_wgpu**:内含 `wgpu_renderer`、`wgpu_atlas`、`cosmic_text_system`——基于 wgpu 的跨平台渲染与 cosmic-text 文本系统,是 Linux 后端的渲染/排版引擎。
- **gpui_web**:浏览器平台,单 canvas 单顶层窗口,默认 WebGPU、自动回退 WebGL2。
- **gpui_windows**:Win32 窗口 + DirectWrite 文本 + DirectX 渲染(从其模块名 `direct_write`、`directx_atlas` 可见)。

**关键架构约束:依赖箭头单向向内**。gpui 本体不依赖任何平台 crate;gpui_macos 依赖 gpui + gpui_apple,gpui_linux 依赖 gpui + (可选)gpui_wgpu。框架只定义 `Platform` trait,实现由外层提供——这是典型的依赖倒置。

#### 4.2.2 核心流程

应用程序拿到平台实现的路径:

```text
应用调用 gpui_platform::application()
  └─ gpui::Application::with_platform(current_platform(false))
       └─ current_platform(headless) 按 #[cfg(target_os = ...)] 编译期选择:
            macOS   → Rc<gpui_macos::MacPlatform>
            Windows → Rc<gpui_windows::WindowsPlatform>
            Linux/BSD → gpui_linux::current_platform(wayland 或 x11)
            wasm    → Rc<gpui_web::WebPlatform>
  └─ 得到持有 Rc<dyn Platform> 的 Application,进入 run()
```

注意这是**编译期**分发:`#[cfg]` 让每个平台只编译自己的分支,`current_platform` 里那些互斥的块在别的平台上根本不存在。

#### 4.2.3 源码精读

**(1) Platform trait:差异的收敛点**

[crates/gpui/src/platform.rs:L124-L130](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/platform.rs#L124-L130) —— `pub trait Platform: 'static` 的前几个方法:提供 `background_executor`/`foreground_executor`(执行器竟由平台实现——因为事件循环归平台管)、`text_system`(字体排版)、`run`(启动平台事件循环)。操作系统能干什么,这个 trait 就抽象什么。

顺带一提,同文件 L95-L122 的 `guess_compositor()` 展示了 Linux 上如何探测 Wayland/X11/Headless,是平台探测逻辑的一个直观小样本。

**(2) gpui_platform:免写 cfg 的便利层**

[crates/gpui_platform/src/gpui_platform.rs:L1-L21](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui_platform/src/gpui_platform.rs#L1-L21) —— crate 文档直言其定位:「re-exports GPUI's platform traits and the `current_platform` constructor so consumers don't need `#[cfg]` gating」;`application()` 在非 wasm 下就是 `Application::with_platform(current_platform(false))`。

[crates/gpui_platform/src/gpui_platform.rs:L57-L81](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui_platform/src/gpui_platform.rs#L57-L81) —— `current_platform()` 的完整分发逻辑:四个 `#[cfg]` 块分别构造 MacPlatform、WindowsPlatform、`gpui_linux::current_platform(headless)`、WebPlatform。这段代码就是 4.2.2 流程图的原文。

**(3) 依赖方向:Cargo.toml 的证据**

[crates/gpui_platform/Cargo.toml:L20-L41](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui_platform/Cargo.toml#L20-L41) —— gpui_platform 的依赖表:无条件依赖 `gpui`,再按 `[target.'cfg(target_os = ...)']` 分别引入 gpui_macos / gpui_windows / gpui_linux / gpui_web。feature 也顺着这条链转发(如 `wayland = ["gpui_linux/wayland"]`)。

反过来,gpui 本体的 `[dependencies]` 里找不到任何 gpui_* 平台 crate;平台 crate 只出现在它的 dev-dependencies 与 examples 中([crates/gpui/Cargo.toml:L147-L165](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/Cargo.toml#L147-L165) —— `gpui_platform` 是 gpui 的 **dev-dependency**,供自身测试与示例使用)。

再看一个平台 crate 的依赖实例:[crates/gpui_linux/Cargo.toml#L61-L63](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui_linux/Cargo.toml#L61-L63) —— gpui_linux 依赖 `gpui`、`gpui_util`、可选的 `gpui_wgpu`,箭头全部指向内层,印证「洋葱单向依赖」。

**(4) Zed 主程序的接入点**

[crates/zed/src/main.rs:L86-L93](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/zed/src/main.rs#L86-L93) —— `build_application()`:一行 `gpui_platform::current_platform(false)` 拿到平台实现,再按环境变量决定是否启用无障碍模式构造 `Application`。这正是 u1-l4 里「current_platform 按编译目标选定 Platform 实现并构建应用壳」那句话的原文出处,现在你应该能完整读懂它了。

#### 4.2.4 代码实践

**实践目标**:确认在你自己的机器上,`current_platform()` 会选中哪个平台 crate,并验证依赖方向。

**操作步骤**:

1. 确认操作系统类型(Linux 上再执行 `echo $XDG_SESSION_TYPE` 或观察 `$WAYLAND_DISPLAY`/`$DISPLAY` 哪个非空,对照 platform.rs L95-L122 的 `guess_compositor` 逻辑)。
2. 阅读分发代码 [crates/gpui_platform/src/gpui_platform.rs:L57-L81](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui_platform/src/gpui_platform.rs#L57-L81),写下你的机器会命中哪个 `#[cfg]` 分支、返回哪个结构体。
3. 用依赖树验证(在仓库根目录执行):

   ```sh
   cargo tree -p zed -i gpui_linux    # Linux 机器上:谁依赖 gpui_linux
   cargo tree -p gpui -i              # 反向:谁依赖 gpui 本体
   ```

4. 运行 `cargo run -p gpui --example hello_world` 成功后,再打开 [crates/gpui/examples/hello_world.rs:L1-L9](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/hello_world.rs#L1-L9),确认示例也是经 `gpui_platform::application` 启动——与应用程序同一入口。

**需要观察的现象**:

- `cargo tree -p zed -i gpui_linux` 输出中出现 gpui_platform → zed 的链路,而不会出现 gpui → gpui_linux(依赖不反向)。
- `cargo tree -p gpui -i` 的反依赖列表非常长(gpui 是工作区最底层 crate 之一,承接 u1-l3 的「扇入大」结论)。
- (cargo tree 输出待本地验证:依赖树因 feature 组合而异,以实际输出为准。)

**预期结果**:你能用一句话说清「zed → gpui_platform → gpui_linux/gpui_macos/gpui_windows → gpui + 渲染后端」这条依赖链,并理解框架本体对平台一无所知。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `current_platform()` 的平台选择靠 `#[cfg]` 而不是运行时判断操作系统?

**参考答案**:各平台 crate 链接的 C 库(Metal、Cocoa、DirectX、wayland-client 等)只能在对应目标上编译链接。`#[cfg]` 让编译产物只包含当前平台的分支,既避免链接错误也减小体积。而 Linux 上 Wayland/X11 的选择部分推迟到运行时(见 `guess_compositor` 读环境变量),因为两者在同一个 gpui_linux crate 里都可用。

**练习 2**:gpui_web 的存在说明 GPUI 的抽象做到了什么程度?它的文档提到什么限制?

**参考答案**:说明 Platform 抽象覆盖到了浏览器(wasm):用 canvas 当窗口、WebGPU/WebGL2 当渲染后端。文档明确的限制是:整个文档共享一个 canvas、只支持一个顶层窗口,重复开窗或关后再开会返回错误。

**练习 3**:如果让你给一个新操作系统(假设叫 ZedOS)适配 GPUI,需要动哪几个 crate?

**参考答案**:新建一个 `gpui_zedos` crate 实现 `Platform` trait(窗口、事件循环、执行器调度、文本系统、渲染),在 `gpui_platform` 的 `current_platform()` 里加一个 `#[cfg(target_os = "zedos")]` 分支并登记其 target 依赖。**不需要改 gpui 本体**——这正是依赖倒置分层带来的可扩展性。(渲染后端可以视硬件能力复用 gpui_wgpu。)

## 5. 综合实践

**任务**:写一段约 200 字的笔记,回答两个问题——「为什么 Zed 不直接用 egui 或 iced?」「GPUI 在 UI 之外还提供了哪些能力?」

**要求与线索**:

1. 论据必须来自你亲手读过的源码,而不是泛泛而谈。可直接引用的证据包括:
   - 模块清单([crates/gpui/src/gpui.rs:L11-L64](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/gpui.rs#L11-L64))中的非 UI 模块;
   - `App` 结构体字段([crates/gpui/src/app.rs:L692-L719](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L692-L719));
   - 自带执行器([crates/gpui/src/executor.rs:L11-L26](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/executor.rs#L11-L26));
   - 测试基座(`test` 模块与 `test-support` feature)。
2. 说明:仓库源码中没有一处官方声明逐条对比 egui/iced,这属于**基于源码证据的分析练习**——你的结论应当是「GPUI 的模块清单显示它同时是 X/Y/Z,而这些能力在纯 UI 库中需要自行拼装」,而不是替官方代言。
3. 写完后用 4.1.4 的分类表自查:笔记里提到的每个能力都能指认出对应模块吗?

**参考思路(供对照,请用自己的话写)**:GPUI 把编辑器所需的应用级设施(状态容器 EntityMap、前后台执行器、keymap/action 键位分发、TestAppContext 测试基座、多平台抽象)与 GPU 渲染 UI 收进同一框架,并为代码编辑器这类高性能场景保留了手写底层 Element 的口子(element.rs 文档明言「渲染代码编辑器」是实现自定义 Element 的典型理由);通用 UI 库通常只覆盖其中的绘制层。

## 6. 本讲小结

- gpui crate 由约 40 个模块组成,可归为六层:UI 构建、状态管理、并发运行时、输入分发、平台抽象、工程质量;其中大半与「画界面」无直接关系。
- GPUI 是「混合立即与保留模式」:状态长期保存在 Entity 中,元素树每帧由 `Render::render()` 重建、布局(taffy)、绘制、然后丢弃。
- gpui 家族是洋葱式单向依赖:gpui 本体不依赖任何平台 crate;gpui_macos/gpui_linux/gpui_windows/gpui_web 实现 `Platform` trait,gpui_apple/gpui_wgpu 提供渲染后端,gpui_platform 用 `#[cfg]` 编译期分发 `current_platform()`。
- Zed 主程序在 `build_application()` 中一行调用 `gpui_platform::current_platform(false)` 完成平台接入,应用与具体操作系统解耦。
- `App` 结构体的字段(executor、entities、keymap、windows……)是「GPUI 既是 UI 框架又是应用运行时」的最直接证据。

## 7. 下一步学习建议

本讲建立了地图,下一讲 u2-l2《Entity 模型与 App 上下文》将深入洋葱最内层的第一站:读 [crates/gpui/src/app/entity_map.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/entity_map.rs) 与 [crates/gpui/src/app/context.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/context.rs),弄清 `Entity<T>` 的 read/update 生命周期与 `cx.notify()` 的传播链路。在进入下一讲前,建议先完成本讲 4.1.4 的示例运行(hello_world),它会让 u2-l3 讲 Render 时的每一行代码都有画面感;有兴趣的读者也可以提前浏览 [crates/gpui/README.md](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/README.md) 中关于 gpui_platform 各平台 feature 的说明,那是 4.2 分层知识的官方版注释。
