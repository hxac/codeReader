# CPU 同步、cached handle 与推理解码复用

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚为什么 dispatch 返回的 `recv_x` 行数要到**内核跑完之后**才能确定，以及由此派生出的三种处理模式。
- 读懂 `do_cpu_sync=True` 时 host 侧的**轮询 + 超时**循环，理解 `encode_decode_positive` 这个自反编码如何用 0 兼任"初值"与"未就绪哨兵"。
- 理解 `do_cpu_sync=False` 的**最坏情况分配**为何能兼容 CUDA graph，代价是什么。
- 掌握 `cached handle` 模式：把首次 dispatch 产出的 `EPHandle` 再次喂给 `dispatch`，让内核复用 `dst_buffer_slot_idx`、`psum_*`、`recv_src_metadata` 等布局张量，从而**跳过 CPU 同步与张量重分配**——这正是推理解码（decode）的快路径。
- 能对照 `csrc/elastic/buffer.hpp` 的 `cached_mode` 分支，逐项说出它省掉了哪些工作。

本讲承接 [u5-l1 直接模式 Dispatch](u5-l1-direct-dispatch.md)：u5-l1 讲的是"一个 CTA 内 notify/dispatch warps 如何协作把 token 搬到对端"，本讲讲的是"dispatch 主内核跑完之后、`recv_x` 交给用户之前，host 侧如何确定接收规模、以及如何用上一次的结果偷懒"。

## 2. 前置知识

- **dispatch/combine 的对称性**：dispatch 把 token 按 `topk_idx` 发往目标专家所在 rank，combine 是它的逆过程。dispatch 返回一个 `EPHandle`，combine 完全依赖它反向路由。见 [u2-l3](u2-l3-dispatch-combine-workflow.md)。
- **EPHandle 是"路由发货单"**：它不持有 token 数据，只持有路由元数据（每 rank/每专家收到几个 token、每个 token 落在 buffer 的哪个槽、源 token 全局索引等）。见 `EPHandle` 类定义 [deep_ep/buffers/elastic.py:L25-L57](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L25-L57)。
- **host workspace**：每个 `ElasticBuffer` 在构造时额外分配一块**页锁定 host 内存**（`cudaMallocHost` + `cudaHostGetDevicePointer`），内核可以通过映射后的设备指针 `mapped_host_workspace` 往里写计数，host 线程可以直接轮询读取，这是 `do_cpu_sync` 的物理基础。见 [csrc/elastic/buffer.hpp:L133-L135](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L133-L135)。
- **CUDA graph 对形状的硬约束**：CUDA graph 捕获的算子图要求所有中间张量形状固定，不能在回放时动态变化。这一点是"无 CPU sync 模式"存在的根本原因。

> 关键直觉：dispatch 是一次 all-to-all，**每个 rank 实际收到多少 token 取决于所有 rank 的 `topk_idx`**，这个数 host 在内核执行前不可能知道。所以"确定 `recv_x` 的行数"这件事，本质上是把 GPU 算出的计数搬回 CPU 的同步问题。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [csrc/elastic/buffer.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) | `ElasticBuffer::dispatch` 的 host 实现，三种模式的分支与轮询循环都在这里。 |
| [deep_ep/buffers/elastic.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py) | Python 侧 `dispatch`/`EPHandle`，把 `handle` 解包成一组 `cached_*` 参数传给 C++，并强制 cached 模式下 `do_cpu_sync=False`。 |
| [deep_ep/include/deep_ep/common/math.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/math.cuh) | `encode_decode_positive` / `is_decoded_positive_ready` 两个工具函数，CPU 轮询与 GPU notify 共用的编码。 |
| [deep_ep/include/deep_ep/impls/dispatch.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh) | 直接模式 dispatch 主内核，`kDoCPUSync` 模板参数控制 notify warps 是否把计数写进 host workspace。 |
| [deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh) | copy epilogue，`kCachedMode` 控制是否复用旧的槽索引、是否跳过 `recv_src_metadata` 的重写。 |
| [README.md](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md) | `decode_dispatch` 示例，演示 cached handle 的标准用法。 |

---

## 4. 核心概念与源码讲解

### 4.1 核心矛盾：recv_x 的行数运行时才知道（三种模式的统一入口）

#### 4.1.1 概念说明

dispatch 的输出 `recv_x` 形状是 `[num_recv_tokens, hidden]`。问题在于：`num_recv_tokens`（本 rank 这次一共收到多少个 token）**不是调用方给的**，而是所有 rank 的 `topk_idx` 共同决定的——只有当 notify warps 做完全 grid 归约、跨 rank 交换完计数之后，这个数才存在于 GPU 显存里。

