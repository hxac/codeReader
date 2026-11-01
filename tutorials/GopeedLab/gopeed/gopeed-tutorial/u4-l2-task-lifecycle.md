# 主链路：Resolve → Create → doStart

## 1. 本讲目标

上一讲我们认识了 `Downloader` 的结构体与初始化，知道了八个存储桶、两级初始化（`NewDownloader` 搭内存骨架、`Setup` 恢复持久化世界）。本讲沿着最核心的一条线走通**一个下载任务从无到有、再到字节开始流动**的完整调用链：

- `Resolve` 如何解析资源并把它缓存在 `fetcherCache` 里，`ResolveResult` 到底返回了什么；
- `CreateDirect` 与 `Create`（`CreateWithOptions`）两条建任务路径的区别，`doCreate` 如何装配出 `Task` 骨架；
- `notifyRunning` 如何在 `MaxRunning` 满载时把任务排进 `waitTasks` 队列、又如何被唤醒；
- `doStart` 中「`statusMut` 同步段 + 异步 `handler`」的两段式结构，以及 `runGeneration` 代数在其中的作用；
- `prepareTask` 的一次性目录决策与重名改名机制，特别是 `checkDuplicateLock` 为什么必须包住 `fetcher.Start()`。

学完本讲，你应该能独立回答：**"一个 URL 交进来之后、第一个字节写进磁盘之前，Gopeed 内部究竟发生了什么？"**

## 2. 前置知识

本讲默认你已读过前置讲义的核心结论，这里只做要点回顾：

- **两阶段模型**：Gopeed 把一次下载拆成「解析（Resolve）」与「传输（Start）」两个阶段。解析产出 `Resource`（文件名、大小、文件列表），传输负责真正搬运字节。
- **Task 的公开层与运行时层**（u2-l2）：`Task` 的 JSON 序列化只带公开字段（ID、Meta、Status、Progress 等），fetcher、锁、timer、测速窗口等运行时字段不会落盘，恢复后由 `initTask` 重建。
- **状态的唯一写入者是 Downloader**（u2-l2 / u3-l1）：Fetcher 不改 `Task.Status`，只通过 `Wait()` 返回值汇报结果。
- **FetcherManager 是协议级单例门面，Fetcher 是任务级执行者**（u3-l2 / u3-l4）：`parseFm(url)` 按注册顺序 first-match-wins 选中协议管理器，`Build()` 造出任务专属的 Fetcher，`setupFetcher` 在 Build 之后注入 Controller。
- **runGeneration 代数**（u2-l2 提及）：每次任务从非 running 态进入 running 态时代数自增，旧协程代数不匹配时必须放弃操作，防止「暂停与完成竞态下旧协程覆盖新状态」。
- **锁的归属**（u4-l1）：任务列表与 `waitTasks` 归主锁 `d.lock`；解析缓存 `fetcherCache` 归 `fetcherMapLock`；任务自身字段归 `task.lock` / `task.statusLock`；重名检查归全局 `checkDuplicateLock`。

不熟悉并发术语的读者只需要记住一句话：**互斥锁（Mutex）保证同一时刻只有一个 goroutine 能进入临界区**，本讲出现的每把锁都对应一个"只能独占访问"的资源。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `pkg/download/downloader.go` | 下载引擎中枢 | `Resolve`、`CreateDirect`/`CreateWithOptions`/`doCreate`、`notifyRunning`、`doStart`、`prepareTask`、`restoreFetcher`、`saveTask` |
| `pkg/download/model.go` | Task 与 ResolveResult 模型 | `ResolveResult` 结构、`Task.NeedsPreparation`、`NewTask`、`updateStatus` |
| `pkg/download/category.go` | 自动分类与目录决策 | `decideDownloadDir`、`suggestedDownloadDir`（细节在 u10-l1 展开） |
| `pkg/download/event.go` | 事件定义 | 七种 `EventKey`、`Event` 结构 |
| `pkg/util/path.go` | 路径工具 | `CheckDuplicateAndRename` 重名改名算法 |
| `pkg/api/service.go` | 与传输无关的服务层 | `resolve` / `createTask` 两个 handler 如何分流两条建任务路径 |
| `pkg/download/downloader_test.go` | 下载器测试 | `TestDownloader_Create`、`TestDownloader_CreateRename` |
| `internal/test/httptest.go` | 测试用 HTTP 假服务器 | `StartTestFileServer` 与文件名常量 |

## 4. 核心概念与源码讲解

本讲的五个最小模块正好按执行顺序排列：`Resolve`（解析与缓存）→ `doCreate`（建任务）→ `notifyRunning`（排队）→ `doStart`（启动）→ `prepareTask`（启动中的一次性准备）。

### 4.1 Resolve 与 ResolveResult：解析阶段的缓存设计

#### 4.1.1 概念说明

用户提交一个 URL 后，UI 通常需要先展示"这要下载什么？多大？有哪些文件？"，等用户确认目录、勾选文件之后再真正创建任务。`Resolve` 就是这个"预览"步骤：它不建任务、不写磁盘，只做资源发现，并把解析结果**连同解析用的 Fetcher 实例**一起缓存起来，等待后续 `Create` 消费。

为什么缓存的不是 `Resource` 而是 Fetcher？因为 HTTP 协议在解析阶段可能已经建立了连接、甚至预取了部分数据（u5-l1 会讲 `asyncPrefetch`）。把 Fetcher 原封不动交给 `Create`，任务启动时可以**无缝接管解析阶段的成果，避免二次请求**。这是"解析结果缓存"设计里最值钱的部分。

#### 4.1.2 核心流程

```
Downloader.Resolve(req)
├── 1. rrId = nanoid()                  # 随机短 ID，作为缓存键
├── 2. ensureRequestRawURL(req)         # RawURL 为空则填成 URL
├── 3. res = triggerOnResolve(req)      # 扩展钩子（u9-l3 详述）
│      └── 若扩展直接产出 Resource → 返回 ResolveResult{ID: "", Res}
│          （没有缓存任何东西，后续只能走 CreateDirect）
├── 4. fetcher = buildFetcher(req.URL)  # parseFm 路由 → fm.Build() → setupFetcher 注入 Controller
├── 5. fetcher.Resolve(req)             # 协议各自的资源发现
├── 6. fetcherCache[rrId] = fetcher     # fetcherMapLock 保护
└── 7. 返回 ResolveResult{ID: rrId, Res: fetcher.Meta().Res,
                          SuggestedDownloadDir: 建议目录}
```

注意第 3 步的特殊分支：当 JS 扩展在 `onResolve` 钩子里直接改写并返回了完整资源时，`ResolveResult.ID` 是**空字符串**——此时没有走协议 Fetcher，自然也没有缓存可消费。

#### 4.1.3 源码精读

`Resolve` 主体（[pkg/download/downloader.go:L540-L576](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L540-L576)）：

