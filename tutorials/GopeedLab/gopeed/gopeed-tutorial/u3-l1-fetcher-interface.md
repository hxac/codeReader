# Fetcher 接口：协议实现的生命周期契约

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐个说出 `Fetcher` 接口十个方法的职责，以及**每个方法由 Downloader 的哪个函数在什么时机调用**。
2. 解释 `FetcherMeta`（Req/Res/Opts 三元组）为什么是 Fetcher 与 Downloader 之间交换数据的唯一媒介，并理解三个路径计算方法的差异。
3. 说明 `Stats` 为什么拆成 `Snapshot`（可持久化）与 `Runtime`（仅内存）两个负载，以及 Downloader 如何分别处理它们。
4. 理解 `DefaultFetcher` 基类与 `DoneCh` 完成通知通道的设计，知道 HTTP、BT、HLS 三种实现各自不同的 `Wait()` 策略。

本讲是第三单元（Fetcher 抽象：协议接入层）的第一讲，也是后续所有协议深潜讲义（HTTP、BT、HLS、FTP、ED2K）的「接口预习课」。

## 2. 前置知识

本讲假设你已读过前两单元，特别是 u2-l2（Task 结构与任务状态机）。在此基础上补充几个概念：

- **接口（interface）即契约**：Go 的接口是一组方法签名。`Fetcher` 接口定义在 `internal/fetcher` 包中，它规定了「任何下载协议实现类必须会做的事」。Downloader 只认识这个契约，不关心背后是 HTTP 还是 BT——这就是 Gopeed 能用同一套任务生命周期管理五种协议的原因。
- **调用方（Caller）与实现方（Implementor）**：阅读接口型代码要始终分清两件事——谁调用方法（Downloader），谁实现方法（各协议的 Fetcher）。本讲会反复使用这张「调用关系图」。
- **内部状态机 vs 任务状态机**：u2-l2 讲过任务的六种状态（ready/wait/running/pause/error/done），那是 Downloader 层的概念。Fetcher 内部还有自己的状态机（如 HTTP 的 idle/resolving/resolved/slowStart/steady/paused/done/error），两者通过方法调用相互驱动，但**任务状态的唯一写入者是 Downloader**，Fetcher 只能通过 `Wait()` 的返回值「汇报」结果。
- **带缓冲 channel**：`make(chan error, 1)` 创建容量为 1 的通道，发送方在通道未满时不会阻塞。这是 `DoneCh` 完成通知的基础，4.4 节详解。
- **快照（Snapshot）与遥测（Telemetry）**：快照是「任务停止后仍有意义」的数据（如 BT 的 piece 完成位图）；遥测是「只在运行时有效」的数据（如当前连接的 peer 列表）。这个二分是 `Stats` 结构的核心动机。
- **internal 包**：`internal/fetcher` 受 Go 编译器可见性规则保护，项目外无法导入。因此本讲的代码实践以「源码阅读 + 在仓库内运行现有测试」为主。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `internal/fetcher/fetcher.go` | Fetcher 契约的定义处 | 接口、Stats、FetcherMeta、DefaultFetcher、Progress |
| `pkg/download/downloader.go` | 接口的唯一调用方 | 各方法的调用点与调用时机 |
| `internal/protocol/http/fetcher.go` | HTTP 协议实现 | Setup/Patch/Stats/Progress/Wait 与 DoneCh 发送 |
| `internal/protocol/bt/fetcher.go` | BT 协议实现 | 与 HTTP 对照的 Resolve/Patch/Stats/Wait |
| `internal/protocol/hls/fetcher.go` | HLS 协议实现 | 唯一内嵌 DefaultFetcher 的实现 |
| `internal/fetcher/fetcher_test.go` | 契约包的单元测试 | Stats 序列化行为、SchemeFilter 匹配 |

## 4. 核心概念与源码讲解

### 4.1 Fetcher 接口：协议实现的生命周期契约

#### 4.1.1 概念说明

Gopeed 支持五种以上协议，每种协议的「资源发现」方式完全不同：HTTP 要发探测请求读响应头，BT 要解析种子文件，磁力链接要先拿元数据，HLS 要解析 m3u8 播放列表。但 Downloader 需要的是统一的任务管理：解析、启动、暂停、删除、统计、监控进度。

`Fetcher` 接口就是这两者之间的抽象层：**每个下载任务对应一个 Fetcher 实例**，Downloader 持有它并按固定顺序调用其方法，协议差异被完全封装在实现内部。接口只有十个方法，却覆盖了一个下载任务从「探测资源」到「传输完成」的完整生命周期。

#### 4.1.2 核心流程

Downloader 调用 Fetcher 方法的完整时序（以「先解析后创建」的路径为例）：

