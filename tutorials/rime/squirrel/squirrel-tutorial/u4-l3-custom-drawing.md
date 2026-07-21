# SquirrelView 自定义绘制

## 1. 本讲目标

本讲是「专家：候选词面板 UI」单元的第三讲。上一讲（u4-l1）我们追踪到 `SquirrelPanel.update` 把候选与 preedit 缝成一段富文本、强制布局、最后调用 `view.drawView(...)` 把「哪些字符区间是候选、哪一行高亮」登记进 `SquirrelView`，并置 `needsDisplay = true`。本讲要回答的核心问题是：

> 当 `needsDisplay` 触发下一帧的 `draw(_:)` 时，Squirrel 是怎么把屏幕上那块带圆角背景、带候选分块底色、带高亮、带边框、带翻页箭头的面板「画」出来的？文字以外的所有图形从哪里来？

学完本讲，你应当能够：

1. 说清 `SquirrelView.draw(_:)` 的整体结构——它把绘制拆成 **background / preedit / candidate / highlighted / highlightedPreedit** 五类路径，按固定顺序先生成路径、再重建图层树。
2. 复述「多层 `CAShapeLayer` + `fillColor`」的图层模型：每一类区域对应一个 shape layer，颜色全部来自 `SquirrelTheme`，并理解 `panelLayer` 与其 `mask` 的关系。
3. 解释 `mutual_exclusive` 选项如何利用 **even-odd 填充规则**在背景上「挖洞」，让每块区域只显示一种颜色、避免叠加混色。
4. 说清翻页箭头三角形如何生成，以及 `view.shape` 这一个 `CAShapeLayer` 为何能**同时**充当半透明背景视图的遮罩与鼠标命中测试区。

本讲聚焦「自绘图形」；面板如何定位（u4-l2）、鼠标/滚轮事件如何回调（u4-l4）在相邻讲义展开。

---

## 2. 前置知识

本讲假设你已经掌握以下概念（来自前置讲义）：

- **`SquirrelPanel.update` 与富文本拼装**（u4-l1）：面板把候选/preedit 缝成单一 `NSMutableAttributedString`，每行的 `NSRange` 存进 `view.candidateRanges`，`preeditRange`、`highlightedPreeditRange`（即 `selRange`）、`hilightedIndex` 一并交给 `drawView`。本讲直接消费这些区间。
- **主题对象 `SquirrelTheme`**（u3-l3）：主题把 YAML 读出的值组装成一组颜色/几何属性。本讲会用到的颜色字段（`backgroundColor`、`highlightedBackColor`、`candidateBackColor`、`preeditBackgroundColor`、`highlightedPreeditColor`、`borderColor`）与几何字段（`cornerRadius`、`hilitedCornerRadius`、`pagingOffset`、`mutualExclusive`、`shadowSize`、`borderLineWidth`）全部来自主题。
- **TextKit 2 布局**（u4-l1 §4.4）：`draw` 里要把 `NSRange` 转成 `NSTextRange`、再用 `enumerateTextSegments` 读出某段文本在屏幕上的矩形——这一切都依赖 `update` 里已经 `ensureLayout` 过。

此外需要一点 Core Animation 与 Core Graphics 的常识：

- **`CALayer` / `CAShapeLayer`**：`CALayer` 是 AppKit/UIKit 上一块可硬件加速的位面。`CAShapeLayer` 是其子类，按一个 `CGPath`（矢量路径）来填充（`fillColor`）或描边（`strokeColor`）。把它挂到某个父 layer 的 `sublayers` 上即可显示。
- **`CGPath` 与填充规则（fill rule）**：`CGPath` 是一组直线/曲线构成的矢量图形。填充规则决定「某点是否被涂色」：`.evenOdd` 规定从该点向外引射线，穿越路径的次数为**奇数**才涂色。两个重叠的闭合子路径会让重叠区「穿越两次 = 偶数 = 不涂色」——这正是「挖洞」的原理。
- **`isFlipped`**：`NSView` 默认坐标系原点在左下、y 向上；重写 `isFlipped` 返回 `true` 后，原点变左上、y 向下（与文字阅读方向一致）。`SquirrelView.isFlipped` 为 `true`，这会牵连到图层坐标与命中测试坐标的换算（见 §4.4）。

> 本讲引用的永久链接基址为：
> `https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/`

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到什么 |
| --- | --- | --- |
| `sources/SquirrelView.swift` | 面板的内容视图，承载 `NSTextView` 并**自绘**面板底板 | `draw(_:)` 的路径组织与图层树、`shapeFromPath` 工厂、`pagingLayer` 生成箭头、`click(at:)` 命中测试、`shape` 属性的双重用途 |
| `sources/SquirrelPanel.swift` | 面板窗口，持有 `view` 与半透明背景视图 `back` | 初始化里 `back.layer?.mask = view.shape`、`sendEvent` 调 `view.click(at:)` |
| `sources/SquirrelTheme.swift` | 主题对象 | 各颜色字段（`backgroundColor` 等）与 `mutualExclusive`/`pagingOffset` 的来源（YAML 键名映射） |

读者可把这三个文件在编辑器里并排打开，边读讲义边对照。

---

## 4. 核心概念与源码精读

### 4.1 draw 的整体结构与路径组织

#### 4.1.1 概念说明

先厘清一个常见误解：**`draw(_:)` 不画文字**。面板上的候选词、编号、注释这些文字，由 `NSTextView`（TextKit 2）自己渲染。`draw` 只负责画文字「底下和周围」的那块底板：

- 面板整体的圆角背景；
- preedit 行的底色；
- 每个非高亮候选的分块底色；
- 当前高亮候选的底色（可能带阴影）；
- preedit 中已转换段的高亮底色；
- 面板边框；
- 左侧翻页列里的上下箭头。

这些图形都是**矢量路径**（`CGPath`），按区域可归为五类：`background`（整体背景）、`preedit`（preedit 底）、`candidate`（所有非高亮候选底，合并成一条路径）、`highlighted`（高亮候选底）、`highlightedPreedit`（preedit 内已转换段高亮）。`draw` 在开头就声明了这五个局部变量来持有它们：

```swift
override func draw(_ dirtyRect: NSRect) {
  var backgroundPath: CGPath?
  var preeditPath: CGPath?
  var candidatePaths: CGMutablePath?
  var highlightedPath: CGMutablePath?
  var highlightedPreeditPath: CGMutablePath?
  let theme = currentTheme
  ...
```

注意 `candidatePaths` 是 `CGMutablePath`——多个候选的底色会**合并进同一条路径**（用 `addPath` 累加），这样它们能共用一个 shape layer、同一种颜色。`highlightedPath` 只有一个（因为同时只有一行高亮）。

