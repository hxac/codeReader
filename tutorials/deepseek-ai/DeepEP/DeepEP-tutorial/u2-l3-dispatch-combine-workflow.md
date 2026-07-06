# MoE Dispatch/Combine 的 Python 工作流与 EPHandle

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `ElasticBuffer.dispatch` 与 `ElasticBuffer.combine` 各自的**输入输出张量形状与类型约束**，包括 BF16 与 FP8（`(data, scale)` 元组）两种数据格式。
- 逐字段解释 `EPHandle`（路由句柄）中 `psum_num_recv_tokens_per_expert`、`recv_src_metadata`、`dst_buffer_slot_idx` 等元数据的含义，并理解它们如何把 dispatch 和 combine 这对“互为反向”的通信串起来。
- 区分三种 dispatch 执行模式——`do_cpu_sync` 同步计数、cached handle 复用布局、无同步最坏情况分配——并知道它们分别适用于训练前向/反向与推理解码的哪一种场景。

本讲只到 **Python 接口层 + C++ host 层** 的边界，即“参数怎么传、句柄怎么用、计数怎么读”。真正的 GPU kernel（`launch_dispatch`、`launch_combine`）内部细节留到 U5/U6。

## 2. 前置知识

阅读本讲前，请先建立以下直觉（在 u1-l1、u2-l2 已讲过）：

- **专家并行（EP）与 dispatch/combine**：MoE 模型里每个 token 会被路由到若干专家。dispatch 把本 rank 的 token 按 `topk_idx` 发到目标专家所在的 rank；combine 则把专家计算后的输出送回原始 rank 并按 `topk_weights` 加权归约。两者互为逆过程。
- **rank 与拓扑**：`num_ranks` 是通信组里的总进程数；本 rank 收到的 token 来自其它所有 rank。在 V2 里逻辑域被拆成 `num_scaleout_ranks × num_scaleup_ranks`（详见 u3-l1），但**本讲只关心直接模式**（单节点 `num_scaleout_ranks == 1`），hybrid 两级模式留到 u5-l2。
- **`ElasticBuffer`** 是 V2 唯一的通信缓冲区对象（u2-l2），它内部已经分配好了 NCCL 对称内存窗口。dispatch/combine 不再分配通信缓冲，而是在这块预分配好的窗口上读写。
- **PyTorch 流（stream）**：默认所有 op 在“计算流”上执行。DeepEP 还会用到一条独立的“通信流”，通过事件（event）与计算流同步——这是下一讲 u2-l4 的主题，本讲你只需知道 dispatch/combine 的返回值里有一个 `event`。

一个关键直觉：**dispatch 的输出形状依赖于运行时数据**。本 rank 到底会收到多少 token、每个专家分到几个 token，要等通信真正发生后才知道。这就引出了本讲的核心张力——“计数从哪来、何时能拿到”——它直接决定了 `EPHandle` 的字段设计和三种 dispatch 模式。

## 3. 本讲源码地图

本讲涉及两个关键源码文件：

| 文件 | 作用 |
| --- | --- |
| [deep_ep/buffers/elastic.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py) | Python 接口层。定义 `EPHandle` 类、`ElasticBuffer.dispatch` 与 `ElasticBuffer.combine` 两个用户 API，负责参数解包、SM/QP 自动决策、句柄拼装与确定性排序钩子。 |
| [csrc/elastic/buffer.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) | C++ host 实现（被 pybind11 暴露为 `_C.ElasticBuffer`）。`ElasticBuffer::dispatch` / `ElasticBuffer::combine` 负责张量校验、计数读取（CPU 轮询）、输出张量分配、以及调用 `launch_dispatch` / `launch_combine` 启动 GPU kernel。 |

调用方向（承接 u1-l2）：

```
用户 Python
  └─ ElasticBuffer.dispatch()          # elastic.py:855   参数解包 + SM/QP 决策
       └─ self.runtime.dispatch(...)    # pybind11 进 C++
            └─ ElasticBuffer::dispatch  # buffer.hpp:702   校验 + 计数 + 分配 + launch_dispatch
                 └─ launch_dispatch_copy_epilogue   # buffer.hpp:1127  拷贝 epilogue
```

---

## 4. 核心概念与源码讲解

### 4.1 dispatch：把 token 路由到目标专家所在 rank

#### 4.1.1 概念说明

dispatch 解决的问题是：本 rank 手里有 `num_tokens` 个 token，每个 token 经过 gate 后选中了 `num_topk` 个专家（记录在 `topk_idx` 里）。这些专家可能分布在本 rank，也可能在别的 rank。dispatch 要按 `topk_idx` 把 token 的数据（以及可选的 FP8 缩放因子、权重）送到对应的 rank，并让每个 rank 知道“我收到了哪些 token、它们原本属于谁、原本要送给我的哪几个专家”。

这里有几个**输入输出约定**值得专门记住：

