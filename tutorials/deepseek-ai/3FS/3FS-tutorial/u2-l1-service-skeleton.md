# 服务骨架：TwoPhaseApplication 与 ServerLauncher

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚为什么 `meta`、`storage`、`mgmtd` 三个服务的 `main` 入口长得几乎一模一样，背后是同一套「两阶段启动」骨架。
- 顺着 `main` → `run()` → `initApplication()` → `start()` 的调用链，画出任意一个 3FS 服务的统一启动时序。
- 看懂 `ServerLauncher`、`ServerEnv`、`ServerAppConfig` 这三个「启动三件套」分别承担什么职责，以及它们如何配合把本地引导配置、远端运行时配置、节点身份信息拼装成一个可运行的服务。
- 识别 `beforeStart / afterStart / beforeStop / afterStop` 这四个生命周期钩子，知道在哪个阶段做客户端创建、在哪个阶段做清理。

本讲是「公共基础设施」单元（u2）的第一篇，承接 [u1-l4](u1-l4-end-to-end-flow.md) 建立的端到端视图——那一讲已经指出「三服务共用 `TwoPhaseApplication` 启动骨架」。本讲就深入这套骨架的内部实现。

## 2. 前置知识

### 2.1 为什么需要一套统一的启动骨架

一个真实的 3FS 服务在「真正开始处理 RPC」之前，要做一大堆准备工作：

1. **解析命令行参数与配置文件**（gflags + TOML）。
2. **初始化日志、内存分配器、监控**等公共组件。
3. **确定自己的身份**：我是哪个集群（`clusterId`）、哪个节点（`nodeId`）、监听哪些端口、提供哪些 RPC 服务（`AppInfo`）。
4. **拉取运行时配置**：服务启动时往往只知道一个「mgmtd 的地址」，真正的运行时配置（线程池大小、超时、路由表等）要从 mgmtd 拉取。
5. **初始化 RDMA/IB 设备**：3FS 严重依赖 InfiniBand，需要在最早期就把网卡准备好。
6. **创建各类客户端**（mgmtd client / storage client / meta client），加入集群。

如果每个服务（meta、storage、mgmtd、monitor_collector……）都自己手写一遍这套流程，会出现大量重复代码，且容易出 bug。3FS 的解法是：把这套流程抽象成一个模板类 `TwoPhaseApplication<Server>`，所有服务只要提供「自己的 `Server` 类型」就能复用。

### 2.2 几个关键术语

- **Launcher（启动器）**：一个只在「引导阶段」存在的临时对象，负责解析本地配置、启动 IB、拼装 `AppInfo`、拉取运行时配置模板。引导完成后它就被销毁。
- **App（应用）**：真正长期运行的对象，内部持有一个 `net::Server`，负责监听端口、处理 RPC。
- **AppInfo**：一个服务的「身份证」，包含 `clusterId`、`nodeId`、主机名、监听地址列表、提供的服务列表等。详见 `src/common/app/AppInfo.h`。
- **gflags**：Google 的命令行参数库，3FS 用它定义 `--app_cfg`、`--launcher_cfg`、`--cfg` 等参数。

### 2.3 C++ 模板与 CRTP 的直觉

`TwoPhaseApplication` 是一个模板类：

```cpp
template <typename Server>
class TwoPhaseApplication : public ApplicationBase { ... };
```

调用方把「自己定义的 Server 类型」作为模板参数传进来，骨架就能复用。这是 C++ 里非常常见的「静态多态」手法，好处是零运行时开销。你不需要精通模板元编程，只要知道「`Server` 是一个类型参数，由各服务自己指定」即可。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/simple_example/main.cpp` | 最简服务的入口，只有 8 行，是理解骨架的最佳起点。 |
| `src/common/app/TwoPhaseApplication.h` | 本讲的主角：两阶段启动模板类的全部逻辑都在这里。 |
| `src/common/app/ApplicationBase.h` / `.cc` | 所有应用的公共基类，定义 `run()` 主循环、信号处理、配置热更新接口。 |
| `src/core/app/ServerLauncher.h` | 启动器模板：解析配置、起 IB、拼 `AppInfo`、拉配置模板。 |
| `src/core/app/ServerLauncherConfig.h` | 启动器自己的配置（`cluster_id`、`ib_devices`、`mgmtd_client` 等）。 |
| `src/core/app/ServerEnv.h` | 运行时共享环境：把 `AppInfo`、KV 引擎、mgmtd stub 工厂等共享对象集中存放。 |
| `src/core/app/ServerAppConfig.h` | 应用层配置，核心就是 `node_id`（节点身份）。 |
| `src/simple_example/service/Server.h` / `.cc` | 示例服务的具体实现，展示如何继承 `net::Server`、实现 `beforeStart`。 |
| `src/common/net/Server.h` / `.cc` | 真正的网络服务基类，定义 `setup/start/stopAndJoin` 与四个生命周期钩子。 |

> 提示：`src/core/app/` 下的文件是「服务通用启动件」，而 `src/common/app/` 下的文件是「应用通用基件」。`core` 依赖 `common`，所以 `ServerLauncher` 内部会用 `ApplicationBase` 提供的能力。

## 4. 核心概念与源码讲解

### 4.1 两阶段启动

#### 4.1.1 概念说明

「两阶段（Two-Phase）」指的是一个 3FS 服务的生命周期被明确切成两段：

- **阶段一：引导（Launch）**。此时服务还不知道自己的完整运行时配置，只有本地几份「引导配置」。`Launcher` 用这些引导配置启动 IB、联系 mgmtd、拿到 `AppInfo`（身份）和完整的运行时配置模板。引导完成后，`Launcher` 就被 `reset()` 销毁——它是一个「用过即弃」的对象。
- **阶段二：运行（Run）**。`net::Server` 拿着合并好的最终配置，完成 `setup()` → `start()`，然后进入主循环等待信号；收到 `SIGTERM`/`SIGINT` 后再走 `stop()` 清理。

为什么要这么切？因为「拿到运行时配置」本身依赖「能连上 mgmtd」，而「连上 mgmtd」又依赖「至少有本地引导配置告诉我 mgmtd 在哪」。这是一个典型的「自举（bootstrap）」问题：必须先用一份极简的本地配置把网络和身份跑起来，才能去拉取真正的配置。把这两段显式分开，代码职责非常清晰。

> 名称澄清：模板类叫 `TwoPhaseApplication`，而它内部又把「引导」拆成 `init()`、把「运行」拆成 `initServer()`+`startServer()`。不要被多个名字绕晕——核心就是「先引导、后运行」，`launcher_.reset()`（[TwoPhaseApplication.h:67](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/TwoPhaseApplication.h#L67)）正是两阶段的交接点。

#### 4.1.2 核心流程

整个启动由 `ApplicationBase::run()` 驱动，下面是它的执行流程（标注了阶段归属）：

```text
main(argc, argv)
  └── TwoPhaseApplication<Server>().run(argc, argv)        // src/simple_example/main.cpp:7
        │
        ├──【公共前置】blockInterruptSignals()              // 屏蔽信号，避免启动期被打断
        ├──【公共前置】parseFlags(argc, argv)               // 解析 --app_cfg / --launcher_cfg / --cfg.* 等
        ├──【公共前置】folly::init(&argc, &argv)            // gflags/google log 初始化
        │
        ├──【阶段一：引导】initApplication()                 // 见下方拆解
        │     ├── launcher_->init()                         // 加载本地配置 + 起 IB + 建 fetcher
        │     ├── loadAppInfo()                             // 向 mgmtd 拿 / 拼 AppInfo
        │     ├── initConfig(...)                           // 拉取并合并运行时配置模板
        │     ├── initCommonComponents(...)                 // 日志/监控/内存
        │     ├── initServer()  → server_->setup()          // 建服务对象、绑定端口
        │     ├── startServer() → server_->start(appInfo)   // ★ 进入阶段二：beforeStart→group.start→afterStart
        │     └── launcher_.reset()                         // ★ 销毁启动器，阶段一结束
        │
        ├──【阶段二：运行】mainLoop()                        // 注册信号，条件变量阻塞等待
        │     └── 收到 SIGTERM/SIGINT/SIGUSR1/SIGUSR2 唤醒
        │
        └──【收尾】memory::shutdown() → stop()              // stopAndJoin(server) + 关 monitor + 关 IB
