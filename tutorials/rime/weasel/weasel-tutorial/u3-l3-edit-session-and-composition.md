# 编辑会话与上屏：EditSession 与 Composition

## 1. 本讲目标

本讲聚焦 WeaselTSF 前端 `weasel.dll` 的「上屏」环节——也就是按键被抓到、Server 也把候选/上屏文字算好送回来之后，前端怎么把这些文字真正写进用户正在用的那个应用（记事本、Word、浏览器输入框……）的文档里。读完本讲，你应当能够：

- 说清楚 TSF **EditSession（编辑会话）** 机制：为什么修改应用文档必须走 `RequestEditSession` + `DoEditSession`，`TfEditCookie`（编辑凭据）和读写锁（`TF_ES_READWRITE`）在其中扮演什么角色。
- 画出一次按键从 `_UpdateComposition` 触发 `DoEditSession`，到「读 Server 响应 → 提交上屏文字（commit） → 维护写作串状态（composing） → 内联写作（inline_preedit）」的完整主链路。
- 对比 **内联写作（inline_preedit）模式** 与 **独立候选窗口模式**：两者在 EditSession 被调用的时机、写入文档的内容、CUAS 兼容补丁上的差异。
- 解释 `_AbortComposition` / `_EndComposition` / `OnCompositionTerminated` / `OnEndEdit` 这一组「中止与清理」逻辑分别在什么时机被触发、各自做了什么。

本讲是 u3 单元的第三讲，承接 u3-l1（TSF 注册与生命周期）讲过的 `ActivateEx` 初始化链与 `ITfEditSession` 聚合，向下接住 u3-l2（KeyEventSink）里 `_ProcessKeyEvent` 之后的 `_UpdateComposition` 调用。本讲只覆盖**前端如何与 TSF 文档打交道**，Server 侧如何算出 `commit`/`preedit` 留给 u4 单元（RimeWithWeasel）。

## 2. 前置知识

阅读本讲前，建议你已经理解以下概念（u3-l1、u3-l2、u2 已铺垫）：

- **TSF 文档模型**：在 TSF 里，用户在应用里看到的「一段可编辑的文字」被抽象成一个 `ITfContext`（上下文）。输入法想往里写字，不能直接调应用的 API，而要通过 TSF 提供的统一接口（`ITfRange`、`ITfContextComposition` 等）。
- **写作串（Composition）**：IME 输入法独有的概念。用户敲 `nihao` 还没选字时，应用文档里那段「正在拼、还没定稿」的文字（可能显示为编码或预测字）就是 composition。它由 `ITfComposition` 表示，有明确的生命周期：开始（Start）→ 多次更新 → 结束（End）。结束时通常会把定稿文字「提交（commit）」进文档。
- **EditSession（编辑会话）**：TSF 规定，任何对文档的修改（写字、改选区、开/关写作串）都必须封装在一个实现了 `ITfEditSession` 的对象里，再交给 TSF 调度执行。执行时 TSF 会发一个 `TfEditCookie ec`（编辑凭据）作为「这次修改合法」的通行证，并提供读写锁保证并发安全。这是本讲的核心机制。
- **命名管道响应（u2-l5）**：Server 把算好的结果以「行协议」文本写回管道，前端用 `weasel::ResponseParser` 把它解析成 `commit`（待上屏文字）、`Context`（写作串 preedit、候选）、`Status`（中/英、是否正在 composing）、`Config`（`inline_preedit` 开关）。

> 小提示：如果对「`WeaselTSF` 类自己也是一个 `ITfEditSession`」这件事感到意外，那是本讲一个关键设计点，4.1 节会专门讲。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [WeaselTSF/EditSession.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/EditSession.cpp) | **本讲主战场之一**。`WeaselTSF::DoEditSession`——每个按键后跑一次的「主编辑会话」，负责读 Server 响应并决定如何上屏。 |
| [WeaselTSF/Composition.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp) | **本讲主战场之二**。一整套具体的 `CEditSession` 子类（开始/结束写作串、内联写作、插入文字、取光标位置）+ `_UpdateComposition`/`_AbortComposition` 等编排函数。 |
| [WeaselTSF/TextEditSink.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/TextEditSink.cpp) | `ITfTextEditSink`（`OnEndEdit`）与 `ITfTextLayoutSink`（`OnLayoutChange`）实现，以及 `_InitTextEditSink` 把这两个 sink 挂到文档上。负责「光标移出写作串就收尾」「布局变了就重定位候选窗」。 |
| [WeaselTSF/EditSession.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/EditSession.h) | `CEditSession` 抽象基类：所有「一次性编辑会话」子类的共同父类，提供 `_pTextService`/`_pContext` 与 COM 引用计数。 |
| [WeaselTSF/WeaselTSF.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.h) | 关键：`WeaselTSF` 自己也继承 `ITfEditSession`（第 19 行），声明 `DoEditSession`（第 88 行），并持有 `_pComposition`、`_pEditSessionContext`、`_committed`、`_async_edit` 等成员。 |
| [WeaselTSF/KeyEventSink.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp) | 每个 `OnXxxKey` 回调末尾都会调 `_UpdateComposition(pContext)`——这是触发本讲编辑会话的入口。 |
| [WeaselTSF/DisplayAttribute.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/DisplayAttribute.cpp) | `_SetCompositionDisplayAttributes` / `_ClearCompositionDisplayAttributes`：给写作串区域打上「下划线等」显示属性，配合内联写作模式使用。 |
| [include/WeaselIPCData.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h) | `Config` 结构体只有一个 `inline_preedit` 字段——它是本讲两种上屏模式的开关。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **EditSession 文档写入与锁**：TSF 编辑会话机制 + 主会话 `DoEditSession` 的全流程。
2. **Composition 的 preedit 与 commit**：开始/更新/结束写作串，内联写作与候选窗口两种模式的差异。
3. **中止与清理**：`_AbortComposition` / `OnEndEdit` / `OnCompositionTerminated` / `_InitTextEditSink` 的分工。

