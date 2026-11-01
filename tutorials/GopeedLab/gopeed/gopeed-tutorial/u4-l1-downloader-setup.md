# u4-l1 Downloader 的结构体与初始化

## 1. 本讲目标

本讲进入 Gopeed 下载引擎的「总指挥室」——`Downloader`。学完本讲你应该能够：

1. 说出 `Downloader` 结构体每一组字段的用途，以及**哪个字段由哪把锁保护**。
2. 理解 `NewDownloader` 与 `Setup` 两级初始化的分工：一个搭内存骨架，一个恢复持久化状态。
3. 逐条列出 `Setup()` 执行的初始化清单，知道每个阶段落盘/启动了什么。
4. 说出全部**八个存储桶**（task / save / task_stats / protocol_state / protocol_jobs / config / extension / extension_storage）分别存放什么、键是什么。
5. 掌握 `DownloaderConfig.Init()` 填充的四个默认值，以及它们为何不能写死在结构体里。

## 2. 前置知识

本讲默认你已完成 u2-l3（配置体系）与 u3-l2（协议路由），以下几个概念会直接用到：

- **两级配置**：`DownloaderConfig` 是进程级配置（存储实现、协议管理器、刷新间隔），由调用方在构造时注入、**不落盘**；`DownloaderStoreConfig` 是用户级配置（下载目录、最大并发等），**持久化**到存储中。前者以匿名内嵌指针的方式持有后者。
- **Fetcher 与 FetcherManager**：FetcherManager 是协议级单例门面（http / bt / ftp / hls / ed2k），Fetcher 是任务级有状态执行者。`Downloader` 持有管理器列表，按 URL 路由（u3-l2）。
- **bbolt**：一个嵌入式键值数据库（类似单文件 SQLite 的 KV 版），数据按「桶（bucket）→ 键 → 值」组织，整库存在一个 `gopeed.db` 文件里。
- **Go 并发原语**：`sync.Mutex`（互斥锁）、`sync.RWMutex`（读写锁）、`sync.Map`（并发安全 map）、`atomic.Bool`（原子布尔）、`context.WithCancel`（可取消上下文）、`sync.WaitGroup`（等待一组协程）。`Downloader` 的字段几乎每个都对应其中一种保护方式。

一个直白的比喻：`Downloader` 像一家下载公司的**总部大楼**——`NewDownloader` 是造楼（立柱上锁、装日志监控），`Setup` 是开业（开仓库、读档案、恢复在途订单、启动保安巡逻）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pkg/download/downloader.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go) | `Downloader` 结构体、`NewDownloader`、`Setup`、存储桶常量、`Close`，本讲主战场 |
| [pkg/download/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go) | `Task` 结构体、`DownloaderConfig` 与其 `Init()` |
| [pkg/download/storage.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go) | `Storage` 接口与 `MemStorage` / `BoltStorage` 双实现 |
| [pkg/download/temp.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/temp.go) | `setupTempDir`：临时目录初始化 |
| [pkg/download/protocol_config.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go) | `initProtocolConfigs`：Setup 阶段的协议配置归一化 |
| [pkg/download/protocol_jobs.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_jobs.go) | `setupProtocolJobs` 与 `protocolScheduler` 定时任务调度器 |
| [internal/tempfiles/scope.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/tempfiles/scope.go) | 临时文件作用域（u3-l3 已精读，本讲只看它在 Downloader 上的挂载点） |
| [internal/logger/logger.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/logger/logger.go) | 日志构造：写文件还是写 stdout |

## 4. 核心概念与源码讲解

本讲的四个最小模块：**Downloader 结构体**、**NewDownloader/Setup**、**存储桶常量**、**DownloaderConfig.Init**。

### 4.1 Downloader 结构体：字段与锁

#### 4.1.1 概念说明

`Downloader` 是 Gopeed 的常驻内存编排中枢：它管理任务列表、调度并发、对接存储、驱动扩展系统和 Blob 注册表。因为会被 REST 服务、FFI 桥接、事件回调等多个 goroutine 同时访问，它的字段必须明确「谁保护谁」。

字段可以分成六组：

1. **生命周期组**：`backgroundCtx` / `cancelBackground` / `closed`——整个 Downloader 的取消信号与关闭标志。
2. **基础设施组**：`Logger`、`ExtensionLogger`、`tempFiles`、`blob`、`webviewProxy`、`webviewProfiles`、`protocolJobs`——日志、临时文件、Blob 资源、WebView 扩展运行时、协议定时任务。
3. **配置组**：`cfg`——唯一的配置入口，内嵌了用户级 `DownloaderStoreConfig`。
4. **任务组**：`tasks`（全部任务）、`waitTasks`（等待调度的队列）、`watchedTasks`（防重复 watch）、`fetcherCache`（已解析未创建的 fetcher）、`listener`（事件回调）。
5. **锁组**：`lock`、`configLock`、`fetcherMapLock`、`checkDuplicateLock`、`webviewProfilesLock`、`webviewProxyLock`。
6. **扩展组**：`extensions`、`extensionGitLock`、`extensionGitWG`、`claimedExtractions`。

#### 4.1.2 核心流程

字段与锁的对应关系（本讲的「地图」，综合实践里要亲手画一遍）：

