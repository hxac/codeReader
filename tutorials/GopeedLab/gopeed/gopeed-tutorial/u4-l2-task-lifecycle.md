# 主链路：Resolve → Create → doStart

## 1. 本讲目标

上一讲我们认识了 `Downloader` 的结构体与初始化，知道了九个存储桶、两级初始化（`NewDownloader` 搭内存骨架、`Setup` 恢复持久化世界）。本讲沿着最核心的一条线走通**一个下载任务从无到有、再到字节开始流动**的完整调用链：

- `Resolve` 如何解析资源并把它缓存在 `fetcherCache` 里，`ResolveResult` 到底返回了什么；
- `CreateDirect` 与 `Resolve → Create/CreateWithOptions` 两条建任务路径的区别，`doCreate` 如何装配出 `Task` 骨架，以及批量创建入口 `CreateDirectBatchWithResult` 如何复用同一条链路；
- `CreateWithOptions` 拆分出的 `createResolved` 对 `fetcherCache` 的「单一创建者」消费与失败归还语义；
- `notifyRunning` 如何在 `MaxRunning` 满载时把任务排进 `waitTasks` 队列、又如何被唤醒；
- `doStart` 中「`statusMut` 同步段 + 异步 `handler`」的两段式结构，以及 `runGeneration` 代数在其中的作用；
- `prepareTask` 的一次性目录决策与重名改名机制，特别是 `checkDuplicateLock` 为什么必须包住 `fetcher.Start()`。

学完本讲，你应该能独立回答：**"一个 URL 交进来之后、第一个字节写进磁盘之前，Gopeed 内部究竟发生了什么？"**

## 2. 前置知识

本讲默认你已读过前置讲义的核心结论，这里只做要点回顾：

- **两阶段模型**：Gopeed 把一次下载拆成「解析（Resolve）」与「传输（Start）」两个阶段。解析产出 `Resource`（文件名、大小、文件列表），传输负责真正搬运字节。
- **Task 的公开层与运行时层**（u2-l2）：`Task` 的 JSON 序列化只带公开字段（ID、Meta、ParentID、Status、Progress 等），fetcher、锁、timer、测速窗口等运行时字段不会落盘，恢复后由 `initTask` 重建。
- **分组归属双轨制**（u2-l2）：`Task.ParentID` 落盘为权威归属，运行时的 `task.group` 指针只是内存缓存；批量操作中组 ID 经 `expandGroupTaskIDs` 展开为成员任务。
- **状态的唯一写入者是 Downloader**（u2-l2 / u3-l1）：Fetcher 不改 `Task.Status`，只通过 `Wait()` 返回值汇报结果。
- **FetcherManager 是协议级单例门面，Fetcher 是任务级执行者**（u3-l2 / u3-l4）：`parseFm(url)` 按注册顺序 first-match-wins 选中协议管理器，`Build()` 造出任务专属的 Fetcher，`setupFetcher` 在 Build 之后注入 Controller。
- **runGeneration 代数**（u2-l2 提及）：每次任务从非 running 态进入 running 态时代数自增，旧协程代数不匹配时必须放弃操作，防止「暂停与完成竞态下旧协程覆盖新状态」。
- **锁的归属**（u4-l1）：任务列表、`waitTasks` 与分组三件套（`groups`/`creatingGroups`/`deletingTasks`）归主锁 `d.lock`；解析缓存 `fetcherCache` 归 `fetcherMapLock`；任务自身字段归 `task.lock` / `task.statusLock`；重名检查归全局 `checkDuplicateLock`。

不熟悉并发术语的读者只需要记住一句话：**互斥锁（Mutex）保证同一时刻只有一个 goroutine 能进入临界区**，本讲出现的每把锁都对应一个"只能独占访问"的资源。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `pkg/download/downloader.go` | 下载引擎中枢 | `Resolve`、`CreateDirect`/`createDirect`、`CreateDirectBatchWithResult`、`CreateWithOptions`/`createResolved`、`doCreate`、`notifyRunning`、`doStart`、`prepareTask`、`restoreFetcher`、`saveTask` |
| `pkg/download/model.go` | Task 与 ResolveResult 模型 | `ResolveResult`、`TaskGroup`、`CreateTaskBatchResult`、`Task.ParentID`/`NeedsPreparation`、`NewTask`、`updateStatus` |
| `pkg/base/model.go` | 公开请求模型 | `CreateTaskBatch`（Group/GroupName）与 `CreateTaskBatchItem`（Rid/Req/Opts） |
| `pkg/download/category.go` | 自动分类与目录决策 | `decideDownloadDirForTask`、`suggestedDownloadDir`（细节在 u10-l1 展开） |
| `pkg/api/service.go` | 与传输无关的服务层 | `resolve` / `createTask` / `createTaskBatch` 三个 handler 如何分流建任务路径 |
| `pkg/util/path.go` | 路径工具 | `CheckDuplicateAndRename` 重名改名算法 |
| `_docs/task-batch-api.md` | 批量创建 API 文档 | rid 优先、opts 整体替换、部分成功语义的官方说明 |
| `pkg/download/downloader_test.go` | 下载器测试 | `TestDownloader_Create`、`TestDownloader_CreateRename`、`TestDownloader_ConcurrentResolvedCreation` 等 |
| `internal/test/httptest.go` | 测试用 HTTP 假服务器 | `StartTestFileServer` 与文件名常量 |

## 4. 核心概念与源码讲解

本讲的六个最小模块按执行顺序排列：`Resolve`（解析与缓存）→ `doCreate`（建任务骨架，含批量入口）→ `createResolved`（消费解析缓存）→ `notifyRunning`（排队）→ `doStart`（启动）→ `prepareTask`（启动中的一次性准备）。

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

`Resolve` 主体（[pkg/download/downloader.go:L562-L598](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L562-L598)）：

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

`buildFetcher`（[pkg/download/downloader.go:L2669-L2677](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2669-L2677)）三步走：`parseFm(url)` 按注册顺序匹配 Filter 选中协议管理器（[pkg/download/downloader.go:L466-L475](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L466-L475)），`fm.Build()` 造出任务级 Fetcher，`setupFetcher` 注入 Controller（u3-l3 详述）。

`ResolveResult` 定义（[pkg/download/model.go:L22-L30](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L22-L30)）：

```go
type ResolveResult struct {
	ID  string         `json:"id"`
	Res *base.Resource `json:"res"`
	// SuggestedDownloadDir ... It is a hint for the ui,
	// the create call decides the final path.
	SuggestedDownloadDir string `json:"suggestedDownloadDir,omitempty"`
}
```

