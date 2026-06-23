# TENT 准入控制与 QoS（AdmissionQueue）

> 所属单元：第 4 单元 TENT 新一代声明式传输引擎
> 依赖讲义：[u4-l1 TENT 概述与设计动机](u4-l1-tent-overview.md)
> 难度：intermediate

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说清楚「准入控制（admission control）」要解决什么问题，以及它与排队、调度有什么区别。
2. 读懂 `LocalTransferAdmissionQueue` 的源码，理解它如何用「在途 owner 数量」和「在途字节数」两套配额来管理资源，以及 `staging` 预留机制如何保证内部暂存流量不被用户流量挤占。
3. 理解 TENT 的 QoS 三级优先级（HIGH/MEDIUM/LOW）是如何在 worker 线程、设备选择、跨进程共享内存三个层面做流量整形的，包括反饥饿（priority promotion）与全局时间片轮转。
4. 设计一个高负载压测实验，观察在途并发被限制后吞吐与延迟的变化曲线。

## 2. 前置知识

在进入源码前，先建立几个直觉。

### 2.1 为什么需要准入控制

假设一个 RDMA 集群同时收到 1 万个传输请求。如果引擎不加限制地全部接受，会出现两类问题：

- **资源耗尽**：每条在途（in-flight）传输都要占用 RDMA 的 send buffer、QP（Queue Pair）凭证、设备内存。在途任务过多会把硬件资源吃满，反而导致所有人都变慢甚至超时失败。
- **尾延迟爆炸**：低优先级的批量传输占满带宽，关键的控制消息/小请求被挤到队尾，99 分位延迟（P99）飙升。

**准入控制（admission control）** 的作用是在「请求进入系统」这一关就做一次判断：当前资源还够不够？如果不够，直接拒绝（返回 `TooManyRequests`）或排队，而不是无脑放行后在底层互相踩踏。

> 一句话区分三个概念：
> - **准入控制（admission）**：决定「这条请求能不能进来」，对应本讲的 `AdmissionQueue`。
> - **排队（queueing）**：决定「先进来的在哪等着」，对应 worker 的优先级队列。
> - **调度（scheduling）**：决定「现在让谁先走」，对应 worker 的出队选择与设备选择。

### 2.2 两类需要被区分的流量

TENT 区分两类「队列主人（owner）」：

- `User`：用户提交的普通传输请求。
- `StagingInternal`：引擎内部暂存（staging/proxy）用的请求，例如 GPU 直通不可用时，先把数据搬到 CPU 中转再发出去。

为什么要把它们分开？因为如果用户流量把资源全占了，引擎内部的暂存路径就会卡死，导致连正常的跨卡传输都做不了。所以 `AdmissionQueue` 给 `StagingInternal` 预留了一部分专属配额（reserve）。

### 2.3 QoS 与优先级

QoS（Quality of Service）的目标是在多租户、多负载环境下，让高优先级请求得到优待。TENT 用三级优先级：

| 优先级 | 数值 | 典型用途 |
|--------|------|----------|
| `PRIO_HIGH` | 0 | 元数据、控制消息、延迟敏感操作 |
| `PRIO_MEDIUM` | 1 | 交互式查询、推理服务 |
| `PRIO_LOW` | 2 | 批量数据搬运、后台任务 |

数值越小优先级越高（这是后面理解 `priority <= slot` 这类判断的关键）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [admission_queue.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/runtime/admission_queue.h) | 准入队列的接口与数据结构（`QueueLimits`、`QueueOwnerKind`、`QueueState`、`QueueOwner`） |
| [admission_queue.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/admission_queue.cpp) | 准入队列的核心实现：`tryAdmit` / `pickForDispatch` / `complete` / `retireBatch` |
| [admission_queue_test.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/tests/admission_queue_test.cpp) | 准入队列的单元测试，是理解其行为约束的最佳材料 |
| [docs/source/design/tent/qos.md](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/tent/qos.md) | TENT QoS 架构与配置文档 |
| [types.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/common/types.h) | 优先级常量 `PRIO_HIGH/MEDIUM/LOW` 与 `Request::priority` 字段 |
| [transfer_engine_impl.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp) | TENT 引擎主实现，`request.priority` 在这里被传递给传输选择 |
| [workers.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/workers.cpp) | RDMA worker 的优先级出队与反饥饿提升 |
| [quota.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp) | `DeviceSelector`：优先级设备过滤与带宽估计（EWMA） |
| [shared_quota.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/transport/rdma/shared_quota.h) / [shared_quota.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/shared_quota.cpp) | 跨进程全局时间片协调（共享内存） |

