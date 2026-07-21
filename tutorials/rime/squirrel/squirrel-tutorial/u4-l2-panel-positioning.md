# 面板定位与全屏缩放

## 1. 本讲目标

上一篇（u4-l1）我们把引擎返回的散件缝成了一段富文本，并主动 `ensureLayout` 让 TextKit 2 把它排好版，最后交给 `drawView` 置 `needsDisplay`。但有一个最现实的问题被刻意悬置了：**这张面板该画在屏幕的哪个位置？该有多大？候选词太多、一行塞不下怎么办？**

本讲精读 `SquirrelPanel` 的私有方法 [`show()`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L361-L529)，它是面板真正「登台亮相」的唯一出口。读完本讲你应当能够：

1. 说清面板的「自然尺寸」是如何用 TextKit 2 测量出来的，以及为何要先把文字宽度限制在一个比例上限。
2. 解释当自然尺寸超过屏幕 95% 时，`optimalTextWidth` 如何用面积守恒推导出一个最优换行宽度，再用统一的 `scale` 把整个面板等比缩小并居中。
3. 描述普通模式下面板如何锚定在光标附近、如何被屏幕边界「推回」并按需翻转到光标上方。
4. 理解垂直模式下 `boundsRotation = -90` 的坐标系旋转原理，以及半透明（translucency）背景视图与面板 `shape` 遮罩的配合。

## 2. 前置知识

在进入源码前，先建立几个 Cocoa 坐标与视图概念，本讲会反复用到它们。

- **frame 与 bounds 的区别**：`frame` 是视图在父视图坐标系里的位置和大小；`bounds` 是视图「自己看自己」的坐标系，原点默认为 `(0,0)`。当 `frame.size` 与 `bounds.size` **不一致**时，Cocoa 会自动对绘制内容做缩放——这正是本讲实现「全屏缩小」的廉价手段：物理尺寸（frame）缩小，绘制坐标（bounds）保持自然尺寸，Cocoa 帮你把后者缩进前者。
- **boundsRotation**：`NSView` 可以旋转自己的 bounds 坐标系（单位是度）。设成 `-90` 后，视图原本「向右为 x 正、向上为 y 正」的坐标系整体顺时针旋转 90°，于是「横排文字」看上去就变成了「竖排」。Squirrel 的垂直模式靠的就是这一招，而非真的逐字重排。
- **NSScreen 的坐标系**：macOS 屏幕坐标系以**左下角为原点**，y 轴向上。光标 caret 矩形 `position` 来自 `IMKTextInput`，也是这个坐标系，`position.minY` 是光标底边、`position.maxY` 是光标顶边。
- **TextKit 2 的惰性布局**：NSTextView 不会主动把所有文本排完，需要调用 `textLayoutManager.ensureLayout(for: documentRange)` 强制排版后，才能用 `enumerateTextSegments` 拿到每段文字的真实矩形。u4-l1 已讲过这一点，本讲依赖它来测量尺寸。
- **`offsetHeight`**：[`SquirrelTheme.offsetHeight`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L11) 是一个常量 `5.0`，代表面板与光标之间留出的呼吸间隙（pt）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [sources/SquirrelPanel.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift) | 本讲主角。私有方法 `show()` 集中了全部定位与缩放逻辑；`maxTextWidth()`/`currentScreen()` 是它的测量与寻屏助手；`makeBackgroundView()` 生成半透明背景视图。 |
| [sources/SquirrelView.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift) | 提供 [`contentRect`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L85-L103) 计算属性——把已排版的文本段聚合成一个包围盒，这是 `show()` 测量自然尺寸的依据。 |
| [sources/SquirrelTheme.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift) | 提供 `edgeInset`、`pagingOffset`、`alpha`、`translucency`、`vertical`、`memorizeSize` 等几何与外观参数。 |

> 提示：本讲只读 `show()` 及其助手；面板的**内容拼装**（富文本、`candidate_format` 模板替换、`noBreak`）已在 u4-l1 讲透，本讲默认那段代码已经把文本送进 `textContentStorage` 并调用过 `drawView`，我们从「文本已排版、即将登台」这一刻接手。

## 4. 核心概念与源码讲解

### 4.1 TextKit 自然尺寸测量

#### 4.1.1 概念说明

「自然尺寸」指的是：**给定一个文字排版宽度上限，让文本自由换行后，整块文字（含 preedit 行与全部候选行）占据的宽 × 高**。它是后续一切定位与缩放判断的基准——普通模式下面板几乎就等于自然尺寸加上内边距；只有当自然尺寸大到快撑爆屏幕时，才会触发全屏缩放。