字段注释明确说了：`SuggestedDownloadDir` 只是给 UI 的**提示**，最终目录由 create 调用决定。它由 `suggestedDownloadDir`（[pkg/download/category.go:L75-L93](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category.go#L75-L93)）计算——只报告分类决策会给出的目录，不展开占位符、不写任何东西。

服务层的分流入口在 `pkg/api/service.go`：`resolve` handler 调 `Downloader.Resolve`（[pkg/api/service.go:L141-L151](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L141-L151)），而 `createTask` handler 按请求体里是 `rid` 还是 `req` 分成两条路径（[pkg/api/service.go:L162-L168](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L162-L168)）：

```go
if request.Rid != "" {
	taskID, err = s.Downloader.CreateWithOptions(request.Rid, request.Opts)
} else if request.Req != nil {
	taskID, err = s.Downloader.CreateDirect(request.Req, request.Opts)
} else {
	return newErrorResponse("param invalid: rid or req", ...)
}
```

还有一个值得注意的细节：在**生产代码**里，`fetcherCache` 的调用点恰好三处（可用 Grep 自行验证）——`Resolve` 的写入（[pkg/download/downloader.go:L590](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L590)）、`createResolved` 入口的「取出即删除」（[pkg/download/downloader.go:L870-L871](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L870-L871)）、失败时的「归还」（[pkg/download/downloader.go:L881](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L881)）。后面两处的语义是 4.3 节的主角。

#### 4.1.4 代码实践

**实践目标**：验证「Resolve 缓存的是活 Fetcher，且全项目只有 createResolved 这一个消费者」。

**操作步骤**（源码阅读型，配合 Grep）：

1. 在 `pkg/download` 目录下搜索 `fetcherCache`（排除 `_test.go`），列出所有读写点；
2. 打开测试 `TestDownloader_Resolve`（[pkg/download/downloader_test.go:L241-L271](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L241-L271)），观察它 Resolve 之后检查了 `rr.Res` 的哪些字段；
3. 阅读 `TestDownloader_Create`（[pkg/download/downloader_test.go:L767-L802](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L767-L802)），注意它 `Resolve` → `CreateWithOptions(rr.ID, opts)` → 等 `EventKeyDone` → 校验文件 MD5 的完整流程。

**需要观察的现象**：`fetcherCache` 的读点与删点在 `createResolved` 的开头同处一个 `fetcherMapLock` 临界区，成对出现。

**预期结果**：你能说出"Resolve 与 Create 必须成对调用，且用同一个 `rr.ID`"，以及"扩展短路路径返回的 `ResolveResult.ID` 为空，只能配 `CreateDirect`"。

（本实践为源码阅读型，无需运行命令。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ResolveResult.SuggestedDownloadDir` 要注明"只是提示，create 才决定最终路径"？

**参考答案**：Resolve 与 Create 是两次独立调用，中间用户可能改配置、改目录、换选项；而且 `suggestedDownloadDir` 只做分类匹配与白名单校验，不展开 `%year%` 之类占位符。真正的目录决策发生在任务启动前的 `prepareTask` → `decideDownloadDirForTask`，那时才会展开占位符、做白名单校验并在分类目录被拒时回退到基础目录。提示与决策分离，保证最终写入磁盘的路径永远经过完整校验。

**练习 2**：如果用户对同一个 URL 先后调用两次 `Resolve` 但从不 `Create`，会发生什么？

**参考答案**：两次各自生成独立 `rrId`，`fetcherCache` 里会留下两个再也不会被消费的 Fetcher（HTTP 场景下可能还挂着预取连接与临时数据）。它们不会被自动清理，直到进程退出。这是当前实现的已知取舍——解析缓存以内存换取"Create 时免二次请求"的收益。

### 4.2 CreateDirect、批量入口与 doCreate：任务骨架装配

#### 4.2.1 概念说明

建任务有三类入口，但底层只有一条装配线：

- **CreateDirect（直建）**：一个 URL 直接变成任务。CLI（`Boot().Create()`，[pkg/download/downloader.go:L3159-L3165](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L3159-L3165)）、REST 的 `req` 分支走这条路。任务创建时 `Resource` 还没解析（`task.Meta.Res == nil`），解析被**推迟**到任务启动时在 `doStart` 里做——所以叫"延迟解析"。
- **Create / CreateWithOptions（消费解析缓存）**：先用 `Resolve` 拿到 `rr.ID`，UI 展示资源、用户选好文件与目录后，再用 `rr.ID` 建任务。此时 Fetcher 已经持有解析好的 `Resource`，任务启动时无需再解析（详见 4.3）。
- **CreateDirectBatchWithResult（批量）**：一次请求建多个任务，成员可以混合 `rid`（已解析资源）与 `req`（直建 URL），可选 `group=true` 把整批任务收进一个共享目录的分组（分组的完整生命周期是 u4-l6 的主题，本讲只看它如何复用装配线）。

三条入口最终都汇入同一个函数 `doCreate`，它负责装配 `Task` 骨架、落盘、并决定任务"立即启动"还是"进等待队列"。

#### 4.2.2 核心流程

```
路径 A：CreateDirect(req, opts)             路径 B：createResolved(rrId, opts, "")   路径 C：批量
├── prepareDefaultPath(opts)                ├── （见 4.3，产出 fetcher）            ├── req.Group？
├── createDirect(req, opts, "")             │                                       │    └── createTaskGroup 预留组目录
│    ├── ensureRequestRawURL                │                                       ├── 逐成员循环：
│    ├── buildFetcher(URL)                  │                                       │    ├── opts 归属：成员 Opts → 批级 Opts → 空（总是 Clone）
│    ├── fetcher.Meta().Req = req           │                                       │    ├── 分组成员：相对路径包含性检查；强制进组目录、SkipAutoCategorize
│    └── initOptions                        │                                       │    └── rid 优先 → createResolved
│         └───────────┐                     │                    否则 req → createDirect；皆无 → 记失败
└─────────────────────┼─────────────────────┴──► doCreate(fetcher, opts, groupID)
                                                    ├── parseFm(URL) 再次路由协议
                                                    ├── NewTask()：ID=nanoid、Status=ready
                                                    ├── task.ParentID = groupID
                                                    ├── 装配：fetcherManager/fetcher/Protocol/Meta/Progress/Uploading
                                                    ├── NeedsPreparation = true
                                                    ├── initTask：重建 timer、四把锁、测速数组
                                                    └── d.lock 内：
                                                         ├── groupID 非空 → 查 d.groups，无则 ErrTaskNotFound
                                                         ├── storage.Put(bucketTask, clone)   # 先落盘
                                                         ├── taskId = task.ID                  # 落盘成功才置
                                                         ├── tasks = append(tasks, task)
                                                         ├── 满载 → Status=wait、进 waitTasks
                                                         └── 有空位 → doStart(task)
```

#### 4.2.3 源码精读

**路径 A 入口**：`CreateDirect`（[pkg/download/downloader.go:L627-L637](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L627-L637)）外层只做"默认目录"处理：`prepareDefaultPath`（[pkg/download/downloader.go:L1802-L1815](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1802-L1815)）要求调用方**显式声明** `SkipAutoCategorize && AsDefaultPath` 且给了非空 `Path`，这个目录才候选成为全局默认下载目录，且要先过 `initDownloadPath` 白名单；建任务成功后 `persistDefaultPathBestEffort` 尽力持久化（失败只记 warning）。真正干活的是 `createDirect`（[pkg/download/downloader.go:L639-L652](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L639-L652)）：建 Fetcher、挂上请求、跑 `initOptions`，然后交给 `doCreate`。

**路径 C 批量入口**：`CreateDirectBatchWithResult`（[pkg/download/downloader.go:L664-L753](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L664-L753)），旧 Go API `CreateDirectBatch` 保留为只取 TaskIDs 的兼容包装（[pkg/download/downloader.go:L654-L662](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L654-L662)）。请求模型在 [pkg/base/model.go:L202-L213](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L202-L213)：批级 `Opts` + 成员 `Rid`/`Req`/`Opts` + `Group`/`GroupName`。逐成员分派的核心 switch（[pkg/download/downloader.go:L730-L740](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L730-L740)）：

```go
switch {
case item == nil:
	itemErr = errors.New("task request or rid is required")
case item.Rid != "":
	taskID, itemErr = d.createResolved(item.Rid, opts, groupID)  // rid 优先
case item.Req != nil:
	taskID, itemErr = d.createDirect(item.Req, opts, groupID)
default:
	itemErr = errors.New("task request or rid is required")
}
```

批量规则的四条要点（[ _docs/task-batch-api.md](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/_docs/task-batch-api.md) 有官方说明）：

1. **rid 优先**：成员同时带 `rid` 与 `req` 时走 `createResolved`；
2. **opts 整体替换**：成员 `Opts` 非空就整体替换批级 `Opts`（缺省字段不从批级继承），否则用批级 `Opts`，再不然用空选项——且非空时总是 `Clone()` 后再改（[pkg/download/downloader.go:L698-L705](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L698-L705)），因为多个成员可能共享同一个 Options 指针；
3. **部分成功**：单个成员失败只记入 `failures` 不中断循环；只要有一个任务落盘成功就返回 `CreateTaskBatchResult{TaskIDs, ParentID}`（[pkg/download/model.go:L40-L44](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L40-L44)），全部失败才报错（[pkg/download/downloader.go:L748-L750](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L748-L750)）；
4. **分组成员路径约束**：`group=true` 时先 `createTaskGroup`（[pkg/download/downloader.go:L757-L826](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L757-L826)）预留一个去重后缀的组目录；成员的相对 `Path` 会被解释为**组目录内的子目录**并做包含性检查（不得逃出组目录），随后强制 `opts.Path = 组目录`、`opts.SkipAutoCategorize = true`（[pkg/download/downloader.go:L708-L729](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L708-L729)）。REST 层 `createTaskBatch` handler 直接调它并返回结果对象（[pkg/api/service.go:L175-L188](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L175-L188)）。

**汇合点 doCreate**（[pkg/download/downloader.go:L2163-L2221](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2163-L2221)）：

```go
func (d *Downloader) doCreate(f fetcher.Fetcher, opts *base.Options, groupID string) (taskId string, err error) {
	if f.Meta().Opts == nil { f.Meta().Opts = opts }  // 路径 A 由 createDirect 已设好，这里兜底
	ensureRequestRawURL(f.Meta().Req)

	fm, err := d.parseFm(f.Meta().Req.URL)            // 建任务时按 URL 再路由一次协议
	task := NewTask()                                  // ID=nanoid、Status=ready、时间戳
	task.ParentID = groupID                            // 分组归属写入公开层
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

	func() {
		d.lock.Lock()
		defer d.lock.Unlock()

		if groupID != "" {                             // 分组任务：组元数据必须还在
			task.group = d.groups[groupID]
			if task.group == nil { err = ErrTaskNotFound; return }
		}
		if err = d.storage.Put(bucketTask, task.ID, task.clone()); err != nil { return }
		taskId = task.ID                               # 只有落盘成功才置 taskId
		d.tasks = append(d.tasks, task)
		if d.remainRunningCount() == 0 {               // 满载：排队
			task.Status = base.DownloadStatusWait      // 直写字段，不走 updateStatus
			d.waitTasks = append(d.waitTasks, task)
			return
		}
		err = d.doStart(task)                          // 有空位：立即启动
	}()
	return
}
```

五个细节值得咀嚼：

1. `parseFm` 在建任务时**再次**执行——协议归属永远以"当时的 URL"为准（u3-l2 讲过路由贯穿多个时刻，这里是其中之一）。
2. `NewTask()`（[pkg/download/model.go:L119-L130](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L119-L130)）生成 nanoid 短 ID 与 `ready` 初始状态；`task.ParentID = groupID` 把分组归属写进公开层（普通任务为空串，序列化时 `omitempty` 省略）。
3. **落盘在 `d.lock` 临界区内完成，且 `taskId` 只在 `storage.Put` 成功之后才赋值**。这个顺序是 4.3 节 `createResolved` 判断"fetcher 是否已被消费"的依据：`taskId != ""` ⇒ 任务已持久化，解析缓存里的 Fetcher 已归属这个任务。
4. 进等待队列用的是 `task.Status = base.DownloadStatusWait` **直写**而非 `updateStatus`——因为此刻任务尚无人观测，且已在 `d.lock` 临界区内，无需再抢 `statusLock`。
5. 任务在**调度之前**就已写入 `bucketTask`，即使进程此刻崩溃，重启后也能恢复出这个任务（恢复语义见 u4-l5）。

`initTask`（[pkg/download/downloader.go:L3057-L3066](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L3057-L3066)）给 Task 装上序列化会丢失的运行时内核：以已用时长初始化的 `timer`、`statusLock`/`lock`/`statsLock`/`blobRefLock` 四把锁、下载与上传两个测速数组。

`initOptions`（[pkg/download/downloader.go:L2233-L2259](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2233-L2259)）做三件"保底"事：nil 选项变空选项；非 HTTP 协议剥掉 `Size` 估计值（`httpSizeOptions`，[pkg/download/downloader.go:L2223-L2231](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2223-L2231)）；`Path` 为空回退到全局 `DownloadDir`。注意目录此刻只是"填了个底"，真正的决策与白名单校验在启动时的 `prepareTask`。

#### 4.2.4 代码实践

**实践目标**：写出「从 Resolve 到 fetcher.Start() 之间每一步对 Task 字段的修改清单」的初版（后半程在 4.5 补完）。

**操作步骤**：

1. 通读 `doCreate`，对照 `Task` 结构体字段（[pkg/download/model.go:L49-L92](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L49-L92)）逐行标注"这一行改了哪个字段"；
2. 把结果整理成表格（参考 4.5.4 给出的完整答案前半部分）；
3. 对照 `TestDownloader_CreateDirectBatch`（[pkg/download/downloader_test.go:L915-L987](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L915-L987)），观察批量直建如何对同一 URL 建 5 个任务并依赖重名改名得到 5 个不同文件名。

**需要观察的现象**：`doCreate` 里对 Task 的写入全部发生在 `storage.Put` 之前（除最后的 wait/启动分支），也就是"先定型、再落盘、后调度"。

**预期结果**：你能不看源码说出 `doCreate` 装配了哪些字段、哪些属于公开层（会序列化）、哪些属于运行时层（`initTask` 重建），以及 `taskId` 为什么只在落盘成功后才赋值。

（本实践为源码阅读型，无需运行命令。）

#### 4.2.5 小练习与答案

**练习 1**：`CreateDirect` 路径建出的任务，`task.Meta.Res` 是什么？什么时候才有值？

**参考答案**：是 `nil`。资源解析被推迟到任务启动时：`doStart` 的 handler 里 `resolvedHere := task.Meta.Res == nil`，为真则调 `task.fetcher.Resolve(task.Meta.Req)` 并把结果回填 `task.Meta.Res`。这样设计让"排队中的任务"不占用解析资源，也避免了"解析了但任务永远不被调度"的浪费。

**练习 2**：批量创建时，成员的 `Opts` 与批级的 `Opts` 是什么关系？为什么代码里总是 `Clone()`？

**参考答案**：成员 `Opts` 非空则**整体替换**批级 `Opts`（不是逐字段合并），为空则继承批级，再为空则用空选项。`Clone()` 是防御性拷贝——批量场景多个成员可能共享同一个 Options 指针（批级 Opts 会被多个成员继承），分组成员还会原地改写 `Path` 与 `SkipAutoCategorize`，不拷贝会互相污染（`httpSizeOptions` 的注释同样强调了 Clone before filtering，因为 batch tasks may share options）。

**练习 3**：`prepareDefaultPath` 为什么要求 `SkipAutoCategorize && AsDefaultPath` 两个标志同时成立？

**参考答案**：注释写得很直白——只有用户**显式点名**的目录才能成为全局默认下载目录。保留自动分类决策的目录"归任务自己所有"，后端不能擅自把它升级为全局默认；不加双重确认，一次随手的选择就会悄悄改掉所有后续任务的落盘位置。

**练习 4**：为什么 `storage.Put(bucketTask, ...)` 要放在 `d.lock` 临界区内、且在分组校验之后？

**参考答案**：对分组任务，必须先确认 `d.groups[groupID]` 仍然存在（组可能已被并发删除）再落盘，否则会落出一个"孤儿 ParentID"的任务；与落盘、入队同处一个临界区，使「组存在性检查 → 持久化 → 进入任务列表」对并发的 `Delete`（同样持 `d.lock` 操作任务列表，u4-l4 详述）表现为原子步骤。同时 `taskId` 只在落盘成功后赋值，这正是 `createResolved` 区分「消费了缓存」与「该归还缓存」的判定依据。

### 4.3 createResolved 与 fetcherCache：单一创建者消费与失败归还

#### 4.3.1 概念说明

`CreateWithOptions` 现在是一个薄包装（[pkg/download/downloader.go:L854-L866](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L854-L866)）：先做 `prepareDefaultPath`（全局默认目录的候选逻辑），再调拆出来的核心 `createResolved(rrId, opts, "")`。拆分的动机是批量入口——`CreateDirectBatchWithResult` 需要带着 `groupID` 调用同一段逻辑，而不该重复实现。

`createResolved` 对解析缓存实现了两条语义，源码注释（[pkg/download/downloader.go:L876-L877](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L876-L877)）一句话讲完：

> "Only one creator may use a resolved fetcher. Failed creation makes it available again; a persisted task consumes it even if starting fails."
> （同一解析结果只允许一个创建者使用。创建失败时它重新可用；一旦任务已持久化，即使随后启动失败，它也算被消费了。）

- **单一创建者**：进入 `createResolved` 的第一件事就是拿**写锁**，把 fetcher 从缓存里取出的同时删掉。两个并发调用拿同一个 `rid`，只有一个能取到，另一个立刻得到 `invalid resource id`。旧实现是读锁取出、函数末尾才删，存在"两个创建者都取到同一个 Fetcher"的窗口；新实现把「读 + 删」合并成一个原子步骤。
- **失败归还**：`defer` 检查 `taskId == ""`——若创建中途失败（选项校验不过、存储写入失败等），把 Fetcher **放回缓存**，同一个 `rid` 可以重试。为什么值得归还？因为缓存的 Fetcher 携带解析成果（HTTP 场景还可能有预取连接与数据），一次瞬时故障不该让整个解析白做。
- **落盘即消费**：一旦 `doCreate` 里 `storage.Put` 成功、`taskId != ""`，任务就**拥有**了这个 Fetcher（`task.fetcher = f`），此后下载启动失败、甚至任务进入 error 态，缓存也不会恢复——否则同一个 Fetcher 会被两个任务同时持有，重试会造成任务重复。

#### 4.3.2 核心流程

```
createResolved(rrId, opts, groupID)
├── fetcherMapLock.Lock()                  # 写锁！
│    ├── fetcher = fetcherCache[rrId]
│    └── delete(fetcherCache, rrId)        # 取出即删除：单一创建者
├── 不存在 → "invalid resource id"
├── defer：taskId == ""（没落盘）→ fetcherCache[rrId] = fetcher   # 失败归还
├── initOptions(opts.Clone(), URL)         # 防御性拷贝 + 三件保底
├── 校验 SelectFiles 下标 → InitSelectFiles 展开为显式列表
├── fetcher.Meta().Opts = taskOpts
├── res.CalcSize(SelectFiles)              # 覆盖式重算大小
├── （http）taskOpts.Size > 0 → fetcher.Patch 提交大小估计值
└── doCreate(fetcher, nil, groupID)        # taskId 非空 ⇒ fetcher 已被消费
```

#### 4.3.3 源码精读

`createResolved` 全函数（[pkg/download/downloader.go:L868-L913](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L868-L913)）：

```go
func (d *Downloader) createResolved(rrId string, opts *base.Options, groupID string) (taskId string, err error) {
	d.fetcherMapLock.Lock()                       // 写锁
	fetcher, ok := d.fetcherCache[rrId]
	delete(d.fetcherCache, rrId)                  // 取出即删除
	d.fetcherMapLock.Unlock()
	if !ok {
		return "", errors.New("invalid resource id")
	}
	// Only one creator may use a resolved fetcher. Failed creation makes it
	// available again; a persisted task consumes it even if starting fails.
	defer func() {
		if taskId == "" {                         // 没有落盘成功 → 归还
			d.fetcherMapLock.Lock()
			d.fetcherCache[rrId] = fetcher
			d.fetcherMapLock.Unlock()
		}
	}()
	if opts == nil {
		opts = &base.Options{}
	}
	taskOpts, err := d.initOptions(opts.Clone(), fetcher.Meta().Req.URL)
	if err != nil { return "", err }
	if res := fetcher.Meta().Res; res != nil {
		for _, index := range taskOpts.SelectFiles {   // 入口先校验下标
			if index < 0 || index >= len(res.Files) { return "", fmt.Errorf(...) }
		}
		taskOpts.InitSelectFiles(len(res.Files))       // 展开为显式列表
	}
	fetcher.Meta().Opts = taskOpts
	if res := fetcher.Meta().Res; res != nil {
		res.CalcSize(fetcher.Meta().Opts.SelectFiles)  // 覆盖式重算大小
	}
	// Resolve has no creation options. Publish a submitted HTTP estimate now
	// while preserving any size already confirmed by the response.
	if taskOpts.Size > 0 {                             // http：把大小估计 Patch 进 fetcher
		if err := fetcher.Patch(nil, &base.Options{Size: taskOpts.Size}); err != nil { return "", err }
	}
	taskId, err = d.doCreate(fetcher, nil, groupID)
	return
}
```

两条语义各有专门测试背书：

- **失败归还**：`TestDownloader_ResolvedCreateFailureRetainsResourceID`（[pkg/download/downloader_test.go:L4503-L4523](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4503-L4523)）用一个"第一次 Put 必失败"的存储桩：第一次 `CreateWithOptions` 失败后，关掉故障，**用同一个 rid** 再次 `CreateWithOptions` 必须成功——证明失败时 Fetcher 被放回了缓存。
- **落盘即消费**：`TestDownloader_ResolvedBatchPreparationFailureConsumesResourceID`（[pkg/download/downloader_test.go:L5116-L5143](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L5116-L5143)）让"准备阶段保存"失败：任务落盘后进入 error 态，此时再用同一 rid 建 task 必须报错，且任务列表中不能出现重复任务。
- **单一创建者**：`TestDownloader_ConcurrentResolvedCreation`（[pkg/download/downloader_test.go:L4916](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4916) 起）用一个带闸门的假 Fetcher 卡住第一次创建的 `Patch`，与此同时发起的第二次创建必须立刻失败（断言见 [pkg/download/downloader_test.go:L4962-L4969](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4962-L4969)），而且**占用一个 rid 不能阻塞别的 rid**。

`Create` 是 `CreateWithOptions(rrId, nil)` 的别名（[pkg/download/downloader.go:L850-L852](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L850-L852)）。

#### 4.3.4 代码实践

**实践目标**：亲手验证「失败归还、落盘即消费、单一创建者」三条语义。

**操作步骤**（可运行型）：

1. 运行三个针对性测试并阅读断言：
   ```
   go test ./pkg/download/ -run 'TestDownloader_ResolvedCreateFailureRetainsResourceID|TestDownloader_ResolvedBatchPreparationFailureConsumesResourceID|TestDownloader_ConcurrentResolvedCreation' -v
   ```
2. 对照 `taskGroupTestStorage` 的 `beforePut` 钩子理解"注入存储故障"的手法；
3. 阅读闸门 Fetcher `resolvedCreateTestFetcher`（[pkg/download/downloader_test.go:L4901-L4914](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4901-L4914)）——它的 `Patch` 第一次被调用时阻塞，直到测试放行，这就是制造"创建中并发再来一个"的手段。

**需要观察的现象**：三个测试全部 PASS；`ConcurrentResolvedCreation` 覆盖了 `单建/批量 × 先来/后来` 的四种组合。

**预期结果**：你能向别人解释"为什么归还的判定条件是 `taskId == ""` 而不是 `err != nil`"——因为 `doCreate` 只在 `storage.Put` 成功后才置 `taskId`，这与「任务是否已拥有 Fetcher」严格对应；而 `err` 可能来自落盘之后的启动阶段，那时任务已经存在，绝不能归还。

（测试运行结果**待本地验证**；断言内容直接源自源码阅读。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `createResolved` 入口用**写锁**完成「取出 + 删除」，而不是像旧版那样读锁取出、末尾再删？

**参考答案**：读-改-删分成两步的话，两个并发的 `createResolved` 可能都通过 `fetcherCache[rrId]` 拿到同一个 Fetcher、各自建出一个任务，同一个解析结果被消费两次。写锁下「取出 + 删除」是一个原子步骤，天然保证只有一个创建者。

**练习 2**：如果 `doCreate` 落盘成功、但任务启动失败进入 error，之后用户用同一个 rid 再次 Create，会发生什么？为什么这是正确行为？

**参考答案**：得到 `invalid resource id`（缓存已被删除且不归还）。因为 Fetcher 已经归属于第一个任务（`task.fetcher = f`，里面可能还持有传输状态与统计），再归还就会让两个任务共享同一个有状态执行者；正确做法是继续/重试**那个已存在的任务**，而不是再建一个。

**练习 3**：`doCreate(fetcher, nil, groupID)` 的第二个参数传 nil 是为什么？

**参考答案**：`opts` 参数只用于 `f.Meta().Opts == nil` 时的兜底赋值。路径 B 中 `fetcher.Meta().Opts` 已在 `createResolved` 前面显式设为 `taskOpts`，永远非 nil，所以传 nil 即可；这个兜底主要服务路径 A 的调用形态。

### 4.4 notifyRunning 与 wait 队列：MaxRunning 调度

#### 4.4.1 概念说明

`MaxRunning` 限制同时处于 running 状态的任务数。`doCreate` 发现满载时把任务标记为 `wait` 并塞进 `waitTasks`（FIFO 切片）；当某个正在跑的任务腾出名额（完成、出错、被暂停、被删除），就需要有人把队首任务拉起来——这件事由 `notifyRunning` 完成。

可以把 `waitTasks` 理解为一个"候车厅"：名额是座位，`notifyRunning` 是每次有人起身时的广播员。

#### 4.4.2 核心流程

```
名额释放点（谁调用 notifyRunning）：
  Pause（腾出被暂停任务的名额；暂停 wait 任务也同步清理 waitTasks）
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

#### 4.4.3 源码精读

`notifyRunning` 与 `remainRunningCount`（[pkg/download/downloader.go:L600-L625](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L600-L625)）：

```go
func (d *Downloader) notifyRunning() {
	go func() {
		d.lock.Lock()
		defer d.lock.Unlock()

		remainRunningCount := d.remainRunningCount()
		if remainRunningCount == 0 { return }   // 仍满载
		if len(d.waitTasks) > 0 {
			wt := d.waitTasks[0]                 // FIFO：先来先服务
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

`notifyRunning` 的四个调用点分别在 [pkg/download/downloader.go:L997](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L997)（Pause）、[pkg/download/downloader.go:L1251](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1251)（Delete）、[pkg/download/downloader.go:L1936](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1936)（watch 循环内任务终态后）与 [pkg/download/downloader.go:L2085](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2085)（handleOnError）。

两个设计要点：

- **异步 + 抢主锁**：`notifyRunning` 自己起 goroutine 并抢 `d.lock`，调用方不必关心锁状态，直接调用即可，不会阻塞当前流程。
- **在锁内调 doStart 安全吗？** 安全。下一节会看到 `doStart` 的同步段只做状态检查与置位（拿的是 `task.statusLock`，与 `d.lock` 不冲突），真正耗时的启动工作全在它自己 spawn 的 goroutine 里。这正是"两段式结构"带来的好处。

顺带一提，`Pause` 现在会在 `doPause` 之前**同步**把被暂停的任务从 `waitTasks` 里清掉（[pkg/download/downloader.go:L978-L990](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L978-L990)）——wait 态任务本来就没在传输，"暂停"它等于直接出队，不必等 `notifyRunning` 轮到它。完整的暂停/继续/删除语义在 u4-l4 展开。

#### 4.4.4 代码实践

**实践目标**：用 Grep 验证四个 `notifyRunning()` 调用点各自所在的函数。

**操作步骤**：

1. 在 `pkg/download/downloader.go` 中搜索 `d.notifyRunning()`；
2. 对每个命中行，向上滚动找到它所属的外层函数（提示：分别在 `Pause`、`Delete`、`watch`、`handleOnError` 内）；
3. 思考：为什么 `Continue`（继续任务）不在其中也能唤醒等待队列？（提示：`Continue` 自己就直接调度了。）

**需要观察的现象**：四个调用点都是"刚释放了一个 running 名额"的位置。

**预期结果**：能说出"名额释放 → notifyRunning → 队首出队 → doStart"这条唤醒链，以及它是异步的（调用方不等队首真的启动）。

（本实践为源码阅读型，无需运行命令。）

#### 4.4.5 小练习与答案

**练习 1**：`waitTasks` 是什么顺序的队列？会不会出现"排在前面的任务先建却后跑"？

**参考答案**：FIFO（`append` 入队、`waitTasks[0]` 出队）。单次唤醒严格按入队顺序；但任务也可能经 `Continue`（继续）等路径不经过队列直接被调度，所以"创建顺序 = 启动顺序"只在"全部经队列唤醒"的场景下成立。

**练习 2**：如果 `doStart` 的同步段就失败（例如 restore 出错），名额会被浪费吗？

**参考答案**：会少一次启动，但不会死锁。`doStart` 出错路径走 `doOnError` 把任务置为 error，而 `handleOnError` 在确认任务进入 error 后会调用 `notifyRunning()` 重新尝试唤醒下一个等待者（[pkg/download/downloader.go:L2085](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2085)），所以队列不会因为一个坏任务而卡死。

### 4.5 doStart：statusMut 同步段与异步 handler

#### 4.5.1 概念说明

`doStart` 是主链路的"发动机"，但它不是一个从上到下执行完的函数，而是**两段式**结构：

- **同步段（`statusMut` 包裹）**：拿 `task.statusLock`，做三件事——拦截不该启动的任务（已 running/done）、恢复 Fetcher、把状态置为 running 并递增 `runGeneration`。这段又快又关键，必须在调用方（可能持有 `d.lock`）的语境里立即完成。
- **异步段（`handler` 闭包 + `go func`）**：拿 `task.lock`，做所有慢活——扩展钩子、延迟解析、目录准备、`fetcher.Start()`、统计与落盘。调用方拿到 `err == nil` 就返回了，不必等下载真正开始。

为什么必须分两段？因为"状态置为 running"必须**立刻**可见（否则并发的 Pause/Continue 会做出错误判断），而启动过程又慢到不能阻塞调度方。代数 `generation` 则是两段之间的"接力棒"：同步段记下本次启动的代数，异步段在每个关键步骤后核对，一旦发现任务已被暂停（代数变了或状态不是 running），立刻放弃剩余动作。

#### 4.5.2 核心流程

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
    ├── 核对代数①（taskIsRunningGeneration）
    ├── fetcher 仍为 nil → restoreFetcher（第二次机会）
    ├── triggerOnStart（扩展钩子）→ 再核对代数②
    ├── fetcher.Meta().Req/Res/Opts = task.Meta.*   # task.Meta 是权威数据源
    ├── syncBlobTaskLease
    ├── resolvedHere（Meta.Res == nil）→ fetcher.Resolve(req)（延迟解析）
    ├── NeedsPreparation？
    │     ├── 是 → （AutoRename 则先拿 checkDuplicateLock）prepareTask
    │     └── 否 → initDownloadPathForTask(Path, task.group) 复检
    │               # 分组任务：包含性 + 组根白名单；普通任务：占位符 + 白名单
    ├── statusLock 单临界区：核对代数③ + Progress.Speed=0 + timer.Start()
    ├── fetcher.Start()          # 同步返回错误；成功则 started = true
    ├── captureTaskStats（失败仅告警：统计是辅助数据）
    ├── saveTask（bucketSave + bucketTask 落盘）
    └── 收尾：
        ├── 失败且代数仍有效 → doOnError
        └── 成功 → emit(EventKeyStart) + go watch(task)
```

`taskIsRunningGeneration`（[pkg/download/downloader.go:L2577-L2584](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2577-L2584)）的判定很简单：`Status == running && runGeneration == generation`，两条件缺一都算"我已过时"。

#### 4.5.3 源码精读

**同步段**（[pkg/download/downloader.go:L2370-L2386](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2370-L2386)）：

```go
isReturn, err := d.statusMut(task, func() (isReturn bool, err error) {
	if task.Status == base.DownloadStatusRunning || task.Status == base.DownloadStatusDone {
		isReturn = true
		return
	}
	err = d.restoreTask(task)                       // fetcher 为 nil 时按存储内容重建
	if err != nil { ...; return }
	task.updateStatus(base.DownloadStatusRunning)   // UpdatedAt + Status
	task.runGeneration++
	generation = task.runGeneration                 # 本次启动的代数
	return
})
```

`statusMut` 本体（[pkg/download/downloader.go:L2300-L2305](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2300-L2305)）只是"拿 statusLock、执行闭包、还锁"的包装。`updateStatus`（[pkg/download/model.go:L175-L184](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L175-L184)）在置状态的同时刷新 `UpdatedAt`，且只在进入 done 时写 `DoneAt`（一次性写入语义，u2-l2 讲过）。

**restoreFetcher**（[pkg/download/downloader.go:L2138-L2161](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2138-L2161)）是 u3-l4 讲过的两段式持久化契约的消费端：`Manager.Restore()` 返回存储模板 `v` 与构造器 `f`；先用 `storage.Get(bucketSave, task.ID, v)` 把落盘的协议状态灌进模板，再 `f(task.Meta, v)` 造出 Fetcher；构造器返回 nil 则 `Build()` 兜底；随后 `setupFetcher` 注入 Controller，并在 Meta 的 Req/Res/Opts 为 nil 时用 `task.Meta` 回填（**仅 nil 时回填**——已有值说明 Fetcher 自带更新鲜的数据，比如刚解析完的 Resource）。

**异步段 handler**（[pkg/download/downloader.go:L2396-L2471](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2396-L2471)），关键节点：

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
			d.checkDuplicateLock.Lock(); defer d.checkDuplicateLock.Unlock() // 为何？见 4.6
		}
		if err := d.prepareTask(task); err != nil { return err }
	} else if _, err := d.initDownloadPathForTask(task.Meta.Opts.Path, task.group); err != nil {
		return err    // 已准备过的任务每次启动仍复检（分组任务走包含性 + 组根白名单）
	}

	task.statusLock.Lock()                                        # 临界区③：三步合一
	if task.Status != base.DownloadStatusRunning || task.runGeneration != generation {
		task.statusLock.Unlock()
		return nil
	}
	task.Progress.Speed = 0
	task.timer.Start()
	task.statusLock.Unlock()
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

