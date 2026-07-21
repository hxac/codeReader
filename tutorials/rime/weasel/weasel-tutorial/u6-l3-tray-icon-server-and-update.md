# 系统托盘、服务进程与自动更新

## 1. 本讲目标

在前面的讲义里，我们已经看清了 Weasel 的「主链路」：`WeaselTSF`（DLL）抓键 → 命名管道 IPC → `WeaselServer`（EXE）→ `RimeWithWeaselHandler` 调用 librime → `WeaselPanel` 画候选并回传上屏文字。但我们一直没有回答几个「工程化」的问题：

- `WeaselServer.exe` 这个后台进程是**怎么被拉起来、又是怎么长期挂着**的？
- 它在系统托盘里的那个小图标（中/英/维护），是**谁画的、谁点击响应**的？
- 用户右键托盘弹出的「输入法设定 / 重新部署 / 检查新版本 / 退出」这一整列菜单，**点击后是怎么分发到具体动作**的？
- 已经有一个 `WeaselServer` 在跑了，再启动一个会**怎么样**？为什么「重启输入法」能做到无缝切换？
- 自动更新（WinSparkle）又是**嵌在哪一步、用什么协议**检查新版本的？

本讲就把这些「外壳」问题一次性讲透。读完本讲，你将能够：

1. 说清 `WeaselServerApp` 如何把 `Server`（IPC 服务端）、`RequestHandler`（引擎桥接）、`UI`（候选窗口）、`WeaselTrayIcon`（托盘）四大件**组装**成一个完整服务，并进入消息循环。
2. 掌握 `WeaselServer.exe` 的命令行参数（`/q`、`/ascii`、`/userdir`、`/update` 等）与**单实例 + 重启**机制。
3. 理解系统托盘图标的创建、刷新（中/英/维护三种状态）与**菜单命令分发**——并把 `WM_COMMAND`（本地点击）与 `WEASEL_IPC_TRAY_COMMAND`（跨进程 IPC）统一到同一张菜单表上。
4. 了解 WinSparkle 自动更新的接入方式与「稳定 / 测试」更新通道。

## 2. 前置知识

本讲属于「专家层」，假设你已经读过：

- **u1-l1 / u1-l2**：知道 `WeaselServer` 是全局唯一的后台 EXE，`WeaselTSF` 是驻留每个应用进程的 DLL，二者经命名管道 IPC 通信。
- **u2-1 ~ u2-3**：知道 `WEASEL_IPC_COMMAND` 枚举、`PipeMessage`（命令 + wParam + lParam 三字段）、`Client`/`Server`/`RequestHandler` 三大抽象，以及 `Client::Connect()` 只是「尝试连上正在运行的服务端管道」。
- **u4-l1**：知道 `RimeWithWeaselHandler` 是 `RequestHandler` 的唯一实现，`Initialize()`/`Finalize()` 控制引擎生命周期，维护模式（maintenance）期间按键被禁用。

需要补充几个 Windows 概念（不熟悉的术语下面用到时还会再解释）：

- **系统托盘（System Tray / 通知区）**：屏幕右下角那片放时钟和小图标的地方。程序通过 `Shell_NotifyIcon` API 向它「注册」一个图标，附带一段提示文字（tooltip）和一个回调消息号；用户对图标点击/右键时，Windows 把对应消息发回程序。
- **消息循环（Message Loop）**：Windows GUI 程序的核心是一个 `while(GetMessage) { TranslateMessage; DispatchMessage; }` 循环。WTL 把它封装成 `CMessageLoop::Run()`。只要这个循环还在转，进程就活着；一旦收到 `WM_QUIT`，循环退出、进程结束。
- **命令互斥体（Named Mutex）**：一种具名内核对象，`CreateMutex` 时若同名互斥体已存在，`GetLastError()` 会返回 `ERROR_ALREADY_EXISTS`。这是实现「全局只有一个实例」的标准手法。
- **WinSparkle**：一个开源的、面向 Windows 的软件自动更新库（类似 macOS 的 Sparkle），通过读取一个远程 `appcast.xml`（发布清单）来比较版本号、提示并下载安装新版本。

## 3. 本讲源码地图

本讲涉及的关键文件集中在 `WeaselServer/` 子工程，外加跨进程通信的两端：

| 文件 | 作用 |
| --- | --- |
| [WeaselServer/WeaselServer.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.cpp) | `WeaselServer.exe` 的 `_tWinMain` 入口：解析命令行、做单实例/重启、拉起 `WeaselServerApp` 并 `Run()`。 |
| [WeaselServer/WeaselServerApp.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp) | 「组装器」：把四大件拼起来、按正确顺序启动、注册托盘菜单处理器、清理。 |
| [WeaselServer/WeaselServerApp.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.h) | `WeaselServerApp` 类声明：`execute`/`explore`/`open`/`check_update`/`install_dir` 等工具函数与成员变量。 |
| [WeaselServer/WeaselTrayIcon.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselTrayIcon.cpp) | Weasel 自己的托盘图标逻辑：`Create` 注册图标、`Refresh` 按中/英/维护状态切换图标。 |
| [WeaselServer/WeaselTrayIcon.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselTrayIcon.h) | `WeaselTrayIcon` 声明、`WeaselTrayMode` 枚举、`WM_WEASEL_TRAY_NOTIFY` 消息号。 |
| [WeaselServer/SystemTraySDK.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/SystemTraySDK.cpp) / [SystemTraySDK.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/SystemTraySDK.h) | 第三方（Chris Maunder）的 `CSystemTray` 轻量封装：`Shell_NotifyIcon`、隐藏窗口、右键菜单、Explorer 崩溃恢复。 |
| [WeaselIPCServer/WeaselServerImpl.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp) / [WeaselServerImpl.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.h) | IPC 服务端：`Start`（单实例互斥体）、`Run`（消息循环 + 命名管道线程）、`OnCommand`（菜单分发）、`HandlePipeMessage`（命令派发）。 |
| [WeaselServer/resource.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/resource.h) | 托盘菜单项 ID（`ID_WEASELTRAY_*`）、图标 ID。 |
| [WeaselServer/WeaselServer.rc](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.rc) | 托盘右键菜单（简/繁/英三语）与 WinSparkle 的 `appcast` URL 资源。 |
| [WeaselIPC/WeaselClientImpl.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp) | 客户端：`ShutdownServer`、`TrayCommand`（跨进程发菜单命令）。 |
| [WeaselIPC/PipeChannel.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/PipeChannel.cpp) | `_TryConnect`：连接服务端管道时的 `ERROR_PIPE_BUSY` 重试。 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

1. **ServerApp 组装与消息循环**：四大件怎么拼、启动顺序、消息循环在哪、怎么退出。
2. **命令行与单实例重启**：`_tWinMain` 的命令行分支、单实例互斥体、旧实例优雅退让、崩溃自重启。
3. **托盘菜单与 WinSparkle 更新**：图标创建/刷新、右键菜单分发、本地 `WM_COMMAND` 与跨进程 `TRAY_COMMAND` 的统一、自动更新接入。

### 4.1 ServerApp 组装与消息循环

#### 4.1.1 概念说明

回忆 u1-l1 的全局架构：`WeaselServer.exe` 要同时干四件事——

- 当 IPC 的**服务端**（监听命名管道，接收每个应用进程里 `WeaselTSF` 发来的按键）；
- 当 **librime 引擎的宿主**（通过 `RimeWithWeaselHandler`）；
- 当**候选窗口的拥有者**（`weasel::UI` + `WeaselPanel`）；
- 当**系统托盘图标的拥有者**（`WeaselTrayIcon`）。

这四件事分别对应四个对象：`weasel::Server`、`RimeWithWeaselHandler`、`weasel::UI`、`WeaselTrayIcon`。`WeaselServerApp` 这个类的唯一职责，就是把这四个对象「装配」到一起，并保证它们的**启动顺序**正确——因为它们之间有依赖：`Server` 需要 `RequestHandler` 才能处理命令；`tray_icon` 需要 `m_ui` 的样式与状态引用才能画图标；`RequestHandler` 又要把引擎状态变化回调给托盘刷新。

> 类比：`WeaselServerApp` 像一个「主板」，上面插着四块「板卡」（IPC、引擎、UI、托盘）。本模块就讲主板怎么通电、各板卡按什么顺序上电、主板自己又怎么进入「待机循环」。

#### 4.1.2 核心流程

`WeaselServerApp` 的生命周期分两个阶段：**构造（装配）** 和 **运行（Run）**。

**装配阶段**（构造函数）做三件事：

```
构造 WeaselServerApp
├─ 1. 构造 m_ui（weasel::UI）          // 成员声明顺序保证最先构造
├─ 2. 构造 m_handler = make_unique<RimeWithWeaselHandler>(&m_ui)
│        // 引擎桥接，把 m_ui 的指针交给它
├─ 3. 构造 tray_icon(m_ui)              // 托盘，持有 m_ui.style()/m_ui.status() 的引用
└─ 4. m_server.SetRequestHandler(m_handler.get())
        // IPC 服务端拿到引擎桥接的指针
   └─ SetupMenuHandlers()                // 注册 11 个托盘菜单命令
```

