# 保留属性：插件→前端协调

## 1. 本讲目标

本讲解决一个看似矛盾的需求：**librime 的转换逻辑可以由插件（plugin）扩展，而候选词最终是 Squirrel 这个前端画出来的。插件如何「指挥」前端，在不修改前端源码的前提下改变候选词的显示方式？**

Squirrel 的回答是：**保留属性（Reserved Property）**。它是一条约定好的「带下划线前缀」的消息通道，让 librime 插件在运行时向前端发送语义化的指令，例如「把第 0、2 条候选的注释染成强调色」「强制刷新一次面板」。

学完本讲，你应当能够：

- 说清楚 `ReservedPropertyKey` 枚举定义了哪三个保留键、各自的含义。
- 读懂 `ReservedPropertyValue.parse` 的两套输入语法（query-string 与历史逗号列表），并能解释为什么都要归一到 `value` 这个字段。
- 跟踪一次 `_comment_highlight=0,2` 从 librime 通知 → `notificationHandler` → 主线程 → `handleReservedProperty` → `SquirrelPanel.update` 的完整调用链。
- 解释为什么这条链路必须用 `Task.detached { @MainActor in ... }` 跳回主线程。
- 理解 `_refresh_ui` 与 `rimeUpdate(clearReservedComments:)` 的配合关系，以及保留高亮「转瞬即逝」的设计原因。

---

## 2. 前置知识

阅读本讲前，你最好已经掌握以下概念（它们在前置讲义中已建立，这里只做最简回顾）：

- **前端与引擎的分界点**：Squirrel（前端）通过 `rimeAPI.process_key` 把按键交给 librime（引擎），引擎处理后通过回调通知前端取结果（参见 u1-l1、u2-l6）。
- **librime 的通知机制**：引擎在 `setupRime` 阶段通过 `rimeAPI.set_notification_handler(...)` 注册一个 C 回调 `notificationHandler`，引擎在 deploy、option、schema、property 等事件发生时回调它（参见 u2-l2）。
- **`@convention(c)` 与 `Unmanaged` 桥接**：C 回调不能直接持有 Swift 对象，所以用 `Unmanaged.passUnretained(self).toOpaque()` 把 `AppDelegate` 裸指针塞进 `context_object`，回调里再 `fromOpaque(...).takeUnretainedValue()` 取回（参见 u2-l2、u5-l4）。
- **候选富文本的拼装**：`SquirrelPanel.update` 把 preedit 行和候选行缝成一个 `NSMutableAttributedString`，注释的颜色由 `commentAttrs` 字典决定，候选行的模板是 `candidate_format`（参见 u4-l1）。
- **Swift 的 typed throws**：`func f() throws(E) -> T` 表示函数只抛 `E` 类型的错误；`do { try f() } catch { ... }` 配套使用。本讲的 `parse`/`indices` 用了这套语法。

> 关于本讲几乎用不到数学，唯一值得一提的「集合」记号是：下文用 \(\{0, 2\}\) 表示一个**候选索引集合**，即「第 0 条和第 2 条候选」。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [sources/ReservedProperty.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/ReservedProperty.swift) | 定义保留属性的「协议」：键枚举 `ReservedPropertyKey`、值解析器 `ReservedPropertyValue`、错误类型 `ReservedPropertyError`。这是插件与前端之间的「契约文档」。 |
| [sources/SquirrelApplicationDelegate.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift) | 里面的 `notificationHandler` 是引擎通知的总入口，负责识别 `property` 类型消息、切分键值，并 `Task.detached` 跳主线程分发。 |
| [sources/SquirrelInputController.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift) | 里面的 `handleReservedProperty(key:value:for:)` 把解析出的值应用到会话状态 `specialCommentIndices`，并控制 `rimeUpdate` 是否清空它。 |
| [sources/SquirrelPanel.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift) | 里面的 `update` 在拼注释富文本时读取 `inputController.specialCommentIndices`，对命中索引的行换上强调/警告色。 |
| [sources/SquirrelTheme.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift) | 定义 `accentCommentTextColor`/`warningCommentTextColor` 两个语义色，分别来自 YAML 的 `accent_text_color` / `warning_text_color`。 |

---

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：先讲「键的词汇表」，再讲「值的解析」，再讲「前端如何应用」，最后讲「消息如何从引擎线程安全地送达前端」。

### 4.1 ReservedPropertyKey：插件与前端共享的词汇表

#### 4.1.1 概念说明

「保留属性」的本质是一份**命名约定**：librime 插件向前端发消息时，键名必须以 `_` 开头，表示这是一个「保留给前端解释」的属性，而不是给引擎自己用的普通配置。