注意临界区③（[pkg/download/downloader.go:L2450-L2457](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2450-L2457)）把「核对代数—清零速度—启动计时器」**合并成一个 `statusLock` 临界区**，且核对是内联判断而非调用 `taskIsRunningGeneration`——后者自己要拿 `statusLock`，持锁重入会死锁。为什么要合并？设想分离的旧写法（先核对、解锁，再单独清零，最后锁外 `timer.Start()`）：核对通过之后、`timer.Start()` 之前，一个并发 `Pause` 可能插入（它在自己的临界区里 `timer.Pause()`），随后 handler 照样 `timer.Start()`，把已暂停任务的计时器重新拨动，`Progress.Used` 时长被污染。合并之后，暂停要么发生在临界区之前（handler 看到 Status 不是 running，直接放弃），要么发生在其后（由暂停方负责停表），不再有夹缝。

三次代数核对（①②③）各挡一种竞态：①挡"启动前任务已被暂停/重启"，②挡"扩展钩子执行期间状态变化"，③挡"准备目录期间状态变化"。`captureTaskStats` 失败只告警不失败——注释明说"统计是辅助数据，不能把已启动的下载变成失败任务"。`saveTask`（[pkg/download/downloader.go:L511-L536](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L511-L536)）先让协议 `Store` 序列化 Fetcher 写 `bucketSave`，再在 `statusLock` 下克隆 Task 写 `bucketTask`——这是 u4-l5 持久化主题的入口，本讲只记住"启动完成后立刻存了一份检查点"。

