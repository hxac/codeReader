# FlashInfer 后端实现

## 1. 本讲目标

在上一讲（u7-l1）里，我们只看了注意力后端的「接口契约」：`BaseAttnBackend` 规定了 `prepare_metadata` / `forward` / 三个 capture 方法，`HybridBackend` 按阶段分发。但接口背后的真正计算——一个 batch 的 query 到底如何去读取池子里成千上万个 token 的 K/V——被一个黑盒 `wrapper.run(...)` 藏了起来。

本讲就打开这个黑盒。读完本讲，你应当能够：

1. 说清楚 FlashInfer 所需的 **CSR（压缩稀疏行）格式**：`indptr`（即 `cu_seqlens`）与 `indices` 是怎么把「不定长的多条请求」拍平成一段连续数组喂给 kernel 的。
2. 读懂 `prepare_metadata` 如何把 `Req` 的三个长度字段（`extend_len` / `device_len` / `cached_len`）翻译成 `cu_seqlens_q`，并能解释 decode、纯 prefill、部分命中 prefill 三种分支为何如此构造。
3. 描述 `forward` 里「先 `store_kv` 落池，再 `wrapper.run` 读池」的固定顺序，以及 `plan` 一次、`run` 每层一次的两段式设计。
4. 理解为什么 CUDA Graph 必须用一个**专用的 `CUDAGraphBatchDecodeWithPagedKVCacheWrapper`**，以及它和普通 wrapper 的差别。

## 2. 前置知识

本讲默认你已经掌握以下内容（均在前置讲义中讲过）：

- **`page_size = 1` 的池存储**（u6-l1 / u6-l3）：KV cache 是一块 `(2, num_layers, num_pages, page_size, local_kv_heads, head_dim)` 的大显存，`page_size=1` 时每个 page 恰好装一个 token。`page_table` 里存的是**逐 token 的槽位下标**（不是页号），`free_slots` 存的也是下标，可直接写入 `page_table`。
- **`Req` 的长度三件套**（u2-l1）：`cached_len`（已缓存）、`device_len`（本轮要算到的逻辑游标）、`extend_len = device_len - cached_len`（本轮新算的 query 数）。decode 恒有 `extend_len = 1`，prefill 时 `extend_len` 等于本块要新算的 prompt 长度。
- **`Batch` 的填充机制**（u5-l3）：decode 批会被 `dummy_req` 补齐到捕获尺寸，所以注意类用到的是 `padded_reqs` 而非 `reqs`。
- **注意力后端抽象**（u7-l1）：`prepare_metadata` 每批调用一次（在 Scheduler 前向之前），`forward` 每层调用一次；CUDA Graph 只对 decode 生效。

还需要一点 FlashInfer 的背景直觉（不熟悉也不影响读源码，下面会结合代码解释）：

- FlashInfer 用 **paged attention** 处理变长 batch：每条请求的 KV 由若干「页」拼接而成，请求与请求的 KV 在显存里**不连续**，靠一个指针数组 `indices` 指过去。
- 它用类似 CSR 稀疏矩阵的 **`indptr`**（前缀和）来划分「哪段 `indices` 属于哪条请求」。
- 工作分两步：先 **`plan`**（规划，算出块稀疏结构、写进 wrapper 内部），再 **`run`**（按规划好的结构真正做注意力）。`plan` 一批一次，`run` 一层一次。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/minisgl/attention/fi.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py) | 本讲主角。定义 `FIMetadata`、`FlashInferBackend`、`FICaptureData`，是 `--attn ...fi` 时真正干活的后端。 |
| [python/minisgl/attention/utils.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/utils.py) | `BaseCaptureData`：CUDA Graph 捕获阶段共享的「固定地址、内容可变」缓冲基类。 |
| [python/minisgl/attention/base.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/base.py) | 抽象基类 `BaseAttnBackend` / `BaseAttnMetadata`（u7-l1 已讲，本讲作对照）。 |
| [python/minisgl/core.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py) | `Batch` / `Req` 定义，提供本讲读取的 `extend_len` / `device_len` / `cached_len` / `out_loc` / `table_idx` 等字段。 |
| [python/minisgl/kvcache/mha_pool.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/mha_pool.py) | `MHAKVCache.store_kv` / `k_cache` / `v_cache`，被 `forward` 调用。 |
| [python/minisgl/engine/graph.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py) | `GraphRunner` 在捕获/回放时调用本后端的三个 capture 方法。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**FIMetadata（数据描述）→ prepare_metadata（描述如何构造）→ plan/run（如何计算）→ capture graph wrapper（如何进 CUDA Graph）**。前三者是普通前向路径，最后一个是 decode 专用的高速路径。

### 4.1 FIMetadata：一批请求的「注意力说明书」

#### 4.1.1 概念说明

FlashInfer 的 paged attention kernel 不认识 `Req`，也不认识 `Batch`，它只认一组「数值化」的描述符：每条请求有几个 query、它的 KV 分布在池子哪些槽位、最后一个页装了多少。`FIMetadata` 就是把这些描述符打包成一个 `dataclass`，挂在 `batch.attn_metadata` 上。它是 u7-l1 里 `BaseAttnMetadata` 的具体实现。

