# rimeUpdate 数据流

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `rimeUpdate()` 为什么是「前端从引擎取回结果」的唯一出口，以及它内部的三段式消费顺序。
- 分别说出 `get_commit` / `get_status` / `get_context` 这三个 librime 调用各自返回什么信息。
- 解释 schema（输入方案）切换时，前端为什么要重新加载设置、并据此计算 `inlinePreedit` / `inlineCandidate` 策略。
- 理解为什么每次成功读取 C 结构后，都必须配对调用 `free_commit` / `free_status` / `free_context`，这背后的 C 内存所有权原理是什么。
- 读懂候选词、注释、标签、页码这些信息是如何从 `RimeContext` 里逐字段抠出来，再交给面板与 marked text 的。

本讲是「输入处理主链路」的关键一环：上一讲（u2-l5）讲的是「按键如何送进引擎」，本讲讲的是它的对偶——「引擎处理完的结果如何取回前端」。

## 2. 前置知识

在进入源码前，先建立几个直觉。

### 引擎是 C 库，结构体由引擎分配内存

librime 是一个 C/C++ 库，前端通过 `rime_get_api_stdbool()` 拿到一个函数表 `rimeAPI`，再调用上面的方法。这里有一个关键的内存约定：当我们调用 `get_commit` / `get_status` / `get_context` 时，引擎会把结果**填进我们传入的 C 结构体**，而这些结构体内部的字符串、数组指针，指向的是**引擎分配的堆内存**。前端用完之后，必须调用对应的 `free_*` 函数通知引擎回收，否则就会内存泄漏。这一点是本讲的核心，后面会反复出现。

### 一次按键处理的两段式结构

回顾 u2-l4：`handle(_:client:)` 里，按键先被翻译并送进引擎（`processKey` → `rimeAPI.process_key`），**紧接着**就调用 `rimeUpdate()` 把引擎的新状态取回来刷新界面。也就是说，「送键进引擎」和「从引擎取结果」永远是成对出现的。除了键盘事件，选词（`selectCandidate`）、翻页（`page`）、移动光标（`moveCaret`）、ASCII 模式切换、和弦输入超时等动作，结尾也都会调用 `rimeUpdate()`。它是**所有「状态可能已变」场景的统一收口**。

### librime 的三块状态

引擎针对一个会话（session）对外暴露三块主要状态：

| 状态 | 读取函数 | 含义 | 释放函数 |
|------|----------|------|----------|
| 已提交文本（commit） | `get_commit` | 引擎决定「现在该上屏」的最终文字 | `free_commit` |
| 会话状态（status） | `get_status` | 当前 schema id、是否 ASCII 模式、是否在组合中 | `free_status` |
| 上下文（context） | `get_context` | 当前 preedit（预编辑串）、候选列表、菜单、页码 | `free_context` |

`rimeUpdate()` 就是**依次**消费这三块状态。

### marked text vs commit text

回顾 u1-l5：Cocoa 文本协议里，`setMarkedText` 设置带下划线的临时预编辑文本（可改可撤），`insertText` 把最终文字上屏。本讲里 `commit(string:)` 走的是 `insertText`，`show(...)` 走的是 `setMarkedText`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [sources/SquirrelInputController.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift) | 输入控制器，本讲主角。`rimeUpdate`、`rimeConsumeCommittedText`、`commit`、`show`、`showPanel` 全在这里。 |
| [sources/BridgingFunctions.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift) | Swift/C 桥接工具。`rimeStructInit()`（清零并填 `data_size`）和 `setCString`（C 字符串所有权）定义在此，是理解「配对释放」的基础。 |
| [sources/SquirrelApplicationDelegate.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift) | 提供 `loadSettings(for:)`（schema 切换重载）与 `RimeStringSlice.asString`（按 `.length` 裁剪的字符串桥接），本讲 schema 重载与标签读取会用到。 |

## 4. 核心概念与源码讲解

### 4.1 三段式消费概览：rimeUpdate 的总体流程

#### 4.1.1 概念说明

`rimeUpdate()` 是一个**纯消费函数**：它不向引擎送任何东西，只把引擎当前的状态读出来、转成 Swift 数据、再驱动界面。它的名字里有个 "Update"，指的是「**更新前端 UI 以反映引擎最新状态**」，而不是「更新引擎」。

它有一个布尔参数 `clearReservedComments`（默认 `true`），与插件保留属性（见 u5-l3）相关——当 librime 只是请求「刷新一下界面」而不改变候选时，会传 `false` 以保留之前高亮的注释。本讲先聚焦主流程，保留属性细节留给后续讲义。