为什么需要先给一个「宽度上限」再去测高度？因为文本宽度与高度是此消彼长的：宽度越宽，每行容字越多，总高度越矮。如果不限制宽度，一行能写完整段编码，面板就会变成一根又细又长的面条。所以 `show()` 一进门先调 `maxTextWidth()` 算出「最多给你用多宽」，再让 TextKit 在这个宽度下排版，量出来的高度才有意义。

#### 4.1.2 核心流程

`show()` 的测量阶段（普通与全屏路径共用）可以用下面的伪代码概括：

```
show():
  currentScreen()                       # 1. 定位光标所在屏幕
  设窗口 appearance                       # 2. 亮/暗外观（详见 u3-l4）
  textView.textContainerInset = edgeInset
  textWidth = maxTextWidth()            # 3. 计算文字宽度上限
  textContainer.size = (textWidth, ∞)   # 4. 高度不限，让它往下长
  关掉 widthTracksTextView / heightTracksTextView
  ensureLayout(documentRange)           # 5. 强制排版
  contentRect = view.contentRect        # 6. 量包围盒
  naturalPanelSize = contentRect + 内边距*2 (+ pagingOffset)
  ...（下一节：判断是否需要全屏缩放）
```

其中第 6 步的 `contentRect` 是 [SquirrelView 的计算属性](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L85-L103)：它把 `candidateRanges` 与 `preeditRange` 合并，对每段调用 `contentRect(range:)`（内部用 `enumerateTextSegments` 枚举该段的所有排版矩形），取它们的**并集包围盒**，并把左/下边对齐到 0。最终得到的 `contentRect` 就是「文字本身」的净尺寸，不含面板内边距。

#### 4.1.3 源码精读

先看宽度上限是怎么定的——[`maxTextWidth()`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L347-L358)：

```swift
let fontScale = font.pointSize / 12
let textWidthRatio = min(1, 1 / (vertical ? 4 : 3) + fontScale / 12)
let maxWidth = if vertical {
  screenRect.height * textWidthRatio - theme.edgeInset.height * 2
} else {
  screenRect.width * textWidthRatio - theme.edgeInset.width * 2
}
```

横排默认给屏幕宽度的 `1/3`，竖排给 `1/4`（竖排每个字占的视觉宽度更大，所以要更窄）；再按字号微调 `fontScale`（12pt 为基准），字号越大允许越宽。`vertical` 分支用 `screenRect.height` 是因为竖排面板旋转后，文字的「宽度方向」对应屏幕的物理高度。

接着看 [`show()` 的测量主体](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L373-L394)：

```swift
var textWidth = maxTextWidth()
// Measure natural text height before constraining the panel.
view.textContainer.size = NSSize(width: textWidth, height: .greatestFiniteMagnitude)
view.textContainer.widthTracksTextView = false
view.textContainer.heightTracksTextView = false
view.textLayoutManager.ensureLayout(for: view.textLayoutManager.documentRange)
view.textView.bounds.origin = .zero

var contentRect = view.contentRect

var naturalPanelSize = NSSize.zero
if vertical {
  naturalPanelSize.width  = contentRect.height + theme.edgeInset.height * 2
  naturalPanelSize.height = contentRect.width  + theme.edgeInset.width * 2 + theme.pagingOffset
} else {
  naturalPanelSize.width  = contentRect.width  + theme.edgeInset.width * 2 + theme.pagingOffset
  naturalPanelSize.height = contentRect.height + theme.edgeInset.height * 2
}
```

两个要点：

1. **关掉 tracking**：`widthTracksTextView`/`heightTracksTextView` 默认会让容器跟着 textView 的当前可见尺寸走，这会与「我要用容器尺寸反推面板尺寸」的意图打架，注释明确警告「it can loop or hide text」，所以一律关掉。
2. **垂直模式的宽高对调与 `pagingOffset`**：竖排时 `contentRect.height`（文字的物理高度，对应竖排的「列数方向」）变成了面板的物理 `width`，反之亦然——这是 `-90°` 旋转的必然结果。而 [`pagingOffset`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L189-L195)（仅当 `show_paging` 开启时等于字号的 1.5 倍，否则为 0）是翻页箭头列的宽度，它总是加在「文字流向的垂直方向」：横排加在 `width`（箭头在左侧），竖排旋转后则落在 `height`。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「文字宽度上限如何影响自然尺寸」。

**操作步骤**（源码阅读 + 本地验证）：

1. 在 [`maxTextWidth()`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L347-L358) 的 `return maxWidth` 前临时加一行日志：`NSLog("textWidth=%f fontScale=%f", maxWidth, fontScale)`（本讲义不修改源码，此为示例，验证后请还原）。
2. 触发一个候选词很多、一行排不下的输入场景（例如拼音连续输入长串）。
3. 阅读上面 [`naturalPanelSize` 的计算](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L387-L394)，对照你日志里的 `textWidth`，估算 `contentRect.height` 应当约为 `naturalPanelSize.height - 2*edgeInset.height`。

