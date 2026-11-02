# 暂停、继续、删除与最大并发调度

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `TaskFilter` 的「过滤批量」操作模型，以及组 ID 如何经 `expandGroupTaskIDs` 展开为成员任务 ID，使「暂停一个分组」变成「暂停组内全部任务」。
2. 读懂 wait 队列的完整生命周期：任务如何入队（`doCreate`）、如何被唤醒（`notifyRunning`）、暂停/删除时如何被同步清理。
3. 掌握 `Continue` 的「腾位算法」：`MaxRunning` 满员时 `doPauseForScheduling` 如何把正在运行的任务物理暂停并压回 wait 队列。
4. 理解 `runGeneration` 代数机制如何防护「暂停与完成同时发生」的竞态，以及 `preparePause`/`runPauseHandler` 两段式设计。
5. 解释重写后的 `Delete` 全流程：`deletingTasks` 标记、`doDelete` 的删除顺序、删除失败时任务被重新写回 `bucketTask` 与 `d.tasks` 的恢复路径，以及空组清理。

本讲是第四单元的高潮：前三讲讲的是任务如何被创建和驱动，本讲讲的是任务如何被**调度、打断和销毁**——这些操作全部并发地发生在运行中的下载器身上，是全项目竞态最密集的区域。

## 2. 前置知识

- **六态状态机**（u2-l2）：`ready → wait → running → pause/error → done`。`wait` 是纯调度概念（排队中），`done` 是唯一终态。状态的唯一写入者是 Downloader，Fetcher 只通过 `Wait()` 返回值汇报结果。
- **两把关键的锁**：`d.lock` 保护 Downloader 级数据（`d.tasks`、`d.waitTasks`、`d.groups` 等）；`task.statusLock` 保护单个任务的 `Status` 与 `runGeneration`。此外还有 `task.lock` 保护任务整体（含 fetcher）。本讲会反复在这三把锁之间切换。
- **wait 队列**（u4-l2）：`d.waitTasks` 是 FIFO 切片，`MaxRunning` 满员时新任务被置为 `wait` 状态入队，空位出现后由 `notifyRunning` 唤醒队首。
- **TaskGroup 与 ParentID**（u2-l1/u4-l1）：批量下载可以建组，组有独立 ID 并持久化在 `task_group` 桶；每个成员任务的 `Task.ParentID` 指向组。对外的批量 API 接受任务 ID **或** 组 ID。
- **事件必须在锁外发射**（u4-l3）：`d.emit` 的监听器可能回调 Downloader 方法重新抢锁，因此在持锁路径上先把要发的事件收集起来，解锁后再发——本讲的 `Continue` 和 `doPauseForScheduling` 都遵守这条纪律。

一个贯穿全讲的直觉：**暂停/继续/删除都是「先改内存调度态，再做物理动作，最后补持久化」的三段式**，而任何一段都可能与其他操作交错，防御手段就是「状态守卫 + 代数校验」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pkg/download/downloader.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go) | 本讲主战场：`Pause`/`Continue`/`Delete`、wait 队列调度、`runGeneration`、删除恢复全部在此 |
| [pkg/download/model.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go) | `Task` 结构（含 `runGeneration` 私有字段）、`updateStatus`、`TaskFilter` |
| [pkg/base/constants.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/constants.go) | 六个下载状态常量定义 |
| [pkg/api/service.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go) | REST 层如何把 URL 参数包装成 `TaskFilter` 调用本讲的三个方法 |
| [pkg/download/downloader_test.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go) | 本讲的「证据库」：代数竞态、调度暂停、删除恢复均有专门测试 |
| [pkg/download/storage.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/storage.go) | `Storage` 接口；实践环节用包装存储注入失败 |

## 4. 核心概念与源码讲解

### 4.1 过滤器批量模型：TaskFilter 与组 ID 展开

#### 4.1.1 概念说明

暂停、继续、删除面对的不是单个任务，而是「一批任务」。Gopeed 用一个极简的 `TaskFilter` 表达「选中哪些任务」：按 ID 列表、按状态白名单、按状态黑名单三个维度做交集。所有批量操作都是同一个套路：

1. 过滤器为空/为 nil → 作用于**全部**任务；
2. 否则先把 ID 列表里的**组 ID 展开成成员任务 ID**；
3. 再叠加状态黑名单（比如暂停时跳过已经是 pause/error/done 的任务）；
4. 最后对匹配到的每个任务执行动作。

组 ID 展开是本讲义所在版本（`1e36599` 引入任务分组）带来的新语义：用户在前端对着一个「文件夹下载」卡片点暂停，传上来的是组 ID，Downloader 必须把它翻译成组内全部成员任务。

#### 4.1.2 核心流程

```text
Pause/Continue/ContinueBatch(filter)
  ├─ filter == nil 或 IsEmpty() → 全量分支（pauseAll / continueAll）
  ├─ len(filter.IDs) > 0
  │    ├─ expandGroupTaskIDs(IDs)：持 d.lock，扫描 d.tasks
  │    │    保留直接命中的任务 ID + ParentID 命中组的成员 ID
  │    └─ 展开结果为空 → 返回 ErrTaskNotFound
  ├─ 叠加 NotStatuses（排除不可操作的状态）
  ├─ GetTasksByFilter(filter) → 目标任务列表（为空则 ErrTaskNotFound）
  └─ 逐个执行动作
```

ID 匹配是「命中即真」的并集，状态匹配是交集：

\[ \text{match}(t) = \text{idMatch}(t) \wedge \text{statusMatch}(t) \wedge \text{notStatusMatch}(t) \]

其中任一维度为空即视为该维度恒真。

#### 4.1.3 源码精读

过滤器本体只有三个字段：

[pkg/download/model.go:L221-L229](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L221-L229) —— `TaskFilter` 定义与 `IsEmpty()`：三个维度全空时表示「全部任务」。

组 ID 展开的实现是一个纯内存扫描，注意它**同时保留**了直接命中的任务 ID 和通过 `ParentID` 命中的成员 ID：

[pkg/download/downloader.go:L830-L848](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L830-L848) —— `expandGroupTaskIDsLocked` 在持有 `d.lock` 时遍历 `d.tasks`，条件是「任务 ID 被选中，或任务的 `ParentID` 被选中」；外层包装 `expandGroupTaskIDs` 负责加锁。注释点明设计意图：即使组的元数据暂不可用，也能凭 `parentId` 关系完成展开。

`Pause` 的入口展示了完整的过滤流程，包括状态黑名单的叠加：

[pkg/download/downloader.go:L962-L1000](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L962-L1000) —— 空过滤器走 `pauseAll`；否则展开组 ID、把 `NotStatuses` 设为 `{pause, error, done}`（这些状态的任务暂停无意义），再取出目标任务依次 `doPause`，最后 `notifyRunning` 让出空位。注意一个细节：`Pause` 直接改写了调用方传入的 `filter`（`filter.IDs = ...`、`filter.NotStatuses = ...`），而后面将看到的 `Delete` 会先拷贝过滤器——同一份代码里两种风格并存，读源码时要留意这种「过滤器被污染」的副作用。

过滤器的求值逻辑：

