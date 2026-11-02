# u2-l2 Task 结构与任务状态机

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `pkg/download.Task` 每个公开字段的含义，以及那些不出现在 JSON 里的运行时私有字段各自守护着什么。
2. 画出 `ready / wait / running / pause / error / done` 六种状态的完整迁移图，并标注每条边由谁触发（Downloader 的哪个方法、Fetcher 通过什么方式汇报）。
3. 解释 `updateStatus` 与 `DoneAt` 的「一次性写入」语义：为什么代码里没有专门的 `if DoneAt == nil` 判断，却能保证完成时间不被覆盖。
4. 说明 `Task.ParentID` 与内嵌 `group *TaskGroup` 指针如何分工表达「任务属于某个分组」——一个负责持久化、一个负责运行时快速访问，序列化时又如何变成 `parentId` 与 `group` 两个 JSON 字段。
5. 手算 `calcSpeed` 滑动窗口的输出：给定若干个 tick 的增量字节和刷新间隔，算出显示给用户的速度值。

## 2. 前置知识

- **状态机（State Machine）**：把一个对象的生命周期抽象成「有限的几个状态 + 状态之间的迁移边」。读下载器源码时，先找到状态常量，再找所有写状态的地方，就能把控制流画成一张图。
- **滑动窗口平均（Moving Average）**：速度不是「瞬时值」而是「最近若干秒增量的平均值」，这样网络抖动时显示不会上蹿下跳。本讲会看到 Gopeed 用一个长度受限的 `[]int64` 切片实现它。
- **Go 的 JSON 序列化规则**：`encoding/json` 只导出**导出字段**（大写开头），结构体的私有字段（小写开头）、互斥锁、函数指针等一律被跳过。这是理解 `Task`「持久化外观」与「运行时内核」分离的关键。
- **互斥锁（`sync.Mutex`）**：多个 goroutine 并发读写同一任务时的保护手段。本讲只需要理解「读写 `Status` 前要先拿 `statusLock`」即可，锁的全景图留到 u11-l2。
- **ID 引用与对象引用**：表达「A 属于 B」有两种做法——存对方的 ID（可序列化、跨进程稳定）或存对方的指针（内存里零查找）。Gopeed 的任务分组两种都用：`ParentID` 是 ID 引用，`group` 是指针引用。理解这个分工是本讲新增内容的基础。
- **上一讲（u2-l1）的词汇**：Request（解析输入）、Resource（解析输出，含 `Name`/`Size`/`Files`）、Options（下载意图，含自定义 `Name`）、以及批量模型 `CreateTaskBatch` 的 `Group/GroupName` 字段。本讲 `Task.Name()` 的回退链建立在前三者之上，`ParentID` 则承接批量模型里「建一个组」的意图。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pkg/download/model.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go) | 本讲主文件：`Task`、`TaskGroup`、`CreateTaskBatchResult` 结构体，`Name()`、`MarshalJSON()`、`updateStatus()`、`calcSpeed()` 全部在这里 |
| [pkg/base/constants.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/constants.go) | 六个状态常量的唯一定义处 |
| [pkg/download/downloader.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go) | 所有状态写入点（`doCreate`/`doStart`/`preparePause`/`markTaskDone`/`markTaskError`…）、分组的挂接与恢复、`Progress` 结构、全局测速循环 |
| [pkg/download/downloader_test.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go) | 分组元数据与恢复的现成测试 `TestDownloader_TaskGroupMetadataAndRestore`，验证 `parentId`/`group` 序列化行为 |
| [pkg/download/model_test.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model_test.go) | 现成的 `calcSpeed` 单元测试，本讲实践的模仿对象 |
| [pkg/util/timer.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/util/timer.go) | `Task.timer` 的用时统计（可暂停累计） |
| [pkg/util/json.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/util/json.go) | `DeepClone`：基于 JSON 的深拷贝，解释了 `clone()` 为何会丢掉运行时字段 |

## 4. 核心概念与源码讲解

### 4.1 Task 结构体：公开字段、运行时字段与 JSON 外观

#### 4.1.1 概念说明

`Task` 是 Gopeed 下载世界的「单位」：一次下载从建任务到文件落盘，所有状态都挂在同一个 `Task` 对象上。它可以拆成三层：

1. **公开层（会被持久化、会被 API 返回）**：`ID`、`Protocol`、`Meta`、`ParentID`、`Status`、`Uploading`、`Progress`、时间戳、`DoneAt`、`NeedsPreparation`。
2. **运行时层（私有字段，JSON 序列化时全部消失）**：`group`、`fetcher`、`timer`、四把锁、测速窗口 `speedArr`、`runGeneration` 等。
3. **展示层（序列化时动态计算）**：`name` 与 `group` 两个字段——磁盘上并不存它们，每次 `MarshalJSON` 时由 `Name()` 与 `group` 指针现算（`ParentID` 本身是公开字段会正常序列化，`group` 对象则是追加输出的）。

这样设计的动机：同一份 `Task` 既要落盘（bbolt 里只存公开层）、又要跨进程恢复（恢复后由 `initTask` 重建运行时层、由 `Setup` 重新挂接分组指针）、还要喂给前端（补上人类可读的名字与所属分组）。

#### 4.1.2 核心流程

一个任务对象的一生：

```text
NewTask() 生成 ID、status=ready、打时间戳
    ↓ doCreate：补 fetcherManager/fetcher/Meta/Progress，赋 ParentID，
      NeedsPreparation=true，initTask 装配运行时字段，锁内挂接 group 指针
    ↓ 存入 bucketTask（此刻 JSON 序列化，运行时字段丢失）
    ↓ 运行期：watch/测速循环 读写 Status 与 Progress
    ↓ 结束：clone() 再次 JSON 序列化落盘（仍只有公开层，含 ParentID）
    ↓ 进程重启恢复：initTask 重建锁/计时器，Setup 按 ParentID 重新挂接 group，
      restoreFetcher 重建协议引擎
```

#### 4.1.3 源码精读

**Task 结构体定义** —— [pkg/download/model.go:L46-L92](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L46-L92)

这段代码定义了任务的全部字段。要点逐条看：

