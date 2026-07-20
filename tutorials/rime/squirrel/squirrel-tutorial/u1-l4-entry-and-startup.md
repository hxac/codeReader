# 程序入口与启动流程

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 Squirrel 的可执行文件有「双重身份」：既能作为输入法常驻运行，也能在命令行里执行一次性维护命令。
- 复述正常（非命令行）启动时，`Main.swift` 里对象的创建顺序：`IMKServer` → `NSApplication` → `SquirrelApplicationDelegate`。
- 解释 `setupRime()` → `startRime()` → `loadSettings()` 这三个方法为什么必须按这个顺序调用。
- 理解 `problematicLaunchDetected()` 如何用一个临时文件检测「崩溃循环」并自救。

本讲是整个第二单元「输入处理主链路」的入口：只有先搞清楚 Squirrel 进程是怎么起来的、librime 引擎是怎么被初始化的，后面阅读键盘事件、候选词面板才有立足点。

## 2. 前置知识

阅读本讲前，建议你先具备以下概念（不熟悉也没关系，下面会顺带解释）：

- **入口点（entry point）**：程序执行的第一行代码。C 程序是 `main()`，Swift 程序用 `@main` 标注的类型。Squirrel 的入口是 [`sources/Main.swift`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift) 里的 `@main struct SquirrelApp`。
- **前端 vs 引擎**（承接 u1-l1）：Squirrel 是「前端」，只负责收键盘事件、画面板、把字上屏；真正把按键翻译成汉字的是「引擎」librime。本讲会看到前端在启动时如何把引擎拉起来。
- **IMK（InputMethodKit）**（承接 u1-l5 的前置）：macOS 提供的输入法开发框架。`IMKServer` 是输入法进程向系统注册的「服务端」，系统的文本输入事件会通过它分发到输入法。
- **Distributed Notification（分布式通知）**：macOS 的一种跨进程广播机制。一个进程发出通知，其他正在运行的进程都能收到。Squirrel 用它让「命令行的一次性进程」去指挥「常驻运行的输入法进程」。
- **launchd 与崩溃重启**：macOS 由 `launchd` 管理输入法进程。如果输入法崩溃，`launchd` 会自动把它重新拉起——这本是好事，但如果崩溃是配置错误导致的，就会变成无限重启的「崩溃循环」。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，再借用两个文件做旁证：

| 文件 | 作用 | 本讲用到的地方 |
| --- | --- | --- |
| `sources/Main.swift` | 程序入口，`@main struct SquirrelApp`，定义 `main()` | 全讲核心 |
| `sources/SquirrelApplicationDelegate.swift` | 应用委托，持有全局状态，包含 `setupRime/startRime/loadSettings/problematicLaunchDetected` | 4.3、4.4 节精读 |
| `resources/Info.plist` | App 元数据，含 `InputMethodConnectionName` | 4.2 节引用连接名 |

> 提示：本讲引用的代码行号基于当前 HEAD `2158538`。如果你本地 checkout 到其它版本，行号可能偏移，但函数名、分支结构基本稳定。

## 4. 核心概念与源码讲解

### 4.1 命令行分支：一个二进制的双重身份

#### 4.1.1 概念说明

很多 macOS 输入法会拆成「输入法本体」和「配置工具」两个程序。Squirrel 走了另一条路：**同一个二进制，既当输入法，又当命令行工具**。

当你从「系统设置」里选中 Squirrel 时，macOS 通过 `launchd` 拉起这个二进制，它就作为输入法常驻运行。但当 `postinstall` 安装脚本、终端里的用户、或某个自动化脚本调用同一个二进制并带上 `--reload`、`--build` 这类参数时，它就变成一个「跑完就退出」的命令行工具。

这样做的好处是：不需要再维护一个额外的可执行文件，所有「对 Squirrel 的操作」都收敛到一个入口。

#### 4.1.2 核心流程

`main()` 的最外层是一个「先试探命令行、再决定是否进入正常启动」的两段式结构：

```text
main() 开始
  ├── 取得 rimeAPI（librime 的 C 接口指针）
  ├──【第一段】autoreleasepool {
  │     读 CommandLine.arguments
  │     如果 args.count > 1，按 args[1] 匹配 switch：
  │       --quit / --reload / --build / --ascii / ... → 执行并返回 true
  │       其它（含无参数）                                   → 返回 false
  │   }
  ├── if handled { return }      ← 命令行命中就到此为止，进程结束
  └──【第二段】autoreleasepool { 正常输入法启动 }
```

关键点：`handled` 为 `true` 时，`main()` 在 [`sources/Main.swift:121-123`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L121-L123) 直接 `return`，**永远不会创建 `IMKServer`，也不会进入输入法主循环**。这就是「双重身份」的分界线。

#### 4.1.3 源码精读

入口标注与 `main()` 函数签名见 [`sources/Main.swift:11-24`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L11-L24)：

```swift
@main
struct SquirrelApp {
  // swiftlint:disable:next cyclomatic_complexity
  static func main() {
    let rimeAPI: RimeApi_stdbool = rime_get_api_stdbool().pointee
    ...
```

