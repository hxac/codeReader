# marked text、commit 与 inline 策略

## 1. 本讲目标

本讲是「输入处理主链路」单元的收尾篇。在 [u2-l6](u2-l6-rime-update-dataflow.md) 中我们已经看到 `rimeUpdate()` 如何从引擎取回 commit、status、context 三段结果；本讲回答最后一个问题：**取回结果之后，前端到底把哪些文字「上屏」、哪些文字「悬浮显示」、用什么形式显示？**

具体目标：

1. 区分 Cocoa 文本协议的两条路径——`commit → insertText`（最终上屏）与 `show → setMarkedText`（临时预编辑）。
2. 理解 `inlinePreedit` / `inlineCandidate` 这两个布尔标志由「主题配置」与「librime 运行时选项」**联合判定**的布尔逻辑。
3. 弄清为什么在非 inline 模式下，要把 marked text 设为全角空格 `U+3000` 而不是半角空格。
4. 理解 `soft_cursor` 选项与 inline 策略的反向联动。

学完后，你应能看懂任意一段输入在 Squirrel 中的「显示走向」，并能据此调整 `squirrel.yaml` 改变候选词的呈现方式。

## 2. 前置知识

本讲假设你已经掌握以下概念（在 [u1-l5](u1-l5-imk-input-method-concepts.md) 与 [u2-l6](u2-l6-rime-update-dataflow.md) 建立）：

- **marked text（标记文本）**：调用 `client.setMarkedText(_:selectionRange:replacementRange:)` 写入宿主应用文本框的一段带下划线的临时文本，代表「正在编辑、尚未定稿」的预编辑串，用户可继续修改或撤销。
- **commit text（提交文本）**：调用 `client.insertText(_:replacementRange:)` 写入宿主应用的最终文字，立即生效、不可撤销，俗称「上屏」。
- **preedit**：输入法内部的预编辑串，混合了「已转换的汉字」与「尚未转换的原始编码」，例如 `已選某些字xiang zuo yi dong`。
- **client（`IMKTextInput`）**：目标应用文本框的代理对象，因生命周期不属于输入法而用 `weak` 弱引用持有（见 [u2-l3](u2-l3-input-controller-lifecycle.md)）。
- **librime 运行时选项（option）**：引擎侧的布尔开关，如 `no_inline`、`inline`、`ascii_mode`、`soft_cursor`，由 `rimeAPI.get_option` / `set_option` 读写，可被 `app_options` 或输入方案动态设置。
- **`?=` 运算符**：项目自定义的「可选赋值」运算符——仅当右侧非 `nil` 时才赋值，定义在 [BridgingFunctions.swift:L44-L56](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L44-L56)。

如果对上述术语还不熟悉，建议先复习 u1-l5 与 u2-l6。

## 3. 本讲源码地图

本讲只深入一个文件，但会横向引用配置与主题文件作为「数据来源」：

| 文件 | 作用 |
|---|---|
| `sources/SquirrelInputController.swift` | 唯一精读文件。包含 `commit(string:)`、`show(preedit:selRange:caretPos:)`、`rimeUpdate()` 中 inline 分支与全角空格占位符逻辑。 |
| `sources/SquirrelPanel.swift` | 提供 `panel.inlinePreedit` / `panel.inlineCandidate` 两个只读属性，转发自主题。 |
| `sources/SquirrelTheme.swift` | 主题加载，从 `style/inline_preedit` 与 `style/inline_candidate` 读取布尔值填入主题。 |
| `data/squirrel.yaml` | 前端配置：`style.inline_preedit`、`style.inline_candidate` 与 `app_options` 下的 `no_inline` / `inline`。 |
| `sources/BridgingFunctions.swift` | `?=` 运算符与 `NSRange.empty` 哨兵定义。 |

## 4. 核心概念与源码讲解

### 4.1 commit：把定稿文字上屏

#### 4.1.1 概念说明

当用户选定某个候选词、或 librime 内部完成了一次自动上屏（如标点、纯英文直接输出），引擎会产生一段 **commit text（定稿文字）**。这段文字已经确定无误，必须立即写入宿主应用文本框——这就是「上屏」。

上屏用的是 Cocoa 的 `insertText(_:replacementRange:)`：它把字符串当作用户的最终输入插入光标处，宿主应用会把它当作普通文本接受。上屏之后，预编辑串应当清空、候选面板应当隐藏。

