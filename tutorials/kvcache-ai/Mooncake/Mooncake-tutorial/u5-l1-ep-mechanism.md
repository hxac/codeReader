# Mooncake EP 机制与编程模型

本讲义讲解 Mooncake EP（Expert Parallel）如何基于 Mooncake Transport 实现 MoE（Mixture of Experts）推理的 token dispatch 与 combine 操作，剖析 dispatch/combine 三阶段数据流、Buffer 布局、IBGDA/RDMA fast path 与 Python fallback 的执行模式选择，以及如何通过 active_ranks tensor 感知 rank 健康状态。

---

## 最小模块 1：EP 编程模型

### 概念说明

MoE 推理的核心挑战是如何高效地将 token 分发（dispatch）到对应的 expert，并将 expert 输出聚合（combine）回 token。Mooncake EP 是一个低延迟的 expert-parallel 通信运行时，专门解决以下问题：

1. **低延迟通信**：在分布式多 rank 环境下，token 需要跨网络传输到对应的 expert owner rank
2. **与 DeepEP 兼容**：保持与 DeepEP 低延迟模式相似的 Python 编程模型
3. **Fast path 利用**：优先使用 IBGDA/RDMA 或 NVLink P2P，降级时才用 Python fallback
4. **Rank 健康感知**：检测并处理 rank 失败，避免无限等待

EP 将 MoE 推理划分为三个阶段：
- **Dispatch**：各 rank 将本地 token 发送到对应的 expert owner rank
- **Expert Compute**：各 rank 在本地 expert 上计算
- **Combine**：expert 输出被路由回原始 token owner 并按 routing weights 聚合

### 伪代码或流程

```python
# 初始化：从 Process Group 创建 Buffer
buffer = Buffer(group, num_ep_buffer_bytes)

# Dispatch：发送 token 到 expert owner
recv_x, recv_count, handle, event, hook = buffer.dispatch(
    x,                    # [num_tokens, hidden] 本地 token
    topk_idx,            # [num_tokens, top_k] 选择的 expert ID
    active_ranks,         # [num_ranks] rank 健康状态
    num_max_dispatch_tokens_per_rank,  # 每个 rank 的接收容量
    num_experts,          # 全局 expert 数量
    timeout_us,           # 超时检测（微秒）
)

# Expert Compute：在本地 expert 上计算
expert_out = run_local_experts(recv_x)

# Combine：expert 输出路由回 token owner 并聚合
combined_x, event, hook = buffer.combine(
    expert_out,           # 本地 expert 输出
    topk_idx,             # dispatch 时用的 expert ID
    topk_weights,         # routing weights
    active_ranks,         # rank 健康状态
    handle=handle,        # dispatch 返回的 handle
    timeout_us=timeout_us,
)
```

### 原理分析

#### 三阶段数据流

1. **Dispatch 阶段**
   - 每个 rank 拥有 `num_tokens` 个本地 token
   - 根据路由决策（`topk_idx`），token 需要发送到不同的 expert owner rank
   - Expert owner rank 计算公式：`expert_owner_rank = expert_id % num_ranks`
   - 接收端按 local expert major layout 打包收到的 token

2. **Expert Compute 阶段**
   - 每个 rank 只运行其本地 expert：`local_experts = num_experts / num_ranks`
   - 输入是 dispatch 阶段打包好的 token
   - 输出将用于 combine 阶段

3. **Combine 阶段**
   - Expert 输出需要路由回原始 token owner
   - 根据 routing weights（`topk_weights`）加权聚合多个 expert 输出
   - 对于 top-k > 1，一个 token 的最终输出是多个 expert 输出的加权和

#### Buffer 双缓冲机制

EP 使用双缓冲（BufferPair）实现 dispatch/combine 流水线重叠：

```cpp
struct BufferPair {
    BufferLayout buffers[2];  // 双缓冲
    // buffers[0] 和 buffers[1] 交替使用
};

struct BufferLayout {
    int* rdma_send_signal_buffer;     // RDMA 发送信号
    int* rdma_recv_signal_buffer;     // RDMA 接收信号
    void* rdma_send_data_buffer;      // RDMA 发送数据
    void* rdma_recv_data_buffer;      // RDMA 接收数据
};
```

每个 buffer 包含 4 部分：
- Signal buffer（发送/接收）：用于 RDMA 同步信号
- Data buffer（发送/接收）：实际传输的 token 数据

总大小计算公式：
```
signaling_buffer_bytes = num_experts × sizeof(int)
send_recv_buffer_bytes = num_experts × num_max_dispatch_tokens_per_rank × (2 × sizeof(int4) + hidden × sizeof(nv_bfloat16))
```

### 代码实践

#### Python API 定义