- `@main` 告诉 Swift 编译器：这个类型的 `static func main()` 就是程序入口。
- `// swiftlint:disable:next cyclomatic_complexity` 是给 SwiftLint 的提示：下一行的圈复杂度超标是故意的（因为 `switch` 分支很多），不要报警。关于 SwiftLint 的细节会在 u5-l6 讲。

`switch` 语句集中处理所有命令行参数，见 [`sources/Main.swift:31-118`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L31-L118)。下表逐一说明：

| 参数 | 行号 | 作用 | 实现要点 |
| --- | --- | --- | --- |
| `--quit` | [L32-36](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L32-L36) | 终止所有正在运行的 Squirrel 进程 | 用 `NSRunningApplication.runningApplications(withBundleIdentifier:)` 找到同 bundleId 的进程并 `terminate()` |
| `--reload` | [L37-39](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L37-L39) | 让已运行实例重新部署方案 | 发分布式通知 `SquirrelReloadNotification`，由常驻实例的观察者接收 |
| `--register-input-source` / `--install` | [L40-42](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L40-L42) | 向系统注册输入源 | 调 `SquirrelInstaller().register()`（底层 `TISRegisterInputSource`） |
| `--enable-input-source [模式...]` | [L43-52](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L43-L52) | 启用指定或默认输入模式 | 可选参数为 Hans/Hant 的输入源 ID；缺省则启用默认主模式 |
| `--disable-input-source [模式...]` | [L53-62](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L53-L62) | 禁用输入模式 | 同上，反向操作 |
| `--select-input-source [模式]` | [L63-69](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L63-L69) | 选中某个输入模式 | 只接受单个模式参数 |
| `--build` | [L70-77](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L70-L77) | 编译当前目录下所有方案 | 用一套独立的 builder traits 调 `deployer_initialize` + `deploy` |
| `--sync` | [L78-80](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L78-L80) | 同步用户词库 | 发 `SquirrelSyncNotification` |
| `--ascii` | [L81-83](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L81-L83) | 切换到 ASCII（英文）模式 | 发 `SquirrelToggleASCIIModeNotification`，`object` 为 `"ascii"` |
| `--nascii` | [L84-86](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L84-L86) | 切换到非 ASCII（中文）模式 | 同上，`object` 为 `"nascii"` |
| `--getascii` | [L87-111](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L87-L111) | 查询当前 ASCII 模式 | 请求-应答协议，最长等 2 秒 |
| `--help` | [L112-114](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L112-L114) | 打印帮助 | 输出静态字符串 `helpDoc` |

其中最值得细看的是三类「跨进程指挥」参数。

**第一类：直接操作本进程能触达的系统 API**（`--quit`、`--register-input-source` 等）。例如 `--quit` 见 [`sources/Main.swift:32-36`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L32-L36)：

```swift
case "--quit":
  let bundleId = Bundle.main.bundleIdentifier!
  let runningSquirrels = NSRunningApplication.runningApplications(withBundleIdentifier: bundleId)
  runningSquirrels.forEach { $0.terminate() }
  return true
```

注意它 terminate 的是「别的」Squirrel 进程——命令行进程本身刚启动，并不在 `runningSquirrels` 里需要特别处理，它执行完 `return true` 就自然退出。

**第二类：发分布式通知，交给常驻实例处理**（`--reload`、`--sync`、`--ascii`、`--nascii`）。以 `--reload` 为例 [`sources/Main.swift:37-39`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L37-L39)：

```swift
case "--reload":
  DistributedNotificationCenter.default().postNotificationName(.init("SquirrelReloadNotification"), object: nil)
  return true
```

为什么用分布式通知？因为「重新部署方案」这件事必须由**正在运行的那个输入法进程**来做（它持有 librime 引擎实例和所有 session）。命令行进程是个全新进程，它没有引擎，所以只能「喊一嗓子」让常驻实例去干活。常驻实例在 `addObservers()` 里注册了对 `SquirrelReloadNotification` 的监听，收到后调 `deploy()`（详见 u5-l1）。

**第三类：请求-应答协议**（`--getascii`）。它不仅要发请求，还要等回执，见 [`sources/Main.swift:87-111`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L87-L111)：

```swift
case "--getascii":
  var responseReceived = false
  var asciiStatus = ""
  let observer = DistributedNotificationCenter.default().addObserver(
    forName: .init("SquirrelASCIIModeResponse"), object: nil, queue: .main) { notification in
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
  if responseReceived { print(asciiStatus) } else { print("nascii") }
  return true
```

时序是：

```text
命令行进程                      常驻输入法进程
    │  注册监听 ASCIIModeResponse
    │  ──── GetASCIIModeNotification ────▶
    │                                   收到后把当前 ascii 状态
    │  ◀──── ASCIIModeResponse ──────────  作为 object 回发
    │  收到 → print(status)
    │  （2 秒内没收到 → print("nascii")）
```

这里有个细节：命令行进程默认没有跑 `RunLoop`，而分布式通知的回调要在 `RunLoop` 上派发，所以代码用 `RunLoop.current.run(until:)` 每 10 毫秒转一次，直到收到回执或超时。这就是「请求-应答协议」。