#### 4.1.2 核心流程

Squirrel 中所有「上屏」都收敛到一个私有方法 `commit(string:)`，它做四件事：

1. 守卫 `client` 非空（弱引用可能已失效）。
2. 调 `client.insertText(_:replacementRange:)` 把字符串插入宿主。
3. 清空本地缓存的 `preedit`。
4. 隐藏候选面板（`hidePalettes()`）。

有两个调用方会触发它：

- **正常上屏路径**：`rimeConsumeCommittedText()` 用 `get_commit` 从引擎取定稿，再调 `commit(string:)`。这是 `rimeUpdate()` 三段式消费的第一段（见 u2-l6）。
- **安全网路径**：`commitComposition(_:)` 在会话被强制收尾时（如 `deactivateServer`、宿主要求立即提交），用 `get_input` 取原始编码原样上屏，再 `clear_composition`。它复用同一个 `commit(string:)`，保证两条路径的上屏行为一致。

#### 4.1.3 源码精读

`commit(string:)` 本体——四行干净利落，注意它清空 `preedit` 并隐藏面板，这会直接影响后续 `show()` 的去重判断（见 4.2）：

[sources/SquirrelInputController.swift:L551-L556](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L551-L556) — 把字符串 `insertText` 上屏，清空 `preedit` 缓存并隐藏面板。

正常上屏路径——`get_commit` 取到定稿后转交 `commit(string:)`，并配对调用 `free_commit` 释放 C 结构（不释放即内存泄漏，原理见 u2-l6）：

[sources/SquirrelInputController.swift:L426-L434](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L426-L434) — `rimeConsumeCommittedText`：`get_commit` 取定稿 → `commit` 上屏 → `free_commit` 释放。

安全网路径——`commitComposition` 用 `get_input` 取原始编码（注意是「原始码」而非「定稿」），原样上屏后再 `clear_composition`，确保用户敲到一半的输入不会丢失：

[sources/SquirrelInputController.swift:L221-L229](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L221-L229) — 强制收尾时把未定稿的原始编码也提交出去，避免「悬挂组合」。

> 关键点：`insertText` 的 `replacementRange` 传 `.empty`（即 `NSRange(location: NSNotFound, length: 0)`，定义见 [BridgingFunctions.swift:L58-L60](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L58-L60)），表示「在当前光标处插入」，不替换任何已有选区。

#### 4.1.4 代码实践

**实践目标**：确认两条上屏路径复用同一个 `commit(string:)`，并理解它们取值来源的差异。

**操作步骤**：

1. 打开 `sources/SquirrelInputController.swift`。
2. 用编辑器搜索 `commit(string:`，找到方法定义（约 L551）。
3. 再搜索所有 `commit(string:` 的**调用点**，应能看到两处：`rimeConsumeCommittedText`（L430）与 `commitComposition`（L225）。
4. 对比两处传入的字符串来源：一处来自 `commitText.text`（`get_commit`），一处来自 `input`（`get_input`）。

**需要观察的现象**：

- 两个调用点都把字符串交给同一个 `commit(string:)`，没有各自重复写 `insertText`。
- 正常路径取的是「定稿」（如选词后的「向左移动」），安全网路径取的是「原始码」（如未选词的 `xiang zuo yi dong`）。

**预期结果**：两条路径上屏动作完全一致（都 `insertText` + 清 preedit + 隐藏面板），差异仅在于「提交什么内容」。这种「收敛到单一出口」的设计保证了上屏行为不会出现分叉。

#### 4.1.5 小练习与答案

**练习 1**：如果 `commit(string:)` 里删掉 `preedit = ""` 这一行，会出现什么问题？

> 参考答案：`show()` 内部用 `self.preedit` 做缓存去重（见 4.2）。上屏后若不清空 `preedit`，下一次 `show(preedit: "")` 时 `self.preedit` 仍等于上一次的非空值，去重判断会误认为「内容没变」而提前 return，导致宿主文本框里残留的 marked text 无法被清除，出现「幽灵预编辑」。

**练习 2**：为什么 `commitComposition` 在 `deactivateServer` 里被调用是「避免悬挂组合」的关键？