> ⚠️ **重要事实**：`LocalTransferAdmissionQueue` 目前是一个**运行时私有、单线程**的模块，仓库已为它编写完整的单元测试，但它**尚未被接入** `TransferEngineImpl::submitTransfer` 的真实调用链。头文件注释明确写道：「the eventual TransferEngineImpl integration owns synchronization」（见 [admission_queue.h:62-64](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/runtime/admission_queue.h#L62-L64)）。因此本讲会分两部分讲：**4.1 节讲准入控制的设计模型**（基于真实源码与测试），**4.2/4.3 节讲当前真正生效的 QoS 机制**（已接入 RDMA transport）。请在「实践」环节注意区分这两者的状态。

## 4. 核心概念与源码讲解

### 4.1 AdmissionQueue：准入控制与资源管理

#### 4.1.1 概念说明

`LocalTransferAdmissionQueue` 是 TENT 为传输请求设计的**资源记账型准入控制器**。它维护两个维度的「账本」：

- **owner 数量**：当前有多少个尚未终结的「队列主人」。
- **字节数**：当前这些主人总共占用了多少字节。

每次请求尝试进入（`tryAdmit`）时，引擎先把它想消耗的 owner 数和字节数累加到账本上，看是否超过上限；如果超过就拒绝，账本保持不变。当一个传输终结（`complete`）时，再从账本上扣减。这本质上是一个**二维的资源配额**模型。

更关键的是 `staging` 预留机制：总配额被切成两份，普通用户流量只能用到「总配额 − 预留」，而内部 `StagingInternal` 流量可以动用全部配额（含预留）。这样无论用户多忙，引擎内部的中转路径总有兜底资源可用。

#### 4.1.2 核心流程

准入队列里一个 owner 的生命周期是一个小状态机：

```
        tryAdmit              pickForDispatch             complete
Queued ─────────► (在 fifo_ 等待) ─────────► Dispatching ─────────► Completed
                     │                                              └─► Failed
                     └─ retireBatch 后从 owners_ 删除
```

`tryAdmit` 的判断流程（伪代码）：

```
function tryAdmit(submit):
    # 1. 参数校验：batch_token、owner kind、length>0、无重复 public_task_id
    # 2. 计算本次提交的总消耗
    owner_charge = submit.owners.size()
    byte_charge  = Σ owner.request.length
    # 3. 累加到「预期值」（先算到临时变量，避免污染当前账本）
    next_owners = outstanding_owners_ + owner_charge
    next_bytes  = outstanding_bytes_  + byte_charge
    # 4. 四道容量闸门
    if next_owners > max_outstanding_owners:          return TooManyRequests
    if next_bytes  > max_outstanding_bytes:           return TooManyRequests
    if next_user_owners > (max_owners - staging_owner_reserve): return TooManyRequests
    if next_user_bytes  > (max_bytes  - staging_byte_reserve):  return TooManyRequests
    # 5. 全部通过 → 真正记账并入队
    outstanding_owners_ = next_owners
    outstanding_bytes_  = next_bytes
    push owner into owners_ and fifo_
```

这里的设计要点是**「先算临时值、全过才提交」**——只要有一道闸门没过，`tryAdmit` 直接返回错误，账本和队列**完全不变**。这一点在测试里有专门验证（见下文 4.1.3）。

资源记账可以用两条不等式概括。设用户在途 owner 数为 \(u_o\)、用户在途字节数为 \(u_b\)、内部在途为相应项，总上限为 \(O_{\max}\) 与 \(B_{\max}\)，预留为 \(O_r\) 与 \(B_r\)，则准入条件为：

\[
\text{total\_owners} = u_o + s_o \le O_{\max}, \qquad
\text{total\_bytes}  = u_b + s_b \le B_{\max}
\]

而用户流量额外受限：

\[
u_o \le O_{\max} - O_r, \qquad u_b \le B_{\max} - B_r
\]

可见预留值是「从用户可用容量里划走的」，而不是额外叠加的；内部流量则受总上限约束即可。

#### 4.1.3 源码精读

**（1）数据结构与配额定义**

`QueueLimits` 定义了四道闸门的参数（[admission_queue.h:40-45](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/runtime/admission_queue.h#L40-L45)）：

```cpp
struct QueueLimits {
    size_t max_outstanding_owners{0};
    size_t max_outstanding_bytes{0};
    size_t staging_owner_reserve{0};
    size_t staging_byte_reserve{0};
};
```

而运行时的四把账本计数器则是私有成员（[admission_queue.h:116-119](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/runtime/admission_queue.h#L116-L119)：

```cpp
size_t outstanding_owners_{0};        // 所有在途 owner
size_t outstanding_bytes_{0};         // 所有在途字节
size_t outstanding_user_owners_{0};   // 仅 User kind
size_t outstanding_user_bytes_{0};    // 仅 User kind
```

注意「总账」和「用户账」是分开记的——这是因为判断闸门时，用户流量要看 `(总上限 − 预留)`，而总流量看 `总上限`。

**（2）四道闸门**

核心判断在 `tryAdmit` 中（[admission_queue.cpp:141-158](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/admission_queue.cpp#L141-L158)）：

```cpp
const size_t user_owner_limit =
    limits_.max_outstanding_owners - limits_.staging_owner_reserve;
const size_t user_byte_limit =
    limits_.max_outstanding_bytes - limits_.staging_byte_reserve;

if (next_outstanding_owners > limits_.max_outstanding_owners)
    return Status::TooManyRequests("queue owner capacity exceeded" LOC_MARK);
if (next_outstanding_bytes > limits_.max_outstanding_bytes)
    return Status::TooManyRequests("queue byte capacity exceeded" LOC_MARK);
if (next_user_owners > user_owner_limit)
    return Status::TooManyRequests("user owner capacity exceeded" LOC_MARK);
if (next_user_bytes > user_byte_limit)
    return Status::TooManyRequests("user byte capacity exceeded" LOC_MARK);
```

关键细节：`next_*` 都是用 `checkedAdd` 算出的临时变量，先算好再比对；任意一道不过，函数立即返回，**已经分配的 `owners_`、`fifo_`、四个计数器一个都不动**。这正是「无副作用拒绝」的保证。

**（3）溢出保护与配额合法性校验**

由于字节数会累加，必须防溢出。`checkedAdd` 用 `size_t` 上限做保护（[admission_queue.cpp:40-47](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/admission_queue.cpp#L40-L47)）：

```cpp
Status checkedAdd(size_t lhs, size_t rhs, size_t& out) {
    if (rhs > std::numeric_limits<size_t>::max() - lhs)
        return Status::InvalidArgument("admission queue charge overflow" LOC_MARK);
    out = lhs + rhs;
    return Status::OK();
}
```

而 `validateLimits` 保证预留值不会超过总上限（否则 `user_owner_limit` 这种减法会下溢），见 [admission_queue.cpp:49-59](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/admission_queue.cpp#L49-L59)。一旦 `QueueLimits` 非法，`limits_status_` 会被记下，后续所有 `tryAdmit` 都会在第一行 `CHECK_STATUS(limits_status_)` 失败。

**（4）FIFO 派发与按字节预算截断**

`pickForDispatch` 决定「现在派发哪些 owner 真正去传输」（[admission_queue.cpp:185-215](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/admission_queue.cpp#L185-L215)）：

```cpp
while (!fifo_.empty() && used_owners < max_owners) {
    auto owner_id = fifo_.front();
    // 防御性跳过已经不在 Queued 状态的陈旧条目
    if (owner_it->second.state != QueueState::Queued) { fifo_.pop_front(); continue; }
    const size_t remaining_bytes = max_bytes - used_bytes;
    if (owner.request.length > remaining_bytes) break;   // 字节预算用尽就停
    fifo_.pop_front();
    owner_it->second.state = QueueState::Dispatching;
    picked.push_back(owner_id);
    used_bytes += owner.request.length;
}
```

两点值得注意：派发顺序严格遵循入队顺序（FIFO，保证公平）；并且派发有个「字节预算」上限——如果一个 owner 太大，塞不进剩余预算，就直接 `break` 而不是跳过它，避免大任务饿死后面的、或造成乱序。

**（5）终结时回退配额**

`complete` 把状态推进到 `Completed`/`Failed`，并把之前记的账扣回去（[admission_queue.cpp:235-243](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/admission_queue.cpp#L235-L243)）：

```cpp
owner.state = terminal_status == TransferStatusEnum::COMPLETED
                  ? QueueState::Completed : QueueState::Failed;
--outstanding_owners_;
outstanding_bytes_ -= owner.request.length;
if (owner.kind == QueueOwnerKind::User) {
    --outstanding_user_owners_;
    outstanding_user_bytes_ -= owner.request.length;
}
```

注意：只有 `Dispatching` 状态的 owner 才能 `complete`——这保证配额「先占后还」的对称性。测试 `RequiresDispatchBeforeTerminalCompletion` 专门验证了「没派发就直接 complete」会被拒绝。

**（6）测试是最好的文档**

行为约束几乎都写在了单元测试里。例如 `PreservesStagingReserveForStagingInternalOwners`（[admission_queue_test.cpp:208-230](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/tests/admission_queue_test.cpp#L208-L230)）构造了 `{max_owners=2, max_bytes=100, staging_owner_reserve=1, staging_byte_reserve=40}` 的配额：先提交一个 60 字节的 User owner（成功，剩 40 用户字节），再提交一个 1 字节的 User owner（失败，因为 `user_byte_limit = 100-40 = 60` 已满），但一个 40 字节的 `StagingInternal` owner 仍能成功（因为它能动用预留）。这把「预留」的语义演示得非常清楚。

#### 4.1.4 代码实践

由于 `AdmissionQueue` 尚未接入主调用链，最可靠的实践是**直接运行它的单元测试**，亲眼看到准入/拒绝行为。

**实践目标**：通过编译并运行 `admission_queue_test`，观察「无副作用拒绝」和「staging 预留」两个核心行为。

**操作步骤**：

1. 定位测试源码与构建配置（[tests/CMakeLists.txt](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/tests/CMakeLists.txt) 中应有 `admission_queue_test` 目标）。
2. 在仓库根目录的构建目录里编译并运行该测试（示例命令，具体路径以你本地构建为准）：
   ```bash
   cmake -B build -DWITH_TE=ON
   cmake --build build --target admission_queue_test -j
   ./build/mooncake-transfer-engine/tent/tests/admission_queue_test
   ```
3. 也可以只跑某个 case 观察细节：
   ```bash
   ./build/.../admission_queue_test --gtest_filter=AdmissionQueueTest.PreservesStagingReserveForStagingInternalOwners
   ```

**需要观察的现象**：
- `RejectsCapacityExceededWithoutPartialAdmission`：超容量提交被拒，且 `outstandingOwners()/outstandingBytes()` 保持为 0（证明账本未被污染）。
- `PreservesStagingReserveForStagingInternalOwners`：User 流量触到「总上限 − 预留」就被拒，但 `StagingInternal` 仍可使用预留额度。

**预期结果**：所有用例 `PASSED`。如果你想动手验证，可以临时在 `admission_queue.cpp` 的 `tryAdmit` 四道闸门处各加一行 `LOG(INFO)` 打印 `next_*` 与对应 `limit_`，重新跑测试观察数值如何变化（这是源码阅读型实践，**记得不要把改动提交到仓库**）。

> 若本地暂无 TENT 编译环境，则此步骤为「待本地验证」；你也可以仅通过阅读 [admission_queue_test.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/tests/admission_queue_test.cpp) 的断言来推断行为。

#### 4.1.5 小练习与答案

**练习 1**：假设 `QueueLimits = {max_owners=3, max_bytes=1000, staging_owner_reserve=1, staging_byte_reserve=200}`。当前已有一个 500 字节的 User owner 在途。现在再尝试提交一个 400 字节的 User owner，能否通过？再尝试一个 400 字节的 `StagingInternal` owner 呢？

**答案**：用户字节上限 = 1000 − 200 = 800。已有 500，再提交 400 会变成 900 > 800，所以 User owner **被拒**（`user byte capacity exceeded`）。而 `StagingInternal` 受总上限约束：500 + 400 = 900 ≤ 1000，且 owner 数 2 ≤ 3，所以**通过**（它用到了 200 字节的预留额度）。

**练习 2**：为什么 `pickForDispatch` 在遇到「owner 大于剩余字节预算」时是 `break` 而不是 `continue`？

**答案**：因为派发要保证 FIFO 公平与顺序性。如果用 `continue` 跳过大 owner 去派发后面的小 owner，会造成乱序，并且大 owner 可能永远排在前面被反复跳过（饥饿）。`break` 表示「这一轮的预算就到这里」，下一轮预算恢复时大 owner 自然排最前面优先派发。

**练习 3**：`complete()` 要求 owner 必须处于 `Dispatching` 状态。如果一个 owner 还在 `Queued`（没被 `pickForDispatch` 选中）就调用 `complete`，会发生什么？为什么需要这个约束？

**答案**：返回 `InvalidEntry`（[admission_queue.cpp:231-233](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/admission_queue.cpp#L231-L233)）。约束的意义在于保证「占用配额」和「释放配额」的对称：配额是在 `tryAdmit` 时记上的，但 `complete` 扣减对应的是「已经派发出去正在传」的资源；只有进入 `Dispatching` 才意味着资源真正在用。这避免了「记账了却没派发」造成的计数错乱。

---

### 4.2 QoS 策略：优先级队列与流量整形

#### 4.2.1 概念说明

上一节的准入控制解决「进多少」，本节的 QoS 解决「先服务谁」。TENT 在 RDMA transport 中已经实现了一套**三级优先级**的流量整形机制，它由三层叠加：

1. **每 worker 的优先级队列**：每个工作线程维护 `PRIO_HIGH/MEDIUM/LOW` 三个队列，严格按 HIGH → MEDIUM → LOW 的顺序出队。
2. **优先级提升（反饥饿）**：低优先级请求等太久会被「提升」一级，保证它最终能被服务。
3. **全局时间片协调**：跨进程场景下，用共享内存里的时间片，让高优先级请求获得专属服务窗口，避免单个进程长期独占带宽。

数值定义在 [types.h:32-35](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/common/types.h#L32-L35)：`PRIO_HIGH=0, PRIO_MEDIUM=1, PRIO_LOW=2`，而 `Request::priority` 默认就是 `PRIO_HIGH`（[types.h:75-76](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/common/types.h#L75-L76)）。

#### 4.2.2 核心流程

每个 worker 线程处理一次发送（`asyncPostSend`）时，决策顺序是（[workers.cpp:306-321](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/workers.cpp#L306-L321)）：

```
1. promoteTimedOutRequests(worker)    # 先做反饥饿提升
2. for prio in [HIGH, MEDIUM, LOW]:   # 严格优先级出队
       if 共享配额存在 且 not canSend(prio): continue   # 跨进程闸门
       result = worker.queues[prio].pop()
       if result 非空: break
3. 把取出的 slice 真正 post 到 RDMA endpoint
```

**全局时间片**的公平性可以用「占空比」来理解。一个完整周期有 `NUM_SLOTS = 3` 个时间片（[shared_quota.h:47](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/transport/rdma/shared_quota.h#L47)），默认每个 2ms，共 6ms。判断式 `isPriorityAllowedInSlot(prio, slot) = prio <= slot` 意味着：

| 时间片 slot | 允许的优先级 | 占周期比例 |
|-------------|-------------|-----------|
| 0 | HIGH only | 1/3 |
| 1 | HIGH + MEDIUM | 1/3 |
| 2 | ALL | 1/3 |

于是在饱和负载下，各类请求被允许发送的时间片比例为：

\[
\text{HIGH} : \frac{3}{3} = 100\%, \quad
\text{MEDIUM} : \frac{2}{3} \approx 67\%, \quad
\text{LOW} : \frac{1}{3} \approx 33\%
\]

这就是 TDM（时分复用）式的带宽分配。HIGH 在每个片都能发，LOW 只在 slot 2 能发，天然获得最高优先级。

#### 4.2.3 源码精读

**（1）每 worker 的三级队列**

worker 上下文里固定三个优先级队列（[workers.h:184-188](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/transport/rdma/workers.h#L184-L188)）：

```cpp
static constexpr int kNumPriorityLevels = PRIO_LOW + 1;   // = 3
struct WorkerContext {
    ...
    BoundedSliceQueue queues[kNumPriorityLevels];  // Priority queues
    ...
    uint64_t next_promotion_check_ns = 0;           // 下次提升检查时刻
};
```

**（2）严格优先级出队 + 全局闸门**

[workers.cpp:306-321](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/workers.cpp#L306-L321)：

```cpp
auto shared_quota =
    device_selector_ ? device_selector_->getSharedSlotManager() : nullptr;
promoteTimedOutRequests(worker);
// Priority selection: HIGH -> MEDIUM -> LOW
for (int prio = PRIO_HIGH; prio < kNumPriorityLevels; ++prio) {
    if (shared_quota && !shared_quota->canSend(prio)) continue;
    worker.queues[prio].pop(result);
    if (!result.empty()) break;
}
```

注意 `canSend(prio)` 是跨进程的全局闸门——即便本地 HIGH 队列有请求，如果当前全局时间片不允许（理论上 slot 0 才允许 HIGH……实际 HIGH 总是允许，见下），也会 `continue`。`break` 保证了「取到任何一个非空队列就停止」，从而严格遵循优先级顺序。

**（3）全局闸门的实现**

`canSend` 读取共享内存里的当前时间片并比对（[shared_quota.cpp:140-151](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/shared_quota.cpp#L140-L151)）：

```cpp
bool SharedSlotManager::canSend(int priority) {
    if (!hdr_) return true;   // 未启用全局协调 → 总是允许
    int current_slot = hdr_->current_slot.load(std::memory_order_acquire);
    return isPriorityAllowedInSlot(priority, current_slot);   // prio <= slot
}
```

而当前时间片由后台线程根据墙上时钟推进（[shared_quota.cpp:168-184](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/shared_quota.cpp#L168-L184)）：

```cpp
uint64_t base_slot = now / (rotation_interval_ms_ * 1000000ull);
int global_slot = static_cast<int>(base_slot % NUM_SLOTS);
hdr_->current_slot.store(global_slot, std::memory_order_release);
```

因为时间片完全由 `now / interval % 3` 决定，所以**所有进程只要共享同一份 `hdr_`（同一台机的共享内存），它们的 `current_slot` 是同步推进的**，无需额外协调就能进入相同的窗口。共享内存头里还放了一把 `PTHREAD_PROCESS_SHARED` + `ROBUST` 的互斥锁（[shared_quota.cpp:53-74](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/shared_quota.cpp#L53-L74)），`ROBUST` 保证某进程持锁时崩溃，下一个获取者能拿到 `EOWNERDEAD` 并恢复。

**（4）反饥饿：优先级提升**

如果 HIGH 队列一直有请求，LOW 就永远轮不上。`promoteTimedOutRequests` 解决这个问题（[workers.cpp:393-433](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/workers.cpp#L393-L433)）：每 1ms 检查一次，把等待超过 `priority_promotion_timeout_us`（默认 10ms）的请求向上提一级——MEDIUM → HIGH，LOW → MEDIUM。

```cpp
worker.queues[PRIO_MEDIUM].pop(promoted);
auto* slice = promoted.front().first;
if (slice && (current_ts - slice->enqueue_ts) >= priority_promotion_timeout_ns_) {
    for (auto& slice_list : promoted)
        worker.queues[PRIO_HIGH].push(slice_list);   // 提升到 HIGH
    return;
}
// 否则原样放回 MEDIUM，避免空 pop 破坏顺序
for (auto& slice_list : promoted) worker.queues[PRIO_MEDIUM].push(slice_list);
```

注意这里「pop 出来再 push 回去」的写法：如果发现还没超时，必须把请求**原样放回**原队列，否则一次「探测性 pop」就把数据丢了。超时阈值由配置项 `transports/rdma/priority_promotion_timeout_us` 控制（[workers.cpp:116-120](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/workers.cpp#L116-L120)，默认 10000us = 10ms）。

**（5）配置项总览**

QoS 相关参数都在 RDMA transport 配置段（详见 [qos.md 配置章节](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/tent/qos.md)）：

| 配置项 | 默认值 | 作用 | 读取位置 |
|--------|--------|------|----------|
| `enable_priority_filtering` | `true` | 是否启用优先级设备过滤 | [workers.cpp:105-106](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/workers.cpp#L105-L106) |
| `local_rotation_interval_us` | `200` | 本地设备优先级轮转间隔 | [workers.cpp:108-110](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/workers.cpp#L108-L110) |
| `priority_promotion_timeout_us` | `10000` | 反饥饿提升超时 | [workers.cpp:118-120](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/workers.cpp#L118-L120) |
| `slot_rotation_interval_ms` | `2` | 全局时间片长度 | `SharedSlotManager::setRotationIntervalMs` |
| `shared_quota_shm_path` | `""` | 共享内存路径，空=禁用全局协调 | `enableSharedQuota` |

#### 4.2.4 代码实践

**实践目标**：通过修改 QoS 配置，体会时间片长度对高/低优先级延迟与吞吐的影响。

**操作步骤**：

1. 阅读官方 QoS 文档的「Performance Considerations / Trade-offs」表（[qos.md:282-291](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/tent/qos.md#L282-L291)），理解「短时间片=高优先级延迟好/低优先级吞吐差」这一权衡。
2. 准备一个能跑跨进程传输的最小 TENT 配置（参考 [u4-l1](u4-l1-tent-overview.md) 与 qos.md 的 Example 1/3）。
3. 分别用三套配置跑同样的混合负载（同时发 HIGH 和 LOW 请求）：
   - A：`slot_rotation_interval_ms=1`，启用全局协调
   - B：默认 `2`
   - C：`slot_rotation_interval_ms=10`
4. 记录每套配置下 HIGH 与 LOW 的吞吐和 P99 延迟。

**需要观察的现象**：时间片越短，HIGH 的 P99 延迟越低（更频繁的专属窗口），但 LOW 吞吐越低；时间片越长则相反。

**预期结果**：与 qos.md 中 Trade-off 表的趋势一致。若手边没有多 NIC/RDMA 环境，则此项为「待本地验证」；此时可退化为**源码阅读型实践**——在 `asyncPostSend` 的出队循环处加日志打印每次取到的 `prio` 与 `canSend` 结果，对照时间片理论值验证调度顺序。

#### 4.2.5 小练习与答案

**练习 1**：全局时间片设为默认 2ms，周期内 LOW 请求平均能获得多少发送窗口比例？如果改成 1ms 呢？

**答案**：无论片长多少，LOW 都只在 slot 2 发送，占周期 \(1/3\)。改成 1ms 只是让周期变短（3ms），比例仍是 \(1/3\)；但单位时间内轮转更频繁，LOW 请求的「最长等待」从约 4ms（slot 0、1 都轮不到）降到约 2ms，所以延迟会改善，比例不变。

**练习 2**：如果一个 LOW 请求在队列里等了 12ms（`priority_promotion_timeout_us=10000`），它会被提升到哪一级？提升后还会不会再被提升？

**答案**：先从 LOW 提升到 MEDIUM。下一次提升检查（≤1ms 后）如果它作为 MEDIUM 又等满 10ms，会再被提升到 HIGH。所以提升是「逐级」的：LOW→MEDIUM→HIGH，不会跨级。

**练习 3**：为什么 `canSend` 在 `hdr_ == nullptr`（未启用全局协调）时直接返回 `true`？

**答案**：`hdr_` 为空意味着没有 `attach` 共享内存，即单进程模式，不需要跨进程公平。此时若仍返回 `false` 会让所有请求都发不出去，所以单进程下退化为「只用本地每 worker 优先级队列」，全局闸门放行一切。

---

### 4.3 资源管理：从准入到设备调度的全局视图

#### 4.3.1 概念说明

把前两节串起来，TENT 的「资源管理」其实是分层的：

- **请求层（准入）**：`AdmissionQueue` 用 owner/字节配额决定「放不放行」——保护系统不被过量在途请求压垮。
- **传输层（QoS 出队）**：worker 的优先级队列 + 全局时间片决定「谁先传」——保证高优先级延迟、跨进程公平。
- **设备层（NUMA 感知调度）**：`DeviceSelector` 在多个 NIC 之间做切片喷射，并做一次**优先级设备过滤**——让高优先级请求优先落在「当前优先级窗口」内的设备上，同时用 EWMA 带宽估计做负载均衡。

这三层是逐步生效的：先准入，再排队，最后落到具体硬件。`Request::priority` 这个字段会一路传递到设备层影响选路。

#### 4.3.2 核心流程

`request.priority` 的传递路径：

```
用户构造 Request(带 priority)
  └─► TransferEngineImpl::submitTransfer
        └─► getTransportType(request)
              └─► SelectionContext.priority_level = request.priority   # 传入选择上下文
                    └─► TransportSelector::select(...)
                          └─► DeviceSelector::buildCandidates(..., request_priority)
                                └─► 设备过滤: 仅当 dev_priority >= request_priority 才入选
```

设备层的「优先级」本身是随时间轮转的——通过 `getDevicePriority(dev_id)` 让不同设备轮流成为「高优先级窗口」的设备，这是一种 NUMA 感知的负载均衡（[qos.md 的 Priority-aware device filtering](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/tent/qos.md)）。

#### 4.3.3 源码精读

**（1）priority 进入选择上下文**

在 `getTransportType` 中，请求优先级被拷进 `SelectionContext`（[transfer_engine_impl.cpp:885-889](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L885-L889)）：

```cpp
SelectionContext ctx;
ctx.transfer_size = request.length;
ctx.priority_level = request.priority;   // 请求优先级参与选择
ctx.policy_name = request.policy_name;
```

**（2）设备优先级过滤**

`DeviceSelector::buildCandidates` 在收集候选设备时，会先用 QoS 过滤一遍（[quota.cpp:149-163](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L149-L163)）：

```cpp
int dev_priority = PRIO_LOW;                 // 默认: 接受所有优先级
if (sched_params_.enable_priority_filtering)
    dev_priority = getDevicePriority(dev_id);
if (dev_priority < request_priority) continue;   // 设备当前优先级不够 → 跳过
add_candidate(dev_id, rank);
```

如果过滤后没有候选设备，会 fallback 到「不过滤、用全部设备」（[quota.cpp:165-174](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L165-L174)），保证不会因为过滤太严而找不到设备。

**（3）设备优先级随时间轮转**

`getDevicePriority` 让设备优先级按时间偏移轮转（[quota.cpp:340-357](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L340-L357)）：

```cpp
uint64_t offset_us = now / 1000;
size_t rotation_offset =
    (offset_us / local_rotation_interval_us) % num_devices;
base_index = (base_index + rotation_offset) % num_devices;
return static_cast<int>(base_index);
```

这让「哪台设备处于高优先级窗口」周期性轮换，配合候选评分（`predicted_time × numa_penalty + jitter`，[quota.cpp:131-147](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L131-L147)），既做 NUMA 亲和又做优先级隔离。

**（4）带宽估计（EWMA）支撑负载均衡**

设备选择评分依赖 `predicted_time = (inflight + slice_bytes) / ewma_bw`，而 `ewma_bw` 是指数加权移动平均，在每次传输完成时更新（[quota.cpp:287-317](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L287-L317)）：

\[
\text{ewma}_{\text{new}} = \alpha \cdot \text{ewma}_{\text{old}} + (1 - \alpha) \cdot \frac{\text{length}}{\text{latency}}
\]

并把结果 clamp 到 `[min_mult, max_mult] × 理论带宽` 之间，避免单次异常拖偏估计。可以用 `device_selector_->printTrafficStats()`（[quota.cpp:319-331](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L319-L331)）观察每台设备的总流量、EWMA 带宽和 inflight 字节，从而判断负载是否均衡。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 `submitTransfer` 中 `request.priority` 的传递路径，画出三层资源管理的分层视图。

**操作步骤**：

1. 在 [transfer_engine_impl.cpp:1218](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1218) 的 `submitTransfer` 处开始跟踪。
2. 跟到 `getTransportType`（[transfer_engine_impl.cpp:825](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L825)），确认 `priority_level` 被填入 `SelectionContext`。
3. 再进入 `TransportSelector::select` → `DeviceSelector::buildCandidates`，确认 `request_priority` 参与了设备过滤。

**需要观察的现象**：`priority` 字段从 `Request` 一路流到设备候选过滤条件，且没有在中间被丢弃。

**预期结果**：得到一张「准入队列（设计层）→ worker 优先级队列（传输层）→ DeviceSelector 设备过滤（设备层）」的分层图。

#### 4.3.5 小练习与答案

**练习 1**：如果 `enable_priority_filtering=false`，`getDevicePriority` 返回什么？设备过滤会怎样？

**答案**：返回 0（[quota.cpp:341-342](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L341-L342)）。在 `buildCandidates` 中 `dev_priority(0) < request_priority` 只有当 `request_priority > 0`（即 MEDIUM/LOW）时才成立——也就是说关掉过滤后，HIGH 默认放行；但更准确地说，过滤分支只在 `enable_priority_filtering` 为真时才走，关掉后 `dev_priority` 直接用默认 `PRIO_LOW=2`，几乎不过滤任何请求，退化为纯负载均衡选路。

**练习 2**：EWMA 的学习率 \(\alpha\) 设为 1.0 会怎样？设为 0.0 呢？

**答案**：\(\alpha=1.0\) 时 `ewma_new = ewma_old`，永远不更新（不学习），评分只能靠静态理论带宽；\(\alpha=0.0\) 时 `ewma_new = observed`，完全采用最近一次观测值，对抖动极其敏感。实际默认 `0.01`（[workers.cpp:81-82](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/workers.cpp#L81-L82)），偏向稳定。

---

## 5. 综合实践

把本讲三块内容串起来，设计一个**高负载压测实验**。由于 `AdmissionQueue` 尚未接入主链路，这个实验以「当前生效的 QoS 机制」为主观测对象，并以「AdmissionQueue 的设计模型」为理论对照。

**实验设计**：

1. **环境**：一台多 NIC 的 RDMA 机器（或退化为 TCP-only 单机，主要观察 QoS 出队行为）。准备一个能发起大量并发传输的客户端（可参考 `mooncake-transfer-engine/example` 下的基准脚本）。
2. **负载**：构造两类请求混合压测——
   - 持续不断的 `PRIO_LOW` 大块传输（模拟批量搬运）。
   - 间歇性的 `PRIO_HIGH` 小请求（模拟控制消息/交互查询）。
3. **变量扫描**：逐步提高并发度（同时在途任务数从 10、50、100、200、500……），每次记录：
   - 整体吞吐（GB/s）；
   - HIGH 与 LOW 各自的 P50 / P99 延迟；
   - 用 `printTrafficStats()` 打印各设备 EWMA 带宽与 inflight。
4. **对照分析**：
   - **传输层**：观察在 HIGH 负载下 LOW 的延迟是否被压制、是否在 ~10ms 后因 promotion 而改善（验证 4.2 节反饥饿）。
   - **设备层**：观察切片是否在多 NIC 间按 EWMA 均衡（验证 4.3 节负载均衡）。
   - **准入层（理论）**：用 4.1 节的配额模型，设想若 `AdmissionQueue` 以 `max_outstanding_bytes = 当前峰值 in-flight` 接入，曲线会在哪个并发度处由「线性增长」转为「平台/拒绝」，从而推断合理的 `max_outstanding_*` 设置。

**记录与产出**：画出三张曲线——吞吐 vs 并发度、HIGH 的 P99 vs 并发度、LOW 的 P99 vs 并发度；并写一段结论，说明 QoS 配置（`slot_rotation_interval_ms`、`priority_promotion_timeout_us`）应如何随你的负载特征调整。

> 若本地无 RDMA/多 NIC 环境，本实验的部分环节为「待本地验证」；可先做「源码阅读型」准备：在 `asyncPostSend` 与 `buildCandidates` 加日志，离线推演在给定并发度下的预期行为。

## 6. 本讲小结

- **准入控制**的本质是在请求入口做资源记账：`AdmissionQueue` 用「在途 owner 数」与「在途字节数」两套配额，加上 `staging` 预留，决定放不放行；拒绝是「无副作用」的——账本和队列完全不变。
- **`AdmissionQueue` 的 owner 状态机**为 `Queued → Dispatching → Completed/Failed`，`pickForDispatch` 严格 FIFO 且带字节预算截断，`complete` 与 `tryAdmit` 的记账严格对称。
- **当前已生效的 QoS** 是 RDMA transport 里的三级优先级机制：每 worker 三队列严格优先级出队、反饥饿提升（默认 10ms 超时逐级提升）、跨进程共享内存时间片（`prio <= slot`）。
- **全局时间片**是 TDM 式公平：HIGH 占 3/3、MEDIUM 占 2/3、LOW 占 1/3 的时间片窗口；时间片由墙上时钟推导，多进程共享同一份共享内存即可同步。
- **资源管理是分层的**：准入（放不放行）→ 传输层 QoS（谁先传）→ 设备层（落到哪台 NIC），`Request::priority` 一路传到 `DeviceSelector` 的设备过滤与 EWMA 负载均衡。
- **诚实的边界**：`LocalTransferAdmissionQueue` 目前是设计完整但尚未接入主链路的模块（有完整单测），本讲的压测实验以已生效的 QoS 机制为主要观测对象。

## 7. 下一步学习建议

- 下一讲 [u4-l5 TENT 控制面与元数据存储（ControlPlane/MetaStore）](u4-l5-tent-control-plane-metastore.md) 会讲 TENT 的分布式协调与故障转移，可以把它和本讲的「资源管理/限流」对照理解——一个是控制面的协调，一个是数据面的整形。
- 想深入 RDMA 层细节，可继续阅读 [workers.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/workers.cpp) 与 [quota.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp)，重点看切片（slice）如何在 worker 队列、endpoint、设备之间流转。
- 建议回头阅读 [qos.md](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/tent/qos.md) 的 Troubleshooting 小节，把本讲学到的配置项与实际故障排查对应起来。
- 关注 `AdmissionQueue` 后续接入 `TransferEngineImpl` 的演进：届时 `submitTransfer` 会先经过 `tryAdmit`，被拒的请求将走排队/退避路径，可对照本讲的配额模型理解新的限流行为。
