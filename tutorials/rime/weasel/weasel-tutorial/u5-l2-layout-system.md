# 布局系统：Layout 与多种排布

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 Weasel 候选窗口里「画在哪里」（布局）与「怎么画」（DirectWrite 绘制）是如何解耦的，以及 `Layout` 抽象基类在这套分工里扮演的角色。
- 看懂 `Layout` → `StandardLayout` → `HorizontalLayout / VerticalLayout / VHorizontalLayout` 这条多态继承链，以及 `FullScreenLayout` 作为「装饰器」包装内层布局的特殊用法。
- 读懂 `_CreateLayout()` 这座工厂如何依据 `UIStyle.layout_type` 选出具体布局类。
- 自己动手仿照 `HorizontalLayout` 设计一个新的布局类（例如「双列候选」），并知道需要在哪些几何接口与工厂里接线。

本讲是 u5 单元的第二讲，承接 [u5-l1 候选窗口外壳 WeaselPanel](u5-l1-weasel-panel-window-and-interaction.md)——上一讲讲了 `WeaselPanel` 这个分层窗口如何接收鼠标/键盘、如何双缓冲上屏；本讲钻进它持有的 `m_layout` 成员，看候选、写作串、状态图标到底是怎么被摆放到屏幕像素上的。文字绘制本身（DirectWrite 画刷、圆角路径、阴影模糊）留到 [u5-l3 DirectWrite 资源与文本绘制]。

## 2. 前置知识

- **候选窗口外壳**：阅读过 u5-l1，知道 `WeaselPanel::Refresh()` → `_CreateLayout()` → `DoLayout()` → `_ResizeWindow()` 这条刷新链。
- **数据契约**：阅读过 [u2-l4 数据模型与 boost 序列化](u2-l4-data-model-and-serialization.md)，知道 `Context`（含 `preedit`/`aux`/`cinfo`）、`Status`、`UIStyle` 这几张表，以及 `CandidateInfo` 里 `candies / comments / labels` 三个按下标对齐的数组。
- **坐标系约定**：本讲所有矩形都基于「内容区（content area）」，内容区左上角恒为 `(0, 0)`。窗口最终的位置由 `WeaselPanel` 的光标跟随逻辑（`MoveTo`）决定，与布局内部坐标无关。
- **一点 GDI+/DirectWrite 直觉**：`CRect` 是左上右下四整数矩形；`CDCHandle` 是设备上下文句柄；`PDWR` 是 `DirectWriteResources` 的共享指针，负责真正的文字测量与绘制资源。本讲只把 DirectWrite 当作「给我一段文字，我返回它的宽高」的黑盒。

> 名字预警：`VHorizontalLayout` 这个名字容易误导。它对应的是 `LAYOUT_VERTICAL_TEXT`（**竖排文字**），候选之间**横向排列**，每个候选内部文字竖着写。所以「V + Horizontal」=「竖排文字（Vertical text）+ 横向（Horizontal）候选流」。读到后面你会反复用到这条记忆。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [WeaselUI/Layout.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/Layout.h) | 抽象基类 `Layout`，定义全部纯虚几何接口；还放圆角路径 `GraphicsRoundRectPath` 与全屏判定宏。 |
| [WeaselUI/Layout.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/Layout.cpp) | `Layout` 构造函数：DPI 缩放、`real_margin`、阴影 `offsetX/Y` 的统一计算。 |
| [WeaselUI/StandardLayout.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/StandardLayout.h) / [.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/StandardLayout.cpp) | 中间层：缓存所有矩形的 getter、文字测量 `GetTextSizeDW`、圆角信息 `_PrepareRoundInfo`、状态图标排布 `UpdateStatusIconLayout`。 |
| [WeaselUI/HorizontalLayout.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/HorizontalLayout.cpp) | 横向排布（含按 `max_width` 自动换行成多行）。 |
| [WeaselUI/VerticalLayout.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/VerticalLayout.cpp) | 竖向排布：候选逐行堆叠，注释单列右对齐。 |
| [WeaselUI/VHorizontalLayout.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/VHorizontalLayout.cpp) | 竖排文字（`LAYOUT_VERTICAL_TEXT`），含 `DoLayoutWithWrap` 多列换行子模式。 |
| [WeaselUI/FullScreenLayout.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/FullScreenLayout.cpp) | 全屏装饰器：包装内层布局，按显示器工作区二分搜索缩放字号并居中。 |
| [WeaselUI/WeaselPanel.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp) | 工厂 `_CreateLayout` 与绘制消费方 `DoPaint`。 |
| [include/WeaselIPCData.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h) | `LayoutType` 枚举、`LayoutAlignType` 枚举与 `UIStyle` 中的布局相关字段。 |

---

## 4. 核心概念与源码讲解

### 4.1 Layout 抽象与多态

#### 4.1.1 概念说明

候选窗口的绘制其实可以拆成两个正交的问题：

1. **「画在哪里」**：第 `i` 个候选的高亮背景是哪个矩形？写作串（preedit）摆在窗口哪个位置？状态图标放哪？翻页箭头 `<` `>` 放哪？
2. **「怎么画」**：用哪个 DirectWrite 画刷、圆角半径多少、要不要加阴影模糊？

