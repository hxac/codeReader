# UI 更新、消息通知与维护/主题

## 1. 本讲目标

前几讲我们已经把 `RimeWithWeaselHandler` 的「数据通路」打通了：u4-l1 把引擎拉起来并接好 `OnNotify` 回调，u4-l2 让一次按键经 `ProcessKeyEvent → rime_api->process_key → _Respond → _UpdateUI` 完成处理，u4-l3 讲清了配置如何按方案/按应用叠加。但你可能还留着几个「最后一公里」的疑问：

- **`_UpdateUI` 到底是怎么把算出来的 `Context`/`Status` 推给候选窗口的？候选窗口什么时候显示、什么时候隐藏？**
- **切方案时弹出的「【明月拼音简体】」提示、部署时弹出的「正在部署 RIME」提示，是从哪里冒出来的？为什么有的开关切换有提示、有的没有？**
- **重新部署时输入法为什么「暂时失灵」？深色模式切换后配色为什么能自动跟着变？**

本讲就回答这些问题。它聚焦 `RimeWithWeaselHandler` 里三个「收尾性质」的模块。读完本讲你应当能够：

- 说清楚 `_UpdateUI` 如何作为**唯一的 UI 推送漏斗**，决定候选窗口的显隐与内容刷新，以及它如何借助 `_ShowMessage` 的返回值在「正常候选显示」与「瞬时提示」两种模式间切换。
- 掌握 Rime 通知（`deploy` / `schema` / `option`）经由静态缓冲 → `_ShowMessage` → `ShowWithTimeout` → 定时器自动隐藏的完整链路，理解 `m_show_notifications`（哪些通知要显示）与 `m_show_notifications_time`（显示多久）两套控制机制。
- 理解维护模式（`StartMaintenance` / `EndMaintenance`）如何让引擎在数据重建期间安全禁用，以及深色主题（`UpdateColorTheme`）如何响应系统配色变化并重算每个会话的样式。

## 2. 前置知识

进入源码前，先建立四个直觉。

**（1）`_UpdateUI` 是「每按键必经」的漏斗。** 回顾 u4-l2：`ProcessKeyEvent` 在调完 `rime_api->process_key` 与 `_Respond` 之后，**必定**调用 `_UpdateUI(ipc_id)`；`CommitComposition`、`ClearComposition`、`FocusIn`、`ChangePage`、`HighlightCandidateOnCurrentPage` 等几乎所有对外命令也都在末尾调 `_UpdateUI`。也就是说，**前端候选窗口看到的一切变化，都汇聚到这一个函数**。理解了 `_UpdateUI`，就理解了 Weasel 的整个「输出侧」。

**（2）候选窗口是一个「会自动消失」的定时器窗口。** 回顾 u5 会详谈 `WeaselPanel`，这里只需知道 `weasel::UI` 暴露了三组显示接口：`Show()`（常显）、`Hide()`（隐藏）、`ShowWithTimeout(ms)`（显示 N 毫秒后自动隐藏）。瞬时提示（如切方案提示）用的就是 `ShowWithTimeout`，它内部靠一个 `SetTimer` 定时器在到期时回调 `Hide()`。`IsCountingDown()` 用来查询「当前是否正有一个倒计时提示在显示」——这个状态在本讲的去重逻辑里至关重要。

**（3）通知来自两个方向。** Weasel 向用户显示的提示文字有两类来源：

| 来源 | 产生者 | 例子 |
|---|---|---|
| 引擎主动推送 | librime 经 `OnNotify` 回调写入静态缓冲（u4-l1 §4.3） | 切方案、部署开始/成功/失败、开关翻转 |
| Handler 主动判断 | `_GetStatus` 检测到 `schema_id` 变化 | 切方案时直接显示方案名 |

两者最终都汇入 `_ShowMessage`，由它统一决定「要不要显示、显示什么、显示多久」。

**（4）维护模式与深色主题都是「全局重算」。** 维护模式会 `Finalize` 整个引擎再重新 `Initialize`；深色主题切换会重新读取 `weasel.yaml` 的配色并**为每一个已存在的会话重算样式**。它们都不针对单个会话，而是影响全局状态，所以都伴随一次 `_UpdateUI(0)`（`ipc_id=0` 是「无会话」的特殊调用，用来刷新禁用态等全局信息）。

## 3. 本讲源码地图

本讲主要围绕 `RimeWithWeasel.cpp` 的若干函数，辅以 UI 实现与系统主题侦测：

| 文件 | 作用 |
|---|---|
| [RimeWithWeasel/RimeWithWeasel.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp) | 本讲主战场：`_UpdateUI`、`_ShowMessage`、`_GetStatus`、`StartMaintenance`/`EndMaintenance`、`UpdateColorTheme`、`_UpdateShowNotifications` 全在此 |
| [include/RimeWithWeasel.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h) | `m_show_notifications` / `m_show_notifications_time` / `m_current_dark_mode` / `m_disabled` 等成员声明 |
| [include/WeaselUI.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUI.h) | `weasel::UI` 的 `Show`/`Hide`/`ShowWithTimeout`/`IsCountingDown`/`Update` 接口契约 |
| [WeaselUI/WeaselUI.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselUI.cpp) | `UIImpl` 的定时器实现：`ShowWithTimeout` 启动 `AUTOHIDE_TIMER`，`OnTimer` 到期回调 `Hide` |
| [WeaselIPCServer/WeaselServerImpl.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp) | `OnColorChange`：监听系统主题变化并触发 `UpdateColorTheme` |
| [include/WeaselUtility.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUtility.h) | `IsUserDarkMode()`：读注册表判断系统深/浅色 |
| [output/data/weasel.yaml](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/data/weasel.yaml) | `show_notifications` / `show_notifications_time` 的真实出厂配置 |

---

## 4. 核心概念与源码讲解

### 4.1 _UpdateUI：唯一的 UI 推送漏斗与显隐控制

#### 4.1.1 概念说明

`ProcessKeyEvent` 调完引擎、`_Respond` 把响应文本写回管道之后，候选窗口并不会「自己」更新——它需要有人显式地把最新的 `Context`（写作串、候选页）和 `Status`（中/英、方案名、是否禁用）喂给 `weasel::UI`。这个「喂」的动作就是 `_UpdateUI`。

`_UpdateUI` 之所以重要，是因为它承担了**三重职责**：