- 输入 `x` 有两种形态：BF16 下是单个张量 `[num_tokens, hidden]`；FP8 下是元组 `(data, scale)`，其中 `data` 是 `[num_tokens, hidden]` 的 `torch.float8_e4m3fn`，`scale` 是缩放因子。
- `topk_idx` 形状 `[num_tokens, num_topk]`，类型是 `deep_ep.topk_idx_t`（通常 `torch.int64`），值 `-1` 表示“这一路不发”（被 mask 掉的 expert）。
- 输出 `recv_x` 与输入 `x` 同型：BF16 进就 BF16 出，FP8 进就 `(data, scale)` 元组出。

#### 4.1.2 核心流程

dispatch 在 Python 层做的事情可以概括为五步：

1. **自动决定 SM/QP 数**：若用户没传 `num_sms`/`num_qps`，用 `get_theoretical_num_sms` / `get_theoretical_num_qps` 解析式估算（u3-l3 详讲）。
2. **解包 FP8 与 cached handle**：把 `x` 拆成 `(data, sf)`；若传入了 `handle`，从中复用 `topk_idx` 等布局信息。
3. **进入 C++ `runtime.dispatch`**：把所有参数（含 cached 字段）一次性传下去，C++ 侧完成校验、计数读取、输出张量分配、`launch_dispatch` 与 copy epilogue。
4. **拼装 `EPHandle`**：把 C++ 返回的元数据张量包成 `EPHandle` 对象。
5. **确定性排序钩子**（可选）：若构造 buffer 时 `deterministic=True`，把 `handle.deterministic_sort` 挂到 event 等待之后。

伪代码：

```text
def dispatch(x, topk_idx, topk_weights, ..., handle=None, do_cpu_sync=None, do_expand=False):
    num_topk = (handle.topk_idx if topk_idx is None else topk_idx).shape[1]
    num_sms  = get_theoretical_num_sms(...)  if num_sms  == 0 else num_sms
    num_qps  = get_theoretical_num_qps(...)  if num_qps  == 0 else num_qps
    x, sf    = x if isinstance(x, tuple) else (x, None)   # FP8 解包
    if handle is not None:                                 # cached 模式
        topk_idx = handle.topk_idx; do_cpu_sync = False
    结果 = self.runtime.dispatch(x, sf, topk_idx, ..., cached_*, do_cpu_sync, do_expand, ...)
    if not is_cached_dispatch:
        handle = EPHandle(结果里的元数据张量...)
    return recv_x, recv_topk_idx, recv_topk_weights, handle, EventOverlap(event)
```

#### 4.1.3 源码精读

**① Python `dispatch` 的签名与返回类型**——注意 FP8 用 `Union[Tensor, Tuple[Tensor, Tensor]]` 表达，返回值固定 5 元组：

[deep_ep/buffers/elastic.py:855-876](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L855-L876) 定义了 dispatch 的全部参数与返回签名。其中 `x` 既可单张量（BF16）也可元组（FP8）；返回 `(recv_x, recv_topk_idx, recv_topk_weights, handle, event_overlap)`。

**② 自动 SM/QP 决策与 FP8 解包**——这是 dispatch 进入 C++ 前的最后一道预处理：

[deep_ep/buffers/elastic.py:927-933](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L927-L933) 先从 `topk_idx` 推出 `num_topk`，再决定 `num_sms`/`num_qps`，最后把 FP8 的 `x` 拆成 `(x, sf)`。

**③ cached handle 复用**——传入了 `handle` 时，强制 `do_cpu_sync=False`，并断言 `topk_idx` 必须为 `None`（从 handle 取）：

[deep_ep/buffers/elastic.py:937-948](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L937-L948) 处理 cached 分支。注意倒数两行的元组相等断言：cached 模式要求本次调用与原 handle 的 `num_experts / expert_alignment / num_max_tokens_per_rank` 完全一致，否则布局复用就是错的。

**④ 真正进入 C++**——把上面解包出的 cached 字段（通过 `_unpack_handle` 展开）连同运行参数一起传给 `runtime.dispatch`：

[deep_ep/buffers/elastic.py:964-996](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L964-L996) 是 Python→C++ 的边界调用，参数列表很长，因为 cached 字段要逐个透传。

**⑤ C++ 侧的输出张量分配**——`recv_x` 的行数取决于 `do_expand`：

[csrc/elastic/buffer.hpp:1073-1083](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1073-L1083) 这一段是理解输出形状的关键：

```cpp
// `recv_src_metadata` includes source token indices and buffer slot indices
const auto num_allocated_tokens = do_expand ? num_expanded_tokens : num_recv_tokens;
auto recv_x = torch::empty({num_allocated_tokens, hidden}, x.options());
...
auto recv_src_metadata = cached_mode ?
    cached_recv_src_metadata.value() :
    torch::empty({num_recv_tokens, num_topk + 2},
                 torch::TensorOptions(torch::kCUDA).dtype(torch::kInt));
```

也就是说：

- 非 expand 模式下，`recv_x` 行数 = `num_recv_tokens`（实际收到的 token 数）。
- expand 模式下，`recv_x` 行数 = `num_expanded_tokens`（每个 token 在每个选中专家处占一槽，且按 `expert_alignment` 对齐）。
- `recv_src_metadata` 形状恒为 `[num_recv_tokens, num_topk + 2]`，前 2 列存源信息、后 `num_topk` 列存每个 top-k 选择的 buffer 槽位。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：在不跑分布式的前提下，通过阅读源码搞清楚“`recv_x` 的形状由谁决定”。