完整的命令行清单也写在 [`sources/Main.swift:160-175`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L160-L175) 的 `helpDoc` 常量里，执行 `/Library/Input\ Methods/Squirrel.app/Contents/MacOS/Squirrel --help` 即可看到。

#### 4.1.4 代码实践

**实践目标**：把所有命令行参数和它们的「执行方式」对应起来，区分哪些是本进程直接做、哪些是发通知委托常驻实例做。

**操作步骤**：

1. 打开 [`sources/Main.swift:31-118`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L31-L118) 的 `switch`。
2. 对照上面的参数表，给每个 `case` 标注它属于「直接执行」还是「发分布式通知」还是「请求-应答」。
3. 注意 `default: break`（[L115-117](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L115-L117)）：未识别的参数会落到这里，`switch` 结束后 `handled` 仍为 `false`，于是程序**会继续走正常输入法启动**。

**需要观察的现象**：如果你在装好 Squirrel 的 Mac 上（待本地验证，本环境为 Linux 无法运行）执行：

```bash
/path/to/Squirrel.app/Contents/MacOS/Squirrel --help
```

**预期结果**：终端打印 `helpDoc` 的内容，进程随即退出，**不会**在屏幕上弹出输入法面板，也不会常驻——因为 `--help` 返回了 `true`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--reload` 不直接在命令行进程里调 `deploy()`，而是发一个分布式通知？

> **参考答案**：`deploy()` 必须作用在**已经初始化、持有用户 session** 的 librime 实例上。命令行进程是个全新进程，它的 librime 还没初始化（命令行分支根本没调 `setupRime`），也没有任何输入会话。只有常驻运行的输入法进程才有资格重新部署，所以必须用分布式通知把请求转交给它。

**练习 2**：执行 `Squirrel --getascii` 时，如果没有常驻的输入法进程在运行，会输出什么？为什么？

> **参考答案**：会输出 `nascii`。因为没有进程回应 `SquirrelASCIIModeResponse`，`responseReceived` 一直为 `false`，2 秒后超时，走到 `else { print("nascii") }` 分支。注意「超时」和「真的是非 ASCII 模式」在这里都映射成同一个输出 `nascii`——这是设计上的一个取舍。

**练习 3**：如果用户敲了一个未定义的参数 `Squirrel --foobar`，会发生什么？

> **参考答案**：`switch` 落到 `default: break`，`handled` 为 `false`，`if handled { return }` 不成立，于是程序**继续往下走正常输入法启动流程**。也就是说，带未知参数运行 Squirrel，等同于把它当成输入法启动。

---

### 4.2 IMKServer 与 NSApplication：输入法进程的诞生

#### 4.2.1 概念说明

当 `handled` 为 `false`（无参数、未知参数，或被 macOS 当输入法拉起）时，进入第二段 `autoreleasepool`，见 [`sources/Main.swift:125-156`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L125-L156)。这里要建立三样东西：

1. **`IMKServer`**：输入法向 macOS 注册的服务端。系统里任何 App 收到键盘事件、且当前输入源是 Squirrel 时，就会通过这个 server 把事件路由过来。
2. **`NSApplication`**：Cocoa 应用对象。输入法本质上也是一个（特殊的）Cocoa 应用，需要事件循环。
3. **`SquirrelApplicationDelegate`**：应用委托，承载所有全局状态和启动逻辑。

#### 4.2.2 核心流程

```text
autoreleasepool {
  1. 从 Info.plist 读 InputMethodConnectionName
  2. IMKServer(name: 连接名, bundleIdentifier: bundleId)   ← 注册输入法服务
  3. app = NSApplication.shared
  4. delegate = SquirrelApplicationDelegate()
  5. app.delegate = delegate
  6. app.setActivationPolicy(.accessory)                   ← 不在 Dock 露脸
  7. 切换工作目录到 SharedSupport（给 OpenCC 用）
  8. 检测崩溃循环 problematicLaunchDetected()
       ├── 命中 → 语音报警，不初始化引擎
       └── 未命中 → setupRime / startRime / loadSettings
  9. app.run()                                              ← 进入事件循环
  10.（退出后）rimeAPI.finalize()                           ← 收尾释放引擎
}
```

#### 4.2.3 源码精读

**创建 IMKServer 与应用对象**，见 [`sources/Main.swift:126-132`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L126-L132)：

```swift
let main = Bundle.main
let connectionName = main.object(forInfoDictionaryKey: "InputMethodConnectionName") as! String
_ = IMKServer(name: connectionName, bundleIdentifier: main.bundleIdentifier!)
let app = NSApplication.shared
let delegate = SquirrelApplicationDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
```

要点：

- `InputMethodConnectionName` 在 [`resources/Info.plist:92-93`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L92-L93) 里定义为 `"Squirrel_Connection"`。`IMKServer` 用这个名字和系统建立命名管道式的连接——系统的输入法框架（`imkagent`）就靠这个名字找到 Squirrel。
- `IMKServer(...)` 的返回值被赋给 `_`，看起来像「创建即丢弃」。其实它被 ARC（自动引用计数）保留在作用域里直到 `autoreleasepool` 结束；只要它存活，输入法服务就一直在线。这也是为什么它必须写在 `app.run()` 之前的同一个作用域内。
- `setActivationPolicy(.accessory)`：输入法平时不应在 Dock 里显示图标、也不该抢焦点。`.accessory` 正是这种「后台 UI 元素」策略。注意 AppDelegate 在弹更新提示时会临时切到 `.regular`（[L30](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L30)），更新完再切回 `.accessory`（[L47](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L47)）。
- `NSApplication.shared` 是单例；`delegate` 必须在 `app.run()` 之前赋值，否则 delegate 的 `applicationWillFinishLaunching` 等回调不会被触发。

**切换工作目录**，见 [`sources/Main.swift:134-135`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L134-L135)：

```swift
// OpenCC uses relative dictionary paths from SharedSupport.
FileManager.default.changeCurrentDirectoryPath(main.sharedSupportPath!)
```

OpenCC（简繁转换库）在加载字典时使用相对路径，所以必须先把进程的「当前工作目录」切到 App 包内的 `SharedSupport/`，否则找不到字典文件。这是 C 库相对路径的典型坑。

**进入事件循环与收尾**，见 [`sources/Main.swift:153-155`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L153-L155)：

```swift
app.run()
print("Squirrel is quitting...")
rimeAPI.finalize()
```

`app.run()` 会阻塞，直到应用被终止（比如 `--quit`、登出、关机）。返回后才执行 `rimeAPI.finalize()` 释放引擎资源。注意 AppDelegate 的 `applicationShouldTerminate`（[L245-249](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L245-L249)）还会在退出前调 `cleanup_all_sessions()` 清理所有输入会话。

> 旁证：AppDelegate 还提供了一个便捷下标 `NSApplication.squirrelAppDelegate`（[L441-445](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L441-L445)），把 `NSApp.delegate` 强转成 `SquirrelApplicationDelegate`。`Main.swift` 第 137、147-149 行的 `NSApp.squirrelAppDelegate.xxx` 就是靠它访问的。

#### 4.2.4 代码实践

**实践目标**：搞清楚「连接名」从哪来、有什么用。

**操作步骤**：

1. 打开 [`resources/Info.plist`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist)，定位 `InputMethodConnectionName`（[L92-93](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L92-L93)）和 `InputMethodServerControllerClass`（[L94-95](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L94-L95)）。
2. 在 [`sources/Main.swift:127-128`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L127-L128) 确认这个连接名被喂给了 `IMKServer`。
3. 思考：如果两个不同的输入法用了相同的 `InputMethodConnectionName`，会发生什么冲突？

**需要观察的现象 / 预期结果**：

- `InputMethodConnectionName` 的值是 `Squirrel_Connection`。
- `InputMethodServerControllerClass` 指向 `Squirrel.SquirrelInputController`——这是系统在收到键盘事件后，会通过连接名找到 server、再实例化的「输入控制器」类（u2-l3 精讲）。
- 第 3 问的结论（待本地验证）：连接名是进程级唯一的「管道名」，重名会导致两个输入法抢同一通道，行为不可预期——这也是为什么每个输入法都要起一个独特的连接名。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `IMKServer(...)` 的返回值可以赋给 `_`？它会被立刻释放吗？

> **参考答案**：不会立刻释放。Swift 用 ARC 管理对象生命周期，`IMKServer` 实例被创建后，只要所在的作用域（这里的 `autoreleasepool` 块）还在执行，它的引用计数就大于 0，对象一直存活。赋给 `_` 只是表示「我不需要再用这个变量名引用它」，并不等于「立刻销毁」。它必须活到 `app.run()` 期间，输入法服务才在线。

**练习 2**：把 `app.delegate = delegate`（[L131](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L131)）挪到 `app.run()`（[L153](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L153)）之后会怎样？

> **参考答案**：`applicationWillFinishLaunching` 是 `NSApplication` 在 `run()` 启动初期、即将完成「应用已启动」广播时同步调用 delegate 的回调。如果此时 delegate 还没赋值，`applicationWillFinishLaunching`（创建 `panel`、`refreshStatusItem`、`addObservers`）就不会被触发，输入法的面板、状态栏图标、观察者全都建不起来。所以 delegate 必须在 `run()` 之前赋值。

**练习 3**：`setActivationPolicy(.accessory)` 对用户最直观的影响是什么？

> **参考答案**：Squirrel 不会在 Dock 上显示图标，也不会出现在 Cmd+Tab 的应用切换器里——它表现得像一个「看不见的后台服务」。只有状态栏（菜单栏）上的「中 / Ａ」小图标暴露它的存在。这正是输入法期望的低调形态。

---

### 4.3 AppDelegate 启动序列：setupRime → startRime → loadSettings

#### 4.3.1 概念说明

确认不是崩溃循环后，[`sources/Main.swift:147-150`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L147-L150) 依次调用三个方法把 librime 引擎拉起来、并把前端配置读进来：

```swift
NSApp.squirrelAppDelegate.setupRime()
NSApp.squirrelAppDelegate.startRime(fullCheck: false)
NSApp.squirrelAppDelegate.loadSettings()
```

这三步是**严格有序的依赖链**：后一步依赖前一步建立的引擎状态。librime 是一个 C++ 库，通过 dylib 暴露 C 接口给 Swift 调用，所以「初始化」分得比较细。

#### 4.3.2 核心流程

```text
setupRime()                          ← 「告诉 librime 我是谁」
  ├── 建 userDir / logDir 目录
  ├── setenv("RIME_LOG_DIR", ...)
  ├── 注册 notification_handler（C 回调，context 指向 self）
  ├── 填充 RimeTraits（数据目录、版本、app_name 等）
  └── rimeAPI.setup(&traits)