`draw` 的另一个重要设计是**每帧从零重建图层树**。它不维护增量状态，而是先 `self.layer?.sublayers = nil` 清空，再用本帧算出的路径重新生成所有 `CAShapeLayer` 挂回去。这换来的是「绘制逻辑无状态、好维护」，代价是每帧重建（但候选面板内容很少，开销可忽略）。

#### 4.1.2 核心流程

`draw(_:)`（[sources/SquirrelView.swift:130-301](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L130-L301)）整体可拆成两大阶段：

```
阶段一：算矩形、生成五类路径
  1. 取主题、算 backgroundRect（= bounds 宽度减去 pagingOffset 翻页列）
  2. 若有 preeditRange：算 preeditRect，按需生成 preeditPath
  3. 从候选区 containingRect 里抠出内边距（carveInset）
  4. 遍历 candidateRanges：
       i == hilightedIndex → 生成 highlightedPath（单个）
       否则                → 累加进 candidatePaths（合并）
  5. 若有 highlightedPreeditRange：生成 highlightedPreeditPath
  6. 最后生成最外层的 backgroundPath（整体圆角矩形）

阶段二：清空旧图层、按顺序挂新图层、组装命中路径
  7. self.layer?.sublayers = nil            # 清空
  8. 组装 backPath（见 4.3）→ panelLayer（背景）+ mask
  9. 依次挂：preedit 底层 / 边框 / preedit 高亮 / 候选底 / 高亮底(+阴影)
 10. panelLayer 平移 pagingOffset 给翻页列让位
 11. 调 pagingLayer 生成上下箭头三角形，挂到 self.layer
 12. 把 backgroundPath + 两个箭头路径合并成 panelPath，赋给 shape.path（见 4.4）
```

阶段一的关键是「**先把所有路径算好，再开始建图层**」。因为图层之间有父子、遮罩关系，路径齐了才能一次性搭起图层树。阶段二的挂载顺序就是视觉上的**从底到顶**叠加顺序（背景最先、高亮最后），这正是 4.2 要讲的颜色驱动表。

> 关于「算矩形」本身的几何细节（`preeditRect` 怎么留高度、`carveInset` 怎么按 `hilitedCornerRadius` 抠内边距、`drawPath` 怎么处理线性/堆叠与圆角），本讲只点到为止，重点放在「路径如何变成带颜色的图层」。几何测量的原理在 u4-l1（`contentRect`/`enumerateTextSegments`）与 u4-l2（自然尺寸）已有铺垫。

#### 4.1.3 源码精读

`draw` 的函数头与五类路径声明见 [sources/SquirrelView.swift:129-136](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L129-L136)。`currentTheme` 会按系统亮暗在 `lightTheme`/`darkTheme` 间二选一（[SquirrelView.swift:42-44](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L42-L44)），所以整段 `draw` 的颜色会自动跟随外观。

`backgroundRect` 的计算见 [sources/SquirrelView.swift:138-140](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L138-L140)：把 `bounds` 的宽度减去 `theme.pagingOffset`，也就是给左侧翻页列预留出空间，面板的「主背景」只占右边那一块：

```swift
var containingRect = self.bounds
containingRect.size.width -= theme.pagingOffset
let backgroundRect = containingRect
```

preedit 路径的生成见 [sources/SquirrelView.swift:142-156](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L142-L156)：只有 `preeditRange.length > 0`（有 preedit）且 `theme.preeditBackgroundColor != nil`（用户配了 preedit 底色）时才生成 `preeditPath`。注意 preedit 的矩形高度被刻意留了 `edgeInset` + `preeditLinespace` 的余量，并在没有候选时再补一段高度——这是为了让 preedit 底与候选底之间有一道视觉间距。

候选路径的循环是阶段一的核心，见 [sources/SquirrelView.swift:158-177](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L158-L177)：

```swift
containingRect = carveInset(rect: containingRect)   # 按 hilitedCornerRadius 抠内边距
for i in 0..<candidateRanges.count {
  let candidate = candidateRanges[i]
  if i == hilightedIndex {
    if candidate.length > 0 && theme.highlightedBackColor != nil {
      highlightedPath = drawPath(highlightedRange: candidate, ..., extraExpansion: 0)?.mutableCopy()
    }
  } else {
    if candidate.length > 0 && theme.candidateBackColor != nil {
      let candidatePath = drawPath(highlightedRange: candidate, ...,
                                   extraExpansion: theme.surroundingExtraExpansion)
      if candidatePaths == nil { candidatePaths = CGMutablePath() }
      if let candidatePath = candidatePath { candidatePaths?.addPath(candidatePath) }
    }
  }
}
```

两个细节值得注意：

- 高亮候选调 `drawPath` 时 `extraExpansion: 0`，而非高亮候选用 `theme.surroundingExtraExpansion`。这让高亮块可以比普通候选块**略大或略小**，形成「高亮行外扩」的视觉强调（由 `surrounding_extra_expansion` 配置）。
- `candidatePaths` 用 `addPath` 累加所有非高亮候选的路径——它们将共用同一个 shape layer 和同一种 `candidateBackColor`。

`drawPath(highlightedRange:...)`（[sources/SquirrelView.swift:624-706](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L624-L706)）是把一个 `NSRange` 变成圆角矩形路径的「重型机械」：它会按 `theme.linear`（横排/堆叠）走两条分支——线性模式下用 `multilineRects`/`multilineVertex` 把多行文本的高亮拼成一个连续多边形，堆叠模式下直接用一个圆角矩形。两条分支最后都把顶点交给 `drawSmoothLines` 生成带圆角的闭合路径。

`drawSmoothLines`（[sources/SquirrelView.swift:355-399](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L355-L399)）用三次贝塞尔曲线在多边形顶点处「削圆」，参数 `alpha`/`beta` 控制圆角程度——它们通常取 `0.3 * cornerRadius` 与 `1.4 * cornerRadius`（见下文 `backgroundPath` 的生成）。这套「贝塞尔圆角」比 Core Graphics 的 `addQuadCurve` 更平滑，是 Squirrel 面板观感的来源。

preedit 内已转换段高亮路径 `highlightedPreeditPath` 的生成见 [sources/SquirrelView.swift:179-210](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L179-L210)，它同样用 `multilineRects`/`linearMultilineFor`/`drawSmoothLines`，区别在于它的外框 `outerBox`/`innerBox` 是依据 `preeditRect`（而非整个 backgroundRect）算的，所以高亮只在 preedit 那一行里。

最外层的 `backgroundPath` 见 [sources/SquirrelView.swift:212-213](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L212-L213)：

```swift
NSBezierPath.defaultLineWidth = 0
backgroundPath = drawSmoothLines(rectVertex(of: backgroundRect), straightCorner: Set(),
                                alpha: 0.3 * theme.cornerRadius, beta: 1.4 * theme.cornerRadius)
```