1. **采集**：从 librime 拉取最新的 `Status`/`Context`（经 `_GetStatus`），并刷新 `inline_preedit` 能力位。
2. **分发提示**：调用 `_ShowMessage` 处理待显示的引擎通知（见 §4.2）。
3. **显隐裁决**：根据 `_ShowMessage` 的返回值，决定是「显示正常候选」还是「让瞬时提示继续显示」，并在末尾清空通知缓冲。

换句话说，`_UpdateUI` 是「正常输入候选」与「一次性提示」两条显示路径的**分叉点**。

#### 4.1.2 核心流程

`_UpdateUI(ipc_id)` 的执行步骤：

```text
_UpdateUI(ipc_id)
  ├─ 0. 守卫：m_ui 为空则直接返回（无 UI 可更新）
  ├─ 1. 取 UI 当前的 status 引用 weasel_status，新建空 weasel_context
  ├─ 2. 若 ipc_id==0（无会话调用，如维护/主题切换）：
  │       weasel_status.disabled = m_disabled      ← 把全局禁用态透传给 UI
  ├─ 3. _GetStatus(weasel_status, ipc_id, weasel_context)
  │       └─ 填充 schema_name/ascii_mode/composing 等
  │       └─ 若检测到方案切换：重载方案样式，可能直接弹方案名提示
  ├─ 4. 按 inline_preedit 选项设置 client_caps 的 INLINE_PREEDIT_CAPABLE 位
  ├─ 5. if (!_ShowMessage(weasel_context, weasel_status)):
  │       m_ui->Hide()                              ← 无提示要显示
  │       m_ui->Update(weasel_context, weasel_status) ← 走「正常候选」路径
  │     （若 _ShowMessage 返回 true，则提示已由它内部显示，跳过正常路径）
  ├─ 6. _RefreshTrayIcon(...)                       ← 刷新托盘/语言栏图标
  └─ 7. 锁内清空 m_message_type/value/label/option_name  ← 消费确认
```

这里最关键的是第 5 步的**返回值分叉**：`_ShowMessage` 返回 `false` 表示「没有提示需要显示，且当前也没有倒计时提示在显示」，于是走正常路径——先 `Hide()` 再 `Update()`（`Update` 内部会按内容是否变化决定是否真的重绘，见下文）；返回 `true` 表示「已经显示了一个瞬时提示，或者有一个倒计时提示正在显示」，此时**不能再 `Hide()`**，否则会把刚弹出的提示立即抹掉。

第 4 步的 `client_caps` 位是 u4-l3 提到的 inline preedit 与 TSF 前端的桥梁：`_UpdateUI` 把引擎当前的 `inline_preedit` 选项翻译成 `INLINE_PREEDIT_CAPABLE` 位写进 `style.client_caps`，再随 `_Respond` 的 `style=` 响应传给 WeaselTSF（见 u3-l3）。

#### 4.1.3 源码精读

`_UpdateUI` 的完整实现见 [RimeWithWeasel/RimeWithWeasel.cpp:518-553](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L518-L553)：

```cpp
void RimeWithWeaselHandler::_UpdateUI(WeaselSessionId ipc_id) {
  // if m_ui nullptr, _UpdateUI meaningless
  if (!m_ui)
    return;

  Status& weasel_status = m_ui->status();   // 注意：取的是 UI 持有的引用
  Context weasel_context;                    // 注意：每次新建一个全新的局部 context

  RimeSessionId session_id = to_session_id(ipc_id);

  if (ipc_id == 0)
    weasel_status.disabled = m_disabled;     // 无会话调用：透传全局禁用态
```

注意一个细节：`weasel_status` 是 `m_ui->status()` 的**引用**，所以对它的修改会直接落到 UI 对象上；而 `weasel_context` 是**全新构造的局部变量**（`Context weasel_context;`），这意味着每次 `_UpdateUI` 都从一张「白纸」开始组装上下文——如果这次没有候选，`weasel_context` 就是空的，`Update` 后窗口自然不显示候选。

`client_caps` 位的设置：

[inline_preedit → client_caps 能力位](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L533-L537) —— 根据 `get_option(session_id, "inline_preedit")` 给 `session_status.style.client_caps` 置位或清位 `INLINE_PREEDIT_CAPABLE`。这是「运行时动态切换 inline preedit」能即时生效的落点。

显隐裁决与正常路径：

[显隐裁决：_ShowMessage 返回值分叉](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L539-L542) —— `_ShowMessage` 返回 `false` 时才 `Hide()` + `Update()`。这个 `Hide()` 是必须的：当一个拼音串被清空（用户按了 Escape 或上屏完毕），`weasel_context` 为空，必须显式隐藏窗口；而 `Update` 会把空上下文写进 UI。

末尾的「消费确认」清空：

[清空通知缓冲](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L546-L552) —— 在 `m_notifier_mutex` 锁内清空四个静态缓冲。这一步保证一条通知只被消费一次，避免下一次按键时 `_ShowMessage` 重复读到旧消息（详见 §4.2）。

