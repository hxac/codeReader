# Weasel 是什么：项目定位与整体架构

## 1. 本讲目标

本讲是「Weasel 源码学习手册」的第一篇。读完本讲，你应当能够：

- 说清楚 **Weasel（小狼毫）** 在 Rime 输入法生态中的定位，以及它与核心引擎 `librime`、其它平台前端（macOS 鼠须管、Linux 的 ibus-rime / fcitx5-rime）的关系。
- 理解 Weasel 在 Windows 上的**多进程架构**：被应用进程加载的 `WeaselTSF` 前端、常驻后台的 `WeaselServer` 服务进程、以及连接两者的**命名管道 IPC**。
- 在脑海中画出（并能用文字复述）一次按键「从记事本被按下 → 最终文字上屏」的**完整数据流概览图**，并能把每个环节对应到具体的源码目录。

本讲不要求你立刻读懂每一行 C++ 代码。我们的目标是先建立「全局地图」，后续每一讲再往地图里填细节。

## 2. 前置知识

在进入源码之前，先用通俗的语言解释几个本讲会用到的概念。如果你已经熟悉，可以跳过本节。

### 2.1 什么是输入法（IME）

输入法（Input Method Editor，IME）是一个把「键盘按键」翻译成「文字」的程序。对中文用户来说，由于键盘上没有直接的汉字键，输入法负责把 `n-i-h-a-o` 这样的按键序列，转成候选词「你好」，再交给正在使用的软件（记事本、浏览器、聊天窗口）显示出来。

### 2.2 什么是 TSF

**TSF（Text Services Framework，文本服务框架）** 是 Windows 提供的一套 COM 接口，操作系统通过它把按键事件分发给输入法，也通过它让输入法把文字「上屏」到当前应用里。可以说，TSF 是输入法和 Windows 之间打交道必须遵守的「官方协议」。

在 Weasel 中，`WeaselTSF` 就是一个实现了 TSF 接口的 DLL，会被 Windows 加载进**每一个**正在输入文字的应用进程里。

### 2.3 什么是 IPC

**IPC（Inter-Process Communication，进程间通信）** 指两个独立运行的进程之间交换数据的方式。Weasel 用的是 Windows 的**命名管道（Named Pipe）**——可以把它想象成一根两端分别连着两个进程的「管子」，一头写字，另一头就能读到。

### 2.4 什么是 librime

`librime` 是 Rime 项目的**核心输入法引擎**，是一个跨平台的 C++ 库。它负责所有「真正像输入法」的事情：加载输入方案、查词典、管理候选词、学习用户词库、处理标点上屏等等。Weasel 本身**不做**这些事，而是把按键交给 `librime` 处理，再把 `librime` 给出的候选结果展示出来。

> 一句话类比：`librime` 是「大脑」，负责算出该打什么字；Weasel 是 Windows 平台上的「眼和手」，负责看键盘、把结果显示给用户、把字写进应用。

## 3. 本讲源码地图

本讲会从下面几个关键文件中提取架构信息。现在你不必读懂细节，只需记住每个文件的「角色」：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/README.md) | 项目说明：Rime 生态定位、跨平台前端清单、安装与定制入口。 |
| [include/WeaselIPC.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h) | IPC 公共接口：`Client` / `Server` / `RequestHandler` 三大类、IPC 命令枚举、命名管道命名规则。 |
| [include/RimeWithWeasel.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h) | `RimeWithWeaselHandler`——实现 `RequestHandler` 接口、桥接 `librime` 的核心类。 |
| [WeaselServer/WeaselServerApp.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp) | `WeaselServer` 服务进程的「组装车间」：把 Handler / UI / Server / 托盘拼装到一起。 |
| [WeaselServer/WeaselServer.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.cpp) | 服务进程的 `main` 入口：命令行解析、单实例重启、启动 `WeaselServerApp`。 |

仓库根目录下的几个关键子工程（目录）也将在本讲中出现：

