# 输入源注册（TIS）

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 macOS 里「输入法」的两套系统接口——IMK（运行时收键）与 TIS（系统输入源注册表）——分别管什么，以及它们如何分工协作。
- 解释 Squirrel 注册的两种输入模式 `Hans`（简体）与 `Hant`（繁体）的区别，以及「默认主模式（primary）」的含义。
- 读懂 `SquirrelInstaller` 的 `register / enable / disable / select` 四个生命周期操作，并理解它们由 `TISRegisterInputSource / TISEnableInputSource / TISDisableInputSource / TISSelectInputSource` 四个 Carbon API 支撑。
- 掌握 `currentInputSourceID()` 查询当前激活输入源的用法，以及它在「状态栏图标显隐」和「悬挂组合兜底」中的运行时作用。
- 理解 `resources/Info.plist` 中的 `TISInputSourceID` 与 `sources/InputSource.swift` 中的 `InputMode` 为什么必须一字不差地保持一致。

## 2. 前置知识

本讲是专家层，但只依赖两个文件（`InputSource.swift` 与 `Info.plist`），概念上承接第一单元 u1-l5（IMK 基础）。在继续前，请确认你理解下面几个词：

- **IMK（InputMethodKit）**：macOS 提供的「输入法开发框架」。Squirrel 在运行时靠 `IMKServer` 暴露服务端点，靠 `SquirrelInputController` 接收键盘事件。这是 u1-l5 已经建立的认知。
- **TIS（Text Input Source）**：属于更底层的 Carbon 框架（HIToolbox）。它是 macOS 维护的「所有键盘与输入法的总注册表」——系统设置里的「键盘 → 输入法（Input Sources）」列表，每一项就是一个 TIS 输入源。本讲的主角就是 TIS。
- **输入源 ID（Input Source ID）**：每个 TIS 输入源都有一全局唯一的字符串标识，例如 `com.apple.keylayout.US`（美式键盘）、`im.rime.inputmethod.Squirrel.Hans`（鼠鬚管简体模式）。
- **bundle（应用包）**：即 `Squirrel.app` 这个目录，`Info.plist` 是它的「身份证」，描述了这个 App 是什么、注册了哪些输入模式。

一句话区分：**IMK 让 Squirrel 在运行时能收到按键；TIS 让 Squirrel 在系统里「挂号」，出现在输入法菜单里、能被用户选中。** 两者缺一不可。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [sources/InputSource.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/InputSource.swift) | 定义 `SquirrelInstaller` 类，封装全部 TIS 操作：注册、启用、禁用、选中、查询。本讲核心文件。 |
| [resources/Info.plist](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist) | Squirrel 的应用身份证。声明了顶层 `TISInputSourceID` 与 `ComponentInputModeDict`（两个输入模式 Hans/Hant），是 TIS 注册的数据来源。 |
| [sources/Main.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift) | 程序入口。把 `--register-input-source / --enable-input-source / --disable-input-source / --select-input-source` 等命令行参数映射到 `SquirrelInstaller` 的方法，并定义 `appDir`（App 安装路径）。 |
| [scripts/postinstall](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/scripts/postinstall) | 安装 `.pkg` 后执行的脚本，串起「注册 → 预编译 → 启用 → 选中」的标准安装时序。 |
| [sources/SquirrelApplicationDelegate.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift) | 运行时调用 `currentInputSourceID()` 的两处：状态栏图标显隐、悬挂组合兜底。 |

---

## 4. 核心概念与源码讲解

### 4.1 概念：TIS 输入源与三层生命周期

#### 4.1.1 概念说明

一个输入法要让用户真正用上，要走过三道关卡，对应 TIS 的三个状态：

1. **注册（register）**：把 App 的 `Info.plist` 提交给系统的 TIS 数据库，告诉系统「世界上存在这样一种输入法，它叫这个 ID，提供这些输入模式」。注册后输入源才「存在于系统」，但**默认不启用、更不是当前输入法**。
2. **启用（enable）**：把某个输入源标记为「已启用」。启用后它才会出现在「系统设置 → 键盘 → 输入法」列表里，**用户能看见、能选**，但还不是当前激活的输入法。
3. **选中（select）**：把某个已启用的输入源设为**当前激活**的键盘输入源。此时系统的按键才会真正送给它（进而由 IMK 交给 `SquirrelInputController`）。

这三层是**严格递进**的：必须先注册才能启用，必须先启用才能选中。Squirrel 把这三层分别封装成 `SquirrelInstaller` 的三个方法，背后各对应一个 Carbon C 函数：

| Squirrel 方法 | Carbon API | 语义 |
| --- | --- | --- |
| `register()` | `TISRegisterInputSource` | 把 App 注册进 TIS 数据库 |
| `enable()` / `disable()` | `TISEnableInputSource` / `TISDisableInputSource` | 启用 / 禁用某输入源 |
| `select()` | `TISSelectInputSource` | 选中为当前输入法 |

注意一个容易混淆的点：`register` 只做一次「登记」就够，它**不等于**启用。这就是为什么安装脚本在 `--register-input-source` 之后，还要额外跑 `--enable-input-source` 和 `--select-input-source`（见 4.4.4）。