再看 UI 侧 `Update` 的「去重」实现，见 [WeaselUI/WeaselUI.cpp:164-179](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselUI.cpp#L164-L179)：

```cpp
void UI::Update(const Context& ctx, const Status& status) {
  if (ctx_ == ctx && status_ == status)
    return;                 // 内容没变就不重绘
  ctx_ = ctx;
  status_ = status;
  // ... candidate_abbreviate_length 截断处理 ...
  Refresh();
}
```

因为 `_UpdateUI` 几乎每按键都调，而很多按键并不改变候选内容（比如在候选窗口已显示时移动光标），这层 `ctx_ == ctx && status_ == status` 的相等比较能避免大量无谓重绘。这也是为什么 `_UpdateUI` 里要先 `Hide()` 再 `Update()`——`Hide` 只是把窗口隐藏，真正决定「画什么」的是 `Update` 里的内容比对。

#### 4.1.4 代码实践

**实践目标**：观察 `_UpdateUI` 的显隐分叉，理解「空上下文 → 隐藏」「有候选 → 显示」的切换。

**操作步骤**：

1. 阅读 [RimeWithWeasel/RimeWithWeasel.cpp:518-553](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L518-L553)，确认每次都新建空的 `weasel_context`，且 `_GetStatus` 内部并没有给它填充候选（候选填充发生在 `_Respond`，见 u4-l2；`_UpdateUI` 这里主要填 `status`）。
2. 在 `_UpdateUI` 的第 5 步分叉处（[L539](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L539)）用阅读方式推断：用户按下字母键产生候选时，`_ShowMessage` 返回什么？用户按 Escape 清空拼音时又返回什么？
3. （**示例代码**，仅用于观察，勿提交）在分叉前后各加一行日志：

   ```cpp
   // 示例代码：仅用于观察，勿提交
   bool showed = _ShowMessage(weasel_context, weasel_status);
   DLOG(INFO) << "_UpdateUI: _ShowMessage=" << showed
              << " composing=" << weasel_status.composing;
   if (!showed) { m_ui->Hide(); m_ui->Update(weasel_context, weasel_status); }
   ```

4. 用 Debug 版 `WeaselServer.exe` 复现「输入拼音 → 选字上屏 → 继续输入」序列，查看 `%TEMP%\rime.weasel\` 下日志（**待本地验证**）。

**需要观察的现象**：

- 有候选/有写作串时，`_ShowMessage` 多数返回 `false`（没有引擎通知），走 `Hide()+Update()` 正常路径，候选窗口显示。
- 上屏完毕、拼音清空时，`weasel_context` 为空，`Update` 把空内容写入，窗口隐藏。
- 切方案那一刻，`_ShowMessage` 返回 `true`（见 §4.2），**跳过** `Hide()+Update()`，提示继续显示。

**预期结果**：能用一句话回答「为什么上屏后候选窗口会消失」——因为下一次 `_UpdateUI` 拿到的是空 `weasel_context`，`_ShowMessage` 返回 `false`，于是 `Hide()`+`Update(空)`。

#### 4.1.5 小练习与答案

**练习 1**：`_UpdateUI` 里 `weasel_status` 用引用、`weasel_context` 用局部变量，为什么不对称？

**参考答案**：`weasel_status` 取 `m_ui->status()` 的引用，是为了把「禁用态」「方案名」等**稳态字段**直接刷到 UI 对象上（`_GetStatus` 也向它写入），省得再拷贝一次；而 `weasel_context`（写作串、候选页）每次按键都可能完全不同，且要和「上一次的内容」做相等比较来决定是否重绘，所以必须从空对象开始重新组装，用值传递交给 `Update`。两者语义不同：status 是「状态刷新」，context 是「内容重建」。

**练习 2**：`UI::Update` 开头的 `if (ctx_ == ctx && status_ == status) return;` 若去掉会有什么后果？

**参考答案**：`_UpdateUI` 几乎每个按键、每个 IPC 命令都会调用，去掉这层去重后，即使候选内容没变也会每次 `Refresh()`（触发 `WM_PAINT` 重绘整个分层窗口）。这在高频输入时会带来明显的 CPU/GPU 开销和闪烁。这层比较是「内容变化才重绘」的关键优化。

**练习 3**：为什么 `_UpdateUI` 在 `ipc_id == 0` 时只设置 `weasel_status.disabled`？

**参考答案**：`ipc_id == 0` 表示这是一次「无具体会话」的调用，发生在 `StartMaintenance` / `EndMaintenance` / `UpdateColorTheme` 等全局动作之后（见 §4.3）。此时没有具体会话可查询，`_GetStatus` 也取不到有意义的内容，调 `_UpdateUI(0)` 的唯一目的就是**把全局禁用态 `m_disabled` 反映到 UI**（让托盘/状态显示「禁用」），所以只设置 `disabled` 字段。

---

### 4.2 通知转提示：OnNotify → _ShowMessage → 定时隐藏

#### 4.2.1 概念说明

u4-l1 §4.3 已经讲过 `OnNotify` 如何把 librime 的主动通知（`deploy`/`schema`/`option`）写进受 `m_notifier_mutex` 保护的静态缓冲。本讲接续这条链路，看缓冲里的消息是如何变成屏幕上一闪而过的提示文字的——这个「翻译 + 显示 + 自动消失」的中介就是 `_ShowMessage`。

`_ShowMessage` 要回答三个问题：

1. **显示什么文字（或图标）？** 不同 `message_type` 对应不同呈现：`deploy` 是「正在部署/部署完成/有错误」，`schema` 是方案名，`option` 要么显示开关的本地化标签、要么只切换图标不显示文字。
2. **要不要显示？** 受 `m_show_notifications`（一个「白名单」集合）和 `add_session`（会话刚创建时抑制提示）双重控制；`deploy` 消息是例外，**总是显示**。
3. **显示多久？** 由 `m_show_notifications_time`（毫秒，默认 1200）决定；为 0 时不启动倒计时（提示常显直到下一次 `Update` 把它顶掉）。

这套机制还内置了**去重/不打断**逻辑：如果一个倒计时提示正在显示，新的非图标类文字提示不会打断它，而是「让位」。

#### 4.2.2 核心流程

通知从产生到消失的完整生命周期：

```text
[可能在线程] librime 产生事件
  └─ OnNotify(this, sid, type, value)        ← u4-l1 §4.3，写静态缓冲
        └─ lock: m_message_type/value/label/option_name

[主线程] 下一次按键/会话动作 → _UpdateUI(ipc_id)
  └─ _ShowMessage(ctx, status)
        └─ lock: 读取 type/value/label
        ├─ 若 type/value 为空 → return IsCountingDown()   （无消息）
        ├─ 按 type 把提示写进 ctx.aux.str 或设 show_icon
        ├─ counter = IsCountingDown()
        ├─ if (!show_icon && counter) return counter       （有提示在显示，不打断）
        ├─ 查 m_show_notifications 白名单（option_name / "always"）
        ├─ if (应显示) {
        │     m_ui->Update(ctx, status)                    （把提示文字喂给 UI）
        │     if (m_show_notifications_time)
        │         m_ui->ShowWithTimeout(m_show_notifications_time)  （启动 AUTOHIDE_TIMER）
        │     return true
        │  } else return IsCountingDown()
  └─ _UpdateUI 末尾：lock 内 clear 四个缓冲              （消费确认）

[UI 侧] ShowWithTimeout 启动的定时器到期
  └─ OnTimer → KillTimer → Hide()                         （提示自动消失）
```

控制显示行为的两个配置（来自 `weasel.yaml`）：

| 配置键 | 成员 | 含义 | 默认 |
|---|---|---|---|
| `show_notifications` | `m_show_notifications` | `true` 表示所有通知都显示（内部记为 `"always"`）；或是一个选项名列表，只有列出的选项通知才显示 | `true` |
| `show_notifications_time` | `m_show_notifications_time` | 提示显示的毫秒数；`0` 表示不自动隐藏 | `1200` |

真实出厂配置见 [output/data/weasel.yaml:14-16](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/data/weasel.yaml#L14-L16)：

```yaml
show_notifications: true
show_notifications_time: 1200
```

#### 4.2.3 源码精读

`_ShowMessage` 的完整实现见 [RimeWithWeasel/RimeWithWeasel.cpp:670-729](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L670-L729)。逐段拆解。

**第 1 段：取锁 + 空消息短路。**

```cpp
bool RimeWithWeaselHandler::_ShowMessage(Context& ctx, Status& status) {
  std::lock_guard<std::mutex> lock(m_notifier_mutex);
  if (m_message_type.empty() || m_message_value.empty())
    return m_ui->IsCountingDown();
```

[空消息短路](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L671-L673) —— 若没有待处理通知，返回 `IsCountingDown()`：如果之前有个倒计时提示还在显示，就返回 `true`（告诉 `_UpdateUI`「别 Hide，让提示继续」）；否则返回 `false`（走正常候选路径）。这一行是「不打断正在显示的提示」的第一道关卡。

**第 2 段：按 type 决定提示内容。**

[deploy 分支：本地化的部署提示](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L677-L699) —— 根据 `message_value`（`start`/`success`/`failure`）和 `GetThreadUILanguage()` 选择简体/繁体/英文文案写入 `ctx.aux.str`（`tips`）。失败分支还会提示查看日志路径。

[schema 分支：提示就是方案名](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L700-L701) —— `tips = status.schema_name;`，即把当前方案名（如「明月拼音·简体」）作为提示文字。注意被注释掉的 `【】` 包裹——作者去掉了方括号，直接显示方案名。

[option 分支：开关切换](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L702-L713) —— 这是逻辑最复杂的一段：

```cpp
} else if (m_message_type == "option") {
    status.type = SCHEMA;
    if (m_message_value == "!ascii_mode") {
      show_icon = true;            // 切回中文：只刷图标，不显示文字
    } else if (m_message_value == "ascii_mode") {
      show_icon = true;            // 切到英文：只刷图标
    } else
      tips = u8tow(m_message_label);   // 其它开关：显示本地化标签

    if (m_message_value == "full_shape" || m_message_value == "!full_shape")
      status.type = FULL_SHAPE;    // 全/半角切换：更新对应状态类型
  }
```

要点：`ascii_mode`（中/英切换）**不弹文字提示**，只设 `show_icon = true` 让图标刷新（因为中英切换频率高、文字提示太吵）；其它开关（如方案内的自定义开关）才显示 `m_message_label`（由 `OnNotify` 经 `get_state_label` 取得的本地化标签，见 u4-l1 §4.3）。`full_shape` 切换会把 `status.type` 改成 `FULL_SHAPE`，配合图标呈现全/半角状态。

**第 3 段：去重与白名单裁决。**

[去重 + 白名单 + 超时显示](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L714-L728) —— 这是本讲最核心的一段：

```cpp
  auto counter = m_ui->IsCountingDown();
  if (!show_icon && counter)
    return counter;                    // (a) 有文字提示在显示，新的文字提示让位
  auto foption = m_show_notifications.find(m_option_name);
  auto falways = m_show_notifications.find("always");
  if ((!add_session && (foption != m_show_notifications.end() ||
                        falways != m_show_notifications.end())) ||
      m_message_type == "deploy") {    // (b) 白名单命中 或 deploy 消息
    m_ui->Update(ctx, status);
    if (m_show_notifications_time)
      m_ui->ShowWithTimeout(m_show_notifications_time);   // (c) 启动倒计时
    return true;
  } else {
    return m_ui->IsCountingDown();     // (d) 不在白名单：维持现状
  }
```

逐条解读：

- **(a) 不打断**：`show_icon` 为 `false`（即要显示文字提示）且当前已有倒计时提示在显示（`counter` 为真），直接返回 `true`，避免新提示覆盖旧的。`show_icon` 为 `true` 的中英切换不受此限制（图标刷新可以与文字提示并存）。
- **(b) 白名单**：`add_session` 是一个全局标志（见 [L66](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L66) 与 [L210-212](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L210-L212)），会话刚创建时为 `true`，用于抑制「欢迎消息」之类的提示；`!add_session` 表示「不是会话创建阶段」。`m_option_name` 是当前通知对应的选项名（仅 `option` 类型有值），若它或 `"always"` 在 `m_show_notifications` 白名单里，则允许显示。`deploy` 消息无条件显示（`|| m_message_type == "deploy"`），因为部署状态用户必须知晓。
- **(c) 超时**：`m_show_notifications_time` 非 0 才调 `ShowWithTimeout` 启动自动隐藏定时器；为 0 时只 `Update` 不启动定时器，提示会一直显示直到下一次 `Update` 用新内容顶掉它。
- **(d) 不在白名单**：返回 `IsCountingDown()` 维持现状，既不显示新提示，也不打扰已有提示。

`m_show_notifications` 白名单本身由 `_UpdateShowNotifications` 构建，见 [RimeWithWeasel/RimeWithWeasel.cpp:1128-1158](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L1128-L1158)：配置为 `true` 时写入 `"always"`；配置为列表时写入各个选项名；未配置时默认 `"always"`。它在 `Initialize`（全局）和 `_LoadSchemaSpecificSettings`（方案级）时都会被重算，所以白名单可以按方案定制——这正是 u4-l3 「方案专属配置」的一个具体落点。

**第 4 段：定时器自动隐藏（UI 侧）。**

`ShowWithTimeout` 的实现见 [WeaselUI/WeaselUI.cpp:61-70](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselUI.cpp#L61-L70)：

```cpp
void UIImpl::ShowWithTimeout(size_t millisec) {
  if (!panel.IsWindow()) return;
  panel.ShowWindow(SW_SHOWNA);
  shown = true;
  SetTimer(panel.m_hWnd, AUTOHIDE_TIMER, static_cast<UINT>(millisec),
           &UIImpl::OnTimer);
  timer = UINT_PTR(this);
}
```

它显示窗口并启动一个 `AUTOHIDE_TIMER`（常量值 `20121220`，见 [L32](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselUI.cpp#L32)）。到期后回调 [OnTimer](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselUI.cpp#L71-L83)，它 `KillTimer` 后调 `Hide()`，提示随之消失。`IsCountingDown()` 就是检查 `timer != 0`（[L144-146](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselUI.cpp#L144-L146)），即「是否正有一个这样的定时器在跑」。

**补充：_GetStatus 里还有一条独立的方案提示路径。** 除了经 `OnNotify` → `_ShowMessage`，切方案时 [_GetStatus 检测到 schema_id 变化](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L1451-L1470) 也会**直接**显示方案名：

```cpp
if (m_show_notifications.find("schema") != m_show_notifications.end() &&
    m_show_notifications_time > 0) {
  ctx.aux.str = stat.schema_name;
  m_ui->Update(ctx, stat);
  m_ui->ShowWithTimeout(m_show_notifications_time);
}
```

这条路径要求白名单里显式有 `"schema"` 且超时 `> 0`。它与 `_ShowMessage` 的 `schema` 分支在同一次 `_UpdateUI` 里可能先后触发：`_GetStatus` 先显示并启动倒计时，随后 `_ShowMessage` 看到 `IsCountingDown() == true`，命中第 (a) 条「不打断」直接返回 `true`，于是不会重复弹窗。这种「双路径 + 倒计时去重」的设计保证切方案提示只出现一次。

#### 4.2.4 代码实践

**实践目标**：追踪「切换输入方案」时通知从产生到显示再到自动消失的全过程，说清去重与超时机制。这也是本讲规格指定的实践任务。

**操作步骤**：

1. **注册点**：确认 `_Setup()` 里 `set_notification_handler(&RimeWithWeaselHandler::OnNotify, this)`（[L104](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L104)）。
2. **产生**：假想用户经候选菜单选了另一个方案。librime 内部完成方案切换后，会回调 `OnNotify(this, sid, "schema", "<新方案id>")`。阅读 [OnNotify](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L383-L406)，确认它把 `"schema"` 与方案 id 写入 `m_message_type`/`m_message_value`（`schema` 不进 `option` 分支，所以 `m_option_name` 不被改写）。
3. **采集**：下一次按键触发 `_UpdateUI` → `_GetStatus`。阅读 [_GetStatus 的方案切换分支](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L1451-L1470)：`schema_id != m_last_schema_id` 成立 → 重载方案样式 → 若 `"schema"` 在白名单且 `time>0`，直接 `Update`+`ShowWithTimeout` 显示方案名，**并启动 AUTOHIDE_TIMER**。
4. **去重**：紧接着 `_UpdateUI` 调 `_ShowMessage`（[L670-729](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L670-L729)）。读到 `m_message_type=="schema"` → `tips = status.schema_name`。然后 `counter = IsCountingDown()` 为 **true**（第 3 步刚启动了定时器），`show_icon` 为 false，命中 `if (!show_icon && counter) return counter;` → 返回 `true`，**不再重复显示**。这就是去重。
5. **超时**：约 `m_show_notifications_time`（默认 1200ms）后，[OnTimer](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselUI.cpp#L71-L83) 触发 `Hide()`，提示消失。
6. **清空**：回到第 3 步那次 `_UpdateUI` 的末尾，[清空缓冲](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L546-L552) 把 `m_message_type` 等清掉，保证后续按键不会重复弹同一条方案提示。

**需要观察的现象**：

- 切方案后，候选窗口位置短暂显示方案名，约 1.2 秒后自动消失。
- 若把 `weasel.yaml` 的 `show_notifications_time` 改成 `0`（**待本地验证**），第 3 步的 `if (m_show_notifications_time > 0)` 不成立，`_GetStatus` 不再直接显示；但 `_ShowMessage` 的 (c) 步骤 `if (m_show_notifications_time)` 也不成立，提示会 `Update` 后**常显**，直到下一次有内容的 `Update` 把它顶掉。
- 若把 `show_notifications` 改成列表（如 `[ascii_mode]`）且不含 `"schema"`（**待本地验证**），则 `_GetStatus` 的方案提示路径（要求白名单含 `"schema"`）不触发；`_ShowMessage` 里 `foption`/`falways` 都查不到，走 (d) 分支不显示——切方案不再弹提示。

**预期结果**：画出一张时序图，横轴是时间，标注 `OnNotify`(写) → `_GetStatus`(显示+启动定时器) → `_ShowMessage`(命中去重返回 true) → `OnTimer`(隐藏) → `_UpdateUI` 末尾(清空缓冲)，并在每一步标注所用的锁与定时器。

#### 4.2.5 小练习与答案

**练习 1**：为什么中/英切换（`ascii_mode`）默认不弹文字提示，只刷新图标？

**参考答案**：因为中英切换极其频繁（很多人每输入几句话就要切一次），如果每次都弹「中文/英文」文字提示会非常打扰。代码里 `ascii_mode` 与 `!ascii_mode` 两个分支都只设 `show_icon = true`、不写 `tips`，于是只触发图标刷新（经 `_RefreshTrayIcon` 与 `status.type`），不显示文字。而 `show_icon` 为 true 时还能绕过第 (a) 条「不打断」检查，避免与正在显示的其它提示互相阻塞。

**练习 2**：`m_show_notifications_time` 设为 `0` 时，提示行为有何不同？为什么？

**参考答案**：设为 0 时，`_ShowMessage` 的 (c) 步骤 `if (m_show_notifications_time)` 不成立，不会调 `ShowWithTimeout`，也就不启动 `AUTOHIDE_TIMER`。提示虽然经 `Update` 显示出来了，但**不会自动消失**，会一直挂到下一次 `_UpdateUI` 用新的（通常是空的）`weasel_context` 走 `Hide()+Update()` 把它隐藏。换句话说，`0` = 「常显直到下一次输入」。`_GetStatus` 的方案提示路径甚至用 `time > 0` 做了前置判断，time 为 0 时该路径完全不触发。

**练习 3**：`_ShowMessage` 第 (a) 条 `if (!show_icon && counter) return counter;` 解决了什么问题？去掉会怎样？

**参考答案**：它解决「连续多个文字提示互相覆盖」的问题。设想部署过程中 librime 连续推送 `deploy=start`、随后某开关变化推送一个 `option` 文字提示：如果不去重，第二个提示会立刻 `Update`+`ShowWithTimeout` 覆盖第一个，用户可能来不及看清「正在部署」。有了这条判断，只要前一个文字提示的倒计时还在（`counter` 为真），新的文字提示就让位、不刷新。去掉后，后到的提示会立即覆盖先到的，用户体验变差。注意它只约束「文字提示」（`!show_icon`），不约束「图标刷新」，所以中英切换仍能即时反映。

---

### 4.3 维护模式与深色主题：全局状态的重算

#### 4.3.1 概念说明

有两类事件会让 `RimeWithWeaselHandler` 做「全局重算」而不是「单会话更新」：

**维护模式（maintenance）。** 当用户从托盘点【重新部署】、或 `WeaselDeployer.exe` 启动时，用户词典和方案配置正在被另一个进程改写。此时引擎如果继续处理按键，可能读到半成品数据。Weasel 的做法是进入「维护模式」：`Finalize` 掉引擎、清空所有会话、把 `m_disabled` 置真，让所有按键直接放行（`ProcessKeyEvent` 首行 `if (m_disabled) return FALSE;`）。部署完成后再 `EndMaintenance` 重新 `Initialize` 引擎。这套机制在 u4-l1 §4.2 已提到互斥体与维护的关系，本讲聚焦它对 UI 与按键的实际影响。

**深色主题（dark mode）。** Windows 10/11 支持深色应用主题。Weasel 允许在 `weasel.yaml` 里分别配置 `style/color_scheme`（浅色）和 `style/color_scheme_dark`（深色）。当系统主题切换时，Weasel 需要重新读取配色并应用到所有已存在的会话。这个重算由 `UpdateColorTheme` 完成，触发者是系统主题变化消息。

两者共同点：都是**全局的、跨会话的**状态变更，完成后都调用 `_UpdateUI(0)`（无会话刷新）来让 UI 反映新状态。

#### 4.3.2 核心流程

**维护模式的进出：**

```text
[部署器/托盘] 经 IPC 发 WEASEL_IPC_START_MAINTENANCE
  └─ ServerImpl::OnStartMaintenance → handler->StartMaintenance()
        ├─ m_session_status_map.clear()      （丢弃所有会话）
        ├─ Finalize()                         （m_disabled=true；rime_api->finalize 销毁引擎）
        └─ _UpdateUI(0)                       （把 disabled 态透传给 UI）

[部署完成] 经 IPC 发 WEASEL_IPC_END_MAINTENANCE，或下一次 AddSession
  └─ EndMaintenance()
        ├─ if (m_disabled) { Initialize(); _UpdateUI(0); }  （重新拉起引擎）
        └─ m_session_status_map.clear()
```

维护期间，任何按键经 IPC 到达 `ProcessKeyEvent` 都因 `m_disabled` 直接返回 `FALSE`（不吃键，放行给应用）；`FindSession` 返回 0；`AddSession` 会尝试 `EndMaintenance` 自愈（[L167-172](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L167-L172)）。

**深色主题的重算：**

```text
[系统] 用户在 Windows 设置里切换深/浅色
  └─ 广播 WM_SETTINGCHANGE / WM_DWMCOLORIZATIONCOLORCHANGED
        └─ ServerImpl::OnColorChange（主线程消息循环）
              ├─ if (IsUserDarkMode() != m_darkMode):
              │     m_darkMode = IsUserDarkMode()
              └─ handler->UpdateColorTheme(m_darkMode)
                    ├─ 重读 weasel.yaml：_UpdateUIStyle(initialize=true)
                    ├─ m_current_dark_mode = darkMode
                    ├─ if (darkMode) 加载 color_scheme_dark
                    ├─ m_base_style = m_ui->style()        （更新全局基础样式）
                    ├─ 遍历每个已存在会话：重载其方案专属样式
                    │     （_LoadSchemaSpecificSettings + _LoadAppInlinePreeditSet + _UpdateInlinePreeditStatus）
                    └─ m_ui->style() = 当前活动会话的 style
```

`IsUserDarkMode()` 的判定见 [include/WeaselUtility.h:48-62](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUtility.h#L48-L62)：读取注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize` 下的 `AppsUseLightTheme`，值为 0 即深色。

#### 4.3.3 源码精读

**维护模式。** `StartMaintenance` / `EndMaintenance` 见 [RimeWithWeasel/RimeWithWeasel.cpp:475-487](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L475-L487)：

```cpp
void RimeWithWeaselHandler::StartMaintenance() {
  m_session_status_map.clear();
  Finalize();        // m_disabled=true；rime_api->finalize()
  _UpdateUI(0);      // ipc_id=0：把 disabled 透传给 UI
}

void RimeWithWeaselHandler::EndMaintenance() {
  if (m_disabled) {
    Initialize();    // 重新 initialize 引擎
    _UpdateUI(0);
  }
  m_session_status_map.clear();
}
```

它们经 IPC 命令触发，见 [WeaselIPCServer/WeaselServerImpl.cpp:295-308](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L295-L308)（`OnStartMaintenance`/`OnEndMaintenance` 调 handler 的对应方法）。`Finalize` 把 `m_disabled` 置真（[L149-155](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L149-L155)），于是维护期间所有 `ProcessKeyEvent`/`FindSession` 等都走禁用分支。注意 `AddSession` 开头有一段自愈逻辑（[L167-172](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L167-L172)）：若发现仍处于禁用态，先调 `EndMaintenance()` 尝试恢复，再继续——这保证部署完成后用户的下一次输入能自动唤醒引擎，无需手动重启 Server。

**深色主题的触发：消息映射。** `ServerImpl` 在消息映射里登记了两个主题相关消息，见 [WeaselIPCServer/WeaselServerImpl.h:28-29](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.h#L28-L29)：

```cpp
MESSAGE_HANDLER(WM_DWMCOLORIZATIONCOLORCHANGED, OnColorChange)
MESSAGE_HANDLER(WM_SETTINGCHANGE, OnColorChange)
```

`OnColorChange` 实现 [WeaselIPCServer/WeaselServerImpl.cpp:56-65](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L56-L65)：

```cpp
LRESULT ServerImpl::OnColorChange(UINT uMsg, WPARAM wParam, LPARAM lParam, BOOL& bHandled) {
  if (IsUserDarkMode() != m_darkMode) {
    m_darkMode = IsUserDarkMode();
    m_pRequestHandler->UpdateColorTheme(m_darkMode);
  }
  return 0;
}
```

注意它先比较 `IsUserDarkMode()` 与缓存的 `m_darkMode`（构造时初始化为 `IsUserDarkMode()`，见 [L33](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L33)），只有**真正变化**才触发重算——因为 `WM_SETTINGCHANGE` 可能在无关设置变化时也广播，这个去抖避免无谓的样式重载。

**UpdateColorTheme 的重算。** 见 [RimeWithWeasel/RimeWithWeasel.cpp:230-262](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L230-L262)：

```cpp
void RimeWithWeaselHandler::UpdateColorTheme(BOOL darkMode) {
  RimeConfig config = {NULL};
  if (rime_api->config_open("weasel", &config)) {
    if (m_ui) {
      _UpdateUIStyle(&config, m_ui, true);   // 重新加载完整基础样式
      m_current_dark_mode = darkMode;
      if (darkMode) {
        // ... 读 style/color_scheme_dark 并 _UpdateUIStyleColor 覆盖配色 ...
      }
      m_base_style = m_ui->style();          // 更新全局基础样式
    }
    rime_api->config_close(&config);
  }

  for (auto& pair : m_session_status_map) {  // 为每个已存在会话重算
    RIME_STRUCT(RimeStatus, status);
    if (rime_api->get_status(to_session_id(pair.first), &status)) {
      _LoadSchemaSpecificSettings(pair.first, std::string(status.schema_id));
      _LoadAppInlinePreeditSet(pair.first, true);
      _UpdateInlinePreeditStatus(pair.first);
      pair.second.status = status;
      pair.second.__synced = false;          // 标记样式需重发前端
      rime_api->free_status(&status);
    }
  }
  m_ui->style() = get_session_status(m_active_session).style;
}
```

这段与 `Initialize` 里初始化深色配色的逻辑（[L125-135](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L125-L135)）高度同构：都是先 `_UpdateUIStyle(initialize=true)` 打底，再按 `m_current_dark_mode` 决定是否叠加 `color_scheme_dark`，最后存入 `m_base_style`。区别在于 `UpdateColorTheme` 多了「遍历每个会话重算方案样式」这一步——因为每个会话可能用了不同方案、不同方案专属配色（见 u4-l3），切换深浅色后必须把每个会话的配色也按新主题重算一遍。重算后置 `__synced = false`，这样下一次 `_Respond` 会把新样式经 `style=` 整块序列化重发给前端（u2-l4、u4-l2），前端拿到新配色后重绘。

#### 4.3.4 代码实践

**实践目标**：观察深色主题切换如何驱动配色重算，并理解维护模式对按键的影响。

**操作步骤**：

1. **触发链**：从 [WeaselServerImpl.h:28-29](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.h#L28-L29) 的消息映射出发，确认系统主题切换广播 `WM_SETTINGCHANGE` → `OnColorChange` → `UpdateColorTheme`。
2. **去抖**：阅读 [OnColorChange](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L56-L65)，说明为什么用 `IsUserDarkMode() != m_darkMode` 做条件——`WM_SETTINGCHANGE` 在很多无关场景（改壁纸、改区域等）都会广播，若不去抖会反复重载样式。
3. **重算影响面**：在 [UpdateColorTheme](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L230-L262) 的 `for` 循环处，列出每个会话被重算时调用的三个函数（`_LoadSchemaSpecificSettings` / `_LoadAppInlinePreeditSet` / `_UpdateInlinePreeditStatus`），并解释 `__synced = false` 的作用（提示：联系 u4-l2 的 `_Respond` 里 `if (!session_status.__synced)` 分支）。
4. **维护模式影响**：阅读 [StartMaintenance](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L475-L479) 与 [ProcessKeyEvent 的禁用守卫](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L269-L270)，说明维护期间按键为何「直接放行」。
5. （**待本地验证**）在 Windows 上配置 `weasel.yaml` 同时给出 `color_scheme` 与 `color_scheme_dark`，在系统设置里切换深浅色，观察候选窗口配色是否即时变化；从托盘触发【重新部署】，观察部署期间按键是否被放行（直接上屏英文而不进入拼音）。

**需要观察的现象**：

- 切换系统深浅色后，无需重启 WeaselServer，候选窗口配色立即切换。
- 部署期间，按字母键直接输出英文字母（`ProcessKeyEvent` 返回 `FALSE`，按键透传给应用）。
- 部署完成后第一次输入会正常进入拼音（`AddSession` 的自愈逻辑触发 `EndMaintenance`）。

**预期结果**：能用一句话说清「为什么 Weasel 不重启就能跟随系统深色模式」——`OnColorChange` 侦测到主题变化后调 `UpdateColorTheme`，重读配色并为每个会话重算样式，置 `__synced=false` 让下一次响应把新样式推给前端。

#### 4.3.5 小练习与答案

**练习 1**：`OnColorChange` 为什么不直接调 `UpdateColorTheme(IsUserDarkMode())`，而要先比较 `m_darkMode`？

**参考答案**：因为 `WM_SETTINGCHANGE` 和 `WM_DWMCOLORIZATIONCOLORCHANGED` 并不只在实际主题切换时才到达——很多无关的系统设置变化也会广播这些消息。如果不比较缓存值就直接重算，会频繁触发 `_UpdateUIStyle`、遍历所有会话等较重的操作，浪费性能。用 `IsUserDarkMode() != m_darkMode` 做「真正的亮/暗翻转」判定，只在深浅色确实切换时才重算，是一种去抖优化。

**练习 2**：`UpdateColorTheme` 末尾把每个会话的 `__synced` 置 `false`，这个标志在何时、由谁消费？

**参考答案**：由 `_Respond` 消费。回顾 u4-l2 / u4-l3：`_Respond` 里有 `if (!session_status.__synced) { ... 序列化 style ...; session_status.__synced = true; }`，只有 `__synced` 为假时才把整份 `UIStyle` 经 `style=` 行发给前端，并在发送后置真。`UpdateColorTheme` 重算样式后置 `__synced=false`，就是为了强制下一次按键的 `_Respond` 把新配色（整块 boost 序列化）重发给前端，前端 `Styler` 反序列化后重绘。这是一种「样式惰性同步」机制，避免每次响应都重发庞大的 `UIStyle`。

**练习 3**：维护期间用户一直在按键盘，为什么不会卡住？

**参考答案**：因为 `ProcessKeyEvent` 第一行就是 `if (m_disabled) return FALSE;`，维护期间 `m_disabled` 为真，按键直接返回「不吃键」，前端 `*pfEaten` 为假，按键原样透传给应用（直接上屏）。引擎根本没被调用，所以不会卡。等部署完成，`EndMaintenance` 重新 `Initialize` 引擎并把 `m_disabled` 置回 `false`（经 `AddSession` 的自愈路径），输入恢复正常。

---

## 5. 综合实践

**任务**：制作一张「`_UpdateUI` 全景决策图」，把本讲三个模块串成一张可长期保存的速查图，用来回答「任意一次 `_UpdateUI` 调用，候选窗口最终会怎样显示」。

**要求**：

1. **主轴是一次 `_UpdateUI(ipc_id)` 调用**，从上到下画出它的 7 个步骤（守卫 → 取 status/建空 context → ipc_id==0 透传 disabled → `_GetStatus` → 设 client_caps → `_ShowMessage` 分叉 → 清空缓冲），每步标注源码行号（[L518-553](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L518-L553)）。
2. **在 `_ShowMessage` 分叉处展开一棵决策树**，覆盖以下分支并各举一个真实场景：
   - 空消息 → `return IsCountingDown()`（普通打字，无通知）；
   - `!show_icon && counter` → 返回 true，不打断（切方案提示显示中又来一条 option 文字提示）；
   - 白名单命中 / `deploy` → `Update` + `ShowWithTimeout`（切方案、部署开始）；
   - 不在白名单 → 维持现状（`show_notifications` 列表未包含该选项）。
3. **用一条侧链画「通知的来源」**：`OnNotify`（线程，写缓冲）与 `_GetStatus`（主线程，方案切换直接显示）两条路径如何汇入 `_ShowMessage`，标注它们对 `m_notifier_mutex` 的「写/读/清」三态。
4. **用两条虚线画「全局重算」**：
   - 维护模式：`StartMaintenance` → `Finalize` → `_UpdateUI(0)`，以及 `m_disabled` 对 `ProcessKeyEvent` 的影响；
   - 深色主题：`WM_SETTINGCHANGE` → `OnColorChange` → `UpdateColorTheme` → 每会话 `__synced=false` → 下次 `_Respond` 重发 `style=`。
5. **在图边写三条「踩坑提醒」**：
   - `weasel_context` 每次新建为空，上屏后必须靠 `Hide()` 隐藏窗口；
   - `m_show_notifications_time=0` 表示常显而非不显示；
   - `OnColorChange` 必须用 `m_darkMode` 去抖，否则无关 `WM_SETTINGCHANGE` 会反复重算。

**验收标准**：拿着这张图，你能不查源码就回答以下三题——

- 「切方案时弹的提示，是 `OnNotify` 触发的还是 `_GetStatus` 触发的？会不会重复弹？」（答：两者都可能触发，但 `_ShowMessage` 的「不打断」去重保证只显示一次。）
- 「为什么我把 `show_notifications_time` 改成 0 后，切方案提示一直不消失？」（答：`ShowWithTimeout` 未被调用，无定时器，提示常显直到下一次 `Update` 顶掉。）
- 「系统切深色后，已打开的多个应用里候选窗口配色都会变吗？」（答：会，`UpdateColorTheme` 遍历每个会话重算并 `__synced=false`，下次按键各自重发新样式。）

## 6. 本讲小结

- `_UpdateUI` 是 Weasel 输出侧的**唯一推送漏斗**：几乎所有 IPC 命令都在末尾调它，它采集 `Status`/`Context`、刷新 `inline_preedit` 能力位、用 `_ShowMessage` 的返回值在「正常候选显示（`Hide`+`Update`）」与「瞬时提示」间分叉，并在末尾清空通知缓冲。
- `UI::Update` 内置 `ctx_ == ctx && status_ == status` 的内容去重，避免每按键都重绘；这也是「上屏后窗口消失」的实现机制（空 `weasel_context` 触发 `Hide`）。
- 引擎通知经 `OnNotify`（写静态缓冲）→ `_ShowMessage`（锁内读 + 翻译 + 裁决）→ `ShowWithTimeout`（启动 `AUTOHIDE_TIMER`）→ `OnTimer`（到期 `Hide`）完成「一闪而过」的提示，整条链路用 `m_notifier_mutex` 保证线程安全（u4-l1 §4.3 的延续）。
- 提示的显示受双重控制：`m_show_notifications`（白名单：`true`→`"always"`，或选项名列表）决定「哪些通知显示」，`m_show_notifications_time`（默认 1200ms，`0`=常显）决定「显示多久」；`deploy` 消息总是显示；`ascii_mode` 只刷图标不显文字。
- `_ShowMessage` 内置「不打断」去重（`!show_icon && counter`）与 `_GetStatus` 的方案提示路径相互配合，保证连续通知/切方案提示不重复、不互相覆盖。
- 维护模式（`StartMaintenance`/`EndMaintenance`）通过 `Finalize`+`m_disabled=true` 让引擎重建期间按键直接放行，`AddSession` 自愈逻辑保证部署完成后第一次输入自动唤醒引擎。
- 深色主题（`UpdateColorTheme`）由 `OnColorChange`（`WM_SETTINGCHANGE`/`WM_DWMCOLORIZATIONCOLORCHANGED`）触发，经 `m_darkMode` 去抖后重读 `color_scheme(_dark)`、更新 `m_base_style`，并为每个会话重算方案样式、置 `__synced=false` 让下次响应重发新配色。

## 7. 下一步学习建议

本讲把 `RimeWithWeaselHandler`（即 Rime 引擎桥接层）的「输出侧」彻底收尾。至此 u4 单元（引擎桥接）完结。建议下一步：

- **u5-l1 WeaselPanel 窗口、分层与交互**：本讲反复提到的 `Show`/`Hide`/`ShowWithTimeout`/`Update`/`Refresh` 都落在 `WeaselPanel` 这个分层窗口上。去读它的消息映射、双缓冲绘制、鼠标交互，理解「提示文字」到底是怎么画出来的。
- **u5-l3 DirectWrite 资源与文本绘制**：深色主题切换后配色能即时生效，最终落在 DirectWrite 的画刷上。去读 `DirectWriteResources` 如何根据 `UIStyle` 的颜色字段创建画刷，把本讲的「样式重算」与「实际绘制」接上。
- **u6-l1 WeaselDeployer 配置器**：本讲的维护模式由 `WeaselDeployer.exe` 触发，去读部署器如何持有 `WeaselDeployerMutex`、经 IPC 让 Server 进出维护，把「重新部署」的完整闭环看清。
- **u7-l3 配色方案与样式定制实战**：本讲提到了 `color_scheme` / `color_scheme_dark` / `m_show_notifications` 等配置，去那一讲动手写一份 `weasel.custom.yaml`，把本讲的理论变成可调试的真实配置。
- **延伸阅读**：在浏览器打开 [output/data/weasel.yaml](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/data/weasel.yaml)，对照本讲找到 `show_notifications`、`show_notifications_time`、`color_scheme`、`preset_color_schemes` 各段，理解它们如何驱动本讲所述的三条链路。
