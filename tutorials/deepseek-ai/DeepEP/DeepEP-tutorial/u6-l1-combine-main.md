# Combine 主流程：把 expert 输出推回原 rank

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚 **combine 是 dispatch 的逆过程**：dispatch 把 token 按 `topk_idx` 发到目标专家所在 rank，combine 则把每个专家计算后的输出按原路推回原始 rank，并按 `topk_weights` 加权归约。
2. 理解 `EPHandle` 中哪些张量（`recv_src_metadata`、`psum_num_recv_tokens_per_scaleup_rank`、`token_metadata_at_forward`、`channel_linked_list`）驱动 combine 的反向路由。
3. 读懂直接模式 combine 内核 `combine_impl` 如何逐 token 解码源地址、经 NVLink 直达或 RDMA 中转写回对端。
4. 掌握 `launch_combine` 的输入约束（BF16、16 字节对齐）、缓冲区布局与 `reduce_buffer` 的作用，以及 hybrid combine 如何复用 dispatch 产出的链表元数据。

本讲只覆盖 **combine 的主流程与反向路由**，不展开 reduce epilogue 的归约细节（留待 u6-l2），也不展开确定性排序（留待 u6-l3）。

## 2. 前置知识

本讲假设你已经学完 **u5-l1（直接模式 Dispatch）**，因此你已经熟悉：

- **dispatch/combine 的对称结构**：dispatch 是 token“发货”，combine 是把专家处理后的结果“退货归约”回原 rank，二者共享同一块 NCCL 对称缓冲区，分时复用。
- **`EPHandle`（路由句柄）**：dispatch 返回的“发货单”，承载反向路由所需的全部元数据。combine 必须依赖它才能工作。
- **NVLink 与 RDMA 两条物理链路**：节点内 NVLink 直达（symmetric memory 写），节点间 RDMA 经 `send_buffer` 中转（`gin.put`）。
- **expand 与非 expand 两种布局**（u5-l3）：非 expand 按到达顺序紧凑排列，保留 `recv_topk_idx`；expand 按专家分组、每 token 一槽，`recv_topk_idx` 返回 `None`。

还需要补充两个本讲会用到的概念：

- **反向路由（reverse routing）**：dispatch 时每个 rank 都记下了“我收到的第 `i` 个 token 来自哪个原 rank 的哪个位置”。combine 就用这张表把专家输出送回那个原位置。
- **`allow_multiple_reduction`（多次归约）**：combine 的一个开关。开启时，hybrid 模式可以“先在节点内 scaleup 归约一次，再跨节点 scaleout 归约一次”，传输量更小但归约次数更多、精度略损；关闭时则要把所有副本原样送到最终 rank 再一次性归约。它会影响缓冲区布局（见 4.3）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`csrc/elastic/buffer.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) | C++ host 层 `ElasticBuffer::combine`：校验输入、从 `EPHandle` 取张量、调用 `launch_combine` 与 reduce epilogue。 |
| [`csrc/kernels/elastic/combine.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/combine.hpp) | JIT 启动器 `CombineRuntime`、host 启动函数 `launch_combine`，决定 warp 划分与缓冲区布局，区分直接/hybrid 模式。 |
| [`deep_ep/include/deep_ep/impls/combine.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh) | 直接模式 combine GPU 内核 `combine_impl` 的真正实现。 |
| [`deep_ep/include/deep_ep/impls/hybrid_combine.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_combine.cuh) | 多节点 hybrid combine GPU 内核 `hybrid_combine_impl`，两级反向路由（scaleup + scaleout）。 |
| [`deep_ep/include/deep_ep/impls/combine_utils.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_utils.cuh) | combine 公用工具：`use_rank_layout`、`get_num_tokens_in_layout`、本地归约 `combine_reduce`。 |
| [`deep_ep/buffers/elastic.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py) | Python 层 `ElasticBuffer.combine` 与 `EPHandle` 字段定义。 |

调用方向（从外到内）：

```
elastic.py: combine(x, handle, topk_weights, ...)
   └─> csrc/elastic/buffer.hpp: ElasticBuffer::combine          (host 校验 + 取 EPHandle 张量)
        └─> csrc/kernels/elastic/combine.hpp: launch_combine     (JIT generate+build+launch)
             └─> combine.cuh: combine_impl<...>                  (直接模式 GPU 内核)
              或 hybrid_combine.cuh: hybrid_combine_impl<...>    (多节点 GPU 内核)
```

## 4. 核心概念与源码讲解

### 4.1 combine 是 dispatch 的逆过程：EPHandle 驱动反向路由

#### 4.1.1 概念说明

理解 combine 的关键是抓住一句话：**combine 不重新计算路由，它“重放”dispatch 已经算好的路由**。

dispatch 阶段，每个 rank 都把“我发出的 token 去了哪些专家、我又收到了哪些来自别 rank 的 token、每个收到的 token 原本属于谁”这些信息写进 `EPHandle`。combine 阶段，专家已经在本地对这些 token 做完计算（产生 `x`），现在要把它们送回各自的原 rank 并按权重归约。原路怎么走？答案全在 `EPHandle` 里。

因此 combine 的输入除了专家输出 `x`，**必须**带上 dispatch 返回的 `handle`。没有 handle，combine 无法知道每个 token 该回哪里。

#### 4.1.2 核心流程