```text
用户提交 URL
    │
    ▼
Downloader.Resolve ──────────► fetcher.Setup(ctl)     注入 Controller（buildFetcher 内）
    │                          fetcher.Resolve(req)   探测资源 → 填充 Meta.Res
    ▼
Downloader.CreateWithOptions ► fetcher.Meta().Opts    写入任务选项
    │                          fetcher.Patch(nil, {Size})  回填大小估计
    ▼
Downloader.doStart（异步 handler，持 task.lock）
    │                          fetcher.Resolve(req)   仅当 Res 为 nil（CreateDirect 延迟解析）
    │                          prepareTask            决定目录与最终文件名
    │                          fetcher.Start()        启动传输协程
    ▼
go Downloader.watch ─────────► 循环 fetcher.Wait()    阻塞等待完成/失败
    │                                │
    │                err == nil ─────┼──── err != nil
    │                    │           │
    │              markTaskDone   doOnError（可触发 fetcher.Patch 换 URL 后重试）
    ▼
用户暂停/调度压制 ────────────► fetcher.Pause()（runPauseHandler）
用户删除任务 ────────────────► fetcher.Close()（doDelete）
```

三条并行的读取通道不参与状态迁移，只做观测：

- `fetcher.Progress()`——每 350ms（RefreshInterval）被速度刷新协程调用一次；
- `fetcher.Stats()`——在启动、暂停、完成、失败、查询时被 `captureTaskStats` 调用；
- `fetcher.Meta()`——被大量读写点使用，是双方共享的「数据黑板」。

#### 4.1.3 源码精读

接口定义本身非常精炼：

[internal/fetcher/fetcher.go:L11-L33](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L11-L33) —— Fetcher 接口的完整定义：前六个方法（Setup/Resolve/Start/Patch/Pause/Close）是生命周期动词，后四个（Stats/Meta/Progress/Wait）是观测与同步原语。注意 `Patch` 的注释直接点明了协议差异：HTTP 用来改请求信息，BT 用来改文件选择。

下面逐个方法给出「Downloader 侧调用点」。**这张表是本讲最重要的产出，建议亲手核对每一个链接**：

| Fetcher 方法 | 调用方（pkg/download/downloader.go） | 调用时机 |
|---|---|---|
| `Setup(ctl)` | `setupFetcher`（[L455-L487](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L455-L487) 中 L486 调用） | 三个入口都会调用：`buildFetcher`（新建，L2301-L2309）、`restoreFetcher`（重启恢复，L1797-L1820）、`resetTaskFetcher`（错误重置，L1749-L1786）。构造出实例后立即注入 Controller（配置读取、代理、临时文件、日志等回调） |
| `Resolve(req)` | ① `Downloader.Resolve`（[L540-L576](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L540-L576) 中 L563）；② `doStart` 的 handler（[L2001-L2120](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2001-L2120) 中 L2055，条件 `task.Meta.Res == nil`） | ① 解析 API 路径（先 Resolve 后 Create）；② `CreateDirect` 直达路径把解析推迟到任务启动时（`resolvedHere` 分支） |
| `Start()` | `doStart` 的 handler（同上，L2090） | 状态切到 running、目录准备（prepareTask）完成之后；`started = true` 标记成功启动，随后才 `go d.watch(task)` |
| `Patch(req, opts)` | ① `CreateWithOptions`（[L662-L711](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L662-L711) 中 L697，req 传 nil）；② `Downloader.Patch`（[L716-L758](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L716-L758) 中 L739）；③ `handleOnError`（[L1696-L1747](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1696-L1747) 中 L1720） | ① 建任务时把用户提交的大小估计推给协议；② 用户显式调用 Patch API；③ 扩展在 onError 钩子里替换了过期 URL 后，经 Patch 通知 fetcher 重置重定向状态 |
| `Pause()` | `runPauseHandler`（[L2178-L2207](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2178-L2207) 中 L2186） | 两个上游：`doPause`（用户暂停，L2122-L2137）与 `doPauseForScheduling`（MaxRunning 满时把任务压回 wait，L2141-L2157）。`Downloader.Close` → `pauseAll` 最终也走这里 |
| `Close()` | `doDelete`（[L1184-L1222](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1184-L1222) 中 L1198） | 删除任务时释放协议资源（注意：进程关闭走的是 `FetcherManager.Close`，见 `Downloader.Close` L1242-L1244，不逐个调 fetcher.Close） |
| `Stats()` | `captureTaskStats`（[L1024-L1084](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1024-L1084) 中 L1028） | 五个上游：`Stats` API 查询（L991-L1015）、`doStart` 启动后（L2094）、`runPauseHandler` 暂停后（L2191）、`watch` 完成时（L1586）、`handleOnError` 失败时（L1731） |
| `Meta()` | `Resolve`（L572-L573）、`createDirect`（L624）、`CreateWithOptions`（L678-L692）、`doStart`（L2046-L2048）、`setupFetcher` 的 GetProxy 回调（L471）等 | 读写共享元数据的 everywhere 方法，详见 4.2 |
| `Progress()` | ① 速度刷新协程（L325，位于 Setup 启动的 "calculate download speed every tick" 循环）；② `RuntimeStatus`（[L1125-L1182](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1125-L1182) 中 L1161）；③ `markTaskDone`（[L2236-L2264](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2236-L2264) 中 L2249） | ① 周期性算速度；② 高频详情轮询接口；③ 任务完成时若 Size 为 0，用实际下载字节数回填总大小 |
| `Wait()` | `watch` 协程（[L1517-L1659](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1517-L1659) 中 L1548，位于 `for` 循环内） | 每个运行中的任务一个 watch 协程；`Wait` 返回 nil → `markTaskDone`，返回 err → `doOnError`；若期间状态已被并发改为 pause/wait 则直接退出（L1550-L1553） |

