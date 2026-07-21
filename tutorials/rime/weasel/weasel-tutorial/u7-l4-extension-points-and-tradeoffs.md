# 扩展点与架构权衡总结

## 1. 本讲目标

学完本讲，你应当能够：

- 拿到一个假想需求（例如「新增一个 IPC 命令」「新增一种候选布局」「新增一个样式配置项」），能**立刻报出**需要改动的文件清单与函数清单，并知道每一处改动落在架构图的哪一层。
- 说清 Weasel「瘦客户端 + 胖服务进程 + 命名管道 IPC」这套多进程架构在**性能、稳定性、安全、复杂度**四个维度上的得失，以及代码里哪些设施（`g_api_mutex`、`PipeChannel` 线程本地句柄、单实例互斥体、`GetPipeName()`）是为这些得失买单的。
- 对 Weasel 整体架构形成自己的设计层面评价：分层抽象做对了什么、有哪些技术债（命令枚举锚定 `WM_APP`、`DWORD` 错误信号、客户端与服务端版本强耦合等），并能提出改进思路。

本讲是整本手册的收尾，**不再引入新的源码细节**，而是把前 24 讲串成一张「改动地图」与一份「架构体检报告」。它承接 [u7-l3 配色方案与样式定制实战](u7-l3-color-scheme-and-style-customization.md)（配置端到绘制端的完整链路）与 [u6-l1 WeaselDeployer 配置器](u6-l1-weasel-deployer-configurator.md)（配置与部署的跨进程协调），是站在系统全局视角的总结。

## 2. 前置知识

本讲默认你已经读过整本手册的核心讲义，至少包括：

- **架构全貌**（[u1-l1](u1-l1-project-overview-and-architecture.md)）：WeaselTSF（DLL，瘦客户端）与 WeaselServer（EXE，胖服务进程）经命名管道通信。
- **IPC 协议**（[u2-l1](u2-l1-ipc-interface-and-command-protocol.md) 到 [u2-l5](u2-l5-response-parser-and-deserializer.md)）：`WEASEL_IPC_COMMAND` 枚举、`PipeMessage`、`RequestHandler` 抽象基类、`PipeChannel` 通道、`ResponseParser` 行协议与懒加载工厂。
- **引擎桥接**（[u4-l1](u4-l1-handler-and-engine-init.md) 到 [u4-l4](u4-l4-ui-update-notify-maintenance-theme.md)）：`RimeWithWeaselHandler` 是 `RequestHandler` 的唯一实现、`_Respond` 编码器、`_UpdateUI` 推送漏斗。
- **UI 渲染**（[u5-l1](u5-l1-weasel-panel-window-and-interaction.md) 到 [u5-l3](u5-l3-directwrite-resources-and-text-rendering.md)）：`WeaselPanel` 外壳、`Layout` 几何抽象、`DirectWriteResources` 绘制资源。
- **服务外壳**（[u6-l3](u6-l3-tray-icon-server-and-update.md)）：`WeaselServerApp` 如何把 Server/Handler/UI/托盘装配起来。

几个会被反复提及的关键术语，先对齐一下：

- **扩展点（extension point）**：代码里预留的、新增功能时**只需在固定位置插入代码、无需改动既有逻辑**的接缝。判断标准是「新增 X，要不要动到与 X 无关的既有代码」——动得越少，扩展点设计得越好。
- **分层（layering）**：Weasel 把职责切成「传输（IPC）→ 引擎（librime）→ UI」三层，层与层之间靠抽象基类（`RequestHandler`、`Layout`、`UI`）解耦。
- **瘦客户端 / 胖服务进程**：`weasel.dll` 只做抓键与上屏，所有重活（算字、画候选、托管引擎）都在全局唯一的 `WeaselServer.exe` 里。

## 3. 本讲源码地图

本讲引用的文件是「全仓库的接线点」——它们定义了协议、抽象与工厂，是所有扩展行为的汇聚处。

| 文件 | 在本讲中的角色 |
| --- | --- |
| [include/WeaselIPC.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h) | IPC 契约总账：命令枚举、`PipeMessage`、`RequestHandler` 抽象基类、`Client`/`Server` 接口、`GetPipeName()`。新增 IPC 命令的第一站。 |
| [include/RimeWithWeasel.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h) | `RequestHandler` 的唯一生产实现，引擎桥接层的扩展点（会话、配置、通知）。 |
| [include/PipeChannel.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h) | 传输层抽象：`PipeChannelBase` 连接管理与 `PipeChannel` 模板的 `Transact` 请求-响应模型。理解 IPC 性能代价的入口。 |
| [WeaselUI/Layout.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/Layout.h) | 纯几何抽象基类 `Layout`，新增布局的唯一接缝。 |
| [WeaselServer/WeaselServerApp.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp) | 装配器：把四大件串起来，`SetupMenuHandlers()` 是新增托盘菜单项的接缝。 |

辅证文件（扩展点落地处）：[WeaselIPCServer/WeaselServerImpl.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp)（命令派发 `HandlePipeMessage`）、[WeaselIPC/WeaselClientImpl.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp)（客户端命令封装 `_SendMessage`）、[WeaselIPC/Deserializer.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.cpp)（响应动作工厂注册）、[WeaselUI/WeaselPanel.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp)（布局工厂 `_CreateLayout`）、[include/WeaselIPCData.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h)（数据契约 `UIStyle`/`LayoutType`）。

---

## 4. 核心概念与源码讲解

### 4.1 功能扩展点清单：从需求到改动文件

#### 4.1.1 概念说明