`watch` 的开头（[pkg/download/downloader.go:L1858-L1864](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1858-L1864)）用 `watchedTasks.LoadOrStore` 保证同一任务只有一个 watch 循环，它的完整逻辑是下一讲（u4-l3）的主角。

#### 4.5.4 代码实践

**实践目标**：用调试器（Delve）跟踪一个 HTTP 任务从 `Resolve` 到 `fetcher.Start()` 的完整调用链，并整理「每一步对 Task 字段的修改清单」。本实践与讲义的综合实践共用同一个程序，这里先给出断点清单与答案表格。

**操作步骤**：

1. 按第 5 节综合实践准备示例程序（放在 `_examples/lifecycle/main.go` 或独立 module）；
2. 用 Delve 启动并打断点：
   ```
   dlv debug ./_examples/lifecycle
   break pkg/download/downloader.go:2173   # doCreate: NewTask
   break pkg/download/downloader.go:2381   # doStart 同步段: updateStatus(running)
   break pkg/download/downloader.go:2420   # handler: resolvedHere
   break pkg/download/downloader.go:2458   # handler: fetcher.Start()
   continue
   ```
3. 每次命中后用 `print *task`（或 `p task.Status`、`p task.runGeneration`、`p task.NeedsPreparation`）记录字段变化。