于是 host 在 dispatch 主内核 launch 之后，面临一个三选一的决策：

1. **`do_cpu_sync=True`（CPU 同步模式）**：等内核把精确计数写回 host workspace，host 轮询读到精确的 `num_recv_tokens`，再按精确大小分配 `recv_x`。优点是张量刚好够用、后续 GEMM 不浪费；缺点是 host 必须阻塞等待 GPU。
2. **`do_cpu_sync=False` 且非 cached（最坏情况分配）**：不等任何东西，直接按"每个 rank 都把 `num_max_tokens_per_rank` 全发给我"的上界分配 `recv_x`。优点是 host 永不阻塞、张量形状固定（兼容 CUDA graph）；缺点是多分配了空间、尾部有无效行。
3. **cached handle（推理解码）**：路由和上一次完全一样，所以连计数都不用重新算——直接复用上一次 handle 里存的 `num_recv_tokens` 和所有布局张量。

#### 4.1.2 核心流程

dispatch host 函数在 launch 主内核之后，进入一个三分支决策（伪代码）：

```
launch_dispatch(...)                       # 主内核异步下发
if cached_mode:                            # 模式二：cached handle
    assert not do_cpu_sync
    num_recv_tokens = cached_num_recv_tokens          # 直接取旧值
    num_recv_tokens_per_expert_list = cached_list
else if do_cpu_sync:                       # 模式一：CPU 同步
    while not all_counters_ready():        # 轮询 host workspace
        if elapsed > num_cpu_timeout_secs: # 超时抛异常
            throw
    # 此时 num_recv_tokens / num_expanded_tokens 已精确得到
else:                                      # 模式三：最坏情况分配
    num_recv_tokens = num_max_tokens_per_rank * num_ranks
num_allocated_tokens = do_expand ? num_expanded_tokens : num_recv_tokens
recv_x = empty([num_allocated_tokens, hidden])   # 按所选规模分配
launch_dispatch_copy_epilogue(...)         # 拷贝内核
```

#### 4.1.3 源码精读

三分支的入口在 [csrc/elastic/buffer.hpp:L1006-L1071](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1006-L1071)。先看三段关键的赋值。

`cached_mode` 由 `cached_num_recv_tokens.has_value()` 推导，且一旦 cached 就强制要求 `do_cpu_sync=False`：

```cpp
// csrc/elastic/buffer.hpp:L1011-L1016
if (cached_mode) {
    EP_HOST_ASSERT(not do_cpu_sync and "Cannot do CPU sync with cached mode");
    num_recv_tokens = cached_num_recv_tokens.value();
    num_recv_tokens_per_expert_list = cached_num_recv_tokens_per_expert_list.value();
    num_expanded_tokens = cached_num_expanded_tokens.value();
}
```

最坏情况分配（模式三）只用了两行算术——把每 rank 的最大 token 数乘上 rank 数当上界，expand 模式再按 `expert_alignment` 对齐放大：

```cpp
// csrc/elastic/buffer.hpp:L1065-L1071
} else {
    // Non-cached mode without CPU sync, allocate with the worst case
    num_recv_tokens = num_max_tokens_per_rank * nccl_context->num_ranks;
    num_expanded_tokens = nccl_context->num_ranks * num_max_tokens_per_rank * std::min(num_topk, num_local_experts);
    num_expanded_tokens += (expert_alignment - 1) * num_local_experts;
    num_expanded_tokens = math::align(num_expanded_tokens, expert_alignment);
}
```

最终分配 `recv_x` 用的是 `num_allocated_tokens`（expand 取 expanded、否则取 recv）：

```cpp
// csrc/elastic/buffer.hpp:L1075-L1076
const auto num_allocated_tokens = do_expand ? num_expanded_tokens : num_recv_tokens;
auto recv_x = torch::empty({num_allocated_tokens, hidden}, x.options());
```

> 注意：模式三的 `recv_x` 行数远大于实际有效行。下游消费时必须配合 `psum_num_recv_tokens_per_scaleup_rank[-1]`（真实接收数，存在 GPU 上）来切片，否则会把尾部垃圾行喂给 GEMM。test_ep.py 里正是这样做的：`cached_recv_x = cached_recv_x[:num_recv_tokens]`（见 [tests/elastic/test_ep.py:L369-L379](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L369-L379)）。

#### 4.1.4 代码实践

**实践目标**：用源码阅读确认三种模式的入口与触发条件。

**操作步骤**：

1. 打开 [csrc/elastic/buffer.hpp:L726-L748](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L726-L748)，找到 `const bool cached_mode = cached_num_recv_tokens.has_value();` 这一行。
2. 往下读到 L734-L748，列出 cached 模式必须提供的 handle 字段（直接模式需要哪些、hybrid 模式额外需要哪些）。
3. 再跳到 L1006-L1071，对照上面伪代码确认三分支。

