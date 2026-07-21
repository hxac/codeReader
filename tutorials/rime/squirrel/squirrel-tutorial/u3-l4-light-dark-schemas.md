# 亮/暗主题与 schema 特化样式

## 1. 本讲目标

本讲承接 [u3-l3 主题加载](u3-l3-theme-loading.md)，把视角从「单个主题对象如何被组装」拉高到「整个 App 在运行期如何管理主题」。

读完本讲，你应当能够：

- 说清楚为什么 Squirrel 在启动时就要同时加载**亮**与**暗**两套主题，而不是只加载一套。
- 区分 `SquirrelTheme` 上两个容易混淆的布尔标志 `native` 与 `available`，并知道它们分别驱动什么下游行为。
- 描述切换输入方案（schema）时主题的重新加载链路，以及「schema 特化样式」是如何叠加在全局样式之上的。
- 区分两种回退：单项级回退（`getXxx` 内部）与整配置级回退（`loadSettings(for:)` 里 schema 无 `style` 节时整体退回 base config）。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：macOS 有「亮」「暗」两种系统外观。** 用户可以在系统设置里把整台 Mac 切到暗色模式。一个体面的输入法面板，理应跟随系统外观——亮色模式下面板是浅底深字，暗色模式下面板是深底浅字。Squirrel 需要同时准备好两套配色，运行时按当前外观选用其一。

**直觉二：「语义色」可以自动跟随亮暗。** macOS 的 AppKit 提供了一批「语义色」，例如 `NSColor.windowBackgroundColor`、`NSColor.labelColor`。它们不是固定的 RGB 值，而是「当前外观下合适的颜色」——亮色模式下自动变浅，暗色模式下自动变深。如果一个主题完全使用语义色，它就天然支持亮暗，无需用户提供任何具体颜色。这就是 Squirrel 里 `native`（原生/系统配色）的含义。

**直觉三：输入方案（schema）可以带自己的样式。** 回顾 [u3-l1](u3-l1-squirrel-config.md)：base config 是 `squirrel.yaml`，schema config 是某个输入方案文件（如 `luna_pinyin.schema.yaml`）。两者是父子关系。一个方案文件里除了定义「按键如何转换」，还可以带一个 `style:` 节，覆盖全局的字体、配色、布局。这样就能做到「双拼用紧凑横排，注音用竖排繁体风」。

> 名词速查：**base config** = `squirrel.yaml`（前端全局配置）；**schema config** = 某个输入方案；**亮主题** = `color_scheme` 指向的配色；**暗主题** = `color_scheme_dark` 指向的配色；**native** = 不读具体颜色、用系统语义色的哨兵值。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [sources/SquirrelApplicationDelegate.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift) | 持有全局 `config` 与 `panel`，定义 `loadSettings()` 与 `loadSettings(for:)` 两个主题加载入口 |
| [sources/SquirrelTheme.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift) | 单个主题对象；`load(config:dark:)` 在此判定 `native`/`available` |
| [sources/SquirrelPanel.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift) | 面板；`load(config:forDarkMode:)` 分发到亮/暗主题，`show()` 据此设置窗口 appearance |
| [sources/SquirrelView.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift) | 实际持有 `lightTheme`/`darkTheme`，并定义 `currentTheme` 的运行时选择 |
| [sources/SquirrelInputController.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift) | 在 `rimeUpdate` 中检测 schema 切换并触发 `loadSettings(for:)` |
| [sources/SquirrelConfig.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift) | 提供 `open(schemaID:baseConfig:)`、`has(section:)`，是 schema 特化与回退的底层 |
| [data/squirrel.yaml](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml) | 默认配置；默认 `color_scheme: native` |

---

## 4. 核心概念与源码讲解

### 4.1 loadSettings：亮/暗双主题的预加载

#### 4.1.1 概念说明

最朴素的实现是：每次面板要显示时，临时问一下系统「现在亮还是暗」，再去读对应配色、组装主题。Squirrel 没有这么做，而是采用**预加载双主题**策略——启动时就把亮、暗两套主题都组装好，分别挂在视图的 `lightTheme` 与 `darkTheme` 上；运行时面板显示前只是「选其一」，不再读配置、不再组装。

这样做有两个好处：

1. **切换零延迟**：系统外观切换是高频用户行为，预组装好的主题对象可即时替换，避免每次显示都走一遍 YAML 读取 + 颜色解析 + 字体级联的开销。
2. **解耦「配置读取」与「主题使用」**：配置只在启动和切方案时读，面板显示路径上不再触碰 librime 配置 API，路径更短、更不易出错。

