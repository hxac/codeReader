# 全局 librime 初始化

## 1. 本讲目标

Squirrel 自己不做「按键→汉字」的转换，这件事交给引擎 librime。那么前端在什么时候、用什么方式把引擎「拉起来」？本讲要回答这个问题。

学完本讲，你应当能够：

1. 说清楚 `setupRime()`、`startRime(fullCheck:)`、`loadSettings()` 这三个方法各自做了什么、为什么顺序不能换。
2. 看懂 `RimeTraits` 这张交给引擎的「身份名片」上有哪些字段，以及它们如何决定引擎读写数据的目录。
3. 理解 Swift 与 C 语言结构体之间的桥接约定：为什么必须用 `rimeStructInit()` 初始化、`setCString` 分配的 C 字符串归谁所有。
4. 解释 librime 的三阶段启动 `setup` → `initialize` → `start_maintenance`，以及 `deploy_config_file` 如何把 `squirrel.yaml` 部署到用户目录。
5. 描述引擎如何通过 `notificationHandler` 回调把「部署中」「方案切换」等消息送回前端。

---

## 2. 前置知识

在进入源码之前，先用通俗语言铺垫几个本讲会用到的概念。

### 2.1 前端与引擎的分工

回顾 [u1-l1](u1-l1-project-overview.md)：Squirrel 是**前端**，负责收键盘事件、画面板、把字上屏；librime 是**引擎**，负责把按键序列转换成候选词。前端和引擎是两个独立的运行单元，前端必须在启动早期把引擎实例「创建好」，后续每一次按键才能交给它处理。

### 2.2 全局状态挂在 AppDelegate 上

回顾 [u2-l1](u2-l1-app-delegate.md)：引擎句柄 `rimeAPI` 是 App 级资源，挂在 `SquirrelApplicationDelegate` 上，而不是挂在会话级的 `SquirrelInputController` 上。原因是引擎在整个 App 生命周期里只创建一次、被所有会话共享。所以「把引擎拉起来」这件事，天然由 AppDelegate 负责。本讲讲的就是 AppDelegate 里负责拉起引擎的那几个方法。

### 2.3 C 结构体与 FFI 桥接

librime 是用 C++ 写的，对外暴露一套 **C API**（头文件 `rime_api_stdbool.h`）。Swift 调用它属于 **FFI（Foreign Function Interface，外部函数接口）**。这里有两个初学者容易踩坑的点：

- **C 结构体需要手动初始化**。C 语言的结构体不会自动清零，里面可能有垃圾值；而且 librime 的结构体里有一个特殊字段 `data_size`，引擎靠它判断调用方填了多大数据。所以 Squirrel 写了一个统一的初始化工具 `rimeStructInit()`。
- **C 字符串的所有权要手动管理**。Swift 的 `String` 和 C 的 `char *` 不能直接互换，需要用 `strdup` 拷贝一份；而这份拷贝什么时候释放，需要程序员自己约定。

本讲的 `setCString`、`rimeStructInit` 就是处理这两件事的工具（它们的完整约定在 [u5-l4](u5-l4-bridging-conventions.md) 深入讲解，本讲只讲用到的那部分）。

### 2.4 librime 的「部署」概念

librime 不会直接读你写的 `.yaml` 方案文件去工作，它需要先把方案**编译（部署）**成内部的二进制形式，缓存到用户目录。这个过程叫 **deployment / maintenance**。所以引擎启动时，除了创建实例，还要触发一次部署，确保方案是最新的。理解了这一点，才能看懂 `start_maintenance` 和 `deploy_config_file` 在做什么。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `sources/SquirrelApplicationDelegate.swift` | 应用委托。包含本讲主角：`setupRime()`、`startRime(fullCheck:)`、`loadSettings()`，以及引擎回调 `notificationHandler`。 |
| `sources/BridgingFunctions.swift` | Swift↔C 桥接工具。包含 `rimeStructInit()`（C 结构体初始化）、`setCString`（C 字符串赋值）等约定。 |
| `sources/Main.swift` | 程序入口。在 `app.run()` 之前按固定顺序调用三个初始化方法，并定义了 `userDir`、`logDir` 两个关键目录。 |

入口处的调用顺序是理解全讲的「主干」，先记在脑子里：

```text
Main.swift: setupRime() → startRime(fullCheck: false) → loadSettings() → app.run()
```

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，按代码执行顺序讲解：

