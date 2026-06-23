# 元数据服务器后端：etcd / HTTP / Redis

## 1. 本讲目标

本讲聚焦 Mooncake 中「元数据（metadata）究竟存在哪里」这一关键选择。读完后你应当能够：

- 区分 Mooncake 里**两个不同层次的元数据**：传输引擎（Transfer Engine, TE）的成员发现/握手元数据，与 Store 的 HA（高可用）元数据（选主、操作日志、快照）。
- 说出 **P2P 握手 / 内嵌 HTTP / etcd / Redis** 四种元数据后端各自的适用场景与代价。
- 看懂三组 CMake 开关：TE 层的 `USE_ETCD` / `USE_REDIS` / `USE_HTTP`，以及 Store HA 层的 `STORE_USE_ETCD` / `STORE_USE_REDIS` / `STORE_USE_K8S_LEASE`，并能解释它们之间的**互斥约束**。
- 理解三个 helper 类的职责边界：`HttpMetadataServer`、`EtcdHelper`、`K8sLeaseHelper`。
- 针对单机开发、小集群、生产大规模三种部署，给出有依据的元数据后端选型建议。

> 本讲是高级（advanced）内容。建议先读 [u2-l2 传输元数据](u2-l2-transfer-metadata.md)（理解 TE 为什么需要元数据）与 [u5-l1 Store 架构](u5-l1-store-architecture.md)（理解 master/segment 模型），以及同单元的 [u7-l1 HA 主备](u7-l1-ha-leader-standby.md)。

## 2. 前置知识

- **元数据（metadata）**：描述「数据的数据」。在 Mooncake 里，它通常是 JSON 文本，记录某个 segment（内存段）在哪台机器、用哪种传输协议、句柄是什么。真正的大块 KV cache 数据走 RDMA/TCP 直传，元数据只负责「指路」。
- **外部协调服务**：像 etcd、Redis、Kubernetes（K8s）这类独立运行的中间件，专门用来存少量但需要**强一致**或**高可用**的小数据（配置、锁、租约）。它们自带持久化、复制、故障转移，Mooncake 直接复用而不自己造轮子。
- **租约（lease）与选主（leader election）**：租约是一张「限期通行证」，到期自动作废；选主是多副本通过争抢一张租约来决定谁是 leader。etcd 的 Lease、K8s 的 Lease 对象、Redis 的过期 key 都能实现这一语义。
- **cgo / Go c-shared 库**：Mooncake 的 C++ 进程通过动态链接一个用 Go 编译出的 `.so`（如 `libetcd_wrapper.so`、`libk8s_lease_wrapper.so`）来调用 Go 版本的 etcd client 与 K8s client-go。一个进程内**只能加载一份 Go runtime**，这正是后面互斥约束的根因。
- **CAS（Compare-And-Swap）事务**：etcd 的事务原语 `Txn.If(cmp).Then(op).Commit()`，可在服务端原子地「满足条件才写入」。Mooncake 用 `CreateRevision == 0`（键不存在）作为条件，实现「键不存在才创建」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mooncake-common/common.cmake` | 定义 TE 层元数据开关 `USE_ETCD` / `USE_REDIS` / `USE_HTTP`，并在三者全关时退化到 P2P 握手 |
| `CMakeLists.txt`（根） | 定义 Store HA 层开关 `STORE_USE_ETCD` / `STORE_USE_REDIS` / `STORE_USE_K8S_LEASE`，以及互斥检查 |
| `mooncake-store/CMakeLists.txt` | 把上面三个 Store 开关翻译成头文件/库的查找与链接 |
| `mooncake-transfer-engine/src/transfer_metadata_plugin.cpp` | TE 的元数据存储插件工厂：按连接串协议选择 etcd/redis/http 插件，以及 P2P 握手插件 |
| `mooncake-store/include/http_metadata_server.h` / `mooncake-store/src/http_metadata_server.cpp` | **内嵌 HTTP 元数据服务器**：master 进程内置一个进程内 KV，供 TE 的 HTTP 插件访问 |
| `mooncake-store/include/etcd_helper.h` / `mooncake-store/src/etcd_helper.cpp` | **EtcdHelper**：Store 侧封装 etcd 操作（含 lease/watch/事务），转调 Go wrapper |
| `mooncake-common/etcd/etcd_wrapper.go` | Go 版 etcd clientv3 封装，区分 TE/Store/快照三套独立 client |
| `mooncake-store/include/k8s_lease_helper.h` / `mooncake-store/src/k8s_lease_helper.cpp` | **K8sLeaseHelper**：Store 侧封装 K8s Lease 选主，转调 Go wrapper |
| `mooncake-common/k8s-lease/k8s_lease_wrapper.go` | Go 版 client-go 封装，用 `leaderelection` 做 K8s Lease 选主 |
| `mooncake-store/include/ha/ha_types.h` | HA 后端类型枚举 `HABackendType` 与可用性校验 |
| `mooncake-store/src/ha/leadership/leader_coordinator_factory.cpp` | Store HA 选主协调器工厂：按类型创建 etcd/redis/k8s 协调器 |
| `mooncake-store/src/ha/oplog/oplog_store_factory.cpp` | 操作日志存储工厂，etcd 分支受 `STORE_USE_ETCD` 守卫 |
| `mooncake-store/tests/ha/leadership/ha_backend_availability_test.cpp` | 验证「后端可用性 = 编译开关」的单元测试（本讲实践的锚点） |
| `mooncake-store/src/master.cpp` | master 主程序：按 `enable_http_metadata_server` 启动内嵌 HTTP 服务器 |

## 4. 核心概念与源码讲解

### 4.1 两个层次的元数据后端

#### 4.1.1 概念说明

初学者最容易混淆的一点：Mooncake 里其实有**两套独立的「元数据后端」选择**，它们用不同的 CMake 开关、服务不同的目的：

1. **TE 层元数据（传输引擎）**——回答「peer 在哪、segment 元信息是什么」。由连接串（如 `etcd://...`、`http://...`、`redis://...`）的**协议前缀**决定用哪个存储插件；如果什么外部服务都不想部署，就退化成 P2P 握手。开关是 `USE_ETCD` / `USE_REDIS` / `USE_HTTP`。
2. **Store HA 层元数据（高可用）**——回答「谁是 leader、操作日志增量、快照目录」。仅当 Store 开启 HA（`enable_ha`）时才需要，由 `HABackendType`（`etcd` / `redis` / `k8s`）决定。开关是 `STORE_USE_ETCD` / `STORE_USE_REDIS` / `STORE_USE_K8S_LEASE`。

