# 确定性路由：EPHandle.deterministic_sort

## 1. 本讲目标

学完本讲后，读者应该能够：

- 说清 DeepEP dispatch 输出**非确定性**的物理来源（多 warp / 多 channel / 多 rank 抢占 `atomicAdd` 槽位的竞态），以及为何这会破坏 CUDA graph 复用与逐位可复现实验。
- 解释 `deterministic_sort` 的总体策略：在 copy epilogue 写完 `recv_x` 之后，**按源 token 全局索引**对收到的 token 重排，得到一个与到达顺序无关的规范位序。
- 掌握 `register_hook_after_wait` 这个 hook 机制如何把「排序」精确地挂到 `current_stream_wait()` 之后，既保证数据就绪又不破坏通信-计算重叠。
- 区分**非 expand**（单键全排序、`recv_src_metadata` 行也被排列）与 **expand**（专家内双键排序、只改 `recv_src_metadata[:, 2:]` 的 slot 指针而不动行序）两种模式的排序键构造，以及 **cached dispatch** 下跳过排序的原因。

## 2. 前置知识

本讲建立在以下已学概念之上（若不熟悉建议先回顾对应讲义）：

- **EPHandle 与 dispatch/combine 工作流**（u2-l3）：`recv_x` 的行数 `num_recv_tokens` 运行时才确定，`EPHandle` 承载路由元数据。
- **copy epilogue 与 expand 布局**（u5-l3）：dispatch 拆成「省 SM 的主 kernel + 满 SM 的 copy epilogue」两个内核，后者把 buffer 里的 token 拆包写入 `recv_x`；expand 模式按专家分组连续排列、非 expand 按到达顺序排列。
- **CPU 同步与 cached handle**（u5-l4）：三种 host 模式——`do_cpu_sync`（精确计数）、cached handle（复用旧布局）、无同步（最坏上界分配）。
- **combine 主流程**（u6-l1）：combine 是 dispatch 的逆过程，依赖 `recv_src_metadata` 重放路由。
- **EventOverlap 与双流控制**（u2-l4）：comm stream 与 compute stream 靠 CUDA event 同步，`current_stream_wait()` 是让计算流等待通信完成的入口。

一个关键事实（来自 u6-l1 与 C++ 源码）：`recv_src_metadata` 形状为 `[num_recv_tokens, num_topk + 2]`，其中**第 0 列是源 token 全局索引**（唯一标识 token 来自哪个 rank 的哪个位置），**第 1 列是另一项源信息**，**后 `num_topk` 列（即 `[:, 2:]`）是 expand 模式下每个 top-k 槽的 slot 指针**。本讲的排序键与 slot 指针改写完全围绕这两段展开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deep_ep/buffers/elastic.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py) | `EPHandle.deterministic_sort`（排序本体）与 `ElasticBuffer.dispatch`（把排序注册成 hook 的入口） |
| [deep_ep/utils/event.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/event.py) | `EventOverlap` 的 `current_stream_wait` / `register_hook_after_wait`（hook 触发时机） |
| [deep_ep/utils/envs.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py) | `check_torch_deterministic`（确定性模式的前置校验） |
| [csrc/elastic/buffer.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) | `recv_src_metadata` 的分配与其 `[num_topk + 2]` 列结构 |
| [tests/elastic/test_ep.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py) | 确定性模式的正确性断言（连续两次 dispatch 比对） |

## 4. 核心概念与源码讲解

### 4.1 非确定性的来源与确定性排序的总体策略

#### 4.1.1 概念说明

dispatch 的目标是把本 rank 的 token 按各自的 top-k 专家发到目标 rank。问题在于：**接收端把 token 落进 buffer 的顺序是不确定的**。

回顾 u5-l1 / u5-l2：接收 warp 用 `atomicAdd` 在对端 buffer 上「抢槽位」，把得到的 `dst_buffer_slot_idx` 作为落地位置。哪个 warp 先执行 `atomicAdd`、哪个 channel 的 RDMA 包先到达、哪个 rank 的数据先到，都取决于当时 GPU 的调度与网络抖动，**每次运行的抢占顺序都不同**。随后 copy epilogue 按 buffer 里的槽位顺序把 token 写进 `recv_x`，于是 `recv_x[i]` 到底对应哪个源 token，会随运行而变。

这种非确定性带来两个实际问题：

1. **逐位复现性**：同样的输入，两次 dispatch 的 `recv_x` 行序不同，让实验难以复现、让数值调试变得痛苦。
2. **CUDA graph 兼容**：虽然无 CPU sync 模式下张量形状固定，但行序抖动仍可能影响下游对顺序敏感的算子。