所谓「扩展点」，回答的是一个问题：**我要加一个功能，到底要动几个文件、哪几个函数？** 一个架构好不好，很大程度取决于这个数字有多小、改动是否集中。

Weasel 的分层设计把功能扩展大致归到六类，每一类都有**固定的接缝**。这一节给出六类扩展的「改动清单」——这是本讲最实用的产出，也是综合实践（第 5 节）的脚手架。判断扩展点设计好坏的方法：理想情况下，新增一类事物（一个命令、一种布局、一个配置项）只需要「声明它 + 在工厂/注册表里登记它 + 实现它」三步，而不必修改任何与既有事物相关的分支代码。

> 提醒：本节列出的文件改动是**阅读源码后推断的最小必要集合**，真实落地时还会牵涉构建脚本（`weasel.sln` 把新 `.cpp` 加入工程、`xmake.lua` 加源文件列表）与资源文件（菜单、字符串表）。这些工程性改动我会标注，但以源码逻辑改动为主。

#### 4.1.2 核心流程

六类扩展及其接缝一览：

| 扩展类型 | 主要接缝（登记点） | 影响层 | 典型规模 |
| --- | --- | --- | --- |
| ① 新增 IPC 命令 | `WEASEL_IPC_COMMAND` 枚举 + `HandlePipeMessage` 派发表 | 协议/传输/引擎 | 5 文件，最重 |
| ② 新增候选布局 | `_CreateLayout()` 工厂 | UI 几何 | 3 文件，最轻 |
| ③ 新增响应动作 | `Deserializer::Initialize` 工厂表 + 服务端 `_Respond` | 协议/引擎/UI | 3 文件 |
| ④ 新增 UIStyle 配置项 | `UIStyle` 四处同步 + yaml 映射 | 数据/配置/UI | 4 处同步 |
| ⑤ 新增托盘菜单项 | `SetupMenuHandlers()` + 资源 ID | 服务外壳 | 2 文件 |
| ⑥ 方案/应用专属设置 | yaml schema + `_LoadSchemaSpecificSettings`/`AppOptionsByAppName` | 配置 | 1~2 文件 |

注意一个规律：**越是底层（协议、数据契约）的扩展，牵连的层越多；越是顶层（布局、菜单）的扩展，越局部。** 这正是分层架构的价值——把高频变化（外观、布局）挡在最外层，让它们不至于波及内核。

#### 4.1.3 源码精读

下面逐类给出接缝的真实代码位置。

**① 新增 IPC 命令——协议层的「全链路接种」**

IPC 命令是跨进程的，所以一条新命令要从客户端一路接种到服务端再到引擎，五处缺一不可。先看命令枚举（新增命令在此加一行，编号自动顺延）：

[include/WeaselIPC.h:18-36](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L18-L36) —— `WEASEL_IPC_COMMAND` 枚举，每个命令是一个 `WM_APP+N` 整数，载荷（`wParam`/`lParam`）语义随命令而变，由定长 `PipeMessage{Msg, wParam, lParam}` 承载。

命令的服务端能力由抽象基类 `RequestHandler` 的虚函数声明，引擎层（`RimeWithWeaselHandler`）实现：

[include/WeaselIPC.h:52-84](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L52-L84) —— `RequestHandler` 全部虚函数（`ProcessKeyEvent`、`SelectCandidateOnCurrentPage`、`StartMaintenance`、`SetOption`、`UpdateColorTheme` 等）。新增命令若需要引擎配合，就在这里加一个虚函数。

服务端的派发是一张 `switch` 表（宏展开），新命令要在这里登记一行：

[WeaselIPCServer/WeaselServerImpl.cpp:377-403](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L377-L403) —— `HandlePipeMessage` 用 `MAP_PIPE_MSG_HANDLE` / `PIPE_MSG_HANDLE` 宏把 `pipe_msg.Msg` 派发到对应 `OnXxx`。新增命令要加一行 `PIPE_MSG_HANDLE(WEASEL_IPC_XXX, OnXxx)`，并实现 `OnXxx`（参考 [WeaselServerImpl.cpp:215-226](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L215-L226) 的 `OnKeyEvent`，它构造 `eat` 闭包把响应回写管道后转交给 `m_pRequestHandler->ProcessKeyEvent`）。

客户端的封装则遵循统一模板「`_Active()` 守卫 → `_SendMessage` → 解释返回值」：

[WeaselIPC/WeaselClientImpl.cpp:58-65](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L58-L65) —— `ProcessKeyEvent` 是客户端命令方法的范本；新命令照抄即可，把枚举换掉。[WeaselClientImpl.cpp:193-202](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L193-L202) 的 `_SendMessage` 把请求封成 `PipeMessage` 走 `channel.Transact`，并用 `catch(DWORD)` 把管道异常退化为返回 0。