**需要观察的现象**：四个断点依次命中；在第 2458 行断点处 `task.Status` 已是 `running`、`NeedsPreparation` 已变 `false`（HTTP 管理器 `AutoRename()` 为 true，`prepareTask` 已执行过）。

**预期结果**——Task 字段修改清单（完整版答案）：

| 序 | 调用点（行号） | 修改内容 | 所属层 |
| --- | --- | --- | --- |
| 1 | `NewTask`（model.go:L119-L130） | `ID`=nanoid、`Status`=ready、`CreatedAt`/`UpdatedAt`=now | 公开层 |
| 2 | `doCreate`（L2173-L2184） | `ParentID`=groupID（普通任务空串）、`fetcherManager`、`fetcher`、`Protocol`、`Meta`、`Progress=&Progress{}`、`Uploading`、`NeedsPreparation=true` | 混合（前四项公开层的内核来源） |
| 3 | `initTask`（L3057-L3066） | `timer`、`statusLock`/`lock`/`statsLock`/`blobRefLock`、`speedArr`/`uploadSpeedArr` | 运行时层（不序列化） |
| 4 | 分组校验 + `storage.Put`（L2197-L2207，d.lock 内） | 不改字段；组存在时挂 `task.group` 运行时指针；任务快照首次落盘 `bucketTask`，成功后 `taskId=task.ID` | 运行时 + 持久化 |
| 5 | 满载分支（L2210-L2214） | `Status`=wait（直写）、进入 `waitTasks` | 公开层 |
| 6 | `doStart` 同步段（L2381-L2383） | `Status`=running、`UpdatedAt`=now（`updateStatus`）、`runGeneration++`；此前可能已由 `restoreFetcher` 重建 `fetcher` | 公开层 + 运行时层 |
| 7 | handler 回填（L2412-L2416） | `fetcher.Meta().Req/Res/Opts` ← `task.Meta.*`（`Meta` 是共享指针，Task 与 Fetcher 看到同一份） | 共享数据 |
| 8 | 延迟解析（L2420-L2427，仅 CreateDirect 路径） | `task.Meta.Res` 首次赋值 | 公开层 |
| 9 | `prepareTask`（L2441 调用，详见 4.6） | `Meta.Opts.Path`=决策目录、`Meta.Opts.Name`=可能改名后的名字、`NeedsPreparation`=false；失败则保持 true | 公开层 |
| 10 | 速度清零（L2455，statusLock 临界区内） | `Progress.Speed`=0 | 公开层 |
| 11 | `fetcher.Start()` 后（L2458-L2469） | `saveTask` 写 `bucketSave`+`bucketTask`；`captureTaskStats` 填 stats | 持久化 |
| 12 | watch 循环（下一讲） | `Progress.Used/Speed/Downloaded` 周期刷新；终态时 `Status`=done/error、`DoneAt` | 公开层 |

