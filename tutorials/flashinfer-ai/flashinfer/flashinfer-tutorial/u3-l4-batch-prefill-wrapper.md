# BatchPrefillWithPagedKVCacheWrapper 的 plan/run

## 1. 本讲目标

学完本讲后，你应当能够：

- 区分 `BatchPrefillWithPagedKVCacheWrapper` 与 `BatchPrefillWithRaggedKVCacheWrapper` 两个 prefill wrapper，知道各自适用的 KV 存储方式。
- 说清楚 prefill 的 **plan 统一、run 分叉** 设计：无论 paged 还是 ragged，plan 走的是同一套调度逻辑（同一个 `PrefillPlanInfo`），而 run 才分出 `paged_run` / `ragged_run` 两条路。
- 用 `qo_indptr` / `kv_indptr` / `paged_kv_indptr` / `seq_lens` 这些前缀和数组描述一个「请求长度各不相同」的变长批次。
- 对比 prefill wrapper 与 u3-l3 的 decode wrapper 在 `plan` 参数和调度上的关键差异（为什么 prefill 多了 `qo_indptr`、`causal`、`custom_mask`）。

## 2. 前置知识

本讲承接 u3-l1（注意力三阶段与 paged KV cache）、u3-l2（KV 布局与页表三件套），并对照 u3-l3 的 decode wrapper。这里用三句话回顾需要用到的概念：

- **prefill / append 阶段**：每个请求有一段（可能很长）的 query 序列要算注意力，属于 compute-bound；这与 decode「每请求只有 1 个 query、memory-bound」截然不同。
- **变长批次的描述方式**：长度不等的请求不能塞进一个规整的 `[batch, max_len, ...]` 张量（否则会大量浪费显存），所以 FlashInfer 把所有请求的 query / KV **拼接（flatten）成一维**，再用一个前缀和数组 `indptr` 标出每个请求的起止位置。
- **plan / run 两段式 API**：plan 处理只依赖「批次结构」的准备工作（划分 tile、决定是否切分 KV、写辅助索引），其结果可被同一前向里多层 Transformer 复用；run 只携带每层的实际数据 `q/k/v` 去算。plan 含动态调度决策、不能进 CUDA Graph，run 可以。

> 说明：u3-l3（decode wrapper）的讲义文件暂时缺失，因此本讲在 4.4 节会自包含地给出 decode `plan` 的签名用于对比，不依赖你已读过 u3-l3。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `flashinfer/prefill.py` | 用户直接调用的 Python wrapper，含 `BatchPrefillWithPagedKVCacheWrapper` 与 `BatchPrefillWithRaggedKVCacheWrapper` 两个类，以及模块加载函数 `get_batch_prefill_module`。 |
| `csrc/batch_prefill.cu` | C++ launcher：`BatchPrefillWithKVCachePlan`（plan）、`BatchPrefillWithRaggedKVCacheRun`（ragged run）、`BatchPrefillWithPagedKVCacheRun`（paged run）。 |
| `csrc/batch_prefill_jit_binding.cu` | TVM-FFI 绑定层：用 `TVM_FFI_DLL_EXPORT_TYPED_FUNC` 把上面 4 个 C++ 函数导出为 `plan` / `workspace_size` / `ragged_run` / `paged_run` 符号供 Python 调用。 |
| `include/flashinfer/attention/scheduler.cuh` | plan 的真正实现：`PrefillPlan`、`PrefillPlanInfo` 结构、`PrefillSplitQOKVIndptr` 切分逻辑。 |
| `include/flashinfer/attention/prefill.cuh` | header-only 的 prefill kernel 模板（`BatchPrefillWithPagedKVCacheDispatched` 等），本讲只引用其分派入口，kernel 内部留到更后面的讲义。 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：4.1 讲两个 wrapper 的整体设计与「plan 统一 / run 分叉」；4.2 讲 ragged 与 paged 在 KV 布局与索引上的差别；4.3 钻进 plan 内部讲变长切分与 `PrefillPlanInfo`；4.4 把 prefill 和 decode 放在一起对比。

### 4.1 prefill wrapper：双兄弟与「plan 统一、run 分叉」

#### 4.1.1 概念说明

FlashInfer 为 batch prefill 提供了**两个并列的 wrapper 类**，区别只在于 KV 怎么存：

