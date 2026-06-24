# 节点注册、心跳与租约续期

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚一个 meta / storage 节点**如何加入集群**（`RegisterNode`），以及它和后续心跳的关系。
- 理解 3FS 的「**心跳即租约续期**」模型：节点周期上报、primary mgmtd 在每次心跳里刷新租约时间戳、并用 `hbVersion` 对过期/乱序心跳做去重。
- 读懂心跳中携带的 **storage target 本地状态上报**（`LocalTargetInfo` / `LocalTargetState`），知道它如何驱动 mgmtd 视图里的 target 状态。
- 复述超时检测的完整后果：`MgmtdHeartbeatChecker` 在租约过期后会把节点翻成 `HEARTBEAT_FAILED`、把 target 翻成 `OFFLINE`，并标记 `routingInfoChanged` 触发后续的数据恢复。

本讲只讲「**成员的加入与存活判定**」，不讲数据恢复的具体回放过程（那是 u5-l5），也不讲 primary 选举本身（那是 u3-l3）。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 mgmtd 是「发现服务」，节点要先加入才能被路由

回顾 u3-l1：mgmtd 在内存里维护一份 `RoutingInfo`，里面有 `nodeMap` / `chainMap` / `targetMap` 等四类实体。一个 meta 或 storage 进程要被集群「看见」，必须先在这份 `nodeMap` 里登记一行——这就是**注册**。注册成功后，节点才开始**周期心跳**，告诉 mgmtd「我还活着」。

### 2.2 心跳 = 租约续期

把每次心跳理解为「**向 mgmtd 续一张有时限的租约**」：

- 节点每 \(T_{\text{send}}\) 发一次心跳（默认 10s）；
- mgmtd 收到心跳，把该节点的「最后心跳时间戳」刷新为当前时刻；
- mgmtd 每 \(T_{\text{check}}\) 跑一次检查器（默认 10s），凡是时间戳距今超过 `heartbeat_fail_interval`（默认 60s）的节点，判为失联。

这是一种典型的「续约—过期」租约模型：续约很频繁、判定留有充足的余量（约 6 个续约周期），可以容忍个别心跳在网络中丢失而不误判。具体的周期和阈值都是可热更新的配置项，不要把它们当死规则记，要记的是「**用单调时钟时间戳 + 一个容忍窗口来判定存活**」这个思想。

### 2.3 两套时钟、两套锁

读这部分源码时记住两个易混点：

- **两套时钟**：每个节点既记一个 UTC 时间戳（`lastHeartbeatTs`，给人看、可持久化），又记一个单调时钟时间戳（`WithTimestamp::ts()`，基于 `SteadyClock`，给超时检查用）。判定失联**只看单调时钟**，从而对系统墙钟跳变免疫。
- **两套锁**（u3-l1 已建立）：外层 `writerMu_`（`coScopedLock`）串行化一次「读—改—写」的整个过程；内层 `CoroSynchronized<MgmtdData> data_` 读写锁保护内存一致性。本讲的注册与心跳都会同时用到这两层。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/mgmtd/ops/RegisterNodeOperation.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/RegisterNodeOperation.cc) | `RegisterNode` RPC 的服务端处理：把新节点写入 FDB 并落到内存 `RoutingInfo`。 |
| [src/mgmtd/ops/HeartbeatOperation.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/HeartbeatOperation.cc) | `Heartbeat` RPC 的服务端处理：续租、去重、时间戳校验、target 本地状态合并。 |
| [src/mgmtd/background/MgmtdHeartbeater.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeater.cc) | **非主 mgmtd** 自己作为客户端，向 primary mgmtd 发心跳（mgmtd 也要续自己的租约）。 |
| [src/mgmtd/background/MgmtdHeartbeatChecker.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeatChecker.cc) | 周期检查器：把超时节点翻成 `HEARTBEAT_FAILED`、把超时 target 翻成 `OFFLINE`。 |
| 辅助：[src/mgmtd/service/MgmtdConfig.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdConfig.h)、[src/mgmtd/service/NodeInfoWrapper.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/NodeInfoWrapper.h)、[src/fbs/mgmtd/MgmtdTypes.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/MgmtdTypes.h)、[src/fbs/mgmtd/HeartbeatInfo.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/HeartbeatInfo.h) | 相关配置项、节点包装类、状态枚举、心跳载荷定义。 |

发送侧（节点如何发出心跳）还会用到 `src/client/mgmtd/MgmtdClient.cc` 与 `src/storage/service/Components.cc`，在 4.2 里展开。

---

## 4. 核心概念与源码讲解