1. **RimeTraits 填充与 setCString**：给引擎准备一张「身份名片」。
2. **notificationHandler 安装**：在引擎启动前装好「回调通道」。
3. **setup → initialize → start_maintenance 三阶段**：引擎的启动主流程。
4. **deploy_config_file 部署 squirrel.yaml**：把前端配置送到用户目录。

> 说明：模块 1、2 都发生在 `setupRime()` 内部；模块 3、4 都发生在 `startRime()` 内部。讲解时我们顺着真实代码的执行顺序走。

---

### 4.1 RimeTraits 填充与 setCString

#### 4.1.1 概念说明

`RimeTraits` 是 librime 定义的一个 C 结构体，你可以把它理解成**前端递给引擎的一张「身份名片 + 工作环境说明」**。引擎拿到这张名片后，才知道：

- 自己叫什么名字、是哪个发行版、版本号多少（用于日志和诊断）；
- 应该去哪个目录读**共享数据**（随 App 发行的方案、字典）；
- 应该去哪个目录读写**用户数据**（用户自己的配置、学习到的词库）；
- 应该把日志写到哪个目录。

如果不填这张名片，引擎就不知道去哪里找方案文件，整个输入法就「无米下锅」。

#### 4.1.2 核心流程

填充 `RimeTraits` 的步骤：

```text
1. 用 RimeTraits.rimeStructInit() 创建一个「正确初始化过」的结构体
2. 用 setCString(...) 逐个填入字符串字段：
   shared_data_dir      ← App 包内的 SharedSupport 目录
   user_data_dir        ← ~/Library/Rime
   log_dir              ← 系统临时目录下的 rime.squirrel
   distribution_code_name ← "Squirrel"
   distribution_name    ← "鼠鬚管"
   distribution_version ← Info.plist 里的构建版本号
   app_name             ← "rime.squirrel"
3. 把填好的结构体交给 rimeAPI.setup(&squirrelTraits)
```

为什么用 `setCString` 而不是直接赋值？因为 `RimeTraits` 里的这些字段类型是 `UnsafePointer<CChar>?`（即 C 字符串指针），不能直接塞 Swift 的 `String`。`setCString` 负责把 Swift 字符串拷贝成一份 C 字符串，再把指针写进结构体。

#### 4.1.3 源码精读

整段 `setupRime()` 见 [sources/SquirrelApplicationDelegate.swift:139-159](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L139-L159)。其中 `RimeTraits` 的填充部分是核心：

```swift
var squirrelTraits = RimeTraits.rimeStructInit()
squirrelTraits.setCString(Bundle.main.sharedSupportPath!, to: \.shared_data_dir)
squirrelTraits.setCString(SquirrelApp.userDir.path(), to: \.user_data_dir)
squirrelTraits.setCString(SquirrelApp.logDir.path(), to: \.log_dir)
squirrelTraits.setCString("Squirrel", to: \.distribution_code_name)
squirrelTraits.setCString("鼠鬚管", to: \.distribution_name)
squirrelTraits.setCString(Bundle.main.object(forInfoDictionaryKey: kCFBundleVersionKey as String) as! String, to: \.distribution_version)
squirrelTraits.setCString("rime.squirrel", to: \.app_name)
rimeAPI.setup(&squirrelTraits)
```

逐字段说明（这是本讲的重点，建议对照上表记住）：

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `shared_data_dir` | `Bundle.main.sharedSupportPath` | 共享数据目录，即 `Squirrel.app/Contents/SharedSupport`，存放随 App 发行的方案与字典（只读）。 |
| `user_data_dir` | `SquirrelApp.userDir` | 用户数据目录，值为 `~/Library/Rime`（见下方说明）。用户的配置、自学习词库存这里（可写）。 |
| `log_dir` | `SquirrelApp.logDir` | 日志目录，值为系统临时目录下的 `rime.squirrel` 子目录。 |
| `distribution_code_name` | `"Squirrel"` | 发行版的英文代号。 |
| `distribution_name` | `"鼠鬚管"` | 发行版的显示名（中文名）。 |
| `distribution_version` | Info.plist 中的 `CFBundleVersion` | App 的构建版本号，从 `Bundle.main` 动态读取，不写死。 |
| `app_name` | `"rime.squirrel"` | 前端的唯一标识名，librime 用它给日志命名、区分不同前端。 |

`user_data_dir` 指向的目录在 [sources/Main.swift:13-17](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L13-L17) 定义，逻辑是：先尝试用 `getpwuid(getuid())` 拿到当前用户的家目录，再拼上 `Library/Rime`；拿不到家目录时回退到系统的 `Library` 目录再拼 `Rime`。所以正常情况下它就是 **`~/Library/Rime`**。

