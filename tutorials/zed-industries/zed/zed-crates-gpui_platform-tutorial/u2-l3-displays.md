# 显示器管理：PlatformDisplay 与多屏几何

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `PlatformDisplay` 契约的五个方法各自回答什么问题，尤其是 `id()`（运行期句柄）与 `uuid()`（可持久化身份）为什么必须是两个东西。
- 画出一次 `cx.displays()` 调用从应用层到平台实现的两层转发链路（Linux 上是三层）。
- 说明 `visible_bounds()` 为什么要排除任务栏 / Dock / 浏览器 UI 区域，以及不带覆盖实现时它默认等于 `bounds()`。
- 对照 macOS、Wayland、X11、Windows 四套实现，说出各自的显示器数据来自哪个系统 API，以及它们在「主显示器」语义上的差异（Wayland 干脆没有这个概念）。

本讲是 u2-l1「Platform trait 全景导览」中「窗口与显示器」分组的下钻，只聚焦显示器这一块。

## 2. 前置知识

### 逻辑像素与物理像素

GPUI 的显示器几何用 `Bounds<Pixels>`（逻辑像素）表达，而不是 `Bounds<DevicePixels>`（物理像素）。两者之间的关系由缩放因子（scale factor）决定：

\[ \text{逻辑尺寸} = \frac{\text{物理尺寸}}{\text{scale}} \]

例如一台 2560×1440 物理分辨率、scale = 2 的显示器，逻辑尺寸就是 1280×720。高 DPI（HiDPI）屏幕普及后，UI 布局用逻辑像素描述才能在不同密度的屏幕上获得一致的视觉大小。本讲会看到 Wayland 和 Windows 的实现都在边界上做了这次除法。

### Bounds 结构

`Bounds<T>` 由 `origin: Point<T>`（左上角坐标）和 `size: Size<T>`（宽高）组成，并附带 `top_right()`、`bottom_left()`、`center()` 等便捷方法。显示器坐标系约定**左上角为原点、y 轴向下**。

### 任务栏、Dock 与工作区

操作系统通常在屏幕边缘保留一块区域放任务栏（Windows）、Dock 和菜单栏（macOS）或面板（Linux 桌面环境）。**工作区（work area）** = 整块屏幕减去这些保留区域，是「窗口放进去不会被挡住」的范围。Windows API 里叫 `rcWork`，macOS 里叫 `NSScreen.visibleFrame`。GPUI 把它抽象为 `visible_bounds()`。

### 两种坐标系的原点位置

AppKit（macOS 的 UI 框架）传统上以**左下角为原点、y 轴向上**；GPUI 和大多数现代 UI 框架以**左上角为原点、y 轴向下**。macOS 实现里因此有一段坐标翻转代码，这是本讲的一个看点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/gpui/src/platform.rs` | 契约层：`PlatformDisplay` trait、`DisplayId`、`Platform::displays` / `primary_display` 声明 |
| `crates/gpui/src/app.rs` | 应用层封装：`App::displays` / `primary_display` / `find_display` 转发 |
| `crates/gpui/src/geometry.rs` | 消费方：`Bounds::centered` / `maximized` 用显示器几何算窗口默认位置 |
| `crates/gpui/examples/window_positioning.rs` | 官方示例：枚举显示器并在每台屏幕的九个方位各开一个窗口 |
| `crates/gpui_macos/src/display.rs` | macOS 实现：CoreGraphics + NSScreen |
| `crates/gpui_linux/src/linux/platform.rs` | Linux 外壳：把显示器调用转发给内部 `LinuxClient` 后端 |
| `crates/gpui_linux/src/linux/wayland/display.rs` 与 `wayland/client.rs` | Wayland 实现：由 `wl_output` 协议事件累积出显示器列表 |
| `crates/gpui_linux/src/linux/x11/display.rs` 与 `x11/client.rs` | X11 实现：从 XCB 连接的 screen 列表读取 |
| `crates/gpui_windows/src/display.rs` | Windows 实现：`EnumDisplayMonitors` + `GetMonitorInfoW` + DPI |
| `crates/gpui_linux/src/linux/headless/window.rs` | headless 后端的假显示器（1920×1080） |

另外两处补充对照：`crates/gpui_linux/src/linux/headless/client.rs`（无头后端怎么填这两个接口）和 `crates/gpui_web/src/display.rs`（浏览器里「显示器」是什么）。消费方真实用例见 `crates/zed/src/zed.rs`。

## 4. 核心概念与源码讲解

### 4.1 契约层：PlatformDisplay trait 与两种显示器标识

#### 4.1.1 概念说明

一台显示器在 GPUI 眼里需要回答五个问题：

| 方法 | 回答的问题 | 有无默认实现 |
| --- | --- | --- |
| `id() -> DisplayId` | 「这次运行中你是哪台？」 | 必须实现 |
| `uuid() -> Result<Uuid>` | 「重启之后你还是哪台？」 | 必须实现 |
| `bounds() -> Bounds<Pixels>` | 「整块屏幕多大、在哪？」 | 必须实现 |
| `visible_bounds() -> Bounds<Pixels>` | 「不被任务栏挡住的可放窗口区域在哪？」 | 默认返回 `bounds()` |
| `default_bounds() -> Bounds<Pixels>` | 「在这台显示器上新开一个窗口，默认放哪？」 | 默认取 `DEFAULT_WINDOW_SIZE` 与屏幕尺寸取小后居中 |

`id()` 和 `uuid()` 的分工是本模块的核心：

- **`DisplayId` 是一次进程运行内的不透明句柄**，本质是 `u64` 的包装。它用来在 `WindowOptions::display_id` 这类接口里快速指认「就开在这台显示器上」，可比较、可哈希、可 `Copy`。
- **`uuid` 是跨重启稳定的身份**，用来持久化。典型场景：Zed 记住「这个工作区的窗口上次在左边的副屏」，写进 workspace 状态的是 uuid；下次启动再用 uuid 找回那台显示器。为什么不能直接存 `DisplayId`？因为它的数值来源各平台不同（Wayland 是协议对象编号、X11 是屏幕序号、macOS 是 `CGDirectDisplayID`），这些值在重启后都可能变化或复用，不适合落盘。

#### 4.1.2 核心流程

以「窗口想记住自己所在的显示器」为例：

```text
运行期（本次会话）:
    WindowOptions { display_id: Some(id) } ──► 平台层按 id 找到显示器 ──► 窗口开在那台屏幕上