**需要观察的现象**：字号越大，`maxWidth` 越大；候选词越多，`contentRect.height` 越大，面板越高。

**预期结果 / 待本地验证**：面板高度随候选数量增长，宽度被钉在屏幕宽度的约 1/3 附近（横排）。无法在当前环境运行 GUI，故日志数值「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `textContainer.size` 的高度要传 `.greatestFiniteMagnitude` 而不是 `screenRect.height`？
**答案**：测量阶段我们要的是「文字在给定宽度下的自然高度」，不应被任何高度上限截断；传一个极大值等于告诉 TextKit「只管往下排，排完为止」，这样 `contentRect` 量出来的才是真实自然高度。若传屏幕高度，超出的部分会被裁掉，后续的全屏判定就会失准。

**练习 2**：竖排模式下，`naturalPanelSize.width` 为什么用 `contentRect.height`？
**答案**：竖排要靠 `-90°` 旋转实现，旋转后文字的「列方向」（即原来的高度）对应面板的物理宽度，所以面板 `width` 取 `contentRect.height`，二者对调。

---

### 4.2 全屏缩放计算（optimalTextWidth）

#### 4.2.1 概念说明

当用户输入的编码特别长、候选特别多时，自然尺寸可能超过屏幕——总不能让面板顶出屏幕外。Squirrel 的策略很优雅：**先把文字「铺得更宽」以充分利用屏幕，再对整个面板做一个等比缩小并居中**，就像把一张大图缩印到屏幕中央。

「铺得更宽」这一步是关键。自然尺寸超标往往是因为面板又高又窄（文字挤成很多行）。如果在不缩放的前提下先让文字换行更少（每行更宽、行数更少），就能把一个瘦长的面板「压扁」成更接近屏幕比例的形状，缩小后空间利用率更高。这一步由 `optimalTextWidth` 计算。

#### 4.2.2 核心流程

```
maxAllowedWidth  = screenRect.width  * 0.95
maxAllowedHeight = screenRect.height * 0.95
requiresFullScreen = naturalPanelSize.width > maxAllowedWidth
                   OR naturalPanelSize.height > maxAllowedHeight

if requiresFullScreen:
  area = contentRect.width * contentRect.height        # 文字面积（近似守恒）
  screenRatio = maxAllowedWidth / maxAllowedHeight     # 目标长宽比
  optimalTextWidth = sqrt(area * screenRatio)          # 横排
                   或 sqrt(area / screenRatio)          # 竖排
  if optimalTextWidth > textWidth:                      # 只放宽，不收窄
    用 optimalTextWidth 重新排版，重算 contentRect / naturalPanelSize

  scaleX = maxAllowedWidth  / naturalPanelSize.width
  scaleY = maxAllowedHeight / naturalPanelSize.height
  scale  = min(scaleX, scaleY)                          # 取更紧的那一个
  panelRect.size = naturalPanelSize * scale
  panelRect.origin = 屏幕中心居中
```

**最优宽度的数学推导**（横排）：假设文字总面积 `area = w·h` 在换行时近似守恒，设排版宽度为 `w`，则高度 `h = area/w`。我们希望面板的长宽比贴合屏幕：`w/h = screenRatio`。代入得 `w/(area/w) = screenRatio`，即

\[
w^{2} = area \cdot screenRatio \quad\Rightarrow\quad w = \sqrt{area \cdot screenRatio}
\]

竖排由于旋转，宽高对调，目标是 `h/w = screenRatio`，即 `area/w^{2} = screenRatio`：

\[
w = \sqrt{\frac{area}{screenRatio}}
\]

这正是源码里 `vertical` 分支用 `sqrt(area / screenRatio)`、横排用 `sqrt(area * screenRatio)` 的由来。

#### 4.2.3 源码精读

先看 [是否需要全屏的判定](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L396-L399)：

```swift
let maxAllowedWidth = screenRect.width * 0.95
let maxAllowedHeight = screenRect.height * 0.95
let requiresFullScreen = naturalPanelSize.width > maxAllowedWidth || naturalPanelSize.height > maxAllowedHeight
```

只要任一维超过屏幕 95% 就进入全屏路径。接着是 [重新铺宽的逻辑](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L401-L430)：