### 4.1 节点注册：把节点写入集群视图

#### 4.1.1 概念说明

`RegisterNode` 是一个节点**第一次**进入 mgmtd 视图的入口。它由 `admin_cli` 的 `register-node` 命令触发（或由部署脚本触发），携带三个关键字段：

```cpp
// src/fbs/mgmtd/Rpc.h
DEFINE_SERDE_HELPER_STRUCT(RegisterNodeReq) {
  SERDE_STRUCT_FIELD(clusterId, String{});
  SERDE_STRUCT_FIELD(nodeId, flat::NodeId(0));
  SERDE_STRUCT_FIELD(type, flat::NodeType(flat::NodeType::MIN));
  SERDE_STRUCT_FIELD(user, flat::UserInfo{});
};
```

注册**只做一件事**：在 `RoutingInfo::nodeMap` 里为该 `nodeId` 建一行 `NodeInfo`，并把它持久化到 FoundationDB。注意——注册阶段**不会**把节点状态设成 `HEARTBEAT_CONNECTED`，节点真正「上线」要等到它的**第一次心跳**到达（见 4.2 的 `onNewNode`）。也就是说，注册是「立户」，心跳是「开始续命」。

为什么要把注册和心跳分开？因为注册是管理员意图（需要 admin 权限校验），而心跳是高频自动行为（每 10s 一次）。把强校验留在低频的注册路径，能让高频心跳路径保持轻量。

#### 4.1.2 核心流程

```
admin_cli register-node nodeId type
   │
   ▼  RPC: RegisterNodeReq
RegisterNodeOperation::handle(state)
   │
   ├─ validateClusterId      ── 校验 clusterId 与本集群一致
   ├─ validateAdmin(user)    ── 必须 admin 身份
   ├─ doAsPrimary(...)       ── 只有 primary mgmtd 才能处理写操作
   │     ├─ nodeId == 0 ?    ── 拒绝非法 id
   │     ├─ coScopedLock<"RegisterNode">   ── 外层写锁，串行化整个 RMW
   │     ├─ 共享读 data_：nodeMap 里不能已存在该 nodeId（去重）
   │     ├─ updateStoredRoutingInfo        ── 写 FDB：storeNodeInfo(persistentNode)
   │     └─ updateMemoryRoutingInfo        ── 写内存：nodeMap[nodeId] = newNode
   ▼
RegisterNodeRsp  （只含一个 dummy 字段，表示成功）
```

#### 4.1.3 源码精读