- `BatchPrefillWithPagedKVCacheWrapper`：KV 存在分页缓存里（物理页池 + 页表），适合「请求已经跑过若干步、KV 已经被 `append_paged_kv_cache` 写进页池」的 append 场景，或服务里复用同一份 paged KV。
- `BatchPrefillWithRaggedKVCacheWrapper`：KV 以**拼接的一维张量**（ragged tensor）直接给出，适合「整段 prompt 第一次 prefill、KV 还没落盘到页池」的场景。ragged 比 paged 简单——不需要页表，只要一个 `kv_indptr` 标出每个请求的 KV 段。

这两个 wrapper 共享同一份编译出来的 JIT 模块（同一个 kernel），关键设计是：**plan 是统一的，run 才分叉**。也就是说，决定「每个请求切成多少 tile、要不要切分 KV、workspace 怎么分」这件事，跟「KV 是分页的还是拼接的」无关——它只关心 query/KV 的长度结构。因此两个 wrapper 的 `plan` 最终都调用同一个 C++ 符号 `plan`（即 `BatchPrefillWithKVCachePlan`），产出同一份 `PrefillPlanInfo`。等到 `run` 的时候，才根据 KV 形态分别走 `paged_run` 或 `ragged_run`。

#### 4.1.2 核心流程

```text
            BatchPrefillWithPagedKVCacheWrapper            BatchPrefillWithRaggedKVCacheWrapper
                        │ plan(qo_indptr, paged_kv_*)                  │ plan(qo_indptr, kv_indptr, ...)
                        ▼                                            ▼
            组装 args（含真实 page_size）                 组装 args（page_size 写死为 1，见 4.2）
                        └──────────────┬──────────────────────────────┘
                                       ▼  同一个 module.plan
                          module = get_batch_prefill_module(backend, dtype, head_dim, ...)
                                       ▼  返回 SimpleNamespace(plan, workspace_size, ragged_run, paged_run)
                          cached_module.plan(*args)  →  self._plan_info (15 个 int64)
                                       ▼
                       run 阶段二选一分叉：
            paged:  cached_module.paged_run(…)   ragged: cached_module.ragged_run(…)
```

#### 4.1.3 源码精读

两个 wrapper 类的定义位置（本讲所有 Python 引用都在 `flashinfer/prefill.py`）：

- `BatchPrefillWithPagedKVCacheWrapper` 类声明（含完整使用示例）：[flashinfer/prefill.py:1475-L1475](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L1475-L1475) — 这个类把 paged KV 的 plan/run 生命周期封装起来。
- `BatchPrefillWithRaggedKVCacheWrapper` 类声明（含完整使用示例）：[flashinfer/prefill.py:2914-L2914](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L2914-L2914) — ragged 版，用法对称但 plan 少了页表参数。

两个 wrapper 共用的 JIT 模块加载函数 `get_batch_prefill_module` 最终返回一个带四个属性的对象，这段代码注释点明了「plan 不属于模型逻辑、不进 CUDA Graph / torch.compile」这条核心约定：

```python
# Note that plan is not part of model logic. It should not be included in
# Cuda Graph or torch.compile. So, we don't provide a torch library for plan.
return SimpleNamespace(
    plan=plan_func,
    workspace_size=workspace_size_func,
    ragged_run=ragged_run,
    paged_run=paged_run,
)
```