`log_dir` 在 [sources/Main.swift:21](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L21) 定义，是 `FileManager.default.temporaryDirectory` 下的 `rime.squirrel` 子目录（即 `$TMPDIR/rime.squirrel`）。

再看两个桥接工具。`rimeStructInit()` 在 [sources/BridgingFunctions.swift:22-30](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L22-L30)：

```swift
static func rimeStructInit() -> Self {
  let valuePointer = UnsafeMutablePointer<Self>.allocate(capacity: 1)
  memset(valuePointer, 0, MemoryLayout<Self>.size)
  var value = valuePointer.move()
  valuePointer.deallocate()
  let offset = MemoryLayout.size(ofValue: \Self.data_size)
  value.data_size = Int32(MemoryLayout<Self>.size - offset)
  return value
}
```

它做两件事：① 用 `memset` 把整块结构体内存清零，避免随机垃圾值；② 正确填写 `data_size` 字段——这个字段是 librime 的 **ABI（二进制接口）大小标记**，引擎靠它判断「调用方填写的结构体有多大」，从而做到向前兼容。`data_size` 的取值意图是「结构体从 `data_size` 字段到末尾的字节数」，即 `总大小 - data_size 字段的偏移`。这就是为什么所有 librime 结构体都必须用 `rimeStructInit()` 初始化、而不能用 Swift 默认的 `RimeTraits()`——后者不会清零、也不会填 `data_size`，引擎可能拒绝或误读。（`data_size` 与 ABI 的完整细节见 [u5-l4](u5-l4-bridging-conventions.md)。）

`setCString` 在 [sources/BridgingFunctions.swift:32-41](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L32-L41)：

```swift
mutating func setCString(_ swiftString: String, to keypath: WritableKeyPath<Self, UnsafePointer<CChar>?>) {
  swiftString.withCString { cStr in
    let mutableCStr = strdup(cStr)
    if let existing = self[keyPath: keypath] {
      free(UnsafeMutableRawPointer(mutating: existing))
    }
    self[keyPath: keypath] = UnsafePointer(mutableCStr)
  }
}
```

关键点：它用 `strdup` 在 C 堆上拷贝一份字符串，把指针交给结构体；如果该字段之前已经有值，会先 `free` 掉旧值再覆盖（所以同一个字段重复 `setCString` 不会泄漏）。这份 `strdup` 出来的 C 字符串的生命周期由结构体持有，最终在引擎 `finalize()` 时随结构体一起回收。

#### 4.1.4 代码实践

**实践目标**：把 `RimeTraits` 的字段表内化，并确认 `user_data_dir` 的真实路径。

**操作步骤**（源码阅读型实践）：

1. 打开 [sources/SquirrelApplicationDelegate.swift:150-158](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L150-L158)，逐行列出 `setCString` 设置的字段。
2. 打开 [sources/Main.swift:13-17](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L13-L17)，跟踪 `userDir` 的拼接过程。
3. 在一张纸上写下：每个字段 → 取值来源 → 一句话含义。

**需要观察的现象 / 预期结果**：

- 你应当能列出至少 5 个字段：`shared_data_dir`、`user_data_dir`、`log_dir`、`distribution_code_name`、`distribution_name`、`distribution_version`、`app_name`（任选 5 个）。
- `user_data_dir` 指向的目录路径是 **`~/Library/Rime`**（展开后类似 `/Users/你的用户名/Library/Rime`）。
- `distribution_version` 不是写死的字符串，而是运行时从 `Bundle.main` 读 `CFBundleVersion`。

> 如果想本地验证路径：在 macOS 上安装 Squirrel 后，用 Finder 前往 `~/Library/Rime`（在 Finder 按 `Cmd+Shift+G` 输入该路径），应能看到 `squirrel.yaml`、各方案目录、用户词库等——这正是 `user_data_dir` 指向的地方。该观察步骤待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `user_data_dir` 设成一个不存在的目录，且没有事先创建，引擎启动会出什么问题？结合 `setupRime()` 开头两行说明 Squirrel 是怎么避免这个问题的。

> **答案**：引擎去一个不存在的目录读写会失败，导致方案加载不到、无法输入。Squirrel 在 [sources/SquirrelApplicationDelegate.swift:140-141](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L140-L141) 用 `createDirIfNotExist(path:)` 提前创建了 `userDir` 和 `logDir`，保证目录就绪后再交给引擎。

