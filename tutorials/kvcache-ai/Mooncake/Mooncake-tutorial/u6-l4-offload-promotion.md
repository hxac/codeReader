# Offload 与 Promotion-on-hit

## 1. 本讲目标

Mooncake Store 把存储分成多个层级：最快的 DRAM（MEMORY 副本）→ 本机 SSD（LOCAL_DISK 副本）→ 远程分布式文件系统。本讲讲清楚这两条**层级迁移**的迁移路径是怎么动的：

1. **Offload（下沉，DRAM → SSD）**：当 DRAM 内存吃紧、触发淘汰时，被选中淘汰的对象先被异步写一份到本地 SSD，再释放 DRAM——数据不丢，只是降级到慢一点的层。
2. **Promotion-on-hit（上浮，SSD → DRAM）**：当一个对象的读请求（Get）发现它**只在 SSD 上**，并且它**被反复访问**，系统会在后台异步把它从 SSD 拷回 DRAM，让它重新变成「热数据」。

这两条路径都不是「请求路径」里同步做的，而是由 **Real Client 的心跳线程**周期性向 Master 拉取任务、再异步执行的。学完本讲你应该能够：

1. 说清 **offload-on-evict** 的触发条件（高水位淘汰）、它和「PutEnd 时立即 offload」两种模式的区别，以及 `PushOffloadingQueue` / `OffloadObjectHeartbeat` / `NotifyOffloadSuccess` 三个 RPC 如何配合把一个对象搬下去。
2. 说清 **promotion-on-hit 的四道准入门控**（频率 / 水位 / 去重 / 上限），并完整画出一次 promotion 的生命周期：Get 观察到只有 LOCAL_DISK → 频率命中 → 分配 DRAM → RDMA 写入 → 提交 COMPLETE。
3. 解释 `PromotionTask` 结构里 **`holder_id` 鉴权为何必要**，以及 `alloc_id` 如何防止「错认别人的半成品副本」。
4. 理解 **心跳驱动的异步任务下发** 模型：Master 不主动推送，而是每个 Real Client 在 `MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS`（默认 10s）的心跳里来「拉」任务，`PromotionObjectHeartbeat` 每次最多只回一个 promotion 任务。

> 本讲是 u6 单元的「迁移机制」讲。它依赖你已经理解多级存储层次（u6-l2）和淘汰/水位/频率追踪（u6-l3）。

## 2. 前置知识

本讲默认你已具备（对应依赖讲义）：

- **多级存储层次（依赖 u6-l2）**：你需要知道一个对象可以同时有 MEMORY 副本（DRAM）和 LOCAL_DISK 副本（本机 SSD）；LOCAL_DISK 副本由 Real Client 的 `FileStorage` 组件负责读写，背后挂一个 `StorageBackend`（bucket / file-per-key / offset-allocator）。
- **淘汰与水位（依赖 u6-l3）**：你需要知道 Master 有一个后台淘汰线程 `EvictionThreadFunc`，当全局内存使用率 `used_ratio` 超过高水位 `eviction_high_watermark_ratio_` 时触发 `BatchEvict`；以及 `CountMinSketch`（Count-Min 草图）如何用很小的内存估计一个 key 的访问频率。
- **副本状态机（依赖 u5-l5）**：你需要知道一个副本有 `INITIALIZED → PROCESSING → COMPLETE` 的状态机。**读者只能看到 COMPLETE 的副本**；一个正在写的副本处于 PROCESSING，对外不可见。promotion 正是利用这个机制：先挂一个 PROCESSING 的 DRAM 副本，RDMA 写完后才翻成 COMPLETE 暴露给读者。
- **控制面/数据面分离（依赖 u5-l1）**：Master 只管元数据和调度（控制面），真正搬数据靠 Transfer Engine（数据面）。offload / promotion 的「搬」动作发生在 Real Client 进程里，Master 只下发任务、登记元数据。

### 为什么需要「下沉」和「上浮」？

用一个比喻建立直觉：

> 想象 DRAM 是一张「很贵但很快的桌子」，SSD 是「便宜但慢的抽屉」。桌子就这么大，新数据要放上来就得先腾地方。
> - **Offload（下沉）**：桌子满了要清场时，别直接把东西扔掉，先塞进抽屉里——下次还能从抽屉找回。
> - **Promotion（上浮）**：某个东西被反复从抽屉里掏出来用（热数据），就值得把它放回桌面上，省得每次都去抽屉里翻。

关键在于：**这两件事都不能卡在用户请求的关键路径上**。用户 `Get` 一个对象时，如果他恰好命中 SSD，应该**先立即从 SSD 把数据给他**（保证延迟），然后**在后台悄悄**决定要不要把它上浮回 DRAM。所以整套机制是**异步、心跳驱动、尽力而为**的。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲怎么用 |
|---|---|---|
| mooncake-store/include/master_service.h | Master 的控制面类，offload/promotion 的 RPC 接口、`PromotionTask` 结构、配置字段全在这里 | 主战场：看 RPC 声明、`PromotionTask` 字段与注释、配置成员 |
| mooncake-store/include/count_min_sketch.h | 频率估计草图，promotion 的「频率闸」用它 | 看 `increment` / `count` / `decay` 实现 |
| mooncake-store/src/master_service.cpp | 控制面实现：`OffloadObjectHeartbeat`、`PushOffloadingQueue`、`TryPushPromotionQueue`、`PromotionObjectHeartbeat`、`PromotionAllocStart`、`NotifyPromotionSuccess/Failure` | 看四道门控、生命周期、淘汰路径里的 offload |
| mooncake-store/src/file_storage.cpp | Real Client 的 `FileStorage`：心跳线程、`Heartbeat()`（offload）、`ProcessPromotionTasks()`（promotion 的真正搬数据） | 看心跳驱动如何把任务从「Master 的队列」搬到「真实读写」 |
| mooncake-store/src/master.cpp | master 进程的 gflags 定义 | 看 offload/promotion 的命令行开关与默认值 |
| mooncake-store/include/master_config.h | `MasterConfig` 结构 | 看配置字段定义 |
| docs/source/deployment/ssd-offload.md | SSD offload 部署文档 | 看心跳间隔、buffer GC 等运行时环境变量 |