```swift
if requiresFullScreen {
  let area = contentRect.width * contentRect.height
  let screenRatio = maxAllowedWidth / maxAllowedHeight
  let optimalTextWidth: CGFloat
  if vertical {
    optimalTextWidth = sqrt(area / screenRatio)
  } else {
    optimalTextWidth = sqrt(area * screenRatio)
  }
  if optimalTextWidth > textWidth {                 // 只在「更宽才有利」时重排
    textWidth = optimalTextWidth
    view.textContainer.size = NSSize(width: textWidth, height: .greatestFiniteMagnitude)
    view.textLayoutManager.ensureLayout(for: view.textLayoutManager.documentRange)
    contentRect = view.contentRect
    // 用新的 contentRect 重算 naturalPanelSize（横/竖两分支同上）
  }
}
```

注意 `if optimalTextWidth > textWidth` 这个守卫：它**只允许把文字铺得更宽，不允许收窄**。这是因为如果最优宽度比默认的 1/3 屏宽还小，说明文字根本没那么多，缩小宽度只会让面板更瘦长、更难看，所以保持原宽度不动。

最后是 [等比缩小并居中](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L434-L446)：

```swift
if requiresFullScreen {
  let scaleX = maxAllowedWidth / naturalPanelSize.width
  let scaleY = maxAllowedHeight / naturalPanelSize.height
  let scale = min(scaleX, scaleY)                   # 取两者中更小的，保证两维都不超屏

  panelRect.size = NSSize(width: naturalPanelSize.width * scale, height: naturalPanelSize.height * scale)
  panelRect.origin = NSPoint(
    x: screenRect.minX + (screenRect.width  - panelRect.width)  / 2,
    y: screenRect.minY + (screenRect.height - panelRect.height) / 2)
  maxHeight = 0                                     # 全屏模式重置宽度记忆
}
```

`scale = min(scaleX, scaleY)` 是这里的精髓：`scaleX` 是「只看宽度能放下的倍数」，`scaleY` 是「只看高度能放下的倍数」，取较小者保证**两个方向都不会溢出**。之后面板被钉在屏幕正中央，不再跟随光标（因为内容太多时光标附近肯定放不下，居中是最稳妥的选择）。

> 这里的缩小并没有真的去改文字字号或重排——下一节你会看到，它靠的是「frame 取缩小后的物理尺寸、bounds 保留自然尺寸」让 Cocoa 自动缩放绘制结果。

#### 4.2.4 代码实践

**实践目标**：手工验证 `scaleX/scaleY/scale` 的计算。

**操作步骤**：

1. 假设屏幕为 `screenRect = (0,0,1440,900)`，则 `maxAllowedWidth = 1368`、`maxAllowedHeight = 855`、`screenRatio ≈ 1.6`。
2. 假设某次超长输入量得 `naturalPanelSize = (900, 1200)`（宽 900、高 1200，高度已超 855）。
3. 手算：`scaleX = 1368/900 = 1.52`，`scaleY = 855/1200 = 0.7125`，`scale = min(...) = 0.7125`。
4. 算出 `panelRect.size = (900*0.7125, 1200*0.7125) = (641.25, 855)`，原点居中。

**需要观察的现象**：因为高度更紧，`scale` 取了高度方向的 `0.7125`；缩小后高度恰好贴满 `855`，宽度 `641` 远小于 `1368`，两维均不溢出。

**预期结果**：面板等比缩小到恰好被高度约束，居中显示。此为纯算术推导，可直接验证，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `scale` 取 `min(scaleX, scaleY)` 而不是 `max` 或平均？
**答案**：要保证缩小后**两个方向都不超过屏幕**。`scaleX` 是宽度方向的「安全倍数」，`scaleY` 是高度方向的；只有取较小者，才能同时满足两个方向的约束。取 `max` 会导致另一维溢出屏幕。

**练习 2**：如果 `optimalTextWidth` 算出来比当前 `textWidth` 还小，会发生什么？
**答案**：被 `if optimalTextWidth > textWidth` 守卫挡住，不会用更窄的宽度重排，保持原 `textWidth`。这避免把本来就不宽的面板进一步收窄成瘦长条。

---

### 4.3 光标附近定位与屏幕边界修正

#### 4.3.1 概念说明

普通模式（未触发全屏）下，面板应当像大多数输入法那样**贴着光标出现**：横排时压在光标上方，竖排时贴在光标左侧。但屏幕是有边的——光标若在屏幕右下角，直接贴上去面板就会跑出屏幕。所以定位分两步：**先按光标算出一个理想原点，再用屏幕边界把它推回可视区**，必要时把横排面板从「光标上方」翻转到「光标下方」。

`position` 是面板持有的一个 `NSRect`，它来自输入控制器在显示前通过 `IMKTextInput` 拿到的光标 caret 矩形，记录了光标在屏幕上的位置。

#### 4.3.2 核心流程