**操作步骤**：

1. 打开 [deep_ep/buffers/elastic.py:855](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L855) 的 `dispatch` 签名，找到 `do_expand: bool = False` 这个参数。
2. 跳到 C++ 侧 [csrc/elastic/buffer.hpp:1075](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1075)，确认 `num_allocated_tokens = do_expand ? num_expanded_tokens : num_recv_tokens`。
3. 再往上找到 `num_recv_tokens` 与 `num_expanded_tokens` 的来源：[csrc/elastic/buffer.hpp:1006-1071](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1006-L1071)。

**需要观察的现象**：你会发现 `num_recv_tokens` 在三种 dispatch 模式下取值完全不同（见 4.4）。这正是 DeepEP 输出形状“运行时才确定”的根源。

**预期结果**：你能用自己的话回答“为什么 `recv_x` 不能在 dispatch 之前就分配好固定大小”。

#### 4.1.5 小练习与答案

**练习 1**：FP8 模式下，dispatch 的输入 `x` 是 `(data, scale)` 元组。请指出代码里哪一行把它拆开成 `x` 和 `sf`，以及 `recv_sf` 又是怎么重新打包回返回值的。

**答案**：在 [deep_ep/buffers/elastic.py:933](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L933) 用 `x, sf = x if isinstance(x, tuple) else (x, None)` 拆开；在 [deep_ep/buffers/elastic.py:1030](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L1030) 用 `recv_x = (recv_x, recv_sf) if recv_sf is not None else recv_x` 重新打包。

**练习 2**：为什么 cached 模式下要断言 `(num_experts, expert_alignment, num_max_tokens_per_rank)` 与原 handle 完全一致？

**答案**：因为 cached 模式复用的是原 handle 里**预计算好的布局张量**（如 `dst_buffer_slot_idx`、`psum_num_recv_tokens_per_expert`），这些张量的形状与含义由 `num_experts / expert_alignment / num_max_tokens_per_rank` 三者共同决定。三者中任何一个变了，旧布局就不再适用，复用会写出越界或错位的数据。

---

### 4.2 combine：把专家输出推回原 rank 并按权重归约

#### 4.2.1 概念说明

combine 是 dispatch 的“逆过程”。dispatch 之后，本 rank 收到了属于自己那 `num_local_experts` 个专家的 token，做完专家 FFN 计算，得到输出 `x`。现在要把这些输出**送回它们各自的原始 rank**，并在原始 rank 上**按 `topk_weights` 加权累加**（因为同一个原始 token 可能被多个专家处理，需要把多个专家的输出加权求和还原成一个 token）。

combine 必须依赖 dispatch 返回的 `handle`——因为“这个 token 原本属于谁、权重是多少、在 buffer 哪个槽位”这些路由信息，只有 dispatch 阶段才知道，全部记在 `EPHandle` 里。

注意一个**类型约束**：combine 的输入 `x` 必须是 `torch.bfloat16`（即使 dispatch 用了 FP8，combine 也用 BF16）；且 `hidden * element_size` 必须是 16 字节（`sizeof(int4)`）的整数倍，这是为了对齐 NVLink/RDMA 的大块拷贝。

#### 4.2.2 核心流程

combine 在 Python 层比 dispatch 简单得多，因为它不需要决定计数——计数都来自 `handle`：

1. **复用 dispatch 的 SM 数**：`num_sms = handle.num_sms if num_sms == 0`。
2. **解包 bias**：`bias` 可以是 0/1/2 个张量（叠加到最终输出上）。
3. **进入 C++ `runtime.combine`**：把 `handle` 里的关键元数据（`recv_src_metadata`、`psum_num_recv_tokens_per_scaleup_rank`、`topk_idx`、`token_metadata_at_forward`、`channel_linked_list`、`do_expand`）逐个传下去。
4. **返回** `(combined_x, combined_topk_weights, EventOverlap)`。

伪代码：

```text
def combine(x, handle, topk_weights=None, bias=None, ...):
    num_sms = handle.num_sms if num_sms == 0 else num_sms
    num_qps = get_theoretical_num_qps(num_sms) if num_qps == 0 else num_qps
    bias_0, bias_1 = _unpack_bias(bias)
    combined_x, combined_topk_weights, event = self.runtime.combine(
        x, topk_weights, bias_0, bias_1,
        handle.recv_src_metadata, handle.topk_idx,
        handle.psum_num_recv_tokens_per_scaleup_rank,
        handle.token_metadata_at_forward, handle.channel_linked_list,
        handle.num_experts, handle.num_max_tokens_per_rank,
        num_sms, num_qps, ..., handle.do_expand)
    return combined_x, combined_topk_weights, EventOverlap(event)
```

#### 4.2.3 源码精读

**① Python `combine` 签名与返回**——注意 `topk_weights` 的形状随 `do_expand` 变化（2D vs 1D）：

