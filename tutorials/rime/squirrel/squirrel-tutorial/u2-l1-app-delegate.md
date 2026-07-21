# 应用委托与全局状态

## 1. 本讲目标

本讲是「进阶：输入处理主链路」单元的第一讲。在前一单元里，我们已经知道 Squirrel 是一个后台常驻的输入法进程，由 `Main.swift` 启动。本讲要回答一个关键问题：**当进程跑起来之后，谁来掌管那些「不属于任何一次输入会话」的全局资源？**

答案就是 `SquirrelApplicationDelegate`（应用委托，下文简称 AppDelegate）。学完本讲你应该能够：

1. 说出 AppDelegate 持有哪些全局状态（`config` / `panel` / `statusItem` / `updateController`），以及它们各自归谁所有、活多久。
2. 描述 `applicationWillFinishLaunching` 在启动时做的三件事，并理解它与 `Main.swift` 中 `setupRime/startRime/loadSettings` 的先后关系。
3. 解释 `addObservers` 注册的三类通知中心、对应哪些命令行命令。
4. 理解状态栏图标如何随 ASCII 模式切换显示「中」/「Ａ」。
5. 能用「生命周期匹配」这一原则，论证为什么 librime 引擎、候选面板这些全局资源必须挂在 AppDelegate 上，而不是挂在每次会话都会新建销毁的输入控制器（`SquirrelInputController`）上。

第 5 点是本讲最重要的「设计直觉」，它会贯穿整个第二单元。

## 2. 前置知识

在进入源码之前，先澄清几个 Cocoa / 输入法的概念。

### 2.1 应用委托（Application Delegate）是什么

macOS 的 GUI 程序围绕 `NSApplication` 这个单例运行。`NSApplication` 把「应用级事件」（启动、退出、切到后台、收到通知）转发给一个**委托对象**（delegate）。委托只需要遵循 `NSApplicationDelegate` 协议，实现感兴趣的方法即可。你可以把 AppDelegate 理解成「整个 App 的管家」：它在 App 出生时被叫醒干活，在 App 退出前负责善后。

### 2.2 单例 vs 会话

输入法里有两类生命周期完全不同的对象：

- **App 级（单例）**：整个 App 从启动到退出只有一份。例如 librime 引擎实例、那个唯一的候选词面板、菜单栏图标。它们的「生」与「死」应当绑定到 App 的生命上。
- **会话级（per-session）**：每当你把光标点进一个文本框、系统就会创建一个 `SquirrelInputController`；光标离开、会话结束就销毁它。同一段时间里可能有多个 controller 同时存在（简体 Hans + 繁体 Hant 模式，或多个 App 同时输入）。

这个区别是本讲所有设计决策的根。把 App 级资源放进会话级对象里，就会「会话一销毁，资源跟着没了」。

### 2.3 三种通知中心

Cocoa 里有三个不同作用域的通知中心，本讲 `addObservers` 会全部用到：

| 通知中心 | 作用域 | 典型用途 |
| --- | --- | --- |
| `NotificationCenter.default` | 进程内 | 同一个 App 内部对象间通信 |
| `DistributedNotificationCenter.default()` | 跨进程 | 命令行进程发消息给常驻的输入法进程 |
| `NSWorkspace.shared.notificationCenter` | 系统工作区 | 关机、切换 App 等系统事件 |

> 承接 [u1-l4](./u1-l4-entry-and-startup.md)：`Squirrel --reload` 这类命令之所以能驱动一个「已经在跑」的输入法实例，靠的就是 `DistributedNotificationCenter`。本讲会看到这些通知在 AppDelegate 里是被谁接收的。

## 3. 本讲源码地图

本讲只深入一个文件，另一个文件用于交代调用时机。

