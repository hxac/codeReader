# Cascade 共享前缀注意力

## 1. 本讲目标

在真实的 LLM 推理服务里，大量请求往往共享同一段前缀——例如同一个 system prompt、同一段 few-shot 示例、或同一份长文档的引用。如果每个请求都把这段共享前缀的 KV-Cache 各存一份、各算一遍，既浪费显存，又浪费带宽。

学完本讲，你应该能够：

- 说清「共享前缀」带来的显存与带宽收益，以及为什么收益不是免费的。
- 读懂 `MultiLevelCascadeAttentionWrapper`（以及被它取代的两个 shared-prefix wrapper）的 `plan`/`run` 全流程，理解多层 cascade 如何把 KV-Cache 拆成「共享级 + 独立级」。
- 掌握 `merge_state` / `merge_state_in_place` / `merge_states` 三个合并函数，并用 logsumexp 的数学原理解释「分段算再合并」为什么等价于「整体算」。
- 自己构造一个共享 system prompt 的场景，用 cascade wrapper 算一遍，再与普通 attention 对拍验证结果一致。

本讲承接 [u3-l4 BatchPrefillWithPagedKVCacheWrapper 的 plan/run](u3-l4-batch-prefill-wrapper.md)：cascade wrapper 本质上是「把多个 prefill wrapper 叠起来 + 一个合并 kernel」，所以你必须先熟悉 prefill wrapper 的 `plan`/`run` 与 `return_lse` 机制。

## 2. 前置知识

在进入源码前，先用三段话把直觉建立起来。

**(1) 注意力的「状态」可以拆开再合并。** 对同一个 query \(q\)，把它的 KV 序列切成两段 \(A\) 与 \(B\)。如果我们分别对两段做注意力，会得到两个「部分输出」\(O_A, O_B\) 和两个标量「对数求和指数」(logsumexp, 简称 LSE) \(l_A, l_B\)。神奇的是，只要保留这两个 LSE，就能把 \(O_A, O_B\) 重新合并成「对整段 \([A;B]\) 做注意力」的精确结果——不需要重新读 K/V。这条性质是 cascade 一切设计的数学根基，本讲 4.3 会给出完整推导。

**(2) 共享前缀 = 一种特殊的「KV 分段」。** 如果一批请求的 KV 都是「共享前缀 \(S\) + 各自独立后缀 \(U_i\)」，那么对每个请求 \(i\)，它的注意力就天然分成两段：\(S\)（所有请求相同）和 \(U_i\)（每请求不同）。于是可以：对 \(S\) 只算一次（所有请求共享这次计算和这块显存），对每个 \(U_i\) 各算一次，最后合并。这就是 cascade 注意力省显存与省算力的来源。

**(3) cascade wrapper = 「N 个 prefill wrapper 的 plan」 + 「N-1 次合并」。** FlashInfer 把上面的想法做成一个通用的「多级」结构：`MultiLevelCascadeAttentionWrapper` 内部持有一组 `BatchPrefillWithPagedKVCacheWrapper`，每一级对应一层 KV（最顶上是全局共享的，越往下越「每请求独立」），`run` 时先算最底层、拿到 `(out, lse)`，再把上面各级的结果逐层用 `merge_state_in_place` 合并进去。

> 术语提示：本讲反复出现 **LSE / logsumexp**、**normalized output（归一化输出）**、**paged KV-Cache 页表三件套**（`paged_kv_indptr` / `paged_kv_indices` / `paged_kv_last_page_len`）。后三者已在 [u3-l2 Paged KV Cache 布局与 append](u3-l2-paged-kv-layout-append.md) 讲过，这里直接使用。

## 3. 本讲源码地图

本讲涉及的关键文件，按「从上到下」的调用层次排列：

| 文件 | 作用 |
|------|------|
| [flashinfer/cascade.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cascade.py) | 用户直接调用的 Python 层：三个 `merge_state*` 函数 + 三个 cascade wrapper 类。 |
| [flashinfer/jit/cascade.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cascade.py) | JIT 代码生成器 `gen_cascade_module`，把两个 `.cu` 装配成一个 `JitSpec`。 |
| [csrc/cascade.cu](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/cascade.cu) | TVM-FFI launcher：张量校验、dtype 派发、调用 kernel 模板。 |
| [csrc/flashinfer_cascade_binding.cu](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/flashinfer_cascade_binding.cu) | 用 `TVM_FFI_DLL_EXPORT_TYPED_FUNC` 把三个 launcher 导出成跨语言符号。 |
| [include/flashinfer/attention/cascade.cuh](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/cascade.cuh) | header-only kernel 模板：`MergeStateKernel` / `MergeStateInPlaceKernel` / `MergeStatesKernel` 等。 |
| [include/flashinfer/attention/state.cuh](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/state.cuh) | `state_t` 结构体：在线 softmax 的 `(m, d, o)` 状态及其 `merge`，是合并数学的源头。 |