[deep_ep/buffers/elastic.py:1046-1082](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L1046-L1082) 文档里明确：非 expand 模式下 `topk_weights` 是 `[num_tokens, num_topk]`，expand 模式下是 `[num_tokens]`（1D）。这是因为 expand 模式下每个槽已经对应一个确定的 top-k 选择，不再需要第二维区分。

**② Python `combine` 主体**——几乎全是参数透传：

[deep_ep/buffers/elastic.py:1086-1107](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L1086-L1107) 可以看到 combine 从 `handle` 一口气读了 `recv_src_metadata / topk_idx / psum_num_recv_tokens_per_scaleup_rank / token_metadata_at_forward / channel_linked_list / num_experts / num_max_tokens_per_rank / do_expand` 这么多字段——这就是“EPHandle 把 dispatch 与 combine 串起来”的具象体现。

**③ C++ `combine` 的类型与对齐校验**：

[csrc/elastic/buffer.hpp:1200-1204](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1200-L1204) 这几行断言体现了上面提到的硬约束：

```cpp
const auto [num_tokens, hidden] = get_shape<2>(x);
EP_HOST_ASSERT(x.is_cuda() and x.is_contiguous());
EP_HOST_ASSERT(x.scalar_type() == torch::kBFloat16);
EP_HOST_ASSERT((x.size(1) * x.element_size()) % sizeof(int4) == 0);
```

**④ combine 的两段式启动**——和 dispatch 一样，主 kernel + reduce epilogue：

[csrc/elastic/buffer.hpp:1285-1303](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1285-L1303) 调用 `launch_combine` 把数据写到对端 rank 的 `reduce_buffer`；随后 [csrc/elastic/buffer.hpp:1316-1329](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1316-L1329) 调用 `launch_combine_reduce_epilogue` 在本地做多 rank 的加权归约并叠加 bias。reduce epilogue 的细节留到 u6-l2。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：弄清 combine 从 `handle` 里到底取了哪些字段，以及 `topk_weights` 形状如何随 `do_expand` 变化。

**操作步骤**：

1. 阅读 [deep_ep/buffers/elastic.py:1091-1106](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L1091-L1106)，列出一个清单：combine 用到了 `handle.` 的哪几个属性。
2. 跳到 C++ 校验 [csrc/elastic/buffer.hpp:1225-1235](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1225-L1235)，看 `topk_weights` 在 `use_expanded_layout` 为真/假时分别要求几维。

**需要观察的现象**：`use_expanded_layout=True` 时 `topk_weights` 必须是 1D（`get_shape<1>`），`False` 时是 2D（`get_shape<2>`）。

**预期结果**：你能在不运行的情况下，预测出“expand 模式 dispatch 后，combine 的 `topk_weights` 该传 1D 还是 2D”。

**待本地验证**：若你想确认行为，可在 `tests/elastic/test_ep.py` 里找到 `reduced_combine_args`（expand 分支）与 `combine_args`（非 expand 分支），对照它们的 `topk_weights` 形状。

#### 4.2.5 小练习与答案

**练习 1**：combine 的输入 `x` 为什么必须是 BF16，哪怕 dispatch 用了 FP8？

**答案**：combine 阶段要做加权累加（reduce），对精度有要求，FP8 直接累加误差太大；同时 BF16 是 dispatch 数据的反向回流口径。代码里在 [csrc/elastic/buffer.hpp:1203](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1203) 用 `EP_HOST_ASSERT(x.scalar_type() == torch::kBFloat16)` 强制。

**练习 2**：combine 的 `bias` 参数最多能传几个张量？它们会叠加到哪里？

**答案**：最多 2 个，通过 `_unpack_bias` 解包成 `bias_0, bias_1`（见 [deep_ep/buffers/elastic.py:1035-1044](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L1035-L1044)）。它们在 reduce epilogue 里叠加到最终输出 `combined_x` 上（[csrc/elastic/buffer.hpp:1323](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1323) 的 `bias_ptrs[0], bias_ptrs[1]`）。

---

### 4.3 EPHandle：承载路由元数据，串联 dispatch 与 combine

#### 4.3.1 概念说明

`EPHandle` 是 dispatch 返回、combine 消费的“路由句柄”。它本身不持有 GPU buffer，只持有**描述路由结果的小张量与标量**。你可以把它理解成一张“发货单”：dispatch 是发货，发货时填好单子（每个收到的 token 原本属于谁、放在 buffer 哪个槽位、要送给哪几个专家）；combine 是按单子把货物退回原主并结账。

`EPHandle` 还有第二个用途：作为 **cached handle** 传给后续的 dispatch，跳过布局重算与 CPU 同步——这正是推理解码场景的核心优化（见 4.4）。

#### 4.3.2 核心流程：字段速查表

下表列出 `EPHandle` 的关键字段（按 [deep_ep/buffers/elastic.py:25-57](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L25-L57) 的类 docstring 整理）：

