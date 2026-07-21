# 分布式通知与外部命令

## 1. 本讲目标

本讲解决一个看似简单、实则贯穿整个 Squirrel 架构的问题：**一个只读命令行的短命进程，如何去操控那个已经常驻在系统里的输入法实例？**

读者学完后应该能够：

- 说清楚 `Squirrel --reload`、`--sync`、`--ascii`、`--nascii`、`--getascii` 这些命令各自通过哪条「分布式通知」驱动常驻实例。
- 复述 ASCII 模式「请求—应答」协议的完整往返（跨进程 `DistributedNotificationCenter` + 进程内 `NotificationCenter` 的两次转发）。
- 解释为什么 `kTISNotifySelectedKeyboardInputSourceChanged` 这条系统通知被 Squirrel 用来做「悬挂组合（stranded composition）」的兜底。
- 理解「单二进制双身份」设计：同一个 `Squirrel` 可执行文件，根据命令行参数决定自己是「一次性命令」还是「常驻输入法」。

## 2. 前置知识

本讲建立在「输入处理主链路」之上，依赖以下已学概念（参见 u2-l1、u1-l4）：

- **AppDelegate 作为 App 级管家**：全局引擎句柄 `rimeAPI`、配置 `config`、面板 `panel`、状态栏图标都挂在 `SquirrelApplicationDelegate` 上，常驻实例的唯一身份。
- **单二进制双身份**：`Main.swift` 的 `main()` 先试探命令行参数，命中维护命令就执行后 `return` 退出，绝不进入输入法主循环（`app.run()`）。
- **三种通知中心**：本讲会反复用到，需要区分：
  - `NotificationCenter.default()`——**进程内**通知，同一 App 内部对象通信用。
  - `DistributedNotificationCenter.default()`——**跨进程**通知，不同 App（甚至同一 App 的不同进程实例）之间通信用。
  - `NSWorkspace.shared.notificationCenter`——系统工作区事件（如关机）。
- **librime 会话（session）**：每个 `SquirrelInputController` 持有一个 `RimeSessionId`，ASCII 模式实际是 session 上的 `ascii_mode` 选项（option）。
- **marked/commit text**：上屏与预编辑的 Cocoa 文本协议。

### 一句话区分两类通知中心

进程内 `NotificationCenter` 像一个房间里的对讲机——只有同一个 App 进程内的对象能听到；分布式 `DistributedNotificationCenter` 像广播电台——同一台 Mac 上任何进程只要调到对应频道（通知名）都能收到。本讲最精妙之处在于：**命令行短命进程无法直接调用常驻实例的方法，于是用「分布式广播」搭桥，而常驻实例内部再用「进程内对讲机」把消息转交给会话级的 controller。**

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [sources/Main.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift) | 程序入口，命令行分支：`--reload/--sync/--ascii/--nascii/--getascii` 发出分布式通知后立即退出。 |
| [sources/SquirrelApplicationDelegate.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift) | 常驻实例的「接收端」：`addObservers()` 注册四条分布式通知的回调，并做本地通知转发与悬挂组合兜底。 |
| [sources/SquirrelInputController.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift) | 会话级控制器：监听 AppDelegate 转发来的本地通知，真正操作 librime session（设 `ascii_mode`、回读状态）。 |

理解时要牢记一条贯穿主线：**命令在 Main.swift 发出 → AppDelegate（App 级）接收并转发 → SquirrelInputController（会话级）执行**。

## 4. 核心概念与源码讲解

### 4.1 分布式通知机制总览：单二进制双身份与四类通知名

#### 4.1.1 概念说明

Squirrel 的命令行维护命令（`--reload`、`--sync`、`--ascii`、`--nascii`、`--getascii`）面临一个矛盾：执行命令的是一个**全新的、短命的进程**，它一执行完就退出；但真正需要被「重新部署」「切换 ASCII」「查询状态」的，是那个**已经常驻在系统里、正接管键盘事件的输入法进程**。两者是同一段二进制代码启动的两个不同进程实例。

Swift 对象的方法调用只能在**同一进程**内进行，短命进程无法直接拿到常驻实例的对象指针。macOS 提供的解法是 `DistributedNotificationCenter`：它是一套跨进程的发布-订阅机制，发送方 `postNotificationName`，接收方 `addObserver`，系统负责跨进程投递。Squirrel 用四条具名分布式通知把命令「广播」给常驻实例：

