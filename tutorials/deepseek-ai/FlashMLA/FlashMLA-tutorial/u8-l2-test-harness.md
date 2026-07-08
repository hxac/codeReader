# 测试体系与 PyTorch 参考实现

## 1. 本讲目标

FlashMLA 的 kernel 是手写的 Hopper/Blackwell CUDA 代码，光读 kernel 看不出「它到底算得对不对」。本讲不进 kernel 内部，而是讲**怎么验证 kernel 正确、怎么测量它的性能**——也就是 `tests/` 目录下的整套测试脚手架。读完本讲你应当能够：

1. 看懂 `tests/lib.py` 如何用一个 `TestParam`/`Testcase` 数据结构把「形状 + 各种开关」翻译成一组随机张量，并能自己新增一组测试参数。
2. 看懂 `tests/ref.py` 里 sparse prefill / sparse decode 的纯 PyTorch 参考实现，理解其中 lse 合并、attn_sink、lonely query 三种边界处理。
3. 区分 `test_flash_mla_*.py` 里 **correctness / corner / performance** 三类用例各自的用途与覆盖范围。
4. 说清 `kk.check_is_allclose` 的三重容差 `abs_tol / rel_tol / cos_diff_tol` 各自在判定中扮演什么角色。

> 本讲承接 u3-l4（dense decode 三段式编排）与 u6-l1（sparse attention 语义与 indices 编码）。前置讲义已经讲过 lse、attn_sink、lonely query 的语义，本讲聚焦「这些语义如何被一个 PyTorch 参考实现复现、如何被测试框架逐位校验」。

## 2. 前置知识

- **参考实现（reference implementation）**：用「慢但显然正确」的纯 PyTorch 写一份注意力，作为对照基准。FlashMLA 的 kernel 输出与它逐元素比对。
- **lse（log-sum-exp）**：softmax 分母的对数 \(\mathrm{lse}=\log\sum_i e^{x_i}\)。u4-l1 讲过 split-KV 靠 rescale lse 合并多段，本讲的 `_merge_two_lse` 是同一数学原理的另一种用法。
- **base-e / base-2**：kernel 内部用 `exp2f`（base-2）算更快，对调用方则统一返回 base-e 的 lse。参考实现全程用 `torch.logsumexp`（base-e），所以二者可以直接比。
- **attn_sink / lonely query**：u4-l2 与 u6-l1 已定义——attn_sink 把输出按比例缩小但**不**改 lse；lonely query 是「没有任何有效 key 可注意」的 query，其输出强制置 0、lse 强制置 +inf。
- **bf16 的精度**：bfloat16 有 7 位尾数，单位最低位（ULP）约为 \(2^{-7}=1/128\)。后面会看到测试容差里反复出现 `2.01/128`，正是「容忍约 2 个 bf16 ULP 的误差」。

## 3. 本讲源码地图

| 文件 | 职责 |
| --- | --- |
| `tests/lib.py` | 测试参数与数据生成中枢。定义 `TestParam`/`Testcase` 等数据结构，`generate_testcase*` 造随机输入，`run_flash_mla_*` 驱动 kernel，`count_flop_and_mem_vol*` 算性能指标。 |
| `tests/ref.py` | sparse prefill / sparse decode 的纯 PyTorch 参考实现（`ref_sparse_attn_fwd`、`ref_sparse_attn_decode`、`_merge_two_lse`）。 |
| `tests/test_flash_mla_dense_decoding.py` | dense decode 的测试入口。**注意：dense decode 的参考实现 `reference_torch` 写在这个文件里，而不在 ref.py。** 含 correctness/corner/performance 三类用例。 |
| `tests/test_flash_mla_sparse_prefill.py` | sparse prefill 的测试入口，复用 `lib.py` 与 `ref.py`，用例分 correctness / correctness_with_features / corner / performance 四档。 |
| `tests/kernelkit/compare.py` | 容差判定核心 `check_is_allclose`、`get_cos_diff`、`check_is_bitwise_equal`。 |
| `tests/kernelkit/bench.py` | 性能测量 `bench_kineto`，按 kernel 名取时间。 |

> 还有一份 `tests/test_flash_mla_sparse_decoding.py` 与 `tests/kernelkit/precision.py`、`utils.py` 作为旁证，本讲会点到为止。

## 4. 核心概念与源码讲解

### 4.1 用例生成 lib.py

#### 4.1.1 概念说明

`lib.py` 是测试体系的「数据工厂」。它的职责只有一个：给定一组**形状参数与开关**，造出一组**带边界条件的随机张量**，再驱动被测 kernel 跑一次。它要解决的问题有三个：

1. **统一参数**：sparse prefill 和 sparse decode 共用同一个 `TestParam` 结构，避免维护两套配置。
2. **可控的边界数据**：测试不光要测「正常输入」，还要构造「无效索引」「零长度序列」「全 -1 索引」这类能逼出 kernel bug 的极端数据。
3. **性能可测**：测试脚本里既要比正确性又要测 TFlops/GB/s，所以需要一份与 kernel 无关的 FLOPs 与访存量计算。

`lib.py` 不属于 `flash_mla` 包，它是测试私有的辅助库；但 `test_flash_mla_sparse_prefill.py`、`test_flash_mla_sparse_decoding.py` 都 `import lib`、`import ref` 复用它。dense decode 的测试文件则**自带**一份精简的 `TestParam`/`generate_test_data`/`reference_torch`，因为它的参数模型更简单（见 4.1.2）。