| 字段 | 形状 / 类型 | 含义 |
| --- | --- | --- |
| `do_expand` | bool | 是否使用了 expand（每专家一槽）布局 |
| `num_experts` / `expert_alignment` | int | 全局专家数 / 每专家接收 token 数的对齐粒度 |
| `num_max_tokens_per_rank` | int | 每 rank 最大 token 数（cached 模式校验用） |
| `num_sms` | int | dispatch 用的 SM 数（combine 默认复用） |
| `topk_idx` | `[num_tokens, num_topk]` | dispatch 时克隆下来的 top-k 专家索引 |
| `psum_num_recv_tokens_per_scaleup_rank` | `[num_scaleup_ranks]` int | 每个 scaleup rank 发来的 token 数的**去重前缀和**；末元素 = 收到的总 token 数 |
| `psum_num_recv_tokens_per_expert` | `[num_local_experts(+1)]` int | 每个本地专家接收 token 数的前缀和（expand 与非 expand 语义不同，见下） |
| `num_recv_tokens_per_expert_list` | Python `list[int]` | **CPU 侧**的每专家接收 token 数列表（仅 `do_cpu_sync` 或 cached 时有值） |
| `num_unaligned_recv_tokens_per_expert` | `[num_local_experts]` int | 每专家**未对齐**的真实接收数（仅 expand 模式填充） |
| `recv_src_metadata` | `[num_recv_tokens, num_topk+2]` int | 第 0 列：源 token 全局索引；后 `num_topk` 列：每个 top-k 选择的 buffer 槽位 |
| `dst_buffer_slot_idx` | 形状随模式变 | dispatch 写出的目标 buffer 槽位索引（cached 复用核心） |
| `token_metadata_at_forward` / `channel_linked_list` | 可选 | 仅 hybrid 模式（多节点）使用，留到 u5-l2 |
| `num_recv_tokens` / `num_expanded_tokens` | int | 接收总数 / expand 后总数（无 CPU sync 时不一定精确） |

**关于 `psum_num_recv_tokens_per_expert` 的两种语义**（这是最容易混淆的字段，docstring 在 [deep_ep/buffers/elastic.py:42-48](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L42-L48)）：

- **非 expand 模式**：它是普通的**包含式前缀和**（inclusive prefix sum），即 `psum[i]` = 前 `i+1` 个专家的对齐 token 数之和。
- **expand 模式**：`psum[i]` = “`i` 之前所有专家的对齐累计数” + “专家 `i` 的**未对齐**真实 token 数”。因此 recover 专家 `i` 的真实计数需要：
  \[
  \text{count}_i = \text{psum}[i] - \mathrm{align}(\text{psum}[i-1],\ \text{expert\_alignment})
  \]
  而专家 `i+1` 的起始偏移则是 \(\mathrm{align}(\text{psum}[i],\ \text{expert\_alignment})\)。

C++ 侧在 [csrc/elastic/buffer.hpp:813-816](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L813-L816) 的注释也印证了这一点：“for expand mode, the input is exclusive prefix sum, while for non-expand, it is inclusive”。

#### 4.3.3 源码精读

**① EPHandle 的构造**——只是把传入的张量与标量存起来，但**注意 topk_idx 的克隆**：

[deep_ep/buffers/elastic.py:59-98](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L59-L98) 是构造函数。注释 `# NOTES: remember to copy the original users' input to prevent uncasual modifications on them` 说明：是否克隆由 dispatch 的 `do_handle_copy` 控制（默认 True），目的是防止用户在 dispatch 之后改动 `topk_idx` 导致 combine 路由错乱。`num_recv_tokens` 注释了“May not be accurate without CPU sync”——没有 CPU sync 时它只是最坏情况上界。

**② EPHandle 怎么被 dispatch 构造出来**：

[deep_ep/buffers/elastic.py:999-1014](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L999-L1014) 把 C++ 返回的 16 个值（含 `num_recv_tokens_per_expert_list`、`psum_*`、`recv_src_metadata`、`dst_buffer_slot_idx` 等）打包成 `EPHandle`。注意 `is_cached_dispatch = handle is not None`：cached 模式下**不新建** handle，而是返回用户传入的那个（这样后续还能继续复用）。

**③ `num_recv_tokens_per_expert_list` 何时非空**——这是新手最容易踩的坑：

[csrc/elastic/buffer.hpp:1008-1071](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1008-L1071) 显示 `num_recv_tokens_per_expert_list` 这个 `std::vector<int>` 只在两个分支被填充：cached 模式（`cached_num_recv_tokens_per_expert_list.value()`，行 1015）或 `do_cpu_sync` 模式（`push_back(count)`，行 1038）。**无 CPU sync 且非 cached 时它是空的**。所以如果你的代码要读这个 list，必须保证 dispatch 用了 `do_cpu_sync=True`（默认）或传入了 cached handle。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：在 dispatch 之后，把 `handle` 的关键字段打印出来，理解每个字段代表什么。

**操作步骤**（需要在多 GPU 环境运行，单机 8 卡）：

1. 在 `tests/elastic/test_ep.py` 的 `test_dispatch_combine` 里、dispatch 调用之后（约 [test_ep.py:152](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L152) 拿到 `handle` 之后），临时加几行打印：