| 层次 | 解决什么问题 | 可选后端 | 开关前缀 |
| --- | --- | --- | --- |
| TE 元数据 | peer 发现 + segment 握手信息 | P2P 握手 / HTTP / etcd / redis | `USE_*` |
| Store HA | 选主 + oplog + 快照 | etcd / redis / k8s（Lease） | `STORE_USE_*` |

两个层次可以独立选择：例如 TE 用 P2P 握手（零依赖），同时 Store HA 用 etcd 做选主，是合法且常见的组合。

#### 4.1.2 核心流程

```text
TE 客户端 / master 启动
        |
        | 拿到一个 metadata 连接串，例如 "etcd://10.0.0.1:2379"
        v
MetadataStoragePlugin::Create(conn_string)   # 按协议前缀分发
        |
        +-- "etcd://"  -> EtcdStoragePlugin   (需 USE_ETCD)
        +-- "redis://" -> RedisStoragePlugin  (需 USE_REDIS)
        +-- "http://"  -> HTTPStoragePlugin   (需 USE_HTTP，对接某处的 HttpMetadataServer)
        +-- 都没开     -> 只有 SocketHandShakePlugin（P2P，无中心元数据）

Store HA（enable_ha=true）
        |
        v
CreateLeaderCoordinator(spec)                 # 按 HABackendType 分发
        |
        +-- ETCD  -> EtcdLeaderCoordinator    (需 STORE_USE_ETCD)
        +-- REDIS -> RedisLeaderCoordinator   (需 STORE_USE_REDIS)
        +-- K8S   -> K8sLeaderCoordinator     (需 STORE_USE_K8S_LEASE)
```

#### 4.1.3 源码精读

TE 层的「按协议分发」实现在 `transfer_metadata_plugin.cpp`：先把连接串切成 `协议://地址`，再依次匹配：

[transfer_metadata_plugin.cpp:544-596](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L544-L596) —— `MetadataStoragePlugin::Create` 按前缀 `etcd` / `redis` / `http`(`https`) 选择插件；若都不匹配则 `LOG(FATAL)`。注意每个分支都被 `#ifdef USE_*` 包裹，未编译进来的后端在这里直接「不存在」。

Store HA 层的「按类型分发」结构对称：