#### 4.1.2 核心流程

一次 sparse prefill 测试的数据流是：

```
TestParam (形状+开关)
   │  generate_testcase
   ▼
Testcase (q, kv, indices, attn_sink, topk_length, sm_scale, dOut)
   │  run_flash_mla_sparse_fwd  → kernel 输出 (out, max_logits, lse)
   │  ref.ref_sparse_attn_fwd   → 参考输出
   ▼
kk.check_is_allclose 三重容差比对
```

sparse decode 多一层 `KVScope`：`generate_testcase_for_decode` 会造一个（或两个，含 extra KV）`KVScope`，里面打包了分页的 `blocked_k`、`block_table`、`indices_in_kvcache`、`cache_seqlens`、`topk_length`，并可选地做 FP8 量化/反量化。

参数层面有两套结构，理解它们的分工很关键：

- `TestParam`：sparse 通用参数（s_q, s_kv, topk, h_q, h_kv, d_qk, d_v，以及 `have_attn_sink`/`have_topk_length`/`is_all_indices_invalid` 等开关，decode 专属字段塞进 `decode: Optional[ExtraTestParamForDecode]`）。
- `RawTestParamForDecode`：decode 测试的**扁平化**参数（把 decode 专属字段从 `TestParam.decode` 里拍平到顶层），并提供 `to_test_param()` 转回 `TestParam`。这是因为用列表推导批量构造用例时，扁平结构写起来更顺手。

#### 4.1.3 源码精读

先看统一参数结构 `TestParam` 与它的 decode 扩展：

[tests/lib.py:28-43](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L28-L43) 定义 `TestParam`，默认值正是 MLA 的典型形状（`h_q=128, h_kv=1, d_qk=512, d_v=512`），`seed=-1` 表示由测试脚本运行时自动分配（见 4.3 节的 `kk.Counter`）。

[tests/lib.py:17-26](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L17-L26) 定义 `ExtraTestParamForDecode`，承载 decode 专属字段（batch、是否 varlen、是否有零长度 KV、extra KV 的 topk/block_size 等）。

[tests/lib.py:74-87](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L74-L87) 是 `RawTestParamForDecode.to_test_param()`，把扁平字段重新打包成嵌套的 `TestParam`——这是「写用例时用 Raw、跑测试时用 TestParam」的桥梁。

边界数据构造的关键是 `_randperm_batch`，它故意掺入一批「看起来合法实则越界」的无效索引候选：

[tests/lib.py:100-119](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L100-L119) 用 `topk` 选出每行的不重复索引，再把超出 `perm_range` 的位置随机替换成无效候选值。注意它临时打开 `use_deterministic_algorithms` 来保证可复现，结束时再关掉。

而真正喂给参考实现和 kernel 的无效值清单在调用处：

[tests/lib.py:131-137](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L131-L137) 列出 `invalid_indices_candidate = [-2147483648, -123456, -1, t.s_kv, 114514, 1919810, 2147480000, 2147483647]`——既有负数（`-1` 是 sparse prefill 的标准无效标记，见 u6-l1），也有等于 `s_kv` 的越界值，还有接近 `INT32_MAX` 的极端值。这些值会被参考实现和 kernel **同样**判定为无效而掩码掉，从而测试 kernel 的边界判定是否与参考一致。`is_all_indices_invalid=True` 时还会把某些行的索引全部置成无效值，专测「整行无有效 key」（即 lonely query）。

decode 路径的 `KVScope` 把分页 KV cache 的全部组件打包到一起，并可选 FP8 量化：

[tests/lib.py:178-193](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L178-L193) 是 `KVScope.quant_and_dequant_()`：按 `d_qk` 选 FP8 布局（`576→V32_FP8Sparse`、`512→MODEL1_FP8Sparse`，见 u5-l1），调 `quant.quantize_k_cache` 量化、再 `dequantize_k_cache` 反量化写回 `blocked_k`。注释点明了动机——量化误差本身可能大到「掩盖 kernel bug」，所以先用真实 FP8 布局量化、再反量化成 bf16，让参考实现和 kernel 都基于「带量化误差的同一份 KV」比对，把量化误差从「正确性误差」里剥离出去。

最后是驱动函数与性能统计：

[tests/lib.py:310-334](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L310-L334) 的 `run_flash_mla_sparse_fwd` / `run_flash_mla_decode` 把构造好的 `Testcase`/`TestcaseForDecode` 喂给 `flash_mla.*` 的 Python 入口，是「测试数据 → kernel」的最后一棒。

[tests/lib.py:367-402](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L367-L402) 的 `count_flop_and_mem_vol_for_decode` 算 decode 的算力与访存：FLOPs \(=2\cdot h_q\cdot \text{num\_attended\_tokens}\cdot(d_{qk}+d_v)\)，访存按 FP8 KV 每 token 656 字节（`d_qk==576`）或 576 字节（`d_qk==512`）估算——这里的 656/576 与 u5-l1 的 FP8 布局完全一致。

#### 4.1.4 代码实践（源码阅读型）

**目标**：追踪一条「形状参数 → 无效索引」的链路，理解边界值从哪进来。

**步骤**：

1. 打开 `tests/lib.py`，定位 `generate_testcase`（121 行起）。
2. 找到 `invalid_indices_candidate` 那一行（131 行），记下 8 个候选值。
3. 往上看 `_randperm_batch` 的 `paddings` 参数如何接收这个列表（132 行调用处把 `invalid_indices_candidate` 当 `paddings` 传入）。
4. 往下看 `_randperm_batch` 内部 113-117 行如何用 `masked_scatter_` 把越界位置填成随机选中的 padding 值。

