# u4-l3 watch 循环、事件系统与进度刷新

## 1. 本讲目标

上一讲（u4-l2）我们走通了「Resolve → Create → doStart」主链路，任务的最后一步是 `fetcher.Start()` 被调用、传输开始。但故事到这里只讲了一半：

- 谁来发现「下载完成了」或「下载失败了」？
- 进度条上的百分比和速度是从哪里冒出来的？
- 为什么任务列表刷新得很平滑，而磁盘写入并没有疯狂增长？

学完本讲，你应该能够：

1. 读懂 `watch` 函数的完整循环逻辑，说清它如何把 Fetcher 的完成信号翻译成状态迁移、事件与持久化动作。
2. 理解七种 `EventKey` 各自的触发位置，以及 `finally` 这个「合成事件」的设计意图。
3. 掌握 `shouldSaveTaskCheckpoint` 的落盘策略：什么情况下写检查点、什么情况下跳过、做种任务为什么每 30 秒必须写一次。
4. 会用 `Listener` 写一个自己的终端进度打印器，并能定量分析 `RefreshInterval` 对事件频率与存储写入次数的影响。

## 2. 前置知识

本讲建立在前面几讲的认知之上，先快速对齐词汇：

- **Task 状态机**（u2-l2）：任务有 ready/wait/running/pause/error/done 六种状态；**状态的唯一写入者是 Downloader**，Fetcher 只能通过 `Wait()` 的返回值「汇报」结果，`watch` 循环负责把汇报翻译成 `done`/`error` 迁移。
- **Fetcher.Wait 与 DoneCh**（u3-l1）：`DefaultFetcher` 内部有一个容量为 1 的 `DoneCh chan error`，`Wait()` 就是 `<-f.DoneCh`，阻塞直到协议实现发来「完成（nil）」或「失败（err）」。见 [internal/fetcher/fetcher.go:L160-L174](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L160-L174)。
- **doStart 的两段式结构**（u4-l2）：同步段改状态，异步 `handler()` 里调 `fetcher.Start()`，全程用 `runGeneration`「代数」核对防止旧协程覆盖新状态。
- **runGeneration（代数）**：任务每次进入 running 时代数 +1（重启也 +1）。旧协程发现自己记录的代数与任务当前代数不一致，就知道「我已经不是本轮运行的负责人了」，主动放弃写入。本讲会看到它在 `watch` 与 `markTaskDone` 里的身影。
- **轮询（pull）vs 推送（push）**：Gopeed 的进度是「拉」模型——Fetcher 不主动上报进度，Downloader 的一个后台协程每 `RefreshInterval` 毫秒去 `fetcher.Progress()` 读一次。这与「推」模型（Fetcher 主动回调）相对，好处是协议实现极简、节奏全局统一。
- **检查点（checkpoint）**：把任务当前状态（进度 + 协议续传数据）写入持久化存储，进程崩溃后重启可以从中恢复。Gopeed 的检查点分布在三个存储桶里：`task`（任务快照）、`save`（协议续传数据）、`task_stats`（统计快照）。

两个容易被忽略的小工具也先认识一下：