两个容易忽略的细节：

1. **`Resolve` 可能被调用两次**（不同实例上）：第一次在 `Downloader.Resolve` 的临时实例上（结果缓存在 `fetcherCache`），第二次在 `doStart` 中当 `task.Meta.Res == nil` 时。后者正是 `CreateDirect`「先建任务、后解析」的路径——CLI 和 Boot API 默认走这条路。
2. **`Pause` 的返回时机是「传输协程完全停止」之后**。以 HTTP 为例，`Pause` 会等待 downloadLoop 退出并 `wg.Wait()` 所有连接协程（见 4.4.3），因此 Downloader 才能安全地在 Pause 返回后立即做 `saveTask` 落盘。

接口旁边还有一个可选扩展：

[internal/fetcher/fetcher.go:L43-L47](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L43-L47) —— `Uploader` 是可选接口，只有做种需求的协议（BT）实现。Downloader 在 `watch` 中用类型断言探测（downloader.go L1527），在启动恢复时调用 `Upload()`（downloader.go L283-L287）。

#### 4.1.4 代码实践：为 Fetcher 接口写方法级中文文档

**实践目标**：把 4.1.3 的表格变成你自己验证过的文档，确保每个「调用方」都能在源码中指认。

**操作步骤**：

1. 打开 `pkg/download/downloader.go`，用编辑器搜索 `fetcher.Resolve(`、`fetcher.Start(`、`fetcher.Patch(`、`fetcher.Pause()`、`fetcher.Close()`、`fetcher.Stats()`、`fetcher.Progress()`、`fetcher.Wait()`，逐一确认调用者函数名与行号。
2. 对每个调用点回答三个问题：调用时任务处于什么状态？调用方持有哪些锁（`task.lock`？`statusLock`？）？调用失败会发生什么？
3. 把结果整理成「方法 → 调用方 → 时机 → 失败后果」四列的表格，存入你自己的学习笔记。
4. 运行契约包的现有测试，确认环境可用（不修改任何源码）：

   ```bash
   go test ./internal/fetcher/ -v
   ```

**需要观察的现象**：搜索结果应与 4.1.3 表格一致——例如 `fetcher.Start(` 在整个 downloader.go 中只有一个调用点（doStart 的 handler 内）；`fetcher.Pause()` 也只有一个（runPauseHandler 内）。测试应输出 `TestStatsMarshalRawSnapshotWithoutRuntime` 与 `TestSchemeFilter_Match` 通过（具体输出待本地验证）。

**预期结果**：你会得到一张经过源码核对的调用关系表，并能说出「Downloader 对 Fetcher 的每一次调用都发生在确定的锁与状态约束下」——这是理解后续并发讲义（u11-l2）的地基。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fetcher.Resolve()` 在 `doStart` 里被调用前要先判断 `task.Meta.Res == nil`？

**参考答案**：因为存在两条建任务路径。「先解析后创建」路径（Resolve API + Create）在解析阶段已经调过 `Resolve` 并填充了 `Meta.Res`；而 `CreateDirect`（CLI/Boot API）为了尽快返回任务 ID，把解析推迟到任务启动时。`resolvedHere := task.Meta.Res == nil`（downloader.go L2053）正是区分这两种情况：只有从未解析过的任务才在启动时补一次 `Resolve`，解析完成后还要把结果写回 `task.Meta.Res`（L2059），供 watch 循环使用。

**练习 2**：`Downloader.Close()` 关闭整个下载器时，会逐个调用每个任务的 `fetcher.Close()` 吗？

**参考答案**：不会。`Downloader.Close`（L1224-L1258）的清理列表是：`pauseAll`（对每个任务走 `doPause` → `fetcher.Pause()`，而非 Close）、逐个 `FetcherManager.Close()`、blob、存储、临时文件。逐任务的 `fetcher.Close()` 只在 `doDelete`（删除单个任务）时调用。也就是说：暂停用 Pause，删除才用 Close；进程级关闭复用暂停语义，把「彻底释放」交给各协议的 Manager。

**练习 3**：`Progress()` 与 `Stats()` 都返回进度类信息，为什么拆成两个方法？

**参考答案**：两者的调用频率与代价完全不同。`Progress()` 只是「每个文件已下载字节数」的轻量求和，被 350ms 一次的速度循环高频调用（HTTP 实现 L2588-L2604 只做加法）；`Stats()` 可能包含昂贵信息（BT 要构造 piece 位图与 peer 列表，bt/fetcher.go L246-L282），只在启动/暂停/完成/失败/查询等低频时机调用，且结果一半会被持久化。拆开后高频路径不为低频数据付费。

### 4.2 FetcherMeta：一次下载的三元组与路径计算

#### 4.2.1 概念说明

`FetcherMeta` 只有三 个字段——`Req`（请求）、`Res`（资源）、`Opts`（选项），正是 u2-l1 讲过的「下载三要素」。它挂在 Fetcher 上（`Meta()` 返回指针），却同时被 Downloader 读写，本质是一块**共享黑板**：

- Resolve 阶段 Fetcher 写入 `Res`（探测结果）；
- Create 阶段 Downloader 写入 `Opts`（用户选择）；
- doStart 阶段 Downloader 把 Task 保存的三元组回灌给（可能被 Restore 重建的）Fetcher；
- 路径计算、文件删除、脚本载荷等下游逻辑全部读这块黑板。

#### 4.2.2 核心流程

`FetcherMeta` 提供三个路径推导方法，决策树如下：

```text
Resource.Name 是否非空？（多文件资源如 BT 种子有根名，单文件 HTTP 没有）
├── 是 → 目录型任务
│     ├── FolderPath()     = Join(Opts.Path, Opts.Name 或 Res.Name)   任务根目录
│     └── RootDirPath()    = FolderPath()
└── 否 → 单文件任务
      ├── SingleFilepath() = Join(Opts.Path, Files[0].Path, Opts.Name 或 Files[0].Name)
      └── RootDirPath()    = Opts.Path
