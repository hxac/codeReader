# RimeWithWeaselHandler 与引擎初始化

## 1. 本讲目标

本讲要回答一个核心问题：**WeaselServer 进程是怎么把按键「喂」给 librime 引擎的？**

更具体地说，本讲聚焦于 `RimeWithWeaselHandler` 这个类——它是 IPC 层抽象基类 `weasel::RequestHandler` 的唯一实现，也是整个 Weasel 项目里**唯一直接调用 librime C API 的地方**。所有从 WeaselTSF 经命名管道送达 Server 的命令，最终都由它翻译成对 librime 的调用。

学完本讲你应当能够：

- 说清 `rime_api`（librime 的 C 接口表）是什么、Weasel 是怎么拿到它的，以及 `RimeTraits` 各字段的作用。
- 画出「构造函数 → `_Setup()` → `Initialize()`」三段式初始化的执行顺序，并在每一步标注调用了哪个 `rime_api` 函数。
- 解释「维护模式（maintenance）」与「部署器互斥锁（WeaselDeployerMutex）」如何避免两个进程同时改写用户词典。
- 理解 `OnNotify` 回调为什么用静态缓冲 + 互斥锁，以及它在跨线程（部署线程）场景下的线程安全设计。

本讲**不**展开按键处理主链路（`ProcessKeyEvent` / `_Respond`）和会话映射细节，那是 u4-l2 的内容；也不展开配色与 UI 样式加载，那是 u4-l3/u4-l4 的内容。本讲只把「引擎怎么被拉起来、回调怎么接回来」这块地基打牢。

## 2. 前置知识

阅读本讲前，你需要先建立以下直觉（对应 u1-l1 与 u2-l1）：

- **引擎与前端分离**：librime 是跨平台的 Rime 输入法引擎（C++ 写成，对外暴露 C 接口），Weasel 只是它在 Windows 上的前端之一。macOS 的鼠须管、Linux 的 ibus-rime / fcitx5-rime 都共用同一个 librime，只是前端不同。
- **多进程 + IPC**：WeaselServer（EXE，全局唯一）托管 librime；WeaselTSF（DLL，驻留每个应用进程）只抓键与上屏。两者经命名管道通信。`RimeWithWeaselHandler` 运行在 **Server 进程内**。
- **RequestHandler 抽象**：IPC 层定义了抽象基类 `weasel::RequestHandler`（[include/WeaselIPC.h:52-84](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L52-L84)），用一组虚函数描述「服务端能做什么」（`AddSession` / `ProcessKeyEvent` / `CommitComposition` …）。IPC 传输层只认这个抽象基类，**不认识 librime**。`RimeWithWeaselHandler` 继承它，把每个虚函数实现成对 librime 的调用，从而把「传输」与「引擎」两层彻底解耦。

另外，有几个 C/Win32 概念会用到，先一句话解释：

- **函数指针表当接口**：librime 对外的 C API 不是一堆自由函数，而是一个装满函数指针的结构体 `RimeApi`。拿到这个结构体的指针，就能通过 `api->方法名(...)` 调用引擎。
- **互斥锁（mutex）**：一种保证「同一时刻只有一个线程进入临界区」的同步原语。C++ 标准库的 `std::lock_guard<std::mutex>` 在构造时加锁、析构时自动解锁（RAII）。
- **命名互斥体（named mutex）**：Windows 的 `CreateMutex` 可以创建一个**跨进程**可见的互斥体，靠名字实现「全系统只能有一个实例持有」。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [include/RimeWithWeasel.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h) | `RimeWithWeaselHandler` 类声明、`SessionStatus` 结构、`OnNotify` 与静态消息缓冲的声明。 |
| [RimeWithWeasel/RimeWithWeasel.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp) | 本讲主角：构造、`_Setup`、`Initialize`、`Finalize`、`_IsDeployerRunning`、`OnNotify` 全部在此实现。 |
| [include/WeaselIPC.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h) | 定义被继承的抽象基类 `weasel::RequestHandler`。 |
| [WeaselServer/WeaselServerApp.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp) | 把 handler 组装进 Server、并调用 `Initialize()` 的地方。 |
| [include/WeaselUtility.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUtility.h) | `WeaselSharedDataPath` / `WeaselUserDataPath` / `WeaselLogPath` / `IsUserDarkMode` 等路径与系统工具。 |
| [include/WeaselConstants.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselConstants.h) | `WEASEL_CODE_NAME`、`WEASEL_VERSION` 等注入常量。 |

> 关于 `rime_api`：`RimeApi` 结构体与 `rime_get_api()`、`RIME_STRUCT`、`RIME_API_AVAILABLE` 等宏都定义在 librime 的头文件 `rime_api.h` 中。本仓库以 git 子模块形式引用 `librime`（见 `.gitmodules`），`RimeWithWeasel.h` 通过 `#include <rime_api.h>` 引入它。本讲**不编造该头文件的链接**，而是依据它在 `RimeWithWeasel.cpp` 中的真实用法来讲解——凡引用都落在 weasel 仓库内确实存在的代码行上。

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：

1. **4.1 librime C API 与 traits**：Weasel 怎么拿到 `RimeApi`、`RimeTraits` 描述了什么。
2. **4.2 Initialize 与维护模式**：三段式初始化、部署器互斥锁、维护线程的串行化。
3. **4.3 OnNotify 通知与互斥锁**：librime 怎么把消息「推」回 Weasel，静态缓冲为何线程安全。

