# 会话管理与按键处理

## 1. 本讲目标

本讲聚焦 `RimeWithWeaselHandler` 里最核心、最高频的一条链路：**一个按键到达 Server 后，如何被交给 librime 处理，又如何把结果回写给前端并刷新 UI**。

学完后你应该能够：

- 说清 `WeaselSessionId`（IPC 层会话号）与 `RimeSessionId`（librime 引擎会话号）这两套 ID 为什么存在、如何互相映射。
- 画出 `ProcessKeyEvent` 从入口到 `_Respond`/`_UpdateUI` 的完整主链路，标出每一步调用的 `rime_api` 函数。
- 解释 `_Respond` 如何把 librime 的 `RimeCommit`/`RimeStatus`/`RimeContext` 三块产出，拼成 u2-l5 中 `ResponseParser` 能消费的「行协议」文本，并通过 `EatLine` 回写管道。
- 理解 `_ReadClientInfo` 在 `AddSession` 时读取客户端应用名与能力、设置 per-app 选项的作用。

本讲是 u4 单元的「中枢」：u4-l1 讲了引擎如何初始化，本讲讲引擎如何「被使用」，u4-l3、u4-l4 再讲配置加载与 UI 更新细节。

## 2. 前置知识

阅读本讲前，你需要具备以下认知（已在依赖讲义中建立）：

- **多进程与 IPC（u1-l1、u2-l1～u2-l3）**：WeaselTSF（`weasel.dll`，驻留每个应用进程）是瘦客户端，按键经命名管道以 `WEASEL_IPC_PROCESS_KEY_EVENT` 命令发给 WeaselServer；Server 端的 `ServerImpl::HandlePipeMessage` 把命令派发给 `RequestHandler` 的虚函数。`RequestHandler` 是抽象基类，`RimeWithWeaselHandler` 是它唯一的生产实现。
- **`EatLine` 回调（u2-l1、u2-l3）**：`RequestHandler` 的若干方法带一个 `EatLine eat` 参数，类型是 `std::function<bool(std::wstring&)>`。它的作用是「把一行文本写回管道缓冲，交给客户端读取」。`_Respond` 就靠它把响应正文一行行回写。
- **响应行协议（u2-l4、u2-l5）**：Server 回写给客户端的文本遵循「首行 `action=a,b,c` 声明本响应包含哪些动作 → 若干 `key=value` 正文行 → 单独一行 `.` 表示结束」的协议；客户端 `ResponseParser` 据此分发到 `Committer`/`StatusUpdater`/`ContextUpdater`/`Styler` 等反序列化器。`UIStyle` 与 `CandidateInfo` 走 boost 文本归档，整体编成一行。
- **librime C API（u4-l1）**：引擎交互收口于一张函数指针表 `RimeApi* rime_api`（文件级静态变量，构造时由 `rime_get_api()` 取得）。会话相关调用形如 `create_session`/`find_session`/`destroy_session`/`process_key`/`get_commit`/`get_status`/`get_context`，均以 `RimeSessionId` 为第一参数。
- **`weasel::KeyEvent`（u3-l2、KeyEvent.h）**：一个 32 位位域结构，低 16 位 `keycode`（ibus 键码）、高 16 位 `mask`（修饰键），作为普通整数经 IPC 零拷贝传输。

一个关键直觉：**librime 不知道也不关心 Weasel 的 IPC**。它只认 `RimeSessionId`；而 IPC 层只认 `WeaselSessionId`（一个 `DWORD`）。`RimeWithWeaselHandler` 的会话管理本质就是在这两套 ID 之间做翻译，并维护每个会话的「缓存状态」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [include/RimeWithWeasel.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h) | `RimeWithWeaselHandler` 类声明；`SessionStatus`/`SessionStatusMap`/`WeaselSessionId` 类型定义；`to_session_id`/`get_session_status` 等内联映射 helper。 |
| [RimeWithWeasel/RimeWithWeasel.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp) | 本讲主战场：`AddSession`/`RemoveSession`/`FindSession`/`ProcessKeyEvent`/`CommitComposition`/`_Respond`/`_ReadClientInfo`/`_UpdateUI` 全部实现。 |
| [include/WeaselIPC.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h) | `RequestHandler` 抽象基类与 `EatLine` 类型定义，是本讲所有虚函数的「合同」。 |
| [include/KeyEvent.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/KeyEvent.h) | `weasel::KeyEvent` 位域结构与 `ibus::Modifier`/`ibus::Keycode` 枚举。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**会话 ID 映射与状态表**、**按键处理主链路**、**`_Respond` 结果回传**。

### 4.1 会话 ID 映射与状态表

#### 4.1.1 概念说明

为什么要两套会话 ID？

- **`RimeSessionId`** 是 librime 内部的会话句柄（`uintptr_t`），由 `rime_api->create_session()` 返回，承载一份输入状态机（当前方案、写作串、候选页、各项开关）。librime 的所有按键/查询 API 都认它。
- **`WeaselSessionId`**（别名 `DWORD`，见头文件 `typedef DWORD WeaselSessionId;`）是 **Weasel IPC 层**的会话号。它在管道协议里充当 `wParam`/`lParam`，用于让 Server 知道「这次按键来自哪个前端会话」。