| 命令行参数 | 分布式通知名 | `object` 携带 | 方向 |
| --- | --- | --- | --- |
| `--reload` | `SquirrelReloadNotification` | `nil` | 单向（命令 → 实例） |
| `--sync` | `SquirrelSyncNotification` | `nil` | 单向 |
| `--ascii` | `SquirrelToggleASCIIModeNotification` | `"ascii"` | 单向 |
| `--nascii` | `SquirrelToggleASCIIModeNotification` | `"nascii"` | 单向 |
| `--getascii` | `SquirrelGetASCIIModeNotification` | `nil` | **请求**（触发应答） |
| （应答） | `SquirrelASCIIModeResponse` | `"ascii"`/`"nascii"` | 实例 → 命令进程 |

注意 `--ascii` 与 `--nascii` **共用同一条通知名**，靠 `object` 字段区分意图（这是分布式通知携带少量载荷的常见手法）。

#### 4.1.2 核心流程

```text
┌─────────────────────────┐         DistributedNotificationCenter          ┌──────────────────────────────┐
│  短命命令进程            │       ┌──────────────────────────────┐          │  常驻输入法进程               │
│  (Squirrel --reload)    │──────▶│ SquirrelReloadNotification    │─────────▶│  AppDelegate.rimeNeedsReload │
│                         │       │ SquirrelSyncNotification      │          │  AppDelegate.rimeNeedsSync   │
│  发完即退出 (return)     │       │ SquirrelToggleASCIIMode...    │          │  AppDelegate.rimeToggleASCII │
│                         │       │ SquirrelGetASCIIMode...       │          │  AppDelegate.rimeGetASCIIMode│
└─────────────────────────┘       └──────────────────────────────┘          └──────────────────────────────┘
```

关键设计取舍：

1. **命名一致性**：发送方与接收方用的是**同一个字符串字面量**（如 `"SquirrelReloadNotification"`），没有任何编译期常量约束，拼错则静默失效。这是分布式通知最大的脆弱点。
2. **生命周期解耦**：发送方不关心有没有接收方——`postNotificationName` 是「发出去就不管」的语义，即使没有常驻实例也不会报错。

#### 4.1.3 源码精读

发送端集中在 `Main.swift` 的命令分支里，每个分支发完通知立即 `return true`：

[Main.swift:37-39](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L37-L39) 是 `--reload` 的全部实现——只有一行 `postNotificationName`，发完 `return true` 让外层 `autoreleasepool` 判定 `handled = true`，进程随后退出。

```swift
case "--reload":
  DistributedNotificationCenter.default().postNotificationName(.init("SquirrelReloadNotification"), object: nil)
  return true
```

接收端在 AppDelegate 的 `addObservers()` 里集中注册：

[SquirrelApplicationDelegate.swift:230-243](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L230-L243) 是整个接收端的总装线：先注册工作区关机通知，再逐条注册四条分布式通知（分别绑定到四个回调方法），最后注册输入源切换通知。

```swift
let notifCenter = DistributedNotificationCenter.default()
notifCenter.addObserver(forName: .init("SquirrelReloadNotification"), object: nil, queue: nil, using: rimeNeedsReload)
notifCenter.addObserver(forName: .init("SquirrelSyncNotification"), object: nil, queue: nil, using: rimeNeedsSync)
notifCenter.addObserver(forName: .init("SquirrelToggleASCIIModeNotification"), object: nil, queue: nil, using: rimeToggleASCIIMode)
notifCenter.addObserver(forName: .init("SquirrelGetASCIIModeNotification"), object: nil, queue: nil, using: rimeGetASCIIMode)
```

`addObservers()` 在 `applicationWillFinishLaunching` 中被调用（[SquirrelApplicationDelegate.swift:58-62](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L58-L62)），即常驻实例启动早期就挂好「天线」。对应的，App 退出时在 `applicationWillTerminate` 里 `removeObserver(self)` 拆掉两条通知中心（[SquirrelApplicationDelegate.swift:64-73](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L64-L73)），避免悬挂回调。

#### 4.1.4 代码实践

**实践目标**：用源码验证「发送方与接收方用同一字符串字面量」这一隐含约定，体会其脆弱性。

**操作步骤**：

1. 在 [Main.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift) 中搜索所有 `postNotificationName(.init("Squirrel` 出现的位置，记录每条通知名字面量与所在 case。
2. 在 [SquirrelApplicationDelegate.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift) 中搜索所有 `addObserver(forName: .init("Squirrel`，记录每条注册的名字字面量与绑定的回调。
3. 列一张配对表：发送方 case ↔ 通知名 ↔ 接收回调。

**需要观察的现象**：两侧字符串必须**逐字符相等**（大小写敏感），任何一侧笔误（例如把 `Squirrel` 写成 `Squrriel`）都会让通知静默丢失，且没有任何编译错误提示。

**预期结果**：四条通知（Reload / Sync / ToggleASCIIMode / GetASCIIMode）在发送端与接收端各出现一次，名字完全一致；`SquirrelASCIIModeResponse` 只在接收端的应答路径出现（见 4.3）。

