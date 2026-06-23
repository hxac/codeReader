# 高可用：Leader 选举与热备（Hot Standby）

> 适用阶段：advanced ｜ 依赖讲义：u5-l2（Store 的整体架构与 Master/Client 角色）

## 1. 本讲目标

本讲聚焦 Mooncake Store 的**高可用（High Availability, HA）**子系统。Mooncake Store 的 Master 是整个集群的“元数据大脑”：它记录每一个 KV 对落在哪些 Segment、哪些副本上。一旦 Master 单点故障，整个集群将无法正常 Put/Get。因此 Mooncake 在 Master 层引入了一套完整的 HA 方案。

学完本讲，你应当能够：

1. 说清楚 **Leader 选举**为什么需要 lease（租约）和 fencing token（隔离令牌），并能对比 etcd / k8s lease / redis 三种后端在实现上的关键差异。
2. 掌握**主→备的 OpLog 复制**链路：Primary 如何把元数据变更写成全局单调递增的操作日志，Standby 如何通过 watch/轮询订阅并按序应用，以及如何处理乱序、丢包与空洞（gap）。
3. 看懂**热备状态机**（`STOPPED → CONNECTING → SYNCING → WATCHING → PROMOTING → PROMOTED`），并解释主备切换（failover）时 OpLog 如何保证元数据“不丢、不复活”。
4. 理解两层状态模型：底层 `StandbyStateMachine`（复制生命周期）与上层 `MasterRuntimeState`（节点角色 starting/standby/candidate/…/serving）。

> 关于状态命名的一个说明：本讲的规格里把概念上的热备阶段描述为 `BOOTSTRAPPING / REPLICATING / PROMOTING / PRIMARY`。在真实源码中，底层复制状态机用的是另一套更细的命名（见 `StandbyState`）。两者的对应关系是：
> - `BOOTSTRAPPING`（引导/初始同步）≈ `CONNECTING` + `SYNCING`
> - `REPLICATING`（稳态复制）≈ `WATCHING`
> - `PROMOTING`（提升中）= `PROMOTING`
> - `PRIMARY`（已提升为主）≈ `PROMOTED` ＋ 上层 `MasterRuntimeState::kServing`（supervisor 进入 serve 阶段）

本讲一律以源码中的真实命名为准，并在用到概念名时给出对应。

---

## 2. 前置知识

### 2.1 为什么 Master 需要高可用

Mooncake 是一个**分离式**架构：数据面（实际承载 KV 的内存/SSD/GPU 段）与元数据面（Master）分离。Master 是强一致的元数据中心，天然是个“单点”。要让它高可用，常见的两条路：

- **共议（consensus）**：让 Master 本身就是 Raft/Paxos 复制组（如 etcd 本身）。
- **主备 + 外部仲裁（Leader/Follower + external arbiter）**：Master 仍是单主，但用一套外部协调服务（etcd / k8s lease / redis）来选出“谁是主”，并用一条复制日志把主的元数据同步给备。

Mooncake Store 的 HA 走的是**第二条路**：复用已有的 etcd（或其他后端）作为仲裁者，Master 进程本身不实现共识算法，而是实现“竞选 + 复制 + 切换”。这样做的好处是 Master 实现简单、可插拔后端；代价是要小心处理租约、隔离和复制延迟。

### 2.2 几个关键术语

- **Leader / Primary**：当前唯一对外提供写服务的 Master。
- **Standby / 备**：被动复制 Leader 元数据、不提供写服务的 Master 实例；随时准备被提升。
- **Lease（租约）**：Leader 持有的一把“带有效期”的锁。租约到期前只有它能续期；到期后别人可以抢。
- **Fencing token / 单调版本号**：每次租约易主都会递增的编号（Mooncake 里叫 `view_version`）。它的作用是“隔离”：旧 Leader 即使还在跑、还在写，它持有的版本号更小，新 Leader 持有更大版本号，下游可用版本号拒绝过期写入，避免**双主（split-brain）**。
- **OpLog（操作日志）**：Primary 把元数据变更（新增 key、撤销、删除）序列化成带全局 `sequence_id` 的记录，写到一个共享存储里；Standby 按序回放。这和数据库的 WAL、Kafka 的复制日志是同一类思想。
- **复制延迟（replication lag）**：Primary 的最新 `sequence_id` 减去 Standby 已应用的 `sequence_id`。延迟越小，切换时丢的数据越少。

### 2.3 一个直觉性的不变式

为了不丢元数据，整个系统维护这样一个不变式：

\[ \text{Standby 已应用的 sequence\_id} \;\le\; \text{共享存储里已持久化的 sequence\_id} \;\le\; \text{Primary 内存中分配的 sequence\_id} \]

主备切换时，必须把 Standby 追到“共享存储里的最新”之后才允许它对外服务，否则就会丢更新。本讲后半部分会反复回到这条不变式。

---

## 3. 本讲源码地图

本讲涉及的代码集中在 `mooncake-store` 的 `include` 与 `src/ha` 目录。按下表建立全局印象：

| 层 | 文件 | 作用 |
| --- | --- | --- |
| 协调者抽象 | `include/ha/leadership/leader_coordinator.h` | 选举后端的统一接口 `LeaderCoordinator` |
| 选举后端 | `src/ha/leadership/backends/{etcd,k8s,redis}/*_leader_coordinator.cpp` | 三种后端的选举实现 |
| 选举工厂 | `src/ha/leadership/leader_coordinator_factory.cpp` | 按配置创建后端 |
| 监督者 | `src/ha/leadership/master_service_supervisor.cpp` | **顶层主循环**：竞选→热备→提升→serve，驱动 `MasterRuntimeState` |
| Standby 控制 | `src/ha/standby_controller.cpp` | 把底层 `HotStandbyService` 的状态映射成 `MasterRuntimeState` |
| 热备服务 | `include/hot_standby_service.h`、`src/hot_standby_service.cpp` | Standby 复制与提升的核心 |
| 状态机 | `include/standby_state_machine.h`、`src/standby_state_machine.cpp` | 底层复制生命周期状态机 |
| OpLog 写入 | `include/ha/oplog/oplog_manager.h` | Primary 侧：分配 `sequence_id`、追加、持久化 |
| OpLog 存储 | `include/ha/oplog/oplog_store.h`、`include/ha/oplog/etcd_oplog_store.h` | 共享存储抽象与 etcd 实现（键布局） |
| OpLog 通知 | `include/ha/oplog/etcd_oplog_change_notifier.h` | Standby 侧：基于 etcd Watch 推送增量 |
| OpLog 复制器 | `include/ha/oplog/oplog_replicator.h` | 把通知器 + 应用器串成流水线 |
| OpLog 应用器 | `include/ha/oplog/oplog_applier.h`、`src/ha/oplog/oplog_applier.cpp` | 按序回放、处理空洞与提升补齐 |
| 类型定义 | `include/ha/ha_types.h` | `HABackendType`、`MasterView`、`LeadershipSession`、`MasterRuntimeState` 等 |
| 管理面 | `include/master_admin_service.h`、`src/master_admin_service.cpp` | HTTP `/role`、`/ha_status` 上报角色 |
| 集成测试 | `tests/ha/oplog/localfs_hot_standby_integration_test.cpp` | 无需外部依赖的端到端复制测试（实践依据） |

