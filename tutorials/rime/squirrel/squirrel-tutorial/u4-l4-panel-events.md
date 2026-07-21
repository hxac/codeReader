# 面板鼠标与滚轮事件

## 1. 本讲目标

前三讲我们解决了「面板画什么」（u4-l1 富文本拼装）、「画在哪、多大」（u4-l2 定位与缩放）、「怎么把图形画出来」（u4-l3 自绘路径）。但一个只能看、不能点的候选面板是没有灵魂的——用户必须能用鼠标点选候选词、点击翻页箭头、点击预编辑区移动光标，还要能滚动滚轮翻页。

本讲要回答的核心问题是：**面板上的一个鼠标点击，如何变成一次候选词上屏？**

学完本讲你应当能够：

1. 说清 `SquirrelPanel.sendEvent(_:)` 如何按事件类型分发，以及为什么坐标要经过两段转换。
2. 复述 `SquirrelView.click(at:)` 的三层命中优先级（翻页箭头 → 面板外形 → 文字落点），以及它如何把一个屏幕点翻译成「第几个候选 / 预编辑第几个字符」。
3. 解释「鼠标按下记一次、鼠标抬起校验一次」这套 down/up 配对机制为何能防误触。
4. 看懂滚轮翻页如何区分触控板（带 phase）与老式鼠标（无 phase），以及为什么竖排模式要反转水平滚动方向。
5. 追踪一次点击从面板事件回调到 `inputController` 的 `selectCandidate/page/moveCaret`，最终回到 `rimeUpdate()` 刷新面板的完整闭环。

## 2. 前置知识

阅读本讲前，请确认你已建立以下认知（它们都来自前序讲义，本讲直接承接，不再重复）：

- **面板的数据模型**：`SquirrelPanel.update(...)` 是面板唯一的刷新入口，它把 preedit 行与多个候选行缝成单一富文本，并把每个候选的字符区间记入 `view.candidateRanges`，preedit 行的区间记入 `view.preeditRange`（见 u4-l1）。本讲的命中测试就是在这两张区间表上做的「落点查询」。
- **`view.shape` 的双重身份**：`SquirrelView.draw(_:)` 每帧重建图层树后，把「面板轮廓 + 翻页箭头」合并成一条 `CGPath` 写进 `view.shape.path`。它既是毛玻璃背景视图的遮罩，也是点击命中测试的「可见区域」（见 u4-l3）。本讲 `click(at:)` 正是用 `shape.path.contains(...)` 判断点击是否落在面板内。
- **`rimeUpdate()` 是刷新面板的统一出口**：任何改变输入态的操作（按键、选词、翻页、移光标）最终都会调用 `rimeUpdate()`，它从引擎取回新的 preedit/候选/页码，再走 `showPanel → panel.update`（见 u2-l6）。本讲讲的所有鼠标/滚轮回调，最终都汇入这条出口。
- **坐标系**：`SquirrelView` 重写了 `isFlipped = true`（见下文源码），即 y 轴向下增长，与 TextKit 文字排版坐标一致；而 `NSEvent.mouseLocation` 是屏幕坐标（y 轴向上）。本讲的坐标转换就是在两者之间搭桥。
- **weak client 与 `?=`**：`inputController` 持有目标应用文本框的弱引用，回调前需判空（见 u2-l3、u1-l5）。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们构成「事件入口—命中测试—回调驱动」的完整三角：

| 文件 | 角色 | 本讲关注的内容 |
|------|------|----------------|
| [sources/SquirrelPanel.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift) | 面板窗口（`NSPanel` 子类），事件入口 | `sendEvent(_:)` 按事件类型分发、坐标转换 `mousePosition()`、down/up 配对、滚轮累计 |
| [sources/SquirrelView.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift) | 自绘视图，命中测试 | `click(at:)` 把屏幕点翻译成 `(candidateIndex, preeditIndex, pagingUp)` |

回调的对端在输入控制器里，本讲只引用、不深读（其内部机制见 u2-l6）：

| 文件 | 角色 | 本讲关注的内容 |
|------|------|----------------|
| [sources/SquirrelInputController.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift) | 输入控制器 | `selectCandidate/page/moveCaret` 三个回调如何驱动 librime 并回到 `rimeUpdate()` |

> 说明：本讲引用的所有源码行号均基于当前 HEAD `2158538755542b11964655e2f9606ba4a066edfe`。

## 4. 核心概念与源码讲解

### 4.1 事件分发总入口：sendEvent 与坐标变换

#### 4.1.1 概念说明

`NSPanel` 继承自 `NSWindow`。Cocoa 规定：所有发往本窗口的事件，都会先经过 `NSWindow.sendEvent(_:)` 这个总入口，再分发给具体的子视图。Squirrel 重写了它，等于在事件到达子视图**之前**先拦下来，用一个 `switch event.type` 自己处理鼠标相关事件，最后再 `super.sendEvent(event)` 让默认流程继续。

这样做的原因是：面板里的「候选词、翻页箭头、预编辑区」并不是三个独立的 `NSView`，而是**同一个自绘视图 `SquirrelView` 里画出来的图形**。普通的事件分发（命中哪个 view 就给哪个 view）在这里用不上——必须自己根据「点击落在哪条 `CGPath` / 哪段文字区间」来决定语义。所以面板选择在窗口层统一拦截、统一判定。