> 待本地验证：此为静态阅读型实践，无需运行，但若你有 macOS 环境，可在两个终端分别 `log stream` 与 `Squirrel --reload` 观察日志中是否出现 `Reloading rime on demand.`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Squirrel 不直接让短命命令进程调用常驻实例的方法，而要绕一圈分布式通知？

**参考答案**：两者是不同的进程，Swift 对象指针不跨进程有效；分布式通知是 macOS 提供的跨进程通信原语，发送方无需拿到接收方对象即可投递消息，且发送方发完即退出，天然解耦。

**练习 2**：若有人把发送端的 `"SquirrelSyncNotification"` 误写成 `"SquirrelSync"`，会发生什么？

**参考答案**：命令仍会执行并退出（不报错），但常驻实例因名字不匹配收不到通知，`--sync` 表面上成功实则失效——这正是分布式通知「拼错即静默失效」的脆弱点，故需要两侧字符串严格一致。

---

### 4.2 reload / sync：重新部署与同步用户数据

#### 4.2.1 概念说明

`--reload` 与 `--sync` 是两条**单向**（fire-and-forget）命令：命令进程发完通知就退出，不关心结果。它们的语义对应 librime 的两个维护操作：

- **reload（重新部署）**：用户改了 `~/Library/Rime` 下的方案、词库或 `squirrel.yaml` 后，需要让引擎重新编译。Squirrel 的实现是**彻底关停引擎再重启**，确保所有配置被重新读取。这与 `--build`（在 Main.swift 里用独立的 `deployer_initialize`+`deploy` 做离线编译，[Main.swift:70-77](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L70-L77)）不同——`--build` 不重启常驻实例。
- **sync（同步用户数据）**：把用户词库与同步目录（如 U 盘、网盘）双向同步，调用 librime 的 `sync_user_data()`。

#### 4.2.2 核心流程

```text
Squirrel --reload
   │ (进程退出)
   ▼ 分布式广播 SquirrelReloadNotification
AppDelegate.rimeNeedsReload(_:)          ← [SquirrelApplicationDelegate.swift:404-407]
   │ 调用 self.deploy()
   ▼
deploy()                                  ← [SquirrelApplicationDelegate.swift:81-86]
   ├─ shutdownRime()     关 config、rimeAPI.finalize()
   ├─ startRime(fullCheck: true)   initialize + start_maintenance(true) + 部署 squirrel.yaml
   └─ loadSettings()     重新打开 base config、重载面板主题
```

`deploy()` 的三步顺序（shutdown → start → loadSettings）刻意复用了正常启动链（`setupRime → startRime → loadSettings`，见 u1-l4），区别仅在于 `startRime` 传入 `fullCheck: true`，强制完整重新检查所有方案。

#### 4.2.3 源码精读

接收回调极其简短，仅做一行委托：

[SquirrelApplicationDelegate.swift:404-407](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L404-L407) 把 `SquirrelReloadNotification` 映射到 `deploy()`：

```swift
func rimeNeedsReload(_: Notification) {
  print("Reloading rime on demand.")
  self.deploy()
}
```

`deploy()` 本身是「关停—重启—重载」三段式（[SquirrelApplicationDelegate.swift:81-86](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L81-L86)）：

```swift
func deploy() {
  print("Start maintenance...")
  self.shutdownRime()
  self.startRime(fullCheck: true)
  self.loadSettings()
}
```

`sync` 路径完全同构：[SquirrelApplicationDelegate.swift:409-412](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L409-L412) 把 `SquirrelSyncNotification` 映射到 `syncUserData()`，后者（[SquirrelApplicationDelegate.swift:88-91](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L88-L91)）只调用一次 `rimeAPI.sync_user_data()`：

```swift
func rimeNeedsSync(_: Notification) {
  print("Sync rime on demand.")
  self.syncUserData()
}
```

#### 4.2.4 代码实践

**实践目标**：对比 `--reload` 与菜单「重新部署」是否走同一条 `deploy()` 路径。

**操作步骤**：

1. 读 [SquirrelInputController.swift:257-259](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L257-L259) 中 `@objc func deploy()` 菜单项的动作。
2. 读 [SquirrelInputController.swift:231-255](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L231-L255) 的 `menu()`，确认菜单项与 `#selector(deploy)` 的绑定。

**需要观察的现象**：状态栏菜单里的「重新部署（Deploy）」项，其 target 是当前会话的 controller，但动作体 `NSApp.squirrelAppDelegate.deploy()` 调用的是**同一个** `deploy()` 方法。

