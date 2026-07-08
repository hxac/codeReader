# LLM 推理中的注意力与 Paged KV Cache

## 1. 本讲目标

本讲是「注意力基础」单元的第一篇，也是从「跑通一个 kernel」走向「理解推理服务如何组织注意力」的转折点。读完本讲，你应当能够：

- 区分 LLM 推理中 **decode / prefill / append** 三个注意力阶段，并说出它们在查询（query）长度、KV 来源、调度方式上的差异；
- 说清楚 **为什么动态批处理服务里 KV cache 必须分页（paged）或变长（ragged）存储**，而不是用一个规整的 `[batch, max_len, ...]` 大张量；
- 看懂 FlashInfer 的 **`plan` / `run` 两段式 API**：`plan` 阶段要预先知道哪些信息、`run` 阶段又为什么把这些信息和真正的张量计算解耦。

本讲只读 `README.md`、`flashinfer/decode.py`、`flashinfer/prefill.py` 三个文件（外加被它们引用的 `flashinfer/page.py`、`flashinfer/utils.py` 中的少量符号），**不深入 CUDA kernel 内部**。kernel 实现留给后续讲义（u3-l3、u3-l4）。

## 2. 前置知识

在进入本讲前，请确认你已经具备下面这些认知（它们来自前置讲义 u1-l5、u2-l1，本讲不重复）：

- **注意力是什么**：给定查询 \(Q\)、键 \(K\)、值 \(V\)，标准 scaled dot-product attention 为

  \[
  \mathrm{Attention}(Q,K,V)=\mathrm{softmax}\!\left(\frac{QK^{\top}}{\sqrt{d}}\right)V
  \]

  其中 \(d\) 是每个注意力头的维度（`head_dim`）。

- **KV cache 是什么**：自回归生成时，每生成一个新 token，都要拿当前 query 去和**历史上所有 token 的 K、V** 做注意力。为了避免每步都重算历史 KV，推理引擎会把历史 KV 缓存下来，这就是 KV cache。

- **FlashInfer 的分层与 JIT**（u1-l3、u2-l1）：`flashinfer/*.py` 是 Python wrapper，它会通过 JIT 生成并加载一个编译好的 CUDA 模块，再调用该模块导出的函数。你不必关心编译细节，只需知道「调用 wrapper = 先拿到一个可执行的模块函数」。

- **GQA（Grouped Query Attention）**：query 头数 `num_qo_heads` 是 KV 头数 `num_kv_heads` 的整数倍，多个 query 头共享同一组 KV。

如果你对「分页内存」这个操作系统概念有印象（虚拟页、页表、按需分配），本讲会非常顺——FlashInfer 的 paged KV cache 几乎就是把这个思想搬到了 GPU 显存上。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪些部分 |
|------|------|------------------|
| `README.md` | 项目总览与功能清单 | 确认 attention 三阶段、paged/ragged KV 的产品定位 |
| `flashinfer/decode.py` | decode 注意力的 Python wrapper | `BatchDecodeWithPagedKVCacheWrapper` 类、`plan`/`run` 方法、page table 参数约定 |
| `flashinfer/prefill.py` | prefill/append 注意力的 Python wrapper | `BatchPrefillWithPagedKVCacheWrapper` 类、`qo_indptr`（变长 query）约定 |
| `flashinfer/page.py` | KV cache 的页表工具与 `append` 写入 | `get_seq_lens`（页表→序列长度）、`append_paged_kv_cache`（写入新 KV） |
| `flashinfer/utils.py` | 公共类型/校验工具 | `TensorLayout` 枚举（`NHD`/`HND`） |

一句话串联：`page.py` 负责「KV cache 长什么样、怎么往里写」，`decode.py`/`prefill.py` 负责「拿着 query 去 cache 上算注意力」，而 `plan`/`run` 是 wrapper 内部的两段式生命周期。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 注意力的三个阶段：decode / prefill / append**
- **4.2 为什么需要分页与变长（ragged）KV cache**
- **4.3 plan / run 两段式 API 的设计动机**

### 4.1 注意力的三个阶段：decode / prefill / append

#### 4.1.1 概念说明

在一个 LLM 推理服务的生命周期里，同一个注意力算子要服务于三种截然不同的「形状」：

1. **Prefill（预填充）**：用户发来一段 prompt（可能是几百到几千个 token），引擎要**一次性**把这段 prompt 的所有 query token 拿去和历史 KV（通常为空或只有系统前缀）做注意力。特点是 **query 是一段变长序列**，计算量大、是 compute-bound。

2. **Decode（解码）**：prefill 之后，模型一个 token 一个 token 地生成。每一步只有 **1 个新 query token**（对应每个请求），它要和该请求的全部历史 KV 做注意力。特点是 **query 长度恒为 1**，但 KV 长度持续增长，是 memory-bound。