| 文件 | 作用 |
| --- | --- |
| [sources/SquirrelApplicationDelegate.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift) | 本讲主角。定义 App 管家：持有全局状态、响应启动/退出、注册观察者、管理状态栏图标、承载 librime 的 C 回调。 |
| [sources/Main.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift) | 入口。在这里 `new` 出 AppDelegate、赋给 `app.delegate`，并在 `app.run()` 之前手动调用 `setupRime/startRime/loadSettings`。 |

> AppDelegate 还顺带持有 `SquirrelPanel`、`SquirrelConfig`、`SquirrelInstaller`（`SquirrelInstaller` 在状态栏可见性判断里用到）等类型实例，本讲只把它们当作「AppDelegate 持有的资源」来看，内部细节留待后续讲义（panel 见第四单元，config 见 u3-l1）。

## 4. 核心概念与源码讲解

### 4.1 全局状态属性（config / panel / statusItem / updateController）

#### 4.1.1 概念说明

AppDelegate 一出生就背着「整个 App 要用的东西」。我们先把它的全部存储属性列清楚，再逐个分析归谁所有。关键问题只有一个：**这个资源的生命周期，是不是和 App 一样长？** 如果是，它就属于 AppDelegate。

#### 4.1.2 核心流程

把 AppDelegate 的属性分成三类来理解：

1. **引擎句柄类**：`rimeAPI`。它是一个 `let` 常量，App 一启动就从 C 函数 `rime_get_api_stdbool()` 取回，指向 librime 的全部能力。整个 App 共用这一个句柄。
2. **App 级单例资源**：`config`（前端配置 squirrel.yaml）、`panel`（唯一的候选面板）、`statusItem`（菜单栏图标）。它们是 `var` 且可选，因为要在启动过程中的某个时刻才被创建。
3. **行为开关 + 外部子系统**：`enableNotifications` / `showStatusIcon` 是两个布尔开关；`updateController` 是 Sparkle 自动更新框架的控制器，声明时就被急切初始化。

#### 4.1.3 源码精读

类的声明与协议遵循（注意它同时是 App 委托、Sparkle 驱动委托、用户通知中心委托）：

[sources/SquirrelApplicationDelegate.swift:L13-L16](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L13-L16) —— 类声明，以及三个供通知用的静态字符串常量。

接着是全局状态属性的核心：

[sources/SquirrelApplicationDelegate.swift:L18-L24](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L18-L24) —— 这六七行就是本讲最关键的「全局状态清单」：

- `rimeAPI`：librime 引擎 API 句柄，`let`，App 级唯一。
- `config: SquirrelConfig?`：前端配置门面（读 squirrel.yaml）。可选，在 `loadSettings()` 里被赋值。
- `panel: SquirrelPanel?`：全局唯一的候选词面板。可选，在 `applicationWillFinishLaunching` 里被创建。
- `enableNotifications` / `showStatusIcon`：两个运行时开关。
- `statusItem: NSStatusItem?`：菜单栏图标。可选，按需创建/移除。
- `updateController = SPUStandardUpdaterController(...)`：Sparkle 更新控制器，**声明即启动**（`startingUpdater: true`），所以 App 一旦跑起来它就开始检查更新。

注意一个容易忽略的细节：`updateController` 不是可选、没有 `lazy`，它在 `init` 阶段就被创建——这意味着 AppDelegate 构造时 Sparkle 就上线了，比 `setupRime` 还早。

为什么这些都是属性、而不是局部变量？因为它们必须跨方法存活：`applicationWillFinishLaunching` 创建 `panel`，而 `rimeUpdate`（在 controller 里）要往这个 `panel` 上画候选；`loadSettings` 读出 `config`，而切换 schema 时还要再用它做回退。AppDelegate 把它们「钉」在自己身上，让所有方法都能拿到同一份实例。

#### 4.1.4 代码实践

**实践目标**：亲手在源码里把「全局状态清单」和「谁创建它」对应起来。

**操作步骤**：