核心是 **CSR 格式的两组数组**：

- `cu_seqlens_q`（query 前缀和）：第 \(i\) 条请求的 query 落在 `q[cu_seqlens_q[i] : cu_seqlens_q[i+1]]`。
- `cu_seqlens_k` + `indices`（KV 前缀和 + 槽位指针）：第 \(i\) 条请求的 KV 是池子里 `indices[cu_seqlens_k[i] : cu_seqlens_k[i+1]]` 这几个槽位。

例如两条请求，KV 长度分别为 3 和 5，槽位是 `[7,8,9]` 与 `[2,3,4,5,6]`，则：

\[
\text{cu\_seqlens\_k} = [0,\,3,\,8],\qquad \text{indices} = [7,8,9,2,3,4,5,6]
\]

#### 4.1.2 核心流程

`FIMetadata` 本身是被动数据袋，构造流程在 `prepare_metadata`（4.2）。它只负责：

1. 在 `__post_init__` 里断言所有张量都在「期望设备」上（CPU 的留 CPU、GPU 的在 GPU），以及 `page_size == 1`。
2. 提供 `get_last_indices(bs)`：从 `cu_seqlens_q_gpu` 取出每条请求**最后一个 query token 的下标**，供 `ParallelLMHead` 在 prefill 时只算最后一位的 logits。

#### 4.1.3 源码精读

数据类定义见 [python/minisgl/attention/fi.py:46-77](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L46-L77)。关键字段：

```python
cu_seqlens_q_cpu:   torch.Tensor  # on cpu
cu_seqlens_k_cpu:   torch.Tensor  # on cpu
cu_seqlens_q_gpu:   torch.Tensor  # on gpu
indices:            torch.Tensor  # on gpu
last_page_len_cpu:  torch.Tensor  # on cpu
...
page_size:          Literal[1] # currently only support page_size=1
wrapper:            BatchPrefillWithPagedKVCacheWrapper | BatchDecodeWithPagedKVCacheWrapper
initialized:        bool = False
```

注意几个细节（这些都会在后面模块解释）：

- **`page_size` 被锁死为 `1`**。这是整个 FlashInfer 后端的硬约束，[python/minisgl/attention/fi.py:65-66](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L65-L66) 的 `__post_init__` 直接断言。所以池子里「一页一 token」，`indices` 存的就是逐 token 槽位下标，与 u6-l3 里 `free_slots` / `page_table` 存下标的设计完全对齐。
- **CPU / GPU 混合存放**：`cu_seqlens_*_cpu`、`last_page_len_cpu`、`seq_lens_cpu` 留在 CPU（带 `pin_memory`），因为 `plan` 阶段 FlashInfer 要在 host 侧做规划并异步 H2D 拷贝（见 4.3）；`cu_seqlens_q_gpu` 与 `indices` 在 GPU，因为 `run` 阶段 kernel 要直接读。
- **`initialized` 标志位**：把「真正的 `plan`」延迟到第一次 `forward`（第一层）才执行，避免在 `prepare_metadata` 里就触发规划（见 4.3）。
- `get_last_indices` 见 [python/minisgl/attention/fi.py:76-77](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L76-L77)，返回 `cu_seqlens_q_gpu[1:1+bs] - 1`，即每条请求最后 query 的下标。

#### 4.1.4 代码实践

**目标**：确认 `FIMetadata` 在系统里只有一个生产者（`prepare_metadata`）、多个消费者（`forward` / `get_last_indices` / capture 三方法）。

**操作步骤**：

1. 打开 [python/minisgl/attention/fi.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py)。
2. 全仓搜索 `FIMetadata(` 构造点和 `isinstance(metadata, FIMetadata)` 消费点。

**需要观察的现象**：`FIMetadata(...)` 只在 `prepare_metadata` 里被构造一次；其余地方都只读它。