（断点行为为静态推导，**待本地验证**；表格内容直接源自源码逐行阅读。）

#### 4.5.5 小练习与答案

**练习 1**：为什么 `fetcher.Start()` 的错误用同步返回值处理，而下载中途的错误走 `DoneCh`/`Wait()` 异步上报？

**参考答案**：`Start()` 的职责是"把传输跑起来"——建目录、建文件、发起首批连接，这些步骤的失败是启动失败，调用方（handler）必须立刻知道并走 `doOnError`。而传输中途断线、校验失败发生在 `Start()` 返回之后，只能由 Fetcher 通过完成通道（u3-l1 的 DoneCh 契约）异步通知，由 watch 循环读 `Wait()` 翻译成 error 状态。

**练习 2**：`doStart` 已经在同步段调了 `restoreTask`，为什么 handler 里还要再判一次 `task.fetcher == nil` 并 `restoreFetcher`？

**参考答案**：防御性兜底。同步段与 handler 是两个时间点，中间隔着 goroutine 调度；此外某些路径（如 `Patch`）也独立恢复 Fetcher。二次检查保证 handler 拿到的一定是可用实例，属于"不变式在关键位置重复确认"的写法。

**练习 3**：已经 `NeedsPreparation=false` 的任务每次启动为什么还要跑 `initDownloadPathForTask` 白名单校验？分组任务和普通任务走的检查有何不同？

**参考答案**：源码注释给了答案——"已准备的任务保留它的目录，但目录在**每次启动**时仍要通过白名单"。目录在首次准备时定死不再改（保证断点续传写同一位置），但白名单是**运行时安全策略**，配置可能中途收紧，所以每次启动复检。普通任务走 `initDownloadPath`：展开占位符 + 白名单（[pkg/download/downloader.go:L2261-L2268](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2261-L2268)）；分组任务走 `initDownloadPathForTask` 的分组分支（[pkg/download/downloader.go:L2270-L2284](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2270-L2284)）：组目录在创建时已定稿，不再展开占位符，改为「任务路径必须落在组目录内（包含性检查）+ 组根目录过白名单」。

### 4.6 prepareTask：一次性目录决策与重名改名

#### 4.6.1 概念说明

`NeedsPreparation` 标记（[pkg/download/model.go:L64-L71](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L64-L71)）的字段注释值得逐句读：任务在资源解析后要跑**一次**准备——决定下载目录、解决目录内的重名——之后标志清除；排队、暂停、重试过的任务照样要准备（只要还没准备过），而**准备过的任务绝不会再被分类或改名**。

为什么要把"定目录"推迟到这里、而不是建任务时就定死？因为 CreateDirect 路径建任务时 `Resource` 还没解析（连文件名、扩展名都不知道），无从分类；而任务一旦准备过，目录与名字就锁定了，暂停/重试/重启都不能变，否则断点续传会写错位置。

分组任务是个例外路径：它的目录在批量创建时就已经被 `createTaskGroup` 在磁盘上**预留**（见 4.2.3），成员任务被强制 `SkipAutoCategorize=true`，于是 `prepareTask` 的分类逻辑整体旁路，任务直接落进组目录。

#### 4.6.2 核心流程

