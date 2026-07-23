# KV Cache 池、存储与 Prefix Cache 抽象

## 1. 本讲目标

本讲是「KV Cache 管理」单元（u6）的第一篇，从最底层讲起。学完本讲后，你应当能够：

- 说出 Mini-SGLang 把 KV cache 存在哪里、用什么形状的张量组织，以及它如何在张量并行（TP）下被切分。
- 解释注意力后端如何把每一层新算出的 K/V 通过 `store_kv`「散布（scatter）」进 KV 池的正确槽位。
- 复述 `BasePrefixCache` 抽象的六个核心方法（`match_prefix` / `insert_prefix` / `evict` / `lock_handle` / `size_info` / `check_integrity`）各自承诺做什么、有哪些隐形契约。
- 说明 `NaivePrefixCache` 这个「什么都不做」的实现为何仍然合法，以及它作为「无前缀复用」基线的价值。

本讲只读「池存储」与「前缀缓存接口」两层，**不**展开 `RadixPrefixCache` 的基数树实现（那是 u6-l2），也**不**展开 `CacheManager` 的页分配回收（那是 u6-l3）。三者关系是：池是「裸显存」，前缀缓存是「在这些裸显存之上决定哪些页存共享前缀」，`CacheManager` 是「调度器侧的包工头，协调两者」。

## 2. 前置知识

在阅读本讲前，你需要先建立以下直觉（来自前置讲义）：

- **KV cache 是什么**：Transformer 自回归推理时，每一层对每个 token 都会算出一对向量 K（key）和 V（value）。decode 阶段每生成一个新 token，都要拿它去和**所有历史 token 的 K** 做注意力打分。为了不每次重算历史，必须把每层的 K/V 存起来，这部分显存就叫 KV cache。
- **Prefill / Decode 两阶段**（u2-l1）：prefill 一次性吃进整段 prompt，产生一大批 K/V；decode 每轮只新增 1 个 token 的 K/V。
- **page 与 page_size**（u5-l1）：KV cache 不是按 token 碎片分配，而是以「页（page）」为单位，每页固定 `page_size` 个 token 的连续槽位。`num_pages` 是总页数，`page_size` 是每页 token 数。
- **TP 下 KV head 的切分**（u5-l1、u9-l1）：注意力有多个「头」，其中 K/V 的头数 `num_kv_heads` 在 GQA/MQA 模型里常常少于 query 头数。多卡张量并行时，KV head 按 rank 均分，分不均时允许复制（`div_even(..., allow_replicate=True)`）。
- **`Context` 全局上下文**（u2-l1）：进程级单例，持有 `kv_cache`、`page_table`、`attn_backend` 等共享设施，让模型层、注意力后端、调度器都能拿到同一份池。

如果你对这些概念还陌生，建议先回到 u2-l1 与 u5-l1。本讲反复出现的 `cached_len` / `page_table` / `out_loc` 等字段都已在前面定义过。

## 3. 本讲源码地图

本讲涉及的关键文件都位于 `python/minisgl/kvcache/` 子包下：

| 文件 | 作用 | 本讲用它讲什么 |
|------|------|----------------|
| [kvcache/base.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/base.py) | 全部抽象基类：`BaseKVCachePool`、`BaseCacheHandle`、`SizeInfo`、`BasePrefixCache` 等 | 接口契约（讲什么、承诺什么） |
| [kvcache/mha_pool.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/mha_pool.py) | `MHAKVCache`：多头注意力（MHA）的池实现 | 真实存储布局与 `store_kv` |
| [kvcache/naive_cache.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/naive_cache.py) | `NaivePrefixCache`：禁用前缀复用的基线实现 | 一个合法的「空实现」长什么样 |
| [kvcache/__init__.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/__init__.py) | 工厂函数与注册表：`create_kvcache_pool`、`create_prefix_cache` | 池/缓存如何被实例化、如何按 `--cache` 选型 |

此外会少量引用调用方与支撑代码以建立上下文：

- [engine/engine.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py)：`create_kvcache_pool` 的唯一调用点，能看到 `+1` 个 dummy page 的来由。
- [scheduler/cache.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py)：`CacheManager` 如何消费前缀缓存接口（u6-l3 主角，本讲只借它的不变量）。
- [kernel/store.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/store.py)：`store_cache` kernel 的 Python 封装。

---

## 4. 核心概念与源码讲解

### 4.1 MHAKVCache：分页式 KV 缓冲布局

#### 4.1.1 概念说明

第一层抽象是「KV cache 池（pool）」——一块在 Engine 初始化时就一次性申请好的大显存，专门用来存所有层、所有 token 的 K 和 V。它的核心设计是**分页（paged）**：

- 不是「每个请求独占一段连续显存」，而是「整个池被切成 `num_pages` 个等大的页，每个请求按需领若干页，页与页之间可以不连续」。
- 注意力 kernel 计算时，通过一张「页表（`page_table`）」把请求的逻辑序列位置映射到池里的绝对槽位。

为什么要分页？因为它让「共享前缀复用」成为可能：两个请求如果共享同一段 system prompt，它们可以指向**同一组物理页**，而不必各自拷贝一份。这比连续分配（vLLM 早期、朴素实现）灵活得多。这一层只负责「存」，至于「哪些页给谁、能不能复用」是上一层 `BasePrefixCache` 的事。

