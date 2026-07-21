# DirectWrite 资源与文本绘制

## 1. 本讲目标

本讲是「候选窗口 UI 渲染」单元的最后一讲。在 [u5-l1](u5-l1-weasel-panel-window-and-interaction.md) 我们讲了候选窗口的外壳 `WeaselPanel`（分层窗口、双缓冲、鼠标交互），在 [u5-l2](u5-l2-layout-system.md) 我们讲了只回答「矩形在哪」的纯几何层 `Layout`。本讲补上最后一块拼图：**真正把像素画到屏幕上的绘制层**。

读完本讲你应当能够：

- 说清 `DirectWriteResources` 这个「资源池」里都装了哪些 Direct2D / DirectWrite 对象，以及它们为什么要在样式或 DPI 变化时整体重建。
- 说出 `WeaselPanel::DoPaint` 里「先画背景、再画文字」的分层绘制顺序，以及每一层用的是 GDI+ 还是 DirectWrite。
- 看懂圆角矩形路径 `GraphicsRoundRectPath`、阴影模糊 `GdiplusBlur`（盒滤波近似高斯）和配色（ABGR/ARGB/RGBA 互转）三件事的来龙去脉。

## 2. 前置知识

本讲涉及三个 Windows 图形技术名词，先用大白话解释：

- **GDI / GDI+**：Windows 传统的二维绘图 API。GDI+ 在它之上封装了画刷（`SolidBrush`）、画笔（`Pen`）、路径（`GraphicsPath`）等面向对象接口。它擅长画**几何形状**（矩形、圆角、路径），但对高质量文字渲染力不从心。
- **Direct2D（D2D）**：基于硬件加速的二维绘图 API，和 GDI+ 同样能画形状，但性能更好、抗锯齿质量更高。
- **DirectWrite（DWrite）**：和 Direct2D 配套的**现代文本排版引擎**，原生支持 ClearType、彩色字形（emoji）、字体回退（fallback）、复杂文字布局。Weasel 用它来画候选词、拼音串、注释。

Weasel 的策略是「各取所长」：**形状用 GDI+，文字用 DirectWrite**。具体做法是先用 GDI+ 把背景、圆角、阴影画进一张内存位图（memDC），再把 DirectWrite 的渲染目标 `BindDC` 到同一张内存位图上画文字，最后用 `UpdateLayeredWindow` 一次性合成到分层窗口。这就是为什么 `DoPaint` 里能混用两套 API 而不打架。

另外两个术语：

- **COLORREF**：Windows 的颜色类型，本质是一个 32 位整数，低 3 字节按 `0x00BBGGRR` 排布（蓝绿红，注意不是 RGB），最高字节常用来放 alpha。
- **DPI**：每英寸点数，决定屏幕缩放。96 DPI 是「100% 缩放」的基准；150% 缩放约为 144 DPI。Weasel 要在 4K/高 DPI 屏上不糊，全靠把字号和布局尺寸按 `dpi/96` 放大。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `WeaselUI/DirectWriteResources.cpp` | DirectWrite 资源池的实现：创建 D2D/DWrite 工厂、DC 渲染目标、画刷、四种文本格式，以及字体权重/风格解析与字体回退表构建。 |
| `include/WeaselUI.h` | `DirectWriteResources` 类的声明（成员变量 + 一堆内联的薄封装方法如 `DrawTextLayoutAt`、`CreateTextLayout`）。 |
| `WeaselUI/WeaselPanel.cpp` | 绘制的总指挥：`DoPaint` 编排顺序，`_HighlightText` 画背景/阴影/边框，`_DrawPreedit` / `_DrawCandidates` 画文字，`_TextOut` 是落到 DirectWrite 的最终一步。 |
| `WeaselUI/GdiplusBlur.cpp` | 阴影模糊算法：用 4 次盒滤波（box blur）近似高斯模糊，可被 OpenMP 并行加速。 |
| `WeaselUI/Layout.h` | 定义圆角矩形路径 `GraphicsRoundRectPath`、四角圆角信息 `IsToRoundStruct`，以及 `Layout` 抽象基类（提供各种 getter 给绘制层用）。 |
| `RimeWithWeasel/RimeWithWeasel.cpp` | 配色来源端：`_RimeGetColor` 把 `weasel.yaml` 里的 `ARGB`/`RGBA` 颜色统一转成 Windows 用的 `ABGR(COLORREF)`。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **DirectWrite 资源池与字体回退** —— 画笔和字号从哪来。
2. **分层绘制流程** —— `DoPaint` 里谁先画谁后画。
3. **圆角、阴影与配色** —— 形状之美。

### 4.1 DirectWrite 资源池与字体回退

#### 4.1.1 概念说明

DirectWrite 是一个「重对象」API：创建工厂（`IDWriteFactory`）、文本格式（`IDWriteTextFormat1`）、画刷（`ID2D1SolidColorBrush`）都比较昂贵，绝不能每画一个字就 new 一个。Weasel 的做法是把所有这些长生命周期对象收进一个叫 `DirectWriteResources`（简称 **DWR**）的资源池里，由 `WeaselPanel` 用智能指针 `PDWR`（即 `std::shared_ptr<DirectWriteResources>`）持有。

DWR 里装着这几样东西：

