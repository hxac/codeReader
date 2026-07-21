# IPC 接口、命令协议与请求处理器

> 本讲属于「进程间通信 IPC 骨架」单元（u2）的第一讲。
> 在 u1-l1 中我们已经建立了全局地图：WeaselTSF（DLL，驻留每个应用进程）通过命名管道把按键事件转发给全局唯一的 WeaselServer（EXE，托管 librime 引擎与候选窗口 UI）。
> 本讲要钻进这条管道的「合同」本身——前后端到底约定了哪些命令、用什么数据格式收发、又是怎么用一套抽象接口把命令派发到引擎的。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `Client`、`Server`、`RequestHandler` 三个抽象接口各自的职责边界，以及谁在 TSF 端用 `Client`、谁在 Server 端实现 `RequestHandler`。
- 逐条列出 `WEASEL_IPC_COMMAND` 枚举里的全部命令，并解释会话生命周期（建立→按键→结束）由哪些命令串联。
- 理解 `PipeMessage` 这个定长消息头如何承载「命令 + 两个 32 位参数」，以及为什么命令编号从 `WM_APP + 1` 开始。
- 解释 `GetPipeName()` 如何用当前 Windows 用户名拼出管道路径，从而实现「按用户隔离」的多用户安全语义。
- 动手排出一张「RequestHandler 虚函数 ↔ IPC 命令 ↔ Client 方法 ↔ Server 派发函数」的四列映射表。

## 2. 前置知识

阅读本讲前，你需要大致了解以下概念（不熟悉也没关系，下面会用一句话带过）：

- **命名管道（Named Pipe）**：Windows 提供的一种进程间通信通道，有一个全局唯一的名字（形如 `\\.\pipe\xxx`），任意进程都能凭名字打开同一根管道来收发字节流。Weasel 用它做 TSF 端 ↔ Server 端的传输层。
- **抽象基类 / 虚函数**：C++ 里用 `virtual` 声明、末尾 `= 0` 的函数是「纯虚函数」，含纯虚函数的类是接口。本讲的 `RequestHandler` 就是一个接口，真正的引擎桥接逻辑由它的子类（`RimeWithWeaselHandler`，u4 会讲）实现。
- **WM_APP**：Windows 消息编号的一个分界线（值为 `0x8000`）。`WM_APP` 及以上的编号留给应用程序自定义，不会和系统消息冲突。你会看到 Weasel 的命令编号恰好从这个范围开始——这是一段历史遗留，下文会解释。
- **会话（Session）**：一次「某个应用进程 ↔ 引擎」的连续对话。每个会话有一个 32 位整数 ID，按键、上屏、选词都带着这个 ID，引擎才能知道把候选结果回给谁。

如果你已经读过 u1-l1，那么「TSF 端是瘦客户端、Server 端是胖服务、二者经管道通信」这张图应当还在脑海里——本讲就是把那条管道拆开看合同。

## 3. 本讲源码地图

本讲几乎全部内容都集中在两个公共头文件里，辅以两个实现文件做对照验证：

| 文件 | 作用 | 本讲用到的地方 |
| --- | --- | --- |
| `include/WeaselIPC.h` | IPC 的总接口契约：命令枚举、消息结构、三大抽象接口、管道命名 | 全讲核心 |
| `include/WeaselIPCData.h` | 跨管道传输的数据结构（`Context`/`Status`/`UIStyle` 等）及其序列化 | 4.2 简要提及，详解留给 u2-l4 |
| `include/WeaselUtility.h` | 工具函数，其中 `getUsername()` 决定管道名 | 4.3 管道命名 |
| `WeaselIPC/WeaselClientImpl.cpp` | `Client` 接口的客户端实现，展示每个方法对应哪条命令 | 4.1、4.4 命令映射 |
| `WeaselIPCServer/WeaselServerImpl.cpp` | `Server` 接口的服务端实现，展示命令如何派发到 `RequestHandler` | 4.4 派发链路 |

> 提示：本讲引用的实现文件属于 `WeaselIPC`（客户端静态库）和 `WeaselIPCServer`（服务端静态库）两个子工程，它们都依赖 `include/` 下的公共头。工程依赖关系在 u1-l2 已梳理过。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 三大接口**：`Client` / `Server` / `RequestHandler`
2. **4.2 IPC 命令枚举与 `PipeMessage` 消息格式**
3. **4.3 管道命名与共享内存常量**
4. **4.4 命令派发全链路**（把前三个模块串起来的综合模块）

### 4.1 三大接口：Client / Server / RequestHandler

#### 4.1.1 概念说明

Weasel 的 IPC 层刻意把「谁能发起请求」「谁能响应请求」「请求最终被谁处理」三种角色拆开，分别用三个抽象来描述：

- **`Client`（客户端接口）**：被 **TSF 端**（`WeaselTSF.dll` 驻留在每个应用进程里）使用。它提供一组人类友好的方法，比如 `ProcessKeyEvent()`、`StartSession()`、`UpdateInputPosition()`。TSF 代码只跟这个接口打交道，完全不需要知道管道细节。
- **`Server`（服务端接口）**：被 **`WeaselServer.exe`** 使用。它负责「把服务跑起来」——创建窗口、进入消息循环、注册谁来处理请求、注册托盘菜单回调。
- **`RequestHandler`（请求处理器，抽象基类）**：定义「服务端到底能干哪些事」的虚函数表。`Server` 自己不实现这些能力，而是持有一个 `RequestHandler*` 指针，把收到的命令转交给它。真正的实现是 u4 要讲的 `RimeWithWeaselHandler`（里面调 librime 的 C API）。