combine 在 host 层（`ElasticBuffer::combine`）大致分四步：

1. **校验**：检查 `x` 是 BF16、第二维 `hidden` 满足 16 字节对齐（`sizeof(int4)`），检查 `handle` 里的张量形状与类型。
2. **取张量**：从 `EPHandle` 取出反向路由所需的元数据张量。
3. **主内核 `launch_combine`**：把 `x` 的每个 token 按元数据推回原 rank 的接收缓冲区（`reduce_buffer`）。
4. **reduce epilogue**：把 `reduce_buffer` 里收到的多份 token 加权归约成最终输出 `combined_x`（本讲不展开，见 u6-l2）。

其中第 2 步是理解“反向路由”的入口，下一节直接看源码。

#### 4.1.3 源码精读

Python 层 `ElasticBuffer.combine` 把 `handle` 的字段逐个传给 C++，这是反向路由的“接线”位置：

```python
# deep_ep/buffers/elastic.py
combined_x, combined_topk_weights, event = \
    self.runtime.combine(x, topk_weights, bias_0, bias_1,
                         handle.recv_src_metadata,      # 反向路由的核心表
                         handle.topk_idx,               # 喂给 reduce epilogue
                         handle.psum_num_recv_tokens_per_scaleup_rank,  # 接收规模
                         handle.token_metadata_at_forward,   # 仅 hybrid
                         handle.channel_linked_list,         # 仅 hybrid
                         handle.num_experts, handle.num_max_tokens_per_rank,
                         num_sms, num_qps, ...,
                         handle.do_expand)               # 决定布局与 topk_weights 形状
```

见 [elastic.py:1091-1106](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L1091-L1106)。注意 `handle` 字段并不是全部都用到主内核——`topk_idx` 只喂给 reduce epilogue，主内核真正用来“认路”的是 `recv_src_metadata` 与 `psum_num_recv_tokens_per_scaleup_rank`（以及 hybrid 模式下的另外两个链表）。

`EPHandle` 各字段的语义定义在 [elastic.py:25-95](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L25-L95)，与本讲反向路由直接相关的有：

| 字段 | 形状 | combine 中的用途 |
| --- | --- | --- |
| `recv_src_metadata` | `[num_recv_tokens, num_topk+2]` | **反向路由主表**：每行描述一个收到的 token 来自哪个原 rank 的哪个位置 |
| `psum_num_recv_tokens_per_scaleup_rank` | `[num_scaleup_ranks]` | 末元素 = 本 rank 收到的总 token 数；无 CPU sync 时 host 用它推断接收规模 |
| `token_metadata_at_forward` | `[num_channels, num_max_forwarded, 2+num_topk*2]` | 仅 hybrid：forward 阶段每个转发 token 的元数据 |
| `channel_linked_list` | `[num_channels, num_max_forwarded, num_scaleup_ranks]` | 仅 hybrid：把零散到达的 token 串成链表供 scaleup 遍历 |
| `do_expand` | bool | 决定接收布局（expand / 非 expand）与 `topk_weights` 形状 |

C++ host 层对 `topk_weights` 形状的校验，清楚展示了 `use_expanded_layout`（即 `handle.do_expand`）如何决定它是 1D 还是 2D：

```cpp
// csrc/elastic/buffer.hpp
if (topk_weights.has_value()) {
    if (use_expanded_layout) {
        const auto [num_tokens__] = get_shape<1>(topk_weights.value());   // 1D: [num_tokens]
        EP_HOST_ASSERT(num_tokens == num_tokens__);
    } else {
        const auto [num_tokens__, num_topk__] = get_shape<2>(topk_weights.value());  // 2D: [num_tokens, num_topk]
        EP_HOST_ASSERT(num_tokens == num_tokens__ and num_topk == num_topk__);
    }
    ...
}
```

见 [buffer.hpp:1225-1235](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1225-L1235)。**结论**：

- **非 expand 模式**：每个 token 保留 `num_topk` 个权重，`topk_weights` 是 2D `[num_tokens, num_topk]`。
- **expand 模式**：每个 token 已被展开到独立的专家槽，权重按槽存，`topk_weights` 退化为 1D `[num_tokens]`（这里 `num_tokens` 是展开后的槽数）。

#### 4.1.4 代码实践

**实践目标**：对照 host 层 `ElasticBuffer::combine`，列出它从 `EPHandle` 读取了哪些张量，并验证 `use_expanded_layout` 对 `topk_weights` 形状的影响。

**操作步骤**：

1. 打开 [buffer.hpp:1179-1303](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1179-L1303)，逐行确认 `combine` 的形参表。
2. 对照 [elastic.py:1092-1106](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L1092-L1106)，画一张“`EPHandle` 字段 → C++ 形参”的映射表。
3. 定位 [buffer.hpp:1225-1235](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1225-L1235) 的两分支断言。

**需要观察的现象**：你会发现主内核 `launch_combine` 实际只接收 `src_metadata`（=`recv_src_metadata`）、`psum_num_recv_tokens_per_scaleup_rank`，以及 hybrid 模式的 `token_metadata_at_forward`、`channel_linked_list`；而 `topk_idx`（作为 `combined_topk_idx`）只传给 reduce epilogue（[buffer.hpp:1285-1303](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1285-L1303) 与 [buffer.hpp:1316-1318](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1316-L1318)）。

