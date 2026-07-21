# SquirrelPanel 模型与 update

## 1. 本讲目标

本讲是「专家：候选词面板 UI」单元的第一讲。读者在前面的讲义里已经看到：`rimeUpdate` 把引擎的候选、preedit、注释、页码取回前端后，最终会调用 `showPanel` 把这些数据交给一个名为 `SquirrelPanel` 的窗口。本讲要回答的核心问题是：

> 引擎给回来的是一串候选字符串，面板是怎么把它变成屏幕上一块带编号、带高亮、带注释的富文本的？

学完本讲，你应当能够：

1. 说清 `SquirrelPanel.update(...)` 如何把「preedit 行 + 多个候选行」拼装成**单一** `NSMutableAttributedString`，并按主题上色。
2. 复述 `candidate_format` 模板（默认 `[label]. [candidate] [comment]`）的「先加属性、再替字面量」两步替换流程。
3. 解释自定义属性 `.noBreak` 在短候选中防止断行的原理，以及它和 TextKit 2 布局代理的配合。
4. 理解为什么拼完文本后必须**强制 `ensureLayout`**，才能拿到正确的换行与高亮矩形。

本讲只讲「数据 → 富文本 → 触发布局」这一段；面板怎么定位、怎么自绘背景、怎么响应鼠标，分别属于后续 u4-l2 / u4-l3 / u4-l4。

---

## 2. 前置知识

本讲假设你已经掌握以下概念（来自前置讲义）：

- **`rimeUpdate` 三段式消费**（u2-l6）：前端从引擎取候选、preedit、注释、页码等信息。
- **marked text 与 commit、inline 策略**（u2-l7）：当 `inlinePreedit`/`inlineCandidate` 关闭时，候选与预编辑**不**写进宿主应用的文本框，而是画进独立的面板窗口——本讲讲的就是这个面板。
- **主题加载**（u3-l3）：`SquirrelTheme.load` 把 YAML 读出的值组装成一组可直接绘制的外观对象，包括各种 `attrs` 字典、`candidateFormat`、`paragraphStyle`、`edgeInset` 等。本讲会大量引用这些主题属性。

此外需要一点 Cocoa 文本系统的常识：

- **`NSAttributedString`（富文本）**：一段字符串，每个字符区间可以挂一组「属性」（字体、颜色、段落样式……）。AppKit 用它来渲染带样式的文字。
- **`NSRange`**：用 `location`（起点）+ `length`（长度）描述一个字符区间，常用 `NSString` 的 UTF-16 编码单位计数。
- **TextKit 2**：macOS 的新一代文本布局引擎，核心类有 `NSTextContentStorage`（存富文本）、`NSTextLayoutManager`（做布局）、`NSTextContainer`（约束排版区域）。布局是**惰性**的——你不主动 `ensureLayout`，它就不会立刻算出每个字符画在哪。

> 本讲引用的永久链接基址为：
> `https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/`

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到什么 |
| --- | --- | --- |
| `sources/SquirrelPanel.swift` | 候选词面板窗口，继承 `NSPanel`，负责「拼富文本 + 测量尺寸 + 定位 + 转发鼠标事件」 | `update(...)` 拼装富文本、`candidate_format` 替换、`.noBreak`、强制 `ensureLayout` |
| `sources/SquirrelView.swift` | 面板的内容视图，承载 `NSTextView` 并自绘背景/高亮 | 自定义 `.noBreak` 属性、`SquirrelLayoutDelegate` 决定能否断行、`drawView(...)` 与 `draw(_:)` 的触发 |
| `sources/SquirrelTheme.swift` | 主题对象，提供各 `attrs`、`candidateFormat`、段落样式 | `candidateFormat` 的默认值与 setter 归一化、各颜色/字体属性字典 |

读者可以先把这三个文件在编辑器里并排打开，边读讲义边对照。

---

## 4. 核心概念与源码精读

### 4.1 富文本拼装（preedit 行 + 候选行）

#### 4.1.1 概念说明

`SquirrelPanel` 是一个窗口，但窗口里真正显示文字的是一个 `NSTextView`。TextKit 2 的设计是：**一个 text view 对应一整段富文本**。所以无论面板上有几行候选、有没有 preedit，最终都要把它们**拼成同一段 `NSAttributedString`**，一次性塞给 `textView.textContentStorage`。

这就解释了为什么 `update` 函数的参数是一堆「散件」（preedit 字符串、候选数组、注释数组、标签数组、高亮索引……）——它们是引擎拆开给回来的，面板要把它们重新「缝」成一段富文本。

缝的逻辑很朴素：

- 第 1 行（可选）：**preedit 行**——用户正在输入的编码串，已转换段高亮。
- 第 2 行起（可选）：**候选行**——每行一个候选词，配编号、注释；当前选中行用高亮属性。
- 行与行之间用换行符 `\n`（堆叠布局 `linear=false`）或两个空格 `"  "`（横排布局 `linear=true`）分隔。

注意「行」是逻辑概念：横排模式下候选之间不是真换行，而是空格分隔，靠段落样式（`headIndent` 缩进）让多行候选对齐成网格。

