# 输入控制器生命周期与会话

## 1. 本讲目标

本讲是「输入处理主链路」的第三讲。在上一讲里，我们认识了应用委托 `SquirrelApplicationDelegate` 这个「管家」：它持有 librime 引擎、前端配置、候选面板等 App 级全局资源。本讲我们要走进真正的「一线员工」——输入控制器 `SquirrelInputController`。

学完本讲，你应当能够：

- 说清 `SquirrelInputController` 与 librime `session` 之间的一一对应关系，并解释「会话（session）」在输入法里的含义。
- 描述一个控制器从「创建 → 激活 → 处理按键 → 失活 → 销毁」的完整生命周期，并指出每个阶段该做哪些初始化与清理。
- 解释为什么 `client` 必须用 `weak` 弱引用，以及为什么 `deactivateServer(_:)` 必须依次做「隐藏面板 → 提交原始输入 → 置空 client」三件事，才能避免「悬挂组合（stranded composition）」。
- 理解 `commitComposition(_:)` 作为「安全网」的作用：在系统要求收尾时，把尚未转换的原始按键原样上屏。

本讲只聚焦「生命周期与会话管理」这一条线，**不**展开按键事件主循环（那是 u2-l4）、按键映射（u2-l5）、数据消费（u2-l6）的细节。我们先把控制器「何时生、何时灭、生灭之间要做什么」这条骨架搭牢，后续几讲再往骨架上挂血肉。

## 2. 前置知识

在进入源码前，先用通俗语言把几个关键概念讲清楚。这些概念在 u1-l5（IMK 基础）和 u2-l1（应用委托）里已经建立，这里做最简短的回顾与补充。

### 2.1 controller 与 client

- **controller（控制器）**：即 `SquirrelInputController`，是输入法「这一侧」的对象，负责接收键盘事件、驱动引擎、画面板。它继承自 IMK 框架的 `IMKInputController`。
- **client（客户端）**：即 `IMKTextInput` 协议的对象，代表「另一侧」——当前接收文字的目标应用里的那个文本输入框（比如备忘录、浏览器地址栏、终端）。输入法最终要把字「上屏」，就是通过 `client` 调用 `insertText` / `setMarkedText`。

一个直观的比喻：controller 是「译员」，client 是「需要翻译服务的客人」。译员为客人服务，但客人的生灭不归译员管——客人随时可能离开（用户切换了输入焦点），所以译员对客人只能「弱引用」。

### 2.2 session（会话）

librime 引擎用「会话」来隔离不同输入上下文的状态（当前输入了什么码、选中了哪个候选、处于什么输入方案）。每个会话由一个 `RimeSessionId`（本质是个整数句柄）标识。

Squirrel 的设计是：**一个 `SquirrelInputController` 实例对应一个 librime session**。controller 被创建时就 `create_session()` 拿到一个 session id，controller 被销毁时就 `destroy_session()` 释放它。这是一一对应、配对管理的关系。

### 2.3 marked text 与 commit text

这是 Cocoa 文本输入协议的两种操作（u1-l5 已讲）：

- **marked text（标记文本 / 预编辑文本）**：`setMarkedText`，带下划线的临时文本，用户还能继续改。比如拼音输入 `nihao` 时显示的「你好」候选预览。
- **commit text（提交文本 / 上屏文本）**：`insertText`，最终确定、写入目标应用的文本。

### 2.4 生命周期与「悬挂组合」

输入法的一个经典难题是「悬挂组合」：用户在某个文本框里敲了一半拼音（已经形成了组合 composition，但还没选词上屏），此时如果输入焦点突然离开（比如用户点了别的窗口），输入法手里这段「半成品」该怎么办？

如果什么都不做，这段半成品可能丢失，或者卡在面板上；如果处理不当，还可能把字符错误地插入到下一个目标应用里。`deactivateServer(_:)` 和 `commitComposition(_:)` 就是为了在「失活」这个时刻干净地收尾。

### 2.5 weak 引用与 `?=` 运算符

Swift 里 `weak var` 声明的引用不会阻止被引用对象被释放——对象没了，weak 引用自动变成 `nil`。项目自定义了一个 `?=` 运算符（定义在 `BridgingFunctions.swift`）：