```text
                    ┌──────────────────────────────────────┐
                    │           Downloader                 │
                    ├──────────────────────────────────────┤
  d.lock ──────────►│ tasks / waitTasks（任务列表与等待队列）│
  d.configLock ────►│ cfg.DownloaderStoreConfig（用户级配置）│
  d.fetcherMapLock─►│ fetcherCache（解析结果缓存）           │
  d.checkDuplicate► │ 同目录重名检查临界区（fetcher.Start 前后）│
  webviewProfilesLock► webviewProfiles                    │
  webviewProxyLock ─► webviewProxy                        │
  sync.Map 自保护 ──► watchedTasks / claimedExtractions    │
  atomic.Bool ─────► closed（关闭标志，无锁读写）            │
  只在构造期写入 ───► backgroundCtx / cancelBackground /     │
                    │ storage / Logger / tempFiles / blob  │
                    │ cfg 的进程级部分（FetchManagers 等）   │
                    └──────────────────────────────────────┘
```

三条使用原则：

- **读多写少用 RWMutex**：`configLock` 与 `fetcherMapLock` 是 `*sync.RWMutex`，读路径（`GetConfig`、`CreateWithOptions` 查缓存）只加读锁。
- **无锁并发用专用结构**：高频且键独立的访问（`watchedTasks`、`claimedExtractions`）用 `sync.Map`；`closed` 用 `atomic.Bool`，`Close()` 里 `Swap(true)` 既置位又判断是否重复关闭。
- **进程级只读字段不加锁**：`storage`、`Logger`、`tempFiles`、`FetchManagers` 在 `Setup` 后不再更换，天然线程安全（各自的内部实现自带锁）。

#### 4.1.3 源码精读

结构体完整定义（注释为讲解所加）：

[downloader.go:L104-L138](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L104-L138) —— `Downloader` 结构体的全部字段：`backgroundCtx`/`cancelBackground` 是全局取消对（`Close` 时先取消它）；`protocolJobs`、`tempFiles` 在 `Setup` 里装配；两把 webview 锁各管一个字段；`cfg` 同时承载进程级与用户级配置；`fetcherCache` 以解析 ID 为键缓存「已 Resolve 还没 Create」的 fetcher；`tasks`/`waitTasks` 是调度核心；四把主锁与 `closed` 原子标志在 L124-L128 集中声明；`claimedExtractions` 记录多卷压缩包解压的认领去重；扩展三件套（列表、git 锁、git WaitGroup）与 `blob` 注册表收尾。

几个容易忽略的字段单独说明：

- [downloader.go:L121](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L121) `watchedTasks sync.Map`：`watch()` 用 `LoadOrStore` 保证同一个任务不会被两个协程同时 watch（见 [downloader.go:L1517-L1523](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1517-L1523)），这是 u4-l3 的伏笔。
- [downloader.go:L128](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L128) `closed atomic.Bool`：`Close()` 第一行 `d.closed.Swap(true)`（[downloader.go:L1225](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1225)）——交换并返回旧值，旧值已是 true 说明别人关过了，直接返回，实现幂等关闭。
- [downloader.go:L116-L118](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L116-L118) `cfg` / `fetcherCache` / `storage`：注意 `storage` 只是 `cfg.Storage` 的一个便捷引用（`NewDownloader` 里直接赋值）。

#### 4.1.4 代码实践

**实践目标**：不运行代码，仅凭源码填出「字段 → 锁」表格，并验证三处锁的真实获取点。

1. **操作步骤**：
   - 在 [downloader.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go) 中搜索 `d.lock.Lock()`，记录所有出现行号，归纳它们都在修改哪些字段。
   - 搜索 `d.configLock` 与 `d.fetcherMapLock`，分别找出一个读锁调用点和一个写锁调用点。
   - 搜索 `d.checkDuplicateLock.Lock()`，确认它只出现在一个函数里。
2. **需要观察的现象**：`d.lock` 的加锁点几乎全部包裹 `d.tasks` / `d.waitTasks` 的读写；`checkDuplicateLock` 只在 `doStart` 的 handler 中按需获取。
3. **预期结果**：你能画出与 4.1.2 一致的对应表，并能说出 `GetTasks`（[downloader.go:L1324-L1329](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1324-L1329)）为什么必须加 `d.lock` 而不是 `configLock`。
4. 本实践为源码阅读型，无需运行，结论可直接在代码里核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fetcherCache` 用 `RWMutex` 而 `tasks` 用普通 `Mutex`？
**答案**：`fetcherCache` 的访问模式是「解析时写入一次、创建时读一次、创建后删除」，且 `Resolve`（写）频率远低于并发查询；`tasks` 的操作常伴随复合动作（遍历计数、切片删除、追加后立刻启动任务），读写难以拆分成独立的临界区，统一互斥更简单也更不易出错。

**练习 2**：`extensions` 列表没有出现在 4.1.2 的锁表里，它怎么保证并发安全？
**答案**：它不由 `d.lock` 保护。列表的追加/删除发生在扩展安装与删除路径（如 [extension.go:L75](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/extension.go#L75)、[extension.go:L167](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/extension.go#L167)），后台 git 拉取由 `extensionGitLock`/`extensionGitWG`（[extension.go:L204-L218](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/extension.go#L204-L218)）串行化与计数；列表本身的线程安全更多依赖 API 层的调用串行性，这是阅读时要留意的「软约定」。

**练习 3**：`closed` 为什么不用 `d.lock` 保护？
**答案**：`closed` 是关闭流程的入口标志，而 `Close()` 里会调用 `pauseAll` 等需要获取 `d.lock` 的函数；如果读取 `closed` 也要先拿 `d.lock`，就可能与已持有 `d.lock` 并检查 `closed` 的路径（如速度循环里 `d.closed.Load()`）互相等待。原子布尔让「检查是否已关」成为零等待操作。

### 4.2 NewDownloader 与 Setup：两级初始化

#### 4.2.1 概念说明

初始化刻意拆成两步：

- **`NewDownloader(cfg)`**：只做「内存世界」的构造——填默认值、创建取消对、初始化 map 与锁、创建日志。**不碰任何持久化状态**，失败只会 panic（如日志目录不可写）。
- **`Setup()`**：做「持久化世界」的恢复——建桶、读配置、迁移旧配置、归一化协议配置、恢复任务与扩展、启动后台协程。返回 `error`，任何一步失败都应该让进程放弃启动。

这么拆的原因：`NewDownloader` 可以在不知道存储内容时被调用（比如先构造再决定日志级别），而 `Setup` 依赖存储已就绪；同时 `Boot()`（库入口）和各产品入口（cmd/web 等）都能复用同一条 `NewDownloader → Setup` 流水线。

#### 4.2.2 核心流程

`NewDownloader` 的五步（伪代码）：

```text
NewDownloader(cfg):
  1. cfg 为空则给空配置；cfg.Init()          # 填默认值（见 4.4）
  2. 创建 backgroundCtx / cancelBackground    # 全局取消对
  3. 组装结构体：fetcherCache、waitTasks、storage、四把锁、extensions
  4. 创建两个 Logger（core.log / extension.log，路径在 StorageDir/logs 下）
  5. ProductionMode 时挂接 panic 崩溃日志（crash.log）