```
prepareTask(task)                     # 持有 task.lock，可能同时持有 checkDuplicateLock
├── Res 未解析 → 报错
├── 深拷贝预备稿：preparedMeta = *Meta、preparedOpts = *Opts、Res 深克隆
│     # 预备稿与活任务隔离：准备失败不污染线上数据
├── decideDownloadDirForTask(Opts, Res, task.group)
│     ├── 分组任务（group != nil 且 SkipAutoCategorize）
│     │     └── initDownloadPathForTask：包含性检查（不得逃出组目录）
│     │         + 组根目录白名单；不再展开占位符
│     └── 普通任务 → 分类路由 + 基础目录 + 占位符展开 + 白名单
│           # 分类目录被白名单拒 → 回退任务基础目录
├── AutoRename？（HTTP 系为 true）
│     ├── 文件名安全化 SafeFilename
│     ├── 目录型资源（Res.Name != ""）→ FolderPath() → CheckDuplicateAndRename
│     └── 单文件资源 → Files[0].Name → SingleFilepath() → CheckDuplicateAndRename
│     └── 改名结果统一写进 preparedOpts.Name（Opts.Name 优先级最高，u3-l1）
├── CalcSize 重算选中文件总大小
├── 先存候选：statusLock 内快照 preparedTask := *task，换 Meta + NeedsPreparation=false → saveTask 落盘
└── 后发布：statusLock 内 *task.Meta = preparedMeta；NeedsPreparation = false
```

「先存候选、后发布」是这个函数最漂亮的细节：先把**定稿后的副本**写入存储，成功后才把活任务的 Meta 整体替换。落盘失败时活任务保持原样、`NeedsPreparation` 仍为 true，下次启动重新准备——目录决策永远不可能"半新半旧"。