这样拆分的好处是 **解耦**：IPC 传输层（管道读写）不关心引擎；引擎层（librime）不关心传输。本讲的两个测试工程（u7-l1）能脱离真实引擎做端到端 IPC 验证，正是因为 `RequestHandler` 可以被一个「假实现」替换掉。

#### 4.1.2 核心流程

三个角色的协作流程可以用下面这段伪代码示意：

```
# TSF 端（每个应用进程里）
client.Connect()                 # 连上命名管道（必要时拉起 Server）
client.StartSession()            # → 返回 session_id
client.ProcessKeyEvent(ke)       # 把一个按键交给引擎
resp = client.GetResponseData()  # 取回候选 / 上屏文本
...
client.EndSession()              # 关闭会话

# Server 端（全局唯一）
server.SetRequestHandler(&handler)   # 注入 RequestHandler 实现
server.AddMenuHandler(id, fn)        # 注入托盘菜单回调
hwnd = server.Start()                # 建窗口、起监听线程
server.Run()                         # 进入消息循环，直到 WM_QUIT
```

`Client` 的每个方法背后都对应一次「往管道写一条命令」；`Server` 收到命令后查表，调用 `RequestHandler` 上对应的虚函数。这层「方法 ↔ 命令 ↔ 虚函数」的对照关系，正是 4.4 要排出的映射表。

#### 4.1.3 源码精读

先看 `RequestHandler` 抽象基类，它定义了服务端的全部能力（注意每个虚函数都有默认空实现，便于子类按需重写）：

[include/WeaselIPC.h:52-84](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L52-L84) —— `RequestHandler` 抽象基类，定义了 18 个虚函数（含构造/析构外的全部能力），以及一个关键的类型别名 `EatLine`。

`EatLine` 是理解整张映射表的钥匙：

```cpp
using EatLine = std::function<bool(std::wstring&)>;
```

它是「服务端把响应文本一行行喂回客户端」的回调。带 `eat` 参数的虚函数（如 `ProcessKeyEvent`、`AddSession`、`HighlightCandidateOnCurrentPage`、`ChangePage`）意味着这次请求 **需要回传候选/状态文本**；不带 `eat` 的（如 `FocusIn`、`UpdateInputPosition`）则是「单向通知」，服务端处理完不必回文本。这层差异会在 4.4 里再次出现。

> `RequestHandler` 上没有「Shutdown」之类的虚函数——关停服务是 `Server` 自己的事，不该让引擎处理器负责。这是职责划分的一个细节。

再看 `Client` 接口，它把管道细节完全藏了起来，对外只暴露业务方法：

[include/WeaselIPC.h:102-148](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L102-L148) —— `Client` 接口。注意它持有一个 `ClientImpl* m_pImpl`（第 147 行），这是 **pImpl（指向实现的指针）** 手法：真正的管道读写逻辑写在 `ClientImpl`（`WeaselIPC/WeaselClientImpl.cpp`）里，接口头只暴露稳定的抽象，更换传输实现时不影响调用方。

`Server` 接口同样用 pImpl 藏起 `ServerImpl`：

[include/WeaselIPC.h:150-168](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L150-L168) —— `Server` 接口。`Start()` 返回 `HWND`（服务端会建一个隐藏窗口，既用于单实例互斥，也用于接收系统消息），`SetRequestHandler()` 把引擎处理器接进来，`AddMenuHandler()` 注册托盘菜单项回调。

接口旁还定义了三个 `typedef`，分别抽象「响应处理」「命令处理」「服务启动」三类回调：

[include/WeaselIPC.h:86-93](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L86-L93) —— `ResponseHandler`（读服务端返回数据时逐块回调）、`CommandHandler`（无参布尔回调，用于菜单/事件）、`ServerLauncher`（拉起服务进程的回调，类型同 `CommandHandler`）。

#### 4.1.4 代码实践

**实践目标**：在不打开实现文件的前提下，仅凭 `WeaselIPC.h` 三个接口，推断「哪个角色在哪一端运行」。

**操作步骤**：

1. 打开 [include/WeaselIPC.h:102-148](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L102-L148)，给 `Client` 的每个公有方法分类：「会话类」「按键类」「候选交互类」「焦点/位置类」「生命周期类」。
2. 对照 [include/WeaselIPC.h:52-84](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L52-L84) 的 `RequestHandler` 虚函数，把 `Client` 方法名和虚函数名 **配对**（你会发现大量同名，比如 `ProcessKeyEvent`、`FocusIn`、`UpdateInputPosition`）。
3. 思考：为什么 `Client` 没有 `FindSession` / `Initialize` / `Finalize` / `SetOption` / `UpdateColorTheme` 方法？这些能力由谁触发？（答案见 4.4.5）

**需要观察的现象**：`Client` 的方法集是 `RequestHandler` 虚函数集的 **真子集**——客户端能发起的请求，只是服务端能力的一部分；另一部分由服务端自己（窗口消息、托盘菜单、系统事件）触发。

**预期结果**：你应当发现客户端缺少 `FindSession`（服务端自检，`Echo` 命令复用它）、`Initialize/Finalize`（服务启停时调用）、`SetOption`（托盘菜单触发）、`UpdateColorTheme`（系统主题变化触发）。

#### 4.1.5 小练习与答案

**练习 1**：`Client` 和 `Server` 都用了 `m_pImpl` 指针隐藏实现。这种手法（pImpl）给 Weasel 带来什么好处？