> 参考答案：`deactivateServer` 表示输入法即将让出焦点（用户切走或光标离开）。此时若组合（composition）还没定稿，宿主应用可能已经不接收 marked text 了，留在引擎里的半截编码就成了「悬挂组合」。`commitComposition` 用 `get_input` 把原始编码强行上屏并 `clear_composition`，保证会话干净退出。详见 [u2-l3](u2-l3-input-controller-lifecycle.md)。

---

### 4.2 show：用 marked text 显示预编辑

#### 4.2.1 概念说明

当输入还在进行中（用户敲了几个字母但还没选词），引擎没有产生定稿文字，此时不能 `insertText`（会上屏错误内容）。前端转而调用 `client.setMarkedText(_:selectionRange:replacementRange:)`，把预编辑串作为 **marked text** 写入宿主文本框。

marked text 在视觉上通常带下划线，表示「这是临时输入，随时可能变」。它有两个关键参数：

- **selectionRange**：光标（插入点）在 marked text 中的位置，用零长度区间表示。
- **高亮样式**：用 `mark(forStyle:at:)` 查询系统，区分「已转换段」与「原始编码段」两种视觉风格。

Squirrel 把这部分封装在 `show(preedit:selRange:caretPos:)` 里。

#### 4.2.2 核心流程

`show` 的执行流程：

1. **守卫** `client` 非空。
2. **缓存去重**：若传入的 `preedit`、`caretPos`、`selRange` 三个值都与上次完全相同，直接 `return`，避免无谓的 `setMarkedText` 调用（`rimeUpdate` 每次按键都会触发，去重能大幅减少与宿主的 IPC 往返）。
3. **更新缓存**：把三个值写回实例属性。
4. **构造富文本**：`NSMutableAttributedString(string: preedit)`。
5. **分段上色**：以 `selRange.location` 为界，前半段（`[0, start)`）应用 `kTSMHiliteConvertedText`（已转换文本高亮），后半段（`[start, 末尾)`）应用 `kTSMHiliteSelectedRawText`（选中原始文本高亮）。
6. **写入宿主**：`client.setMarkedText(attrString, selectionRange: 零长度区间@caretPos, replacementRange: .empty)`。

以 inline 预编辑串 `已選某些字xiang zuo yi dong` 为例（`start` 落在「已選某些字」之后）：

```
[已選某些字]  [xiang zuo yi dong]
 ↑ 已转换段     ↑ 原始编码段（光标在此）
 kTSMHiliteConvertedText   kTSMHiliteSelectedRawText
```

#### 4.2.3 源码精读

`show` 方法本体——注意开头的三值去重判断，以及用 `mark(forStyle:at:)` 查询系统高亮字典后强制解包 `!`：

[sources/SquirrelInputController.swift:L558-L578](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L558-L578) — `show`：三值去重 → 构造富文本 → 分段上色 → `setMarkedText`。

两点说明：

- **去重的必要性**：`handle` 在每次 `keyDown`/`flagsChanged` 结尾都调 `rimeUpdate()`，而 `rimeUpdate` 几乎总会走到 `show`。若不去重，即使预编辑串没变也会反复 `setMarkedText`，触发宿主不必要的重绘与光标抖动。
- **两种高亮常量**：`kTSMHiliteConvertedText` 与 `kTSMHiliteSelectedRawText` 是 Apple Text Services Manager 的常量，`mark(forStyle:at:)` 是 `IMKInputController` 提供的基类方法，返回系统决定的属性字典（通常包含下划线、背景色等）。Squirrel 不自己硬编码样式，而是交给系统，从而在不同 macOS 版本上获得原生外观。

#### 4.2.4 代码实践

**实践目标**：理解 marked text 的「分段上色」边界由 `selRange.location` 决定。

**操作步骤**：

1. 阅读 [SquirrelInputController.swift:L558-L578](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L558-L578)。
2. 找到 `let start = selRange.location`（L568），它是分段边界。
3. 注意 `if start > 0` 才给前半段上色（L570）——若 `start == 0`，说明 preedit 从头到尾都是原始编码（还没有任何已转换段），此时只上后半段一种颜色。
4. 追踪 `selRange` 的来源：它是 `rimeUpdate` 里根据引擎 `ctx.composition.sel_start` 计算出来的（见 [u2-l6](u2-l6-rime-update-dataflow.md)）。

**需要观察的现象**：

