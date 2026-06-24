# 数据恢复与同步：ResyncWorker

## 1. 本讲目标

本讲是 storage 服务单元（u5）的收官篇，聚焦一个核心问题：**当一个离线崩溃过的 storage target 重新上线后，它丢失的数据如何被追平？**

读完本讲，你应当能够：

- 说清楚一个 target 从 `OFFLINE` 重启，到恢复 `UPTODATE` 的完整状态流转，以及恢复过程是被谁、如何触发和调度的。
- 解释为什么恢复期间所有写请求（无论是后台追平还是客户端实时写）一律被改造成 **full-chunk-replace（整块替换）**，以及 `dump-chunkmeta` 元数据对比的判定规则。
- 描述 `sync-done` 之后 local state 翻转为 `UPTODATE` 的流程，并理清它与 mgmtd 侧 public 状态机的衔接。

本讲承接 u5-l3（写路径与 CRAQ 链式复制）与 u3-l5（Target 状态机与故障检测）。在 u5-l3 中我们建立了「链头加锁 → 双版本（pending/committed）→ 沿链转发 → ACK 回传」的写模型；在 u3-l5 中我们建立了 mgmtd 的 public 状态机（SERVING/SYNCING/WAITING/LASTSRV/OFFLINE）。本讲要把这两端连起来：mgmtd 把恢复中的 target 标成 `SYNCING` 并置于链尾，storage 数据面的 `ResyncWorker` 在此期间把整条链的数据流式灌给它。

## 2. 前置知识

在进入源码前，先用三段直觉建立认知。

### 2.1 为什么要「整块替换」

回顾 CRAQ 的写模型（u5-l3）：正常写只改 chunk 的一部分，链头分配 `updateVer`、沿链串行转发、链尾提交后 ACK 回传。每个 chunk 维护 `commitVer ≤ updateVer ≤ commitVer+1` 的双版本不变量。

现在设想一个 target C 崩溃重启。它磁盘上的 chunk 可能停留在任意中间状态：某个 chunk 的 `updateVer` 比前驱 B 新（崩溃前收到了 B 转发但还没 ACK），或更旧（崩溃前没收到），甚至带了一个错误的 pending 版本。如果让恢复期间的写仍然走「部分写 + 双版本推进」，C 必须和 B 精确对齐每个 chunk 的版本历史，这在异步崩溃场景下极其脆弱。

3FS 的选择是**降维**：恢复期间，凡是要落到 C 上的写，统统改成「读出整块内容 + 整块覆盖写」。一次 full-chunk-replace 直接把 C 上某个 chunk 的内容、chain version、committed 版本号整体覆盖成和 B 一致，绕开增量版本对齐的复杂性。这就是本讲反复出现的核心机制。

### 2.2 谁是「前驱」，谁是「后继」

CRAQ 链是有方向的：写请求从 head 沿链向 tail 传播。所以一条链 `A → B → C` 中：

- B 把数据**转发**给 C，B 是 C 的**前驱（predecessor）**，C 是 B 的**后继（successor）**。
- 恢复时，**前驱**负责把数据**推给**恢复中的**后继**。

这点容易记反：数据是「前驱主动推给后继」，而不是后继向前驱拉。本讲的 `ResyncWorker` 就运行在前驱节点上，把本机 target 的数据同步给它处于 `SYNCING` 状态的后继。

### 2.3 状态机回顾（承接 u3-l5）

| 角色 | 字段 | 恢复期间取值 | 含义 |
|------|------|------------|------|
| 后继 C（恢复中） | publicState | `SYNCING` | mgmtd 下发：可服务读、数据恢复进行中 |
| 后继 C（恢复中） | localState | `ONLINE` | 本机：进程存活，但尚未追平 |
| 后继 C（恢复完成） | localState | `UPTODATE` | 本机：数据已追平，可转为正式服务 |

`localState` 是 storage 本机自报给 mgmtd 的「输入事件」，`publicState` 是 mgmtd 综合后下发的「输出结论」。恢复的起点是 mgmtd 把 C 翻成 `SYNCING`，终点是 C 自报 `UPTODATE` 后 mgmtd 把它翻成 `SERVING`。中间这段「追平」的脏活累活，就是本讲的主角。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/storage/sync/ResyncWorker.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.h) | 数据恢复 worker 的接口与配置；声明 `handleSync`/`forward` 与 `SyncingStatus` 状态簿记。 |
| [src/storage/sync/ResyncWorker.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.cc) | 恢复主循环与核心算法：扫描 syncing 链、`syncStart` 拉远端元数据、对比生成 write/remove 列表、分批转发、`syncDone` 收尾。 |
| [src/storage/service/StorageOperator.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc) | RPC 落点：`syncStart`（后继吐出全量 chunk 元数据）、`syncDone`（翻转 localState）。 |
| [src/storage/service/TargetMap.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.cc) | 路由维护：`updateRouting` 识别 `SYNCING` 后继并登记 syncing 链、`syncReceiveDone` 翻转状态、`updateLocalState` 的状态推导规则。 |
| [src/storage/service/ReliableForwarding.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/ReliableForwarding.cc) | 链转发器：`doForward` 中「后继处于 SYNCING → 先整块读再转发」的 full-chunk-replace 改造逻辑。 |
| [src/storage/service/Components.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc) | 组件装配：`resyncWorker.start()` 在服务启动序列中的位置。 |
| [docs/design_notes.md](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md) | 设计原文的「Data recovery」一节，是本讲算法的权威描述。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**恢复触发**、**元数据对比**、**全量回放**。