```

`Opts.Name` 非空时优先于资源自带名字——这就是「重命名下载」的实现机制。

#### 4.2.3 源码精读

[internal/fetcher/fetcher.go:L49-L54](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L49-L54) —— FetcherMeta 结构：三个指针字段，序列化标签让它们可以直接 JSON 落盘（Restore 时由 Manager 的反序列化器重建）。

[internal/fetcher/fetcher.go:L57-L75](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L57-L75) —— `FolderPath` 与 `SingleFilepath` 的实现。注意两者都先检查 `Opts.Name` 重命名，再回退到资源自带名字；`SingleFilepath` 还会拼接 `file.Path`（文件在资源内部的相对目录）。

[internal/fetcher/fetcher.go:L78-L84](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L78-L84) —— `RootDirPath`：有根名返回 FolderPath，无根名返回下载目录本身（BT 无根名种子、纯单文件都属于后者）。

Downloader 侧对这块黑板的三个典型操作：

- **写入**：[pkg/download/downloader.go:L2045-L2049](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2045-L2049)（doStart 中）——每次启动都把 Task 持久化的三元组回灌给 fetcher，保证「重启恢复的 Fetcher」与「Task 快照」一致。restoreFetcher（[L1797-L1820](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1797-L1820)）在重建后也做同样的 nil 检查补写。
- **读路径（删除文件）**：[pkg/download/downloader.go:L1202-L1212](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1202-L1212)（doDelete 中）——`force` 删除时按 `Res.Name` 是否非空选择删整目录（FolderPath）还是删单文件（SingleFilepath）。
- **读路径（对外服务）**：[pkg/rest/server.go:L470-L473](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/rest/server.go#L470-L473) —— Web 端任务文件服务以 `RootDirPath()` 为根暴露任务文件。

HTTP Fetcher 自己也是路径计算的消费者：[internal/protocol/http/fetcher.go:L850-L863](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L850-L863)（doStart 中）用 `f.meta.SingleFilepath()` 定位目标文件并用 `ctl.Touch` 预分配大小。

#### 4.2.4 代码实践：追踪路径计算的全部调用点

**实践目标**：确认 `FolderPath/SingleFilepath/RootDirPath` 的消费者清单，理解「一块黑板多方读取」的辐射面。

**操作步骤**：

1. 在仓库根目录执行（只读检索，不改代码）：

   ```bash
   grep -rn --include='*.go' -E '\.(FolderPath|SingleFilepath|RootDirPath)\(' pkg internal | grep -v _test.go
   ```

2. 把每个命中点归类到：删除清理 / 目录准备（prepareTask）/ 下载目标定位 / 对外文件服务 / 脚本与 webhook 载荷 / 自动解压。

**需要观察的现象**：命中应集中在 `pkg/download/downloader.go`（删除 L1204/L1208、prepareTask L1964/L1972、解压 L2425/L2440、镜像重置 L1764）、`pkg/download/script.go`（L115/L118）、`pkg/rest/server.go`（L472）以及各协议 fetcher 内部（如 ftp/fetcher.go L236-L239）。

**预期结果**：你会看到同一份 `FetcherMeta` 至少被六类逻辑消费，从而理解为什么 doStart 每次启动都要回灌三元组——任何一处读到 nil 的 Res/Opts 都会崩溃或删错文件。（命令输出待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：一个 BT 种子包含根目录 `Ubuntu/` 和三个文件，用户下载时把 `Opts.Name` 设为 `"my-ubuntu"`，`FolderPath()` 返回什么？

**参考答案**：`path.Join(Opts.Path, "my-ubuntu")`。`FolderPath` 的重命名检查（fetcher.go L60-L62）优先取 `Opts.Name`，仅在为空时才用 `Res.Name`。这意味着重命名会改变任务根目录本身，而不是目录内的文件名。

**练习 2**：为什么 `RootDirPath()` 在 `Res.Name` 为空时要退回 `Opts.Path` 而不是返回空字符串？

**参考答案**：因为无根名的资源（无根目录的种子、单文件 HTTP 下载）没有「任务专属子目录」，任务的文件直接散落在下载目录 `Opts.Path` 下。REST 的任务文件服务（rest/server.go L472）需要一个统一的「任务文件根」来拼接安全路径，退回 `Opts.Path` 让两类资源可以共用同一套路径逻辑。

### 4.3 Stats 双负载：Snapshot 与 Runtime 的二分设计

#### 4.3.1 概念说明

`Stats` 回答「这个任务现在/最终处于什么协议细节状态」。设计者把协议信息切成两半：

- **Snapshot（快照）**：任务停止后仍有意义、需要跨进程持久化的部分。HTTP 的连接明细、BT 的 piece 完成位图与做种统计都属于此类——重启后你仍想知道「上次下到哪些 piece 了」。
- **Runtime（运行时遥测）**：只在 fetcher 活着时有效的东西。BT 当前连接的 peers 列表、下载速率都是实例一关就作废的数据。

Downloader 对两者区别对待：**只有 Snapshot 落盘（bucketTaskStats 桶），Runtime 随查询实时返回、任务失活即丢弃**。协议拥有两个负载的 schema，Downloader 把它们当不透明值处理——这是「Downloader 不依赖任何具体协议包」的又一体现（u1-l3 的分层原则）。

#### 4.3.2 核心流程

`captureTaskStats` 的裁剪与落盘逻辑：

```text
captureTaskStats(task, live, force)
    │
    ├─ stats = fetcher.Stats()          ← 协议返回双负载
    │
    ├─ 若 !live（任务已不在运行/上传）:
    │      stats = Stats{Snapshot: stats.Snapshot}   ← Runtime 直接丢弃
    │
    ├─ Snapshot == nil ?
    │      是 → 保留上一次可信快照，只刷新 Runtime（BT 校验期专用语义）
    │      否 → JSON 编码，与已存字节比较，无变化且 !force 则跳过写盘
    │
    └─ 写入 bucketTaskStats 桶 {taskID: {"snapshot": <raw>}}