**预期结果**：无论从命令行 `--reload` 还是菜单点击「重新部署」，最终都汇聚到 AppDelegate 的 `deploy()`——这是把「外部触发」与「UI 触发」收敛到单一执行路径的良好设计。`--sync` 与菜单「Sync user data」同理（`syncUserData()`，[SquirrelInputController.swift:261-263](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L261-L263)）。

> 待本地验证：在有 librime 环境的 macOS 上，执行 `Squirrel --reload` 后观察日志应依次出现 `Start maintenance...`、`Initializing la rime...`。

#### 4.2.5 小练习与答案

**练习 1**：`deploy()` 为什么要先 `shutdownRime()` 再 `startRime()`，而不是直接调 librime 的某个「热重载」API？

**参考答案**：librime 的配置（方案、词库、squirrel.yaml）在 `initialize` 时读取，运行期改动需 `finalize` 释放引擎、重新 `initialize` 才能确保全部重读；`fullCheck: true` 强制 `start_maintenance` 完整检查所有方案是否需要重新编译。先关停再重启是最稳妥的「全量重载」。

**练习 2**：`--reload`（重启常驻实例的引擎）和 `--build`（离线编译）有何区别？

**参考答案**：`--reload` 通过分布式通知让**已运行的常驻实例**关停并重启自己的引擎；`--build` 则在**短命命令进程内**用独立的 `deployer_initialize`/`deploy` 编译方案（[Main.swift:70-77](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L70-L77)），不触碰常驻实例。前者影响正在输入的会话，后者不影响。

---

### 4.3 ASCII toggle/get 协议：请求-应答与本地通知转发

#### 4.3.1 概念说明

ASCII 模式（`ascii_mode`）是 librime session 上的一个布尔 option，它决定了按键是走「中文转换」还是「直接输出西文字符」。真正持有 session 的是会话级的 `SquirrelInputController`，而分布式通知的接收方是 App 级的 AppDelegate。这造成一个层级错配：

- AppDelegate 能收到分布式通知，但**不直接持有任何 session**。
- `SquirrelInputController` 持有 session，但**听不到跨进程的分布式通知**（且可能有多个会话实例）。

Squirrel 的解法是**两级转发**：

1. **toggle（切换）**：分布式通知 → AppDelegate → 进程内本地通知 → controller 操作 session。这是单向命令，不需要应答。
2. **get（查询）**：在 toggle 之上再叠加一层应答——命令进程发出查询后**不立即退出**，而是等一个 `SquirrelASCIIModeResponse` 分布式通知回送结果。

#### 4.3.2 核心流程

**toggle（`--ascii` / `--nascii`）单向链：**

```text
Squirrel --ascii   (object: "ascii")
   │ 退出
   ▼ 分布式: SquirrelToggleASCIIModeNotification  (object="ascii")
AppDelegate.rimeToggleASCIIMode(_)        ← [SquirrelApplicationDelegate.swift:414-423]
   │  enableASCII = (mode == "ascii")
   ▼ 进程内本地: SquirrelSetASCIIModeNotification  (object: Bool)
SquirrelInputController.handleASCIIModeToggle(_)   ← [SquirrelInputController.swift:593-599]
   │  rimeAPI.set_option(session, "ascii_mode", enableASCII)
   └─ rimeUpdate()   刷新面板/状态栏
```

**get（`--getascii`）请求-应答往返：**

```text
命令进程                                   常驻实例
   │                                         │
   │ ① 先挂监听 SquirrelASCIIModeResponse     │
   │◄────────────────────────────────────────│ (回调提前就绪)
   │                                         │
   │ ② 分布式: SquirrelGetASCIIModeNotification
   │────────────────────────────────────────►│
   │                                         │ AppDelegate.rimeGetASCIIMode(_)
   │                                         │   ← [SquirrelApplicationDelegate.swift:425-427]
   │                                         │ ③ 进程内: SquirrelReportASCIIModeNotification
   │                                         │────────────────────────────────────────────►│
   │                                         │                          SquirrelInputController.reportASCIIMode(_)
   │                                         │                          ← [SquirrelInputController.swift:601-612]
   │                                         │ ④ get_option(session,"ascii_mode") → "ascii"/"nascii"
   │                                         │ ⑤ 分布式: SquirrelASCIIModeResponse (object=status)
   │◄────────────────────────────────────────│
   │ ⑥ 命令进程的 observer 触发, responseReceived=true
   │ ⑦ print(asciiStatus)  → 输出 "ascii" 或 "nascii"
```

#### 4.3.3 源码精读

**命令进程侧（`--getascii`）** 是整个协议里最复杂的代码，因为要同步等待一个异步回送的通知。看 [Main.swift:87-111](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L87-L111)：