`rectVertex(of:)`（[SquirrelView.swift:401-406](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L401-L406)）把矩形转成 4 个顶点，`straightCorner` 传空集表示四角全圆。`cornerRadius=0` 时贝塞尔退化为直角，所以不配圆角也不会出错。

#### 4.1.4 代码实践

**实践目标**：在脑中跑一遍「两个候选 + 一段 preedit、高亮第 0 个」时，`draw` 阶段一生成了哪几条路径。

**操作步骤**：

1. 打开 [sources/SquirrelView.swift:130-213](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L130-L213)。
2. 设主题配齐了所有底色（`preeditBackgroundColor`/`candidateBackColor`/`highlightedBackColor`/`highlightedPreeditColor` 均非 nil），输入状态为：`preeditRange` 非空、`candidateRanges` 有 2 个、`hilightedIndex = 0`、`highlightedPreeditRange`（即 `selRange`）非空。
3. 逐行判断阶段一结束时，五个路径变量哪些非 nil、各有几条子路径。

**需要观察的现象 / 预期结果**：

- `backgroundPath`：非 nil，1 条整体圆角矩形。
- `preeditPath`：非 nil（因 preedit 存在且配了 preedit 底色）。
- `highlightedPath`：非 nil，1 条（候选 0 的底）。
- `candidatePaths`：非 nil，**1 条合并路径**（只有候选 1 是非高亮，被 `addPath` 进去）。
- `highlightedPreeditPath`：非 nil，preedit 内已转换段的圆角多边形。

若把 `candidateBackColor` 设为 nil（即用户没配候选底色），则 `candidatePaths` 保持 nil，阶段二就不会生成候选底层——这正是「不配就不画」的按需绘制。

> 本实践为源码阅读型，不要求编译运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `candidatePaths` 要把所有非高亮候选的路径合并成一条，而不是每个候选各建一个 layer？

**答案**：因为它们共用同一种颜色（`candidateBackColor`）和同一种绘制方式，合并成一条路径后只需创建一个 `CAShapeLayer`、设一次 `fillColor`。减少图层数量、简化图层树，也让 `mutualExclusive` 的「挖洞」操作（4.3）能一次把所有候选底区从背景里扣掉。

**练习 2**：`draw` 为什么在阶段二开头要 `self.layer?.sublayers = nil`？

**答案**：`draw` 采用「每帧从零重建」的无状态策略。不清空就会把上一帧的图层与新图层叠在一起，出现重影、颜色翻倍。清空后用本帧算出的路径重新挂载，保证绘制结果只反映当前状态。

---

### 4.2 多层 CAShapeLayer 与 fillColor/mask

#### 4.2.1 概念说明

阶段一把路径算齐后，阶段二要决定「每条路径涂什么颜色、以什么顺序叠加」。Squirrel 选择的不是用 `NSBezierPath.fill()` 直接画进图形上下文，而是**为每类区域创建一个 `CAShapeLayer`**，设好 `fillColor`，挂成一棵图层子树。这样做的好处：

- **硬件加速**：shape layer 由 Core Animation 交给 GPU 合成，圆角路径缩放不锯齿。
- **天然叠加**：图层有明确的 z 序（sublayers 数组顺序即从底到顶），半透明、阴影、遮罩都是现成能力。
- **颜色与路径解耦**：同一条路径可被多个 layer 复用（如 `backgroundPath` 既是 panelLayer 的填充，又是边框 layer 的描边路径，还是命中路径的一部分）。

颜色全部来自主题 `SquirrelTheme`，而主题又来自 YAML 配色方案的键。下表把「图层 → 路径 → 主题颜色字段 → YAML 键」串起来（YAML 键名映射见 [SquirrelTheme.swift:234-239](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L234-L239)）：

| 图层（挂载顺序，由底到顶） | 使用的路径 | `fillColor` / `strokeColor` 来源 | 对应 YAML 颜色键 |
| --- | --- | --- | --- |
| `panelLayer`（背景） | `backPath` | `theme.backgroundColor` | `back_color` |
| preedit 底层 | `preeditPath` | `theme.preeditBackgroundColor` | `preedit_back_color` |
| `borderLayer`（边框） | `backgroundPath`（描边） | `theme.borderColor`（stroke） | `border_color` |
| preedit 高亮层 | `highlightedPreeditPath` | `theme.highlightedPreeditColor` | `hilited_back_color` |
| 候选底层 | `candidatePaths` | `theme.candidateBackColor` | `candidate_back_color` |
| 高亮底层（+阴影） | `highlightedPath` | `theme.highlightedBackColor` | `hilited_candidate_back_color` |
| `pagingLayer`（翻页箭头） | 两个三角形 | `theme.backgroundColor` | 复用 `back_color` |

这就是本讲核心要回答的第一问：**`panelLayer` 由 `backgroundColor` 驱动、`highlightedPath` 对应的图层由 `highlightedBackColor` 驱动、`candidatePaths` 对应的图层由 `candidateBackColor` 驱动**——三者的颜色互不相同、互不干扰。

注意「挂载顺序」就是视觉叠加顺序：背景在最底，往上依次是 preedit 底、边框、preedit 高亮、候选底、高亮底。高亮底最后挂，所以它叠在最上面、压住候选底——这正是「当前选中行」最醒目的原因。

#### 4.2.2 核心流程

阶段二的图层搭建（伪代码）：

```
self.layer?.sublayers = nil

# 1) 背景层 panelLayer
panelLayer = shapeFromPath(backPath)
panelLayer.fillColor = backgroundColor
panelLayer.mask = shapeFromPath(backgroundPath)   # 裁到外圆角矩形
addSublayer(panelLayer)

# 2) preedit 底层（可选）
if preeditBackgroundColor != nil:
    layer = shapeFromPath(preeditPath); layer.fillColor = preeditBackgroundColor
    layer.mask = shapeFromPath(backgroundPath [+ highlightedPreeditPath 若互斥])
    panelLayer.addSublayer(layer)

# 3) 边框（可选）
if borderLineWidth > 0:
    borderLayer = shapeFromPath(backgroundPath)
    borderLayer.strokeColor = borderColor; borderLayer.fillColor = nil
    panelLayer.addSublayer(borderLayer)

# 4) preedit 高亮（可选）
if highlightedPreeditColor != nil:
    layer = shapeFromPath(highlightedPreeditPath); layer.fillColor = highlightedPreeditColor
    panelLayer.addSublayer(layer)

# 5) 候选底（可选）
if candidateBackColor != nil:
    layer = shapeFromPath(candidatePaths); layer.fillColor = candidateBackColor
    panelLayer.addSublayer(layer)

# 6) 高亮底（可选，可带阴影）
if highlightedBackColor != nil:
    layer = shapeFromPath(highlightedPath); layer.fillColor = highlightedBackColor
    if shadowSize > 0: 追加 shadowLayer + 描边
    panelLayer.addSublayer(layer)

panelLayer 平移 pagingOffset（给翻页列让位）
pagingLayer（箭头）挂到 self.layer
```