`deterministic_sort` 的思路非常直接：**既然到达顺序不可控，那就在数据全部写完之后，按一个与到达顺序无关的规范键（源 token 全局索引）把 `recv_x` 重排一遍**。源 token 全局索引由 `recv_src_metadata[:, 0]` 提供，它编码了「这个 token 来自哪个 rank 的哪个位置」，对同一份路由输入是固定且唯一的。排序后，`recv_x` 的行序只取决于「源在哪」，不再取决于「谁先到」。

#### 4.1.2 核心流程

确定性排序在 dispatch 链路中的位置：

```text
主 dispatch kernel（跨 rank 通信，顺序不确定）
        │
        ▼
dispatch_copy_epilogue（把 buffer 写进 recv_x，行序 = 抢槽顺序）
        │  ← recv_src_metadata[:, 0] 记录了每行的源全局索引
        ▼
【current_stream_wait：计算流等通信流写完 recv_x】  ← 排序必须在这之后
        │
        ▼
EPHandle.deterministic_sort（按源全局索引重排 recv_x 等）  ← 本讲核心
        │
        ▼
返回给用户的 recv_x（行序确定）
```

排序本体分两条支路，由 `do_expand` 决定：

- **非 expand**：每行 = 一个 token，用单键 `recv_src_metadata[:, 0]` 做一次全排序，所有「依赖到达顺序」的张量套用同一排列。
- **expand**：每行 = 一个「专家-槽」位（按专家分组、带对齐 padding），必须保持专家分组不变，故构造「专家优先、专家内按源索引」的**双键**，只在专家内重排，并改写 slot 指针。

#### 4.1.3 源码精读

确定性能力的总开关在 `ElasticBuffer` 构造参数里：

[deep_ep/buffers/elastic.py:239](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L239) 处的 `deterministic: bool = False` 构造参数，在 [deep_ep/buffers/elastic.py:277](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L277) 存为 `self.deterministic`。

dispatch 入口先做一道前置校验：

