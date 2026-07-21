# 候选列表、语言栏与显示属性

## 1. 本讲目标

前几讲我们走通了「按键被抓到 → 经命名管道发给 Server → Server 算出候选与上屏文字 → 前端用 EditSession 把文字写进应用文档」这条主链路。但还剩下三块「看得见、却还没拆」的前端能力：

- **候选列表（CandidateList）**：那个浮在光标旁边、显示「1 你好 2 你 3 泥」的小窗口，用户怎么用鼠标点选、滚轮翻页，点选的结果又是怎么传回 Server 的？
- **语言栏（LanguageBar）**：任务栏上那个中/英、输入法图标按钮，左键点一下切中英、右键弹菜单，它怎么和输入法状态联动？
- **显示属性（DisplayAttribute）与状态存储（Compartment）**：内联写作模式下那根下划线是哪来的？「键盘开/关」「中/英/全半角」这些状态存在 TSF 的哪里？

读完本讲，你应当能够：

- 说清楚 `CCandidateList` 如何同时实现 `ITfUIElement`/`ITfCandidateListUIElement`/`ITfIntegratableCandidateListUIElement` 等一组 TSF 接口，把 Weasel 自绘的候选窗口「伪装」成 TSF 认识的候选 UI 元素。
- 画出「鼠标点击候选 → `_UICallback` → `HandleUICallback` → `m_client.SelectCandidateOnCurrentPage` → IPC 命令 → 模拟 `VK_SELECT` 取回结果上屏」的完整调用链。
- 解释 `CLangBarItemButton` 如何通过左键切 `ascii_mode`、右键弹菜单、图标随状态刷新，以及它如何把状态写进 TSF 的 Conversion compartment。
- 区分 DisplayAttribute（给写作串打「下划线」样式属性）与 Compartment（TSF 提供的「键值状态仓库」）两套机制各自的用途。

本讲是 u3 单元（TSF 前端）的最后一讲，承接 u3-l1 的 `ActivateEx` 初始化链与 u3-l3 的 EditSession/Composition。本讲只讲**前端**这三块表面能力的实现；候选文字本身由谁算出来、配色与布局如何绘制，分别留给 u4（RimeWithWeasel）与 u5（WeaselUI 渲染）。

## 2. 前置知识

阅读本讲前，建议你已经理解（u3-l1、u3-l2、u3-l3、u2 已铺垫）：

- **TSF 接口聚合**：一个 TSF 文本服务对象（`WeaselTSF`）会同时实现很多 `ITfXxx` 接口，系统通过 `QueryInterface` 按需取到对应接口的指针。本讲里的 `CCandidateList`、`CLangBarItemButton`、`CCompartmentEventSink` 都是各自聚合一组接口、独立维护引用计数的 COM 对象。
- **UIElement 机制**：TSF 提供 `ITfUIElementMgr`，允许输入法把自己的 UI 注册成一个「UI 元素」。这样即便应用（尤其 UWP / 触摸键盘 / 无障碍辅助）自己想画候选，也能通过 `ITfUIElement` 拿到候选数量、当前选中、字符串等数据。Weasel 选择「自己画窗口」的同时，仍把这些元数据暴露成 UIElement，兼顾两者。
- **EditSession 与上屏（u3-l3）**：把文字写进应用文档必须走 `RequestEditSession` + `DoEditSession`。本讲 4.1 里「点选候选后怎么上屏」会复用这条路径。
- **命名管道 IPC 命令（u2-l1）**：`WEASEL_IPC_SELECT_CANDIDATE_ON_CURRENT_PAGE`、`WEASEL_IPC_HIGHLIGHT_CANDIDATE_ON_CURRENT_PAGE`、`WEASEL_IPC_CHANGE_PAGE`、`WEASEL_IPC_TRAY_COMMAND` 等命令是前端告诉 Server「我选了/高亮了/翻页了/点了托盘菜单」的合同。

> 术语提示：本讲里「候选窗口」「候选 UI」指的是 Weasel 自己用 DirectWrite 画的那个浮层（u5 会讲怎么画）；而「候选 UI 元素（UIElement）」是把它注册给 TSF 的元数据视图。两者由同一个 `CCandidateList` 对象承担，但角色不同，不要混淆。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [WeaselTSF/CandidateList.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp) | **4.1 主战场**。`CCandidateList` 实现 TSF 候选 UI 元素全套接口 + `_ui`（自绘窗口）生命周期管理 + `HandleUICallback` 把鼠标交互翻译成 IPC 命令。 |
| [WeaselTSF/CandidateList.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.h) | `CCandidateList` 的接口声明：可见它继承 `ITfIntegratableCandidateListUIElement` 与 `ITfCandidateListUIElementBehavior` 两个 TSF 接口。 |
| [WeaselUI/WeaselPanel.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp) | 候选窗口本身的鼠标事件处理：`OnLeftClickedUp`/`OnLeftClickedDown`/`OnMouseWheel` 在判定命中哪个候选后，通过 `_UICallback` 回调通知 `CCandidateList`。 |
| [include/WeaselUI.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUI.h) | `weasel::UI` 的 `uiCallback()`/`SetUICallBack()`：定义回调签名 `void(size_t* sel, size_t* hov, bool* next, bool* scroll_next)`，是 UI 与 TSF 前端之间的「四参数信使」。 |
| [WeaselTSF/LanguageBar.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/LanguageBar.cpp) | **4.2 主战场**。`CLangBarItemButton` 实现语言栏按钮（图标/文字/菜单），`_InitLanguageBar`/`_UpdateLanguageBar`/`_HandleLangBarMenuSelect` 负责注册、状态同步与菜单分发。 |
| [WeaselTSF/LanguageBar.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/LanguageBar.h) | `CLangBarItemButton` 的接口声明：继承 `ITfLangBarItemButton` + `ITfSource`。 |
| [WeaselTSF/DisplayAttribute.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/DisplayAttribute.cpp) | **4.3 上半**。`_SetCompositionDisplayAttributes`/`_ClearCompositionDisplayAttributes` 给写作串区域打上/清除显示属性，`_InitDisplayAttributeGuidAtom` 把自定义属性 GUID 注册成 `TfGuidAtom`。 |
| [WeaselTSF/DisplayAttributeInfo.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/DisplayAttributeInfo.cpp) | 显示属性的实际样式定义 `_daiDisplayAttribute`（虚线下划线 `TF_LS_DOT`）。 |
| [WeaselTSF/DisplayAttributeProvider.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/DisplayAttributeProvider.cpp) | `ITfDisplayAttributeProvider` 的 `EnumDisplayAttributeInfo`/`GetDisplayAttributeInfo`：把上面的样式枚举给 TSF。 |
| [WeaselTSF/Compartment.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp) | **4.3 下半**。`CCompartmentEventSink` 监听 compartment 变化；`_IsKeyboardOpen`/`_SetKeyboardOpen`/`_Get/SetCompartmentDWORD` 读写 TSF 状态仓库；`_InitCompartment`/`_HandleCompartment` 负责挂载与响应。 |
| [WeaselTSF/Compartment.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.h) | `CCompartmentEventSink` 声明：把一个 `std::function<HRESULT(REFGUID)>` 包成 TSF sink。 |
| [WeaselIPC/WeaselClientImpl.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp) | `SelectCandidateOnCurrentPage`/`HighlightCandidateOnCurrentPage`/`ChangePage`/`TrayCommand`：把前端动作翻译成对应 IPC 命令发往管道。 |
| [WeaselTSF/WeaselTSF.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp) | 构造函数里 `_cand = new CCandidateList(this)`，`ActivateEx` 里依次 `_InitDisplayAttributeGuidAtom`/`_InitLanguageBar`/`_InitCompartment`。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 候选列表交互**：`CCandidateList` 如何既当 TSF 的候选 UI 元素、又当自绘窗口的宿主，并把鼠标交互翻译成 IPC。
- **4.2 语言栏与状态切换**：`CLangBarItemButton` 如何呈现图标、响应左右键、并把状态写进 Conversion compartment。
- **4.3 显示属性与 Compartment**：写作串下划线怎么打、TSF 状态仓库怎么读写与监听。