#### 4.1.2 核心流程

启动期的主题加载链路如下（顺序不可调换，承接 [u2-l2](u2-l2-global-rime-init.md) 的 `setupRime → startRime → loadSettings`）：

```
loadSettings()                         # AppDelegate，启动/sync 时调用
  ├── openBaseConfig()                 # 打开 squirrel.yaml
  ├── 读 show_notifications_when / status_icon/show
  └── panel.load(config:, forDarkMode: false)   # 组装亮主题 → view.lightTheme
      panel.load(config:, forDarkMode: true)    # 组装暗主题 → view.darkTheme

panel.load(config:, forDarkMode: isDark)
  ├── isDark == true  → 新建 darkTheme  → darkTheme.load(config:, dark: true)
  └── isDark == false → 新建 lightTheme → lightTheme.load(config:, dark: false)
```

运行期，面板真正要绘制时，由视图按当前系统外观**二选一**：

```
currentTheme = isDark && darkTheme.available ? darkTheme : lightTheme
```

注意这个选择是**即时计算属性**（`var currentTheme`），系统外观一变、下次访问就拿到另一套主题，无需任何重新加载。

#### 4.1.3 源码精读

`loadSettings()` 的核心就是最后两次 `panel.load`，一次亮、一次暗：

[sources/SquirrelApplicationDelegate.swift:169-182](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L169-L182)（加载 base config 后，分别用 `forDarkMode: false` 与 `forDarkMode: true` 把亮暗两套主题灌进 panel）：

```swift
func loadSettings() {
  config = SquirrelConfig()
  if !config!.openBaseConfig() { return }
  enableNotifications = config!.getString("show_notifications_when") != "never"
  showStatusIcon = config!.getBool("status_icon/show") ?? true
  refreshStatusItem()
  if let panel = panel, let config = self.config {
    panel.load(config: config, forDarkMode: false)   // 亮
    panel.load(config: config, forDarkMode: true)    // 暗
  }
}
```

`SquirrelPanel.load` 只做一件事：按 `isDark` 把新主题挂到视图的对应槽位，真正的组装工作交给 `SquirrelTheme.load`（见 4.2）。

[sources/SquirrelPanel.swift:319-327](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L319-L327)（亮暗分支各建一个全新的 `SquirrelTheme`）：

```swift
func load(config: SquirrelConfig, forDarkMode isDark: Bool) {
  if isDark {
    view.darkTheme = SquirrelTheme()
    view.darkTheme.load(config: config, dark: true)
  } else {
    view.lightTheme = SquirrelTheme()
    view.lightTheme.load(config: config, dark: isDark)
  }
}
```

运行时的「二选一」在视图层。`isDark` 直接询问 AppKit 当前有效外观是否匹配 `.darkAqua`：

[sources/SquirrelView.swift:40-44](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L40-L44) 与 [sources/SquirrelView.swift:74-76](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L74-L76)：

```swift
var lightTheme = SquirrelTheme()
var darkTheme = SquirrelTheme()
var currentTheme: SquirrelTheme {
  if isDark && darkTheme.available { darkTheme } else { lightTheme }
}
// ...
var isDark: Bool {
  NSApp.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
}
```

注意 `currentTheme` 里那个 `darkTheme.available` 守卫——它正是 4.2 要讲的重点：如果用户压根没配暗主题，系统即便处于暗色模式，也得退回亮主题。

#### 4.1.4 代码实践

**实践目标**：观察「预加载双主题 + 运行时即时切换」的真实表现。

**操作步骤**（需要在 macOS 图形环境，无图形环境则改为「源码阅读型实践」，见下方）：

1. 构建并安装 Squirrel（参考 [u1-l3](u3-l2-squirrel-yaml.md) 的 Makefile 流程不属于本讲，构建见入门单元）。
2. 编辑 `~/Library/Rime/squirrel.yaml`，同时配置亮暗两套非 native 配色：
   ```yaml
   style:
     color_scheme: aqua
     color_scheme_dark: clean_white
   ```
3. 重新部署：终端执行 `Squirrel --reload`（该命令经分布式通知触发已运行实例重新部署，见 [u5-l1](u5-l1-distributed-notifications.md)）。
4. 唤起输入法，在任意文本框输入拼音触发候选面板。
5. 打开「系统设置 → 外观」，在亮/暗之间切换，观察面板底色与字色的变化。

**需要观察的现象**：切换系统外观后面板**立刻**变色，无肉眼可感的延迟——这正是「双主题预加载」的效果；面板不会闪一下白屏再去读配置。

