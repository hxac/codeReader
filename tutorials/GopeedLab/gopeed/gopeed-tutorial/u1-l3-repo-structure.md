# 仓库地图：目录结构与模块职责

## 1. 本讲目标

前两讲（u1-l1、u1-l2）我们建立了「一个 Go 核心 + 多种外壳」的架构认知，并且亲手把 CLI 和 Web 服务跑了起�来。但当你想真正读懂源码时，遇到的第一个障碍往往是：**这个仓库这么大，我想看的代码到底在哪个目录里？**

本讲就是来解决这个问题的。学完本讲，你应该能够：

1. 说出仓库每个顶层目录（`cmd/`、`bind/`、`pkg/`、`internal/`、`ui/`、`_examples/`、`_docs/`）对应的模块职责。
2. 掌握 Go 项目两条重要的「隐形规则」：**多入口复用同一模块**（`cmd/` 下每个子目录都是一个可执行程序）和 **`pkg/` 与 `internal/` 的可见性分层**（公共 API 与内部实现分离）。
3. 做到「1 分钟定位」：给出一个功能点（比如「HTTP 多连接下载」「JS 扩展引擎」「REST 路由」），能立刻说出它大致位于哪个目录、哪个文件。
4. 看懂 [go.mod](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/go.mod) 中的关键第三方依赖（torrent、goja、bbolt、mux 等），并说出它们分别被哪些模块使用。

本讲是纯「地图课」，不深入任何模块内部——后续单元会逐一深潜。但地图不准，后面每一步都会迷路。

## 2. 前置知识

| 概念 | 通俗解释 |
| --- | --- |
| package main | Go 的可执行程序必须有一个 `package main` 包，其中包含无参的 `main()` 函数作为入口。一个仓库可以有**多个** main 包，各自编译出不同的可执行文件。 |
| module（模块） | `go.mod` 文件定义的一个编译单元，包含模块路径（这里是 `github.com/GopeedLab/gopeed`）和全部依赖版本。一个 module 可以同时容纳「库代码」和「多个可执行入口」。 |
| import 路径 | 包的完整引用路径 = 模块路径 + 目录相对路径。例如 `github.com/GopeedLab/gopeed/pkg/download` 对应仓库里的 `pkg/download/` 目录。 |
| internal 目录 | Go 语言的编译器级规则：`internal/` 下的包**只能被其父目录树之内**的代码 import。外界（包括你自己新建的外部项目）无法 import 它。这是 Go 官方推荐的「内部实现」隔离手段。 |
| pkg 目录 | 社区惯例（非语言规则）：存放**打算对外公开**的库代码。Gopeed 把领域模型、下载引擎、API 服务层放在 `pkg/`，表示它们可以被当�作 Go 库复用。 |
| cgo 与 `//export` | Go 通过 cgo 与 C 交互。`import "C"` 之后用 `//export 函数名` 注释，可以把 Go 函数导出成 C 符号，编译成动态库（`.dll`/`.dylib`/`.so`）后供其他语言（如 Dart FFI）调用。 |
| gomobile | Go 官方工具，把 Go 包编译成 Android `.aar` / iOS framework 供移动 App 嵌入。产物里的包名由源码的 `package` 声明决定（Gopeed 是 `libgopeed`）。 |
| build tags | 写在文件首行的条件编译指令（如 `//go:build web`），决定该文件是否参与本次编译。上一讲已详细讲过，本讲会看到它如何影响 `cmd/web` 的文件布局。 |
| bbolt | 一个嵌入式键值数据库（B+ 树），数据落在一个单文件里，无需独立数据库进程。Gopeed 用它做任务与配置的持久化。 |
| goja | 用 Go 实现的 ECMAScript 5.1+ 解释器，让你在 Go 进程里直接跑 JavaScript 代码。Gopeed 的 JS 扩展系统建立在它之上。 |

## 3. 本讲源码地图

本讲涉及的关键文件（全部为真实路径，已逐一核对）：

| 文件 / 目录 | 作用 |
| --- | --- |
| [go.mod](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/go.mod) | 模块定义与第三方依赖清单（本讲第 4.4 节的依赖地图依据） |
| [cmd/gopeed/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go) | CLI 入口：命令行下载器 |
| [cmd/api/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/api/main.go) | 本地开发用最小 API 服务入口 |
| [cmd/web/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/web/main.go) | Web 服务端入口（`web` 构建标签 + 嵌入前端产物） |
| [cmd/server.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go) | `cmd/api` 与 `cmd/web` 共享的启动逻辑 `Start()` |
| [bind/desktop/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/bind/desktop/main.go) | 桌面端 c-shared 动态库入口（C 导出接口） |
| [bind/mobile/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/bind/mobile/main.go) | 移动端 gomobile 绑定入口（package libgopeed） |
| [bind/native/invoke.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/bind/native/invoke.go) | 桌面/移动共享的请求执行器 |
| [pkg/base/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go) | 领域模型：Request / Resource / Options / 全局配置 |
| [pkg/download/downloader.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go) | 下载引擎核心（Downloader 结构体与任务生命周期） |
| [pkg/download/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go) | Task 模型与 DownloaderConfig（协议注册表） |
| [pkg/api/service.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/api/service.go) | 与传输无关的应用服务层（路由表 + Dispatch） |
| [internal/fetcher/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go) | 协议抽象层：Fetcher 接口与 URL 匹配规则 |
| [ui/flutter/lib/main.dart](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/ui/flutter/lib/main.dart) | Flutter 前端入口 |
| [_examples/basic/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/_examples/basic/main.go) | 把 Gopeed 当 Go 库使用的最小示例 |