最后还要在 [include/WeaselIPC.h:102-148](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L102-L148) 的 `Client` 类（以及 `ClientImpl`）加一个对外方法，TSF 前端才能调用。引擎侧 `RimeWithWeaselHandler` 在 [include/RimeWithWeasel.h:36-64](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h#L36-L64) override 这个新虚函数。

**② 新增候选布局——最干净的扩展点**

这是全书设计得最好的扩展点：**新增布局完全不碰绘制代码**。接缝只有一个工厂函数：

[WeaselUI/WeaselPanel.cpp:110-132](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L110-L132) —— `_CreateLayout()` 按 `m_style.layout_type` `new` 出对应子类。新增布局只需在此加一个 `else if` 分支 `new MyLayout(...)`。

布局类本身继承抽象基类 `Layout`，只重写 `DoLayout` 与若干 getter：

[WeaselUI/Layout.h:65-125](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/Layout.h#L65-L125) —— `Layout` 纯虚接口（`DoLayout`、`GetCandidateRect`、`GetPreeditRect` 等），新增子类把它们落地即可，`WeaselPanel::DoPaint` 一行都不用改。

要让用户能在 yaml 里选到新布局，还要在 [include/WeaselIPCData.h:206-213](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L206-L213) 的 `LayoutType` 枚举加一个值，并让 `RimeWithWeasel.cpp` 里 yaml 字符串→枚举的映射认识它（详见 u5-l2、u7-l3）。

**③ 新增响应动作——懒加载工厂的登记**

服务端响应是行协议，客户端用「懒加载工厂 + 自描述协议」分发。新增一种响应行（例如只回传某个新字段），要登记工厂并让服务端在响应头声明它：

[WeaselIPC/Deserializer.cpp:13-28](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.cpp#L13-L28) —— `Initialize` 用 `Define(L"xxx", XxxUpdater::Create)` 登记工厂。注意代码里的注释 `// TODO: extend the parser's functionality in the future by defining more actions here`，作者明确把这里留作扩展点。

新动作的子类继承 [WeaselIPC/Deserializer.h:17-36](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.h#L17-L36) 的 `Deserializer`，实现 `Store(key, value)`，并写一个静态 `Create` 工厂。最后服务端的 `_Respond`（`RimeWithWeasel.cpp`）要在 `action=` 头里加上新动作名、在正文里输出 `key=value` 行——否则 `ActionLoader`（[WeaselIPC/ActionLoader.cpp:17-31](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ActionLoader.cpp#L17-L31)）会因「未在头声明」而静默丢弃数据。

**④ 新增 UIStyle 配置项——四处同步的纪律**

`UIStyle` 是跨进程序列化的数据契约，加一个字段必须在**四个位置**同步（u2-l4 已强调），漏一处就会出 bug：

1. [include/WeaselIPCData.h:195](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L195) 起的 `struct UIStyle` 里声明字段；
2. [include/WeaselIPCData.h:318](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L318) 起的构造函数给默认值；
3. [include/WeaselIPCData.h:365](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L365) 起的 `operator!=` 里参与比较（否则 `UI::Update` 去重会误判「没变化」而不刷新）；
4. [include/WeaselIPCData.h:430](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L430) 起的 `serialize` 模板里 `ar & s.新字段;`（追加在末尾以保证前后端兼容）。

之后还要在 `RimeWithWeasel.cpp` 加 yaml 键→字段的映射（颜色走 `_UpdateUIStyleColor` 的 `COLOR` 宏表，其它走 `_LoadSchemaSpecificSettings`），并在 `WeaselUI` 侧（`Layout`/`DirectWriteResources`/`WeaselPanel`）消费它。

**⑤ 新增托盘菜单项——一行注册**

托盘菜单是「命令表」模式，新增菜单项只需注册一个 `CommandHandler`：

[WeaselServer/WeaselServerApp.cpp:48-76](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L48-L76) —— `SetupMenuHandlers()` 用 `m_server.AddMenuHandler(ID_WEASELTRAY_XXX, handler)` 逐项登记。`AddMenuHandler` 的实现在 [WeaselIPCServer/WeaselServerImpl.h:85-87](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.h#L85-L87)，只是往 `m_MenuHandlers` map 插一条。本地点击的 `WM_COMMAND` 与跨进程的 `WEASEL_IPC_TRAY_COMMAND` 最终都汇入 [WeaselServerImpl.cpp:110-132](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L110-L132) 的 `OnCommand` 查同一张表——所以新菜单项**天然就能被前端 TSF 经 IPC 触发**，无需额外接线。别忘了在资源文件里加菜单项与对应 `ID_WEASELTRAY_XXX`。

**⑥ 方案/应用专属设置——配置驱动**

这是改动最轻的扩展：多数时候只改 yaml，不改 C++。方案专属样式写在 `<schema>.schema.yaml` 的 `style:` 段，由 `_LoadSchemaSpecificSettings` 加载；应用级选项（如某游戏默认英文）写在 `weasel.yaml` 的 `app_options:`，由 `AppOptionsByAppName` 存储、`_ReadClientInfo` 在每次建会话时套用（详见 u4-l3）。仅当你要一个**引擎尚不认识的全新选项**时，才需要改 `RimeWithWeasel.cpp` 的读取逻辑。

#### 4.1.4 代码实践

**实践目标**：把六类扩展点固化成一张个人速查表，下次动手前先查表。

**操作步骤**：

1. 新建一个 Markdown 笔记，按下表模板为每类扩展填入「文件 → 函数 → 改动要点」三列。
2. 打开本节引用的每个永久链接，确认该函数确实是你想象的样子（例如打开 `_CreateLayout` 确认它就是一堆 `if/else if`）。
3. 对六类扩展各写一句话「为什么接缝在这里」。

**需要观察的现象**：

- ① 与 ④ 牵连最多（跨进程契约不能漏）；② 与 ⑤ 牵连最少（局部接缝）。
- ⑤ 的 `OnCommand` 同时服务本地 `WM_COMMAND` 与 IPC `TRAY_COMMAND`，是「一处注册、两处可用」的典型。

**预期结果**：得到一张类似下表的速查表（综合实践会用到完整版）：

| 扩展 | 第 1 站 | 第 2 站 | 第 3 站 |
| --- | --- | --- | --- |
| IPC 命令 | `WeaselIPC.h` 枚举+虚函数 | `WeaselServerImpl.cpp` 派发+`OnXxx` | `WeaselClientImpl.cpp` 封装+`RimeWithWeaselHandler` 实现 |
| 布局 | `Layout.h` 子类 | `WeaselPanel.cpp _CreateLayout` | `WeaselIPCData.h LayoutType` |
| 响应动作 | `Deserializer` 子类 | `Deserializer.cpp Initialize` | `RimeWithWeasel.cpp _Respond` |

#### 4.1.5 小练习与答案

**练习 1**：为什么「新增布局」可以完全不动 `WeaselPanel::DoPaint`，而「新增 IPC 命令」却必须同时改客户端和服务端？

> **答案**：布局走的是**进程内多态**——`DoPaint` 只通过 `Layout` 基类的 getter 取矩形，子类在工厂 `_CreateLayout` 里被选中，绘制代码对具体子类无感知，符合「开闭原则」。而 IPC 命令走的是**跨进程协议**——客户端与服务端是两个独立编译单元、两个进程，没有任何语言层面的多态能把它们连起来，必须靠「枚举 + 派发表 + 客户端封装」三处显式接种，协议才能两端对齐。

**练习 2**：新增一个 `UIStyle` 颜色字段时，如果忘了在 `operator!=` 里加它，会出现什么现象？

> **答案**：`weasel::UI::Update` 靠 `style != ostyle` 判断是否需要刷新（u4-l4、u5-l1）。漏掉 `operator!=` 会导致「字段其实变了，但 `UI` 认为没变」而不重绘/不重发布局，表现为改了配色却看不到效果。这是「四处同步」纪律存在的根本原因。

**练习 3**：在 `Deserializer::Initialize` 里登记新工厂后，如果服务端 `_Respond` 没有在 `action=` 头里声明该动作，会发生什么？

> **答案**：该动作的工厂虽已登记，但 `ActionLoader` 只对 `action=` 头里列出的动作名调用 `Require` 实例化（u2-l5）。未声明则对应分发器不会被激活，正文里的 `key=value` 行在 `ResponseParser::Feed` 里因 `deserializers` 表查无此项而被**静默丢弃**——不报错，但数据不生效。

---

### 4.2 多进程架构权衡：性能、稳定性、安全、复杂度

#### 4.2.1 概念说明

Weasel 最核心的架构决策是：**输入法引擎不跑在你正在打字的应用进程里，而是跑在一个全局唯一的 `WeaselServer.exe` 里**。`weasel.dll` 只是被 Windows 加载进每个应用进程的瘦客户端，负责抓键与上屏；真正的算字、候选、配置全在服务进程。两者经命名管道通信。

这是一个典型的「**瘦客户端 / 胖服务进程**」架构。它和「引擎直接跑在应用进程内」（很多传统 IME 的做法）是两种截然不同的取舍。理解这一节的钥匙是：**每一个架构优点都对应一个代价，代码里那些看似古怪的设施（全局互斥锁、线程本地句柄、单实例互斥体、按用户命名的管道）都是为了支付这些代价。**

#### 4.2.2 核心流程

先看一次按键的跨进程往返，量化「性能代价」从哪里来：

```text
应用进程 (记事本.exe，加载 weasel.dll)
  │ OnKeyDown → ConvertKeyEvent → m_client.ProcessKeyEvent
  │   └─ _SendMessage → channel.Transact(req)      ← 写命名管道（内核态切换 1）
  ▼ /////////////////// 进程边界 ///////////////////
WeaselServer.exe (全局唯一)
  │ _ProcessPipeThread 收到 PipeMessage
  │   └─ g_api_mutex 上锁 → HandlePipeMessage → OnKeyEvent
  │       └─ RequestHandler::ProcessKeyEvent → rime_api->process_key
  │           └─ _Respond 把候选/状态编成文本 → eat 闭包回写管道
  │   └─ g_api_mutex 解锁                           ← 写回管道（内核态切换 2）
  ▼ /////////////////// 进程边界 ///////////////////
应用进程
  │ _ReceiveResponse 拿到 DWORD(吃键?) + GetResponseData 拿响应正文
  │   └─ ResponseParser 解析 → 更新 Context/Status → DoEditSession 上屏
```

四个维度的得失可总结成下表（\[ \] 后是源码里支付代价的设施）：

| 维度 | 得（优点） | 失（代价） | 支付代价的源码设施 |
| --- | --- | --- | --- |
| **性能** | 服务进程常驻，引擎与词典只加载一次，跨应用共享缓存 | 每次按键至少 2 次进程跨界 + 管道读写，延迟高于进程内方案 | `PipeChannel` 线程本地句柄（免去每键重连）、`Transact` 复用连接 |
| **稳定性** | 引擎/UI 崩溃不拖垮宿主应用；宿主应用崩溃不丢引擎状态；服务可崩溃自愈 | 全局单点：服务进程一挂，**所有应用**都打不了字 | `RegisterApplicationRestart`（u6-l3）、维护期按键放行（u4-l4） |
| **安全/隔离** | 按用户隔离管道，不同用户的服务互不可见；引擎状态集中可控 | 所有应用的所有按键汇入单进程，信任边界要靠管道权限把关 | `GetPipeName()` 含用户名、`SecurityAttribute` 设管道 ACL、单实例互斥体 |
| **复杂度** | 引擎与前端可独立演进、独立部署 | 协议、序列化、双会话 ID、版本耦合等大量胶水 | boost 序列化 + 行协议、`WeaselSessionId`↔`RimeSessionId` 映射 |

#### 4.2.3 源码精读

**性能代价与缓解一：每键一次管道往返。** 传输由模板 `PipeChannel` 承担，`Transact` 是请求-响应的原子单元：

[include/PipeChannel.h:120-125](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L120-L125) —— `Transact = _Ensure + _Send + _ReceiveResponse`。每次按键都走一遍这条链路。缓解措施是**线程本地句柄**：句柄存在 `boost::thread_specific_ptr` 里，同一线程反复按键只连接一次（[PipeChannel.h:59-63](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L59-L63)），`_Ensure` 命中已有连接即跳过重连。这是用空间（每线程一份句柄）换时间（免去每键重建连接）。

**性能代价与缓解二：全局串行锁。** librime 不是线程安全的，而服务端为每个管道连接派生了独立线程（`_ProcessPipeThread`）。于是用一把全局互斥锁把所有 `HandlePipeMessage` 串行化：

[WeaselIPCServer/WeaselServerImpl.cpp:165-186](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L165-L186) —— `g_api_mutex` 是文件级 `static std::mutex`，`listener` lambda 在调 `HandlePipeMessage` 前 `std::lock_guard guard(g_api_mutex)`。这意味着：**多连接并发接收，但引擎访问串行**。它用吞吐量（并发按键被排队）换来了安全性（不会两个线程同时进 librime）。对输入法这个低 QPS 场景，这笔交易非常划算。

**稳定性代价：全局单点。** 因为只有一个服务进程，它一旦崩溃，系统里所有用 Weasel 的窗口都失去输入能力。代码用两条路缓解：进程级单实例互斥体保证只有一个服务（[WeaselServerImpl.cpp:142-156](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L142-L156) 的 `Start()` 里 `CreateMutex` 探测 `ERROR_ALREADY_EXISTS`），以及崩溃后由客户端在新一次按键时按需重启服务（`Client::Connect` 的 `ServerLauncher`，u2-l3）。但本质上，**「全局单服务」是把 N 个应用的稳定性绑在一起**——这是该架构最大的风险点。

**安全/隔离代价：管道权限。** 按用户隔离的管道名是第一道防线：

[include/WeaselIPC.h:170-177](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L170-L177) —— `GetPipeName()` 拼出 `\\.\pipe\<用户名>\WeaselNamedPipe`，让不同 Windows 用户的管道互不干扰。管道创建时还带 `SecurityAttribute`（[WeaselServerImpl.cpp:31-36](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServerImpl.cpp#L31-L36) 构造函数传入 `sa.get_attr()`）设置 ACL，限制只有同用户进程能连入。这是把「按键数据集中进单进程」这个信任边界用操作系统权限兜底。

**复杂度代价：协议与双会话 ID。** 跨进程就要序列化。Weasel 用了两套：UIStyle/CandidateInfo 走 boost 文本归档，Context/Status 走逐字段行协议（u2-l4、u2-l5）。还要维护 `WeaselSessionId`（IPC 层，DWORD）与 `RimeSessionId`（librime 层）的双向映射（[include/RimeWithWeasel.h:88-96](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h#L88-L96) 的 `to_session_id`/`get_session_status`）。这些都是「不在一个进程里」的直接后果。

#### 4.2.4 代码实践

**实践目标**：在源码里亲眼定位四个维度的「支付设施」，验证它们确实对应表中的代价。

**操作步骤**：

1. **性能**：打开 [PipeChannel.h:59-63](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L59-L63)，确认 `hpipe_ptr` 与 `context` 都是 `thread_specific_ptr`。再打开 [WeaselServerImpl.cpp:165-186](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L165-L186) 找到 `g_api_mutex` 的上锁点。
2. **稳定性**：在 [WeaselServerImpl.cpp:142-156](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L142-L156) 找到单实例互斥体名（`(WEASEL)Furandōru-Sukāretto-` + 用户名）。
3. **安全**：在 [WeaselIPC.h:170-177](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L170-L177) 找到用户名如何拼进管道名。
4. **复杂度**：在 [RimeWithWeasel.h:88-96](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h#L88-L96) 找到两层会话 ID 的映射 helper。

**需要观察的现象**：每一项「代价」都能精确定位到具体行号，没有哪条代价是「凭空出现」的——架构选型与代码设施一一对应。

**预期结果**：你能指着代码向别人解释「为什么这里有把全局锁」「为什么管道名带用户名」，而不是停留在「大概是为了安全」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `g_api_mutex` 去掉、让多个 `_ProcessPipeThread` 并发进 librime，会怎样？

> **答案**：librime 内部状态（会话表、词典缓存、配置）非线程安全，并发进入会引发数据竞争，轻则候选错乱、重则崩溃。这把锁是用「串行化引擎访问」换取「线程安全」。输入法是低 QPS（人每秒按键有限）场景，串行的吞吐损失可忽略，是正确的取舍。

**练习 2**：相比「引擎跑在每个应用进程内」的单进程方案，Weasel 多进程方案在「切窗口连续打字」时反而可能更快，为什么？

> **答案**：单进程方案下，每个应用进程都要各自加载 librime、装载方案与用户词典（秒级的冷启动）；而 Weasel 的服务进程常驻，引擎与词典只加载一次，所有应用共享这份已就绪的状态。切窗口时只是新建一个轻量 IPC 会话（`AddSession`），不重新装载引擎——这是「胖服务进程」在多应用场景下的性能红利。

**练习 3**：管道名里嵌用户名（`GetPipeName`）主要防的是什么？

> **答案**：主要防**同一台机器上不同 Windows 用户**的服务互相串扰或越权连接。在多用户系统或终端服务场景下，A 用户的按键不应被 B 用户的服务处理；按用户名分管道 + ACL 让每个用户有自己独立的服务实例。它不是用来防恶意进程的（同用户进程仍可连入）。

---

### 4.3 整体设计评价：做对了什么，欠了什么债

#### 4.3.1 概念说明

评价一个架构，不要只看「能不能跑」，而要看它**面对变化时是否友好**（扩展性）、**面对故障时是否坚韧**（健壮性）、以及**留给后来者的认知负担**（可维护性）。这一节把 Weasel 放到这三把尺子下量一量，给出一份设计层面体检报告。

核心判断：Weasel 的**分层抽象**是它最大的资产——`RequestHandler`、`Layout`、`UI`、`Deserializer` 这几个抽象基类把变化点钉死在固定接缝上，让六类扩展绝大多数都能局部完成。它的主要技术债集中在**跨进程契约的脆弱性**上：命令枚举的历史包袱、`DWORD` 错误信号、客户端与服务端的版本强耦合。

#### 4.3.2 核心流程

设计评价汇总：

| 尺子 | 做对了什么（资产） | 欠了什么债（技术债） |
| --- | --- | --- |
| **扩展性** | 四大抽象基类钉死变化点；布局/菜单扩展近乎零波及 | IPC 命令是「全链路接种」，加一条要动 5 处；`UIStyle` 加字段要四处同步 |
| **健壮性** | 进程隔离让引擎/UI 故障不扩散；维护期按键安全放行；`catch(DWORD)` 兜底断线不卡键 | 全局单点服务；客户端/服务端版本强耦合，协议改了就要同时升级两端 |
| **可维护性** | 行协议可读、易调试；`eat` 闭包解耦业务与传输；抽象边界清晰 | 命令枚举锚定 `WM_APP`、`DWORD` 当错误码、4KB 缓冲硬上限等历史遗留 |

#### 4.3.3 源码精读

**资产一：`RequestHandler` 抽象——传输与引擎彻底解耦。** 整个 `WeaselIPCServer` 工程不知道 librime 的存在，它只认 `RequestHandler*`：

[include/WeaselIPC.h:52-84](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L52-L84) —— `RequestHandler` 是纯虚接口。`WeaselServerApp` 在构造时注入唯一实现（[WeaselServerApp.cpp:5-11](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L5-L11) 的 `m_handler(std::make_unique<RimeWithWeaselHandler>(&m_ui))` + `m_server.SetRequestHandler(...)`）。这意味着：理论上你可以换一个不依赖 librime 的 `RequestHandler`（测试工程 `TestWeaselIPC` 正是这么做的，u7-l1），传输层原封不动。这是教科书式的依赖倒置。

**资产二：`eat` 闭包——业务逻辑不知道管道。** 服务端的 `OnXxx` 把响应回写管道的能力封装成一个 lambda 传给 handler：

[WeaselIPCServer/WeaselServerImpl.cpp:215-226](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L215-L226) —— `auto eat = [this](std::wstring& msg){ *channel << msg; return true; };`，然后 `m_pRequestHandler->ProcessKeyEvent(KeyEvent(wParam), lParam, eat)`。`RimeWithWeaselHandler::_Respond` 只管调 `eat(text)` 把响应文本交出去，完全不知道 `channel` 是什么。传输细节（怎么写、写到哪）与业务细节（响应内容是什么）被这个闭包切开。

**资产三：`Layout` 抽象——几何与绘制正交。** 见 4.1.3 已述，`Layout` 只回答「矩形在哪」，`DoPaint` 只管「往矩形里画字」。新增布局零波及绘制，是开闭原则的优秀范例。

**技术债一：命令枚举锚定 `WM_APP`。**

[include/WeaselIPC.h:18-19](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L18-L19) —— `WEASEL_IPC_ECHO = (WM_APP + 1)`。命令编号继承自早期「用窗口消息做 IPC」的设计，现在虽已改用命名管道，编号却仍从 `WM_APP+1` 起算，纯属历史包袱。它本身无害，但提示这套协议经历过传输机制迁移。

**技术债二：`DWORD` 当错误码 + 返回 0 兜底。** 客户端把一切管道异常都压成「返回 0」：

[WeaselIPC/WeaselClientImpl.cpp:193-202](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L193-L202) —— `catch(DWORD /* ex */) { return 0; }`。这让断线时按键安全放行（不卡键），但也吞掉了所有错误信息，调试时看不到「为什么没响应」。这是用「可观测性」换「可用性」的典型妥协。

**技术债三：版本强耦合。** `weasel.dll`（客户端）与 `WeaselServer.exe`（服务端）必须**同一次构建**的产物。它们的 `PipeMessage` 布局、`WEASEL_IPC_COMMAND` 编号、`UIStyle` 序列化字段顺序必须完全一致，否则协议错位。代码里没有版本协商（`RIME_STRUCT`/`RIME_API_AVAILABLE` 是与 librime 的 ABI 协商，不是 IPC 自身的）。`WEASEL_IPC_LAST_COMMAND`（[WeaselIPC.h:35](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L35)）只是枚举哨兵，不做运行期校验。`WeaselSetup` 安装时停服务、升级、再启服务（u6-l2）就是为了规避这个耦合——保证两端同时更新。

**技术债四：4KB 硬上限。**

[include/WeaselIPC.h:12-16](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L12-L16) —— `WEASEL_IPC_BUFFER_SIZE = 4*1024`。单次响应正文不能超过 4KB（2048 宽字符），超长候选页或超大 `UIStyle` 会被截断。这是定长缓冲换简单性的代价。

#### 4.3.4 代码实践

**实践目标**：形成你自己的改进思路清单。

**操作步骤**：

1. 对本节列出的每一条技术债，写下一句「如果要改，最小改动是什么、风险是什么」。
2. 对照 4.1 的扩展点表，判断这些技术债**目前是否阻碍了扩展**（阻碍的优先还，不阻碍的可暂缓）。

**需要观察的现象**：

- 「命令枚举锚定 `WM_APP`」几乎不阻碍扩展（加命令照常工作），优先级低。
- 「版本强耦合」是真实运维痛点（升级要停服务），但改造成本高（要设计协议版本握手），是中长期议题。
- 「`DWORD` 吞错误」影响调试体验，可在 `catch` 块加一行日志低成本缓解。

**预期结果**：你得到一份按「阻碍程度 / 改造成本」二维分级的改进清单，而不是「哪里都不好」的泛泛批评。待本地验证：实际升级一次 Weasel，观察是否真要停服务。

#### 4.3.5 小练习与答案

**练习 1**：用一句话概括 Weasel 架构最大的优点和最大的风险。

> **答案**：最大的优点是**分层抽象把变化点钉死在接缝上**（`RequestHandler`/`Layout`/`UI`/`Deserializer`），让多数扩展局部可完成；最大的风险是**全局单服务进程**——它是所有应用输入能力的单点，一旦挂掉全员失语，且与客户端版本强耦合。

**练习 2**：为什么 `RequestHandler` 被设计成抽象基类，而不是让 `WeaselServerImpl` 直接调 librime？

> **答案**：为了让**传输层（`WeaselIPCServer`）不依赖引擎层（librime）**。这样传输层可以被任何 `RequestHandler` 实现复用（如测试用的 `TestRequestHandler`），引擎层也可以独立演进。如果 `WeaselServerImpl` 直接调 librime，传输与引擎就耦合死了，换引擎或脱离引擎测试都要重写传输——这是依赖倒置原则的应用。

**练习 3**：如果让你给 Weasel 的 IPC 协议加一个「版本协商」机制，最小可行方案是什么？

> **答案**：最小方案：给 `PipeMessage` 增加一个版本字段（或在 `WEASEL_IPC_ECHO` 的载荷里带上客户端协议版本），服务端 `OnEcho` 比对双方版本，不匹配则拒绝建会话并提示「客户端/服务端版本不一致，请重启」。这能把目前的「静默协议错位」变成「显式报错」，成本远低于重做协议。实际落地需评估对定长 `PipeMessage` 布局的影响——待确认。

---

## 5. 综合实践

**任务**：选择一个假想需求——「**新增一个 IPC 命令 `WEASEL_IPC_TOGGLE_LOGGING`，让前端能远程开关服务端的 librime 日志**」——列出从协议、客户端、服务端、Handler 到（可选）UI 需要改动的全部文件与函数清单，并给出改动顺序。这是 4.1 节扩展点①的完整演练。

### 5.1 需求拆解

- **目的**：调试时从前端（或托盘）一键开关 librime 的详细日志，免去手动改配置重启服务。
- **语义**：命令带一个 `wParam`（0=关、1=开），无需会话上下文（类似 `WEASEL_IPC_START_MAINTENANCE`），返回值表示切换后的状态。
- **不需要**：响应正文（不必走 `_Respond`/`eat`），返回 `DWORD` 即可。

### 5.2 改动清单（按依赖顺序）

| 步骤 | 文件 | 函数/位置 | 改动要点 |
| --- | --- | --- | --- |
| 1 | `include/WeaselIPC.h` | `WEASEL_IPC_COMMAND` 枚举（[L18-36](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L18-L36)） | 在 `WEASEL_IPC_LAST_COMMAND` 前加 `WEASEL_IPC_TOGGLE_LOGGING`。 |
| 2 | `include/WeaselIPC.h` | `RequestHandler`（[L52-84](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L52-L84)） | 加虚函数 `virtual DWORD ToggleLogging(BOOL on) { return 0; }`（默认空实现，保持向后兼容）。 |
| 3 | `WeaselIPCServer/WeaselServerImpl.h` | `ServerImpl`（[L50-72](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.h#L50-L72) 区段） | 声明 `DWORD OnToggleLogging(WEASEL_IPC_COMMAND, DWORD, DWORD);`。 |
| 4 | `WeaselIPCServer/WeaselServerImpl.cpp` | `HandlePipeMessage`（[L381-400](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L381-L400)） | 加一行 `PIPE_MSG_HANDLE(WEASEL_IPC_TOGGLE_LOGGING, OnToggleLogging);`。 |
| 5 | `WeaselIPCServer/WeaselServerImpl.cpp` | 新增 `OnToggleLogging` | 仿 `OnStartMaintenance`（[L295-301](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L295-L301)）：判空 `m_pRequestHandler` 后 `return m_pRequestHandler->ToggleLogging(wParam);`。 |
| 6 | `include/RimeWithWeasel.h` | `RimeWithWeaselHandler`（[L36-64](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h#L36-L64)） | 声明 override `DWORD ToggleLogging(BOOL on);`。 |
| 7 | `RimeWithWeasel/RimeWithWeasel.cpp` | 新增 `ToggleLogging` | 调 `rime_api->set_option(...)` 或 librime 的日志开关 API（**待确认**：librime 是否暴露运行期日志开关，若无须先加配置重启方案）。 |
| 8 | `include/WeaselIPC.h` | `Client`（[L102-148](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L102-L148)） | 加 `BOOL ToggleLogging(bool on);`。 |
| 9 | `WeaselIPC/WeaselClientImpl.h` / `.cpp` | `ClientImpl` | 加方法：仿 `ShutdownServer`（[WeaselClientImpl.cpp:54-56](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L54-L56)），`return _SendMessage(WEASEL_IPC_TOGGLE_LOGGING, on?1:0, 0) != 0;`（注意：本命令无会话依赖，不必 `_Active()` 守卫）。 |
| 10（可选） | `WeaselServer/WeaselServerApp.cpp` | `SetupMenuHandlers`（[L48-76](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L48-L76)） + 资源 `ID_WEASELTRAY_TOGGLE_LOGGING` | 加 `AddMenuHandler(ID_WEASELTRAY_TOGGLE_LOGGING, [this]{...发 IPC 命令...});`，让托盘也能触发。 |

### 5.3 验证思路

1. **编译**：把新 `.cpp` 加入 `weasel.sln` 与 `xmake.lua` 的源文件列表，两端（`weasel.dll` 与 `WeaselServer.exe`）必须**同次构建**——否则步骤 1 的枚举编号不一致，协议直接错位（呼应 4.3 的「版本强耦合」债）。
2. **冒烟**：用 `test/TestWeaselIPC`（u7-l1）的 `TestRequestHandler` 替换 librime，给 `ToggleLogging` 打桩，验证客户端发命令→服务端派发→handler 被调→返回值正确回传的完整往返。
3. **真机**：构建安装后，从前端或托盘触发，观察 `%TEMP%\rime.weasel`（`WeaselLogPath`，u7-l2）日志输出的开关切换。

### 5.4 反思点

- 这个需求暴露了「全链路接种」的成本：一个看似简单的开关，牵动 8~10 处改动。
- 若日志开关其实**不需要前端触发**（只是本地调试），更划算的做法是走扩展点⑤（托盘菜单 + 本地命令），省掉 IPC 这一整圈——**先判断需求是否真要跨进程，再决定动用哪类扩展点**，这是本讲最重要的元教训。

## 6. 本讲小结

- **六类扩展点**各有固定接缝：IPC 命令（枚举+派发+客户端+Handler，5 处）、布局（`_CreateLayout` 工厂，最轻）、响应动作（`Deserializer::Initialize` 工厂表）、UIStyle 配置项（四处同步）、托盘菜单（`SetupMenuHandlers`）、方案/应用设置（yaml 驱动）。**越底层的扩展牵连越多层，越顶层的扩展越局部。**
- **判断扩展点好坏**的标准是「开闭原则」：新增一类事物应只做「声明 + 登记 + 实现」，不动既有分支。Weasel 的 `Layout` 与 `RequestHandler` 是优秀范例，IPC 命令的「全链路接种」是反面（受跨进程所限）。
- **多进程架构**用「每键一次管道往返 + 全局串行锁」的代价，换来了「引擎一次加载多应用共享」「故障不扩散」「按用户隔离」的红利；`PipeChannel` 线程本地句柄、`g_api_mutex`、`GetPipeName()`、单实例互斥体都是为支付这些代价而存在的设施。
- **最大资产**是分层抽象（传输/引擎/UI 经 `RequestHandler`/`Layout`/`UI`/`Deserializer` 解耦）与 `eat` 闭包式解耦；**最大风险**是全局单服务进程的单点故障与客户端/服务端版本强耦合。
- **主要技术债**：命令枚举锚定 `WM_APP`、`DWORD` 当错误码且 `catch` 吞错、4KB 响应硬上限、IPC 协议无版本协商。改进应按「阻碍扩展/故障的程度」与「改造成本」二维排序，优先还阻碍性高、成本低的债（如给 `catch` 加日志）。
- **动手前的元原则**：先判断需求是否真要跨进程/真要新协议，再决定动用哪类扩展点——能走 yaml/托盘/布局等局部接缝的，绝不轻易动 IPC 契约。

## 7. 下一步学习建议

本讲是整本手册的终点，没有「下一讲」。建议你从以下三个方向继续深化：

- **动手做一个真扩展**：挑 4.1 表里的一类（推荐「新增布局」或「新增托盘菜单项」，成本最低），照着综合实践的清单实际改一遍、编译通过。只有亲手接种过一次，扩展点表才真正变成你的肌肉记忆。
- **深入 librime 引擎本身**：Weasel 只是前端，真正的输入法逻辑（方案、词典、翻译算法）在 librime。`RimeWithWeaselHandler` 是唯一的入口——从 [include/RimeWithWeasel.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h) 里它调用的 `rime_api->*` 出发，去 [rime](https://github.com/rime/librime) 仓库阅读 `rime_api.h` 与方案编写文档（[rime.im/docs](https://rime.im/docs/)）。
- **横向对比其它 Rime 前端**：把本讲的架构权衡对照 macOS 鼠须管（Squirrel）、Linux 的 ibus-rime/fcitx5-rime——它们同样基于 librime，却各自做了不同的进程/IPC 选型。对比「同样的引擎、不同的前端如何取舍」是巩固本讲架构评价视角的最佳练习。

回看路线：若哪一类扩展点的源码细节记不清了，按「IPC→TSF→Rime→UI→Deploy」的单元顺序回查 [u2](u2-l1-ipc-interface-and-command-protocol.md) 到 [u6](u6-l1-weasel-deployer-configurator.md) 即可。整本手册至此闭合。