### 4.1 候选列表交互

#### 4.1.1 概念说明

Windows TSF 对「输入法候选 UI」给了一个标准抽象：`ITfUIElement` + `ITfCandidateListUIElement`（及扩展的 `ITfCandidateListUIElementBehavior`、`ITfIntegratableCandidateListUIElement`）。实现这些接口的对象可以注册到 `ITfUIElementMgr`，于是 TSF、触摸键盘、无障碍工具都能统一地问它：「你现在有几个候选？高亮了第几个？第 N 个字符串是什么？」。

Weasel 的设计是**「自己画窗口」+「同时把元数据暴露成 UIElement」**两不误：

- 候选窗口本身是 `weasel::UI`（实现在 WeaselUI 子工程，u5 讲怎么用 DirectWrite 画）。`CCandidateList` 持有一个 `std::unique_ptr<weasel::UI> _ui`。
- `CCandidateList` 自己实现那一组 TSF 接口，`GetCount`/`GetSelection`/`GetString` 等方法直接读 `_ui->ctx().cinfo` 里的数据返回给 TSF。

这样做的好处是：在普通桌面应用里，Weasel 用自己的漂亮窗口；而在 UWP / 触摸键盘等场景，应用可以决定「我自己来画」，这时它通过 UIElement 接口从 `CCandidateList` 取数据。

#### 4.1.2 核心流程

候选 UI 元素的完整生命周期：

```text
开始写作串 _StartComposition
   └─> _cand->StartUI()
         ├─ 若尚未注册回调：_ui->SetUICallBack(HandleUICallback)  ← 把鼠标事件接入 TSF 侧
         ├─ pUIElementMgr->BeginUIElement(this, &_pbShow, &uiid)  ← 向 TSF 注册，拿到元素 id
         └─ 若 _pbShow（TSF 允许自绘）：_MakeUIWindow() → _ui->Create(activeWnd)

每次按键 DoEditSession 末尾
   └─ _UpdateUI(ctx, status)
         ├─ _ui->Update(ctx, status)        ← 刷新窗口内容
         ├─ _UpdateUIElement()              ← pUIElementMgr->UpdateUIElement(uiid) 通知 TSF 元素变了
         └─ Show(status.composing ? _pbShow : FALSE)  ← composing 才显示

鼠标在候选窗口上交互
   └─ WeaselPanel::_UICallback(sel?, hov?, next?, scroll_next?)  ← 四参数，非空者表示动作
         └─ HandleUICallback
               ├─ sel   → _SelectCandidateOnCurrentPage → m_client.SelectCandidateOnCurrentPage + 模拟 VK_SELECT
               ├─ hov   → _HandleMouseHoverEvent        → m_client.HighlightCandidateOnCurrentPage
               └─ next/scroll_next → _HandleMousePageEvent → m_client.ChangePage / Highlight / ProcessKeyEvent

结束/销毁
   └─ Destroy() → Show(FALSE) + _DisposeUIWindow()
   └─ EndUI()   → pUIElementMgr->EndUIElement(uiid) + _DisposeUIWindow()
```

四个回调参数的语义（这是本模块最关键的「协议」）：

| 参数 | 类型 | 非空时表示的动作 | 最终 IPC 命令 |
| --- | --- | --- | --- |
| `sel` | `size_t*` | 选中某个候选（定稿上屏） | `WEASEL_IPC_SELECT_CANDIDATE_ON_CURRENT_PAGE` |
| `hov` | `size_t*` | 高亮（hover）某个候选，但还没定稿 | `WEASEL_IPC_HIGHLIGHT_CANDIDATE_ON_CURRENT_PAGE` |
| `next` | `bool*` | 点击上一页/下一页按钮 | `WEASEL_IPC_CHANGE_PAGE` |
| `scroll_next` | `bool*` | 鼠标滚轮翻页或滚动高亮 | `WEASEL_IPC_CHANGE_PAGE` / `HIGHLIGHT` / `PROCESS_KEY_EVENT` |

#### 4.1.3 源码精读

**(a) 多接口聚合 + 引用计数**

`CCandidateList` 同时实现 `ITfIntegratableCandidateListUIElement` 与 `ITfCandidateListUIElementBehavior`，`QueryInterface` 按请求的 IID 返回不同接口指针：

[WeaselTSF/CandidateList.cpp:18-41](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L18-L41) —— `QueryInterface` 里把 `ITfUIElement`/`ITfCandidateListUIElement`/`ITfCandidateListUIElementBehavior` 三个 IID 映射到同一个 `this`（强转成 behavior 指针），把 `IUnknown`/`ITfIntegratableCandidateListUIElement` 映射到 integratable 指针。这是 COM 多接口聚合的标准写法。

引用计数 `_cRef` 在构造时为 1（[CandidateList.cpp:11-14](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L11-L14)），`Release` 减到 0 时 `delete this`（[CandidateList.cpp:47-57](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L47-L57)）。

**(b) 把 `_ui` 的数据暴露给 TSF**

TSF 通过下面这组方法读取候选元数据，它们全是直接读 `_ui->ctx().cinfo`：

- [WeaselTSF/CandidateList.cpp:110-113](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L110-L113) —— `GetCount` 返回 `cinfo.candies.size()`（候选总数）。
- [WeaselTSF/CandidateList.cpp:115-118](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L115-L118) —— `GetSelection` 返回 `cinfo.highlighted`（当前高亮下标）。
- [WeaselTSF/CandidateList.cpp:120-130](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L120-L130) —— `GetString(uIndex)` 返回 `candies[uIndex].str`。

`cinfo` 就是 u2-l4 讲过的 `CandidateInfo`，由按下标对齐的 `candies`/`comments`/`labels` 三数组 + `highlighted` 等字段构成。也就是说，TSF 看到的候选数据，和自绘窗口用的，是同一份。

**(c) UIElement 生命周期：StartUI / UpdateUI / EndUI**

注册与建窗在 `StartUI` 里完成，关键一步是 `BeginUIElement`：

[WeaselTSF/CandidateList.cpp:285-311](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L285-L311) —— 先确保回调已挂上（`SetUICallBack`），再 `pUIElementMgr->BeginUIElement(this, &_pbShow, &uiid)`：把 `this`（候选元素）注册给 TSF，TSF 回填两样东西——`_pbShow`（是否允许输入法自己显示窗口，UWP 等场景可能由应用接管而返回 FALSE）、`uiid`（元素 id，后续 `UpdateUIElement`/`EndUIElement` 都靠它）。仅当 `_pbShow` 为真才 `_MakeUIWindow()` 真正创建自绘窗口。

`StartUI` 在 `_StartComposition`（开始写作串）时被调用：

[WeaselTSF/Composition.cpp:73-84](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L73-L84) —— `_StartComposition` 里先 `_cand->StartUI()`，再发起 `CStartCompositionEditSession`。即：候选 UI 元素在写作串开始时就向 TSF 登记。

