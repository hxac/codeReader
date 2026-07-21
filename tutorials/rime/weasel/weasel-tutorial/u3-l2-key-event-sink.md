# 按键事件捕获：KeyEventSink

## 1. 本讲目标

本讲聚焦 WeaselTSF（前端 `weasel.dll`，住在应用进程里）「抓键」这一环节。读完本讲，你应当能够：

- 说清楚 `OnTestKeyDown` / `OnKeyDown`（以及 `OnTestKeyUp` / `OnKeyUp`）这两对回调如何用 `_fTestKeyDownPending` / `_fTestKeyUpPending` 配合，把「同一个按键」只送给后台 Server 一次。
- 画出一次按键从 Windows 虚拟键（`WPARAM`/`LPARAM`）经 `ConvertKeyEvent` 转成 `weasel::KeyEvent`，再经 `m_client.ProcessKeyEvent` 走命名管道发往 Server 的完整路径。
- 解释键盘被关闭/禁用、或连不上 Server 时为什么按键要「放行」（`*pfEaten = FALSE`），以及 Caps Lock 这种特殊键为什么要额外模拟两次击键。

本讲是 u3 单元的第二讲，承接 u3-l1（TSF 注册与生命周期）讲过的 `_InitKeyEventSink`，向下打通到 u2 单元讲过的 IPC `WEASEL_IPC_PROCESS_KEY_EVENT` 命令。

## 2. 前置知识

阅读本讲前，建议你已经理解以下概念（u3-l1、u2-l1 已铺垫）：

- **TSF 文本服务（Text Services Framework）**：Windows 提供的输入法框架。系统在应用进程里加载 `weasel.dll`，每当用户在输入框里按键，系统会回调输入法实现的 `ITfKeyEventSink` 接口方法。
- **KeyEventSink（按键事件接收器）**：`ITfKeyEventSink` 的几个核心回调——`OnTestKeyDown`、`OnKeyDown`、`OnTestKeyUp`、`OnKeyUp`。系统把按键交给输入法「先问要不要吃」（Test），再「正式通知」（正式）。
- **`pfEaten`（按键是否被「吃掉」）**：每个回调都带一个 `BOOL* pfEaten` 出参。`*pfEaten = TRUE` 表示输入法要拦截这个键（不传递给应用），`FALSE` 表示放行（让应用自己处理）。
- **命名管道 IPC**：WeaselTSF 把按键转成 `weasel::KeyEvent` 后，通过 `m_client.ProcessKeyEvent` 经命名管道发给全局唯一的 WeaselServer，由其调用 librime 算字。命令号是 `WEASEL_IPC_PROCESS_KEY_EVENT`（见 u2-l1）。

如果你对 TSF 把 sink 「挂上去」的过程还不清楚，可以先回到 u3-l1 的 `ActivateEx` 初始化链看 `_InitKeyEventSink`；如果对 IPC 命令协议不熟，可以先看 u2-l1。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [WeaselTSF/KeyEventSink.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp) | `ITfKeyEventSink` 的 6 个回调实现、`_InitKeyEventSink`/`_UninitKeyEventSink`，以及核心私有方法 `_ProcessKeyEvent`。本讲主战场。 |
| [WeaselTSF/KeyEvent.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp) | `ConvertKeyEvent` 与 `TranslateKeycode`：把 Windows 虚拟键翻译成 Rime/librime 认识的 ibus keycode。 |
| [include/KeyEvent.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/KeyEvent.h) | `KeyInfo`（解析 `LPARAM` 位域）、`weasel::KeyEvent`（跨 IPC 传输的按键表示）、ibus 的 `Keycode`/`Modifier` 两个大枚举。 |
| [WeaselTSF/WeaselTSF.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.h) | 成员变量声明：`_fTestKeyDownPending`、`_lpbKeyState`、`m_client`、`_cand`、`_status`、`_committed`、`_async_edit`、`_isToOpenClose` 等。 |
| [WeaselTSF/WeaselTSF.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp) | `_EnsureServerConnected`：按键前确认管道还连着后台 Server，连不上会重试甚至拉起 Server。 |
| [WeaselTSF/Compartment.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp) | `_IsKeyboardDisabled` / `_IsKeyboardOpen`：读 TSF Compartment 判断键盘开关状态，决定按键要不要处理。 |
| [WeaselIPC/WeaselClientImpl.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp) | `ClientImpl::ProcessKeyEvent` 与 `_SendMessage`：把 `KeyEvent` 打包成 `PipeMessage` 经命名管道发出去，管道异常退化为「不吃键」。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：去重（`_fTestKeyDownPending`）、按键转换（`ConvertKeyEvent`）、特殊键与异常处理（Caps Lock、键盘开关、断线）。

### 4.1 TestKeyDown/KeyDown 去重：`_fTestKeyDownPending` 的作用

#### 4.1.1 概念说明