startRime(fullCheck: false)          ← 「真正启动引擎」
  ├── rimeAPI.initialize(nil)
  └── start_maintenance(fullCheck)
        └── 若返回 true → deploy_config_file("squirrel.yaml", "config_version")

loadSettings()                       ← 「读前端自己的配置」
  ├── openBaseConfig()（打开 squirrel.yaml）
  ├── 读 show_notifications_when / status_icon/show
  ├── refreshStatusItem()
  └── panel.load(config:, forDarkMode: false/true)  ← 预读亮/暗主题
```

三者的分工可以记成一句话：**setup 描述身份、start 启动引擎、load 读前端配置**。

#### 4.3.3 源码精读

**setupRime** 见 [`sources/SquirrelApplicationDelegate.swift:139-159`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L139-L159)。关键片段：

```swift
func setupRime() {
  createDirIfNotExist(path: SquirrelApp.userDir)
  createDirIfNotExist(path: SquirrelApp.logDir)
  setenv("RIME_LOG_DIR", SquirrelApp.logDir.path(), 1)
  let notification_handler: @convention(c) (...) -> Void = notificationHandler
  let context_object = Unmanaged.passUnretained(self).toOpaque()
  rimeAPI.set_notification_handler(notification_handler, context_object)

  var squirrelTraits = RimeTraits.rimeStructInit()
  squirrelTraits.setCString(Bundle.main.sharedSupportPath!, to: \.shared_data_dir)
  squirrelTraits.setCString(SquirrelApp.userDir.path(), to: \.user_data_dir)
  squirrelTraits.setCString(SquirrelApp.logDir.path(), to: \.log_dir)
  squirrelTraits.setCString("Squirrel", to: \.distribution_code_name)
  squirrelTraits.setCString("鼠鬚管", to: \.distribution_name)
  squirrelTraits.setCString(..., to: \.distribution_version)
  squirrelTraits.setCString("rime.squirrel", to: \.app_name)
  rimeAPI.setup(&squirrelTraits)
}
```

要点：

- `userDir` 和 `logDir` 是 `SquirrelApp` 顶部定义的静态路径。`userDir` 指向 `~/Library/Rime`（用户方案、词库存放处），见 [`sources/Main.swift:13-17`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L13-L17)；`logDir` 指向系统临时目录下的 `rime.squirrel`，见 [L21](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L21)。
- `set_notification_handler` 把一个 C 函数指针 `notificationHandler` 和一个「上下文指针」`context_object` 交给 librime。`context_object` 用 `Unmanaged.passUnretained(self).toOpaque()` 把 Swift 的 `self` 装箱成裸指针——这样 librime 在 C 回调里就能通过这个指针找回 delegate（详见 [L266-267](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L266-L267) 的反向拆箱）。关于这套桥接约定的细节归 u5-l4。
- `RimeTraits` 是个 C 结构体，必须用 `.rimeStructInit()` 初始化（不能默认 `init()`），并通过 `setCString` 逐字段填充字符串。`rimeAPI.setup(&squirrelTraits)` 把这些「我是谁、数据在哪」的信息注册给引擎。

**startRime** 见 [`sources/SquirrelApplicationDelegate.swift:161-167`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L161-L167)：

```swift
func startRime(fullCheck: Bool) {
  print("Initializing la rime...")
  rimeAPI.initialize(nil)
  if rimeAPI.start_maintenance(fullCheck) {
    _ = rimeAPI.deploy_config_file("squirrel.yaml", "config_version")
  }
}
```

- `initialize(nil)` 真正构造引擎实例（`nil` 表示用 setup 阶段设好的默认 traits）。
- `start_maintenance(fullCheck)` 是 librime 的「自检/部署」入口：它会检查用户数据是否需要重新编译。返回 `true` 表示「需要部署 `squirrel.yaml` 这个前端配置」，于是紧接着调 `deploy_config_file`。`fullCheck: false` 表示启动时只做必要检查（首次或版本变化时才全量），避免每次开机都慢吞吞地全量重建。

**loadSettings** 见 [`sources/SquirrelApplicationDelegate.swift:169-182`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L169-L182)：

```swift
func loadSettings() {
  config = SquirrelConfig()
  if !config!.openBaseConfig() { return }
  enableNotifications = config!.getString("show_notifications_when") != "never"
  showStatusIcon = config!.getBool("status_icon/show") ?? true
  refreshStatusItem()
  if let panel = panel, let config = self.config {
    panel.load(config: config, forDarkMode: false)
    panel.load(config: config, forDarkMode: true)
  }
}
```

- 这里读的是**前端自己的配置** `squirrel.yaml`（不是输入方案 schema），决定「要不要显示通知、要不要显示状态栏图标、面板长什么样」。
- 注意时序细节：`loadSettings()` 在 `app.run()` **之前**调用，而 `panel` 是在 `applicationWillFinishLaunching`（`app.run()` 启动期间）才创建的。所以**首次启动时 `panel` 还是 nil**，`if let panel = panel` 这段预读会跳过；面板的主题在后续 `loadSettings(for:)`（切方案时）才真正加载。这一点会在 u3-l4 详细展开，本讲只需记住「loadSettings 负责把配置读进 config」。

#### 4.3.4 代码实践

**实践目标**：验证三个方法的调用顺序与依赖关系。

**操作步骤（源码阅读型）**：

1. 在 [`sources/Main.swift:146-151`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L146-L151) 确认顺序是 `setupRime` → `startRime` → `loadSettings`。
2. 在 [`sources/SquirrelApplicationDelegate.swift:161-167`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L161-L167) 的 `startRime` 里找到对 `setupRime` 建立的 traits 的隐式依赖：`initialize(nil)` 之所以能传 `nil`，是因为 `setupRime` 已经把默认 traits 注册好了。
3. 思考：如果把 `startRime` 挪到 `setupRime` 之前，`initialize(nil)` 还能正常工作吗？

**需要观察的现象 / 预期结果**：

- 三步顺序固定，不可调换。
- 第 3 问结论：`setup` 是「登记身份信息」，`initialize` 是「按登记的身份信息真正构造引擎」。不先 `setup` 就 `initialize(nil)`，引擎拿不到数据目录、日志目录，行为会异常（无法定位方案与词库）。这正是顺序强约束的根本原因。

> 想看真实运行日志的话（待本地验证，需 macOS + 已构建的 Squirrel）：构建后从命令行前台运行 `Squirrel.app/Contents/MacOS/Squirrel`，标准输出会依次打印 `Initializing la rime...`（[L162](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L162)）和 `Squirrel reporting!`（[L150](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L150)）。

#### 4.3.5 小练习与答案

**练习 1**：`setupRime` 里设置 `user_data_dir` 指向哪个目录？这个目录里通常放什么？

> **参考答案**：指向 `~/Library/Rime`（由 `SquirrelApp.userDir` 决定）。里面放用户的输入方案（`.yaml` / `.schema.yaml`）、用户词库（`.userdb`）、构建产物（`build/`）以及 `squirrel.yaml`（前端配置）等。

**练习 2**：`start_maintenance(fullCheck)` 返回 `true` 才会 `deploy_config_file("squirrel.yaml", ...)`。这里的第二个参数 `"config_version"` 是什么意思？

> **参考答案**：`deploy_config_file` 的签名是 `deploy_config_file(file_name, version_key)`。librime 会读取 `squirrel.yaml` 里以 `version_key`（即 `config_version`）为键的版本号，与上次部署时记录的版本对比；只有版本变了（或从未部署过）才真正重新部署。这是一种「按版本号去跳过无变化配置」的增量机制，避免每次启动都重做部署。

**练习 3**：为什么 `notificationHandler` 要通过 `Unmanaged.passUnretained(self)` 把 `self` 传给 librime，而不是直接捕获？

> **参考答案**：librime 是 C 接口，回调只能接收一个 `void*` 上下文指针，不能持有 Swift 的强引用。`Unmanaged.passUnretained` 把 `self` 转成裸指针而不增加引用计数——这是安全的，因为 delegate 的生命周期由 `NSApplication` 保证（它比 librime 引擎活得久），不会先于回调被销毁。回调触发时再用 `Unmanaged.fromOpaque(...).takeUnretainedValue()` 把指针拆回 Swift 对象（见 u5-l4）。

---

### 4.4 problematicLaunchDetected：防崩溃循环的自愈机制

#### 4.4.1 概念说明

设想一个事故场景：用户改错了 `squirrel.yaml`，导致 librime 在初始化时崩溃。Squirrel 进程一启动就 crash，`launchd` 立刻把它重启，又崩溃，又重启……CPU 飙满、风扇狂转，而用户连输入法面板都看不到，根本不知道发生了什么。

`problematicLaunchDetected()` 就是针对这种「崩溃循环」的自愈保险：用一个临时文件记录「上次启动的时刻」，如果发现「上次启动就在 2 秒内」，就判定为崩溃循环，**跳过引擎初始化**，并用系统语音合成（`/usr/bin/say`）朗读一句报警——因为在崩溃循环里没有任何 UI 能弹窗，声音是唯一能触达用户的通道。

#### 4.4.2 核心流程

```text
problematicLaunchDetected() → Bool
  1. 读临时文件 squirrel_launch.json（上次启动时刻）
       ├── 文件不存在（首次启动）         → detected = false，静默继续
       ├── 上次时刻距今 < 2 秒             → detected = true（崩溃循环！）
       └── 其它读错误                     → 沿用 detected（false）
  2. 把「现在」写入 squirrel_launch.json（无论是否检测到）
  3. 返回 detected