注意：除 `panelLayer` 与 `pagingLayer` 直接挂到 `self.layer` 外，其余都挂成 `panelLayer` 的**子图层**（`panelLayer.addSublayer(...)`）。这意味着它们会跟随 `panelLayer` 一起平移 `pagingOffset`（第 282 行对 `panelLayer` 设的仿射变换会传导到子图层）。

#### 4.2.3 源码精读

每帧清空图层树见 [sources/SquirrelView.swift:215](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L215)。

所有 shape layer 都由工厂方法 `shapeFromPath(path:)` 创建，见 [sources/SquirrelView.swift:546-551](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L546-L551)：

```swift
func shapeFromPath(path: CGPath?) -> CAShapeLayer {
  let layer = CAShapeLayer()
  layer.path = path
  layer.fillRule = .evenOdd      # 关键：统一用 even-odd 填充
  return layer
}
```

`fillRule = .evenOdd` 是后续「挖洞」的前提（4.3），也为 mask 服务。

**背景层 `panelLayer`** 见 [sources/SquirrelView.swift:228-232](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L228-L232)：

```swift
let panelLayer = shapeFromPath(path: backPath)
panelLayer.fillColor = theme.backgroundColor.cgColor
let panelLayerMask = shapeFromPath(path: backgroundPath)
panelLayer.mask = panelLayerMask
self.layer?.addSublayer(panelLayer)
```

这里出现第一个 `mask`：`panelLayer.mask` 的路径是 `backgroundPath`（最外层圆角矩形）。它的作用是把背景填充**裁剪**在外圆角矩形之内——即便 `backPath` 因挖洞产生一些边缘情况，最终可见区域也绝不会超出面板外轮廓。

**preedit 底层** 见 [sources/SquirrelView.swift:234-244](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L234-L244)，它的 mask 是 `backgroundPath` 加上（互斥时）`highlightedPreeditPath`——目的是让 preedit 底色不要盖住 preedit 高亮区，同样用 even-odd 挖洞实现。

**边框层** 见 [sources/SquirrelView.swift:245-251](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L245-L251)，注意它 `fillColor = nil`（只描边不填充），`lineWidth = theme.borderLineWidth * 2`（乘 2 是因为描边沿路径两侧各画一半，路径本身在矩形边界上）。

**候选底 / 高亮底** 见 [sources/SquirrelView.swift:257-261](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L257-L261) 与 [sources/SquirrelView.swift:262-281](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L262-L281)。高亮底层多了**阴影**逻辑，见 [sources/SquirrelView.swift:265-279](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L265-L279)：

```swift
if theme.shadowSize > 0 {
  let shadowLayer = CAShapeLayer()
  shadowLayer.shadowColor = NSColor.black.cgColor
  shadowLayer.shadowOffset = NSSize(width: theme.shadowSize/2,
                                    height: (theme.vertical ? -1 : 1) * theme.shadowSize/2)
  shadowLayer.shadowPath = highlightedPath
  shadowLayer.shadowRadius = theme.shadowSize
  shadowLayer.shadowOpacity = 0.2
  let outerPath = backgroundPath?.mutableCopy()
  outerPath?.addPath(path)
  let shadowLayerMask = shapeFromPath(path: outerPath)
  shadowLayer.mask = shadowLayerMask
  ...
  layer.addSublayer(shadowLayer)
}
```

阴影只画在高亮候选上（`shadowPath = highlightedPath`），制造「高亮行浮起」的层次感。`shadowLayerMask` 用「外框 + 高亮」的合并路径，靠 even-odd 让阴影只在**高亮块之外**（即投向周围候选与背景）显示，不污染高亮块自身——这是 even-odd 挖洞的又一次复用。

最后，`panelLayer` 整体平移 `pagingOffset` 见 [sources/SquirrelView.swift:282](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L282)，把主面板内容推到翻页列右侧：

```swift
panelLayer.setAffineTransform(CGAffineTransform(translationX: theme.pagingOffset, y: 0))
```

#### 4.2.4 代码实践

**实践目标**：把「图层 → 颜色字段 → YAML 键」三栏对上号，理解每个区域的颜色都可在 `squirrel.yaml` 里独立配置。

**操作步骤**：

1. 对照 [sources/SquirrelView.swift:228-281](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L228-L281) 的六个图层挂载点，记下每个 `fillColor`/`strokeColor` 取自哪个主题字段。
2. 跟进 [sources/SquirrelTheme.swift:234-239](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L234-L239)，把字段映射到 YAML 键：
   - `backgroundColor` ← `back_color`
   - `preeditBackgroundColor` ← `preedit_back_color`
   - `highlightedPreeditColor` ← `hilited_back_color`
   - `highlightedBackColor` ← `hilited_candidate_back_color`
   - `candidateBackColor` ← `candidate_back_color`
   - `borderColor` ← `border_color`
3. 打开 `data/squirrel.yaml` 任选一个 `preset_color_schemes`（如 `macos_light`），找到这些键，确认它们正是面板各区域的配色。

**需要观察的现象 / 预期结果**：

- 面板背景、候选底、高亮底、preedit 底、preedit 高亮、边框**各有独立颜色键**，互不共用。
- `highlightedBackColor` 缺省时会回退到 `highlightedPreeditColor`（见 [SquirrelTheme.swift:236](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L236) 的 `?? highlightedPreeditColor`），所以即便用户只配了 `hilited_back_color`，高亮候选也有底色。
- 某颜色键缺失时，对应字段为 nil，`draw` 里 `if let color = ...` 不成立，**该图层整段跳过**——面板上就不出现那块底色。这就是「不配就不画」。

> 本实践为源码阅读 + 配置对照型，可同步修改本地 `~/Library/Rime/squirrel.yaml` 后重新部署观察效果（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`borderLayer` 为什么 `fillColor = nil` 且 `lineWidth = borderLineWidth * 2`？

**答案**：边框只想描边、不想再填一遍背景色，所以 `fillColor = nil`。`lineWidth * 2` 是因为 Core Animation 的描边以路径为中心线、向两侧各延伸一半宽度；`backgroundPath` 画在矩形边界上，要得到视觉宽度为 `borderLineWidth` 的边框，需把 layer 的 `lineWidth` 设成两倍。

**练习 2**：为什么候选底层、高亮底层都挂成 `panelLayer` 的子图层，而不是和 `panelLayer` 一样直接挂到 `self.layer`？

**答案**：挂成 `panelLayer` 子图层后，它们会继承 `panelLayer` 在第 282 行设置的 `pagingOffset` 平移变换，自动跟着主面板一起右移，躲开翻页列。若直接挂到 `self.layer`，就得每个图层各自再算一次平移，既啰嗦又易错。图层树的父子关系天然提供了「变换继承」。