二者不能合并，因为：前端进程（每个应用的 `weasel.dll`）只跟 Server 打过交道、只认 Server 给它的 `WeaselSessionId`；而真正干活的是 librime，它只认 `RimeSessionId`。Server 必须在内部维护一张「IPC 号 ↔ 引擎号」的翻译表，并在每个 IPC 号下缓存该会话的样式与状态，避免每次按键都重新向 librime 拉取全量信息。

这张表就是 `SessionStatusMap`，它的值类型 `SessionStatus` 还兼任「每会话缓存」。

#### 4.1.2 核心流程

会话的完整生命周期：

```
前端首次连入
  └─ Server: AddSession(buffer, eat)
       ├─ rime_api->create_session()          → RimeSessionId
       ├─ _GenerateNewWeaselSessionId(...)    → WeaselSessionId (ipc_id)
       ├─ new_session_status(ipc_id)          → 在 map 中建条目
       │     └─ 记 session_id / 拷贝 m_base_style / __synced=false
       ├─ _ReadClientInfo(ipc_id, buffer)     → 读应用名、套用 per-app 选项
       ├─ 读 status → _LoadSchemaSpecificSettings / _LoadAppInlinePreeditSet
       ├─ 若有 eat → _Respond(ipc_id, eat)     → 回写欢迎/初始响应
       └─ _UpdateUI(ipc_id) / m_active_session = ipc_id

后续每次按键
  └─ Server: ProcessKeyEvent(keyEvent, ipc_id, eat)
       └─ session_id = to_session_id(ipc_id)   → 查表翻译
       └─ rime_api->process_key(session_id, ...)

前端断开
  └─ Server: RemoveSession(ipc_id)
       ├─ rime_api->destroy_session(to_session_id(ipc_id))
       └─ m_session_status_map.erase(ipc_id)
```

翻译动作由三个内联 helper 完成，它们都直接对 `m_session_status_map` 操作：

- `to_session_id(ipc_id)` → 返回 `m_session_status_map[ipc_id].session_id`（IPC 号翻译成引擎号）。
- `get_session_status(ipc_id)` → 返回该会话的 `SessionStatus&` 引用（读写缓存）。
- `new_session_status(ipc_id)` → 在表中插入一个默认构造的 `SessionStatus` 并返回引用（建条目）。

#### 4.1.3 源码精读

**`SessionStatus` 与映射表的定义** —— 注意它缓存了哪些字段：