```

`Setup` 的十二步清单（这是本讲要求背下来的「开业检查表」）：

```text
Setup():
   1. setupTempDir()                 # 临时目录：默认 os.TempDir()，MkdirAll(0700)，建 tempFiles 作用域
   2. blob = NewRegistry("")         # Blob 资源注册表（扩展在内存中产出的资源）
   3. storage.Setup(八个桶)          # 建桶（bbolt 里是 CreateBucketIfNotExists）
   4. 读 bucketConfig/config          # 有则装载用户级配置；无则标记 FirstLoad=true
   5. MigrateLegacyExtraCategories    # v2.0.0-beta 旧分类迁移，成功则直接落盘
   6. initProtocolConfigs()           # cfg.Init() 补默认 + 逐协议 normalizeProtocol 归一化后落盘
   7. 给实现了 StatefulFetcherManager 的协议挂 protocolStateStore  # 协议级共享状态存取
   8. setupProtocolJobs()             # 收集各协议 CronJob，建 protocolScheduler（尚未启动）
   9. 读 bucketTask 全部任务           # 剔除 Meta 为 nil 的损坏任务；重新路由协议；initTask 重建内核；
                                     #   非 done/error 一律改为 pause
  10. 按 CreatedAt 升序排序           # 任务列表顺序 = 创建时间顺序
  11. 读 bucketExtension 扩展列表；cleanupNonExistingTasks()  # 按配置清理磁盘文件已丢失的任务
  12. 启动三个后台角色：
      a. 上传恢复协程   # done 且 Uploading 的任务恢复 fetcher 并继续做种
      b. 进度刷新协程   # 每 RefreshInterval 毫秒计算速度、落统计快照、发 progress 事件、按需存检查点
      c. protocolJobs.start()  # 每个定时任务一个 loop 协程