`MHAKVCache`（Multi-Head Attention KV Cache）是当前唯一的池实现，类名暗示「按多头注意力组织」——TODO 注释里提到将来可能支持 MLA（Multi-head Latent Attention，DeepSeek 那种潜空间注意力）等变体。

#### 4.1.2 核心流程

池的一生：

1. **Engine 初始化时创建一次**：根据 `model_config`（头数、层数、头维）和算出的 `num_pages`，一次性 `torch.empty` 出整块显存（见 [engine/engine.py:L57-L63](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L57-L63)）。
2. **挂到全局 `Context`**：`self.ctx.kv_cache = ...`，此后模型层、注意力后端都通过 `ctx.kv_cache` 拿到它。
3. **写**：注意力后端每层前向算出新 K/V，调 `store_kv(...)` 写进对应槽位。
4. **读**：注意力后端调 `k_cache(layer_id)` / `v_cache(layer_id)` 拿到某层的视图，交给 paged-attention kernel 读取。
5. **页的归属流转**由上层 `CacheManager` + `BasePrefixCache` 决定，池本身不关心某页此刻属于谁。

整块缓冲的字节量是：

\[
\text{bytes} = 2 \times L \times P \times S \times H_{kv} \times D \times \text{itemsize}
\]

其中 \(L\) 是层数、\(P\) 是页数、\(S\) 是 `page_size`、\(H_{kv}\) 是本卡的 KV 头数、\(D\) 是 `head_dim`，因子 2 表示 K 与 V 两份，`itemsize` 是 dtype 每元素字节数（bf16/fp16 = 2）。这个式子和 [engine/engine.py:L150-L157](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L150-L157) 里 `cache_per_page` 的计算是一致的（`cache_per_page` 是「每页、跨所有层」的字节数）。

#### 4.1.3 源码精读

