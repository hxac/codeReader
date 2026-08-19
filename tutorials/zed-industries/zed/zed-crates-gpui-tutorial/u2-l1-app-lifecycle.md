# u2-l1 App 与 Application：应用生命周期

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 `Application`、`AppCell`、`App` 三者的关系图，说清「谁拥有谁、谁借用谁」。
2. 解释为什么 `App` 被装在 `RefCell` 里、为什么 GPUI 的所有前台代码都跑在一个线程上，以及违反这条规则时会发生什么（panic）。
3. 会用 `with_assets`、`with_http_client`、`with_quit_mode`、`on_app_quit`、`on_window_closed` 这些生命周期钩子。
4. 说清 `cx.quit()` 之后到进程真正退出之间发生了什么（含 200ms 的关闭超时）。
5. 理解 `run` 与 `run_embedded` 的区别，以及 wasm 场景下「事件循环不属于 GPUI」意味着什么。

本讲承接 u1-l2 的「启动四步曲」（`application().run` → `cx.open_window` → `cx.new` → `impl Render`），把第一步拆开看个究竟。

## 2. 前置知识

本讲需要一点 Rust 标准库的基础概念，用通俗语言先过一遍：

- **`Rc<T>`（引用计数指针）**：多个所有者共享同一份数据的智能指针，最后一个所有者离开时数据被释放。它只能用在单线程里。
- **`Weak<T>`（弱引用）**：指向 `Rc` 内部数据但不增加引用计数的句柄，用 `upgrade()` 尝试换回强引用。GPUI 大量用它避免循环引用（比如实体对 App 的反指）。
- **`RefCell<T>`（内部可变性）**：Rust 的借用检查通常在编译期完成，而 `RefCell` 把检查推迟到运行时：
  - `borrow()` 拿共享借用（可多个并存）；
  - `borrow_mut()` 拿可变借用（独占）；
  - 如果在已经 `borrow_mut` 的情况下再次 `borrow_mut`，程序会在**运行时 panic**，报错形如 `already borrowed: BorrowMutError`。这不是 bug，而是 `RefCell` 在替你把关「可变访问必须独占」这条铁律。
- **trait 对象 `Rc<dyn Platform>`**：一个「实现了 Platform 接口的某个平台实现」的句柄。GPUI 核心不知道自己跑在 Windows、Linux 还是浏览器里，它只调用 `Platform` trait 的方法，具体行为由平台实现决定。
- **事件循环（event loop）**：GUI 程序的标准形态——程序启动后进入一个「等待事件 → 处理事件 → 回去等待」的循环，直到退出。处理事件的权力在平台手里，你的代码只是被平台「回调」。

另外回顾 u1-l2 的结论：`application()` 由门面 crate `gpui_platform` 按操作系统选择平台实现；`run` 的回调只在应用完全启动后执行一次。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/app.rs` | 本讲主战场：`Application`、`AppCell`、`App` 的定义，启动与退出流程都在这里 |
| `src/app/async_context.rs` | `AsyncApp`——跨 `await` 点持有的异步上下文，内部持有 `Weak<AppCell>` |
| `src/platform.rs` | `Platform` trait：`run`/`quit`/`on_quit` 等平台事件循环接口 |
| `src/subscription.rs` | `Subscription`：`on_app_quit` 等钩子返回的「注销凭证」 |
| `docs/contexts.md` | 官方文档，一句话定义了 `App` 作为根上下文的地位 |
| `examples/hello_world.rs` | u1-l2 逐行读过的示例，本讲实践的改造基底 |
| `examples/on_window_close_quit.rs` | 演示 `on_window_closed` + `cx.quit()` 的官方示例 |
| `../gpui_platform/src/gpui_platform.rs` | `application()` / `current_platform()` 的门面实现 |
| `../gpui_web/src/platform.rs` | 浏览器平台的 `run`，用于说明 `run_embedded` 的场景 |

> 提示：本讲引用了 gpui crate 之外的三个文件（后两行），永久链接会指向仓库内对应路径。

## 4. 核心概念与源码讲解

### 4.1 Application：应用的构建与启动入口

#### 4.1.1 概念说明

在 u1-l2 里我们把 `application().run(|cx| ...)` 当作一个整体黑盒。现在拆开：这一行里其实有两个类型、三个角色。

- **`Application`**：一个「尚未启动的应用」。它只在 `main` 函数里短暂存在，用来做初始配置（资产源、HTTP 客户端、退出策略），然后调用 `run` 把自己消耗掉。源码里它的定义只有一行——包着 `AppCell` 的 `Rc`：

  [src/app.rs:L143-L145](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L143-L145)：`Application` 就是 `Rc<AppCell>` 的包装。文档注释也明说：除了初始配置和启动，你几乎不会和这个类型打交道。

- **`AppCell`**：`RefCell<App>` 的薄包装，纯粹为了在调试双重借用时加日志（后面 4.2 详讲）。

- **`App`**：真正的全局状态容器——所有实体（`EntityMap`）、所有窗口、键位表、全局单例、执行器……官方文档 [docs/contexts.md:L7-L9](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/docs/contexts.md#L7-L9) 对它的定义是：根上下文，拥有一切实体的数据。

为什么拆成三层？因为「配置期」和「运行期」需要不同的能力：配置期需要 builder 式的链式方法（按值消费 `self`、可移动），运行期需要被平台反复地借用。`Rc<AppCell>` 正是两者之间的传送带：`Application` 持有它，`run` 把它克隆进闭包，之后平台每想起「我该调用应用代码了」，就从这个 `Rc` 里借出 `&mut App`。

#### 4.1.2 核心流程

从 `main` 到第一个窗口出现，`Application` 一侧的流程是：

```text
application()                        # gpui_platform 按操作系统构造平台
    │
    ├─ Application::with_platform    # App::new_app：构造 App 装入 Rc<AppCell>
    │      ├─ assert 必须在主线程构造
    │      ├─ 初始化 EntityMap / Keymap / 执行器 / 文本系统
    │      └─ 向平台注册 on_quit / on_keyboard_layout_change 等回调
    │
    ├─ .with_assets(...)             # 可选：设置资产源（图片、SVG）
    ├─ .with_http_client(...)        # 可选：设置 HTTP 客户端
    ├─ .with_quit_mode(...)          # 可选：设置退出策略
    │
    └─ .run(on_finish_launching)     # 把控制权交给 platform.run（见 4.3）
           └─ 事件循环启动后回调一次：borrow_mut 得到 &mut App → 你的代码