```

#### 4.2.3 源码精读

**NewDownloader 全文**：

[downloader.go:L140-L170](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L140-L170) —— 注意四个细节：① `cfg.Init()` 在这里被调用，所以传入的配置对象会被就地补默认值；② `storage: cfg.Storage` 说明存储实现由配置注入（默认 `MemStorage`）；③ 四把锁都在这里 `new` 出来，字段存的是**指针**，保证 `Task` 等结构克隆时不会复制锁；④ [downloader.go:L163-L168](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L163-L168) 先设置 zerolog 的堆栈序列化器，再创建两个日志器，最后 `ProductionMode` 时调用 `logPanic`（[downloader.go:L2281-L2290](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2281-L2290)）用 `debug.SetCrashOutput` 把崩溃重定向到 `logs/crash.log`。

日志器构造见 [logger.go:L21-L52](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/logger/logger.go#L21-L52)：第一个参数为 true 时写 `logPath` 文件（目录不存在会自动创建），否则写 stdout。`NewDownloader` 把 `ProductionMode` 传给了这个参数——**生产模式日志进文件、开发模式日志上屏幕**，这是综合实践要观察的现象之一。

**Setup 逐段精读**：

- [downloader.go:L172-L181](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L172-L181) —— 前三步：`setupTempDir`（实现见下）、Blob 注册表（[internal/blob/registry.go:L94](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/blob/registry.go#L94) `NewRegistry`）、`storage.Setup` 传入**八个桶名**。`MemStorage.Setup` 只是建空 map（[storage.go:L61-L70](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go#L61-L70)）；`BoltStorage.Setup` 则在写事务里 `CreateBucketIfNotExists`（[storage.go:L154-L164](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go#L154-L164)）。
- [downloader.go:L182-L217](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L182-L217) —— 配置三连：读取 `bucketConfig` 中键为 `"config"` 的用户级配置（不存在则造一个 `FirstLoad: true` 的空配置）；执行旧分类迁移并直接 `storage.Put`（注释解释了为何不走 `PutConfig`：此刻配置尚未 Init，校验会失败，这是 u2-l3 讲过的失败安全迁移范本）；随后 `initProtocolConfigs()`（[protocol_config.go:L154-L172](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L154-L172)：先 `cfg.Init()` 种默认值、校验镜像配置、逐协议 `normalizeProtocol` 归一化，最后整体落盘）。接着遍历 `FetchManagers`，实现了 `StatefulFetcherManager` 接口（[fetcher.go:L145-L149](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L145-L149)）的协议会拿到一个 `protocolStateStore` 适配器，它把 `Load/Save/Delete` 映射到 `bucketProtocolState` 桶中以协议名为键的记录（[downloader.go:L1274-L1292](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1274-L1292)）。
- [downloader.go:L219-L249](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L219-L249) —— 先 `setupProtocolJobs()`（[protocol_jobs.go:L48-L88](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_jobs.go#L48-L88)：遍历实现了 `CronProvider` 的协议，校验每个 CronJob 的名字/周期/超时，从 `bucketProtocolJobs` 恢复上次执行状态，只登记不启动）；然后从 `bucketTask` 列出全部任务，**从后往前遍历**删除 `Meta == nil` 的损坏任务（倒序遍历是为了切片删除不跳元素），对幸存任务调用 `assignFetcherManager`（按当时 URL 重新路由协议）、`initTask`（重建锁、timer、测速窗口等运行时内核，见 [downloader.go:L2689-L2698](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2689-L2698)），并把非 done/error 的状态统一压成 `pause`——这正是 u2-l2 说过的「重启后回落 pause」。最后按 `CreatedAt` 排序。
- [downloader.go:L251-L262](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L251-L262) —— 装载扩展列表，然后 `cleanupNonExistingTasks()`（[downloader.go:L401-L442](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L401-L442)）：配置开关打开时，对每个任务算出目标路径并 `os.Stat`，文件已不存在的任务直接走 `Delete` 清掉。
- [downloader.go:L264-L291](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L264-L291) —— 上传恢复协程：快照任务列表（持 `d.lock` 拷贝切片后立刻释放），逐个对「done 且 `Uploading`」的任务 `restoreTask` 并调用 `uploader.Upload()` 继续做种（细节留给 u6-l3）。
- [downloader.go:L292-L391](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L292-L391) —— 进度刷新协程：`for !d.closed.Load()` 主循环，每轮快照任务、逐任务 `TryLock`（拿不到锁说明正在被暂停/准备，跳过本轮）、计算速度与上传量、`captureTaskStats` 落统计快照、发 `EventKeyProgress`、按 `shouldSaveTaskCheckpoint`（[downloader.go:L395-L397](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L395-L397)，判据是「下载数据变了 / 上传数据变了 / 正在上传且到 30 秒检查点」）决定是否 `saveTask` 存检查点，最后 `Sleep(RefreshInterval)`。循环体逐行的讲解属于 u4-l3，本讲只需知道它在这里启动。
- 最后一行 [downloader.go:L391](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L391) `d.protocolJobs.start()`：为每个登记的定时任务启动一个 loop 协程（[protocol_jobs.go:L89-L94](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_jobs.go#L89-L94)），调度细节在 u6-l4。

**临时目录初始化**：

[temp.go:L12-L26](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/temp.go#L12-L26) —— `setupTempDir`：未配置则用 `os.TempDir()`；转绝对路径；`MkdirAll(root, 0700)`；最后创建 `tempfiles.Scope` 赋给 `d.tempFiles`。注意文件头注释的承诺：**初始化从不清空目录内容，关闭时只删除本 Downloader 自己 Track 过的路径**——因为系统临时目录是与 OS 共享的，不能乱删（Scope 语义在 u3-l3 已讲）。

顺带一提库入口：[downloader.go:L2768](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2768) 的包级单例 `defaultDownloader = NewDownloader(nil)` 与 [downloader.go:L2799-L2805](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2799-L2805) 的 `Boot()`——后者就是对单例调用 `Setup()` 再返回链式构造器，这就是 u1-l4 里 Boot API 背后的真身。

#### 4.2.4 代码实践

**实践目标**：数一数 `Setup` 到底做了多少事，把 4.2.2 的十二步清单与源码一一对应。

1. **操作步骤**：打开 [downloader.go:L172-L393](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L172-L393)，对每一步在源码行旁写下步骤编号；特别标出三处会**写存储**的调用（提示：迁移落盘、`initProtocolConfigs` 落盘、任务排序前后有没有写？）。
2. **需要观察的现象**：十二步中只有两处显式 `storage.Put`（迁移与协议配置初始化），任务列表在 Setup 阶段**只读不写**——重启后任务状态被改成 pause 这件事发生在内存里，并不立即落盘。
3. **预期结果**：你得到的清单与 4.2.2 一致；并能回答「为什么 Setup 不把改过的 pause 状态立刻写回 bucketTask」。
4. 参考答案：因为落盘代价应当摊到真正发生变化时（进度循环的 `saveTask`、暂停时的 `runPauseHandler` 都会写），Setup 只负责让内存视图正确。

#### 4.2.5 小练习与答案

**练习 1**：为什么恢复任务时要从后往前（`i := len(tasks) - 1; i >= 0; i--`）遍历？
**答案**：循环体里会用 `append(tasks[:i], tasks[i+1:]...)` 删除损坏任务。Go 切片删除会使后续元素前移，若正向遍历，删除第 i 个后下一个被检查的元素实际是原来的 i+2 个；倒序遍历则未检查的部分不受影响，逻辑正确且无需额外索引回退。

**练习 2**：如果 `Setup()` 在第 9 步（读任务）返回错误，前面已经创建的资源会怎样？
**答案**：`Setup` 直接向上返回错误，此时 Blob 注册表、桶、后台协程都没有启动（它们要么在更早步骤完成、要么在最后），但**存储本身没有关闭**。调用方应当随后调用 `Close()`（各产品入口的 defer 链会做），`Close` 的幂等设计（`closed.Swap(true)`）保证了这一点。这也提醒我们：`Setup` 失败不等于可以丢弃 Downloader 实例不管。

**练习 3**：`waitTasks` 队列在重启后为什么是空的？
**答案**：`waitTasks` 从不持久化，只存在于内存。重启时所有非 done/error 任务都被压成 `pause`（第 9 步），等待队列由后续 `Continue`/`Create` 时的调度逻辑（`notifyRunning`、`continueAll`）重新填充。wait 本来就是「纯调度概念」（u2-l2 的结论），落盘它没有意义。

### 4.3 存储桶常量：八个子世界的键值布局

#### 4.3.1 概念说明

`Downloader` 把全部持久化数据放进**八个桶**里。桶是 bbolt 的「命名空间」，等价于一张只有两列（key → value JSON）的表。把不同性质的数据分桶，一是隔离（删任务不会碰配置），二是让 `MemStorage` 与 `BoltStorage` 能用同一套桶名开关。

大纲里提到「七个桶」，那是清单的简写——当前 HEAD 的代码里实际定义了八个（`extension_storage` 是扩展专属 KV 存储的第八个桶）。

#### 4.3.2 核心流程

| 桶名常量 | 桶名 | 键 | 值（JSON） | 主要写入者 |
| --- | --- | --- | --- | --- |
| `bucketTask` | `task` | 任务 ID | `Task` 快照（`task.clone()`） | `doCreate`/`saveTask`/暂停/删除 |
| `bucketSave` | `save` | 任务 ID | fetcher 私有续传数据（协议自定义） | `saveTask` → `Manager.Store` |
| `bucketTaskStats` | `task_stats` | 任务 ID | `{"snapshot": ...}` 统计快照 | `captureTaskStats` |
| `bucketProtocolState` | `protocol_state` | 协议名（如 `bt`） | 协议级共享状态（如 tracker 列表） | `protocolStateStore.Save` |
| `bucketProtocolJobs` | `protocol_jobs` | `协议/任务名`（如 `bt/update-trackers`） | 定时任务执行状态 | `protocolScheduler` |
| `bucketConfig` | `config` | 固定 `"config"`（单键） | `DownloaderStoreConfig` 用户级配置 | `initProtocolConfigs`/`PutConfig` |
| `bucketExtension` | `extension` | 扩展 Identity | `Extension` 元数据 | 扩展安装/升级/删除 |
| `bucketExtensionStorage` | `extension_storage` | 扩展自定义键 | 扩展自己的 KV 数据 | 扩展 `storage` API |

两类键的设计取向：

- **按任务 ID 分散**：task / save / task_stats 三个桶共用任务 ID 作键，删除任务时三桶同删（见 `doDelete`：[downloader.go:L1187-L1195](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1187-L1195)）。
- **按名字聚集**：config 只有一个键；protocol_state / protocol_jobs / extension 系列以「协议名 / 扩展身份」为键，天然一协议一条。

#### 4.3.3 源码精读

[downloader.go:L36-L54](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L36-L54) —— 八个桶常量与 `uploadCheckpointInterval`（30 秒上传检查点）集中定义，每个常量带一行注释说明用途；`Setup` 在 [downloader.go:L179](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L179) 把这个切片原样交给 `storage.Setup`。

存储接口与双实现：

- [storage.go:L13-L23](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go#L13-L23) —— `Storage` 接口的八个方法：`Setup`（建桶）、`Put/Get/List/Pop/Delete`（CRUD）、`Close/Clear`。`Downloader` 只认识这个接口，不关心背后是内存还是 bbolt。
- [storage.go:L49-L59](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go#L49-L59) —— `MemStorage`：`map[bucket]map[key]any` 加一把 RWMutex，**存的是 Go 原值而不是 JSON**，所以进程退出数据全部丢失，适合测试与临时用途（也是 `DownloaderConfig.Init` 的默认值）。
- [storage.go:L130-L152](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go#L130-L152) —— `BoltStorage`：库文件名固定 `gopeed.db`（L131 常量 `dbFile`），`NewBoltStorage(dir)` 会先 `MkdirAll` 再 `bbolt.Open`，打开失败直接 panic。`Put/Get` 的值都经 `json.Marshal/Unmarshal`（[storage.go:L166-L194](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go#L166-L194)），因此**任何放进桶里的结构体都必须可 JSON 序列化**——这解释了 `Task` 为何精心设计 `MarshalJSON`（u2-l2 讲过）。

一个把三桶联系起来的例子——`saveTask`（[downloader.go:L489-L514](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L489-L514)）：先让协议管理器把 fetcher 序列化进 `bucketSave`（返回 nil 则删掉旧记录），再把 `task.clone()` 快照写进 `bucketTask`。一次检查点落盘 = 两个桶各写一条。

#### 4.3.4 代码实践

**实践目标**：验证「删除任务会清空三个桶」。

1. **操作步骤**：在综合实践的可运行程序（见第 5 节）里，把存储换成 `NewBoltStorage(dir)`；创建一个 HTTP 下载任务等它完成；然后调用 `d.Delete(&download.TaskFilter{IDs: []string{taskID}}, false)`；最后用 `bbolt` 的只读视图（或直接 `ls -la` 看 `gopeed.db` 大小变化前后对比）检查。
2. **需要观察的现象**：`task`、`save`、`task_stats` 三个桶中该任务 ID 的记录全部消失；`config` 桶不受影响。
3. **预期结果**：与 [downloader.go:L1187-L1195](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1187-L1195) 的三次 `storage.Delete` 一一对应。
4. 若本地没有现成的 bbolt 查看工具，可用 `go run` 写五行只读程序打开 db 遍历桶名（`tx.ForEach`），**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `bucketSave` 的值由协议自己序列化（`Manager.Store`），而 `bucketTask` 的值由 Downloader 统一序列化？
**答案**：`Task` 是协议无关的公共模型，Downloader 知道怎么存它；而 fetcher 的续传数据（HTTP 的分块表、BT 的 piece 完成位图）是协议私有结构，Downloader 不应理解其内容，只当不透明字节搬运——这正是 u3-l1「Store/Restore 两段式契约」的落点。

**练习 2**：`MemStorage` 里 `Get` 用 `changeValue` 直接把存的值反射赋给目标，这和 `BoltStorage` 的 JSON 往返有什么语义差异？
**答案**：`MemStorage` 存的是原对象的引用/副本，类型必须与读取目标匹配或可转换；`BoltStorage` 一律走 JSON，多一层「序列化快照」语义——取出来的一定是落盘时刻的深拷贝。这带来一个测试陷阱：用 `MemStorage` 写的测试可能因为共享引用而「假通过」，换 `BoltStorage` 才暴露未导出字段序列化丢失之类的问题。

**练习 3**：`protocol_state` 桶为什么以协议名而不是任务 ID 为键？
**答案**：它存的是**协议级共享状态**（例如 BT 全局的 tracker 订阅列表），所有同协议任务共享一份；与任务无关，自然不该按任务分键。对照 `save` 桶（任务级续传数据），两者粒度刻意不同。

### 4.4 DownloaderConfig.Init：进程级配置的默认值

#### 4.4.1 概念说明

`DownloaderConfig` 是调用方与 Downloader 之间的「装机单」：存储实现、协议管理器集合、刷新间隔、存储/临时目录、WebView 提供者、生产模式开关，以及内嵌的用户级 `DownloaderStoreConfig`。`Init()` 的职责是**给零值字段补默认**，让调用方只填自己关心的项（这正是 u1-l4 里 `Boot()` 能零配置启动的原因）。

它只在两个时机被调用：`NewDownloader`（[downloader.go:L144](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L144)）与 `Setup` 内 `initProtocolConfigs` 里对内嵌配置的 `cfg.Init()`（不同对象：前者是进程级配置，后者是 `DownloaderStoreConfig` 的方法，u2-l3 已讲）。

#### 4.4.2 核心流程

`Init()` 的四个默认值填充规则（伪代码）：

```text
Init():
  if Controller == nil:    Controller = controller.NewController()
  if len(FetchManagers) == 0:
      FetchManagers = [ftp, hls, http, bt, ed2k]   # 顺序即协议路由优先级！
  if RefreshInterval == 0: RefreshInterval = 350    # 毫秒
  if Storage == nil:       Storage = NewMemStorage()
  return cfg                                # 支持链式调用