```swift
case "--getascii":
  var responseReceived = false
  var asciiStatus = ""
  let observer = DistributedNotificationCenter.default().addObserver(
    forName: .init("SquirrelASCIIModeResponse"), object: nil, queue: .main
  ) { notification in
    if let status = notification.object as? String {
      asciiStatus = status
      responseReceived = true
    }
  }
  DistributedNotificationCenter.default().postNotificationName(.init("SquirrelGetASCIIModeNotification"), object: nil)
  let timeout = Date().addingTimeInterval(2.0)
  while !responseReceived && Date() < timeout {
    RunLoop.current.run(until: Date().addingTimeInterval(0.01))
  }
  DistributedNotificationCenter.default().removeObserver(observer)
  if responseReceived {
    print(asciiStatus)
  } else {
    print("nascii")
  }
  return true
```

这里有三个细节值得注意：

1. **先挂监听，再发请求**（行 90 先 `addObserver`，行 100 才 `post`）——避免应答比监听就绪更早到达而错过。
2. **用 `RunLoop.current.run(until:)` 轮询**（行 102-104）——分布式通知在主队列（`queue: .main`）投递，必须让主线程的 RunLoop 转起来，回调才有机会执行；`while` 循环不断「泵」RunLoop 直到收到应答或 2 秒超时。
3. **超时降级**（行 106-110）——若 2 秒内没收到应答（比如没有常驻实例、或 session 不存在），默认打印 `"nascii"`，保证命令总会有输出。

**AppDelegate 的转发层**很薄，把分布式通知「翻译」成本地通知。[SquirrelApplicationDelegate.swift:414-423](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L414-L423) 处理 toggle——把 `object` 字符串 `"ascii"`/`"nascii"` 翻译成 `Bool`：

```swift
func rimeToggleASCIIMode(_ notification: Notification) {
  guard let mode = notification.object as? String else { return }
  let enableASCII = mode == "ascii"
  if enableASCII {
    NotificationCenter.default.post(name: .init("SquirrelSetASCIIModeNotification"), object: true)
  } else {
    NotificationCenter.default.post(name: .init("SquirrelSetASCIIModeNotification"), object: false)
  }
}
```

`rimeGetASCIIMode` 更简单，只做一次纯转发（[SquirrelApplicationDelegate.swift:425-427](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L425-L427)）：

```swift
func rimeGetASCIIMode(_: Notification) {
  NotificationCenter.default.post(name: .init("SquirrelReportASCIIModeNotification"), object: nil)
}
```

**controller 侧**在 `init` 时注册两条本地通知（[SquirrelInputController.swift:188-208](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L188-L208)）：

```swift
NotificationCenter.default.addObserver(
  forName: .init("SquirrelSetASCIIModeNotification"), object: nil, queue: nil
) { [weak self] notification in
  self?.handleASCIIModeToggle(notification)
}
NotificationCenter.default.addObserver(
  forName: .init("SquirrelReportASCIIModeNotification"), object: nil, queue: nil
) { [weak self] notification in
  self?.reportASCIIMode(notification)
}
```

执行体 `handleASCIIModeToggle` 直接把布尔值写进 session（[SquirrelInputController.swift:593-599](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L593-L599)）：

```swift
private func handleASCIIModeToggle(_ notification: Notification) {
  guard let enableASCII = notification.object as? Bool else { return }
  guard session != 0 && rimeAPI.find_session(session) else { return }
  rimeAPI.set_option(session, "ascii_mode", enableASCII)
  rimeUpdate()
}
```

而 `reportASCIIMode` 把 session 的 `ascii_mode` 读出来，**反向**通过分布式通知回送给命令进程（[SquirrelInputController.swift:601-612](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L601-L612)）：

```swift
private func reportASCIIMode(_: Notification) {
  guard client != nil else { return }
  guard session != 0 && rimeAPI.find_session(session) else { return }
  let isASCIIMode = rimeAPI.get_option(session, "ascii_mode")
  let status = isASCIIMode ? "ascii" : "nascii"
  DistributedNotificationCenter.default().postNotificationName(
    .init("SquirrelASCIIModeResponse"), object: status
  )
}
```

注意 `reportASCIIMode` 里有 `guard client != nil`——只有当前正接管某个文本框的 controller 才会应答，避免多个闲置会话同时回送造成混乱。

#### 4.3.4 代码实践

**实践目标**：亲手画出 `--getascii` 的完整请求-应答时序，并理解两级通知转发。

**操作步骤**：

1. 在三张纸/三个文件里分别列出三个角色持有的通知监听：
   - **Main.swift `--getascii`**：监听 `SquirrelASCIIModeResponse`（分布式），发送 `SquirrelGetASCIIModeNotification`（分布式）。
   - **AppDelegate**：监听 `SquirrelGetASCIIModeNotification`（分布式），发送 `SquirrelReportASCIIModeNotification`（本地）。
   - **SquirrelInputController**：监听 `SquirrelReportASCIIModeNotification`（本地），发送 `SquirrelASCIIModeResponse`（分布式）。