- 当 `start == 0` 时，`if start > 0` 分支被跳过，整段 preedit 只用 `kTSMHiliteSelectedRawText` 一种样式。
- `selectionRange` 传的是 `NSRange(location: caretPos, length: 0)`——零长度，表示这是一个「插入点光标」而非选区。

**预期结果**：你能解释「为什么刚开始敲第一个字母时，整个 marked text 颜色一致，而输入一段后会出现两种颜色」——因为随着已转换段出现，`start` 从 0 变为正值，触发了前半段的上色。

#### 4.2.5 小练习与答案

**练习 1**：`show` 里的去重判断比较了 `preedit`、`caretPos`、`selRange` 三个值。为什么不能只比较 `preedit` 字符串？

> 参考答案：光标位置 `caretPos` 或选区 `selRange` 可能独立变化而字符串不变——例如用户按左右方向键移动光标，preedit 文本完全没变，但光标位置变了。若只比字符串，会误判为「没变化」而跳过 `setMarkedText`，导致宿主文本框里的光标不跟随移动。

**练习 2**：`mark(forStyle:at:)` 返回的字典最后用了 `! as! [NSAttributedString.Key: Any]` 强制解包与转换。这里为什么 Squirrel 可以放心地强制解包？

> 参考答案：这两个 style 常量（`kTSMHiliteConvertedText` / `kTSMHiliteSelectedRawText`）是 IMK 框架保证支持的标准高亮风格，`mark(forStyle:at:)` 对这些标准 style 总会返回非空字典。这是一种「依赖框架契约」的有意取舍——若框架返回 nil，属于环境异常，让程序崩溃比静默显示错误样式更易定位问题。

---

### 4.3 inline 策略的联合判定

#### 4.3.1 概念说明

`show`（marked text，嵌在宿主文本框里）和 `showPanel`（独立浮动面板，由 `SquirrelPanel` 自绘）是两套**互为补充**的显示渠道。到底把预编辑串放哪、把候选词放哪，由两个布尔标志控制：

- **`inlinePreedit`**：是否把 preedit 串作为 marked text 嵌入宿主文本框（inline）。
- **`inlineCandidate`**：是否把当前高亮候选词也「预演」嵌入宿主文本框。

这两个标志的取值**不是单一来源**，而是由「主题配置」与「librime 运行时选项」**联合判定**：

- **主题配置方**（静态、由 `squirrel.yaml` 决定）：`panel.inlinePreedit`、`panel.inlineCandidate`，来自 `style/inline_preedit`、`style/inline_candidate`，可能被配色方案覆盖。
- **librime 运行时选项方**（动态、由 `app_options` 或方案决定）：`no_inline`（否决）、`inline`（强制开启）。

之所以要两方联合，是因为「全局偏好」（用户在主题里设的默认显示方式）与「上下文约束」（某个应用不适合 inline，或某个应用必须 inline）需要同时表达。例如终端类应用（iTerm2、Terminal）渲染 marked text 会有回显问题，需要 `no_inline` 否决；而 Chrome/Edge 有 bug 需要 `inline` 强制开启。

#### 4.3.2 核心流程

这两个标志**只在 schema 切换时计算一次**（在 `rimeUpdate` 的 status 段，检测到 `schema_id` 变化时），而非每次按键都算。计算发生在 schema 重载样式之后：

1. `get_status` 检测到 `schema_id` 变化。
2. 调 `loadSettings(for: schemaId)` 重载该方案的样式（刷新 `panel.inlinePreedit` 等）。
3. 用主题值与运行时选项计算 `inlinePreedit` / `inlineCandidate`。
4. 反向同步 `soft_cursor` 选项给引擎（见 4.4）。

布尔逻辑（记 \(P\) = `panel.inlinePreedit`，\(C\) = `panel.inlineCandidate`，\(N\) = `no_inline`，\(I\) = `inline`）：

\[
\text{inlinePreedit} = (P \land \lnot N) \lor I
\]

\[
\text{inlineCandidate} = C \land \lnot N
\]

真值表（重点看 `inlinePreedit`）：

| \(P\) | \(N\) | \(I\) | inlinePreedit | 说明 |
|:---:|:---:|:---:|:---:|---|
| true | false | false | **true** | 主题开启，无否决 → inline |
| true | true | false | false | `no_inline` 否决 |
| false | false | false | false | 主题未开启 |
| false | false | true | **true** | `inline` 单方面强制开启 |
| true | true | true | **true** | `inline` 强制力压过 `no_inline` |