1. 打开 [sources/SquirrelApplicationDelegate.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift)，定位 L18–L24。
2. 在编辑器里对 `config`、`panel`、`statusItem`、`updateController`、`rimeAPI` 这五个属性分别用「查找引用」功能，看它们各自第一次被**赋值/创建**的位置。
3. 填下面这张表（参考答案见 4.1.5）：

| 属性 | 第一次创建/赋值的位置 | 何时销毁/释放 |
| --- | --- | --- |
| `rimeAPI` | 声明处（L18） | App 退出时 `rimeAPI.finalize()` |
| `config` | `loadSettings()`（L170） | `shutdownRime()` 里 `config?.close()` |
| `panel` | `applicationWillFinishLaunching`（L59） | `applicationWillTerminate` 里 `panel?.hide()` |
| `statusItem` | `setupStatusItem()`（经 `refreshStatusItem`） | `applicationWillTerminate` 里 `removeStatusItem` |
| `updateController` | 声明处（L24） | 随 AppDelegate 一起 |

**需要观察的现象**：你会看到 `panel`、`config`、`statusItem` 都是**可选**（`?`）属性，而 `rimeAPI`、`updateController` 不是。这说明前者的创建时机分散在启动流程的不同阶段，后者在 AppDelegate 构造时就已就绪。

**预期结果**：你能复述每个属性「由谁、在哪一行、什么时候」创建，以及「由谁在何时」善后。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `rimeAPI` 用 `let` 而 `config` / `panel` 用 `var ... ?`？

> **答案**：`rimeAPI` 是从 C 接口取回的引擎句柄，App 生命周期内不变，用 `let` 表达「一次性绑定、永不改」。而 `config` / `panel` 要在启动流程的特定阶段才创建（且退出时要清理），必须可写、必须可选以表达「尚未创建」的状态。

**练习 2**：`updateController` 在 AppDelegate 的 `init` 阶段就创建了，比引擎初始化还早。这会带来什么好处和潜在风险？

> **答案**：好处是 App 一上线就开始检查更新，无需额外触发；潜在风险是若 Sparkle 自身需要读取配置或访问网络，而此时其他子系统（如引擎、配置）尚未就绪，可能要靠 Sparkle 内部的延迟/重试机制兜底。本项目中 Sparkle 是独立框架，不依赖 librime，所以这样安排是安全的。

---

### 4.2 applicationWillFinishLaunching 初始化

#### 4.2.1 概念说明

`applicationWillFinishLaunching(_:)` 是 `NSApplicationDelegate` 协议里的钩子，在 App 即将完成启动、即将进入事件循环前被调用。它通常是「创建 App 级 UI 资源、注册监听」的最佳时机——此时 `NSApplication` 已就绪，但还没开始处理用户输入。

注意：这个方法**不是** AppDelegate 做的第一件事。在它之前，`Main.swift` 已经手动调用过 `setupRime` / `startRime` / `loadSettings`（详见 [u1-l4](./u1-l4-entry-and-startup.md)）。理清这个先后顺序非常重要。

#### 4.2.2 核心流程

启动阶段的真实时间线（来自 [sources/Main.swift:L125-L153](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L125-L153)）：

```
1. AppDelegate() 构造         ← updateController 在此时急切创建
2. app.delegate = delegate
3. setupRime()                ← 登记 RimeTraits、安装通知回调（引擎尚未 initialize）
4. startRime(fullCheck: false) ← rimeAPI.initialize + start_maintenance
5. loadSettings()             ← 创建 config、读开关；此时 panel 还是 nil，panel.load 被跳过
6. app.run()
   └─ 触发 applicationWillFinishLaunching:
        6a. panel = SquirrelPanel(position: .zero)   ← panel 在此刻才出生
        6b. refreshStatusItem()                       ← 按需创建菜单栏图标
        6c. addObservers()                            ← 注册所有通知观察者
   └─ 进入事件循环，开始接收键盘事件
```