#### 4.1.2 核心流程

整个函数可以看作严格的三段，每一段都遵循「**声明结构 → 调 get_* → 用结果 → 调 free_***」的固定模式：

```text
rimeUpdate(clearReservedComments):
  0.（可选）清空保留注释标记
  1. rimeConsumeCommittedText()        —— get_commit：取并上屏「该提交的文字」
  2. 声明 RimeStatus → get_status      —— 取会话状态；若 schema 变了：
        loadSettings(for: schemaId)    ——   重载该 schema 的样式
        计算 inlinePreedit/inlineCandidate —— 并据此 set_option("soft_cursor", ...)
     → free_status
  3. 声明 RimeContext → get_context    —— 取 preedit / 候选 / 标签 / 页码
        根据 inline 策略调用 show(...) 或 showPanel(...)
        若 get_context 失败 → hidePalettes()
     → free_context
```

注意第 2、3 段里的 `free_status` / `free_context` **只在对应的 `get_*` 返回 true（成功）时才调用**——这正是因为内存是引擎分配的，只有成功填充了结构，才需要通知引擎回收。

#### 4.1.3 源码精读

函数的开头先决定是否清空保留注释标记，然后立即进入第一段：

[sources/SquirrelInputController.swift:437-441](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L437-L441) 把 `clearReservedComments` 默认值设为 `true`，并在为真时清空 `specialCommentIndices`，随后调用 `rimeConsumeCommittedText()` 开启第一段消费。

三段的骨架（精简后）如下，注意每个 `get_*` 都被 `if` 包住，成功才进入处理与 `free_*`：

```swift
// 第 2 段：status
var status = RimeStatus_stdbool.rimeStructInit()
if rimeAPI.get_status(session, &status) {
  // ...schema 切换检测、inline 策略计算...
  _ = rimeAPI.free_status(&status)
}

// 第 3 段：context
var ctx = RimeContext_stdbool.rimeStructInit()
if rimeAPI.get_context(session, &ctx) {
  // ...读 preedit / 候选 / 标签 / 页码，调用 show / showPanel...
  _ = rimeAPI.free_context(&ctx)
} else {
  hidePalettes()   // 取不到上下文（比如会话已空），就收起面板
}
```

[sources/SquirrelInputController.swift:458-548](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L458-L548) 是第 3 段的完整实现，末尾 [sources/SquirrelInputController.swift:545](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L545) 的 `free_context` 与 [sources/SquirrelInputController.swift:546-548](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L546-L548) 的 `else hidePalettes()` 构成「成功就释放、失败就藏面板」的对称结构。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立 `rimeUpdate` 的三段心智模型。
2. **操作步骤**：打开 `sources/SquirrelInputController.swift`，定位到 `func rimeUpdate(clearReservedComments:)`（约第 437 行），用三个不同颜色的高亮分别标出：第 1 段 `rimeConsumeCommittedText()`、第 2 段 `get_status`~`free_status`、第 3 段 `get_context`~`free_context`。
3. **需要观察的现象**：确认每一段都遵循「`var xxx = ....rimeStructInit()` → `if rimeAPI.get_xxx(session, &xxx)` → 处理 → `free_xxx`」的相同模板。
4. **预期结果**：三段结构完全同构，唯一差别是第 1 段被抽成了独立函数 `rimeConsumeCommittedText`，而第 2、3 段内联在 `rimeUpdate` 里。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `rimeUpdate` 的三段顺序是 commit → status → context，而不是别的顺序？

> 参考答案：commit 表示「引擎已经定稿、必须立即上屏」的最高优先级动作，必须最先处理，否则后续刷新界面时上屏文字可能错位；status 里的 schema 切换会改变 inline 策略，必须在读 context 之前算好，因为 context 段要根据 inline 策略决定走 `show` 还是 `showPanel`。所以顺序有依赖关系，不能随意调换。

**练习 2**：`rimeUpdate` 这个名字会不会误导初学者？它的「Update」指什么？

> 参考答案：会。「Update」指的是**更新前端 UI**（让面板、marked text 反映引擎最新状态），而不是更新引擎。函数内部没有任何 `process_key` / `set_option`（schema 切换时的 `soft_cursor` 除外）这类「向引擎写入」的调用，它是纯读取+渲染。

---

### 4.2 第一段：rimeConsumeCommittedText —— get_commit 取上屏文本

#### 4.2.1 概念说明

当用户选中一个候选词、或引擎因某些规则决定「这段输入可以定稿了」，引擎就会产生一条 **commit**（提交文本）。前端的任务是：把这条文字通过 `client.insertText` 真正「上屏」到目标应用，然后释放引擎分配的结构。

