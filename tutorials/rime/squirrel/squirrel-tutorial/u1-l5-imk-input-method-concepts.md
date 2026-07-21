# macOS 输入法（IMK）基础概念

## 1. 本讲目标

本讲是第一单元（入门）的收尾，目标是帮你建立「macOS 输入法到底是怎么跑起来的」这套宏观认知。读完本讲你应该能够：

- 说清楚 `IMKServer` 是什么，以及它为什么需要一个「连接名」（connection name）。
- 解释 Squirrel 在 `Info.plist` 里注册的简体（Hans）与繁体（Hant）两个输入模式各自代表什么、有什么区别。
- 理解 `IMKInputController`（控制器）与 `client`（客户端）的关系，并能解释为什么 `client` 必须用 `weak` 弱引用。
- 区分输入法协议里的两种核心文本操作：**marked text（标记文本 / 预编辑文本）** 与 **commit text（提交文本 / 上屏文本）**，并知道它们分别对应代码里的哪个调用。

本讲只讲「输入法与系统是怎么对接的」这一层框架知识，**不**深入按键处理主循环——那是第二单元（u2-l4）的主题。本讲为第二单元打地基：只有先理解了 controller/client/marked/commit 这些 IMK 协议概念，后面读 `handle(_:client:)` 时才不会在框架细节里迷路。

## 2. 前置知识

在开始之前，请确认你已经理解了 [u1-l1（项目定位）](u1-l1-project-overview.md) 里建立的几个关键事实，本讲会直接使用它们：

- **前端 vs 引擎**：Squirrel 是 macOS 上的「前端」，负责收键盘事件、画面板、把文字送回应用；真正的「按键 → 汉字」转换由引擎 **librime** 完成。
- **IMK（InputMethodKit）**：Apple 提供的输入法开发框架，Squirrel 通过它与 macOS 对接。
- **入口**：程序从 [sources/Main.swift](../sources/Main.swift) 的 `@main struct SquirrelApp` 启动（见 [u1-l4](u1-l4-entry-and-startup.md)）。

本讲会用到两个新名词，先在这里点一下，后面详细展开：

| 名词 | 一句话解释 |
|------|-----------|
| **IMKServer** | 输入法进程暴露给系统的「服务端」，系统通过它把按键事件派发给输入法。 |
| **输入模式（input mode）** | 一个输入法可以注册多个模式（如简体/繁体），系统菜单里会分别列出。 |
| **controller** | 处理某次输入会话的对象（`IMKInputController` 的子类），即 `SquirrelInputController`。 |
| **client** | 正在输入的那个应用/文本框（`IMKTextInput`），输入法要把文字回传给它。 |
| **marked text** | 还在编辑中的临时文本（带下划线那种），可以继续改。 |
| **commit text** | 最终敲定的文字，真正「上屏」进应用文档。 |

> 名词提示：初学者常把「controller」和「client」搞混。一个简单的记忆法是：**controller 属于输入法，client 属于应用**。输入法（controller）收到按键，转换出文字，再「喂」给应用（client）。

## 3. 本讲源码地图

本讲只涉及两个文件，都很短：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `resources/Info.plist` | 输入法 bundle 的声明文件，告诉 macOS「我是一个输入法，我有这些模式」 | 连接名、两个输入模式、controller 类名 |
| `sources/SquirrelInputController.swift` | 处理按键、消费 librime 状态、把文本送回应用的控制器实现 | 弱引用 client、marked/commit 文本调用 |

辅助参考（已在 u1-l4 详读，本讲只引用关键两行）：

| 文件 | 本讲关注点 |
|------|-----------|
| `sources/Main.swift` | 创建 `IMKServer` 的那两行 |
| `sources/BridgingFunctions.swift` | `?=` 运算符的定义（u5-l4 会详述） |

## 4. 核心概念与源码讲解

### 4.1 IMKServer 与 InputMethodConnectionName

#### 4.1.1 概念说明

一个 macOS 输入法本质上是一个**后台常驻进程**。它并不直接挂钩在每个应用上，而是以「服务」的形式运行；系统里负责输入法的子系统（历史上由 `imklaunchd` 之类进程调度）在有应用需要输入时，通过一个 **Mach 连接**去找到这个服务、把按键事件递给它。

`IMKServer` 就是这个「服务端点」的抽象类。一个输入法进程启动后，要做的第一件事之一就是创建一个 `IMKServer` 实例，相当于「开门营业」。