一个贯穿全讲的**心智模型**：Master 内部维护两个「任务队列」（都是 per-client 的 map）：

- `LocalDiskSegment::offloading_objects`：待下沉到 SSD 的对象清单。
- `LocalDiskSegment::promotion_objects`：待上浮到 DRAM 的对象清单。

Master **不主动通知**任何客户端。每个 Real Client 起一个心跳线程，定期用 `OffloadObjectHeartbeat` / `PromotionObjectHeartbeat` 来「拉」走自己那份任务（拉走即从队列里清空），再去执行真正的 SSD 读写 + RDMA 传输，最后用 `NotifyOffloadSuccess` / `NotifyPromotionSuccess` 回报结果、更新元数据。

## 4. 核心概念与源码讲解

### 4.1 心跳驱动：异步任务下发的总框架

#### 4.1.1 概念说明

先建立「心跳驱动」这个总框架，后面 offload 和 promotion 都套这个模型。

**问题**：Master 想让某个 Real Client 把对象 A 搬到 SSD。它怎么把这件事告诉那个 Client？

- **不能用 Master 主动 push**：Store 的 RPC 框架（coro_rpc）在控制面里是「Client 调 Master」的方向，Master 没有到 Client 的主动通道（和 Copy/Move/Drain 任务不一样，那些走独立的 task 下发机制）。
- **不能在用户请求里同步做**：用户 `Get`/`Put` 的延迟不能被「顺便帮我搬个数据」拖累。

**解法**：每个 Real Client 启动时起一个**心跳线程**，每 `heartbeat_interval_seconds`（默认 10 秒，对应环境变量 `MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS`）做一次 `Heartbeat()` 调用。这次心跳同时干两件拉取任务的事：

1. 拉走自己名下的 offload 任务（如果有），执行 SSD 写入。
2. 拉走自己名下的 promotion 任务（如果有），执行 SSD 读 + RDMA 写。

这样 Master 只需要「往某个 client 的队列里塞任务」，剩下交给那个 client 自己来拉。**任务队列是 per-client 的**：offload/promotion 都只涉及「本机 SSD」，所以任务天然只发给拥有那块 SSD 的那个 Real Client。

#### 4.1.2 核心流程

```text
Master 侧                        Real Client 侧（FileStorage 心跳线程）
─────────                        ──────────────────────────────────
[某时刻把任务塞进 client X 的      每 heartbeat_interval_seconds（默认10s）:
 offloading_objects /                │
 promotion_objects 队列]             ├─ Heartbeat():
       │                             │   ├─ RPC: OffloadObjectHeartbeat(X)
       │  ←───── 拉取 ───────────────┼───┤   → 拿到一批 offload 任务（队列被清空）
       │                             │   ├─ OffloadObjects(tasks): SSD 写 + RPC: NotifyOffloadSuccess
       │                             │   └─ ProcessPromotionTasks():
       │  ←───── 拉取 ───────────────┼───┤   ├─ RPC: PromotionObjectHeartbeat(X)
                                     │       │   → 拿到至多 max_per_heartbeat 个任务
                                     │       ├─ PromotionAllocStart（Master 分配 DRAM）
                                     │       ├─ BatchLoad（SSD 读）→ PromotionWrite（RDMA 写）
                                     │       └─ NotifyPromotionSuccess（提交 COMPLETE）
                                     └ sleep(10s) → 下一轮
```

#### 4.1.3 源码精读