这部分逻辑被单独抽成了一个私有函数 `rimeConsumeCommittedText()`，名字里的 "Consume"（消费）很贴切：取走、用掉、清理。

#### 4.2.2 核心流程

```text
rimeConsumeCommittedText:
  1. 声明 RimeCommit = .rimeStructInit()   // 清零 + 填 data_size
  2. if get_commit(session, &commitText):  // 引擎有定稿文字？
       a. 取出 commitText.text（C 字符串）→ 转 Swift String
       b. commit(string:)                    // 上屏：client.insertText
       c. free_commit(&commitText)           // 通知引擎回收
```

注意 `get_commit` 的语义：**只有当引擎本轮有新的定稿文字时才返回 true**。如果用户只是在 preedit 里继续敲字、尚未定稿，`get_commit` 返回 false，整段被跳过，不会重复上屏。

#### 4.2.3 源码精读

[sources/SquirrelInputController.swift:426-434](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L426-L434) 是完整实现，中文逐行说明：

- 第 427 行 `var commitText = RimeCommit.rimeStructInit()`：声明一个 `RimeCommit` C 结构并用桥接工具初始化（见 4.5 节）。
- 第 428 行 `rimeAPI.get_commit(session, &commitText)`：把结构体地址传给引擎，引擎若有定稿则填充 `commitText.text` 并返回 true。
- 第 429-430 行：把 C 字符串 `commitText.text` 转成 Swift `String`。
- 第 430 行 `commit(string:)`：上屏。
- 第 432 行 `free_commit`：配对释放。

上屏动作 `commit(string:)` 本身非常薄：

[sources/SquirrelInputController.swift:551-556](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L551-L556) 用 `client.insertText(string, replacementRange: .empty)` 把文字插进目标应用，然后清空缓存的 `preedit` 并隐藏面板——因为定稿意味着本次输入结束，预编辑状态归零。

> 小提示：这里复用的 `commit(string:)` 与 `commitComposition`（u2-l3 讲过的「安全网」）共用同一条上屏路径，区别只是文字来源——一个来自引擎定稿（`get_commit`），一个来自原始输入码（`get_input`）。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：理解 `get_commit` 的「有则上屏、无则跳过」语义。
2. **操作步骤**：在 `rimeConsumeCommittedText` 的 `if rimeAPI.get_commit(...)` 这一行打个断点（或想象打），分别模拟两种场景：(a) 用户刚按下数字键选了第 1 个候选；(b) 用户只是在拼音串里加了一个字母 `n`。
3. **需要观察的现象**：场景 (a) 中 `get_commit` 返回 true，`commitText.text` 指向被选中的汉字，随后 `commit(string:)` 执行；场景 (b) 中 `get_commit` 返回 false，整个 `if` 块跳过。
4. **预期结果**：场景 (a) 上屏并隐藏面板；场景 (b) 不上屏，流程继续进入 status/context 段去刷新 preedit。
5. 若无法本地运行，明确标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉第 432 行的 `free_commit`，会发生什么？

> 参考答案：引擎为 `commitText.text` 分配的堆内存不会被回收。由于用户每上屏一个词就会触发一次 `get_commit`，长时间使用会造成持续内存泄漏。

**练习 2**：`get_commit` 返回 false 时，`commitText` 这个结构体还需要 `free` 吗？

> 参考答案：不需要。`get_commit` 返回 false 意味着引擎没有填充任何需要回收的内容（`text` 仍为 nil），所以代码用 `if` 把整个处理+释放都包住了，false 时直接跳过 `free_commit`。结构体本身是栈上的值类型，函数返回即自动销毁。

---

### 4.3 第二段：get_status 与 schema 切换重载、inline 策略计算

#### 4.3.1 概念说明

`get_status` 返回会话的「元状态」，其中前端最关心的是 **`schema_id`**（当前输入方案标识符，如 `luna_pinyin`）。输入方案决定了按键到汉字的规则集合（回顾 u1-l1）。当用户切换方案（比如从朙月拼音切到双拼），`schema_id` 会变，前端必须：

1. **重新加载该方案的样式**（字体、配色、是否 inline 预编辑等）——因为不同方案可能配不同主题。
2. **重新计算 inline 策略**——`inlinePreedit` / `inlineCandidate` 决定 preedit 是画在应用自己的文本框里（inline），还是画在独立的面板里。

#### 4.3.2 核心流程