Mooncake EP 的用户入口是 `Buffer` 类，定义在 [mooncake/mooncake_ep_buffer.py](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_ep_buffer.py#L64-L84)：

```python
class Buffer:
    def __init__(self, group: dist.ProcessGroup, num_ep_buffer_bytes: int = 0):
        from mooncake import ep

        # 从 Process Group 提取元数据
        self.rank = group.rank()
        self.group_size = group.size()
        self.group = group

        # 创建 C++ runtime（自动 NIC 探测）
        self.runtime = ep.Buffer(self.rank, self.group_size, num_ep_buffer_bytes)

        # 初始化 fallback 标志和缓冲
        self._use_fallback = bool(self.runtime.ibgda_disabled())
        self._fallback_next_combine_buffer = None

        # 连接：交换元数据，建立 QP/IPC
        self.connect()
```

关键点：
- `ep.Buffer` 是 C++ 实现的 native runtime
- `ibgda_disabled()` 检测是否禁用 IBGDA（如 NIC 不可用）
- `connect()` 方法负责 peer 元数据交换

#### Buffer 容量计算

Buffer 容量需要在创建时指定，可通过静态方法计算，见 [mooncake/mooncake_ep_buffer.py#L204-L215](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_ep_buffer.py#L204-L215)：

```python
@staticmethod
def get_ep_buffer_size_hint(
    num_max_dispatch_tokens_per_rank: int,
    hidden: int,
    num_ranks: int,
    num_experts: int,
) -> int:
    from mooncake.ep import get_ep_buffer_size_hint

    return get_ep_buffer_size_hint(
        num_max_dispatch_tokens_per_rank, hidden, num_ranks, num_experts
    )
```

**重要**：`num_max_dispatch_tokens_per_rank` 应按峰值需求配置，而非平均值。过小会导致溢出，过大会浪费内存。

### 练习题

1. **基础题**：在一个 8 rank 系统，全局 64 个 expert，每个 rank 有 1000 个 token，hidden=2048，top_k=4。计算每个 rank 最多需要接收多少个 token？

2. **进阶题**：如果使用 FP8 dispatch，为什么 `send_recv_buffer_bytes` 计算中要额外加 `2 × sizeof(int4)`？这些额外空间存储什么？

3. **设计题**：双缓冲的目的是什么？在什么场景下单缓冲够用？

4. **故障题**：如果 `num_max_dispatch_tokens_per_rank` 设置过小，运行时会发生什么？如何从错误日志中诊断？

### 答案

1. **基础题答案**：每个 rank 最多接收 `1000 × 4 × (1/8) = 500` 个 token（假设均匀分布）。但实际上需要考虑最坏情况：所有 1000 个 token 都选择同一个 rank 的 expert，所以 `num_max_dispatch_tokens_per_rank` 应至少设为 1000。

2. **进阶题答案**：额外的 `2 × sizeof(int4)` 存储元数据：
   - `int4`（4 bytes）存储 source rank 信息，用于 combine 时路由回 token owner
   - 另一个 `int4` 可能用于 padding 或其他元数据（如 token 索引）
   这些元数据使 combine 阶段能正确重建 token 到 expert 的映射。

3. **设计题答案**：双缓冲目的是流水线重叠：
   - Buffer[0] 正在进行 dispatch/combine 的 RDMA 传输
   - Buffer[1] 可以同时准备下一轮的数据或接收信号
   单缓冲在异步重叠场景（如 CUDA graph）下会降低吞吐。在完全同步、无重叠的简单场景下，单缓冲可能够用。

4. **故障题答案**：如果 `num_max_dispatch_tokens_per_rank` 过小：
   - 某些 rank 接收的 token 超过容量，导致 buffer overflow
   - 可能触发 CUDA illegal memory access 或 assert 失败
   - 错误日志可能显示 "dispatch overflow" 或具体的 count 超限
   - 诊断方法：检查 `recv_count` 中每个 local expert 的实际接收数量，是否超过 `num_max_dispatch_tokens_per_rank`

---

## 最小模块 2：Dispatch/Combine

### 概念说明

Dispatch 和 Combine 是 EP 的两个核心操作，分别负责：
- **Dispatch**：将本地 token 发送到 expert owner rank，并接收需要本地 expert 处理的远程 token
- **Combine**：将本地 expert 输出发送回 token owner，并聚合所有 expert 对本地 token 的贡献

这两个操作都是 all-to-all 通信模式的特化版本：
- 不是所有 rank 之间全连接，而是根据 expert ownership 建立 sparse 连接
- 需要处理可变长度的 token 数量（每个 rank 的 token 数可能不同）
- 需要维护元数据（source info、layout）用于反向路由

### 伪代码或流程

#### Dispatch 流程

```python
def dispatch(x, topk_idx, active_ranks, num_max_dispatch_tokens_per_rank, num_experts, timeout_us):
    # 1. 对每个本地 token，确定其 expert owner rank
    for each token in x:
        for each expert_id in topk_idx[token]:
            owner_rank = expert_id % num_ranks
            prepare_to_send(token, owner_rank, expert_id)

    # 2. 发送 token 到对应 owner rank（通过 RDMA 或 P2P）
    send_tokens_to_owners()

    # 3. 接收发送到本地 expert 的 token，按 local expert major 打包
    recv_x = receive_and_pack_tokens_by_local_expert()

    # 4. 记录元数据：每个接收 token 的 source rank 和位置
    src_info = record_source_information()
    layout_range = record_layout_per_expert_per_rank()

    # 5. 返回 packed data 和 handle
    handle = (src_info, layout_range, num_max_dispatch_tokens_per_rank, hidden, num_experts)
    return recv_x, recv_count, handle
```

#### Combine 流程

```python
def combine(expert_out, topk_idx, topk_weights, active_ranks, handle, timeout_us):
    src_info, layout_range, num_max_dispatch_tokens_per_rank, hidden, num_experts = handle

    # 1. 根据元数据，将 expert_out 发送回对应的 token owner rank
    send_expert_outputs_back_to_owners(expert_out, src_info, layout_range)

    # 2. 接收属于本地 token 的 expert 输出
    recv_expert_out = receive_outputs_for_local_tokens()

    # 3. 按 topk_weights 加权聚合
    combined = weighted_reduce(recv_expert_out, topk_weights)

    return combined
```

### 原理分析

#### Dispatch 数据布局

Dispatch 的输出是 **local-expert-major layout**：

```
recv_x shape: [num_local_experts, num_max_dispatch_tokens, hidden]
```

- 第一维：本地 expert（每个 rank 负责 `num_experts / num_ranks` 个 expert）
- 第二维：打包到该 expert 的 token（最多 `num_max_dispatch_tokens`）
- 第三维：hidden 维度

例如，8 ranks，64 experts，每个 rank 负责专家 0-7（rank 0）、8-15（rank 1）...以此类推。Rank 0 的 `recv_x[0]` 包含所有路由到 expert 0 的 token，`recv_x[1]` 包含所有路由到 expert 1 的 token。

#### 元数据 handle 的结构

Dispatch 返回的 handle 包含 combine 所需的元数据，定义在 [mooncake/mooncake_ep_buffer.py#L280-L286](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_ep_buffer.py#L280-L286)：

```python
handle = (
    packed_recv_src_info,        # [num_local_experts, num_max_dispatch_tokens] 源 token 索引
    packed_recv_layout_range,    # [num_local_experts, num_ranks] 每个 expert 从每个 rank 接收的范围
    num_max_dispatch_tokens_per_rank,
    x.size(1),                   # hidden
    num_experts,
)
```

`layout_range` 使用 packed 64-bit 整数编码 `(begin, count)` 对：
```
layout_range[le, src_rank] = (begin << 32) | count
```

解码时：
```python
entry = layout_range[le, src_rank]
begin = (entry >> 32) & 0xFFFFFFFF
count = entry & 0xFFFFFFFF
```

这种编码使得 combine 可以快速定位每个 expert 从每个 source rank 接收的 token 数据范围。

#### Combine 的加权聚合

Combine 需要处理 top-k > 1 的情况，即一个 token 的最终输出是多个 expert 输出的加权和：

\[
\text{output}_i = \sum_{j=1}^{k} w_{i,j} \cdot \text{expert}_{\text{topk_idx}[i,j]}(\text{token}_i)
\]

其中 \(w_{i,j}\) 是 `topk_weights[i,j]`，expert 输出通过 combine 的 all-to-all 通信获取。

### 代码实践

#### Dispatch 实现（Python wrapper）

Dispatch 的 Python 入口在 [mooncake/mooncake_ep_buffer.py#L218-L302](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_ep_buffer.py#L218-L302)：

```python
def dispatch(
    self,
    x: torch.Tensor,                # [num_tokens, hidden]
    topk_idx: torch.Tensor,          # [num_tokens, top_k]
    active_ranks: torch.Tensor,      # [num_ranks]
    num_max_dispatch_tokens_per_rank: int,
    num_experts: int,
    timeout_us: int,
    use_fp8: bool = True,
    async_finish: bool = False,
    return_recv_hook: bool = False,
) -> Tuple[...]:
    if self._use_fallback:
        # Python fallback 路径
        (packed_recv_x, packed_recv_x_scales, packed_recv_count,
         packed_recv_src_info, packed_recv_layout_range, event, hook) = \
            self._fallback_dispatch(...)
    else:
        # Fast path：C++ native runtime
        (packed_recv_x, packed_recv_x_scales, packed_recv_count,
         packed_recv_src_info, packed_recv_layout_range, event, hook) = \
            self.runtime.dispatch(...)

    # 构造 handle 供 combine 使用
    handle = (
        packed_recv_src_info,
        packed_recv_layout_range,
        num_max_dispatch_tokens_per_rank,
        x.size(1),
        num_experts,
    )

    return (
        (packed_recv_x, packed_recv_x_scales) if use_fp8 else packed_recv_x,
        packed_recv_count,
        handle,
        EventOverlap(event, ...),
        hook,
    )
```

关键点：
- 根据 `_use_fallback` 标志选择 fast path 或 fallback
- 返回的 handle 包含 combine 所需的元数据
- `EventOverlap` 封装了 CUDA 事件和 tensor 记录，用于流同步

#### Fallback Dispatch 实现

Fallback dispatch 使用 PyTorch collectives 实现，见 [mooncake/mooncake_ep_buffer.py#L426-L644](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_ep_buffer.py#L426-L644)：

```python
def _fallback_dispatch(self, x, topk_idx, ...):
    # 1. 收集各 rank 的 token 数量（处理可变长度）
    num_tokens_tensor = torch.tensor([num_tokens], ...)
    num_tokens_list = [torch.empty(1, ...) for _ in range(num_ranks)]
    dist.all_gather(num_tokens_list, num_tokens_tensor, group=self.group)

    # 2. Pad 到最大长度（all_gather 要求相同 shape）
    max_num_tokens = max(num_tokens_per_rank)
    if num_tokens < max_num_tokens:
        x_padded = torch.cat([x, torch.zeros((pad_size, hidden), ...)], dim=0)
        topk_padded = torch.cat([topk_idx, torch.full((pad_size, k), -1, ...)], dim=0)

    # 3. All-gather 所有 rank 的输入
    all_x = torch.empty((num_ranks, max_num_tokens, hidden), ...)
    dist.all_gather_into_tensor(all_x, x_padded, group=self.group)
    all_topk = torch.empty((num_ranks, max_num_tokens, k), ...)
    dist.all_gather_into_tensor(all_topk, topk_padded, group=self.group)

    # 4. 为每个本地 expert 收集 token
    for le in range(num_local_experts):
        expert_id = self.rank * num_local_experts + le
        for src_rank in range(num_ranks):
            # 找到从 src_rank 发送到这个 expert 的 token
            src_topk = all_topk[src_rank, :src_num_tokens]
            pos = (src_topk == expert_id).any(dim=1).nonzero(as_tuple=False).view(-1)
            tokens_per_rank_tensors.append(pos)

        # 5. 构建 ordered list 并记录 layout_range
        layout_range[le, src_rank] = (begin << 32) | count

        # 6. 提取实际数据并可选 FP8 量化
        gathered = all_x[ordered_src_ranks[:num_valid], ordered_token_indices[:num_valid]]
        if use_fp8:
            fp8, scales = self._fp8_cast(gathered)
            recv_x_list.append(fp8)
        else:
            recv_x_list.append(gathered)

    # 7. Stack 成 local-expert-major layout
    packed_recv_x = torch.stack(recv_x_list, dim=0)

    return packed_recv_x, packed_recv_x_scales, recv_count, src_info, layout_range, event, hook
```

#### Combine 实现

Combine 的 Python 入口在 [mooncake/mooncake_ep_buffer.py#L305-L373](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_ep_buffer.py#L305-L373)：

```python
def combine(
    self,
    x: torch.Tensor,                # expert 输出
    topk_idx: torch.Tensor,
    topk_weights: torch.Tensor,
    active_ranks: torch.Tensor,
    timeout_us: int,
    handle: tuple,                   # dispatch 返回的 handle
    zero_copy: bool = False,
    async_finish: bool = False,
    return_recv_hook: bool = False,
    out: Optional[torch.Tensor] = None,
) -> Tuple[torch.Tensor, EventOverlap, Callable]:
    src_info, layout_range, num_max_dispatch_tokens_per_rank, hidden, num_experts = handle

    if self._use_fallback:
        combined_x, event, hook = self._fallback_combine(...)
    else:
        combined_x, event, hook = self.runtime.combine(...)

    return combined_x, EventOverlap(event, ...), hook
```

#### Zero-Copy Combine

Zero-copy 模式允许直接写内部 buffer，避免额外拷贝，通过 `get_next_combine_buffer()` 实现，见 [mooncake/mooncake_ep_buffer.py#L375-L405](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_ep_buffer.py#L375-L405)：

```python
def get_next_combine_buffer(self, handle: object):
    if self._use_fallback:
        # Fallback 路径：预分配 buffer
        self._fallback_next_combine_buffer = torch.empty(
            (num_experts // self.group_size,
             num_max_dispatch_tokens_per_rank * self.group_size,
             hidden),
            dtype=torch.bfloat16, device="cuda",
        )
        return self._fallback_next_combine_buffer
    else:
        # Fast path：直接获取 native runtime 的 buffer
        return self.runtime.get_next_combine_buffer(
            num_max_dispatch_tokens_per_rank, hidden, num_experts
        )
```

使用方式：
```python
# 传统方式（有拷贝）
expert_out = compute_expert(recv_x)
combined = buffer.combine(expert_out, ...)

# Zero-copy 方式（无拷贝）
combine_buf = buffer.get_next_combine_buffer(handle)
compute_expert_directly_into_buffer(combine_buf, recv_x)
combined = buffer.combine(combine_buf, ..., zero_copy=True)
```

### 练习题

1. **基础题**：在一个 4 rank 系统，16 experts，rank 0 负责专家 0-3，rank 1 负责专家 4-7，以此类推。Token A 的 topk_idx=[5, 9]，这两个 expert 分别由哪个 rank 负责？

2. **进阶题**：为什么 dispatch 输出是 local-expert-major layout，而不是 token-major layout？这种布局有什么优势？

3. **设计题**：`layout_range` 为什么用 packed 64-bit 整数存储 `(begin, count)`，而不是两个独立的 32-bit 数组？这样编码有什么好处？

4. **故障题**：如果 dispatch 和 combine 的 `num_max_dispatch_tokens_per_rank` 不一致，会发生什么？

### 答案

1. **基础题答案**：
   - Expert 5：`5 % 4 = 1`，由 rank 1 负责
   - Expert 9：`9 % 4 = 1`，由 rank 1 负责
   所以 token A 的两个 expert 都在 rank 1。

2. **进阶题答案**：Local-expert-major layout 的优势：
   - Expert compute 可以直接遍历第一维（local experts），无需重排
   - 每个 expert 的 token 连续存储，cache 友好
   - Combine 时可以按 expert 批量发送，减少通信次数
   Token-major layout 会导致每个 expert 的 token 分散存储，需要 gather 操作才能计算。

3. **设计题答案**：Packed 64-bit 编码的优势：
   - **内存紧凑**：一半的内存占用（一个 64-bit vs 两个 32-bit）
   - **Cache 友好**：减少内存访问次数
   - **原子操作**：某些架构支持 64-bit 原子读写，简化并发控制
   - **CUDA 实现**：CUDA kernel 内用 64-bit 整数解码效率高（一次移位和掩码操作）

4. **故障题答案**：如果不一致：
   - Dispatch 用较小的值，combine 用较大的值：combine 可能越界访问
   - Dispatch 用较大的值，combine 用较小的值：combine 可能丢失数据（部分 token 未聚合）
   - **最佳实践**：dispatch 和 combine 应使用相同的 `num_max_dispatch_tokens_per_rank` 值，从同一个配置或 handle 中获取。

---

## 最小模块 3：Fast path 选择

### 概念说明

Mooncake EP 支持三种执行模式，按性能从高到低：

1. **IBGDA/RDMA fast path**：跨节点 GPU 直接内存访问，通过 InfiniBand/RoCE RDMA
2. **P2P/IPC fast path**：节点内 peer-to-peer 访问，通过 NVLink/PCIe
3. **Python fallback**：基于 PyTorch collectives 的降级实现

EP 在初始化时自动检测并选择最佳路径，优先使用 fast path，fast path 不可用时才降级到 fallback。这种分层设计确保：
- 在支持 RDMA/P2P 的环境中达到最低延迟
- 在不支持的环境中仍能正确运行（功能降级，性能降低）

### 伪代码或流程

```python
def connect():
    # 1. 尝试 IBGDA/RDMA fast path
    if not ibgda_disabled:
        try:
            # 交换 RDMA 元数据：MR 地址、rkey、QPN、LID、GID
            exchange_rdma_metadata()
            # 连接 QPs
            sync_ibgda_peers(raddrs, rkeys, qpns, lids, gids, active_ranks_mask)
            fast_path_ready = True
        except Exception as e:
            fast_path_ready = False
            ibgda_disabled = True

    # 2. 尝试 P2P/IPC fast path
    try:
        # 交换 IPC handles
        exchange_ipc_handles()
        sync_nvlink_ipc_handles(remote_handles, active_ranks_mask)
        if p2p_all_peers_accessible():
            fast_path_ready = True
    except Exception as e:
        fast_path_ready = False

    # 3. 判断是否可用 fast path
    if fast_path_ready:
        _use_fallback = False
    else:
        _use_fallback = True
        warnings.warn("IBGDA and P2P both unavailable, using fallback")
```

### 原理分析

#### Fast Path 判定逻辑

Native runtime 的 `use_fast_path()` 定义在 [mooncake-ep/include/mooncake_ep_buffer.h#L134-L142](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-ep/include/mooncake_ep_buffer.h#L134-L142)：

```cpp
bool use_fast_path() {
    if (!ibgda_disabled_) return true;  // IBGDA 可用 → fast path
    bool p2p_all = p2p_transport_ && p2p_transport_->allPeersAccessible();
    if (!p2p_all) {
        LOG(WARNING) << "IBGDA unavailable and P2P not fully accessible. "
                     << "Using fallback (degraded performance).";
    }
    return p2p_all;  // 所有 peer 可通过 P2P 访问 → fast path
}
```

判定逻辑：
1. **IBGDA 优先**：如果 IBGDA 未禁用，直接使用 fast path
2. **P2P 降级**：IBGDA 不可用时，检查所有 peer 是否 P2P 可访问
3. **Fallback**：两者都不可用时，使用 Python fallback

#### 为什么需要两层 fast path？

- **IBGDA/RDMA**：跨节点通信，通过 InfiniBand/RoCE 网络，需要：
  - RDMA-capable NIC（Mellanox/ConnectX）
  - 正确配置的 QP（Queue Pair）
  - GDR（GPUDirect RDMA）支持

- **P2P/IPC**：节点内通信，通过 NVLink/PCIe，需要：
  - Peer accessibility（`cudaDeviceCanAccessPeer`）
  - CUDA IPC handle 交换

两层设计的原因：
- 某些环境只有节点内 P2P，没有跨节点 RDMA（如单机多 GPU）
- 某些环境 RDMA 设置失败，但 P2P 仍可用（如 IB 网络配置问题）
- 提供更细粒度的降级策略

#### 元数据交换

Connect 阶段交换两类元数据：

**IBGDA 元数据**（[mooncake/mooncake_ep_buffer.py#L88-L164](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_ep_buffer.py#L88-L164)）：
```python
# 1. MR 地址和 rkey
raddr, rkey = self.runtime.get_mr_info()
raddrs = dist.all_gather(raddr, ...)
rkeys = dist.all_gather(rkey, ...)

# 2. QP numbers（all-to-all 交换）
local_qpns = self.runtime.get_local_qpns()
remote_qpns = dist.all_to_all(local_qpns, ...)

# 3. LID 和 GID
local_lids = self.runtime.get_local_lids()
remote_lids = dist.all_to_all(local_lids, ...)
subnet_prefix, interface_id = self.runtime.get_gid()
subnet_prefixes = dist.all_gather(subnet_prefix, ...)
interface_ids = dist.all_gather(interface_id, ...)

# 4. Active rank mask
active_ranks_mask = get_active_ranks(self.backend).tolist()

# 5. 同步 IBGDA peers
self.runtime.sync_ibgda_peers(raddrs, rkeys, peer_qpns, peer_lids,
                              subnet_prefixes, interface_ids, active_ranks_mask)
```

**IPC 元数据**（[mooncake/mooncake_ep_buffer.py#L166-L198](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_ep_buffer.py#L166-L198)）：
```python
# 1. 本地 IPC handle
local_handle_ints = self.runtime.get_ipc_handle()
local_handle_tensor = torch.tensor(local_handle_ints, ...)

# 2. All-gather 所有 rank 的 handles
handles = [torch.empty(...) for _ in range(self.group_size)]
dist.all_gather(handles, local_handle_tensor, self.group)
remote_handles = [h.tolist() for h in handles]

# 3. 同步 NVLink IPC handles
self.runtime.sync_nvlink_ipc_handles(remote_handles, active_ranks_mask)

# 4. 重新评估 fast path
use_fast_path = bool(self.runtime.use_fast_path())
self._use_fallback = not use_fast_path
```

#### Fallback 实现原理

Fallback 使用 PyTorch 的 `all_gather` 和 `all_reduce` collective 操作，模拟 all-to-all 通信：

**Dispatch fallback**：
- `all_gather` 所有 rank 的输入
- 在本地重建每个 expert 的 token 列表
- 手动构建 `src_info` 和 `layout_range` 元数据

**Combine fallback**：
- `all_gather` 所有 rank 的路由信息（`topk_idx`、`topk_weights`）
- 根据元数据将 expert 输出分发到对应 rank
- `all_reduce` 聚合所有 expert 对本地 token 的贡献

Fallback 的性能劣势：
- 多次 collective（而非一次 all-to-all）
- 中间 buffer 需要显式分配和管理
- 无法利用 GPUDirect/RDMA 的零拷贝优势

### 代码实践

#### Connect 方法

`Buffer.connect()` 实现元数据交换和 fast path 检测，见 [mooncake/mooncake_ep_buffer.py#L85-L199](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_ep_buffer.py#L85-L199)：

```python
def connect(self, is_update: bool = False):
    # --- IBGDA/RDMA 路径 ---
    if not self._use_fallback:
        # 交换 MR 信息
        (raddr, rkey) = self.runtime.get_mr_info()
        raddr = torch.tensor([raddr], dtype=torch.int64, device="cuda")
        raddrs = [torch.empty(1, ...) for _ in range(self.group_size)]
        dist.all_gather(raddrs, raddr, self.group)
        raddrs = torch.cat(raddrs).tolist()

        # 交换 rkey
        rkey = torch.tensor([rkey], dtype=torch.int32, device="cuda")
        rkeys = [torch.empty(1, ...) for _ in range(self.group_size)]
        dist.all_gather(rkeys, rkey, self.group)
        rkeys = torch.cat(rkeys).tolist()

        # All-to-all 交换 QP numbers
        all_to_all_size = ep.MAX_QP_COUNT // self.group_size
        if is_update:
            self.runtime.update_local_qpns()
        local_qpns = self.runtime.get_local_qpns()
        local_qpns = list(torch.unbind(torch.tensor(local_qpns, ...).view(-1, all_to_all_size)))
        remote_qpns = [torch.empty(all_to_all_size, ...) for _ in range(self.group_size)]
        dist.all_to_all(remote_qpns, local_qpns, self.group)
        peer_qpns = [remote_qpns[r].tolist() for r in range(self.group_size)]

        # 交换 LID
        local_lids = self.runtime.get_local_lids()
        local_lids = list(torch.unbind(torch.tensor(local_lids, ...).view(-1, all_to_all_size)))
        remote_lids = [torch.empty(all_to_all_size, ...) for _ in range(self.group_size)]
        dist.all_to_all(remote_lids, local_lids, self.group)
        peer_lids = [remote_lids[r].tolist() for r in range(self.group_size)]

        # 交换 GID（subnet prefix + interface ID）
        (subnet_prefix, interface_id) = self.runtime.get_gid()
        subnet_prefixes_list = [torch.empty(1, ...) for _ in range(self.group_size)]
        dist.all_gather(subnet_prefixes_list, torch.tensor([subnet_prefix], ...), self.group)
        subnet_prefixes = torch.cat(subnet_prefixes_list).tolist()

        interface_ids_list = [torch.empty(1, ...) for _ in range(self.group_size)]
        dist.all_gather(interface_ids_list, torch.tensor([interface_id], ...), self.group)
        interface_ids = torch.cat(interface_ids_list).tolist()

        # 获取 active rank mask
        active_ranks_mask = get_active_ranks(self.backend).tolist()

        # 同步 IBGDA peers
        self.runtime.sync_ibgda_peers(raddrs, rkeys, peer_qpns, peer_lids,
                                      subnet_prefixes, interface_ids, active_ranks_mask)

    # --- P2P/IPC 路径 ---
    try:
        local_handle_ints = self.runtime.get_ipc_handle()
        local_handle_tensor = torch.tensor(local_handle_ints, ...)
        handles = [torch.empty(len(local_handle_ints), ...) for _ in range(self.group_size)]
        dist.all_gather(handles, local_handle_tensor, self.group)
        remote_handles = [h.tolist() for h in handles]
        active_ranks_mask = get_active_ranks(self.backend).tolist()
        self.runtime.sync_nvlink_ipc_handles(remote_handles, active_ranks_mask)
    except Exception as e:
        warnings.warn(f"Failed to exchange IPC handles: {e}. Falling back.", RuntimeWarning)

    # --- 重新评估 fast path ---
    use_fast_path = False
    try:
        use_fast_path = bool(self.runtime.use_fast_path())
    except Exception:
        ibgda_disabled = bool(self.runtime.ibgda_disabled())
        use_fast_path = not ibgda_disabled

    self._use_fallback = not use_fast_path
```

关键点：
- `is_update=True` 时调用 `update_local_qpns()`（用于 PG recovery 后刷新）
- IBGDA 元数据交换包含 MR、QP、LID、GID 四类信息
- IPC handle 交换可能失败（如 peer 不可访问），捕获异常后降级
- 最后重新评估 `use_fast_path()`，可能因为 IPC 成功从 `True` 变 `False`

#### C++ 层的元数据访问器

C++ 层提供元数据访问器供 Python 调用，定义在 [mooncake-ep/include/mooncake_ep_buffer.h#L158-L182](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-ep/include/mooncake_ep_buffer.h#L158-L182)：

```cpp
// MR 信息：地址和 rkey
std::tuple<int64_t, int32_t> get_mr_info() {
    if (!rdma_transport_) return {0, 0};
    auto m = rdma_transport_->localMetadata();
    return {m.raddr, m.rkey};
}

// GID：subnet prefix 和 interface ID
std::tuple<int64_t, int64_t> get_gid() {
    if (!rdma_transport_) return {0, 0};
    auto m = rdma_transport_->localMetadata();
    return {m.subnet_prefix, m.interface_id};
}

// QP numbers
std::vector<int32_t> get_local_qpns() {
    if (!rdma_transport_) return {};
    return rdma_transport_->localMetadata().qpns;
}

// LID
std::vector<int32_t> get_local_lids() {
    if (!rdma_transport_) return {};
    return rdma_transport_->localMetadata().lids;
}

// IPC handle（P2P）
std::vector<int32_t> get_ipc_handle();
```

这些访问器封装了底层 `RdmaTransport` 和 `P2pTransport` 的元数据查询。

### 练习题

1. **基础题**：在一个 2 节点，每节点 4 GPU 的集群，节点内用 NVLink，节点间用 InfiniBand。IBGDA 突然不可用（如 NIC 故障），EP 会如何选择执行模式？

2. **进阶题**：为什么 IBGDA 元数据交换需要 `all_to_all`（如 QP numbers），而 MR 地址和 GID 用 `all_gather` 即可？

3. **设计题**：如果需要在单机多 GPU 环境禁用 P2P（如调试），应该怎么修改代码？

4. **故障题**：Connect 阶段 IBGDA 交换成功，但 IPC handle 交换失败，`_use_fallback` 最终是 True 还是 False？

### 答案

1. **基础题答案**：IBGDA 不可用后：
   - `ibgda_disabled_` 设为 `True`
   - `use_fast_path()` 检查 P2P 是否全可达
   - 节点内 NVLink P2P 应该可用（`p2p_transport_->allPeersAccessible()` 返回 `True`）
   - 所以 `_use_fallback = False`，仍用 fast path（只是降级到 P2P）

2. **进阶题答案**：
   - **MR 地址和 GID**：每个 rank 只需要一个全局值，`all_gather` 让所有 rank 获取其他 rank 的单一值即可
   - **QP numbers**：每个 rank 需要为每个 peer 分配专用的 QPN（all-to-all 拓扑），所以 rank 0 需要 rank 1 专用的 QPN、rank 2 专用的 QPN... 这需要 `all_to_all` 交换每个 rank 为每个其他 rank 准备的 QPN 列表

3. **设计题答案**：可以通过以下方式禁用 P2P：
   - **方法 1（运行时）**：设置环境变量强制禁用（如果实现支持）
   - **方法 2（代码）**：在 `connect()` 中，`sync_nvlink_ipc_handles()` 前直接 `return`，跳过 IPC 交换
   - **方法 3（C++）**：在 `MooncakeEpBuffer` 构造函数中不创建 `p2p_transport_`
   - **方法 4（测试）**：在测试中直接设置 `buf._use_fallback = True`

4. **故障题答案**：`_use_fallback = False`。
   - IBGDA 交换成功 → `ibgda_disabled_` 初始为 `False`
   - `use_fast_path()` 首先检查 `if (!ibgda_disabled_) return true;`
   - 所以即使 IPC 失败，只要 IBGDA 可用，fast path 仍然启用
   - IPC 失败只是警告，不会强制 fallback

---

## 最小模块 4：Rank 健康感知

### 概念说明

在分布式 MoE 推理中，rank 故障会导致：
- Dispatch 阶段：等待故障 rank 发送 token 时无限阻塞
- Combine 阶段：等待故障 rank 发送 expert 输出时无限阻塞

Mooncake EP 通过 **timeout-aware kernels** 和 **active_ranks tensor** 实现故障检测：

1. **Timeout 检测**：native kernel 在等待 source rank 数据时，如果超过 `timeout_us` 仍未收到信号，判定该 rank 故障
2. **Active_ranks 标记**：kernel 直接修改 `active_ranks[src_rank] = 0`，标记该 rank 失活
3. **跳过故障 rank**：后续通信跳过失活 rank，避免无限等待

这种机制提供了 EP 层面的故障检测，与 Mooncake Backend (PG) 的 active-rank 状态配合，实现端到端的弹性。

### 伪代码或流程

```python
def dispatch_with_timeout(x, topk_idx, active_ranks, timeout_us):
    for each local_expert le:
        for each source_rank src in range(num_ranks):
            if active_ranks[src] == 0:
                continue  # 跳过已知的故障 rank

            start_time = current_time_us()
            while not recv_signal_from(src):
                if timeout_us != -1 and (current_time_us() - start_time) > timeout_us:
                    # 超时，标记 src_rank 故障
                    active_ranks[src] = 0
                    log(f"Rank {src} timeout in dispatch, marking inactive")
                    break

            if active_ranks[src] == 1:
                receive_data_from(src, le)

    return recv_x, recv_count, active_ranks
```

### 原理分析

#### Timeout 参数语义

`timeout_us` 参数：
- `-1`：禁用超时检测，无限等待（用于已知健康的集群）
- `> 0`：超时阈值（微秒），超过后标记 rank 失活

典型值：
- 生产环境：5 秒（`5_000_000`），避免短暂延迟误判
- 测试环境：1 秒（`1_000_000`），快速模拟故障

#### Active_ranks Tensor 的传递

`active_ranks` 在 dispatch 和 combine 中都作为输入/输出参数：

**Dispatch**：
```python
active_ranks = torch.ones((num_ranks,), dtype=torch.int32)  # 初始全健康
recv_x, recv_count, handle, event, hook = buffer.dispatch(
    x, topk_idx, active_ranks,  # 输入：假设健康；输出：kernel 可能修改
    ..., timeout_us=5_000_000,
)
# dispatch 后，active_ranks 可能某些位被清零
```

**Combine**：
```python
# 传递 dispatch 后的 active_ranks（可能已有 rank 被标记故障）
combined_x, event, hook = buffer.combine(
    expert_out, topk_idx, topk_weights, active_ranks,  # 输入/输出
    ..., timeout_us=5_000_000,
)
```

#### 与 PG 层 Active-ranks 的关系

PG 层的 active-rank mask（通过 `get_active_ranks(backend)`）和 EP 层的 `active_ranks` tensor 是两个不同层次：

- **PG 层**：process group 级别的 rank 健康状态，影响所有 collective 操作
- **EP 层**：dispatch/combine 特定的故障检测结果，仅影响当前 EP 操作

关系：
- EP 可以通过 PG 的 active-rank mask 初始化 `active_ranks` tensor
- EP 检测到的故障应传播回 PG（通过 `update_ep_member()` 触发 PG recovery）
- 但两者不是自动同步的，需要集成层协调

#### 故障恢复流程

当 rank 故障被检测到后，恢复流程涉及多层（[docs/source/design/mooncake-ep.md#L195-L207](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/mooncake-ep.md#L195-L207)）：

1. **PG 层**：更新 active-rank state，停止 collectives 等待故障 rank
2. **调度层**：MoE routing 停止向不可用 expert 分配 token
3. **Recovery**：新 rank 通过 PG elastic protocol 加入
4. **EP 层**：调用 `update_ep_member()` 刷新 peer metadata 和 QPs

### 代码实践

#### Dispatch 中的 active_ranks

C++ 层的 `dispatch` 签名在 [mooncake-ep/include/mooncake_ep_buffer.h#L107-L113](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-ep/include/mooncake_ep_buffer.h#L107-L113)：

```cpp
std::tuple<torch::Tensor, std::optional<torch::Tensor>, torch::Tensor,
           torch::Tensor, torch::Tensor, std::optional<EventHandle>,
           std::optional<std::function<void()>>>
dispatch(const torch::Tensor& x,
         const torch::Tensor& topk_idx,
         torch::Tensor& active_ranks,  // 引用传递，kernel 可修改
         int num_max_dispatch_tokens_per_rank,
         int num_experts,
         int timeout_us,
         bool use_fp8,
         bool async,
         bool return_recv_hook);
```

`active_ranks` 是引用传递（`torch::Tensor&`），CUDA kernel 可以直接修改其内容。

#### Fallback 中的 active_ranks 同步

Fallback dispatch 会从 backend 同步 active-ranks 状态，见 [mooncake/mooncake_ep_buffer.py#L254-L259](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_ep_buffer.py#L254-L259)：

```python
if self._use_fallback:
    from mooncake.ep import get_active_ranks

    # 运行 fallback dispatch
    (packed_recv_x, packed_recv_x_scales, packed_recv_count,
     packed_recv_src_info, packed_recv_layout_range, event, hook) = \
        self._fallback_dispatch(...)

    # 从 backend 同步 active-ranks 到 EP 层 tensor
    backend_active_ranks = get_active_ranks(self.backend).to(
        device=active_ranks.device, dtype=active_ranks.dtype
    )
    if active_ranks.numel() == backend_active_ranks.numel():
        active_ranks.copy_(backend_active_ranks)  # 覆盖 EP 层状态
```

这确保 fallback 路径的 active_ranks 与 PG 层保持一致。

#### 测试中的故障模拟

测试中通过 `os._exit(0)` 模拟 rank 故障，见 [mooncake-ep/tests/test_ep_grid.py#L94-L96](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-ep/tests/test_ep_grid.py#L94-L96)：

```python
dist.barrier(group)

if rank == fail_rank:
    os._exit(0)  # 直接退出，模拟 rank 崩溃

# Dispatch：其他 rank 等待故障 rank，触发 timeout
recv_x, recv_count, handle, event, hook = buf.dispatch(
    x, topk_idx, active_ranks,
    ..., timeout_us=timeout_us,  # 应设置足够大的值
)

# 验证故障被检测到
if fail_rank != -1:
    assert active_ranks[fail_rank].item() == 0, \
        f"Failed rank {fail_rank} should be marked inactive"
    assert active_ranks.sum().item() == active_ranks.numel() - 1, \
        f"Expected exactly one failed rank"
```

验证逻辑：
1. 故障 rank 直接退出（`os._exit(0)`）
2. 其他 rank 在 dispatch 时等待，触发 timeout
3. Kernel 将 `active_ranks[fail_rank]` 设为 0
4. 测试验证该位被清零，且只有该位被清零

#### Update_ep_member 方法

PG recovery 后需要刷新 EP peer metadata，通过 `update_ep_member()` 实现，见 [mooncake/mooncake_ep_buffer.py#L201-L203](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/mooncake/mooncake_ep_buffer.py#L201-L203)：

```python
def update_ep_member(self):
    self.connect(True)  # is_update=True，触发 update_local_qpns()
```

调用时机：
- PG elastic 成功，新 rank 加入后
- 需要重新建立 QPs 和 peer metadata 时

### 练习题

1. **基础题**：在一个 4 rank 系统，rank 2 在 dispatch 阶段超时故障。Dispatch 完成后，`active_ranks` 的值是多少？

2. **进阶题**：为什么 `active_ranks` 在 dispatch 和 combine 中都需要传递，而不是只在 dispatch 中检测一次？

3. **设计题**：如果在 dispatch 中检测到 rank 2 故障，combine 时 rank 3 又故障，如何处理这种情况？

4. **故障题**：设置 `timeout_us=1000`（1ms），但网络延迟偶尔达到 2ms。这会导致什么问题？

### 答案

1. **基础题答案**：`active_ranks = [1, 1, 0, 1]`
   - Rank 0, 1, 3 仍健康（值为 1）
   - Rank 2 被标记故障（值为 0）

2. **进阶题答案**：
   - **Dispatch 检测**：检测发送 token 阶段的 rank 故障
   - **Combine 仍需检测**：检测发送 expert 输出阶段的 rank 故障
   - 原因：
     - Rank 可能在 dispatch 和 combine 之间故障（如 expert compute 时崩溃）
     - 即使 dispatch 健康，combine 时仍可能超时
     - 两个阶段需要独立的 timeout 保护

3. **设计题答案**：处理方案：
   - **第一次故障**（dispatch）：`active_ranks[2] = 0`，dispatch 跳过 rank 2 的数据
   - **第二次故障**（combine）：`active_ranks[3] = 0`，combine 跳过 rank 3 的数据
   - **最终状态**：`active_ranks = [1, 1, 0, 0]`（假设 rank 0, 1 健康）
   - **恢复**：需要触发 PG recovery，替换 rank 2 和 rank 3，然后调用 `update_ep_member()`

4. **故障题答案**：`timeout_us=1000`（1ms）< 偶尔延迟 2ms 会导致：
   - **误判**：正常的 rank 被错误标记为故障（false positive）
   - **数据丢失**：跳过健康 rank 的数据，导致输出错误
   - **级联故障**：多个 rank 被误判后，剩余 rank 可能过载
   - **解决**：
     - 增加 `timeout_us` 到安全值（如 5 秒）
     - 或根据实际网络 P99 延迟设置阈值

---

## 总结

本讲义覆盖了 Mooncake EP 机制的四个核心模块：

1. **EP 编程模型**：三阶段数据流（Dispatch → Expert Compute → Combine）、Buffer 双缓冲布局、从 Process Group 创建 Buffer 的初始化流程

2. **Dispatch/Combine**：local-expert-major 输出布局、handle 元数据结构、fast path 与 fallback 的两种实现路径、zero-copy combine 优化

3. **Fast path 选择**：IBGDA/RDMA、P2P/IPC、Python fallback 三层降级策略、元数据交换（MR/QP/LID/GID/IPC handles）、`use_fast_path()` 判定逻辑

4. **Rank 健康感知**：timeout-aware kernels、`active_ranks` tensor 的修改与传播、与 PG 层 active-rank state 的关系、故障恢复流程

EP 机制的核心价值在于：将 MoE 推理的通信模式特化为 all-to-all expert routing，优先利用硬件 fast path（RDMA/P2P），提供 timeout 故障检测，并与 Mooncake Backend (PG) 的弹性机制配合，实现高可用的分布式 MoE 推理。