main() 里：
  if problematicLaunchDetected() {
      不调 setupRime/startRime/loadSettings   ← 跳过最可能崩溃的引擎初始化
      /usr/bin/say 朗读报警
  } else {
      正常 setupRime / startRime / loadSettings
  }
  app.run()   ← 两种情况都进入事件循环，让进程「活下来」
```

「让进程活下来」是关键：只有当前这次启动不再立即崩溃并进入 `app.run()` 常驻，`launchd` 才不会在 2 秒内再次拉起它，循环就此打断。

#### 4.4.3 源码精读

调用点在 [`sources/Main.swift:137-151`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L137-L151)：

```swift
if NSApp.squirrelAppDelegate.problematicLaunchDetected() {
  print("Problematic launch detected!")
  let args = ["Problematic launch detected! Squirrel may be suffering a crash due to improper configuration. Revert previous modifications to see if the problem recurs."]
  let task = Process()
  task.executableURL = "/usr/bin/say".withCString { dir in
    URL(fileURLWithFileSystemRepresentation: dir, isDirectory: false, relativeTo: nil)
  }
  task.arguments = args
  try? task.run()
} else {
  NSApp.squirrelAppDelegate.setupRime()
  NSApp.squirrelAppDelegate.startRime(fullCheck: false)
  NSApp.squirrelAppDelegate.loadSettings()
  print("Squirrel reporting!")
}
```

检测逻辑见 [`sources/SquirrelApplicationDelegate.swift:202-228`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L202-L228)，关键判定：

```swift
let logFile = FileManager.default.temporaryDirectory.appendingPathComponent("squirrel_launch.json", conformingTo: .json)
...
let previousLaunch = try decoder.decode(Date.self, from: archive)
if previousLaunch.timeIntervalSinceNow >= -2 {
  detected = true
}
```

`timeIntervalSinceNow` 返回「该时刻距现在多少秒」，过去的时间为负数。`>= -2` 的含义是「该时刻不早于 2 秒前」，即**上次启动就在最近 2 秒内**：

\[ \text{上次启动时刻} - \text{现在} \in [-2,\ 0] \quad\Longleftrightarrow\quad \text{上次启动在 0\text{～}2 秒前} \]

错误处理分两种：

- 文件不存在（`NSFileReadNoSuchFileError`，[L213](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L213)）：首次启动的正常情况，`catch` 体留空，`detected` 保持 `false`。
- 其它错误（比如 JSON 损坏）：打印错误，返回当前 `detected`（[L215-217](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L215-L217)）。

无论检测结果如何，函数末尾都会把「当前时刻」写回文件（[L219-226](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L219-L226)），为下一次启动提供比对基准。

报警用 `/usr/bin/say` 通过 `Process` 异步执行（`try? task.run()` 不等待朗读结束），所以即便很长的句子也不会阻塞 `app.run()`。

#### 4.4.4 代码实践

**实践目标**：理解「2 秒阈值」如何区分正常重启与崩溃循环。

**操作步骤（源码阅读 + 推理型）**：

1. 阅读 [`sources/SquirrelApplicationDelegate.swift:202-228`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L202-L228)，找出三处 `do/catch`。
2. 推演下面三种情形下 `detected` 的值与程序走向：
   - 情形 A：系统首次安装 Squirrel，`squirrel_launch.json` 不存在。
   - 情形 B：用户正常退出 Squirrel 后，过 1 分钟再切回输入法（`launchd` 重新拉起）。
   - 情形 C：librime 因配置错误崩溃，`launchd` 在 0.5 秒内重启。

**需要观察的现象 / 预期结果**：

| 情形 | 文件中上次时刻距今 | `detected` | 走向 |
| --- | --- | --- | --- |
| A 首次启动 | 文件不存在 | `false` | 正常初始化引擎 |
| B 间隔 60 秒 | 约 −60 秒，`< −2` | `false` | 正常初始化引擎 |
| C 崩溃循环 | 约 −0.5 秒，`≥ −2` | `true` | 跳过引擎初始化 + 语音报警 |

> 想真正复现（待本地验证，需 macOS 且敢于制造崩溃）：故意把 `~/Library/Rime/squirrel.yaml` 改坏到让 librime 崩溃，然后反复切输入法，应能听到 `/usr/bin/say` 的英文语音报警，且 CPU 不会持续飙满。**做完记得改回配置并删除 `$TMPDIR/squirrel_launch.json`**，否则下次正常启动也可能因为残留的「最近时刻」被误判。

#### 4.4.5 小练习与答案

**练习 1**：为什么报警用 `/usr/bin/say`（语音）而不是弹一个 `NSAlert`？

> **参考答案**：崩溃循环里 Squirrel 还没进入稳定的 UI 状态（甚至连 `app.run()` 都可能没正常跑起来），而且 `setActivationPolicy(.accessory)` 使它没有前台窗口。此时 `NSAlert` 没有合适的窗口依附、可能根本显示不出来。系统自带的 `say` 命令走音频输出，不依赖任何 UI 窗口，是崩溃场景下唯一可靠的用户触达手段。

**练习 2**：如果 `squirrel_launch.json` 被人为改成了一个损坏的 JSON，`problematicLaunchDetected()` 会怎样？

> **参考答案**：`decoder.decode` 抛错，落到第二个 `catch`（[L215](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L215)），打印错误并返回 `detected`（此时仍为 `false`）。然后末尾的 `do` 块会用当前时刻**覆盖**那个损坏的文件，所以下一次启动就恢复正常比对了——具有自修复能力。

**练习 3**：检测到崩溃循环后，程序为什么仍然要执行 `app.run()`（[L153](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L153)），而不是直接退出？

> **参考答案**：如果不进入 `app.run()`，进程会立刻结束，`launchd` 会在很短时间内再次拉起它——而下次启动读到的「上次时刻」依然在 2 秒内，于是再次被判为崩溃循环……虽然这次不再初始化引擎、不会再崩，但进程还是会反复重启，浪费资源。进入 `app.run()` 让进程**稳定常驻**，才能彻底打断 `launchd` 的重启循环。

---

## 5. 综合实践

**任务**：画出 Squirrel 从「被系统拉起」到「进入输入法主循环」的完整启动时序图，并把命令行分支和正常分支都画进去。

**操作步骤**：

1. 通读 [`sources/Main.swift:24-158`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L24-L158) 的整个 `main()`。
2. 在一张图上标注以下节点，并写出每个节点对应的源码行号：
   - 取 `rimeAPI`
   - 命令行 `switch`（列出至少 6 个 `case` 及其归属：直接执行 / 分布式通知 / 请求-应答）
   - `if handled { return }` 分界点
   - `IMKServer` 创建（连接名来自哪个 plist 键）
   - `NSApplication` + delegate + `.accessory`
   - `changeCurrentDirectoryPath`（为什么切到 SharedSupport）
   - `problematicLaunchDetected()` 的两条分支
   - 正常分支的 `setupRime` → `startRime` → `loadSettings`
   - `app.run()` 与退出后的 `rimeAPI.finalize()`
3. 在图上额外标出 `applicationWillFinishLaunching`（[L58-62](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L58-L62)）的触发时机：它发生在 `app.run()` 内部，晚于三个启动方法。

**预期产出**：一张能回答以下问题的时序图——

- 「敲 `Squirrel --reload` 时，进程会不会画面板？」（答：不会，发完通知就 `return` 退出。）
- 「`setupRime` 和 `startRime` 能不能调换？」（答：不能，后者依赖前者注册的 traits。）
- 「检测到崩溃循环时，引擎会被初始化吗？」（答：不会，但进程仍会 `app.run()` 常驻以打断重启循环。）

> 提示：如果在本环境（Linux）无法运行 Squirrel，可以把「时序图 + 行号标注 + 三个问答」作为纯源码阅读产出。真实的运行验证（打印 `Initializing la rime...`、`Squirrel reporting!`、`/usr/bin/say` 报警）需在已构建的 macOS 环境下进行，标注「待本地验证」。

## 6. 本讲小结

- Squirrel 是**单二进制双身份**：带命令行参数时是一次性工具（`--quit/--reload/--build/--ascii/--getascii/...`），命中后 `if handled { return }` 直接退出；不带可识别参数时才作为输入法常驻。
- 命令行参数分三类执行方式：直接操作系统 API（`--quit/--register-input-source`）、发分布式通知委托常驻实例（`--reload/--sync/--ascii`）、请求-应答协议（`--getascii`）。
- 正常启动按固定顺序建立三大对象：`IMKServer`（连接名 `Squirrel_Connection` 来自 `Info.plist`）→ `NSApplication.shared` → `SquirrelApplicationDelegate`，并设为 `.accessory` 激活策略、切工作目录到 SharedSupport。
- 引擎初始化是 **`setupRime`（登记身份/traits）→ `startRime`（`initialize` + `start_maintenance`）→ `loadSettings`（读 squirrel.yaml 前端配置）** 的依赖链，顺序不可调换。
- `problematicLaunchDetected()` 用 `$TMPDIR/squirrel_launch.json` 记录上次启动时刻，若距今 < 2 秒则判定崩溃循环，跳过引擎初始化并用 `/usr/bin/say` 语音报警，但仍 `app.run()` 常驻以打断 `launchd` 的重启循环。
- `app.run()` 返回后才执行 `rimeAPI.finalize()` 收尾；退出前 `applicationShouldTerminate` 还会 `cleanup_all_sessions()`。

## 7. 下一步学习建议

本讲把「进程怎么起来、引擎怎么初始化」讲完了，接下来可以沿两条线深入：

- **纵向，进入输入主链路（第二单元）**：建议先读 u2-l1《应用委托与全局状态》，搞清楚 `SquirrelApplicationDelegate` 持有的 `config / panel / statusItem / updateController` 各自的归属；再读 u2-l3《输入控制器生命周期》，看系统通过 `IMKServer` 找到 `SquirrelInputController` 后是如何创建 session 的——那是键盘事件真正进入 Squirrel 的入口。
- **横向，了解安装与桥接（第五单元）**：如果你对 `--register-input-source` 背后的 TIS 注册、或 `setupRime` 里的 `Unmanaged`/`rimeStructInit` 桥接约定感兴趣，可以先跳读 u5-l2《输入源注册（TIS）》和 u5-l4《Swift/C 桥接约定》，再回到第二单元。

推荐继续精读的源码：[`sources/SquirrelApplicationDelegate.swift`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift)（全局状态与引擎初始化）、[`sources/SquirrelInputController.swift`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift)（输入控制器，下一讲的主角）。