## 4. 核心概念与源码讲解

先给一张总览图，后面四个小节逐一展开。仓库的 Go 代码自上而下可以分为四层：

```text
┌─────────────────────────────────────────────────────────┐
│  入口层  cmd/            七个可执行入口（CLI、服务端、辅助进程） │
│  桥接层  bind/           桌面 FFI + 移动 gomobile + 共享执行器  │
│  公共层  pkg/            领域模型 / 下载引擎 / API 服务 / REST  │
│  内部层  internal/       协议实现 / fetcher 抽象 / 基础设施     │
├─────────────────────────────────────────────────────────┤
│  前端    ui/flutter/     Flutter 应用（桌面 / 移动 / Web 三端） │
│  示例    _examples/      Go 库使用示例                        │
│  文档    _docs/          图片资源                              │
└─────────────────────────────────────────────────────────┘
```

### 4.1 cmd/ 多入口：一个模块编译出多种可执行形态

#### 4.1.1 概念说明

很多语言项目只有一个入口（比如 `src/main.ts`），而 Gopeed 是 Go 社区典型的 **「单 module 多入口」** 布局：`cmd/` 下每个子目录都是一个 `package main`，各自有 `main()` 函数，编译产物名默认等于目录名。上一讲我们说过 Gopeed 有五种产品形态，本节要回答的是：**每种形态对应哪个入口目录？**

先看目录清单（真实存在）：

| 入口 | 编译产物 | 职责 |
| --- | --- | --- |
| `cmd/gopeed/` | `gopeed` | CLI 命令行下载器（终端进度条） |
| `cmd/api/` | `api` | 本地开发用最小 API 服务（默认 `127.0.0.1:9999`） |
| `cmd/web/` | `web`（需 `-tags web`） | 生产 Web 服务端：嵌入 Flutter Web 产物 + REST API |
| `cmd/host/` | `host` | 桌面端辅助进程：通过本地 socket 检测/唤醒正在运行的 Gopeed，并转发请求 |
| `cmd/updater/` | `updater` | 自更新辅助进程：等旧进程退出后替换二进制（编译产物放入 `ui/flutter/assets/exec/`） |
| `cmd/extmock/` | `extmock` | 扩展模拟服务：测试 WebView 扩展链路用的假服务 |
| `cmd/server.go` | （不是入口） | **包级共享代码**：`cmd/api` 与 `cmd/web` 共用的 `Start()` 函数 |

注意最后一行：`cmd/server.go` 位于 `cmd/` 根目录、包名为 `cmd`，它**不是**可执行入口，而是一个普通库包，被两个入口复用。这是「多入口 + 共享启动逻辑」的常见写法。

#### 4.1.2 核心流程

以 Web 服务端为例，从入口到运行的调用链：

```text
cmd/web/main.go (带 //go:build web 标签)
  ├─ 解析命令行参数（cmd/web/flags.go）
  ├─ fs.Sub(dist, "dist")           取出嵌入的 Flutter Web 静态资源
  ├─ 组装 model.StartConfig          网络地址、存储、鉴权、MCP 开关等
  └─ cmd.Start(cfg)                  ← 跳到 cmd/server.go 的共享逻辑
        ├─ rest.BuildServer(cfg)     组装 HTTP 服务器（路由/中间件/静态资源）
        ├─ Downloader.GetConfig()    首次启动时写入默认下载目录与内置分类
        ├─ watchExit()               监听 SIGINT/SIGTERM 做优雅关闭
        └─ srv.Serve(listener)       开始对外服务
```

CLI 入口则完全不同：它不启动 HTTP 服务器，直接以 Go 库的方式驱动下载引擎（`download.Boot()` 链式 API，上一讲 u1-l4 预告过，第 4.1.3 节看源码）。

#### 4.1.3 源码精读