但系统怎么知道该连哪一个服务？答案是靠一个**连接名（connection name）**——一个字符串。输入法在 `Info.plist` 里用键 `InputMethodConnectionName` 声明这个名字；启动时用同一个名字创建 `IMKServer`；系统也通过这个名字来寻址。三者必须对上号，连接才能建立。

> 直觉类比：`IMKServer` 像是一家店铺，`InputMethodConnectionName` 是它的招牌。系统（顾客）按招牌找店；招牌对不上，生意就做不成。

#### 4.1.2 核心流程

输入法被系统识别并连接的过程可以概括为：

1. macOS 通过 `Info.plist` 判定这个 bundle 是输入法（关键字段 `InputMethodConnectionName`、`InputMethodServerControllerClass` 等）。
2. 用户在「系统设置 → 键盘 → 输入法」里勾选了 Squirrel，系统注册其输入源（`TISInputSourceID`，详见 4.2 与 u5-l2）。
3. 某应用获得焦点、需要输入时，系统按连接名找到正在运行的 Squirrel 进程里的 `IMKServer`。
4. `IMKServer` 为这个应用创建一个 `SquirrelInputController` 实例（见 4.3），后续按键事件就派发给它。
5. 进程退出时，`IMKServer` 随之销毁，所有 controller 一并清理。

#### 4.1.3 源码精读

**连接名在 `Info.plist` 里声明**（值为 `Squirrel_Connection`）：

[resources/Info.plist:92-93](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L92-L93) —— 定义 `InputMethodConnectionName = "Squirrel_Connection"`。这是连接名的「权威来源」。

**进程启动时读取这个名字并创建 `IMKServer`**（见 [u1-l4](u1-l4-entry-and-startup.md) 对启动流程的完整分析）：

[sources/Main.swift:127-128](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L127-L128) —— 从 `Info.plist` 读出连接名，用它构造 `IMKServer`。注意这里用 `_ =` 丢弃了返回值：`IMKServer` 一旦创建就会被 IMK 框架内部强引用并接管生命周期，Squirrel 自己不需要再持有它。

```swift
let connectionName = main.object(forInfoDictionaryKey: "InputMethodConnectionName") as! String
_ = IMKServer(name: connectionName, bundleIdentifier: main.bundleIdentifier!)
```

> 设计要点：Squirrel 没有把连接名硬编码进 Swift 代码，而是从 `Info.plist` 动态读取。这样连接名只在一处定义（plist），代码与配置不会失同步——这是「单一事实来源（single source of truth）」的小体现。

#### 4.1.4 代码实践

**实践目标**：确认连接名在「plist 声明」与「代码读取」两处一致，理解寻址机制。

**操作步骤**：

1. 打开 `resources/Info.plist`，定位 `InputMethodConnectionName`，记下它的值。
2. 打开 `sources/Main.swift` 第 127 行，确认代码是用 `object(forInfoDictionaryKey: "InputMethodConnectionName")` 读这个名字，而不是写死字符串。
3. 思考：如果把 plist 里的值改成 `Squirrel_Connection_X`，但代码不动，会发生什么？

**需要观察的现象 / 预期结果**：

- plist 值为 `Squirrel_Connection`。
- 代码动态读取，无硬编码。
- 若两侧不一致：`IMKServer` 监听的名字与系统期望的名字不匹配，系统连不上服务，表现为「输入法装上了但按键没反应」。这是输入法开发里最常见的「连线」错误之一。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Squirrel 用 `_ = IMKServer(...)` 丢弃返回值，而不像 `let app = NSApplication.shared` 那样把它存到一个属性里？

> **参考答案**：`IMKServer` 创建后会被 InputMethodKit 框架内部强引用并接管，它的生命周期与进程绑定；Squirrel 不需要（也不应该）自己额外持有。存到属性里反而是多余的强引用。而 `NSApplication.shared` 在后面还要频繁使用（设置 delegate、`app.run()`），所以要留引用。

**练习 2**：连接名 `Squirrel_Connection` 的命名里有个下划线，为什么输入法之间要用各自唯一的连接名？

> **参考答案**：连接名是系统寻址输入法服务的「地址」。多个输入法进程会并存，若连接名撞车，系统会把按键派发给错误的服务，造成串扰。所以每个输入法用自己唯一的连接名。

---

### 4.2 输入模式（Hans/Hant）与 controller 类名

