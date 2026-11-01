# Gopeed 是什么：项目定位与总体架构

> 所属单元：u1 初识 Gopeed（第 1 讲 / 共 4 讲） · 难度：入门 · 无前置讲义

## 1. 本讲目标

读完本讲，你应该能够：

1. 用一两句话说清 Gopeed 是什么、支持哪些下载协议、有哪些产品形态。
2. 理解「一个 Go 核心 + 多种外壳」的架构思想：桌面、移动、Web、CLI 四种形态如何复用同一份 Go 代码。
3. 理解 Flutter 前端与 Go 后端的职责分工，以及它们之间的三条通信通道（桌面 FFI、移动 gomobile、Web REST）。
4. 在仓库中快速定位三类代码：核心逻辑、桥接层、前端界面。

本讲不要求你读懂任何 Go 或 Dart 代码细节——我们只建立地图，行军路线留给后面的讲义。

## 2. 前置知识

本讲需要的概念都很轻量， unfamiliar 的术语在这里一站式解释：

- **下载管理器（Download Manager）**：一类专门负责「把文件从网上搬下来」的软件，核心诉求是快（多连接并行）、稳（断点续传）、全（支持多种协议）。Gopeed 就是一个下载管理器，对标 IDM、Free Download Manager 等。
- **HTTP / HTTPS**：最常见的网页传输协议，下载普通直链文件（比如 `https://example.com/big.zip`）用它。服务器支持 Range 请求时可以把文件切成多段并行下载。
- **BitTorrent（BT）与磁力链接**：P2P 下载协议。`.torrent` 种子文件描述了文件清单和分片哈希；磁力链接（`magnet:?xt=urn:btih:...`）是一种「不带文件清单的种子」，需要先从网络获取元数据。下载的同时也在上传（做种）。
- **ed2k**：eDonkey2000 网络（电驴体系）的链接格式，形如 `ed2k://|file|名称|大小|哈希|/`。
- **Go**：Google 开发的编译型语言，特点是并发模型轻量（goroutine）、交叉编译方便、单文件部署。Gopeed 的全部核心逻辑用 Go 编写。
- **Flutter / Dart**：Google 的跨平台 UI 框架，一套 Dart 代码渲染出 Windows/macOS/Linux/Android/iOS/Web 上的原生界面。Gopeed 的全部界面用 Flutter 编写。
- **FFI（Foreign Function Interface）**：不同语言编写的代码互相调用的机制。Gopeed 把 Go 编译成 C 风格动态库（`.dll`/`.dylib`/`.so`），Dart 通过 FFI 直接调用里面的函数——进程内调用，不经过网络。
- **gomobile**：Go 官方工具，把 Go 代码打包成 Android 的 `.aar` 和 iOS 的 `.xcframework`，供 Kotlin/Swift 调用。
- **REST API**：基于 HTTP 的接口风格。Gopeed 的 Web 端和外部程序通过 `http://host:9999/api/v1/...` 这样的 HTTP 接口操作下载器。
- **MCP（Model Context Protocol）**：让 AI Agent 调用外部工具的开放协议。Gopeed 内置 MCP 服务器，AI 可以用自然语言帮你建任务。
- **bbolt**：Go 编写的嵌入式键值数据库（单文件存储），Gopeed 用它持久化任务与配置。

## 3. 本讲源码地图

本讲涉及的关键文件（全部建议通读，都很短）：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README.md) | 项目门面：功能清单、协议支持、各平台构建命令 |
| [README_zh-CN.md](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README_zh-CN.md) | 中文版 README，内容与英文版对齐 |
| [CONTRIBUTING.md](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/CONTRIBUTING.md) | 贡献指南，包含本地开发的推荐姿势（起后端 + Flutter debug） |
| [ui/flutter/docs/backend-transport-architecture.md](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/ui/flutter/docs/backend-transport-architecture.md) | **2.0 架构的权威文档**：Go–Flutter 进程内 API 架构设计 |
| [cmd/gopeed/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go) | CLI 入口：把 Gopeed 当 Go 库使用的活例子 |
| [bind/desktop/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/bind/desktop/main.go) | 桌面桥接层：Go 导出给 Dart FFI 的 C 接口 |
| [pkg/download/downloader.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go) | Go 核心中的核心：Downloader 与 Boot 链式 API |
| [pkg/api/service.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/api/service.go) | 与传输无关的服务层：所有接口的唯一业务实现 |