```python
# 示例代码（仅用于阅读/调试，非项目原有代码）
if dist.get_rank() == 0:
    print('num_recv_tokens =', handle.psum_num_recv_tokens_per_scaleup_rank[-1].item())
    print('per-expert (CPU list) =', handle.num_recv_tokens_per_expert_list)
    print('per-expert (GPU tensor) =', handle.psum_num_recv_tokens_per_expert.tolist())
    print('recv_src_metadata shape =', tuple(handle.recv_src_metadata.shape))
```

2. 用默认参数跑一次：`torchrun --nproculocal 8 tests/elastic/test_ep.py ...`（具体命令见 u1-l4）。

**需要观察的现象**：

- `num_recv_tokens_per_expert_list`（CPU list）的长度等于本 rank 的本地专家数 `num_local_experts = num_experts // num_ranks`。
- `recv_src_metadata` 的列数 = `num_topk + 2`。
- `psum_num_recv_tokens_per_scaleup_rank[-1]` 等于 `sum(num_recv_tokens_per_expert_list)`（去重后的总接收数）。

**预期结果**：你能对照 4.3.2 的字段表，把每个打印值与含义对上号。

**待本地验证**：实际数值取决于随机 `topk_idx` 与 `unbalanced_ratio`，无法预知具体数字，但上述**关系**应当成立。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `EPHandle` 要把 `topk_idx` 克隆一份存起来（`do_handle_copy=True`）？

**答案**：dispatch 之后到 combine 之前，用户可能复用 `topk_idx` 这个张量做别的事（例如反向时被 autograd 改写）。combine 必须用 dispatch 时刻的 `topk_idx` 来反向路由，所以 handle 里存一份克隆，把用户后续修改与 combine 隔离开。

**练习 2**：`psum_num_recv_tokens_per_scaleup_rank[-1]` 与 `num_recv_tokens` 一定是同一个值吗？

**答案**：不一定。`num_recv_tokens` 在无 CPU sync 时是最坏情况上界（`num_max_tokens_per_rank * num_ranks`，见 [csrc/elastic/buffer.hpp:1067](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1067)）；而 `psum_num_recv_tokens_per_scaleup_rank[-1]` 是 GPU 上由 notify warps 写入的真实接收数。只有 `do_cpu_sync=True` 时两者才一致。docstring 在 [deep_ep/buffers/elastic.py:93](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L93) 明确标注了 “May not be accurate without CPU sync”。

---

### 4.4 三种 dispatch 模式：CPU sync / cached handle / 最坏情况分配

#### 4.4.1 概念说明

dispatch 的输出形状依赖运行时数据，但 Python 用户拿到的 `recv_x` 又必须是一个确定形状的张量。DeepEP 用三种“计数获取策略”来化解这个矛盾，分别对应三种使用场景：

| 模式 | 触发条件 | 计数来源 | `recv_x` 行数 | 典型场景 |
| --- | --- | --- | --- | --- |
| **CPU 同步** | `do_cpu_sync=True`（默认，且未传 handle） | host 端轮询 workspace，读到精确计数 | 精确 = `num_recv_tokens` | 训练前向（需要精确形状喂 GEMM） |
| **cached handle** | 传入了 `handle=` | 复用上次的计数与布局，**不做** CPU 同步 | 精确（复用旧值） | 推理解码（路由不变的多步解码） |
| **最坏情况** | `do_cpu_sync=False` 且未传 handle | 按 `num_max_tokens_per_rank * num_ranks` 上界分配 | 上界（含大量 padding） | CUDA graph 兼容（形状必须固定） |

#### 4.4.2 核心流程

C++ 侧用 `cached_mode`（`cached_num_recv_tokens.has_value()`）和 `do_cpu_sync` 两个布尔区分这三个分支：

```text
if cached_mode:                       # 复用旧计数，不 sync
    num_recv_tokens = cached_num_recv_tokens
elif do_cpu_sync:                     # host 轮询读精确计数
    while not all ready:
        读 scaleup rank 计数、读 expert 计数（encode_decode_positive 解码）
        超时则抛 EPException
else:                                 # 无 sync，按最坏情况
    num_recv_tokens = num_max_tokens_per_rank * num_ranks
```

#### 4.4.3 源码精读

**① cached 模式**——直接取缓存值，并强制 `not do_cpu_sync`：

[csrc/elastic/buffer.hpp:1011-1016](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1011-L1016) 这几行说明 cached 模式跳过了所有运行时计数读取。

**② CPU 同步模式**——host 端轮询 + 超时保护：

[csrc/elastic/buffer.hpp:1017-1064](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1017-L1064) 这段循环是 CPU sync 的核心。它反复读 host workspace 里被 GPU notify warps 写入的计数值，用 `math::encode_decode_positive` 解码、`math::is_decoded_positive_ready` 判断是否就绪；超时（`num_cpu_timeout_secs`，默认 300s）则抛 `EPException`。

> 小知识：为什么需要 `encode_decode_positive`？GPU 写计数是异步的，host 可能读到“写了一半”的值。DeepEP 用一种编码让“尚未写完”的状态可被识别（`is_decoded_positive_ready` 返回 false），从而安全轮询。这套编码在 `deep_ep/include/deep_ep/common/math.cuh` 实现，u8-l1 会讲到。