如果把这两件事混在一个函数里，代码会迅速变成「水平排布 + DirectWrite 画水平」「竖直排布 + DirectWrite 画竖直」这种笛卡尔积式的重复。Weasel 的做法是把第一件事**完全抽离**成一个纯几何层——`Layout` 类。它只回答「矩形坐标是什么」，完全不知道 DirectWrite 的存在。`WeaselPanel` 的绘制函数则只管「拿着这些矩形，把字画进去」。

这样一来，新增一种排布（比如双列、九宫格）只需要新写一个 `Layout` 子类，绘制代码一行都不用动。这就是 `Layout` 抽象基类存在的根本理由。

多态链是这样的（自顶向下）：

```
Layout                      // 纯抽象：~20 个纯虚几何接口
  └─ StandardLayout         // 中间层：缓存矩形 + 共享工具函数
       ├─ HorizontalLayout  // 只实现 DoLayout
       ├─ VerticalLayout    // 只实现 DoLayout
       ├─ VHorizontalLayout // 只实现 DoLayout（+ DoLayoutWithWrap）
       └─ FullScreenLayout  // 装饰器：内部再持有一个 Layout*
```

注意两个关键设计：

- **`StandardLayout` 把所有 getter 落地**：子类只重写 `DoLayout` 一个虚函数，把算好的矩形写进 `StandardLayout` 的成员数组（如 `_candidateRects[i]`），getter 直接返回缓存。这避免了每个子类都要重写一遍 `GetCandidateRect`。
- **`FullScreenLayout` 是装饰器模式**：它自己继承 `StandardLayout`，但内部还握着一个 `Layout* m_layout`（被包装的内层布局）。它把真正的排布委托给内层，自己只负责「缩放字号让它塞进全屏」和「整体居中偏移」。详见 4.2.4。

#### 4.1.2 核心流程

`Layout` 的生命周期由 `WeaselPanel` 驱动，每个按键回合跑一遍：

```
WeaselPanel::Refresh()
   ├─ _InitFontRes()          // 按需创建/刷新 DirectWriteResources（pDWR）
   ├─ _CreateLayout()         // 工厂：按 layout_type new 出对应子类，赋给 m_layout
   ├─ m_layout->DoLayout(dc, pDWR)   // 子类把所有矩形算好、缓存进成员
   ├─ _ResizeWindow()         // 用 GetContentSize() 调整窗口尺寸
   └─ RedrawWindow() → DoPaint()     // 绘制时反复调各种 GetXxxRect()
```

构造阶段（在 `Layout` 基类构造函数里）会做两件影响**所有**子类的事：

1. **DPI 缩放**：把 `_style` 里所有像素量（`margin_x`、`spacing`、`round_corner`、`shadow_radius`……）乘以 `pDWR->dpiScaleLayout`（即 `dpi/96`）。于是子类里写 `_style.spacing` 时已经是缩放后的真值，无需再算。
2. **统一算 `real_margin` 与 `offsetX/Y`**：
   - `real_margin_x = max(|margin_x|, hilite_padding_x)`：内容到边框的真实内边距。取二者最大，保证即使 `margin_x` 很小，高亮内边距 `hilite_padding_x` 仍有伸展空间。
   - `offsetX = |shadow_offset_x| + shadow_radius*2 + border*2`：为阴影和边框在内容四周预留的「画布外圈」。所有候选/preedit 矩形都从 `(offsetX, offsetY)` 起算，而 `_contentSize` 把 `offsetX` 算进总宽高，这样阴影画出来不会被裁掉。

#### 4.1.3 源码精读

抽象基类 `Layout` 的全部纯虚接口——注意它们清一色返回 `CRect`/`CSize`，没有任何绘制相关类型，印证了「纯几何层」的定位：