Real Client 的心跳线程在 `FileStorage` 初始化时启动，循环体就是「`Heartbeat()` + 睡一个间隔」—— [mooncake-store/src/file_storage.cpp:L306-L316](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/file_storage.cpp#L306-L316)。这一段说明心跳驱动的心脏：默认 10 秒一拍。

`Heartbeat()` 做三件事：先拉 offload 任务并执行（STEP 1/2），再驱动 promotion—— [mooncake-store/src/file_storage.cpp:L611-L684](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/file_storage.cpp#L611-L684)。注意第 680 行 `(void)ProcessPromotionTasks();`——promotion 的失败被刻意吞掉（best-effort），**绝不能因为 promotion 出错而影响 offload**，因为 offload 关系到内存回收，promotion 只是性能优化。

心跳间隔由环境变量控制—— [docs/source/deployment/ssd-offload.md:L105-L120](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L105-L120)，其中 `MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS` 默认 10 秒。

### 4.2 Offload：DRAM → LOCAL_DISK 下沉

#### 4.2.1 概念说明

Offload 解决的问题：**DRAM 容量有限，淘汰时别把数据直接扔了**。当一个对象在 DRAM 里待不下去了，先把它落一份到本机 SSD（LOCAL_DISK 副本），再回收 DRAM。之后即便 MEMORY 副本全没了，`Get` 还能从 LOCAL_DISK 副本读到数据（读 SSD 比重新算一遍 KVCache 便宜得多）。

开启 offload 的总开关是 master 的 `--enable_offload=true`，并且 Real Client 也要 `--enable_offload=true`（部署文档反复强调两端都要开）—— [docs/source/deployment/ssd-offload.md:L1-L15](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L1-L15)。

#### 4.2.2 两种 offload 触发模式

Mooncake 支持两种「何时把对象塞进 offload 队列」的策略，由 `offload_on_evict` 开关切换：

| 模式 | 开关 | 何时塞队列 | 特点 |
|---|---|---|---|
| **默认模式（写完即下沉）** | `enable_offload=true` 且 `offload_on_evict=false` | 对象写完（`PutEnd` 标 COMPLETE）时，每个 MEMORY 副本立即进 offload 队列 | 尽快让 SSD 有副本；SSD 持续被写满 |
| **offload-on-evict（淘汰时下沉）** | `offload_on_evict=true` | 只有当淘汰线程要淘汰它、且它**还没有** LOCAL_DISK 副本时才进队列 | SSD 只保留「真正被挤下 DRAM」的数据，省 SSD 空间与写放大 |

**默认模式**（写完即下沉）：在 `PutEnd` 里，如果开了 offload 但没开 offload-on-evict，就把每个刚 COMPLETE 的 MEMORY 副本推进 offload 队列—— [mooncake-store/src/master_service.cpp:L1950-L1966](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1950-L1966)。

**offload-on-evict 模式**（本讲重点）：把下沉动作推迟到淘汰时刻。配置加载时确定模式—— [mooncake-store/src/master_service.cpp:L253-L264](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L253-L264)。注意第一行 `offload_on_evict_ = enable_offload_ && config.offload_on_evict;`——offload-on-evict 隐含要求 offload 本身也开着。

#### 4.2.3 offload-on-evict 核心流程

当淘汰线程在高水位下扫描候选对象时，对每个要淘汰的对象 `try_evict_or_offload` 做如下决策（在 `offload_on_evict_` 开启时）：

```text
对一个候选淘汰对象 K：
1. K 已经有 LOCAL_DISK 副本？ → 数据在 SSD 上有保障了，直接淘汰它的 MEMORY 副本（释放 DRAM）
2. 还没有 LOCAL_DISK 副本：
   a. 如果开了 offload_force_evict 且「本周期已 offload 数量达到上限 offload_cap」
      → 强制淘汰（不 offload，可能丢这份数据，靠 force_evict 开关兜底）
   b. 否则：从 K 的 MEMORY 副本里挑一个（completed 且 refcnt==0），
      PushOffloadingQueue(把它塞进 owner client 的 offloading_objects 队列)
        → 成功：给这个 MEMORY 副本 inc_refcnt（防它在写盘前被别人删），
               登记一个 offloading_task，本周期 offload 计数 +1；
               K 的其余冗余 MEMORY 副本可以立即淘汰
        → 失败（队列满 / 未开 offloading）：
            · 开了 force_evict：强制淘汰
            · 没开 force_evict：跳过本轮（保数据，下轮再试），返回释放 0 字节
```

**关键点 1：refcnt 防删除**。被选中 offload 的那个 MEMORY 副本会 `inc_refcnt()`，这样在 Real Client 把它写进 SSD 之前，它不会被淘汰逻辑再次干掉（数据是 source）。

**关键点 2：单副本 offload**。一个 key 只需把**一个** MEMORY 副本落盘即可保证数据存活，其余冗余副本可以立即回收 DRAM。

**关键点 3：force-evict 是「宁可丢数据也要腾空间」的兜底开关**。默认关闭（保数据优先）。`offload_cap` 用 `offloading_queue_limit_ * kOffloadCapRatio` 算出，防止 offload 队列无限膨胀把 master 撑爆。

#### 4.2.4 源码精读

**配置加载**：模式判定、force-evict 开关—— [mooncake-store/src/master_service.cpp:L253-L264](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L253-L264)。对应的 gflags 定义与默认值在 master.cpp—— [mooncake-store/src/master.cpp:L127-L145](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master.cpp#L127-L145)（`enable_offload`、`offload_on_evict`、`offload_force_evict` 默认都是 false）。

**淘汰路径里的 offload 决策**：`try_evict_or_offload` lambda 的完整逻辑，包含「已有 LOCAL_DISK 就直接淘汰」「force-evict 上限」「挑单副本 push」「失败回退」四段—— [mooncake-store/src/master_service.cpp:L5283-L5360](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5283-L5360)。读这段时重点看 5332 行 `PushOffloadingQueue(...)` 成功后立即 `replica.inc_refcnt()` 与登记 `offloading_tasks`。

**PushOffloadingQueue**：把一个对象塞进「拥有目标 segment 的那个 client」的 `offloading_objects` map。它会校验：该 segment 找得到 owner、该 client 开了 `enable_offloading`、队列没超过 `offloading_queue_limit_`（否则返回 `KEYS_ULTRA_LIMIT`）—— [mooncake-store/src/master_service.cpp:L3500-L3547](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3500-L3547)。

**OffloadObjectHeartbeat**（Real Client 拉取 offload 任务）：找到本 client 的 `LocalDiskSegment`，在锁内把它的 `offloading_objects` 整个 `move` 出来返回（**即拉走即清空**），同时刷新 `enable_offloading` 状态；如果 client 关了 offloading，则顺手清空队列并回收对应 refcnt—— [mooncake-store/src/master_service.cpp:L3356-L3416](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3356-L3416)。

**NotifyOffloadSuccess**（Real Client 写完 SSD 后回报）：对每个已落盘对象，先 `dec_refcnt`（source MEMORY 副本完成使命、解除 pin）并清掉 `offloading_tasks`，再 `AddReplica` 一个新的 **COMPLETE 的 LOCAL_DISK 副本**（记录 transport_endpoint 等）—— [mooncake-store/src/master_service.cpp:L3453-L3498](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3453-L3498)。这一步之后，即便原来的 MEMORY 副本被淘汰，`Get` 也能命中新的 LOCAL_DISK 副本。

**Real Client 侧执行**：`Heartbeat()` STEP 1 调 `OffloadObjectHeartbeat` 拉任务，STEP 2 调 `OffloadObjects` 真正写 SSD（内部最终会 `NotifyOffloadSuccess`）—— [mooncake-store/src/file_storage.cpp:L611-L675](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/file_storage.cpp#L611-L675)。注意 618-663 行：若 master 返回 `SEGMENT_NOT_FOUND`（通常是 master 重启丢了段），client 会尝试重新 `MountLocalDiskSegment` 再重试一次心跳，并触发后台元数据重扫——保证 master 重启后 offload 能自愈。

#### 4.2.5 代码实践

**实践目标**：对照部署文档与源码，跑通一次 offload-on-evict，并观察「对象被淘汰后仍可从 SSD 读到」。

**操作步骤（示例部署）**：

1. 建存储目录：`mkdir -p /nvme/mooncake_offload`。
2. 启 master（开 offload 与 offload-on-evict）：

   ```bash
   mooncake_master --rpc_port=50051 \
       --enable_offload=true --offload_on_evict=true
   ```

3. 起 Real Client，把 `--global_segment_size` 设得**小于你即将写入的数据总量**（这样才能压出高水位触发 offload），并设 SSD 路径——完整示例见 [docs/source/deployment/ssd-offload.md:L198-L233](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L198-L233)。
4. 用 Python SDK 持续 `put` 对象，直到内存使用率超过高水位。
5. 继续写入，触发淘汰；之后对**已被淘汰**的 key 调 `get`。

**需要观察的现象**：

- master 日志出现 `Offload-on-evict mode enabled`（配置加载时的提示）。
- Real Client 日志周期性出现心跳与 `BatchStore`（写盘）耗时。
- `/nvme/mooncake_offload` 下出现 `.bucket` / `.meta` 文件。
- 被淘汰的 key 仍能 `get` 成功（命中 LOCAL_DISK 副本）；若开了 metrics，`file_cache_hit_nums` 计数上升。

**预期结果**：offload-on-evict 下，数据先落盘再回收 DRAM，被淘汰对象仍可读。**若内存池远大于写入量，offload 不会被触发**（这是部署文档「SSD offload is not triggering」排错项的第一条）—— [docs/source/deployment/ssd-offload.md:L280-L285](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L280-L285)。

> 真实运行需要 RDMA 设备与多分区环境；若本地不具备，本实践可降级为「源码阅读型」：跟踪 `BatchEvict → try_evict_or_offload → PushOffloadingQueue`，对照 5332 行说明 refcnt 在此处为何必须先 `inc` 再入队。

#### 4.2.6 小练习与答案

**练习 1**：为什么 offload-on-evict 默认**只把一个** MEMORY 副本落盘，而不是全部？

**参考答案**：一个 key 只需一份存活的 LOCAL_DISK 副本就能保证数据不丢；其余冗余 MEMORY 副本应立即淘汰以尽快回收 DRAM（源码注释见 5342-5348 行 `queued` 分支）。全部落盘是浪费 SSD 写带宽与空间。

**练习 2**：`offload_force_evict` 关闭时，若 offload 队列满了（`PushOffloadingQueue` 失败），淘汰逻辑会怎么做？这会导致什么后果？

**参考答案**：返回释放 0 字节、跳过本轮（源码 5351-5359 行的「default 数据保」分支）。后果是该对象本轮不会被回收，内存压力暂时缓解不了，要等 offload 队列在后续心跳被 Real Client 拉走排空后，下轮淘汰才会再试。开了 `force_evict` 才会「宁可丢数据也要强制淘汰」。

### 4.3 Promotion-on-hit：LOCAL_DISK → DRAM 上浮

#### 4.3.1 概念说明

Promotion 解决的问题：**一个被挤到 SSD 的对象，如果又被频繁访问，应该让它回到 DRAM**，否则每次读都要走慢得多的 SSD。

但「频繁访问」这个判断要小心——不能见一次访问就上浮（那等于否定 offload），也不能上浮得太猛（会把 DRAM 又写满、引发新的淘汰风暴，形成「下沉↔上浮」抖动）。所以 promotion 设了一串**准入门控**，只有在「确实热 + DRAM 还有余量 + 没在重复处理 + 全局在途数没超上限」时才放行。

开启开关是 master 的 `--promotion_on_hit=true`。注意它**隐含要求** `--enable_offload=true`——因为只有 offload 才会产生 LOCAL_DISK 副本，没有 offload 就没有可上浮的对象。若你只开 promotion 不开 offload，master 会打一条 WARNING 并静默禁用 promotion—— [mooncake-store/src/master_service.cpp:L288-L303](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L288-L303)。

#### 4.3.2 准入门控四道闸

promotion 的「触发点」藏在 `GetReplicaList`（也就是用户 `Get` 的控制面调用）里：如果读到这个 key 时发现它**没有任何 MEMORY 副本、但至少有一个 LOCAL_DISK 副本**，就标记 `promotion_eligible`，然后在释放只读锁后调用 `TryPushPromotionQueue`—— [mooncake-store/src/master_service.cpp:L1491-L1509](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1491-L1509)。

`TryPushPromotionQueue` 内部依次过**四道闸**，任一不过就放弃（打一个对应的 `promotion_rejected_*` 指标）：

| 闸 | 判断 | 不过的后果 / 指标 | 设计意图 |
|---|---|---|---|
| **① 频率闸** | `promotion_sketch_->increment(key)` 得到的估计频次 `< promotion_admission_threshold_`（默认 2） | `promotion_rejected_frequency` | 见一次不升，多次才升，避免冷数据抖动 |
| **② 水位闸** | 全局内存使用率 `>= eviction_high_watermark_ratio_` | `promotion_rejected_watermark` | DRAM 已经在淘汰了，别再往上塞 |
| **③ 去重闸** | 该 key 已有在途 promotion 任务，或期间已出现 MEMORY 副本 | 静默返回 | 不重复排队 |
| **④ 上限闸** | 全局在途 promotion 任务数 `>= promotion_queue_limit_`（默认 50000） | `promotion_rejected_cap` | 防止 promotion 反客为主耗尽资源 |

四道闸都过了，才会：找到那个 LOCAL_DISK 源副本 → `inc_refcnt()`（防它在被拷回前被淘汰）→ `PushPromotionQueue` 把它塞进 holder client 的 `promotion_objects` 队列 → 在 shard 里登记一个 `PromotionTask` → 全局在途计数 `+1`。

#### 4.3.3 完整生命周期

一次 promotion 从「Get 观察到」到「DRAM 副本对读者可见」的完整生命周期（这是本讲**核心实践任务**，务必对照源码走一遍）：

```text
[Master: GetReplicaList]
   观察 key 只有 LOCAL_DISK、无 MEMORY → promotion_eligible = true
        │
        ▼
[Master: TryPushPromotionQueue]  四道闸：频率✓ 水位✓ 去重✓ 上限✓
   ├─ 找到 LOCAL_DISK source，inc_refcnt（钉住源）
   ├─ PushPromotionQueue → 塞进 holder client 的 promotion_objects 队列
   ├─ shard 里记 PromotionTask{source_id, alloc_id=0, object_size, holder_id}
   └─ promotion_in_flight_++（占一个全局名额）
        │
        │  ... 到下一次心跳（≤10s）...
        ▼
[Real Client: ProcessPromotionTasks（心跳线程里）]
   ├─ RPC: PromotionObjectHeartbeat(holder) → 拿走至多 max_per_heartbeat 个任务
   ├─ 对每个任务：
   │   ① RPC: PromotionAllocStart(holder, key, size)
   │        Master 校验 holder_id == 任务持有者 且 size == 源 object_size
   │        → 用 AllocationStrategy 分配一个 DRAM 缓冲，挂成 PROCESSING 的 MEMORY 副本
   │          （此时读者还看不到它），把它的 id 记到 task.alloc_id，重置 start_time
   │   ② BatchLoad：从本机 SSD 把数据读进对齐的 staging buffer
   │   ③ PromotionWrite：用 TE 把 staging → 新分配的 DRAM 副本（RDMA/TCP 写）
   │   ④ RPC: NotifyPromotionSuccess(holder, key)
   │        Master：把 alloc_id 指的那个 PROCESSING 副本 mark_complete（对读者可见！）
   │                source 的 refcnt--（解除钉住），删 task，promotion_in_flight_--
   │
   └─ 任何一步失败 → RPC: NotifyPromotionFailure（立即释放 master 侧名额与暂存缓冲）
```

> 注意 `alloc_id` 与 `holder_id` 这两个字段：它们解决的是「在并发与故障下，怎么确保不会把**错的副本**提交、不会被**错的客户端**提交」。详见 4.3.4。

#### 4.3.4 源码精读：PromotionTask 结构与鉴权

先看承载一次在途 promotion 的数据结构 `PromotionTask`，它的注释把两个关键字段的设计意图讲得很清楚—— [mooncake-store/include/master_service.h:L1121-L1149](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1121-L1149)：

```cpp
struct PromotionTask {
    ReplicaID source_id;    // the LOCAL_DISK replica being promoted
    ReplicaID alloc_id{0};  // the new MEMORY replica staged by AllocStart
    uint64_t object_size;
    std::chrono::system_clock::time_point start_time;
    UUID holder_id;  // owner of source LOCAL_DISK; only Notifier allowed
};
```

**`holder_id`：为什么鉴权是必要的？**（这是实践任务要求解释的点。）

注释原文说得很直白（master_service.h L1136-L1142）：如果没有 `holder_id` 鉴权，**任何**知道这个 key 的客户端都能在 holder 的 RDMA 写还没落地之前，抢先调 `NotifyPromotionSuccess` 把那个还处于 PROCESSING、**内容还没写完**的 DRAM 副本翻成 COMPLETE，于是读者会读到**撕裂/半成品数据（torn data）**。

具体到代码：`NotifyPromotionSuccess` 和 `PromotionAllocStart`、`NotifyPromotionFailure` 都有同一道闸—— [mooncake-store/src/master_service.cpp:L3748-L3751](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3748-L3751)（AllocStart）、[L3837-L3840](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3837-L3840)（NotifySuccess）、[L3911-L3914](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3911-L3914)（NotifyFailure）：如果 `task.holder_id != client_id` 直接返回 `INVALID_PARAMS`。`holder_id` 是谁？就是那个 LOCAL_DISK 源副本的 owner client——在准入时由 `source->get_local_disk_client_id()` 捕获—— [mooncake-store/src/master_service.cpp:L3672-L3685](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3672-L3685)。

> 一句话总结 **holder_id 鉴权的必要性**：promotion 会新建一个对读者可见的 DRAM 副本，而「让它可见」这个动作必须等到**真正在写它的那个 holder client** 完成写入后才能做。`holder_id` 把「提交权」锁定在唯一的那个 client 手里，杜绝别的 client 抢先把半成品副本暴露出去。

**`alloc_id`：为什么不用「第一个 PROCESSING 内存副本」？**

因为同一个 key 上可能并发发生别的事（比如并发的 `Put`），它们也会挂出 PROCESSING 的 MEMORY 副本。如果提交时只是「把第一个 PROCESSING 内存副本标 COMPLETE」，就可能提交到**别人**那个还没写完的副本。`alloc_id` 精确钉死了「本次 promotion 分配出来的那个副本」，杜绝歧义—— [mooncake-store/src/master_service.cpp:L3793-L3810](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3793-L3810)（AllocStart 记录 alloc_id）、[L3826-L3848](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3826-L3848)（NotifySuccess 按 `GetReplicaByID(alloc_id)` 精确提交）。

**`start_time` 重置**：AllocStart 成功后会重置 `start_time`。原因是 reaper（清理线程）用 `start_time` 当超时锚点；排队等待阶段和「真正传输」阶段应各自享有完整的 TTL 窗口，否则一个排队很久的任务进入传输阶段时 TTL 已所剩无几，reaper 可能会在 RDMA 写到一半时把暂存副本删掉—— [mooncake-store/src/master_service.cpp:L3798-L3809](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3798-L3809)。

**size 防御**：`PromotionAllocStart` 还校验调用方传来的 `size` 必须等于准入时记录的 `object_size`，否则返回 `INVALID_PARAMS`——防止一个有 bug 或恶意的 caller 申请一个错误大小的缓冲（小的会 RDMA 越界写，大的会白白 pin 一块 DRAM）—— [mooncake-store/src/master_service.cpp:L3753-L3759](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3753-L3759)。

**为何每次心跳最多回一个任务**：`PromotionObjectHeartbeat` 用 `promotion_max_per_heartbeat_`（默认 1）限制每次返回的任务数。注释解释：每个任务是 client 侧一次同步的「SSD 读 + RDMA 写」，一次心跳里做太多会阻塞超过 client 存活窗口，master 会误判该 client 死了。多余的留在 `promotion_objects` 队列里下次再给—— [mooncake-store/src/master_service.cpp:L3703-L3715](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3703-L3715)。

**Eager 失败回收（NotifyPromotionFailure）**：client 侧 `ProcessPromotionTasks` 在 AllocStart 之后、提交之前的任何失败，都**立即**调 `NotifyPromotionFailure` 释放 master 侧名额和暂存缓冲，而不是干等 reaper 的 TTL（默认约 10 分钟）来回收——否则短暂的 SSD 抖动/RDMA 抖动会把 `promotion_queue_limit_` 名额占满整整一个 TTL—— [mooncake-store/src/file_storage.cpp:L757-L828](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/file_storage.cpp#L757-L828)。对应的 master 侧实现—— [mooncake-store/src/master_service.cpp:L3889-L3951](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3889-L3951)。

#### 4.3.5 代码实践

> 这是本讲的核心实践任务，对应规格里的要求。

**实践目标**：对照 `master_service.h` 中 `PromotionTask` 结构与相关注释，**口述/画出**一次 promotion-on-hit 的完整生命周期，并解释 `holder_id` 鉴权为何必要。这是一个**源码阅读型实践**（不依赖完整集群即可完成）。

**操作步骤**：

1. 打开 `mooncake-store/include/master_service.h`，定位 `struct PromotionTask`（L1143-L1149），通读它上面的注释块（L1121-L1142），特别记下 `source_id / alloc_id / holder_id / start_time` 四个字段各自解决什么问题。
2. 在 `mooncake-store/src/master_service.cpp` 里依次跳转到：
   - `GetReplicaList`（L1444）→ 看 `promotion_eligible` 如何判定（L1495-L1501）。
   - `TryPushPromotionQueue`（L3587）→ 逐行走四道闸（频率 3600、水位 3609、去重 3628、上限 3642），最后看 refcnt pin 与 `PromotionTask` 登记（3659-3685）。
   - `PromotionObjectHeartbeat`（L3691）→ 看 `max_per_heartbeat` 限流（3712）。
   - `PromotionAllocStart`（L3719）→ 看 holder 鉴权（3749）、size 防御（3757）、分配并记 `alloc_id`（3808）。
   - `NotifyPromotionSuccess`（L3813）→ 看 holder 鉴权（3838）、按 `alloc_id` 精确 `mark_complete`（3843-3848）、`source.dec_refcnt`（3853）、`promotion_in_flight_--`（3857）。
3. 打开 `mooncake-store/src/file_storage.cpp` 的 `ProcessPromotionTasks`（L687），看 client 侧的四步：`PromotionAllocStart`（734）→ `BatchLoad`（787）→ `PromotionWrite`（805）→ `NotifyPromotionSuccess`（816），以及每个失败分支如何 `NotifyPromotionFailure`（748、783、791、810、826）。

**需要观察的现象（阅读层面）**：

- 全程**没有任何「master 主动推送给 client」**的调用，所有任务都是 client 心跳来**拉**的。
- 源 LOCAL_DISK 副本从准入（`inc_refcnt`）到提交（`dec_refcnt`）期间一直被钉住，保证它在被拷回前不会被淘汰。
- 新 DRAM 副本在 `mark_complete` 之前对 `GetReplicaList` 不可见（读者只会看到 COMPLETE 副本），所以「半成品」窗口对用户透明。

**预期结果（口述答案）**：

> 「用户 `Get` key → `GetReplicaList` 发现只有 LOCAL_DISK、无 MEMORY → 标记 eligible → `TryPushPromotionQueue` 过频率/水位/去重/上限四道闸 → 钉住源副本、塞进 holder 队列、登记 `PromotionTask{holder_id}` → holder client 下次心跳 `PromotionObjectHeartbeat` 拉走任务 → `PromotionAllocStart`（holder 鉴权 + size 校验）分配一个 PROCESSING 的 DRAM 副本 → client 从 SSD `BatchLoad` 再 `PromotionWrite`（RDMA）→ `NotifyPromotionSuccess` 把那个副本 `mark_complete`、解钉源副本。`holder_id` 鉴权之所以必要：提交动作会让新 DRAM 副本对读者可见，必须由真正在写它的 holder 在写完后才能做，否则别的 client 抢先提交会让读者看到尚未写完的撕裂数据。」

**（可选）跑通验证**：若具备 RDMA 环境，开 `--enable_offload=true --offload_on_evict=true --promotion_on_hit=true`，先把一批对象写到只余 LOCAL_DISK 副本，再**重复**读某个 key 达到频率阈值，观察 metrics 里 `promotion_admitted` / `promotion_completed` 上升，且该 key 重新出现 MEMORY 副本。**待本地验证**（依赖真实集群与 metrics 抓取）。

#### 4.3.6 小练习与答案

**练习 1**：频率闸默认阈值是 2。如果把它设成 1，promotion 行为会如何变化？设成 0 会被接受吗？

**参考答案**：阈值 1 意味着「见一次访问就上浮」，promotion 会非常激进，容易和 offload 形成「下沉↔上浮」抖动。设成 0 **不会被接受**——master.cpp 在 flag 解析时把阈值 clamp 到 `[1, 255]`，构造期还有一道防御 clamp（threshold=0 会绕过频率闸，因为频次是 `uint8_t`，`freq < 0` 恒为假）—— [mooncake-store/src/master_service.cpp:L278-L287](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L278-L287)。

**练习 2**：假设 holder client 在 `PromotionAllocStart` 成功、分配了 DRAM 副本之后进程崩溃了，再也不会调 `NotifyPromotionSuccess`。master 会怎么处理这个悬挂的暂存副本？

**参考答案**：promotion 任务带 `start_time`，且 AllocStart 阶段会重置它。后台 reaper 会按 `put_start_release_timeout_sec_`（默认约 10 分钟）的超时扫到这个任务，走和 `NotifyPromotionFailure` 一样的「到期回收」路径：解钉源副本 refcnt、按 `alloc_id` 删掉那个 PROCESSING 的暂存 DRAM 副本、删任务、`promotion_in_flight_--`。所以崩溃最坏后果是暂存缓冲被白占一个 TTL，最终被回收——这正是 `NotifyPromotionFailure` 要 eager 调用、以及 `start_time` 重置要存在的理由（见 L3798-L3809 注释）。

### 4.4 Count-Min Sketch：访问频率的轻量估计

#### 4.4.1 概念说明

promotion 的「频率闸」需要一个能回答「这个 key 最近被访问了多少次」的数据结构。朴素做法是给每个 key 维护一个精确计数器，但 key 数量可能上千万，内存吃不消。

**Count-Min Sketch（CMS）** 是一个经典的**概率型数据结构**：用很小的固定内存（这里默认 `4096 宽 × 4 行` 共 16KB）估计每个 key 的频次，代价是**只会高估、不会低估**（对「够热才上浮」的策略是安全的，高估顶多多放行几次，去重闸会兜住）。它是 promotion 专用的—— [mooncake-store/include/master_service.h:L1759-L1762](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1759-L1762)（`promotion_sketch_` 仅在 `promotion_on_hit_` 为真时构造，内部自带互斥锁，任何 `GetReplicaList` 调用都能安全访问）。

#### 4.4.2 核心流程与源码精读

CMS 的原理：

- 一个 `depth_ × width_` 的二维表，每个格子是 `uint8_t`（最大 255）。
- 对 key 做 `depth_` 个**相互独立**的哈希，每行落到一个格子，全部 `+1`。
- 估计频次 = 这 `depth_` 个格子的**最小值**（取最小是为了让「哈希碰撞导致的高估」尽可能小）。
- 总增量达到 `width_*depth_` 时触发 **decay（衰减）**：把所有格子右移一位（除以 2），让老访问逐步淡出——这样草图跟踪的是「近期」频率而非全局累计频率。

**哈希独立性**：每行用一个种子和 `std::hash` 组合，再拌入两个魔数常数做 finalizer，得到相互独立的哈希函数—— [mooncake-store/include/count_min_sketch.h:L62-L70](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/count_min_sketch.h#L62-L70)。`0x9e3779b9...` 是经典的 golden ratio 常数。

**increment + 自动 decay**：`+1` 后取各行最小值返回；同时累计总增量，达到阈值就 `decayLocked()`—— [mooncake-store/include/count_min_sketch.h:L25-L39](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/count_min_sketch.h#L25-L39)。

**衰减**：所有格子统一 `>>= 1`—— [mooncake-store/include/count_min_sketch.h:L72-L79](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/count_min_sketch.h#L72-L79)。

为什么取最小值能降低误差，可以用最小估计的定义直观理解：哈希碰撞会让某些格子的计数偏大，但因为一个 key 的真实频次 **不超过** 它落在任一行的那个格子的当前值，所以**下界**就是这些格子的最小值。形式化地，对 key \(k\) 的估计 \(\hat{f}(k)\) 满足：

\[
\hat{f}(k) = \min_{i=1}^{\text{depth}} \text{table}[i,\, h_i(k)] \;\ge\; f(k)
\]

即估计值始终不低于真实频次 \(f(k)\)（只高估）。这正合 promotion 的胃口：宁可多放行（有去重闸兜底），不要漏掉真热 key。

#### 4.4.3 小练习与答案

**练习**：CMS 用 `uint8_t` 存计数（最大 255）。如果不做 decay，一个长期热点 key 会怎样？为什么源码在 `increment` 里就触发 decay 而不是另起一个定时线程？

**参考答案**：不做 decay 的话热点格子会很快饱和到 255（`if (table_[i][idx] < UINT8_MAX) ++...`），之后无法区分「255 次」和「100 万次」，频率闸失效。在 `increment` 内触发 decay（见 L35-L37）的好处是：不需要额外的定时线程、不引入跨线程状态，且衰减节奏自然绑定到访问量——访问越多衰减越频繁，正好让草图始终反映「近期」热度。

## 5. 综合实践

设计一个把本讲三块内容（offload、promotion、心跳驱动）串起来的**源码追踪任务**：

**任务**：跟踪同一个 key 的「**下沉再上浮**」全程，画出它在 Master 元数据里副本集合的变化。

**背景设定**：master 开 `--enable_offload=true --offload_on_evict=true --promotion_on_hit=true`，promotion 阈值用默认 2。

**追踪步骤**：

1. **写入阶段**：`PutStart` → `PutEnd`。此时 key 只有 1 个 COMPLETE 的 MEMORY 副本。
2. **下沉阶段**（高水位淘汰触发）：定位 `BatchEvict → try_evict_or_offload`（[L5299-L5360](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5299-L5360)）。
   - 说明此时 key 无 LOCAL_DISK 副本 → 走 push 分支 → MEMORY 副本 `inc_refcnt` + 登记 `offloading_tasks`。
   - 下一次心跳：client `OffloadObjectHeartbeat` 拉走 → 写 SSD → `NotifyOffloadSuccess`：`AddReplica` 一个 LOCAL_DISK 副本、MEMORY 副本 `dec_refcnt`。
   - **此刻 key 的副本集合**：1 个 COMPLETE 的 LOCAL_DISK 副本（MEMORY 副本可能在后续被淘汰消失）。
3. **上浮阶段**（重复 Get 触发）：
   - 第 1 次 `Get`：`GetReplicaList` 发现无 MEMORY、有 LOCAL_DISK → eligible → `TryPushPromotionQueue`：频率=1 < 2，被**频率闸**挡掉（`promotion_rejected_frequency++`）。
   - 第 2 次 `Get`：频率=2 ≥ 2，过闸；若水位/去重/上限都过 → 登记 `PromotionTask`、钉住源 LOCAL_DISK 副本。
   - 下一次心跳：client `PromotionObjectHeartbeat` 拉走 → `PromotionAllocStart` 挂一个 PROCESSING 的 MEMORY 副本 → SSD 读 → RDMA 写 → `NotifyPromotionSuccess`：该 MEMORY 副本 `mark_complete`。
   - **此刻 key 的副本集合**：1 个 LOCAL_DISK + 1 个 COMPLETE 的 MEMORY（读者重新能直接命中 DRAM）。
4. **回答三个问题**作为交付物：
   - 全程哪些动作发生在**用户请求路径**里（同步、影响延迟）？哪些在**心跳线程**里（异步、best-effort）？
   - 源 LOCAL_DISK 副本的 refcnt 在下沉与上浮两个阶段分别如何起伏？
   - 如果第 2 步下沉用的 Real Client（holder A）和第 3 步上浮时的 holder 不同，promotion 还能成功吗？为什么？（提示：`holder_id` 取自源副本的 `get_local_disk_client_id`，而源副本在哪个 client 的 SSD 上，就由那个 client 来拉取并执行上浮。）

**预期结果**：你能画出一张「key 的副本集合随时间演变」的时序图，并清晰标出每一步是同步还是异步、由谁触发、改了哪个 refcnt。这张图也是对本讲三块最小模块（Offload / Promotion-on-hit / 心跳驱动）的总检验。

## 6. 本讲小结

- **Offload 是「淘汰时保数据」**：高水位淘汰触发 offload-on-evict，被淘汰对象先经 `PushOffloadingQueue` 入队、心跳拉取后写 SSD、`NotifyOffloadSuccess` 登记 LOCAL_DISK 副本，从而即便 MEMORY 副本被回收，`Get` 仍可命中 SSD。它与「PutEnd 时即下沉」是两种可选模式，由 `offload_on_evict` 切换。
- **Promotion-on-hit 是「热点回温」**：`Get` 发现只有 LOCAL_DISK 副本时，经**频率/水位/去重/上限**四道闸后登记 `PromotionTask`，由 holder client 心跳拉取、分配 DRAM、SSD 读、RDMA 写、提交 COMPLETE，让热数据回到 DRAM。
- **心跳驱动是统一骨架**：Master 不主动推送，offload 与 promotion 任务都存在 per-client 队列里，由 Real Client 每 ~10s 的 `Heartbeat()` 主动拉取执行；`PromotionObjectHeartbeat` 每次最多回一个任务以防阻塞过 client 存活窗口。
- **`holder_id` 鉴权防撕裂读**：提交动作会让新 DRAM 副本对读者可见，必须由真正在写它的 holder client 在写完后才能做；`alloc_id` 则精确钉死本次分配的副本，避免错认并发的 PROCESSING 副本。两者合起来保证并发与故障下的正确性。
- **refcnt 贯穿迁移全程**：下沉时钉住源 MEMORY 副本（防写盘前被删），上浮时钉住源 LOCAL_DISK 副本（防拷回前被淘汰），完成后解除——refcnt 是「迁移期间保护数据源」的核心手段。
- **Count-Min Sketch 提供频率闸**：用约 16KB 固定内存估计访问频率，只高估不低估，配 decay 衰减跟踪「近期」热度，安全地服务于「够热才上浮」的策略。

## 7. 下一步学习建议

- **u6-l5 多租户配额**：本讲的 offload/promotion 都在「全局/单 client」粒度调度，配额机制则从「租户」粒度约束容量分配，二者配合理解才能看清 Store 的资源治理全貌。
- **u6-l6 RPC 服务与异步 Copy/Move/Drain 任务**：本讲的 offload/promotion 是「client 心跳拉取」式异步任务；Copy/Move/Drain 则是另一套「master 下发 + `FetchTasks`/`MarkTaskToComplete`」式异步任务。对比这两套异步任务模型，能更深刻理解 Store 控制面→数据面的协作设计。
- **继续读源码**：想加深理解，建议精读 `mooncake-store/src/master_service.cpp` 里 `TryPushPromotionQueue`（L3587）与 `ProcessPromotionTasks`（file_storage.cpp L687）这两段——它们分别是 promotion 的「准入」与「执行」两端，串起来就是本讲的灵魂。