`CheckDuplicateAndRename`（[pkg/util/path.go:L42-L74](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/util/path.go#L42-L74)）的算法：`os.Stat` 目标路径，不存在则原样返回名字；存在则从 `a (1).txt`、`a (2).txt`… 依次探测直到找到磁盘上不存在的名字（无扩展名与纯隐藏文件如 `.gitignore` 有特判）。

#### 4.6.3 源码精读

`prepareTask` 全函数（[pkg/download/downloader.go:L2312-L2366](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2312-L2366)）：

```go
func (d *Downloader) prepareTask(task *Task) error {
	if task.Meta.Res == nil { return errors.New("task resource is not resolved") }
	preparedMeta := *task.Meta                    // 浅拷贝 Meta
	preparedOpts := *task.Meta.Opts               // 拷贝 Opts
	preparedMeta.Opts = &preparedOpts
	preparedMeta.Res = util.DeepClone(task.Meta.Res) // Res 深克隆，彻底隔离
	dir, err := d.decideDownloadDirForTask(preparedMeta.Opts, preparedMeta.Res, task.group)
	if err != nil { return err }
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

	// Save a candidate task before publishing its metadata. The fetcher keeps
	// its resolved protocol state, so a failed save cannot change the live
	// directory, name or preparation flag.
	task.statusLock.Lock()
	preparedTask := *task                          // 快照也在 statusLock 内取
	task.statusLock.Unlock()
	preparedTask.Meta = &preparedMeta
	preparedTask.NeedsPreparation = false
	if err := d.saveTask(&preparedTask); err != nil { return err }
	task.statusLock.Lock()
	*task.Meta = preparedMeta                      # 发布：整块替换
	task.NeedsPreparation = false
	task.statusLock.Unlock()
	return nil
}
```

目录决策 `decideDownloadDirForTask`（[pkg/download/category.go:L38-L73](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category.go#L38-L73)）：基础目录取 `opts.Path`，空则取全局 `DownloadDir`，再空直接报错（拒绝回落到进程工作目录）；**分组分支在最前面**——`group != nil && opts.SkipAutoCategorize` 时直接转 `initDownloadPathForTask(base, group)`（做包含性 + 组根白名单，见 [pkg/download/category.go:L55-L57](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category.go#L55-L57)），完全跳过分类；普通路径未跳过自动分类且命中分类则改走分类目录，展开占位符并做白名单校验，分类目录被拒时**回退到基础目录**而不是让任务失败。旧的 `decideDownloadDir` 退化为传 `group=nil` 的兼容包装（[pkg/download/category.go:L34-L36](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category.go#L34-L36)）。分类匹配细节（只路由单文件资源等）留给 u10-l1。

**`checkDuplicateLock` 为什么必须包住 `fetcher.Start()`？** 答案写在 doStart 的注释里（[pkg/download/downloader.go:L2430-L2436](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2430-L2436)）：

> "The duplicate name is resolved from the files on disk, but the file only appears when the fetcher starts, so the lock has to cover the start as well."
> （重名是从磁盘上的文件解析出来的，但文件要到 fetcher 启动时才出现，所以这把锁必须连启动一起罩住。）

展开讲：`CheckDuplicateAndRename` 的判重依据是 `os.Stat`——**磁盘上此刻有没有这个名字的文件**。而目标文件要到 `fetcher.Start()` 执行、协议层调用 `ctl.Touch` 创建文件之后才真正存在。设想两个并发任务下载同一个文件到同一目录：

- 任务 A：Stat 发现 `a.txt` 不存在 → 决定用 `a.txt`；
- 任务 B：Stat 也发现 `a.txt` 不存在（A 还没来得及创建文件）→ 也决定用 `a.txt`；
- 两个任务写同一个文件，数据互相覆盖。

把「查重 + 改名 + `fetcher.Start()`（创建文件）」整体放进全局 `checkDuplicateLock` 临界区，就把"检查—改名—占位"做成了原子操作：A 拿着锁走完全程、文件已落盘之后，B 才开始 Stat，此时必然能看到 `a.txt` 而改用 `a (1).txt`。这就是 4.5 流程图中 `d.checkDuplicateLock.Lock(); defer d.checkDuplicateLock.Unlock()` 用 **defer** 解锁的原因——它要罩住包括 `fetcher.Start()` 在内的 handler 余下全部步骤。仓库测试 `TestDownloader_CreateRename`（[pkg/download/downloader_test.go:L1122-L1166](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L1122-L1166)）正是这个行为的回归：同一 URL 并发直建两个任务，最终校验 `download.data` 与 `download (1).data` 两个文件的 MD5 都与源文件一致。批量场景下 `TestDownloader_CreateDirectBatch`（[pkg/download/downloader_test.go:L915-L987](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L915-L987)）更是连建 5 个同名任务，靠同一机制得到 5 个互不覆盖的文件。

#### 4.6.4 代码实践

**实践目标**：亲手复现重名改名，观察 `checkDuplicateLock + prepareTask + Touch` 的协作效果。

**操作步骤**：

1. 运行第 5 节综合实践的示例程序（它会对同一 URL 连续 `CreateDirect` 两次）；
2. 观察输出中的 `[file]` 列表；
3. 对照源码走一遍：两个任务的 handler 并发进入 → 第一个拿到 `checkDuplicateLock`，`prepareTask` 决定 `trace.data`，`fetcher.Start()` 创建文件，释放锁 → 第二个 Stat 到 `trace.data` 存在 → 改名 `trace (1).data`。

**需要观察的现象**：下载目录里出现 `trace.data` 与 `trace (1).data`，内容完全相同。

**预期结果**：与 `TestDownloader_CreateRename` 的断言一致（该测试用 `download.data` / `download (1).data` 验证过同样逻辑）。程序实际输出**待本地验证**。

#### 4.6.5 小练习与答案

**练习 1**：`prepareTask` 为什么要深克隆 `Res`、拷贝 `Opts`，而不是直接改 `task.Meta`？

**参考答案**：准备过程可能在中途失败（目录决策被白名单拒绝、磁盘 Stat 出错、saveTask 失败）。在预备稿上操作，失败时活任务的 Meta 一字未动、`NeedsPreparation` 仍为 true，下次启动可以安全重试；成功时用 `*task.Meta = preparedMeta` 整块替换发布。若直接改活任务，一次失败就会留下"目录改了一半"的脏状态。这个语义还被 `TestDownloader_ResolvedBatchPreparationFailureConsumesResourceID` 间接覆盖：准备阶段保存失败，任务留在任务列表且可重试，但解析缓存不归还。

**练习 2**：`checkDuplicateLock` 是全局锁，会不会所有下载都被它串行化、拖慢速度？

**参考答案**：不会。只有 `NeedsPreparation && fetcherManager.AutoRename()` 的任务才拿这把锁，而准备只在**首次启动**发生一次；此后每次续传、重试都不再进入该分支（走白名单复检）。已准备任务的启动完全不碰这把锁。

**练习 3**：BT 任务（目录型资源）与 HTTP 任务（单文件）在 `prepareTask` 里的改名路径有何不同？

**参考答案**：目录型资源 `Res.Name != ""`，对 `FolderPath()` 整目录路径查重改名；单文件资源对 `SingleFilepath()` 的文件完整路径查重改名。两条路径最终都把新名字写进 `preparedMeta.Opts.Name`——因为 `FetcherMeta` 的路径计算里 `Opts.Name` 优先级最高（u3-l1），这样改名对协议层透明。

**练习 4**：分组任务为什么不需要 `CheckDuplicateAndRename` 防止组目录撞名？

**参考答案**：组目录的查重发生在更早的 `createTaskGroup`（[pkg/download/downloader.go:L783-L814](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L783-L814)）：它在 `d.lock` 内逐个尝试 `name`、`name (1)`…（上限 1000 个后缀），既对照内存中已有组的 `Path`，又用 `os.Mkdir` 的原子性对抗磁盘上已存在的目录，成功后立即登记 `d.groups` 与 `creatingGroups`。组内成员文件的改名仍走本节的通用逻辑。

## 5. 综合实践

设计一个贯穿本讲的跟踪实验：**一个程序，四个观察点**——完整调用链、wait 队列调度、重名改名、批量与分组创建。

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
		fmt.Printf("[event] %-8s task=%.6s parent=%s status=%-7s err=%v\n",
			e.Key, e.Task.ID, e.Task.ParentID, e.Task.Status, e.Err)
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

	// 观察点④：批量 + 分组创建（rid 与 req 混合、部分成功语义）
	rr, err := d.Resolve(&base.Request{URL: url}) // 先解析拿 rid
	if err != nil {
		panic(err)
	}
	batch, err := d.CreateDirectBatchWithResult(&base.CreateTaskBatch{
		Group:     true,
		GroupName: "my-batch",
		Opts:      &base.Options{Path: dlDir}, // 批级 Path = 组的父目录
		Reqs: []*base.CreateTaskBatchItem{
			{Rid: rr.ID},                                            // rid 优先：消费解析缓存
			{Req: &base.Request{URL: url}},                          // 直建
			{Opts: &base.Options{Path: "sub"}},                      // 无 rid/req → 该成员失败，其余照常
		},
	})
	fmt.Printf("[batch] result=%+v err=%v\n", batch, err) // 观察 taskIds 与 parentId
	for _, e := range d.GetTasks() {
		if e.ParentID != "" {
			fmt.Printf("[group-member] %.6s parent=%.6s dir=%s\n", e.ID, e.ParentID, e.Meta.Opts.Path)
		}
	}
}
```

**要完成的任务清单**：

1. **调用链清单（观察点①）**：结合 Delve 断点（见 4.5.4）或对照源码，填完 4.5.4 的字段修改清单表，重点确认：CreateDirect 路径的 `resolvedHere` 为真（断点 2420 行处 `task.Meta.Res == nil`）。
2. **wait 队列（观察点②）**：`MaxRunning=1` 时第二个任务建出即为 wait，`[poll]` 轮询能看到 `wait → running` 的切换；第一个任务 done 后约一个刷新周期内第二个被 `notifyRunning` 唤醒。
3. **重名改名（观察点③）**：`[file]` 列表应出现 `trace.data` 与 `trace (1).data`，由此验证 `checkDuplicateLock` 把「查重—改名—Start 建文件」串成了原子段；写出"若没有这把锁，两个并发任务会发生什么"的分析（答案见 4.6.3）。
4. **批量与分组（观察点④）**：`[batch]` 输出应含 2 个 `taskIds` 与 1 个 `parentId`（第三个成员失败但批次部分成功）；分组成员的 `Meta.Opts.Path` 落在 `<dlDir>/my-batch/` 下（若目录重名则带 `(1)` 后缀）；对照 [_docs/task-batch-api.md](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/_docs/task-batch-api.md) 逐条核对 rid 优先、opts 整体替换与部分成功语义。

**预期结果**（程序输出**待本地验证**）：事件序列大致为 `start → progress… → done` 依次出现两组（trace.data 两个任务）加一组（other.data），轮询输出可见第二个/第三个任务经历 wait 态；批量阶段的分组任务再次经历完整主链路（其中 rid 成员不触发延迟解析）。若把 `MaxRunning` 恢复默认 5 再跑一次，三个任务应同时 running，且更能体现并发场景下 `checkDuplicateLock` 的作用。

**思考题（选做）**：把示例中第三个任务删除（`d.Delete`），观察 `notifyRunning`（[pkg/download/downloader.go:L1251](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1251) 调用点）能否让队列里的任务提前上场。再试着删除分组里的全部成员任务，观察组目录与 `task_group` 元数据如何被清理（深入见 u4-l6）。

## 6. 本讲小结

- **多入口、一条装配线**：`CreateDirect`（直建，Resource 延迟到 `doStart` 内解析）、`Resolve → Create/CreateWithOptions`（消费解析缓存）与 `CreateDirectBatchWithResult`（rid/req 混合批量，rid 优先、成员 opts 整体替换、部分成功）三者在 `doCreate` 汇合。
- **createResolved 的缓存契约**：写锁下「取出即删除」保证同一解析结果只有**一个创建者**；`taskId == ""`（未落盘）时归还 Fetcher 供重试，落盘成功即被任务**永久消费**——判定依据正是 `doCreate` 把 `storage.Put` 与 `taskId` 赋值都放进了 `d.lock` 临界区。
- **doCreate 装配骨架**：`NewTask` 出 ID 与 ready 态，依次挂上 `ParentID`（分组归属）、fetcherManager/fetcher/Protocol/Meta/Progress/Uploading 与 `NeedsPreparation=true`，`initTask` 重建运行时内核；任务先落盘 `bucketTask` 再进 `tasks`（同处一个临界区，含组存在性校验），满载时直写 wait 态入 `waitTasks` 队列。
- **调度唤醒**：`notifyRunning` 异步抢主锁，`remainRunningCount` 有余量就 FIFO 弹出队首调 `doStart`；Pause/Delete/watch 终态/handleOnError 四处触发。
- **doStart 两段式**：同步段（`statusMut`/statusLock）拦截、`restoreFetcher`（两段式 Restore 契约）、置 running 并 `runGeneration++`；异步 handler（task.lock）做扩展钩子、Meta 回填、延迟解析、准备目录、`fetcher.Start()`；「核对代数—清零速度—启动计时器」合并为单个 statusLock 临界区，消除"核对之后、计时器启动之前"被暂停插入的窗口。
- **prepareTask 一次性准备**：在深克隆的预备稿上决策目录（普通任务走分类路由 + 白名单 + 回退；分组任务旁路分类、直接落预留组目录并做包含性检查）并解决重名，「先存候选、后发布」保证失败不产生半新半旧状态；`NeedsPreparation` 清除后永不再分类改名，但每次启动仍复检白名单。
- **checkDuplicateLock 的临界区**：查重的依据是磁盘 Stat，而文件要到 `fetcher.Start()` 才出现，所以锁必须用 defer 罩住整个「查重—改名—启动建文件」过程，把跨任务的占位变成原子操作。

## 7. 下一步学习建议

任务启动之后，主角交给 **u4-l3《watch 循环、事件系统与进度刷新》**：`go d.watch(task)` 启动的轮询如何读 `fetcher.Wait()`/`Progress()`、用滑动窗口算速度、按策略落检查点，并把 start/progress/done/error 翻译成 `emit` 事件。随后 **u4-l4** 讲 Pause/Continue/Delete 与 `doPauseForScheduling`、`runGeneration` 的完整竞态防护——本讲已经铺垫了代数机制的前半部分，也预告了 Pause 对 waitTasks 的同步清理与 Delete 的删除失败恢复。本讲反复出现的 `groupID`、`createTaskGroup` 与 `task_group` 桶的完整生命周期（目录预留、组 ID 展开、空组清理）由新增的 **u4-l6《任务分组与批量创建》** 专门展开。若想先横向扩展，可以跳去 **u5-l1** 看 HTTP 协议如何实现本讲的 `fetcher.Resolve` 与 `fetcher.Start`（包括 `asyncPrefetch` 如何让"消费解析缓存"免二次请求）。