#### 4.1.2 核心流程

`sendEvent` 处理的事件类型一览：

```
sendEvent(event):
  switch event.type:
    .leftMouseDown  → 记录按下时的命中（候选 index / 翻页方向），暂不触发
    .leftMouseUp    → 重新命中并校验，通过后才回调 inputController
    .mouseEntered   → 打开 mouseMoved 投递
    .mouseExited    → 关闭 mouseMoved 投递，还原高亮
    .mouseMoved     → 高亮跟随鼠标（仅改绘制，不改数据）
    .scrollWheel    → 累计滚动量，达阈值则翻页
    default         → 不处理
  super.sendEvent(event)   # 让默认分发继续
```

一个贯穿全讲的前提是**坐标统一**。鼠标事件给的是屏幕坐标，而命中测试要在视图的「y 轴向下」坐标系里做。`mousePosition()` 负责这两段转换：

```swift
func mousePosition() -> NSPoint {
  var point = NSEvent.mouseLocation          // ① 屏幕坐标（y 向上）
  point = self.convertPoint(fromScreen: point)  // ② 转成面板窗口坐标
  return view.convert(point, from: nil)       // ③ 转成 SquirrelView 坐标
}
```

`SquirrelView` 声明了 `isFlipped = true`，使第 ③ 步后 y 轴向下，与 TextKit 排版坐标一致，这样后面用 `textLayoutManager.textLayoutFragment(for: point)` 查文字落点才正确。

#### 4.1.3 源码精读

事件总入口与分发骨架：[sources/SquirrelPanel.swift:67-143](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L67-L143)。注意第 142 行 `super.sendEvent(event)` 统一收口，无论上面哪个分支都要先把事件放行给默认流程。

面板持有的几个关键状态属性，是理解后续分支的前提：[sources/SquirrelPanel.swift:28-34](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L28-L34)。其中：

- `index: Int` —— 「已提交」的高亮候选（仅当 `update(update: true)` 时才刷新，即来自 `rimeUpdate` 的真实高亮）。
- `cursorIndex: Int` —— 「当前绘制」的高亮候选（每次 `update` 都刷新，mouseMoved 悬停时也跟着变）。
- `pagingUp: Bool?` —— 按下时命中的翻页箭头方向，`nil` 表示没命中箭头。

这两套 index（`index` vs `cursorIndex`）的区分是 down/up 防误触的核心，4.3、4.4 节会展开。

坐标转换两段式：[sources/SquirrelPanel.swift:331-335](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L331-L335)。

视图翻转声明：[sources/SquirrelView.swift:71-73](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L71-L73)。

#### 4.1.4 代码实践

**实践目标**：建立「事件类型 → 处理分支」的整体印象，并验证坐标系的翻转。

**操作步骤**：

1. 打开 [sources/SquirrelPanel.swift:68](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L68) 的 `sendEvent`，数一下 `switch` 一共处理了几种 `event.type`。
2. 对照 [sources/SquirrelView.swift:71-73](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L71-L73) 的 `isFlipped`，思考：如果把它改成 `false`，`click(at:)` 里用 `textLayoutManager.textLayoutFragment(for: point)` 查到的文字行会是哪一行？

**需要观察的现象 / 预期结果**：

- 共 6 种事件类型被处理：`leftMouseDown / leftMouseUp / mouseEntered / mouseExited / mouseMoved / scrollWheel`，其余落入 `default: break`。
- 若 `isFlipped` 为 `false`（y 向上），同一个鼠标点的 y 坐标会指向「视觉上的另一行」，命中测试整体错位——这解释了为什么翻转声明不能动。**待本地验证**（需要能编译运行 Squirrel 才能直观看到错位）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Squirrel 要重写 `NSPanel.sendEvent` 而不是给每个候选词各做一个 `NSButton`？

> **参考答案**：因为候选区、翻页箭头、预编辑区都是同一个 `SquirrelView` 用 Core Graphics 画出来的图形，并非独立子视图，Cocoa 默认的「按 view 命中」分发用不上；只能在窗口层统一拦截事件，再用路径/区间命中测试自己判定语义。

**练习 2**：`mousePosition()` 做了几次坐标转换？分别从哪个坐标系到哪个坐标系？

> **参考答案**：两次。第一次 `convertPoint(fromScreen:)` 从屏幕坐标（y 向上）转到面板窗口坐标；第二次 `view.convert(_:from:)` 转到 `SquirrelView` 自身坐标（因 `isFlipped`，y 向下，与文字排版一致）。

---

### 4.2 命中测试三层优先级：click(at:)

#### 4.2.1 概念说明

`SquirrelView.click(at:)` 是整个鼠标交互的「翻译器」：输入一个视图坐标的点，输出一个三元组 `(candidateIndex, preeditIndex, pagingUp)`，告诉调用方「这个点点什么了」：