顺带认识几个目录名，后面反复出现：`cmd/`（多个可执行入口）、`bind/`（桥接层）、`pkg/`（对外核心包）、`internal/`（内部实现）、`ui/flutter/`（前端）。

## 4. 核心概念与源码讲解

本讲的三个最小模块：**① README 功能与协议清单 → ② Go 核心 + Flutter 外壳架构 → ③ bind 桥接层概览**。

### 4.1 README 功能与协议清单

#### 4.1.1 概念说明

读开源项目的第一课永远是读 README——它是最「官方」的功能承诺清单。Gopeed 的 README 开头一句话完成定位：

> Gopeed (short for **Go Speed**) is a fast, modern, free, and open-source download manager built with Go and Flutter.

拆开看有三个关键信息：**快**（多连接、P2P）、**技术栈**（Go 写核心 + Flutter 写界面）、**开源免费**（GPLv3）。

#### 4.1.2 核心流程

不需要理解执行流程，这里只需建立一个「能力清单」的印象。按 README 的表述，Gopeed 的能力可以归为四层：

```text
协议层    HTTP / HTTPS / BitTorrent / 磁力链接 / ed2k（仓库内部还有 HLS、FTP 实现）
速度层    多任务并发、HTTP 多连接分段、BT P2P、断点续传
管理层    暂停/续传/重试/批量/搜索/分类、重启后任务恢复
扩展层    JS 扩展、浏览器接管、REST API、CLI、MCP、Webhook、自动解压
```

其中协议层是后续单元的主角：仓库里 `internal/protocol/` 下每个子目录（`http`、`bt`、`hls`、`ftp`、`ed2k`）就是一种协议的实现。

#### 4.1.3 源码精读

README 开头的项目介绍——注意协议清单里只列了五个，但仓库实际实现了更多（HLS、FTP 是后来加的，README 尚未列入，这本身就是「文档滞后于代码」的例子）：