#### 4.2.1 概念说明

一个输入法 bundle **不限于只代表一种输入**。它可以在 `Info.plist` 的 `ComponentInputModeDict` 里声明多个「输入模式（input mode）」，每个模式对应一种语言/书写系统的变体。系统会在输入法菜单里把每个模式作为独立的可选项展示，用户能在它们之间切换。

Squirrel 声明了**两个**模式，正好对应中文的两大书写体系：

| 输入模式 ID | 含义 | 目标语言 |
|------------|------|---------|
| `im.rime.inputmethod.Squirrel.Hans` | 简体（Hans = Han Simplified） | `zh-Hans` |
| `im.rime.inputmethod.Squirrel.Hant` | 繁体（Hant = Han Traditional） | `zh-Hant` |

> 术语提示：`Hans` / `Hant` 是 Unicode 与 BCP 47 语言标签里对「简体汉字 / 繁体汉字」的标准缩写，不是 Squirrel 自创的。`zh-Hans` 即「简体中文」，`zh-Hant` 即「繁体中文」。

另一个关键声明是 `InputMethodServerControllerClass`：它告诉系统「当按键事件到来时，要实例化哪个类来处理」。Squirrel 指向 `Squirrel.SquirrelInputController`（`模块名.类名` 的格式）。这正是我们在 [u1-l2](u1-l2-repo-structure.md) 里看到的 `sources/SquirrelInputController.swift` 所定义的类。

#### 4.2.2 核心流程

输入模式在 plist 里的组织是一个嵌套字典：

```
ComponentInputModeDict
└── tsInputModeListKey (字典：模式ID → 模式属性)
    ├── im.rime.inputmethod.Squirrel.Hans  (简体)
    └── im.rime.inputmethod.Squirrel.Hant  (繁体)
└── tsVisibleInputModeOrderedArrayKey (数组：菜单显示顺序)
```

每个模式属性字典里的关键字段含义：

| 键 | 含义 |
|----|------|
| `TISInputSourceID` | 该模式的输入源唯一 ID（TIS = Text Input Source，系统输入源管理） |
| `TISIntendedLanguage` | 该模式面向的语言（`zh-Hans` / `zh-Hant`） |
| `tsInputModeDefaultStateKey` | 是否默认启用（`true`=启用，`false`=装上但默认不勾选） |
| `tsInputModePrimaryInScriptKey` | 是否是该书写系统下的「主模式」（影响系统菜单分组） |
| `tsInputModeScriptKey` | 书写系统（`smUnicodeScript`） |
| `tsInputModeCharacterRepertoireKey` | 该模式可输入的字符集范围（如 `Hans`、`Hant`） |

`tsVisibleInputModeOrderedArrayKey` 是一个数组，决定两个模式在系统菜单里的排列顺序。

#### 4.2.3 源码精读

**整体输入源 ID**（bundle 级，不是模式级）：

[resources/Info.plist:5-6](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L5-L6) —— `TISInputSourceID = im.rime.inputmethod.Squirrel`。这是整个输入法在系统里的「根」ID，两个模式 ID 都是在它后面加 `.Hans` / `.Hant` 后缀。

**简体模式 Hans 的完整声明**：

[resources/Info.plist:29-56](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L29-L56) —— 注意其中 `tsInputModeDefaultStateKey` 为 `true`（第 42-43 行），表示安装后简体模式**默认启用**；`tsInputModePrimaryInScriptKey` 为 `true`（第 52-53 行），表示简体是简体书写系统下的主模式。

**繁体模式 Hant 的完整声明**：

[resources/Info.plist:57-84](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L57-L84) —— 与 Hans 结构相同，但 `tsInputModeDefaultStateKey` 为 `false`（第 70-71 行），说明繁体模式安装后**默认不勾选**，用户需要手动在系统设置里开启。

**菜单显示顺序**：

[resources/Info.plist:86-90](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L86-L90) —— `tsVisibleInputModeOrderedArrayKey` 数组先 Hans 后 Hant，决定了系统菜单里的排列。

**controller 类名**（系统据此实例化处理器）：

[resources/Info.plist:94-97](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L94-L97) —— `InputMethodServerControllerClass` 与 `InputMethodServerDelegateClass` 都指向 `Squirrel.SquirrelInputController`。注意这里同一个类既当 controller 又当 delegate——Squirrel 让 `SquirrelInputController` 同时承担两个角色。

