# Mooncake PG 弹性进程组

本讲义深入分析 Mooncake PG（Process Group）作为 PyTorch 分布式后端的实现机制，讲解如何通过 MooncakeBackendOptions 扩展 PyTorch 进程组、active_ranks 状态追踪、弹性恢复协议，以及如何与 EP buffer 协同实现容错 MoE 推理。

## 1. PyTorch 后端集成

### 概念说明

PyTorch 分布式通信通过 ProcessGroup 抽象层提供集合通信（collective）和点对点（P2P）原语。Mooncake PG 是一个自定义后端实现，继承 `c10d::ProcessGroup`，并注册为 `mooncake`（加速器设备）和 `mooncake-cpu`（CPU 设备）两个后端。

Mooncake PG 的核心价值在于：
- 提供 RDMA/IBGDA 高速传输能力
- 内置 rank 健康状态追踪
- 支持弹性恢复协议，允许部分 rank 失效后替换进程继续服务
- 与 Mooncake EP buffer 协同，实现容错 MoE 推理

### 伪代码或流程

```
# PyTorch 后端注册与初始化流程
import torch.distributed as dist
from mooncake import pg

# 1. 定义 active_ranks 状态张量（int32，长度为 max_world_size）
active_ranks = torch.tensor([1, 1, 1, 0], dtype=torch.int32, device="cuda")

# 2. 配置 Mooncake 后端选项
pg_options = pg.MooncakeBackendOptions(
    activeRanks=active_ranks,      # rank 健康状态
    isExtension=False,              # 是否为扩展/替换 rank
    maxWorldSize=4                  # 预留容量
)

# 3. 初始化进程组
dist.init_process_group(
    backend="mooncake",
    rank=0,
    world_size=3,
    pg_options=pg_options
)

# 4. 正常使用 PyTorch 分布式 API
tensor = torch.randn(1024, 768).cuda()
dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
```

### 原理分析

Mooncake PG 通过以下机制集成到 PyTorch 分布式体系：

**后端注册机制**
```python
# mooncake/pg/__init__.py 中的注册逻辑
# mooncake 和 mooncake-cpu 两个后端被注册到 PyTorch 的 Backend 注册表
# PyTorch 通过 dist.init_process_group(backend="mooncake") 查找并实例化
```

**类继承结构**
```
c10d::ProcessGroup (PyTorch 基类)
    ↑ 继承
MooncakeBackend (Mooncake 实现)
    - 覆盖所有 collective 操作：allreduce, allgather, broadcast, 等
    - 实现 P2P 操作：send, recv
    - 添加 Mooncake 特定方法：extendGroupSizeTo, getPeerState, recoverRanks, joinGroup
```

**P2P 分发适配**
- PyTorch 的 `batch_isend_irecv` 需要 `c10d::Backend` 对象（而非 ProcessGroup）
- Mooncake 提供 `MooncakeP2PShim` 轻量级适配器，继承 `c10d::Backend`，将 send/recv 委托给所属的 `MooncakeBackend`
- Shim 在 ProcessGroup 的 `deviceTypeToBackend_` 映射中注册，供 P2P 调度路径查找

**容量 vs 可见大小**
- `size`（容量）：后端预留的 rank 槽位数
- `activeSize`（可见大小）：`dist.get_world_size()` 返回的值，仅包含 active ranks
- 当 `max_world_size > initial_world_size`，额外槽位被预留但标记为 inactive

### 代码实践

Mooncake PG 后端初始化和注册的核心实现：