注意第 1～3 步的顺序由**成员声明顺序**决定（C++ 成员按声明顺序构造，与初始化列表写法无关）。查看声明可以看到顺序是 `m_server` → `m_ui` → `tray_icon` → `m_handler`，因此 `m_ui` 一定在 `tray_icon` 之前就绪，`tray_icon` 持有的引用才有效。

**运行阶段**（`Run()`）的启动顺序很关键，错一步就可能崩溃或功能缺失：

```
Run()
├─ 1. m_server.Start()        // 建隐藏窗口 + 单实例互斥体检查；失败直接 return -1
├─ 2. WinSparkle 配置（注册表路径、界面语言）+ win_sparkle_init()
├─ 3. m_ui.Create(m_server.GetHWnd())   // 创建候选窗口，父窗口是 IPC 隐藏窗口
├─ 4. m_handler->Initialize()           // 真正初始化 librime（setup + 维护 + 读 weasel.yaml）
├─ 5. m_handler->OnUpdateUI([this](){ tray_icon.Refresh(); })
│        // 注册回调：引擎每次更新 UI 后，刷新托盘图标
├─ 6. tray_icon.Create(m_server.GetHWnd()) + tray_icon.Refresh()  // 托盘图标上桌
├─ 7. ret = m_server.Run()    // ★ 进入消息循环（阻塞，直到收到 WM_QUIT）
└─ 8. 清理：m_handler->Finalize() → m_ui.Destroy() → tray_icon.RemoveIcon() → win_sparkle_cleanup()
```

其中第 7 步 `m_server.Run()` 是**整个进程的「主心跳」**——它内部同时跑两条线程：

- **主线程**：WTL 的 `CMessageLoop`，负责处理窗口消息（包括托盘点击 `WM_COMMAND`、系统关机 `WM_ENDSESSION`、配色变化 `WM_DWMCOLORIZATIONCOLORCHANGED` 等）。
- **后台 `boost::thread`**：`PipeServer::Listen`，负责接受命名管道连接、把每条 `PipeMessage` 交给 `HandlePipeMessage` 派发（详见 u2-l3）。

进程退出靠 `m_server.Stop()`，它做的事情非常克制——只 `PostMessage(WM_QUIT)`，让主线程的消息循环自然结束，**绝不直接 `exit()`**，以便各对象有机会走析构清理。

#### 4.1.3 源码精读

先看装配。构造函数用初始化列表把 `m_handler` 绑到 `m_ui`、构造 `tray_icon(m_ui)`，随后注册处理器和菜单：

[WeaselServer/WeaselServerApp.cpp:5-11](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L5-L11) —— 构造函数：组装四大件。

```cpp
WeaselServerApp::WeaselServerApp()
    : m_handler(std::make_unique<RimeWithWeaselHandler>(&m_ui)),
      tray_icon(m_ui) {
  m_server.SetRequestHandler(m_handler.get());
  SetupMenuHandlers();
}
```

> 解读：`m_handler` 持有 `&m_ui`（引擎要把候选/状态画到 UI 上）；`tray_icon` 持有 `m_ui.style()` 与 `m_ui.status()` 的引用（托盘要根据状态切图标）；`m_server` 持有 `m_handler.get()`（IPC 命令要转交给引擎）。这张「指针网」就是整个进程的神经。

成员声明顺序决定了构造顺序，可以在头文件里核对：