```

阶段一的 `initApplication()` 是真正的「装配车间」，它把本地引导配置、远端运行时配置、公共组件、服务对象、生命周期钩子按固定顺序串起来。任何一个环节失败都会 `XLOGF_IF(FATAL, ...)` 直接终止进程——启动期不容许「带病上岗」。

#### 4.1.3 源码精读

**入口：所有服务的 `main` 都是一行模板调用。**

以 `simple_example` 为例（`storage`、`meta`、`mgmtd` 的 main 仅模板参数不同）：

```cpp
// src/simple_example/main.cpp:5-8
int main(int argc, char *argv[]) {
  using namespace hf3fs;
  return TwoPhaseApplication<simple_example::server::SimpleExampleServer>().run(argc, argv);
}
```

把三个真实服务并排看就更清楚了——它们结构完全相同（[meta/meta.cpp:5-8](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/meta.cpp#L5-L8)、[storage/storage.cpp:5-8](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/storage.cpp#L5-L8)、[mgmtd/mgmtd.cpp:5-8](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/mgmtd.cpp#L5-L8)）：唯一的差别就是尖括号里的 `Server` 类型。这就是「统一启动骨架」最直接的证据。

**`run()` 主循环由基类提供。**

[ApplicationBase.cc:49-74](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ApplicationBase.cc#L49-L74) 定义了所有应用共享的 `run()`：

```cpp
int ApplicationBase::run(int argc, char *argv[]) {
  Thread::blockInterruptSignals();          // 20 屏蔽信号
  auto parseFlagsRes = parseFlags(&argc, &argv);
  XLOGF_IF(FATAL, !parseFlagsRes, ...);
  folly::init(&argc, &argv);                // 30 初始化 folly/gflags
  if (FLAGS_release_version) { ... return 0; }  // --release_version 只打印版本就退出
  auto initRes = initApplication();         // 40 阶段一装配（由子类 TwoPhaseApplication 实现）
  XLOGF_IF(FATAL, !initRes, ...);
  auto exitCode = mainLoop();               // 阶段二：阻塞等待信号
  memory::shutdown();
  stop();                                   // 收尾清理
  return exitCode;
}
```

注意三个被注释编号 `20/30/40` 的步骤——这是作者刻意留下的阶段标记。`initApplication()` 和 `stop()` 是纯虚函数（[ApplicationBase.h:62-64](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ApplicationBase.h#L62-L64)），由 `TwoPhaseApplication` 提供具体实现。

**阶段二主循环：信号驱动的优雅退出。**

[ApplicationBase.cc:76-90](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ApplicationBase.cc#L76-L90) 的 `mainLoop()` 很精巧：

```cpp
int ApplicationBase::mainLoop() {
  signal(SIGINT, handleSignal);     signal(SIGTERM, handleSignal);
  signal(SIGUSR1, handleSignal);    signal(SIGUSR2, handleSignal);
  Thread::unblockInterruptSignals();
  {
    auto lock = std::unique_lock(loopMutex);
    loopCv.wait(lock, [] { return exitLoop.load(); });   // 阻塞，直到 handleSignal 置位
  }
  return exitCode.load();
}
```

服务启动后就「睡」在一个条件变量上，只有收到信号（`handleSignal` 把 `exitLoop` 置 true 并 `notify_one`，见 [ApplicationBase.cc:40-47](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ApplicationBase.cc#L40-L47)）才会醒来。其中 `SIGUSR2` 会让进程以 `128+SIGUSR2` 退出码退出（常被 systemd 用来做「重启」判断）。

**阶段一装配：`TwoPhaseApplication::initApplication()`。**

这是本讲信息密度最高的一段，[TwoPhaseApplication.h:36-70](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/TwoPhaseApplication.h#L36-L70)：

```cpp
Result<Void> TwoPhaseApplication::initApplication() final {
  if (FLAGS_dump_default_cfg) { fmt::print(...); exit(0); }   // --dump_default_cfg 打印默认配置后退出
  auto firstInitRes = launcher_->init();                       // ① 加载本地配置 + 起 IB + 建 fetcher
  XLOGF_IF(FATAL, !firstInitRes, ...);
  app_detail::loadAppInfo([this]{ return launcher_->loadAppInfo(); }, appInfo_);          // ② 拼 AppInfo
  app_detail::initConfig(config_, configFlags_, appInfo_, [this]{ return launcher_->loadConfigTemplate(); });  // ③ 拉运行时配置
  app_detail::initCommonComponents(config_.common(), Server::kName, appInfo_.nodeId);     // ④ 日志/监控/内存
  onLogConfigUpdated_ = app_detail::makeLogConfigUpdateCallback(...);   // ⑤ 注册热更新回调
  onMemConfigUpdated_  = app_detail::makeMemConfigUpdateCallback(...);
  app_detail::persistConfig(config_);                                  // ⑥ 落盘最终配置
  auto initRes = initServer();        // ⑦ 构造 Server 对象并 setup()
  auto startRes = startServer();      // ⑧ start()：beforeStart→group.start→afterStart（进入阶段二）
  launcher_.reset();                  // ⑨ ★ 销毁启动器
  return Void{};
}
```

关键点：

- **① `launcher_->init()`** 是引导的起点，详见 4.2。
- **②③ 拼身份 + 拉配置**：先用本地的 `node_id`/`cluster_id` 构造一个基本 `AppInfo`，再联系 mgmtd 把它补全；同时用 `loadConfigTemplate()` 从 mgmtd 拉运行时配置模板，与命令行 `--config.*` 覆盖项合并。
- **⑦⑧ `initServer()` / `startServer()`** 是阶段一内部对阶段二的「交接」：先 `setup()` 绑定端口（[TwoPhaseApplication.h:91-96](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/TwoPhaseApplication.h#L91-L96)），再 `start()` 真正跑起来（[TwoPhaseApplication.h:98-103](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/TwoPhaseApplication.h#L98-L103)）。
- **⑨ `launcher_.reset()`** 是两阶段的物理边界——启动器用完即弃。

**收尾：`stop()` 的对称清理。**

[TwoPhaseApplication.h:72-80](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/TwoPhaseApplication.h#L72-L80) 与启动严格对称：先确保 `launcher_` 已销毁（防止引导期崩溃残留），再 `stopAndJoin(server_)`。`stopAndJoin` 的实现在 [ApplicationBase.cc:275-283](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ApplicationBase.cc#L275-L283)，依次关闭 `net::Server`、`monitor`、`IBManager`。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，亲手画出 `simple_example` 从 `main` 到进入主循环的完整时序，并定位「两阶段交接点」。

**操作步骤**：

1. 打开 [src/simple_example/main.cpp](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/simple_example/main.cpp)，确认入口只有一行 `TwoPhaseApplication<...>().run(...)`。
2. 跳到 [ApplicationBase.cc:49](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ApplicationBase.cc#L49) 的 `run()`，记下 `parseFlags → folly::init → initApplication → mainLoop → stop` 五步。
3. 跳到 [TwoPhaseApplication.h:36](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/TwoPhaseApplication.h#L36) 的 `initApplication()`，把上面 ①~⑨ 九个步骤逐一对应到行号。
4. 找到 [TwoPhaseApplication.h:67](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/TwoPhaseApplication.h#L67) 的 `launcher_.reset()`，在图上标注「★ 阶段一结束 / 阶段二开始」。
5. 跳到 [ApplicationBase.cc:76](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ApplicationBase.cc#L76) 的 `mainLoop()`，确认服务最终阻塞在 `loopCv.wait(...)`。

**需要观察的现象**：

- `run()` 中三个被作者注释为 `// 20`、`// 30`、`// 40` 的步骤，分别对应「解析参数」「初始化 folly」「初始化应用」。
- `initApplication()` 内部并没有真正的「死循环」——它做完装配就返回，之后控制权交给 `mainLoop()`。