**关键不对称**：`inline`（\(I\)）只能**强制开启 inlinePreedit**，对 inlineCandidate **无效**；而 `no_inline`（\(N\)）能**同时否决两者**。这是一个有意的设计：`inline` 是为绕过特定应用 bug（如 Chrome issue #435）而设的「最小强制」，只敢动 preedit，不敢贸然把候选词也塞进可能有 bug 的宿主。

#### 4.3.3 源码精读

联合判定的核心两行——注意 `inlineCandidate` 表达式里**没有** `|| inline`：

[sources/SquirrelInputController.swift:L450-L452](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L450-L452) — schema 切换时计算 `inlinePreedit` / `inlineCandidate`，并反向设置 `soft_cursor`。

主题配置方——`panel.inlinePreedit` / `panel.inlineCandidate` 只是转发自主题的只读属性：

[sources/SquirrelPanel.swift:L60-L65](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L60-L65) — 面板把 inline 标志转发给 `view.currentTheme`。

主题加载方——从 `style/inline_preedit` / `style/inline_candidate` 读取（用 `?=` 仅在配置存在时覆盖默认值）：

[sources/SquirrelTheme.swift:L200-L201](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L200-L201) — 主题加载时读取 inline 配置项。

配置默认值——`data/squirrel.yaml` 里 `inline_preedit: true`、`inline_candidate: false`，即「默认把编码嵌在文本框，但不把候选词嵌进去」：

[data/squirrel.yaml:L36-L38](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L36-L38) — 默认 inline 策略。

运行时选项方——`app_options` 里终端类应用设 `no_inline: true`（否决），Chrome/Edge 设 `inline: true`（强制）：

[data/squirrel.yaml:L400-L405](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L400-L405) — Terminal/iTerm2 用 `no_inline` 关闭 inline。

[data/squirrel.yaml:L429-L434](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L429-L434) — Chrome/Edge 用 `inline` 强制开启（绕过各自 bug）。

> 注意 `app_options` 里的 `no_inline` / `inline` 并不是 Squirrel 前端直接读的，而是由 `updateAppOptions()` 通过 `rimeAPI.set_option(session, key, value)` 下发到**引擎运行时选项**（见 [SquirrelInputController.swift:L366-L381](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L366-L381)），前端再经 `rimeAPI.get_option(session, ...)` 读回来。所以「librime 运行时选项」这一方，本质就是 yaml 里 `app_options` 写下去又读上来的值。

#### 4.3.4 代码实践

**实践目标**：用真值表推演真实配置场景下的 `inlinePreedit` / `inlineCandidate` 取值。

**操作步骤**：

1. 假设当前主题 `style.inline_preedit: true`、`style.inline_candidate: false`（即 `data/squirrel.yaml` 的默认值），故 \(P=\text{true}\)、\(C=\text{false}\)。
2. 用户在 **iTerm2** 里输入（`app_options` 设了 `no_inline: true`，未设 `inline`），故 \(N=\text{true}\)、\(I=\text{false}\)。
3. 代入公式：
   - `inlinePreedit = (true && !true) || false = false || false = false`
   - `inlineCandidate = false && !true = false`
4. 改为在 **Chrome** 里输入（`app_options` 设了 `inline: true`，未设 `no_inline`），故 \(N=\text{false}\)、\(I=\text{true}\)。
5. 再代入：
   - `inlinePreedit = (true && !false) || true = true || true = true`
   - `inlineCandidate = false && !false = false`（注意：`inline` 没有影响到它）

**需要观察的现象**：

- iTerm2：两个 inline 标志都是 false → preedit 与候选都走浮动面板，不嵌入文本框（这正是终端类应用需要的）。
- Chrome：`inlinePreedit` 被 `inline` 强制为 true，但 `inlineCandidate` 仍为 false → 只有编码嵌入文本框，候选词仍走面板。

**预期结果**：你验证了「`inline` 单方面强制开启 inlinePreedit，但对 inlineCandidate 无效；`no_inline` 同时否决两者」这一不对称性。

> 待本地验证：若你有 Mac 环境，可在 iTerm2 与 Chrome 中实际敲入中文，观察前者编码出现在浮动面板、后者编码嵌在地址栏文本框内。

