# xdg-desktop-portal 集成：文件选择器与系统级对话框

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚为什么 Linux 上的文件选择器不走 GTK/Qt 原生工具包，而是经 `ashpd` 客户端请求 xdg-desktop-portal 服务，以及 portal 机制对沙箱化桌面应用（Flatpak 等）的必要性。
2. 逐步描述 `prompt_for_paths` 在 Linux 上的完整异步链路：从 `App::prompt_for_paths` 到 `LinuxPlatform` 的 portal 请求，再到 `oneshot` 通道把结果（选中路径 / 用户取消 / 打开失败）交回调用方。
3. 理解 `FILE_PICKER_PORTAL_MISSING` 错误常量的触发条件——它只在 `ashpd::Error::PortalNotFound` 这一个分支被使用，以及上游 Zed 编辑器收到该错误后如何回退到应用内文件查找器。
4. 说明 `PathPromptOptions` 四个字段在 Linux 实现中的真实语义（有一个字段根本没被用上），以及 `LinuxClient::window_identifier` 在 Wayland、X11、headless 三种后端下的三种实现姿态。

本讲是第 5 单元「Linux 平台深入」的收官篇。u5-l1 建立了「LinuxPlatform 外壳 + LinuxClient 三后端」的骨架，u5-l2/l3/l4 分别深入了 headless、X11、Wayland 后端；本讲把视角拉回到「外壳 + 各后端」协作的一组具体能力上：系统级对话框与桌面设置订阅。

## 2. 前置知识

### 2.1 DBus 会话总线与 xdg-desktop-portal

**DBus** 是 Linux 桌面的进程间通信（IPC）标准，分为系统总线（system bus）和会话总线（session bus）。每个登录会话有一条会话总线，服务以「总线名」（如 `org.freedesktop.portal.Desktop`）注册其上，其他进程通过总线向该名字发起方法调用。

**xdg-desktop-portal** 是 freedesktop.org 定义的一组标准化 DBus 接口的总称，运行形态是：

- 一个常驻服务 `xdg-desktop-portal` 挂在会话总线上，对外暴露统一接口（FileChooser 文件选择、Settings 桌面设置、Notification 通知、OpenURI 打开链接等）。
- 桌面环境提供**后端**（backend）：GNOME 上是 `xdg-desktop-portal-gnome`/`-gtk`，KDE 上是 `xdg-desktop-portal-kde`。真正的对话框由后端用本桌面的工具包渲染——所以 GNOME 用户看到 GTK 风格的文件选择器，KDE 用户看到 Qt 风格的。
- 应用只需要面对一套稳定的 DBus 接口，不需要关心自己运行在 GNOME、KDE 还是别的桌面。

### 2.2 为什么沙箱化应用离不开 portal

以 Flatpak 打包的应用被沙箱限制：看不到宿主文件系统的任意路径、不能直接弹出自己的「系统」对话框去骗用户授权。portal 是官方出路——对话框在**沙箱之外**由 portal 服务代为弹出，用户选中的文件以 URI 形式返还，权限授予由文档门户等机制完成。Zed 虽是自绘 UI 的应用，但同样受益：portal 让它在任何桌面都获得与本桌面一致的对话框，且天然兼容沙箱分发。

### 2.3 ashpd 与 async-io

**ashpd**（XDG Desktop Portal 的 Rust 客户端库）把上述 DBus 接口封装成异步 Rust API。Zed 在 workspace 根 `Cargo.toml` 统一声明该依赖，并只按需开启功能门：