[pkg/download/downloader.go:L1677-L1723](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1677-L1723) —— `getTasksByFilter` 的三个内嵌匹配函数。关键陷阱：`len(filter.IDs) == 0` 时 `idMatch` 对**所有**任务返回真——「空 ID 列表」等价于「不过滤」。这解释了后文 `Delete` 里为何要用一个哨兵 ID 防止语义反转。

REST 层如何构造过滤器：

[pkg/api/service.go:L208-L230](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L208-L230) —— `pauseTask`/`continueTask` 把路径参数 `id` 包成 `TaskFilter{IDs: []string{taskID}}`，`applyTaskAction` 统一分发；批量版本（`pauseTasks` 等）则从 query 参数解析过滤器。前端传上来的既可能是任务 ID 也可能是组 ID，Downloader 一律兼容。

`Continue` 与 `ContinueBatch` 的过滤入口与 `Pause` 同构，只是黑名单不同：

[pkg/download/downloader.go:L1021-L1035](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1021-L1035) —— `Continue` 排除 `{running, done}`，即 pause/error/wait/ready 状态的任务都可以被继续。

[pkg/download/downloader.go:L1126-L1144](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1126-L1144) —— `ContinueBatch` 同样展开组 ID 后对每个匹配任务直接 `doStart`，**不做 MaxRunning 仲裁**（它是一条「越过闸门」的通道）。值得注意：该方法目前在仓库内没有任何调用方（含测试），属于预留接口——阅读源码时要能识别「存在但未接线」的代码，不要臆测它的用途。

#### 4.1.4 代码实践

**实践目标**：用 REST API 直观感受「组 ID 展开」的批量语义。

**操作步骤**（需要能访问网络的目标 URL，若无可先做源码阅读部分）：

1. 参考 u1-l2 启动开发 API 服务：`go run ./cmd/api`（默认监听 `localhost:9999`，无 token）。
2. 提交一个分组批量任务（示例代码，可用 `curl` 或 REST 客户端）：

```bash
curl -X POST http://localhost:9999/api/v1/tasks/batch \
  -H 'Content-Type: application/json' \
  -d '{"group":true,"groupName":"demo","reqs":[{"req":{"url":"https://example.com/a.zip"}},{"req":{"url":"https://example.com/b.zip"}}]}'
```

3. 记下响应里的 `parentId` 与两个 `taskIds`。
4. 用**组 ID** 暂停：`curl -X PUT "http://localhost:9999/api/v1/tasks/pause?id=<parentId>"`（查询参数是可重复的 `id`，由 `taskFilterFromQuery` 收集为数组），然后 `GET /api/v1/tasks` 查看两个成员任务的状态。
5. 阅读上面的 `expandGroupTaskIDsLocked` 源码，确认第 4 步的效果正是它造成的。

**需要观察的现象**：一次调用后**两个**成员任务同时变为 `pause`，而不是只暂停了某个不存在的「组任务」——组本身不是任务，只是一份共享目录元数据。

**预期结果**：两个成员任务状态均为 `pause`。命令执行效果「待本地验证」（取决于网络与目标 URL 可达性）；源码层面可确认：`expandGroupTaskIDsLocked` 会把 `parentId` 翻译成两个成员 ID。

#### 4.1.5 小练习与答案

**练习 1**：`Pause` 里如果把 `NotStatuses` 设为 `{running, wait}` 会发生什么？

**答案**：只有 `ready` 状态的任务能被暂停（error/pause/done 未被排除但它们本来就不在可暂停路径上），正在运行与排队的任务全部被过滤器排除，`pauseTasks` 很可能为空从而返回 `ErrTaskNotFound`——这与「暂停」的语义正好相反。`NotStatuses` 排除的是「暂停无意义/不可能」的状态，而不是「要暂停」的状态。

**练习 2**：为什么 `expandGroupTaskIDsLocked` 需要调用方持有 `d.lock`，而 `expandGroupTaskIDs` 又提供了一个加锁版本？

**答案**：展开过程要遍历 `d.tasks` 且读取 `task.ParentID`，必须与 `Delete`/批量创建等修改 `d.tasks` 的路径互斥。已持锁的调用方（如 `Delete`）直接用 Locked 版本避免重复加锁导致死锁；`Pause`/`Continue` 未持锁时用加锁包装版。

**练习 3**：一个组内只有 2 个成员，其中 1 个已 `done`。用组 ID 调用 `Continue` 后，`done` 的任务会被再次启动吗？

**答案**：不会。展开后的 ID 仍指向全部成员，但 `Continue` 叠加了 `NotStatuses = {running, done}`，`done` 成员在 `GetTasksByFilter` 处被排除；即便漏过，`doStart` 的状态守卫（见 4.3.3）也会对 `done` 任务直接返回。

### 4.2 调度核心：wait 队列、notifyRunning 与 doPauseForScheduling

#### 4.2.1 概念说明

`MaxRunning` 是全局最大并发下载数。它由一个「闸门 + 队列」实现：

- **闸门**：`remainRunningCount()` 实时计算「还能再跑几个」；
- **队列**：满员时新任务置为 `wait` 状态进入 `d.waitTasks`（FIFO），空位出现时由 `notifyRunning()` 从队首取一个启动。

三个动作会触发空位释放，它们都调用 `notifyRunning`：任务暂停后（`Pause` 末尾）、任务删除后（`Delete` 末尾）、任务完成或失败后（`watch` 循环与 `handleOnError` 中）。

本模块最有趣的是 `Continue` 的**腾位算法**：当用户明确要求「继续」某些任务而闸门已满时，Gopeed 不会简单地拒绝或无限排队，而是**主动暂停一些正在运行的任务**给新任务腾位——被腾位的任务回到 wait 队列**尾部**，稍后自动恢复。这就是 `doPauseForScheduling`（为调度而暂停）与普通 `doPause`（用户暂停）的区别：前者终态是 `wait`（回队列），后者终态是 `pause`（停下来等用户）。

#### 4.2.2 核心流程

空位计算：