```

关键约束：

- **顺序敏感**：`FetchManagers` 是 first-match-wins 路由（u3-l2），FTP 必须排在按扩展名匹配的协议之前，否则 `ftp://host/x.torrent` 会被 BT 抢走；源码注释原样写了这个理由。
- **整体替换**：只要你传入了自己的 `FetchManagers`（例如 u3-l4 的 mock 协议），默认五个协议**全部消失**——想保留必须自己拼全列表。
- **零值即未提供**：四个判断都用「是否为零值」，所以 `RefreshInterval` 无法显式配置成 0（会被当作未设置），这是 Go 配置结构的常见取舍。

#### 4.4.3 源码精读

[model.go:L211-L225](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L211-L225) —— `DownloaderConfig` 字段全表：`Controller`（共享服务包，u3-l3）、`FetchManagers`（协议门面列表）、`RefreshInterval`（进度刷新毫秒数）、`Storage`/`StorageDir`/`TempDir`（存储实现与两个目录）、`WhiteDownloadDirs`（下载目录白名单，匹配逻辑在 `initDownloadPath`：[downloader.go:L1913-L1931](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1913-L1931)）、`WebViewProvider`（WebView 扩展运行时注入点，u9-l4）、`ProductionMode`（日志与崩溃文件开关），最后匿名内嵌 `*base.DownloaderStoreConfig`。