> 旁注（与本讲主题相关、细节留给 [u5-l2](u5-l2-input-source-registration.md)）：[resources/Info.plist:110-111](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L110-L111) 设了 `TICapsLockLanguageSwitchCapable = true`，意思是允许用户用 Caps Lock 键在本输入法与上一个英文键盘之间切换。这也是为什么后面 `handle` 里要对 capslock 做特殊处理（见 u2-l4）。

#### 4.2.4 代码实践

**实践目标**：对照真实 `Info.plist`，把两个模式的差异讲清楚。

**操作步骤**：

1. 在 `resources/Info.plist` 中分别找到 Hans 与 Hant 两个字典。
2. 对比两者的 `tsInputModeDefaultStateKey`、`TISIntendedLanguage`、`tsInputModeCharacterRepertoireKey` 三个字段。
3. 找到 `InputMethodServerControllerClass`，确认它指向的类名与 `sources/SquirrelInputController.swift` 第 10 行的类声明是否一致。

**需要观察的现象 / 预期结果**：

| 字段 | Hans | Hant |
|------|------|------|
| `TISIntendedLanguage` | `zh-Hans` | `zh-Hant` |
| `tsInputModeDefaultStateKey` | `true` | `false` |
| 字符集范围 | Hans, Hant | Hant, Hans |
| controller 类 | `Squirrel.SquirrelInputController` | 同上（两个模式共用一个 controller 类） |

关键结论：**两个模式共用同一个 `SquirrelInputController` 类**，简体/繁体的区分对 IMK 层来说只是「两个入口标签」，真正的简繁行为差异由 librime 的输入方案（schema）决定——这正是 u1-l1 强调的「前端只管收发，引擎才管转换」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Squirrel 不为简体和繁体各写一个 controller 类，而是共用一个？

> **参考答案**：因为对前端而言，简繁的处理流程完全一样——收按键、转发给 librime、画面板、上屏。真正的简繁区别在 librime 加载的输入方案（schema）里。共用一个 controller 避免了代码重复，符合「前端薄、引擎厚」的分工。

**练习 2**：用户刚装好 Squirrel 后，繁体模式默认不会出现在输入法菜单里勾选，需要手动开。哪个 plist 字段控制这件事？

> **参考答案**：Hant 模式字典里的 `tsInputModeDefaultStateKey = false`（[Info.plist:70-71](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L70-L71)）。Hans 对应值为 `true`，所以简体默认启用。

---

### 4.3 IMKInputController 与弱引用 client

#### 4.3.1 概念说明

`IMKInputController` 是输入法处理一次输入会话的「控制器」。每当一个应用（更准确地说是应用里某个接收文本的窗口/文本框）需要输入，`IMKServer` 就会为它**实例化一个 controller**。Squirrel 的实现就是 `SquirrelInputController`：

[sources/SquirrelInputController.swift:10](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L10) —— `final class SquirrelInputController: IMKInputController`。

controller 持有一个关键引用 `client`，类型是 `IMKTextInput`，它**代表「当前正在输入的那个应用/文本框」**。输入法把转换好的文字、临时预编辑文本，都是通过这个 `client` 回传给应用的。可以说：controller 是输入法这边的「接线员」，client 是通向应用那边的「电话线」。

**为什么 `client` 必须是 `weak`（弱引用）？**

因为 `client` 对象的生命周期**由系统与宿主应用管理，不属于输入法**。设想：

- 用户在某应用的文本框输入一会儿，然后切走或关闭了那个应用。
- 此时系统会销毁对应的 client 对象。
- 如果输入法用 `strong`（强引用）持有 client，输入法就成了最后一个握住它的人，client 对象无法被释放 → **内存泄漏**。
- 更糟的是，若输入法随后还去用这个 client（比如想给一个已经不存在的文本框上屏），就会访问到状态错乱甚至已释放的对象 → **悬垂引用**。

用 `weak` 后，当系统释放 client 时，输入法这边这个引用会**自动变成 `nil`**，安全无害。所以代码里每次用 client 之前都要先 `guard let client = client` 守卫一下。

> 直觉类比：client 就像你借来用的别人的工具。工具的原主（系统/应用）随时可能要回去。弱引用相当于「我借用，但不占有」——主人要回去了，你这边的借条自动作废（变 nil），不会死抱着不放。

#### 4.3.2 核心流程

`SquirrelInputController` 与 client 交互的典型生命周期：

