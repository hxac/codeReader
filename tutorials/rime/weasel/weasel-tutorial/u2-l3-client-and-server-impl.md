# IPC 客户端与服务器实现

## 1. 本讲目标

本讲是「IPC 骨架」单元的第三讲。前两讲我们已经搭好了两块基石：

- u2-l1 讲清了 IPC 的「合同」：`Client` / `Server` / `RequestHandler` 三大抽象、`WEASEL_IPC_COMMAND` 命令枚举、定长 `PipeMessage`，以及那张「方法↔命令↔派发函数↔虚函数」映射表。
- u2-l2 讲清了「通道」：`PipeChannel` 模板如何把一条「定长头 + 变长正文」的请求-响应字节流，经命名管道在两端搬运，并用线程本地存储隔离并发。

本讲把镜头拉到通道**两端的使用者**：客户端的 `ClientImpl` 与服务端的 `PipeServer` / `ServerImpl`。换句话说，我们要回答：客户端怎么把一个高层方法（比如 `ProcessKeyEvent`）变成管道上的一条命令、又怎么管理「会话」这个概念；服务端怎么用一条监听线程 + 一堆工作线程接住这些命令、再派发到 `RequestHandler` 与托盘菜单。

学完本讲你应该能够：

- 说出 `ClientImpl` 如何用 `session_id` 把「连接」与「会话」分成两层状态，并能复述 `StartSession` 从写客户端信息到拿到会话号的完整过程。
- 画出服务端的线程模型：`Run` 起一条 `pipeThread` 跑 `Listen`，`Listen` 每接到一个连接就派生一条 `_ProcessPipeThread`，再由一把全局 `g_api_mutex` 把所有命令处理串行化。
- 解释 `HandlePipeMessage` 的宏 `switch` 如何把 `PipeMessage.Msg` 派发到各个 `OnXxx` 处理器，以及 `eat` 这个回调为什么能把响应正文写回管道。
- 理解 `ServerImpl` 既是 IPC 派发器、又是一个 WTL 隐藏窗口（承载托盘菜单与系统关机消息），并知道如何新增一个托盘菜单命令。

---

## 2. 前置知识

在进入源码前，先建立四个直觉。

### 2.1 「连接」与「会话」是两层状态

回忆日常上网的经历：连上 Wi-Fi（连接）不等于登录了某个账号（会话）。Weasel 的客户端也是这个两层模型：

- **连接（connection）**：一根命名管道，由 `PipeChannel` 管理，代表「我能不能把字节送到 Server」。
- **会话（session）**：Server 端为「某个应用进程的某次输入上下文」分配的一个编号 `session_id`，代表「Server 认不认识我、愿不愿意处理我的按键」。

很多命令（`PROCESS_KEY_EVENT`、`COMMIT_COMPOSITION`、`UPDATE_INPUT_POS`……）都必须带上 `session_id` 才有意义——Server 要靠它找到对应的输入上下文。所以客户端的几乎所有方法第一行都是 `_Active()` 检查：既连上了、又有有效会话号，才允许发命令。

### 2.2 pImpl（指向实现的指针）惯用法

`Client` 和 `Server` 是暴露给上层（WeaselTSF、WeaselServer）的**接口类**，它们的方法都只是「转发给 `m_pImpl`」的一行壳：

```cpp
bool Client::Connect(ServerLauncher launcher) { return m_pImpl->Connect(launcher); }
```

真正的逻辑住在 `ClientImpl` / `ServerImpl` 里（见 [WeaselClientImpl.cpp:211-213](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L211-L213)）。这样做的好处是：把 `PipeChannel`、boost、WTL 这些实现细节关在 `.cpp` 里，公共头 `WeaselIPC.h` 保持干净、ABI 稳定。本讲的源码精读主要看 `*Impl`，`Client` / `Server` 的转发壳只在最后略作交代。

### 2.3 boost::thread 的「可打断」特性

服务端监听是一条 `for(;;)` 死循环，正常情况下永远不退出。Weasel 用 `boost::thread` 而不是 `std::thread`，关键原因之一是 `boost::thread` 支持 **interruption**（打断）：在循环里插入 `boost::this_thread::interruption_point()`，当主线程调用 `pipeThread->interrupt()` 时，循环会在最近的打断点抛出 `boost::thread_interrupted` 从而优雅退出。本讲的 `_Finailize` / `Listen` 都依赖这个机制。

### 2.4 WTL 隐藏窗口 + 消息循环

`ServerImpl` 继承自 `CWindowImpl`（WTL/ATL 的窗口基类），它创建了一个**不可见的窗口**。这个窗口不是为了显示，而是为了两件事：

1. **承载消息循环**：`CMessageLoop::Run()` 让 Server 进程常驻，并能把 Windows 消息（关机、配色变更、托盘菜单命令）派发给 `OnXxx`。
2. **单实例锁**：通过窗口类名 + 命名互斥量保证全局只有一个 WeaselServer。

所以 `ServerImpl` 同时扮演两个角色：**IPC 命令派发器**（处理管道来的 `PipeMessage`）和**系统消息处理器**（处理窗口来的 `WM_COMMAND` 等）。理解这种「双身份」是本讲后半段的关键。

> 名词速查：`EatLine` 是 `RequestHandler` 里定义的回调类型 `std::function<bool(std::wstring&)>`（见 [include/WeaselIPC.h:53](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L53)），Server 用它把「这次按键产生的候选文本」一段段写回客户端。本讲会反复看到它的实例化。

---

## 3. 本讲源码地图

本讲盯住「通道两端的使用者」，涉及的文件集中在两个子工程：

| 文件 | 作用 |
| --- | --- |
| [WeaselIPC/WeaselClientImpl.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.h) | `ClientImpl` 类声明：会话状态、客户端信息、`_SendMessage` 封装。 |
| [WeaselIPC/WeaselClientImpl.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp) | 客户端全部实现：连接、各命令封装、`StartSession`、pImpl 转发壳。 |
| [WeaselIPCServer/WeaselServerImpl.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.h) | `ServerImpl` 类声明：WTL 消息映射、`OnXxx` 处理器表、成员（管道线程、处理器指针、菜单表）。 |
| [WeaselIPCServer/WeaselServerImpl.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp) | `PipeServer`、`ServerImpl` 与 pImpl 转发的全部实现。 |
| [include/WeaselIPC.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h) | `Client` / `Server` 接口、`RequestHandler` 抽象基类、命令枚举、`GetPipeName()`（u2-l1 已详述，本讲按需引用）。 |
| [include/PipeChannel.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h) | `PipeChannel` 模板（u2-l2 已详述）：`Connect`/`Transact`/`ReceiveBuffer`/`HandleResponseData` 等内联方法。 |

> 前置提醒：通道本身的字节级细节（`_Ensure`/`Transact`/线程本地缓冲）已在 u2-l2 讲透，本讲直接把它们当成「能用的工具」，只在需要佐证两端协作时点一下。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **ClientImpl 连接与会话管理** —— 客户端如何把高层方法变成 `PipeMessage`，如何用 `session_id` 管理会话。
2. **PipeServer 监听与线程模型** —— 服务端如何用一条监听线程接连接、用工作线程读命令，并用一把全局锁串行化处理。
3. **ServerImpl 命令派发与菜单** —— `HandlePipeMessage` 的宏派发表、`eat` 回调如何回写响应正文，以及托盘菜单与系统消息。