**证据一：CLI 入口用 Boot 链式 API 直连核心。** [cmd/gopeed/main.go:L34-L63](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go#L34-L63) 中，`main()` 解析参数后直接调用 `download.Boot().URL(...).Listener(...).Create(...)` 完成一次下载：URL 链式传入、`Listener` 回调里处理进度与最终结果事件、`Create` 接收 `base.Options`（含 HTTP 协议的 `OptsExtra{Connections, Checksum}`）。注意它 import 的是 `pkg/download` 与 `pkg/protocol/http`——CLI 不经过任何 HTTP 服务器。

**证据二：`cmd/web` 的文件头是构建标签，前端产物靠 go:embed 打进二进制。** [cmd/web/main.go:L1-L18](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/web/main.go#L1-L18)：第 1-2 行 `//go:build web` 说明不带 `-tags web` 时这个文件根本不参与编译（所以 `cmd/web` 目录在没有标签时是「空」的）；第 17-18 行 `//go:embed dist/*` 把编译好的 Flutter Web 产物整目录嵌入。接着 [cmd/web/main.go:L50-L64](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/web/main.go#L50-L64) 组装 `StartConfig`：`Storage: model.StorageBolt`（bbolt 持久化）、`WebEnable: true` + `WebFS: sub`（托管 Web UI）、可选的 `WebAuth` 账号密码与 `ApiToken`。

**证据三：两个服务入口共享 `cmd.Start()`。** [cmd/server.go:L19-L69](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go#L19-L69) 是 `cmd/api` 与 `cmd/web` 共同的启动函数：构建服务器、首次加载时写默认下载目录（Docker 内与本地用户目录不同）、初始化内置分类、注册退出信号监听。而 [cmd/api/main.go:L12-L24](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/api/main.go#L12-L24) 的 `main()` 只是组装一个最小 `StartConfig`（注释明确写着 `// only for local development`）后调用 `cmd.Start(cfg)`。

**证据四：`cmd/updater` 的用途写在注释里。** [cmd/updater/main.go:L19](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/updater/main.go#L19) 的注释给出了它的构建目标：`go build ... -o ui/flutter/assets/exec/ .../cmd/updater`——它是随桌面应用分发的更新辅助程序，通过 `-pid` 等参数（见同文件 `flag` 定义）等待旧进程退出后完成替换。

#### 4.1.4 代码实践

**实践目标**：验证「cmd 下每个子目录 = 一个独立可执行程序」，并亲眼看到构建标签的作用。

**操作步骤**：

1. 在仓库根目录执行 `go list ./...`，观察输出的包路径，确认 `cmd/gopeed`、`cmd/api`、`cmd/web` 等各自成包。
2. 编译 CLI：`go build -o /tmp/gopeed-cli ./cmd/gopeed`，运行 `/tmp/gopeed-cli --help` 查看参数。
3. 尝试不带标签编译 web 入口：`go build ./cmd/web`，观察报错或产物情况；再执行 `go vet -tags web ./cmd/web`（若无 `dist/` 目录会因 `go:embed` 报错——这正好证明嵌入是编译期行为）。
4. 阅读 `cmd/host/main.go` 的 `wakeup()` 函数（约 L40-L60）：它先通过本地 socket `Dial()` 检测桌面端是否在运行，不在则用 `browser.OpenURL("gopeed:...")` 唤起，理解它作为「协议处理器辅助进程」的角色。

**需要观察的现象**：步骤 3 中，不带 `web` 标签时 `cmd/web` 目录下没有任何可编译的 Go 文件（main.go 被标签排除）；带标签但缺 `dist/` 时 embed 报错。

**预期结果**：`go list` 能列出全部入口包；CLI 二进制可执行并展示 `--url`、`--dir` 等参数（参数定义在 `cmd/gopeed/flags.go`）。步骤 3 的具体报错文案因本机环境而异，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cmd/api` 和 `cmd/web` 不各自写一份服务器启动代码，而要共享 `cmd/server.go` 里的 `Start()`？

**参考答案**：两者的差异只在「入口参数与配置来源」（`cmd/web` 有嵌入前端、账号密码、token 等配置；`cmd/api` 只有最小配置），而「构建服务器 → 初始化默认配置 → 监听退出信号 → 开始服务」的流程完全一致。抽取共享的 `Start()` 后，新增服务形态（比如以后加一个 gRPC 入口）只需写一个薄入口 + 复用启动逻辑，避免复制粘贴导致的维护分叉。

**练习 2**：`cmd/extmock` 和 `cmd/updater` 都不是「下载器」，为什么也放在 `cmd/` 下？

**参考答案**：`cmd/` 的语义是「可执行入口的集合」，而不是「下载器入口」。`extmock` 是测试 WebView 扩展链路时独立运行的模拟服务，`updater` 是随桌面应用分发、负责替换二进制的更新辅助进程——它们都需要独立编译成可执行文件，因此遵循惯例放在 `cmd/` 下。

### 4.2 pkg/ 与 internal/ 的分层：公共 API 与内部实现

#### 4.2.1 概念说明

Gopeed 的核心代码分住在 `pkg/` 和 `internal/` 两个顶层目录，这是刻意的架构分层：

- **`pkg/`（公共层）**：打算对外暴露的能力。理论上任何外部 Go 项目都可以 `import "github.com/GopeedLab/gopeed/pkg/download"` 把 Gopeed 当库用（`_examples/basic` 正是这么做的）。
- **`internal/`（内部层）**：受 Go 编译器保护的实现细节。`internal/` 下的包只能被本模块内引用，外部项目 import 会直接编译失败。协议的具体实现、fetcher 抽象、各种基础设施都在这里，作者因此可以随时重构而不担心破坏外部使用者。

两层的子目录职责如下（均为真实目录）：

| pkg/ 子目录 | 职责 |
| --- | --- |
| `pkg/base` | 领域词汇表：Request、Resource、Options、全局配置、状态常量、PieceMap |
| `pkg/download` | 下载引擎：Downloader、Task、存储、扩展系统（含 `engine/` JS 引擎）、分类、解压、webhook |
| `pkg/api` | 与传输无关的应用服务层：路由表 + `Dispatch` 进程内调用 |
| `pkg/rest` | REST 服务器：HTTP 组装、鉴权、静态资源、原生桥接的 `Dispatch` 入口 |
| `pkg/mcpserver` | MCP 服务器：把任务操作暴露给 AI 客户端 |
| `pkg/protocol/{http,bt,ed2k,ftp}` | 各协议**对外公开的模型**（如 `http.OptsExtra`、`http.ChecksumOption`） |
| `pkg/util` | 通用工具：字节格式化、URL、路径、定时器 |

| internal/ 子目录 | 职责 |
| --- | --- |
| `internal/fetcher` | 协议抽象契约：Fetcher/FetcherManager 接口、URL 匹配过滤器 |
| `internal/protocol/{http,bt,hls,ftp,ed2k}` | 协议**具体实现**（HTTP 分块、BT 做种、HLS 解密合并等） |
| `internal/controller` | 注入给 Fetcher 的共享服务（配置读取、代理、文件预分配） |
| `internal/httpclient` | 统一 HTTP 客户端封装 |
| `internal/logger`、`internal/tempfiles` | 日志、临时文件目录作用域 |
| `internal/blob` | 扩展产出内存资源（Blob）的注册表 |
| `internal/githubmirror` | GitHub 下载 URL 镜像改写 |
| `internal/webview` | WebView 运行时（go/goprovider、rpcprovider、proxy） |
| `internal/test` | 测试辅助（httptest 假服务器等） |

注意一个容易混淆的点：**`pkg/protocol/http` 与 `internal/protocol/http` 是两个不同的包**。前者放「使用者需要填的参数结构」（公开模型），后者放「真正干活的 Fetcher 实现」（内部细节）。CLI 入口 import 的是前者。

#### 4.2.2 核心流程

一次「创建下载任务」请求在分层中的传导路径（自上而下）：

```text
外部调用（REST / FFI / CLI / MCP）
   ↓
pkg/api      Service.Dispatch 匹配路由，执行 createTask handler
   ↓
pkg/download Downloader.CreateDirect → 生成 Task → 调度 doStart
   ↓
internal/fetcher   按 URL 匹配 FetcherManager（协议路由）
   ↓
internal/protocol/xxx   具体协议 Fetcher 开始 Resolve / Start
   ↓
pkg/base     全程使用 Request / Resource / Options 这套词汇
```

`pkg/` 依赖 `internal/`（引擎调用协议实现），反向则不行也不需要——`internal/protocol` 只依赖 `internal/fetcher` 与 `pkg/base` 的模型。这个单向依赖让「换协议实现」不影响上层。

#### 4.2.3 源码精读

**证据一：`pkg/base` 是全项目的词汇表。** [pkg/base/model.go:L18-L28](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L18-L28) 定义 `Request`（下载请求：URL、Extra、标签、代理）；[L96-L105](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L96-L105) 定义 `Resource`（探测到的资源：名称、大小、是否支持 Range、文件列表）；[L139-L156](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L139-L156) 定义 `Options`（用户选项：路径、重命名、SelectFiles、协议 Extra）。同一文件还集中了全局配置 `DownloaderStoreConfig`（L213 起）——**改一个字段全项目生效**，这就是把模型收敛在一处的好处。

**证据二：`pkg/download` 是引擎心脏。** [pkg/download/downloader.go:L104-L138](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L104-L138) 的 `Downloader` 结构体聚合了任务列表、等待队列、fetcher 缓存、存储、扩展系统、各类锁；[L39-L53](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L39-L53) 定义了 8 个存储桶常量（task/save/task_stats/protocol_state/protocol_jobs/config/extension/extension_storage），说明 bbolt 里的数据分桶布局。文件末尾 [L2799-L2805](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2799-L2805) 的 `Boot()` 就是 CLI 用的链式入口。

**证据三：协议注册表在 `pkg/download/model.go`。** [pkg/download/model.go:L227-L250](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L227-L250) 的 `DownloaderConfig.Init()` 在未指定 FetchManagers 时注册默认协议管理器，顺序为 **ftp → hls → http → bt → ed2k**。源码注释解释了为什么 FTP 排最前：它按 URL scheme 匹配，必须排在按文件扩展名匹配的协议之前，否则 `ftp://host/x.torrent` 会被 BT 协议抢走。这张注册表就是第 3 单元要讲的「协议路由」的源头。

**证据四：`internal/fetcher` 定义协议契约。** [internal/fetcher/fetcher.go:L11-L33](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L11-L33) 定义 `Fetcher` 接口（Setup/Resolve/Start/Patch/Pause/Close/Stats/Meta/Progress/Wait）——每个下载任务对应一个 Fetcher 实例。同文件 [L86-L119](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L86-L119) 定义三类 URL 过滤器（url scheme / 文件扩展名 / base64 data），是协议匹配的判定依据。它放在 `internal/` 意味着：**协议接入点是内部扩展点，不是对外稳定 API**。

**证据五：`pkg/api` 是与传输无关的服务层。** [pkg/api/service.go:L46-L86](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/api/service.go#L46-L86) 的 `NewService` 用一张 `[]RouteSpec` 声明了全部 `/api/v1/*` 路由（info/resolve/tasks/config/extensions/webhook.test 等）；[L92-L119](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/api/service.go#L92-L119) 的 `Dispatch` 在**进程内**直接遍历路由执行 handler，不经过任何网络。REST 服务器、FFI 桥接、测试共用这一份实现——这正是上一讲「一份业务实现」结论的代码落点。

#### 4.2.4 代码实践

**实践目标**：训练「1 分钟定位」能力——只靠目录地图找到功能所在文件。

**操作步骤**：

1. 不看答案，先独立回答下面 5 个问题，写出你猜测的目录/文件：
   - Q1：HTTP 多连接分块下载的算法在哪？
   - Q2：任务持久化到 bbolt 的代码在哪？
   - Q3：REST 接口的鉴权中间件在哪？
   - Q4：JS 扩展的 goja 引擎封装在哪？
   - Q5：`Connections: 8` 这个参数的结构体定义在哪？
2. 然后用 Grep 验证（在仓库根目录）：
   - `grep -rn "expandConnections" internal/` → Q1
   - `grep -rn "func NewBoltStorage" pkg/` → Q2
   - `grep -rn "gopeed-session" pkg/rest/` → Q3
   - `grep -rn "dop251/goja" pkg/download/engine/` → Q4
   - `grep -rn "Connections" pkg/protocol/http/model.go` → Q5

**需要观察的现象**：每个 grep 的命中文件是否与你猜测的目录一致；`internal/protocol/http/fetcher.go` 这类文件里同时藏着多少你熟悉的函数名。

**预期结果**：Q1 → `internal/protocol/http/fetcher.go`；Q2 → `pkg/download/storage.go`；Q3 → `pkg/rest/server.go`；Q4 → `pkg/download/engine/engine.go`；Q5 → `pkg/protocol/http/model.go`。如果 5 题全对，说明本讲的地图已经在你脑子里了。

#### 4.2.5 小练习与答案

**练习 1**：如果外部项目写 `import "github.com/GopeedLab/gopeed/internal/protocol/http"` 会发生什么？为什么作者要这样设计？

**参考答案**：编译失败，报 "use of internal package ... not allowed"。Go 规定 `internal/` 下的包只能被以其父目录（这里是模块根）为前缀的包树内引用。这样设计是为了把协议实现锁定为内部细节：作者可以自由重构 HTTP/BT 实现而不破坏外部使用者；外部能依赖的只有 `pkg/` 里的稳定 API。

**练习 2**：`pkg/protocol/http` 和 `internal/protocol/http` 各放什么？为什么拆成两个包？

**参考答案**：前者放使用者需要构造的**公开参数模型**（如 `OptsExtra.Connections`、`ChecksumOption`，CLI 的 main.go 就 import 了它）；后者放协议**实现**（Fetcher、FetcherManager、分块算法等）。拆开后，「参数结构的稳定性」与「实现的可变性」解耦：实现再怎么重写，公开模型的 JSON 字段不变，外部调用方与已落盘的任务数据都不受影响。

### 4.3 bind/ 桥接目录：原生应用接入 Go 核心的三种形态

#### 4.3.1 概念说明

上一讲我们说过：桌面端通过 Dart FFI 调 C 动态库，移动端通过 gomobile 绑定。`bind/` 目录就是这两种「原生接入方式」的家，共三个子目录：

| 子目录 | 形态 | 产物 |
| --- | --- | --- |
| `bind/desktop/` | cgo + `//export` | C 共享库（Windows `.dll` / macOS `.dylib` / Linux `.so`），桌面 Flutter 通过 FFI 调用 |
| `bind/mobile/` | gomobile 绑定 | Android `.aar` / iOS framework，包名 `libgopeed` |
| `bind/native/` | 共享 Go 包（非入口） | 桌面与移动共用的 `invokeExecutor` 请求执行器 |

关键认知（承接 u1-l1 的结论）：**桥接层刻意小而稳定**。它不实现任何业务，只做「翻译」——把来自 Dart/Kotlin/Swift 的 `(method, path, query, body)` 四元组转发给 Go 侧的统一入口 `rest.Dispatch`。因此新增 API 接口时桥接层**零改动**。

另外注意 `bind/mobile/main.go` 的包名是 `libgopeed`（不是 main），且它**没有 `main()` 函数**——gomobile 会为它生成平台绑定代码。

#### 4.3.2 核心流程

一次原生调用（以 Flutter 桌面端创建任务为例）的完整链路：

```text
Dart (FFI 调用 C 符号 InvokeAsync)
  ↓  method/path/query/body 字符串 + requestID + 回调指针
bind/desktop/main.go  InvokeAsync (//export 导出)
  ↓  转成 Go 字符串，包装 complete 回调
bind/native/invoke.go nativebridge.InvokeAsync
  ↓  包装成 invokeTask 提交进 invokeExecutor（每个请求一个 goroutine）
pkg/rest/invoke.go    rest.Dispatch(method, path, query, body)
  ↓
pkg/api/service.go    Service.Dispatch 匹配 RouteSpec → 执行 handler
  ↓
pkg/download          Downloader 真正干活
  ↓ （结果逆流而上）
invokeTask.finish → C.callInvokeResultCallback → Dart 回调收到 JSON
```

任务事件（进度/完成/出错）走另一条对称通道：`SubscribeTaskEvents` 订阅，Go 侧主动回调 `C.callTaskEventCallback` 把 JSON payload 推给 Dart。

#### 4.3.3 源码精读

**证据一：桌面导出接口极简。** [bind/desktop/main.go:L32-L52](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/bind/desktop/main.go#L32-L52)：`//export Start` 接收 JSON 配置字符串，反序列化为 `StartConfig` 后调用 `rest.Start` 启动核心，再 `nativebridge.ResumeInvokes()` 放行请求队列；`//export Stop` 只有一行 `nativebridge.Stop()`。[L86-L119](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/bind/desktop/main.go#L86-L119) 的 `InvokeAsync` 把 C 字符串转 Go 字符串后全部交给 `nativebridge.InvokeAsync`——注意它自己**不做任何路由判断**，路由匹配发生在 `pkg/api` 的 RouteSpec 里。

**证据二：移动端绑定与桌面共享同一执行器。** [bind/mobile/main.go:L15-L28](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/bind/mobile/main.go#L15-L28) 的 `Start` 与桌面版逻辑几乎逐行相同（反序列化配置 → `rest.Start` → `ResumeInvokes`）；[L63-L86](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/bind/mobile/main.go#L63-L86) 的 `InvokeAsync` / `SubscribeTaskEvents` 通过接口（`InvokeResultListener` / `TaskEventListener`）接收回调——这是 gomobile 的风格差异（gomobile 传接口，FFI 传函数指针）。

**证据三：共享执行器保证「一个卡死请求不拖垮全局」。** [bind/native/invoke.go:L41-L57](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/bind/native/invoke.go#L41-L57) 的 `InvokeAsync` 把请求包装成 `invokeTask` 提交执行器；[L106-L127](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/bind/native/invoke.go#L106-L127) 显示执行器在初始化时注入的 handler 就是 `rest.Dispatch`，并且注释明确说明「每个原生 API 请求都跑在自己的 goroutine 里，与 net/http 的并发模型一致；一个卡住的请求绝不能阻止其他请求启动」。[L14-L15](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/bind/native/invoke.go#L14-L15) 的 `StopTimeout = 3 * time.Second` 则保证关闭时最多等 3 秒。

**证据四：C 头文件随仓库分发。** [ui/flutter/include/libgopeed.h](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/ui/flutter/include/libgopeed.h) 是桌面动态库的 C 头文件，Dart FFI 侧靠它知道有哪些符号可调——`bind/` 与 `ui/` 的契约文档就在这两个位置。

#### 4.3.4 代码实践

**实践目标**：确认「桥接层零业务」——数一数桥接层暴露的全部接口，验证新增 API 不需要改这里。

**操作步骤**：

1. 执行 `grep -n "//export" bind/desktop/main.go`，列出桌面导出的全部 C 符号。
2. 对照 `ui/flutter/include/libgopeed.h` 中的函数声明，确认两侧一一对应。
3. 执行 `grep -rn "tasks" bind/`，观察 bind 目录下**没有任何**任务相关业务代码（唯一的命中应该只是无关字符串或没有）。
4. 回看 [pkg/api/service.go:L51-L80](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/api/service.go#L51-L80) 的路由表：里面已经有 resolve/tasks/config/extensions 等几十个接口，而桥接层只有 8 个左右的导出函数。

**需要观察的现象**：`//export` 数量是固定的个位数；路由表数量远大于导出函数数量；两者数量之差就是「零桥接改动」换来的接口数。

**预期结果**：桌面导出符号包括 `Start`、`Stop`、`GetAPIServerState`、`StartAPIServer`、`StopAPIServer`、`RestartAPIServer`、`InvokeAsync`、`SubscribeTaskEvents`、`FreeCString`（以实际 grep 输出为准）。

#### 4.3.5 小练习与答案

**练习 1**：为什么桌面端用「函数指针回调」（`C.callInvokeResultCallback`），移动端却用「接口回调」（`InvokeResultListener`）？

**参考答案**：桌面动态库通过 C ABI 与 Dart FFI 交互，C ABI 只认函数指针和基本类型，所以回调只能以指针形式跨语言传递（还配套导出了 `FreeCString` 来管理内存所有权）；gomobile 的绑定生成器原生支持把 Go interface 映射成 Java/Kotlin/Swift 的接口实现，所以移动端可以直接传接口对象。这是两种原生通道能力差异导致的自然选择。

**练习 2**：如果想在 REST API 新增一个「批量导出任务列表」的接口，`bind/` 目录需要改吗？

**参考答案**：不需要。新增接口只需在 `pkg/api/service.go` 的 `NewService` 路由表里加一行 `newRoute(...)` 并实现 handler。原生端统一通过 `InvokeAsync("POST", "/api/v1/xxx", ...)` 调用，路径是运行时字符串，桥接层对接口数量无感知——这正是「桥接小而稳定」设计的意义。

### 4.4 ui/flutter/ 前端目录与 go.mod 依赖地图

#### 4.4.1 概念说明

`ui/` 下有两个目录：`ui/flutter`（现行前端）和 `ui/flutter_old`（旧版保留）。`ui/flutter` 是一个标准 Flutter 工程，但它同时要产出一个产物：

- 桌面 App（Windows/macOS/Linux）——通过 FFI 调 `bind/desktop` 编出的动态库；
- 移动 App（Android/iOS）——通过 gomobile 绑定；
- **Web 版静态资源**——`flutter build web` 的产物被放进 `cmd/web/dist`，由 Go 服务端嵌入托管。

所以 `ui/` 不只是「界面代码」，它是三种产品形态共享的 UI 层。`lib/` 内部分层遵循仓库 `ui/flutter/AGENTS.md` 约定的结构：

| lib/ 子目录 | 职责 |
| --- | --- |
| `lib/api/` | 对接 Go 核心的调用层（FFI/REST 封装） |
| `lib/app/` | 应用壳：路由、主界面骨架、语言配置 |
| `lib/core/` | 核心基础设施：网络、窗口管理、启动引导（`core/libgopeed_boot.dart` 等） |
| `lib/features/` | 按功能域组织（任务、扩展、设置等） |
| `lib/shared/` | 跨功能的共享组件 |
| `lib/l10n/` | 多语言文案（arb 文件） |

`ui/flutter/docs/` 下还有两份重要架构文档：`backend-transport-architecture.md`（三端通信架构，u1-l1 已读）与 `window-capability-rpc.md`（跨窗口能力 RPC）。

本节另一半主题是 **go.mod 依赖地图**——「每个重量级依赖养活了哪个模块」是理解仓库结构的另一条线索。

#### 4.4.2 核心流程

Flutter 前端的启动决策流程（[ui/flutter/lib/main.dart](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/ui/flutter/lib/main.dart) 的 `main()`）：

```text
main(args)
  ├─ 平台判断：macOS 跳过沙盒检查 / Android 初始化前台任务通信端口
  ├─ AppWindowLaunchContext.fromArgs(args)
  │     ├─ 是子窗口 → 连接主窗口会话，运行 ChildWindowApp
  │     └─ 是主窗口 ↓
  ├─ 桌面端读取初始 URI（deep link 唤起，配合 cmd/host）
  ├─ AppInitializer.ensureStorageInitialized()   初始化本地存储
  ├─ AppWindowBootstrap.setupMainWindow()        窗口/托盘/多窗口引导
  └─ runApp(ProviderScope(child: GopeedApp()))   进入 Riverpod 状态管理的应用
```

#### 4.4.3 源码精读

**证据一：main.dart 是三端共用的单一入口。** [ui/flutter/lib/main.dart:L19-L55](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/ui/flutter/lib/main.dart#L19-L55) 的 `main()` 通过 `Util.isMacos()` / `Util.isAndroid()` / `Util.isDesktop()` 做平台分支，再区分主窗口与子窗口两条启动路径。它不直接 import `lib/api/api.dart` 的重逻辑——按照 docs 的架构约束，只有主窗口拥有 Gopeed 运行时连接。

**证据二：Go 库使用示例在 `_examples`。** [_examples/basic/main.go:L11-L34](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/_examples/basic/main.go#L11-L34) 用 30 行代码完成一次 8 连接下载：`download.Boot().URL(...).Listener(...).Create(...)`。它证明 `pkg/download` 是设计上可独立复用的库——这也是 `pkg/` 与 `internal/` 分层存在的实证。

**证据三：go.mod 关键依赖 → 使用模块对照表。** 以下每一行都经过 grep 验证（依赖版本见 [go.mod:L5-L33](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/go.mod#L5-L33) 的 require 块）：

| 依赖 | 用途 | 被谁使用（import 位置） |
| --- | --- | --- |
| `github.com/anacrolix/torrent`（L8） | BitTorrent 协议全家桶 | `internal/protocol/bt/fetcher.go`、`storage.go` |
| `github.com/dop251/goja` + `goja_nodejs`（L11-L12） | JS 解释器（扩展系统） | `pkg/download/engine/engine.go`、`inject/*/module.go`、`extension.go` |
| `github.com/gorilla/mux`（L17） | REST 路由 | `pkg/rest/server.go`、`pkg/rest/api.go` |
| `github.com/imroc/req/v3`（L18） | HTTP 客户端 | `internal/httpclient/client.go` |
| `github.com/jlaffaye/ftp`（L19） | FTP 客户端 | `internal/protocol/ftp/{fetcher,conn,walk}.go` |
| `github.com/modelcontextprotocol/go-sdk`（L23） | MCP 服务器 SDK | `pkg/mcpserver/server.go` |
| `github.com/monkeyWie/goed2k`（L24） | eDonkey2000 协议 | `internal/protocol/ed2k/fetcher.go` |
| `github.com/go-git/go-git/v5`（L14） | 扩展的 git 安装方式 | `pkg/download/extension.go`、`extension_git_transport.go` |
| `github.com/GopeedLab/webview_go`（L7） | WebView 绑定 | `internal/webview/goprovider/` |
| `go.etcd.io/bbolt`（L31） | 嵌入式 KV 存储 | `pkg/download/storage.go` |

#### 4.4.4 代码实践

**实践目标**：亲手验证依赖地图——用工具自动回答「依赖 X 被谁使用」。

**操作步骤**：

1. 在仓库根目录执行 `go mod graph | head -20`，感受依赖图的规模。
2. 用 Grep 复核上表任意三行，例如：
   - `grep -rln "anacrolix/torrent" --include=*.go .`
   - `grep -rln "dop251/goja" --include=*.go .`
   - `grep -rln "go.etcd.io/bbolt" --include=*.go .`
3. 执行 `go list -deps ./cmd/gopeed | grep -c "github.com/GopeedLab/gopeed"` 观察 CLI 入口递归依赖了多少个本模块内的包。
4. （可选，需要 Flutter 环境）在 `ui/flutter` 下执行 `ls lib/features/`，对照 4.4.1 的表格确认功能域划分。

**需要观察的现象**：步骤 2 中每个依赖的命中文件是否都聚集在同一模块（例如 bbolt 只命中 `pkg/download/storage.go`），这种「聚集」就是模块边界清晰的信号。

**预期结果**：bbolt 的命中应该只有 `pkg/download/storage.go` 一个非测试文件；goja 的命中集中在 `pkg/download/engine/` 与 `pkg/download/extension*.go`；torrent 的命中全部在 `internal/protocol/bt/`。步骤 3 的具体数字**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`ui/flutter_old` 为什么还留在仓库里？

**参考答案**：它是旧版前端的历史保留，方便对照迁移与回溯设计。从「读源码」的角度可以忽略它——所有现行前端代码都在 `ui/flutter`。这也提醒我们读开源项目时先确认「哪个目录是活的」，避免在废弃代码里浪费时间（判断依据之一是看 CI 构建脚本 `.github/workflows/build.yml` 引用了哪个目录）。

**练习 2**：Gopeed 的 MCP 服务器（`pkg/mcpserver`）和 REST 服务器（`pkg/rest`）是什么关系？

**参考答案**：两者是并列的两个「传输外壳」，都构建在同一个 Downloader 之上。REST 把 `/api/v1/*` 暴露给浏览器与原生端；MCP 通过 `modelcontextprotocol/go-sdk` 把任务操作暴露给 AI 客户端。它们共享 `pkg/api` 服务层与同一个核心实例——这就是 u1-l1「一份业务实现、多种外壳」在服务端的体现。

## 5. 综合实践

**任务：制作你自己的《Gopeed 仓库导航手册》**（对应本讲的 practice_task）。

### 5.1 实践目标

把本讲所学的目录地图内化成一份你自己的笔记，之后整个学习手册的阅读都靠它检索。

### 5.2 操作步骤

1. **顶层目录职责页**：为 `cmd/`、`bind/`、`pkg/`、`internal/`、`ui/`、`_examples/` 六个顶层目录各写一页说明，每页包含：
   - 一句话职责（不许抄本讲义，用自己的话写）；
   - 三个代表性文件及其作用（用 `ls` / `Glob` 实际找，链接到源码）；
   - 「我会从这里找什么」：至少两条定位线索。
2. **依赖地图页**：从 [go.mod](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/go.mod) 的直接依赖中挑 5 个关键依赖（建议：anacrolix/torrent、dop251/goja、go.etcd.io/bbolt、gorilla/mux、modelcontextprotocol/go-sdk），对每个依赖记录：
   - 它解决什么问题（一句话）；
   - 被 `grep -rln` 找到的使用文件清单；
   - 如果明天它被替换成另一个库，估计要改动哪些目录。
3. **一分钟定位自测**：合上所有资料，回答以下 8 题，写出目录级定位（答案见下）：
   - (1) BT 做种逻辑；(2) 任务速度计算；(3) Web 登录会话；(4) HLS 分片解密；(5) 自动解压队列；(6) 全局下载目录配置的字段；(7) 扩展的 onResolve 钩子触发点；(8) `gopeed://` 协议唤起桌面端。
4. 把这份手册保存为 `gopeed-tutorial/notes/repo-map.md`（这是你的个人笔记，不属于讲义正文；也可以放在仓库外的任意位置）。

### 5.3 需要观察的现象

写「依赖地图页」时你会发现一个规律：**几乎每个重量级依赖的使用文件都高度聚集在单一模块内**（torrent 只在 `internal/protocol/bt/`，goja 只在 `pkg/download/engine/` 相关文件）。这个规律反过来就是最好的检索启发式：「问题里出现某个协议/某种技术，就直接去它对应的技术目录找」。

### 5.4 预期结果（8 题定位答案）

| 题 | 定位 |
| --- | --- |
| (1) BT 做种 | `internal/protocol/bt/fetcher.go` |
| (2) 速度计算 | `pkg/download/model.go`（`calcSpeed` 滑动窗口） |
| (3) Web 登录会话 | `pkg/rest/server.go`（`gopeed-session` cookie 与 webSessionStore） |
| (4) HLS 解密 | `internal/protocol/hls/crypto.go` |
| (5) 自动解压队列 | `pkg/download/extract_queue.go` |
| (6) 全局下载目录字段 | `pkg/base/model.go`（`DownloaderStoreConfig`） |
| (7) 扩展 onResolve 触发点 | `pkg/download/downloader.go`（Resolve 主链路中） |
| (8) `gopeed://` 唤起 | `cmd/host/main.go`（`wakeup` 用 `browser.OpenURL("gopeed:...")`） |

## 6. 本讲小结

- `cmd/` 是**多入口集合**：gopeed（CLI）、api（开发用服务）、web（生产 Web 服务，需 `-tags web`）、host（唤起辅助）、updater（更新辅助）、extmock（测试模拟）；`cmd/server.go` 是共享启动逻辑而非入口。
- `pkg/` 与 `internal/` 是**可见性分层**：`pkg/`（base/download/api/rest/mcpserver/protocol/util）是可被外部复用的公共库；`internal/`（fetcher/protocol/controller/httpclient 等）受编译器保护，是可自由重构的内部实现。
- `pkg/protocol/http`（公开参数模型）与 `internal/protocol/http`（协议实现）是两个不同的包，「模型稳定」与「实现可变」由此解耦。
- `bind/` 是**小而稳定的桥接层**：desktop 用 cgo `//export`，mobile 用 gomobile（package libgopeed），两者共享 `bind/native` 的 invokeExecutor，统一汇入 `rest.Dispatch` → `pkg/api.Service.Dispatch`，新增 API 桥接层零改动。
- `ui/flutter` 是**三端共享的 UI 层**（桌面 FFI / 移动绑定 / Web 静态资源），`lib/` 内部按 api/app/core/features/shared/l10n 分层；`ui/flutter_old` 是历史保留可忽略。
- go.mod 的重量级依赖与模块一一对应：torrent→`internal/protocol/bt`，goja→`pkg/download/engine`，bbolt→`pkg/download/storage.go`，mux→`pkg/rest`，MCP SDK→`pkg/mcpserver`——依赖聚集处即模块边界。

## 7. 下一步学习建议

地图已经画好，下一讲（u1-l4「第一个程序：用 Boot API 写命令行下载器」）将第一次**动手写代码**：基于 `_examples/basic/main.go` 与 `cmd/gopeed/main.go`，用 `download.Boot()` 链式 API 完成一个带进度条的下载器，并对比不同连接数下的表现。

如果你想在进入下一讲前再热身，推荐按本讲的地图做两件事：

1. 通读 [pkg/download/model.go:L211-L250](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L211-L250)，记住 `DownloaderConfig` 的字段——下一讲的 Boot API 本质上就是在构造这份配置。
2. 浏览 [internal/fetcher/fetcher.go:L11-L33](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L11-L33) 的 Fetcher 接口方法名，混个眼熟——第 3 单元会逐方法精读这份「协议契约」。