**③ 最坏情况模式**——按上界分配以兼容 CUDA graph：

[csrc/elastic/buffer.hpp:1065-1071](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1065-L1071) 计算 expand 与非 expand 两种上界。CUDA graph 要求每次 capture 的张量形状固定，所以宁可用上界（多余部分作 padding）也不能让形状随数据变。

**④ Python 侧的默认值与 cached 强制**：

[deep_ep/buffers/elastic.py:944](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L944) 在 cached 分支里 `do_cpu_sync = False`；[deep_ep/buffers/elastic.py:961](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L961) 把 `do_cpu_sync` 默认设为 `True`。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：弄清“想拿到精确的每专家 token 数，应该用哪种模式”。

**操作步骤**：

1. 阅读 [csrc/elastic/buffer.hpp:1005-1071](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1005-L1071)，把三个分支的 `num_recv_tokens` 取值抄下来。
2. 对照 README 的推理解码示例 [README.md:280-313](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L280-L313)，看 `cached_handle` 是怎么被传入和回传的。

**需要观察的现象**：`decode_dispatch` 第一次调用时 `cached_handle=None`（走 CPU sync 建立布局），之后把返回的 `handle` 作为 `cached_handle` 传回（走 cached 模式，跳过 CPU sync）。

**预期结果**：你能解释“为什么推理解码连续多步时，只在第一步付 CPU sync 的代价”。

**待本地验证**：可在 `tests/elastic/test_ep.py` 里找到 `cached_dispatch_args`（约 [test_ep.py:163](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L163)）与 `cached_expanded_dispatch_args`（约 [test_ep.py:173](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L173)），它们正是 cached 模式的实测用例。

#### 4.4.5 小练习与答案

**练习 1**：为什么 cached 模式必须 `not do_cpu_sync`？

**答案**：cached 模式复用的是上一次 dispatch 已经算好的精确计数与布局张量，这些值在 host 端已经是已知常量，没必要再去轮询 workspace；更重要的是 cached 模式为了与 CUDA graph 配合，根本不想引入 host-GPU 同步点。代码在 [csrc/elastic/buffer.hpp:1013](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1013) 直接断言 `not do_cpu_sync`。

**练习 2**：在“最坏情况”模式下，`num_recv_tokens` 的值是多少？这种模式牺牲了什么换来了什么？

**答案**：`num_recv_tokens = num_max_tokens_per_rank * num_ranks`（[csrc/elastic/buffer.hpp:1067](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1067)）。牺牲了内存与显存（分配了远超实际接收量的张量，多余部分为 padding），换来了**固定形状**——这是兼容 CUDA graph 捕获的必要条件，同时也省掉了 host-GPU 同步带来的延迟。

---

## 5. 综合实践

把本讲的知识串起来：参照 README 训练示例，写一个**最小的 dispatch → combine 闭环**，并打印每专家接收 token 数。

**实践目标**：用随机的 `topk_idx` 调用 dispatch 得到 `handle`，再用 `handle` 调用 combine，验证两者能成对工作；最后打印 `handle.num_recv_tokens_per_expert_list` 与每专家接收数。

**操作步骤**（需在多 GPU 环境运行；以下为示例代码骨架，基于 README 的训练示例 [README.md:177-252](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L177-L252) 与 `tests/elastic/test_ep.py` 的数据构造方式编写）：

```python
# 示例代码（综合实践骨架，非项目原有文件）
import torch
import torch.distributed as dist
import deep_ep

def run(rank, world_size):
    init_method = ...  # 由 torchrun 注入 MASTER_ADDR/PORT 等，见 u1-l4
    dist.init_process_group('nccl', rank=rank, world_size=world_size)
    torch.cuda.set_device(rank)

    num_experts, num_max_tokens_per_rank, hidden, num_topk = 64, 4096, 7168, 8
    buffer = deep_ep.ElasticBuffer(
        dist.group.WORLD,
        num_max_tokens_per_rank=num_max_tokens_per_rank,
        hidden=hidden, num_topk=num_topk,
        allow_hybrid_mode=True)

    # 1) 构造随机路由（每个 token 选 num_topk 个专家）
    num_tokens = num_max_tokens_per_rank
    scores = torch.randn(num_tokens, num_experts, device='cuda')
    topk_weights, topk_idx = torch.topk(scores, num_topk, dim=-1, sorted=False)
    topk_idx = topk_idx.to(deep_ep.topk_idx_t)

    x = torch.randn(num_tokens, hidden, dtype=torch.bfloat16, device='cuda')

    # 2) dispatch：默认 do_cpu_sync=True，所以会拿到精确计数
    recv_x, recv_topk_idx, recv_topk_weights, handle, event = buffer.dispatch(
        x, topk_idx=topk_idx, topk_weights=topk_weights,
        num_experts=num_experts,
        num_max_tokens_per_rank=num_max_tokens_per_rank,
        async_with_compute_stream=True)
    event.current_stream_wait()   # 等通信完成（u2-l4 详讲）

    # 3) 打印每专家接收 token 数（重点观察对象）
    num_local_experts = num_experts // world_size
    print(f'[rank {rank}] per-expert recv counts =', handle.num_recv_tokens_per_expert_list)
    print(f'[rank {rank}] total recv tokens =',
          handle.psum_num_recv_tokens_per_scaleup_rank[-1].item())

    # 4) 模拟专家计算（保持 BF16、16 字节对齐），再 combine 回原 rank
    local_y = recv_x  # 实际应是专家 FFN 的输出，这里直接复用做演示
    combined_x, _, combine_event = buffer.combine(
        local_y, handle=handle,
        topk_weights=recv_topk_weights,
        async_with_compute_stream=True)
    combine_event.current_stream_wait()
    print(f'[rank {rank}] combined_x shape =', tuple(combined_x.shape))

if __name__ == '__main__':
    # 用 torchrun --nproculocal <N> 起多进程，或用 torch.multiprocessing.spawn
    ...
```