### 4.1 ClientImpl 连接与会话管理

#### 4.1.1 概念说明

`ClientImpl` 是 WeaselTSF（以及任何想用 Rime 的进程）一侧的 IPC 客户端实现。它的职责可以浓缩成一句话：**把一组语义清晰的方法，翻译成「向管道写一条 `PipeMessage`，再读回一个 `DWORD` 结果」的机械动作**。

它内部只有四个成员（见 [WeaselClientImpl.h:41-46](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.h#L41-L46)）：

| 成员 | 含义 |
| --- | --- |
| `UINT session_id` | 当前会话号；`0` 表示「无有效会话」。 |
| `std::wstring app_name` | 宿主进程的可执行名（小写），如 `notepad.exe` → `notepad.exe`。 |
| `bool is_ime` | 客户端类型：`.ime` 旧式输入法为 `true`，TSF 为 `false`。 |
| `PipeChannel<PipeMessage> channel` | 通道（u2-l2 主角），模板参数说明「我发 `PipeMessage`、收 `DWORD`」。 |

两个状态判定是理解所有命令方法的前提（见 [WeaselClientImpl.h:38-39](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.h#L38-L39)）：

```cpp
bool _Connected() const { return channel.Connected(); }
bool _Active() const { return channel.Connected() && session_id != 0; }
```

`_Connected` 是「管道通不通」，`_Active` 是「能不能发需要会话的命令」。这两层判定贯穿整个客户端。

#### 4.1.2 核心流程

客户端从「冷启动」到「能上屏」的生命周期伪代码：

```
构造 ClientImpl:
    session_id = 0
    channel = PipeChannel(GetPipeName())   # 还没连
    _InitializeClientInfo()                # 探测 app_name / is_ime

Connect():          channel.Connect()  →  _Ensure()  →  _Connect(管道名)   # 拉起一根管道
StartSession():
    若 _Active() 且 Echo() 通过:  return            # 复用旧会话
    _WriteClientInfo()             # 把 app_name 写进通道正文缓冲
    session_id = _SendMessage(START_SESSION, 0, 0)  # 发命令、收会话号
ProcessKeyEvent(ke):
    若 not _Active(): return false
    ret = _SendMessage(PROCESS_KEY_EVENT, ke, session_id)
    return ret != 0                 # ret 非 0 表示 Server「吃掉」了这个键
GetResponseData(handler):          # 把候选文本交给上层解析（u2-l5 主题）
    channel.HandleResponseData(handler)
EndSession():
    _SendMessage(END_SESSION, 0, session_id)
    session_id = 0
```

其中 `_SendMessage` 是所有命令的**唯一出口**（见 [WeaselClientImpl.cpp:193-202](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L193-L202)）：它把三字段打包成 `PipeMessage`，调 `channel.Transact(req)` 走完「写头+正文 → 读响应」一趟，并用 `catch(DWORD)` 把任何管道异常吞成返回值 `0`（即「命令失败/未被吃掉」）。这种「异常码 → 0」的退化正是上层 `ret != 0` 判断的安全网。

#### 4.1.3 源码精读

**(a) 构造与客户端信息探测**

构造函数初始化三层状态并探测宿主信息（[WeaselClientImpl.cpp:7-10](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L7-L10)）：

```cpp
ClientImpl::ClientImpl()
    : session_id(0), channel(GetPipeName()), is_ime(false) {
  _InitializeClientInfo();
}
```

注意 `channel(GetPipeName())` 把管道名交给通道但**并不连接**——真正的连接发生在后续的 `Connect()` 里，这是 u2-l2 讲过的「按需连接」。

`_InitializeClientInfo` 探测两件事（[WeaselClientImpl.cpp:26-42](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L26-L42)）：用 `GetModuleFileName(NULL, ...)` 取**宿主进程**（即加载了 weasel.dll 的应用，如 notepad.exe）的名字；再用 `GetCurrentModule()` 取 weasel.dll 自身路径，看后缀是不是 `.ime` 来判定客户端类型。这两条信息会在 `StartSession` 时上报给 Server，供其做应用级选项（u4-l3 的 AppOptions 会用到 `app_name`）。

> 小贴士：`GetCurrentModule()` 用 `GetModuleHandleEx(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS, &GetCurrentModule, ...)` 这一招，通过「取本函数地址所属的模块」来拿到 DLL 自身的 `HMODULE`——这是 Win32 下不依赖全局变量的经典写法（[WeaselClientImpl.cpp:18-24](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L18-L24)）。

**(b) Connect：仅建立管道**

```cpp
bool ClientImpl::Connect(ServerLauncher const& launcher) {
  return channel.Connect();
}
```

这段代码（[WeaselClientImpl.cpp:44-46](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L44-L46)）有一个值得诚实指出的细节：**形参 `launcher` 在当前 HEAD 里并没有被使用**——方法体只是 `channel.Connect()`（即 `_Ensure()`，去连一根已存在的管道）。换言之，「按需启动 Server 进程」这件事并不发生在这里。Server 进程的拉起由更外层负责（安装器、WeaselServer 自身的单实例重启、TSF 的激活流程，见 u6-l3）。`launcher` 这个参数保留了接口形态，便于上层传入但当前实现不消费它。

> 待本地验证：若你想确认「Server 没运行时 `Connect` 会怎样」，可在未启动 WeaselServer 时让客户端调 `Connect()`——预期 `channel.Connect()` 内部 `_TryConnect` 会因 `CreateFile(OPEN_EXISTING)` 找不到管道而返回 `INVALID_HANDLE_VALUE`，`_Ensure` 最终返回 `false`。

**(c) StartSession：写客户端信息 → 拿会话号**

这是客户端最关键的一步（[WeaselClientImpl.cpp:145-152](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L145-L152)）：

```cpp
void ClientImpl::StartSession() {
  if (_Active() && Echo())
    return;                       // 已有活会话且 Server 仍认得它，直接复用
  _WriteClientInfo();             // 把 app_name/type 写进通道正文缓冲
  UINT ret = _SendMessage(WEASEL_IPC_START_SESSION, 0, 0);
  session_id = ret;               // Server 返回的会话号
}
```

`Echo()`（[WeaselClientImpl.cpp:169-175](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L169-L175)）发一条 `WEASEL_IPC_ECHO`，把 `session_id` 发给 Server 让它原样回传；若回传值相等，说明这条会话在 Server 侧还活着，于是不必重建——这是一个轻量的**会话存活探测**，避免应用切换焦点时反复重建会话。

`_WriteClientInfo` 把客户端信息以**文本正文**形式写进通道缓冲（[WeaselClientImpl.cpp:185-191](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L185-L191)）：

```cpp
channel << L"action=session\n";
channel << L"session.client_app=" << app_name.c_str() << L"\n";
channel << L"session.client_type=" << (is_ime ? L"ime" : L"tsf") << L"\n";
channel << L".\n";                # 单独一个点号表示正文结束
```

回忆 u2-l2：`operator<<` 把内容累积进 `ChannelContext::write_stream`（一块定长缓冲里「头部之后」的区域），并把 `has_body` 置 `true`；随后 `_SendMessage` → `Transact` → `_Send` 会把「`PipeMessage` 头 + 这段正文」一次性写进管道。末尾的 `.\n` 是正文结束标记，对端据此知道客户端信息到此为止——这套「行文本 + 点号结尾」的协议正是 u2-l5 要讲的 `ResponseParser` 行协议的同款风格。

**(d) 命令封装与 `_SendMessage` 出口**

以 `ProcessKeyEvent` 为例（[WeaselClientImpl.cpp:58-65](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L58-L65)）：

```cpp
bool ClientImpl::ProcessKeyEvent(KeyEvent const& keyEvent) {
  if (!_Active())
    return false;
  LRESULT ret = _SendMessage(WEASEL_IPC_PROCESS_KEY_EVENT, keyEvent, session_id);
  return ret != 0;
}
```

可以看到一个固定模板：**`_Active()` 守卫 → `_SendMessage(命令, wParam, session_id)` → 用返回值是否非 0 解释语义**。`CommitComposition` / `ClearComposition` / `SelectCandidateOnCurrentPage` / `HighlightCandidateOnCurrentPage` / `ChangePage`（[WeaselClientImpl.cpp:67-104](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L67-L104)）全都长这样，只是命令枚举与 `wParam` 的载荷不同（候选索引、翻页方向等）。

`_SendMessage` 是统一出口（[WeaselClientImpl.cpp:193-202](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L193-L202)）：

```cpp
LRESULT ClientImpl::_SendMessage(WEASEL_IPC_COMMAND Msg, DWORD wParam, DWORD lParam) {
  try {
    PipeMessage req{Msg, wParam, lParam};
    return channel.Transact(req);
  } catch (DWORD /* ex */) {
    return 0;
  }
}
```

`Transact` 的返回类型是模板的 `_TyRes`，客户端这里就是 `DWORD`（[PipeChannel.h:120-125](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L120-L125)）。对 `START_SESSION` 这个 `DWORD` 是会话号；对 `PROCESS_KEY_EVENT` 这个 `DWORD` 是「是否吃键」（非 0 为吃）；对 `ECHO` 是回传的会话号——同一个返回槽，语义随命令而变（与 u2-l1 的「载荷语义随命令而变」一脉相承）。

**(e) UpdateInputPosition：把矩形压进一个 DWORD**

候选窗口要「光标跟随」，客户端必须把光标矩形送给 Server。但 `PipeMessage` 只有 `wParam`/`lParam` 两个 `DWORD`，于是 Weasel 把整个 `RECT` 压缩进一个 `DWORD`（[WeaselClientImpl.cpp:106-130](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L106-L130)）。32 个比特位的分配如下：

```
 bit31       bit24..30    bit12..23      bit0..11
+--------+-------------+--------------+----------+
| hi_res |  height(7)  |  top(12,有符号) | left(12,有符号)|
+--------+-------------+--------------+----------+
```

- `hi_res`（1 bit）：高分辨率标志。当矩形超出普通范围（高 ≥128、或 left/top 越过 ±2048）时置 1，此时各字段右移一位以扩大量程（牺牲 1 位精度换更大范围）。
- `height`（7 bit）、`top`/`left`（各 12 bit 有符号）：分别表示候选窗口相对光标的高度与左上角坐标。

打包后用 `_SendMessage(WEASEL_IPC_UPDATE_INPUT_POS, compressed_rect, session_id)` 发出，服务端再用位运算还原（见 4.3.3）。这是一个典型的「把结构体塞进定长协议字段」的工程取舍。

**(f) 取回响应正文**

按键发出去后，Server 算出的候选文本要通过通道回传。客户端用 `GetResponseData` 把「接收缓冲整块」交给上层的 `ResponseHandler`（[WeaselClientImpl.cpp:177-183](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L177-L183)）：

```cpp
bool ClientImpl::GetResponseData(ResponseHandler const& handler) {
  if (!handler) return false;
  return channel.HandleResponseData(handler);
}
```

`HandleResponseData` 把整块缓冲当宽字符数组交给 handler（[PipeChannel.h:139-148](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L139-L148)）。注意它发生在 `Transact` **之后**：`Transact` 只读回了定长的 `DWORD` 响应头，而变长正文仍留在接收缓冲里等这一步取走。如何把这段正文解析成 `Context`/`Status`/`UIStyle`，正是 u2-l5 的主题。

#### 4.1.4 代码实践

**实践目标**：在源码里完整追踪 `Client::Connect → StartSession → ProcessKeyEvent` 这条「建立连接 → 建立会话 → 打一个键」的调用链，把每一步对应的代码行与管道上发出的命令写出来。

**操作步骤**：

1. 打开 [WeaselClientImpl.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp)，定位下面三个入口的 pImpl 转发壳与实现。
2. 对每一步，记录：调用的代码行、构造的 `PipeMessage`（`Msg`/`wParam`/`lParam` 三字段）、是否带正文（`has_body`）、`Transact` 返回的 `DWORD` 如何被解释。
3. 建议画一张三列时序表。

**需要观察的现象 / 预期结果**（参考答案形式）：

| 步骤 | 入口代码行 | 实现代码行 | 管道上的 `PipeMessage` | 正文？ | 返回 `DWORD` 的含义 |
| --- | --- | --- | --- | --- | --- |
| Connect | `Client::Connect` [211-213](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L211-L213) | `ClientImpl::Connect` [44-46](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L44-L46) | 无（仅 `channel.Connect()` 建立管道，未发命令） | 否 | `bool`：管道是否连通 |
| StartSession | `Client::StartSession` [259-261](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L259-L261) | `ClientImpl::StartSession` [145-152](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L145-L152) | `{WEASEL_IPC_START_SESSION, 0, 0}` | 是（`_WriteClientInfo` 写入 app/type，见 [185-191](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L185-L191)） | 会话号 `session_id`（0 表示失败） |
| ProcessKeyEvent | `Client::ProcessKeyEvent` [223-225](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L223-L225) | `ClientImpl::ProcessKeyEvent` [58-65](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L58-L65) | `{WEASEL_IPC_PROCESS_KEY_EVENT, keyEvent, session_id}` | 否（请求不带正文；响应正文由后续 `GetResponseData` 取回） | 非 0 = Server 吃掉该键；0 = 放行不处理 |

**进阶观察**：注意 `StartSession` 的复用分支——若 `_Active() && Echo()` 为真，则**根本不会**发 `START_SESSION`。也就是说，同一个应用进程里连续多次「焦点回到文档」并不会每次都重建会话，前提是 `Echo()` 探测成功。请把这个短路分支在你的时序图里单独标出。

> 待本地验证：上表是纯源码追踪结果，未实际运行。若你在 Windows 构建环境里附加调试器到 WeaselServer，可在 `ServerImpl::HandlePipeMessage`（见 4.3）打断点核对实际收到的命令序列是否与上表一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `EndSession`、`StartMaintenance`、`EndMaintenance` 这三个方法执行后都把 `session_id` 置 0（见 [WeaselClientImpl.cpp:154-167](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L154-L167)）？

> **参考答案**：`EndSession` 主动终结了当前会话，会话号自然失效；`StartMaintenance`/`EndMaintenance` 把 Server 切到「维护模式」，此期间不接受按键，旧会话对客户端而言已不可用。把 `session_id` 清 0 让 `_Active()` 返回 `false`，从而阻止后续命令在「无有效会话」时被发出，是一种防御式状态管理。

**练习 2**：若某次 `_SendMessage` 内部 `channel.Transact` 抛出了 `DWORD` 异常（比如管道断了），调用方（如 `ProcessKeyEvent`）会返回什么？这个返回值会不会被上层误解为「Server 明确表示不吃这个键」？

> **参考答案**：`_SendMessage` 的 `catch(DWORD)` 会吞掉异常并返回 `0`（[WeaselClientImpl.cpp:199-201](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L199-L201)），于是 `ProcessKeyEvent` 返回 `false`。这个语义在功能上等同于「不吃键」——按键会被放行给应用。这是一种**安全的退化**：Server 不可用时，输入法宁可「放行原始按键」也不要「吞键」，避免用户按键彻底失效。代价是无法区分「Server 说不要」与「Server 连不上」。

---

### 4.2 PipeServer 监听与线程模型

#### 4.2.1 概念说明

服务端在通道里的身份，u2-l2 已点破：客户端写 `PipeChannel<PipeMessage>`（发 `PipeMessage`、收 `DWORD`），服务端则把模板参数「反过来」写——`PipeServer : public PipeChannel<DWORD, PipeMessage>`（见 [WeaselServerImpl.cpp:9-24](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L9-L24)）。也就是说，**服务端发的 `Msg` 是 `DWORD`（响应头），收的 `Res` 是 `PipeMessage`（请求）**。这种「镜像」正是同一套通道能在两端复用的关键。

`PipeServer` 在 `PipeChannel` 之上只增加了三样东西（[WeaselServerImpl.cpp:9-24](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L9-L24)）：

- 三个 `std::function` 类型别名：`ServerRunner`、`Respond`（把一个 `Msg` 即 `DWORD` 写回管道）、`ServerHandler`（拿到 `PipeMessage` 后，附带一个 `Respond` 回调交给业务层）。
- `Listen(handler)`：监听主循环。
- `_ProcessPipeThread(pipe, handler)`：每连接的工作循环。

而 `ServerImpl` 用一条独立的 `boost::thread`（`pipeThread`）来跑 `Listen`，自己在主线程跑 WTL 消息循环。两者通过 `handler` 闭包通信。

#### 4.2.2 核心流程

服务端启动到处理一条命令的全流程伪代码：

```
ServerImpl::Run():
    listener = [](msg, resp):
        lock_guard(g_api_mutex)          # 关键：全局串行化
        HandlePipeMessage(msg, resp)
    pipeThread = new boost::thread( channel->Listen(listener) )
    CMessageLoop().Run()                 # 主线程进消息循环，常驻

PipeServer::Listen(handler):
    for(;;):
        interruption_point()             # 可被 interrupt() 打断
        pipe = _ConnectServerPipe(pname) # 建实例并阻塞等客户端连入
        new boost::thread( _ProcessPipeThread(pipe, handler) )  # 每连接一线程
        # 循环回去，继续等下一个连接

PipeServer::_ProcessPipeThread(pipe, handler):
    for(;;):
        msg = _Receive(pipe)             # 读一条 PipeMessage 请求
        handler(msg, resp=[](r){ _Send(pipe, r) })  # 交给业务，并提供"回写响应"回调
```

这里有两个要点：

1. **连接级并发 + 处理级串行**：`Listen` 为每个接入的管道实例派生一条新线程（`_ProcessPipeThread`），所以**读**是并发的；但 `listener` 闭包里那把 `g_api_mutex`（[WeaselServerImpl.cpp:165](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L165)）把所有 `HandlePipeMessage` 调用串成一条——**同一时刻只有一个命令真正进入 `RequestHandler`**。这规避了 librime 非线程安全的并发风险，代价是失去了命令级并行。
2. **回调式响应**：`handler` 不直接返回结果，而是接收一个 `Respond` 回调，由业务层在准备好后调用 `resp(result)` 把 `DWORD` 写回管道。这种「控制反转」让业务层可以先把变长正文写进通道缓冲，再发响应头（见 4.3.3 的 `eat` 模式）。

#### 4.2.3 源码精读

**(a) Listen：监听主循环**

```cpp
void PipeServer::Listen(ServerHandler const& handler) {
  for (;;) {
    HANDLE pipe = INVALID_HANDLE_VALUE;
    try {
      boost::this_thread::interruption_point();      // 优雅退出点
      pipe = _ConnectServerPipe(pname);              // 建实例 + ConnectNamedPipe 阻塞等连入
      boost::thread th(
          [&handler, pipe, this] { _ProcessPipeThread(pipe, handler); });
    } catch (DWORD ex) {
      _FinalizePipe(pipe);                           // 某个实例创建/连接失败，清掉重试
    }
    boost::this_thread::interruption_point();
  }
}
```

见 [WeaselServerImpl.cpp:408-421](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L408-L421)。`_ConnectServerPipe` 是 u2-l2 讲过的服务端原语：`CreateNamedPipe(..., PIPE_UNLIMITED_INSTANCES, ...)` 建一个新实例，再 `ConnectNamedPipe` 阻塞到有客户端连入（[PipeChannel.cpp:104-113](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/PipeChannel.cpp#L104-L113)）。一旦连入，立刻派生一条 `boost::thread` 去服务这个连接，主循环回头继续等下一个——这就是「每个客户端连接一条工作线程」的由来。两个 `interruption_point` 让 `_Finailize` 能干净地叫停它。

**(b) _ProcessPipeThread：连接工作循环**

```cpp
void PipeServer::_ProcessPipeThread(HANDLE pipe, ServerHandler const& handler) {
  try {
    for (;;) {
      Res msg;                                        // Res = PipeMessage
      _Receive(pipe, &msg, sizeof(msg));              // 读一条请求
      handler(msg, [this, pipe](Msg resp) { _Send(pipe, resp); });  // Msg = DWORD
    }
  } catch (...) {
    _FinalizePipe(pipe);                              // 客户端断开等异常 → 清理这条连接
  }
}
```

见 [WeaselServerImpl.cpp:428-438](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L428-L438)。注意模板实例化后的类型：`Res` 是 `PipeMessage`（服务端**收**的请求），`Msg` 是 `DWORD`（服务端**发**的响应头）。`_Receive`/`_Send` 都是 u2-l2 里 `PipeChannel` 的成员，`_Receive` 会处理 `ERROR_MORE_DATA` 把变长正文续读到缓冲（[PipeChannel.cpp:88-102](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/PipeChannel.cpp#L88-L102)）。`catch(...)` 兜住所有异常（客户端中途断开、读失败等），把这条管道实例关掉——**只影响这一个连接，不影响其他连接和主循环**，这就是连接级隔离的好处。

**(c) ServerImpl::Run：双线程启动**

```cpp
static std::mutex g_api_mutex;

int ServerImpl::Run() {
  auto listener = [this](PipeMessage msg, PipeServer::Respond resp) -> void {
    std::lock_guard guard(g_api_mutex);     // 串行化所有命令处理
    HandlePipeMessage(msg, resp);
  };
  pipeThread = std::make_unique<boost::thread>(
      [this, &listener]() { channel->Listen(listener); });

  CMessageLoop theLoop;
  _Module.AddMessageLoop(&theLoop);
  int nRet = theLoop.Run();                  // 主线程进入 WTL 消息循环
  _Module.RemoveMessageLoop();
  return nRet;
}
```

见 [WeaselServerImpl.cpp:165-186](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L165-L186)。这是整个服务端的「双核」启动：一条 `pipeThread` 跑管道监听（及其派生的工作线程），主线程跑 `CMessageLoop::Run()` 处理窗口消息。`listener` 闭包捕获 `this`，把每条 `PipeMessage` 交给 `HandlePipeMessage`，并用 `g_api_mutex` 串行化——这是本模块最核心的一行，它决定了 Weasel 的 IPC 处理是「单线程语义」的。

> 注意：`g_api_mutex` 是文件级 `static std::mutex`（[WeaselServerImpl.cpp:165](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L165)），全局唯一。无论多少个工作线程同时拿到命令，真正进入 `HandlePipeMessage` → `RequestHandler` 的只有一个。

**(d) Start / Stop / _Finailize：生命周期**

`Start` 用命名互斥量保证单实例（[WeaselServerImpl.cpp:142-156](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L142-L156)）：

```cpp
std::wstring instanceName = L"(WEASEL)Furandōru-Sukāretto-";
instanceName += getUsername();
HANDLE hMutexOneInstance = ::CreateMutex(NULL, FALSE, instanceName.c_str());
bool areYouOK = (::GetLastError() == ERROR_ALREADY_EXISTS ||
                ::GetLastError() == ERROR_ACCESS_DENIED);
if (areYouOK) return 0;          // 已有实例在跑，不再创建窗口
HWND hwnd = Create(NULL);        // 创建隐藏窗口
return hwnd;
```

互斥量名带上 `getUsername()`，与管道名一样按用户隔离；若已存在（`ERROR_ALREADY_EXISTS`），直接返回 `0` 不再创建窗口——这就是「全局唯一 Server」的守卫。窗口类名由 `DECLARE_WND_CLASS(WEASEL_IPC_WINDOW)` 定义（[WeaselServerImpl.h:20](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.h#L20)），即 `WeaselIPCWindow_1.0`（[WeaselIPC.h:9](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L9)）。

`Stop` 只是 `PostMessage(WM_QUIT)`（[WeaselServerImpl.cpp:158-163](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L158-L163)）——它**不**直接终结进程，而是让主消息循环退出，把收尾交给上层 `WeaselServer` 进程；注释明确写了「DO NOT exit process or finalize here」。

`_Finailize`（注意源码里的拼写）打断管道线程并销毁窗口（[WeaselServerImpl.cpp:42-54](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L42-L54)）：`pipeThread->interrupt()` 让 `Listen` 在最近的 `interruption_point` 抛出退出，再把 `pipeThread` 置空避免重复终结。

#### 4.2.4 代码实践

**实践目标**：阅读 `Listen` 与 `_ProcessPipeThread`，画出服务端的线程模型图，并标注「连接级并发」与「处理级串行」分别发生在哪一行。

**操作步骤**：

1. 在 [WeaselServerImpl.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp) 中定位 `Run`（167）、`Listen`（408）、`_ProcessPipeThread`（428）、`g_api_mutex`（165）。
2. 画出四类线程：① 主线程（`CMessageLoop`）、② `pipeThread`（跑 `Listen`）、③ N 条 `_ProcessPipeThread`（每连接一条）、④ 隐含的「命令处理临界区」（同一时刻仅一条）。
3. 在图上用两种颜色区分「并发执行段」与「串行执行段」。

**需要观察的现象 / 预期结果**：

- 主线程与 `pipeThread` **并行**：一个跑消息循环，一个跑监听。
- 多条 `_ProcessPipeThread` **彼此并行**（分别阻塞在各自的 `_Receive` 上）。
- 但当多条工作线程**同时**拿到命令并调用 `listener` 时，`g_api_mutex` 会让它们**排队**进入 `HandlePipeMessage`——这是唯一的串行点。
- 若某条 `_ProcessPipeThread` 抛异常（客户端断开），只会被自己的 `catch(...)` 清理，不影响 `pipeThread` 与其他工作线程。

**进阶思考（待本地验证）**：如果你要在不破坏串行语义的前提下让「互不相关的命令」并行（比如 `ECHO` 与 `PROCESS_KEY_EVENT` 理论上无数据依赖），你会如何改造？提示：需要 `RequestHandler` 内部自行管理细粒度锁，而不能再依赖一把全局锁——但 librime 的线程安全性是这种改造的前置约束。

#### 4.2.5 小练习与答案

**练习 1**：`Listen` 里 `new boost::thread(...)` 创建的工作线程没有被保存到任何成员变量里，它会不会变成「失控线程」？为什么它仍然能被正确回收？

> **参考答案**：这条线程对象是临时的，离开 `try` 块后即析构，但 `boost::thread` 的析构是 **detach** 语义（不 join，线程继续跑）。线程本身靠 `_ProcessPipeThread` 内的 `catch(...)` → `_FinalizePipe(pipe)` 自行收尾：一旦客户端断开，`_Receive` 抛异常，线程清理管道后自然退出。换言之，工作线程的生命周期绑定在「这条管道连接」上，而不是绑定在 `ServerImpl` 对象上。主监听线程 `pipeThread` 才是被成员变量持有、用 `interrupt()` 主动终结的那一个。

**练习 2**：假如去掉 `g_api_mutex`，让多条工作线程同时进入 `HandlePipeMessage`，最可能在哪里出问题？

> **参考答案**：`HandlePipeMessage` 会调用 `RequestHandler`（即 `RimeWithWeaselHandler`）的方法，后者访问 librime 的会话表与引擎状态；同时多个 `OnXxx` 还会向同一个 `channel` 缓冲写响应正文（`*channel << msg`）。没有锁的话，会话状态读写竞争、响应正文交错都会发生。`g_api_mutex` 以「放弃命令级并行」换取了「单线程语义的安全性」——这是 Weasel 在 librime 非线程安全前提下做出的明确取舍。

---

### 4.3 ServerImpl 命令派发与菜单

#### 4.3.1 概念说明

工作线程拿到一条 `PipeMessage` 后，如何决定调用哪个 `OnXxx`？这就是 `HandlePipeMessage` 的职责——一张用宏写出来的 `switch` 派发表。它把 u2-l1 那张「命令↔派发函数」映射表落成代码：每条 `PIPE_MSG_HANDLE(命令, 函数)` 就是一个 `case`。

派发到 `OnXxx` 后，处理函数做两件事之一：

- **转发给 `RequestHandler`**：绝大多数命令都是这样，`OnXxx` 只是把 `wParam`/`lParam` 翻译成对 `m_pRequestHandler` 的调用。`RequestHandler` 由 u4 单元的 `RimeWithWeaselHandler` 实现，本讲把它当成黑盒。
- **回写响应正文**：对于会产生候选文本的命令（`PROCESS_KEY_EVENT`、`HIGHLIGHT_CANDIDATE_ON_CURRENT_PAGE`、`CHANGE_PAGE`、`START_SESSION`），`OnXxx` 会构造一个 `eat` 闭包（即 `EatLine` 回调）传给 `RequestHandler`；`RequestHandler` 在计算过程中调用 `eat(msg)`，`eat` 把 `msg` 经 `*channel << msg` 写进响应缓冲，最后 `HandlePipeMessage` 调 `resp(result)` 发出响应头。

此外，`ServerImpl` 还是一个 WTL 窗口，承担两类**非 IPC** 消息：托盘菜单命令（`WM_COMMAND`）与系统/配色消息（关机、深色模式切换）。所以本模块除了「管道命令派发」，还要讲「菜单与系统消息派发」。

#### 4.3.2 核心流程

命令派发与响应回写的伪代码：

```
HandlePipeMessage(pipe_msg, resp):
    switch(pipe_msg.Msg):
        ECHO                      → OnEcho:   return FindSession(lParam)
        START_SESSION             → OnStartSession:
                                        buf = channel->ReceiveBuffer()      # 客户端写来的正文
                                        eat = [](msg){ *channel << msg }    # 回写响应正文的回调
                                        return AddSession(buf, eat)         # 返回会话号
        PROCESS_KEY_EVENT         → OnKeyEvent:
                                        eat = [](msg){ *channel << msg }
                                        return ProcessKeyEvent(KeyEvent(wParam), lParam, eat)
        UPDATE_INPUT_POS          → OnUpdateInputPosition: 位运算还原 RECT → UpdateInputPosition(rc, lParam)
        TRAY_COMMAND              → OnCommand (重载): 走 WM_COMMAND 同一套菜单逻辑
        ... 其余命令同理 ...
    resp(result)                   # 把 DWORD 响应头写回管道
```

`eat` 是贯穿始终的关键对象：它是 `RequestHandler::EatLine`（`std::function<bool(std::wstring&)>`），本质是「把一段响应正文写进通道缓冲」的回调。`RequestHandler` 不关心管道细节，只管「算出一段文本就调一次 `eat`」；`eat` 在服务端闭包里捕获 `channel`，把文本塞进响应缓冲。这种设计把「业务（算字）」与「传输（写字节）」彻底解耦。

#### 4.3.3 源码精读

**(a) 宏派发表**

```cpp
template <typename _Resp>
void ServerImpl::HandlePipeMessage(PipeMessage pipe_msg, _Resp resp) {
  DWORD result;
  MAP_PIPE_MSG_HANDLE(pipe_msg.Msg, pipe_msg.wParam, pipe_msg.lParam)
  PIPE_MSG_HANDLE(WEASEL_IPC_ECHO, OnEcho)
  PIPE_MSG_HANDLE(WEASEL_IPC_START_SESSION, OnStartSession)
  ...
  PIPE_MSG_HANDLE(WEASEL_IPC_TRAY_COMMAND, OnCommand)
  END_MAP_PIPE_MSG_HANDLE(result);
  resp(result);
}
```

见 [WeaselServerImpl.cpp:377-403](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L377-L403)。三个宏（[WeaselServerImpl.cpp:361-375](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L361-L375)）展开后就是一个标准的 `switch`，把 `pipe_msg.Msg`（`WEASEL_IPC_COMMAND`）映射到对应 `OnXxx`，结果写入局部 `_result`，最后由 `END_MAP_PIPE_MSG_HANDLE(result)` 拷出。派发完成后调 `resp(result)`——这个 `resp` 就是 `_ProcessPipeThread` 传入的 `[this, pipe](Msg resp){ _Send(pipe, resp) }`，即把 `DWORD` 响应头写回这条管道。

注意一个细节：`WEASEL_IPC_TRAY_COMMAND` 被映射到 `OnCommand`（[WeaselServerImpl.cpp:399](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L399)）——这正是「IPC 命令」与「窗口菜单命令」的合流点（见 (d)）。

**(b) OnStartSession：读客户端正文 + 提供 eat**

```cpp
DWORD ServerImpl::OnStartSession(WEASEL_IPC_COMMAND uMsg, DWORD wParam, DWORD lParam) {
  if (!m_pRequestHandler) return 0;
  return m_pRequestHandler->AddSession(
      reinterpret_cast<LPWSTR>(channel->ReceiveBuffer()),
      [this](std::wstring& msg) -> bool { *channel << msg; return true; });
}
```

见 [WeaselServerImpl.cpp:194-205](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L194-L205)。这里把 4.1 讲的客户端发送时序对上号：

- 客户端 `_WriteClientInfo` 把 `app_name`/`type` 写进**请求正文** → `_Send` 连同 `START_SESSION` 头一起发出。
- 服务端 `_ProcessPipeThread` 的 `_Receive` 把正文读到 `channel` 的接收缓冲（u2-l2 讲过 `ERROR_MORE_DATA` 的续读）。
- `OnStartSession` 用 `channel->ReceiveBuffer()`（[PipeChannel.h:137](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L137)）拿到这段正文，当作宽字符缓冲交给 `AddSession`——后者（u4-l2）会解析出 `session.client_app` 等字段。
- `eat` 闭包 `[this](std::wstring& msg){ *channel << msg; return true; }` 传给 `AddSession`，使其能把会话初始化结果文本写进**响应缓冲**。
- `AddSession` 返回新会话号，最终经 `resp(result)` 回到客户端，成为客户端的 `session_id`。

**(c) OnKeyEvent：eat 回写的典型场景**

```cpp
DWORD ServerImpl::OnKeyEvent(WEASEL_IPC_COMMAND uMsg, DWORD wParam, DWORD lParam) {
  if (!m_pRequestHandler) return 0;
  auto eat = [this](std::wstring& msg) -> bool { *channel << msg; return true; };
  return m_pRequestHandler->ProcessKeyEvent(KeyEvent(wParam), lParam, eat);
}
```

见 [WeaselServerImpl.cpp:215-226](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L215-L226)。`wParam` 被包成 `KeyEvent(wParam)`，`lParam` 就是会话号。`ProcessKeyEvent`（u4-l2 详述）在 librime 里算字，过程中多次调用 `eat(...)` 把 `preedit`/候选/状态等文本一段段写进响应缓冲；返回值是「是否吃键」（非 0 为吃）。这个返回值经 `resp` 回到客户端，正是 4.1.4 表格里 `ProcessKeyEvent` 那一行的「非 0 = Server 吃键」。响应正文则留在缓冲里，等客户端调 `GetResponseData` 取走。

`OnHighlightCandidateOnCurrentPage` 与 `OnChangePage` 用的是同一个 `eat` 模式（[WeaselServerImpl.cpp:335-359](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L335-L359)），因为它们也会改变候选页、需要回传新的候选文本。

**(d) OnUpdateInputPosition：位运算还原 RECT**

客户端把 `RECT` 压进一个 `DWORD`（见 4.1.3 (e)），服务端这里把它还原（[WeaselServerImpl.cpp:253-293](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L253-L293)）：

```cpp
RECT rc;
int hi_res = (wParam >> 31) & 0x01;
rc.left  = ((wParam & 0x7ff) - (wParam & 0x800)) << hi_res;            // 12bit 有符号
rc.top   = (((wParam >> 12) & 0x7ff) - ((wParam >> 12) & 0x800)) << hi_res;
int height = ((wParam >> 24) & 0x7f) << hi_res;
rc.right  = rc.left + 6;     // 宽度固定 6
rc.bottom = rc.top + height;
```

`(x & 0x7ff) - (x & 0x800)` 是把 12 位**无符号**解释成 12 位**有符号**的经典位技巧：当最高位（符号位 `0x800`）为 1 时减去 `0x800` 即得负数。`hi_res` 控制是否左移一位恢复精度。之后还用 `PhysicalToLogicalPointForPerMonitorDPI`（动态从 user32.dll 取地址，[WeaselServerImpl.cpp:279-289](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L279-L289)）做高 DPI 物理→逻辑坐标换算，最后交给 `UpdateInputPosition(rc, lParam)`——这就是候选窗口「光标跟随」的数据来源。

**(e) 托盘菜单与系统消息：窗口消息派发**

`ServerImpl` 的 WTL 消息映射（[WeaselServerImpl.h:22-31](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.h#L22-L31)）登记了窗口消息到 `OnXxx` 的对应关系：

```cpp
BEGIN_MSG_MAP(WEASEL_IPC_WINDOW)
  MESSAGE_HANDLER(WM_CREATE, OnCreate)
  MESSAGE_HANDLER(WM_DESTROY, OnDestroy)
  MESSAGE_HANDLER(WM_CLOSE, OnClose)
  MESSAGE_HANDLER(WM_QUERYENDSESSION, OnQueryEndSystemSession)
  MESSAGE_HANDLER(WM_ENDSESSION, OnEndSystemSession)
  MESSAGE_HANDLER(WM_DWMCOLORIZATIONCOLORCHANGED, OnColorChange)
  MESSAGE_HANDLER(WM_SETTINGCHANGE, OnColorChange)
  MESSAGE_HANDLER(WM_COMMAND, OnCommand)
END_MSG_MAP()
```

`WM_COMMAND` 处理器是托盘菜单的总入口（[WeaselServerImpl.cpp:110-132](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L110-L132)）：

```cpp
LRESULT ServerImpl::OnCommand(UINT uMsg, WPARAM wParam, LPARAM lParam, BOOL& bHandled) {
  UINT uID = LOWORD(wParam);
  switch (uID) {
    case ID_WEASELTRAY_ENABLE_ASCII:
      m_pRequestHandler->SetOption(lParam, "ascii_mode", true); return 0;
    case ID_WEASELTRAY_DISABLE_ASCII:
      m_pRequestHandler->SetOption(lParam, "ascii_mode", false); return 0;
    default:;
  }
  auto it = m_MenuHandlers.find(uID);
  if (it == m_MenuHandlers.end()) { bHandled = FALSE; return 0; }
  it->second();   // 执行注册的命令回调
  return 0;
}
```

这里有两层派发：

1. **内置命令**：`ID_WEASELTRAY_ENABLE_ASCII` / `DISABLE_ASCII` 直接调 `RequestHandler::SetOption` 切换中/英文（`lParam` 是会话号）。
2. **注册命令**：其余菜单 ID 在 `m_MenuHandlers`（`std::map<UINT, CommandHandler>`，[WeaselServerImpl.h:97](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.h#L97)）里查，找到就执行其 `CommandHandler`（一个 `std::function<bool()>`）。这张表由 `AddMenuHandler(uID, handler)` 填充（[WeaselServerImpl.h:85-87](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.h#L85-L87)），具体填什么由 `WeaselServerApp` 在组装时决定（u6-l3 主题）。

注意 IPC 的 `WEASEL_IPC_TRAY_COMMAND` 命令也复用了这套逻辑：`HandlePipeMessage` 把它派给重载的 `OnCommand(WEASEL_IPC_COMMAND, DWORD, DWORD)`（[WeaselServerImpl.cpp:134-140](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L134-L140)），后者转调上面那个 `WM_COMMAND` 版本。于是无论「用户在托盘点菜单」还是「客户端通过管道发 `TRAY_COMMAND`」，最终都走同一张派发表——这是「双身份」带来的复用。

**(f) 配色与关机：系统消息处理**

- `OnColorChange`（[WeaselServerImpl.cpp:56-65](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L56-L65)）：监听 `WM_DWMCOLORIZATIONCOLORCHANGED` / `WM_SETTINGCHANGE`，检测系统深/浅色模式切换，调用 `RequestHandler::UpdateColorTheme(m_darkMode)` 通知 UI 换色（u4-l4 主题）。
- `OnEndSystemSession`（[WeaselServerImpl.cpp:99-108](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L99-L108)）：收到 `WM_ENDSESSION`（用户关机/注销）时调 `RequestHandler::Finalize()` 收尾引擎，再置空处理器指针。
- `OnQueryEndSystemSession` 直接返回 `TRUE`，允许系统关机。

**(g) pImpl 转发壳**

`Server` 类的方法同样只是转发给 `m_pImpl`（[WeaselServerImpl.cpp:442-471](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L442-L471)）：`Start`/`Stop`/`Run`/`SetRequestHandler`/`AddMenuHandler`/`GetHWnd` 各一行。其中 `GetHWnd` 暴露隐藏窗口句柄，供托盘图标（`WeaselTrayIcon`，u6-l3）归属使用。

#### 4.3.4 代码实践

**实践目标**：理解 `eat` 回调如何把响应正文写回管道，并学会「新增一个托盘菜单命令」需要动哪里。

**操作步骤（源码阅读型）**：

1. 在 [WeaselServerImpl.cpp:194-226](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L194-L226) 对比 `OnStartSession` 与 `OnKeyEvent` 的 `eat` 闭包，确认它们结构完全一致（都是 `[this](std::wstring& msg){ *channel << msg; return true; }`）。
2. 追踪 `eat` 的去向：它被传给 `RequestHandler::AddSession` / `ProcessKeyEvent`（[include/WeaselIPC.h:59-65](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L59-L65)）。在本讲把 `RequestHandler` 当黑盒即可，u4-l1/l2 会展示 `RimeWithWeaselHandler` 如何在算字过程中调用 `eat`。
3. 阅读托盘菜单派发 [WeaselServerImpl.cpp:110-132](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L110-L132) 与注册接口 [WeaselServerImpl.h:85-87](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.h#L85-L87)，列出「新增一个菜单命令」的改动清单。

**需要观察的现象 / 预期结果**（改动清单，仅作分析、不实际修改源码）：

要新增一个托盘菜单项（例如「重新部署」），在不改 IPC 协议的前提下需要：

| 改动点 | 位置 | 内容 |
| --- | --- | --- |
| 1. 菜单资源 ID | `resource.h` / `.rc` | 新增一个 `ID_WEASELTRAY_XXX` 常量与菜单项 |
| 2. 注册处理器 | `WeaselServerApp::SetupMenuHandlers`（u6-l3） | 调 `server.AddMenuHandler(ID_WEASELTRAY_XXX, [](){ /* 动作 */ })` |
| 3. （可选）复用 IPC | 本讲无需改动 | 若希望客户端也能触发，直接发 `WEASEL_IPC_TRAY_COMMAND` 带该 ID，会自动走 `OnCommand` 同一张表 |

注意「2」之所以在 `WeaselServerApp` 而不在 `ServerImpl`，是因为 `ServerImpl` 只提供「菜单 ID → 回调」的**通用机制**，具体填什么菜单、回调做什么，是组装层（`WeaselServerApp`）的职责——这正是 u2-l1 强调的「传输/派发与业务解耦」。

> 待本地验证：上表是结构分析。若你实际添加菜单项，需在 Windows 构建环境里重新编译 WeaselServer 并重启服务，才能在托盘看到新项。

#### 4.3.5 小练习与答案

**练习 1**：`OnStartSession` 用 `channel->ReceiveBuffer()` 取客户端写来的正文，而 `OnKeyEvent` 里似乎没有读 `ReceiveBuffer`——为什么按键命令不需要读请求正文？

> **参考答案**：按键命令把全部信息编码进了定长字段：`wParam` 是 `KeyEvent`（按键码），`lParam` 是 `session_id`，没有额外的变长正文。而 `START_SESSION` 需要带「客户端应用名/类型」这种变长文本，只能放进请求正文。这正是 u2-l1 所说的「`wParam`/`lParam` 的载荷语义随命令而变」——有的命令三字段够用，有的命令必须借助正文缓冲。

**练习 2**：`OnCommand` 既能由窗口 `WM_COMMAND` 触发，也能由 IPC 的 `WEASEL_IPC_TRAY_COMMAND` 触发。这两种来源传给 `OnCommand` 的参数含义是否一致？`ID_WEASELTRAY_ENABLE_ASCII` 那两行的 `lParam` 含义是什么？

> **参考答案**：两者最终都把 `LOWORD(wParam)` 当菜单 ID 查派发表，逻辑一致。差别在 `lParam`：窗口 `WM_COMMAND` 的 `lParam` 通常是控件句柄，而 IPC 路径下 `lParam` 是**会话号**（客户端 `TrayCommand` 经 `_SendMessage(WEASEL_IPC_TRAY_COMMAND, menuId, session_id)` 发来，见 [WeaselClientImpl.cpp:141-143](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L141-L143)）。所以 `SetOption(lParam, "ascii_mode", true)` 里的 `lParam` 是会话号，`SetOption` 据此定位到具体的输入上下文——这也解释了为什么托盘切英文需要知道「是哪个会话」。

---

## 5. 综合实践

把三个最小模块串起来，完成一次**端到端的「按键往返」追踪**。这是本讲的总练习。

**任务**：假设用户在记事本里按下一个键，请把以下七个阶段的代码位置与数据形态逐一标注，最终产出一张「两端对照」的完整时序图。

1. **客户端抓键**（u3-l2 主题，本讲从 `_SendMessage` 起接）：`ClientImpl::ProcessKeyEvent` 调 `_SendMessage(PROCESS_KEY_EVENT, keyEvent, session_id)`。
2. **客户端打包发送**：`_SendMessage` 构造 `PipeMessage{PROCESS_KEY_EVENT, keyEvent, session_id}`，`channel.Transact(req)` 内部 `_Send` 把定长头写进管道（无请求正文，`has_body=false`）。
3. **服务端接收**：某条 `_ProcessPipeThread` 的 `_Receive` 读到这条 `PipeMessage`，调 `listener(msg, resp)`。
4. **服务端串行化**：`listener` 闭包获取 `g_api_mutex`，进入 `HandlePipeMessage`。
5. **服务端派发**：宏 `switch` 把 `PROCESS_KEY_EVENT` 派给 `OnKeyEvent`（[WeaselServerImpl.cpp:215-226](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L215-L226)），构造 `eat`，调 `m_pRequestHandler->ProcessKeyEvent(...)`。
6. **服务端回写**：`RequestHandler`（u4-l2）算字过程中多次调 `eat(text)`，每次 `*channel << text` 把候选正文写进响应缓冲；返回「是否吃键」。
7. **客户端收尾**：`HandlePipeMessage` 调 `resp(result)` 把 `DWORD` 响应头写回管道 → 客户端 `_ReceiveResponse` 读到它 → `_SendMessage` 返回 → `ProcessKeyEvent` 据此返回 `bool`；候选正文仍留在客户端接收缓冲，等上层 `GetResponseData(handler)` 取走并交给 `ResponseParser`（u2-l5）。

**产出要求**：

- 画两条平行时间轴（客户端线程 / 服务端工作线程），用箭头表示管道写/读。
- 在「服务端工作线程」轴上标出 `g_api_mutex` 的 `lock`/`unlock` 区间，说明这段时间内其他工作线程即使拿到命令也只能等待。
- 用不同颜色区分「定长头（`PipeMessage`/`DWORD`）」与「变长正文（客户端信息/候选文本）」在管道上的流向。
- 标注每一步对应的源码文件与行号。

> 提示：这张图把 u2-l1（命令合同）、u2-l2（通道字节流）、本讲（两端实现）三者缝合成一张全景图，是后续 u2-l4（数据模型）、u2-l5（响应解析）的导航索引。完成后，你应该能一眼看出「为什么 `GetResponseData` 必须在 `ProcessKeyEvent` 之后调用」「为什么多客户端并发但命令处理仍是串行」。

---

## 6. 本讲小结

- `ClientImpl` 用 `session_id` 把状态分成两层：`_Connected()`（管道通不通）与 `_Active()`（能否发需要会话的命令）；几乎所有命令方法都是「`_Active()` 守卫 → `_SendMessage` → 用返回 `DWORD` 解释语义」的同一套模板。
- `_SendMessage` 是客户端唯一出口：把三字段打包成 `PipeMessage` 调 `channel.Transact`，并用 `catch(DWORD)` 把管道异常退化成返回值 `0`，让「Server 不可用」等价于「不吃键」的安全放行。
- `StartSession` 是客户端的核心：先 `_WriteClientInfo` 把应用名/类型写进请求正文，再发 `START_SESSION` 拿回会话号；`Echo()` 提供轻量会话存活探测，避免无谓重建会话。
- 服务端线程模型是「连接级并发 + 处理级串行」：`Listen` 为每个管道连接派生 `_ProcessPipeThread`，但 `Run` 里的 `g_api_mutex` 把所有 `HandlePipeMessage` 串成一条，规避了 librime 的并发风险。
- `HandlePipeMessage` 是一张宏 `switch` 派发表，把 `PipeMessage.Msg` 映射到 `OnXxx`；后者把 `wParam`/`lParam` 翻译成对 `RequestHandler` 的调用，并用 `eat` 闭包 `[this](msg){ *channel << msg }` 让业务层把响应正文写回通道缓冲——业务（算字）与传输（写字节）由此解耦。
- `ServerImpl` 兼具「IPC 派发器」与「WTL 隐藏窗口」双重身份：窗口消息（托盘 `WM_COMMAND`、关机 `WM_ENDSESSION`、配色 `WM_SETTINGCHANGE`）与 IPC 命令（`TRAY_COMMAND`）共用同一张 `OnCommand` 菜单派发表，`AddMenuHandler` 提供了扩展点。

---

## 7. 下一步学习建议

本讲把「命令在两端如何被封装、传输、派发」讲到了 `RequestHandler` 这一抽象边界。`RequestHandler` 的另一侧是什么？是 librime。接下来的两讲分别从两个方向深入：

- **u2-l4（数据模型与 boost 序列化）**：先看清 `RequestHandler` 与 IPC 之间传递的「货物」——`Context` / `Status` / `CandidateInfo` / `UIStyle` 这些结构体的字段含义，以及它们如何被序列化。这是理解 `eat` 回调写出的文本正文的「词汇表」。
- **u2-l5（响应解析、反序列化与动作分发）**：再看客户端 `GetResponseData` 拿到那段文本后，`ResponseParser` / `Deserializer` / `ActionLoader` 如何把它一行行解析回 `Context`/`Status`/`UIStyle` 更新。与本讲的 `eat` 回写正好组成一个闭环。

读完 u2-l4、u2-l5，IPC 单元就闭合了。之后建议进入：

- **u4 单元（Rime 引擎桥接）**：实现 `RequestHandler` 的 `RimeWithWeaselHandler` 在那里登场，你会看到 `OnKeyEvent` 调用的 `ProcessKeyEvent` 内部究竟如何驱动 librime，以及 `eat` 回调在算字过程中被调用的真实时机。
- **u6-l3（系统托盘、服务进程与自动更新）**：那里会展示 `WeaselServerApp` 如何把本讲的 `Server`、`RequestHandler`、UI、托盘菜单（`AddMenuHandler`）组装成一个完整的服务进程，并讲解命令行参数与单实例重启。