- 两个**工厂**：`pD2d1Factory`（Direct2D）和 `pDWFactory`（DirectWrite）。工厂全局共享，建一次即可。
- 一个 **DC 渲染目标** `pRenderTarget`（`ID2D1DCRenderTarget`）：DirectWrite 画文字的「画布」，最终 `BindDC` 到 GDI 的内存 DC 上。
- 一把**画刷** `pBrush`：单色实心画刷，画字时反复改它的颜色即可，不必重建。
- 四个**文本格式**：候选正文 `pTextFormat`、拼音串 `pPreeditTextFormat`、序号 `pLabelTextFormat`、注释 `pCommentTextFormat`——分别对应 UIStyle 里的四组字体设置。

关键生命周期约定：**样式变了、DPI 变了、或渲染目标绘制失败，就整体销毁重建 DWR**。因为文本格式把字号、字重、行距、回退表全固化了，改一处不如重建。

#### 4.1.2 核心流程

DWR 的构造与刷新流程：

```
构造 DirectWriteResources(style, dpi)
   ├── 建 D2D 工厂（多线程）
   ├── 建 DWrite 工厂（共享）
   ├── 建 DC 渲染目标（BGRA / 预乘 alpha）
   ├── 设置文字抗锯齿模式 + 图元抗锯齿
   ├── 建白底画刷
   ├── 算 DPI 缩放系数：dpiScaleFontPoint = dpi/72, dpiScaleLayout = dpi/96
   └── InitResources(style, dpi)
          └── init_font() ×4：解析字重/风格 → CreateTextFormat → SetFontFallback
```

什么时候触发重建？看 `_InitFontRes`：

```
forced || pDWR==NULL || 样式变了(m_ostyle != m_style) || DPI 变了(dpiX != dpi)
   → pDWR.reset() 重新 make_shared 一个全新的 DWR
```

#### 4.1.3 源码精读