**预期结果**：写出一表，至少包含 `recv_src_metadata / topk_idx / psum_num_recv_tokens_per_scaleup_rank / token_metadata_at_forward / channel_linked_list / num_experts / num_max_tokens_per_rank / do_expand` 八项，并标注每项“喂给主内核 / 喂给 epilogue / 仅校验”。

#### 4.1.5 小练习与答案

**练习 1**：如果调用 combine 时忘记传 `handle.recv_src_metadata`（传成空），内核还能正确路由吗？为什么？

**答案**：不能。`recv_src_metadata` 是反向路由的“源地址表”，主内核逐 token 从中解码出“这个 token 来自哪个原 rank 的哪个位置”（见 4.2.3）。没有它，内核无法知道每个 token 该写回哪里。

**练习 2**：为什么 `handle.topk_idx` 喂给 reduce epilogue 而不是主内核？

**答案**：主内核只负责“把 token 搬到正确的接收缓冲区位置”（物理搬运）；reduce epilogue 才负责“把同一原 token 的多份副本按权重加权归约成一行”，归约时需要知道每份副本对应哪个 top-k 槽位，于是需要 `topk_idx`。

### 4.2 直接模式 combine 内核：逐 token 的反向路由

#### 4.2.1 概念说明

直接模式即 `num_scaleout_ranks == 1`（单节点，纯 NVLink 节点内通信，无 RDMA 跨节点）。此时 combine 内核 `combine_impl` 的任务很朴素：对本 rank 收到的每一个 token（共 `num_reduced_tokens` 个），查 `recv_src_metadata` 得到它的源地址，把专家输出 `x` 中对应的那一行写到**源 rank 的接收缓冲区**里。

这与 dispatch 是镜像的：dispatch 时 notify warps 统计“我收到了多少”，dispatch warps 把 token 写进对端 buffer；combine 时则是反过来读 `recv_src_metadata`，把本地专家输出写回对端 buffer。

#### 4.2.2 核心流程

`combine_impl` 的一个 CTA（一个 SM 上的一个 block）内部：

```
1. 计算 warp_idx（用 rank_idx 旋转，均衡 QP 负载），初始化 TMA mbarrier
2. gpu_barrier：确保所有 rank 的远程 buffer 已就绪（dispatch 写完、combine 才能读/写）
3. 把 num_reduced_tokens 均分给 (num_sms * num_warps) 个 warp，每个 warp 处理一段连续 token
4. 对每个 token i：
   a. 从 src_metadata[i] 解码出 src_rank_idx、src_token_idx、（expand 模式）top-k 槽位
   b. 判断 src_rank 是否 NVLink 可达：
        - 是 → 直接 TMA store 到对端 recv_buffer 对应位置（gin.get_sym_ptr 翻译对称指针）
        - 否 → 先 TMA store 到本地 send_buffer，再 gin.put 经 RDMA 发出
   c. 写 topk_weights（非 expand + 非 expanded-send 模式）
5. 末尾 gpu_barrier：确保所有 token 都已到达对端
```

第 4 步是反向路由的核心，下面精读。

#### 4.2.3 源码精读

**源地址解码**——`recv_src_metadata` 每行 `num_topk+2` 个 int，前两个 int 编码了源地址：

```cpp
// deep_ep/include/deep_ep/impls/combine.cuh
constexpr int kMetadataStride = 2 + kNumTopk;
const int src_token_idx   = __ldg(src_metadata + i * kMetadataStride) % kNumMaxTokensPerRank;
const int src_rank_topk_idx = __ldg(src_metadata + i * kMetadataStride + 1);
const int src_rank_idx  = src_rank_topk_idx / kNumTopk;
const int src_topk_idx  = src_rank_topk_idx % kNumTopk;
```