1. **创建**：系统调 `init(server:delegate:client:)`，controller 把传入的 client 弱引用存起来，并立即创建一个 librime 会话（session）。
2. **运行中**：每次 `handle(_:client:)` 收到事件，先**刷新** client 引用（因为同一个 controller 在不同时刻可能服务不同的 client 实例），再做守卫后使用。
3. **每次使用前**：`guard let client = client else { return }`——若 client 已被释放就什么也不做。
4. **失活**：`deactivateServer(_:)` 被调用时，显式把 client 置为 `nil`，表示「这条线挂断了」。
5. **销毁**：controller 析构（`deinit`）时销毁对应的 librime session。

#### 4.3.3 源码精读

**弱引用声明**：

[sources/SquirrelInputController.swift:14](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L14) —— `private weak var client: IMKTextInput?`。注意 `weak` 关键字和可选类型 `?`（弱引用必须用可选类型，因为它可能变 nil）。

**创建时持有弱引用**：

[sources/SquirrelInputController.swift:188-191](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L188-L191) —— `init(server:delegate:client:)` 中把传入的 client 转为 `IMKTextInput` 后弱引用保存，然后调用父类初始化并创建 librime session。

```swift
override init!(server: IMKServer!, delegate: Any!, client: Any!) {
  self.client = client as? IMKTextInput
  super.init(server: server, delegate: delegate, client: client)
  createSession()
  ...
}
```

**运行中刷新 client**（`?=` 是项目自定义运算符）：

[sources/SquirrelInputController.swift:47](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L47) —— `self.client ?= sender as? IMKTextInput`。

这里的 `?=` 不是 Swift 标准运算符，而是 Squirrel 自己定义的「可选赋值」运算符（见 [sources/BridgingFunctions.swift:44-49](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L44-L49)）：**当右侧非 nil 时才赋值给左侧**。也就是说，如果这次事件传进来的 sender 转成 `IMKTextInput` 成功，就更新 client；若是 nil（比如没拿到有效 client），就保留旧值。这是「尽力更新、失败不破坏」的稳健写法。（该运算符的完整约定会在 u5-l4 详述。）

同样的刷新也出现在 [activateServer](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L168)（第 168 行）里。

**使用前守卫**：

[sources/SquirrelInputController.swift:551-556](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L551-L556) —— `commit(string:)` 一开头就 `guard let client = client else { return }`。如果 client 已经被释放，整个上屏动作直接放弃，绝不冒险解包。`show(...)` 方法（第 559 行）也有同样的守卫。

**失活时清理**：

[sources/SquirrelInputController.swift:210-214](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L210-L214) —— `deactivateServer(_:)` 做三件事：隐藏面板 → 把未提交的输入上屏 → **把 client 置 nil**。

```swift
override func deactivateServer(_ sender: Any!) {
  hidePalettes()
  commitComposition(sender)
  client = nil
}
```

> 这正是 [u2-l3（输入控制器生命周期）](u2-l3-input-controller-lifecycle.md) 会展开讲的核心清理逻辑：失活时主动断开 client 引用，配合 weak 声明形成「双重保险」。

#### 4.3.4 代码实践

**实践目标**：在源码里把「弱引用 client」的完整防护链找出来。

**操作步骤**：

1. 在 `sources/SquirrelInputController.swift` 搜索 `client`，统计它出现在多少处。
2. 重点看 4 个位置：声明（第 14 行）、初始化（第 189 行）、`?=` 刷新（第 47 行、第 168 行）、`guard let client` 守卫（第 552、559 行）、置 nil（第 213 行）。
3. 思考：如果把第 14 行的 `weak` 去掉，会有什么风险？

**需要观察的现象 / 预期结果**：

- `client` 是贯穿整个文件的核心字段。
- 每个真正「使用」client 的方法（`commit`、`show`、`showPanel`、`activateServer` 等）开头都有 `guard let client = client` 或 `client?.xxx` 的可选链调用。
- 去掉 `weak`：输入法会强引用 client，应用关闭后 client 无法释放（泄漏）；若再使用还可能访问悬垂对象。这是 IMK 编程的典型陷阱。

#### 4.3.5 小练习与答案

**练习 1**：Swift 里 `weak` 引用为什么必须是 `var` 且是可选类型？