> **答案**：把管道读写的全部细节（缓冲、线程本地句柄、boost 流）关在 `.cpp` 和私人头里，公共接口头 `WeaselIPC.h` 只暴露稳定的抽象类。这意味着：调用方（TSF、Server）`#include <WeaselIPC.h>` 时不必拖入 `PipeChannel.h`、`boost` 等重型依赖，编译耦合度大幅降低；将来更换传输层也只需改 `Impl`，不影响调用方。

**练习 2**：`RequestHandler` 的虚函数为什么都给一个默认空实现（比如 `virtual void FocusIn(...) {}`），而不是纯虚 `= 0`？

> **答案**：因为不是每个实现都需要重写全部能力。比如 u7 的测试用 `RequestHandler` 假实现时，往往只关心按键；默认空实现让子类「按需重写」，避免强迫每个子类都写一堆空函数。只有真正需要的能力才覆盖。

---

### 4.2 IPC 命令枚举与 PipeMessage 消息格式

#### 4.2.1 概念说明

接口层的方法名（`ProcessKeyEvent`）是给人读的；真正在管道里跑的字节需要一个机器友好的「命令编号」。`WEASEL_IPC_COMMAND` 枚举就是这套编号表，它把客户端可能发起的所有请求列成一张清单。每条跑在管道上的消息用一个定长结构体 `PipeMessage` 包装，只有三个字段：**命令编号 + 两个 32 位参数**。这是一种极简的「消息头 + 载荷」设计。

#### 4.2.2 核心流程

一条命令在管道上的形态：

```
PipeMessage {
  WEASEL_IPC_COMMAND Msg;   // 命令编号，如 WEASEL_IPC_PROCESS_KEY_EVENT
  DWORD wParam;             // 参数 1：含义随命令而变（按键码 / 候选序号 / 压缩坐标 ...）
  DWORD lParam;             // 参数 2：通常是 session_id
}
```

`wParam` / `lParam` 这两个名字直接借用自 Win32 的 `WPARAM`/`LPARAM`（窗口消息的两个参数），语义也类似：**具体含义由命令决定**。例如：

- `WEASEL_IPC_PROCESS_KEY_EVENT`：`wParam` = 打包成 32 位的 `KeyEvent`，`lParam` = `session_id`。
- `WEASEL_IPC_SELECT_CANDIDATE_ON_CURRENT_PAGE`：`wParam` = 候选序号 `index`，`lParam` = `session_id`。
- `WEASEL_IPC_UPDATE_INPUT_POS`：`wParam` = 把 `RECT`（光标位置矩形）**位压缩**进一个 32 位整数，`lParam` = `session_id`。

位压缩的位分配在 `WeaselClientImpl.cpp:109-129` 的注释里有完整说明（高位 1 bit 是高分辨率标志，随后 7 bit 高度、12 bit top、12 bit left）。这套压缩是为了让一个矩形塞进单个 `DWORD`，省去额外的载荷往返。

> 关于命令编号的起点：`WEASEL_IPC_ECHO = (WM_APP + 1)`（`WM_APP = 0x8000`）。这是历史遗留——Weasel 早期的 IPC 是基于隐藏窗口的 `SendMessage`/窗口消息实现的，命令编号必须落在 `WM_APP` 以上的自定义区间以免和系统消息冲突。后来改用命名管道传输，但编号值原样保留，所以你看到的命令编号都很大（32769 起）。`WEASEL_IPC_WINDOW` 这个窗口类名（第 9 行）也是同一时期的化石。

#### 4.2.3 源码精读

先看命令枚举全集（按定义顺序，连续递增）：

[include/WeaselIPC.h:18-36](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L18-L36) —— `WEASEL_IPC_COMMAND` 枚举。共 16 个具名命令 + 1 个哨兵 `WEASEL_IPC_LAST_COMMAND`，编号从 `WM_APP + 1` 开始连续递增。

按语义可以把这 16 条命令分成五组：

| 分组 | 命令 | 用途 |
| --- | --- | --- |
| 连通性 | `ECHO` | 探活/校验会话是否还在 |
| 会话生命周期 | `START_SESSION` / `END_SESSION` | 建立 / 销毁一个会话 |
| 核心 | `PROCESS_KEY_EVENT` | 把一个按键交给引擎（最高频命令） |
| 写作控制 | `COMMIT_COMPOSITION` / `CLEAR_COMPOSITION` | 上屏 / 清空当前写作串 |
| 候选交互 | `SELECT_CANDIDATE_ON_CURRENT_PAGE` / `HIGHLIGHT_CANDIDATE_ON_CURRENT_PAGE` / `CHANGE_PAGE` | 鼠标点选 / hover 高亮 / 翻页 |
| 焦点与定位 | `FOCUS_IN` / `FOCUS_OUT` / `UPDATE_INPUT_POS` | 应用获焦/失焦/光标移动（定位候选窗口） |
| 维护模式 | `START_MAINTENANCE` / `END_MAINTENANCE` | 重新部署期间暂停输入 |
| 控制 | `SHUTDOWN_SERVER` / `TRAY_COMMAND` | 关停服务 / 托盘菜单命令转发 |

再看承载命令的定长消息结构：

[include/WeaselIPC.h:39-49](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L39-L49) —— `PipeMessage`（命令 + wParam + lParam）和 `IPCMetadata`（携带服务端窗口句柄与窗口类名，供客户端定位服务端窗口）。

`PipeMessage` 的体积很关键，它决定了共享内存缓冲的尺寸（见 4.3）。`IPCMetadata` 则保留自窗口消息时代——客户端可以从中读到服务端窗口句柄 `server_hwnd`，用于一些跨进程窗口操作。

#### 4.2.4 代码实践

**实践目标**：通过客户端实现，验证「每个命令的 `wParam`/`lParam` 到底塞了什么」。