```swift
infix operator ?= : AssignmentPrecedence
func ?=<T>(left: inout T, right: T?) {
  if let right = right {
    left = right
  }
}
```

它的语义是「**只有右边非 nil 时才赋值**」。输入法频繁收到 `sender`（可能为 nil 的 client），用 `self.client ?= sender as? IMKTextInput` 可以在 sender 有效时刷新引用、无效时保留旧值，避免误把好端端的 client 清成 nil。这个运算符在 u5-l4 桥接约定里会详讲，本讲只要会用、能读懂即可。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `sources/SquirrelInputController.swift` | 输入控制器，IMK 框架与 librime 之间的桥梁 | 生命周期方法、session 管理、client 弱引用 |
| `sources/BridgingFunctions.swift` | Swift/C 桥接工具 | `?=` 运算符、`NSRange.empty`（仅在解释时引用） |

`SquirrelInputController.swift` 全文约 615 行，但生命周期相关的代码集中在几处：类顶部的属性声明（`client`、`session`）、四个生命周期方法（`init` / `activateServer` / `deactivateServer` / `deinit`）、以及两个私有辅助方法（`createSession` / `destroySession`）。本讲会逐段精读这些地方。

## 4. 核心概念与源码讲解

### 4.1 init 与 createSession：控制器的诞生

#### 4.1.1 概念说明

当 macOS 的 IMK 框架认为需要一个新的输入上下文时（典型场景：用户把光标点进一个新的文本输入框），它会创建一个 `SquirrelInputController` 实例。创建时，IMK 会调用这个控制器的**指定初始化器** `init(server:delegate:client:)`，把三项关键信息传进来：

- `server`：`IMKServer`，输入法服务端点（u1-l5 已讲，连接名 `Squirrel_Connection`）。
- `delegate`：委托对象。
- `client`：`IMKTextInput`，也就是「客人」——当前文本框。

Squirrel 在这个初始化器里要做两件最重要的事：

1. **保存 client 的弱引用**，并调用父类初始化器。
2. **立刻创建一个 librime session**，让这个控制器和引擎的状态一一绑定。

#### 4.1.2 核心流程

控制器的「诞生」可以用下面的伪代码描述：

```
IMK 创建 SquirrelInputController
  └─ init(server, delegate, client)
       ├─ self.client = client as? IMKTextInput   // 弱引用，保存客人
       ├─ super.init(...)                          // 必须先调父类
       ├─ createSession()                          // 创建 librime session
       │    ├─ 取 client 的 bundleIdentifier（未知则生成 UnknownApp 名）
       │    ├─ session = rimeAPI.create_session()  // 向引擎申请会话
       │    └─ updateAppOptions()                  // 应用 app_options
       └─ 注册两个本地通知观察者
            ├─ SquirrelSetASCIIModeNotification  → handleASCIIModeToggle
            └─ SquirrelReportASCIIModeNotification → reportASCIIMode
```

注意顺序：**必须先 `super.init`，再 `createSession()`**。因为 `createSession()` 内部会用到已初始化完成的实例状态（如 `client`、`session`），而 Swift 要求在父类初始化完成前不能使用 `self`。

#### 4.1.3 源码精读

先看类顶部的关键属性声明：

[sources/SquirrelInputController.swift:14-20](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L14-L20)：声明了 `weak var client`（弱引用的客人）、`rimeAPI`（librime API 句柄）、`session`（librime 会话 id，初值 0 表示「尚无会话」）。`session` 的类型是 `RimeSessionId`，本质是个整数，`0` 是「无效会话」的哨兵值。

接着是初始化器本体：

[sources/SquirrelInputController.swift:188-208](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L188-L208)：先把 `client` 转型存为弱引用，调用 `super.init` 完成父类初始化，然后调用 `createSession()` 创建会话，最后注册两个本地通知观察者（用于 ASCII 模式的切换与查询，属于 u5-l1 分布式通知的内容，本讲只需知道「这里埋了两个观察者」）。

再看 `createSession()` 的实现：