**练习 2**：`distribution_version` 为什么用 `Bundle.main.object(forInfoDictionaryKey:)` 动态读取，而不直接写一个 `"1.0"` 这样的字符串？

> **答案**：因为版本号会随每次发版变化。从 `Info.plist` 的 `CFBundleVersion` 动态读取，能保证引擎日志里记录的版本号和 App 实际版本一致，避免发版后忘记同步。

---

### 4.2 notificationHandler 安装

#### 4.2.1 概念说明

引擎在工作时（部署中、方案切换、选项变化等），需要**主动通知前端**一些事情。比如部署完成了，前端应该弹个提示告诉用户。librime 用「回调函数」机制实现这个：前端注册一个函数指针，引擎在合适时机调用它。

这里的难点是：librime 是 C API，它要的是一个 **C 函数指针**；而 Squirrel 的逻辑写在 Swift 的 `SquirrelApplicationDelegate` 里。怎么把两者连起来？答案是用 `@convention(c)` 标注的闭包 + `Unmanaged` 指针桥接。

#### 4.2.2 核心流程

```text
1. 把 Swift 方法 notificationHandler 包装成 C 兼容的函数指针（@convention(c)）
2. 用 Unmanaged.passUnretained(self).toOpaque() 拿到 self 的裸指针，作为 context_object
3. 调 rimeAPI.set_notification_handler(函数指针, context_object)
4. 引擎回调时：从 context_object 还原出 self，再分发消息
```

`context_object` 的作用是「夹带私货」：C 回调函数是全局的，它本身不知道该通知哪个 AppDelegate 实例。所以注册时把 `self` 的指针塞进去，回调时再取出来，就能回到正确的对象上。

#### 4.2.3 源码精读

安装部分在 `setupRime()` 里，位于 [sources/SquirrelApplicationDelegate.swift:145-148](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L145-L148)：

```swift
let notification_handler: @convention(c) (UnsafeMutableRawPointer?, RimeSessionId, UnsafePointer<CChar>?, UnsafePointer<CChar>?) -> Void = notificationHandler
let context_object = Unmanaged.passUnretained(self).toOpaque()
rimeAPI.set_notification_handler(notification_handler, context_object)
```

几个要点：

- `@convention(c)` 把 Swift 闭包标注成 C 函数指针的调用约定，这样 librime 才能正确调用它。注意 `notificationHandler` 是一个**顶层 `private func`**（不是实例方法），因为 C 函数指针不能捕获 Swift 的上下文——上下文靠 `context_object` 显式传递。
- `Unmanaged.passUnretained(self).toOpaque()` 把 `self` 变成裸指针 `UnsafeMutableRawPointer`，**不带所有权转移**（`Unretained`）。这是安全的，因为 AppDelegate 的生命周期覆盖整个 App，引擎回调期间它一定还活着。
- 这里还先把日志目录通过环境变量暴露出去：[sources/SquirrelApplicationDelegate.swift:143](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L143) 的 `setenv("RIME_LOG_DIR", ...)`，目的是让 librime 的**插件**（它们可能不读 `RimeTraits`）也能找到日志目录。

回调函数本体 `notificationHandler` 在 [sources/SquirrelApplicationDelegate.swift:265-333](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L265-L333)。它先从 `contextObject` 还原出 delegate：

```swift
let delegate: SquirrelApplicationDelegate = Unmanaged<SquirrelApplicationDelegate>.fromOpaque(contextObject!).takeUnretainedValue()
```

然后根据 `messageType` 分发四类消息：

| `messageType` | 含义 | 前端动作 |
| --- | --- | --- |
| `"deploy"` | 部署进度（start/success/failure） | 弹本地化提示，如「部署开始 / 成功 / 失败」 |
| `"option"` | 某个选项开关变化（如 `ascii_mode`） | 刷新状态栏图标（中/Ａ）、弹状态文案 |
| `"property"` | 插件保留属性（键以 `_` 开头） | 转发到主线程的 `handleReservedProperty` |
| `"schema"` | 当前方案切换 | 弹方案名提示 |

其中 `"deploy"` 的处理最能体现「引擎→前端」通道的价值，见 [sources/SquirrelApplicationDelegate.swift:272-283](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L272-L283)：当引擎报告 `success` 时，前端调用 `showMessage(msgText: NSLocalizedString("deploy_success", comment: ""))`，用户就看到了「部署完成」的提示。