#### 4.1.2 核心流程

`update(...)` 是面板唯一的「刷新入口」，其结构可概括为：

```
update(...):
  1. 若 update=true：把入参缓存到实例属性（供鼠标移动时局部重绘用）
  2. cursorIndex = index
  3. 分流：
     - 有候选或有 preedit → 清掉状态消息，继续拼富文本
     - 无候选且无 preedit → 显示状态消息或隐藏面板，return
  4. 取主题 currentTheme、算当前屏幕 currentScreen()
  5. 新建空 text = NSMutableAttributedString()
  6. 拼 preedit 行（若有）→ append
  7. 循环拼每个候选行 → append（每行先记下 NSRange 存进 candidateRanges）
  8. text → textView.textContentStorage
  9. 设排版方向（横/竖）
  10. 强制 ensureLayout（见 4.4）
  11. drawView(candidateRanges, ...) 触发自绘
  12. show() 定位并显示窗口
```

第 1 步的「缓存」很关键：`sendEvent` 在鼠标移动/离开时会以 `update:false` 重新调用本函数，只为改变高亮索引而重绘，此时用的是缓存里的 preedit/候选/注释。所以入参里那个布尔参数 `update` 的含义是「是否刷新缓存数据」，而不是「是否重绘」。

#### 4.1.3 源码精读

整个函数签名与缓存逻辑在 [sources/SquirrelPanel.swift:153-164](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L153-L164)，注意它因为参数多、分支多被 `swiftlint` 标注了 `cyclomatic_complexity function_parameter_count` 两条豁免：

```swift
// swiftlint:disable:next cyclomatic_complexity function_parameter_count
func update(preedit: String, selRange: NSRange, caretPos: Int, candidates: [String],
            comments: [String], labels: [String], highlighted index: Int,
            page: Int, lastPage: Bool, update: Bool) {
  if update {
    self.preedit = preedit
    ...
    self.index = index
    ...
  }
  cursorIndex = index
```

随后是「分流」：有内容就清状态消息继续，否则显示状态消息或隐藏——见 [sources/SquirrelPanel.swift:167-179](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L167-L179)。这说明面板同时承担「候选面板」和「短暂状态提示（如同步完成）」两种用途，两者互斥。

拼 preedit 行见 [sources/SquirrelPanel.swift:184-204](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L184-L204)：

```swift
let text = NSMutableAttributedString()
...
if !preedit.isEmpty {
  preeditRange = NSRange(location: 0, length: preedit.utf16.count)
  highlightedPreeditRange = selRange

  let line = NSMutableAttributedString(string: preedit)
  line.addAttributes(theme.preeditAttrs, range: preeditRange)         // 整段上 preedit 色/字体
  line.addAttributes(theme.preeditHighlightedAttrs, range: selRange)  // 已转换段再叠高亮色
  text.append(line)

  text.addAttribute(.paragraphStyle, value: theme.preeditParagraphStyle,
                   range: NSRange(location: 0, length: text.length))
  if !candidates.isEmpty {
    text.append(NSAttributedString(string: "\n", attributes: theme.preeditAttrs))  // 与候选行分隔
  }
}
```

这里体现了富文本「属性叠加」的典型用法：先对整段 preedit 加 `preeditAttrs`，再对 `selRange` 这一小段**覆盖** `preeditHighlightedAttrs`（同键后者覆盖前者），从而得到「编码段灰、已转换段高亮」的效果。`preeditRange` 与 `highlightedPreeditRange` 被记下来，后面交给 `drawView` 用于画 preedit 的背景/高亮矩形。

候选行的循环主体在 [sources/SquirrelPanel.swift:206-283](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L206-L283)，它的细节（模板替换、属性）会在 4.2、4.3 展开，这里先看「拼装」本身：

```swift
let lineSeparator = NSAttributedString(string: linear ? "  " : "\n", attributes: attrs)
if i > 0 { text.append(lineSeparator) }   // 行间分隔：横排两空格、堆叠换行
...
candidateRanges.append(NSRange(location: text.length, length: line.length))  // 记下本行在整段中的区间
text.append(line)                                                              // 把这一候选行缝进大文本
```

`candidateRanges` 是连接「文本世界」与「绘制世界」的桥梁：绘制时需要知道每个候选词在哪几个字符、当前高亮是第几行，才能画出对应的背景矩形（见 u4-l3 的 `draw`）。所以拼装阶段每 append 一行，就立刻把它的 `NSRange` 存起来。

最终整段文本在 [sources/SquirrelPanel.swift:285-286](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L285-L286) 交给 text view，并设置横排/竖排方向：

```swift
view.textView.textContentStorage?.attributedString = text
view.textView.setLayoutOrientation(vertical ? .vertical : .horizontal)
```

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，亲手在脑中走一遍「两个候选 + 一段 preedit」是如何拼成一段富文本的。

**操作步骤**：