**预期结果**：你会看到唯一的构造在 `prepare_metadata`（[fi.py:211](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L211)），而 `forward`、`prepare_for_capture`、`prepare_for_replay` 都用 `assert isinstance(metadata, FIMetadata)` 把它「认领」出来再读字段。这说明 metadata 是「一次构造、多处只读」的不可变描述符。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cu_seqlens_q` 既有 `_cpu` 版又有 `_gpu` 版，而 `indices` 只有 GPU 版？

**参考答案**：`cu_seqlens_q_cpu` / `cu_seqlens_k_cpu` 是 `plan`（CPU 侧规划）的输入，必须留 CPU；`cu_seqlens_q_gpu` 是 `get_last_indices`（给 `ParallelLMHead` 取最后一位 logits）和 kernel 读的输入，必须在 GPU。`indices` 只在 `run`（GPU kernel）里被读，因此只造 GPU 版，构造时直接 `torch.cat([...])` 落在 `self.device` 上（见 [fi.py:215](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L215)）。

**练习 2**：如果将来要让 FlashInfer 后端支持 `page_size = 64`，`FIMetadata.__post_init__` 的哪一行会率先报错？

**参考答案**：[fi.py:66](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L66) 的 `assert self.page_size == 1` 会触发。这说明当前实现是「逐 token 分页」的特化版本，改 page_size 不只是删掉断言，还要重写 `last_page_len`、`indices` 收集逻辑（4.2）以及 `forward` 里的 `_flatten_cache`（4.3）。

---

### 4.2 prepare_metadata：把 Req 翻译成 CSR

#### 4.2.1 概念说明

`prepare_metadata` 是 `BaseAttnBackend` 规定的「每批一次」方法，由 Scheduler 在前向之前调用（[scheduler.py:211](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L211)）。它的唯一职责是：读 `Batch` 里每条 `Req` 的长度字段，算出 FlashInfer 要的 CSR 数组，然后构造 `FIMetadata` 挂回 `batch.attn_metadata`。

它不做任何 GPU 计算，纯 CPU 拼数组——所以这一步可以和上一批的 GPU 前向重叠（这是 Overlap Scheduling 能藏住 CPU 开销的关键之一，见 u4-l1）。

#### 4.2.2 核心流程

设 `padded_reqs` 有 \(N\) 条请求（含 dummy 补齐）。对每条请求取三个长度：

\[
\text{seqlens\_q}[i] = \text{extend\_len}[i],\quad
\text{seqlens\_k}[i] = \text{device\_len}[i],\quad
\text{cached\_lens}[i] = \text{cached\_len}[i]
\]

- **KV 侧**（固定算法）：
  \[
  \text{cu\_seqlens\_k} = \mathrm{cumsum}([0] + \text{seqlens\_k})
  \]
  这是把所有请求的 KV 拼成一段连续下标空间的前缀和。

- **Query 侧**（三分支，见下文）。

- **indices**：把每条请求在 `page_table` 中的前 `device_len` 个槽位下标 `torch.cat` 起来，按请求顺序拼接，`cu_seqlens_k` 正好把它切成每条请求一段。

- **last_page_len**：恒为全 1（`page_size=1`，每页正好装满 1 个 token）。

Query 侧 `cu_seqlens_q` 的三分支是本模块最值得品味的部分（见 [fi.py:203-208](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L203-L208)）：

1. **decode（全部 `extend_len == 1`）**：每条请求只贡献 1 个 query，于是前缀和就是等差数列：
   \[
   \text{cu\_seqlens\_q} = [0, 1, 2, \dots, N] = \mathrm{arange}(0,\,N+1)
   \]
2. **纯 prefill（全部 `cached_len == 0`，无前缀命中）**：此时 `extend_len == device_len`，所以 query 数等于 KV 数，`cu_seqlens_q` 与 `cu_seqlens_k` 完全相同——直接复用已算好的张量，省一次分配：
   \[
   \text{cu\_seqlens\_q} = \text{cu\_seqlens\_k}
   \]
3. **部分命中 prefill（extend，`cached_len > 0`）**：一般情形，老老实实按 `extend_len` 算前缀和：
   \[
   \text{cu\_seqlens\_q} = \mathrm{cumsum}([0] + \text{seqlens\_q})
   \]

#### 4.2.3 源码精读

完整方法在 [python/minisgl/attention/fi.py:190-225](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L190-L225)。关键片段：

```python
reqs = batch.padded_reqs
seqlens_q = [req.extend_len for req in reqs]
seqlens_k = [req.device_len for req in reqs]
cached_lens = [req.cached_len for req in reqs]
...
cu_seqlens_k_cpu = torch.tensor([0] + seqlens_k, **CPU_KWARGS).cumsum_(dim=0)
if max_seqlen_q == 1:                       # decode：每条 1 个 query
    cu_seqlens_q_cpu = torch.arange(0, padded_size + 1, **CPU_KWARGS)
elif all(l == 0 for l in cached_lens):      # 纯 prefill：无缓存命中
    cu_seqlens_q_cpu = cu_seqlens_k_cpu      # 复用！
else:                                       # extend：部分命中
    cu_seqlens_q_cpu = torch.tensor([0] + seqlens_q, **CPU_KWARGS).cumsum_(dim=0)