TSF 把一次物理按键拆成「询问」和「通知」两个阶段调用输入法：

1. **`OnTestKeyDown`**：系统先问「这个键你（输入法）要不要拦截？」输入法通过 `*pfEaten` 回答。
2. **`OnKeyDown`**：系统随后正式通知「这个键发生了」，此时输入法才真正去处理。

问题在于：不同应用对这两步的调用并不规范。Weasel 在源码注释里点出了两种「怪行为」：

- **QQ2012**：只调 `OnKeyDown`，不调 `OnTestKeyDown`。
- **MS WORD 2010 x64**：对同一个按键，连续调多次 `OnTestKeyDown`。

如果输入法每次回调都把按键发给后台 Server，那么在 WORD 里一次按键会被 Server 收到多次，候选窗口会「跳好几次」；在 QQ 里则可能因为 `OnTestKeyDown` 缺失而完全漏发。Weasel 的解法是用一个布尔标志 `_fTestKeyDownPending` 来做**幂等去重**：

- **核心假设**：每一次按键最终都会（且只会）引发一次 `OnKeyDown`。
- **约定**：`OnTestKeyDown` 是「预演」，真正决定按键是否发给 Server 的逻辑放在私有方法 `_ProcessKeyEvent` 里。第一次 `OnTestKeyDown` 调 `_ProcessKeyEvent` 并把结果记到 `_fTestKeyDownPending`；后续重复的 `OnTestKeyDown` 直接复用标志、不再发；`OnKeyDown` 时检查标志，若已 pending 就直接复用（不发），否则才调 `_ProcessKeyEvent`。

#### 4.1.2 核心流程

一次按键的去重流程（`_fTestKeyDownPending` 简记为 `P`，初值 `FALSE`）：

```text
系统回调 OnTestKeyDown(key)
├─ P == TRUE ?   →  *pfEaten = TRUE; 直接返回   （重复 Test，复用之前的「吃」决定）
├─ 否则：
│   ├─ _ProcessKeyEvent(key, pfEaten)   ← 第一次真正处理、发 IPC
│   └─ if (*pfEaten) P = TRUE           ← 标记「已预演过，等 OnKeyDown 复用」

系统回调 OnKeyDown(key)
├─ P == TRUE ?   →  P = FALSE; *pfEaten = TRUE; 返回  （复用预演结果，不重复发）
├─ 否则：_ProcessKeyEvent(key, pfEaten)               （应用没调 Test，这里补发）
```

> 注意 `OnKeyUp` / `OnTestKeyUp` 用的是另一组对称的标志 `_fTestKeyUpPending`，原理完全相同。本节以 Down 为例。

#### 4.1.3 源码精读

源码顶部那段注释正是上述策略的文字版，值得逐句读：

[WeaselTSF/KeyEventSink.cpp:76-84](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L76-L84) —— 注释说明「不同应用发的 Test/Key 组合很怪」，并用 `_fTestKeyDownPending` 同时实现「省略多次 Test」与「Key 复查 Test 结果」。

`OnTestKeyDown` 的实现：

[WeaselTSF/KeyEventSink.cpp:86-100](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L86-L100) —— 进入时先把 `_fTestKeyUpPending = FALSE`（清掉上一次 Up 阶段的残余标志）；若 `_fTestKeyDownPending` 已为真，说明这是重复 Test，直接 `*pfEaten = TRUE` 返回；否则调 `_ProcessKeyEvent` 真正处理，并在「吃键」时把标志置真。

```cpp
STDAPI WeaselTSF::OnTestKeyDown(ITfContext* pContext, WPARAM wParam,
                                LPARAM lParam, BOOL* pfEaten) {
  _fTestKeyUpPending = FALSE;
  if (_fTestKeyDownPending) { *pfEaten = TRUE; return S_OK; }
  _ProcessKeyEvent(wParam, lParam, pfEaten);
  _UpdateComposition(pContext);
  if (*pfEaten) _fTestKeyDownPending = TRUE;
  return S_OK;
}
```

`OnKeyDown` 的实现：

[WeaselTSF/KeyEventSink.cpp:102-115](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L102-L115) —— 若 `_fTestKeyDownPending` 为真，说明预演已处理过，这里只清标志并复用 `*pfEaten = TRUE`（**不再发 IPC**）；否则（QQ 那种没发 Test 的应用）才在这里调 `_ProcessKeyEvent` 补发。

这两个标志与键盘状态缓冲区声明在同一处：