跨重启（持久化）:
    启动时: platform.displays() ──► 每台 display.uuid() ──► 和存档里的 uuid 比对
    找到 ──► 用这台显示器恢复窗口位置
    没找到（显示器拔了）──► 回退 primary_display() 或 (0,0) 兜底
```

`default_bounds()` 的默认算法（伪代码）：

```text
window_size = min(DEFAULT_WINDOW_SIZE, bounds.size)   # 窗口不能比屏幕大
origin      = bounds.center() - window_size / 2       # 居中
return Bounds { origin, size: window_size }
```

#### 4.1.3 源码精读

先看契约本体。`PlatformDisplay` 定义在 gpui 主 crate 的 platform.rs 中：

- [crates/gpui/src/platform.rs:L343-L372](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L343-L372) —— `PlatformDisplay` trait 全文。注意 `visible_bounds()`（L358-L360）与 `default_bounds()`（L363-L371）带默认实现，前三个方法（`id`/`uuid`/`bounds`）必须由平台实现。doc 注释明确写了 uuid 的用途：「Returns a stable identifier for this display that can be persisted and used across system restarts」。
- [crates/gpui/src/platform.rs:L460-L487](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L460-L487) —— `DisplayId` 的定义：`pub struct DisplayId(pub(crate) u64)`，字段是私有的，外界只能通过 `new`/`From<u64>` 构造、`From<DisplayId> for u64` 取回，`Debug` 输出形如 `DisplayId(3)`。「不透明」是刻意设计：调用方不该对数值含义做任何假设。

再看一个真实消费者，体会 uuid 的价值：

- [crates/zed/src/zed.rs:L361-L366](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/zed/src/zed.rs#L361-L366) —— Zed 编辑器的 `build_window_options`：拿持久化保存的 `display_uuid`，遍历 `cx.displays()` 用 `display.uuid().ok() == Some(uuid)` 找回上次那台显示器。这就是「DisplayId 不能落盘、uuid 可以」的活例子。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认 `DisplayId` 的数值语义随平台不同，从而理解为什么不能拿它做持久化键。
2. **操作步骤**：
   - 打开 [crates/gpui_linux/src/linux/wayland/display.rs:L26-L29](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/wayland/display.rs#L26-L29)，看 Wayland 的 `id()` 用 `self.id.protocol_id()`——这是 Wayland 连接里 `wl_output` 协议对象的编号，同一次连接内唯一，重连后会重新分配。
   - 打开 [crates/gpui_linux/src/linux/x11/display.rs:L39-L42](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/x11/display.rs#L39-L42)，看 X11 的 `id()` 只是屏幕在列表里的**下标**。
   - 打开 [crates/gpui_windows/src/display.rs:L77-L79](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/display.rs#L77-L79)，看 Windows 的 `DisplayId` 直接装的是 `HMONITOR` 句柄的指针值。
3. **需要观察的现象**：三个平台的 `u64` 来源完全不同——对象编号 / 数组下标 / 系统句柄。
4. **预期结果**：你会得出结论：`DisplayId` 只在「当前这次平台会话」内有意义；任何要写进配置文件的场景都必须走 `uuid()`。

#### 4.1.5 小练习与答案

**练习 1**：`uuid()` 为什么返回 `Result<Uuid>` 而不是 `Uuid`？哪些平台真的会失败？

**答案**：因为部分平台拿不到稳定身份。macOS 的 `CGDisplayCreateUUIDFromDisplayID` 可能返回空指针（实现里用 `anyhow::ensure!` 报错，见 [crates/gpui_macos/src/display.rs:L79-L84](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/display.rs#L79-L84)）；Wayland 的显示器可能没上报 `name`，此时用 `context(...)` 报错（[crates/gpui_linux/src/linux/wayland/display.rs:L31-L37](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/wayland/display.rs#L31-L37)）。Windows、X11、headless、Web 的实现则直接 `Ok(...)`，不会失败——契约按「最谨慎的实现」对齐。

**练习 2**：`default_bounds()` 为什么要把窗口尺寸和屏幕尺寸做 `min`？

**答案**：见 [crates/gpui/src/platform.rs:L363-L371](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L363-L371) 中的 `DEFAULT_WINDOW_SIZE.min(&bounds.size)`。如果窗口默认尺寸比屏幕还大（小窗口的弹出层开在超小屏幕上），不减一下窗口会超出屏幕；先取小再按它居中，保证窗口完整落在屏幕内。

### 4.2 枚举入口：Platform::displays / primary_display 与应用层封装

#### 4.2.1 概念说明

`Platform` trait 用两个方法把显示器交给外界：

- `displays() -> Vec<Rc<dyn PlatformDisplay>>`：当前所有活动显示器。**没有默认实现**，任何平台实现都必须提供。
- `primary_display() -> Option<Rc<dyn PlatformDisplay>>`：主显示器，返回 `None` 表示「本平台没有这个概念」。

应用代码不直接摸 `Platform`，而是经由 `App` 上的同名方法（`cx.displays()`），中间还有一层按 id 查找的便捷封装 `find_display`。最上层的消费者是 `Bounds::centered` / `Bounds::maximized` 这类「帮我算窗口默认位置」的工具函数，它们组成一条回退链。

为什么 `primary_display` 是 `Option`？因为 Wayland 协议里根本没有「主显示器」的概念—— compositor 不告诉客户端哪块屏是主屏，客户端只能自己猜。契约用 `Option` 承认了这种平台差异。

#### 4.2.2 核心流程

一次 `Bounds::centered(Some(id), size, cx)` 调用的决策链：

```text
find_display(id)                 # 按 DisplayId 在 displays() 里线性查找
    │ 找到 ──► 用它的 bounds().center() 居中
    ▼ 没找到 / id 是 None