一个容易被忽略的点：第 5 步 `loadSettings()` 执行时 `panel` 还是 `nil`，所以其中 `if let panel = panel` 分支首次是空跑的。面板的样式加载主要发生在 `applicationWillFinishLaunching` 创建 `panel` 之后、以及后续切换 schema 时。

#### 4.2.3 源码精读

`applicationWillFinishLaunching` 本体非常简洁，只有三行：

[sources/SquirrelApplicationDelegate.swift:L58-L62](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L58-L62) —— 创建面板、刷新状态栏图标、注册观察者。三件事的共性是：都依赖 `NSApplication` 已就绪（面板是 `NSPanel` 子类、状态栏图标要 `NSStatusBar`、观察者要往系统的通知中心注册）。

对照退出时的善后（与启动对称）：

[sources/SquirrelApplicationDelegate.swift:L64-L73](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L64-L73) —— `applicationWillTerminate` 里移除所有观察者、隐藏面板、把菜单栏图标从状态栏摘下并置空。注意引擎的真正 `finalize` 不在这里，而是在 [sources/Main.swift:L155](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L155) `app.run()` 返回之后，由 `Main.swift` 调用 `rimeAPI.finalize()`。

还有一个与「启动/退出」配套的钩子：

[sources/SquirrelApplicationDelegate.swift:L245-L249](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L245-L249) —— `applicationShouldTerminate` 在系统询问「可以退出吗」时，调用 `rimeAPI.cleanup_all_sessions()` 清理所有输入会话，再回答 `.terminateNow`。这是引擎层面的收尾。

#### 4.2.4 代码实践

**实践目标**：在源码里验证「启动时间线」，确认 `loadSettings` 确实在 `panel` 创建之前。

**操作步骤**：

1. 读 [sources/Main.swift:L146-L153](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L146-L153)，确认三个手动调用（`setupRime` / `startRime` / `loadSettings`）出现在 `app.run()` 之前。
2. 读 [sources/SquirrelApplicationDelegate.swift:L169-L182](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L169-L182)，看 `loadSettings()` 内部对 `panel` 做了什么。
3. 问自己：`loadSettings` 里 `if let panel = panel, let config = self.config { panel.load(...) }` 这段，在「App 刚启动、`app.run()` 还没调用」时会执行 `panel.load` 吗？

**需要观察的现象**：第 3 步你会得出「不会，因为此时 `panel` 还是 `nil`」。

**预期结果**：能用一句话解释「为什么面板样式的首次加载，不靠 `loadSettings` 在启动时的那次调用」——因为 `panel` 还没出生；真正的样式加载发生在面板创建之后（以及后续 schema 切换时）。

> 待本地验证：若想亲见这个顺序，可在 `applicationWillFinishLaunching` 与 `loadSettings` 各加一行 `print`（属于源码阅读型实践，仅本地调试用，勿提交），运行后观察日志先后。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `panel = SquirrelPanel(...)` 从 `applicationWillFinishLaunching` 挪到 `loadSettings()` 开头，会发生什么？

> **答案**：功能上仍可工作，因为 `loadSettings` 在 `app.run()` 之前被调用，此时 `NSApplication.shared` 已创建（`NSPanel` 的构造在 `Main.swift` 里 `NSApplication.shared` 之后）。但语义上更混乱：`loadSettings` 的职责会从「读配置」膨胀到「还要建 UI」。当前代码把「建 UI」和「读配置」分开，职责更清晰。

**练习 2**：`applicationWillTerminate` 里没有调用 `rimeAPI.finalize()`，引擎最终是在哪里被销毁的？

> **答案**：在 [sources/Main.swift:L155](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L155)，`app.run()` 返回之后由 `Main.swift` 调用 `rimeAPI.finalize()`。AppDelegate 里 `workspaceWillPowerOff` 也会在关机前调用 `shutdownRime()`（含 `finalize`）做提前收尾。这是「双保险」。

---

### 4.3 观察者注册 addObservers

#### 4.3.1 概念说明