**预期结果**：亮色模式下面板呈现 `aqua` 的浅底深字，暗色模式下呈现 `clean_white`（或你配置的暗色方案）的配色。

> 待本地验证：若无 macOS 图形环境，改为源码阅读型实践——在 `currentTheme` 的 getter 处与 `isDark` 处各加一行 `print`，编译运行后切换系统外观，从日志确认「主题对象的切换发生在运行期、且不触发 `loadSettings`」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `loadSettings()` 要调用两次 `panel.load`，而不是只调一次、让主题内部记住亮暗？

**参考答案**：因为亮、暗是两个**独立的**主题对象（`lightTheme` 与 `darkTheme`），分别持有各自的配色、字体、布局。调用两次才能把两个槽位都填满，运行时 `currentTheme` 才能在两者间即时切换而不必重新组装。

**练习 2**：`currentTheme` 是计算属性（`var` + getter），而不是一个被赋值的存储属性。这样做的好处是什么？

**参考答案**：计算属性每次访问都重新求值，因此系统外观一变，下一次访问 `currentTheme` 自动拿到新外观对应的主题，无需任何「监听外观变化 → 重新赋值」的额外代码。主题对象的选用与系统外观始终同步。

---

### 4.2 native 与 available：两个布尔标志的含义

#### 4.2.1 概念说明

`SquirrelTheme` 上有两个布尔标志，初学者极易混淆，但它们解决的是**两个完全不同的问题**：

- **`native`**（默认 `true`）——「这个主题有没有指定具体配色？」回答的是**内容来源**问题。如果 `color_scheme` 取值为 `"native"`，表示「我不提供任何颜色，全用 macOS 语义色」，此时 `native` 保持 `true`；一旦取值是任何具体方案名（如 `aqua`、`solarized_dark`），`native` 被置为 `false`，表示「我提供了具体颜色，别用语义色了」。
- **`available`**（默认 `true`）——「这个主题存不存在 / 可不可用？」回答的是**存在性**问题。只有当对应的 `color_scheme`（或 `color_scheme_dark`）键**完全不存在**时，`available` 才被置为 `false`。

一句话区分：`native` 问「用不用系统语义色」，`available` 问「这套主题配没配」。

#### 4.2.2 核心流程

`SquirrelTheme.load` 根据 `dark` 参数先决定读哪个键，再按取值分三种情况：

```
colorSchemeOption = dark ? "style/color_scheme_dark" : "style/color_scheme"
取 colorScheme = config.getString(colorSchemeOption)

┌─ colorScheme 有值
│   ┌─ colorScheme == "native"   → 什么都不读，native 保持 true（用语义色）
│   └─ colorScheme != "native"   → native = false，读 preset_color_schemes/<方案名>
└─ colorScheme 无值（else）       → available = false（这套主题没配）
```

亮、暗主题**各自独立**走一遍这个流程，互不影响。因此完全可能出现「亮主题是 native、暗主题是某个具体方案」的组合。

这两个标志的下游消费者主要有两处，可以用真值表概括：

| 场景 | 判定 | 含义 |
| --- | --- | --- |
| `currentTheme` 选主题 | `isDark && darkTheme.available` | 暗模式下，仅当暗主题**存在**才用它，否则退回亮主题 |
| `show()` 设窗口 appearance | `theme.native \|\| darkTheme.available` | 当前主题是 native，**或**暗主题存在，窗口才跟随系统外观；否则强制亮色窗口 |

第二行的逻辑尤其精妙，详见 4.2.3 末尾的解读。

#### 4.2.3 源码精读

两个标志的默认值都是 `true`——这点很关键，意味着「什么都不配」时主题默认可用且走 native：

[sources/SquirrelTheme.swift:30-31](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L30-L31)：

```swift
private(set) var available = true
private(set) var native = true
```

`load` 里决定读哪个配色键的那一行，是整段逻辑的「总开关」：

[sources/SquirrelTheme.swift:228-231](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L228-L231)（按 `dark` 选键；命中 `"native"` 时**跳过**整段 `preset_color_schemes` 读取，`native` 保持默认 `true`）：

```swift
let colorSchemeOption = dark ? "style/color_scheme_dark" : "style/color_scheme"
if let colorScheme = config.getString(colorSchemeOption) {
  if colorScheme != "native" {
    native = false
    let prefix = "preset_color_schemes/\(colorScheme)"
    // ... 读取该方案的所有具体颜色 ...
```