> 注意：`"property"` 消息的处理涉及插件→前端协调，是 [u5-l3](u5-l3-reserved-property.md) 的主题，本讲只需知道它在这条回调通道里被分发即可。

#### 4.2.4 代码实践

**实践目标**：跟踪一条「部署成功」消息从引擎到用户的完整路径。

**操作步骤**（源码阅读型实践）：

1. 在 [sources/SquirrelApplicationDelegate.swift:272-283](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L272-L283) 找到 `"deploy"` 分支的 `"success"` case。
2. 跟踪它调用的 `showMessage(msgText:)`（静态方法，[L114-L137](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L114-L137)），看它如何用 `UNUserNotificationCenter` 发系统通知。
3. 思考：这条消息最初是由谁、在什么时机抛出的？

**需要观察的现象 / 预期结果**：

- 你应当理清调用链：`引擎部署完成` → `notificationHandler(type:"deploy", value:"success")` → `showMessage(msgText:)` → `UNUserNotificationCenter` 系统通知。
- 回调之所以能找到正确的 AppDelegate 实例，是因为注册时塞入了 `context_object`，回调里用 `Unmanaged...fromOpaque` 还原。

> 本地可选验证：在 macOS 上执行 `Squirrel --reload` 触发一次部署，部署结束后应看到系统通知「部署完成」（前提是 `show_notifications_when` 没设成 `never`）。该观察步骤待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `notificationHandler` 必须是顶层函数（`private func`），而不是 `SquirrelApplicationDelegate` 的实例方法？

> **答案**：因为它要被包装成 C 函数指针（`@convention(c)`）。C 函数指针不能捕获 Swift 实例上下文，所以不能是会隐式捕获 `self` 的实例方法。需要 `self` 时，靠注册时传入的 `context_object` 裸指针显式还原。

**练习 2**：`Unmanaged.passUnretained(self)` 用的是 `Unretained`（不持有）。为什么这里不用 `Retained`（持有）？

> **答案**：因为 AppDelegate 本身由 `NSApplication` 持有，生命周期覆盖整个 App，引擎回调期间它一定存在。用 `Unretained` 避免额外的引用计数循环；如果用 `Retained` 反而要记得在某个时机 `release`，容易漏。前提是「被指对象活得比回调久」——这里成立。

---

### 4.3 setup → initialize → start_maintenance 三阶段

#### 4.3.1 概念说明

librime 的启动不是「一个函数搞定」，而是分成三个职责清晰的阶段。理解这三个阶段，是理解整个引擎生命周期的钥匙：

1. **`setup`（登记）**：告诉引擎「我是谁、日志写哪」。属于全局配置，轻量。
2. **`initialize`（创建实例）**：真正创建引擎实例，让引擎进入可用状态。
3. **`start_maintenance`（部署/维护）**：在后台检查并编译方案、字典，确保数据是最新的。

Squirrel 把这三个阶段分别放进 `setupRime()`（含 setup）和 `startRime(fullCheck:)`（含 initialize + start_maintenance）两个方法，并且**顺序严格不可调换**：没有 setup 就 initialize，引擎不知道自己的身份和目录；没有 initialize 就 start_maintenance，根本没有实例可供维护。

#### 4.3.2 核心流程

```text
setupRime():  [createDir] [setenv] [装回调] [填 Traits] → rimeAPI.setup(&traits)
startRime():  rimeAPI.initialize(nil) → rimeAPI.start_maintenance(fullCheck)
                                  └─若返回 true─→ deploy_config_file("squirrel.yaml", ...)
```

这里有一个容易被忽略的细节：`start_maintenance` 有返回值（`Bool`），Squirrel 用它来决定是否额外部署 `squirrel.yaml`。

#### 4.3.3 源码精读

`startRime(fullCheck:)` 全文很短，见 [sources/SquirrelApplicationDelegate.swift:161-167](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L161-L167)：

```swift
func startRime(fullCheck: Bool) {
  print("Initializing la rime...")
  rimeAPI.initialize(nil)
  if rimeAPI.start_maintenance(fullCheck) {
    _ = rimeAPI.deploy_config_file("squirrel.yaml", "config_version")
  }
}
```

逐行：