| 目录 | 产出 | 角色 |
| --- | --- | --- |
| `WeaselTSF/` | DLL | TSF 前端，被加载进每个应用进程，作为 IPC 客户端。 |
| `WeaselServer/` | EXE | 后台服务进程，托管 `librime`、运行 IPC 服务端、驱动 UI。 |
| `WeaselIPC/` | lib | IPC 客户端实现（管道读写、响应解析）。 |
| `WeaselIPCServer/` | lib | IPC 服务端实现（管道监听、命令派发）。 |
| `RimeWithWeasel/` | lib | 把 `librime` 封装成 `RequestHandler`。 |
| `WeaselUI/` | lib | 候选窗口的界面与 DirectWrite 渲染。 |
| `include/` | 公共头 | 各工程共享的接口与数据结构定义。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 Rime 引擎与各平台前端**：搞清 Weasel 在 Rime 生态里的位置。
- **4.2 Weasel 的多进程架构与职责划分**：搞清「为什么要有两个进程」以及各模块分工。
- **4.3 按键到上屏的主链路概览**：把前两个模块串成一条数据流。

---

### 4.1 Rime 引擎与各平台前端

#### 4.1.1 概念说明

Rime 是一个跨平台的中文输入法项目。它的精髓在于**把「输入法引擎」和「平台前端」彻底分离**：

- **引擎** `librime`：与操作系统无关，只负责「按键 → 候选词」的计算逻辑。任何平台都可以复用它。
- **前端**：每个操作系统有自己的输入法接入方式（macOS、Linux、Windows 各不相同），所以需要为每个平台单独写一个前端。

这样做的好处是：同一套输入方案、同一份用户词库、同一套配置语法（YAML），可以在所有平台上获得一致的体验。你在 Windows 上学的 Rime 配置知识，搬到 macOS 鼠须管上几乎完全通用。

#### 4.1.2 核心流程

不同平台的 Rime 前端结构上高度相似，可以用下面这个统一模型来理解：

```
操作系统按键 ──▶ 平台前端（接入系统） ──▶ librime 引擎 ──▶ 候选/上屏结果 ──▶ 平台前端 ──▶ 屏幕
                       (Weasel/鼠须管/...)        (跨平台核心)
```

也就是说，**所有 Rime 前端都围着 `librime` 这一个引擎转**，区别只在于「如何接入各自的操作系统」。Weasel 就是 Windows 这一侧的前端。

#### 4.1.3 源码精读

README 的开头说明了 Weasel 与引擎的关系，并列出了其它平台的发行版：

> 「基於 中州韻輸入法引擎／Rime Input Method Engine 等開源技術」
> 「您可能還需要 RIME 用於其他操作系統的發行版：ibus-rime、fcitx5-rime 或 fcitx-rime 用於 Linux；【鼠鬚管】用於 macOS（64位）」

