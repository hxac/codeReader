# 第一个程序：用 Boot API 写命令行下载器

## 1. 本讲目标

前面三讲我们认识了 Gopeed 的架构、把它构建运行了起来、画好了仓库地图。本讲要写第一段代码：**把 Gopeed 当作一个 Go 库来使用**，用最少的代码完成一次真实下载。

学完本讲，你应该能够：

1. 使用 `download.Boot()` 链式 API 创建一个下载任务，说清它背后每一环做了什么。
2. 使用 `Listener` 回调监听 `start`、`progress`、`finally` 等七种事件，并知道每种事件在源码里的触发位置和触发时机。
3. 通过 `base.Options` 指定下载目录，通过 `http.OptsExtra` 的 `Connections` 字段控制 HTTP 并发连接数，并验证多连接确实更快。

本讲是「动手课」：三个最小模块（Boot 链式构造器、Listener 事件回调、Options 与 OptsExtra）都配有可以直接运行的实践。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（不熟悉的话先补一补，都很轻量）：

- **Go module 与 import 路径**：Gopeed 是一个单一 module，module 路径为 `github.com/GopeedLab/gopeed`（见 [go.mod:1](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/go.mod#L1)）。所以在仓库内写代码时，`import "github.com/GopeedLab/gopeed/pkg/download"` 就能直接使用下载引擎。
- **链式 API（Builder 模式）**：形如 `Obj().A(x).B(y).C(z)` 的写法，每个方法都返回自身，像流水线一样逐步配置，最后一步执行真正动作。你在 u1-l1 已经见过 `Boot()` 的调用照片，本讲会读它的实现。
- **回调函数（Listener）**：你不主动轮询结果，而是把一个函数交给框架，框架在事情发生时调用它。这是 GUI、网络编程里最常见的「观察者模式」。
- **URL scheme 与协议识别**：`https://`、`magnet:?`、`ed2k://` 这些 URL 前缀决定了任务交给哪个协议处理器。u1-l3 讲过 `internal/protocol` 下各协议的注册顺序，本讲只需记住：**Boot 只给你一个 URL，协议路由完全自动**。
- **`any` 类型的 Extra 机制**：`base.Request.Extra` 和 `base.Options.Extra` 字段类型是 `any`（接口），用来装「某个协议专属的配置结构体」。传给 HTTP 任务时它是 `http.ReqExtra` / `http.OptsExtra`，传给 BT 任务时则是别的结构。协议实现内部再用类型断言取回。这是 Go 里常见的高扩展性手法，我们会在 4.3 精读。

另外，承接 u1-l2 的成果：你应该已经能用 `go build` 编译本项目，本讲的实践都在仓库根目录内用 `go run` 完成。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| [_examples/basic/main.go](_examples/basic/main.go) | 官方最简示例，30 行完成一次下载 | 我们的主教材，逐行精读 |
| [cmd/gopeed/main.go](cmd/gopeed/main.go) | CLI 版 Gopeed 的主逻辑：进度条打印、校验和 | 进度条打印的最佳参考实现 |
| [cmd/gopeed/flags.go](cmd/gopeed/flags.go) | CLI 参数解析（`-C` 连接数、`-D` 目录等） | 看 CLI 如何把参数填进 `Options` |
| [pkg/download/downloader.go](pkg/download/downloader.go) | 下载引擎核心，`Boot()`、`Listener`、`emit` 都在这里 | 只读三个角落：Boot 构造器（文件末尾）、事件发射点、进度刷新循环 |
| [pkg/download/event.go](pkg/download/event.go) | 七种事件常量与 `Event` 结构 | 全文只有 19 行，完整精读 |
| [pkg/download/model.go](pkg/download/model.go) | `Task`、`DownloaderConfig` 等模型 | 看默认配置如何生成 |
| [pkg/base/model.go](pkg/base/model.go) | 领域模型：`Request`、`Options`、`Resource` | 精读 `Request` 与 `Options` |
| [pkg/protocol/http/model.go](pkg/protocol/http/model.go) | HTTP 协议公开参数模型 | 精读 `OptsExtra`（含 `Connections`） |

> 提示：`pkg/download/downloader.go` 有 2800 多行，本讲只带你读其中 5 个片段，其余部分留给第 4 单元。

## 4. 核心概念与源码讲解

### 4.1 Boot 链式构造器

#### 4.1.1 概念说明

Gopeed 除了是桌面应用、Web 服务，还是一个**可以直接 import 的 Go 库**。`download.Boot()` 就是这个库对外敞开的最简大门：它包揽了「初始化引擎 → 配置 URL → 注册监听器 → 创建并启动任务」的全过程，让三行代码完成一次下载成为可能。

它的设计要点有两个：

1. **包级单例**：`Boot()` 背后是一个包内全局的 `defaultDownloader`，第一次调用 `Boot()` 时自动完成初始化，调用方完全不需要感知 `Downloader` 结构体。
2. **链式配置**：返回的 `boot` 结构体提供 `URL()`、`Extra()`、`Listener()` 三个配置方法，每个都返回自身，最后用 `Create()` 收口。

这层封装刻意保持「小而薄」——它只是 `NewDownloader + Setup + CreateDirect` 的语法糖。当你后面（第 4 单元）需要多任务、自定义存储、独立配置时，就直接使用底层的 `Downloader` API，`Boot()` 的定位就是「示例与简单工具」。

#### 4.1.2 核心流程

一次 `Boot().URL(...).Listener(...).Create(...)` 调用的完整流程：

```text
download.Boot()
    └─ defaultDownloader.Setup()          # 初始化：临时目录、存储桶、加载配置、启动进度刷新循环
    └─ 返回空 boot 结构体

boot.URL("https://...")                    # 记录 URL（可链式继续）
boot.Listener(fn)                          # 记录回调函数
boot.Create(&base.Options{...})
    └─ defaultDownloader.Listener(fn)      # 把回调安装到全局单例上
    └─ defaultDownloader.CreateDirect(&base.Request{URL: ..., Extra: ...}, opts)
        └─ createDirect
            ├─ buildFetcher(url)           # 按 URL 选中协议（http/bt/ed2k/...），构建 fetcher
            ├─ initOptions(opts, url)      # 规整 Options（空值回退默认目录等）
            └─ doCreate(fetcher, opts)
                ├─ NewTask()               # 生成任务 ID，初始状态 ready
                ├─ 存储任务、加入任务列表
                └─ 还有运行名额？ ──是──▶ doStart(task)  # 异步启动下载 + watch
                                └─否──▶ 进入 wait 队列，等名额空出再启动
```

也就是说：**`Create` 返回时任务已经排队或开始下载**，返回值是任务 ID（字符串），可以留着以后查询、暂停使用。

#### 4.1.3 源码精读

先看官方最简示例 [_examples/basic/main.go:11-34](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/_examples/basic/main.go#L11-L34)：

```go
finallyCh := make(chan error)
_, err := download.Boot().
    URL("https://www.baidu.com/index.html").
    Listener(func(event *download.Event) {
        if event.Key == download.EventKeyFinally {
            finallyCh <- event.Err
        }
    }).
    Create(&base.Options{
        Extra: http.OptsExtra{
            Connections: 8,
        },
    })
if err != nil {
    panic(err)
}
err = <-finallyCh
```

这段代码做的事情：建一个接收「最终结果」的 channel，用 Boot 链创建任务（8 连接），主 goroutine 阻塞在 `err = <-finallyCh` 上等结果——**异步回调 + channel 通知**是 Go 里把异步事件转回同步等待的标准手法。

接着看 `Boot()` 的真身，全部位于 [pkg/download/downloader.go:2768-2805](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2768-L2805)：

```go
var defaultDownloader = NewDownloader(nil)

type boot struct {
    url      string
    extra    interface{}
    listener Listener
}

func (b *boot) URL(url string) *boot { b.url = url; return b }
func (b *boot) Extra(extra interface{}) *boot { b.extra = extra; return b }
func (b *boot) Listener(listener Listener) *boot { b.listener = listener; return b }

func (b *boot) Create(opts *base.Options) (string, error) {
    defaultDownloader.Listener(b.listener)
    return defaultDownloader.CreateDirect(&base.Request{
        URL:   b.url,
        Extra: b.extra,
    }, opts)
}

func Boot() *boot {
    err := defaultDownloader.Setup()
    if err != nil {
        panic(err)
    }
    return &boot{}
}
```

拆解几个关键点：

- [downloader.go:2768](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2768)：`defaultDownloader` 是**包加载时就创建的全局单例**，配置传 `nil`。
- [downloader.go:2799-2805](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2799-L2805)：`Boot()` 对单例执行 `Setup()`（初始化失败直接 panic），然后返回一个空的 `boot`。
- [downloader.go:2791-2797](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2791-L2797)：`Create()` 把链上收集的三样东西打包：回调安装到单例上、URL 和 Extra 组装成 `base.Request`，再交给 `CreateDirect`。

`nil` 配置会走到哪套默认值？看 [pkg/download/model.go:227-250](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L227-L250) 的 `DownloaderConfig.Init()`：

```go
if len(cfg.FetchManagers) == 0 {
    cfg.FetchManagers = []fetcher.FetcherManager{
        new(ftp.FetcherManager),
        new(hls.FetcherManager),
        new(http.FetcherManager),
        new(bt.FetcherManager),
        new(ed2k.FetcherManager),
    }
}
if cfg.RefreshInterval == 0 {
    cfg.RefreshInterval = 350
}
if cfg.Storage == nil {
    cfg.Storage = NewMemStorage()
}
```

也就是说 `Boot()` 默认注册了 FTP、HLS、HTTP、BT、ED2K 五个协议管理器（注册顺序即匹配优先级，u1-l3 和第 3 单元会展开），进度每 350 毫秒刷新一次，存储用**内存存储**——进程退出后任务记录不落盘，这是 Boot 简单模式与完整服务的区别之一。

最后看 `CreateDirect` 的入口 [pkg/download/downloader.go:605-630](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L605-L630)：

```go
func (d *Downloader) CreateDirect(req *base.Request, opts *base.Options) (taskId string, err error) {
    defaultPath, err := d.prepareDefaultPath(opts)
    ...
    taskId, err = d.createDirect(req, opts)
    ...
}

func (d *Downloader) createDirect(req *base.Request, opts *base.Options) (taskId string, err error) {
    ensureRequestRawURL(req)
    var fetcher fetcher.Fetcher
    fetcher, err = d.buildFetcher(req.URL)   // 协议路由：URL → 协议管理器 → fetcher
    ...
    fetcher.Meta().Req = req
    initOpt, err := d.initOptions(opts, req.URL)
    ...
    return d.doCreate(fetcher, initOpt)
}
```

「Direct」的含义是**跳过独立的 Resolve（元数据解析）步骤，直接建任务**——解析会在任务启动后由 fetcher 自己完成。而 `doCreate` 的收尾在 [downloader.go:1856-1870](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1856-L1870)：

```go
d.tasks = append(d.tasks, task)
remainRunningCount := d.remainRunningCount()
if remainRunningCount == 0 {
    task.Status = base.DownloadStatusWait
    d.waitTasks = append(d.waitTasks, task)
    return
}
err = d.doStart(task)
```

运行名额（`MaxRunning`）用完就排队（`wait`），否则立即 `doStart`。`doStart` 内部用一个 goroutine 执行启动逻辑，成功后发出 `start` 事件并启动 `watch` 协程，见 [downloader.go:2104-2117](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2104-L2117)：

```go
go func() {
    if err := handler(); err != nil {
        if d.taskIsRunningGeneration(task, generation) {
            d.doOnError(task, err)
        }
        return
    }
    if started {
        if d.taskIsRunningGeneration(task, generation) {
            d.emit(EventKeyStart, task)
        }
        go d.watch(task)
    }
}()
```

#### 4.1.4 代码实践

**实践目标**：把官方示例跑起来，直观感受「三行代码一次下载」。

**操作步骤**：

1. 进入仓库根目录（有 `go.mod` 的那层）。
2. 执行 `go run ./_examples/basic`。
3. 等待输出 `download success`。
4. 打开当前工作目录，找一找下载下来的 `index.html`（默认下载目录由配置决定；如果在 output 里找不到，改用第 5 节综合实践里带 `-D` 目录参数的版本）。

**需要观察的现象**：程序几乎立即返回 `download success`（目标文件很小，一瞬完成）；再次运行会在 `logs/` 下产生核心日志文件（`Setup` 阶段初始化日志，见 [downloader.go:163-165](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L163-L165)）。

**预期结果**：终端输出 `download success`。若网络无法访问示例 URL，会输出 `download fail:...` 及具体错误——这也是验证错误分支的方式。

（本实践依赖外网连通性，具体输出待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：`Boot()` 为什么不需要传任何参数？它内部的 `NewDownloader(nil)` 中的 `nil` 最终变成了什么？

<details><summary>参考答案</summary>

`Boot()` 面向「最简使用」场景，所有配置走默认值。`NewDownloader(nil)` 内部会把 `nil` 替换成空 `DownloaderConfig` 并调用 `Init()` 补默认值：默认协议管理器列表（ftp/hls/http/bt/ed2k）、`RefreshInterval = 350ms`、`MemStorage` 内存存储等，见 [model.go:140-144](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L140-L144) 与 [model.go:227-250](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L227-L250)。

</details>

**练习 2**：示例为什么用无缓冲 channel `finallyCh` 接收结果，而不是在回调里直接 `fmt.Println` 后结束？

<details><summary>参考答案</summary>

回调只是「事件到达时被调用」，`Create()` 返回并不代表下载结束。主 goroutine 需要一个同步点知道「何时可以退出」，`err = <-finallyCh` 就是这个同步点。而且 `finally` 事件在一次任务生命周期里恰好触发一次（见 4.2），用它收尾不会收到多次或零次。直接在回调里打印进度信息没问题，但**决定程序何时退出**必须有类似 channel/WaitGroup 的机制（CLI 版用的是 `sync.WaitGroup`，见 [cmd/gopeed/main.go:20-21](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go#L20-L21) 与 [main.go:54](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go#L54)）。

</details>

**练习 3**：如果把示例中的 URL 换成 `magnet:?xt=urn:btih:...`（磁力链接），`Create` 会失败吗？

<details><summary>参考答案</summary>

不会因为「不支持」而失败。`createDirect` 里的 `buildFetcher` 会按 URL 特征把磁力链接路由给 BT 协议管理器（u1-l3 讲过的协议注册与匹配），后续流程完全一样，只是 `Options.Extra` 里 HTTP 专属的 `OptsExtra` 对 BT 任务不生效。这正是「协议路由自动化」的含义：调用方只管给 URL。

</details>

### 4.2 Listener 事件回调

#### 4.2.1 概念说明

下载是一个长时间、多阶段的过程：解析元数据 → 建立连接 → 持续传输 → 完成（或失败）。程序需要一种方式在**每个阶段被通知**，这就是事件系统。Gopeed 的事件模型极其轻量：

- 一个回调类型 `Listener func(event *Event)`；
- 一个事件结构 `Event{Key, Task, Err}`；
- 七种事件键 `EventKey`。

安装方式你已经见过：`boot.Listener(fn)`（内部就是给 `Downloader` 的 `listener` 字段赋值）。**注意它是全局单例上唯一的监听器**——如果你创建多个任务，所有任务的事件都会流入同一个回调，靠 `event.Task.ID` 区分。

#### 4.2.2 核心流程

七种事件与它们的触发位置（本讲只要求理解前四种）：

| 事件 Key | 触发时机 | 源码位置（downloader.go） |
| --- | --- | --- |
| `start` | 任务首次真正开始下载（`doStart` 的启动 handler 成功后） | [L2113](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2113) |
| `progress` | 进度刷新循环每 350ms 一次；Patch（运行中修改任务）等状态变化时也会发 | [L367](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L367)、[L755](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L755) |
| `pause` | 任务被成功暂停（手动或调度） | [L2133](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2133) |
| `error` | 任务进入 error 状态（下载失败且未恢复） | [L1742](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1742) |
| `done` | 任务成功完成 | [L1593](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1593) |
| `finally` | **done 或 error 之后必然紧跟发出一次**，代表「本次运行尘埃落定」 | [L1594](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1594)、[L1743](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1743) |
| `delete` | 任务被删除 | [L1213](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1213) |

关键结论：

1. **`finally` 是合成事件**：源码里没有任何地方直接以 `finally` 为「原始事实」——它总是紧跟在 `done`（Err 为 nil）或 `error`（Err 非 nil）之后发出。所以「等待一个任务结束」只需要监听 `finally`，通过 `event.Err` 是否为 nil 判断成败。
2. **`progress` 是周期性心跳**：频率由 `RefreshInterval`（默认 350ms）决定，事件里的 `event.Task.Progress` 携带最新已下载字节数和速度。
3. **回调是同步调用**：`emit` 直接调用你的函数，你的回调执行在触发事件的那个 goroutine 上。

#### 4.2.3 源码精读

事件模型全文在 [pkg/download/event.go:5-19](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/event.go#L5-L19)：

```go
const (
    EventKeyStart    = "start"
    EventKeyPause    = "pause"
    EventKeyProgress = "progress"
    EventKeyError    = "error"
    EventKeyDelete   = "delete"
    EventKeyDone     = "done"
    EventKeyFinally  = "finally"
)

type Event struct {
    Key  EventKey
    Task *Task
    Err  error
}
```

`Listener` 类型与事件发射器在 [pkg/download/downloader.go:61](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L61) 和 [downloader.go:1294-1310](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1294-L1310)：

```go
type Listener func(event *Event)

func (d *Downloader) Listener(fn Listener) {
    d.listener = fn
}

func (d *Downloader) emit(eventKey EventKey, task *Task, errs ...error) {
    if d.listener != nil {
        var err error
        if len(errs) > 0 {
            err = errs[0]
        }
        d.listener(&Event{Key: eventKey, Task: task, Err: err})
    }
}
```

`emit` 只是「如果装了监听器就调用它」，没有任何队列和缓冲。

progress 心跳的来源是 `Setup()` 启动的全局刷新循环，见 [downloader.go:292-296](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L292-L296) 的注释 `// calculate download speed every tick` 与 [downloader.go:340-367](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L340-L367)：

```go
tick := float64(d.cfg.RefreshInterval) / 1000
...
if task.Status == base.DownloadStatusRunning {
    downloadDataChanged = current != task.Progress.Downloaded
    task.Progress.Used = task.timer.Used()
    task.Progress.Speed = task.updateSpeed(max(0, delta), tick)
    task.Progress.Downloaded = current
}
...
// Listener callbacks may Pause/Continue and acquire statusLock.
d.emit(EventKeyProgress, task)
```

这个循环每 `RefreshInterval`（[downloader.go:388](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L388) 的 `time.Sleep`）遍历一次所有任务，把 fetcher 报告的累计字节数写进 `task.Progress`，同时算出瞬时速度，然后发出 `progress` 事件。`Task.Progress` 的字段定义在 [downloader.go:81-102](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L81-L102)，最常用的三个：`Downloaded`（已下载字节）、`Speed`（字节/秒）、`Used`（累计耗时纳秒）。

再看「终局」的两处。成功路径在 `watch` 协程里（`watch` 负责盯着 fetcher 直到它结束，[downloader.go:1517](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1517)），任务完成时执行 [downloader.go:1583-1599](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1583-L1599)：

```go
if !d.markTaskDone(task) {
    return
}
...
d.storage.Put(bucketTask, task.ID, snapshot)
d.emit(EventKeyDone, task)
d.emit(EventKeyFinally, task, err)   // err 为 nil
d.notifyRunning()                     // 唤醒 wait 队列里的下一个任务
```

失败路径在 `doOnError` 的收尾 [downloader.go:1740-1746](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1740-L1746)：

```go
if d.taskStatus(task) == base.DownloadStatusError {
    d.releaseBlobTask(task)
    d.emit(EventKeyError, task, err)
    d.emit(EventKeyFinally, task, err)
    ...
}
```

两处对照即可证明 4.2.2 的结论：`finally` 永远伴随 `done`/`error` 出现，且只出现一次。

最后看 CLI 版如何消费这些事件——[cmd/gopeed/main.go:36-56](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go#L36-L56)：

```go
Listener(func(event *download.Event) {
    if event.Key == download.EventKeyProgress {
        printProgress(event.Task, "downloading...")
    }
    if event.Key == download.EventKeyFinally {
        var title string
        if event.Err != nil {
            title = "fail"
        } else {
            title = "complete"
        }
        printProgress(event.Task, title)
        fmt.Println()
        ...
        wg.Done()
    }
}).
```

与 basic 示例不同，CLI 版同时消费 `progress`（刷新进度条）和 `finally`（打印结论并 `wg.Done()` 放行主 goroutine 的 `wg.Wait()`）。

#### 4.2.4 代码实践

**实践目标**：读懂 CLI 的进度条实现，理解「事件 → 渲染」的数据流。

**操作步骤**：

1. 阅读 [cmd/gopeed/main.go:82-113](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go#L82-L113) 的 `printProgress`：

   ```go
   func printProgress(task *download.Task, title string) {
       var rate float64
       if task.Meta.Res == nil {
           task = emptyTask
       }
       if task.Meta.Res.Size <= 0 {
           rate = 0
       } else {
           rate = float64(task.Progress.Downloaded) / float64(task.Meta.Res.Size)
       }
       completeWidth := int(progressWidth * rate)
       speed := util.ByteFmt(task.Progress.Speed)
       totalSize := util.ByteFmt(task.Meta.Res.Size)
       sb.WriteString(fmt.Sprintf("\r%s [", title))
       for i := 0; i < progressWidth; i++ {
           if i < completeWidth {
               sb.WriteString("■")
           } else {
               sb.WriteString("□")
           }
       }
       sb.WriteString(fmt.Sprintf("] %.1f%%    %s/s    %s", rate*100, speed, totalSize))
       ...
   }
   ```

2. 回答三个问题（答案在「需要观察的现象」后面）：
   - 为什么开头要判断 `task.Meta.Res == nil`？
   - `\r` 和「补空格抹掉旧输出」分别解决什么问题？
   - `util.ByteFmt` 是什么？
3. 用 `go run ./cmd/gopeed -C 4 -D /tmp/gopeed-cli <某个文件URL>` 实际跑一次，观察进度条刷新节奏。

**需要观察的现象**：进度条约每 0.35 秒向前推进一段（与 `RefreshInterval = 350` 对应）；完成时标题从 `downloading...` 变成 `complete` 并换行打印保存路径。

三个问题的答案：

1. `progress` 事件可能在协议 fetcher 尚未解析出资源（`Res`，含文件名和总大小）时到达，此时 `Res` 为 nil，直接用会空指针崩溃，所以换成预构造的空任务 `emptyTask`（[cmd/gopeed/main.go:74-79](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go#L74-L79)）兜底显示 0%。
2. `\r`（回车不换行）让每次打印覆盖上一行，形成原地刷新的动画；由于百分比位数、速度单位长度会变化，短的新输出会残留旧字符，所以记录 `lastLineLen` 用空格补齐抹除。
3. 字节数人性化格式化函数（1024 进制换算成 B/KB/MB/GB），见 [pkg/util/bytefmt.go:12-36](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/util/bytefmt.go#L12-L36)。

**预期结果**：能独立说出「350ms 心跳 → emit(progress) → Listener → printProgress 重绘」这条链路。（命令输出待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么「判断一次下载的最终结果」要监听 `finally` 而不是 `done` 和 `error` 各监听一次？

<details><summary>参考答案</summary>

监听 `finally` 一个就够：它在 `done`（成功）和 `error`（失败）两条路径上都会发出，用 `event.Err == nil` 区分成败（[downloader.go:1593-1594](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1593-L1594)、[downloader.go:1742-1743](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1742-L1743)）。分别监听 `done` 和 `error` 也能实现，但 `finally` 语义上就是「无论成败，本任务这次运行结束了」，代码更简洁，官方两个示例都用它收尾。

</details>

**练习 2**：任务被用户暂停时会收到 `finally` 吗？会收到什么事件？

<details><summary>参考答案</summary>

不会收到 `finally`。暂停路径只发出 `pause`（[downloader.go:2133](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2133)），`finally` 只属于「终局」（完成或失败）。暂停的任务之后被继续（Continue）再跑完，仍会正常收到一次 `finally`。

</details>

**练习 3**：如果在 Listener 回调里做耗时 2 秒的操作，会发生什么？

<details><summary>参考答案</summary>

`emit` 是同步调用（[downloader.go:1298-1310](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1298-L1310)），回调会阻塞触发它的 goroutine。对 `progress` 来说就是拖慢全局刷新循环（它在循环里逐任务 emit），所有任务的进度刷新都会变慢。正确做法是回调里只做轻量记录，重活交给独立 goroutine/channel。源码特意保证 emit 在 `task.lock` 之外调用，因此回调里**可以安全地**再调用 `Pause`/`Patch` 等 Downloader 方法（见 [downloader.go:366](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L366)、[downloader.go:754-755](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L754-L755) 的注释）——「能调用」不等于「该在里面阻塞」。

</details>

### 4.3 Options 与 OptsExtra

#### 4.3.1 概念说明

创建任务需要两组输入，方向正好相反：

- **`base.Request`（请求，进来的）**：描述「从哪下载」——URL、请求方法/头/体（HTTP 专属 Extra）、标签、代理等。Boot 模式下它由 `boot.Create` 从链上配置自动组装。
- **`base.Options`（选项，控制怎么下载）**：描述「存到哪、下载什么、用什么参数」——下载目录、文件名、选择哪些文件、以及协议专属 Extra。

「协议专属 Extra」是这套模型最巧的部分：`Options.Extra` 类型是 `any`。HTTP 任务里放 `http.OptsExtra`（连接数、自动做种、校验和……），BT 任务里放 BT 的结构。协议实现拿到任务后自己做类型断言取回——**核心引擎完全不关心每种协议的细节**，扩展新协议不需要改动 `base.Options`。这与 u1-l1 讲过的「一份业务实现、多协议接入」一脉相承。

#### 4.3.2 核心流程

以 HTTP 任务设置 `Connections: 8` 为例，数据流是：

```text
你的代码: Create(&base.Options{Path: "/tmp/dl", Extra: http.OptsExtra{Connections: 8}})
    │
    ▼
Downloader: initOptions()          # 空目录回退默认目录等规整（downloader.go:1889）
    │
    ▼
HTTP Fetcher.initOptions()          # 协议侧解析 Extra
    │   base.ParseOptExtra[fhttp.OptsExtra](opts)   # JSON 往返 → 类型断言
    │   if extra.Connections <= 0 { 回退全局配置; 再回退 1 }
    ▼
Fetcher.Start(): maxConns := extra.Connections      # 用来构建慢启动控制器，开多条连接分块下载
```

两个值得记住的行为：

1. **类型安全地接受任意输入**：`Extra` 从 `any` 到具体结构体要经过 `base.ParseOptExtra`（内部做 JSON 序列化/反序列化转换），所以你传错了类型不会 panic，而是返回错误。
2. **三级回退**：任务级 Extra → 全局协议配置 → 协议默认值。`Connections <= 0` 时用全局配置，全局也没有就用 1（[internal/protocol/http/fetcher.go:756-760](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L756-L760)）。

多连接的原理一句话版：服务器支持 Range 请求时，文件被切成多个区间由多条连接并行下载（详细算法在第 5 单元精读）。所以**多连接加速的前提是服务器返回 `Accept-Ranges: bytes`**。

#### 4.3.3 源码精读

`base.Request` 的字段见 [pkg/base/model.go:18-28](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L18-L28)：

```go
type Request struct {
    RawURL string `json:"rawUrl"`
    URL    string `json:"url"`
    Extra  any    `json:"extra"`
    // Labels is used to mark the download task
    Labels map[string]string `json:"labels"`
    // Proxy is special proxy config for request
    Proxy *RequestProxy `json:"proxy"`
    // SkipVerifyCert is the flag that skip verify cert
    SkipVerifyCert bool `json:"skipVerifyCert"`
}
```

`base.Options` 的字段见 [pkg/base/model.go:139-156](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L139-L156)：

```go
type Options struct {
    // Size is an optional HTTP download size estimate. ...
    Size int64 `json:"size,omitempty"`
    // Download file name
    Name string `json:"name"`
    // Download file path
    Path string `json:"path"`
    // Use this path as the default download directory after creation succeeds
    AsDefaultPath bool `json:"asDefaultPath,omitempty"`
    // SkipAutoCategorize keeps the base download directory of this task ...
    SkipAutoCategorize bool `json:"skipAutoCategorize,omitempty"`
    // Select file indexes to download
    SelectFiles []int `json:"selectFiles"`
    // Extra info for specific fetcher
    Extra any `json:"extra"`
}
```

本讲最常用的是 `Path`（下载目录）和 `Extra`；`SelectFiles`（多文件种子里选哪些文件）会在第 6 单元 BT 部分、`Name`（改名）在第 2 单元细讲。

HTTP 的请求侧 Extra（[pkg/protocol/http/model.go:9-13](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/protocol/http/model.go#L9-L13)）：

```go
type ReqExtra struct {
    Method string            `json:"method"`
    Header map[string]string `json:"header"`
    Body   string            `json:"body"`
}
```

任务侧 Extra（[pkg/protocol/http/model.go:81-98](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/protocol/http/model.go#L81-L98)）：

```go
type OptsExtra struct {
    Connections int `json:"connections"`
    // AutoTorrent when task download complete, and it is a .torrent file, ...
    AutoTorrent *bool `json:"autoTorrent"`
    // DeleteTorrentAfterDownload when true, deletes the .torrent file ...
    DeleteTorrentAfterDownload *bool `json:"deleteTorrentAfterDownload"`
    // AutoExtract when task download complete, and it is an archive file, ...
    AutoExtract *bool `json:"autoExtract"`
    // ArchivePassword is the password for extracting password-protected archives
    ArchivePassword string `json:"archivePassword"`
    // DeleteAfterExtract when true, deletes the archive file after successful extraction
    DeleteAfterExtract bool `json:"deleteAfterExtract"`
    // Checksum verifies the completed file against an expected hash after download
    Checksum *ChecksumOption `json:"checksum,omitempty"`
}
```

注意很多字段是 `*bool`：**三态**（nil = 跟随全局配置，true/false = 显式指定），这是避免「没法表达未设置」的常见技巧。

协议侧消费 `Connections` 的两处证据：解析与回退在 [internal/protocol/http/fetcher.go:743-761](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L743-L761)；真正使用在 Start 里创建慢启动控制器 [internal/protocol/http/fetcher.go:900-902](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L900-L902)：

```go
// Initialize slow start controller
maxConns := f.meta.Opts.Extra.(*fhttp.OptsExtra).Connections
f.slowStart = newSlowStartController(maxConns)
```

「连接数不是一次开满，而是像 TCP 慢启动一样分批增长」——这个算法在第 5 单元（u5-l2）专门讲，本讲只需知道 `Connections` 是**上限**。

最后看 CLI 怎么把命令行参数填进 Options：[cmd/gopeed/flags.go:23-26](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/flags.go#L23-L26) 定义了 `-C`（连接数，默认 16）、`-D`（目录，默认当前目录）、`-checksum`、`-checksum-algo` 四个参数；[cmd/gopeed/main.go:57-63](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go#L57-L63) 原样填入：

```go
Create(&base.Options{
    Path: *args.dir,
    Extra: http.OptsExtra{
        Connections: *args.connections,
        Checksum:    checksumOpt,
    },
})
```

#### 4.3.4 代码实践

**实践目标**：验证 `Connections` 对下载耗时的影响，体会多连接加速的条件。

**操作步骤**：

1. 先选一个测试 URL：找一个大一些（比如 50MB 以上）的文件直链，并用 `curl -sI <URL> | grep -i accept-ranges` 确认响应头包含 `Accept-Ranges: bytes`（各大开源镜像站的系统镜像直链通常满足；没有现成的也可以本地起一个静态文件服务器，待本地验证）。
2. 用 CLI 各跑一次，`time` 计时：

   ```bash
   time go run ./cmd/gopeed -C 1  -D /tmp/c1 <URL>
   time go run ./cmd/gopeed -C 16 -D /tmp/c16 <URL>
   ```

3. 观察两次进度条推进方式：单连接时速度平稳，多连接时初段可能更快，且连接是分批建立的（慢启动）。
4. 对比两个目录里文件的大小和可用性（如为压缩包可尝试解压校验完整性）。

**需要观察的现象**：

- 服务器支持 Range 时，`-C 16` 总耗时明显短于 `-C 1`（提升幅度取决于服务器与带宽）。
- 服务器**不支持** Range（响应无 `Accept-Ranges: bytes`）时，两次耗时接近——HTTP fetcher 会回退为单连接顺序下载（回退机制的源码在第 5 单元精读）。

**预期结果**：支持 Range 的源上多连接更快；两次下载的最终文件一致。具体数字待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`http.OptsExtra.Connections` 传 0 会怎样？传 -5 呢？

<details><summary>参考答案</summary>

都不会报错。HTTP fetcher 的 `initOptions` 判断 `extra.Connections <= 0` 时先取全局协议配置里的值，全局也没配则回退为 1（[internal/protocol/http/fetcher.go:756-760](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L756-L760)）。0 和 -5 效果相同：表现为「未设置」。

</details>

**练习 2**：为什么 `AutoExtract` 用 `*bool` 而 `Connections` 用 `int`？

<details><summary>参考答案</summary>

`AutoExtract` 这类开关需要区分「用户没说」（跟随全局配置）和「用户明确要开/关」三种状态，Go 的 `bool` 零值 `false` 无法表达「没设置」，所以用指针：nil 即未设置。而 `Connections <= 0` 天然可以当作「未设置」的哨兵值（合法连接数必为正整数），无需指针（见 [pkg/protocol/http/model.go:81-98](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/protocol/http/model.go#L81-L98) 的注释 `nil means use global config, true/false means explicit setting`）。

</details>

**练习 3**：给 BT 磁力任务传 `Extra: http.OptsExtra{Connections: 8}` 会发生什么？

<details><summary>参考答案</summary>

不会崩溃，也不会生效。`Options.Extra` 会被存进任务 Meta 原样传递，但 BT 的 fetcher 实现只会按 BT 自己的结构去解析 Extra，HTTP 专属字段被忽略；BT 的连接/做种参数走 BT 自己的配置（第 6 单元）。这就是 `any` 型 Extra 的边界：**含义由最终接手的协议决定**。

</details>

## 5. 综合实践

把三个模块串起来：写一个自己的命令行下载器 `mydown`，具备**进度条、结果打印、耗时统计、连接数参数**。所有代码标注为「示例代码」。

**操作步骤**：

1. 在仓库根目录新建目录 `mydown/`（与 `cmd/` 平级，纯练习产物，不要提交），创建 `main.go`：

   ```go
   // 示例代码：仿 _examples/basic 与 cmd/gopeed 写的练习版下载器
   package main

   import (
       "flag"
       "fmt"
       "strings"
       "sync"
       "time"

       "github.com/GopeedLab/gopeed/internal/fetcher"
       "github.com/GopeedLab/gopeed/pkg/base"
       "github.com/GopeedLab/gopeed/pkg/download"
       "github.com/GopeedLab/gopeed/pkg/protocol/http"
       "github.com/GopeedLab/gopeed/pkg/util"
   )

   func main() {
       conn := flag.Int("C", 4, "concurrent connections")
       dir := flag.String("D", ".", "download directory")
       flag.Parse()
       if flag.NArg() == 0 {
           fmt.Println("usage: mydown [-C n] [-D dir] <url>")
           return
       }
       url := flag.Arg(0)

       var wg sync.WaitGroup
       wg.Add(1)
       start := time.Now()

       _, err := download.Boot().
           URL(url).
           Listener(func(event *download.Event) {
               switch event.Key {
               case download.EventKeyStart:
                   fmt.Printf("task %s started\n", event.Task.ID)
               case download.EventKeyProgress:
                   printBar(event.Task)
               case download.EventKeyFinally:
                   fmt.Println()
                   if event.Err != nil {
                       fmt.Printf("FAIL after %.1fs: %v\n", time.Since(start).Seconds(), event.Err)
                   } else {
                       fmt.Printf("DONE after %.1fs -> %s\n", time.Since(start).Seconds(), *dir)
                   }
                   wg.Done()
               }
           }).
           Create(&base.Options{
               Path: *dir,
               Extra: http.OptsExtra{
                   Connections: *conn,
               },
           })
       if err != nil {
           panic(err)
       }
       wg.Wait()
   }

   // printBar 原地刷新一个 20 格进度条（简化自 cmd/gopeed 的 printProgress）
   var lastLen int

   func printBar(t *download.Task) {
       if t.Meta == nil || t.Meta.Res == nil || t.Meta.Res.Size <= 0 {
           return // 解析未完成时先不画
       }
       rate := float64(t.Progress.Downloaded) / float64(t.Meta.Res.Size)
       width := 20
       filled := int(float64(width) * rate)
       bar := strings.Repeat("■", filled) + strings.Repeat("□", width-filled)
       line := fmt.Sprintf("\r[%s] %.1f%%  %s/s", bar, rate*100, util.ByteFmt(t.Progress.Speed))
       if pad := lastLen - len(line); pad > 0 {
           line += strings.Repeat(" ", pad)
       }
       lastLen = len(line)
       fmt.Print(line)
   }

   // 引用 fetcher 包仅为与 cmd/gopeed 的 emptyTask 结构保持一致，可按需删减
   var _ = fetcher.FetcherMeta{}
   ```

   （上面为了让文件可独立编译，用 `var _ = fetcher.FetcherMeta{}` 抵消未使用的 import；实际不需要 emptyTask 兜底时，可删掉 `internal/fetcher` 的 import 与这一行。）

2. 运行：

   ```bash
   go run ./mydown -C 1  -D /tmp/m1 <URL>
   go run ./mydown -C 16 -D /tmp/m16 <URL>
   ```

3. 记录两组 `DONE after ...` 的耗时并对比。

**需要观察的现象**：

- `task xxx started` 先出现（start 事件），然后进度条以约 350ms 的节奏原地刷新（progress 事件），最后出现 `DONE after ...`（finally 事件）。
- `Connections` 更大时总耗时更短（前提：服务器支持 Range）。
- 换一个不支持 Range 的 URL，两种连接数耗时接近。

**预期结果**：一个完整可用的迷你下载器；事件时序符合 `start → progress×N → finally`；多连接对比结论与 4.3.4 一致。（耗时数字待本地验证。）

**思考题（选做）**：把 `Listener` 里的 `wg.Done()` 删掉会发生什么？（答：`wg.Wait()` 永远阻塞，程序无法退出——因为 `finally` 只发一次，错过就不再有信号。）

## 6. 本讲小结

- `download.Boot()` 是库使用的最简入口：背后是包级单例 `defaultDownloader`（`NewDownloader(nil)` + `Setup()`），链式方法 `URL/Extra/Listener` 只是收集配置，`Create()` 把它们组装成 `base.Request` + `base.Options` 交给 `CreateDirect`。
- `Create` 返回即「任务已入队/已启动」：`doCreate` 里有运行名额就 `doStart`，没有就进 `wait` 队列。
- 事件模型只有一函数一结构七常量：`Listener func(*Event)`；`finally` 是紧跟 `done`/`error` 的合成事件，一次任务恰好一次，是「等待结束」的正确挂点；`progress` 由 Setup 启动的全局循环每 350ms（`RefreshInterval`）发射。
- 回调是同步调用，运行在引擎的 goroutine 上——轻量处理，重活外抛；但可以安全地在回调里调用 `Pause`/`Patch`。
- `base.Options` 描述「怎么下载」（目录/文件名/选文件），`Extra any` 挂协议专属配置；HTTP 的 `OptsExtra.Connections` 决定最大连接数，经「任务级 → 全局 → 1」三级回退后由慢启动控制器分批建连，多连接加速依赖服务器支持 Range。

## 7. 下一步学习建议

本讲你把 Gopeed 当黑盒库用了一遍，下一讲（u2-l1「Request、Resource、Options：一次下载的三要素」）将打开这个盒子，系统精读 [pkg/base/model.go](pkg/base/model.go)：`Resource`（解析出来的资源长什么样、`Files` 如何表达多文件）、`FileInfo`、以及 `ParseReqExtra/ParseOptExtra` 的泛型解析实现。

如果你已经迫不及待想深入引擎内部，可以先自己带着两个问题预习：

1. `Event.Task` 里的 `Meta.Res` 从 nil 变为有值，发生在哪个阶段？（答案在第 4 单元 `doStart` 的解析流程里）
2. `RefreshInterval` 改成 100ms，事件频率和存储写入会发生什么？（第 4 单元 watch 与检查点策略）

第 4 单元（u4-l1 起）将完整精读 [pkg/download/downloader.go](pkg/download/downloader.go) 的任务生命周期——本讲你只看到了它的 5 个片段。