注意：`data/squirrel.yaml` 里 `preset_color_schemes` 下确实列了一个 `native` 条目，但它只有一个 `name: 系統配色` 字段、没有任何颜色——而上面的代码在 `colorScheme == "native"` 时根本不会进入读取分支。所以 `native` 是一个**代码层面的哨兵值**，配置文件里那条目只是给用户看的说明，不会被读。

[data/squirrel.yaml:76-78](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L76-L78)（`preset_color_schemes/native` 仅含名称、无颜色定义）：

```yaml
preset_color_schemes:
  native:
    name: 系統配色
```

而默认的 `style/color_scheme` 正是 `native`：

[data/squirrel.yaml:27-31](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L27-L31)：

```yaml
style:
  color_scheme: native
  # Optional: define both light and dark color schemes to match system appearance
  #color_scheme: solarized_light
  #color_scheme_dark: solarized_dark
```

`available = false` 只在「键完全不存在」时触发，即 `if let` 失败的 `else` 分支：

[sources/SquirrelTheme.swift:279-281](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L279-L281)：

```swift
    } else {
      available = false
    }
```

这两个标志最关键的消费者是 `SquirrelPanel.show()`。它在绘制前决定窗口本身的 appearance（外观）：

[sources/SquirrelPanel.swift:364-369](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L364-L369)：

```swift
if theme.native || view.darkTheme.available {
  self.appearance = NSApp.effectiveAppearance
} else {
  // user configured only a light theme, set window appearance to light.
  self.appearance = NSAppearance(named: .aqua)
}
```

**这段逻辑值得细读**。`theme` 是当前生效主题（`currentTheme`）。判定条件翻译成人话是：

> 「只要当前主题用的是系统语义色（native），**或者**用户配了暗主题，窗口就跟随系统外观；只有当用户**只配了一个非 native 的亮主题、且没配暗主题**时，才强制窗口为亮色（aqua）。」

为什么要强制？想象用户只配了 `color_scheme: aqua`（亮色，非 native）、没配 `color_scheme_dark`。系统切到暗色模式时：`darkTheme.available` 为 `false`，于是 `currentTheme` 退回 `lightTheme`（即 `aqua` 的浅底深字）。此时如果窗口 appearance 也跟系统变暗，就会出现「亮色配画面板画在暗色窗口里」的割裂感。强制窗口为 `aqua`，让「亮色配色 + 亮色窗口」保持一致。反之，只要暗主题可用或当前是 native（语义色本就自适应），窗口跟随系统就是安全的。

#### 4.2.4 代码实践

**实践目标**：亲手验证 `native` 与 `available` 在三种配置下的取值差异。

**操作步骤**（源码阅读 + 配置对照型）：

1. 在 [sources/SquirrelTheme.swift:231](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L231) 的 `native = false` 下一行、以及 [sources/SquirrelTheme.swift:280](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L280) 的 `available = false` 下一行，各加一行日志：
   ```swift
   print("[theme] dark=\(dark) colorScheme=\(colorScheme ?? "nil") native=\(native) available=\(available)")
   ```
   > 注意：这是**示例代码**，仅用于本地观察，不要提交。修改源码不在本讲义授权范围内，请在本地副本上操作。
2. 分别用三种 `~/Library/Rime/squirrel.yaml` 配置部署并观察日志：
   - **情形 A**：`color_scheme: native`
   - **情形 B**：`color_scheme: aqua`
   - **情形 C**：整个 `style:` 节删掉 `color_scheme` 键

**需要观察的现象**：每次 `loadSettings` 会打印两行（`dark=false` 与 `dark=true` 各一次），关注 `native`/`available` 的组合。

**预期结果**：

| 情形 | dark=false 行 | dark=true 行 | 解读 |
| --- | --- | --- | --- |
| A (`native`) | native=true, available=true | native=true, available=true | 亮暗都走语义色 |
| B (`aqua`) | native=**false**, available=true | native=true, available=**false** | 亮主题有具体色、暗主题键不存在 |
| C (无键) | native=true, available=**false** | native=true, available=**false** | 亮暗都缺失 |

> 待本地验证：若无运行环境，可纯靠上表对照源码逻辑推演——重点理解情形 B 中「亮暗两个标志不同步」的现象，这正是 4.2.2 真值表第二行强制 `aqua` 窗口的触发条件。

#### 4.2.5 小练习与答案

**练习 1**：用户配置了 `color_scheme: aqua` 但没配 `color_scheme_dark`。系统处于暗色模式时，`currentTheme` 返回哪个主题？窗口 appearance 是什么？