1. 打开 [sources/SquirrelPanel.swift:184-283](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L184-L283)。
2. 假设引擎给回：`preedit = "ni hao"`、`candidates = ["你好", "拟好"]`、`comments = ["[han]", "[han]"]`、`labels = []`、`index = 0`（高亮第 0 个）。
3. 在纸上画出最终 `text` 的字符序列与每段的属性归属。

**需要观察的现象 / 预期结果**：

- `text` 的字符序列大致是：`ni hao\n[label]. [candidate] [comment]  [label]. [candidate] [comment]`（其中 `[label]/[candidate]/[comment]` 已被实际替换）。
- preedit 段带 `preeditAttrs`，其中 `selRange` 子段额外叠 `preeditHighlightedAttrs`。
- 第一个候选行带 `highlightedAttrs`/`labelHighlightedAttrs`/`commentHighlightedAttrs`（因为 `i == index`），第二个候选行带普通的 `attrs`/`labelAttrs`/`commentAttrs`。
- 两个候选行之间是 `"  "`（横排）或 `"\n"`（堆叠）。

> 本实践为源码阅读型，不要求编译运行。

#### 4.1.5 小练习与答案

**练习 1**：`update` 的最后一个参数叫 `update: Bool`，它控制什么？为什么 `sendEvent` 里鼠标移动时要传 `update: false`？

**答案**：它控制「是否把入参刷新到实例缓存」。鼠标移动时只改变高亮索引（`highlighted: index`），preedit/候选/注释都没变，所以传 `update: false` 复用缓存，避免重新拷贝数据；但函数仍然会走完拼装与重绘流程以更新高亮显示。

**练习 2**：为什么面板要把 preedit 和所有候选拼成**同一段**富文本，而不是给每个候选一个独立的 `NSTextField`？

**答案**：一是 TextKit 2 的模型本身是「一段文本对应一个布局器」，单段文本能统一处理换行、对齐、段落缩进（如堆叠模式用 `headIndent` 对齐候选）；二是后续 `drawView` 要用 `candidateRanges` 在同一坐标系里画背景矩形、做命中测试，单段文本的区间映射最简单、最一致。

---

### 4.2 candidate_format 模板替换

#### 4.2.1 概念说明

候选行的样子不是写死的。Squirrel 用一个模板字符串 `candidate_format` 来描述「每一行候选长什么样」，默认是：

```
[label]. [candidate] [comment]
```

三个具名占位符的含义：

| 占位符 | 含义 | 来源 |
| --- | --- | --- |
| `[label]` | 候选编号（如 `1`、`2`） | `labels` 数组，或自动从 1 递增 |
| `[candidate]` | 候选词本身（如「你好」） | `candidates[i]` |
| `[comment]` | 注释（如拼音 `[han]`） | `comments[i]` |

用户可在 `squirrel.yaml` 里改它，例如改成 `[candidate] [comment]`（不要编号）或保留旧式 `%c %@`（会被归一化，见 4.2.4）。

模板替换有一个**非常关键的设计**：**先对占位符所在区间加属性，再把占位符字面量替换成实际字符串**。顺序不能反——因为加属性用的是 `NSRange`，而一旦把 `[candidate]` 换成「你好」，字符串长度变了，之前算好的区间就错位了。

#### 4.2.2 核心流程

单个候选行的构造（伪代码）：

```
line = NSMutableAttributedString(string: candidateFormat, attributes: labelAttrs)
                       # 整行默认用 labelAttrs（编号的字体/颜色）

for 每个匹配 [candidate] 的区间:
    line.addAttributes(attrs, range: [candidate]区间)        # 候选词字体/颜色
    若 candidate.count <= 5:
        line.addAttribute(.noBreak, range: [candidate]区间去掉首字符)  # 防断行

for 每个匹配 [comment] 的区间:
    若该行非高亮 且 命中 specialCommentIndices:
        用 accent/warning 颜色覆盖 foregroundColor
    否则:
        line.addAttributes(commentAttrs, range: [comment]区间)

line.replaceOccurrences("[label]"    → 实际编号字符串)   # 现在才替换字面量
labeledLine = line.copy()                                  # 拷贝一份「只换好 label」的版本（用于量 label 宽度）
line.replaceOccurrences("[candidate]" → 实际候选词)
line.replaceOccurrences("[comment]"   → 实际注释)
```

注意第 250 行替换 `[label]` 之后，第 251 行**立即拷贝**了一份 `labeledLine`——这是为了在替换 `[candidate]`/`[comment]` 之前，先量出「编号 + 模板前缀」的宽度，用作堆叠模式下后续候选行的缩进对齐（`headIndent`，见 4.1.3 的 `paragraphStyleCandidate`）。

#### 4.2.3 源码精读

候选行循环开头先按「是不是高亮行」选出三套属性，见 [sources/SquirrelPanel.swift:208-210](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L208-L210)：

```swift
let attrs = i == index ? theme.highlightedAttrs : theme.attrs
let labelAttrs = i == index ? theme.labelHighlightedAttrs : theme.labelAttrs
let commentAttrs = i == index ? theme.commentHighlightedAttrs : theme.commentAttrs
```