```text
get_status 段：
  if get_status(session, &status):
    if schema_id 变了（或首次）:
      schemaId = 新 schema_id
      loadSettings(for: schemaId)          // AppDelegate 重载样式（亮+暗）
      计算 inline 策略：
        inlinePreedit   = (panel.inlinePreedit && !no_inline) || inline
        inlineCandidate = panel.inlineCandidate && !no_inline
        set_option("soft_cursor", !inlinePreedit)   // 把光标可见性同步给引擎
    free_status
```

这里有一个关键的「**两方联合判定**」：

- `panel.inlinePreedit` / `panel.inlineCandidate`：来自**主题配置**（squirrel.yaml 的 `style` 节，由 `loadSettings` 刚刚刷新），代表用户/主题的意愿。
- `no_inline` / `inline`：来自 **librime 运行时选项**（`rimeAPI.get_option`），可被 app 级选项（`app_options`）或方案动态设置，代表当前上下文的约束。

两者取交集/并集，才能得到最终行为。这正是下一讲（u2-l7）的主题。

`set_option("soft_cursor", !inlinePreedit)` 是把「要不要软光标」这个决定**反向同步给引擎**——当 preedit 不在应用内 inline 显示时，引擎需要在 preedit 串里渲染一个光标符号（soft_cursor）。

#### 4.3.3 源码精读

[sources/SquirrelInputController.swift:443-456](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L443-L456) 是完整的 status 段，中文说明：

- 第 443 行 `RimeStatus_stdbool.rimeStructInit()`：初始化 status 结构。
- 第 444 行 `get_status(session, &status)`：填充 `status.schema_id` 等字段。
- 第 446 行的条件 `schemaId == "" || schemaId != String(cString: schema_id)`：**首次（`schemaId` 为空）或发生了切换**才进入重载。这是个重要的去重——同一次会话内每次按键都调 `rimeUpdate`，但只有方案真正改变时才重载样式，避免无谓开销。
- 第 448 行 `NSApp.squirrelAppDelegate.loadSettings(for: schemaId)`：委托 AppDelegate 重新加载该方案的样式。
- 第 450-452 行：联合判定计算两个 inline 标志，并把 `soft_cursor` 同步给引擎。
- 第 455 行 `free_status`：配对释放。

重载细节在 AppDelegate 里：

[sources/SquirrelApplicationDelegate.swift:184-199](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L184-L199) 用一个临时 `SquirrelConfig` 打开「schema 配置叠加在 base config 之上」，如果该 schema 有自己的 `style` 节，就为亮/暗两种模式分别加载（`panel.load(config:forDarkMode:)`）；否则回退到 base config。这保证了「方案没特化样式时沿用全局样式」的回退语义（详见第三单元）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：理解 schema 切换的「去重 + 重载 + 反向同步」三件事。
2. **操作步骤**：阅读 [sources/SquirrelInputController.swift:446-453](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L446-L453)，回答三个问题：(1) 为什么条件里要有 `schemaId == ""`？(2) `loadSettings(for:)` 何时回退到 base config？(3) `set_option("soft_cursor", !inlinePreedit)` 是「前端→引擎」还是「引擎→前端」的方向？
3. **需要观察的现象**：注意 `inlinePreedit` 表达式里 `||` 和 `&&` 的组合——`inline` 选项可以**单方面强制开启** inline preedit，而 `no_inline` 只能**关闭**它。
4. **预期结果**：(1) 首次进入时缓存 `schemaId` 为空，必须重载一次；(2) 当 `schema.open(...)` 失败或该 schema 无 `style` 节时回退；(3) 是前端→引擎的反向同步。
5. 若无法本地运行，明确标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：假设某应用在 `app_options` 里设了 `no_inline: true`，而当前主题 `style.inline_preedit: true`。最终 `inlinePreedit` 是 true 还是 false？

> 参考答案：false。代入公式 `inlinePreedit = (panel.inlinePreedit && !no_inline) || inline = (true && !true) || false = false`。`no_inline` 起到了「否决」作用。

**练习 2**：为什么 schema 没变时，要跳过整个重载块？

> 参考答案：性能。`rimeUpdate` 在每次按键、选词、翻页后都会被调用，频率极高。schema 在会话内大多数时候不变，重复执行 `loadSettings`（涉及磁盘读配置、重建主题对象、两次面板加载）会造成巨大浪费。用 `schemaId` 缓存做去重是必要的优化。

---

### 4.4 第三段：get_context 读取 preedit / 候选 / 标签 / 页码

#### 4.4.1 概念说明

`get_context` 是信息量最大的一段，它返回 `RimeContext`，里面有：