> **参考答案**：弱引用不增加对象的引用计数，当对象被销毁时，弱引用必须能被自动改写为 `nil`，所以它必须是可变的（`var`）且类型为可选（`Optional`，用 `?` 表示）。常量（`let`）无法在运行时被改写，非可选类型无法承载 nil，因此都不能用作 weak。

**练习 2**：`self.client ?= sender as? IMKTextInput` 这行，为什么不直接写 `self.client = sender as? IMKTextInput`？

> **参考答案**：直接赋值时，如果 `sender as? IMKTextInput` 为 nil（这次没拿到有效 client），会把原本可能还有效的旧 client 引用覆盖成 nil，导致后续上屏失败。`?=` 只在右侧非 nil 时才赋值，保留了「拿不到新的就先用着旧的」的兜底，更稳健。

---

### 4.4 marked text / commit text 协议

#### 4.4.1 概念说明

输入法要把文字交给应用，靠的不是直接往文档里写字，而是遵循 **Cocoa 文本输入协议** 的两种「文本操作」。理解这两种操作，是理解整个中文输入体验的钥匙。

**commit text（提交文本 / 上屏文本）**

- 调用：`client.insertText(_:replacementRange:)`
- 含义：**最终敲定**的文字。一旦 insert，文字就真正进入应用文档，光标前进，不可撤销回输入法态。
- 时机：用户选定了候选词、或按了回车/空格确认、或输入法决定把原始按键直接放行（直通模式）。

**marked text（标记文本 / 预编辑文本）**

- 调用：`client.setMarkedText(_:selectionRange:replacementRange:)`
- 含义：**还在编辑中**的临时文字。这段文字在应用里以「待定」样式显示（常见为带下划线或高亮底色），用户可以继续输入修改它。它**还没真正进入文档**，只是「贴」在光标处占位。
- 时机：用户正在敲拼音、编码还没确认时。比如输入 `nihao`，应用里会显示带下划线的 `你好` 候选预览或 `nihao` 编码，候选窗口跟在后面移动。

**两者的关系**：

- 输入过程中反复 `setMarkedText` 更新预编辑态。
- 用户确认后，先（可能）清掉 marked text，再 `insertText` 上屏。
- 一个常见的中文输入链：敲 `n` → `setMarkedText("n")` → 敲 `i` → `setMarkedText("ni")` → … → 选「你」 → `insertText("你")`。

> 直觉类比：commit 是「寄出的信」（落地为安），marked 是「还在写的草稿」（随时能改、能撤）。marked text 让中文输入的「光标跟随、行内预览」成为可能——临时文本就贴在应用文档里光标的位置，而不是孤零零地飘在一个独立浮窗里。

**为什么 marked text 这么重要？**

如果没有 marked text 机制，输入法只能把临时内容画在自己的候选面板里，应用文档里光标位置就看不到正在输入什么，体验割裂。有了 marked text，应用「配合」地把这段临时文本以可识别的样式渲染在原位，输入法再叠加一个候选浮窗，两者视觉上连成一体——这就是我们习以为常的中文输入体验。

#### 4.4.2 核心流程

输入法在一次输入会话里对这两种文本的典型使用顺序：

```
用户按键
   │
   ▼
handle(_:client:)          ← u2-l4 详讲的事件主循环
   │  转发给 librime.process_key
   ▼
rimeUpdate()               ← u2-l6 详讲的状态消费
   │
   ├─ 有最终结果？─是→ commit(string:)  → client.insertText   (commit text)
   │
   └─ 还在编辑中？─是→ show(...)        → client.setMarkedText (marked text)
```

注意 `show` 还做了一个**去重缓存**：如果本次要显示的 preedit 文本、选区、光标位置都和上次一模一样，就直接 return，不重复调用 `setMarkedText`——避免对应用的冗余刷新。

#### 4.4.3 源码精读

**commit text（上屏）**：

[sources/SquirrelInputController.swift:551-556](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L551-L556) —— `commit(string:)` 守卫 client 后调用 `client.insertText(string, replacementRange: .empty)`，这就是 commit text 操作。之后清空本地 preedit 缓存并隐藏面板。

```swift
func commit(string: String) {
  guard let client = client else { return }
  client.insertText(string, replacementRange: .empty)
  preedit = ""
  hidePalettes()
}
```

**marked text（预编辑）**：

[sources/SquirrelInputController.swift:558-578](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L558-L578) —— `show(preedit:selRange:caretPos:)` 方法。开头先和上次的状态比对（去重，第 560-562 行），相同就 return；不同则更新缓存，构造一个带高亮样式的 `NSAttributedString`，最后第 577 行调用 `client.setMarkedText(...)` 把它作为 marked text 交给应用。