接着是 `[label]` 实际文本的解析，逻辑见 [sources/SquirrelPanel.swift:212-222](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L212-L222)：只有当模板里**含** `[label]` 时才计算编号文本（否则编号不显示）；编号来源优先级是：多元素 `labels` 数组 → 单字符串 `labels` 的第 i 个字符 → 默认 `i+1`。模板里没有 `[label]` 时给空串，连 `[label]` 占位符本身也会在替换时变成空——这是「无编号样式」的实现方式。

候选词与注释都做了 Unicode 规范化，见 [sources/SquirrelPanel.swift:224-225](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L224-L225)：

```swift
let candidate = candidates[i].precomposedStringWithCanonicalMapping
let comment = comments[i].precomposedStringWithCanonicalMapping
```

`precomposedStringWithCanonicalMapping` 把组合字符（如 `e` + `´`）合成预组合形式（`é`），避免同一字形用不同码位导致显示/测量不一致。

模板先整体用 `labelAttrs` 创建（因为编号外的标点也归「label 视觉」），见 [sources/SquirrelPanel.swift:227](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L227)：

```swift
let line = NSMutableAttributedString(string: theme.candidateFormat, attributes: labelAttrs)
```

然后是「先加属性」阶段，`[candidate]` 与 `[comment]` 分别处理，见 [sources/SquirrelPanel.swift:228-249](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L228-L249)。这里用 Swift 的正则字面量 `/\[candidate\]/` 匹配所有出现位置，再用 `line.string.ranges(of:)` 拿到 `Range<String.Index>`，由 [convert(range:in:)](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L547-L551) 转成 UTF-16 的 `NSRange` 再加属性：

```swift
for range in line.string.ranges(of: /\[candidate\]/) {
  let convertedRange = convert(range: range, in: line.string)
  line.addAttributes(attrs, range: convertedRange)
  if candidate.count <= 5 {
    line.addAttribute(.noBreak, value: true,
                     range: NSRange(location: convertedRange.location+1, length: convertedRange.length-1))
  }
}
```

`[comment]` 区间还额外支持「语义色」：当某候选的注释被插件标记为 `_comment_highlight`/`_comment_warning`（见 u5-l3 保留属性），且本行**非高亮**时，用 `accentCommentTextColor`/`warningCommentTextColor` 覆盖前景色——见 [sources/SquirrelPanel.swift:235-249](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L235-L249)。

最后才是「再替字面量」阶段，三行 `replaceOccurrences` 依次替换，见 [sources/SquirrelPanel.swift:250-253](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L250-L253)：

```swift
line.mutableString.replaceOccurrences(of: "[label]",    with: label,    range: NSRange(location: 0, length: line.length))
let labeledLine = line.copy() as! NSAttributedString                      // 量 label 宽用
line.mutableString.replaceOccurrences(of: "[candidate]", with: candidate, range: NSRange(location: 0, length: line.length))
line.mutableString.replaceOccurrences(of: "[comment]",   with: comment,   range: NSRange(location: 0, length: line.length))
```

#### 4.2.4 candidate_format 的默认值与旧式归一化

默认模板与归一化逻辑在主题文件里。默认值见 [sources/SquirrelTheme.swift:80](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L80)：

```swift
private var _candidateFormat = "[label]. [candidate] [comment]"
```

旧式 `%@`/`%c` 的归一化在 setter 里完成，见 [sources/SquirrelTheme.swift:175-188](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L175-L188)：`%@` 展开成 `[candidate] [comment]`、`%c` 替换成 `[label]`。所以无论用户写新式还是旧式（如 `data/squirrel.yaml` 的 `clean_white` 配色用的 `%c %@`，见 [data/squirrel.yaml:239](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L239)），到达 `update` 时都已经是统一的具名占位符形式。这一归一化在 u3-l3 已详细讲过，这里只做承接。

#### 4.2.5 代码实践

**实践目标**：手工走一遍默认模板 `[label]. [candidate] [comment]` 在第 0 个候选（`i=0`，高亮）上的替换流程。

**操作步骤**：设 `candidateFormat = "[label]. [candidate] [comment]"`，`label = "1"`，`candidate = "你好"`（count=2，≤5），`comment = "ni hao"`，`i == index`（高亮）。

1. 初始 `line.string` = `"[label]. [candidate] [comment]"`，整段属性 `labelHighlightedAttrs`。
2. 找到 `[candidate]` 区间（位置 9–19），叠 `highlightedAttrs`；因 `candidate.count=2 ≤ 5`，再在该区间去掉首字符处加 `.noBreak=true`。
3. 找到 `[comment]` 区间（位置 21–29），叠 `commentHighlightedAttrs`（非 special，走 else 分支）。
4. `replaceOccurrences("[label]" → "1")`，`line.string` 变 `"1. [candidate] [comment]"`。
5. 拷贝 `labeledLine`（此时只换了 label）。
6. `replaceOccurrences("[candidate]" → "你好")`、`"[comment]" → "ni hao")`，最终 `"1. 你好 ni hao"`。

**需要观察的现象 / 预期结果**：