- `candidateIndex: Int?` —— 命中第几个候选（从 0 开始），未命中候选为 `nil`。
- `preeditIndex: Int?` —— 命中预编辑区第几个字符（用于移动光标），未命中为 `nil`。
- `pagingUp: Bool?` —— 命中翻页箭头时，`true` 表上页箭头、`false` 表下页箭头；未命中箭头为 `nil`。

它的判定有**严格的优先级**：翻页箭头 > 面板外形 > 文字落点。这个顺序不能乱，因为箭头画在面板轮廓内部，若先判面板就会把箭头点击误判成候选。

#### 4.2.2 核心流程

```
click(at: point) -> (candidateIndex, preeditIndex, pagingUp):
  ① 若 point 在 downPath(下页三角) 内   → return (nil, nil, false)
  ② 若 point 在 upPath(上页三角) 内     → return (nil, nil, true)
  ③ 若 point 在 shape.path(面板轮廓) 内:
       扣除 textContainerInset 与 pagingOffset 得到文字坐标
       用 TextKit2 查 point 所在的 textLayoutFragment
       在 fragment 内逐行细化，得到精确字符 index
       若 index 落在 preeditRange   → preeditIndex = index
       否则遍历 candidateRanges 命中 → candidateIndex = i
  ④ 否则（点在面板外）→ return (nil, nil, nil)
```

#### 4.2.3 源码精读

完整命中测试：[sources/SquirrelView.swift:303-341](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L303-L341)。

三层优先级的「短路返回」：[sources/SquirrelView.swift:307-313](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L307-L313)。注意 `downPath`/`upPath` 这两个 `CGPath?` 是 `draw(_:)` 在每帧绘制翻页箭头时顺手存下的副本（见 [sources/SquirrelView.swift:37-38](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L37-L38) 与 [sources/SquirrelView.swift:291-298](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L291-L298)）。`false` 对应下页箭头、`true` 对应上页箭头——这与 `pagingLayer` 里 `canPageDown` 画 `downPath`、`canPageUp` 画 `upPath` 一一对应（[sources/SquirrelView.swift:740-753](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L740-L753)）。

文字落点的定位（最精妙的部分）：[sources/SquirrelView.swift:313-339](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L313-L339)。它做了三级「由粗到细」的定位：

1. **扣偏移**：先减去 `textContainerInset`（文字内边距）和 `pagingOffset`（留给翻页箭头的列宽），得到纯文字区坐标。
2. **定位文字片段**：`textLayoutManager.textLayoutFragment(for: point)` 找到这个点落在哪个排版片段（`NSTextLayoutFragment`），并取该片段起始的字符偏移作为基准 `index`。
3. **逐行细化**：在该片段的若干 `textLineFragments` 中，找到 `typographicBounds.contains(point)` 的那一行，再用 `lineFragment.characterIndex(for:)` 得到行内精确字符偏移，累加到 `index`。

最后用这个 `index` 做分类：落在 `preeditRange` 内是预编辑点击，否则遍历 `candidateRanges` 看落在哪个候选区间里（[sources/SquirrelView.swift:325-335](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L325-L335)）。这里用的 `candidateRanges` 与 `preeditRange` 正是 u4-l1 里 `update(...)` 拼富文本时记录的区间表，命中测试与绘制共用同一套坐标数据。

#### 4.2.4 代码实践

**实践目标**：理解「路径命中」与「文字区间命中」两种判定如何分工。

**操作步骤**：

1. 阅读 [sources/SquirrelView.swift:723-755](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L723-L755) 的 `pagingLayer(theme:preeditRect:)`，确认 `downPath`/`upPath` 是两个三角形 `CGPath`，且只有 `canPageDown`/`canPageUp` 为真时才会生成。
2. 回到 `click(at:)`，追踪一个落在「下页三角」上的点：它在第 307 行就 `return (nil, nil, false)`，根本不会进入第 313 行的文字定位。

**需要观察的现象 / 预期结果**：

- 翻页箭头用 `CGPath.contains(_:)` 判定（几何命中），候选/预编辑用 TextKit 字符区间判定（文字命中），两套机制在优先级里拼接。
- 当候选只有一页时（`canPageUp == canPageDown == false`），`pagingLayer` 返回 `(layer, nil, nil)`，于是 `click(at:)` 的 ①② 两步都因为 `downPath`/`upPath` 为 `nil` 而跳过，所有面板内点击都走文字定位——此时面板上没有箭头可点，行为正确。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `click(at:)` 要先判 `downPath`/`upPath`，再判 `shape.path`？

> **参考答案**：翻页三角形画在面板轮廓内部，若先判 `shape.path`（面板外形）会先命中，把箭头点击误带入文字定位流程；必须让面积更小、语义更具体的箭头路径优先短路返回。

**练习 2**：`click(at:)` 返回的三元组里，第三个值 `pagingUp` 为 `nil` 代表什么？

> **参考答案**：代表这次点击没有命中任何翻页箭头（既不是上页也不是下页），调用方据此把 `self.pagingUp` 清空。

---

### 4.3 候选点击的 down/up 配对防误触

#### 4.3.1 概念说明

一个朴素的设计是「鼠标点中候选就立刻选词」。但这会有误触问题：用户本想点候选 A，按下后手抖了一下拖到候选 B 再抬起，到底该选 A 还是 B？或者用户按下后改变主意，把鼠标移出面板再抬起，这次点击应该作废。