- [pkg/util/timer.go:L5-L26](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/util/timer.go#L5-L26) 的 `Timer`：`Used()` 返回「有效用时」——`Pause()` 会把已流逝时间累计进 `used`，`Start()` 重新起算。也就是说**暂停期间不计时**，任务的 `Progress.Used` 是净下载时间。
- [pkg/download/storage.go:L13-L23](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go#L13-L23) 的 `Storage` 接口：本讲的「存储写入次数」指的就是对它 `Put` 的调用次数。内存版 `MemStorage` 是 map 操作，bbolt 版每次 `Put` 是一个完整写事务（[pkg/download/storage.go:L166-L175](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go#L166-L175)），写放大差异很大。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [pkg/download/downloader.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go) | 下载引擎中枢 | `watch` 函数、`emit`/`Listener`、Setup 里的刷新协程、`shouldSaveTaskCheckpoint`、`saveTask`、`markTaskDone` |
| [pkg/download/event.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/event.go) | 事件定义 | 七种 `EventKey` 与 `Event` 结构 |
| [pkg/download/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go) | Task/Progress 模型 | `calcSpeed` 滑动窗口、`Progress` 字段、`RefreshInterval` 默认值 |
| [pkg/util/timer.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/util/timer.go) | 净用时计时器 | 暂停不计时 |
| [cmd/gopeed/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go) | 官方 CLI | Listener 的官方用法与进度条渲染 |
| [_examples/basic/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/_examples/basic/main.go) | 最小示例 | 只监听 `finally` 的极简写法 |

## 4. 核心概念与源码讲解

本讲四个最小模块的关系可以用一句话串起来：

> **刷新协程**（全局一个）按 `RefreshInterval` 拉进度 → 更新内存里的 `Progress`、算速度 → 满足条件时写**检查点** → 对外 `emit(progress)`；**watch 协程**（每个运行中任务一个）阻塞在 `fetcher.Wait()` → 把结果翻译成 `done`/`error` → `emit(done/error)` + `emit(finally)`。

### 4.1 watch 轮询循环：任务的守夜人

#### 4.1.1 概念说明

`fetcher.Start()` 只是「点火」——它启动传输后立刻返回，下载在协议自己的 goroutine 里跑。Downloader 必须再派一个「守夜人」goroutine 守在这个任务旁边，阻塞等待 Fetcher 的最终结果，然后做一整套收尾：状态迁移、最终快照落盘、发事件、唤醒等待队列、触发扩展钩子和 webhook。

这个守夜人就是 `watch`。它的关键设计：

- **一个任务同一时刻最多一个守夜人**：用 `watchedTasks sync.Map`（任务 ID → 占位）去重，防止快速「暂停→继续」时新旧两个 watch 并存、争抢同一段状态。
- **守夜人可能守「多轮」运行**：主循环是 `for { err := fetcher.Wait(); ... }`，如果一次错误返回后任务又被重启了（状态仍是 running），守夜人 `continue`，接着守候新的 fetcher，而不是错误地退出或重复报错。
- **守夜人不只守下载**：对「下载已完成、正在做种」的任务，它另起一个 goroutine 守 `WaitUpload()`。

#### 4.1.2 核心流程

`watch` 的完整执行流程（伪代码）：

```text
doStart 的异步 handler 成功返回
  ├─ 代数核对通过 → emit(start)
  └─ go watch(task)
       ├─ watchedTasks.LoadOrStore(task.ID) 已存在？→ 直接返回（已有守夜人）
       ├─ defer watchedTasks.Delete(task.ID)        ← 让下一次启动能再注册
       ├─ 任务在做种(Uploading)？
       │    └─ 另起协程：WaitUpload() → 任务未被删除则 Uploading=false 并落盘
       ├─ 任务已是 done？→ 返回（只做种，不再进入主循环）
       └─ for {
            err := fetcher.Wait()          // 阻塞直到完成/失败/被打断
            ├─ err != nil：
            │    ├─ 当前状态是 pause/wait？→ 静默返回（用户暂停打断了等待，不是失败）
            │    ├─ Blob 生产者源出错？    → doOnBlobSourceError（会重建 fetcher）
            │    ├─ 其他错误               → doOnError（标记 error + 发 error/finally 事件）
            │    └─ 处理完状态仍是 running？→ continue（任务已被重启，接着守新 fetcher）
            │       否则 → return
            ├─ 成功分支：
            │    ├─ Res 为空 或 任务已被删除？→ 返回（未解析任务被删的边界）
            │    ├─ Blob 源错误检查（同上）
            │    ├─ markTaskDone()          // 一次性写入 done 终态 + 全程平均速度
            │    ├─ captureTaskStats(false, true)  // 强制落一份稳定统计快照
            │    ├─ task.clone() → Put(bucketTask) // 最终任务快照
            │    ├─ emit(done) + emit(finally, nil)
            │    ├─ notifyRunning()         // 唤醒 wait 队列里的下一个任务
            │    ├─ releaseBlobTask / triggerOnDone / webhook / script
            │    └─ 自动种子 / 自动解压（postDownloadExtra）
            │    → return
          }
```

注意 `finally` 事件的 `Err` 参数：成功分支传的 `err` 此时是 `nil`，所以**监听方用 `event.Err == nil` 判定成功**。

#### 4.1.3 源码精读

**启动点：doStart 的收尾**。异步 handler 成功且代数核对通过后，先发 `start` 事件，再启动守夜人：

[pkg/download/downloader.go:L2104-L2117](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2104-L2117) —— handler 出错且仍是本代运行则走 `doOnError`；成功且 `started` 为真，核对代数后 `emit(EventKeyStart)`，随后 `go d.watch(task)`。上一讲的 `fetcher.Start()` 在 [L2090](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2090)，`started = true` 在 [L2093](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2093)。

**去重与上传分支**：

[pkg/download/downloader.go:L1516-L1545](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1516-L1545) —— `LoadOrStore` 返回 `loaded=true` 说明同 ID 的守夜人已在岗，直接返回；`defer Delete` 在守夜人退岗时销账，给下一次启动让位。做种分支里 `WaitUpload()` 返回后先 `GetTask` 确认任务没被删，才把 `Uploading` 置 false 并写 `bucketTask`。最后 `done` 短路：只做种的任务不进入主循环。

**主循环：错误分支**：

[pkg/download/downloader.go:L1547-L1567](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1547-L1567) —— `Wait()` 返回错误时，先看任务当前状态：若是 `pause`/`wait`，说明是暂停流程打断了等待，静默退出（这不是下载失败）；否则区分 Blob 生产者源错误与普通错误分别处理；处理完若状态仍是 `running`，意味着错误返回的同时任务已被重启（新 fetcher 已经挂到 `task.fetcher` 上），`continue` 回到循环顶继续守候，而不是把新的一轮运行误标为失败。

**主循环：成功分支**：

[pkg/download/downloader.go:L1569-L1599](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1569-L1599) —— 依次是：未解析/已删除边界检查 → Blob 源错误复查 → `markTaskDone` → 强制统计快照 → 最终任务快照落盘 → `emit(done)` 与 `emit(finally)` → `notifyRunning()` 唤醒等待队列 → 释放 Blob 引用 → 触发扩展 `onDone` 钩子 → webhook 与脚本。这一段是「下载完成」在核心层的全部定义。

**错误处理的另一半：handleOnError**：

[pkg/download/downloader.go:L1696-L1747](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1696-L1747) —— `doOnError`/`doOnBlobSourceError` 都收敛到 `handleOnError`。它先拿 `task.lock`，用 `markTaskError` 做「状态是 running 才写入 error」的守卫（[L1700-L1704](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1700-L1704)，非 running 直接放弃——这就是上面 `continue` 分支不发 error 事件的原因）；随后触发扩展 `onError` 钩子、若扩展改写了 URL 则通过 `fetcher.Patch` 复位重定向状态；最后在锁外（[L1739-L1746](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1739-L1746)）确认任务确为 error 后 `emit(EventKeyError)` + `emit(EventKeyFinally)` + `notifyRunning()` + webhook。

**终态写入：markTaskDone**：

[pkg/download/downloader.go:L2236-L2264](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2236-L2264) —— 双锁（`task.lock` + `statusLock`）保护下，只有 `running` 状态才允许写入 `done`（`done` 是唯一终态，一次写入永不回退，对应 u2-l2 讲过的 `DoneAt` 一次性语义）。注意三处收尾数学：`Progress.Used` 取最终净用时；若资源大小为 0（如 openEnded 资源）用实际下载字节数回填 `Res.Size`；速度改写为**全程平均** `totalSize / used`（`used` 至少按 1 秒计）。最后 `runGeneration++`，让任何仍在飞的旧协程立刻失势。

**完成之后的连锁反应**（本讲只点名，细节留给 u10）：

[pkg/download/downloader.go:L1601-L1656](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1601-L1656) —— `postDownloadExtra` 取出任务选项里的 `AutoTorrent`/`AutoExtract`：下载的是 `.torrent` 文件则链式创建 BT 任务（必要时删除种子文件），是归档文件则进入全局解压队列。

#### 4.1.4 代码实践

**实践目标**：不看输出猜事件——先在纸上写出事件序列，再用真实下载验证 `watch` 的执行路径。

**操作步骤**（源码阅读 + 运行观察型）：

1. 基于 4.2 节的监听器代码（或直接用 `_examples/basic`）跑一次正常下载，记下事件序列。预期：`start → progress×N → done → finally`。
2. 下载中途 Ctrl-C 杀掉进程，对照 [L1549-L1553](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1549-L1553) 思考：进程都没了，为什么不会有 `pause`/`error` 事件？（答案见 u4-l5：重启后由 `Setup` 把未完成任务统一压成 `pause`，事件是进程内的概念。）
3. 下载一个返回 404 的 URL，观察事件序列。预期：`start → error → finally`，且 `finally` 的 `event.Err` 非 nil。
4. 在 `watch` 的 `continue` 分支（[L1563-L1565](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1563-L1565)）前加一行日志（本地实验用，勿提交），构造「下载失败的瞬间点继续」的场景，验证它确实极少触发但确实存在。

**需要观察的现象**：正常路径 `done` 与 `finally` 总是成对且相邻出现；失败路径 `error` 与 `finally` 成对；用户主动暂停只会出现 `pause`，不会出现 `error`/`finally`。

**预期结果**：你能把每一条事件对应到 4.1.2 流程图里的一个框。

*（本实践依赖本地运行环境，具体输出待本地验证。）*

#### 4.1.5 小练习与答案

**练习 1**：为什么 `watch` 入口要用 `watchedTasks.LoadOrStore` 去重？去掉会怎样？

**答案**：快速「暂停→继续」时，旧 watch 可能还阻塞在 `Wait()` 上，新一次 `doStart` 又 `go d.watch(task)`。不去重就会出现两个守夜人同时消费同一个 `DoneCh`、并发写同一任务的状态与存储。`LoadOrStore` 保证同 ID 同时只有一个守夜人；`defer Delete` 在退岗时销账，下一次启动可以重新注册。

**练习 2**：`Wait()` 返回了错误，为什么 `watch` 可能既不发 `error` 事件也不退出？

**答案**：两种情况。其一，任务当前状态是 `pause`/`wait`——暂停流程打断了等待，这个错误只是「被叫停」的信号，不是下载失败，静默退出。其二，错误处理完状态仍是 `running`——错误返回的同时任务已被重启（`task.fetcher` 已换成新实例），`markTaskError` 因状态守卫拒绝写入，`watch` 用 `continue` 接着守候新 fetcher。

**练习 3**：`emit(EventKeyDone)` 之后为什么还要 `emit(EventKeyFinally, task, err)`？`err` 此时是什么值？

**答案**：`finally` 是合成事件，紧跟 `done` 或 `error` 出现，让监听方（如 CLI 的 `wg.Done()`）只需订阅一个键就能等到「本轮运行已终结」，不必同时匹配两个键再判重。成功分支的 `err` 是 `nil`，所以 `event.Err == nil` 即成功。

### 4.2 emit/Listener 分发：七种事件与一个回调

#### 4.2.1 概念说明

Gopeed 核心层的事件系统刻意做得极简：**一个回调 + 七个键**。

[pkg/download/event.go:L3-L19](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/event.go#L3-L19) 定义了全部词汇：

```go
type EventKey string

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

三条使用约定：

1. **单订阅者**：`Downloader` 只有一个 `listener` 字段，后设置的覆盖先设置的。多端订阅的需求不在核心层解决——REST/FFI 的事件分发由 `pkg/api` 的 `SubscribeTaskEvents` 位掩码订阅承担（u8-l4 专讲）。
2. **同步调用**：`emit` 就是直接调函数，没有队列、没有 goroutine。监听器执行多久，调用方（刷新协程、watch 协程）就等多久。
3. **必须在锁外调用**：因为监听器被明确允许回调 `Pause`/`Continue`（它们会拿 `d.lock`/`statusLock`），emit 时绝不能持有这些锁。

#### 4.2.2 核心流程

七种事件的触发位置全景表（行号均指向当前 HEAD）：

| EventKey | 触发位置 | 语义 |
| --- | --- | --- |
| `start` | `doStart` 异步 handler 成功后（[L2113](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2113)） | 传输真正开始（不是任务创建） |
| `progress` | 刷新协程每 tick（[L367](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L367)）；`Patch` 成功后（[L755](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L755)）；自动解压各阶段（[L2332-L2683](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2332-L2683) 多处） | 进度或运行时状态有更新 |
| `pause` | `doPause`（[L2133](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2133)）；`Continue` 里被调度压回 wait 的任务补发（[L854-L858](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L854-L858)） | 任务暂停（含调度性暂停） |
| `error` | `handleOnError` 锁外确认 error 后（[L1742](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1742)） | 任务失败 |
| `delete` | `doDelete`（[L1213](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1213)） | 任务被删除 |
| `done` | `watch` 成功分支（[L1593](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1593)） | 任务完成 |
| `finally` | 紧跟 `done`（[L1594](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1594)）或 `error`（[L1743](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1743)） | 本轮运行终结的合成事件，恰好一次，`Err==nil` 即成功 |

一个精细的差别值得指出：`doPauseForScheduling`（[L2139-L2157](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2139-L2157)）在持有 `d.lock` 时把超载任务压回 wait，**故意不发事件**；`Continue` 在释放锁后统一补发 `pause`（[L854-L858](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L854-L858) 的注释写明了原因）。这是「emit 必须在锁外」约定直接塑造出来的代码形态。

#### 4.2.3 源码精读

**emit 与 Listener 注册**：

[pkg/download/downloader.go:L1294-L1310](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1294-L1310) —— `Listener(fn)` 把回调挂到 `d.listener` 字段；`emit` 是薄包装：listener 非空就同步构造 `Event` 并调用。`errs` 是可变参数，取第一个作为 `Event.Err`。

**「锁外 emit」的两处自注释**：

[pkg/download/downloader.go:L365-L367](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L365-L367) —— 刷新协程先 `task.lock.Unlock()` 再 emit，注释写明「Listener 回调可能调用 Pause/Continue 并获取 statusLock」。[L854-L855](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L854-L855) 是同一约定在 `Continue` 里的复现。

**官方用法一：极简版**。

[_examples/basic/main.go:L12-L28](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/_examples/basic/main.go#L12-L28) —— 只关心 `finally`，用一个 channel 把 `event.Err` 传回主协程：

```go
finallyCh := make(chan error)
_, err := download.Boot().
	URL("https://www.baidu.com/index.html").
	Listener(func(event *download.Event) {
		if event.Key == download.EventKeyFinally {
			finallyCh <- event.Err
		}
	}).
	Create(&base.Options{Extra: http.OptsExtra{Connections: 8}})
```

**官方用法二：进度条版**。

[cmd/gopeed/main.go:L34-L68](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go#L34-L68) —— `progress` 事件驱动 `printProgress` 刷进度条，`finally` 负责收尾与 `wg.Done()`。注意它处理了一个边界：任务未解析时 `Meta.Res` 为 nil，此时用一个空的 `emptyTask` 兜底渲染（[L71-L80](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go#L71-L80)）；进度条渲染本体在 [L82-L113](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go#L82-L113)，用 `\r` 回车符原地覆盖、按上一行长度补空格防残影。

**Boot 链如何接线**：

[pkg/download/downloader.go:L2786-L2805](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2786-L2805) —— `boot.Listener` 只是把回调暂存到 `boot` 结构体，`Create` 时才通过 `defaultDownloader.Listener(b.listener)` 转交给全局单例。而 `defaultDownloader` 由 `NewDownloader(nil)` 创建（[L2768](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2768)）——这意味着 **Boot 单例的 RefreshInterval 永远是默认 350ms、存储永远是 MemStorage**，本讲综合实践里要改刷新间隔，就必须绕开 Boot 自己 `NewDownloader`。

**测试里的监听器范式**：

[pkg/download/downloader_test.go:L2317-L2337](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L2317-L2337) —— 自动解压测试用监听器收集 `ExtractStatus` 的变化序列，并用 `sync.Once` + `close(channel)` 的惯用法通知主测试协程。写自己的监听器时值得照抄这个骨架（互斥锁护共享切片 + channel 收尾信号）。

#### 4.2.4 代码实践

**实践目标**：写一个「终端进度打印器」progress-reporter，功能对标 `cmd/gopeed`，但把每个事件的 key 都打出来，并体会它与官方实现的取舍差异。

**操作步骤**（示例代码，完整可运行版本见第 5 节综合实践）：

1. 新建独立目录（不要放进仓库），写 `main.go`：

```go
// 示例代码：最小事件观察器
package main

import (
	"fmt"

	"github.com/GopeedLab/gopeed/pkg/base"
	"github.com/GopeedLab/gopeed/pkg/download"
)

func main() {
	finally := make(chan error)
	d := download.NewDownloader(nil) // 生产中记得 defer d.Close()
	if err := d.Setup(); err != nil {
		panic(err)
	}
	d.Listener(func(event *download.Event) {
		switch event.Key {
		case download.EventKeyStart:
			fmt.Println("[start ]", event.Task.ID)
		case download.EventKeyProgress:
			p := event.Task.Progress
			fmt.Printf("[prog  ] %d / %d  %d B/s\n", p.Downloaded, event.Task.Meta.Res.Size, p.Speed)
		case download.EventKeyDone:
			fmt.Println("[done  ]")
		case download.EventKeyError:
			fmt.Println("[error ]", event.Err)
		case download.EventKeyFinally:
			fmt.Println("[finally]", event.Err)
			finally <- event.Err
		}
	})
	if _, err := d.CreateDirect(&base.Request{URL: "https://example.com/some-file.zip"}, &base.Options{}); err != nil {
		panic(err)
	}
	if err := <-finally; err != nil {
		panic(err)
	}
}
```

2. 换成一个真实可下载的 URL 运行，统计每种事件出现的次数。
3. 与 [cmd/gopeed/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go) 逐点对比。

**需要观察的现象**：`progress` 次数远多于其他事件；`done` 与 `finally` 恰好各一次且相邻；URL 无效时只有 `error` + `finally`。

**预期结果**：得到一个能与官方 CLI 对比的清单——

| 维度 | cmd/gopeed | 本实践 |
| --- | --- | --- |
| 入口 | `Boot()` 链式（默认单例，350ms / MemStorage 固定） | `NewDownloader`（可注入 RefreshInterval、Storage） |
| 监听键 | `progress` + `finally` | 全部七键 |
| 渲染 | `\r` 原地覆盖进度条、防残影、空任务兜底 | 逐行打印（事件流可审计） |
| 速度展示 | `util.ByteFmt` 人类可读 | 原始字节 |

*（运行结果依赖网络与本地环境，待本地验证。）*

#### 4.2.5 小练习与答案

**练习 1**：在 Listener 回调里调用 `downloader.Pause(...)` 安全吗？如果 emit 时持有 `d.lock` 会发生什么？

**答案**：安全，这是被明确支持的场景（源码注释：listeners are allowed to call back into Downloader methods）。但如果 emit 时持有 `d.lock`，而 `Pause` 也要拿 `d.lock`，就会死锁。这就是为什么刷新协程、`Continue`、`handleOnError` 都先放锁再 emit。

**练习 2**：既然只有一个 `listener` 字段，REST 服务同时服务多个浏览器标签页时事件怎么分发给多个客户端？

**答案**：核心层不分发。`pkg/download` 的 Listener 是库级回调，REST 层通过 `pkg/api` 的 `SubscribeTaskEvents`（位掩码订阅）把核心事件转发给多个连接，那是一条独立的管道，u8-l4 专门讲解。

**练习 3**：为什么 `doPauseForScheduling` 不自己发 `pause` 事件，而要留给 `Continue` 补发？

**答案**：调度性暂停发生在 `Continue` 持有 `d.lock` 的临界区内（压回 wait 队列必须原子），而 emit 必须在锁外（监听器可能回调拿 `d.lock` 的方法）。所以先收集 `pausedTasks`，释放锁后统一补发——约束（锁外 emit）直接决定了代码结构。

### 4.3 进度与速度计算：刷新协程与滑动窗口

#### 4.3.1 概念说明

进度刷新是一个**全局唯一**的后台协程，在 `Setup()` 末尾与「上传恢复」「协议定时任务」一起启动。它按 `RefreshInterval`（默认 350ms，[pkg/download/model.go:L243-L245](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L243-L245)）轮询**所有** running/uploading 任务，职责链是：

读 `fetcher.Progress()` → 算增量与速度 → 更新内存 `Progress` → 刷新统计缓存 → 发 `progress` 事件 → 视条件写检查点。

速度不是「本 tick 增量 ÷ tick」的瞬时值，而是**约 5 秒滑动窗口的平均**，以平滑网络抖动。窗口按「样本数上限 = 5 秒 / tick 秒数」截断：350ms 时约 14 个样本，1s 时恰好 5 个。

#### 4.3.2 核心流程

刷新协程单次 tick 对单个任务的伪代码：

```text
tick = RefreshInterval / 1000                    // 秒
for 每个 task in tasks 快照:
    GetTask(task.ID) == nil?        → 跳过（已删除；且不许持 statusLock 再去拿 d.lock）
    task.lock.TryLock 失败?          → 跳过（正在被启动/解析替换元数据，读半初始化状态无意义）
    状态不是 running 且不在做种?      → 跳过
    current = fetcher.Progress().TotalDownloaded()
    delta    = current - Progress.Downloaded
    if Blob 生产者任务: current/delta 改用 producer.Progress() 的产出/接收增量
    if running:
        Progress.Used  = timer.Used()             // 净用时（暂停不计时）
        Progress.Speed = updateSpeed(delta, tick) // 滑动窗口平均
        Progress.Downloaded = current
    if 做种:
        同理刷新 UploadSpeed / Uploaded
    captureTaskStats(live=true)                   // 内存刷新统计，Snapshot 变化才落盘
    放锁 → emit(progress) → 视条件 saveTask       // 检查点，见 4.4
sleep(RefreshInterval)
```

速度计算的数学：设窗口内样本为 \(\delta_1,\dots,\delta_n\)（每样本代表一个 tick 的增量），则

\[
\bar{v} \;=\; \frac{\sum_{i=1}^{n}\delta_i}{n \cdot \Delta t}, \qquad n \le \left\lfloor \frac{5}{\Delta t} \right\rfloor
\]

其中 \(\Delta t\) 为 tick 秒数。完成时速度被改写为全程平均：

\[
v_{\text{final}} \;=\; \frac{\text{totalSize}}{\max(\text{Used}/10^9,\,1)}
\]

#### 4.3.3 源码精读

**刷新协程主体**（在 `Setup()` 内启动）：

[pkg/download/downloader.go:L292-L390](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L292-L390) —— `for !d.closed.Load()` 主循环：先在 `d.lock` 下拷贝任务列表快照（[L297-L299](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L297-L299)），再逐任务处理，最后 `time.Sleep(RefreshInterval)`（[L388](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L388)）。

**三道跳过闸门**：

[pkg/download/downloader.go:L302-L318](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L302-L318) —— ① `GetTask` 判存活，注释点明锁序约束（不许持 statusLock 时经 GetTask 拿 d.lock，调度路径锁序相反）；② `task.lock.TryLock` 非阻塞抢锁，失败就跳过本 tick——注释说明启动阶段可能正在替换 Blob 资源元数据，宁可少刷新一次也不读半初始化状态，同时保证刷新协程永不被单个任务卡死；③ 状态过滤，`running` 或 `Uploading` 二者其一。

**进度读取与速度写入**：

[pkg/download/downloader.go:L325-L348](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L325-L348) —— `fetcher.Progress().TotalDownloaded()` 汇总各文件已下载字节；`delta = current - Progress.Downloaded`；Blob 生产者任务在 [L327-L339](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L327-L339) 被替换为「扩展在内存里产出了多少 / 本 tick 接收了多少」，且切换生产者时清空速度窗口（`task.speedArr = nil`）。`tick := float64(d.cfg.RefreshInterval) / 1000`（[L340](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L340)），随后三连写：`Used`（净用时）、`Speed`（窗口平均）、`Downloaded`（累计值）。上传侧对称地在 [L350-L357](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L350-L357) 刷新 `UploadSpeed`/`Uploaded`。

**滑动窗口算法**：

[pkg/download/model.go:L178-L199](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L178-L199) —— `calcSpeed` 是速度计算的全部：负增量直接清空窗口返回 0（进度被重置的信号）；追加样本后若 `len > int(5.0/usedTime)` 就丢弃最旧样本；返回 `总和 / 样本数 / tick`。调用侧 `Task.updateSpeed`/`updateUploadSpeed`（[L170-L176](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L170-L176)）分别喂给 `speedArr`/`uploadSpeedArr` 两个窗口。

**净用时计时器**：

[pkg/util/timer.go:L16-L26](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/util/timer.go#L16-L26) —— `Start()` 记录起点，`Pause()` 把这段流逝时间累计进 `used`，`Used()` 返回「当前段 + 历史累计」。暂停期间任务不会被刷新协程刷 `Used`（状态闸门挡掉了），恢复时 `doStart` 重新 `timer.Start()`。跨重启时 `initTask` 用落盘的 `Progress.Used` 重建计时器（[pkg/download/downloader.go:L2689-L2691](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2689-L2691)），净用时得以延续。

**完成时的速度改写**：见 4.1.3 引用的 [markTaskDone，L2247-L2257](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2247-L2257)——窗口平均被全程平均取代，`Downloaded` 直接对齐 `totalSize`。

#### 4.3.4 代码实践

**实践目标**：把 `RefreshInterval` 从 350ms 调到 1000ms，测量 `progress` 事件频率的变化，并解释速度窗口的样本数变化。

**操作步骤**：

1. 关键前提：`Boot()` 用的是 `NewDownloader(nil)` 单例，**改不了**刷新间隔。必须自己构造：

```go
// 示例代码：可调刷新间隔的 Downloader
d := download.NewDownloader(&download.DownloaderConfig{
	RefreshInterval: 1000, // 想要 350 就写 350；写 0 会被 Init 补成 350
	StorageDir:      tempDir,
})
```

依据：[pkg/download/model.go:L243-L245](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L243-L245) 只有零值才补默认；`RefreshInterval` 字段注释见 [L215](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L215)。

2. 在 Listener 里用计数器统计 `progress` 次数，下载同一慢速 URL，分别以 350 与 1000 运行。
3. 同时打印每次事件的 `Speed`，观察 1000ms 档的速度曲线是否更「阶梯」。

**需要观察的现象**：约 T 秒的下载，350ms 档 `progress` 次数 ≈ T/0.35，1000ms 档 ≈ T/1.0；速度窗口样本上限从 14 降到 5，速度曲线抖动变大。

**预期结果**：事件频率与刷新间隔成反比，且每 tick 的 `delta` 大约增大 2.86 倍（间隔变长、单样本增量变大），平均速度数值基本不变——窗口算法保证的是速率估计，不是事件量。

*（具体数字待本地验证。）*

#### 4.3.5 小练习与答案

**练习 1**：`calcSpeed` 收到负增量时为什么清空整个窗口并返回 0？

**答案**：负增量意味着累计进度回落——fetcher 被重建、Blob 生产者切换、或断点重算。此时历史样本对应的「旧进度基线」已失效，继续平均会产出无意义的速度；清窗后从新基线重新积累。

**练习 2**：窗口为什么取「最近 5 秒」而不是「从头到 now 的总平均」？

**答案**：下载中用户关心的是**当前**速率（决定还要等多久、该不该换镜像），总平均会被启动慢、中途卡顿稀释，反应迟钝；5 秒窗口既平滑抖动又保持敏感。总平均只在完成时一次性汇报（`markTaskDone`）。

**练习 3**：刷新协程为什么用 `TryLock` 而不是阻塞 `Lock`？

**答案**：两个原因。正确性：注释指出启动过程可能正在替换资源元数据（Blob 解析过渡期），读到半初始化状态会算出错进度；健壮性：刷新协程是全局唯一的，若被某个任务长期阻塞，**所有**任务的进度、事件、检查点都会停摆。跳过一个 tick 的代价远小于全局停摆。

### 4.4 检查点保存策略：shouldSaveTaskCheckpoint

#### 4.4.1 概念说明

刷新协程每 tick 都会更新内存进度、发 `progress` 事件，但**落盘是有条件的**。这就是检查点策略要回答的问题：崩溃后最多丢多少进度 vs 空闲时写多少次磁盘。

判定函数只有一行：

[pkg/download/downloader.go:L395-L397](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L395-L397)

```go
func shouldSaveTaskCheckpoint(downloadChanged, uploadChanged, uploading, uploadCheckpointDue bool) bool {
	return downloadChanged || uploadChanged || uploading && uploadCheckpointDue
}
```

读作：**下载有增量，或上传有增量，或（在做种且 30 秒周期到了）**，三者其一即写检查点。反过来：

- 任务静默（无下载增量、不做种）：一个字节都不写盘——暂停的任务挂在列表里不会产生任何 IO。
- 纯做种无流量：`uploading && uploadCheckpointDue` 兜底，每 30 秒（`uploadCheckpointInterval`，[L36-L37](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L36-L37)）写一次，为的是把「做种时长」这类协议自有的状态固化，重启后做种统计不丢。

「下载有增量就写」意味着检查点频率 = 有效下载的 tick 频率，即约 `1000/RefreshInterval` 次/秒/任务。这就是 4.3 实践里「测量存储写入次数」的理论基线。

#### 4.4.2 核心流程

一次 `saveTask`（检查点）写两个桶；统计快照（第三个桶）由 `captureTaskStats` 独立管理：

```text
刷新协程 tick（任务有增量）
  ├─ saveTask(task)
  │    ├─ fetcherManager.Store(fetcher) → data
  │    │     data != nil → Put(bucketSave, task.ID, data)   // 协议续传数据
  │    │     data == nil → Delete(bucketSave, task.ID)      // 协议表示无可续传状态
  │    └─ task.clone()（statusLock 下）→ Put(bucketTask, task.ID)  // 任务快照
  └─ captureTaskStats(task, live=true, force=false)
       ├─ fetcher.Stats() → Snapshot/Runtime
       ├─ 仅 Snapshot 参与 JSON 序列化（Runtime 只活在内存）
       └─ 非 force 且 JSON 与上次相同 → 跳过 Put(bucketTaskStats)
```

三个桶的写入时机对照：

| 桶 | 写入时机 | 内容 | 崩溃后作用 |
| --- | --- | --- | --- |
| `task` | 每次检查点、启动、完成、出错、Patch | 任务元数据 + 进度快照 | 任务列表与进度恢复 |
| `save` | 每次检查点 | 协议续传数据（HTTP chunk 表、BT 会话等） | 断点续传 |
| `task_stats` | Snapshot JSON 变化时，或 force（完成/出错时） | 协议统计快照（peers、piece 等） | 统计页恢复 |

#### 4.4.3 源码精读

**判定与调用点**：

[pkg/download/downloader.go:L369-L381](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L369-L381) —— emit 之后（注意：事件不受检查点策略影响，每 tick 照发）才做判定；注释直说动机：「即使无流量也要周期性固化做种状态，让 BT 做种时长这类协议自有值保持持久」。`uploadCheckpointDue` 在 tick 开头统一计算（[L294-L296](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L294-L296)），周期到了就在 tick 末尾重置（[L385-L387](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L385-L387)）。

**saveTask 双桶写入**：

[pkg/download/downloader.go:L489-L514](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L489-L514) —— 先 `fetcherManager.Store(task.fetcher)` 序列化协议续传数据：`data != nil` 写入 `bucketSave`；`data == nil`（协议表示当前无可续传状态，例如单连接已完成段落）则**删除**旧键——残留旧键会在 Restore 时复活过期状态。然后在 `statusLock` 下深克隆任务快照写入 `bucketTask`。深克隆是必须的：落盘的 JSON 序列化不能与并发的进度写入互相撕扯。

**统计快照的「变化才写」**：

[pkg/download/downloader.go:L1024-L1084](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1024-L1084) —— `captureTaskStats(task, live, force)`：`live=true` 保留 Runtime 载荷供内存读取（刷新协程每 tick 调用）；落盘只序列化 Snapshot，且 `!force && bytes.Equal(snapshot, task.statsSnapshot)` 时跳过写入（[L1075-L1078](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1075-L1078)）——统计没变就不写盘。`watch` 完成分支用的是 `(false, true)`（[L1586-L1588](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1586-L1588)）：丢弃 Runtime、强制落一份最终稳定快照。函数头注释（[L1021-L1023](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1021-L1023)）点明设计：Runtime 遥测「任务失活即弃」。

**错误路径也落快照**：

[pkg/download/downloader.go:L1731-L1738](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1731-L1738) —— `handleOnError` 里同样强制 `captureTaskStats(false, true)`，任务确为 error 后再 `Put(bucketTask)`。保证失败现场（进度、错误时的统计）也被固化，重启后可见。

#### 4.4.4 代码实践

**实践目标**：定量验证检查点策略——同样的下载，350ms 与 1000ms 两档各产生多少次存储写入。

**操作步骤**：

1. 给 Storage 加一个按桶计数的包装器（完整代码见第 5 节）：

```go
// 示例代码：按桶统计 Put 次数的 Storage 装饰器
type countingStorage struct {
	download.Storage // 内嵌接口，其余方法零成本转发
	mu   sync.Mutex
	puts map[string]int
}

func (c *countingStorage) Put(bucket, key string, v any) error {
	c.mu.Lock()
	c.puts[bucket]++
	c.mu.Unlock()
	return c.Storage.Put(bucket, key, v)
}
```

2. 分别以 `RefreshInterval: 350` 和 `1000` 运行同一个约 5 秒的慢速下载，结束后打印 `puts`。
3. 对照理论值：有效下载期间每 tick 一次 `saveTask`（= 1 次 `save` Put + 1 次 `task` Put），加上启动 1 次、完成 1 次。

**需要观察的现象**：350ms 档约 14 次检查点（5s ÷ 0.35s），1000ms 档约 5 次；`task` 与 `save` 两个桶的 Put 次数几乎相等（每次 saveTask 各写一次）；`task_stats` 桶次数取决于统计是否变化，通常明显更少。

**预期结果**：写入次数与 RefreshInterval 成反比——这就是「更细的进度粒度」在 IO 上的价格；反之把间隔调大是在用进度刷新率换磁盘寿命（bbolt 下每次 Put 是一个写事务）。

*（具体计数待本地验证。）*

#### 4.4.5 小练习与答案

**练习 1**：一个暂停了两天的任务会累积多少次存储写入？

**答案**：零。暂停任务被刷新协程的状态闸门（非 running 且非 uploading）直接跳过，不进 `shouldSaveTaskCheckpoint` 判定，也没有任何事件。检查点只在「有增量的 tick」或「做种周期到期」时发生。

**练习 2**：为什么做种任务即使没有任何上传流量，也要每 30 秒写一次检查点？

**答案**：做种时长等状态由协议（BT 客户端）自己持有，不在 `Progress.Downloaded/Uploaded` 里。若只按「增量」判定，纯做种任务永远不落盘，重启后做种时长归零。源码注释原文：「Store active upload state periodically even without traffic so protocol-owned values such as BT seed time remain durable」。

**练习 3**：`saveTask` 里 `Store` 返回 nil 时为什么删除 `bucketSave` 的旧键，而不是什么都不做？

**答案**：nil 表示协议当前无可续传状态。如果旧键残留，重启恢复时 `Restore` 会把过期的续传数据（比如已失效的 chunk 表）灌回新 fetcher，轻则进度错乱重则数据损坏。删除旧键让「无状态」也是一个被持久化的事实。

## 5. 综合实践：事件观察器 watchprobe

把四个模块串成一个工具：**慢速 HTTP 服务器 + 可调 RefreshInterval 的 Downloader + 全事件监听 + 按桶计数存储**，一次运行同时得到事件序列、事件频率与存储写入次数，最后与 `cmd/gopeed` 对比。

**实践目标**：用一份代码回答三个问题——事件按什么顺序发？事件频率由什么决定？落盘次数由什么决定？

**操作步骤**：

1. 在仓库外新建目录 `watchprobe/`，写入 `main.go`（示例代码，依赖主 module，可放进仓库内任意子目录用 `go run` 运行，或复制 module 路径自建 go.mod）：

```go
// 示例代码：watchprobe —— 事件频率与检查点写入观测器
package main

import (
	"bytes"
	"flag"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"sync"
	"time"

	"github.com/GopeedLab/gopeed/pkg/base"
	"github.com/GopeedLab/gopeed/pkg/download"
	ghttp "github.com/GopeedLab/gopeed/pkg/protocol/http"
	"github.com/GopeedLab/gopeed/pkg/util"
)

// ---- 按桶计数Storage 装饰器 ----
type countingStorage struct {
	download.Storage
	mu   sync.Mutex
	puts map[string]int
}

func newCountingStorage(inner download.Storage) *countingStorage {
	return &countingStorage{Storage: inner, puts: map[string]int{}}
}

func (c *countingStorage) Put(bucket, key string, v any) error {
	c.mu.Lock()
	c.puts[bucket]++
	c.mu.Unlock()
	return c.Storage.Put(bucket, key, v)
}

// ---- 限速 Seeker：让 ServeContent 支持 Range 的同时限制读速 ----
type slowSeeker struct{ *bytes.Reader }

func (s slowSeeker) Read(p []byte) (int, error) {
	if len(p) > 64<<10 { // 每次最多读 64KB
		p = p[:64<<10]
	}
	time.Sleep(20 * time.Millisecond) // ≈3.2MB/s，16MB 约 5 秒
	return s.Reader.Read(p)
}

func main() {
	interval := flag.Int("interval", 350, "RefreshInterval in ms")
	flag.Parse()

	// 1. 慢速服务器：http.ServeContent 自动处理 Range/206/Content-Length
	data := bytes.Repeat([]byte{0xA5}, 16<<20)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.ServeContent(w, r, "probe.bin", time.Time{}, slowSeeker{bytes.NewReader(data)})
	}))
	defer srv.Close()

	// 2. 自建 Downloader：Boot() 单例改不了 RefreshInterval
	tmp, _ := os.MkdirTemp("", "watchprobe")
	store := newCountingStorage(download.NewMemStorage()) // 先留住引用，汇报时读它
	d := download.NewDownloader(&download.DownloaderConfig{
		RefreshInterval: *interval,
		Storage:         store,
		StorageDir:      tmp,
	})
	if err := d.Setup(); err != nil {
		panic(err)
	}
	defer d.Close()

	// 3. 全事件监听：计数 + progress 打印一行
	events := map[string]int{}
	var mu sync.Mutex
	finallyCh := make(chan error, 1)
	d.Listener(func(ev *download.Event) {
		mu.Lock()
		events[string(ev.Key)]++
		mu.Unlock()
		if ev.Key == download.EventKeyProgress {
			p := ev.Task.Progress
			total := int64(0)
			if ev.Task.Meta != nil && ev.Task.Meta.Res != nil {
				total = ev.Task.Meta.Res.Size
			}
			fmt.Printf("\rprogress %s / %s  %s/s   ",
				util.ByteFmt(p.Downloaded), util.ByteFmt(total), util.ByteFmt(p.Speed))
		} else {
			fmt.Printf("\n[%s] err=%v\n", ev.Key, ev.Err)
			if ev.Key == download.EventKeyFinally {
				finallyCh <- ev.Err
			}
		}
	})

	start := time.Now()
	if _, err := d.CreateDirect(&base.Request{URL: srv.URL + "/probe.bin"}, &base.Options{
		Path:  tmp,
		Extra: ghttp.OptsExtra{Connections: 1},
	}); err != nil {
		panic(err)
	}
	if err := <-finallyCh; err != nil {
		panic(err)
	}
	elapsed := time.Since(start)

	// 4. 汇报
	mu.Lock()
	fmt.Printf("\n---- elapsed=%s interval=%dms ----\n", elapsed.Round(time.Millisecond), *interval)
	for k, v := range events {
		fmt.Printf("event %-8s x %d\n", k, v)
	}
	store.mu.Lock()
	for bucket, n := range store.puts {
		fmt.Printf("put bucket %-12s x %d\n", bucket, n)
	}
	store.mu.Unlock()
	mu.Unlock()
}
```

4. 运行两遍并对比：

```bash
go run . -interval 350
go run . -interval 1000
```

**需要观察的现象**：

- 事件序列：`start` → 多次 `progress` → `done` → `finally`（相邻且各一次）。
- `progress` 次数 ≈ 下载时长 ÷ interval（每档差约 2.86 倍）。
- `put bucket save` 与 `put bucket task` 次数几乎相同且 ≈ progress 次数（每 tick 有增量即检查点）；`task_stats` 通常更少（Snapshot 未变则跳过）。
- interval 越大，`Speed` 曲线越阶梯化（窗口样本上限从约 14 降到 5）。

**预期结果**：一张三列对照表（350ms / 1000ms / 倍率），证明「事件频率与检查点频率都由 RefreshInterval 驱动，而平均速度数值基本不受影响」。

**与 cmd/gopeed 对比优劣**：官方 CLI 用 `Boot()` 三行接线、渲染精美（`\r` 覆盖、防残影、空任务兜底），但单例写死 350ms/MemStorage，无法观测存储行为；watchprobe 牺牲渲染换取三样官方版给不了的东西——可调刷新间隔、全事件审计、按桶写入计数。这也正好回答了「什么时候该用 Boot、什么时候必须 NewDownloader」：产品化脚本用 Boot，做实验与二次开发用 NewDownloader。

*（本实践未在当前环境运行，输出数字待本地验证。）*

## 6. 本讲小结

- **watch 是任务结果的翻译官**：每个运行中任务一个守夜人协程，阻塞在 `fetcher.Wait()`，把 Fetcher 的「完成/失败」翻译成 `markTaskDone`/`handleOnError` 状态迁移 + 事件 + 最终快照 + `notifyRunning` 唤醒等待队列；`watchedTasks` 去重与 `runGeneration` 代数共同防住快速暂停/继续与错误竞态。
- **七键一回调的极简事件系统**：`start/progress/pause/error/delete/done/finally`，单一 Listener、同步调用、必须在锁外 emit（监听器允许回调 Pause/Continue）；`finally` 是紧跟 `done`/`error` 的合成收尾事件，恰好一次，`Err==nil` 即成功。
- **进度是拉模型**：全局唯一刷新协程每 `RefreshInterval`（默认 350ms）轮询所有 running/uploading 任务；速度是约 5 秒滑动窗口的平均增量，完成时改写为全程平均；`TryLock` + 状态闸门保证刷新协程永不卡死、不读半初始化状态。
- **检查点策略在「丢进度」与「写磁盘」间取衡**：`downloadChanged || uploadChanged || (uploading && 30s 周期到)` 才落盘——静默任务零 IO，做种任务每 30 秒固化协议自有状态；`saveTask` 一次写 `save`（续传数据）+ `task`（任务快照）两个桶，统计快照「变化才写」。
- **Boot 与 NewDownloader 的分水岭**：`Boot()` 背后是不可配置的默认单例（350ms/MemStorage）；要改刷新间隔、换存储、注入自定义协议，必须自己 `NewDownloader`。

## 7. 下一步学习建议

- **u4-l4 暂停、继续、删除与最大并发调度**：本讲两次遇到 `notifyRunning()` 却只把它当黑盒——下一讲讲清 wait 队列、`doPauseForScheduling` 如何在 MaxRunning 满载时压任务回 wait，以及 `runGeneration` 在暂停/完成竞态下的完整防护。建议先重读 [pkg/download/downloader.go:L578-L603](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L578-L603)。
- **u4-l5 持久化与断点恢复**：本讲的检查点写了什么，下一讲讲重启后 `fetcherCache`、`restoreTask`/`restoreFetcher` 如何把它们读回来续传，两讲合起来才是完整的持久化闭环。
- **u8-l4 任务事件流**：核心层的单 Listener 如何变成 REST/FFI 多端可订阅的事件管道（`TaskEventMask` 位掩码），以及为什么平台回调绝不能压垮下载协程。
- 顺带可以精读 [cmd/gopeed/main.go:L82-L113](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go#L82-L113) 的终端渲染细节（`\r` 覆盖、行宽补齐、空任务兜底），如果你的综合实践工具想做得更漂亮。