记忆口诀：`cascade.py`（用户 API）→ `jit/cascade.py`（生成）→ `csrc/cascade.cu`（launcher）→ `flashinfer_cascade_binding.cu`（FFI 导出）→ `include/.../cascade.cuh` + `state.cuh`（kernel 与数学）。这条链路与 [u3-l4](u3-l4-batch-prefill-wrapper.md) 讲过的「四层调用栈」完全一致。

## 4. 核心概念与源码讲解

### 4.1 共享前缀

#### 4.1.1 概念说明

考虑一个典型的服务场景：一个推理批次里有 7 个请求，它们都以同一段 8192 token 的 system prompt 开头，再各自接上几十到上百 token 的「独立对话内容」。朴素做法是把每个请求的 KV-Cache 存成 `[max_len, ...]`，于是那 8192 token 的 KV 被复制了 7 份。

共享前缀（shared prefix）机制把每个请求的 KV 拆成两段：

- **共享段** \(S\)：所有请求完全相同，全局只存一份、只算一次。
- **独立段** \(U_i\)：每请求不同，各自存储、各自计算。

收益有两个层面：

1. **显存**：8192 token 的 KV 从「7 份」降到「1 份」。在 GQA、大 head_dim、深层数下，这是一笔极大的节省。
2. **带宽/算力**：decode 阶段是 memory-bound，对共享段只读一次 K/V，对所有请求复用，相当于把共享部分的访存量摊薄到 1/N。