第 571、575 行用到的 `mark(forStyle:...)` 是 `IMKInputController` 父类提供的样式询问方法：`kTSMHiliteConvertedText` 表示「已转换部分」的高亮样式，`kTSMHiliteSelectedRawText` 表示「选中原始文本」的高亮样式。应用据此用不同视觉区分 preedit 里已经转成汉字的部分和还在编码的部分。

> 这段高亮/去重逻辑会在 [u2-l7（marked text、commit 与 inline 策略）](u2-l7-marked-text-commit.md) 里完整展开。本讲只需建立「`setMarkedText` = marked text 操作」这个对应关系。

**一个值得注意的小细节**——非 inline 模式下用全角空格占位：

[sources/SquirrelInputController.swift:513-516](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L513-L516) —— 当不采用行内预编辑时，Squirrel 仍会调一次 `setMarkedText`，但内容是一个全角空格 `　`（U+3000）占位符，而不是真实的 preedit。这是为了让光标在应用里「占住一个位置」好让候选面板跟过来，同时避免半角空格导致 iTerm2 等终端的回显抖动。这正好印证了 marked text 的「占位」本质。

#### 4.4.4 代码实践

**实践目标**：在源码里把 marked / commit 两条路径分别定位出来，建立「调用 → 协议动作」的映射。

**操作步骤**：

1. 在 `sources/SquirrelInputController.swift` 中搜索 `insertText`，确认它只出现在 `commit(string:)` 里（commit text 路径）。
2. 搜索 `setMarkedText`，确认它只出现在 `show(...)` 里（marked text 路径）。
3. 顺着调用关系：`rimeUpdate()` 什么情况下调 `commit`，什么情况下调 `show`？（提示：`rimeConsumeCommittedText` 拿到最终结果就 commit；否则根据 inline 策略走 show）。
4. 回忆你平时用中文输入法的过程，把「敲拼音看到下划线文字」对应到 `setMarkedText`，「选词后文字进文档」对应到 `insertText`。

**需要观察的现象 / 预期结果**：

- `insertText` → commit text（最终上屏）。
- `setMarkedText` → marked text（临时预编辑）。
- 两者都通过 `client` 调用，都前置了 `guard let client = client` 守卫。
- `show` 有去重缓存，`commit` 没有（因为上屏是不可逆的确定动作，每次都要执行）。

> 待本地验证：上述调用点可以通过静态阅读确认；若你想观察运行时行为，可在 Mac 上对 `commit` 和 `show` 各加一行 `print` 日志，然后真实输入观察控制台输出顺序（本仓库是 macOS 项目，Linux 环境下无法运行，标注为待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：假设用户在备忘录里输入拼音 `nihao`，还没选词就按了 Esc 取消。整个过程中，输入法对备忘录调用过哪些文本操作？文档里最后留下了什么？

> **参考答案**：输入 `n`、`i`、`h`、`a`、`o` 的过程中，输入法反复调用 `setMarkedText` 更新预编辑态（备忘录里显示带下划线的临时内容）。按 Esc 取消后，输入法会清掉 marked text（通常是用空内容再 setMarkedText 或直接结束），但**从未调用 `insertText`**。所以文档里最终什么也没留下——因为从未真正上屏。这正是 marked text「可撤销、可取消」的特性。

**练习 2**：为什么 `show(...)` 要做去重缓存（连续两次相同内容就 return），而 `commit(string:)` 不需要？

> **参考答案**：marked text 在快速连续输入时会被高频调用，且大部分相邻调用内容变化不大；去重能避免对应用的冗余刷新，提升性能。而 commit 是「敲定上屏」的确定动作，每次都代表一次真实确认，应当每次都执行，不能因为「和上次内容相同」就跳过（比如用户可能连续上屏两个相同的词）。

---

## 5. 综合实践

把本讲的四个模块串起来，完成下面这个「端到端追踪」小任务。这是一个**源码阅读型实践**（仓库为 macOS 项目，当前 Linux 环境无法实际运行，重在建立全局认知）。

**任务**：追踪「一个按键事件，从系统到达 Squirrel，再到应用」的完整 IMK 层路径，并在每一步标注本讲学到的概念。

**操作步骤**：

