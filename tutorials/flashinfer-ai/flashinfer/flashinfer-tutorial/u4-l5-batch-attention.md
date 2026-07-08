# BatchAttention 统一混合批处理

## 1. 本讲目标

在持续批处理（continuous batching）的推理服务里，同一个 forward 迭代往往**同时**包含两类请求：

- 正在 **prefill**（提示词尚未算完，query 是一段长序列）；
- 正在 **decode**（已进入逐 token 生成，query 只有 1 个或几个 token）。

前面几讲我们用两个独立 wrapper（`BatchPrefillWithPagedKVCacheWrapper` 与 `BatchDecodeWithPagedKVCacheWrapper`）分别处理它们。本讲介绍另一种思路——`flashinfer.attention.BatchAttention`：它把整批请求塞进**一次** kernel launch，用一个「persistent（常驻）kernel」统一调度 prefill 与 decode。

学完本讲你应当能够：

1. 说清 `BatchAttention` 的定位：它是「holistic persistent kernel」式的统一 wrapper，而不是再写一遍 plan/run。
2. 复述它**在 plan 阶段**如何依据每个请求的 `qo_indptr` 区间（即 query 长度）把请求分派到「prefill 阶段 / decode 阶段」两个工作列表。
3. 理解「单次 cooperative launch + `grid.sync` 屏障 + 合并归约」如何让一个 kernel 同时服务两类负载。
4. 区分它与 u4-l4 介绍的 POD 混合批处理在机制上的根本不同，并知道各自适合什么场景。

---

## 2. 前置知识

本讲是「进阶注意力变体」单元的一篇，默认你已经掌握：

- **plan/run 两段式 API**（u3-l1、u3-l3、u3-l4）：plan 只依赖批次结构（含动态调度，不可进 CUDA Graph），run 只携带每层数据。`BatchAttention` 同样遵循这套约定。
- **paged KV-Cache 与 CSR 前缀和索引**（u3-l2）：`qo_indptr` / `kv_indptr` 是描述变长批次的 CSR（压缩稀疏行）数组，`qo_indptr[i+1]-qo_indptr[i]` 就是第 `i` 个请求的 query 长度 `qo_len`。
- **GQA（分组查询注意力）**：`num_qo_heads` 是 `num_kv_heads` 的整数倍，倍数 `gqa_group_size = num_qo_heads/num_kv_heads` 表示一个 KV 头被几个 Q 头共享。
- **JIT 三层架构**（第 2 单元）：Python wrapper → JIT 生成器 `gen_*_module` → csrc launcher + TVM-FFI 绑定 → include 模板 kernel。
- **LSE 合并 / 在线 softmax**（u4-l2 cascade）：被切分的多段 attention 部分结果可用 logsumexp 精确合并。本讲的 split-k 合并复用了同一套数学。

本讲会新引入两个 CUDA 概念，先建立直觉：

- **Persistent kernel（常驻核函数）**：普通 kernel「一个 CTA 干一块活、干完即退」；persistent kernel 启动约等于 SM 数量的 CTA，让它们**常驻不退**，循环地从全局工作列表里领取任务，直到全部干完。好处是把「很多小 kernel 的启动开销 + 尾部 SM 空转」换成了「一次启动 + 软件工作调度」。
- **Cooperative groups + `grid.sync()`**：CUDA 协作组允许整个 grid 的所有 CTA 在 kernel 内部做一次全局屏障（`grid.sync()`）。`BatchAttention` 用它在「prefill 阶段」与「decode 阶段」之间插一道屏障，保证两阶段在同一个 kernel 内顺序执行，且后者能看到前者的全局内存写入。

> 小提示：cooperative launch 要求 grid 大小不能超过设备最大可驻留 CTA 数，所以 persistent kernel 的 grid 维度是精心算出来的（见 4.3）。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`flashinfer/attention/_core.py`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/attention/_core.py) | `BatchAttention` 类定义：`__init__` 分配 workspace、`plan` 触发 JIT 并调用 C++ plan、`run` 调用 C++ run。 |
| [`flashinfer/attention/__init__.py`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/attention/__init__.py) | 把 `BatchAttention` 从包顶层导出。 |
| [`flashinfer/jit/attention/modules.py`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py) | `gen_batch_attention_module` / `gen_customize_batch_attention_module`：按 dtype/head_dim 渲染 Jinja、产出 `JitSpec`。 |
| [`csrc/batch_attention.cu`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_attention.cu) | C++ launcher：`BatchPagedAttentionPlan`（调 `TwoStageHolisticPlan`）与 `BatchPagedAttentionRun`（组装 `params[0]/params[1]` 并启动 persistent kernel）。 |
| [`csrc/batch_attention_jit_binding.cu`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_attention_jit_binding.cu) | TVM-FFI 导出 `plan` / `run` 两个符号。 |
| [`include/flashinfer/attention/scheduler.cuh`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh) | **核心调度逻辑**：`HolisticPlanInfo` 与 `TwoStageHolisticPlan`——按 `qo_indptr` 区间把请求划入 prefill/decode 两个阶段。 |
| [`include/flashinfer/attention/persistent.cuh`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/persistent.cuh) | `BlockBatchPagedAttentionPersistent`（每个 CTA 的工作循环）与 `BatchPagedAttentionPersistent`（host 端 cooperative launch）。 |
| [`include/flashinfer/attention/persistent_template.cuh`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/persistent_template.cuh) | `PersistentKernelTemplate`：单 kernel 内「prefill 阶段 → `grid.sync` → decode 阶段 → `grid.sync` → 合并归约」的三段顺序执行。 |
| [`tests/attention/test_batch_attention.py`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/attention/test_batch_attention.py) | 正确性测试：把混合批次喂给 `BatchAttention`，并与 `BatchPrefillWithPagedKVCacheWrapper(fa2)` 逐元素比对。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 统一 wrapper**：`BatchAttention` 的接口与 workspace 持有方式。
- **4.2 区间分派**：plan 阶段如何依据 `qo_indptr` 把请求划入 prefill/decode 两阶段（本讲的核心）。
- **4.3 混合批处理**：单一 persistent kernel + cooperative groups + split-k 合并归约如何把两阶段缝合成一次 launch。