**需要观察的现象**：cached 模式的判定完全基于"用户有没有传 cached handle 字段"，而不是某个布尔开关；`do_cpu_sync` 在 cached 模式下会被 `EP_HOST_ASSERT` 直接拒绝。

**预期结果**：能口述"cached 模式 ⟺ `cached_num_recv_tokens` 有值 ⟺ Python 侧传入了 `handle=` 参数"。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cached_mode` 的判定不直接用一个 `bool cached` 参数，而是用 `cached_num_recv_tokens.has_value()`？

**参考答案**：因为 cached 模式不是一个独立功能，而是"复用一组旧布局张量"的状态。把判定挂在最具代表性的 `cached_num_recv_tokens` 上，可以同时承担"是否 cached"和"核心复用值是哪个"两重含义，减少冗余参数；同时 `EP_HOST_ASSERT` 在 L736-L748 强制其余 `cached_*` 字段必须成对出现，保证不会"半 cached"。

**练习 2**：模式三（最坏情况分配）下，`recv_x` 的真实有效行数存在哪里？

**参考答案**：存在 GPU 上的 `psum_num_recv_tokens_per_scaleup_rank` 的最后一个元素（前缀和的总和），即 `handle.psum_num_recv_tokens_per_scaleup_rank[-1]`。host 并不知道这个数，必须由下游 kernel 或用户根据这个 GPU 张量切片。

---

### 4.2 CPU 同步：do_cpu_sync 的轮询、超时与 encode_decode_positive 编码

#### 4.2.1 概念说明

`do_cpu_sync=True` 是训练前向的标准路径：host 阻塞等待，拿到精确的每 rank / 每专家接收计数，再分配精确大小的 `recv_x`。它依赖三个部件协作：

- **GPU 侧 notify warps**：做完全 grid 归约与跨 rank 计数交换后，把最终的 rank/expert 计数写进**映射到 host 的 workspace**（`mapped_host_workspace`）。这是由 dispatch 内核的模板参数 `kDoCPUSync` 控制的编译期分支，只有 `kDoCPUSync=true` 才会写。
- **host 侧轮询循环**：在 `ElasticBuffer::dispatch` 里，用一个 `while(true)` 循环反复读 host workspace，直到所有计数都"就绪"。
- **`encode_decode_positive` 编码**：用同一个自反函数 \(f(x)=-x-1\)，让"未写入的 0"自然表示"未就绪"，而真实计数 \(c\ge 0\) 写入后能被 host 解码回 \(c\)。

#### 4.2.2 核心流程

编码数学：定义

\[
f(x) = -x - 1
\]

它是一个**对合（involution）**，即 \(f(f(x)) = x\)：

\[
f(f(x)) = -((-x-1)) - 1 = x + 1 - 1 = x
\]

GPU 写入计数 \(c\) 时，存的是 \(f(c) = -c-1 \le -1\)（负数）。host 读到原始值 \(v\) 后，再算一次 \(f(v)\) 就还原出 \(c\)。判定"就绪"用 `is_decoded_positive_ready(decoded) := decoded >= 0`：

| 内存原始值 \(v\) | 含义 | 解码 \(f(v)\) | 就绪？ |
| --- | --- | --- | --- |
| 0（初值，memset 清零） | 内核还没写 | \(-1\) | 否 |
| \(-c-1\)（内核写了计数 \(c\)） | 已写 | \(c \ge 0\) | 是 |

于是 **0 同时兼任"初值"与"未就绪哨兵"**，负号位充当就绪标志，真实计数无损保存在数值里——一个 int 同时承载"状态 + 数据"。

轮询流程：

```
# 内核 launch 前：把 host workspace 的 rank/expert 计数段清零
fill(host_rank_count, 0); fill(host_expert_count, 0)
atomic_thread_fence(seq_cst)            # 保证清零对 GPU 可见有序
launch_dispatch(..., do_cpu_sync=True)  # 内核里 kDoCPUSync=true 才会写 host ws

start = now()
while True:
    ready = True
    while 还有 rank 计数没读 and ready:
        v = host_rank_count[i]
        c = f(v)                        # encode_decode_positive
        if c >= 0:                      # is_decoded_positive_ready
            num_recv_tokens += c; i += 1
        else: ready = False
    while 还有 expert 计数没读 and ready:
        ...同理累加到 num_expanded_tokens / per_expert_list...
    if ready: break
    if now() - start > num_cpu_timeout_secs:
        throw EPException(...)          # 超时，打印当前 buffer 状态