**参考答案**：`darkTheme.available` 为 `false`（`color_scheme_dark` 键不存在），所以 `currentTheme` 退回 `lightTheme`（即 `aqua`）。又因为 `theme.native`（aqua 非 native，故 false）且 `darkTheme.available`（false），`show()` 走 else 分支，窗口 appearance 被强制为 `aqua`（亮色）。结果是：暗色模式下，面板以亮色窗口 + 亮色配色一致地显示。

**练习 2**：把 `color_scheme` 设为 `native`、`color_scheme_dark` 设为 `solarized_dark`。亮、暗两个主题对象的 `native` 标志分别是什么？

**参考答案**：亮主题 `native=true`（值为 `"native"`，跳过读取）；暗主题 `native=false`（值为 `"solarized_dark"`，进入读取分支）。两套主题的 `native` 标志**各自独立**，可以不同。

---

### 4.3 loadSettings(for:)：schema 特化样式

#### 4.3.1 概念说明

4.1 讲的是「**全局**主题」——所有输入方案共用 `squirrel.yaml` 里那一套亮暗配色。但实际使用中，用户常常希望「换一种输入法，换一种皮肤」：用拼音时是横排紧凑，用注音/双拼时想换成竖排繁体风。

Squirrel 支持在**输入方案文件**（schema，如 `double_pinyin.schema.yaml`）里写一个 `style:` 节，覆盖全局样式——这就是 **schema 特化样式**。当用户切换到该方案时，前端会用方案自带的 `style:` 重新加载主题，实现「一个方案一套皮肤」。

#### 4.3.2 核心流程

schema 特化的触发点不在 AppDelegate，而在输入控制器的 `rimeUpdate`——每次引擎处理完按键，前端都会检查「当前 schema 变了没」：

```
rimeUpdate()
  └── get_status → status.schema_id
      └── 若 schema_id 与缓存的 schemaId 不同：
          schemaId = 新 schema_id             # 更新缓存（去重，避免重复加载）
          loadSettings(for: schemaId)         # 触发主题重载
          └── 重新计算 inlinePreedit / inlineCandidate / soft_cursor
```

`loadSettings(for:)` 内部的两条路径：

```
loadSettings(for: schemaID)
  ├── 校验 schemaID（非空、不以 '.' 开头）
  ├── schema = SquirrelConfig()
  └── if schema.open(schemaID:, baseConfig: config) 且 schema.has(section: "style"):
        panel.load(schema, dark:false) + panel.load(schema, dark:true)   # 用方案样式
      else:
        panel.load(config, dark:false) + panel.load(config, dark:true)   # 回退全局（见 4.4）
```

注意 `schema.open(schemaID:baseConfig: config)`——打开方案时把全局 `config` 作为它的 `baseConfig` 传入，建立起父子回退关系（详见 4.4）。

#### 4.3.3 源码精读

触发点：`rimeUpdate` 用 `schemaId` 字段缓存上次的方案 ID，仅在变化时才重载，避免每次按键都重新加载主题：

[sources/SquirrelInputController.swift:446-448](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L446-L448)（`schemaId == ""` 处理首次进入、`schemaId != ...` 处理切换）：

```swift
if let schema_id = status.schema_id, schemaId == "" || schemaId != String(cString: schema_id) {
  schemaId = String(cString: schema_id)
  NSApp.squirrelAppDelegate.loadSettings(for: schemaId)
```

`loadSettings(for:)` 的核心是一个 `if-else`：方案**带有** `style:` 节就用方案样式，否则走 4.4 的回退：

[sources/SquirrelApplicationDelegate.swift:184-199](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L184-L199)：

```swift
func loadSettings(for schemaID: String) {
  if schemaID.count == 0 || schemaID.first == "." { return }   // 过滤无效 ID
  let schema = SquirrelConfig()
  if let panel = panel, let config = self.config {
    if schema.open(schemaID: schemaID, baseConfig: config) && schema.has(section: "style") {
      panel.load(config: schema, forDarkMode: false)            // 用方案样式
      panel.load(config: schema, forDarkMode: true)
    } else {
      panel.load(config: config, forDarkMode: false)            // 回退全局
      panel.load(config: config, forDarkMode: true)
    }
  }
  schema.close()
}
```

为什么需要 `schemaID.first == "."` 这种过滤？以 `.` 开头的 schema ID 是 librime 内部的特殊/隐藏方案，不应触发前端样式重载。`schema.close()` 保证方案配置句柄及时归还给 librime（C 资源，不关即泄漏，呼应 [u2-l6](u2-l6-rime-update-dataflow.md) 的 `free_*` 配对释放原则）。

底层 `open(schemaID:baseConfig:)` 把全局 config 挂为方案的 `baseConfig`，是后续单项级回退能成立的前提：