- `rimeAPI.initialize(nil)`：创建引擎实例。传 `nil` 表示「复用 `setup` 阶段登记过的 `RimeTraits`」，所以这里不需要再传一遍 traits。这就是为什么 `setupRime` 必须先于 `startRime` 执行。
- `rimeAPI.start_maintenance(fullCheck)`：启动后台部署任务。`fullCheck` 参数控制是否强制全量检查：
  - 正常启动时（[Main.swift:148](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L148)）传 `false`，引擎只做增量检查，启动更快；
  - 用户主动 `--reload` 时，`deploy()` 调用 `startRime(fullCheck: true)`（[sources/SquirrelApplicationDelegate.swift:81-86](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L81-L86)），强制全量重新部署。
- `start_maintenance` 的返回值表示「是否有维护任务需要执行」。只有返回 `true`（确实有活要干）时，才额外调用 `deploy_config_file` 把 `squirrel.yaml` 重新部署一遍。这样既保证了首次启动/配置变更时会部署前端配置，又避免了每次启动都重复无谓的部署工作。

**与 `--build` 命令的对比**：入口 [Main.swift:70-77](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L70-L77) 里有一条**仅供命令行部署**的路径，它复用了 `setup`，但后面接的是 `deployer_initialize` + `deploy`，而不是 `initialize` + `start_maintenance`：

```swift
var builderTraits = RimeTraits.rimeStructInit()
builderTraits.setCString("rime.squirrel-builder", to: \.app_name)
rimeAPI.setup(&builderTraits)
rimeAPI.deployer_initialize(nil)
_ = rimeAPI.deploy()
```

这说明 `setup` 是「登记身份」的公共前置，而 `initialize`（运行时引擎）和 `deployer_initialize`（纯部署器）是两条不同的后续路径。`--build` 只要编译方案，不需要常驻运行，所以走更轻的 deployer 路径。把这个对比记住，三阶段的职责会更清晰。

#### 4.3.4 代码实践

**实践目标**：对比「正常启动」与「`--reload` 重新部署」两条路径在 `fullCheck` 上的差异，并理解 `initialize(nil)` 为何能省略 traits。

**操作步骤**（源码阅读型实践）：

1. 在 [sources/Main.swift:147-149](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L147-L149) 找到正常启动的三连调用，确认 `startRime(fullCheck: false)`。
2. 在 [sources/SquirrelApplicationDelegate.swift:81-86](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L81-L86) 找到 `deploy()` 方法，确认它调用 `startRime(fullCheck: true)`。
3. 跟踪 `deploy()` 又是被谁触发的：在 [sources/SquirrelApplicationDelegate.swift:404-407](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L404-L407) 的 `rimeNeedsReload`，而它绑定到分布式通知 `SquirrelReloadNotification`（`--reload` 命令发出，见 [Main.swift:37-39](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L37-L39)）。

**需要观察的现象 / 预期结果**：

- 你应当能画出两条调用链：
  - 正常启动：`Main.main()` → `setupRime()` → `startRime(fullCheck: false)` → `loadSettings()`。
  - 重新部署：`Squirrel --reload` → 分布式通知 `SquirrelReloadNotification` → `rimeNeedsReload` → `deploy()` → `shutdownRime()` + `startRime(fullCheck: true)` + `loadSettings()`。
- 解释 `initialize(nil)` 为什么能省略 traits：因为上一阶段的 `setup(&squirrelTraits)` 已经把 traits 登记在引擎全局，`initialize(nil)` 复用它。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `startRime` 里的 `rimeAPI.initialize(nil)` 改成在 `setupRime` 之前执行，会发生什么？

> **答案**：会出问题。`initialize` 复用的是 `setup` 登记过的 traits（目录、身份）。若先 `initialize`，此时还没 `setup`，引擎不知道 `user_data_dir`、`shared_data_dir` 在哪，无法正确加载方案与字典。所以 `setupRime`（含 `setup`）必须先于 `startRime`（含 `initialize`）。

**练习 2**：为什么正常启动用 `fullCheck: false`，而 `--reload` 用 `fullCheck: true`？

> **答案**：正常启动追求快，增量检查即可——如果方案没变就不重新编译；`--reload` 是用户明确要求「重新部署」，必须强制全量检查并重建，确保配置变更生效。

---

### 4.4 deploy_config_file 部署 squirrel.yaml

#### 4.4.1 概念说明

`librime` 在 `start_maintenance` 里会部署**方案**（schema），但前端自己的配置文件 `squirrel.yaml` 是另一回事——它不是输入方案，而是 Squirrel 专属的「前端配置」（界面颜色、字体、应用级选项等）。所以 Squirrel 用一个专门的 API `deploy_config_file` 单独部署它。