[model.go:L227-L250](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L227-L250) —— `Init()` 全文。L233-L241 的注释直接解释了 FTP 排在最前的原因（「FTP 仅按 scheme 匹配，必须先于按文件扩展名匹配的管理器，`ftp://host/x.torrent` 或 `x.m3u8` 才会作为 FTP 文件下载」）。L243-L248 分别补 `RefreshInterval = 350` 与 `Storage = NewMemStorage()`。

对照 `Task` 结构体（[model.go:L35-L74](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L35-L74)）可以看清两个「运行时内核」的对应关系：`Task` 的 `statusLock/lock/statsLock/blobRefLock/speedArr` 等字段没有 JSON 标签、不落盘，落盘恢复后由 `initTask`（[downloader.go:L2689-L2698](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2689-L2698)）重建——`Downloader` 的锁在 `NewDownloader` 建，`Task` 的锁在 `initTask` 建，模式一致。

#### 4.4.4 代码实践

**实践目标**：验证「传入自定义 FetchManagers 会整体替换默认协议」。

1. **操作步骤**：在综合实践程序（第 5 节）基础上，给 `DownloaderConfig` 填入 `FetchManagers: []fetcher.FetcherManager{new(http.FetcherManager)}`（需要 import `internal/fetcher` 与 `internal/protocol/http`——注意它们在 `internal/` 下，**只有在 gopeed module 内部才能引用**，因此本实践要在仓库内建一个 `_examples` 同级的临时包，或直接写测试）。
2. **操作步骤（替代方案）**：在仓库内新建 `pkg/download/` 的临时测试文件，构造只含 HTTP 管理器的配置，`Setup` 后调用 `d.Resolve(&base.Request{URL: "magnet:?xt=urn:btih:..."})`（任意磁力链接）。
3. **需要观察的现象**：`Resolve` 返回 `ErrUnSupportedProtocol` 错误。
4. **预期结果**：证明默认五协议被整体替换，BT 不再注册；记得删除临时测试文件，不要改动仓库源码。
5. 本实践结论也可纯读代码得出（`if len(cfg.FetchManagers) == 0` 的分支条件），运行验证为可选，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`Boot()`（[downloader.go:L2799-L2805](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2799-L2805)）从未传入配置，它的存储是什么？日志写到哪里？
**答案**：`defaultDownloader = NewDownloader(nil)`，`Init()` 给空配置补 `MemStorage`——所以 Boot API 的任务**不持久化**，进程退出即失；`ProductionMode` 为 false，日志写 stdout；`StorageDir` 为空字符串时日志路径退化为相对路径 `logs/core.log`（`filepath.Join("", "logs", "core.log")`），即当前工作目录下的 logs 子目录。