#### 4.3.5 小练习与答案

**练习 1**：假设某应用同时设了 `no_inline: true` 和 `inline: true`，主题 `inline_preedit: true`。最终 `inlinePreedit` 是 true 还是 false？这合理吗？

> 参考答案：`inlinePreedit = (true && !true) || true = false || true = true`。即 `inline` 压过了 `no_inline`。合理——`no_inline` 是「建议否决」，`inline` 是「为绕 bug 必须开启」，后者优先级更高（见 Chrome 的 `inline: true` 就是为绕过 issue #435 而设的硬性要求）。但注意 `inlineCandidate = panel.inlineCandidate && !no_inline` 仍会被 `no_inline` 否决，`inline` 救不了它。

**练习 2**：为什么 `inlinePreedit` / `inlineCandidate` 只在 schema 切换时计算，而不是每次按键都算？

> 参考答案：因为它们的输入（主题配置 `panel.inlinePreedit` 与运行时选项 `no_inline`/`inline`）在一次 schema 会话内是稳定的——主题在 schema 切换时重载，`app_options` 在切应用时下发。每次按键都重算既无必要（结果不会变），也浪费 `get_option` 调用。这是「按需计算、缓存复用」的典型取舍。

---

### 4.4 全角空格占位符与 soft_cursor

#### 4.4.1 概念说明

当 `inlinePreedit == false`（非 inline 预编辑模式），前端**不应该**把真实编码塞进宿主文本框——编码只显示在浮动面板里。但这里有个微妙问题：**完全不调用 `setMarkedText` 也不行**。

原因在于 macOS 文本输入协议：宿主应用需要知道「现在有输入法组合正在进行」，以便正确处理光标、阻止某些快捷键、维持输入焦点。这个「组合进行中」的状态，是通过存在一段 marked text 来传达的。若完全没有任何 marked text，许多宿主（尤其是终端）会以为输入结束，导致回显错乱或组合被打断。

Squirrel 的折中方案：**在非 inline 模式下，仍调用 `setMarkedText`，但传入一个「不可见的占位符」**。而这个占位符必须是**全角空格 `U+3000`（`　`）**，不能是半角空格 `U+0020`。

为什么？源码注释给了两条理由：

1. **防止 iTerm2 回显原始编码**：某些终端会把 marked text 的内容回显到屏幕，半角空格占位符会触发异常回显路径。
2. **稳定中文基线**：半角空格会让中文组合的基线（baseline）不稳定、文字上下跳动；全角空格与中文字符等宽，基线平稳。

此外还有一个**反向联动**：`soft_cursor` 选项。`soft_cursor` 是告诉引擎「前端没有 inline 光标，请用软件方式（即在浮动面板里）显示光标」。它的值正好是 `!inlinePreedit`——非 inline 时开启 soft_cursor。

#### 4.4.2 核心流程

在 `rimeUpdate` 的 context 段，根据 inline 策略分三路输出 marked text：

1. **`inlineCandidate == true`**：把高亮候选词预演（`commit_text_preview`）作为 marked text 嵌入。
2. **`inlineCandidate == false` 且 `inlinePreedit == true`**：把真实 preedit 串作为 marked text 嵌入。
3. **两者皆 false（非 inline 模式）**：传一个全角空格 `U+3000` 作为占位 marked text（preedit 为空时传空串）。

同时，在第 3 种模式下，`soft_cursor` 选项为 true（在 schema 切换时由 `set_option(session, "soft_cursor", !inlinePreedit)` 设置），让引擎知道要在面板里画软光标。

#### 4.4.3 源码精读

非 inline 模式的全角空格占位符——注意字符串字面量 `"　"` 是 `U+3000`，且 `preedit.isEmpty` 时传空串（避免给空输入也塞占位符）：

[sources/SquirrelInputController.swift:L509-L517](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L509-L517) — 非 inline 模式三路分支：inlineCandidate / inlinePreedit / 全角空格占位符。

具体占位行（含原注释，说明了两条理由）：

[sources/SquirrelInputController.swift:L513-L515](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L513-L515) — 用全角空格 `U+3000` 作占位符，注释解释：防 iTerm2 回显 + 稳定中文基线。

`soft_cursor` 的反向同步——值正好是 `!inlinePreedit`，在 schema 切换时写回引擎：

