# Target 状态机与故障检测

## 1. 本讲目标

本讲深入 mgmtd（集群管理服务）中**最核心的容错逻辑**：一个存储目标（Target）在故障、恢复过程中，状态是如何被计算和迁移的。学完本讲你应当能够：

1. 说出 Target 的**五种 public 状态**（`SERVING / LASTSRV / SYNCING / WAITING / OFFLINE`）各自的读写语义。
2. 说出 Target 的**三种 local 状态**（`UPTODATE / ONLINE / OFFLINE`），并理解为什么 local 状态是状态机的「触发事件」。
3. 读懂 `generateNewChain` 的状态转换算法，并能**手工推导**一次「serving 目标所在服务崩溃」后，链上各目标状态如何一步步演变直至稳定。

本讲只讲**状态怎么算**（mgmtd 侧）。至于 sync-done 消息是怎么产生的、full-chunk-replace 怎么回放数据，属于 storage 侧的数据恢复（见 u5-l5），本讲只把它们当成「会改变 local 状态的外部事件」。

## 2. 前置知识

本讲假设你已经掌握（来自前置讲义）：

- **CRAQ 链式复制**（u1-l1、u5-l3）：数据写在链头串行化、沿链向尾传播，读可打链上任意 target。
- **Chain / ChainTable / Target 数据模型与版本号**（u3-l4）：一条 Chain 含一串 ChainTargetInfo；`chainVersion` 随成员变更单调递增，storage 在数据面逐请求校验、拒绝过期写。
- **节点心跳与租约**（u3-l2）：storage 通过心跳向 mgmtd 续租并上报本地状态；`heartbeat_fail_interval`（默认 60s）超时即判定租约过期。
- **mgmtd 的双层锁**（u3-l1）：外层 `coScopedLock` 写锁串行化跨 FDB 的读—改—写，内层 `CoroSynchronized<MgmtdData>` 读写锁保护内存一致性。

本讲把视角从「数据结构」推进到「状态如何随故障演化」。一个关键直觉先建立起来：

> mgmtd **不主动探测** SSD 好坏。它只做两件事——**收心跳**（被动得知 storage 还活着、target 本地状态如何）和**周期性重算**（根据最新 local 状态，把每条链的 public 状态重新算一遍并下发）。这就是整个状态机的运转方式。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| `src/fbs/mgmtd/MgmtdTypes.h` | 定义 `PublicTargetState` 与 `LocalTargetState` 两个枚举 |
| `src/fbs/mgmtd/TargetInfo.h` | 完整 Target 视图，含 `publicState` 与 `localState` 双状态 |
| `src/fbs/mgmtd/LocalTargetInfo.h` | storage 心跳上报的单个 target 本地信息（含 `localState`、`chainVersion`） |
| `src/mgmtd/service/updateChain.cc` | **本讲核心**：状态转换算法 `generateNewChain`，以及 `rotateLastSrv`、`shutdownChain` |
| `src/mgmtd/service/MgmtdData.cc` | `appendChangedChains`：调用 `generateNewChain` 并推进 `chainVersion` |
| `src/mgmtd/background/MgmtdChainsUpdater.cc` | 周期性后台任务：挑选「发生变化」的链，触发重算 |
| `src/mgmtd/service/RoutingInfo.cc` | `localUpdateTargets`（合并心跳里的 local 状态）、`applyChainTargetChanges`（把新 public 状态写回内存） |
| `src/mgmtd/background/MgmtdHeartbeatChecker.cc` | 故障检测：心跳超时把 target 的 `localState` 翻成 `OFFLINE` |
| `tests/mgmtd/TestUpdateChain.cc` | 状态机的「黄金转换表」与完整模拟器，是验证理解的最佳参照 |
| `docs/design_notes.md` | 官方设计文档，给出状态语义表与转换表 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**public 状态**、**local 状态**、**状态转换表**。

### 4.1 public 状态：对外可见的目标状态

#### 4.1.1 概念说明

public 状态是 **mgmtd 计算出来、随 ChainTable 一起下发给 client / storage / meta** 的状态。它直接决定了数据面的行为：client 是否会把读请求打到这个 target、storage 是否会把写请求沿链传播给它。

官方设计文档（[docs/design_notes.md:L185-L191](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L185-L191)）给出五种 public 状态及其读写语义：

| Public State | Read | Write | 含义 |
| :----------- | :--: | :---: | :--- |
| serving      |  Y   |   Y   | 服务存活，正在服务客户端请求 |
| syncing      |  N   |   Y   | 服务存活，数据恢复进行中 |
| waiting      |  N   |   N   | 服务存活，数据恢复尚未开始 |
| lastsrv      |  N   |   N   | 服务已下线，且它是链上最后一个 serving 的目标 |
| offline      |  N   |   N   | 服务下线或存储介质故障 |