```

`indices` 的收集（[fi.py:215](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L215)）：

```python
indices=torch.cat([page_table[req.table_idx, : req.device_len] for req in reqs]),
```

每条请求取自己 `page_table` 那一行的前 `device_len` 列（即它到目前为止用过的所有 KV 槽位），按请求顺序拼成一段连续的 GPU int32 数组。注意它用的是全局 `get_global_ctx().page_table`——正是 u6-l3 里 CacheManager 写入的那张表，FlashInfer 在这里直接读它。

最后构造 `FIMetadata`（[fi.py:211-225](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L211-L225)），其中 wrapper 按阶段二选一：

```python
wrapper=self.decode_wrappers if batch.is_decode else self.prefill_wrapper,
```

这呼应了 u7-l1 的 `HybridBackend`：当上层把 prefill 和 decode 用不同后端时，本后端可能只负责其中一段；当 `--attn fi` 单后端时，自己内部仍区分 prefill/decode 两个 wrapper（因为两者的 FlashInfer 入口函数不同，见 4.3）。

> 注意一个容易混淆的点：分支判断用的是 `max_seqlens_q == 1` 和 `all(cached_len == 0)`，而不是 `batch.is_decode`。理论上一个 prefill 批也可能所有请求都只算 1 个新 token，此时它会走「decode」的 q-indptr 分支，但 wrapper 仍由 `is_decode` 决定。实践中 decode 批的 `extend_len` 恒为 1，所以这两套判断高度吻合。

#### 4.2.4 代码实践（本讲指定实践）

**目标**：对照 `prepare_metadata` 的三分支，亲手推演 decode 与纯 prefill 两种情况下 `cu_seqlens_q` 的构造差异。

**操作步骤**（纯阅读 + 手算，可在无 GPU 环境完成）：

1. 打开 [python/minisgl/attention/fi.py:190-225](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L190-L225)。
2. 设想一个 `padded_reqs = [R0, R1, R2]` 的 batch。

   **情形 A（decode）**：三条请求都已经 decode 到 `device_len = 10`，本轮各算 1 个新 token。
   - `extend_len = [1,1,1]`，`device_len = [10,10,10]`，`cached_len = [9,9,9]`。
   - 手算 `cu_seqlens_q`、`cu_seqlens_k`、`indices` 的形状与内容。

   **情形 B（纯 prefill，无缓存命中）**：三条全新 prompt，长度 `4, 2, 3`，`cached_len` 全为 0。
   - `extend_len = [4,2,3]`，`device_len = [4,2,3]`，`cached_len = [0,0,0]`。
   - 手算同样三个量。

3. 对照源码核对你的手算结果。

**需要观察的现象**：

- 情形 A：`max_seqlen_q == 1` 成立 → `cu_seqlens_q = [0,1,2,3]`；而 `cu_seqlens_k = [0,10,20,30]`。两者完全不同——q 只有 3 个 token（每条 1 个），但每条要读取 10 个 KV。
- 情形 B：`all(cached_len==0)` 成立 → `cu_seqlens_q` 直接复用 `cu_seqlens_k = [0,4,6,9]`。验证此时 query 总数（9）确实等于 KV 总数（9）。

**预期结果**：你会清楚地看到，decode 与 prefill 的本质差异在「q 的稀疏度」——decode 时 q 是极度稀疏的（每请求 1 个 query 指向一长串 KV），所以 FlashInfer 走专门的 decode kernel；纯 prefill 时 q 与 k 等密，复用 k 的 indptr 即可。

> 如果你想在本地真正跑一遍，可以写一个最小脚本：构造 3 个假的 `Req`（`input_ids` 为 cpu int32 张量、`cached_len/device_len` 满足不变量），组装成 `Batch`，调用后端的 `prepare_metadata` 后打印 `batch.attn_metadata.cu_seqlens_q_cpu`。**待本地验证**（需要真实 `Req`/`cache_handle`，构造较繁琐，阅读推演已足够）。

#### 4.2.5 小练习与答案

**练习 1**：为什么纯 prefill 分支可以写 `cu_seqlens_q_cpu = cu_seqlens_k_cpu`，而 extend 分支不能？

**参考答案**：纯 prefill 时 `cached_len == 0`，由 `extend_len = device_len - cached_len` 得 `extend_len == device_len`，于是 `seqlens_q == seqlens_k`，前缀和自然相等，复用省一次分配。extend 分支里 `cached_len > 0`，`extend_len < device_len`，`seqlens_q != seqlens_k`，必须独立计算。

**练习 2**：`indices` 的总长度由什么决定？它和 `cu_seqlens_k` 的最后一个元素有什么关系？

**参考答案**：`indices` 长度 = `sum(device_len)` = 所有请求本轮读取的 KV 槽位总数。而 `cu_seqlens_k[-1]` = `cumsum(device_len)` 的最后一个值 = 同一个总和，所以 `cu_seqlens_k[-1] == len(indices)`，这正是 CSR 格式「前缀和末项 = 数组长度」的不变量。

---

### 4.3 plan 与 run：先写池，再读池

#### 4.3.1 概念说明

有了 `FIMetadata`，`forward` 才真正算注意力。FlashInfer 把一次注意力的执行拆成两段：

- **`plan`（规划）**：一批一次。根据 `indptr` / `indices` / 头数等，算出「块稀疏」的执行计划，写进 wrapper 内部的 instruction buffer。这一步主要在 CPU 做规划，并触发一次异步 H2D 把规划结果搬上 GPU。
- **`run`（执行）**：一层一次。用已经规划好的结构，对 query 和 paged KV 做真正的注意力计算。

为什么要拆？因为一个 batch 有几十层 decoder，每层的 `plan` 结果都一样（KV 布局不随层变，变的只是第几层的 K/V 数据）。规划一次、复用每层，把规划开销摊薄到 1/num_layers。

`forward` 内部还有一步关键动作：**`store_kv`**——把当前层新算的 K/V 按 `out_loc` 写进池子。顺序必须是「先写、再读」，因为 `run` 要读到本轮刚写入的 K/V（对齐 u6-l1 的「定序先写后读」）。

#### 4.3.2 核心流程

`forward` 的执行顺序（[fi.py:176-188](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L176-L188)）：

```
1. 取出 batch.attn_metadata（FIMetadata）
2. _initialize_metadata_once(metadata)   # 懒 plan：仅第一层真正调 plan
3. kvcache.store_kv(k, v, batch.out_loc, layer_id)   # 把本层新 K/V 写进池
4. kv_cache = (k_cache(layer_id), v_cache(layer_id))  # 取本层池视图
5. kv_cache = (_flatten_cache(...), _flatten_cache(...))  # 视作 page=1 拍平
6. return metadata.wrapper.run(q=q, paged_kv_cache=kv_cache)  # 读池算注意力
```

`plan` 的两段式（decode 用 `indptr/indices`，prefill 用 `qo_indptr/paged_kv_indptr`）在 `_initialize_metadata_once` 里按 wrapper 类型分派（[fi.py:123-166](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L123-L166)）。

#### 4.3.3 源码精读

`forward` 见 [python/minisgl/attention/fi.py:176-188](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L176-L188)：

```python
def forward(self, q, k, v, layer_id, batch):
    def _flatten_cache(cache):               # 把 (pages, page_size, heads, dim)
        return cache.view(-1, 1, cache.shape[2], cache.shape[3])  # 视作 (tokens, 1, heads, dim)
    metadata = batch.attn_metadata
    assert isinstance(metadata, FIMetadata)
    self._initialize_metadata_once(metadata) # 懒 plan
    self.kvcache.store_kv(k, v, batch.out_loc, layer_id)  # 先写
    kv_cache = (self.kvcache.k_cache(layer_id), self.kvcache.v_cache(layer_id))
    kv_cache = (_flatten_cache(kv_cache[0]), _flatten_cache(kv_cache[1]))
    return metadata.wrapper.run(q=q, paged_kv_cache=kv_cache)  # 再读
