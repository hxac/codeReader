# Platform trait 全景导览：从执行器到凭据的八大方法分组

## 1. 本讲目标

学完本讲，你应该能够：

1. 不看源码，按八大功能组复述 `Platform` trait 的方法清单（执行器、生命周期、窗口与显示器、外观、系统集成、菜单与通知、键盘、杂项）。
2. 准确说出哪些方法带默认实现、哪些必须实现，并解释「默认实现」对平台实现者和对上层调用者分别意味着什么。
3. 熟练使用 rust-analyzer 的 Go to Implementations，以及 `grep`，在 gpui_macos、gpui_windows、gpui_linux、gpui_web 四个平台 crate 中定位任意一个 trait 方法的真实实现位置。

本讲是整个第二单元的地基：后面三讲（生命周期、显示器、系统集成）都是在本讲建立的地图上，分别 zoom into 某一个分组。

## 2. 前置知识

本讲默认你已读完 u1 全部四讲，知道：

- **门面 crate**：`gpui_platform` 自身没有功能代码，它再导出 gpui 的平台 trait，并用条件编译在 `current_platform(headless)` 中按编译目标挑选平台实现。
- **`#[cfg]` 条件编译**：同一段源码在不同编译目标上可以「长出」或「删掉」不同的方法，这一点在 u1-l4 已经用 `current_platform` 的四段互斥分支验证过。

在此之外，本讲还需要几项 Rust 基础，先用通俗语言过一遍：