对照枚举定义 [src/fbs/mgmtd/MgmtdTypes.h:L10-L19](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/MgmtdTypes.h#L10-L19)：

```cpp
enum class PublicTargetState : uint8_t {
  INVALID = 0,
  SERVING = 1,
  LASTSRV = 2,
  SYNCING = 4,
  WAITING = 8,
  OFFLINE = 16,
  ...
};
```

注意几个非显然的点：

- **`syncing` 可写不可读**：恢复中的 target 不承接读（怕读到半截数据），但写请求会沿链传播到它——这正是「边写边恢复」的基础，前驱把每个 chunk 以 full-chunk-replace 方式同步给它。
- **`lastsrv` 是「悲剧状态」**：当链上**所有** serving 副本都下线时，最后一个 serving 的目标会被标为 lastsrv。它持有最新的数据，链必须等它恢复，否则无法安全地用一个更旧的副本顶替。
- **`waiting` 与 `syncing` 的区别**在于「是否已经开始恢复」。同一时刻一条链上至多一个 `syncing`（恢复串行进行），其余存活但未开始恢复的副本都是 `waiting`。

#### 4.1.2 核心流程

public 状态**不是由某个 storage 节点自己决定的**，而是 mgmtd 看着整条链的 local 状态**统一重算**出来的。重算结果会保证一条链上的 public 状态呈如下有序布局（从链头到链尾）：

```
[ SERVING ... ] [ LASTSRV? ] [ SYNCING? ] [ WAITING ... ] [ OFFLINE ... ]
   ↑可读可写      ↑最后服务     ↑恢复中      ↑待恢复          ↑下线/故障
   （链头）                                                          （链尾）
```

这个布局有几个铁律（来自测试 `validatePs`，后文会讲）：

- 有任何 `SERVING` 就不能有 `LASTSRV`（链还能服务，不需要纪念「最后一个 serving」）。
- 有任何 `SYNCING` 就不能有 `LASTSRV`；`SYNCING` 至多 1 个；`LASTSRV` 至多 1 个。
- 没有 `SERVING` 就不能有 `SYNCING`（没人可同步）。
- 各状态在链内的相对顺序固定为 `SERVING < LASTSRV < SYNCING < WAITING < OFFLINE`。

每次重算如果改变了任何 target 的 public 状态，链的 `chainVersion` 就 +1，整条链重新下发。

#### 4.1.3 源码精读

public 状态存放在 [src/fbs/mgmtd/TargetInfo.h:L11-L17](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/TargetInfo.h#L11-L17)，与 local 状态同处一个结构体：

```cpp
SERDE_STRUCT_FIELD(targetId, TargetId(0));
SERDE_STRUCT_FIELD(publicState, PublicTargetState(PublicTargetState::INVALID));
SERDE_STRUCT_FIELD(localState, LocalTargetState(LocalTargetState::INVALID));
SERDE_STRUCT_FIELD(chainId, ChainId(0));
SERDE_STRUCT_FIELD(nodeId, std::optional<NodeId>{});
...
```

`generateNewChain` 在算完所有新状态后，按固定顺序拼接结果，这就保证了上面的有序布局（[src/mgmtd/service/updateChain.cc:L97-L103](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc#L97-L103)）：

```cpp
std::vector<ChainTargetInfoEx> newTargets;
for (auto s : {PS::SERVING, PS::LASTSRV, PS::SYNCING, PS::WAITING, PS::OFFLINE}) {
  const auto &v = newTargetsByPs[s];
  newTargets.insert(newTargets.end(), v.begin(), v.end());
}
assert(oldTargets.size() == newTargets.size());
```

而那些「铁律」并非靠注释维护，而是靠单元测试 `validatePs` 把它们钉死（[tests/mgmtd/TestUpdateChain.cc:L92-L135](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/tests/mgmtd/TestUpdateChain.cc#L92-L135)）。例如「有 SERVING 就不能有 LASTSRV」「SYNCING 至多 1 个」都在这里被断言。改算法时，这些断言就是回归网。

#### 4.1.4 代码实践：观察一条真实链的 public 状态

1. **实践目标**：直观看到一条链上各 target 的 public 状态分布，验证「链头 serving、链尾 offline」的布局。
2. **操作步骤**：
   - 在测试集群中用 `admin_cli` 执行 `list-chains`（参见 u1-l3），它会强制刷新 mgmtd 路由信息后打印每条链。
   - 关注输出里每条 chain 的 targets 列表，以及每个 target 的 public 状态。
3. **需要观察的现象**：正常情况下绝大多数链应是 `[SERVING, SERVING, SERVING]`（三副本都在线）。
4. **预期结果**：若曾有过节点重启，能看到形如 `[SERVING, SERVING, OFFLINE]` 或 `[SERVING, SERVING, SYNCING]` 的链——offline/syncing 的目标一定排在链尾。
5. 若手头没有运行中的集群，**改为源码阅读型实践**：阅读 `validatePs`（[tests/mgmtd/TestUpdateChain.cc:L92-L135](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/tests/mgmtd/TestUpdateChain.cc#L92-L135)），逐条把 6 条规则翻译成中文，说明每条规则防的是哪种异常布局。运行结果：待本地验证（需可运行的测试集群或编译环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么「有任何 `SERVING` 就不能有 `LASTSRV`」？
> **答案**：`LASTSRV` 的语义是「链已无可服务副本，这是最后一个 serving 过的、持有最新数据的目标，必须等它」。一旦链上还有 `SERVING` 副本，链仍可正常读写，就不存在「等谁」的问题，自然不需要 lastsrv。

**练习 2**：为什么 `SYNCING` 至多只能有 1 个？
> **答案**：恢复是**串行**的——同一时刻只有一个未完成副本可以从前驱拉数据，避免多个恢复副本争抢前驱带宽、也便于管理进度。其余存活但未恢复的副本排队为 `WAITING`，等当前 syncing 恢复完变成 serving 后，下一个 waiting 才会被提升为 syncing。

---

### 4.2 local 状态：本地触发事件

#### 4.2.1 概念说明

如果说 public 状态是「mgmtd 对外公布的结论」，那么 local 状态就是「storage 上报上来的事实」。设计文档有一句点睛之笔：

> The local state plays the role of a triggering event.（[docs/design_notes.md:L201](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L201)）

也就是说，**public 状态是函数的输出，local 状态是触发它的输入事件**。local 状态只有三种（[docs/design_notes.md:L195-L199](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L195-L199)）：

| Local State | 含义 |
| :---------- | :--- |
| up-to-date  | 服务存活，数据已是最新的（可直接 serving） |
| online      | 服务存活，但数据还在恢复中或尚未恢复（处于 syncing/waiting） |
| offline     | 服务下线或存储介质故障 |

枚举定义见 [src/fbs/mgmtd/MgmtdTypes.h:L21-L28](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/MgmtdTypes.h#L21-L28)：

```cpp
enum class LocalTargetState : uint8_t {
  INVALID = 0,
  UPTODATE = 1,
  ONLINE = 2,
  OFFLINE = 4,
  ...
};
```

两个关键区分：

- **local 状态只在 storage 与 mgmtd 之间流转**，不进 ChainTable 下发给 client。它存在 mgmtd 内存里（[docs/design_notes.md:L193](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L193)）。
- **`UPTODATE` 与 `ONLINE` 都是「活着」**，区别只在「数据是否已经追平」。`UPTODATE` 意味着可以放心地转为 `SERVING`；`ONLINE` 还在恢复，只能转为 `SYNCING` 或 `WAITING`。

#### 4.2.2 核心流程

local 状态进入 mgmtd 有两条路径，对应「正常上报」与「被动判定」：

1. **心跳主动上报**：storage 在每次心跳里携带一批 `LocalTargetInfo`，告诉 mgmtd「我这个 target 现在本地是什么状态」。其中：
   - 服务正常运行、数据最新 → `UPTODATE`。
   - 服务刚重启、正在恢复 → 先 `OFFLINE`（bootstrap 阶段），恢复开始后 `ONLINE`。
   - 收到 sync-done、恢复完成 → 下次心跳翻为 `UPTODATE`（[docs/design_notes.md:L209](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L209)、[L244](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L244)）。
   - 介质故障 → `OFFLINE`。
2. **mgmtd 被动判定**：如果某个 target 超过 `heartbeat_fail_interval`（默认 60s）没收到心跳，mgmtd 的 `MgmtdHeartbeatChecker` 直接把它的 `localState` 翻成 `OFFLINE`——这是故障检测的核心。

无论哪条路径，local 状态一旦变化（`ts()` 更新），对应的链就会在下一次 `MgmtdChainsUpdater` 周期里成为「候选链」，触发 `generateNewChain` 重算 public 状态。

#### 4.2.3 源码精读

**心跳上报的合并**——`localUpdateTargets` 把 storage 上报的 local 状态写进内存（[src/mgmtd/service/RoutingInfo.cc:L38-L87](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/RoutingInfo.cc#L38-L87)）。关键片段：

```cpp
bool shouldIgnore = [&] {
  if (lti.chainVersion == 0 || !config.heartbeat_ignore_stale_targets()) return false;
  const auto &chain = getChain(base.chainId);
  return chain.chainVersion > lti.chainVersion;   // 陈旧心跳：丢弃
}();
if (shouldIgnore) return;
...
base.localState = lti.localState;   // 真正写入 local 状态
base.nodeId = nodeId;
```

这里有一道重要保护：如果上报携带的 `chainVersion` 落后于 mgmtd 当前链版本（`shouldIgnore`），则直接忽略——防止一个旧版本的心跳把已经迁移过的 target 状态「拉回去」（u3-l2 讲过去重心跳对齐，这里是服务端侧的版本守卫）。

**故障检测**——`MgmtdHeartbeatChecker` 周期扫描，把超时 target 的 local 状态翻成 `OFFLINE`（[src/mgmtd/background/MgmtdHeartbeatChecker.cc:L48-L52](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeatChecker.cc#L48-L52) 检测，[L66-L71](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeatChecker.cc#L66-L71) 翻转）：

```cpp
for (const auto &[tid, ti] : ri.getTargets()) {
  if (ti.base().localState != flat::LocalTargetState::OFFLINE &&
      ti.ts() + heartbeatFailInterval < steadyNow) {
    candidateTargetIds.push_back(tid);   // 超时且尚未 offline
  }
}
...
for (auto tid : candidateTargetIds) {
  ri.updateTarget(tid, [steadyNow](auto &ti) {
    ti.base().localState = flat::LocalTargetState::OFFLINE;   // 翻成 OFFLINE
    ti.updateTs(steadyNow);
  });
}
```

注意：检查器只改 **local 状态**，不动 public 状态。public 状态留给 `generateNewChain` 去算。这种「输入与输出分离」正是状态机清晰的原因。

> 补充：storage 上报用的载荷 `LocalTargetInfo` 字段见 [src/fbs/mgmtd/LocalTargetInfo.h:L11-L16](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/LocalTargetInfo.h#L11-L16)，其中也带 `chainVersion`，正是上面 `shouldIgnore` 判断的依据。

#### 4.2.4 代码实践：跟踪一次故障检测

1. **实践目标**：理解「服务崩溃 → local 状态 OFFLINE」的完整链路，并确认 mgmtd 没有主动探测。
2. **操作步骤**：
   - 阅读 [MgmtdHeartbeatChecker.cc:L48-L71](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeatChecker.cc#L48-L71)。
   - 回答：判定超时用的是 `ti.ts()`（target 最近一次被更新的时间），这个时间戳在何处被刷新？（提示：心跳合并 `localUpdateTargets` 与检查器翻转都会 `updateTs`）。
3. **需要观察的现象**：当 storage 进程被 `kill -9`，它的心跳停发；约 `heartbeat_fail_interval`（默认 60s）后，该节点上所有 target 的 local 状态会从 `UPTODATE/ONLINE` 翻成 `OFFLINE`。
4. **预期结果**：日志里会出现 `found timeouted nodes:[...] targets:[...]`（[L56-L59](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeatChecker.cc#L56-L59)），随后该链被重算。
5. 运行结果：待本地验证（需可触发节点故障的测试集群）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 local 状态用 storage 心跳上报，而不是 mgmtd 主动去 ping 每个 SSD？
> **答案**：因为只有 storage 自己知道 SSD 是否真的可用（介质级故障、数据是否追平）。mgmtd 主动 ping 只能测网络/进程，测不到介质健康与恢复进度。把判定权下放给 storage，再由 mgmtd 用「心跳超时」兜底进程级故障，职责分明。

**练习 2**：`UPTODATE` 和 `ONLINE` 都是「活着」，状态机为什么要把它们区分开？
> **答案**：因为「活着但数据没追平」的副本**不能**直接对外服务（读了会拿到半截/旧数据）。区分开后，`ONLINE` 只能转为 `SYNCING/WAITING`（继续恢复），`UPTODATE` 才能转为 `SERVING`。这避免了把未恢复的副本当 serving 用。

---

### 4.3 状态转换表：generateNewChain 算法

#### 4.3.1 概念说明

状态转换表回答一个问题：**给定一条链上每个 target 的「旧 public 状态 + 当前 local 状态」，新的 public 状态应该是什么？**

设计文档给出的是一张以 `(local 状态, 当前 public 状态, 前驱 public 状态) → 新 public 状态` 组织的表（[docs/design_notes.md:L211-L230](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L211-L230)）。摘录几条最能体现语义的：

| Local | 当前 Public | 前驱 Public | 新 Public | 直觉 |
| :---- | :---------- | :---------- | :-------- | :--- |
| up-to-date | syncing | (any) | **serving** | 恢复完成，立即上岗 |
| up-to-date | lastsrv | (any) | **serving** | 最后的服务副本恢复，链复活 |
| online | syncing | not serving | waiting | 没人可同步，退回等待 |
| offline | serving | has no predecessor | **lastsrv** | 唯一副本死了，标记为最后服务者 |
| offline | serving | has predecessor | offline | 还有前驱可依赖，直接下线 |

代码并没有逐条查表，而是用一个等价但更紧凑的算法 `generateNewChain` 实现：**按旧 public 状态分桶，依固定顺序处理，用「桶里已经放过什么」来隐式表达「前驱/链内是否已有 serving/syncing」**。

#### 4.3.2 核心流程

`generateNewChain` 的算法骨架（伪代码）：

```
输入：oldTargets（一条链，每个 target 带 旧publicState + localState）
1. 按 旧publicState 分到 5 个桶：SERVING / LASTSRV / SYNCING / WAITING / OFFLINE
2. 依次处理每个桶，决定每个 target 的新 publicState，放入 newTargetsByPs：

   处理 SERVING 桶：
     - 若 localState ∈ {ONLINE, UPTODATE}        → 仍是 SERVING
     - 否则（localState=OFFLINE）：
         - 若还没人当 LASTSRV                     → 第一个死的当 LASTSRV
         - 否则                                   → OFFLINE

   处理 LASTSRV 桶（旧的 lastsrv）：
     - 若链上还没有新的 SERVING：
         - localState ∈ {ONLINE, UPTODATE}        → 升为 SERVING（它恢复了！）
         - 否则                                   → 保持 LASTSRV
     - 否则（已有 SERVING）                        → OFFLINE

   处理 SYNCING 桶：
     - localState = UPTODATE                       → SERVING（恢复完成）
     - localState = ONLINE：
         - 已有 SERVING                            → 保持 SYNCING
         - 没有 SERVING                            → WAITING（没人可同步）
     - localState = OFFLINE                        → OFFLINE

   处理 WAITING 桶：
     - 已有 SERVING 且 没有 SYNCING 且 ONLINE       → 提升为 SYNCING
     - ONLINE 或 UPTODATE                          → 保持 WAITING
     - 否则                                        → OFFLINE

   处理 OFFLINE 桶：（规则同 WAITING）
     - 已有 SERVING 且 没有 SYNCING 且 ONLINE       → 提升为 SYNCING
     - ONLINE 或 UPTODATE                          → WAITING
     - 否则                                        → OFFLINE

3. 收尾：若产生了任何 SERVING，则把所有 LASTSRV 降为 OFFLINE（清空 lastsrv）
4. 按 SERVING, LASTSRV, SYNCING, WAITING, OFFLINE 顺序拼接输出
```

理解这套规则的两个要点：

- **「已有 SERVING / 已有 SYNCING」是用 `newTargetsByPs[...]` 是否为空来判断的**。因为桶按固定顺序处理，先处理的 SERVING 桶会先填满 `newTargetsByPs[SERVING]`，后处理的桶就能据此知道链头是否还有人在服务。这等价于设计表里的「前驱 public 状态」。
- **一次重算可能让一个 target 跨多级跳变**：比如一个 `OFFLINE` 副本重启后 `localState=ONLINE`，会先变 `WAITING`，下一轮再变 `SYNCING`，恢复完变 `SERVING`——通常是分多轮、每轮 `chainVersion+1` 逐步推进，而不是一步到位（保证恢复可控、可观测）。

#### 4.3.3 源码精读

**算法主体**——`generateNewChain`（[src/mgmtd/service/updateChain.cc:L25-L104](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc#L25-L104)）。`dispatch` 是个记录日志并改状态的辅助（[L11-L22](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc#L11-L22)）。各桶对应：SERVING [L31-L42](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc#L31-L42)、LASTSRV [L44-L54](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc#L44-L54)、SYNCING [L56-L68](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc#L56-L68)、WAITING [L70-L78](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc#L70-L78)、OFFLINE [L80-L88](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc#L80-L88)。收尾的「有 SERVING 则清空 LASTSRV」见 [L90-L95](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc#L90-L95)：

```cpp
if (!newTargetsByPs[PS::SERVING].empty()) {
  for (auto &ti : newTargetsByPs[PS::LASTSRV]) {
    dispatch(newTargetsByPs, std::move(ti), PS::OFFLINE, "Has SERVING");
  }
  newTargetsByPs[PS::LASTSRV].clear();
}
```

**调用点与版本推进**——`appendChangedChains`（[src/mgmtd/service/MgmtdData.cc:L116-L144](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.cc#L116-L144)）。它先跳过新生链的宽限期（`newBornChains`，[L119](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.cc#L119)——刚重启的链先稳一稳再算），把旧 target 装配上各自 local 状态（[L123-L126](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.cc#L123-L126)），调用 `generateNewChain`，仅在结果与旧值不同时才推进 `chainVersion`（[L128-L143](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.cc#L128-L143)）：

```cpp
auto newTargets = generateNewChain(oldTargets);
...
if (oldTargets != newTargets) {
  flat::ChainInfo newChain;
  newChain.chainId = chainId;
  newChain.chainVersion = nextVersion(oldChain.chainVersion);   // 版本号 +1
  ...
}
```

**谁触发重算**——`MgmtdChainsUpdater`（[src/mgmtd/background/MgmtdChainsUpdater.cc:L25-L37](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdChainsUpdater.cc#L25-L37)）周期扫描，凡是 `ti.ts() > lastUpdateTs`（target 状态自上次后变过）的，其所属链进入候选集：

```cpp
for (const auto &[tid, ti] : ri.getTargets()) {
  if (ti.ts() > lastUpdateTs) {
    candidateChains.insert(ti.base().chainId);   // 这条链要重算
  }
  ...
}
```

**算完怎么落盘与下发**——重算结果先经 FDB 事务持久化（`updateStoredRoutingInfo`），再写回内存（`updateMemoryRoutingInfo`），后者调用 `applyChainTargetChanges` 把新 public 状态刷到 target 上（[src/mgmtd/service/RoutingInfo.cc:L8-L26](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/RoutingInfo.cc#L8-L26)），并触发 `routingInfoVersion` 推进、对外下发（u3-l1、u3-l6）。

**黄金转换表（验证用）**——测试里把每种 `(旧public, local) → 新public` 的合法迁移都列了出来（[tests/mgmtd/TestUpdateChain.cc:L154-L192](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/tests/mgmtd/TestUpdateChain.cc#L154-L192)）。比如 `SERVING + OFFLINE → {OFFLINE, LASTSRV}`、`SYNCING + UPTODATE → SERVING`、`LASTSRV + ONLINE → SERVING`。看懂这张表，等于看懂了整条状态机。

**两个管理用的辅助函数**（了解即可）：

- `rotateLastSrv`（[updateChain.cc:L143-L163](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc#L143-L163)）：当 lastsrv 短期无法恢复时，管理员可手动把它挪到链尾、让下一个目标顶上当新的 lastsrv，**以承担丢数据风险为代价恢复服务**（注释明确警告 `on risk of losing some data forever`）。
- `shutdownChain`（[updateChain.cc:L165-L186](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc#L165-L186)）：优雅停链，把 `SERVING→LASTSRV`、`SYNCING/WAITING→OFFLINE`，用于下线整条链。

#### 4.3.4 代码实践：手工推导「serving 目标崩溃」到稳定（核心实践）

这是本讲的重头戏。我们跟踪一条 **3 副本链 A→B→C**（A 是链头），三副本初始都是 `SERVING + UPTODATE`。假设 **A 所在的 storage 服务崩溃**，之后又重启恢复。请按状态转换表逐步推导。

**约定**：链按从链头到链尾书写，每个 target 标注 `(publicState, localState)`。

---

**第 0 步（初始）**：

```
链: [ A(SERVING, UPTODATE), B(SERVING, UPTODATE), C(SERVING, UPTODATE) ]
```

---

**第 1 步：A 崩溃，mgmtd 心跳超时判定 A 为 OFFLINE**

`MgmtdHeartbeatChecker` 在约 60s 后把 A 的 `localState` 翻成 `OFFLINE`（[MgmtdHeartbeatChecker.cc:L66-L71](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeatChecker.cc#L66-L71)）。现在输入给 `generateNewChain` 的是：

```
[ A(SERVING, OFFLINE), B(SERVING, UPTODATE), C(SERVING, UPTODATE) ]
```

按算法处理 **SERVING 桶**（[L31-L42](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc#L31-L42)）：

- A：`localState=OFFLINE`，且尚无 LASTSRV → A 临时进 LASTSRV 桶（「first SERVING」）。
- B：`localState=UPTODATE` → 仍是 SERVING。
- C：`localState=UPTODATE` → 仍是 SERVING。

收尾（[L90-L95](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc#L90-L95)）：**已有 SERVING（B、C）**，所以把 LASTSRV 里的 A **降为 OFFLINE**。

```
结果: [ B(SERVING), C(SERVING), A(OFFLINE) ]   chainVersion + 1
```

> **观察**：因为 B、C 还在服务，A 没有资格当 lastsrv，直接变 OFFLINE 并被挪到**链尾**。链继续由 B（新链头）、C 服务，读写不受影响。这正是 CRAQ 多副本的价值——单点崩溃不中断服务。

---

**第 2 步：A 重启，开始恢复，上报 `localState = ONLINE`**

A 重启后从 mgmtd 拉到最新链表，看到自己是 OFFLINE，于是退出 bootstrap、开始恢复，下次心跳上报 `ONLINE`（sync-done 之前都是 ONLINE）。输入：

```
[ B(SERVING, UPTODATE), C(SERVING, UPTODATE), A(OFFLINE, ONLINE) ]
```

处理：

- SERVING 桶：B、C `UPTODATE` → 仍 SERVING。
- OFFLINE 桶（A，[L80-L88](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc#L80-L88)）：**已有 SERVING 且 没有 SYNCING 且 `localState=ONLINE`** → A **提升为 SYNCING**。

```
结果: [ B(SERVING), C(SERVING), A(SYNCING) ]   chainVersion + 1
```

> **观察**：A 占据了链上唯一的 SYNCING 名额，开始从前驱 C 接收 full-chunk-replace 写入做数据恢复（具体恢复机制见 u5-l5）。

---

**第 3 步：A 恢复完成，收到 sync-done，上报 `localState = UPTODATE`**

A 收到 sync-done 后（[docs/design_notes.md:L244](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L244)），下次心跳上报 `UPTODATE`。输入：

```
[ B(SERVING, UPTODATE), C(SERVING, UPTODATE), A(SYNCING, UPTODATE) ]
```

处理：

- SERVING 桶：B、C → 仍 SERVING。
- SYNCING 桶（A，[L56-L68](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc#L56-L68)）：`localState=UPTODATE` → **A 升为 SERVING**。

```
结果: [ B(SERVING), C(SERVING), A(SERVING) ]   chainVersion + 1
```

> **观察**：A 重新成为 SERVING，但**位置仍在链尾**——它不会自动抢回链头。三副本全部恢复，状态稳定。

---

**整个过程的状态时间线**：

| 步骤 | 事件 | 链状态（public） | A 的 local | chainVersion |
| :--: | :--- | :--- | :--- | :---: |
| 0 | 初始 | `[A:S, B:S, C:S]` | UPTODATE | v |
| 1 | A 崩溃超时 | `[B:S, C:S, A:OFFLINE]` | OFFLINE | v+1 |
| 2 | A 恢复中 | `[B:S, C:S, A:SYNCING]` | ONLINE | v+2 |
| 3 | A 恢复完成 | `[B:S, C:S, A:S]` | UPTODATE | v+3 |

（`S` = SERVING）

**可选验证**：若你有编译环境，跑 `TestUpdateChain` 里的 `testThreeReplica_Recover`（[TestUpdateChain.cc:L501-L532](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/tests/mgmtd/TestUpdateChain.cc#L501-L532)），它正是反复施加 local 状态变化、最终断言所有 target 都回到 SERVING。运行结果：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`LASTSRV` 到底在什么条件下才会真正出现？把上面第 1 步改成「A、B、C 同时崩溃」，推导结果。
> **答案**：只有当链上**所有** serving 副本都下线、且至少有一个曾经 serving 的目标时才会出现 lastsrv。若 A、B、C 同时崩溃（localState 全 OFFLINE）：处理 SERVING 桶时，A（第一个）进 LASTSRV，B、C 进 OFFLINE；收尾时 `newTargetsByPs[SERVING]` 为空，故**不清空** LASTSRV。结果为 `[A:LASTSRV, B:OFFLINE, C:OFFLINE]`。此时链无任何 serving 副本，**无法安全写入**，必须等 A（持有最新数据）恢复。这正是 `LASTSRV` 名字「last serving」的由来。

**练习 2**：`rotateLastSrv` 为什么被注释警告「on risk of losing some data forever」？
> **答案**：lastsrv 是链上数据最新的副本。如果它迟迟不恢复，管理员用 `rotateLastSrv` 把它挪到链尾、让一个**数据更旧**的下一个目标顶上当新 lastsrv 来恢复服务，那么当旧 lastsrv 后来恢复时，它独有的那部分较新数据就**永久丢失**了（已被更旧的数据覆盖）。这是用数据完整性换可用性的最后手段。

**练习 3**：第 1 步里 A 为什么先被放进 LASTSRV 桶、随后又被降为 OFFLINE，而不是直接判 OFFLINE？
> **答案**：算法在遍历 SERVING 桶时还**不知道**后面是否还有其他 serving 副本会保留下来，所以先按「第一个死的当 lastsrv」处理；等所有桶处理完、确定 `newTargetsByPs[SERVING]` 是否非空后，再用收尾规则（[L90-L95](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc#L90-L95)）统一修正：只要有任何 serving 留下，就把 lastsrv 降为 offline。这种「先乐观放置、后统一校正」的写法保证了不管副本顺序如何，结论都正确。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**端到端状态推演**任务。

**场景**：一条 3 副本链 A→B→C，初始 `[A:S, B:S, C:S]`（全 UPTODATE）。请按时间顺序处理以下事件序列，**画出每一步后整条链的 public 状态与相关 target 的 local 状态**，并标注 `chainVersion` 是否变化：

1. B 所在服务崩溃并被超时判定（60s 后）。
2. A 所在服务也崩溃并被超时判定。
3. B 重启，上报 ONLINE。
4. A 重启，上报 ONLINE。
5. B 收到 sync-done，上报 UPTODATE。
6. A 收到 sync-done，上报 UPTODATE。

**要求**：

- 每步都要写明这一步输入给 `generateNewChain` 的链状态、用了哪条规则（引用 [updateChain.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc) 的行号或 [design_notes.md:L211-L230](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L211-L230) 的转换表）。
- 特别注意第 1、2 步：当 B 先死、A 再死时，谁会成为 LASTSRV？链是否会进入「无 serving」状态？
- 用 [TestUpdateChain.cc:L154-L192](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/tests/mgmtd/TestUpdateChain.cc#L154-L192) 的黄金转换表，校验你每一步的单目标迁移是否合法。

**参考思路（不是唯一答案，请先自己推）**：第 1 步后链 `[A:S, C:S, B:OFFLINE]`（B 挪尾）；第 2 步后 A 也死，此时链上无 serving → A 成为 LASTSRV，得 `[A:LASTSRV, C:OFFLINE, B:OFFLINE]`，链不可写；第 3 步 B ONLINE → 因有 LASTSRV（A）、无 serving，B 进 WAITING；第 4 步 A ONLINE → A 从 LASTSRV 升为 SERVING（它数据最新，恢复即可直接服务）；之后 C、B 依次恢复为 SYNCING→SERVING。最终全 SERVING。

> 提示：若你推导的结果与「参考思路」不同，不要急着改答案——先回到 [generateNewChain](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/updateChain.cc#L25-L104) 逐桶核对，状态机的美妙之处就在于它是确定性的，对同一输入只有唯一正确输出。

## 6. 本讲小结

- Target 有**双状态**：对外下发的 public 状态（5 种）和仅存 mgmtd 内存的 local 状态（3 种）。public 是结论，local 是触发事件。
- public 状态在链内呈固定有序布局 `SERVING < LASTSRV < SYNCING < WAITING < OFFLINE`，由 `validatePs` 的多条不变量钉死。
- local 状态由 storage 心跳上报、由 `MgmtdHeartbeatChecker` 在心跳超时时被动翻成 OFFLINE；mgmtd **不主动探测** SSD。
- `generateNewChain` 是状态机的核心：按旧 public 状态分桶、顺序处理、用「桶里已放过什么」隐式表达前驱状态，最后用「有 SERVING 则清空 LASTSRV」收尾。
- 每次重算若改变 public 状态，`chainVersion` +1，经 FDB 持久化后写回内存并推进 `routingInfoVersion` 下发。
- `LASTSRV` 是链上所有 serving 副本全死时的「最后服务者」，链必须等它；`rotateLastSrv` 是以丢数据为代价的应急手段。

## 7. 下一步学习建议

本讲解完了「状态怎么算」。状态机的输出（SYNCING/WAITING）会触发数据面动作，建议接着读：

- **u5-l3 写路径与 CRAQ 链式复制**：理解 `committed/pending` 双版本、链头串行化，弄清为什么 SYNCING 目标能接收 full-chunk-replace 写入。
- **u5-l5 数据恢复与同步 ResyncWorker**：本讲把「sync-done 后 localState 翻 UPTODATE」当成黑盒事件，这一讲讲清 dump-chunkmeta 对比、chunk 传输与 sync-done 是如何产生的，补全恢复链路。
- **u3-l3 主选举与故障切换**：本讲假设 mgmtd 是 primary；这一讲回答 primary mgmtd 自己崩溃时，集群如何选出新 primary 且不丢失路由信息——是本讲状态机能持续运转的前提。
- 若想再夯实，重读 [docs/design_notes.md:L180-L260](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L180-L260) 的「Data recovery」一节，与 `generateNewChain` 逐条对照，体会代码如何精确实现设计意图。