- 字符串 `"1. 你好 ni hao"` 中，「你好」二字携带 `highlightedAttrs + .noBreak`，「ni hao」携带 `commentHighlightedAttrs`，其余字符（`1`、`.`、空格）携带 `labelHighlightedAttrs`。
- `.noBreak` 加在「你好」去掉首字符的区间，意图是让这两个字尽量不被 TextKit 拆到两行。

> 本实践为源码阅读型，可在 Xcode 里给 `update` 打断点观察 `line.string` 在三步 `replaceOccurrences` 前后的值，以验证上面的推理（待本地验证）。

#### 4.2.6 小练习与答案

**练习 1**：如果用户把 `candidate_format` 设成 `[candidate]`（既无 label 也无 comment），候选编号还会显示吗？

**答案**：不会。代码在 [L212-222](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L212-L222) 用 `candidateFormat.contains(/\[label\]/)` 判断，模板不含 `[label]` 时 `label` 取空串，第 250 行替换 `[label]` 时把不存在的占位符当无操作，于是整行没有编号。

**练习 2**：为什么「加属性」必须在「替换字面量」之前？

**答案**：加属性用的是基于当前字符串算出的 `NSRange`。如果先替换 `[candidate]` 成「你好」，字符串长度改变，原先针对 `[comment]` 算出的区间会错位，属性会加到错误的字符上。所以代码先用占位符的固定长度算区间、加属性，再统一替换字面量。

---

### 4.3 主题属性应用与 noBreak 防断行

#### 4.3.1 概念说明

上两节解决了「拼什么」和「怎么填模板」，这一节解决「为什么短候选不会被拆成两行」。

问题背景：横排模式下，面板宽度有限，当一行候选词太长时，TextKit 会自动换行。但候选词本身被拆开是很难看的——比如「你好」的「你」在第一行末尾、「好」跑到第二行。Squirrel 的做法是给短候选加一个**自定义属性 `.noBreak`**，让布局引擎在决定断行点时跳过它。

`.noBreak` 不是 AppKit 内置属性，而是 Squirrel 自己注册的：

```swift
extension NSAttributedString.Key {
  static let noBreak = NSAttributedString.Key("noBreak")
}
```

它之所以能起作用，是因为面板的 `NSTextLayoutManager` 挂了一个自定义代理 `SquirrelLayoutDelegate`，在「是否允许在此处之前断行」的回调里检查这个属性。

#### 4.3.2 核心流程

防断行有两个层次：

1. **候选词内部不断**（[L231-233](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L231-L233)）：对 `[candidate]` 占位符区间，若候选词字符数 `≤ 5`，在「去掉首字符」的子区间加 `.noBreak`。去掉首字符是为了允许在候选词**前面**断行（把整个候选词推到下一行），但不允许在候选词**内部**断。
2. **整行很短时全行不断**（[L255-257](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L255-L257)）：替换完字面量后，若整行 `length ≤ 10`，给「去掉首字符」的整行加 `.noBreak`，让这一候选行尽量保持在一行里。

代理回调的逻辑（[SquirrelView.swift:10-19](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L10-L19)）：

```
shouldBreakLineBefore(location):
    offset = location 相对文档开头的偏移
    attributes = 该偏移处的属性
    if attributes[.noBreak] == true:
        return false   # 不允许在此处之前断行
    return true
```

即：TextKit 每次想在某个字符前断行，都先问代理「这里能断吗？」；代理看断点**之后**那个字符有没有 `.noBreak`，有就拒绝。

#### 4.3.3 源码精读

自定义属性的定义在 [sources/SquirrelView.swift:21-23](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L21-L23)：

```swift
extension NSAttributedString.Key {
  static let noBreak = NSAttributedString.Key("noBreak")
}
```

布局代理在 [sources/SquirrelView.swift:10-19](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L10-L19)，注意它把 `NSTextLocation` 换算成文档偏移量，再去查那个位置的属性：

```swift
private class SquirrelLayoutDelegate: NSObject, NSTextLayoutManagerDelegate {
  func textLayoutManager(_ textLayoutManager: NSTextLayoutManager,
                         shouldBreakLineBefore location: any NSTextLocation,
                         hyphenating: Bool) -> Bool {
    let index = textLayoutManager.offset(from: textLayoutManager.documentRange.location, to: location)
    if let attributes = textLayoutManager.textContainer?.textView?.textContentStorage?.attributedString?
                        .attributes(at: index, effectiveRange: nil),
       let noBreak = attributes[.noBreak] as? Bool, noBreak {
      return false
    }
    return true
  }
}
```

这个代理在视图初始化时挂到 `textLayoutManager` 上，见 [sources/SquirrelView.swift:61](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L61)：

```swift
textView.textLayoutManager?.delegate = squirrelLayoutDelegate
```

加属性的两处代码：候选词内部不断见 [sources/SquirrelPanel.swift:231-233](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L231-L233)，整行不断见 [sources/SquirrelPanel.swift:255-257](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L255-L257)：