**操作步骤**：

1. 打开 [WeaselIPC/WeaselClientImpl.cpp:54-65](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L54-L65)，读 `ShutdownServer()` 和 `ProcessKeyEvent()` 两个方法。
2. 注意 `ProcessKeyEvent` 的调用：`_SendMessage(WEASEL_IPC_PROCESS_KEY_EVENT, keyEvent, session_id)`——`keyEvent`（一个 `KeyEvent`，32 位）放进 `wParam`，`session_id` 放进 `lParam`。
3. 再读 [WeaselIPC/WeaselClientImpl.cpp:99-104](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L99-L104) 的 `ChangePage(bool backward)`：`backward` 这个 `bool` 直接当 `wParam` 传。

**需要观察的现象**：同一个 `wParam` 字段，在不同命令里承载完全不同类型的数据（按键码、候选序号、bool、压缩矩形）——类型安全完全靠「命令编号 + 双方约定」保证。

**预期结果**：你会理解为什么 `_SendMessage` 的签名是 `(WEASEL_IPC_COMMAND, DWORD, DWORD)` 而不是强类型——`DWORD` 是万能容器，语义由命令决定。

**待本地验证**：如果想直观看到字节流，可在 [WeaselIPC/WeaselClientImpl.cpp:193-202](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L193-L202) 的 `_SendMessage` 里临时加一行 `DEBUG << L"send cmd=" << Msg ...;`（项目自带的 `DebugStream` 宏，见 `WeaselUtility.h`），用 DebugView 观察输出。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `WEASEL_IPC_LAST_COMMAND` 没有对应的处理逻辑，却要定义它？

> **答案**：它是枚举末尾的「哨兵」值，本身不代表任何命令，主要用于：① 遍历或校验命令范围（`cmd < WEASEL_IPC_LAST_COMMAND`）；② 未来在末尾追加新命令时不必改既有值。这是 C 风格枚举的常见收尾写法。

**练习 2**：`UPDATE_INPUT_POS` 的 `wParam` 用一个 `DWORD` 装下整个 `RECT`，而不是直接发 16 字节的 `RECT` 结构。这种位压缩的代价和收益各是什么？

> **答案**：收益是 `PipeMessage` 保持定长（12 字节），一次写即可，无需附加可变载荷，简化管道协议；代价是字段位数受限（left/top 各 12 位有符号，高度 7 位），需要高分辨率标志位 `hi_res` 扩展，且编解码两端必须严格对齐（编在 `WeaselClientImpl.cpp:106-130`，解在 `WeaselServerImpl.cpp:270-277`）。这是「用复杂度换紧凑」的典型取舍。

---

### 4.3 管道命名与共享内存常量

#### 4.3.1 概念说明

命令和消息格式解决了「说什么」，本模块解决「往哪说」。命名管道靠 **名字** 寻址——只要客户端和服务端用同一个名字打开管道，就能互通。Weasel 把当前 Windows 用户名嵌进管道名，从而实现 **按用户隔离**：同一台机器上两个不同 Windows 账户各自启动的 WeaselServer 互不干扰，各自的输入会话、用户词典完全隔离。

此外，头文件顶部还定义了一组缓冲尺寸常量，决定了单次 IPC 往返能传多大文本（候选列表、preedit、UI 样式等都装在这块缓冲里）。这组常量在 u2-l2 讲 `PipeChannel` 时会被反复引用，本讲先建立印象。

#### 4.3.2 核心流程

管道命名规则：

```
\\.\pipe\<用户名>\WeaselNamedPipe
```

即 `GetPipeName()` 把三段拼起来：固定前缀 `\\.\pipe\` + `getUsername()` + 分隔符 `\` + 常量 `WEASEL_IPC_PIPE_NAME`（值为 `L"WeaselNamedPipe"`）。

缓冲尺寸体系（全部在 `WeaselIPC.h` 顶部）：

```
WEASEL_IPC_METADATA_SIZE     = 1024          // 元数据缓冲
WEASEL_IPC_BUFFER_SIZE       = 4 * 1024      // 文本载荷缓冲（4 KiB 字节）
WEASEL_IPC_BUFFER_LENGTH     = BUFFER_SIZE / sizeof(WCHAR)  // = 2048 个宽字符
WEASEL_IPC_SHARED_MEMORY_SIZE = sizeof(PipeMessage) + WEASEL_IPC_BUFFER_SIZE
```

含义：每次 IPC 往复的「数据通道」最多承载 2048 个 `wchar_t`（约 2048 个汉字）的响应文本，外加一个定长 `PipeMessage` 头。这 2048 宽字符要装下 preedit、候选列表、注释、标签、状态、UI 样式——所以候选词太多时会有截断（候选数量上限另有 `MAX_CANDIDATE_COUNT` 约束，见 u5）。

#### 4.3.3 源码精读

管道名常量与缓冲尺寸常量定义在头文件最顶部：

[include/WeaselIPC.h:9-16](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L9-L16) —— `WEASEL_IPC_WINDOW`（遗留窗口类名）、`WEASEL_IPC_PIPE_NAME`（管道名常量）、四个缓冲尺寸常量，以及由它们推导出的 `WEASEL_IPC_SHARED_MEMORY_SIZE`。

管道名拼接函数 `GetPipeName()` 是一个 `inline` 函数，定义在 `weasel` 命名空间里：

[include/WeaselIPC.h:170-177](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L170-L177) —— `GetPipeName()`：把前缀、用户名、分隔符、管道名常量拼成完整路径。

其中 `getUsername()` 调用 Win32 的 `GetUserName` 取当前登录用户名：

[include/WeaselUtility.h:14-32](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUtility.h#L14-L32) —— `getUsername()`：两次调用 `GetUserName`（第一次取长度，第二次取名字），返回当前用户名 `std::wstring`。

客户端和服务端 **都调用同一个 `GetPipeName()`**，所以两端能算出同一个名字：

```cpp
// 客户端（WeaselClientImpl.cpp 构造函数，第 8 行）
ClientImpl::ClientImpl()
    : session_id(0), channel(GetPipeName()), is_ime(false) { ... }