```go
func (d *Downloader) Resolve(req *base.Request) (rr *ResolveResult, err error) {
	rrId, err := gonanoid.New()
	...
	ensureRequestRawURL(req)

	res, err := d.triggerOnResolve(req)   // 扩展钩子
	if res != nil && len(res.Files) > 0 { // 扩展直接产出资源：无缓存、无 ID
		rr = &ResolveResult{Res: res, SuggestedDownloadDir: d.suggestedDownloadDir(res)}
		return
	}

	fetcher, err := d.buildFetcher(req.URL)
	err = fetcher.Resolve(req)
	d.fetcherMapLock.Lock()
	d.fetcherCache[rrId] = fetcher       // 缓存的是 Fetcher 实例本身
	d.fetcherMapLock.Unlock()
	rr = &ResolveResult{ID: rrId, Res: fetcher.Meta().Res, ...}
	return
}
```

`buildFetcher`（[pkg/download/downloader.go:L2301-L2309](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2301-L2309)）三步走：`parseFm(url)` 按注册顺序匹配 Filter 选中协议管理器（[pkg/download/downloader.go:L444-L453](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L444-L453)），`fm.Build()` 造出任务级 Fetcher，`setupFetcher` 注入 Controller（u3-l3 详述）。

`ResolveResult` 定义（[pkg/download/model.go:L22-L30](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L22-L30)）：

```go
type ResolveResult struct {
	ID  string         `json:"id"`
	Res *base.Resource `json:"res"`
	// SuggestedDownloadDir ... It is a hint for the ui,
	// the create call decides the final path.
	SuggestedDownloadDir string `json:"suggestedDownloadDir,omitempty"`
}
```