primary_display()                # 问平台要主显示器
    │ 有 ──► 用主显示器居中
    ▼ 没有（例如 Wayland）
兜底 Bounds { origin: (0,0), size }   # 不理想但不崩溃
```

Linux 上的调用链比别的平台多一层（呼应 u1-l4 的两层分发）：

```text
cx.displays()                     # App (gpui/src/app.rs)
  └─ platform.displays()          # LinuxPlatform 外壳 (gpui_linux/src/linux/platform.rs)
       └─ self.inner.displays()   # LinuxClient 后端：WaylandClient / X11Client / HeadlessClient
```

#### 4.2.3 源码精读

- [crates/gpui/src/platform.rs:L139-L140](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L139-L140) —— `Platform` trait 中两个显示器方法的声明，位于 trait 顶部「窗口与显示器」分组，紧跟生命周期方法之后，且都无默认实现。
- [crates/gpui/src/app.rs:L1303-L1311](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1303-L1311) —— `App::displays()` 与 `App::primary_display()`：单纯转发给 `self.platform`。这就是 u2-l1 说过的「运行期逐方法转发」在显示器分组的具体形态。
- [crates/gpui/src/app.rs:L1325-L1331](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1325-L1331) —— `App::find_display(id)`：每次调用都重新枚举一遍 `displays()` 再按 `display.id() == id` 线性查找。注意这里没有缓存。
- [crates/gpui/src/geometry.rs:L740-L765](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/geometry.rs#L740-L765) —— `Bounds::centered` 与 `Bounds::maximized`：完整的回退链实现。`centered` 找不到任何显示器时退到 `(0,0)`；`maximized` 退到 1024×768 的兜底矩形。
- [crates/gpui/examples/window_positioning.rs:L73-L95](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/window_positioning.rs#L73-L95) —— 官方示例的入口：`application().run` 回调里 `for screen in cx.displays()`，对每台显示器用 `screen.id()` 和 `screen.bounds()` 的各个角点/中点定位窗口；`build_window_options`（L53-L71）把 `display_id: Some(display_id)` 塞进 `WindowOptions`，保证窗口真的开在那台屏幕上。这是本讲实践任务的骨架。

#### 4.2.4 代码实践（运行官方示例）

1. **实践目标**：亲眼看到「枚举显示器 → 在每台屏幕上定位窗口」的完整链路跑起来。
2. **操作步骤**：
   1. 在 zed 仓库根目录执行：

      ```bash
      cargo run -p gpui --example window_positioning
      ```

   2. 观察每台显示器上出现的九个彩色小窗口（四角 + 四边中点 + 正中）。
   3. 回到源码，把 L84 的 `origin: point(margin_offset, margin_offset)` 与 L97-L101 的 `screen.bounds().top_right() - ...` 对照窗口实际出现的位置。
3. **需要观察的现象**：每个小窗口都落在对应显示器的对应方位上；窗口里的文字显示 `Top Left DisplayId(N)` 之类的字样，`N` 就是那台显示器的 `DisplayId` 数值。
4. **预期结果**：单屏机器上九个窗口都在同一块屏；双屏机器上两组窗口分别落在两块屏。若在无显示环境（如 SSH 会话）运行，Linux 会走 headless 后端（u1-l4 讲过的探测规则），窗口逻辑存在但不上屏——具体是否报错**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`App::find_display` 每次都重新枚举显示器并线性查找。如果一次渲染里要查 100 次，有什么影响？

**答案**：每次 `find_display` 都会调用 `platform.displays()` 走一遍平台枚举（macOS 上是 `CGGetActiveDisplayList` 系统调用、Windows 上是 `EnumDisplayMonitors`），复杂度 \( O(n) \) 且有系统调用开销。100 次就是 100 次完整枚举。好消息是调用方通常在开窗时查一次；如果真有热路径需求，应在调用方自行缓存 `Rc<dyn PlatformDisplay>`。

**练习 2**：一个平台实现可以只实现 `displays()` 而让 `primary_display()` 返回 `None` 吗？调用方会发生什么？

**答案**：可以，Wayland 就是这么做的（下一节看源码）。调用方走 `Bounds::centered` 的回退链：找不到 id 就退到 `primary_display()`，再是 `None` 就退到 `(0,0)` 起点的兜底矩形。UI 仍能用，只是默认窗口位置不再「在主屏居中」。

### 4.3 四套实现对照：显示器数据从哪里来

#### 4.3.1 概念说明

同一个契约，四套实现的数据来源完全不同，这正是平台层的意义所在：

| 平台 | 显示器列表来源 | 主显示器来源 | uuid 来源 | 缩放来源 |
| --- | --- | --- | --- | --- |
| macOS | `CGGetActiveDisplayList`（CoreGraphics） | `NSScreen::screens` 的第 0 个 | `CGDisplayCreateUUIDFromDisplayID`（系统级稳定身份） | `NSWindow`/AppKit 自行处理，显示器层不涉及 |
| Wayland | `wl_output` 协议事件累积（客户端被动接收） | **无，返回 `None`** | 由 output `name` 派生的 v5 UUID | `wl_output` 的 `Scale` 事件 |
| X11 | XCB 连接 setup 里的 screen 列表 | `x_root_index` 那个 screen | **全零占位 UUID** | 客户端配置的统一 scale |
| Windows | `EnumDisplayMonitors` 回调收集 `HMONITOR` | `MonitorFromPoint((0,0))` | 由 `szDevice` 设备名派生的 v5 UUID | `GetDpiForMonitor(MDT_EFFECTIVE_DPI)` |

「被动接收」与「主动查询」是理解 Wayland 与其他平台差异的钥匙：X11/Windows/macOS 都可以随时问系统「现在有哪些显示器」，而 Wayland 客户端只能在启动时绑定 compositor 广播的全局对象，然后等 `wl_output` 的几何/模式/缩放事件一点点到齐，攒够了一条才算一台完整显示器。

#### 4.3.2 核心流程

Wayland 侧「攒显示器」的状态机：

```text
compositor 广播全局对象 wl_output
    ──► 客户端 bind 到 WlOutput，登记进 in_progress_outputs
    ──► 事件陆续到达:
            Geometry { x, y }   ──► position = Some(...)
            Mode { width, height } ──► size = Some(...)
            Scale { factor }    ──► scale = Some(...)     # 缺省按 1
            Name { name }       ──► name = Some(...)      # uuid 的原料
    ──► Done 事件（一台显示器的信息发完了）
            position 与 size 都齐了？
                是 ──► 组装 Output 存入 state.outputs
                否 ──► 丢弃这条 in_progress 记录