[sources/SquirrelInputController.swift:351-364](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L351-L364)：先确定一个「应用名」——优先用 `client?.bundleIdentifier()`（即目标应用的 bundle id，如 `com.apple.Terminal`）；如果拿不到 client（理论上很少见），就用一个静态计数器生成 `UnknownApp1`、`UnknownApp2` 这样的兜底名。然后调用 `rimeAPI.create_session()` 向引擎申请会话，拿到非 0 的 id 存入 `session`，并把 `schemaId` 重置为空（表示方案尚未确定）。最后若 session 创建成功，调用 `updateAppOptions()` 把该应用专属的选项（如 `ascii_mode`、`vim_mode`）应用到这个会话上。

注意第 356 行的 `print("createSession: \(app)")`——这是项目里保留的一条调试日志，实际运行时可以在 Console.app 或终端里看到它，是观察会话创建时机的绝佳线索（实践任务会用到）。

#### 4.1.4 代码实践

**实践目标**：观察 controller 何时被创建、`createSession` 何时被调用、为哪些应用创建会话。

**操作步骤**：

1. 打开 `sources/SquirrelInputController.swift`，定位到第 356 行的 `print("createSession: \(app)")`。
2. 在你的 Mac 上构建并安装 Squirrel（参考 u1-l3 的 `make release` / `make install`），把它设为当前输入法。
3. 打开「控制台」应用（Console.app），在搜索框过滤 `createSession`，或直接在终端运行 Squirrel 进程观察 stdout。
4. 依次把光标点进不同应用（备忘录、Safari 地址栏、终端），每次切换都留意日志。

**需要观察的现象**：每当你把光标点进一个新的可输入区域，日志里应出现一条 `createSession: <bundle id>`。

**预期结果**：你会看到 `createSession: com.apple.Notes`、`createSession: com.apple.Safari` 之类，证实「一个输入焦点 → 一个 controller → 一个 session」的对应关系。

> 待本地验证：实际是否每次焦点切换都重建 controller，取决于 IMK 的复用策略（某些情况下 IMK 会复用已有 controller 并触发 `activateServer` 而非重建）。具体行为以你在本机观察到的为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `session` 的初值要设成 `0`？如果创建会话失败（`create_session` 返回 0），后续代码会发生什么？

**参考答案**：`0` 是 librime 约定的「无效会话」哨兵。如果 `create_session` 失败返回 0，`session` 保持 0，后续 `handle(_:client:)` 里会有 `if session == 0 || !rimeAPI.find_session(session) { createSession() }` 的自愈逻辑（见 4.4.3），尝试重新创建；若仍失败则直接 `return false` 放行事件，不至于崩溃。

**练习 2**：`createSession()` 里为什么要费力气为「未知应用」生成 `UnknownAppN` 这样的名字，而不是直接用空字符串？

**参考答案**：因为应用名会传给 `updateAppOptions()`，用于查 `app_options` 配置表（按 bundle id 定制输入行为，见 u3-l2）。一个非空的、唯一的名字能保证即便在极端情况下，每个会话也有可区分的标识，便于调试日志（`print("createSession: \(app)")`）追踪。

---

### 4.2 activateServer / deactivateServer：激活与失活

#### 4.2.1 概念说明

IMK 框架在控制器的生命周期里会反复调用两个方法：

- **`activateServer(_:)`**：当这个控制器**被激活**时调用——即用户开始在这个文本框里输入，输入法要「上岗」。这时需要做上岗准备：覆盖键盘布局（如果配置了）、重置预编辑文本、刷新状态栏图标。
- **`deactivateServer(_:)`**：当控制器**被失活**时调用——用户离开了这个文本框（焦点转移、切到别的输入法、窗口关闭等）。这时**必须做收尾**，否则就会产生「悬挂组合」。

激活与失活**可以反复发生**多次：同一个 controller 可能被激活→失活→再激活。所以这两个方法里的逻辑必须是「可重复执行、可幂等」的。

#### 4.2.2 核心流程

`activateServer(_:)` 的流程：

```
activateServer(sender)
  ├─ self.client ?= sender as? IMKTextInput        // 刷新客人引用
  ├─ 读取 config 的 keyboard_layout，做归一化：
  │     "last"/""  → 不覆盖（保持系统布局）
  │     "default"  → com.apple.keylayout.ABC
  │     其它无前缀 → 补 com.apple.keylayout. 前缀
  ├─ 若布局非空：client.overrideKeyboard(...)        // 切到指定键盘布局
  ├─ preedit = ""                                   // 重置预编辑文本
  └─ 若 session 有效：读 ascii_mode 状态，刷新状态栏图标
```