### 4.1 EditSession 文档写入与锁

#### 4.1.1 概念说明：为什么不能直接往应用里写字？

假设我们想要最简单的实现：用户按键 → Server 算出字 → 前端直接调一个「写字」API 把字塞进记事本。这为什么不行？

因为 TSF 是一个**多输入源、并发安全**的框架。同一个文档可能同时被「用户用键盘打字」「输入法插字」「应用自己撤销/重做」多方操作。如果输入法直接改文档，会和这些操作互相踩踏。所以 TSF 定了一条硬规矩：

> **任何对文档的修改，都必须在一个「编辑会话（Edit Session）」里进行。**

一个编辑会话是一个实现了 `ITfEditSession` 接口的 COM 对象，它只有一个核心方法：

```cpp
HRESULT DoEditSession(TfEditCookie ec);
```

- 你**把要做的修改写进 `DoEditSession` 里**（怎么改 `ITfRange`、怎么开写作串……）。
- 然后**把对象交给 TSF**：`ITfContext::RequestEditSession(...)`。
- TSF 在合适的时机（拿到文档锁之后）回调你的 `DoEditSession`，并传入一个 `TfEditCookie ec`。
- `ec` 是「本次修改合法」的**编辑凭据**：你调 `ITfRange::SetText(ec, ...)` 等修改类 API 时，必须带上它，证明「我是被 TSF 授权、在锁保护下修改文档的」，否则调用失败。这相当于一把钥匙。

`RequestEditSession` 的第三个参数是**锁标志**，决定了这个会话需要的访问级别：

- `TF_ES_READ`：只读，不改文档（例如只是读光标位置）。
- `TF_ES_READWRITE`：要读也要写，会请求文档的**读写锁**。TSF 保证在 `DoEditSession` 执行期间文档不会被别人改，你改完安全释放锁。

Weasel 几乎所有写文档的会话都用 `TF_ES_ASYNCDONTCARE | TF_ES_READWRITE`：

- `TF_ES_READWRITE`：要改文档。
- `TF_ES_ASYNCDONTCARE`：不关心是同步还是异步执行（TSF 自己决定；可能立即回调，也可能稍后）。

#### 4.1.2 核心流程：两套 EditSession

WeaselTSF 里其实有**两套** EditSession，初学时容易混淆，先把它们分清楚：

**第一套：WeaselTSF 自己就是 ITfEditSession（主编辑会话）**