### 4.1 恢复触发：从 offline 到 SYNCING 的状态流转

#### 4.1.1 概念说明

「恢复」不是某个独立服务，而是 storage 主进程内一个**后台 worker**周期性扫描出来的活儿。它的触发链是：

1. 某 storage 节点崩溃 → mgmtd 心跳超时（u3-l2/u3-l5）→ 该节点所有 target 的 local 翻成 `OFFLINE`，mgmtd 把它们挪到链尾。
2. 节点重启后，按设计约定**先拉路由、暂不发心跳**，直到本机所有 target 在最新链表里都被标记 offline——这保证每个 target 都会走一遍恢复流程，而不是「以为自己是正常的」就直接服务。
3. mgmtd 状态机把恢复中的 target 翻成 `SYNCING`（可读、但数据未追平），放在链尾；其前驱则正常 `SERVING`。
4. 前驱节点通过 mgmtd 下发的路由更新发现「我的后继是 `SYNCING`」，于是把这条链登记进 `syncingChains_`。
5. 前驱上的 `ResyncWorker` 周期扫描 `syncingChains_`，对每条链发起一次 `handleSync`。

#### 4.1.2 核心流程

`ResyncWorker` 由三部分组成：一个 `CoroutinesPool`（处理单条链的恢复任务）、一个 `loop()` 周期扫描协程、一个 `Shards<SyncingChainIds>` 状态簿记（防同链并发）。它的启动与其他 worker 一起发生在服务 `beforeStart` 阶段：