```

HTTP 的连接份额计算还带一个小数学关系——n 个连接均摊总大小，向上取整避免提前报 100%：

\[ \text{Total}_\text{conn} = \left\lceil \frac{\text{Res.Size}}{n} \right\rceil \]

#### 4.3.3 源码精读

[internal/fetcher/fetcher.go:L35-L41](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L35-L41) —— Stats 结构与注释：明确写了「Downloader 持久化 Snapshot、只在内存保留 Runtime；协议拥有两个 payload schema」。

[pkg/download/downloader.go:L1017-L1019](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1017-L1019) —— 落盘形态 `storedTaskStats` 只有 Snapshot 一个字段，`json.RawMessage` 类型避免二次编解码（对应注释见 loadTaskStats L1086-L1088）。

[pkg/download/downloader.go:L1028-L1034](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1028-L1034) —— 双负载的分水岭：`if !live { stats = &fetcher.Stats{Snapshot: stats.Snapshot} }`，一行代码完成「Runtime 失活即弃」。

[pkg/download/downloader.go:L1055-L1071](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1055-L1071) —— nil Snapshot 的语义：协议「暂时无法给出可信快照」时（例如 BT 恢复存储校验 piece 期间），Downloader 保留旧快照而不是用空值覆盖，注释里写明了这个场景。

两个协议的实现对读：

[internal/protocol/http/fetcher.go:L2520-L2570](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2520-L2570) —— HTTP 的 Stats：只填 Snapshot（`&fhttp.Stats{Connections: [...]}`，每连接含 Downloaded/Total/Completed/Failed/RetryTimes），Runtime 留空。特例：终态且无连接但预取已覆盖全量大小时，合成一条聚合连接（L2527-L2539）。

[internal/protocol/bt/fetcher.go:L246-L282](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/bt/fetcher.go#L246-L282) —— BT 的 Stats：Snapshot 是做种统计 + PieceMap（未就绪时为 nil，L260-L268），Runtime 是 peers 明细（TotalPeers/ActivePeers/ConnectedSeeders/Peers 列表）。BT 是唯一两个负载都填的协议。

序列化行为有测试背书：

[internal/fetcher/fetcher_test.go:L8-L20](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher_test.go#L8-L20) —— 断言 `Stats{Snapshot: RawMessage({"value":1})}` 序列化为 `{"snapshot":{"value":1},"runtime":null}`，证明 Runtime 为 nil 时合法且不丢字段。

#### 4.3.4 代码实践：观察 Snapshot/Runtime 的序列化与裁剪

**实践目标**：用现有测试验证双负载的序列化行为，并整理出两协议的字段归属表。

**操作步骤**：

1. 运行契约测试并阅读断言：

   ```bash
   go test ./internal/fetcher/ -run TestStatsMarshalRawSnapshotWithoutRuntime -v
   ```

2. 阅读上面四个代码链接，填写下面的表格（示例答案已给出一半）：

| 字段 | HTTP 归属 | BT 归属 | 是否落盘 |
|---|---|---|---|
| 连接明细（下载量/重试/失败） | Snapshot | —（BT 无连接概念） | 是 |
| piece 完成位图 | — | Snapshot | 是 |
| 做种字节/分享率/做种时长 | — | Snapshot | 是 |
| 当前 peers 列表 | — | Runtime | 否 |
| 上传/下载速率 | — | Runtime | 否 |

3. 思考并验证：如果把 BT 的 peers 移进 Snapshot 会发生什么？（提示：每次 watch 触发 `captureTaskStats` 都会编码比较，L1075-L1077。）

**需要观察的现象**：测试通过且输出包含期望 JSON；表格能完整覆盖两协议 Stats 的全部字段。

**预期结果**：`runtime:null` 出现在序列化结果中（测试断言的 want 字符串即为此，运行输出待本地验证）；你会得出结论——Runtime 若落盘会造成高频无意义写盘，且数据在重启后立即失效。

#### 4.3.5 小练习与答案

**练习 1**：为什么 HTTP 的 Stats 不填 Runtime，功能却不缺失？

**参考答案**：HTTP 的「运行时观测」需求（已下载多少、速度多少）由更轻的 `Progress()` 通道承担（速度循环 350ms 调用），连接明细这类稳定信息则直接属于 Snapshot。BT 因为有 peers、piece 这类只有它能提供的信息，才需要 Runtime 通道。

**练习 2**：`captureTaskStats` 中 `if !bytes.Equal(snapshot, task.statsSnapshot) { return nil }`（L1075-L1077）这段「无变化跳过写盘」保护了什么？

**参考答案**：`Stats()` 在任务启动、暂停、完成、失败、每次 API 查询时都会被调用，而 HTTP Snapshot 的内容（各连接下载量）在任务暂停后不再变化。字节比较使得暂停后的重复调用不会反复写 bbolt，减少存储磨损与锁竞争；`force` 参数（完成/暂停/失败时为 true）则保证关键节点即使内容相同也强制落盘一次。

### 4.4 DefaultFetcher 基类与 DoneCh 完成通知

#### 4.4.1 概念说明

`Wait()` 是 Fetcher 接口里唯一的「阻塞同步」方法：Downloader 的 watch 协程靠它等待传输结束。问题在于——Downloader 怎么知道结束了？协议实现怎么通知？

`DefaultFetcher` 给出的默认答案是 `DoneCh chan error`：一个容量为 1 的错误通道。传输协程完成时向通道发送结果（nil 表示成功），`Wait()` 阻塞接收。它是一个可内嵌的「半成品基类」，替简单协议省去手写通知机制。

但它是**可选的**：HTTP 与 BT 都没有内嵌它，而是各自实现了更贴合自身模型的 `Wait()`——这正是 Go「接口满足即可，不强制继承」的风格。

#### 4.4.2 核心流程

DoneCh 生命周期（以使用基类的协议为例）：

```text
Setup(ctl)
    └─ DoneCh = make(chan error, 1)        容量 1：发送方永不阻塞