见 [flashinfer/prefill.py:865-L873](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L865-L873)。注意这里把 `ragged_run` 和 `paged_run` 放在同一个模块里——这正是「run 分叉但模块统一」的直接体现。C++ 侧也对应导出了 4 个符号（`plan`/`workspace_size`/`ragged_run`/`paged_run`），见 [csrc/batch_prefill_jit_binding.cu:55-L58](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_prefill_jit_binding.cu#L55-L58)。

> 为什么 plan 能统一？因为 plan 关心的是「如何把工作切成 tile / 切分 KV」，这只需要知道每个请求的 qo 长度和 kv 长度，跟 KV 是分页的还是拼接的无关。分页带来的复杂性（页号间接寻址）只在 run 时才需要。

#### 4.1.4 代码实践

**目标**：用眼睛走一遍两个 wrapper 的 plan，确认它们最后都汇入同一个 `module.plan`。

**步骤**：

1. 打开 `flashinfer/prefill.py`，分别跳到 paged 的 `plan`（约 2049 行）和 ragged 的 `plan`（约 3164 行）。
2. 在两个方法里搜索 `self._cached_module.plan(`，确认它们都把一个 `args` 列表喂给同一个 `plan` 函数。
3. 对比两个 `args` 列表，找出唯一一处关键差异（提示：`page_size` 那一项）。

**预期结果**：你会看到 paged plan 在 [flashinfer/prefill.py:2446-L2448](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L2446-L2448) 调用 `self._cached_module.plan(*args)`，ragged plan 在 [flashinfer/prefill.py:3538-L3540](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L3538-L3540) 调用同一个 `self._cached_module.plan(*args)`。两者结构相同，paged 传真实 `page_size`，ragged 传 `1`。

> 「待本地验证」：若你已在本地装好 flashinfer，可以在 plan 前后各打印一次 `self._backend` 与 `self._plan_info`，观察 plan 返回的 15 个整数。

#### 4.1.5 小练习与答案

**练习 1**：为什么 FlashInfer 把 paged 和 ragged 做成两个 wrapper 类，而不是一个类加一个 `kv_layout` 参数？
**答案**：因为两者 `plan` 的**入参形状不一样**——paged 还需要 `paged_kv_indices` / `paged_kv_last_page_len`（页表三件套的后两项），而 ragged 只要一个 `kv_indptr`。做成两个类可以让类型签名清晰、减少条件分支，同时复用底层同一份 JIT 模块和同一个 `plan` 符号。

**练习 2**：注释里说「we don't provide a torch library for plan」，这意味着什么？
**答案**：plan 不会被注册成 `torch.library` 自定义算子，因此它无法被 `torch.compile` 融合、也无法被捕获进 CUDA Graph。这是有意为之——plan 含依赖批次结构的动态调度，本就该在图外执行。

---

### 4.2 ragged vs paged：KV 布局与索引差异

#### 4.2.1 概念说明

batch prefill 要处理「请求长度各不相同」的批次。FlashInfer 的做法是把所有请求的 query 拼成一维 `q`、KV 也拼成一维，再用前缀和数组描述边界：

- **`qo_indptr`**：query/output 的前缀和，形状 `[batch_size + 1]`。请求 `i` 的 query 是 `q[qo_indptr[i] : qo_indptr[i+1]]`，总 query 行数 `= qo_indptr[-1] = q.shape[0]`。两个 wrapper 都需要它。

KV 侧则因存储方式不同而分叉：

- **ragged**：KV 直接拼接成一维，用 **`kv_indptr`**（形状 `[batch_size + 1]`）标出每个请求的 KV 段。请求 `i` 的 kv 长度就是 `kv_indptr[i+1] - kv_indptr[i]`。
- **paged**：KV 散落在物理页池里，需要 u3-l2 讲过的**页表三件套**：`paged_kv_indptr`（每请求页段起止）、`paged_kv_indices`（物理页号列表）、`paged_kv_last_page_len`（每请求最后一页的有效长度）。请求 `i` 的 kv 长度要靠这三者反推。

#### 4.2.2 核心流程

两个 wrapper 在 plan 里把「批次结构」翻译成同一套 `(qo_indptr_h, kv_indptr_h, kv_len_arr, ...)` 喂给底层：

```text
ragged:  kv_len_arr[i] = kv_indptr[i+1] - kv_indptr[i]              （直接相减）
paged :  kv_len_arr[i] = get_seq_lens(paged_kv_indptr, last_page_len, page_size)[i]
                                                              （页数×page_size 再补最后一页）
         两者的 kv_indptr_h：
           ragged → 直接用 kv_indptr
           paged  → 用 paged_kv_indptr（页的前缀和）
```

关键技巧：**ragged 把 `page_size` 写死为 1**。这样底层 plan 只需要一种逻辑——「按 page_size 把 KV 切成 chunk」，而 ragged 下每个 token 就是一个大小为 1 的「页」，于是同一套切分代码同时服务两种存储。

#### 4.2.3 源码精读

ragged 版直接相减得到 kv 长度，并把 page_size 硬编码为 1：

```python
kv_len_arr = kv_indptr_host[1:] - kv_indptr_host[:-1]
...
args = [
    self._float_workspace_buffer,
    self._int_workspace_buffer,
    self._pin_memory_int_workspace_buffer,
    qo_indptr_host,
    kv_indptr_host,
    kv_len_arr,
    self._max_total_num_rows or total_num_rows,
    batch_size,
    num_qo_heads,
    num_kv_heads,
    1,  # page_size          ← ragged 在这里写死为 1
    self.is_cuda_graph_enabled,
    head_dim_qk,
    head_dim_vo,
    causal,
    window_left,
]
```

见 [flashinfer/prefill.py:3409-L3409](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L3409-L3409) 与 [flashinfer/prefill.py:3516-L3533](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L3516-L3533)。

paged 版则需要从页表三件套反推 kv 长度（用 u3-l2 介绍过的 `get_seq_lens`），并传真实的 `page_size`：

```python
if seq_lens is None:
    kv_lens_arr_host = get_seq_lens(
        paged_kv_indptr_host, paged_kv_last_page_len_host, page_size
    )
else:
    kv_lens_arr_host = seq_lens.cpu().flatten()
```

见 [flashinfer/prefill.py:2264-L2271](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L2264-L2271)。注意 paged 还允许用户直接传 `seq_lens` 跳过反推（省一次计算）。

此外，当使用自定义掩码（`custom_mask`）时，两种存储的 mask 偏移计算也不同——paged 要把「页」换算成 token 数：

- ragged 的 mask 偏移：[flashinfer/prefill.py:2900-L2911](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L2900-L2911)（`_compute_mask_indptr`，直接用 `qo_len * kv_len`）。
- paged 的 mask 偏移：[flashinfer/prefill.py:1452-L1472](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L1452-L1472)（`_compute_page_mask_indptr`，要把 `(页数-1)*page_size + last_page_len` 还原成 kv 长度）。

#### 4.2.4 代码实践

**目标**：手工构造一个 3 请求的变长 ragged 批次，验证 `qo_indptr`/`kv_indptr` 的含义。

**步骤**（示例代码，可在装好 flashinfer 的环境运行；无 GPU 则只能读不能跑，标注「待本地验证」）：

```python
import torch
# 3 个请求，qo 长度分别是 2, 3, 1，总行数 6
qo_indptr = torch.tensor([0, 2, 5, 6], dtype=torch.int32)
# kv 长度分别是 4, 2, 5，总 kv token 数 11
kv_indptr = torch.tensor([0, 4, 6, 11], dtype=torch.int32)
# 手算：请求 1 的 kv 长度
print((kv_indptr[2] - kv_indptr[1]).item())   # 预期 2
```

**预期结果**：打印 `2`。这验证了「`kv_indptr` 的差就是每请求的 kv 长度」。把它换成 paged 时，你要把 `[0,4,6,11]` 换成「每请求占多少页 + 最后一页长度」的页表三件套，再用 `get_seq_lens` 反推回同样的 `[4,2,5]`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 ragged 把 `page_size` 设成 1 就能复用 paged 的 plan 代码？
**答案**：plan 内部按「以 page_size 为单位切 KV chunk」。当 page_size=1 时，「页」退化为「单个 token」，`paged_kv_indptr` 退化为 `kv_indptr`，切分逻辑自然等价于直接按 token 切。这是一种用统一抽象同时表达两种存储的工程技巧。

**练习 2**：paged 版 `plan` 里 `seq_lens` 参数为 `None` 时会怎样？传了又会怎样？
**答案**：为 `None` 时，用 `get_seq_lens(paged_kv_indptr, paged_kv_last_page_len, page_size)` 从页表反推每请求 kv 长度；若用户已预先算好并传入 `seq_lens`，则直接 `.cpu().flatten()` 使用，省去反推。

---

### 4.3 变长索引与 plan 内部调度（PrefillPlanInfo / Split-QO-KV）

#### 4.3.1 概念说明

`plan` 真正在做的事，是给这个变长批次做**工作划分**。prefill 是 compute-bound，单个长请求算不过来时，要把工作拆给更多 SM 同时算。FlashInfer 的拆法是二维的：

- **切 Q**：把每个请求的 query 序列按 `cta_tile_q`（如 128）切成多个 Q tile，每个 Q tile 是一个独立的工作单元。
- **切 KV（split-kv）**：当某请求的 KV 太长，把它的 KV 切成若干 chunk，每个 `(Q tile, KV chunk)` 组合作为一个 CTA 独立算出一份「部分输出 + 部分 logsumexp」，最后用 `merge_indptr` 指挥一个 merge kernel 把这些部分结果合并成最终输出。

这些划分只依赖批次结构，所以放在 plan 里算一次、多层复用。plan 的产物是 `PrefillPlanInfo`——一个 15 个 `int64` 的数组，记录了 `padded_batch_size`、`cta_tile_q`、以及各种辅助数组（`request_indices` / `qo_tile_indices` / `kv_tile_indices` / `merge_indptr` / `o_indptr` 等）在 workspace 里的**偏移量**。run 时就凭这份偏移表去 workspace 里取辅助数组。

#### 4.3.2 核心流程

```text
Python plan()
  ├─ 把 indptr 拷到 CPU（plan 需要 host 端遍历长度）        prefill.py:2253 / 3347
  ├─ determine_attention_backend(...) 选后端（fa2/fa3/...）
  ├─ get_batch_prefill_module(...) 取/编 JIT 模块
  └─ cached_module.plan(args) ──► C++ BatchPrefillWithKVCachePlan (csrc/batch_prefill.cu:47)
                                       └─ PrefillPlan<IdType>(...) (scheduler.cuh:877)
                                             └─ PrefillPlanImpl (scheduler.cuh:744)
                                                   ├─ PrefillSplitQOKVIndptr(...)  切 Q/KV   (scheduler.cuh:775)
                                                   ├─ 在 int workspace 里分配辅助数组并填值
                                                   ├─ 若 split_kv：在 float workspace 里分配 tmp_v/tmp_s
                                                   └─ cudaMemcpyAsync 把 page-locked int buffer → device
                                       返回 Array(plan_info.ToVector())   (15 个 int64)
```

切分时还用到一个小代价函数来衡量一个 tile 的工作量，从而决定要不要切分 KV：

\[
\text{cost}(qo\_len, kv\_len) = 2 \cdot qo\_len + kv\_len
\]

它近似 attention 一行的计算量（QK 和 PV 两步，前者的工作量正比于 qo_len，后者正比于 kv_len 的两倍贡献被简化）。`PrefillBinarySearchKVChunkSize` 用它在「总 CTA 数不超过 SM 容量」与「不要切太碎」之间二分搜索一个合适的 `kv_chunk_size`。

#### 4.3.3 源码精读

`PrefillPlanInfo` 把 15 个字段压成一个一维数组，便于跨 TVM-FFI 边界传回 Python（[include/flashinfer/attention/scheduler.cuh:665-L741](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L665-L741)）。注意其中一半字段是 workspace 偏移（`*_offset`），另一半是标量决策（`cta_tile_q`、`split_kv`、`padded_batch_size`）。

切分逻辑的核心是 `PrefillSplitQOKVIndptr`，它在双重循环里枚举每个 `(Q tile, KV chunk)`，把它们登记成一个新的「虚拟请求」：

```cpp
for (uint32_t q_tile_idx = 0; q_tile_idx < num_tiles_q; ++q_tile_idx) {
  for (uint32_t kv_tile_idx = 0; kv_tile_idx < num_chunks_kv; ++kv_tile_idx) {
    new_batch_size += 1;
    request_indices.push_back(request_idx);
    qo_tile_indices.push_back(q_tile_idx);
    kv_tile_indices.push_back(kv_tile_idx);
  }
}
```

见 [include/flashinfer/attention/scheduler.cuh:636-L643](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L636-L643)。这就是「变长批次被展平成 `padded_batch_size` 个等粒度工作单元」的过程。代价函数定义在 [include/flashinfer/attention/scheduler.cuh:916-L916](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L916-L916)。

只有当决定要 split_kv 时，才会在 **float workspace** 里分配存放部分结果的 `tmp_v` / `tmp_s` 缓冲，并在 int workspace 里分配 `merge_indptr`（见 [include/flashinfer/attention/scheduler.cuh:836-L848](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L836-L848)）——这也是为什么用户传给 wrapper 的 `float_workspace_buffer`（推荐 128MB）必须够大。

最后，C++ 把填好的 page-locked int buffer 异步拷到 device（`cudaMemcpyAsync`，见 [include/flashinfer/attention/scheduler.cuh:866-L868](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L866-L868)），`Array(plan_info.ToVector())` 返回 15 个整数给 Python（见 [csrc/batch_prefill.cu:63-L75](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_prefill.cu#L63-L75)）。这份 `plan_info` 被原样存进 `self._plan_info`，在 `run` 时再原样传回 C++（run 不解析它，只透传）。

#### 4.3.4 代码实践

**目标**：观察 `plan_info` 的形状，确认它就是 15 个 `int64`。

**步骤**：

1. 在 paged 或 ragged wrapper 调用 `plan(...)` 之后，打印 `wrapper._plan_info`。
2. 对照 `PrefillPlanInfo::FromVector`（[scheduler.cuh:719-L740](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L719-L740)），把这 15 个数依次填回字段名。

**预期结果**：你会得到一个长度恰为 15 的整数列表。例如 `vec[3]` 就是 `cta_tile_q`（通常是 64/128/256 之一），`vec[14]` 是 `split_kv`（0 或 1）。把批次里某个请求的 qo 长度调到很大（如 8192），再对比一次，应能看到 `split_kv` 从 0 变 1、`padded_batch_size` 变大。**待本地验证**（需要 GPU 触发实际 plan）。

#### 4.3.5 小练习与答案

**练习 1**：`PrefillPlanInfo` 里为什么存的是「偏移量」而不是直接的指针？
**答案**：因为这些辅助数组都分配在同一块 workspace buffer 里。存偏移量而非绝对指针，既能在 host 端用 page-locked buffer 计算、再整块 `cudaMemcpyAsync` 到 device，也能让 run 阶段用 `GetPtrFromBaseOffset<T>(base, offset)` 在 device buffer 上还原出指针——指针会随分配位置变，而偏移在 plan 期间就固定了。

**练习 2**：`split_kv` 为真时，run 阶段会比不切分多做哪一步？
**答案**：多一次 merge。每个 `(Q tile, KV chunk)` 各算出一份部分输出和 logsumexp（存到 `tmp_v` / `tmp_s`），run 结束后还要按 `merge_indptr` 跑一个 merge kernel，用 logsumexp 做加权求和把这些部分结果合并成最终 attention 输出。

---

### 4.4 prefill 与 decode 的 plan 差异对比

#### 4.4.1 概念说明

理解了 prefill 的 plan，再回头看 u3-l3 的 decode wrapper，就能抓住两者的本质差异。差异的根源是两个阶段的负载性质不同：

- **decode**：每请求只有 **1 个 query**（上一步刚生成的 token），是 memory-bound——瓶颈在读 KV，算力富余。所以 decode 的 plan 不需要切 Q（没有 Q 序列），主要决策是 split-KV（把长 KV 分给多个 SM 并行读）。
- **prefill**：每请求有 **一段 query 序列**，是 compute-bound——既要切 Q，也要切 KV。还要支持 **causal 掩码**（prefill 时未来 token 不能看到过去 query 的 KV）和 **自定义掩码**（custom_mask），这些 decode 都不需要。

#### 4.4.2 核心流程

下表把两个 `plan` 的关键参数并排对比：

| 维度 | decode `plan` | prefill `plan` |
|------|---------------|----------------|
| query 描述 | 无 `qo_indptr`（每请求固定 1 个 query，`q_len_per_req=1`） | 必须传 `qo_indptr`（变长 query 序列） |
| head_dim | 单个 `head_dim` | 拆成 `head_dim_qk` 与 `head_dim_vo`（可不同） |
| 掩码 | 无 `causal`/`custom_mask`（decode 天然只看历史） | 有 `causal`、`custom_mask`、`packed_custom_mask` |
| 切分对象 | 只切 KV（split-KV） | 同时切 Q（`cta_tile_q`）和 KV |
| 典型负载 | memory-bound | compute-bound |
| 后端选项 | fa2/fa3/cudnn/tensor-core/cuda-core 等 | fa2/fa3/cudnn/trtllm-gen/cutlass/cute-dsl |

decode 的 `plan` 入参里没有 `qo_indptr` 这一项，取而代之的是一个固定的「每请求 query 数」`q_len_per_req`（默认 1）——见 [flashinfer/decode.py:1159-L1184](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1159-L1184)。而 prefill 的 `plan` 第一参就是 `qo_indptr`——见 [flashinfer/prefill.py:2048-L2085](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L2048-L2085)。

#### 4.4.3 源码精读

decode wrapper 类与 plan 签名：[flashinfer/decode.py:653-L653](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L653-L653)（类）、[flashinfer/decode.py:1159-L1184](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1159-L1184)（`plan` 签名）。

prefill paged wrapper 的 `run` 还会校验 query 形状与 plan 期记录的 `qo_indptr[-1]` 一致（避免 GPU 同步，用 plan 里缓存的值核对）：

```python
if q.size(0) != self._qo_indptr_last:
    raise ValueError(
        f"q.shape[0] ({q.size(0)}) does not match qo_indptr[-1] ({self._qo_indptr_last}). "
        f"For paged prefill, q must have shape [total_tokens, num_heads, head_dim] "
        f"where total_tokens = qo_indptr[-1]."
    )
```

见 [flashinfer/prefill.py:2648-L2653](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L2648-L2653)。decode 因为每请求只有 1 个 query，没有这种「变长 query 总数」的校验需求。

最后看清 run 的分叉：paged run 在 [flashinfer/prefill.py:2854-L2855](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L2854-L2855) 调 `self._cached_module.paged_run(*run_args)`，C++ 侧 `BatchPrefillWithPagedKVCacheRun` 会把页表三件套组装成 `paged_kv_t` 结构（[csrc/batch_prefill.cu:296-L303](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_prefill.cu#L296-L303)），再分派到 `BatchPrefillWithPagedKVCacheDispatched` kernel（[csrc/batch_prefill.cu:365-L370](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_prefill.cu#L365-L370)）。ragged run 则走 `BatchPrefillWithRaggedKVCacheRun`（[csrc/batch_prefill.cu:108-L114](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_prefill.cu#L108-L114)），用裸的 `RaggedParams`（`k`/`v` 是连续一维指针，无需页表）。两者共享同一份 `plan_info`——这就是 4.1 节「plan 统一、run 分叉」在 C++ 层的落点。

#### 4.4.4 代码实践

**目标**：把 decode 与 prefill 的 `plan` 签名并排阅读，亲眼看清楚「prefill 多了什么」。

**步骤**：

1. 打开 `flashinfer/decode.py` 跳到 1159 行的 `def plan`，数一下它有几个位置参数（不含 `self`）。
2. 打开 `flashinfer/prefill.py` 跳到 2049 行的 paged `def plan`，对比它多出了哪些位置参数。

**预期结果**：decode 的位置参数是 `(indptr, indices, last_page_len, num_qo_heads, num_kv_heads, head_dim, page_size, ...)`；prefill paged 的是 `(qo_indptr, paged_kv_indptr, paged_kv_indices, paged_kv_last_page_len, num_qo_heads, num_kv_heads, head_dim_qk, page_size, head_dim_vo=..., custom_mask=..., causal=...)`。你能直观看到 prefill 多出 `qo_indptr`、`head_dim_qk`/`head_dim_vo` 拆分、`causal`、`custom_mask` 这些 decode 没有的项。

#### 4.4.5 小练习与答案

**练习 1**：decode 的 `plan` 里为什么没有 `qo_indptr`？
**答案**：decode 阶段每个请求只有 1 个 query（刚生成的那一个 token），query 数恒等于 batch_size，不存在「变长 query 序列」，自然不需要 `qo_indptr`。它用 `q_len_per_req`（默认 1）来描述这个固定值。

**练习 2**：prefill 的 `head_dim_qk` 和 `head_dim_vo` 可以不同，这对哪个场景有用？
**答案**：对「QK 用一种 head_dim、V/输出用另一种」的模型有用（某些多模态或特定注意力变体）。decode 只用一个 `head_dim`，因为它沿用的是 Q/K/V 同维的传统设定。

## 5. 综合实践

把本讲三个要点（ragged 变长索引、plan/run 两段式、与 decode 的差异）串起来。**任务**：构造一个变长 ragged prefill 批次，跑通 `BatchPrefillWithRaggedKVCacheWrapper`，再把它和 decode wrapper 的 plan 参数逐项对比。

**操作步骤**（示例代码；无 GPU 环境下标注「待本地验证」）：

```python
import torch, flashinfer

# 1) 构造 3 个变长请求：qo 长度 [2,3,1]，kv 长度 [4,2,5]
batch = 3
qo_indptr = torch.tensor([0, 2, 5, 6], dtype=torch.int32, device="cuda:0")
kv_indptr = torch.tensor([0, 4, 6, 11], dtype=torch.int32, device="cuda:0")
num_qo_heads, num_kv_heads, head_dim = 8, 2, 128   # GQA: 8/2=4

nnz_qo, nnz_kv = 6, 11
q = torch.randn(nnz_qo, num_qo_heads, head_dim, dtype=torch.float16, device="cuda:0")
k = torch.randn(nnz_kv, num_kv_heads, head_dim, dtype=torch.float16, device="cuda:0")
v = torch.randn(nnz_kv, num_kv_heads, head_dim, dtype=torch.float16, device="cuda:0")

# 2) 建 wrapper + plan（注意：ragged plan 没有 paged_kv_indices/last_page_len）
workspace = torch.empty(128 * 1024 * 1024, dtype=torch.uint8, device="cuda:0")
wrapper = flashinfer.BatchPrefillWithRaggedKVCacheWrapper(workspace, "NHD")
wrapper.plan(qo_indptr, kv_indptr, num_qo_heads, num_kv_heads, head_dim, causal=True)

# 3) run（每个 Transformer 层调一次，复用 plan 的辅助结构）
o = wrapper.run(q, k, v)
print(o.shape)   # 预期 [6, 8, 128]
```

**需要观察的现象**：

1. 首次 `plan`/`run` 会触发 JIT 编译（首次较慢），第二次同参数调用走缓存（很快）——对应 u2-l5 的两级缓存。
2. `o.shape[0]` 必须等于 `qo_indptr[-1]`（这里是 6），这正是 4.4.3 里那条校验的来源。
3. 对比 decode：如果你把同一个 workspace 拿去建 `BatchDecodeWithPagedKVCacheWrapper`，它的 `plan` 不接受 `qo_indptr`，而是接受 `indptr/indices/last_page_len`（页表三件套）——亲手对比这两个 `plan` 的参数列表，就能直观体会 4.4 节的差异表。

**预期结果**：输出形状 `[6, 8, 128]`；与一个朴素的 PyTorch 逐请求 attention（带 causal 掩码）结果在 `atol=1e-3` 量级一致（待本地验证）。

## 6. 本讲小结

- FlashInfer 的 batch prefill 有两个并列 wrapper：`BatchPrefillWithPagedKVCacheWrapper`（KV 在页池）和 `BatchPrefillWithRaggedKVCacheWrapper`（KV 是拼接一维张量），二者复用同一份 JIT 模块。
- 核心设计是 **plan 统一、run 分叉**：两个 wrapper 的 `plan` 都汇入同一个 C++ `plan` 符号、产出同一份 15 个 `int64` 的 `PrefillPlanInfo`；只有 `run` 才分出 `paged_run` / `ragged_run`。
- **ragged 把 `page_size` 写死为 1**，从而让同一套「按 page 切 KV chunk」的 plan 代码同时服务分页与拼接两种存储。
- 变长批次靠前缀和数组描述：`qo_indptr`（两个 wrapper 都要）、`kv_indptr`（ragged）或页表三件套（paged）；plan 把它们翻译成统一的 `(qo_indptr_h, kv_indptr_h, kv_len_arr)`。
- plan 内部用 `PrefillSplitQOKVIndptr` 同时切 Q（`cta_tile_q`）和 KV（split-kv），把变长批次展平成 `padded_batch_size` 个等粒度工作单元；split-kv 为真时还要分配 `tmp_v`/`tmp_s` 做部分结果合并。
- 与 decode 相比，prefill 多了 `qo_indptr`（变长 query）、`causal`/`custom_mask`（掩码）、`head_dim_qk`/`head_dim_vo`（可拆分），并且是 compute-bound 需要切 Q，而 decode 是 memory-bound 只切 KV。

## 7. 下一步学习建议

- **后端选择**：本讲多次出现 `determine_attention_backend`，下一讲 u3-l5 会专门讲它如何根据硬件/dtype 在 fa2/fa3/cudnn/trtllm-gen 之间选优。
- **进阶变体**：当你想给 prefill 注入额外参数（自定义 logit 软截断、attention sink 等），去看 u4-l6 的 `*_customize_config.jinja` 与 variants 机制，它是建立在本讲的 `get_customize_batch_prefill_module` 之上的。
- **kernel 内部**：本讲止步于 `BatchPrefillWithPagedKVCacheDispatched` 的分派入口；想看 tile 内部的 QK/softmax/PV 计算，应继续阅读 `include/flashinfer/attention/prefill.cuh` 的模板实现（注意它很大，建议从 `SingleM`/`BatchPrefill` 的入口函数顺着读）。
- **统一混合批次**：如果想在一个批次里同时跑 prefill 和 decode 请求，u4-l5 的 `BatchAttention` 会把这些 wrapper 再包一层做区间分派。