- `Meta *fetcher.FetcherMeta`：打包了上一讲的 `Req/Res/Opts` 三要素（[internal/fetcher/fetcher.go:L50-L54](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/fetcher/fetcher.go#L50-L54)），所以 `Task.Meta.Res` 为 nil 就代表「还没解析」。
- `ParentID string`（L53-L54）：分组归属的**持久化表达**，带 `omitempty`——不属于任何组的任务不输出这个字段。详细语义见 4.4。
- `DoneAt *time.Time`（L60-L63）：指针类型，nil 表示从未完成过；注释明说「done 任务永远不会离开 done 状态，所以该值写一次就保留」。
- `NeedsPreparation`（L64-L71）：注释说明了语义——首次启动前要做一次「决定下载目录 + 重名自动改名」的准备，做完就清掉；排队、暂停、失败重试的任务仍会补做这次准备，而**已准备过的任务永远不会再被分类或改名**。
- 私有字段里，`group *TaskGroup`（L73）是 4.4 的主角之一；`speedArr`/`uploadSpeedArr` 是 4.5 的主角，`runGeneration` 是 4.3 的主角，四把锁分别保护状态、整体结构、统计数据、blob 引用。

**NewTask：ready 是起点** —— [pkg/download/model.go:L119-L130](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L119-L130)

任务创建时状态为 `ready`，同时生成 nanoid 作为 `ID`。注意此时**锁和 timer 还没装配**——那是 `initTask` 的活。

**Name() 的四级回退** —— [pkg/download/model.go:L132-L159](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L132-L159)

显示名按优先级回退：

1. 用户自定义名 `Meta.Opts.Name`；
2. 任务还没解析（`Meta.Res == nil`）→ 请协议管理器从 URL 猜名 `fetcherManager.ParseName(Req.URL)`，猜不出返回 `"unknown"`；
3. 已解析且是多文件资源 → 文件夹名 `Meta.Res.Name`；
4. 单文件资源 → 第一个文件名 `Meta.Res.Files[0].Name`。

**MarshalJSON：避免无限递归的别名技巧 + 追加分组** —— [pkg/download/model.go:L161-L173](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L161-L173)

`type rawTaskType Task` 把 `Task` 变成一个「没有自定义 MarshalJSON 的新类型」，然后用匿名嵌入 + 追加 `Name` 与 `Group *TaskGroup` 两个字段。这是 Go 处理「在默认序列化之上加计算字段」的标准手法。两处细节：

- `Group` 带 `omitempty`（L166）：`group` 指针为 nil（普通任务）时 JSON 里完全没有 `group` 字段；有组时输出完整的 `TaskGroup` 对象（`id/name/path/createdAt`）。
- 序列化结果里同时会出现 `parentId`（来自公开字段）与 `group.id`（来自追加字段），两者应当一致——这是 4.4 实践要验证的不变量。

**私有字段在这次序列化中全部消失**——这正是落盘内容与内存对象的差异来源。

**initTask：恢复运行时内核** —— [pkg/download/downloader.go:L3057-L3066](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L3057-L3066)

装配 timer（用已保存的 `Progress.Used` 续上用时）、四把锁和两个测速窗口。它在新任务创建（[pkg/download/downloader.go:L2184](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2184)）和进程重启恢复（[pkg/download/downloader.go:L256](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L256)）两处被调用——因为 `clone()` 走的是 JSON 深拷贝（[pkg/util/json.go:L16-L24](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/util/json.go#L16-L24)），锁、fetcher 和 group 指针必然丢失，只能重建。

**TaskRuntimeStatus：轻量轮询视图** —— [pkg/download/model.go:L94-L109](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L94-L109)

详情页高频刷新时不必反复传输完整 Task，这个只含 `Status/Speed/Downloaded/...` 的瘦身结构就是为此准备的（注释写明了意图）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：搞清「一个 Task 落盘后长什么样、恢复时缺什么」。
2. **操作步骤**：
   - 阅读 `MarshalJSON` 与 `DeepClone`，列出哪些字段会进 JSON、哪些不会；
   - 对照 `initTask` 与 `Setup`，逐个回答：每个被 JSON 丢掉的私有字段由谁重建？（锁/timer/窗口 ← `initTask`；fetcher ← `restoreFetcher`；fetcherManager ← `assignFetcherManager`，见 [pkg/download/downloader.go:L2660-L2666](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2660-L2666)；group 指针 ← `Setup` 按 `ParentID` 查表回填，见 4.4）；
   - 填一张 `Name()` 回退表：给四种输入（自定义名 / 未解析的 HTTP URL / 多文件种子 / 单文件），写出 `Name()` 各自返回什么。
3. **需要观察的现象**：`runGeneration`、`speedArr`、`fetcher`、`group` 都不在任何 JSON 输出里（`group` 的**内容**会被 MarshalJSON 追加输出，但 `clone()` 之后的对象上该指针是 nil）。
4. **预期结果**：得到一张「公开层 vs 运行时层」的两栏清单；理解为什么重启后必须重新装配而不能直接用反序列化出来的对象。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DoneAt` 声明为 `*time.Time` 而不是 `time.Time`？
**答案**：需要区分「从未完成」（nil）和「完成于零时刻」两种情况。值类型 `time.Time` 的零值无法与真实时间区分，且 JSON 里会序列化成 `"0001-01-01T00:00:00Z"`；指针的语义让「没完成」就是「没有这个字段」。

**练习 2**：`MarshalJSON` 里 `type rawTaskType Task` 这一行删掉会怎样？
**答案**：不定义新类型而直接嵌入 `*Task` 的话，序列化 `Task` 时会再次调用 `Task.MarshalJSON`，形成无限递归直到栈溢出。定义一个不带该方法的新类型（新类型不继承方法集里的 MarshalJSON），才能拿到「默认序列化行为」。

**练习 3**：`NeedsPreparation` 为什么不能只用 `Status == ready` 来代替？
**答案**：`ready` 是瞬时状态，入队后立刻变成 `wait`/`running`；而「尚未做过首准备」这件事要跨排队、暂停、失败重试持续成立，直到 `prepareTask` 真正跑完才清除。两者生命周期完全不同。

**练习 4**：`ParentID` 和 `group` 都是表达分组归属，为什么一个用导出字段、一个用私有指针？
**答案**：`ParentID` 要落盘——重启后存储里只剩 JSON 反序列化出的公开字段，必须靠它找回组织；`group` 是纯内存加速——运行中频繁要读组的 `Path`（如启动时校验目录），每次都去查 `d.groups` 表可以接受，但持有指针更直接，且它**不能**跟着默认序列化走（否则组元数据会冗余地复制进每个任务记录）。两者的分工正是「持久化用 ID、运行时用引用」。

### 4.2 六种状态与状态迁移图：谁在改状态

#### 4.2.1 概念说明

状态的「词汇表」只有一个地方：`pkg/base/constants.go`。六个状态里，`wait` 是纯调度概念（Downloader 的排队队列），Fetcher 对它一无所知；`done` 是唯一终态；`error` 不是终态，可以重试。

先给出本讲最重要的结论：**`task.Status` 的写入者只有 Downloader**。Fetcher 从不直接改任务状态，它只通过 `Wait()` 的返回值「汇报」完成或失败，由 Downloader 的 `watch` 循环翻译成 `done`/`error` 迁移。这条职责边界是整个并发设计不乱套的前提。

#### 4.2.2 核心流程

完整状态迁移图（边上标注触发者，全部是 Downloader 的方法）：

```text
                    NewTask()
                        │
                        ▼
                   ┌─────────┐  doCreate(名额已满，入 waitTasks 队列)
        ┌─────────▶│  ready  │───────────────┐
        │          └─────────┘                │
        │             │ doCreate(有余量)      ▼
        │             │                 ┌─────────┐
        │             └────────────────▶│  wait   │◀───────────────┐
        │                               └─────────┘                │
        │                        notifyRunning│ │Continue(doStart) │
        │                        ┌───────────┘ │                  │
        │                        ▼             │                  │
        │   Continue/doStart ┌─────────┐       │ doPauseForScheduling(排队分支)
        └───────────────────▶│ running │       │ Continue(名额不足)
                 ┌───────────└─────────┘───────┘
                 │                │
     preparePause│via doPause     │ fetcher.Wait() 返回 nil
     (doPause /  │via doPause-    │        → watch → markTaskDone
     ForSchedul- │ForScheduling   │ fetcher.Wait() 返回 err
     ing)        ▼                │        → watch → doOnError → handleOnError
                 ┌─────────┐      │          → markTaskError
        ┌───────▶│  pause  │      ▼                   (或 doStart 的启动 handler 失败)
        │        └─────────┘ ┌─────────┐  ┌─────────┐
        │                   │  error  │  │  done   │ ◀─ 终态：无出边
        └───────────────────└─────────┘  └─────────┘
              Continue/doStart     Continue/doStart(error 可重试)

  另有一条「重启边」：进程启动恢复时，一切非 done/error 的任务统一回落为 pause。
```

汇总成表格：

| 迁移 | 触发者（Downloader 方法） | 现实场景 |
| --- | --- | --- |
| （新建）→ ready | `NewTask` | 任务对象刚诞生 |
| ready → wait | `doCreate`（名额算完入队） | `MaxRunning` 已满 |
| ready → running | `doCreate → doStart` | 有并发余量，立即启动 |
| wait → running | `notifyRunning → doStart`；`Continue → doStart` | 有任务腾出名额 / 用户手动继续 |
| running → pause | `preparePause`（被 `doPause`、`doPauseForScheduling` 调用） | 用户暂停或调度让位 |
| pause → wait | `doPauseForScheduling` 的排队分支；`Continue` 名额不足分支 | 高优先级任务要跑，把它挤回队列 |
| wait → pause | `doPause`（`Pause` 先把被暂停成员从 `waitTasks` 队列同步剔除；`pauseAll` 直接清空整个队列） | 用户暂停全部或指定任务 |
| running → error | `markTaskError`（由 `handleOnError` 调用） | `fetcher.Wait()` 返回错误，或启动 handler 失败 |
| running → done | `markTaskDone`（由 `watch` 调用） | `fetcher.Wait()` 返回 nil |
| error → running | `Continue → doStart` | 用户重试失败任务 |
| 任意非 done/error → pause | `Setup` 启动恢复逻辑 | 进程重启，未完成任务统一回落 |

注意两条特殊语义：

- **done 无出边**：`doStart` 与 `preparePause` 都对 done 提前返回，`markTaskDone`/`markTaskError` 只接受 running，所以完成后状态永远不变。BT 做种时状态保持 done，「上传中」由独立的 `Uploading` 布尔字段表达（[pkg/download/downloader.go:L1866-L1882](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1866-L1882)：上传协程等 `WaitUpload()` 结束后才把 `Uploading` 翻回 false 并落盘），不占用状态机。
- **ready 几乎是瞬时的**：`NewTask` 之后紧接着 `doCreate` 就会改成 wait 或启动，外部观察者基本看不到 ready。

还有一条与分组相关的旁路：`Pause`/`Continue`/`ContinueBatch`/`Delete` 接受的 ID 列表里如果出现**组 ID**（即某任务的 `ParentID`），会先经 `expandGroupTaskIDs` 展开成全部成员任务 ID 再走上述迁移——它改变的是「哪些任务被选中」，不新增任何状态边（详见 4.4 与 u4-l6）。

#### 4.2.3 源码精读

**状态常量定义** —— [pkg/base/constants.go:L5-L12](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/constants.go#L5-L12)

六个字符串常量，注释即语义：`ready` 是「创建但未启动」，`wait` 是「等待运行」。状态用字符串而非整数，是为了 REST API 与存储的可读性——你将在任务 JSON 里直接看到 `"status":"running"`。

**建任务时的分叉** —— [pkg/download/downloader.go:L2193-L2218](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2193-L2218)

`doCreate` 在持有全局锁的临界区里计算 `remainRunningCount`（[pkg/download/downloader.go:L617-L625](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L617-L625)，即 `MaxRunning - 正在运行数`）：名额为零就置 `wait` 并压入 `waitTasks` 队列，否则直接 `doStart`。这就是 ready→wait / ready→running 两条边。

**wait → running 的唤醒** —— [pkg/download/downloader.go:L600-L615](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L600-L615)

`notifyRunning` 弹出队首的等待任务并启动它。任务完成（`watch` 末尾）、出错、被暂停之后都会调用它，把腾出的并发名额立刻分给排队者。

**wait → pause：暂停时的队列清理** —— [pkg/download/downloader.go:L962-L1000](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L962-L1000)

`Pause` 在真正调用 `doPause` 之前，先在锁内把被暂停的任务从 `waitTasks` 里同步剔除（L978-L990）——否则一个还挂在队列里的「已暂停」任务会被随后的 `notifyRunning` 错误地重新启动。`pauseAll`（[L1002-L1018](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1002-L1018)）则直接清空整个队列。

**pause → wait：调度让位** —— [pkg/download/downloader.go:L2507-L2525](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2507-L2525)

`doPauseForScheduling` 先走完整的暂停流程（`preparePause` + `runPauseHandler`），再在 `statusLock` 保护下把刚变成 pause 的任务改判为 wait。`Continue`（[pkg/download/downloader.go:L1021-L1096](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1021-L1096)）恢复一批任务前若名额不够，就用它把正在运行的任务挤回队列——「插队」机制的两半都在这里。

**重启回落边** —— [pkg/download/downloader.go:L239-L261](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L239-L261)

`Setup` 从存储加载任务列表后，除 done/error 外一律改成 pause（L257-L259）——重启后没有任务「自动」跑起来，等 `ContinueOnStartup` 配置或用户操作再续。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：亲手验证「写 `task.Status` 的地方全部在 downloader.go」。
2. **操作步骤**：

   ```bash
   # 找出所有对 Status 的写入点（直接赋值与方法调用两类）
   grep -n "task.Status = \|\.updateStatus(" pkg/download/downloader.go
   grep -rn "DownloadStatus" pkg/base/ pkg/download/ --include='*.go' | grep -v _test
   ```

   逐行分类：哪几行是「赋值」，哪几行是 `updateStatus`；再对照本小节的迁移表，确认每条边都能对上代码行。
3. **需要观察的现象**：除 `model.go` 里的 `NewTask` 外，所有写入点都集中在 `pkg/download` 包；`internal/protocol/` 下的各协议实现一次都不会出现。
4. **预期结果**：得到一张「代码行 ↔ 迁移边」的对照清单，并且确认 Fetcher 侧没有状态写入。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `wait` 不做成 Fetcher 可见的状态？
**答案**：wait 只描述「Downloader 的并发名额不够」，与传输层无关；任务在队列里时甚至可能还没有可用的 fetcher。把它留在调度层，Fetcher 接口（Setup/Resolve/Start/Pause/Close…）才能保持与调度策略正交。

**练习 2**：error 和 done 都是「不再传输」的状态，为什么 error 有出边而 done 没有？
**答案**：error 代表「这次尝试失败了」，语义上允许重试（`Continue` 的过滤器只排除 running 和 done，见 [pkg/download/downloader.go:L1032](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1032)）；done 代表「文件已完整落盘」，再迁移会破坏 `DoneAt` 的一次性语义（见 4.3），也可能让下游（做种、解压、webhook）重复触发。

**练习 3**：进程重启后，一个重启前处于 wait 的任务会变成什么状态？它的排队位置还在吗？
**答案**：变成 pause（重启回落边）。「排队位置」不在了——`waitTasks` 是内存队列，重启后清零，需要再次 Continue 才会重新进入调度。

**练习 4**：`Pause` 为什么要先把任务从 `waitTasks` 里剔除，再去逐个 `doPause`？反过来行不行？
**答案**：不行。如果先逐个 `doPause`，期间某次暂停会触发 `notifyRunning`（见 `doPause` 尾部与 `Pause` 尾部的调用），它可能立刻把仍留在队列里的「正在被暂停」任务弹出来 `doStart`，与暂停意图打架。先剔除后暂停保证了队列里永远不会残留「即将变成 pause」的任务。

### 4.3 updateStatus 与 DoneAt 的一次性写入语义

#### 4.3.1 概念说明

`updateStatus` 是状态机唯一的「统一入口」之一：改状态、刷 `UpdatedAt`，并在进入 done 时顺手写入 `DoneAt`。有趣的是，它**没有** `if t.DoneAt == nil` 这样的判空保护，却能保证完成时间只写一次。保证不来自这一行代码，而来自状态机的整体形状：**done 是终态 + markTaskDone 只接受 running**，两个约束合起来使「进入 done」这件事在一个任务的生命周期内最多发生一次。这是「不变量由结构保证，而非由分支保证」的典型例子。

同一形状还顺带解决了并发问题：`doStart`/`preparePause`/`markTaskDone`/`markTaskError` 每次切换状态都会让 `runGeneration` 自增，旧协程在关键动作前用 `taskIsRunningGeneration` 检查「自己还是当前这一轮吗」，不是就放弃——防止「暂停与完成同时发生」时旧协程覆盖新状态。

#### 4.3.2 核心流程

`updateStatus(status)` 的执行：

```text
UpdatedAt = now
Status = status
if status == done:  DoneAt = now   // 因为 done 无出边，本分支整个生命周期最多执行一次
```

`watch` 循环里完成/失败的翻译过程（谁调 updateStatus）：

```text
fetcher.Wait() 返回 nil  → markTaskDone:
    守卫：Status 必须是 running，否则返回 false（什么都不做）
    收尾：写总用时、补齐 Res.Size、算「全程平均速度」、清 producer
    updateStatus(done) → DoneAt 落地，runGeneration++
fetcher.Wait() 返回 err → doOnError → handleOnError → markTaskError:
    守卫：Status 必须是 running
    updateStatus(error)，runGeneration++
```

#### 4.3.3 源码精读

**updateStatus 本体** —— [pkg/download/model.go:L175-L184](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L175-L184)

注意 L178-L179 的注释：**「done 任务永远不会离开 done 状态，所以不存在任何需要清除 DoneAt 的迁移分支」**——这句注释就是一次性写入语义的权威声明。

**markTaskDone：完成迁移的守卫与收尾** —— [pkg/download/downloader.go:L2604-L2632](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2604-L2632)

三层要点：

1. L2612-L2614 守卫：`Status != running` 直接返回 false。已 done 的任务再次路过这里会被拒之门外——这就是「DoneAt 不会被第二次写入」的直接执行者。注意它拿锁的顺序是先 `task.lock` 再 `statusLock`（L2608-L2611），比 `markTaskError` 多一层结构锁，因为收尾要读写 `Meta/Progress/fetcher` 等整体字段。
2. L2615-L2625 收尾：总用时取自 `task.timer`；若资源大小未知（`Res.Size == 0`）就取 fetcher 实际下载量补上；把 `Speed` 改写为「总大小 / 总秒数」的**全程平均速度**（`used == 0` 时按 1 秒兜底，避免除零），并把 `Downloaded` 拉齐到总大小——覆盖掉 4.5 的滑动窗口值，完成那一刻显示的是整段旅程的均值，而非最近 5 秒。
3. L2629-L2630：`updateStatus(done)` 写入 DoneAt，随后 `runGeneration++` 让任何持旧代数的协程失效。

**markTaskError：失败迁移** —— [pkg/download/downloader.go:L2634-L2646](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2634-L2646)

同样的 running 守卫 + `updateStatus(error)` + 代数自增。done 与 error 走的是同一套「守卫—提交—失效化旧协程」骨架。

**doStart 的入口闸门** —— [pkg/download/downloader.go:L2368-L2393](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2368-L2393)

在 `statusMut`（持有 `statusLock` 执行闭包，[pkg/download/downloader.go:L2300-L2306](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2300-L2306)）里检查：running/done 直接返回（前者防重复启动，后者是 done 终态的体现），否则恢复任务数据、`updateStatus(running)`、`runGeneration++` 并记下本轮代数。error 任务之所以能重试，正是因为闸门只拦 running/done。

**代数检查** —— [pkg/download/downloader.go:L2577-L2584](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2577-L2584)

`taskIsRunningGeneration` 要求「状态是 running **且** 代数匹配」。`doStart` 的异步 handler（[pkg/download/downloader.go:L2396-L2471](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2396-L2471)）在多个关键节点反复调用它，一旦任务被暂停就静默退出，不会把旧结果写回。完整竞态分析留给 u4-l4/u11-l2。

#### 4.3.4 代码实践

写一个包内测试验证 DoneAt 的一次性写入。因为 `Task` 的关键字段是私有的，测试要放在 `package download` 里（模仿现成的 [pkg/download/model_test.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model_test.go)），并在 `initTask` 之前先把 `Progress` 准备好（`initTask` 会读 `task.Progress.Used`）。

以下为**示例代码**（可保存为 `pkg/download/status_machine_test.go`——本实践建议仅在本地尝试，不要提交到仓库）：

```go
package download

import (
	"testing"
	"time"

	"github.com/GopeedLab/gopeed/internal/fetcher"
	"github.com/GopeedLab/gopeed/pkg/base"
)

func TestMarkTaskDoneWritesDoneAtOnce(t *testing.T) {
	task := NewTask()
	task.Progress = &Progress{} // 必须在 initTask 之前：initTask 要读 Progress.Used
	initTask(task)
	task.Meta = &fetcher.FetcherMeta{
		Res:  &base.Resource{Name: "demo.bin", Size: 1000},
		Opts: &base.Options{},
	}
	task.updateStatus(base.DownloadStatusRunning)
	task.timer.Start() // markTaskDone 用 timer.Used() 算全程平均速度，计时器必须先启动

	d := &Downloader{} // markTaskDone 不使用 Downloader 自身状态，零值即可
	if !d.markTaskDone(task) {
		t.Fatal("第一次 markTaskDone 应当成功")
	}
	if task.Status != base.DownloadStatusDone {
		t.Fatalf("status = %s, want done", task.Status)
	}
	first := *task.DoneAt
	if first.IsZero() {
		t.Fatal("DoneAt 不应是零值")
	}

	time.Sleep(20 * time.Millisecond) // 拉开时间差，便于发现覆盖
	if d.markTaskDone(task) {
		t.Fatal("对已完成任务再次 markTaskDone 应当被守卫拒绝")
	}
	if !task.DoneAt.Equal(first) {
		t.Fatalf("DoneAt 被改写: %v -> %v", first, *task.DoneAt)
	}

	// error 分支同样只接受 running
	task2 := NewTask()
	task2.Progress = &Progress{}
	initTask(task2)
	if d.markTaskError(task2) {
		t.Fatal("对 ready 任务 markTaskError 应当被拒绝")
	}
}
```

运行方式：

```bash
go test ./pkg/download/ -run 'TestMarkTaskDoneWritesDoneAtOnce' -v
```

1. **实践目标**：用可执行的断言证明「DoneAt 一次写入」靠的是 running 守卫 + done 终态，而不是 updateStatus 里的判空。
2. **操作步骤**：如上创建测试文件并运行；再故意把第一个断言里的 `markTaskDone` 换成直接调用 `task.updateStatus(base.DownloadStatusDone)` 两次，观察 DoneAt 是否被第二次覆盖——从而理解「结构保证」与「分支保证」的差别。
3. **需要观察的现象**：第一次 `markTaskDone` 返回 true 且 `Speed == 1000`（总大小 1000 / 兜底 1 秒）、`Downloaded == 1000`；第二次返回 false，`DoneAt` 纹丝不动。
4. **预期结果**：测试全绿；换成裸调 `updateStatus` 的变体里 DoneAt 会被覆盖——说明守卫在 `markTaskDone` 层，`updateStatus` 本身并无防重入能力。

#### 4.3.5 小练习与答案

**练习 1**：如果去掉 `markTaskDone` 里的 running 守卫，最坏会发生什么？
**答案**：任何重复路过完成路径的协程（例如 watch 循环与一次迟到的启动 handler 并发）都能再次执行 `updateStatus(done)`，DoneAt 被改写成更晚的时间；配合代数自增失效机制被破坏，还可能对已 done 任务重复发 done 事件、重复触发 webhook/解压。

**练习 2**：为什么 `markTaskDone` 里要把 `Speed` 改成全程平均，而不是保留最后的滑动窗口值？
**答案**：任务结束后速度不再刷新，展示层会永久停留在「最后 5 秒的均值」上；用总大小/总耗时更符合「这次下载整体多快」的直觉，也是各下载器完成页的通用做法。

**练习 3**：`updateStatus` 为什么不顺便做 `runGeneration++`，而要让每个调用方自己做？
**答案**：两者的耦合点不同。`doStart`/`preparePause` 需要在自增**之后**读取新代数并传给后续异步流程；`markTaskDone`/`markTaskError` 只需要让旧代数失效。让调用方掌控自增与读取的相对顺序，避免了「先读后增」或「先增后读」的隐式约定（本条为对现状设计的解读，供讨论）。

### 4.4 ParentID 与 TaskGroup：分组归属的两种表达

#### 4.4.1 概念说明

上一讲（u2-l1）末尾提到：批量请求 `CreateTaskBatch` 带 `Group=true` 时，Gopeed 会为整批任务建一个「组」，预留一个共享目录（如 `<下载目录>/我的收藏/`），成员任务各自独立调度，但文件都落进这个目录。本讲回答最底层的问题：**单个 Task 如何记住自己属于哪个组？**

答案是双轨制：

- **`Task.ParentID`（公开字段）**：存组的 ID，跟着任务一起序列化、落盘、跨进程存活。它是「事实上的归属凭证」。
- **`Task.group *TaskGroup`（私有指针）**：运行时直接指向组对象（含 `Name/Path/CreatedAt`），免查表；序列化任务时由 `MarshalJSON` 动态追加输出成 `group` 字段。它不落盘，重启后由 `Setup` 按 `ParentID` 重新挂接。

`TaskGroup` 本体非常小——[pkg/download/model.go:L32-L38](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L32-L38)：`ID`（组标识，与任务 ID 同为 nanoid）、`Name`（目录名）、`Path`（预留的绝对路径）、`CreatedAt`。注意**组不是任务**：`GetTask(groupID)` 返回 nil，它只是元数据记录，存在独立的 `task_group` 存储桶里（[pkg/download/downloader.go:L39-L40](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L39-L40) 附近定义的 `bucketTaskGroup` 常量）。

与批量创建的结果对应：`CreateDirectBatchWithResult` 返回的 [CreateTaskBatchResult](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L40-L44)（`taskIds` + `parentId`）里的 `parentId` 就是成员任务身上的 `ParentID`——前端靠它把卡片聚成一组。

#### 4.4.2 核心流程

分组归属在任务生命周期各阶段的维护：

```text
批量创建（Group=true）:
    createTaskGroup 预留目录、生成 TaskGroup、写入 task_group 桶
    ↓ 对每个成员：doCreate 赋 task.ParentID = groupID
    ↓ 锁内挂接 task.group = d.groups[groupID]（查不到 → 任务创建失败）
    ↓ 批次结束：清除 creatingGroups 标记 + cleanupEmptyGroups（一个成员都没有的组会被回收）

运行期:
    启动准备（prepareTask）按 task.group 决定目录：分组任务跳过自动分类，直接用组目录
    批量操作（Pause/Continue/Delete）收到组 ID 时用 expandGroupTaskIDs 展开为成员任务 ID

重启恢复（Setup）:
    先加载 task_group 桶 → d.groups 表
    再加载任务 → task.group = d.groups[task.ParentID]   ← 指针重新挂接
    cleanupEmptyGroups：把没有任何成员任务的组连同空目录清掉
```

关键不变量：**`ParentID` 是权威，`group` 指针只是缓存**。展开组 ID 的实现甚至不依赖 `d.groups` 表——注释写明「Group IDs expand through parentId, even when metadata is unavailable」（组元数据丢了也能按 ParentID 展开），见 [pkg/download/downloader.go:L829-L849](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L829-L849)。

#### 4.4.3 源码精读

**TaskGroup 与 CreateTaskBatchResult 模型** —— [pkg/download/model.go:L32-L44](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L32-L44)

两个新增模型：`TaskGroup` 注释强调「存储被独立调度的任务们共享的目录」；`CreateTaskBatchResult` 则是普通批量与分组批量共用的响应结构。

**创建时赋 ID 与挂指针** —— [pkg/download/downloader.go:L2163-L2221](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2163-L2221)

`doCreate` 的两个动作分两处：L2174 在锁外先赋 `task.ParentID = groupID`（随后 `initTask` 装配运行时字段）；L2197-L2203 在全局锁内查 `d.groups[groupID]` 挂接 `task.group` 指针——查不到（组刚被删掉等竞态）则整个创建以 `ErrTaskNotFound` 失败，**且不会落盘**（`storage.Put` 在挂接之后）。普通任务 `groupID` 为空串，跳过挂接。

**重启时重新挂接** —— [pkg/download/downloader.go:L230-L267](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L230-L267)

`Setup` 的顺序很讲究：先从 `task_group` 桶加载全部组进 `d.groups`（L230-L237，注释：组元数据独立于任务/fetcher 恢复记录），再加载任务并在 L254 `task.group = d.groups[task.ParentID]` 逐个回填指针；最后 L262-L267 调 `cleanupEmptyGroups`——如果重启前组里任务已被删光，这个组和它的空目录在启动时被回收。

**序列化输出 parentId 与 group** —— [pkg/download/model.go:L161-L173](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L161-L173)

4.1 已逐行读过 `MarshalJSON`：`parentId` 来自默认序列化的公开字段（omitempty），`group` 来自显式追加的指针字段（omitempty）。前端拿到的每个任务 JSON 因此自带完整分组信息，不需要再查一次组接口。

**组 ID 展开到成员** —— [pkg/download/downloader.go:L829-L849](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L829-L849)

`expandGroupTaskIDs` 遍历任务表，选中「ID 命中或 ParentID 命中」的所有任务——传组 ID 等于传「该组全部成员 ID」。`Pause`（[L967-L972](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L967-L972)）、`Continue`（[L1026-L1031](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1026-L1031)）、`Delete` 都在入口处先做这一步。批量创建与删除的完整语义（部分成功、目录预留去重、`creatingGroups`/`deletingTasks` 竞态防护）是 u4-l6 的主题，本讲只记「组 ID 会被展开」这一条。

**现成测试：分组元数据与恢复** —— [pkg/download/downloader_test.go:L4070-L4110](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4070-L4110)

`TestDownloader_TaskGroupMetadataAndRestore` 把本节不变量全部固化：创建后断言成员的 `ParentID`/`group`/目录/SkipAutoCategorize（L4084-L4088）、`json.Marshal` 输出含 `"group":`（L4089-L4092）；关闭再用同一 BoltStorage 重建 Downloader，断言指针恢复（L4097-L4103）；最后删光成员，断言组记录被清（L4104-L4109）。

#### 4.4.4 代码实践

1. **实践目标**：验证「带 `ParentID` 的任务序列化后输出 `parentId` 与 `group` 两个字段，且普通任务两者皆无」。
2. **操作步骤**：

   先跑现成测试感受行为（它覆盖了创建、序列化、重启恢复、删除回收全链路）：

   ```bash
   go test ./pkg/download/ -run 'TestDownloader_TaskGroupMetadataAndRestore' -v
   ```

   再补一个只针对 `MarshalJSON` 的最小测试（**示例代码**，追加到本地的 `status_machine_test.go`；注意 `Name()` 会解引用 `Meta.Opts`，所以 Meta 必须初始化）：

   ```go
   func TestTaskMarshalJSONWithGroup(t *testing.T) {
       task := NewTask()
       task.Progress = &Progress{}
       initTask(task)
       task.Meta = &fetcher.FetcherMeta{
           Req:  &base.Request{URL: "https://example.com/a.bin"},
           Res:  &base.Resource{Name: "a.bin", Size: 10},
           Opts: &base.Options{},
       }

       // 普通任务：无 parentId 也无 group
       wire, err := json.Marshal(task)
       if err != nil {
           t.Fatal(err)
       }
       if bytes.Contains(wire, []byte(`"parentId"`)) || bytes.Contains(wire, []byte(`"group"`)) {
           t.Fatalf("普通任务不应输出分组字段: %s", wire)
       }

       // 分组任务：parentId 与 group 同时出现，且 group.id == parentId
       grp := &TaskGroup{ID: "g1", Name: "folder", Path: "/tmp/folder", CreatedAt: time.Now()}
       task.ParentID = grp.ID
       task.group = grp
       wire, err = json.Marshal(task)
       if err != nil {
           t.Fatal(err)
       }
       if !bytes.Contains(wire, []byte(`"parentId":"g1"`)) || !bytes.Contains(wire, []byte(`"group":`)) {
           t.Fatalf("分组任务应输出 parentId 与 group: %s", wire)
       }
       var m map[string]any
       if err := json.Unmarshal(wire, &m); err != nil {
           t.Fatal(err)
       }
       if g, ok := m["group"].(map[string]any); !ok || g["id"] != "g1" {
           t.Fatalf("group.id 应等于 parentId: %s", wire)
       }
   }
   ```

   运行：`go test ./pkg/download/ -run TestTaskMarshalJSONWithGroup -v`（import 需补 `bytes`、`encoding/json`）。
3. **需要观察的现象**：普通任务的 JSON 里两个字段都缺席（omitempty 生效）；分组任务的 JSON 同时有 `"parentId":"g1"` 和完整的 `"group":{...}` 对象。
4. **预期结果**：两个测试全绿。进一步可把 `task.group` 置 nil 但保留 `ParentID` 再序列化——输出只剩 `parentId` 没有 `group`，这正是「重启后先加载组、再挂指针」顺序存在的原因：反序列化出来的任务一开始就是这种「有 ID 无指针」形态。

#### 4.4.5 小练习与答案

**练习 1**：为什么组元数据要单独存 `task_group` 桶，而不是塞进每个任务的 JSON 里？
**答案**：任务记录只存轻量的 `ParentID`，组对象（名字、路径、创建时间）全局只有一份。若冗余进每个任务，改名或清理组时要批量改写所有成员记录，还可能产生不一致；单独成桶后，加载是一次 `List`，挂接是一次 map 查找。

**练习 2**：`doCreate` 里挂接 `group` 指针失败（组不存在）时，任务为什么不会残留在存储里？
**答案**：挂接发生在 `storage.Put(bucketTask, ...)` **之前**（同一个锁内临界区，L2197-L2207 的顺序是先挂接再落盘），失败路径直接返回错误，任务从未写入桶，也不会进 `d.tasks`。

**练习 3**：`expandGroupTaskIDs` 遍历的是 `d.tasks` 而不是 `d.groups`，这带来什么好处？
**答案**：展开的依据是每个任务自己的 `ParentID`，组元数据表只是加速挂接的缓存。即使 `task_group` 桶里的记录损坏或丢失（`d.groups` 查不到），按组 ID 暂停/删除成员仍然有效——归属的权威数据始终在任务身上。

### 4.5 calcSpeed：5 秒滑动窗口测速

#### 4.5.1 概念说明

下载器界面上那个不断跳动的速度数字，来自一个很朴素的方案：每隔一个 tick 问一次 fetcher「到目前为止一共下了多少字节」，把**相邻两次的差值**（本 tick 的增量）压进一个最多装 5 秒样本的队列，然后用队列均值除以 tick 时长得到「字节/秒」。差值而非累积量的原因：累积量只增不减，无法反映「现在」的快慢；用差值窗口平均，既能平滑抖动又能跟随变化。

同一份算法被下载与上传（BT 做种）复用，各自持有独立的窗口数组（`speedArr` / `uploadSpeedArr`）。

#### 4.5.2 核心流程

```text
Downloader.Setup 启动一个全局 goroutine：
loop（每 RefreshInterval 毫秒一轮，默认 350ms）:
    for 每个任务:
        current = fetcher.Progress().TotalDownloaded()      # 累积下载量
        delta   = current - task.Progress.Downloaded        # 本轮增量
        (blob 生产者任务走单独的 received 口径)
        tick    = RefreshInterval / 1000                    # 每轮秒数
        task.Progress.Used   = task.timer.Used()            # 累积用时（可暂停）
        task.Progress.Speed  = updateSpeed(max(0, delta), tick)  # ← 滑动窗口
        task.Progress.Downloaded = current
        发射 progress 事件
    sleep(RefreshInterval)
```

`calcSpeed(speedArr, downloaded, usedTime)` 的算法：

1. `usedTime <= 0`：直接返回 0（防除零）。
2. `downloaded < 0`：**回退清零**——把窗口清空并返回 0。这是为「任务从头重来」（例如失败重置后累积量比之前还小）准备的；调用点用 `max(0, delta)` 兜底，此处是第二道防线（现有测试 [pkg/download/model_test.go:L5-L18](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model_test.go#L5-L18) 验证的就是这个分支）。
3. 追加样本；若窗口长度超过 \( \lfloor 5.0 / \Delta t \rfloor \) 就丢弃最旧样本。
4. 返回窗口均值除以 tick 时长。

数学表达（\(\Delta_i\) 为第 \(i\) 轮增量字节，\(\Delta t\) 为每轮秒数，\(N = \lfloor 5.0/\Delta t \rfloor\) 为窗口容量）：

\[
\text{speed} \;=\; \frac{\dfrac{1}{N}\displaystyle\sum_{i=1}^{N} \Delta_i}{\Delta t} \;\;[\text{字节/秒}]
\]

默认配置下 \(\Delta t = 0.35\)，\(N = \lfloor 5.0/0.35 \rfloor = 14\)，即窗口实际覆盖约 \(14 \times 0.35 = 4.9\) 秒。

#### 4.5.3 源码精读

**Progress 结构体** —— [pkg/download/downloader.go:L82-L103](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L82-L103)

`Used`（纳秒总用时）、`Speed`（字节/秒）、`Downloaded`（累积字节）构成下载侧三元组，`UploadSpeed`/`Uploaded` 是上传侧镜像，后四个字段服务于归档解压进度。注意 `Used` 来自 `task.timer`——一个支持暂停累计的计时器（[pkg/util/timer.go:L20-L26](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/util/timer.go#L20-L26)：`Pause` 把耗时累进 `used`，暂停期间不计费）。

**全局测速循环** —— [pkg/download/downloader.go:L314-L412](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L314-L412)

要点（以 [L335-L405](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L335-L405) 为核心）：

- 只处理 `Status == running`（或 `Uploading`）的任务——暂停/排队任务的速度自然冻结。
- `delta = current - task.Progress.Downloaded`；blob 生产者任务（扩展在内存里产出的资源）切换口径时直接 `task.speedArr = nil` 重置窗口（L350-L360），避免新旧口径的样本混平均。
- `task.Progress.Speed = task.updateSpeed(max(0, delta), tick)`（L368）——这就是滑动窗口的调用点。
- 每轮末尾 `time.Sleep(RefreshInterval)`（[L410](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L410)）；`RefreshInterval` 默认 350ms 由 [pkg/download/model.go:L263-L265](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L263-L265) 设定。
- 循环里锁的顺序是「task.lock → statusLock」并特意用 `TryLock` 跳过忙任务（L325-L334 的注释解释了与调度锁序相反的问题），此处了解即可。

**calcSpeed 本体** —— [pkg/download/model.go:L198-L219](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L198-L219)

算法即 4.5.2 的四步。注意它操作的是 `*[]int64`（切片的指针），因为要修改调用方持有的切片头（清空、追加、丢头部）——`t.updateSpeed`/`t.updateUploadSpeed`（[L190-L196](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L190-L196)）分别绑定到下载/上传两个窗口。

**速度的另一处写入**：`doStart` 启动时在 `statusLock` 内把 `Progress.Speed` 清零并启动计时器（[pkg/download/downloader.go:L2450-L2457](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2450-L2457)），`markTaskDone` 完成时改写为全程平均（4.3 已讲）。窗口值只在运行途中是「权威」的。

#### 4.5.4 代码实践

1. **实践目标**：用测试固化「5 秒窗口均值」的行为，并亲手算一遍与程序对账。
2. **操作步骤**：先运行现成测试：

   ```bash
   go test ./pkg/download/ -run TestCalcSpeedResetOnRollback -v
   ```

   再新增以下**示例代码**（同样放在 `package download` 内，可追加到 `status_machine_test.go`）：

   ```go
   func TestCalcSpeedFiveSecondWindow(t *testing.T) {
       arr := make([]int64, 0)
       tick := 0.5 // 假设 RefreshInterval = 500ms
       var got int64
       for _, d := range []int64{100, 200, 300, 400, 500, 600} {
           got = calcSpeed(&arr, d, tick)
       }
       // 均值 = 2100/6 = 350 字节/tick → 350/0.5 = 700 B/s
       if got != 700 {
           t.Fatalf("speed = %d, want 700", got)
       }

       // 窗口容量 int(5.0/0.5) = 10：再喂 10 个 0，把旧样本全部挤出窗口
       for i := 0; i < 10; i++ {
           got = calcSpeed(&arr, 0, tick)
       }
       if got != 0 {
           t.Fatalf("speed = %d, want 0", got)
       }
       if len(arr) != 10 {
           t.Fatalf("window len = %d, want 10", len(arr))
       }
   }
   ```

   运行：`go test ./pkg/download/ -run TestCalcSpeedFiveSecondWindow -v`
3. **需要观察的现象**：第一段断言验证均值公式；第二段断言验证窗口上限——10 个零样本把 6 个旧样本全部挤出，速度从 700 衰减到 0，窗口长度停在 10 不再增长。
4. **预期结果**：两个测试全绿。若把第二段喂零的次数改成 5，只有最旧的 1 个样本（100）被挤出，窗口为 `[200,300,400,500,600,0,0,0,0,0]`，`got = 400`——可先手算再跑验证（即小练习 2）。

#### 4.5.5 小练习与答案

**练习 1**：把 `RefreshInterval` 从 350ms 改成 700ms，窗口容量和「实际覆盖时长」分别变成多少？
**答案**：容量 \(N = \lfloor 5.0/0.7 \rfloor = 7\)；覆盖 \(7 \times 0.7 = 4.9\) 秒。有趣的是覆盖时长几乎不变（总是略小于 5 秒），变的是样本粒度：tick 越长，速度越迟钝但越省 CPU 与存储写入。

**练习 2**：接续 4.5.4 的场景——喂完 `[100..600]` 之后再喂 **5** 个 0，此刻 `calcSpeed` 返回值是多少？
**答案**：注意溢出时每次调用只丢弃**一个**最旧样本（`arr = arr[1:]`），不是一次性裁到容量。前 4 个 0 只追加（长度 7→10 不触发丢弃），第 5 个 0 追加后长度变 11 才挤掉最旧的 100，窗口为 `[200,300,400,500,600,0,0,0,0,0]`，均值 \(= 2000/10 = 200\) 字节/tick，速度 \(= 200/0.5 = 400\) B/s。可见速度是逐个样本「线性挤出」式衰减，而不是瞬间归零。

**练习 3**：为什么 `calcSpeed` 需要 `downloaded < 0` 的回退分支？调用点不是已经 `max(0, delta)` 了吗？
**答案**：`max(0, delta)` 只是当前调用点的策略；`calcSpeed` 作为包内公共函数还可能被新调用方使用（防御性设计），而且「累积量回退」本身是一个明确的业务事件（任务从头重来），清空窗口能让此前的旧样本不污染重启后的第一次测速。现有测试 `TestCalcSpeedResetOnRollback` 正是把这一隐含契约固化下来，防止将来有人「顺手删掉没用的分支」。

## 5. 综合实践

把本讲五个模块串起来：先画图，再用测试证明图上的关键不变量。

**任务 A：完整状态迁移图（触发者标注版）**

把 4.2.2 的迁移表扩写成你自己的图，要求：

1. 六个状态一个不缺；
2. 每条边标注「触发方法名 + 触发场景」；
3. 用两种箭头颜色/线型区分「信息源是 Fetcher（Wait 返回值）」的边（running→done、running→error）和「纯 Downloader 决策」的边（其余所有边）；
4. 额外标出重启回落边、`Pause` 的 waitTasks 队列清理，以及 `Uploading` 布尔字段（done 态做种）与状态机的关系。

参考答案：见 4.2.2 的图与表。关键结论自查——所有边的**写入者**都是 Downloader；Fetcher 的两条边只是「汇报完成/失败」，翻译动作发生在 `watch`（[pkg/download/downloader.go:L1858-L2000](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1858-L2000)）里。

**任务 B：不变量测试**

在 `pkg/download` 包内新建 `status_machine_test.go`（示例代码，本地练习用），包含 4.3.4、4.4.4 与 4.5.4 的三个测试，然后一次性运行：

```bash
go test ./pkg/download/ -run 'TestMarkTaskDoneWritesDoneAtOnce|TestTaskMarshalJSONWithGroup|TestCalcSpeedFiveSecondWindow|TestCalcSpeedResetOnRollback|TestDownloader_TaskGroupMetadataAndRestore' -v
```

验证四件事：

1. `DoneAt` 一次写入（done 终态 + running 守卫）；
2. 5 秒窗口均值算法与手算一致；
3. 仓库已有的回退清零契约仍然成立；
4. 分组任务的 `parentId`/`group` 序列化行为及重启后的指针恢复。

**任务 C（可选，运行观察型）：让状态机动起来**

用 u1-l4 学过的 Boot API 写一个最小程序，创建 3 个任务并把 `MaxRunning` 设为 1，在 `Listener` 里打印每次事件的 `task.Status`，对照你的图核对 `wait → running → done` 的序列与 `notifyRunning` 的唤醒时机。若本地不便运行完整下载，此项可标注「待本地验证」。

## 6. 本讲小结

- `Task` 分公开层（可序列化字段 + 动态计算的 `name`/`group`）与运行时层（fetcher、锁、timer、测速窗口、runGeneration）；JSON 序列化天然丢掉后者，所以每次落盘恢复后都要 `initTask` 重建内核。
- 六种状态中 `wait` 是纯调度概念，`done` 是唯一终态，`error` 可重试；重启后一切未完成任务统一回落 `pause`；`Pause` 会先把目标从 `waitTasks` 队列剔除再逐个暂停。
- 状态的唯一写入者是 Downloader；Fetcher 只通过 `Wait()` 的返回值汇报完成/失败，由 `watch` 循环翻译成 `done`/`error` 迁移。
- `DoneAt` 的一次性写入由结构保证：`markTaskDone`/`markTaskError` 的 running 守卫 + done 无出边，比任何判空分支都可靠；`runGeneration` 代数机制则防止旧协程覆盖新状态。
- 分组归属是双轨制：`ParentID`（公开、落盘、权威）+ `group` 指针（私有、运行时缓存、MarshalJSON 时追加输出）；创建时锁内挂接、重启时按 ParentID 回填、空组在启动与删除时回收；批量操作里的组 ID 经 `expandGroupTaskIDs` 展开为成员任务。
- 速度是「最近约 5 秒差值样本的均值 ÷ tick 时长」：窗口容量 \(\lfloor 5.0/\Delta t \rfloor\)，默认 14 个样本；负增量触发窗口清零；完成那一刻改写为全程平均速度。

## 7. 下一步学习建议

- 下一讲 **u2-l3 DownloaderStoreConfig：全局配置体系**：`MaxRunning`（本讲反复出现的并发名额）、`RefreshInterval`（测速 tick）等参数的默认值与部分更新机制都定义在那里。
- 进入第四单元前，建议先通读 [pkg/download/downloader.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go) 中的 `doStart`、`watch`、`notifyRunning` 三个函数，把本讲的状态图「贴」到调用链上；**u4-l2（主链路：Resolve → Create → doStart）**与 **u4-l4（暂停、继续、删除与最大并发调度）** 会逐行展开。
- 本讲 4.4 只讲了「单个任务如何表达分组归属」；`CreateDirectBatchWithResult` 的批量语义、`createTaskGroup` 的目录预留去重、`creatingGroups`/`deletingTasks` 竞态标记与空组清理全流程，请直接进入 **u4-l6（任务分组与批量创建：TaskGroup 与批量 API）**。
- 想理解 `fetcher.Wait()` 背后的完成通知机制（DoneCh），预习 [internal/fetcher/fetcher.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/fetcher/fetcher.go)，它是 u3-l1 的主角。