`deactivateServer(_:)` 的流程（**本讲的核心**）：

```
deactivateServer(sender)
  ├─ hidePalettes()          // 1. 隐藏候选面板
  ├─ commitComposition(sender)  // 2. 把原始输入上屏（安全网）
  └─ client = nil            // 3. 主动置空弱引用的客人
```

这三步的顺序不能乱、一步不能少，原因见 4.2.4 的实践任务。

#### 4.2.3 源码精读

先看 `activateServer(_:)`：

[sources/SquirrelInputController.swift:167-186](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L167-L186)：第 168 行用 `?=` 稳健刷新 client；第 169-176 行读取全局配置里的 `keyboard_layout` 字符串并做归一化（这是用户在 `squirrel.yaml` 顶层可配的选项，见 u3-l2）；第 177-179 行若布局非空就调用 `client?.overrideKeyboard` 切换键盘——这解释了为什么 Squirrel 能在激活时强制用 ABC 布局，避免某些应用的快捷键冲突；第 180 行重置 `preedit`；第 181-185 行若 session 有效，读出 `ascii_mode` 当前值并刷新菜单栏图标（「中」/「Ａ」）。

再看 `deactivateServer(_:)`：

[sources/SquirrelInputController.swift:210-214](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L210-L214)：短短三行，但每行都不可省。`hidePalettes()` 隐藏面板（见下文），`commitComposition(sender)` 把残留的原始输入提交上屏（见 4.3），`client = nil` 主动丢弃对客人的引用。

[sources/SquirrelInputController.swift:216-219](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L216-L219)：`hidePalettes()` 的实现——隐藏 AppDelegate 持有的那个共享面板，再调父类的 `hidePalettes()`。注意面板是 AppDelegate 级的全局单例（u2-l1 已讲），这里只是「隐藏」而非「销毁」。

#### 4.2.4 代码实践

这就是本讲规格里要求的实践任务。

**实践目标**：理解 `client` 为何必须 `weak`，并解释 `deactivateServer(_:)` 三步收尾为何能避免悬挂组合。

**操作步骤**：

1. 阅读第 14 行 `private weak var client: IMKTextInput?` 与第 210-214 行的 `deactivateServer`。
2. 做如下分析（源码阅读型实践，无需运行）：

   **(a) 为什么 client 必须是 weak？**
   `client`（`IMKTextInput`）代表目标应用里的文本框，它的生命周期由**目标应用和 IMK 框架**共同管理，**不属于输入法**。如果用强引用（`var client`），就会形成一个所有权倒挂：输入法 controller 持有 client，而 client 又（经由 IMK）反过来持有 controller，形成**引用循环（retain cycle）**，两者都无法被释放，造成内存泄漏。声明为 `weak` 后，当 client 真正消失时，引用自动置 nil，controller 不会无意义地保住一个已死的客人。使用前还必须 guard 守卫（如第 552 行 `guard let client = client else { return }`），因为弱引用随时可能变 nil。

   **(b) deactivateServer 的三件事分别解决什么问题？**

   | 步骤 | 调用 | 解决的问题 |
   | --- | --- | --- |
   | 1 | `hidePalettes()` | 把候选面板从屏幕上撤掉，否则失活后面板会孤零零悬停在原处，误导用户 |
   | 2 | `commitComposition(sender)` | 把用户已敲入、但尚未转换的原始按键（如半截拼音）**原样上屏**，避免输入丢失 |
   | 3 | `client = nil` | 主动丢弃客人引用，防止后续误向一个已失活的文本框写字 |

   **(c) 为什么这三步能避免「悬挂组合」？**
   悬挂组合的本质是：失活时手里那段未完成的组合（composition）既没上屏也没清除，残留状态会污染下一次激活或错误地插入到别的应用。`commitComposition` 正是切断这条残留链的刀——它把原始输入 `get_input` 取出来、`commit(string:)` 上屏、再 `clear_composition` 清空引擎侧的组合（见 4.3）。配上「隐藏面板 + 置空 client」，失活后控制器回到一个干净、无持有、无显示的状态，下次激活时不会带历史包袱。