**观察现象**：你会看到无效索引不是固定 `-1`，而是从 8 个候选里随机抽，且每个测试行可能不同。

**预期结果**：能口头说出「`-1`、`s_kv`、`INT32_MAX` 这三种无效值分别测的是 sparse prefill 的负数标记、上界越界、整数溢出三种 kernel 必须处理的边界」。**待本地验证**：若你改 `invalid_indices_candidate` 只留 `[-1]`，corner 用例里的 lonely query 行为应仍然成立（因为 `-1` 本就是 prefill 的标准无效标记）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `KVScope.quant_and_dequant_()` 要「量化再反量化」，而不是直接拿原始 bf16 KV 当参考？

**答案**：因为 FP8 量化本身会引入误差。如果参考实现用原始 bf16、kernel 用 FP8，比对误差里会混入「量化误差」，可能大到掩盖真正的 kernel bug。先量化再反量化，让参考实现也基于「带量化误差的 KV」，二者站在同一起跑线，剩下的误差才纯粹反映 kernel 实现质量。

**练习 2**：`RawTestParamForDecode` 相比 `TestParam` 多了什么、又少了什么？为什么要两套？

**答案**：`RawTestParamForDecode` 把 decode 专属字段（`b`、`is_varlen`、`extra_*`、`block_size` 等）从 `TestParam.decode` 嵌套里拍平到顶层，少了 `decode` 这个嵌套对象与 `h_kv`（decode 恒为 1）。两套是因为批量构造用例时扁平结构写列表推导更顺手，而 `lib.py` 内部统一用嵌套的 `TestParam`，靠 `to_test_param()` 桥接。

---

### 4.2 参考实现 ref.py

#### 4.2.1 概念说明

`ref.py` 是 sparse 路径的**数值黄金参考**：用 `torch.logsumexp`、`index_select`、矩阵乘这些「显然正确但慢」的 PyTorch 算子，逐步重放 sparse attention 的数学定义。它必须和 kernel 处理**完全相同的边界**——无效索引、attn_sink、lonely query——否则比对会因为「语义不一致」而非「kernel 算错」而失败。

这里有个容易踩的坑：**dense decode 的参考实现不在 `ref.py` 里**，而在 `test_flash_mla_dense_decoding.py` 的 `reference_torch` 函数里。这是因为 dense decode 的测试文件历史更早、参数模型更简单（没有 indices/sink/topk_length），所以自带一份参考实现，没有迁到 `ref.py`。读源码时不要去 `ref.py` 找 dense decode。

`ref.py` 里有两套参考实现：`ref_sparse_attn_fwd`（prefill，无 batch 维）与 `ref_sparse_attn_decode`（decode，有 batch 维、读 `indices_in_kvcache`）。二者数学同构，差异在数据布局与 attn_sink 的落地方式。

#### 4.2.2 核心流程

sparse prefill 参考实现的流程（`ref_sparse_attn_fwd`）：

```
indices ── mask(topk_length) ── invalid_mask(<0 | >=s_kv) ── 把无效处置 0
   │
   ▼ index_select 从 kv 里 gather 出 [s_q, topk, d_qk]
q @ gathered_kvᵀ → P [s_q, h_q, topk]，乘 sm_scale，无效位置置 -inf
   │
   ├─ orig_lse = logsumexp(P)          # base-e，不含 sink
   ├─ max_logits = P.max()
   ├─ lse_for_o = merge(orig_lse, attn_sink)   # 用 logsumexp 合并
   ├─ out = softmax(P - lse_for_o) @ gathered_kv[:, :d_v]
   └─ lonely_q: orig_lse==-inf → out=0, lse=+inf
```

sparse decode 参考实现（`ref_sparse_attn_decode`）多了两件事：①支持 extra KV scope（concat 第二段 gathered_kv）；②attn_sink 的落地不是改 lse_for_o，而是直接对输出乘一个缩放因子。两者的 lonely query 处理一致（out=0、lse=+inf）。

lse 与 attn_sink 合并的数学：设正常 logits 的 lse 为 \(L_o\)、sink 的 logits 为 \(s\)，则合并 lse

\[
L'=\log\bigl(e^{L_o}+e^{s}\bigr)=\mathrm{logsumexp}(L_o,\,s)
\]

这正是 `_merge_two_lse` 用 `torch.logsumexp(torch.stack([lse0, lse1]))` 做的事，与 u4-l1 讲的 split-KV rescale 合并是同一个公式。

#### 4.2.3 源码精读

先看 lse 合并工具：

[tests/ref.py:7-17](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L7-L17) 的 `_merge_two_lse`：把 `lse0`（正常 logits 的 lse）与可选的 `lse1`（attn_sink，形状 `[h_q]`，需 broadcast 到 `[s_q, h_q]`）用 `torch.logsumexp` 沿新维度合并。`lse1 is None` 时直接返回 `lse0`——这就是 `have_attn_sink=False` 时的快路径。

再看 sparse prefill 参考实现的三个关键段落：

[tests/ref.py:27-38](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L27-L38) 处理索引掩码与打分：先用 `topk_length` 把「最左若干个之外」的索引置 -1（u6-l1 的截断语义），再用 `(indices<0)|(indices>=s_kv)` 得到无效掩码、把无效索引处临时填 0 以便 `index_select` 不报错，算完 `P` 后再把无效位置乘回 `-inf`。这保证无效 key 不影响 `max`/`lse`。