Squirrel 在前端侧用一个 `String` 原始值的枚举把支持的键集中登记下来。这样做有两个好处：

1. **编译期穷举**：前端处理时用 `switch key`，新增键必须显式处理，避免漏掉分支。
2. **文档即代码**：枚举的三个 case 就是「前端目前认得哪些保留属性」的权威清单，插件作者照着写即可。

这套机制是在 PR #1143（`feat: ReservedProperty protocol for plugin→frontend coordination`）中引入的，动机见代码注释里引用的 `rime/squirrel#1124`：早期插件只能用历史遗留的「逗号列表」负载，新协议统一成 URL 风格的 query-string，同时向后兼容旧格式。

#### 4.1.2 核心流程

枚举本身极简，关键是理解三个键各自的语义：

```
_comment_highlight → 给指定候选索引的注释上「强调色」（accent）
_comment_warning   → 给指定候选索引的注释上「警告色」（warning）
_refresh_ui        → 不带索引，只要求前端「立刻刷新一次面板」
```

前两个键携带「候选索引集合」，改变的是**非高亮候选行的注释颜色**；第三个键不带数据，是一个纯粹的「重绘触发器」。这条区分非常重要，后面会反复用到。

#### 4.1.3 源码精读

枚举定义在文件开头，三个 case 各自绑定一个带下划线前缀的字符串原始值：