**MooncakeBackendOptions 定义**
[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/include/mooncake_backend.h#L61-L80](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/include/mooncake_backend.h#L61-L80)

这段代码定义了 Mooncake 特定的进程组配置选项：
- `activeRanks_`：rank 健康状态张量，必须为 `torch.int32` 类型
- `isExtension_`：标记当前进程是否为扩展/替换 rank
- `maxWorldSize_`：预留容量上限，当大于 0 时后端会预分配 rank 元数据

**MooncakeBackend 构造函数**
[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/include/mooncake_backend.h#L93-L95](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/include/mooncake_backend.h#L93-L95)

构造函数接收 PyTorch 的分布式选项和 Mooncake 特定选项，初始化 active-rank 掩码和预留槽位。

**P2P Shim 适配器**
[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/include/mooncake_backend.h#L33-L57](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/include/mooncake_backend.h#L33-L57)

`MooncakeP2PShim` 是非拥有指针的轻量级适配器，仅实现 PyTorch P2P 调度路径所需的接口，将实际操作委托给所属的 `MooncakeBackend`。

### 练习题

1. 为什么需要 `MooncakeP2PShim` 而不是直接让 `MooncakeBackend` 继承 `c10d::Backend`？

2. 当 `max_world_size=4` 但初始只有 3 个 rank 时，`dist.get_world_size()` 返回什么值？

3. 如果 activeRanks 张量被放在错误设备上（如 `mooncake-cpu` 后端使用 GPU 张量），会发生什么？

### 答案

1. PyTorch 的 ProcessGroup 已经继承自有别于 Backend 的基类体系，且 P2P 调度路径严格要求 `getBackend()` 返回 `c10d::Backend` 实例。使用 Shim 可以在不改变 MooncakeBackend 继承层次的情况下满足此要求。

2. 返回 3。`max_world_size` 仅预留容量，不改变 `dist.get_world_size()` 的可见大小。只有通过 `recover_ranks()` 激活新 rank 后，`activeSize` 才会增长。

3. 后端初始化会失败或运行时报错，因为 Mooncake PG 要求 activeRanks 张量设备类型与后端类型匹配（CPU 后端用 CPU 张量，加速器后端用加速器张量）。

## 2. 弹性恢复协议

### 概念说明

Mooncake PG 的弹性恢复协议允许在部分 rank 失效后，通过替换进程恢复通信能力，而无需重建整个进程组。这对长运行的 MoE 推理服务至关重要，因为重启整个服务会丢失所有正在处理的请求。

协议的核心思想是：健康 rank 预留容量（`max_world_size`），新 rank 以扩展模式加入，健康 rank 通过显式协议激活新 rank，双方协同刷新高层元数据（如 EP buffer）。

### 伪代码或流程

```
# 弹性恢复协议的三阶段流程

# 阶段 1：健康 rank 预留容量
healthy_rank_init:
    init_process_group(world_size=3, max_world_size=4, is_extension=False)
    # 此时 active_ranks = [1, 1, 1, 0]，rank 3 槽位预留但未激活

# 阶段 2：替换 rank 发布元数据并等待
replacement_rank_init:
    wait_for("recover_start_signal")
    init_process_group(world_size=4, rank=3, is_extension=True, max_world_size=4)
    # 发布本地 peer 元数据（连接信息、内存区域等）
    join_group(backend)  # 阻塞，等待健康 rank 调用 recover_ranks

# 阶段 3：健康 rank 激活替换 rank
healthy_rank_recover:
    while not get_peer_state(backend, ranks=[3]):
        sleep(0.05)  # 轮询等待替换 rank 发布元数据
    recover_ranks(backend, ranks=[3])  # 激活 rank 3
    # 替换 rank 的 join_group() 返回，双方进入正常 collective
```

### 原理分析

弹性恢复协议的正确性依赖于以下机制：

**元数据发布顺序**
1. 扩展 rank 在 `init_process_group` 时自动发布本地 peer 元数据（通过 `publishLocalPeerMetadata()`）
2. 健康 rank 通过 `get_peer_state(ranks)` 轮询检查目标 rank 是否已发布元数据
3. 所有健康 rank 需以**一致顺序**调用 `get_peer_state` 和 `recover_ranks`，避免死锁

**ExtensionState 传播**
- `recover_ranks()` 将 `ExtensionState`（包括 `activeRanks`、`p2pEpochs`、`taskCount`）写入共享 Store
- 扩展 rank 的 `join_group()` 轮询等待此状态出现，读取后返回
- 这确保扩展 rank 获得与所有健康 rank 一致的视图

**连接建立与 QP 刷新**
- 连接轮询器（Connection Poller）持续监听新 peer 的连接请求
- `recover_ranks()` 触发与目标 rank 的连接建立（如 RDMA QP 创建）
- EP buffer 需调用 `Buffer.update_ep_member()` 刷新 peer 元数据

**状态一致性保证**
```
健康 rank 视角：
activeSize: 3 → 3（recover_ranks 前） → 4（recover_ranks 后）
activeRanks: [1,1,1,0] → [1,1,1,1]

扩展 rank 视角：
activeSize: 4（本地始终为 max_world_size，通过 activeRanks 掩码实现本地独占）
activeRanks: [1,1,1,0] → [1,1,1,1]（join_group 返回后）
```

### 代码实践

**弹性恢复协议的实现**
[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/tests/test_pg_elastic.py#L603-L677](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/tests/test_pg_elastic.py#L603-L677)

此测试展示了完整的恢复流程：
- 健康 rank 初始 4 个 rank，rank 3 退出
- 存活 rank 继续运行 collective（3 个 rank）
- 替换 rank 以 `is_extension=True` 加入
- 健康 rank 轮询 `get_peer_state` 等待替换 rank 就绪
- 调用 `recover_ranks` 激活，双方完成最终 collective

**get_peer_state 轮询模式**
[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/tests/test_pg_elastic.py#L638-L645](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/tests/test_pg_elastic.py#L638-L645)

`wait_until` 封装了轮询逻辑，使用较长间隔（2s）避免连接轮询器过载。

**ExtensionState 序列化**
[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/include/mooncake_backend.h#L258-L264](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/include/mooncake_backend.h#L258-L264)

`ExtensionState` 结构体包含扩展所需的关键状态，通过 `serialize`/`deserialize` 在 Store 中传播。

### 练习题

1. 如果健康 rank 和替换 rank 同时调用 `recover_ranks` 和 `join_group`（不遵守协议顺序），会发生什么？

2. 为什么扩展 rank 的 `activeSize` 初始值等于 `world_size`（而非 1）？

3. `get_peer_state` 返回 `True` 是否意味着 target rank 已经可以参与 collective？

### 答案

1. 可能死锁或状态不一致。`join_group` 会阻塞等待 `ExtensionState`，而此状态由 `recover_ranks` 写入；如果顺序错误，双方永远无法同步。

2. 因为扩展 rank 的 `activeSize` 只是 PyTorch API 的返回值，真正的本地独占行为由 `activeRanks` 掩码控制。扩展 rank 初始时 `activeRanks` 全为 0（或仅自身为 1），无论 `activeSize` 为何值，collective 都会跳过其他 rank。

3. 不是。`get_peer_state` 仅表示 target rank 已发布元数据且连接可达，但尚未激活。必须等待 `recover_ranks()` 调用后，target rank 才能参与 collective。

## 3. Active Ranks 追踪

### 概念说明

Active Ranks（活跃 rank）是 Mooncake PG 实现容错的核心机制。每个 rank 维护一个 `activeRanks` 布尔掩码（以 `torch.int32` 张量形式暴露），标识哪些 rank 槽位当前参与通信。

当 rank 失效时，其对应的 `activeRanks` 条目被置为 0，collective 操作会跳过该 rank；当恢复协议激活新 rank 时，对应条目置为 1，重新加入通信。

### 伪代码或流程

```
# active_ranks 的语义和操作

# 初始化（world_size=4, max_world_size=8）
active_ranks = torch.tensor([1, 1, 1, 1, 0, 0, 0, 0], dtype=torch.int32, device="cuda")

# 正常 collective：仅对 active_ranks[i]==1 的 rank 执行通信
all_reduce(tensor):
    for i in range(size):
        if active_ranks[i] == 1:
            participate_in_collective(i)
        else:
            skip_rank(i)

# rank 2 失效后的状态更新
active_ranks[2] = 0  # → [1, 1, 0, 1, 0, 0, 0, 0]
# 后续 collective 自动跳过 rank 2

# 恢复 rank 2（替换进程加入）
recover_ranks(ranks=[2])
active_ranks[2] = 1  # → [1, 1, 1, 1, 0, 0, 0, 0]
```

### 原理分析

**host vs device 掩码**
Mooncake PG 维护两份 active-ranks 掩码：
- `activeRanks`（host）：CPU 侧，供 Python API 和控制流查询
- `activeRanksDevice`（device）：GPU/CUDA 设备侧，供 collective kernel 直接读取

这种双缓冲设计确保：
- GPU kernel 执行 collective 时无需 host 同步，直接读取 device 掩码
- Python 层查询 `pg.get_active_ranks()` 返回 host 掩码副本

**张量语义**
`activeRanks` 张量的类型必须为 `torch.int32`（而非 `bool`），原因是：
- CUDA kernel 对 int32 的原子操作支持更好
- 与 PyTorch 其他分布式后端的兼容性
- 张量可通过 `activeRanksTensor` 直接暴露给 Python 用户代码

**更新传播路径**
```
recover_ranks() 调用
    ↓
syncActiveRanksTensor() 同步 host 掩码
    ↓
更新 device 掩码（如需 GPU kernel 可见）
    ↓
后继 collective 读到新掩码
```

**溢出路径处理**
当 `activeSize < size`（有预留容量）时，部分 collective 操作需正确处理缓冲区大小：
- `_allgather_base`：输出缓冲区按 `activeSize` 分配，避免访问未激活槽位
- `_reduce_scatter_base`：输入缓冲区跳过 inactive ranks

### 代码实践

**TransferGroupMeta 中的 activeRanks 字段**
[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/include/mooncake_worker.cuh#L39-L58](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/include/mooncake_worker.cuh#L39-L58)

此结构体持有进程组的共享元数据，包括：
- `activeSize`：可见大小（dist.get_world_size() 返回值）
- `activeRanks`（host 指针）和 `activeRanksDevice`（device 指针）
- `activeRanksTensor`：PyTorch 张量，暴露给 Python

**弹性测试中的 activeRanks 验证**
[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/tests/test_pg_elastic.py#L458-L468](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/tests/test_pg_elastic.py#L458-L468)

此测试验证了扩展场景下 activeRanks 的正确性：初始 3 个 rank 激活，预留第 4 个槽位，扩展后全部激活。

**getActiveRanksTensor 暴露**
[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/include/mooncake_backend.h#L201](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/include/mooncake_backend.h#L201)

`getActiveRanksTensor()` 返回内部的 `activeRanksTensor`，供用户代码或 EP buffer 查询当前 rank 健康状态。

### 练习题

1. 为什么 `_allgather_base` 需要特殊处理 `activeSize < size` 的情况，而 `allreduce` 不需要？

2. 如果用户代码直接修改 `activeRanksTensor` 的内容（而非通过 `recover_ranks`），会发生什么？

3. host 掩码和 device 掩码如何保证一致性？在多线程环境下是否有竞态风险？

### 答案

1. `_allgather_base` 输出缓冲区大小由 `activeSize` 决定，若错误地按 `size` 访问会导致越界；`allreduce` 是就地操作，只需在 kernel 中跳过 inactive ranks，无需调整缓冲区大小。

2. 未定义行为。后端可能在任意时刻同步 host 和 device 掩码，直接修改可能导致状态不一致，或被后继同步覆盖。

3. `syncActiveRanksTensor()` 是同步点，通常在 `recover_ranks()` 等关键调用后执行，持有内部锁保护。多线程环境下应避免并发调用修改掩码的 API。

## 4. PG-EP 协同

### 概念说明

Mooncake PG 和 EP buffer 协同实现容错 MoE 推理。EP 负责专家并行通信（dispatch/combine），PG 负责底层集合通信和 rank 健康状态。当 PG 恢复 rank 后，EP 必须刷新其 peer 元数据（RDMA MR、QP、IPC handle）才能与恢复后的 rank 正常通信。

### 伪代码或流程

```
# PG-EP 协同的容错 MoE 推理流程

# 初始化阶段
pg_options = pg.MooncakeBackendOptions(
    activeRanks=torch.tensor([1, 1, 1, 0], dtype=torch.int32, device="cuda"),
    maxWorldSize=4
)
dist.init_process_group(backend="mooncake", pg_options=pg_options)
buffer = Buffer(group=group, num_ep_buffer_bytes=1<<28)
buffer.connect()  # EP 通过 PG 交换元数据

# 正常推理阶段
def inference_step(x, topk_idx):
    buffer.dispatch(x, topk_idx, active_ranks=pg.get_active_ranks(backend))
    expert_output = run_local_experts()
    output = buffer.combine(expert_output, active_ranks=pg.get_active_ranks(backend))
    return output

# rank 2 失效后的恢复
pg.recover_ranks(backend, ranks=[2])  # PG 层激活 rank 2
buffer.update_ep_member()           # EP 层刷新 peer 元数据
# 后续 dispatch/combine 可与恢复的 rank 2 通信
```

### 原理分析

**EP 依赖 PG 的资源**
- EP 通过 PG 的 `dist.all_gather` 和 `dist.all_to_all` 交换 RDMA 元数据
- EP 读取 PG 的 `activeRanks` 状态，在 dispatch/combine kernel 中跳过失效 rank
- EP 的 fallback 路径使用 PG 的 collective 实现

**恢复后的元数据刷新**
`Buffer.update_ep_member()` 执行以下操作：
1. 重新执行元数据交换（all_gather/all_to_all）
2. 重建与新 rank 的 RDMA QP
3. 刷新本地 peer 映射表
4. 更新 timeout-aware kernel 使用的 `active_ranks` 参数

**故障感知机制**
EP 的 dispatch/combine kernel 通过 `timeout_us` 参数检测故障：
- 若源 rank 在超时内未发送完成，kernel 认定该 rank 失效
- 返回部分结果，上层可决定触发恢复
- 此超时机制与 PG 的 active-ranks 掩码配合，形成双重保护

**Split-Ranks 模式**
在多子组场景下（如不同专家组分配到不同 rank 子集），PG 和 EP 需协调子组恢复：
- 每个子组独立调用 `recover_ranks`
- EP buffer 为每个子组独立更新元数据
- 子组的 `backendIndex` 和 store 前缀必须对齐

### 代码实践

**EP buffer 从 PG 构造**
[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/mooncake-ep.md#L44-L59](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/mooncake-ep.md#L44-L59)

Mooncake EP 必须从 Mooncake PG 进程组构造，通过 PG 交换 RDMA 元数据并使用 PG 的 active-rank 状态。

**EP 恢复后调用 update_ep_member**
[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/mooncake-ep.md#L56-L59](https://github.com/kvcake-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/mooncake-ep.md#L56-L59)

PG 恢复改变成员后，EP 必须调用此方法刷新 peer 元数据和 QP，才能与恢复的 rank 通信。

**子组弹性测试**
[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/tests/test_pg_elastic.py#L165-L392](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-pg/tests/test_pg_elastic.py#L165-L392)

此测试展示了 split-ranks 模式下的弹性扩展：不同子组（group_a, group_b, group_c）独立恢复，rank 0/1 为初始成员，rank 2/3 分别加入对应子组。

**EP dispatch 的 active_ranks 参数**
[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/mooncake-ep.md#L141-L146](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/mooncake-ep.md#L141-L146)

EP 的 `dispatch()` 操作接收 `active_ranks` 张量参数，用于在 kernel 中跳过失效 rank，实现容错通信。

### 练习题

1. 如果 EP buffer 在 PG 恢复后忘记调用 `update_ep_member()`，会发生什么？

2. 为什么 EP 需要独立的 `timeout_us` 参数，而不是完全依赖 PG 的 active-ranks 掩码？

3. 在 split-ranks 模式下，如果 group_a 和 group_b 同时恢复不同 rank，是否会有竞态条件？

### 答案

1. EP 的 peer 元数据（RDMA QP、IPC handle）仍为旧状态，尝试与恢复的 rank 通信时会失败或超时，可能导致 kernel 卡死或返回错误结果。

2. EP 的 kernel 级超时是快速故障检测机制（微秒级），而 PG 的 active-ranks 掩码是高层状态管理（秒级）。双重保护确保即使 PG 尚未标记 rank 失效，EP 也能通过超时快速感知并返回部分结果。

3. 无竞态，只要满足两个条件：(1) 每个子组的 `backendIndex` 和 store 前缀对齐（通过 `new_group` 调用顺序保证）；(2) 每个子组独立调用 `recover_ranks` 和 `update_ep_member`，不跨子组依赖。子组间通过 PG 的全局 active-ranks 掩码协调，不会互相干扰。

---

**总结**：本讲义覆盖了 Mooncake PG 弹性进程组的四个核心模块——PyTorch 后端集成（通过 MooncakeBackendOptions 扩展进程组、P2P Shim 适配）、弹性恢复协议（get_peer_state、recover_ranks、join_group 三阶段）、Active ranks 追踪（host/device 双掩码、溢出路径处理）、PG-EP 协同（容错 MoE 推理、元数据刷新）。这些机制共同实现了长运行推理服务在部分 rank 失效后的无缝恢复。