```

#### 4.2.3 源码精读

两个工具函数极其简短，定义在 [deep_ep/include/deep_ep/common/math.cuh:L26-L33](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/math.cuh#L26-L33)：

```cpp
template <typename dtype_t>
__forceinline__ __device__ __host__ bool is_decoded_positive_ready(const dtype_t& value) {
    return value >= 0;
}
template <typename dtype_t>
__forceinline__ __device__ __host__ dtype_t encode_decode_positive(const dtype_t& value) {
    return -value - static_cast<dtype_t>(1);
}
```

> 注意 `__device__ __host__` 双修饰：GPU notify warps 用它**编码**写入，CPU 轮询用它**解码**读取——同一个函数，两边复用，这是它能当对合的关键设计。

GPU 侧：dispatch 内核的模板参数里就有 `kDoCPUSync`（[deep_ep/include/deep_ep/impls/dispatch.cuh:L17-L30](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L17-L30)），SM 0 的 notify warps 在做完前缀和之后，**仅当 `kDoCPUSync` 为真**才把计数编码写进 host workspace：

```cpp
// deep_ep/include/deep_ep/impls/dispatch.cuh:L224-L230
if constexpr (kDoCPUSync) {
    for (int i = thread_idx; i < kNumRanks + kNumExpertsPerRank; i += kNumNotifyThreads) {
        host_workspace_layout.get_scaleup_rank_expert_count_ptr<false>()[i] =
            math::encode_decode_positive(rank_expert_count[i]);
    }
    __syncwarp();
}
```

host 侧轮询循环主体在 [csrc/elastic/buffer.hpp:L1017-L1064](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1017-L1064)。读 rank 计数的内层循环（注意 `encode_decode_positive` 与 `is_decoded_positive_ready` 的组合用法）：

```cpp
// csrc/elastic/buffer.hpp:L1024-L1031
while (counter_scaleup_rank_idx < nccl_context->num_scaleup_ranks and ready) {
    const auto count = math::encode_decode_positive(
        host_workspace_layout.get_scaleup_rank_count_ptr<false>()[counter_scaleup_rank_idx]);
    if ((ready = math::is_decoded_positive_ready(count))) {
        num_recv_tokens += count;
        ++ counter_scaleup_rank_idx;
    }
}
```

超时检测在循环末尾，超时则抛出带调试信息的异常（`get_buffer_info()` 会把当前所有计数拼成字符串，方便定位哪个 rank 卡住）：

```cpp
// csrc/elastic/buffer.hpp:L1060-L1063
const auto now = std::chrono::high_resolution_clock::now();
if (std::chrono::duration_cast<std::chrono::seconds>(now - start_cpu_time).count() > num_cpu_timeout_secs)
    throw EPExceptionWithLineInfo("Dispatch CPU wait", get_buffer_info());
```

还有一个容易被忽略但至关重要的前置步骤：**每轮 dispatch 前必须把 host workspace 的计数段清零**，否则上一轮残留的编码值会被误判为"就绪"。清零后跟一条 `seq_cst` 内存栅栏，保证清零在内核 launch 之前对 GPU 全局可见：

```cpp
// csrc/elastic/buffer.hpp:L969-L976
const auto host_workspace_layout = layout::WorkspaceLayout(
    host_workspace, nccl_context->num_scaleout_ranks, nccl_context->num_scaleup_ranks, num_experts);