### 4.1 librime C API 与 traits

#### 4.1.1 概念说明

librime 是用 C++ 写的引擎，但为了被各平台前端复用，它对外暴露的是一套 **C ABI**。这套 ABI 的入口不是一堆散落的函数，而是一个叫 `RimeApi` 的结构体——它本质上是一张**函数指针表**，引擎的每一个能力（建会话、处理按键、读配置、部署……）都是表里的一个函数指针字段。

这种设计有两个直接好处：

- **二进制兼容**：C ABI 没有名字改编（name mangling），任何能调用 C 函数的语言/编译器都能用 librime。
- **版本协商**：`RimeApi` 结构体里还带有版本信息和「字段是否存在」的判定能力，前端可以用较新的头文件编译、却安全地运行在较旧的 librime 上（下面 `RIME_API_AVAILABLE` 就是干这个的）。

Weasel 拿到这张表的唯一入口是函数 `rime_get_api()`，它返回一个 `RimeApi*`。在 `RimeWithWeasel.cpp` 里，这个指针被存进一个**文件级静态变量**：

```cpp
static RimeApi* rime_api;   // 全文件共享的唯一引擎句柄
```

定义见 [RimeWithWeasel/RimeWithWeasel.cpp:26](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L26)。此后本文件里所有的 `rime_api->xxx(...)` 调用，都是在通过这张表驱动引擎。

除了「拿句柄」，初始化还要告诉引擎「我是谁、我的数据放在哪」。这份自我介绍由 `RimeTraits` 结构体承载，主要字段包括：

| 字段 | 含义 | 在 Weasel 中的取值 |
| --- | --- | --- |
| `shared_data_dir` | 共享数据目录（预置方案、二进制词典） | `WeaselSharedDataPath()` |
| `user_data_dir` | 用户数据目录（用户方案、用户词典） | `WeaselUserDataPath()` |
| `prebuilt_data_dir` | 预编译产物目录 | 与 `shared_data_dir` 相同 |
| `distribution_name` | 发行版名称 | `get_weasel_ime_name()`（中文环境为「小狼毫」） |
| `distribution_code_name` | 发行版代号 | `WEASEL_CODE_NAME`（`"Weasel"`） |
| `distribution_version` | 发行版版本号 | `WEASEL_VERSION` |
| `app_name` | 应用标识 | `"rime.weasel"` |
| `log_dir` | 日志目录 | `WeaselLogPath()`（`%TEMP%\rime.weasel`） |