```swift
if candidate.count <= 5 {
  line.addAttribute(.noBreak, value: true,
                   range: NSRange(location: convertedRange.location+1, length: convertedRange.length-1))
}
...
if line.length <= 10 {
  line.addAttribute(.noBreak, value: true, range: NSRange(location: 1, length: line.length-1))
}
```

两处都用 `location+1`、`length-1`，即「去掉第一个字符」。原因前述：允许在候选词/候选行**之前**断行（整体挪到下一行），但禁止在它**内部**断。

> 关于「属性字典」本身：`preeditAttrs`、`highlightedAttrs`、`labelAttrs`、`commentAttrs` 等都是 `SquirrelTheme` 用 `lazy var` 预计算好的 `[NSAttributedString.Key: Any]` 字典，含 `.foregroundColor`/`.font`/`.baselineOffset`，定义见 [sources/SquirrelTheme.swift:110-149](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L110-L149)。主题加载（u3-l3）已讲过它们怎么来，本讲只消费。

#### 4.3.4 代码实践

**实践目标**：观察 `.noBreak` 对短候选换行的实际影响。

**操作步骤**：

1. 阅读代理 [SquirrelView.swift:10-19](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L10-L19)，确认它读的是「断点之后那个字符」的属性。
2. 阅读两处加属性代码 [L231-233](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L231-L233) 与 [L255-257](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L255-L257)，注意都「去掉首字符」。
3. （可选，待本地验证）在本地构建 Squirrel 后，临时把 `candidate.count <= 5` 改成 `candidate.count <= 0`（即不给任何候选加 `.noBreak`），重新部署，在窄面板下输入一个较长候选，观察候选词是否会被拆到两行。

**需要观察的现象 / 预期结果**：

- 正常情况下，5 字以内的候选词不会被拆行。
- 关掉 `.noBreak` 后，窄面板可能把候选词从中间拆开（如「你」在一行末、「好」在下一行首）。
- 还原修改（**本讲禁止修改源码，若做本地实验请自行管理改动并还原**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `.noBreak` 区间是「去掉首字符」（`location+1, length-1`）而不是整个区间？

**答案**：因为代理检查的是「断点之后那个字符」的属性。如果给第一个字符也加 `.noBreak`，那么 TextKit 想在候选词**前面**断行（把整词推到下一行）也会被拒绝，导致候选词无法整体换行，反而挤在行尾。去掉首字符后，允许「在候选词前断行」但禁止「在候选词内部断行」，正是想要的效果。

**练习 2**：`.noBreak` 是 AppKit 内置属性吗？为什么需要自定义？

**答案**：不是，它是 Squirrel 自己用 `NSAttributedString.Key("noBreak")` 注册的自定义键（见 [SquirrelView.swift:21-23](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L21-L23)）。AppKit 的 TextKit 2 提供了 `shouldBreakLineBefore` 代理回调但没有「按属性禁止断行」的内置机制，所以 Squirrel 用自定义属性 + 自定义代理把这个能力补上。

---

### 4.4 TextKit 2 ensureLayout 强制布局

#### 4.4.1 概念说明

把富文本塞给 `textContentStorage` 之后，**文字还没被排好版**。TextKit 2 的布局是惰性的：它只在需要（如滚动、绘制、查询几何）时才计算每个字符画在哪个坐标。

这对 Squirrel 是个坑，因为面板的尺寸、候选高亮矩形、翻页箭头位置，全都依赖「布局完成后才能拿到的几何信息」：

- `view.contentRect` 要遍历候选区间，用 `enumerateTextSegments` 拿每段文本的矩形——没布局就拿不到。
- `show()` 要量出面板的「自然尺寸」来决定是否进入全屏缩放——没布局就量不出。
- `drawView` 要画候选背景、高亮、边框——没布局就不知道画在哪。

所以 `update` 在拼完文本后，必须**主动调用 `ensureLayout`**，把整段文档的布局一次性算完，再去读几何、触发绘制。

#### 4.4.2 核心流程

布局前的准备与强制布局见 [sources/SquirrelPanel.swift:289-298](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L289-L298)：

```
textWidth  = maxTextWidth()                       # 算文本可用最大宽度（限制换行）
maxTextHeight = 屏幕高(或宽，竖排) - 上下内边距
textContainer.size = (textWidth, maxTextHeight)    # 给布局器一个约束框
textLayoutManager.ensureLayout(for: documentRange) # 强制把整篇布局算完
textView.scrollToBeginningOfDocument(nil)          # 防止超高文本自动滚过第一行
drawView(candidateRanges, hilightedIndex, ...)     # 触发自绘（draw 里再读几何）
show()                                             # 量尺寸、定位、显示窗口
```

`ensureLayout` 是同步的——返回时布局已完成，后续的 `contentRect`/`show` 才能拿到正确坐标。注意 `show()` 内部还会**再次** `ensureLayout`（因为全屏缩放路径会改 `textContainer.size` 后重新量尺寸），这在 u4-l2 会详讲。

#### 4.4.3 源码精读

拼完文本后的收尾在 [sources/SquirrelPanel.swift:285-298](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L285-L298)：