建议先读三个文件建立骨架：`master_service_supervisor.cpp`（顶层循环）、`hot_standby_service.cpp`（Standby 核心）、`standby_state_machine.h`（状态图）。

---

## 4. 核心概念与源码讲解

### 4.1 高可用整体架构：监督者（Supervisor）的双层状态模型

#### 4.1.1 概念说明

Mooncake 不是把“选举”和“复制”写在一起，而是用一个**监督者循环 `MasterServiceSupervisor`** 来编排整个生命周期。一个 Master 进程启动后，它的角色是动态的：

- 刚启动：`starting`
- 抢不到主、当备：`standby` / `recovering` / `catching_up`
- 正在抢主：`candidate`
- 抢到了、预热中：`leader_warmup`
- 正式对外服务：`serving`

这套角色由 `MasterRuntimeState` 定义：

[mooncake-store/include/ha/ha_types.h:114-122](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/ha_types.h#L114-L122)

同时，`MasterRuntimeRoleToString` 把这些状态归并为两个“角色”字符串（`leader` / `standby`），这正是 HTTP `/role` 接口返回的内容：

[mooncake-store/include/ha/ha_types.h:144-157](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/ha_types.h#L144-L157)

在这套上层角色之下，还有一套更细的**底层复制状态机** `StandbyStateMachine`（见 4.4）。两层分工：

- 上层 `MasterRuntimeState`：面向运维/监控，描述“这个节点现在在干什么、能不能接客”。
- 底层 `StandbyStateMachine`：面向实现，精确描述复制线程的每一个阶段，并对非法转移做严格校验。

`StandbyController` 就是把底层状态翻译成上层状态的“适配器”（见 4.1.3）。

#### 4.1.2 核心流程

监督者主循环 `RunSupervisorLoop` 的骨架（伪代码）：

```
启动 admin HTTP 服务，状态 = starting
进入 standby 模式（连不上主就先空转）
loop:
    coordinator = CreateLeaderCoordinator(spec)        # 创建仲裁客户端
    while 还没拿到 leadership:
        state = candidate
        view = coordinator.ReadCurrentView()           # 读当前谁是主
        if view 为空（没主）:
            r = coordinator.TryAcquireLeadership(self) # 抢主
            if 抢到: leadership_session = r.session; break
            else: 进入 standby；WaitForViewChange() 等别人
        else:
            进入 standby（跟随 view 指向的 leader）；WaitForViewChange()

    # 已经抢到 leadership：
    standby_controller.PromoteStandby()                # 把备提升为主
    state = leader_warmup
    WarmupLeadership(...)                              # 在租约期内续约，证明自己是合法主
    renew preflight = coordinator.RenewLeadership()    # serve 前再做一次续约校验
    monitor = coordinator.StartLeadershipMonitor(...)  # 后台盯租约，丢了就 server.stop()
    启动 RPC 服务，state = serving
    阻塞直到服务停止（被 monitor 触发或出错）
    释放 leadership，回到 standby，继续 loop
```

关键点：**“抢到租约 → 续约预热 → serve 前再续约一次 → 后台监控租约”** 这一串，构成了严格的“先证明是合法主，再开服务”的顺序，最大程度避免双主。

#### 4.1.3 源码精读

顶层循环在 `RunSupervisorLoop`。注意它在 `while(true)` 里反复“竞选—服务—失败回退”，是一个长期运行的监督循环：

[mooncake-store/src/ha/leadership/master_service_supervisor.cpp:191-451](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/master_service_supervisor.cpp#L191-L451)

其中“候选→抢主”的核心是这一段——先 `ReadCurrentView`，若没有主就 `TryAcquireLeadership`，抢不到就退回 standby 并 `WaitForViewChange` 等待视图变化：

[mooncake-store/src/ha/leadership/master_service_supervisor.cpp:222-298](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/master_service_supervisor.cpp#L222-L298)

抢到之后，依次执行“提升备 → 预热续约 → serve 前续约 → 启动监控 → 启动 RPC”。注意 `StartLeadershipMonitor` 注册的回调：一旦租约丢失，立刻 `server.stop()` 并把状态打回 `kStandby`，这是“主动让位”的体现：

[mooncake-store/src/ha/leadership/master_service_supervisor.cpp:385-409](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/master_service_supervisor.cpp#L385-L409)

把底层状态翻译成上层角色的逻辑在 `StandbyController` 实现里：

[mooncake-store/src/ha/standby_controller.cpp:45-69](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/standby_controller.cpp#L45-L69)

可以看到 `WATCHING` 且有 lag 会被映射成 `kCatchingUp`（正在追平），`PROMOTING/PROMOTED` 映射成 `kLeaderWarmup`。这套映射正是上层 `/ha_status` 能反映“追平没有”的来源。

#### 4.1.4 代码实践：观察节点的角色

1. **实践目标**：确认 Mooncake Master 暴露了哪些 HA 观测接口，并理解返回值来自哪个状态层。
2. **操作步骤**：
   - 打开 [mooncake-store/src/master_admin_service.cpp:354-368](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_admin_service.cpp#L354-L368)，阅读 `HandleRole` 与 `HandleHaStatus`。
   - 对照 [mooncake-store/src/master_admin_service.cpp:819-824](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_admin_service.cpp#L819-L824) 确认路由：`/role` 返回 `leader`/`standby`，`/ha_status` 返回更细的 `MasterRuntimeState` 字符串。
3. **需要观察的现象**：在一个真实双节点集群里，对两个 Master 各 `curl http://<master>:<metrics_port>/role`，应始终恰好有一个返回 `leader`、其余返回 `standby`。
4. **预期结果**：角色与租约一致；当 Leader 被 kill，几秒（取决于租约 TTL）后某个 Standby 的 `/ha_status` 会经历 `standby → candidate → leader_warmup → serving`。
5. 若本地无集群：此项为**待本地验证**，可仅做源码阅读。

#### 4.1.5 小练习与答案

- **练习**：为什么 supervisor 在 `TryAcquireLeadership` 抢到后，还要在 serve 之前再做一次 `RenewLeadership`（见 supervisor 第 358 行附近）？
- **参考答案**：抢主成功只能说明“刚才那一刻租约是我的”。在预热（`WarmupLeadership`）和构造 RPC 服务之间可能经过较长时间；serve 前再续约一次，是为了在“真正对外开服务”这个关键时间点确认租约仍然有效，避免一个租约已过期（但本地还不知道）的旧主对外写入，缩小双主窗口。

---

### 4.2 Leader 选举：etcd / k8s lease / redis 三后端

#### 4.2.1 概念说明

`LeaderCoordinator` 是选举后端的统一抽象。无论底层是 etcd、k8s 还是 redis，它都对外提供同一组方法：

[mooncake-store/include/ha/leadership/leader_coordinator.h:21-43](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/leadership/leader_coordinator.h#L21-L43)

核心方法语义：

- `ReadCurrentView()`：读“当前视图”——谁是 leader、视图版本号是多少。返回 `nullopt` 表示还没人选出来。
- `TryAcquireLeadership(addr)`：尝试抢主。结果分两种：`ACQUIRED`（抢到，附带 session）或 `CONTENDED`（已被别人占着，附带当前 view）。
- `RenewLeadership(session)`：续租约。返回 `true` 表示还是我的；`false` 表示丢了。
- `WaitForViewChange(known_version, timeout)`：以已知版本号为起点，阻塞等待视图发生变化（用于 standby 跟随 leader）。
- `StartLeadershipMonitor(session, on_lost)`：后台监控租约，丢失时回调（触发让位）。
- `ReleaseLeadership(session)`：主动让位。

后端类型用 `HABackendType` 表示，并支持字符串解析：

[mooncake-store/include/ha/ha_types.h:21-26](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/ha_types.h#L21-L26)

工厂按 `spec.type` 实例化对应后端，其中 **k8s 后端需要编译期开关 `STORE_USE_K8S_LEASE`**：

[mooncake-store/src/ha/leadership/leader_coordinator_factory.cpp:12-51](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/leader_coordinator_factory.cpp#L12-L51)

`LeadershipSession` 把“视图 + 后端发放的不透明持有令牌 `owner_token` + 租约 TTL”打包。注意注释强调 `owner_token` 是后端私有的（只有创建它的后端能解释），这保证了不同后端的持有证明互不通用，避免跨后端误用：

[mooncake-store/include/ha/ha_types.h:89-95](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/ha_types.h#L89-L95)

#### 4.2.2 核心流程：租约 + 单调视图版本号

三种后端在“抢主”这件事上的共同模型是 **“一个只能由持租约者续期的键 + 单调递增的版本号”**：

1. 抢主 = 原子地创建/占用这个键（带 TTL）。
2. 续约 = 在 TTL 到期前刷新 TTL；只有持有者能刷。
3. 失主 = TTL 过期，键被自动删除，版本号自增（视图变化）。
4. 防双主 = 用 `view_version` 作为 fencing token。新 leader 的版本号严格大于旧 leader。

租约的“安全续约节拍”通常满足：

\[ T_{\text{renew}} \;\le\; \frac{T_{\text{lease}}}{3} \]

即在租约的三分之一时间内就续一次，留出网络抖动和重试的余量。以 k8s client-go 的经典配置为例，lease=15s、renew-deadline=10s、retry=2s，正是这个比例的工程化体现（见 4.2.3 的 k8s 常量）。

#### 4.2.3 源码精读：三种后端的“抢主”差异

**① etcd 后端：lease + 带 lease 的原子创建（事务）**

etcd 后端先 `GrantLease` 申请一个租约，再用 `CreateWithLease` 把 master_view key 绑定到这个 lease 上创建——这是 etcd 的“创建型 CAS”：只有 key 不存在才能创建成功，失败返回 `ETCD_TRANSACTION_FAIL`，说明已被别人占着（`CONTENDED`）：

[mooncake-store/src/ha/leadership/backends/etcd/etcd_leader_coordinator.cpp:185-252](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/backends/etcd/etcd_leader_coordinator.cpp#L185-L252)

要点：
- `owner_token` 就是 lease_id 的编码（`MakeOwnerToken(lease_id)`），续约即 `KeepAlive(lease)`。
- 抢失败时会 `RevokeLease` 主动释放刚申请的 lease，避免泄漏。
- key 绑定 lease 意味着：持有者一旦停止 KeepAlive，lease 过期 → key 自动删除 → 视图版本变化 → standby 通过 `WaitForViewChange` 的 **prefix watch** 感知到，从而触发新一轮竞选。

续约 `RenewLeadership` 在 etcd 后端特别值得读，因为它说明了“adapter”设计：底层 wrapper 只暴露阻塞式 KeepAlive 循环，而接口契约是“一次性 renew”，于是 etcd 后端用一个 `keepalive_thread_` 在后台跑 KeepAlive，并把 `RenewLeadership` 的语义实现为“线程还在跑就返回 true，线程已停就返回 false”：

[mooncake-store/src/ha/leadership/backends/etcd/etcd_leader_coordinator.cpp:254-345](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/backends/etcd/etcd_leader_coordinator.cpp#L254-L345)

`WaitForViewChange` 则展示了 etcd watch 的经典用法，并有非常详细的注释解释“先 watch 后读”为何不会漏事件：

[mooncake-store/src/ha/leadership/backends/etcd/etcd_leader_coordinator.cpp:347-443](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/backends/etcd/etcd_leader_coordinator.cpp#L347-L443)

**② k8s 后端：Kubernetes Lease 锁（client-go leaderelection）**

k8s 后端复用 K8s 原生的 `coordination.k8s.io/Lease` 对象，通过 `K8sLeaseHelper::RunElection` 启动 client-go 的选举 goroutine，再 `WaitElected` 阻塞等待“我是否当选”：

[mooncake-store/src/ha/leadership/backends/k8s/k8s_leader_coordinator.cpp:108-176](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/backends/k8s/k8s_leader_coordinator.cpp#L108-L176)

与 etcd 后端最大的不同：
- 续约是 **client-go 内部自动做的**（`RenewLeadership` 的注释明确写道 “client-go handles renewal internally”），所以它只要确认“选举 goroutine 还在 active”就返回 true：
  [mooncake-store/src/ha/leadership/backends/k8s/k8s_leader_coordinator.cpp:178-253](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/backends/k8s/k8s_leader_coordinator.cpp#L178-L253)
- `view_version` 用的是 Lease 对象的 `transitions`（领导者更替次数）：
  [mooncake-store/src/ha/leadership/backends/k8s/k8s_leader_coordinator.cpp:81-106](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/backends/k8s/k8s_leader_coordinator.cpp#L81-L106)
- `WaitForViewChange` 因为 client-go watch 复杂，这里**退化为轮询** `GetHolder`（每 `kViewChangePollInterval` 查一次），这是三种后端里唯一用轮询而非推送等待视图变化的：

  [mooncake-store/src/ha/leadership/backends/k8s/k8s_leader_coordinator.cpp:255-294](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/backends/k8s/k8s_leader_coordinator.cpp#L255-L294)

  对应的 helper 接口见 [mooncake-store/include/k8s_lease_helper.h:11-42](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/k8s_lease_helper.h#L11-L42)。

**③ redis 后端：Lua 脚本保证原子 CAS**

redis 没有 etcd lease / k8s Lease 这种一等公民，于是 Mooncake 用 **Lua 脚本 + 带过期的 Hash 键** 自己实现一套等价语义。抢主脚本 `kAcquireLeadershipScript` 的逻辑是：若 key 已存在则失败（`{0}`），否则 `INCR` 一个 view_version 计数器、`HSET` 写入 leader_address/owner_token、`PEXPIRE` 设置过期：

[mooncake-store/src/ha/leadership/backends/redis/redis_leader_coordinator.cpp:46-60](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/backends/redis/redis_leader_coordinator.cpp#L46-L60)

续约脚本会校验 `owner_token` 匹配后才 `PEXPIRE`，保证只有持有者能续：

[mooncake-store/src/ha/leadership/backends/redis/redis_leader_coordinator.cpp:62-72](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/backends/redis/redis_leader_coordinator.cpp#L62-L72)

释放脚本同理，owner 不匹配返回 `-1`（用于检测“我是不是已经失主了”）：

[mooncake-store/src/ha/leadership/backends/redis/redis_leader_coordinator.cpp:74-84](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/backends/redis/redis_leader_coordinator.cpp#L74-L84)

> 为什么必须用 Lua 脚本？因为 “EXISTS + INCR + HSET + PEXPIRE” 这几步在并发下必须**原子**。Redis 单线程执行 Lua 保证了这一点，等价于 etcd 的事务 / k8s 的 Lease 乐观锁。

抢主调用 `EVAL` 执行该脚本：

[mooncake-store/src/ha/leadership/backends/redis/redis_leader_coordinator.cpp:340-427](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/backends/redis/redis_leader_coordinator.cpp#L340-L427)

续约是一个独立的后台线程周期性 `RenewLeadershipOnceLocked`：

[mooncake-store/src/ha/leadership/backends/redis/redis_leader_coordinator.cpp:727-770](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/backends/redis/redis_leader_coordinator.cpp#L727-L770)

#### 4.2.4 代码实践：对比三个后端的“抢主”原子原语

1. **实践目标**：用一句话概括每种后端“保证只有一个 winner”的底层原语。
2. **操作步骤**：分别打开下面三处，只看“抢主成功/失败的那一两行”：
   - etcd：[etcd_leader_coordinator.cpp:200-217](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/backends/etcd/etcd_leader_coordinator.cpp#L200-L217) —— `CreateWithLease` + `ETCD_TRANSACTION_FAIL`。
   - k8s：[k8s_leader_coordinator.cpp:115-140](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/backends/k8s/k8s_leader_coordinator.cpp#L115-L140) —— `RunElection` + `WaitElected`。
   - redis：[redis_leader_coordinator.cpp:358-401](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/backends/redis/redis_leader_coordinator.cpp#L358-L401) —— `EVAL kAcquireLeadershipScript`。
3. **需要观察的现象**：三者都返回 `AcquireLeadershipResult`，区别仅在“谁是裁判”。
4. **预期结果**：你能填出下表（答案见 4.2.5）。

| 后端 | 原子原语 | 谁负责续约 | 等待视图变化的方式 | view_version 来源 |
| --- | --- | --- | --- | --- |
| etcd | ? | ? | ? | ? |
| k8s  | ? | ? | ? | ? |
| redis | ? | ? | ? | ? |

#### 4.2.5 小练习与答案

- **练习 1（填表）**：补全上表。
  - **参考答案**：
    | 后端 | 原子原语 | 谁负责续约 | 等待视图变化 | view_version 来源 |
    | --- | --- | --- | --- | --- |
    | etcd | 带 lease 的 key 创建事务 `CreateWithLease` | 本进程 KeepAlive 线程 | prefix **watch**（推送） | etcd key 的 mod_revision |
    | k8s | K8s Lease + client-go 选举（乐观锁） | **client-go 内部自动** | **轮询** `GetHolder` | Lease `transitions` |
    | redis | Lua 脚本（EXISTS+INCR+HSET+PEXPIRE） | 本进程 renew 线程 `PEXPIRE` | 轮询（实现同 k8s 风格） | 自维护的 `view_version` 计数器（`INCR`） |

- **练习 2**：redis 后端为什么要在释放脚本里返回 `-1`（owner 不匹配）？supervisor 在什么场景下会用到这个返回值？
  - **参考答案**：返回 `-1` 表示“我想释放租约，但租约当前持有者已经不是我了”，即我早已失主。这让 `ReleaseLeadership` 能区分“正常让位（删自己的键）”与“我其实已经不是主了（键已被别人占）”。supervisor 在 serve 结束、主动 `ReleaseLeadership` 时据此打日志（`LogLeadershipReleaseWarning`），避免把“释放了别人的租约”误报成正常。

---

### 4.3 OpLog 复制：主写 → 共享存储 → 备回放

#### 4.3.1 概念说明

选举解决的是“谁是主”，**复制**解决的是“备如何与主保持一致”。Mooncake 用一条**操作日志（OpLog）**作为复制载体，而不是把整个元数据快照实时同步。

OpLog 的设计要点：

- **全局单调递增 `sequence_id`**：Primary 对每一次元数据变更分配一个递增编号，Standby 严格按编号回放，保证最终一致。
- **写共享存储**：Primary 把 OpLog 写到一个所有 Master 都能访问的存储（etcd 或本地文件系统），Standby 从同一个存储读。这是一种“**通过共享存储解耦生产者与消费者**”的模型，避免了 Primary↔Standby 之间的直连 RPC。
- **操作类型很少**：只有 `PUT_END`（对象可用了）、`PUT_REVOKE`（撤销某次 put）、`REMOVE`（删除 key）三类，外加一个已弃用的 `LEASE_RENEW`：

  [mooncake-store/include/ha/oplog/oplog_manager.h:22-30](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/oplog/oplog_manager.h#L22-L30)

- **持久化分级**：不是所有操作都同等重要。`PUT_END` 是幂等且容忍延迟的（best-effort 异步写）；而 `REMOVE`/`PUT_REVOKE` 关系到“内存是否已被回收”，**必须在返回前持久化**，否则备在提升时可能漏掉删除、读到指向已复用内存的旧描述符，造成静默数据损坏。这正是 `Append` 与 `AppendAndPersist` 两套接口存在的根本原因（见 4.3.3）。

#### 4.3.2 核心流程：生产者—共享存储—消费者

整体链路（与集成测试注释里的描述一致）：

```
Primary OpLogManager ──WriteOpLog──► OpLogStore (WRITER, etcd/本地fs)
                                          │
                                   /oplog/{cluster}/{seq}
                                   /oplog/{cluster}/latest
                                          ▲
                Standby  EtcdOpLogChangeNotifier (etcd Watch)
                              │ 增量事件
                              ▼
                        OpLogReplicator ──► OpLogApplier ──► StandbyMetadataStore
```

- **Primary 侧**：业务调用 `OpLogManager::Append(...)`（best-effort）或 `AppendAndPersist(...)`（强持久），分配 `sequence_id`，写到 `OpLogStore`；同时后台批量更新 `/latest` 方便备观测延迟。
- **共享存储（etcd）键布局**：
  - 每条记录：`/oplog/{cluster_id}/{sequence_id}`
  - 最新进度：`/oplog/{cluster_id}/latest`（批量更新，仅用于监控）
  
  见类注释：

  [mooncake-store/include/ha/oplog/etcd_oplog_store.h:19-28](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/oplog/etcd_oplog_store.h#L19-L28)

  以及常量：

  [mooncake-store/include/ha/oplog/etcd_oplog_store.h:187-189](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/oplog/etcd_oplog_store.h#L187-L189)

- **Standby 侧**：
  1. `EtcdOpLogChangeNotifier` 用 etcd Watch 订阅 `/oplog/{cluster}/` 前缀，收到增量事件；
  2. `OpLogReplicator` 把事件交给 `OpLogApplier`；
  3. `OpLogApplier` 校验 checksum、按 `sequence_id` 顺序应用，遇空洞则缓冲/请求/超时跳过；
  4. 写入本地 `StandbyMetadataStore`。

`OpLogStore` 抽象接口把读写、序列号管理、快照、清理统一抽象：

[mooncake-store/include/ha/oplog/oplog_store.h:27-62](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/oplog/oplog_store.h#L27-L62)

存储类型与角色（Primary 是 WRITER、Standby 是 READER）：

[mooncake-store/include/ha/oplog/oplog_store_factory.h:13-21](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/oplog/oplog_store_factory.h#L13-L21)

> 注意：OpLog 的 etcd 后端只有当 HA 后端是 etcd 时才启用（`has_oplog_following = (spec.type == ETCD)`，见 4.4.3 的 `standby_controller.cpp`）。redis/k8s 后端目前只支持 snapshot 引导，不做 OpLog 跟随。这是选后端时要权衡的能力差异。

#### 4.3.3 源码精读

**Primary 侧：分配、追加、持久化**

`OpLogManager` 持有一个有界 deque 缓冲，并记录 `first_seq_id_`/`last_seq_id_`。两个写入接口：

[mooncake-store/include/ha/oplog/oplog_manager.h:69-99](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/oplog/oplog_manager.h#L69-L99)

注意 `AppendAndPersist` 的设计——它采用 **seq 预分配**：`sequence_id` 先分配且永不复用，即使 etcd 写失败，重试也用同一个（更小的）`sequence_id`。这避免了“重试产生空洞”的问题。注释里解释了为什么 REMOVE 必须走这条强持久路径：

[mooncake-store/include/ha/oplog/oplog_manager.h:85-99](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/oplog/oplog_manager.h#L85-L99)

提升为新主时，需要把新主的 `last_seq_id` 接着旧的往下编，`SetInitialSequenceId` 就是干这个的：

[mooncake-store/include/ha/oplog/oplog_manager.h:104-107](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/oplog/oplog_manager.h#L104-L107)

**Standby 侧：通知器（etcd Watch）**

`EtcdOpLogChangeNotifier` 是增量推送的来源。它会先 `ReadOpLogSince(start_seq)` 拉历史，拿到一个 etcd revision 作为 watch 续点（resume point），再开 watch 循环；断线重连用指数退避，并 `SyncMissedEntries` 补齐：

[mooncake-store/include/ha/oplog/etcd_oplog_change_notifier.h:40-101](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/oplog/etcd_oplog_change_notifier.h#L40-L101)

这里有一个值得学习的并发安全设计：因为 etcd 的 Watch 回调可能在 `Stop()` 返回之后才到达，代码用 `ChangeNotifierCallbackContext`（带 mutex + 指针）而不是裸 `this` 作为回调上下文，避免回调访问已释放的对象：

[mooncake-store/include/ha/oplog/etcd_oplog_change_notifier.h:27-36](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/oplog/etcd_oplog_change_notifier.h#L27-L36)

**Standby 侧：复制器（流水线胶水）**

`OpLogReplicator` 很薄，它把 `OpLogChangeNotifier`（数据来源）和 `OpLogApplier`（落地）串起来，并维护 `last_processed_sequence_id_`：

[mooncake-store/include/ha/oplog/oplog_replicator.h:29-85](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/oplog/oplog_replicator.h#L29-L85)

**Standby 侧：应用器（按序回放 + 空洞处理）**

这是复制链路里逻辑最重的一环。`ApplyOpLogEntry` 先做 DoS 尺寸校验和 checksum 校验，再用 `expected_sequence_id_` 做全局顺序判断，分三种情况：

[mooncake-store/src/ha/oplog/oplog_applier.cpp:31-151](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/oplog/oplog_applier.cpp#L31-L151)

- **序号 = 期望**：直接应用（`ApplyPutEnd/ApplyRemove/...`），推进 `expected_sequence_id_`，并尝试消化已缓冲的后续条目。
- **序号 > 期望（未来条目）**：放进 `pending_entries_` 等待，不推进期望值（保证顺序）。
- **序号 < 期望（重复/迟到）**：若是曾被跳过的 delete/revoke，则补删；若是 PUT_END，则丢弃——这一条极其关键，注释里强调“**不能复活可能已过期的元数据**”：

  [mooncake-store/src/ha/oplog/oplog_applier.cpp:60-98](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/oplog/oplog_applier.cpp#L60-L98)

`ProcessPendingEntries` 处理空洞：先登记 `missing_sequence_ids_`，超过 1s 就向 etcd `RequestMissingOpLog` 主动拉，超过 3s 仍缺就 `skipped_sequence_ids_` 跳过（避免单个缺失卡住全局进度）：

[mooncake-store/src/ha/oplog/oplog_applier.cpp:174-229](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/oplog/oplog_applier.cpp#L174-L229)

应用 `PUT_END` 时会把 payload（struct_pack 序列化的 `MetadataPayload`）反序列化成结构化 `StandbyObjectMetadata` 存下，这样提升后能立刻服务：

[mooncake-store/src/ha/oplog/oplog_applier.cpp:425-474](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/oplog/oplog_applier.cpp#L425-L474)

#### 4.3.4 代码实践：跑通一条最简单的复制链路

集成测试 `localfs_hot_standby_integration_test.cpp` 用本地文件系统代替 etcd，是理解整条链路最省事的入口。

1. **实践目标**：在不部署 etcd 的前提下，观察“Primary 写 OpLog → Standby 回放 → 元数据一致”。
2. **操作步骤**：
   - 阅读测试夹具，看清 Primary 端如何创建 WRITER 存储并写到共享目录：
     [mooncake-store/tests/ha/oplog/localfs_hot_standby_integration_test.cpp:88-106](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/ha/oplog/localfs_hot_standby_integration_test.cpp#L88-L106)
   - 阅读 `TestPrimaryStandbySync`：写 10 条（前 9 条用 best-effort `Append`，最后 1 条用 `AppendAndPersist` 触发落盘），再启动 Standby 并 `WaitForSync`：

     [mooncake-store/tests/ha/oplog/localfs_hot_standby_integration_test.cpp:137-175](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/ha/oplog/localfs_hot_standby_integration_test.cpp#L137-L175)
   - 在构建产物里执行（前提是已用 `-DSTORE_USE_ETCD=OFF` 或未启用 etcd 的方式编译了测试二进制）：
     ```bash
     # 示例命令，具体二进制名/路径以你的 build 为准（待本地验证）
     ctest -R LocalFsHotStandbyIntegration --output-on-failure
     ```
3. **需要观察的现象**：日志里能看到 Standby 的状态从 `SYNCING` 进入 `WATCHING`，`applied_seq_id` 逐步追上 `primary_seq_id`，最终 `lag_entries == 0`。
4. **预期结果**：`standby.ExportMetadataSnapshot(snapshot)` 导出的 key 集合与 Primary 写入的 10 个 key 一致。
5. **如果无法运行**：这是**源码阅读型实践**的最小形态——在 `WaitForSync`（第 108-126 行）里追踪：它正是通过轮询 `GetSyncStatus()`，等待 `state == WATCHING && lag_entries == 0`。这条断言就是“复制完成”的判据。

#### 4.3.5 小练习与答案

- **练习 1**：假设 Primary 连写了 5 条 OpLog（seq 1..5），其中 seq 3 因为网络抖动没及时写到 etcd，但 seq 4、5 写成功了。Standby 的 watch 先收到 4、5。`OpLogApplier` 会怎么处理？
- **参考答案**：收到 4 时，期望值是 3，4 是“未来条目”，会被放入 `pending_entries_`；收到 5 同理。`ProcessPendingEntries` 会把 3 登记进 `missing_sequence_ids_`；满 1s 后主动 `RequestMissingOpLog(3)` 向 etcd 拉取；若拉到就回放 3，然后顺序消化 pending 里的 4、5。若 3 在 3s 内始终拉不到，会被记入 `skipped_sequence_ids_` 跳过以避免卡死，但一旦迟到的 3 真到达，对 delete/revoke 仍会补删、对 PUT_END 则丢弃。

- **练习 2**：为什么 `REMOVE` 必须走 `AppendAndPersist`（强持久），而不能像 `PUT_END` 那样 best-effort？
- **参考答案**：`REMOVE` 通常意味着底层内存/段已被回收、可能被复用。如果它是 best-effort 且在持久化前 Primary 就崩，那么新主（原 Standby）提升后可能**没看到这次删除**，从而对外返回指向“已复用内存”的旧描述符，造成静默数据损坏。`PUT_END` 则是幂等的、容忍延迟的（最坏只是备暂时没这条，提升后再补）。所以删除类操作必须先持久化再返回。

---

### 4.4 热备状态机：从 STOPPED 到 PROMOTED

#### 4.4.1 概念说明

`StandbyStateMachine` 是 `HotStandbyService` 内部用来精确管理“复制线程生命周期”的线程安全状态机。它把复制过程拆成 9 个状态、17 个事件，所有转移都经过显式校验并记录历史，便于排障。

状态枚举与头部注释里的**官方状态转移图**：

[mooncake-store/include/standby_state_machine.h:12-76](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/standby_state_machine.h#L12-L76)

把头部注释的 ASCII 图用更规整的方式表达，核心路径是：

```
STOPPED --START--> CONNECTING --CONNECTED--> SYNCING --SYNC_COMPLETE--> WATCHING
                                                                   |
                                                                   | (断线/错误)
                                                                   v
                                              RECONNECTING <-- DISCONNECTED/WATCH_BROKEN
                                                                   |
                                              RECOVERING <-- MAX_ERRORS_REACHED
                                                                   |
                              (恢复) ----> WATCHING <---- RECOVERY_SUCCESS
                                  |
                                  | PROMOTE
                                  v
                              PROMOTING --PROMOTION_SUCCESS--> PROMOTED --STOP--> STOPPED
                                  |
                                  | PROMOTION_FAILED
                                  v
                                FAILED
```

事件枚举（用户动作 / 连接 / 同步 / watch / 恢复 / 提升 / 错误）：

[mooncake-store/include/standby_state_machine.h:109-139](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/standby_state_machine.h#L109-L139)

几个关键的“能力查询”方法决定了外部行为——比如只有在 `WATCHING` 才允许提升：

[mooncake-store/include/standby_state_machine.h:246-248](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/standby_state_machine.h#L246-L248)

#### 4.4.2 核心流程：状态转移 + 提升时的补齐

状态机的核心是一张**转移表**（`ValidateTransition`）：给定 `(当前状态, 事件)`，判定是否允许、跳到哪个新状态。例如只有 `WATCHING` 上 `PROMOTE` 才允许，且跳到 `PROMOTING`：

[mooncake-store/src/standby_state_machine.cpp:81-84](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/standby_state_machine.cpp#L81-L84)

`PROMOTING` 上 `PROMOTION_SUCCESS` 才到 `PROMOTED`：

[mooncake-store/src/standby_state_machine.cpp:162-167](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/standby_state_machine.cpp#L162-L167)

`ProcessEvent` 用 **CAS 双检** 模式保证多线程下转移原子：先读旧状态，在锁内再读一次确认没被别的线程改掉，再写新状态、记历史、回调：

[mooncake-store/src/standby_state_machine.cpp:209-266](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/standby_state_machine.cpp#L209-L266)

**提升（Promote）流程**（对应概念里的 `REPLICATING → PROMOTING → PRIMARY`）：

1. 校验 `IsReadyForPromotion()`（必须在 `WATCHING`，但允许有 lag，提升后再补）：

   [mooncake-store/src/hot_standby_service.cpp:455-475](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/hot_standby_service.cpp#L455-L475)

2. `ProcessEvent(PROMOTE)`：`WATCHING → PROMOTING`。
3. 停掉复制器（`oplog_replicator_->Stop()`）。
4. `ResolvePromotionGapsLocked()`：尽力把当前已知的空洞补一次（不阻塞提升）：

   [mooncake-store/src/hot_standby_service.cpp:477-501](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/hot_standby_service.cpp#L477-L501)

5. `FinalCatchUpForPromotionLocked()`：从共享存储**批量读**尚未应用的 OpLog，一次性追到最新（有 30s / 100 批上限，超限也不阻塞提升）：

   [mooncake-store/src/hot_standby_service.cpp:503-570](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/hot_standby_service.cpp#L503-L570)

6. `ProcessEvent(PROMOTION_SUCCESS)`：`PROMOTING → PROMOTED`，更新 `applied_seq_id_`/`primary_seq_id_` 为最终值。

   完整 `Promote()` 见：

   [mooncake-store/src/hot_standby_service.cpp:572-628](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/hot_standby_service.cpp#L572-L628)

提升后，supervisor 会用 `GetLatestAppliedSequenceId()` 拿到最终 `sequence_id`，让新主的 `OpLogManager` 从这里继续编号（`SetInitialSequenceId`），保证编号连续不重叠。

`Start()` 的流程把状态机串起来：`START → CONNECTING → CONNECTED → SYNCING`（做 bootstrap baseline，可选加载 snapshot）`→ SYNC_COMPLETE → WATCHING`：

[mooncake-store/src/hot_standby_service.cpp:132-189](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/hot_standby_service.cpp#L132-L189)

#### 4.4.3 源码精读：能力差异与提升保证

`HotStandbyConfig` 里两个开关决定了 Standby 的能力组合，直接决定走 `CapabilityDrivenStandbyController` 还是 `NoopStandbyController`：

[mooncake-store/include/hot_standby_service.h:33-54](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/hot_standby_service.h#L33-L54)

而能力的判定就在 `standby_controller.cpp`：只有 etcd 后端才有 OpLog 跟随（`has_oplog_following = (spec.type == ETCD)`）；非 etcd 后端若开了 snapshot restore，则走 snapshot-only 模式（只引快照、不跟 OpLog）：

[mooncake-store/src/ha/standby_controller.cpp:37-43](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/standby_controller.cpp#L37-L43)

`PromoteStandby` 最终委托给 `HotStandbyService::Promote()`，失败会 `Stop()` 复位：

[mooncake-store/src/ha/standby_controller.cpp:184-210](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/standby_controller.cpp#L184-L210)

提升时的“不丢元数据”保证，关键在 `TryResolveGapsOnceForPromotion`：它把所有 `missing` + `skipped` 的 `sequence_id` 集中起来，去 etcd **逐个回读**；对 `REMOVE/PUT_REVOKE` 补删，对 `PUT_END` 丢弃（标记已处理，不再重试）。注意它**只清理成功取回的 gap**，失败的 gap 保留以便重试/监控：

[mooncake-store/src/ha/oplog/oplog_applier.cpp:348-416](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/oplog/oplog_applier.cpp#L348-L416)

#### 4.4.4 代码实践：绘制状态转移图 + 解释“切换不丢元数据”

这是本讲规格里指定的核心实践任务。

1. **实践目标**：(a) 画出 `STOPPED → … → PRIMARY` 的完整状态转移图；(b) 用一句话说清 OpLog 在主备切换中如何保证元数据不丢。
2. **操作步骤**：
   - 通读 [standby_state_machine.h:12-76](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/standby_state_machine.h#L12-L76) 的状态图与 [standby_state_machine.cpp:10-207](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/standby_state_machine.cpp#L10-L207) 的转移表。
   - 把概念四态映射进来，亲手在纸上画出（参考 4.4.1 的图）：
     - `BOOTSTRAPPING`：`STOPPED → CONNECTING → SYNCING`（`Start()` 触发，期间做 baseline 引导）；
     - `REPLICATING`：`WATCHING`（`SYNC_COMPLETE` 进入，稳态跟随 OpLog）；
     - `PROMOTING`：`PROMOTING`（`PROMOTE` 事件进入，期间做 gap 补齐 + final catch-up）；
     - `PRIMARY`：`PROMOTED`（`PROMOTION_SUCCESS`）＋ supervisor 把上层 `MasterRuntimeState` 置为 `kServing`。
   - 追踪“不丢”链条：`Promote()`（[hot_standby_service.cpp:572](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/hot_standby_service.cpp#L572)）→ `ResolvePromotionGapsLocked`（[L477](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/hot_standby_service.cpp#L477)）→ `FinalCatchUpForPromotionLocked`（[L503](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/hot_standby_service.cpp#L503)）→ `TryResolveGapsOnceForPromotion`（[oplog_applier.cpp:348](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/oplog/oplog_applier.cpp#L348)）。
3. **需要观察的现象**：注意到“不丢”由三重保证叠加：
   - 共享存储里**已持久化**的 OpLog（尤其 `REMOVE` 强制走 `AppendAndPersist`）不会因 Primary 崩溃而丢；
   - 提升时先 `final catch-up` 批量读共享存储，把进度追到“存储里最新”；
   - 对历史空洞，`gap resolve` 再逐个回读，且对删除类操作“宁可补删”。
4. **预期结果**：你能写出这样的结论——**“OpLog 保证不丢”的本质是：删除类变更强持久到共享存储，提升前 Standby 会从该存储把进度追到最新并对空洞逐个补齐；删除补删、PUT_END 不复活，因此新主对外提供的元数据不会比共享存储里已确认的更新更旧。**
5. 一句话答案（参考）：**主备切换时，Standby 在 `PROMOTING` 阶段会停复制、补空洞、批量追平共享存储里的最新 OpLog，且删除类操作一律补齐（PUT_END 不复活），从而保证提升后元数据不丢、不复活。**

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `IsReadyForPromotion()` 只允许在 `WATCHING` 提升，但 `Promote()` 内部又允许“带 lag 提升”？
- **参考答案**：`WATCHING` 是“稳态跟随”状态，表明复制链路健康、基线已建立，是提升的安全前提。允许带 lag 是工程权衡：完全追平可能要等很久，而 `Promote()` 内部有 `final catch-up`（最多 30s/100 批）和 `gap resolve`，能在提升过程中把剩余的补上；即使超限，最坏也只是新主上线后继续从共享存储补同步，不会丢数据（因为强持久语义保证了删除类操作已在存储里）。

- **练习 2**：状态机的 `ProcessEvent` 为什么要做“锁内二次读状态”的 CAS 双检？
- **参考答案**：因为状态机会被多个线程并发驱动（复制线程、watch 回调、提升线程）。从读旧状态到加锁之间，旧状态可能已被别的线程改变。锁内再读一次并重新 `ValidateTransition`，能避免基于过期状态做出非法转移，保证转移串行且一致。

---

## 5. 综合实践

**任务：用一次“主挂→备升”的纸面推演，把三块知识串起来。**

设定：etcd 后端集群，Primary P 正在服务（`view_version=7`），Standby S 处于 `WATCHING`、`lag_entries=2`。现 P 所在机器突然断电。

请按顺序回答并对应到源码：

1. **租约阶段**：P 停止 KeepAlive，etcd lease 在 TTL 后过期，master_view key 被删、`view_version` 自增。S 的 supervisor 此刻正在 `WaitForViewChange` 里——它是如何感知到变化的？（提示：[etcd_leader_coordinator.cpp:347-443](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/backends/etcd/etcd_leader_coordinator.cpp#L347-L443) 的 watch）。
2. **竞选阶段**：S（可能还有别的候选者）进入 `candidate`，调用 `TryAcquireLeadership`。谁会成为新主？fencing token 是多少，它的隔离作用体现在哪？
3. **提升阶段**：胜者调用 `PromoteStandby` → `HotStandbyService::Promote()`。追踪它在 `PROMOTING` 阶段做了哪些“追平”动作（[hot_standby_service.cpp:572-628](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/hot_standby_service.cpp#L572-L628)）。
4. **服务阶段**：supervisor 做 serve 前续约、启动监控、启动 RPC，上层 `MasterRuntimeState` 进入 `kServing`。此时若停电的 P 恢复上线，它持有的 `view_version=7` 比当前主小，会发生什么？（提示：它在 `TryAcquireLeadership` 会失败 → 退回 standby 跟随新主。）

**交付物**：
- 一张时序图（P、S、etcd 三条泳道），标注关键事件与 `view_version` 变化。
- 一段说明：为什么即使 S 提升时还有 `lag_entries=2`，新主上线后元数据也不会丢（引用 4.4.4 的三重保证）。

> 这是“源码阅读型综合实践”，无需运行集群；但若你有 etcd 测试环境，可用 `mooncake_master` 起两个实例、`kill -9` 主进程，配合 `curl /role` 与日志复现上述时序（待本地验证）。

---

## 6. 本讲小结

- Mooncake Store 的 HA 走 **“主备 + 外部仲裁”** 路线：用 etcd/k8s lease/redis 选主，用 OpLog 把主元数据同步给备，监督者 `MasterServiceSupervisor` 编排“竞选→预热→serve→让位”循环。
- **Leader 选举**三后端共享“租约 + 单调 `view_version`（fencing token）”模型，但原子原语不同：etcd 用带 lease 的创建事务，k8s 复用原生 Lease + client-go（续约自动化），redis 用 Lua 脚本（EXISTS+INCR+HSET+PEXPIRE）。`view_version` 是防双主的关键。
- **OpLog 复制**是“通过共享存储解耦生产者/消费者”：Primary 写 `/oplog/{cluster}/{seq}`，Standby 用 etcd Watch 订阅增量、`OpLogApplier` 按序回放、处理空洞；`REMOVE` 强持久（`AppendAndPersist`）是“不丢”的前提。
- **热备状态机** `StandbyStateMachine` 用 9 状态/17 事件精确管理复制生命周期，CAS 双检保证并发安全；只有 `WATCHING` 可提升，提升在 `PROMOTING` 做 gap 补齐 + final catch-up，最终 `PROMOTED`。
- 主备切换的“不丢不复活”由三重保证叠加：删除强持久 + 提升前批量追平共享存储 + 空洞逐个补齐（删除补删、PUT_END 丢弃）。
- 两层状态模型分工：底层 `StandbyStateMachine` 面向实现，上层 `MasterRuntimeState` 面向运维（`/role`、`/ha_status`）。

---

## 7. 下一步学习建议

- **快照与引导**：本讲多次提到 `snapshot_provider_` 和 `LoadSnapshotBaselineLocked`。下一讲建议深入 `mooncake-store/src/ha/snapshot/`（catalog、object store、`CatalogBackedSnapshotProvider`），理解冷启动时如何用快照建立基线、再叠 OpLog。
- **Primary 侧 OpLog 写入**：本讲聚焦 Standby。建议接着读 `master_service.h` / `master_service.cpp` 中 `OpLogManager` 的实际调用点（Put/Remove 路径），看 Primary 如何在业务流程里 `Append`/`AppendAndPersist`。
- **etcd Watch 的工程细节**：`EtcdOpLogChangeNotifier` 和 etcd 后端的 `WaitForViewChange` 都涉及“revision 续点、断线重连、补齐”。对照 `etcd_helper.h` 与 Go wrapper 行为，能加深对“最终一致但不漏”的理解。
- **可观测性**：把 `ha_metric_manager.h`、`hybrid_metric.h` 与本讲的 `HAMetricManager::inc_oplog_*` 串起来，搭建一套 HA 健康度看板（lag_entries、watch 断连、checksum 失败、状态转移次数）。
- **跨讲串联**：回顾 u5-l2 中 Master 的元数据结构，再结合本讲，体会“元数据 + OpLog + 租约”如何共同支撑 Store 的强一致高可用。