- **trait 作为契约（contract）**：trait 只声明「你必须会做什么」，不规定「你怎么做」。`Platform` 就是 gpui 与操作系统之间的一份契约：gpui 说「我要能开窗口、读剪贴板、跑事件循环」，至于窗口是 NSWindow、Win32 窗口还是 Wayland surface，契约不管。
- **trait 对象 `Rc<dyn Platform>`**：一个「只知道契约、不知道具体类型」的指针。`current_platform` 返回它，gpui 内部用它调用平台能力，运行时动态分发到当前平台的实现。用 `Rc` 而不是 `Arc`，是因为 GPUI 的所有平台交互都发生在单一前台线程上（详见 u4）。
- **trait 方法的默认实现**：Rust 允许 trait 方法带方法体。实现这个 trait 的类型若不覆盖该方法，就自动继承这份默认行为。这是本讲的核心研究对象之一。
- **两种异步返回形态**：契约里大量方法返回 `oneshot::Receiver<Result<T>>`（一次性通道的接收端，结果稍后从别的线程送来）或 `Task<T>`（GPUI 自己包装的 future）。现在只需认识它们是「异步结果」的两种外壳，细节在 u4 展开。
- **`Box<dyn FnMut(...)>` 回调**：上层把一个闭包「注册」进平台，平台在相应事件发生时（比如用户点菜单、系统要退出了）回头调用它。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [gpui/src/platform.rs:L125-L341](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L125-L341) | `Platform` trait 的定义，本讲主战场（约 220 行，纯契约，几乎没有逻辑） |
| [gpui_platform/src/gpui_platform.rs:L1-L4](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/src/gpui_platform.rs#L1-L4) | 门面 crate 对 trait 的再导出：`pub use gpui::Platform;` |
| [gpui_platform/src/gpui_platform.rs:L57-L81](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/src/gpui_platform.rs#L57-L81) | `current_platform`：构造 `Rc<dyn Platform>` 的地方 |
| [gpui/src/app.rs:L779-L803](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L779-L803) | `App::new_app`：契约的「消费者」，启动时从平台对象抽取执行器、文本系统、键盘 |
| [gpui_macos/src/platform.rs:L478](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L478) | `impl Platform for MacPlatform` 起点 |
| [gpui_windows/src/platform.rs:L408](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/platform.rs#L408) | `impl Platform for WindowsPlatform` 起点 |
| [gpui_linux/src/linux/platform.rs:L233](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L233) | `impl<P: LinuxClient> Platform for LinuxPlatform<P>` 起点（注意它是泛型的） |
| [gpui_web/src/platform.rs:L267](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_web/src/platform.rs#L267) | `impl Platform for WebPlatform` 起点 |
| [gpui/src/platform/test/platform.rs:L309](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform/test/platform.rs#L309) | `impl Platform for TestPlatform`：gpui 自带的测试替身（u8-l4 的主角） |

> 提示：本讲涉及大量兄弟 crate 的源码，正文里所有链接都指向仓库内真实文件；在自己机器上阅读时，从 `crates/gpui_platform` 出发，用 `../gpui/src/platform.rs` 这样的相对路径即可找到它们。

## 4. 核心概念与源码讲解

### 4.1 门面与契约的连接点：`pub use gpui::Platform`

#### 4.1.1 概念说明

u1-l1 说过 `gpui_platform` 是门面 crate，但当时没有回答一个问题：**下游代码写的 `gpui_platform::Platform` 到底是什么？**

答案藏在门面 crate 的第一行代码里：`pub use gpui::Platform;`。这行再导出没有复制任何代码，只是给 `gpui` crate 里定义的那个 trait 起了一个「免 cfg」的别名。于是：

- 定义处：gpui 主 crate 的 [gpui/src/platform.rs:L126](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L126)，`pub trait Platform: 'static`。
- 使用处：任何依赖 `gpui_platform` 的 crate 直接写 `use gpui_platform::Platform;`，不需要知道背后是哪个平台 crate。
- 实现处：四个平台 crate 各自 `impl Platform for XxxPlatform`，再加上 gpui 内的测试替身。

一条契约、六个实现者（4 个真实平台 + 2 个测试替身），这就是本讲要环视的全景。

另一个必须建立的认知是**谁在消费这份契约**。平台对象 `Rc<dyn Platform>` 诞生于 `current_platform`，随后被塞进 `Application::with_platform`，最终由 `App` 结构体长期持有。也就是说：上游（你的应用代码）几乎从不直接摸这个 trait 对象，而是调用 `App` 上的转发方法，`App` 再去调平台。理解这条链路，你才知道 grep 一个方法名时为什么会命中好几处。

#### 4.1.2 核心流程

从程序启动到一次平台调用，契约对象的旅程是：

```text
gpui_platform::application()
  └─ current_platform(false)                    # 按 cfg 挑实现，返回 Rc<dyn Platform>
       └─ gpui::Application::with_platform(...)
            └─ App::new_app(platform, ...)      # app.rs:779，消费契约的起点
                 ├─ platform.background_executor()   # 立刻抽取
                 ├─ platform.foreground_executor()   # 立刻抽取
                 ├─ assert!(background_executor.is_main_thread())
                 ├─ platform.text_system()           # 立刻抽取
                 ├─ platform.keyboard_layout()       # 立刻抽取
                 ├─ platform.keyboard_mapper()       # 立刻抽取
                 └─ App { platform, ... }            # 存进字段，之后按需转发
应用运行期
  └─ cx.write_to_clipboard(item) 之类的 App 方法
       └─ self.platform.write_to_clipboard(item)    # 转发到具体平台实现
```

注意抽取顺序透露的信息：**trait 开头三个方法（执行器、文本系统）之所以排在最前面，是因为 `new_app` 在构造 `App` 时就要用它们**；而 `assert!(... is_main_thread())` 则是 GPUI「单前台线程」模型的第一个显式检查点，u4 会展开。

#### 4.1.3 源码精读

先看门面的再导出（[gpui_platform/src/gpui_platform.rs:L1-L4](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/src/gpui_platform.rs#L1-L4)）：

```rust
//! Convenience crate that re-exports GPUI's platform traits and the
//! `current_platform` constructor so consumers don't need `#[cfg]` gating.

pub use gpui::Platform;
```

这段代码说明：trait 的「权威定义」永远在 gpui 主 crate，门面只负责传播名字。

再看构造侧（[gpui_platform/src/gpui_platform.rs:L57-L60](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/src/gpui_platform.rs#L57-L60)）：

```rust
/// Returns the default [`Platform`] for the current OS.
pub fn current_platform(headless: bool) -> Rc<dyn Platform> {
    #[cfg(target_os = "macos")]
    {
        Rc::new(gpui_macos::MacPlatform::new(headless))
    }
```

返回类型 `Rc<dyn Platform>` 就是「契约指针」：编译期不锁定具体类型，运行期动态分发。u1-l4 已逐分支分析过这四段 cfg，这里不再重复。

然后是消费侧（[gpui/src/app.rs:L779-L803](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L779-L803)）：

```rust
pub(crate) fn new_app(
    platform: Rc<dyn Platform>,
    ...
) -> Rc<AppCell> {
    let background_executor = platform.background_executor();
    let foreground_executor = platform.foreground_executor();
    assert!(
        background_executor.is_main_thread(),
        "must construct App on main thread"
    );
    ...
    let text_system = Arc::new(TextSystem::new(platform.text_system()));
    ...
    let keyboard_layout = platform.keyboard_layout();
    let keyboard_mapper = platform.keyboard_mapper();
```

这段代码验证了流程图里的抽取顺序：执行器 → 主线程断言 → 文本系统 → 键盘。trait 定义里没有「文档」告诉你哪些方法在启动期就会被调用，源码的消费侧才是真相。

> **版本演进提示**：从提交 `6e0a0835` 起，`new_app` 在主线程断言之后多了一段编译期门控的插桩——[gpui/src/app.rs:L790-L791](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L790-L791) 的 `#[cfg(feature = "profiler")] let foreground_journal = crate::profiler::journal::install_foreground_journal();`（对应 `App` 结构体里 L816-L817 的同门控字段）。它不影响上面讨论的抽取顺序，只在启用 gpui 的 `profiler` feature 时为前台工作日志安装采集器，完整机制在 u4-l6 展开——你在阅读 `app.rs` 时遇到的所有 `cfg(feature = "profiler")` 代码块都源于这个 feature。本讲义锚定的正是这个 HEAD，因此本段的行号说明与链接均已对齐。

最后看一次典型的运行期转发。`App` 上的公开方法（[gpui/src/app.rs:L1014](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1014) 的 `quit`、[L1304](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1304) 的 `displays`）内部都只有一行，形如：

```rust
self.platform.quit();        // app.rs:1015，位于 pub fn quit(&self) 内
self.platform.displays()     // app.rs:1305，位于 pub fn displays(&self) 内
```

所以当你想找「剪贴板在 Linux 上到底怎么实现」时，正确的检索路径是：先在 `gpui/src/app.rs` 找到转发方法确认 trait 方法名，再沿 `impl Platform for` 找平台实现，而不是一头扎进平台 crate 猜函数名。

#### 4.1.4 代码实践：跟踪一条转发链（源码阅读型）

1. **实践目标**：亲手确认「应用层方法 → App 转发 → trait 契约 → 平台实现」四层结构，记住每层的落点。
2. **操作步骤**：
   1. 打开 [gpui/src/app.rs:L1423](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1423)，阅读 `pub fn write_to_clipboard(&self, item: ClipboardItem)` 的函数体。
   2. 记下它调用的 trait 方法及所在文件行号（对照 [gpui/src/platform.rs:L311](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L311) 的契约声明）。
   3. 再打开你当前操作系统对应的 `impl Platform for ...`（见第 3 节地图），找到同名方法，记下文件与行号。如果你在 Linux 上，可以从 [gpui_linux/src/linux/platform.rs:L747](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L747) 的 `read_from_clipboard` 附近找起，同一片区域通常还有 `write_to_clipboard`。
3. **需要观察的现象**：`App` 的转发方法体极短（一两行）；trait 声明无方法体（必需方法）或只有几行默认体；平台实现里出现的都是各操作系统的原生 API（NSPasteboard、IDataObject、wl_data_offer 之类）。
4. **预期结果**：得到一条形如「app.rs:L1423 → platform.rs 契约 → 平台 crate 实现行号」的三段式记录。转发语句的具体行号待本地确认（不同平台实现的行号不同），但三层结构必然存在。
5. 如果无法运行或打开项目，本实践纯靠 GitHub 链接即可完成，无需「待本地验证」的运行步骤。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `current_platform` 返回 `Rc<dyn Platform>` 而不是具体的 `MacPlatform`/`WindowsPlatform`？

答案：因为返回具体类型的话，函数签名会随编译目标变化，下游每个使用处都得写 `#[cfg]`——这正是门面 crate 要消灭的样板。`Rc<dyn Platform>` 把「哪个实现」推迟到运行时分发，把「选哪个实现」收敛到唯一一处条件编译，让下游一份代码通吃四个平台。

**练习 2**：`App::new_app` 里为什么敢在构造期就 `assert!(background_executor.is_main_thread())`？

答案：GPUI 的设计约定是所有实体更新与 UI 操作都发生在单一前台线程（u4 主题）。平台实现返回的 `ForegroundExecutor`/`BackgroundExecutor` 都绑定主循环；断言把「必须在主线程构造 App」变成启动期的 fail-fast，而不是等到运行期出现难以排查的数据竞争。gpui_platform 的测试模块里那三个 macOS 测试之所以 `#[ignore]`，也是同一根源（u8-l4 详述）。

**练习 3**：如果给 `Platform` 增加一个新方法 `fn cloud_sync_status(&self) -> Option<CloudStatus>` 并提供默认实现 `None`，六个实现者需要改代码吗？

答案：不需要。带默认实现的 trait 方法对实现者是可选覆盖；`MacPlatform`、`WindowsPlatform`、`LinuxPlatform`、`WebPlatform`、`TestPlatform`、`VisualTestPlatform` 都能原样通过编译。只有当某个平台真的能提供云同步状态时才去覆盖它。这正是 4.3 要系统分析的「默认实现减负」机制。

### 4.2 `Platform` trait 完整签名：69 个方法、八大分组与 cfg 门控

#### 4.2.1 概念说明

[gpui/src/platform.rs:L125-L341](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L125-L341) 的 `Platform` trait 在当前 HEAD 下共有 **69 个方法**，其中 **18 个带默认实现**，另有 **4 个方法按平台 cfg 门控**（只在特定编译目标上存在）。直接从头读到尾容易迷路，本讲把它们组织成八个功能组：

1. **执行器与文本系统**（3 个）——GPUI 的运行基础。
2. **生命周期**（12 个）——启动、退出、重启、各种系统事件回调。
3. **窗口与显示器**（7 个）——枚举屏幕、开窗口、查激活窗口。
4. **外观**（3 个）——亮暗主题与窗口按钮布局。
5. **系统集成**（18 个）——URL、文件对话框、剪贴板、凭据，是最大的一组。
6. **菜单与通知**（15 个）——应用菜单、Dock 菜单、跳转列表、系统通知、热状态。
7. **键盘**（3 个）——键盘布局与按键映射。
8. **杂项**（8 个）——合成器名、可执行文件路径、光标样式、滚动手势服务等。

有个结构性的细节值得先说：trait 声明处标着 `#[expect(missing_docs)]`（[gpui/src/platform.rs:L125](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L125)），意味着大部分方法**没有**文档注释，语义要靠实现与调用处反推；只有少数方法（如 `set_window_appearance`、通知三件套）带 doc。读这份契约时，带 doc 的方法往往是行为最微妙的地方。

#### 4.2.2 核心流程

下面这张总表就是本讲的核心产出物。列含义：

- **行号**：契约在 `gpui/src/platform.rs` 中的声明位置。
- **必需**：无默认实现，每个（可见该方法的）平台必须实现；**默认**：带默认体，可覆盖可不覆盖。
- **cfg**：标注该方法只在部分编译目标上存在。

**第 1 组 · 执行器与文本系统**（[L127-L129](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L127-L129)）

| 方法 | 行号 | 必需/默认 | 说明 |
| --- | --- | --- | --- |
| `background_executor` | L127 | 必需 | 后台线程池执行器，`new_app` 启动期抽取 |
| `foreground_executor` | L128 | 必需 | 前台（主线程）执行器 |
| `text_system` | L129 | 必需 | 平台文本系统（u8-l1 主题） |

**第 2 组 · 生命周期**（[L131-L137](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L131-L137)、[L203-L205](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L203-L205)、[L211-L222](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L211-L222)）

| 方法 | 行号 | 必需/默认 | 说明 |
| --- | --- | --- | --- |
| `run` | L131 | 必需 | 进入平台事件循环，`on_finish_launching` 回调在循环启动前执行一次 |
| `quit` | L132 | 必需 | 请求退出 |
| `restart` | L133 | 必需 | 重启进程（可指定二进制路径与参数） |
| `activate` | L134 | 必需 | 把应用带到前台（macOS 语义最完整） |
| `hide` / `hide_other_apps` / `unhide_other_apps` | L135-L137 | 必需 | 隐藏应用窗口（Dock 场景） |
| `on_quit` / `on_reopen` / `on_system_wake` | L203-L205 | 必需 | 注册退出前/重新打开/系统唤醒回调 |
| `on_app_lifecycle` | L216 | 默认（空） | 移动端生命周期阶段回调；doc 明言「Desktop platforms never invoke this」 |
| `on_memory_warning` | L222 | 默认（空） | 移动端内存压力回调；桌面平台同样永不调用 |

**第 3 组 · 窗口与显示器**（[L139-L166](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L139-L166)）

| 方法 | 行号 | 必需/默认 | 说明 |
| --- | --- | --- | --- |
| `displays` | L139 | 必需 | 枚举全部显示器 |
| `primary_display` | L140 | 必需 | 主显示器 |
| `active_window` | L141 | 必需 | 当前聚焦窗口的句柄 |
| `window_stack` | L142-L144 | 默认 `None` | 窗口叠放顺序；探测型默认 |
| `is_screen_capture_supported` | L146-L148 | 默认 `false` | 屏幕捕获能力探测 |
| `screen_capture_sources` | L150-L160 | 默认（返回带错误的通道） | 未启用 screen-capture feature 时的兜底 |
| `open_window` | L162-L166 | 必需 | 创建平台窗口，返回 `Box<dyn PlatformWindow>`（u3 主题） |

**第 4 组 · 外观**（[L168-L184](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L168-L184)）

| 方法 | 行号 | 必需/默认 | 说明 |
| --- | --- | --- | --- |
| `window_appearance` | L168-L169 | 必需 | 查询当前亮暗外观 |
| `set_window_appearance` | L171-L179 | 默认（no-op） | 覆盖系统级外观；doc 明言目前仅 macOS 实现了真实行为 |
| `button_layout` | L182-L184 | 默认 `None` | 窗口关闭/最小化/最大化按钮的布局 |

**第 5 组 · 系统集成：URL / 文件 / 剪贴板 / 凭据**（[L186-L201](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L186-L201)、[L310-L336](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L310-L336)）

| 方法 | 行号 | 必需/默认 | 说明 |
| --- | --- | --- | --- |
| `open_url` / `on_open_urls` / `register_url_scheme` | L186-L188 | 必需 | 打开 URL、注册「别的应用用 URL 唤起我」的回调、注册自定义 URL scheme |
| `prompt_for_paths` / `prompt_for_new_path` | L190-L198 | 必需 | 系统文件打开/保存对话框，返回 `oneshot::Receiver` |
| `can_select_mixed_files_and_dirs` | L199 | 必需 | 对话框能否同时选文件和目录 |
| `reveal_path` / `open_with_system` | L200-L201 | 必需 | 在文件管理器中显示 / 用系统默认程序打开 |
| `read_from_clipboard` / `write_to_clipboard` | L310-L311 | 必需 | 同步剪贴板读写 |
| `read_from_clipboard_async` | L313-L322 | 默认（包装同步版） | 异步读剪贴板；浏览器等权限门控平台覆盖它 |
| `read_from_primary` / `write_to_primary` | L324-L327 | 必需，**cfg linux/freebsd** | X11 主选区（鼠标中键粘贴的那套剪贴板） |
| `read_from_find_pasteboard` / `write_to_find_pasteboard` | L329-L332 | 必需，**cfg macos** | macOS 查找粘贴板（⌘F/E 共享的那块） |
| `write_credentials` / `read_credentials` / `delete_credentials` | L334-L336 | 必需 | 凭据三件套，落到各平台钥匙串/凭据管理器 |

**第 6 组 · 菜单与通知**（[L231-L291](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L231-L291)）

| 方法 | 行号 | 必需/默认 | 说明 |
| --- | --- | --- | --- |
| `set_menus` | L231 | 必需 | 设置应用菜单栏 |
| `get_menus` | L232-L234 | 默认 `None` | 读回当前菜单（用于校验/测试） |
| `set_dock_menu` | L236 | 必需 | 设置 Dock 菜单 |
| `perform_dock_menu_action` | L237 | 默认（no-op） | 触发 Dock 菜单项 |
| `add_recent_document` | L238 | 默认（no-op） | 登记「最近打开」 |
| `update_jump_list` | L239-L245 | 默认（返回空 `Task`） | Windows 跳转列表；默认即「无事发生」 |
| `on_app_menu_action` / `on_will_open_app_menu` / `on_validate_app_menu_command` | L246-L248 | 必需 | 菜单交互回调三件套 |
| `thermal_state` / `on_thermal_state_change` | L250-L251 | 必需 | 热状态查询与变化回调 |
| `set_app_identity` | L253-L261 | 默认（no-op） | 设置进程身份（Windows AppUserModelID）与用户可见名 |
| `show_system_notification` | L263-L271 | 默认（no-op） | 发系统通知，同 tag 替换旧通知 |
| `dismiss_system_notification` | L273-L279 | 默认（no-op） | 撤回通知，best-effort |
| `on_system_notification_response` | L281-L291 | 默认（no-op） | 用户点通知/通知按钮时的回调 |

**第 7 组 · 键盘**（[L338-L340](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L338-L340)）

| 方法 | 行号 | 必需/默认 | 说明 |
| --- | --- | --- | --- |
| `keyboard_layout` | L338 | 必需 | 当前键盘布局（u3-l3 主题） |
| `keyboard_mapper` | L339 | 必需 | 原始按键 → `Keystroke` 的映射器 |
| `on_keyboard_layout_change` | L340 | 必需 | 布局变化回调 |

**第 8 组 · 杂项**（[L224-L229](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L224-L229)、[L293-L308](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L293-L308)）

| 方法 | 行号 | 必需/默认 | 说明 |
| --- | --- | --- | --- |
| `gestures` | L224-L229 | 默认 `None` | 平台手势识别服务（超出 GPUI 内置识别器的部分） |
| `compositor_name` | L293-L295 | 默认 `""` | 合成器名；u1-l4 用它观测 Linux 后端选择 |
| `app_path` / `path_for_auxiliary_executable` | L296-L297 | 必需 | 当前应用路径 / 辅助可执行文件路径 |
| `set_cursor_style` | L299 | 必需 | 设置鼠标光标样式 |
| `hide_cursor_until_mouse_moves` | L303 | 必需 | 打字时隐藏光标 |
| `is_cursor_visible` | L306 | 必需 | 查询光标可见性 |
| `should_auto_hide_scrollbars` | L308 | 必需 | 是否应自动隐藏滚动条 |

**cfg 门控的算术**：4 个条件方法使不同目标「看到」的方法总数不同——

\[ \text{可见方法数} = 69 - 2_{\text{对方平台独占}} \]

- Linux/FreeBSD 与 macOS 目标：各看不到对方的 2 个方法，可见 67 个，其中必需 \(67 - 18 = 49\) 个。
- Windows 与 wasm 目标：4 个条件方法全部不可见，可见 65 个，其中必需 \(65 - 18 = 47\) 个。

这也意味着「最小 Platform 实现」的工作量随平台浮动，u8-l5 的毕业实践会直接利用这一点。

#### 4.2.3 源码精读

把八个分组放回源码里，能看到作者其实就是按这些主题分段书写的。以第 5 组剪贴板段落为例（[gpui/src/platform.rs:L310-L336](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L310-L336)）：

```rust
fn read_from_clipboard(&self) -> Option<ClipboardItem>;
fn write_to_clipboard(&self, item: ClipboardItem);

/// Reads the clipboard, resolving once its contents are available.
///
/// Most platforms read synchronously and return a ready task. Platforms
/// whose clipboard access is inherently asynchronous and permission-gated
/// (e.g. the browser's async clipboard API) override this method; ...
fn read_from_clipboard_async(&self) -> Task<Result<Option<ClipboardItem>, ClipboardReadError>> {
    Task::ready(Ok(self.read_from_clipboard()))
}

#[cfg(any(target_os = "linux", target_os = "freebsd"))]
fn read_from_primary(&self) -> Option<ClipboardItem>;
#[cfg(any(target_os = "linux", target_os = "freebsd"))]
fn write_to_primary(&self, item: ClipboardItem);

#[cfg(target_os = "macos")]
fn read_from_find_pasteboard(&self) -> Option<ClipboardItem>;
```

四点值得咀嚼：

1. 同步版 `read_from_clipboard` 无默认体（必需）；异步版默认体就是「把同步结果装进现成的 `Task`」——默认实现复用另一个必需方法，这是很值得学习的 trait 设计手法。
2. doc 注释直接写明了覆盖者是谁（「e.g. the browser's async clipboard API」），契约作者在替实现者做决策。
3. `#[cfg]` 紧贴在方法声明上，与 u1-l4 见过的「函数体内 cfg 块」是两种不同粒度的条件编译。
4. 主选区与查找粘贴板是**必需**而非默认——在这两个平台上，实现者无处可躲，必须给出真实行为。

再看菜单组中默认设计最讲究的 `update_jump_list`（[gpui/src/platform.rs:L239-L245](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L239-L245)）：

```rust
fn update_jump_list(
    &self,
    _menus: Vec<MenuItem>,
    _entries: Vec<SmallVec<[PathBuf; 2]>>,
) -> Task<Vec<SmallVec<[PathBuf; 2]>>> {
    Task::ready(Vec::new())
}
```

默认返回「立即就绪的空列表」而不是 `None`：调用方拿到的类型永远是合法结果，不需要分情况处理「平台不支持」。类型签名（返回列表而非 Option）本身就承载了降级策略。

#### 4.2.4 代码实践：亲手数一遍方法（校验型）

1. **实践目标**：用命令行验证本节的 69/18/4 三个数字，建立「表格可信」的手感。
2. **操作步骤**：
   1. 在仓库根目录执行：

      ```bash
      sed -n '125,341p' crates/gpui/src/platform.rs | grep -c 'fn '
      ```

      统计 trait 体内的方法声明总数（trait 体 L125-L341 内没有嵌套函数，`fn ` 只命中方法名）。
   2. 再统计默认实现的个数：

      ```bash
      sed -n '125,341p' crates/gpui/src/platform.rs | grep -c 'fn .*{$\|fn .*) -> .* {$'
      ```

      （默认方法必然带 `{` 开方法体；结果允许 ±1~2 的口径误差，以人工核对为准。）
   3. 统计 cfg 门控的方法：

      ```bash
      sed -n '125,341p' crates/gpui/src/platform.rs | grep -B0 '#\[cfg' | wc -l
      ```

      注意 L324/L326/L329/L331 四处才是方法级 cfg，文件头部还有模块级 cfg，别混入。
3. **需要观察的现象**：第一步应得到 69 附近的结果；若偏差，重点检查是否漏数了 `gestures`（L227）和四个 cfg 方法。
4. **预期结果**：与 4.2.2 的表格互相印证：69 个方法、18 个默认、4 个 cfg 门控。若你的数字与表格不一致，以实际源码为准并更新自己的表格——这正是「维护学习笔记」的意义。
5. 本实践的精确输出依赖 grep 口径，标注**待本地验证**；结论以人工逐行核对为最终标准。

#### 4.2.5 小练习与答案

**练习 1**：`read_from_primary` 为什么设计成「仅 Linux 必需」而不是「全平台默认空」？

答案：主选区（primary selection）是 X11/Unix 桌面独有的概念——鼠标选中即复制、中键粘贴，与「复制粘贴」剪贴板是两条独立通道。Windows/macOS/浏览器根本没有对应物，暴露默认空实现只会诱导上层在这些平台上写出「看起来能用」的死代码。cfg 掉之后，非 Linux 平台连调用它的代码都编译不过，错误在编译期就被拦截。

**练习 2**：`on_app_lifecycle` 和 `on_memory_warning` 属于哪一组？为什么它们有默认实现而 `on_quit` 没有？

答案：属于生命周期组。`on_quit` 是所有桌面平台真实发生的事件（用户关窗、Cmd+Q），每个平台都必须给出注册入口，所以是必需；而这两个方法是为移动端（iOS/Android）准备的，doc 明言桌面平台永不调用，给默认空实现可以让四个桌面/Web 实现者完全无视它们，同时为未来的移动后端预留契约。

**练习 3**：不看表格，说出 `open_window`、`set_menus`、`write_credentials` 分别属于哪个分组、是否必需。

答案：`open_window` 属于窗口与显示器组，必需（L162）；`set_menus` 属于菜单与通知组，必需（L231），但其配对的 `get_menus` 是默认 `None`；`write_credentials` 属于系统集成组，必需（L334），与 `read_credentials`、`delete_credentials` 构成三件套。

### 4.3 默认实现的语义光谱：18 个默认方法的三种姿态

#### 4.3.1 概念说明

18 个默认方法并不是同一种东西。按默认体「说了什么谎」，可以分成三种姿态：

1. **能力探测型**——默认返回 `None` / `false` / `""` / 空集合，语义是「本平台没有这个能力，请自行降级」。代表：`window_stack`、`button_layout`、`get_menus`、`is_screen_capture_supported`、`compositor_name`、`gestures`。调用方必须检查返回值。
2. **优雅降级 no-op 型**——默认体为空或 `let _ = ...`，语义是「这个动作在本平台无意义，忽略即可」。代表：`set_window_appearance`、`set_app_identity`、`show_system_notification` 三件套、`perform_dock_menu_action`、`add_recent_document`、`on_app_lifecycle`、`on_memory_warning`。调用方无需感知。
3. **通用回退型**——默认体提供一份「能跑但降级」的真实结果，语义是「大多数平台用这份，特殊的自己覆盖」。代表：`read_from_clipboard_async`（包同步版）、`screen_capture_sources`（返回错误通道）、`update_jump_list`（返回空列表）。

对**实现者**，这张光谱就是减负清单：47~49 个必需方法之外，18 个默认方法按「我的平台有没有这个能力」挑选覆盖。对**调用者**，光谱决定了防御式代码的写法：探测型必须处理 `None`，no-op 型可以放心直调，回退型拿到的永远是合法值。

#### 4.3.2 核心流程

判断一个默认方法「被谁覆盖」的标准检索流程：

```text
1. 在契约里确认方法名与默认体姿态        （gpui/src/platform.rs）
2. 在四个平台 crate 里并行 grep 方法名    （fn <method_name>）
3. 命中处向上找所属 impl 块               （impl Platform for XxxPlatform）
4. 记录：平台 → 文件:行号 → 覆盖语义
5. 未命中 = 该平台使用默认实现（继承契约里的默认体）
```

下面是一份已验证的「覆盖矩阵」样例（默认方法 × 平台，✓ = 覆盖，空 = 用默认）：

| 默认方法 | macOS | Windows | Linux | Web |
| --- | --- | --- | --- | --- |
| `set_window_appearance` | ✓ L688 | — | — | — |
| `set_app_identity` | — | ✓ L693 | ✓ L569 | — |
| `show_system_notification` | ✓ L1004 | ✓ L711 | ✓ L574 | — |
| `get_menus` | ✓ L1053 | ✓ L742 | ✓ L627 | — |
| `update_jump_list` | — | ✓ L947 | — | — |
| `read_from_clipboard_async` | — | — | — | ✓ L559 |
| `window_stack` | ✓ L645 | — | ✓ L380 | — |
| `compositor_name` | — | — | ✓ L284 | ✓ L495 |
| `is_screen_capture_supported` | ✓ L627 | ✓ L553 | ✓ L365 | — |

（行号均在各平台 crate 的 `src/platform.rs` 内，Linux 的特例见 4.3.3。）

这张表直观展示了两件事：浏览器几乎对所有系统能力说「不」（默认即态度）；而 `update_jump_list`、`set_window_appearance` 这类「单平台专属」能力的默认 no-op，让其他三个平台的实现文件里干脆**不存在**这个方法名——grep 不到也是一种信息。

#### 4.3.3 源码精读

**案例一：`set_window_appearance` —— 只有 macOS 说真话。** 契约（[gpui/src/platform.rs:L171-L179](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L171-L179)）的 doc 写得很直白：覆盖外观是为了让原生窗口边框跟随应用主题，且「A no-op on other platforms」。唯一的实现者在 [gpui_macos/src/platform.rs:L688-L703](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L688-L703)：

```rust
fn set_window_appearance(&self, appearance: Option<WindowAppearance>) {
    unsafe {
        let app: id = msg_send![APP_CLASS, sharedApplication];
        // `None` clears the override by setting a nil appearance, ...
        let ns_appearance: id = match appearance {
            None => nil,
            Some(appearance) => { ... NSAppearanceNameAqua / NSAppearanceNameDarkAqua ... }
```

它直接设置 `NSApplication` 的 appearance，把 Light/Dark/Vibrant 四种枚举映射到 NSAppearance 常量。其他三个平台的实现文件里搜不到这个方法——继承 no-op。

**案例二：`set_app_identity` —— Windows 与 Linux 各取所需。** Windows 侧（[gpui_windows/src/platform.rs:L693-L709](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/platform.rs#L693-L709)）调用 Win32 的 `SetCurrentProcessExplicitAppUserModelID`：

```rust
fn set_app_identity(&self, identifier: &str, name: &str) {
    // If the process has package identity, it's automatally granted an AUMID by the system.
    if self.has_package_identity {
        return;
    }
    ...
    SetCurrentProcessExplicitAppUserModelID(...)
```

注意它还处理了「有包身份则系统自动分配 AUMID」的分支——覆盖不是照抄契约，而是带平台知识的实现。Linux 侧（[gpui_linux/src/linux/platform.rs:L569](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L569)）则只是把应用名存进 `LinuxCommon`，供发系统通知时使用；macOS 与 Web 用默认 no-op。

**案例三：`compositor_name` —— 探测型默认与两层分发。** 契约默认返回空串（[gpui/src/platform.rs:L293-L295](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L293-L295)）。Linux 的覆盖（[gpui_linux/src/linux/platform.rs:L284](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L284)）并不是终点，它转发给内部后端：这是 u1-l4 讲过的两层分发——`LinuxPlatform` 外壳实现了 `Platform`，而 `compositor_name` 的真实答案（"Wayland"/"X11"/"headless"）来自实现 `LinuxClient` 契约（[gpui_linux/src/linux/platform.rs:L51-L52](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L51-L52)）的三个客户端，如 Wayland 侧（[gpui_linux/src/linux/wayland/client.rs:L1223](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/wayland/client.rs#L1223)）。所以 grep 一个方法名时，Linux 上常会命中三层：外壳转发、`LinuxClient` 声明、具体后端实现。

**案例四：`read_from_clipboard_async` —— 回退型默认的唯一覆盖者。** 默认体把同步版包成 `Task::ready`（[gpui/src/platform.rs:L320-L322](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L320-L322)）。浏览器因为剪贴板读取受权限与异步 API 限制，是唯一覆盖者（[gpui_web/src/platform.rs:L559](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_web/src/platform.rs#L559)）。这解释了契约 doc 里那句「callers that can await should prefer this method」：能等待的调用方永远走异步版，平台自己决定要不要快路径。

#### 4.3.4 代码实践：验证「单平台覆盖」现象（grep 型）

1. **实践目标**：用 grep 复现 4.3.2 覆盖矩阵中的三行结论，体会「搜不到也是答案」。
2. **操作步骤**：在仓库根目录执行：

   ```bash
   # 1. set_window_appearance 应只在 gpui_macos 命中 impl 块内的覆盖
   grep -rn "fn set_window_appearance" crates/gpui_macos/src crates/gpui_windows/src \
        crates/gpui_linux/src crates/gpui_web/src

   # 2. update_jump_list 应只在 gpui_windows 命中（外加 destination_list.rs 的辅助函数）
   grep -rn "fn update_jump_list" crates/gpui_macos/src crates/gpui_windows/src \
        crates/gpui_linux/src crates/gpui_web/src

   # 3. read_from_clipboard_async 应只在 gpui_web 命中
   grep -rn "fn read_from_clipboard_async" crates/gpui_macos/src crates/gpui_windows/src \
        crates/gpui_linux/src crates/gpui_web/src
   ```

3. **需要观察的现象**：第一条只命中 `gpui_macos/src/platform.rs:688`；第二条在 Windows 命中**三处**——`gpui_windows/src/platform.rs:947`（trait 覆盖）、`gpui_windows/src/platform.rs:273`（固有 `impl WindowsPlatform` 块里的私有辅助方法，与 trait 方法恰好同名）、`gpui_windows/src/destination_list.rs:64`（封装 Win32 ICustomDestinationList 的 helper，u6-l3 会讲到）；第三条只命中 `gpui_web/src/platform.rs:559`。
4. **预期结果**：与上述行号一致（本讲写作时已在当前 HEAD 验证过这些命中；若你 checkout 了新提交，行号可能漂移，命中文件不应变）。其余平台对这三个方法零命中——它们继承契约默认体。`update_jump_list` 在 Windows 上构成一条三层链：trait 覆盖（[L947-L953](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/platform.rs#L947-L953)）只有一行，转发给固有辅助 `self.update_jump_list(menus, entries)`（[L273](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/platform.rs#L273)），后者再异步调用 destination_list 的 Win32 封装——这是「grep 命中 ≠ trait 实现」的最佳教材：判断命中属于谁，永远要看它落在哪个 `impl` 块里。
5. 若你在自己的 fork 里改过这些文件，以你的仓库为准，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：把 `window_stack`（默认 `None`）和 `update_jump_list`（默认空 `Task`）归入三种姿态中的哪一种？为什么返回类型不同？

答案：`window_stack` 是能力探测型——默认 `None` 明确告诉调用方「此平台不提供窗口叠放信息」，调用方要处理 None 分支；`update_jump_list` 是通用回退型——默认返回立即就绪的空列表，类型上不允许「没有答案」，调用方无须分支。前者把降级判断交给调用方，后者把降级结果替调用方做好。

**练习 2**：为什么 `show_system_notification` 在 macOS/Windows/Linux 都被覆盖，唯独 Web 没有任何实现？

答案：三个桌面平台都有各自的系统通知中心（通知中心 / Action Center / DBus 或 portal），而浏览器页面没有「绕过标签页直接向操作系统发通知」的通用能力（Web Notifications 涉及权限与服务工作者，且不属于「系统通知中心」语义）。因此 WebPlatform 继承默认 no-op——发通知静默失败，符合「优雅降级」的契约注释（No-op on platforms without notification support）。

**练习 3**：如果 Linux 想让 `window_stack` 返回真实数据，它实际改的是哪个文件？

答案：不是直接改 `impl Platform for LinuxPlatform`（外壳的转发逻辑已有），而是改实现 `LinuxClient` 的某个后端客户端。外壳在 [gpui_linux/src/linux/platform.rs:L380](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L380) 覆盖 `Platform::window_stack` 后转发给 `self.inner`（LinuxClient），目前 Wayland（client.rs:L1219）、X11（client.rs:L1817）、headless（client.rs:L96）三个后端各自给出实现。这是 u5-l1 的伏笔。

### 4.4 主实践：方法分组表 + 四平台实现定位

#### 4.4.1 概念说明

本讲规格里指定的实践任务是把 4.2 的分组表升级成**带实现位置的三维表格**：行是方法，列除了「分组 / 必需还是默认」，还要有四个平台 crate 的实现落点。做完后你将拥有一份可以长期维护的索引——以后读任何平台代码，先查表定位，再精读。

同时掌握两种互补的定位工具：rust-analyzer 适合「交互式、语义准确」的浏览，grep 适合「批量、可脚本化」的清单生产。两者结论应互相印证。

#### 4.4.2 核心流程

**rust-analyzer 路线**（以 VS Code + rust-analyzer 为例，其他编辑器命令名相同）：

```text
1. 打开 crates/gpui/src/platform.rs，光标置于 L126 的 trait 名 Platform 上
2. 「Go to Definition」      → 确认定义处
3. 「Go to Implementations」 → 列出当前编译目标下所有实现者
   预期（Linux 目标）: LinuxPlatform<P>、TestPlatform、VisualTestPlatform
   预期（macOS 目标）: MacPlatform、TestPlatform、VisualTestPlatform ...
   注意: cfg 门控同样作用于 rust-analyzer，非本目标的平台不会出现
4. 把光标移到某个方法名（如 window_appearance）上再做
   「Go to Implementations」→ 直达该方法的所有覆盖，未覆盖者不出现
5. 「Peek Implementations」可以在不跳走的情况下内联预览
```

**grep 路线**：

```bash
# 六个实现块的锚点
grep -rn "impl Platform for\|impl.*Platform for" \
     crates/gpui_macos/src crates/gpui_windows/src crates/gpui_linux/src crates/gpui_web/src \
     crates/gpui/src/platform

# 某方法在四平台的落点（以 window_appearance 为例）
grep -rn "fn window_appearance" crates/gpui_{macos,windows,linux,web}/src
```

两条路线的共同陷阱：Linux 命中三层（外壳 / LinuxClient / 后端），要沿 `impl` 块归属判断命中属于谁。

#### 4.4.3 源码精读

六个实现块的锚点位置（当前 HEAD 已验证）：

| 实现者 | 位置 | 备注 |
| --- | --- | --- |
| `MacPlatform` | [gpui_macos/src/platform.rs:L478](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L478) | AppKit 实现，约 660 行 |
| `WindowsPlatform` | [gpui_windows/src/platform.rs:L408](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/platform.rs#L408) | Win32 实现 |
| `LinuxPlatform<P>` | [gpui_linux/src/linux/platform.rs:L233](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L233) | 泛型外壳，`P: LinuxClient` 是 Wayland/X11/headless 之一 |
| `WebPlatform` | [gpui_web/src/platform.rs:L267](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_web/src/platform.rs#L267) | 浏览器实现 |
| `TestPlatform` | [gpui/src/platform/test/platform.rs:L309](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform/test/platform.rs#L309) | 测试替身（u8-l4） |
| `VisualTestPlatform` | [gpui/src/platform/visual_test.rs:L67](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform/visual_test.rs#L67) | 可视化测试替身，仅 macOS cfg |

注意 grep 模式里 `impl.*Platform for` 是为 `impl<P: LinuxClient + 'static> Platform for LinuxPlatform<P>` 这种带泛型参数的写法准备的——精确的 `impl Platform for` 匹配不到它，这也是很多人第一次 grep 时「漏掉 Linux」的原因。

作为表格的「参考答案」样例，下面给出已验证的 12 个填格（每平台 3 个，正对应实践任务要求）：

| 方法（分组） | macOS | Windows | Linux | Web |
| --- | --- | --- | --- | --- |
| `run`（生命周期） | [platform.rs:L491](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L491) | 在 `impl` 块内，待定位 | [platform.rs:L267](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L267) | 在 `impl` 块内，待定位 |
| `text_system`（执行器） | [platform.rs:L487](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L487) | 在 `impl` 块内，待定位 | [platform.rs:L244](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L244) | 在 `impl` 块内，待定位 |
| `read_from_clipboard`（系统集成） | 在 `pasteboard.rs` 附近，待定位 | 在 `clipboard.rs`，待定位 | [platform.rs:L747](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L747) | 在 `impl` 块内，待定位 |
| `open_window`（窗口） | 在 `impl` 块内，待定位 | 在 `impl` 块内，待定位 | [platform.rs:L384](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L384) | 在 `impl` 块内，待定位 |
| `set_app_identity`（菜单与通知） | 用默认 | [platform.rs:L693](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/platform.rs#L693) | [platform.rs:L569](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L569) | 用默认 |
| `keyboard_layout`（键盘） | [platform.rs:L1025](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L1025) | 在 `impl` 块内，待定位 | [platform.rs:L248](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L248) | 在 `impl` 块内，待定位 |
| `update_jump_list`（菜单与通知） | 用默认 | [platform.rs:L947](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/platform.rs#L947) | 用默认 | 用默认 |
| `set_window_appearance`（外观） | [platform.rs:L688](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L688) | 用默认 | 用默认 | 用默认 |
| `read_from_clipboard_async`（系统集成） | 用默认 | 用默认 | 用默认 | [platform.rs:L559](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_web/src/platform.rs#L559) |
| `window_stack`（窗口） | [platform.rs:L645](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L645) | 用默认 | [platform.rs:L380](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L380) | 用默认 |
| `compositor_name`（杂项） | 用默认 | 用默认 | [platform.rs:L284](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L284) | [platform.rs:L495](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_web/src/platform.rs#L495) |
| `is_screen_capture_supported`（窗口） | [platform.rs:L627](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L627) | [platform.rs:L553](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/platform.rs#L553) | [platform.rs:L365](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L365) | 用默认 |

「待定位」「用默认」也是合法填格：前者留给读者完成（这正是实践任务），后者是 4.3 讲过的「搜不到即默认」。

#### 4.4.4 代码实践：制作你自己的分组表（本讲主实践）

1. **实践目标**：产出一份 `platform-trait-map.md` 个人笔记：八大分组 ×（必需/默认）× 四平台实现位置。
2. **操作步骤**：
   1. 以 4.2.2 的八张分组表为骨架建一个新表，加四列：macOS / Windows / Linux / Web。
   2. 用 4.4.2 的 grep 命令批量抓取每个方法在四个平台 crate 的 `fn <方法名>` 命中行；命中为空填「用默认」，Linux 的命中要判断属于外壳还是后端。
   3. 用 rust-analyzer 的 Go to Implementations 抽查 5 个方法，校正 grep 结论（特别是带泛型的 `impl` 块归属）。
   4. 把 4.4.3 表格中的「待定位」格子全部填满，并与你自己的结果合并。
3. **需要观察的现象**：
   - 大多数必需方法在四个平台各有恰好一处命中；
   - Linux 上部分方法会额外命中 `LinuxClient` 声明与后端实现（三层结构）；
   - 默认方法在很多平台 crate 中零命中；
   - cfg 门控的 4 个方法只在对应平台 crate 中存在。
4. **预期结果**：一张 69 行的完整表格。它将成为你阅读 u2 其余三讲、u5/u6/u7 平台实现讲时的随身地图。
5. 表格中未验证的格子以「待本地验证」标注，不要凭印象填写——包括本讲给出的样例行，也建议你重跑一遍 grep 复核（HEAD 漂移会导致行号变化）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 rust-analyzer 的 Go to Implementations 在 Linux 开发机上通常看不到 `MacPlatform`？

答案：rust-analyzer 按当前编译目标解析 cfg。`gpui_macos` 只在 macOS 目标被 Cargo 解析为依赖（u1-l3 讲过按 target 分段的依赖声明），Linux 目标下 `MacPlatform` 类型根本不可见，自然不会出现在实现列表。想看它要么换 macOS 机器，要么直接读源码/用 GitHub 链接。

**练习 2**：grep `fn run` 会在四个平台 crate 命中很多无关方法（各窗口、客户端也有 `run`）。如何写出更精确的模式？

答案：先缩小文件范围到 `src/platform.rs`（或已知实现文件），再匹配带签名特征的完整形态，例如 `grep -rn "fn run(&self, on_finish_launching" crates/gpui_*/src`——用契约里的参数名 `on_finish_launching` 当指纹，几乎不可能误命中。这也是读契约的一个额外收益：签名就是天然的 grep 指纹。

**练习 3**：`TestPlatform` 和 `VisualTestPlatform` 也实现了 `Platform`，它们覆盖默认方法的策略和真实平台有何不同？

答案：真实平台只覆盖「本平台有能力」的方法，其余继承默认；测试替身的策略是**把不确定性与异步都替换成确定性行为**——例如用 TestDispatcher 代替真实事件循环（u8-l4），让 `run_until_parked`、`advance_clock` 可以驱动时间。它们大量覆盖方法的目的不是提供能力，而是提供可预测性。这也是 gpui_platform.rs 测试模块里三个 macOS 测试存在的意义（[gpui_platform/src/gpui_platform.rs:L113-L206](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/src/gpui_platform.rs#L113-L206)）。

## 5. 综合实践

**任务：产出一份「Platform 能力矩阵」并写一段架构评论。**

1. 从 18 个默认方法中任选 10 个作为行，四个平台作为列，用 4.4 的方法填满每一格（覆盖者给文件:行号，未覆盖者标「默认」）。
2. 对矩阵做两个统计：
   - 每个平台覆盖了多少个默认方法（覆盖数 = 该平台「额外能力」的粗略度量）；
   - 每个默认方法被几个平台覆盖（覆盖数 = 0 的方法是「事实上没人需要」，覆盖数 = 3~4 的方法值得怀疑「为什么不让它变成必需」）。
3. 写 200 字左右的评论，回答：如果让你为一个新的操作系统（假设是某自研 RTOS）实现 `Platform`，你打算覆盖哪几个默认方法？哪些必需方法你会觉得最难实现（提示：`text_system` 与 `keyboard_mapper` 是公认硬骨头，u8-l1/u3-l3 会解释原因）？
4. 把矩阵存进你的学习笔记仓库，并在 HEAD 变化时用 `git log -p -- crates/gpui/src/platform.rs` 检查契约是否新增/删除了方法，更新矩阵——这份讲义的所有行号都锚定在 `6e0a083575`，随仓库演进它们会漂移，而你的矩阵应该活着。

## 6. 本讲小结

- `Platform` 是 gpui 与操作系统之间的契约，定义于 [gpui/src/platform.rs:L125-L341](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L125-L341)，当前共 69 个方法，可归为八大功能组：执行器与文本系统、生命周期、窗口与显示器、外观、系统集成、菜单与通知、键盘、杂项。
- 其中 18 个方法带默认实现、4 个方法按平台 cfg 门控；因此「最小实现」在 Linux/macOS 上是 49 个方法，在 Windows/Web 上是 47 个。
- 默认实现分三种姿态：能力探测型（返回 `None`/`false`/`""`，调用方降级）、优雅降级 no-op 型（平台不支持即忽略）、通用回退型（默认体给出能用的结果）。覆盖情况高度不对称：`set_window_appearance` 只有 macOS 实现，`update_jump_list` 只有 Windows 实现，`read_from_clipboard_async` 只有 Web 覆盖。
- 契约的消费链路是：`current_platform` 构造 `Rc<dyn Platform>` → `Application::with_platform` → `App::new_app` 启动期抽取执行器/文本系统/键盘并断言主线程 → 运行期 `App` 逐方法转发。找实现时沿这条链路走，不要在平台 crate 里盲猜函数名。
- Linux 是特例：`impl<P: LinuxClient> Platform for LinuxPlatform<P>` 是外壳，同一方法可能再转发给 Wayland/X11/headless 后端，grep 会命中多层。
- rust-analyzer 的 Go to Implementations 与 `grep -rn "fn <方法名>"` 互补：前者语义准确但受编译目标限制，后者批量但需人工判断 `impl` 块归属。

## 7. 下一步学习建议

本讲建立了全景地图，接下来三讲逐组下钻，建议按序：

1. **u2-l2（生命周期与运行循环）**：精读 `run`/`quit`/`restart` 与 `on_quit`/`on_reopen`/`on_system_wake` 的桌面实现，理解各平台事件循环入口的对应关系（NSApplication run vs calloop vs Win32 消息循环）。
2. **u2-l3（显示器管理）**：下钻 `displays`/`primary_display` 与 `PlatformDisplay` 契约（[gpui/src/platform.rs:L344-L372](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L344-L372)，紧接在 `Platform` 之后），看多屏几何如何上报。
3. **u2-l4（系统集成服务）**：下钻本讲第 5 组，覆盖剪贴板、URL、文件对话框与凭据的完整异步链路。

延伸阅读（本讲已埋的伏笔）：

- [gpui/src/platform/test/platform.rs:L309](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform/test/platform.rs#L309) 的 `TestPlatform`：一份「如何用最少的代码满足 47+ 个必需方法」的活样本，是 u8-l5 毕业实践的最佳起点。
- [gpui_linux/src/linux/platform.rs:L51-L104](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L51-L104) 的 `LinuxClient` trait：观察一个真实平台内部如何再做一层「迷你契约」拆分后端，u5-l1 的主角。