[../../Cargo.toml:L522-L529](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../../Cargo.toml#L522-L529)

这段声明了 `ashpd = "0.13"`，`default-features = false`，仅启用 `async-io`、`notification`、`open_uri`、`file_chooser`、`settings`、`trash` 六个 feature——即本讲涉及的文件选择器与设置订阅，加上通知（u6-l3 会讲到）等。

注意 `async-io`：这里显式选用 async-io 作为 ashpd 的异步后端，与 GPUI 在 Linux 上使用的 smol/async-io 生态保持一致，使 DBus 的读写能被 GPUI 已有的执行器驱动。

### 2.4 oneshot 通道

`futures::channel::oneshot` 是「一次性、单值」的异步通道：`channel()` 返回 `(Sender, Receiver)`，Sender 只能发一次，Receiver 是一个可以 `.await` 的 future。u2-l4 已经介绍过：gpui 平台契约里凡是「平台稍后异步给结果」的方法（如 `prompt_for_paths`），返回值都是 `oneshot::Receiver` 而不是 `Task`，因为 `Task` 与 GPUI 执行器绑定，而通道是纯数据结构，调用方拿到后想怎么等都行。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `../gpui_linux/src/linux/platform.rs` | `LinuxPlatform` 外壳与 `LinuxClient` 契约所在；本讲主角 `prompt_for_paths`/`prompt_for_new_path`/`window_identifier` 的外壳实现都在这里 |
| `../gpui_linux/src/linux/xdg_desktop_portal.rs` | 名字叫 portal 的模块，实际承载的是 **Settings 门户**的 calloop 事件源 `XDPEventSource` |
| `../gpui_linux/src/linux/wayland/client.rs` | Wayland 后端：`window_identifier` 的异步实现与 `XDPEventSource` 的消费回调 |
| `../gpui_linux/src/linux/x11/client.rs` | X11 后端：`window_identifier` 的同步实现与 `XDPEventSource` 的消费回调 |
| `../gpui/src/platform.rs` | 契约层：`Platform::prompt_for_paths` 方法签名与 `PathPromptOptions` 结构体定义 |
| `../gpui/src/app.rs` | 调用方入口 `App::prompt_for_paths`（纯转发） |
| `../workspace/src/workspace.rs` | 真实消费者：Zed 工作区收到 portal 错误后的回退逻辑 |
| `../gpui/src/app/test_context.rs` | 测试侧的 `simulate_path_prompt_response`，展示测试平台如何绕开真实 portal |

## 4. 核心概念与源码讲解

本讲三个最小模块：**xdg_desktop_portal**（portal 机制与设置事件源）、**PathPromptOptions 与 prompt_for_paths 异步链路**、**LinuxClient::window_identifier**。

### 4.1 xdg_desktop_portal 模块：portal 机制与 Settings 事件源

#### 4.1.1 概念说明

先纠正一个容易产生的误会：`gpui_linux/src/linux/xdg_desktop_portal.rs` 这个模块里**没有文件选择器的任何代码**。文件选择器的实现在 `platform.rs`（见 4.2）。这个模块解决的是另一个问题：

> GPUI 的 Wayland/X11 后端把 calloop 事件循环当主循环（u4-l3、u5-l4），但 portal 的 DBus 调用是异步的、运行在执行器上。**怎么把「执行器上产生的 portal 事件」变成「主循环上能轮询到的事件源」？**

答案是经典的「channel 桥」：后台任务通过 ashpd 订阅桌面设置，把每次变更 `send` 进一个 calloop channel；`XDPEventSource` 把这个 channel 包装成 calloop 的 `EventSource`，注册进主循环。这样 portal 事件与 Wayland/X11 事件就走同一条分发路径。

模块开头的文档注释也点明了它的定位：

[../gpui_linux/src/linux/xdg_desktop_portal.rs:L1-L19](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/xdg_desktop_portal.rs#L1-L19)

这段定义了四种事件：`WindowAppearance`（深浅色）、`CursorTheme`、`CursorSize`、`ButtonLayout`（标题栏按钮布局，如 `close:maximize,minimize`）。注意 `CursorTheme`/`CursorSize` 上面的 `#[cfg_attr(feature = "x11", allow(dead_code))]`——X11 后端根本不消费它们（X 服务端自己管理光标主题），加这个属性是为了在只编 x11 feature 时不产生死代码警告。

u3-l4 讲过「Linux 的 `window_appearance` 值来自 xdg-desktop-portal 的 color-scheme」——其数据源头就是这个模块。

#### 4.1.2 核心流程

```
[后台任务：ashpd Settings + async-io]          [前台：calloop 主循环]
Settings::new() ── 连接 DBus Settings 门户
  │
  ├─ 读初始值 color_scheme ──────┐
  ├─ 读初始值 cursor-theme ──────│── sender.send(Event::…) ──▶ XDPEventSource
  ├─ 读初始值 cursor-size ───────┘         (calloop channel)      │
  ├─ 读初始值 button-layout ─────┘                                │ EventSource::process_events
  │                                                               ▼
  ├─ 订阅 cursor-theme 变更流 ─┐                        wayland/x11 client 的回调：
  ├─ 订阅 cursor-size 变更流 ──┼─ 每次变更 send ──▶     更新 common.appearance、
  ├─ 订阅 button-layout 变更流 ┘                        common.button_layout、
  └─ 订阅 color-scheme 变更流 ── while let Some ──▶     cursor 主题/尺寸
```

关键点：

1. **初始值与变更流都要**：只订阅变更流会错过「应用启动时系统已是深色」的场景，所以先同步读四个初始值各发一个事件，再挂上四个变更流。
2. **失败即静默降级**：如果系统里没有 portal（很多最小化 WM 会话如此），`Settings::new().await?` 直接以错误结束整个任务，事件源永远不发事件——应用照常运行，只是拿不到外观联动。这是刻意设计的优雅降级。
3. **变更流常驻**：每个变更流的消费循环是独立 `background.spawn(...).detach()` 的任务，流不断则任务不结束。

#### 4.1.3 源码精读

先看 `XDPEventSource` 的构造，重点看「读初始值 + 订阅变更」的结构：

[../gpui_linux/src/linux/xdg_desktop_portal.rs:L25-L60](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/xdg_desktop_portal.rs#L25-L60)

`new(executor: &BackgroundExecutor)` 先建一个 calloop channel（`sender` 留给后台任务，`channel` 存进结构体），然后在**后台执行器**上 spawn 一个总任务。L33 的 `Settings::new().await?` 是与 portal 服务的握手：失败则整个任务（含外层 spawn 的结果）被丢弃——注意外层是 `.detach()`，错误不会传给任何人，这就是上一节说的「静默降级」。L35-L39 读初始 `color_scheme` 并立即发一个 `WindowAppearance` 事件；`if let Ok(...)` 说明单项读取失败只跳过该项，不影响后续。

一个值得品味的小注释在 L47-L48：

[../gpui_linux/src/linux/xdg_desktop_portal.rs:L47-L53](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/xdg_desktop_portal.rs#L47-L53)

GSettings 里 `cursor-size` 的 DBus 类型签名是 int32，用 `u32` 反序列化会报 invalid type，所以这里显式用 `i32` 读、再 `as u32` 转换——这是「类型系统与世界不符」的典型小坑。

接着是四个变更流的订阅，以 cursor-theme 为例：

[../gpui_linux/src/linux/xdg_desktop_portal.rs:L62-L79](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/xdg_desktop_portal.rs#L62-L79)

`receive_setting_changed_with_args` 返回一个异步流，每次桌面设置变化产出一项。每个流由独立的后台任务驱动（`.detach()` 常驻），任务里 `while let Some(theme) = ...next().await` 逐项转发进 channel。cursor-size（L81-L98）与 button-layout（L100-L117）的订阅结构完全同型。最后 color-scheme 的变更流则留在总任务里循环（L119-L124），四种事件的汇聚出口都是同一个 `sender`。

再看 calloop 侧的桥接：

[../gpui_linux/src/linux/xdg_desktop_portal.rs:L134-L156](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/xdg_desktop_portal.rs#L134-L156)

`EventSource` 是 calloop 的核心 trait（u4-l3 讲过它的 `process_events`/`register`/`reregister`/`unregister` 四件套）。这里的实现是**纯委托**：把 calloop 的轮询原样转交给内部的 `channel`，只在回调里把 `calloop::channel::Event::Msg(msg)` 解包后上抛。也就是说 `XDPEventSource` 本质上就是「calloop channel 的一层类型化包装」，真正的通知机制（fd 就绪唤醒）完全复用 channel。

最后是枚举映射：

[../gpui_linux/src/linux/xdg_desktop_portal.rs:L185-L191](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/xdg_desktop_portal.rs#L185-L191)

ashpd 的 `ColorScheme` 三值映射到 GPUI 的 `WindowAppearance`：`PreferDark → Dark`，`PreferLight` 与 `NoPreference` 都落到 `Light`——「无偏好」时选亮色作为默认。

消费端在两个后端的初始化里。Wayland 后端：

[../gpui_linux/src/linux/wayland/client.rs:L853-L894](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L853-L894)

`insert_source(XDPEventSource::new(&common.background_executor), …)` 把事件源注册进 Wayland 客户端的主循环（u5-l4 讲过这个 calloop loop），回调里按事件类型分发：`WindowAppearance` 写入 `common.appearance` 并对所有窗口调 `set_appearance`；`ButtonLayout` 先用 `WindowButtonLayout::parse` 解析字符串、失败回退 `linux_default()`，再刷新所有窗口的按钮布局；`CursorTheme`/`CursorSize` 更新 cursor 管理器。X11 后端的注册在 [../gpui_linux/src/linux/x11/client.rs:L488-L511](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L488-L511)，结构相同，区别是 `CursorTheme(_) | CursorSize(_) => {}`——X11 上是显式 noop（X 服务端代管）。

顺带一提，模块声明在 [../gpui_linux/src/linux.rs:L14](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux.rs#L14)，它不受 feature 门控，两种后端都可用。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「Settings 门户事件 → GPUI 外观联动」这条链路真实存在，并理解无 portal 时的静默降级。

**操作步骤**：

1. 在 Linux 桌面会话中运行任意 GPUI 应用（例如 u1-l2 你建的窗口程序，或 `cargo run -p gpui --example window`）。
2. 打开系统设置切换深色/浅色主题（GNOME/KDE 均可），观察窗口颜色联动；若是自建程序，可在 Wayland 回调处临时加 `eprintln!` 观察（只在你自己的 crate 里做，不要改 zed 源码）。
3. 用 `busctl --user list | grep -i portal` 确认会话里 portal 服务存在；再用 `systemctl --user status xdg-desktop-portal` 查看具体后端。
4. 阅读上面的 L853-L894，对照你观察到的现象，在笔记里画一张「DBus 信号 → XDPEventSource → client 回调 → window.set_appearance」的时序图。

**需要观察的现象**：主题切换后 GPUI 窗口不需要重启即跟随变化；`busctl` 输出里有 `org.freedesktop.portal.Desktop`。

**预期结果**：事件链路与 4.1.2 的流程图一致。若你的会话没有 portal，切换主题时 GPUI 无反应，但应用一切正常——这正对应 `Settings::new().await?` 失败后的静默降级。（主题联动依赖桌面环境实际接入 portal，个别环境可能需要重启应用才能拿到初始值——待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CursorTheme`/`CursorSize` 两个事件变体要加 `#[cfg_attr(feature = "x11", allow(dead_code))]`，而 `WindowAppearance` 不用？

**答案**：X11 后端在回调里对这两个事件是显式 noop（x11/client.rs L506-L508），也不在任何其他地方构造或匹配它们；只编 x11 feature 时这两个变体从未被读取，编译器会报死代码警告。`WindowAppearance` 在两种后端的回调里都被匹配，所以不需要。

**练习 2**：如果系统里完全没有 xdg-desktop-portal，`XDPEventSource::new` 返回的事件源会发生什么？应用会崩溃吗？

**答案**：不会崩溃。channel 正常创建、事件源正常注册进主循环；但后台任务在 `Settings::new().await?` 处以错误终止（任务被 detach，错误被丢弃），channel 永远收不到消息，事件源一直安静。应用只是失去外观/光标主题/按钮布局的桌面联动。

**练习 3**：为什么初始值读取（L35-L60）和变更流订阅（L62-L124）要分开做，而不是只订阅变更流？

**答案**：变更流只推送**未来**的变化；应用启动时系统当前的主题、光标设置不会被推送。若只订阅变更流，用户不碰设置的话 GPUI 就永远用默认值，与桌面实际状态不一致。所以先主动读一次初始值并发事件，再挂变更流。

### 4.2 PathPromptOptions 与 prompt_for_paths 的完整异步链路

#### 4.2.1 概念说明

文件选择器是「应用请求操作系统弹一个对话框、等用户选完拿回路径」的能力。GPUI 的契约层把它定义为 `Platform` 的两个**必需方法**（无默认实现，每个平台必须给）：

[../gpui/src/platform.rs:L190-L198](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui/src/platform.rs#L190-L198)

`prompt_for_paths` 是「打开文件/文件夹」选择器，`prompt_for_new_path` 是「另存为」选择器。两者都通过 oneshot 通道异步返回，结果的三态语义是本模块的灵魂：

| 收到的值 | 含义 |
| --- | --- |
| `Ok(Some(paths))` | 用户确认了选择 |
| `Ok(None)` | 用户取消（点了关闭/Cancel） |
| `Err(err)` | 对话框**打开失败**（例如 portal 缺失） |

「用户取消」不算错误这一点很重要——它决定 Linux 实现必须把某个 ashpd 错误变体翻译成 `Ok(None)`（见 4.2.3）。

调用方入口是 App 的纯转发：

[../gpui/src/app.rs:L1564-L1569](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui/src/app.rs#L1564-L1569)

而 `PathPromptOptions` 是跨平台的应用层参数包：

[../gpui/src/platform.rs:L2139-L2148](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui/src/platform.rs#L2139-L2148)

四个字段：`files`（可选文件吗）、`directories`（可选目录吗）、`multiple`（可多选吗）、`prompt`（确认按钮文案）。但注意——**这是跨平台契约的字段集，Linux 实现并没有全部用上**（见 4.2.3 的字段对照表），`files` 在 Linux 路径上是既不控制标题也不控制请求参数的「哑字段」。这体现了契约层「取各平台能力并集」的常见代价。

#### 4.2.2 核心流程

`prompt_for_paths` 在 Linux 上的完整链路：

```
应用代码: cx.prompt_for_paths(options)
  │  (gpui/src/app.rs:1564  纯转发)
  ▼
LinuxPlatform::prompt_for_paths          (platform.rs:401)
  ├─ let (done_tx, done_rx) = oneshot::channel();   ── done_rx 直接返回给调用方
  ├─ 零 feature 构建时: done_tx.send(Ok(None)) 即刻返回（永远是「取消」）
  ├─ identifier = self.inner.window_identifier()    ── 4.3 的入口，拿到一个 future
  └─ foreground_executor().spawn(async move {
        title = directories ? "Open Folder" : "Open File"
        OpenFileRequest::default()
          .identifier(identifier.await)     // 等窗口标识符就绪（Wayland 上是异步的）
          .modal(true)                      // 请求模态吸附到父窗口
          .title(title)
          .accept_label(options.prompt)     // 确认按钮文案
          .multiple(options.multiple)
          .directory(options.directories)
          .send().await                     // ── 真正的 DBus 请求
          ├─ Err(PortalNotFound) ──▶ done_tx.send(Err(FILE_PICKER_PORTAL_MISSING))
          ├─ Err(其他)          ──▶ done_tx.send(Err(原始错误))
          └─ Ok(request)
               request.response()           // 等 portal 应答
               ├─ Ok(response)  ──▶ uris → Url::parse → to_file_path → Ok(Some(Vec<PathBuf>))
               ├─ Err(Response) ──▶ Ok(None)                // 用户取消
               └─ Err(其他)      ──▶ Err(e)
     }).detach()
```

两个值得注意的设计：

1. **外壳拿通道，细节进任务**：`prompt_for_paths` 同步返回 `done_rx`，所有 portal 交互都在 spawn 出去的任务里进行。调用方拿到的是纯数据结构（oneshot Receiver），不绑定任何执行器。
2. **任务 spawn 在前台执行器**，但 `.send().await` 期间并不阻塞主线程——ashpd 的 DBus IO 由 async-io 驱动，等待期间主循环照常处理输入与绘制。这与 u4-l1 讲的「前台任务按序运行」一致：对话框结果回来时，续体仍在主线程执行。

#### 4.2.3 源码精读

`FILE_PICKER_PORTAL_MISSING` 常量只有两行，但它是用户真实会看到的消息：

[../gpui_linux/src/linux/platform.rs:L47-L49](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L47-L49)

它受 `#[cfg(any(feature = "wayland", feature = "x11"))]` 门控——零 feature（纯 headless）构建里根本没有 portal 代码，也就没有这条错误。**触发条件**：仅当 `OpenFileRequest/SaveFileRequest::send().await` 返回 `ashpd::Error::PortalNotFound` 变体，即「portal 服务可见，但 FileChooser 接口没有可用后端」。注意它**不覆盖**「DBus 上连 portal 服务名都不存在」的情形——那会落到其他错误变体，最终以原始错误文本呈现（练习 3 会让你想清楚这两者的边界）。

现在精读主实现：

[../gpui_linux/src/linux/platform.rs:L401-L430](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L401-L430)

逐行拆解：

- L405 建 oneshot 通道；L407-L408 是零 feature 分支：`let _ = (done_tx.send(Ok(None)), options);` 用元组同时消费掉 sender 和 `options` 变量，一次性发 `Ok(None)`——纯 headless 构建里这个提示器永远表现为「用户取消」。注意：默认 feature 集（wayland+x11）下即使运行 `ZED_HEADLESS=1` 走 headless 后端，走的也是下面的 portal 分支（cfg 看的是**编译 feature**，不是运行期后端）——headless 后端只是拿不到窗口标识符（见 4.3），对话框照样会弹。
- L411 调 `self.inner.window_identifier()`（LinuxClient 方法，4.3 详述）拿到 future，注意这一步在 spawn **之前**、在调用线程上完成（Wayland 实现需要在这里借用客户端状态，所以不能推迟进任务）。
- L414-L415 任务 spawn 在前台执行器上。
- L416-L420 标题只由 `directories` 决定；`files` 字段在这里没有任何作用。
- L422-L430 组装请求。`.identifier(identifier.await)` 先等标识符 future 完成——`identifier` 是 `Option<WindowIdentifier>`，传 `None` 时 portal 把对话框当无父窗口。`.modal(true)` 请求对话框模态吸附在父窗口上。

继续看发送失败与应答处理：

[../gpui_linux/src/linux/platform.rs:L431-L459](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L431-L459)

- L434-L437 错误分拣：`ashpd::Error::PortalNotFound(_) => anyhow!(FILE_PICKER_PORTAL_MISSING)`，其余 `err => err.into()`——即只有这一个变体被翻译成人话。
- L443-L454 应答三态：`Ok(response)` 时把 `response.uris()`（`ashpd::Uri` 列表）经 `url::Url::parse` + `to_file_path()` 两级 `filter_map` 转成 `Vec<PathBuf>`——非 `file://` 的 URI 或无法转为本地路径的项被**静默丢弃**；`Err(ashpd::Error::Response(_))` 是「用户取消了对话框」的专用变体，映射为 `Ok(None)`；其余错误原样上抛。最后 `done_tx.send(result)`。

「另存为」的 `prompt_for_new_path` 是同构的姐妹篇：

[../gpui_linux/src/linux/platform.rs:L461-L522](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L461-L522)

差异点：用 `SaveFileRequest`；`.current_folder(directory)` 设初始目录（L487 的 `.expect("pathbuf should not be nul terminated")`——路径含内嵌 NUL 才会 panic，属启动期编程错误兜底）；`.current_name(suggested_name)` 设建议文件名；结果只取 `uris().first()`（保存对话框天然单选）。L497-L499 同样有 `PortalNotFound → FILE_PICKER_PORTAL_MISSING` 的分拣。

最后一个成员暴露了 portal 的能力边界：

[../gpui_linux/src/linux/platform.rs:L524-L527](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L524-L527)

`can_select_mixed_files_and_dirs` 在 Linux 上固定返回 `false`，注释写明原因：`org.freedesktop.portal.FileChooser` 接口只有「选文件」和「选目录」两种模式，没有「混合选」。这解释了 `PathPromptOptions::files` 与 `directories` 在 Linux 上不能同时为真的底层原因，也是「契约字段集 ⊃ 各平台能力」的又一例证（调用方需按 `can_select_mixed_files_and_dirs()` 降级 UI）。

真实消费者怎么用这三态结果？看 Zed 工作区的 `prompt_for_open_path`：

[../workspace/src/workspace.rs:L3100-L3139](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../workspace/src/workspace.rs#L3100-L3139)

L3117 拿到通道后 spawn 等待；`Ok(result)` 直接透传；`Err(err)`（包含 `FILE_PICKER_PORTAL_MISSING`）则走 L3128-L3136：向用户展示 `PortalError`，**并立即回退到 Zed 应用内的文件查找器**（`on_prompt_for_open_path` 注入的 prompt 回调）。也就是说，portal 缺失时 Zed 的用户体验不是「不能打开文件」，而是「换一个内置选择器」。

测试侧则完全不碰真实 portal。gpui 的测试上下文提供 `simulate_path_prompt_response`：

[../gpui/src/app/test_context.rs:L1293-L1319](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui/src/app/test_context.rs#L1293-L1319)

`TestAppContext` 里 `cx.prompt_for_paths` 不会弹任何对话框（测试平台的实现记录调用），`did_prompt_for_paths()` 断言发生过提示，`simulate_path_prompt_response` 注入假响应——`Some(selected)` 模拟确认、闭包返回 `None` 模拟取消。这段测试同时是三态语义的活文档（u8-l4 会展开测试平台）。

#### 4.2.4 代码实践

**实践目标**：亲手触发成功、取消、失败三条路径，并对照源码解释每条路径的产出。

**操作步骤**：

1. 仿照 u1-l2 建一个依赖 `gpui` 与 `gpui_platform` 的独立 crate（不要修改 zed 源码）。
2. 参照 [../gpui/examples/window.rs:L311-L337](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui/examples/window.rs#L311-L337) 的 `run_example` 骨架，借用其中的 `button` 辅助组件（[L14-L27](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui/examples/window.rs#L14-L27)），在窗口里加一个按钮（**示例代码**）：

   ```rust
   use gpui::PathPromptOptions;

   child(button("Open File (portal)", |_, _, cx| {
       let done = cx.prompt_for_paths(PathPromptOptions {
           files: true,
           directories: false,
           multiple: false,
           prompt: Some("打开".into()),
       });
       cx.spawn(async move |_| {
           match done.await {
               Ok(Ok(Some(paths))) => println!("已选择: {paths:?}"),
               Ok(Ok(None)) => println!("用户取消"),
               Ok(Err(err)) => println!("打开失败: {err}"),
               Err(_closed) => println!("通道被关闭"),
           }
       })
       .detach();
   }))
   ```
3. **成功路径**：点击按钮，在弹出的对话框里选中一个文件并确认，观察终端输出 `已选择: [...]`；再选一次目录（把 `directories` 改为 `true` 重新编译），确认对话框标题变成 "Open Folder"。
4. **取消路径**：点击按钮后直接关闭对话框，观察输出 `用户取消`。
5. **失败路径（无 portal 会话模拟）**：用一条私有的、没有 portal 服务的会话总线运行程序：

   ```bash
   dbus-run-session -- ./target/debug/my-portal-demo
   ```

   `dbus-run-session` 会启动一个全新的 `dbus-daemon` 会话总线（其中没有任何桌面服务）再拉起你的程序。点击按钮观察终端输出。
6. 对照 `platform.rs` L434-L437 与 L443-L454，把三条路径各自的输出记入表格。

**需要观察的现象**：成功时拿到 `file://` URI 转换而来的 `PathBuf`；取消时得到 `Ok(None)` 而非报错；`dbus-run-session` 下点击按钮应得到 `打开失败: …` 的错误文本。

**预期结果**：成功/取消两条路径与 4.2.2 流程图完全吻合。失败路径的错误文本值得细看：若错误以 `Couldn't open file picker due to missing xdg-desktop-portal implementation.` 开头，说明命中了 `PortalNotFound → FILE_PICKER_PORTAL_MISSING` 分拣；若显示的是 zbus/DBus 原始错误（私有总线上根本没有 `org.freedesktop.portal.Desktop` 这个名字的属主），说明落进了 `err => err.into()` 的兜底分支——这两者的差别恰好验证了 4.2.3 对触发条件的说明。具体命中哪条取决于 ashpd 对「服务名无属主」的归类，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`PathPromptOptions` 有四个字段，Linux 实现真正用到了哪几个？`files` 字段去哪了？

**答案**：用到 `directories`（决定标题 "Open Folder"/"Open File" 与请求的 `.directory(bool)`）、`multiple`（`.multiple(bool)`）、`prompt`（`.accept_label`）。`files` 在 Linux 实现中完全未使用——portal 的 FileChooser 接口只有「文件/目录」一个布尔开关，没有单独的「允许文件」标志；`files` 字段是为其他平台（如 macOS `NSOpenPanel` 可以同时允许文件与目录）保留的契约字段。

**练习 2**：为什么「用户取消对话框」在代码里要写成 `Err(ashpd::Error::Response(_)) => Ok(None)` 而不是返回 `Err`？

**答案**：因为契约的三态语义把「取消」定义为正常结果（`Ok(None)`），只有「对话框打开失败」才是 `Err`。ashpd 把用户取消表达为 `Error::Response`（portal 协议的 response 非 0），Linux 实现负责把这一平台细节翻译回契约语义。如果这里返回 `Err`，上游（如 workspace.rs 的 `prompt_for_open_path`）会误把取消当成故障弹错误提示。

**练习 3**：`FILE_PICKER_PORTAL_MISSING` 会在「用户根本没有安装任何 xdg-desktop-portal」时出现吗？

**答案**：不一定。该常量只匹配 `ashpd::Error::PortalNotFound` 一个变体——语义是「portal 前端服务可达，但请求的接口没有后端实现」。若会话总线上连 `org.freedesktop.portal.Desktop` 名字都没有属主（服务未安装/未启动），`send()` 失败产生的是 DBus 层错误（如 ServiceUnknown 之类），会走 `err => err.into()` 分支，用户看到的是原始错误文本。两种情形都意味着「对话框打不开」，但对用户排障的提示价值不同——前者提示「装个后端」（如 `xdg-desktop-portal-gtk`），后者提示「portal 服务本身缺失」。

**练习 4**：零 feature 构建下 `prompt_for_paths` 的行为是什么？默认 feature 下运行 `ZED_HEADLESS=1` 的程序行为又是什么？

**答案**：零 feature（`--no-default-features` 且不开 wayland/x11）时，L407-L408 生效：立即发送 `Ok(None)`，提示器永远表现为「用户取消」。默认 feature 下即使 `ZED_HEADLESS=1`，cfg 条件 `any(feature = "wayland", feature = "x11")` 在编译期为真，仍走 portal 分支——headless 后端的 `window_identifier` 返回 `None`（4.3），对话框以无父窗口形式弹出（前提是该会话里有 portal 可用）。

### 4.3 LinuxClient::window_identifier：窗口标识符如何传递

#### 4.3.1 概念说明

portal 对话框是独立进程渲染的窗口，它需要知道「谁是调用它的父窗口」，才能：把对话框置于父窗口之上、`modal(true)` 时阻止用户操作父窗口、在任务栏里正确归属。XDG 规范把这表达为请求里的 `parent_window` 字符串标识符，**不同窗口系统的格式与获取方式完全不同**：

| 窗口系统 | 标识符形态 | 获取方式 |
| --- | --- | --- |
| X11 | `x11:<XID>`（X 窗口号，全局整数） | 本地即可算出，同步 |
| Wayland | `wayland:…`（协议导出的 surface 信息） | 需与合成器交互，异步 |
| 无窗口/headless | 无 | 只能传 `None` |

GPUI 把「拿到标识符」定义为 LinuxClient 契约的方法（headless 用默认实现），外壳在发 portal 请求前调用它。这就是 4.2 流程图中 `identifier.await` 的来源。

#### 4.3.2 核心流程

```
LinuxPlatform::prompt_for_paths / prompt_for_new_path
  └─ self.inner.window_identifier()          ← 契约方法，按后端分派
       ├─ HeadlessClient: 契约默认实现 → ready(None)
       ├─ X11Client:       keyboard_focused_window → x_window as u64
       │                    → WindowIdentifier::from_xid(xid)   （同步，包成 ready future）
       └─ WaylandClient:   先同步借用状态取 keyboard_focused_window 的 wl_surface
                            → WindowIdentifier::from_wayland(&surface).await  （真异步）
  …
  OpenFileRequest::identifier(identifier.await)   // None 也能接受：对话框无父
```

注意共同点：两个桌面后端都用**键盘焦点窗口**（`keyboard_focused_window`）作为对话框的父窗口——用户正在交互的窗口，语义上正是「从这个窗口发起的请求」。

#### 4.3.3 源码精读

契约与默认实现在 LinuxClient trait 里：

[../gpui_linux/src/linux/platform.rs:L98-L103](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L98-L103)

方法受 `#[cfg(any(feature = "wayland", feature = "x11"))]` 门控，返回 `impl Future<Output = Option<ashpd::WindowIdentifier>> + Send + 'static`——**返回 future 而不是值**，是因为 Wayland 实现是异步的；`Send + 'static` 让它能被移动进 4.2 的 spawn 任务里 `await`。默认实现 `std::future::ready(None)` 服务所有不覆写的后端：headless 客户端（u5-l2 讲过 `HeadlessClient` 是 LinuxClient 的最小实现）没有真实窗口，直接返回 `None`。

X11 的覆写在客户端 trait 实现的尾部：

[../gpui_linux/src/linux/x11/client.rs:L1862-L1870](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1862-L1870)

三步链条：`keyboard_focused_window`（窗口 id）→ `windows` 表里查到窗口对象 → 取其 `x_window as u64` → `WindowIdentifier::from_xid(x_window)`。X11 的窗口号是全局已知的整数，所以整段计算是同步的，最后包一层 `std::future::ready` 以满足契约的返回类型。没有焦点窗口时同样落到 `ready(None)`。

Wayland 的覆写：

[../gpui_linux/src/linux/wayland/client.rs:L1283-L1295](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1283-L1295)

两个细节值得注意：

1. **借用与异步的分离**：L1292 的 `self.0.borrow()` 是 `RefCell` 借用，不能跨 `await` 持有（u5-l3 讲过这种借用舞蹈）。所以先在借用期间把焦点窗口的 `wl_surface` **克隆出来**（L1294 的 `active_window.map(|aw| aw.surface())`），再交给内部 async 块。内部 `inner` 函数对 `None` 直接返回 `None`。
2. **真异步**：`ashpd::WindowIdentifier::from_wayland(&surface).await`——Wayland 没有全局窗口号，标识符需要通过 Wayland 协议交互从合成器处导出 surface 信息，这正是契约必须返回 future 的原因。

支撑这段代码的 feature 接线在 gpui_linux 的 Cargo.toml：

[../gpui_linux/Cargo.toml:L17-L46](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/Cargo.toml#L17-L46)

`wayland` feature 追加 `ashpd/wayland`（L20，启用 `from_wayland`），`x11` feature 启用 `ashpd`（L36）；ashpd 本体是 optional 依赖（[L83](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/Cargo.toml#L83)）。两扇 feature 门与契约方法上的 cfg 条件一一对应——这是 u1-l3「feature 透传改写运行期行为」的又一实例。

#### 4.3.4 代码实践

**实践目标**：在真实会话中确认「父窗口标识符」对对话框行为的影响。

**操作步骤**：

1. 复用 4.2.4 的示例程序，在 Wayland 或 X11 会话中运行，点击按钮弹出对话框，观察对话框是否出现在你的窗口中央上方、任务栏归属是否正确。
2. 打开两个 GPUI 窗口，让**第二个**窗口持有键盘焦点，再点按钮——对照 L1293/L1865 的 `keyboard_focused_window`，确认对话框吸附的是**焦点窗口**而不是「第一个窗口」。
3. 阅读式验证：在笔记里抄下三种后端各自的 `window_identifier` 返回形态（from_xid / from_wayland / ready(None)），并为每种标注「同步还是异步、为什么」。

**需要观察的现象**：对话框作为你应用的子窗口出现（窗口管理器会以模态/置顶方式呈现）；焦点窗口不同，对话框的父也随之不同。

**预期结果**：与 `keyboard_focused_window` 语义一致。若在无焦点的状态下触发（例如通过全局快捷键），`window_identifier` 可能返回 `None`，对话框退化为无父窗口——具体表现依赖桌面环境，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 LinuxClient::window_identifier 返回 `impl Future` 而不是直接返回 `Option<WindowIdentifier>`？

**答案**：因为 Wayland 后端获取标识符需要与合成器做协议交互（`from_wayland` 是 async 的）。契约要同时容纳 X11（同步即可算出）与 Wayland（必须异步），只能取两者的公共上限——返回 future；X11 实现用 `std::future::ready` 把同步结果包装成已完成的 future。

**练习 2**：Wayland 实现里为什么要先 `self.0.borrow()` 把 surface 克隆出来，而不是直接在 async 块里借用客户端状态？

**答案**：客户端状态存放在 `Rc<RefCell<…>>` 里，`RefCell` 的借用不能跨 `await` 持有——若跨持，事件循环后续再借用同一状态（例如处理输入事件）会直接 panic。所以先在同步段完成借用并克隆 `wl_surface`（Wayland 对象句柄是克隆友好的代理），再进入 async 块。这与 u5-l3 讲的 `take_xim`/`restore_xim` 是同一类「借用舞蹈」。

**练习 3**：headless 后端（默认 feature 下 `ZED_HEADLESS=1` 运行）调用 `prompt_for_paths` 时，对话框还有父窗口吗？

**答案**：没有。headless 客户端不覆写 `window_identifier`，走契约默认实现 `ready(None)`，portal 请求携带 `parent_window = None`，对话框以无父窗口形式弹出（前提是会话中有 portal）。但程序仍会等用户在对话框里操作完毕——headless 挡住的是窗口系统交互，不是 portal 交互。

## 5. 综合实践

把本讲三个模块串起来，做一份「Linux portal 能力探测报告」：

1. **准备**：复用 4.2.4 的示例 crate，把界面扩展成三个按钮：Open File（`files: true`）、Open Folder（`directories: true`）、Save File（调用 `cx.prompt_for_new_path(Path::new("."), Some("report.txt"))`），三者都把结果按三态打印。
2. **正常会话采集**：在你的桌面会话里逐一触发并记录：对话框风格（GTK/Qt？——这暴露了你装的是哪个 portal 后端）、成功/取消两条路径的输出、对话框相对焦点窗口的位置。
3. **无 portal 会话采集**：用 `dbus-run-session -- ./my-portal-demo` 重跑（可提前 `dbus-run-session -- bash -c 'sleep infinity'` 观察总线差异），记录三个按钮各自的错误文本，并对照 `platform.rs` L434-L437/L496-L501 判断每条错误走的是 `FILE_PICKER_PORTAL_MISSING` 分拣还是原始错误兜底。
4. **设置链路采集**：程序运行期间切换系统深浅色主题，确认窗口联动；结合 `busctl --user list | grep -i portal` 的输出，说明这条事件链依赖哪个总线服务。
5. **产出**：一份 Markdown 报告，包含三种对话框的结果表、无 portal 环境的错误归类表、以及一张把 `prompt_for_paths` → `window_identifier` → `OpenFileRequest.send()` → `done_tx` 全程串起来的调用链图（每个节点标注源码文件与行号）。

若没有 Linux 桌面环境，第 2、4 步可以替换为「源码阅读型实践」：沿 4.2.2 的流程图为每个箭头找到对应的源码行号，并写出每步的输入输出类型。

## 6. 本讲小结

- xdg-desktop-portal 是会话总线上的标准化服务（配桌面环境后端），GPUI 经 ashpd 0.13（async-io 驱动）使用它的 FileChooser 与 Settings 两组接口；`xdg_desktop_portal.rs` 模块实际承载的是 Settings 事件源——后台任务读初始值并订阅变更，经 calloop channel 桥接进主循环，失败即静默降级。
- `prompt_for_paths` 的链路是「外壳建 oneshot 通道 → 取窗口标识符 → 前台任务发 portal 请求 → 三态结果回传」；三态语义里**用户取消是 `Ok(None)` 而非错误**，只有对话框打开失败才是 `Err`。
- `FILE_PICKER_PORTAL_MISSING` 只在 `ashpd::Error::PortalNotFound` 分支出现；上游 Zed 工作区收到 `Err` 后展示错误并回退到应用内文件查找器，而不是让功能不可用。
- `PathPromptOptions::files` 在 Linux 实现中未被使用——portal 的 FileChooser 只有「文件/目录」一个开关，`can_select_mixed_files_and_dirs()` 固定 `false` 正是这一能力边界的对外声明。
- `LinuxClient::window_identifier` 三种姿态：X11 用 `from_xid` 同步算、Wayland 用 `from_wayland` 异步取（先把 surface 从 `RefCell` 借用中克隆出来再 await）、headless 走默认 `ready(None)`；标识符让 portal 对话框正确吸附到键盘焦点窗口。
- portal 是「选择性使用」的：文件对话框与设置订阅走 portal，而 `open_uri`/`reveal_path` 在 Wayland 上走 xdg-activation 协议、兜底 `xdg-open`（[platform.rs:L533-L548](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L533-L548)）——按能力选机制，不为 portal 而 portal。

## 7. 下一步学习建议

- 第 6 单元（u6-l3）会把「系统通知」也搬到 portal/DBus 语境下对比三平台实现，届时可以看到 Linux 的系统通知如何经 notify-rust/portal 发出——与本讲的 Settings/FileChooser 构成 portal 三件套。
- 想继续深挖 Linux 后端，可回头重读 u5-l4 的 Wayland 客户端，把本讲的 `window_identifier` 与 `open_uri` 里的 xdg-activation token、serial 跟踪放在一张图里理解。
- 对测试侧感兴趣的话，u8-l4 会展开 `TestAppContext`/`simulate_path_prompt_response` 背后的测试平台（`platform/test` 目录），你会看到 `prompt_for_paths` 在测试里如何被完全替换成可注入的桩。
- 延伸阅读：freedesktop 的 [FileChooser portal 规范](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.FileChooser.html)与 [Settings portal 规范](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Settings.html)，对照 ashpd 的请求构造器逐字段印证。