```

`_flatten_cache` 利用 `page_size=1`：池里第 `layer_id` 层的 K buffer 形状是 `(num_pages, 1, local_kv_heads, head_dim)`，`.view(-1, 1, heads, dim)` 把它拍成 `(num_pages, 1, heads, dim)`——即「每个槽位一行、每行一个 token」。于是 `indices`（存槽位下标）就能直接索引到这一行的 K/V，`run` 据此做 paged attention。

懒 plan 见 [python/minisgl/attention/fi.py:123-166](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L123-L166)：

```python
def _initialize_metadata_once(self, metadata):
    if metadata.initialized:
        return                                  # 第二层起直接返回
    metadata.initialized = True
    self.last_event.synchronize()               # 等上一批 plan 的 H2D 拷贝完成
    if isinstance(metadata.wrapper, BatchDecodeWithPagedKVCacheWrapper):
        metadata.wrapper.plan(indptr=..., indices=..., last_page_len=..., non_blocking=True)
    else:                                        # prefill wrapper
        metadata.wrapper.plan(qo_indptr=..., paged_kv_indptr=..., causal=True, non_blocking=True)
    self.last_event.record()
```

两个关键点：

1. **懒触发**：`metadata.initialized` 一开始是 `False`，所以第一层 `forward` 才真正 `plan`，之后所有层直接 `return`。这就解释了为什么 `prepare_metadata` 只造数据、不规划——把规划推迟到「确实要算」的时刻，与 Overlap Scheduling 的 stream 切换时机更契合。
2. **`last_event` 同步**：`plan(..., non_blocking=True)` 会启动一次异步 H2D 拷贝，且 FlashInfer 内部**复用同一块 pinned host 暂存区**。如果连续两批的 `plan` 紧挨着发，后一批会覆盖前一批还没搬完的 host 缓冲。于是每批 `plan` 前先 `synchronize` 上一批的 event，`plan` 后 `record` 一个新 event。这在 overlap 模式下尤其重要（多批 metadata 可能同时在途）。

`decode` 的 `plan` 参数（[fi.py:134-148](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L134-L148)）把 `cu_seqlens_k_cpu` 作为 `indptr`、`indices` 作为 KV 指针；`prefill` 的 `plan`（[fi.py:150-165](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L150-L165)）多传一个 `qo_indptr=cu_seqlens_q_cpu`（因为 prefill 每条请求 query 数不等），并开 `causal=True`（因果掩码，prefill 时未来 token 不可见）。

最后，后端构造时准备了两个 wrapper（[fi.py:93-103](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L93-L103)）：`BatchPrefillWithPagedKVCacheWrapper` 与 `BatchDecodeWithPagedKVCacheWrapper`，共享一块 128 MiB 的 `float_workspace_buffer`，并 hack 复用 `int_workspace_buffer` 省显存；两者都用 `backend="fa2"`（注释说明 flashinfer 的 fa3 反而更慢）。

#### 4.3.4 代码实践

**目标**：验证「先 `store_kv`、后 `wrapper.run`」的顺序不可颠倒。

**操作步骤**：

1. 打开 [python/minisgl/attention/fi.py:176-188](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L176-L188)。
2. 阅读 `store_kv` 的实现 [python/minisgl/kvcache/mha_pool.py:45-56](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/mha_pool.py#L45-L56)：它把当前层新算的 `k, v` 按 `out_loc` scatter 写进 `_k_buffer[layer_id]` / `_v_buffer[layer_id]`。

**需要观察的现象**：`forward` 第 185 行先 `store_kv`，第 188 行才 `wrapper.run`；而 `run` 读的正是同一个 `layer_id` 的池视图。

**预期结果**：你会确认本轮新生成的 token 的 K/V 必须先落池，注意力才能在「读池」时把它纳入计算。若把两行对调，本轮 token 会读到未初始化（或上一轮残留）的 K/V，结果错误。这就是 u6-l1 强调的「定序须先写后读」在 FlashInfer 后端的具象体现。

> 顺带观察：`store_kv` 用的 `out_loc` 来自 Scheduler 写入的 `batch.out_loc`（[scheduler.py:210](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L210)），它就是「本轮新算的这些 token 该写到池子哪些槽位」的下标，与 `indices`（读哪些槽位）来源相同（都是 `page_table`），形成闭环。

#### 4.3.5 小练习与答案

**练习 1**：一个 32 层的模型、batch 里有 4 条 decode 请求，`plan` 会执行几次？`run` 会执行几次？

**参考答案**：`plan` 执行 1 次（仅第一层触发，`metadata.initialized` 之后短路）；`run` 执行 32 次（每层一次）。这正是「规划一次、复用每层」的设计动机——把 plan 的固定开销摊到 1/32。

**练习 2**：`_initialize_metadata_once` 里的 `self.last_event.synchronize()` 去掉会怎样？

**参考答案**：`plan(..., non_blocking=True)` 复用 FlashInfer 内部的 pinned host 暂存区做异步 H2D。若连续两批 plan 不加同步，后一批的 host 写会覆盖前一批尚未搬运完的数据，导致前一批的规划结果错乱、注意力读到错误的 KV 布局。这个 event 是 overlap 模式下多批 metadata 并发时的正确性护栏。

---

### 4.4 CUDA Graph 捕获专用 wrapper

#### 4.4.1 概念说明

u5-l3 讲过：decode 每轮形状固定，可以录成 CUDA Graph 反复 `replay`，省掉逐层 launch kernel 的 CPU 开销。但普通的 `BatchDecodeWithPagedKVCacheWrapper.plan()` 内部包含 **H2D 拷贝和 host 侧工作**，这些是「不能被录制进 graph」的（graph 只能录 GPU kernel launch）。

为此 FlashInfer 提供了专用类 `CUDAGraphBatchDecodeWithPagedKVCacheWrapper`：它的 `plan` 把结果写进**预先分配好的固定 GPU buffer**（`indptr_buffer` / `indices_buffer` / `last_page_len_buffer`），整个过程可被 graph 捕获。Mini-SGLang 在 `init_capture_graph` / `prepare_for_capture` / `prepare_for_replay` 三个方法里装配它。

#### 4.4.2 核心流程

三段式（对应 u7-l1 的 capture 协议）：

```
init_capture_graph(max_seq_len, bs_list)   # 建一次：分配 max_bs 级 capture 数据
        │
        ▼