Squirrel 的解法是经典的「**按下记录、抬起校验**」配对：

- **`.leftMouseDown`**：只把命中结果**记下来**（候选记进 `self.index`，箭头方向记进 `self.pagingUp`），**不触发任何回调**。
- **`.leftMouseUp`**：重新命中同一个点，只有当「抬起的命中 == 按下时记下的命中」时，才真正触发回调。

这等于要求「按下和抬起必须落在同一个候选/同一个箭头上」，中间的拖拽被容忍掉，跨目标的拖拽则作废。

#### 4.3.2 核心流程

```
leftMouseDown:
  (index, _, pagingUp) = click(at: mousePosition())
  if let pagingUp:    self.pagingUp = pagingUp     # 记住按下了哪个箭头
  else:               self.pagingUp = nil
  if index 合法:       self.index = index            # 记住按下了哪个候选（不触发）

leftMouseUp:
  (index, preeditIndex, pagingUp) = click(at: mousePosition())
  ① 翻页：if pagingUp != nil 且 pagingUp == self.pagingUp:
            inputController.page(up: pagingUp)      # 同一箭头按下又抬起 → 翻页
          else: self.pagingUp = nil                  # 跨箭头或移出 → 作废
  ② 移光标：if preeditIndex 合法:
            根据 preeditIndex 在 caretPos 的左/右，调 moveCaret(forward:)
  ③ 选词：if index != nil 且 index == self.index:    # 同一候选按下又抬起 → 选词
            inputController.selectCandidate(index)
```

注意三件事：①②③ 在同一次抬起里**都可能执行**（不过实际中一个点通常只命中一类）；三者都用 `==` 比对「按下记录」来决定是否放行。

#### 4.3.3 源码精读

按下分支（只记录不触发）：[sources/SquirrelPanel.swift:70-79](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L70-L79)。第 77-79 行的 `if let index, index >= 0 && index < candidates.count` 是越界守卫——`click(at:)` 返回的 index 已经过区间过滤，这里再保险一次。

抬起分支（校验后触发）：[sources/SquirrelPanel.swift:80-97](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L80-L97)。三段判定清晰可读：

- 翻页校验在第 83 行 `pagingUp == self.pagingUp`——抬起命中的箭头必须与按下时记录的相同。
- 选词校验在第 95 行 `index == self.index`——抬起命中的候选必须与按下时记录的相同。

这套「双 index」机制之所以能工作，依赖 `update(...)` 里对两个变量的差异化更新：[sources/SquirrelPanel.swift:153-165](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L153-L165)。第 161 行 `self.index = index` 只在 `update == true`（即来自 `rimeUpdate` 的真实刷新）时执行；第 165 行 `cursorIndex = index` 则每次都执行。也就是说：

- `self.index` 是「**引擎认可的真实高亮**」，只有重绘全量数据时才动，按下时被临时借用为「按下记录」。
- `cursorIndex` 是「**当前画面上的高亮**」，悬停时也会跟着动（见 4.4 节）。

#### 4.3.4 代码实践

**实践目标**：亲身体验「按下记录、抬起校验」的防误触效果。

**操作步骤**（源码阅读型实践，配合本地运行观察）：

1. 在候选面板上把鼠标移到第 2 个候选（不要按下），观察高亮是否跟着鼠标跳到第 2 个（这是 4.4 节的 mouseMoved 在起作用）。
2. 在第 1 个候选上**按下**鼠标左键，**保持按住**拖到第 3 个候选，再**抬起**。
3. 预测：这次会选中哪个候选？为什么？

**需要观察的现象 / 预期结果**：

- 步骤 2 中，按下时 `self.index` 被记为 0（第 1 个），抬起时 `click(at:)` 返回 `index = 2`（第 3 个）。第 95 行 `index == self.index` 即 `2 == 0` 为假，**不会选词**。
- 结论：跨候选的按下→拖动→抬起被判定为「取消」，不会误选。这正是 down/up 配对的价值。**待本地验证**（需运行 Squirrel 并呼出候选面板）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `leftMouseDown` 不直接调用 `selectCandidate`，而要先记进 `self.index`？

> **参考答案**：为了支持「按下后拖动可取消」。若按下即选词，用户手抖拖到别的候选就来不及反悔；记录后延迟到抬起时校验，能容忍拖拽、丢弃跨目标的误触。

**练习 2**：第 95 行的 `index == self.index` 如果误写成 `index != self.index`，会出现什么行为？

> **参考答案**：会变成「抬起命中的候选必须与按下时不同」才选词，即正常点选（按下抬起同一个）失效，反而只有拖到别的候选才选——完全背离预期，可见这个 `==` 校验是正确性的关键。

---

### 4.4 mouseMoved 高亮跟随与 mouseExited 还原

#### 4.4.1 概念说明

down/up 配对解决了「选词的确定性」，但还有个体验问题：用户在面板上移动鼠标时，希望高亮**实时跟着鼠标**走（悬停预览），而不用等到按下。这由 `.mouseMoved` 负责。