传输协程结束
    └─ DoneCh <- err（或 select 非阻塞发送）
watch 协程
    └─ err := <-DoneCh ──────────────────── 阻塞直到有结果
           nil → markTaskDone → done 事件
           err → doOnError → error 事件（可重试则回到运行）
```

容量 1 + 非阻塞发送组合的动机：**发送方永远不能因为没人接收而卡死传输协程**。watch 协程可能已被销毁（任务被暂停、删除），此时结果留在缓冲里自然丢弃；下个生命周期开始前，实现方负责清空残留。

#### 4.4.3 源码精读

[internal/fetcher/fetcher.go:L160-L174](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L160-L174) —— DefaultFetcher：三个字段（Ctl/Meta/DoneCh），Setup 初始化通道，Wait 阻塞接收。共 15 行，就是一个「带通道的骨架」。

唯一的使用者是 HLS：

[internal/protocol/hls/fetcher.go:L119-L156](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/hls/fetcher.go#L119-L156) —— HLS Fetcher 内嵌 `fetcher.DefaultFetcher`，其 Setup 第一行调用 `f.DefaultFetcher.Setup(ctl)`，注释写明「初始化 Ctl 与 DoneCh，Wait() 依赖它们」。

HTTP 自己维护同构机制（字段名就叫 doneCh）：

- 创建：[internal/protocol/http/fetcher.go:L346-L348](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L346-L348)（Setup 中 `make(chan error, 1)`）；
- 发送：[internal/protocol/http/fetcher.go:L2270-L2284](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2270-L2284)（onDownloadComplete 尾部）——先校验和、再设状态，最后 `select { case f.doneCh <- finalErr: default: }` 非阻塞发送；
- 清空：[internal/protocol/http/fetcher.go:L807-L815](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L807-L815)（doStart 开头）——从 paused/error 恢复时先排空通道里上一轮周期的残留结果，注释「每个 Start/Wait 周期只能观察到自己的完成结果」点明了竞态防护；
- 接收：[internal/protocol/http/fetcher.go:L2606-L2608](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2606-L2608)（Wait）。

BT 走了第三条路——轮询而非通道：

[internal/protocol/bt/fetcher.go:L468-L490](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/bt/fetcher.go#L468-L490) —— BT 的 Wait 用 1 秒 ticker 循环检查 `finishDownload()`（选中文件全部完成后顺带清理未选文件，L492-L512）或 torrent 关闭事件。原因：anacrolix/torrent 库没有单一「完成点」，完成状态是「所有选中 piece 就绪」的聚合判断，轮询是最自然的表达。

消费端在 watch：

[pkg/download/downloader.go:L1547-L1567](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1547-L1567) —— `for { err := task.fetcher.Wait() ... }`：err 非 nil 时先检查任务是否已被并发暂停（是则安静退出，L1550-L1553），否则进入错误处理；err 为 nil 走 markTaskDone 完成链路。**Fetcher 汇报、Downloader 裁决**——状态迁移权始终在 Downloader 手里，呼应 u2-l2 的结论。

#### 4.4.4 代码实践：追踪一次 DoneCh 的完整旅程

**实践目标**：沿 HTTP 一次「暂停→继续→完成」的过程，指出 DoneCh 在四个关键点的代码位置与作用。

**操作步骤**（纯源码阅读）：

1. 找到 DoneCh 的**创建点**（提示：Setup，L348）。
2. 找到**发送点**（提示：onDownloadComplete 末尾的 select，L2281-L2284），注意它为什么用 `default` 分支。
3. 找到**清空点**（提示：doStart 从 paused/error 状态进入时，L810-L815），思考「不清空会怎样」。
4. 找到**消费点**（提示：watch 的 for 循环，downloader.go L1548），以及消费到 nil 之后 Downloader 做的第一件事（L1583 markTaskDone）。
5. 对照 BT 的 Wait（L468-L490），回答：如果 BT 也改用容量 1 的 DoneCh，`finishDownload` 里「清理未选文件」的失败该如何上报？

**需要观察的现象**：四个点全部定位成功；能口述「容量 1 + 非阻塞发送 + 周期开始前排空」三件套各自防住的竞态。

**预期结果**：发送点非阻塞保证传输协程不被已退出的 watch 卡死；清空点保证继续任务后 Wait 不会立刻读到上一轮的过期结果；消费点拿到 nil 后由 markTaskDone 在 running 守卫下唯一地写入 done 终态。第 5 步的思考题答案是开放的——轮询模型里错误就是返回值，而通道模型需要把「清理失败」编码成一次 err 发送，这正是 BT 选择轮询的原因之一。

#### 4.4.5 小练习与答案

**练习 1**：`DefaultFetcher.Setup` 里 `make(chan error, 1)` 的容量 1 能改成无缓冲吗？

**参考答案**：不能。无缓冲通道的发送会阻塞直到接收方就绪。而发送 DoneCh 结果的时机在传输协程内部——如果此刻 watch 协程已经退出（例如任务在传输完成的同一瞬间被暂停/删除，或错误路径无人再调用 Wait），发送方将永久阻塞并泄漏协程。容量 1 让「结果没人收」退化为「结果被丢弃」，代价只是要小心处理残留（HTTP 的清空点就是为此存在）。

**练习 2**：watch 循环里 `err := task.fetcher.Wait()` 返回 err 后，为什么要先查 `d.taskStatus(task)` 再决定是否报错（L1550-L1553）？

**参考答案**：因为「传输结束」与「用户暂停」可能并发。Pause 已把任务状态改为 pause 并调用了 fetcher.Pause()，fetcher 的传输协程随后以「被取消」类错误结束并发出结果；此时任务在用户视角是「已暂停」而非「失败」。若不检查就会把一次正常暂停误报成 error 事件。检查到 pause/wait 状态时安静返回，让暂停语义完整——这也是 u2-l2 讲过的「状态唯一写入者是 Downloader」的又一次体现。

## 5. 综合实践

把本讲四个模块串成一份**《Fetcher 接口契约文档》**，这也是本讲规格中指定的实践任务。完成品应包含三部分：

**第一部分：方法级中文文档（4.1 的深化）**。以表格形式列出 Fetcher 全部十个方法，每行包含：方法签名、中文职责说明、Downloader 调用函数名（精确到函数）、调用时机、锁/状态前置条件、失败后果。以 `Start()` 一行为例的参考答案：

> `Start() error`——启动数据传输。由 `doStart` 的异步 handler 调用（downloader.go L2090），前置条件：任务已置为 running、runGeneration 已递增、`prepareTask` 已决定目录与最终文件名；调用点持有 `task.lock`（AutoRename 场景下还持有 `d.checkDuplicateLock`）；失败则 handler 返回错误，由 `doOnError` 走 error 状态并可能触发扩展 onError 钩子换 URL 重试。

其余九行请按 4.1.3 的表格与链接自行补全。

**第二部分：HTTP 与 BT 的实现差异对比表**（4.1/4.3 的综合）。参考答案：

| 维度 | HTTP（internal/protocol/http/fetcher.go） | BT（internal/protocol/bt/fetcher.go） |
|---|---|---|
| Resolve 职责 | 发不带 Range 的探测请求；解析文件名（Content-Disposition → URL 路径 → 主机名三级回退）、大小（Content-Length 或 206 的 total）、Range 支持、If-Range 校验器；顺手启动 asyncPrefetch 预取数据（L419 起） | addTorrent 解析 .torrent/磁力；updateRes 生成多文件 Resource（含 InfoHash、逐文件 Path/Size），Range 恒为 true（L103-L118） |
| Resolve 是否产生数据传输 | 是（预取前缀，Start 时无缝接管，免二次请求） | 否（磁力链接甚至可能拿不到元数据） |
| Patch 可修改项 | req.URL（同时清空重定向缓存）、Method/Body/Header 合并、Labels、Proxy、opts.Size 大小估计（L2371-L2430） | 仅 opts.SelectFiles：先 CancelPieces 全部取消，再按新选择 DownloadAll 或逐文件 Download，CalcSize 重算大小并重置 Progress（L594-L648） |
| Stats Snapshot | 连接明细列表，Total 按均摊公式向上取整（L2520-L2570） | 做种统计 + PieceMap 位图，校验期可为 nil（L246-L282） |
| Stats Runtime | 无 | peers 明细（数量/速度/完成度/来源） |
| Wait 实现 | `<-f.doneCh`，容量 1 通道 + 周期开始前排空（L2606/L807-L815） | 1 秒 ticker 轮询 finishDownload + torrent.Closed（L468-L490） |
| Pause 语义 | cancel 两个 context → 等 downloadLoop 退出 → wg.Wait 所有连接 → 落盘预取（L2432-L2506） | 仅 DisallowDataDownload 一行，torrent 会话保留以便继续做种（L211-L219） |

**第三部分：一致性自查**。回到 4.1.2 的时序图，检查你的表格中每个调用点是否都能落进图中某个环节；再挑 `Patch` 一列做实地验证：用 u1-l4 的 Boot API 写 10 行程序，对一个运行中的 BT 任务调用 `Downloader.Patch` 追加选择一个文件，观察任务 Size 变化是否与「CalcSize 重算」的描述一致（涉及真实种子网络，结果待本地验证）。

## 6. 本讲小结

- `Fetcher` 是协议接入的唯一契约：六个生命周期方法（Setup/Resolve/Start/Patch/Pause/Close）加四个观测同步方法（Stats/Meta/Progress/Wait）；Downloader 是唯一调用方，每个调用点都有确定的函数、锁与状态前置条件。
- `Resolve` 有两条触发路径：解析 API 的即时路径与 `CreateDirect` 延迟到 `doStart` 的路径；`Patch` 有三个触发方：建任务回填 Size、用户 Patch API、扩展恢复 URL。
- `FetcherMeta`（Req/Res/Opts）是 Fetcher 与 Downloader 的共享黑板，doStart 每次启动回灌三元组；`FolderPath/SingleFilepath/RootDirPath` 三个路径方法按「资源有无根名」分流，重命名通过 `Opts.Name` 优先级实现。
- `Stats` 二分为 Snapshot（落盘、跨重启有意义）与 Runtime（仅内存、失活即弃）；nil Snapshot 是「暂不可信」信号，Downloader 会保留旧值；Downloader 对两个 payload 保持协议无关。
- `DefaultFetcher` 是可选基类，核心是容量 1 的 DoneCh；HTTP 用同构的自管通道并加了「周期开始前排空」竞态防护，BT 因聚合完成语义改用轮询——三种 `Wait()` 策略展示了同一契约下的实现自由度。
- 状态裁决权始终在 Downloader：Fetcher 只通过 `Wait()` 的返回值汇报结果，watch 循环结合当前任务状态决定 done/error 还是安静退出。

## 7. 下一步学习建议

- 下一讲 **u3-l2（FetcherManager 与协议路由）**：本讲只回答了「Fetcher 生命周期怎么驱动」，还没回答「URL 怎么选中协议」。去读 `FetcherManager` 接口、`SchemeFilter.Match` 的三类匹配与 `DownloaderConfig.Init` 的注册顺序，你就补全了接入层的另一半。
- 如果你已急着想看协议内部，推荐先读 [internal/protocol/http/fetcher.go:L769-L796](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L769-L796) 的 `Start` 状态机分派，它是本讲「实现自有状态机」的最佳注脚，也是 u5 单元的入口。
- 想加深 Stats 印象的读者，可以预读 [internal/fetcher/fetcher_test.go:L22-L114](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher_test.go#L22-L114) 的 `TestSchemeFilter_Match` 表驱动测试——它是 u3-l2 实践的直接素材。