[sources/SquirrelInputController.swift:L452](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L452) — `set_option("soft_cursor", !inlinePreedit)`：非 inline 时让引擎画软光标。

> 联动关系小结：`inlinePreedit == false` ⇒（前端）传全角空格占位 marked text ＋（后端）`soft_cursor = true`。前端「不显示真实编码」与后端「改在面板画光标」是一体两面。

#### 4.4.4 代码实践

**实践目标**：理解为什么占位符必须是全角 `U+3000` 而非半角 `U+0020`。

**操作步骤**：

1. 打开 [SquirrelInputController.swift:L513-L515](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L513-L515)，读注释。
2. 注意字面量 `"　"`——它看起来像一个空格，但实际是全角空格 `U+3000`（可用光标在该字符上停留，编辑器会显示它占两个半角宽度）。
3. 思考：为什么注释说「half-width placeholders make the Chinese composition baseline unstable」（半角占位符会让中文组合基线不稳定）？
4. 对比两个 Unicode 码点的宽度属性：
   - `U+0020`（半角空格）：宽度 0.5em，与西文字符等宽。
   - `U+3000`（全角空格 / 表意文字空格）：宽度 1em，与中文字符等宽。

**需要观察的现象**：

- 在等宽字体下，全角空格与一个中文字符占同样的宽度；半角空格只有一半。
- 当 marked text 是全角空格时，宿主文本框为「组合」预留的视觉空间与一个中文字符一致，光标基线平稳。

**预期结果**：你能解释「为什么不能用半角空格」——半角空格会让宿主为组合预留的高度/宽度按西文行高计算，导致后续真正上屏中文时基线上跳下窜；全角空格强制按中文行高预留，避免抖动。这是 Squirrel 针对中文输入场景的一个细致工程权衡。

> 待本地验证：若在 macOS 上把 `"　"` 改成 `" "`（半角）重新编译，在终端类应用（非 inline 模式）里输入中文，可观察到光标行高度抖动或 iTerm2 回显异常。

#### 4.4.5 小练习与答案

**练习 1**：为什么非 inline 模式不直接「不调用 `setMarkedText`」，而要费劲传一个占位符？

> 参考答案：macOS 文本协议依赖「存在 marked text」来维持「输入法组合进行中」的状态。完全不调 `setMarkedText`，宿主会以为输入已结束，导致组合被打断、光标错位、或某些终端把后续按键当作普通字符回显。占位符的作用是「占住组合状态但不显示真实编码」。

**练习 2**：`soft_cursor` 选项的值为什么是 `!inlinePreedit` 而不是 `!inlineCandidate`？

> 参考答案：`soft_cursor`（软光标）解决的是「preedit 光标在哪里显示」的问题。当 `inlinePreedit == true`，preedit 嵌在宿主文本框里，光标由宿主原生渲染（硬光标），不需要软光标；当 `inlinePreedit == false`，preedit 只在浮动面板里显示，引擎就得自己用软件方式在面板里画光标（软光标）。而 `inlineCandidate` 控制的是「候选词是否预演嵌入」，与光标渲染渠道无关，所以不用它。

---

## 5. 综合实践

**任务**：把本讲的两个核心结论串起来——解释 `inlinePreedit` / `inlineCandidate` 的联合判定，并说明非 inline 模式下的全角空格占位符策略。

请按以下步骤完成一份「配置 → 行为」推演表：

1. **列出两方输入**：
   - 主题配置方：`panel.inlinePreedit`（来自 `style/inline_preedit`）、`panel.inlineCandidate`（来自 `style/inline_candidate`）。
   - librime 运行时选项方：`no_inline`、`inline`（来自 `app_options` 经 `set_option` 下发）。
2. **写出公式**：
   - `inlinePreedit = (panel.inlinePreedit && !no_inline) || inline`
   - `inlineCandidate = panel.inlineCandidate && !no_inline`
3. **推演四个场景**，填出 inlinePreedit / inlineCandidate / soft_cursor 三个值，以及「编码显示在哪」「候选词显示在哪」：

   | 场景 | 主题 P/C | no_inline | inline | inlinePreedit | inlineCandidate | soft_cursor | 编码显示 | 候选显示 |
   |---|---|---|---|---|---|---|---|---|
   | 默认（网页文本框） | true/false | false | false | ? | ? | ? | ? | ? |
   | iTerm2 | true/false | true | false | ? | ? | ? | ? | ? |
   | Chrome | true/false | false | true | ? | ? | ? | ? | ? |
   | 关闭 inline 的主题 | false/false | false | false | ? | ? | ? | ? | ? |