2. 按时间顺序标注每一步用到的通知中心类型（**分布式**还是**本地**），并画出消息箭头。
3. 回答：为什么 toggle 链只有两级（分布式→本地），而 get 链是三级往返（分布式→本地→分布式）？

**需要观察的现象**：消息在「跨进程（分布式）」与「进程内（本地）」两种通道间来回切换；AppDelegate 始终是分布式通知的入口/出口，controller 始终是 session 的唯一操作者。

**预期结果**：你应当得到与 4.3.2「核心流程」一致的时序图。核心洞察是——**AppDelegate 充当「跨进程网关」，controller 充当「session 代理」**，本地通知是连接两者的进程内桥梁。

**进阶操作（源码阅读型）**：在 [SquirrelInputController.swift:601-612](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L601-L612) 的 `reportASCIIMode` 里，假设把 `guard client != nil` 这一行删掉，思考会有什么后果（提示：闲置会话、多应答）。

> 待本地验证：在 macOS 上对正在输入的 App 执行 `/Library/Input\ Methods/Squirrel.app/Contents/MacOS/Squirrel --getascii`，应秒级返回 `ascii` 或 `nascii`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `--getascii` 必须先 `addObserver` 再 `post`，且要手动 `RunLoop.run`？

**参考答案**：分布式通知在主队列异步投递，必须先挂好监听才不会错过应答；而要让主队列的回调得以执行，主线程的 RunLoop 必须运转，故用 `while + RunLoop.current.run(until:)` 主动「泵」事件，直到收到应答或 2 秒超时。

**练习 2**：toggle（`--ascii`）为什么不需要应答，而 get（`--getascii`）需要？

**参考答案**：toggle 是「设定」语义，命令进程发完即退出，不关心执行结果；get 是「查询」语义，命令进程必须把结果打印出来，故需要常驻实例回送一个 `SquirrelASCIIModeResponse`，命令进程阻塞等待。

**练习 3**：若当前没有任何 App 正接管 Squirrel（即所有 controller 的 `client` 都为 nil），`--getascii` 会输出什么？

**参考答案**：因为所有 `reportASCIIMode` 都被 `guard client != nil` 挡住，不会有任何 `SquirrelASCIIModeResponse` 发出，命令进程等满 2 秒超时，走降级分支打印 `"nascii"`（[Main.swift:106-110](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L106-L110)）。

---

### 4.4 kTISNotifySelectedKeyboardInputSourceChanged 与悬挂组合兜底

#### 4.4.1 概念说明

这是本讲最后、也是最「系统级」的一条通知。`kTISNotifySelectedKeyboardInputSourceChanged` 是 macOS Carbon 框架（Text Input Source，TIS）在**用户切换输入源**时广播的系统级分布式通知——无论你是从菜单栏切换，还是被 `macism`、`Input Source Pro` 这类第三方工具用 `TISSelectInputSource()` 程序化切换，系统都会发出它。

Squirrel 监听它的初衷很简单：**当前激活的是不是 Squirrel？是就显示状态栏图标，不是就隐藏**（`updateStatusItemVisibility`）。

但它后来被赋予了一个更重要的「兜底」职责——**悬挂组合（stranded composition）清理**。背景是 2025 年 macOS 26 引入的一个回归（见 [SquirrelApplicationDelegate.swift:370-376](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L370-L376) 的注释，对应 PR #1140/#1142）：

> 当其他进程通过 `TISSelectInputSource()` 把输入源从 Squirrel 切走时，macOS 26 **不再调用** `deactivateServer`。结果用户已经敲到一半的候选词组合被「悬挂」——既不上屏也不消失，候选面板孤零零留在屏幕上。

Squirrel 的对策：既然 `kTISNotifySelectedKeyboardInputSourceChanged` 这条通知仍然会送达，就在它的回调里**手动补做一次 `deactivateServer` 该做的事**。

#### 4.4.2 核心流程

```text
系统切换输入源（菜单 / macism / Input Source Pro）
   │
   ▼ 分布式: kTISNotifySelectedKeyboardInputSourceChanged  (queue: .main)
AppDelegate 回调                                ← [SquirrelApplicationDelegate.swift:239-242]
   ├─ updateStatusItemVisibility()
   │     └─ currentInputSourceID 以 "im.rime.inputmethod.Squirrel" 开头?
   │         是 → statusItem.isVisible = true   否 → false
   └─ finalizeStrandedComposition()              ← [SquirrelApplicationDelegate.swift:377-383]
         ├─ 当前输入源是 Squirrel? 是 → 直接 return（无需兜底）
         └─ 否 → inputController.deactivateServer(inputController.client)
                （手动补做：隐藏面板 + 提交原始输入 + 置空 client）
```