整个 `handle` 的骨架在 [src/mgmtd/ops/RegisterNodeOperation.cc:7-45](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/RegisterNodeOperation.cc#L7-L45)。逐段看：

**前置校验**（clusterId + admin 权限 + 必须 primary）：

[src/mgmtd/ops/RegisterNodeOperation.cc:8-9](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/RegisterNodeOperation.cc#L8-L9) 先校验集群 id，再 `co_await validateAdmin` 要求 admin 身份；最后的 [L44](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/RegisterNodeOperation.cc#L44) `doAsPrimary(state, handler)` 把整段 handler 包起来，`doAsPrimary` 会先 `ensureSelfIsPrimary`（见 [src/mgmtd/service/helpers.h:24-29](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.h#L24-L29)）——非 primary 拒绝处理写操作。

**去重检查**：[src/mgmtd/ops/RegisterNodeOperation.cc:18-24](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/RegisterNodeOperation.cc#L18-L24) 先拿外层写锁 `coScopedLock<"RegisterNode">`，再加 `data_` 共享锁，确认 `nodeMap` 里没有这个 `nodeId`，否则报 `kRegisterFail`（防止重复注册）。

**先持久化、再改内存**：

[src/mgmtd/ops/RegisterNodeOperation.cc:30-40](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/RegisterNodeOperation.cc#L30-L40) 先用 `updateStoredRoutingInfo` 把节点写入 FDB（内部会顺带把 `routingInfoVersion` 加一，见 [helpers.h:72-81](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.h#L72-L81)），成功后再用 `updateMemoryRoutingInfo` 改内存，并 `recordStatusChange` 记一条状态变更。这里的顺序很重要：**FDB 是事实来源**，只有落盘成功才让内存视图可见，保证 primary 切换时不丢节点。

> 补充：`NodeStatus` 枚举见 [src/fbs/mgmtd/MgmtdTypes.h:30-38](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/MgmtdTypes.h#L30-L38)，注册时新建节点的默认状态是 `HEARTBEAT_CONNECTING`（`NodeInfo` 字段默认值），真正变成 `HEARTBEAT_CONNECTED` 要等心跳。

#### 4.1.4 代码实践

**实践目标**：用 `admin_cli` 注册一个节点，并验证 mgmtd 视图里多了这一行。

1. 阅读命令实现 [src/client/cli/admin/RegisterNode.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/RegisterNode.cc)，确认它发出的就是 `RegisterNodeReq{clusterId, nodeId, type, user}`。
2. 在测试集群中执行（命令名以本仓库 `tools/commands` 实际注册为准，待本地验证）：

   ```text
   admin_cli --cluster <id> register-node <nodeId> storage
   ```

3. 执行后立刻 `admin_cli list-nodes`，观察新节点出现，状态为 `HEARTBEAT_CONNECTING`（尚未心跳）。
4. 启动该 storage 进程，等几秒再次 `list-nodes`，状态应被心跳翻成 `HEARTBEAT_CONNECTED`。

**预期结果**：注册成功 → 节点可见但未上线；storage 起来心跳后 → 上线。若重复注册同一 `nodeId`，第二次会收到 `kRegisterFail: duplicated`。完整运行结果「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么注册之后节点状态不是直接 `HEARTBEAT_CONNECTED`？
**答案**：注册只证明「管理员允许这个 id 加入集群」，不证明「这个进程现在真的活着」。上线状态由真实进程的心跳驱动，所以留到 `onNewNode`/`onNodeChanged` 里设置 `HEARTBEAT_CONNECTED`。

**练习 2**：如果绕过 `admin_cli` 直接重复调用 `RegisterNode`，会发生什么？
**答案**：[RegisterNodeOperation.cc:21-23](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/RegisterNodeOperation.cc#L21-L23) 在 `data_` 共享锁下检查 `nodeMap.contains(nodeId)`，命中则返回 `kRegisterFail`，保证幂等。

---

### 4.2 心跳续期：用 HeartbeatOperation 刷新租约

#### 4.2.1 概念说明

心跳是本讲最核心、也最高频的操作。它要同时解决四件事：

1. **续租**：刷新节点的时间戳，证明「我还活着」。
2. **配置拉取**：心跳响应里带回最新的 `ConfigInfo`，节点据此热更新配置（承接 u2-l5）。
3. **去重**：用 `hbVersion` 拒绝过期/乱序/重复的心跳，避免旧状态覆盖新状态。
4. **上报本地状态**：storage 节点在心跳里携带每个 target 的本地状态（`UPTODATE/ONLINE/OFFLINE`），mgmtd 据此维护 target 视图。

心跳载荷 `HeartbeatInfo` 是一个带类型的联合体（[src/fbs/mgmtd/HeartbeatInfo.h:32-93](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/HeartbeatInfo.h#L32-L93)）。三类节点各带不同 payload：`MetaHeartbeatInfo`/`MgmtdHeartbeatInfo` 是空壳（`dummy`），只有 `StorageHeartbeatInfo` 真正携带数据——一个 `vector<LocalTargetInfo>`（[HeartbeatInfo.h:18-23](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/HeartbeatInfo.h#L18-L23)）。此外还有两个版本号字段：

- `hbVersion`（默认 1）：节点自己的心跳序号，**每次成功 +1**，用于服务端拒绝旧心跳；
- `configVersion`（默认 0）：节点当前已应用的配置版本，用于服务端判断是否需要下发新配置。

#### 4.2.2 核心流程

**发送侧（storage 节点）**：

```
TargetMap 变化 或 AutoHeartbeat 定时器(10s) 触发
   │
   ▼
Components::updateHeartbeatPayload(targetMap)   ── 把每个 target 打包成 LocalTargetInfo
   │
   ▼
MgmtdClient::heartbeat() → heartbeatImpl()
   ├─ 组装 HeartbeatReq{clusterId, HeartbeatInfo(app, payload, hbVersion, configVersion, configStatus), UtcClock::now()}
   ├─ stub.heartbeat(req)
   ├─ 若返回 kHeartbeatVersionStale → 解析服务端 lastHbVersion，设 hbVersion=v+1 后重试一次
   └─ 若成功 → hbVersion+1；若带 config → 应用新配置、推进 configVersion
```

**接收侧（primary mgmtd 的 `HeartbeatOperation`）**：

```
HeartbeatOperation::handle
   ├─ validateClusterId
   ├─ doAsPrimary(handler):
   │    ├─ nodeId != selfId, != 0
   │    ├─ 时间戳偏斜校验（validWindow，默认 30s）
   │    ├─ coScopedLock<"Heartbeat">            ── 外层写锁
   │    ├─ 共享读 data_ → prepareHandleHeartbeat:
   │    │     ├─ checkConfigVersion（拒绝 client 版本号超前）
   │    │     ├─ 查 oldNode
   │    │     ├─ prepareNewNodeInfo:
   │    │     │     ├─ 未注册 → allow_heartbeat_from_unregistered? 自动注册 : 失败
   │    │     │     ├─ type 不匹配 / DISABLED → 失败
   │    │     │     ├─ lastHbVersion >= hbVersion → kHeartbeatVersionStale（拒绝旧心跳）
   │    │     │     └─ onNodeChanged：状态置 HEARTBEAT_CONNECTED
   │    │     └─ 若 STORAGE：校验 targets（无重复、localState 合法）
   │    ├─ needPersist / statusChanged 判定
   │    ├─ 若 needPersist → 写 FDB（storeNodeInfo）
   │    └─ 排他写 data_：
   │          ├─ nodeMap[nodeId] = newNode；lastHeartbeatTs=now；updateTs(steadyNow)；updateHeartbeatVersion(hbVersion)
   │          ├─ 若 STORAGE → routingInfo.localUpdateTargets(nodeId, targets)
   │          └─ 组装 rsp.config（最新配置）返回
   ▼
HeartbeatRsp{config?}
```

#### 4.2.3 源码精读

##### (a) 时间戳偏斜校验——防时钟漂移

[HeartbeatOperation.cc:133-141](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/HeartbeatOperation.cc#L133-L141)：

```cpp
auto now = state.utcNow();
const auto validWindow = state.config_.heartbeat_timestamp_valid_window().asUs();
if (validWindow.count() != 0 && (timestamp + validWindow < now || now + validWindow < timestamp)) {
  // 拒绝：心跳时间戳与 mgmtd 时钟偏差超过 30s
}
```

请求里的 `timestamp` 由发送方填 `UtcClock::now()`。这里同时挡住两种异常：心跳**太旧**（`timestamp + window < now`，可能来自卡住的旧请求）和**太新**（`now + window < timestamp`，可能时钟跑飞）。注意它挡的是 UTC 墙钟，只用于这一次请求的有效性判定，**不**参与超时检测（超时用单调时钟，见 4.3）。

##### (b) 旧心跳去重——hbVersion

这是整个心跳设计里最巧妙的一点。服务端为每个节点记一个 `lastHbVersion()`（存在 [NodeInfoWrapper.h:14-15](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/NodeInfoWrapper.h#L14-L15) 的 `version_`）。[HeartbeatOperation.cc:57-60](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/HeartbeatOperation.cc#L57-L60)：

```cpp
if (oldNode->lastHbVersion() >= hb.hbVersion) {
  return makeError(MgmtdCode::kHeartbeatVersionStale, fmt::format("{}", oldNode->lastHbVersion().toUnderType()));
}
```

由于网络重传/重试，后发出的旧心跳可能晚到。服务端只接受**严格递增**的 `hbVersion`，旧心跳被拒绝，并把服务端当前的 `lastHbVersion` 写进错误消息返回。发送方据此对齐自己的序号（见下文 (c)）。处理成功后，[L181](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/HeartbeatOperation.cc#L181) `info.updateHeartbeatVersion(hb.hbVersion)` 把服务端的记录推进到这次的版本。

##### (c) 发送侧如何配合——storage 的 `heartbeatImpl`

storage 侧的发送逻辑在 [src/client/mgmtd/MgmtdClient.cc:688-743](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L688-L743)。它和 (b) 的去重机制正好咬合：[L716-727](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L716-L727) 收到 `kHeartbeatVersionStale` 时，从错误消息里解析服务端的 `lastHbVersion v`，把自己的 `hbVersion` 设为 `v+1` 后**立即重试一次**（`retryable=false` 防止无限重试）。成功路径 [L730](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L730) 则 `hbVersion+1`。两者合起来保证：哪怕发生乱序，下一次心跳的 `hbVersion` 一定严格大于服务端记录。

发送的触发来源有两个：① 周期任务 `autoHeartbeat`（[MgmtdClient.cc:293-298](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L293-L298)，周期由 `auto_heartbeat_interval` 默认 10s，[MgmtdClient.h:21-22](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.h#L21-L22)）；② `TargetMap` 一旦变化就主动触发一次（让 mgmtd 尽快感知 target 状态变化）。

##### (d) storage target 本地状态上报

storage 侧把本地 target 打包进心跳的逻辑在 [src/storage/service/Components.cc:242-261](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc#L242-L261)：

```cpp
flat::LocalTargetInfo targetInfo;
targetInfo.targetId   = targetId;
targetInfo.localState = offline ? flat::LocalTargetState::OFFLINE : target.localState;
targetInfo.diskIndex  = target.diskIndex;
targetInfo.lowSpace   = target.lowSpace;
// ...
targetInfo.usedSize    = target.storageTarget->usedSize();
targetInfo.chainVersion= target.vChainId.chainVer;
```

每个 target 上报 `localState`（取值见 [MgmtdTypes.h:21-28](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/MgmtdTypes.h#L21-L28)：`INVALID/UPTODATE/ONLINE/OFFLINE`）、`diskIndex`、`usedSize`、`chainVersion` 等。注意停止时会以 `offline=true` 把所有 target 标成 `OFFLINE` 再发一次（[Components.cc:156-157](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc#L156-L157)），让 mgmtd「主动感知」下线，而不必苦等超时。

服务端在 [HeartbeatOperation.cc:183-185](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/HeartbeatOperation.cc#L183-L185) 调用 `routingInfo.localUpdateTargets(nodeId, targets, config)` 合并这些上报。合并实现 [src/mgmtd/service/RoutingInfo.cc:38-87](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/RoutingInfo.cc#L38-L87) 有两个要点：

- **跳过陈旧上报**：[RoutingInfo.cc:55-61](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/RoutingInfo.cc#L55-L61) 当 `heartbeat_ignore_stale_targets` 打开且 target 上报的 `chainVersion` 落后于 mgmtd 已知的 chain 版本时，忽略这次上报——防止慢节点用旧视图覆盖 mgmtd 的新视图。
- **孤儿 target**：[RoutingInfo.cc:75-86](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/RoutingInfo.cc#L75-L86) 如果上报的 `targetId` 在 mgmtd 的 `targets` 里查不到（比如 chain 还没建好），先记进 `orphanTargets`，等链真正建立时（`insertNewChain`）再认领。

##### (e) 非 primary mgmtd 也心跳——MgmtdHeartbeater

不要忘了 mgmtd 自己也是集群成员，非 primary 的 mgmtd 实例同样需要向 primary 续租，否则它的路由视图会停滞。这就是 [src/mgmtd/background/MgmtdHeartbeater.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeater.cc) 的职责。它的 `send()` 有一组清晰的短路条件 [L58-92](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeater.cc#L58-L92)：

```cpp
if (!state_.config_.send_heartbeat()) { ... co_return; }            // 配置关闭
auto lease = co_await state_.currentLease(start);
if (!lease.has_value()) { ... co_return; }                          // 还没选出 primary
else if (lease->primary.nodeId == state_.selfId()) { ... co_return; } // 自己就是 primary，不必给自己心跳
```

只有「配置开启 + 已知 primary + 自己不是 primary」时，才建一个到 primary 的 `MgmtdStub` 发心跳（payload 是空的 `MgmtdHeartbeatInfo`）。响应里如果带回新配置，就在 [L37-45](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeater.cc#L37-L45) 调 `updateSelfConfig` 应用并推进 `configVersion`；同样在 [L26-34](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeater.cc#L26-L34) 处理 `kHeartbeatVersionStale` 对齐 `hbVersion`。这条「mgmtd→primary mgmtd」的心跳路径，是 u3-l3 主选举与故障切换能够平滑运转的前提。

> 一个关键区分：**服务节点**（meta/storage，以及非主 mgmtd）靠心跳续租；而 **FUSE client** 默认 `enable_auto_heartbeat=false`（见 [MgmtdClientForClient.h:12](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClientForClient.h#L12)），它不续租，而是靠 `AutoRefresh` 周期拉取路由信息（u3-l6）。client 用另一套「client session」机制管理存活（u3 涉及 `ExtendClientSession`），不在本讲范围。

#### 4.2.4 代码实践

**实践目标**：人为制造一次「心跳版本乱序」，观察去重机制如何工作。

1. 阅读测试 [src/client/mgmtd/MgmtdClient.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L688-L743) 的 `heartbeatImpl`，找到收到 `kHeartbeatVersionStale` 后重试的那段（`retryable` 参数控制只重试一次）。
2. 把 `auto_heartbeat_interval` 调小到 `1s`（热更新即可），让心跳变密。
3. 在 `HeartbeatOperation.cc:57-60` 的 stale 判定处加一行 `XLOGF(INFO, ...)` 日志（仅用于观察，**不要提交**），打印 `nodeId / lastHbVersion / hbVersion`。
4. 启动集群后，让某个 storage 的网络在 mgmtd 侧短暂抖动（例如用 `tc` 加丢包），制造心跳超时重发。

**需要观察的现象**：日志里偶现 `HeartbeatVersionStale`，且发送侧紧接着重试一次就成功（`hbVersion` 跳到 `v+1`），节点状态保持 `HEARTBEAT_CONNECTED` 不回退。

**预期结果**：旧心跳被拒、新心跳被收，节点视图不发生错误回退。若本地无法制造抖动，则改为「源码阅读型实践」：跟踪 [NodeInfoWrapper.h:27](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/NodeInfoWrapper.h#L27) 的 `version_` 在 `updateHeartbeatVersion` 与 `lastHbVersion` 之间的读写，画出一次乱序的时间线即可。运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`heartbeat_timestamp_valid_window`（墙钟 30s）和 `heartbeat_fail_interval`（60s）都用时钟判定，为什么不合并成一个？
**答案**：前者校验的是**单次请求**的时间戳有效性（防墙钟漂移的旧/未来请求），用 UTC；后者判定的是**节点存活**（租约是否过期），用单调时钟 `ts()` + 容忍窗口。两者时钟不同、语义不同，不能合并——合并会引入墙钟跳变导致的误判。

**练习 2**：为什么 storage 停止时要先把所有 target 标成 `OFFLINE` 再发一次心跳？
**答案**：[Components.cc:156-157](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc#L156-L157) 这是一次「优雅下线」的主动通知，让 mgmtd 立刻把 target 视为 `OFFLINE`，而不必等 60s 超时才被动发现，从而更快触发后续处理。

---

### 4.3 超时检测：MgmtdHeartbeatChecker 判定失联

#### 4.3.1 概念说明

有了续租，就需要有「**判定租约过期**」的机制，否则一个崩溃的节点会永远留在 `HEARTBEAT_CONNECTED` 状态。`MgmtdHeartbeatChecker` 就是这个裁判。它由 [MgmtdBackgroundRunner.cc:51-54](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdBackgroundRunner.cc#L51-L54) 每 `check_status_interval`（默认 10s）调度一次，但**只有 primary mgmtd 真正执行判定**——非 primary 直接 `kNotPrimary` 跳过（[MgmtdHeartbeatChecker.cc:85-96](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeatChecker.cc#L85-L96)）。

判定规则是一条简单的不等式。设节点最近一次心跳刷新的单调时钟时间戳为 \(t_{\text{hb}}\)，当前时刻为 \(t_{\text{now}}\)，容忍窗口为 \(T_{\text{fail}}\)（`heartbeat_fail_interval`，默认 60s），则：

\[
t_{\text{hb}} + T_{\text{fail}} < t_{\text{now}} \quad\Longrightarrow\quad \text{判定失联}
\]

即「已经超过 \(T_{\text{fail}}\) 没收到心跳」。这条规则同时作用于节点和 target 两个维度：节点失联→`HEARTBEAT_FAILED`，target 失联→`localState = OFFLINE`。

#### 4.3.2 核心流程

```
每 check_status_interval(10s) 触发 MgmtdHeartbeatChecker::check()
   │
   ▼
doAsPrimary(handler)                       ── 非 primary：记 kNotPrimary 跳过
   │
   ▼
coScopedLock<"CheckHeartbeat">             ── 外层写锁
   │
   ├─ 共享读 data_，扫描收集候选：
   │     ├─ 节点：status 不在 {DISABLED, HEARTBEAT_FAILED, PRIMARY_MGMTD} 且 ts+Tfail<now
   │     │        → 加入 timeoutedNodes（状态翻 HEARTBEAT_FAILED）
   │     └─ target：localState != OFFLINE 且 ts+Tfail<now
   │              → 加入 candidateTargetIds（翻 OFFLINE）
   │
   ├─ 若两者都空 → 直接返回（无变化）
   │
   └─ 排他写 data_：
         ├─ 候选 target → localState = OFFLINE，updateTs(now)
         ├─ 超时节点 → nodeMap[x] 状态 = HEARTBEAT_FAILED
         └─ routingInfoChanged = true        ── 标记视图变化，触发后续 routing 版本推进/恢复
```

注意一个细节：检查器**先在共享锁下收集候选、再到排他锁下应用**，把持锁时间分摊，避免长时间占住写锁阻塞心跳处理。

#### 4.3.3 源码精读

核心在 [src/mgmtd/background/MgmtdHeartbeatChecker.cc:19-78](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeatChecker.cc#L19-L78) 的 `Op::handle`。

**节点扫描** [L34-46](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeatChecker.cc#L34-L46)：

```cpp
for (const auto &[nodeId, nodeInfo] : ri.nodeMap) {
  switch (nodeInfo.base().status) {
    case flat::NodeStatus::DISABLED:
    case flat::NodeStatus::HEARTBEAT_FAILED:
    case flat::NodeStatus::PRIMARY_MGMTD:
      break;  // 这些状态不再判定
    default:
      if (nodeInfo.ts() + heartbeatFailInterval < steadyNow) {
        timeoutedNodeIds.push_back(nodeId);
        timeoutedNodes.push_back(onNodeFailed(nodeInfo.base(), start));  // → HEARTBEAT_FAILED
      }
  }
}
```

`onNodeFailed`（[L10-14](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeatChecker.cc#L10-L14)）只做一件事：把状态置为 `HEARTBEAT_FAILED`。`ts()` 就是 4.2 里每次心跳更新的单调时钟时间戳。三个被 `break` 跳过的状态值得记住：`DISABLED`（管理员手动禁用，尊重人工意图）、`HEARTBEAT_FAILED`（已经判过，不重复）、`PRIMARY_MGMTD`（自己，不该被自己判超时）。

**target 扫描** [L48-52](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeatChecker.cc#L48-L52)：对每个 `localState != OFFLINE` 的 target，用同一套 `ts() + heartbeatFailInterval < steadyNow` 判定，命中则进 `candidateTargetIds`。

**应用** [L62-75](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeatChecker.cc#L62-L75)：排他锁下把候选 target 置 `OFFLINE`、把超时节点写回 `nodeMap`，最后 `ri.routingInfoChanged = true`。这一行是连接本讲与后续讲义的「扳道岔」：视图变化会驱动 `MgmtdRoutingInfoVersionUpdater`（u3-l6）推进 `routingInfoVersion`，进而触发 chain 的 target 状态迁移与数据恢复（u3-l5、u5-l5）。

#### 4.3.4 代码实践

**实践目标**：亲手触发一次超时判定，观察节点与 target 状态翻转。

1. 在测试集群里 `list-nodes` 确认某 storage 节点为 `HEARTBEAT_CONNECTED`，`list-chains` 确认其 target 为正常状态。
2. **强制杀死**该 storage 进程（`kill -9`，模拟崩溃，而非优雅停止——优雅停止会主动上报 `OFFLINE`，看不到超时路径）。
3. 等待约 60–70s（`heartbeat_fail_interval` 默认 60s，加一个检查周期）。
4. 期间观察 mgmtd 日志，应出现 [L56-59](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeatChecker.cc#L56-L59) 的 `found timeouted nodes:[...] targets:[...]`。
5. 再次 `list-nodes` / `list-chains`。

**需要观察的现象**：节点状态变为 `HEARTBEAT_FAILED`，其上的 target `localState` 变为 `OFFLINE`，且 `routingInfoVersion` 随之上升。

**预期结果**：崩溃 → 60s 内被判定 → 节点 `HEARTBEAT_FAILED`、target `OFFLINE` → 触发恢复流程。若想加速，可先用 `set-config` 把 `heartbeat_fail_interval` 调小到 `20s` 做实验，**实验完务必改回**。运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：检查器为什么跳过 `PRIMARY_MGMTD` 状态的节点？
**答案**：`PRIMARY_MGMTD` 是 primary mgmtd 自己。它不会给自己发心跳（4.2 (e) 里 primary 直接短路），所以它没有常规心跳时间戳；若参与超时判定会误判自己失联，故显式排除。

**练习 2**：把 `heartbeat_fail_interval` 设成 `5s`、`check_status_interval` 保持 `10s`，会出现什么问题？
**答案**：检查周期（10s）大于容忍窗口（5s）会出现「漏判/抖动」——一个正常心跳（每 10s）的节点可能在两次检查之间就超过 5s，被误判失联后又因下一次心跳恢复，状态反复抖动。正确做法是保证 \(T_{\text{fail}} \gg T_{\text{send}}, T_{\text{check}}\)，留足余量。

**练习 3**：超时判定用的是 `ts()`（单调时钟）而非 `lastHeartbeatTs`（UTC），有什么好处？
**答案**：单调时钟不会因 NTP 校时或人工改时间而回退/跳跃，判定稳定；若用 UTC，一次墙钟回退会让「已经过期」的节点被误判为「刚刚续租」，导致失联节点迟迟不被发现。

---

## 5. 综合实践

**任务**：追踪一次 storage 节点心跳「从发送到被 `MgmtdHeartbeatChecker` 处理」的完整生命周期，并写出超时后会发生的全部后果。

请按下面的链路逐站打卡，每站给出对应的源码位置与关键字段：

1. **发送触发**：storage 的 `TargetMap` 变化或 `AutoHeartbeat` 定时器到点 → [Components.cc:242-261](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc#L242-L261) 打包 `LocalTargetInfo`。
2. **组装请求**：[MgmtdClient.cc:688-743](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L688-L743) 组装 `HeartbeatReq`（含 `hbVersion`、`configVersion`、`UtcClock::now()`）。
3. **服务端接收**：[HeartbeatOperation.cc:118-201](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/HeartbeatOperation.cc#L118-L201) 校验时间戳、去重 `hbVersion`、刷新 `ts()` 与 `lastHbVersion`、合并 target 上报、回带最新配置。
4. **周期检查**：[MgmtdBackgroundRunner.cc:51-54](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdBackgroundRunner.cc#L51-L54) 每 10s 调度 → [MgmtdHeartbeatChecker.cc:19-78](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeatChecker.cc#L19-L78) 扫描时间戳。

**写出超时后会发生什么**（用一张状态迁移表）：

| 对象 | 触发前状态 | 超时后状态 | 由谁翻转 | 后续影响 |
| --- | --- | --- | --- | --- |
| 节点 | `HEARTBEAT_CONNECTED` | `HEARTBEAT_FAILED` | `MgmtdHeartbeatChecker` | 从路由视图中视为不可用 |
| 该节点上的 target | `UPTODATE/ONLINE` | `OFFLINE`（localState） | `MgmtdHeartbeatChecker` | 进入 `updateChain` 状态机（u3-l5），可能触发 `lastsrv`/数据恢复 |
| `routingInfoVersion` | N | N+1 | `MgmtdRoutingInfoVersionUpdater`（u3-l6） | 通知 client/meta/storage 拉取新视图 |
| 节点重启回来 | `HEARTBEAT_FAILED` | `HEARTBEAT_CONNECTED` | 下一次心跳的 `onNodeChanged` | target 进入 `syncing` 恢复期（u5-l5） |

把这张表填完后，你就把「成员存活判定 → 视图变更 → 数据恢复触发」这条主干打通了。

## 6. 本讲小结

- **注册（`RegisterNode`）**是「立户」：校验 admin 权限、`doAsPrimary`、外层写锁 + 去重，先写 FDB 再改内存；节点真正上线要等第一次心跳。
- **心跳（`HeartbeatOperation`）**是「续命」：刷新单调时钟时间戳 `ts()`、推进 `lastHbVersion`、合并 storage target 本地状态、回带最新配置。
- **去重**靠 `hbVersion` 严格递增：服务端拒旧、客户端按服务端返回值对齐后重试一次，保证乱序不污染视图。
- **两套时钟**分工：UTC 墙钟（`validWindow`）管单次请求有效性，单调时钟（`ts()`）管租约过期，互不干扰。
- **storage target 上报**通过 `localUpdateTargets` 合并，带「跳过陈旧 chainVersion」「孤儿 target 暂存」两道保护。
- **超时检测（`MgmtdHeartbeater` 的对偶 `MgmtdHeartbeatChecker`）**只由 primary 执行，用 \(t_{\text{hb}}+T_{\text{fail}}<t_{\text{now}}\) 把失联节点翻 `HEARTBEAT_FAILED`、把失联 target 翻 `OFFLINE`，并置 `routingInfoChanged` 触发恢复。

## 7. 下一步学习建议

- **u3-l3 主选举与故障切换**：本讲反复出现的 `doAsPrimary` / `ensureSelfIsPrimary` / `currentLease` 来自 mgmtd 的租约选举，下一讲讲清楚 primary 怎么选出来、怎么续约、primary 崩了怎么切。
- **u3-l5 Target 状态机**：本讲把 target 翻成 `OFFLINE` 只是起点，`updateChain` 里的 public 状态（serving/syncing/waiting/lastsrv/offline）转换才是数据恢复的真正驱动。
- **u5-l5 数据恢复**：target 从 `OFFLINE` 回到 `up-to-date` 的 `ResyncWorker` 全量回放过程，承接本讲的「重启回来 → syncing」。
- **延伸阅读**：`src/mgmtd/ops/UnregisterNodeOperation.cc`、`EnableDisableNodeOperation.cc` 是注册/心跳的对偶操作，对照阅读能补全「成员生命周期」的全貌。