4. **回答占位符问题**：在上表「iTerm2」与「关闭 inline 的主题」两行（即 `inlinePreedit == false` 的场景）中，`show` 会被传入什么字符串？为什么用全角空格 `U+3000` 而非半角空格？

**参考答案（推演表）**：

| 场景 | inlinePreedit | inlineCandidate | soft_cursor | 编码显示 | 候选显示 |
|---|---|---|---|---|---|
| 默认（网页文本框） | true | false | false | 嵌入文本框（marked text） | 浮动面板 |
| iTerm2 | false | false | true | 浮动面板（marked text 为全角空格占位） | 浮动面板 |
| Chrome | true | false | false | 嵌入文本框 | 浮动面板 |
| 关闭 inline 的主题 | false | false | true | 浮动面板（全角空格占位） | 浮动面板 |

**占位符回答**：在 `inlinePreedit == false` 的两行里，`show` 被传入 `"　"`（全角空格 `U+3000`，preedit 为空时传 `""`）。用全角而非半角的理由：(1) 防止 iTerm2 等终端回显原始编码；(2) 全角空格与中文字符等宽，保证中文组合的基线平稳，不会因半角占位符导致行高抖动。同时 `soft_cursor` 被设为 true，让引擎在浮动面板里用软件方式绘制光标。

## 6. 本讲小结

- **commit 走 `insertText`，show 走 `setMarkedText`**：前者是最终上屏、不可撤销；后者是临时预编辑、可改可撤。两条路径在 Squirrel 中分别收敛到 `commit(string:)` 与 `show(preedit:selRange:caretPos:)`。
- **上屏有单一出口**：正常选词（`get_commit`）与强制收尾（`get_input`）两条路径都复用 `commit(string:)`，保证上屏行为一致。
- **show 做三值去重**：`preedit`、`caretPos`、`selRange` 都没变时直接 return，避免每次按键都无谓地 `setMarkedText`。
- **marked text 分段上色**：以 `selRange.location` 为界，前半段「已转换」用 `kTSMHiliteConvertedText`，后半段「原始编码」用 `kTSMHiliteSelectedRawText`。
- **inline 策略由两方联合判定**：`inlinePreedit = (panel.inlinePreedit && !no_inline) || inline`；`inlineCandidate = panel.inlineCandidate && !no_inline`。`no_inline` 能否决两者，`inline` 只能强制开启 inlinePreedit（不对称）。
- **非 inline 模式传全角空格占位符**：用 `U+3000` 而非半角，既防 iTerm2 回显，又稳定中文基线；同时反向把 `soft_cursor` 设为 true，让引擎在面板里画软光标。

## 7. 下一步学习建议

本讲完成了「输入处理主链路」单元——从按键到引擎、再从引擎到上屏/显示的完整闭环已经打通。接下来建议：

1. **横向进入配置与主题单元（u3）**：本讲的 `panel.inlinePreedit`、`panel.inlineCandidate` 来自主题，建议读 [u3-l1 SquirrelConfig](u3-l1-squirrel-config.md) 与 [u3-l3 SquirrelTheme 加载](u3-l3-theme-loading.md)，弄清 `style/inline_preedit` 是如何从 yaml 一路读到主题对象的。
2. **进入面板 UI 单元（u4）**：本讲多次提到「浮动面板」与 `showPanel`，但没讲面板内部如何自绘。建议读 [u4-l1 SquirrelPanel.update](u4-l1-panel-model-update.md)，看候选富文本是如何拼装的。
3. **深入 soft_cursor 的对端**：`soft_cursor` 是前端写给引擎的选项，引擎据此决定光标渲染。可结合 librime 文档理解「前端选项如何影响引擎行为」。
4. **回顾主链路**：若想再巩固整条链路，可重读 [u2-l4 键盘事件主循环](u2-l4-key-event-loop.md) → [u2-l6 rimeUpdate 数据流](u2-l6-rime-update-dataflow.md) → 本讲，这三篇构成了 Squirrel 输入处理的核心三角。