std::fill_n(host_workspace_layout.get_scaleup_rank_count_ptr<false>(), nccl_context->num_scaleup_ranks, 0);
std::fill_n(host_workspace_layout.get_scaleup_expert_count_ptr<false>(), num_local_experts, 0);
std::atomic_thread_fence(std::memory_order_seq_cst);
```

#### 4.2.4 代码实践

**实践目标**：直观验证 `encode_decode_positive` 的对合性质与"0 即未就绪"的哨兵语义。

**操作步骤**：

1. 写一段独立 Python（不依赖 GPU）模拟该编码：
   ```python
   # 示例代码：不依赖 DeepEP，仅演示编码语义
   def f(x): return -x - 1
   for c in [0, 1, 5, 100]:
       stored = f(c)          # GPU 写入
       decoded = f(stored)    # CPU 读出
       ready = decoded >= 0
       print(f"count={c:3d} -> stored={stored:4d} -> decoded={decoded:3d}, ready={ready}")
   # 初值 0
   print("initial 0 -> decoded =", f(0), "ready =", f(0) >= 0)
   ```
2. 运行它，对照本讲表格核对每一行。
3. （可选，需 8 卡 Hopper）跑 `python tests/elastic/test_ep.py --num-experts 64 --num-topk 6 --do-cpu-sync 1`，并设 `EP_BUFFER_DEBUG=1`，观察 dispatch 中 CPU 侧打印的 `CPU side received count ...` 那一行（对应 [csrc/elastic/buffer.hpp:L1045-L1057](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1045-L1057) 的 `get_buffer_info`）。

**需要观察的现象**：步骤 2 中，所有 `count>=0` 都 `ready=True` 且 `decoded==count`；而初值 0 解码为 -1、`ready=False`。步骤 3（若可运行）会看到每个 rank 收到的 token 计数列表。

**预期结果**：步骤 2 输出与表格完全一致。步骤 3 标注「待本地验证」（需要 Hopper + 多卡环境）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `encode_decode_positive` 改成 `f(x) = -x`（去掉 `-1`），这套"0 兼任哨兵"的机制还能工作吗？

**参考答案**：不能。若 \(f(x)=-x\)，则计数 \(c=0\) 写入后存的是 0，与"未写入的初值 0"无法区分——一个真的收到 0 个 token 的 rank 会被永远误判为"未就绪"，导致轮询死循环到超时。`-1` 这一项正是为了让任意 \(c\ge 0\) 都映射到严格负数，腾出 0 这个值专门表示"未写入"。注意 dispatch 内核里 notify warps 跨 rank 计数交换时也复用了同一编码（[dispatch.cuh:L128-L132](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L128-L132)），原因相同。

**练习 2**：为什么 `num_cpu_timeout_secs` 默认高达 300 秒（见 [elastic.py:L245](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L245)）？设小一点不是能更快发现死锁吗？

**参考答案**：dispatch 主内核里本身有 GPU 侧超时保护（`num_gpu_timeout_cycles`，默认 100 秒换算成周期，见 [buffer.hpp:L121-L123](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L121-L123)），GPU 端先超时会打印并卡住内核；CPU 这边的 300 秒是兜底，避免在大模型训练的合法长 kernel / 调度抖动下误报。设太小会在正常重负载时频繁误抛异常。

---

### 4.3 cached handle：复用布局，跳过同步（推理解码快路径）

#### 4.3.1 概念说明

推理解码（decode）有一个重要特点：**很多步的 `topk_idx` 路由结果是相同的**（例如同一批请求在若干步内命中相同专家，或采用固定路由策略）。既然路由相同，那么：

- 每个 rank/专家收到的 token 数相同 → `num_recv_tokens` 与 per-expert 计数列表不变。
- 每个 token 落在 buffer 里的槽位相同 → `dst_buffer_slot_idx` 不变。
- combine 反向所需的所有元数据（`recv_src_metadata`、`token_metadata_at_forward`、`channel_linked_list`）都不变。

`cached handle` 模式就是把这些"上一次算好的布局"原封不动地再喂回 `dispatch`，让内核**直接复用槽索引搬数据**，host 则**跳过 CPU 同步、跳过这些布局张量的重新分配**。

这里有一个微妙但关键的点：**cached 模式下内核仍然要搬数据**（token 内容是新的），只是不再做 notify 计数、不再 atomicAdd 抢槽——它从 `dst_buffer_slot_idx` 直接读出预先算好的槽位写入。这通过启动器把 `num_notify_warps` 置 0、`reuse_slot_indices=true` 实现。

#### 4.3.2 核心流程

Python 侧的标准用法（取自 README 的 decode 示例）：

```
# 首次 dispatch：正常路径，生成 handle（含全部布局张量）
recv_x, recv_topk_idx, recv_topk_weights, handle, event = buffer.dispatch(
    x, topk_idx=topk_idx, topk_weights=topk_weights, num_experts=..., async_with_compute_stream=True)

# 后续步：topk_idx 不变，把同一个 handle 传回去
recv_x, _, _, handle, event = buffer.dispatch(
    x, handle=cached_handle, num_sms=..., async_with_compute_stream=True)