[src/storage/service/Components.cc:62](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc#L62) —— 在 Components 启动序列里拉起 `resyncWorker`，排在各数据/元信息 worker 之后。

`loop()` 每 500ms 醒来一次，取一次 `TargetMap` 快照，拿到当前所有 syncing 链，**打乱顺序**后逐条尝试入队：

```text
loop():
  while not stopping:
    wait 500ms
    syncingChains = targetMap.snapshot().syncingChains()
    shuffle(syncingChains)               // 打散，避免热点
    for vChainId in syncingChains:
      加锁检查该 chainId:
        若 !isSyncing 且 距上次同步 > 30s:
          标记 isSyncing = true
          pool_.enqueueSync(vChainId)    // 交给协程池跑 handleSync
        否则: 跳过（正在同步或冷却中）
```

`syncingChains_` 的来源在 `TargetMap::updateRouting`：每当遍历到本机某个 target 的后继处于 `SYNCING`，就把 `(chainId, chainVersion)` 推入列表：

[src/storage/service/TargetMap.cc:231-234](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.cc#L231-L234) —— 后继是 `SYNCING` 时登记 syncing 链（注意只有「直接前驱」的后继才是 SYNCING，因此只有直接前驱会发起同步，链上更上游的节点后继是 SERVING、不触发）。

[src/storage/sync/ResyncWorker.cc:66-99](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.cc#L66-L99) —— 周期扫描与限并发入队，含每链 30 秒冷却。

这里有两个并发控制细节值得注意：

- **防同链重入**：`SyncingStatus{isSyncing, lastSyncingTime}` 记录每条链是否正在被同步。只有「未在同步」且「距上次同步超过 30 秒」才会重新入队，避免同一链被多个协程同时恢复。退出时（哪怕出错）由 guard 把 `isSyncing` 复位、记录 `lastSyncingTime`。
- **打乱顺序**：`std::shuffle` 让多条 syncing 链的恢复顺序随机化，避免固定的恢复顺序在多节点间造成同步式负载尖峰。

#### 4.1.3 源码精读

`SyncingStatus` 与分片状态簿记定义在头文件：

[src/storage/sync/ResyncWorker.h:75-81](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.h#L75-L81) —— `Shards<SyncingChainIds, 32>` 用 32 分片降低锁争用；`requestId_` 为每次 forward 生成唯一请求 id。

恢复 worker 的配置项同样在此：

[src/storage/sync/ResyncWorker.h:28-37](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.h#L28-L37) —— `num_threads`(16)/`num_channels`(1024)/`batch_size`(16，热更新)/`sync_start_timeout`(10s)，以及 `full_sync_level`、`full_sync_chains` 两个用于人工触发「全量重传」的开关（后述）。

#### 4.1.4 代码实践

**实践目标**：确认恢复 worker 在服务中的启动位置与触发源。

**操作步骤**：

1. 打开 `src/storage/service/Components.cc`，定位 `resyncWorker.start()`（约第 62 行），观察它排在哪些 worker 之后——你会看到它紧跟在各 `update`/`sync` worker 之后，但启动顺序上属于「数据面就绪后」。
2. 在 `src/storage/service/TargetMap.cc` 的 `updateRouting`（约 124 行起）里，跟随 `successor` 的赋值逻辑，找到第 231-234 行：确认只有后继 `publicState == SYNCING` 时才 `push_back` 到 `syncingChains_`。
3. 在 `ResyncWorker.cc` 的 `loop()`（66-99 行）里，把 `cond_.wait_for` 的 500ms 与 `lastSyncingTime > 30_s` 两个阈值记下来。

**需要观察的现象**：恢复是「被动反应式」的——`ResyncWorker` 自己不判断 target 是否需要恢复，它只看 `TargetMap` 给它的 `syncingChains_` 列表；而该列表的填充完全取决于 mgmtd 下发的路由里后继是否为 `SYNCING`。

**预期结果**：你应能在脑中画出「mgmtd 翻 SYNCING → 路由下发 → 前驱 updateRouting → syncingChains_ 增项 → loop 扫到 → 入队 handleSync」这条触发链。

**待本地验证**：若要在真实集群观察，可在 admin_cli 触发一次 target 下线/恢复（或重启某 storage 节点），随后在前驱节点日志中 grep `start sync chain` 与 `sync done chain`（见 4.3 的日志点）观察恢复的实际起止。

#### 4.1.5 小练习与答案

**练习 1**：链 `A → B → C`，C 崩溃重启进入 `SYNCING`。为什么是 B 而不是 A 发起对 C 的恢复？

**答案**：恢复由「直接前驱」发起。A 的后继是 B（SERVING），不是 C，因此 A 的 `syncingChains_` 里没有这条链；只有 B 的后继是 C（SYNCING），B 才会把该链登记并恢复。这是 CRAQ 链式结构决定的：只有直接前驱持有需要转发给后继的写流。

**练习 2**：`loop()` 里为什么要在入队前打乱 `syncingChains` 顺序？

**答案**：多条链同时进入恢复时，若总是按固定顺序处理，会在同一批 SSD/网络上形成同步化的突发负载。打乱后各前驱节点的恢复顺序互不同步，把负载在时间维度上摊平。

---

### 4.2 元数据对比：dump-chunkmeta 与 chunk 传输判定规则

#### 4.2.1 概念说明

直接把前驱 B 上所有 chunk 全量传给后继 C 太浪费——很多 chunk 其实已经一致。3FS 的做法是**先比对元数据，再决定传哪些 chunk**。这就是设计文档里说的 **dump-chunkmeta**：

1. 前驱 B 向后继 C 发 `syncStart`，C 遍历本地 chunk 元数据存储，把自己所有 chunk 的 `(chunkId, chainVer, updateVer, commitVer, chunkState, checksum)` 吐给 B。
2. B 同时读出本机 target 的全量 chunk 元数据。
3. B 把两份元数据逐 chunk 对比，按一组**判定规则**分成三类：要传给 C 的（writeList）、要从 C 删掉的（removeList）、已一致可跳过的。

注意：dump-chunkmeta 只传**元数据**（很小的结构体），不传 chunk 数据本身。真正的数据传输发生在下一步（4.3）的 full-chunk-replace 里。

#### 4.2.2 核心流程

`handleSync` 是整个恢复的主体函数，它的骨架是四步：

```text
handleSync(vChainId):
  1. (guard) 退出时复位该链的 isSyncing
  2. 找到本机 target（前驱）、拼一个 clientId
  3. syncStart：向后继发 SyncStartReq，拿到 remoteMetas（后继的全量 chunk 元数据）
     同时 getAllMetadataMap 取本机 localMetas
  4. 逐 remoteMeta 对比，生成 writeList / removeList
  5. 分批 forward 每个 chunk（WRITE 或 REMOVE）
  6. syncDone：通知后继恢复完成
```

`syncStart` 的服务端实现（运行在**后继 C** 上）做三件事：校验状态、吐元数据、二次校验路由版本：

[src/storage/service/StorageOperator.cc:1007-1050](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L1007-L1050) —— `syncStart` 要求本 target 必须 `publicState==SYNCING && localState==ONLINE`，否则报错；随后 `storageTarget->getAllMetadata` 遍历本地 chunk 元数据存储返回全部 `ChunkMeta`；返回前再 `getByChainId` 复查一次路由版本，防止吐元数据期间拓扑已变。

对比与判定规则的核心在 `handleSync` 中段，逐条遍历**远端（后继）**元数据：

[src/storage/sync/ResyncWorker.cc:203-275](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.cc#L203-L275) —— 元数据对比主体。判定逻辑可整理为下表（local = 前驱 B，remote = 后继 C，`needForward=true` 表示要传）：

| 条件（逐 remoteMeta 判断） | 结论 | 对应设计文档规则 |
|------|------|------|
| remote chunk 不在 local 里 | → removeList（C 有但 B 没有，应删） | "只存在于远端 → 删除" |
| local chunk 在回收态（recycleState≠NORMAL） | 跳过（不传，记 WARNING） | 安全保护 |
| `local.chainVer > remote.chainVer` | needForward（传） | "本地链版本更高 → 传" |
| remote 未提交（`updateVer!=commitVer` 或 `chunkState!=COMMIT`） | 记 WARNING，不传（远端有脏数据，但不能由此推本地错） | — |
| `local.chainVer < remote.chainVer` 且 local 已 COMMIT | **fatal**：B 落后于 C 却已提交，理论不该发生，下线后继 | 不变量保护 |
| `local.chainVer < remote.chainVer` 且 local 未 COMMIT | needForward=false（local 正在写） | — |
| 链版本相等但 `local.updateVer != remote.commitVer` | 需结合当前链版本判定（见源码 239-249） | "同链版本但版本号不等 → 传" |
| checksum 不一致 | 结合当前链版本判定，可能 fatal | 内容校验 |
| 全部相等（含 checksum） | needForward=false（已一致，跳过） | — |

遍历完远端后，**仍留在 `localMetas` 里的**（即后继完全没有、本机独有的 chunk）全部加入 writeList——对应规则「只存在于本地 → 传」：

[src/storage/sync/ResyncWorker.cc:289-292](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.cc#L289-L292) —— 远端缺失的本地 chunk 全部加入 writeList，并计入 `remoteMissCount`。

#### 4.2.3 源码精读

`ChunkMeta`（线上的精简元数据）与 `ChunkMetadata`（本机完整元数据）的字段定义在：

[src/fbs/storage/Common.h:537-543](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/storage/Common.h#L537-L543) —— `ChunkMeta` 含 `updateVer/commitVer/chainVer/chunkState/checksum`，正是 `syncStart` 吐回前驱用于比对的字段。

[src/fbs/storage/Common.h:652-676](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/storage/Common.h#L652-L676) —— 本机 `ChunkMetadata` 在 `ChunkMeta` 基础上多了 `recycleState`、`checksumType/checksumValue`、`innerFileId`（含 chunkSize）等，`checksum()` 方法把 type+value 还原成 `ChecksumInfo` 供对比。

对比中的一个关键不变量保护——**fatal 事件处理**：

[src/storage/sync/ResyncWorker.cc:277-287](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.cc#L277-L287) —— 一旦发现「local 已 COMMIT 但 chainVer 反而低于 remote」「checksum 不一致且非当前链版本在写」等违反 CRAQ 不变量的情形，置 `hasFatalEvents`，跳出循环，并主动向后继发 `offlineTarget(force=true)` 把它下线——宁可让 target 停下来人工介入，也不要把损坏扩散。

对比逻辑里反复出现的 `vChainId.chainVer`（当前同步任务对应的链版本）扮演「裁判」角色：当 local/remote 链版本都等于当前任务版本、却仍不一致时，说明是「正在发生的并发写」造成的暂态差异，此时**不**当作 fatal，而是 `needForward=false` 跳过、留给下一轮重试；只有链版本已落后/超前于当前任务、却仍提交一致时，才是真正的不变量违反。

#### 4.2.4 代码实践

**实践目标**：把设计文档的判定规则与源码逐条对应起来。

**操作步骤**：

1. 打开 [docs/design_notes.md:262-271](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L262-L271)，读到 4 条 chunk 传输规则。
2. 打开 [src/storage/sync/ResyncWorker.cc:203-292](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.cc#L203-L292)，为每条规则标注它落在哪个 `if` 分支：
   - 「只存在于本地 → 传」↔ 第 289-292 行（遍历后剩余的 localMetas）。
   - 「只存在于远端 → 删」↔ 第 206-209 行（`it == localMetas.end()` → removeList）。
   - 「本地链版本更高 → 传」↔ 第 223 行（`meta.chainVer > remoteMeta.chainVer`）。
   - 「同链版本但版本号不等 → 传」↔ 第 239 行（`meta.updateVer != remoteMeta.commitVer`）。
3. 阅读 [tests/storage/sync/TestSyncStartAndDone.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/tests/storage/sync/TestSyncStartAndDone.cc)，这是项目自带的 syncStart/syncDone 集成测试，用单节点 + RocksDB 后端构造最小恢复场景。

**需要观察的现象**：源码比设计文档多出来的部分——recycleState 跳过、fatal 不变量保护、当前链版本 `vChainId.chainVer` 作为暂态/真错的裁判——都是为了在「恢复与正常写并发」的现实中保证安全。

**预期结果**：你能用自己的话解释「为什么 checksum 不一致有时是 fatal、有时却只是跳过」——区别在于涉事 chunk 的 chainVer 是否等于当前同步任务版本：等于说明是本任务应负责的一致性、出错即 fatal；不等说明属于其他并发写、暂态、跳过即可。

#### 4.2.5 小练习与答案

**练习 1**：`syncStart` 为什么在返回元数据前后各调用一次 `getByChainId`？

**答案**：第一次取 target 用于遍历元数据；第二次是**复查路由版本**。吐全量元数据可能耗时较长，期间 mgmtd 可能已下发新路由（链成员变化），复查确保返回的元数据仍属于请求时的链版本，避免用陈旧拓扑的数据误导前驱判定。

**练习 2**：后继 C 的某个 chunk 在 `syncStart` 吐出时 `chunkState` 不是 `COMMIT`（即有未提交的 pending 版本），前驱 B 会怎么处理？

**答案**：见源码 225-227 行，B 记一条 WARNING 并计入 `remoteUncommittedCount`，**不**把它加入 writeList、也不删除——因为远端这条 pending 可能是 C 崩溃前的脏状态，B 不能据此时序乱下结论；该 chunk 的最终一致性会由后续的 full-chunk-replace（无论来自本轮还是客户端实时写）来覆盖纠正。

---

### 4.3 全量回放：full-chunk-replace 与 sync-done 状态翻转

#### 4.3.1 概念说明

对比得出 writeList/removeList 后，就要把数据真正搬过去。这里的核心机制是 **full-chunk-replace**：每个要传的 chunk 都被改造成「整块读出 → 整块覆盖写」，而非增量写。它有**两个来源**：

- **后台追平**：`ResyncWorker::forward` 主动为 writeList 里的每个 chunk 发起一次同步写。
- **客户端实时写**：恢复期间客户端的写请求经 CRAQ 链正常转发到 C 时，转发器发现 C 处于 `SYNCING`，也会把它改造成整块写。

之所以两条路径都强制整块写，是因为 C 上每个 chunk 的历史版本不可信，只有整体覆盖才能保证最终一致。设计文档把这一阶段描述为「The full state of the predecessor is copied to the returning service through a continuous stream of full-chunk-replace writes」。

所有 chunk 传完后，前驱发 `syncDone`，后继把 localState 翻成 `UPTODATE`，并在后续心跳上报 mgmtd，mgmtd 状态机再把 publicState 从 `SYNCING` 翻成 `SERVING`——恢复闭环。

#### 4.3.2 核心流程

**后台追平**的分批转发逻辑：

[src/storage/sync/ResyncWorker.cc:305-354](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.cc#L305-L354) —— removeList 与 writeList 各自按 `batch_size`(16) 分批，每批内并发 `forward`，整批受 `batchConcurrencyLimiter`(64) 限流；每处理完一个 batch 复查一次路由版本并更新剩余计数。先处理 remove、再处理 write。

单个 chunk 的 `forward` 构造的是一个**整块写请求**：

[src/storage/sync/ResyncWorker.cc:433-446](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.cc#L433-L446) —— `offset=0`、`chunkSize=整块大小`、`updateVer=1`、`updateType=WRITE`、`options.isSyncing=true`、`options.commitChainVer=当前链版本`，且**不附带数据**（`rdmabuf` 为空）。数据由转发器在 `readForSyncing` 路径里现读现传（见下）。

注意 `forward` 里先 `lockChunk` 拿到 chunk 锁（与正常写共用同一把按 chunk 的锁），再二次 `queryChunk` 校验：若本意要 REMOVE 的 chunk 已被正常写更新过，就跳过 remove；若本意要 WRITE 的 chunk 已被删除，就跳过 write——这保证后台追平与客户端实时写并发时不会互相覆盖出错：

[src/storage/sync/ResyncWorker.cc:404-424](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.cc#L404-L424) —— 加锁后复查 chunk 现状，实现「remove-after-update」「update-after-remove」两种竞态的安全跳过。

**full-chunk-replace 的真正发生地**在转发器 `doForward`。关键判断是 `readForSyncing`：

[src/storage/service/ReliableForwarding.cc:152-160](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/ReliableForwarding.cc#L152-L160) —— 后继 `publicState==SYNCING` 即置 `isSyncing=true`，并标 `updateReq.options.isSyncing=true`、`commitChainVer=当前链版本`。`readForSyncing` 为真的条件是「后继 SYNCING 且非 REMOVE 且（本请求自带 isSyncing，或是 truncate/extend，或是只写了 chunk 的一部分）」。

当 `readForSyncing` 为真，转发器会**从本机整块读出该 chunk**，重算 checksum，再把整块作为一次 WRITE 转发给后继：

[src/storage/service/ReliableForwarding.cc:161-223](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/ReliableForwarding.cc#L161-L223) —— `readForSyncing` 分支：分配整块 buffer、用 `AioReadJob` 以 `readUncommitted=true`（连未提交的 pending 也读，因为恢复期要以本机最新内容为准）整块读出、用读出的 `updateVer` 与 checksum 改写请求、`offset=0, length=整块长度`、`rdmabuf=读出的整块`，最终转发。若数据较小还会内联进请求（`SEND_DATA_INLINE`）。这就是 full-chunk-replace 的数据装配过程。

> 说明：`ResyncWorker::forward` 构造的请求不附带数据、且 `options.isSyncing=true`，所以它必然命中 `readForSyncing` 分支——也就是说，ResyncWorker 自己不读盘装数据，而是把「整块读 + 整块转发」复用给转发器统一完成。这是一个优雅的复用：后台追平与客户端实时写在 SYNCING 后继上走的是同一条 full-chunk-replace 通路。

**恢复收尾**——所有 chunk 传完后发 `syncDone`：

[src/storage/sync/ResyncWorker.cc:356-377](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.cc#L356-L377) —— 向后继发 `SyncDoneReq`，成功后记一条 INFO 日志 `sync done chain ... update ... remove ...`。注意此处对 `lengthInfo` 的校验——`syncDone` 回包携带长度信息，失败需报错重试。

后继收到 `syncDone` 后翻转 localState：

[src/storage/service/StorageOperator.cc:1052-1063](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L1052-L1063) 与 [src/storage/service/TargetMap.cc:111-122](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.cc#L111-L122) —— `syncDone` 调 `TargetMap::syncReceiveDone`，把该 target 的 `localState` 由 `ONLINE` 翻成 `UPTODATE`，并打 WARNING 日志记录这次状态翻转。

最后由 `updateLocalState` 把 local/public 状态衔接成完整闭环：

[src/storage/service/TargetMap.cc:329-352](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.cc#L329-L352) —— `updateLocalState` 给出两条关键转换：`ONLINE + publicState==SERVING → UPTODATE`（新 target 首次正式服务）与 `UPTODATE + publicState∈{OFFLINE,LASTSRV,WAITING} → OFFLINE`（被下线）。注意：恢复 target 在 `SYNCING` 期间 localState 维持 `ONLINE`，要等 `syncReceiveDone` 显式翻 `UPTODATE`，再经心跳上报 mgmtd，mgmtd 状态机（u3-l5）才把 publicState 由 `SYNCING` 推进到 `SERVING`。

整个状态推进可概括为：

```text
C 崩溃:        local=OFFLINE, public=OFFLINE (链尾)
C 重启恢复中:   local=ONLINE,  public=SYNCING  ← B 推数据
B 发 syncDone:  local=UPTODATE,public=SYNCING  ← C 本机翻 UPTODATE
C 心跳上报:     local=UPTODATE,public=SYNCING  → mgmtd 状态机
mgmtd 推进:    local=UPTODATE,public=SERVING  ← 恢复完成，正常服务
```

#### 4.3.3 源码精读

`handleUpdate` 对恢复期请求的约束——拒绝 truncate/extend，只允许整块 WRITE/REMOVE：

[src/storage/service/StorageOperator.cc:352-356](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L352-L356) —— `isSyncing` 的 truncate/extend 请求被直接拒绝。这是 full-chunk-replace 语义的防线：恢复期不允许任何「部分修改」，只接受整块覆盖。

`getAllChunkMetadata`（区别于 `syncStart`，它面向外部查询，要求 target 已 `SERVING + UPTODATE`）：

[src/storage/service/StorageOperator.cc:1175-1206](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L1175-L1206) —— 与 `syncStart` 形成对照：`getAllChunkMetadata` 校验 `publicState==SERVING && localState==UPTODATE`，是给「已恢复完成」的 target 用的全量元数据导出接口；而 `syncStart` 校验的是 `SYNCING + ONLINE`，专供恢复期。两者底层都调 `storageTarget->getAllMetadata`，但前置状态门禁不同。

人工触发「全量重传」的开关（运维兜底）：

[src/storage/sync/ResyncWorker.h:24-34](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.h#L24-L34) —— `full_sync_level`(NONE/HEAVY) 与 `full_sync_chains`。设为 `HEAVY` 后，即使 checksum 已一致也会强制重传（`heavyFullSync` 分支，源码 250-251 行），用于怀疑静默损坏时的人工校准；`full_sync_chains` 可限定只对指定链全量重传。

#### 4.3.4 代码实践

**实践目标**：把「整块替换」的数据流从 ResyncWorker 一路追到转发器，看清数据在哪一段被装配。

**操作步骤**：

1. 从 [src/storage/sync/ResyncWorker.cc:433-453](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.cc#L433-L453) 入手：注意 `forward` 构造的 `UpdateReq` 没有数据（`forwardWithRetry(..., {}, ...)` 第三个参数 `rdmabuf` 为空），但 `offset=0, chunkSize=整块, isSyncing=true`。
2. 跟进 `forwardWithRetry` → `forward` → `doForward`（[ReliableForwarding.cc:138](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/ReliableForwarding.cc#L138)）。在第 152 行看到后继 SYNCING 触发 `isSyncing`，第 158-160 行看到 `readForSyncing` 命中。
3. 在第 161-213 行看到：分配整块 buffer → `AioReadJob` 整块读（`readUncommitted=true`）→ 用读出的长度/版本/checksum 改写请求 → 把整块 buffer 作为 `rdmabuf` 转发。
4. 最后在 [StorageOperator.cc:1052-1063](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L1052-L1063) 与 [TargetMap.cc:111-122](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.cc#L111-L122) 确认 `syncDone` 把 localState 翻成 `UPTODATE`。

**需要观察的现象**：数据「装进请求」的动作并不在 `ResyncWorker::forward` 里完成，而是在 `ReliableForwarding::doForward` 的 `readForSyncing` 分支里完成——因为同一段代码还要服务「客户端实时写命中 SYNCING 后继」的场景。这是代码复用，不是冗余。

**预期结果**：你能画出一条单 chunk 的同步数据流：`ResyncWorker.forward（无数据，仅元信息）→ doForward.readForSyncing（整块读本机）→ messenger.update（整块发给后继）→ 后继整块覆盖落盘`。

**待本地验证**：真实运行中，可在前驱节点 grep 日志关键字：`start sync chain`（恢复开始，源码 118 行）、`sync done chain ... update N remove M`（恢复完成，源码 380-385 行），对比两条日志的时间差即为单链恢复耗时。

#### 4.3.5 小练习与答案

**练习 1**：客户端在恢复期间对某 chunk 发起一次只改 4KB 的部分写，最终落到后继 C 上的是 4KB 还是整块？

**答案**：整块。客户端的部分写经 CRAQ 链转发到 C 时，`doForward` 发现 C 处于 `SYNCING`，`readForSyncing` 条件 `isWrite() && length != chunkSize` 成立，于是先从本机把整个 chunk 整块读出（含这次 4KB 的新内容），再以整块 WRITE 转发给 C。C 收到的是一次 full-chunk-replace。

**练习 2**：`syncDone` 之后，后继的 publicState 会立刻变成 `SERVING` 吗？

**答案**：不会立刻。`syncDone` 只把**本机** localState 由 `ONLINE` 翻成 `UPTODATE`。publicState 是 mgmtd 综合下发的，要等 C 在下一次心跳把 `UPTODATE` 上报给 mgmtd，mgmtd 状态机（u3-l5）才会把 publicState 由 `SYNCING` 推进到 `SERVING`。即「本机先自证追平，mgmtd 再盖章放行」。

**练习 3**：为什么 `readForSyncing` 里要用 `readUncommitted=true` 读？

**答案**：恢复期要以本机当前最新的 chunk 内容为准覆盖后继，包括尚未 ACK 提交的 pending 版本（CRAQ 双版本中的 `updateVer`）。若只读 committed，会把本机已在写但未 ACK 的较新内容漏掉，导致后继追平后仍落后于前驱。`readUncommitted` 保证整块读到的是前驱最完整的当前状态。

---

## 5. 综合实践

**任务**：完整描述一个 target 从 `OFFLINE` 重启到恢复 `UPTODATE` 的全过程，列出每个阶段涉及的关键消息与判定规则，并对照源码验证。

请按下面的提纲，结合本讲源码与 u3-l5 的状态机，写出一份「恢复时序说明」：

1. **崩溃与下线**（mgmtd 侧，参考 u3-l2/u3-l5）
   - C 进程崩溃 → 心跳超时（`heartbeat_fail_interval`，默认 60s）→ mgmtd 把 C 的 target local 翻 `OFFLINE`，public 经状态机处理、C 被挪到链尾。
   - 涉及判定：心跳租约过期（单调时钟）。

2. **重启与就绪**（storage 侧）
   - C 重启，按设计约定先拉路由、暂不发心跳，直到本机所有 target 在路由里都 offline。
   - 涉及规则：design_notes.md 第 238 行「确保所有 target 都走恢复流程」。

3. **进入 SYNCING**（mgmtd → storage）
   - mgmtd 状态机把 C 翻 `SYNCING`，下发路由。C 本机 `updateRouting` 后 localState=`ONLINE`、publicState=`SYNCING`。
   - 前驱 B 的 `updateRouting` 发现后继 C 是 `SYNCING` → 登记进 `syncingChains_`。
   - 关键源码：[TargetMap.cc:231-234](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.cc#L231-L234)。

4. **触发恢复**（B 侧 ResyncWorker）
   - B 的 `loop()` 扫到该链，过 30s 冷却与 isSyncing 检查后入队 `handleSync`。
   - 关键源码：[ResyncWorker.cc:66-99](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.cc#L66-L99)。

5. **dump-chunkmeta**（B ↔ C）
   - B 向 C 发 `SyncStartReq`；C 校验 `SYNCING+ONLINE`，吐全量 `ChunkMeta`。
   - 关键源码：[StorageOperator.cc:1007-1050](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L1007-L1050)。
   - B 取本机 localMetas，按四条规则对比生成 writeList/removeList。
   - 关键源码：[ResyncWorker.cc:203-292](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.cc#L203-L292)；规则见 [design_notes.md:262-271](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L262-L271)。

6. **全量回放**（B → C）
   - writeList/removeList 分批 `forward`，每个 chunk 经 `readForSyncing` 改造成整块写发给 C。
   - 关键消息：`UpdateReq`（`isSyncing=true, offset=0, length=chunkSize`）；关键源码：[ResyncWorker.cc:433-446](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.cc#L433-L446)、[ReliableForwarding.cc:152-223](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/ReliableForwarding.cc#L152-L223)。
   - 并发客户端写也走同一条 full-chunk-replace 通路。

7. **sync-done 与状态翻转**（B → C → mgmtd）
   - B 发 `SyncDoneReq`；C 调 `syncReceiveDone` 把 localState 翻 `UPTODATE`。
   - 关键源码：[StorageOperator.cc:1052-1063](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L1052-L1063)、[TargetMap.cc:111-122](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.cc#L111-L122)。
   - C 心跳上报 `UPTODATE`，mgmtd 状态机把 publicState 推进到 `SERVING`，恢复闭环。

**交付物**：一张标注了「阶段 / 触发方 / 关键消息 / 判定规则 / 对应源码行号」的表格，外加一段 200 字说明 full-chunk-replace 为什么让恢复「与正常活动重叠且最小化中断」（提示：读路径不阻断、写路径复用 CRAQ 转发、元数据比对只传必要 chunk）。

## 6. 本讲小结

- **恢复是被动触发的**：`ResyncWorker` 不自己判断 target 是否需恢复，只扫描 `TargetMap` 由路由更新填入的 `syncingChains_`；该列表的源头是 mgmtd 把恢复 target 翻 `SYNCING` 并置于链尾，前驱在 `updateRouting` 中识别 SYNCING 后继而登记。
- **先比对元数据再传数据**：`syncStart`（dump-chunkmeta）让后继吐全量 `ChunkMeta`，前驱按四条规则（本地独有→传、远端独有→删、本地链版本更高→传、同链版本但版本号不等→传）生成 writeList/removeList，避免全量无脑传输。
- **恢复期一切写都是 full-chunk-replace**：无论后台追平还是客户端实时写，命中 SYNCING 后继时都由 `doForward` 的 `readForSyncing` 分支整块读出、整块覆盖转发，绕开增量版本对齐的脆弱性；`handleUpdate` 还显式拒绝恢复期的 truncate/extend。
- **fatal 不变量保护**：发现「已提交却链版本落后」「checksum 不一致且非并发写所致」等违反 CRAQ 不变量的情形时，主动 `offlineTarget(force)` 下线后继，宁可停服也不扩散损坏。
- **sync-done 翻 localState，心跳翻 publicState**：`syncReceiveDone` 把本机 localState 由 `ONLINE` 翻 `UPTODATE`，再经心跳上报，mgmtd 状态机才把 publicState 由 `SYNCING` 推进到 `SERVING`——本机先自证追平，mgmtd 再盖章放行。
- **后台追平与客户端实时写复用同一条通路**：`ResyncWorker::forward` 不自带数据，而是把整块装配复用给 `ReliableForwarding::doForward`，两条来源在 SYNCING 后继上走同一套 full-chunk-replace 逻辑。

## 7. 下一步学习建议

- **深入 chunk engine（u6）**：本讲频繁出现的 `storageTarget->getAllMetadata`、整块读、整块覆盖写的真正落盘执行都在 Rust 实现的 chunk engine 里。建议接着读 u6-l1（chunk engine 总览与 FFI）、u6-l3（chunk 元数据与 RocksDB），理解 `readUncommitted`、full-chunk-replace 在物理块层是如何原子替换的。
- **回到 mgmtd 状态机（u3-l5）**：若对 `SYNCING → SERVING` 的 public 状态推进细节仍想确认，重读 u3-l5 的状态转换表与 `generateNewChain`，把本讲的 localState 翻转与 mgmtd 的 publicState 推进对照看，体会「输入事件（local）/输出结论（public）」的分工。
- **阅读集成测试**：[tests/storage/sync/TestSyncStartAndDone.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/tests/storage/sync/TestSyncStartAndDone.cc) 用单节点 + RocksDB 构造最小恢复场景，是验证你对 `syncStart/syncDone` 理解的好材料；可尝试在其基础上加断言，观察 writeList/removeList 的生成。
- **运维视角（u8）**：`full_sync_level=HEAVY` 是怀疑静默损坏时的人工全量重传兜底，结合 u8 的监控讲义（`storage.resync`、`storage.syncing.*` 系列指标）可以建立「恢复进度可观测」的运维能力。