- [README.md:L17-L19](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README.md#L17-L19) —— 用一句话给出项目定位（Go + Flutter 下载器）、支持的协议（HTTP/HTTPS/BT/磁力/ed2k）与覆盖平台（桌面、移动、Web），并点出浏览器集成、JS 扩展、REST API、CLI、自托管 Web UI 这些进阶形态。

功能特性清单（中文版对应 [README_zh-CN.md:L27-L40](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README_zh-CN.md#L27-L40)）：

- [README.md:L25-L40](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README.md#L25-L40) —— 15 条特性，逐条读完你会得到一张完整的「Gopeed 能干什么」地图。几条值得划重点：
  - **L27-L29**：高速下载的三板斧——并发任务、HTTP 多连接、BT P2P；以及完整 BT 能力（DHT、uTP、Web Seed、按文件选择、Tracker 管理、做种限制）。
  - **L31**：**「非 Electron、非 WebView 套壳」**——界面由 Flutter 原生渲染，这是项目的一个核心卖点，也解释了为什么架构是「Go 核心 + Flutter 壳」而不是「Go 服务 + 网页壳」。
  - **L37-L39**：三条开放路径——JS 扩展（扩展下载源）、MCP（AI 管理）、REST/CLI/Webhook/脚本（自动化）。

2.0 版本说明，一句话预告了本讲后面要讲的架构：

- [README.md:L62-L68](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README.md#L62-L68) —— Gopeed 2.0.0 处于公开 Beta，重点提到「通过 FFI 直连 Go 核心的原生通信架构」和「MCP 接入」。**你即将阅读的这套源码就是 2.0 架构。**

CLI 的安装方式只有一行，说明核心对「被当作 Go 库使用」有第一等支持：

- [README.md:L75-L81](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README.md#L75-L81) —— `go install github.com/GopeedLab/gopeed/cmd/gopeed@latest` 直接装上命令行版；没有 npm/pip 之类的运行时依赖，这是 Go 单二进制分发的好处。

#### 4.1.4 代码实践

**实践：把 README 的「承诺」翻译成一张协议 × 能力矩阵**

1. **实践目标**：把 README 的文字描述结构化，形成你自己的功能索引，后面每学一个模块就回来打一个勾。
2. **操作步骤**：
   - 通读 [README.md:L25-L40](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README.md#L25-L40) 与 [README_zh-CN.md:L27-L40](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README_zh-CN.md#L27-L40)。
   - 建一张表，行为五种协议（HTTP/HTTPS、BT、磁力、ed2k，外加你在 `internal/protocol/` 目录里多发现的协议），列为能力（多连接、断点续传、按文件选择、做种/上传、需要解析元数据）。
   - 用 `ls internal/protocol/` 核对仓库里实际有哪些协议实现目录，把 README 没写但代码里有的协议补进表里（提示：`hls`、`ftp`）。
3. **需要观察的现象**：README 列出的协议集合与 `internal/protocol/` 目录集合并不完全一致。
4. **预期结果**：得到一张 7 行左右的矩阵表，并能说出「HLS 和 FTP 在代码里有、README 没写」——文档滞后是常态，以代码为准是源码阅读的基本素养。
5. 本实践为纯阅读任务，无需运行结果；`ls` 命令的输出即验证依据。

#### 4.1.5 小练习与答案

**练习 1**：README 说 Gopeed「非 Electron、非 WebView 套壳」，这句话和它的技术栈选型有什么关系？

**参考答案**：Electron/WebView 套壳方案的界面是网页，宿主是一个捆绑了浏览器内核的运行时；Gopeed 的界面用 Flutter 原生渲染，后端逻辑在同进程内用 Go 编写并通过 FFI/gomobile 直连，因此安装包更小、内存开销更低、界面响应不经过 HTML/CSS 排版管线。这句话本质上是在强调「原生渲染」这个卖点。

**练习 2**：只看 README 的 AI 接入一节，MCP 提供的 `resolve_task` 和 `create_task` 为什么要拆成两个工具，而不是合成一个 `download`？

**参考答案**：`resolve_task` 先解析 URL 得到资源元数据（文件名、大小、文件列表）但不建任务，`create_task` 再用解析结果或直接请求建任务。拆开的目的是让调用方（AI 或程序）有机会在「看到资源信息」之后做决策——比如让用户选择要下载种子里的哪些文件、确认文件名，然后再创建任务。这是「先解析、后创建」的两段式设计，后面的 Downloader 主链路讲义（u4-l2）会看到对应的 `Resolve` 与 `Create` 两个方法。

**练习 3**：`go install github.com/GopeedLab/gopeed/cmd/gopeed@latest` 安装的是整个项目吗？

**参考答案**：不是。它只编译并安装 `cmd/gopeed` 这一个入口（CLI），产出单个可执行文件。Go 项目里 `cmd/` 下每个子目录都是一个独立的 `main` 包；Gopeed 还有 `cmd/api`、`cmd/web` 等其他入口，它们都链接同一份核心库代码——这正是「一个核心、多种外壳」的体现。

### 4.2 Go 核心 + Flutter 外壳架构

#### 4.2.1 概念说明

这是本讲最重要的心智模型，一句话：

> **Gopeed = 一个用 Go 写的下载引擎核心（不依赖任何界面），外面套了若干种「外壳」去使用它。**

Go 核心负责所有「重活」：协议解析、多连接调度、任务生命周期、断点续传、持久化（bbolt）、JS 扩展引擎、REST/MCP 服务。外壳只负责交互：Flutter 界面、命令行参数、HTTP 请求。

为什么这样设计？因为「下载」这件事的本质与界面无关。把核心做成纯库后，同一份代码可以同时支撑：

| 产品形态 | 外壳 | 复用的部分 |
| --- | --- | --- |
| Windows/macOS/Linux 桌面版 | Flutter 桌面应用 | 全部核心（编译成动态库经 FFI 调用） |
| Android/iOS 移动版 | Flutter 移动应用 | 全部核心（gomobile 打包成 aar/xcframework） |
| Web 版 | Flutter Web + Go 服务器 | 全部核心（经 REST HTTP 调用） |
| CLI | 纯 Go 命令行 | 全部核心（直接进程内函数调用） |
| 作为 Go 库 | 你的程序 | 全部核心（`import` 即用） |

新增一种协议或修一个下载 bug，五种形态全部受益——这就是「核心与外壳分离」的回报。

#### 4.2.2 核心流程

三种外壳与核心的三条通道（来自架构文档的平台策略表）：

```text
Windows / macOS / Linux     Flutter ──Dart FFI──▶  Go 动态库（进程内，无网络）
Android / iOS               Flutter ──MethodChannel + gomobile──▶  Go（进程内，无网络）
Web                         Flutter Web ──REST(HTTP)──▶  Go 服务器（跨进程，走网络）
```

把三条通道汇总成一张分层图（对应架构文档中的总体架构图）：

```text
        Flutter Widget / Riverpod Controller          ← 外壳（界面，Dart）
                      │
                 GopeedClient                        ← 类型化 API 客户端（业务方法只写一次）
                      │
                GopeedTransport                       ← 传输抽象接口
        ┌─────────────┼─────────────┐
   Desktop FFI    Mobile gomobile   Web REST          ← 三条通道
   Transport      Transport         Transport
        └─────────────┼─────────────┘
                      │
              API Dispatcher                          ← 统一入口：method/path/query/body
                      │
        Typed Application Service                     ← 业务唯一实现（pkg/api/service.go）
                      │
      Downloader / Storage / Events                   ← 下载核心（pkg/download）
                      │
        Optional REST Server Adapter                  ← 可选的 REST 服务器
```

关键点：**三条通道最终都汇入同一个 Dispatcher 和同一个 Service**。也就是说，无论请求从 FFI、gomobile 还是 HTTP 进来，业务逻辑只执行一份代码。Dispatcher 收发的不是 HTTP 对象，而是一个归一化的请求结构：

```json
{ "method": "GET", "path": "/api/v1/tasks", "query": "status=running", "body": "" }
```

它复用了 REST 的路径契约作为「进程内请求格式」，但完全不启动 HTTP、不经过 `net/http`——所以原生端不需要监听端口，也就没有端口占用、token 引导、连接回退这些麻烦。

Go 后端内部再分一层职责：

```text
Runtime            进程内单例生命周期：创建/关闭 Downloader、独立启停 REST 服务器
ApplicationService 业务唯一实现层（Resolve/CreateTask/GetTasks/GetConfig…）
Dispatcher         显式注册的路由表（RouteSpec: Method + Path + Handler）
TaskEventCallback  任务事件回调（done/error 推送给原生端）
Storage            bbolt 持久化（任务、配置、扩展数据）
RestServer(可选)   仅传输职责：token/登录/CORS/静态资源
```

#### 4.2.3 源码精读

架构文档开篇的「背景」一节解释了为什么要做这次架构改造——先读问题再读方案，理解会深得多：

- [ui/flutter/docs/backend-transport-architecture.md:L3-L13](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/ui/flutter/docs/backend-transport-architecture.md#L3-L13) —— 列出旧架构（原生端也走 REST over socket）的五个痛点：要管理监听端口/Unix socket/token、REST 服务器生命周期与 Downloader 耦合、Flutter 只能轮询拿不到事件推送、双持久化系统（Hive + Bolt）、每加一个 API 要改三处。

平台策略表——本讲最重要的一张表：

- [ui/flutter/docs/backend-transport-architecture.md:L19-L25](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/ui/flutter/docs/backend-transport-architecture.md#L19-L25) —— 明确三种平台分别用哪条通道：桌面用 Dart FFI、移动用 MethodChannel + gomobile、Web 用 REST（必须）。注意最后一列：原生端的本地 REST 服务器**默认关闭**，可单独开启。

总体架构图（就是上面那张分层图的原文）：

- [ui/flutter/docs/backend-transport-architecture.md:L29-L49](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/ui/flutter/docs/backend-transport-architecture.md#L29-L49) —— 从 Flutter Widget 一路向下到 Downloader/Storage/Events，再到底部「可选的 REST 服务器适配器」。看懂这张图，本讲的目标就完成了一半。

Go 后端的分层模型与 Dispatcher 的归一化请求/响应格式：

- [ui/flutter/docs/backend-transport-architecture.md:L83-L107](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/ui/flutter/docs/backend-transport-architecture.md#L83-L107) —— Runtime/ApplicationService/Dispatcher/Storage/RestServer 各自的职责边界；特别强调 Service 方法不得依赖 `http.Request`、`http.ResponseWriter` 这类 HTTP 概念，只接受业务 DTO。
- [ui/flutter/docs/backend-transport-architecture.md:L135-L169](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/ui/flutter/docs/backend-transport-architecture.md#L135-L169) —— RouteSpec 显式注册（禁止 `reflect.MethodByName` 反射路由），以及 Dispatcher 的 `method/path/query/body` 归一化输入和 `{statusCode, body:{code, data}}` 归一化输出。

这套设计不是空中楼阁，落地的代码就在核心包里。看三个「锚点」，后面的讲义会逐一深入：

- [pkg/download/downloader.go:L140](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L140) —— `NewDownloader(cfg *DownloaderConfig)`：下载核心的构造函数，核心只是一个普通 Go 结构体，谁都能创建——库形态的基础。
- [pkg/download/downloader.go:L2799](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2799) —— `Boot()`：给库使用者准备的链式 API 入口（下一小节在 CLI 里能看到它被链式调用）。
- [pkg/api/service.go:L40-L46](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/api/service.go#L40-L46) —— `Service` 结构体与 `NewService(downloader)`：架构文档里 ApplicationService 的真身，注意它只持有一个 `Downloader`，没有任何 HTTP 成员——「与传输无关」是字面意思。
- [pkg/api/service.go:L92-L121](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/api/service.go#L92-L121) —— `Dispatch(...)` 与 `JSON(...)`：Dispatcher 的进程内入口。REST 适配器、FFI、gomobile、测试代码都调用它，业务逻辑只此一份。

还有一个容易踩的坑要提前指出——README 的 Development 一节写着「前后端通过 http 协议通讯，unix 系统用 unix socket，windows 用 tcp」：

- [README.md:L97-L101](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README.md#L97-L101) —— 这是 1.x 时代的通信方式（也是当前 Web 形态仍然在用的方式）。
- 对照 [README.md:L64](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README.md#L64) 的 2.0 说明和架构文档，可知**原生端已改为 FFI/gomobile 进程内直连，socket 方案仅剩 Web 形态使用**。两份文档各自都对，只是描述的版本不同——交叉验证是读文档的基本功。

本地开发姿势（来自贡献指南，说明前后端可以完全独立调试）：

- [CONTRIBUTING.md:L12-L16](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/CONTRIBUTING.md#L12-L16) —— 推荐用 Web 形态开发：先 `go run cmd/api/main.go` 起后端（默认端口 9999），再以 debug 模式起 Flutter。这正是「核心与外壳分离」给开发者带来的便利。

#### 4.2.4 代码实践

**实践：在源码里验证「三条通道、一份业务」**

1. **实践目标**：不靠背诵，用代码证据确认架构文档的分层真实存在。
2. **操作步骤**：
   - 打开 [pkg/api/service.go:L92](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/api/service.go#L92)，记下 `Dispatch` 的签名。
   - 全局搜索 `Dispatch(`（IDE 里 Ctrl+Shift+F，或命令行 `grep -rn "\.Dispatch(" --include="*.go" .`），统计有哪些包调用了它。预期至少能看到 `pkg/rest`（REST 适配器）与 `bind`（原生桥接）两类调用方。
   - 打开 [pkg/download/downloader.go:L2799](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2799) 附近的 `Boot()`，确认它不经过任何网络，直接返回一个构建器对象。
3. **需要观察的现象**：`Dispatch` 的调用方分散在多个不同传输相关的包里，但它的实现只有一处；`Boot()` 返回的对象上只有链式设置方法。
4. **预期结果**：你能列出至少 2 个 `Dispatch` 的调用方包，并说出「REST、FFI、gomobile 都只是 Dispatcher 的适配器」。搜索结果即为验证，无需运行程序。
5. 若你本地没有 Go 环境，`grep` 步骤可用 GitHub 网页搜索代替；结论本身不依赖运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Web 端「必须」用 REST，而桌面/移动端「可以不用」？

**参考答案**：Flutter Web 运行在浏览器沙箱里，编译产物是 JS/WASM，无法加载 C 动态库（FFI 不可用），也无法嵌入 gomobile 产物；浏览器与 Go 服务器天然是两个进程，HTTP 是唯一通道。桌面/移动端则是 Flutter 与 Go 编译进同一个进程，函数调用即可，无需网络传输，也就免掉了端口管理、token、连接回退等问题。

**练习 2**：架构文档规定 ApplicationService 的方法「不得依赖 `http.Request`/`http.ResponseWriter`/HTTP 头」。如果违反这条规定，最先遭殃的是哪种产品形态？

**参考答案**：桌面和移动端。因为 FFI/gomobile 通道没有 HTTP 上下文，一个依赖 `http.Request` 的业务方法在原生端根本无从调用；REST 端反而能凑合（包一层假请求）。这条规定的本质是把「传输概念」挡在业务层之外，保证业务对所有通道中立。

**练习 3**：架构文档提到共享执行器的并发度是 \(\text{workers} = \min(\max(2 \times \text{GOMAXPROCS},\ 4),\ 32)\)。为什么做成有界池而不是来一个请求开一个 goroutine？

**参考答案**：Go 的 goroutine 虽轻，但每个在途请求背后可能持有内存、文件句柄和锁竞争；FFI 回调与移动端 MethodChannel 的吞吐也有限。有界池 + FIFO 排队把并发压在可控范围内，池满表现为「背压」（排队等待）而不是错误或内存暴涨，这对嵌入式场景（手机、桌面）更安全。

### 4.3 bind 桥接层概览

#### 4.3.1 概念说明

「桥接层」是连接 Dart 世界和 Go 世界的翻译官。仓库里 `bind/` 目录下有三个子目录，职责分明：

| 目录 | 产物 | 服务的平台 |
| --- | --- | --- |
| `bind/desktop` | C 动态库（`.dll`/`.dylib`/`.so`） | Windows / macOS / Linux |
| `bind/mobile` | `.aar` / `.xcframework` | Android / iOS |
| `bind/native` | （Go 包，被上面两者共享） | 全部原生端 |

它们导出的函数集合刻意保持**小而稳定**：启动/停止运行时、管理可选的 REST 服务器、以及一个万能的 `InvokeAsync` 入口。业务接口不是一个个导出的——所有业务调用都序列化成 `method/path/query/body` 字符串，从 `InvokeAsync` 这一个门进去。这样每加一个 API，桥接层**零改动**。

#### 4.3.2 核心流程

桌面端一次业务调用的完整旅程（以「获取任务列表」为例）：

```text
Dart: gopeedClient.getTasks(...)
  → 序列化为 {method:"GET", path:"/api/v1/tasks", query:"status=running", body:""}
  → Dart FFI 调用 C 导出函数 InvokeAsync(字符串, request_id, 回调指针)
  → Go: 解析 JSON → Service.Dispatch(...) → 业务执行 → 结果 JSON
  → Go 通过 C 回调指针把结果送回 Dart（NativeCallable.listener）
  → Dart: 解码 JSON → 补全 Future → 调用 FreeCString 释放 Go 分配的字符串
```

注意两个工程细节：

1. **内存归属**：Go 分配的 C 字符串必须由 Dart 侧调用 `FreeCString` 显式释放，否则跨语言边界会泄漏。
2. **异步回调**：`InvokeAsync` 立即返回不阻塞 UI 线程，结果通过回调送达；移动端则由 gomobile listener 补全挂起的 MethodChannel 调用。

#### 4.3.3 源码精读

桌面桥接层如何用 cgo 导出 C 接口——注意文件头那段 C 代码定义的只是两个「回调转发器」：

- [bind/desktop/main.go:L3-L19](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/bind/desktop/main.go#L3-L19) —— `import "C"` 之前的注释块就是 C 源码：定义 `TaskEventCallback`/`InvokeResultCallback` 两个函数指针类型，以及把 Go 传来的指针转回 C 函数调用的两个静态包装函数。这是 cgo 的标准做法：Go 不能直接持有 C 函数指针，需要一个 C 层中转。
- [bind/desktop/main.go:L32-L47](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/bind/desktop/main.go#L32-L47) —— `//export Start` 导出的启动函数：把 Dart 传来的 JSON 配置反序列化为 `StartConfig`，标记生产模式与原生模式，然后调用 `rest.Start` 启动核心，最后 `nativebridge.ResumeInvokes()` 恢复请求池。整个函数不到 15 行——**桥接层不含业务逻辑，只做翻译**。

导出清单与架构文档的对照（文档与代码一一对应）：

- [ui/flutter/docs/backend-transport-architecture.md:L251-L263](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/ui/flutter/docs/backend-transport-architecture.md#L251-L263) —— 桌面 C ABI 的完整清单：`Start`/`Stop`/`GetAPIServerState`/`StartAPIServer`/`StopAPIServer`/`RestartAPIServer`/`InvokeAsync`/`FreeCString`/`SubscribeTaskEvents`，共 9 个。其中业务入口只有 `InvokeAsync` 一个。
- [ui/flutter/docs/backend-transport-architecture.md:L265-L276](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/ui/flutter/docs/backend-transport-architecture.md#L265-L276) —— 桥接的三条硬规则：输入字符串在 `InvokeAsync` 返回前必须复制完；Go 侧回调载荷在 Dart 复制并调 `FreeCString` 前保持有效；业务错误不使用独立的 ABI 错误模型，`InvokeAsync` 回调返回的 JSON 与 REST 完全一致。
- [ui/flutter/docs/backend-transport-architecture.md:L283-L291](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/ui/flutter/docs/backend-transport-architecture.md#L283-L291) —— 移动端 gomobile 导出的同构清单（参数形态不同：`InvokeAsync(method, path, query, body, requestID, listener)`）。桌面与移动共享 `bind/native` 里的有界并发执行器。

CLI 是另一种「桥」的极端——没有桥，直接调：

- [cmd/gopeed/main.go:L34-L60](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go#L34-L60) —— 命令行入口用 `download.Boot().URL(...).Listener(回调).Create(&base.Options{...})` 的链式调用一步建任务：设置 URL、注册事件监听（进度事件刷终端、finally 事件收尾）、传下载路径与 HTTP 连接数。这就是「把 Gopeed 当 Go 库用」的最小样板——没有 FFI 也没有 REST，纯进程内函数调用。

最后看两份构建产物如何落地（README 的构建命令印证了桥接层的产物形态）：

- [README.md:L129-L148](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README.md#L129-L148) —— 三种桌面平台的构建：先用 `go build -buildmode=c-shared` 把 `bind/desktop` 编译成 `libgopeed.dll`/`libgopeed.dylib`/`libgopeed.so` 放进 Flutter 工程对应目录，再 `flutter build`。`-tags nosqlite` 表示不启用 sqlite 存储（用 bbolt）。
- [README.md:L165-L189](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README.md#L165-L189) —— 移动端用 `gomobile bind` 打包 `bind/mobile`；Web 端先 `flutter build web`，把产物拷进 `cmd/web/dist`，再用 `-tags nosqlite,web` 构建 Go 服务器——`web` 标签激活把前端嵌入二进制的逻辑。

#### 4.3.4 代码实践

**实践：数一数桥接层导出了什么，验证「业务入口只有 InvokeAsync」**

1. **实践目标**：亲手确认原生 ABI 的「小而稳定」，并理解为什么加 API 不需要动桥接层。
2. **操作步骤**：
   - 在 [bind/desktop/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/bind/desktop/main.go) 中搜索 `//export`，列出全部导出函数。
   - 对照 [ui/flutter/docs/backend-transport-architecture.md:L251-L263](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/ui/flutter/docs/backend-transport-architecture.md#L251-L263) 的清单，核对两边是否一致（以代码为准，文档可能滞后）。
   - 对每个导出函数分类：生命周期类（Start/Stop…）、REST 服务器管理类、业务调用类、内存管理类、事件订阅类。
3. **需要观察的现象**：导出函数总数不到 10 个，其中能发起任意业务请求的只有 `InvokeAsync` 一个；其余都是固定用途。
4. **预期结果**：得到一张「导出函数 × 分类 × 用途」小表，并能回答：**为什么 Gopeed 加一个 `GET /api/v1/tasks/{id}/peers` 接口不需要重新编译桌面动态库？**（因为请求只是 `InvokeAsync` 的一个字符串参数，业务在 Go 侧的 Service 里实现。）
5. 本实践为源码阅读型，搜索 `//export` 的结果即为验证依据，无需编译运行。

#### 4.3.5 小练习与答案

**练习 1**：`bind/desktop`、`bind/mobile`、`bind/native` 三个包，为什么前两个不能合并成一个？

**参考答案**：它们的产物形态由平台工具链决定，无法统一：桌面需要 cgo 的 `c-shared` 动态库 + C 函数指针回调；移动端需要 gomobile 的 `.aar`/`.xcframework` + listener 接口回调。两者的 ABI 约定、内存管理、线程模型都不同。但两者共享 `bind/native` 里的请求执行器等纯 Go 逻辑，所以是「两个薄适配器 + 一个共享内核」的结构。

**练习 2**：CLI（`cmd/gopeed`）和桌面版用的核心代码相同吗？它们的差别在哪一层？

**参考答案**：完全相同。两者都直接链接 `pkg/download` 等核心包，差别只在外壳层：CLI 用命令行参数 + 终端进度条（见 `cmd/gopeed/main.go` 的 `printProgress`），桌面版用 Flutter 界面 + FFI 桥（`bind/desktop`）。核心对两者一视同仁。

**练习 3**：架构文档说 `InvokeAsync` 的回调返回「与 REST 相同的 Result JSON」，业务错误不单独设计 ABI 错误码。这样设计的好处是什么？

**参考答案**：统一错误模型让三条通道（REST/FFI/gomobile）的客户端可以用同一套解析逻辑，Flutter 侧把所有错误统一转成 `GopeedException`；同时也让「共享请求夹具」的测试成为可能——同一份测试输入可以验证 Dispatcher 结果与 REST 结果完全一致。如果原生端有独立错误模型，每条通道都要维护一套错误翻译，出错的面就变大了。

## 5. 综合实践

**手绘 Gopeed 总体架构图，并标注四种产品形态的复用范围**（本讲唯一必做作业，建议耗时 30–45 分钟）。

1. **实践目标**：把本讲三个模块的知识压缩进一张图，形成可以指导后续 47 讲阅读的「作战地图」。
2. **操作步骤**：
   - 准备纸笔或任意画图工具。先画一个大方框代表 **Go 核心**，内部从下到上分五层：存储层（`pkg/download/storage.go`，bbolt）、下载核心（`pkg/download/downloader.go`）、协议实现（`internal/protocol/` 下 http/bt/hls/ftp/ed2k）、JS 扩展引擎（`pkg/download/engine/`）、服务层（`pkg/api/service.go` 的 Dispatcher + Service）。
   - 在核心右侧画一个可选的 **REST 服务器适配器**（`pkg/rest`），从服务层接出。
   - 在核心上方画 **Flutter UI**（`ui/flutter`），与核心之间画三条通道：桌面 Dart FFI（`bind/desktop`）、移动 gomobile（`bind/mobile`）、Web REST。
   - 再补两个小外壳：**CLI**（`cmd/gopeed`，直接进程内调用核心）和**外部程序**（经 REST/MCP 访问，`pkg/mcpserver`）。
   - 最后用四种颜色的笔在图上圈出四种产品形态各自复用的范围：桌面版、移动版、Web 版（`cmd/web` + Flutter Web）、CLI 版。
3. **需要观察的现象**：画的过程中你会被迫回答一些问题——JS 扩展引擎在核心内还是核心外？CLI 需要经过 Dispatcher 吗？Web 版的 REST 服务器是必须的还是可选的？答不上来就回到 4.2.3 的源码精读再确认。
4. **预期结果**：一张包含 **Go 核心（含分层）、Flutter 外壳、三条通信通道、JS 扩展引擎、存储层、CLI/外部程序入口** 的架构图，以及四个标注了复用范围的圈。参考要点：桌面/移动复用全部核心但走进程内通道且 REST 默认关闭；Web 版复用全部核心且必须走 REST；CLI 复用全部核心且不经过任何传输层。
5. 画完后用第 4.1.4 节的协议矩阵对照检查：核心里的协议实现目录是否都画进去了。本实践为文档阅读 + 绘图任务，无需运行程序。

## 6. 本讲小结

- Gopeed 是一个 **Go 核心 + Flutter 外壳** 的高速开源下载器，支持 HTTP/HTTPS、BT、磁力、ed2k（代码中还有 HLS 与 FTP），覆盖桌面、移动、Web、CLI、Go 库五种形态。
- 架构的灵魂是「**一个核心、多种外壳、一份业务实现**」：三条通道（桌面 FFI / 移动 gomobile / Web REST）最终都汇入同一个 Dispatcher 与 ApplicationService。
- 原生端的本地 REST 服务器**默认关闭**、可独立启停，与 Downloader 生命周期解耦；Web 形态则必须走 REST。
- 桥接层（`bind/`）刻意保持 **小而稳定**：业务入口只有 `InvokeAsync` 一个，请求以 `method/path/query/body` 字符串形式进入，因此新增 API 时桥接层零改动。
- CLI（`cmd/gopeed`）展示了「无桥直连」的极限形态：`download.Boot().URL(...).Create(...)` 链式调用即可完成一次下载。
- 阅读源码要**以代码为准、交叉验证文档**：README 的 Development 一节仍描述旧的 socket 通信，而 2.0 架构文档描述的是当前的 FFI/gomobile 直连。

## 7. 下一步学习建议

下一讲 **u1-l2《把它跑起来：环境准备与各端构建运行》**：亲手用 `go build` 编译 CLI、用 Docker 启动 Web 服务，把本讲的架构图变成能摸到的进程。建议预先安装 Go 1.25+（可选：Flutter 3.41+ 与 Docker）。

如果想提前热身，可以通读：

- [ui/flutter/docs/backend-transport-architecture.md](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/ui/flutter/docs/backend-transport-architecture.md) 的第 4 节（后端维护模型）与第 8 节（加一个 API 的成本）——这两节最能体现该架构的维护优势。
- [cmd/gopeed/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go) 全文（约 150 行）——下一单元 u1-l4 会带你逐行拆解它。
- [go.mod](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/go.mod)——扫一眼依赖清单（torrent、goja、bbolt、mux 等），对核心用到的轮子有个印象。