- `composition.preedit`：当前预编辑串（如「你 hao」对应的带高亮标记的字符串）。
- `composition.sel_start` / `sel_end` / `cursor_pos`：选中区与光标在 preedit 里的字节偏移。
- `menu`：候选菜单，含候选数组（`candidates[i].text` / `.comment`）、`select_keys`、`num_candidates`、`page_no`、`is_last_page`、`highlighted_candidate_index`、`page_size`。
- `commit_text_preview`：inline 候选时，当前高亮候选的预览文字。
- `select_labels`：每页的序号标签（如「1 2 3 …」）。

前端要做的，是把这些 C 字段逐一转成 Swift 的 `[String]` / `Int` / `Bool`，再分别驱动两条输出：

- **`show(...)`**：设置应用文本框里的 marked text（带高亮的 preedit）。
- **`showPanel(...)`**：刷新独立候选面板（preedit + 候选 + 标签 + 页码）。

#### 4.4.2 核心流程

```text
get_context 段：
  if get_context(session, &ctx):
    preedit = ctx.composition.preedit → String
    由 sel_start/sel_end/cursor_pos（UTF-8 字节偏移）转成 String.Index

    根据 inline 策略分三路输出 marked text：
      inlineCandidate: 用 commit_text_preview 拼出候选预览，show(...)
      else inlinePreedit: 把完整 preedit 当 marked text，show(...)
      else (都不 inline): 用全角空格 "　" 作占位 marked text，show(...)
                       （真 preedit 留给独立面板画）

    读取候选列表：
      for i in 0..<num_candidates:
        candidates += ctx.menu.candidates[i].text
        comments  += ctx.menu.candidates[i].comment
    读取标签：优先 select_keys，否则 select_labels
    读取页码：page_no / is_last_page

    showPanel(preedit, selRange, caretPos, candidates, comments, labels, highlighted, page, lastPage)
    free_context
  else:
    hidePalettes()
```

#### 4.4.3 源码精读

**读取 preedit 与字节偏移转换**：

[sources/SquirrelInputController.swift:460-464](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L460-L464) 把 `ctx.composition.preedit` 转成 Swift 字符串，再用 `preedit.utf8.index(...offsetBy:)` 把引擎给出的 **UTF-8 字节偏移**（`sel_start` / `sel_end` / `cursor_pos`）转成 Swift 的 `String.Index`。这一步必须做，因为引擎按字节计数，而 Swift 的 `String` 按 UTF-16 或字形计数。每处转换都带 `?? preedit.startIndex` 兜底，防止越界偏移导致崩溃。

**inline 策略分三路输出 marked text**：

[sources/SquirrelInputController.swift:466-517](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L466-L517) 是 inline 拼装逻辑，外层 `if inlineCandidate` / `else if inlinePreedit` / `else` 三分支：

- `inlineCandidate` 分支用 `ctx.commit_text_preview` 作为候选预览，并在 `inlinePreedit` 同时开启时把光标后的未翻译编码从 preedit 截取拼到预览尾（[sources/SquirrelInputController.swift:486-489](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L486-L489)），代码上方那一大段中文注释用图形精确描述了 preedit / commit_text_preview / candidate_preview 三者在不同光标位置下的对齐关系。
- 纯 `inlinePreedit` 分支直接把完整 preedit 作为 marked text。
- 两个都不 inline 时，用**全角空格 `　`（U+3000）**作占位（[sources/SquirrelInputController.swift:513-515](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L513-L515)），注释说明这是为了「防止 iTerm2 回显原始 preedit、半角空格会让中文组合基线不稳」。真正的 preedit 留给独立面板显示。这一细节是下一讲（u2-l7）的重点。

`show(preedit:selRange:caretPos:)` 内部有缓存去重：

[sources/SquirrelInputController.swift:558-562](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L558-L562) 当 `preedit`、`caretPos`、`selRange` 三者都和上次相同时直接 return，避免重复向应用发 `setMarkedText`。

**读取候选、注释、标签、页码**：

[sources/SquirrelInputController.swift:519-539](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L519-L539) 遍历 `ctx.menu.candidates`，把每个候选的 `.text` 和 `.comment`（都是 `UnsafePointer<CChar>?`）用 `String(cString:)` 转成 Swift 字符串。标签有两套来源（[sources/SquirrelInputController.swift:529-536](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L529-L536)）：优先 `select_keys`（方案自定义的选字键，如 `abcdef`），否则用 `select_labels`（按页生成的序号标签）。

> 这里有个相关但本讲不展开的桥接细节：状态栏图标读取的 `get_state_label_abbreviated` 返回的是 `RimeStringSlice`，需要用 `.asString`（[sources/SquirrelApplicationDelegate.swift:258-262](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L258-L262)）按 `.length` 裁剪；而候选的 `.text` / `.comment` 是普通 C 字符串，用 `String(cString:)` 即可。两种字符串类型对应两种桥接方式，不能混用。

