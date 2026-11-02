# u4-l5 持久化与断点恢复：存储桶与 Store/Restore

## 1. 本讲目标

前几讲我们把任务从创建一路追到暂停、删除与调度，但始终留了一个悬案：**进程死掉之后，这一切凭什么还能找回来？** 本讲专门回答这个问题。读完本讲，你应该能够：

1. 解释 `Storage` 接口的八个方法各自做什么，`MemStorage` 与 `BoltStorage` 双实现的差异，以及 bbolt 文件里「桶—键—值」的真实布局。
2. 说出 `saveTask` 在任务生命周期的哪些时刻被调用、往 `task` / `save` / `task_stats` 三个桶里各写了什么。
3. 跟踪重启后的完整恢复路径：`Setup` 读 `task` 桶重建任务骨架 → 惰性 `restoreFetcher` 读 `save` 桶重建协议内核 → `ContinueOnStartup` 决定要不要自动续跑。
4. 对照 HTTP、BT、ed2k 三个协议的 `Manager.Store/Restore` 实现，理解「协议内脏」的序列化约定。

## 2. 前置知识

本讲需要以下背景概念，均已在前几讲建立，这里只做一句 recap：

- **存储桶（bucket）**：u4-l1 介绍过 Downloader 的八个桶名常量。本讲下钻到桶之下：桶里的键是什么、值是什么、谁在什么时候读写。
- **bbolt**：一个纯 Go 实现的**嵌入式键值数据库**（类似 SQLite 之于 SQL），整个数据库就是磁盘上的一个文件，数据按「桶 → 键 → 字节数组值」组织，读写必须包在事务里。它是 etcd 的底层存储，Gopeed 直接拿它当任务库。
- **Task 的公开层与运行时层**：u2-l2 讲过 `Task` 分公开字段（ID/Meta/Status/Progress…）和运行时字段（fetcher、锁、timer、测速窗口…），JSON 序列化会丢掉后者。这正是 `task` 桶里存的只是「骨架」的原因——重启后要用 `initTask` 重建内核。
- **检查点（checkpoint）**：u4-l3 讲过进度刷新协程按 `shouldSaveTaskCheckpoint` 决定何时调 `saveTask`。本讲把 `saveTask` 本身拆开看。
- **Store/Restore 两段式契约**：u3-l4 讲过 `Store(f)` 返回可 JSON 序列化的协议状态（nil 表示无状态），`Restore()` 返回「空模板 + 构造器」。本讲不再重复契约定义，而是对照三个真实协议看它们**各自存了什么、为什么**。
- **惰性恢复（lazy restore）**：重启后任务的 `fetcher` 字段是 `nil`，直到真正需要（启动、Patch、做种恢复）才从 `save` 桶重建。这是本讲的主线之一。

一个直觉比喻：`task` 桶存「病历卡」（谁、什么状态、下载到哪了），`save` 桶存「手术记录」（协议私有的续传细节，比如每条连接停在哪个字节），`task_stats` 桶存「体检报告」（BT 的 peers/piece 快照等统计）。删除任务时三份一起烧掉。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [pkg/download/storage.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go) | `Storage` 接口 + 内存/bbolt 双实现 | 接口八个方法、gopeed.db、JSON 序列化 |
| [pkg/download/downloader.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go) | 下载引擎中枢 | 桶常量、`saveTask`/`restoreTask`/`restoreFetcher`、`Setup` 恢复流程、`ContinueOnStartup` |
| [pkg/download/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go) | Task 模型 | `Task.clone()` 深克隆、`DownloaderConfig` 默认存储 |
| [internal/protocol/http/fetcher_manager.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go) | HTTP 协议管理器 | `fetcherData` 字段逐个精读、并发安全快照 |
| [internal/protocol/http/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go) | HTTP Fetcher | 恢复后 `resumeConnections` 如何从 chunk 边界续传 |
| [internal/protocol/bt/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/bt/fetcher.go) | BT Fetcher | BT 极简的 `fetcherData` 与 piece 状态的另存渠道 |
| [internal/protocol/ed2k/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ed2k/fetcher.go) | ed2k Fetcher | 「无任务级状态」的最简合法写法 |
| [pkg/rest/server.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/rest/server.go) | REST 服务器 | 双实现的选型处、`ContinueOnStartup` 的调用时机 |
| [pkg/base/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go) | 全局配置 | `AutoStartTasks` 字段 |

## 4. 核心概念与源码讲解

### 4.1 Storage 接口与双实现

#### 4.1.1 概念说明

`Storage` 是 Gopeed 唯一的持久化抽象。Downloader 不关心数据落在内存还是磁盘，只面向这八个方法编程：

- `Setup(buckets)`：声明并创建全部桶（幂等）。
- `Put/Get/List/Delete`：常规键值 CRUD，`List` 把整个桶读进一个切片。
- `Pop`：读取并删除（原子消费一条记录）。
- `Close`：关闭存储（bbolt 关闭文件）。
- `Clear`：彻底清空（bbolt 直接删文件）。

双实现的分工非常明确：**`MemStorage` 给测试与临时进程用**（数据随进程消失），**`BoltStorage` 给桌面端、服务端等长生命周期形态用**（数据落在 `gopeed.db`）。选型不在 Downloader 内部决定，而是由传输层入口注入——u4-l1 讲过 `DownloaderConfig.Storage` 是进程级配置。

#### 4.1.2 核心流程

一次 `Put` 在两个实现里的旅程：

```text
BoltStorage.Put(bucket, key, v)
  ├─ json.Marshal(v)              值 → JSON 字节
  └─ db.Update(事务)              bbolt 写事务
       └─ tx.Bucket(bucket).Put(key, json)

MemStorage.Put(bucket, key, v)
  └─ data[bucket][key] = v        直接存 any 原值（不序列化！）
```

关键差异与推论：