### 4.1 统一 wrapper：BatchAttention 的定位与接口

#### 4.1.1 概念说明

`BatchAttention` 的定位是「**holistic（整体式）**」wrapper：它不为 prefill 与 decode 各开一个 wrapper，而是把整批请求一次性交给一个 persistent kernel。从用户视角看，它仍是熟悉的 `plan` / `run` 两段式；区别在于：

1. **workspace 由实例自己持有**。prefill/decode wrapper 要求用户外部传入 workspace 张量；`BatchAttention` 在 `__init__` 里直接分配三大块 workspace，后续 `plan`/`run` 复用。这意味着实例本身是有状态的、绑定在某个 CUDA device 上。
2. **「分派」发生在 plan 阶段的 C++ 侧**，而不是 Python 侧。Python wrapper 本身不做任何 prefill/decode 的 if-else，它只是把整批 `qo_indptr` 等结构原样传给 C++ 的 `plan`。

#### 4.1.2 核心流程

```text
实例化 BatchAttention(kv_layout, device)
        │  分配 float/int/page-locked int 三块 workspace（常驻）
        ▼
plan(qo_indptr, kv_indptr, kv_indices, kv_len_arr, 头参, page_size, ...)
        │  1. 按编译期参数(dtype/head_dim/...) 取/编译 JIT 模块  (get_holistic_attention_module)
        │  2. 把 qo_indptr/kv_indptr/kv_len_arr 拷到 CPU（plan 需在 host 读这些结构）
        │  3. module.plan(...) → 进入 C++ TwoStageHolisticPlan：
        │       依据 qo_indptr 把请求划入 阶段0(prefill) / 阶段1(decode) 两个工作列表
        │       返回 plan_info（一串 int64 偏移量，描述 workspace 里的索引数组）
        ▼
run(q, kv_cache, ...)
        │  module.run(...) → BatchPagedAttentionRun：
        │       组装 params[0](prefill)/params[1](decode)，一次 cooperative launch
        │       kernel 内：prefill 阶段 → grid.sync → decode 阶段 → grid.sync → 合并归约
        ▼
返回 (out, lse)
```

#### 4.1.3 源码精读

类的 docstring 点明了它的「fuses paged-prefill and paged-decode into a single kernel launch」定位，以及「按 `qo_indptr`/`kv_indptr` 区间分派」：