但收益不是免费的：把两段结果合并成最终输出需要额外的 `merge_state` kernel 开销，所以「级数越多越好」并不成立——源码注释明确提醒 *"it's not always beneficial to increase the number of levels because of the overhead of merging attention results."*（见 [flashinfer/cascade.py:226-234](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cascade.py#L226-L234)）。

#### 4.1.2 核心流程

把「共享前缀注意力」抽象成伪代码：

```text
输入: batch 个请求，每个请求的 KV = S (共享) ++ U_i (独立)
1. 对共享段 S 做一次 batch-attention（所有请求一起，因为共享）→ (O_shared, LSE_shared)
2. 对每个独立段 U_i 做各自的 batch-attention → (O_unique, LSE_unique)
3. 用 merge_state_in_place 把 (O_shared, LSE_shared) 合并进 (O_unique, LSE_unique)
4. 返回合并后的 O
```

关键点：第 1 步只执行一次，且共享段只占一份显存；第 3 步是纯 element-wise 的轻量 kernel，不读 K/V。只要合并数学正确（4.3 节证明），最终结果与「把 \(S ++ U_i\) 拼起来整体算一次 attention」逐比特等价。

#### 4.1.3 源码精读

最直观的「共享 + 独立」两段实现是（已标记为 deprecated，但最易读懂）`BatchDecodeWithSharedPrefixPagedKVCacheWrapper.forward`：

[flashinfer/cascade.py:749-L811](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cascade.py#L749-L811) —— 这段 `forward` 完整对应上面的伪代码：先用 `single_prefill_with_kv_cache` 把共享前缀 `k_shared/v_shared` 算成 `(V_shared, S_shared)`，再用底层的 decode wrapper 把独立 paged KV 算成 `(V_unique, S_unique)`，最后用 `merge_state_in_place` 合并。

注意它把两段都要求 `return_lse=True`：共享段调用 `single_prefill_with_kv_cache(..., return_lse=True)`，独立段调用 `self._batch_decode_wrapper.forward_return_lse(...)`。**没有 LSE 就无法合并**——这正是 4.1.1 所说「收益不是免费」的核心约束：每个分段 attention 都必须额外吐出 LSE。

另一个要点：共享段的 `causal=False`。共享前缀是「已完成的过去」，对所有 query 都完全可见，不存在因果掩码；因果掩码（如果需要）只作用于独立段（见 [flashinfer/cascade.py:793-809](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cascade.py#L793-L809) 中 `causal=False` 与后续 prefill 版本的 `causal=causal` 对比）。

#### 4.1.4 代码实践（源码阅读 + 手算对拍）

**目标**：用纸笔验证「分段算 + LSE 合并 = 整体算」，建立对 4.3 数学结论的信心。

**步骤**：

1. 打开 [tests/attention/test_shared_prefix_kernels.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/attention/test_shared_prefix_kernels.py)，阅读 `test_batch_attention_with_shared_prefix_paged_kv_cache`，看它如何把同一组 K/V 同时塞进「共享 paged 表 + 独立 paged 表」并构造参考结果。
2. 自己构造一个极小例子（`head_dim=8, num_heads=1, seq_len=4`），手写两个 token 的「共享段」和两个 token 的「独立段」：
   - 用 PyTorch 对 `[S; U]` 拼接后整体算一次 attention（标准 softmax）得到 \(O_{\text{full}}\)；
   - 分别对 `S` 和 `U` 算 attention，得到 \((O_S, l_S), (O_U, l_U)\)，再用本讲 4.3.2 的合并公式手算 \(O_{\text{merged}}\)；
   - 比较 \(O_{\text{full}}\) 与 \(O_{\text{merged}}\)。

**需要观察的现象**：两者在每个元素上都一致（允许 float32 舍入误差）。

**预期结果**：在 float32 下两者最大绝对差应在 \(10^{-6}\) 量级。

> 待本地验证：本实践未实际运行，结果为「按公式推导的预期」。如果你在 GPU 上用 fp16 跑，误差会大一些（fp16 的 LSE 精度有限），但仍应与直接拼接算的 fp16 结果接近。

#### 4.1.5 小练习与答案

**练习 1**：如果把共享段和独立段的 attention 都设成 `return_lse=False`，cascade 还能正确工作吗？为什么？

> **答案**：不能。合并两个部分输出必须知道各自的 LSE 才能恢复正确的 softmax 归一化（4.3 节）。没有 LSE，你只有两个已归一化的 \(O_A, O_B\)，无法知道哪段「权重更大」。

**练习 2**：共享前缀在 decode 阶段（memory-bound）和在 prefill 阶段（compute-bound）哪个收益更大？为什么？

> **答案**：decode 收益通常更大。decode 的瓶颈是读 KV 的带宽，共享段只读一次能直接减少访存量；prefill 是 compute-bound，共享段的计算量节省更显著，但合并 kernel 的相对开销也更显眼，需实测权衡。

---

### 4.2 cascade wrapper

#### 4.2.1 概念说明

`MultiLevelCascadeAttentionWrapper` 把「共享前缀」推广为「多级 cascade」：所有级的 KV-Cache 都存在**同一个统一的 paged 表**里，但用「级」来描述「哪些页被哪些请求共享」。

- **第 0 级（最顶层）**：全局共享的 KV，所有请求都看这一段。
- **中间级**：按需划分的共享粒度。
- **最后一级（最底层）**：每请求独立的 KV 后缀。

级数 `num_levels` 由用户指定。最常见的 2 级就是「system prompt（共享）+ 对话历史（独立）」。源码用一句话点题：*"this API assumes all levels KV-Cache are stored in a unified paged table"*（[flashinfer/cascade.py:227-229](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cascade.py#L227-L229)）。

> 历史包袱：仓库里还有两个更老、更窄的 wrapper——`BatchDecodeWithSharedPrefixPagedKVCacheWrapper` 与 `BatchPrefillWithSharedPrefixPagedKVCacheWrapper`，它们只支持「恰好 2 级、且共享段用独立张量 `k_shared/v_shared` 存储」。两者都被标注 *"This API will be deprecated in the future, please use `MultiLevelCascadeAttentionWrapper` instead."*（见 [flashinfer/cascade.py:568-571](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cascade.py#L568-L571)）。本讲以新版为主，老版只在 4.1 用过一次作为最简样板。

#### 4.2.2 核心流程

`MultiLevelCascadeAttentionWrapper` 的生命周期同样是 `plan` → 多次 `run`：

```text
__init__(num_levels, float_workspace_buffer, kv_layout):
    内部创建 num_levels 个 BatchPrefillWithPagedKVCacheWrapper，共享同一块 float workspace

plan(qo_indptr_arr, paged_kv_indptr_arr, paged_kv_indices_arr,
     paged_kv_last_page_len_arr, num_qo_heads, num_kv_heads, head_dim, page_size, ...):
    for 每一级 i:
        wrapper[i].plan(这一级的 qo_indptr / 页表三件套 / 头参数 / ...)
        # 注意：causal 只在最后一级（最底层、独立段）生效，上面各级强制 causal=False

run(q, paged_kv_cache):
    out, lse = wrappers[-1].run(q, paged_kv_cache, return_lse=True)   # 先算最底层
    for 上面的每一级 wrapper:
        out_i, lse_i = wrapper.run(q, paged_kv_cache, return_lse=True)
        merge_state_in_place(out, lse, out_i, lse_i)                  # 逐层合并
    return out
```

数据布局的关键约定（取自类 docstring 的示例 [flashinfer/cascade.py:236-298](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cascade.py#L236-L298)）：

- 共享级 `qo_indptr = [0, batch_size]`：所有 `batch_size` 个 query 落在同一个区间，说明它们全部共享这一级的 KV。
- 独立级 `qo_indptr = [0,1,2,...,batch_size]`：每个 query 各占一格，说明每个 query 只看自己那一段 KV。
- 物理页号连续编号：`shared_kv_page_indices = [0..shared_kv_num_pages)`，`unique_kv_page_indices = [shared_kv_num_pages .. total_num_pages)`，全部指向同一个 `paged_kv_cache` 张量。

#### 4.2.3 源码精读

**构造**：[flashinfer/cascade.py:300-L372](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cascade.py#L300-L372) —— `__init__` 创建 `num_levels` 个 `BatchPrefillWithPagedKVCacheWrapper`；当 `use_cuda_graph=True` 时把每级的页表缓冲区显式传给子 wrapper，以便捕获进图。

**plan 的逐级派发**：[flashinfer/cascade.py:482-L517](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cascade.py#L482-L517) —— 用 `zip(..., strict=True)` 把每级的 `qo_indptr`/页表三件套对齐，逐级调用子 wrapper 的 `plan`。最关键的一行是：

```python
causal=causal if i == self._num_levels - 1 else False,
```

只有最底层（`num_levels - 1`，即独立段）才会应用因果掩码；上面各级都是「过去已完成的共享段」，强制 `causal=False`。这与 4.1.3 老版 wrapper 的语义一致。

**run 的合并循环**：[flashinfer/cascade.py:521-L556](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cascade.py#L521-L556) —— 这是 cascade 的「心脏」：

```python
out, lse = self._batch_prefill_wrappers[-1].run(q, paged_kv_cache, return_lse=True)
for wrapper in self._batch_prefill_wrappers[:-1]:
    out_i, lse_i = wrapper.run(q, paged_kv_cache, return_lse=True)
    merge_state_in_place(out, lse, out_i, lse_i)
return out
```

读懂三点：(1) 先算最底层、把它的 `(out, lse)` 当作「累加器」；(2) 对上面每一级各算一次 `(out_i, lse_i)`；(3) 用 `merge_state_in_place` 把上一级结果原地合并进累加器。注意是 `in_place`：`out` 和 `lse` 张量被直接改写，省掉一次拷贝。

> 这也解释了为何 `MultiLevelCascadeAttentionWrapper` 内部用的是 `BatchPrefill` 而非 `BatchDecode`：prefill wrapper 的 `run` 支持 `return_lse=True` 且能处理「一个 query 对一段 KV」的一般情形，恰好满足 cascade「每级一次 attention」的需求。即便你做的是 decode（每请求 1 个 query），通过 prefill wrapper 也能正确工作（query 序列长就是 1）。

#### 4.2.4 代码实践

**目标**：用 `MultiLevelCascadeAttentionWrapper` 跑一个 2 级 cascade（1 段共享 + 每请求 1 段独立），并与「拼接后整体 prefill」对拍。

**操作步骤**（参照类 docstring 示例 [flashinfer/cascade.py:236-298](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cascade.py#L236-L298)）：

1. 准备 `num_qo_heads=64, num_kv_heads=8, head_dim=128, page_size=16, batch_size=7`。
2. 分配统一 paged KV：`total_num_pages = shared_kv_num_pages + unique_kv_num_pages`，张量形状 `[total_num_pages, 2, page_size, num_kv_heads, head_dim]`，dtype `float16`。
3. 构造两级页表：
   - 共享级：`shared_kv_page_indices=[0..512)`、`shared_kv_page_indptr=[0, 512]`、`shared_kv_last_page_len=[16]`、`shared_qo_indptr=[0, 7]`。
   - 独立级：`unique_kv_page_indices=[512..640)`、`unique_kv_page_indptr=[0,17,29,44,48,66,100,128]`、`unique_kv_last_page_len=[1,7,14,4,3,1,16]`、`unique_qo_indptr=[0,1,2,3,4,5,6,7]`。
4. `wrapper.plan([shared_qo_indptr, unique_qo_indptr], [shared_indptr, unique_indptr], [shared_indices, unique_indices], [shared_lpl, unique_lpl], 64, 8, 128, 16)`。
5. `o = wrapper.run(q, kv_cache)`。
6. **对拍**：另起一个 `BatchPrefillWithPagedKVCacheWrapper`，把每个请求的页表设成「共享页 ++ 该请求的独立页」拼接（即不做 cascade，直接整体算），用相同 `q` 和 `kv_cache` 跑一遍，比较两次 `o`。

**需要观察的现象**：两次输出在 fp16 下最大绝对差应在 \(10^{-2}\) 量级以内（fp16 精度）。

**预期结果**：cascade 输出与「整体 prefill」输出逐元素接近，证明多级合并数学正确。

> 待本地验证：步骤 6 的对拍脚本需要你自行编写；本实践未实际运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `plan` 里强调 `causal` 只对最后一级生效？如果对共享级也开 `causal=True` 会怎样？

> **答案**：共享级是所有请求共享的「已完成历史」，对任意 query 都应完全可见，没有未来 token 需要遮蔽。若对共享级开 `causal`，会把共享段里「位置大于 query 位置」的 token 错误地遮掉，导致结果错误（除非你的语义确实要求如此）。

**练习 2**：把 `num_levels` 从 2 调到 4，显存一定更省吗？

> **答案**：不一定。显存上，多级只有在「确实存在多层共享结构」时才省；计算上，每多一级就多一次 attention + 一次 `merge_state_in_place`。源码注释明确警告级数过多会被合并开销吃掉收益。

---

### 4.3 state merge

#### 4.3.1 概念说明

`merge_state` 系列函数是 cascade 的「粘合剂」。它们解决的问题可以一句话说清：

> 已知对同一段 KV 的两个不相交子段分别做注意力得到的「归一化输出 + LSE」，求对整段做注意力的「归一化输出 + LSE」。

提供三个函数（[flashinfer/cascade.py:40-L213](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cascade.py#L40-L213)）：

| 函数 | 输入 | 用途 |
|------|------|------|
| `merge_state(v_a, s_a, v_b, s_b)` | 两个状态 | 合并恰好 2 段，返回新的 `(V, S)`。cascade wrapper 之外也常单独用。 |
| `merge_state_in_place(v, s, v_other, s_other, mask=...)` | 两个状态，原地写回 `(v, s)` | cascade wrapper 的 `run` 循环里用它逐层合并；`mask` 支持 CUDA Graph 下「某些序列不参与合并」。 |
| `merge_states(v, s)` | 多个状态（带 `num_states` 维） | 一次性合并 \(\ge 2\) 段，内部按段数自动选 kernel。 |

它们都被 `@register_custom_op` 注册成 torch custom op，从而兼容 `torch.compile` 与 CUDA Graph；同时挂了 `@flashinfer_api(trace=...)` 以支持 `fi_trace`（详见 u9-l5）。

> 为什么 cascade wrapper 用 `merge_state_in_place` 而非 `merge_state`？因为多级合并是一个「把第 1 级合并进累加器、再把第 2 级合并进同一个累加器、…」的串行过程，原地写回省掉每级的输出张量分配。

#### 4.3.2 核心流程：合并的数学

设 pre-softmax logits 为 \(a_i\)（已含 \(1/\sqrt{d}\) 缩放）。对子段 \(A\) 定义：

\[
m_A = \max_{i\in A} a_i,\qquad
d_A = \sum_{i\in A} \exp(a_i - m_A),\qquad
O_A^{\text{unnorm}} = \sum_{i\in A} \exp(a_i - m_A)\, v_i
\]

归一化输出与 LSE 分别为：

\[
O_A = O_A^{\text{unnorm}} / d_A,\qquad
l_A = m_A + \ln d_A
\]

（FlashInfer 在 kernel 里用 base-2：\(l_A = m_A + \log_2 d_A\)，\(\exp\) 换成 \(\exp_2\)，数值等价、PTX 指令更快。）对子段 \(B\) 同理得 \(O_B, l_B\)。那么对整段 \([A;B]\) 的归一化输出满足：

\[
O = \frac{\exp(l_A)\,O_A + \exp(l_B)\,O_B}{\exp(l_A)+\exp(l_B)}
\]

令 \(M = \max(l_A, l_B)\)，分子分母同除 \(\exp(M)\)：

\[
\alpha_A = \frac{\exp(l_A-M)}{\exp(l_A-M)+\exp(l_B-M)},\quad
\alpha_B = \frac{\exp(l_B-M)}{\exp(l_A-M)+\exp(l_B-M)},\quad
O = \alpha_A O_A + \alpha_B O_B
\]

整段的 LSE 则是：

\[
l = M + \ln\!\bigl(\exp(l_A-M)+\exp(l_B-M)\bigr)
\]

这就是合并两个**已归一化**状态的全部数学。它只用到 \(O_A, l_A, O_B, l_B\) 四个量，**完全不需要再读 K/V**——这正是 cascade 省带宽的根本原因。

#### 4.3.3 源码精读

**(1) kernel 实现 `MergeStateKernel`**：[include/flashinfer/attention/cascade.cuh:44-L71](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/cascade.cuh#L44-L71) —— 它把 4.3.2 的公式逐行翻译成 CUDA：

```cpp
float s_max = max(s_a_val, s_b_val);              // M = max(l_A, l_B)
s_a_val = math::ptx_exp2(s_a_val - s_max);        // exp2(l_A - M)
s_b_val = math::ptx_exp2(s_b_val - s_max);        // exp2(l_B - M)
float a_scale = s_a_val / (s_a_val + s_b_val);    // α_A
float b_scale = s_b_val / (s_a_val + s_b_val);    // α_B
for (...) v_merged_vec[i] = a_scale * v_a_vec[i] + b_scale * v_b_vec[i];  // O
s_merged[...] = math::ptx_log2(s_a_val + s_b_val) + s_max;                // l
```

注意三处工程细节：(a) 用 `ptx_exp2`/`ptx_log2` 走 base-2，比 `expf`/`logf` 快；(b) 一个 block 处理一个 `(pos, head)`，`tx` 切 `head_dim`、`ty` 列举 `num_heads`；(c) 用 `vec_t` + `cast_load` 做向量化读写，把 `head_dim` 一次搬 `vec_size` 个元素。

**(2) 原地版 `MergeStateInPlaceKernel`**：[include/flashinfer/attention/cascade.cuh:86-L116](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/cascade.cuh#L86-L116) —— 数学完全相同，区别有二：结果直接写回 `v`/`s`（in-place）；开头多一行 `if (mask != nullptr && mask[pos] == 0) return;`，这个 `mask` 是为 CUDA Graph 准备的——图捕获时 batch 大小固定，但某些序列可能「没有上层级」，用 mask 跳过它们的合并。

**(3) 多段版与「大段数」优化**：`MergeStatesKernel`（[cascade.cuh:213-L256](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/cascade.cuh#L213-L256)）处理一般 `num_index_sets` 段，用 `state_t::merge` 循环合并；当 `num_index_sets >= seq_len` 时，launcher `MergeStates`（[cascade.cuh:637-L668](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/cascade.cuh#L637-L668)）会切到 `MergeStatesLargeNumIndexSetsKernel`，它用 `cp_async` 多级共享内存流水线预取（[cascade.cuh:275-L340](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/cascade.cuh#L275-L340)），是典型的「按数据形状选 kernel」分派。

**(4) 数学源头 `state_t`**：[include/flashinfer/attention/state.cuh:28-L79](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/state.cuh#L28-L79) —— `state_t` 是 FlashAttention「在线 softmax」的状态三元组：

```cpp
vec_t<float, vec_size> o;   // 加权和（未归一化）
float m;                      // 当前已见 logits 的最大值
float d;                      // sum exp(a_i - m)
```

`merge` 方法（[state.cuh:53-L62](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/state.cuh#L53-L62)）实现的就是「两个未归一化状态的在线合并」：

```cpp
m = max(m_prev, other_m);
d = d_prev * exp2(m_prev - m) + other_d * exp2(other_m - m);
o[i] = o[i] * exp2(m_prev - m) + other_o[i] * exp2(other_m - m);
```

这与 4.3.2 的推导是同一件事的「未归一化」视角。`MergeStateKernel` 处理的是已归一化的 \((O, l)\)，`state_t::merge` 处理的是未归一化的 \((o, m, d)\)，两者数学等价。`get_lse()`（[state.cuh:45](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/state.cuh#L45)）给出 \(m + \log_2 d\) 正好把两种视角连起来。

**(5) launcher 与 FFI 导出**：[csrc/cascade.cu:23-L58](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/cascade.cu#L23-L58) 的 `merge_state` launcher 做张量形状校验（`v_a` 是 3 维 `[seq_len, num_heads, head_dim]`，`s_a` 是 2 维 `[seq_len, num_heads]`，且 `v_a.size(0)==s_a.size(0)`、`v_a.size(1)==s_a.size(1)`），再用 `DISPATCH_DLPACK_DTYPE_TO_CTYPE_FP16` 把 fp16/bf16 派发到具体 C 类型，最后调用 kernel 模板 `MergeState`。三个 launcher 由 [csrc/flashinfer_cascade_binding.cu:30-L34](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/flashinfer_cascade_binding.cu#L30-L34) 用 `TVM_FFI_DLL_EXPORT_TYPED_FUNC` 导出，被 `gen_cascade_module`（[flashinfer/jit/cascade.py:21-L28](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cascade.py#L21-L28)）装配成 JIT 模块、由 `get_cascade_module()`（[flashinfer/cascade.py:35-L37](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cascade.py#L35-L37)，带 `@functools.cache`）加载——这正是 [u2-l3](u2-l3-codegen-pattern.md) 五步生成模式的最简实例。

#### 4.3.4 代码实践

**目标**：直接调用 `flashinfer.merge_state`，亲手验证 4.3.2 的合并公式与「整体算」等价。

**操作步骤**：

```python
# 示例代码（需 flashinfer 已安装；本讲未实际运行）
import math, torch, flashinfer

torch.manual_seed(0)
seq_len, num_heads, head_dim = 64, 8, 128
dkv = 32                                     # A 段长度，B 段长度 = seq_len - dkv
q  = torch.randn(seq_len, num_heads, head_dim, device="cuda", dtype=torch.float16)
k  = torch.randn(seq_len, num_heads, head_dim, device="cuda", dtype=torch.float16)
v  = torch.randn(seq_len, num_heads, head_dim, device="cuda", dtype=torch.float16)

# (1) 分段：用 single_prefill 各算一次，要求 return_lse=True
from flashinfer import single_prefill_with_kv_cache
o_a, lse_a = single_prefill_with_kv_cache(q, k[:dkv], v[:dkv], return_lse=True)
o_b, lse_b = single_prefill_with_kv_cache(q, k[dkv:], v[dkv:], return_lse=True)

# (2) 合并
o_merged, lse_merged = flashinfer.merge_state(o_a, lse_a, o_b, lse_b)

# (3) 参考：对整段直接算一次
o_full, lse_full = single_prefill_with_kv_cache(q, k, v, return_lse=True)

print((o_merged - o_full).abs().max())      # 期望 fp16 量级误差
print((lse_merged - lse_full).abs().max())
```

**需要观察的现象**：`o_merged` 与 `o_full` 的最大绝对差在 fp16 下应在 \(10^{-2}\) 量级；`lse_merged` 与 `lse_full` 的差也极小。

**预期结果**：合并输出与整体算输出一致（fp16 舍入误差内）。

> 待本地验证：上述脚本依赖 GPU 与已 JIT 编译的 cascade 模块；未在本讲环境中运行。

#### 4.3.5 小练习与答案

**练习 1**：`merge_state` 要求输入 `s_a`/`s_b` 是 float32，即使 `v` 是 fp16。为什么 LSE 必须用更高精度？

> **答案**：LSE 参与指数运算 \(\exp(l_A - M)\)。当 \(l_A - M\) 较大时，指数值会剧烈变化，fp16 的有限动态范围和精度会让 \(\alpha_A, \alpha_B\) 严重失真，进而把合并权重算错。源码里 `merge_state` 开头就把 `s_a = s_a.to(torch.float32)`（[flashinfer/cascade.py:90-L91](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cascade.py#L90-L91)），kernel 内部所有 scale 计算都在 float32 下进行。

**练习 2**：`MergeStateKernel` 里 `s_merged[...] = math::ptx_log2(s_a_val + s_b_val) + s_max`，其中 `s_a_val`/`s_b_val` 此刻已经是 `exp2(l - M)`。请用 4.3.2 的公式验证这行写回了正确的整段 LSE。

> **答案**：4.3.2 给出 \(l = M + \ln(\exp(l_A-M)+\exp(l_B-M))\)。换 base-2 即 \(l = M + \log_2(\exp_2(l_A-M)+\exp_2(l_B-M))\)。代码里 `s_a_val`/`s_b_val` 正是 \(\exp_2(l_A-M)\) 与 \(\exp_2(l_B-M)\)，故 `ptx_log2(s_a_val+s_b_val) + s_max` 与公式完全一致。

---

## 5. 综合实践

把本讲三块知识串成一个完整任务：**构造一个所有请求共享 system prompt 的 decode 场景，用 cascade wrapper 计算，再用 `merge_state` 手动复现合并过程，最后与普通 attention 对拍。**

1. **场景搭建**：`batch_size=4`，共享 system prompt 长 `S=512`，每请求独立对话长 `U_i ∈ {16, 24, 32, 40}`，`num_qo_heads=32, num_kv_heads=8, head_dim=128, page_size=16`，dtype fp16。
2. **方式 A：用 `MultiLevelCascadeAttentionWrapper`（2 级）**：
   - 把共享段放进 `[0, S/page_size)` 这些页，独立段放进后续页；
   - 共享级 `qo_indptr=[0,4]`、独立级 `qo_indptr=[0,1,2,3,4]`；
   - `plan(...)` 后 `o_cascade = wrapper.run(q, kv_cache)`。
3. **方式 B：手动复现合并**（验证你对 4.3 的理解）：
   - 用 `single_prefill_with_kv_cache` 对共享段算 `(O_S, l_S)`（所有请求共享同一个 `k_S/v_S`）；
   - 用 `BatchDecodeWithPagedKVCacheWrapper` 对各自的独立段算 `(O_U, l_U)`（参考 [flashinfer/cascade.py:749-L811](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cascade.py#L749-L811) 的老版 forward 写法）；
   - 调 `flashinfer.merge_state(O_S, l_S, O_U, l_U)` 得 `o_manual`。
4. **方式 C：参考答案（普通 attention）**：把每个请求的 KV 拼成 `S ++ U_i`，用 `single_prefill_with_kv_cache`（或 `BatchPrefillWithPagedKVCacheWrapper`）整体算一次得 `o_full`。
5. **比较**：`o_cascade ≈ o_manual ≈ o_full`（fp16 误差内）。

**思考题**（写进你的实验报告）：方式 A 与方式 C 相比，省下了多少次对共享段 K/V 的读取？方式 B 中的 `merge_state` 调用相比方式 A 内部的 `merge_state_in_place`，多分配了哪些张量？

> 待本地验证：本综合实践依赖真实 GPU 与 JIT 编译；脚本需自行编写，结果为「按本讲原理推导的预期」。

## 6. 本讲小结

- **共享前缀**把每个请求的 KV 拆成「全局共享段 + 各自独立段」，共享段只存一份、只算一次，主要省显存与 decode 带宽；但每多一级都要付出一次合并 kernel 的代价，级数并非越多越好。
- **`MultiLevelCascadeAttentionWrapper`** 是当前推荐的 cascade API：所有级 KV 存于统一 paged 表，内部持有一组 `BatchPrefillWithPagedKVCacheWrapper`；`plan` 逐级派发（`causal` 仅对最底层生效），`run` 先算最底层、再逐层 `merge_state_in_place` 合并。两个 `*SharedPrefix*` 老 wrapper 已 deprecated。
- **合并数学**是 cascade 的根基：对两个已归一化的部分输出 \((O_A, l_A), (O_B, l_B)\)，用 LSE 做加权 \(O = \alpha_A O_A + \alpha_B O_B\) 即可精确等价于对整段算 attention，且**不需要重读 K/V**。
- **`merge_state` / `merge_state_in_place` / `merge_states`** 三个函数分别处理「合 2 段出新张量」「原地合 2 段（带 mask，供 CUDA Graph）」「一次合多段（按段数自动选 kernel）」，kernel 在 `include/flashinfer/attention/cascade.cuh`、数学源头在 `state.cuh` 的 `state_t`。
- cascade 的合并 kernel 与 wrapper 都用 base-2 的 `exp2/log2`、LSE 强制 float32，既是性能优化也是数值稳定性需要。
- 整条调用链遵循项目一贯的「四层」结构：`cascade.py` → `jit/cascade.py` → `csrc/cascade.cu` + `flashinfer_cascade_binding.cu` → `include/.../cascade.cuh` + `state.cuh`。

## 7. 下一步学习建议

- **顺着合并数学往下读**：`state_t` 的在线 softmax `merge` 同样是 split-k decode/prefill（[u3-l3](u3-l3-batch-decode-wrapper.md)、[u3-l4](u3-l4-batch-prefill-wrapper.md) 里 plan 阶段的 split-k）合并部分结果的基石。建议读 [include/flashinfer/attention/state.cuh](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/state.cuh) 与 merge kernel，理解「split-k 合并」和「cascade 合并」共用同一套数学。
- **下一讲 [u4-l3 稀疏与块稀疏注意力](u4-l3-sparse-attention.md)**：从「按 KV 分段共享」转向「按 KV 位置稀疏」，是另一条省算力/带宽的路线，可与 cascade 对比学习。
- **工程化方向**：若你想知道 `merge_state` 这类 API 如何被 `fi_trace` 导出成可复现的 benchmark JSON，可跳读 [flashinfer/trace/templates/cascade.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/trace/templates/cascade.py)，对应 u9-l5 的 trace 系统。
- **多级 cascade 的真实用法**：在仓库里检索 `MultiLevelCascadeAttentionWrapper` 的调用方与 [tests/attention/test_shared_prefix_kernels.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/attention/test_shared_prefix_kernels.py)，看看它在「system prompt + 多轮对话」场景里如何与 `append_paged_kv_cache`（[u3-l2](u3-l2-paged-kv-layout-append.md)）配合，完成一次完整的「写入新 token → cascade decode」循环。
