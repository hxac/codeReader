# 本地化与 CI

## 1. 本讲目标

本讲是第五单元（系统集成与扩展）的收尾篇。前面几讲关注的是「Squirrel 跑起来之后做了什么」，本讲把视角拉到工程的「外圈」——当一个普通中文用户安装 Squirrel 时，为什么菜单里显示的是「鼠须管」而不是「Squirrel」？为什么开发者改了一行代码，CI 会自动告诉他「这个函数太复杂了」或「这段代码没人用」？为什么 `make release` 在本地能跑、在云端也能跑出几乎一样的产物？

读完本讲，你应当能够：

1. 说清 `.xcstrings` 字符串目录（String Catalog）的 JSON 结构，以及 `Localizable.xcstrings` 与 `InfoPlist.xcstrings` 的分工。
2. 追踪一个本地化键（如 `deploy_success`）从 YAML 事件触发，到 `NSLocalizedString` 查表，再到屏幕显示的完整路径。
3. 读懂 `.swiftlint.yml` 的规则配置，并能解释 `swiftlint:disable:next`、`swiftlint:disable`/`swiftlint:enable` 三种抑制注释各自的适用场景。
4. 描述三条 GitHub Actions 工作流（commit / pull-request / release）的触发条件、共享流水线与差异点。
5. 解释 `action-build.sh` 如何通过 `ARCHS`、`BUILD_UNIVERSAL`、`SQUIRREL_BUNDLED_RECIPES` 等环境变量驱动 `make`，以及它调用的 `action-install.sh` 在 CI 里扮演「下载预编译依赖」的角色。

---

## 2. 前置知识

本讲依赖 u1-l3（构建与运行）建立的「Squirrel 构建 = 先备四件依赖、再编译 Swift 前端」认知。如果你还没读过，建议先看一眼 `Makefile` 里的 `DEPS_CHECK` 与 `release`/`package` 目标。

本讲涉及的几个基础概念，先用大白话解释：

- **本地化（Localization / i18n）**：让同一个程序在不同语言环境下显示不同文字。例如英文系统看到 "Squirrel"，简体中文系统看到「鼠须管」，繁体中文系统看到「鼠鬚管」。
- **字符串目录（String Catalog，`.xcstrings`）**：Xcode 15 引入的本地化文件格式，本质是一个 JSON 文件，把「源语言文字 / 键」与「各语言译文」对应起来，替代了旧版的 `.strings` + `.stringsdict` 两套文件。
- **`Info.plist`**：macOS App 的身份证文件，记录 App 名称、版本、图标等元信息。其中部分字段（如 `CFBundleName`）可以被本地化——不同语言系统下显示不同名字。
- **Lint（静态检查）**：不运行代码，只读源码文本就能发现「命名太短」「函数太长」「圈复杂度太高」等问题。SwiftLint 是 Swift 生态里最主流的 lint 工具。
- **CI（Continuous Integration，持续集成）**：每次提交代码，云端自动跑一遍「检查 + 编译 + 打包」，把「人为忘记的事」变成机器强制执行的事。Squirrel 用 GitHub Actions 实现 CI。
- **Universal Binary（通用二进制）**：同一个可执行文件同时包含 arm64（Apple Silicon）和 x86_64（Intel）两份机器码，在两种 Mac 上都能原生运行。

---

## 3. 本讲源码地图

本讲涉及的文件横跨「资源」「工程配置」「CI 脚本」三类，全部是配置/脚本，不是 Swift 运行逻辑源码：

| 文件 | 作用 | 本讲用来讲什么 |
| --- | --- | --- |
| `resources/Localizable.xcstrings` | 应用界面文案的字符串目录（菜单项、部署提示、更新提示） | xcstrings 的 JSON 结构与 `NSLocalizedString` 查表 |
| `resources/InfoPlist.xcstrings` | `Info.plist` 字段的字符串目录（App 名、输入源名、版权） | Info.plist 本地化与输入源 ID 的关系 |
| `resources/Info.plist` | App 元信息 | 本地化的「源语言」与开发区域声明 |
| `.swiftlint.yml` | SwiftLint 规则配置 | 规则阈值与扫描范围 |
| `sources/*.swift`（多处） | 源码 | `swiftlint:disable` 抑制注释与 `NSLocalizedString` 调用点 |
| `.github/workflows/commit-ci.yml` | 提交触发的工作流 | CI 流水线主体 |
| `.github/workflows/pull-request-ci.yml` | PR 触发的工作流 | 与 commit-ci 的差异（产物保留期） |
| `.github/workflows/release-ci.yml` | 打 tag / master 触发的工作流 | 归档构建与发布 |
| `action-build.sh` | CI 构建入口脚本 | 环境变量如何驱动 `make` |
| `action-install.sh` | CI 依赖下载脚本 | 在云端下载预编译的 librime / Sparkle |
| `action-changelog.sh` | 发布日志生成脚本 | tag 发布时生成 changelog |
| `Makefile` | 本地与 CI 共用的构建脚本 | `release` / `package` / `archive` 目标 |

---

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：xcstrings 本地化、SwiftLint 规则与抑制注释、GitHub Actions 工作流、action-build.sh 构建脚本。

### 4.1 xcstrings 字符串目录本地化

#### 4.1.1 概念说明

Squirrel 的界面文字分两类，分别由两个 `.xcstrings` 文件管理：

