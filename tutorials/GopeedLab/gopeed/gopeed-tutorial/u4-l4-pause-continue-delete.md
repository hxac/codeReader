# u4-l4 暂停、继续、删除与最大并发调度

## 1. 本讲目标

前两讲（u4-l2、u4-l3）我们走通了「建任务 → 启动 → watch 收尾」的正向链路。但一个真实的下载器还必须回答四个反向问题：

- 用户点「暂停」，正在跑的 goroutine 怎么被安全地按住？
- 用户点「继续」，而 `MaxRunning` 名额已满，调度器怎么腾位置？
- 用户点「删除」，任务怎么从列表、队列、磁盘三处一起消失？
- 「暂停」和「下载完成」恰好同时发生时，谁来保证任务不会既不是 pause 又不是 done？

学完本讲，你应该能够：

1. 解释 `TaskFilter` 的批量操作模型：`Pause`/`Continue`/`Delete` 三个入口如何用同一个过滤器选出目标任务集合。
2. 理解 `waitTasks` 等待队列与 `notifyRunning` 的唤醒机制，以及 `Continue` 在 `MaxRunning` 满载时的「腾挪算法」。
3. 掌握 `runGeneration` 代数机制：为什么 `doStart` 的异步 handler 需要「三次核对」，`markTaskDone`/`markTaskError` 的 running 守卫如何防止旧协程覆盖新状态。
4. 会写测试复现「暂停与完成同时发生」的竞态，并亲手观察 8 个任务在 `MaxRunning=2` 下的排队与切换时序。

## 2. 前置知识

本讲是第四单元最「并发」的一讲，先把要用的旧知识对齐，再补两个新概念。

**旧知识速查：**