---

### 4.3 mutual_exclusive 互斥叠加规则

#### 4.3.1 概念说明

现在回答一个关键问题：高亮底色和候选底色都叠在背景之上，它们会不会和背景色**混色**？

默认情况下会。如果背景是不透明的，叠加上层颜色当然只显示上层；但如果颜色带透明度（YAML 颜色可带 alpha，见 u3-l1 的 `0xAABBGGRR` 字节序），叠加就会混色，导致高亮块的色调被背景「拉偏」。

Squirrel 提供了一个开关 `mutual_exclusive`（YAML 键 `mutual_exclusive`，布尔，默认 `false`，见 [SquirrelTheme.swift:66](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L66) 与 [SquirrelTheme.swift:203](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L203)）。开启它后，背景会在候选/高亮区域**挖洞**——即这些区域的背景填充被「扣掉」，改由各自的颜色层独占，于是颜色互不混合，每块都是纯粹的单色。这就是名字「互斥（mutually exclusive）」的含义：**同一块像素只归属一种底色**。

挖洞的数学原理就是 §2 提到的 **even-odd 填充规则**。

#### 4.3.2 核心流程

关键在于 `backPath` 的组装（[sources/SquirrelView.swift:216-227](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L216-L227)）：

```
backPath = backgroundPath（外圆角矩形）.mutableCopy()
if preeditPath 存在:        backPath.addPath(preeditPath)        # preedit 区始终挖洞
if mutualExclusive:
    if highlightedPath 存在: backPath.addPath(highlightedPath)    # 高亮区挖洞
    if candidatePaths 存在:  backPath.addPath(candidatePaths)     # 候选区挖洞

panelLayer = shapeFromPath(backPath)   # fillRule = .evenOdd
panelLayer.fillColor = backgroundColor
panelLayer.mask = shapeFromPath(backgroundPath)   # 裁到外框
```

由于 `shapeFromPath` 统一设了 `fillRule = .evenOdd`，`backPath` 里多个重叠的闭合子路径会让重叠区「穿越次数 = 2 = 偶数 = 不填充」。效果就是：

- 背景填充覆盖**除 preedit / 高亮 / 候选之外**的所有区域；
- preedit / 高亮 / 候选区域成了背景上的「洞」，透出下面（其实是 `panelLayer` 之下、`self.layer` 的背景，通常是透明）；
- 这些洞随后被各自的颜色层（preedit 底层、候选底层、高亮底层）从上面盖住，填上专属颜色。

因此每块区域只显示一种颜色，无混色。

需要特别注意一个不对称：**preedit 区总是被挖洞**（`preeditPath` 无条件 `addPath` 进 `backPath`，见 [L217-219](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L217-L219)），而候选/高亮区**只在 `mutualExclusive` 为真时才挖洞**。也就是说，preedit 底色始终是「互斥」的，候选/高亮底色是否互斥由开关控制。

#### 4.3.3 源码精读

`backPath` 的组装见 [sources/SquirrelView.swift:216-227](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L216-L227)：

```swift
let backPath = backgroundPath?.mutableCopy()
if let path = preeditPath {
  backPath?.addPath(path)              # preedit 始终挖洞
}
if theme.mutualExclusive {
  if let path = highlightedPath {
    backPath?.addPath(path)
  }
  if let path = candidatePaths {
    backPath?.addPath(path)
  }
}
```

紧接着构造 `panelLayer` 并设 mask，见 [sources/SquirrelView.swift:228-232](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L228-L232)。`fillRule = .evenOdd` 在 `shapeFromPath`（[L546-551](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L546-L551)）里统一设置。

even-odd 挖洞的效果可用一个简单数学描述。设背景外框区域为 \( R \)，preedit 区为 \( P \)、高亮区为 \( H \)、候选区为 \( C \)。`backPath` 的填充区域（`mutualExclusive` 开启时）为：

\[
\mathrm{Fill} = \big( R \oplus P \oplus H \oplus C \big) \cap R
\]

其中 \( \oplus \) 表示对称差（XOR），即「奇数次覆盖才保留」；最后的 \( \cap R \) 来自 mask 把结果裁到外框。于是 preedit/高亮/候选区域被从背景填充里**减去**，留出洞。

同样的「挖洞」手法在 preedit 底层也用了一次：当 `mutualExclusive` 开启且存在 preedit 高亮时，preedit 底层的 mask 会把 `highlightedPreeditPath` 加入，让 preedit 底色不在已转换段高亮区叠加——见 [sources/SquirrelView.swift:237-243](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L237-L243)：

```swift
let maskPath = backgroundPath?.mutableCopy()
if theme.mutualExclusive, let hilitedPath = highlightedPreeditPath {
  maskPath?.addPath(hilitedPath)
}
let mask = shapeFromPath(path: maskPath)
layer.mask = mask
```

#### 4.3.4 代码实践

**实践目标**：理解开启/关闭 `mutual_exclusive` 对带透明度底色的视觉影响。

**操作步骤**：

1. 阅读 [sources/SquirrelView.swift:216-232](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L216-L232)，确认 `mutualExclusive` 为真时 `highlightedPath`/`candidatePaths` 会被加入 `backPath`。
2. 假设主题里 `back_color` 带透明度（如 `0xCCFFFFFF`，约 80% 不透明白）、`hilited_candidate_back_color` 也带透明度（如 `0xCC0000FF`，80% 蓝）。
3. 分别推演 `mutualExclusive = false` 与 `true` 两种情况下，高亮候选像素最终的颜色。

**需要观察的现象 / 预期结果**：

- `mutualExclusive = false`：背景填充覆盖整块（含高亮区），高亮蓝层叠在上面 → 蓝色与背景白色**透明混合**，得到偏淡的蓝。
- `mutualExclusive = true`：高亮区被从背景挖洞，洞里没有背景白，高亮蓝层直接落在透明上 → 显示**纯粹的蓝**（仅受窗口整体 `alphaValue` 影响）。
- 结论：`mutual_exclusive` 适合「底色都带透明度、希望颜色不互相干扰」的配色；关闭时则呈现更柔和的叠加观感。

> 本实践为源码阅读型，可在本地 `~/Library/Rime/squirrel.yaml` 的某个配色方案下切换 `style/mutual_exclusive` 后重新部署观察（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 preedit 区**无论 `mutualExclusive` 是否开启**都会被挖洞？

**答案**：见 [L217-219](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L217-L219)，`preeditPath` 的 `addPath` 不在 `if theme.mutualExclusive` 分支内。这是设计上的固定选择：preedit 行和候选行是两个独立视觉区，preedit 底色始终不应与面板主背景混色，所以无条件互斥；而候选/高亮是否互斥交给用户开关决定。