`deploy_config_file` 接受两个参数：文件名和一个「版本键」。它的作用是：把随 App 发行的 `squirrel.yaml`（在 `shared_data_dir` 里）**部署/合并**到用户的 `user_data_dir`，并在版本变化时更新。引擎靠「版本键」判断要不要重新部署——如果用户本地的版本和发行版一致，就跳过，避免覆盖用户的个性化修改。

#### 4.4.2 核心流程

```text
deploy_config_file("squirrel.yaml", "config_version")
       │
       ├── 读 shared_data_dir/squirrel.yaml 里的 config_version
       ├── 读 user_data_dir/squirrel.yaml   里的 config_version（若存在）
       ├── 比较：发行版版本 > 用户版版本？
       │     是 → 重新部署（合并/覆盖），更新用户副本
       │     否 → 跳过，保留用户现有配置
       └── 返回是否执行了部署
```

`config_version` 是 `squirrel.yaml` 文件里的一个顶层字段，作者每次改动发行版配置时会递增它，这正是触发用户端「配置需要更新」的信号。

#### 4.4.3 源码精读

部署那一行在 [sources/SquirrelApplicationDelegate.swift:164-166](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L164-L166)：

```swift
if rimeAPI.start_maintenance(fullCheck) {
  _ = rimeAPI.deploy_config_file("squirrel.yaml", "config_version")
}
```

要点：

- 只有 `start_maintenance` 返回 `true`（确有维护工作）时才部署 `squirrel.yaml`。这避免每次启动都重复部署。
- 第一个参数 `"squirrel.yaml"` 是相对文件名，引擎会在 `shared_data_dir` 找发行版、在 `user_data_dir` 找/写用户版。
- 第二个参数 `"config_version"` 是版本键，对应 `squirrel.yaml` 里的 `config_version:` 字段。引擎比较这个字段的值来决定是否需要重新部署。
- 返回值被 `_ =` 忽略——前端只关心「部署动作被触发」，不关心引擎内部是否真的改写了文件。

部署完成后的下一步是 `loadSettings()`（[sources/SquirrelApplicationDelegate.swift:169-182](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L169-L182)），它通过 `config.openBaseConfig()` 打开刚刚部署到用户目录的 `squirrel.yaml`，读出 `show_notifications_when`、`status_icon/show` 等前端选项，并加载亮/暗主题。这条链印证了三阶段的顺序：先 setup 登记 → 再 initialize + maintenance + deploy_config_file 把配置就位 → 最后 loadSettings 读取配置。顺序颠倒的话，`loadSettings` 会读到旧的或缺失的配置。

> `squirrel.yaml` 的具体结构（`style`、`preset_color_schemes`、`app_options` 等）是 [u3-l2](u3-l2-squirrel-yaml.md) 的主题，本讲只关注它「如何被部署到用户目录」。

#### 4.4.4 代码实践

**实践目标**：理解 `config_version` 字段如何充当「是否需要重新部署」的开关。

**操作步骤**（源码阅读型实践）：

1. 打开 `data/squirrel.yaml`（仓库里签入的发行版配置），在文件顶部找到 `config_version:` 字段，记下它的值（通常是一个日期串或版本串）。
2. 思考：如果一个用户在 `~/Library/Rime/squirrel.yaml` 里把 `config_version` 改成一个比发行版**更大**的值，下次启动时 `deploy_config_file` 会怎么处理这个文件？

**需要观察的现象 / 预期结果**：

- 你应能指出：`config_version` 是顶层字段，是 `deploy_config_file` 第二个参数的对应物。
- 预期行为：`deploy_config_file` 比较发行版与用户版的 `config_version`。当用户版 >= 发行版时，引擎认为用户配置已是最新，**不会覆盖**用户的手工修改——这正是 Rime「尊重用户定制」的设计。把用户版改大，相当于「骗」引擎跳过部署，从而保护自定义配置。

> 该「改大版本号保护定制」的行为是 librime 的既有约定，建议在本地安装的 Squirrel 上小范围验证（先备份 `~/Library/Rime/squirrel.yaml`）。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`deploy_config_file` 的第二个参数为什么是 `"config_version"` 而不是别的字符串？

> **答案**：因为它必须对应 `squirrel.yaml` 文件里实际存在的一个字段名，引擎读这个字段的值来做版本比较。`config_version` 是该文件顶层定义的版本字段。如果传一个不存在的字段名，引擎就无法判断版本，部署逻辑会失效。