**驱动面板并释放**：

[sources/SquirrelInputController.swift:541-544](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L541-L544) 调用 `showPanel(...)` 把全部信息推给面板（注意 `preedit` 在 inline 模式下设为 `""`，避免面板和应用文本框重复显示）。`showPanel` 内部（[sources/SquirrelInputController.swift:581-591](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L581-L591)）先通过 `client.attributes(forCharacterIndex:0, lineHeightRectangle:)` 取到光标在屏幕上的位置，再调 `panel.update(...)` 刷新面板内容。最后第 545 行 `free_context` 释放。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：把 C 结构里的字段映射到 Swift 数据，理清两条输出路径。
2. **操作步骤**：在 [sources/SquirrelInputController.swift:519-544](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L519-L544) 旁边画一张表，左边列 `RimeContext` 的 C 字段（`menu.num_candidates`、`menu.candidates[i].text`、`menu.candidates[i].comment`、`menu.select_keys`、`select_labels`、`menu.page_no`、`menu.is_last_page`、`menu.highlighted_candidate_index`），右边列它们转换后的 Swift 变量（`candidates`、`comments`、`labels`、`page`、`lastPage`、传给 `showPanel` 的 `highlighted`）。
3. **需要观察的现象**：注意标签的「优先 select_keys、否则 select_labels」二选一逻辑，以及页码用的是 `ctx.menu.page_no`（当前页号）和 `ctx.menu.is_last_page`（是否最后一页）两个布尔/整数。
4. **预期结果**：你应能说出，当用户翻到第 2 页候选时，`page` 为 1（从 0 计）、`lastPage` 取决于引擎判断。
5. 若无法本地运行，明确标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：候选的 `.text` 和状态图标的 label 都是从 librime 取回的字符串，为什么前者用 `String(cString:)`，后者用 `.asString`？

> 参考答案：`.text` 是普通 `UnsafePointer<CChar>`，以 `\0` 结尾，`String(cString:)` 适用；状态 label 是 `RimeStringSlice`（带 `str` 指针 + `length` 长度），引擎在无 `abbrev:` 时会把 `.length` 截到首个字形，若用 `String(cString:)` 会读到结尾 `\0`、误取完整的 `states:` 值。所以必须用 `.asString` 尊重 `.length`。

**练习 2**：`show(...)` 为什么要做缓存去重（比较 `self.preedit == preedit && ...`）？

> 参考答案：`rimeUpdate` 调用极其频繁（每次按键），但很多情况下 preedit 实际没变（例如按了个被引擎忽略的键）。重复调用 `client.setMarkedText` 会引发不必要的文本系统刷新、光标抖动甚至性能问题，所以先比对缓存、相同就跳过。

---

### 4.5 配对释放：rimeStructInit 与 free_commit/free_status/free_context 的内存约定

#### 4.5.1 概念说明

前面四节反复出现「配对释放」。这一节集中讲清背后的 **C 内存所有权**原理，这是理解整段 `rimeUpdate` 为什么这么写的根本。

librime 的 `get_commit` / `get_status` / `get_context` 都遵循一个约定：**调用者传入一个结构体地址，引擎把结果填进去**。其中结构体本身的内存由调用者（Swift 栈）拥有，但结构体**内部**的字符串指针、候选数组等，指向的是**引擎在堆上分配**的缓冲区。因此：

- 填充后，调用者负责读取这些数据。
- 用完后，调用者必须调用对应的 `free_*` 通知引擎「我用完了，你可以回收内部缓冲区了」。
- 如果不调 `free_*`，引擎分配的那块内存就永远无法回收 → 内存泄漏。

同时，传入的结构体必须正确初始化——尤其要填上 `data_size` 字段，引擎靠它做 **ABI 版本/大小校验**（这是 C 结构体跨版本兼容的常见手法）。

#### 4.5.2 核心流程

初始化用桥接工具 `rimeStructInit()`，它做两件事：

```text
rimeStructInit():
  1. memset 整块结构为零            // 避免野指针/脏数据
  2. data_size = sizeof(Self) - offsetof(data_size)  // 告诉引擎「我这块有多大」
```

释放用三个一一对应的函数：

| 读取 | 释放 | 何时调 |
|------|------|--------|
| `get_commit` | `free_commit` | `get_commit` 返回 true 之后 |
| `get_status` | `free_status` | `get_status` 返回 true 之后 |
| `get_context` | `free_context` | `get_context` 返回 true 之后 |