AppDelegate 不仅是「管家」，还是「总接线员」。它要在启动时把各类通知（来自系统、来自其他进程、来自引擎）接到自己的处理方法上。`addObservers()` 就是这张「接线表」。理解它的关键，是分清三种通知中心——尤其要看出哪些分布式通知对应哪些命令行命令。

#### 4.3.2 核心流程

`addObservers` 注册的观察者可以分成三组：

```
A. 工作区通知中心（系统级事件）
   willPowerOff ───────────────→ workspaceWillPowerOff   （关机前 shutdownRime）

B. 分布式通知中心（跨进程：命令行 → 常驻实例）
   SquirrelReloadNotification        → rimeNeedsReload     （= Squirrel --reload）
   SquirrelSyncNotification          → rimeNeedsSync       （= Squirrel --sync）
   SquirrelToggleASCIIModeNotification → rimeToggleASCIIMode （= --ascii / --nascii）
   SquirrelGetASCIIModeNotification  → rimeGetASCIIMode    （= --getascii）

C. 输入源切换通知（系统级，主队列）
   kTISNotifySelectedKeyboardInputSourceChanged → 更新图标可见性 + finalizeStrandedComposition
```

B 组是这套机制的核心：`Squirrel` 命令行进程（一个独立进程）发出分布式通知，正在常驻运行的输入法进程通过 AppDelegate 接收并处理。这就是为什么 `Squirrel --reload` 能让「已经在跑」的输入法重新部署。

#### 4.3.3 源码精读

`addObservers` 全貌：

[sources/SquirrelApplicationDelegate.swift:L230-L243](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L230-L243) —— 先注册工作区关机通知，再注册四个分布式通知，最后注册输入源切换通知。注意最后一项显式指定 `queue: .main`，因为它的回调要碰 UI（状态栏图标可见性）。

接收后的处理方法都很短，以 reload 和 sync 为例：

[sources/SquirrelApplicationDelegate.swift:L404-L412](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L404-L412) —— `rimeNeedsReload` 调用 `deploy()`（关闭并重启引擎、重读配置）；`rimeNeedsSync` 调用 `syncUserData()`。`deploy()` 的本体在 [L81-L86](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L81-L86)，体现了「shutdown → start → loadSettings」的重启三步。

ASCII 模式那一对是「请求-转发」模式，比较特殊：

[sources/SquirrelApplicationDelegate.swift:L414-L427](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L414-L427) —— `rimeToggleASCIIMode` 收到分布式通知后，**转发**为一个进程内通知 `SquirrelSetASCIIModeNotification`；`rimeGetASCIIMode` 同理转发为 `SquirrelReportASCIIModeNotification`。为什么转发？因为真正持有「当前 ASCII 模式状态」的是某个输入会话的 controller，AppDelegate 不知道，只能通过进程内通知「广播」给 controller 们，由它们响应/上报。这套请求-应答协议的完整时序在 [u5-l1](./u5-l1-distributed-notifications.md) 会详细展开。

输入源切换通知的回调直接内联，做了两件事：

[sources/SquirrelApplicationDelegate.swift:L239-L242](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L239-L242) —— 切换输入法后，更新状态栏图标可见性（如果切走了 Squirrel，就隐藏图标），并尝试收尾「悬挂组合」（详见 4.3.5 与 u5-l1）。

#### 4.3.4 代码实践

**实践目标**：把命令行命令、分布式通知名、AppDelegate 处理方法三者对上号。

**操作步骤**：

1. 打开 [sources/Main.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift)，找出 `--reload` / `--sync` / `--ascii` / `--nascii` / `--getascii` 各自发出的分布式通知名（提示：L37-L100）。
2. 回到 [sources/SquirrelApplicationDelegate.swift:L230-L243](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L230-L243)，确认这些通知名都被注册了观察者。
3. 画出「命令行进程发通知 → 常驻实例 AppDelegate 接收 → 调用什么方法 → 最终做什么」的对照表。