[flashinfer/attention/_core.py:44-62](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/attention/_core.py#L44-L62) —— 类定义与文档：说明 `BatchAttention` 把 paged-prefill 与 paged-decode 融合为单次 launch，并依据 `plan` 时给出的 `qo_indptr`/`kv_indptr` 区间对每个请求分派。

模块加载走的是和 single_decode/single_prefill 同样的两级缓存（`@functools.cache` + 磁盘 `.so`，见 u2-l5）：

[flashinfer/attention/_core.py:39-41](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/attention/_core.py#L39-L41) —— `get_holistic_attention_module` 被 `@functools.cache` 装饰，键是编译期参数元组，命中则直接复用已加载模块。

workspace 持有方式（注意三块 buffer 的用途差异）：

[flashinfer/attention/_core.py:77-92](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/attention/_core.py#L77-L92) —— 实例自持三块 workspace：`float_workspace_buffer`（384 MiB，存 partial_o/partial_lse 等浮点中间结果）、`int_workspace_buffer`（8 MiB，存 plan 生成的各类 CSR 索引数组）、`page_locked_int_workspace_buffer`（8 MiB、锁页 CPU 内存，plan 先在 CPU 上写好再 `cudaMemcpyAsync` 到 GPU）。

`plan` 触发 JIT 并把批次结构交给 C++ 的关键片段：

[flashinfer/attention/_core.py:170-213](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/attention/_core.py#L170-L213) —— 先用编译期参数组装 `get_module_args`（注意 `PosEncodingMode["NONE"]`：BatchAttention 固定不用 RoPE，位置编码由调用方在送入 q 前处理好），取到模块后把 `qo_indptr`/`kv_indptr`/`kv_len_arr` 拷到 CPU 并 `synchronize`，再调 `self.module.plan(...)` 得到 `_plan_info`。`_plan_info` 实际上是一个 int64 列表（见 4.2.3），记录了所有 CSR 索引数组在 workspace 里的偏移。

一个重要的能力边界（head_dim 上限）：

[flashinfer/attention/_core.py:160-168](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/attention/_core.py#L160-L168) —— `head_dim_qk` 或 `head_dim_vo` 大于 256 时直接抛错，并提示改用 fa2 prefill 或 tensor-core decode。这与 persistent kernel 内部的 `static_assert` 对应（见 4.3.3）。

`run` 仅做参数整理与 dtype 推断，把活儿全交给 C++：

[flashinfer/attention/_core.py:302-325](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/attention/_core.py#L302-L325) —— 调用 `self.module.run(...)`，把 `_plan_info`、q、k/v cache、`_kv_indices`、out/lse、各种 scale 等传入。注意 Python 侧没有任何 prefill/decode 的分支判断——**分派完全发生在 C++ plan 里**。

#### 4.1.4 代码实践

> 目标：用最少的代码把 `BatchAttention` 跑起来，确认它和「老办法」(`BatchPrefillWithPagedKVCacheWrapper(fa2)`) 在数值上一致。

操作步骤（参照测试 [`tests/attention/test_batch_attention.py:91-204`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/attention/test_batch_attention.py#L91-L204) 的模式）：

1. 构造一个混合批次：例如 `qo_lens = [128, 1, 1, 1]`（1 个 prefill + 3 个 decode），`kv_lens = [1024, 2048, 512, 4096]`。
2. 由 `qo_lens`/`kv_lens` 累加得到 CSR 数组 `q_indptr`、`kv_indptr`，并构造 paged KV-Cache（`page_size` 自选）。
3. 分别用 `BatchPrefillWithPagedKVCacheWrapper(backend="fa2")` 与 `BatchAttention` 跑一遍，比对输出。

需要观察的现象：

- `BatchAttention` 首次 `plan` 会触发 JIT 编译（耗秒级到分钟级，取决于 head_dim/dtype 组合是否命中缓存）；第二次相同参数的实例化几乎瞬时。
- 两者的 `out` 与 `lse` 应在 `rtol=1e-2, atol=1e-2` 内一致（这正是该测试的断言）。

预期结果（待本地验证）：两条路径输出一致，证明 `BatchAttention` 只是换了一种调度方式，数学结果不变。

```python
# 示例代码（仅展示调用骨架，参数需按你的 GPU/形状填）
import torch, flashinfer
dev = "cuda:0"
qo_lens = torch.tensor([128, 1, 1, 1], dtype=torch.int32, device=dev)
kv_lens = torch.tensor([1024, 2048, 512, 4096], dtype=torch.int32, device=dev)
q_indptr = torch.cat([torch.tensor([0], device=dev), qo_lens.cumsum(0)]).int()
# kv_indptr / kv_indices / kv_cache 的构造参见 test_batch_attention.py
wrapper = flashinfer.BatchAttention(kv_layout="NHD")
wrapper.plan(q_indptr, kv_indptr, kv_indices, kv_lens,
             num_qo_heads, num_kv_heads, head_dim, head_dim, page_size, causal=True)
out, lse = wrapper.run(q, kv_cache)
```

#### 4.1.5 小练习与答案

**练习 1**：`BatchAttention` 的 workspace 为什么由实例自己持有，而 prefill/decode wrapper 要用户外部传入？

**参考答案**：persistent kernel 的 plan 会把大量 CSR 索引数组写进 int workspace、把 split-k 的 partial_o/partial_lse 写进 float workspace，这些中间结构体积大且只与本 kernel 强相关；由实例统一持有可避免每次 plan/run 重复分配，也便于跨多层复用。而 prefill/decode wrapper 设计得更「无状态」，让用户掌控显存，便于在多个 wrapper 间共享一块大 workspace。

**练习 2**：`plan` 里为什么要 `qo_indptr.to(torch.device("cpu"))` 并 `torch.cuda.synchronize()`？

**参考答案**：因为 C++ 侧的 `TwoStageHolisticPlan` 是**在 host 上**读这些 CSR 数组来做请求分派与工作划分的（见 4.2），必须把数据搬到 CPU 并等拷贝完成，plan 才能读到正确的值。

---

### 4.2 区间分派：按 qo_indptr 划分 prefill/decode 两阶段

> 这是本讲的核心。`BatchAttention` 的「分派」不是 Python 里的 if-else，而是 plan 阶段在 host 上依据每个请求的 query 长度，把它丢进两个工作列表之一。

#### 4.2.1 概念说明

关键直觉：**「prefill」与「decode」在本 kernel 里不是两个 boolean 标签，而是两种 CTA tile 大小**。

- 阶段 0（prefill 阶段）：`CTA_TILE_Q = 128`，每个 CTA 一次处理 128 行（packed）query，适合 query 很长的请求；
- 阶段 1（decode 阶段）：`CTA_TILE_Q = 16`，每个 CTA 一次处理 16 行（packed）query，适合 query 很短（如 decode 的 1 个 token）的请求。

判定一个请求走哪个阶段，看它的「打包 query 长度」`packed_qo_len = qo_len * gqa_group_size`（把共享同一个 KV 头的若干 Q 头「打包」成连续行后，该请求在 packed 视角下的行数）：

- `packed_qo_len > 16` → 阶段 0（prefill tile 128）；
- 否则 → 阶段 1（decode tile 16）。

阈值 16 正好是 decode tile 的大小。所以判定本质是「**这批 query 行能不能塞进一个小 tile**」：单个 decode token 在 GQA 下（`gqa_group_size` 不超过 16）仍只有 ≤16 行，走小 tile；prefill 的一长串 token 行数远超 16，走大 tile。

> 注意：这里的「prefill/decode」是按 **query 体量**自适应划分的，而不是按请求是否处于解码阶段。一次很短的 prefill（如投机解码的 17 个 token、或 chunked-prefill 的小块）也可能落进 decode 阶段；这正是「holistic」的灵活之处——它不要求调用方声明每个请求是 prefill 还是 decode，而是凭 `qo_indptr` 自动归类。

#### 4.2.2 核心流程

`TwoStageHolisticPlan` 在 host 上做四件事：

```text
输入: qo_indptr[0..batch], kv_indptr[0..batch], kv_len_arr[0..batch], 头参, causal

1. 分桶：对每个请求 i
     qo_len   = qo_indptr[i+1] - qo_indptr[i]
     packed   = qo_len * gqa_group_size
     if packed > 16:  丢进 桶0 (prefill, tile=128)
     else:            丢进 桶1 (decode,  tile=16)

2. 对每个桶 task ∈ {0,1}：
   - 把桶内每个请求切成 (qo_tile × kv_tile) 的细粒度「工作项」
     * qo 方向按 cluster_tile_q = CTA_TILE_Q[task] 切
     * kv 方向按 kv_len_limit（自适应的 split-k chunk）切，长 kv 被拆成多段 → split-k
   - 用最小堆（MinHeap）做「贪心负载均衡」：把工作项逐个派给当前累计代价最小的 cluster（SM）
   - 产出该桶的 CSR 索引数组：q_indptr/kv_indptr/q_len/kv_len/q_start/kv_start/kv_end/...
     以及 work_indptr（每个 cluster 分到的工作项区间）

3. 为 split-k 记录合并信息：merge_indptr / merge_o_indices
   （描述哪些 partial 输出需要被合并、合并到最终输出的哪个位置）

4. 把所有 CSR 数组写进 page-locked CPU buffer，再 cudaMemcpyAsync 到 GPU int workspace；
   返回 HolisticPlanInfo（一串 int64 偏移量，定位这些数组在 workspace 里的位置）
```

负载均衡用最小堆而非轮转，是因为 prefill 工作项的代价远大于 decode 工作项（注释里也强调 "chunked-prefill workloads are much more expensive than decode"），需要按代价加权才能让各 SM 大致同时空闲。代价函数 `cost_function(cluster_tile_q, actual_len)` 与桶的 tile 大小、kv 段长相关。

split-k 的 kv 段长 `kv_len_limit` 也是自适应算出来的（先统计全部请求的总 kv 长度，再按 cluster 数与 KV 头数摊分，并对 prefill 桶额外除以一个系数以补偿其更高的单工作项代价）。

#### 4.2.3 源码精读

`HolisticPlanInfo` 是 plan 的全部产出——它不存实际数据，只存「数据在 workspace 里的偏移」。注意它对**两个 task（阶段）各存一组** `tasks[0]`/`tasks[1]` 偏移：

[include/flashinfer/attention/scheduler.cuh:1139-1163](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L1139-L1163) —— `HolisticPlanInfo<2>` 持有 `num_blks_x/num_blks_y`（grid 维度）、每个阶段的 10 个 CSR 数组偏移（`q_indptr_offset`/`kv_indptr_offset`/`partial_indptr_offset`/`q_len_offset`/`kv_len_offset`/`q_start_offset`/`kv_start_offset`/`kv_end_offset`/`kv_head_idx_offset`/`work_indptr_offset`），以及 8 个跨阶段共享的偏移（split-k 合并相关）。它通过 `ToVector()` 压平成 int64 列表回传给 Python，`run` 时再 `FromVector()` 还原——这正是 Python 侧 `_plan_info` 的真身。

**分派的核心几行**（本讲最关键的代码点）：

[include/flashinfer/attention/scheduler.cuh:1228-1265](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L1228-L1265) —— 先定义 `CTA_TILE_Q_SIZES[2] = {128, 16}`；再遍历每个请求，算 `qo_len` 与 `packed_qo_len = qo_len * gqa_group_size`，按 `packed_qo_len > CTA_TILE_Q_SIZES[1]`（即 >16）把请求三元组 `{请求下标, qo_len, kv_len}` 推入 `idx_qo_kv_len_vec[0]`（prefill 桶）或 `[1]`（decode 桶）。这就是「按 qo_indptr 区间分派」的全部秘密。

分桶之后，对每个桶做工作项切分 + 最小堆负载均衡：

[include/flashinfer/attention/scheduler.cuh:1312-1384](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L1312-L1384) —— 对桶内每个请求，按 `cluster_tile_q` 切 qo tile；对每个 qo tile，按 `kv_len_limit` 决定是否 split-k（`split_kv = remaining_len > kv_len_limit`），逐段派给最小堆弹出的当前最闲 cluster，并记录 `q_start/kv_start/kv_end/kv_head_idx` 等坐标；对 split-k 的工作项额外登记 `merge_indptr/merge_o_indices` 以备合并。

每个桶的 CSR 数组最终写进 page-locked buffer 并拷到 GPU：

[include/flashinfer/attention/scheduler.cuh:1407-1445](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L1407-L1445) —— 用 `AlignedAllocator` 在 int workspace 里为每个阶段的 10 个 CSR 数组分配 16 字节对齐的偏移，再 `CopyToPageLockedBuffer` 把 host vector 写过去。两个阶段（task=0/1）各分配一份，正好对应 `params[0]`/`params[1]`。

#### 4.2.4 代码实践

> 目标：亲手追踪「一个混合批次是如何被 `qo_indptr` 分到两个桶里的」——这是纯源码阅读型实践，不需要 GPU。

操作步骤：

1. 构造一批假数据：`qo_lens = [1, 1, 200, 1, 50]`，`num_qo_heads=32, num_kv_heads=8`（故 `gqa_group_size=4`）。
2. 对每个请求手算 `packed_qo_len = qo_len * 4`：`[4, 4, 800, 4, 200]`。
3. 按 `packed > 16` 判定每个请求落桶 0 还是桶 1。
4. 打开 [`include/flashinfer/attention/scheduler.cuh:1256-1264`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L1256-L1264) 对照你的判定。

需要观察的现象与预期结果：

- 请求 0、1、3（`packed=4`）→ 桶 1（decode）；请求 2（800）、请求 4（200）→ 桶 0（prefill）。
- 若把 `num_kv_heads` 改成 32（`gqa_group_size=1`），则请求 4 的 `packed=50` 仍 >16 进 prefill，但一个 `qo_len=1` 的 decode 请求 `packed=1` 进 decode——验证了「decode token 永远进小 tile」。
- 进一步思考：若某请求 `qo_len=4, gqa_group_size=4`（`packed=16`），它**不大于** 16，会进 decode 桶。可见阈值是开区间 `>16`。

#### 4.2.5 小练习与答案

**练习 1**：为什么阈值用 `packed_qo_len = qo_len * gqa_group_size` 而不是单纯的 `qo_len`？

**参考答案**：因为 kernel 内部把 GQA 的多个 Q 头「打包」成连续行一起算（`packed_qo_start` 等坐标都以 packed 行为单位）。一个 decode 请求虽然 `qo_len=1`，但在 GQA 下实际要处理 `gqa_group_size` 行；判定它能否塞进 `CTA_TILE_Q=16` 的小 tile，必须按打包后的行数算才准确。

**练习 2**：阶段 0 与阶段 1 的 `CTA_TILE_Q` 分别是多少？为什么 decode 阶段用更小的 tile？

**参考答案**：阶段 0 是 128，阶段 1 是 16（见 `CTA_TILE_Q_SIZES[2]={128,16}`）。decode 请求 query 行少但 KV 长、是 memory-bound，用小 tile 可以让单个 CTA 覆盖一个完整请求的全部 KV、减少跨 CTA 的 split-k 合并开销；prefill 请求 query 行多、是 compute-bound，用大 tile 提高每个 CTA 的算术强度与 tensor core 利用率。

**练习 3**：`HolisticPlanInfo` 为什么以「一串 int64 偏移」而不是「实际索引数组」回传给 Python？

**参考答案**：实际数据（几万项的 CSR 数组）已经写进 GPU workspace 了；Python 侧只需要在 `run` 时把这些偏移再传回 C++，C++ 用 `GetPtrFromBaseOffset` 从 workspace 基址 + 偏移还原出指针即可。只回传偏移既省去 Python↔C++ 大数组拷贝，也让 `plan_info` 成为可序列化的轻量句柄。

---

### 4.3 混合批处理：单 persistent kernel + cooperative groups + 合并归约

#### 4.3.1 概念说明

分派解决了「谁走 prefill、谁走 decode」的问题，但还有一个工程问题：**能不能只用一次 kernel launch 把两个阶段都跑完**？`BatchAttention` 的答案是「能」，靠三件事：

1. **Persistent kernel**：启动约等于 `2 × num_sm` 个 CTA（head_dim<256 时每个 SM 驻留 2 个 CTA），它们不随工作项退出，而是循环从全局工作列表领活儿。grid 维度 `num_blks_x × num_blks_y` 中，`blockIdx.y` 标识 cluster（SM），每个 cluster 通过 `work_indptr[blockIdx.y]` 取到自己分到的工作项区间。
2. **Cooperative groups + `grid.sync()`**：同一个 kernel 里，所有 CTA 先集体跑 prefill 阶段（消费 `params[0]` 的工作列表），`grid.sync()` 屏障，再集体跑 decode 阶段（消费 `params[1]` 的工作列表），再 `grid.sync()`，最后跑合并归约。三段顺序执行、共享同一批常驻 CTA。
3. **Split-k 合并归约**：prefill 阶段里 kv 被切多段的请求会产生 partial 输出 `partial_o/partial_lse`，归约阶段用**在线 softmax / LSE 合并**（与 u4-l2 cascade 同源）把它们精确合成最终 `out/lse`。

为什么值得这么做？传统做法要么「prefill 和 decode 各 launch 一次」（多一次启动 + decode 期间 prefill 的 SM 空转），要么把所有请求都按 prefill 跑（decode 的 memory-bound 特性没被优化）。persistent + cooperative 让两类负载在**同一群常驻 CTA** 上交错占用，SM 利用率更平稳，且只付一次启动开销。

#### 4.3.2 核心流程

一次 `run` 在 GPU 上的执行可以画成：

```text
host: BatchPagedAttentionRun
   组装 params[0](prefill)/params[1](decode)
   cudaLaunchCooperativeKernel(PersistentKernelTemplate,
                               grid=(num_blks_x, num_blks_y)≈(1, 2·num_sm))

device: 每个常驻 CTA (blockIdx.y = cluster_id) 执行 PersistentKernelTemplate:
   ┌─ 阶段 A: BlockBatchPagedAttentionPersistent<KTraits1>(params_0)   # CTA_TILE_Q=128
   │     for work_idx in work_indptr_0[blockIdx.y .. blockIdx.y+1]:
   │         取该工作项坐标 → 跑一段 prefill attention（可能写 partial_o/partial_lse）
   ├─ grid.sync()                                                       # 全 grid 屏障
   ├─ 阶段 B: BlockBatchPagedAttentionPersistent<KTraits2>(params_1)   # CTA_TILE_Q=16
   │     for work_idx in work_indptr_1[blockIdx.y .. blockIdx.y+1]:
   │         取该工作项坐标 → 跑一段 decode attention
   ├─ grid.sync()
   └─ 阶段 C: BlockBatchReductionPersistent                             # 合并归约
         用 LSE 把 partial_o/partial_lse 合成 final_o/final_lse
```

注意阶段 A、B 用的是**两套不同的 `KernelTraits`**（`KTraits1`/`KTraits2`，对应 tile 128/16），但它们被编译进**同一个** kernel 函数 `PersistentKernelTemplate`，靠 `grid.sync` 串起来——这正是「融合成单次 launch」的实现手段。

#### 4.3.3 源码精读

C++ launcher 先调 plan、再用 `params[0]/params[1]` 启动 kernel。plan 入口：

[csrc/batch_attention.cu:38-65](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_attention.cu#L38-L65) —— `BatchPagedAttentionPlan` 把 workspace 与 CSR 数组交给 `TwoStageHolisticPlan`（4.2 的主角），返回 `HolisticPlanInfo<2>::ToVector()`。

run 入口里组装两份 params 并启动（注意模板实参 `<128, 16, ...>` 与阶段 tile 的对应）：

[csrc/batch_attention.cu:108-191](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_attention.cu#L108-L191) —— `DISPATCH_context` 宏展开后，循环 `for (int i=0;i<2;i++)` 填充 `params[i]`：从 workspace + `plan_info.tasks[i].*_offset` 还原出该阶段的 q_indptr/kv_indptr/q_len/kv_start/kv_end 等 CSR 指针；最后调用 `BatchPagedAttentionPersistent<128, 16, HEAD_DIM_QK, HEAD_DIM_VO, MASK_MODE, AttentionVariant>(params[0], params[1], num_blks_x, num_blks_y, stream)`。`params[0]`↔prefill(tile128)、`params[1]`↔decode(tile16)。

每个常驻 CTA 的工作循环（persistent 的精髓）：

[include/flashinfer/attention/persistent.cuh:270-293](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/persistent.cuh#L270-L293) —— `for (work_idx = work_indptr[blockIdx.y]; work_idx < work_indptr[blockIdx.y+1]; ++work_idx)`：本 CTA 只处理属于自己 cluster（`blockIdx.y`）的工作项区间；每项通过 `get_block_coord` 取出 `q_indptr/kv_indptr/q_len/kv_len/q_start/kv_start/kv_end/...` 坐标，再据此做一段 attention。这正是「persistent CTA 逐项消费工作列表」。

`get_block_coord` 把 workspace 偏移翻译成具体坐标：

[include/flashinfer/attention/persistent.cuh:31-38](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/persistent.cuh#L31-L38) —— 由 `params` 的各 CSR 数组与 `work_idx` 取出本工作项的全部坐标（含 `len_kv_chunk` 用于 split-k）。

「三段顺序执行」的 kernel 模板：

[include/flashinfer/attention/persistent_template.cuh:56-97](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/persistent_template.cuh#L56-L97) —— `PersistentKernelTemplate` 接收两个 BlockRunner 类型与一个 ReductionRunner；函数体内依次 `BlockPersistentRunner1::Run(params_1,...)`（prefill）、`BlockPersistentRunner2::Run(params_2,...)`（decode）、`grid.sync()`、`BlockReductionRunner::Run(...)`（合并归约）。`grid` 来自 `cooperative_groups::this_grid()`，`grid.sync()` 是全 grid 屏障。

host 端 cooperative launch 与 grid 维度计算（`num_blks_x=cluster_size=1`，`num_blks_y=num_clusters`）：

[include/flashinfer/attention/persistent.cuh:681-692](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/persistent.cuh#L681-L692) —— 选定最大 shared memory、设 `dim3 nblks(num_blks_x, num_blks_y)`、用 `cudaLaunchCooperativeKernel` 启动，参数包 `args={&params_1, &params_2}`。cooperative launch 要求 grid 全部 CTA 能同时驻留，所以 `num_blks_y` 严格按 SM 数 × 每驻留 CTA 数算（plan 里 `num_sm *= 2` for head_dim<256）。

grid 维度在 plan 里确定（解释了「为什么 persistent grid 大小要由 plan 决定」）：

[include/flashinfer/attention/scheduler.cuh:1230-1270](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L1230-L1270) —— 读 `cudaDevAttrMultiProcessorCount` 得 `num_sm`，按 head_dim 是否 ≥256 决定每 SM 驻留 1 还是 2 个 CTA；`cluster_size=1`、`num_clusters=num_sm`（已含倍数），写入 `plan_info.num_blks_x/num_blks_y`。

split-k 合并的 partial 输出空间分配：

[include/flashinfer/attention/scheduler.cuh:1479-1484](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L1479-L1484) —— 在 float workspace 分配 `partial_o`（`max_num_kv_splits × head_dim × num_kv_heads`）与 `partial_lse`，供合并归约阶段消费。

#### 4.3.4 代码实践

> 目标：用一次 `BatchAttention` 跑一个真实混合批次，并从 `plan_info` 与 grid 维度两个角度确认「两阶段被缝进了一次 launch」。

操作步骤：

1. 用 4.1.4 的骨架，把批次设成「明显混合」，如测试里 [`tests/attention/test_batch_attention.py:64-74`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/attention/test_batch_attention.py#L64-L74) 的 `[(2048,1)]*77 + [(4099,129)]*2`（77 个 decode + 2 个 prefill）这种「decode 为主、夹杂 prefill」的真实持续批处理负载。
2. 在 `plan` 之后打印 `wrapper._plan_info`（一个 int64 列表），按 [`HolisticPlanInfo::FromVector`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L1189-L1216) 的字段顺序，解出 `num_blks_x`、`num_blks_y` 与两个阶段各自的偏移量。
3. 用 `torch.cuda.get_device_properties(0).multi_processor_count` 得到本机 SM 数，验证 `num_blks_y ≈ 2 × num_sm`（head_dim=128 < 256 时）。
4. （可选）设置 `use_profiler=True` 走 profiler 变体，配合 `profiler_buffer` 观察 kernel 内 `kRunner1`(prefill)/`kRunner2`(decode)/`kReduction` 三类事件占比。

需要观察的现象：

- `num_blks_y` 约为 SM 数的两倍，符合 persistent kernel 的「每 SM 驻留 2 CTA」。
- 无论批次里 prefill/decode 比例如何，都只发生**一次** kernel launch（可用 nsys/nccl-profile 或简单地在 run 前后插 CUDA event 计时确认）。
- 改变 prefill/decode 比例（如全 decode、或全 prefill），输出仍与 `BatchPrefillWithPagedKVCacheWrapper(fa2)` 一致。

预期结果（待本地验证）：单次 launch 完成；`plan_info` 解码出的 grid 维度与 SM 数成比例；数值与 fa2 参考吻合。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `BatchAttention` 用 `cudaLaunchCooperativeKernel` 而不是普通 `cudaLaunchKernel`？

**参考答案**：因为它需要在 kernel 内部做 `grid.sync()` 全局屏障（prefill 阶段 → decode 阶段 → 合并归约之间的同步）。普通 launch 不保证 grid 内所有 CTA 同时驻留，无法做 grid 级屏障；cooperative launch 保证整个 grid 同时在 SM 上运行，`grid.sync()` 才安全。

**练习 2**：阶段 C（合并归约）在数学上依赖什么？它和 u4-l2 的 cascade 合并有什么关系？

**参考答案**：依赖 logsumexp（LSE）的在线 softmax 合并——对一段 attention 的部分输出 `(partial_o, partial_lse)`，可按 LSE 加权精确合并成整段结果，等价于没切分。这与 u4-l2 cascade 的 `merge_state` 同源（都追溯到 `state.cuh` 的 `(o,m,d)` 在线 softmax 三元组），区别在于这里是把同一个请求因 split-k 产生的多段 partial 合并，cascade 是把「共享前缀段」与「独立段」合并。

**练习 3**：如果一个批次**全是 decode**（所有 `qo_len=1`），`BatchAttention` 还会启动 prefill 阶段吗？会有什么开销？

**参考答案**：会启动，但 prefill 阶段的工作列表为空（`idx_qo_kv_len_vec[0]` 没有元素，对应工作项数为 0），每个 CTA 的 `for work_idx` 循环零次即结束，仅多付出一次 `grid.sync()` 与少量模板实例化的寄存器/shared memory 成本。即「阶段存在但空转」，仍只一次 launch。

---

## 5. 综合实践

把三个最小模块串起来：**构造一个真实风格的混合批次，跑通 `BatchAttention`，并用源码追踪解释「为什么这一次调用就能同时服务 prefill 和 decode」。**

建议步骤：

1. **准备批次**：设计 4 个请求，`qo_lens=[512, 1, 1, 64]`、`kv_lens=[4096, 8192, 1024, 2048]`，`num_qo_heads=32, num_kv_heads=8`（`gqa_group_size=4`），`head_dim=128`，`page_size=16`，`causal=True`。
2. **手算分派**（对应 4.2）：算出每个请求的 `packed_qo_len` 与所属阶段，预期 `[512→prefill, 1→decode(4), 1→decode(4), 64→prefill(256)]`。
3. **运行并验证**：参照 [`tests/attention/test_batch_attention.py:159-204`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/attention/test_batch_attention.py#L159-L204) 同时跑 `BatchPrefillWithPagedKVCacheWrapper(backend="fa2")` 与 `BatchAttention`，`torch.testing.assert_close(out_old, out_new, rtol=1e-2, atol=1e-2)`。
4. **追踪 plan**：打印 `_plan_info`，按 [`HolisticPlanInfo::FromVector`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L1189-L1216) 字段顺序，画出「两阶段各自的 CSR 偏移 + grid 维度」表格。
5. **追踪 run**：在 [`csrc/batch_attention.cu:186-191`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_attention.cu#L186-L191) 处确认 `params[0]/params[1]` 与 `<128,16,...>` 模板实参，在 [`persistent_template.cuh:79-86`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/persistent_template.cuh#L79-L86) 确认三段顺序执行。
6. **写一段总结**：用自己的话说清「一次 `wrapper.run` 如何在 GPU 上展开为 prefill 阶段 → grid.sync → decode 阶段 → grid.sync → 合并归约」。

验收标准：输出与 fa2 参考一致；`_plan_info` 解码出的两阶段工作项数与手算分派吻合；能用一张图讲清整条调用链 `Python wrapper → csrc plan/run → scheduler 分派 → persistent_template 三段 → reduction 合并`。

> 待本地验证：以上涉及的具体耗时、JIT 编译是否命中缓存、profiler 事件占比等，需在你自己的 GPU 上运行确认。

---

## 6. 本讲小结

- `BatchAttention` 是「holistic persistent kernel」式的统一 wrapper：把 paged-prefill 与 paged-decode 融合为**单次** kernel launch，实例自持 workspace，对外仍是 plan/run 两段式。
- **分派发生在 plan 阶段的 C++ 侧**：[`TwoStageHolisticPlan`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L1228-L1265) 依据每个请求的 `qo_indptr` 区间算出 `packed_qo_len`，以 `>16` 为阈值把请求丢进 prefill 桶（`CTA_TILE_Q=128`）或 decode 桶（`CTA_TILE_Q=16`）。Python wrapper 不做任何 if-else。
- 分派后用**最小堆按代价做负载均衡**，把工作项（qo tile × kv tile）派给最闲的 cluster（SM），并对长 KV 做 split-k。
- 单次 launch 靠 **cooperative groups + `grid.sync()`** 串起三段：prefill 阶段 → 屏障 → decode 阶段 → 屏障 → 合并归约；grid 维度 ≈ `2 × num_sm` 个常驻 CTA。
- split-k 的 partial 输出用 **LSE 在线 softmax 合并**精确归约，数学与 u4-l2 cascade 同源。
- 与 POD（u4-l4）的区别：POD 用 PTX `%smid` 动态同驻 prefill/decode CTA、decode 复用 batched-prefill 模板；BatchAttention 用 plan 时刻分派 + cooperative `grid.sync` 顺序执行两套不同 tile 的 KernelTraits——机制不同，BatchAttention 不依赖 `%smid`、可移植性更广。

---

## 7. 下一步学习建议

- **对照 POD（u4-l4）**：重读 `flashinfer/pod.py` 与 `include/flashinfer/attention/pod.cuh`，把「动态同驻」与「plan 分派 + grid.sync 顺序执行」两种混合批处理思路放在一起比较，理解各自的硬件前提与取舍。
- **深入合并归约数学**：阅读 `include/flashinfer/state.cuh` 的 `state_t` 与 `StateReductionKernelTraits`，把本讲阶段 C 与 u4-l2 cascade、u4-l1 MLA 的 split-k/LSE 合并贯通成一套「在线 softmax」知识。
- **persistent kernel 模板复用**：本讲的 `PersistentKernelTemplate` 同样服务 MLA（见 `include/flashinfer/attention/mla.cuh` 的 cooperative launch）。可比对 `MLAPlanInfo` 与 `HolisticPlanInfo`，理解「一个 persistent 模板 + 多种 plan」的复用模式。
- **回到 JIT 与新增算子**：若你想在 `BatchAttention` 上加自定义变体（如新的 logits 变换），按 u9-l3 阅读 [`csrc/batch_attention_customize_config.jinja`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_attention_customize_config.jinja) 与 [`gen_customize_batch_attention_module`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py#L1899-L1993)，参照 `BatchAttentionWithAttentionSinkWrapper` 的写法注入额外参数。
- **性能验证**：结合 u10-l3 的 `bench_gpu_time(enable_cupti=True)`，在同一个混合批次上对比 `BatchAttention`（单 launch）与「prefill+decode 两次 launch」的 kernel 时间，直观感受融合的收益。
