# WeaselPanel 窗口、分层与交互

## 1. 本讲目标

本讲是 UI 渲染单元（u5）的第一篇，聚焦候选窗口的「外壳」——`WeaselPanel`。读完本讲，你应当能够：

- 说清楚 `WeaselPanel` 为什么是一组「分层 + 置顶 + 不抢焦点」的 popup 窗口，以及这些窗口样式（`WS_EX_LAYERED` / `WS_EX_TOPMOST` / `WS_EX_NOACTIVATE` 等）各自解决什么问题。
- 描述双缓冲绘制链路：从 `Refresh()` 到 `DoPaint()` 再到 `UpdateLayeredWindow`，以及为什么要手动 `RedrawWindow()`。
- 追踪鼠标 hover、点击选词、滚轮/点击翻页三种交互如何从窗口消息翻译成 `_UICallback`，再经 TSF 层发出 IPC 命令。
- 理解光标跟随（`MoveTo` / `_RepositionWindow`）、多显示器/DPI 变化下的窗口定位，以及「粘顶」（`m_sticky`）机制。

本讲只讲「窗口与交互」，至于候选文字如何被 DirectWrite 画出来、布局如何计算几何矩形，分别留给 u5-l3（DirectWrite 绘制）和 u5-l2（Layout 系统）。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **TSF 前端的候选 UI 元素**（u3-l4）：`CCandidateList` 既是一个被 TSF 管理的 `ITfUIElement`，又持有一个 `weasel::UI`，后者内部就是 `WeaselPanel`。本讲的鼠标交互最终会回调到 `CCandidateList` 注入的 `_UICallback`。
- **UI 输出漏斗**（u4-l4）：服务端 `RimeWithWeaselHandler::_UpdateUI` 把 `Context`/`Status` 推送给 `weasel::UI::Update`，再触发 `WeaselPanel::Refresh`。本讲是这条推送链的「最后一公里」。
- **基本的 Win32 窗口概念**：窗口样式（Window Style / Extended Window Style）、消息循环、`WM_PAINT`、`HWND`。
- **WTL（Windows Template Library）**：Weasel 的 GUI 基于 WTL/ATL。`CWindowImpl` 是窗口基类，`BEGIN_MSG_MAP` / `MESSAGE_HANDLER` 是消息映射宏，`CDoubleBufferImpl` 是 WTL 提供的双缓冲混入类（mixin）。如果你没接触过 WTL，只需把它理解为「用模板和宏把 Win32 的窗口过程封装成可读的 C++ 类」。

几个本讲反复出现的术语，先统一解释：

| 术语 | 含义 |
| --- | --- |
| 分层窗口（Layered Window） | 带 `WS_EX_LAYERED` 的窗口，可按每像素 alpha 半透明合成到屏幕，适合做圆角、阴影、半透明的候选框。 |
| popup 窗口 | `WS_POPUP` 样式的无边框弹出窗口，没有标题栏和边框。 |
| 不抢焦点（No-activate） | `WS_EX_NOACTIVATE`：窗口显示时不会偷走键盘焦点，保证用户当前打字的应用不失去焦点。 |
| 命中测试（Hit-test） | 给定一个鼠标坐标，判断它落在哪个候选词的矩形里（`CRect::PtInRect`）。 |
| 双缓冲（Double Buffering） | 先把所有内容画到一块内存位图（memDC），再一次性贴到屏幕，避免闪烁。 |

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [WeaselUI/WeaselPanel.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.h) | `WeaselPanel` 类声明：窗口样式 traits、消息映射表、所有鼠标/绘制/定位方法的声明，以及关键成员变量（`m_hoverIndex`、`m_sticky`、`m_istorepos`、`bar_scale_` 等）。 |
| [WeaselUI/WeaselPanel.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp) | `WeaselPanel` 的全部实现：构造、`Refresh`、`DoPaint`、鼠标事件处理、`MoveTo` / `_RepositionWindow`。本讲的主力文件。 |
| [WeaselUI/WeaselUI.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselUI.cpp) | `weasel::UIImpl` 把 `WeaselPanel` 包成一个 pImpl，提供 `Show` / `Hide` / `ShowWithTimeout` / `Refresh`，并定义 `AUTOHIDE_TIMER`。`UI::Create` 真正创建窗口。 |
| [include/WeaselUI.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUI.h) | `weasel::UI` 抽象接口与 `_UICallback` 签名（4 个指针参数：select / hover / next / scroll_next）。 |
| [WeaselTSF/CandidateList.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp) | `_UICallback` 的注入点与 `HandleUICallback`，把鼠标意图翻译成 IPC 命令（`SelectCandidateOnCurrentPage` / `HighlightCandidateOnCurrentPage` / `ChangePage`）。 |

辅助引用（仅在概念说明中点到）：[WeaselUI/Layout.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/Layout.h)（几何接口）、[WeaselUI/StandardLayout.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/StandardLayout.h)（`MAX_CANDIDATES_COUNT` / `STATUS_ICON_SIZE`）、[include/WeaselIPCData.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h)（`UIStyle::HoverType`）。

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**分层窗口与双缓冲**、**鼠标交互与选词**、**光标跟随与 DPI**。

### 4.1 分层窗口与双缓冲

#### 4.1.1 概念说明

候选窗口是一个「浮在所有应用之上、半透明、带圆角和阴影、但不抢键盘焦点」的弹出层。要同时满足这些诉求，单靠一个普通窗口做不到，Win32 提供了一组扩展样式来组合：

- `WS_POPUP`：无边框弹出窗口（不要标题栏）。
- `WS_EX_TOOLWINDOW`：不出现在任务栏（Alt+Tab 里也别露脸）。
- `WS_EX_TOPMOST`：置顶，`Z` 序永远在普通窗口之上。
- `WS_EX_NOACTIVATE`：显示时不激活、不抢焦点——这是输入法候选框的命根子，否则用户每打一个字焦点就跳到候选框里了。
- `WS_EX_LAYERED`：分层窗口，支持每像素 alpha 合成，是圆角、软阴影、半透明的物理基础。

分层窗口的更新方式和普通窗口不一样：普通窗口靠 `WM_PAINT` + `BitBlt`，分层窗口通常用 `UpdateLayeredWindow` 一次性把整张位图（带 alpha 通道）合成上去。这意味着 `WeaselPanel` 的绘制链路是「画到一张内存位图 → `UpdateLayeredWindow` 合成」，而不是传统的「`WM_PAINT` → 直接画到屏幕」。

#### 4.1.2 核心流程

分层窗口的绘制与刷新链路（注意 `WM_PAINT` 在这里**不是**主路径）：

```text
服务端 _UpdateUI (u4-l4)
        │  推送 Context/Status
        ▼
weasel::UI::Update(ctx, status)        // 去重 + 候选缩写
        │
        ▼
UIImpl::Refresh()                      // 若有 AUTOHIDE 定时器先关掉
        │
        ▼
WeaselPanel::Refresh()                 // 算 hide_candidates、_CreateLayout、DoLayout
        │                               //   _ResizeWindow + _RepositionWindow
        ▼  (仅当 ctx 变化时)
WeaselPanel::RedrawWindow()            // 手动调用 DoPaint
        │
        ▼
WeaselPanel::DoPaint(dc)
        ├─ ModifyStyleEx: WS_EX_TRANSPARENT → WS_EX_LAYERED
        ├─ 建内存 DC + 兼容位图 (memDC)
        ├─ 用 GDI+/DirectWrite 把背景/候选/preedit 全画进 memDC
        └─ _LayerUpdate(rcw, memDC)
                └─ UpdateLayeredWindow(..., ULW_ALPHA)   // 合成上屏
```