**练习 2**：如果把 `shapeFromPath` 里的 `layer.fillRule = .evenOdd` 改成默认的 `.nonZero`，「挖洞」还会生效吗？

**答案**：不会（通常情况下）。`.nonZero` 规则考虑路径方向，只有「方向相反的闭合子路径」才能形成洞；而这里所有子路径都是顺时针同向生成的（见 `drawSmoothLines` 的 `path.closeSubpath()` 与 `enlarge` 注释「Assumes clockwise iteration」），同向子路径在 non-zero 规则下不会互相扣除。所以 even-odd 是挖洞成立的关键。（本讲禁止改源码，此为思想实验。）

---

### 4.4 paging 翻页箭头与 shape 命中区

#### 4.4.1 概念说明

最后一个最小模块要讲两件看似无关、实则共用一个对象的事：

1. **翻页箭头怎么画**：当 `show_paging` 开启且候选可上/下翻页时，面板左侧（宽 `pagingOffset` 的列）会画一上一下两个三角形，点击它们可翻页。
2. **`view.shape` 这个 `CAShapeLayer` 的双重身份**：它既被当作**半透明背景视图 `back` 的遮罩**，又被当作**鼠标命中测试的区域**。

先说 `shape` 是什么。它是 `SquirrelView` 的一个实例属性，类型 `CAShapeLayer`，声明见 [sources/SquirrelView.swift:36](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L36)：

```swift
var shape = CAShapeLayer()
```

它的 `.path` 在每次 `draw` 末尾被赋成 `panelPath`（面板外轮廓 + 两个箭头的合并路径，见下文）。这个 layer 不挂进 `self.layer` 的显示树——它根本不是用来「显示」的，而是被**两个地方借用其几何**：

- **作为遮罩**：在面板初始化时，`back.layer?.mask = view.shape`（[SquirrelPanel.swift:46](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L46)）。`back` 是半透明背景视图（`NSGlassEffectView` 或 `NSVisualEffectView`，见 [SquirrelPanel.swift:553-565](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L553-L565)），用 `shape` 当 mask 后，毛玻璃效果只在面板轮廓（含箭头）内显现，外边被裁掉。
- **作为命中区**：鼠标点击时，`click(at:)` 用 `shape.path.contains(clickPoint)` 判断点击是否落在面板内（[SquirrelView.swift:313](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L313)），再细分到候选/preedit/箭头。

这两个用途共享同一个 `shape`，是因为它们要的几何**完全一致**：面板「可见轮廓」就是「可点击区域」。一处定义、两处复用，避免了几何重复计算与不一致风险。

#### 4.4.2 核心流程

**翻页箭头的生成**（`pagingLayer`，[sources/SquirrelView.swift:723-755](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L723-L755)）：

```
pagingLayer(theme, preeditRect):
  if not showPaging 或 (not canPageUp 且 not canPageDown): 返回空 layer
  算箭头高度 height（基于首候选高度 + preedit 高度）
  radius = min(0.5*pagingOffset, 2*height/9)
  trianglePath = drawSmoothLines(triangle(center:.zero, radius:radius), ...)   # 正三角形
  if canPageDown:
    downTransform = 平移到翻页列下方
    downLayer = shapeFromPath(trianglePath 经 downTransform)
    downLayer.fillColor = backgroundColor
    downPath = 三角形路径（经变换）
  if canPageUp:
    upTransform = 旋转 π + 平移到翻页列上方
    upLayer = shapeFromPath(trianglePath 经 upTransform)
    upLayer.fillColor = backgroundColor
    upPath = 三角形路径（经变换）
  return (layer 含 downLayer/upLayer, downPath, upPath)
```

下三角朝下（`canPageDown`，向下一页），上三角由正三角形旋转 π 得到、朝上（`canPageUp`，向上一页）。两个箭头都填 `backgroundColor`，与面板背景同色——它们靠**形状**而非颜色来识别。

**命中路径 `panelPath` 的组装**（[sources/SquirrelView.swift:283-300](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L283-L300)）：

```
panelPath = CGMutablePath()
panelPath.addPath(backgroundPath, 经坐标换算)   # 面板主轮廓
panelPath.addPath(downPath, 经 flipTransform)   # 下箭头
panelPath.addPath(upPath,   经 flipTransform)   # 上箭头
shape.path = panelPath                          # 同时供 mask 与命中测试
```

其中 `flipTransform = CGAffineTransform(scaleX: 1, y: -1).translatedBy(x: 0, y: -bounds.height)`（[L290](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L290)）用来把图层坐标系（y 向上）换算到视图坐标系（`isFlipped` 为 true、y 向下），使 `panelPath` 与鼠标点击点在同一坐标系，`contains()` 才正确。两个箭头路径还被单独存进 `self.downPath`/`self.upPath`（[L293](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L293)、[L297](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L297)），供 `click(at:)` 优先判断。

**命中测试 `click(at:)`**（[sources/SquirrelView.swift:303-341](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L303-L341)）的三段判断：

```
click(at: clickPoint) -> (candidateIndex, preeditIndex, pagingUp?)
  1. 若 downPath 包含 clickPoint → return (nil, nil, false)   # 点中下箭头=下翻
  2. 若 upPath   包含 clickPoint → return (nil, nil, true)    # 点中上箭头=上翻
  3. 若 shape.path 包含 clickPoint:                            # 点在面板内
       用 textLayoutFragment 定位到具体字符
       若落在 preeditRange → preeditIndex = 字符偏移（移动光标）
       若落在某 candidateRange → candidateIndex = 行号（选词）
  4. 否则 → (nil, nil, nil)  # 点在面板外
```

注意箭头判断**优先**于面板整体判断——因为箭头在 `shape.path`（含箭头）之内，若不先判箭头，点中箭头会被误当作「点在面板内」进而走到文字命中分支。先判 `downPath`/`upPath` 并直接 return，就绕开了这个冲突。

#### 4.4.3 源码精读

翻页箭头生成函数见 [sources/SquirrelView.swift:723-755](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L723-L755)。其中 `triangle(center:radius:)`（[L717-721](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L717-L721)）给出一个顶点朝上的正三角形三顶点；下箭头直接平移，上箭头先旋转 π 再平移：

```swift
if canPageDown {
  var downTransform = CGAffineTransform(translationX: 0.5 * theme.pagingOffset, y: 2 * height / 3 + preeditHeight)
  let downLayer = shapeFromPath(path: trianglePath.copy(using: &downTransform))
  downLayer.fillColor = theme.backgroundColor.cgColor
  downPath = trianglePath.copy(using: &downTransform)
  layer.addSublayer(downLayer)
}
if canPageUp {
  var upTransform = CGAffineTransform(rotationAngle: .pi).translatedBy(x: -0.5 * theme.pagingOffset, y: -height / 3 - preeditHeight)
  ...
}
```