[sources/SquirrelConfig.swift:24-31](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L24-L31)（打开方案成功才记下 baseConfig）：

```swift
func open(schemaID: String, baseConfig: SquirrelConfig?) -> Bool {
  close()
  isOpen = rimeAPI.schema_open(schemaID, &config)
  if isOpen {
    self.baseConfig = baseConfig
  }
  return isOpen
}
```

#### 4.3.4 代码实践

**实践目标**：亲手为一个输入方案添加 schema 特化样式，观察切换方案时面板的变化。

**操作步骤**（配置编辑型）：

1. 找到 `~/Library/Rime/` 下某个方案文件，例如 `double_pinyin_flypy.schema.yaml`（小鹤双拼；若不存在可先用 plum 下载一个方案）。
2. 在该文件的顶层加一个 `style:` 节，给一个明显区别于全局的配色与布局：
   ```yaml
   style:
     color_scheme: azure
     candidate_list_layout: linear
     font_point: 18
   ```
   > 这是**示例配置**，仅用于本地观察。
3. 执行 `Squirrel --reload` 重新部署。
4. 切换到该双拼方案（用状态栏菜单或快捷键），唤起候选面板。
5. 再切换回默认拼音方案，对比面板外观。

**需要观察的现象**：切到双拼方案时，面板立即变成 `azure` 配色 + 横排 + 18pt 字号；切回拼音时恢复全局样式。

**预期结果**：方案切换瞬间面板外观随之改变，验证「schema 特化样式在 `loadSettings(for:)` 被即时应用」。

> 待本地验证：若无 macOS 环境，改为源码阅读型实践——在 `loadSettings(for:)` 的 `if` 两个分支各加一行 `print`，标注「用方案样式 / 回退全局」，对照日志确认哪些方案走了哪条路径。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `rimeUpdate` 里要用 `schemaId` 字段缓存上一次的方案 ID，而不是每次都直接调 `loadSettings(for:)`？

**参考答案**：`rimeUpdate` 在**每次按键**后都会被调用。若不缓存去重，即便方案没变，每次按键都会重新打开配置、组装主题，造成巨大的无谓开销。缓存后只有 `schema_id` 真正变化（首次进入或用户切方案）时才触发重载。

**练习 2**：`loadSettings(for:)` 开头的 `if schemaID.count == 0 || schemaID.first == "." { return }` 各挡掉什么情况？

**参考答案**：空字符串（`count == 0`）挡掉 librime 尚未给出有效 schema_id 的瞬间；以 `.` 开头挡掉 librime 内部的隐藏/特殊方案。两者都不应触发前端主题重载。

---

### 4.4 回退到 base config：整配置级 vs 单项级

#### 4.4.1 概念说明

「回退」（fallback）在本讲里出现于**两个层级**，必须分清：

- **单项级回退**（per-key）：在 `SquirrelConfig` 的 `getBool/getString/...` 内部。读方案配置的某个键时，若该键在方案里不存在，就递归去 `baseConfig`（即 `squirrel.yaml`）里找。这是 [u3-l1](u3-l1-squirrel-config.md) 已建立的认识。
- **整配置级回退**（whole-config）：在 `loadSettings(for:)` 里。若方案**根本没有 `style:` 节**，则整个主题加载**不再使用方案配置**，而是直接拿全局 `config`（base config）去加载——也就是 4.1 的 `loadSettings()` 路径。

两者的差别是颗粒度：单项级回退是「这一个键找不到，去问爸爸」；整配置级回退是「这孩子整个 style 都没有，干脆别用它，直接用爸爸的全部」。

为什么需要整配置级回退？因为如果方案没有 `style:` 节，却仍然用方案配置去加载主题，那么 `SquirrelTheme.load` 读 `style/color_scheme` 时固然能单项回退到 base config，但语义上「这个方案没声明任何样式偏好」应当等同于「完全沿用全局样式」。整配置级回退让这层语义显式且高效——直接复用已组装好的全局 config，不必再走一遍方案配置的读取。

#### 4.4.2 核心流程

判定分界点是 `schema.has(section: "style")`：

```
loadSettings(for: schemaID)
  schema.open(schemaID, baseConfig: config) 成功？
    └─ 是 → schema.has(section: "style")？
        ├─ 是 → 用 schema 加载主题（方案特化，单项级回退仍可在 getXxx 内发生）
        └─ 否 → 用 config（base）加载主题（整配置级回退）
    └─ 否 → 用 config（base）加载主题（方案打不开，同样回退）
```