**需要观察的现象**：如果你注释掉这三步中的任意一步（仅作为思维实验，**不要真改源码**），推测会发生什么——例如只去掉 `commitComposition`，那么用户在 A 应用敲到一半切到 B 应用，那段半成品会丢失或滞留。

**预期结果**：能口头复述「失活 = 隐藏面板 + 提交原始输入 + 置空 client」这条收尾铁律，并解释每一步的必要性。

#### 4.2.5 小练习与答案

**练习 1**：`activateServer` 里第 168 行用了 `self.client ?= sender as? IMKTextInput`，而 `deactivateServer` 里第 213 行直接写 `client = nil`。为什么前者要小心翼翼地用 `?=`，后者却敢直接置 nil？

**参考答案**：激活时 `sender` 是一个新的有效 client，我们希望「有效就更新」，所以用 `?=`（非 nil 才赋值）避免把一个好端端的 client 误清成 nil。失活时我们**目的就是**丢弃客人，所以直接 `client = nil`，无论它之前是否有值。

**练习 2**：`activateServer` 与 `init` 都会涉及 client，它们在时机和职责上有什么区别？

**参考答案**：`init` 是控制器**第一次**诞生时执行一次，负责创建 session、注册通知；`activateServer` 是控制器**每次上岗**时执行（可能多次），负责「上岗准备」——覆盖键盘布局、重置 preedit、刷新状态图标。两者职责互补：init 管一生的开始，activate 管每一次上岗。

---

### 4.3 commitComposition：提交原始输入的安全网

#### 4.3.1 概念说明

`commitComposition(_:)` 是 `IMKInputController` 的一个方法，IMK 框架会在「希望输入法立刻收尾」时调用它——最典型的就是上一节 `deactivateServer` 里主动调它。系统也可能在其它需要「清场」的场合调它。

Squirrel 覆写这个方法，做了一件很重要的事：**把用户已经敲入、但还没转换成汉字的原始按键，原样提交上屏**。

为什么是「原始按键」而不是「转换后的候选词」？因为 `commitComposition` 的语义是「**立即结束组合，别再等用户选词了**」。此时最稳妥的做法是把用户实际敲的字符（比如 `nihao` 这串字母）原样交还给目标应用，让应用自己处理，而不是自作主张地挑一个候选词。这保证了「输入不丢失」这条底线。

#### 4.3.2 核心流程

```
commitComposition(sender)
  ├─ self.client ?= sender as? IMKTextInput
  └─ if session != 0:
       ├─ if let input = rimeAPI.get_input(session):   // 取引擎里当前的原始输入
       │    ├─ commit(string: String(cString: input))   // 原样上屏
       │    └─ rimeAPI.clear_composition(session)       // 清空引擎侧组合
       └─ (无原始输入则什么都不做)
```

注意：如果 session 里根本没有待提交的输入（`get_input` 返回 nil），就什么都不做——这是幂等的，可以安全地多次调用。

#### 4.3.3 源码精读

[sources/SquirrelInputController.swift:221-229](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L221-L229)：先刷新 client，然后仅当 `session != 0` 时进入逻辑。`rimeAPI.get_input(session)` 返回一个 C 字符串指针（指向引擎里这段会话尚未处理完的原始输入码），非 nil 时用 `String(cString:)` 转成 Swift 字符串，调用 `commit(string:)` 上屏，紧接着 `rimeAPI.clear_composition(session)` 把引擎侧的组合状态清空。

[sources/SquirrelInputController.swift:551-556](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L551-L556)：`commit(string:)` 的实现——`guard let client = client else { return }` 守卫弱引用（client 可能已 nil），然后 `client.insertText(string, replacementRange: .empty)` 把文本写入目标应用，重置 `preedit`，并 `hidePalettes()` 隐藏面板。这里的 `.empty` 是项目扩展的 `NSRange` 哨兵，定义在 [sources/BridgingFunctions.swift:58-60](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L58-L60)，值为 `NSRange(location: NSNotFound, length: 0)`，表示「在当前光标位置插入」。