`pagingLayer` 返回三层信息：装好三角形的 `CAShapeLayer`（用于显示）、`downPath`、`upPath`（用于命中）。回到 `draw`，见 [sources/SquirrelView.swift:286-298](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L286-L298)：只有当 `pagingLayer.sublayers` 非空（即至少画了一个箭头）时才把它挂到 `self.layer`；同时把两个箭头路径经 `flipTransform` 加进 `panelPath`，并单独存进实例属性。

`shape.path` 的最终赋值见 [sources/SquirrelView.swift:300](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L300)：

```swift
shape.path = panelPath
```

这一行是「双重身份」的交汇点：赋值后，`back.layer?.mask`（借用 `shape`）立刻反映新轮廓，`click(at:)` 里的 `shape.path.contains(...)` 也用上同一份路径。

`shape` 作为遮罩的使用在面板初始化里，见 [sources/SquirrelPanel.swift:36-52](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L36-L52)：

```swift
init(position: NSRect) {
  ...
  self.view = SquirrelView(frame: position)
  self.back = Self.makeBackgroundView()
  ...
  back.wantsLayer = true
  back.layer?.mask = view.shape      # shape 借给 back 当遮罩
  let contentView = NSView()
  contentView.addSubview(back)
  contentView.addSubview(view)
  contentView.addSubview(view.textView)
  self.contentView = contentView
}
```

注意 `back`（毛玻璃）与 `view`（自绘底板）、`view.textView`（文字）是**三个并排的子视图**叠在一起：最底是毛玻璃 `back`（被 `shape` 裁成面板轮廓），中间是 `view`（画背景/候选/高亮），最上是 `textView`（画文字）。`shape` 既裁了最底的毛玻璃，又给中间的 `view` 提供命中区——一个对象、跨视图复用。

`back` 只在 `theme.translucency` 开启时显示，见 [SquirrelPanel.swift:515-523](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L515-L523)：关闭时 `back.isHidden = true`，毛玻璃不显示，但 `shape` 作为命中区的职责仍在。

命中测试本体见 [sources/SquirrelView.swift:303-341](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L303-L341)，三段优先级清晰：

```swift
func click(at clickPoint: NSPoint) -> (Int?, Int?, Bool?) {
  ...
  if let downPath = self.downPath, downPath.contains(clickPoint) {
    return (nil, nil, false)                       # 下箭头
  }
  if let upPath = self.upPath, upPath.contains(clickPoint) {
    return (nil, nil, true)                        # 上箭头
  }
  if let path = shape.path, path.contains(clickPoint) {
    // 用 textLayoutManager.textLayoutFragment(for:) 定位字符
    // 落在 preeditRange → preeditIndex；落在 candidateRanges[i] → candidateIndex = i
    ...
  }
  return (candidateIndex, preeditIndex, nil)
}
```

返回的三元组 `(candidateIndex, preeditIndex, pagingUp)` 由 `SquirrelPanel.sendEvent` 消费（[SquirrelPanel.swift:70-97](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L70-L97)）：`pagingUp` 决定上/下翻页、`candidateIndex` 决定选哪个候选、`preeditIndex` 决定光标移动方向。这部分回调细节属于 u4-l4。

#### 4.4.4 代码实践

**实践目标**：说清 `panelLayer`、`highlightedPath`、`candidatePaths` 各自的颜色驱动，以及 `self.shape.path` 为何能同时充当遮罩与命中区（对应本讲核心实践任务）。

**操作步骤**：

1. 在 [sources/SquirrelView.swift:228-281](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L228-L281) 找到三个图层的 `fillColor`：
   - `panelLayer.fillColor = theme.backgroundColor.cgColor`（[L229](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L229)）——对应 YAML `back_color`。
   - 高亮底图层的 `layer.fillColor = color.cgColor`，其中 `color = theme.highlightedBackColor`（[L262-264](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L262-L264)）——对应 `hilited_candidate_back_color`。
   - 候选底图层的 `layer.fillColor = color.cgColor`，其中 `color = theme.candidateBackColor`（[L257-259](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L257-L259)）——对应 `candidate_back_color`。
2. 在 [sources/SquirrelPanel.swift:46](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L46) 找到 `back.layer?.mask = view.shape`，确认 `shape` 是毛玻璃背景视图的遮罩。
3. 在 [sources/SquirrelView.swift:300](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L300) 找到 `shape.path = panelPath`，再在 [L313](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L313) 找到 `shape.path.contains(clickPoint)`，确认同一份路径被命中测试复用。

**需要观察的现象 / 预期结果（关键结论）**：

- 三个图层颜色互不相同：`panelLayer` ← `backgroundColor`、高亮 ← `highlightedBackColor`、候选 ← `candidateBackColor`，分别由三个独立 YAML 键驱动。
- `self.shape` 是一个**不参与显示树**的 `CAShapeLayer`，它的 `.path`（即 `panelPath`，面板外轮廓 + 翻页箭头）被两处借用：
  1. 作为 `back`（毛玻璃背景视图）的 `layer.mask`——把毛玻璃裁成面板轮廓，使模糊效果只在面板（含箭头）内显现。这里需要澄清：`shape` 并不是 `panelLayer.mask`（`panelLayer.mask` 是另一个由 `backgroundPath` 构造的 `panelLayerMask`）；`shape` 是更外层的、毛玻璃视图的遮罩。可以把它理解为「整张面板轮廓的遮罩」。
  2. 作为 `click(at:)` 的命中测试区——`shape.path.contains(clickPoint)` 判断点击是否落在面板可见区内，再细分到候选/preedit。
- 两处能共用同一个 `shape`，是因为「面板可见轮廓」与「可点击区域」本就是同一几何。一处计算（`draw` 末尾组装 `panelPath`）、两处复用，保证遮罩范围与命中范围永远一致——既不会「看得见却点不到」，也不会「点得到却没有图形」。

> 本实践为源码阅读型，不要求编译运行。

#### 4.4.5 小练习与答案

**练习 1**：`click(at:)` 为什么先判断 `downPath`/`upPath`，再判断 `shape.path`？颠倒会怎样？

**答案**：因为两个箭头路径也包含在 `shape.path`（`panelPath`）之内。若先判 `shape.path`，点中箭头会被当作「点在面板内」而走进文字命中分支，可能误选到箭头下方的候选。先判箭头并直接 `return (nil, nil, false/true)`，把翻页意图拦截在最前面，避免歧义。

**练习 2**：`view.shape` 这个 `CAShapeLayer` 有没有被加进 `self.layer` 的 sublayers 用来显示？

**答案**：没有。`shape` 从不作为显示图层挂载，它只被借用几何：赋给 `back.layer?.mask`（当遮罩）和读取其 `.path` 做 `contains()` 命中测试。它的「显示」完全通过 `back` 这个毛玻璃视图的遮罩间接体现。所以即便它不画任何颜色，也参与了最终视觉（裁出面板轮廓）。