**铁律**：每一个成功（返回 true）的 `get_*`，都必须在本次 `rimeUpdate` 调用内，配对一次 `free_*`。

#### 4.5.3 源码精读

`rimeStructInit` 定义在桥接文件里：

[sources/BridgingFunctions.swift:10-19](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L10-L19) 用 `protocol DataSizeable` 约束所有含 `data_size` 字段的 librime 结构体（`RimeContext_stdbool`、`RimeTraits`、`RimeCommit`、`RimeStatus_stdbool`、`RimeModule`）。

[sources/BridgingFunctions.swift:21-30](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L21-L30) 是 `rimeStructInit()` 的实现：`allocate` 一块内存 → `memset` 清零 → `move` 出值 → `deallocate` → 用 `MemoryLayout.size(ofValue: \Self.data_size)` 算出 `data_size` 字段在结构体内的偏移 → 把 `data_size` 设为「结构体总大小减去这个偏移」。中文说就是：`data_size` 表示「从 `data_size` 字段之后到结构体末尾」的字节数，引擎据此判断调用方编译时用的是哪个版本的布局。

> 关键点：为什么不能用 Swift 默认的 `RimeCommit()`？因为 Swift 默认初始化不会把整块内存清零，也不会填 `data_size`。残留的脏指针会被引擎误读，`data_size` 为 0 会让引擎拒绝填充。所以**所有 librime 结构都必须用 `.rimeStructInit()`**，不能用默认 `init`。

配对释放的三个调用点都已在前面三节标注：`free_commit`（[SquirrelInputController.swift:432](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L432)）、`free_status`（[SquirrelInputController.swift:455](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L455)）、`free_context`（[SquirrelInputController.swift:545](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L545)）。注意三处都用 `_ =` 忽略返回值——`free_*` 通常返回 bool 表示是否释放成功，但前端不需要区分。

与之对照的是 `setCString`（[sources/BridgingFunctions.swift:32-41](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L32-L41)）：它处理的是**前端写入引擎**的方向（如 u2-l2 设置 RimeTraits 字段），用 `strdup` 在堆上复制一份 C 字符串，覆盖前先 `free` 旧值。这与 `free_commit` 的「读取后释放」是相反方向但同源的内存约定：C 字符串跨 Swift/C 边界时，谁分配谁负责释放。

#### 4.5.4 代码实践（代码实践任务，对应本讲主任务）

1. **实践目标**：说清三个 `get_*` 各自返回什么，以及为什么必须配对 `free_*`（C 内存所有权）。
2. **操作步骤**：
   - 阅读本节源码与 [sources/SquirrelInputController.swift:426-434](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L426-L434)、[443-456](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L443-L456)、[458-548](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L458-L548)。
   - 填写下面这张表（在心里或纸上）：

     | 调用 | 返回什么信息 | 配对的 free | 内存归谁分配 |
     |------|--------------|-------------|--------------|
     | `get_commit` | ? | ? | ? |
     | `get_status` | ? | ? | ? |
     | `get_context`| ? | ? | ? |

   - 进阶：在 `rimeConsumeCommittedText` 里临时注释掉 `free_commit` 那一行（**仅本地实验，勿提交**），连续输入上百个字，观察内存占用变化。
3. **需要观察的现象**：注释掉 `free_*` 后，每次成功 `get_*` 都会让引擎分配的内部缓冲区无法回收，内存曲线应持续上升。
4. **预期结果（表格答案）**：
   - `get_commit` → 引擎本轮定稿、需上屏的文字（`text`）；配对 `free_commit`；内部 `text` 字符串由引擎分配。
   - `get_status` → 会话元状态，主要是 `schema_id`、是否组合中、是否 ASCII；配对 `free_status`；`schema_id` 等字符串由引擎分配。
   - `get_context` → 预编辑上下文（preedit、选中区、光标、候选菜单、页码）；配对 `free_context`；preedit 字符串、候选数组等由引擎分配。
   - **必须配对 free 的原因**：这些指针指向的是引擎在 C 堆上分配的缓冲区，引擎不知道前端何时用完，只能由前端显式调用 `free_*` 通知它回收；不调则泄漏，这就是 C 内存所有权约定。
5. 若无法本地运行内存实验，明确标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `rimeStructInit` 里 `data_size` 算的是「总大小减去 data_size 字段的偏移」，而不是直接 `sizeof(Self)`？

> 参考答案：librime 的 ABI 约定 `data_size` 表示「从 `data_size` 字段起到结构体末尾」的字节数（不含 ABI 头部那些前置字段）。引擎据此判断调用方编译时的结构体版本/大小，从而做向后兼容。直接用 `sizeof` 会让引擎误判版本。