prepare_for_capture(batch)  # 每个 bs 调一次：为该 bs 建专用 graph_wrapper 并 plan
        │                     （在 GraphRunner 录制每个尺寸图时调用）
        ▼
prepare_for_replay(batch)   # 每次 decode 回放调一次：换上该 bs 的 graph_wrapper 并重 plan
                              （在 GraphRunner.replay 时调用）
```

要点：`prepare_for_replay` **每一步都要重新 `plan`**，因为每批的 `indices`（KV 槽位映射）不同；但 plan 写的是固定 buffer，graph 回放时读这些 buffer 即可。

#### 4.4.3 源码精读

先看捕获数据基类 [python/minisgl/attention/utils.py:6-23](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/utils.py#L6-L23)：

```python
@dataclass
class BaseCaptureData:
    seq_lens / positions / cu_seqlens_k / cu_seqlens_q / page_table  # 全是 GPU 张量

    @classmethod
    def create(cls, max_bs, max_seq_len, device, **kwargs):
        return cls(seq_lens=torch.ones((max_bs,)), ...,
                   cu_seqlens_k=torch.arange(0, max_bs + 1), ...,
                   page_table=torch.zeros((max_bs, max_seq_len)), ...)
```

`BaseCaptureData` 体现 CUDA Graph 的核心哲学——**地址固定、内容可变**：在 `create` 时按 `max_bs` 一次性分配好最大尺寸的 GPU buffer，录制时录下这些地址，回放时只改写它们的内容。`cu_seqlens_k/q` 初始化为 `arange(0, max_bs+1)`，正好就是 decode 的 q-indptr 形状（每个请求 1 个 query）。

FlashInfer 的捕获数据子类 [python/minisgl/attention/fi.py:35-43](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L35-L43)：

```python
@dataclass
class FICaptureData(BaseCaptureData):
    @property
    def one_tensor(self):   # 全 1 张量，给 last_page_len 用（page_size=1 恒满）
        return self.seq_lens
    @property
    def indices(self):      # 把 2D page_table 视作 1D ragged indices
        return self.page_table