1. **`Localizable.xcstrings`**：管理「代码里主动取的文案」，例如菜单项「重新部署」「同步用户数据」「日志...」，以及部署引擎时的状态提示「部署完成。」「有错误！请查看日志」。这类文字在 Swift 代码里通过 `NSLocalizedString(_ key:comment:)` 按「键」查表取值。
2. **`InfoPlist.xcstrings`**：管理「`Info.plist` 里的字段」，例如 App 显示名 `CFBundleName`、`CFBundleDisplayName`、版权 `NSHumanReadableCopyright`，以及两个输入源 ID `im.rime.inputmethod.Squirrel.Hans` / `.Hant` 的可读名（详见 u5-l2）。这类文字由系统在读取 `Info.plist` 时自动按当前语言替换，不需要代码显式调用。

> 小贴士：为什么把界面文案和 Info.plist 文案分成两个文件？因为它们的「消费者」不同——前者是你的 Swift 代码，后者是 macOS 系统本身。分开存放让构建系统能用不同规则把它们编译进 App 包的不同位置。

`.xcstrings` 文件的核心思想是「以源语言为锚」。Squirrel 的源语言是英文（`en`），所以每个键要么本身就是一句英文（如 `"Deploy"`，菜单原文），要么是一个程序化键名（如 `"deploy_success"`，这种键不直接给人看，必须为所有语言——包括英文——提供译文）。

#### 4.1.2 核心流程

一个本地化字符串从「写成 JSON」到「显示在屏幕」的流程：

```
开发者写 .xcstrings JSON（键 + 各语言译文）
        │
        ▼
Xcode 构建 ── 把 .xcstrings 编译进 Squirrel.app 的 .lproj 目录
        │
        ▼
程序运行，系统读取用户当前语言（如 zh-Hans）
        │
   ┌────┴────────────────────┐
   ▼                         ▼
NSLocalizedString(key, comment)     系统 reads Info.plist 字段
   │  按 key 在 .lproj 查表          │  按 key 自动替换
   ▼                                 ▼
返回 zh-Hans 对应的译文              返回本地化的 App 名
```

`NSLocalizedString` 的两个参数含义：

- `key`：在 `.xcstrings` 里查表用的键，通常就是源语言原文（如 `"Deploy"`），也可以是程序化键名（如 `"deploy_success"`）。
- `comment`：给翻译者看的上下文说明。它会原样写进 `.xcstrings` 的 `comment` 字段，不影响运行时取值，但能帮人理解这个字符串用在哪里（如 `"Menu item"`）。

#### 4.1.3 源码精读

先看 `Localizable.xcstrings` 的文件头与源语言声明：