每次按键后的刷新走 `UpdateUI`：

[WeaselTSF/CandidateList.cpp:201-218](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L201-L218) —— 先按 `inline_preedit` 设置 `client_caps`（告诉 Server 本客户端是否支持内联写作，u4 会用），`_ui->Update(ctx, status)` 刷新窗口内容，`_UpdateUIElement()` 通知 TSF「元素数据变了」（让接管方重读 `GetCount`/`GetSelection`），最后按 `status.composing` 决定 `Show` 或隐藏。

其中 `_UpdateUIElement` 的实现：

[WeaselTSF/CandidateList.cpp:268-283](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L268-L283) —— 取 `ITfUIElementMgr`，调 `UpdateUIElement(uiid)`。这就是「自绘窗口」与「TSF 元素视图」同步的纽带。

`_UpdateUI` 由 `WeaselTSF::_UpdateUI` 转发，调用点是 `DoEditSession` 末尾（[EditSession.cpp:44](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/EditSession.cpp#L44)），即每次按键处理完都刷新一次候选 UI。

**(d) 鼠标交互入口：四参数回调**

回调的挂接在 `StartUI` 里：

[WeaselTSF/CandidateList.cpp:300-304](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L300-L304) —— 若 `_ui->uiCallback()` 为空（尚未设置），则 `SetUICallBack([this](sel,hov,next,scroll_next){ _tsf->HandleUICallback(...); })`。于是自绘窗口的鼠标事件就能回调到 TSF 侧的 `HandleUICallback`。

回调签名定义在 `weasel::UI`：

[include/WeaselUI.h:71-80](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUI.h#L71-L80) —— `std::function<void(size_t* const, size_t* const, bool* const, bool* const)>`，四个指针参数分别对应 sel/hov/next/scroll_next。

自绘窗口（`WeaselPanel`）在判定鼠标命中后填充对应参数并触发回调。例如点击「当前高亮项」即为「选中」：

[WeaselUI/WeaselPanel.cpp:298-334](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L298-L334) —— `OnLeftClickedUp`（左键抬起）：取当前高亮项的矩形，若点击点落在其中，则 `_UICallback(&i, NULL, NULL, NULL)`（`i = cinfo.highlighted`），即「选中当前高亮」。

[WeaselUI/WeaselPanel.cpp:422-443](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L422-L443) —— `OnLeftClickedDown`（左键按下）：遍历每个候选矩形，若命中且与当前高亮不同，则 `_UICallback(NULL, &i, NULL, NULL)`，即「先高亮（hover）这一项」，需要再点一次（此时它已是高亮项）才会触发上面的「选中」。

> 关键交互模型：Weasel 的候选窗口是「点击切换高亮，再点击高亮项才上屏」的「两次点击」模型（除非 `hover_type` 设为悬停即高亮）。这一点在做本讲实践时要重点观察。

滚轮翻页：

[WeaselUI/WeaselPanel.cpp:285-296](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L285-L296) —— `OnMouseWheel`：`_UICallback(NULL, NULL, NULL, &nextpage)`，`nextpage = delta < 0`（向下滚=下一页）。

**(e) 把交互翻译成 IPC：HandleUICallback 与三条分支**

[WeaselTSF/CandidateList.cpp:431-441](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L431-L441) —— `HandleUICallback` 按「非空优先级」分发：`sel` 非空→选中，否则 `hov` 非空→hover，否则 `next`/`scroll_next`→翻页。注意四类动作互斥（一次回调只填一个参数）。

选中分支是最精妙的一段：

[WeaselTSF/CandidateList.cpp:380-391](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L380-L391) —— `_SelectCandidateOnCurrentPage(index)`：

1. `m_client.SelectCandidateOnCurrentPage(index)` —— 经命名管道发 `WEASEL_IPC_SELECT_CANDIDATE_ON_CURRENT_PAGE`，告诉 Server「用户选了当前页第 index 个」，Server（RimeWithWeasel）据此让 librime 完成选词、产生 commit 文字。
2. 然后**模拟一次 `VK_SELECT` 按键**（`SendInput` 一个按下+抬起），故意走标准的「按键 → `OnKeyDown` → `_ProcessKeyEvent` → IPC `PROCESS_KEY_EVENT` → `DoEditSession`」流程，把上一步产生的 commit/preedit 取回并写进应用文档。

注释里写明这是个 workaround（`fix me: are there any better ways?`）：选词后的结果要通过模拟按键才能「借道」既有上屏链路取回。`VK_SELECT` 之所以选它，是因为 `TranslateKeycode` 对它返回非零（u3-l2 讲过，非零才不会被当成普通字符键），能保证走按键处理主链路。

hover 与翻页分支：

[WeaselTSF/CandidateList.cpp:421-429](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L421-L429) —— `_HandleMouseHoverEvent`：若 `index` 与当前高亮不同，`m_client.HighlightCandidateOnCurrentPage(index)` + `_UpdateComposition` 刷新。

[WeaselTSF/CandidateList.cpp:393-419](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L393-L419) —— `_HandleMousePageEvent`：滚轮走 `paging_on_scroll` 分支（直接 `ChangePage`），否则移动高亮（到边界时用 `ProcessKeyEvent` 发上/下方向键让 librime 自然翻页）；点击翻页按钮走 `ChangePage`。

**(f) 客户端封装**

这三个动作在 `ClientImpl` 里都是「`_Active()` 守卫 → `_SendMessage` 发对应命令」的模板：

[WeaselIPC/WeaselClientImpl.cpp:83-104](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L83-L104) —— `SelectCandidateOnCurrentPage` 发 `WEASEL_IPC_SELECT_CANDIDATE_ON_CURRENT_PAGE(index, session_id)`；`HighlightCandidateOnCurrentPage` 发 `WEASEL_IPC_HIGHLIGHT_CANDIDATE_ON_CURRENT_PAGE`；`ChangePage(backward)` 发 `WEASEL_IPC_CHANGE_PAGE`。这三个命令在枚举里相邻：

[include/WeaselIPC.h:31-34](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L31-L34) —— `WEASEL_IPC_TRAY_COMMAND`、`SELECT_CANDIDATE_ON_CURRENT_PAGE`、`HIGHLIGHT_CANDIDATE_ON_CURRENT_PAGE`、`CHANGE_PAGE` 顺序定义。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，确认「鼠标点击候选」走的到底是「选中」还是「高亮」分支，并理解为什么需要模拟 `VK_SELECT`。

**操作步骤**：

1. 打开 [WeaselUI/WeaselPanel.cpp:298-334](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L298-L334)（`OnLeftClickedUp`）与 [WeaselPanel.cpp:422-443](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L422-L443)（`OnLeftClickedDown` 的候选命中段）。
2. 对照阅读 [CandidateList.cpp:431-441](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L431-L441)（`HandleUICallback`）与 [CandidateList.cpp:380-391](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L380-L391)（`_SelectCandidateOnCurrentPage`）。
3. 在 `_SelectCandidateOnCurrentPage` 的 `m_client.SelectCandidateOnCurrentPage(index);` 这一行**之后**临时加一行日志（例如 `OutputDebugStringW(L"[weasel] select candidate -> simulate VK_SELECT\n");`），便于在 DebugView 里观察。
4. （可选进阶）阅读 u3-l2 的 `ConvertKeyEvent`/`TranslateKeycode`，确认 `VK_SELECT` 为何能走按键主链路。

**需要观察的现象**：

- 单击一个**非高亮**候选：先触发 `OnLeftClickedDown` 的 hover 分支（`_UICallback(NULL, &i, ...)`），候选高亮跳到该项；该次点击**不**上屏。
- 再次点击**已高亮**项：触发 `OnLeftClickedUp` 的选中分支（`_UICallback(&i, ...)`），候选上屏。
- 若能看到日志，会发现选中分支里「先 IPC 选词、再模拟 `VK_SELECT`」两步紧挨着发生。

**预期结果**：能在源码层面说清楚「点击非高亮项 = hover 高亮」「点击高亮项 = 选中上屏」这一双击模型，并能解释 `VK_SELECT` 模拟按键是为了借道 `DoEditSession` 上屏。

> 说明：本实践涉及 Windows GUI 行为，无法在当前 Linux 环境运行验证，结论基于源码逻辑推导，待本地在 Windows 上验证。

#### 4.1.5 小练习与答案

**练习 1**：`CCandidateList::QueryInterface` 为什么要把不同 IID 映射到「两个不同的 `this` 指针强转」（behavior 指针 vs integratable 指针），而不是统一返回一个？

**参考答案**：因为 `CCandidateList` 多重继承了 `ITfIntegratableCandidateListUIElement` 与 `ITfCandidateListUIElementBehavior` 两个有独立 vtable 的接口基类，`static_cast` 到不同接口得到的对象地址会因 vtable 偏移而不同。COM 要求 `QueryInterface` 对同一 IID 返回**稳定且与对象身份一致**的指针；对这两个接口族分别返回各自正确的接口指针，调用方才能正确解引用 vtable。这是 C++ 多继承下 COM 的标准处理。

**练习 2**：假如 `BeginUIElement` 回填的 `_pbShow` 为 `FALSE`（比如在某个 UWP 应用里 TSF 想自己接管绘制），Weasel 的候选窗口还会出现吗？数据还能被 TSF 读到吗？

**参考答案**：不会出现自绘窗口——`StartUI` 里 `if (_pbShow) { _MakeUIWindow(); }` 决定只有 `_pbShow` 为真才建窗（[CandidateList.cpp:307-310](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L307-L310)）。但数据仍可被 TSF 读到，因为 `CCandidateList` 已通过 `BeginUIElement` 注册成 UI 元素，TSF/应用可经 `GetCount`/`GetSelection`/`GetString` 读 `_ui->ctx().cinfo`，并用 `UpdateUIElement` 通知刷新。这就是「自绘 + 元数据视图」双轨设计的意义。

**练习 3**：为什么 `_SelectCandidateOnCurrentPage` 在发完 IPC 选词命令后，还要 `SendInput` 模拟一个 `VK_SELECT`？能不能直接在这里把结果写进文档？

**参考答案**：因为「把文字写进应用文档」必须走 TSF 的 EditSession 机制（u3-l3），而当前这串代码并不在 EditSession 上下文里（没有 `TfEditCookie`）。模拟 `VK_SELECT` 是为了把后续处理「借用」到标准的按键主链路（`OnKeyDown`→`_ProcessKeyEvent`→IPC→`DoEditSession`），由 `DoEditSession` 在合法的编辑会话里完成上屏。源码注释也标注这是 workaround，存在更优雅的实现空间。

### 4.2 语言栏与状态切换

#### 4.2.1 概念说明

Windows 的语言栏（现在多体现为任务栏输入法图标）是 TSF 的 `ITfLangBarItem` 体系。一个语言栏项需要实现：

- `ITfLangBarItem`：提供项的基本信息（`GetInfo`）、状态（`GetStatus`）、显隐（`Show`）。
- `ITfLangBarItemButton`：按钮特有行为——点击（`OnClick`）、菜单初始化（`InitMenu`）、菜单选择（`OnMenuSelect`）、图标（`GetIcon`）、文字（`GetText`）。
- `ITfSource`：允许 TSF（或别人）挂 `ITfLangBarItemSink` 来监听按钮状态/图标变化（`AdviseSink`/`UnadviseSink`）。

Weasel 的语言栏项 `CLangBarItemButton` 同时实现这三者，承担：

- **左键**：在中/英（`ascii_mode`）之间切换，并同步给 Server。
- **右键**：弹出右键菜单（简体/繁体/英文不同菜单资源），菜单项分发到 `_HandleLangBarMenuSelect`。
- **图标**：按 `ascii_mode` 与当前方案图标（`current_zhung_icon`/`current_ascii_icon`）显示中/英图标。
- **状态同步**：Server 回传的 `Status.ascii_mode` 变化时，刷新图标。

#### 4.2.2 核心流程

```text
ActivateEx
   └─ _InitLanguageBar()
         └─ new CLangBarItemButton(this, GUID_LBI_INPUTMODE, _cand->style())
         └─ pLangBarItemMgr->AddItem(_pLangBarButton)   ← 注册到 TSF 语言栏
         └─ _pLangBarButton->Show(TRUE)

左键点击 (TF_LBI_CLK_LEFT)
   └─ OnClick
         └─ _HandleLangBarMenuSelect(ascii_mode ? DISABLE_ASCII : ENABLE_ASCII)
         └─ ascii_mode 翻转
         └─ _pLangBarItemSink->OnUpdate(STATUS|ICON)   ← 通知 TSF 重画图标

右键点击 (TF_LBI_CLK_RIGHT)
   └─ OnClick
         └─ 按 get_language_id() 加载 HANS/HANT/英文菜单资源
         └─ TrackPopupMenuEx → 得到 wID
         └─ _HandleLangBarMenuSelect(wID)
               └─ 各分支：打开安装目录/用户目录/日志/文档/论坛
               └─ default: m_client.TrayCommand(wID)   ← 转发托盘命令给 Server

Server 回传新 Status
   └─ DoEditSession → _UpdateLanguageBar(_status)
         └─ 按 ascii_mode/full_shape 改写 GUID_COMPARTMENT_KEYBOARD_INPUTMODE_CONVERSION
         └─ _pLangBarButton->UpdateWeaselStatus(stat)
               └─ 同步 ascii_mode、方案图标
               └─ _pLangBarItemSink->OnUpdate(STATUS|ICON)
```

#### 4.2.3 源码精读

**(a) 注册与项信息**

[WeaselTSF/LanguageBar.cpp:367-387](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/LanguageBar.cpp#L367-L387) —— `_InitLanguageBar`：`new CLangBarItemButton(this, GUID_LBI_INPUTMODE, _cand->style())`（注意它拿到的是候选列表的 `style()` 引用，所以方案图标能实时读取），`AddItem` 注册到 `ITfLangBarItemMgr`，再 `Show(TRUE)`。

项的样式声明：

[WeaselTSF/LanguageBar.cpp:102-110](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/LanguageBar.cpp#L102-L110) —— `GetInfo` 设 `dwStyle = TF_LBI_STYLE_BTN_BUTTON | TF_LBI_STYLE_BTN_MENU | TF_LBI_STYLE_SHOWNINTRAY`：既像按钮（可点）又像菜单（可弹），且在系统托盘显示。

**(b) 左键切中英、右键弹菜单**

[WeaselTSF/LanguageBar.cpp:151-183](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/LanguageBar.cpp#L151-L183) —— `OnClick`：

- 左键（`TF_LBI_CLK_LEFT`）：调 `_HandleLangBarMenuSelect(ascii_mode ? ID_WEASELTRAY_DISABLE_ASCII : ID_WEASELTRAY_ENABLE_ASCII)`（即「当前是英文就关英文、当前是中文就开英文」），翻转 `ascii_mode`，再 `OnUpdate(TF_LBI_STATUS | TF_LBI_ICON)` 让 TSF 重画图标。
- 右键（`TF_LBI_CLK_RIGHT`）：按 `get_language_id()` 选简体（`IDR_MENU_POPUP_HANS`）/繁体（`HANT`）/英文（`IDR_MENU_POPUP`）菜单资源，`TrackPopupMenuEx` 弹出并把选中的 `wID` 交给 `_HandleLangBarMenuSelect`。

工具提示也按语言分流：

[WeaselTSF/LanguageBar.cpp:137-149](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/LanguageBar.cpp#L137-L149) —— `GetTooltipString` 按简/繁/英文返回不同提示串（「左键切换模式，右键打开菜单」/「Left-click to switch modes…」）。

**(c) 菜单分发：本地动作 vs 托盘命令**

[WeaselTSF/LanguageBar.cpp:295-339](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/LanguageBar.cpp#L295-L339) —— `_HandleLangBarMenuSelect`：

- `ID_WEASELTRAY_RERUN_SERVICE`：异步 `ShellExecuteW` 跑 `start_service.bat` 重启服务。
- `ID_WEASELTRAY_INSTALLDIR`/`USERCONFIG`/`LOGDIR`：从注册表读路径后用 `open()`（`ShellExecuteW`）打开安装目录、用户配置目录（`%AppData%\Rime`）、日志目录。
- `ID_WEASELTRAY_WIKI`/`FORUM`：打开 rime.im 文档/讨论页。
- `default`：`m_client.TrayCommand(wID)`——把菜单 id 当托盘命令经 IPC 发给 Server（u6-l3 会讲 Server 侧的 `TRAY_COMMAND` 派发）。

即：语言栏菜单和系统托盘菜单共用同一套 `ID_WEASELTRAY_*` 命令 id，绝大多数项最终都走 `TrayCommand` → `WEASEL_IPC_TRAY_COMMAND`。

**(d) 图标随状态变化**

[WeaselTSF/LanguageBar.cpp:198-221](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/LanguageBar.cpp#L198-L221) —— `GetIcon`：若 `ascii_mode` 为真，按 `_style.current_ascii_icon` 是否为空决定「从方案图标文件加载」还是「用内置 `IDI_EN`」；否则用 `current_zhung_icon` 或内置 `IDI_ZH`。这样切换方案/中英时图标自动变化。

**(e) 状态同步：UpdateWeaselStatus 与 _UpdateLanguageBar**

[WeaselTSF/LanguageBar.cpp:252-265](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/LanguageBar.cpp#L252-L265) —— `UpdateWeaselStatus`：当 Server 回传的 `Status.ascii_mode` 与本地不同，或方案图标变了，就同步并 `OnUpdate(STATUS|ICON)` 刷新。

[WeaselTSF/LanguageBar.cpp:402-418](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/LanguageBar.cpp#L402-L418) —— `_UpdateLanguageBar`：除了调 `UpdateWeaselStatus`，还会按 `ascii_mode`/`full_shape` 改写 TSF 的 `GUID_COMPARTMENT_KEYBOARD_INPUTMODE_CONVERSION`（设/清 `TF_CONVERSIONMODE_NATIVE`、`TF_CONVERSIONMODE_FULLSHAPE`）。这一步把 Weasel 自己的中/英、全/半角状态「翻译」成 TSF 标准的 Conversion 模式位，让系统其它部分（如输入指示器）保持一致。compartment 的读写见 4.3。

#### 4.2.4 代码实践

**实践目标**：通过源码阅读，搞清楚「左键点语言栏」这一下到底改了什么状态、触发了哪些 IPC。

**操作步骤**：

1. 阅读 [LanguageBar.cpp:151-160](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/LanguageBar.cpp#L151-L160)（左键分支），记下它调的菜单 id（`ID_WEASELTRAY_ENABLE_ASCII`/`DISABLE_ASCII`）。
2. 跟到 [LanguageBar.cpp:335-338](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/LanguageBar.cpp#L335-L338)（`default: m_client.TrayCommand(wID);`），再到 [WeaselClientImpl.cpp:141-143](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L141-L143)（`TrayCommand` 发 `WEASEL_IPC_TRAY_COMMAND`）。
3. 在 `CLangBarItemButton::OnClick` 左键分支的 `ascii_mode = !ascii_mode;` 处加一行日志，观察翻转方向。
4. 在 [resource.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/resource.h) 或菜单资源（`*.rc`）里搜索 `ID_WEASELTRAY_ENABLE_ASCII` / `IDR_MENU_POPUP_HANS`，确认它们确实存在（不编造）。

**需要观察的现象**：

- 左键点击语言栏：`ascii_mode` 翻转，图标在 `IDI_ZH`/`IDI_EN`（或方案自定义图标）间切换，并发送一条 `WEASEL_IPC_TRAY_COMMAND`。
- Server 收到命令后回传的新 `Status` 会经 `DoEditSession`→`_UpdateLanguageBar` 再次校准图标与 Conversion compartment。

**预期结果**：能说清「左键 = 发 ENABLE/DISABLE_ASCII 托盘命令 + 翻转本地 `ascii_mode` + 刷新图标」三件事，并理解真正的中/英切换状态权威在 Server（librime）侧，前端按钮只是触发与显示。

> 说明：涉及 Windows 运行时行为，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`CLangBarItemButton` 的 `_style` 是 `weasel::UIStyle&`（引用），它从哪来？为什么用引用而不是拷贝？

**参考答案**：构造时传入的是 `_cand->style()`（[LanguageBar.cpp:374-375](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/LanguageBar.cpp#L374-L375)），即候选列表持有的样式对象。用引用是为了「方案图标变化时语言栏能立刻读到新值」——`UpdateStyle`/Server 回传会原地修改 `UIStyle`，语言栏 `GetIcon` 每次都读最新内容，无需额外同步。代价是生命周期上语言栏项不得超出 `_cand`（两者都由 `WeaselTSF` 持有，满足）。

**练习 2**：为什么语言栏菜单和系统托盘菜单能共用同一组 `ID_WEASELTRAY_*`？

**参考答案**：因为 `_HandleLangBarMenuSelect` 的 `default` 分支直接 `m_client.TrayCommand(wID)`（[LanguageBar.cpp:335-338](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/LanguageBar.cpp#L335-L338)），把菜单 id 原样经 `WEASEL_IPC_TRAY_COMMAND` 发给 Server。Server 侧（u6-l3 的 `WeaselServerApp` 菜单表）也用同一组 id 注册了 `OnCommand` 处理。于是同一 id 在「语言栏右键菜单」「系统托盘左/右键菜单」两个入口都能命中同一处理逻辑，复用了一套命令编号。

### 4.3 显示属性与 Compartment

本模块把两个相对独立、但都属于「TSF 标准设施」的机制合在一起讲：DisplayAttribute 负责给写作串「贴样式标签」，Compartment 负责「存状态」。

#### 4.3.1 概念说明

**DisplayAttribute（显示属性）**：TSF 允许输入法声明一种「显示属性」（`ITfDisplayAttributeProvider`），描述写作串应该长什么样（下划线样式、颜色、粗细等）。应用（Word、记事本等）在渲染写作串时会查询这段区域上的属性，按属性画出下划线。Weasel 定义了一个「虚线下划线」属性，在内联写作（`inline_preedit`）模式下贴到写作串上，让用户看到「这段文字还在拼、未定稿」。

属性本身用 GUID 标识（`c_guidDisplayAttributeInput`），但 TSF 内部为了高效比较，会用 `ITfCategoryMgr::RegisterGUID` 把它换算成一个 `TfGuidAtom`（一个 32 位整数原子）。

**Compartment（状态仓库）**：TSF 提供的一套「按 GUID 存取 `VARIANT` 值」的全局/线程级键值仓库，由 `ITfCompartmentMgr` 管理。TSF 预定义了一批 GUID，例如：

- `GUID_COMPARTMENT_KEYBOARD_OPENCLOSE`：输入法开/关（整个键盘是否启用）。
- `GUID_COMPARTMENT_KEYBOARD_INPUTMODE_CONVERSION`：转换模式位（`TF_CONVERSIONMODE_NATIVE` 中文、`TF_CONVERSIONMODE_FULLSHAPE` 全角等）。
- `GUID_COMPARTMENT_KEYBOARD_DISABLED`/`GUID_COMPARTMENT_EMPTYCONTEXT`：键盘禁用标志。

Weasel 用它来：读「键盘是否开」（`_IsKeyboardOpen`）、写「键盘开/关」（`_SetKeyboardOpen`）、读写转换模式位（`_Get/_SetCompartmentDWORD`），并监听变化（`CCompartmentEventSink`）。

#### 4.3.2 核心流程

**DisplayAttribute 流程**：

```text
ActivateEx
   └─ _InitDisplayAttributeGuidAtom()
         └─ CoCreateInstance(CLSID_TF_CategoryMgr)
         └─ RegisterGUID(c_guidDisplayAttributeInput) → _gaDisplayAttributeInput (TfGuidAtom)

内联写作 _ShowInlinePreedit (u3-l3)
   └─ _SetCompositionDisplayAttributes(ec, pContext, pRangeComposition)
         └─ pContext->GetProperty(GUID_PROP_ATTRIBUTE)
         └─ property->SetValue(ec, range, {VT_I4, _gaDisplayAttributeInput})  ← 给区域贴原子

结束写作串
   └─ _ClearCompositionDisplayAttributes(ec, pContext)
         └─ property->Clear(ec, range)   ← 清掉标签
```

应用渲染时通过 `GUID_PROP_ATTRIBUTE` 查到原子，再经 `ITfDisplayAttributeProvider::GetDisplayAttributeInfo`（用 `c_guidDisplayAttributeInput`）拿到 `TF_DISPLAYATTRIBUTE`（虚线下划线 `TF_LS_DOT`）。

**Compartment 流程**：

```text
ActivateEx
   └─ _InitCompartment()
         └─ new CCompartmentEventSink(回调=_HandleCompartment)  ×2
         └─ _Advise(_pThreadMgr, GUID_COMPARTMENT_KEYBOARD_OPENCLOSE)
         └─ _Advise(_pThreadMgr, GUID_COMPARTMENT_KEYBOARD_INPUTMODE_CONVERSION)

状态变化（如系统切换输入法开关）
   └─ CCompartmentEventSink::OnChange(guid)
         └─ _callback(guid) → _HandleCompartment(guid)
               ├─ OPENCLOSE 分支：读 _IsKeyboardOpen，关则清写作串，按需 EnableLanguageBar
               └─ CONVERSION 分支：读 _IsKeyboardOpen，开则拉取 Status 刷新语言栏

读写状态
   └─ _IsKeyboardOpen / _SetKeyboardOpen / _Get/SetCompartmentDWORD
         └─ GetCompartment(guid) → GetValue/SetValue(VARIANT)
```

#### 4.3.3 源码精读

**(a) 显示属性样式定义**

[WeaselTSF/DisplayAttributeInfo.cpp:10-17](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/DisplayAttributeInfo.cpp#L10-L17) —— `_daiDisplayAttribute`：文本色与背景色均为 `TF_CT_NONE`（用应用默认），下划线样式 `TF_LS_DOT`（虚线），不下划线加粗，属性类别 `TF_ATTR_INPUT`（输入态）。注释明确「只改样式，颜色留给应用」。

GUID 定义：

[WeaselTSF/Globals.cpp:31-36](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Globals.cpp#L31-L36) —— `c_guidDisplayAttributeInput = {2AC87E79-3260-4B32-9DEA-F8390976C20B}`。

**(b) 注册 GUID 原子**

[WeaselTSF/DisplayAttribute.cpp:55-75](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/DisplayAttribute.cpp#L55-L75) —— `_InitDisplayAttributeGuidAtom`：`CoCreateInstance(CLSID_TF_CategoryMgr)`，`pCategoryMgr->RegisterGUID(c_guidDisplayAttributeInput, &_gaDisplayAttributeInput)` 把 GUID 换算成原子存进成员 `_gaDisplayAttributeInput`。

注意 `ActivateEx` 里这一步**不受检**（失败也不 `goto ExitError`），源码注释说明某些应用（如部分 OpenGL 程序）不提供 DisplayAttributeInfo 会初始化失败，故容忍（[WeaselTSF.cpp:143-147](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L143-L147)）。

**(c) 贴/清显示属性**

[WeaselTSF/DisplayAttribute.cpp:24-53](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/DisplayAttribute.cpp#L24-L53) —— `_SetCompositionDisplayAttributes`：取写作串 range（参数为空则从 `_pComposition->GetRange` 取），`pContext->GetProperty(GUID_PROP_ATTRIBUTE)` 拿到属性对象，`SetValue(ec, range, &var)`，其中 `var.vt = VT_I4`、`var.lVal = _gaDisplayAttributeInput`（把原子作为属性值贴到 range 上）。

[WeaselTSF/DisplayAttribute.cpp:5-22](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/DisplayAttribute.cpp#L5-L22) —— `_ClearCompositionDisplayAttributes`：取 range，`GetProperty` 后 `Clear(ec, range)` 清掉属性。

贴属性发生在内联写作时（`_ShowInlinePreedit` 的编辑会话里，[Composition.cpp:290](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L290)），清除发生在结束写作串的会话里（[Composition.cpp:113](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L113)）。

**(d) DisplayAttributeProvider：把样式枚举给 TSF**

[WeaselTSF/DisplayAttributeProvider.cpp:7-46](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/DisplayAttributeProvider.cpp#L7-L46) —— `EnumDisplayAttributeInfo` 返回一个枚举器（`CEnumDisplayAttributeInfo`），`GetDisplayAttributeInfo(guidInfo)` 在 `guidInfo == c_guidDisplayAttributeInput` 时返回 `CDisplayAttributeInfoInput` 对象（持有上面的 `_daiDisplayAttribute`）。应用就是通过这条路径拿到「虚线下划线」样式的。

**(e) Compartment 事件 sink**

[WeaselTSF/Compartment.h:5-30](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.h#L5-L30) —— `CCompartmentEventSink` 把一个 `std::function<HRESULT(REFGUID)>` 包装成 `ITfCompartmentEventSink`。

[WeaselTSF/Compartment.cpp:46-48](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp#L46-L48) —— `OnChange(guid)` 直接 `_callback(guid)` 转发。

[WeaselTSF/Compartment.cpp:50-73](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp#L50-L73) —— `_Advise`：`QueryInterface(ITfCompartmentMgr)` → `GetCompartment(guid)` 拿到 compartment → `QueryInterface(ITfSource)` → `AdviseSink(IID_ITfCompartmentEventSink, this, &_cookie)` 完成订阅。`_Unadvise` 反向解订（[Compartment.cpp:74-88](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp#L74-L88)）。

**(f) 挂载两个 compartment 监听**

[WeaselTSF/Compartment.cpp:215-231](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp#L215-L231) —— `_InitCompartment`：用 `std::bind` 把 `_HandleCompartment` 绑成回调，`new` 两个 `CCompartmentEventSink`，分别 `_Advise` 到 `GUID_COMPARTMENT_KEYBOARD_OPENCLOSE` 与 `GUID_COMPARTMENT_KEYBOARD_INPUTMODE_CONVERSION`。

**(g) 变化处理：_HandleCompartment**

[WeaselTSF/Compartment.cpp:244-278](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp#L244-L278) —— `_HandleCompartment` 按 guid 分两支：

- `OPENCLOSE`：又按 `_isToOpenClose`（注册表 `ToggleImeOnOpenClose` 配置）分两种语义——若用作开/关，则读 `_IsKeyboardOpen`，关闭时清写作串 + `_EndComposition`，并 `_EnableLanguageBar`；否则当作中/英切换，翻转 `_status.ascii_mode`、`_SetKeyboardOpen(true)`、发 `ENABLE/DISABLE_ASCII` 托盘命令。
- `CONVERSION`：若键盘开，则构造一个只解析 `Status` 的 `ResponseParser`，`m_client.GetResponseData` 拉取最新状态，`_UpdateLanguageBar` 刷新。

**(h) 读写 compartment**

[WeaselTSF/Compartment.cpp:144-160](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp#L144-L160) —— `_IsKeyboardOpen`：`GetCompartment(GUID_COMPARTMENT_KEYBOARD_OPENCLOSE)` → `GetValue(&var)`，`var.vt == VT_I4` 时取 `var.lVal`。注释提醒「即便 `VT_EMPTY`，`GetValue` 也可能成功」，所以必须判 `vt`。

[WeaselTSF/Compartment.cpp:162-178](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp#L162-L178) —— `_SetKeyboardOpen`：构造 `var{VT_I4, fOpen}`，`pCompartment->SetValue(_tfClientId, &var)`。

[WeaselTSF/Compartment.cpp:180-213](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp#L180-L213) —— `_Get/_SetCompartmentDWORD` 是通用 DWORD 读写模板，`_UpdateLanguageBar`（4.2）用它操作 Conversion 模式位。

`_IsKeyboardDisabled`（[Compartment.cpp:90-142](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp#L90-L142)）读 `GUID_COMPARTMENT_KEYBOARD_DISABLED` 与 `GUID_COMPARTMENT_EMPTYCONTEXT`，是 u3-l2 按键放行的一道闸门。

#### 4.3.4 代码实践

**实践目标**：用源码阅读，追踪内联写作模式下「写作串下划线」从声明到贴上区域的全过程，并理解 compartment 是怎么把「系统级输入法开关」事件接进来的。

**操作步骤**：

1. 顺着 `_gaDisplayAttributeInput` 这个成员变量阅读：声明在 [WeaselTSF.h:232](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.h#L232)，注册在 [DisplayAttribute.cpp:55-75](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/DisplayAttribute.cpp#L55-L75)，使用在 [DisplayAttribute.cpp:40-44](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/DisplayAttribute.cpp#L40-L44)。
2. 阅读 [Composition.cpp:290](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L290) 与 [Composition.cpp:113](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L113)，确认贴属性与清属性的调用时机。
3. 阅读 [Compartment.cpp:215-231](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp#L215-L231) 与 [Compartment.cpp:244-278](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp#L244-L278)，画一张「compartment 变化 → OnChange → _HandleCompartment → 各动作」的时序。
4. 修改实验：把 [DisplayAttributeInfo.cpp:13](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/DisplayAttributeInfo.cpp#L13) 的 `TF_LS_DOT` 改成 `TF_LS_SOLID`（实线）或 `TF_LS_DASH`（粗虚线），重新编译（按 u1-l3 的 build 流程），在内联写作模式下观察下划线样式变化。

**需要观察的现象**：

- 内联写作（`inline_preedit: true`）时，应用文档里的写作串带虚线下划线；改属性后下划线样式随之变化。
- 非内联（独立候选窗口）模式下，文档里只有 CUAS 占位空格（不可见），看不到下划线——因为 `_SetCompositionDisplayAttributes` 只在内联路径被调用。
- 用系统快捷键开关输入法时，`_HandleCompartment` 的 `OPENCLOSE` 分支被触发。

**预期结果**：能说清「GUID → TfGuidAtom → 贴到 range 的属性值 → 应用查 Provider 拿样式」这条 DisplayAttribute 链，以及「compartment 是 TSF 的 GUID 键值仓库，Weasel 订阅了开/关与转换模式两个键」这一 Compartment 机制。

> 说明：第 4 步需在 Windows + MSBuild 环境编译运行，待本地验证；前 3 步为纯源码阅读，可直接完成。

#### 4.3.5 小练习与答案

**练习 1**：`_SetCompositionDisplayAttributes` 把 `_gaDisplayAttributeInput`（一个 `TfGuidAtom`，整数）作为属性值贴到 range 上，而不是直接贴 GUID。为什么要先经过 `RegisterGUID` 这一步？

**参考答案**：因为 TSF 的属性（`ITfProperty`，`GUID_PROP_ATTRIBUTE`）值用 `VARIANT` 存，而 TSF 内部对显示属性的比对、检索是按 `TfGuidAtom`（32 位整数）进行的——整数比较远比 GUID 字符串比较快，且占用更小。`ITfCategoryMgr::RegisterGUID` 把 GUID 登记进全局表并返回原子，后续 `SetValue(VT_I4, atom)` 与渲染时的查找都用这个原子。应用拿到原子后再反查回 GUID，经 `GetDisplayAttributeInfo` 取样式。

**练习 2**：`_IsKeyboardOpen` 里有一句注释「Even VT_EMPTY, GetValue() can succeed」。如果不判 `var.vt == VT_I4` 直接用 `var.lVal`，会有什么后果？

**参考答案**：compartment 可能从未被写过值，此时 `GetValue` 仍返回 `S_OK` 但 `var.vt == VT_EMPTY`，`var.lVal` 是未定义/0 的垃圾。直接用会把「未设置」误判成「0 = 键盘关闭」，导致输入法一启动就被认为关闭、无法输入。判 `vt` 是为了区分「真值为 0」与「根本没设过」。`_GetCompartmentDWORD` 同理在 `vt != VT_I4` 时返回 `S_FALSE`（[Compartment.cpp:188-191](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp#L188-L191)）。

**练习 3**：`_HandleCompartment` 里 `OPENCLOSE` 分支为何要根据 `_isToOpenClose` 走两套完全不同的逻辑？

**参考答案**：因为同一个 `GUID_COMPARTMENT_KEYBOARD_OPENCLOSE` 变化事件，在用户配置不同时语义不同：当 `ToggleImeOnOpenClose == "yes"`（注册表配置）时，开关事件就是「开/关输入法」，应清写作串、禁用语言栏；否则（默认）把它当作「中/英切换」的触发，翻转 `ascii_mode` 并强制保持键盘开。这是 Weasel 为兼容不同用户对「输入法开关键」的预期而做的策略分支，把 TSF 的一个通用事件映射成了两套应用级行为。

## 5. 综合实践

把三个模块串起来，完成规格要求的端到端追踪：**用户用鼠标点击候选列表第 2 项后的处理——从 CandidateList 事件到 `m_client.SelectCandidateOnCurrentPage` 的 IPC 调用，写出完整调用链。**

**前提**：当前高亮项不是第 2 项（这样能完整经历「先 hover 高亮、再选中」两步）。设当前 `cinfo.highlighted = 0`，用户点击第 2 项（下标 `i = 1`）。

**第一步：第一次点击 → hover 高亮（走 4.1 的 hov 分支）**

1. 鼠标左键按下，`WeaselPanel::OnLeftClickedDown` 命中第 1 项矩形，且 `i(1) != highlighted(0)`（[WeaselPanel.cpp:429-434](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L429-L434)）。
2. 触发 `_UICallback(NULL, &i, NULL, NULL)`（hov 非空）。
3. 回调进入 `WeaselTSF::HandleUICallback`（[CandidateList.cpp:431-441](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L431-L441)），因 `hov` 非空走 `_HandleMouseHoverEvent(1)`（[CandidateList.cpp:421-429](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L421-L429)）。
4. `m_client.HighlightCandidateOnCurrentPage(1)` → `_SendMessage(WEASEL_IPC_HIGHLIGHT_CANDIDATE_ON_CURRENT_PAGE, 1, session_id)`（[WeaselClientImpl.cpp:91-97](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L91-L97)），通知 Server 高亮第 1 项。
5. `_UpdateComposition` 触发 `DoEditSession`，回传的 `Context` 里 `cinfo.highlighted` 变为 1，候选窗高亮跳到第 2 项。

**第二步：第二次点击（点在现已高亮的第 2 项）→ 选中上屏（走 4.1 的 sel 分支）**

6. 鼠标左键抬起，`WeaselPanel::OnLeftClickedUp`：取 `highlighted(1)` 的矩形，命中（[WeaselPanel.cpp:315-327](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L315-L327)）。
7. 触发 `_UICallback(&i, NULL, NULL, NULL)`（sel 非空，`i = highlighted = 1`）。
8. `HandleUICallback` 因 `sel` 非空走 `_SelectCandidateOnCurrentPage(1)`（[CandidateList.cpp:431-441](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L431-L441)）。
9. **关键 IPC 调用**：`m_client.SelectCandidateOnCurrentPage(1)`（[CandidateList.cpp:381](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L381)）→ `ClientImpl::SelectCandidateOnCurrentPage`（[WeaselClientImpl.cpp:83-89](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L83-L89)）→ `_SendMessage(WEASEL_IPC_SELECT_CANDIDATE_ON_CURRENT_PAGE, 1, session_id)`。Server 侧的 `RimeWithWeaselHandler` 收到后让 librime 选定该候选，产生 commit 文字。
10. **借道上屏**：紧接着 `SendInput` 模拟 `VK_SELECT` 按下+抬起（[CandidateList.cpp:385-390](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.cpp#L385-L390)），走 `OnKeyDown`→`_ProcessKeyEvent`→IPC `PROCESS_KEY_EVENT`（u3-l2）→`DoEditSession`（u3-l3），把 commit 文字写进应用文档，同时若 `Status.ascii_mode`/`full_shape` 有变化，`_UpdateLanguageBar`（4.2）会刷新语言栏图标与 Conversion compartment（4.3）。

**输出要求**：把上述 10 步整理成一张「步骤 | 所在文件:行 | 关键变量/命令 | 作用」表格，并在最后画一张精简时序图（用户 → WeaselPanel → HandleUICallback → ClientImpl → 命名管道 → Server）。

> 说明：本综合实践为源码调用链追踪，结论基于真实源码逐行推导；其中涉及的实际运行行为（如「两次点击」交互）待在 Windows 上手动验证。

## 6. 本讲小结

- `CCandidateList` 一个对象同时扮演「TSF 候选 UI 元素」（实现 `ITfUIElement`/`ITfCandidateListUIElement`/`Behavior`/`Integratable`，把 `_ui->ctx().cinfo` 暴露给 TSF）与「自绘窗口宿主」（持有 `weasel::UI`）两个角色，由 `BeginUIElement`/`UpdateUIElement`/`EndUIElement` 管理生命周期。
- 鼠标交互经「四参数回调」`_UICallback(sel, hov, next, scroll_next)` 从 `WeaselPanel` 传到 `HandleUICallback`，再分三条分支翻译成 IPC：选中→`SELECT_CANDIDATE_ON_CURRENT_PAGE`+模拟 `VK_SELECT` 借道上屏；hover→`HIGHLIGHT`；翻页→`CHANGE_PAGE`/方向键。
- `CLangBarItemButton` 实现语言栏按钮：左键切中/英（发 `ENABLE/DISABLE_ASCII` 托盘命令 + 翻转 `ascii_mode` + 刷新图标），右键弹多语言菜单（菜单 id 经 `TrayCommand` 复用托盘命令），图标随 `ascii_mode` 与方案图标变化。
- `_UpdateLanguageBar` 把 Weasel 的中/英、全/半角状态翻译成 TSF 标准 `TF_CONVERSIONMODE_NATIVE`/`FULLSHAPE` 位写入 `GUID_COMPARTMENT_KEYBOARD_INPUTMODE_CONVERSION`，保证系统其它指示器一致。
- DisplayAttribute 用「GUID → `TfGuidAtom` → 贴到写作串 range 的 `GUID_PROP_ATTRIBUTE` 属性」三步，配合 `ITfDisplayAttributeProvider` 暴露的虚线下划线样式，在内联写作模式下让写作串带下划线。
- Compartment 是 TSF 的 GUID 键值仓库：`CCompartmentEventSink` 把回调包成 sink 订阅 `OPENCLOSE`/`CONVERSION` 两个键的变化，`_IsKeyboardOpen`/`_SetKeyboardOpen`/`_Get·SetCompartmentDWORD` 负责读写——这是 Weasel 与系统级输入法状态对接的通道。

## 7. 下一步学习建议

- **横向补全 TSF 前端**：本讲结束后，u3 单元（TSF 前端）的四讲（注册生命周期、按键捕获、编辑会话上屏、候选/语言栏/显示属性）已自成闭环。建议回头画一张「按键 → 上屏 → 候选交互 → 状态同步」的全景图，把 u3-l1~l4 串起来。
- **向下追 Server 侧**：本讲多次出现 `m_client.SelectCandidateOnCurrentPage`/`TrayCommand` 等 IPC 调用，它们到达 Server 后由 `RimeWithWeaselHandler` 处理。建议进入 **u4-l2（会话管理与按键处理）**，看 Server 侧如何把「选候选」翻译成 librime 的 `process_key`，以及 `_Respond` 如何把结果写回管道。
- **深入候选窗口绘制**：本讲的 `WeaselPanel::_UICallback`、`GetCandidateRect`、命中判定都依赖布局几何。想理解「候选矩形怎么算出来的」「窗口怎么画的」，进入 **u5 单元（候选窗口 UI 渲染）**，尤其是 u5-l1（WeaselPanel 窗口与交互）与 u5-l2（布局系统）。
- **托盘与菜单的另一面**：本讲语言栏菜单的 `TrayCommand` 与系统托盘菜单共用命令 id，Server 侧的菜单组装与派发在 **u6-l3（系统托盘、服务进程与自动更新）**，可对照阅读理解「同一命令 id 的两端」。