- **Task 状态机**（u2-l2）：六种状态 `ready/wait/running/pause/error/done`，定义在 [pkg/base/constants.go:L5-L12](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/constants.go#L5-L12)。其中 `wait` 是**纯调度概念**（任务没问题，只是没名额），`done` 是唯一终态；**状态的唯一写入者是 Downloader**，Fetcher 只能通过 `Wait()` 的返回值「汇报」。
- **doStart 两段式**（u4-l2）：同步段在 `statusMut`（拿 `task.statusLock`）里改状态、递增 `runGeneration`；真正的 `fetcher.Start()` 在异步 handler goroutine 里执行。这个「同步改状态、异步做 IO」的结构是本讲全部竞态的来源。
- **watch 守夜人**（u4-l3）：每个运行中任务一个 watch goroutine，阻塞在 `fetcher.Wait()`；返回 `nil` 走 `markTaskDone`，返回错误走 `handleOnError` → `markTaskError`。
- **锁的层次**（u4-l1）：任务列表与 `waitTasks` 归主锁 `d.lock`；任务状态归 `task.statusLock`；任务整体（含 fetcher 操作）归 `task.lock`；拿锁顺序永远是「先 `d.lock` 后任务锁」，反过来拿会死锁。
- **MaxRunning**：最大同时运行任务数，配置字段在 [pkg/base/model.go:L217](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L217)，默认值 5 由 `Init()` 补齐（[pkg/base/model.go:L234-L235](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L234-L235)）。

**新概念一：批量操作的「过滤器」模型。**

`Pause`/`Continue`/`Delete` 的入参不是任务 ID，而是一个 `TaskFilter`——用「ID 集合 + 状态白名单 + 状态黑名单」三个维度描述「我要操作哪些任务」。空过滤器等价于「全部任务」。这套模型让 REST API 能用一行查询参数表达「暂停所有 running 任务」这样的批量意图。

**新概念二：代数（generation）防竞态。**

`Task` 里有一个不序列化的字段 `runGeneration`（[pkg/download/model.go:L71](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L71)）。它是一个单调递增计数器：**任务每次状态被「重启类」操作改写（进入 running、被暂停、被标记 done/error）时代数 +1**。每个异步协程在动手前记下当时的代数，写回前核对「任务当前代数是否还等于我记下的那个」——不相等就说明「我已经不是本轮运行的负责人了」，主动放弃。这相当于给每个并发协程发了一张一次性门票。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [pkg/download/downloader.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go) | 下载引擎中枢 | `Pause`/`Continue`/`Delete`、`notifyRunning`、`remainRunningCount`、`doPauseForScheduling`、`preparePause`/`runPauseHandler`、`markTaskDone`/`markTaskError`、`doStart` 的代数核对 |
| [pkg/download/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go) | Task/配置模型 | `TaskFilter` 结构、`runGeneration` 字段、`updateStatus` |
| [pkg/base/constants.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/constants.go) | 状态常量 | 六种 `Status` |
| [pkg/api/service.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/api/service.go) | REST 服务层 | 过滤器如何从 HTTP 查询参数构造 |
| [pkg/download/downloader_test.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go) | 引擎测试 | `generationTestManager` 测试替身与三个官方竞态测试 |

本讲 95% 的内容在 `downloader.go` 一个文件里，但跨越了它的三个区域：对外操作入口（L760-L989）、任务收尾区（L1517-L1747）、状态写入核心区（L2001-L2278）。建议按这个顺序读。

## 4. 核心概念与源码讲解

四个最小模块的关系：

> **4.1 批量入口**用 `TaskFilter` 选出任务集合；**4.2 调度器**决定谁 running、谁 wait（`Continue` 满载时用 `doPauseForScheduling` 腾位置）；**4.3 代数机制**保证「同步改状态、异步做 IO」结构下旧协程不覆盖新状态；**4.4 终态守门人** `markTaskDone`/`markTaskError` 用 running 守卫 + 代数递增把 done/error 变成一次性写入。

### 4.1 Pause/Continue/Delete：TaskFilter 的批量操作模型

#### 4.1.1 概念说明

`TaskFilter` 是三个批量操作的统一「选人」语言：

```go
type TaskFilter struct {
	IDs         []string      // 命中任一 ID
	Statuses    []base.Status // 状态白名单：命中任一状态
	NotStatuses []base.Status // 状态黑名单：命中任一状态即排除
}
```

三个条件是 **AND** 关系；同一条件内部是 **OR**。空切片表示「不限制」，全空（`IsEmpty()`）表示「全部任务」。

值得注意的是 `Pause` 和 `Continue` 会在过滤器上**追加自己的默认黑名单**：`Pause` 排除 `pause/error/done`（暂停一个已暂停的任务没有意义），`Continue` 排除 `running/done`（继续一个运行中的任务也没有意义）。这意味着 API 层不必传状态参数，语义已经内建在核心层。

`Delete` 则不做状态排除——任何状态的任务都可以删除，并额外带一个 `force` 参数决定是否连磁盘上的文件一起删。

#### 4.1.2 核心流程

三个入口的统一形态是「**先选人，再逐个执行，最后唤醒队列**」：

```text
Pause(filter)
  ├─ filter 为空？→ pauseAll()：清空 waitTasks 队列 + 逐个 doPause
  ├─ 注入 NotStatuses = {pause, error, done}
  ├─ GetTasksByFilter(filter) 选出 pauseTasks（空则返回 ErrTaskNotFound）
  ├─ for 每个 task → doPause(task)
  └─ notifyRunning()            ← 暂停腾出了名额，唤醒队首

Continue(filter)   （详见 4.2）
  ├─ filter 为空？→ continueAll()
  ├─ 注入 NotStatuses = {running, done}
  └─ 调度算法：可能先暂停别人，再启动选中的任务

Delete(filter, force)
  ├─ filter 为空？→ deleteAll()
  ├─ GetTasksByFilter 选出 deleteTasks（空则静默返回 nil，注意与 Pause/Continue 不同）
  ├─ 【d.lock 内】从 d.tasks 和 d.waitTasks 里移除这些任务
  ├─ 【锁外】逐个 doDelete：删三个存储桶 → fetcher.Close → force 则删文件 → emit(delete)
  └─ notifyRunning()            ← 删除也腾出了名额
```

#### 4.1.3 源码精读

**过滤器本体**：

[pkg/download/model.go:L201-L209](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L201-L209) —— `TaskFilter` 三个字段与 `IsEmpty()`。匹配算法在 [pkg/download/downloader.go:L1333-L1382](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1333-L1382) 的 `GetTasksByFilter`：三个闭包 `idMatch`/`statusMatch`/`notStatusMatch` 依次 AND，遍历 `d.tasks`（持 `d.lock`）收集结果。注意它拿的是**任务列表的快照引用**，匹配基于当时的 `task.Status` 快照——后面逐个 `doPause` 时状态可能又变了，所以每个执行函数内部还有第二道状态守卫（见 4.3）。

**Pause 入口**：

[pkg/download/downloader.go:L760-L779](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L760-L779) —— 注意三个细节：① 空过滤器走 `pauseAll`；② `filter.NotStatuses = ...` 是对**调用方过滤器对象的直接改写**（副作用）；③ 循环结束后调用 `notifyRunning()`——暂停一个 running 任务等于释放一个名额，队列里的候补任务应立即顶上。

**pauseAll**：

[pkg/download/downloader.go:L781-L797](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L781-L797) —— 先在 `d.lock` 内把 `waitTasks` 整体清空（`d.waitTasks[:0]`，复用底层数组），再对每个任务 `doPause`。**清队列是关键**：批量暂停后不允许任何候补再被拉起。对比一下——带过滤器的 `Pause` **不会**动 `waitTasks`，这个差异会在 4.1.5 的练习里考察。

**Delete 入口与 doDelete**：

[pkg/download/downloader.go:L923-L968](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L923-L968) —— `Delete` 的两段式：先在 `d.lock` 内把任务从 `d.tasks` 与 `d.waitTasks` 两个列表里摘除（对 `waitTasks` 是 O(n) 的切片拼接删除），然后在**锁外**逐个 `doDelete`。存储 IO 和文件删除放锁外，是为了不让慢速磁盘操作阻塞整个任务列表。

[pkg/download/downloader.go:L1184-L1222](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1184-L1222) —— `doDelete` 的清理清单：`defer releaseBlobTask` 释放 Blob 引用（u9 内容）；依次删除 `bucketTask`/`bucketSave`/`bucketTaskStats` 三个存储桶中该任务的键（u4-l1 讲过删任务三桶同删）；若 fetcher 已加载则 `fetcher.Close()`（注意：**删除直接 Close，不先 Pause**）；`force=true` 时按资源形态删整个目录（`Res.Name != ""` 是文件夹资源）或单个文件；最后 `emit(EventKeyDelete)`。

**API 层如何构造过滤器**：

[pkg/api/service.go:L57-L62](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/api/service.go#L57-L62) —— REST 路由为每个操作提供「单任务」和「批量」两种形态：`PUT /api/v1/tasks/{id}/pause` 与 `PUT /api/v1/tasks/pause`。

[pkg/api/service.go:L208-L237](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/api/service.go#L208-L237) —— 单任务版本只是把路径参数包成 `TaskFilter{IDs: []string{taskID}}` 再调同一个 Downloader 方法；批量版本由 [pkg/api/service.go:L450-L459](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/api/service.go#L450-L459) 的 `taskFilterFromQuery` 从查询参数 `id`/`status`/`notStatus`（均可重复）构造。**桥接层不知道批量语义，语义完全收敛在核心层**——这正是 u1-l1 讲的「桥接小而稳定」原则的又一次体现。

#### 4.1.4 代码实践：用 curl 体验批量过滤器

**实践目标**：验证 `TaskFilter` 的三个维度（`id`/`status`/`notStatus`）在 REST 层的真实行为。

**操作步骤**（需要 u1-l2 构建的服务端，如 `go run ./cmd/api` 或 docker 启动的 Web 版；假设监听 `localhost:9999`，token 为空）：

```bash
# 1. 建两个慢速任务（找一个限速的大文件 URL，或重复用同一个）
curl -X POST localhost:9999/api/v1/tasks -H 'Content-Type: application/json' \
  -d '{"req":{"url":"https://example.com/big-file.iso"}}'
curl -X POST localhost:9999/api/v1/tasks -H 'Content-Type: application/json' \
  -d '{"req":{"url":"https://example.com/another-file.zip"}}'

# 2. 批量暂停所有 running 任务（状态白名单）
curl -X PUT 'localhost:9999/api/v1/tasks/pause?status=running'

# 3. 批量继续所有 pause 任务
curl -X PUT 'localhost:9999/api/v1/tasks/continue?status=pause'

# 4. 用黑名单表达同一意图：notStatus=done & notStatus=error
curl -X PUT 'localhost:9999/api/v1/tasks/pause?notStatus=done&notStatus=error&notStatus=pause'

# 5. 查看任务列表确认状态
curl 'localhost:9999/api/v1/tasks'

# 6. 单任务删除（force=true 连文件一起删）
curl -X DELETE 'localhost:9999/api/v1/tasks/{taskId}?force=true'
```

**需要观察的现象**：第 2 步后两个任务都变 `pause`；第 3 步后变回 `running`（若 `MaxRunning` 足够）；第 4 步与第 2 步效果相同（因为核心层还会再注入自己的黑名单）。

**预期结果**：批量接口与单任务接口最终调用的都是 `Downloader.Pause/Continue/Delete`，行为一致；删除后任务从列表消失，`force=true` 时下载目录中的文件也被移除。

**待本地验证**：本沙箱环境无法启动服务，以上命令未实际运行；尤其第 4 步与第 2 步的等价性请以实际响应为准。

#### 4.1.5 小练习与答案

**练习 1**：`Pause(nil)` 和 `Pause(&TaskFilter{IDs: []string{id}})` 对一个正在 `wait` 排队的任务分别有什么效果？

**参考答案**：`Pause(nil)` 走 `pauseAll`，会先把 `waitTasks` 整队清空再暂停所有任务，该任务最终为 `pause` 且**不在任何队列里**；带过滤器的 `Pause` 不修改 `waitTasks`，该任务被置为 `pause` 但**仍留在队列中**。随后两者都会调 `notifyRunning()`——而 `doStart` 的同步段只拒绝 `running/done`（见 4.3.3），所以「过滤暂停一个 wait 任务后，它可能仍会在名额空出时被调度器重新拉起」。这是代码字面行为，是否构成实际可观察的怪异表现，待本地验证（见 4.3.4 实践的延伸问题）。

**练习 2**：为什么 `Delete` 匹配不到任务时返回 `nil`，而 `Pause`/`Continue` 匹配不到时返回 `ErrTaskNotFound`（[pkg/download/downloader.go:L57](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L57)）？

**参考答案**：删除是幂等的终态操作——「要删的东西已经不在了」等于「删除成功」，返回 nil 让重试和并发删除都安全。暂停/继续则不然：找不到任务通常意味着调用方拿到了过期的任务 ID（比如任务刚被别处删除），报错能把问题暴露给上层 UI。这是 API 设计中「幂等操作宽容、状态操作严格」的典型取舍。

**练习 3**：`GetTasksByFilter` 返回的任务集合，到逐个 `doPause` 执行时还准确吗？

**参考答案**：不一定。选人时读的是状态快照，逐个执行期间其他 goroutine（watch、另一个 API 请求）可能已经改了任务状态。所以批量操作的正确性不靠「选得准」，靠「每个执行函数内部的第二道守卫」：`preparePause` 会检查任务是否已处于 `pause/done/error` 而直接跳过（4.3.3），`doStart` 会检查 `running/done` 而直接返回。过滤器只负责「缩小范围」，不负责「保证」。

### 4.2 wait 队列与 doPauseForScheduling：MaxRunning 的守门与腾挪

#### 4.2.1 概念说明

`MaxRunning` 是一个**软性并发上限**：它不限制 goroutine 数量，只限制同时处于 `running` 状态的任务数。守门的不是某个常驻调度器，而是四个「决策点」上的简单算术：

1. **建任务时**（`doCreate`）：有空位直接 `doStart`，满载则标记 `wait` 进 `waitTasks` FIFO 队列。
2. **名额释放时**（`notifyRunning`）：完成、出错、暂停、删除都会唤醒队首。
3. **用户点名继续时**（`Continue`）：这是最复杂的一种——用户说「我要这几个任务现在跑」，如果没有空位，调度器要**现场暂停别的 running 任务腾位置**。
4. **批量继续全部时**（`continueAll`）：能启动的启动，多余的排队。

`doPauseForScheduling` 就是第 3 种场景的专用武器：**物理暂停一个 running 任务并原子地把它变成 wait 状态塞回队列**。它与普通 `doPause` 的区别在于：不发事件（调用方持 `d.lock`，事件必须延后到锁外发）、成功后把状态从 `pause` 翻转成 `wait`（被腾挪的任务不是「被用户暂停」，而是「还在排队」）。

一个重要的架构事实：**`doStart` 本身不检查 `MaxRunning`**。名额仲裁只发生在上述入队决策点；凡是走到 `doStart` 的任务，都是被决策点「授权越过闸门」的。

#### 4.2.2 核心流程

先看两个基础设施：

```text
remainRunningCount():
    遍历 d.tasks 数 running 状态的任务数 → MaxRunning - runningCount
    （空闲名额数；running 超过 MaxRunning 时可能为负）

notifyRunning():                        // 异步！go func 起 goroutine
    持 d.lock →
      空闲名额 == 0？→ 返回
      waitTasks 非空？→ 弹出队首 wt → doStart(wt)   // FIFO，一次只拉一个
```

再看 `Continue` 的腾挪算法（全称在 4.2.3 精读）。设 \( M \) 为 `MaxRunning`、\( C \) 为本次要继续的任务数、\( R \) 为当前 running 数、空闲名额 \( F = M - R \)，则：

\[ \text{needRunningCount} = \min(M,\ C) \]
\[ \text{needPauseCount} = \max\bigl(0,\ \min(M, C) - (M - R)\bigr) \]

即「本次最多实际启动 \( \min(M, C) \) 个，不够的名额靠现场暂停别的任务来凑」。整个决策在一次 `d.lock` 持有中完成：

```text
Continue(filter)  【d.lock 内】
  ├─ 计算 needPauseCount > 0？
  │    └─ 按 d.tasks 创建顺序扫描 running 任务：
  │         doPauseForScheduling(task)
  │           ├─ preparePause：状态→pause、代数++（同 doPause）
  │           ├─ runPauseHandler：物理 fetcher.Pause() + 落盘
  │           └─ 【statusLock 内】状态仍是 pause 且代数未变？
  │                是 → 翻转为 wait，返回 queued=true → 调用方 append 到 waitTasks 队尾
  │                否 → 中途有人插足（完成/出错/重启），放弃入队
  │         直到腾够 needPauseCount 个
  ├─ 遍历 continueTasks：前 needRunningCount 个进 realContinueTasks
  │    其余 → 状态直写 wait + append 到 waitTasks
  ├─ 【释放 d.lock 后】给被腾挪的任务补发 pause 事件
  └─ 逐个 doStart(realContinueTasks)
```

一个具体例子：`MaxRunning=3`，当前 running = {A, B, C}，用户 `Continue` 点名 {D, E}。\( F = 0 \)，`needRunningCount` \( = \min(3,2) = 2 \)，`needPauseCount` \( = 2 \)。调度器按创建顺序暂停 A、B（物理 `fetcher.Pause`、翻回 wait、排到队尾），启动 D、E。最终 running = {C, D, E}，`waitTasks` = [A, B]。A、B 的 `pause` 事件在锁释放后补发。

#### 4.2.3 源码精读

**名额计算与唤醒**：

[pkg/download/downloader.go:L595-L603](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L595-L603) —— `remainRunningCount` 用「实时数一遍 running」而不是维护计数器，简单且不会漂移；代价是 O(任务数)，但任务列表通常很短。

[pkg/download/downloader.go:L578-L593](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L578-L593) —— `notifyRunning` 的三个要点：① `go func` 异步执行，调用方（比如 watch 的成功分支）不必等调度完成；② 整个「算名额 → 弹队首 → doStart」持 `d.lock`，多个 `notifyRunning` 并发时串行化，且每次都重算名额，**不会超发**；③ 空闲名额用 `== 0` 判断——若运行时调低 `MaxRunning` 导致名额为负，同样不会启动新任务。调用点共四处：任务完成（[L1595](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1595)）、任务出错（[L1744](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1744)）、`Pause`（[L776](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L776)）、`Delete`（[L966](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L966)）。

**建任务时的入队决策**：

[pkg/download/downloader.go:L1856-L1870](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1856-L1870) —— `doCreate` 在 `d.lock` 内：任务先入 `d.tasks`，再算名额；`remainRunningCount == 0` 时状态直写 `wait` 并 append 进 `waitTasks`（注意 `doStart` 的同步段会立刻把状态改成 running，所以这里的 wait 是「跳过启动」的标记）；否则就地 `doStart`。

**Continue 的腾挪主体**：

[pkg/download/downloader.go:L800-L867](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L800-L867) —— 对照 4.2.2 的伪代码逐行读。几个容易漏看的点：① 被腾挪任务的 `pause` 事件**必须**在 `d.lock` 释放后补发（[L854-L858](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L854-L858) 的注释写明原因：listener 回调可能反过来调用需要 `d.lock` 的 Downloader 方法，锁内 emit 会死锁——这与 u4-l3 讲的「事件必须在锁外 emit」一脉相承）；② 没被选中的多余任务排在**被腾挪任务之后**入队，即队尾顺序是「先腾挪的旧任务，再本次溢出的新任务」；③ `Continue` 的返回错误只来自 `doPauseForScheduling`/`doStart`，选人阶段匹配为空才返回 `ErrTaskNotFound`。

**continueAll 与 ContinueBatch**：

[pkg/download/downloader.go:L870-L897](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L870-L897) —— `continueAll` 不腾挪任何人：按空闲名额启动前 `remainCount` 个非 running/done 任务，其余排队。启动策略是「先到先得」（`d.tasks` 创建顺序）。

[pkg/download/downloader.go:L899-L911](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L899-L911) —— `ContinueBatch` 对每个匹配任务直接 `doStart`，**不做 `MaxRunning` 仲裁**（它本身就是一个「越过闸门」的通道）。值得注意：该方法目前在仓库内没有任何调用方（含测试），属于预留接口——阅读源码时要能识别「存在但未接线」的代码，不要臆测它的用途。

**doPauseForScheduling 的原子翻转**：

[pkg/download/downloader.go:L2139-L2157](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2139-L2157) —— 全函数分三步：`preparePause`（改状态、记代数）→ `runPauseHandler`（物理暂停 + 落盘）→ `statusLock` 内**复核**「状态仍是 pause 且代数等于我记下的那个」，双条件都满足才翻转为 `wait` 并返回 `queued=true`。这个复核就是竞态防护：如果两步之间有别的协程插足（任务恰好完成被标成 done、或被标成 error、或被再次启动），代数必然已经变化，`queued=false`，调用方**不会**把它塞进等待队列——插足者的裁决得以保留。被腾挪任务入队的动作由调用方 `Continue` 完成（[L830-L834](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L830-L834)），保持「队列写操作全在 `d.lock` 内」的纪律。

顺带一个运行期细节：通过 `PutConfig` 调大 `MaxRunning` 并**不会**触发 `notifyRunning`（[pkg/download/downloader.go:L1399-L1459](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1399-L1459) 全函数无此调用），排队任务要等到下一次「名额释放事件」才会被拉起。

#### 4.2.4 代码实践：8 个任务、MaxRunning=2 的排队时序

**实践目标**：亲眼观察 `waitTasks` 的 FIFO 纪律与 `notifyRunning` 的逐个唤醒，画出 wait→running 的切换时序。

**操作步骤**：在**你自己的仓库克隆**里新建 `pkg/download/u4l4_practice_test.go`（与现有测试同包，因此可以直接复用 [pkg/download/downloader_test.go:L33-L44](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L33-L44) 的 `generationTestManager` 测试替身和 [pkg/download/extension_test.go:L2042-L2061](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/extension_test.go#L2042-L2061) 的 `waitForTaskStatus` 辅助函数），写入以下内容（**示例代码**，非仓库原有代码）：

```go
package download

import (
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/GopeedLab/gopeed/internal/fetcher"
	"github.com/GopeedLab/gopeed/pkg/base"
)

// holdOpen=true：fetcher.Start 后不自动完成，何时完成由测试发信号控制
func TestU4L4_PracticeScheduleTimeline(t *testing.T) {
	manager := &generationTestManager{holdOpen: true}
	downloader := NewDownloader(&DownloaderConfig{
		FetchManagers: []fetcher.FetcherManager{manager},
	})
	if err := downloader.Setup(); err != nil {
		t.Fatal(err)
	}
	defer downloader.Clear()
	downloader.cfg.MaxRunning = 2

	// 记录事件时间线（emit 可能来自不同 goroutine，加锁保护）
	var mu sync.Mutex
	timeline := make([]string, 0)
	downloader.Listener(func(event *Event) {
		mu.Lock()
		defer mu.Unlock()
		timeline = append(timeline, fmt.Sprintf("%s %-7s %s",
			time.Now().Format("15:04:05.000"), event.Key, event.Task.ID))
	})

	// 一次性创建 8 个任务
	ids := make([]string, 0, 8)
	for i := 0; i < 8; i++ {
		id, err := downloader.CreateDirect(
			&base.Request{URL: "generation://slot"},
			&base.Options{Path: t.TempDir(), Name: "slot.bin"},
		)
		if err != nil {
			t.Fatal(err)
		}
		ids = append(ids, id)
	}

	// 断言 1：恰好 2 个 running、6 个 wait
	running, waiting := 0, 0
	for _, task := range downloader.GetTasks() {
		switch downloader.taskStatus(task) {
		case base.DownloadStatusRunning:
			running++
		case base.DownloadStatusWait:
			waiting++
		}
	}
	if running != 2 || waiting != 6 {
		t.Fatalf("want 2 running + 6 wait, got %d running + %d wait", running, waiting)
	}

	// 辅助：给第一个 running 任务的 fetcher 发“完成”信号
	signalFirstRunningDone := func() {
		for _, task := range downloader.GetTasks() {
			if downloader.taskStatus(task) == base.DownloadStatusRunning {
				task.fetcher.(*generationTestFetcher).done <- nil
				return
			}
		}
		t.Fatal("no running task to finish")
	}
	// 辅助：等待物理 Start 次数达到 want
	waitStarts := func(want int32) {
		deadline := time.Now().Add(3 * time.Second)
		for manager.starts.Load() != want && time.Now().Before(deadline) {
			time.Sleep(5 * time.Millisecond)
		}
		if got := manager.starts.Load(); got != want {
			t.Fatalf("fetcher.Start called %d times, want %d", got, want)
		}
	}

	// 先等前两个任务物理启动，避免把完成信号发给尚未 Start 的 fetcher
	waitStarts(2)

	// 断言 2：逐个完成任务，候补按 FIFO 顶上（starts: 2→8）
	for finished := 1; finished <= 8; finished++ {
		signalFirstRunningDone()
		if finished <= 6 { // 队列空之前，每完成一个就有候补被拉起
			waitStarts(int32(finished + 2))
		}
	}
	// 断言 3：最终 8 个任务全部 done
	for _, id := range ids {
		waitForTaskStatus(t, downloader, id, base.DownloadStatusDone, 3*time.Second)
	}

	mu.Lock()
	defer mu.Unlock()
	t.Logf("event timeline (%d events):", len(timeline))
	for _, line := range timeline {
		t.Log(line)
	}
}
```

运行：

```bash
go test ./pkg/download/ -run 'TestU4L4_PracticeScheduleTimeline' -v
```

**需要观察的现象**：创建 8 个任务后立即断言 `2 running + 6 wait` 通过；每完成一个 running 任务，`manager.starts` 计数加一（说明候补被 `notifyRunning` 拉起）；最后 8 个任务全部 `done`；时间线里 `start`/`done` 事件严格交替成对出现。

**预期结果**：候补任务被唤醒的顺序与创建顺序一致（`ids[2]`、`ids[3]`、……、`ids[7]` 依次进入 running），验证 `waitTasks` 的 FIFO 语义与「一次唤醒只拉一个」的设计。

**待本地验证**：本沙箱无 Go 工具链，以上测试未实际运行；逻辑模型来自对 `doCreate`/`notifyRunning`/`watch` 的源码推演，与官方测试 `TestDownloader_SchedulingPauseCompletesBeforeReplacementStart`（[pkg/download/downloader_test.go:L697-L765](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L697-L765)）使用同一套替身。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `notifyRunning` 要用 `go func` 异步执行，而不是同步完成调度？

**参考答案**：`notifyRunning` 的调用点都在关键路径上——watch 的收尾分支、`Pause`/`Delete` 的末尾。同步调度意味着这些调用方要等「持 `d.lock` → 算名额 → doStart」整个流程走完（`doStart` 的同步段还可能做 `restoreTask` 的存储读取），把存储 IO 的延迟传染给每一个事件链路。异步化后调用方立即返回；代价是「任务完成到候补启动」之间有微小延迟，以及并发 `notifyRunning` 需要靠 `d.lock` 串行化——这两点都被设计接受了。

**练习 2**：`Continue` 点名继续的任务，最多能同时启动几个？被腾挪的任务最后排在队列什么位置？

**参考答案**：最多 \( \min(M, C) \) 个——既受 `MaxRunning` 约束，也受本次点名数量约束。被腾挪的任务由 `doPauseForScheduling` 翻成 wait 后 append 到 `waitTasks` **队尾**，且它们先于本次溢出的点名任务入队；也就是说被抢占者不会丢掉排队资格，只是让出了当前名额。

**练习 3**：如果把 `doPauseForScheduling` 里「翻转为 wait」的复核条件从「状态是 pause **且** 代数未变」改成「状态是 pause」，会有什么风险？

**参考答案**：复核的代数条件防的是「`preparePause` 与翻转之间有第三方插足」。设想 watch 恰在此间把任务标成 done（`markTaskDone` 会置 done 并递增代数）：只查状态的话……实际上 `markTaskDone` 成功时状态已不是 pause，只查状态也能拦住。真正的差异在更刁钻的交错——比如任务被插足者再次启动（状态经历 pause→running）。总之代数复核是**防御性冗余**：状态检查依赖「每个写状态的人都会改状态」这一约定，代数检查则直接验证「从我来过之后没有任何人动过」，后者对未来代码演进更稳健。这类「双保险」在并发代码里值得留意，但也说明此处不变量微妙、改动需格外小心。

### 4.3 runGeneration 代数：与异步协程的竞态防护

#### 4.3.1 概念说明

本讲的竞态都源于同一个结构（u4-l2 讲过）：`doStart` 同步段改状态、异步 handler 做 IO。想象这条时间线：

```text
协程甲（doStart 的异步 handler，代数 gen=5）    协程乙（用户点了暂停）
  ├─ 正在做启动准备（回填 Meta、prepareTask…）
  │                                              ├─ preparePause：status=pause，gen→6
  │                                              └─ runPauseHandler：fetcher.Pause()
  ├─ 准备完成，调 fetcher.Start()   ← 灾难！任务已暂停却又被物理启动
  └─ watch 收到完成信号 → markTaskDone？
```

没有防护的话，最终状态取决于两个协程谁后写入——这是典型的数据竞争。Gopeed 的解法不是给整个过程加一把大锁（那样 IO 期间整个调度器都被卡住），而是**代数核对**：

- 同步段改状态前 `task.runGeneration++`，把新代数**抄写**给将要启动的异步协程；
- 异步协程在每个「有副作用的步骤」前核对 `taskIsRunningGeneration(task, gen)`——「任务现在仍是 running 且代数还是我记下的那个吗？」；
- 任何其他状态写入者（`preparePause`、`markTaskDone`、`markTaskError`）改状态时也 `runGeneration++`，**自动作废**所有在途旧代数协程的门票。

于是「谁该写状态」不再依赖锁的时序，而是依赖一个逻辑判断：**只有本轮运行的负责人才能收尾**。

#### 4.3.2 核心流程

`runGeneration` 的全部读写点（可用 `grep -n "runGeneration" pkg/download/downloader.go` 验证）：

```text
递增点（4 处，全部在状态写入的关键函数里）：
  doStart        L2015  进入 running   → 本轮运行的代数诞生
  preparePause   L2167  进入 pause     → 作废所有在途 start/watch
  markTaskDone   L2262  进入 done      → 作废所有在途协程（终态封版）
  markTaskError  L2276  进入 error     → 同上

核对点：
  taskIsRunningGeneration  L2209  status==running && gen 匹配
  taskIsPausedGeneration   L2218  (status==pause || status==wait) && gen 匹配
                             ↑ 注意接受 wait：doPauseForScheduling 翻转成 wait 后代数不变，
                               runPauseHandler 的复核仍要认账
  doPauseForScheduling      L2151  queued 的判定条件之一
```

`doStart` 异步 handler 内部的三次核对位置：

```text
handler()（持有 task.lock）:
  ① 拿到锁后立即核对        ← 防止「排队等锁期间被暂停」
  ② triggerOnStart 后核对    ← 扩展钩子（u9）执行期间可能耗时，期间可能被暂停
  ③ timer.Start 前核对       ← 最后一道闸，过了它就 fetcher.Start()
外层 goroutine wrapper:
  ④ handler 出错 → doOnError 前          核对（旧代数的错误不许标记新任务）
  ⑤ started 且成功 → emit(start) 前核对  （旧代数不许发 start 事件）
```

暂停侧的对应结构是 `doPause` = `preparePause`（同步改状态、记代数）+ `runPauseHandler`（核对 `taskIsPausedGeneration` 后物理 `fetcher.Pause()` + 落盘）——和 `doStart` 完全镜像。

#### 4.3.3 源码精读

**doStart 的同步段：代数的诞生**：

[pkg/download/downloader.go:L2001-L2026](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2001-L2026) —— `statusMut`（[L1933-L1938](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1933-L1938)，即 `task.statusLock` 的语法糖）内：`running/done` 直接返回（第一道守卫）；`restoreTask` 恢复 fetcher；`updateStatus(running)` 与 `runGeneration++` 在同一临界区**原子地**发生，`generation = task.runGeneration` 把新代数抄给外层闭包。这一步的原子性是整个机制的地基：状态与代数要么一起变、要么都不变。

**handler 的三次核对**：

[pkg/download/downloader.go:L2029-L2044](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2029-L2044) —— 第 ① 次核对在拿到 `task.lock` 后（[L2032](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2032)）：handler 可能在 goroutine 队列里等了很久才轮到执行，先确认「我这一轮还有效」。第 ② 次在 `triggerOnStart`（扩展 onStart 钩子，可能执行任意 JS）之后（[L2042](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2042)）。

[pkg/download/downloader.go:L2083-L2093](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2083-L2093) —— 第 ③ 次核对在 `fetcher.Start()` 前一刻（[L2086](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2086)）：清零速度、启动计时器，然后 `started = true` 标记「物理启动已发生」——这个布尔值决定外层是否发 `start` 事件、是否启动 watch。

[pkg/download/downloader.go:L2104-L2117](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2104-L2117) —— 外层 wrapper 的第 ④⑤ 次核对：handler 失败时只有「仍是本代运行」才调 `doOnError`——**旧代数的启动失败不允许把新任务标成 error**；成功时只有本代才 `emit(start)` 并 `go d.watch(task)`。

**暂停侧的镜像结构**：

[pkg/download/downloader.go:L2122-L2137](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2122-L2137) —— `doPause` = `preparePause` + `runPauseHandler`，成功后发 `pause` 事件与 webhook。它被 `Pause`（用户暂停）调用。

[pkg/download/downloader.go:L2159-L2176](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2159-L2176) —— `preparePause` 在 `statusMut` 内：`pause/done/error` 直接跳过（对已暂停任务重复暂停是 no-op）；否则 `updateStatus(pause)` + `runGeneration++` + `timer.Pause()`（净用时计时器停表，u4-l3）。注意它**不等**物理暂停完成就返回了代数——物理动作交给 `runPauseHandler`。

[pkg/download/downloader.go:L2178-L2207](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2178-L2207) —— `runPauseHandler` 持 `task.lock`：先 `taskIsPausedGeneration` 核对（[L2181](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2181)）——若从 `preparePause` 到这里之间任务被别人动了（比如又启动了），直接放弃；然后 `fetcher.Pause()` 物理暂停、`captureTaskStats` 存统计快照（失败仅告警，注释说明「fetcher 已停，生命周期不能被可选的统计失败拖垮」）、`saveTask` 落盘（无 fetcher 时退化为直接写 `bucketTask` 的任务克隆）。返回 `handled=true` 表示「这次暂停完整落地了」，`doPause` 才会发 `pause` 事件。

[pkg/download/downloader.go:L2209-L2225](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2209-L2225) —— 两个核对函数本体。`taskIsPausedGeneration` 接受 `pause` **或** `wait`：`doPauseForScheduling` 把状态翻成 wait 时不递增代数，核对仍须通过——这是「代数标识一次裁决，状态可随后微调」的体现。

**官方竞态测试（必读三连）**：

[pkg/download/downloader_test.go:L577-L633](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L577-L633) —— `TestDownloader_StaleStartGenerationCannotRestartTask`：用 `task.lock.Lock()` 把「第一次 Continue 的 handler、Pause、第二次 Continue」全部排在锁外候着，制造「旧 start handler 在暂停之后才恢复执行」的极端交错，断言 `fetcher.Start` 只被调用 1 次（旧代数的 handler 被第 ① 次核对拦下）。

[pkg/download/downloader_test.go:L635-L695](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L635-L695) —— `TestDownloader_PauseWinsAgainstStaleResolveError`：让 `Resolve` 阻塞在自制通道上，期间执行 Pause，再放行一个注定失败的 Resolve——断言任务保持 pause、没有 error 事件（第 ④ 次核对拦截了旧代数的错误）。

[pkg/download/downloader_test.go:L697-L765](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L697-L765) —— `TestDownloader_SchedulingPauseCompletesBeforeReplacementStart`：让 `fetcher.Pause` 阻塞，验证 Continue 的替换任务**不会**在物理暂停完成前启动（`doPauseForScheduling` 是同步完成的，不是发了就算）。

#### 4.3.4 代码实践：复现「暂停与完成同时发生」

**实践目标**：验证 `markTaskDone` 的 running 守卫 + 代数递增如何防止「晚到的完成信号把已暂停的任务标成 done」。

**操作步骤**：继续写在你自己的 `pkg/download/u4l4_practice_test.go` 里（**示例代码**）：

```go
func TestU4L4_PracticePauseWinsAgainstCompletion(t *testing.T) {
	manager := &generationTestManager{holdOpen: true}
	downloader := NewDownloader(&DownloaderConfig{
		FetchManagers: []fetcher.FetcherManager{manager},
	})
	if err := downloader.Setup(); err != nil {
		t.Fatal(err)
	}
	defer downloader.Clear()
	downloader.cfg.MaxRunning = 1

	id, err := downloader.CreateDirect(
		&base.Request{URL: "generation://race"},
		&base.Options{Path: t.TempDir(), Name: "race.bin"},
	)
	if err != nil {
		t.Fatal(err)
	}
	// 等任务 running 且物理启动完成、watch 已就位
	waitForTaskStatus(t, downloader, id, base.DownloadStatusRunning, 2*time.Second)
	deadline := time.Now().Add(2 * time.Second)
	for manager.starts.Load() != 1 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if manager.starts.Load() != 1 {
		t.Fatal("task did not physically start")
	}

	// ① 暂停先赢：等状态确实变成 pause
	pauseDone := make(chan error, 1)
	go func() { pauseDone <- downloader.Pause(&TaskFilter{IDs: []string{id}}) }()
	waitForTaskStatus(t, downloader, id, base.DownloadStatusPause, 2*time.Second)

	// ② “旧协程”（watch）此刻才收到完成信号
	downloader.GetTask(id).fetcher.(*generationTestFetcher).done <- nil

	// ③ 给 watch 一点时间处理完成信号，然后裁决：任务必须仍是 pause
	time.Sleep(300 * time.Millisecond)
	if got := downloader.taskStatus(downloader.GetTask(id)); got != base.DownloadStatusPause {
		t.Fatalf("stale completion overwrote pause, status = %s", got)
	}
	if err := <-pauseDone; err != nil {
		t.Fatal(err)
	}
}
```

运行：

```bash
go test ./pkg/download/ -run 'TestU4L4_PracticePauseWinsAgainstCompletion' -v
```

**需要观察的现象**：② 向 `done` 通道发信号后，watch 从 `Wait()` 中醒来、尝试走成功分支，但 ③ 处任务状态始终是 `pause`——`markTaskDone` 在 [pkg/download/downloader.go:L2244-L2246](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2244-L2246) 的守卫（`Status != running → return false`）把它拦下，`watch` 随即返回（[L1583-L1585](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1583-L1585) 的 `if !d.markTaskDone(task) { return }`），不发 `done` 事件、不 `notifyRunning`。

**预期结果**：测试通过。任务最终是 `pause` 而非 `done`；若把断言换成「等待 done」则会超时失败——这正是防护生效的证明。反向交错（完成先到、暂停后到）留作 4.4.4 的实践。

**待本地验证**：本沙箱无 Go 工具链，测试未实际运行；交错模型与官方 `TestDownloader_PauseWinsAgainstStaleResolveError` 同构。

**延伸问题（选做）**：结合 4.1.5 练习 1——构造「带过滤器暂停一个 wait 任务、然后释放名额」的场景，观察该任务是否会被 `notifyRunning` 重新拉起（`doStart` 同步段只拒绝 `running/done`）。把你观察到的行为记录下来，并思考这是否符合用户直觉。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `runGeneration` 不需要原子类型或锁保护，直接 `task.runGeneration++` 就行？

**参考答案**：因为对它的每一次读写都在锁内：递增点分别在 `statusMut`（`statusLock`）、`markTaskDone`/`markTaskError` 的临界区、`doPauseForScheduling` 的 `statusLock` 段内；核对点 `taskIsRunningGeneration`/`taskIsPausedGeneration` 也先拿 `statusLock`。代数与状态的变更被**同一把锁**绑定成原子对，这正是 4.3.3 强调的「状态与代数一起变」的落地方式。

**练习 2**：watch goroutine 阻塞在 `fetcher.Wait()` 时任务被暂停，watch 醒来后为什么「静默退出」而不是报错？

**参考答案**：见 [pkg/download/downloader.go:L1547-L1553](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1547-L1553)：`Wait()` 返回错误后先查当前状态，`pause/wait` 说明错误来自「暂停打断了传输」（比如 HTTP fetcher 关闭连接导致读错误），属于预期内的打断而非下载失败，直接 return。同理，`Wait()` 返回 nil 时若 `markTaskDone` 因状态非 running 而拒绝，watch 也直接返回——「暂停期间的任何信号都不改变 pause 裁决」。

**练习 3**：假如去掉 `doStart` handler 里 `triggerOnStart` 之后的那次核对（第 ② 次），最坏会发生什么？

**参考答案**：`triggerOnStart` 执行的是扩展的 onStart 钩子（u9），可能耗时任意长。期间用户暂停了任务（`preparePause` 置 pause、代数++、`runPauseHandler` 物理 `fetcher.Pause` 完成）。没有第 ② 次核对的话，handler 会继续走 prepareTask、`timer.Start()`、`fetcher.Start()`——把一个用户明确暂停的任务又物理启动了，且代数还是旧的，之后 watch 的收尾、事件的归属全部错乱。三次核对分别守住「进锁后」「慢操作后」「启动前」三个风险窗口，删任何一次都会打开对应的窗口。

### 4.4 markTaskDone / markTaskError：终态的唯一守门人

#### 4.4.1 概念说明

`done` 和 `error` 是两个终态（done 不可逆，error 可经 Continue 重试离开）。它们的写入被收敛到两个私有函数，全部调用点只有三处：

- `markTaskDone` ← `watch` 成功分支（`Wait()` 返回 nil）
- `markTaskError` ← `handleOnError`（`doOnError`/`doOnBlobSourceError` 的共同实现）

两个函数都用同一个模式把终态写入变成**一次性裁决**：

1. **running 守卫**：状态不是 running 直接返回 `false`——不管调用者是谁、持有哪个代数；
2. **成功后递增代数**：`runGeneration++` 让所有仍在途的旧代数协程（迟到的 start handler、迟到的 pause handler）全部失效。

守卫挡住「迟到的写者」，递增作废「已过检查但还没写完的写者」——两层合起来，终态一旦写下就不可被旧协程覆盖。

#### 4.4.2 核心流程

```text
watch: Wait() == nil                        handleOnError(err)
  ├─ 边界检查（Res 非空、任务未删）            ├─ task.lock
  ├─ markTaskDone(task) → true?              │   ├─ markTaskError(task) → true?
  │    ├─ 【task.lock + statusLock】          │   │    ├─ 【statusLock】
  │    ├─ 守卫：Status != running → false     │   │    ├─ 守卫：Status != running → false
  │    ├─ Used = timer.Used()（净用时）       │   │    └─ updateStatus(error) + gen++
  │    ├─ Res.Size==0 → 用 fetcher 进度补齐   │   ├─ triggerOnError（扩展可改 URL 救活任务）
  │    ├─ Speed = 总大小 / used（全程平均）    │   └─ …统计、落盘
  │    ├─ Downloaded = 总大小（对齐 100%）     └─ 锁外：emit(error) + emit(finally) + notifyRunning
  │    ├─ 清空 producer（Blob 状态）
  │    ├─ updateStatus(done)（DoneAt 一次性写入）
  │    └─ runGeneration++（封版）
  ├─ 统计快照 + 任务快照落盘
  └─ emit(done) + emit(finally) + notifyRunning() + webhook/script
```

注意 `markTaskDone` 是唯一同时拿 `task.lock` 和 `statusLock` 的状态写入者——因为要改 `Progress`、`Meta.Res.Size` 和 `producer` 这些任务级字段；`markTaskError` 只改状态，只拿 `statusLock`。

#### 4.4.3 源码精读

[pkg/download/downloader.go:L2236-L2264](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2236-L2264) —— `markTaskDone` 逐行：`task == nil || task.statusLock == nil` 防的是**从存储恢复但尚未 `initTask` 重建内核**的任务对象（u2-l2 讲过 JSON 序列化丢运行时字段）；running 守卫；`Used = task.timer.Used()` 取净用时（暂停时段不计）；`Res.Size == 0` 时用 `fetcher.Progress().TotalDownloaded()` 补齐（未知大小资源的收尾）；`used` 按秒取整且至少为 1，`Speed = totalSize / used`——**完成时刻的速度从滑动窗口均值改写为全程平均**（u4-l3 的伏笔在这里闭环）；`Downloaded = totalSize` 把进度精确对齐到 100%；`producer`/`producerReceived` 清零是 Blob 资源（u9）的收尾；最后 `updateStatus(done)` + `runGeneration++`。

[pkg/download/model.go:L155-L164](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L155-L164) —— `updateStatus` 里 `DoneAt` 只在进入 done 时写入且**没有任何分支负责清除它**——「done 任务永不离开 done 态」的结构保证（u2-l2 的「一次性写入」语义）。

[pkg/download/downloader.go:L2266-L2278](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2266-L2278) —— `markTaskError`：同款 running 守卫 + 代数递增，但只改状态。它被 `handleOnError` 在持 `task.lock` 的前提下调用（[L1700-L1704](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1700-L1704)），守卫返回 false 时整个错误处理直接放弃——这正是 4.3.5 练习 2 里「暂停打断不报错」的机制另一半：不是 watch 不报，是守卫不让报。

[pkg/download/downloader.go:L1583-L1599](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1583-L1599) —— watch 成功分支对 `markTaskDone` 返回值的处理：`false` 立即 return（任务已被别人裁决），`true` 才做全套收尾并 `notifyRunning()`——**名额释放以终态成功落笔为前提**，不会出现「任务其实没 done 却把名额放出去了」的错账。

#### 4.4.4 代码实践：反向交错与速度改写

**实践目标**：① 验证「完成先到、暂停后到」时 `preparePause` 的跳过守卫；② 验证完成时刻速度被改写为全程平均。

**操作步骤**：

1. 把 4.3.4 的测试复制为 `TestU4L4_PracticeDoneWinsAgainstPause`，把②③两步交换：先发 `done <- nil`、`waitForTaskStatus(done)`，再调用 `Pause`，最后断言任务**仍是 done**（`preparePause` 见 `done` 直接 `isReturn`，[pkg/download/downloader.go:L2161-L2164](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2161-L2164)）。
2. 给 `generationTestFetcher` 换一个思路验证速度改写：直接写一个纯逻辑小测试（**示例代码**）：

```go
func TestU4L4_PracticeAverageSpeedMath(t *testing.T) {
	// 复算 markTaskDone 的速度公式：Speed = totalSize / max(Used/1e9, 1)
	totalSize := int64(10 * 1024 * 1024) // 10 MiB
	usedSeconds := int64(3)              // 净用时 3.2s → 按秒取整为 3
	want := totalSize / usedSeconds      // = 3495253 字节/秒
	if want != 3495253 {
		t.Fatalf("speed math changed: %d", want)
	}
}
```

**需要观察的现象**：反向交错测试中任务保持 done、无 pause 事件；速度公式测试通过（若失败说明公式变了，请回读 [L2251-L2256](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2251-L2256)）。

**预期结果**：两个方向的交错各自保持先到的裁决；`done` 之后任何 `Pause`/`Continue`（点名 running/done 会被过滤器排除）都无法移动它。

**待本地验证**：同上，本沙箱未运行。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `markTaskDone` 要拿 `task.lock` 而 `markTaskError` 不用？

**参考答案**：`markTaskDone` 除了改状态还要写 `Progress.Used/Speed/Downloaded`、可能改 `Meta.Res.Size`、清 `producer`——这些是任务级字段，需要 `task.lock` 防止与 Patch、异步 handler 等并发写坏；`markTaskError` 只写 `Status` 和代数，`statusLock` 足够。（调用方 `handleOnError` 自己会拿 `task.lock` 做后续的错误处理，但那不是 `markTaskError` 的需求。）

**练习 2**：一个任务下载到 99% 时被暂停，随后 `Continue` 重启并完成。`DoneAt`、`Progress.Used`、最终 `Speed` 分别是什么口径？

**参考答案**：`DoneAt` 是第二次运行完成的时刻（第一次从未进入 done）；`Used` 是两轮的**净用时之和**（`timer.Pause` 累计、`Start` 续算，暂停时段不计入，见 [pkg/util/timer.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/util/timer.go)）；`Speed` 是 `总大小 / 净用时秒数` 的全程平均——不是任何一轮的瞬时速度。

**练习 3**：`markTaskError` 成功后任务进入 error，用户 `Continue` 它。代数经历了怎样的变化？旧 watch 还有机会写状态吗？

**参考答案**：error 落笔时 `runGeneration++`（作废旧协程）；Continue 走 `doStart`，进入 running 时再次 `runGeneration++` 并把新代数交给新的 handler 与（可能复用的）watch。旧 watch 若还阻塞在旧 fetcher 的 `Wait()` 上，醒来后要么被状态检查（非 running 不标 done、非 running 不标 error）拦下，要么发现状态仍是 running 但代数语义上已属新一轮——`watch` 主循环的错误分支靠「处理后状态仍 running 则 continue」续守新一轮（u4-l3），但 `markTaskDone`/`markTaskError` 的 running 守卫保证它**只能成功于当前轮、不可能覆盖别人的裁决**。

## 5. 综合实践：调度器探针——排队、腾挪与竞态一网打尽

把本讲四个模块串成一个探针测试。场景：`MaxRunning=2`，创建 4 个任务（前 2 个 running、后 2 个 wait），然后用户 `Continue` 点名最后一个任务——观察「腾挪」全过程中**状态、队列、事件、物理调用**四个视角的一致性。

在**你自己的克隆**里追加到 `pkg/download/u4l4_practice_test.go`（**示例代码**）：

```go
func TestU4L4_PracticeContinuePreempts(t *testing.T) {
	manager := &generationTestManager{holdOpen: true}
	downloader := NewDownloader(&DownloaderConfig{
		FetchManagers: []fetcher.FetcherManager{manager},
	})
	if err := downloader.Setup(); err != nil {
		t.Fatal(err)
	}
	defer downloader.Clear()
	downloader.cfg.MaxRunning = 2

	ids := make([]string, 0, 4)
	for i := 0; i < 4; i++ {
		id, err := downloader.CreateDirect(
			&base.Request{URL: "generation://pre"},
			&base.Options{Path: t.TempDir(), Name: "pre.bin"},
		)
		if err != nil {
			t.Fatal(err)
		}
		ids = append(ids, id)
	}
	// 等前两个任务物理启动（running = ids[0], ids[1]；wait = ids[2], ids[3]）
	deadline := time.Now().Add(2 * time.Second)
	for manager.starts.Load() != 2 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if manager.starts.Load() != 2 {
		t.Fatal("first two tasks did not physically start")
	}

	// 记录 pause 事件
	var mu sync.Mutex
	pauseEvents := make([]string, 0)
	downloader.Listener(func(event *Event) {
		if event.Key == EventKeyPause {
			mu.Lock()
			pauseEvents = append(pauseEvents, event.Task.ID)
			mu.Unlock()
		}
	})

	// 用户点名继续 ids[3]：空闲名额 0，需要现场腾挪 1 个
	if err := downloader.Continue(&TaskFilter{IDs: []string{ids[3]}}); err != nil {
		t.Fatal(err)
	}

	// 视角一：状态 —— ids[0] 被腾挪成 wait，ids[1] 保持 running，ids[3] 变 running
	if got := downloader.taskStatus(downloader.GetTask(ids[0])); got != base.DownloadStatusWait {
		t.Fatalf("ids[0] should be preempted to wait, got %s", got)
	}
	if got := downloader.taskStatus(downloader.GetTask(ids[1])); got != base.DownloadStatusRunning {
		t.Fatalf("ids[1] should keep running, got %s", got)
	}
	if got := downloader.taskStatus(downloader.GetTask(ids[3])); got != base.DownloadStatusRunning {
		t.Fatalf("ids[3] should be running, got %s", got)
	}
	// 视角二：事件 —— 恰好一个 pause 事件，且发给 ids[0]
	mu.Lock()
	if len(pauseEvents) != 1 || pauseEvents[0] != ids[0] {
		t.Fatalf("pause events = %v, want exactly [%s]", pauseEvents, ids[0])
	}
	mu.Unlock()
	// 视角三：物理调用 —— 一次 fetcher.Pause
	if got := manager.pauses.Load(); got != 1 {
		t.Fatalf("physical pauses = %d, want 1", got)
	}

	// 视角四：队列 —— 释放 ids[1] 的名额，观察队首（ids[2]）而非 ids[0] 顶上
	downloader.GetTask(ids[1]).fetcher.(*generationTestFetcher).done <- nil
	deadline = time.Now().Add(3 * time.Second)
	for manager.starts.Load() != 3 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if got := manager.starts.Load(); got != 3 {
		t.Fatalf("replacement did not start, starts = %d", got)
	}
	if got := downloader.taskStatus(downloader.GetTask(ids[2])); got != base.DownloadStatusRunning {
		t.Fatalf("ids[2] should be running after ids[1] finished, got %s", got)
	}
	if got := downloader.taskStatus(downloader.GetTask(ids[0])); got != base.DownloadStatusWait {
		t.Fatalf("ids[0] should still be waiting, got %s", got)
	}
}
```

运行：

```bash
go test ./pkg/download/ -run 'TestU4L4_Practice' -v
```

**完成标准**：

1. 能解释为什么被腾挪的是 `ids[0]`（`Continue` 按 `d.tasks` 创建顺序扫描 running，[pkg/download/downloader.go:L823-L839](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L823-L839)）。
2. 能解释 `ids[0]` 为何排在 `ids[2]` 之后被唤醒（腾挪者 append 到队尾，而 `ids[2]` 早已在队列里）。
3. 能在测试输出里指出四个视角（状态/事件/物理调用/队列）各自对应的断言，并说出「pause 事件为何一定已经发出」（Continue 在释放 `d.lock` 后、`doStart` 之前补发事件，而 `doStart` 的同步段在 `Continue` 返回前已完成）。
4. 把 4.3.4 的竞态测试与本测试放在一起跑若干次（`-count=10`），体会「交错不确定、结果确定」的并发设计目标。

**待本地验证**：以上代码未在本环境运行；若断言失败，优先检查你对「`Continue` 扫描的是 `d.tasks` 而非 `waitTasks`」与「事件补发时机」的理解，再回读 4.2.3 与 4.3.3。

## 6. 本讲小结

- **批量操作模型**：`Pause`/`Continue`/`Delete` 共用 `TaskFilter`（IDs + Statuses + NotStatuses，AND 语义）；`Pause`/`Continue` 各自注入默认黑名单，`Delete` 不设限且幂等（匹配空返回 nil）；API 层只做「单 ID/查询参数 → 过滤器」的翻译，语义全部收敛在核心层。
- **MaxRunning 是软上限**：仲裁只发生在入队决策点（`doCreate`、`Continue`、`continueAll`、`notifyRunning` 弹出），`doStart` 本身不查名额；`waitTasks` 是 FIFO 切片，`notifyRunning` 异步、持锁、一次只拉一个、每次重算名额，天然防超发。
- **Continue 的腾挪算法**：\( \text{needPauseCount} = \max(0, \min(M, C) - (M - R)) \)；被腾挪任务经 `doPauseForScheduling`「同步物理暂停 + 代数复核 + 翻转 wait」后排队尾，pause 事件延迟到 `d.lock` 释放后补发。
- **runGeneration 代数**：状态与代数在同一把锁内成对变更；异步协程（start handler、pause handler、watch）在关键步骤前核对代数，「不是本轮负责人就不动手」；递增点共 4 处（doStart/preparePause/markTaskDone/markTaskError）。
- **终态守门人**：`markTaskDone`/`markTaskError` 用 running 守卫拒绝迟到者、用代数递增作废在途者，使 done/error 成为一次性裁决；`DoneAt` 靠「无清除分支」的结构保证一次性写入；完成时刻速度改写为全程平均。
- **事件纪律**：凡是在持有 `d.lock`（`Continue` 的调度暂停）或 `task.lock`（watch 收尾）期间发生的事件，一律延迟到锁外 `emit`——因为 listener 回调可能反过来调用任何需要这些锁的 Downloader 方法，锁内发事件等于埋死锁。

## 7. 下一步学习建议

本讲补全了 Downloader 生命周期的「控制面」：暂停、继续、删除与调度。至此第四单元还剩最后一块拼图——**持久化与断点恢复**（u4-l5）：本讲反复出现的 `saveTask`、`restoreFetcher`、三个存储桶（`task`/`save`/`task_stats`）到底写了什么、重启后任务如何恢复成 pause 并被 `ContinueOnStartup` 拉起。建议：

1. 依次精读 [pkg/download/storage.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go) 的 `Storage` 接口与 `BoltStorage` 实现、`downloader.go` 的 `Setup` 恢复路径（`restoreTask`/`restoreFetcher`），以及 HTTP 与 BT 两个协议各自的 `Store`/`Restore`。
2. 做一个承上启下的实验：把 4.2.4 的时序测试换成 `NewBoltStorage(t.TempDir())`，在任务 running 时 `Close()` 再重建 Downloader，验证任务回落 pause、`ContinueOnStartup`（[pkg/download/downloader.go:L916-L921](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L916-L921)）的行为与本讲的 `continueAll` 语义一致。
3. 之后进入第五单元（u5-l1 HTTP Resolve 深潜），把 `fetcher.Pause()` 在 HTTP 协议里到底关掉了什么连接作为引子——控制面的每一次「暂停」，最终都要由协议实现翻译成传输面的具体动作。