但鼠标移动事件很频繁，若每次都重画整个面板（重排富文本、强制布局）会很卡。Squirrel 的优化是：mouseMoved 调 `update(...)` 时传 `update: false`——**只改高亮、不改数据**，复用已缓存的 preedit/候选，仅重画高亮矩形。

另一个细节：鼠标离开面板时，高亮应当**还原**回引擎认可的真实高亮（`self.index`），而不是停留在最后一次悬停的候选上。这由 `.mouseExited` 负责。

#### 4.4.2 核心流程

```
mouseEntered:  acceptsMouseMovedEvents = true     # 进面板，开 mouseMoved 投递
mouseExited:   acceptsMouseMovedEvents = false    # 离面板，关投递
               if cursorIndex != index:           # 若画面高亮 != 真实高亮
                 update(... highlighted: index, update: false)  # 还原成 self.index
               pagingUp = nil

mouseMoved:    (index, _, _) = click(at: mousePosition())
               if index 合法 且 cursorIndex != index:           # 悬停目标变了
                 update(... highlighted: index, update: false)  # 只改高亮
```

`acceptsMouseMovedEvents` 是 `NSWindow` 的属性：为 `true` 时窗口才接收 `.mouseMoved`。面板只在鼠标进入时打开、离开时关闭，避免鼠标在面板外时还白白收到一堆移动事件。

#### 4.4.3 源码精读

进入/离开分支：[sources/SquirrelPanel.swift:98-105](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L98-L105)。第 102-104 行的还原逻辑：`cursorIndex`（画面高亮）若因悬停被挪到了别处，离开时用 `highlighted: index`（即 `self.index`，真实高亮）重画一次 `update: false`，把画面拉回真实状态。

悬停跟随分支：[sources/SquirrelPanel.swift:106-110](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L106-L110)。关键在第 108 行的 `cursorIndex != index`——只有当悬停目标与当前画面高亮**不同**时才重画，避免鼠标在同一候选内微动时反复触发重绘。

两次 `update(... update: false)` 都只传 `highlighted` 不同，数据参数（preedit/candidates/...）全部沿用实例缓存，这正是 u4-l1 讲过的「`update` 布尔参数控制是否刷新缓存」的用武之地：传 `false` 时 [sources/SquirrelPanel.swift:154-164](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L154-L164) 的缓存块整段跳过，但第 165 行 `cursorIndex = index` 仍会执行，于是画面高亮更新、数据不变。

#### 4.4.4 代码实践

**实践目标**：区分「悬停高亮（临时）」与「真实高亮（引擎认可）」两套状态。

**操作步骤**：

1. 呼出候选面板（例如输入拼音出现多个候选），默认真实高亮在第 1 个候选（`self.index == 0`）。
2. 把鼠标移到第 3 个候选上悬停（不点击），观察高亮跳到第 3 个——此时 `cursorIndex == 2`，`self.index` 仍为 0。
3. 把鼠标移出面板，观察高亮是否跳回第 1 个。
4. 查 [sources/SquirrelPanel.swift:102-104](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L102-L104)，确认步骤 3 的还原正是这段代码触发的。

**需要观察的现象 / 预期结果**：

- 悬停时高亮实时跟随，移出后自动还原到真实高亮。两套 index 各司其职：`cursorIndex` 管画面、`self.index` 管真相。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：mouseMoved 调 `update(...)` 时为什么必须传 `update: false`？

> **参考答案**：mouseMoved 触发极频繁，若传 `true` 会每次重排富文本、强制 TextKit 布局，造成卡顿；传 `false` 只刷新 `cursorIndex` 并重画高亮矩形，复用已缓存数据，开销很小。

**练习 2**：如果删掉 `.mouseExited` 里第 102-104 行的还原逻辑，会有什么体验问题？

> **参考答案**：鼠标悬停把高亮挪到第 3 个候选后离开面板，画面会一直停在第 3 个，与引擎真实高亮（第 1 个）不一致；下次按键刷新前，用户看到的「当前候选」是错的。

---

### 4.5 翻页：箭头点击与滚轮

#### 4.5.1 概念说明

翻页有两条路径，都汇入同一个回调 `inputController.page(up:)`：

1. **点击翻页箭头**：u4-l3 讲过，`draw(_:)` 在面板右侧画了上下两个三角形（仅当有上/下页时）。点中它们即翻页，走的是 4.2、4.3 节的 down/up 配对。
2. **滚动滚轮/触控板**：在面板上两指滑动或滚动鼠标滚轮。这是本节的重点，因为它要同时处理两种差别很大的输入设备。

**触控板 vs 老式鼠标滚轮的本质区别**：

- 触控板手势带**相位（phase）**：`.began`（手指按下）→ 多次 `.changed`（滑动中）→ `.ended`（手指抬起），抬开后还可能进入 `.momentum`（惯性）阶段，`event.momentumPhase` 非 0。
- 老式鼠标滚轮**没有相位**：`event.phase` 与 `event.momentumPhase` 都是 0（`init(rawValue: 0)`），它只会在滚动时一次性投递带 `scrollingDeltaX/Y` 的事件。

Squirrel 用 `event.phase` 与 `event.momentumPhase` 是否为 0 把两者分流到不同的累计逻辑。