字段注释明确说了：`SuggestedDownloadDir` 只是给 UI 的**提示**，最终目录由 create 调用决定。它由 `suggestedDownloadDir`（[pkg/download/category.go:L72-L86](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/category.go#L72-L86)）计算——只报告 `decideDownloadDir` 会做出的分类决策，不展开占位符、不写任何东西。

服务层的分流入口在 `pkg/api/service.go`：`resolve` handler 调 `Downloader.Resolve`（[pkg/api/service.go:L141-L151](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/api/service.go#L141-L151)），而 `createTask` handler 按请求体里是 `rid` 还是 `req` 分成两条路径（[pkg/api/service.go:L162-L167](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/api/service.go#L162-L167)）：

```go
if request.Rid != "" {
	taskID, err = s.Downloader.CreateWithOptions(request.Rid, request.Opts)
} else if request.Req != nil {
	taskID, err = s.Downloader.CreateDirect(request.Req, request.Opts)
}
```

还有一个值得注意的细节：`fetcherCache` 的删除点全项目只有一处——`CreateWithOptions` 的 `defer`（后面 4.2.3 会看到）。也就是说，**只 Resolve 不 Create 的解析结果会一直留在内存里**。这是一个可以用 Grep 自行验证的事实（在 `pkg/download` 内搜 `fetcherCache`，命中仅有写入、读取、删除三个调用点）。

#### 4.1.4 代码实践

**实践目标**：验证「Resolve 缓存的是活 Fetcher，且只有 Create 才会消费它」。

**操作步骤**（源码阅读型，配合 Grep）：

1. 在 `pkg/download` 目录下搜索 `fetcherCache`，列出所有读写点；
2. 打开测试 `TestDownloader_Resolve`（[pkg/download/downloader_test.go:L241-L272](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L241-L272)），观察它 Resolve 之后检查了 `rr.Res` 的哪些字段；
3. 阅读 `TestDownloader_Create`（[pkg/download/downloader_test.go:L767-L802](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L767-L802)），注意它 `Resolve` → `CreateWithOptions(rr.ID, opts)` → 等 `EventKeyDone` → 校验文件 MD5 的完整流程。

**需要观察的现象**：`fetcherCache` 的读点（`CreateWithOptions`）和删点（同一个函数的 defer）在同一个函数里成对出现。

**预期结果**：你能说出"Resolve 与 Create 必须成对调用，且用同一个 `rr.ID`"，以及"扩展短路路径返回的 `ResolveResult.ID` 为空，只能配 `CreateDirect`"。

（本实践为源码阅读型，无需运行命令。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ResolveResult.SuggestedDownloadDir` 要注明"只是提示，create 才决定最终路径"？

**参考答案**：Resolve 与 Create 是两次独立调用，中间用户可能改配置、改目录、换选项；而且 `suggestedDownloadDir` 只做分类匹配与白名单校验，不展开 `%year%` 之类占位符。真正的目录决策发生在任务启动前的 `prepareTask` → `decideDownloadDir`，那时才会展开占位符、做白名单校验并在分类目录被拒时回退到基础目录。提示与决策分离，保证最终写入磁盘的路径永远经过完整校验。

**练习 2**：如果用户对同一个 URL 先后调用两次 `Resolve` 但从不 `Create`，会发生什么？

**参考答案**：两次各自生成独立 `rrId`，`fetcherCache` 里会留下两个再也不会被消费的 Fetcher（HTTP 场景下可能还挂着预取连接与临时数据）。它们不会被自动清理，直到进程退出。这是当前实现的已知取舍——解析缓存以内存换取"Create 时免二次请求"的收益。

### 4.2 CreateDirect 与 doCreate：两条建任务路径与任务骨架装配

#### 4.2.1 概念说明

建任务有两条路径，对应两类使用场景：

- **CreateDirect（直建）**：一个 URL 直接变成任务。CLI（`Boot().Create()`）、REST 的 `req` 分支走这条路。任务创建时 `Resource` 还没解析（`task.Meta.Res == nil`），解析被**推迟**到任务启动时在 `doStart` 里做——所以叫"延迟解析"。
- **Create / CreateWithOptions（消费解析缓存）**：先用 `Resolve` 拿到 `rr.ID`，UI 展示资源、用户选好文件与目录后，再用 `rr.ID` 建任务。此时 Fetcher 已经持有解析好的 `Resource`，任务启动时无需再解析。

两条路径最终都汇入同一个函数 `doCreate`，它负责装配 `Task` 骨架、落盘、并决定任务"立即启动"还是"进等待队列"。

#### 4.2.2 核心流程

```
路径 A：CreateDirect(req, opts)                路径 B：CreateWithOptions(rrId, opts)
├── prepareDefaultPath(opts)                   ├── 从 fetcherCache 取 fetcher（无则报 invalid resource id）
│    # 仅 SkipAutoCategorize+AsDefaultPath     ├── prepareDefaultPath(opts)
│    # 且 Path 非空时，候选为全局默认目录        ├── initOptions(opts.Clone(), URL)
├── createDirect                               ├── 校验 SelectFiles 下标 → InitSelectFiles
│    ├── ensureRequestRawURL                   ├── fetcher.Meta().Opts = taskOpts
│    ├── buildFetcher(URL)                     ├── res.CalcSize(SelectFiles)
│    ├── fetcher.Meta().Req = req              ├── （http）Patch 提交 Size 估计值
│    └── initOptions ──────────────┐           ├── defer: delete(fetcherCache, rrId)
└──────────────────────────────────┼───► doCreate(fetcher, opts)
                                        ├── parseFm(URL) 再次路由协议
                                        ├── NewTask()：ID=nanoid、Status=ready
                                        ├── 装配：fetcherManager/fetcher/Protocol/Meta/Progress/Uploading
                                        ├── NeedsPreparation = true
                                        ├── initTask：重建 timer、四把锁、测速数组
                                        ├── storage.Put(bucketTask, clone)   # 先落盘
                                        └── d.lock 内：
                                             ├── tasks = append(tasks, task)
                                             ├── 满载 → Status=wait、进 waitTasks
                                             └── 有空位 → doStart(task)
```

`initOptions`（[pkg/download/downloader.go:L1889-L1911](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1889-L1911)）做三件"保底"事：nil 选项变空选项；非 HTTP 协议剥掉 `Size` 估计值（`httpSizeOptions`，[pkg/download/downloader.go:L1877-L1883](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1877-L1883)）；`Path` 为空回退到全局 `DownloadDir`。注意目录此刻只是"填了个底"，真正的决策与白名单校验在启动时的 `prepareTask`。

#### 4.2.3 源码精读

**路径 A 入口**：`Boot().Create()` 直接调 `CreateDirect`（[pkg/download/downloader.go:L2791-L2797](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2791-L2797)）。`CreateDirect`（[pkg/download/downloader.go:L605-L615](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L605-L615)）外层只做"默认目录"处理：`prepareDefaultPath`（[pkg/download/downloader.go:L1461-L1474](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1461-L1474)）要求调用方**显式声明** `SkipAutoCategorize && AsDefaultPath` 且给了非空 `Path`，这个目录才候选成为全局默认下载目录，且要先过 `initDownloadPath` 白名单；建任务成功后 `persistDefaultPathBestEffort` 尽力持久化（失败只记 warning）。

**路径 B**：`Create` 是 `CreateWithOptions(rrId, nil)` 的别名（[pkg/download/downloader.go:L656-L658](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L656-L658)）。`CreateWithOptions`（[pkg/download/downloader.go:L662-L711](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L662-L711)）关键片段：

```go
fetcher, ok := d.fetcherCache[rrId]          // 取解析缓存
if !ok { return "", errors.New("invalid resource id") }
...
taskOpts, err := d.initOptions(opts.Clone(), fetcher.Meta().Req.URL)
if res := fetcher.Meta().Res; res != nil {
	for _, index := range taskOpts.SelectFiles {     // 入口先校验下标
		if index < 0 || index >= len(res.Files) { return "", fmt.Errorf(...) }
	}
	taskOpts.InitSelectFiles(len(res.Files))          // 展开为显式列表
}
fetcher.Meta().Opts = taskOpts
if res := fetcher.Meta().Res; res != nil {
	res.CalcSize(fetcher.Meta().Opts.SelectFiles)     // 覆盖式重算大小
}
if taskOpts.Size > 0 {                               // http：把提交的大小估计 Patch 进 fetcher
	if err := fetcher.Patch(nil, &base.Options{Size: taskOpts.Size}); err != nil { ... }
}
defer func() { /* delete(d.fetcherCache, rrId) */ }() // 缓存一次性消费
taskId, err = d.doCreate(fetcher, nil)
```

**汇合点 doCreate**（[pkg/download/downloader.go:L1822-L1873](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1822-L1873)）：

```go
func (d *Downloader) doCreate(f fetcher.Fetcher, opts *base.Options) (taskId string, err error) {
	if f.Meta().Opts == nil { f.Meta().Opts = opts }  // 路径 A 由 createDirect 已设好，这里兜底
	ensureRequestRawURL(f.Meta().Req)

	fm, err := d.parseFm(f.Meta().Req.URL)            // 建任务时按 URL 再路由一次协议
	task := NewTask()                                  // ID=nanoid、Status=ready、时间戳
	task.fetcherManager = fm
	task.fetcher = f
	task.Protocol = fm.Name()
	task.Meta = f.Meta()
	task.Progress = &Progress{}
	_, task.Uploading = f.(fetcher.Uploader)           // 实现了 Uploader 接口（BT）则可上传
	task.NeedsPreparation = true                       // 新任务等待第一次准备
	initTask(task)                                     // 重建 timer、锁、测速数组等运行时内核
	if err = d.syncBlobTaskLease(task); err != nil { return "", err }
	defer func() { if err != nil { d.releaseBlobTask(task) } }() // 失败时释放 Blob 租约
	if err = d.storage.Put(bucketTask, task.clone()); err != nil { return } // 先落盘再调度
	taskId = task.ID

	func() {
		d.lock.Lock()
		defer d.lock.Unlock()
		d.tasks = append(d.tasks, task)
		if d.remainRunningCount() == 0 {              // 满载：排队
			task.Status = base.DownloadStatusWait     // 直写字段，不走 updateStatus
			d.waitTasks = append(d.waitTasks, task)
			return
		}
		err = d.doStart(task)                          // 有空位：立即启动
	}()
	return
}
```

四个细节值得咀嚼：

1. `parseFm` 在建任务时**再次**执行——协议归属永远以"当时的 URL"为准（u3-l2 讲过路由贯穿六个时刻，这里是其中之一）。
2. `NewTask()`（[pkg/download/model.go:L101-L112](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L101-L112)）生成 nanoid 短 ID 与 `ready` 初始状态。
3. 进等待队列用的是 `task.Status = base.DownloadStatusWait` **直写**而非 `updateStatus`——因为此刻任务尚无人观测，且已在 `d.lock` 临界区内，无需再抢 `statusLock`。
4. 任务在**调度之前**就已写入 `bucketTask`，即使进程此刻崩溃，重启后也能恢复出这个任务（恢复语义见 u4-l5）。

`initTask`（[pkg/download/downloader.go:L2689-L2698](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2689-L2698)）给 Task 装上序列化会丢失的运行时内核：以已用时长初始化的 `timer`、`statusLock`/`lock`/`statsLock`/`blobRefLock` 四把锁、下载与上传两个测速数组。

#### 4.2.4 代码实践

**实践目标**：写出「从 Resolve 到 fetcher.Start() 之间每一步对 Task 字段的修改清单」的初版（后半程在 4.4 补完）。

**操作步骤**：

1. 通读 `doCreate`，对照 `Task` 结构体字段（[pkg/download/model.go:L35-L74](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L35-L74)）逐行标注"这一行改了哪个字段"；
2. 把结果整理成表格（参考 4.4.4 给出的完整答案前半部分）；
3. 对照 `TestDownloader_CreateDirectBatch`（[pkg/download/downloader_test.go:L915-L988](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L915-L988)），观察批量直建如何复用 `doCreate`。

**需要观察的现象**：`doCreate` 里对 Task 的写入全部发生在 `storage.Put` 之前（除最后的 wait/启动分支），也就是"先定型、再落盘、后调度"。

**预期结果**：你能不看源码说出 `doCreate` 装配了哪些字段、哪些属于公开层（会序列化）、哪些属于运行时层（`initTask` 重建）。

（本实践为源码阅读型，无需运行命令。）

#### 4.2.5 小练习与答案

**练习 1**：`CreateDirect` 路径建出的任务，`task.Meta.Res` 是什么？什么时候才有值？

**参考答案**：是 `nil`。资源解析被推迟到任务启动时：`doStart` 的 handler 里 `resolvedHere := task.Meta.Res == nil`，为真则调 `task.fetcher.Resolve(task.Meta.Req)` 并把结果回填 `task.Meta.Res`。这样设计让"排队中的任务"不占用解析资源，也避免了"解析了但任务永远不被调度"的浪费。

**练习 2**：`CreateWithOptions` 为什么要 `opts.Clone()` 之后才处理？`doCreate(fetcher, nil)` 传 nil 又是为什么？

**参考答案**：`Clone()` 是防御性拷贝——批量创建等场景多个请求可能共享同一个 Options 指针，直接改会互相污染（`httpSizeOptions` 的注释同样强调了 Clone before filtering）。而 `doCreate` 的 `opts` 参数只用于 `f.Meta().Opts == nil` 时的兜底赋值，路径 B 中 `fetcher.Meta().Opts` 已在前面显式设为 `taskOpts`，所以传 nil 即可。

**练习 3**：`prepareDefaultPath` 为什么要求 `SkipAutoCategorize && AsDefaultPath` 两个标志同时成立？

**参考答案**：注释写得很直白——只有用户**显式点名**的目录才能成为全局默认下载目录。保留自动分类决策的目录"归任务自己所有"，后端不能擅自把它升级为全局默认；不加双重确认，一次随手的选择就会悄悄改掉所有后续任务的落盘位置。

### 4.3 notifyRunning 与 wait 队列：MaxRunning 调度

#### 4.3.1 概念说明

`MaxRunning` 限制同时处于 running 状态的任务数。`doCreate` 发现满载时把任务标记为 `wait` 并塞进 `waitTasks`（FIFO 切片）；当某个正在跑的任务腾出名额（完成、出错、被暂停、被删除），就需要有人把队首任务拉起来——这件事由 `notifyRunning` 完成。

可以把 `waitTasks` 理解为一个"候车厅"：名额是座位，`notifyRunning` 是每次有人起身时的广播员。

#### 4.3.2 核心流程

```
名额释放点（谁调用 notifyRunning）：
  Pause（腾出被暂停任务的名额）
  Delete（移除任务）
  watch 循环（任务完成/被移除）
  handleOnError（任务出错）

notifyRunning()                    # 异步：go func + d.lock
├── remainRunningCount()           # MaxRunning - 当前 running 数
│     └── 0 → 直接返回（仍然满载）
├── waitTasks 非空？
└── 弹出队首 wt = waitTasks[0]
    └── doStart(wt)                # 注意：仍在 d.lock 临界区内调用
```

#### 4.3.3 源码精读

`notifyRunning` 与 `remainRunningCount`（[pkg/download/downloader.go:L578-L603](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L578-L603)）：

```go
func (d *Downloader) notifyRunning() {
	go func() {
		d.lock.Lock()
		defer d.lock.Unlock()
		if d.remainRunningCount() == 0 { return }   // 仍满载
		if len(d.waitTasks) > 0 {
			wt := d.waitTasks[0]                     // FIFO：先来先服务
			d.waitTasks = d.waitTasks[1:]
			d.doStart(wt)
		}
	}()
}

func (d *Downloader) remainRunningCount() int {
	runningCount := 0
	for _, t := range d.tasks {
		if d.taskStatus(t) == base.DownloadStatusRunning { runningCount++ }
	}
	return d.cfg.MaxRunning - runningCount
}
```

`notifyRunning` 的四个调用点分别在 [pkg/download/downloader.go:L776](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L776)（Pause）、[pkg/download/downloader.go:L966](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L966)（Delete）、[pkg/download/downloader.go:L1595](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1595)（watch 循环内任务终态后）与 [pkg/download/downloader.go:L1744](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1744)（handleOnError）。

两个设计要点：

- **异步 + 抢主锁**：`notifyRunning` 自己起 goroutine 并抢 `d.lock`，调用方不必关心锁状态，直接调用即可，不会阻塞当前流程。
- **在锁内调 doStart 安全吗？** 安全。下一节会看到 `doStart` 的同步段只做状态检查与置位（拿的是 `task.statusLock`，与 `d.lock` 不冲突），真正耗时的启动工作全在它自己 spawn 的 goroutine 里。这正是"两段式结构"带来的好处。

#### 4.3.4 代码实践

**实践目标**：用 Grep 验证四个 `notifyRunning()` 调用点各自所在的函数。

**操作步骤**：

1. 在 `pkg/download/downloader.go` 中搜索 `d.notifyRunning()`；
2. 对每个命中行，向上滚动找到它所属的外层函数（提示：分别在 `Pause`、`Delete`、`watch`、`handleOnError` 内）；
3. 思考：为什么 `Continue`（继续任务）不在其中也能唤醒等待队列？（提示：`Continue` 自己就直接调度了。）

**需要观察的现象**：四个调用点都是"刚释放了一个 running 名额"的位置。

**预期结果**：能说出"名额释放 → notifyRunning → 队首出队 → doStart"这条唤醒链，以及它是异步的（调用方不等队首真的启动）。

（本实践为源码阅读型，无需运行命令。）

#### 4.3.5 小练习与答案

**练习 1**：`waitTasks` 是什么顺序的队列？会不会出现"排在前面的任务先建却后跑"？

**参考答案**：FIFO（`append` 入队、`waitTasks[0]` 出队）。单次唤醒严格按入队顺序；但任务也可能经 `Continue`（继续）等路径不经过队列直接被调度，所以"创建顺序 = 启动顺序"只在"全部经队列唤醒"的场景下成立。

**练习 2**：如果 `doStart` 的同步段就失败（例如 restore 出错），名额会被浪费吗？

**参考答案**：会少一次启动，但不会死锁。`doStart` 出错路径走 `doOnError` 把任务置为 error，而 `handleOnError` 内部也会调用 `notifyRunning()` 重新尝试唤醒下一个等待者（[pkg/download/downloader.go:L1744](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1744)），所以队列不会因为一个坏任务而卡死。

### 4.4 doStart：statusMut 同步段与异步 handler

#### 4.4.1 概念说明

`doStart` 是主链路的"发动机"，但它不是一个从上到下执行完的函数，而是**两段式**结构：

- **同步段（`statusMut` 包裹）**：拿 `task.statusLock`，做三件事——拦截不该启动的任务（已 running/done）、恢复 Fetcher、把状态置为 running 并递增 `runGeneration`。这段又快又关键，必须在调用方（可能持有 `d.lock`）的语境里立即完成。
- **异步段（`handler` 闭包 + `go func`）**：拿 `task.lock`，做所有慢活——扩展钩子、延迟解析、目录准备、`fetcher.Start()`、统计与落盘。调用方拿到 `err == nil` 就返回了，不必等下载真正开始。

为什么必须分两段？因为"状态置为 running"必须**立刻**可见（否则并发的 Pause/Continue 会做出错误判断），而启动过程又慢到不能阻塞调度方。代数 `generation` 则是两段之间的"接力棒"：同步段记下本次启动的代数，异步段在每个关键步骤后核对 `taskIsRunningGeneration`，一旦发现任务已被暂停（代数变了或状态不是 running），立刻放弃剩余动作。

#### 4.4.2 核心流程

```
doStart(task)
├─ 同步段 statusMut(task.statusLock)
│   ├── Status ∈ {running, done} → isReturn，直接返回
│   ├── restoreTask：fetcher == nil → restoreFetcher
│   │     └── Manager.Restore() 两段式：取存储模板 v ← bucketSave
│   │         构造 fetcher = f(Meta, v)；nil 则 Build() 兜底
│   │         setupFetcher 注入 Controller；回填 Meta 的 Req/Res/Opts（仅 nil 时）
│   ├── updateStatus(running)   # UpdatedAt + Status；DoneAt 只在 done 分支写
│   └── runGeneration++ → 记下 generation
│
└─ 异步段 go handler()（task.lock 保护）
    ├── 核对代数（taskIsRunningGeneration）
    ├── fetcher 仍为 nil → restoreFetcher（第二次机会）
    ├── triggerOnStart（扩展钩子）→ 再核对代数
    ├── fetcher.Meta().Req/Res/Opts = task.Meta.*   # task.Meta 是权威数据源
    ├── resolvedHere（Meta.Res == nil）→ fetcher.Resolve(req)（延迟解析）
    ├── NeedsPreparation？
    │     ├── 是 → （AutoRename 则先拿 checkDuplicateLock）prepareTask
    │     └── 否 → initDownloadPath 白名单复检（每次启动都查）
    ├── Progress.Speed = 0 → 核对代数
    ├── timer.Start()
    ├── fetcher.Start()          # 同步返回错误；成功则 started = true
    ├── captureTaskStats（失败仅告警：统计是辅助数据）
    ├── saveTask（bucketSave + bucketTask 落盘）
    └── 收尾：
        ├── 失败且代数仍有效 → doOnError
        └── 成功 → emit(EventKeyStart) + go watch(task)
```

`taskIsRunningGeneration`（[pkg/download/downloader.go:L2209-L2216](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2209-L2216)）的判定很简单：`Status == running && runGeneration == generation`，两条件缺一都算"我已过时"。

#### 4.4.3 源码精读

**同步段**（[pkg/download/downloader.go:L2001-L2026](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2001-L2026)）：

```go
isReturn, err := d.statusMut(task, func() (isReturn bool, err error) {
	if task.Status == base.DownloadStatusRunning || task.Status == base.DownloadStatusDone {
		isReturn = true
		return
	}
	err = d.restoreTask(task)          // fetcher 为 nil 时按存储内容重建
	if err != nil { ...; return }
	task.updateStatus(base.DownloadStatusRunning) // UpdatedAt + Status
	task.runGeneration++
	generation = task.runGeneration    # 本次启动的代数
	return
})
```

`statusMut` 本体（[pkg/download/downloader.go:L1933-L1938](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1933-L1938)）只是"拿 statusLock、执行闭包、还锁"的包装。`updateStatus`（[pkg/download/model.go:L155-L164](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L155-L164)）在置状态的同时刷新 `UpdatedAt`，且只在进入 done 时写 `DoneAt`（一次性写入语义，u2-l2 讲过）。

**restoreFetcher**（[pkg/download/downloader.go:L1797-L1820](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1797-L1820)）是 u3-l4 讲过的两段式持久化契约的消费端：`Manager.Restore()` 返回存储模板 `v` 与构造器 `f`；先用 `storage.Get(bucketSave, task.ID, v)` 把落盘的协议状态灌进模板，再 `f(task.Meta, v)` 造出 Fetcher；构造器返回 nil 则 `Build()` 兜底；随后 `setupFetcher` 注入 Controller，并在 Meta 的 Req/Res/Opts 为 nil 时用 `task.Meta` 回填（**仅 nil 时回填**——已有值说明 Fetcher 自带更新鲜的数据，比如刚解析完的 Resource）。

**异步段 handler**（[pkg/download/downloader.go:L2028-L2103](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2028-L2103)），关键节点：

```go
handler := func() error {
	task.lock.Lock()
	defer task.lock.Unlock()
	if !d.taskIsRunningGeneration(task, generation) { return nil } // 核对①
	if task.fetcher == nil { d.restoreFetcher(task) }
	d.triggerOnStart(task)                                        // 扩展 onStart 钩子
	if !d.taskIsRunningGeneration(task, generation) { return nil } // 核对②
	if task.fetcher != nil {                                      // task.Meta 是权威
		task.fetcher.Meta().Req, .Res, .Opts = task.Meta.Req, task.Meta.Res, task.Meta.Opts
	}
	d.syncBlobTaskLease(task)
	resolvedHere := task.Meta.Res == nil                          // 延迟解析（CreateDirect 路径）
	if resolvedHere {
		task.fetcher.Resolve(task.Meta.Req)
		task.Meta.Res = task.fetcher.Meta().Res
	}

	if task.NeedsPreparation {
		if task.fetcherManager.AutoRename() {
			d.checkDuplicateLock.Lock(); defer d.checkDuplicateLock.Unlock() // 为何？见 4.5
		}
		if err := d.prepareTask(task); err != nil { return err }
	} else if _, err := d.initDownloadPath(task.Meta.Opts.Path); err != nil {
		return err                                                // 已准备过的任务每次启动仍复检白名单
	}

	task.statusLock.Lock(); task.Progress.Speed = 0; task.statusLock.Unlock()
	if !d.taskIsRunningGeneration(task, generation) { return nil } // 核对③
	task.timer.Start()
	if err := task.fetcher.Start(); err != nil { return err }      // 字节开始流动
	started = true
	if err := d.captureTaskStats(task, true, true); err != nil { /* 仅告警 */ }
	return d.saveTask(task)
}
go func() {
	if err := handler(); err != nil {
		if d.taskIsRunningGeneration(task, generation) { d.doOnError(task, err) }
		return
	}
	if started {
		if d.taskIsRunningGeneration(task, generation) { d.emit(EventKeyStart, task) }
		go d.watch(task)                                          // 进度循环交由下一讲
	}
}()
```

三次代数核对（①②③）各挡一种竞态：①挡"启动前任务已被暂停/重启"，②挡"扩展钩子执行期间状态变化"，③挡"准备目录期间状态变化"。`captureTaskStats` 失败只告警不失败——注释明说"统计是辅助数据，不能把已启动的下载变成失败任务"。`saveTask`（[pkg/download/downloader.go:L489-L514](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L489-L514)）先让协议 `Store` 序列化 Fetcher 写 `bucketSave`，再在 `statusLock` 下克隆 Task 写 `bucketTask`——这是 u4-l5 持久化主题的入口，本讲只记住"启动完成后立刻存了一份检查点"。

`watch` 的开头（[pkg/download/downloader.go:L1517-L1523](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1517-L1523)）用 `watchedTasks.LoadOrStore` 保证同一任务只有一个 watch 循环，它的完整逻辑是下一讲（u4-l3）的主角。

#### 4.4.4 代码实践

**实践目标**：用调试器（Delve）跟踪一个 HTTP 任务从 `Resolve` 到 `fetcher.Start()` 的完整调用链，并整理「每一步对 Task 字段的修改清单」。本实践与讲义的综合实践共用同一个程序，这里先给出断点清单与答案表格。

**操作步骤**：

1. 按第 5 节综合实践准备示例程序（放在 `_examples/lifecycle/main.go` 或独立 module）；
2. 用 Delve 启动并打断点：
   ```
   dlv debug ./_examples/lifecycle
   break pkg/download/downloader.go:1832   # doCreate: NewTask
   break pkg/download/downloader.go:2014   # doStart 同步段: updateStatus(running)
   break pkg/download/downloader.go:2053   # handler: resolvedHere
   break pkg/download/downloader.go:2090   # handler: fetcher.Start()
   continue
   ```
3. 每次命中后用 `print *task`（或 `p task.Status`、`p task.runGeneration`、`p task.NeedsPreparation`）记录字段变化。

**需要观察的现象**：四个断点依次命中；在第 2090 行断点处 `task.Status` 已是 `running`、`NeedsPreparation` 已变 `false`（HTTP 管理器 `AutoRename()` 为 true，`prepareTask` 已执行过）。

**预期结果**——Task 字段修改清单（完整版答案）：

| 序 | 调用点（行号） | 修改内容 | 所属层 |
| --- | --- | --- | --- |
| 1 | `NewTask`（model.go:L101-L112） | `ID`=nanoid、`Status`=ready、`CreatedAt`/`UpdatedAt`=now | 公开层 |
| 2 | `doCreate`（L1833-L1841） | `fetcherManager`、`fetcher`、`Protocol`、`Meta`、`Progress=&Progress{}`、`Uploading`、`NeedsPreparation=true` | 混合（前四项公开层的内核来源） |
| 3 | `initTask`（L2689-L2698） | `timer`、`statusLock`/`lock`/`statsLock`/`blobRefLock`、`speedArr`/`uploadSpeedArr` | 运行时层（不序列化） |
| 4 | `storage.Put`（L1851） | 不改字段，任务快照首次落盘 `bucketTask` | — |
| 5 | 满载分支（L1864-L1865） | `Status`=wait（直写）、进入 `waitTasks` | 公开层 |
| 6 | `doStart` 同步段（L2014-L2016） | `Status`=running、`UpdatedAt`=now（`updateStatus`）、`runGeneration++`；此前可能已由 `restoreFetcher` 重建 `fetcher` | 公开层 + 运行时层 |
| 7 | handler 回填（L2045-L2049） | `fetcher.Meta().Req/Res/Opts` ← `task.Meta.*`（`Meta` 是共享指针，Task 与 Fetcher 看到同一份） | 共享数据 |
| 8 | 延迟解析（L2053-L2060，仅 CreateDirect 路径） | `task.Meta.Res` 首次赋值 | 公开层 |
| 9 | `prepareTask`（L2074，详见 4.5） | `Meta.Opts.Path`=决策目录、`Meta.Opts.Name`=可能改名后的名字、`NeedsPreparation`=false；失败则保持 true | 公开层 |
| 10 | 速度清零（L2083-L2085） | `Progress.Speed`=0 | 公开层 |
| 11 | `fetcher.Start()` 后（L2094-L2101） | `saveTask` 写 `bucketSave`+`bucketTask`；`captureTaskStats` 填 stats | 持久化 |
| 12 | watch 循环（下一讲） | `Progress.Used/Speed/Downloaded` 周期刷新；终态时 `Status`=done/error、`DoneAt` | 公开层 |

（断点行为为静态推导，**待本地验证**；表格内容直接源自源码逐行阅读。）

#### 4.4.5 小练习与答案

**练习 1**：为什么 `fetcher.Start()` 的错误用同步返回值处理，而下载中途的错误走 `DoneCh`/`Wait()` 异步上报？

**参考答案**：`Start()` 的职责是"把传输跑起来"——建目录、建文件、发起首批连接，这些步骤的失败是启动失败，调用方（handler）必须立刻知道并走 `doOnError`。而传输中途断线、校验失败发生在 `Start()` 返回之后，只能由 Fetcher 通过完成通道（u3-l1 的 DoneCh 契约）异步通知，由 watch 循环读 `Wait()` 翻译成 error 状态。

**练习 2**：`doStart` 已经在同步段调了 `restoreTask`，为什么 handler 里还要再判一次 `task.fetcher == nil` 并 `restoreFetcher`？

**参考答案**：防御性兜底。同步段与 handler 是两个时间点，中间隔着 goroutine 调度；此外某些路径（如 `Patch`）也独立恢复 Fetcher。二次检查保证 handler 拿到的一定是可用实例，属于"不变式在关键位置重复确认"的写法。

**练习 3**：已经 `NeedsPreparation=false` 的任务每次启动为什么还要跑 `initDownloadPath` 白名单校验？

**参考答案**：源码注释给了答案——"已准备的任务保留它的目录，但目录在**每次启动**时仍要通过白名单"。目录在首次准备时定死不再改（保证断点续传写同一位置），但白名单是**运行时安全策略**，配置可能中途收紧（例如服务端管理员更新了 `WhiteDownloadDirs`），所以每次启动复检，策略变更能立即生效。

### 4.5 prepareTask：一次性目录决策与重名改名

#### 4.5.1 概念说明

`NeedsPreparation` 标记（[pkg/download/model.go:L48-L55](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L48-L55)）的字段注释值得逐句读：任务在资源解析后要跑**一次**准备——决定下载目录、解决目录内的重名——之后标志清除；排队、暂停、重试过的任务照样要准备（只要还没准备过），而**准备过的任务绝不会再被分类或改名**。

为什么要把"定目录"推迟到这里、而不是建任务时就定死？因为 CreateDirect 路径建任务时 `Resource` 还没解析（连文件名、扩展名都不知道），无从分类；而任务一旦准备过，目录与名字就锁定了，暂停/重试/重启都不能变，否则断点续传会写错位置。

#### 4.5.2 核心流程

```
prepareTask(task)                     # 持有 task.lock，可能同时持有 checkDuplicateLock
├── Res 未解析 → 报错
├── 深拷贝预备稿：preparedMeta = *Meta、preparedOpts = *Opts、Res 深克隆
│     # 预备稿与活任务隔离：准备失败不污染线上数据
├── decideDownloadDir(Opts, Res)      # 分类路由 + 基础目录 + 占位符展开 + 白名单
│     # 分类目录被白名单拒 → 回退任务基础目录
├── AutoRename？（HTTP 系为 true）
│     ├── 文件名安全化 SafeFilename
│     ├── 目录型资源（Res.Name != ""）→ FolderPath() → CheckDuplicateAndRename
│     └── 单文件资源 → Files[0].Name → SingleFilepath() → CheckDuplicateAndRename
│     └── 改名结果统一写进 preparedOpts.Name（Opts.Name 优先级最高，u3-l1）
├── CalcSize 重算选中文件总大小
├── 先存候选：clone 任务 + 换 Meta + NeedsPreparation=false → saveTask 落盘
└── 后发布：statusLock 内 *task.Meta = preparedMeta；NeedsPreparation = false
```

「先存候选、后发布」是这个函数最漂亮的细节：先把**定稿后的副本**写入存储，成功后才把活任务的 Meta 整体替换。落盘失败时活任务保持原样、`NeedsPreparation` 仍为 true，下次启动重新准备——目录决策永远不可能"半新半旧"。

`CheckDuplicateAndRename`（[pkg/util/path.go:L42-L74](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/util/path.go#L42-L74)）的算法：`os.Stat` 目标路径，不存在则原样返回名字；存在则从 `a (1).txt`、`a (2).txt`… 依次探测直到找到磁盘上不存在的名字（无扩展名与纯隐藏文件如 `.gitignore` 有特判）。

#### 4.5.3 源码精读

`prepareTask` 全函数（[pkg/download/downloader.go:L1945-L1999](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1945-L1999)）：

```go
func (d *Downloader) prepareTask(task *Task) error {
	if task.Meta.Res == nil { return errors.New("task resource is not resolved") }
	preparedMeta := *task.Meta                    // 浅拷贝 Meta
	preparedOpts := *task.Meta.Opts               // 拷贝 Opts
	preparedMeta.Opts = &preparedOpts
	preparedMeta.Res = util.DeepClone(task.Meta.Res) // Res 深克隆，彻底隔离
	dir, err := d.decideDownloadDir(preparedMeta.Opts, preparedMeta.Res)
	preparedMeta.Opts.Path = dir

	if task.fetcherManager.AutoRename() {
		preparedMeta.Opts.Name = util.SafeFilename(preparedMeta.Opts.Name)
		if preparedMeta.Res.Name != "" {          // 目录型（BT 多文件）
			preparedMeta.Res.Name = util.SafeFilename(preparedMeta.Res.Name)
			newName, err := util.CheckDuplicateAndRename(preparedMeta.FolderPath())
			preparedMeta.Opts.Name = newName
		} else {                                  // 单文件
			preparedMeta.Res.Files[0].Name = util.SafeFilename(preparedMeta.Res.Files[0].Name)
			newName, err := util.CheckDuplicateAndRename(preparedMeta.SingleFilepath())
			preparedMeta.Opts.Name = newName
		}
	}
	preparedMeta.Res.CalcSize(preparedMeta.Opts.SelectFiles)

	// 先存候选再发布：fetcher 保留其解析出的协议状态，
	// 保存失败不可能改变线上的目录、名字或准备标志。
	preparedTask := *task
	preparedTask.Meta = &preparedMeta
	preparedTask.NeedsPreparation = false
	if err := d.saveTask(&preparedTask); err != nil { return err }
	task.statusLock.Lock()
	*task.Meta = preparedMeta                     # 发布：整块替换
	task.NeedsPreparation = false
	task.statusLock.Unlock()
	return nil
}
```

目录决策 `decideDownloadDir`（[pkg/download/category.go:L34-L66](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/category.go#L34-L66)）：基础目录取 `opts.Path`，空则取全局 `DownloadDir`，再空直接报错（拒绝回落到进程工作目录）；未跳过自动分类且命中分类则改走分类目录；展开占位符并做白名单校验，分类目录被拒时**回退到基础目录**而不是让任务失败。分类匹配细节（只路由单文件资源等）留给 u10-l1。

**`checkDuplicateLock` 为什么必须包住 `fetcher.Start()`？** 答案写在 doStart 的注释里（[pkg/download/downloader.go:L2062-L2069](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2062-L2069)）：

> "重名是从磁盘上的文件解析出来的，但文件要到 fetcher 启动时才出现，所以这把锁必须连启动一起罩住。"

展开讲：`CheckDuplicateAndRename` 的判重依据是 `os.Stat`——**磁盘上此刻有没有这个名字的文件**。而目标文件要到 `fetcher.Start()` 执行、协议层调用 `ctl.Touch` 创建文件之后才真正存在。设想两个并发任务下载同一个文件到同一目录：

- 任务 A：Stat 发现 `a.txt` 不存在 → 决定用 `a.txt`；
- 任务 B：Stat 也发现 `a.txt` 不存在（A 还没来得及创建文件）→ 也决定用 `a.txt`；
- 两个任务写同一个文件，数据互相覆盖。

把「查重 + 改名 + `fetcher.Start()`（创建文件）」整体放进全局 `checkDuplicateLock` 临界区，就把"检查—改名—占位"做成了原子操作：A 拿着锁走完全程、文件已落盘之后，B 才开始 Stat，此时必然能看到 `a.txt` 而改用 `a (1).txt`。这就是 4.4 流程图中 `d.checkDuplicateLock.Lock(); defer d.checkDuplicateLock.Unlock()` 用 **defer** 解锁的原因——它要罩住包括 `fetcher.Start()` 在内的 handler 余下全部步骤。仓库测试 `TestDownloader_CreateRename`（[pkg/download/downloader_test.go:L1122-L1166](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L1122-L1166)）正是这个行为的回归：同一 URL 并发直建两个任务，最终校验 `download.data` 与 `download (1).data` 两个文件的 MD5 都与源文件一致。

#### 4.5.4 代码实践

**实践目标**：亲手复现重名改名，观察 `checkDuplicateLock + prepareTask + Touch` 的协作效果。

**操作步骤**：

1. 运行第 5 节综合实践的示例程序（它会对同一 URL 连续 `CreateDirect` 两次）；
2. 观察输出中的 `[file]` 列表；
3. 对照源码走一遍：两个任务的 handler 并发进入 → 第一个拿到 `checkDuplicateLock`，`prepareTask` 决定 `trace.data`，`fetcher.Start()` 创建文件，释放锁 → 第二个 Stat 到 `trace.data` 存在 → 改名 `trace (1).data`。

**需要观察的现象**：下载目录里出现 `trace.data` 与 `trace (1).data`，内容完全相同。

**预期结果**：与 `TestDownloader_CreateRename` 的断言一致（该测试用 `download.data` / `download (1).data` 验证过同样逻辑）。程序实际输出**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`prepareTask` 为什么要深克隆 `Res`、拷贝 `Opts`，而不是直接改 `task.Meta`？

**参考答案**：准备过程可能在中途失败（目录决策被白名单拒绝、磁盘 Stat 出错、saveTask 失败）。在预备稿上操作，失败时活任务的 Meta 一字未动、`NeedsPreparation` 仍为 true，下次启动可以安全重试；成功时用 `*task.Meta = preparedMeta` 整块替换发布。若直接改活任务，一次失败就会留下"目录改了一半"的脏状态。

**练习 2**：`checkDuplicateLock` 是全局锁，会不会所有下载都被它串行化、拖慢速度？

**参考答案**：不会。只有 `NeedsPreparation && fetcherManager.AutoRename()` 的任务才拿这把锁，而准备只在**首次启动**发生一次；此后每次续传、重试都不再进入该分支（走白名单复检）。已准备任务的启动完全不碰这把锁。

**练习 3**：BT 任务（目录型资源）与 HTTP 任务（单文件）在 `prepareTask` 里的改名路径有何不同？

**参考答案**：目录型资源 `Res.Name != ""`，对 `FolderPath()` 整目录路径查重改名；单文件资源对 `SingleFilepath()` 的文件完整路径查重改名。两条路径最终都把新名字写进 `preparedMeta.Opts.Name`——因为 `FetcherMeta` 的路径计算里 `Opts.Name` 优先级最高（u3-l1），这样改名对协议层透明。

## 5. 综合实践

设计一个贯穿本讲的跟踪实验：**一个程序，三个观察点**——完整调用链、wait 队列调度、重名改名。

**示例代码**（新建 `_examples/lifecycle/main.go`，本地学习用、不提交；也可以放到独立 module 并用 `replace github.com/GopeedLab/gopeed => <仓库路径>` 引用）：

```go
// 示例代码：跟踪 Resolve→Create→doStart 主链路的观察程序
package main

import (
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/GopeedLab/gopeed/pkg/base"
	"github.com/GopeedLab/gopeed/pkg/download"
)

func main() {
	// 观察点③的素材：本地文件服务器，提供 4MB 测试文件
	srcDir, _ := os.MkdirTemp("", "gopeed-src")
	os.WriteFile(filepath.Join(srcDir, "trace.data"), make([]byte, 4*1024*1024), 0644)
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	go http.Serve(ln, http.FileServer(http.Dir(srcDir)))
	url := fmt.Sprintf("http://%s/trace.data", ln.Addr().String())

	dlDir, _ := os.MkdirTemp("", "gopeed-dl")

	d := download.NewDownloader(nil)
	if err := d.Setup(); err != nil {
		panic(err)
	}
	defer d.Clear()

	// 观察点②：把 MaxRunning 压到 1，制造 wait 队列
	cfg, _ := d.GetConfig()
	cfg.MaxRunning = 1
	if err := d.PutConfig(cfg); err != nil { // 读-改-写整体提交（u2-l3 的约定）
		panic(err)
	}

	var wg sync.WaitGroup
	wg.Add(3) // 3 个任务
	d.Listener(func(e *download.Event) {
		fmt.Printf("[event] %-8s task=%.6s status=%-7s err=%v\n",
			e.Key, e.Task.ID, e.Task.Status, e.Err)
		if e.Key == download.EventKeyDone {
			wg.Done()
		}
	})

	// 观察点①+③：同一个 URL 建两个任务（第二个会排队，且会撞名）
	// 再补一个不同 URL 的任务，凑满 3 个
	for i := 0; i < 2; i++ {
		id, err := d.CreateDirect(&base.Request{URL: url}, &base.Options{Path: dlDir})
		fmt.Printf("[create] task=%s err=%v\n", id, err)
		if err != nil {
			panic(err)
		}
	}
	os.WriteFile(filepath.Join(srcDir, "other.data"), make([]byte, 1024*1024), 0644)
	id, err := d.CreateDirect(
		&base.Request{URL: fmt.Sprintf("http://%s/other.data", ln.Addr().String())},
		&base.Options{Path: dlDir})
	fmt.Printf("[create] task=%s err=%v\n", id, err)

	// 观察点②：趁前两个还在排队/下载，轮询任务状态
	for i := 0; i < 6; i++ {
		time.Sleep(300 * time.Millisecond)
		for _, t := range d.GetTasks() {
			fmt.Printf("[poll %d] %.6s %s\n", i, t.ID, t.Status)
		}
	}

	wg.Wait()
	entries, _ := os.ReadDir(dlDir)
	for _, e := range entries {
		fmt.Println("[file]", e.Name())
	}
}
```

**要完成的任务清单**：

1. **调用链清单（观察点①）**：结合 Delve 断点（见 4.4.4）或对照源码，填完 4.4.4 的字段修改清单表，重点确认：CreateDirect 路径的 `resolvedHere` 为真（断点 2053 行处 `task.Meta.Res == nil`）。
2. **wait 队列（观察点②）**：`MaxRunning=1` 时第二个任务建出即为 wait，`[poll]` 轮询能看到 `wait → running` 的切换；第一个任务 done 后约一个刷新周期内第二个被 `notifyRunning` 唤醒。
3. **重名改名（观察点③）**：`[file]` 列表应出现 `trace.data` 与 `trace (1).data`，由此验证 `checkDuplicateLock` 把「查重—改名—Start 建文件」串成了原子段；写出"若没有这把锁，两个并发任务会发生什么"的分析（答案见 4.5.3）。

**预期结果**（程序输出**待本地验证**）：事件序列大致为 `start → progress… → done` 依次出现两组（trace.data 两个任务）加一组（other.data），轮询输出可见第二个/第三个任务经历 wait 态。若把 `MaxRunning` 恢复默认 5 再跑一次，三个任务应同时 running，且更能体现并发场景下 `checkDuplicateLock` 的作用。

**思考题（选做）**：把示例中第三个任务删除（`d.Delete`），观察 `notifyRunning`（[pkg/download/downloader.go:L966](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L966) 调用点）能否让队列里的任务提前上场。

## 6. 本讲小结

- **两路径建任务**：`CreateDirect`（直建，Resource 延迟到 `doStart` 内解析）与 `Resolve → Create/CreateWithOptions`（消费 `fetcherCache` 里带着解析成果与预取数据的活 Fetcher），两者在 `doCreate` 汇合；解析缓存只在 Create 的 defer 里删除一次。
- **doCreate 装配骨架**：`NewTask` 出 ID 与 ready 态，依次挂上 fetcherManager/fetcher/Protocol/Meta/Progress/Uploading 与 `NeedsPreparation=true`，`initTask` 重建运行时内核；任务先落盘 `bucketTask` 再进 `tasks`，满载时直写 wait 态入 `waitTasks` 队列。
- **调度唤醒**：`notifyRunning` 异步抢主锁，`remainRunningCount` 有余量就 FIFO 弹出队首调 `doStart`；Pause/Delete/watch 终态/handleOnError 四处触发。
- **doStart 两段式**：同步段（`statusMut`/statusLock）拦截、`restoreFetcher`（两段式 Restore 契约）、置 running 并 `runGeneration++`；异步 handler（task.lock）做扩展钩子、Meta 回填、延迟解析、准备目录、`fetcher.Start()`，三次代数核对防止旧协程覆盖新状态。
- **prepareTask 一次性准备**：在深克隆的预备稿上决策目录（分类路由 + 白名单 + 回退）并解决重名，「先存候选、后发布」保证失败不产生半新半旧状态；`NeedsPreparation` 清除后永不再分类改名，但每次启动仍复检白名单。
- **checkDuplicateLock 的临界区**：查重的依据是磁盘 Stat，而文件要到 `fetcher.Start()` 才出现，所以锁必须用 defer 罩住整个「查重—改名—启动建文件」过程，把跨任务的占位变成原子操作。

## 7. 下一步学习建议

任务启动之后，主角交给 **u4-l3《watch 循环、事件系统与进度刷新》**：`go d.watch(task)` 启动的轮询如何读 `fetcher.Wait()`/`Progress()`、用滑动窗口算速度、按策略落检查点，并把 start/progress/done/error 翻译成 `emit` 事件。随后 **u4-l4** 讲 Pause/Continue/Delete 与 `doPauseForScheduling`、`runGeneration` 的完整竞态防护——本讲已经铺垫了代数机制的前半部分。若想先横向扩展，可以跳去 **u5-l1** 看 HTTP 协议如何实现本讲反复出现的 `fetcher.Resolve` 与 `fetcher.Start`（包括 `asyncPrefetch` 如何让"消费解析缓存"免二次请求）。