**练习 2**：想把 Gopeed 嵌进自己的服务并持久化任务，最少要配置 `DownloaderConfig` 的哪几个字段？
**答案**：`Storage: NewBoltStorage(dir)` 与 `StorageDir: dir` 两个即可（`StorageDir` 同时决定日志目录）。其余字段全有默认值；`TempDir` 不填则共享系统临时目录。

**练习 3**：`RefreshInterval` 设成 100 会有什么影响？
**答案**：进度刷新协程每 100ms 轮询一次全部 running 任务：速度计算窗口更细腻（u2-l2 的 5 秒窗口内样本数变多）、progress 事件频率变为 10 次/秒、`saveTask` 检查点判断频率也随之提高。代价是 CPU 与存储写入次数上升，收益是 UI 更顺滑——这是一个典型的可调参数权衡。

## 5. 综合实践

把本讲四个模块串成一个可运行实验：**用显式配置（含 `MemStorage`）启动一个 Downloader，观察日志文件、临时目录的创建位置，并列出 Setup 的初始化步骤**；再换成 `BoltStorage` 对比差异。

### 5.1 实践目标

1. 亲手走一遍 `NewDownloader → Setup → Close` 生命周期。
2. 验证 4.2 的十二步清单中肉眼可见的部分（日志文件、临时目录、数据库文件）。
3. 画出 Downloader 字段与锁的对应关系图（4.1.2 的自绘版）。

### 5.2 示例代码

以下为**示例代码**（非项目原有文件），放在 gopeed module 之外的独立 module 中运行，避免改动仓库：

```go
// setup-demo/main.go —— 观察初始化副作用
package main

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/GopeedLab/gopeed/pkg/download"
)

func main() {
	// 1. 准备两个目录：存储目录（放日志）与临时目录
	baseDir, err := os.MkdirTemp("", "gopeed-setup-*")
	if err != nil {
		panic(err)
	}
	defer os.RemoveAll(baseDir)
	storageDir := filepath.Join(baseDir, "storage")
	tempDir := filepath.Join(baseDir, "tmp")

	// 2. 显式配置：Storage 留空 -> Init() 默认 MemStorage
	//    ProductionMode=true -> 日志写文件 + crash.log
	cfg := &download.DownloaderConfig{
		StorageDir:     storageDir,
		TempDir:        tempDir,
		ProductionMode: true,
	}

	// 3. 两级初始化
	d := download.NewDownloader(cfg) // 内存骨架：默认值、锁、日志
	if err := d.Setup(); err != nil { // 持久化恢复：建桶、配置、后台协程
		panic(err)
	}
	defer d.Close() // 幂等关闭：停协程、关存储、清临时文件

	// 4. 观察副作用
	fmt.Println("== 初始化后观察 ==")
	fmt.Println("TempDir 存在:", dirExists(cfg.TempDir))
	logDir := filepath.Join(storageDir, "logs")
	for _, f := range listDir(logDir) {
		fmt.Println("日志文件:", f) // 预期: core.log / extension.log / crash.log
	}
	fmt.Println("gopeed.db 存在:", fileExists(filepath.Join(storageDir, "gopeed.db"))) // 预期: false（MemStorage）
	fmt.Println("任务数:", len(d.GetTasks())) // 预期: 0
}

func dirExists(p string) bool { info, err := os.Stat(p); return err == nil && info.IsDir() }
func fileExists(p string) bool { _, err := os.Stat(p); return err == nil }
func listDir(p string) []string {
	entries, err := os.ReadDir(p)
	if err != nil {
		return nil
	}
	var names []string
	for _, e := range entries {
		names = append(names, e.Name())
	}
	return names
}
```