**需要观察的现象**：你会发现命令行进程（`Main.swift` 的 `--reload` 分支）只做一件事——`postNotificationName`，然后就 `return true` 退出了。真正的重活（`deploy()`）发生在常驻实例里。

**预期结果**：能复述「`Squirrel --reload` 不自己部署，而是发 `SquirrelReloadNotification`，由常驻 AppDelegate 的 `rimeNeedsReload` 接收后调用 `deploy()`」。

#### 4.3.5 小练习与答案

**练习 1**：为什么最后一项观察者（输入源切换）要指定 `queue: .main`，而前面几个分布式通知不指定？

> **答案**：输入源切换回调要修改状态栏图标（`statusItem.isVisible`）等 UI 状态，必须在主线程执行。前面的 reload/sync 等回调最终会触引擎部署，本身不强依赖主线程，故用默认队列即可。

**练习 2**：`rimeToggleASCIIMode` 为什么不直接改 ASCII 模式，而是再发一个进程内通知 `SquirrelSetASCIIModeNotification`？

> **答案**：因为 AppDelegate 不持有任何输入会话，也不知道「当前活动会话」是哪个。ASCII 模式状态属于会话级，存在 controller/引擎 session 里。AppDelegate 只能通过进程内通知广播请求，由持有活动会话的 controller 响应（这套转发在 u5-l1 详细讲）。

---

### 4.4 状态栏图标与 ASCII 模式显示

#### 4.4.1 概念说明

macOS 右上角菜单栏上那个「中」/「Ａ」小图标，就是 Squirrel 的 `statusItem`。它的显示内容会随当前是中文还是 ASCII（西文）模式而变化，让用户一眼看出当前状态。这一节看 AppDelegate 如何创建、显隐、刷新这个图标，以及「ASCII 模式」这个状态是从哪里冒出来的。

#### 4.4.2 核心流程

状态栏图标涉及一组互相调用的小方法，关系如下：

```
loadSettings() ──读 showStatusIcon──→ refreshStatusItem()
                                        ├─ 需要显示且 item==nil → setupStatusItem()
                                        │                          ├─ 创建 NSStatusItem
                                        │                          ├─ applyStatusIcon(初始: 中)
                                        │                          └─ updateStatusItemVisibility()
                                        └─ 不需要显示且 item!=nil → 从状态栏移除并置空

输入法被切到前台/后台 ──(输入源切换通知)──→ updateStatusItemVisibility()
                                          （按 currentInputSourceID 是否以 im.rime...Squirrel 开头决定 isVisible）

librime 通知 ascii_mode 变化 ──(notificationHandler)──→ updateStatusIcon(asciiMode:)
                                                         └─(派发到主线程)→ applyStatusIcon()
                                                                              ├─ 有 schemaLabel → 显示方案缩写
                                                                              └─ 否则 → asciiMode ? "Ａ" : "中"
```

#### 4.4.3 源码精读

公开入口，负责把刷新动作派发到主线程：

[sources/SquirrelApplicationDelegate.swift:L75-L79](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L75-L79) —— `updateStatusIcon` 用 `DispatchQueue.main.async` 异步切到主线程，再调 `applyStatusIcon`。因为它的调用方（librime 回调）不一定在主线程，而碰 `statusItem.button` 必须在主线程。

实际改图标标题的方法：

[sources/SquirrelApplicationDelegate.swift:L385-L392](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L385-L392) —— 优先用方案缩写 `schemaLabel`（例如「月」代表朙月拼音）；没有缩写就按 ASCII 模式显示全角「Ａ」或汉字「中」。这里用全角「Ａ」而不是半角「A」，是为了在菜单栏里与「中」字宽度视觉一致。

创建/移除的决策点：

[sources/SquirrelApplicationDelegate.swift:L342-L362](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L342-L362) —— `refreshStatusItem` 依据 `showStatusIcon`（来自 squirrel.yaml 的 `status_icon/show`，默认 true）决定创建或移除；`setupStatusItem` 创建 `NSStatusItem`、设置半粗字体和工具提示，并把初始图标设为「中」。