关键判定：只有当当前输入源**切走**（不再是 Squirrel）时才补做 `deactivateServer`；若切回 Squirrel，则什么都不做（此时本就有正常的 `activateServer` 流程）。

#### 4.4.3 源码精读

注册处——注意这条通知指定了 `queue: .main`，而前面四条自定义通知用的是 `queue: nil`（[SquirrelApplicationDelegate.swift:239-242](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L239-L242)）：

```swift
notifCenter.addObserver(forName: .init(kTISNotifySelectedKeyboardInputSourceChanged as String), object: nil, queue: .main) { [weak self] _ in
  self?.updateStatusItemVisibility()
  self?.finalizeStrandedComposition()
}
```

指定主队列是因为后续要触碰 UI（状态栏图标、面板）与 controller，必须在主线程。

状态栏显隐逻辑（[SquirrelApplicationDelegate.swift:364-368](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L364-L368)）：

```swift
func updateStatusItemVisibility() {
  guard let statusItem = statusItem else { return }
  let currentInputSourceID = SquirrelInstaller.currentInputSourceID() ?? ""
  statusItem.isVisible = currentInputSourceID.hasPrefix("im.rime.inputmethod.Squirrel")
}
```

悬挂组合兜底（[SquirrelApplicationDelegate.swift:377-383](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L377-L383)）：

```swift
func finalizeStrandedComposition() {
  let currentInputSourceID = SquirrelInstaller.currentInputSourceID() ?? ""
  guard !currentInputSourceID.hasPrefix("im.rime.inputmethod.Squirrel") else { return }
  if let inputController = panel?.inputController {
    inputController.deactivateServer(inputController.client)
  }
}
```

它复用了会话级 controller 的 `deactivateServer`（见 u2-l3）——后者依次 `hidePalettes` → `commitComposition` → 置空 `client`（[SquirrelInputController.swift:210-214](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L210-L214)），正是「把半截组合安全收尾」所需的三件事。注意它通过 `panel?.inputController` 反向拿到当前 controller——面板持有 controller 的反向引用（在 `showPanel` 里设置，[SquirrelInputController.swift:585-589](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L585-L589)）。

注释明确说明：经菜单栏正常切换时，系统**会**先调 `deactivateServer`，于是这里的补做变成无副作用的 no-op；只有程序化切换（macOS 26 不调 `deactivateServer`）时才真正兜底（[SquirrelApplicationDelegate.swift:370-376](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L370-L376)）。

#### 4.4.4 代码实践

**实践目标**：理解「兜底」为什么必须做成幂等（可重复执行而无副作用）。

**操作步骤**：

1. 读 [SquirrelInputController.swift:210-214](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L210-L214) 的 `deactivateServer`，列出它做的三件事。
2. 读 [SquirrelInputController.swift:221-229](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L221-L229) 的 `commitComposition`，理解它在 `session != 0` 且有 `get_input` 时才上屏。
3. 思考：菜单栏切换时，系统先调了一次 `deactivateServer`，随后 `finalizeStrandedComposition` 又调一次，为什么不会「双倍上屏」？

**需要观察的现象**：`deactivateServer` 内部对 `client` 与 `session` 都有隐含的空值/有效性守卫，重复调用时第二次往往找不到待提交的输入（已被第一次 `clear_composition` 清空），自然成为 no-op。

**预期结果**：兜底代码的安全性来自「查询当前输入源」+「`deactivateServer` 本身的幂等性」双重保险。这正是它敢挂在每条输入源切换通知上的原因。

> 待本地验证：在 macOS 26 + macism 环境下，输入到一半候选词时用 macism 切走输入源，观察候选面板是否被收起、半截编码是否上屏（修复前会悬挂，修复后正常收尾）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `finalizeStrandedComposition` 要先判断「当前输入源不是 Squirrel」才补做 `deactivateServer`？

**参考答案**：若当前输入源仍是 Squirrel（用户切回 Squirrel），说明输入法仍激活，半截组合应保留，此时补做收尾反而是破坏；只有切走（不再是 Squirrel）时才需要把悬挂组合收尾。这也避免切回 Squirrel 时误清空正在进行的输入。

**练习 2**：这条通知为什么显式指定 `queue: .main`，而前面四条自定义分布式通知用 `queue: nil`？