注意 `commit(string:)` 与 `rimeUpdate` 里的「正式上屏」是同一个函数：正式输入流里，当选定候选词后，引擎通过 `get_commit` 产出转换后的文本，再调用 `commit(string:)` 上屏；而 `commitComposition` 这里是**绕过转换**、把 `get_input` 的原始输入直接喂给同一个 `commit(string:)`。两条路径复用了上屏逻辑，但数据来源不同。

#### 4.3.4 代码实践

**实践目标**：对比「正常选词上屏」与「commitComposition 原始上屏」两条路径用了哪些 librime API，理解为什么后者叫「安全网」。

**操作步骤**：

1. 在 `SquirrelInputController.swift` 里定位以下三处：
   - `commitComposition` 的第 224 行：`rimeAPI.get_input(session)` + 第 226 行 `clear_composition`。
   - `commit(string:)` 的第 551-556 行。
   - `rimeConsumeCommittedText()`（第 426-434 行，属于 u2-l6 的正式上屏路径）：`rimeAPI.get_commit(session, &commitText)` + `free_commit`。
2. 画一张对照表，列出两条路径各自的「数据来源」「上屏函数」「引擎清理动作」。

**需要观察的现象**：两条路径最后都汇聚到 `commit(string:)` → `client.insertText`，但数据来源一个是 `get_input`（原始码），一个是 `get_commit`（转换后的文本）。

**预期结果**：能说清——正常路径是「引擎转换好→`get_commit`→上屏」，`commitComposition` 是「来不及转换→`get_input` 取原始码→原样上屏→`clear_composition`」。后者保证即使用户中途离开，已敲的字符也不会凭空消失。

#### 4.3.5 小练习与答案

**练习 1**：假如把 `commitComposition` 里第 226 行的 `rimeAPI.clear_composition(session)` 删掉，会发生什么？

**参考答案**：原始输入虽然上屏了，但引擎侧的组合状态没被清空。下次该 session 被复用（或同一会话继续）时，引擎可能仍认为有一段未完成的组合，导致状态错乱、预编辑文本残留。`clear_composition` 是保证「上屏后引擎回到干净状态」的必要收尾。

**练习 2**：`commitComposition` 在 `session == 0` 时直接什么都不做，这样安全吗？

**参考答案**：安全。`session == 0` 意味着根本没有有效的引擎会话，自然没有「原始输入」可言。什么都不做正是正确的幂等行为——既不会误调用 `get_input(0)`（向无效 session 查询），也不会留下任何副作用。

---

### 4.4 deinit 与 destroySession：控制器的谢幕与 session 自愈

#### 4.4.1 概念说明

每个 controller 实例对应一个 librime session。当 controller 被销毁时（ARC 引用计数归零，触发 `deinit`），它**必须**把对应的 session 也释放掉，否则引擎里会泄漏越来越多的「幽灵会话」，最终耗尽资源。

Squirrel 用 `deinit` → `destroySession()` 这条链来保证「controller 死，session 也死」。此外，源码里还有一处「session 自愈」逻辑：在按键处理主循环开头，如果发现 session 失效了（为 0 或 `find_session` 返回 false），会**立刻重建**一个，避免引擎会话意外丢失后输入法彻底失灵。

#### 4.4.2 核心流程

控制器的「谢幕」：

```
controller 引用计数归零
  └─ deinit
       └─ destroySession()
            ├─ if session != 0:
            │    ├─ rimeAPI.destroy_session(session)   // 释放引擎会话
            │    └─ session = 0                          // 标记无效
            └─ clearChord()                              // 清理打字机（chord）计时器等
```

session 自愈（位于按键主循环 `handle` 开头）：

```
handle(event, sender)
  └─ if session == 0 或 rimeAPI.find_session(session) 为 false:
       ├─ createSession()       // 重建会话
       └─ if session == 0: return false   // 仍失败则放行事件
```

完整生命周期串联起来：

```
   ┌─────────┐
   │  init   │  createSession → session≠0
   └────┬────┘
        ↓
   ┌──────────┐ ⇄ ┌────────────┐
   │ activate │   │ deactivate │  (可反复)
   └────┬─────┘   └─────┬──────┘
        │               │ hidePalettes + commitComposition + client=nil
        ↓               ↑
   ┌────────────────────────┐
   │  handle (按键主循环)    │  ← session 失效时自愈重建
   └────────────────────────┘
        ↓ (controller 不再被引用)
   ┌─────────┐
   │  deinit │  destroySession → session=0
   └─────────┘
```

