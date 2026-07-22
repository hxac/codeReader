# KV 内存池：ReqToTokenPool 与 TokenToKVPool

## 1. 本讲目标

上一讲（u4-l1）我们讲了 RadixAttention 的「大脑」——基数树 `RadixCache`，它负责决定「哪些前缀的 KV 可以复用」。但基数树自己并不真正存放 KV 张量，它只是给出了一张「命中了哪些 token 槽」的索引清单。这些索引到底指向哪里？真实的 KV 数据放在 GPU 显存的哪个张量里？一条新请求来了，它的 token 又是怎么被安插进显存的？

本讲就来回答这些问题。读完本讲，你应当能够：

1. 说出 SGLang **两级内存池**（`ReqToTokenPool` 与 `TokenToKVPool`）各自存放什么、为什么需要两级。
2. 读懂 `ReqToTokenPool` 如何用一张二维表把「请求 → 它的每个 token」映射到物理 KV 槽。
3. 区分 `MHATokenToKVPool`（普通多头注意力）与 `MLATokenToKVPool`（DeepSeek 风格的多头潜在注意力）两种物理 KV 池的形状差异。
4. 了解 `FP4`、`MXFP8` 等量化内存池如何用更少字节存放 KV。
5. 解释 `out_cache_loc` 这个贯穿调度器、`ForwardBatch` 与 `RadixAttention.forward` 的关键字段到底起什么作用。

## 2. 前置知识

- **注意力与 KV 缓存**：Transformer 自回归生成时，每生成一个 token，都要用当前 query 去和之前所有 token 的 Key/Value 做注意力。为了避免每步重算历史 token 的 K/V，我们把这些 K/V 缓存下来，称为 **KV cache**。一段长度为 \(L\) 的序列，每层都要存 \(L\) 份 K 和 \(L\) 份 V。
- **Paged 内存管理**：就像操作系统用「页」管理虚拟内存、用页表把虚拟地址翻译成物理地址一样，推理引擎也用「物理槽 + 索引表」来管理 KV 显存，避免为每条请求预分配一整块连续显存。SGLang 默认 `page_size=1`（一个 token 一页），也支持更大的页。
- **多头注意力（MHA）与多头潜在注意力（MLA）**：MHA 模型每个 token 在每层要存 `head_num × head_dim` 的 K 和 V；MLA（DeepSeek 提出）先用低秩把 KV 压缩成一个短的「潜在向量」再缓存，从而大幅省显存，代价是注意力计算更复杂。
- 建议先读 u4-l1（RadixAttention 与基数树缓存），本讲大量承接其中的 `out_cache_loc`、`prefix_indices` 等概念。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [python/sglang/srt/mem_cache/memory_pool.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py) | 内存池主体。定义两级池的全部类：`ReqToTokenPool`（请求→token 映射）、`MHATokenToKVPool`/`MLATokenToKVPool`（物理 KV 缓存），以及 `FP4`/`MXFP8` 等量化变体。文件开头注释给出了两级池的权威定义。 |
| [python/sglang/srt/mem_cache/allocation.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/allocation.py) | 两级池之间的「桥」。`alloc_for_extend` / `alloc_for_decode` 同时从两个池申请槽位，产出 `out_cache_loc`，并回写索引表。 |
| [python/sglang/srt/model_executor/forward_batch_info.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/model_executor/forward_batch_info.py) | `ForwardBatch` 数据类。其中的 `out_cache_loc` 字段把分配结果带给模型与注意力后端。 |
| [python/sglang/srt/layers/radix_attention.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py) | `RadixAttention.forward`，注意力后端的统一入口，它读取 `forward_batch.out_cache_loc` 决定把新算出的 K/V 写到哪里。 |
| [python/sglang/srt/managers/schedule_batch.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py) | `prepare_for_extend` / `prepare_for_decode` 调用 `allocation.py` 完成内存分配，并把 `out_cache_loc` 挂到批上。 |

## 4. 核心概念与源码讲解

### 4.1 两级映射全景与 out_cache_loc 数据流

#### 4.1.1 概念说明

SGLang 的 KV 内存管理用了一个非常清晰的两级结构，文件开头的注释就是权威定义：

```text
SGLang has two levels of memory pool.
ReqToTokenPool maps a request to its token locations.
TokenToKVPoolAllocator manages the indices to kv cache data.
KVCache actually holds the physical kv cache.
```

对应源码 [memory_pool.py:15-21](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L15-L21)。

把它翻译成一张「地址翻译」表：

| 层级 | 数据结构 | 形状 | 存什么 |
|------|----------|------|--------|
| 第一级 | `ReqToTokenPool.req_to_token` | `[请求数+1, 最大上下文长]` int32 | 每个请求一行，第 `t` 列存「该请求第 t 个 token 的物理 KV 槽号」 |
| 第二级 | `MHATokenToKVPool.k_buffer/v_buffer` | `[槽总数+page, head_num, head_dim]` 每层一个 | 每个 KV 槽号对应一行真实的 K（或 V）向量 |

为什么非要两级？因为：

1. **请求长度不固定**：不同请求 token 数差异巨大，若给每条请求预分配一整块连续显存，会浪费极多（内部碎片）。
2. **前缀复用**：两条请求共享前缀时，它们的 KV 在物理上只该存一份。两级映射让「请求视角的逻辑位置」和「物理槽」解耦，基数树才能把同一段物理槽借给多条请求。
3. **便于回收**：请求结束或被淘汰时，只需把它的物理槽归还给第二级池即可，不用搬运数据。