#### 4.5.2 核心流程

```
scrollWheel(event):
  if phase == .began:                          # 触控板手势开始
    scrollDirection = .zero                    # 清空累计向量
  else if phase == .ended 或 (phase==0 且 momentum != 0):   # 手势结束/惯性中
    若 |dx| > |dy| 且 |dx| > 10:   page(up: (dx<0) == vertical)   # 水平滑动主导
    否则若 |dy| > |dx| 且 |dy| > 10: page(up: dy > 0)            # 垂直滑动主导
    scrollDirection = .zero
  else if phase == 0 且 momentum == 0:         # 老式鼠标滚轮
    若闲置超 1 秒: scrollDirection = .zero     # 防止跨次滚动累加
    累加 dy（方向反转时清零重计）
    if |dy| > 10: page(up: dy > 0); scrollDirection = .zero
  else:                                        # .changed 等中间帧
    scrollDirection.dx += scrollingDeltaX      # 持续累计
    scrollDirection.dy += scrollingDeltaY
```

几个关键设计：

- **阈值 10**：累计量超过 10 才翻页，过滤手抖和微小滑动。
- **主轴判定**：手势结束时比较 `|dx|` 与 `|dy|`，谁大就以谁的方向为准，避免斜向滑动误判。
- **竖排反转**：水平滑动主导时，翻页方向用 `(dx < 0) == vertical` 计算——`vertical` 为真（竖排）时方向取反。这是因为竖排面板整体旋转了 −90°，用户视觉上的「上下」对应滚轮的「左右」。
- **老式鼠标的方向反转清零**：[sources/SquirrelPanel.swift:126-130](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L126-L130) 中，若新事件的 `scrollingDeltaY` 与累计方向相反，就把累计清零重计，避免来回滚动互相抵消却仍触发翻页。

#### 4.5.3 源码精读

箭头点击翻页（走 down/up 配对）：[sources/SquirrelPanel.swift:80-87](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L80-L87)。按下时 `self.pagingUp` 被记下，抬起时第 83 行校验 `pagingUp == self.pagingUp` 通过才调 `inputController.page(up:)`。

滚轮分支完整逻辑：[sources/SquirrelPanel.swift:111-138](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L111-L138)。注意四个分支的判别条件互斥：

- 第 112 行 `.began`：仅触控板。
- 第 114 行 `.ended` 或「无 phase 但有 momentum」：手势结束或惯性阶段。
- 第 121 行「无 phase 且无 momentum」：仅老式鼠标。
- 第 135 行 `else`：`.changed` 等中间帧（仅触控板）。

竖排方向反转在第 116 行 `page(up: (scrollDirection.dx < 0) == vertical)`，`vertical` 来自 [sources/SquirrelPanel.swift:57-59](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L57-L59)（读取当前主题的 `vertical` 属性）。累计向量与时间戳的状态声明在 [sources/SquirrelPanel.swift:30-31](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L30-L31)。

#### 4.5.4 代码实践

**实践目标**：验证竖排模式下水平滚动方向被反转。

**操作步骤**：

1. 在 `data/squirrel.yaml`（或 `~/Library/Rime/squirrel.yaml`）的 `style` 节把 `candidate_list_layout` 设为 `vertical`（或 `vertical_text: true`，具体键名见 u3-l2/u3-l3），重新部署使面板竖排。
2. 在面板上用触控板**水平**两指滑动，观察翻页方向。
3. 把布局改回横排，重复水平滑动，对比翻页方向是否相反。
4. 对照 [sources/SquirrelPanel.swift:115-116](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L115-L116)，确认方向差异来自 `== vertical` 这个异或式取反。

**需要观察的现象 / 预期结果**：

- 同一个水平滑动方向，横排与竖排下触发相反的翻页方向（一个上一页、一个下一页）。这是因为竖排面板旋转后，用户的「上下翻页」直觉对应滚轮的左右轴。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：滚轮翻页为什么用阈值 10，而不是滚动一丝就翻一页？

> **参考答案**：触控板和滚轮都会有微小抖动与噪声，阈值 10 起到低通滤波作用，只有「明确、持续」的滚动意图才触发翻页，避免误触。

**练习 2**：老式鼠标滚轮分支里，第 122 行 `if scrollTime.timeIntervalSinceNow < -1` 的作用是什么？

> **参考答案**：老式鼠标没有 `.began/.ended` 相位，无法靠相位清零累计；这里用「距上次滚动超过 1 秒」判定为「一次新的滚动开始」，主动清零 `scrollDirection`，防止间隔较久的两次滚动被错误累加。

---

### 4.6 回调驱动 librime：selectCandidate / page / moveCaret

#### 4.6.1 概念说明

前几节的所有判定，最终都汇成对 `inputController` 的三个回调之一。`inputController` 是面板持有的弱引用（[sources/SquirrelPanel.swift:13](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L13)），指向当前输入控制器 `SquirrelInputController`。三个回调各自封装一次 librime 调用，并在成功后统一调 `rimeUpdate()` 把引擎新状态取回前端、刷新面板，形成闭环。