**需要观察的现象**：

1. `handle.num_recv_tokens_per_expert_list` 是一个长度为 `num_local_experts`（=`num_experts // world_size`）的 Python 列表，**总和**应等于 `handle.psum_num_recv_tokens_per_scaleup_rank[-1]`。
2. 因为是随机 gate，每个专家接收数不会完全相等，但所有 rank 的接收数之和应等于 `world_size * num_tokens * num_topk`（在 `topk_idx` 无 -1 的前提下）。
3. `combined_x.shape[0]` 应等于本 rank 原始的 `num_tokens`（combine 把分散到各专家的输出还回原 rank）。

**预期结果**：dispatch 与 combine 成对完成，打印出的计数满足上面的等式关系。

**待本地验证**：实际带宽、具体计数取决于硬件与随机种子；若没有多卡环境，可降级为“源码阅读型实践”——只读 [tests/elastic/test_ep.py:59-231](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L59-L231) 里的 `test_dispatch_combine`，对照其中的断言（如 [test_ep.py:180-184](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L180-L184) 关于 `num_recv_tokens` 与 expand 模式一致的断言）理解行为。

---

## 6. 本讲小结

- `dispatch` 把本 rank 的 token 按 `topk_idx` 发到目标专家所在 rank，输入 `x` 支持 BF16 单张量与 FP8 `(data, scale)` 元组两种形态，返回 `(recv_x, recv_topk_idx, recv_topk_weights, handle, event)`。
- `combine` 是 dispatch 的逆过程，把专家输出推回原 rank 并按 `topk_weights` 加权归约；它**必须**依赖 `handle`，从中读取 `recv_src_metadata`、`psum_num_recv_tokens_per_scaleup_rank`、`topk_idx`、`do_expand` 等十余个字段来驱动反向路由。
- `EPHandle` 是承载路由元数据的“发货单”：关键字段包括 `psum_num_recv_tokens_per_scaleup_rank`（去重前缀和，末元素=总接收数）、`psum_num_recv_tokens_per_expert`（expand 与非 expand 语义不同）、`recv_src_metadata`（`[num_recv_tokens, num_topk+2]`）、`dst_buffer_slot_idx`（cached 复用核心）。
- `recv_x` 的行数运行时才确定，DeepEP 用三种计数策略化解：CPU 同步（精确，训练前向）、cached handle（复用，推理解码）、最坏情况上界（固定形状，兼容 CUDA graph）。
- `num_recv_tokens_per_expert_list` 这个 CPU 侧 Python 列表**只在 CPU sync 或 cached 模式下有值**，无 sync 且非 cached 时为空——这是新手最常踩的坑。
- `topk_weights` 在 combine 中的形状随 `do_expand` 变化：非 expand 是 `[num_tokens, num_topk]` 2D，expand 是 `[num_tokens]` 1D；combine 输入 `x` 强制 BF16 且 16 字节对齐。

## 7. 下一步学习建议

- **下一步必读**：u2-l4（通信-计算重叠与 `EventOverlap`）。本讲里多次出现的 `event.current_stream_wait()` 就是 `EventOverlap` 的接口，搞懂它才能真正用好多流重叠。
- **横向延伸**：u3-l2（缓冲区内存布局）会解释 `recv_src_metadata`、`dst_buffer_slot_idx` 这些张量在底层 buffer 里的物理排布，与本讲的“逻辑字段”形成对照。
- **纵向深入**：若你想知道 `launch_dispatch` / `launch_combine` 这两个 GPU kernel 内部到底怎么写 NVLink/RDMA，请依次读 U5（dispatch 链路）与 U6（combine 链路）；其中 u5-l4 会专门讲 cached handle 在内核层的实现，u5-l3 讲 expand 布局与 copy epilogue。
- **建议阅读的源码**：先把本讲引用的 [deep_ep/buffers/elastic.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py) 的 `EPHandle` 与 `dispatch`/`combine` 通读一遍，再去读 [tests/elastic/test_ep.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py) 的 `test_dispatch_combine`，对照其中的断言验证你对字段语义的理解。