```

`init_capture_graph` 见 [python/minisgl/attention/fi.py:227-234](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L227-L234)：用 `max_bs` 建一份 `FICaptureData`，并把 `page_table` 展平成 1D（因为 FlashInfer 的 indices 是一维 ragged 数组）。

`prepare_for_capture` 见 [python/minisgl/attention/fi.py:244-264](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L244-L264)，对每个捕获尺寸 `bs`：

```python
bs = batch.size
self.graph_wrappers[bs] = CUDAGraphBatchDecodeWithPagedKVCacheWrapper(
    self.float_workspace_buffer,
    kv_layout="NHD",
    use_tensor_cores=self.use_tensor_cores,
    indptr_buffer=capture.cu_seqlens_k[: bs + 1],   # 切出 bs+1 长的固定 buffer
    indices_buffer=capture.indices,                  # 1D ragged 指针
    last_page_len_buffer=capture.one_tensor[:bs],    # bs 个 1
)
self.graph_wrappers[bs]._backend = "fa2"
self.graph_wrappers[bs]._int_workspace_buffer = self.int_workspace_buffer  # 复用
self.prepare_metadata(batch)                         # 用真实 batch 建一份 metadata
metadata.wrapper = self.graph_wrappers[bs]           # 换成 graph wrapper
self._initialize_metadata_once(metadata)             # 触发一次 plan
```

注意它把 buffer **切片**（`[:bs+1]`、`[:bs]`）而不是新分配——切片得到的仍是原 buffer 的视图，地址固定，可被 graph 录制。`prepare_metadata` 之后把 `metadata.wrapper` 从普通 decode wrapper 换成 graph wrapper，再 plan 一次，让这个 graph wrapper 内部带上正确的规划结果。

`prepare_for_replay` 见 [python/minisgl/attention/fi.py:266-271](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L266-L271)：

```python
def prepare_for_replay(self, batch):
    metadata, bs = batch.attn_metadata, batch.padded_size
    assert isinstance(metadata, FIMetadata) and not metadata.initialized
    metadata.wrapper = self.graph_wrappers[bs]   # 换上该 bs 的 graph wrapper
    self._initialize_metadata_once(metadata)     # 用当前 batch 的 indices 重新 plan