```

`App::new_app` 里最值得注意的三件事：

1. **主线程断言**：不在主线程构造直接 `assert!` 失败。
2. **`Rc::new_cyclic`**：`App` 内部有一个指回 `AppCell` 的 `Weak`（字段 `this`），用循环构造避免先有鸡还是先有蛋的问题。
3. **提前注册平台回调**：键盘布局变化、系统唤醒、退出……这些回调此刻就注册好，之后平台随时能反向调用到 `App`。

#### 4.1.3 源码精读

**门面：`application()` 如何选平台**

[../gpui_platform/src/gpui_platform.rs:L13-L21](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_platform/src/gpui_platform.rs#L13-L21)：`application()` 在 wasm 下走浏览器后端，否则用 `Application::with_platform(current_platform(false))` 构造。

[../gpui_platform/src/gpui_platform.rs:L57-L81](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_platform/src/gpui_platform.rs#L57-L81)：`current_platform` 按 `target_os` 返回 macOS / Windows / Linux / Web 平台实现，参数 `headless` 控制是否以无头模式运行。

**构造：`with_platform` 与 `App::new_app`**

[src/app.rs:L174-L182](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L174-L182)：`with_platform` 接收调用方自己提供的平台实现，默认资产源为空、HTTP 客户端为 `NullHttpClient`。

[src/app.rs:L769-L779](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L769-L779)：`App::new_app` 的开头——从平台取出前台/后台执行器，并断言 `background_executor.is_main_thread()`，报错信息是 "must construct App on main thread"。

[src/app.rs:L790-L793](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L790-L793)：用 `Rc::new_cyclic` 创建 `AppCell`，`App` 的第一个字段 `this` 就是回指自己的 `Weak<AppCell>`。

[src/app.rs:L903-L910](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L903-L910)：向平台注册 `on_quit` 回调——平台通知「该退出了」时，升级 `Weak` 并调用 `cx.borrow_mut().shutdown()`。这是 4.3 退出链的入口。

**`App` 里装了什么（节选）**

[src/app.rs:L675-L690](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L675-L690)：`App` 结构体开头几行——`this: Weak<AppCell>`、`platform: Rc<dyn Platform>`，随后是文本系统、动作注册表、后台/前台执行器、`entities: EntityMap`（u2-l2 的主角）。

[src/app.rs:L687-L708](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L687-L708)：窗口表 `windows: SlotMap<WindowId, Option<Box<Window>>>`、键位表 `keymap`，以及一族观察者集合——其中就有本讲要用的 `quit_observers` 和 `window_closed_observers`。

**配置钩子一览**

| 方法 | 行号 | 作用 |
| --- | --- | --- |
| `with_assets` | [L199-L207](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L199-L207) | 设置资产源，同时重建 `SvgRenderer`（SVG 图标要用它加载） |
| `with_http_client` | [L209-L215](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L209-L215) | 设置 HTTP 客户端（默认是「什么都不做」的 `NullHttpClient`） |
| `with_quit_mode` | [L217-L222](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L217-L222) | 设置退出策略（4.3 详讲） |
| `new_inaccessible` | [L184-L197](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L184-L197) | 强制关闭 AccessKit 无障碍集成 |
| `on_reopen` | [L270-L283](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L270-L283) | macOS 上点击 Dock 图标重新打开应用时回调 |
| `on_open_urls` | [L260-L268](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L260-L268) | 系统要求本应用打开若干 URL 时回调 |

注意这些方法的共同写法：`borrow_mut` → 改字段 → `drop` 归还借用 → 返回 `self` 继续链式调用。配置发生在 `run` 之前，此时借用没有任何竞争。

#### 4.1.4 代码实践：观察 `with_quit_mode` 的效果

**实践目标**：验证「在 Linux/Windows 上，GPUI 默认关掉最后一个窗口就退出」，以及改成 `QuitMode::Explicit` 后行为如何变化。

**操作步骤**：

1. 打开 [examples/hello_world.rs:L92-L109](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs#L92-L109)，把第 93 行临时改为（示例代码）：

   ```rust
   application()
       .with_quit_mode(gpui::QuitMode::Explicit)
       .run(|cx: &mut App| {
   ```

   （原回调体的其余部分保持缩进不变即可。）

2. 运行：

   ```bash
   cargo run -p gpui --example hello_world
   ```

3. 点击窗口的关闭按钮，观察终端里的进程是否结束。

**需要观察的现象**：

- 改动前（默认 `QuitMode::Default`）：在 Linux 上关闭窗口后 `cargo run` 进程立即退出。
- 改动后（`QuitMode::Explicit`）：关闭窗口后**进程仍在运行**，终端没有回到提示符，需要 `Ctrl-C` 手动终止。

**预期结果**：与上面两条一致。原理在 [src/app.rs:L1854-L1858](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1854-L1858)：`QuitMode::Explicit` 时 `quit_on_empty = false`，最后一个窗口关闭也不会调用 `cx.quit()`。本讲义没有替你运行过这条命令，结果标注为「待本地验证」。

4. 实验完用 `git checkout -- examples/hello_world.rs` 还原。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `App` 的字段 `this` 是 `Weak<AppCell>` 而不是 `Rc<AppCell>`？

**答案**：`App` 本身就存放在 `AppCell` 里，而 `AppCell` 已经被 `Application` 的 `Rc` 持有。如果 `App.this` 也是强引用，就形成 `Rc → AppCell → App → Rc` 的引用环，引用计数永远不为零，应用退出后整棵状态树都无法释放。`Weak` 只能在需要时 `upgrade()`，不阻止回收。

**练习 2**：`with_assets` 的文档说它会同时重建 `SvgRenderer`。为什么不像 `http_client` 那样只赋值一个字段？

**答案**：[src/app.rs:L200-L207](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L200-L207) 显示 `SvgRenderer::new(asset_source)` 用新的资产源重新构造了渲染器——SVG 渲染器在创建时就要绑定资产源（之后要从它加载 `.svg` 文件），所以必须整体重建而不是只换字段。

**练习 3**：`Application` 的配置方法都按值拿 `self`、返回 `Self`。这和拿 `&mut self` 相比有什么好处？

**答案**：按值消费让配置成为「一次性」流程——配置完成后你手里不再有半配置状态的 `Application`，只能继续链式调用或交给 `run`（`run` 也按值拿 `self`）。拿 `&mut self` 则允许调用方保留句柄、在启动后再改配置，状态更难推理。

### 4.2 AppCell 与 RefCell：单前台线程的借用模型

#### 4.2.1 概念说明

GPUI 有一条贯穿全部源码的架构决定：**所有应用状态的读写都发生在单一前台线程上**。这不是因为作者偷懒，而是因为 UI 开发里 90% 的痛点是「状态被并发改了却不知道是谁改的」。GPUI 用两个机制把并发错误变成「要么编译不过、要么立刻 panic」：

1. `App` 不是 `Send`，后台线程拿不到它（u2-l5 会讲前后台执行器的分工）。
2. `App` 装在 `RefCell` 里——运行时独占检查。

于是 `AppCell` 的一生只有一种节奏：

```text
平台事件到来
    └─ AppCell::borrow_mut()          # 独占地借出 &mut App
           └─ 你的回调执行（期间任何人再 borrow/borrow_mut 都会 panic）
    └─ RefMut 析构，归还借用
```

为什么需要 `RefCell` 而不是直接把 `&mut App` 存在平台里？因为 `App` 的入口太多：`run` 的启动回调、`ApplicationHandle::update`、`AsyncApp::update`、各种平台回调……Rust 无法在编译期为「同一时刻只有一个入口在跑」提供证明，`RefCell` 把这个证明变成运行时检查。代价是：**重入即 panic**。这条规则你在自己的代码里也要遵守——绝不在已经持有 `&mut App` 的回调里，再通过另一个句柄去更新 App。

#### 4.2.2 核心流程

借用关系全景：

```text
Application(Rc<AppCell>)          # 配置期持有强引用
        │ run() 克隆 Rc 进闭包，Application 自身被消耗
        ▼
platform.run(closure)             # 事件循环持有闭包，闭包持有 Rc<AppCell>
        │ 事件到来，执行闭包
        ▼
let cx = &mut *this.borrow_mut(); # 独占可变借用，持续到回调结束
        ▼
on_finish_launching(cx)           # 你的代码拿到 &mut App

AsyncApp { app: Weak<AppCell> }   # 异步上下文只持弱引用
        │ 每次调用 .update(...) 时
        ▼
upgrade() → Rc<AppCell> → borrow_mut() → 执行 → 归还
```

两个要点：

- `run` 的启动回调**整个执行期间**都持有可变借用（见下面源码），所以在回调里再走任何会 `borrow_mut` 的入口都会 panic。
- `AsyncApp` 每次方法调用都是「升级弱引用 → 短暂借用 → 归还」，不跨 `await` 点持有借用，因此不会卡住别人。

另外，借出 `&mut App` 之后 GPUI 内部还有一层「更新计数 + 效果队列」：`App::update` 会递增 `pending_updates`，在最外层更新结束时执行 `flush_effects`，把累积的 `Notify`/`Emit`/`RefreshWindows`/`Defer` 等效果（[src/app.rs:L2839-L2860](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2839-L2860) 的 `Effect` 枚举）逐个消化。你在 u1-l2 见过的「`cx.notify()` 引发下一帧重绘」，就是在这一步被排队处理的。本讲只需记住：**借用的尾巴上挂着一次效果冲刷**。

#### 4.2.3 源码精读

**AppCell：带调试日志的 RefCell**

[src/app.rs:L78-L83](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L78-L83)：注释自嘲 "Temporary(?) wrapper"——存在的意义是帮助调试双重借用。

[src/app.rs:L98-L104](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L98-L104)：`borrow_mut` 直接转发给内部 `RefCell`，外加一行条件日志——注意 `option_env!` 是**编译期**读取环境变量，想看这行日志必须在构建时就设置 `TRACK_THREAD_BORROWS=1`。

[src/app.rs:L117-L141](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L117-L141)：`AppRef`/`AppRefMut` 是对 `Ref`/`RefMut` 的 `Deref` 包装，Drop 时同样打日志，用于追踪「哪个线程借的、什么时候还的」。

**run：回调期间借用被独占**

[src/app.rs:L224-L236](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L224-L236)：`run` 的全部实现——克隆 `Rc<AppCell>`，取出平台，把「borrow_mut + 调用你的回调」打包成 `FnOnce` 交给 `platform.run`。`let cx = &mut *this.borrow_mut();` 这行产生的 `RefMut` 会活到语句块结束，也就是**你的整个回调执行期间**。

**ApplicationHandle：官方文档明说会 panic 的重入口**

[src/app.rs:L156-L163](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L156-L163)：`ApplicationHandle::update` 的文档注释写着 "Must not be called re-entrantly … will panic on a double borrow"——这是 GPUI 自己对 `RefCell` 规则的正式警告。

**AsyncApp：弱引用 + 短暂借用**

[src/app/async_context.rs:L15-L26](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs#L15-L26)：`AsyncApp` 内部持有 `Weak<AppCell>`，文档说明它被丢弃时会 panic。

[src/app/async_context.rs:L162-L167](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs#L162-L167)：`AsyncApp::update` 每次调用都 `borrow_mut` 一次——如果在 `run` 回调内同步调用它，就是教科书级的双重借用。

**效果冲刷：借用的收尾工作**

[src/app.rs:L1045-L1063](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1045-L1063)：`App::update` 用 `pending_updates` 计数嵌套更新，只有最外层（计数归 1 时）才触发 `flush_effects`，`flushing_effects` 标志防止递归冲刷。

[src/app.rs:L1627-L1660](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1627-L1660)：`flush_effects` 的主循环——从 `pending_effects` 队列逐个弹出 `Effect` 并应用，队列空了才退出。你的 `cx.notify()`、`cx.emit(...)`、`cx.defer(...)` 最终都在这里兑现。

#### 4.2.4 代码实践：亲手触发一次双重借用 panic

**实践目标**：用纯公开 API 在 `run` 回调里制造第二次 `borrow_mut`，观察 `RefCell` 的运行时保护。

**操作步骤**：

1. 再次临时修改 [examples/hello_world.rs:L92-L109](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs#L92-L109) 的 `run_example`，在回调开头插入两行（示例代码）：

   ```rust
   fn run_example() {
       application().run(|cx: &mut App| {
           // 此时 run 的闭包正持有 AppCell 的可变借用
           let async_cx = cx.to_async();
           async_cx.update(|_| {}); // AsyncApp::update 会再次 borrow_mut → panic
           // ...原有代码...
       });
   }
   ```

2. 运行：`cargo run -p gpui --example hello_world`。

**需要观察的现象**：应用在启动瞬间崩溃，终端输出一段 panic 信息，调用栈里能看到 `RefCell::borrow_mut` 与 `AsyncApp::update`。

**预期结果**：panic 消息包含 `already borrowed: BorrowMutError`（具体措辞随 Rust 版本可能略有差异，「待本地验证」）。原因链：`run` 闭包 [L232-L235](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L232-L235) 持有 `borrow_mut` → `async_cx.update` 内部 [L163-L167](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs#L163-L167) 再次 `borrow_mut` → 运行时冲突。

3. 观察完注释掉那两行（或 `git checkout` 还原）。

**思考延伸**：这不是故意刁难——真实项目里的典型翻车场景是「在某个回调里调用了一个库函数，而那个库函数内部又 `spawn` 了一个立刻执行的前台任务并 `update`」。GPUI 宁可让你立刻 panic，也不让两段代码同时改一个 `App`。

#### 4.2.5 小练习与答案

**练习 1**：`AppCell` 的日志用 `option_env!("TRACK_THREAD_BORROWS")` 控制。为什么不用 `std::env::var` 在运行时读取？

**答案**：`option_env!` 宏在**编译期**把环境变量烧进二进制，不设置时整个 `if` 分支被优化掉，正常运行零开销。如果用运行时 `env::var`，每次借用都要查一次环境变量，热路径上代价不可接受。

**练习 2**：既然 `run` 回调独占借用，那 `cx.spawn` 出来的异步任务为什么不会和后续的事件处理撞车？

**答案**：异步任务拿到的是 `AsyncApp`（持有 `Weak<AppCell>`），它**并不持续持有借用**；只有真正执行到某个方法（如 `update`）时才短暂 `borrow_mut`，用完立刻归还。事件处理与异步任务都在同一个前台线程上排队执行，天然串行，因此只要没人「在持有借用期间重入」，就不会冲突。

**练习 3**：如果把 `AppCell` 里的 `RefCell<App>` 换成 `Mutex<App>`，能解决什么、又失去什么？

**答案**：`Mutex` 允许多线程访问，能解决「重入 panic」——重入者会阻塞等待而不是崩溃。但代价是：可能出现死锁（两个入口互相等对方归还锁），而且 UI 状态的变更不再有单一顺序，竞态 bug 更难复现。GPUI 选择单线程 + `RefCell`：错误更早暴露、推理更简单。

### 4.3 平台事件循环：`run` 把控制权交给谁

#### 4.3.1 概念说明

`Application::run` 做的最后一件大事，是把控制权**永远地**交出去：

[src/platform.rs:L125-L131](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/platform.rs#L125-L131)：`Platform` trait 的头几个方法——`run` 接收一个 `FnOnce` 回调并启动事件循环，`quit` 请求退出。在桌面平台上 `run` 会**阻塞**到应用退出，你的 `main` 函数在 `application().run(...)` 这行之后就没有代码了。

之后你的代码不再是「主动运行」，而是「被事件驱动」：用户点击、按键、窗口重绘请求、定时器到点……平台把这些变成一次次的「借出 `&mut App` → 调用 GPUI → GPUI 调用你」。

**`run` vs `run_embedded`**：有些宿主环境不允许 GPUI 拥有事件循环——最典型的是浏览器里的 wasm（事件循环属于 JS），或把 GPUI 视图嵌进别的原生应用。这类平台的 `run` 实现「调用启动回调然后立即返回」。为此 GPUI 提供了 `run_embedded`：它返回一个 `ApplicationHandle`，由嵌入方持有（drop 即释放应用），并在外部事件循环每次交还控制权时通过 `handle.update(...)` 重新进入 GPUI。

**退出不是一瞬间**：`cx.quit()` 只是「请求平台退出」；平台答应之后回调 GPUI 注册的 `on_quit`，进入 `App::shutdown()`。shutdown 会给你注册的 `on_app_quit` 回调一个运行异步收尾工作的机会，但只等 200ms（`SHUTDOWN_TIMEOUT`）。

#### 4.3.2 核心流程

一次完整退出的调用链：

```text
cx.quit()                              # App::quit，转发给平台
    └─ platform.quit()                 # 平台开始标准退出流程
           └─ 平台调用 new_app 时注册的 on_quit 回调
                  └─ App::shutdown()
                         ├─ 1. 逐个调用 quit_observers（on_app_quit 注册的）
                         │      收集它们返回的 future
                         ├─ 2. 清空所有窗口（windows.clear()）
                         ├─ 3. flush_effects()（消化残留效果）
                         ├─ 4. quitting = true
                         ├─ 5. block_with_timeout(SHUTDOWN_TIMEOUT, join_all(futures))
                         │      # 最多等 200ms；超时记 error 日志
                         └─ 6. quitting = false，shutdown 返回，应用真正结束
```

而「最后一个窗口关闭」是否自动触发 `cx.quit()`，由 `QuitMode` 决定：

| QuitMode | 行为 |
| --- | --- |
| `Default` | macOS 上等于 `Explicit`，其他平台等于 `LastWindowClosed` |
| `LastWindowClosed` | 最后一个窗口关闭即退出 |
| `Explicit` | 只有显式 `cx.quit()`（或系统退出请求）才退出 |

对应的判断逻辑就在窗口移除的代码里：`QuitMode::Explicit => false`、`QuitMode::LastWindowClosed => true`、`QuitMode::Default => cfg!(not(target_os = "macos"))`。

另外两个生命周期钩子：`on_window_closed`（任意窗口关闭时回调，此时窗口已不可访问）和 `on_app_restart`（重启前回调，早于 `on_app_quit`）。它们与 `on_app_quit` 一样返回 `Subscription`——这个类型 drop 即注销回调，所以要么把返回值存起来，要么 `.detach()` 让它活得和 App 一样久。

#### 4.3.3 源码精读

**Platform trait：事件循环接口**

[src/platform.rs:L130-L131](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/platform.rs#L130-L131)：`run` 与 `quit` 的签名——`run` 拿走的回调类型是 `Box<dyn 'static + FnOnce()>`，即「只调用一次」。

[src/platform.rs:L202](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/platform.rs#L202)：`on_quit`——平台注册「应用将被要求退出」的回调，GPUI 在 `App::new_app` 里用它接上 `shutdown`。

**run 与 run_embedded 的对照**

[src/app.rs:L238-L258](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L238-L258)：`run_embedded` 的文档——普通平台的 `run` 阻塞到应用生命期结束，应用状态靠 `run` 的栈帧保活；嵌入式平台（wasm guest、原生应用内嵌 GPUI 视图）的 `run` 调用启动回调后立即返回，所以返回一个 `ApplicationHandle` 由嵌入方持有。

[src/app.rs:L147-L170](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L147-L170)：`ApplicationHandle`——`update` 是嵌入方重新进入 GPUI 的入口（内部就是 `borrow_mut`，文档再次警告不可重入）；`to_async` 生成跨 `await` 的 `AsyncApp`。

**浏览器平台的 run：立即返回的实例**

[../gpui_web/src/platform.rs:L279-L295](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_web/src/platform.rs#L279-L295)：`WebPlatform::run` 用 `wasm_bindgen_futures::spawn_local` 启动一个本地异步任务，等浏览器图形初始化成功后调用 `on_finish_launching()`，然后**返回**——事件循环留在 JS 那边，这正是 `run_embedded` 存在的场景。

**退出链**

[src/app.rs:L75-L76](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L75-L76)：`SHUTDOWN_TIMEOUT = Duration::from_millis(200)`——`on_app_quit` 回调的总预算。

[src/app.rs:L998-L1001](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L998-L1001)：`App::quit` 只有一行——委托 `platform.quit()`。注意它拿 `&self` 就够了，因为真正的状态变更发生在平台一侧。

[src/app.rs:L946-L970](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L946-L970)：`App::shutdown` 的完整实现，与 4.3.2 的流程图逐行对应：先取出所有 `quit_observers` 并收集 future，清空窗口、冲刷效果，然后用 `block_with_timeout` 等待所有收尾 future，超时则记录 `"timed out waiting on app_will_quit"`。

**注册钩子**

[src/app.rs:L2285-L2303](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2285-L2303)：`on_app_quit`——接收一个返回 future 的闭包，注册进 `quit_observers`，返回 `Subscription`。文档明言：到这一步**无法取消退出**。

[src/app.rs:L2320-L2329](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2320-L2329)：`on_window_closed`——回调参数是 `(&mut App, WindowId)`，此时窗口已从表中移除。

**窗口关闭 → 决定是否退出**

[src/app.rs:L1849-L1862](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1849-L1862)：窗口被移除后的 `trail` 收尾逻辑——先通知所有 `window_closed_observers`，再按 `quit_mode` 计算 `quit_on_empty`，窗口全空且允许时调用 `cx.quit()`。

[src/app.rs:L315-L325](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L315-L325)：`QuitMode` 枚举定义，`Default` 的文档注释直接写明了平台差异。

**Subscription：回调的注销凭证**

[src/subscription.rs:L147-L168](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/subscription.rs#L147-L168)：`Subscription` 是 `#[must_use]` 类型——Drop 时自动注销回调；`detach()` 主动放弃注销权，让回调随订阅对象的生命期自然结束。这就是为什么官方示例里都要写 `.detach()`。

**官方示例：on_window_close_quit**

[examples/on_window_close_quit.rs:L40-L50](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/on_window_close_quit.rs#L40-L50)：`run_example` 的开头——绑定 `cmd-w` 到 `CloseWindow` 动作，并用 `cx.on_window_closed(...).detach()` 注册「窗口全关就 `cx.quit()`」。在 macOS 上（默认 `QuitMode::Explicit`）这正是必须的手动退出写法；在 Linux 上这行其实是双保险。

[examples/on_window_close_quit.rs:L52-L66](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/on_window_close_quit.rs#L52-L66)：连续打开两个窗口——关掉一个另一个还在，两个都关掉应用才退出。用它验证本节内容最直观。

#### 4.3.4 代码实践：给 hello_world 挂上退出钩子并观察超时

**实践目标**：体验 `on_app_quit` 的完整流程，验证「收尾 future 超过 200ms 会被放弃」。

**操作步骤**：

1. 临时修改 [examples/hello_world.rs:L92-L109](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs#L92-L109)，在 `run` 回调开头加入（示例代码）：

   ```rust
   use std::time::Duration; // 放在文件顶部 use 区

   // 回调内：
   cx.on_app_quit(|cx| {
       // future 必须是 'static 的，所以先把要用的东西克隆出来
       let executor = cx.background_executor().clone();
       async move {
           eprintln!("[on_app_quit] 开始收尾……");
           // 故意睡 500ms，超过 SHUTDOWN_TIMEOUT 的 200ms 预算
           executor.timer(Duration::from_millis(500)).await;
           eprintln!("[on_app_quit] 收尾完成"); // 预期不会打印
       }
   })
   .detach();
   ```

2. 运行 `cargo run -p gpui --example hello_world`，然后关闭窗口（Linux/Windows 上关闭唯一窗口即触发默认退出；macOS 上需要 `Cmd-Q`，或参照 on_window_close_quit 示例手动 `cx.quit()`）。

**需要观察的现象**：

- 关闭窗口后，终端先出现 `[on_app_quit] 开始收尾……`。
- 大约 200ms 后进程退出，**没有** `[on_app_quit] 收尾完成`。
- 如果以 `RUST_LOG=error` 运行，还能看到 `timed out waiting on app_will_quit` 的错误日志。

**预期结果**：与上述一致，对应 [src/app.rs:L960-L967](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L960-L967) 的 `block_with_timeout(SHUTDOWN_TIMEOUT, ...)` 与超时日志。结果标注「待本地验证」。

3. 把 `Duration::from_millis(500)` 改成 `100`（低于 200ms），重新运行——这次两条日志都应出现，且进程在收尾完成后才退出。
4. `git checkout -- examples/hello_world.rs` 还原。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `App::quit` 拿 `&self`（不可变借用）就够了，而 `shutdown` 需要 `&mut self`？

**答案**：`quit` 只做一件事——调用 `platform.quit()`（[src/app.rs:L999-L1001](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L999-L1001)），平台句柄本身是 `Rc<dyn Platform>`，不需要改 `App` 的任何字段。真正的状态清理（清窗口、跑观察者）发生在稍后平台回调的 `shutdown` 里，那一步要改 `App`，所以是 `&mut self`。

**练习 2**：用户在窗口标题栏点关闭按钮，`on_window_closed` 和 `on_app_quit` 都可能被触发。它们的触发时机有何不同？

**答案**：`on_window_closed` 在**每个**窗口被移除时触发一次（回调拿到 `WindowId`，见 [src/app.rs:L1849-L1852](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1849-L1852)）；`on_app_quit` 只在整个应用退出、`shutdown` 开始时触发一次（[src/app.rs:L951-L953](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L951-L953)）。关掉一个窗口不等于退出应用（可能还有别的窗口，或 `QuitMode::Explicit`）。

**练习 3**：在 wasm 平台上为什么不能用普通的 `Application::run` 长期阻塞？

**答案**：浏览器禁止 JS 主线程被长时间占用——阻塞意味着页面冻结、无法响应任何事件。[../gpui_web/src/platform.rs:L279-L295](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_web/src/platform.rs#L279-L295) 的实现是异步初始化图形后立即返回，把控制权还给浏览器；所以嵌入方要用 `run_embedded` 拿 `ApplicationHandle`，在浏览器每次让出控制权时再进入 GPUI。

## 5. 综合实践

把本讲三个模块串成一个「生命周期实验台」。基于 hello_world 改造出下面的完整示例（示例代码，可直接替换 `examples/hello_world.rs` 的内容，实验后还原）：

```rust
// examples/hello_world.rs —— 生命周期实验版（示例代码）
#![cfg_attr(target_family = "wasm", no_main)]

use gpui::{App, Context, QuitMode, Window, WindowOptions, div, prelude::*};
use gpui_platform::application;
use std::time::Duration;

struct HelloWorld;

impl Render for HelloWorld {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        div().size_full().bg(gpui::black()).child("Lifecycle Lab")
    }
}

fn run_example() {
    application()
        // 模块一：配置期钩子（Application 层）
        .with_quit_mode(QuitMode::LastWindowClosed)
        .run(|cx: &mut App| {
            // 模块三：窗口级生命周期钩子
            cx.on_window_closed(|cx, window_id| {
                eprintln!("[on_window_closed] 窗口 {window_id:?} 已关闭，剩余 {}", cx.windows().len());
            })
            .detach();

            // 模块三：应用级退出钩子（异步收尾，预算 200ms）
            cx.on_app_quit(|cx| {
                let executor = cx.background_executor().clone(); // future 需 'static
                async move {
                    eprintln!("[on_app_quit] 保存状态中……");
                    executor.timer(Duration::from_millis(100)).await;
                    eprintln!("[on_app_quit] 保存完成");
                }
            })
            .detach();

            // 模块二：取消下面两行的注释，可观察双重借用 panic
            // let async_cx = cx.to_async();
            // async_cx.update(|_| {});

            cx.open_window(WindowOptions::default(), |_window, cx| {
                cx.new(|_| HelloWorld)
            })
            .unwrap();
        });
}

#[cfg(not(target_family = "wasm"))]
fn main() {
    run_example();
}
```

实验步骤与检查点：

1. 原样运行：窗口关闭 → 终端依次出现 `[on_window_closed]`（窗口数变 0）、`[on_app_quit] 保存状态中……`、`[on_app_quit] 保存完成` → 进程退出。这条输出顺序就是 4.3.2 流程图的直接证据。
2. 把 `timer` 改为 500ms：第 3 条日志消失（超时被放弃）。
3. 把 `with_quit_mode` 换成 `QuitMode::Explicit`：关窗后只剩 `[on_window_closed]` 日志，进程不退出——验证 4.1.4 的结论。
4. 取消双重借用两行的注释：应用在启动瞬间 panic——验证 4.2 的借用规则。
5. 每步之间用 `git checkout -- examples/hello_world.rs` 或编辑器撤销来控制变量。

预期结果：四个检查点全部符合描述（本讲义未替读者执行，标注「待本地验证」）。

## 6. 本讲小结

- `Application` 只是 `Rc<AppCell>` 的配置期包装：`with_platform` 构造 `App`，`with_assets`/`with_http_client`/`with_quit_mode` 等链式方法在启动前完成配置，`run` 消耗掉它并把控制权交给平台事件循环。
- `App` 是唯一的全局状态容器（实体、窗口、键位表、观察者、执行器……），其他一切上下文最终都 `Deref` 到它。
- `AppCell = RefCell<App>`：单前台线程 + 运行时独占借用检查。`run` 的回调整个执行期间持有 `borrow_mut`，任何重入（如在其中调用 `AsyncApp::update`）都会 panic。
- 借用的收尾挂着 `flush_effects`：`Notify`/`Emit`/`RefreshWindows`/`Defer` 等效果在每次最外层更新结束时被排队消化。
- 退出是一条链：`cx.quit()` → `platform.quit()` → 平台回调 `on_quit` → `App::shutdown()`（运行 `on_app_quit` future，预算 `SHUTDOWN_TIMEOUT`=200ms）→ 进程结束。
- `run` 在桌面平台阻塞到应用结束；`run_embedded` 面向「事件循环属于别人」的宿主（如浏览器 wasm），返回 `ApplicationHandle` 供嵌入方反复进入。

## 7. 下一步学习建议

本讲解决了「App 是什么、怎么启动、怎么退出」。下一讲 **u2-l2 Entity 与所有权模型** 将钻进 `App` 最大的字段——`entities: EntityMap`，回答：为什么创建一个状态对象必须走 `cx.new`、`Entity<T>` 句柄为什么必须借助上下文才能读写、强句柄互持为什么导致内存泄漏。

推荐的源码预读（按顺序）：

1. `src/_ownership_and_data_flow.rs`——官方所有权文档，篇幅不长，是下一讲的主纲。
2. `src/app/entity_map.rs` 的开头注释与 `EntityMap` 结构体——带着「slot 预留」的问题去读。
3. 回头再看一眼本讲的 [src/app.rs:L675-L690](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L675-L690)，你会发现 `Context<T>` 一族（u2-l3）全部建立在本讲的 `App` 借用模型之上。