先看抽象基类 [`BaseKVCachePool`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/base.py#L10-L37)，它只规定四件事：按层取 K/V、按层写 K/V、暴露 device/dtype/num_layers。

```python
# kvcache/base.py:L16-L25 （节选）
@abstractmethod
def k_cache(self, index: int) -> torch.Tensor: ...
@abstractmethod
def v_cache(self, index: int) -> torch.Tensor: ...
@abstractmethod
def store_kv(self, k, v, out_loc, layer_id: int) -> None: ...
```

注意 `k_cache(layer_id)` 返回的是**单层**的视图，这样注意力后端可以逐层处理，而不必一次性把所有层都搬出来。

再看真实实现 [`MHAKVCache.__init__`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/mha_pool.py#L16-L37)，这是本讲最关键的一段：

```python
# kvcache/mha_pool.py:L26-L37
tp_info = get_tp_info()
local_kv_heads = div_even(num_kv_heads, tp_info.size, allow_replicate=True)
self._kv_buffer = torch.empty(
    (2, num_layers, num_pages, page_size, local_kv_heads, head_dim),
    device=device, dtype=dtype,
)
self._num_layers = num_layers
self._k_buffer = self._kv_buffer[0]
self._v_buffer = self._kv_buffer[1]
self._device = device
self._storage_shape = (num_pages * page_size, local_kv_heads, head_dim)
```

逐维解读 `_kv_buffer` 的形状 `(2, num_layers, num_pages, page_size, local_kv_heads, head_dim)`：

| 维度 | 含义 |
|------|------|
| `2` | 第 0 份是所有层的 K，第 1 份是所有层的 V。`_k_buffer = _kv_buffer[0]`、`_v_buffer = _kv_buffer[1]` 就是在这一维切片 |
| `num_layers` | Transformer 层数 |
| `num_pages` | 总页数 |
| `page_size` | 每页 token 数 |
| `local_kv_heads` | **本卡**的 KV 头数（TP 切分后） |
| `head_dim` | 每个头的维度 |

**TP 切分**体现在 `local_kv_heads = div_even(num_kv_heads, tp_info.size, allow_replicate=True)`：把全局 `num_kv_heads` 个头均分给 `tp_info.size` 张卡。`div_even` 的行为见 [utils/misc.py:L20-L26](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/misc.py#L20-L26)：当 `allow_replicate=True` 且「头数少于卡数」（GQA/MQA 常见，例如 8 个 KV head 配 16 张卡）时，允许每张卡复制同一份 KV head（`b % a == 0` 时返回 1）。这保证各卡形状一致，是后续 `all_reduce` 维度对齐的前提。

`_storage_shape = (num_pages * page_size, local_kv_heads, head_dim)` 是把「页×页内 token」两维压平后的视图，`store_kv` 写入时就用它。`_kv_buffer[0]`（K）和 `_kv_buffer[1]`（V）是**同一块显存的视图**，并不额外占显存——`_k_buffer` / `_v_buffer` 只是别名。

读取方法极其轻量，就是切片：

```python
# kvcache/mha_pool.py:L39-L43
def k_cache(self, index: int) -> torch.Tensor:
    return self._k_buffer[index]
def v_cache(self, index: int) -> torch.Tensor:
    return self._v_buffer[index]
```

`k_cache(layer_id)` 返回形状 `(num_pages, page_size, local_kv_heads, head_dim)` 的单层视图，注意力后端再把它 reshape 成 paged kernel 需要的样子。

**dummy page 的小细节**：Engine 调用工厂时传的是 `num_pages + 1`（见 [engine/engine.py:L57-L63](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L57-L63)），多出的那一页专门留给 CUDA Graph 捕获用的 `dummy_req`（见 u5-l3），其 `page_table` 整行指向这个 dummy page，避免污染真实 KV。

#### 4.1.4 代码实践

**目标**：亲手验证「池形状 → 字节量」与 Engine 的显存估算公式一致。

**操作步骤**（纯 CPU 即可，不需要 GPU）：

1. 打开 [kvcache/mha_pool.py:L28-L32](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/mha_pool.py#L28-L32)，确认 `_kv_buffer` 的 6 维形状。
2. 在 Python 里（不需要导入 minisgl）手动构造一个等形张量并算它占多少字节：

```python
import torch
# 假设：8 层、256 页、page_size=1、本卡 8 个 KV head、head_dim=128、bf16
num_layers, num_pages, page_size, local_kv_heads, head_dim = 8, 256, 1, 8, 128
buf = torch.empty((2, num_layers, num_pages, page_size, local_kv_heads, head_dim), dtype=torch.bfloat16)
print("总字节:", buf.numel() * buf.element_size())
# cache_per_page（跨所有层、K+V）：
cache_per_page = 2 * head_dim * local_kv_heads * page_size * buf.element_size() * num_layers
print("每页字节:", cache_per_page, "× 页数:", cache_per_page * num_pages)
```

3. 对照 [engine/engine.py:L150-L157](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L150-L157) 的 `cache_per_page` 公式，确认两者相等。

**需要观察的现象**：`buf.numel() * buf.element_size()` 应当等于 `cache_per_page * num_pages`，即整块缓冲的字节数 = 「每页字节 × 页数」。

**预期结果**：上面的例子中 `2*8*256*1*8*128*2 = 8388608` 字节 = 8 MiB，`cache_per_page = 2*128*8*1*2*8 = 32768`，`32768*256 = 8388608`，两者一致。这验证了「分页缓冲布局」与 Engine 显存估算口径自洽。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_kv_buffer` 的第 0 维是 `2`（K/V 分开），而不是把 K 和 V 拼成 `2*head_dim` 存在最后一维？

**答案**：因为注意力 kernel（FlashAttention/FlashInfer）习惯把 K 和 V 当作两个独立的 `(tokens, heads, head_dim)` 张量传入，分开存储后 `k_cache(layer_id)`、`v_cache(layer_id)` 各取一份即可，无需在 kernel 内部做 `[:head_dim]` / `[head_dim:]` 切片；同时也便于将来 K/V 用不同 dtype 或不同淘汰策略。

**练习 2**：若某模型 `num_kv_heads=8`，分别在 `tp_size=4` 和 `tp_size=16` 下，本卡 `local_kv_heads` 各是多少？

**答案**：`tp_size=4` 时 `8/4=2`，每卡 2 个 KV head；`tp_size=16` 时头数少于卡数，`allow_replicate=True` 且 `16 % 8 == 0`，返回 `1`——每卡复制同一份全部 8 个 head 的 KV（这正是 GQA 在多卡下的常规处理）。

---

### 4.2 store_kv：把新 K/V 散布进池

#### 4.2.1 概念说明

池有了，但注意力每层算出的新 K/V 是「按本批 token 顺序排好的连续张量」，要把它们写进池里**正确的、可能不连续的**槽位。这个「按位置散布写入」就是 `store_kv` 的职责。

这里的关键概念是 `out_loc`：它是一个一维索引张量，长度等于本批要写的 token 总数，每个元素指明「这个 token 的 K/V 应写到池里的第几个绝对槽位」。`out_loc` 由调度器根据 `page_table` 填好（见 [core.py:L78](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L78) 的 `Batch.out_loc` 字段，以及 u4 调度器讲义）。于是 `store_kv` 本质是一次**按索引的 scatter 写**。

#### 4.2.2 核心流程

写入流程：

1. 注意力层在 RoPE 之后得到本批新 token 的 `k`、`v`（形状 `(num_new_tokens, heads, head_dim)`）。
2. 调 `ctx.kv_cache.store_kv(k, v, batch.out_loc, layer_id)`。
3. `store_kv` 内部：
   - 取出该层的 `_k_buffer[layer_id]` 和 `_v_buffer[layer_id]`，按 `_storage_shape` 压平成 `(num_pages*page_size, local_kv_heads, head_dim)`。
   - 调底层 `store_cache` kernel，以 `out_loc` 为索引，把 `k`/`v` 写进压平后的缓冲。
4. 写完后，注意力 kernel 再用同一张 `page_table` 把这些刚写入的 K/V 连同历史 K/V 一起读出来算注意力。

注意：**先写后读**是定序关键——同一批 prefill/extend 的新 token 必须先 `store_kv` 落池，注意力才能正确看到它们（见 [attention/fi.py:L185-L186](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L185-L186)，先 `store_kv` 再 `k_cache/v_cache` 读）。

#### 4.2.3 源码精读

[`MHAKVCache.store_kv`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/mha_pool.py#L45-L56) 很短：

```python
# kvcache/mha_pool.py:L45-L56
def store_kv(self, k, v, out_loc, layer_id):
    from minisgl.kernel import store_cache
    store_cache(
        k_cache=self._k_buffer[layer_id].view(self._storage_shape),
        v_cache=self._v_buffer[layer_id].view(self._storage_shape),
        indices=out_loc, k=k, v=v,
    )
```

两个要点：

- **按层取视图再压平**：`self._k_buffer[layer_id]` 形状是 `(num_pages, page_size, local_kv_heads, head_dim)`，`.view(self._storage_shape)` 把前两维合并成 `num_pages * page_size`，得到 `(total_slots, local_kv_heads, head_dim)`。这样 `out_loc` 里的整数就能直接当「绝对 token 槽位号」用。
- **延迟导入 `store_cache`**：`from minisgl.kernel import store_cache` 写在函数体内，避免在模块加载时触发 kernel JIT 编译（kernel 依赖 tvm-ffi，编译较重）。

底层 [`store_cache`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/store.py#L30-L42) 是一个按 `element_size` 特化的 JIT CUDA kernel：

```python
# kernel/store.py:L37-L42
num_tokens = k_cache.shape[0]
k_cache = k_cache.view(num_tokens, -1)
v_cache = v_cache.view(num_tokens, -1)
element_size = k_cache.shape[1] * k_cache.element_size()
module = _jit_store_module(element_size)
module.launch(k_cache, v_cache, indices, k, v)
```

`element_size` 是「单个 token 的 K（或 V）一行占多少字节」=`local_kv_heads * head_dim * itemsize`。kernel 按它编译出对应向量化拷贝宽度的特化版本（`_jit_store_module` 带 `@functools.cache`，同一 `element_size` 只编译一次），然后 `module.launch` 真正执行 scatter 写。这是一个典型的「把每个 token 的 K/V 当作一段连续字节整体搬运」的 kernel。

#### 4.2.4 代码实践

**目标**：理解 `out_loc` 如何决定写入位置，跟踪一次写入的调用链。

**操作步骤**（源码阅读型实践，无需 GPU）：

1. 打开 [attention/fi.py:L185](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L185)，确认调用顺序是「先 `store_kv(... batch.out_loc ...)`，再 `k_cache/v_cache` 读取」。（[attention/fa.py:L53](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fa.py#L53) 与 [attention/trtllm.py:L57](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/trtllm.py#L57) 是同样模式。）
2. 用 Grep/搜索在 `python/minisgl/scheduler/` 下找到 `out_loc` 的填充处（提示：它由调度器根据 `page_table` 与每请求的 `cached_len/device_len` 构造）。
3. 用 NumPy 在 CPU 上模拟一次 scatter 写，直观感受 `out_loc` 的作用：

```python
import numpy as np
total_slots = 8
heads, head_dim = 2, 3
pool = np.zeros((total_slots, heads, head_dim), dtype=np.float32)
# 本批 3 个新 token 的 K，以及它们要写到池里的槽位号
k = np.arange(3*heads*head_dim, dtype=np.float32).reshape(3, heads, head_dim)
out_loc = np.array([5, 0, 5+1])  # 注意：槽位可以乱序、可不连续
pool[out_loc] = k
print(pool[5], "(槽位5被写入)"); print(pool[0], "(槽位0被写入)")
```

**需要观察的现象**：`pool[out_loc] = k` 会把 `k[0]` 写进槽位 5、`k[1]` 写进槽位 0、`k[2]` 写进槽位 6，证明写入顺序由 `out_loc` 决定、与 token 在批次里的顺序无关。

**预期结果**：槽位 5 的内容等于 `k[0]`，槽位 0 等于 `k[1]`。这就是 `store_kv` 的语义在 CPU 上的等价物；真实 kernel 只是把它换成了按 `element_size` 特化的高效 GPU scatter。

#### 4.2.5 小练习与答案

**练习 1**：`store_kv` 为什么按 `layer_id` 逐层调用，而不是一次性把所有层写完？

**答案**：因为模型前向本身就是逐层推进的——第 \(l\) 层算出自己的 K/V 时，下一层还没算。`store_kv` 在每层 `forward` 内部被调用，写入该层对应的 `_k_buffer[layer_id]` / `_v_buffer[layer_id]`。逐层写入天然贴合前向的执行顺序。

**练习 2**：若 `out_loc` 里出现重复槽位号会发生什么？这是 bug 吗？

**答案**：后写的会覆盖先写的（scatter 语义）。在同一批内出现重复槽位通常是 bug（同一 token 槽位被赋两次值），但跨层不会冲突，因为每一层是独立的 `_k_buffer[layer_id]`。调度器负责保证同层同批的 `out_loc` 不重复。

---

### 4.3 BasePrefixCache：前缀缓存接口契约

#### 4.3.1 概念说明

`MHAKVCache` 只是「裸存储」——它知道有哪些槽位，但不知道「槽位 3~7 现在存的是请求 A 和请求 B 共享的 system prompt 前缀，可以复用」。

`BasePrefixCache` 这层抽象回答的是一个**资源管理**问题：在有限的 `num_pages` 里，哪些页是「空闲的」、哪些页「存了某段 token 前缀、可以被新请求命中复用」、哪些被命中后「正在被使用、不能淘汰」。它把「按 token 序列前缀做匹配、插入、淘汰」的能力抽象成六个方法，与具体数据结构（基数树、哈希表……）解耦。

这一层对调度器（`CacheManager`）至关重要：调度器在 prefill 前先 `match_prefix` 看能复用多少、prefill 后 `insert_prefix` 把新前缀登记进缓存、显存吃紧时 `evict` 回收。换不同实现（`naive` / `radix`）只需改 `--cache` 一个参数，调度器代码不动。

#### 4.3.2 核心流程

一个请求经过前缀缓存的典型生命周期（配合 [scheduler/cache.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py) 的 `CacheManager`）：

```
请求到达
  │
  ▼
match_prefix(input_ids)  ──→  MatchResult(handle)   # 读：能复用多少前缀？返回 handle
  │                              │
  │  handle.get_matched_indices() 得到可复用的页下标
  ▼
lock_handle(handle)  ──────────── # 锁住：这些页在我用的时候不能被淘汰
  │
  ▼
分配新页 → prefill → store_kv 把新 K/V 写入
  │
  ▼
insert_prefix(input_ids, 新页indices) ──→ InsertResult(cached_len, new_handle)  # 写：登记新前缀
  │
  ▼
unlock(old_handle) / lock(new_handle)   # 换锁：旧前缀解锁，新前缀锁住
  │
  ▼ （显存不够时）
evict(needed_size) ──→ 被淘汰的页下标      # 回收：把没人锁的旧前缀赶走
```

接口把「读（match）」「写（insert）」「回收（evict）」「并发保护（lock/unlock）」「自检（check_integrity）」清楚分开，每个方法的副作用（是否改缓存）都在文档里写明。

#### 4.3.3 源码精读

先看四个配套数据类型，它们是接口的「返回值契约」：

[`BaseCacheHandle`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/base.py#L40-L45) 是一个「句柄」，核心是 `cached_len`（这段前缀在缓存里有多少 token）和 `get_matched_indices()`（返回这些 token 对应的页下标）：

```python
# kvcache/base.py:L40-L45
@dataclass(frozen=True)
class BaseCacheHandle(ABC):
    cached_len: int
    @abstractmethod
    def get_matched_indices(self) -> torch.Tensor: ...
```

[`SizeInfo`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/base.py#L48-L54) 描述缓存的「容量占用」：`evictable_size` 是「可被淘汰的 token 数」、`protected_size` 是「被锁住、不可淘汰的 token 数」，`total_size` 是两者之和。`CacheManager.available_size` 就靠它算还能分配多少（见 [scheduler/cache.py:L32-L34](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L32-L34)）。

```python
# kvcache/base.py:L48-L54
class SizeInfo(NamedTuple):
    evictable_size: int
    protected_size: int
    @property
    def total_size(self) -> int:
        return self.evictable_size + self.protected_size
```

[`InsertResult`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/base.py#L57-L59) 与 [`MatchResult`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/base.py#L62-L64) 分别是 `insert_prefix` / `match_prefix` 的返回值。注意 `InsertResult.cached_len` 的语义很微妙——「插入**之前**这段前缀已经在缓存里的长度」，调用方据此知道哪些页是这次新插入、需要后续管理（见 u6-l3 `cache_req`）。

再看 [`BasePrefixCache`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/base.py#L67-L136) 的六个抽象方法，每个都有详细 docstring 声明副作用。三个最关键的：

```python
# kvcache/base.py:L82-L93 （match_prefix：只读）
def match_prefix(self, input_ids: torch.Tensor) -> MatchResult:
    """Match prefix and return the indices of the matched prefix in the cache.
    This operation will not modify the cache.
    The returned indices is only safe to use when the handle is locked."""
```

隐形契约：**返回的 indices 只有在 handle 被 lock 之后才安全使用**——否则别的请求触发 `evict` 可能把这些页回收掉，你拿到的下标就指向被覆盖的数据。

```python
# kvcache/base.py:L108-L122 （evict：会改缓存，可能抛错）
def evict(self, size: int) -> torch.Tensor:
    """Evict some prefixes from the cache to free up space. This operation will modify the cache.
    Note that evict 0 is always safe and does nothing.
    Note that the actual evict size may be larger than the requested size.
    ...
    Raises:
        RuntimeError: If the requested size is larger than the evictable size."""
```

两条关键契约：`evict(0)` 永远安全（空操作）；实际淘汰量可能**大于**请求量（因为按页对齐，必须整页整页地赶）。

```python
# kvcache/base.py:L68-L80 （lock_handle：不改缓存内容，只改 SizeInfo）
def lock_handle(self, handle, unlock: bool = False) -> None:
    """... This operation will not modify the cache, but change the size info only.
    When a handle is locked, it cannot be evicted.
    Handles must be locked before the previously-returned tensor of `match_prefix` is used."""
```

`lock` 的本质是把一段前缀从 `evictable_size` 挪到 `protected_size`，让 `evict` 跳过它。这解释了为何 `SizeInfo` 要分这两个桶。

最后 [`check_integrity`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/base.py#L133-L135) 是一个自检钩子，`CacheManager.check_integrity`（[scheduler/cache.py:L81-L91](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L81-L91)）会用它断言「空闲页 + 缓存页 == 总页数」这个守恒不变量。

#### 4.3.4 代码实践

**目标**：把六个方法的「副作用」与「契约」整理成一张表，并对照 `CacheManager` 验证调用顺序符合契约。

**操作步骤**：

1. 通读 [kvcache/base.py:L67-L136](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/base.py#L67-L136) 的全部 docstring。
2. 填写下面这张「契约表」：

| 方法 | 是否改缓存 | 是否改 SizeInfo | 可能抛错 | 关键契约 |
|------|-----------|----------------|---------|---------|
| `match_prefix` |  |  |  | 返回值须 lock 后才安全用 |
| `insert_prefix` |  |  |  | 返回的 `cached_len` 是插入前已缓存长度 |
| `evict` |  |  | size>可淘汰量时 | `evict(0)` 安全；实际淘汰量≥请求量 |
| `lock_handle` |  |  |  | 只动 SizeInfo，不碰缓存内容 |
| `size_info` | 否 | — | 否 | 暴露 evictable/protected 两桶 |
| `check_integrity` | 否 | 否 | 缓存损坏时 | 自检，不改状态 |

3. 打开 [scheduler/cache.py:L55-L79](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L55-L79) 的 `cache_req`，核对其调用顺序确实是 `insert_prefix → unlock(old) → lock(new)`，与契约吻合。

**需要观察的现象**：`CacheManager` 在拿到 `match_prefix` 的 handle 后，**总是先 `lock` 再用 indices**（见 [scheduler/cache.py:L36-L40](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L36-L40) 的 `lock`/`unlock` 转发），严格遵守 docstring 的契约。

**预期结果**：契约表填好后，你会看到「读/写/回收」与「改缓存/改 SizeInfo」是两套正交的分类——这正是接口设计干净的地方。

#### 4.3.5 小练习与答案

**练习 1**：`evict(0)` 为什么被特意声明「永远安全、什么也不做」？

**答案**：因为 `CacheManager._allocate` 在「需要的页数 > 空闲页数」时才调 `evict((needed - free) * page_size)`；当差额为 0（即刚好够或算出 0）时，调一个安全的 `evict(0)` 比加 `if` 分支更简单，也避免实现方忘记处理「请求淘汰 0」的边界。

**练习 2**：`SizeInfo` 为什么要分 `evictable_size` 和 `protected_size` 两个桶，而不是只给一个 `total_size`？

**答案**：因为 `CacheManager.available_size = evictable_size + 空闲页`（[scheduler/cache.py:L32-L34](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L32-L34)）——只有 `evictable` 那部分能被 `evict` 回收再分配，`protected`（被锁、正在用）的部分不能动。若只有一个总和，调度器就无法判断「还能挤出多少页」，会导致把正在用的前缀淘汰掉、破坏正确性。

---

### 4.4 NaivePrefixCache：无前缀复用的基线实现

#### 4.4.1 概念说明

`NaivePrefixCache` 是 `BasePrefixCache` 的一个「合法但啥也不干」的实现。它满足接口的全部契约，但：

- `match_prefix` 永远返回「命中 0 个 token」（空前缀）；
- `insert_prefix` 永远返回 `cached_len=0` 且不真正登记任何前缀；
- `evict(size>0)` 直接抛 `NotImplementedError`；
- `size_info` 永远是 `(0, 0)`——缓存里什么都没有。

换句话说，它**彻底禁用了前缀复用**：每个请求都从零 prefill，谁也不复用谁的 KV。它的价值在于：

1. **基线对照**：做 benchmark 时，用 `--cache naive` 跑一遍，能测出 Radix Cache 到底带来了多少收益（这是工程上验证优化有效性的标准做法）。
2. **调试**：怀疑 bug 出在前缀缓存逻辑时，切到 naive 可以快速隔离问题。
3. **最小正确路径**：它证明这套接口的最小实现可以非常简单，是理解接口契约的最佳「参考样本」。

它被注册为 `"naive"` 类型，与 `"radix"` 并列（见 [kvcache/__init__.py:L47-L58](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/__init__.py#L47-L58)），由 `--cache` CLI 参数选择。

#### 4.4.2 核心流程

`NaivePrefixCache` 的「退化」逻辑可以这样理解（对照 4.3.2 的完整生命周期）：

```
match_prefix ──→ 永远 MatchResult(handle{cached_len=0, indices=空})   # 不命中任何前缀
insert_prefix ──→ 永远 InsertResult(cached_len=0, dummy handle)        # 不登记
lock_handle   ──→ pass                                                 # 没东西可锁
size_info     ──→ (0, 0)                                               # 缓存里 0 token
evict(size)   ──→ size==0 返回空；size>0 抛 NotImplementedError          # 没东西可淘汰
```

把它代入 `CacheManager` 后，系统行为退化为「所有页都从 `free_slots` 分配、用完直接 `_free` 回收，永不在缓存里留存可复用前缀」。`CacheManager.available_size` 此时恒等于 `len(free_slots) * page_size`（因为 `evictable_size=0`），逻辑仍然自洽。

#### 4.4.3 源码精读

先看配套的 [`NaiveCacheHandle`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/naive_cache.py#L6-L13)：

```python
# kvcache/naive_cache.py:L6-L13
class NaiveCacheHandle(BaseCacheHandle):
    empty_tensor: torch.Tensor  # should be set by NaivePrefixCache
    def __init__(self):
        super().__init__(cached_len=0)
    def get_matched_indices(self) -> torch.Tensor:
        return self.empty_tensor
```

它把 `cached_len` 写死为 0，`get_matched_indices()` 返回一个**共享的空张量** `empty_tensor`。这个空张量由 `NaivePrefixCache.__init__` 创建并挂到类属性上（见下），所有 handle 共用同一份，省去重复分配。

再看 [`NaivePrefixCache`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/naive_cache.py#L16-L45)：

```python
# kvcache/naive_cache.py:L17-L21
def __init__(self, device: torch.device):
    self.device = device
    self.empty_tensor = torch.empty(0, dtype=torch.int32, device=device)
    NaiveCacheHandle.empty_tensor = self.empty_tensor   # 把空张量挂给 handle 类
    super().__init__()
```

注意 `empty_tensor = torch.empty(0, ...)`——长度为 0 的张量，dtype 是 `int32`（与 `page_table` 一致，因为存的是页下标）。把它挂到 `NaiveCacheHandle` 类属性，是为了让 handle 实例无需各自持有一份。

四个核心方法的「退化」实现：

```python
# kvcache/naive_cache.py:L26-L35
def match_prefix(self, input_ids) -> MatchResult:
    return MatchResult(NaiveCacheHandle())        # 命中 0 个 token

def insert_prefix(self, input_ids, indices) -> InsertResult:
    return InsertResult(0, NaiveCacheHandle())     # cached_len=0，不登记

def evict(self, size: int) -> torch.Tensor:
    if size == 0:
        return self.empty_tensor                   # evict(0) 安全：契约要求
    raise NotImplementedError("NaiveCacheManager does not support eviction.")
```

三个关键点：

- `match_prefix` 无视 `input_ids`，永远返回一个 `cached_len=0` 的 handle——`get_matched_indices()` 给出空张量，意味着「这段 prompt 没有任何前缀命中，请从第 0 个 token 开始算」。
- `insert_prefix` 同样无视入参，返回 `cached_len=0`——按 `InsertResult` 的语义，「插入前这段前缀在缓存里的长度是 0」，且没有产生需要管理的新缓存项。`CacheManager.cache_req` 据此知道「没有新前缀需要 lock」（`new_handle` 是 dummy）。
- `evict(0)` 返回空张量（严格履行「evict 0 永远安全」的契约），`evict(size>0)` 抛错——因为缓存里压根没有可淘汰的东西。这是个**诚实**的实现：与其假装淘汰了一堆假数据，不如直接告诉调用方「我不支持」。

`size_info` 恒为 `(0, 0)`：

```python
# kvcache/naive_cache.py:L40-L42
@property
def size_info(self) -> SizeInfo:
    return SizeInfo(evictable_size=0, protected_size=0)
```

这保证 `CacheManager.check_integrity` 里的守恒式 `len(free_slots) + 0 == num_pages` 始终成立（因为所有页都待在 `free_slots` 里，从没人把它们登记进缓存）。

#### 4.4.4 代码实践

**目标**：亲手验证 `NaivePrefixCache` 是一个「合法的空实现」，并解释它如何作为无前缀复用的基线。

**操作步骤**（CPU 即可，约 3 分钟）：

1. 在项目根目录起一个 Python（需能 `import minisgl`，或直接把下面的逻辑抄到任意脚本里手动验证）：

```python
import torch
from minisgl.kvcache.naive_cache import NaivePrefixCache

cache = NaivePrefixCache(device=torch.device("cpu"))

# (a) match_prefix 对任意输入都返回 cached_len=0、空 indices
h = cache.match_prefix(torch.tensor([1,2,3,4], dtype=torch.int32)).cuda_handle
print("matched cached_len:", h.cached_len, "indices:", h.get_matched_indices().tolist())

# (b) insert_prefix 返回 cached_len=0，不真正登记
r = cache.insert_prefix(torch.tensor([1,2,3]), torch.tensor([10,11,12]))
print("insert cached_len:", r.cached_len)
print("size_info:", cache.size_info)   # 仍是 (0, 0)

# (c) evict(0) 安全，evict(>0) 抛错
print("evict(0):", cache.evict(0).tolist())
try:
    cache.evict(100)
except NotImplementedError as e:
    print("evict(100) ->", e)
```

2. 对照 [scheduler/cache.py:L32-L34](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L32-L34) 与 [scheduler/cache.py:L81-L91](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L81-L91)：确认 `available_size = 0 + len(free_slots)*page_size`、`check_integrity` 的守恒式在 naive 下天然成立。

**需要观察的现象**：`match_prefix` 的 `cached_len` 恒为 0、`indices` 为空列表；`insert_prefix` 后 `size_info` 仍是 `(evictable_size=0, protected_size=0)`；`evict(0)` 返回空、`evict(100)` 抛 `NotImplementedError`。

**预期结果**：以上输出与代码注释完全一致。结论：`NaivePrefixCache` 满足 `BasePrefixCache` 的全部契约（match 只读且返回空、insert 返回 cached_len=0、evict(0) 安全、size_info 反映真实占用），因此是一个合法实现；它通过「让 match 永不命中、让 size_info 恒为 0」从机制上关闭了前缀复用，于是 `CacheManager` 退化成「纯页分配器」，可作为衡量 Radix Cache 收益的基线。若你在无 GPU/无完整 minisgl 环境下运行，可把上述逻辑改写为纯 NumPy/手写类验证，结论一致（标记：若 import 失败则为「待本地验证」）。

#### 4.4.5 小练习与答案

**练习 1**：既然 `NaivePrefixCache` 什么前缀都不缓存，为什么 `CacheManager._allocate` 在空闲页耗尽时不会因为 `evict` 抛错而崩溃？

**答案**：因为 naive 下没有任何前缀被 `insert_prefix` 真正登记，所有页始终在 `free_slots` 与「正在被某请求占用」之间流转；只要总工作量不超过 `num_pages`，空闲页就不会真的耗尽到需要淘汰缓存页。一旦真耗尽（工作量超容量），那属于真实的显存不足，此时抛 `NotImplementedError`（或上层 OOM 断言）反而是诚实的失败。换言之，naive 把「无前缀复用」做到极致，淘汰路径在正常负载下根本不会触发。

**练习 2**：如果想把 `--cache` 默认值从 `radix` 改成 `naive` 来做一次基线 benchmark，最少要改哪里？

**答案**：不用改源码——`--cache` 是 CLI 参数（见 u2-l2 配置体系）。启动时加 `--cache naive` 即可，`create_prefix_cache(device, "naive")`（[kvcache/__init__.py:L61-L62](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/__init__.py#L61-L62)）会从注册表取到 `NaivePrefixCache`。这正是注册表 + 工厂模式带来的可插拔好处。

---

## 5. 综合实践

把本讲的「池存储」与「前缀缓存接口」串起来，完成下面这个贯穿任务：

**任务**：为一个假想的「最小前缀缓存」实现绘制契约对照表，并判断它是否合法。

假设有人写了这样一个 `ToyPrefixCache`（伪代码，**示例代码**，非项目原有）：

```python
class ToyPrefixCache(BasePrefixCache):
    def match_prefix(self, input_ids):
        return MatchResult(SomeHandle(cached_len=5, indices=torch.tensor([0,1,2,3,4])))
    def insert_prefix(self, input_ids, indices):
        return InsertResult(0, SomeHandle(...))
    def evict(self, size):
        return torch.empty(0, dtype=torch.int32)   # 无论 size 多大都返回空
    def lock_handle(self, handle, unlock=False): pass
    def size_info(self):
        return SizeInfo(evictable_size=0, protected_size=0)
    def reset(self): pass
    def check_integrity(self): pass
```

请回答：

1. 它是否违反了 `match_prefix` 的「只读」契约？（提示：没有，它不改缓存。）
2. 它是否违反了 `evict` 的契约？（提示：违反——`size>0` 时它既不抛错（docstring 说 size>evictable 时应 `RuntimeError`）、又返回空（实际淘汰量理应 ≥ 请求量），调用方会以为淘汰成功而其实没有，导致 `CacheManager._allocate` 的 `assert len(free_slots) >= needed_pages` 失败。）
3. 它的 `size_info` 与 `match_prefix` 之间是否自洽？（提示：不自洽——match 说命中了 5 个 token，但 size_info 说缓存里 0 个 token，`available_size` 会算错。）

**完成标志**：你能用本讲学到的六条契约，逐条指出这个玩具实现「看似实现了接口、实则破坏了守恒不变量」的具体位置。这正好引出下一讲 RadixPrefixCache 必须小心翼翼维护的 `size_info` 与 `check_integrity`。

## 6. 本讲小结

- **池是裸存储**：`MHAKVCache` 用一个 `(2, num_layers, num_pages, page_size, local_kv_heads, head_dim)` 的大张量一次性持有所有层、所有页的 K/V，第 0 维区分 K/V，`local_kv_heads` 已按 TP 切分（GQA 下允许复制）。
- **写入靠 scatter**：`store_kv` 把每层新算的 K/V 按 `out_loc` 散布进压平后的 `_storage_shape` 视图，底层是按 `element_size` 特化的 JIT CUDA kernel `store_cache`；定序上必须先 `store_kv` 再读取。
- **接口把职责正交分解**：`BasePrefixCache` 的六方法按「读/写/回收」与「改缓存/改 SizeInfo」两套正交维度划分，每条副作用与异常都在 docstring 里写明，`SizeInfo` 的 evictable/protected 两桶支撑 `available_size` 与淘汰正确性。
- **handle 必须先锁后用**：`match_prefix` 返回的 indices 只有 `lock_handle` 之后才安全，否则可能被并发 `evict` 覆盖。
- **NaivePrefixCache 是合法的空实现**：通过「match 永不命中、size_info 恒为 0、evict(>0) 抛错」彻底关闭前缀复用，满足全部契约，作为基线对照与调试隔离的工具，并印证了接口的最小实现可以极其简单。
- **工厂 + 注册表实现可插拔**：`create_kvcache_pool` / `create_prefix_cache` 配合 `SUPPORTED_CACHE_MANAGER` 注册表，让 `--cache naive|radix` 一个参数就能切换前缀缓存策略，调度器代码不动。

## 7. 下一步学习建议

本讲建立了「池存储」与「前缀缓存接口」两块地基，接下来：

- **u6-l2 Radix Cache 实现**：进入 `RadixPrefixCache`，看它如何用基数树（Radix Tree）真正实现 `match_prefix`/`insert_prefix`/`evict`——共享前缀如何被节点表示、`split_at` 如何分裂、最小堆如何按 timestamp 做 LRU 淘汰。这是对本讲抽象的第一个「真」实现。
- **u6-l3 CacheManager 页分配、回收与淘汰**：从调度器侧看 `CacheManager` 如何消费本讲的接口——`free_slots` 页分配、`allocate_paged` 写 `page_table`、`cache_req` 把结果插回前缀缓存并区分 finished 释放尾部、`lazy_free_region` 延迟回收。
- **如果想先横向验证**：阅读 [tests/core/test_cache_allocate.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/core/test_cache_allocate.py)，看测试如何用 `CacheManager(type="radix")` 在 CPU 上验证「分配—插入—淘汰」循环的页对齐与守恒不变量，那正是 u6-l3 的内容。