路径工具的定义在 [include/WeaselUtility.h:34-46](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUtility.h#L34-L46)，版本常量在 [include/WeaselConstants.h:3-9](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselConstants.h#L3-L9)。

> 注意一个**生命周期陷阱**：`_Setup()` 把 `weasel_traits` 的各 `*_dir` 指针指向了**局部变量** `shared_dir` / `user_dir` / `log_dir` 的 `.c_str()`。这之所以安全，是因为 `_Setup()` 把 trait 立刻交给 `rime_api->setup()` 使用、不会跨调用持有；但这也意味着不能在 `_Setup()` 返回后继续使用这些指针。

#### 4.1.2 核心流程

获取引擎句柄并完成「自我介绍」的流程：

```text
进程启动
  └─ rime_get_api()           → 拿到 RimeApi*（唯一入口）
        └─ 校验非空（assert）
              └─ _Setup()
                    ├─ RIME_STRUCT(RimeTraits, ...)   初始化带版本号的结构体
                    ├─ 填写 shared/user/prebuilt 目录、发行版信息、日志目录
                    ├─ rime_api->setup(&traits)        把 traits 交给引擎（必须在 initialize 之前）
                    └─ rime_api->set_notification_handler(OnNotify, this)
                                                                注册回调（在 initialize 之前注册，才能收到部署期通知）
```

关于 `RIME_STRUCT`：librime 的对外结构体都带一个 `data_size`（或类似）头部字段，用于 ABI 版本判定。宏 `RIME_STRUCT(Type, var)` 的作用是**零初始化结构体并把头部尺寸字段设为 `sizeof(Type)`**，相当于告诉引擎「我是按这个版本的布局编译的」。因此每处需要 librime 结构体（`RimeTraits` / `RimeStatus` / `RimeContext` / `RimeCommit`）的地方都先写一句 `RIME_STRUCT(Type, var);`，这是该 API 的固定用法。

#### 4.1.3 源码精读

句柄获取在构造函数里，见 [RimeWithWeasel/RimeWithWeasel.cpp:46-48](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L46-L48)：

```cpp
rime_api = rime_get_api();
assert(rime_api);
m_pid = GetCurrentProcessId();
```

`_Setup()` 填写 traits 并交给引擎，见 [RimeWithWeasel/RimeWithWeasel.cpp:89-105](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L89-L105)：

```cpp
void RimeWithWeaselHandler::_Setup() {
  RIME_STRUCT(RimeTraits, weasel_traits);
  std::string shared_dir = wtou8(WeaselSharedDataPath().wstring());
  std::string user_dir = wtou8(WeaselUserDataPath().wstring());
  weasel_traits.shared_data_dir = shared_dir.c_str();
  weasel_traits.user_data_dir = user_dir.c_str();
  weasel_traits.prebuilt_data_dir = weasel_traits.shared_data_dir;
  std::string distribution_name = wtou8(get_weasel_ime_name());
  weasel_traits.distribution_name = distribution_name.c_str();
  weasel_traits.distribution_code_name = WEASEL_CODE_NAME;
  weasel_traits.distribution_version = WEASEL_VERSION;
  weasel_traits.app_name = "rime.weasel";
  std::string log_dir = WeaselLogPath().u8string();
  weasel_traits.log_dir = log_dir.c_str();
  rime_api->setup(&weasel_traits);
  rime_api->set_notification_handler(&RimeWithWeaselHandler::OnNotify, this);
}
```

要点逐句：

- `wtou8(...)` 把宽字符路径转成 UTF-8（librime 用 UTF-8 处理字符串），宏定义在 [include/WeaselUtility.h:243-247](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUtility.h#L243-L247)。
- `prebuilt_data_dir` 直接复用 `shared_data_dir` 指针，表示预编译产物就在共享目录里。
- `setup()` 必须在 `initialize()` **之前**调用，它让引擎记住「这次会话的数据目录与发行版身份」。
- `set_notification_handler()` 把 `OnNotify`（4.3 节详述）挂上去，第二个参数 `this` 是回传给回调的 `context_object`——librime 不懂 C++ 对象，只把它当成 `void*` 原样回传。

#### 4.1.4 代码实践

**实践目标**：建立「`RimeApi` 方法名 → 引擎能力 → 调用位置」的全局索引，便于后续阅读。

**操作步骤**：

1. 打开 `RimeWithWeasel/RimeWithWeasel.cpp`。
2. 全文搜索 `rime_api->`，把每一个调用点摘录下来。
3. 按生命周期阶段给它们分组，例如：
   - **初始化/终结**：`rime_get_api`、`setup`、`set_notification_handler`、`initialize`、`start_maintenance`、`join_maintenance_thread`、`finalize`。
   - **配置**：`config_open`、`config_close`、`config_get_string`、`config_get_bool`、`config_get_int`、`config_begin_map`、`config_next`、`config_end`、`schema_open`。
   - **会话**：`create_session`、`destroy_session`、`find_session`。
   - **按键与结果**：`process_key`、`get_commit`、`get_status`、`get_context`、`free_commit`、`free_status`、`free_context`、`select_candidate_on_current_page`、`highlight_candidate_on_current_page`、`change_page`、`commit_composition`、`clear_composition`。
   - **选项/属性**：`set_option`、`get_option`、`set_property`、`get_property`、`get_state_label`。
4. 用编辑器的「转到定义」或全局搜索确认：除 `RimeWithWeasel.cpp` 外，weasel 仓库里**没有别的文件**直接调用 `rime_api->`（这正是「引擎调用收口于一处」的体现）。

**需要观察的现象**：你会看到 `rime_api->` 在这一个文件里出现 50 次以上，但全仓库其它子工程（WeaselTSF、WeaselUI、WeaselIPC……）零调用。

**预期结果**：得到一张「能力 → 方法 → 行号」的对照表。后续阅读任意虚函数实现时，都能在这张表里快速定位它调了引擎的哪个能力。

> 本实践为源码阅读型，**待本地验证**（行号可能随版本变化，请以你 checkout 的 HEAD 为准）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Weasel 把 `rime_api` 设成**文件级静态变量**而不是类的成员？

**参考答案**：因为 `RimeApi*` 在整个进程内是唯一的、不可变的（`rime_get_api()` 每次返回同一张表），与 handler 实例无关；用文件静态变量既省去每次 `this->` 访问，也向读者宣告「这是全局唯一的引擎句柄」。代价是它不在线程隔离的意义上安全——真正的并发安全由 4.2 的维护串行与 u2-l3 提到的 `g_api_mutex` 提供。

**练习 2**：`_Setup()` 里若把 `rime_api->setup(&weasel_traits)` 与 `rime_api->set_notification_handler(...)` 的调用顺序对调，会出什么问题？

**参考答案**：功能上大概率仍可用，因为两者都发生在 `initialize()` 之前。但语义上 `setup()` 是「配置引擎身份」的入口，`set_notification_handler()` 是「挂回调」；librime 的约定是先 `setup` 再挂回调。保持现有顺序可避免在极少数 librime 版本上回调注册被 `setup` 重置的边界情况。

---

### 4.2 Initialize 与维护模式

#### 4.2.1 概念说明

librime 的初始化分两步：`setup()`（登记身份，4.1 已讲）和 `initialize()`（真正构造引擎实例、加载方案与词典）。Weasel 把这两步刻意**分开到不同函数、不同时机**调用：

- `_Setup()` 在**构造函数**里就调用（进程一启动、handler 一创建就跑）。
- `Initialize()` 在 **Server 启动消息循环之前**才被 `WeaselServerApp::Run()` 显式调用。

为什么要拆开？因为 `initialize()` 会触发**维护（maintenance）**：引擎检查共享/用户目录里的方案与词典是否需要重新编译。这是一次可能很重的磁盘 I/O，而且**必须在「没有别的进程在改用户词典」的前提下进行**。把这一步挪到构造之后、消息循环之前，既让窗口和托盘有机会先就位，也便于在「部署器正在跑」时优雅地退化为禁用状态。

这里出现两个关键概念：

- **维护模式（maintenance）**：librime 用 `start_maintenance()` 启动一个后台线程重新构建二进制词典/预编译方案。Weasel 在此期间把自身标记为 `m_disabled = true`，使所有按键命令直接放行（不进引擎），避免在数据未就绪时误用。
- **部署器互斥锁（WeaselDeployerMutex）**：当用户从托盘点【重新部署】时，会启动另一个独立进程 `WeaselDeployer.exe`（u6-l1）。它和 WeaselServer 都会改写用户数据目录，必须互斥。Weasel 用一个**命名互斥体**探测部署器是否在跑：若在跑，Server 就**不**调用 `initialize()`，直接保持禁用。

#### 4.2.2 核心流程

`Initialize()` 的判定与加载流程：

```text
Initialize()
  ├─ m_disabled = _IsDeployerRunning()        探测 WeaselDeployerMutex
  │     └─ 若部署器在跑 → 直接 return（保持禁用，等部署器结束后再来调 EndMaintenance）
  ├─ rime_api->initialize(NULL)               构造引擎
  ├─ rime_api->start_maintenance(False)       需要重建？
  │     └─ 是 → m_disabled=true; join_maintenance_thread()  阻塞等维护线程跑完
  ├─ config_open("weasel", &config)           打开 weasel.yaml
  │     ├─ _UpdateUIStyle(...)                读 style/* 外观
  │     ├─ _UpdateShowNotifications(...)      读提示开关
  │     ├─ 深色模式 → 额外加载 color_scheme_dark
  │     ├─ 读 global_ascii / show_notifications_time
  │     └─ _LoadAppOptions(...)               读 app_options（应用级选项）
  └─ m_last_schema_id.clear()
```

部署器互斥锁的探测逻辑（跨进程）：

```text
_IsDeployerRunning()
  ├─ hMutex = CreateMutex(NULL, TRUE, L"WeaselDeployerMutex")
  ├─ deployer_detected = (hMutex && GetLastError()==ERROR_ALREADY_EXISTS)
  └─ CloseHandle(hMutex)   ← 注意：立刻关闭，仅作“探测”，不长期持有
```

关键点：`CreateMutex` 以 `WeaselDeployerMutex` 为名，**跨进程**可见。如果这个互斥体已被另一个进程（部署器）创建，`GetLastError()` 就会返回 `ERROR_ALREADY_EXISTS`——这是一次「只读探测」，所以紧接着 `CloseHandle` 释放掉，并不长期占用。

> 关于维护串行的并发语义：`start_maintenance` 会起**后台线程**，而 Weasel 用 `join_maintenance_thread()` **阻塞等待它结束**，意味着 `Initialize()` 返回时数据一定已就绪。注意 u2-l3 提到 IPC 服务端另有 `g_api_mutex` 把所有 librime 调用串行化——两者并不冲突：维护线程在 `join` 之前独占引擎，`join` 之后才允许后续 IPC 触发的按键调用进入。

#### 4.2.3 源码精读

`Initialize()` 主体见 [RimeWithWeasel/RimeWithWeasel.cpp:107-147](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L107-L147)：

```cpp
void RimeWithWeaselHandler::Initialize() {
  m_disabled = _IsDeployerRunning();
  if (m_disabled) {
    return;                 // 部署器在跑，暂不 initialize
  }

  LOG(INFO) << "Initializing la rime.";
  rime_api->initialize(NULL);
  if (rime_api->start_maintenance(/*full_check = */ False)) {
    m_disabled = true;
    rime_api->join_maintenance_thread();
  }

  RimeConfig config = {NULL};
  if (rime_api->config_open("weasel", &config)) {
    if (m_ui) {
      _UpdateUIStyle(&config, m_ui, true);
      _UpdateShowNotifications(&config, true);
      m_current_dark_mode = IsUserDarkMode();
      if (m_current_dark_mode) {
        // ... 读 style/color_scheme_dark 并应用 ...
      }
      m_base_style = m_ui->style();
    }
    Bool global_ascii = false;
    if (rime_api->config_get_bool(&config, "global_ascii", &global_ascii))
      m_global_ascii_mode = !!global_ascii;
    if (!rime_api->config_get_int(&config, "show_notifications_time",
                                  &m_show_notifications_time))
      m_show_notifications_time = 1200;
    _LoadAppOptions(&config, m_app_options);
    rime_api->config_close(&config);
  }
  m_last_schema_id.clear();
}
```

部署器探测见 [RimeWithWeasel/RimeWithWeasel.cpp:509-516](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L509-L516)：

```cpp
bool RimeWithWeaselHandler::_IsDeployerRunning() {
  HANDLE hMutex = CreateMutex(NULL, TRUE, L"WeaselDeployerMutex");
  bool deployer_detected = hMutex && GetLastError() == ERROR_ALREADY_EXISTS;
  if (hMutex) {
    CloseHandle(hMutex);
  }
  return deployer_detected;
}
```

`Initialize()` 由谁调用？见 [WeaselServer/WeaselServerApp.cpp:30-33](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L30-L33)——在 UI 创建之后、Server 消息循环 `Run()` 之前：

```cpp
m_ui.Create(m_server.GetHWnd());
m_handler->Initialize();
m_handler->OnUpdateUI([this]() { tray_icon.Refresh(); });
```

收尾的 `Finalize()` 见 [RimeWithWeasel/RimeWithWeasel.cpp:149-155](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L149-L155)，在 Server 退出消息循环后被调用，调用 `rime_api->finalize()` 销毁引擎：

```cpp
void RimeWithWeaselHandler::Finalize() {
  m_active_session = 0;
  m_disabled = true;
  m_session_status_map.clear();
  LOG(INFO) << "Finalizing la rime.";
  rime_api->finalize();
}
```

维护模式还提供了一对「手动进出」的接口，见 [RimeWithWeasel/RimeWithWeasel.cpp:475-487](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L475-L487)：`StartMaintenance()` 调 `Finalize()` 再刷新 UI；`EndMaintenance()` 在仍处于禁用时调 `Initialize()` 把引擎重新拉起来。它们对应 IPC 命令 `WEASEL_IPC_START_MAINTENANCE` / `WEASEL_IPC_END_MAINTENANCE`（u2-l1）。

#### 4.2.4 代码实践

**实践目标**：把三段式初始化串成一张带「rime_api 调用」标注的时序，并验证部署器互斥行为。

**操作步骤**：

1. 在 `RimeWithWeasel.cpp:37`（构造函数）打条件断点或阅读，记录初始化列表里 `m_disabled(true)` 的初值——注意构造完成时 handler **是禁用的**。
2. 顺着构造函数体读到 `_Setup()` 调用（[L57](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L57)），确认此处调用了 `rime_get_api` / `setup` / `set_notification_handler`，但**没有** `initialize`。
3. 跳到 `WeaselServerApp::Run()`（[WeaselServerApp.cpp:32](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L32)），确认 `Initialize()` 在此处才被调用，进而执行 `initialize` / `start_maintenance` / `config_open`。
4. 验证部署器互斥（思路，**待本地验证**）：在 Windows 上，先手动启动 `WeaselDeployer.exe /deploy`，再启动 `WeaselServer.exe`；用调试器附加到 Server，观察 `Initialize()` 进入时 `_IsDeployerRunning()` 返回 `true`，函数立即 `return`，`m_disabled` 保持 `true`。等部署器退出后，下一次有 IPC 命令触发 `EndMaintenance()`（见 [AddSession](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L166-L172) 中的恢复逻辑）才会真正 `initialize`。

**需要观察的现象**：

- 构造完成 → handler 处于禁用态；`Initialize()` 跑完且无维护 → `m_disabled` 变 `false`。
- 维护触发 → `m_disabled` 在 `join` 期间为 `true`，`join` 结束后仍未被置回 `false`（要等后续按键经 `AddSession` 路径恢复——这是刻意设计，确保第一次交互时数据已就绪）。

**预期结果**：写出形如下面的时序表（节选）：

| 时机 | 函数 | rime_api 调用 | 作用 |
| --- | --- | --- | --- |
| handler 构造 | 构造函数 | `rime_get_api()` | 取引擎句柄 |
| handler 构造 | `_Setup()` | `setup(&traits)` | 登记目录/发行版身份 |
| handler 构造 | `_Setup()` | `set_notification_handler(OnNotify, this)` | 挂通知回调 |
| Server 启动后 | `Initialize()` | `initialize(NULL)` | 构造引擎 |
| Server 启动后 | `Initialize()` | `start_maintenance(False)` / `join_maintenance_thread()` | 必要时重建并等待 |
| Server 启动后 | `Initialize()` | `config_open/config_get_*/config_close` | 读 weasel.yaml |
| Server 退出时 | `Finalize()` | `finalize()` | 销毁引擎 |

#### 4.2.5 小练习与答案

**练习 1**：`Initialize()` 里 `start_maintenance()` 返回 `true` 时为什么要把 `m_disabled` 置 `true`，还要 `join_maintenance_thread()`？

**参考答案**：`start_maintenance` 返回 `true` 表示「确实有数据需要重建」，它在后台线程跑。重建期间用户词典/方案处于不一致状态，必须禁用按键（`m_disabled=true`）以免读到半成品数据；`join_maintenance_thread()` 阻塞当前线程直到重建完成，保证 `Initialize()` 返回时数据可用。注意 `join` 后 `m_disabled` 并未自动复位为 `false`——要等第一次 `AddSession` 触发 `EndMaintenance` 恢复。

**练习 2**：若用户在 WeaselServer 运行期间从托盘点【重新部署】，引擎数据会被另一个进程改写。Server 自己怎么「知道」并避免冲突？

**参考答案**：托盘的【重新部署】菜单项（见 [WeaselServerApp.cpp:52-54](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L52-L54)）启动的是**独立的 `WeaselDeployer.exe`**。部署器启动后会持有 `WeaselDeployerMutex`，并通过 IPC 让 Server 进入 `StartMaintenance`（`Finalize` 掉引擎、清会话）；部署完成后 Server 再经 `EndMaintenance` → `Initialize` 重新装载。命名互斥体 + 维护模式共同保证了「同一时刻只有一个进程在动用户数据」。

---

### 4.3 OnNotify 通知与互斥锁

#### 4.3.1 概念说明

librime 不只是被动地被调用，它还会在关键时刻**主动推送消息**给前端，比如：

- 部署开始 / 成功 / 失败（`message_type == "deploy"`）；
- 切换了输入方案（`"schema"`）；
- 某个开关状态翻转，如中/英、全/半角（`"option"`）。

推送机制是回调：前端用 `set_notification_handler` 注册一个函数指针，librime 在适当时机调用它，传入「会话 id、消息类型、消息值」。在 Weasel 里这个回调是静态成员 `RimeWithWeaselHandler::OnNotify`。

这里有一个**线程安全的棘手之处**（代码注释也点明了）：注释写道 `// may be running in a thread when deploying rime`——也就是说，`OnNotify` 可能在**维护/部署的后台线程**里被调用，而消费这些消息的 `_ShowMessage` / `_UpdateUI` 却运行在**主线程**。如果消息直接写进普通成员变量，就会出现数据竞争。

Weasel 的解法是经典的「**生产者写静态缓冲 + 互斥锁 + 消费者取走后清空**」：

- 用一组**静态成员**（`m_message_type` / `m_message_value` / `m_message_label` / `m_option_name`）当共享信箱；
- 一把**静态互斥锁** `m_notifier_mutex` 保护它们；
- 生产者 `OnNotify` 在锁内写入；
- 消费者 `_ShowMessage` 在锁内读取并决定怎么显示；
- `_UpdateUI` 在刷新完成后在锁内把信箱**清空**，避免同一条消息被重复提示。

为什么用**静态**成员而不是实例成员？因为 `OnNotify` 是 C 风格回调，签名由 librime 决定（`void(void*, uintptr_t, const char*, const char*)`，没有 `this`），只能做成静态函数；它通过 librime 回传的 `context_object`（即注册时传的 `this`）找回对象。而信箱做成静态，配合静态锁，是最直接的线程安全写法。

#### 4.3.2 核心流程

通知从「引擎产生」到「用户看到提示」的完整生命周期：

```text
[部署/按键线程] librime 产生事件
      └─ OnNotify(context_object=this, session_id, type, value)   ← 静态回调
            └─ lock(m_notifier_mutex)
                  ├─ m_message_type  = type
                  ├─ m_message_value = value
                  └─ 若 type=="option"：
                        用 RIME_API_AVAILABLE 判定 get_state_label 是否存在
                        解析 "!xxx" 表示关闭，取选项名与本地化标签写入 m_message_label / m_option_name
                  （写入后即返回，不直接碰 UI）

[主线程] 下一次按键或会话动作触发 _UpdateUI(ipc_id)
      └─ _ShowMessage(ctx, status)
            └─ lock(m_notifier_mutex) 读取 type/value/label
            └─ 把提示写进 ctx.aux / status，按规则决定是否 m_ui->ShowWithTimeout(...)
      └─ _UpdateUI 末尾：
            └─ lock(m_notifier_mutex) 清空 type/value/label/option_name   ← 消费完毕
```

`option` 消息的取值约定值得注意：librime 用前缀 `!` 表示「关闭」，如 `"!ascii_mode"` 表示切回中文、`"ascii_mode"` 表示切到英文。`OnNotify` 里用 `message_value[0] != '!'` 解析出布尔状态，再用 `message_value + !state` 跳过可能的 `!` 前缀拿到选项名。

`RIME_API_AVAILABLE(rime_api, get_state_label)` 是 librime 的版本判定宏——只有当当前 librime 版本的 `RimeApi` 表里存在 `get_state_label` 这个字段时才为真，避免在旧版 librime 上调用不存在的函数。

#### 4.3.3 源码精读

静态成员的**声明**在头文件 [include/RimeWithWeasel.h:109-117](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h#L109-L117)：

```cpp
static void OnNotify(void* context_object,
                     uintptr_t session_id,
                     const char* message_type,
                     const char* message_value);
static std::string m_message_type;
static std::string m_message_value;
static std::string m_message_label;
static std::string m_option_name;
static std::mutex m_notifier_mutex;
```

静态成员的**定义**（C++ 静态成员需在类外定义一次）见 [RimeWithWeasel/RimeWithWeasel.cpp:377-381](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L377-L381)：

```cpp
std::string RimeWithWeaselHandler::m_message_type;
std::string RimeWithWeaselHandler::m_message_value;
std::string RimeWithWeaselHandler::m_message_label;
std::string RimeWithWeaselHandler::m_option_name;
std::mutex RimeWithWeaselHandler::m_notifier_mutex;
```

`OnNotify` 实现见 [RimeWithWeasel/RimeWithWeasel.cpp:383-406](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L383-L406)：

```cpp
void RimeWithWeaselHandler::OnNotify(void* context_object,
                                     uintptr_t session_id,
                                     const char* message_type,
                                     const char* message_value) {
  // may be running in a thread when deploying rime
  RimeWithWeaselHandler* self =
      reinterpret_cast<RimeWithWeaselHandler*>(context_object);
  if (!self || !message_type || !message_value)
    return;
  std::lock_guard<std::mutex> lock(m_notifier_mutex);
  m_message_type = message_type;
  m_message_value = message_value;
  if (RIME_API_AVAILABLE(rime_api, get_state_label) &&
      !strcmp(message_type, "option")) {
    Bool state = message_value[0] != '!';
    const char* option_name = message_value + !state;
    m_option_name = option_name;
    const char* state_label =
        rime_api->get_state_label(session_id, option_name, state);
    if (state_label) {
      m_message_label = std::string(state_label);
    }
  }
}
```

要点：

- `context_object` 就是 `_Setup()` 注册时传入的 `this`，这里 `reinterpret_cast` 找回对象指针（注意：本函数其实只用到静态成员，`self` 主要用于空指针校验）。
- 一进来就 `std::lock_guard` 加锁，整段对共享缓冲的写操作都在锁内。
- `option` 分支解析 `!` 前缀、取本地化标签。`!state` 作为指针偏移：`state` 为 `true`（无 `!`）时偏移 0，`state` 为 `false`（有 `!`）时偏移 1，正好跳过 `!`。

消费端 `_ShowMessage` 取锁见 [RimeWithWeasel/RimeWithWeasel.cpp:670-672](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L670-L672)：

```cpp
bool RimeWithWeaselHandler::_ShowMessage(Context& ctx, Status& status) {
  std::lock_guard<std::mutex> lock(m_notifier_mutex);
  if (m_message_type.empty() || m_message_value.empty())
    return m_ui->IsCountingDown();
  // ... 根据 type 把提示写进 ctx.aux / status ...
```

`_UpdateUI` 在刷新完 UI 后清空信箱，见 [RimeWithWeasel/RimeWithWeasel.cpp:546-552](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L546-L552)：

```cpp
{
  std::lock_guard<std::mutex> lock(m_notifier_mutex);
  m_message_type.clear();
  m_message_value.clear();
  m_message_label.clear();
  m_option_name.clear();
}
```

这一步是「消费确认」：清空后，下一次 `_ShowMessage` 看到 `m_message_type.empty()` 就知道没有待处理通知，避免同一条 deploy/schema/option 消息被反复弹出。

#### 4.3.4 代码实践

**实践目标**：跟踪一条「切换输入方案」通知从产生到显示再到清空的完整链路，理解三个函数如何协作。

**操作步骤**：

1. 定位注册点：`_Setup()` 里的 `set_notification_handler(&RimeWithWeaselHandler::OnNotify, this)`（[L104](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L104)）。
2. 假想用户切方案，librime 推送 `message_type="schema"`、`message_value="<方案名>"`。阅读 `OnNotify`（[L383-406](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L383-L406)），确认它把这两个字符串写入 `m_message_type` / `m_message_value`（`schema` 不进 `option` 分支，故 `m_message_label` 不被改写）。
3. 跳到 `_ShowMessage`（[L670-729](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L670-L729)），找到 `else if (m_message_type == "schema")` 分支：它把 `status.schema_name` 赋给 `ctx.aux.str` 作为提示文字，再根据 `m_show_notifications` 规则与 `m_show_notifications_time` 决定是否 `ShowWithTimeout`。
4. 跳到 `_UpdateUI`（[L518-553](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L518-L553)），确认末尾在锁内 `clear()` 了四个静态缓冲。
5. 在纸上画出三个函数对同一把 `m_notifier_mutex` 的「写 → 读 → 清」时序，标注各自所在线程。

**需要观察的现象**：

- `OnNotify` 自身**不调用任何 `m_ui->...`**，只写缓冲。UI 的真正更新发生在主线程的 `_ShowMessage`。
- 三处对静态缓冲的访问，**每一处都在 `lock_guard` 保护下**，没有任何一次「锁外读静态缓冲」。

**预期结果**：得到一张时序图，说明通知的「产生—缓存—展示—清除」四步分别由哪个函数、在哪个线程、在锁内完成。

> 本实践为源码阅读型。若要实测，可在 `OnNotify` 入口与 `_ShowMessage` 的 `schema` 分支各加一行 `DLOG(INFO)` 日志（**示例代码**，非项目原有）：
> ```cpp
> // 示例代码：仅用于观察，勿提交
> DLOG(INFO) << "OnNotify: " << message_type << "=" << message_value;
> ```
> 随后用 Debug 版本 `WeaselServer.exe` 复现切方案，查看 `%TEMP%\rime.weasel\` 下的日志。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接在 `OnNotify` 里调用 `m_ui->ShowWithTimeout(...)` 弹提示，而要绕一圈经静态缓冲？

**参考答案**：因为 `OnNotify` 可能在 librime 的**部署后台线程**被调用，而 `m_ui`（WeaselPanel 窗口）只能在创建它的**主线程**操作（Win32 窗口的线程亲和性）。直接在回调里碰 UI 会跨线程访问窗口，轻则无效、重则死锁或崩溃。用静态缓冲把消息「暂存」，再由主线程在 `_UpdateUI` 里安全地取出并渲染，是标准的「生产者—消费者」解耦。

**练习 2**：`_UpdateUI` 末尾为什么要在锁内 `clear()` 四个缓冲？不清会怎样？

**参考答案**：清空是「消费确认」。若不清，下一次 `_UpdateUI`（哪怕对应的按键并没有新通知）时 `_ShowMessage` 仍会读到旧消息，导致同一条提示被反复弹出。清空后，`_ShowMessage` 看到 `m_message_type.empty()` 就走「无消息」分支，避免重复提示。

**练习 3**：`OnNotify` 里 `const char* option_name = message_value + !state;` 这句如何同时处理 `"ascii_mode"` 与 `"!ascii_mode"` 两种取值？

**参考答案**：`state = message_value[0] != '!'`——无 `!` 前缀时 `state` 为 `true`（`!state` 为 0），指针偏移 0，`option_name` 指向 `"ascii_mode"`；有 `!` 前缀时 `state` 为 `false`（`!state` 为 1），指针偏移 1，跳过 `!`，`option_name` 仍指向 `"ascii_mode"`。一句代码同时解析出「开关状态」与「干净的选项名」，是 C 字符串指针运算的典型用法。

---

## 5. 综合实践

**任务**：制作一张「`RimeWithWeaselHandler` 引擎初始化全景图」，把本讲三个模块串成一张可长期保存的速查图。

**要求**：

1. 横轴是**时间**，按以下顺序排列关键节点（并标注对应的源码位置）：
   - `WeaselServerApp` 构造 → `new RimeWithWeaselHandler(&m_ui)`（[WeaselServerApp.cpp:6](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L6)）；
   - `RimeWithWeaselHandler` 构造函数体（[RimeWithWeasel.cpp:37-58](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L37-L58)）；
   - `_Setup()`（[L89-105](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L89-L105)）；
   - `WeaselServerApp::Run()` 中调用 `m_handler->Initialize()`（[WeaselServerApp.cpp:32](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L32)）；
   - `Initialize()`（[RimeWithWeasel.cpp:107-147](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L107-L147)）；
   - Server 退出时的 `Finalize()`（[L149-155](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L149-L155)）。
2. 在每个节点下方，**列出该节点调用的全部 `rime_api` 函数**及其一句话作用。例如 `_Setup()` 节点下应列出 `rime_get_api`（实际在构造函数）、`setup`、`set_notification_handler`。
3. 用一条**纵向侧栏**单独画出「`OnNotify` 信箱」：标注它由 `_Setup` 注册、由 `OnNotify`（可能在线程）在锁内**写**、由 `_ShowMessage` 在锁内**读**、由 `_UpdateUI` 在锁内**清**。
4. 用**虚线**标出两条「禁用态」路径：
   - `_IsDeployerRunning()` 为真 → `Initialize()` 提前 return；
   - `start_maintenance` 返回真 → `m_disabled=true` 直到 `join` 结束。
5. 在图边写一条「**踩坑提醒**」：`_Setup()` 里 trait 的 `*_dir` 指针指向局部变量，不能在函数返回后使用。

**验收标准**：拿着这张图，你能不查源码就回答：「切方案时弹出的提示，是在哪个函数、哪个线程、哪把锁下被读出来并显示的？」如果答得出「`_ShowMessage` / 主线程 / `m_notifier_mutex`」，本讲就过关了。

## 6. 本讲小结

- `RimeWithWeaselHandler` 是 `weasel::RequestHandler` 的唯一实现，也是**全仓库唯一直接调用 librime C API 的类**，实现了 IPC 传输层与引擎层的解耦。
- librime 的 C API 是一张函数指针表 `RimeApi`，经 `rime_get_api()` 获取；`RimeTraits` 向引擎登记数据目录与发行版身份，`setup()` 必须在 `initialize()` 之前调用。
- 初始化是**三段式**：构造函数取句柄并 `_Setup()`（`setup` + 挂回调）→ `WeaselServerApp::Run()` 显式调 `Initialize()`（`initialize` + 维护 + 读 `weasel.yaml`）→ 退出时 `Finalize()`（`finalize`）。
- 「维护模式」+「命名互斥体 `WeaselDeployerMutex`」共同保证引擎数据重建期间按键被禁用、且不会与独立的 `WeaselDeployer.exe` 同时改写用户数据。
- `OnNotify` 是 librime 的主动通知回调，可能运行在部署后台线程；Weasel 用**静态消息缓冲 + `m_notifier_mutex`** 实现「生产者写—消费者读—用完清」的线程安全，UI 更新严格留在主线程。
- `RIME_STRUCT` / `RIME_API_AVAILABLE` 是 librime 的 ABI 版本协商机制，分别用于「带版本头初始化结构体」和「按版本判定某 API 字段是否存在」。

## 7. 下一步学习建议

本讲只搭好了「引擎拉起来、回调接回来」的地基，按键与会话的处理细节还未展开。建议下一步：

- **u4-l2 会话管理与按键处理**：阅读 `AddSession` / `RemoveSession` / `ProcessKeyEvent` / `_Respond`，搞清 `WeaselSessionId`（带 pid 编码的 IPC 会话号）与 librime `RimeSessionId` 的双向映射，以及一次按键如何经 `rime_api->process_key` 后由 `_Respond` 回写响应文本。
- **u4-l3 方案配置、App 选项与 inline preedit**：阅读 `_LoadSchemaSpecificSettings` / `_LoadAppOptions` / `_LoadAppInlinePreeditSet`，理解按方案/按应用加载配置的机制。
- **u4-l4 UI 更新、消息通知与维护/主题**：精读 `_UpdateUI` / `_ShowMessage` 的完整分支，把本讲 4.3 的通知链路在「显示策略」层面补全。
- **延伸阅读**：如果想更深入 librime 本身，可在本仓库执行 `git submodule update --init librime` 拉取子模块，阅读其中的 `rime_api.h`（`RimeApi` 结构体与各函数指针的权威定义）与 `rime_api.cc`，对照本讲提到的 `setup` / `initialize` / `start_maintenance` / `set_notification_handler` 的实现。