// 服务端（WeaselServerImpl.cpp 构造函数，第 34 行）
ServerImpl::ServerImpl()
    : ..., channel(std::make_unique<PipeServer>(GetPipeName(), sa.get_attr())) { ... }
```

> 安全语义：因为名字里嵌了用户名，而 Windows 命名管道的路径 `\\.\pipe\<user>\...` 可结合 `SECURITY_ATTRIBUTES`（服务端构造里传入的 `sa.get_attr()`）做 ACL 限制，确保只有同用户进程能连进来——防止其他用户的进程偷读你的按键。这一点在服务端创建管道时落地，u2-l3 会详述。

#### 4.3.4 代码实践

**实践目标**：亲手算出你这台机器上 Weasel 管道的完整名字，并理解它为何随账户变化。

**操作步骤**：

1. 假设当前 Windows 用户名是 `alice`，按 `GetPipeName()` 的拼接规则写出完整管道名：`\\.\pipe\alice\WeaselNamedPipe`。
2. 思考：如果机器上有 `alice` 和 `bob` 两个账户同时登录、各自启动了 `WeaselServer.exe`，它们的管道名分别是什么？会不会冲突？
3. 进一步思考：`getUsername()` 取的是「当前进程所属用户」。TSF DLL 被加载进 `explorer.exe` 或某应用进程时，这些进程和 `WeaselServer.exe` 是不是同一个用户？这为什么是「能连上」的前提？

**需要观察的现象**：管道名完全由「登录用户名」这一变量决定，其余都是常量。

**预期结果**：`alice` 的管道是 `\\.\pipe\alice\WeaselNamedPipe`，`bob` 的是 `\\.\pipe\bob\WeaselNamedPipe`，两者互不相同、互不干扰。TSF 端和 Server 端只有运行在同一用户下，`getUsername()` 才返回同值，才能连上同一根管道。

**待本地验证**：在 Windows 上可用 PowerShell 的 `Get-ChildItem \\.\pipe\ | Where-Object Name -like '*WeaselNamedPipe'` 列出实际存在的管道实例，验证名字里确实含用户名。

#### 4.3.5 小练习与答案

**练习 1**：把 `WEASEL_IPC_BUFFER_SIZE` 从 4 KiB 改成 1 KiB，会直接影响什么功能？

> **答案**：`WEASEL_IPC_BUFFER_LENGTH` 会从 2048 降到 512 个宽字符，单次响应能承载的文本变短。首当其冲是「单页候选数较多 + 每条候选带长注释」的场景：preedit + 候选 + 注释 + 标签的序列化文本可能超出 512 字符而被截断，导致候选窗口显示不全或 UI 样式更新丢失。这是一个牵一发动全身的常量。

**练习 2**：为什么 `GetPipeName()` 是 `inline` 函数放在头文件里，而不是放在某个 `.cpp` 里编译？

> **答案**：因为它要被 **客户端库**（`WeaselIPC`）和 **服务端库**（`WeaselIPCServer`）两个独立编译单元共同使用。`inline` 函数定义在头里可被多个 `.cpp` 包含而不会违反 ODR（一次定义规则），保证两端拼出的名字逐字节一致。若放 `.cpp`，则两个库要么各自实现一份（容易写不一致），要么产生链接符号冲突。

---

### 4.4 命令派发全链路（综合串联）

#### 4.4.1 概念说明

前三个模块分别讲了「角色」「消息格式」「寻址」。本模块把它们串成一条完整的命令派发链路，让你看清：**你在 TSF 端调用 `client.ProcessKeyEvent(ke)` 这一行，到底经过了哪些环节，才最终触发服务端 `RequestHandler::ProcessKeyEvent(...)` 这个虚函数。**

理解这条链路是本讲最重要的产出，因为：

- 它解释了为什么 `Client` 方法名和 `RequestHandler` 虚函数名几乎一一对应（中间只是把方法调用「翻译」成命令编号，再「翻译」回虚函数调用）。
- 它揭示了 `EatLine` 回调的真实用途——把服务端的文本响应顺着同一条管道回写给客户端。
- 它是 u2-l2（`PipeChannel` 传输细节）、u2-l3（客户端/服务端实现）和 u2-l5（响应解析）的总索引。

#### 4.4.2 核心流程

一次 `client.ProcessKeyEvent(ke)` 的完整往返：

```
[TSF 端 / 应用进程]                              [Server 端 / WeaselServer.exe]
Client::ProcessKeyEvent(ke)                       
  └─ ClientImpl::ProcessKeyEvent(ke)              
       ├─ _SendMessage(PROCESS_KEY_EVENT, ke, sid) 
       │    └─ channel.Transact(PipeMessage{...})  ──写管道──►
       │                                                            PipeServer 监听线程收到 PipeMessage
       │                                                            └─ HandlePipeMessage(msg, resp)
       │                                                                 └─ switch: PROCESS_KEY_EVENT → OnKeyEvent()
       │                                                                      └─ handler->ProcessKeyEvent(ke, sid, eat)
       │                                                                           └─ (RimeWithWeaselHandler 真正调 librime)
       │                                                                           └─ eat(respText) 把候选/状态文本写回管道
       │                                              ◄──写管道── resp(result)
       │    ◄── Transact 返回 DWORD 结果 ◄──读管道──
       └─ 返回 ret != 0