1. **BoltStorage 每次读写都是 JSON 往返**——写进桶的值必须可 JSON 序列化，读出来也经过 `json.Unmarshal` 反序列化到调用方给的指针。这就是协议 `Store()` 返回值必须可序列化的根本原因。
2. **MemStorage 存的是原始对象引用，零拷贝**——`Get` 后拿到的就是当初 `Put` 进去的同一个对象。这带来一个测试中的陷阱：通过 `Get` 读出的对象与写入方共享内存，改它会直接影响存储里的值。bbolt 路径则天然隔离。
3. bbolt 的桶在 `Setup` 里用 `CreateBucketIfNotExists` 建立（[pkg/download/storage.go:L154-L164](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go#L154-L164)），所以旧版本数据库文件遇到新桶名也能平滑升级——这也是 Gopeed 用「新增桶」而非「改表结构」演进持久化格式的底气。

#### 4.1.3 源码精读

**接口定义**——[pkg/download/storage.go:L13-L23](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go#L13-L23) 定义 `Storage` 接口，八个方法签名如上所述。注意没有任何「事务批量写」接口：一次 `saveTask` 写两个桶是**两次独立事务**，中间进程崩溃可能产生「新 task + 旧 save」的组合——4.3 节会看到 HTTP 协议如何用「快照权威字段」自愈这种撕裂。

**BoltStorage 构造**——[pkg/download/storage.go:L130-L152](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go#L130-L152)：`NewBoltStorage(dir)` 先 `MkdirAll` 目录，再把 `gopeed.db` 拼进路径、`bbolt.Open` 打开。数据库文件名硬编码为 `gopeed.db`（`dbFile` 常量），所以你在任何 Gopeed 数据目录里看到它都不意外。打开失败直接 `panic`——存储不可用对下载器是致命伤。

**Put/Get 的 JSON 往返**——[pkg/download/storage.go:L166-L194](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go#L166-L194)：`Put` 先 `json.Marshal(v)` 再开写事务；`Get` 在读事务里取字节，`data == nil` 表示键不存在（返回 `false, nil`，不是错误），存在则 `json.Unmarshal` 到调用方的 `v`。**「不存在」与「出错」被明确区分**，这让恢复逻辑可以用 `exists` 分支处理首次启动。

**List 的反射装配**——[pkg/download/storage.go:L196-L217](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go#L196-L217)：`List` 要求 `v` 是「指向切片的指针」（如 `&tasks`），它用 `reflect` 取出切片元素类型，逐键反序列化后 append，最后经 `changeValue`（[pkg/download/storage.go:L25-L47](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go#L25-L47)）写回。`Setup` 里加载全部任务用的就是它。

**MemStorage 的零拷贝**——[pkg/download/storage.go:L49-L110](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go#L49-L110)：双层 map（桶 → 键 → `any`）加一把 `RWMutex`。`Get` 命中后调 `changeValue` 做指针/切片的反射赋值，**不做序列化**。

**选型注入点**——[pkg/rest/server.go:L403-L407](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/rest/server.go#L403-L407)：REST 启动路径按 `startCfg.Storage` 是否为 `StorageBolt` 决定注入哪个实现。而库模式默认值在 [pkg/download/model.go:L246-L248](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L246-L248)——`DownloaderConfig.Init` 里 `Storage == nil` 时兜底 `NewMemStorage()`，这就是 u1-l4 用 `Boot()` 写的迷你下载器「重启即忘」的原因。

#### 4.1.4 代码实践

**实践：亲眼看到 gopeed.db 与桶布局**

1. **实践目标**：确认 BoltStorage 的文件布局与 JSON 值格式，建立对「桶—键—值」的具体感受。
2. **操作步骤**：
   - 在仓库内新建 `_examples/storage-peek/main.go`（`_` 前缀让 `go build ./...` 忽略它；示例代码如下）：

     ```go
     // 示例代码：_examples/storage-peek/main.go
     package main

     import (
         "fmt"
         "github.com/GopeedLab/gopeed/pkg/download"
     )

     func main() {
         dir := "./peek-data"
         s := download.NewBoltStorage(dir)
         defer s.Close()

         // 模拟 Setup 建桶 + 写一条任务
         _ = s.Setup([]string{"task", "save"})
         type demo struct{ Name string; Size int64 }
         _ = s.Put("task", "abc123", &demo{Name: "ubuntu.iso", Size: 100})

         var got demo
         exists, _ := s.Get("task", "abc123", &got)
         fmt.Println("exists:", exists, "value:", got)

         var list []*demo
         _ = s.List("task", &list)
         fmt.Println("list len:", len(list), "first:", list[0])
     }
     ```

   - 运行 `go run ./_examples/storage-peek`，观察输出。
   - 运行后 `ls peek-data/` 应看到 `gopeed.db`；可用 `xxd peek-data/gopeed.db | head -40` 直接观察文件字节——bbolt 页面里能辨认出桶名 `task`、键 `abc123` 和 JSON 值的明文片段。
3. **需要观察的现象**：`exists: true`；`list len: 1`；gopeed.db 中肉眼可见桶名与 JSON 字符串。
4. **预期结果**：桶名与键是裸字节键，值是 JSON 文本。bbolt 是 B+ 树文件，别期望像 CSV 一样可读，但字符串键值会以明文出现在页里。
5. 本实践在无网环境即可完成；若 `xxd` 观察结果与描述有出入，以实际输出为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`MemStorage.Get` 返回的对象与当初 `Put` 的对象是什么关系？这会给依赖 `MemStorage` 的测试带来什么影响？

**答案**：是**同一个对象**（零拷贝引用）。测试里通过 `Get` 拿到对象后修改它，会直接改掉存储里的值，可能与「真实磁盘环境的序列化隔离」行为不一致——例如测试里改一个未导出字段也能生效，而 BoltStorage 路径会因 JSON 序列化丢失未导出字段。写跨存储的测试时要意识到这种语义差异。

**练习 2**：为什么 `NewBoltStorage` 打开失败直接 `panic`，而 `Put` 失败只返回 `error`？

**答案**：打开失败意味着存储介质/路径根本不可用，Downloader 没有任何降级方案，晚崩不如早崩（构造期 panic 会被上层启动逻辑捕获并终止启动）；而 `Put` 失败只是**某一次**写入失败，任务还在内存里正常跑，Downloader 对检查点写入失败的处理是记日志继续（见 4.2.3 的 `persist task checkpoint failed` 日志），下次检查点再试。

**练习 3**：旧版本 gopeed.db（只有 6 个桶）被新版本二进制（需要 8 个桶）打开会发生什么？

**答案**：`Setup` 对每个桶调 `CreateBucketIfNotExists`（[pkg/download/storage.go:L154-L164](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go#L154-L164)），缺的桶会被补建，已有数据不动。所以桶级演进天然向后兼容；字段级演进则依赖 JSON 反序列化「多余字段忽略、缺失字段取零值」的特性（4.3 节 HTTP 的 `SizeKnown *bool` 就是为字段级演进设计的）。

### 4.2 saveTask 与 restoreTask：落盘时机与恢复路径

#### 4.2.1 概念说明

落盘与恢复是一对镜像操作，核心函数三个：

- `saveTask(task)`：**写路径**。一次写两个桶——先 `Manager.Store(fetcher)` 序列化协议状态进 `save` 桶，再把 Task 快照写进 `task` 桶。
- `restoreTask(task)`：**恢复入口**。仅当 `task.fetcher == nil` 时调 `restoreFetcher`，本质是「惰性重建」的守卫。
- `restoreFetcher(task)`：**重建内核**。从 `save` 桶读回协议状态，组装出活的 Fetcher 并重新注入 Controller。

而 `task_stats` 桶由独立的 `captureTaskStats` 写入，与 `saveTask` 解耦——统计是「可丢失的增强信息」，续传数据才是「不可丢失的命脉」，两者写入失败时的容忍度完全不同。

#### 4.2.2 核心流程

先看写路径的全部触发点（谁在什么时候调 `saveTask`）：

```text
saveTask 调用点全景
├─ doStart 异步 handler 末尾        任务真正跑起来之后（首检点）
├─ prepareTask 候选保存             目录/名字决策后、发布前（u4-l2 讲过「先存候选后发布」）
├─ 进度刷新协程                     shouldSaveTaskCheckpoint 通过时（周期检查点，u4-l3）
├─ runPauseHandler                  暂停落定后
└─ Patch                            用户改选项 / 扩展改请求后
```

恢复路径（进程重启后）分三步：

```text
重启恢复三部曲
1. Setup：storage.List(bucketTask) 读出全部 Task 骨架
   ├─ task.Meta == nil → 判为坏数据，从列表剔除
   ├─ assignFetcherManager：按当时 URL 重新路由协议（不落盘，内存字段）
   ├─ initTask：重建 timer/锁/测速窗口等运行时内核
   └─ 非 done/error 的任务一律压成 pause（重启后没有任务自动跑）

2. 惰性 restoreFetcher（真正需要时才发生）：
   ├─ doStart 同步段：restoreTask → restoreFetcher
   ├─ Patch：fetcher 为 nil 时先恢复再改
   └─ Setup 尾部的做种恢复协程：done+Uploading 的任务恢复 fetcher 续做种

3. ContinueOnStartup（可选）：AutoStartTasks 开启时自动 Continue(nil)
```

#### 4.2.3 源码精读

**桶常量**——[pkg/download/downloader.go:L39-L53](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L39-L53) 定义八个桶名。与持久化恢复直接相关的是三个**按任务 ID 分键**的桶：`task`（骨架）、`save`（协议内脏）、`task_stats`（统计快照）——u4-l1 讲过删除任务时三桶同删，本讲在 4.2.3 末尾给出对应代码。

**saveTask 本体**——[pkg/download/downloader.go:L489-L514](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L489-L514)：

- 先调 `task.fetcherManager.Store(task.fetcher)` 拿协议序列化值：非 nil 就 `Put(bucketSave, task.ID, data)`；**nil 就 `Delete(bucketSave, task.ID)` 清掉旧记录**（协议声明「我没有任务级状态」时保持存储干净）。
- 再在 `statusLock` 保护下 `task.clone()` 做深克隆快照，`Put(bucketTask, task.ID, snapshot)` 落盘。快照经过 JSON 序列化，自然剥掉了 fetcher、锁等运行时字段——u2-l2 讲的「公开层/运行时层」二分在这里兑现。

**Setup 的任务恢复段**——[pkg/download/downloader.go:L223-L249](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L223-L249)：

- `storage.List(bucketTask, &tasks)` 读全部骨架；`task.Meta == nil` 的坏任务直接剔除。
- `d.assignFetcherManager(task)`（实现在 [pkg/download/downloader.go:L2292-L2299](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2292-L2299)）按 `task.Meta.Req.URL` 重新跑一遍协议路由——**协议归属不落盘，恢复时以当时 URL 重新判定**（u3-l2 讲过路由贯穿六个时刻，重启恢复是其中之一）。
- `initTask`（[pkg/download/downloader.go:L2689-L2698](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2689-L2698)）重建 timer（用 `task.Progress.Used` 续上已用时间）与四把锁。
- 状态收敛：非 done/error 一律 `pause`。注意 **error 任务保留 error 状态**——重启不清洗历史错误。

**做种恢复协程**——[pkg/download/downloader.go:L264-L290](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L264-L290)：`Setup` 末尾异步遍历任务，对 `done` 且 `Uploading` 的任务调 `restoreTask` 恢复 fetcher 并 `uploader.Upload()` 续做种。这是「惰性恢复」的**一个例外**——做种任务不经过 doStart，必须在这里主动恢复。

**restoreFetcher 本体**——[pkg/download/downloader.go:L1797-L1820](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1797-L1820)，五步走：

1. `v, f := task.fetcherManager.Restore()`：拿「空模板 v + 构造器 f」。
2. `v != nil` 时 `storage.Get(bucketSave, task.ID, v)` 把落盘数据反序列化进模板（v 为 nil 的协议跳过读取）。
3. `task.fetcher = f(task.Meta, v)`：构造器把回填的 v 和任务 meta 组装成 Fetcher；**返回 nil 则兜底 `fm.Build()`**。
4. `d.setupFetcher(...)`：重新注入 Controller（GetConfig/GetProxy/Touch/TempFiles…，u3-l3 讲过的整套中介服务）。**每实例恰好一次**，与 Build 路径对称。
5. meta 三元组回填：Fetcher 的 Req/Res/Opts 任一为 nil 就从 Task.Meta 补上——保证「骨架与内脏」字段一致。

**doStart 里的两个恢复点**——[pkg/download/downloader.go:L2009](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2009) 同步段先 `restoreTask`（重启后首次启动走这里）；异步 handler 里 [L2036-L2040](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2036-L2040) 再兜底一次。恢复成功后 handler 末尾 [L2099-L2101](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2099-L2101) 立刻 `saveTask` 写首检点——把「恢复成功」这个事实本身也落盘。

**task_stats 桶的独立通道**——[pkg/download/downloader.go:L1017-L1019](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1017-L1019) 定义 `storedTaskStats`，值只有一个字段 `Snapshot json.RawMessage`——**协议统计快照以原始 JSON 字节透传存储**，Downloader 不理解其 schema（保持协议无关，u3-l1 讲过的 Snapshot/Runtime 二分）。写入在 [L1074-L1083](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1074-L1083)：仅当快照内容有实质变化时落盘（`bytes.Equal` 去重）；惰性读取在 [L1101-L1119](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1101-L1119)：首次查统计才 `Get(bucketTaskStats)`，之后走内存缓存。特别看 [pkg/download/downloader.go:L997-L999](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L997-L999) 的注释：**统计读取绝不允许触发 save 桶消费**——`Stats()` 只读活 fetcher 或缓存，不为读统计去恢复 fetcher。

**删除与重置的三桶清理**——[pkg/download/downloader.go:L1187-L1195](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1187-L1195) `doDelete` 依次 `Delete` 三个任务级桶。另一处容易忽略：[pkg/download/downloader.go:L1782-L1785](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1782-L1785) `resetTaskFetcher`（换源重解析时）会删掉 `task_stats` 与 `save`——**旧来源的续传数据不能被新来源继承**，否则会「从 A 文件的断点续传 B 文件」。

还有一个值得注意的不对称：任务完成时（watch 循环里 `markTaskDone` 之后）只写 `task` 桶（[pkg/download/downloader.go:L1583-L1592](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1583-L1592)），**不再更新 save 桶**——done 是终态，最后一次检查点的协议内脏留在 save 桶里也无害，等删除时一并清理。

#### 4.2.4 代码实践

**实践：杀进程重启，跟踪三个桶的读取路径**

1. **实践目标**：亲历「下载中途强杀 → 重启 → 恢复」，并搞清楚恢复到底读了哪些桶、**没读**哪个内存结构（`fetcherCache`）。
2. **操作步骤**：
   - 在仓库内新建 `_examples/resume/main.go`（示例代码，基于 u1-l4 的 Boot 风格改用显式 NewDownloader 以便注入 BoltStorage）：

     ```go
     // 示例代码：_examples/resume/main.go
     package main

     import (
         "fmt"
         "os"
         "time"

         "github.com/GopeedLab/gopeed/internal/test"
         "github.com/GopeedLab/gopeed/pkg/base"
         "github.com/GopeedLab/gopeed/pkg/download"
     )

     func main() {
         mode := "start"
         if len(os.Args) > 1 {
             mode = os.Args[1]
         }
         storageDir := "./resume-data"

         cfg := &download.DownloaderConfig{
             StorageDir: storageDir,
             Storage:    download.NewBoltStorage(storageDir), // 关键：磁盘存储
         }
         cfg.Init()
         d := download.NewDownloader(cfg)
         if err := d.Setup(); err != nil { // 重启时在这里读 task 桶
             panic(err)
         }

         switch mode {
         case "start":
             ln := test.StartTestLowSpeedServer(time.Millisecond * 8) // 慢速服务器：每字节延迟 8ms
             defer ln.Close()
             _, err := d.CreateDirect(&base.Request{
                 URL: "http://" + ln.Addr().String() + "/" + test.BuildName,
             }, &base.Options{Path: "./resume-dl"})
             if err != nil {
                 panic(err)
             }
             time.Sleep(30 * time.Second) // 期间 Ctrl+C 杀掉进程
         case "resume":
             tasks, _ := d.GetTasks()
             for _, t := range tasks {
                 fmt.Printf("task %s status=%s downloaded=%d\n", t.ID, t.Status, t.Progress.Downloaded)
             }
             if err := d.Continue(nil); err != nil { // 手动续跑（等价于 AutoStartTasks 的效果）
                 panic(err)
             }
             time.Sleep(60 * time.Second)
         }
     }
     ```

   - 第一遍：`go run ./_examples/resume start`，等进度跑起来后 `Ctrl+C`（或 `kill -9`）强杀。
   - 第二遍：`go run ./_examples/resume resume`，观察打印出的任务状态与已下载字节数。
   - 在 `restoreFetcher`（[pkg/download/downloader.go:L1797](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1797)）和 `Setup` 的任务加载段（[L223-L227](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L223-L227)）各加一行临时日志（`d.Logger.Info().Msgf(...)` 或 fmt.Println），再跑一遍第二遍命令，记录日志触发顺序。
3. **需要观察的现象**：
   - 第二遍启动时任务状态是 `pause`，`downloaded` 保留着被杀前的进度；
   - 日志顺序应为：`Setup` 读 `task` 桶 → `Continue` → `doStart` → `restoreFetcher` 读 `save` 桶；
   - 查看任务统计（若有 BT 任务则更明显）时才可能读 `task_stats` 桶；
   - 全程**没有任何代码读 `fetcherCache`**。
4. **预期结果**：续跑从断点继续（观察磁盘上已下载的部分文件与最终完成大小），且 `fetcherCache` 为空——它是 Resolve→Create 两步 API 之间的**内存**缓存（键是 resolve 请求 ID，不是任务 ID），与持久化恢复无关。重启恢复走的是 `task` 桶 + `save` 桶，不是 fetcherCache。
5. 慢速服务器参数（8ms/字节）是为 200MB 测试文件设计的大约值，若 30 秒内进度过快/过慢可自行调整；具体行为待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Setup` 恢复任务时不立刻 `restoreFetcher`，而要拖到 `doStart`/`Patch`？

**答案**：惰性恢复。重启后可能有几百个暂停任务，立刻全部重建 fetcher（含 HTTP 连接表、BT 客户端句柄）既费内存也费 CPU，而其中大多数可能一直不被启动。拖到真正需要时才恢复，代价按需支付；代价是每次 `doStart` 同步段多一次 nil 判断与可能的重建。

**练习 2**：强杀进程后，`task` 桶与 `save` 桶会不会出现「不一致」？举例说明。

**答案**：会。`saveTask` 写两个桶是**两次独立事务**（见 4.1.3），若进程死在「写完 task、还没写 save」之间，task 桶里的进度比 save 桶里的连接表更新（或相反）。Gopeed 的对策不是加跨桶事务，而是让**恢复侧自愈**：HTTP 的 `fetcherData` 里冗余存了 `Range/ResourceSize/FileSize` 等权威字段（见 4.3.3），恢复时以 save 桶快照为准覆盖 meta，容忍两边短暂不一致。

**练习 3**：`d.GetTasks()` 在重启后返回的任务，其 `fetcher` 字段是什么？`Name()` 还能正常工作吗？

**答案**：`fetcher` 为 nil（骨架状态）。`Name()` 仍可工作：它只依赖 `Meta.Opts.Name`、`Meta.Res`（已随骨架落盘）与 `fetcherManager.ParseName`——后者在 `Setup` 里由 `assignFetcherManager` 重新赋值（[pkg/download/downloader.go:L238](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L238)）。这正是 u2-l2 强调「状态唯一写入者是 Downloader、Fetcher 只是执行者」带来的好处：UI 展示不依赖活 fetcher。

### 4.3 Manager Store/Restore 约定：协议各存什么「内脏」

#### 4.3.1 概念说明

u3-l4 已定义契约（`Store` 返回可序列化值或 nil；`Restore` 返回模板 + 构造器）。本讲的问题是：**一个协议到底该存什么？** 判据只有一条——**重启后无法从其他地方重新推导的信息才值得进 save 桶**：

- 服务器告诉过我们的东西（重定向后的真实地址、Range 是否支持、ETag 校验器）——重启后不重新探测就可能不一致；
- 传输进度的精确位置（每条连接停在哪个字节）——丢掉就要从头下；
- 协议私有累积量（BT 已做种字节数/时长）——丢掉统计就清零。

反过来，**能从磁盘文件或服务器重新推导的就不存**：BT 的 piece 完成状态存在下载目录里（anacrolix/torrent 的存储层自己管），ed2k 的分块校验由其客户端经 `protocol_state` 桶另存。所以三个协议的 `fetcherData` 大小悬殊：HTTP 十个字段、BT 三个字段、ed2k 干脆 nil。

另一个隐藏维度是**并发安全**：`Store` 在下载仍在跑时被周期调用（检查点），它序列化的对象正被下载协程并发修改——协议必须在 `Store` 里做一致性快照，而不是裸返回内部指针。

#### 4.3.2 核心流程

HTTP 一次检查点的快照构造与恢复使用：

```text
Store（周期检查点时调用，下载仍在跑）
  ├─ redirectLock 下取 redirectURL           ← 小锁单独保护
  └─ connMu 下：
       ├─ snapshotConnectionsLocked 深拷贝连接表（含 chunk 指针深拷贝）
       ├─ ifRange / rangeReprobeEligible / rangeValidatorPinned ...
       └─ meta.Res 的 Range/Size/Files[0].Size 冗余进快照
  → 返回不可变 fetcherData，Downloader 落 save 桶

Restore（重启后续跑）
  ├─ 模板 &fetcherData{} ← storage.Get 回填
  ├─ 构造器：新建 Fetcher，回填 connections/redirectURL/ifRange...
  ├─ ParseReqExtra/ParseOptExtra：把 any 类型的 Extra 解析回具体协议结构
  └─ 快照里的 Range/Size 权威字段覆盖 meta.Res
       （自愈 task 桶与 save 桶的撕裂，见练习 2）

Start（fetcher.go）
  └─ downloadLoop：len(connections) > 0 ⇒ isResume
       └─ resumeConnections()：未完成连接重置状态后从各自 chunk 边界继续
```

#### 4.3.3 源码精读

**HTTP：最重的 fetcherData**——[internal/protocol/http/fetcher_manager.go:L16-L27](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go#L16-L27) 十个字段，逐个看存它的理由：

| 字段 | 作用 | 丢了会怎样 |
|---|---|---|
| `Connections` | 每条连接的 chunk 区间与已下载量 | 无法断点续传，从头下 |
| `RedirectURL` | 重定向后的真实地址 | 续传时重新撞一遍重定向，可能拿到不同节点 |
| `IfRange` | ETag/Last-Modified 校验器 | 无法安全续传（服务器文件可能已变更） |
| `RangeReprobeEligible` | 降级顺序下载前服务器曾支持 Range | 丢失「可以重新试探 Range」的机会 |
| `RangeValidatorPinned` | 恢复时必须沿用同一 If-Range 校验器 | 续传请求校验器漂移，206/200 判定错乱 |
| `SequentialSizeUnknown` | 未知大小的顺序流已中断 | 错误地从非零字节重启 chunked 流 |
| `SizeKnown` / `Range` / `ResourceSize` / `FileSize` | 快照时刻的权威元数据（`*bool`/`*int64` 指针类型，nil 专给旧版本记录） | task 桶旧快照覆盖新事实，显示错误大小 |

四个指针字段（`SizeKnown` 等）用「nil 表示旧版本记录没有此字段」的方式做**字段级向后兼容**——JSON 反序列化时缺失字段保持 nil，恢复代码据此判断「信快照还是信 meta」。

**Store 的一致性快照**——[internal/protocol/http/fetcher_manager.go:L81-L125](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go#L81-L125)。这段的注释值得逐字读：值会在 `Store` 返回**之后**才被存储层序列化，如果返回内部活指针，序列化瞬间下载协程可能正在改它，落盘的就是「混合状态」。所以先在 `redirectLock` 下取重定向地址，再在 `connMu` 下深拷贝连接表（[snapshotConnectionsLocked，L129-L153](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go#L129-L153)——注意 chunk 的 `End` 是 `*int64` 指针，openEnded 区间必须递归深拷贝），返回一个**不可变**的 `fetcherData`。

**Restore 的重建与自愈**——[internal/protocol/http/fetcher_manager.go:L155-L192](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go#L155-L192)：构造器里除了常规回填，还调 `base.ParseReqExtra/ParseOptExtra` 把 meta 里 `any` 类型的 Extra 重新解析为 `fhttp.ReqExtra/OptsExtra`（u2-l1 讲过的泛型翻译机制，落盘往返后类型信息丢失，必须重解析）；随后 `fd.Range/ResourceSize/FileSize` 非 nil 时**覆盖 `meta.Res` 对应字段**——注释明说这是「恢复关键字段与 Connections 存在同一快照里，权威性高于 task 元数据」，即对 4.2 练习 2 那种桶间撕裂的自愈。

**续传如何使用快照**——恢复出的 Fetcher 进 `Start` 后，[internal/protocol/http/fetcher.go:L932-L960](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L932-L960) 判断 `len(f.connections) > 0` 即视为 resume，调 [resumeConnections，L2139 起](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2139-L2170)：跳过真正完成的连接、放弃重试耗尽/永久 403 的连接、其余连接重置状态后带着各自 chunk 边界重新发起 Range 请求。字段级细节（慢启动重batch、顺序前缀恢复）属 u5 范围，本讲只需看到「save 桶里的连接表在这里变成真实的续传请求」。

**BT：极简的 fetcherData**——[internal/protocol/bt/fetcher.go:L871-L876](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/bt/fetcher.go#L871-L876) 只有 `Progress`（各文件已下载字节数组）、`SeedBytes`、`SeedTime` 三个字段。**piece 完成状态不在这里**——BT 的传输内核（anacrolix/torrent）通过自己的存储层把 piece 完成位图持久化在下载目录旁（[internal/protocol/bt/fetcher.go:L37-L54](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/bt/fetcher.go#L37-L54) 的 `storage *deferredFileStorage` 字段即其入口，u6-l5 专题讲解）。[Store，L965-L972](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/bt/fetcher.go#L965-L972) 在 `mu` 锁下值拷贝 `*_f.data` 并**单独深拷贝 Progress 切片**（切片是引用，不拷贝就会跟 u4-l3 讲的运行时进度共享底层数组）；[Restore，L974-L981](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/bt/fetcher.go#L974-L981) 把回填的 `fetcherData` 直接挂进新 Fetcher——`Setup`（[L56-L68](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/bt/fetcher.go#L56-L68)）里 `data != nil` 时原样保留，这就是「BT Fetcher 拿回记忆」的全部。

**ed2k：nil 即合法**——[internal/protocol/ed2k/fetcher.go:L429-L440](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ed2k/fetcher.go#L429-L440)：`Store` 返回 `nil, nil`（无任务级状态），`Restore` 返回 nil 模板 + 闭包构造器。ed2k 的续传状态由其传输客户端经协议级状态渠道持久化——这引出最后一块拼图：

**协议级状态：protocol_state 桶**——任务级 `save` 桶之外，还有一种**协议单例级**的状态（ed2k 的节点表、BT 的 tracker 列表等），走 `StatefulFetcherManager` 可选接口。[pkg/download/downloader.go:L209-L217](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L209-L217) 在 `Setup` 时给实现了该接口的管理器注入 `protocolStateStore`；其实现 [pkg/download/downloader.go:L1274-L1292](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1274-L1292) 只有三个方法，全部以**协议名**为键读写 `bucketProtocolState`（注意：不是任务 ID）。任务级与协议级两条持久化通道并行，互不干扰。

#### 4.3.4 代码实践

**实践：dump 三个协议的 save 桶内容**

1. **实践目标**：直观对比 HTTP/BT/ed2k 落盘的 `fetcherData` 差异，验证「存的东西 = 无法重新推导的东西」。
2. **操作步骤**：
   - 复用 4.2.4 的 `_examples/resume` 骨架，`start` 分支分别用 `test.StartTestLowSpeedServer` 的 HTTP 地址、`internal/protocol/bt/testdata/test.torrent` 的文件路径（`file://` 前缀，CreateDirect 支持）和任一 `ed2k://` 链接各建一个任务，下载中途强杀；
   - 写一小段读取代码（示例代码）：

     ```go
     // 示例代码：读取 save 桶原始 JSON
     s := download.NewBoltStorage("./resume-data")
     defer s.Close()
     var tasks []*download.Task
     _ = s.List("task", &tasks) // 借 task 桶拿到全部任务 ID（此处仅示意，实际 Task 为骨架）
     for _, t := range tasks {
         var raw map[string]any
         exists, _ := s.Get("save", t.ID, &raw)
         fmt.Println(t.Protocol, "save bucket:", exists, raw)
     }
     ```

   - 也可以直接用 4.1.4 的 `xxd` 办法在 gopeed.db 里搜 `connections`、`seedBytes` 等字段名。
3. **需要观察的现象**：HTTP 任务的 save 值是一大坨含 `connections` 数组的 JSON；BT 任务的值只有 `progress`/`seedBytes`/`seedTime`；ed2k 任务的键**不存在**（Store 返回 nil 导致 save 桶无记录）。
4. **预期结果**：与 4.3.3 的字段表一一对应。BT 的 piece 状态不在 save 桶，而是伴随下载目录（做种任务的目录里）。
5. ed2k 链接需要一个有效服务器才能跑通传输，若只想验证「save 桶无记录」，在 Resolve 后暂停再查即可；完整行为待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：BT 的 `Store` 为什么要单独 `append(fetcher.Progress(nil), snapshot.Progress...)` 拷贝 Progress，而其他字段直接值拷贝？

**答案**：`fetcherData` 其余字段是值类型（int64），值拷贝即隔离；`Progress` 是切片，赋值只拷贝切片头、共享底层数组。不拷贝的话，返回的「快照」会在存储层序列化那一刻仍被下载协程并发写入——和 HTTP 注释里说的是同一类问题。

**练习 2**：为什么 HTTP 要把 `meta.Res` 的 Range/Size **冗余**一份进 fetcherData，恢复时还用它反向覆盖 meta？

**答案**：task 桶和 save 桶是两次独立写入（4.2 练习 2 的撕裂窗口）。Range 模式与大小是恢复正确性的关键：若 task 桶快照恰好是「降级为顺序模式之前」的旧值，而连接表已是 Range 模式的中间状态，用旧 meta 续传会把连接的 chunk 请求发错。把权威字段与 Connections 放进**同一个** JSON 快照，保证它们原子一致，恢复时以它为准。

**练习 3**：假如你要给 HTTP 加一个「跨重启的失败重试计数」功能，应该存进 `fetcherData`（save 桶）、`Task` 公开字段（task 桶）还是 `protocol_state` 桶？

**答案**：任务级且无法从别处推导 → 三选一先排除 `protocol_state`（那是协议单例级）。再判断它是否属于「协议传输细节」：重试计数影响连接重建策略、与 chunk/连接状态强耦合，放进 `fetcherData` 可与连接表保持原子一致；若只是给 UI 展示的统计，放 `Task` 公开字段（task 桶）即可，还能避免协议层膨胀。实践中先问「谁消费这个值」——传输层消费进 save 桶，UI 消费进 task 桶。

### 4.4 ContinueOnStartup：重启后自动续跑

#### 4.4.1 概念说明

`Setup` 把所有未完成任务压成 `pause`，那么「重启后自动接着下」由谁触发？答案是一个独立的显式步骤 `ContinueOnStartup`：

- 它是**策略**而非机制：机制（恢复任务、惰性 restoreFetcher）都在前面讲完了，它只回答「要不要自动 Continue」。
- 开关是用户级配置 `AutoStartTasks`（默认关闭）。
- 它必须**晚于网络监听就绪**调用——自动续跑的动机常是「服务重启后无人值守继续下」，如果 API 服务器还没起来就开跑，客户端无法观察与干预。

#### 4.4.2 核心流程

```text
进程启动
└─ rest.Start / buildServer
     ├─ initializeCore → NewDownloader + Setup（任务已全部压成 pause）
     ├─ net.Listen(...)                      监听就绪
     └─ Downloader.ContinueOnStartup()
          ├─ cfg.AutoStartTasks == false → 直接返回（保持 pause）
          └─ d.Continue(nil) → continueAll
               ├─ 跳过 running（不可能有）与 done
               ├─ 取前 remainRunningCount（= MaxRunning - 当前运行数）个 → doStart
               │     └─ doStart 同步段 restoreFetcher（惰性恢复在此兑现）
               └─ 其余任务置 wait 入 waitTasks FIFO 队列
```

注意 `Continue(nil)` 的语义细节：过滤条件是「非 running 且非 done」，**error 任务会被重试**——注释（[pkg/download/downloader.go:L913-L915](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L913-L915)）明说这是刻意与 API 的 Continue 全量语义对齐。数量受 `MaxRunning` 软上限约束（u4-l4），放不下的进 wait 队列等 `notifyRunning` 唤醒。

#### 4.4.3 源码精读

**ContinueOnStartup 本体**——[pkg/download/downloader.go:L913-L921](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L913-L921)：三行逻辑，`AutoStartTasks` 关闭时直接 nil；开启时委托 `Continue(nil)`。配置字段定义在 [pkg/base/model.go:L229](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L229)，注释写明语义「backend 启动时继续所有未完成任务」。

**continueAll 的名额分配**——[pkg/download/downloader.go:L870-L897](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L870-L897)：`d.lock` 保护下先算 `remainRunningCount`（MaxRunning 剩余名额），按任务列表顺序取前 N 个非 running/done 任务 `doStart`，**其余就地置 wait 并入队**。这里复用的正是 u4-l4 讲过的 wait 队列机制——自动续跑与手动 Continue 走完全相同的调度路径，没有专用代码。

**调用时机：两个入口**——[pkg/rest/server.go:L129-L131](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/rest/server.go#L129-L131) 是 `Start` 路径（原生模式：先初始化核心与可选 API 配置，再续跑）；[pkg/rest/server.go:L193-L197](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/rest/server.go#L193-L197) 是 `buildServer` 路径（Web/服务端模式：`net.Listen` 成功**之后**才调）。两处失败都只记 Warn 不阻断启动——自动续跑是增值行为，不能拖垮服务可用性。

**与 Setup 收敛的配合**：正因为 `Setup` 已把未完成任务统一压成 pause（[pkg/download/downloader.go:L240-L242](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L240-L242)），`continueAll` 才能用一个简单过滤（非 running 非 done）覆盖 pause、error、wait 全部形态——**先收敛状态，再统一调度**，这是本套恢复设计的第三个不动点（前两个：状态唯一写入者、惰性恢复）。

#### 4.4.4 代码实践

**实践：AutoStartTasks 打开后的无人值守续跑**

1. **实践目标**：验证配置开关 → 自动续跑 → MaxRunning 排队的完整链路。
2. **操作步骤**：
   - 改造 `_examples/resume`：`start` 分支里建完任务后调 `d.PutConfig`（读-改-写，u2-l3 讲过 PutConfig 不做 Merge）把 `AutoStartTasks` 置 true，同时把 `cfg.MaxRunning` 设为 1，并连建 3 个慢速任务；照旧 30 秒后强杀；
   - `resume` 分支**删掉** `d.Continue(nil)` 那行，改成启动一个最小 HTTP 场景或直接调用 `rest` 太重——更简单的办法：在 `Setup()` 之后手动调用 `d.ContinueOnStartup()`（它就是 Start 路径调的那个函数，效果等价）；
   - 观察日志与 `GetTasks` 输出。
3. **需要观察的现象**：重启后 3 个任务自动活动起来——1 个 running、2 个 wait；running 完成后 wait 队首被 `notifyRunning` 唤醒。
4. **预期结果**：全程无人工 Continue 调用；若把 `AutoStartTasks` 留 false 再跑一遍，重启后 3 个任务全部停留在 pause——开关的唯一作用点就是 `ContinueOnStartup` 开头的那个判断。
5. 具体排队时序受任务完成速度影响，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ContinueOnStartup` 不在 `Setup` 里顺便调掉，而要 transport 层显式调？

**答案**：职责与时序。`Setup` 是库级初始化，完成时网络监听、API 服务都还没就绪——此时开跑，客户端既连不上也无法暂停干预；而「监听就绪后再放行」只有传输层（rest）知道。把策略调用权交给 transport，也让 CLI、测试等不需要自动续跑的形态可以不调。

**练习 2**：`AutoStartTasks` 开启时，一个重启前处于 error 的任务会发生什么？这合理吗？

**答案**：会被重试（continueAll 的过滤只排除 running/done）。合理性见仁见智：动机是「无人值守场景下瞬时错误（断网、服务器 5xx）不该要求人工逐个点继续」；代价是永久性错误（404 链接）会在每次重启时白白重试一次。Gopeed 选择了前者，并在注释中明示这是与 API Continue(nil) 对齐的刻意行为。

**练习 3**：`ContinueOnStartup` 调用时 `d.tasks` 里有 wait 状态的任务吗？

**答案**：没有。`Setup` 把所有非 done/error 任务统一压成 pause（wait 会被覆盖），所以重启后首次 Continue 面对的是清一色 pause（外加保留的 error）。wait 只会在 Continue 分配名额时**重新产生**。这是「Setup 收敛 + Continue 再调度」两段式设计的直接推论。

## 5. 综合实践

**给 u3-l4 的 mock:// 协议补上真正的跨进程断点续传。** u3-l4 的实现里 `Store` 返回 nil——任务能跑但重启后从零开始。现在把它变成「能记住自己写到第几个字节」的协议，串联本讲全部知识点：

1. **改造 mockFetcher**（示例代码，基于 u3-l4 的 `_examples/mock`）：

   ```go
   // 示例代码：给 mockFetcher 增加可恢复状态
   type mockFetcher struct {
       manager *mockManager
       meta    *fetcher.FetcherMeta
       ctl     *controller.Controller
       written int64 // 新增：已写入字节数（运行时累积）
       done    bool
   }

   type mockData struct { // 新增：save 桶里的协议内脏
       Written int64 `json:"written"`
   }

   func (m *mockManager) Store(f fetcher.Fetcher) (any, error) {
       mf := f.(*mockFetcher)
       return &mockData{Written: mf.written}, nil // 返回不可变快照
   }

   func (m *mockManager) Restore() (any, func(*fetcher.FetcherMeta, any) fetcher.Fetcher) {
       return &mockData{}, func(meta *fetcher.FetcherMeta, v any) fetcher.Fetcher {
           mf := &mockFetcher{manager: m, meta: meta}
           if data, ok := v.(*mockData); ok && data != nil {
               mf.written = data.Written // 把断点装回新实例
           }
           return mf
       }
   }
   ```

   同时改 mockFetcher 的 `Start`：从 `f.written` 而不是 0 开始写文件（用 `os.OpenFile` + `Seek`，或写前 `Truncate(written)` 再续写），进度返回 `fetcher.Progress{f.written}`。

2. **验证流程**：
   - 参照 4.2.4 把 `_examples/mock` 的 Downloader 换成 `NewBoltStorage`；
   - 第一次运行：下载中途 `Ctrl+C`；用 4.3.4 的 dump 代码查看 save 桶，确认 `{"written":N}` 已落盘；
   - 第二次运行同一程序：任务应以 pause 出现，`Continue` 后从 N 而非 0 继续写，最终文件完整；
   - 加分项：打开 `AutoStartTasks`，第二次运行时不再手动 Continue。

3. **观察要点**：
   - `restoreFetcher` 的五个步骤里，你的断点经过第 2 步（`Get(bucketSave)` 反序列化）和第 3 步（构造器回填）；
   - 若你的 `Store` 忘了在 `Pause`/检查点时被调用，重启后 `written` 会停在最后一次检查点的值——对照 4.2.2 的调用点全景检查哪些路径覆盖了 mock 协议（提示：暂停与周期检查点都会走 `saveTask`）；
   - 故意把 `Restore` 的模板写成 `nil`，观察 `Get(bucketSave)` 被跳过、断点失效——亲手验证「nil 模板 = 不读取」的契约。

## 6. 本讲小结

- `Storage` 接口八个方法，`MemStorage`（零拷贝、随进程消失）与 `BoltStorage`（gopeed.db、每读写一次 JSON 往返、桶幂等创建）双实现，由传输层入口注入，库模式默认内存。
- 任务级持久化分三个桶：`task` 存骨架（Task 深克隆快照）、`save` 存协议内脏（`Manager.Store` 的序列化值，nil 则删旧记录）、`task_stats` 存统计快照（`json.RawMessage` 透传，内容变化才落盘）；删除任务三桶同删，换源重置也清 save。
- `saveTask` 在启动后、prepareTask 候选、周期检查点、暂停、Patch 五类时机被调用；恢复是惰性的——`Setup` 只重建骨架并把非 done/error 压成 pause，`doStart`/`Patch`/做种恢复时才 `restoreFetcher`（模板回填 → 构造器组装 → 重注入 Controller → meta 回填）。
- 协议存什么的判据是「无法重新推导才存」：HTTP 存连接表/重定向/If-Range 校验器/权威元数据（并发安全深拷贝快照 + 指针字段做版本兼容），BT 只存进度与做种统计（piece 状态由 torrent 存储层自管），ed2k 存 nil；协议单例级状态另走 `protocol_state` 桶。
- `ContinueOnStartup` 是显式的启动策略步骤：`AutoStartTasks` 开启且监听就绪后由 rest 层调用，委托 `Continue(nil)` 走与手动续传完全相同的 MaxRunning 调度路径，error 任务也会被重试。

## 7. 下一步学习建议

本讲补全了第四单元「Downloader 核心」的最后一块拼图。接下来：

- **u5 单元（HTTP 深潜）**：本讲只看到 `fetcherData` 被存取，u5-l2/u5-l3 将拆开 chunk 模型、慢启动与 `validateRangeResponse`，解释 `IfRange`/`RangeValidatorPinned` 这些字段为何是续传正确性的命脉。
- **u6-l5（BT 会话存储与恢复）**：承接本讲留下的悬念——BT 的 piece 完成状态如何在 `internal/protocol/bt/storage.go` 里独立持久化，做种任务如何跨重启恢复。
- **u11-l2（并发控制与优雅关闭）**：本讲的强杀实验是「最坏情况」，那一讲分析 `Close()` 如何在正常退出路径上排空在途写入，让检查点尽量完整。
- 若想继续动手：把综合实践的 mock 协议扩展成「多连接 + 各自断点」的迷你版，你会提前撞上 u5-l2 的所有问题（chunk 切分、互助、边界对齐），是极好的预习。