#### 4.4.3 源码精读

[sources/SquirrelInputController.swift:297-299](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L297-L299)：`deinit` 只有一行——调用 `destroySession()`。Swift 的 `deinit` 在对象被释放前自动调用，是释放资源的天然钩子。

[sources/SquirrelInputController.swift:383-389](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L383-L389)：`destroySession()` 的实现——仅当 `session != 0` 时调用 `rimeAPI.destroy_session(session)` 释放引擎会话，再把 `session` 置 0，最后 `clearChord()` 清理 chord 打字相关的计时器与缓冲（chord 是 u2-l4/u2-l5 的内容，这里只需知道它有一份需要清理的资源）。

再看自愈逻辑：

[sources/SquirrelInputController.swift:40-45](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L40-L45)：在 `handle(_:client:)` 的最开头，检查 `session == 0 || !rimeAPI.find_session(session)`。`find_session` 是 librime 用来判断一个 session id 是否仍有效的 API。如果 session 已失效（比如引擎内部因故清掉了它），就调用 `createSession()` 重建；若重建后仍是 0（创建失败），直接 `return false` 放行这次按键。这是一个典型的「防御式编程」——输入法是常驻进程，绝不能因为一次 session 丢失就彻底罢工。

#### 4.4.4 代码实践

**实践目标**：跟踪一次 session 失效后的自愈过程，理解它对输入法稳定性的意义。

**操作步骤**：

1. 阅读第 40-45 行的自愈分支，以及第 188-191 行 `init` 末尾对 `createSession()` 的调用、第 297-299 行的 `deinit`。
2. 用文字推演以下场景（源码阅读型实践）：
   - 假设某个 controller 已经 `init` 成功（session = 42），用户正在输入。
   - 某种异常导致 librime 内部把 session 42 清掉了（`find_session(42)` 返回 false）。
   - 用户按下下一个键，进入 `handle`。
3. 写出这时代码的执行路径。

**需要观察的现象**：`handle` 第 40 行的条件命中，进入 `createSession()`，假设成功拿到 session = 43，`session != 0` 所以**不** return，继续往下处理这次按键。

**预期结果**：你应能描述出——得益于自愈逻辑，用户对这次「内部故障」几乎无感，最多丢一个按键的转换，下一次按键就已经在新会话上工作了。如果没有这段自愈，session 失效后 controller 会带着一个死 session 继续往引擎发 `process_key(42, ...)`，全部无效，输入法彻底哑火。

> 待本地验证：session 在正常运行中实际失效的概率与触发条件，需结合 librime 行为进一步确认；本实践聚焦于读懂自愈分支的设计意图。

#### 4.4.5 小练习与答案

**练习 1**：`destroySession()` 里为什么要有 `if session != 0` 的判断？直接调用 `destroy_session(session)` 会怎样？

**参考答案**：`session == 0` 是「无效会话」哨兵，对它调用 `destroy_session(0)` 是无意义甚至可能危险的（向引擎传无效句柄）。判断后只在确有会话时才释放，保证幂等——即使 `destroySession` 被调用多次（比如 deinit 调一次、别处又调一次），也不会重复释放或误操作。

**练习 2**：`deinit` 里只调了 `destroySession()`，为什么没有手动移除 `init` 里注册的那两个通知观察者（第 193-207 行）？

**参考答案**：那两个观察者是用 `NotificationCenter.default.addObserver(forName:...)` 配合 `[weak self]` 闭包注册的。因为捕获的是 `weak self`，controller 销毁后 `self` 变 nil，闭包里 `self?.xxx` 自动失效，不会再触发回调。虽然 iOS/macOS 较新版本下基于 block 的观察者会随对象释放而自动清理（且这里用的是 weak 捕获），不会造成崩溃或泄漏，这是一种惯用的安全写法。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「全生命周期追踪」任务。

**任务背景**：你的同事刚读完 Squirrel 源码，提出一个「优化」建议——「`deactivateServer` 里调 `commitComposition` 太啰嗦了，反正 controller 马上就要 `deinit`，到时候 `destroySession` 一关，组合自然就没了，不如省掉这两步」。请你用本讲学到的知识，找出这个建议的三个错误。