```

四个翻译环节：

1. **方法 → 命令**：`ClientImpl::ProcessKeyEvent` 把方法调用翻译成 `WEASEL_IPC_PROCESS_KEY_EVENT` 命令（在 `_SendMessage` 里）。
2. **命令 → 字节**：`_SendMessage` 构造 `PipeMessage{Msg, wParam, lParam}`，交给 `channel.Transact()` 写进管道（传输细节 u2-l2 讲）。
3. **字节 → 派发**：服务端监听线程收到 `PipeMessage`，在 `HandlePipeMessage` 的 `switch` 里把命令编号映射到具体的 `OnXxx()` 处理函数。
4. **派发 → 虚函数**：`OnXxx()` 调用 `m_pRequestHandler->Xxx(...)`，把请求交给引擎处理器。

#### 4.4.3 源码精读

**环节 1+2：客户端「方法 → 命令 → 管道字节」**

`_SendMessage` 是所有客户端命令的统一出口，它把任意命令打包成 `PipeMessage` 并发起一次 `Transact`（请求-响应往返）：

[WeaselIPC/WeaselClientImpl.cpp:193-202](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L193-L202) —— `_SendMessage`：构造 `PipeMessage req{Msg, wParam, lParam}`，调用 `channel.Transact(req)` 并返回服务端回的 `LRESULT`；捕获 `DWORD` 异常（管道断开等）时返回 0。

典型的命令封装（按键处理）就在它上面：

[WeaselIPC/WeaselClientImpl.cpp:58-65](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L58-L65) —— `ProcessKeyEvent`：先 `_Active()` 检查会话是否在线，再 `_SendMessage(WEASEL_IPC_PROCESS_KEY_EVENT, keyEvent, session_id)`，返回值非 0 表示按键被引擎「吃掉」。

> `StartSession` 略有不同——它在发命令前，会先用 `channel << L"action=session\n"...` 往管道写一段 **文本**（客户端应用名、类型），服务端 `AddSession` 通过 `channel->ReceiveBuffer()` 读到这段文本。这是「命令头 + 文本载荷」的混合用法，详见 [WeaselClientImpl.cpp:145-152](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L145-L152) 与 [WeaselClientImpl.cpp:185-191](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L185-L191) 的 `_WriteClientInfo`。

**环节 3：服务端「字节 → 派发」**

服务端用一个宏构造的 `switch` 把命令编号映射到 `OnXxx` 处理函数。这套宏（`MAP_PIPE_MSG_HANDLE` / `PIPE_MSG_HANDLE` / `END_MAP_PIPE_MSG_HANDLE`）本质上就是一个命令分发表：

[WeaselIPCServer/WeaselServerImpl.cpp:377-403](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L377-L403) —— `HandlePipeMessage`：把 `PipeMessage` 的 `Msg` 字段 `switch` 到对应 `OnXxx`，把结果通过 `resp(result)` 回写给客户端。

注意这里 **每条命令都对应一个 `OnXxx` 函数**，且 `WEASEL_IPC_TRAY_COMMAND` 被路由到 `OnCommand`（复用了窗口菜单命令的处理路径）。

**环节 4：「派发 → 虚函数」+ `EatLine` 回写**

以按键为例，看 `OnKeyEvent` 如何把请求交给 `RequestHandler`，并用 `eat` lambda 把响应文本回写管道：

[WeaselIPCServer/WeaselServerImpl.cpp:215-226](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L215-L226) —— `OnKeyEvent`：构造 `eat = [this](std::wstring& msg) { *channel << msg; return true; }`，调用 `m_pRequestHandler->ProcessKeyEvent(KeyEvent(wParam), lParam, eat)`。引擎处理器在生成候选/状态文本后，调用 `eat(text)` 即可把文本顺着同一条管道送回客户端。

这就是 `EatLine` 的真身：**服务端把响应写回管道的回调**。带 `eat` 参数的虚函数（`AddSession`、`ProcessKeyEvent`、`HighlightCandidateOnCurrentPage`、`ChangePage`）都走这条路；客户端随后用 `GetResponseData()` 把这些文本取出来，交给 u2-l5 的 `ResponseParser` 解析成 `Context`/`Status`/`UIStyle`。

> 另一类派发：不是所有 `RequestHandler` 虚函数都由 IPC 命令触发。`SetOption` 由 **托盘菜单** 的 WM_COMMAND 触发（[WeaselServerImpl.cpp:110-132](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L110-L132) 里 `ID_WEASELTRAY_ENABLE_ASCII` 直接调 `SetOption(...,"ascii_mode",true)`），`UpdateColorTheme` 由系统主题变化的窗口消息触发（[WeaselServerImpl.cpp:56-65](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L56-L65)），`Initialize`/`Finalize` 由服务启停触发。它们是「服务端内部触发」，不经过命名管道。

#### 4.4.4 代码实践：排出完整命令-方法映射表

**实践目标**：把 `RequestHandler` 的全部虚函数逐一对应到「触发它的 IPC 命令（或其它来源）」「对应的 `Client` 方法」「服务端的 `OnXxx` 派发函数」，形成本讲的总收束表。这也是本讲规格里指定的实践任务。

**操作步骤**：

1. 打开 [include/WeaselIPC.h:52-84](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L52-L84)，把 18 个虚函数逐行抄下。
2. 对每个虚函数，到 [WeaselClientImpl.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp) 里找同名 `ClientImpl` 方法，看它发的 `WEASEL_IPC_*` 命令是什么。
3. 再到 [WeaselServerImpl.cpp:377-403](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L377-L403) 的派发表里找这条命令对应的 `OnXxx`。
4. 对找不到的虚函数（`Initialize`/`Finalize`/`SetOption`/`UpdateColorTheme`/`FindSession`），到 `WeaselServerImpl.cpp` 里搜索虚函数名，找出「不是 IPC 命令」的触发源。

**参考答案表**（这是本讲的核心交付物）：

| `RequestHandler` 虚函数 | 触发命令 / 来源 | `Client` 方法 | 服务端派发 |
| --- | --- | --- | --- |
| `Initialize()` | 服务启动（非 IPC） | — | `ServerImpl::Start/Run` 时机由实现自调 |
| `Finalize()` | 系统关机/退出（非 IPC） | — | `OnEndSystemSession` ([L99-108](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L99-L108)) |
| `FindSession(sid)` | `WEASEL_IPC_ECHO` | `Echo()` | `OnEcho` |
| `AddSession(buf, eat)` | `WEASEL_IPC_START_SESSION` | `StartSession()` | `OnStartSession` |
| `RemoveSession(sid)` | `WEASEL_IPC_END_SESSION` | `EndSession()` | `OnEndSession` |
| `ProcessKeyEvent(ke, sid, eat)` | `WEASEL_IPC_PROCESS_KEY_EVENT` | `ProcessKeyEvent()` | `OnKeyEvent` |
| `CommitComposition(sid)` | `WEASEL_IPC_COMMIT_COMPOSITION` | `CommitComposition()` | `OnCommitComposition` |
| `ClearComposition(sid)` | `WEASEL_IPC_CLEAR_COMPOSITION` | `ClearComposition()` | `OnClearComposition` |
| `SelectCandidateOnCurrentPage(i, sid)` | `WEASEL_IPC_SELECT_CANDIDATE_ON_CURRENT_PAGE` | `SelectCandidateOnCurrentPage()` | `OnSelectCandidateOnCurrentPage` |
| `HighlightCandidateOnCurrentPage(i, sid, eat)` | `WEASEL_IPC_HIGHLIGHT_CANDIDATE_ON_CURRENT_PAGE` | `HighlightCandidateOnCurrentPage()` | `OnHighlightCandidateOnCurrentPage` |
| `ChangePage(backward, sid, eat)` | `WEASEL_IPC_CHANGE_PAGE` | `ChangePage()` | `OnChangePage` |
| `FocusIn(param, sid)` | `WEASEL_IPC_FOCUS_IN` | `FocusIn()` | `OnFocusIn` |
| `FocusOut(param, sid)` | `WEASEL_IPC_FOCUS_OUT` | `FocusOut()` | `OnFocusOut` |
| `UpdateInputPosition(rc, sid)` | `WEASEL_IPC_UPDATE_INPUT_POS` | `UpdateInputPosition()` | `OnUpdateInputPosition` |
| `StartMaintenance()` | `WEASEL_IPC_START_MAINTENANCE` | `StartMaintenance()` | `OnStartMaintenance` |
| `EndMaintenance()` | `WEASEL_IPC_END_MAINTENANCE` | `EndMaintenance()` | `OnEndMaintenance` |
| `SetOption(sid, opt, val)` | 托盘菜单 WM_COMMAND（非 IPC） | — | `OnCommand` ([L110-132](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L110-L132)) |
| `UpdateColorTheme(dark)` | 系统主题变化窗口消息（非 IPC） | — | `OnColorChange` ([L56-65](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L56-L65)) |

另外有两条命令 **不** 对应任何 `RequestHandler` 虚函数（属于 `Server` 自身职责）：

| 命令 | `Client` 方法 | 服务端处理 | 说明 |
| --- | --- | --- | --- |
| `WEASEL_IPC_SHUTDOWN_SERVER` | `ShutdownServer()` | `OnShutdownServer` → `Stop()` | 直接关停服务，不经过处理器 |
| `WEASEL_IPC_TRAY_COMMAND` | `TrayCommand(menuId)` | `OnCommand` → `m_MenuHandlers` | 转发到注册的托盘菜单回调 |

**需要观察的现象**：13 个虚函数由 13 条 IPC 命令经命名管道触发；4 个虚函数由服务端内部事件触发；2 条命令属于服务控制本身。这张表覆盖了 `WEASEL_IPC_COMMAND` 枚举的全部 16 个具名命令。

**预期结果**：你得到一张三栏对照表，凭它可以在源码里任意跳转——从 TSF 端方法一键定位到引擎处理函数，反之亦然。这是后续阅读 u2-l3、u4 的导航图。

#### 4.4.5 小练习与答案

**练习 1**：`Echo()` 调用的是 `WEASEL_IPC_ECHO` 命令，但服务端 `OnEcho` 调用的是 `FindSession()`，名字对不上。为什么？

> **答案**：`Echo` 是客户端语义（「服务还在吗？我的会话还有效吗？」），`FindSession` 是服务端语义（「查一下这个 session_id 是否存在」）。客户端 [WeaselClientImpl.cpp:169-175](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L169-L175) 的 `Echo()` 拿 `FindSession` 的返回值与本地 `session_id` 比较，相等才认为会话仍有效。这是「同一件事在前后端取了不同名字」的典型例子，映射表的价值正在于把这种错位显式化。

**练习 2**：如果想新增一条命令「让服务端开关调试日志」，按本讲的架构，至少要改哪几个地方？

> **答案**：① `WeaselIPC.h` 的 `WEASEL_IPC_COMMAND` 枚举里加一条（放在 `WEASEL_IPC_LAST_COMMAND` 之前）；② `WeaselIPC.h` 的 `RequestHandler` 加一个对应虚函数（带默认空实现）；③ `Client` 接口 + `WeaselClientImpl` 加一个方法，发新命令；④ `WeaselServerImpl.cpp` 的 `HandlePipeMessage` 派发表里加一行 `PIPE_MSG_HANDLE(...)`，并新增一个 `OnXxx` 调用处理器；⑤ 真正的处理器实现（`RimeWithWeaselHandler`）里重写该虚函数，落地日志开关。这正是 u7-l4「扩展点」要详细讨论的改动清单。

## 5. 综合实践

**任务**：选一个真实场景——「用户在应用里按下一个枚举键，候选窗口翻到下一页，再用鼠标点选第 2 个候选」——画出这一连串动作对应的 **IPC 命令时序图**，并标注每条命令的 `wParam`/`lParam` 含义、发起方（`Client` 方法）、服务端派发函数（`OnXxx`）、最终触发的 `RequestHandler` 虚函数。

**操作步骤**：

1. 先列出会涉及的命令序列：`START_SESSION` →（多次）`PROCESS_KEY_EVENT` → `CHANGE_PAGE` → `SELECT_CANDIDATE_ON_CURRENT_PAGE` →（最终）`COMMIT_COMPOSITION` → `END_SESSION`。
2. 对每条命令，从本讲 4.4.4 的映射表里查出 `Client` 方法、`OnXxx`、虚函数三列。
3. 对每条命令，从 [WeaselClientImpl.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp) 对应方法的实现里确认 `wParam`/`lParam` 装的是什么（例如 `CHANGE_PAGE` 的 `wParam` 是 `bool backward`，`SELECT_CANDIDATE_ON_CURRENT_PAGE` 的 `wParam` 是候选序号 `size_t index`）。
4. 标出哪些命令会带 `eat` 回调、因而会有「响应文本回流」（`PROCESS_KEY_EVENT`、`CHANGE_PAGE`、`HIGHLIGHT_CANDIDATE_ON_CURRENT_PAGE`），哪些是单向通知（`FOCUS_IN`、`UPDATE_INPUT_POS`）。
5. 画出时序箭头：TSF 端 → 管道 → Server 派发 → `RequestHandler` →（经 `eat`）→ 管道 → TSF 端 `GetResponseData`。

**预期结果**：一张完整的时序图，能清楚回答「一次选词上屏在管道上跑了几条命令、每条装了什么、哪些有回文本」。这张图同时是 u2-l2（传输细节）、u2-l5（响应解析）、u4（引擎处理）的总入口。

**待本地验证**：时序图的细节（尤其 `eat` 回流时机）可在 `_SendMessage` 和 `OnKeyEvent`/`OnChangePage` 处加 `DEBUG` 日志，用 DebugView 实测确认。

## 6. 本讲小结

- Weasel 的 IPC 层用 **三个抽象** 分工：`Client`（TSF 端用，pImpl 藏管道细节）、`Server`（服务端用，建窗口+消息循环+注入处理器）、`RequestHandler`（定义服务端能力的抽象基类，由 `RimeWithWeaselHandler` 实现）。
- 在管道上跑的是 **`PipeMessage`**——一个定长结构（命令 + `wParam` + `lParam`），命令编号来自 `WEASEL_IPC_COMMAND` 枚举（共 16 个具名命令，编号从 `WM_APP+1` 起的历史遗留）。
- 管道寻址靠 `GetPipeName()` 拼出 `\\.\pipe\<用户名>\WeaselNamedPipe`，**用户名实现按用户隔离**；缓冲尺寸常量（4 KiB / 2048 宽字符）框定了单次往返的文本上限。
- 命令派发是四次翻译：`Client` 方法 → 命令编号 → 管道字节 → `ServerImpl::OnXxx` → `RequestHandler` 虚函数。带 `EatLine` 回调的虚函数会把响应文本顺着同一条管道回写。
- 13 个 `RequestHandler` 虚函数由 IPC 命令触发，4 个由服务端内部事件触发（托盘菜单、系统主题、启停），2 条命令属服务控制本身——这张映射表是本讲的核心交付物，也是后续阅读的导航图。

## 7. 下一步学习建议

本讲只看了 IPC 的「合同」（接口、命令、命名），没有钻进「管道到底怎么读写」。建议按以下顺序继续：

1. **u2-l2 命名管道通道 PipeChannel**：精读 `include/PipeChannel.h` 与 `WeaselIPC/PipeChannel.cpp`，搞清本讲反复出现的 `channel.Transact()`、`channel << msg`、`channel->ReceiveBuffer()` 背后的连接管理、线程本地句柄和 boost 缓冲流。
2. **u2-l3 IPC 客户端与服务器实现**：对照 `WeaselClientImpl`（客户端如何按需拉起服务进程、管理会话）与 `WeaselServerImpl`/`PipeServer`（监听线程模型、`HandlePipeMessage` 派发的多线程细节）。
3. **u2-l4 数据模型与 boost 序列化**：精读 `WeaselIPCData.h` 里 `Context`/`Status`/`UIStyle`/`CandidateInfo` 的字段语义和 `boost::serialization` 模板——本讲提到的「响应文本」装的就是这些结构。
4. **u2-l5 响应解析与反序列化**：看客户端如何用 `ResponseParser` 把 `eat` 回写的文本流解析回 `Context`/`Status`/`UIStyle`，闭合本讲 4.4 里「响应回流」的最后一环。