注意：即便走了「用 schema 加载」这条路径，`SquirrelTheme.load` 内部读 `style/color_scheme` 等键时，若某键在方案里没写，仍会单项级回退到 base config——两层回退是叠加的，不是互斥的。

#### 4.4.3 源码精读

整配置级回退就在 `loadSettings(for:)` 的 `if-else`：

[sources/SquirrelApplicationDelegate.swift:190-196](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L190-L196)（`&&` 短路：open 成功**且**含 style 节才用方案；否则 else 分支整体退回 `config`）：

```swift
if schema.open(schemaID: schemaID, baseConfig: config) && schema.has(section: "style") {
  panel.load(config: schema, forDarkMode: false)
  panel.load(config: schema, forDarkMode: true)
} else {
  panel.load(config: config, forDarkMode: false)
  panel.load(config: config, forDarkMode: true)
}
```

`has(section:)` 用 librime 的 `config_begin_map` 探测某节是否存在（存在则能成功 begin 一个迭代器）：

[sources/SquirrelConfig.swift:45-53](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L45-L53)：

```swift
func has(section: String) -> Bool {
  if isOpen {
    var iterator: RimeConfigIterator = .init()
    if rimeAPI.config_begin_map(&iterator, &config, section) {
      rimeAPI.config_end(&iterator)
      return true
    }
  }
  return false
}
```

对比单项级回退——以 `getBool` 为例，键不存在时返回 `baseConfig?.getBool(option)`：

[sources/SquirrelConfig.swift:65](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L65)（单项级回退的典型形态：当前配置读不到，递归问 baseConfig）：

```swift
return baseConfig?.getBool(option)
```

把两层回退并列对照：

| 维度 | 整配置级回退 | 单项级回退 |
| --- | --- | --- |
| 发生位置 | `loadSettings(for:)` | `SquirrelConfig.getXxx` |
| 触发条件 | 方案无 `style:` 节（或打开失败） | 某个具体键在当前配置缺失 |
| 颗粒度 | 整套样式 | 单个键 |
| 效果 | 直接用 base config 加载主题 | 该键值取自 base config，其余仍取方案 |

#### 4.4.4 代码实践

**实践目标**：对比「方案有 `style:` 节」与「方案无 `style:` 节」两种情况下，主题加载走的路径。

**操作步骤**（源码阅读 + 配置对照型）：

1. 在 [sources/SquirrelApplicationDelegate.swift:191](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L191) 与 [sources/SquirrelApplicationDelegate.swift:194](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L194) 各加一行日志：
   ```swift
   print("[fallback] schema=\(schemaID) 用方案样式")      // if 分支
   print("[fallback] schema=\(schemaID) 回退全局")        // else 分支
   ```
   > **示例代码**，仅本地观察用。
2. 准备两个方案：
   - 方案 X：带有 `style:` 节（如 4.3.4 中的双拼方案）。
   - 方案 Y：不带 `style:` 节（多数纯拼音方案的默认形态）。
3. 部署后分别切换到 X 与 Y，观察日志。

**需要观察的现象**：切到 X 打印「用方案样式」，切到 Y 打印「回退全局」。

**预期结果**：验证 `has(section: "style")` 是整配置级回退的唯一判据——只要方案声明了 `style:` 节（哪怕里面只写了一个键），就走方案路径；否则整体退回 base config。

> 待本地验证：若无可运行的方案文件，可纯阅读 `has(section:)` 与 `loadSettings(for:)` 的 `&&` 短路逻辑推演——重点是理解「打开成功但无 style 节」与「打开失败」两种情况都落入同一 else 分支。

#### 4.4.5 小练习与答案

**练习 1**：某方案文件里只写了 `style: { color_scheme: aqua }`，没有任何其他样式键。切换到该方案后，面板的 `font_point` 来自哪里？

**参考答案**：因为方案**含有** `style:` 节，走「用方案加载」路径。`SquirrelTheme.load` 读 `style/font_point` 时，该键在方案里不存在，触发**单项级回退**，从 base config（`squirrel.yaml`）读取。所以 `color_scheme` 取自方案（`aqua`），`font_point` 取自全局——两层回退叠加。

**练习 2**：如果把 `loadSettings(for:)` 里的 `&& schema.has(section: "style")` 删掉、只保留 `schema.open(...)`，会有什么不同？

**参考答案**：那么只要方案文件能打开（即便它没有 `style:` 节），就会用方案配置去加载主题。此时 `SquirrelTheme.load` 读所有 `style/*` 键都会单项级回退到 base config，最终结果与走 else 分支**外观相同**，但多了一次「打开方案配置 + 逐键单项回退」的开销，且语义上把「方案没有样式偏好」错误地表达成了「方案有样式配置」。所以 `has(section:)` 既是优化也是语义澄清。