### 5.3 操作步骤

1. 在仓库外创建独立 module 并用 `replace` 指向本地源码：

   ```bash
   mkdir -p ~/setup-demo && cd ~/setup-demo
   go mod init setup-demo
   go mod edit -replace github.com/GopeedLab/gopeed=/path/to/gopeed   # 换成你的仓库绝对路径
   go mod tidy && go run .
   ```

2. 记录输出中的三行观察结果（临时目录、日志文件列表、gopeed.db）。
3. 把 `Storage` 字段改为 `Storage: download.NewBoltStorage(storageDir)`（构造移到配置前），再跑一次，对比 `gopeed.db` 是否出现。
4. 对照 4.2.2 的十二步清单，在输出旁标注每一步对应源码行号。
5. 最后合上电脑前的最后一题：画出你的「字段 → 锁」图，与 4.1.2 互相校对。

### 5.4 需要观察的现象与预期结果

| 观察 | 预期（MemStorage + ProductionMode） | 换 BoltStorage 后 |
| --- | --- | --- |
| `cfg.TempDir` | 存在，权限 0700（`MkdirAll(root, 0700)`） | 同左 |
| `StorageDir/logs` | `core.log`、`extension.log`、`crash.log` 三个文件 | 同左 |
| `gopeed.db` | **不存在**（内存存储无文件） | 存在于 `storageDir`，含 8 个桶 |
| stdout | 无核心日志（都进了文件） | 同左 |
| `GetTasks()` | 空 | 重跑第二次时能恢复上次任务（若有） |

说明：`crash.log` 来自 `logPanic` 的 `os.Create`（[downloader.go:L2281-L2290](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2281-L2290)）；若 `go mod tidy` 因网络拉取依赖失败，可设置 `GOPROXY` 后重试——本示例在离线环境的可运行性**待本地验证**。

### 5.5 字段与锁参考图（自绘对照答案）

| 字段 | 保护者 | 一句话说明 |
| --- | --- | --- |
| `tasks` / `waitTasks` | `lock`（Mutex） | 任务列表与等待队列，所有增删查排它 |
| `cfg.DownloaderStoreConfig` | `configLock`（RWMutex） | 用户级配置读写 |
| `fetcherCache` | `fetcherMapLock`（RWMutex） | 解析结果缓存（rrId → fetcher） |
| 同目录重名临界区 | `checkDuplicateLock` | 包住「查重改名 + fetcher 创建文件」 |
| `webviewProfiles` / `webviewProxy` | 各自专用 Mutex | WebView 运行时两个资源 |
| `watchedTasks` / `claimedExtractions` | `sync.Map` 自身 | 高频独立键并发访问 |
| `closed` | `atomic.Bool` | 幂等关闭标志 |
| `backgroundCtx` / `storage` / `Logger` / `tempFiles` / `blob` / `cfg` 进程级部分 | 无锁（Setup 后只读） | 构造期写入，运行期只读 |

## 6. 本讲小结

- `Downloader` 的字段按「生命周期 / 基础设施 / 配置 / 任务 / 锁 / 扩展」六组划分，每块数据有明确归属的锁：任务列表归 `lock`，用户配置归 `configLock`，解析缓存归 `fetcherMapLock`，重命名临界区归 `checkDuplicateLock`，高频独立键用 `sync.Map`，关闭标志用原子布尔。
- 初始化分两级：`NewDownloader` 只搭内存骨架（默认值、取消对、锁、日志），`Setup` 负责持久化世界（建桶、读配置、迁移、归一化、恢复任务与扩展、启动三个后台角色）。
- `Setup` 的十二步清单中，只有旧分类迁移与协议配置初始化两处显式落盘；任务恢复只改内存状态（非 done/error 一律压成 pause），落盘推迟到真正发生变化时。
- 持久化由八个桶组成：`task`/`save`/`task_stats` 按任务 ID 分键且删除时三桶同删，`protocol_state`/`protocol_jobs`/`config`/`extension`/`extension_storage` 按名字聚集，`Storage` 接口下挂着 `MemStorage`（内存、默认）与 `BoltStorage`（gopeed.db、JSON 往返）双实现。
- `DownloaderConfig.Init()` 补四个默认值：Controller、五协议管理器（顺序即路由优先级，FTP 必须最前）、RefreshInterval=350ms、MemStorage；传入自定义 `FetchManagers` 会整体替换默认协议。
- `Task` 与 `Downloader` 共享同一套模式：锁都在运行时重建（`NewDownloader` 与 `initTask`），序列化只保存公开层。

## 7. 下一步学习建议

本讲只搭好了舞台，下一讲 **u4-l2 主链路：Resolve → Create → doStart** 将让第一个任务在舞台上跑起来：`Resolve` 如何生成缓存 ID、`CreateDirect/Create` 两条建任务路径的差异、`notifyRunning` 的排队唤醒、`doStart` 里 `restoreFetcher`/`prepareTask`/`fetcher.Start` 的异步结构。建议带着本讲的两个字段先行阅读：`fetcherCache`（谁写入、何时删除）与 `waitTasks`（谁入队、谁消费）。后续 u4-l3 会展开本讲埋下的进度刷新协程，u4-l5 展开存储桶的续传语义。