**练习 2**：为什么部署 `squirrel.yaml` 被放在 `start_maintenance` 返回 `true` 的分支里，而不是无条件执行？

> **答案**：`start_maintenance` 返回 `true` 表示「确有维护工作要做」（比如首次启动、或检测到文件变化）。只有在需要维护时才部署 `squirrel.yaml`，可以避免每次启动都重复读、比、写配置文件，加快启动速度，也减少对用户配置的无谓干扰。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「全链路阅读」任务。

**任务**：从入口出发，完整描述「正常启动时，引擎是如何被初始化、`squirrel.yaml` 是如何就位的」，并标注每一步对应的源码行。

**建议产出一张时序图（文字版即可）**，至少包含以下节点，并附上永久链接：

1. `Main.main()` 判定非命令行分支，创建 `SquirrelApplicationDelegate`（[Main.swift:125-132](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L125-L132)）。
2. `problematicLaunchDetected()` 为假，进入初始化（[Main.swift:137-151](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L137-L151)）。
3. `setupRime()`：建目录 → 暴露 `RIME_LOG_DIR` → 装 `notificationHandler` → 填 `RimeTraits`（7 个字段）→ `rimeAPI.setup`（[L139-L159](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L139-L159)）。
4. `startRime(fullCheck: false)`：`initialize(nil)` → `start_maintenance(false)` → 视返回值 `deploy_config_file("squirrel.yaml","config_version")`（[L161-L167](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L161-L167)）。
5. `loadSettings()`：`openBaseConfig()` 读 `squirrel.yaml`，加载主题（[L169-L182](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L169-L182)）。
6. `app.run()` 进入事件循环，引擎就绪，等待按键。

**进阶追问**（选做）：在上面的时序里，如果第 3 步的 `set_notification_handler` 被注释掉，第 4 步部署 `squirrel.yaml` 时用户还能看到「部署完成」的提示吗？为什么？

> 参考答案：不能。部署完成的提示完全依赖 `notificationHandler` 回调（`"deploy"`/`"success"` 分支）。注释掉注册，引擎仍会部署，但前端收不到通知，`showMessage` 不会被调用，用户就看不到提示。这正说明「装回调」必须早于「触发部署」。

---

## 6. 本讲小结

- Squirrel 通过 `setupRime()` → `startRime(fullCheck:)` → `loadSettings()` 三步把 librime 引擎拉起，顺序严格不可调换。
- `RimeTraits` 是前端的「身份名片」，七个字符串字段告诉引擎目录与身份；其中 `user_data_dir` 指向 `~/Library/Rime`。
- 所有 librime C 结构体必须用 `rimeStructInit()` 初始化（清零 + 填 `data_size`），字符串字段用 `setCString` 赋值（`strdup` 拷贝、覆盖前 `free`）。
- 引擎启动分三阶段：`setup`（登记身份/目录）→ `initialize`（创建实例，复用 setup 的 traits）→ `start_maintenance`（后台部署方案）。
- `notificationHandler` 通过 `@convention(c)` + `Unmanaged` 裸指针建立「引擎→前端」回调通道，分发 deploy/option/property/schema 四类消息。
- `deploy_config_file("squirrel.yaml", "config_version")` 用版本键决定是否把前端配置部署到用户目录，既保证更新又尊重用户定制。

---

## 7. 下一步学习建议

本讲把「引擎如何启动」讲完了，但启动后真正的输入处理还没开始。建议按以下顺序继续：

1. **下一讲 [u2-l3 输入控制器生命周期与会话](u2-l3-input-controller-lifecycle.md)**：引擎就绪后，每个输入焦点会创建一个 `SquirrelInputController` 和一个 librime session。学完本讲再看会话生命周期，就能把「全局引擎」和「每会话状态」对上号。
2. **配置方向**：本讲提到 `loadSettings` 读 `squirrel.yaml`，想深入了解配置结构，可跳读 [u3-l1 SquirrelConfig](u3-l1-squirrel-config.md) 与 [u3-l2 squirrel.yaml](u3-l2-squirrel-yaml.md)。
3. **桥接方向**：如果对 `rimeStructInit`、`setCString`、`data_size` 这些 C 桥接约定意犹未尽，直接读 [u5-l4 Swift/C 桥接约定](u5-l4-bridging-conventions.md)。
4. **源码延伸**：对照阅读 [sources/SquirrelApplicationDelegate.swift:81-91](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L81-L91) 的 `deploy()` 与 `syncUserData()`，它们复用了本讲的三阶段，是巩固理解的好材料。