看 [WeaselTSF/WeaselTSF.h:19](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.h#L19) —— `WeaselTSF` 的继承列表里赫然有 `public ITfEditSession`；并在 [WeaselTSF/WeaselTSF.h:88](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.h#L88) 声明了 `STDMETHODIMP DoEditSession(TfEditCookie ec);`。也就是说，**输入法主对象本身同时充当一个编辑会话**。

触发它在 [WeaselTSF/Composition.cpp:384-393](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L384-L393)：

```cpp
void WeaselTSF::_UpdateComposition(com_ptr<ITfContext> pContext) {
  HRESULT hr;
  _pEditSessionContext = pContext;          // 记住是哪个文档
  _pEditSessionContext->RequestEditSession(
      _tfClientId, this, TF_ES_ASYNCDONTCARE | TF_ES_READWRITE, &hr);
  _async_edit = !!(hr == TF_S_ASYNC);       // 记录是否异步
  _UpdateCompositionWindow(pContext);
}
```

注意 `RequestEditSession` 的第二个参数是 `this`——把自己这个 `ITfEditSession` 交上去。`_UpdateComposition` 又是谁调的？是 u3-l2 讲过的每个按键回调末尾（例如 [WeaselTSF/KeyEventSink.cpp:96](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L96)）。所以**每个按键 → `_UpdateComposition` → 主编辑会话 `DoEditSession`**，这是上屏的总入口。

`_async_edit`（[WeaselTSF.h:233](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.h#L233)）记录这次会话是不是被异步排队的（`RequestEditSession` 返回 `TF_S_ASYNC` 表示稍后才执行）。这个标记会影响 `OnKeyUp` 是否需要再补一次 `_UpdateComposition`（见 [KeyEventSink.cpp:143-144](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L143-L144)），用于规避某些按键时序问题。

**第二套：一次性的 CEditSession 子类（专项编辑会话）**

对于「开始写作串」「结束写作串」「插入文字」「取光标坐标」「写内联 preedit」这些**单一职责**的操作，Weasel 定义了一组小类，都继承自 [WeaselTSF/EditSession.h:6-51](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/EditSession.h#L6-L51) 的 `CEditSession`：

```cpp
class CEditSession : public ITfEditSession {
  // 持有 _pTextService（回指 WeaselTSF）和 _pContext（目标文档）
  // 自己管 COM 引用计数
  virtual STDMETHODIMP DoEditSession(TfEditCookie ec) = 0;  // 纯虚，子类实现
};
```

这些子类（`CStartCompositionEditSession`、`CEndCompositionEditSession`、`CInlinePreeditEditSession`、`CInsertTextEditSession`、`CGetTextExtentEditSession`）各自把一件小事写进自己的 `DoEditSession`，由对应的 `WeaselTSF::_Xxx` 方法 new 出来并 `RequestEditSession` 提交。它们在 4.2 节逐一精读。

> 设计要点：主编辑会话用「对象本身实现接口」省去每次 new；专项会话用「小对象承载单一变更」便于复用与隔离失败。两者都受 TSF 的 `TfEditCookie` 锁机制约束。

#### 4.1.3 源码精读：主编辑会话 DoEditSession

[WeaselTSF/EditSession.cpp:6-47](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/EditSession.cpp#L6-L47) 是本讲最核心的函数——**每次按键后，TSF 在读写锁保护下回调它**。它的职责是把 Server 算好的结果应用上去：

```cpp
STDAPI WeaselTSF::DoEditSession(TfEditCookie ec) {
  std::wstring commit;                                  // 待上屏文字
  weasel::Config config;                                // 含 inline_preedit 开关
  auto context = std::make_shared<weasel::Context>();   // 写作串 + 候选
  weasel::ResponseParser parser(&commit, context.get(), &_status, &config,
                                &_cand->style());

  bool ok = m_client.GetResponseData(std::ref(parser)); // 读管道响应并解析
  _UpdateLanguageBar(_status);

  if (ok) {
    if (!commit.empty()) {                  // ① 有上屏文字 → 提交
      if (!_IsComposing())
        _StartComposition(_pEditSessionContext,
                          _fCUASWorkaroundEnabled && !config.inline_preedit);
      _InsertText(_pEditSessionContext, commit);
      _EndComposition(_pEditSessionContext, false);
      _committed = TRUE;
    } else {
      _committed = FALSE;
    }
    if (_status.composing && !_IsComposing())        // ② 该写作却没开 → 开
      _StartComposition(_pEditSessionContext,
                        _fCUASWorkaroundEnabled && !config.inline_preedit);
    else if (!_status.composing && _IsComposing())   // ③ 不该写作却开着 → 关
      _EndComposition(_pEditSessionContext, true);
    if (_IsComposing() && config.inline_preedit)     // ④ 内联模式 → 写 preedit
      _ShowInlinePreedit(_pEditSessionContext, context);
    _UpdateCompositionWindow(_pEditSessionContext);  // ⑤ 重定位候选窗
  }

  _UpdateUI(*context, _status);              // 最后刷新 UI（候选窗显示/隐藏）
  return TRUE;
}
```

逐段对照：

- **第 11-14 行**：构造 `ResponseParser`（u2-l5 讲过），把管道返回的文本解析进 `commit`/`context`/`_status`/`config`。`m_client.GetResponseData` 阻塞读管道正文并喂给 parser。
- **第 18-31 行（①提交上屏）**：如果 Server 给了 `commit`（定稿文字），就「开写作串 → 插文字 → 关写作串」三连，把字真正写进文档。`_committed = TRUE` 记一笔，给 u3-l2 里 Caps Lock 模拟击键逻辑用。
- **第 32-37 行（②③写作串状态对齐）**：`_status.composing` 是 Server 告知的「引擎现在到底在不在写作」，`_IsComposing()` 是前端「TSF 写作串到底开没开」。两者可能不一致（比如第一键、或刚 commit 完），这里强制对齐：该开就开，该关就关。
- **第 38-40 行（④内联写作）**：仅当 `inline_preedit` 为真、且正在写作时，把 preedit 写进文档（详见 4.2.3）。
- **第 41 行（⑤重定位）**：每次都更新候选窗贴在光标附近的位置。

注意 `_StartComposition` / `_InsertText` / `_EndComposition` / `_ShowInlinePreedit` 这些**不是直接改文档**，而是 new 出对应的 `CEditSession` 子类再 `RequestEditSession`（嵌套会话）。也就是说，主编辑会话内部又「派发」了一批专项会话。

#### 4.1.4 代码实践：阅读型——追踪一个会话的两层嵌套

这是一个**源码阅读型实践**（无法在此环境真正运行 Windows IME，标注「待本地验证」的部分请有 Windows 环境的读者验证）。

1. **实践目标**：搞清楚「主编辑会话 `DoEditSession`」和「专项会话」是两层 `RequestEditSession`，理解嵌套关系。
2. **操作步骤**：
   - 打开 [WeaselTSF/Composition.cpp:384](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L384)，确认 `_UpdateComposition` 用 `this` 提交了**第一层**会话。
   - 打开 [WeaselTSF/EditSession.cpp:6](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/EditSession.cpp#L6)，在 `DoEditSession` 里找到对 `_StartComposition`、`_InsertText` 的调用。
   - 跳到 [WeaselTSF/Composition.cpp:73](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L73)（`_StartComposition`）和 [WeaselTSF/Composition.cpp:369](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L369)（`_InsertText`），确认它们各 new 了一个 `CEditSession` 子类并 `RequestEditSession`——这是**第二层**。
3. **需要观察的现象**：第一层会话（`WeaselTSF::DoEditSession`）持有读写锁；它在锁内再派发的第二层会话（如 `CInsertTextEditSession`）能否拿到锁？答案是能——因为它们在**同一线程、同一锁**上下文里被同步调度（`TF_ES_ASYNCDONTCARE` 在持锁时通常同步执行）。
4. **预期结果**：在纸上画出两层调用栈——`OnKeyDown → _UpdateComposition →[RequestEditSession]→ WeaselTSF::DoEditSession → _InsertText →[RequestEditSession]→ CInsertTextEditSession::DoEditSession → SetText`。
5. 待本地验证：在 `CInsertTextEditSession::DoEditSession` 的 `SetText` 处下断点，观察调用栈是否真的出现两层 `DoEditSession`。

#### 4.1.5 小练习与答案

**练习 1**：`RequestEditSession` 的最后一个参数 `TF_ES_READWRITE` 改成 `TF_ES_READ` 会怎样？以 `_InsertText` 为例说明。

**参考答案**：`_InsertText` 的 `CInsertTextEditSession::DoEditSession` 里要调 `pRange->SetText(ec, ...)` 修改文档，这需要写锁。如果只请求 `TF_ES_READ`，TSF 不会授予写权限，`SetText` 会失败、文字写不进去、上屏失效。所以凡是改文档的会话必须 `TF_ES_READWRITE`。

**练习 2**：`TfEditCookie ec` 在 `DoEditSession` 之外（比如普通成员函数里）还有效吗？

**参考答案**：无效。`ec` 只在 TSF 回调 `DoEditSession` 期间、持锁期间有效，相当于「本次会话的通行证」。Weasel 的做法是把需要 `ec` 的操作都写进某个 `CEditSession::DoEditSession` 里，而不是把它存起来在别处复用。

---

### 4.2 Composition 的 preedit 与 commit

#### 4.2.1 概念说明：写作串的两种显示模式

写作串（composition）在用户眼里是什么样，取决于 `inline_preedit` 这个开关（[WeaselIPCData.h:189-193](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L189-L193) 的 `Config::inline_preedit`）：

- **独立候选窗口模式（`inline_preedit = false`，默认）**：写作串（编码 `nihao`、预测字）**不**写进应用文档，而是显示在一个**独立的浮层候选窗**里（由 WeaselUI 的 `WeaselPanel` 绘制）。应用文档这时是「干净」的，输入法只是悄悄维护一个**不可见/占位**的 TSF 写作串，用于定位光标和接收 TSF 事件。
- **内联写作模式（`inline_preedit = true`）**：写作串**直接写进应用文档**（记事本里会真的看到 `nihao` 或预测字出现），候选窗只显示候选页。这种模式下用户看到的「正在拼的字」是应用文档的一部分。

两种模式各有取舍：独立窗口模式兼容性最好（不污染文档、不受应用排版影响），但写作串浮在文档外；内联模式所见即所得，但某些应用（尤其老 CUAS 应用）对内联写作串的坐标计算有问题。

无论哪种模式，TSF 写作串的「开 → 改 → 关」三步骨架是一样的，区别只在于：
- **开（Start）** 时是否需要塞一个「占位空格」（CUAS 兼容补丁）。
- **改（Update）** 时写入文档的内容是「空/占位」还是「真实 preedit」。
- **提交（commit）** 时都一样：把定稿文字插进文档。

#### 4.2.2 核心流程：Start / Update / End / Commit

把四个专项会话串起来看一次「`ni` → 选『你』上屏」的完整写作串生命周期（独立窗口模式）：

```text
按键 n  →  DoEditSession:
            _status.composing=true 但还没开写作串
            → _StartComposition (CStartCompositionEditSession)
              · StartComposition 建 ITfComposition
              · CUAS 补丁：塞一个占位空格 " " 到写作串范围
              · Collapse 选区到开头
            → (inline_preedit=false，不写 preedit)
            → _UpdateCompositionWindow 定位候选窗
按键 i  →  DoEditSession:
            _IsComposing()==true，_status.composing==true → 状态对齐，不重开
            → 候选窗内容由 _UpdateUI 刷新（Server 算的 preedit/候选）
选『你』 →  DoEditSession:
            commit="你" 非空
            → _InsertText (CInsertTextEditSession) 把 "你" 写进文档
            → _EndComposition(clear=false) 关写作串（commit 已经写了，不用再清）
            → _status.composing=false, _IsComposing()==true → 第③分支关掉写作串
```

注意第 ④ 步「选『你』」时，commit 非空走的是 `DoEditSession` 第 19-28 行的「开→插→关」三连：这里先 `_StartComposition`（如果之前已关）、`_InsertText` 写 `你`、`_EndComposition(false)` 关掉。`clear=false` 是因为要保留刚写的 `你`，不要清空。

#### 4.2.3 源码精读：开始、内联、插入、结束

**(a) 开始写作串——`CStartCompositionEditSession` 与 CUAS 补丁**

[WeaselTSF/Composition.cpp:27-71](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L27-L71)。关键两步：先用 `ITfInsertAtSelection::InsertTextAtSelection(TF_IAS_QUERYONLY)` 查到光标处的选区范围，再 `ITfContextComposition::StartComposition` 真正开写作串，把得到的 `ITfComposition` 经 `_SetComposition` 存进 `_pComposition`。

最值得关注的是 [Composition.cpp:48-56](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L48-L56) 的 **CUAS 补丁**：

```cpp
/* CUAS 在写作串为空时 GetTextExt() 返回错误坐标，
   所以塞一个空格占位。仅 inline_preedit 关闭时需要。*/
if (!_inlinePreeditEnabled) {
  pRangeComposition->SetText(ec, TF_ST_CORRECTION, L" ", 1);
}
```

CUAS（Cicero Unmanaged Application Support）是 TSF 为老式 IMM 应用准备的兼容层。它在写作串为空时算不出光标坐标，导致候选窗定位错乱。Weasel 的 workaround 是在**独立窗口模式**下往写作串里塞一个空格占位（内联模式因为本身就有 preedit 文字，不需要）。这正是 `DoEditSession` 里 `_StartComposition` 第二个参数写作 `_fCUASWorkaroundEnabled && !config.inline_preedit` 的原因（[EditSession.cpp:24](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/EditSession.cpp#L24)）——只有「需要补丁 且 非内联」时才启用占位。

之后 [Composition.cpp:59-67](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L59-L67) 设置选区：内联模式塌缩到末尾（`TF_ANCHOR_END`，因为 preedit 会接在后面写），非内联塌缩到开头（`TF_ANCHOR_START`）。

**(b) 内联写作——`CInlinePreeditEditSession`**

[WeaselTSF/Composition.cpp:268-308](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L268-L308)。把 Server 算的 `context->preedit.str`（写作串文本）直接 `SetText` 进写作串范围：

```cpp
std::wstring preedit = _context->preedit.str;
_pComposition->GetRange(&pRangeComposition);
pRangeComposition->SetText(ec, 0, preedit.c_str(), preedit.length());
// 找高亮属性，定位光标 sel_cursor
_pTextService->_SetCompositionDisplayAttributes(ec, _pContext, pRangeComposition);
// 把选区塌缩到高亮光标处
```

注意它还会调 `_SetCompositionDisplayAttributes`（[DisplayAttribute.cpp:24-53](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/DisplayAttribute.cpp#L24-L53)）给写作串打显示属性（如下划线），并尝试根据 preedit 里的 `HIGHLIGHTED` 属性把光标放到高亮段。内联模式因为文字真在文档里，这些显示属性用户能直接看到。

**(c) 插入上屏文字——`CInsertTextEditSession`**

[WeaselTSF/Composition.cpp:343-367](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L343-L367)。commit 的真身：拿到写作串范围，`SetText` 写入定稿文字，再把选区塌缩到末尾（光标跟在刚上屏的字后面）。

**(d) 结束写作串——`CEndCompositionEditSession`**

[WeaselTSF/Composition.cpp:105-123](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L105-L123)：

```cpp
_pTextService->_ClearCompositionDisplayAttributes(ec, _pContext);  // 清显示属性
if (_clear && _pComposition->GetRange(&pRangeComposition) == S_OK)
  pRangeComposition->SetText(ec, 0, L"", 0);                       // 可选：清空占位
_pComposition->EndComposition(ec);                                 // 关写作串
_pTextService->_FinalizeComposition();                             // 置 _pComposition=nullptr
```

`_clear` 参数控制要不要把写作串范围清空：commit 上屏时传 `false`（保留刚写的字），其它收尾场景传 `true`（清掉占位空格）。`_FinalizeComposition`（[Composition.cpp:415-417](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L415-L417)）只是把 `_pComposition` 置空，于是 `_IsComposing()`（[Composition.cpp:423-425](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L423-L425)，判 `_pComposition != NULL`）变假。

#### 4.2.4 代码实践：两种模式差异对照表（本讲指定实践任务）

这是本讲的核心实践，目标正是规格里要求的「对比内联 vs 候选窗口两种模式」。

1. **实践目标**：用一张表说清 `inline_preedit` 开关如何改变 EditSession 的调用与文档写入内容。
2. **操作步骤**：
   - 在 [WeaselTSF/EditSession.cpp:22-40](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/EditSession.cpp#L22-L40) 圈出受 `config.inline_preedit` 影响的三处：`_StartComposition` 的第二参数、是否进第④分支调 `_ShowInlinePreedit`。
   - 在 [WeaselTSF/Composition.cpp:54-63](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L54-L63) 圈出 CUAS 占位与选区塌缩方向的差异。
   - 填写下面对照表。
3. **需要观察的现象 / 预期结果**：对照表如下（这是实践产物）：

| 维度 | 独立候选窗口模式（`inline_preedit=false`） | 内联写作模式（`inline_preedit=true`） |
| --- | --- | --- |
| 主编辑会话 `DoEditSession` 是否调用 | 调用（每个按键都调） | 调用（每个按键都调） |
| `_StartComposition` 第二参数（CUAS 占位） | `_fCUASWorkaroundEnabled && true` → 可能塞占位空格 `" "` | `... && false` → **不**塞占位 |
| 写作串里的实际内容 | 一个占位空格（不可见的占位） | 真实的 preedit 文本（`nihao`/预测字） |
| 调 `_ShowInlinePreedit` 吗（第④分支） | **不调**（`config.inline_preedit` 为假） | 调（`CInlinePreeditEditSession` 把 preedit 写进文档） |
| 写作串显示属性（下划线等） | 一般不打（占位不可见） | 打（`_SetCompositionDisplayAttributes`） |
| preedit 给谁看 | WeaselPanel 浮层候选窗（经 `_UpdateUI`） | 应用文档本身（外加候选窗显示候选页） |
| 选区塌缩方向（Start 时） | `TF_ANCHOR_START`（开头） | `TF_ANCHOR_END`（末尾） |
| commit 上屏流程 | 相同：`_InsertText` 写字 | 相同：`_InsertText` 写字 |

4. **结论**：开关 `inline_preedit` 不改变「开→改→关」骨架，只改变三件事——是否塞 CUAS 占位、是否把 preedit 写进文档、选区塌缩方向。`commit` 上屏在两种模式下完全一致。
5. 待本地验证：在 `weasel.custom.yaml` 里切换 `style/inline_preedit`，分别观察记事本里写作时是否出现可见文字。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CUAS 占位只在 `inline_preedit=false` 时塞？

**参考答案**：CUAS 在写作串为空时算不出光标坐标。内联模式下写作串里始终有 preedit 文字（非空），CUAS 能正常算坐标，不需要占位；独立窗口模式下写作串「本来该是空的」（preedit 显示在浮层），所以必须塞个空格骗过 CUAS。源码注释 [Composition.cpp:48-53](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L48-L53) 明确了这一点。

**练习 2**：`DoEditSession` 里 commit 非空时为什么要 `_StartComposition`→`_InsertText`→`_EndComposition` 三步，而不是直接 `_InsertText`？

**参考答案**：因为 TSF 要求写文档必须在写作串上下文里。如果当前没在写作（`!_IsComposing()`），`CInsertTextEditSession` 的 `_pComposition` 为空会直接 `return E_FAIL`（[Composition.cpp:348-349](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L348-L349)）。所以必须先开写作串，再插字，最后关掉。注释 [EditSession.cpp:20-21](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/EditSession.cpp#L20-L21) 也说这是为「auto-selecting 时 commit 和 preedit 同时存在」的情况设计的。

---

### 4.3 中止与清理

#### 4.3.1 概念说明：写作串会被哪些事件打断？

写作串不会总是「开→改→commit→关」这么顺。它会被多种事件**意外打断**，前端必须保证任何情况下都能干净收尾，否则会留下「幽灵写作串」（文档里卡着一个下划线区域、光标行为异常）。主要打断源：

- **焦点离开**（u3-l2 的 `OnSetFocus(!fForeground)`）：用户切走了，立即 `_AbortComposition`。
- **光标移出写作串范围**（`OnEndEdit`）：用户用鼠标或方向键把光标移出了写作串，TSF 通过 `ITfTextEditSink::OnEndEdit` 通知，前端应结束写作。
- **写作串被清空**（`OnCompositionTerminated`）：当某次编辑会话让写作串变空，TSF 会回调 `ITfCompositionSink::OnCompositionTerminated`（即便是正常结束也会调，源码注释吐槽了这点）。
- **应用主动改文档**：也可能触发 `OnEndEdit`。
- **布局变化**（`OnLayoutChange`）：文档重新排版了（滚动、窗口大小变），候选窗要跟着重定位，但写作串本身不一定要结束。

这些都靠两个 sink 接收：`ITfTextEditSink`（文本/选区变化）和 `ITfTextLayoutSink`（布局变化）。

#### 4.3.2 核心流程：sink 注册 + 三种收尾路径

**注册 sink**：[WeaselTSF/TextEditSink.cpp:73-118](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/TextEditSink.cpp#L73-L118) 的 `_InitTextEditSink` 在文档获得焦点时，对顶层 `ITfContext` 同时 `AdviseSink` 挂上 `ITfTextEditSink` 和 `ITfTextLayoutSink`，记下 cookie。换文档时先 `UnadviseSink` 旧的。

**三条收尾路径**汇到 `_EndComposition` / `_AbortComposition`：

```text
路径 A（焦点离开）:  OnSetFocus(false) ──> _AbortComposition
路径 B（光标移出）:  OnEndEdit ─┬─ 选区变化且不在写作串内 ─> _EndComposition(clear=true)
路径 C（写作串被清空/正常结束）: OnCompositionTerminated ─> _AbortComposition
```

`_AbortComposition`（[Composition.cpp:406-413](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L406-L413)）是最彻底的清理：

```cpp
void WeaselTSF::_AbortComposition(bool clear) {
  m_client.ClearComposition();          // 通知 Server 清掉它那边的写作状态
  if (_IsComposing())
    _EndComposition(_pEditSessionContext, clear);  // 关 TSF 写作串
  _committed = TRUE;
  _cand->Destroy();                     // 销毁候选窗
}
```

它比 `_EndComposition` 多了两件大事：通知 Server 端的 librime 也清状态（`m_client.ClearComposition()` 走 IPC），以及销毁前端候选窗 `_cand->Destroy()`。也就是说 `_AbortComposition` 是「前后端一起清」，`_EndComposition` 只是「前端 TSF 写作串收尾」。

#### 4.3.3 源码精读：OnEndEdit 与 OnCompositionTerminated

**`OnEndEdit`——光标移出即收尾**

[WeaselTSF/TextEditSink.cpp:20-57](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/TextEditSink.cpp#L20-L57)。每次文档编辑结束 TSF 都会调它，参数 `ecReadOnly` 是**只读**凭据（这里只能看不能改）：

```cpp
if (选区变了 && 正在写作) {
  取当前选区 range;
  取写作串范围 pRangeComposition;
  if (!IsRangeCovered(ecReadOnly, 选区range, 写作串范围))  // 选区不在写作串内
    _EndComposition(pContext, true);                      // 结束并清空
}
```

辅助函数 `IsRangeCovered`（[TextEditSink.cpp:4-18](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/TextEditSink.cpp#L4-L18)）用 `CompareStart`/`CompareEnd` 判断「选区是否被写作串范围完全覆盖」。若用户把光标点到了写作串外面，说明要中断输入，于是干净地结束写作串。

注意它故意**不**在 `OnEndEdit` 里响应「文本变化」（[TextEditSink.cpp:49-55](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/TextEditSink.cpp#L49-L55) 只取了变化范围就 Release 掉，没做事）——文本变化由 Weasel 自己的编辑会话驱动，不需要在这里再处理，避免循环。

**`OnCompositionTerminated`——吐槽式注释与幂等清理**

[WeaselTSF/Composition.cpp:396-404](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L396-L404)：

```cpp
STDAPI WeaselTSF::OnCompositionTerminated(TfEditCookie ecWrite,
                                          ITfComposition* pComposition) {
  // NOTE: 即使是正常关闭，只要写作串空了 TSF 也会调这个。Silly M$.
  _AbortComposition();
  return S_OK;
}
```

注释点出了一个 TSF 的「坑」：这个回调不仅在异常终止时调，**正常结束且写作串为空时也会调**。所以 `_AbortComposition` 必须是**幂等**的——重复调不能出错。看它的实现：`m_client.ClearComposition()` 是发 IPC（Server 幂等）、`if (_IsComposing())` 守卫保证没开写作串时不重复关、`_cand->Destroy()` 也是幂等销毁。整条路径对重复调用安全。

**`OnLayoutChange`——布局变了就重定位**

[WeaselTSF/TextEditSink.cpp:59-71](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/TextEditSink.cpp#L59-L71)：文档布局变化（`TF_LC_CHANGE`）时，只要还在写作、且是当前关注的文档，就 `_UpdateCompositionWindow` 让候选窗跟着光标重新定位。它不动写作串本身。

#### 4.3.4 代码实践：阅读型——画出三条收尾路径

1. **实践目标**：验证三种打断源最终都汇到安全的收尾，且都幂等。
2. **操作步骤**：
   - 路径 A：从 [KeyEventSink.cpp:65-74](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L65-L74) 的 `OnSetFocus(false)` → `_AbortComposition` → [Composition.cpp:406](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L406)。
   - 路径 B：从 [TextEditSink.cpp:20](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/TextEditSink.cpp#L20) 的 `OnEndEdit` → `_EndComposition(pContext, true)`。
   - 路径 C：从 [Composition.cpp:396](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L396) 的 `OnCompositionTerminated` → `_AbortComposition`。
3. **需要观察的现象**：三条路径里，路径 A 和 C 用 `_AbortComposition`（前后端都清 + 毁候选窗），路径 B 只用 `_EndComposition`（仅前端 TSF 收尾，不通知 Server、不毁窗）。想一想为什么 B 不毁候选窗？
4. **预期结果**：因为 B（光标移出）发生在一次编辑会话内，紧接着还会有别的会话处理 UI；而且 `_EndComposition` 里已经 `_cand->EndUI()`（[Composition.cpp:129](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L129)）隐藏了候选窗，不需要彻底 Destroy。而 A/C 是「整个输入被打断/结束」，需要彻底清理。
5. 待本地验证：在写作中途用鼠标点击写作串外的位置，观察候选窗是否消失（路径 B 生效）。

#### 4.3.5 小练习与答案

**练习 1**：`_AbortComposition` 为什么要先 `m_client.ClearComposition()` 再 `_EndComposition`？顺序反过来会怎样？

**参考答案**：`m_client.ClearComposition()` 通知 Server（librime）清掉引擎侧的写作状态，避免下次按键时 Server 还以为在写作、给出残留的 preedit。顺序反过来，前端先关了 TSF 写作串，理论上也能工作，但若 `ClearComposition` 的 IPC 失败，Server 状态没清干净，下次按键 Server 可能仍返回非空 preedit，而前端写作串已关，会出现「状态不一致」。先清 Server 是更安全的顺序（即便前端失败，Server 也已干净）。

**练习 2**：`OnEndEdit` 收到的 `ecReadOnly` 是只读凭据，但它调的 `_EndComposition` 内部会 new 一个写会话——这矛盾吗？

**参考答案**：不矛盾。`OnEndEdit` 里用 `ecReadOnly` 只是**判断**（读选区、读写作串范围），真正改文档（清空、关写作串）的工作交给 `_EndComposition` → `CEndCompositionEditSession`，由后者通过 `RequestEditSession(... TF_ES_READWRITE ...)` 重新申请写锁。这正是 EditSession 机制的意义：把「判断」和「改」分开，写操作一律走带写锁的会话。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**端到端追踪任务**：追踪「用户在记事本里输入 `ni`，然后用方向键把光标左移出写作串」的全过程，标注每一步涉及的 EditSession 与 sink。

要求产出一张时序图（文字版即可），至少包含以下节点，并标出对应源码位置：

1. `OnTestKeyDown('n')` → `_ProcessKeyEvent` → `m_client.ProcessKeyEvent`（u3-l2）→ `_UpdateComposition` → 主编辑会话 `WeaselTSF::DoEditSession`（[EditSession.cpp:6](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/EditSession.cpp#L6)）。
2. 在 `DoEditSession` 内：`_status.composing=true` 且 `!_IsComposing()` → `_StartComposition`（[Composition.cpp:73](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L73)）→ `CStartCompositionEditSession::DoEditSession`（开写作串 + CUAS 占位）。
3. `_UpdateCompositionWindow`（[Composition.cpp:215](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L215)）→ `CGetTextExtentEditSession` 取光标坐标 → `_SetCompositionPosition` → `m_client.UpdateInputPosition`（告诉 Server 候选窗该贴哪）。
4. `_UpdateUI`（[EditSession.cpp:44](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/EditSession.cpp#L44)）刷新候选窗显示 `ni` 的候选。
5. 用户按左方向键把光标移出写作串 → 应用改了选区 → TSF 回调 `OnEndEdit`（[TextEditSink.cpp:20](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/TextEditSink.cpp#L20)）→ `IsRangeCovered` 判定选区已不在写作串内 → `_EndComposition(pContext, true)`（[Composition.cpp:125](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L125)）→ `CEndCompositionEditSession` 清占位 + 关写作串 + `_FinalizeComposition`。

进阶：把这张图里每个箭头标注是「同步调用」「`RequestEditSession` 派发的新会话」还是「TSF 回调 sink」，借此检验你是否真的分清了 4.1 讲的两套 EditSession 与 4.3 讲的 sink。待本地验证：在 Windows 上 attach 到记事本进程的 `weasel.dll`，在 `WeaselTSF::DoEditSession` 和 `OnEndEdit` 下断，复现上述时序。

## 6. 本讲小结

- TSF 规定改文档必须在 **EditSession** 里进行：`RequestEditSession` 提交一个 `ITfEditSession` 对象，TSF 回调 `DoEditSession(TfEditCookie ec)`，`ec` 是持锁修改文档的「编辑凭据」；写操作用 `TF_ES_READWRITE`。
- Weasel 有**两套** EditSession：`WeaselTSF` 自己实现 `ITfEditSession` 作为「每个按键的主会话」（`DoEditSession` 读 Server 响应并编排上屏），以及一组继承 `CEditSession` 的「专项会话」（开始/结束写作串、插字、内联写作、取坐标）。
- 主会话 `DoEditSession` 的四步：① 有 `commit` 就「开→插→关」上屏；②③ 用 `_status.composing` 与 `_IsComposing()` 对齐写作串开关；④ `inline_preedit` 为真时把 preedit 写进文档；⑤ 重定位候选窗；最后 `_UpdateUI` 刷新。
- **内联模式 vs 候选窗口模式**的差异只在三处：是否塞 CUAS 占位空格、是否把 preedit 写进文档（`CInlinePreeditEditSession`）、选区塌缩方向；commit 上屏流程两者一致。
- CUAS 补丁：老式 IMM 应用在写作串为空时算不出光标坐标，故非内联模式下往写作串塞一个空格占位（[Composition.cpp:48-56](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Composition.cpp#L48-L56)）。
- 中止与清理有三条路径：焦点离开与写作串被清空走 `_AbortComposition`（前后端都清 + 毁候选窗，幂等），光标移出写作串走 `OnEndEdit` → `_EndComposition`（仅前端收尾）；`ITfTextEditSink`/`ITfTextLayoutSink` 由 `_InitTextEditSink` 挂到文档。

## 7. 下一步学习建议

本讲把「前端如何把字写进应用文档」讲完了，但 `DoEditSession` 里读到的 `commit`/`context`/`status`/`config` 是**谁算出来的**还没展开——那是 WeaselServer 进程里的 `RimeWithWeaselHandler`。建议：

- 进入 **u4 单元（Rime 引擎桥接 RimeWithWeasel）**，从 u4-l1「Handler 与引擎初始化」开始，看 `RequestHandler::ProcessKeyEvent` 如何调用 librime 的 `rime_api->process_key`，以及 `_Respond` 如何把结果拼成 u2-l5 讲的行协议文本回传。
- 如果想先把 TSF 前端的其余部分补全，可以读 **u3-l4（候选列表、语言栏与显示属性）**：本讲多次出现的 `_cand`（`CCandidateList`）和 `_UpdateUI`/`_SetCompositionDisplayAttributes` 在那里有完整讲解。
- 进阶可关注 [WeaselTSF/Compartment.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp) 与 u3-l2 提到的 `_isToOpenClose`，理解键盘开关如何与 `_AbortComposition` 配合决定按键放行。