```

这里每次回放前，Scheduler 已经用当前真实 batch 调过 `prepare_metadata`（[scheduler.py:211](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L211)），造出的 `metadata.initialized` 是 `False`、`wrapper` 是普通 decode wrapper。`prepare_for_replay` 把 wrapper 换成该 `bs` 的 graph wrapper，并立刻 plan——plan 把当前 batch 的 `indices`/`indptr` 写进 graph wrapper 的固定 buffer。随后 `GraphRunner.replay`（[graph.py:152-158](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L152-L158)）执行的图就读这些 buffer，完成 paged decode attention。

> 为什么要「换 wrapper」？因为 plan 写的是 wrapper 内部的固定 buffer，每个 `bs` 有自己的 graph_wrapper（`self.graph_wrappers` 字典），录制时录下了对应地址。回放时必须把 metadata 指向「与当前 `padded_size` 匹配」的那个 graph_wrapper，地址才对得上录好的图。

#### 4.4.4 代码实践

**目标**：理清「普通 decode wrapper」与「graph decode wrapper」的分工，以及它们何时被切换。

**操作步骤**：

1. 在 [python/minisgl/engine/graph.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py) 里找 `prepare_for_capture` 与 `prepare_for_replay` 的调用点（[graph.py:136](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L136) 与 [graph.py:156](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L156)）。
2. 回到 [fi.py:244-271](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L244-L271)，跟踪一次 `metadata.wrapper` 的取值变化：先在 `prepare_metadata` 里被设成 `self.decode_wrappers`，再在 `prepare_for_replay` 里被换成 `self.graph_wrappers[bs]`。

**需要观察的现象**：graph 路径下，普通 decode wrapper 的 `plan` 从未真正执行（因为 `forward` 走的是 graph replay，不经过 `FlashInferBackend.forward`），只有 graph wrapper 的 plan 会执行。

**预期结果**：你会看到「换 wrapper」是连接「每批新 metadata」与「固定地址 graph buffer」的桥梁——普通 wrapper 负责非 graph 路径，graph wrapper 负责回放路径，二者靠 `metadata.wrapper = ...` 在 `prepare_for_replay` 里切换。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `prepare_for_replay` 必须在每次回放前重新 `plan`，而不是录制时 plan 一次就够了？

**参考答案**：每批 decode 请求的 KV 槽位布局（`indices`、`indptr`）都在变（新 token 不断写入、请求进进出出），plan 的作用正是把当前布局写进 wrapper 的固定 buffer。录制时 plan 的是 dummy batch 的布局，与真实 batch 无关；只有每次回放前用真实 metadata 重 plan，graph 在 replay 时读到的才是正确的当前布局。

**练习 2**：`FICaptureData.indices` 为何要把 2D `page_table` 展平成 1D？

**参考答案**：FlashInfer 的 paged attention 用 CSR 格式：`indices` 是一维的 ragged 指针数组，靠 `indptr`（`cu_seqlens_k`）切分给各请求。2D `page_table`（每行一条请求）展平后正好就是按请求顺序拼接的一维指针数组，配合 `cu_seqlens_k[:bs+1]` 切片即可表达「bs 条请求各自的 KV 槽位」。展平是把 Mini-SGLang 的二维表视图适配到 FlashInfer 一维 CSR 视图的转换步骤。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「**一条 decode 请求在 FlashInfer 后端的完整注意力之旅**」的源码追踪：

1. **起点**：Scheduler 为某 decode 批调用 `_prepare_batch`，其中 [scheduler.py:211](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L211) 调 `attn_backend.prepare_metadata(batch)`。
2. **翻译**（4.2）：进入 [fi.py:190](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L190)，因 `extend_len` 全为 1，走 decode 分支，`cu_seqlens_q = arange(0, N+1)`；`indices` 从 `page_table` 收集每条请求的槽位；挂上 `FIMetadata`。
3. **若走 graph 路径**（4.4）：`GraphRunner.replay` → `prepare_for_replay` 换上 `graph_wrappers[bs]` 并重 plan（[fi.py:266](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L266)）。
4. **每层前向**（4.3）：`AttentionLayer.forward` 调 `attn_backend.forward(q,k,v,layer_id,batch)`（[attention.py:56](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py#L56)）→ 第一层懒 `plan`（[fi.py:184](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L184)）→ `store_kv` 写池（[fi.py:185](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L185)）→ `wrapper.run` 读池（[fi.py:188](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L188)）。
5. **终点**：注意力输出回到 `AttentionLayer`，经残差、MLP 进入下一层，循环至所有层完成。

**你的任务**：画出这条调用链的时序图（横轴时间，纵轴列出 Scheduler / FlashInferBackend / kvcache / wrapper 四个角色），标注：
- `prepare_metadata` 在哪一步把 `cu_seqlens_q` 设成 `arange`；
- `plan` 在第几层触发、之后是否再触发；
- `store_kv` 与 `run` 的先后与依赖；
- graph 路径下 wrapper 的切换点。

> 进阶：若你本地有 GPU，可用 `MINISGL_DISABLE_OVERLAP_SCHEDULING=1`（见 u4-l1）跑一次离线 bench，在 `forward` 的 `store_kv` 与 `wrapper.run` 之间各加一行日志（仅阅读练习，不要提交改动），观察每层调用顺序与 4.3 描述是否一致。**待本地验证**。

## 6. 本讲小结

- **`FIMetadata`** 是把一个 batch 翻译成 FlashInfer CSR 描述符的数据袋：`cu_seqlens_q/k` 是前缀和、`indices` 是逐 token 槽位指针，且硬约束 `page_size == 1`。
- **`prepare_metadata`** 用 `extend_len/device_len/cached_len` 算 q-indptr，三分支（decode→arange、纯 prefill→复用 k-indptr、extend→独立 cumsum）体现了对长度语义的精细复用。
- **`forward`** 遵循「懒 plan（一批一次）→ `store_kv` 先写池 → `wrapper.run` 后读池」的固定顺序，`last_event` 护栏防止异步 H2D 覆盖。
- **`plan` 一次、`run` 每层一次** 是两段式设计的核心，把规划开销摊薄到 1/num_layers。
- **CUDA Graph 必须用专用 `CUDAGraphBatchDecodeWithPagedKVCacheWrapper`**，因为它把 plan 结果写进固定 GPU buffer，可被录制；`prepare_for_replay` 每步换 wrapper 并重 plan。
- 整个后端只认「逐 token 槽位」与 `page_table`，与 u6 的池存储、CacheManager 完全对齐，是 KV cache 管理与注意力计算之间的桥梁。

## 7. 下一步学习建议

- **对比 FlashAttention 后端**：阅读 [python/minisgl/attention/fa.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fa.py) 的 `prepare_metadata`，看它如何用相似的长度字段构造 FA 所需的元数据，与 FlashInfer 做对照。
- **对比 trtllm 后端**：阅读 [python/minisgl/attention/trtllm.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/trtllm.py)，理解 u7-l1 提到的「SM100 选 trtllm 且强制 page_size=64」如何与本章 `page_size=1` 的实现形成取舍。
- **回看 CUDA Graph 全流程**：结合 u5-l3 重读 [engine/graph.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py)，把本章三个 capture 方法放回「捕获 / pad_batch / replay」的大图中。
- **向上追溯 KV 槽位来源**：回看 u6-l3 的 `CacheManager.allocate_paged` 与 `cache_req`，理解 `page_table` 里这些槽位下标是如何被分配、写入、回收的，闭环 FlashInfer 读取的 `indices` 的全生命周期。