[WeaselTSF/WeaselTSF.h:204-205](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.h#L204-L205) —— `_lpbKeyState[256]` 保存 256 个虚拟键的当前状态，`_fTestKeyDownPending`/`_fTestKeyUpPending` 就是上面用到的去重标志。

把这套 sink 挂到 TSF 框架，是在 u3-l1 提过的初始化链里完成的：

[WeaselTSF/KeyEventSink.cpp:156-167](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L156-L167) —— `_InitKeyEventSink` 从线程管理器取 `ITfKeystrokeMgr`，调 `AdviseKeyEventSink` 把 `this`（WeaselTSF 聚合了 `ITfKeyEventSink`）注册成按键接收器。`_UninitKeyEventSink`（169-176 行）对称地 `UnadviseKeyEventSink` 注销。

#### 4.1.4 代码实践

**实践目标**：用一个表追踪 `_fTestKeyDownPending` 在两种应用行为下的变化，验证去重逻辑。

**操作步骤（源码阅读型实践，不修改源码）**：

1. 打开 [WeaselTSF/KeyEventSink.cpp:86-115](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L86-L115)。
2. 假设用户按下一个颗会被输入法「吃掉」的键（例如拼音里的字母 `n`），分别针对「标准应用」「WORD（多次 Test）」「QQ（不发 Test）」三种情形，填写下表（`P` = `_fTestKeyDownPending`，`IPC` = 是否真的调用了 `_ProcessKeyEvent` → `m_client.ProcessKeyEvent`）：

   | 调用序列 | 进 OnTestKeyDown 前 P | 调 _ProcessKeyEvent? | 出 OnTestKeyDown 后 P | 进 OnKeyDown 前 P | 出 OnKeyDown 后 P | 一共发几次 IPC |
   | --- | --- | --- | --- | --- | --- | --- |
   | Test, Key（标准） | FALSE | 是 | TRUE | TRUE | FALSE | 1 |
   | Test, Test, Key（WORD） | FALSE / TRUE | 是 / 否 | TRUE / TRUE | TRUE | FALSE | 1 |
   | Key（QQ，无 Test） | — | — | FALSE | FALSE | FALSE | 1 |

3. 核对结论：无论哪种调用序列，最终都**只发一次 IPC**——这正是去重策略的目标。

**需要观察的现象**：在「标准」情形里，`OnKeyDown` 命中 `if (_fTestKeyDownPending)` 分支，复用了 Test 阶段的 `*pfEaten = TRUE`，不会再走 `_ProcessKeyEvent`。

**预期结果**：三次情形 IPC 计数均为 1。若你发现某一情形 IPC 计数为 0 或 ≥2，说明你对去重流程的理解有误，回到 4.1.2 的流程图对照。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `OnTestKeyDown` 里的 `if (*pfEaten) _fTestKeyDownPending = TRUE;` 改成无条件 `_fTestKeyDownPending = TRUE;`，会发生什么？

**参考答案**：对于「放行的键」（`*pfEaten == FALSE`，如功能键 F1），也会把标志置真，导致随后的 `OnKeyDown` 被当作「已预演」而直接 `*pfEaten = TRUE`——即把本该放行给应用的键也吃掉了。所以这个标志**只在「吃键」时才置真**，是刻意为之。

**练习 2**：`OnKeyDown` 里 `_fTestKeyUpPending = FALSE;` 这一行（106 行）的作用是什么？

**参考答案**：进入 Down 阶段时清掉上一次按键 Up 阶段残留的 `_fTestKeyUpPending`，避免按键序列「Down…Up…Down」之间标志串味。它与 `_fTestKeyDownPending` 是两组独立、对称的标志。

### 4.2 ConvertKeyEvent：把 Windows 按键翻译成 `weasel::KeyEvent`

#### 4.2.1 概念说明

`_ProcessKeyEvent` 拿到的 `WPARAM wParam` 是 Windows 虚拟键码（Virtual Key，如 `0x4E` 表示 `N`），`LPARAM lParam` 是按键的杂项信息（重复计数、扫描码、是否扩展键、是否抬起等）。但后台 librime 引擎用的是 **ibus keycode 体系**（与 X11/XKB 同源，如 `0x06E` 表示 `n`）。两者不能直接互通，必须翻译。

翻译的产物是 `weasel::KeyEvent`——一个 32 位的紧凑结构，低 16 位是 keycode、高 16 位是修饰键掩码（mask）。这个结构会被原样塞进命名管道发给 Server（见 4.2.3）。

几个关键数据结构（都在 `KeyEvent.h`）：

- `KeyInfo`：用位域从 `LPARAM` 里抠出扫描码、扩展键标志、是否抬起等信息。
- `weasel::KeyEvent`：`keycode : 16` + `mask : 16` 的位域，并提供 `operator UINT32()` 与 `KeyEvent(UINT x)`，便于**整体当作一个 32 位整数在 IPC 的 `wParam` 里搬运**。
- `ibus::Keycode` / `ibus::Modifier`：两个大枚举，是翻译的目标字典。

#### 4.2.2 核心流程

`ConvertKeyEvent` 的三步翻译：

```text
ConvertKeyEvent(vkey, kinfo/*解析自lParam*/, keyState[256], &result)
│
├─ 1. 算 mask（修饰键）
│     读 keyState[]：Shift↓ → SHIFT_MASK；CapsLock锁定 → LOCK_MASK；
│                    Ctrl↓ → CONTROL_MASK；Alt↓ → ALT_MASK；
│                    kinfo.isKeyUp → RELEASE_MASK
│     特例：vkey==VK_CAPITAL 且非抬起 → 把 LOCK_MASK 反转（见 4.3）
│
├─ 2. 算 keycode
│     a) 先查 TranslateKeycode(vkey, kinfo) —— 命中「有固定映射」的键
│        （方向键、F1~F24、NumPad、Shift/Ctrl/Alt 左右、Caps Lock 等）直接返回
│     b) 查不到 → 用 ToUnicodeEx() 把虚拟键解码成 Unicode 字符，
│        ret==1 时 result.keycode = 字符码（如 'n'==0x6E）
│     c) 仍失败 → keycode=0，返回 false（无法识别）
│
└─ 3. 返回 bool：true 表示翻译成功，可发给 Server；false 表示未知键
```

#### 4.2.3 源码精读

先看输入侧如何解析 `LPARAM`：

[include/KeyEvent.h:3-15](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/KeyEvent.h#L3-L15) —— `KeyInfo` 用位域直接 `reinterpret_cast` 自 `LPARAM`，把扫描码（8 位）、是否扩展键、是否抬起等位抠出来。注意它的构造函数接收的就是原始 `lParam`。

再看输出侧的紧凑结构：

[include/KeyEvent.h:18-27](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/KeyEvent.h#L18-L27) —— `weasel::KeyEvent` 是 `keycode:16 + mask:16`，并提供与 32 位整数的互转。正因为这套互转，Server 端才能用 `KeyEvent(wParam)`（见 u2-l1 的 `OnKeyEvent`）把管道 `wParam` 直接还原成 `KeyEvent`，全程零拷贝。

`ConvertKeyEvent` 的 mask 计算部分：

[WeaselTSF/KeyEvent.cpp:4-35](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L4-L35) —— 用 `KEY_DOWN(0x80)` / `TOGGLED(0x01)` 两个掩码从 `keyState[]` 判断各修饰键，按位或进 `result.mask`。其中 18-19 行读 `VK_CAPITAL` 的**锁定状态位**（`& 0x01`）来设 `LOCK_MASK`，与读按下状态（`& 0x80`）的 Shift/Ctrl/Alt 不同——这是 Caps Lock 作为「开关型」修饰键的特点。

`ConvertKeyEvent` 的 keycode 计算部分：

[WeaselTSF/KeyEvent.cpp:38-58](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L38-L58) —— 先调 `TranslateKeycode` 查固定映射表；查不到（普通字母、数字、标点）则用 `ToUnicodeEx` 解码成 Unicode 字符，`ret==1` 时把字符码作为 keycode。注意 48-51 行在解码前清掉了 Ctrl/Alt 状态，是为了让 `ToUnicodeEx` 返回「不带修饰的原始字符」（否则 Ctrl+N 会解出控制字符而非 `n`）。

`TranslateKeycode` 是一张巨大的 `switch(vkey)` 字典：

[WeaselTSF/KeyEvent.cpp:61-254](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L61-L254) —— 把 Windows 虚拟键逐个映射到 ibus keycode。注意几个有「左右」之分的关键处理：
- [L75-80](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L75-L80) 用扫描码 `0x36` 区分左/右 Shift。
- [L81-86](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L81-L86) 用 `isExtended` 区分左/右 Ctrl。
- [L69-74](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L69-L74) 用 `isExtended` 区分主回车与数字小键盘回车（`KP_Enter`）。
- [L91-92](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L91-L92) `VK_CAPITAL` → `ibus::Caps_Lock`。

ibus 枚举里的几个取值（作为「目标字典」参考）：

[include/KeyEvent.h:199-208](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/KeyEvent.h#L199-L208) —— `Shift_L/Shift_R`、`Control_L/Control_R`、`Caps_Lock`、`Alt_L/Alt_R` 等的十六进制值；[L221-245](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/KeyEvent.h#L221-L245) 是 `Modifier` 掩码枚举（`SHIFT_MASK=1`、`LOCK_MASK=2`、`CONTROL_MASK=4`、`ALT_MASK=8`、`RELEASE_MASK=1<<14` 等）。

#### 4.2.4 代码实践

**实践目标**：手算一次「左 Shift + N」按键，追踪它如何变成 `weasel::KeyEvent`。

**操作步骤**：

1. 假设按键时键盘状态 `keyState` 中 `VK_SHIFT` 的高位为 1（Shift 按下），`VK_CAPITAL` 锁定位为 0（未开大写）。
2. `wParam = 0x4E`（VK，字母 N），`lParam` 解析出的 `kinfo.isKeyUp = false`。
3. 在 [KeyEvent.cpp:13-29](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L13-L29) 逐项推 `mask`：
   - `VK_SHIFT` 按下 → `mask |= SHIFT_MASK`（= 1）
   - `VK_CAPITAL` 未锁定、`VK_CONTROL`/`VK_MENU` 未按下 → 其余不置位
   - 非抬起 → 不加 `RELEASE_MASK`
   - 结果：`mask = 0x0001`
4. 在 [KeyEvent.cpp:38-58](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L38-L58) 推 `keycode`：`TranslateKeycode(0x4E, …)` 在 switch 里查不到 `VK_N`（字典只列了特殊键），返回 `ibus::Null`（0）；于是走 `ToUnicodeEx`，清掉 Ctrl/Alt 后解码出字符 `N`（大写，因为 Shift 按下），`ret==1`，`keycode = 0x004E`（即 `'N'` 的 Unicode）。

   > 说明：字母/数字/标点在 Weasel 里**不走** `TranslateKeycode` 的固定表，而是靠 `ToUnicodeEx` 解码出当前键盘布局下的实际字符（含大小写），所以 `Shift+N` 解出的是大写 `N` 而非小写 `n`。具体的「这个字符要不要由 Rime 处理」是后台 librime 的事，本讲只负责把键送过去。

5. 最终 `weasel::KeyEvent{keycode=0x4E, mask=0x0001}`，作为一个 32 位整数（低 16 = 0x4E，高 16 = 0x01，即 `0x0001004E`）通过 IPC 发出。

**需要观察的现象**：修饰键信息全部进了 `mask`，字符本身进了 `keycode`；二者拼成一个 32 位整数。

**预期结果**：`KeyEvent = 0x0001004E`。可对照 [include/KeyEvent.h:24-26](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/KeyEvent.h#L24-L26) 的 `operator UINT32()` 验证位拼装顺序。

> 待本地验证：第 4 步 `ToUnicodeEx` 在不同键盘布局（如 Dvorak）下会解出不同字符，本讲以标准 QWERTY 为例。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ToUnicodeEx` 调用前要 `table[VK_CONTROL]=0; table[VK_MENU]=0;`（48-51 行）？

**参考答案**：带着 Ctrl/Alt 状态去解码，`ToUnicodeEx` 会返回控制字符（如 Ctrl+M → `0x0D`）而非字母 `m`，Rime 就认不出这是普通字母键了。临时清掉这两个修饰键，是为了得到「这个键本身对应什么字符」。

**练习 2**：左 Shift 和右 Shift 的虚拟键都是 `VK_SHIFT`，Weasel 如何区分它们？

**参考答案**：在 [KeyEvent.cpp:75-80](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L75-L80) 用 `lParam` 里的扫描码区分：扫描码 `0x36` 是右 Shift（`Shift_R`），否则左 Shift（`Shift_L`）。左/右 Ctrl 则用扩展键标志 `isExtended` 区分（81-86 行）。

### 4.3 特殊键与连接异常处理：放行与 Caps Lock 补丁

#### 4.3.1 概念说明

并非所有按键都要送给 Server。`_ProcessKeyEvent` 在调 IPC 之前有两道「放行闸门」，还有一道针对 Caps Lock 的特殊补丁。理解这三段逻辑，是理解「为什么有些键直接透传给应用」的关键。

**闸门一：键盘开关 / 禁用**。如果用户把输入法键盘关掉了（语言栏切到英文状态、且配置了「键盘可关闭」），或者当前文档把键盘禁用了（如全屏游戏），按键应当**直接放行**给应用，不发给 Server。

**闸门二：连不上 Server**。命名管道没连通（Server 没起来、崩了），按键也只能放行——否则用户会在 Server 不可用时「打不出任何字」。`_EnsureServerConnected` 在连不上时还会尝试拉起 Server（u3-l1 已讲过），本讲只关注它返回 `false` 时的放行后果。

**Caps Lock 补丁**。Caps Lock 在 Windows 里是个「按下即切换状态」的开关键，而 librime/Rime 期望的是「先收到 `Caps_Lock` 按键事件，再看到状态变化」。两边的时序不一致，Weasel 用两段 hack 弥补：
1. **翻译时反转 mask**：`VK_CAPITAL` 按下时，Windows 已经把锁定状态翻转了，所以 `ConvertKeyEvent` 里要 `mask ^= LOCK_MASK` 把它「翻回去」，让 Rime 看到的是翻转前的状态。
2. **抬起时模拟两次击键**：当检测到 Caps Lock 抬起、且此时 Caps Lock 处于锁定态、且刚有提交或正在组词时，用 `SendInput` 模拟一次「按下+抬起 Caps Lock」，把状态再切回去，避免输入法状态与系统大写状态错乱。

#### 4.3.2 核心流程

`_ProcessKeyEvent` 的整体流程：

```text
_ProcessKeyEvent(wParam, lParam, *pfEaten):
  ① 键盘关闭(_isToOpenClose && !_IsKeyboardOpen()) 或 键盘禁用(_IsKeyboardDisabled())
     → *pfEaten = FALSE; return          （闸门一：放行）
  ② !_EnsureServerConnected()
     → *pfEaten = FALSE; return          （闸门二：放行）

  GetKeyboardState(_lpbKeyState)         （抓 256 键当前状态）
  ke = ConvertKeyEvent(wParam, lParam, _lpbKeyState)   （4.2 的翻译）
  if (!Convert 成功)
     → *pfEaten = FALSE; return          （未知键：放行）

  ③ 候选窗口「竖排自动反转」时，把 Up/Down 互换
  ④ if (keyCountToSimulate == 0)
        *pfEaten = m_client.ProcessKeyEvent(ke)   ← 真正发 IPC
  ⑤ Caps Lock 补丁：抬起 + 锁定 + 刚提交/组词中 → SendInput 补两次击键
  ⑥ 记录 prevKeyEvent / prevfEaten 供下次 Caps Lock 判断
```

#### 4.3.3 源码精读

`_ProcessKeyEvent` 全貌：

[WeaselTSF/KeyEventSink.cpp:11-63](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L11-L63) —— 两道放行闸门在前（14-23 行），翻译与发送在中（24-38 行），Caps Lock 补丁在后（40-58 行）。下面分段说明。

**闸门一：键盘开关/禁用**

[WeaselTSF/KeyEventSink.cpp:14-17](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L14-L17) —— `_isToOpenClose` 是配置项（来自 [WeaselTSF/WeaselTSF.cpp:176](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L176) 的 `_ToggleImeOnOpenClose == L"yes"`），表示「允许用键盘开关键盘」；`_IsKeyboardOpen()` 读 `GUID_COMPARTMENT_KEYBOARD_OPENCLOSE`：

[WeaselTSF/Compartment.cpp:144-160](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp#L144-L160) —— `_IsKeyboardOpen` 从 TSF Compartment 读键盘开关状态。

[WeaselTSF/Compartment.cpp:90-142](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp#L90-L142) —— `_IsKeyboardDisabled` 读 `GUID_COMPARTMENT_KEYBOARD_DISABLED` 与 `GUID_COMPARTMENT_EMPTYCONTEXT`，任一被应用置位即视为禁用（典型场景：游戏、密码框）。

**闸门二：连不上 Server**

[WeaselTSF/KeyEventSink.cpp:20-23](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L20-L23) —— `_EnsureServerConnected()` 返回 false 时放行。该函数：

[WeaselTSF/WeaselTSF.cpp:238-281](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L238-L281) —— 先 `m_client.Echo()` 探测管道；失败则 `_Reconnect` 并累计 `retry`，连续 6 次失败后会起后台线程跑 `start_service.bat` 拉起 `WeaselServer.exe` 再重连。注意它**最终用最后一次 `Echo()` 的结果**决定返回值——所以即便它在后台拉 Server，当前这一次按键仍会被放行（`*pfEaten = FALSE`），用户要等下一颗键才能恢复输入。

**翻译与发送**

[WeaselTSF/KeyEventSink.cpp:24-38](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L24-L38) —— `GetKeyboardState` 抓当前 256 键状态喂给 `ConvertKeyEvent`；翻译失败放行；成功后处理候选窗口竖排反转（31-36 行，见 [CandidateList.h:59-62](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/CandidateList.h#L59-L62) 的 `GetIsReposition`），最后 `*pfEaten = (BOOL)m_client.ProcessKeyEvent(ke)` 真正发 IPC。

`m_client.ProcessKeyEvent` 在客户端的实现：

[WeaselIPC/WeaselClientImpl.cpp:58-65](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L58-L65) —— `_Active()` 守卫（无会话直接返回 false），否则 `_SendMessage(WEASEL_IPC_PROCESS_KEY_EVENT, keyEvent, session_id)`，返回非 0 即「吃键」。

`_SendMessage` 把异常退化为「不吃键」：

[WeaselIPC/WeaselClientImpl.cpp:195-202](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L195-L202) —— 把 `KeyEvent` 打包进 `PipeMessage`（命令=PROCESS_KEY_EVENT、wParam=`KeyEvent` 整数、lParam=`session_id`）调 `channel.Transact`；`catch(DWORD)` 捕获管道异常返回 0。这与 u2-l3 讲过的「管道异常退化为返回 0（不吃键）」一致——**这是一道隐形的第三道放行闸门**：即使前两道闸门都通过、键也翻译成功，一旦管道在传输中崩了，`ProcessKeyEvent` 仍返回 0，`*pfEaten` 变 `FALSE`，键被放行给应用。

**Caps Lock 补丁**

翻译时的 mask 反转：

[WeaselTSF/KeyEvent.cpp:30-35](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L30-L35) —— 注释点明 Windows 的 `VK_CAPITAL` 在送来时锁定状态已经翻转，而 Rime 期望看到翻转前的状态，故用异或 `^=` 反转 `LOCK_MASK`。

抬起时的两次击键模拟：

[WeaselTSF/KeyEventSink.cpp:40-58](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L40-L58) —— 仅当「当前是 Caps_Lock 抬起（`RELEASE_MASK`）」「上一颗也是 Caps_Lock 且被吃了」「不是模拟击键过程中」三个条件同时成立，且 `GetKeyState(VK_CAPITAL)` 显示锁定态、且刚提交（`_committed`）或正在组词（`_status.composing`）时，用 `SendInput` 模拟一次「按下+抬起」把状态切回去；同时用文件级静态变量 `keyCountToSimulate`（[KeyEventSink.cpp:9](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L9)）计数，确保这两颗模拟键不会再被 `_ProcessKeyEvent` 重复发给 Server（37 行 `if (!keyCountToSimulate)` 守卫）。`prevKeyEvent`/`prevfEaten`（7-8 行）用于跨按键记住上一次状态。

#### 4.3.4 代码实践

**实践目标**：追踪「Server 没启动时按字母键」的完整放行路径，体会三道闸门的兜底作用。

**操作步骤（源码阅读型实践）**：

1. 假设后台 `WeaselServer.exe` 未运行，用户在记事本按下 `a`。
2. 系统调 `OnTestKeyDown`（假设是「标准」应用，于是调 `_ProcessKeyEvent`）。
3. 在 [KeyEventSink.cpp:14-23](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L14-L23) 判断：键盘未禁用、`_isToOpenClose` 为假（或键盘开着）→ 闸门一通过；`_EnsureServerConnected()` 内 `m_client.Echo()` 探测失败 → **闸门二返回 false → `*pfEaten = FALSE`，函数直接 return**。
4. 后续 `OnTestKeyDown` 因 `*pfEaten == FALSE` 不会把 `_fTestKeyDownPending` 置真。
5. `OnKeyDown` 里 `_fTestKeyDownPending == FALSE`，于是再走一次 `_ProcessKeyEvent`——但这次 `_EnsureServerConnected` 的内部 `retry` 已累加，可能仍未连上，再次放行（`*pfEaten = FALSE`）。
6. 最终 `*pfEaten = FALSE`，系统把 `a` 透传给记事本，记事本正常打出 `a`。

**需要观察的现象**：整个过程中 `m_client.ProcessKeyEvent` 一次都没真正发出去（因为闸门二在 20 行就 return 了）；用户仍能打字。

**预期结果**：记事本正常出现字母 `a`，Weasel 不卡键。若 `_EnsureServerConnected` 后台线程成功拉起 Server，那么**下一颗键**起 `_EnsureServerConnected` 返回 true，开始正常进入候选流程。

> 待本地验证：`retry` 是文件级静态变量（[WeaselTSF/WeaselTSF.cpp:236](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L236)），其累加节奏与 6 次阈值后的拉起行为，需结合真实进程观察。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉闸门二（把 [KeyEventSink.cpp:20-23](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L20-L23) 去掉），Server 没启动时按字母键会怎样？

**参考答案**：会继续走到 38 行 `m_client.ProcessKeyEvent(ke)`。客户端 `_SendMessage` 经 `channel.Transact` 时管道写失败抛 `DWORD` 异常，被 [WeaselClientImpl.cpp:199-201](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L199-L201) 的 `catch` 退化为返回 0，`*pfEaten` 仍为 `FALSE`，键最终仍放行。也就是说闸门二并非「正确性必需」，而是**性能优化**——避免每次按键都走一次必然失败的 IPC 往返和异常抛接，并借机拉起 Server。第三道隐形闸门（catch 退化为 0）才是最终兜底。

**练习 2**：Caps Lock 补丁里的 `keyCountToSimulate` 为什么是文件级静态变量，而不是成员变量？

**参考答案**：它要跨「真实的 Caps Lock 按下/抬起」和「`SendInput` 模拟出来的两颗击键」计数。模拟键会再次进入 `_ProcessKeyEvent`，用 `if (!keyCountToSimulate)` 守卫（37 行）阻止它们被重复发给 Server，并在 56-57 行递减。由于模拟击键由系统重新分发、可能落到同进程的同一个 WeaselTSF 实例上，用文件级静态变量保证计数可见即可（早期 TSF 单实例场景下足够）。

## 5. 综合实践

把三个模块串起来，完成一次「全链路按键追踪」。选择一个**普通字母键**（例如拼音输入法下打 `n`），按下表逐格填写，把每一步对应的源码行号、关键变量值写清楚：

| 阶段 | 系统回调 / 调用 | 源码位置 | `_fTestKeyDownPending` | `_lpbKeyState`/`ke` | `pfEaten` | 是否发 IPC |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | OnTestKeyDown 进入 | KeyEventSink.cpp:86 | FALSE（初始） | — | 未定 | — |
| 2 | 去重判断 | KeyEventSink.cpp:91 | FALSE → 继续 | — | — | — |
| 3 | 闸门一/二 | KeyEventSink.cpp:14-23 | FALSE | 键盘开着、Server 在 | — | — |
| 4 | GetKeyboardState + ConvertKeyEvent | KeyEventSink.cpp:25-26 | FALSE | ke={0x6E, 0}（小写 n，无修饰） | 未定 | — |
| 5 | ProcessKeyEvent 发 IPC | KeyEventSink.cpp:38 | FALSE | ke 同上 | TRUE（Rime 认了） | ✅ |
| 6 | _UpdateComposition | KeyEventSink.cpp:96 | FALSE | — | TRUE | — |
| 7 | 置 pending | KeyEventSink.cpp:98 | **TRUE** | — | TRUE | — |
| 8 | OnKeyDown 进入 | KeyEventSink.cpp:102 | TRUE | — | — | — |
| 9 | 复用 pending | KeyEventSink.cpp:107-109 | TRUE → FALSE | — | TRUE | ❌（不重发） |

**要求**：

1. 用 [KeyEvent.cpp:4-59](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L4-L59) 推出 `ke` 的精确值（注意：拼音模式下按 `n` 通常没有修饰键，`ToUnicodeEx` 解出小写 `n` = `0x6E`）。
2. 在第 5 步标注 IPC 命令号 `WEASEL_IPC_PROCESS_KEY_EVENT` 与 `wParam` = `KeyEvent` 整数值、`lParam` = `session_id`（参考 [WeaselClientImpl.cpp:62-63](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L62-L63)）。
3. 用一句话解释：为什么第 9 步不重发 IPC？答：`OnKeyDown` 命中 `_fTestKeyDownPending` 为真，复用了 Test 阶段的「吃键」结论。

> 待本地验证：第 4 步 `ke.mask` 在「Shift 没按、大写没开」时为 0；若你在本机按的是 `Shift+n`，请相应改成 `mask = SHIFT_MASK`、`keycode` 为大写 `N`。

## 6. 本讲小结

- Weasel 用 `_fTestKeyDownPending`（Down）和 `_fTestKeyUpPending`（Up）两组对称标志，把 TSF 的「Test + 正式」两阶段回调去重为「每个物理按键只发给后台 Server 一次」，兼容了 WORD（多次 Test）和 QQ（不发 Test）两类不规范应用。
- `ConvertKeyEvent` 三步翻译：先用 `keyState[]` 算修饰键 `mask`，再用 `TranslateKeycode` 字典查特殊键，查不到则用 `ToUnicodeEx` 解码成 Unicode 字符；产物 `weasel::KeyEvent{keycode:16, mask:16}` 作为一个 32 位整数经 IPC 搬运，Server 端零拷贝还原。
- `_ProcessKeyEvent` 有两道显式放行闸门（键盘关闭/禁用、连不上 Server）和一道隐式兜底（`_SendMessage` 的 `catch(DWORD)` 退化为返回 0），共同保证「Server 不可用时不卡键」。
- Caps Lock 因 Windows 与 Rime 的状态时序不一致，有两段补丁：翻译时 `mask ^= LOCK_MASK` 反转锁定状态、抬起时按需 `SendInput` 模拟两次击键并用 `keyCountToSimulate` 防止重复发送。
- 竖排候选窗口自动反转时，`Up`/`Down` 方向键在发送前会被互换（`_cand->GetIsReposition()`）。

## 7. 下一步学习建议

- **按键送出去之后**：本讲止步于 `m_client.ProcessKeyEvent` 把键发进管道。Server 收到后如何调 librime `process_key`、如何回写 Context/Status，请进入 **u4 单元**（RimeWithWeasel 桥接），特别是 u4-l2「会话管理与按键处理」。
- **上屏逻辑**：`_ProcessKeyEvent` 后紧跟着 `_UpdateComposition`，它如何用 TSF EditSession 把 preedit 写进应用文档、如何提交最终文字，是 **u3-l3「编辑会话与上屏」** 的主题。
- **候选列表交互**：本讲提到 `_cand->GetIsReposition()` 的竖排反转，候选窗口的鼠标点选、hover 高亮如何回传给 Server，见 **u3-l4「候选列表、语言栏与显示属性」**。
- **IPC 传输细节**：若想深究 `PipeMessage` 如何在命名管道上往返、`channel.Transact` 的重连机制，回到 **u2-l2「PipeChannel」** 与 **u2-l3「客户端与服务器实现」**。