3. **Append（追加写入）**：严格说 append 不是「算注意力」，而是**把新生成 token 的 K、V 写进 KV cache**。它是 prefill/decode 之前/之后的「配套动作」——你得先把新 KV 存好，下一步注意力才能读到它。

README 在介绍 attention 能力时就把这三者并列：

- **Paged and Ragged KV-Cache**: Efficient memory management for dynamic batch serving
- **Decode, Prefill, and Append**: Optimized kernels for all attention phases

参见 [README.md:31-32](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/README.md#L31-L32)，这里明确点出「paged/ragged 是为了 dynamic batch serving」，以及 decode/prefill/append 是「all attention phases」。

#### 4.1.2 核心流程

把一个请求在服务中的生命周期画成时间线：

```
[prompt 到达]
   │  ① prefill: q=[prompt 的全部 token], 一次性算注意力
   │           同时 append: 把 prompt 的 K/V 写入 KV cache
   ▼
[生成 token_1]  ② append token_1 的 K/V → decode: q=[token_1] 对全部历史 KV
   ▼
[生成 token_2]  ② append token_2 的 K/V → decode: q=[token_2]
   ▼
   ...（持续 decode）...
```

关键区别用一张表总结：

| 阶段 | 每请求 query 长度 | KV 来源 | 主要瓶颈 | 对应 wrapper |
|------|------------------|---------|----------|--------------|
| prefill | 一段（变长，由 `qo_indptr` 描述） | KV cache（通常一开始较少） | compute | `BatchPrefillWithPagedKVCacheWrapper` |
| decode | 1（`q_len_per_req=1`） | KV cache（持续增长） | memory 带宽 | `BatchDecodeWithPagedKVCacheWrapper` |
| append | —— | 把新 K/V 写进 cache | 写入 | `append_paged_kv_cache`（`page.py`） |

#### 4.1.3 源码精读

**decode 阶段**：`decode.py` 的类文档开宗明义，说明它是「为一批请求做带 paged kv-cache 的 decode 注意力」，并指出 paged kv-cache 这个思路最早由 vLLM 提出：

```python
class BatchDecodeWithPagedKVCacheWrapper:
    r"""Wrapper class for decode attention with paged kv-cache (first proposed in
    `vLLM <https://arxiv.org/abs/2309.06180>`_) for batch of requests.
```

见 [flashinfer/decode.py:653-656](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L653-L656)。

`run` 方法对 query 形状的描述清楚体现了「每请求只有 1 个 query 向量」：

```python
q : torch.Tensor
    The query tensor, shape: ``[batch_size * q_len_per_req, num_qo_heads, head_dim]``
    q_len_per_req doesn't need to match the value passed to plan()
```

见 [flashinfer/decode.py:1643-1645](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1643-L1645)。注意第一维是 `batch_size * q_len_per_req`，标准 decode 时 `q_len_per_req=1`，所以 q 的形状就是 `[batch_size, num_qo_heads, head_dim]`——一个请求一行。

**prefill 阶段**：`prefill.py` 的类文档说它是「为一批请求做 prefill/append 注意力」：

```python
class BatchPrefillWithPagedKVCacheWrapper:
    r"""Wrapper class for prefill/append attention with paged kv-cache for batch of
    requests.
```

见 [flashinfer/prefill.py:1475-1477](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L1475-L1477)。它的 docstring 示例里 query 不再是「每请求一行」，而是一个**拼起来的变长序列**，用一个前缀和数组 `qo_indptr` 来切分：

```python
>>> nnz_qo = 100
>>> qo_indptr = torch.tensor(
...     [0, 33, 44, 55, 66, 77, 88, nnz_qo], dtype=torch.int32, device="cuda:0"
... )
```

见 [flashinfer/prefill.py:1497-1500](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L1497-L1500)。`nnz_qo=100` 表示这批 7 个请求的 query token **总共有 100 个**，`qo_indptr` 的相邻差值（33-0=33, 44-33=11, …）就是每个请求各自的 query 长度。这就是 prefill 与 decode 在 query 形状上的根本差异。

**append 阶段**：写入新 KV 的入口在 `page.py`：

```python
def append_paged_kv_cache(
    append_key: torch.Tensor,
    append_value: torch.Tensor,
    batch_indices: torch.Tensor,
    positions: torch.Tensor,
    paged_kv_cache: Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]],
    kv_indices: torch.Tensor,
    kv_indptr: torch.Tensor,
    kv_last_page_len: torch.Tensor,
    kv_layout: str = "NHD",
) -> None:
```

见 [flashinfer/page.py:403-413](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/page.py#L403-L413)。它接收「待写入的 K/V」加上「写到哪个请求、哪个位置」的索引信息（`batch_indices`、`positions`），把数据 scatter 写进 paged KV cache。本讲只把它作为「append 阶段」的入口认识一下，细节在 u3-l2 详讲。

> 小结：同样一个注意力，**因为 query 的长度不同（1 个 vs 一段），FlashInfer 给了两个不同的 wrapper**（decode / prefill），而 append 是独立的「写 cache」算子。这是理解后续所有注意力变体的前提。

#### 4.1.4 代码实践

**实践目标**：从源码层面确认「decode 的 query 是每请求 1 个，prefill 的 query 是变长序列」。

**操作步骤**：

1. 打开 [flashinfer/decode.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py)，定位 `BatchDecodeWithPagedKVCacheWrapper.run` 的 docstring（约 1643 行），记录 q 的形状约定。
2. 打开 [flashinfer/prefill.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py)，定位 `BatchPrefillWithPagedKVCacheWrapper` 的 docstring 示例（约 1497 行），记录 `qo_indptr` 与 `nnz_qo`。
3. 在纸上画一个 batch_size=3 的场景：decode 时 q 形状 `[3, H, d]`；prefill 时三个请求 query 长度分别为 33、11、11，则 `qo_indptr=[0,33,44,55]`，q 形状 `[55, H, d]`。

**需要观察的现象**：decode 的 q 第一维等于 batch_size（`q_len_per_req=1`），prefill 的 q 第一维等于「所有请求 query 长度之和」。

**预期结果**：你能用一句话向别人解释「为什么 prefill 需要 `qo_indptr` 而 decode 不需要」。

**待本地验证**：如果你本地有 GPU 且已装 flashinfer，可以分别构造上面两种形状的 q，调用对应 wrapper（注意先 `plan`），确认没有 shape 报错。

#### 4.1.5 小练习与答案

**练习 1**：decode 阶段每个请求每步只生成 1 个 token，为什么仍然要把「一批请求」放在一起算，而不是一个一个算？

**参考答案**：单个请求的 decode 是 memory-bound（要读全部历史 KV 但只算 1 个 query），GPU 算力大量闲置。把多个请求的 query 拼成 `[batch_size, num_qo_heads, head_dim]` 一起算，可以**复用同一次 KV 的访存**、提高硬件利用率，这正是 continuous batching（动态批处理）的核心收益。

**练习 2**：`append_paged_kv_cache` 和 `BatchPrefillWithPagedKVCacheWrapper` 都带「append」字样，它们是一回事吗？

**参考答案**：不是。`append_paged_kv_cache`（`page.py`）只负责**把新的 K/V 数据写进 cache**，不计算注意力；而 `BatchPrefillWithPagedKVCacheWrapper` 的类文档里写的是「prefill/**append** attention」，这里的 append 指的是「对新追加进来的 token 段算注意力」（一种计算），两者一个是「写」、一个是「算」。

---

### 4.2 为什么需要分页与变长（ragged）KV cache

#### 4.2.1 概念说明

先看「最朴素」的 KV cache 存法：给 batch 里每个请求预留 `max_len` 长度的连续显存，cache 张量形状是 `[batch_size, max_len, num_kv_heads, head_dim]`。这在推理服务里会暴露三个严重问题：

1. **显存碎片与浪费**：请求长度参差不齐，短的只用几十个 token，却占了 `max_len` 的空间；不同请求寿命不同（有的早结束），释放后留下空洞，难以复用。
2. **无法动态扩容**：请求是动态到来的，`batch_size` 在 continuous batching 下不断变化，事先定死 `[batch, max_len, ...]` 大张量既浪费又僵硬。
3. **共享前缀重复存储**：很多请求共享同一个 system prompt，朴素的 per-request 连续存储会让这段共享 KV 在每个请求里都存一份。

**分页 KV cache（paged KV cache）** 借鉴操作系统的虚拟内存分页：把 KV cache 切成固定大小的「页（page）」，每个 page 存 `page_size` 个 token 的 KV；每个请求用一张**页表（page table）**记录「我的第 i 段 KV 在哪个物理页」。这样：

- 显存按需分配，一个 page 用完再申请下一个，没有内部碎片以外的浪费；
- 请求之间共享物理页（共享前缀只要指向同一批 page 即可）；
- `batch_size`、序列长度都能动态变化。

**变长 / ragged 表示** 则是另一面：因为请求长度不一，把所有请求的 query（或 KV）**紧凑地拼成一维**，再用一个前缀和数组（`indptr`）切分，而不是用 `[batch, max_len, ...]` 这种带 padding 的稠密张量。FlashInfer 里 prefill 的 query 就是用 `qo_indptr` 描述的 ragged 张量。

#### 4.2.2 核心流程

FlashInfer 的 paged KV cache 用**三个一维数组**描述整批请求的页表（这与 vLLM 的 block table 思路一致）：

```
paged_kv_indptr       : [batch_size + 1]   前缀和，第 i 个请求的页在 indices 里的起始/结束
paged_kv_indices      : [nnz_pages]        把所有请求用到的物理页号顺序拼起来
paged_kv_last_page_len: [batch_size]       每个请求最后一页实际用了几个 token（1 ≤ 该值 ≤ page_size）
```

举个具体例子，设 `page_size=16`，batch 有 2 个请求，分别用了 17 页和 12 页（`indices` 简化为连续编号）：

```
请求 0 用了 17 页（第 0..16 号页），最后一页填满 → last_page_len[0] = 16
请求 1 用了 12 页（第 17..28 号页），最后一页只用 7 个 → last_page_len[1] = 7

paged_kv_indptr        = [0, 17, 29]      # 长度 = batch_size+1 = 3
paged_kv_indices       = [0,1,...,16, 17,18,...,28]   # 长度 = indptr[-1] = 29
paged_kv_last_page_len = [16, 7]          # 长度 = batch_size = 2
```

这正是 `decode.py` 类 docstring 里给出的示例数据（见 [flashinfer/decode.py:675-682](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L675-L682)）：

```python
>>> kv_page_indices = torch.arange(max_num_pages).int().to("cuda:0")
>>> kv_page_indptr = torch.tensor(
...     [0, 17, 29, 44, 48, 66, 100, 128], dtype=torch.int32, device="cuda:0"
... )
>>> # 1 <= kv_last_page_len <= page_size
>>> kv_last_page_len = torch.tensor(
...     [1, 7, 14, 4, 3, 1, 16], dtype=torch.int32, device="cuda:0"
... )
```

**从页表反推每个请求的真实序列长度**：一个请求的页数 = `indptr[i+1]-indptr[i]`；其中前 `页数-1` 页都是满的（各 `page_size` 个 token），只有最后一页是 `last_page_len[i]` 个 token。所以：

\[
\text{seq\_len}[i] = (\text{indptr}[i+1]-\text{indptr}[i]-1)\times \text{page\_size} + \text{last\_page\_len}[i]
\]

这个公式在源码里就是 `get_seq_lens`：

```python
def get_seq_lens(kv_indptr, kv_last_page_len, page_size):
    return (
        torch.clamp(kv_indptr[1:] - kv_indptr[:-1] - 1, min=0) * page_size
        + kv_last_page_len
    )
```

见 [flashinfer/page.py:326-349](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/page.py#L326-L349)。`clamp(..., min=0)` 是为了处理「某请求只用 1 页且没填满」的边界（此时 `页数-1=0`）。

**两种 KV 内存布局**：KV 张量本身的维度顺序有两种约定，由 `TensorLayout` 区分：

```python
class TensorLayout(Enum):
    NHD = 0   # [seq_len/num_pages, num_heads, head_dim] 维度顺序：先 token 后 head
    HND = 1   # [num_heads, seq_len/num_pages, head_dim] 维度顺序：先 head 后 token
```

见 [flashinfer/utils.py:45-47](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L45-L47)。NHD（token 在前）更接近「一段序列」的自然顺序；HND（head 在前）对某些后端 kernel 访存更友好。wrapper 构造时通过 `kv_layout="NHD"`/`"HND"` 选定，全批次统一。

#### 4.2.3 源码精读

`BatchDecodeWithPagedKVCacheWrapper.plan` 的参数列表完整体现了「页表三件套」：

```python
def plan(
    self,
    indptr: torch.Tensor,          # [batch_size+1]，页表前缀和
    indices: torch.Tensor,         # [indptr[-1]]，物理页号
    last_page_len: torch.Tensor,   # [batch_size]，最后一页长度
    num_qo_heads: int,
    num_kv_heads: int,
    head_dim: int,
    page_size: int,
    ...
)
```

见 [flashinfer/decode.py:1159-1167](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1159-L1167)，参数 docstring 在 [flashinfer/decode.py:1189-1195](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1189-L1195) 明确写了每个张量的 shape 与 dtype（均为 `torch.int32`）。

而 `run` 阶段对 **paged KV cache 张量本身** 的形状描述，则把「物理页」这一层暴露出来：

```
* a tuple (k_cache, v_cache) of 4-D tensors, each with shape:
  [max_num_pages, page_size, num_kv_heads, head_dim]  if kv_layout is NHD,
  [max_num_pages, num_kv_heads, page_size, head_dim]  if kv_layout is HND.

* a single 5-D tensor with shape:
  [max_num_pages, 2, page_size, num_kv_heads, head_dim]  if kv_layout is NHD,
  [max_num_pages, 2, num_kv_heads, page_size, head_dim]  if kv_layout is HND,
  where paged_kv_cache[:, 0] is the key-cache and paged_kv_cache[:, 1] is the value-cache.
```

见 [flashinfer/decode.py:1646-1658](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1646-L1658)。注意第一维是 `max_num_pages`（物理页总数），而不是 `batch_size` 或 `max_len`——这正是分页的体现：**KV cache 是一个「页池」，所有请求共享它，靠 page table 把逻辑序列映射到这些页**。

`plan` 内部会把页表转成「每个请求的真实 KV 长度」供 kernel 调度使用，调用的正是上面的 `get_seq_lens`：

```python
if seq_lens is None:
    kv_lens_arr_host = get_seq_lens(indptr_host, last_page_len_host, page_size)
else:
    kv_lens_arr_host = seq_lens.cpu()
```

见 [flashinfer/decode.py:1348-1351](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1348-L1351)。

> 一个易混点：`plan` 收到的 `indptr/indices/last_page_len` 描述的是**逻辑→物理的映射**（哪些页属于哪个请求），而 `run` 收到的 `paged_kv_cache` 张量是**物理页池本身**。两者通过 `indices` 里的页号勾连。

#### 4.2.4 代码实践

**实践目标**：亲手构造一个最小的 paged KV cache 页表，并用 `get_seq_lens` 反推序列长度，验证你对页表的理解。

**操作步骤**（纯 Python，不需要 GPU）：

```python
# 示例代码（可在 CPU 上运行，仅演示页表与序列长度换算）
import torch

page_size = 16
# 2 个请求：请求 0 用 3 页（最后一页填 8 个），请求 1 用 2 页（最后一页填 16 个=满）
paged_kv_indptr        = torch.tensor([0, 3, 5], dtype=torch.int32)
paged_kv_indices       = torch.tensor([10, 11, 12, 20, 21], dtype=torch.int32)  # 物理页号
paged_kv_last_page_len = torch.tensor([8, 16], dtype=torch.int32)

# 复现 page.get_seq_lens 的公式
seq_lens = torch.clamp(paged_kv_indptr[1:] - paged_kv_indptr[:-1] - 1, min=0) * page_size \
           + paged_kv_last_page_len
print(seq_lens)  # 期望: [2*16+8, 1*16+16] = [40, 32]
```

**需要观察的现象**：`seq_lens` 应输出 `tensor([40, 32], dtype=torch.int32)`。请求 0 有 3 页，前 2 页满（2×16=32），最后一页 8 个 → 40；请求 1 有 2 页，前 1 页满（16），最后一页 16 个 → 32。

**预期结果**：你能解释 `indices=[10,11,12,20,21]` 表示「请求 0 的 3 段 KV 分别存在物理页 10/11/12，请求 1 的 2 段存在页 20/21」，物理页号可以不连续、甚至乱序——这正是分页带来的灵活性。

**待本地验证**：若已装 flashinfer，可直接 `from flashinfer.page import get_seq_lens` 并与上面手算对比。

#### 4.2.5 小练习与答案

**练习 1**：如果某个请求刚开始生成、KV 还很短（比如只有 5 个 token，`page_size=16`），它的页表三项分别是什么？

**参考答案**：`indptr` 里它只占 1 页，`last_page_len=5`（`1 ≤ 5 ≤ 16`），`indices` 里有一个物理页号。此时 `seq_len = (1-1)*16 + 5 = 5`。

**练习 2**：为什么 `paged_kv_last_page_len` 的取值范围被 docstring 限定为 `1 ≤ x ≤ page_size`？0 行不行？

**参考答案**：一个请求只要还存活，就至少有 1 个 token 的 KV，最后一页至少有 1 个有效 entry，所以下界是 1。上界 `page_size` 表示最后一页正好填满（此时该请求恰好用了整数个页）。若允许 0，就分不清「最后一页空」和「该请求刚好填满上一页」这两种情况，会让 `get_seq_lens` 的 `-1` 项产生歧义。

---

### 4.3 plan / run 两段式 API 的设计动机

#### 4.3.1 概念说明

注意 `BatchDecodeWithPagedKVCacheWrapper` 的典型用法（来自类 docstring）：

```python
decode_wrapper = flashinfer.BatchDecodeWithPagedKVCacheWrapper(workspace_buffer, "NHD")
decode_wrapper.plan(kv_page_indptr, kv_page_indices, kv_last_page_len,
                    num_qo_heads, num_kv_heads, head_dim, page_size, ...)
for i in range(num_layers):
    q = ...
    o = decode_wrapper.run(q, kv_cache)   # 多层复用同一个 plan
```

见 [flashinfer/decode.py:671-706](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L671-L706)。注意一个关键现象：**`plan` 只调一次，`run` 在每一层都调一次**。这就是「两段式 API」。

为什么要拆成两段？因为有一类**只依赖批次结构、而不依赖具体张量数据**的「准备工作」，如果每次 `run` 都重做就是巨大浪费。具体来说：

- **split-k（分段并行）的划分**：decode 是 memory-bound，单个请求的 KV 很长时，为了打满 GPU，会把一个请求的 KV 切成若干段，分给多个线程块（CTA）并行算，最后再合并（reduce）。**切成几段、每段多长，只取决于「这个请求有多长」「batch 多大」「workspace 多大」**，与 q/k/v 的具体数值无关。
- **kernel 选择 / 启动参数**：根据 head 数、head_dim、数据类型、后端，要决定用哪个 kernel、launch 多少个 block。这些也只取决于形状/类型，与数据无关。
- **辅助索引的生成**：把页表、序列长度等整理成 kernel 启动时要用的紧凑结构。

这些「准备」在一个 Transformer 前向里对**所有层都一样**（各层只是 q/k/v 不同，但形状、批次结构完全相同）。所以合理的做法是：**`plan` 做一次准备并存下来，`run` 只负责拿数据去算**。

#### 4.3.2 核心流程

wrapper 的生命周期可以画成：

```
构造 wrapper（分配 workspace）
        │
        ▼
   plan(...)          ← 输入: 页表三件套 + 形状/类型/后端参数
        │                 做: 算 split-k 划分、选 kernel、生成辅助结构
        │                 产出: plan_info（存进 wrapper 内部，可被多次 run 复用）
        ▼
   run(q, kv_cache)   ← 输入: 这一层真正的 q 和 paged_kv_cache 张量
   run(q, kv_cache)      做: 用 plan_info + 数据真正算注意力
   run(q, kv_cache)      （每一层各一次）
        ▼
   (批次结构变化时) 再次 plan(...)   ← continuous batching 下，请求进出会导致 batch 变化，需重新 plan
```

这里有一个对 continuous batching 至关重要的细节：**`plan` 的结果只在「批次结构不变」时有效**。一旦有新请求加入、有请求结束（batch_size 或各请求长度变了），就必须重新 `plan`。所以实际服务里 `plan` 的频率是「每次批处理调度变化时一次」，而不是「每个 token 一次」，更不是「每层一次」。

`plan` 还有一个工程约束：**它不能被 CUDA Graph / `torch.compile` 捕获**。docstring 明确写了：

> The `plan` method cannot be used in Cuda Graph or in `torch.compile`.

见 [flashinfer/decode.py:1264](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1264)（相关 Note 在 [flashinfer/decode.py:1254-1264](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1254-L1264)）。原因是 `plan` 内部会根据运行时的 batch/长度**动态分配/调整 buffer、选择 kernel**，这些是 host 端的动态决策，无法被静态图捕获。`run` 则是确定性的张量运算，可以进图。这也是为什么 `CUDAGraphBatchDecodeWithPagedKVCacheWrapper`（[flashinfer/decode.py:2058-2070](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L2058-L2070)）要求 `batch_size` 固定、并把 indptr/indices/last_page_len 作为预分配 buffer 传入——为了让 `run` 能在固定形状下进图。

> 名词澄清：`end_forward` 在旧版 API 里用于「一次前向结束、清理 plan 状态」，但当前版本**已废弃、空操作**：

```python
def end_forward(self) -> None:
    r"""Warning: this function is deprecated and has no effect."""
    pass
```

见 [flashinfer/decode.py:2053-2055](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L2053-L2055)。你现在写代码不必调用它。

#### 4.3.3 源码精读

**`plan` 在校验和缓存形状/类型之外，真正干的活**是把页表换成序列长度（前面 4.2.3 已看到 `get_seq_lens`），然后据此决定 split-k 的分段等调度参数，最后把「这一批的结构信息」打成 `plan_info`，连同选好的 JIT 模块一起存到 wrapper 上。注意 `plan` 方法签名里那一长串**形状/类型/后端参数**（`num_qo_heads`、`num_kv_heads`、`head_dim`、`page_size`、`q_data_type`、`sm_scale`、`logits_soft_cap`、`window_left` …）——它们全部是「与具体 q/k/v 数值无关、只描述这一批结构」的信息：

见 [flashinfer/decode.py:1159-1184](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1159-L1184)。`plan` 把这些值缓存到 `self._cached_q_data_type`、`self._num_qo_heads`、`self._batch_size`、`self._sm_scale` 等成员上（见 [flashinfer/decode.py:1339-1346](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1339-L1346)），供后续 `run` 直接取用，避免每层重算。

**workspace 分两种**：构造 wrapper 时传入的 `float_workspace_buffer`（用户分配，建议 128MB，用于存 split-k 的中间注意力结果），加上 wrapper 内部维护的 `int_workspace_buffer`（存 plan 阶段生成的整型辅助结构）。`plan` 开头先做对齐校验：

```python
_check_workspace_buffer_alignment(self._float_workspace_buffer, "float_workspace_buffer")
_check_workspace_buffer_alignment(self._int_workspace_buffer, "int_workspace_buffer")
```

见 [flashinfer/decode.py:1266-1271](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1266-L1271)。float workspace 的用途在构造函数 docstring 里写得很直白：

```
float_workspace_buffer : torch.Tensor. Must be initialized to 0 for its first use.
    The user reserved float workspace buffer used to store intermediate attention results
    in the split-k algorithm. The recommended size is 128MB ...
```

见 [flashinfer/decode.py:736-740](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L736-L740)。「intermediate attention results in the split-k algorithm」一语道破 workspace 与 plan 阶段 split-k 划分的关系。

**`run` 阶段**则把缓存好的结构信息和这一层的张量一起喂给 JIT 模块。注意 `run` 会校验 q 与 plan 时缓存的 dtype 是否一致（`_check_cached_qkv_data_type`，[flashinfer/decode.py:1740-1742](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1740-L1742)），并从 q 的第一维**反推** `q_len_per_req`：

```python
actual_batch_size = self._paged_kv_last_page_len_buf.size(0)
...
q_len_per_req = q.size(0) // actual_batch_size
```

见 [flashinfer/decode.py:1743-1763](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1743-L1763)。这说明 `run` 是「数据相关、形状轻量」的：它信任 `plan` 已经把调度参数算好，自己只负责补齐「这一层特有的 q」。

**prefill 的 plan/run 模式同理**，只是多了一组描述「变长 query」的 `qo_indptr`：

```python
def plan(
    self,
    qo_indptr: torch.Tensor,         # [batch_size+1]，query 的前缀和（ragged）
    paged_kv_indptr: torch.Tensor,
    paged_kv_indices: torch.Tensor,
    paged_kv_last_page_len: torch.Tensor,
    num_qo_heads, num_kv_heads, head_dim_qk, page_size, ...
)
```

见 [flashinfer/prefill.py:2049-2058](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L2049-L2058)。prefill 的 `plan` 比 decode 多了「query 也是变长」这一层复杂性（要同时为 query 段和 KV 段做调度），但**两段式的动机完全一致**：把只依赖结构的准备工作和依赖数据的计算分开。

> 一句话总结 plan/run：**`plan` = 「这批请求长什么样、该怎么并行」，`run` = 「拿这一层的数据，按 plan 好的方式算」**。前者随批次结构变化而重做，后者每层都调、可进 CUDA Graph。

#### 4.3.4 代码实践

**实践目标**：对照源码，列举「`plan` 阶段需要预先知道的信息」与「`run` 阶段才提供的信息」，体会两段式的边界。

**操作步骤**：

1. 打开 [flashinfer/decode.py 的 `plan` 签名](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1159-L1184)，把它的参数分成三类抄在纸上：
   - **页表结构**：`indptr`、`indices`、`last_page_len`、`page_size`
   - **形状/类型**：`num_qo_heads`、`num_kv_heads`、`head_dim`、`q_data_type`、`kv_data_type`
   - **注意力数值参数**：`sm_scale`、`logits_soft_cap`、`window_left`、`pos_encoding_mode`、`rope_scale`
2. 打开 [flashinfer/decode.py 的 `run` 签名](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1618-L1638)，观察它真正「每层变化」的输入只有 `q` 和 `paged_kv_cache`（以及少量可选的 scale）。
3. 思考：为什么 `sm_scale`（softmax 缩放）放在 `plan` 而 `q` 放在 `run`？——因为 `sm_scale` 对同一批的所有层都一样（它只取决于 `head_dim` 和模型配置），而 `q` 每层不同。

**需要观察的现象**：你会清晰地看到，「与具体张量数值无关」的信息几乎全部进了 `plan`，「每层都变的数据」进了 `run`。

**预期结果**：你能用自己的话回答「`plan` 阶段需要预先知道哪些信息」——答案是：**页表三件套、形状（head 数与 head_dim、page_size）、数据类型、以及注意力的数值参数（sm_scale 等）**；唯独不需要真正的 q/k/v 张量数值。

**待本地验证**：本实践是源码阅读型，无需运行；若想跑通，需 GPU 且已装 flashinfer，可参照类 docstring 的示例（[flashinfer/decode.py:659-709](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L659-L709)）写一个最小 demo。

#### 4.3.5 小练习与答案

**练习 1**：在 continuous batching 服务里，下面哪些事件发生后必须重新 `plan`？为什么？
(a) 某个请求生成结束被移出批次；(b) 进入下一层 Transformer；(c) 新请求加入批次；(d) 同一请求生成了下一个 token。

**参考答案**：(a) 和 (c) 必须重新 `plan`——它们改变了 `batch_size` 或页表结构（`indptr/indices/last_page_len` 都会变）。 不必——同一前向内各层批次结构相同，`plan` 结果可复用，这正是两段式的收益。 严格说 `last_page_len` 或页数会变（KV 变长了），所以下一个生成步开始前通常也要重新 `plan`；但「同一前向的不同层之间」不需要。

**练习 2**：为什么 `plan` 不能进 CUDA Graph，而 `run` 可以？

**参考答案**：`plan` 会根据运行时的 batch_size、各序列长度**动态地**生成辅助结构、选择/调整 kernel 启动配置，这些都是 host 端的动态分支决策，CUDA Graph 无法静态捕获。而 `run` 在批次结构固定后，其 kernel 启动形状和指针都是确定的（只是指针指向的数据在变），属于可被静态图捕获的张量运算。所以 `CUDAGraphBatchDecodeWithPagedKVCacheWrapper` 才要求 batch 固定、buffer 预分配——把动态性局限在 `plan`，让 `run` 足够「死」以进图。

---

## 5. 综合实践

本讲的核心实践任务是（与学习目标对应）：**用一段文字 + 示意图说明，在一个动态批处理推理服务中，为什么需要把 KV cache 分页存储，以及 `plan` 阶段需要预先知道哪些信息**。

请按以下步骤完成：

1. **画一张分页 KV cache 示意图**。要求：
   - 画出 2~3 个请求，长度各不相同；
   - 画出一个「物理页池」（若干个 page，每个 `page_size` 个 token），用箭头/页号表示每个请求的逻辑序列如何散落在不同物理页上（参考 4.2.2 的例子）；
   - 在图上标注 `paged_kv_indptr`、`paged_kv_indices`、`paged_kv_last_page_len` 三个数组的具体取值。

2. **写一段 150~300 字的说明**，必须覆盖：
   - 朴素 `[batch, max_len, ...]` 存储方式在动态批处理下的两个具体痛点（碎片浪费、无法动态扩容 / 难以共享前缀）；
   - 分页如何缓解这两个痛点；
   - `plan` 阶段需要预先知道哪些信息（页表三件套、形状、数据类型、sm_scale 等数值参数），以及它**为什么不需要真正的 q/k/v 数据**。

3. **自检**：用你图里的页表数据，手算每个请求的序列长度，再与公式
   \[
   \text{seq\_len}[i] = (\text{indptr}[i+1]-\text{indptr}[i]-1)\times \text{page\_size} + \text{last\_page\_len}[i]
   \]
   对照，确认无误（这一步等价于复现 [page.py 的 `get_seq_lens`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/page.py#L326-L349)）。

> 本实践不需要 GPU，重点是建立「分页 + 两段式」的心智模型。完成后，你应该能向一个没读过 FlashInfer 源码的同事讲清楚这两个设计，这就达到了本讲的目标。

## 6. 本讲小结

- LLM 推理的注意力分三个阶段：**prefill**（query 是变长序列，compute-bound）、**decode**（每请求 1 个 query，memory-bound）、**append**（把新 KV 写进 cache）。它们对应不同的 wrapper 与算子。
- 动态批处理下，KV cache **必须分页（paged）或变长（ragged）存储**：分页用一个「物理页池 + 页表」消除显存碎片、支持按需分配与共享前缀；变长用 `indptr` 前缀和把参差序列紧凑拼接，避免 padding 浪费。
- 页表三件套是 `paged_kv_indptr`（`[batch+1]`）、`paged_kv_indices`（物理页号）、`paged_kv_last_page_len`（最后一页长度，`1≤x≤page_size`）；序列长度可由 `get_seq_lens` 公式反推。
- **`plan` / `run` 两段式 API**：`plan` 做「只依赖批次结构的准备」（split-k 划分、kernel 选择、辅助索引），结果对同一前向的所有层复用；`run` 只拿这一层的数据去算。
- `plan` 不能进 CUDA Graph（含动态决策），`run` 可以；continuous batching 下批次结构一变（请求进出）就要重新 `plan`，`end_forward` 已废弃。
- KV 张量有两种布局 `NHD`/`HND`（`TensorLayout`），第一维是 `max_num_pages` 而非 `batch_size`——这是「共享页池」的直接体现。

## 7. 下一步学习建议

本讲建立了「注意力三阶段 + 分页 KV + plan/run」的概念框架，但**没有真正跑过一个分页注意力、也没看 kernel 内部**。建议按以下顺序深入：

1. **u3-l2（Paged KV Cache 布局与 append）**：动手用 `append_paged_kv_cache` 把 token 的 KV 写进一个真实分页 cache，把本讲的页表概念变成可运行代码。
2. **u3-l3（BatchDecodeWithPagedKVCacheWrapper 的 plan/run）**：跟着一次真实 `plan`/`run` 调用进入 `csrc/batch_decode*.cu`，看 `plan_info` 如何被 JIT 模块消费、split-k 如何落地。
3. **u3-l4（BatchPrefillWithRaggedKVCacheWrapper）**：把 `qo_indptr` 这种变长 query 的调度也跑通，对照 decode 理解两者的差异。
4. **u3-l5（后端选择）**：在能跑之后，再看 `determine_attention_backend` 如何根据硬件/dtype 在 fa2/fa3/cudnn 间选优——这是你理解 FlashInfer「多后端」价值的下一站。

阅读源码时，建议常驻打开 [flashinfer/decode.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py) 与 [flashinfer/page.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/page.py)，它们是整个注意力子系统的「数据契约」所在。