[tests/ref.py:40-48](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L40-L48) 是 lse 合并与输出计算的核心：`orig_lse` 是不含 sink 的真 lse（这是要返回给外部比对的值），`lse_for_o` 是合并 sink 后用于算 `out` 的 lse。注意第 46 行 `lse_for_o[lse_for_o==-inf]=+inf`——当某 query 全无效时 \(e^{P-\mathrm{lse}}\) 会全是 0，输出自然是 0。`out` 用 `gathered_kv[..., :d_v]` 取前 `d_v=512` 维（V 只取 K 的 latent 段，呼应 MLA「V=K 的前 dv 维」，见 u1-l1/u7-l2）。

[tests/ref.py:50-52](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L50-L52) 处理 lonely query：`orig_lse==-inf` 说明该 query 没有任何有效 key，于是把返回的 lse 改成 `+inf`（与 kernel 约定一致），输出由前面的 `+inf` 机制已经保证为 0。返回四元组 `(out_bf16, out_fp32, max_logits, orig_lse)`——多返回一份 fp32 的 `out` 供测试用更高精度比对。

最后看 sparse decode 参考实现的 attn_sink 与 lonely query 落地：

[tests/ref.py:94-101](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L94-L101) 与 prefill 不同，decode 的 attn_sink 直接对**输出**乘缩放因子：

\[
\text{out} \mathrel{*}= \frac{1}{1+e^{s-L}},\quad L=\text{lse}
\]