可见性随输入源切换：

[sources/SquirrelApplicationDelegate.swift:L364-L368](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L364-L368) —— 只有当系统当前选中的输入源 ID 以 `im.rime.inputmethod.Squirrel` 开头时，图标才可见。这样当你切到别的输入法时，Squirrel 的图标会自动隐去。

那么 `asciiMode` 这个状态从哪来？来自 librime 的「option」通知。AppDelegate 通过一个 C 回调接收：

[sources/SquirrelApplicationDelegate.swift:L265-L312](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L265-L312) —— `notificationHandler` 是个顶层 `@convention(c)` 函数，通过 `Unmanaged<SquirrelApplicationDelegate>.fromOpaque(contextObject!)` 拿回 AppDelegate（承接 [u1-l4](./u1-l4-entry-and-startup.md) 提到的 `Unmanaged` 桥接）。当收到 `messageType == "option"` 且 `optionName == "ascii_mode"` 时，调用 `delegate.updateStatusIcon(asciiMode: state, schemaLabel: shortLabel())`。这就是「中」↔「Ａ」切换的源头。

#### 4.4.4 代码实践

**实践目标**：追踪一次「按下 Shift 切换到 ASCII 模式」之后，状态栏图标是如何从「中」变成「Ａ」的。

**操作步骤**：

1. 从 [sources/SquirrelApplicationDelegate.swift:L304-L306](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L304-L306) 出发，确认 ASCII 切换会调 `updateStatusIcon`。
2. 顺着 `updateStatusIcon`（L75）→ `applyStatusIcon`（L385）读下去，确认最终改的是 `statusItem?.button` 的 `title`。
3. 思考：librime 为什么会把 ascii_mode 的变化以「option」通知发出来？前端又是怎么把它转成 UI 刷新的？

**需要观察的现象**：状态变化是从「引擎内部选项变化」→「C 回调」→「Swift AppDelegate」→「主线程」→「NSStatusItem.button.title」一路传过来的。

**预期结果**：能画出这条数据流：`rimeApi 选项变化 → notificationHandler(option, ascii_mode) → updateStatusIcon → 主线程 → applyStatusIcon → button.title = "Ａ"`。

> 待本地验证：实际切换 ASCII 模式需要安装并启用 Squirrel（macOS 13.0+），可在本地装好后按 Shift（或方案定义的切换键）观察菜单栏图标变化。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `applyStatusIcon` 优先显示 `schemaLabel` 而不是直接显示「中」/「Ａ」？

> **答案**：因为有些输入方案有专门的缩写标签（ librime 通过 `get_state_label_abbreviated` 提供），显示缩写能让用户清楚知道当前用的是哪个方案（如「月」= 朙月拼音）。只有当方案没提供缩写时，才回退到通用的「中」/「Ａ」。

**练习 2**：如果用户在 squirrel.yaml 里设 `status_icon/show: false`，状态栏图标会怎样？

> **答案**：`loadSettings` 读出 `showStatusIcon = false`（[L176](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L176)），随后 `refreshStatusItem`（[L347-L350](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L347-L350)）会把已存在的 `statusItem` 从状态栏移除并置 nil，之后图标不再显示。

---

## 5. 综合实践

**实践目标**：用一个表格 + 一段论证，把本讲四个模块串起来——说清 AppDelegate 持有的三个核心对象的生命周期归属，并论证引擎为何必须挂在 AppDelegate 上。

**操作步骤**：

1. 完成下表（核心对象生命周期归属）：

| 对象 | 类型 | 创建位置（行号） | 销毁/清理位置（行号） | 归属层级 |
| --- | --- | --- | --- | --- |
| 候选面板 | `SquirrelPanel` | L59（`applicationWillFinishLaunching`） | L68（`applicationWillTerminate` 里 `hide`） | App 级 |
| 前端配置 | `SquirrelConfig` | L170（`loadSettings`） | `shutdownRime` 里 `config?.close()`（L395） | App 级 |
| 状态栏图标 | `NSStatusItem` | L354（`setupStatusItem`） | L70-L71（`removeStatusItem` 并置 nil） | App 级 |