[leader_coordinator_factory.cpp:12-51](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/leadership/leader_coordinator_factory.cpp#L12-L51) —— `CreateLeaderCoordinator` 对 `ETCD` / `REDIS` / `K8S` 分别构造对应协调器；`K8S` 分支在未开启 `STORE_USE_K8S_LEASE` 时返回 `UNAVAILABLE_IN_CURRENT_MODE`。

#### 4.1.4 代码实践

1. **目标**：建立「连接串前缀 = 后端类型」的直觉。
2. **步骤**：打开 [transfer_metadata_plugin.cpp:525-542](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L525-L542)（`parseConnectionString`），对照 `Create` 里的 `if` 分支，把下表填全：

   | 连接串 | proto | 命中的插件 | 需要的开关 |
   | --- | --- | --- | --- |
   | `etcd://host:2379` | `etcd` | EtcdStoragePlugin | `USE_ETCD` |
   | `redis://host:6379` | `redis` | RedisStoragePlugin | `USE_REDIS` |
   | `http://host:8080/metadata` | `http` | HTTPStoragePlugin | `USE_HTTP` |

3. **观察**：若把 `redis://...` 传给一个没编译 `USE_REDIS` 的二进制，会走到第 592 行 `LOG(FATAL)`。
4. **预期结果**：进程直接 abort，日志提示找不到对应 storage plugin——这是「后端必须编译期选定」的直接体现。

#### 4.1.5 小练习与答案

- **练习**：为什么 Mooncake 不在运行时动态加载 etcd/redis 客户端，而要用编译期开关？
  - **答**：etcd 走 Go c-shared 库、redis 走 hiredis C 库、K8s 走 client-go，依赖链和体积差异巨大；且 Go runtime 一个进程只能加载一份（见 4.6）。编译期开关既能裁剪依赖，也能在编译期暴露互斥错误。
- **练习**：TE 层和 Store HA 层能否用不同后端？
  - **答**：能。两者互相独立，例如 TE 用 P2P 握手、Store HA 用 etcd 选主是合法组合。

---

### 4.2 P2P 握手与内嵌 HTTP 元数据服务器

#### 4.2.1 概念说明

当**不想部署任何外部协调服务**时，TE 提供两条「零外部依赖」的路径：

- **P2P 握手（SocketHandShakePlugin）**：根本没有中心元数据服务器。节点之间直接用 TCP socket 互发 JSON，临时交换握手信息（连接、元数据、通知、探测）。它是 `USE_ETCD`/`USE_REDIS`/`USE_HTTP` 全关时的唯一可用方式。
- **内嵌 HTTP 元数据服务器（HttpMetadataServer）**：master 进程**自己**起一个极简的 HTTP KV（基于 `coro_http`），TE 客户端用 curl 风格的 `GET/PUT/DELETE /metadata?key=` 来读写。相当于把协调服务「嵌进」master，省去单独运维 etcd。

#### 4.2.2 核心流程

P2P 握手（服务端侧）：

```text
监听 TCP 端口 -> accept 连接 -> 读一条 JSON 请求（带类型: Connection/Metadata/Notify/Probe）
             -> 回调 on_*_callback_ 处理 -> 回写本地 JSON -> 关闭连接
```

内嵌 HTTP 元数据服务器：

```text
master 启动 -> 若 enable_http_metadata_server -> new HttpMetadataServer(port, host) -> start()
GET    /metadata?key=K   -> store_[K] 命中则返回 JSON，否则 404
PUT    /metadata?key=K   -> store_[K] = body（对 rpc_meta key 做去重保护）
DELETE /metadata?key=K   -> erase(K)
GET    /health           -> "OK"（健康检查）
```

#### 4.2.3 源码精读

P2P 握手是无中心方案的核心。它用一个监听线程接收对端 JSON，并按消息类型分发回调：

[transfer_metadata_plugin.cpp:618-832](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L618-L832) —— `SocketHandShakePlugin::startDaemon` 建监听 socket，`accept` 后按 `HandShakeRequestType`（`Connection` / `Metadata` / `Notify` / `Probe`）触发对应回调，再回写本地信息。这就是「P2P」：没有第三方，双方直接交换。

当三者全关时，构建脚本会明确告知你只剩下这一条路：

[common.cmake:415-417](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-common/common.cmake#L415-L417) —— `NOT USE_ETCD AND NOT USE_REDIS AND NOT USE_HTTP` 时打印「only P2PHANDSHAKE is supported」。

内嵌 HTTP 服务器本身非常薄，就是一个加了互斥锁的 `unordered_map`：

[http_metadata_server.cpp:22-108](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/http_metadata_server.cpp#L22-L108) —— `init_server` 注册 `/metadata` 的 GET/PUT/DELETE 与 `/health`。注意 PUT 对键名含 `rpc_meta` 的写入做了「值相同则跳过、重复则拒绝」的保护，防止握手元数据被重复覆盖。

它的对外接口很简单，用一个轮询枚举表达就绪状态：

[http_metadata_server.h:13-51](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/http_metadata_server.h#L13-L51) —— `KVPoll` 枚举（`Failed/Bootstrapping/WaitingForInput/Transferring/Success`）与 `HttpMetadataServer` 类。`poll()` 在运行时返回 `Success`，否则 `Failed`（见 [http_metadata_server.cpp:131-136](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/http_metadata_server.cpp#L131-L136)）。

master 在启动时按配置决定要不要拉起它：

[master.cpp:1093-1107](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master.cpp#L1093-L1107) —— `enable_http_metadata_server` 为真时调用 `StartHttpMetadataServer`，失败直接 `FATAL` 退出，并 `sleep(1s)` 等服务就绪。对应的 gflag 与默认值见 [master.cpp:185-190](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master.cpp#L185-L190)（默认 `enable_http_metadata_server=false`、端口 `8080`、监听 `0.0.0.0`）。

#### 4.2.4 代码实践

1. **目标**：理解 HTTP 元数据服务器「就是 master 进程里的一个内存 map」。
2. **步骤**：
   - 用 `-DUSE_HTTP=ON` 编译（默认就是 ON，见 [common.cmake:121](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-common/common.cmake#L121)）。
   - 启动 master 时传 `--enable_http_metadata_server=true --http_metadata_server_port=8080`。
   - 用 `curl` 手动验证三个端点（这是真正可运行的最小示例）：
     ```bash
     curl -s "http://127.0.0.1:8080/health"                       # 期望: OK
     curl -s -X PUT --data '{"v":1}' "http://127.0.0.1:8080/metadata?key=demo"
     curl -s "http://127.0.0.1:8080/metadata?key=demo"            # 期望: {"v":1}
     curl -s -X DELETE "http://127.0.0.1:8080/metadata?key=demo"
     curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8080/metadata?key=demo"  # 期望: 404
     ```
3. **观察**：master 重启后 `demo` key 消失——因为 `store_` 是纯内存 `unordered_map`，没有持久化。
4. **预期结果**：HTTP 接口可用但**非持久、单点**。这正好解释了它只适合「单 master、能容忍重启丢元数据」的场景。若无法本地运行 master，标注「待本地验证」。

#### 4.2.5 小练习与答案

- **练习**：既然有内嵌 HTTP，为什么还需要 P2P 握手？
  - **答**：HTTP 仍是「中心化」的（所有客户端都连同一个 master 的 8080）；P2P 握手连这个中心都不需要，节点之间直接点对点交换，适合无 master、临时组网、或规避单点的场景。
- **练习**：PUT 对 `rpc_meta` key 的特殊处理是为了防什么？
  - **答**：防止同一 rpc_meta 键被不同值重复写入导致握手信息被覆盖（见 [http_metadata_server.cpp:61-74](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/http_metadata_server.cpp#L61-L74)）。

---

### 4.3 etcd 后端：go 包与 legacy 两种实现

#### 4.3.1 概念说明

etcd 是 Mooncake **生产规模下最常用**的元数据后端，因为它天生强一致（Raft）、支持 watch/lease、可多副本。Mooncake 对 etcd 有**两条实现路径**，由 `USE_ETCD_LEGACY` 切换：

- **go 包（默认，`libetcd_wrapper.so`）**：用 Go 的 `clientv3` 编译成 c-shared 库，C++ 通过 cgo 导出的 C 函数调用。这是 TE 与 Store 共用的主力实现。
- **legacy（`etcd-cpp-api-v3`）**：纯 C++ 的 etcd 客户端库，无需 Go runtime。`USE_ETCD_LEGACY=ON` 时启用。

之所以并存，是因为 Go c-shared 库有「一个进程一份 Go runtime」的限制（见 4.6）；当你需要同时加载多个 Go wrapper 时，legacy 这条「无 Go」的路径就成了逃生口。

#### 4.3.2 核心流程

Store 侧 etcd 调用链：

```text
C++ 业务代码
  -> EtcdHelper::Get/Put/CreateWithLease/...   (etcd_helper.cpp，线程安全静态封装)
  -> NewStoreEtcdClient / EtcdStoreGetWrapper / ...  (cgo 边界，C 符号)
  -> Go: storeClient.Get/Put/Txn               (etcd_wrapper.go，clientv3)
  -> etcd 集群
```

「键不存在才创建」用 etcd 事务的 CAS 实现，判定条件是 `CreateRevision == 0`：

\[
\text{Txn}.\text{If}\big(\text{CreateRevision}(k) = 0\big).\text{Then}\big(\text{Put}(k,v)\big).\text{Commit}()
\]

只有当 `resp.Succeeded == true`（条件成立、确实写入了）才算创建成功；否则说明键已存在（返回 `ETCD_TRANSACTION_FAIL` / `-2`）。

#### 4.3.3 源码精读

`EtcdHelper` 是 Store 侧的统一门面，全部静态方法、带全局互斥锁，保证「全局 store etcd client 只连接一次」：

[etcd_helper.h:16-47](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/etcd_helper.h#L16-L47) —— 类注释明确「All methods thread-safe」「global etcd client … only connected once」。`ConnectToEtcdStoreClient` 保证单例连接，`ResetEtcdStoreClient` 用于断开重连（会取消活跃的 watch/keepalive）。

连接逻辑里有个细节：`NewStoreEtcdClient` 在 Go 侧只允许初始化一次（返回 `-2` 表示已初始化），C++ 侧把 `-2` 当作「已连接」的合法情况：

[etcd_helper.cpp:16-43](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/etcd_helper.cpp#L16-L43) —— `ConnectToEtcdStoreClient` 加锁后若已连接且 endpoints 一致则直接返回 OK，否则调用 Go 的 `NewStoreEtcdClient`，并把 `-2` 视作成功。`ResetEtcdStoreClient` 走 [etcd_helper.cpp:45-63](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/etcd_helper.cpp#L45-L63)。

Go wrapper 关键设计是「**不同用途用不同 client，互不影响**」：

[etcd_wrapper.go:59-81](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-common/etcd/etcd_wrapper.go#L59-L81) —— 注释直说「Use different etcd client so they are not affected by each other」。这里维护了三套：`globalClient`（TE，带引用计数）、`storeClient`（Store）、`snapshotClient`（GB 级快照，`MaxCallSendMsgSize=2GB`）。

Store client 的创建与「只能初始化一次」约束：

[etcd_wrapper.go:243-267](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-common/etcd/etcd_wrapper.go#L243-L267) —— `NewStoreEtcdClient` 若 `storeClient != nil` 直接返回 `-2`。重置逻辑 [etcd_wrapper.go:269-297](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-common/etcd/etcd_wrapper.go#L269-L297) 会先 `cancelAllStoreKeepAlives/Watches/PrefixWatches` 再换 client。

CAS 创建（「键不存在才写」）对应 `EtcdStoreCreateWrapper`：

[etcd_wrapper.go:724-749](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-common/etcd/etcd_wrapper.go#L724-L749) —— `txn.If(CreateRevision(k)==0).Then(OpPut(k,v)).Commit()`，`resp.Succeeded` 为真返回 `0`，否则 `-2`（key already exists）。

TE 侧的 etcd 插件则在 `transfer_metadata_plugin.cpp` 里，按 `USE_ETCD_LEGACY` 二选一：

[transfer_metadata_plugin.cpp:397-523](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L397-L523) —— legacy 版用 `etcd::SyncClient`（[L399-L447](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L399-L447)）；go 包版调 `NewEtcdClient`/`EtcdGetWrapper`/`EtcdPutWrapper`（[L449-L521](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L449-L521)）。

#### 4.3.4 代码实践

1. **目标**：通过单元测试理解 etcd 后端「编译期可用性」如何被断言。
2. **步骤**：阅读 [ha_backend_availability_test.cpp:19-27](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/ha/leadership/ha_backend_availability_test.cpp#L19-L27)。
3. **观察**：同一个 `ValidateHABackendAvailability(ETCD)` 调用，在 `#ifdef STORE_USE_ETCD` 下断言返回 `OK`，否则返回 `UNAVAILABLE_IN_CURRENT_MODE`。
4. **预期结果**：这说明「etcd 能不能用」不是运行时判断，而是**编译那一刻就定了**。对应实现见 [ha_types.h:59-64](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/ha_types.h#L59-L64)。

#### 4.3.5 小练习与答案

- **练习**：为什么 `EtcdHelper` 要把 client 做成「全局只连一次」？
  - **答**：Go 侧 `storeClient` 本身就是单例（`NewStoreEtcdClient` 拒绝二次初始化）；C++ 侧用 `etcd_connected_` + mutex 对齐这一约束，避免多个线程重复建连、泄漏连接，并让 `ResetEtcdStoreClient` 有明确的「断开所有 watch/keepalive 再重连」时机。
- **练习**：go 包版与 legacy 版分别什么情况下选？
  - **答**：默认 go 包版功能更全（lease、watch、prefix watch、JSON range 都在 [etcd_helper.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/etcd_helper.h) 里）。只有在「同进程必须避免多份 Go runtime」时（典型是同时要 K8s Lease，见 4.5/4.6），才用 legacy（纯 C++，无 Go）。

---

### 4.4 Redis 后端

#### 4.4.1 概念说明

Redis 作为元数据后端的价值是「**已经有 Redis、不想再运维 etcd**」。它是内存型 KV，延迟低，但一致性语义弱于 etcd（默认异步复制、无原生 watch 的强保证），适合对一致性强求不高的元数据场景。Mooncake 的 Redis 后端**两层都有**：TE 层的 `RedisStoragePlugin`（连接串 `redis://`）与 Store HA 层的 `RedisLeaderCoordinator`。

#### 4.4.2 核心流程

TE 侧 Redis 插件非常直白——`GET/SET/DEL` 三个命令包一层：

```text
get(key)  -> redisCommand("GET %s", key) -> JSON parse -> Value
set(key)  -> JSON stringify            -> redisCommand("SET %s %s", ...)
remove(k) -> redisCommand("DEL %s", k)
```

Store HA 侧 Redis 则用 key 过期模拟租约来实现选主（细节在 `redis_leader_coordinator.cpp`，本讲聚焦「它是 Redis 后端」这一层）。

#### 4.4.3 源码精读

CMake 层面，Redis 是唯一一个**用纯 C 库 hiredis** 的后端，因此不引入 Go runtime、与其它后端没有互斥问题：

[mooncake-store/CMakeLists.txt:46-50](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/CMakeLists.txt#L46-L50) —— `STORE_USE_REDIS` 时 `find_path/find_library` 找 `hiredis`。TE 层开关见 [common.cmake:120](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-common/common.cmake#L120) 与 [common.cmake:405-408](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-common/common.cmake#L405-L408)。

TE 侧 Redis 插件支持鉴权与多 db：

[transfer_metadata_plugin.cpp:71-133](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L71-L133) —— 构造时可传 `username/password/db_index`，分别用 `AUTH` 与 `SELECT` 命令配置。注意它对 client 加了 `std::mutex`（`access_client_mutex_`），因为 hiredis 的同步上下文不是线程安全的。

连 `Create` 工厂里 Redis 的鉴权参数都来自环境变量，避免连接串里裸奔密码：

[transfer_metadata_plugin.cpp:553-582](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L553-L582) —— 从 `MC_REDIS_USERNAME` / `MC_REDIS_PASSWORD` / `MC_REDIS_DB_INDEX` 读取，并对 db_index 做 `[0,255]` 校验。

可用性校验同样是编译期：

[ha_types.h:65-70](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/ha_types.h#L65-L70) —— `REDIS` 在 `#ifdef STORE_USE_REDIS` 下返回 `OK`，否则 `UNAVAILABLE_IN_CURRENT_MODE`。

#### 4.4.4 代码实践

1. **目标**：体会「Redis 后端 = hiredis 同步 client + 一把锁」。
2. **步骤**：阅读 [transfer_metadata_plugin.cpp:142-201](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L142-L201) 的 `get/set/remove`，注意每个方法第一行都是 `std::lock_guard<std::mutex> lock(access_client_mutex_)`。
3. **观察**：三处都先抢锁再用 `redisCommand`。若去掉这把锁，多线程并发用同一个 `redisContext` 会出错。
4. **预期结果**：理解「为什么 Mooncake 的 Redis 插件是粗粒度串行」——hiredis 同步上下文非线程安全，最简单的安全方式就是全局互斥。代价是吞吐受限，因此 Redis 后端更适合元数据量小、并发不极端的场景。

#### 4.4.5 小练习与答案

- **练习**：相比 etcd，Redis 后端的主要妥协是什么？
  - **答**：一致性/持久化语义较弱（无原生 Raft、watch 依赖 keyspace 通知）、同步 client 加全局锁限制并发。换来的是部署更轻、延迟更低、复用现有 Redis。
- **练习**：为什么 Redis 后端不参与 Go runtime 互斥？
  - **答**：它用 C 库 hiredis，不加载任何 Go `.so`，所以可与 etcd/K8s 后端自由组合。

---

### 4.5 K8s Lease 后端与 K8sLeaseHelper

#### 4.5.1 概念说明

K8s Lease 后端是 **Store HA 专用**的一种选主方式（TE 层没有对应物）。它直接复用 Kubernetes 集群自带的 `coordination.k8s.io/Lease` 对象和 client-go 的 `leaderelection` 机制：多个 master 副本争抢同一个 Lease，拿到的是 leader。好处是「**已经在 K8s 上跑，就不再需要额外部署 etcd**」——K8s 控制面本身就是 etcd。它由 `STORE_USE_K8S_LEASE` 开关启用，通过 Go wrapper `libk8s_lease_wrapper.so` 实现。

#### 4.5.2 核心流程

租约选主的时间参数（K8s leaderelection 三件套）：

\[
T_{\text{lease}} \ge T_{\text{renew}} \ge T_{\text{retry}}
\]

- `lease_dur`（租期）：Lease 对象的持有时长，到期未续约则可被别人抢占。
- `renew_deadline`（续约截止）：leader 在此期限内必须续约成功，否则自认失主。
- `retry_period`（重试间隔）：抢占/续约失败后的重试周期。

```text
K8sLeaseHelper::Init()                      # 初始化 clientset（in-cluster 或 KUBECONFIG）
  -> RunElection(ns, lease, identity, lease_dur, renew_deadline, retry_period)  # 后台参选
  -> WaitElected(ns, lease, timeout)        # 阻塞等到当选
  -> ... leader 工作 ...
  -> WaitLost(ns, lease)                    # 阻塞直到失主
  -> CancelElection(ns, lease)
```

#### 4.5.3 源码精读

`K8sLeaseHelper` 与 `EtcdHelper` 结构对称——静态方法 + 编译期双分支：

[k8s_lease_helper.h:11-42](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/k8s_lease_helper.h#L11-L42) —— 接口含 `Init/RunElection/WaitElected/WaitLost/CancelElection/GetHolder/WatchHolder/CancelWatch`。

开启编译时，C++ 只是把参数透传给 Go wrapper：

[k8s_lease_helper.cpp:16-30](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/k8s_lease_helper.cpp#L16-L30) —— `Init` 加锁后只连一次，调 `K8sLeaseInit`。`RunElection` 等（[L32-L48](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/k8s_lease_helper.cpp#L32-L48)）把 `ns/lease/identity/lease_dur/renew_deadline/retry_period` 原样传给 `K8sLeaseRunElection`。

未开启编译时，所有方法都 `LOG(FATAL)`——这是「编译期不可用」的硬失败：

[k8s_lease_helper.cpp:152-200](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/k8s_lease_helper.cpp#L152-L200) —— `#else` 分支里 `Init()` 直接 `LOG(FATAL) << "K8s Lease is not enabled in compilation"`。

Go wrapper 用 client-go 的 `leaderelection` 与 Lease API：

[k8s_lease_wrapper.go:29-37](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-common/k8s-lease/k8s_lease_wrapper.go#L29-L37) —— 导入 `coordinationv1`（Lease 对象）、`k8s.io/client-go/tools/leaderelection`（选主框架）、`resourcelock`（锁类型）。clientset 的初始化优先用 in-cluster 配置，回退到 `KUBECONFIG`：

[k8s_lease_wrapper.go:80-95](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-common/k8s-lease/k8s_lease_wrapper.go#L80-L95) —— `initClient` 先 `rest.InClusterConfig()`，失败再读 `KUBECONFIG` / `~/.kube/config`。

一个值得注意的细节：`ValidateHABackendAvailability` 对 K8s **恒返回不可用**，与 etcd/redis 的「随编译开关变化」不同：

[ha_types.h:71-72](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/ha_types.h#L71-L72) —— `K8S` 直接返回 `UNAVAILABLE_IN_CURRENT_MODE`（没有 `#ifdef` 守卫）。测试也固化了这一点：

[ha_backend_availability_test.cpp:39-42](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/ha/leadership/ha_backend_availability_test.cpp#L39-L42) —— `K8sLeaseIsRejectedUntilCoordinatorExists` 断言 K8s 在该入口恒被拒。这说明 K8s 选主的实际启用路径走的是 `K8sLeaseHelper`/`K8sLeaderCoordinator`，而非这个通用可用性校验函数。

#### 4.5.4 代码实践

1. **目标**：理解 K8s 后端的「不可用即 FATAL」语义。
2. **步骤**：在一个**没有**开启 `STORE_USE_K8S_LEASE` 的构建里，跟踪 `K8sLeaseHelper::Init` 的调用。
3. **观察**：它会进入 [k8s_lease_helper.cpp:154-157](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/k8s_lease_helper.cpp#L154-L157) 的 `#else` 分支，`LOG(FATAL)` 导致进程 abort。这与 etcd/redis「返回错误码」的温和处理不同——K8s 后端要么编译进来可用，要么一调用就死。
4. **预期结果**：理解 K8s 后端是「重编译才能启用」的可选件，运行时没有优雅降级。若本地无 K8s 集群，标注「待本地验证」。

#### 4.5.5 小练习与答案

- **练习**：K8s Lease 后端相比 etcd 选主，最大优势是什么？
  - **答**：部署在 K8s 上时**零额外组件**——直接用 K8s 控制面（其底层本就是 etcd）的 Lease 对象，省去独立运维一套 etcd/Redis。
- **练习**：`lease_dur / renew_deadline / retry_period` 三个参数若设置不当会怎样？
  - **答**：`renew_deadline` 过大 → leader 挂了很久才切换（长不可用窗口）；过小 → 网络抖动就误判失主（频繁切换、脑裂风险）。三者须满足 `lease_dur ≥ renew_deadline ≥ retry_period`，生产需按 RTT 与故障检测 SLA 调参。

---

### 4.6 CMake 开关与互斥约束

#### 4.6.1 概念说明

把前面所有开关集中讲清楚。一共两组、六个开关，外加两个**硬互斥**约束。理解互斥的关键是记住：**任何走 Go c-shared 的后端，一个进程内只能存在一份 Go runtime**。

#### 4.6.2 核心流程

```text
TE 层（common.cmake，默认状态）：
  USE_HTTP=ON(默认)  USE_ETCD=OFF  USE_REDIS=OFF
  -> 若三者全 OFF，仅 P2P 握手可用

Store HA 层（根 CMakeLists.txt，默认全 OFF）：
  STORE_USE_ETCD        -> 生成 libetcd_wrapper.so   (Go)
  STORE_USE_REDIS       -> 链接 hiredis              (C，无 Go)
  STORE_USE_K8S_LEASE   -> 生成 libk8s_lease_wrapper.so (Go)

互斥检查（configure 阶段即 FATAL_ERROR，不会编出坏二进制）：
  (1) STORE_USE_K8S_LEASE + STORE_USE_ETCD          => 冲突（两个 Go wrapper）
  (2) STORE_USE_K8S_LEASE + (USE_ETCD 且非 legacy)   => 冲突（仍是两个 Go wrapper）
```

> Redis（`STORE_USE_REDIS`）因为是 C 库，**不参与**任何 Go 互斥，可与任意后端共存。

#### 4.6.3 源码精读

互斥约束的「权威来源」就是根 `CMakeLists.txt` 这段：

[CMakeLists.txt:49-58](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/CMakeLists.txt#L49-L58) —— `STORE_USE_K8S_LEASE` 开启时做两次检查：与 `STORE_USE_ETCD` 同开报「cannot be enabled together because both build Go c-shared HA backends」；与非 legacy 的 `USE_ETCD` 同开报「both build Go c-shared libraries in the same process」。这是 configure 期就拦截的硬错误。

三个 Store 开关本身的定义：

[CMakeLists.txt:41-58](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/CMakeLists.txt#L41-L58) —— `STORE_USE_ETCD` / `STORE_USE_REDIS` / `STORE_USE_K8S_LEASE` 的 `option` 声明与 `add_compile_definitions`。

TE 层开关与「全关即 P2P」的判定：

[common.cmake:118-121](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-common/common.cmake#L118-L121) —— `USE_ETCD` / `USE_ETCD_LEGACY` / `USE_REDIS` / `USE_HTTP`(默认 ON)。

子目录把开关翻译成「找库 + 链接 + include」：

[mooncake-store/CMakeLists.txt:36-54](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/CMakeLists.txt#L36-L54) —— `STORE_USE_ETCD` 指向 `libetcd_wrapper.so`、`STORE_USE_K8S_LEASE` 指向 `libk8s_lease_wrapper.so`、`STORE_USE_REDIS` 找 hiredis；三者全关时打印「Store HA backends are disabled」。

`USE_ETCD_LEGACY` 的作用（决定走哪条 etcd 实现）：

[CMakeLists.txt:32-40](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/CMakeLists.txt#L32-L40) —— `USE_ETCD` 开启后，`USE_ETCD_LEGACY` 决定是 etcd-cpp-api-v3（legacy）还是 go package。

操作日志工厂也用同样的开关守卫，可作为「开关如何影响代码路径」的第二个例子：

[oplog_store_factory.cpp:17-33](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/oplog/oplog_store_factory.cpp#L17-L33) —— `OpLogStoreType::ETCD` 分支在 `#ifdef STORE_USE_ETCD` 下才创建 `EtcdOpLogStore`，否则打印「ETCD support not compiled in」并返回 `nullptr`。

#### 4.6.4 代码实践

1. **目标**：亲手触发并理解互斥约束，建立「configure 期就报错」的体感。
2. **步骤**（纯源码阅读 + 构造配置，不实际编译也可完成推理）：
   - 阅读互斥源码 [CMakeLists.txt:49-58](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/CMakeLists.txt#L49-L58)。
   - 构造两个「故意违规」的 cmake 命令，预测结果：
     - `cmake -DSTORE_USE_ETCD=ON -DSTORE_USE_K8S_LEASE=ON ..` → 命中第 52 行 FATAL_ERROR。
     - `cmake -DUSE_ETCD=ON -DSTORE_USE_K8S_LEASE=ON ..`（未设 `USE_ETCD_LEGACY`）→ 命中第 55 行 FATAL_ERROR。
   - 再构造一个**合法**组合：`cmake -DUSE_ETCD_LEGACY=ON -DUSE_ETCD=ON -DSTORE_USE_K8S_LEASE=ON ..` → legacy etcd 是纯 C++、无 Go，可与 K8s Lease 共存，configure 通过。
3. **观察**：违规命令在 `cmake ..` 阶段（还未 make）就打印 `FATAL_ERROR` 并退出；合法组合能进入编译。
4. **预期结果**：互斥不是运行期才暴露的 bug，而是构建系统在最早期就拦截的硬约束。这是 Mooncake 把「Go runtime 单实例」这一底层限制显式化的设计。若本地不编译，标注「待本地验证」。

#### 4.6.5 小练习与答案

- **练习**：为什么 `STORE_USE_K8S_LEASE` 和 `STORE_USE_ETCD` 不能同开，但和 `STORE_USE_REDIS` 可以？
  - **答**：前两者各产出一个 Go c-shared wrapper（`libk8s_lease_wrapper.so` + `libetcd_wrapper.so`），同进程加载两份 Go runtime 会冲突；Redis 用 hiredis（C 库），不引入 Go runtime，故可共存。
- **练习**：若生产环境一定要同时用 etcd 元数据 + K8s Lease 选主，怎么配？
  - **答**：用 `USE_ETCD_LEGACY=ON`（纯 C++ 的 etcd-cpp-api-v3）+ `STORE_USE_K8S_LEASE=ON`，避开「两份 Go runtime」。注意 legacy 版的功能集可能少于 go 包版（见 4.3.5）。

---

## 5. 综合实践

**任务**：对照 CMakeLists.txt 的互斥约束，给出三种部署场景下的元数据后端选型，并说明理由。

请按下表逐项填写，每一格都要能引用到本讲的源码依据：

| 场景 | TE 层元数据后端 | Store HA 后端 | 推荐的 CMake 开关组合 | 理由（引用源码/约束） |
| --- | --- | --- | --- | --- |
| **单机开发**（一台机器，调试代码） | | | | |
| **小集群**（几台机器，无 K8s） | | | | |
| **生产大规模**（多机，已有 K8s） | | | | |

**参考答案**（先自己写完再对照）：

- **单机开发**：TE 用 **P2P 握手**（`USE_ETCD/USE_REDIS/USE_HTTP` 全 OFF，见 [common.cmake:415-417](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-common/common.cmake#L415-L417)）；Store 不开 HA（默认 `enable_ha=false`），因此 Store HA 后端**任选/不选均可**。理由：零外部依赖，起一个进程就能跑，P2P 握手无中心、无需部署 etcd。
- **小集群（无 K8s）**：TE 用 **etcd**（`USE_ETCD=ON`，连接串 `etcd://...`）；Store HA 用 **etcd**（`STORE_USE_ETCD=ON`，走 `EtcdHelper`）。理由：etcd 提供强一致 + watch + lease（[etcd_helper.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/etcd_helper.h)），一套 etcd 同时服务 TE 与 Store HA，运维成本最低；Redis 虽轻但一致性弱于 etcd。
- **生产大规模（已有 K8s）**：TE 用 **etcd**（或 P2P，视架构而定）；Store HA 用 **K8s Lease**（`STORE_USE_K8S_LEASE=ON`，走 `K8sLeaseHelper`），并配合 `USE_ETCD_LEGACY=ON` 若同时需要 etcd。理由：K8s Lease 复用控制面、零额外组件（[k8s_lease_wrapper.go:29-37](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-common/k8s-lease/k8s_lease_wrapper.go#L29-L37)）；若 TE 也要 etcd，必须用 legacy 版以绕开 Go runtime 互斥（[CMakeLists.txt:49-58](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/CMakeLists.txt#L49-L58)）。

**进阶**：把你的选型写成一条 `cmake` 命令和一个示例连接串，并解释为什么这条命令不会被 4.6 的 FATAL_ERROR 拦截。

## 6. 本讲小结

- Mooncake 有**两层**元数据：TE 层（peer/segment 发现）受 `USE_ETCD`/`USE_REDIS`/`USE_HTTP` 控制；Store HA 层（选主/oplog/快照）受 `STORE_USE_ETCD`/`STORE_USE_REDIS`/`STORE_USE_K8S_LEASE` 控制，两者可独立选型。
- 四种 TE 元数据后端：**P2P 握手**（无中心、零依赖，三者全关时唯一可用）、**内嵌 HTTP**（master 进程内置内存 KV）、**etcd**（强一致，主力）、**Redis**（轻量，复用现有 Redis）。
- etcd 有**两条实现**：go 包版（默认，`libetcd_wrapper.so`）与 legacy 版（`etcd-cpp-api-v3`，无 Go），由 `USE_ETCD_LEGACY` 切换。
- 三个 helper 类各司其职：`HttpMetadataServer`（内嵌 HTTP KV）、`EtcdHelper`（Store 侧 etcd 门面，封装 lease/watch/CAS）、`K8sLeaseHelper`（Store 侧 K8s Lease 选主门面）。
- **核心互斥**：`STORE_USE_K8S_LEASE` 不能与 `STORE_USE_ETCD` 同开，也不能与非 legacy 的 `USE_ETCD` 同开——根因是一个进程只能加载一份 Go runtime；Redis 不受此限。
- 后端可用性是**编译期**决定、非运行期：未编译进来的后端，要么 `LOG(FATAL)`（K8s），要么返回 `UNAVAILABLE_IN_CURRENT_MODE`（etcd/redis），工厂里对应的 `#ifdef` 分支直接不存在。

## 7. 下一步学习建议

- 深入 etcd 后端的并发原语：阅读 [etcd_helper.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/etcd_helper.h) 的 `KeepAlive`/`WatchWithPrefixFromRevision`/`GetRangeAsJson`，对照 [etcd_wrapper.go](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-common/etcd/etcd_wrapper.go) 里 prefix watch 的 `createdCh`/`done` 同步机制，理解 lease keepalive 与 watch 的生命周期。
- 把选主串起来：结合 [u7-l1 HA 主备](u7-l1-ha-leader-standby.md)，跟踪 `leader_coordinator_factory.cpp` → 各 `LeaderCoordinator` → `K8sLeaseHelper`/`EtcdHelper` 的调用链，理解一个 HA 集群如何用本讲的后端完成「选举—续约—失主—切换」。
- 动手验证互斥：按 4.6.4 构造三条 cmake 命令，实际跑一次 configure，亲眼看到 FATAL_ERROR 与通过两种结果，建立对构建约束的肌肉记忆。