1. **连线（4.1）**：阅读 [sources/Main.swift:127-128](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L127-L128)，确认系统是通过连接名 `Squirrel_Connection`（来自 [Info.plist:92-93](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L92-L93)）找到 Squirrel 的 `IMKServer`。

2. **派发（4.2 / 4.3）**：系统根据 [Info.plist:94-95](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L94-L95) 的 `InputMethodServerControllerClass`，实例化一个 `SquirrelInputController`。对照 [SquirrelInputController.swift:188-191](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L188-L191)，理解 controller 弱引用持有 client（第 14 行 `weak`）。

3. **收事件（4.3）**：按键进入 [handle(_:client:)](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L32)（第 32 行），第 47 行用 `?=` 刷新 client。（按键如何被翻译并交给 librime，是 u2-l4/u2-l5 的主题，本步先跳过细节。）

4. **回传文本（4.4）**：librime 处理后，`rimeUpdate()` 决定走哪条路——有最终结果就 [commit(string:)](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L551-L556)（→ `insertText`，commit text），否则 [show(...)](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L558-L578)（→ `setMarkedText`，marked text）。

**产出**：画一张包含以下节点的流程图（手绘或文字版均可）：

```
系统 ──(按连接名)──▶ IMKServer ──(实例化)──▶ SquirrelInputController(weak client)
                                                          │
                                              handle(_:client:) 收按键
                                                          │
                                              (转发 librime，u2 详讲)
                                                          │
                                              rimeUpdate() 消费状态
                                                    ┌─────┴─────┐
                                              insertText    setMarkedText
                                              (commit)       (marked)
                                                    └─────┬─────┘
                                                          ▼
                                                       应用文档
```

**预期结果**：你能用本讲学到的 6 个术语（IMKServer、连接名、输入模式、controller、client、marked/commit text）把这张图每一条边都解释清楚。完成后，你就具备了进入第二单元（输入处理主链路）所需的全部框架知识。

## 6. 本讲小结

- **IMKServer** 是输入法进程暴露给系统的服务端点，靠 **连接名**（Squirrel 的是 `Squirrel_Connection`）与系统寻址对齐；连接名在 `Info.plist` 声明、代码动态读取，必须一致。
- **输入模式**：Squirrel 注册了简体 `...Squirrel.Hans` 与繁体 `...Squirrel.Hant` 两个模式，共用同一个 `SquirrelInputController` 类；简繁差异由 librime 的 schema 决定，前端不区分。
- **controller vs client**：`SquirrelInputController` 是输入法这边的控制器，`client`（`IMKTextInput`）是通向应用的接线员；`client` 因生命周期不属于输入法而**必须用 `weak`**，使用前要守卫。
- **`?=` 运算符**是项目自定义的「可选赋值」，用于稳健地刷新 client 引用。
- **marked text（`setMarkedText`）= 还在编辑的临时文本**（带下划线、可改可撤）；**commit text（`insertText`）= 最终上屏**。两者构成 Cocoa 文本输入协议的核心。
- 本讲只覆盖 IMK 框架层；按键如何被翻译、librime 状态如何被消费，是第二单元的主线。

## 7. 下一步学习建议

本讲建立的是「输入法与系统怎么对接」的框架认知，**有意没碰按键处理的内部细节**。接下来按依赖关系推荐：

1. **第二单元 u2-l1（应用委托与全局状态）**：先看 `SquirrelApplicationDelegate` 持有哪些全局对象（panel、config、statusItem），理解 controller 运行时所依赖的全局环境。
2. **第二单元 u2-l3（输入控制器生命周期）**：深入 `SquirrelInputController` 的 `createSession`/`activateServer`/`deactivateServer`/`deinit`，把本讲的「弱引用 client」放进完整生命周期里理解。
3. **第二单元 u2-l4（键盘事件处理主循环）**：精读 `handle(_:client:)`，看一个 `NSEvent` 是如何被翻译并交给 librime 的——这是 Squirrel 的灵魂主链路。
4. **第二单元 u2-l7（marked text、commit 与 inline 策略）**：把本讲的 marked/commit 协议与 inline 预编辑策略、全角空格占位符等细节彻底讲透。

如果你对「输入法如何被系统注册、启用、选中」更感兴趣，也可以先跳到 **u5-l2（输入源注册 TIS）**，那里会展开 `TISInputSourceID`、Hans/Hant 模式的 enable/select 流程，与本讲的 4.2 节直接呼应。