#### 4.1.2 核心流程

输入法从「装上硬盘」到「能用」的全流程：

```text
安装 .pkg
  │
  ▼  postinstall 脚本触发
Squirrel.app 已落盘到 /Library/... 下
  │
  ▼  --register-input-source
TISRegisterInputSource(Squirrel.app 的 URL)
  │   系统读取 Info.plist 的 ComponentInputModeDict
  │   把 Hans、Hant 两个模式写入 TIS 数据库
  ▼  此时：输入源「已注册」，但未启用
--enable-input-source
  │   TISEnableInputSource(Hans)
  ▼  此时：Hans 出现在输入法列表里，但未选中
--select-input-source
  │   TISSelectInputSource(Hans)
  ▼  此时：Hans 成为当前输入法
用户按键 → 系统按「当前输入源」路由 → IMKServer → SquirrelInputController
```

关键在于：**注册是一次性的「挂号」，而启用/选中是用户级偏好，会被系统记住。** 因此 `register()` 设计成幂等（重复注册无害），而 `enable()` 会刻意保留用户手动启用过的模式（见 4.4）。

#### 4.1.3 源码精读

`register()` 调用的核心 C 函数是 `TISRegisterInputSource`，它的实参是 App 安装目录的 URL：

[sources/InputSource.swift:42-50](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/InputSource.swift#L42-L50) —— `register()` 先查「是否已有模式被启用」，没有才调用 `TISRegisterInputSource(SquirrelApp.appDir as CFURL)` 把 App 注册进 TIS。

`SquirrelApp.appDir` 指向 App 在硬盘上的安装位置：

[sources/Main.swift:18-20](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L18-L20) —— 用 `withCString` + `URL(fileURLWithFileSystemRepresentation:)` 从 C 字符串构造 App 目录的 URL。

> 📌 待本地确认：该常量在源码中的字面值为 `"/Library/Input Library/Squirrel.app"`。按 macOS 约定，输入法的标准安装目录是 `/Library/Input Methods/`（即 `Squirrel.app` 通常位于 `/Library/Input Methods/Squirrel.app`，这也是 u1-l3 中 `install` 目标拷贝的目的地）。本讲只忠实引用源码字面值，不对二者差异做判断——在你的本地环境里应以实际安装路径为准。

而 App「是一个输入法」这件事，是由 `Info.plist` 中的两把钥匙声明的：

[resources/Info.plist:92-97](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L92-L97) —— `InputMethodConnectionName`（连接名 `Squirrel_Connection`，u1-l5 已讲）声明这是 IMK 输入法；`InputMethodServerControllerClass` 指向控制器类 `Squirrel.SquirrelInputController`。

#### 4.1.4 代码实践

**实践目标**：从 `Info.plist` 确认 Squirrel 同时具备「IMK 输入法」与「TIS 输入源」两重身份。

**操作步骤**：

1. 打开 `resources/Info.plist`，找到顶层 `<key>TISInputSourceID</key>`（第 5-6 行），它声明了**整个 App** 的输入源 ID。
2. 找到 `InputMethodConnectionName`（第 92-93 行），它声明 IMK 运行时连接名。
3. 找到 `ComponentInputModeDict`（第 25 行起），它声明 App 提供的输入模式。

**需要观察的现象**：你会发现 App 既有 IMK 的 `InputMethodConnectionName`，又有 TIS 的 `TISInputSourceID` 与 `ComponentInputModeDict`——这两套钥匙共存，正是「IMK 管运行、TIS 管挂号」的直接证据。

**预期结果**：

- 顶层 `TISInputSourceID` = `im.rime.inputmethod.Squirrel`（App 整体）。
- `InputMethodConnectionName` = `Squirrel_Connection`（IMK 运行时）。
- `ComponentInputModeDict` 下有 `Hans` 与 `Hant` 两个模式（TIS 输入源，见 4.2）。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 App 只写了 `InputMethodConnectionName`、却没有 `TISInputSourceID` 和 `ComponentInputModeDict`，会发生什么？

**参考答案**：系统不会把它登记为 TIS 输入源，因此它不会出现在「系统设置 → 键盘 → 输入法」列表里，用户也就无法启用和选中；即使代码里起了 `IMKServer`，也收不到按键，因为系统根本不知道该把按键路由给谁。TIS 挂号是 IMK 能跑起来的前提。

**练习 2**：`register / enable / select` 三层能否调换顺序，比如先 `select` 再 `register`？

**参考答案**：不能。`select` 依赖输入源已经「启用」，而「启用」依赖它已经「注册」。颠倒顺序时，`TISCreateInputSourceList` 查不到对应 ID（因为还没注册），后续 `enable/select` 会因找不到输入源而空转、打印失败日志。

---

### 4.2 InputMode、输入源 ID 与 Info.plist 一致性

#### 4.2.1 概念说明

Squirrel 虽然是一个 App，但在系统的 TIS 注册表里却是**两个输入源**：

- `im.rime.inputmethod.Squirrel.Hans` —— 简体模式，目标语言 `zh-Hans`。
- `im.rime.inputmethod.Squirrel.Hant` —— 繁体模式，目标语言 `zh-Hant`。

为什么是两个？因为同一个 App 既能打简体也能打繁体，而 macOS 的输入法菜单习惯按「语言/文字」逐项列出。注册两个模式后，用户可以单独启用其中一个，甚至能在菜单栏用快捷键在 Hans/Hant 间切换（`Info.plist` 里配了 `tsInputModeKeyEquivalentModifiersKey`）。

需要强调：**简繁之分是给「系统菜单」看的标签**。真正输出简体还是繁体，是由 librime 加载的输入方案（schema）决定的，前端 `SquirrelInputController` 对两个模式一视同仁（u1-l5 已讲）。所以 Hans/Hant 只是两块「门牌」，进去之后引擎是同一套。

这两个 ID 在代码里被收进一个枚举：

```swift
enum InputMode: String, CaseIterable {
  static let primary = Self.hans
  case hans = "im.rime.inputmethod.Squirrel.Hans"
  case hant = "im.rime.inputmethod.Squirrel.Hant"
}
```

`primary` 是「默认主模式」——当用户没有显式指定要操作哪个模式时（例如不带参数的 `--enable-input-source`），Squirrel 默认操作 `Hans`。这与 `Info.plist` 里 Hans 的 `tsInputModeDefaultStateKey = true`、Hant 的 `tsInputModeDefaultStateKey = false` 保持一致：**简体是默认开启的主模式**。

#### 4.2.2 核心流程

输入源 ID 的「单一事实来源」是 `Info.plist` 的 `ComponentInputModeDict`。它的流动路径是：

```text
Info.plist 的 ComponentInputModeDict.tsInputModeListKey
  ├─ key = "im.rime.inputmethod.Squirrel.Hans"  →  Hans 模式描述
  └─ key = "im.rime.inputmethod.Squirrel.Hant"  →  Hant 模式描述
        │
        ▼  TISRegisterInputSource 时被系统读取
   TIS 数据库里出现两个输入源（ID 即上述字符串）
        │
        ▼  运行时 TISCreateInputSourceList 枚举所有源
   SquirrelInstaller 用 mode.rawValue 匹配回 Hans / Hant
        │
        ▼  sources/InputSource.swift 的 InputMode 枚举
   代码侧也写死了同样的字符串
```

两边写的是**同一串字符**，这就是「一致性」要求：`Info.plist` 里的 key、模式字典内的 `TISInputSourceID`、以及 `InputMode` 的 `rawValue`，三者必须一字不差。否则代码里 `getInputSource` 会匹配不到系统返回的输入源，所有 `enable/select` 都会失效。

#### 4.2.3 源码精读

枚举定义与默认主模式：

[sources/InputSource.swift:12-16](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/InputSource.swift#L12-L16) —— `InputMode` 是 `String` 原始值枚举，`rawValue` 直接就是 TIS 输入源 ID；`static let primary = Self.hans` 指定简体为默认主模式。

`Info.plist` 的两个模式声明：

[resources/Info.plist:29-56](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L29-L56) —— Hans 模式：`TISIntendedLanguage = zh-Hans`、`tsInputModeDefaultStateKey = true`（默认启用）、`tsInputModePrimaryInScriptKey = true`（脚本主模式）、`tsInputModeCharacterRepertoireKey = [Hans, Hant]`。

[resources/Info.plist:57-84](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L57-L84) —— Hant 模式：`TISIntendedLanguage = zh-Hant`、`tsInputModeDefaultStateKey = false`（默认不启用），其余结构与 Hans 对称。

[resources/Info.plist:86-90](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L86-L90) —— `tsVisibleInputModeOrderedArrayKey` 规定菜单里的排列顺序：先 Hans 后 Hant。

把这三处与枚举的 `rawValue` 对照：

| 位置 | Hans 的 ID | Hant 的 ID |
| --- | --- | --- |
| `Info.plist` 模式字典的 key | `im.rime.inputmethod.Squirrel.Hans` | `im.rime.inputmethod.Squirrel.Hant` |
| 模式字典内 `TISInputSourceID` | 同上 | 同上 |
| `InputMode.hans.rawValue` | 同上 | 同上 |

三者完全相同——这就是必须维护的**一致性约定**。如果你改了 `Info.plist` 里的 ID，就必须同步改 `InputMode` 枚举，反之亦然；并且顶层 App 的 `TISInputSourceID`（`im.rime.inputmethod.Squirrel`，第 5-6 行）通常是两个模式 ID 的「公共前缀」。

#### 4.2.4 代码实践

**实践目标**：亲手核对三处 ID 的一致性，理解「单一事实来源」。

**操作步骤**：

1. 在 `resources/Info.plist` 中找到 `ComponentInputModeDict` 下的两个模式 key（第 29、57 行）。
2. 记下它们的 `TISIntendedLanguage` 与 `tsInputModeDefaultStateKey`。
3. 在 `sources/InputSource.swift` 第 12-16 行找到 `InputMode` 枚举的 `rawValue`。
4. 比较两边字符串是否完全相等。

**需要观察的现象**：两边的 Hans/Hant ID 字符串逐字符相同；Hans 的默认状态是 `true`，Hant 是 `false`，与代码里 `primary = .hans` 一致。

**预期结果**：确认一致性成立。若有人想新增一个模式（例如「粤语」），必须**同时**改 `Info.plist`（加一个模式字典）与 `InputMode` 枚举（加一个 case），缺一不可。

#### 4.2.5 小练习与答案

**练习 1**：为什么顶层 App 的 `TISInputSourceID` 是 `im.rime.inputmethod.Squirrel`，而两个模式却多了 `.Hans` / `.Hant` 后缀？

**参考答案**：顶层 ID 标识「整个输入法 App」，模式 ID 标识「App 里的一个具体输入源」。macOS 用「App ID + 模式后缀」的命名约定来表示二者是从属关系——系统看到 `im.rime.inputmethod.Squirrel.Hans` 就知道它属于 `im.rime.inputmethod.Squirrel` 这个 App。这种「公共前缀 + 模式后缀」的结构也被 `currentInputSourceID()` 的前缀判断 `hasPrefix("im.rime.inputmethod.Squirrel")` 利用了（见 4.5）。

**练习 2**：`InputMode` 用 `String` 原始值枚举、并且 `CaseIterable`，这两个设计分别服务于什么？

**参考答案**：`String` 原始值让枚举值直接等于 TIS 输入源 ID，可以在「ID 字符串」与「强类型枚举」之间用 `InputMode(rawValue:)` 互转（`Main.swift` 解析命令行参数时正是这么做）；`CaseIterable` 提供 `.allCases`，让代码能一句话枚举「全部模式」，例如 `enabledModes()` 里用它判断「是否两个模式都已启用」。

---

### 4.3 register()：把 App 注册进系统

#### 4.3.1 概念说明

`register()` 的职责单一：调用 `TISRegisterInputSource`，让系统读取 `Squirrel.app/Contents/Info.plist`，把 Hans/Hant 两个模式写入 TIS 数据库。

它面临一个现实问题：**`TISRegisterInputSource` 本身是幂等的吗？重复调用安全吗？** Squirrel 采取的策略是「先查后注册」——先用 `enabledModes()` 检查是否已经有模式被启用。如果已经有，说明用户之前已经注册并启用过，就**直接返回**，不再重复注册。

这种「检测到已启用就跳过」的设计有两个好处：

1. **避免打扰用户偏好**：用户可能手动启用/禁用了某些模式（比如只启用了 Hans 没启用 Hant）。如果每次升级都重新注册、强制重置，会覆盖用户的手动选择。
2. **幂等保障**：安装脚本、升级流程可能多次调用 `--register-input-source`，跳过已注册的情况能减少不必要的系统调用与日志噪音。

注意判断标准是「是否有模式被**启用**（`IsEnabled`）」而不是「是否已注册」。这是因为 TIS 没有直接暴露「是否已注册」的简单布尔，而「已启用」是注册的下游状态——能查到已启用的模式，说明注册必然已经发生过。

#### 4.3.2 核心流程

```text
register()
  │
  ▼
enabledModes()
  │  遍历 TISCreateInputSourceList 返回的全部输入源
  │  用 mode.rawValue 匹配 Hans/Hant
  │  对命中的源查 kTISPropertyInputSourceIsEnabled
  ▼
enabledInputModes 为空？
  ├─ 是 → TISRegisterInputSource(appDir)   真正注册
  └─ 否 → print("User already registered ...") 直接返回
```

支撑这个流程的有四个辅助设施：懒加载的 `inputSources` 字典、`enabledModes()`、`getInputSource(modes:)`、`getBool(for:key:)`。

#### 4.3.3 源码精读

`register()` 主体：

[sources/InputSource.swift:42-50](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/InputSource.swift#L42-L50) —— 先取 `enabledModes()`，非空则直接 `return`；为空才调用 `TISRegisterInputSource(SquirrelApp.appDir as CFURL)`。

「是否已有模式被启用」的判定函数：

[sources/InputSource.swift:29-40](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/InputSource.swift#L29-L40) —— `enabledModes()` 遍历所有模式，查 `kTISPropertyInputSourceIsEnabled`，收集已启用者；一旦集齐两个模式就提前 `break`。

而「所有模式」对应的真实 `TISInputSource` 句柄来自一个懒加载属性：

[sources/InputSource.swift:17-27](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/InputSource.swift#L17-L27) —— `inputSources` 在首次访问时调用 `TISCreateInputSourceList(nil, true)` 枚举系统里**全部已安装**的输入源（第二参数 `true` 表示「包括未启用的」），用每个源的 `kTISPropertyInputSourceID` 做 key 建字典。后续按 ID 查询就是一次字典查找。

> 💡 `TISCreateInputSourceList(nil, true)` 的第二个参数 `includeAllInstalled = true` 很关键：它让 Squirrel 能查到「已注册但未启用」的模式——这正是「只注册、未启用」时 `enabledModes()` 返回空、进而触发真正 `register` 的前提。

两个小工具让代码更整洁：

[sources/InputSource.swift:108-116](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/InputSource.swift#L108-L116) —— `getInputSource(modes:)` 把「模式枚举」翻译成「TIS 句柄」，只返回确实存在于系统里的那些。

[sources/InputSource.swift:118-122](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/InputSource.swift#L118-L122) —— `getBool(for:key:)` 是个薄封装：用 `TISGetInputSourceProperty` 取出属性，`unsafeBitCast` 还原成 `CFBoolean`，再 `CFBooleanGetValue` 转成 Swift `Bool`。返回 Optional，属性不存在时为 `nil`。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：解释 `register()` 为什么在「检测到已有模式被启用」时直接返回，而不重复注册。

**操作步骤（源码阅读型）**：

1. 读 [sources/InputSource.swift:42-50](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/InputSource.swift#L42-L50)，定位 `if !enabledInputModes.isEmpty { ... return }` 这个早退分支。
2. 追问三个问题：
   - 用「是否已启用」而不是「是否已注册」作为判断依据，原因是什么？（提示：TIS 没有直接的「已注册」布尔，而「已启用」是注册的下游状态。）
   - 如果强行每次都调 `TISRegisterInputSource`，会对用户的手动偏好（比如只启用了 Hans）造成什么影响？
   - 这个早退为什么能保证「幂等」——多次执行安装脚本不会累积副作用？

**需要观察的现象 / 预期结果**：在终端模拟两次安装（或直接看 `postinstall`），第一次 `register()` 会真正调用 `TISRegisterInputSource` 并打印 `Registered input source from ...`；第二次起，只要用户已启用过任一模式，就会打印 `User already registered Squirrel method(s): [...]` 并直接返回，不再触达 `TISRegisterInputSource`。

> ⚠️ 待本地验证：实际打印文案与是否真正跳过，需在装有 Squirrel 的 macOS 上运行 `Squirrel --register-input-source` 两次并观察 stdout 才能确认。

**参考解释要点**：

- **为什么用 `enabledModes()` 判定**：TIS 注册表没有简单的「该 App 是否已注册」布尔接口，但「有模式被启用」必然蕴含「已注册」（不注册不可能启用），所以这是一个可靠的充分条件。
- **为什么不重复注册**：保护用户偏好。用户可能在「系统设置」里手动只启用 Hans、关闭 Hant。重复注册可能让系统重新应用 `Info.plist` 的 `tsInputModeDefaultStateKey`，把用户改过的状态冲掉。早退分支让「已配置好的系统」免受安装/升级脚本打扰。
- **幂等性**：因为第二次起直接 return，无论脚本跑多少遍，行为一致，无累积副作用。

#### 4.3.5 小练习与答案

**练习 1**：假如一个全新系统，从未装过 Squirrel，第一次执行 `--register-input-source`，`enabledModes()` 会返回什么？接下来会发生什么？

**参考答案**：返回空数组（系统里还没有 `im.rime.inputmethod.Squirrel.*` 的任何输入源）。因此 `!enabledInputModes.isEmpty` 为假，进入真正注册分支，调用 `TISRegisterInputSource`，Hans/Hant 被写入 TIS 数据库。注意：此刻它们只是「已注册」，尚未「启用」。

**练习 2**：`inputSources` 被声明为 `private lazy var`。如果把它改成普通的 `let`（在 `init` 里初始化），会有什么问题？

**参考答案**：`TISCreateInputSourceList` 枚举的是「当前时刻」的系统输入源。`register()` 会改变系统状态（注册后会多出 Hans/Hant）。`lazy` 保证字典在**首次访问时**才生成——如果在 `register()` 之前访问过，它缓存的可能是注册前的旧列表，导致 `enabledModes()` 查不到刚注册的源。事实上 `register()` 内部并不依赖 `inputSources`（它只调 `enabledModes`，而 `enabledModes` 会触发懒加载），所以当前顺序是安全的；但这也说明了「枚举系统状态的快照」要谨慎对待时序。

---

### 4.4 enable() / disable() / select()：启用、禁用、选中

#### 4.4.1 概念说明

注册只是「挂号」，要让用户能用，还要启用，要让它成为当前输入法还要选中。这三个方法对应输入源生命周期的后半段：

- **`enable(modes:)`**：对每个尚未启用的目标模式调 `TISEnableInputSource`。启用后模式出现在输入法列表里。
- **`disable(modes:)`**：对每个已启用的目标模式调 `TISDisableInputSource`。禁用后从列表移除（但仍在 TIS 数据库里，可重新启用）。
- **`select(mode:)`**：对已启用的目标模式调 `TISSelectInputSource`，把它设为当前激活输入法。

这三个方法共享一套设计模式：

1. **默认值回退到 `primary`**：不传参数时，`enable` 默认只启用 `Hans`，`disable` 默认操作**全部**模式，`select` 默认选 `Hans`。
2. **先查后改，避免无效调用**：改之前都用 `getBool` 读取当前状态，只在「需要改变」时才调 C 函数。例如 `enable` 只对 `IsEnabled == false` 的源调 `TISEnableInputSource`。
3. **保护用户偏好**：`enable()` 在「不传 modes 且已有模式启用」时会直接返回，并注释 `// Preserve manually enabled input modes.`——不覆盖用户的手动选择。

`select` 比 `enable` 多一道检查：它要求输入源同时满足 `IsEnabled && IsSelectCapable && !IsSelected` 才会调用 `TISSelectInputSource`。`IsSelectCapable` 表示「该源是否具备被选为当前输入法的能力」（有些源只能作为辅助、不能成为主键盘）。

#### 4.4.2 核心流程

以 `select(mode: nil)`（默认选 Hans）为例：

```text
select(mode: nil)
  │  modeToSelect = .primary = .hans
  ▼
enabledModes().contains(.hans) ?
  ├─ 否 且 mode==nil → print("Default method not enabled yet") 返回
  ├─ 否 且 mode!=nil  → 先 enable([hans]) 再继续
  └─ 是 → 继续
  ▼
对 hans 源查三个布尔：
  IsEnabled && IsSelectCapable && !IsSelected
  ├─ 全满足 → TISSelectInputSource(源)   设为当前
  └─ 否则   → print("Failed to select ...")
```

`enable` 与 `disable` 的结构类似，只是查询的布尔和调用的 C 函数不同。

#### 4.4.3 源码精读

`enable(modes:)`：

[sources/InputSource.swift:52-66](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/InputSource.swift#L52-L66) —— 若不传 `modes` 且已有模式启用，则直接返回以保留用户手动启用项；否则 `modesToEnable` 默认 `[.primary]`，对每个 `IsEnabled == false` 的源调 `TISEnableInputSource`。

`select(mode:)`：

[sources/InputSource.swift:68-90](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/InputSource.swift#L68-L90) —— 先确保目标模式已启用（默认模式未启用时，只有显式传 `mode` 才会顺带 `enable`，否则打印未启用并返回）；再对满足三布尔条件的源调 `TISSelectInputSource`。

`disable(modes:)`：

[sources/InputSource.swift:98-106](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/InputSource.swift#L98-L106) —— `modesToDisable` 不传时默认 `InputMode.allCases`（全部模式），对每个 `IsEnabled == true` 的源调 `TISDisableInputSource`。

这三个方法在命令行入口被串起来：

[sources/Main.swift:43-69](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L43-L69) —— `--enable-input-source`、`--disable-input-source`、`--select-input-source` 三个分支：可选地用 `InputMode(rawValue:)` 把命令行参数解析成模式枚举（解析失败的会被 `compactMap` 丢弃），再调用对应方法。不传模式名时走默认值。

#### 4.4.4 代码实践

**实践目标**：跟踪 `.pkg` 安装时 `postinstall` 脚本如何按「注册 → 启用 → 选中」的顺序把 Squirrel 推上线。

**操作步骤（源码阅读型）**：

1. 打开 [scripts/postinstall](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/scripts/postinstall)。
2. 按行号顺序列出它对 `Squirrel` 可执行文件调用的命令。
3. 对每条命令，说明它最终落到 `SquirrelInstaller` 的哪个方法、哪个 Carbon API。

**需要观察的现象 / 预期结果**：

| 行号 | 命令 | SquirrelInstaller 方法 | Carbon API | 语义 |
| --- | --- | --- | --- | --- |
| 第 12 行 | `--register-input-source` | `register()` | `TISRegisterInputSource` | 把 Hans/Hant 写入 TIS 数据库 |
| 第 16 行 | `--build` | （部署器，非本讲） | librime `deploy` | 预编译方案 |
| 第 19 行 | `--enable-input-source` | `enable()` | `TISEnableInputSource` | 启用主模式 Hans |
| 第 20 行 | `--select-input-source` | `select()` | `TISSelectInputSource` | 选中 Hans 为当前输入法 |

注意第 19-20 行用 `sudo -u "${login_user}"` 以**登录用户**身份执行——因为「启用/选中」是用户级偏好，必须写在用户身份下，否则会装到 root 的偏好里、登录用户看不到。

> ⚠️ 待本地验证：实际能否在输入法菜单看到 Squirrel，需在 macOS 上完整跑一次 `.pkg` 安装并检查「系统设置 → 键盘 → 输入法」列表。

#### 4.4.5 小练习与答案

**练习 1**：`enable(modes:)` 里 `if !enabledInputModes.isEmpty && modes.isEmpty { return }` 这条短路，保护的是什么？

**参考答案**：保护用户的手动偏好。场景：用户在系统设置里手动只启用了 Hant、没启用 Hans。如果这时跑 `--enable-input-source`（不带参数，`modes` 为空），若无此短路，代码会按默认 `[.primary]` 去启用 Hans，可能改变用户「只想用 Hant」的意图。短路让「已有任意模式启用」时，无参 enable 直接返回，把决定权留给用户。

**练习 2**：`select()` 为什么要同时检查 `IsSelectCapable`，而不像 `enable` 只检查 `IsEnabled`？

**参考答案**：`IsEnabled` 只表示「出现在列表里」，`IsSelectCapable` 表示「能被选为当前键盘输入法」。有些输入源（例如某些辅助面板源）可以启用、可见，但不具备成为「当前激活键盘」的资格。`TISSelectInputSource` 对这类源会失败，所以代码先用 `IsSelectCapable` 过滤，避免无效调用并把失败原因打印出来。

---

### 4.5 currentInputSourceID()：查询当前输入源与运行时用途

#### 4.5.1 概念说明

前面四个方法（register/enable/disable/select）都是**写**操作——改变输入源状态。`currentInputSourceID()` 是唯一的**读**操作：查询「此刻系统当前激活的键盘输入源是谁」，返回它的 ID 字符串。

它和前面的写操作不同：写操作发生在**安装/命令行**阶段（短命进程），而 `currentInputSourceID()` 被 AppDelegate 在**运行时**调用，服务于两个目的：

1. **状态栏图标显隐**：只有当 Squirrel 是当前输入法时，菜单栏才显示「中/Ａ」图标；切到别的输入法时图标隐藏。
2. **悬挂组合兜底**：当别的进程程序化地把输入法切走（macOS 26 上不触发 `deactivateServer`），用当前输入源 ID 判断「Squirrel 已不是当前源」，进而补做一次组合收尾（这部分在 u5-l1 已详述，本讲只关注 `currentInputSourceID` 这一环）。

两处判断都用同一个技巧：检查返回的 ID 是否**以 `im.rime.inputmethod.Squirrel` 开头**。因为 Hans 和 Hant 两个模式的 ID 都是这个前缀（见 4.2 练习 1），一个 `hasPrefix` 就能覆盖两种模式。

#### 4.5.2 核心流程

```text
currentInputSourceID()
  │
  ▼  TISCopyCurrentKeyboardInputSource()
   取当前激活的键盘输入源（TISInputSource）
  │
  ▼  TISGetInputSourceProperty(源, kTISPropertyInputSourceID)
   读出它的 ID 字符串
  │
  ▼  unsafeBitCast 还原为 CFString → String?
   返回 Optional（取属性失败时为 nil）

调用方：
  hasPrefix("im.rime.inputmethod.Squirrel") ?
    ├─ 是 → 当前是 Squirrel（Hans 或 Hant）
    └─ 否 → 当前是别的输入法
```

#### 4.5.3 源码精读

查询函数本身（注意是 `static`，不需要实例）：

[sources/InputSource.swift:92-96](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/InputSource.swift#L92-L96) —— `TISCopyCurrentKeyboardInputSource().takeRetainedValue()` 取当前源（`takeRetainedValue` 表示接管 Core Foundation 的所有权，ARC 下负责释放），再读 `kTISPropertyInputSourceID`。

运行时用途一：状态栏图标显隐。

[sources/SquirrelApplicationDelegate.swift:364-368](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L364-L368) —— `updateStatusItemVisibility()` 用 `currentInputSourceID().hasPrefix("im.rime.inputmethod.Squirrel")` 决定 `statusItem.isVisible`：是 Squirrel 就显示图标，否则隐藏。

运行时用途二：悬挂组合兜底（u5-l1 已讲机制，这里只看 ID 判断这一步）。

[sources/SquirrelApplicationDelegate.swift:377-383](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L377-L383) —— `finalizeStrandedComposition()` 先 `guard !currentInputSourceID.hasPrefix("im.rime.inputmethod.Squirrel")`：只有「当前已经不是 Squirrel」时才补做 `deactivateServer`，避免在仍是 Squirrel 时误触发。

这两处合起来说明：**前缀判断 `im.rime.inputmethod.Squirrel` 是整个项目识别「自己是否处于激活态」的唯一手段**，而这个前缀正是 4.2 讨论的「App 顶层 TISInputSourceID」。代码与 `Info.plist` 的一致性在这里又一次体现——若 ID 改了，这两处 `hasPrefix` 也必须同步改。

#### 4.5.4 代码实践

**实践目标**：理解「前缀判断」如何用一个字符串同时覆盖 Hans 和 Hant 两种模式。

**操作步骤（源码阅读型）**：

1. 读 [sources/InputSource.swift:12-16](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/InputSource.swift#L12-L16)，写下 Hans 与 Hant 的完整 `rawValue`。
2. 读 [sources/SquirrelApplicationDelegate.swift:367](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L367) 与 [第 379 行](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L379)，写下 `hasPrefix` 的实参。
3. 验证：Hans 和 Hant 的 ID 是否都以该前缀开头？顶层 App 的 `TISInputSourceID`（[Info.plist:5-6](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L5-L6)）是否就是该前缀？

**需要观察的现象 / 预期结果**：

- `hasPrefix("im.rime.inputmethod.Squirrel")` 同时匹配 `...Squirrel.Hans` 和 `...Squirrel.Hant`，一个判断覆盖两种模式。
- 该前缀正是顶层 App 的 `TISInputSourceID`，三处字符串同源。

> ⚠️ 待本地验证：在 macOS 上用 `Squirrel --getascii` 之外，可借助系统 API 或第三方工具观察切到 Squirrel 后 `currentInputSourceID()` 的实际返回值，确认它确实是 `...Squirrel.Hans` 或 `...Squirrel.Hant`。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `currentInputSourceID()` 被声明为 `static func`，而 `register/enable/select` 是实例方法？

**参考答案**：`currentInputSourceID()` 只调用无状态的系统 API（`TISCopyCurrentKeyboardInputSource`），不依赖 `SquirrelInstaller` 实例的任何状态（尤其不依赖懒加载的 `inputSources` 快照），所以可以静态调用，AppDelegate 里直接 `SquirrelInstaller.currentInputSourceID()` 即可，不必创建实例。而 register/enable/select 会用到实例属性 `inputSources`（以及内部辅助方法 `getInputSource/getBool`），必须是实例方法。

**练习 2**：假如未来新增一个粤语模式 `im.rime.inputmethod.Squirrel.Yue`，`currentInputSourceID()` 的前缀判断需要改吗？

**参考答案**：不需要。因为新模式的 ID 仍以 `im.rime.inputmethod.Squirrel` 开头，现有的 `hasPrefix` 自动覆盖。这正是「公共前缀 + 模式后缀」命名约定的红利——只要新模式的 ID 遵守前缀约定，所有基于前缀的判断都向后兼容。需要改的只是 `InputMode` 枚举（加 `.yue` case）和 `Info.plist`（加模式字典）。

---

## 5. 综合实践

把本讲的知识串起来，完成一次「安装时序 + 一致性核对」的综合练习。

**任务**：假设你负责给 Squirrel 新增第三个输入模式「粤拼」（Yue），请列出需要同步修改的所有位置，并解释每处为什么必须改。

**操作步骤**：

1. **`Info.plist`**：在 `ComponentInputModeDict.tsInputModeListKey` 下新增一个 key 为 `im.rime.inputmethod.Squirrel.Yue` 的模式字典，参考 [Hant 模式结构](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L57-L84) 填写 `TISIntendedLanguage`（如 `zh-Yue` 或 `yue`）、`tsInputModeCharacterRepertoireKey` 等；并在 [`tsVisibleInputModeOrderedArrayKey`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L86-L90) 里加上新 ID。
2. **`InputSource.swift`**：在 [`InputMode` 枚举](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/InputSource.swift#L12-L16) 加 `case yue = "im.rime.inputmethod.Squirrel.Yue"`。
3. **核对一致性**：确认新 ID 的前缀仍是 `im.rime.inputmethod.Squirrel`，这样 [`currentInputSourceID()`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/InputSource.swift#L92-L96) 与两处 `hasPrefix` 判断无需改动即可识别新模式。
4. **命令行兼容**：确认 [`Main.swift`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L43-L69) 用 `InputMode(rawValue:)` 解析参数的机制对新 case 自动生效——用户可以 `--enable-input-source im.rime.inputmethod.Squirrel.Yue`。

**需要观察的现象 / 预期结果**：

- 注册后系统输入法列表多出第三个模式，ID 与代码枚举一致。
- 切到粤拼模式时，状态栏图标依然显示（因为前缀判断覆盖），证明前缀设计的向后兼容性。
- `register()` 的「已启用就跳过」逻辑不受影响——只要用户启用过任意模式，重复注册仍会被早退分支挡掉。

> ⚠️ 待本地验证：完整流程涉及重新签名安装 `Squirrel.app`，需在 macOS 开发环境实测；本练习侧重源码改动点的梳理与一致性推理。

## 6. 本讲小结

- **IMK 与 TIS 分工**：IMK（`IMKServer`/`SquirrelInputController`）管运行时收键，TIS（Carbon HIToolbox）管系统输入源注册表；一个输入法必须「在 TIS 挂号 + 在 IMK 运行」才能被用上。
- **三层生命周期**：`register`（登记进数据库）→ `enable`（出现在列表、可被选）→ `select`（成为当前输入法），严格递进，分别由 `TISRegisterInputSource / TISEnableInputSource / TISSelectInputSource` 支撑。
- **Hans 与 Hant**：Squirrel 注册两个输入模式，ID 为 `im.rime.inputmethod.Squirrel.Hans`（简，`primary`、默认启用）与 `...Hant`（繁，默认不启用）；简繁之分只是门牌，真正输出由 librime 方案决定。
- **register 的幂等早退**：`register()` 用 `enabledModes()` 检测「是否已有模式启用」，有则直接返回，既避免重复注册，又保护用户手动启用偏好。
- **currentInputSourceID 的前缀判断**：项目用 `hasPrefix("im.rime.inputmethod.Squirrel")` 一招同时识别 Hans/Hant，驱动状态栏图标显隐与悬挂组合兜底。
- **一致性约定**：`Info.plist` 的 `TISInputSourceID` / 模式字典 key 与 `InputMode.rawValue` 必须一字不差，这是 register/enabling/select/currentInputSourceID 全部能工作的基石。

## 7. 下一步学习建议

- **u5-l5 打包、安装与 Sparkle 更新**：本讲的 `register/enable/select` 由 `scripts/postinstall` 驱动，下一讲会完整覆盖 `.pkg` 打包、`DEV_ID` 签名公证、`postinstall` 安装时序与 Sparkle 自动更新，把「装上去」这一环讲透。
- **u5-l3 保留属性：插件→前端协调**：若你想了解运行时 Squirrel 如何与 librime 插件协调，可继续读保留属性协议，它会再次用到「运行时状态查询与回调」的思路。
- **延伸阅读源码**：精读 [sources/InputSource.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/InputSource.swift) 全文（仅 123 行），结合本讲对照 [sources/Main.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift) 的命令行分发与 [scripts/postinstall](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/scripts/postinstall) 的安装时序，即可掌握 TIS 注册的全貌。