2. 写一段论证，回答本讲的核心问题：**为什么 librime 引擎、候选面板、菜单栏图标必须由 AppDelegate 持有，而不是由 `SquirrelInputController` 持有？** 你的论证至少要覆盖以下三点：
   - **生命周期匹配**：controller 是会话级、会被频繁创建销毁；引擎/面板是 App 级、只生灭一次。把 App 级资源放进会话级对象，会随会话销毁而丢失。
   - **共享性**：同一时刻可能存在多个 controller（Hans/Hant、多个前台 App），它们必须共享同一个引擎实例和同一个面板，不能各建一份。
   - **跨会话一致性**：配置（squirrel.yaml）、菜单栏图标是全局的，不属于任何单个会话；把它们放在 App 级，才能保证切换会话时状态连续。

3. 进阶思考（可选）：`notificationHandler` 是一个独立的顶层 C 函数，并不挂在 AppDelegate 上。它是怎么拿到 AppDelegate 实例的？（提示：见 [L145-L148](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L145-L148) 的 `context_object` 与 `Unmanaged`。）

**预期结果**：你能用「生命周期匹配 + 共享性 + 跨会话一致性」三原则，解释 AppDelegate 这个「管家」存在的必要性——这正是整个第二单元「输入处理主链路」的立足点。

## 6. 本讲小结

- AppDelegate 是整个 App 的「管家」，持有所有 App 级全局状态：`rimeAPI`（引擎句柄）、`config`（前端配置）、`panel`（唯一候选面板）、`statusItem`（菜单栏图标）、`updateController`（Sparkle 更新）。
- 真正的启动初始化分两段：`Main.swift` 在 `app.run()` 前手动调 `setupRime/startRime/loadSettings`（建引擎、读配置）；`applicationWillFinishLaunching` 在 `app.run()` 内创建 `panel`、刷新状态栏图标、注册观察者。
- `addObservers` 注册了三种通知中心的观察者：工作区关机通知、四个跨进程分布式通知（对应 `--reload/--sync/--ascii/--getascii` 命令）、输入源切换通知。
- 状态栏图标靠一组小方法协作：`refreshStatusItem` 决定创建/移除，`updateStatusItemVisibility` 按当前输入源决定显隐，`applyStatusIcon` 按 ASCII 模式显示「中」/「Ａ」或方案缩写。
- ASCII 模式的状态变化源自 librime 的 option 通知，经 `notificationHandler`（C 回调）→ `updateStatusIcon` → 主线程 → `button.title` 一路刷新到 UI。
- **核心设计原则**：App 级资源必须挂在 AppDelegate 上，不能挂在会话级的 controller 上——因为后者生命周期短、会被多实例共享、且无法保证跨会话一致性。

## 7. 下一步学习建议

本讲建立了「AppDelegate 是全局管家」的认知，接下来沿着主链路继续下钻：

- **[u2-l2 全局 librime 初始化](./u2-l2-global-rime-init.md)**：精读本讲只是点到为止的 `setupRime` / `startRime` / `loadSettings`，看 RimeTraits 各字段怎么填、`notificationHandler` 怎么安装、`squirrel.yaml` 怎么被部署。
- **[u2-l3 输入控制器生命周期与会话](./u2-l3-input-controller-lifecycle.md)**：去看与本讲「App 级」相对的「会话级」对象 `SquirrelInputController`，理解它为什么用 `weak` 持有 client。
- 若你对状态栏图标背后的 ASCII 协议更感兴趣，可先跳到 **[u5-l1 分布式通知与外部命令](./u5-l1-distributed-notifications.md)**，看完整的请求-应答时序。