[ReservedProperty.swift:11-15](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/ReservedProperty.swift#L11-L15) 定义三个保留键，`commentHighlight`/`commentWarning`/`refreshUI` 分别映射到字符串 `_comment_highlight`/`_comment_warning`/`_refresh_ui`。

注意文件顶部这段注释，它把整个协议的设计意图一句话讲透了——值用 URL 风格 query-string，裸值则存进 `value` 字段以兼容历史逗号列表负载：

[ReservedProperty.swift:8-10](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/ReservedProperty.swift#L8-L10) 说明保留属性用于插件→前端协调，并预告了「query-string 优先、裸值兼容」的解析策略。

#### 4.1.4 代码实践

**实践目标**：确认枚举与字符串的映射，理解「键名前缀 `_`」是协议的硬性约定。

**操作步骤**：

1. 打开 `sources/ReservedProperty.swift`，对照三个 case 抄下它们的 raw value。
2. 打开 `sources/SquirrelApplicationDelegate.swift`，搜索 `messageValue.first == "_"`，确认前端只对**首字符是 `_`** 的消息走保留属性分支（见 4.4 节）。

**需要观察的现象**：枚举的三个 raw value 与 `notificationHandler` 里的下划线判定一致——任何不以 `_` 开头的 property 消息都不会进入保留属性通道。

**预期结果**：你能在脑中建立「`_` 前缀 = 保留给前端」这条规则。

**待本地验证**：无（纯静态阅读即可确认）。

#### 4.1.5 小练习与答案

**练习 1**：如果一个插件想新增一个 `_comment_info` 键来上第三种颜色，前端会发生什么？

**参考答案**：`ReservedPropertyKey(rawValue: "_comment_info")` 返回 `nil`（枚举里没有这个 case），`handleReservedProperty` 会 `throw .unknownInput(rawKey)`（见 4.3 节）。也就是说，**前端必须先在枚举里登记新键**，否则插件发的消息会被静默丢弃并打印一行错误日志。

**练习 2**：为什么键名要强制以 `_` 开头？

**参考答案**：librime 的 property 通道本来也可能承载普通配置项。`_` 前缀是一种「命名空间隔离」：它让 `notificationHandler` 能用 `messageValue.first == "_"` 一眼区分「这是给前端解释的保留属性」和「这是引擎/其它消费者关心的普通属性」，避免误抢消息。

---

### 4.2 ReservedPropertyValue：query-string 与历史逗号列表的统一解析

#### 4.2.1 概念说明

键定了，值怎么传？协议支持两种写法：

- **历史逗号列表**（bare value）：例如 `_comment_highlight=0,2`，等号右边是 `0,2`。
- **URL 风格 query-string**：例如 `_comment_highlight=value=0,2`，等号右边是 `value=0,2`，形如 `键=值` 的键值对。

注意：`notificationHandler` 在 4.4 节会把整条 `messageValue` 在**第一个** `=` 处切开，所以 `parse` 拿到的 `raw` 只是等号右边那一段——对上面两个例子分别是 `"0,2"` 和 `"value=0,2"`。

`ReservedPropertyValue` 的职责是把这两种异构输入**统一归一化成一个字段字典 `fields: [String: String]`**，并约定一个「默认字段名」`value`。这样下游的 `indices()` 访问器只需要读 `fields["value"]` 一处，无需关心输入是哪种语法。

#### 4.2.2 核心流程

`parse` 的判定树如下（伪代码）：

```
parse(raw):
  if raw 为空            → 抛 emptyInput
  if raw 不含 "="        → 兼容历史格式，整段当作一个值：fields = {"value": raw}
  else                   → 当作 query-string 解析：
    构造 "?raw" 喂给 URLComponents 取 queryItems
    把 [(name, value)] 折叠成字典（重名取最后一个）
    成功                 → fields = 该字典
    失败                 → 抛 unknownInput(raw)
```

这里有一个巧妙的设计：`URLComponents` 解析 query-string 要求字符串以 `?` 开头（否则会被当成整个 URL 而非查询串），所以代码用 `"?\(raw)"` 临时拼一个前缀。`indices()` 访问器则负责把 `fields["value"]` 里的逗号列表切成 `Set<Int>`：

```
indices():
  取 fields["value"]，缺失 → 抛 missingDefaultFields
  按 "," 切分，逐段：
    去空格，转 Int，要求 >= 0，否则抛 invalidIndex(该段)
  返回去重后的 Set<Int>
```

注意 `Set<Int>` 天然去重：`"0,0,2"` 与 `"0,2"` 解析结果相同，即 \(\{0, 2\}\)。

#### 4.2.3 源码精读

先看 `parse` 的三段判定——空检查、无等号的兼容快路、URLComponents 的正式解析：

[ReservedProperty.swift:24-36](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/ReservedProperty.swift#L24-L36) `parse` 函数：第 26-28 行是无 `=` 的兼容分支，直接把整段塞进 `defaultField`（即 `"value"`）；第 30-34 行用 `URLComponents(string: "?\(raw)")` 解析 query-string，`uniquingKeysWith: { _, new in new }` 表示重名键取**后者**覆盖前者。

再看把字段字典转成索引集合的 `indices()`：

[ReservedProperty.swift:38-50](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/ReservedProperty.swift#L38-L50) `indices()` 函数：第 39 行强制要求 `fields["value"]` 存在（query-string 若用了别的键名就会在这里抛 `missingDefaultFields`）；第 41-48 行按 `,` 切分、trim、转非负整数，非法段抛 `invalidIndex`。

最后是错误类型清单，它穷举了解析过程中所有可能的失败原因，前端可以据此给出有意义的日志：

[ReservedProperty.swift:53-58](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/ReservedProperty.swift#L53-L58) `ReservedPropertyError` 四个 case：空输入、无法识别的输入（带原文）、缺默认字段、非法索引（带原文）。

#### 4.2.4 代码实践

**实践目标**：亲手验证两种输入语法解析出同一个字段字典，建立「归一到 `value`」的直觉。

**操作步骤**：

1. 在 `sources/ReservedProperty.swift` 同目录下临时新建一个 `ReservedProperty.playground`（**示例代码**，不要提交，练习后删除）。
2. 把 `ReservedProperty.swift` 的内容拷进去（playground 不能直接引用本 target），然后分别打印：

```swift
// 示例代码：仅供练习，不属于项目源码
print(try ReservedPropertyValue.parse("0,2").fields)        // 历史：["value": "0,2"]
print(try ReservedPropertyValue.parse("value=0,2").fields)  // query：["value": "0,2"]
print(try ReservedPropertyValue.parse("value=0,2").indices()) // 两种输入都得到 {0, 2}
```

**需要观察的现象**：前两行的 `fields` 完全相同（都是 `["value": "0,2"]`），第三行的 `indices()` 输出 `Set([0, 2])`。

**预期结果**：你亲眼看到「bare value 与 `value=` query-string 等价」，理解为什么 `indices()` 只读 `fields["value"]` 就够了。

**待本地验证**：是。本实践需要本地 macOS + Xcode/playground 才能运行；若环境受限，可改为「纸面跟踪」——在 `parse` 的第 26、30 行分别代入两个输入，手写出 `fields` 的值。

#### 4.2.5 小练习与答案

**练习 1**：`parse("indices=0,2").indices()` 会得到什么？

**参考答案**：会**抛错** `missingDefaultFields`。因为字符串含 `=`，走 query-string 分支，解析出 `fields = ["indices": "0,2"]`，而 `indices()` 只认 `fields["value"]`，该键不存在故抛错。这正是为什么协议文档推荐用 `value=` 或裸值——`indices()` 这个访问器是专门为 `value` 字段设计的。

**练习 2**：`parse("0, 2 ,2").indices()` 结果是什么？

**参考答案**：得到 `Set([0, 2])`。`split(separator: ",")` 切出 `["0", " 2 ", "2"]`，每段 `trimmingCharacters(in: .whitespaces)` 后是 `"0"`/`"2"`/`"2"`，转 Int 后插入 `Set` 自动去重，所以是 \(\{0, 2\}\)。这说明输入对空格和重复值都是宽容的。

---

### 4.3 handleReservedProperty：把解析结果应用到会话状态

#### 4.3.1 概念说明

解析只是「读懂指令」，真正改变显示的是**应用**。前端把「当前会话的特殊注释索引」存在控制器的一个属性 `specialCommentIndices` 里：

```
specialCommentIndices: [ReservedPropertyKey: Set<Int>]
```

它是一个「键 → 索引集合」的字典，键只有 `.commentHighlight` / `.commentWarning` 两种（`.refreshUI` 不存索引）。`SquirrelPanel.update` 在画每一行候选时，会反查这个字典决定注释颜色（见 4.4 节后段）。

`handleReservedProperty(key:value:for:)` 是这条通道的「应用器」，它做三件事：

1. **会话校验**：只对「当前活跃会话」生效，防止过期通知污染。
2. **键分发**：`switch key` 三个分支，`.commentHighlight`/`.commentWarning` 写索引，`.refreshUI` 触发刷新。
3. **配合 `clearReservedComments`**：控制这些索引「活多久」。

#### 4.3.2 核心流程

```
handleReservedProperty(key, value, sessionId):
  guard 当前 session == sessionId 且 session 合法且 find_session 成立 → 否则 return
  key 不在枚举里 → throw unknownInput(key)
  parsed = parse(value)              // 可能抛 ReservedPropertyError
  switch key:
    .commentHighlight → specialCommentIndices[.commentHighlight] = parsed.indices()
    .commentWarning   → specialCommentIndices[.commentWarning]   = parsed.indices()
    .refreshUI        → rimeUpdate(clearReservedComments: false)   // 重绘但保留索引
```

关键设计点在 `.refreshUI` 这一行：它调用的 `rimeUpdate(clearReservedComments: false)`。看 `rimeUpdate` 的签名与默认值就能明白「保留高亮转瞬即逝」的设计：

```
func rimeUpdate(clearReservedComments: Bool = true)
  if clearReservedComments { specialCommentIndices = [:] }   // 默认清空
  ... 正常取 commit/status/context 并刷新面板 ...
```

也就是说：

- **普通的 `rimeUpdate()`**（每次按键、选词、翻页都会触发）默认 `clearReservedComments: true`，会**清空**特殊索引。
- 所以插件想让高亮「立刻显示出来」，必须**先设索引、再用 `_refresh_ui` 触发一次不清空的刷新**。
- 而下一次正常按键的 `rimeUpdate()` 又会把索引清掉，于是高亮**自然过期**——这避免了一条候选词永远挂着强调色。

这套「设值 + 刷新不清空 + 正常按键清空」的三段式，让插件驱动的注释着色成为一个**一次性的、随下次输入自动消失的**视觉提示，符合输入法的使用直觉。

#### 4.3.3 源码精读

先看存储属性与 `handleReservedProperty` 主体：

[SquirrelInputController.swift:281](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L281) 声明 `specialCommentIndices` 字典，它是「键 → 索引集合」的会话级状态。

[SquirrelInputController.swift:283-295](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L283-L295) `handleReservedProperty` 全文：第 284 行是三重会话守卫；第 285 行把字符串键转成枚举，失败即抛错；第 286 行解析值；第 287-294 行 `switch` 三个分支，前两分支写索引，第三分支调 `rimeUpdate(clearReservedComments: false)`。

再看 `rimeUpdate` 开头的清空逻辑，理解「默认即清空」：

[SquirrelInputController.swift:437-440](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L437-L440) `rimeUpdate(clearReservedComments: Bool = true)`：默认参数为 `true`，所以任何不带参数的 `rimeUpdate()` 调用（按键主循环末尾、选词、翻页、ASCII 切换等）都会先清空 `specialCommentIndices`。

最后看面板侧如何消费这些索引——这是「应用」的终点：

[SquirrelPanel.swift:235-248](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L235-L248) 在 `update` 拼注释富文本时：第 238 行先判断「有 inputController、且 specialCommentIndices 非空、且当前行**不是**高亮行」；第 240-241 行若该行索引在 `.commentHighlight` 集合里，注释色换成 `theme.accentCommentTextColor`；第 242-243 行若在 `.commentWarning` 集合里，换成 `theme.warningCommentTextColor`；都不命中则第 247 行用普通 `commentAttrs`。

注意第 238 行的 `i != index` 这个条件：**强调色/警告色只作用于非高亮候选行**。高亮行（即当前选中、即将上屏的那条）始终用 `commentHighlightedAttrs`，不受保留属性影响。这是有意的——保留属性是用来「提示其它候选」，不该干扰用户正要选中的那条。

这两个语义色来自主题，对应 YAML 键：

[SquirrelTheme.swift:249-250](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L249-L250) `accentCommentTextColor` 来自配色方案的 `accent_text_color`，`warningCommentTextColor` 来自 `warning_text_color`。注意它们**没有** `??` 默认值回退（对比同文件第 243-244 行的 `candidateTextColor ?? textColor`），所以若配色方案没配这两个键，对应颜色就是 `nil`，`SquirrelPanel` 在第 241/243 行给 `.foregroundColor` 赋 `nil` 时等同于「不改变颜色」——这是一个静默降级，不会崩溃。

#### 4.3.4 代码实践

**实践目标**：用 `git grep` 一次性看清「谁会清空 specialCommentIndices」「谁会消费它」，建立完整的数据生命周期视图。

**操作步骤**：

1. 在仓库根目录执行（**只读命令**，安全）：

```bash
git grep -n "specialCommentIndices" -- sources/
git grep -n "clearReservedComments" -- sources/
```

**需要观察的现象**：

- `specialCommentIndices` 的引用点应包含：声明（`SquirrelInputController.swift:281`）、两处写入（289、291 行）、一处清空（439 行）、面板侧两处读取（`SquirrelPanel.swift:238/240/242`）。
- `clearReservedComments` 应只有两处：定义默认值 `= true`（437 行）与传 `false` 调用（293 行）。

**预期结果**：你确认了「写入只有 `_comment_*`、清空只有 `rimeUpdate` 默认路径、`_refresh_ui` 是唯一保留不清空的入口」这条不变量。

**待本地验证**：无（`git grep` 输出即结论）。

#### 4.3.5 小练习与答案

**练习 1**：插件按顺序发送 `_comment_highlight=0,2`，然后用户按了一个普通字母键。第 0、2 行的注释还会是强调色吗？

**参考答案**：**不会**。普通字母键最终走主循环末尾的 `rimeUpdate()`（默认 `clearReservedComments: true`），第 439 行把 `specialCommentIndices` 清成 `[:]`，面板重画时第 238 行的 `!specialCommentIndices.isEmpty` 为假，于是所有注释回到普通色。这正是「保留高亮转瞬即逝」的体现。

**练习 2**：为什么 `handleReservedProperty` 第 284 行要写三个并列的 guard 条件（`session == sessionId`、`session != 0`、`rimeAPI.find_session(session)`）？

**参考答案**：`notificationHandler` 拿到的 `sessionId` 可能属于一个**已经销毁或不再活跃**的会话（比如通知在排队，而 controller 已经 `destroySession`）。第一个条件确保只处理「当前这个 controller 的会话」的通知；后两个是 librime session 的常规有效性校验（0 是无效哨兵，`find_session` 确认引擎侧仍认得它）。三者任一不满足就静默 `return`，避免对错误会话写入脏状态。

---

### 4.4 notificationHandler：从引擎线程到主线程的 property 消息路由

#### 4.4.1 概念说明

前面三节假设「指令已经到达 `handleReservedProperty`」，但 librime 的通知回调 `notificationHandler` 是一个 `@convention(c)` 的 C 函数指针，**它由 librime 在自己的线程上调用，不一定在主线程**。而 `handleReservedProperty` 会修改 `specialCommentIndices`、进而触发 `rimeUpdate` 刷新 AppKit 面板——这些都必须在**主线程**执行，否则会触发 UI 线程违规甚至崩溃。

所以 `notificationHandler` 里对 `property` 消息做了两件事：

1. **识别并切分**：判定 `messageType == "property"` 且值以 `_` 开头且含 `=`，切出 `key`/`value`。
2. **线程跳转**：用 `Task.detached { @MainActor in ... }` 把实际应用工作异步投递到主线程。

还要理解一条「寻址」细节：`notificationHandler` 只持有 `AppDelegate` 与 `sessionId`，并不直接持有「当前活跃的 `SquirrelInputController`」。它通过 `delegate.panel?.inputController` 找到**最近一次显示面板时登记的控制器**（`showPanel` 里 `panel.inputController = self` 设的），再把 `sessionId` 带进 `handleReservedProperty` 做二次校验。这是一条「先按面板找控制器、再用 sessionId 校验归属」的两段式寻址。

#### 4.4.2 核心流程

```
notificationHandler(contextObject, sessionId, messageTypeC, messageValueC):
  delegate = 从 contextObject 取回 AppDelegate
  messageType / messageValue = C 字符串转 Swift String

  if messageType == "deploy"   → 显示部署状态消息，return
  else if messageType == "option" → 处理 option（如 ascii_mode 改状态栏图标），return
  else if messageType == "property"
         且 messageValue 含 "="
         且 messageValue 首字符 == "_":
       在第一个 "=" 处切出 key、value
       Task.detached { @MainActor in
         try delegate.panel?.inputController?.handleReservedProperty(key:value:for: sessionId)
       }
       return
  // 其它（schema 等）→ 通知提示
```

切分用的「第一个 `=`」很关键：保留属性的值本身可能含 `=`（如 query-string `value=0,2`），所以必须用 `firstIndex(of: "=")` 只切第一刀，等号左边归 key、右边整体归 value。

#### 4.4.3 源码精读

先看通知回调的注册——这就是 `notificationHandler` 得以被引擎调用的起点（详见 u2-l2）：

[SquirrelApplicationDelegate.swift:145-148](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L145-L148) 把 Swift 函数 `notificationHandler` 包装成 `@convention(c)` 函数指针，用 `Unmanaged.passUnretained(self)` 把 `AppDelegate` 作为 `context_object` 透传，再 `rimeAPI.set_notification_handler` 注册。

再看 `notificationHandler` 的 `property` 分支——切分 + 主线程跳转的核心：

[SquirrelApplicationDelegate.swift:313-324](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L313-L324) `property` 分支：第 313-314 行三重条件（类型是 property、含 `=`、首字符 `_`）；第 315-316 行用 `firstIndex(of: "=")` 只切第一刀得到 key/value；第 317-323 行 `Task.detached { @MainActor in ... }` 把 `handleReservedProperty` 投递到主线程，并用 `do/catch` 把 `ReservedPropertyError` 打印成日志而不让异常逃逸。

关于 `Task.detached { @MainActor in ... }` 的含义：

- `Task.detached` 创建一个**脱离当前上下文**的新任务（不继承调用方的 actor、优先级等）。
- `{ @MainActor in ... }` 把这个任务的执行**绑定到主 actor**，即主线程。
- 因此无论 `notificationHandler` 在哪个线程被 librime 调用，`handleReservedProperty` 及其触发的 `rimeUpdate`/面板刷新都保证在主线程执行。

最后看「寻址」链路的一环——`showPanel` 如何把控制器登记到面板，让 `delegate.panel?.inputController` 能找到它：

[SquirrelInputController.swift:585-589](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L585-L589) `showPanel` 里 `panel.inputController = self` 把当前控制器登记进共享面板；`notificationHandler` 正是经 `delegate.panel?.inputController?` 取回它，再带 `sessionId` 进 `handleReservedProperty` 做二次校验（见 4.3 节第 284 行）。

> 把 4.3 与 4.4 合起来看，整条链路是：
>
> 引擎（任意线程）→ `notificationHandler` 切出 key/value → `Task.detached @MainActor` → `delegate.panel?.inputController?.handleReservedProperty(key:value:for: sessionId)` → 会话校验 + 写 `specialCommentIndices`（或 `_refresh_ui` 触发 `rimeUpdate(clearReservedComments: false)`）→ 下一次 `SquirrelPanel.update` 读 `specialCommentIndices` 给注释上色。
>
> 这就是「插件→前端协调」的完整闭环。

#### 4.4.4 代码实践

**实践目标**：跟踪一次 `_comment_highlight=0,2` 从引擎到面板上色的完整调用链，并在脑中画清楚「在哪一步跨了线程、在哪一步做了归属校验」。

**操作步骤**（源码阅读型实践，无需运行）：

1. 假想一个 librime 插件在处理某个候选时，调用引擎 API 发出一条通知，其 `messageType = "property"`、`messageValue = "_comment_highlight=0,2"`。
2. 按 下表逐格填出每一步的「所在文件:行」「所在线程」「数据形态」：

| 步骤 | 文件:行 | 线程 | key / value 形态 |
| --- | --- | --- | --- |
| ① 引擎回调 | SquirrelApplicationDelegate.swift:266 | librime 线程 | `("_comment_highlight", "0,2")` 未切分 |
| ② 切分 | SquirrelApplicationDelegate.swift:315-316 | librime 线程 | key=`"_comment_highlight"` value=`"0,2"` |
| ③ 跳主线程 | SquirrelApplicationDelegate.swift:317 | **切到主线程** | 同上 |
| ④ 会话校验 | SquirrelInputController.swift:284 | 主线程 | 通过则继续 |
| ⑤ 解析键 | SquirrelInputController.swift:285 | 主线程 | `ReservedPropertyKey.commentHighlight` |
| ⑥ 解析值 | ReservedProperty.swift:24-36 | 主线程 | `fields=["value":"0,2"]` |
| ⑦ 转索引 | ReservedProperty.swift:38-50 | 主线程 | `Set([0,2])` |
| ⑧ 写状态 | SquirrelInputController.swift:289 | 主线程 | `specialCommentIndices[.commentHighlight]={0,2}` |
| ⑨ 面板上色 | SquirrelPanel.swift:240-241 | 主线程 | 第 0、2 行注释用 `accentCommentTextColor` |

3. 补一个追问：若插件紧接着发了 `_refresh_ui`，链路在 ⑤ 处走 `switch` 的第三分支（293 行），触发 `rimeUpdate(clearReservedComments: false)`——这一次刷新**不会**清空 ⑧ 刚写的索引，于是 ⑨ 的强调色能立刻显示出来。

**需要观察的现象**：你应能清晰指出「跨线程只发生在 ②→③ 这一步」，其余全部在主线程。

**预期结果**：你能不看源码复述出这张 9 步表，并解释「为什么 `notificationHandler` 不能直接调 `handleReservedProperty`」（因为它可能在非主线程）。

**待本地验证**：无需运行；若想实测，需要本地带 librime 插件开发环境，难度较高，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果删掉 `Task.detached { @MainActor in ... }`，直接在 `notificationHandler` 里调 `handleReservedProperty`，会发生什么？

**参考答案**：`handleReservedProperty` 会改 `specialCommentIndices`，而 `SquirrelPanel.update`（主线程）也会读它——两个线程并发读写同一个字典，构成**数据竞争（data race）**；更糟的是 `.refreshUI` 分支会直接调 `rimeUpdate` 触发 AppKit 面板刷新，而 AppKit 要求**所有 UI 操作必须在主线程**，在后台线程刷新会触发断言或未定义行为。`Task.detached { @MainActor }` 同时解决了这两个问题。

**练习 2**：`messageValue.first == "_"` 这个判定去掉会怎样？

**参考答案**：去掉后，**所有**含 `=` 的 property 消息都会被当成保留属性去切分。但 `handleReservedProperty` 第 285 行 `ReservedPropertyKey(rawValue: key)` 对非 `_` 前缀的键会返回 `nil` 并抛 `unknownInput`——结果就是「不会崩溃，但每次普通 property 通知都会打一行错误日志」。所以 `_` 前缀判定既是协议约定，也是一种**前置过滤**，把显然不属于保留属性的消息挡在切分逻辑之外，避免噪声日志。

---

## 5. 综合实践

设计一个贯穿本讲的端到端理解任务：**为一个假想的 librime 拼音插件，手写一份「向前端发保留属性」的交互剧本，并在 Squirrel 源码中标注每一步的落点。**

**背景**：假设你写了一个 librime 插件，能在候选词的注释里标注「这个词来自哪个词库」。你希望：词库 A 的候选（索引 0、2）注释用强调色，词库 B 的候选（索引 1）注释用警告色，并且这些高亮在用户**按下下一个键时自动消失**。

**要求完成以下三件事**：

1. **写出通知序列**：按正确顺序列出插件应发送的两条 property 通知（`messageType`、`messageValue` 各是什么），并解释为什么必须先发 `_comment_*` 再发 `_refresh_ui`、为什么不需要手动发「清除」通知。

2. **写出 YAML 配色**：在 `data/squirrel.yaml` 的某个 `preset_color_schemes` 方案下，补上让强调色生效的两个键（提示：`accent_text_color` / `warning_text_color`），并说明若漏配会怎样（提示：见 4.3.3 节 SquirrelTheme 第 249-250 行无 `??` 回退、SquirrelPanel 第 241 行给 `.foregroundColor` 赋 `nil` 的行为）。

3. **画出线程时序**：画一条时间轴，标出「librime 线程」与「主线程」两条泳道，把 `_refresh_ui` 从 `notificationHandler`（第 313-324 行）经 `Task.detached @MainActor` 跨到 `handleReservedProperty`（第 292-293 行）再到 `rimeUpdate`（第 437 行）再到 `SquirrelPanel.update`（第 235-248 行）的过程标注清楚，特别标出「跨线程的那一跳」。

**参考要点**（先自己写再对照）：

- 通知序列：先 `property / _comment_highlight=0,2`，再 `property / _comment_warning=1`，最后 `property / _refresh_ui`（值随意，因为 `.refreshUI` 不读值，但协议要求 messageValue 至少含 `=` 且以 `_` 开头，故通常写成 `_refresh_ui=1` 之类）。必须最后发 `_refresh_ui`，因为它调的 `rimeUpdate(clearReservedComments: false)` 才是不清空索引的重绘；不需要手动清除，因为用户下次按键的普通 `rimeUpdate()` 默认就会清空。
- 配色：在某方案下加 `accent_text_color: 0x...` 与 `warning_text_color: 0x...`（Rime 字节序 `0xAABBGGRR`）。漏配则两色为 `nil`，赋给 `.foregroundColor` 等于不改变，注释维持普通色——静默降级，不报错。
- 时序：librime 线程上完成 `notificationHandler` 的类型判定与 `key/value` 切分（第 313-316 行）；**唯一跨线程的一跳**是第 317 行 `Task.detached { @MainActor in ... }`；此后 `handleReservedProperty` → `rimeUpdate` → `SquirrelPanel.update` 全在主线程泳道。

---

## 6. 本讲小结

- **保留属性是一份命名约定**：键以 `_` 开头、值用 query-string 或历史逗号列表，让 librime 插件在不改前端源码的前提下指挥前端显示（[ReservedProperty.swift:11-15](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/ReservedProperty.swift#L11-L15)）。
- **三种键**：`_comment_highlight`/`_comment_warning` 携带候选索引集合（给非高亮行的注释上强调/警告色），`_refresh_ui` 是纯重绘触发器。
- **解析统一归一到 `value` 字段**：`parse` 对无 `=` 的裸值与 `value=` 形式的 query-string 一视同仁，`indices()` 只读 `fields["value"]` 并宽容空格与重复值（[ReservedProperty.swift:24-50](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/ReservedProperty.swift#L24-L50)）。
- **应用集中在 `handleReservedProperty`**：写 `specialCommentIndices`，且强调/警告色只作用于非高亮候选行（[SquirrelInputController.swift:283-295](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L283-L295)、[SquirrelPanel.swift:235-248](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L235-L248)）。
- **「转瞬即逝」靠 `clearReservedComments` 设计**：普通 `rimeUpdate()` 默认清空索引，只有 `_refresh_ui` 调用的 `rimeUpdate(clearReservedComments: false)` 保留索引，使插件着色随下次按键自动消失（[SquirrelInputController.swift:437-440](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L437-L440)）。
- **线程安全靠 `Task.detached { @MainActor }`**：C 回调可能在非主线程，必须跳主线程后再动 AppKit 面板与会话状态（[SquirrelApplicationDelegate.swift:313-324](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L313-L324)）。

---

## 7. 下一步学习建议

- **横向对比另一条引擎→前端通道**：本讲讲的是 `property` 消息，建议回头精读 `notificationHandler` 里的 `option` 分支（[SquirrelApplicationDelegate.swift:284-312](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L284-L312)），对比「option 通道（驱动 ascii_mode、状态栏图标）」与「property 通道（驱动候选注释着色）」在分发方式上的异同——前者同步直接调 `delegate` 方法，后者必须经 `panel?.inputController?` 寻址。
- **深入 Swift/C 桥接约定**：本讲的 `notificationHandler` 是 `@convention(c)` + `Unmanaged` 的典型用例，建议接着学 u5-l4（Swift/C 桥接约定），系统了解 `rimeStructInit`、`setCString`、`?=` 等项目级约定。
- **回到面板自绘**：本讲到 `SquirrelPanel.update` 的注释上色为止，若想看这些 `NSColor` 最终如何被 Core Graphics 画出来，继续读 u4-l3（SquirrelView 自定义绘制）。
- **动手扩展（进阶）**：若你想新增一个保留属性（如 `_comment_info` 上第三种色），按本讲梳理的步骤依次修改：在 `ReservedPropertyKey` 加 case → 在 `SquirrelTheme` 加色字段 → 在 `SquirrelPanel.update` 加分支 → 在 `handleReservedProperty` 的 `switch` 加分支，体会「契约即枚举」带来的编译期保障。