这等价于 u4-l2 讲的「把内部 lse 抬高成含 sink 的 \(L'\)，使 softmax 权重整体缩小」——展开后就是对 out 乘 \(e^{L}/(e^{L}+e^{s})=1/(1+e^{s-L})\)。注意它**不改 `lse` 变量**，返回的 lse 仍是 sink 修改前的值，与 kernel 的 attn_sink 契约（只缩放 out、不动 lse）一致。lonely query 的 `out=0, lse=+inf` 处理与 prefill 完全相同。

#### 4.2.4 代码实践（源码阅读型）

**目标**：对比 prefill 与 decode 两份参考实现里 attn_sink 的落地差异。

**步骤**：

1. 在 `ref.py` 里分别打开 `ref_sparse_attn_fwd`（43 行 `_merge_two_lse`）与 `ref_sparse_attn_decode`（95-96 行）。
2. 在草稿纸上把 decode 的缩放因子 \(1/(1+e^{s-L})\) 代回：把 out 写成 \(\mathrm{softmax}(P)\cdot V\)，再把 \(\mathrm{softmax}\) 的分母从 \(\sum e^{P}\) 换成 \(\sum e^{P}+e^{s}\)，验证两者代数等价。
3. 确认两份实现返回的 `lse` 是否都「不含 sink」。

**预期结果**：你会得出结论——prefill 通过改「算 out 用的 lse」来缩放、decode 通过直接乘因子来缩放，数学等价；两者的对外 lse 都不含 sink。**待本地验证**：若你把 decode 第 96 行的缩放去掉，corner 用例里 `have_attn_sink=True` 的 out 比对会失败，而 lse 比对仍通过——这正是「sink 只影响 out 不影响 lse」的实证。

#### 4.2.5 小练习与答案

**练习 1**：`ref_sparse_attn_fwd` 为什么返回两份 out（bf16 和 fp32）？

**答案**：因为 kernel 输出是 bf16，直接和 bf16 参考比会再叠一层 bf16 舍入误差。多返回一份 fp32 参考让测试可以把 kernel 的 bf16 输出 `.float()` 后与 fp32 参考比对，消除参考侧的 bf16 误差，只留 kernel 侧的。`test_flash_mla_sparse_prefill.py` 第 46 行正是 `prefill_ans_out.float()` 对 `ref_out_fp32`。

**练习 2**：为什么 dense decode 的参考实现写在 `test_flash_mla_dense_decoding.py` 里，而不是 `ref.py`？

**答案**：历史原因。dense decode 测试文件参数模型简单（无 indices/sink/topk_length），自带一份 `reference_torch` 即可，没有迁入共享的 `ref.py`；而 sparse 两类测试（prefill/decode）共享 `TestParam` 与边界语义，故抽出 `ref.py` 复用。读源码时按文件位置定位即可。

---

### 4.3 三类测试用例

#### 4.3.1 概念说明

FlashMLA 的测试入口文件（`test_flash_mla_dense_decoding.py` 等）把用例分成三类，每类目标不同、配置不同：

1. **correctness（正确性）**：穷举大量「正常形状」组合，`test_performance=False`（dense）或 `num_runs=0`（sparse），**只比正确性不测速**。目的是覆盖各种 `b/s_q/s_k/h_q/h_kv` 与开关组合，抓住「某组形状算错」的回归。
2. **corner（边界）**：故意构造极端输入——零长度 KV、全无效索引、超大 `s_q`、`s_kv<topk`（block 内无有效索引）等。这些用例逼出 kernel 的边界判定 bug。
3. **performance（性能）**：用「生产典型形状」（如 V3.2、MODEL1 的真实配置），`num_runs>0`，测 TFlops/GB/s，**只测速不比正确性**（或正确性次要）。

三类用例拼成 `testcases = correctness + corner + performance` 一次跑完。sparse prefill 还多一档 `correctness_cases_with_features`，专门覆盖 `have_attn_sink`/`have_topk_length` 等功能开关的组合。

#### 4.3.2 核心流程

每个用例的执行入口是 `test_flash_mla(t)`（dense）/`run_test(p)`（sparse prefill），流程统一为「造数据 → 跑 kernel → 跑参考 → 比对 →（可选）测速」。dense 版本的关键判定在 [tests/test_flash_mla_dense_decoding.py:169-172](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py#L169-L172)：

```python
is_correct &= kk.check_is_allclose("out", out_ans, out_ref, abs_tol=8e-4, rel_tol=2.01/128, cos_diff_tol=5e-6)
is_correct &= kk.check_is_allclose("lse", lse_ans, lse_ref, abs_tol=1e-6, rel_tol=8.01/65536)
assert is_correct
```

性能段在 `test_performance=True` 时走 `kk.bench_kineto`，按 kernel 名取时间，再除以 FLOPs/访存得到 TFlops/GB/s（见 4.3.3）。

`sparse prefill` 的 `run_test` 多了一步「先存答案再测速」的微妙处理（见源码精读），并按 `num_runs==0` 跳过测速、按 `check_correctness` 跳过比对。

#### 4.3.3 源码精读

先看 dense decode 的三类用例定义：

[tests/test_flash_mla_dense_decoding.py:204-214](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py#L204-L214) 的 `correctness_cases` 用多层列表推导穷举 `b∈{1,2,6,64}`、`s_q∈{1,2,4}`、`s_k∈{20,140,4096}`、`h_q∈{1,3,9,63,64,126,128}`、`h_kv∈{1,2,3,8}`、varlen/causal，并用 `if h_q % h_kv == 0` 过滤掉非法的 GQA 配置。全部 `test_performance=False`，只验正确性。

[tests/test_flash_mla_dense_decoding.py:216-223](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py#L216-L223) 的 `corner_cases` 专门设 `have_zero_seqlen_k=True`——让部分请求的 KV 长度为 0，测 kernel 对空序列（lonely query）的处理。

[tests/test_flash_mla_dense_decoding.py:225-230](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py#L225-L230) 的 `performance_cases` 用 `b=128`、`s_q∈{1,2}`、`s_k∈{4096,8192,16384,32768}` 的生产形状，`test_performance=True`。

再看 sparse prefill 的 corner 用例如何覆盖「block 内无有效索引」与「超大 s_q」：

[tests/test_flash_mla_sparse_prefill.py:113-144](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_prefill.py#L113-L144) 三段 corner：①`is_all_indices_invalid=True` 整行无效；②`s_kv<topk`（如 `(32,2048)`、`(64,8192)`）让某些 block 完全无有效索引；③`s_q=70000` 超大 query 数，注释明说「无法放进 grid 第二维」——专测 grid 维度溢出的兜底逻辑（呼应仓库 commit `71c7379` 修复的超长序列 grid 维问题）。

性能测量的核心是 `bench_kineto`：

[tests/kernelkit/bench.py:103-158](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/kernelkit/bench.py#L103-L158) 用 PyTorch 的 CUPTI profiler（`torch.profiler`）跑 `num_tests` 次 kernel，先 warmup 一轮、再 active 一轮采集，并每次用 8GB `memset` 刷 L2 缓存避免数据复用干扰。返回的 `BenchKinetoRawResult` 把每个 kernel 名映射到一组 `(start, end)` 时间区间。

[tests/kernelkit/bench.py:74-100](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/kernelkit/bench.py#L74-L100) 的 `get_kernel_time`（按子串匹配单个 kernel 取平均）与 `get_e2e_time`（取「最后一个 kernel 结束 − 第一个 kernel 开始」的端到端时间）是测试脚本算 TFlops/GB/s 的依据。dense decode 在 [test_flash_mla_dense_decoding.py:175](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py#L175) 用 `get_kernel_time("flash_fwd_splitkv_mla_kernel")` 取主 kernel 时间；sparse decode 则用 `get_e2e_time(splitkv, combine)` 把主 kernel 与 combine 一起计入端到端时间（见 u4-l2 的三段式）。

#### 4.3.4 代码实践（源码阅读型）

**目标**：统计 dense decode 三类用例各有多少条，并解释 `if h_q % h_kv == 0` 过滤的意义。

**步骤**：

1. 打开 `tests/test_flash_mla_dense_decoding.py`，对 204-214 行的 `correctness_cases` 手算组合数（`b=4 × s_q=3 × s_k=3 × h_q=7 × h_kv=4 × is_varlen=2 × is_causal=2`，再乘以过滤后的命中率）。
2. 解释为何 `h_q=9, h_kv=8`（9 不能被 8 整除）会被过滤。
3. 对比 sparse prefill 的用例清单（62-160 行），数出 correctness / with_features / corner / performance 各自的规模量级。

**预期结果**：dense correctness 用例在数千条量级，corner 数十条，performance 数条。`h_q % h_kv == 0` 是 GQA 的硬约束——每个 kv 头要被 `h_q/h_kv` 个 q 头共享，不能整除则无意义。**待本地验证**：若你在 correctness 列表里加 `h_q=9, h_kv=8`（去掉过滤），kernel 会在接口校验层就报错（呼应 u3-l4 的 dtype/shape 校验）。

#### 4.3.5 小练习与答案

**练习 1**：performance 用例为什么用 `num_runs>0` 而 correctness 用例用 `num_runs=0`？

**答案**：`num_runs` 是 `bench_kineto` 的采样次数。correctness 用例只验正确性，设 `num_runs=0` 让 `run_test` 跳过测速分支（见 sparse prefill 第 32 行 `if p.num_runs > 0`），省掉 profiler 开销；performance 用例要测速，需多次采样取平均，故 `num_runs>0`。

**练习 2**：sparse prefill 的 corner 里有一条 `s_q=70000, check_correctness=False`，为什么 correctness 关掉？

**答案**：因为 `s_q=70000` 的全量参考实现要构造并计算超大张量，PyTorch 参考侧会非常慢甚至 OOM。这条用例的目的是测「grid 维度不溢出、kernel 能正确启动并跑完」这个工程边界，而非数值正确性，所以关掉 correctness 比对、只验证不崩溃。

---

### 4.4 容差与正确性判定

#### 4.4.1 概念说明

kernel 是手写 CUDA、参考是 PyTorch 浮点，两者不可能逐位相等（bf16 舍入、并行归约顺序、base-2/base-1 指数实现差异都会产生误差）。所以判定「正确」必须用**容差**。FlashMLA 用 `kk.check_is_allclose(name, ans, ref, abs_tol, rel_tol, cos_diff_tol)`，它不是 PyTorch 自带的 `allclose`，而是一套**三重判定**：

1. **anomaly（异常值）逐位匹配**：`inf`、`-inf`、`nan` 出现的位置在 `ans` 和 `ref` 里必须**完全一致**（这是 lonely query `lse=+inf`、`out=0` 能通过的关键）。
2. **abs/rel 双门 OR 判定**：每个元素满足「绝对误差 < abs_tol **或** 相对误差 < rel_tol」即算通过。
3. **cos_diff 全局相似度**：整个张量的余弦相似度差异要小于 `cos_diff_tol`，作为「整体方向没跑偏」的兜底。

容差数值经过精心调校，和 bf16 精度挂钩：`rel_tol=2.01/128` 容忍约 2 个 bf16 ULP（\(1/128=2^{-7}\)）；lse 更敏感，用 `8.01/65536`、`2.01/65536`（\(1/65536=2^{-16}\)）这种紧得多的阈值。

#### 4.4.2 核心流程

`check_is_allclose` 内部的判定逻辑：

```
1. 形状/dtype 断言（ans 与 ref 必须同形同 dtype）
2. 异常值处理：对 inf / -inf / nan 三种值，
   分别取 ans 与 ref 的掩码，清零后比对掩码是否一致（不一致直接 FAIL）
3. 算 raw_abs_err = |ans-ref|，raw_rel_err = |ans-ref|/(|ref|+1e-6)
4. rel_err = raw_rel_err 在「abs 已达标」处置 0；abs_err = raw_abs_err 在「rel 已达标」处置 0
5. pass_mask = (abs_err < abs_tol) | (rel_err < rel_tol)
6. 若 pass_mask 全真 且 cos_diff < cos_diff_tol → 通过；否则打印最差点并 FAIL
```

第 4 步的 `masked_fill` 是为了**报告更干净**：把「已经被另一准则覆盖的误差」置零后，`argmax` 找出的最差点才是真正「两条准则都不满足」的元素。实际通过条件就是第 5 步的 OR。

绝对误差与相对误差的关系：对元素值 \(r\)（参考值）、\(a\)（实际值），

\[
e_{\text{abs}}=|a-r|,\qquad e_{\text{rel}}=\frac{|a-r|}{|r|+\varepsilon}
\]

大数值（如 `out` 的某些大分量）用相对误差更合理；接近 0 的数值（如 lonely query 的 `out=0` 附近）相对误差会爆炸，这时绝对误差兜底。OR 语义正是为此设计。

#### 4.4.3 源码精读

[tests/kernelkit/compare.py:31-60](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/kernelkit/compare.py#L31-L60) 是 `check_is_allclose` 的开头：先断言形状/dtype 一致，把两边都转成 `float`（fp32）再比，避免 bf16 比较的额外误差。`deal_with_anomalies` 对 `inf`/`-inf`/`nan` 三种值分别处理：用 `ref==val`（或 `ref!=ref` 判 nan）取掩码，**把掩码处置 0 后继续比数值**，同时要求掩码在 ans/ref 间**完全一致**。这条规则让 lonely query 的 `lse=+inf`、`out=0` 能通过——`inf` 掩码一致即放行。

[tests/kernelkit/compare.py:62-67](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/kernelkit/compare.py#L62-L67) 是双门 OR 判定的核心：

```python
cos_diff = get_cos_diff(ans, ref)
raw_abs_err = torch.abs(ans-ref)
raw_rel_err = raw_abs_err / (torch.abs(ref)+(1e-6))
rel_err = raw_rel_err.masked_fill(raw_abs_err<abs_tol, 0)
abs_err = raw_abs_err.masked_fill(raw_rel_err<rel_tol, 0)
pass_mask = (abs_err < abs_tol) | (rel_err < rel_tol)
```

注意分母里的 `+1e-6` 防 `ref` 为 0 时除零。

[tests/kernelkit/compare.py:69-92](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/kernelkit/compare.py#L69-L92) 的收尾：异常值不匹配直接 FAIL；`pass_mask` 不全真则打印最大 abs/rel 误差点的位置与数值、通过率、cos_diff；全真时再单独卡一道 `cos_diff < cos_diff_tol`。失败时打印的「pos / ans vs ref / 通过率」是排错第一手信息。

余弦相似度的定义见 [tests/kernelkit/compare.py:19-29](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/kernelkit/compare.py#L19-L29)：用 Dice 系数形式 \(1-\frac{2\sum ar}{\sum(a^2+r^2)}\)，`ref` 近零时直接返回 0。

各测试文件实际传入的容差（用于横向对比）：

| 测试 | 张量 | abs_tol | rel_tol | cos_diff_tol |
| --- | --- | --- | --- | --- |
| dense decode | out | `8e-4` | `2.01/128` | `5e-6` |
| dense decode | lse | `1e-6` | `8.01/65536` | — |
| sparse prefill | out | `8e-4` | `3.01/128` | `7e-6` |
| sparse prefill | max_logits | `1e-6` | `2.01/65536` | — |
| sparse prefill | lse | `1e-6` | `2.01/65536` | — |
| sparse decode | out | `1e-3` | `2.01/128` | `5e-6` |
| sparse decode | lse | `1e-6` | `8.01/65536` | — |

dense decode 取值见 [test_flash_mla_dense_decoding.py:170-171](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py#L170-L171)；sparse prefill 见 [test_flash_mla_sparse_prefill.py:46-48](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_prefill.py#L46-L48)；sparse decode 见 [test_flash_mla_sparse_decoding.py:228-229](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_decoding.py#L228-L229)。规律：`out` 用宽容差（bf16 ULP 量级），`lse`/`max_logits` 用紧容差（\(2^{-16}\) 量级），因为 lse 是 log 域、误差对下游 softmax 权重有放大效应。

#### 4.4.4 代码实践（核心实践任务）

**目标**：仿照 `test_flash_mla_dense_decoding.py`，为一个新的 `(b, s_q, s_k)` 组合编写一条 correctness 用例，并说明三重容差各自的作用。

**步骤**：

1. 在 `tests/test_flash_mla_dense_decoding.py` 的 `correctness_cases` 列表里追加一条手动构造的用例（绕开列表推导），例如 `b=3, s_q=2, s_k=500, h_q=64, h_kv=1`：

   ```python
   # 示例代码（非项目原有，需手动加入 correctness_cases 列表）
   correctness_cases.append(
       TestParam(3, 2, 500, is_varlen=False, is_causal=False,
                 test_performance=False, have_zero_seqlen_k=False,
                 block_size=64, h_q=64, h_kv=1)
   )
   ```

2. 运行 `python tests/test_flash_mla_dense_decoding.py`（需 SM90 GPU 与已编译的 `flash_mla`），观察这条用例的 `out`/`lse` 比对结果。

3. 阅读 [compare.py:62-67](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/kernelkit/compare.py#L62-L67)，准备口头解释三个容差。

**需要观察的现象**：`out` 比对通过（abs 或 rel 至少一条达标，且 cos_diff < 5e-6）；`lse` 比对通过（阈值更紧）。

**预期结果**：用例通过。**待本地验证**（无 SM90 环境时无法实际运行，但可在 CPU 上单独跑 `reference_torch` 验证参考侧逻辑，或用小规模随机张量手算一组 `out`/`lse` 与参考对照）。

**三重容差作用说明**（这是本实践的交付物）：

- **abs_tol（绝对容差）**：当参考值本身接近 0 时（如 lonely query 的 `out=0` 附近），相对误差会爆炸，此时用绝对容差兜底——只要 `|ans-ref|<abs_tol` 就算通过。dense decode 的 `out` 用 `8e-4`，lse 用 `1e-6`。
- **rel_tol（相对容差）**：当参考值较大时，关注相对幅度更合理，容许约 `2.01/128`（≈2 个 bf16 ULP）的相对误差。它与 abs_tol 是 **OR** 关系——任一达标即通过，兼顾「大数值」与「小数值」两种情况。
- **cos_diff_tol（余弦相似度容差）**：前两者是逐元素判定，cos_diff 是**整张量**的方向相似度兜底。即便每个元素都勉强通过 abs/rel，但整体方向跑偏（cos_diff 大），仍判失败。它捕捉 abs/rel 逐点判定漏掉的「系统性偏差」。

#### 4.4.5 小练习与答案

**练习 1**：`check_is_allclose` 里为什么先 `masked_fill` 再 `argmax` 找最差点，而不是直接对 `raw_abs_err` 取 `argmax`？

**答案**：直接对 `raw_abs_err` 取 `argmax`，最差点可能落在「绝对误差大但相对误差已经达标」的元素上——那其实是个通过点，报出来会误导排错。先 `masked_fill` 把「已被另一准则覆盖的误差」置零，`argmax` 找出的才是真正「abs 和 rel 都不达标」的失败元素，排错信息更准确。注意这只影响**报告**，不影响通过判定（判定用的是 `pass_mask` 的 OR）。

**练习 2**：为什么 `lse` 的 `rel_tol`（`8.01/65536`）比 `out` 的（`2.01/128`）紧这么多？

**答案**：lse 是 log 域的标量，会被下游用来做 softmax 归一化（`exp(P-lse)`），lse 的小误差会被指数放大成 softmax 权重的大误差。所以 lse 必须用紧容差（\(2^{-16}\) 量级）卡准。而 `out` 已经是加权求和后的结果，误差不再被放大，用 bf16 ULP 量级（\(2^{-7}\)）的宽容差即可。

**练习 3**：如果一个 query 是 lonely query（无有效 key），kernel 返回 `lse=+inf, out=0`，参考也是 `+inf, 0`，`check_is_allclose` 会如何判定？

**答案**：异常值处理阶段，`deal_with_anomalies(+inf)` 会取 ans/ref 的 `inf` 掩码，二者一致（都在同一位置）即放行，并把掩码处置 0；`out` 在 0 附近靠 abs_tol 兜底通过。所以 lonely query 能正确通过——这正是异常值逐位匹配机制的设计目的。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「为 sparse prefill 新增一条 corner 用例并理解它如何被校验」的端到端任务。

**任务**：为 `tests/test_flash_mla_sparse_prefill.py` 新增一条 corner 用例，覆盖「`s_kv` 远小于 `topk` 且开启 attn_sink 与 topk_length」的场景，并追踪它从参数到判定的完整链路。

**操作步骤**：

1. **造参数**：在 `corner_cases` 列表（113 行起）追加一条，例如 `TestParam(s_q=1, s_kv=16, topk=512, h_q=128, is_all_indices_invalid=False, num_runs=0, have_attn_sink=True, have_topk_length=True, d_qk=576)`。这里 `s_kv=16 < topk=512`，意味着每个 query 最多只有 16 个有效 key，却有 512 个索引槽——绝大多数索引无效。

2. **造数据**：跟踪 `lib.generate_testcase`（121 行）如何用这条参数造 `indices`——`_randperm_batch` 会把超出 `s_kv=16` 的位置填成 `invalid_indices_candidate` 里的越界值；`have_topk_length=True` 还会额外生成一个 `[s_q]` 的截断长度。

3. **跑参考**：跟踪 `ref.ref_sparse_attn_fwd`（19 行）如何处理——`invalid_mask=(indices<0)|(indices>=16)` 会把绝大多数位置标无效，`P` 在这些位置置 `-inf`，最终 `orig_lse` 可能是 `-inf`（若 `topk_length` 把所有有效 key 也截掉了），触发 lonely query 分支。

4. **比对**：跟踪 `run_test`（13 行）第 46-48 行的三组 `check_is_allclose`，注意 `out` 用 `cos_diff_tol=7e-6`、`lse`/`max_logits` 用 `2.01/65536`。

5. **判定**：若该用例 `orig_lse` 全 `-inf`（lonely），参考把 lse 改 `+inf`、out 置 0；kernel 应返回一致结果，异常值掩码匹配后通过。

**预期结果**：用例通过，且你能完整说出「参数 → `_randperm_batch` 造越界索引 → `ref` 的 invalid_mask 与 lonely 分支 → `check_is_allclose` 的异常值匹配与三重容差」这条链路。**待本地验证**（需 SM90/SM100 GPU 与编译好的扩展；无 GPU 时可在 CPU 上单独跑 `ref.ref_sparse_attn_fwd` 与 `lib.generate_testcase` 验证参考侧与数据生成侧的逻辑自洽）。

## 6. 本讲小结

- `tests/lib.py` 是数据工厂：`TestParam`/`Testcase`/`KVScope` 把形状与开关翻译成带边界条件的随机张量，`_randperm_batch` 故意掺入 `-1`/`s_kv`/`INT32_MAX` 等无效索引候选来逼出边界 bug。
- `tests/ref.py` 是 sparse 路径的数值黄金参考：`_merge_two_lse` 用 logsumexp 合并 lse 与 attn_sink，prefill 通过改「算 out 的 lse」缩放、decode 直接对 out 乘因子，两者数学等价且对外 lse 都不含 sink；lonely query 一律 `out=0, lse=+inf`。dense decode 的参考 `reference_torch` 写在测试文件里而非 `ref.py`。
- 用例分三类：correctness（穷举正常形状、只验正确性）、corner（零长度 KV/全无效索引/超大 s_q 等边界）、performance（生产形状、测 TFlops/GB/s，靠 `bench_kineto` 的 CUPTI 采样与 L2 刷缓存）。
- `kk.check_is_allclose` 是三重判定：异常值（inf/-inf/nan）逐位掩码匹配 + abs/rel 双门 OR + cos_diff 全局兜底；容差与 bf16 精度挂钩（out 用 `2.01/128`、lse 用 `8.01/65536`），lse 因被指数放大而用紧容差。
- 性能测量用 `bench_kineto` 按内核名取时间（`get_kernel_time`/`get_e2e_time`），FLOPs 与访存量由 `lib.count_flop_and_mem_vol*` 与 kernel 无关地算出，相除得 TFlops/GB/s。

## 7. 下一步学习建议

- 想深入性能测量与多实现对比，读 u8-l3（benchmark、kernelkit 与性能调优），它会展开 `benchmark/bench_flash_mla.py` 对比 torch/flashinfer/triton 以及 NVCC 性能 flag。
- 想理解 FP8 量化与 `KVScope.quant_and_dequant_` 背后的字节布局，读 u5-l1（FP8 KV Cache 布局与量化）。
- 想看 split-KV 的 lse 合并数学如何体现在 combine kernel 而非参考实现，读 u4-l1/u4-l2。
- 建议动手：挑一条 sparse decode 的 corner 用例（`tests/test_flash_mla_sparse_decoding.py` 的 `gen_testcase`），对照 `ref.ref_sparse_attn_decode` 画出 extra KV scope 的 concat 与 attn_sink 缩放流程图，作为本讲内容的延伸验证。