[include/RimeWithWeasel.h:25-35](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h#L25-L35) 定义了 `SessionStatus{ UIStyle style; RimeStatus status; bool __synced; RimeSessionId session_id; }`、`SessionStatusMap = std::map<DWORD, SessionStatus>` 与 `WeaselSessionId = DWORD`。其中 `__synced` 标记「样式是否已经同步给前端」，是 `_Respond` 决定要不要重发 `style=` 的关键（见 4.3）。

**三个翻译 helper**：

[include/RimeWithWeasel.h:88-96](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h#L88-L96) 是 `to_session_id` / `get_session_status` / `new_session_status`。它们都直接索引 `m_session_status_map`——注意 `to_session_id` 用 `operator[]`，意味着对一个不存在的 `ipc_id` 调用会**隐式创建**一个空条目（`session_id` 为 0）。正因如此，所有公共方法在调用 `to_session_id` 前，都必须先确保 `ipc_id` 确实在表里（由 `AddSession` 建好）。

**`WeaselSessionId` 的生成 —— 基于 Server PID 的高位平移**：

[RimeWithWeasel/RimeWithWeasel.cpp:27-31](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L27-L31) 是 `_GenerateNewWeaselSessionId`：表空时返回 `pid + 1`，否则返回 `sm.rbegin()->first + 1`（当前最大键 +1，因为 `std::map` 按 key 升序，`rbegin()->first` 即最大键）。这是一个单调递增、不回收的分配策略。

而 `pid` 并非原始进程号，它在构造函数里被「高位平移」过：

[RimeWithWeasel/RimeWithWeasel.cpp:48-56](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L48-L56) 取 Server 进程号 `GetCurrentProcessId()`，找出其最高置位比特位置 `msbit`，再左移 `(31 - msbit)` 位，把这个比特顶到第 31 位。

设原始 PID 的最高置位比特在第 \(k\) 位，则平移后的基数为：

\[
\text{base} = \text{PID} \times 2^{\,31-k}
\]

其效果是把 PID 整体搬到接近第 31 位的「高区位」，随后 `+1, +2, ...` 递增出的 `WeaselSessionId` 都落在这个高位区间。这是一种让 IPC 层会话号与 librime 从 1 起步的小整数 `RimeSessionId` 在数值上明显区分、且单次 Server 生命周期内基本唯一的启发式策略（代码无注释，此处据代码行为描述，确切设计意图待确认）。

**`FindSession` —— 存活探测**：

[RimeWithWeasel/RimeWithWeasel.cpp:157-164](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L157-L164) 先用 `m_disabled` 守卫，再调 `rime_api->find_session(to_session_id(ipc_id))` 问 librime「这个引擎会话还在吗」。注意它的返回值约定：找到则**原样返回 `ipc_id`**（让前端继续用这个 IPC 号），找不到或禁用时返回 `0`。这正是 u2-l3 提到 `Client::Echo()`/会话存活探测在 Server 侧的落点。

**`AddSession` —— 建会话并初始化缓存**：

[RimeWithWeasel/RimeWithWeasel.cpp:166-215](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L166-L215) 是最完整的建会话流程。关键步骤：

1. 若 `m_disabled`（维护中），先尝试 `EndMaintenance()` 恢复，仍禁用则返回 0（拒绝建会话）。
2. `rime_api->create_session()` 拿到 `RimeSessionId`；若开启 `m_global_ascii_mode`，从已有会话里继承中/英状态。
3. `_GenerateNewWeaselSessionId` 生成 `ipc_id`，`new_session_status(ipc_id)` 建表项，写入 `session_id` 并把全局 `m_base_style` 拷给该会话。
4. `_ReadClientInfo(ipc_id, buffer)` 解析前端传来的应用名、套用 per-app 选项（见 4.1.3 末尾与 u4-l3）。
5. 读 librime `get_status` → 据当前 `schema_id` 加载方案专属样式与 inline preedit 设置。
6. 若带 `eat`，调 `_Respond(ipc_id, eat)` 把初始响应（含 style）回写前端。
7. 置全局 `add_session = true`（见 4.1.4 说明）后 `_UpdateUI(ipc_id)`，再复位、记录 `m_active_session`。

**`RemoveSession` —— 销毁与清理**：

[RimeWithWeasel/RimeWithWeasel.cpp:217-228](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L217-L228) 先 `m_ui->Hide()` 收起候选窗，再 `rime_api->destroy_session` 销毁引擎会话、`m_session_status_map.erase(ipc_id)` 删表项、清空 `m_active_session`。代码注释里的 `TODO: force committing?` 表明：若前端断开时还有未上屏的写作串，当前实现会直接丢弃。

**`_ReadClientInfo` —— 读应用名与能力**：

[RimeWithWeasel/RimeWithWeasel.cpp:408-448](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L408-L448) 以 `wbufferstream` 逐行解析前端在 `START_SESSION` 正文里写来的请求文本，识别 `session.client_app=<应用名>`（小写化、转 UTF-8）。拿到 `app_name` 后：用 `rime_api->set_property(session_id, "client_app", ...)` 把应用名登记进引擎（供 `_RefreshTrayIcon` 等使用），再查 `m_app_options` 表，若该应用有 per-app 选项就逐条 `rime_api->set_option` 套用（例如让某游戏默认英文）。最后据会话样式设置 `inline_preedit` 与 `soft_cursor` 两个引擎开关——这正是 u4-l3「AppOptions」与「inline preedit」在本讲的落点。

#### 4.1.4 代码实践

**实践目标**：用一次 `AddSession` 的源码追踪，建立「IPC 号 ↔ 引擎号 ↔ 缓存」三者的对应关系，并理解 `__synced` 标志的作用。

**操作步骤**（源码阅读型实践）：

1. 打开 [RimeWithWeasel/RimeWithWeasel.cpp:166-215](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L166-L215) 的 `AddSession`。
2. 准备一张三列表格：`WeaselSessionId(ipc_id)` | `RimeSessionId(session_id)` | `SessionStatus 缓存字段(style/status/__synced)`。
3. 假设 Server PID 平移后 `m_pid = 0x80000000`，这是建表后的第一个会话。按代码填入：
   - `create_session()` 假设返回 `session_id = 1`。
   - `_GenerateNewWeaselSessionId` 因表空 → `ipc_id = 0x80000001`。
   - `new_session_status` 后：`style = m_base_style`、`session_id = 1`、`__synced = false`（默认构造）。
4. 继续看 `_Respond` 在 `AddSession` 末尾被调用（行 207-209），结合 4.3 找到它会因 `__synced == false` 而**首次发送 `style=` 整块**并把 `__synced` 置 `true`。
5. 再假设紧接着前端发了第二个 `AddSession`（另一应用进程），重做一遍：此时表非空，`ipc_id = 0x80000002`，`session_id = 2`。

**需要观察的现象**：第二个会话与第一个会话的 `ipc_id`、`session_id` 各自独立递增；两个会话的 `style` 都来自同一份 `m_base_style`，但可在 `_LoadSchemaSpecificSettings` 后各自分化。

**预期结果**：你应得到一张清晰的映射表，并理解 `__synced` 让样式只在「首次响应」或「方案切换后」整块重发，避免每次按键都重传约 80 个字段的 `UIStyle`。

> 说明：本实践为源码阅读型，无需编译运行；若要在真实运行中观察这些值，可临时在 `AddSession` 的 `DLOG(INFO)` 行后追加一条日志打印 `ipc_id`/`session_id`，但**不要提交该改动到源码**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `to_session_id` 用 `m_session_status_map[ipc_id]`（`operator[]`）而不是 `.at(ipc_id)`？如果对一个未 `AddSession` 过的 `ipc_id` 调用 `ProcessKeyEvent` 会发生什么？

**答案**：`operator[]` 在 key 不存在时会插入默认值（`session_id` 为 0）。若对一个未建表项的 `ipc_id` 调 `ProcessKeyEvent`，`to_session_id` 会返回 `0`，随后 `rime_api->process_key(0, ...)` 作用于一个无效会话号，librime 返回未处理（`handled=false`），按键被放行。这正是「防御性默认」：不会崩溃，但按键不生效。生产中由 `AddSession` 保证表项先于按键建立。

**练习 2**：`_GenerateNewWeaselSessionId` 为什么用「最大键 +1」而不是「`pid + 表大小`」？

**答案**：用 `sm.rbegin()->first + 1`（当前最大键 +1）能保证即便有过 `RemoveSession` 删除中间项，新号也绝不与现存或近期回收的号冲突，是单调不回收策略；而「`pid + 表大小`」在频繁增删后会与已分配号重叠，造成歧义。

---

### 4.2 按键处理主链路

#### 4.2.1 概念说明

`ProcessKeyEvent` 是整个输入法最高频的入口：前端每按一次键（去重后），就经 IPC 触发一次。它的职责很纯粹——**把按键翻译给 librime、把 librime 的产出回写给前端、刷新 UI**。它本身不做任何「算字」逻辑，算字全在 librime 里。

理解它要抓住三个要点：

1. **`handled` 的语义**：`rime_api->process_key` 返回 `Bool`，表示「引擎是否消费了这个键」。这个值最终被 `ProcessKeyEvent` 原样作为 `BOOL` 返回，回传到客户端后变成 u3-l2 里的 `*pfEaten`——`TRUE` 表示「吃掉」（拦截，不传给应用），`FALSE` 表示「放行」（透传给应用文档）。所以「上屏文字」并不是这个返回值，而是通过 `_Respond` 里的 `commit=` 正文回传的。
2. **修饰键位再映射**：`weasel::KeyEvent.mask` 是 16 位紧凑掩码，而 librime 沿用 ibus 的 32 位修饰键布局，需要 `expand_ibus_modifier` 做位扩展。
3. **vim 模式补丁**：当引擎未处理某键且该键是「Escape 或 Ctrl+[ / c / C」时，若开启了 `vim_mode` 且当前非英文，自动切到 `ascii_mode`——这是 Rime 双拼/拼音方案里「按 Esc 回到命令模式即转英文」的来源。

#### 4.2.2 核心流程

```
ProcessKeyEvent(keyEvent, ipc_id, eat)
  ├─ if (m_disabled) return FALSE;              // 维护中直接放行
  ├─ session_id = to_session_id(ipc_id);        // IPC 号 → 引擎号
  ├─ handled = rime_api->process_key(
  │       session_id,
  │       keyEvent.keycode,
  │       expand_ibus_modifier(keyEvent.mask)); // 位扩展后的修饰键
  ├─ if (!handled && 不是键抬起):               // 仅 keydown 触发 vim 补丁
  │     if (Escape 或 Ctrl+[ / c / C) 且
  │         get_option("vim_mode") 且
  │         !get_option("ascii_mode"))
  │         set_option("ascii_mode", True)
  ├─ _Respond(ipc_id, eat);                     // 拼响应正文、回写管道
  ├─ _UpdateUI(ipc_id);                         // 刷新候选窗/状态
  ├─ m_active_session = ipc_id;
  └─ return (BOOL)handled;                      // → 客户端 pfEaten
```

`expand_ibus_modifier` 的位运算：

\[
\text{out} = (m\ \&\ \text{0xff})\ \big|\ ((m\ \&\ \text{0xff00}) \ll 16)
\]

即低 8 位（`SHIFT/LOCK/CONTROL/ALT/MODn` 等基础修饰，见 `ibus::Modifier`）保持原位，高字节（第 8～15 位，含 `HANDLED/IGNORED/SUPER/HYPER/META/RELEASE` 等）整体搬到第 24～31 位。这样 weasel 的 16 位紧凑掩码就被铺开成 librime 期望的 32 位布局。

`CommitComposition` / `ClearComposition` 是同形态的简化版：直接调对应 `rime_api` 函数，再 `_UpdateUI`、记 `m_active_session`，但不带 `eat`、不回写正文（上屏走前端 TSF 文档写入，见 u3-l3）。

#### 4.2.3 源码精读

**主链路 `ProcessKeyEvent`**：

[RimeWithWeasel/RimeWithWeasel.cpp:264-292](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L264-L292) 即上述流程的源码。注意几个细节：

- 行 269-270：`m_disabled` 时直接 `return FALSE`——维护模式下按键一律放行给应用，等同于「输入法临时不工作」。
- 行 272-273：核心调用 `rime_api->process_key`，第三参数是 `expand_ibus_modifier(keyEvent.mask)`。
- 行 275：`!(keyEvent.mask & ibus::Modifier::RELEASE_MASK)` 判断「不是键抬起」。`RELEASE_MASK = 1<<14`（KeyEvent.h），用它区分 keydown/keyup，确保 vim 补丁只在按下时触发。
- 行 288-289：`_Respond` 与 `_UpdateUI` 的顺序——**先回写客户端，再刷新 UI**。这个顺序很重要：客户端拿到 `commit=` 后即可上屏，UI 同时显示新候选。
- 行 291：`return (BOOL)handled`，这是吃键/放行的唯一信号。

**`expand_ibus_modifier` 位扩展**：

[RimeWithWeasel/RimeWithWeasel.cpp:33-35](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L33-L35) 一行位运算实现上述公式。

**vim 模式补丁**：

[RimeWithWeasel/RimeWithWeasel.cpp:275-287](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L275-L287) 判定 `isVimBackInCommandMode`：键码是 `Escape`，或（带 Ctrl 修饰 `keyEvent.mask & (1<<2)`，即 `CONTROL_MASK`）且键码是 `XK_c`/`XK_C`/`XK_bracketleft`（对应 `[`）。命中且方案开了 `vim_mode`、当前非 `ascii_mode` 时，`set_option("ascii_mode", True)`。

**同族方法 `CommitComposition` / `ClearComposition`**：

[RimeWithWeasel/RimeWithWeasel.cpp:294-310](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L294-L310) 两者结构一致：守卫 → `rime_api->commit_composition`/`clear_composition` → `_UpdateUI` → 记活跃会话。它们由 `WEASEL_IPC_COMMIT_COMPOSITION` / `WEASEL_IPC_CLEAR_COMPOSITION` 命令触发（u2-l1），用于前端在某些场景（如焦点切换、用户主动确认）强制结束写作串。

#### 4.2.4 代码实践

**实践目标**：追踪一个普通字母键 `n`（假设当前为拼音方案、中文模式）从 `ProcessKeyEvent` 入口到返回的全过程，标出每一步的 `rime_api` 调用与变量变化。

**操作步骤**（源码追踪型实践）：

1. 设定输入：`keyEvent.keycode = 'n' (0x6e)`，`keyEvent.mask = 0`（无修饰键），`ipc_id = 0x80000001`，对应 `session_id = 1`，`m_disabled = false`。
2. 在 [RimeWithWeasel/RimeWithWeasel.cpp:264-292](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L264-L292) 逐步标注：
   - 行 269：`m_disabled` 为 false，不返回。
   - 行 271：`session_id = 1`。
   - 行 272-273：`expand_ibus_modifier(0) = 0`，`handled = rime_api->process_key(1, 0x6e, 0)`，引擎把 `n` 加入写作串，返回 `True`（消费）。
   - 行 275-287：`handled` 为 true，跳过 vim 分支。
   - 行 288：`_Respond` 会拼出 `action=status,ctx,config`（此时无 `commit`，因为还没成词；有 `ctx` 因为 `is_composing=true`），见 4.3。
   - 行 289：`_UpdateUI` 刷新候选窗。
   - 行 291：返回 `TRUE` → 客户端 `*pfEaten = TRUE`，按键被拦截（不写入应用文档，而是显示在候选窗 preedit 里）。
3. 再追踪一次「数字键 `1` 选词」：设此时已有候选页，`keycode = '1'`。`process_key` 返回 `True`（引擎识别为选词），`_Respond` 这次会带 `commit=`（上屏词）+ `ctx`（候选清空）。

**需要观察的现象**：两次按键的 `action=` 头不同——纯输入字母时无 `commit`，选词确认时有 `commit`。

**预期结果**：你应能解释「为什么按字母键时应用文档里什么都不出现、而选词后文字瞬间出现」——前者 `handled=TRUE` 拦截 + 无 commit，后者 `handled=TRUE` 拦截 + 有 commit 让前端上屏。

> 说明：上述具体键码与引擎返回值依赖 librime 方案配置，标注的行为为典型情况；确切结果待本地用真实方案验证。

#### 4.2.5 小练习与答案

**练习 1**：如果 `process_key` 返回 `false`（引擎未消费，比如在中文模式下按 `F1`），`ProcessKeyEvent` 返回什么？前端会怎样？

**答案**：返回 `FALSE`。前端据此把 `*pfEaten` 置 false，按键透传给应用（例如浏览器收到 `F1` 打开帮助）。同时 `_Respond` 仍会被调用——但因为既无 commit、`is_composing` 多半仍为 false，响应头会是 `action=config`（甚至 `noop`），前端 UI 基本无变化。

**练习 2**：vim 补丁里为什么用 `keyEvent.mask & (1<<2)` 而不是命名常量？

**答案**：`1<<2` 就是 `ibus::Modifier::CONTROL_MASK`（KeyEvent.h 第 226 行）。这里用裸位是历史写法，语义等价于「按住了 Ctrl」。可读性不如具名常量，是可改进点。

---

### 4.3 `_Respond` 结果回传

#### 4.3.1 概念说明

`_Respond` 是连接「librime 产出」与「前端可消费文本」的**编码器**。它做三件事：

1. 向 librime 拉取本次按键的三类产出：`RimeCommit`（要上屏的文字）、`RimeStatus`（中/英、是否在写、方案号等稳态）、`RimeContext`（写作串 preedit、候选页 menu）。
2. 把它们编码成 u2-l5「行协议」：先收集一个 `actions` 列表（本次响应包含哪些动作），再把每个动作的正文 `key=value` 拼进一个宽字符串 `body`。
3. 先 `eat(header)`（`action=...` 声明行），再 `eat(body + ".\n")`（正文 + 结束标记），把响应经 `EatLine` 回写管道。

它和 u2-l5 的 `ResponseParser` 是一对**严格配对的编/解码器**：`_Respond` 写的每个 `key`，都必须在客户端有对应的 `Deserializer`（`commit`→`Committer`、`status`→`StatusUpdater`、`ctx`→`ContextUpdater`、`config`→`Configurator`、`style`→`Styler`、`ctx.cand`→`ContextUpdater` 内的候选反序列化）。

#### 4.3.2 核心流程

```
_Respond(ipc_id, eat)
  ├─ body.reserve(4096); actions.reserve(8);
  ├─ get_commit(session_id, &commit)
  │     └─ 有 → actions+="commit"; body += "commit=<escape(text)>\n"
  ├─ get_status(session_id, &status)
  │     ├─ actions+="status"
  │     ├─ body += "status.ascii_mode=" ... "status.composing=" ...
  │     │        "status.disabled=" ... "status.full_shape=" ... "status.schema_id=" ...
  │     ├─ 若 global_ascii 且本会话中英态变化 → 同步到其他所有会话
  │     └─ 缓存 status 到 session_status.status
  ├─ get_context(session_id, &ctx)
  │     ├─ 若 is_composing → actions+="ctx"
  │     │     └─ 按 preedit_type 编码 preedit + cursor:
  │     │         PREVIEW / COMPOSITION / PREVIEW_ALL
  │     └─ 若有候选 → boost text_woarchive 序列化 CandidateInfo
  │                   body += "ctx.cand=<archive>\n"
  ├─ actions+="config"; body += "config.inline_preedit=<0|1>\n"
  ├─ if (!__synced)
  │     ├─ boost text_woarchive 序列化 session_status.style
  │     ├─ actions+="style"; body += "style=<archive>\n"
  │     └─ __synced = true                  // 只在首次/方案切换后整块发
  ├─ header = actions 空 ? "action=noop\n" : "action=" + join(actions, ",") + "\n"
  ├─ if (!eat(header)) return false;
  ├─ body += ".\n";                          // 结束标记
  └─ return eat(body);
```

两个关键设计：

- **`__synced` 惰性同步**：`UIStyle` 有约 80 个字段，每次按键都整块重传既浪费管道带宽又会拖累 UI 重布局。因此只在「首次响应」「方案切换（`_GetStatus` 里 `schema_id != m_last_schema_id` 时把 `__synced` 复位）」「主题切换」后才发 `style=`，发完立即置 `__synced=true`。
- **`action=` 头先行**：先回写头部声明本次有哪些动作，客户端 `ActionLoader`（u2-l5）据此**懒激活**对应反序列器；未在头里声明的动作，其正文行会被丢弃。所以「在 `actions` 里登记」与「在 `body` 里写正文」必须成对出现。

#### 4.3.3 源码精读

**`_Respond` 主体**：

[RimeWithWeasel/RimeWithWeasel.cpp:738-935](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L738-L935) 是完整实现。分段看：

- **commit 段** [RimeWithWeasel/RimeWithWeasel.cpp:746-752](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L746-L752)：`get_commit` 取到本次要上屏的文字，`actions.push_back("commit")`，正文 `commit=<escape_string(u8tow(commit.text))>`。`escape_string`（u2-l5 提及）把换行等特殊字符转义，保证一行一动作的协议不被破坏。
- **status 段** [RimeWithWeasel/RimeWithWeasel.cpp:755-785](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L755-L785)：`get_status` 后逐字段拼 `status.ascii_mode=`/`composing=`/`disabled=`/`full_shape=`/`schema_id=`。其中 `Bool_wstring[] = {L"0", L"1"}` 是把 C 布尔转宽字符的查表小技巧。行 775-782 处理「全局 ASCII 模式」：若本会话中英态变化，把该变化广播到其它所有会话（`global_ascii_mode` 的同步点）。最后把 status 缓存进 `session_status.status`。
- **ctx（preedit）段** [RimeWithWeasel/RimeWithWeasel.cpp:787-883](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L787-L883)：`get_context` 后，若 `is_composing`，按 `session_status.style.preedit_type` 分三种 preedit 编码：
  - `PREVIEW`（行 804-823）：优先用 `ctx.commit_text_preview`（首选预览），无则 fall through 到 `COMPOSITION`。
  - `COMPOSITION`（行 824-838）：直接用 `ctx.composition.preedit`，并附 `ctx.preedit.cursor=start,end,cursor`（写作串高亮选区与光标位置）。注意 `cursor` 用 `utf8towcslen` 把 UTF-8 字节偏移换算成宽字符偏移——这是跨编码的关键。
  - `PREVIEW_ALL`（行 839-881）：把所有候选拼进 preedit 文本里（一种「把候选塞进 preedit 行」的紧凑布局，配合 `mark_text` 标记高亮项）。
- **候选段** [RimeWithWeasel/RimeWithWeasel.cpp:884-892](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L884-L892)：若 `has_candidates`，用 `boost::archive::text_woarchive` 把 `_GetCandidateInfo` 填好的 `CandidateInfo cinfo` 序列化成一行宽字符串，正文 `ctx.cand=<archive>`。这就是 u2-l4 讲的「`CandidateInfo` 走 boost 整块序列化」。
- **config 段** [RimeWithWeasel/RimeWithWeasel.cpp:897-900](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L897-L900)：每次都发 `config.inline_preedit=`，让前端知道当前会话是否启用内联 preedit。
- **style 段（惰性）** [RimeWithWeasel/RimeWithWeasel.cpp:903-911](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L903-L911)：仅当 `!session_status.__synced` 时，boost 序列化 `session_status.style` 整块、登记 `style` 动作、发完置 `__synced=true`。
- **组头与回写** [RimeWithWeasel/RimeWithWeasel.cpp:914-934](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L914-L934)：把 `actions` 逗号拼接成 `action=a,b,c\n`（空则 `action=noop\n`）。注释「send header first to avoid vector head-insert cost」解释了为何头部最后才拼却最先发：避免在 `body` 已 `reserve` 后再做头部前插的开销。随后 `eat(header)`、给 `body` 追加 `.\n`（结束标记，对应 u2-l5 里 `ResponseParser` 的响应终止符）、`eat(body)`。任一 `eat` 返回 false 即中止。

**`_GetCandidateInfo` —— 候选结构填充**：

[RimeWithWeasel/RimeWithWeasel.cpp:450-473](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L450-L473) 把 librime 的 `ctx.menu` 翻译成 weasel 的 `CandidateInfo`：按候选数 resize 三个对齐数组 `candies/comments/labels`，逐项 `escape_string(u8tow(...))` 转义转宽；标签优先取 `select_labels`，其次 `select_keys`，都没有则用 `(i+1)%10` 数字。还记录 `highlighted`/`currentPage`/`is_last_page`。这是 `_Respond` 候选段的数据来源。

**`EatLine` 的来源（衔接 u2-l3）**：

[include/WeaselIPC.h:53](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L53) 定义 `using EatLine = std::function<bool(std::wstring&)>;`。在 `ServerImpl` 里，`eat` 是一个捕获了管道 channel 的 lambda（形如 `[this](std::wstring& msg){ *channel << msg; return true; }`），所以 `_Respond` 调 `eat(header)` 实际就是把这一行写进该连接的管道缓冲，最终被前端 `ResponseParser` 读到。`_Respond` 因此对传输层完全无感——它只管「拼文本、调 `eat`」。

#### 4.3.4 代码实践

**实践目标**：给定两种典型按键场景，预测 `_Respond` 产出的 `action=` 头与正文行，从而验证你对编码协议的掌握。

**操作步骤**（协议推演型实践）：

1. **场景 A：中文模式下按字母 `n`（开始写作，无上屏）**。设 `is_composing=true`、无 commit、无候选、`__synced=true`、`inline_preedit=false`。
   - 推演 `_Respond`：
     - `get_commit` 无 → 不登记 `commit`。
     - `get_status` 有 → 登记 `status`，正文含 `status.composing=1` 等。
     - `get_context` 有且 composing → 登记 `ctx`，正文 `ctx.preedit=n` + `ctx.preedit.cursor=...`；无候选 → 不发 `ctx.cand`。
     - 登记 `config`：`config.inline_preedit=0`。
     - `__synced=true` → 不发 `style`。
   - 预期头：`action=status,ctx,config`。
2. **场景 B：写作 `ni` 后按 `1` 选词「你」上屏**。设选词后 `get_commit` 返回 `你`、`is_composing=false`、`__synced=true`。
   - 推演：
     - 登记 `commit`：`commit=你`。
     - 登记 `status`：`status.composing=0`。
     - composing 为 false → **不登记 `ctx`**（preedit 段在 `if (is_composing)` 内）。
     - 登记 `config`。
   - 预期头：`action=commit,status,config`。
3. **场景 C：刚 `AddSession` 的首次响应**。设 `__synced=false`。
   - 在场景 A 基础上多一个 `style`：预期头 `action=status,ctx,config,style`，且 `style=` 后跟一长串 boost 归档文本；之后 `__synced` 变 true。
4. 打开 [RimeWithWeasel/RimeWithWeasel.cpp:738-935](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L738-L935) 逐行核对你的推演。

**需要观察的现象**：三种场景的 `action=` 头不同；`style` 只在场景 C 出现；`commit` 只在场景 B 出现；`ctx` 在无写作时消失。

**预期结果**：你能不看源码说出「这个按键会带哪些动作」。这是日后排查「候选窗没刷新/没上屏/样式没生效」类问题的核心能力——对照 `action=` 头即可定位是编码端没发还是解码端没解析。

> 说明：场景中的引擎返回值（`is_composing`、是否有 commit/候选）依赖真实方案与按键序列，上述为典型推演；待本地用日志验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ctx.preedit.cursor` 要用 `utf8towcslen(preedit, start)` 转换，而不能直接把 librime 给的字节偏移写进去？

**答案**：librime 的 `composition.sel_start/sel_end/cursor_pos` 是 **UTF-8 字节偏移**，而前端的 `weasel::Text` 与 TSF 文档用**宽字符（UTF-16）偏移**来标注高亮范围。若直接传字节偏移，前端会按宽字符下标去取子串，位置全错（中文一个字 3 字节但 1 个宽字符）。`utf8towcslen` 负责这层单位换算。

**练习 2**：如果新增一种 preedit 类型（比如「只显示首候选注释」），需要改 `_Respond` 的哪些地方？前端要同步改什么？

**答案**：`_Respond` 的 `switch (session_status.style.preedit_type)` 里加一个 `case`，并在 `UIStyle::PreeditType` 枚举与 `_UpdateUIStyle` 的解析表（见 u7-l3）里登记新值；编码出的仍应是 `ctx.preedit=` 行，所以前端 `ContextUpdater` 无需改。这正体现了「服务端决策、前端执行」的分工（u2-l4）。

---

## 5. 综合实践

**综合任务**：完成本讲规格里要求的主链路追踪——「一次有效按键从 `ProcessKeyEvent` 到 `_Respond` 的完整流程」，把本讲三个模块串起来。

请在一张大图上画出下面这条链路，并逐节点标注**函数名 + 源码行号 + 涉及的 `rime_api` 调用 + 对会话状态/UI/管道的副作用**：

1. 前端经 IPC 发来 `WEASEL_IPC_PROCESS_KEY_EVENT`，`ServerImpl::HandlePipeMessage` 派发到 `ProcessKeyEvent(keyEvent, ipc_id, eat)`（入口 [RimeWithWeasel/RimeWithWeasel.cpp:264](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L264)）。
2. `to_session_id(ipc_id)` 经 `m_session_status_map` 翻译出 `session_id`（4.1）。
3. `rime_api->process_key(session_id, keycode, expand_ibus_modifier(mask))` → `handled`（4.2）。
4. `_Respond(ipc_id, eat)`：
   - `get_commit`/`get_status`/`get_context` 三个 `rime_api` 调用拉产出（4.3）。
   - 更新 `session_status.status` 缓存（会话状态副作用）。
   - 拼 `action=...` 头 + 正文，`eat(header)`、`eat(body+".\n")` 回写管道（管道副作用）。
5. `_UpdateUI(ipc_id)`（[RimeWithWeasel/RimeWithWeasel.cpp:518-553](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L518-L553)）：`_GetStatus`/`_ShowMessage`/`m_ui->Update` 刷新候选窗（UI 副作用），并按 `m_notifier_mutex` 清空静态通知缓冲。
6. `m_active_session = ipc_id`，返回 `(BOOL)handled` → 客户端 `*pfEaten`（衔接 u3-l2）。

**进阶**：在图上用不同颜色标出三类副作用——「会话状态写入」「管道回写」「UI 刷新」，并指出 `global_ascii_mode` 开启时，步骤 4 的 status 段会额外向**其它会话**广播中/英态（[RimeWithWeasel/RimeWithWeasel.cpp:775-782](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L775-L782)）。

> 说明：本实践为源码阅读 + 画图型，无需编译运行。完成后你应拥有一张可直接用于排查「按键无响应/不上屏/候选不刷新」问题的总链路图。

## 6. 本讲小结

- Weasel 维护**两套会话 ID**：`WeaselSessionId`（IPC 层 `DWORD`，由 Server 基于 PID 高位平移后单调递增生成）与 `RimeSessionId`（librime 引擎号），由 `SessionStatusMap m_session_status_map` 双向翻译，`to_session_id`/`get_session_status`/`new_session_status` 三个内联 helper 封装访问。
- `SessionStatus` 不仅是 ID 映射，还缓存每会话的 `UIStyle style`、`RimeStatus status` 与 `__synced` 标志，避免每次按键都向 librime 拉全量数据。
- `AddSession`/`RemoveSession`/`FindSession` 构成会话生命周期；`AddSession` 中 `_ReadClientInfo` 读取前端应用名、套用 per-app 选项，是「不同应用不同输入行为」的落点。
- `ProcessKeyEvent` 的主链路极简：`to_session_id` → `rime_api->process_key`（修饰键经 `expand_ibus_modifier` 位扩展）→ `_Respond` → `_UpdateUI` → 返回 `handled`；返回值即客户端的吃键/放行信号，与「上屏文字」是两回事。
- vim 模式补丁在「引擎未处理 + Escape/Ctrl+[ 等按键 + 开了 vim_mode + 非英文」时自动切 `ascii_mode`，仅在 keydown 触发。
- `_Respond` 是 librime 产出到前端行协议的编码器：`get_commit`/`get_status`/`get_context` 三路拉取，编码成 `action=commit,status,ctx,config[,style]` 头 + 对应正文，经 `EatLine` 回写管道；`style` 受 `__synced` 惰性控制，候选与样式走 boost 整块序列化，preedit 光标偏移用 `utf8towcslen` 做字节→宽字符换算。

## 7. 下一步学习建议

- **u4-l3（方案配置、App 选项与 inline preedit）**：深入 `_LoadSchemaSpecificSettings`、`AppOptionsByAppName`、`_LoadAppInlinePreeditSet`/`_UpdateInlinePreeditStatus`，理解本讲里多次出现的 per-app 选项与方案切换样式重载的完整机制。
- **u4-l4（UI 更新、消息通知与维护/主题）**：精读 `_UpdateUI`、`_ShowMessage`、`OnNotify` 与维护模式，把本讲「`_UpdateUI` 刷新 UI」这一步展开成完整的显隐控制与通知去重逻辑。
- **回看 u2-l5（响应解析）**：把本讲 `_Respond` 写出的每一行，对照 `ResponseParser`/`Committer`/`Styler` 的 `Store` 实现，确认编解码配对，巩固对 IPC 文本协议的整体掌握。
- **延伸阅读**：`_GetStatus`（[RimeWithWeasel.cpp:1435-1474](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L1435-L1474)）里方案切换检测与 `__synced` 复位逻辑，是理解「为什么切方案后样式会重新整块下发」的关键。