```
# 普通模式
panelRect.size = naturalPanelSize

if vertical:                                   # 竖排：贴在光标左侧
  if 光标在上半屏 (position.midY/height >= 0.5):
    origin.y = position.minY - offsetHeight - height + pagingOffset   # 光标下方
  else:
    origin.y = position.maxY + offsetHeight                           # 光标上方
  origin.x = position.minX - width - offsetHeight                     # 光标左侧
  若有 preedit：origin.x += preeditRect.height + edgeInset.width
else:                                          # 横排：压在光标正上方
  origin = (position.minX - pagingOffset, position.minY - offsetHeight - height)

# 边界修正
若 maxX > 屏幕右边：右贴边
若 minX < 屏幕左边：左贴边
若 minY < 屏幕底边：竖排→贴底；横排→翻转到光标上方 (origin.y = position.maxY + offsetHeight)
若 maxY > 屏幕顶边：顶贴边
若 minY < 屏幕底边（再次兜底）：贴底
```

#### 4.3.3 源码精读

先看 [竖排锚定](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L464-L475)：

```swift
if vertical {
  // Anchor vertical panels on one side of the cursor to avoid jumping while typing.
  if position.midY / screenRect.height >= 0.5 {
    panelRect.origin.y = position.minY - SquirrelTheme.offsetHeight - panelRect.height + theme.pagingOffset
  } else {
    panelRect.origin.y = position.maxY + SquirrelTheme.offsetHeight
  }
  panelRect.origin.x = position.minX - panelRect.width - SquirrelTheme.offsetHeight
  if view.preeditRange.length > 0, let preeditTextRange = view.convert(range: view.preeditRange) {
    let preeditRect = view.contentRect(range: preeditTextRange)
    panelRect.origin.x += preeditRect.height + theme.edgeInset.width
  }
}
```

竖排时面板永远贴在光标**左侧**（`origin.x = position.minX - width - offsetHeight`）。y 方向则按光标在屏幕的上/下半区选择锚点：上半屏时光标下方空间大，把面板顶边对齐到光标下方；下半屏时把面板底边对齐到光标上方。最后一处 `preeditRect` 修正很巧妙：竖排里 preedit（编码行）是横向铺开占宽度的，所以要把面板再往左挪一个 preedit 高度，避免面板与 preedit 文字重叠。

再看 [横排锚定](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L476-L478)：

```swift
} else {
  panelRect.origin = NSPoint(x: position.minX - theme.pagingOffset, y: position.minY - SquirrelTheme.offsetHeight - panelRect.height)
}
```

横排简单直接：面板底边贴在光标顶边上方一个 `offsetHeight` 间隙处，x 方向左移一个 `pagingOffset`（给左侧翻页箭头列让位，使第一个候选词大致对齐光标）。

最后是 [边界修正](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L480-L486)：

```swift
if panelRect.maxX > screenRect.maxX { panelRect.origin.x = screenRect.maxX - panelRect.width }
if panelRect.minX < screenRect.minX { panelRect.origin.x = screenRect.minX }
if panelRect.minY < screenRect.minY {
  if vertical { panelRect.origin.y = screenRect.minY } else { panelRect.origin.y = position.maxY + SquirrelTheme.offsetHeight }
}
if panelRect.maxY > screenRect.maxY { panelRect.origin.y = screenRect.maxY - panelRect.height }
if panelRect.minY < screenRect.minY { panelRect.origin.y = screenRect.minY }
```

逐条解读：

- **右边溢出**：把面板右边缘贴到屏幕右边缘（`origin.x = screenRect.maxX - width`），相当于整体左移。
- **左边溢出**：贴到屏幕左边缘。
- **底边溢出（光标在屏幕底部）**：竖排直接贴底；**横排则翻转到光标上方**——这是输入法常见的「光标在底行时面板自动翻到上面」行为，靠 `position.maxY + offsetHeight` 实现。
- **顶边溢出**：贴到屏幕顶边。
- 最后再来一次 `minY` 兜底，防止翻转后仍超出。

#### 4.3.4 代码实践

**实践目标**：验证横排面板在屏幕底行会自动翻转到光标上方。

**操作步骤**：

1. 阅读 [`show()` 的横排分支与边界修正](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L476-L486)。
2. 构造场景：假设 `screenRect = (0,0,1440,900)`，光标在底行 `position = (700, 5, 710, 25)`（光标顶边 25 离屏幕底 900 很远？注意原点在左下，所以「底行」其实是 `minY` 很小、靠近 0）。
3. 先按横排公式算 `origin.y = position.minY - 5 - height`，若 `height = 60`，则 `origin.y = 5 - 5 - 60 = -60`，`minY = -60 < 0`（屏幕底）。
4. 走第三条边界修正：横排分支 `origin.y = position.maxY + 5 = 25 + 5 = 30`，面板翻到光标上方。