\[ \text{remainRunningCount} = \text{MaxRunning} - \#\{t \in \text{tasks} \mid t.\text{status} = \text{running}\} \]

`Continue` 的腾位决策（全部在 `d.lock` 内完成）：

\[ \text{needRunningCount} = \min(\text{MaxRunning},\ \text{continueCount}) \]
\[ \text{needPauseCount} = \text{needRunningCount} - \text{remainRunningCount} \]

```text
Continue(过滤出 continueTasks)
  持 d.lock：
    若 needPauseCount > 0：
      按 d.tasks 顺序遍历 running 任务
        doPauseForScheduling(task)：
          物理暂停 fetcher（不 emit 事件！）
          若仍处于本次暂停的代数 → 置 wait，返回 queued=true
        queued → 追加到 waitTasks 尾部（腾位成功，计数+1）
      直到腾够 needPauseCount 个
    continueTasks 前 needRunningCount 个 → realContinueTasks（即将启动）
    其余 → 置 wait，追加到 waitTasks 尾部
  释放 d.lock：
    对腾位任务补发 EventKeyPause（锁外发射！）
    对 realContinueTasks 逐个 doStart
```

`notifyRunning` 则简单得多：异步抢 `d.lock`，若还有空位且队列非空，取队首 `doStart`。

#### 4.2.3 源码精读

wait 队列与竞态标记字段在 Downloader 结构体上：

[pkg/download/downloader.go:L120-L131](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L120-L131) —— `d.tasks`（全量）、`d.waitTasks`（排队中）、`d.groups`/`d.creatingGroups`（分组元数据与创建中标记）、`d.deletingTasks`（删除中标记，见 4.4），以及保护它们的 `d.lock`。

空位计算与队列唤醒：

[pkg/download/downloader.go:L617-L625](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L617-L625) —— `remainRunningCount` 用 `taskStatus`（内部抢 `statusLock`）逐个统计 running 数，再减 `MaxRunning`。

[pkg/download/downloader.go:L600-L615](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L600-L615) —— `notifyRunning` 起一个新 goroutine 抢 `d.lock`，切掉队首任务并 `doStart`。注意它是 `go func()` 异步的：调用方（如 `Pause`）不必等队列消费完成即可返回。

任务入队的入口在 `doCreate`：

[pkg/download/downloader.go:L2210-L2217](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2210-L2217) —— 满员时把任务置 `wait` 并追加到 `d.waitTasks`，否则直接 `doStart`。入队发生在 `d.lock` 临界区内，与 `notifyRunning` 的消费端互斥。

`Pause` 在物理暂停之前会先**同步清理 wait 队列**——这是本讲的必考细节：

[pkg/download/downloader.go:L978-L997](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L978-L997) —— 持 `d.lock` 用原地过滤（`d.waitTasks[:0]` 复用底层数组）把被暂停的任务从队列里剔除，再逐个 `doPause`，最后 `notifyRunning`。如果不先清队列，会发生什么？`doPause` 把任务置为 `pause`，而队列里的同一指针稍后被 `notifyRunning` 取出 `doStart`——暂停瞬间被「复活」。先剔除再暂停，顺序保证了队列里永远只有 wait 状态的任务。

`Continue` 的腾位主体：

[pkg/download/downloader.go:L1040-L1093](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1040-L1093) —— 闭包内完成全部计算与状态翻转（注意 `pausedTasks` 的收集），解锁后**先补发 pause 事件再 doStart**。L1081-L1082 的注释解释了为什么必须这样做：监听器允许回调 `Pause`/`Continue` 等会抢 `d.lock` 的方法，持锁发射会死锁。

腾位专用暂停：

[pkg/download/downloader.go:L2507-L2525](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2507-L2525) —— `doPauseForScheduling` 的注释直说设计约束：「在调用方持有 d.lock 时物理暂停一个运行中任务且不 emit 事件，然后原子地把它移入 wait 队列状态」。它复用 `preparePause`/`runPauseHandler` 两段式（见 4.3），成功后在 `statusLock` 内检查「是否仍是本次暂停的代数且状态为 pause」，是则翻转为 `wait` 并返回 `queued=true`。

`continueAll`（`Continue(nil)` 的全量分支）则**不做腾位**：

[pkg/download/downloader.go:L1097-L1124](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1097-L1124) —— 注释写明「continue 所有任务但**不影响**已在运行的任务」：只填满剩余空位，多出的置 wait 入队。启动策略上 `ContinueOnStartup`（L1149-L1154）复用的正是这一语义。

#### 4.2.4 代码实践

**实践目标**：创建 8 个任务、`MaxRunning=2`，亲手记录 wait→running 的切换时序，并触发一次腾位调度。

**操作步骤**：仓库测试里有一个专为并发测试设计的 `generation://` 假协议，`holdOpen: true` 时任务**永不完成**，是观察调度的理想工具。在 `pkg/download/` 下新建一个测试文件（如 `schedule_practice_test.go`，练习完成后可删除或保留为本地笔记）：

```go
// 示例代码：读者练习用，仿照仓库内既有测试风格编写
package download

import (
	"testing"

	"github.com/GopeedLab/gopeed/pkg/base"
)

func TestPractice_ScheduleTimeline(t *testing.T) {
	manager := &generationTestManager{holdOpen: true} // 任务永不完成
	d := newTaskGroupDownloader(t, nil, 2)            // MaxRunning=2，见 downloader_test.go:4048
	_ = manager
	ids := make([]string, 0, 8)
	for i := 0; i < 8; i++ {
		id, err := d.CreateDirect(&base.Request{URL: "generation://x"},
			&base.Options{Path: t.TempDir(), Name: "x.bin"})
		if err != nil {
			t.Fatal(err)
		}
		ids = append(ids, id)
	}
	// TODO(读者)：此处轮询打印 8 个任务的状态快照
}
```

说明：`newTaskGroupDownloader` 内部就是注册 `generationTestManager{holdOpen: true}` 并设置 `MaxRunning`（[pkg/download/downloader_test.go:L4048-L4060](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4048-L4060)），读者也可以完全自己写 `NewDownloader(&DownloaderConfig{FetchManagers: ...})`。然后：

1. 创建 8 个任务后立即（轮询 10ms 间隔）打印每个任务状态，直到稳定；
2. `d.Pause(&TaskFilter{IDs: []string{ids[0]}})`，继续轮询观察谁被 `notifyRunning` 唤醒；
3. 把 6 个排队任务之一用 `d.Continue(&TaskFilter{IDs: []string{ids[5]}})` 继续，观察腾位：谁被 `doPauseForScheduling` 压回队列、队列顺序如何变化；
4. 运行：`go test ./pkg/download/ -run TestPractice_ScheduleTimeline -v`。

**需要观察的现象**：稳定后恰好 2 个 `running`、6 个 `wait`；暂停一个 running 后队首任务自动变 `running`；`Continue(ids[5])` 时**另一个 running 任务**被腾位成 `wait` 并排到队尾，`ids[5]` 变 `running`。

**预期结果**：与 `TestDownloader_SchedulingPauseCompletesBeforeReplacementStart`（[pkg/download/downloader_test.go:L697-L765](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L697-L765)）断言的行为一致：替换任务在物理 Pause 完成之前不得 `Fetcher.Start`（该测试断言 `starts == 1` 不变）。精确的时序数字「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`notifyRunning` 为什么用 `go func()` 异步执行，而不是在调用点同步做？

**答案**：一是调用方（`Pause`/`Delete`/`watch`）不应被队列消费阻塞——`doStart` 可能包含耗时的恢复与文件准备；二是把「取队首 + 启动」整体放进一个抢 `d.lock` 的原子段，天然与入队、腾位等路径互斥，避免队列被并发消费出双份启动。

**练习 2**：`Pause` 时为什么先清 `waitTasks` 再 `doPause`，反过来行不行？

**答案**：不行。若先 `doPause` 再清队列，`doPause` 把任务置为 `pause` 后到清理完成之间，`notifyRunning`（由其他任务的完成事件触发）可能取走队列里这个「已是 pause 状态」的任务并 `doStart`，产生「用户暂停的任务又被启动」的竞态。先剔除使队列中永远只有待启动任务。（当然，即便顺序颠倒，`doStart` 的代数守卫也会兜底拒绝过期启动，但正确顺序让语义在队列层面就保持一致。）

**练习 3**：`continueAll` 与 `Continue(指定任务)` 对「正在运行任务」的处理差异是什么？

**答案**：`continueAll` 完全不动 running 任务，只填空位；`Continue(指定任务)` 在空位不足时会主动 `doPauseForScheduling` 一批 running 任务腾位。前者用于启动时全量续跑等「补位」场景，后者保证用户点「继续」的任务**一定**跑起来。

### 4.3 runGeneration 代数：preparePause 与 runPauseHandler 的竞态防护

#### 4.3.1 概念说明

设想这个场景：任务即将下载完，`watch` 协程正要把 `fetcher.Wait()` 的 nil 结果翻译成 `done`；同一瞬间用户点了暂停，`doPause` 正要把状态翻成 `pause`。两个写者，谁赢？

再想一个更刁钻的：`doStart` 的异步 handler 还在跑（尚未调用 `fetcher.Start`），此时用户先 `Pause` 再 `Continue`——**第一次启动**的 handler 拿到锁后应该继续启动吗？如果不加防护，这个「过期的启动」会把用户刚暂停的任务又拉起来。

Gopeed 的答案是给每个任务装一个**单调递增的代数计数器** `task.runGeneration`（`uint64`）：每次成功的状态迁移（start、pause、done、error）都 `++`。任何异步操作在真正产生副作用之前，都会核对「当前代数是否还是我出发时记录的那一代」——不是，就 silently 放弃。这本质上是乐观并发控制：**状态迁移是「票据」，代数是「票据编号」，编号过期作废**。

两段式暂停是代数机制的第一个应用：

- **`preparePause`（第一段）**：在 `statusLock` 内检查状态可暂停 → 置 `pause`、`runGeneration++`、暂停计时器，返回新代数。这一段是**原子的状态声明**：「从现在起，这个任务由我这次暂停负责」。
- **`runPauseHandler`（第二段）**：在 `task.lock` 内做物理动作（`fetcher.Pause()`、统计快照、`saveTask` 落盘），每个动作前核对代数仍是第一段返回的那一代。

为什么拆开？因为第一段必须快（抢 `statusLock` 即得），让「谁赢得了暂停」立刻有定论；第二段慢（fetcher 暂停、磁盘 IO），必须允许与新的 `Continue` 并发——而新 `Continue` 会再次 `runGeneration++`，使第二段的代数核对失败、放弃剩余动作。

#### 4.3.2 核心流程

```text
doPause(task)                         doStart(task)
  preparePause:                          statusMut(持 statusLock):
    持 statusLock:                         非 running/done 可启动
    状态 ∈ {pause,done,error} → 返回        restoreTask
    置 pause, runGeneration++ (记为 G)      置 running, runGeneration++ (记为 G')
    timer.Pause()                          （记下 G'）
  runPauseHandler(G):                   异步 handler（持 task.lock）:
    持 task.lock:                          核对 running 代数 == G'？
    taskIsPausedGeneration(G)? ──否→ 放弃    否 → 直接 return（过期启动作废）
    fetcher.Pause()                        …prepareTask / fetcher.Start…
    captureTaskStats(快照)                再核对一次（Start 前最后防线）
    saveTask(落盘)
  handled → emit(EventKeyPause)         成功 → emit(EventKeyStart) + go watch(task)
```

watch 循环的终态裁决也靠守卫函数完成：`markTaskDone`/`markTaskError` 只在状态为 `running` 时才允许写入终态（见 4.5），配合代数递增，任何「过期协程」都只能空手而归。

代数防护覆盖的三类经典竞态：

| 竞态场景 | 防护点 |
| --- | --- |
| 暂停 vs 完成：`watch` 想标 done，`doPause` 想标 pause | `markTaskDone` 要求状态仍是 running；`preparePause` 先到则 `watch` 落败静默退出 |
| 过期启动：连续 Pause→Continue，第一次 `doStart` 的 handler 晚到 | handler 内两次 `taskIsRunningGeneration(G')` 核对 |
| 过期暂停动作：Pause 后被 Continue，`runPauseHandler` 才拿到 `task.lock` | `taskIsPausedGeneration(G)` 核对失败 → 放弃 |

#### 4.3.3 源码精读

代数字段是 Task 的私有运行时字段（不序列化，重启后由 `initTask` 重建，从 0 开始）：

[pkg/download/model.go:L79-L91](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L79-L91) —— `runGeneration uint64` 位于运行时层字段区，与 `statusLock`、`speedArr` 等并列（分层概念见 u2-l2）。

`doStart` 的两段结构与代数记录：

[pkg/download/downloader.go:L2368-L2393](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2368-L2393) —— 同步段在 `statusMut` 内完成「可启动检查 + 置 running + 代数自增」，把新代数捕获进局部变量 `generation`。

[pkg/download/downloader.go:L2396-L2411](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2396-L2411) —— 异步 handler 一进来就核对 `taskIsRunningGeneration(task, generation)`，失败直接 return；`triggerOnStart`（扩展钩子可能耗时）之后**再核对一次**——扩展回调期间状态可能已被并发修改。

[pkg/download/downloader.go:L2450-L2461](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2450-L2461) —— 调用 `fetcher.Start()` 前的最后一道防线：在 `statusLock` 内检查 `Status == running && runGeneration == generation`，通过才启动并把 `started` 置 true。

[pkg/download/downloader.go:L2472-L2485](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2472-L2485) —— goroutine 收尾：handler 失败时也只在「代数仍有效」时才 `doOnError`；`started` 且代数有效才发 `EventKeyStart` 并启动 `watch`。

两个守卫函数（代数核对的原语）：

[pkg/download/downloader.go:L2577-L2593](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2577-L2593) —— `taskIsRunningGeneration` 要求「running 且代数一致」；`taskIsPausedGeneration` 放宽为「pause **或** wait 且代数一致」——后者兼容 `doPauseForScheduling`（它会把 pause 翻成 wait，见 4.2）。两者都防御 `statusLock == nil`（任务尚未 `initTask` 或来自反序列化快照）。

两段式暂停本体：

[pkg/download/downloader.go:L2527-L2544](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2527-L2544) —— `preparePause` 全部逻辑在 `statusMut` 闭包内：pause/done/error 状态直接短路返回（幂等），否则置 pause、`runGeneration++`、`timer.Pause()`（停止净用时计时，见 u4-l3 的速度与时间统计）。

[pkg/download/downloader.go:L2546-L2575](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2546-L2575) —— `runPauseHandler` 持 `task.lock`：先核对暂停代数，再依次 `fetcher.Pause()` → 统计快照（失败仅告警，注释说明「fetcher 已暂停，生命周期不能被可选的统计失败打断」）→ `saveTask` 落盘（无 fetcherManager 时退化为直接写 `bucketTask`）。返回 `handled` 表示这次暂停真正生效。

[pkg/download/downloader.go:L2490-L2505](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2490-L2505) —— `doPause` 把两段串起来，`handled` 为真才 emit `EventKeyPause` 并触发 `WebhookEventDownloadPause`。对比 `doPauseForScheduling`（4.2.3）：它调用同样的两段但**不 emit**——因为调用方持着 `d.lock`，事件由 `Continue` 收集后在锁外补发。

仓库里有两个直击竞态的测试，值得逐行读：

[pkg/download/downloader_test.go:L577-L633](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L577-L633) —— `TestDownloader_StaleStartGenerationCannotRestartTask`：用 `task.lock.Lock()` 人为制造「第一次 Start、Pause 处理器、第二次 Start 全部排队」的极端交错，最终断言 `Fetcher.Start` 只发生 1 次——过期启动被代数守卫拦下。

[pkg/download/downloader_test.go:L635-L695](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L635-L695) —— `TestDownloader_PauseWinsAgainstStaleResolveError`：让 `Resolve` 阻塞后返回错误，期间完成 Pause，断言「暂停赢」且陈旧的 Resolve 错误既不能覆盖 pause 状态、也不能发出 error 事件。

假协议工具箱（实践复用）：`generationTestManager`/`generationTestFetcher` 定义在 [pkg/download/downloader_test.go:L33-L44](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L33-L44) 与 [L128-L185](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L128-L185)——`Start` 可挂起（`holdOpen`）、`Resolve`/`Pause` 可注入阻塞与错误（`resolveRelease`/`pauseRelease` 通道），`Wait()` 阻塞在 done 通道上。

#### 4.3.4 代码实践

**实践目标**：复现「暂停与完成同时发生」，验证 runGeneration 防止过期协程把任务标成 done。

**操作步骤**：由于 `generationTestFetcher.Wait()` 在 `holdOpen: true` 时永不返回，无法直接制造「完成与暂停同时」，我们改用它的孪生场景（同一防护机制覆盖）：**「Resolve 阻塞期间发起 Pause，随后 Resolve 失败返回」**——这正是陈旧结果试图覆盖新状态的竞态。在上一步的练习测试文件中追加：

```go
// 示例代码：读者练习用
func TestPractice_PauseWinsOverStaleWork(t *testing.T) {
	release := make(chan struct{})
	manager := &generationTestManager{
		resolveStarted: make(chan struct{}),
		resolveRelease: release,
		resolveErr:     errors.New("stale"),
	}
	d := NewDownloader(&DownloaderConfig{FetchManagers: []fetcher.FetcherManager{manager}})
	if err := d.Setup(); err != nil {
		t.Fatal(err)
	}
	defer d.Clear()

	id, err := d.CreateDirect(&base.Request{URL: "generation://y"},
		&base.Options{Path: t.TempDir(), Name: "y.bin"})
	if err != nil {
		t.Fatal(err)
	}
	<-manager.resolveStarted            // 等 Resolve 真正开始
	pauseDone := make(chan error, 1)
	go func() { pauseDone <- d.Pause(&TaskFilter{IDs: []string{id}}) }()
	// 轮询等待状态变 pause（参考 downloader_test.go:671-677 的写法）
	<-pauseDone
	close(release)                      // 放行陈旧的 Resolve 失败
	time.Sleep(200 * time.Millisecond)  // 给过期协程机会作恶
	if got := d.taskStatus(d.GetTask(id)); got != base.DownloadStatusPause {
		t.Fatalf("stale work overwrote pause: %s", got)
	}
}
```

运行：`go test ./pkg/download/ -run TestPractice_PauseWinsOverStaleWork -v`。然后对照官方测试 `TestDownloader_PauseWinsAgainstStaleResolveError` 补全事件断言（订阅 `EventKeyError`，断言 100ms 内不触发）。

**需要观察的现象**：`close(release)` 之后任务状态**仍是 `pause`**，没有变成 `error`；错误事件不发射。这正是 handler 尾部 `taskIsRunningGeneration` 核对失败后静默 return 的效果。

**预期结果**：测试通过。若想观察「没有防护会怎样」，可以在本地实验性地删掉 `doStart` handler 中两处代数核对再跑官方的 `TestDownloader_StaleStartGenerationCannotRestartTask`（记得改回来）——它会因 `starts != 1` 失败。改动效果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`preparePause` 已经把状态置为 `pause` 了，`runPauseHandler` 为什么还要再核对一次代数？

**答案**：两段之间会释放 `statusLock` 并去抢 `task.lock`，这个间隙里一个完整的 `Continue` 可能已经发生（置 running、代数再 ++，甚至新的启动 handler 已经在跑）。若不核对，本次暂停会 `fetcher.Pause()` 一个刚被重新启动的下载、并把 pause 快照覆盖新状态。核对失败即放弃，让位给最新一代。

**练习 2**：`taskIsPausedGeneration` 为什么接受 `wait` 状态，而 `taskIsRunningGeneration` 只接受 `running`？

**答案**：`doPauseForScheduling` 在 `runPauseHandler` 成功后会把 pause 翻转为 wait（入队），如果之后同一代暂停的落盘动作还要核对（或测试在翻转后核对），接受 wait 才不会误判失败。而「运行代数」语义上只对应 running——wait 意味着已让出执行权，不存在「仍在运行的过期启动」。

**练习 3**：`runGeneration` 为什么不持久化、重启后归零也安全？

**答案**：代数只用于**同一进程内**区分「同一任务的第 N 次状态迁移」，进程关闭后所有相关协程（watch、启动 handler）都不复存在，不存在跨进程的过期协程，归零即安全。它属于 u2-l2 说的「运行时层字段」。

### 4.4 doDelete 与删除失败恢复：deletingTasks 标记

#### 4.4.1 概念说明

删除是唯一会让任务**从系统里消失**的操作，也是最难写对的：它要删内存（`d.tasks`、`d.waitTasks`）、删持久化（三个存储桶）、关资源（fetcher、Blob 租约）、可选删文件（`force`）、还可能触发空组清理——任何一步都可能失败，而前几步可能已经生效。

本版本的 `Delete` 采用「**先摘除、再执行、失败回滚**」的结构：

1. **临界区内一次摘除**：持 `d.lock` 把目标任务从 `d.tasks` 与 `d.waitTasks` 中移出、登记进 `d.deletingTasks`（「删除中」标记），并收集涉及的组 ID；
2. **锁外逐个 `doDelete`**：删桶、关 fetcher、删文件；
3. **失败恢复**：某个任务删除失败 → 把它**重新写回 `bucketTask`**（持久化恢复）、追加回 `d.tasks`（必要时回到 `d.waitTasks` 队尾）；
4. **收尾**：清掉 `deletingTasks` 标记、清理空组、`notifyRunning` 补位。

`deletingTasks` 标记的作用是让**并发的其他删除/清理路径**知道「这个任务正在被删除」：`cleanupTaskGroups` 会跳过仍被删除中任务引用的组，防止组元数据先于成员删除而被误清（然后成员删除又失败，组就永久丢了）。`creatingGroups` 则反向保护：批量创建分组进行中时，`Delete` 直接报错「task group is being created」。

#### 4.4.2 核心流程

```text
Delete(filter, force)
  持 d.lock：
    收集目标组（空过滤器→全部组；ID 过滤器→直接命中的组）
    ID 过滤器：copyFilter（不污染调用方！）→ 展开组 ID
      展开为空但有命中组 → 哨兵 ID []string{""}（防止「空 IDs=全部任务」语义反转）
    任一目标组 ∈ creatingGroups → 报错退出
    tasks = getTasksByFilter(copyFilter)
    每个任务：deletingTasks[ID] = task；记 wasQueued；从 d.tasks / d.waitTasks 摘除
    groupIDs ∪= 各任务的 ParentID
  释放 d.lock：
    for task in tasks: doDelete(task, force)
      失败 → storage.Put(bucketTask, task.ID, task.clone())  ← 持久化恢复
             持 d.lock：d.tasks 追加回；wasQueued → waitTasks 队尾追加回
  持 d.lock：清 deletingTasks 标记
  force=false 时 groups 置 nil（非强删不删文件）
  cleanupTaskGroups(groupIDs, forceGroups)
    候选组 = groupIDs - 存活任务的 ParentID - 删除中任务的 ParentID - 创建中组
    force → os.RemoveAll(组目录)；否则 removeEmptyGroupDirectories（只删空目录树）
    storage.Delete(bucketTaskGroup, id)；从 d.groups 移除
  notifyRunning()
```

`doDelete` 内部的删除顺序（决定失败后丢了什么）：

```text
bucketTask → bucketSave → bucketTaskStats → fetcher.Close → (force) 删文件 → emit(EventKeyDelete)
```

任何一步失败即中断返回；`Delete` 的恢复路径只回写 `bucketTask`——因此失败点越靠后，已丢失的辅助数据越多（这是有意的取舍：`bucketTask` 是任务的权威记录，恢复它就恢复了任务本身）。

#### 4.4.3 源码精读

`Delete` 主体的摘除段：

[pkg/download/downloader.go:L1156-L1186](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1156-L1186) —— 注意三个细节：①`copyFilter := *filter` 浅拷贝再改 `IDs`，不动调用方的过滤器（对比 `Pause` 的直接改写）；②展开为空但命中了组时，`copyFilter.IDs = []string{""}`——因为 `getTasksByFilter` 对空 ID 列表会匹配**全部**任务，哨兵保证「删空组」不会误删所有任务；③`creatingGroups` 守卫在锁内快速失败。

摘除与登记：

[pkg/download/downloader.go:L1187-L1218](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1187-L1218) —— `deletingTasks[task.ID] = task` 登记；`deleted` 集合驱动两个原地过滤（`d.tasks[:0]`、`d.waitTasks[:0]` 复用底层数组）完成摘除；`wasQueued` 记录被删任务是否在排队，供失败恢复。

失败恢复段：

[pkg/download/downloader.go:L1219-L1244](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1219-L1244) —— L1222-L1223 的注释「Deletion can fail after removing bucketTask. Keep recovery durable.」点明回写 `bucketTask` 的动机；恢复动作含两层容错：回写本身失败（`saveErr`）也记录为 firstErr；任务追加回 `d.tasks`（在锁内）与 `d.waitTasks`（仅当原来在队列）。最后统一清理 `deletingTasks` 标记。

空组清理与 force 语义：

[pkg/download/downloader.go:L1245-L1253](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1245-L1253) —— `force=false` 时把 `groups` 置 nil，即「非强删不整目录删除」；随后 `cleanupTaskGroups` + `notifyRunning` 收尾。

[pkg/download/downloader.go:L1262-L1304](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1262-L1304) —— `cleanupTaskGroups` 的候选集合运算：`groupIDs` 减去「存活任务的 ParentID」减去「删除中任务的 ParentID」（L1272-L1274——这就是 `deletingTasks` 的防御价值），再跳过创建中的组；force 组做 `os.RemoveAll`，普通组做 `removeEmptyGroupDirectories`；最后删 `bucketTaskGroup` 元数据并从 `d.groups` 移除。

[pkg/download/downloader.go:L1307-L1325](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1307-L1325) —— `removeEmptyGroupDirectories` 递归后序删除空目录，保留仍有文件（或符号链接，用 `Lstat` 判断）的子树——非强删时绝不碰用户数据。

单任务删除的物理动作：

[pkg/download/downloader.go:L1520-L1558](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1520-L1558) —— `doDelete` 先 `defer d.releaseBlobTask(task)`（Blob 租约必须无条件释放，见 u9-l4），随后按「task 桶 → save 桶 → stats 桶 → fetcher.Close → force 删文件 → emit delete 事件」的顺序执行；成功后 `task = nil`（阻断对已完成删除的任务误用），失败则保留 task 供日志输出 `task.ID`。force 删文件分两种：资源有根名（文件夹）用 `os.RemoveAll`，单文件用 `util.SafeRemove`。

删除失败的官方复现测试（本讲实践的范本）：

[pkg/download/downloader_test.go:L4387-L4401](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4387-L4401) —— `groupFailureConcurrentDeleteStorage`：包装 `MemStorage`，在 `Delete(bucketTask, blockedID)` 时注入错误——这正是「存储报错」的模拟手法。

[pkg/download/downloader_test.go:L4479-L4501](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4479-L4501) —— `TestDownloader_TaskGroupDeleteFailureRestoresQueue`：分组批量建 2 个任务（MaxRunning=1），注入其中一个的桶删除失败，断言 `Delete` 返回错误**且** `waitTasks` 里仍有 1 个成员（恢复路径生效）；再删掉另一个，断言恢复的任务最终被调度为 running——完整验证「失败 → 回滚 → 重新排队 → 正常调度」。

顺带一提，优雅关闭也复用了 `pauseAll`：[pkg/download/downloader.go:L1575-L1584](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1575-L1584) —— `Close` 把 `d.pauseAll` 排在关闭序列第一位，先清空队列并暂停全部任务，再关协议管理器与存储（详见 u11-l2）。

#### 4.4.4 代码实践

**实践目标**：模拟 `doDelete` 中途存储报错，验证任务被重新写回 `bucketTask` 与 `d.tasks` 的恢复路径。

**操作步骤**：仿照官方测试，用一个会「选择性失败」的包装存储。在练习测试文件中追加：

```go
// 示例代码：读者练习用
type failOnceStorage struct {
	Storage                  // 嵌入真实存储（MemStorage 或 BoltStorage）
	failDeleteTaskID string  // 对这个任务 ID 的 bucketTask 删除报错
}

func (s *failOnceStorage) Delete(bucket, key string) error {
	if bucket == bucketTask && key == s.failDeleteTaskID {
		return errors.New("injected storage failure")
	}
	return s.Storage.Delete(bucket, key)
}

func TestPractice_DeleteFailureRestores(t *testing.T) {
	inner := NewMemStorage()
	storage := &failOnceStorage{Storage: inner}
	d := newTaskGroupDownloader(t, storage, 0) // MaxRunning=0，全部排队
	result, err := d.CreateDirectBatchWithResult(taskGroupBatch(t.TempDir(), 2))
	if err != nil {
		t.Fatal(err)
	}
	victim := result.TaskIDs[0]
	storage.failDeleteTaskID = victim

	if err := d.Delete(&TaskFilter{IDs: []string{victim}}, false); err == nil {
		t.Fatal("expected deletion failure")
	}
	// 断言 1：任务回到内存列表
	if d.GetTask(victim) == nil {
		t.Fatal("failed deletion lost the task in memory")
	}
	// 断言 2：任务回到持久化（bucketTask 桶里能读回）
	var restored bool
	d.storage.List(bucketTask, &[]*Task{}) // List 语义见 storage.go，读者可改用计数断言
	restored = d.GetTask(victim) != nil
	if !restored {
		t.Fatal("task not restored")
	}
	// 断言 3（进阶）：用 inner.List(bucketTask, &[]*Task{}) 统计数量 == 2
}
```

运行：`go test ./pkg/download/ -run TestPractice_DeleteFailureRestores -v`。更严格的持久化断言建议直接模仿官方测试 `TestDownloader_TaskGroupDeleteFailureRestoresQueue`（检查 `waitTasks` 长度与后续调度），它是最完整的范本。

**需要观察的现象**：`Delete` 返回非 nil 错误；但 `GetTask(victim)` 找得到任务（回到 `d.tasks`）；另一个未受影响的任务删除正常；用 `MemStorage` 的 `List` 可以看到 victim 仍在 `bucketTask` 桶中（恢复写回成功）。

**预期结果**：三条断言全部通过；日志中出现 `delete task failed, task id: ...`（来自 `doDelete` 的错误日志）。若把存储换成 `NewBoltStorage(t.TempDir())` 再跑，行为一致——恢复路径与具体存储实现无关（接口见 [pkg/download/storage.go:L13-L23](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/storage.go#L13-L23)）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Delete` 需要哨兵 ID `[]string{""}`？

**答案**：`getTasksByFilter` 对「空 ID 列表」的解释是「匹配全部任务」。当过滤器只含组 ID 且展开后为空（比如组内无成员，但组本身存在）时，若不填哨兵，`Delete` 会误删**所有**任务。哨兵是一个不可能命中任何任务 ID 的占位符，把语义从「全部」钉回「无」。

**练习 2**：删除失败恢复把任务追加回 `d.waitTasks` 的**尾部**而不是原位置，会有什么影响？为什么可以接受？

**答案**：恢复任务失去原有排队位置，会比原本晚启动。可以接受是因为：删除本身是用户显式的「破坏性」操作，失败后任务能存活并重新排队已经是恢复的目标；为极罕见的失败路径维护精确的队列位置插还逻辑，复杂度收益不成比例（FIFO 队列用切片实现，中部插入是 O(n)）。

**练习 3**：`deletingTasks` 与 `creatingGroups` 分别防住哪两个方向的竞态？

**答案**：`creatingGroups` 防「创建方」：批量分组创建尚未落定时，`Delete` 抢先删除会把组目录/元数据删掉，创建完成后写入就会落空或悬空，因此 `Delete` 在锁内检测到即报错。`deletingTasks` 防「删除方」：成员删除在途时，其他清理路径（如 `cleanupTaskGroups`）不能把该组判定为「无成员」而清掉元数据——一旦成员删除失败回滚，组关系必须还在。

### 4.5 markTaskDone 与 markTaskError：终态的唯一守门人

#### 4.5.1 概念说明

任务怎么从 running 变成 `done` 或 `error`？答案不是「谁想写就写」，而是两个专职守门函数：

- `markTaskDone(task) bool`：watch 循环在 `fetcher.Wait()` 返回 nil（下载完成）后调用；
- `markTaskError(task) bool`：错误处理路径（`handleOnError`）在 fetcher 报错后调用。

两者都是「检查-写入」原子段：**只有当前状态是 running 才允许写入终态**，写入同时 `runGeneration++` 使所有在途协程的票据作废。返回值 `bool` 告诉调用方「这次迁移归我」——false 意味着别人（通常是 `preparePause`）已经先一步改写了状态，调用方必须放弃后续所有收尾动作。

这一设计直接回答了本讲开头的问题「暂停与完成同时发生谁赢」：**先抢到 `statusLock` 并通过守卫的那一方赢**；`markTaskDone` 的守卫要求 running，所以 `preparePause` 先到（状态已变 pause）时，watch 的 done 迁移被拒绝、静默退出——一个已经暂停的任务不该被标成 done（文件可能确实下完了，但状态机的权威归属是暂停方；数据进度早已通过检查点落盘，继续任务后会看到已下完的进度）。

#### 4.5.2 核心流程

```text
watch(task)（每任务一个守夜协程，见 u4-l3）
  loop:
    err := fetcher.Wait()
    err != nil:
      状态 ∈ {pause, wait} → return（暂停抢到了，守夜人退场）
      …错误处理（可能触发扩展改 URL 后 continue 重试）…
      最终 handleOnError → markTaskError 守卫
    err == nil（完成）:
      markTaskDone(task) ──false→ return（暂停/删除抢到了）
      true → 快照落盘 bucketTask
             emit(done) → emit(finally)
             notifyRunning()          ← 空位释放，队列下一个顶上
             releaseBlobTask / onDone 钩子 / webhook / script
             …auto-torrent / auto-extract 等后处理…
```

`markTaskDone` 里的速度改写：终态时 `Progress.Speed` 不再是 5 秒滑动窗口值，而是**全程平均速度** \[ \text{Speed} = \frac{\text{TotalSize}}{\max(\text{Used}, 1\text{s})} \]，并把 `Downloaded` 对齐到总大小——给 UI 一个稳定的最终数字。

#### 4.5.3 源码精读

[pkg/download/downloader.go:L2604-L2632](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2604-L2632) —— `markTaskDone`：同时持有 `task.lock` 与 `statusLock`（先 lock 后 statusLock，与 `doStart`/`runPauseHandler` 的获取顺序一致，避免死锁）；守卫 `Status != running → false`；随后填 `Used`、兜底 `Res.Size`（未知大小资源用实际下载量补齐）、计算全程平均速度、清空生产者状态、`updateStatus(done)` 并 `runGeneration++`。

[pkg/download/downloader.go:L2634-L2646](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2634-L2646) —— `markTaskError`：更轻量，只抢 `statusLock`，同样以 running 为唯一准入，置 error 并代数自增。

调用方一：watch 的完成分支：

[pkg/download/downloader.go:L1888-L1941](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1888-L1941) —— `Wait()` 出错且状态已是 pause/wait 时直接退场（L1891-L1894）；成功路径先 `markTaskDone`，返回 false 就 return（L1924-L1926）；true 才落快照、发 done/finally 事件、`notifyRunning` 释放空位、触发 onDone/webhook/script 与 auto-torrent 等后处理。

调用方二：错误处理：

[pkg/download/downloader.go:L2037-L2085](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2037-L2085) —— `handleOnError` 持 `task.lock` 后第一件事就是 `markTaskError`，false 即整段放弃（L2042-L2045）——这保证了「Pause 赢得状态后，陈旧的 fetcher 错误无法把任务改成 error」，与 4.3 的测试互相印证。收尾处若任务确为 error，同样 `notifyRunning`（L2085）释放空位。

状态常量与 DoneAt 的唯一写入门：

[pkg/base/constants.go:L6-L11](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/constants.go#L6-L11) —— 六个状态常量。

[pkg/download/model.go:L175-L184](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L175-L184) —— `updateStatus` 在迁移到 done 时写入 `DoneAt`；由于 `markTaskDone` 是进入 done 的唯一入口且要求 running，「DoneAt 只写一次」由结构保证（u2-l2 的结论在此落地）。

#### 4.5.4 代码实践

**实践目标**：验证「done 任务 DoneAt 只写一次」与「error 后空位被补上」。

**操作步骤**：纯源码阅读 + 运行既有测试：

1. 运行 `go test ./pkg/download/ -run 'TestDownloader_PauseAndContinue|TestDownloader_PauseAllAndContinueAll' -v`（[pkg/download/downloader_test.go:L1913-L1978](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L1913-L1978) 与 [pkg/download/downloader_test.go:L2095-L2182](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L2095-L2182)）。注意第一个测试用的是慢速文件服务器（2 秒/请求），保证暂停发生在传输中途。
2. 在测试输出中确认：暂停→状态 pause→继续→最终 done 的完整断言链。
3. 源码追踪练习：从 `markTaskDone` 向上找三处调用约束——`watch` 的完成分支、`updateStatus` 的 DoneAt 写入、`runGeneration++` 的作废效果，画一张「谁可能调用、谁会被拒绝」的表格。

**需要观察的现象**：两个测试均 PASS；慢速服务器场景下任务在被暂停的时刻确实处于 running（否则 Pause 的过滤器会跳过它）。

**预期结果**：全部通过。若想看「DoneAt 只写一次」的数据面证据，可在练习测试里对 done 任务连续调用两次 `d.Continue(&TaskFilter{IDs: ...})`，第二次因 `NotStatuses` 排除 done 而返回 `ErrTaskNotFound`，`doStart` 根本不会执行——终态无出边（u2-l2）从 API 层到内核双层封死。「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`markTaskDone` 为什么要同时持有 `task.lock` 和 `statusLock`，而 `markTaskError` 只持 `statusLock`？

**答案**：`markTaskDone` 除了改状态还要读写任务正文：`Progress.Used`、`Res.Size` 兜底、`producer` 清理，这些字段被其他持 `task.lock` 的路径（如 `runPauseHandler` 的快照）并发访问，必须用 `task.lock` 互斥；`markTaskError` 只动 `Status` 与 `runGeneration`，这两个字段专属于 `statusLock`。锁粒度最小化也是 `preparePause` 只抢 `statusLock` 的原因。

**练习 2**：一个任务物理上已经下载完毕，但用户在 `fetcher.Wait()` 返回前一刻点了暂停。最终状态是什么？数据丢了吗？

**答案**：最终状态是 `pause`（若 `preparePause` 先完成状态翻转，`markTaskDone` 被守卫拒绝）。数据不丢：进度数据由 fetcher 写入磁盘文件、检查点由 watch 周期与暂停路径落盘（`runPauseHandler` 会 `saveTask`）；用户继续任务后，协议实现（如 HTTP 的 Range 续传）会发现文件已完整并立即完成。

**练习 3**：`handleOnError` 里 `markTaskError` 返回 false 时，为什么连扩展的 onError 钩子都不该执行？

**答案**：返回 false 说明任务已不处于 running（典型：被暂停）。onError 钩子带有「恢复」语义（扩展可改写 URL 并让任务继续，见 u9-l3），对一个用户明确暂停的任务执行「错误恢复」会违背用户意图；整段放弃让暂停语义保持完整。

## 5. 综合实践

把本讲四个模块串成一个「调度观察台」小项目。在 `pkg/download/` 下写一个练习测试文件（全部基于仓库既有的 `generationTestManager` 工具，无需网络）：

1. **场景搭建**（4.2）：`MaxRunning=2`，一次性创建 8 个 `generation://` 任务（`holdOpen: true`），用轮询记录每个任务从创建到稳定的状态时间线，画出「2 running + 6 wait」的调度表。
2. **腾位实验**（4.2）：`Continue` 一个队尾任务，验证腾位算法：哪个 running 任务被 `doPauseForScheduling` 压回队尾、目标任务是否立即 running；对照 `TestDownloader_SchedulingPauseCompletesBeforeReplacementStart` 的断言方式。
3. **组暂停实验**（4.1）：改用 `CreateDirectBatchWithResult` 建一个 3 成员的分组，用返回的 `parentId` 一次 `Pause`，断言 3 个成员同时 pause 且 wait 队列同步清空（检查 `len(d.waitTasks)`）。
4. **竞态复现**（4.3）：实现 4.3.4 的「Pause 赢过陈旧 Resolve」测试，并额外断言 `EventKeyError` 不触发。
5. **删除恢复**（4.4）：实现 4.4.4 的存储故障注入，验证失败任务回到 `d.tasks`、`bucketTask` 与 wait 队列，且组元数据因 `deletingTasks` 保护而未消失（用 `d.groups` 长度断言）。

完成标志：一张手绘的完整状态-事件时序图（8 任务 × 暂停/继续/删除三次扰动），加上 5 个全绿的练习测试。若时间有限，优先做 2、4、5——它们分别对应本讲最核心的三个机制：腾位调度、代数防护、删除恢复。

## 6. 本讲小结

- **过滤批量模型**：`Pause`/`Continue`/`Delete` 共享「空过滤器=全量 → 组 ID 经 `expandGroupTaskIDs` 展开 → 状态黑名单过滤 → 逐个执行」的骨架；`Delete` 额外拷贝过滤器并用哨兵 ID 防「空 IDs=全部任务」的语义反转。
- **wait 队列与腾位**：`MaxRunning` 由「闸门（remainRunningCount）+ FIFO 队列（waitTasks）+ 唤醒者（notifyRunning）」实现；`Pause` 先持锁清队列再物理暂停；`Continue` 空位不足时用 `doPauseForScheduling` 把 running 任务物理暂停（不 emit）并压回队尾腾位。
- **runGeneration 代数**：每次成功状态迁移自增的计数器，是所有异步协程的「票据编号」；`doStart` handler 的两次核对、`runPauseHandler` 的暂停代数核对、`taskIsRunningGeneration`/`taskIsPausedGeneration` 原语，共同保证过期协程只能静默放弃。
- **两段式暂停**：`preparePause`（statusLock 内原子声明 pause + 代数自增）与 `runPauseHandler`（task.lock 内做 fetcher 暂停、统计与落盘）分离，让「谁赢得暂停」快速定论、慢动作可被更新一代取代。
- **删除三段式**：锁内一次摘除并登记 `deletingTasks` → 锁外 `doDelete`（task→save→stats 桶 → 关 fetcher → force 删文件）→ 失败回滚（回写 bucketTask、追加回 d.tasks/waitTasks）；`creatingGroups` 与 `deletingTasks` 双向标记守住分组创建/删除的竞态，`cleanupTaskGroups` 只清理真正无成员的组。
- **终态守门**：`markTaskDone`/`markTaskError` 以 running 为唯一准入并自增代数，使「暂停 vs 完成」「暂停 vs 出错」都有一致的裁决规则——先到者赢，后到者静默退场。

## 7. 下一步学习建议

- **u4-l5 持久化与断点恢复**：本讲多次出现 `saveTask`/`bucketSave`，下一讲系统讲解九个存储桶的布局与 `Store`/`Restore` 两段式契约——暂停的持久化语义在那里闭环。
- **u4-l6 任务分组与批量创建**：本讲只消费了组 ID 展开与空组清理，下一讲从创建侧完整讲解 `TaskGroup` 模型、`CreateDirectBatchWithResult` 的部分成功语义与组目录预留。
- **u11-l2 并发控制与优雅关闭**：本讲聚焦任务级竞态，收官讲系统梳理 Downloader 的完整锁清单与 `Close` 的排水时序（本讲已见到 `Close` 先 `pauseAll` 的伏笔）。
- **动手方向**：给 `ContinueBatch` 这个「未接线」的预留接口找一个合理用途（比如扩展批量恢复失败任务），或者为本讲补一个「删除与完成同时发生」的测试——观察 `watch` 里 `d.GetTask(task.ID) == nil` 的判断（L1911）如何让守夜协程在任务被删后退场。