[WeaselUI/Layout.h:72-103](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/Layout.h#L72-L103) 定义「内容区左上角恒为 (0,0)」的坐标系约定，以及 `DoLayout`（唯一需要子类实现的计算入口）和一整套 `GetXxxRect()` 纯虚函数。其中 `GetRoundInfo(i)` 返回的是「第 i 个候选的高亮背景该圆哪几个角」，把几何与圆角美学信息一并下发给绘制层。

构造函数里 DPI 缩放与边距/阴影预留的计算（这是整条继承链共享的初始化）：

[WeaselUI/Layout.cpp:20-56](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/Layout.cpp#L20-L56) 先把 `_style` 各像素量按 `dpiScaleLayout` 缩放（L20-39），再算 `real_margin_x/y = max(|margin|, hilite_padding)`（L40-45），最后算 `offsetX/Y` = 阴影预留 + `border*2`（L47-56）。这两组量是后续所有子类 `DoLayout` 里反复用到的「画布原点」与「内边距」。

`LayoutType` 枚举（驱动工厂选择）：

[include/WeaselIPCData.h:206-213](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L206-L213) 定义五种布局类型，前三种是「普通排布」，后两种是「全屏变体」。

工厂 `_CreateLayout`——把枚举翻译成具体子类的唯一地点：

[WeaselUI/WeaselPanel.cpp:110-132](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L110-L132) 依据 `layout_type` 选择布局类：`LAYOUT_VERTICAL_TEXT` → `VHorizontalLayout`（L115-116）；`LAYOUT_VERTICAL`/`LAYOUT_VERTICAL_FULLSCREEN` → `VerticalLayout`（L118-120）；`LAYOUT_HORIZONTAL`/`LAYOUT_HORIZONTAL_FULLSCREEN` → `HorizontalLayout`（L121-123）；最后若是全屏类型（`IS_FULLSCREENLAYOUT` 宏），再用 `FullScreenLayout` 把刚 new 出来的内层布局包一层（L126-129）。这条 `if-else` 就是你新增布局类后必须改动的接线点。

#### 4.1.4 代码实践

**实践目标**：在不动源码的前提下，确认你对「工厂 → 子类 → 缓存矩形 → getter」这条链的理解。

**操作步骤**：

1. 打开 [WeaselUI/WeaselPanel.cpp:110-132](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L110-L132)，把每个 `layout_type` 取值映射到具体 `new` 出的类。
2. 打开 [WeaselUI/StandardLayout.h:23-52](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/StandardLayout.h#L23-L52)，确认所有 getter（`GetCandidateRect`、`GetPreeditRect`……）都是「返回成员变量」的一行函数，没有任何计算。
3. 在 `HorizontalLayout.cpp` 的 `DoLayout` 里搜索 `_candidateRects[i].SetRect`，确认子类确实把计算结果写进了基类的成员数组。

**需要观察的现象**：getter 不计算、只读缓存；所有「重活」都集中在 `DoLayout` 里。

**预期结果**：你能用一句话回答「为什么新增一种排布只需写一个 `DoLayout`」——因为其余接口都被 `StandardLayout` 用缓存兜住了。

#### 4.1.5 小练习与答案

**练习 1**：如果直接让 `HorizontalLayout` 继承 `Layout`（跳过 `StandardLayout`），会带来什么重复？
**答案**：你得在 `HorizontalLayout` 里重写 `GetCandidateRect`/`GetPreeditRect`/`GetCandidateLabelRect` 等全部约 20 个 getter，并自己声明对应的成员变量；而这些 getter 的实现（返回缓存）对所有排布都一样，会被 `VerticalLayout`/`VHorizontalLayout` 原样复制一遍。`StandardLayout` 正是为了消除这部分重复。

**练习 2**：为什么 `real_margin_x` 要取 `max(|margin_x|, hilite_padding_x)` 而不是直接用 `margin_x`？
**答案**：高亮候选背景会按 `hilite_padding_x` 向外膨胀（见 `WeaselPanel` 绘制处的 `rect.InflateRect(hilite_padding_x, hilite_padding_y)`）。如果 `margin_x` 比 `hilite_padding_x` 小，膨胀后的高亮就会顶到甚至越过边框。取最大值保证内边距同时满足「用户设定的 margin」与「高亮膨胀所需的空间」。

---

### 4.2 水平、竖直与竖排文字三种排布

三种「普通」排布都继承 `StandardLayout`、只实现 `DoLayout`。它们的差异本质上是**主轴方向**与**候选内部分区**的不同。下面用一个表先建立直觉，再逐一精读。

| 布局类 | `layout_type` | 主轴 | 候选内部 | 是否多行/多列换行 |
| --- | --- | --- | --- | --- |
| `HorizontalLayout` | `LAYOUT_HORIZONTAL` | 横向（→） | label‖text‖comment 横排 | 按 `max_width` 换行成多行 |
| `VerticalLayout` | `LAYOUT_VERTICAL` | 竖向（↓） | label‖text 在左，comment 单独成列右对齐 | 单列，不换行 |
| `VHorizontalLayout` | `LAYOUT_VERTICAL_TEXT` | 横向（→），但文字竖排 | label/text/comment 在每个候选内**竖向堆叠** | `vertical_text_with_wrap` 时按 `max_height` 换成多列 |

> 默认值：`UIStyle` 构造时 `layout_type(LAYOUT_VERTICAL)`（[include/WeaselIPCData.h:318](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L318)），所以不配置时小狼毫是竖排。

#### 4.2.1 概念说明

每种排布都要回答同样的几个问题，只是答案不同：

1. **写作串 preedit 放哪**：通常在最上方（横排/竖排）或最左侧（竖排文字）。`IsInlinePreedit()` 为真时（TSF 内联模式）则完全不在窗口里画 preedit。
2. **辅助串 aux 放哪**：紧跟 preedit 之后。
3. **每个候选的三段（label / text / comment）如何摆放**、它们之间的 `hilite_spacing` 与候选之间的 `candidate_spacing` 怎么算。
4. **整体宽高 `_contentSize` 如何收口**：要满足 `min_width/min_height`，受 `max_width/max_height` 约束（用于换行）。
5. **圆角信息 `_roundInfo` 与状态图标**：交给 `StandardLayout` 的 `_PrepareRoundInfo` 与 `UpdateStatusIconLayout` 统一收尾。

#### 4.2.2 核心流程

以 `HorizontalLayout::DoLayout` 为典型，所有子类的 `DoLayout` 都遵循同一个七段式骨架：

```
DoLayout(dc, pDWR):
  1. 算 mark_text（高亮候选左侧的小标记）尺寸、page 指示器(< >)尺寸
  2. 若非内联：放 preedit 矩形 → 累加 height
  3. 放 auxiliary 矩形 → 累加 height
  4. for 每个候选 i：测 label/text/comment → SetRect 进 _candidateXxxRects[i]
     （横排：横向累加 w；超出 max_width 则换行）
  5. 按 align_type 对齐（center/bottom/top），合成 _candidateRects[i]
  6. 收口：算 _contentSize、_contentRect、_highlightRect = _candidateRects[id]
  7. UpdateStatusIconLayout(&width, &height)；_PrepareRoundInfo(dc)
```

竖排与竖排文字的差异主要在第 4、5 步的累加方向与对齐方式，骨架完全一致。文字测量统一走 `StandardLayout::GetTextSizeDW`，它内部按 `layout_type` 决定 DirectWrite 的阅读方向（横排左→右，竖排文字上→下）。

#### 4.2.3 源码精读

**横向排布**的候选主循环与多行换行逻辑：

[WeaselUI/HorizontalLayout.cpp:84-164](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/HorizontalLayout.cpp#L84-L164) 逐个候选测出 label/text/comment 宽度并 `SetRect`（L92-124）；当某个候选右端超过 `_style.max_width` 且不是行首时（L130-132），把该候选整体 `OffsetRect` 挪到下一行（L136-151），并用 `row_of_candidate[i]` 记录它属于第几行。这段是「按需换行」的核心。

横向排布的对齐与收口：

[WeaselUI/HorizontalLayout.cpp:166-224](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/HorizontalLayout.cpp#L166-L224) 按 `align_type`（`ALIGN_CENTER`/`ALIGN_BOTTOM`，默认 `ALIGN_BOTTOM`）在行高内竖直居中或贴底（L176-196）；把每行最右候选的右边对齐到统一位置以美观（L212-219）；最后 `_highlightRect = _candidateRects[id]`（L221），`_contentSize` 收口（L223）。

**竖向排布**的候选逐行堆叠与「注释单列右对齐」：

[WeaselUI/VerticalLayout.cpp:85-163](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/VerticalLayout.cpp#L85-L163) 每个候选占一行，`height` 向下累加（L87）；label 和 text 在左侧紧挨（L92-111），comment 先临时放在左侧（L119-130），同时用 `comment_shift_width = max(...)` 记下「有注释的候选里最长的 label+text 宽度」（L119）。随后第二轮循环把所有注释统一平移到这个 `comment_shift_width` 位置，形成右对齐的注释列——这就是小狼毫竖排里「候选词与注释各成一列、注释左边对齐」的来源：

[WeaselUI/VerticalLayout.cpp:171-189](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/VerticalLayout.cpp#L171-L189) 把每个 `_candidateCommentRects[i]` 横向 `OffsetRect` 到 `comment_shift_width`，再据此重算跨整行的 `_candidateRects[i]`（左右各留 `real_margin_x`）。

**竖排文字**的两个子模式入口：

[WeaselUI/VHorizontalLayout.cpp:7-11](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/VHorizontalLayout.cpp#L7-L11) 在 `DoLayout` 最开头判断 `vertical_text_with_wrap`：为真则走 `DoLayoutWithWrap`（多列换行），否则走单行竖排文字。竖排文字之所以「文字竖着写」，靠的是 DirectWrite 的阅读方向被设成「上→下」——这件事在共享的 `GetTextSizeDW` 里统一处理：

[WeaselUI/StandardLayout.cpp:29-42](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/StandardLayout.cpp#L29-L42) 当 `layout_type == LAYOUT_VERTICAL_TEXT` 时，把 DirectWrite 布局的阅读方向设为 `TOP_TO_BOTTOM`、流向按 `vertical_text_left_to_right` 设为左→右或右→左。于是同一段测量代码既能测横排也能测竖排文字，布局类无需关心。

`DoLayoutWithWrap` 的多列换行与竖排文字的方向修正（右→左时要把整个候选序列镜像）：

[WeaselUI/VHorizontalLayout.cpp:329-505](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/VHorizontalLayout.cpp#L329-L505) 候选在列内竖向堆叠、超出 `max_height` 换列（L377-393），结构上与横向排布的多行换行对称，只是轴互换；最后若 `!vertical_text_left_to_right`，还要把所有列整体重新排列成右→左顺序（L457-505）。

#### 4.2.4 源码精读：FullScreenLayout 装饰器

全屏布局（`LAYOUT_VERTICAL_FULLSCREEN` / `LAYOUT_HORIZONTAL_FULLSCREEN`）不是一个从零开始的排布，而是「拿一个普通排布，把字号缩到能塞进屏幕、再整体居中」。它通过持有内层 `Layout* m_layout` 实现装饰器模式：

[WeaselUI/FullScreenLayout.cpp:6-66](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/FullScreenLayout.cpp#L6-L66) 取光标所在显示器的工作区 `workArea`（L14-22），然后 `do { m_layout->DoLayout(...) } while (AdjustFontPoint(...))`（L25-27）反复重排，直到内层布局的内容尺寸放进工作区。之后把内层的所有矩形 `OffsetRect(offsetx, offsety)` 居中偏移后拷贝到自己的同名成员（L32-61），最后把自己的 `_contentSize` 直接设成整个工作区大小（L63）。所以对外它和普通布局接口完全一致，绘制层无感知。

[WeaselUI/FullScreenLayout.cpp:68-123](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/FullScreenLayout.cpp#L68-L123) `AdjustFontPoint` 是一个二分搜索：若内容超出工作区就把步长 `step` 取半并取负、字号减 `step`；若内容太 小（小于工作区的 31/32）则把步长取半取正、字号加 `step`；否则返回 `false` 收敛。每次调整都调用 `pDWR->InitResources(...)` 重建 DirectWrite 字体资源。初始 `step = 32`（[FullScreenLayout.cpp:24](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/FullScreenLayout.cpp#L24)）。

#### 4.2.5 代码实践

**实践目标**：用读源码的方式，验证三种排布在「同一份输入」下产出的矩形差异。

**操作步骤**：

1. 假设输入：3 个候选，label=`1./2./3.`，text=`你好/世界/测试`，无 comment，无 preedit。
2. 在 `HorizontalLayout.cpp` 里跟踪第 1 个候选：`_candidateLabelRects[0]`、`_candidateTextRects[0]`、`_candidateRects[0]` 的 `left/top/right/bottom` 是怎么随着 `w` 和 `height` 累加出来的（[L86-124](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/HorizontalLayout.cpp#L86-L124)）。
3. 切到 `VerticalLayout.cpp`，同样跟踪第 1 个候选（[L85-130](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/VerticalLayout.cpp#L85-L130)），比较 `_candidateRects[0]` 的宽：横排下它只包住「1. 你好」，竖排下它横跨整行（`real_margin_x` 到 `width - real_margin_x`）。

**需要观察的现象**：横排候选矩形窄而高（彼此并排），竖排候选矩形宽而矮（逐行铺满），竖排文字候选矩形则是一个个竖向小列。

**预期结果**：你能画出三种排布下「3 个候选矩形」的示意草图，并标注 `candidate_spacing` 出现在候选与候选之间。

> 本实践为源码阅读型，未实际运行；若要本地复现，可在 `weasel.custom.yaml` 设 `style/layout: horizontal`（或 `vertical` / `vertical_text`），重新部署后观察候选窗口外观。

#### 4.2.6 小练习与答案

**练习 1**：竖排（`VerticalLayout`）为什么不支持「多行换行」？
**答案**：竖排每个候选独占一整行（`_candidateRects[i]` 横跨 `real_margin_x..width-real_margin_x`，见 [VerticalLayout.cpp:187-188](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/VerticalLayout.cpp#L187-L188)），主轴是垂直方向、候选天然向下堆叠，没有「这一行放不下换到下一行」的概念。需要换行能力时用横排（按 `max_width` 换行）或竖排文字的 `vertical_text_with_wrap`（按 `max_height` 换列）。

**练习 2**：`FullScreenLayout` 为什么必须持有 `Layout* m_layout` 而不是直接复制内层的矩形？
**答案**：它需要在调整字号后**重新跑一遍内层的 `DoLayout`**（[FullScreenLayout.cpp:25-27](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/FullScreenLayout.cpp#L25-L27)），而字号→尺寸的关系是非线性的，只能迭代求解。持有内层对象才能反复调用其 `DoLayout`；若只复制一次矩形，就无法在字号变化后重新计算。这也是它叫「装饰器」而非「子类」的原因——它复用的是内层的行为，不是内层的状态快照。

**练习 3**：`VHorizontalLayout` 的名字里「Horizontal」指什么？
**答案**：指候选之间的排列方向是横向（候选一个个向右排），区别于 `VerticalLayout` 的候选向下排。而「V」指每个候选内部的文字是竖排（Vertical text）。合起来：竖排文字 + 横向候选流。

---

### 4.3 几何接口与绘制协作

#### 4.3.1 概念说明

`Layout` 算完矩形后，自己一个字都不画。真正动笔的是 `WeaselPanel::DoPaint`（及其辅助函数 `_DrawPreedit`、`_DrawCandidates`、`_HighlightText` 等，属于 u5-l3）。绘制层和布局层的协作契约，就是 `Layout` 暴露的那组 getter。可以把它们分成几类来记：

| 分类 | 接口 | 绘制层用途 |
| --- | --- | --- |
| 窗口尺寸 | `GetContentSize()` | `_ResizeWindow` 据此设定窗口宽高 |
| 文本块位置 | `GetPreeditRect / GetAuxiliaryRect` | 决定写作串/辅助串画在哪 |
| 候选分区 | `GetCandidateLabelRect / GetCandidateTextRect / GetCandidateCommentRect(i)` | 分别画序号、候选词、注释 |
| 高亮 | `GetHighlightRect() = GetCandidateRect(highlighted)` | 画当前候选的高亮背景 |
| 背景/边框 | `GetContentRect()` | 画整个候选窗的圆角背景 |
| 状态图标 | `GetStatusIconRect() + ShouldDisplayStatusIcon()` | 画中/英文状态图标 |
| 翻页 | `GetPrepageRect / GetNextpageRect()` | 画 `<` `>` 翻页箭头 |
| 圆角美学 | `GetRoundInfo(i) / GetTextRoundInfo()` | 告诉绘制层这个矩形该圆哪几个角 |
| preedit 分段 | `GetPreeditRange + GetBeforeSize/HilitedSize/AfterSize` | 把写作串拆成「光标前/高亮段/光标后」三段分别着色 |
| 行为查询 | `IsInlinePreedit() / GetLabelText(...)` | 决定是否跳过 preedit、格式化序号文本 |

此外还有两个由 `StandardLayout` 提供、被各 `DoLayout` 在收尾时调用的「协作工具」：`UpdateStatusIconLayout`（摆状态图标）和 `_PrepareRoundInfo`（算圆角信息）。

#### 4.3.2 核心流程

绘制一帧的协作时序（简化）：

```
DoPaint(dc):
  backrc  = m_layout->GetContentRect()         // 整体背景
  画圆角背景 + 阴影(GdiplusBlur)
  if(!IsInlinePreedit()):
     range = GetPreeditRange(); before/hilit/after = GetBeforeSize/...
     preeditrc = GetPreeditRect()
     分三段画 preedit
  for i in 候选:
     rect = GetCandidateRect(i); rd = GetRoundInfo(i)
     _HighlightText(dc, rect, ..., rd)          // 用 rd 决定圆角
     画 label/text/comment = GetCandidateLabelRect/TextRect/CommentRect(i)
  if(ShouldDisplayStatusIcon()):
     画图标于 GetStatusIconRect()
  画翻页箭头于 GetPrepageRect()/GetNextpageRect()
```

关键点：**绘制层完全信任布局层给的矩形与圆角信息**，自己不做任何坐标计算。这就是 4.1 里说的「正交解耦」落地后的样子。

#### 4.3.3 源码精读

绘制层对几何接口的消费——以「画非高亮候选的阴影背景」为例：

[WeaselUI/WeaselPanel.cpp:805-820](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L805-L820) 对每个非高亮候选，先 `rect = m_layout->GetCandidateRect(i)`（L809）拿位置，再 `rd = m_layout->GetRoundInfo(i)`（L810）拿圆角信息，按 `hilite_padding` 膨胀后交给 `_HighlightText` 绘制（L815-818）。注意这里 `WeaselPanel` 没有自己算任何坐标，全是从 `m_layout` 取。

状态图标排布规则（`StandardLayout::UpdateStatusIconLayout`）——所有 `DoLayout` 收尾都会调它：

[WeaselUI/StandardLayout.cpp:317-378](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/StandardLayout.cpp#L317-L378) 注释里写明了三条规则：状态图标与 preedit/aux 文本中线对齐（取先出现者）；二者间留 `spacing`；当「margin+文本宽+spacing+图标宽+margin < min_width」时图标右对齐到窗口右侧（L350-368 是横排分支，L324-349 是竖排文字分支）。全屏布局还会额外把图标向内偏移一个 `border`（L376-377）。

圆角信息的准备（`_PrepareRoundInfo`）——决定每个候选高亮背景圆哪几个角：

[WeaselUI/StandardLayout.cpp:155-315](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/StandardLayout.cpp#L155-L315) 先用 `_IsHighlightOverCandidateWindow`（基于 GDI+ Region 的 XOR，[L133-152](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/StandardLayout.cpp#L133-L152)）判断某个高亮矩形是否「探出」了候选窗背景 `_bgRect`——探出意味着该角贴近窗口外缘，需要与窗口的 `round_corner_ex` 圆角对齐（即 `Hemispherical` 半圆顶）。随后按 `[布局类型][是否内联][候选位置 FIRST/MID/LAST/ONLY]` 四维查表 `is_to_round_corner`（[L207-253](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/StandardLayout.cpp#L207-L253)），为每个候选填好 `_roundInfo[i]` 的四个角布尔值。这就是小狼毫高亮背景能与窗口圆角「无缝贴合」的原理。

`GetLabelText`——把原始 label 文本按格式串格式化：

[WeaselUI/StandardLayout.cpp:6-12](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/StandardLayout.cpp#L6-L12) 用 `swprintf_s` 按 `label_text_format`（如 `"%s."` 或 `"%s "`）把 `labels[i].str` 格式化成显示用的序号串。这样用户在 `weasel.custom.yaml` 改 `style/label_text_format` 就能改变序号样式，布局与绘制都不用动。

`IsInlinePreedit` 与 `ShouldDisplayStatusIcon`——两个行为开关：

[WeaselUI/StandardLayout.cpp:380-397](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/StandardLayout.cpp#L380-L397) `IsInlinePreedit` 为真要求三件事同时成立：样式开了 `inline_preedit`、客户端（TSF）声明了 `INLINE_PREEDIT_CAPABLE` 能力位、且不是全屏布局（全屏强制在窗口内画）。`ShouldDisplayStatusIcon` 则综合「英文模式且非内联」「正在 composing」「有 aux 提示」等条件，并排除「全屏 + 有提示文本」的特例。

#### 4.3.4 代码实践

**实践目标**：把「布局算矩形 → 绘制取矩形」的协作链亲手走一遍，验证某个具体候选的最终落点。

**操作步骤**：

1. 在 [WeaselUI/WeaselPanel.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp) 搜索 `m_layout->GetCandidateRect`，统计它被调用了多少次、分别在哪几个绘制阶段（阴影、普通背景、高亮、hover）。
2. 选定一次调用（如 [L809](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L809)），反向追溯：这个 `rect` 接下来传给了 `_HighlightText`，而 `_HighlightText` 又用 `rd = GetRoundInfo(i)` 决定圆角路径。
3. 对照 `VerticalLayout.cpp` 里 `_candidateRects[i]` 的赋值处（[L187-188](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/VerticalLayout.cpp#L187-L188)），确认绘制层取到的 `rect` 正是布局层算出来的同一个对象。

**需要观察的现象**：同一个 `GetCandidateRect(i)` 在一次 `DoPaint` 里被多个绘制阶段重复调用，每次都返回同一份缓存值——没有重复计算。

**预期结果**：你能画出「`VerticalLayout::DoLayout` 写入 `_candidateRects[i]` → `WeaselPanel::DoPaint` 多次读 `GetCandidateRect(i)`」的数据流，确认两者通过缓存解耦。

#### 4.3.5 小练习与答案

**练习 1**：如果新增一个布局类，`GetRoundInfo` 不填（保留 `IsToRoundStruct` 默认全 `true`），会怎样？
**答案**：每个候选高亮背景的四角都会被圆角处理（默认构造见 [Layout.h:57-62](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/Layout.h#L57-L62)）。视觉上候选之间不会有「合并圆角」的效果，但功能正常、不会崩溃。`_PrepareRoundInfo` 的复杂查表是为了让相邻候选的高亮在拼接处共享直角、在外缘贴合窗口圆角，属于美化而非必需。

**练习 2**：`GetHighlightRect()` 和 `GetCandidateRect(highlighted)` 返回的矩形一样吗？
**答案**：一样。各 `DoLayout` 末尾都有 `_highlightRect = _candidateRects[id]`（如 [HorizontalLayout.cpp:221](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/HorizontalLayout.cpp#L221)、[VerticalLayout.cpp:215](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/VerticalLayout.cpp#L215)）。提供 `GetHighlightRect()` 只是为了调用方语义更清晰（「我要高亮那个」而非「我要第 id 个」）。

---

## 5. 综合实践

**任务**：仿照 `HorizontalLayout` 的结构，设计一个「双列候选」布局类 `TwoColumnLayout`——候选从上到下排成两列（左列放奇数候选、右列放偶数候选，或前半左列、后半右列），用伪代码写出它的 `DoLayout`，并说明要重写哪些接口、如何在 `_CreateLayout` 里接线。

**目标**：把本讲的三个最小模块——抽象多态、排布算法、几何接口协作——串起来用一次。

**参考解答（伪代码 + 接线说明）**：

```
// 1. 类声明：只重写 DoLayout，其余继承 StandardLayout
class TwoColumnLayout : public StandardLayout {
  void DoLayout(CDCHandle dc, PDWR pDWR) override;
  // 不需要重写任何 GetXxxRect —— StandardLayout 已用缓存兜底
  // 额外成员（可选）：记录每个候选属于左列还是右列
  int _colOfCandidate[MAX_CANDIDATES_COUNT];
};

// 2. DoLayout 伪代码
TwoColumnLayout::DoLayout(dc, pDWR):
  // (a) 复用 StandardLayout 的测量能力
  // 先算 mark_text / page 指示器尺寸（照抄 HorizontalLayout 开头）

  // (b) preedit / aux 放最上方，横跨两列（同 HorizontalLayout）
  if(!IsInlinePreedit() && preedit 非空):
      size = GetPreeditSize(...)
      _preeditRect.SetRect(offsetX+real_margin_x, height, ..., height+size.cy)
      height += size.cy + spacing

  // (c) 双列候选主循环
  leftX  = offsetX + real_margin_x
  rightX = leftX + columnWidth + candidate_spacing   // columnWidth 须先探测
  leftTop = height; rightTop = height
  for i in 0..candidates_count:
      // 决定本候选放左列还是右列：前半左、后半右
      col = (i < candidates_count/2) ? LEFT : RIGHT
      x   = (col==LEFT) ? leftX : rightX
      y   = (col==LEFT) ? leftTop : rightTop
      _colOfCandidate[i] = col

      // 测 label/text/comment（完全照抄 HorizontalLayout 的三段测量）
      w = x
      SetRect(_candidateLabelRects[i], w, y, ...);  w += labelW + hilite_spacing
      SetRect(_candidateTextRects[i],   w, y, ...);  w += textW
      if(有 comment): SetRect(_candidateCommentRects[i], w, y, ...)

      // 合成候选整体矩形
      _candidateRects[i].SetRect(x, y, w, y + lineHeight)
      // 推进对应列的游标
      if(col==LEFT) leftTop  += lineHeight + candidate_spacing
      else          rightTop += lineHeight + candidate_spacing

  // (d) 收口
  width  = max(leftColRight, rightColRight) + real_margin_x
  height = max(leftTop, rightTop) + real_margin_y
  width  = max(width, _style.min_width); height = max(height, _style.min_height)
  _highlightRect = _candidateRects[id]
  _contentSize.SetSize(width+offsetX, height+2*offsetY)
  _contentRect.SetRect(0,0,_contentSize.cx,_contentSize.cy)

  // (e) 复用 StandardLayout 的收尾工具
  UpdateStatusIconLayout(&width, &height)
  CopyRect(_bgRect, _contentRect); _bgRect.DeflateRect(offsetX+1, offsetY+1)
  _PrepareRoundInfo(dc)          // 圆角信息交给通用算法
  _contentRect.DeflateRect(offsetX, offsetY)
```

**需要重写的接口**：只有 `DoLayout` 一个。所有 `GetCandidateRect/GetPreeditRect/GetRoundInfo/...` 都继承 `StandardLayout` 的缓存实现——只要你在 `DoLayout` 里把 `_candidateRects[i]`、`_preeditRect` 等成员填好，绘制层就能直接用。这正是 4.1 多态设计的回报。

**接线（`_CreateLayout`）**：在 [WeaselUI/WeaselPanel.cpp:110-132](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L110-L132) 的 `else` 分支里加一个 `else if`，并在 [include/WeaselIPCData.h:206-213](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L206-L213) 的 `LayoutType` 枚举加一项（如 `LAYOUT_TWO_COLUMN`），再在 [RimeWithWeasel.cpp:1264-1268](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L1264-L1268) 的 `layout_type` 字符串映射表里加 `"two_column" → LAYOUT_TWO_COLUMN`。三处改完，用户在 `weasel.custom.yaml` 写 `style/layout: two_column` 即可启用。

**需要观察的现象**：新增一种排布，绘制层（`DoPaint`）与 DirectWrite 资源层（`DirectWriteResources`）一行都不用改。

**预期结果**：候选以两列呈现，高亮跟随当前候选，圆角与状态图标自动正确（因为复用了 `_PrepareRoundInfo` 与 `UpdateStatusIconLayout`）。本实践为设计型，未实际编译运行；如本地实现，注意 `columnWidth` 需先做一遍预扫描取所有候选的最大宽度，否则右列起点无法确定。

---

## 6. 本讲小结

- `Layout` 是**纯几何层**：只回答「矩形在哪」，不碰 DirectWrite；`WeaselPanel::DoPaint` 是**纯绘制层**：只管「按矩形画字」。二者通过一整套 `GetXxxRect()` getter 解耦。
- 多态链 `Layout → StandardLayout → {Horizontal, Vertical, VHorizontal}` 中，`StandardLayout` 把所有 getter 落地为「返回缓存」，子类**只需重写 `DoLayout`** 一个虚函数。
- `Layout` 构造函数统一做两件事：把 `_style` 的像素量按 DPI 缩放、算出 `real_margin`（内边距）与 `offsetX/Y`（阴影+边框外圈）。
- `_CreateLayout()` 是把 `UIStyle.layout_type` 枚举翻译成具体子类的唯一工厂；`FullScreenLayout` 是装饰器，在工厂里把普通布局包一层。
- 横排支持按 `max_width` 多行换行；竖排候选逐行铺满、注释单列右对齐；竖排文字（`VHorizontalLayout`）靠 DirectWrite 阅读方向 `TOP_TO_BOTTOM` 实现，`vertical_text_with_wrap` 时可多列换行。
- `UpdateStatusIconLayout` 与 `_PrepareRoundInfo` 是两个共享收尾工具：前者按三条规则摆状态图标，后者用 GDI+ Region 判断「探出窗口边缘」来决定每个候选圆哪几个角，实现高亮与窗口圆角的无缝贴合。

## 7. 下一步学习建议

- **继续 u5 单元**：阅读 [u5-l3 DirectWrite 资源与文本绘制]，看 `DirectWriteResources` 如何创建字体格式/画刷、`WeaselPanel::_DrawPreedit/_DrawCandidates/_HighlightText` 如何消费本讲算出的矩形与 `GetRoundInfo`，并用 `GdiplusBlur` 画出阴影。本讲是「在哪里画」，u5-l3 是「怎么画」。
- **回看配置链路**：读 [u4-l3 方案配置、App 选项与 inline preedit]，对照 [RimeWithWeasel.cpp:1264-1268](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L1264-L1268) 里 `layout_type` 从 YAML 字符串到枚举的映射，理解用户改 `style/layout` 是如何一路传到本讲的 `_CreateLayout` 的。
- **动手扩展**：把第 5 节的 `TwoColumnLayout` 真正实现出来，是一次很好的综合训练；若想做更小的练习，试着在某个 `DoLayout` 里临时加一行把 `_candidateRects[0]` 平移 10 像素，重新部署后观察候选窗口的偏移，验证你对「布局→绘制」数据流的判断。