**需要观察的现象**：原本算出的 `origin.y` 为负（顶出屏幕底部），被修正为正数，面板出现在光标上方而非下方。

**预期结果 / 待本地验证**：在屏幕底行唤起输入法，面板应出现在光标上方。GUI 行为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：横排面板默认出现在光标的上方还是下方？靠哪个 `offsetHeight` 控制间隙？
**答案**：默认在**上方**（`origin.y = position.minY - offsetHeight - height`，面板底边在光标顶边之上）。间隙由常量 [`SquirrelTheme.offsetHeight = 5`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L11) 控制。

**练习 2**：为什么竖排在光标位于屏幕上半屏和下半屏时，y 锚点算式不同？
**答案**：为了把面板放在光标附近**空间更大的一侧**，减少被屏幕边界裁切的可能，也避免面板在打字过程中因边界修正而频繁跳动。上半屏走「光标下方」锚点，下半屏走「光标上方」锚点。

---

### 4.4 垂直旋转与 translucency 背景视图

#### 4.4.1 概念说明

前面三节算出的 `panelRect` 是面板在屏幕上的**物理**位置和大小。但「绘制」用的坐标系可以和物理坐标系分离——Squirrel 利用这一点同时解决了两个问题：

1. **全屏缩放**：物理 frame 取缩小后的 `panelRect.size`，绘制 bounds 保留 `naturalPanelSize`，Cocoa 自动把大坐标系的绘制结果缩进小 frame。
2. **竖排**：把 contentView 的 bounds 旋转 `-90°`，原本横排的文字绘制就整体立起来变成竖排，无需逐字重排。

此外，主题可开启 `translucency`（毛玻璃半透明）。此时面板背后会垫一个系统提供的模糊视图（macOS 26+ 用 `NSGlassEffectView`，旧版用 `NSVisualEffectView`），并用 `view.shape`（u4-l1 中由 `draw()` 构造的面板轮廓路径）作为它的遮罩，让模糊只在圆角面板形状内透出。

#### 4.4.2 核心流程

```
setFrame(panelRect, display: true)             # 1. 物理尺寸定下来

contentView.frame  = (origin 0, size panelRect.size)        # 物理（可能缩小后）
contentView.bounds = (origin 0, size naturalPanelSize)      # 自然绘制坐标

if vertical:
  contentView.boundsRotation = -90
  contentView.setBoundsOrigin((0, naturalPanelSize.width))  # 旋转后修正原点
else:
  contentView.boundsRotation = 0
  contentView.setBoundsOrigin(.zero)

textView.boundsRotation = 0; setBoundsOrigin(.zero)         # textView 自身不旋转
subviewFrame = contentView.bounds                           # 读旋转后的 bounds（宽高已对调）
view.frame = subviewFrame
textView.frame = subviewFrame 左移 pagingOffset

if translucency:
  back.frame = subviewFrame（含 pagingOffset 列）
  back.appearance = 系统外观; back 显示
else:
  back 隐藏

alphaValue = theme.alpha; invalidateShadow(); orderFront(nil)
```

#### 4.4.3 源码精读

先看 [frame 与 bounds 的分离](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L489-L501)：

```swift
self.setFrame(panelRect, display: true)

// Keep the window frame at the scaled physical size while drawing in natural coordinates through bounds.
contentView!.frame = NSRect(origin: .zero, size: panelRect.size)
contentView!.bounds = NSRect(origin: .zero, size: naturalPanelSize)

if vertical {
  contentView!.boundsRotation = -90
  contentView!.setBoundsOrigin(NSPoint(x: 0, y: naturalPanelSize.width))
} else {
  contentView!.boundsRotation = 0
  contentView!.setBoundsOrigin(.zero)
}
```

这段是全讲义最值得品味的几行：

- **缩放即 frame/bounds 差**：当触发全屏缩放时，`panelRect.size` 是缩小后的物理尺寸，而 `bounds` 仍是 `naturalPanelSize`。Cocoa 发现 `bounds` 比容器 `frame` 大，便自动把 bounds 内的绘制内容缩放进 frame——这就是上一节「`scale` 缩小」真正落地的地方，**没有改动任何字号或重排**。
- **`boundsRotation = -90`**：把 contentView 的坐标系顺时针旋转 90°。旋转后，原来的 x 轴指向变成 y 轴方向，横排文字就「立」了起来。但旋转会让原点发生偏移（旋转中心是 bounds 原点），所以紧接着用 `setBoundsOrigin((0, naturalPanelSize.width))` 把原点平移到正确位置——这里的 `naturalPanelSize.width` 正是旋转后需要补偿的位移量（旋转把原来的宽度方向叠到了高度轴上）。横排时旋转归零、原点也归零。