```

Python `dispatch` 看到 `handle is not None` 后做的事：

1. 强制 `topk_idx = handle.topk_idx`（用户不能再传 `topk_idx`）。
2. 强制 `do_cpu_sync = False`（cached 与 CPU sync 互斥）。
3. 把 handle 的 10 个字段解包成 `cached_*` 可选参数传给 C++（`_unpack_handle`）。
4. C++ 侧 `cached_mode = cached_num_recv_tokens.has_value()` 为真 → 走 4.1 的 cached 分支。

C++ 侧 cached 分支省掉的工作：

| 工作 | 非 cached | cached |
| --- | --- | --- |
| notify warps 计数与跨 rank 交换 | 有（`kNumNotifyWarps` 个 warp） | **无**（`num_notify_warps=0`） |
| 把计数写 host workspace | 有（`kDoCPUSync` 时） | **无** |
| host 轮询 + 超时 | 有 | **无** |
| 重新分配 `psum_*`、`dst_buffer_slot_idx`、`recv_src_metadata` 等布局张量 | 有 | **无**（复用传入的 cached 张量） |
| `num_recv_tokens` 来源 | 实时算 / 最坏上界 | **直接取 `cached_num_recv_tokens`** |
| dispatch warps 抢槽方式 | `atomicAdd` 实抢 | **读 `dst_buffer_slot_idx` 复用**（`reuse_slot_indices=true`） |

#### 4.3.3 源码精读

Python 侧：handle 解包与强制约束在 [deep_ep/buffers/elastic.py:L937-L956](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L937-L956)，核心几行：

```python
# deep_ep/buffers/elastic.py:L937-L948
if handle is not None:
    assert topk_idx is None
    assert do_cpu_sync is None or not do_cpu_sync, 'Cannot do CPU sync with cached handle'
    topk_idx = handle.topk_idx
    num_max_tokens_per_rank = value_or(num_max_tokens_per_rank, handle.num_max_tokens_per_rank)
    num_experts = value_or(num_experts, handle.num_experts)
    expert_alignment = value_or(expert_alignment, handle.expert_alignment)
    do_cpu_sync = False
    assert (num_experts, expert_alignment, num_max_tokens_per_rank) == \
           (handle.num_experts, handle.expert_alignment, handle.num_max_tokens_per_rank)
```

`_unpack_handle` 把 handle 的字段平铺成 10 个返回值，正好对应 C++ `dispatch` 的 10 个 `cached_*` 入参（[deep_ep/buffers/elastic.py:L511-L527](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L511-L527)）。注意最后还有一个判断：返回的 `handle` 是否复用旧对象——

```python
# deep_ep/buffers/elastic.py:L999-L1000
is_cached_dispatch = handle is not None
if not is_cached_dispatch:
    handle = EPHandle(...)   # 只有首次才新建 EPHandle