| 回调 | 触发场景 | librime 调用 | 语义 |
|------|----------|--------------|------|
| `selectCandidate(_ index:)` | 点击候选 | `select_candidate_on_current_page` | 选中当前页第 index 个候选，通常立即上屏 |
| `page(up:)` | 点箭头 / 滚轮 | `change_page` | 翻到上一页（up=true）或下一页（up=false） |
| `moveCaret(forward:)` | 点预编辑区 | `get_caret_pos` / `set_caret_pos` | 把编辑光标左移（forward=true）或右移 |

#### 4.6.2 核心流程（以选词为例的完整闭环）

```
用户点击候选 A
  → sendEvent(.leftMouseDown) 记录 self.index = A
  → sendEvent(.leftMouseUp)   校验 index == self.index 通过
  → inputController.selectCandidate(A)
      → rimeAPI.select_candidate_on_current_page(session, A)   # 告诉引擎选了哪个
      → rimeUpdate()                                            # 取回引擎结果
          → get_commit / get_status / get_context (见 u2-l6)
          → showPanel(...) → panel.update(... update: true)     # 全量刷新面板
              → 候选已上屏，新候选页/空面板绘制出来
```

注意 `selectCandidate` 的返回值 `Bool` 表示 librime 是否接受这次选择，只有 `success` 时才调 `rimeUpdate()`——选词失败（如 index 越界）就不刷新，避免无谓重绘。

`moveCaret` 稍特殊：它先用 `get_caret_pos` 拿当前光标、`get_input` 拿原始编码串，做边界检查（不能移到串外），再用 `set_caret_pos` 写回新位置，最后 `rimeUpdate()`。边界检查失败直接 `return false` 不动引擎。

#### 4.6.3 源码精读

三个回调的实现：[sources/SquirrelInputController.swift:126-161](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L126-L161)。

- `selectCandidate` 第 127-130 行：调 `select_candidate_on_current_page`，成功才 `rimeUpdate()`。
- `page` 第 137-140 行：调 `change_page(session, up)`，`up` 即「是否上一页」。
- `moveCaret` 第 145-160 行：注意第 148-150 行 `forward`（前进=向左）时 `currentCaretPos <= 0` 直接返回，第 154-156 行非前进（向右）时超过串长也返回，这是光标边界保护。

`rimeUpdate()` 是这条链的终点（定义在 [sources/SquirrelInputController.swift:437](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L437)，内部机制见 u2-l6），它最终调用 `showPanel(...)`（[sources/SquirrelInputController.swift:581-591](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L581-L591)）。注意第 587 行 `panel.inputController = self`——每次 `showPanel` 都把控制器重新挂回面板，这就是面板能回调到正确控制器的由来（面板的 `inputController` 是弱引用，控制器切会话时需要重挂）。

#### 4.6.4 代码实践

**实践目标**：追踪一次候选点击的完整闭环，把本讲所有模块串起来。

**操作步骤**：

1. 从 [sources/SquirrelPanel.swift:70](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L70)（leftMouseDown）开始，顺着 `view.click(at:)` → `self.index = ...` 记录按下。
2. 跳到 [sources/SquirrelPanel.swift:80](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L80)（leftMouseUp），看第 95 行校验通过后调 `inputController.selectCandidate(index)`。
3. 进 [sources/SquirrelInputController.swift:126](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L126)，看 `select_candidate_on_current_page` → `rimeUpdate()`。
4. `rimeUpdate()` 内部最终到 [sources/SquirrelInputController.swift:581](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L581) 的 `showPanel` → `panel.update(... update: true)`，面板用新数据重绘。

**需要观察的现象 / 预期结果**：

- 一条完整链路：`sendEvent(.leftMouseDown)` 记录 → `sendEvent(.leftMouseUp)` 校验 → `selectCandidate` → `select_candidate_on_current_page` → `rimeUpdate` → `showPanel` → `panel.update`。面板与引擎在这条链里完成「用户意图 → 引擎处理 → 前端重绘」的一次回合。

**关于 down/up 分开记录/校验的总结**：候选与翻页都要求「按下和抬起命中同一个目标」才生效。分开两步是为了**容忍拖拽、丢弃误触**——按下只记账不触发，抬起比对账本一致才放行。若合并在一步（抬起时直接选词），就无法区分「稳稳的点选」与「按下后拖到别处再抬起」两种意图。

#### 4.6.5 小练习与答案

**练习 1**：`selectCandidate` 为什么在 `success == false` 时不调 `rimeUpdate()`？

> **参考答案**：选词失败意味着引擎状态没变（如 index 越界），此时调 `rimeUpdate()` 只是白白重绘同一画面；跳过它既省开销，也避免不必要的面板闪烁。

**练习 2**：面板的 `inputController` 是 `weak`，那它什么时候会被重新赋值，保证回调能找到正确的控制器？

> **参考答案**：每次 `showPanel(...)`（[sources/SquirrelInputController.swift:587](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L587)）都会执行 `panel.inputController = self`。因为面板是 App 级单例、控制器是会话级对象（见 u2-l1），切会话/重显面板时必须把当前控制器重新挂回，回调才能命中正确的 session。

---

## 5. 综合实践