> 关于 `setBoundsOrigin` 为什么要传 `(0, naturalPanelSize.width)`：`-90°` 旋转把内容坐标的 `(x,y)` 映射到物理坐标的 `(y, -x)`，旋转后内容会整体跑到负坐标区域被裁掉。把 bounds 原点设为 `(0, width)` 等于把内容向上平移一个「原宽度」，使其重新落回可视区。这是 Cocoa 旋转视图时的标准补偿手法。

接着是 [子视图按旋转后尺寸布局](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L503-L513)：

```swift
view.textView.boundsRotation = 0
view.textView.setBoundsOrigin(.zero)

// Subviews must read the post-rotation bounds; Cocoa adjusts the origin and swaps dimensions in vertical mode.
let subviewFrame = contentView!.bounds
view.frame = subviewFrame

var textFrame = subviewFrame
textFrame.size.width -= theme.pagingOffset
textFrame.origin.x += theme.pagingOffset
view.textView.frame = textFrame
```

注意 `textView` 自身的 `boundsRotation` 被**显式重置为 0**——旋转由父级 contentView 统一负责，textView 不再二次旋转。然后读取 `contentView.bounds`（竖排时 Cocoa 已自动把宽高对调），作为 `view` 和 `textView` 的 frame。`textView` 比 `view` 左移并收窄一个 `pagingOffset`，给翻页箭头列留出位置（箭头由 u4-l3/u4-l4 的 `pagingLayer` 在那块区域绘制）。

最后是 [半透明背景](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L515-L528)：

```swift
if theme.translucency {
  var backFrame = subviewFrame
  backFrame.size.width += theme.pagingOffset
  back.frame = backFrame
  back.appearance = NSApp.effectiveAppearance
  back.isHidden = false
} else {
  back.isHidden = true
}

alphaValue = theme.alpha
invalidateShadow()
orderFront(nil)
```

`back` 视图在面板 [`init`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L45-L46) 时就被设过 `back.layer?.mask = view.shape`，所以模糊效果只透出面板形状（含翻页箭头）。`back.appearance` 跟随系统亮暗，保证毛玻璃颜色与系统一致。它的 frame 比 textView 多出一个 `pagingOffset` 宽度，覆盖到翻页列。结束时按主题 [`alpha`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L63) 设置整体不透明度，刷新阴影，面板登台。

[`makeBackgroundView()`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L553-L565) 根据系统版本选择不同的毛玻璃实现：

```swift
if #available(macOS 26.0, *) {
  let glassView = NSGlassEffectView()
  glassView.style = .clear
  return glassView
} else {
  let visualEffectView = NSVisualEffectView()
  visualEffectView.blendingMode = .behindWindow
  visualEffectView.material = .hudWindow
  visualEffectView.state = .active
  return visualEffectView
}
```

新版用 `NSGlassEffectView`，旧版退回 `NSVisualEffectView` + `.hudWindow` 材质 + 窗后混合模式。

#### 4.4.4 代码实践

**实践目标**：理解 `boundsRotation = -90` 后 `setBoundsOrigin` 为何要传 `(0, naturalPanelSize.width)`。

**操作步骤**：

1. 在 [垂直旋转分支](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L495-L498) 处，把 `setBoundsOrigin` 的参数临时改成 `.zero`（示例修改，验证后还原）。
2. 在配置里开启竖排（`style/text_orientation: vertical`）。
3. 唤起面板观察。

**需要观察的现象**：原点未补偿时，竖排文字可能整体错位或部分被裁到可视区外；恢复 `(0, naturalPanelSize.width)` 后正常。

**预期结果 / 待本地验证**：补偿值正确时竖排面板显示正常，文字可见且方向正确。GUI 行为「待本地验证」。若无法运行，可改用「源码阅读型实践」：对照注释「Cocoa adjusts the origin and swaps dimensions in vertical mode」，画出 `-90°` 旋转前后坐标轴的对应关系，论证平移量应为「原宽度」。

#### 4.4.5 小练习与答案

**练习 1**：全屏缩小时，文字字号有变化吗？缩放是通过什么机制实现的？
**答案**：字号**没有**变化。缩放靠 `contentView.frame`（物理缩小尺寸）与 `contentView.bounds`（自然绘制尺寸）不一致，由 Cocoa 自动把 bounds 内的绘制内容缩放进 frame 来实现，是一种纯粹的坐标变换。