**预期结果**：你能画一张包含两层结构的时序图：外层是 `run()` 的五步骨架，内层是 `initApplication()` 的九步装配，并用一个箭头标出 `launcher_.reset()` 这个交接点。

> 说明：本实践是「源码阅读型实践」，无需运行集群，专注于建立骨架的全局心智模型。

#### 4.1.5 小练习与答案

**练习 1**：如果 `initServer()` 成功但 `startServer()` 失败，进程会怎样？配置会落盘吗？

**参考答案**：会 `XLOGF_IF(FATAL, ...)` 直接终止进程（见 [TwoPhaseApplication.h:62-65](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/TwoPhaseApplication.h#L62-L65)）。注意 `persistConfig(config_)` 在 ⑥ 已经执行过（[TwoPhaseApplication.h:55](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/TwoPhaseApplication.h#L55)），所以合并后的配置会落盘，便于事后排查；但服务不会上线。

**练习 2**：`launcher_.reset()` 为什么放在 `initApplication()` 末尾，而不是 `stop()` 里？

**参考答案**：因为启动器只在引导阶段被需要——身份和配置一旦拿到，运行期就不再依赖它。尽早销毁可以释放它持有的资源（如引导用的网络连接），并明确表达「引导结束」的语义。`stop()` 里对 `launcher_` 的判空（[TwoPhaseApplication.h:74-76](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/TwoPhaseApplication.h#L74-L76)）只是为了兜底：若进程在引导途中崩溃，`launcher_` 可能还活着。

**练习 3**：`--release_version` 这个参数的作用是什么？它在哪一步生效？

**参考答案**：在 [ApplicationBase.cc:59-62](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ApplicationBase.cc#L59-L62)，`folly::init` 之后、`initApplication` 之前。它只打印版本号和完整 commit hash 然后返回 0 退出，常用于运维查询部署的二进制版本，不会启动任何服务。

---

### 4.2 ServerLauncher：引导阶段的「装配工」

#### 4.2.1 概念说明

`ServerLauncher` 是「阶段一」的核心执行者。`TwoPhaseApplication` 把引导的具体活儿全部委托给它。一个 `Server` 类型要想接入骨架，必须通过几个内嵌类型别名告诉 `ServerLauncher` 三件事：

- `AppConfig`：应用层配置的类型（通常是 `core::ServerAppConfig`）。
- `LauncherConfig`：启动器自己的配置类型（通常是 `core::ServerLauncherConfig`）。
- `RemoteConfigFetcher`：负责「联系 mgmtd 拉 `AppInfo` 和运行时配置模板」的抓取器类型。

这些别名在 [simple_example/service/Server.h:35-40](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/simple_example/service/Server.h#L35-L40) 里看得一清二楚：

```cpp
using AppConfig = core::ServerAppConfig;
struct LauncherConfig : public core::ServerLauncherConfig { ... };
using RemoteConfigFetcher = core::launcher::ServerMgmtdClientFetcher;
using Launcher = core::ServerLauncher<SimpleExampleServer>;
```

`ServerLauncher` 拿到这些类型后，就能完成「加载本地配置 → 起 IB → 拼 `AppInfo` → 拉配置模板 → 启动 Server」这条链路。

#### 4.2.2 核心流程

`ServerLauncher` 对外暴露的引导方法（都被 `TwoPhaseApplication::initApplication()` 依次调用）：

```text
launcher_->init()
  ├── appConfig_.init(FLAGS_app_cfg, ...)        # 加载应用配置（含 node_id）
  ├── launcherConfig_.init(FLAGS_launcher_cfg, ...)  # 加载启动器配置（含 cluster_id、ib_devices、mgmtd 地址）
  ├── net::IBManager::start(launcherConfig_.ib_devices())   # ★ 启动 InfiniBand
  └── fetcher_ = make_unique<RemoteConfigFetcher>(launcherConfig_)   # 创建「联系 mgmtd」的抓取器

launcher_->loadAppInfo()
  ├── buildBasicAppInfo(nodeId, clusterId)       # 用本地 node_id/cluster_id 构造基本身份
  └── fetcher_->completeAppInfo(appInfo)         # 找 mgmtd 补全（如主机名、监听地址）

launcher_->loadConfigTemplate()
  └── fetcher_->loadConfigTemplate(kNodeType)    # 按「我是哪类节点」拉对应的运行时配置模板

launcher_->startServer(server, appInfo)
  ├── 若 fetcher 实现了 startServer → fetcher_->startServer(...)   # 抓取器可直接接管（如先注册节点）
  └── 否则 → server.start(appInfo)               # 直接启动 net::Server
```

注意最后一环 `startServer` 用了 C++20 的 `if constexpr (requires {...})`（[ServerLauncher.h:62](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/core/app/ServerLauncher.h#L62)）：编译期检测抓取器是否提供了 `startServer`，有就用它，没有就退回 `server.start()`。这是一种「可选钩子」的实现技巧，让 `ServerMgmtdClientFetcher` 这类抓取器可以在「服务真正 start 之前」插入额外逻辑（例如先把本节点注册进集群）。

#### 4.2.3 源码精读

**`init()`：加载配置 + 起 IB。**

[ServerLauncher.h:34-47](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/core/app/ServerLauncher.h#L34-L47)：

```cpp
Result<Void> init() {
  appConfig_.init(FLAGS_app_cfg, FLAGS_dump_default_app_cfg, appConfigFlags_);
  launcherConfig_.init(FLAGS_launcher_cfg, FLAGS_dump_default_launcher_cfg, launcherConfigFlags_);
  XLOGF(INFO, "Full AppConfig:\n{}", appConfig_.toString());
  XLOGF(INFO, "Full LauncherConfig:\n{}", launcherConfig_.toString());
  auto ibResult = net::IBManager::start(launcherConfig_.ib_devices());   // ★ 关键：尽早起 IB
  XLOGF_IF(FATAL, !ibResult, "Failed to start IBManager: {}", ibResult.error());
  fetcher_ = std::make_unique<RemoteConfigFetcher>(launcherConfig_);
  return Void{};
}
```

三个要点：

1. 配置通过 gflags 指定：`--app_cfg` 指向应用配置文件、`--launcher_cfg` 指向启动器配置文件。`--dump_default_app_cfg` / `--dump_default_launcher_cfg` 用于把默认配置导出（方便首次部署生成模板）。
2. **IB 必须在联系 mgmtd 之前启动**——因为 mgmtd 抓取器很可能走 RDMA，没有 IB 就连不上。
3. 抓取器 `fetcher_` 持有 `launcherConfig_`（里面有 mgmtd 地址），后续所有「找 mgmtd」的操作都通过它。

**`loadAppInfo()`：从「半成品身份」到「完整身份」。**

[ServerLauncher.h:55-59](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/core/app/ServerLauncher.h#L55-L59)：

```cpp
Result<flat::AppInfo> loadAppInfo() {
  auto appInfo = launcher::buildBasicAppInfo(appConfig_.getNodeId(), launcherConfig_.cluster_id());
  RETURN_ON_ERROR(fetcher_->completeAppInfo(appInfo));
  return appInfo;
}
```

`buildBasicAppInfo`（声明在 [LauncherUtils.h:6](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/core/app/LauncherUtils.h#L6)）用本地的 `node_id` + `cluster_id` 拼一个最小身份；`completeAppInfo` 再找 mgmtd 补全。这正体现了「自举」：先用本地知道的最少信息开局。

**`loadConfigTemplate()`：按节点类型拉取运行时配置。**

[ServerLauncher.h:49-53](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/core/app/ServerLauncher.h#L49-L53)：

```cpp
Result<std::pair<String, String>> loadConfigTemplate() {
  auto res = fetcher_->loadConfigTemplate(kNodeType);
  RETURN_ON_ERROR(res);
  return std::make_pair(res->content, res->genUpdateDesc());
}
```

`kNodeType` 是每个 `Server` 类型的编译期常量（如 `SimpleExampleServer::kNodeType = flat::NodeType::CLIENT`，见 [Server.h:22](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/simple_example/service/Server.h#L22)）。不同节点类型（meta/storage/mgmtd/...）会从 mgmtd 拉到不同的运行时配置模板——这就是 [u1-l3](u1-l3-deploy-and-admin-cli.md) 讲过的「`*_main.toml` 由 mgmtd 统一托管」在代码层面的实现。返回的 `pair` 第一项是配置内容、第二项是更新描述（用于热更新审计）。

**`startServer()`：可选钩子。**

[ServerLauncher.h:61-67](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/core/app/ServerLauncher.h#L61-L67) 已在上文给出，核心是 `if constexpr (requires { fetcher_->startServer(server, appInfo); })`。

#### 4.2.4 代码实践

**实践目标**：定位 `RemoteConfigFetcher` 的真实类型，理解「联系 mgmtd」这一步具体由谁完成。

**操作步骤**：

1. 在 [Server.h:39](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/simple_example/service/Server.h#L39) 看到 `using RemoteConfigFetcher = core::launcher::ServerMgmtdClientFetcher;`。
2. 用编辑器全局搜索 `class ServerMgmtdClientFetcher`（文件位于 `src/core/app/ServerMgmtdClientFetcher.h`）。
3. 阅读它的 `loadConfigTemplate`、`completeAppInfo`、`loadAppInfo` 方法，确认它们内部都通过一个 mgmtd client 去 RPC mgmtd。
4. 回到 [ServerLauncher.h:45](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/core/app/ServerLauncher.h#L45)，确认 `fetcher_` 就是用 `launcherConfig_` 构造的这个抓取器。

**需要观察的现象**：

- 抓取器内部持有一个 mgmtd client，而该 client 的地址来自 `launcherConfig_.mgmtd_client()`——也就是本地 `*_launcher.toml` 里写的 mgmtd 地址。这与 [u1-l3](u1-l3-deploy-and-admin-cli.md) 讲的「所有进程启动都要先知道 mgmtd 在哪」完全吻合。
- `loadConfigTemplate` 会根据 `kNodeType` 区分 meta/storage/mgmtd 的配置模板。

**预期结果**：你能用一句话回答「服务启动时是怎么找到 mgmtd 并拿到运行时配置的」——通过 `ServerMgmtdClientFetcher`，它用 `launcher.toml` 里的 mgmtd 地址建立 mgmtd client，按节点类型拉取对应的 `*_main.toml` 模板。

> 说明：`ServerMgmtdClientFetcher` 的实现细节涉及 mgmtd client（[u3](u3-l1-mgmtd-overview.md) 单元会展开），本讲只需理解它在启动链中的位置。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `IBManager::start` 放在 `ServerLauncher::init()` 里，而不是放到 `net::Server::start()` 里？

**参考答案**：因为联系 mgmtd 拉 `AppInfo` 和配置模板这一步可能就要走 RDMA，必须在抓取器工作之前把 IB 准备好。`net::Server::start()` 已经是「阶段二的运行期」，那时 IB 必须早就绪。

**练习 2**：`startServer()` 里的 `if constexpr (requires {...})` 如果去掉、直接调用 `fetcher_->startServer(...)`，会有什么问题？

**参考答案**：这会强制要求所有 `RemoteConfigFetcher` 类型都实现 `startServer` 方法，降低扩展性。用 `if constexpr + requires` 提供「默认退回 `server.start()`」的行为，让简单的抓取器不必实现这个钩子。这是 C++20 concepts 的一个实用场景。

**练习 3**：`kNodeType`（如 `flat::NodeType::CLIENT`）在启动链中起到了什么作用？

**参考答案**：它被传给 `loadConfigTemplate(kNodeType)`，决定从 mgmtd 拉哪一份运行时配置模板。同一套骨架，因为 `kNodeType` 不同，meta/storage/mgmtd 各自拉到适合自己的配置——这是「一套骨架，多种服务」的关键参数之一。

---

### 4.3 应用配置：三层配置与生命周期钩子

#### 4.3.1 概念说明

3FS 的配置体系在 [u1-l3](u1-l3-deploy-and-admin-cli.md) 已经介绍过「三类配置文件」：`*_launcher.toml`（引导）、`*_app.toml`（节点身份）、`*_main.toml`（运行时，由 mgmtd 托管）。本讲从代码角度看它们如何被加载、合并、热更新，以及服务运行期的「生命周期钩子」在配置装配完成后如何被触发。

代码层面的对应关系：

- `*_launcher.toml` → `ServerLauncherConfig`（[ServerLauncherConfig.h:9-21](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/core/app/ServerLauncherConfig.h#L9-L21)）：`cluster_id`、`ib_devices`、`client`、`mgmtd_client`、`allow_dev_version`。
- `*_app.toml` → `ServerAppConfig`（[ServerAppConfig.h:8-19](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/core/app/ServerAppConfig.h#L8-L19)）：核心是 `node_id`（节点身份）。
- `*_main.toml`（远端托管）→ `TwoPhaseApplication::Config`（[TwoPhaseApplication.h:24-27](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/TwoPhaseApplication.h#L24-L27)）里的 `common` + `server` 两部分，由 `loadConfigTemplate()` 从 mgmtd 拉取。

此外还有 `ServerEnv`：它不是配置，而是「运行时共享环境容器」，把 `AppInfo`、KV 引擎、mgmtd stub 工厂、后台线程池、配置更新回调等共享对象集中存放，供服务内部各组件按需取用。

#### 4.3.2 核心流程

**配置加载与合并顺序**：

```text
① ServerLauncher::init()
     ├── appConfig_.init(--app_cfg)         # 加载 *_app.toml → 拿到 node_id
     └── launcherConfig_.init(--launcher_cfg)  # 加载 *_launcher.toml → 拿到 cluster_id、mgmtd 地址、ib_devices

② TwoPhaseApplication::initApplication()
     ├── loadAppInfo()                       # 用 node_id + cluster_id 拼 AppInfo
     └── initConfig(config_, --config.*, appInfo, loadConfigTemplate())
           ├── 从 mgmtd 拉 *_main.toml 模板（common + server）
           ├── 用命令行 --config.xxx 覆盖
           └── 合并成最终 config_

③ 热更新（运行期）
     └── configPushable() == true 时，mgmtd 可推送新配置
           └── ApplicationBase::updateConfig() → getConfigManager().updateConfig(...) → onConfigUpdated()
```

**`configPushable()` 的判定**（[TwoPhaseApplication.h:86](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/TwoPhaseApplication.h#L86)）：

```cpp
bool configPushable() const final { return FLAGS_cfg.empty() && !FLAGS_use_local_cfg; }
```

含义：只有当启动时**没有**用 `--cfg` 指定本地配置文件、也**没有**开启 `--use_local_cfg` 时，才允许 mgmtd 推送配置。换句话说：如果运维显式选择了「用本地配置跑」，就尊重本地配置、拒绝远端推送。这是一种「本地优先」的保护机制。

**`net::Server` 的四个生命周期钩子**（[Server.h:79-88](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Server.h#L79-L88)）：

| 钩子 | 触发时机 | 典型用途 |
| --- | --- | --- |
| `beforeStart()` | `start()` 中、各 ServiceGroup 启动**之前** | 创建 mgmtd/storage 客户端、注册 RPC 服务 |
| `afterStart()` | 各 ServiceGroup 启动**之后** | 启动后台周期任务、对外宣告就绪 |
| `beforeStop()` | `stopAndJoin()` 中、各 ServiceGroup 停止**之前** | 优雅停止客户端、撤销注册 |
| `afterStop()` | 各 ServiceGroup 停止**之后** | 最终资源回收 |

它们由 `net::Server::start()` 和 `stopAndJoin()` 在固定位置调用（见 [Server.cc:35-46](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Server.cc#L35-L46) 与 [Server.cc:48-64](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Server.cc#L48-L64)）。子类（如 `SimpleExampleServer`）通过 `override` 这些虚函数插入自己的逻辑。

#### 4.3.3 源码精读

**应用配置 `ServerAppConfig`：几乎只有 `node_id`。**

[ServerAppConfig.h:8-19](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/core/app/ServerAppConfig.h#L8-L19)：

```cpp
struct ServerAppConfig : public ConfigBase<ServerAppConfig> {
  CONFIG_ITEM(node_id, 0);
  CONFIG_ITEM(allow_empty_node_id, true);
 public:
  using Base = ConfigBase<ServerAppConfig>;
  using Base::init;
  void init(const String &filePath, bool dump, const std::vector<config::KeyValue> &updates);
  flat::NodeId getNodeId() const { return flat::NodeId(node_id()); }
};
```

`CONFIG_ITEM` 是 3FS 配置框架的声明宏（详见 [u2-l5](u2-l5-config-system.md)）。这里只声明了 `node_id`（默认 0）和 `allow_empty_node_id`（默认 true，允许临时未分配 nodeId 的节点启动）。`getNodeId()` 把整数转成强类型 `flat::NodeId`。

**启动器配置 `ServerLauncherConfig`：引导所需的一切。**

[ServerLauncherConfig.h:9-21](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/core/app/ServerLauncherConfig.h#L9-L21)：

```cpp
struct ServerLauncherConfig : public ConfigBase<ServerLauncherConfig> {
  CONFIG_ITEM(cluster_id, "");                         # 集群 ID
  CONFIG_OBJ(ib_devices, net::IBDevice::Config);       # IB 网卡配置
  CONFIG_OBJ(client, net::Client::Config);             # 引导用的网络客户端
  CONFIG_OBJ(mgmtd_client, client::MgmtdClient::Config);  # ★ mgmtd 的地址就在这里
  CONFIG_ITEM(allow_dev_version, true);
};
```

`mgmtd_client` 字段是关键——它告诉抓取器「mgmtd 在哪个地址」。这就是为什么部署文档要求每个服务的 `*_launcher.toml` 都必须正确填写 mgmtd 地址。

**`ServerEnv`：运行时共享环境。**

[ServerEnv.h:10-52](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/core/app/ServerEnv.h#L10-L52) 是一个纯「getter/setter 容器」，集中存放跨组件共享的对象：

```cpp
class ServerEnv {
 public:
  const flat::AppInfo &appInfo() const;            // 服务身份
  const std::shared_ptr<kv::IKVEngine> &kvEngine();  // KV 引擎（如 FoundationDB）
  const std::shared_ptr<MgmtdStubFactory> &mgmtdStubFactory();  // mgmtd RPC stub 工厂
  CPUExecutorGroup *backgroundExecutor();           // 后台线程池
  const ConfigUpdater &configUpdater();             // 配置更新回调
  const ConfigValidater &configValidater();         // 配置校验回调
  // ... 每个 getter 都配有 setXxx
};
```

它的设计意图是「依赖注入容器」：服务内部的各组件不各自去 new 自己的依赖，而是从一个集中的 `ServerEnv` 取。这样便于测试（可注入 mock）和统一生命周期管理。（注意：`ServerEnv` 主要被 `core` 层服务使用；本讲的 `simple_example` 没有直接用 `ServerEnv`，但 meta/storage/mgmtd 等更复杂的服务会用到。）

**`beforeStart` 的真实例子：在 RPC 端口就绪前建好客户端。**

[simple_example/service/Server.cc:21-54](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/simple_example/service/Server.cc#L21-L54) 的 `beforeStart()` 是理解钩子用法的最佳样本：

```cpp
Result<Void> SimpleExampleServer::beforeStart() {
  if (!backgroundClient_) {
    backgroundClient_ = std::make_unique<net::Client>(config_.background_client());
    RETURN_ON_ERROR(backgroundClient_->start());
  }
  if (!mgmtdClient_) {
    auto ctxCreator = [this](net::Address addr) { return backgroundClient_->serdeCtx(addr); };
    mgmtdClient_ = std::make_shared<client::MgmtdClientForServer>(
        appInfo().clusterId,
        std::make_unique<stubs::RealStubFactory<mgmtd::MgmtdServiceStub>>(std::move(ctxCreator)),
        config_.mgmtd_client());
  }
  mgmtdClient_->setAppInfoForHeartbeat(appInfo());
  mgmtdClient_->setConfigListener(ApplicationBase::updateConfig);   // ★ 把热更新入口接上
  mgmtdClient_->updateHeartbeatPayload(flat::MetaHeartbeatInfo{});
  folly::coro::blockingWait(mgmtdClient_->start(&tpg().bgThreadPool().randomPick()));
  auto mgmtdClientRefreshRes = folly::coro::blockingWait(mgmtdClient_->refreshRoutingInfo(false));
  XLOGF_IF(FATAL, !mgmtdClientRefreshRes, "Failed to refresh initial routing info!");
  // ... 创建 storage client、注册 RPC 服务
  RETURN_ON_ERROR(addSerdeService(std::make_unique<SimpleExampleService>(), true));
  RETURN_ON_ERROR(addSerdeService(std::make_unique<core::CoreService>()));
  return Void{};
}
```

这段代码示范了钩子的典型用法：

1. **创建后台网络客户端** `backgroundClient_`（用于发 RPC）。
2. **创建 mgmtd 客户端** `mgmtdClient_`，并把 `ApplicationBase::updateConfig` 设为它的「配置监听器」——这样 mgmtd 在心跳响应里推送新配置时，会直接进入 [ApplicationBase.cc:115](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ApplicationBase.cc#L115) 的 `updateConfig()`，完成热更新。这就是配置「下发」在代码里的接线点。
3. **首次刷新路由信息** `refreshRoutingInfo(false)`——`beforeStart` 阶段必须拿到初始路由表，否则启动期就会 FATAL。
4. **注册 RPC 服务** `addSerdeService`——把自己提供的 service（如 `SimpleExampleService`、`CoreService`）挂到 ServiceGroup 上。

对称地，`beforeStop()`（[Server.cc:56-66](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/simple_example/service/Server.cc#L56-L66)）负责优雅停止 mgmtd client 和 background client。

#### 4.3.4 代码实践

**实践目标**：亲手导出一份默认配置，直观感受「三层配置」的内容差异。

**操作步骤**：

1. 假设你已按 [u1-l2](u1-l2-repo-and-build.md) 编译出 `build/bin/simple_example`（若无，则跳到步骤 3 改为源码阅读）。
2. 分别执行：

   ```bash
   ./build/bin/simple_example --dump_default_launcher_cfg
   ./build/bin/simple_example --dump_default_app_cfg
   ./build/bin/simple_example --dump_default_cfg
   ```

3. 对照 [ServerLauncherConfig.h:9-14](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/core/app/ServerLauncherConfig.h#L9-L14) 与 [ServerAppConfig.h:9-10](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/core/app/ServerAppConfig.h#L9-L10)，确认 dump 出来的字段与源码里的 `CONFIG_ITEM/CONFIG_OBJ` 一一对应。
4. 在 [ServerLauncher.h:35-36](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/core/app/ServerLauncher.h#L35-L36) 确认这两个 dump 标志分别由 `appConfig_.init` / `launcherConfig_.init` 处理；在 [TwoPhaseApplication.h:37-40](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/TwoPhaseApplication.h#L37-L40) 确认 `--dump_default_cfg` 打印的是合并后的 `common+server` 配置。

**需要观察的现象**：

- `--dump_default_launcher_cfg` 的输出里能找到 `cluster_id`、`ib_devices`、`mgmtd_client`（含 mgmtd 地址）——对应 `*_launcher.toml`。
- `--dump_default_app_cfg` 的输出里只有 `node_id`、`allow_empty_node_id`——对应 `*_app.toml`。
- `--dump_default_cfg` 的输出包含 `common`（日志/监控/内存）和 `server`（网络服务）两大块——对应 `*_main.toml`。

**预期结果**：你能清楚说出「三层配置分别由哪三个标志 dump、分别对应哪个结构体」。如果本地无法编译运行，请在源码中阅读上述三个 `CONFIG_*` 声明，并标注「待本地验证」。

> 说明：dump 出来后进程会立即 `exit(0)`，不会真正启动服务，所以这是一个安全、零副作用的探索手段。

#### 4.3.5 小练习与答案

**练习 1**：如果一个服务用 `--cfg my_local_main.toml --use_local_cfg` 启动，mgmtd 还能热推送配置吗？为什么？

**参考答案**：不能。因为 [TwoPhaseApplication.h:86](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/TwoPhaseApplication.h#L86) 的 `configPushable()` 返回 `FLAGS_cfg.empty() && !FLAGS_use_local_cfg`，指定了 `--cfg` 或 `--use_local_cfg` 就返回 false。此时 `updateConfig()` 会被 `configPushable()` 判定拒绝（[ApplicationBase.cc:121-127](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ApplicationBase.cc#L121-L127)），返回 `kCannotPushConfig`。这是为了尊重运维「显式本地优先」的选择。

**练习 2**：`beforeStart()` 里为什么要调用 `mgmtdClient_->refreshRoutingInfo(false)`，并且失败就 FATAL？

**参考答案**：因为服务在正式对外提供服务前，必须先知道集群的路由信息（哪些 storage target、哪些 chain），否则后续 RPC 无法正确路由。`refreshRoutingInfo(false)` 的 `false` 表示「不强制刷新」（先用缓存），但首次启动缓存为空，所以会真正向 mgmtd 拉一次。拉不到就说明连 mgmtd 都有问题，启动没有意义，故 FATAL。

**练习 3**：`ServerEnv` 和 `ServerLauncher` 都持有 `AppInfo`，它们有什么区别？

**参考答案**：`ServerLauncher` 持有的 `AppInfo` 是引导阶段「拼装中」的身份，引导结束（`launcher_.reset()`）后就不存在了。`ServerEnv` 持有的是「运行期长期存在」的身份，供服务内部组件在运行期反复读取。两者体现了「引导期 vs 运行期」的资源生命周期分离。

---

## 5. 综合实践

**任务**：对照 `simple_example/main.cpp`，画出 `meta`、`storage`、`mgmtd` 三个服务的**统一**启动时序图，并标注 `beforeStart` / 启动 / 停止三个阶段。

**背景**：本讲已经证明三个服务的 `main` 结构完全一致，只差模板参数。本任务要求你把这套「统一时序」固化成一张图，作为后续阅读各服务具体实现的地图。

**操作步骤**：

1. 并排打开三个入口文件：[meta/meta.cpp:5-8](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/meta.cpp#L5-L8)、[storage/storage.cpp:5-8](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/storage.cpp#L5-L8)、[mgmtd/mgmtd.cpp:5-8](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/mgmtd.cpp#L5-L8)，确认它们都是 `TwoPhaseApplication<XxxServer>().run(argc, argv)`。
2. 以 [ApplicationBase.cc:49-74](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ApplicationBase.cc#L49-L74) 为骨架，画出：`parseFlags → folly::init → initApplication → mainLoop → stop`。
3. 展开 `initApplication`（[TwoPhaseApplication.h:36-70](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/TwoPhaseApplication.h#L36-L70)），标注 `launcher_->init → loadAppInfo → initConfig → initCommonComponents → initServer(setup) → startServer`。
4. 展开 `startServer → server.start(appInfo)`（[Server.cc:35-46](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Server.cc#L35-L46)），在图上明确标出：
   - **beforeStart 阶段**：`beforeStart()` 被调用（创建客户端、注册服务）。
   - **启动阶段**：各 `ServiceGroup::start()`（真正监听端口）→ `afterStart()`。
5. 展开 `stop → stopAndJoin(server)`（[Server.cc:48-64](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Server.cc#L48-L64) 与 [ApplicationBase.cc:275-283](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ApplicationBase.cc#L275-L283)），标注：
   - **停止阶段**：`beforeStop()` → 各 `ServiceGroup::stopAndJoin()` → `afterStop()` → 关闭 `tpg`/`independentTpg` → 关 `monitor` → 关 `IBManager`。
6. 在图上用三种颜色/图例区分三个阶段，并在 `launcher_.reset()`（[TwoPhaseApplication.h:67](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/TwoPhaseApplication.h#L67)）处画一条「阶段一/阶段二」分界线。

**预期产出**：一张时序图，外层是 `run()` 的五步，中层是 `initApplication` 的装配序列，内层是 `net::Server` 的 `beforeStart/start/beforeStop/stopAndJoin` 钩子序列。图旁附一句话：「meta/storage/mgmtd 三服务的唯一差异是模板参数 `Server` 及其 `beforeStart` 内的具体业务逻辑；骨架完全相同。」

**进阶（可选）**：挑一个真实服务（如 `storage`），找到它的 `beforeStart` 实现（在 `src/storage/service/StorageServer.h` 中），对比 `simple_example` 的 `beforeStart`，指出它额外做了哪些 storage 特有的初始化（如打开本地 SSD、创建 StorageTarget）。这能为 [u5-l1](u5-l1-storage-overview.md) 埋下伏笔。

## 6. 本讲小结

- 3FS 所有服务共用同一套两阶段启动骨架：`main` 只有一行 `TwoPhaseApplication<Server>().run(argc, argv)`，meta/storage/mgmtd 的入口仅模板参数不同。
- **阶段一（引导）** 由 `ServerLauncher` 主导：加载 `*_app.toml`/`*_launcher.toml`、启动 IB、联系 mgmtd 拼 `AppInfo` 并拉取运行时配置模板；引导完成后 `launcher_.reset()` 销毁启动器。
- **阶段二（运行）** 由 `net::Server` 主导：`setup()` 绑定端口 → `start()` 依次调用 `beforeStart` → 各 ServiceGroup 启动 → `afterStart`，然后 `mainLoop()` 阻塞等待信号。
- 优雅停止与启动严格对称：`stopAndJoin` 依次走 `beforeStop` → 各 ServiceGroup 停止 → `afterStop`，再关闭线程池、monitor、IB。
- 三层配置（launcher/app/main）分别对应 `ServerLauncherConfig`/`ServerAppConfig`/`TwoPhaseApplication::Config`；`configPushable()` 用 `--cfg`/`--use_local_cfg` 两个标志控制是否允许 mgmtd 热推送。
- `beforeStart` 是各服务插入业务初始化（建客户端、刷路由、注册 RPC 服务）的标准位置；`simple_example` 是理解这一模式的最小样本。

## 7. 下一步学习建议

本讲建立了「服务如何启动」的骨架认知。建议接下来按以下顺序深入：

1. **[u2-l2 RPC 与序列化框架](u2-l2-rpc-and-serde.md)**：本讲提到 `addSerdeService` 注册 RPC 服务，下一讲就讲清楚 Service/CallContext/FlatBuffers 这套 RPC 抽象是怎么工作的。
2. **[u2-l5 配置系统与热更新](u2-l5-config-system.md)**：本讲只点了 `CONFIG_ITEM`/`CONFIG_OBJ` 宏和 `configPushable`，下一讲会展开 `ConfigBase` 的声明式配置与热更新回调机制。
3. **[u2-l3 协程、线程池与后台任务](u2-l3-coroutine-and-pools.md)**：`beforeStart` 里出现的 `folly::coro::blockingWait`、`tpg().bgThreadPool()` 都依赖协程与线程池，下一讲系统讲解。
4. **进入具体服务**：有了骨架，再去读 [u3-l1 mgmtd 服务总览](u3-l1-mgmtd-overview.md)、[u4-l1 meta 服务总览](u4-l1-meta-overview.md)、[u5-l1 storage 服务总览](u5-l1-storage-overview.md) 时，可以重点对比它们的 `beforeStart` 各自做了什么特有事——你会发现「骨架相同、血肉不同」。