---

## 5. 综合实践

把本讲四个模块串起来，做一次完整的「主题加载链路追踪」。

**任务**：在一份新的 `~/Library/Rime/squirrel.yaml` 里配置「亮主题用 native、暗主题用具体方案」，并准备一个带 `style:` 节的方案，追踪从启动到切方案的完整主题加载过程。

**步骤**：

1. 配置 `~/Library/Rime/squirrel.yaml`：
   ```yaml
   style:
     color_scheme: native
     color_scheme_dark: clean_white
   ```
2. 准备一个带 `style:` 节的方案（如 4.3.4 的双拼方案）。
3. 在以下四个位置加临时日志（**示例代码**，本地观察后还原）：
   - [sources/SquirrelApplicationDelegate.swift:179](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L179)：`print("[boot] 全局亮暗主题加载")`
   - [sources/SquirrelTheme.swift:231](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L231)：`print("[theme] dark=\(dark) native=\(native)")`
   - [sources/SquirrelApplicationDelegate.swift:191](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L191) 与 :194：标注用方案 / 回退全局
   - [sources/SquirrelView.swift:43](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L43)：`print("[pick] isDark=\(isDark) 用 \(currentTheme === darkTheme ? "dark" : "light")")`
4. 部署、启动 Squirrel，先切到拼音（无 style 节），再切到双拼（有 style 节），最后切换系统亮暗外观。
5. 对照日志，画出主题加载的时序：启动 → 全局亮暗各 load 一次（亮 native、暗 clean_white）→ 切拼音走「回退全局」→ 切双拼走「用方案样式」→ 系统外观切换时 `currentTheme` 在 light/dark 间即时换。

**预期结果**：你应当能用自己的话复述这条链路，并解释每一步 `native`/`available` 标志的取值，以及为什么 `show()` 在「亮 native + 暗具体方案」这种组合下窗口跟随系统外观。

> 待本地验证：完整链路需 macOS 图形环境。无图形环境时，至少完成日志注入后的源码逻辑推演，把上述时序写下来交给同伴 review。

## 6. 本讲小结

- Squirrel 在**启动时**就用 `loadSettings()` 把亮、暗两套主题分别组装进 `view.lightTheme` 与 `view.darkTheme`，运行时 `currentTheme` 按系统外观即时二选一，避免显示路径上读配置。
- `native` 与 `available` 是两个含义不同的标志：`native` 表示「是否用系统语义色」（`color_scheme` 非 `"native"` 才 false），`available` 表示「该主题是否被配置」（对应键缺失才 false）；亮、暗两套各自独立判定。
- `show()` 据 `theme.native || darkTheme.available` 决定窗口 appearance——仅在「只配了非 native 亮主题、且无暗主题」时强制窗口为亮色，避免亮配色画在暗窗口里。
- 切换输入方案时，`rimeUpdate` 检测 `schema_id` 变化（用 `schemaId` 缓存去重），触发 `loadSettings(for:)` 重载主题，实现 schema 特化样式。
- 回退分两层：**整配置级**（方案无 `style:` 节则整体退回 base config）与**单项级**（`getXxx` 内部逐键回退）；两者叠加，让方案只覆盖它想覆盖的键。

## 7. 下一步学习建议

本讲把「主题在运行期如何被管理与切换」讲完，主题体系（[u3-l1](u3-l1-squirrel-config.md) ~ u3-l4）就此收尾。接下来有两个方向：

- **纵向深入 UI 绘制**：主题对象（`SquirrelTheme`）最终被 `SquirrelPanel.update` 与 `SquirrelView.draw` 消费，把配色、字体、布局画成可见的面板。建议进入第四单元，从 [u4-l1 SquirrelPanel 模型与 update](u4-l1-panel-model-update.md) 开始，看主题属性如何变成富文本与绘制路径。
- **横向回到主链路**：若你想再看一次「schema 切换 → 主题重载 → 面板刷新」的完整输入流，可重温 [u2-l6 rimeUpdate 数据流](u2-l6-rime-update-dataflow.md)，把本讲的 `loadSettings(for:)` 放回它所属的 `get_status` 段中理解。

建议阅读源码顺序：先重读 [SquirrelPanel.swift 的 show()](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L361) 看 appearance 判定，再读 [SquirrelView.swift 的 currentTheme](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L42) 看运行时选择，最后带着这些认知进入第四单元。