DWR 构造函数创建四大基础对象，[WeaselUI/DirectWriteResources.cpp:17-58](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/DirectWriteResources.cpp#L17-L58)。其中关键几步：

D2D 工厂用「多线程」类型，让 D2D 自己管线程安全；DWrite 工厂用「共享」类型以便跨进程缓存字体数据：[WeaselUI/DirectWriteResources.cpp:36-41](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/DirectWriteResources.cpp#L36-L41)

```cpp
HR(::D2D1CreateFactory(D2D1_FACTORY_TYPE_MULTI_THREADED,
                       pD2d1Factory.ReleaseAndGetAddressOf()));
HR(DWriteCreateFactory(DWRITE_FACTORY_TYPE_SHARED, __uuidof(IDWriteFactory),
      reinterpret_cast<IUnknown**>(pDWFactory.ReleaseAndGetAddressOf())));
```

DC 渲染目标选 `BGRA + 预乘 alpha` 像素格式——这正是分层窗口 `UpdateLayeredWindow` 期望的格式，保证后续合成时半透明像素正确：[WeaselUI/DirectWriteResources.cpp:43-49](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/DirectWriteResources.cpp#L43-L49)

```cpp
const D2D1_PIXEL_FORMAT format = D2D1::PixelFormat(
    DXGI_FORMAT_B8G8R8A8_UNORM, D2D1_ALPHA_MODE_PREMULTIPLIED);
// ...
HR(pRenderTarget->CreateDCRenderTarget(&properties, &pRenderTarget));
pRenderTarget->SetTextAntialiasMode(mode);        // 来自 UIStyle.antialias_mode
pRenderTarget->SetAntialiasMode(D2D1_ANTIALIAS_MODE_PER_PRIMITIVE);
```

抗锯齿模式 `mode` 直接来自 `UIStyle.antialias_mode`，而该枚举的取值与 Direct2D 的 `D2D1_TEXT_ANTIALIAS_MODE` 一一对应：[include/WeaselIPCData.h:196-202](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L196-L202)（`DEFAULT=0 / CLEARTYPE=1 / GRAYSCALE=2 / ALIASED=3`），所以能直接强转。

DPI 缩放系数的计算很关键，字号用「点（point）」为单位（1 点 = 1/72 英寸），布局用「像素」为单位（96 像素 = 1 英寸）：[WeaselUI/DirectWriteResources.cpp:53-55](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/DirectWriteResources.cpp#L53-L55)

```cpp
dpiScaleFontPoint = dpiScaleLayout = (float)dpi;
dpiScaleFontPoint /= 72.0f;   // 点 → 像素
dpiScaleLayout /= 96.0f;      // 逻辑像素 → 物理像素
```

**字体回退**是 DWR 最有价值的特性之一。输入法候选里常混排汉字、拉丁字母、emoji，没有单一字体能全覆盖。`_SetFontFallback` 允许用户在 `font_face` 里写一串「字体名:起始码点:结束码点」的组合，按 Unicode 区段指定回退字体，最后再补上系统默认回退表：[WeaselUI/DirectWriteResources.cpp:231-278](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/DirectWriteResources.cpp#L231-L278)。它支持三种写法：

- `Segoe UI Emoji:1f300:1faff` —— 指定字体名 + 起止码点；
- `Segoe UI Emoji:1f300` —— 只给起始码点，结束默认 `10ffff`；
- `Segoe UI Emoji` —— 整段全用该字体。

构建流程是先用 `IDWriteFontFallbackBuilder` 逐条 `AddMapping`，再 `AddMappings(系统回退表)` 兜底，最后 `CreateFontFallback` 挂到文本格式上。

为了让「每个字体区段都能被用户自定义」，主字体名被故意设成无效名 `_InvalidFontName_`，从而强制所有字形都走回退表：[WeaselUI/DirectWriteResources.cpp:89-114](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/DirectWriteResources.cpp#L89-L114)

```cpp
const wstring _mainFontFace = L"_InvalidFontName_";
// ...
pDWFactory->CreateTextFormat(_mainFontFace.c_str(), NULL, fontWeight,
    fontStyle, DWRITE_FONT_STRETCH_NORMAL,
    fontpoint * dpiScaleFontPoint, L"",
    reinterpret_cast<IDWriteTextFormat**>(_pTextFormat.ReleaseAndGetAddressOf()));
```

字重（thin/light/bold/...）和字形（italic/oblique）从字体名字符串里用正则解析出来，例如 `"Microsoft YaHei:bold:italic"`：[WeaselUI/DirectWriteResources.cpp:183-221](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/DirectWriteResources.cpp#L183-L221)。

`WeaselPanel::_InitFontRes` 是 DWR 的「重建闸门」，它把「样式变化 / DPI 变化 / 渲染失败」统一收敛成一次 `pDWR.reset()` + 重建：[WeaselUI/WeaselPanel.cpp:183-200](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L183-L200)

```cpp
if (forced || (pDWR == NULL) || (m_ostyle != m_style) || (dpiX != dpi)) {
  pDWR.reset();
  pDWR = std::make_shared<DirectWriteResources>(m_style, dpiX);
  pDWR->pRenderTarget->SetTextAntialiasMode(
      (D2D1_TEXT_ANTIALIAS_MODE)m_style.antialias_mode);
}
```

#### 4.1.4 代码实践

**实践目标**：观察「样式变化触发 DWR 重建」这一行为，理解资源池的惰性重建策略。

**操作步骤**：

1. 在 [WeaselUI/WeaselPanel.cpp:191](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L191) 的 `if (forced || ...)` 体内，`pDWR.reset();` 这一行上方加一条诊断输出，例如 `OutputDebugStringW(L"[Weasel] DWR rebuilt\n");`（需 `#include <windows.h>`，stdafx 一般已含）。
2. 用 `build.bat` 编译 WeaselUI，把产物按 [u1-l3](u1-l3-build-and-run-from-source.md) 的方式装进 `output/` 并启动 `WeaselServer.exe`。
3. 在任意应用里打字触发候选窗口，然后通过托盘菜单切换输入方案（会改变 `font_face` 等样式）。
4. 用 [DebugView](https://learn.microsoft.com/sysinternals/downloads/debugview)（Sysinternals）捕获内核调试输出。

**需要观察的现象**：每次切换方案（样式变化）或拖动窗口跨不同 DPI 的显示器时，DebugView 里应出现 `[Weasel] DWR rebuilt`；而单纯翻页、移动光标（样式不变、DPI 不变）时不出现。

**预期结果**：确认 DWR 只在「样式/DPI 变化」时重建，平时复用——这正是资源池的意义。**待本地验证**（本环境为 Linux，无法实际运行 Windows 输入法）。

#### 4.1.5 小练习与答案

**练习 1**：为什么主字体名要故意设成无效的 `_InvalidFontName_`，而不是直接用用户填的第一个字体名？

**参考答案**：如果用真实字体名作为主字体，DirectWrite 会优先用它渲染**所有**字符，只有该字体缺字时才回退；这样用户自定义的「按码点区段指定字体」就被主字体抢了优先级，回退表形同虚设。设成无效名，强制所有字符都走回退表，用户对各 Unicode 区段的字体指定才真正生效。

**练习 2**：`dpiScaleFontPoint` 和 `dpiScaleLayout` 两个系数分别除以 72 和 96，能否合并成一个？

**参考答案**：不能。字号以「点」为单位（1 英寸 = 72 点），要换算成像素需 `× dpi / 72`；而布局尺寸（margin、圆角半径等）本身已经是「逻辑像素」（96 DPI 下 1 像素 = 1/96 英寸），要换算成物理像素需 `× dpi / 96`。两者单位不同，缩放系数必须分开。

---

### 4.2 分层绘制流程：DoPaint 的顺序

#### 4.2.1 概念说明

`WeaselPanel::DoPaint` 是整个绘制子系统的总入口。它要解决的核心矛盾是：**GDI+ 画形状、DirectWrite 画文字，两者要画到同一张内存位图上，还不能互相覆盖**。

Weasel 的解法是把绘制分成两个清晰的阶段：

- **背景阶段（GDI+）**：先画最底层的大背景，再画各候选的背景圆角矩形、阴影、边框。这一阶段全部用 GDI+，画在 `memDC` 上。
- **文字阶段（DirectWrite）**：把 DirectWrite 渲染目标 `BindDC` 到同一个 `memDC`，在背景之上叠画所有文字（拼音串、候选词、注释、序号）。

为什么要「先背景后文字」？因为候选文字要落在背景圆角矩形**之上**，如果反过来，背景会把文字盖掉。而把同类型的绘制批处理（先画完所有形状，再 `BeginDraw`/`EndDraw` 画完所有文字），还能减少 DirectWrite 渲染目标的状态切换开销。

> 说明：本讲规格里给出的名义顺序「背景→preedit→候选背景→候选文字→高亮→阴影」是一个概括。真实代码里，**阴影是和它所属的背景一起画的**（每个候选的阴影在它的背景圆角紧前面画，见 4.3），高亮候选的背景也在背景阶段一起画完。所以更精确的说法是：「背景层（含阴影/边框/高亮背景，GDI+）→ 文字层（DirectWrite）→ 图标 → 合成」。

#### 4.2.2 核心流程

`DoPaint` 的完整编排：

```
DoPaint(dc)
  ├── 准备 memDC + memBitmap（双缓冲）            // L989-L997
  ├── 若 m_istorepos：算各候选/preedit 的纵向偏移  // L1002-L1036
  ├── 【背景阶段 · GDI+】
  │     ├── 大背景 _HighlightText(BACKGROUND)        // L1040-L1044  Layout.GetContentRect()
  │     ├── aux 写作串背景 _DrawPreeditBack(aux)     // L1048
  │     ├── preedit 背景 _DrawPreeditBack(preedit)   // L1053
  │     └── 候选背景 _DrawCandidates(back=true)      // L1056  阴影/普通/半高亮/高亮+mark
  ├── 【文字阶段 · DirectWrite】
  │     ├── pRenderTarget->BindDC(memDC)             // L1061-L1064  失败则重建 DWR
  │     ├── BeginDraw                                // L1065
  │     ├── aux 文字 _DrawPreedit(aux)               // L1068  _TextOut→DrawTextLayoutAt
  │     ├── preedit 文字 _DrawPreedit(preedit)       // L1071
  │     ├── 候选文字 _DrawCandidates(back=false)     // L1074  label/text/comment
  │     └── EndDraw（失败则重建 DWR 并 Refresh）     // L1075-L1078
  ├── 状态图标 DrawIconEx（GDI）                     // L1082-L1108
  ├── _LayerUpdate → UpdateLayeredWindow 合成        // L1113
  └── 清理 memDC / memBitmap                         // L1116-L1117
```

#### 4.2.3 源码精读

`DoPaint` 开头先关掉 `WS_EX_TRANSPARENT`、翻成 `WS_EX_LAYERED`，并准备双缓冲内存 DC（这承接 [u5-l1](u5-l1-weasel-panel-window-and-interaction.md) 讲过的「手动绘制链路」）：[WeaselUI/WeaselPanel.cpp:989-997](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L989-L997)

```cpp
ModifyStyleEx(WS_EX_TRANSPARENT, WS_EX_LAYERED);
GetClientRect(&rcw);
CDCHandle hdc = ::GetDC(m_hWnd);
CDCHandle memDC = ::CreateCompatibleDC(hdc);
HBITMAP memBitmap = ::CreateCompatibleBitmap(hdc, rcw.Width(), rcw.Height());
::SelectObject(memDC, memBitmap);
```

背景阶段第一步是画整块大背景，几何来自 `Layout::GetContentRect()`，颜色用 `m_style.back_color` / `shadow_color` / `border_color`：[WeaselUI/WeaselPanel.cpp:1040-1044](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1040-L1044)

```cpp
CRect backrc = m_layout->GetContentRect();
_HighlightText(memDC, backrc, m_style.back_color, m_style.shadow_color,
               DPI_SCALE(m_style.round_corner_ex), BackType::BACKGROUND,
               IsToRoundStruct(), m_style.border_color);
```

进入文字阶段前，把 DirectWrite 渲染目标 `BindDC` 到刚才画好背景的 `memDC`。如果 `BindDC` 失败（渲染目标失效），强制重建 DWR 再绑一次：[WeaselUI/WeaselPanel.cpp:1061-1065](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1061-L1065)

```cpp
if (FAILED(pDWR->pRenderTarget->BindDC(memDC, &rcw))) {
  _InitFontRes(true);
  pDWR->pRenderTarget->BindDC(memDC, &rcw);
}
pDWR->pRenderTarget->BeginDraw();
```

文字画完后 `EndDraw`；若返回失败（例如设备丢失），同样重建 DWR 并 `Refresh()` 重画一帧：[WeaselUI/WeaselPanel.cpp:1075-1078](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1075-L1078)。这就是 DirectWrite 绘制能自愈的原因。

最终的合成由 `_LayerUpdate` 完成，它调用 `UpdateLayeredWindow` 用预乘 alpha 把 `memDC` 一次性贴到分层窗口：[WeaselUI/WeaselPanel.cpp:1128-1140](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1128-L1140)

```cpp
BLENDFUNCTION bf = {AC_SRC_OVER, 0, 0XFF, AC_SRC_ALPHA};
UpdateLayeredWindow(m_hWnd, ScreenDC, &WindowPosAtScreen, &sz, dc,
                    &PointOriginal, RGB(0, 0, 0), &bf, ULW_ALPHA);
```

`_DrawCandidates` 内部用 `back` 形参区分两趟：`back=true` 时只画背景/阴影/边框/高亮标记（GDI+，走 `_HighlightText`），`back=false` 时只画文字（DirectWrite，走 `_TextOut`）：[WeaselUI/WeaselPanel.cpp:785-799](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L785-L799) 与 [WeaselUI/WeaselPanel.cpp:909-951](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L909-L951)。文字趟对每个候选依次取 `GetCandidateLabelRect` / `GetCandidateTextRect` / `GetCandidateCommentRect` 三个几何，分别用 `pLabelTextFormat` / `pTextFormat` / `pCommentTextFormat` 三种文本格式画 label、正文、注释。

#### 4.2.4 代码实践

**实践目标**：把 `DoPaint` 的绘制顺序落到一张「步骤 ↔ DWR 接口 ↔ Layout 几何接口」对照表上，作为后续二次开发的导航。

**操作步骤**（源码阅读型实践）：

1. 打开 [WeaselUI/WeaselPanel.cpp:988](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L988) 的 `DoPaint`。
2. 从上到下逐行标注每个绘制调用，按下表填写。

**参考对照表**（已根据源码整理）：

| 步骤 | 调用 | DWR / GDI+ 接口 | Layout 几何接口 |
| --- | --- | --- | --- |
| ① 双缓冲准备 | `CreateCompatibleDC` / `CreateCompatibleBitmap` | —— | `GetClientRect` |
| ② 大背景 | `_HighlightText(BACKGROUND)` | GDI+ `Graphics::FillPath` | `GetContentRect()` |
| ③ aux 背景 | `_DrawPreeditBack(aux)` | GDI+（经 `_HighlightText`） | `GetAuxiliaryRect()`、`GetTextRoundInfo()` |
| ④ preedit 背景 | `_DrawPreeditBack(preedit)` | GDI+ | `GetPreeditRect()`、`GetTextRoundInfo()` |
| ⑤ 候选阴影 | `_DrawCandidates(back=true)` 阴影段 | GDI+ `_HighlightText` + `DoGaussianBlur` | `GetCandidateRect(i)`、`GetRoundInfo(i)` |
| ⑥ 候选普通背景/边框 | `_DrawCandidates` 普通段 | GDI+ `_HighlightText` | `GetCandidateRect(i)`、`GetRoundInfo(i)` |
| ⑦ 半高亮（hover）背景 | `_HighlightText`（HALF_ALPHA） | GDI+ | `GetCandidateRect(m_hoverIndex)` |
| ⑧ 高亮候选背景+mark | `_HighlightText` + mark 画刷 | GDI+ | `GetHighlightRect()`、`GetRoundInfo(highlighted)` |
| ⑨ 文字渲染准备 | `BindDC` + `BeginDraw` | DWR `pRenderTarget` | —— |
| ⑩ aux 文字 | `_DrawPreedit(aux)` → `_TextOut` | DWR `DrawTextLayoutAt` | `GetPreeditRange()`、`GetBeforeSize/HilitedSize/AfterSize`、`GetPrepageRect/NextpageRect` |
| ⑪ preedit 文字 | `_DrawPreedit(preedit)` → `_TextOut` | DWR `DrawTextLayoutAt` | 同上 |
| ⑫ 候选文字 | `_DrawCandidates(back=false)` → `_TextOut` | DWR `DrawTextLayoutAt` | `GetCandidateLabelRect/TextRect/CommentRect` |
| ⑬ 文字结束 | `EndDraw` | DWR `pRenderTarget` | —— |
| ⑭ 状态图标 | `DrawIconEx` | GDI `DrawIconEx` | `GetStatusIconRect()` |
| ⑮ 合成 | `_LayerUpdate` → `UpdateLayeredWindow` | GDI `UpdateLayeredWindow` | —— |

**需要观察的现象**：背景类（②~⑧）全走 GDI+，文字类（⑩~⑬）全走 DirectWrite，两者通过 `BindDC` 共享同一张 `memDC`。

**预期结果**：得到一张完整的对照表，任何「我想改某一步绘制」的需求都能从表中直接定位到调用点与几何来源。

#### 4.2.5 小练习与答案

**练习 1**：如果调换顺序，先 `BeginDraw` 画文字、再画背景圆角，会出现什么现象？

**参考答案**：背景圆角矩形会把刚画好的候选文字盖住，文字消失（或只剩背景圆角之外的部分）。因此必须严格「先背景后文字」。

**练习 2**：为什么 `BindDC` 和 `EndDraw` 失败时都调 `_InitFontRes(true)`？

**参考答案**：这两个失败通常意味着 DirectWrite 渲染目标或其依赖的设备上下文已失效（如显式释放、设备丢失、DPI 剧变）。`_InitFontRes(true)` 用 `forced=true` 强制整体重建 DWR，恢复出全新的工厂/渲染目标/文本格式，从而让下一帧绘制能自愈。

---

### 4.3 圆角、阴影与配色

#### 4.3.1 概念说明

形状之美来自三个细节：

- **圆角矩形**：候选背景不是直角方块，而是带可配置圆角半径的圆角矩形；当多个候选竖排时，首尾两项的圆角还要特殊处理（整体外观像一个连通的胶囊）。
- **阴影**：候选窗口和高亮候选可以有柔和的投影。Weasel 没用系统阴影，而是自己用「盒滤波近似高斯」算法把一个圆角矩形模糊掉，模拟出柔和阴影。
- **配色**：用户在 `weasel.yaml` 里写的颜色可能是 `ARGB`、`RGBA` 或 `ABGR` 三种格式之一，而 Windows 的 `COLORREF` 和 GDI+/DirectWrite 的取色宏都期望 `0xAABBGGRR`（alpha 在最高字节，其余按 BGR）。所以需要一个统一的格式转换。

#### 4.3.2 核心流程

**圆角矩形路径** 由 `GraphicsRoundRectPath`（继承自 GDI+ 的 `GraphicsPath`）负责。它支持两种构造：四角统一圆角半径，或四角分别指定是否圆角（用 `IsToRoundStruct` 描述）。当候选竖排时，`ReconfigRoundInfo` 会把首尾两项的「上圆角」翻成「下圆角」，让整列拼出连贯的外轮廓。

**阴影** 的算法链是：

```
_HighlightText 需要画阴影时
   ├── 新建一张 PARGB 离屏 Bitmap（比目标大一圈 margin）
   ├── 在离屏位图上用阴影色画一个圆角矩形（偏移 shadow_offset）
   │     - 有偏移：FillPath 一块实心圆角
   │     - 无偏移：从细到粗画多层圆角线，模拟环状衰减
   ├── DoGaussianBlur(bitmap, radius, radius)  模糊
   └── DrawImage 把模糊结果贴回主画布
```

**高斯模糊的数学原理**：真高斯卷积核很贵（每个像素要扫整个核）。常用近似是「多次盒滤波（box blur）逼近高斯」——n 个等宽盒滤波级联，其效果逼近标准差为 σ 的高斯。利用盒滤波可分离 + 滑动窗口，单趟是 O(像素数) 而非 O(像素数 × 核宽)。设每个盒宽为 w，则盒滤波方差为 \((w^2-1)/12\)，n 个级联后方差为 \(n(w^2-1)/12\)，令其等于 \(\sigma^2\) 解得理想盒宽：

\[
w_{\text{ideal}} = \sqrt{\frac{12\sigma^2}{n} + 1}
\]

这正是 `boxesForGauss` 第一行的公式（n 取 4）。

**配色转换**：`RimeWithWeasel.cpp` 里的 `_RimeGetColor` 读取颜色后，按 `color_format` 把 `ARGB` 或 `RGBA` 统一转成 Windows 用的 ABGR(COLORREF)，再存进 `UIStyle` 的颜色字段，最终被绘制层消费。

#### 4.3.3 源码精读

**圆角路径** `GraphicsRoundRectPath` 在 [WeaselUI/Layout.h:16-49](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/Layout.h#L16-L49) 声明，四角分别圆角的构造原型：

```cpp
GraphicsRoundRectPath(const CRect rc, int corner,
                      bool roundTopLeft, bool roundTopRight,
                      bool roundBottomRight, bool roundBottomLeft);
```

四角圆角信息结构体 `IsToRoundStruct` 默认四角全圆，并带一个 `Hemispherical` 标志位（半圆风格）：[WeaselUI/Layout.h:51-63](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/Layout.h#L51-L63)。绘制高亮背景时会读 `Layout::GetRoundInfo(i)` 和 `GetTextRoundInfo()` 决定四角圆不圆。

**阴影绘制** 全在 `_HighlightText` 里。条件是「阴影半径非 0 且阴影色非完全透明且非全屏布局」：[WeaselUI/WeaselPanel.cpp:558-604](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L558-L604)。关键步骤：

```cpp
// 离屏位图（PARGB，预留 blurMargin）
pBitmapDropShadow = new Gdiplus::Bitmap(
    (INT)rc.Width() + blurMarginX*2, (INT)rc.Height() + blurMarginY*2,
    PixelFormat32bppPARGB);
Gdiplus::Graphics g_shadow(pBitmapDropShadow);
// ... 用阴影色画圆角矩形（有偏移画实心块，无偏移画多层环）...
DoGaussianBlur(pBitmapDropShadow,
    (float)DPI_SCALE(m_style.shadow_radius),
    (float)DPI_SCALE(m_style.shadow_radius));
g_back.DrawImage(pBitmapDropShadow, rc.left - blurMarginX, rc.top - blurMarginY);
```

紧接着画背景填充和边框：背景色非透明才 `FillPath`，边框色非透明且 `border>0` 才 `DrawPath`：[WeaselUI/WeaselPanel.cpp:606-624](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L606-L624)。整个 `_HighlightText` 是「阴影 → 背景 → 边框」三步，这就是为什么 4.2 说阴影总是和它所属的背景一起画。

**高斯模糊** 的入口 `DoGaussianBlur` 在 [WeaselUI/GdiplusBlur.cpp:307-347](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/GdiplusBlur.cpp#L307-L347)，它 `LockBits` 拿到像素裸数据后调 `gaussBlur_4`。`gaussBlur_4` 先用 `boxesForGauss` 算出 4 个盒宽，再级联 4 次 `boxBlur_4`：[WeaselUI/GdiplusBlur.cpp:287-305](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/GdiplusBlur.cpp#L287-L305)

```cpp
void gaussBlur_4(...) {
  int bxsX[4]; boxesForGauss(rx, bxsX, 4);
  int bxsY[4]; boxesForGauss(ry, bxsY, 4);
  boxBlur_4(scl, tcl, w, h, (bxsX[0]-1)/2, (bxsY[0]-1)/2, bpp, stride);
  boxBlur_4(tcl, scl, w, h, (bxsX[1]-1)/2, (bxsY[1]-1)/2, bpp, stride);
  // ... 共 4 次 ...
}
```

`boxesForGauss` 实现了上面那个理想盒宽公式，并对奇偶性做了修正（盒宽必须为奇数以保证对称）：[WeaselUI/GdiplusBlur.cpp:15-30](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/GdiplusBlur.cpp#L15-L30)。盒滤波本身可分离为水平 `boxBlurH_4` 和垂直 `boxBlurT_4` 两趟，且用滑动窗口（增删两端像素）做到 O(像素数)；函数名后缀 `_4` 表示一次处理 4 个字节（BGRA 四通道），还用 `#pragma omp parallel for` 在图大于 64 像素时并行：[WeaselUI/GdiplusBlur.cpp:32-151](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/GdiplusBlur.cpp#L32-L151)。

**配色转换** 的两个宏在 [RimeWithWeasel/RimeWithWeasel.cpp:16-22](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L16-L22)：

```cpp
#define ARGB2ABGR(value) ((value & 0xff000000) | ((value & 0x000000ff) << 16) | \
                          (value & 0x0000ff00) | ((value & 0x00ff0000) >> 16))
#define RGBA2ABGR(value) (((value & 0xff) << 24) | ((value & 0xff000000) >> 24) | \
                          ((value & 0x00ff0000) >> 8) | ((value & 0x0000ff00) << 8))
```

`ARGB2ABGR` 只交换 R、B 两字节（alpha、G 不动），因为 ARGB（`0xAARRGGBB`）和 ABGR（`0xAABBGGRR`）的区别正是 R/B 互换。`RGBA2ABGR` 则是整体重排（alpha 从尾移到头，RGB 顺序反转）。转换发生在 `_RimeGetColor` 读取颜色之后：[RimeWithWeasel/RimeWithWeasel.cpp:1034-1038](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L1034-L1038)

```cpp
if (fmt == COLOR_ARGB)        value = ARGB2ABGR(value);
else if (fmt == COLOR_RGBA)   value = RGBA2ABGR(value);
value &= 0xffffffff;
```

绘制层消费这些颜色时，GDI+ 路径用 `GDPCOLOR_FROM_COLORREF`（alpha 取最高字节、RGB 用 `GetRValue/GetGValue/GetBValue`），DirectWrite 路径在 `_TextOut` 里也用同样的 `GetRValue/GetGValue/GetBValue` 取色：[WeaselUI/WeaselPanel.cpp:20-22](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L20-L22) 与 [WeaselUI/WeaselPanel.cpp:1301-1304](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1301-L1304)。两套路径取色方式一致，前提是颜色已统一成 ABGR。

> 补充：半透明叠加的更复杂场景（如配色层之间互相混合）由 `blend_colors` 处理，它做带 alpha 的「over 运算」合成并输出预乘 alpha 的 ABGR：[RimeWithWeasel/RimeWithWeasel.cpp:939-968](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L939-L968)。其合成公式为：

\[
\alpha_{\text{out}} = \alpha_f + (1-\alpha_f)\alpha_b, \quad
C_{\text{out}} = \frac{C_f\alpha_f + C_b\alpha_b(1-\alpha_f)}{\alpha_{\text{out}}}
\]

#### 4.3.4 代码实践

**实践目标**：亲手算一次颜色转换，验证「YAML 里写的颜色」如何变成「屏幕上的 COLORREF」。

**操作步骤**：

1. 假设某配色方案 `color_format: argb`，某字段写 `back_color: 0xCC112233`（即 alpha=0xCC, R=0x11, G=0x22, B=0x33 的 ARGB）。
2. 按 `ARGB2ABGR` 宏手算：保留 alpha `0xCC000000`，把低 3 字节的 R/B 互换 → `0xCC332211`。
3. 在 `_RimeGetColor` 设断点或加日志，确认运行时该字段值确为 `0xCC332211`（即 CC 33 22 11）。
4. 追到 `DoPaint` → `_HighlightText` → `GDPCOLOR_FROM_COLORREF`，验证 alpha=`0xCC`、`GetRValue=0x11`、`GetGValue=0x22`、`GetBValue=0x33`，对应屏幕上 (R=17, G=34, B=51, 透明度约 80%)。

**需要观察的现象**：手算结果与代码运行结果一致，颜色通道语义正确（用户写的 ARGB 红 0x11 在屏幕上确实是 R 通道 0x11）。

**预期结果**：确认整个配色链路 `YAML(ARGB) → _RimeGetColor → ARGB2ABGR → UIStyle → GDPCOLOR_FROM_COLORREF / GetXValue → 画刷/画笔` 没有任何字节序错位。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`ARGB2ABGR` 为什么只交换 R、B 两字节，而 alpha 和 G 原地不动？

**参考答案**：ARGB 的排布是 `A R G B`（从高到低字节），ABGR 是 `A B G R`。两者 alpha 都在最高字节、G 都在中间字节，只有 R 和 B 的位置互换了，所以只需交换最低字节（R↔B）。这是位运算最快的方式。

**练习 2**：阴影算法为什么要用「盒滤波近似高斯」而不是直接做高斯卷积？

**参考答案**：直接高斯卷积的核宽随 σ 线性增长，复杂度是 O(像素数 × 核宽)，大半径下极慢；而盒滤波可分离成水平/垂直两趟且能用滑动窗口实现 O(像素数)，4 次级联盒滤波在视觉上几乎等同于高斯，性能却高得多。这是图像处理里经典的「box blur ≈ gaussian」技巧。

**练习 3**：`_HighlightText` 里画阴影时为什么要先画到一张「比目标大一圈」的离屏位图，再贴回去？

**参考答案**：模糊会把像素向外扩散超过原圆角矩形的边界。如果直接在主画布上模糊，扩散出去的阴影会被画布边界截断，或污染相邻区域。先画到一张预留了 `blurMargin`（= `offsetX/offsetY`，即阴影边框外圈）的离屏位图上，模糊后再整体贴回，才能得到完整、柔和、不越界的阴影。

---

## 5. 综合实践

**任务**：用一张「分层剖面图」把本讲三个模块串起来，证明你掌握了 Weasel 候选窗口的完整绘制管线。

请按以下步骤完成：

1. **画一张 `DoPaint` 纵剖面图**：从下到上分 5 层——①大背景、②候选背景（含阴影/边框/高亮）、③候选文字、④状态图标、⑤合成输出。在每层右侧标注它用的技术（GDI+ / DirectWrite / GDI）和触发它的 `DoPaint` 行号区间（参考 4.2.4 对照表）。
2. **标注资源来源**：在文字层旁注明「文本格式来自 DWR 的 `pTextFormat/pLabelTextFormat/pCommentTextFormat`，画刷来自 `pBrush`，何时重建由 `_InitFontRes` 决定」。
3. **标注形状细节**：在候选背景层注明「圆角来自 `GraphicsRoundRectPath` + `IsToRoundStruct`，阴影来自 `DoGaussianBlur`（4 次盒滤波逼近高斯），配色来自 `_RimeGetColor` 的 ARGB/RGBA→ABGR 转换」。
4. **验证一处调用链**：选「点击候选词后高亮背景重画」这一场景，写出它从 `OnLeftClickedDown` → `_UICallback`（[u5-l1](u5-l1-weasel-panel-window-and-interaction.md)）→ IPC 改 `highlighted` → 下一次 `Refresh`/`DoPaint` → `_DrawCandidates(back=true)` → `_HighlightText` 的完整路径。

**预期成果**：一张图 + 一段调用链追踪，能向别人讲清「用户点了一下候选词，屏幕上那一格高亮是怎么重画出来的」。这一步把 u5-l1（交互）、u5-l2（布局）、u5-l3（绘制）三章连成闭环。

## 6. 本讲小结

- **DirectWrite 资源池（DWR）** 把 D2D/DWrite 工厂、DC 渲染目标、画刷、四种文本格式收为一处，在「样式变化 / DPI 变化 / 渲染失败」时由 `_InitFontRes` 整体重建；字体回退表用 `_InvalidFontName_` 主名强制按 Unicode 区段分配字体。
- **分层绘制** 严格遵循「先 GDI+ 画背景（含阴影/边框/高亮背景），再 `BindDC` 用 DirectWrite 画文字」的顺序，两套 API 共享同一张 `memDC`，最后 `UpdateLayeredWindow` 一次性合成。
- **圆角、阴影、配色** 三细节：圆角由 `GraphicsRoundRectPath` + `IsToRoundStruct` 控制；阴影用盒滤波近似高斯（`boxesForGauss` + 4 次可分离盒滤波）实现；配色由 `_RimeGetColor` 把 ARGB/RGBA 统一转成 Windows 的 ABGR(COLORREF)，GDI+ 与 DirectWrite 取色方式一致。
- **自愈机制**：`BindDC` 与 `EndDraw` 失败都会触发 `_InitFontRes(true)` 重建 DWR，让绘制能在设备丢失后自动恢复。
- **几何与绘制解耦**：绘制层只通过 `Layout` 的 getter（`GetCandidateRect`、`GetContentRect` 等）拿矩形，不关心具体排布——这是 [u5-l2](u5-l2-layout-system.md) 讲过的「布局换、绘制不变」的关键。

## 7. 下一步学习建议

本讲讲完了 u5「候选窗口 UI 渲染」单元。接下来建议：

- **横向打通配色实战**：进入 [u7-l3 配色方案与样式定制实战](u7-l3-color-scheme-and-style-customization.md)，亲手写一份 `weasel.custom.yaml`，把本讲讲的 `UIStyle` 颜色字段、`color_format` 转换、圆角/阴影参数串成一个可运行的配色方案。
- **回顾部署侧**：[u6-l1 WeaselDeployer 配置器](u6-l1-weasel-deployer-configurator.md) 讲这些样式是怎么经 `UIStyleSettingsDialog` 写回 `weasel.custom.yaml` 的，能补全「用户改样式 → 文件 → 加载 → DWR 重建 → 重绘」的完整闭环。
- **若想深入扩展**：尝试仿照 4.2.4 的对照表，加一个新的绘制步骤（例如在状态图标层之后画一个自定义水印），体会「几何来自 Layout、形状用 GDI+、文字用 DirectWrite」这条统一规则。