```swift
view.textView.textContentStorage?.attributedString = text
view.textView.setLayoutOrientation(vertical ? .vertical : .horizontal)

// Force TextKit 2 layout before measuring wrapped text and highlight bounds.
let textWidth = maxTextWidth()
let maxTextHeight = vertical ? screenRect.width - theme.edgeInset.width * 2
                             : screenRect.height - theme.edgeInset.height * 2
view.textContainer.size = NSSize(width: textWidth, height: maxTextHeight)
view.textLayoutManager.ensureLayout(for: view.textLayoutManager.documentRange)

// Keep very tall wrapped text from auto-scrolling past the first line.
view.textView.scrollToBeginningOfDocument(nil)

view.drawView(candidateRanges: candidateRanges, hilightedIndex: index,
              preeditRange: preeditRange, highlightedPreeditRange: highlightedPreeditRange,
              canPageUp: page > 0, canPageDown: !lastPage)
show()
```

`textContainer.size` 决定了换行宽度：宽度越小，候选越早换行。`maxTextWidth()` 的实现见 [sources/SquirrelPanel.swift:347-358](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L347-L358)，它根据屏幕尺寸、字体缩放、横竖排给出一个上限宽度，目的是「面板别占满整屏」。

`drawView` 只是「登记参数 + 置 `needsDisplay = true`」，真正的绘制在下一帧的 `draw(_:)` 里发生，见 [sources/SquirrelView.swift:118-127](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L118-L127)：

```swift
func drawView(candidateRanges: [NSRange], hilightedIndex: Int, preeditRange: NSRange,
              highlightedPreeditRange: NSRange, canPageUp: Bool, canPageDown: Bool) {
  self.candidateRanges = candidateRanges
  ...
  self.needsDisplay = true     // 标脏，触发 draw(_:)
}
```

而 `draw(_:)`（[sources/SquirrelView.swift:130-301](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L130-L301)）画背景、高亮、候选框、翻页箭头时，会调用 `convert(range:)`（[SquirrelView.swift:78-83](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L78-L83)）把 `NSRange` 转成 `NSTextRange`，再 `enumerateTextSegments` 读矩形——这一切都依赖前面 `ensureLayout` 已完成。`draw` 的具体绘制细节留给 u4-l3，本讲只强调「为什么必须先 ensureLayout」。

`convert(range:)` 把基于 UTF-16 偏移的 `NSRange` 换算成 TextKit 2 的 `NSTextRange`，是文本区间与几何区间之间的桥梁：

```swift
func convert(range: NSRange) -> NSTextRange? {
  guard range != .empty else { return nil }
  guard let startLocation = textLayoutManager.location(textLayoutManager.documentRange.location,
                                                       offsetBy: range.location) else { return nil }
  guard let endLocation = textLayoutManager.location(startLocation, offsetBy: range.length) else { return nil }
  return NSTextRange(location: startLocation, end: endLocation)
}
```

#### 4.4.4 代码实践

**实践目标**：理解「ensureLayout → 读几何 → 绘制」的顺序依赖。

**操作步骤**：

1. 在 [SquirrelPanel.swift:285-298](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L285-L298) 找到三件事的先后：设 `textContentStorage` → `ensureLayout` → `drawView`。
2. 跟进 `drawView`（[SquirrelView.swift:118-127](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L118-L127)）确认它只是置 `needsDisplay`。
3. 跟进 `draw(_:)`（[SquirrelView.swift:130-177](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L130-L177)）里对 `convert(range:)` 与 `contentRect(range:)` 的调用，体会「绘制读几何、几何依赖布局」。

**需要观察的现象 / 预期结果**：

- 若 hypothetically 把 `ensureLayout` 那行注释掉（**仅思想实验，本讲禁止改源码**），`contentRect` 可能返回 `.zero` 或错误矩形，导致面板尺寸为 0、高亮画错位置。
- 这验证了「强制布局」是后续一切几何计算的前提。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `update` 里设完 `textContentStorage.attributedString` 后不能直接 `drawView`，而要先 `ensureLayout`？

**答案**：TextKit 2 布局是惰性的，设完文本时还没排好版。`drawView` 触发的 `draw(_:)` 要用 `enumerateTextSegments` 读候选/preedit 的矩形来画背景与高亮，几何信息只有在布局完成后才正确。不 `ensureLayout` 就读几何，会拿到 `.zero` 或错位矩形。

**练习 2**：`textContainer.size` 的宽度意味着什么？把它设得很大或很小分别会怎样？

**答案**：它是布局的换行约束宽度。设得很大，候选不换行、面板很宽；设得很小，候选提早换行、面板变窄变高。`maxTextWidth()`（[L347-358](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L347-L358)）根据屏幕与字体算出一个合理上限，避免面板占满屏幕。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「端到端」的源码追踪。

**任务**：给定一次输入的引擎返回值，完整描述 `SquirrelPanel.update` 如何把它变成屏幕上的候选面板。