见 [combine.cuh:87-92](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh#L87-L92)。即：第 0 列存“全局源 token 索引”（`src_rank * num_max_tokens_per_rank + src_token_idx`，对 `num_max_tokens_per_rank` 取模恢复局部索引）；第 1 列把 `src_rank_idx` 和 `src_topk_idx` 打包成一个 int（除/模 `num_topk` 拆开）。第 2 列起（共 `num_topk` 个）是该 token 在 expand 模式下的各 top-k 槽位，用于本地归约（[combine.cuh:115-119](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh#L115-L119)）。

**NVLink 直达 vs RDMA 中转**——这是反向路由的“两条路”：

```cpp
// combine.cuh
const bool nvlink_bypass = gin.is_nvlink_accessible<team_t>(src_rank_idx);
layout::TokenLayout master_token_buffer = [=]() {
    if (nvlink_bypass) {                       // 节点内：NVLink 直达
        auto token_buffer = recv_buffer.get_rank_buffer(...).get_token_buffer(src_token_idx);
        token_buffer.set_base_ptr(gin.get_sym_ptr<team_t>(token_buffer.get_base_ptr(), src_rank_idx));
        return token_buffer;
    }
    return send_buffer.get_rank_buffer(src_rank_idx).get_token_buffer(src_token_idx);  // 节点间：先进 send_buffer
}();
```

见 [combine.cuh:94-106](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh#L94-L106)。`gin.get_sym_ptr` 是 NCCL Gin 后端的对称指针翻译（把“本 rank 视角下的地址”翻译成“目标 rank 视角下的同一块物理对称内存”），与 dispatch 中写对端 buffer 是同一套机制（u5-l1 已讲）。

**三个搬运分支**——拿到目标位置后，按 expand / 多次归约的组合分三种情况把 `x` 写过去：

```cpp
// combine.cuh: 3 cases:
//  - no expand + no reduce, or expand + no reduce   （无需本地归约，直接发）
//  - expand + reduce                                 （本地先归约多份副本再发）
//  - expand + send all                               （展开发送：每个 top-k 槽各发一份）
```

见 [combine.cuh:121-213](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh#L121-L213)。其中“直接发”分支用 TMA load 把 `x` 的一行搬进共享内存暂存区，再 TMA store 到目标位置（[combine.cuh:133-143](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh#L133-L143)）；“本地归约”分支调用 `combine_reduce` 把同一 token 的多个 top-k 副本在共享内存里加起来再发（[combine.cuh:144-176](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh#L144-L176)，归约实现见 [combine_utils.cuh:55-170](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_utils.cuh#L55-L170)）。

**RDMA 发送**——非 NVLink 直达时，写完 `send_buffer` 后由 warp leader 发起 RDMA：

```cpp
// combine.cuh
if (not kDoExpandedSend and not nvlink_bypass and ptx::elect_one_sync()) {
    ptx::tma_store_wait();
    const auto dst_ptr = recv_buffer.get_rank_buffer(...).get_token_buffer(src_token_idx).get_base_ptr();
    gin.put<team_t>(dst_ptr, master_token_buffer.get_base_ptr(),
                    master_token_buffer.get_num_bytes<false>(), src_rank_idx);
}
```

见 [combine.cuh:230-236](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh#L230-L236)。

**首尾 barrier**——内核开头与结尾各有一次 `gpu_barrier`：

```cpp
// combine.cuh 开头：确保对端 buffer 可用
comm::gpu_barrier<..., comm::kCombineTag0, false, false, true>(gin, workspace_layout, 0, rank_idx, sm_idx, thread_idx);
// ...逐 token 反向路由...
// 结尾：确保所有 token 到达对端
comm::gpu_barrier<..., comm::kCombineTag1, true, true, false>(gin, workspace_layout, 0, rank_idx, sm_idx, thread_idx);
```

见 [combine.cuh:76-80](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh#L76-L80) 与 [combine.cuh:239-242](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh#L239-L242)。开头 barrier 保证“dispatch 写完 + 对端 recv_buffer 就绪”；结尾 barrier 保证“本 rank 发出的 token 已全部落盘”，之后 reduce epilogue 才能安全读取。

**一个容易被忽略的细节——`num_reduced_tokens` 的兜底**：

```cpp
// combine.cuh
if (num_reduced_tokens == kNumMaxTokensPerRank * kNumRanks)
    num_reduced_tokens = __ldg(psum_num_recv_tokens_per_scaleup_rank + kNumRanks - 1);
```

见 [combine.cuh:45-46](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh#L45-L46)。这正是 u5-l4 提到的“无 CPU sync 模式”：host 按最坏上界 `num_max_tokens_per_rank * num_ranks` 传进来，内核发现等于上界时，改用 `psum` 末元素（真实接收数）来界定循环范围，避免扫描无意义的空行。

#### 4.2.4 代码实践

**实践目标**：跟踪一个 token 在 combine 中的反向路由路径，理解 `recv_src_metadata` 的解码。

**操作步骤**：

1. 在 [combine.cuh:82-92](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh#L82-L92) 处，假设 `num_max_tokens_per_rank=4096`、`num_topk=6`，且某 token 的 `src_metadata[i] = [4096*2 + 100, 2*6 + 3, ...]`（即第 0 列=8292，第 1 列=15）。
2. 手算：`src_token_idx`、`src_rank_idx`、`src_topk_idx` 分别是多少？
3. 若该 rank 与 `src_rank_idx` 在同一节点（NVLink 可达），token 会被写到哪个 buffer 的哪个位置？

**预期结果**：
- `src_token_idx = 8292 % 4096 = 100`
- `src_rank_idx = 15 / 6 = 2`
- `src_topk_idx = 15 % 6 = 3`
- 该 token 会被 TMA store 到 **rank 2 的 recv_buffer** 中（经 `gin.get_sym_ptr` 翻译后的对称地址），槽位由 `src_token_idx` 决定；具体落在 rank 槽还是 topk 槽取决于 `kUseRankLayout`（见 [combine.cuh:99](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh#L99)）。

> 数值结果待本地验证（实际索引还受 `use_rank_layout` 与缓冲区布局影响）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `combine_impl` 开头和结尾都需要 `gpu_barrier`？去掉结尾的 barrier 会怎样？

**答案**：开头 barrier 保证对端 `recv_buffer` 已被 dispatch 写好且对称内存注册就绪，避免读到未初始化数据；结尾 barrier 保证本 rank 经 NVLink/RDMA 发出的 token 全部到达对端，否则后续的 reduce epilogue（在同一缓冲区上归约）可能读到尚未到达的旧数据，导致归约结果错误。

**练习 2**：`combine_impl` 里 `warp_idx = (get_warp_idx() + rank_idx) % kNumWarps`（[combine.cuh:39](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh#L39)），为什么要加 `rank_idx` 再取模？

**答案**：这是一种 QP/通道负载均衡技巧。若所有 rank 的 warp 0 都映射到同一个 QP/通道，会造成拥塞；用 `rank_idx` 旋转后，不同 rank 的同一物理 warp 会落到不同逻辑 warp（进而不同 QP），把 RDMA 流量分散开。

### 4.3 launch_combine 的输入约束、缓冲区布局与 reduce_buffer

#### 4.3.1 概念说明

`launch_combine` 是 host 侧的 JIT 启动器（[combine.hpp:114-193](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/combine.hpp#L114-L193)）。它做三件事：决定 warp 数、生成并编译内核、启动。其中“决定 warp 数”与“缓冲区布局”直接关系到 combine 能否跑通、占多少显存。

一个关键概念是 **`reduce_buffer`**：主内核 `combine_impl` 不直接产出最终结果，而是把 token 写进一块**中间缓冲区**（就是 `launch_combine` 的返回值 `reduce_buffer`），再由 reduce epilogue 在这块缓冲区上做加权归约。这块缓冲区复用的是 dispatch/combine 共享的那块 NCCL 对称窗口里的 GPU buffer 区段。

#### 4.3.2 核心流程

`launch_combine` 的内部逻辑：

```
1. 按“最大化共享内存利用率”算 warp 数：
     num_warps = min(num_smem_bytes / 一个 token 的打包字节数, 32)
2. 若是 hybrid（num_scaleout_ranks > 1）：
     校验 num_channels 能被 num_sms 整除
     num_scaleup_warps = num_forward_warps = num_channels / num_sms
     num_warps = scaleup + forward
3. JIT：generate（按模式选 combine_impl 或 hybrid_combine_impl）→ build → launch
4. 返回 reduce_buffer 指针：
     直接模式 → 返回 buffer 本身
     hybrid 模式 → 跳过 scaleup buffer 区段，返回 scaleup_buffer 之后的位置
```

#### 4.3.3 源码精读

**输入约束（host 校验）**——`ElasticBuffer::combine` 对 `x` 的强约束：

```cpp
// csrc/elastic/buffer.hpp
EP_HOST_ASSERT(x.scalar_type() == torch::kBFloat16);                 // 必须 BF16
EP_HOST_ASSERT((x.size(1) * x.element_size()) % sizeof(int4) == 0);  // hidden 必须 16 字节对齐
EP_HOST_ASSERT(num_combined_tokens <= num_max_tokens_per_rank);
```

见 [buffer.hpp:1203-1214](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1203-L1214)。BF16 + 16 字节对齐是为了让内核用 `int4`（16 字节）向量读写、配合 TMA 大块搬运。注意：dispatch 支持 FP8 输入，但 **combine 只支持 BF16**——FP8 专家输出需要先反量化成 BF16 再 combine。

**warp 数计算**——直接模式按共享内存容量最大化 warp：

```cpp
// csrc/kernels/elastic/combine.hpp
const auto token_layout = get_combine_token_layout(hidden, sizeof(nv_bfloat16), num_topk);
auto num_warps = std::min(num_smem_bytes / token_layout.get_num_bytes<true>(), 32);
```

见 [combine.hpp:134-135](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/combine.hpp#L134-L135)。注意这里的 `token_layout` 用了 `get_combine_token_layout`，它的 SF 段大小是 0（[combine.hpp:109-112](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/combine.hpp#L109-L112)），因为 combine 不传 scaling factor。

**hybrid 模式的 channel 整除约束**：

```cpp
// combine.hpp
if (num_scaleout_ranks > 1) {
    EP_HOST_ASSERT(num_channels % num_sms == 0 and ...);   // channel 数必须能被 SM 数整除
    EP_HOST_ASSERT(num_channels / num_sms <= 16);
    num_scaleup_warps = num_forward_warps = num_channels / num_sms;
    num_warps = num_scaleup_warps + num_forward_warps;
    EP_HOST_ASSERT(num_warps * token_layout.get_num_bytes<true>() <= num_smem_bytes and
                   "Invalid combine SM count, please try to match your dispatch config");
}
```

见 [combine.hpp:138-148](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/combine.hpp#L138-L148)。这条断言很重要：**combine 的 SM 数必须和 dispatch 一致**，否则 channel 划分对不上。这也是为什么 Python 层 `combine` 默认 `num_sms=handle.num_sms`（[elastic.py:1086](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L1086)）——复用 dispatch 的 SM 数。

**缓冲区布局与 reduce_buffer 的返回**——直接模式与 hybrid 模式返回不同的位置：

```cpp
// combine.hpp
if (num_scaleout_ranks == 1)
    return buffer;                       // 直接模式：整个 buffer 就是 reduce_buffer

// hybrid 模式：跳过 scaleup buffer 区段
const bool is_scaleup_buffer_rank_layout = allow_multiple_reduction ? (num_scaleup_ranks <= num_topk) : false;
const auto scaleup_buffer = layout::BufferLayout<false>(
    token_layout,
    is_scaleup_buffer_rank_layout ? num_scaleup_ranks : num_topk,
    num_scaleout_ranks * num_max_tokens_per_rank,
    buffer);
return scaleup_buffer.get_buffer_end_ptr();   // reduce_buffer 在 scaleup buffer 之后
```

见 [combine.hpp:180-192](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/combine.hpp#L180-L192)。hybrid 模式下缓冲区是分层的：`scaleup_recv_buffer → scaleout_recv_buffer → scaleout_send_buffer`（见 [hybrid_combine.cuh:69-78](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_combine.cuh#L69-L78)），reduce epilogue 应该作用在跨节点归约后的 `scaleout_recv_buffer` 上，所以要跳过前面的 scaleup 段。

**缓冲区总大小**由 `get_combine_buffer_size` 决定（[buffer.hpp:616-650](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L616-L650)），它把 send_buffer 与 recv_buffer 的字节相加；最终 `calculate_buffer_size` 取 dispatch 与 combine 的**最大值**对齐 2MB（[buffer.hpp:679-685](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L679-L685)），因为两者分时复用同一块内存。

**`use_rank_layout` 优化**——当 `allow_multiple_reduction` 开启且 rank 数 ≤ top-k 时，接收缓冲区按“rank 槽”而非“topk 槽”布局，省空间：

```cpp
// deep_ep/include/deep_ep/impls/combine_utils.cuh
template <bool kAllowMultipleReduction, int kNumRanks, int kNumTopk>
constexpr bool use_rank_layout() {
    if constexpr (not kAllowMultipleReduction)
        return false;
    return kNumRanks <= kNumTopk;
}
```

见 [combine_utils.cuh:8-18](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_utils.cuh#L8-L18)。这是 combine 布局里一个隐藏的“省显存”开关。

#### 4.3.4 代码实践

**实践目标**：理解 combine 的缓冲区布局，并验证“combine SM 数须与 dispatch 一致”这一约束。

**操作步骤**：

1. 阅读 [combine.hpp:138-148](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/combine.hpp#L138-L148) 的 hybrid 断言。
2. 在单机 8 卡环境，故意用 `buffer.dispatch(..., num_sms=N)` 然后 `buffer.combine(handle, num_sms=M)`（`N != M`），观察是否触发 `Invalid combine SM count` 断言（多节点场景）。
3. 对照 [buffer.hpp:616-650](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L616-L650)，给定 `num_max_tokens_per_rank=4096`、`hidden=7168`、`num_topk=6`、`num_ranks=8`、`is_scaleup_nvlink=true`、`allow_multiple_reduction=false`，手算直接模式 combine 的 `send_buffer + recv_buffer` 字节数量级。

**预期结果**：单机（`num_scaleout_ranks==1`）不会触发 channel 整除断言（那条只在 hybrid 分支）；多节点下 SM 数不匹配会直接断言失败。手算时注意 `is_scaleup_nvlink=true` 时 send_buffer 第一维为 0（节点内无需 RDMA 中转，见 [buffer.hpp:626-627](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L626-L627)），主要开销在 recv_buffer。

> 精确字节数待本地验证（需考虑 `TokenLayout` 的 32 字节对齐填充）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 combine 强制 `x` 是 BF16，而 dispatch 允许 FP8？

**答案**：dispatch 发的是“输入”（可低精度 FP8 传输省带宽）；combine 发的是“专家计算后的输出”，需要保留精度用于后续加权归约，所以强制 BF16。若专家输出是 FP8，用户需先反量化成 BF16 再调 combine。

**练习 2**：hybrid 模式下 `launch_combine` 为什么返回 `scaleup_buffer.get_buffer_end_ptr()` 而不是 `buffer`？

**答案**：hybrid 缓冲区分三段（scaleup_recv → scaleout_recv → scaleout_send）。reduce epilogue 要在跨节点归约完成的 `scaleout_recv_buffer` 上工作，而 `scaleup_buffer.get_buffer_end_ptr()` 正好指向 scaleout 段的起点（直接模式下三段退化为一段，故直接返回 `buffer`）。

### 4.4 hybrid combine：两级反向路由与 channel_linked_list 重放

#### 4.4.1 概念说明

多节点（`num_scaleout_ranks > 1`）时，combine 也走两级：**scaleup（NVLink 节点内）+ scaleout（RDMA 跨节点）**，与 hybrid dispatch 对称。但 combine 多了一个难点：dispatch 时 token 是“从远到近”汇聚的，到了 combine，每个 rank 手里是“我收到的 token 经专家处理后的输出”，要把它们送回各自的**原始** rank——这个原始 rank 可能在别的节点。

于是 hybrid combine 复用了 dispatch 阶段写入的两份元数据：

- `token_metadata_at_forward`：dispatch 的 forward 阶段记录的“每个转发 token 的元数据”。
- `channel_linked_list`：把零散到达的 token 按 channel + scaleup peer 串成的链表。

combine 用它们“重放”一遍 dispatch 的转发路径，只是方向相反。

#### 4.4.2 核心流程

`hybrid_combine_impl` 把一个 CTA 的 warp 分成两类：

- **scaleup warps**（前 `num_scaleup_warps` 个）：遍历 `channel_linked_list`，对每个收到的 token，查 `src_metadata` 解码源地址，把专家输出经 NVLink 写到对端节点的 scaleup buffer（或本地归约）。
- **forward warps**（后 `num_forward_warps` 个）：遍历 `token_metadata_at_forward`，“重放 dispatch 的 forward 路径”——把 scaleup buffer 里收到的 token 再经 RDMA `gin.put` 转发到目标 scaleout rank。

二者通过 workspace 里的 tail 信号（`st_release_sys` / `ld_acquire_sys`）做生产者-消费者同步，与 hybrid dispatch 的 signaled tail 机制一致。

#### 4.4.3 源码精读

**缓冲区分层布局**：

```cpp
// deep_ep/include/deep_ep/impls/hybrid_combine.cuh
auto scaleup_buffer = layout::BufferLayout<false>(token_layout, kNumTokensInScaleupLayout,
    kNumScaleoutRanks * kNumMaxTokensPerRank, buffer);
auto scaleout_recv_buffer = layout::BufferLayout<false>(token_layout, kNumTokensInScaleoutLayout,
    kNumMaxTokensPerRank, scaleup_buffer.get_buffer_end_ptr());
auto scaleout_send_buffer = layout::BufferLayout<false>(token_layout,
    kAllowMultipleReduction ? 1 : kNumTopk,
    kNumChannels * (kNumScaleoutRanks * kNumMaxTokensPerChannel),
    scaleout_recv_buffer.get_buffer_end_ptr());
```

见 [hybrid_combine.cuh:69-78](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_combine.cuh#L69-L78)。三段顺序拼接，正是 4.3 中 `launch_combine` 要“跳过 scaleup 段”的原因。

**scaleup warps 遍历 channel_linked_list**——这是“反向路由”的入口：

```cpp
// hybrid_combine.cuh: 形状 [num_channels, num_max_tokens_per_channel+1, num_scaleup_ranks]
// [i, j, k] 表示：从 channel i、scaleup peer k 的链表中第 j 项的 token 索引
stored_token_idx[i] = __ldg(channel_linked_list +
      channel_idx * (kNumScaleoutRanks * kNumMaxTokensPerChannel + 1) * kNumScaleupRanks +
      stored_ll_idx[i] * kNumScaleupRanks + j);
```

见 [hybrid_combine.cuh:150-167](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_combine.cuh#L150-L167)。链表以尾指针 + 0 号 head 哨兵串联（u5-l2 已讲其构造），combine 这里只是消费它：沿链表逐项取出 token 索引，查 `src_metadata` 得到源地址（两级解码：`src_scaleout_rank_idx = src_global_token_idx / (num_max_tokens_per_rank * num_scaleup_ranks)`，见 [hybrid_combine.cuh:202-204](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_combine.cuh#L202-L204)），然后 TMA store 到对端 scaleup buffer（[hybrid_combine.cuh:205-216](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_combine.cuh#L205-L216)）。

**forward warps 重放 dispatch**——读 `token_metadata_at_forward`，把 scaleup 收到的 token 经 RDMA 发到目标 scaleout rank：

```cpp
// hybrid_combine.cuh: "Replay the dispatch"
for (int i = 0; ; ++ i) {
    const auto src_token_global_idx = __ldg(token_metadata_at_forward + i * kNumForwardMetadataDims);
    const auto is_token_last_in_chunk = __ldg(token_metadata_at_forward + i * kNumForwardMetadataDims + 1);
    const auto src_rank_idx = src_token_global_idx / kNumMaxTokensPerRank;
    const auto src_scaleout_rank_idx = src_rank_idx / kNumScaleupRanks;
    ...
    if (src_scaleout_rank_idx != scaleout_rank_idx) {
        gin.put<ncclTeamTagRail>(recv_buffer_ptr, send_buffer_ptr,
            token_layout.get_num_bytes<false>(), src_scaleout_rank_idx, ...);
    }
}
```

见 [hybrid_combine.cuh:389-494](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_combine.cuh#L389-L494)。注意 `token_metadata_at_forward` 的每一项还缓存了 top-k 的 scaleup peer 与 slot 索引（[hybrid_combine.cuh:397-400](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_combine.cuh#L397-L400)），让 forward 不必重新查 `src_metadata`。

**生产者-消费者同步**——scaleup warps 周期性地用 `st_release_sys` 把“已发送 token 数”写进 channel tail（[hybrid_combine.cuh:127-148](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_combine.cuh#L127-L148)），forward warps 用 `ld_acquire_sys` 轮询该 tail（[hybrid_combine.cuh:416-446](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_combine.cuh#L416-L446)），超时则打印诊断（与 hybrid dispatch 的 timeout 打印风格一致）。

#### 4.4.4 代码实践

**实践目标**：理解 hybrid combine 复用 dispatch 元数据的机制。

**操作步骤**：

1. 对照 [buffer.hpp:1258-1281](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1258-L1281)，确认 host 层对 `token_metadata_at_forward`（形状 `[num_channels, num_scaleout_ranks*num_max_tokens_per_channel+1, 2+num_topk*2]`）与 `channel_linked_list`（形状 `[num_channels, ..., num_scaleup_ranks]`）的断言。
2. 在 [hybrid_combine.cuh:150-167](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_combine.cuh#L150-L167) 与 [hybrid_combine.cuh:389-402](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_combine.cuh#L389-L402) 分别确认 scaleup warps 与 forward warps 读的是哪份元数据。
3. 解释：为什么这两份元数据必须由 **dispatch** 阶段写好、combine 阶段只读？

**预期结果**：dispatch 是 token“汇聚”的过程，只有它知道每个 token 经过了哪条 channel、被哪条 forward 路径转发；combine 是逆过程，必须复用 dispatch 记录的转发拓扑，否则无法重建跨节点的反向通路。所以这两份元数据是 dispatch 产出、combine 消费的“单向账本”。

> 多节点运行需真实 RDMA 集群，单机无法触发 hybrid 分支（`num_scaleout_ranks==1` 时直接走 `combine_impl`）。源码阅读型实践，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：hybrid combine 里 scaleup warps 与 forward warps 各负责哪一级通信？它们如何同步？

**答案**：scaleup warps 负责 NVLink 节点内（scaleup 级），把 token 写到对端节点的 scaleup buffer；forward warps 负责 RDMA 跨节点（scaleout 级），把 scaleup 收到的 token 转发到目标 scaleout rank。两者通过 workspace 里的 channel tail 信号同步：scaleup warps 用 `st_release_sys` 更新“已发送数”，forward warps 用 `ld_acquire_sys` 轮询。

**练习 2**：为什么 hybrid combine 末尾没有像直接模式那样的 `gpu_barrier`（[hybrid_combine.cuh:623](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_combine.cuh#L623) 注释 "No barrier at epilogue"）？

**答案**：hybrid combine 的同步已经由 forward warps 末尾的 signaled tail 等待（[hybrid_combine.cuh:599-619](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_combine.cuh#L599-L619)）完成——每个 channel 都确认所有 scaleout peer 的 RDMA 到达后才退出，等价于一次隐式的逐 channel barrier，故无需再加全局 grid barrier。

## 5. 综合实践

把本讲知识串起来：**跟踪一个 token 在 dispatch → 专家计算 → combine 中的完整往返**。

1. **准备**：阅读 [`tests/elastic/test_ep.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py) 中 `test_dispatch_combine` 的非 expand 用例，确认它调用了 `dispatch` 得到 `handle`，模拟专家计算（通常是对 `recv_x` 做一次逐元素操作），再调 `combine(x, handle, topk_weights)`。

2. **观察 EPHandle**：在 dispatch 后打印 `handle.recv_src_metadata.shape`、`handle.psum_num_recv_tokens_per_scaleup_rank`、`handle.do_expand`，验证形状符合本讲 4.1.3 的表格。

3. **验证反向路由**：combine 返回的 `combined_x` 形状应为 `[num_combined_tokens, hidden]`。对照 [`deep_ep/utils/refs.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/refs.py) 的 `ref_combine`（纯 NCCL/PyTorch 参考实现），用 `torch.allclose` 比对 DeepEP combine 与参考实现的结果，确认反向路由正确。

4. **切换布局**：把 dispatch 改成 `do_expand=True`，重新跑 combine，观察 `topk_weights` 必须从 2D `[num_tokens, num_topk]` 改成 1D `[num_tokens]`（否则触发 [buffer.hpp:1225-1235](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1225-L1235) 的断言）。

5. **画图**：画出“token 从 rank A 发出 → 在 rank B 被专家处理 → combine 推回 rank A 并归约”的数据流图，标注每一步用到的 `EPHandle` 字段。

> 数值正确性比对需在真实多 GPU 环境运行（待本地验证）；源码阅读与流程梳理部分可离线完成。

## 6. 本讲小结

- **combine 是 dispatch 的逆过程**，不重新算路由，而是“重放”`EPHandle` 里 dispatch 已写好的路由元数据。
- 反向路由的主表是 `recv_src_metadata`（每行编码一个收到的 token 的源 rank + 源位置 + top-k 槽位）；规模由 `psum_num_recv_tokens_per_scaleup_rank` 末元素给出，无 CPU sync 时内核自行从最坏上界兜底。
- 直接模式 `combine_impl` 逐 token 解码源地址，NVLink 可达则 TMA store 直达对端，否则经 `send_buffer` + `gin.put` 走 RDMA；首尾各一次 `gpu_barrier` 保证可见性。
- combine 强制 **BF16 + 16 字节对齐**输入；`launch_combine` 按“最大化共享内存”定 warp 数，hybrid 模式要求 **SM 数与 dispatch 一致**；返回的 `reduce_buffer` 是 reduce epilogue 的工作区（hybrid 下需跳过 scaleup 段）。
- `use_expanded_layout`（=`handle.do_expand`）决定 `topk_weights` 形状：非 expand 为 2D `[num_tokens, num_topk]`，expand 为 1D `[num_tokens]`。
- hybrid combine 复用 dispatch 产出的 `token_metadata_at_forward` 与 `channel_linked_list`，scaleup warps 遍历链表做节点内反向路由，forward warps 重放 forward 路径做跨节点 RDMA，二者靠 signaled tail 同步。

## 7. 下一步学习建议

- **u6-l2（Combine reduce epilogue 与 multiple reduction）**：本讲只讲到 token 被推进 `reduce_buffer`，真正的“加权归约 + bias 叠加”发生在 reduce epilogue，下一讲深入 `combine_reduce_epilogue` 与 `allow_multiple_reduction` 的精度/传输权衡。
- **u6-l3（确定性排序）**：combine 输出的 token 位序受多 channel/多 rank 到达顺序影响，若需位序确定，看 `EPHandle.deterministic_sort` 如何在 event 等待后排序。
- **自行阅读**：[`deep_ep/utils/refs.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/refs.py) 的 `ref_combine` 是理解 combine 语义的最佳参照——它用纯 PyTorch 实现了同样的反向路由与归约，可与内核行为逐一对照。