- [resources/Localizable.xcstrings:1-3](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Localizable.xcstrings#L1-L3) 声明 `"sourceLanguage" : "en"`，整个文件以英文为锚。

再看一个程序化键 `deploy_success` 的完整定义——它同时提供了 `en` / `zh-Hans` / `zh-Hant` 三种语言译文：

- [resources/Localizable.xcstrings:99-120](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Localizable.xcstrings#L99-L120) `deploy_success` 在英文下是 "Squirrel is ready."，简体下是「部署完成。」，繁体下也是「部署完成。」。

注意结构：每个键下有 `localizations` 字典，键是语言代码（`en` / `zh-Hans` / `zh-Hant`），值是一个含 `stringUnit` 的对象；`stringUnit.state` 通常为 `"translated"`（已翻译），`stringUnit.value` 才是真正的译文。

对照看「菜单项」类键的写法——它的键就是英文原文，并且带 `comment`：

- [resources/Localizable.xcstrings:38-54](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Localizable.xcstrings#L38-L54) `"Deploy"` 键，`comment` 为 `"Menu item"`，简体译「重新部署」、繁体也译「重新部署」。注意这类「键即英文」的条目没有单独的 `en` 译文，因为键本身就是英文源语言值。

接下来看代码里如何取这些键。`deploy_*` 系列键的消费者是 `notificationHandler`——librime 在部署引擎时回调这个函数，函数按 `messageValue`（`start` / `success` / `failure`）查不同的本地化键：

- [sources/SquirrelApplicationDelegate.swift:272-283](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L272-L283) 收到 librime 的 `"deploy"` 通知后，用 `switch messageValue` 分别取 `deploy_start` / `deploy_success` / `deploy_failure` 三个键，调 `showMessage` 弹给用户。

这里能清楚看到 `comment` 参数传空串 `""`（因为这些键是程序化键名，不需要给翻译者额外上下文），而菜单项则传了有意义的 `comment`：

- [sources/SquirrelInputController.swift:232-243](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L232-L243) 用 `NSLocalizedString("Deploy", comment: "Menu item")` 等构造菜单项标题，`comment` 与 `.xcstrings` 里的 `"comment" : "Menu item"` 完全对应。

再看 `InfoPlist.xcstrings` 的不同之处——它的键是 `Info.plist` 的字段名或输入源 ID：

- [resources/InfoPlist.xcstrings:74-96](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/InfoPlist.xcstrings#L74-L96) 键 `im.rime.inputmethod.Squirrel.Hans`（简体输入源 ID）在英文下显示 "Squirrel - Simplified"，简繁下都显示「鼠须管」。这个字符串会被系统输入法菜单直接读取，不需要 Swift 代码调用。

最后看 `Info.plist` 里对源语言的声明，它是本地化的总开关：

- [resources/Info.plist:7-8](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L7-L8) `CFBundleDevelopmentRegion` 设为 `English`，与 `.xcstrings` 的 `sourceLanguage: en` 一致——告诉系统「这个 App 的原生语言是英文，其它语言是翻译」。

#### 4.1.4 代码实践

**实践目标**：亲手追踪一个本地化键从「JSON 定义」到「代码引用」的完整链路。

**操作步骤**：

1. 打开 `resources/Localizable.xcstrings`，定位到 `deploy_failure` 键（约第 55 行起）。
2. 用 `Grep` 在 `sources/` 里搜索 `deploy_failure`，找到引用它的 Swift 文件与行号。
3. 打开该 Swift 文件对应行，确认它被包在哪个 `switch` 分支里、由什么 `messageValue` 触发。
4. 思考：如果用户系统语言是日文（Squirrel 未提供 `ja` 译文），`NSLocalizedString("deploy_failure", ...)` 会返回什么？

**需要观察的现象**：`deploy_failure` 在 JSON 里同时定义了 `en` / `zh-Hans` / `zh-Hant` 三种译文；代码引用点只有一处（`SquirrelApplicationDelegate.swift` 的 `notificationHandler`）。

**预期结果**：

- `deploy_failure` 的英文值是 "Error occurred. Please check log files"，简体是「有错误！请查看日志」。
- 当系统语言找不到对应译文时，`NSLocalizedString` 回退到**源语言**——这里源语言是 `en`，所以日文系统会显示英文 "Error occurred. Please check log files"。

**待本地验证**：以上回退行为是 Apple Foundation 的标准约定，可在不同语言系统的 Mac 上实测确认。

#### 4.1.5 小练习与答案

**练习 1**：`Localizable.xcstrings` 里的键 `"Deploy"` 为什么没有单独的 `en` 译文，而 `"deploy_success"` 却有？

**参考答案**：`"Deploy"` 采用「键即英文原文」的写法，键本身就是源语言（英文）的值，Xcode 自动把它当作英文，无需重复写一份 `en`。`"deploy_success"` 是程序化键名（不是任何自然语言），所以必须为包括英文在内的每种语言显式提供译文，否则英文系统会原样显示 `deploy_success` 这个字符串。

**练习 2**：如果要让 Squirrel 的菜单在繁体系统下把「重新部署」改成「重新建置」，应该改哪个文件的哪一行？

**参考答案**：改 `resources/Localizable.xcstrings` 里 `"Deploy"` 键的 `zh-Hant.stringUnit.value`（当前是「重新部署」）。改完需重新构建 App 才生效，因为 `.xcstrings` 要被编译进 `.lproj` 才能被运行时读取。

**练习 3**：`InfoPlist.xcstrings` 与 `Localizable.xcstrings` 最大的区别是什么？

**参考答案**：消费者不同。`Localizable.xcstrings` 由你的 Swift 代码经 `NSLocalizedString` 主动查表；`InfoPlist.xcstrings` 由 macOS 系统在读取 `Info.plist`（如 App 名、输入源名）时自动替换，Swift 代码不直接参与。

---

### 4.2 SwiftLint 规则与抑制注释

#### 4.2.1 概念说明

SwiftLint 是 Swift 的静态分析工具，读 `.swift` 源码就能报告几百类问题：命名风格、函数长度、圈复杂度、强制类型转换等。Squirrel 用一个 `.swiftlint.yml` 配置文件统一约定「哪些规则开、哪些规则关、阈值是多少」，并在 CI 里强制执行——lint 不过，构建中断。

规则分三档：

- **默认规则**：SwiftLint 自带的 sensible defaults，开箱即用。
- **`opt_in_rules`**：默认关闭、需手动开启的规则（如 `explicit_self`）。
- **`analyzer_rules`**：需要跨文件分析（基于索引）才能跑的规则，用 `swiftlint analyze` 触发。

但有些场景下「违规」是不可避免的——比如桥接 C API 时遇到的 `snake_case` 变量名、天然分支多的事件分发函数。SwiftLint 提供**抑制注释（suppression comment）**，允许你在特定位置局部豁免某条规则，并要求写明豁免哪条规则、为什么。

#### 4.2.2 核心流程

SwiftLint 的运行流程：

```
swiftlint（在仓库根目录运行）
   │
   ▼
读取 .swiftlint.yml（确定规则集 + 阈值 + 扫描范围）
   │
   ▼
遍历 included 路径（这里只扫 sources/）下所有 .swift
   │
   ▼
对每个文件按规则检查，遇到 // swiftlint:disable 注释则局部跳过
   │
   ▼
按 reporter（github-actions-logging）输出 ── 在 PR 上变成行内批注
```

三档规则严重级别（针对带阈值的规则）：

- 仅 `warning`：只警告，不阻断。
- `error`：当作错误，会让 `swiftlint` 命令以非零状态码退出 → CI 失败。
- `strict: true` 时所有 warning 都升为 error。Squirrel 设 `strict: false`，即「警告不致命」。

#### 4.2.3 源码精读

先看 `.swiftlint.yml` 的规则开关与扫描范围：

- [.swiftlint.yml:2-5](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.swiftlint.yml#L2-L5) `disabled_rules` 关掉三条默认规则：`force_cast`（`as!` 强制转型）、`force_try`（`try!`）、`todo`（TODO 注释）。Squirrel 选择容忍这些，因为桥接 C 时偶有必要的强制转换。
- [.swiftlint.yml:13-14](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.swiftlint.yml#L13-L14) `analyzer_rules` 开启 `explicit_self`（要求显式写 `self.`）。
- [.swiftlint.yml:16-17](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.swiftlint.yml#L16-L17) `included: sources`——**只扫描 `sources/` 目录**，这意味着 `plum/`、`librime/` 等子模块代码不受 Squirrel 自己的 lint 约束。

再看阈值规则——这些是「超标才报」的规则，决定了抑制注释为什么会出现：

- [.swiftlint.yml:28-29](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.swiftlint.yml#L28-L29) `line_length: 200`、`function_body_length: 200`——单行/函数体超过 200 字符才警告。
- [.swiftlint.yml:47-51](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.swiftlint.yml#L47-L51) `identifier_name` 要求标识符最小长度 warning 3 / error 2，并把 `i`、`URL`、`of`、`by` 列入豁免名单。

> 名词解释——**圈复杂度（cyclomatic complexity）**：衡量一个函数里独立执行路径数量的指标，粗略等于 `if/else/switch/case/for/while/&&/||` 的分支点数加一。分支越多，越难测试与维护。SwiftLint 的 `cyclomatic_complexity` 规则默认 warning 10 / error 20。

接着看源码里三类抑制注释的真实用法。

**第一类：`// swiftlint:disable:next <rule>` ——只豁免紧接的下一行。** 入口函数 `main()` 要分派大量命令行参数（`--quit`/`--reload`/`--build`/...），`switch` 分支天然很多，圈复杂度必然超标：

- [sources/Main.swift:23-24](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L23-L24) 在 `static func main()` 上一行写 `// swiftlint:disable:next cyclomatic_complexity`，告诉 lint「这个函数分支多是设计使然，下一行（函数声明）的圈复杂度警告请跳过」。

同样的模式还出现在 librime 回调 `notificationHandler`（要分派 deploy/option/property/schema 多类消息）：

- [sources/SquirrelApplicationDelegate.swift:265-266](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L265-L266) 给 `notificationHandler` 函数豁免 `cyclomatic_complexity`。

**第二类：`// swiftlint:disable <rule>` … `// swiftlint:enable <rule>` ——豁免一段代码块。** 桥接 librime C 结构时，字段名是 `snake_case`（如 `select_keys`、`select_labels`），违反 Swift 的 `identifier_name`（应驼峰）。Squirrel 用成对注释把这一小段整块豁免：

- [sources/SquirrelInputController.swift:528-537](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L528-L537) 先 `// swiftlint:disable identifier_name`，使用 `select_keys` / `select_labels` 两个 C 风格变量名，再 `// swiftlint:enable identifier_name` 关闭豁免。这样「污染」被限制在这一段，不会扩散。

**第三类：自定义运算符的空白规则。** Squirrel 在桥接层自定义了 `?=` 可选赋值运算符（见 u5-l4），它的定义语法触犯 `operator_whitespace` 规则：

- [sources/BridgingFunctions.swift:45](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L45) `// swiftlint:disable:next operator_whitespace` 豁免下一行 `func ?=` 的运算符空白检查——因为自定义运算符的声明本就无法满足标准运算符的空白约定。

最后看输出方式——这决定了 lint 结果如何在 CI 里呈现：

- [.swiftlint.yml:55](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.swiftlint.yml#L55) `reporter: "github-actions-logging"` 让 SwiftLint 输出 GitHub Actions 格式，违规会变成 PR 的行内批注（annotation），开发者点开 diff 就能看到「第几行违反了哪条规则」。

#### 4.2.4 代码实践

**实践目标**：体会「阈值规则」与「抑制注释」的配合关系。

**操作步骤**：

1. 读 `.swiftlint.yml` 第 28-29 行，记下 `function_body_length: 200` 的阈值。
2. 用 `Grep` 在 `sources/` 搜索 `swiftlint:disable:next cyclomatic_complexity`，统计它出现在哪几个文件、对应哪几个函数。
3. 任选一处（如 `sources/SquirrelPanel.swift` 的某处），读该函数体，数一数它的 `if/switch case/for` 分支点，估算圈复杂度。
4. 思考：如果删掉那行 `swiftlint:disable:next` 注释，CI 会发生什么？

**需要观察的现象**：被豁免的函数都是「事件分发 / 状态分派」类的大 `switch`，分支点多但每个分支逻辑简单；阈值规则（如圈复杂度默认 warning 10）确实会被这些函数触发。

**预期结果**：

- 删掉抑制注释后，`swiftlint` 会针对该函数报告 `cyclomatic_complexity` warning。由于 `strict: false`，warning 本身不致命；但 `reporter: github-actions-logging` 会在 PR 里显示批注，提醒维护者。
- 这正是抑制注释的价值：**显式声明「这里故意如此」**，而不是让噪声告警淹没真正的违规。

**待本地验证**：如本地装了 swiftlint（`brew install swiftlint`），可删注释后跑 `swiftlint` 复现告警。

#### 4.2.5 小练习与答案

**练习 1**：`.swiftlint.yml` 里 `included: sources` 有什么实际效果？为什么不把整个仓库都纳入扫描？

**参考答案**：只扫描 `sources/` 目录下的 Swift 文件。Squirrel 仓库里还有 `librime/`、`plum/`、`Sparkle/` 等 git 子模块，它们的代码不属于 Squirrel 项目、且各有各的代码规范，纳入扫描既会爆大量无关告警、也越权改不了别人家的代码，所以必须排除。

**练习 2**：`// swiftlint:disable:next cyclomatic_complexity` 与 `// swiftlint:disable cyclomatic_complexity`（无 `:next`）的区别是什么？

**参考答案**：前者只豁免**紧随其后的下一行**（通常是函数声明那一行），范围最小、最精准；后者会一直生效直到遇到 `// swiftlint:enable cyclomatic_complexity`，豁免**中间所有行**。前者适合「单个函数超标」，后者适合「一段代码整体违反某规则」（如 C 风格变量名聚集的代码块）。

**练习 3**：为什么 `disabled_rules` 里要关掉 `force_cast`？

**参考答案**：Squirrel 通过桥接层大量调用 librime 的 C API，处理 `UnsafePointer<CChar>`、`Unmanaged` 等指针时偶有需要强制转型（`as!`）的场合。强行禁用会让这些必要的桥接代码无法通过 lint，所以项目选择在配置层关掉该规则，转而在具体必要时用局部抑制——这是一种「项目级取舍」。

---

### 4.3 GitHub Actions 工作流

#### 4.3.1 概念说明

Squirrel 在 `.github/workflows/` 下放了三条工作流，分别对应三种代码生命周期阶段：

| 工作流 | 触发时机 | 主要产物 |
| --- | --- | --- |
| `commit-ci.yml` | 推送到任意分支 | 可安装 `.pkg`（保留 90 天） |
| `pull-request-ci.yml` | 提交或更新 PR | 可安装 `.pkg`（保留 30 天） |
| `release-ci.yml` | 打 tag、推 master、手动触发 | 归档包 + GitHub Release（草稿 / nightly） |

三者共享几乎相同的「检查 + 构建」流水线，差异只在「触发条件」和「产物去向」。这是 CI 设计的常见模式：**用一套核心步骤覆盖所有场景，只在入口和出口做差异化**。

工作流跑在 `macos-26` runner 上（GitHub 托管的 macOS 云主机），并固定 Xcode 版本（`26.5`），保证「云端编译环境」与维护者本机一致、且不随时间漂移。

#### 4.3.2 核心流程

以最典型的 `commit-ci.yml` 为例，其流水线如下：

```
push 到任意分支
   ▼
checkout（含子模块 submodules: true）
   ▼
固定 Xcode 26.5            ← 保证工具链一致
   ▼
brew install swiftlint → swiftlint      ← 静态检查
   ▼
./action-build.sh package                ← 编译 + 打 pkg
   ▼
periphery scan                            ← 死代码检查
   ▼
upload-artifact（package/*.pkg，保留 90 天）
```

关键设计点：

1. **`submodules: true`**：Squirrel 依赖 librime / plum / Sparkle 三个子模块，checkout 时必须递归拉取，否则后续构建找不到依赖。
2. **lint 在 build 之前**：先静态检查、再编译，让「低级错误」尽早暴露，省下昂贵的 macOS 编译时间。
3. **`periphery scan`**：用 [Periphery](https://github.com/peripheryapp/periphery) 扫描「声明了但没人引用」的代码（死代码），复用上一步 `action-build.sh` 产生的索引（`--index-store-path build/Index.noindex/DataStore`），不必重新编译（`--skip-build`）。
4. **`git describe --always`**：把当前 commit 的简短描述（如 tag 名或短 hash）写进 `$GITHUB_ENV`，用作产物文件名，方便回溯是哪次提交构建的。

#### 4.3.3 源码精读

先看 `commit-ci.yml` 的触发与运行环境：

- [.github/workflows/commit-ci.yml:1-8](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.github/workflows/commit-ci.yml#L1-L8) `on.push.branches: ['*']` 监听所有分支的推送，`runs-on: macos-26` 用 macOS 26 runner。
- [.github/workflows/commit-ci.yml:10-18](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.github/workflows/commit-ci.yml#L10-L18) 固定 Xcode `26.5`，`actions/checkout@v6` 带 `submodules: true`。

lint 步骤——这一步直接消费本讲 4.2 节讲的 `.swiftlint.yml`：

- [.github/workflows/commit-ci.yml:20-24](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.github/workflows/commit-ci.yml#L20-L24) `brew install swiftlint` 装工具，然后裸跑 `swiftlint`（它自动找仓库根的 `.swiftlint.yml`）。

构建步骤——把 `package` 目标透传给 `action-build.sh`：

- [.github/workflows/commit-ci.yml:30-31](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.github/workflows/commit-ci.yml#L30-L31) `./action-build.sh package`。这正是 4.4 节要讲脚本的入口。

死代码检查与产物上传：

- [.github/workflows/commit-ci.yml:36-37](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.github/workflows/commit-ci.yml#L36-L37) `periphery scan --relative-results --skip-build --index-store-path build/Index.noindex/DataStore`，复用构建产物里的索引数据。
- [.github/workflows/commit-ci.yml:39-45](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.github/workflows/commit-ci.yml#L39-L45) `upload-artifact` 把 `package/*.pkg` 上传为 `Squirrel-<git_ref_name>.zip`，保留 90 天。

`pull-request-ci.yml` 与 `commit-ci.yml` 几乎逐行相同，差异只有两处：触发器换成 `on: [pull_request]`、产物保留期 30 天（PR 评审窗口比分支短，省存储）。

`release-ci.yml` 则在出口处多了「发布」逻辑，是三条里最复杂的：

- [.github/workflows/release-ci.yml:1-8](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.github/workflows/release-ci.yml#L1-L8) 触发条件是「打 tag」**或**「推 master」**或**「手动 `workflow_dispatch`」。
- [.github/workflows/release-ci.yml:31-32](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.github/workflows/release-ci.yml#L31-L32) 调 `./action-build.sh archive`（注意是 `archive` 而非 `package`，多打包一份归档与 `sign_update`，供 Sparkle 更新用——见 u5-l5）。

  > 名词解释——**tag 触发 vs master 触发**：在 Git 里，tag 是给某个 commit 打的「永久书签」，通常用于标记发布版本。`release-ci` 用 `if: startsWith(github.ref, 'refs/tags/')` 区分「这是正式打 tag」还是「只是 master 日常推送」，两者走不同的发布路径。

- [.github/workflows/release-ci.yml:40-46](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.github/workflows/release-ci.yml#L40-L46) 仅在打 tag 时，调 `action-changelog.sh` 生成两个 tag 之间的提交日志，写进 `$GITHUB_OUTPUT`。
- [.github/workflows/release-ci.yml:48-56](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.github/workflows/release-ci.yml#L48-L56) 打 tag 时用 `ncipollo/release-action` 创建一个**草稿 Release**（`draft: true`），把 `package/Squirrel-*.pkg` 挂为附件，正文填上一步的 changelog——草稿不公开，需维护者手动点「发布」。
- [.github/workflows/release-ci.yml:58-66](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.github/workflows/release-ci.yml#L58-L66) 推 master 时（且仓库确为 `rime/squirrel`）创建 **nightly 构建**，打 `latest` 标签、标记为预发布（`prerelease: true`），覆盖上一次 nightly。

#### 4.3.4 代码实践

**实践目标**：对比三条工作流，找出它们的「共享骨架」与「差异出口」。

**操作步骤**：

1. 并排打开 `commit-ci.yml`、`pull-request-ci.yml`、`release-ci.yml`。
2. 列出三者**完全相同**的步骤（如 Set Xcode、Checkout、Install SwiftLint、Lint、Install periphery、Check Unused Code）。
3. 找出三者的差异点：触发器、`action-build.sh` 的参数（`package` vs `archive`）、产物保留期、是否创建 Release。
4. 思考：为什么 `release-ci.yml` 在 checkout 时多带了 `fetch-depth: 0`？

**需要观察的现象**：三条工作流的 lint + build + periphery 三步是逐字相同的；差异全部集中在「触发器」和「产物出口（artifact vs release）」两端。

**预期结果**：

- 共享骨架：固定 Xcode → checkout 子模块 → swiftlint → action-build.sh → periphery scan。
- `fetch-depth: 0` 是为了拉取**完整 git 历史**（默认 `actions/checkout` 只取最近一个 commit），因为 `action-changelog.sh` 要用 `git describe --tags` 与 `git log` 跨 tag 查历史，浅克隆会让这些命令找不到上一个 tag。

#### 4.3.5 小练习与答案

**练习 1**：`commit-ci.yml` 和 `pull-request-ci.yml` 步骤几乎一样，为什么不合并成一条？

**参考答案**：虽然步骤相同，但「触发时机」和「产物保留策略」不同——commit 触发于所有分支推送（包括维护者的私有分支），保留 90 天；PR 触发于公开评审，保留 30 天。分开定义让两条流水线的产物互不干扰、保留期各自独立，也便于在 PR 页面只看到 PR 自己的产物。这是「同骨架、不同策略」的有意拆分。

**练习 2**：如果开发者提交了一段含未使用私有函数的代码，CI 的哪一步会失败？

**参考答案**：`periphery scan`（"Check Unused Code" 步骤）会报告该死代码并使 CI 失败。lint（`swiftlint`）管的是「代码风格」，periphery 管的是「代码是否真的被用到」，两者互补。

**练习 3**：`release-ci.yml` 里 `if: startsWith(github.ref, 'refs/tags/')` 这个条件的作用是什么？

**参考答案**：判断当前触发是不是「打了一个 tag」。只有打 tag（正式发版）才走「生成 changelog + 创建草稿 Release」路径；如果只是推 master，则走「nightly 构建」路径，两者共用同一份构建产物但发布方式不同。

---

### 4.4 action-build.sh 构建脚本

#### 4.4.1 概念说明

`action-build.sh` 是 CI 的构建总入口——三条 GitHub Actions 工作流最后都落到这个脚本上。它的角色是「**把 CI 环境特有的变量注入，再调 `make`**」。

回顾 u1-l3：`Makefile` 是本地与 CI 共用的构建脚本，里面定义了 `release` / `debug` / `package` / `archive` 等目标，并通过 `BUILD_SETTINGS` 累加 `xcodebuild` 的覆盖参数（如 `ARCHS`、`BUILD_UNIVERSAL`）。`action-build.sh` 做的就是「在调 `make` 之前，把这些环境变量 `export` 出去」，让一次 `make` 在 CI 上产出与维护者本机一致的 universal 二进制。

它还调一个姐妹脚本 `action-install.sh`，后者负责「**下载预编译依赖**」——在 CI 上不从头编译 librime / Sparkle（太慢），而是直接从 GitHub Releases 拉官方预编译产物。

#### 4.4.2 核心流程

`action-build.sh` 的执行流程：

```
target = 第一个命令行参数，缺省 "release"
   ▼
export ARCHS='arm64 x86_64'      ← 两种架构
export BUILD_UNIVERSAL=1         ← 要求 universal 二进制
export SQUIRREL_BUNDLED_RECIPES  ← 内置 Rime 方案配方
   ▼
./action-install.sh              ← 下载 librime / Sparkle / plum 配方
   ▼
make "${target}"                 ← 真正构建（release/package/archive）
   ▼
find package -name '*.pkg' ...   ← 列出产物
```

`target` 取值决定产物形态：

- `release` / `debug`：只编译出 `Squirrel.app`（在 `build/` 派生目录）。
- `package`：先 `release`，再打成可安装的 `package/Squirrel.pkg`（依赖 Makefile 的 `package: release $(PACKAGE)`）。
- `archive`：先 `package`，再额外构建 Sparkle 的 `sign_update` 工具并打归档（`archive: package package/sign_update`），供正式发布与自动更新使用。

#### 4.4.3 源码精读

先看 `action-build.sh` 如何接收目标参数与注入环境变量：

- [action-build.sh:5](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/action-build.sh#L5) `target="${1:-release}"`——取第一个命令行参数，没传就默认 `release`。这就是为什么工作流传 `package` 或 `archive` 能改变行为。
- [action-build.sh:7-8](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/action-build.sh#L7-L8) `export ARCHS='arm64 x86_64'` 与 `BUILD_UNIVERSAL=1`，要求 xcodebuild 同时编译 Apple Silicon 与 Intel 两种架构，产出能在任意 Mac 原生运行的 universal 二进制。这两个变量最终被 Makefile 的 `BUILD_SETTINGS` 累加进 `xcodebuild` 命令行（见 u1-l3）。

  > 名词解释——**`export`**：bash 命令，把变量标记为「环境变量」，使其能被子进程继承。这里 `export` 后，`make` 及其调用的 `xcodebuild` 都能读到 `ARCHS`。

- [action-build.sh:10-13](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/action-build.sh#L10-L13) `SQUIRREL_BUNDLED_RECIPES` 列出要内置的 Rime 配方：`lotem/rime-octagram-data`（简体八股文统计模型）与其 `@hant`（繁体）变体。这个变量会被 `action-install.sh` 透传给 `plum/rime-install`，决定把哪些方案词库打包进 App。

依赖下载与主构建：

- [action-build.sh:16](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/action-build.sh#L16) `./action-install.sh`——在 `make` 之前先把 librime 动态库、OpenCC 字典、Sparkle 框架、plum 方案数据准备好。注意第 18-19 行注释掉了 `# make deps`——CI 故意**不**走「从源码编译依赖」的本地路径，而是用 `action-install.sh` 下载预编译产物，省时。
- [action-build.sh:22](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/action-build.sh#L22) `make "${target}"`——真正执行构建。`${target}` 被双引号包裹是为了防止空值展开成 `make ""`，是 shell 的稳健写法。

`action-install.sh` 锁定的依赖版本（保证可复现）：

- [action-install.sh:5-7](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/action-install.sh#L5-L7) 把 librime 版本钉在 `1.17.0`（git hash `33e7814`）、Sparkle 钉在 `2.6.2`，从各自的 GitHub Releases 下载 macOS universal 预编译包。版本固定让「今天构建」和「半年后构建」拿到同样的依赖。

最后看 `Makefile` 里 `target` 三个取值对应的真实目标，验证 `action-build.sh` 透传的语义：

- [Makefile:102-105](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L102-L105) `release` 目标：建派生目录、`bash package/add_data_files` 塞资源、`xcodebuild ... -configuration Release ... build`。
- [Makefile:153-156](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L153-L156) `package: release $(PACKAGE)` 打 pkg；`archive: package package/sign_update` 再加 `make_archive` 与签名工具。这解释了为什么 `release-ci.yml` 用 `archive`、另两条用 `package`。

#### 4.4.4 代码实践

**实践目标**：追踪 `action-build.sh` 的一次执行，看清「参数 → 环境变量 → make 目标 → 产物」的传递链。

**操作步骤**：

1. 假设 CI 执行 `./action-build.sh package`，确认 `target` 变量被赋成什么值（看 `action-build.sh:5`）。
2. 列出该脚本 `export` 的三个环境变量（`ARCHS`、`BUILD_UNIVERSAL`、`SQUIRREL_BUNDLED_RECIPES`），说明各自被谁消费。
3. 在 `Makefile` 里找到 `package` 目标（第 153 行），写出它依赖的两个前置目标。
4. 对照 `action-install.sh:5-7`，记录 CI 锁定的 librime 与 Sparkle 版本号。

**需要观察的现象**：`action-build.sh` 自身不编译任何东西，它只是「设环境变量 + 调 install + 调 make」三步；真正的编译逻辑全在 `Makefile` 与 `action-install.sh` 里。

**预期结果**：

- `target="package"` → `make package` → 触发 `package: release $(PACKAGE)` → 先跑 `release`（编译 `Squirrel.app`），再跑 `$(PACKAGE)`（用 `package/make_package` 打成 `Squirrel.pkg`）。
- 三个环境变量的消费者：`ARCHS` / `BUILD_UNIVERSAL` 被 `Makefile` 的 `BUILD_SETTINGS` 累加给 `xcodebuild`；`SQUIRREL_BUNDLED_RECIPES` 被 `action-install.sh` 透传给 `plum/rime-install`。
- CI 锁定：librime `1.17.0`（hash `33e7814`）、Sparkle `2.6.2`。

**待本地验证**：上述依赖关系可在 `Makefile` 中静态核对；实际运行需 macOS 环境与网络下载依赖。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `action-build.sh` 要把 `ARCHS` 和 `BUILD_UNIVERSAL` 设成全局 `export`，而不是写死在 `Makefile` 里？

**参考答案**：因为这两个变量是「CI 特有偏好」——CI 必须产出能在所有 Mac 上跑的 universal 包分发给用户；而本地开发者调试时可能只想编译当前机器的一种架构以加快速度。把偏好放在调用方（`action-build.sh`）而非 `Makefile`，让同一份 `Makefile` 既能服务 CI（传 universal）也能服务本地（不传，按默认），实现了「构建脚本与构建偏好解耦」。

**练习 2**：`action-build.sh` 第 19 行 `# make deps` 被注释掉了，CI 改用什么方式准备依赖？为什么？

**参考答案**：改用第 16 行的 `./action-install.sh` 直接下载 librime / Sparkle / OpenCC 的预编译产物。原因是从源码编译 librime（含 Boost 依赖）和 Sparkle 非常耗时，会拖慢每次 CI；而官方已发布 universal 预编译包，下载即用，把 CI 时间从「几十分钟」压到「几分钟」。

**练习 3**：`target="${1:-release}"` 里的 `:-release` 起什么作用？

**参考答案**：这是 bash 的「默认值」语法——当 `$1`（第一个参数）为空或未设时，`target` 取默认值 `release`。这样直接跑 `./action-build.sh`（不带参数）也能安全地构建 release，不会因为参数缺失而把空串传给 `make`。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「**从改一个本地化键到 CI 自动验证**」的完整模拟：

**任务背景**：假设你要新增一个本地化键 `deploy_skipped`（部署被跳过时的提示），并确保它被代码正确引用、不破坏 lint、能通过 CI。

**操作步骤**：

1. **改 xcstrings**：在 `resources/Localizable.xcstrings` 的 `strings` 字典里，仿照 `deploy_success`（[Localizable.xcstrings:99-120](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Localizable.xcstrings#L99-L120)）新增 `deploy_skipped` 键，提供 `en` / `zh-Hans` / `zh-Hant` 三种译文（如简体「无需部署。」）。注意 JSON 逗号与缩进格式。

2. **加代码引用**：在 `sources/SquirrelApplicationDelegate.swift` 的 `notificationHandler`（[SquirrelApplicationDelegate.swift:272-283](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L272-L283)）的 `switch messageValue` 里新增一个 `case "skipped":` 分支，调用 `NSLocalizedString("deploy_skipped", comment: "")`。

3. **判断是否需要抑制注释**：新增一个 `case` 后，`notificationHandler` 的圈复杂度上升。回顾该函数上方已有一行 [SquirrelApplicationDelegate.swift:265](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L265) 的 `// swiftlint:disable:next cyclomatic_complexity`——它已经覆盖整个函数声明，所以无需再加注释；但如果复杂度将来超过 error 阈值，需要重新评估。

4. **本地预检**：若装了 swiftlint，跑 `swiftlint` 确认无新告警；用 `git describe --always` 模拟 CI 里的产物命名。

5. **推送到分支**：`commit-ci.yml` 会自动触发——按 [commit-ci.yml:20-31](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.github/workflows/commit-ci.yml#L20-L31) 的顺序跑 lint、`action-build.sh package`、periphery。观察 PR 上的 github-actions-logging 批注是否报告你的改动。

**预期结果**：新增的键被 `NSLocalizedString` 正确引用，lint 因已有的 `cyclomatic_complexity` 抑制注释而不报新警告，CI 产出带新提示文案的 `Squirrel.pkg`。

**待本地验证**：完整流程需 macOS 构建环境与 GitHub Actions 运行；JSON 改动与代码引用关系可静态核对。

> 这个综合练习把四件事连了起来：xcstrings 键的写法（4.1）、lint 抑制注释的判断（4.2）、CI 触发与产物（4.3）、`action-build.sh` 在其中的位置（4.4）。它也是真实贡献者给 Squirrel 加功能时的典型流程。

---

## 6. 本讲小结

- Squirrel 用两个 `.xcstrings` 文件做本地化：`Localizable.xcstrings` 管代码经 `NSLocalizedString` 取的文案，`InfoPlist.xcstrings` 管 `Info.plist` 里被系统自动替换的字段；源语言是 `en`，提供 `zh-Hans` 与 `zh-Hant` 译文。
- `deploy_success` / `deploy_failure` 等「程序化键」必须为包括英文在内的所有语言提供译文；而 `"Deploy"` 这类「键即英文」的菜单项则无需重复写 `en`。
- `.swiftlint.yml` 用 `included: sources` 限定只扫自家代码、`disabled_rules` 关掉 `force_cast` 等桥接必备规则、用阈值规则（`line_length`/`function_body_length`/`cyclomatic_complexity`）约束代码规模；`reporter: github-actions-logging` 让违规变成 PR 行内批注。
- 抑制注释有三类：`:next` 豁免下一行（如给大 `switch` 函数豁免圈复杂度）、`disable`/`enable` 成对豁免一段（如 C 风格 `select_keys` 变量名）、以及针对单条规则的运算符空白豁免（如自定义 `?=`）。
- 三条 GitHub Actions 工作流共享「固定 Xcode → checkout 子模块 → swiftlint → action-build.sh → periphery scan」骨架，差异在触发器（push / PR / tag+master）与出口（artifact 保留期 / 草稿 Release / nightly）。
- `action-build.sh` 是 CI 构建总入口，靠 `target="${1:-release}"` 接收目标、`export ARCHS BUILD_UNIVERSAL` 注入 universal 偏好、调 `action-install.sh` 下载预编译依赖（librime 1.17.0、Sparkle 2.6.2）、最后 `make ${target}` 落到 `Makefile` 的 `release`/`package`/`archive` 目标。

---

## 7. 下一步学习建议

本讲是第五单元的收尾，也是整套学习手册的最后一篇。建议你：

1. **回头串读打包安装链路**：本讲的 CI 产物（`.pkg`）正是 u5-l5（打包、安装与 Sparkle 更新）里 `postinstall` 注册输入源的对象。重读 u5-l5 的「`pkgbuild`/`productbuild` → 签名公证 → `postinstall` 五步」，把 CI 产物与安装时序对接起来。
2. **动手改一个本地化键**：挑一个 `Localizable.xcstrings` 里的菜单项，改它的简体译文，本地构建一次 Squirrel.app，亲眼看到菜单文字变化——这是验证你对 4.1 节理解最直接的方式。
3. **关注上游 librime 与 Sparkle 的版本演进**：本讲看到 CI 锁定 librime `1.17.0`、Sparkle `2.6.2`。追踪这两个项目的 Release Notes，能帮你理解 Squirrel 何时、为何升级依赖（对应 `action-install.sh` 的版本号修改）。
4. **通读 SKILL.md**：仓库根目录的 `SKILL.md`（24KB）是面向 AI 代理的项目指南，里面系统性总结了 Squirrel 的架构与约定，可作为整套手册的「索引页」长期参考。

至此，你已从「Squirrel 是什么」（u1）出发，走过输入主链路（u2）、配置与主题（u3）、候选面板 UI（u4），抵达系统集成与扩展（u5）。整条学习路线完成闭环。