参见 [README.md:1-20](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/README.md#L1-L20)。可以看到：

- Weasel（小狼毫）= Windows 前端。
- 鼠须管 = macOS 前端。
- ibus-rime / fcitx5-rime / fcitx-rime = Linux 前端。
- 它们背后共用 `librime`。README 在「引用的开源软件」一节里也明确列出了 [librime](https://github.com/rime/librime)。

而在 Weasel 源码里，「桥接 `librime`」这件事由 `RimeWithWeaselHandler` 承担。它的头文件直接包含了 `librime` 的 C API 头：

[include/RimeWithWeasel.h:8](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h#L8)

```cpp
#include <rime_api.h>
```

`rime_api.h` 正是 `librime` 对外暴露的 C 语言接口。后续第 4 单元会专门讲 Weasel 如何调用这套 API（如 `rime_api` 的 `initialize`、`process_key` 等），本讲只需记住：**Weasel 通过这个头文件拿到了 `librime` 的全部能力**。

#### 4.1.4 代码实践

- **实践目标**：直观感受「同一引擎、多端前端」的生态布局。
- **操作步骤**：
  1. 打开本仓库 [README.md](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/README.md)，定位到「您可能還需要 RIME 用於其他操作系統的發行版」一节。
  2. 在一张纸上写下三行：「引擎 = librime」「macOS 前端 = 鼠须管」「Linux 前端 = ibus-rime / fcitx5-rime」「Windows 前端 = Weasel（本仓库）」。
  3. 用 `Grep` 在仓库里搜索 `rime_api.h`，看看哪些源码文件依赖了引擎 API（提示：主要在 `RimeWithWeasel/` 和 `include/`）。
- **需要观察的现象**：依赖 `rime_api.h` 的文件几乎都集中在 `RimeWithWeasel/` 这一个目录，说明引擎交互被很好地收口在这里。
- **预期结果**：你会确认「Weasel 与引擎打交道」的代码主要集中在 `RimeWithWeasel` 子工程，这正是后续第 4 单元的主题。

#### 4.1.5 小练习与答案

**练习 1**：如果有人想把 Weasel 移植到一个全新的操作系统，理论上需要重写哪个部分、复用哪个部分？

> **参考答案**：需要重写「平台前端」（接入新系统的按键事件与上屏机制），可以几乎原样复用 `librime` 引擎本身，也可以参考 `RimeWithWeasel` 的写法把引擎封装成新前端的处理器。

**练习 2**：`librime` 是用 C++ 写的库，为什么 `RimeWithWeasel.h` 里要用 `rime_api.h` 这个 **C API** 而不是直接用 C++ 接口？

> **参考答案**：`rime_api.h` 是 `librime` 对外提供的稳定 C 接口。C ABI（应用二进制接口）在不同编译器、不同版本之间比 C++ ABI 稳定得多，用 C 接口可以避免因编译器/运行库版本不同而导致的链接与崩溃问题。

---

### 4.2 Weasel 的多进程架构与职责划分

#### 4.2.1 概念说明

很多输入法是「单进程」的：一个 DLL 同时负责接收按键、运行引擎、画候选窗口。Weasel 选择了**多进程**架构，分成两个角色：

1. **WeaselTSF（DLL，运行在每个应用进程内）**：它是「驻外办事处」，每个打开了输入框的程序（记事本、Word、浏览器）里都有一份它的实例。它只负责两件事：**抓住按键**、**把上屏文字写回当前应用**。它本身**不包含**输入法引擎，也不画候选窗口。
2. **WeaselServer（EXE，全局唯一的后台进程）**：它是「总部」，常驻后台。真正的 `librime` 引擎、候选窗口 UI 都在这里运行。它对外开了一根命名管道，等待各处「驻外办事处」把按键事件送过来。

为什么这么设计？核心动机有两个：

- **引擎与词库只需要加载一份**。如果把引擎放进每个应用进程，内存占用会成倍增加，用户词库也难以在所有应用间同步。集中到一个 Server 进程，所有应用共享同一份引擎、同一份词库状态。
- **稳定性**。Server 崩溃只影响输入法本身，重启即可恢复；而应用进程（你的 Word、浏览器）不会因为输入法的 bug 而被拖垮。

连接这两个进程的，就是**命名管道 IPC**。`WeaselTSF` 是 IPC 的客户端，`WeaselServer` 是 IPC 的服务端。

#### 4.2.2 核心流程

多进程协作的整体流程如下：

```
┌─────────────────────────────┐        命名管道 IPC         ┌──────────────────────────────────┐
│  应用进程（如 notepad.exe）  │   ◀──────────────────────▶  │      WeaselServer.exe（后台）      │
│  ┌───────────────────────┐  │                              │  ┌────────────┐  ┌─────────────┐  │
│  │  WeaselTSF.dll        │  │   1. 按键事件 (ProcessKey)   │  │ IPC 服务端  │─▶│ librime 引擎│  │
│  │  - 抓按键             │──┼──────────────────────────────┼─▶│ (派发命令)  │  │ (查词典)    │  │
│  │  - IPC 客户端         │◀─┼──────────────────────────────┼──│            │◀─│             │  │
│  │  - 把文字写回应用      │  │   2. 候选/上屏结果 (回包)     │  └────────────┘  └─────────────┘  │
│  └───────────────────────┘  │                              │  ┌────────────┐                   │
└─────────────────────────────┘                              │  │ WeaselUI   │ 候选窗口绘制       │
                                                              │  └────────────┘                   │
                                                              │  + 系统托盘、自动更新等           │
                                                              └──────────────────────────────────┘
```

关键点：**应用进程里没有任何引擎逻辑，也没有候选窗口**。它只是把按键通过管道发给 Server，再把 Server 算出来的「要上屏的文字」写回应用。候选窗口是 Server 画的（一个独立的置顶小窗口），漂浮在屏幕上。

#### 4.2.3 源码精读

**① IPC 接口的三件套：`Client` / `Server` / `RequestHandler`**

整个 IPC 设计的「契约」定义在 `include/WeaselIPC.h` 里。先看命名管道的名字是怎么拼出来的——它和**当前系统用户名**绑定，因此不同 Windows 用户互不干扰：

[include/WeaselIPC.h:170-177](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L170-L177)

```cpp
inline std::wstring GetPipeName() {
  std::wstring pipe_name;
  pipe_name += L"\\\\.\\pipe\\";
  pipe_name += getUsername();
  pipe_name += L"\\";
  pipe_name += WEASEL_IPC_PIPE_NAME;  // L"WeaselNamedPipe"
  return pipe_name;
}
```

这段代码说明管道全形如 `\\.\pipe\<用户名>\WeaselNamedPipe`。`getUsername()` 把用户名拼进路径，实现了**按用户隔离**——这在多人共用一台机器或服务会话场景下很重要。

接着看 IPC 都能传哪些「命令」。这是一组从 `WM_APP + 1` 开始的枚举：

[include/WeaselIPC.h:18-36](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L18-L36)

```cpp
enum WEASEL_IPC_COMMAND {
  WEASEL_IPC_ECHO = (WM_APP + 1),
  WEASEL_IPC_START_SESSION,
  WEASEL_IPC_END_SESSION,
  WEASEL_IPC_PROCESS_KEY_EVENT,   // ← 处理按键，这是最核心的命令
  WEASEL_IPC_SHUTDOWN_SERVER,
  WEASEL_IPC_FOCUS_IN,
  WEASEL_IPC_FOCUS_OUT,
  WEASEL_IPC_UPDATE_INPUT_POS,
  ...
  WEASEL_IPC_TRAY_COMMAND,
  WEASEL_IPC_SELECT_CANDIDATE_ON_CURRENT_PAGE,
  ...
  WEASEL_IPC_LAST_COMMAND
};
```

其中 `WEASEL_IPC_PROCESS_KEY_EVENT` 就是「把一个按键交给服务端处理」的命令，它是整条主链路的心脏。命令通过 `PipeMessage` 结构在管道里传输：

[include/WeaselIPC.h:39-43](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L39-L43)

```cpp
struct PipeMessage {
  WEASEL_IPC_COMMAND Msg;
  DWORD wParam;
  DWORD lParam;
};
```

`Client` 类（住在应用进程里的客户端）把这些命令封装成了一个个易用的方法，例如：

[include/WeaselIPC.h:102-148](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L102-L148)（节选关键方法）

```cpp
class Client {
 public:
  // 连接到服务，必要时启动服务进程
  bool Connect(ServerLauncher launcher = 0);
  void ShutdownServer();
  void StartSession();
  void EndSession();
  bool Echo();
  // 请求服务处理按键消息
  bool ProcessKeyEvent(KeyEvent const& keyEvent);
  // 上屏正在編輯的文字
  bool CommitComposition();
  // 选择当前页面编号为index的候选
  bool SelectCandidateOnCurrentPage(size_t index);
  ...
};
```

注意 `Connect` 的注释「必要时启动服务进程」——这正是多进程架构能「自动拉起」Server 的关键：`WeaselTSF` 发现管道没人监听时，会主动把 `WeaselServer.exe` 启动起来（后续 u2 单元会讲细节）。

`Server` 类（服务端）则把请求交给一个可替换的 `RequestHandler`：

[include/WeaselIPC.h:52-84](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L52-L84)（节选）

```cpp
struct RequestHandler {
  virtual void Initialize() {}
  virtual DWORD AddSession(LPWSTR buffer, EatLine eat = 0) { return 0; }
  virtual DWORD RemoveSession(DWORD session_id) { return 0; }
  virtual BOOL ProcessKeyEvent(KeyEvent keyEvent,
                               DWORD session_id,
                               EatLine eat) { return FALSE; }
  virtual void CommitComposition(DWORD session_id) {}
  virtual void SelectCandidateOnCurrentPage(size_t index, DWORD session_id) {}
  ...
};
```

`RequestHandler` 是一个**抽象基类**（全是虚函数），它把「IPC 服务端」和「真正干活的引擎」解耦了：IPC 层只负责收发命令，至于命令怎么处理，交给 `RequestHandler` 的某个子类。这种设计让 IPC 代码可以脱离引擎独立测试（详见 u7 单元的测试工程）。

**② 谁来当 `RequestHandler`？——`RimeWithWeaselHandler`**

桥接 `librime` 的那个子类就是 `RimeWithWeaselHandler`。看它的类声明开头：

[include/RimeWithWeasel.h:36-47](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h#L36-L47)

```cpp
class RimeWithWeaselHandler : public weasel::RequestHandler {
 public:
  RimeWithWeaselHandler(weasel::UI* ui);
  virtual void Initialize();
  virtual DWORD FindSession(WeaselSessionId ipc_id);
  virtual DWORD AddSession(LPWSTR buffer, EatLine eat = 0);
  virtual BOOL ProcessKeyEvent(weasel::KeyEvent keyEvent,
                               WeaselSessionId ipc_id,
                               EatLine eat);
  virtual void CommitComposition(WeaselSessionId ipc_id);
  ...
};
```

它**继承自 `weasel::RequestHandler`**，并重写了 `ProcessKeyEvent`、`AddSession` 等方法——也就是说，IPC 服务端收到的每一个按键命令，最终都会落到这里，再由它转交给 `librime`。

**③ 服务进程怎么把这一切组装起来？——`WeaselServerApp`**

最后看「组装车间」。`WeaselServerApp` 的构造函数干净利落地把四大件拼好：

[WeaselServer/WeaselServerApp.cpp:5-11](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L5-L11)

```cpp
WeaselServerApp::WeaselServerApp()
    : m_handler(std::make_unique<RimeWithWeaselHandler>(&m_ui)),
      tray_icon(m_ui) {
  m_server.SetRequestHandler(m_handler.get());
  SetupMenuHandlers();
}
```

读这段代码可以提炼出 WeaselServer 的四大组成：

1. `m_ui`（`weasel::UI`）——候选窗口。
2. `m_handler`（`RimeWithWeaselHandler`）——引擎桥接器，构造时把 `m_ui` 的指针传进去，这样它才能驱动界面。
3. `m_server`（`weasel::Server`）——IPC 服务端，通过 `SetRequestHandler` 绑定到 `m_handler`，于是「管道里收到的命令 → Handler 处理」这条链就接通了。
4. `tray_icon`——系统托盘图标。

`Run()` 方法则是启动顺序的总指挥：

[WeaselServer/WeaselServerApp.cpp:15-46](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L15-L46)（节选）

```cpp
int WeaselServerApp::Run() {
  if (!m_server.Start())        // 1. 启动 IPC 服务端（开始监听管道）
    return -1;
  ...
  win_sparkle_init();           // 2. 初始化自动更新（WinSparkle）
  m_ui.Create(m_server.GetHWnd());  // 3. 创建候选窗口（隐藏，待显示）
  m_handler->Initialize();      // 4. 初始化 librime 引擎
  ...
  tray_icon.Create(...);        // 5. 创建系统托盘图标
  int ret = m_server.Run();     // 6. 进入消息循环，开始服务
  ...
}
```

这六步正好对应「一个输入法后台服务从无到有」的全过程。

#### 4.2.4 代码实践

- **实践目标**：用源码验证「IPC 层与引擎层是解耦的」这一架构判断。
- **操作步骤**：
  1. 打开 [include/WeaselIPC.h:52-84](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L52-L84)，数一数 `RequestHandler` 有多少个虚函数。
  2. 再打开 [include/RimeWithWeasel.h:40-64](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h#L40-L64)，对比 `RimeWithWeaselHandler` 重写了哪些。
  3. 用 `Grep` 在 `include/WeaselIPC.h` 中搜索 `rime_api.h`，确认 IPC 头文件**没有**直接依赖 `librime`。
- **需要观察的现象**：`WeaselIPC.h` 里完全找不到 `rime_api.h` 或任何引擎类型，说明 IPC 这一层对引擎一无所知。
- **预期结果**：你会得出结论——IPC 层只认 `RequestHandler` 这个抽象，换一个完全不同的引擎实现（只要也继承 `RequestHandler`），`WeaselServer` 的代码一行都不用改。这就是「依赖抽象」带来的解耦。

#### 4.2.5 小练习与答案

**练习 1**：管道名里为什么要拼上 `getUsername()`？

> **参考答案**：为了让不同 Windows 用户各自拥有一条独立管道，避免 A 用户的输入法服务收到 B 用户的按键。同时它也间接保证只有当前用户的应用进程能连上自己的 Server。

**练习 2**：`WeaselServerApp` 构造函数里，为什么要把 `&m_ui` 传给 `RimeWithWeaselHandler`，再单独把 `m_handler.get()` 传给 `m_server.SetRequestHandler`？

> **参考答案**：因为 Handler 需要驱动界面（算出候选后要让 UI 显示），所以它持有 UI 的指针；而 Server 是命令的入口，它需要知道「命令来了交给谁处理」，所以持有 Handler。这样数据流向是：`Server → Handler → (librime) → UI`，职责清晰、单向依赖。

**练习 3**：如果 `m_server.Start()` 返回失败（比如管道已被占用），`WeaselServerApp::Run()` 会怎样？

> **参考答案**：直接 `return -1`，不会继续创建 UI、初始化引擎或进入消息循环。这是一种「快速失败」策略——服务端起不来就没必要做后续初始化。

---

### 4.3 按键到上屏的主链路概览

#### 4.3.1 概念说明

把前两个模块连起来，就能回答一个贯穿全手册的核心问题：**「我在记事本里按下一个键，到底发生了什么？」**

这条链路横跨两个进程，经历「抓键 → 跨进程 → 算字 → 回传 → 上屏」五个阶段。本讲只给你**概览**，每个阶段的源码细节会在后续单元展开：

| 阶段 | 所在进程 | 主要源码 | 后续讲义 |
| --- | --- | --- | --- |
| ① 抓按键 | 应用进程 | `WeaselTSF/KeyEventSink.cpp` | u3-l2 |
| ② 跨进程发命令 | 应用进程 → Server | `WeaselIPC/WeaselClientImpl.cpp`、`include/PipeChannel.h` | u2-l2、u2-l3 |
| ③ 引擎算字 | Server | `RimeWithWeasel/RimeWithWeasel.cpp`（调用 `librime`） | u4-l2 |
| ④ 回传候选 + 画窗口 | Server | `WeaselUI/WeaselPanel.cpp` | u5 |
| ⑤ 上屏 | 应用进程 | `WeaselTSF/EditSession.cpp`、`Composition.cpp` | u3-l3 |

#### 4.3.2 核心流程

下面这张图把五个阶段串起来，**请把它当作本讲的「最重要的一张图」**记住：

```
记事本 notepad.exe（应用进程，内含 WeaselTSF.dll）
   │
   │ ① 用户敲下 'n' 键
   ▼
[WeaselTSF] KeyEventSink::OnKeyDown          ← TSF 把按键交给输入法
   │   ConvertKeyEvent: Windows 虚拟键 → weasel::KeyEvent
   ▼
[WeaselTSF] client.ProcessKeyEvent(ke)        ← IPC 客户端
   │   通过命名管道发出 WEASEL_IPC_PROCESS_KEY_EVENT
   ▼
====================== 命名管道 \\.\pipe\<用户>\WeaselNamedPipe ======================
   ▼
[WeaselServer] IPC 服务端收到命令 → 派发给 RequestHandler
   ▼
[RimeWithWeaselHandler] ProcessKeyEvent(...)   ← ②③ 跨进程后进入引擎桥接
   │   调用 librime 的 process_key → 得到候选词 / 上屏文字
   ▼
[WeaselServer] _UpdateUI / _Respond            ← ④ 把候选推给 UI
   │
   ├──▶ [WeaselUI] WeaselPanel 显示候选窗口（置顶小窗口）
   │
   └──▶ 通过管道把「需要上屏的最终文字」回传给应用进程
   ▼
[WeaselTSF] 收到回包 → EditSession 把文字写进记事本文档   ← ⑤ 上屏
   ▼
记事本里出现文字 / 候选窗口浮现在光标旁
```

理解这条链路时，有两个关键认知：

1. **「抓键」和「上屏」一定发生在应用进程内**，因为只有应用进程能感知到自己的输入框、能用 TSF 修改自己的文档。这就是为什么 `WeaselTSF` 必须是 DLL、必须被加载进每个应用。
2. **「算字」和「画候选窗口」发生在 Server 进程内**，因为引擎和词库要全局唯一。候选窗口是一个独立的置顶窗口，不属于任何应用，所以它能浮在所有窗口之上。

#### 4.3.3 源码精读

本讲只点出链路两端各一个「锚点」源码，证明这条链路真实存在，细节留给后续单元。

**应用进程侧（WeaselTSF）的客户端入口**：`WeaselTSF` 通过 `weasel::Client` 与 Server 通信，关键方法 `ProcessKeyEvent` 定义在 [include/WeaselIPC.h:123-124](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L123-L124)：

```cpp
// 请求服务处理按键消息
bool ProcessKeyEvent(KeyEvent const& keyEvent);
```

它会被 `WeaselTSF` 的按键处理代码调用，把转换后的按键事件发往 Server。

**Server 侧的处理入口**：服务端收到命令后，最终调用 `RimeWithWeaselHandler::ProcessKeyEvent`——这正是 [include/RimeWithWeasel.h:45-47](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h#L45-L47) 声明的那个重写：

```cpp
virtual BOOL ProcessKeyEvent(weasel::KeyEvent keyEvent,
                             WeaselSessionId ipc_id,
                             EatLine eat);
```

这两个同名方法（一个在 `Client`、一个在 `Handler`）一前一后，恰好就是「管道」的两端：应用进程里的 `Client::ProcessKeyEvent` 把按键送出去，Server 进程里的 `Handler::ProcessKeyEvent` 把按键接住并交给 `librime`。整条主链路就靠这一对方法贯通。

**Server 进程自身的启动**也值得知道：它是通过 `WeaselServer.cpp` 的 `_tWinMain` 进入的。除了正常的启动流程，它还处理一些命令行参数（本讲先建立印象，细节在 u6-l3）：

[WeaselServer/WeaselServer.cpp:65-107](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.cpp#L65-L107)（节选）

```cpp
if (!wcscmp(L"/userdir", lpstrCmdLine)) { ... }      // 打开用户目录
if (!wcscmp(L"/ascii", lpstrCmdLine) || ...) { ... }  // 切换中/英文
// command line option /q stops the running server
bool quit = !wcscmp(L"/q", lpstrCmdLine) || ...;
// restart if already running
{
  weasel::Client client;
  if (client.Connect()) { client.ShutdownServer(); ... }  // 单实例重启
}
...
WeaselServerApp app;
nRet = app.Run();                                     // 真正进入服务循环
```

注意这里有一个很有意思的细节：`WeaselServer.exe` 自己也可以作为一个 IPC **客户端**（`weasel::Client client; client.Connect();`）去连接**正在运行的旧 Server**，从而实现「单实例重启」——第二次启动时先通过管道让旧的 Server 退出，再让自己成为新的 Server。这正是多进程 IPC 架构带来的额外便利。

#### 4.3.4 代码实践

- **实践目标**：把本讲学的「数据流」画成一张可放进学习笔记的图，并标注源码位置。
- **操作步骤**：
  1. 拿一张白纸或打开任意画图工具，画出 4.3.2 中的数据流图。
  2. 在每个方框旁边，用括号标注它对应的**源码目录或文件**（参考 4.3.1 的表格）。
  3. 在两个进程之间的「命名管道」上，标注管道名格式 `\\.\pipe\<用户名>\WeaselNamedPipe`，并指出这个名字来自 [include/WeaselIPC.h:170-177](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L170-L177)。
  4. 标出「按键进」与「结果出」这一对同名方法：`Client::ProcessKeyEvent` 与 `RimeWithWeaselHandler::ProcessKeyEvent`。
- **需要观察的现象**：画完后，你应该能一眼看出「哪些步骤在应用进程、哪些在 Server 进程」，以及「IPC 边界」划在哪里。
- **预期结果**：得到一张标注完整的架构数据流图。这是后续所有讲义的「索引地图」——以后读到任何模块，你都能把它放回这张图的正确位置。
- **提示**：如果你暂时没有 Windows 环境运行 Weasel，不必担心，这是一道「源码阅读 + 画图」型实践，不需要真正运行程序。标注「待本地验证」的运行类实践会在后续讲义出现。

#### 4.3.5 小练习与答案

**练习 1**：候选窗口（WeaselPanel）是画在记事本里的，还是独立于记事本的？为什么？

> **参考答案**：它是独立于记事本的、由 `WeaselServer` 进程创建的置顶窗口。因为 `WeaselServer` 才持有 UI 与引擎，而应用进程（记事本）里只有轻量的 `WeaselTSF` 客户端，不具备绘制候选窗口的条件。

**练习 2**：`WeaselServer.exe /q` 这条命令为什么能让正在运行的输入法服务退出？它本身不就是「服务端」吗？

> **参考答案**：虽然它逻辑上是服务端程序，但它启动时先以**客户端**身份（`weasel::Client`）连接到已运行的旧 Server，通过 `WEASEL_IPC_SHUTDOWN_SERVER` 命令让旧 Server 自行退出。这正是多进程 IPC 架构的复用：同一份 Client 代码既能用于应用进程发按键，也能用于第二个 Server 实例去控制第一个。

**练习 3**：如果一个应用进程里 `WeaselTSF` 调用 `client.ProcessKeyEvent` 时，Server 还没启动，会发生什么？

> **参考答案**：根据 [include/WeaselIPC.h:108](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L108) `Connect` 的注释「必要时启动服务进程」，Client 在发现管道无人监听时会先启动 `WeaselServer.exe`（这就是 `ServerLauncher` 参数的用途），等它就绪后再发送命令。所以用户通常感知不到 Server 缺失——它会按需自动拉起。具体实现细节见 u2-l3。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿性小任务：

**任务：编写一份「Weasel 架构速查卡」**

要求产出一张 Markdown 表格 + 一段文字说明，包含：

1. **进程对照表**：列出「应用进程（含 WeaselTSF）」和「WeaselServer 进程」各自包含哪些模块、各自负责什么。至少覆盖：抓键、上屏、IPC 客户端、IPC 服务端、引擎、UI、托盘。
2. **IPC 边界说明**：用一句话写清楚「数据跨进程的位置」在哪里、走的是什么通道、通道名如何构造（引用 [include/WeaselIPC.h:170-177](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L170-L177)）。
3. **一键链路**：用编号步骤（① ~ ⑤）写出一次按键从记事本到上屏的过程，每一步标注对应的源码目录或文件。
4. **架构动机**：用两三句话说明「为什么 Weasel 要拆成两个进程而不是单进程」，结合本讲 4.2.1 的论述。

**参考答案要点**（你可以据此自检）：

- 进程对照表应能体现「应用进程＝瘦客户端（TSF + IPC client）」「Server 进程＝胖服务端（IPC server + Handler + librime + UI + 托盘）」。
- IPC 边界应指出命名管道 `\\.\pipe\<用户名>\WeaselNamedPipe`，且命令是 `WEASEL_IPC_PROCESS_KEY_EVENT`。
- 一键链路应与 4.3.2 的图一致。
- 动机应提到「引擎/词库全局唯一」「稳定性隔离」。

完成后把这张速查卡保存到你的学习笔记里——它会在阅读后续每一讲时帮你快速定位。

## 6. 本讲小结

- Weasel（小狼毫）是 Rime 输入法引擎 `librime` 在 **Windows** 平台的前端；macOS 用鼠须管、Linux 用 ibus-rime / fcitx5-rime，它们共用同一个引擎。
- Weasel 采用**多进程架构**：`WeaselTSF`（DLL）作为瘦客户端运行在每个应用进程内，负责抓键与上屏；`WeaselServer`（EXE）作为后台服务托管 `librime`、UI 和托盘，全局唯一。
- 两个进程之间通过**命名管道 IPC** 通信，管道名 `\\.\pipe\<用户名>\WeaselNamedPipe` 按 Windows 用户隔离；命令集定义在 `WEASEL_IPC_COMMAND` 枚举里，其中 `WEASEL_IPC_PROCESS_KEY_EVENT` 是核心。
- IPC 层通过抽象基类 `RequestHandler` 与引擎解耦，`RimeWithWeaselHandler` 继承它并把命令转交给 `librime`（`rime_api.h`）。
- `WeaselServerApp` 是服务进程的组装车间：把 `Server`（IPC 服务端）、`RimeWithWeaselHandler`（引擎桥接）、`UI`（候选窗口）、`tray_icon`（托盘）拼装到一起，`Run()` 里依次启动它们。
- 一次按键的主链路：记事本 → `WeaselTSF` 抓键 → 管道发命令 → `WeaselServer` 派发 → `librime` 算字 → UI 画候选 + 回传上屏文字 → `WeaselTSF` 写回应用文档。

## 7. 下一步学习建议

本讲建立了「全局地图」。接下来建议按以下顺序深入：

1. **先打通 IPC 骨架**：进入第 2 单元（u2），重点读 `include/WeaselIPC.h`（命令协议）、`include/PipeChannel.h`（命名管道通道）、`WeaselIPC/WeaselClientImpl.cpp` 与 `WeaselIPCServer/WeaselServerImpl.cpp`（客户端/服务端实现）。理解了 IPC，你就真正看懂了 4.3 链路中「跨进程」的那一段。
2. **再读 TSF 前端**：进入第 3 单元（u3），看 `WeaselTSF` 如何被 Windows 加载、如何抓住按键、如何把文字写回应用。
3. **然后读引擎桥接**：进入第 4 单元（u4），看 `RimeWithWeaselHandler` 如何把按键交给 `librime` 并回传结果。
4. 如果你更想先了解「怎么把项目跑起来」或「目录怎么组织」，可以先跳到 u1-l2（目录结构）和 u1-l3（构建与运行），再回到 IPC。

无论你选哪条路径，记住：**随时回到本讲 4.3.2 的数据流图**，把新学的模块放回图的正确位置——这是阅读 Weasel 这种多进程项目最有效的导航方法。