**练习 3**：翻页箭头的颜色为什么是 `backgroundColor`？它靠什么让用户「看见」？

**答案**：箭头与面板主背景同色（[L743](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L743)、[L750](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L750)）。它靠**形状**（三角形）与**位置**（翻页列内、上下两端）被识别，而非靠颜色对比；这也意味着只有 `show_paging` 开启且确有可翻页方向时才画出对应箭头（`canPageUp`/`canPageDown`），避免误导。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「从区间到图层」的完整追踪。

**任务**：给定一次输入的面板状态，完整描述 `draw(_:)` 如何把 `candidateRanges`/`preeditRange` 变成屏幕上的分层底板。

设主题为某个完整配色方案（所有底色字段非 nil，`mutualExclusive = true`，`show_paging = true`，`cornerRadius > 0`），输入状态为：`preeditRange` 非空、`highlightedPreeditRange`（`selRange`）非空、`candidateRanges` 有 3 个、`hilightedIndex = 1`、`canPageUp = true`、`canPageDown = true`。

**要求你按顺序回答**：

1. **阶段一路径**：阶段一结束时，`backgroundPath`/`preeditPath`/`highlightedPath`/`candidatePaths`/`highlightedPreeditPath` 各是什么？其中 `candidatePaths` 合并了哪几个候选？（参考 [L142-213](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L142-L213)）
2. **backPath 与挖洞**：`mutualExclusive = true` 时，`backPath` 由哪几条路径相加组成？even-odd 填充会让哪些区域成为背景上的「洞」？（参考 [L216-227](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L216-L227)）
3. **图层颜色**：`panelLayer`、候选底层、高亮底层分别用什么颜色？写出它们对应的主题字段与 YAML 键。（参考 [L228-281](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L228-L281) 与 [SquirrelTheme.swift:234-239](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L234-L239)）
4. **翻页箭头**：`pagingLayer` 会画几个三角形？为什么是这些？它们填什么颜色？（参考 [L723-755](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L723-L755)）
5. **shape 双重身份**：`shape.path` 最终由哪几条路径合并而成？它被哪两处借用？（参考 [L283-300](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L283-L300)、[SquirrelPanel.swift:46](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L46)、[SquirrelView.swift:313](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L313)）

**参考答案要点**：

1. 五条路径都非 nil。`candidatePaths` 合并了候选 0 与候选 2（高亮的是候选 1，单独成 `highlightedPath`，不进 `candidatePaths`）。`highlightedPreeditPath` 是 preedit 内 `selRange` 段的圆角多边形。
2. `backPath = backgroundPath + preeditPath + highlightedPath + candidatePaths`（因 `mutualExclusive`）。even-odd 让 preedit 区、高亮候选（候选 1）区、非高亮候选（候选 0、2）区都成为「洞」——这些区域不填背景色，改由各自的颜色层独占。
3. `panelLayer` ← `backgroundColor`（`back_color`）；候选底层 ← `candidateBackColor`（`candidate_back_color`）；高亮底层 ← `highlightedBackColor`（`hilited_candidate_back_color`）。三者颜色独立。
4. 画 2 个三角形（上、下各一），因为 `canPageUp` 与 `canPageDown` 都为 true。都填 `backgroundColor`，靠形状与位置被识别。
5. `panelPath = backgroundPath + downPath + upPath`（经坐标换算）。它被 `back.layer?.mask = view.shape`（毛玻璃遮罩）与 `click(at:)` 里的 `shape.path.contains(clickPoint)`（命中测试）两处借用——同一几何定义可见轮廓与可点击区。

---

## 6. 本讲小结

- `SquirrelView.draw(_:)` 不画文字（文字由 `NSTextView` 渲染），只画底板：把绘制拆成 **background / preedit / candidate / highlighted / highlightedPreedit** 五类路径，分两阶段——先算路径、再每帧从零重建图层树（`sublayers = nil`）。
- 五类路径里，`candidatePaths` 是多个非高亮候选的**合并路径**（共用一种颜色），`highlightedPath` 是当前高亮行的单条路径；最外层 `backgroundPath` 是整体圆角矩形，圆角由 `drawSmoothLines` 的贝塞尔削角产生。
- 颜色完全由主题驱动且按图层独立：`panelLayer.fillColor = backgroundColor`、候选底层 = `candidateBackColor`、高亮底层 = `highlightedBackColor`，分别对应 YAML 的 `back_color`/`candidate_back_color`/`hilited_candidate_back_color`；某字段为 nil 时对应图层整段跳过（「不配就不画」）。
- `mutual_exclusive` 开启时，靠 `shapeFromPath` 统一设的 **`.evenOdd` 填充规则**把高亮/候选路径加入 `backPath`，在背景上「挖洞」，使每块像素只归属一种底色、避免透明混色；preedit 区则无条件挖洞。
- 翻页箭头由 `pagingLayer` 生成两个三角形（下/上），填 `backgroundColor`，靠形状识别；箭头路径经 `flipTransform` 并入 `panelPath`。
- `view.shape` 是一个不参与显示的 `CAShapeLayer`，其 `.path`（= `panelPath` = 面板轮廓 + 箭头）被**两处复用**：作为毛玻璃背景视图 `back` 的 `layer.mask`（裁出轮廓），以及 `click(at:)` 里 `shape.path.contains(...)` 的命中测试区——保证「可见即可点击」。

---

## 7. 下一步学习建议

- **u4-l4 面板鼠标与滚轮事件**：本讲的 `click(at:)` 只讲了「命中判断」，下一讲会接着讲 `SquirrelPanel.sendEvent` 如何在 `leftMouseDown`/`leftMouseUp` 之间配合 `click(at:)` 的返回值完成选词、翻页、移动光标，以及 `scrollWheel` 如何根据 dx/dy 决定翻页方向。
- **回看 u4-l1 / u4-l2**：本讲大量依赖 u4-l1 存入的 `candidateRanges`/`preeditRange`/`hilightedIndex`，以及 u4-l1 讲过的 `ensureLayout` 后 `enumerateTextSegments` 才能给出正确矩形；若对几何测量不熟，回看这两讲。
- **配色实战**：在本地 `~/Library/Rime/squirrel.yaml` 自定义一个 `preset_color_schemes`，把 `back_color`/`candidate_back_color`/`hilited_candidate_back_color` 设成带不同 alpha 的颜色，分别切换 `mutual_exclusive` 与 `translucency`，对照本讲 §4.2/§4.3 观察图层叠加与挖洞的视觉差异（需重新部署生效）。
- **后续 u5-l3 保留属性**：本讲提到候选注释的 accent/warning 语义色（`accentCommentTextColor`/`warningCommentTextColor`），其索引由 librime 插件经保留属性消息传入，将在 u5-l3 详解整条传递链。