```

之后每次 `displays()` 就是把 `state.outputs` 快照出来，并把物理像素的 `Bounds<DevicePixels>` 按 scale 换算成 `Bounds<Pixels>`。

macOS 侧 `visible_bounds()` 的坐标翻转：AppKit 的 `visibleFrame` 以**左下**为原点，GPUI 要**左上**为原点，因此：

\[ y_{\text{top-left}} = H_{\text{screen}} - y_{\text{appkit}} - H_{\text{visible}} + y_{\text{screen,origin}} \]

#### 4.3.3 源码精读

**macOS**

- [crates/gpui_macos/src/display.rs:L48-L66](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/display.rs#L48-L66) —— `MacDisplay::all()`：调用 `CGGetActiveDisplayList`，注释里假设系统不超过 32 台显示器；返回非 0 直接 `panic!`。
- [crates/gpui_macos/src/display.rs:L28-L45](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/display.rs#L28-L45) —— `MacDisplay::primary()`：**不走** `all()`，而是取 `NSScreen::screens` 的第 0 个再读出它的 `NSScreenNumber`。注释解释了原因：机器睡眠等状态下 `CGGetActiveDisplayList` 不保证返回完整列表，并附了 Chromium 同样处理的源码链接。「主显示器 = 有菜单栏的那块 = AppKit screen 列表第 0 个」。
- [crates/gpui_macos/src/display.rs:L108-L119](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/display.rs#L108-L119) —— `bounds()`：调 `CGDisplayBounds` 拿全局坐标，但**把 origin 丢弃、置为默认值 (0,0)**，只保留尺寸。也就是说 macOS 的 `bounds()` 表达的是「这台屏自己的局部几何」，多屏定位必须配合 `WindowOptions::display_id`（正如 window_positioning 示例的做法）。这是阅读 macOS 代码时最容易误判的一个细节。
- [crates/gpui_macos/src/display.rs:L121-L148](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/display.rs#L121-L148) —— `visible_bounds()`：找到对应的 `NSScreen`，取 `frame` 与 `visibleFrame` 之差（菜单栏 + Dock），并做上一节说的 y 轴翻转；找不到 `NSScreen` 时优雅退回 `bounds()`。
- [crates/gpui_macos/src/display.rs:L79-L106](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/display.rs#L79-L106) —— `uuid()`：`CGDisplayCreateUUIDFromDisplayID` → `CFUUIDGetUUIDBytes` → 手工逐字节拼成 `Uuid`，最后 `CFRelease` 释放 CoreFoundation 对象。这是四套实现里唯一由操作系统原生提供的稳定身份。
- [crates/gpui_macos/src/platform.rs:L616-L624](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L616-L624) —— `MacPlatform` 对两个 trait 方法的实现：一行包一个 `Rc<MacDisplay>`。

**Wayland**

- [crates/gpui_linux/src/linux/wayland/client.rs:L1435-L1476](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/wayland/client.rs#L1435-L1476) —— `wl_output` 事件分发：`Name`/`Scale`/`Geometry`/`Mode` 逐项填进 `InProgressOutput`，`Done` 事件触发 `complete()` 判定。这就是 4.3.2 状态机的落地代码。
- [crates/gpui_linux/src/linux/wayland/client.rs:L272-L303](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/wayland/client.rs#L272-L303) —— `InProgressOutput::complete()`（position 与 size 必须都到齐，scale 缺省为 1）与最终形态 `Output`（几何以 `Bounds<DevicePixels>` 存储）。
- [crates/gpui_linux/src/linux/wayland/client.rs:L929-L942](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/wayland/client.rs#L929-L942) —— `displays()`：把 `state.outputs` 逐条换成 `WaylandDisplay`，关键一行 `output.bounds.to_pixels(output.scale as f32)` 完成「物理像素 → 逻辑像素」的除法。
- [crates/gpui_linux/src/linux/wayland/client.rs:L960-L962](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/wayland/client.rs#L960-L962) —— `primary_display()` 恒返回 `None`：Wayland 协议没有主显示器概念，契约的 `Option` 为它留了后门。
- [crates/gpui_linux/src/linux/wayland/display.rs:L26-L42](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/wayland/display.rs#L26-L42) —— `PlatformDisplay for WaylandDisplay`：`id()` 用协议对象编号；`uuid()` 用 `Uuid::new_v5(NAMESPACE_DNS, name)` 从 output 名字**确定性派生**（同一台屏重启后名字不变则 uuid 不变），没有名字就报错；`bounds()` 返回缓存值。注意它没有覆盖 `visible_bounds`——Wayland 客户端不知道面板占多大地，只能退回默认实现（等于 `bounds()`）。

**X11**

- [crates/gpui_linux/src/linux/x11/display.rs:L14-L36](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/x11/display.rs#L14-L36) —— `X11Display::new`：从 XCB 连接的 `setup().roots[x_screen_index]` 读 `width_in_pixels`/`height_in_pixels`，除以 scale 得逻辑尺寸；origin 固定为默认 (0,0)。`uuid` 则是 `Uuid::from_bytes([0; 16])` —— **全零占位符**，所有 X11 显示器的 uuid 都相同，持久化身份在这套实现里等于不可用（这也解释了为什么契约把 `uuid()` 设计成尽力而为的 `Result`）。
- [crates/gpui_linux/src/linux/x11/client.rs:L1546-L1570](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/x11/client.rs#L1546-L1570) —— `displays()` 遍历 `setup().roots` 每个 screen 构造一个 `X11Display`；`primary_display()` 用 `state.x_root_index`（当前所在 screen）构造。X11 的「显示器」粒度是 X screen，而不是物理显示器——多数现代 X 会话只有一个 screen 横跨所有物理屏。

**Windows**

- [crates/gpui_windows/src/display.rs:L148-L172](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/display.rs#L148-L172) —— `available_monitors()`：`EnumDisplayMonitors` 系统调用 + `monitor_enum_proc` 回调把 `HMONITOR` 收进 `SmallVec<[HMONITOR; 4]>`（栈上内联 4 个，覆盖绝大多数机器）。
- [crates/gpui_windows/src/display.rs:L36-L75](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/display.rs#L36-L75) —— `WindowsDisplay::new`：与 macOS「每次调用实时查询」相反，Windows 实现在构造时一次性算好**所有字段并缓存**——`GetMonitorInfoW` 的 `rcMonitor`（整屏）与 `rcWork`（工作区，扣除任务栏）分别变成 `bounds` 与 `visible_bounds`；`GetDpiForMonitor(MDT_EFFECTIVE_DPI)` 除以 96 得 scale，物理矩形除以 scale 得逻辑矩形；uuid 由 `szDevice` 设备名（如 `\\.\DISPLAY1`）派生 v5。
- [crates/gpui_windows/src/display.rs:L81-L93](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/display.rs#L81-L93) —— `primary_monitor()`：`MonitorFromPoint((0,0), MONITOR_DEFAULTTOPRIMARY)`——「覆盖 (0,0) 点的显示器就是主显示器」，注释链到 Raymond Chen 的经典解释。
- [crates/gpui_windows/src/display.rs:L113-L146](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/display.rs#L113-L146) —— `displays()`（枚举 + 构造）与 `PlatformDisplay` 实现（四个字段全是读缓存）。

**两个补注（headless 与 Web）**

- [crates/gpui_linux/src/linux/headless/window.rs:L25-L51](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/headless/window.rs#L25-L51) —— `HeadlessDisplay`：写死的 1920×1080、`DisplayId::new(0)`、`Uuid::nil()`，注释直言「恰好只有一台 headless 显示器，nil 即稳定身份」。配合 [crates/gpui_linux/src/linux/headless/client.rs:L66-L72](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/headless/client.rs#L66-L72)（`displays` 返回 `[它]`、`primary_display` 返回 `Some(它)`），无头环境里所有窗口几何都有据可查。
- [crates/gpui_web/src/display.rs:L18-L101](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_web/src/display.rs#L18-L101) —— `WebDisplay`：浏览器里「显示器」退化为「当前浏览器窗口所在的 screen」。`bounds()` 用 `window.screen().width/height`；`visible_bounds()` 用 `innerWidth/innerHeight`（视口，天然排除浏览器地址栏等 UI——网页版的「任务栏」就是浏览器 chrome）；`id` 固定为 1；uuid 是构造时随机生成的 `new_v4()`——**每次构造都不同**，跨刷新不稳定。它还覆盖了 `default_bounds()`（可见区的 75% 居中），是少数重写该方法的地方。

#### 4.3.4 代码实践（对照表 + 双屏/缩放验证）

1. **实践目标**：用一张自制的对照表固化四套实现的差异，并在真实多屏/缩放环境下验证逻辑像素换算。
2. **操作步骤**：
   1. 按 4.3.1 的表头（列表来源 / 主显示器 / uuid / 缩放）重画一张表，每格补上你刚读过的源码行号引用。
   2. 在系统显示设置里（或 `xrandr`、Windows 的缩放滑块）把某台显示器缩放调为非 100%（如 200%）。
   3. 运行下一节综合实践里的 `display_probe` 示例，记录该屏打印出的逻辑尺寸。
   4. 用公式验证：打印的逻辑宽 × 缩放百分比 ≈ 系统报告的物理分辨率。
3. **需要观察的现象**：
   - Wayland/X11 下 scale 正确时，`bounds().size` 是物理分辨率的一半（200% 缩放）。
   - macOS/Windows 下 `visible_bounds().size` 在对应方向上小于 `bounds().size`，差值约等于 Dock/任务栏宽度。
   - Wayland 与 X11 下 `visible_bounds()` 打印结果应与 `bounds()` 完全一致（走默认实现）。
4. **预期结果**：换算误差在几个像素内（近似 DPI 取整）。若完全对不上，检查该平台 scale 的来源（Wayland 的 `Scale` 事件、X11 的客户端配置）。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：macOS 的 `primary()` 为什么舍近求远，不用 `all()` 返回的第一个？

**答案**：[crates/gpui_macos/src/display.rs:L29-L35](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/display.rs#L29-L35) 的注释：机器刚唤醒等状态下 `CGGetActiveDisplayList` 不保证返回活动显示器列表，而 AppKit 的 `NSScreen::screens` 始终可用且第 0 个就是带菜单栏的主屏；Chromium 也采用同一策略。

**练习 2**：在 X11 实现上，把窗口「记住上次所在显示器」的功能（存 uuid）会出什么问题？

**答案**：X11 的 `uuid()` 返回全零占位（[crates/gpui_linux/src/linux/x11/display.rs:L44-L46](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/x11/display.rs#L44-L46)），所有显示器 uuid 相同，存档后永远匹配到列表里第一台。功能降级但不报错——契约把 uuid 定义为 `Result` 且消费方（如 zed.rs 的 `find`）找不到就自然回退，正是为这种「尽力而为」的实现留的余地。

**练习 3**：Wayland 实现为什么不像 Windows 那样在构造时缓存 `visible_bounds`？

**答案**：不是风格差异，是**信息不存在**：Wayland 协议不向客户端暴露面板（任务栏）的位置和大小，客户端无从计算工作区，所以 `WaylandDisplay` 干脆不覆盖 `visible_bounds()`，走 trait 默认实现返回 `bounds()`（[crates/gpui/src/platform.rs:L358-L360](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L358-L360)）。Windows 能做是因为 `GetMonitorInfoW` 直接给出 `rcWork`。

## 5. 综合实践

**任务**：写一个 `display_probe` 示例程序，枚举并报告当前所有显示器，把本讲的契约、转发链路与平台差异一次串起来。

在你自己的 zed 仓库克隆里新建 `crates/gpui/examples/display_probe.rs`（gpui 的 examples 已依赖 `gpui_platform`，参考 window_positioning.rs 的导入即可）：

```rust
// 示例代码：枚举显示器并打印几何信息
#![cfg_attr(target_family = "wasm", no_main)]