```

即 cached 模式下 `dispatch` 返回的 handle 就是传入的同一个对象（`do_handle_copy` 也被忽略），这也是 test_ep.py 里 `assert handle.topk_idx.data_ptr() == cached_handle.topk_idx.data_ptr()`（[test_ep.py:L366](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L366)）成立的原因。

C++ 侧：cached 模式下，所有布局张量都走"传入即复用"分支。以 `psum_num_recv_tokens_per_expert` 为例，cached 时直接用传入的张量并校验形状，非 cached 才 `torch::empty` 新建（[csrc/elastic/buffer.hpp:L806-L816](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L806-L816)）。最关键的启动器改动在 launch_dispatch：**cached 模式把 notify warps 数直接置 0**，并设 `reuse_slot_indices=true`：

```cpp
// csrc/kernels/elastic/dispatch.hpp:L175-L177
const int num_notify_warps = cached_mode ? 0 : kNumNotifyWarps;
const bool reuse_slot_indices = cached_mode;
const int num_notify_smem_bytes = cached_mode ? 0 : get_num_notify_smem_bytes(num_ranks, num_experts);
```

`num_notify_warps=0` 意味着整个 notify 阶段（计数、归约、跨 rank 交换、写 host workspace）被编译期裁掉；`reuse_slot_indices=true` 经模板参数 `kReuseSlotIndices` 传进内核，让 dispatch warps 直接按 `dst_buffer_slot_idx` 的旧槽位写入，而不是 atomicAdd 实抢（见 [dispatch.hpp:L55-L57](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L55-L57) 把 `do_cpu_sync` / `reuse_slot_indices` 烘焙进模板实参）。

copy epilogue 里同样有 `kCachedMode` 模板参数：cached 模式下**跳过 `recv_src_metadata` 的重写**，因为它是首次 dispatch 生成、combine 还要原样使用的"路由发货单"，不能被覆盖：

```cpp
// deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh:L188-L192
// Write source token index (skip in cached mode as metadata is reused)
// And:
//   - Non-hybrid mode: the source scaleup peer rank index and master top-k lane index
//   - Hybrid mode: the slot index and master top-k lane index
if constexpr (not kCachedMode) {
    ... recv_src_metadata[i * kMetadataStride + 0] = ...;
}
```

#### 4.3.4 代码实践

**实践目标**：写一段"首次 dispatch 生成 handle → 后续多次用同一 cached handle dispatch"的最小代码，并对照源码说清 cached 分支跳过了什么。

**操作步骤**：

1. 阅读 README 的 `decode_dispatch` 示例 [README.md:L280-L313](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L280-L313)，理解 `cached_handle` 的传入/传出约定。
2. 仿照它写一段最小代码（**示例代码**，需 8 卡 Hopper 才能运行）：
   ```python
   # 示例代码：cached handle 复用
   import torch, deep_ep
   # ...（省略 dist 初始化与 buffer 构造，参考 tests/elastic/test_ep.py）
   num_experts, num_max_tokens_per_rank, num_topk = 64, 4096, 6
   topk_idx = torch.randint(0, num_experts, (num_max_tokens_per_rank, num_topk), device='cuda', dtype=torch.int64)
   topk_weights = torch.rand(num_max_tokens_per_rank, num_topk, device='cuda')
   x = torch.randn(num_max_tokens_per_rank, 7168, device='cuda', dtype=torch.bfloat16)

   # 第 1 次：正常 dispatch，生成 handle
   recv_x, recv_topk_idx, recv_topk_weights, handle, ev = buffer.dispatch(
       x, topk_idx=topk_idx, topk_weights=topk_weights,
       num_experts=num_experts, num_max_tokens_per_rank=num_max_tokens_per_rank)
   ev.current_stream_wait()
   num_recv_tokens = handle.psum_num_recv_tokens_per_scaleup_rank[-1].item()

   # 第 2 次：同一个 topk_idx，复用 handle（注意不再传 topk_idx）
   cached_x, _, _, cached_handle, cev = buffer.dispatch(x, handle=handle)
   cev.current_stream_wait()

   # 正确性：路由相同 → 接收内容应逐位一致
   assert torch.equal(recv_x[:num_recv_tokens], cached_x[:num_recv_tokens])
   assert cached_handle is handle  # 同一对象
   ```
3. 对照 [csrc/elastic/buffer.hpp:L806-L816](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L806-L816)、[L1011-L1016](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1011-L1016) 与 [dispatch.hpp:L175-L177](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L175-L177)，逐项写下第 2 次 dispatch 跳过了哪些工作。

**需要观察的现象**：第 2 次 dispatch 不再触发任何 CPU 同步等待（host 立即返回）；`assert` 全部通过；`cached_handle is handle` 为真。

**预期结果**：能列出"跳过 notify warps（计数/归约/写 host ws）、跳过 host 轮询、跳过 psum/dst_buffer_slot_idx/recv_src_metadata 等张量的重新分配、num_recv_tokens 直接取缓存值"。运行部分标注「待本地验证」（需多卡 Hopper）。

> 真实测试可直接参考 [tests/elastic/test_ep.py:L162-L177](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L162-L177) 的 `cached_dispatch_args`，以及 L434-L443 的逐位断言（含 FP8 的 `(recv_x[0], recv_x[1])` 双张量比对）。

#### 4.3.5 小练习与答案

**练习 1**：cached 模式下，内核完全不做计数，那它怎么知道每个 token 该写到 buffer 的哪个槽？

**参考答案**：首次 dispatch 时，dispatch warps 用 `atomicAdd` 在对端 buffer 上抢槽，并把抢到的槽位写进 `dst_buffer_slot_idx`（直接模式 `[num_tokens, num_topk]`）或 `token_metadata_at_forward`（hybrid 模式）。cached 模式下 `reuse_slot_indices=true`，dispatch warps 直接从这些张量读出预先算好的槽位写入，无需再抢。前提是路由 `topk_idx` 与首次完全一致——这正是 Python 侧 `assert topk_idx is None` 并强制 `topk_idx = handle.topk_idx` 的原因。

**练习 2**：为什么 cached 模式下，`recv_src_metadata` 在 copy epilogue 里不能重写，但 `recv_x` 却必须重写？

**参考答案**：`recv_src_metadata` 是"路由发货单"——记录每个收到的 token 来自哪个源 token、哪个 topk lane、落在哪个槽，combine 要原样用它反向路由。它在首次 dispatch 已生成且路由不变，重写既无必要又会破坏 combine。而 `recv_x` 是 token 的**隐状态数据**，每一步 decode 的 x 都是新算出来的，必须重新搬运，所以内核仍要跑 dispatch warps + copy epilogue 把新 x 写进 `recv_x`。

**练习 3**：如果两次 dispatch 之间 `topk_idx` 其实变了（用户强行复用旧 handle），会发生什么？

**参考答案**：内核会按旧的 `dst_buffer_slot_idx` 写新路由的 token，槽位与实际路由错配，combine 反向归约会得到错误的 `combined_x`（静默数据错误，无断言保护）。因此 cached handle 的复用前提由**调用方**保证路由不变，库本身不校验 `topk_idx` 内容是否改变——这也是 README decode 示例注释强调"when the gating decisions remain unchanged"的原因。

---

## 5. 综合实践

**任务**：在单机 8 卡上，用同一份 `topk_idx` 跑三种模式，对比它们的输出与开销。

**步骤**：

1. 参考 [tests/elastic/test_ep.py:L143-L177](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L143-L177) 的三组 dispatch 参数（`dispatch_args` / `cached_dispatch_args` / 带 `do_cpu_sync=False` 的版本），构造同一段 `x` 和 `topk_idx`。
2. 分别用：
   - `do_cpu_sync=True`（精确分配，参考 `--do-cpu-sync 1`）
   - `do_cpu_sync=False` 且不传 handle（最坏情况分配）
   - 传 `handle=`（cached）
   跑 dispatch，记录三者返回的 `recv_x.shape[0]`、`handle.num_recv_tokens`、以及真实有效行数 `handle.psum_num_recv_tokens_per_scaleup_rank[-1]`。
3. 对三种 `recv_x[:num_recv_tokens]` 做逐位比对（`torch.equal`），验证有效内容一致。
4. 用 `bench_kineto`（参考 [test_ep.py:L276-L284](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L276-L284) 的 cached dispatch 基准段）测三种模式的 dispatch 耗时，观察 cached 是否明显快于非 cached（因为少了 notify 阶段）。

**预期结论**：

- 模式一 `recv_x.shape[0] == num_recv_tokens`（精确）；模式三 `recv_x.shape[0]` 远大于 `num_recv_tokens`（最坏上界）；cached 与模式一的 `recv_x.shape[0]` 一致（复用了精确的 `cached_num_recv_tokens`）。
- 三者有效内容 `[:num_recv_tokens]` 逐位相等。
- cached dispatch 因 `num_notify_warps=0` 通常比非 cached 更快。

> 运行部分标注「待本地验证」（需 Hopper + 8 卡 NVLink 环境）。无 GPU 时可降级为"源码阅读型实践"：完成 4.3.4 的第 3 步即可。

## 6. 本讲小结

- dispatch 返回的 `recv_x` 行数 `num_recv_tokens` 由所有 rank 的 `topk_idx` 共同决定，host 在内核执行前无法知道——这是三种 host 处理模式存在的根本原因。
- **模式一 `do_cpu_sync=True`**：内核（`kDoCPUSync=true`）把计数写进映射到 host 的 workspace，host 用 `while` 循环轮询，靠 `encode_decode_positive`（对合 \(f(x)=-x-1\)）让 0 兼任"初值/未就绪哨兵"、负号位充当就绪标志，超时（默认 300s）抛异常。分配精确大小的 `recv_x`，是训练前向的标准路径。
- **模式三 无 CPU sync**：按 `num_max_tokens_per_rank * num_ranks` 的最坏上界分配 `recv_x`，host 永不阻塞、张量形状固定，代价是尾部有无效行（需用 GPU 上的前缀和切片），是兼容 CUDA graph 的路径。
- **模式二 cached handle**：路由不变时，把首次 dispatch 产出的 `EPHandle` 整体回传，C++ 侧 `cached_mode` 分支 `num_notify_warps=0`、`reuse_slot_indices=true`、跳过 host 轮询与所有布局张量重分配，`num_recv_tokens` 直接取缓存值——这是推理解码的快路径。
- 三种模式的入口是 [buffer.hpp:L1006-L1071](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1006-L1071) 的三分支；cached 与 CPU sync 互斥（`EP_HOST_ASSERT(not do_cpu_sync)`）。
- copy epilogue 在 cached 模式下跳过 `recv_src_metadata` 重写（[dispatch_copy_epilogue.cuh:L192](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch_copy_epilogue.cuh#L192)），因为它是 combine 反向路由要原样复用的"发货单"。

## 7. 下一步学习建议

- 继续往 combine 方向读：[u6-l1 Combine 主流程](u6-l1-combine-main.md)，看 `EPHandle.recv_src_metadata`、`psum_num_recv_tokens_per_scaleup_rank` 如何驱动 combine 的反向路由——本讲讲的正是这些字段在 dispatch 端是如何"生产并可选缓存"的。
- 若对"为什么 expand 模式下 `psum_num_recv_tokens_per_expert` 被当原子计数器、而非 cached 时却当只读布局"感兴趣，回顾 [u5-l3 Dispatch copy epilogue 与 expand 布局](u5-l3-dispatch-epilogue.md)。
- 想理解 cached handle 里 hybrid 模式特有的 `token_metadata_at_forward`、`channel_linked_list` 如何被 combine 复用，可预习 [u5-l2 Hybrid Dispatch](u5-l2-hybrid-dispatch.md) 与 [u6-l1](u6-l1-combine-main.md) 的 hybrid combine 段。
- 对"运行时确定形状的张量如何塞进 CUDA graph"这个通用工程问题，可对照本讲模式三，进一步阅读项目内对 `do_cpu_sync=False` 路径与 expand 上界公式的相关测试断言。