**参考答案**：回调里要操作状态栏图标（UI）与 controller（通常需主线程访问），故强制主队列；前面四条回调大多是「关停/重启引擎」「转发本地通知」等可在任意线程进行的逻辑，或其内部本身会 `DispatchQueue.main.async` 调度（如 `updateStatusIcon`），故用 `nil` 让系统选队。指定 `.main` 是为了确保 UI 相关副作用线程安全。

---

## 5. 综合实践

**任务**：为 Squirrel 设计并追踪一条**全新的**分布式命令 `--toggle-sync-state`（假设语义：查询并打印用户词库最近一次同步是否成功），参照本讲四类通知的实现范式，写出它在三处的代码骨架，并标注每一步用的通知中心类型。

要求：

1. 在 `Main.swift` 仿照 [Main.swift:87-111](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L87-L111) 的 `--getascii` 写出命令端：先挂监听 `SquirrelSyncStateResponse`，再发 `SquirrelGetSyncStateNotification`，RunLoop 等待 2 秒，超时降级打印 `"unknown"`。
2. 在 `SquirrelApplicationDelegate.addObservers()` 仿照 [SquirrelApplicationDelegate.swift:238](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L238) 注册 `SquirrelGetSyncStateNotification`，回调转发一条本地通知 `SquirrelReportSyncStateNotification`。
3. 思考：这条命令应放在哪一层执行实际的 `rimeAPI.sync_user_data()` 与状态读取？为什么不能直接在 AppDelegate 里读（提示：session 归属、是否需要应答）？

**评判标准**：

- 正确区分请求-应答（get 型）与单向（reload/sync 型）两种范式。
- 正确标注每一步是「分布式」还是「本地」通知。
- 能指出「状态读取如果依赖 session，则必须转发到 controller 层；若只依赖引擎全局状态，则 AppDelegate 可直接读后回送」——这考察你对「session 归属」与「两级转发」是否真正理解。

**预期产出**：一张与 4.3.2 类似的时序图，加上三段代码骨架（标注「示例代码」）。这是把本讲四类通知融会贯通的检验。

> 说明：本任务为源码阅读 + 设计型实践，不要求真正编译运行；重点是检验你对「单二进制双身份、两级通知转发、请求-应答协议」三条主线的掌握。

## 6. 本讲小结

- **单二进制双身份**：同一个 `Squirrel` 可执行文件，命令行命中维护命令就发分布式通知后退出，否则作为常驻输入法跑 `app.run()`。
- **四条自定义分布式通知**（`SquirrelReloadNotification` / `SyncNotification` / `ToggleASCIIModeNotification` / `GetASCIIModeNotification`）是命令进程→常驻实例的跨进程桥梁，靠字符串字面量匹配，拼错即静默失效。
- **reload/sync 是单向命令**：AppDelegate 接收后直接 `deploy()`（关停—重启—重载三段式）或 `syncUserData()`，与菜单项收敛到同一路径。
- **ASCII toggle 是两级转发**：分布式（`object="ascii"`/`"nascii"`）→ AppDelegate 翻译成 `Bool` → 本地通知 → controller 写 `ascii_mode` option。
- **ASCII get 是三级请求-应答往返**：分布式请求 → 本地转发 → controller 读 session 后用分布式回送 `SquirrelASCIIModeResponse`；命令进程 `RunLoop` 泵事件等待，2 秒超时降级为 `"nascii"`。
- **`kTISNotifySelectedKeyboardInputSourceChanged` 兼做两职**：状态栏图标显隐 + macOS 26 程序化切走时悬挂组合的 `finalizeStrandedComposition` 兜底（PR #1140/#1142），依赖 `deactivateServer` 的幂等性保证无副作用。

## 7. 下一步学习建议

本讲把「外部命令如何驱动常驻实例」讲透了，接下来的学习方向：

- **u5-l2 输入源注册（TIS）**：本讲多次引用 `SquirrelInstaller.currentInputSourceID()` 与输入源 ID 前缀 `im.rime.inputmethod.Squirrel`，下一讲深入 `register/enable/disable/select` 的 TIS 流程，理解这些 ID 从何而来。
- **u5-l3 保留属性：插件→前端协调**：本讲 4.4 提到 `panel?.inputController` 的反向引用与 `deactivateServer`，下一讲讲 librime 插件如何通过 `notificationHandler` 的 `property` 消息（另一种「外部→前端」通道）协调 UI。
- **重读 u2-l3 输入控制器生命周期**：带着本讲对 `deactivateServer` 幂等性的理解，回去重看「悬挂组合」收尾的三步，体会「安全网」设计。
- **延伸阅读**：`DistributedNotificationCenter` 的投递语义（`postNotificationName:object:suspendedBehavior:`）、Carbon TIS 框架的 `kTISNotify*` 常量族，是理解 macOS 输入法系统级集成的基础。