[WeaselServer/WeaselServerApp.h:66-69](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.h#L66-L69) —— 四大件的声明顺序。

```cpp
weasel::Server m_server;
weasel::UI m_ui;
WeaselTrayIcon tray_icon;
std::unique_ptr<RimeWithWeaselHandler> m_handler;
```

再看运行阶段的启动序列：

[WeaselServer/WeaselServerApp.cpp:15-46](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L15-L46) —— `Run()` 的完整启动与清理。

```cpp
int WeaselServerApp::Run() {
  if (!m_server.Start())
    return -1;
  // ... WinSparkle 配置见 4.3 ...
  win_sparkle_init();
  m_ui.Create(m_server.GetHWnd());

  m_handler->Initialize();
  m_handler->OnUpdateUI([this]() { tray_icon.Refresh(); });

  tray_icon.Create(m_server.GetHWnd());
  tray_icon.Refresh();

  int ret = m_server.Run();   // 阻塞：消息循环

  m_handler->Finalize();
  m_ui.Destroy();
  tray_icon.RemoveIcon();
  win_sparkle_cleanup();
  return ret;
}
```

> 解读：注意 `m_handler->Initialize()` 推迟到 `Run()` 里调用（而不是构造函数）。这是有意为之——`Initialize()` 会真正启动 librime、跑维护模式、读配置，比较重且可能失败；而构造函数应当轻量、不抛异常。第 33 行 `OnUpdateUI` 注册的 lambda 是本讲的「线索」之一：引擎每次算完字、更新完 UI，都会回调它去刷新托盘图标（让中/英状态实时反映）。

那么 `m_server.Run()` 到底在循环什么？答案在 `ServerImpl::Run()`：

[WeaselIPCServer/WeaselServerImpl.cpp:167-186](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L167-L186) —— 消息循环 + 命名管道监听线程。

```cpp
int ServerImpl::Run() {
  auto listener = [this](PipeMessage msg, PipeServer::Respond resp) -> void {
    std::lock_guard guard(g_api_mutex);
    HandlePipeMessage(msg, resp);
  };
  pipeThread = std::make_unique<boost::thread>(
      [this, &listener]() { channel->Listen(listener); });

  CMessageLoop theLoop;
  _Module.AddMessageLoop(&theLoop);
  int nRet = theLoop.Run();          // 主线程阻塞在这里
  _Module.RemoveMessageLoop();
  return nRet;
}
```

> 解读：后台 `boost::thread` 跑 `Listen`（接受管道连接、`_ProcessPipeThread` 逐条处理，详见 u2-l3）；主线程跑 `CMessageLoop::Run()`，处理窗口消息。两条线程经 `g_api_mutex` 串行化对 librime 的访问（因为 librime 非线程安全）。当主线程收到 `WM_QUIT`，`theLoop.Run()` 返回，进程进入清理。

最后看「怎么让消息循环退出」：

[WeaselIPCServer/WeaselServerImpl.cpp:158-163](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L158-L163) —— `Stop()` 只发 `WM_QUIT`，不强行终止。

```cpp
int ServerImpl::Stop() {
  // DO NOT exit process or finalize here
  // Let WeaselServer handle this
  PostMessage(WM_QUIT);
  return 0;
}
```

> 解读：注释点明了设计意图——`Stop()` 只负责「请求退出」，真正的清理（`Finalize`、`Destroy`、`RemoveIcon`）交给 `WeaselServerApp::Run()` 在 `m_server.Run()` 返回后统一做。`PostMessage(WM_QUIT)` 会把 `WM_QUIT` 投递到本窗口所属线程的消息队列，`CMessageLoop::Run()` 取到后即返回。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：验证「装配顺序」与「启动顺序」确实满足依赖关系，并理解改动的边界。

**操作步骤**：

1. 打开 [WeaselServer/WeaselServerApp.h:66-69](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.h#L66-L69)，确认成员声明顺序为 `m_server`、`m_ui`、`tray_icon`、`m_handler`。
2. 假设把 `tray_icon` 的声明**挪到 `m_ui` 之前**（仅思考，不要真改源码），问：构造函数里 `tray_icon(m_ui)` 会发生什么？
3. 打开 [WeaselServer/WeaselTrayIcon.cpp:12-18](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselTrayIcon.cpp#L12-L18)，看构造函数 `m_style(ui.style())`、`m_status(ui.status())` 是**引用绑定**。

**需要观察的现象 / 预期结果**：

- 若 `tray_icon` 在 `m_ui` 之前构造，`m_style`/`m_status` 会绑定到一个**尚未构造**的 `m_ui` 内的引用成员 → 未定义行为（很可能崩溃）。
- 结论（待本地验证编译器行为）：成员声明顺序不是风格问题，而是正确性约束。`WeaselServerApp` 的当前顺序是安全的。

> 本实践为「源码阅读型」，不需要编译运行；若要本地验证，可在分支上调整声明顺序后用 MSBuild 构建，观察是否出现运行期断言或崩溃。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `m_handler->Initialize()` 放在 `Run()` 里，而不放在构造函数？

> **参考答案**：构造函数应当轻量、尽量不抛异常，便于「先成功创建对象、再处理失败」。`Initialize()` 会真正初始化 librime（`setup`/`initialize`/维护模式、读 `weasel.yaml`），耗时且有失败可能；放在 `Run()` 里、且 `Run()` 已被 `_tWinMain` 的 `try/catch` 包住（见 4.2），失败可被捕获。

**练习 2**：`m_server.Run()` 同时驱动「窗口消息」和「命名管道」。如果直接在 `Stop()` 里调用 `ExitProcess(0)`，会丢失哪些清理？

> **参考答案**：会跳过 `m_handler->Finalize()`（引擎资源/词典未落盘）、`m_ui.Destroy()`、`tray_icon.RemoveIcon()`（托盘图标残留在屏幕上，直到鼠标划过才消失）、`win_sparkle_cleanup()`，以及 `boost::thread` 的 `interrupt`（管道监听线程被强杀）。因此 `Stop()` 选择 `PostMessage(WM_QUIT)` 让循环优雅退出。

---

### 4.2 命令行与单实例重启

#### 4.2.1 概念说明

`WeaselServer.exe` 不仅在「双击启动」时跑，还在很多场景被反复启动：

- 用户在托盘点「退出」后再「开始菜单 → 小狼毫」重新启动；
- 安装器/部署器 `WeaselDeployer.exe` 在重新部署后，会拉起一个新的 `WeaselServer`；
- 用户配置了「重启输入法」快捷键，本质就是再启动一次 `WeaselServer.exe`；
- Windows 在进程崩溃后通过「重启管理器」自动重启它。

这就带来一个核心问题：**如果已经有一个 `WeaselServer` 在跑，再启动一个该怎么办？** Weasel 的设计是「新实例让旧实例退场，然后自己接管」——即所谓**单实例 + 优雅重启**。此外，`_tWinMain` 还支持一组命令行参数，让同一个 EXE 既能当「服务主程序」，也能当「轻量命令工具」（如 `/q` 退出、`/ascii` 切英文）。

> 引入术语：
> - **单实例（single instance）**：全局只允许一个进程存活。Weasel 用**命名互斥体**实现。
> - **优雅退让**：新进程通过 IPC 通知旧进程自行退出，而不是强行 kill。
> - **`RegisterApplicationRestart`**：Windows API，向系统登记「本进程崩溃后请重新启动我」。

#### 4.2.2 核心流程

`_tWinMain` 处理命令行的总流程：

```
_tWinMain(lpstrCmdLine)
├─ 语言/系统/DPI/IME 等环境初始化
├─ SYSTEM 用户? → 直接退出（不在会话 0 跑）
├─ 分支 A: /userdir         → 打开用户数据目录，return 0
├─ 分支 B: /weaseldir       → 打开安装目录，return 0
├─ 分支 C: /ascii | /nascii → 连上正在跑的 Server，发 TRAY_COMMAND 切中/英，return 0
├─ 分支 D: 单实例 + 重启
│     ├─ client.Connect() 成功?（说明已有旧实例）
│     │     ├─ ShutdownServer()（发 WEASEL_IPC_SHUTDOWN_SERVER）
│     │     ├─ 若是 /q 或 /quit → 直接 return 0（仅退出，不重启）
│     │     └─ 否则重试最多 10 次（每 50ms）ShutdownServer，直到旧实例退场
│     └─ Connect() 失败? → 本机没有旧实例，继续
├─ 分支 E: /update → check_update()（手动检查更新）
├─ 创建用户数据目录
└─ WeaselServerApp app; RegisterApplicationRestart(NULL, 0); app.Run();
```

**单实例重启**的精髓在第 D 分支：新进程不直接调 `Start()` 建互斥体（那会因互斥体已存在而失败），而是**先用客户端身份连上旧实例的管道**，让它自己 `ShutdownServer()`，等它退出腾出互斥体后，再以服务端身份启动。重试循环（最多 10 次、每次 50ms）是为了应对「旧实例收到 `WM_QUIT` 后到真正退出」之间这段窗口期。

重启重试的时序可以用一个不等式概括「最坏等待时间」：

\[
T_{\text{wait}} \le 10 \times 50\,\text{ms} = 500\,\text{ms}
\]

若 500ms 内旧实例仍未退场（`retry >= 10`），新实例放弃接管、直接退出，避免两个实例互相「踢皮球」。

崩溃自重启则更简单——`RegisterApplicationRestart(NULL, 0)` 告诉 Windows「我崩了你帮我重启」，由系统的「重启管理器」负责，不需要 Weasel 自己写代码。

#### 4.2.3 源码精读

入口函数的前半段是环境初始化，几个值得注意的细节：

[WeaselServer/WeaselServer.cpp:21-46](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.cpp#L21-L46) —— `_tWinMain` 的环境准备。

```cpp
LANGID langId = get_language_id();
SetThreadUILanguage(langId);
SetThreadLocale(langId);

if (!IsWindowsBlueOrLaterEx()) { /* 弹窗：系统版本过低，退出 */ }
SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE);
ImmDisableIME(-1);   // 防止服务进程自己开输入法（避免递归）

WCHAR user_name[20] = {0};
DWORD size = _countof(user_name);
GetUserName(user_name, &size);
if (!_wcsicmp(user_name, L"SYSTEM")) {
  return 1;          // 不在 session 0 / SYSTEM 账户下运行
}
```

> 解读：
> - `ImmDisableIME(-1)` 很关键——服务进程本身不该再挂一个输入法，否则会「输入法调输入法」递归。
> - 拒绝在 `SYSTEM` 用户下运行：会话 0（session 0）没有交互桌面，输入法在那里没有意义；管道名也按用户隔离（见 u2-1 的 `GetPipeName()`）。

接下来是「轻量命令」分支（`/userdir`、`/weaseldir`、`/ascii`）：

[WeaselServer/WeaselServer.cpp:65-85](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.cpp#L65-L85) —— 轻量命令：打开目录 / 切换中英文。

```cpp
if (!wcscmp(L"/userdir", lpstrCmdLine)) {
  CreateDirectory(WeaselUserDataPath().c_str(), NULL);
  WeaselServerApp::explore(WeaselUserDataPath());
  return 0;
}
// ... /weaseldir 同理 ...
if (!wcscmp(L"/ascii", lpstrCmdLine) || !wcscmp(L"/nascii", lpstrCmdLine)) {
  weasel::Client client;
  bool ascii = !wcscmp(L"/ascii", lpstrCmdLine);
  if (client.Connect()) {                       // 连上正在运行的 Server
    if (ascii) client.TrayCommand(ID_WEASELTRAY_ENABLE_ASCII);
    else       client.TrayCommand(ID_WEASELTRAY_DISABLE_ASCII);
  }
  return 0;
}
```

> 解读：`/ascii` 这一支非常巧妙——它**不自己改中英状态**，而是作为一个「客户端」连上正在跑的 `WeaselServer`，发一条 `WEASEL_IPC_TRAY_COMMAND`（载荷是菜单 ID `ID_WEASELTRAY_ENABLE_ASCII`）。这样状态变更永远只发生在唯一的服务端进程里，避免多实例间状态不一致。`Client::TrayCommand` 的实现见 [WeaselIPC/WeaselClientImpl.cpp:141-143](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L141-L143)。

核心的「单实例 + 重启」分支：

[WeaselServer/WeaselServer.cpp:88-107](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.cpp#L88-L107) —— 让旧实例退场、自己接管。

```cpp
bool quit = !wcscmp(L"/q", lpstrCmdLine) || !wcscmp(L"/quit", lpstrCmdLine);
{
  weasel::Client client;
  if (client.Connect()) {              // 已有旧实例在跑
    client.ShutdownServer();           // 发 WEASEL_IPC_SHUTDOWN_SERVER
    if (quit) return 0;                // /q：只退出，不重启
    int retry = 0;
    while (client.Connect() && retry < 10) {
      client.ShutdownServer();
      retry++;
      Sleep(50);
    }
    if (retry >= 10) return 0;         // 旧实例赖着不走，放弃
  } else if (quit) return 0;           // 没有旧实例，/q 无意义
}
```

> 解读：`client.Connect()` 返回 true 意味着能连上命名管道，即「已有旧实例」。`ShutdownServer()` 让旧实例 `PostMessage(WM_QUIT)`。重试循环解决「旧实例退出需要时间」的窗口期。注意 `Connect()` 内部对 `ERROR_PIPE_BUSY` 也有 `WaitNamedPipe` 处理（见 [WeaselIPC/PipeChannel.cpp:58-69](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/PipeChannel.cpp#L58-L69)），所以这里的 `Connect()` 不会因为「管道实例忙」而误判为「无旧实例」。

那么「单实例」的硬保证在哪？在 `ServerImpl::Start()`：

[WeaselIPCServer/WeaselServerImpl.cpp:142-156](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L142-L156) —— 命名互斥体保证全局唯一。

```cpp
HWND ServerImpl::Start() {
  std::wstring instanceName = L"(WEASEL)Furandōru-Sukāretto-";
  instanceName += getUsername();
  HANDLE hMutexOneInstance = ::CreateMutex(NULL, FALSE, instanceName.c_str());
  bool areYouOK = (::GetLastError() == ERROR_ALREADY_EXISTS ||
                   ::GetLastError() == ERROR_ACCESS_DENIED);
  if (areYouOK) {
    return 0;            // 已有实例：返回空 HWND，WeaselServerApp::Run 会 return -1
  }
  HWND hwnd = Create(NULL);   // 创建 IPC 隐藏窗口
  return hwnd;
}
```

> 解读：互斥体名里带 `getUsername()`，所以**按用户隔离**——同一台机器上多个 Windows 用户各有各的 `WeaselServer`（与管道按用户隔离一致，见 u2-1）。互斥体名前缀 `(WEASEL)Furandōru-Sukāretto-`（「芙兰朵露·斯卡蕾特」，一个彩蛋）是为了避免与其它程序的同名互斥体撞车。若已存在，`Start()` 返回 0，[WeaselServerApp.cpp:16-17](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L16-L17) 的 `if (!m_server.Start()) return -1;` 让进程直接退出。

最后是崩溃自重启与主启动：

[WeaselServer/WeaselServer.cpp:109-124](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.cpp#L109-L124) —— `/update`、创建目录、装配并运行。

```cpp
bool check_updates = !wcscmp(L"/update", lpstrCmdLine);
if (check_updates) {
  WeaselServerApp::check_update();   // 手动检查更新（见 4.3）
}
CreateDirectory(WeaselUserDataPath().c_str(), NULL);

int nRet = 0;
try {
  WeaselServerApp app;
  RegisterApplicationRestart(NULL, 0);   // 向系统登记：崩溃后帮我重启
  nRet = app.Run();
} catch (...) {
  nRet = -1;   // bad luck...
}
```

> 解读：`RegisterApplicationRestart(NULL, 0)` 是「崩溃自愈」的关键——若 `WeaselServer.exe` 因访问违例等原因崩溃，Windows 的重启管理器会自动把它再拉起来（用户几乎无感）。`try/catch(...)` 是最后一道兜底，避免异常逃逸到系统导致错误码弹窗。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：跟踪一次「重启输入法」的全过程，确认新实例如何让旧实例退场。

**操作步骤**：

1. 假设旧实例正在运行。新执行 `WeaselServer.exe`（无参数）。
2. 在源码中跟踪：`_tWinMain` → `client.Connect()`（[WeaselServer/WeaselServer.cpp:92](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.cpp#L92)）→ `client.ShutdownServer()`。
3. 跟踪 `ShutdownServer` 的实现 [WeaselIPC/WeaselClientImpl.cpp:54-56](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L54-L56)：它发 `WEASEL_IPC_SHUTDOWN_SERVER`。
4. 在服务端跟踪 [WeaselIPCServer/WeaselServerImpl.cpp:228-233](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L228-L233)：`OnShutdownServer` 调 `Stop()` → `PostMessage(WM_QUIT)`。
5. 旧实例的消息循环退出、做清理、进程结束、互斥体释放。
6. 新实例的重试循环 `Connect()` 失败（旧实例已没），跳出循环 → `app.Run()` → `m_server.Start()` 成功拿到互斥体 → 接管。

**需要观察的现象 / 预期结果**：

- 整个过程对正在打字的用户来说表现为「候选窗口/托盘图标闪一下后恢复」，输入会话会被重建（因为是新进程）。
- 重试上限 10 次、间隔 50ms，最坏 ~500ms。

> 本实践为「调用链跟踪型」。若要本地验证，可在 Windows 上安装小狼毫后，用 Process Explorer 观察两次启动期间 PID 的变化。

#### 4.2.5 小练习与答案

**练习 1**：`/q`（quit）和「无参数重启」对旧实例的处理有何不同？

> **参考答案**：两者都先 `ShutdownServer()` 让旧实例退出。区别在于之后：`/q` 在旧实例退出后 `return 0`，**不启动新实例**（纯退出）；无参数则在旧实例退场后继续走 `app.Run()` **启动新实例**接管（重启）。

**练习 2**：为什么互斥体名要拼接 `getUsername()`，而不是用固定的全局名？

> **参考答案**：为了按用户隔离。终端服务器或多用户机器上，A 用户和 B 用户各有各的桌面与会话，应各有独立的 `WeaselServer` 与命名管道（管道名同样按用户隔离，见 u2-1 的 `GetPipeName()`）。若用固定全局名，B 用户登录时 `Start()` 会因 A 的实例占着互斥体而失败，无法输入。

**练习 3**：如果 `retry >= 10`（旧实例 500ms 内没退出），新实例直接 `return 0`。此时用户会观察到什么？这个设计是否合理？

> **参考答案**：新实例不启动，旧实例若也已在退出途中，结果是「没有 `WeaselServer` 在跑」→ 打字时无候选。设计上宁可「短暂无输入法」也不要「两个实例并存互踢」；用户/系统会通过 `RegisterApplicationRestart` 或下次按键触发再次拉起。这是「最终一致」的取舍。

---

### 4.3 托盘菜单与 WinSparkle 更新

#### 4.3.1 概念说明

本模块讲「用户看得见、点得到」的部分：

1. **托盘图标**：一个小图标 + tooltip「小狼毫」/「Weasel」。它随输入法状态变化——中文（ZHUNG）、英文（ASCII）、维护中（DISABLED）。
2. **右键菜单**：右键托盘弹出「输入法设定 / 用户词典管理 / 用户资料同步 / 各种文件夹 / 帮助 / 检查新版本 / 重新部署 / 退出」。
3. **菜单分发**：点击菜单项后，命令是怎么路由到 `WeaselDeployer.exe`、浏览器、或 `Stop()` 的。
4. **WinSparkle 自动更新**：后台静默检查 + 用户主动「检查新版本」。

这里有两个关键的**统一**：

- **本地点击与跨进程 IPC 的统一**：用户在托盘右键点击（产生本地 `WM_COMMAND`）和别的进程通过 `client.TrayCommand()` 发来的命令（产生 `WEASEL_IPC_TRAY_COMMAND`），最终都汇入**同一个 `OnCommand` 分发函数**、查**同一张 `m_MenuHandlers` 表**。这样无论命令从哪来，行为一致。
- **托盘刷新与引擎状态的统一**：引擎每次更新 UI（算完字/切了方案/切了中英），都会经 `OnUpdateUI` 回调 `tray_icon.Refresh()`，让图标实时反映状态。

`WeaselTrayIcon` 继承自 `CSystemTray`（[SystemTraySDK.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/SystemTraySDK.h)）。`CSystemTray` 是 Chris Maunder 在 1998 年写的经典轻量封装（注释里写着「Expect bugs」），把 `Shell_NotifyIcon`、隐藏窗口、右键菜单、Explorer 崩溃恢复等脏活全包了；Weasel 只需重写 `Refresh()` 来做状态切换。

#### 4.3.2 核心流程

**图标创建**（`WeaselTrayIcon::Create`）：

```
Create(hTargetWnd)
├─ CSystemTray::Create(模块句柄, 回调消息 = WM_WEASEL_TRAY_NOTIFY,
│                      tooltip = "小狼毫", 初始图标 = IDI_ZH, 菜单 = IDR_MENU_POPUP)
│     ├─ RegisterClass("TrayIconClass") + CreateWindow(隐藏窗口)
│     ├─ 填 NOTIFYICONDATA（uCallbackMessage / hIcon / szTip）
│     └─ Shell_NotifyIcon(NIM_ADD)  // 上桌
├─ SetTargetWnd(hTargetWnd)   // 鼠标消息转发给 IPC 隐藏窗口
└─ 依 m_style.display_tray_icon 决定 AddIcon / RemoveIcon
```

**图标刷新**（`WeaselTrayIcon::Refresh`）——这是状态机的核心：

```
Refresh()
├─ 若「不显示图标 且 非维护中」→ RemoveIcon，return
├─ 计算 mode = disabled ? DISABLED : (ascii_mode ? ASCII : ZHUNG)
├─ 若 mode 变了 或 图标变了 或 首次初始化：
│     ├─ ShowIcon()
│     ├─ 按 mode 选图标：
│     │     ├─ ASCII → 方案自定义 ascii 图标，否则 IDI_EN
│     │     ├─ ZHUNG → 方案自定义中文图标，否则 IDI_ZH
│     │     └─ DISABLED → IDI_RELOAD
│     └─ 若是 DISABLED 且首次 → ShowBalloon("正在维护…")
└─ 否则若不可见 → ShowIcon()  // 兜底显示
```

**菜单分发**（点击菜单项）有两条入口，汇入一处：

```
路径 A：本地点击
  鼠标右键托盘 → CSystemTray::OnTrayNotification → TrackPopupMenu
  → 用户点某项 → PostMessage(WM_COMMAND, ID_XXX) 到 IPC 隐藏窗口
  → ServerImpl 消息映射 MESSAGE_HANDLER(WM_COMMAND, OnCommand)
  → OnCommand(UINT, WPARAM, LPARAM, BOOL&)

路径 B：跨进程 IPC
  别的进程 client.TrayCommand(ID_XXX)
  → 管道消息 WEASEL_IPC_TRAY_COMMAND
  → HandlePipeMessage → PIPE_MSG_HANDLE(WEASEL_IPC_TRAY_COMMAND, OnCommand)
  → OnCommand(WEASEL_IPC_COMMAND, DWORD, DWORD) → 转调上面的 OnCommand(UINT,...)

  OnCommand 统一逻辑：
  ├─ 若是 ENABLE_ASCII / DISABLE_ASCII → m_pRequestHandler->SetOption(ascii_mode)  // 特例
  └─ 否则查 m_MenuHandlers 表 → 调对应 lambda
```

**WinSparkle 自动更新**：

```
启动时（Run 里）：
  win_sparkle_set_registry_path("Software\\Rime\\Weasel\\Updates")
  win_sparkle_set_lang("zh-CN"/"zh-TW"/"en")
  win_sple_init()   // 后台线程定期检查 appcast.xml

用户点「检查新版本」菜单（ID_WEASELTRAY_CHECKUPDATE）：
  check_update()
  ├─ 读注册表 UpdateChannel（"testing"?）
  ├─ GetCustomResource 取对应 APPCAST URL（稳定 / 测试）
  ├─ win_sparkle_set_appcast_url(url)
  └─ win_sparkle_check_update_with_ui()   // 带界面、立即检查
```

#### 4.3.3 源码精读

**图标状态机**——先看 `Refresh()` 如何根据 `Status` 选图标：

[WeaselServer/WeaselTrayIcon.cpp:40-93](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselTrayIcon.cpp#L40-L93) —— 按中/英/维护三态切换图标。

```cpp
void WeaselTrayIcon::Refresh() {
  if (!m_style.display_tray_icon && !m_status.disabled) {
    if (m_mode != INITIAL) { RemoveIcon(); m_mode = INITIAL; }
    m_disabled = false;
    return;
  }
  WeaselTrayMode mode = m_status.disabled     ? DISABLED
                        : m_status.ascii_mode ? ASCII
                                              : ZHUNG;
  if (mode != m_mode || m_schema_zhung_icon != m_style.current_zhung_icon ||
      /* ...图标变化或首次初始化... */) {
    ShowIcon();
    m_mode = mode;
    // ...记录上次图标，便于下次比较...
    if (mode == ASCII) {
      if (m_schema_ascii_icon.empty()) SetIcon(mode_icon[mode]);  // IDI_EN
      else SetIcon(m_schema_ascii_icon.c_str());                   // 方案自定义
    } else if (mode == ZHUNG) { /* 中文同理 */ }
    else SetIcon(mode_icon[mode]);                                 // IDI_RELOAD
    // ...
  } else if (!Visible()) { ShowIcon(); }
}
```

> 解读：关键优化是「只有状态变化才真正 `SetIcon`」，避免每次按键（都会触发 `OnUpdateUI` 回调）都调 `Shell_NotifyIcon`。`mode_icon[]` 是内置图标数组（`IDI_ZH`/`IDI_EN`/`IDI_RELOAD`，见 [WeaselServer/WeaselTrayIcon.cpp:8](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselTrayIcon.cpp#L8)），而 `m_schema_*_icon` 允许输入方案自定义图标（如「明月拼音」用不同的图标）。`m_style`/`m_status` 是对 `m_ui` 内部状态的**引用**（见构造函数 [WeaselTrayIcon.cpp:12-18](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselTrayIcon.cpp#L12-L18)），所以引擎一改状态，这里立刻能看到。

`WeaselTrayMode` 枚举与回调消息号定义在头文件：

[WeaselServer/WeaselTrayIcon.h:6-15](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselTrayIcon.h#L6-L15) —— 状态枚举与回调消息。

```cpp
#define WM_WEASEL_TRAY_NOTIFY (WEASEL_IPC_LAST_COMMAND + 100)

class WeaselTrayIcon : public CSystemTray {
 public:
  enum WeaselTrayMode { INITIAL, ZHUNG, ASCII, DISABLED };
  // ...
};
```

> 解读：`WM_WEASEL_TRAY_NOTIFY` 是托盘鼠标事件的回调消息号，刻意取 `WEASEL_IPC_LAST_COMMAND + 100` 以避免与 IPC 命令号（从 `WM_APP+1` 起）冲突。它必须 ≥ `WM_APP`（`CSystemTray::Create` 里有 `ASSERT(uCallbackMessage >= WM_APP)`）。

`Create()` 把这个回调消息号、菜单资源、初始图标交给基类：

[WeaselServer/WeaselTrayIcon.cpp:22-38](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselTrayIcon.cpp#L22-L38) —— 注册托盘图标。

```cpp
BOOL WeaselTrayIcon::Create(HWND hTargetWnd) {
  HMODULE hModule = GetModuleHandle(NULL);
  CIcon icon;
  icon.LoadIconW(IDI_ZH);
  BOOL bRet = CSystemTray::Create(hModule, NULL, WM_WEASEL_TRAY_NOTIFY,
                          get_weasel_ime_name().c_str(), icon, IDR_MENU_POPUP);
  if (hTargetWnd) SetTargetWnd(hTargetWnd);
  if (!m_style.display_tray_icon) RemoveIcon();
  else AddIcon();
  return bRet;
}
```

> 解读：`get_weasel_ime_name()` 按用户界面语言返回「小狼毫」或「Weasel」（见 [WeaselUtility.h:189-201](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUtility.h#L189-L201)），作为 tooltip。`IDR_MENU_POPUP` 是右键菜单资源（简/繁/英三语版本，见 [WeaselServer.rc:151](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.rc#L151)）。`SetTargetWnd` 把菜单命令的投递目标设为 IPC 隐藏窗口，这样点击菜单项产生的 `WM_COMMAND` 会进入 `ServerImpl` 的消息映射——这就是「本地点击」与 IPC 汇合的物理基础。

基类 `CSystemTray::Create` 真正调用 `Shell_NotifyIcon(NIM_ADD)`：

[WeaselServer/SystemTraySDK.cpp:150-272](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/SystemTraySDK.cpp#L150-L272) —— 创建隐藏窗口并注册图标（节选关键段）。

```cpp
// 注册 TrayIconClass、创建隐藏窗口 m_hWnd
m_tnd.cbSize = NOTIFYICONDATA_V2_SIZE;   // XP 兼容
m_tnd.hWnd = (hParent) ? hParent : m_hWnd;
m_tnd.uID = uID;
m_tnd.hIcon = icon;
m_tnd.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
m_tnd.uCallbackMessage = uCallbackMessage;
// ...
bResult = Shell_NotifyIcon(NIM_ADD, &m_tnd);   // 图标上桌
```

> 解读：`NOTIFYICONDATA`（简称 `m_tnd`）是托盘图标的「身份证」——回调消息号、图标句柄、tooltip 全在里面，所有后续修改（换图标、隐藏、弹气泡）都是改它再 `Shell_NotifyIcon(NIM_MODIFY)`。`cbSize` 用 `NOTIFYICONDATA_V2_SIZE` 是为了在 XP 上也能跑（注释「2012-01-05 GONG Chen, XP compatibility」）。

右键菜单的弹出在 `OnTrayNotification`：

[WeaselServer/SystemTraySDK.cpp:759-811](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/SystemTraySDK.cpp#L759-L811) —— 右键弹出菜单、双击执行默认项（节选）。

```cpp
LRESULT CSystemTray::OnTrayNotification(WPARAM wParam, LPARAM lParam) {
  if (wParam != m_tnd.uID) return 0L;
  HWND hTargetWnd = GetTargetWnd();
  if (!hTargetWnd) return 0L;
  if (LOWORD(lParam) == WM_RBUTTONUP) {            // 右键
    HMENU hMenu = ::LoadMenu(m_hInstance, MAKEINTRESOURCE(m_tnd.uID));
    HMENU hSubMenu = ::GetSubMenu(hMenu, 0);
    CustomizeMenu(hSubMenu);                        // WeaselTrayIcon 留的扩展点（空实现）
    GetCursorPos(&pos);
    ::SetForegroundWindow(m_tnd.hWnd);
    ::TrackPopupMenu(hSubMenu, 0, pos.x, pos.y, 0, hTargetWnd, NULL);
    ::PostMessage(m_tnd.hWnd, WM_NULL, 0, 0);       // 经典 bugfix
    DestroyMenu(hMenu);
  }
  else if (LOWORD(lParam) == WM_LBUTTONDBLCLK) { /* 双击执行默认项 */ }
  return 1;
}
```

> 解读：`TrackPopupMenu` 是模态弹出菜单，用户点某项后，Windows 向 `hTargetWnd`（即 IPC 隐藏窗口）`PostMessage(WM_COMMAND, 菜单ID, 0)`。那句 `PostMessage(WM_NULL)` 是微软文档里的经典修复——见注释「PRB: Menus for Notification Icons Don't Work Correctly」，目的是让菜单立即消失。`CustomizeMenu` 是基类留给子类的虚函数，Weasel 重写为空（[WeaselTrayIcon.cpp:20](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselTrayIcon.cpp#L20)），目前没用。

**菜单分发**——`ServerImpl` 的消息映射接收 `WM_COMMAND`：

[WeaselIPCServer/WeaselServerImpl.h:22-31](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.h#L22-L31) —— WTL 消息映射表。

```cpp
BEGIN_MSG_MAP(WEASEL_IPC_WINDOW)
MESSAGE_HANDLER(WM_CREATE, OnCreate)
MESSAGE_HANDLER(WM_DESTROY, OnDestroy)
MESSAGE_HANDLER(WM_CLOSE, OnClose)
MESSAGE_HANDLER(WM_QUERYENDSESSION, OnQueryEndSystemSession)
MESSAGE_HANDLER(WM_ENDSESSION, OnEndSystemSession)
MESSAGE_HANDLER(WM_DWMCOLORIZATIONCOLORCHANGED, OnColorChange)
MESSAGE_HANDLER(WM_SETTINGCHANGE, OnColorChange)
MESSAGE_HANDLER(WM_COMMAND, OnCommand)     // ★ 菜单命令入口
END_MSG_MAP()
```

`OnCommand` 先处理两个特例（中英切换），再查表：

[WeaselIPCServer/WeaselServerImpl.cpp:110-132](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L110-L132) —— 菜单命令统一分发。

```cpp
LRESULT ServerImpl::OnCommand(UINT uMsg, WPARAM wParam, LPARAM lParam, BOOL& bHandled) {
  UINT uID = LOWORD(wParam);
  switch (uID) {
    case ID_WEASELTRAY_ENABLE_ASCII:
      m_pRequestHandler->SetOption(lParam, "ascii_mode", true);   return 0;
    case ID_WEASELTRAY_DISABLE_ASCII:
      m_pRequestHandler->SetOption(lParam, "ascii_mode", false);  return 0;
    default:;
  }
  std::map<UINT, CommandHandler>::iterator it = m_MenuHandlers.find(uID);
  if (it == m_MenuHandlers.end()) { bHandled = FALSE; return 0; }
  it->second();   // 执行注册的 lambda
  return 0;
}
```

> 解读：`ENABLE_ASCII`/`DISABLE_ASCII` 是特例——它们不调外部程序，而是直接 `SetOption` 改引擎的 `ascii_mode`（因此 `/ascii` 命令最终落到这里）。其它菜单 ID 走 `m_MenuHandlers` 这张「ID → lambda」表。注意 `lParam` 在 IPC 路径里其实是 `session_id`（见 `TrayCommand(menuId)` 发的是 `(menuId, session_id)`），所以 `SetOption` 的第一个参数是会话 ID。

跨进程的 `WEASEL_IPC_TRAY_COMMAND` 如何汇入同一个 `OnCommand`？看命令派发表：

[WeaselIPCServer/WeaselServerImpl.cpp:381-403](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L381-L403) —— `HandlePipeMessage` 把 `TRAY_COMMAND` 路由到 `OnCommand`。

```cpp
MAP_PIPE_MSG_HANDLE(pipe_msg.Msg, pipe_msg.wParam, pipe_msg.lParam)
// ... 其它命令 ...
PIPE_MSG_HANDLE(WEASEL_IPC_TRAY_COMMAND, OnCommand);   // ★ 复用 OnCommand
END_MAP_PIPE_MSG_HANDLE(result);
resp(result);
```

> 解读：`PIPE_MSG_HANDLE(WEASEL_IPC_TRAY_COMMAND, OnCommand)` 让 IPC 来的托盘命令调用 `OnCommand(WEASEL_IPC_COMMAND, DWORD wParam, DWORD lParam)` 这个重载（[WeaselServerImpl.cpp:134-140](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L134-L140)），它内部又转调上面那个 `OnCommand(UINT, WPARAM, LPARAM, BOOL&)`。于是「本地点击」和「跨进程 IPC」两条路在 `OnCommand` 汇合——同一张菜单表、同一份行为。

**菜单表注册**——`SetupMenuHandlers` 把每个 ID 绑到一个动作：

[WeaselServer/WeaselServerApp.cpp:48-76](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L48-L76) —— 注册 11 个菜单命令。

```cpp
void WeaselServerApp::SetupMenuHandlers() {
  std::filesystem::path dir = install_dir();
  m_server.AddMenuHandler(ID_WEASELTRAY_QUIT,        [this] { return m_server.Stop() == 0; });
  m_server.AddMenuHandler(ID_WEASELTRAY_DEPLOY,      std::bind(execute, dir / L"WeaselDeployer.exe", std::wstring(L"/deploy")));
  m_server.AddMenuHandler(ID_WEASELTRAY_SETTINGS,    std::bind(execute, dir / L"WeaselDeployer.exe", std::wstring()));
  m_server.AddMenuHandler(ID_WEASELTRAY_DICT_MANAGEMENT, std::bind(execute, dir / L"WeaselDeployer.exe", std::wstring(L"/dict")));
  m_server.AddMenuHandler(ID_WEASELTRAY_SYNC,        std::bind(execute, dir / L"WeaselDeployer.exe", std::wstring(L"/sync")));
  m_server.AddMenuHandler(ID_WEASELTRAY_WIKI,        std::bind(open, L"https://rime.im/docs/"));
  m_server.AddMenuHandler(ID_WEASELTRAY_HOMEPAGE,    std::bind(open, L"https://rime.im/"));
  m_server.AddMenuHandler(ID_WEASELTRAY_FORUM,       std::bind(open, L"https://rime.im/discuss/"));
  m_server.AddMenuHandler(ID_WEASELTRAY_CHECKUPDATE, check_update);
  m_server.AddMenuHandler(ID_WEASELTRAY_INSTALLDIR,  std::bind(explore, dir));
  m_server.AddMenuHandler(ID_WEASELTRAY_USERCONFIG,  std::bind(explore, WeaselUserDataPath()));
  m_server.AddMenuHandler(ID_WEASELTRAY_LOGDIR,      std::bind(explore, WeaselLogPath()));
}
```

> 解读：四类动作清晰可辨——
> - **退出**：`ID_WEASELTRAY_QUIT` → `m_server.Stop()`（发 `WM_QUIT`）。
> - **启动 Deployer**：`DEPLOY`/`SETTINGS`/`DICT_MANAGEMENT`/`SYNC` 都用 `execute` 拉起 `WeaselDeployer.exe`，靠不同命令行参数（`/deploy`、无参、`/dict`、`/sync`）区分模式（详见 u6-l1）。
> - **打开网页**：`WIKI`/`HOMEPAGE`/`FORUM` 用 `open` 调默认浏览器。
> - **打开文件夹**：`INSTALLDIR`/`USERCONFIG`/`LOGDIR` 用 `explore` 打开资源管理器。
> - **检查更新**：`CHECKUPDATE` → 静态方法 `check_update`。
>
> 这些 `execute`/`explore`/`open` 都是 `WeaselServerApp` 的静态工具函数，本质是 `ShellExecuteW`（见 [WeaselServerApp.h:20-34](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.h#L20-L34)）。`AddMenuHandler` 的实现仅仅是往 `std::map<UINT, CommandHandler>` 里塞一条（[WeaselServerImpl.h:85-87](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.h#L85-L87)），是天然的扩展点。

菜单项的中文文案与 ID 在资源文件里一一对应：

[WeaselServer/WeaselServer.rc:151-168](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.rc#L151-L168) —— 简体托盘菜单。

```rc
IDR_MENU_POPUP MENU
    POPUP "WeaselTray"
        MENUITEM "输入法设定 (&S)",          ID_WEASELTRAY_SETTINGS
        MENUITEM "用户词典管理 (&D)",         ID_WEASELTRAY_DICT_MANAGEMENT
        MENUITEM "用户资料同步 (&N)",         ID_WEASELTRAY_SYNC
        MENUITEM SEPARATOR
        MENUITEM "用户文件夹 (&C)",          ID_WEASELTRAY_USERCONFIG
        MENUITEM "程序文件夹(&P)",           ID_WEASELTRAY_INSTALLDIR
        MENUITEM "日志文件夹 (&L)",          ID_WEASELTRAY_LOGDIR
        MENUITEM SEPARATOR
        MENUITEM "帮助文档 (&H)",           ID_WEASELTRAY_WIKI
        MENUITEM "参加讨论 (&J)",           ID_WEASELTRAY_FORUM
        MENUITEM SEPARATOR
        MENUITEM "检查新版本 (&U)",          ID_WEASELTRAY_CHECKUPDATE
        MENUITEM "重新部署 (&R)",           ID_WEASELTRAY_DEPLOY
        MENUITEM "退出 (&Q)",             ID_WEASELTRAY_QUIT
```

> 解读：资源文件有简/繁/英三套（按编译时的语言宏选择），但 ID 相同，所以 `SetupMenuHandlers` 注册一次即可三语通用。ID 的数值定义在 [WeaselServer/resource.h:15-29](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/resource.h#L15-L29)（如 `ID_WEASELTRAY_QUIT = 40001`）。

**WinSparkle 自动更新**——启动时配置，运行时后台检查：

[WeaselServer/WeaselServerApp.cpp:19-29](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L19-L29) —— 启动时初始化 WinSparkle。

```cpp
win_sparkle_set_registry_path("Software\\Rime\\Weasel\\Updates");
if (GetThreadUILanguage() == MAKELANGID(LANG_CHINESE, SUBLANG_CHINESE_TRADITIONAL))
  win_sparkle_set_lang("zh-TW");
else if (GetThreadUILanguage() == MAKELANGID(LANG_CHINESE, SUBLANG_CHINESE_SIMPLIFIED))
  win_sparkle_set_lang("zh-CN");
else
  win_sparkle_set_lang("en");
win_sparkle_init();
```

> 解读：`win_sparkle_set_registry_path` 告诉 WinSparkle 把「上次检查时间/是否跳过某版本」等状态存在 `HKCU\Software\Rime\Weasel\Updates` 下（与 Weasel 自己的配置共用一个注册表根）。`win_sparkle_set_lang` 让更新提示框用对应语言。`win_sparkle_init()` 启动后台线程，定期拉取 `appcast.xml` 比较版本号——**这一步是静默的**，发现新版本才弹窗。

用户主动「检查新版本」走的是带 UI、且可看测试版的逻辑：

[WeaselServer/WeaselServerApp.h:36-50](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.h#L36-L50) —— 手动检查更新，支持「测试通道」。

```cpp
static bool check_update() {
  // when checked manually, show testing versions too
  std::string feed_url = GetCustomResource("ManualUpdateFeedURL", "APPCAST");
  std::wstring channel{};
  auto ret = RegGetStringValue(HKEY_CURRENT_USER, L"Software\\Rime\\Weasel",
                               L"UpdateChannel", channel);
  if (!ret && channel == L"testing") {
    feed_url = GetCustomResource("TestingManualUpdateFeedURL", "APPCAST");
  }
  if (!feed_url.empty()) {
    win_sparkle_set_appcast_url(feed_url.c_str());
  }
  win_sparkle_check_update_with_ui();
  return true;
}
```

> 解读：
> - `appcast` URL 不是写死在代码里，而是存在 PE 资源里（`GetCustomResource(..., "APPCAST")`），便于改地址而不必重编译。资源定义见 [WeaselServer.rc:54-83](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.rc#L54-L83)，共 4 条：`FEEDURL`/`MANUALUPDATEFEEDURL`/`TESTINGFEEDURL`/`TESTINGMANUALUPDATEFEEDURL`（内容为 UTF-16 编码的 URL）。
> - 「更新通道」由注册表 `HKCU\Software\Rime\Weasel\UpdateChannel` 控制：值为 `testing` 时，手动检查会拉测试版 feed，否则拉稳定版。
> - 区分「自动」与「手动」两套 feed：手动检查（用户明确想升级）会展示更多版本（含测试版），自动检查更保守。
> - `_tWinMain` 的 `/update` 分支调的就是这个 `check_update()`（[WeaselServer.cpp:109-112](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.cpp#L109-L112)），让安装器等外部程序也能触发更新检查。

**状态联动**——最后串起「引擎状态 → 托盘图标」的回调链。`Run()` 里注册的回调：

[WeaselServer/WeaselServerApp.cpp:33](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L33) —— 引擎更新 UI 后刷新托盘。

```cpp
m_handler->OnUpdateUI([this]() { tray_icon.Refresh(); });
```

引擎侧 `RimeWithWeaselHandler::_UpdateUI` 在每次按键/切方案/焦点变化后都会调用这个回调（见 [RimeWithWeasel.cpp:544](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L544) 的 `_RefreshTrayIcon(session_id, _UpdateUICallback)`）。于是：用户按 Shift 切英文 → 引擎改 `ascii_mode` → `_UpdateUI` → 回调 → `tray_icon.Refresh()` → 图标从 `IDI_ZH` 变 `IDI_EN`。一条完整的状态同步链。

#### 4.3.4 代码实践（源码阅读型 + 配置观察）

**实践目标**：理解「添加一个新菜单项」需要改哪几处，并观察 WinSparkle 的更新通道配置。

**操作步骤**：

1. **追踪菜单项的生命周期**：以「重新部署」为例，从资源到动作完整走一遍：
   - 资源定义：[WeaselServer.rc:167](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.rc#L167)（`MENUITEM "重新部署 (&R)", ID_WEASELTRAY_DEPLOY`）。
   - ID 数值：[resource.h:16](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/resource.h#L16)（`ID_WEASELTRAY_DEPLOY = 40002`）。
   - 动作注册：[WeaselServerApp.cpp:52-54](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L52-L54)（`execute(WeaselDeployer.exe /deploy)`）。
   - 派发路径：用户右键点击 → `OnTrayNotification` → `TrackPopupMenu` → `WM_COMMAND(40002)` → `ServerImpl::OnCommand` → `m_MenuHandlers[40002]()` → `execute` 拉起 Deployer。

2. **假设新增一个菜单项「打开日志」**（实际上 `ID_WEASELTRAY_LOGDIR` 已经是做这个的），列出需要改动的位置：
   - `resource.h`：新增一个 `ID_WEASELTRAY_*` 数值（注意不要与现有的 40001–40016 冲突）。
   - `WeaselServer.rc`：在 `IDR_MENU_POPUP` 的三语版本各加一行 `MENUITEM`。
   - `WeaselServerApp.cpp::SetupMenuHandlers`：`AddMenuHandler(新ID, 动作)`。

3. **观察 WinSparkle 更新通道**（若本地已安装小狼毫）：用 `regedit` 查看 `HKEY_CURRENT_USER\Software\Rime\Weasel\Updates`（WinSparkle 的状态）和 `UpdateChannel`（是否为 `testing`）。**待本地验证**——仅在有 Windows 环境且已安装时进行。

**需要观察的现象 / 预期结果**：

- 一个菜单项的完整链路是「资源（文案+ID）→ resource.h（ID 数值）→ SetupMenuHandlers（ID→动作）→ OnCommand（派发）」。三处缺一不可。
- `Updates` 注册表项里会有 `LastCheckTime` 之类的值；`UpdateChannel` 不存在时默认走稳定通道。

#### 4.3.5 小练习与答案

**练习 1**：用户右键托盘点「退出」，和别的进程执行 `WeaselServer.exe /q`，最终调到的是不是同一段代码？

> **参考答案**：是。前者：`OnTrayNotification` → `WM_COMMAND(ID_WEASELTRAY_QUIT)` → `OnCommand` → `m_MenuHandlers[QUIT]` → `m_server.Stop()`。后者：`_tWinMain` 检测到 `/q`，作为**客户端**连上旧实例发 `WEASEL_IPC_SHUTDOWN_SERVER` → `OnShutdownServer` → `Stop()`。两条路最终都到 `ServerImpl::Stop()`（`PostMessage(WM_QUIT)`）。区别只是「谁发起」与「走不走菜单表」。

**练习 2**：`/ascii` 切换英文，为什么图标会跟着变？请把调用链说全。

> **参考答案**：`WeaselServer.exe /ascii` →（客户端）`client.TrayCommand(ID_WEASELTRAY_ENABLE_ASCII)` →（管道）`WEASEL_IPC_TRAY_COMMAND` → `HandlePipeMessage` → `OnCommand` → `SetOption(session_id, "ascii_mode", true)` → 引擎改 option → 下一次 `_UpdateUI`（或 option 变化触发）→ `_RefreshTrayIcon` → `_UpdateUICallback` → `tray_icon.Refresh()` → `mode` 变为 `ASCII` → `SetIcon(IDI_EN)`。状态联动是靠 `OnUpdateUI` 注册的那个 lambda。

**练习 3**：`CSystemTray` 为什么要监听 `TaskbarCreated` 这个注册窗口消息（见 [SystemTraySDK.cpp:62-63](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/SystemTraySDK.cpp#L62-L63) 与 [SystemTraySDK.cpp:746-749](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/SystemTraySDK.cpp#L746-L749)）？

> **参考答案**：当 `explorer.exe` 崩溃重启后，整个系统托盘会被重建，原先 `Shell_NotifyIcon(NIM_ADD)` 注册的图标全部丢失。Windows 会在 explorer 重启后广播 `TaskbarCreated` 消息通知所有顶层窗口。`CSystemTray` 监听它，收到后调 `InstallIconPending()` 重新 `NIM_ADD`，让图标自动恢复——否则用户得手动重启 Weasel 才能重新看到托盘图标。

---

## 5. 综合实践

**任务**：对照 `WeaselServer.cpp` 的命令行分支与 `WeaselServerApp::SetupMenuHandlers`，制作一张「命令 / 菜单项 → 行为」对照表，并标注每条命令的「入口来源」与「派发路径」。

下面是一份参考答案表（建议你先自己填，再对照）：

| 命令 / 菜单项 | 来源 | 入口代码 | 行为 | 派发终点 |
| --- | --- | --- | --- | --- |
| `/userdir` | 命令行 | [WeaselServer.cpp:65-69](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.cpp#L65-L69) | 打开用户数据目录（资源管理器） | `explore(WeaselUserDataPath())` |
| `/weaseldir` | 命令行 | [WeaselServer.cpp:70-73](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.cpp#L70-L73) | 打开安装目录 | `explore(install_dir())` |
| `/ascii` | 命令行 | [WeaselServer.cpp:74-85](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.cpp#L74-L85) | 切英文（连旧实例发 IPC） | `SetOption(ascii_mode=true)` |
| `/nascii` | 命令行 | 同上 | 切中文（连旧实例发 IPC） | `SetOption(ascii_mode=false)` |
| `/q` `/quit` | 命令行 | [WeaselServer.cpp:88-107](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.cpp#L88-L107) | 让旧实例退出、自己不重启 | `Stop()` → `WM_QUIT` |
| `/update` | 命令行 | [WeaselServer.cpp:109-112](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.cpp#L109-L112) | 手动检查更新 | `win_sparkle_check_update_with_ui()` |
| 无参数（已有旧实例） | 命令行 | [WeaselServer.cpp:90-104](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.cpp#L90-L104) | 旧实例退场、自己接管 | 重启为新服务端 |
| 退出 (`ID_WEASELTRAY_QUIT`) | 托盘菜单 / IPC | [WeaselServerApp.cpp:50-51](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L50-L51) | 退出服务 | `m_server.Stop()` |
| 输入法设定 (`SETTINGS`) | 托盘菜单 / IPC | [WeaselServerApp.cpp:55-57](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L55-L57) | 打开 Deployer（无参） | `execute(WeaselDeployer.exe)` |
| 重新部署 (`DEPLOY`) | 托盘菜单 / IPC | [WeaselServerApp.cpp:52-54](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L52-L54) | 重新部署 | `execute(WeaselDeployer.exe /deploy)` |
| 用户词典管理 (`DICT_MANAGEMENT`) | 托盘菜单 / IPC | [WeaselServerApp.cpp:58-60](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L58-L60) | 词典管理 | `execute(WeaselDeployer.exe /dict)` |
| 用户资料同步 (`SYNC`) | 托盘菜单 / IPC | [WeaselServerApp.cpp:61-63](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L61-L63) | 同步用户词典 | `execute(WeaselDeployer.exe /sync)` |
| 帮助文档 (`WIKI`) | 托盘菜单 / IPC | [WeaselServerApp.cpp:64-65](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L64-L65) | 打开 rime.im/docs | `open(https://rime.im/docs/)` |
| 主页 (`HOMEPAGE`) | 托盘菜单 / IPC | [WeaselServerApp.cpp:66-67](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L66-L67) | 打开 rime.im | `open(https://rime.im/)` |
| 参加讨论 (`FORUM`) | 托盘菜单 / IPC | [WeaselServerApp.cpp:68-69](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L68-L69) | 打开讨论页 | `open(https://rime.im/discuss/)` |
| 检查新版本 (`CHECKUPDATE`) | 托盘菜单 / IPC | [WeaselServerApp.cpp:70](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L70) | 手动检查更新 | `check_update()` |
| 程序文件夹 (`INSTALLDIR`) | 托盘菜单 / IPC | [WeaselServerApp.cpp:71](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L71) | 打开安装目录 | `explore(install_dir())` |
| 用户文件夹 (`USERCONFIG`) | 托盘菜单 / IPC | [WeaselServerApp.cpp:72-73](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L72-L73) | 打开用户目录 | `explore(WeaselUserDataPath())` |
| 日志文件夹 (`LOGDIR`) | 托盘菜单 / IPC | [WeaselServerApp.cpp:74-75](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L74-L75) | 打开日志目录 | `explore(WeaselLogPath())` |

**进阶思考**（选做）：

- 注意 `/ascii` 与「菜单里的中英切换」其实**不走同一张表**——`/ascii` 走 IPC 后命中 `OnCommand` 里的 `ENABLE_ASCII` 特例分支（直接 `SetOption`），而菜单表 `m_MenuHandlers` 里**并没有** `ENABLE_ASCII`/`DISABLE_ASCII` 这两条（因为它们是被特例处理的）。语言栏的中英切换（u3-l4）也复用同一对 ID。请验证：能否不修改 `OnCommand` 的特例分支、改为把它们也注册进 `m_MenuHandlers`？这样做会丢失什么（提示：`lParam` 里的 `session_id`）？

- 观察到 `ID_WEASELTRAY_RERUN_SERVICE`（40015）在 [resource.h:28](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/resource.h#L28) 有定义，但在菜单 RC 与 `SetupMenuHandlers` 里都**没有**使用。这是一个保留/未启用的 ID。可在 git 历史里查它的来历。

---

## 6. 本讲小结

- **`WeaselServerApp` 是装配器**：构造函数把 `weasel::Server`（IPC）、`RimeWithWeaselHandler`（引擎）、`weasel::UI`（候选窗口）、`WeaselTrayIcon`（托盘）四大件用指针网连起来；`Run()` 按严格顺序（`Start` → WinSparkle → `m_ui.Create` → `Initialize` → 注册回调 → `tray_icon.Create` → `m_server.Run`）启动。成员声明顺序保证 `m_ui` 先于 `tray_icon` 构造。
- **消息循环是双线程**：主线程跑 WTL `CMessageLoop`（窗口消息），后台 `boost::thread` 跑 `PipeServer::Listen`（命名管道），经 `g_api_mutex` 串行化 librime 访问。退出靠 `Stop()` → `PostMessage(WM_QUIT)`，绝不强行 `ExitProcess`，以保证清理。
- **命令行让一个 EXE 多用**：`/userdir`、`/weaseldir`、`/ascii`、`/nascii` 是「轻量命令」（连旧实例发 IPC 后即退）；`/q`、`/quit`、无参数重启走「单实例 + 优雅退让」。
- **单实例靠命名互斥体**：`ServerImpl::Start` 用 `(WEASEL)Furandōru-Sukāretto-<用户名>` 互斥体保证全局唯一（按用户隔离）；新实例先用客户端身份让旧实例 `ShutdownServer`，重试最多 10×50ms。`RegisterApplicationRestart` 提供崩溃自愈。
- **托盘 = `Shell_NotifyIcon` + 状态机**：`WeaselTrayIcon::Refresh` 按 `disabled`/`ascii_mode` 在 `ZHUNG`/`ASCII`/`DISABLED` 三态间切图标，仅在状态变化时才真正改图标。引擎经 `OnUpdateUI` 回调驱动刷新。
- **本地点击与跨进程 IPC 统一**：右键菜单的 `WM_COMMAND` 与管道来的 `WEASEL_IPC_TRAY_COMMAND` 都汇入 `ServerImpl::OnCommand`，查同一张 `m_MenuHandlers` 表；只有 `ENABLE_ASCII`/`DISABLE_ASCII` 是特例（直接 `SetOption`）。
- **WinSparkle 提供自动更新**：启动时 `win_sparkle_init` 后台静默检查；`check_update()` 走带 UI 的手动检查，`appcast` URL 存在 PE 资源里，按注册表 `UpdateChannel`（稳定/测试）选 feed。

## 7. 下一步学习建议

本讲是 u6（部署、安装与系统集成）单元的收尾。接下来：

- **u7-l1（测试工程）**：如果你想知道「单实例重启」「菜单分发」这类逻辑有没有被测试覆盖，可读 `test/TestWeaselIPC`，看它如何端到端验证 IPC 通道（本讲的 `/ascii`、`ShutdownServer` 都建立在同一套 IPC 之上）。
- **u7-l2（工具函数与常量）**：本讲大量用到 `getUsername`、`WeaselUserDataPath`、`WeaselLogPath`、`get_weasel_ime_name`、`GetCustomResource`、`RegGetStringValue` 等工具函数，下一讲会系统梳理它们的定义与用法。
- **u7-l4（扩展点与架构权衡）**：本讲已经显露了几个扩展点——`AddMenuHandler`（加菜单命令）、`CustomizeMenu`（改菜单内容）、`appcast` 资源（改更新源）、`UpdateChannel`（切更新通道）。u7-l4 会把全仓库的扩展点汇总成一份「二次开发清单」。
- **建议继续阅读的源码**：若对「崩溃自愈」与「服务模式」感兴趣，可读 [WeaselServer/WeaselService.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselService.cpp)（`WeaselService` 把 `WeaselServerApp` 包装成可选的 Windows 服务，复用同一套 `app.Run()`）。它解释了为何 `WeaselServer` 既能当普通后台进程、也能被 SCM 当服务托管。