[deep_ep/buffers/elastic.py:924](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L924) 处的 `check_torch_deterministic()`。它的实现见 [deep_ep/utils/envs.py:183-189](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py#L183-L189)：断言 PyTorch 的 `use_deterministic_algorithms` 与 `fill_uninitialized_memory` 不能同时开启——因为后者会让 `torch.empty()` 触发一个初始化内核，可能和通信流重叠导致错误。换言之，DeepEP 自己保证确定性，不需要依赖 PyTorch 这两个开关。

`deterministic_sort` 的方法签名与设计意图注释（[deep_ep/buffers/elastic.py:100-116](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L100-L116)）非常清楚地说明了 expand / 非 expand / cached 三种情况分别排什么：

```python
def deterministic_sort(self,
                       do_cpu_sync, is_cached_dispatch,
                       recv_x, recv_sf, recv_topk_idx, recv_topk_weights,
                       channel_linked_list):
    # 非扩展：对所有依赖到达顺序的张量排序，含 recv_src_metadata（仅非 cached）
    # 扩展：只对扩展数组 recv_x/recv_sf/recv_topk_weights 排序，
    #       并更新 recv_src_metadata[:, 2:] 的 slot 指针，但不重排 recv_src_metadata 本身
```

`EPHandle` 还在 [deep_ep/buffers/elastic.py:97-98](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L97-L98) 预留了 `cached_recv_src_metadata_before_sort = None` 字段——它缓存「排序前的 `recv_src_metadata`」，是后续所有排序键的基准（见 4.3 / 4.4）。

#### 4.1.4 代码实践

**实践目标**：在不跑 GPU 的情况下，先从源码层面确认「到达顺序非确定」这一论断。

**操作步骤**：

1. 打开 u5-l1 讲义引用的 [csrc/kernels/elastic/dispatch.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp)，在 dispatch warps 段落搜索 `atomicAdd`，找到「领对端槽位」的那条语句。
2. 思考：多个 SM 上的 warp 同时对同一个对端 rank 的计数器 `atomicAdd`，返回值的先后顺序是否可预测？

**需要观察的现象 / 预期结果**：你会看到槽位索引是 `atomicAdd` 的返回值，而 `atomicAdd` 的执行顺序由 GPU 运行时调度决定，无软件保证。因此 `recv_x` 行序非确定。本实践为源码阅读型，结论「待本地验证」仅指真实行序抖动需在多卡上实测观察。

#### 4.1.5 小练习与答案

**练习 1**：既然行序不确定，为什么 DeepEP 不直接在 GPU 内核里按源索引写入，而要事后排序？

**参考答案**：内核里按源索引写入需要先知道「本 rank 总共收到多少 token、来自哪些源」，这本身要等所有 warp 归约完计数才能确定；而内核的首要目标是省 SM、把数据尽快搬过 NVLink/RDMA。事后再用一个轻量的纯本地排序（只动 SM 不动网络）来规范顺序，是通信效率与确定性之间的合理折中。

**练习 2**：`check_torch_deterministic` 为何禁止 `fill_uninitialized_memory` 与确定性算法同时开启？

**参考答案**：`fill_uninitialized_memory` 会让每个 `torch.empty()` 额外触发一个初始化内核，该内核在默认流上执行，可能与 comm stream 上的通信内核重叠/乱序，从而破坏 DeepEP 自行管理的流同步，导致错误。

---

### 4.2 hook 机制：把排序挂到 current_stream_wait 之后

#### 4.2.1 概念说明

排序必须发生在 copy epilogue **写完 `recv_x` 之后**——否则排序读到的是半成品。但 dispatch 默认走异步重叠路径（`async_with_compute_stream=True`）：通信在 comm stream 上跑，控制权立刻还给用户，用户拿到一个 `EventOverlap`，等自己觉得合适了再 `current_stream_wait()`。

于是排序面临一个时机问题：**它该在什么时候、由谁触发？** 如果让用户手动调用，既容易忘，又会和「重叠」的抽象冲突。DeepEP 的解法是给 `EventOverlap` 装一个 **hook**：dispatch 把排序函数注册成「等待完成后的回调」，用户照常 `current_stream_wait()`，wait 一返回，hook 自动在**当前流（compute stream）**上执行排序。这样：

- 排序一定在数据就绪后（因为它挂在 wait 之后）。
- 排序一定在 compute stream 上（因为它在 `current_stream_wait()` 内部触发，而该函数由 compute stream 上下文调用），与用户的后续算子自然串行。
- 用户无需感知排序的存在，重叠用法不变。

#### 4.2.2 核心流程

```text
dispatch(async_with_compute_stream=True)
        │
        ├─ 注册 hook：event_overlap.register_hook_after_wait(deterministic_sort)
        │
        └─ 返回 event_overlap 给用户
                │
用户调用 event_overlap.current_stream_wait()
        │
        ├─ (1) self.event.current_stream_wait()   ← 计算流真的开始等通信完成
        │
        ├─ (2) if hook_after_wait: hook_after_wait()  ← 触发 deterministic_sort
        │            self.hook_after_wait = None      ← 一次性，触发后清空
        │
        └─ 返回，recv_x 已是规范序
```

若 `async_with_compute_stream=False`（同步路径），则不存在「等待」环节，dispatch 直接当场调用排序函数。

#### 4.2.3 源码精读

`EventOverlap` 在 [deep_ep/utils/event.py:37](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/event.py#L37) 定义了 hook 槽位，其上方注释（[deep_ep/utils/event.py:35-36](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/event.py#L35-L36)）直接点明它的用途就是「确定性 dispatch 需要在 `current_stream_wait()` 之后排序」。

触发逻辑在 [deep_ep/utils/event.py:39-54](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/event.py#L39-L54)：

```python
def current_stream_wait(self, release_handle=False):
    self.event.current_stream_wait()          # (1) 真正的流等待
    if self.hook_after_wait is not None:      # (2) 触发一次性 hook
        self.hook_after_wait()
        self.hook_after_wait = None
    if release_handle:
        self.event = None
```

注册接口 [deep_ep/utils/event.py:56-61](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/event.py#L56-L61) 用断言保证「一个 event 最多挂一个 hook」，避免重复排序。

dispatch 侧的注册代码（[deep_ep/buffers/elastic.py:1019-1027](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L1019-L1027)）用 `functools.partial` 把 `deterministic_sort` 的除 `self` 外参数全部柯里化，得到一个无参回调，再根据是否异步二选一：

```python
if self.deterministic:
    epilogue = functools.partial(
        handle.deterministic_sort,
        do_cpu_sync, is_cached_dispatch,
        recv_x, recv_sf, recv_topk_idx, recv_topk_weights, channel_linked_list)
    event_overlap.register_hook_after_wait(epilogue) if async_with_compute_stream else epilogue()
```

注意这里捕获的 `recv_x` 等张量是 dispatch 刚分配、即将返回给用户的同一个对象，`deterministic_sort` 内部用 `tensor.copy_()` 原地改写它们（见 4.3），所以用户最终拿到的就是排序后的张量，无需额外传递。

#### 4.2.4 代码实践

**实践目标**：验证 hook 是「一次性」的，且只在 `current_stream_wait()` 内触发。

**操作步骤**（源码阅读型，可在单卡用 mock 验证逻辑）：

1. 构造一个 `EventOverlap`，`register_hook_after_wait(lambda: print("hook fired"))`。
2. 调用 `current_stream_wait()` 两次（需先有一个真实 event，或用一个已 record 的 event）。

**需要观察的现象 / 预期结果**：第一次 wait 打印 `hook fired`，第二次不再打印（因为 [deep_ep/utils/event.py:48](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/event.py#L48) 在触发后把 `self.hook_after_wait` 置 `None`）。这保证同一个 dispatch 的排序不会被重复执行。完整多卡行为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么排序要放在 `current_stream_wait()` **之后**而不是之前？

**参考答案**：`recv_x` 由 comm stream 上的 copy epilogue 写入。排序在 compute stream 上做 `recv_x[orig_indices]` 的读取，若在 wait 之前执行，读取可能先于 epilogue 写入，读到未初始化数据。wait 之后两流已同步，读取安全。

**练习 2**：如果用户开了 `async_with_compute_stream=True` 却**从不**调用 `current_stream_wait()`，会发生什么？

**参考答案**：排序永远不会触发（hook 挂在 wait 内），`recv_x` 保持非确定行序；更严重的是计算流与通信流未建立依赖，直接使用 `recv_x` 属于数据竞争。这违反了 u2-l4 强调的「dispatch(async=True) → wait → 才用 recv_x」标准用法。

---

### 4.3 非 expand 模式：以源 token 全局索引为单键排序

#### 4.3.1 概念说明

非 expand（compact）模式下，`recv_x` 的每一行就是一个收到的 token，行序即 token 到达序。要让它确定，只需**对所有行按源全局索引升序排一次**。由于源全局索引（`recv_src_metadata[:, 0]`）对同一份路由是唯一且固定的，排序结果唯一。

但有一类张量「依赖到达顺序」：除了 `recv_x`，还有 FP8 的 scaling factor `recv_sf`、`recv_topk_weights`、`recv_topk_idx`，以及 `recv_src_metadata` 本身（combine 要用它反向路由，行序必须和 `recv_x` 一致）。它们都要套用**同一个排列**。

还有两处细节：

- **越界行（oob）**：无 CPU sync 时 `recv_x` 按最坏上界分配，只有前 `num_recv_tokens` 行有效，尾部的越界行 `recv_src_metadata[:, 0]` 可能是脏值。排序前必须把它们的键设为 `int 最大值`，让它们沉到末尾，不干扰有效行的相对顺序（用户之后会切片 `[:num_recv_tokens]`）。
- **cached 模式**：cached dispatch 不重新生成 `recv_src_metadata`（直接复用 handle 里的），所以**不能**再去排列 `recv_src_metadata` 的行——但它仍然要排列 `recv_x` 等（因为到达序仍抖动）。排序键用的是「首次非 cached dispatch 时缓存的」那份 `recv_src_metadata`。

#### 4.3.2 核心流程

```text
1. 缓存基准：若非 cached，cached_recv_src_metadata_before_sort = recv_src_metadata.clone()
2. sort_keys = cached_recv_src_metadata_before_sort[:, 0]          # 源全局索引
3. 算 num_recv_tokens（CPU sync 用行数，否则用 psum 末位元素）
4. 若无 CPU sync：把 oob 行的 sort_keys 置为 int_max
5. orig_indices = torch.sort(sort_keys).indices                    # 目标排列
6. 对 recv_x / recv_sf / recv_topk_weights / recv_topk_idx 各自 copy_(t[orig_indices])
7. 若非 cached：对 recv_src_metadata 也 copy_(...[orig_indices])
8. 若非 cached 且有 channel_linked_list：用反向排列重映射链表节点
```

第 8 步的 channel_linked_list（hybrid 模式下 combine 反向枚举 token 用的链表，见 u5-l2）的节点值是 token 在 buffer 里的旧位置，行序变了之后必须把这些指针翻译成新位置，否则 combine 会指错。

#### 4.3.3 源码精读

基准缓存与键提取（[deep_ep/buffers/elastic.py:122-125](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L122-L125)）：

```python
if not is_cached_dispatch:
    self.cached_recv_src_metadata_before_sort = self.recv_src_metadata.clone()
assert self.cached_recv_src_metadata_before_sort is not None
sort_keys = self.cached_recv_src_metadata_before_sort[:, 0]
```

注意 `.clone()` 很关键：后续若排列了 `self.recv_src_metadata`，没有克隆的话键也会跟着变，排序就错了。

越界处理与排序（[deep_ep/buffers/elastic.py:128-133](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L128-L133)）：

```python
num_recv_tokens = self.psum_num_recv_tokens_per_scaleup_rank[-1] if not do_cpu_sync else self.recv_src_metadata.shape[0]
if not do_cpu_sync:
    oob_tokens_mask = torch.arange(...) >= num_recv_tokens
    sort_keys = sort_keys.clone()
    sort_keys[oob_tokens_mask] = torch.iinfo(sort_keys.dtype).max
orig_indices = torch.sort(sort_keys).indices
```

两个工具函数（[deep_ep/buffers/elastic.py:135-144](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L135-L144)）：`get_reverse_permutation` 由「正向排列」反推出「旧位置 → 新位置」的反向表（用于改写指针）；`permute` 用 `tensor.copy_(tensor[orig_indices])` **原地**重排，保证返回给用户的张量对象不变。

非 expand 主干（[deep_ep/buffers/elastic.py:146-159](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L146-L159)）：

```python
permute(recv_x, orig_indices)
permute(recv_sf, orig_indices)
permute(recv_topk_weights, orig_indices)
permute(recv_topk_idx, orig_indices)
if not is_cached_dispatch:
    permute(self.recv_src_metadata, orig_indices)        # cached 模式跳过
if not is_cached_dispatch and channel_linked_list is not None:
    valid_mask = (channel_linked_list >= 0) & (channel_linked_list < num_recv_tokens)
    to_indices = get_reverse_permutation(orig_indices)
    channel_linked_list[valid_mask] = to_indices[channel_linked_list[valid_mask]].to(...)
```

可以看到 `recv_x` 等四项**总是**被排列（即使 cached），而 `recv_src_metadata` 与 `channel_linked_list` 的改写**只在非 cached** 时进行——这是 cached 与非 cached 在非 expand 下的唯一差异。

#### 4.3.4 代码实践

**实践目标**：用纯 PyTorch 复现「单键排序 + 原地 copy_」的最小逻辑，确认 `copy_(t[indices])` 等价于「按索引重排」。

**操作步骤**（可在单卡 CPU/GPU 跑）：

```python
import torch
# 模拟 4 行 token，源全局索引顺序为 [3,1,0,2]（到达序混乱）
recv_x = torch.arange(4*2).float().reshape(4,2)          # [[0,1],[2,3],[4,5],[6,7]]
sort_keys = torch.tensor([3,1,0,2])
orig = torch.sort(sort_keys).indices                      # tensor([2,1,3,0])
recv_x.copy_(recv_x[orig])                                # 原地重排
print(recv_x)                                             # 行序按源索引 0,1,2,3 排好
```

**需要观察的现象 / 预期结果**：重排后第 0 行是原来的第 2 行、第 1 行是原来的第 1 行……行序按源索引升序固定。这正是 `deterministic_sort` 在非 expand 下对 `recv_x` 做的事。注意 `copy_(t[indices])` 必须用 `t[indices]`（产生新张量）再写回，**不能**写成 `t = t[indices]`——后者会换对象，丢失「原地改写返回张量」的语义。

#### 4.3.5 小练习与答案

**练习 1**：为何 [deep_ep/buffers/elastic.py:123](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L123) 用 `.clone()` 缓存，而 [deep_ep/buffers/elastic.py:131](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L131) 又 `sort_keys = sort_keys.clone()`？

**参考答案**：前者是为了冻结「排序前的 `recv_src_metadata`」作基准，避免后面 `permute(self.recv_src_metadata, ...)` 改坏基准（键取自它的第 0 列）；后者是因为接下来要把 oob 行的键改成 `int_max`，不克隆就会污染基准张量的第 0 列。两次克隆保护的是同一个数据的不同用途。

**练习 2**：cached 模式下为何不排列 `recv_src_metadata`，却仍排列 `recv_x`？

**参考答案**：cached dispatch 复用 handle 里已有的 `recv_src_metadata`，它记录的是首次 dispatch 的源信息，行序已与 combine 约定好，不能动；但本次 dispatch 的 token 到达序仍可能抖动，所以 `recv_x` 等数据张量仍需按缓存的源索引键重排。排序键用的正是 `cached_recv_src_metadata_before_sort`（首次缓存的那份）。

---

### 4.4 expand 模式：双键排序与 recv_src_metadata 的 slot 指针改写

#### 4.4.1 概念说明

expand 模式下 `recv_x` 不是「一行一 token」，而是「按专家分组连续排列、每组带 `expert_alignment` 对齐 padding」（见 u5-l3）。这意味着：

- **不能**像非 expand 那样用一个全局源索引排序——那会破坏专家分组，让 per-expert GEMM 切片失效。
- 必须保持「专家 e 的所有行仍连续、padding 仍在组末」的前提下，**在每个专家组内**按源全局索引排。
- 而且 expand 模式下 `recv_x` 的「行」是 expand 后的槽位，与 `recv_src_metadata` 的「行」（compact token，每行一个 token）维度不同。`recv_src_metadata[:, 2:]` 存的正是「这个 token 的第 k 个 top-k 选择，落在 expand buffer 的哪个槽」。

所以 expand 排序的目标是：**重排 expand 数组 `recv_x/recv_sf/recv_topk_weights`，使每个专家组内按源索引有序；同时更新 `recv_src_metadata[:, 2:]` 的 slot 指针，让它们指向重排后的新位置**。注意——这正是本讲实践任务的核心——`recv_src_metadata` **本身的行序不动**：它的行与 compact token 一一对应，combine 依赖这套行序做反向路由；expand 排序只移动 expand 数组，并把指针翻译过去。

构造「专家优先、组内按源索引」的双键，靠的是一个数学编码技巧（见下）。此外，expand 排序**只在非 cached 时执行**：cached 模式下 copy epilogue 直接按 `recv_src_metadata[:, 2:]` 的缓存槽位放置 token，布局天然确定，无需排序。

#### 4.4.2 核心流程

双键编码（把两维序关系压进单个 int64，从而一次 `torch.sort` 完成）：

设 \( B = 10^{10} \)（代码里的 `src_token_global_index_max_x2`）。对 expand buffer 的第 \( r \) 行：

\[ \text{key}(r) = \text{expert\_idx}(r) \cdot B \;+\; \begin{cases} -\tfrac{B}{2} + \text{src\_global\_idx}(\text{owner}(r)), & r \text{ 是有效 token 槽} \\ 0, & r \text{ 是 padding 槽} \end{cases} \]

其中 \(\text{expert\_idx}(r)\) 由「每个专家的起始偏移」`bucketize` 得到，\(\text{owner}(r)\) 是占据该槽的源 token。

于是对同一个专家 \( e \)：

- 有效槽的键落在 \( eB - \tfrac{B}{2} + [0, \text{全局索引上限}) \) 区间；
- padding 槽的键恰为 \( eB \)。

升序排序后：专家 \( e \) 的所有行仍连续（因为键以 \( eB \) 为主导）；组内有效槽在前、padding 在后；有效槽之间按源全局索引升序。约束 \( \text{全局索引上限} < \tfrac{B}{2} \) 保证有效槽键严格小于 padding 键。完整流程：

```text
1. 算每个专家组的起始：expert_token_idx_start = psum - num_unaligned
2. bucketize 得到每行的 expert_idx
3. sort_keys = expert_idx * B   （padding 槽的最终键就是它）
4. slots = cached_recv_src_metadata_before_sort[:, 2:]   # [num_recv_tokens, topk]
5. 对每个有效 (token, topk)：scatter_add_ 进 slot 处 += (-B/2 + src_global_idx[token])
6. orig_indices = torch.sort(sort_keys, stable=True).indices
7. permute(recv_x / recv_sf / recv_topk_weights, orig_indices)
8. 反向表 to_indices = get_reverse_permutation(orig_indices)
9. recv_src_metadata[:, 2:][valid] = to_indices[ recv_src_metadata[:, 2:][valid] ]   # 指针翻译
```

第 5 步用 `scatter_add_` 是因为 expand 的「目标槽」存在 `slots` 张量里（散列的、每 token 每个 topk 一个），要把「该槽的键增量」累加到 `sort_keys` 的对应位置。每个有效槽恰好被一个 (token, topk) 占据，故累加一次。

#### 4.4.3 源码精读

expand 分支由 `elif not is_cached_dispatch` 把守（[deep_ep/buffers/elastic.py:161](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L161)），注释（[deep_ep/buffers/elastic.py:162-170](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L162-L170)）完整说明了双键编码。

确定每行所属专家（[deep_ep/buffers/elastic.py:171-177](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L171-L177)）：

```python
src_token_global_index_max_x2 = 10000000000    # 1e10
tensor_dim0_after_expand = recv_x.shape[0]
expert_token_idx_start = self.psum_num_recv_tokens_per_expert - self.num_unaligned_recv_tokens_per_expert
token_idx2expert_idx = torch.bucketize(torch.arange(tensor_dim0_after_expand, device='cuda'),
                                       expert_token_idx_start[1:], right=True)
sort_keys_for_expanded_tensors = token_idx2expert_idx * src_token_global_index_max_x2
```

这里 `expert_token_idx_start` 是「每个专家在 expand buffer 中的真实起始行」（用 u5-l3 讲过的 `psum - unaligned` 技巧恢复，因为 expand 下 `psum` 语义是「对齐累计 + 当前 unaligned」）。`bucketize` 把行号映射回专家 id。

构造有效槽键（[deep_ep/buffers/elastic.py:179-184](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L179-L184)）：

```python
slots = self.cached_recv_src_metadata_before_sort[:, 2:]            # [num_recv_tokens, topk]
src_global_idx = self.cached_recv_src_metadata_before_sort[:, 0]
valid_mask = slots >= 0
if not do_cpu_sync:
    valid_mask[oob_tokens_mask] = False
sort_keys_for_expanded_tensors.scatter_add_(
    0, slots[valid_mask],
    -src_token_global_index_max_x2//2 + src_global_idx.unsqueeze(1).expand_as(slots)[valid_mask].to(torch.int64))
```

`slots >= 0` 过滤掉无效 top-k 槽（padding 槽在 `recv_src_metadata` 里用负值表示，见 u5-l3）；无 CPU sync 时再排除 oob token。`scatter_add_` 把每个有效槽的键增量 `(-B/2 + src_global_idx)` 累加到该槽所在行。

排序、原地重排、指针翻译（[deep_ep/buffers/elastic.py:186-192](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L186-L192)）：

```python
orig_indices_for_expanded_tensors = torch.sort(sort_keys_for_expanded_tensors, stable=True).indices.to(torch.int32)
permute(recv_x, orig_indices_for_expanded_tensors)
permute(recv_sf, orig_indices_for_expanded_tensors)
permute(recv_topk_weights, orig_indices_for_expanded_tensors)

to_indices_for_expanded_tensors = get_reverse_permutation(orig_indices_for_expanded_tensors)
self.recv_src_metadata[:, 2:][valid_mask] = to_indices_for_expanded_tensors[self.recv_src_metadata[:, 2:][valid_mask]]
```

最后一行是「指针翻译」：`recv_src_metadata[:, 2:]` 里存的是旧槽号，重排后旧槽号 `s` 的新位置是 `to_indices[s]`，于是用反向表把每个指针改写为新槽号。注意这里用的是 `self.recv_src_metadata`（当前、可能已被前面逻辑引用），而键用的是 `cached_recv_src_metadata_before_sort`（冻结基准）——基准与可变分离。**整个过程没有任何对 `self.recv_src_metadata` 行序的 `permute`**，仅改写其 `[:, 2:]` 列内的指针值。

#### 4.4.4 代码实践

**实践目标**：回答本讲核心问题——expand 模式下为何只排 `recv_x/sf/weights`，而不动 `recv_src_metadata` 的行序？

**操作步骤**（源码阅读型）：

1. 重读 [deep_ep/buffers/elastic.py:186-192](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L186-L192)，确认：`recv_x/recv_sf/recv_topk_weights` 被排列；`recv_src_metadata` 只有 `[:, 2:]` 被指针翻译，**没有** `permute(self.recv_src_metadata, ...)`。
2. 对照 u6-l1：combine 用 `recv_src_metadata` 的行（compact token 序）驱动反向路由。

**需要观察的现象 / 预期结果 / 解释**：expand 模式下 `recv_src_metadata` 的**行**对应 compact token（一行一 token），combine 按这套行序逐 token 反向路由；而 `recv_x` 是 expand 后的「专家分组」布局，两者维度和语义都不同。若把 `recv_src_metadata` 也按 expand 的双键排列，会破坏它「一 token 一行」的约定，combine 就指错路。正确做法是：只把 expand 数组排成专家内规范序，再通过指针翻译（最后一行）让 `recv_src_metadata[:, 2:]` 跟随到新位置——既让 `recv_x` 确定，又保住 combine 的路由表。

#### 4.4.5 小练习与答案

**练习 1**：双键编码里，为什么有效槽的增量用 `-B/2 + src_global_idx`，而不是 `+ src_global_idx`？

**参考答案**：为了让有效槽的键**严格小于** padding 槽的键。padding 槽的键是 `expert_idx * B`；若有效槽只加正的 `src_global_idx`，其键 `expert_idx*B + src_global_idx` 会**大于** padding 键，排序后 padding 反而排到有效槽前面。改成 `-B/2 + src_global_idx` 后，有效槽键为 `expert_idx*B - B/2 + src_global_idx`，只要 `src_global_idx < B/2`（全局索引远小于 \(5\times10^9\)），有效槽键就落在 `(expert_idx*B - B/2, expert_idx*B)` 区间，严格小于 padding 键，从而「有效在前、padding 在后」。

**练习 2**：expand 排序为什么用 `stable=True`，而非 expand 没用？

**参考答案**：expand 下 padding 槽的键相同（都是 `expert_idx*B`），`stable=True` 保证这些并列的 padding 行保持原相对顺序，避免不同后端实现的不稳定 tie-break 带来新的非确定性。非 expand 下键是唯一的源全局索引（每个 token 一个），不存在并列，故无需 stable。

---

## 5. 综合实践

**任务**：在单机 8 卡上，用 `deterministic=True` 跑通确定性 dispatch，并验证「连续两次 dispatch 的 `recv_x` 行序完全一致」。

**操作步骤**：

1. 直接使用项目自带的测试入口（它已内置断言）：

   ```bash
   torchrun --nproc_per_node=8 tests/elastic/test_ep.py \
       --deterministic --skip-perf-test
   ```

2. 跟踪断言逻辑：打开 [tests/elastic/test_ep.py:387-397](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L387-L397)。它在 `args.deterministic` 为真时，用**相同的 `dispatch_args`** 再 launch 一次 dispatch，然后逐项断言：

   ```python
   assert torch.equal(recv_x_bf16, recv_x_twice_bf16)
   assert torch.equal(recv_topk_idx, recv_topk_idx_twice[:num_recv_tokens])
   assert torch.equal(recv_topk_weights, recv_topk_weights_twice[:num_recv_tokens])
   assert torch.equal(handle.recv_src_metadata[:, :1], handle_twice.recv_src_metadata[:num_recv_tokens, :1])
   ```

   即两次 dispatch 的 `recv_x`、`recv_topk_idx`、`recv_topk_weights`、以及 `recv_src_metadata` 的第 0 列（源全局索引）逐位相等。

3. **再关掉确定性**跑一次（去掉 `--deterministic`），用一个小脚本在两次 dispatch 后比较 `recv_x`——预期会发现行序不同（这是非确定性的直观证据）。

**需要观察的现象 / 预期结果**：

- 开 `--deterministic`：所有断言通过，两次 `recv_x` 完全相等。
- 关 `--deterministic`：两次 `recv_x` 行序可能不同（内容集合相同，顺序不同）。

**解释要点**（把本讲知识串起来）：

- 断言之所以成立，是因为 `deterministic_sort` 用 `cached_recv_src_metadata_before_sort[:, 0]`（源全局索引）做排序键，相同路由输入 → 相同键 → 相同 `orig_indices` → 相同行序。
- 断言只比对 `recv_src_metadata[:, :1]`（第 0 列）：因为非 expand 下整个 `recv_src_metadata` 都被同序排列（第 0 列自然升序），而 expand 下第 0 列本就不动（行序不变），两种模式下第 0 列都是「升序的源全局索引」，可作为统一校验锚点。
- expand 模式的对应断言在 [tests/elastic/test_ep.py:399-404](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L399-L404)，只比对 `valid_expanded_indices` 处的 `recv_x`（跳过 padding），呼应 4.4 讲的「只对有效槽排序、padding 不初始化」。

若手头没有 8 卡环境，本实践的断言结果标注「待本地验证」，但第 2、3 步的源码阅读与逻辑推导可独立完成。

## 6. 本讲小结

- dispatch 输出的非确定性来自接收端 `atomicAdd` 抢槽与多 channel / 多 rank 的到达顺序竞态，每次运行 `recv_x` 行序都可能不同。
- `deterministic_sort` 的核心策略：在 copy epilogue 写完之后，**按源 token 全局索引**（`recv_src_metadata[:, 0]`）重排 `recv_x`，得到与到达顺序无关的规范位序。
- 时机由 `EventOverlap` 的 hook 机制保证：dispatch 用 `functools.partial` 把排序柯里化成无参回调，`register_hook_after_wait` 挂到 event 上；用户 `current_stream_wait()` 一返回，hook 在计算流上一次性触发排序。
- 非 expand 模式用单键全排序，并对所有依赖顺序的张量（含 `recv_src_metadata` 行、`channel_linked_list` 指针）套用同一排列；cached 模式下跳过对 `recv_src_metadata` 的改动。
- expand 模式用「专家优先、组内按源索引」的双键编码（`expert_idx*B + (-B/2 + src_global_idx)`），只重排 expand 数组并改写 `recv_src_metadata[:, 2:]` 的 slot 指针，**不**动其行序，以保住 combine 的 compact-token 路由表；cached 模式下 expand 排序整体跳过。
- 无 CPU sync 时通过把越界行的键置为 `int_max` 让其沉末，保证有效行的相对顺序不被脏值干扰。

## 7. 下一步学习建议

- **回到 combine**：本讲多次提到「combine 依赖 `recv_src_metadata` 行序」，建议结合 u6-l1 与 u6-l2（reduce epilogue）验证：确定性 dispatch 产出的 handle，其 combine 结果是否也因此更稳定。
- **CUDA graph 联动**：u5-l4 讲过无 CPU sync + 最坏上界分配是为 CUDA graph 兼容；可思考确定性排序（行序固定）与 CUDA graph（地址/形状固定）如何叠加得到完全可复现的 EP 前向。
- **性能代价**：确定性排序是一次纯本地的满-SM 排序内核，会引入额外延迟。可阅读 [deep_ep/utils/testing.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/testing.py) 的 `bench_kineto`，实测开/关 `--deterministic` 时 dispatch 的端到端延迟差，量化这笔代价。
- **下一讲**：进入 U7 实验性特性（barrier / Engram / PP / AGRS），这些特性同样依赖 `EventOverlap` 的等待语义与对称内存可见性，本讲的 hook 时机理解将直接复用。