#### 4.1.2 核心流程

一次 prefill（extend）的内存分配与回写流程（来自 [allocation.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/allocation.py)）：

```text
prepare_for_extend(batch)
   └── alloc_for_extend(batch)
         ├── alloc_req_slots(...)            # 第一级：给每个请求分配一行 req_pool_idx
         │     └── ReqToTokenPool.alloc(reqs)
         ├── alloc_token_slots(...)          # 第二级：为新 token 分配物理 KV 槽
         │     └── allocator.alloc(N)        #   返回 out_cache_loc（N 个物理槽号）
         └── write_cache_indices(...)        # 把 prefix 槽 + 新槽写回 req_to_token 表
   └── self.out_cache_loc = out_cache_loc    # 挂到 ScheduleBatch，再借给 ForwardBatch
```

decode 阶段流程类似，走 [alloc_for_decode](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/allocation.py#L539-L593)：每条请求只新增 1 个 token，从第二级池要 1 个物理槽，写进 `req_to_token` 表中「当前序列长度」那一列。

#### 4.1.3 源码精读

`out_cache_loc` 的定义与注释——它是「输出 token 在 token_to_kv_pool 中的索引」：

```python
# The indices of output tokens in the token_to_kv_pool
out_cache_loc: torch.Tensor
```
见 [forward_batch_info.py:386-387](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/model_executor/forward_batch_info.py#L386-L387)。也就是说，它就是一组「物理 KV 槽号」，长度等于本轮要写入 KV 的新 token 数。

它的诞生地在 `prepare_for_extend` 里对 `alloc_for_extend` 的调用：

```python
# Allocate memory
out_cache_loc, req_pool_indices_tensor, req_pool_indices_cpu = alloc_for_extend(
    self
)
```
见 [schedule_batch.py:2216-2218](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L2216-L2218)，随后 `self.out_cache_loc = out_cache_loc`（[schedule_batch.py:2382](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L2382)）。decode 路径则是 `self.out_cache_loc = alloc_for_decode(self, token_per_req=1)`（[schedule_batch.py:2876](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L2876)）。

最终，`RadixAttention.forward` 把它交给注意力后端，后端据此把新算出的 K/V 写进物理池（细节见 4.3.3）。在 PCG（piecewise CUDA graph）路径里，`out_cache_loc` 还会被按真实 token 数裁剪：

```python
original_out_cache_loc = forward_batch.out_cache_loc
forward_batch.out_cache_loc = original_out_cache_loc[:real_query_num_tokens]
...
forward_batch.out_cache_loc = original_out_cache_loc
```
见 [radix_attention.py:346-349](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py#L346-L349) 与 [radix_attention.py:356-364](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py#L356-L364)。

#### 4.1.4 代码实践（源码阅读型）

**目标**：把 `out_cache_loc` 从诞生到消费的完整链路在源码里走一遍。

**操作步骤**：
1. 打开 [allocation.py:303-403](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/allocation.py#L303-L403) 的 `alloc_for_extend`，确认它返回的第一个值就是 `out_cache_loc`。
2. 打开 [schedule_batch.py:2216](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L2216)，确认 `out_cache_loc` 被赋给 `self.out_cache_loc`。
3. 打开 [forward_batch_info.py:387](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/model_executor/forward_batch_info.py#L387)，确认 `ForwardBatch` 借用了同名字段。
4. 打开 [radix_attention.py:143-280](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py#L143-L280)，确认 `RadixAttention.forward` 最终把 `forward_batch`（含 `out_cache_loc`）传给注意力后端。

**需要观察的现象 / 预期结果**：你会看到 `out_cache_loc` 是一根**一维 int64 张量**，在 prefill 时长度＝批内所有新 token 之和，在 decode 时长度＝批内请求数。它从头到尾没有被复制内容，只是「索引清单」在传递。

#### 4.1.5 小练习与答案

**练习 1**：如果 `out_cache_loc` 里出现了重复的物理槽号，会发生什么错误？
**答案**：两个新 token 会被写进同一个物理 KV 槽，后写的覆盖先写的，导致其中一条请求读到错误的 KV，输出错乱。因此第二级池的分配器必须保证不重发同一个空闲槽。

**练习 2**：为什么 `out_cache_loc` 是 int64，而 `req_to_token` 表是 int32？
**答案**：`req_to_token` 存的是物理槽号，规模在几百万以内，int32 够用且省一半显存；`out_cache_loc` 要进注意力后端的 CUDA kernel 做高级索引，很多 kernel（如 `store_cache`）要求 int64 索引以避免溢出和类型转换开销。

---

### 4.2 ReqToTokenPool：请求→token 槽映射

#### 4.2.1 概念说明

`ReqToTokenPool` 是两级映射的「第一级」。你可以把它想成一张大表：**每一行代表一个在途请求，每一列代表该请求的一个 token 位置，格子里的值是「这个 token 在第二级物理池里的槽号」**。

它解决两个问题：

- 给每条新到的请求分配一个**行号** `req_pool_idx`（即请求在池中的身份）。
- 维护这张「逻辑 token 序号 → 物理槽号」的翻译表，供注意力后端读取历史 KV、供调度器写入新 KV。

#### 4.2.2 核心流程

```text
请求到达
  └── alloc(reqs) → 给请求分一行 req_pool_idx（若无）
prefill 时
  └── write_cache_indices 把 [prefix 槽 ... + 新 out_cache_loc] 写进 req_to_token[req_pool_idx, 0:seq_len]
decode 每步
  └── write((req_pool_indices, seq_lens), new_slot)  # 在 seq_len 列追加新槽号
请求结束
  └── free(req) → 把 req_pool_idx 这一行还回 free_slots
```

注意一个细节：第 0 行是**保留的填充行**。CUDA Graph 回放时，为了对齐批大小会做 padding，被填充的「假请求」默认 `req_pool_idx=0`，于是它们对表的任何读写都落在第 0 行，不会污染真实请求。所以可用行号从 1 开始。

#### 4.2.3 源码精读

类定义与构造，见 [memory_pool.py:251-278](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L251-L278)：

```python
class ReqToTokenPool:
    """A memory pool that maps a request to its token locations."""
    ...
    def __init__(self, size, max_context_len, device, enable_memory_saver):
        ...
        self.size = size
        # +1 padding row at index 0: cuda-graph padded batches default
        # req_pool_indices to 0, so dummy reads/writes land here harmlessly.
        self._alloc_size = size + 1
        ...
        self.req_to_token = torch.zeros(
            (self._alloc_size, max_context_len), dtype=torch.int32, device=device
        )
        self.free_slots = list(range(1, self._alloc_size))
        self.req_generation = torch.zeros(self._alloc_size, dtype=torch.int64)
```

要点：
- `self._alloc_size = size + 1`：多分配一行作填充行（[memory_pool.py:268-270](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L268-L270)）。
- `self.req_to_token`：核心二维表，`int32`。
- `self.free_slots`：空闲行号栈，初始跳过 0。
- `self.req_generation`：每次行被重新分配时自增，用来检测「陈旧槽引用」（异步路径下防止读到上一代请求残留的索引）。

分配与回收，见 [memory_pool.py:286-317](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L286-L317)：

```python
def alloc(self, reqs: list[Req]) -> Optional[List[int]]:
    # 已经有 req_pool_idx 的请求（如 chunked prefill 跨块复用）直接复用
    reusing = [i for i, r in enumerate(reqs) if r.req_pool_idx is not None]
    ...
    need_size = len(reqs) - len(reusing)
    if need_size > len(self.free_slots):
        return None                       # 池满，返回 None 让上层 fail-loud
    select_index = self.free_slots[:need_size]
    self.free_slots = self.free_slots[need_size:]
    offset = 0
    for r in reqs:
        if r.req_pool_idx is None:
            r.req_pool_idx = select_index[offset]
            self.req_generation[r.req_pool_idx] += 1
            offset += 1
    return [r.req_pool_idx for r in reqs]

def free(self, req: Req):
    assert req.req_pool_idx is not None, "request must have req_pool_idx"
    self.free_slots.append(req.req_pool_idx)
    req.req_pool_idx = None
```

写表操作极简（[memory_pool.py:280-281](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L280-L281)）：

```python
def write(self, indices, values):
    self.req_to_token[indices] = values
```

谁在调用 `alloc` / `write`？正是 [allocation.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/allocation.py) 里的桥函数。`alloc_req_slots` 调 `req_to_token_pool.alloc(reqs)`（[allocation.py:284](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/allocation.py#L284)）；decode 时 `alloc_for_decode` 直接 `batch.req_to_token_pool.write((batch.req_pool_indices, locs), out_cache_loc.to(torch.int32))`（[allocation.py:578-580](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/allocation.py#L578-L580)）。

> 扩展：Mamba/线性注意力模型还会用 [HybridReqToTokenPool](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L1109)，它在 `ReqToTokenPool` 之外额外挂一个 `MambaPool` 与 `MambaSlotAllocator`，用 `req_index_to_mamba_index_mapping` 把请求再映射到一个 Mamba 状态槽。其 `req_to_token` 表本身与父类完全一致。

#### 4.2.4 代码实践（源码阅读型）

**目标**：追踪一张请求的「行」在 `req_to_token` 表里的演化。

**操作步骤**：
1. 在 [memory_pool.py:286-312](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L286-L312) 的 `alloc` 中确认：新请求拿到一个 `req_pool_idx`（假设是 7）。
2. 在 [allocation.py:55-101](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/allocation.py#L55-L101) 的 `write_cache_indices` 中确认：prefill 把 `req_to_token[7, 0:seq_len]` 填上「prefix 槽 + 新分配的 out_cache_loc」。
3. 在 [allocation.py:578-580](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/allocation.py#L578-L580) 确认：每个 decode 步把新槽写进 `req_to_token[7, seq_len]`，于是这一列不断向右延伸。
4. 在 [memory_pool.py:314-317](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L314-L317) 确认：请求结束后 `free` 把行号 7 还回 `free_slots`。

**需要观察的现象 / 预期结果**：你会看到 `req_to_token` 的某一行从左到右被逐步填满物理槽号，这正是「这条请求的 token 序列」在物理池里的足迹。**待本地验证**：若想看真实数值，可在 `alloc_for_extend` 返回后打印 `req_to_token[req_pool_idx, :seq_len]`。

#### 4.2.5 小练习与答案

**练习 1**：`--max-running-requests` 这个参数和 `ReqToTokenPool.size` 是什么关系？
**答案**：`size` 就是同时在途的请求数上限，对应 `--max-running-requests`。每条在途请求占一行；池满时 `alloc` 返回 `None`，`alloc_req_slots` 会抛 `RuntimeError` 提示调小 `--max-running-requests`（见 [allocation.py:285-290](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/allocation.py#L285-L290)）。

**练习 2**：chunked prefill（分块预填充）的请求跨多个块时，会不会每次都分配新行？
**答案**：不会。`alloc` 里有 `reusing = [i for i, r in enumerate(reqs) if r.req_pool_idx is not None]`，已经持有 `req_pool_idx` 的请求（chunked prefill 的中间块）直接复用原行，避免重排索引表。

---

### 4.3 MHATokenToKVPool：多头的物理 KV 缓存

#### 4.3.1 概念说明

`MHATokenToKVPool` 是两级映射的「第二级」，也是占用显存最大的对象。它**真正持有 K/V 张量**：每个物理槽号对应一行 K（和一行 V），行里装的是这个 token 在所有注意力头上的 K/V 向量。

它的关键参数：

- `size`：槽总数（即最多能缓存多少个 token 的 KV）。
- `page_size`：每页 token 数（默认 1）。
- `head_num`、`head_dim`：每个 token 的 K/V 形状。
- `layer_num`：模型层数——**每一层都有自己的一套 K/V 缓冲**，所以 `k_buffer`/`v_buffer` 是「每层一个张量」的列表。

显存占用大致为（bf16，`head_dim == v_head_dim` 时）：

\[
\text{bytes} \approx \text{layer\_num} \times \text{size} \times 2 \times \text{head\_num} \times \text{head\_dim} \times 2
\]

（最后那个 ×2 是 K 和 V 各一份；dtype 字节数 bf16=2。）这也是为什么 KV 缓存是长上下文推理的主要显存开销。

#### 4.3.2 核心流程

```text
读取（attention 用历史 KV）
  └── get_key_buffer(layer_id) → 返回 k_buffer[layer] 的 [槽总数, head_num, head_dim]
写入（新 token 算完 K/V）
  └── set_kv_buffer(layer, loc=out_cache_loc, cache_k, cache_v)
        └── _store_kv_layer → store_cache kernel 按 loc 散射写进 k_buffer/v_buffer
迁移（retract / 槽重排）
  └── move_kv_cache(tgt_loc, src_loc) → 把 src 槽的内容搬到 tgt 槽
```

存储布局有两种主流选择，由 `kv_cache_layout` 决定（[memory_pool.py:1739-1763](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L1739-L1763)）：

- **NHD**（默认）：`(slots, head_num, head_dim)`，一个 token 槽是一行。
- **HND**（`SGLANG_USE_HND_KVCACHE`）：`(num_pages, head_num, page_size, head_dim)`，把 `(page, head)` 折进一个索引，便于「按 KV 头稀疏」的页表（如 trtllm_mha 后端）。

> 还有一个 ROCm AITER 专用的 `vectorized_5d` 布局，非 ROCm 平台会被忽略，初学者可先跳过。

#### 4.3.3 源码精读

物理缓冲的形状由 `_kv_buffer_shapes` 给出（NHD 分支），见 [memory_pool.py:1978-1989](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L1978-L1989)：

```python
def _kv_buffer_shapes(self):
    """(k_shape, v_shape)"""
    ...
    rows = self.size + self.page_size
    return (
        (rows, self.head_num, self.head_dim),
        (rows, self.head_num, self.v_head_dim),
    )
```

注意又是 `size + page_size`：多出来的 `page_size` 行（即第 0 页）用来吸收「填充/假 token」的写入，和第一级池的填充行同理。真正的分配在 `_create_buffers_normal`（[memory_pool.py:1991-2042](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L1991-L2042)），为每一层创建一个 `k_buffer` 和一个 `v_buffer`。

**写入**是本池最关键的方法，`set_kv_buffer`（[memory_pool.py:2253-2329](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L2253-L2329)），核心几行：

```python
def set_kv_buffer(self, layer, loc_info, cache_k, cache_v, ...):
    loc, _, _ = unwrap_write_loc(loc_info)        # loc = out_cache_loc
    maybe_detect_oob(loc, 0, self.size + self.page_size, "set_kv_buffer (MHA)")
    ...
    if self.store_dtype != self.dtype:
        cache_k = cache_k.view(self.store_dtype)
        cache_v = cache_v.view(self.store_dtype)
    ...
    self._store_kv_layer(layer_id - self.start_layer, loc, cache_k, cache_v)
```

`_store_kv_layer`（[memory_pool.py:2331-2378](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L2331-L2378)）最终调用 `_set_kv_buffer_impl`，后者在能用高效 kernel 时走 `store_cache`（[memory_pool.py:141-188](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L141-L188)），把 `cache_k/cache_v` 按 `loc` 散射进 `k_buffer/v_buffer`。**这里就回答了实践任务的核心问题**：`out_cache_loc`（即 `loc`）告诉注意力后端「把这一批新算出的 K/V 写到物理池的哪些行」。

**读取**给注意力后端用历史 KV，见 [memory_pool.py:2221-2227](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L2221-L2227)：

```python
def get_key_buffer(self, layer_id: int):
    if self.layer_transfer_counter is not None:
        self.layer_transfer_counter.wait_until(layer_id - self.start_layer)
    return self._get_key_buffer(layer_id)
```

它返回整层的 K 缓冲，注意力后端再配合页表（来自 `req_to_token`）只读取相关槽。

谁调用 `set_kv_buffer`？是各注意力后端（flashinfer / fa / triton 等）在 `RadixAttention.forward` 里算完 Q/K/V 之后，对**新增 token** 调用 `set_kv_buffer` 落盘——这是 u5-l3 注意力后端的内容，这里只需知道 `out_cache_loc` 是写入地址即可。

#### 4.3.4 代码实践（参数观测型）

**目标**：感受 `size`、`head_num`、`head_dim`、`layer_num` 对 KV 显存的影响。

**操作步骤**：
1. 用一个小模型启动服务（如 `Qwen2.5-0.5B`，层数较少），观察日志里 `KV Cache is allocated. dtype: ..., #tokens: ..., KV size: ... GB` 这一行（来自 [_finalize_allocation_log](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L1615-L1633)）。
2. 改 `--mem-fraction-static`（调大→给 KV 更多显存→`size` 增大→`#tokens` 增大），重启再看日志。
3. 用 `--context-length` 调大单请求上限，观察 `ReqToTokenPool` 的 `max_context_len` 维度（即表的列数）变化，而 `size`（表的行数与第二级槽数）主要由剩余显存决定。

**需要观察的现象 / 预期结果**：`#tokens` 与 `KV size` 应近似线性关系；增大 `mem-fraction-static` 后 `#tokens` 上升。**待本地验证**：若环境无 GPU，可只读日志格式，理解 `KVCache._finalize_allocation_log` 如何计算并打印。

#### 4.3.5 小练习与答案

**练习 1**：为什么 K 和 V 要分成两个 buffer 列表，而不是合并成一个？
**答案**：注意力计算中 K 和 V 用途不同（K 参与 QKᵀ 打分，V 用于加权求和），很多算子分别接收 K、V；分开存储让 kernel 访问模式清晰、便于独立做量化（如只量化 K）或独立迁移。MLA 池（4.4）则会把它们融合，因为 MLA 的 K/V 本就来自同一个潜在向量。

**练习 2**：`store_dtype` 和 `dtype` 何时不同？
**答案**：当 KV 用 FP8 等类型时，PyTorch 对 `float8_e5m2` 等不支持 `index_put`，所以代码把 buffer 以 `torch.uint8` 存储（`store_dtype=torch.uint8`），读写时再 `view` 回 `dtype`（见 [memory_pool.py:1591-1595](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L1591-L1595)）。

---

### 4.4 MLATokenToKVPool：潜在 KV 缓存（DeepSeek MLA）

#### 4.4.1 概念说明

MLA（Multi-head Latent Attention，多头潜在注意力）是 DeepSeek-V2/V3 提出的注意力机制。它的核心思想：**不缓存每个头的完整 K/V，而是缓存一个低秩压缩后的「潜在向量」**，推理时再用矩阵把潜在向量「升回」多头 K/V。

这样做的好处是显存大幅下降。以 DeepSeek-V3 为例：

- 普通多头：每 token 每层 K+V ≈ \(2 \times \text{head\_num} \times \text{head\_dim}\)（如 128×128×2 = 32768 元素）。
- MLA：每 token 每层只缓存 \(\text{kv\_lora\_rank} + \text{qk\_rope\_head\_dim}\)（如 512+64 = 576 元素）。

节省比约 \(576 / 32768 \approx 1/57\)，这是 DeepSeek 能跑超长上下文的关键之一。

`MLATokenToKVPool` 就是专为这种「潜在 KV」设计的物理池。它和 MHA 池最大的区别是：**只有一个 `kv_buffer`（K/V 融合），且第二个维度是 1**（因为所有头共享同一个潜在向量）。

#### 4.4.2 核心流程

```text
维度计算
  kv_cache_dim = kv_lora_rank + qk_rope_head_dim   # 每个 token 缓存的元素数
分配缓冲
  kv_buffer[layer] = zeros(size + page_size, 1, kv_cache_dim)
写入（新 token）
  set_mla_kv_buffer(layer, loc, cache_k_nope, cache_k_rope)
        └── 把「潜在部分 + RoPE 部分」拼进 kv_buffer[loc]
读取（attention）
  get_mla_kv_buffer(layer, loc) → 返回拆分好的 (k_nope, k_rope)
```

`kv_lora_rank` 是潜在向量的维度（如 512），`qk_rope_head_dim` 是解耦的 RoPE 维度（如 64），二者拼接成一个 `kv_cache_dim`（如 576）的向量存下来。

#### 4.4.3 源码精读

类定义与维度，见 [memory_pool.py:3809-3850](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L3809-L3850)：

```python
class MLATokenToKVPool(KVCache):
    def __init__(self, size, page_size, dtype, kv_lora_rank, qk_rope_head_dim,
                 layer_num, device, enable_memory_saver, ...):
        ...
        self.kv_lora_rank = kv_lora_rank
        self.qk_rope_head_dim = qk_rope_head_dim
        ...
        self.kv_cache_dim = (
            override_kv_cache_dim
            if self.dsa_kv_cache_store_fp8
            else (kv_lora_rank + qk_rope_head_dim)
        )
        self._create_buffers()
```

物理缓冲，见 [memory_pool.py:3863-3878](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L3863-L3878)：

```python
def _create_buffers(self):
    ...
    # The padded slot 0 is used for writing dummy outputs from padded tokens.
    self.kv_buffer = [
        torch.zeros(
            (self.size + self.page_size, 1, self.kv_cache_dim),
            dtype=self.store_dtype,
            device=self.device,
        )
        for _ in range(self.layer_num)
    ]
```

对比 MHA 池的 `(slots, head_num, head_dim)`，这里第二个维度是 **1**（所有头共享一个潜在向量），第三个维度是 `kv_cache_dim`（潜在+RoPE 拼接）。注意它依然遵守 `size + page_size` 与填充槽 0 的约定。

**读取**时如何拆出 K 的「潜在部分」和「RoPE 部分」？见 [memory_pool.py:3909-3917](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L3909-L3917)：

```python
def get_value_buffer(self, layer_id: int):
    ...
    return self.kv_buffer[layer_id - self.start_layer][..., : self.kv_lora_rank]
```

即 V（潜在部分）就是 `kv_buffer` 的前 `kv_lora_rank` 切片，K 的 RoPE 部分是后 `qk_rope_head_dim` 切片。`get_mla_kv_buffer`（[memory_pool.py:4014-4035](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L4014-L4035)）则用 triton kernel 一次性把两段读出来。

**写入**用专门的 `set_mla_kv_buffer`（[memory_pool.py:3998-4012](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L3998-L4012），内部 `_write_mla_kv_buffer` 在 [memory_pool.py:3949-3996](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L3949-L3996)），通过 `set_mla_kv_buffer_triton` 把 `cache_k_nope`（潜在）和 `cache_k_rope`（RoPE）写入 `kv_buffer[loc]`。注意它也接收 `loc`（即 `out_cache_loc`），和 MHA 池一样靠这个索引定位写入行。

> 还有一个 `MLATokenToKVPoolFP4`（[memory_pool.py:4079](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L4079)），把 MLA 的潜在向量也做 FP4 量化，进一步省显存。

#### 4.4.4 代码实践（对比型）

**目标**：对比 MHA 与 MLA 两种池的形状差异。

**操作步骤**：
1. 打开 [memory_pool.py:1985-1988](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L1985-L1988)（MHA 的 `(rows, head_num, head_dim)`）。
2. 打开 [memory_pool.py:3871-3877](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L3871-L3877)（MLA 的 `(rows, 1, kv_cache_dim)`）。
3. 假设 `size=10000`、`layer_num=28`、bf16，分别手算两种池的显存：MHA（head_num=16, head_dim=128）vs MLA（kv_cache_dim=576）。

**需要观察的现象 / 预期结果**：
- MHA 每层 ≈ \(10000 \times 16 \times 128 \times 2 \text{(K,V)} \times 2 \text{(bf16)} \approx 81.9\text{MB}\)。
- MLA 每层 ≈ \(10000 \times 1 \times 576 \times 2 \text{(bf16)} \approx 11.5\text{MB}\)（K/V 已融合，无需再 ×2）。
MLA 单层约为 MHA 的 1/7（此处 head 配置较小；真实 DeepSeek 配置下差距更大）。**待本地验证**：可用 `torch.zeros(...).element_size()` 写小脚本核算。

#### 4.4.5 小练习与答案

**练习 1**：MLA 池的 `kv_buffer` 第二维为什么是 1，而不是 `head_num`？
**答案**：MLA 把所有头的 K/V 压缩进一个共享的低秩潜在向量，物理上只存这一份；多头是在注意力计算时由「上投影」矩阵从潜在向量恢复出来的，不在缓存里。

**练习 2**：`get_value_buffer` 为什么只取前 `kv_lora_rank` 列？
**答案**：`kv_buffer` 把「潜在部分（长度 `kv_lora_rank`）」和「RoPE 部分（长度 `qk_rope_head_dim`）」拼在一起存。Value 只用潜在部分，所以切前半段；Key 的 RoPE 部分单独取后半段用于位置编码。

---

### 4.5 量化内存池：FP4 与 MXFP8

#### 4.5.1 概念说明

KV 缓存是长上下文场景的显存大头，因此 SGLang 提供多种**量化内存池**，用更少字节存 KV，换取更大的 `size`（即更多可缓存 token）。本节看两种代表性实现：

- **FP4**（`MHATokenToKVPoolFP4`）：每个元素 4 比特，两个 FP4 值打包进 1 个 `uint8`；外加每 16 个元素共享一个 block scale（MX 风格）。相比 bf16 约压缩 4 倍。
- **MXFP8**（`MHATokenToKVPoolMXFP8`）：数据用 FP8 E4M3（8 比特），每 32 个元素配一个 E8M0 指数 scale。相比 bf16 约压缩 2 倍，精度比 FP4 高，配合 FA4 MXFP8 kernel。

两者都继承自 `MHATokenToKVPool`，只重写缓冲创建与读写，所以「两级映射 / `out_cache_loc` 写入」机制完全不变——量化的差异被封装在池内部。

#### 4.5.2 核心流程

```text
FP4 写入
  set_kv_buffer → FP4MXBlock16KVQuantizeUtil.batched_quantize(cache_k)
        └── bf16 → 打包 FP4(uint8) + per-16 scale
        └── k_buffer[loc] = packed_fp4 ; k_scale_buffer[loc] = scales
FP4 读取
  get_key_buffer → batched_dequantize(k_buffer, k_scale_buffer) → 还原 bf16 给 attention
MXFP8 类似，scale 块大小为 32，dtype 为 float8_e4m3fn + float8_e8m0fnu
```

#### 4.5.3 源码精读

**FP4 池**的缓冲创建，见 [memory_pool.py:2898-2946](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L2898-L2946)：

```python
class MHATokenToKVPoolFP4(MHATokenToKVPool):
    def _create_buffers(self):
        ...
        scale_block_size = 16
        self.store_dtype = torch.uint8
        self.k_buffer = [
            torch.zeros((m, n, k // 2), dtype=self.store_dtype, device=self.device)
            for _ in range(self.layer_num)
        ]                       # k//2：两个 FP4 打包进一个 uint8
        ...
        self.k_scale_buffer = [
            torch.zeros((m, (n * k) // scale_block_size), dtype=self.store_dtype, ...)
            for _ in range(self.layer_num)
        ]                       # 每 16 个元素一个 scale
```

要点：数据维度从 `head_dim` 变成 `head_dim // 2`（4 比特打包）；额外有 `k_scale_buffer`，每 `scale_block_size=16` 个元素一个 scale。写入时量化、读取时反量化（`_get_key_buffer` 调 `FP4MXBlock16KVQuantizeUtil.batched_dequantize`，见 [memory_pool.py:2954-2970](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L2954-L2970)）。

**MXFP8 池**的缓冲创建，见 [memory_pool.py:3206-3299](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L3206-L3299)：

```python
class MHATokenToKVPoolMXFP8(MHATokenToKVPool):
    MXFP8_SCALE_BLOCK_SIZE = 32
    def _create_buffers(self):
        ...
        self.store_dtype = torch.float8_e4m3fn
        self.k_buffer = [
            torch.zeros((m, n, k), dtype=self.store_dtype, device=self.device)
            for _ in range(self.layer_num)
        ]                       # 8 比特，无需打包
        ...
        self.k_scale_buffer = [
            torch.zeros(k_sf_shape, dtype=torch.float8_e8m0fnu, device=self.device)
            for _ in range(self.layer_num)
        ]                       # UE8M0 scale，每 32 元素一个
```

数据用真正的 `float8_e4m3fn`（8 比特，无需 `//2` 打包），scale 用 `float8_e8m0fnu`（仅指数），每 32 个元素一个。`page_size==128` 时 scale 还会按 FA4 的 `BlockScaledBasicChunk` 交错布局存放（[memory_pool.py:3266-3287](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L3266-L3287)）。

> 共同点：两者都在 `set_kv_buffer` 里接收 `loc`（= `out_cache_loc`），把量化后的数据和 scale 一起按 `loc` 散射写进缓冲。即「量化」是池内部细节，对外的两级映射接口完全不变。详细的量化方案对比（FP8/AWQ/GPTQ 等）见 u11-l1。

#### 4.5.4 代码实践（对比估算型）

**目标**：估算 FP4 / MXFP8 相对 bf16 的显存节省。

**操作步骤**：
1. 设 `size=100000`、`layer_num=28`、`head_num=16`、`head_dim=128`。
2. 手算三种池的每层显存：
   - bf16：\(100000 \times 16 \times 128 \times 2 \text{(K,V)} \times 2 \text{(字节)}\)
   - FP4：数据 \(100000 \times 16 \times 64 \times 2 \times 1\)（uint8，每元素 0.5 字节）＋ scale \(100000 \times (16\times128)//16 \times 2 \times 1\)
   - MXFP8：数据 \(100000 \times 16 \times 128 \times 2 \times 1\)（每元素 1 字节）＋ scale \(100000 \times 16 \times (128//32) \times 2 \times 1\)
3. 对比三者总量。

**需要观察的现象 / 预期结果**：FP4 总量约为 bf16 的 1/4～1/3（含 scale 开销），MXFP8 约为 1/2。也就是说，同样显存下 FP4 能缓存约 3～4 倍的 token。**待本地验证**：可在脚本里按上述公式直接相加。

#### 4.5.5 小练习与答案

**练习 1**：为什么 FP4 池的 `k_buffer` 第三维是 `head_dim // 2`，而 MXFP8 是 `head_dim`？
**答案**：FP4 是 4 比特，两个值打包进一个 8 比特的 `uint8`，所以元素数减半；MXFP8 是 8 比特 FP8，一个值占一个字节，元素数与原 `head_dim` 相同。

**练习 2**：量化内存池会不会改变 `out_cache_loc` 的含义？
**答案**：不会。`out_cache_loc` 始终是「物理 KV 槽号」（即第一维 `size+page_size` 的行索引），量化只改变「每个槽里存什么、几个字节」，索引体系完全一致。这也是为什么量化池只需重写 `_create_buffers`/`set_kv_buffer`/`get_key_buffer`，而不用动调度与分配逻辑。

---

## 5. 综合实践

**任务**：用一张图把一条 8 token 的请求在两级内存池里的分配过程画出来，并标注 `out_cache_loc` 在 `RadixAttention.forward` 中的作用。这是本讲规格要求的核心实践。

**场景设定**（`page_size=1`，MHA 模型）：

- 池初始状态：`ReqToTokenPool` 第 0 行为填充行，第 1 行空闲；`MHATokenToKVPool` 的物理槽 0 为填充槽，槽 1..8 空闲。
- 一条新请求 R 到达，无前缀命中（`prefix_indices` 为空），要 prefill 8 个 token。

**第一步：分配请求行**。`alloc_req_slots` → `ReqToTokenPool.alloc([R])`，R 拿到 `req_pool_idx = 1`（跳过填充行 0）。

**第二步：分配物理 KV 槽**。`alloc_token_slots` → 第二级分配器给出 8 个空闲槽，假设是 `[1,2,3,4,5,6,7,8]`，这就是 `out_cache_loc`。

**第三步：回写索引表**。`write_cache_indices` 把这 8 个槽写进 `req_to_token[1, 0:8]`：

```text
req_to_token 表（int32）          物理池 MHATokenToKVPool.k_buffer[layer]
行0(填充): [ .. ]                 槽0(填充): dummy
行1(请求R): [1,2,3,4,5,6,7,8]     槽1: token0 的 K   <- out_cache_loc[0]=1
行2:       [ .. ]                 槽2: token1 的 K   <- out_cache_loc[1]=2
...                               ...
                                  槽8: token7 的 K   <- out_cache_loc[7]=8
```

**第四步：前向与写入**。`prepare_for_extend` 把 `out_cache_loc=[1..8]` 挂到 `ForwardBatch`。模型前向算出这 8 个 token 的 K/V 后，`RadixAttention.forward`（[radix_attention.py:143](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py#L143)）把它交给注意力后端；后端对**新 token** 调用 `set_kv_buffer(layer, loc=out_cache_loc, cache_k, cache_v)`（[memory_pool.py:2253](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L2253)），于是 `k_buffer[1..8]` / `v_buffer[1..8]` 被填上真实 K/V。

**第五步：decode 延伸**。下一步 decode 时，`alloc_for_decode` 给 R 再要 1 个槽（假设槽 9），写进 `req_to_token[1, 8]=9`，新的 `out_cache_loc=[9]`（长度=批内请求数），后端把第 9 个 token 的 K/V 写进 `k_buffer[9]`。请求 R 的「足迹」就这样在表里向右延伸、在物理池里逐槽填满。

**用文字图总结两级映射**：

```text
请求 R (req_pool_idx=1)
   │  req_to_token[1, t]
   ▼
物理槽号 (1,2,3,...,8,9,...)
   │  k_buffer[layer][slot]
   ▼
真实 KV 向量 (head_num × head_dim)
```

**若把模型换成 MLA**（4.4）：第二步物理槽还是 `[1..8]`，`out_cache_loc` 不变；区别只在第四步写入的是 `kv_buffer[1..8]`（形状 `(1, kv_cache_dim)` 而非 `(head_num, head_dim)`），由 `set_mla_kv_buffer` 完成。这正说明两级映射对 MHA/MLA/量化池是统一的。

> 本实践为「源码阅读 + 手工推演」型：若需在真实服务中观察，可在 `alloc_for_extend` 返回后打印 `out_cache_loc.tolist()` 与 `req_to_token[req_pool_idx, :seq_len]` 核对（**待本地验证**）。

## 6. 本讲小结

- SGLang 用**两级内存池**管理 KV：第一级 `ReqToTokenPool` 把「请求的每个 token」映射到「物理槽号」，第二级 `TokenToKVPool`（如 `MHATokenToKVPool`）按物理槽号存放真实 K/V 张量。文件头注释 [memory_pool.py:15-21](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/memory_pool.py#L15-L21) 是权威定义。
- `ReqToTokenPool` 的核心是一张 `(size+1, max_context_len)` 的 `int32` 表，第 0 行/槽作填充，供 CUDA Graph 假请求安全落地；`alloc`/`free`/`write` 管理行号与回写。
- `allocation.py` 的 `alloc_for_extend`/`alloc_for_decode` 是两级池之间的桥，同时申请请求行与物理槽，产出 `out_cache_loc` 并回写索引表。
- `out_cache_loc` 是「新 token 在物理 KV 池中的槽号清单」，它从 `allocation.py` 诞生，经 `ScheduleBatch` 借给 `ForwardBatch`，最终在 `RadixAttention.forward` 里指导注意力后端把新 K/V 写到正确物理行。
- `MHATokenToKVPool` 用 `(slots, head_num, head_dim)` 的每层 K/V 缓冲；`MLATokenToKVPool` 用 `(slots, 1, kv_lora_rank+qk_rope_head_dim)` 的单一融合缓冲，靠低秩大幅省显存。
- `FP4`/`MXFP8` 等量化池只重写缓冲与读写，不改索引体系——量化是池内部细节，`out_cache_loc` 的语义对所有池一致。

## 7. 下一步学习建议

- **本讲只讲了「池怎么存」，没讲「槽怎么分/怎么淘汰」**。物理槽的分配与回收由 `TokenToKVPoolAllocator` 与基数树 `RadixCache` 协作完成，淘汰决策见 u4-l3（前缀缓存接口与淘汰策略）。
- **池被谁创建、`size` 如何算出来**：这些在 `ModelRunner`/`TpModelWorker` 的初始化里，结合 `--mem-fraction-static` 等参数决定，见 u5-l1（ModelRunner 与前向执行路径）。
- **`out_cache_loc` 的下游**：它如何被各注意力后端（flashinfer/fa/triton）消费，见 u5-l3（注意力后端）。
- **量化方案的完整对比**（FP8/AWQ/GPTQ/MXFP4 以及 KV cache 量化如何接入），见 u11-l1（量化方案）。
- **PD 分离下 KV 如何跨进程迁移**：`get_cpu_copy`/`load_cpu_copy`/`get_contiguous_buf_infos` 这些池方法服务于 KV 传输，见 u9-l2（KV 传输与连接器）。