use gpui::App;
use gpui_platform::application;

fn probe() {
    application().run(|cx: &mut App| {
        let displays = cx.displays();
        println!("检测到 {} 台显示器", displays.len());

        for (i, display) in displays.iter().enumerate() {
            let bounds = display.bounds();
            let visible = display.visible_bounds();
            let uuid = display
                .uuid()
                .map(|u| u.to_string())
                .unwrap_or_else(|e| format!("<不可用: {e}>"));

            println!("[{i}] id={:?} uuid={uuid}", display.id());
            println!(
                "    bounds         origin=({}, {}) size=({} x {})",
                bounds.origin.x, bounds.origin.y, bounds.size.width, bounds.size.height
            );
            println!(
                "    visible_bounds origin=({}, {}) size=({} x {})",
                visible.origin.x, visible.origin.y, visible.size.width, visible.size.height
            );
            println!(
                "    被系统 UI 占用: {} x {}",
                bounds.size.width - visible.size.width,
                bounds.size.height - visible.size.height
            );
        }

        match cx.primary_display() {
            Some(primary) => println!("主显示器: {:?}", primary.id()),
            None => println!("主显示器: <平台无此概念（如 Wayland）>"),
        }

        cx.quit(); // 打印完就退出，不进入交互
    });
}

#[cfg(not(target_family = "wasm"))]
fn main() {
    probe();
}
```

运行：

```bash
cargo run -p gpui --example display_probe
```

**验证清单**：

1. 单屏 + 100% 缩放：`bounds` 与 `visible_bounds` 的差值 ≈ 任务栏/Dock 占地（macOS/Windows），或两者完全相等（Linux 桌面多数配置）。
2. 缩放：把系统缩放调到 200% 再跑一次，`bounds().size` 应变为原来的一半（Wayland/X11/Windows）。
3. 双屏：确认两行输出、两个不同的 `DisplayId`；macOS 上两台的 `bounds().origin` 都是 (0,0)（4.3.3 讲过的「丢弃 origin」行为）。
4. uuid 稳定性：重启（或重跑）程序两次，macOS/Windows 的 uuid 应不变；Web 上每次都变；X11 恒为全零。
5. 无头环境：`ZED_HEADLESS=1 cargo run -p gpui --example display_probe`（Linux）应打印 HeadlessDisplay 的 1920×1080。

**预期结果**：以上现象与本讲各实现的源码行为一一对应；`cx.quit()` 在启动回调里触发后的退出时机依平台而异（u2-l2 讲过 macOS 会推迟 terminate），若程序未自动退出可 Ctrl+C。各项数值**待本地验证**。

## 6. 本讲小结

- `PlatformDisplay` 用五个方法描述一台显示器：`id`/`uuid`/`bounds` 必须实现，`visible_bounds`/`default_bounds` 有默认实现；`DisplayId` 是运行期不透明句柄（u64 包装），`uuid` 是可跨重启持久化的身份，两者不可混用。
- `visible_bounds()` 表达「不被任务栏/Dock/浏览器 UI 挡住的工作区」；macOS 用 `NSScreen.visibleFrame` 并做 y 轴翻转，Windows 用 `rcWork`，Wayland/X11 信息不存在、退回 `bounds()`。
- `Platform::displays` 与 `primary_display` 都无默认实现；应用层经 `App::displays`/`primary_display`/`find_display` 转发，`Bounds::centered`/`maximized` 组成「指定 id → 主显示器 → 兜底矩形」的回退链。
- 四套实现数据来源各异：macOS 主动查询 CoreGraphics/NSScreen；Wayland 被动累积 `wl_output` 事件且没有主显示器概念（返回 `None`）；X11 从 XCB screen 列表读取、uuid 是全零占位；Windows 用 `EnumDisplayMonitors` 并在构造时缓存全部几何与 DPI。
- 值得记住的诚实细节：macOS `bounds()` 丢弃 origin（每台屏都是局部 (0,0) 坐标）；X11 的 uuid 不可持久化；Web 的 uuid 每次随机——契约的 `Option`/`Result` 与消费方的回退链正是为这些「尽力而为」的实现设计的。

## 7. 下一步学习建议

显示器几何最终是为「把窗口放对位置」服务的。下一讲 **u3-l1 窗口创建主链路：WindowOptions、WindowParams 与 Platform::open_window** 会跟踪 `WindowOptions::display_id`（本讲 window_positioning 示例已经用上它）如何一路下沉到平台层开窗，把 `PlatformDisplay` 与 `PlatformWindow` 两条线索接起来。

继续阅读建议：

- [crates/gpui/examples/window_positioning.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/window_positioning.rs) —— 把它改造成「在每台显示器的 visible_bounds 内开窗口」，观察与 bounds 定位的差别。
- [crates/zed/src/zed.rs:L361](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/zed/src/zed.rs#L361) 起的 `build_window_options` —— 生产代码如何组合 uuid 恢复、窗口装饰等选项。
- u8-l2（PlatformAtlas 与渲染后端）会用到显示器的 scale 信息解释 HiDPI 渲染，可提前留个印象。