**练习 2**：`back` 背景视图的可见区域为什么是圆角面板形状而不是整个矩形？
**答案**：因为 `init` 中设置了 `back.layer?.mask = view.shape`，而 `view.shape` 是 u4-l1 里由 `draw()` 构造的面板轮廓路径（含翻页箭头）。遮罩让模糊视图只在路径范围内可见。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「面板定位全流程追踪」。

**任务**：构造一个假想场景并手工推演 `show()` 的关键计算，绘制一张从「文本已排版」到「面板登台」的流程图。

**给定条件**：

- 横排模式，`screenRect = (0,0,1440,900)`，`edgeInset = (6,6)`，`pagingOffset = 0`（未开翻页），`offsetHeight = 5`。
- `maxTextWidth()` 算出 `textWidth = 470`；排版后量得 `contentRect = (0,0,470,360)`。

**步骤**：

1. **测自然尺寸**（4.1）：`naturalPanelSize = (470 + 6*2 + 0, 360 + 6*2) = (482, 372)`。
2. **判全屏**（4.2）：`maxAllowed = (1368, 855)`，`482 < 1368` 且 `372 < 855`，不触发全屏。
3. **光标定位**（4.3）：假设光标 `position = (200, 400, 210, 420)`（位于屏幕中部偏左），横排 `origin = (200 - 0, 400 - 5 - 372) = (200, 23)`。
4. **边界修正**：`minY=23 > 0`、`maxY=23+372=395 < 900`、`maxX=200+482=682 < 1440`，均不溢出，原点不变。
5. **登台**（4.4）：`setFrame` 后，横排 `boundsRotation = 0`，bounds 与 frame 同为 `(482,372)`（无缩放），`orderFront` 显示。

**进阶追问**：若把 `contentRect` 的高度改成 `900`（候选极多），重走第 2~4 步——你会发现触发全屏，算出 `scale = min(1368/482, 855/912) = min(2.84, 0.937) = 0.937`，面板缩小并居中。把这组数字填进你画的流程图，本讲就彻底通了。

> 本任务为纯算术推演，可在纸面/表格中完成验证，不依赖运行环境。

## 6. 本讲小结

- `show()` 是面板登台的唯一出口，先用 `maxTextWidth()` 给文字一个宽度上限（横排约屏宽 1/3、竖排 1/4），再让 TextKit 在「宽度受限、高度不限」下排版，用 `view.contentRect` 量出自然尺寸。
- 自然尺寸 = 文字包围盒 + 双倍内边距（+ 翻页箭头列 `pagingOffset`）；竖排因 `-90°` 旋转，宽高在计算时整体对调。
- 当自然尺寸超过屏幕 95%，进入全屏路径：先用面积守恒推导 `optimalTextWidth = sqrt(area·ratio)`（竖排为 `sqrt(area/ratio)`）把文字铺宽，再取 `scale = min(scaleX, scaleY)` 等比缩小并居中。
- 普通模式下，横排面板压在光标上方、竖排贴在光标左侧，y 方向按光标在屏幕上/下半区选择锚点；屏幕边界会把溢出面推回，横排在底行时翻转到光标上方。
- 缩放与旋转都靠 **frame（物理）与 bounds（绘制坐标）分离** 实现：frame 取缩小后尺寸、bounds 保留自然尺寸即得缩放；竖排把 `boundsRotation` 设为 `-90` 并用 `setBoundsOrigin((0, width))` 补偿原点。
- `translucency` 开启时垫入 `NSGlassEffectView`/`NSVisualEffectView` 毛玻璃背景，用 `view.shape` 作遮罩让模糊只透出面板圆角形状。

## 7. 下一步学习建议

本讲讲清了「面板画在哪、多大、怎么缩放与旋转」，但**面板里那些圆角矩形、高亮块、翻页箭头究竟是怎么用 Core Graphics 画出来的**仍是黑盒。建议继续：

- **u4-l3 SquirrelView 自定义绘制**：精读 [`SquirrelView.draw(_:)`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L130-L301)，看 `backgroundPath`/`highlightedPath`/`candidatePaths` 如何由 `drawSmoothLines` 生成贝塞尔圆角路径，并理解 `shape.path` 为何同时充当本讲 `back.layer?.mask` 的遮罩。
- **u4-l4 面板鼠标与滚轮事件**：精读 [`SquirrelPanel.sendEvent`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L68-L143) 与 [`SquirrelView.click`](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L303-L341)，弄清点击如何命中本讲建立的 `shape` 区域并回调 `selectCandidate/page/moveCaret`。

回顾本讲时，建议把「frame vs bounds」「自然尺寸 vs 物理尺寸」「横排锚定 vs 竖排锚定」这三组对照关系记在笔记里，它们是理解整个面板模块的钥匙。