**操作步骤**：

1. 重新阅读 `deactivateServer`（第 210-214 行）、`commitComposition`（第 221-229 行）、`destroySession`（第 383-389 行）。
2. 结合 4.2.4 里「悬挂组合」的分析，逐条反驳同事的建议。
3. 把反驳写成一份简短的「代码评审意见」，至少包含以下三点：
   - **输入丢失**：省掉 `commitComposition` 后，用户已敲入的原始码会随 session 销毁而消失，没有上屏。
   - **时机不对**：`deactivate` 与 `deinit` 不是一回事——controller 失活后未必立即销毁（可能被复用、再激活），把收尾推迟到 `deinit` 等于让脏状态存活整段「失活但未销毁」的时间窗。
   - **destroySession 不上屏**：`destroySession` 只是在引擎侧释放会话，**根本不会**把原始输入写回目标应用，所以「session 一关组合自然没了」是丢了而非交还了。

**需要观察的现象**：你能清晰区分「deactivate（逻辑失活，可逆、可复用）」与「deinit（物理销毁，一次性）」两个不同时机，并指出收尾必须在 deactivate 完成。

**预期结果**：写出至少三条有理有据的反驳，结论是——`deactivateServer` 的三步收尾（hidePalettes / commitComposition / client=nil）缺一不可，不能用 `deinit` + `destroySession` 替代。

> 待本地验证：若有本地构建环境，可尝试在 `commitComposition` 里临时加一条 `print("commit raw: \(input)")`，切出文本框时观察是否真的触发了原始输入上屏，以此印证「失活即收尾」。

## 6. 本讲小结

- `SquirrelInputController` 与 librime session 是**一一对应**的关系：`init` 时 `createSession()` 申请会话，`deinit` 时 `destroySession()` 释放会话。
- `client`（`IMKTextInput`）必须声明为 `weak`——它的生命周期归目标应用与 IMK 框架，不属于输入法；强引用会造成引用循环与泄漏。使用前需 guard 守卫，用项目自定义的 `?=` 运算符稳健刷新。
- `activateServer(_:)` 是「上岗准备」：覆盖键盘布局、重置 preedit、刷新状态栏图标；`deactivateServer(_:)` 是「下岗收尾」：**隐藏面板 → 提交原始输入 → 置空 client**，三步缺一不可，是避免「悬挂组合」的关键。
- `commitComposition(_:)` 是一张「安全网」：在系统要求收尾时，用 `get_input` 取出原始按键、原样 `insertText` 上屏、再 `clear_composition` 清空引擎，保证输入不丢失。它与正常选词上屏（`get_commit`）复用同一个 `commit(string:)`，但数据来源不同。
- `handle(_:client:)` 开头有 **session 自愈**逻辑：发现 session 失效就立即重建，保证输入法在引擎内部故障后能自恢复，不会哑火。
- 生命周期是一条不可乱序的链：`init → activate ⇄ deactivate → deinit`，每一步该做的初始化与清理都是为下一步铺路。

## 7. 下一步学习建议

本讲搭好了控制器的「生命周期骨架」，但骨架上的「按键处理主循环」我们只是路过（第 40-45 行的自愈分支、第 32 行的 `handle` 签名）。下一讲 **u2-l4 键盘事件处理主循环** 将深入 `handle(_:client:)` 内部，讲清 `flagsChanged` 与 `keyDown` 两条分支、修饰键的释放优先、capslock 特殊处理、Command 快捷键放行，以及返回 `true/false` 的「吞掉 vs 透传」语义。

建议你带着本讲建立的 session 概念去读 u2-l4：你会发现 `handle` 的第一件事就是检查并自愈 session，本讲的 4.4.3 正是 u2-l4 的开场。读完 u2-l4 后，可以继续 u2-l5（按键映射）、u2-l6（rimeUpdate 数据流）、u2-l7（marked/commit 文本规则），完整走通这条输入主链路。

如果想提前验证本讲的生命周期理解，可以回到 u2-l1 对照 AppDelegate 持有的那些 App 级资源（panel、config、statusItem），再次体会「App 级 vs 会话级」生命周期的差异——这正是 `client` 必须弱引用、而 `panel` 由 AppDelegate 强持有的根本原因。