**任务**：画一张「面板鼠标交互全链路」时序图，并回答三个综合问题。

请准备纸笔（或任意画图工具），画出下列参与者的交互时序：`用户`、`SquirrelPanel.sendEvent`、`SquirrelView.click(at:)`、`SquirrelInputController`、`librime(rimeAPI)`、`SquirrelPanel.update`。然后在这张图上分别标注三条路径：

1. **悬停高亮**：用户移动鼠标 → `mouseMoved` → `click(at:)` 命中候选 2 → `update(update: false)` 仅改高亮（不经过 librime）。
2. **选词**：用户在候选 2 上按下抬起 → `leftMouseDown` 记录 `self.index=2` → `leftMouseUp` 校验 → `selectCandidate(2)` → `select_candidate_on_current_page` → `rimeUpdate` → `update(update: true)` 全量刷新。
3. **滚轮翻页**：用户在面板上滚动 → 多次 `scrollWheel(.changed)` 累计 → `scrollWheel(.ended)` 达阈值 → `page(up:)` → `change_page` → `rimeUpdate` → 全量刷新。

画完后回答：

- 路径 1 为什么不碰 librime，而路径 2、3 必须碰？
- 三条路径里，哪两条最终都汇入 `rimeUpdate()`？哪一条例外？为什么这条例外是合理的？
- 如果把 `click(at:)` 的三层优先级打乱（先判 `shape.path` 再判箭头），路径 2 在「点翻页箭头」时会发生什么错误？

> **参考要点**：① 悬停只是改前端绘制高亮，不改变引擎状态，故无需碰 librime；选词/翻页要改变引擎状态（选中候选、切换页码），必须经 librime。② 路径 2、3 汇入 `rimeUpdate()`，路径 1 例外——因为它纯粹是视图层重绘（`update: false`），不应触发昂贵的引擎往返，这正是 `update` 布尔参数设计的意义。③ 若先判 `shape.path`，点翻页箭头会被 `shape.path.contains` 先命中而进入文字定位流程，可能把箭头位置误判成某个候选或预编辑区，导致点箭头却选了词或移了光标。

## 6. 本讲小结

- `SquirrelPanel.sendEvent(_:)` 是面板事件总入口，按 `event.type` 分发六类鼠标/滚轮事件，统一在末尾 `super.sendEvent` 放行；所有命中都基于 `mousePosition()` 把屏幕坐标两段转换成视图坐标（`isFlipped` 使 y 向下，贴合 TextKit）。
- `SquirrelView.click(at:)` 用三层优先级把一个点翻译成 `(candidateIndex, preeditIndex, pagingUp)`：翻页箭头（`downPath`/`upPath`）→ 面板外形（`shape.path`）→ TextKit 文字落点（`textLayoutFragment` + `characterIndex`）。
- 候选点击采用「**down 记录、up 校验**」配对：按下只把命中写进 `self.index`/`self.pagingUp`，抬起比对一致才触发回调，从而容忍拖拽、丢弃跨目标的误触。
- `mouseMoved` 让高亮实时跟随鼠标，但只调 `update(update: false)` 复用缓存、仅改高亮；`mouseExited` 把高亮还原回真实高亮 `self.index`。两套 index（`cursorIndex` 管画面 vs `self.index` 管真相）支撑这套行为。
- 滚轮翻页用 `event.phase`/`momentumPhase` 区分触控板（带相位，`.began` 清零、`.ended` 判主轴）与老式鼠标（无相位，靠 1 秒闲置清零）；阈值 10 滤抖动，竖排模式下水平方向用 `== vertical` 取反。
- 三类交互最终汇入 `inputController` 的 `selectCandidate/page/moveCaret`，它们各自封装一次 librime 调用，成功后统一 `rimeUpdate()` 取回新状态、全量刷新面板，形成「用户意图 → 引擎处理 → 前端重绘」闭环。

## 7. 下一步学习建议

本讲讲完了面板的「输入侧」（鼠标/滚轮如何驱动引擎）。至此第四单元「候选词面板 UI」全部结束。建议接下来：

1. **进入第五单元（系统集成与扩展）**：本讲的闭环依赖 `inputController` 与 `rimeUpdate()`，而 `rimeUpdate` 内部的 commit/status/context 三段式消费在 u2-l6 已讲过；第五单元会从「面板/UI」上升到「App 与 macOS 系统」的集成，建议从 u5-l1（分布式通知与外部命令）开始，看 `--reload/--getascii` 等命令如何跨进程驱动已运行的输入法实例。
2. **若对「引擎侧」仍想深挖**：可回头重读 u2-l6（rimeUpdate 数据流）与本讲的 4.6 节对照，体会「前端回调 → librime 调用 → 前端刷新」这一来一回在两个文件里是如何对接的。
3. **动手扩展（进阶）**：试着在 `sendEvent` 的 `scrollWheel` 分支加一行日志（打印 `event.phase`、`scrollingDeltaY` 与累计 `scrollDirection.dy`），本地运行后用触控板和鼠标滚轮分别滚动，观察两类设备的事件序列差异——这是验证本讲「设备分流」设计最直观的方式。注意：修改源码仅用于本地学习观察，勿提交。