设：`preedit = "ni"`，`selRange` 覆盖整个 `"ni"`，`candidates = ["你", "尼"]`，`comments = ["nǐ", "ní"]`，`labels = []`，`index = 0`，`page = 0`，`lastPage = false`，`update = true`，主题为默认（`candidate_format = "[label]. [candidate] [comment]"`，横排 `linear = true`）。

**要求你按顺序回答**：

1. **缓存**：哪些实例属性被刷新？（参考 [L154-164](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L154-L164)）
2. **拼 preedit 行**：`preeditAttrs` 与 `preeditHighlightedAttrs` 分别加在哪个区间？为什么 `selRange` 覆盖整个 `"ni"` 时高亮色会盖住普通色？（参考 [L188-204](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L188-L204)）
3. **第一个候选行（高亮）**：写出三步 `replaceOccurrences` 之后 `line.string` 的最终值；指出「你」字上携带哪些属性；说明为什么「你」会被加 `.noBreak`（`candidate.count = 1 ≤ 5`，且整行 `length ≤ 10`）。（参考 [L227-257](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L227-L257)）
4. **第二个候选行（非高亮）**：它用的是哪套属性？它与第一个候选行之间插入的分隔符是什么？（参考 [L208-210](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L208-L210) 与 [L259](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L259)）
5. **强制布局与绘制**：为什么 `ensureLayout` 必须在 `drawView` 之前？`candidateRanges` 里存了什么，供 `draw(_:)` 使用？（参考 [L281-298](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L281-L298)）

**参考答案要点**：

1. `preedit/selRange/caretPos/candidates/comments/labels/index/page/lastPage` 全部刷新；`cursorIndex = 0`。
2. `preeditAttrs` 加在整个 preedit 区间（`location:0, length: preedit.utf16.count`），`preeditHighlightedAttrs` 加在 `selRange`。`addAttributes` 对同键是覆盖，高亮段的 `.foregroundColor` 覆盖了普通段的，故整段显示高亮色。
3. 最终 `line.string` = `"1. 你 nǐ"`（label=`"1"`，candidate=`"你"`，comment=`"nǐ"`）。「你」携带 `highlightedAttrs`（候选色）与 `.noBreak`（因 count≤5，且整行 length=7≤10 也会再加一次整行 noBreak）。分隔符 `"  "`。
4. 第二个候选行用 `attrs`/`labelAttrs`/`commentAttrs`（非高亮）。横排下分隔符是两个空格 `"  "`。
5. TextKit 2 惰性布局，不 `ensureLayout` 则 `draw` 里 `enumerateTextSegments` 读不到正确矩形。`candidateRanges` 存了每个候选行在整段富文本中的 `NSRange`，`draw(_:)` 据此画出每行的背景与高亮矩形（详见 u4-l3）。

---

## 6. 本讲小结

- `SquirrelPanel.update` 是面板的唯一刷新入口，把 preedit 行 + 多个候选行**缝成单一 `NSMutableAttributedString`**，统一交给 TextKit 2 的 `textContentStorage`。
- 候选行由模板 `candidate_format`（默认 `[label]. [candidate] [comment]`）描述，替换分两步：**先对占位符区间加属性、再替换字面量**——顺序不可反，否则 `NSRange` 错位。
- 高亮行与非高亮行用不同的 `attrs` 字典（来自 `SquirrelTheme`），注释还支持插件驱动的 accent/warning 语义色。
- 短候选（`count ≤ 5`）与短整行（`length ≤ 10`）被加上自定义属性 `.noBreak`，配合 `SquirrelLayoutDelegate.shouldBreakLineBefore` 阻止 TextKit 在候选词内部断行。
- 拼完文本后必须**主动 `ensureLayout`**，否则后续的尺寸测量、高亮矩形、自绘都会因布局未完成而读到错误几何。
- 本讲只到「数据 → 富文本 → 触发布局」为止；面板如何定位（u4-l2）、如何自绘背景与高亮（u4-l3）、如何响应鼠标（u4-l4）在后续讲义展开。

---

## 7. 下一步学习建议

- **u4-l2 面板定位与全屏缩放**：精读 `SquirrelPanel.show`，看它如何在 `ensureLayout` 后量出面板自然尺寸、判断是否进入全屏缩放、并把面板钉在光标附近。注意 `show` 内部会**再次** `ensureLayout`。
- **u4-l3 SquirrelView 自定义绘制**：精读 `SquirrelView.draw(_:)`，看本讲存进 `candidateRanges`/`preeditRange` 的区间，如何被转成 `NSTextRange`、进而画出背景、高亮、边框、翻页箭头。
- **u4-l4 面板鼠标与滚轮事件**：精读 `SquirrelPanel.sendEvent` 与 `SquirrelView.click`，看本讲缓存的 `candidates`/`preedit`/`caretPos` 如何在鼠标命中测试与选词回调中被复用。
- 复习依赖：若对 `attrs` 字典与 `candidateFormat` 归一化的来源不熟，回看 **u3-l3（主题加载）**；若对「何时走面板而非 inline」不熟，回看 **u2-l7（marked/commit 与 inline 策略）**。