两个关键点先记住，下面源码精读里会展开：① 窗口创建时带的是 `WS_EX_TRANSPARENT`，到 `DoPaint` 才换成 `WS_EX_LAYERED`；② `RedrawWindow` 直接调 `DoPaint` 而不走 `WM_PAINT`。

#### 4.1.3 源码精读

**窗口样式 traits。** `WeaselPanel` 用 WTL 的 `CWinTraits` 声明了一套默认样式，并继承 `CWindowImpl`（窗口）和 `CDoubleBufferImpl`（双缓冲混入）：

[WeaselUI/WeaselPanel.h:L13-L26](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.h#L13-L26) —— 这段定义了 `CWeaselPanelTraits`（注意默认扩展样式里带 `WS_EX_LAYERED`）和类的双继承。

```cpp
typedef CWinTraits<WS_POPUP | WS_CLIPSIBLINGS | WS_DISABLED,
                   WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE |
                       WS_EX_LAYERED>
    CWeaselPanelTraits;

class WeaselPanel
    : public CWindowImpl<WeaselPanel, CWindow, CWeaselPanelTraits>,
      CDoubleBufferImpl<WeaselPanel> {
```

**真正创建窗口时覆盖了 traits。** `UI::Create` 调用 `panel.Create(...)` 时显式传了扩展样式，覆盖掉 traits 的默认值——实际生效的是带 `WS_EX_TRANSPARENT`（而非 `WS_EX_LAYERED`）的一组：

[WeaselUI/WeaselUI.cpp:L85-L103](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselUI.cpp#L85-L103) —— 创建窗口，扩展样式为 `WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT`。

```cpp
pimpl_->panel.Create(
    parent, 0, 0, WS_POPUP,
    WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT,
    0U, 0);
```

> 小知识：在 ATL 里，`CWindowImpl::Create` 只有当传入的 `dwStyle` / `dwExStyle` 为 0 时才会回退到 traits 的默认值；这里两个参数都非 0，所以 traits 被完全覆盖。traits 在这里更多是「文档作用」。

**绘制时再把 `WS_EX_TRANSPARENT` 换回 `WS_EX_LAYERED`。** `DoPaint` 第一行就做了样式翻转：

[WeaselUI/WeaselPanel.cpp:L988-L998](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L988-L998) —— 把 `WS_EX_TRANSPARENT` 关掉换成 `WS_EX_LAYERED`，理由注释写得很直白：「for better resp performance」（响应性能更好），随后创建内存 DC 与兼容位图。

```cpp
void WeaselPanel::DoPaint(CDCHandle dc) {
  // turn off WS_EX_TRANSPARENT, for better resp performance
  ModifyStyleEx(WS_EX_TRANSPARENT, WS_EX_LAYERED);
  GetClientRect(&rcw);
  // prepare memDC
  CDCHandle hdc = ::GetDC(m_hWnd);
  CDCHandle memDC = ::CreateCompatibleDC(hdc);
  HBITMAP memBitmap = ::CreateCompatibleBitmap(hdc, rcw.Width(), rcw.Height());
  ::SelectObject(memDC, memBitmap);
  ...
```

为什么要这样切换？`WS_EX_TRANSPARENT | WS_EX_LAYERED` 同时存在时，鼠标点击会「穿透」窗口（click-through），收不到鼠标消息。Weasel 的策略是：平时让窗口以 `WS_EX_TRANSPARENT` 存在（绘制相关行为更省），到了真正要重绘的 `DoPaint` 里才切回 `WS_EX_LAYERED`，这样既能用 `UpdateLayeredWindow` 做半透明合成，又能正常接收鼠标事件。

**手动重绘，绕开 `WM_PAINT`。** 注释解释了为什么自己写一个 `RedrawWindow()`：

[WeaselUI/WeaselPanel.cpp:L1120-L1126](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1120-L1126) —— 由于部分应用在消息循环里直接丢掉 `WM_PAINT`，`CDoubleBufferImpl` 的 `WM_PAINT → DoPaint` 路径不可靠，所以这里手动 `GetDC` 后直接调 `DoPaint`。

```cpp
// 由于某些软件并不依赖 WM_PAINT 消息来重绘，在消息循环中直接忽略掉了 WM_PAINT
// 消息， 导致 DoPaint() 永远不会被调用，这里手动调用 DoPaint() 强制重绘
void WeaselPanel::RedrawWindow() {
  HDC hdc = GetDC();
  DoPaint(hdc);
  ReleaseDC(hdc);
}
```

这就是为什么 `WeaselPanel` 虽然继承了 `CDoubleBufferImpl`，实际刷新走的是「`Refresh()` → `RedrawWindow()` → `DoPaint()`」这条手动的、不依赖 `WM_PAINT` 的路径。`CDoubleBufferImpl` 在这里更像是一份「保险」与共用 `DoPaint` 方法名。

**最后一步：`UpdateLayeredWindow` 合成上屏。** `DoPaint` 把所有东西都画进 `memDC` 后，交给 `_LayerUpdate`：

[WeaselUI/WeaselPanel.cpp:L1128-L1140](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1128-L1140) —— 用 `ULW_ALPHA` 把 memDC 按每像素 alpha 合成到屏幕，这是分层窗口上屏的标准做法。

```cpp
void WeaselPanel::_LayerUpdate(const CRect& rc, CDCHandle dc) {
  HDC ScreenDC = ::GetDC(NULL);
  ...
  BLENDFUNCTION bf = {AC_SRC_OVER, 0, 0XFF, AC_SRC_ALPHA};
  UpdateLayeredWindow(m_hWnd, ScreenDC, &WindowPosAtScreen, &sz, dc,
                      &PointOriginal, RGB(0, 0, 0), &bf, ULW_ALPHA);
  ReleaseDC(ScreenDC);
}
```

**`Refresh()`：决定要不要重画、重画前先布局。** 这是 UI 推送漏斗在 panel 内部的入口：

[WeaselUI/WeaselPanel.cpp:L135-L181](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L135-L181) —— 计算 `hide_candidates`、在内容变化时 `_CreateLayout` → `DoLayout` → `_ResizeWindow` → `_RepositionWindow`，最后只有 `m_ctx != m_octx`（新旧 context 不同）才真正 `RedrawWindow`。

```cpp
void WeaselPanel::Refresh() {
  ...
  m_candidateCount = min(m_ctx.cinfo.candies.size(), MAX_CANDIDATES_COUNT);
  ...
  if (!hide_candidates || inline_no_candidates) {
    _InitFontRes();
    _CreateLayout();
    CDCHandle dc = GetDC();
    m_layout->DoLayout(dc, pDWR);
    ReleaseDC(dc);
    _ResizeWindow();
    _RepositionWindow();
    if (m_ctx != m_octx) { m_octx = m_ctx; RedrawWindow(); }
  }
}
```

这里的 `m_ctx != m_octx` 是第二道去重——上层 `UI::Update` 已经做过一次内容相等比较（见 [WeaselUI/WeaselUI.cpp:L164-L168](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselUI.cpp#L164-L168)），panel 内部再保险一次，避免布局变了但内容没变时也频繁重绘。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认「窗口实际生效的扩展样式」与「traits 声明的样式」并不相同，并理解 `DoPaint` 的样式翻转。

**操作步骤（源码阅读型实践）**：

1. 打开 [WeaselUI/WeaselPanel.h:L13-L16](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.h#L13-L16)，抄下 traits 里声明的全部窗口样式与扩展样式。
2. 打开 [WeaselUI/WeaselUI.cpp:L87-L101](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselUI.cpp#L87-L101)，抄下 `panel.Create` 实际传入的 `dwStyle` 与 `dwExStyle`。
3. 做一张对照表，标出：traits 有、Create 没有的样式（`WS_EX_LAYERED`、`WS_CLIPSIBLINGS`、`WS_DISABLED`），以及 Create 有、traits 没有的样式（`WS_EX_TRANSPARENT`）。
4. 打开 [WeaselUI/WeaselPanel.cpp:L990](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L990)，确认 `ModifyStyleEx(WS_EX_TRANSPARENT, WS_EX_LAYERED)` 这一行的存在。

**需要观察的现象**：你会清楚地看到窗口创建时的「瞬态样式」（`WS_EX_TRANSPARENT`）与重绘后的「稳态样式」（`WS_EX_LAYERED`）是不同的；`WS_EX_LAYERED` 是在第一次 `DoPaint` 时才被装上的。

**预期结果**：能口头复述「为什么要切换」——`WS_EX_TRANSPARENT | WS_EX_LAYERED` 同时存在会导致鼠标穿透，所以平时留一个、画的时候换另一个，兼顾合成能力与鼠标响应。本结论无需运行，纯源码可证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `DoPaint` 里的 `ModifyStyleEx(WS_EX_TRANSPARENT, WS_EX_LAYERED)` 删掉，候选窗口还能正常显示和点击吗？为什么？

> **答案**：显示会出问题。没有 `WS_EX_LAYERED`，`UpdateLayeredWindow`（[WeaselPanel.cpp:L1137](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1137)）无法把带 alpha 的位图合成上屏，半透明、圆角、阴影都会失效；窗口可能完全不显示或显示成实心黑块。

**练习 2**：`WeaselPanel` 继承了 `CDoubleBufferImpl`，但为什么还要自己写 `RedrawWindow()` 直接调 `DoPaint`？

> **答案**：因为部分宿主应用在消息循环里直接丢弃 `WM_PAINT`，导致 `CDoubleBufferImpl` 拦截 `WM_PAINT` → 调 `DoPaint` 的经典路径不可靠（见 [WeaselPanel.cpp:L1120-L1121](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1120-L1121) 的注释）。手动调 `DoPaint` 绕开 `WM_PAINT`，保证无论宿主行为如何，候选窗口都能被强制重绘。

---

### 4.2 鼠标交互与选词

#### 4.2.1 概念说明

候选窗口不是死板的显示框，它要支持三类鼠标交互：

1. **hover 高亮**：鼠标悬停在某个候选上，给它一个视觉反馈（高亮或半高亮）。
2. **点击选词**：鼠标点击某个候选，把它选为结果并上屏。
3. **翻页**：点击 `<` `>` 翻页符，或滚动滚轮，切换候选页。

这套交互有一个核心抽象：`_UICallback`。它是一个 `std::function`，签名是 4 个指针参数：

```cpp
std::function<void(size_t* const /*sel*/, size_t* const /*hov*/,
                   bool* const /*next*/, bool* const /*scroll_next*/)>
```

（见 [include/WeaselUI.h:L71-L80](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUI.h#L71-L80)。）

四个参数「哪个非空就表示哪个意图」：`sel` 非空 = 选中上屏，`hov` 非空 = 移动高亮，`next` 非空 = 点击翻页，`scroll_next` 非空 = 滚轮翻页。`WeaselPanel` 只管「发生了什么鼠标事件」，通过这 4 个指针把意图报上去；至于「意图如何变成 IPC 命令」，由 TSF 层的 `HandleUICallback` 负责（承接 u3-l4）。

这种设计把「窗口交互」和「命令派发」彻底解耦——`WeaselPanel`（住在 WeaselUI 库里）完全不知道 IPC、librime 的存在。

#### 4.2.2 核心流程

一次「hover 后点击上屏」的完整事件流：

```text
鼠标移到候选 i 上
   └─ WM_MOUSEMOVE → OnMouseMove
        ├─ hover_type==NONE?  直接 return（无 hover 效果）
        ├─ TrackMouseEvent(TME_LEAVE)  // 订阅离开事件
        ├─ 屏幕坐标没变? return（去抖）
        └─ 命中候选 i 的矩形（GetCandidateRect + InflateRect + PtInRect）
             ├─ hover_type==HILITE:    _UICallback(NULL,&i,NULL,NULL)
             │       → HandleUICallback → HighlightCandidateOnCurrentPage  // IPC 往返，真高亮
             └─ hover_type==SEMI_HILITE: m_hoverIndex=i; InvalidateRect      // 纯本地视觉，不走 IPC

鼠标左键按下候选 i
   └─ WM_LBUTTONDOWN → OnLeftClickedDown
        ├─ 命中候选 i 矩形
        │    ├─ i != 当前高亮?  _UICallback(NULL,&i,NULL,NULL)  // 先把高亮移过去
        │    ├─ bar_scale_ = 0.8f                              // 点击缩放反馈
        │    └─ SetTimer(AUTOREV_TIMER, 1000ms)                // 1 秒后还原缩放
        └─ （命中 < > 则发翻页；click_to_capture 则截图到剪贴板）

鼠标左键抬起
   └─ WM_LBUTTONUP → OnLeftClickedUp
        └─ 命中的是「当前高亮」候选矩形?
             └─ _UICallback(&i,NULL,NULL,NULL)
                   → HandleUICallback → _SelectCandidateOnCurrentPage
                         ├─ m_client.SelectCandidateOnCurrentPage(i)   // IPC：选中
                         └─ SendInput(VK_SELECT)                       // 借道 DoEditSession 上屏
```

这里有一个非常关键的设计——**「两段式点击」**：在非高亮候选上按下只会把高亮移过去（`OnLeftClickedDown` 里发 `hov`），必须在「已经是高亮」的候选上抬起才会真正选中上屏（`OnLeftClickedUp` 里发 `sel`）。换句话说，鼠标选词需要「点一下移高亮、再点一下上屏」两步（除非目标本来就是高亮项）。这避免了误触，也与 hover 行为一致。

几个状态变量需要记住（实践任务要用到）：

| 成员 | 类型 | 作用 |
| --- | --- | --- |
| `m_hoverIndex` | `int` | SEMI_HILITE 模式下的本地悬停索引，`-1` 表示无悬停。绘制时用半透明色画它（见 [WeaselPanel.cpp:L843-L857](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L843-L857)）。 |
| `bar_scale_` | `float` | 高亮标记条（mark）的缩放，点击时置 `0.8f` 做按压反馈，`AUTOREV_TIMER` 到点还原 `1.0f`。 |
| `m_sticky` | `bool` | 窗口是否「粘在输入位置上方」（见 4.3）。 |
| `m_mouse_entry` | `bool` | 是否已订阅 `WM_MOUSELEAVE`（`TrackMouseEvent` 只需调一次）。 |
| `m_lastMousePos` | `CPoint` | 上次鼠标屏幕坐标，用于去抖（位置没变就不处理）。 |
| `ptimer` | `UINT_PTR` | `AUTOREV_TIMER` 的静态归属指针，回调里用它找回 `this`。 |

#### 4.2.3 源码精读

**消息映射表。** 入口在这里，一目了然地列出了所有被处理的窗口消息：

[WeaselUI/WeaselPanel.h:L28-L39](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.h#L28-L39) —— `BEGIN_MSG_MAP` 把 `WM_CREATE/DESTROY/DPICHANGED/MOUSEACTIVATE/LBUTTONUP/LBUTTONDOWN/MOUSEWHEEL/MOUSEMOVE/MOUSELEAVE` 分别映射到对应 handler，最后 `CHAIN_MSG_MAP(CDoubleBufferImpl<WeaselPanel>)` 把未处理的消息交给双缓冲基类。

```cpp
BEGIN_MSG_MAP(WeaselPanel)
MESSAGE_HANDLER(WM_CREATE, OnCreate)
MESSAGE_HANDLER(WM_DESTROY, OnDestroy)
MESSAGE_HANDLER(WM_DPICHANGED, OnDpiChanged)
MESSAGE_HANDLER(WM_MOUSEACTIVATE, OnMouseActivate)
MESSAGE_HANDLER(WM_LBUTTONUP, OnLeftClickedUp)
MESSAGE_HANDLER(WM_LBUTTONDOWN, OnLeftClickedDown)
MESSAGE_HANDLER(WM_MOUSEWHEEL, OnMouseWheel)
MESSAGE_HANDLER(WM_MOUSEMOVE, OnMouseMove)
MESSAGE_HANDLER(WM_MOUSELEAVE, OnMouseLeave)
CHAIN_MSG_MAP(CDoubleBufferImpl<WeaselPanel>)
END_MSG_MAP()
```

**不抢焦点。** `WM_MOUSEACTIVATE` 的处理是输入法窗口的标配——返回 `MA_NOACTIVATE` 告诉系统「鼠标点我也别激活我」：

[WeaselUI/WeaselPanel.cpp:L277-L283](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L277-L283)

```cpp
LRESULT WeaselPanel::OnMouseActivate(...) {
  bHandled = true;
  return MA_NOACTIVATE;
}
```

**hover：`OnMouseMove`。** 这是本模块最值得读的方法，它把 hover 的三种模式（`HoverType::NONE/SEMI_HILITE/HILITE`）讲得清清楚楚：

[WeaselUI/WeaselPanel.cpp:L463-L516](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L463-L516) —— 先按 `hover_type==NONE` 早退；首次进入时 `TrackMouseEvent` 订阅离开；屏幕坐标未变则去抖；命中候选矩形后，`HILITE` 发 IPC 移高亮、`SEMI_HILITE` 只改本地 `m_hoverIndex`。

```cpp
LRESULT WeaselPanel::OnMouseMove(...) {
  if (m_style.hover_type == UIStyle::NONE) return 0;
  if (m_mouse_entry == false) {
    TRACKMOUSEEVENT tme; ... tme.dwFlags = TME_LEAVE; tme.dwHoverTime = 20;
    TrackMouseEvent(&tme);
  }
  m_mouse_entry = true;
  ...
  if (ptScreen == m_lastMousePos || m_lastMousePos.x == -1) { ... return 0; }  // 去抖
  m_lastMousePos = ptScreen;
  for (size_t i = 0; i < m_candidateCount && i < MAX_CANDIDATES_COUNT; ++i) {
    CRect rect = m_layout->GetCandidateRect((int)i);
    ...
    rect.InflateRect(DPI_SCALE(m_style.hilite_padding_x), DPI_SCALE(m_style.hilite_padding_y));
    if (rect.PtInRect(point)) {
      if (i != m_ctx.cinfo.highlighted) {
        if (m_style.hover_type == UIStyle::HoverType::HILITE) {
          if (_UICallback) _UICallback(NULL, &i, NULL, NULL);          // 走 IPC
        } else if (m_hoverIndex != i) {
          m_hoverIndex = static_cast<int>(i); InvalidateRect(&rcw, true);  // 纯本地
        }
      } else if (m_style.hover_type == UIStyle::HoverType::SEMI_HILITE && m_hoverIndex != -1) {
        m_hoverIndex = -1; InvalidateRect(&rcw, true);  // 悬停到已高亮项上，取消半高亮
      }
    }
  }
  return 0;
}
```

注意 `HILITE` 与 `SEMI_HILITE` 的本质差别：前者把鼠标意图通过 `_UICallback` 一路送到服务端 `HighlightCandidateOnCurrentPage`（一次 IPC 往返，会重算候选），后者只动 `m_hoverIndex`——一个本地变量，绘制时用半透明色叠加（见 [WeaselPanel.cpp:L843-L857](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L843-L857)）。`HoverType` 枚举定义在 [include/WeaselIPCData.h:L205](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L205)：`enum HoverType { NONE, SEMI_HILITE, HILITE };`。

**鼠标离开。** `OnMouseLeave` 清掉半高亮并重置追踪状态：

[WeaselUI/WeaselPanel.cpp:L518-L526](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L518-L526)

```cpp
LRESULT WeaselPanel::OnMouseLeave(...) {
  m_hoverIndex = -1;
  InvalidateRect(&rcw, true);
  m_mouse_entry = false;
  return 0;
}
```

**点击：`OnLeftClickedDown`（移高亮 + 反馈）与 `OnLeftClickedUp`（选中上屏）。** 按下阶段处理「移高亮、翻页、截图」：

[WeaselUI/WeaselPanel.cpp:L423-L443](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L423-L443) —— 命中候选 `i`：若非当前高亮就发 `hov` 移高亮，置 `bar_scale_=0.8f`，并起 `AUTOREV_TIMER`（1000ms）做按压反馈。

```cpp
for (size_t i = 0; i < m_candidateCount && i < MAX_CANDIDATES_COUNT; ++i) {
  CRect rect = m_layout->GetCandidateRect((int)i);
  ...
  rect.InflateRect(DPI_SCALE(m_style.hilite_padding_x), DPI_SCALE(m_style.hilite_padding_y));
  if (rect.PtInRect(point)) {
    bar_scale_ = 0.8f;
    if (i != m_ctx.cinfo.highlighted) {
      if (_UICallback) _UICallback(NULL, &i, NULL, NULL);   // 移高亮
    } else {
      RedrawWindow();
    }
    ptimer = UINT_PTR(this);
    ::SetTimer(m_hWnd, AUTOREV_TIMER, 1000, &WeaselPanel::OnTimer);
    ...
  }
}
```

抬起阶段只有在「点中当前高亮项」时才真正选中：

[WeaselUI/WeaselPanel.cpp:L298-L334](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L298-L334) —— 取当前高亮候选矩形，命中则发 `sel` 上屏。

```cpp
LRESULT WeaselPanel::OnLeftClickedUp(...) {
  ...
  ::KillTimer(m_hWnd, AUTOREV_TIMER);
  bar_scale_ = 1.0; ptimer = 0;
  {
    CRect rect = m_layout->GetCandidateRect((int)m_ctx.cinfo.highlighted);
    ...
    rect.InflateRect(DPI_SCALE(m_style.hilite_padding_x), DPI_SCALE(m_style.hilite_padding_y));
    if (rect.PtInRect(point)) {
      size_t i = m_ctx.cinfo.highlighted;
      if (_UICallback) {
        m_mouse_entry = false;
        _UICallback(&i, NULL, NULL, NULL);     // 选中上屏
        if (!m_status.composing) DestroyWindow();
      }
    } else { RedrawWindow(); }
  }
  ...
}
```

**按压反馈的还原。** `AUTOREV_TIMER`（常量值 `20240315`，像是个日期彩蛋）到点后把缩放还原：

[WeaselUI/WeaselPanel.cpp:L449-L461](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L449-L461) —— 静态 `OnTimer` 通过 `ptimer` 找回 `this`，还原 `bar_scale_` 并重绘。

```cpp
UINT_PTR WeaselPanel::ptimer = 0;
VOID CALLBACK WeaselPanel::OnTimer(...) {
  ::KillTimer(hwnd, idEvent);
  WeaselPanel* self = (WeaselPanel*)ptimer;
  ptimer = 0;
  if (self) { self->bar_scale_ = 1.0; self->RedrawWindow(); }
}
```

`bar_scale_` 在绘制时缩放高亮标记条的尺寸（见 [WeaselPanel.cpp:L882-L885](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L882-L885)），从而实现「点一下候选，标记条缩小再回弹」的视觉反馈。

**滚轮翻页。** 最简单的交互：把滚轮方向翻译成 `scroll_next`：

[WeaselUI/WeaselPanel.cpp:L285-L296](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L285-L296)

```cpp
LRESULT WeaselPanel::OnMouseWheel(...) {
  int delta = GET_WHEEL_DELTA_WPARAM(wParam);
  if (_UICallback && delta != 0) {
    bool nextpage = delta < 0;
    _UICallback(NULL, NULL, NULL, &nextpage);
  }
  ...
}
```

**`_UICallback` 的注入点与派发。** 回调在 TSF 层被注入并在 `HandleUICallback` 里分派：

[WeaselTSF/CandidateList.cpp:L300-L304](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L300-L304) —— 注入回调，把 4 个指针转发给 `WeaselTSF::HandleUICallback`。

```cpp
if (!_ui->uiCallback())
  _ui->SetUICallBack([this](size_t* const sel, size_t* const hov,
                            bool* const next, bool* const scroll_next) {
    _tsf->HandleUICallback(sel, hov, next, scroll_next);
  });
```

[WeaselTSF/CandidateList.cpp:L431-L441](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L431-L441) —— 「哪个指针非空就走哪条路」。

```cpp
void WeaselTSF::HandleUICallback(size_t* const sel, size_t* const hov,
                                 bool* const next, bool* const scroll_next) {
  if (sel)        _SelectCandidateOnCurrentPage(*sel);
  else if (hov)   _HandleMouseHoverEvent(*hov);
  else if (next || scroll_next) _HandleMousePageEvent(next, scroll_next);
}
```

最终落到具体的 IPC 命令（承接 u2-l1 的命令表）：

- hover：[CandidateList.cpp:L421-L428](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L421-L428) `_HandleMouseHoverEvent` → `m_client.HighlightCandidateOnCurrentPage(index)`。
- 选中上屏：[CandidateList.cpp:L380-L391](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L380-L391) `_SelectCandidateOnCurrentPage` → `m_client.SelectCandidateOnCurrentPage(index)`，再 `SendInput` 一个 `VK_SELECT` 模拟按键，借道 `DoEditSession` 把结果写回应用文档（衔接 u3-l3）。
- 翻页：[CandidateList.cpp:L393-L419](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L393-L419) `_HandleMousePageEvent` → `m_client.ChangePage(...)` 或滚轮时的 `HighlightCandidateOnCurrentPage`。

#### 4.2.4 代码实践

**实践目标**：画出「hover 高亮 → 点击上屏」的完整事件流转图，标注一路上变化的成员变量（这正是本讲规格里的实践任务）。

**操作步骤（源码阅读型实践，纸笔即可）**：

1. 准备一张白纸，横向画两列：左列「窗口消息 + `WeaselPanel`」，右列「`HandleUICallback` + IPC」。
2. 假设场景：当前高亮是第 0 个候选，用户把鼠标移到第 2 个候选上，停留后点击。
3. 按时序画出下列节点，并在箭头上标注每个成员变量的取值变化：
   - `WM_MOUSEMOVE`（首次）→ `m_mouse_entry: false→true`，调用 `TrackMouseEvent`。
   - `WM_MOUSEMOVE`（移到第 2 个）→ 命中 `i=2`。分两种情况标注：
     - `hover_type==SEMI_HILITE`：`m_hoverIndex: -1→2`，`InvalidateRect`，**不发 IPC**。
     - `hover_type==HILITE`：`_UICallback(NULL,&2,NULL,NULL)` → `HighlightCandidateOnCurrentPage(2)`（IPC 往返，回来后 `highlighted: 0→2`）。
   - `WM_LBUTTONDOWN`（点第 2 个，此时 `highlighted==2`）→ 命中 `i=2`，因为 `i==highlighted` 走 `else` 分支只 `RedrawWindow`；`bar_scale_: 1.0→0.8`；`ptimer=this`；起 `AUTOREV_TIMER`。
   - `WM_LBUTTONUP` → 命中「当前高亮（第 2 个）」矩形 → `_UICallback(&2,NULL,NULL,NULL)` → `_SelectCandidateOnCurrentPage(2)` → `SelectCandidateOnCurrentPage` + `SendInput(VK_SELECT)` 上屏。`bar_scale_` 还原 `1.0`，`ptimer=0`。
   - 1 秒后 `AUTOREV_TIMER` 到点（若还没被 `KillTimer`）→ `bar_scale_: 0.8→1.0`，`RedrawWindow`。
4. 在图上用红笔圈出「两段式点击」的关键：`OnLeftClickedDown` 发 `hov`（移高亮），`OnLeftClickedUp` 发 `sel`（上屏）。

**需要观察的现象**：你会清楚地看到 `SEMI_HILITE` 模式下整条 hover 链路**完全不出 `WeaselPanel`**（只动 `m_hoverIndex`），而 `HILITE` 模式每次 hover 都会绕一圈 IPC。

**预期结果**：得到一张标注了 `m_hoverIndex`、`m_mouse_entry`、`m_lastMousePos`、`bar_scale_`、`ptimer`、`m_ctx.cinfo.highlighted` 取值变化的时序图。若想验证 `SEMI_HILITE` 不走 IPC，可在 [WeaselPanel.cpp:L493-L514](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L493-L514) 确认该分支没有 `_UICallback` 调用（仅 `m_hoverIndex=` 与 `InvalidateRect`）。待本地验证：实际运行时可在 `OnMouseMove` 临时加一行日志确认调用频次。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `WeaselPanel` 用「4 个指针参数」的回调来表达鼠标意图，而不是定义一个枚举 + 一个值？

> **答案**：4 个指针天然支持「任意子集非空」的组合语义，且 `nullptr` 表示「该意图不参与」，调用点写起来极简（如 `_UICallback(NULL, &i, NULL, NULL)` 只表达 hover）。更重要的是，它把「交互意图」与「派发实现」解耦：`WeaselPanel`（WeaselUI 库）完全不需要知道 `HandleUICallback` 内部会发哪条 IPC 命令，降低了 UI 库对 TSF/IPC 的耦合。

**练习 2**：用户用鼠标点击一个**非高亮**候选，会发生什么？能一次点上屏吗？

> **答案**：不能一次上屏。`OnLeftClickedDown` 命中非高亮候选 `i` 时，只会 `_UICallback(NULL,&i,NULL,NULL)` 把高亮移到 `i`（[WeaselPanel.cpp:L432-L434](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L432-L434)），并起按压动画。真正上屏发生在 `OnLeftClickedUp` 且命中「当前高亮」矩形时（[WeaselPanel.cpp:L320-L327](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L320-L327)）。所以需要「点一下移高亮、再点一下上屏」两步（这就是 u3-l4 提到的「双击模型」）。

---

### 4.3 光标跟随与 DPI

#### 4.3.1 概念说明

候选窗口必须「跟着输入光标走」——你在哪儿打字，候选框就贴在那儿下方。这件事听起来简单，实际有三层麻烦：

1. **工作区边界**：光标在屏幕底部时，候选框往下放会超出屏幕，得自动翻到光标上方（并记住「粘顶」状态，避免后续每打一个字就上下抖动）。
2. **多显示器**：不同显示器分辨率/DPI 不同，窗口跨屏时尺寸和位置都要重算。
3. **DPI 缩放**：高 DPI 屏上，所有以像素为单位的尺寸（字号、间距、圆角、阴影半径）都要乘以一个缩放系数，否则候选框会小得看不见。

Weasel 用三个机制分别应对：`MoveTo` / `_RepositionWindow` 算位置、`m_sticky` 防抖、`dpiScaleLayout` + `_InitFontRes` 做 DPI 缩放。

#### 4.3.2 核心流程

光标跟随与定位的流程：

```text
TSF 报告光标矩形 rc (ITfContextView::GetWndExtents / composition 坐标)
   └─ weasel::UI::UpdateInputPosition(rc)
        └─ WeaselPanel::MoveTo(rc)
             ├─ 判断是否该重置 m_sticky（会话结束 / 位置大幅移动 / 内容空）
             ├─ ascii_tip_follow_cursor? 把提示图标挪到鼠标处
             └─ _RepositionWindow(adj=true)
                   ├─ MonitorFromRect 找光标所在显示器
                   ├─ GetMonitorInfo 取工作区 rcWork
                   ├─ 算 x/y，按 shadow/竖排文字做偏移
                   ├─ 超出工作区底部?  m_sticky=true，翻到光标上方 (y = top - height - 6)
                   │                   + vertical_auto_reverse 时 m_istorepos=true（候选倒序）
                   ├─ 超出左右/顶部?  钳到工作区内
                   └─ SetWindowPos(HWND_TOPMOST, x, y, SWP_NOACTIVATE)
```

DPI 处理的流程：

```text
构造 / _InitFontRes / OnDpiChanged
   └─ MonitorFromRect(m_inputPos) → GetDpiForMonitor(MDT_EFFECTIVE_DPI) → dpiX
        ├─ dpiScaleLayout = dpi / 96.0f
        ├─ DPI_SCALE(t) = t * dpiScaleLayout     // 所有布局尺寸经它缩放
        ├─ 若 dpi 变了: pDWR.reset() 重建 DirectWrite 资源
        └─ 记录 m_hMonitor，跨屏时 m_redraw_by_monitor_change=true 触发重绘
```

#### 4.3.3 源码精读

**`MoveTo`：跟随光标，并决定何时重置粘顶。** 这是 TSF 把光标矩形喂进来的入口：

[WeaselUI/WeaselPanel.cpp:L1172-L1220](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1172-L1220) —— 重置 `m_sticky` 的三个条件（会话结束 / 位置移动超 50px / 内容空），以及一个抗抖细节：忽略 `rc.bottom` 的 1~2 像素微小变化（注释提到 Word 2021 的 inline_preedit 会闪烁）。

```cpp
void WeaselPanel::MoveTo(RECT const& rc) {
  if (!m_layout) return;
  m_redraw_by_monitor_change = false;
  // 重置 sticky 的条件：会话结束 / 位置大幅移动 / 内容空
  bool should_reset_sticky =
      (m_ctx.empty() || (abs(rc.left - m_inputPos.left) > 50) ||
       (abs(rc.bottom - m_inputPos.bottom) > 50));
  if (should_reset_sticky && m_sticky) {
    m_sticky = false;
    m_inputPos = rc; m_inputPos.OffsetRect(0, 6);
    _RepositionWindow(true); RedrawWindow(); return;
  }
  // ascii 提示跟随鼠标
  if (m_style.ascii_tip_follow_cursor && m_ctx.empty() && (!m_status.composing)
      && m_layout->ShouldDisplayStatusIcon()) { ... }
  else if (... || m_layout->ShouldDisplayStatusIcon()) {
    // in some apps like word 2021, with inline_preedit set,
    // bottom of rc would flicker 1 px or 2, make the candidate flickering
    m_inputPos = rc; m_inputPos.OffsetRect(0, 6);
    bool m_istorepos_buf = m_istorepos;
    _RepositionWindow(true);
    if (m_istorepos != m_istorepos_buf || !m_ctx.aux.empty()
        || m_layout->ShouldDisplayStatusIcon() || m_redraw_by_monitor_change)
      RedrawWindow();
  }
}
```

`m_inputPos.OffsetRect(0, 6)` 给候选框与光标之间留 6 像素的呼吸距离。

**`_RepositionWindow`：算最终坐标，处理边界与粘顶。** 这是最见功夫的一段：

[WeaselUI/WeaselPanel.cpp:L1222-L1292](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1222-L1292) —— 用 `MonitorFromRect` + `GetMonitorInfo` 取工作区；窗口超出底部时翻到光标上方并置 `m_sticky=true`；竖排布局且 `vertical_auto_reverse` 时置 `m_istorepos=true`（让候选在窗口里倒序排列，下面会讲）；最后 `SetWindowPos(HWND_TOPMOST, ...)`。

```cpp
void WeaselPanel::_RepositionWindow(const bool& adj) {
  RECT rcWorkArea; ...
  HMONITOR hMonitor = MonitorFromRect(m_inputPos, MONITOR_DEFAULTTONEAREST);
  if (hMonitor) {
    MONITORINFO info; info.cbSize = sizeof(MONITORINFO);
    if (GetMonitorInfo(hMonitor, &info)) rcWorkArea = info.rcWork;
    if (hMonitor != m_hMonitor) { m_hMonitor = hMonitor; m_redraw_by_monitor_change = true; }
  }
  ...
  int x = m_inputPos.left;
  int y = m_inputPos.bottom;
  ...
  if (adj) m_istorepos = false;
  if (x > rcWorkArea.right) x = rcWorkArea.right;
  if (x < rcWorkArea.left)  x = rcWorkArea.left;
  // 在底部附近？翻到光标上方
  if (y > rcWorkArea.bottom || m_sticky) {
    if (!m_sticky) m_sticky = true;
    y = m_inputPos.top - height - 6;
    ...
    m_istorepos = (m_style.vertical_auto_reverse &&
                   m_style.layout_type == UIStyle::LAYOUT_VERTICAL);
    ...
  }
  if (y < rcWorkArea.top) y = rcWorkArea.top;
  m_inputPos.bottom = y;
  SetWindowPos(HWND_TOPMOST, x, y, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOREDRAW);
}
```

**`m_sticky` 的意义**：一旦窗口因为「放不下」被翻到光标上方，`m_sticky` 就置真，后续哪怕候选高度变化导致又能放下了，也**继续保持在上方**，避免窗口在「打字过程中」上下反复横跳（bouncing）。只有 `MoveTo` 里检测到会话结束、位置大幅移动、内容清空时才重置它（上面已看到）。`Refresh` 里还有一个重置点：候选从「有」变「无」时清 `m_sticky`（[WeaselPanel.cpp:L141-L143](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L141-L143)）。

**`m_istorepos` 与「竖排倒序」。** 当窗口被迫翻到光标上方、且样式开了 `vertical_auto_reverse` 时，竖排候选会「倒着排」——第 1 个候选画在最下面、贴近光标。这时 `m_istorepos=true`，绘制与命中测试里到处可见对它的处理：用 `m_offsetys[]` 给每个候选一个纵向偏移（见 [WeaselPanel.cpp:L1002-L1036](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1002-L1036)），并把圆角信息上下对调（`ReconfigRoundInfo`，[WeaselPanel.cpp:L40-L51](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L40-L51)）。鼠标命中测试里也处处 `if (m_istorepos) rect.OffsetRect(0, m_offsetys[i]);`。

**DPI 缩放：`DPI_SCALE` 宏与 `dpiScaleLayout`。** 所有以像素为单位的样式尺寸，使用前都过一遍 `DPI_SCALE`：

[WeaselUI/WeaselPanel.h:L77-L80](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.h#L77-L80) —— 缩放系数就是 `dpiScaleLayout`。

```cpp
template <typename T>
int DPI_SCALE(T t) {
  return (int)(t * dpiScaleLayout);
}
```

[WeaselUI/WeaselPanel.cpp:L183-L200](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L183-L200) —— `_InitFontRes` 用 `GetDpiForMonitor` 取当前显示器 DPI，算出 `dpiScaleLayout = dpi / 96.0f`，并在 DPI 变化或样式变化时重建 DirectWrite 资源（`pDWR.reset()`）。

```cpp
void WeaselPanel::_InitFontRes(bool forced) {
  HMONITOR hMonitor = MonitorFromRect(m_inputPos, MONITOR_DEFAULTTONEAREST);
  UINT dpiX = 96, dpiY = 96;
  if (hMonitor) GetDpiForMonitor(hMonitor, MDT_EFFECTIVE_DPI, &dpiX, &dpiY);
  // 样式变了 / dpi 变了 / pDWR 为空 → 重建 DWrite 资源
  if (forced || (pDWR == NULL) || (m_ostyle != m_style) || (dpiX != dpi)) {
    pDWR.reset();
    pDWR = std::make_shared<DirectWriteResources>(m_style, dpiX);
    pDWR->pRenderTarget->SetTextAntialiasMode((D2D1_TEXT_ANTIALIAS_MODE)m_style.antialias_mode);
  }
  m_ostyle = m_style;
  dpi = dpiX;
  dpiScaleLayout = (float)dpi / 96.0f;
}
```

所以你在源码里会看到铺天盖地的 `DPI_SCALE(m_style.hilite_padding_x)`、`DPI_SCALE(m_style.round_corner)`、`DPI_SCALE(m_style.shadow_radius)` 等——它们把「逻辑像素」换算成「物理像素」。`STATUS_ICON_SIZE` 与 `MAX_CANDIDATES_COUNT` 等常量定义在 [WeaselUI/StandardLayout.h:L10-L11](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/StandardLayout.h#L10-L11)。

**`WM_DPICHANGED`：跨屏拖动 / DPI 变化时重算。** 系统在窗口的 DPI 改变时（如拖到另一个 DPI 不同的显示器）发这条消息：

[WeaselUI/WeaselPanel.cpp:L1164-L1170](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1164-L1170) —— 直接 `Refresh()` 重走一遍布局/定位/重绘（内部会再调 `_InitFontRes` 读新 DPI、`_RepositionWindow` 重定位）。

```cpp
LRESULT WeaselPanel::OnDpiChanged(...) {
  Refresh();
  return LRESULT();
}
```

注意 `_RepositionWindow` 里还有一句 `if (hMonitor != m_hMonitor) { m_hMonitor = hMonitor; m_redraw_by_monitor_change = true; }`，它让「光标跨屏移动到另一个显示器」时也能触发一次重绘（因为不同显示器 DPI 可能不同，老的绘制已失效）。

#### 4.3.4 代码实践

**实践目标**：理解「粘顶」状态机，能用纸笔推演 `m_sticky` 在一组连续按键中的取值变化。

**操作步骤（源码阅读型实践）**：

1. 假设光标位于屏幕底部（`rc.bottom` 接近 `rcWorkArea.bottom`），用户连续输入 5 个字符，每个字符都让光标 `rc.bottom` 微小变化但始终在底部附近。
2. 画出每一次 `MoveTo(rc)` 调用时 `m_sticky` 与窗口 `y` 坐标的推演：
   - 第 1 次：`y > rcWorkArea.bottom` 成立 → `m_sticky: false→true`，`y = m_inputPos.top - height - 6`（翻到上方）。
   - 第 2~5 次：`m_sticky==true`，即便 `y` 算下来不再超底部，仍走 `if (y > rcWorkArea.bottom || m_sticky)` 分支保持在上方——**不抖动**。
3. 接着假设用户按回车上屏，会话结束（`m_ctx.empty()==true`）。画出 `MoveTo` 中 `should_reset_sticky` 命中 `m_ctx.empty()` → `m_sticky: true→false`。
4. 再到 [WeaselPanel.cpp:L141-L143](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L141-L143) 确认 `Refresh` 里「候选从有到无」也会清 `m_sticky`，作为第二条重置路径。

**需要观察的现象**：你会看到 `m_sticky` 是个「一旦置真就持续保持、直到显式重置」的滞回（hysteresis）标志，正是它消除了打字时的上下抖动。

**预期结果**：一张 `m_sticky` 状态转移图，标注三个重置触发点（`MoveTo` 里的大幅移动/会话结束/内容空，以及 `Refresh` 里的候选归零）。待本地验证：可在 `_RepositionWindow` 临时给 `m_sticky` 的赋值加日志，观察底部输入时的实际翻转时机。

#### 4.3.5 小练习与答案

**练习 1**：为什么需要 `m_sticky`？直接「每次都按 `y > rcWorkArea.bottom` 判断翻不翻」不行吗？

> **答案**：不行。打字过程中候选高度会变化（候选数变多/变少、preedit 变长），如果不滞回，窗口可能在这一帧刚好放得下（在下方）、下一帧又放不下（翻上方），反复横跳，体验极差。`m_sticky` 一旦置真就「锁定在上方」，直到会话结束或位置大幅变化才解除，保证稳定（见 [WeaselPanel.cpp:L1271-L1274](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1271-L1274)）。

**练习 2**：`dpiScaleLayout` 是怎么算出来的？为什么是 `dpi / 96.0f`？

> **答案**：Windows 以 96 DPI 为「逻辑 DPI」（100% 缩放），所以缩放系数 = 物理 DPI / 96。例如 144 DPI（150%）下 `dpiScaleLayout = 1.5`，`DPI_SCALE(10) = 15`。这样样式里写的尺寸都是「96 DPI 基准下的逻辑像素」，跨 DPI 自动放大（见 [WeaselPanel.cpp:L199](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L199)）。

**练习 3**：候选窗口从一个 100% 缩放的显示器拖到 150% 缩放的显示器，代码如何感知并重绘？

> **答案**：两条路径互补。① 系统发 `WM_DPICHANGED` → `OnDpiChanged` → `Refresh()` → `_InitFontRes` 读新 DPI、`dpiScaleLayout` 重算、`pDWR` 重建、重绘（[WeaselPanel.cpp:L1164-L1170](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1164-L1170)）。② `_RepositionWindow` 里 `hMonitor != m_hMonitor` 时置 `m_redraw_by_monitor_change=true`，`MoveTo` 据此触发 `RedrawWindow`（[WeaselPanel.cpp:L1232-L1235](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1232-L1235) 与 [L1216-L1218](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1216-L1218)）。

---

## 5. 综合实践

把本讲三个模块串起来，做一次「端到端鼠标选词」的全链路追踪。这是一道**源码阅读型**综合题，目标是验证你已经把「窗口样式 → 绘制 → 鼠标交互 → 光标跟随 → IPC 上屏」整条链打通。

**任务背景**：用户在记事本里输入拼音，候选窗口出现在光标下方；用户把鼠标移到第 3 个候选（`SEMI_HILITE` 模式），按下并抬起鼠标左键完成上屏。整个过程光标位于屏幕底部，所以窗口实际「粘」在光标上方。

**要求产出一张大图，包含四个泳道**：

1. **样式/绘制泳道**：标注窗口从创建（`WS_EX_TRANSPARENT`）到首次 `DoPaint` 翻成 `WS_EX_LAYERED`、`UpdateLayeredWindow` 上屏的过程；标出双缓冲 memDC 的位置。
2. **鼠标泳道**：`WM_MOUSEMOVE` → `OnMouseMove`（`m_hoverIndex: -1→3`，`InvalidateRect`，不走 IPC）→ `WM_LBUTTONDOWN` → `OnLeftClickedDown`（命中 `i=3`，因非高亮发 `hov` 移高亮，`bar_scale_:1.0→0.8`，起 `AUTOREV_TIMER`）→ `WM_LBUTTONUP` → `OnLeftClickedUp`（命中当前高亮，发 `sel`，`KillTimer`，`bar_scale_→1.0`）。
3. **定位泳道**：首次 `MoveTo` 时 `y > rcWorkArea.bottom` → `m_sticky: false→true`，窗口翻到光标上方；后续按键 `m_sticky` 保持 `true` 不抖动。
4. **IPC/上屏泳道**：`_UICallback(&3,NULL,NULL,NULL)` → `HandleUICallback` → `_SelectCandidateOnCurrentPage(3)` → `m_client.SelectCandidateOnCurrentPage(3)`（IPC 命令，回 u2-l1/u4-l2）→ `SendInput(VK_SELECT)` → `DoEditSession` 把字写回记事本（回 u3-l3）。

**验证方法**：

- 用本讲给出的所有永久链接逐一核对每个箭头的代码出处。
- 特别确认 `SEMI_HILITE` 分支确实没有 `_UICallback` 调用（[WeaselPanel.cpp:L504-L507](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L504-L507)），而点击选中确实走到 `SelectCandidateOnCurrentPage`（[CandidateList.cpp:L380-L391](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L380-L391)）。
- 如果你有 Windows 构建环境（见 u1-l3），可在 `OnMouseMove`、`OnLeftClickedDown`、`OnLeftClickedUp` 各加一行 `DEBUG << ...` 日志，实际运行验证时序与成员变量取值；若无法运行，标注「待本地验证」即可。

## 6. 本讲小结

- `WeaselPanel` 是一个 `WS_POPUP` + `WS_EX_TOPMOST | WS_EX_NOACTIVATE` 的分层 popup 窗口：置顶、不出现在任务栏、不抢焦点，靠 `WS_EX_LAYERED` + `UpdateLayeredWindow` 实现半透明合成。
- 窗口创建时带 `WS_EX_TRANSPARENT`，`DoPaint` 第一行用 `ModifyStyleEx` 翻成 `WS_EX_LAYERED`，兼顾鼠标响应与分层合成能力。
- 绘制走「`Refresh()` → `RedrawWindow()` → `DoPaint()` → memDC → `_LayerUpdate`/`UpdateLayeredWindow`」的**手动**路径，绕开不可靠的 `WM_PAINT`（部分宿主应用会吞掉 `WM_PAINT`）。
- 鼠标意图统一抽象为 4 个指针参数的 `_UICallback`（sel/hov/next/scroll_next），`WeaselPanel` 只报意图、不关心 IPC，由 TSF 层 `HandleUICallback` 派发到 `SelectCandidateOnCurrentPage` / `HighlightCandidateOnCurrentPage` / `ChangePage`。
- hover 有三种模式：`NONE`（无效果）、`SEMI_HILITE`（纯本地 `m_hoverIndex` + 半透明绘制，不走 IPC）、`HILITE`（每次 hover 发 IPC 真正移高亮）；点击选词是「两段式」——按下移高亮（`bar_scale_` 缩放反馈 + `AUTOREV_TIMER`），抬起在当前高亮项上才上屏。
- 光标跟随由 `MoveTo` / `_RepositionWindow` 完成，超出工作区底部时翻到光标上方并置 `m_sticky=true` 防抖；DPI 由 `GetDpiForMonitor` + `dpiScaleLayout = dpi/96` + `DPI_SCALE` 宏统一缩放，跨屏由 `WM_DPICHANGED` 与 `m_hMonitor` 变化触发重绘。

## 7. 下一步学习建议

本讲只讲了「窗口外壳与交互」，候选词具体画在哪儿、画多大，都依赖 `m_layout->GetCandidateRect(i)` 这类几何接口。建议：

- **下一讲 u5-l2（布局系统）**：阅读 [WeaselUI/Layout.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/Layout.h) 与 `HorizontalLayout` / `VerticalLayout` / `VHorizontalLayout` / `FullScreenLayout`，搞清楚 `GetCandidateRect` / `GetPreeditRect` / `GetHighlightRect` 这些接口如何根据 `UIStyle.layout_type` 算出每个元素的矩形——本讲里的命中测试全都建立在它们之上。
- **u5-l3（DirectWrite 资源与文本绘制）**：深入 `_DrawPreedit` / `_DrawCandidates` / `_HighlightText` 与 `DirectWriteResources`，理解文字、圆角、阴影具体怎么落到 memDC 上。
- **回看 u3-l4**：把本讲的 `_UICallback` 与 u3-l4 的 `CCandidateList` 双重身份（TSF UI 元素 + 自绘窗口宿主）对照，理解「为什么鼠标事件最终能变成 IPC 命令」。
- 若想做二次开发（例如新增一种鼠标手势），切入点是 `BEGIN_MSG_MAP`（[WeaselPanel.h:L28-L39](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.h#L28-L39)）里加一个 `MESSAGE_HANDLER`，并在 `_UICallback` 的 4 指针协议里复用或扩展。