**练习 2**：如果 `get_context` 返回 true，但前端在读候选时抛了异常、提前 return 了，导致 `free_context` 没执行，会有什么后果？

> 参考答案：引擎为这次 context 分配的 preedit 字符串、候选数组等堆内存无法回收，发生一次泄漏。这就是为什么源码里 `free_context` 紧跟在所有读取逻辑之后、且没有提前 return 的分支——保证读取完毕必然到达释放。注意源码在 `get_context` 失败时走 `else hidePalettes()`，此时引擎没分配内容，不需要 `free`，设计上是自洽的。

## 5. 综合实践

**任务：画出一次「输入拼音 → 选词上屏」的完整 rimeUpdate 数据流图。**

假设用户在某文本框里已经敲了 `ni`，preedit 显示「你」，现在按下 `hao` 中的 `h`。请结合本讲源码，按时间顺序画出 `rimeUpdate()` 这次调用里发生的事，要求标注：

1. 三段消费各自的 `get_*` / `free_*` 配对。
2. 哪一段会触发 `loadSettings`？（提示：只在 schema 变化时，本次 `h` 不会触发。）
3. preedit 从哪个字段取出？经过怎样的字节偏移转换？
4. 本次 `get_commit` 大概率返回 true 还是 false？为什么？（提示：用户还没选词，preedit 还在组合中。）
5. 最终调用的是 `show(...)` 还是 `showPanel(...)` 还是两者都调？分别传了什么？

完成后，再考虑一个对照场景：用户按数字键 `1` 选了第一个候选，此时 `get_commit` 返回什么？`commit(string:)` 做了什么？面板为何隐藏？

> 参考思路：按 `h` 这一次，`get_commit` 返回 false（仍在组合），`get_status` 中 schema 未变故跳过重载块，`get_context` 取出新 preedit（如「你 h」），走 inline 或面板刷新。按 `1` 选词那一次，`get_commit` 返回 true，上屏候选词，`commit(string:)` 调 `insertText` 并隐藏面板。本实践旨在把三段消费、schema 去重、字节偏移转换、配对释放、双输出路径全部串起来。

## 6. 本讲小结

- `rimeUpdate()` 是「从引擎取结果」的唯一出口，采用 **commit → status → context** 的三段式消费，每段都是「`rimeStructInit` → `get_*` → 处理 → `free_*`」的同构模板。
- 第一段 `rimeConsumeCommittedText` 用 `get_commit` 取回定稿文字并 `insertText` 上屏，无定稿则整段跳过。
- 第二段 `get_status` 检测 schema 切换（用 `schemaId` 缓存去重），触发 `loadSettings(for:)` 重载样式，并联合主题配置与 librime 选项计算 `inlinePreedit` / `inlineCandidate`，再把 `soft_cursor` 反向同步给引擎。
- 第三段 `get_context` 信息量最大：把 UTF-8 字节偏移转成 `String.Index`，按 inline 策略三路输出 marked text（`show`），并读取候选/注释/标签/页码驱动面板（`showPanel`）。
- 所有 librime 结构必须用 `.rimeStructInit()` 初始化（清零 + 填 `data_size` 做 ABI 校验），每个成功的 `get_*` 必须配对 `free_*`，因为结构内部的字符串/数组是引擎在 C 堆上分配的，前端不释放就泄漏。
- `rimeUpdate` 在每次按键、选词、翻页、移动光标、ASCII 切换、和弦超时后都会被调用，是前端反映引擎状态变更的统一收口。

## 7. 下一步学习建议

- 下一讲 **u2-l7「marked text、commit 与 inline 策略」** 会深入本讲留下的两个悬念：`inlinePreedit` / `inlineCandidate` 的完整联合判定规则，以及非 inline 模式下为何要用全角空格 U+3000 作占位 marked text（iTerm2 回显问题）。建议接着读。
- 想了解 schema 切换时样式如何叠加/回退，可先读第三单元的 **u3-l1（SquirrelConfig 门面）** 与 **u3-l3（SquirrelTheme 加载）**，再回看本讲 `loadSettings(for:)` 调用链。
- 想理解 `soft_cursor`、`_linear`、`_vertical`、`_chord_typing` 这些 librime 运行时选项的全貌，建议通读 `SquirrelInputController.swift` 中所有 `get_option` / `set_option` 调用点。
- 关于 `RimeStringSlice.asString` 与 `String(cString:)` 两种字符串桥接的差异，可结合 `SquirrelApplicationDelegate.swift` 第 253-263 行的状态图标代码对照理解。
