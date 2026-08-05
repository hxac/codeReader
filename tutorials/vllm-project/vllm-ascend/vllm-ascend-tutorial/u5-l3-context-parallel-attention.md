# 上下文并行注意力（CP）

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「上下文并行（Context Parallel，CP）」要解决什么问题，以及 vllm-ascend 里它的具体形态——**Decode Context Parallel（DCP）**。
- 理解 `enable_dcp()` 这个开关如何让标准注意力后端在运行时「切换」到 CP 实现，并知道 GQA、MLA、SFA/DSA 三类后端各自切换到哪一个 CP 变体。
- 读懂 CP 元数据的构造过程：interleave 感知的「本地 KV 长度」如何计算、`common_cp.py` 提供了哪些公共能力（DCP group、all-gather、LSE 合并）。
- 区分三类后端的 CP 分片策略：GQA 按 head 切、MLA 在隐空间 gather KV、SFA 复制 indexer 而分片大 KV、DSA 按 token 切。
- 独立完成实践任务：说明 `enable_dcp()` 为真时 `get_impl_cls` 切换到哪个实现，并解释为何 MLA 与 SWA-MLA（SFA）可以分别独立地做 CP。

## 2. 前置知识

在阅读本讲前，建议你已经掌握 u5-l1（注意力后端注册与元数据）和 u5-l2（MLA/SFA/DSA）。本讲用到但需要再强调的几个概念：

- **注意力后端（Attention Backend）**：vLLM 把「如何算注意力」抽象成后端，由平台钩子 `NPUPlatform.get_attn_backend_cls` 根据模型特征 `(use_mla, use_sparse, use_compress)` 选中一个后端类。后端类里有 `get_impl_cls()`（返回真正算注意力的实现类）和 `get_builder_cls()`（返回元数据构建器）两个工厂方法。
- **KV cache 与序列维度**：注意力计算中，Query 对一整段历史的 Key/Value 求注意力。这段历史的长度就是「序列维度（sequence dimension）」。当序列很长（几十万 token）时，KV cache 会占用惊人的显存。
- **TP / SP**：张量并行把权重沿 head 维切分到多卡；序列并行（SP）把激活沿 token 维切分。它们都不切 KV cache 的「序列维度」——每个 TP rank 仍然持有自己那部分 head 对应的全部历史 KV。
- **Flash Attention 的 LSE**：FlashAttention 在分块计算时会同时输出每个 query 的 attention 输出 \(O\) 和一个对数求和Exp值 \(l=\log\sum_j e^{s_j}\)（log-sum-exp，简称 LSE）。把多个分块的 \((O_i, l_i)\) 合并成全局结果，靠的是下面的「在线 softmax」公式。

把多卡各自只看一段 KV 得到的「局部结果」合并成「看完整序列的结果」，正是 CP 的核心数学：

\[
l = \log\sum_i \exp(l_i), \qquad O = \sum_i \exp(l_i - l)\cdot O_i
\]

只要每个 DCP rank 各自算出针对自己那段 KV 的 \((O_i, l_i)\)，就能无损合并出整序列的注意力。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `vllm_ascend/attention/context_parallel/common_cp.py` | CP 公共能力：DCP group 发现、interleave 本地长度计算、all-gather、partial 输出与 LSE 合并 |
| `vllm_ascend/attention/context_parallel/attention_cp.py` | **GQA** 后端的 DCP 实现（标准 MHA/GQA 模型） |
| `vllm_ascend/attention/context_parallel/mla_cp.py` | **MLA** 后端的 DCP 实现（DeepSeek 类隐式 KV 压缩模型） |
| `vllm_ascend/attention/context_parallel/sfa_cp.py` | **SFA** 后端的 DCP 实现（GLM-5.2 类稀疏+SWA 注意力，复制 indexer） |
| `vllm_ascend/attention/context_parallel/dsa_cp.py` | **DSA-CP** 实现（DeepSeek-V4 类，按 token 切分 + 压缩器/indexer） |
| `vllm_ascend/attention/utils.py` | `enable_dcp()` 开关、`AscendDCPMetadata`、公共元数据 |
| `vllm_ascend/worker/dcp_utils.py` | 在 model runner 侧为每个请求生成 DCP 元数据（`num_computed_tokens_of_dcp` 等） |
| `vllm_ascend/attention/attention_v1.py` / `mla_v1.py` / `sfa_v1.py` | 三类后端各自的 `get_impl_cls`/`get_builder_cls` 分发点 |
| `docs/source/developer_guide/Design_Documents/context_parallel.md` | DCP 设计文档（KV 布局、backend 结构、prefill/decode 流程） |

---

## 4. 核心概念与源码讲解

### 4.1 上下文并行（CP）机制总览

#### 4.1.1 概念说明：为什么需要 CP / DCP

长序列推理的最大瓶颈是 **KV cache 显存**：一个请求越长，它累积的 Key/Value 越多。在普通 TP 下，每个 TP rank 都要把自己负责的 head 对应的**全部历史 KV** 存一份——也就是说，同一段序列的 KV 在多个 rank 上被重复存储（只是 head 不同）。

**上下文并行（Context Parallel）** 的思路是：把 KV cache 沿**序列维度**切分到一组设备上，每个设备只存序列的一段，从而消除这种冗余存储，且不需要增加进程数（world size 不变）。

vllm-ascend 实现的是其中的 **Decode Context Parallel（DCP）**，它在 **TP 组内部**沿序列维度分片 KV。设计文档第一段就点明了定位：

> Decode Context Parallel shards the KV cache along the sequence dimension across devices in a Tensor Parallel (TP) group. It eliminates redundant KV-cache storage without adding devices to the process world.

参见 [docs/source/developer_guide/Design_Documents/context_parallel.md:1-5](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/docs/source/developer_guide/Design_Documents/context_parallel.md#L1-L5)（说明 DCP 的目标与边界：Prefill Context Parallel 不被支持，文档只讲 DCP 与 DSA-CP）。

代价是：每个 rank 只能看到一段 KV，单卡算出的注意力是「局部」的，必须跨 rank 通信合并。如何用尽量少的通信得到与单卡完全等价的结果，是 DCP 全部复杂度的来源。

#### 4.1.2 核心流程：开关、分发与 KV 布局

DCP 的总流程可以用三步概括：

1. **开关**：用户在启动参数里设置 `decode_context_parallel_size > 1`（即 DCP size）。运行时函数 `enable_dcp()` 据此返回 True。
2. **分发**：每个注意力后端类的 `get_impl_cls()` / `get_builder_cls()` 在 `enable_dcp()` 为真时，**延迟 import 并返回 CP 变体类**，否则返回普通类。这样同一份模型代码，开关一开就自动走 CP 路径。
3. **KV 布局 + 合并**：KV cache 按 interleave 布局分散到各 rank；前向时每个 rank 算局部 \((O_i, l_i)\)，再用 all-gather / all-to-all + LSE 合并出全局结果。

KV 的 interleave 布局是理解后续一切元数据的基础。设计文档给出（`cp_kv_cache_interleave_size` 记为 \(I\)，DCP size 记为 \(D\)，`block_size` 记为 \(B\)）：

- 虚拟块大小 \(= B \cdot D\)（一个虚拟块横跨所有 DCP rank）；
- 对 token \(x\)：`virtual_block_index = x // (B*D)`，`offset = x % (B*D)`，`local_block_index = offset // I`，`target_rank = local_block_index % D`。

参见 [docs/source/developer_guide/Design_Documents/context_parallel.md:7-18](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/docs/source/developer_guide/Design_Documents/context_parallel.md#L7-L18)。直观地说：token 被轮流分给各个 rank，每个 rank 拿一「条」宽度为 \(I\) 的数据。默认 \(I=1\) 时就是最朴素的逐 token 交错。

#### 4.1.3 源码精读：开关与三类后端的分发点

**① 全局开关 `enable_dcp()`**：用 `lru_cache` 缓存，只看 `decode_context_parallel_size` 是否大于 1。

[vllm_ascend/attention/utils.py:181-184](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/utils.py#L181-L184) —— 读取并行配置判定 DCP 是否开启：

```python
@lru_cache(maxsize=1)
def enable_dcp():
    parallel_config = get_current_vllm_config().parallel_config
    return parallel_config.decode_context_parallel_size > 1
```

**② GQA（标准 MHA）后端的分发**：这是普通 Transformer 模型走的后端。

[vllm_ascend/attention/attention_v1.py:84-97](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/attention_v1.py#L84-L97) —— 开启 DCP 时返回 GQA 的 CP 实现与构建器：

```python
@staticmethod
def get_impl_cls() -> type["AscendAttentionBackendImpl"]:
    if enable_dcp():
        from vllm_ascend.attention.context_parallel.attention_cp import AscendAttentionDCPImpl
        return AscendAttentionDCPImpl
    return AscendAttentionBackendImpl

@staticmethod
def get_builder_cls() -> type["AscendAttentionMetadataBuilder"]:
    if enable_dcp():
        from vllm_ascend.attention.context_parallel.attention_cp import AscendAttentionDCPMetadataBuilder
        return AscendAttentionDCPMetadataBuilder
    return AscendAttentionMetadataBuilder
```

**③ MLA 后端的分发**（DeepSeek 类）：

[vllm_ascend/attention/mla_v1.py:81-105](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/mla_v1.py#L81-L105) —— MLA 后端同样用 `enable_dcp()` 切换到 `AscendMlaDCPImpl` / `AscendMlaDCPMetadataBuilder`。

**④ SFA 后端的分发**（GLM-5.2 类，稀疏 + SWA）：注意它的门控不是 `enable_dcp()`，而是 `enable_sfa_dcp_replicated_indexer()`。

[vllm_ascend/attention/sfa_v1.py:122-146](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/sfa_v1.py#L122-L146) —— SFA 后端按「复制 indexer」策略切换到 `AscendSFADCPImpl` / `AscendSFADCPMetadataBuilder`。

而 `enable_sfa_dcp_replicated_indexer()` 本质上也是「SFA 稀疏模型 且 `decode_context_parallel_size > 1`」：

[vllm_ascend/utils.py:122-129](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py#L122-L129) —— SFA DCP 的启用条件。

**⑤ 平台层如何把模型路由到对应后端**：`NPUPlatform.get_attn_backend_cls` 用 `(use_mla, use_sparse, use_compress)` 三元组查 `backend_map`，决定一个模型用 MLA / SFA / DSA / 标准 GQA 后端中的哪一个。每个被选中的后端再各自决定是否走 CP。

[vllm_ascend/platform.py:803-822](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L803-L822) —— `backend_map` 把模型特征映射到具体后端类路径（返回的是字符串路径，由 vLLM 延迟 import）。

```python
backend_map = {
    (True, False, False): "vllm_ascend.attention.mla_v1.AscendMLABackend",
    (False, False, False): "vllm_ascend.attention.attention_v1.AscendAttentionBackend",
    (True, True, False):  "vllm_ascend.attention.sfa_v1.AscendSFABackend",
    (True, False, True):  "vllm_ascend.attention.dsa_v1.AscendDSABackend",
}
```

> 关键结论：**CP 不是独立的后端，而是每个后端内部的一个「变体」**。设计文档把这一点表述为「DCP 是相应 v1 后端的特化（specialization），而不是它的平行副本」。参见 [docs/source/developer_guide/Design_Documents/context_parallel.md:24-39](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/docs/source/developer_guide/Design_Documents/context_parallel.md#L24-L39)。

#### 4.1.4 代码实践：开启 DCP 跑长序列

**实践目标**：通过一个真实示例，看清「用户侧只需加一个参数」就能开启 DCP。

**操作步骤**：

1. 阅读 `examples/offline_inference_npu_long_seq.py`，它是 vllm-ascend 自带的长序列 DCP 示例。
2. 找到开启 DCP 的关键参数：

[vllm_ascend-tutorial 示例引用 —— examples/offline_inference_npu_long_seq.py:44-53](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/examples/offline_inference_npu_long_seq.py#L44-L53) —— 同时设置 TP 与 DCP：

```python
llm = LLM(
    model=args.model_path,
    trust_remote_code=True,
    enforce_eager=True,
    tensor_parallel_size=args.tp,
    decode_context_parallel_size=args.dcp,   # <- 这一行开启 DCP
    enable_prefix_caching=False,
    enable_chunked_prefill=False,
    block_size=128,
    ...
)
```

   命令行参数 `--dcp` 默认为 2（见同文件 `--dcp` 的 argparse 定义）。

**需要观察的现象**：DCP 开启后，单卡 KV cache 占用应近似减半（dcp=2 时）；生成结果与不开 DCP 时应一致（无损）。

**预期结果**：脚本会打印 `TTFT`（首 token 延迟）与每条 prompt 的生成文本。

> 说明：本实践需要真实的昇腾 NPU 环境与 CANN/torch-npu 才能运行。若你当前在无 NPU 的环境，请只做源码阅读：确认 `decode_context_parallel_size` 这个参数会进入 `parallel_config`，进而被 `enable_dcp()` 读取——这条链路即「参数 → 开关 → 后端切换」。运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `decode_context_parallel_size` 设为 1，`enable_dcp()` 返回什么？三类后端的 `get_impl_cls` 分别返回哪个类？

> **答案**：返回 False（`1 > 1` 为假）。GQA 返回 `AscendAttentionBackendImpl`、MLA 返回 `AscendMLAImpl`、SFA 返回 `AscendSFAImpl`——即全部走「非 CP」的普通实现。

**练习 2**：DCP 与 TP 在「切分维度」上的根本区别是什么？

> **答案**：TP 沿 head 维切分权重与 KV（每个 rank 持有部分 head 的**完整序列** KV）；DCP 沿序列维切分 KV（每个 rank 持有**部分序列**的 KV）。两者正交，DCP 在 TP 组内部再沿序列维切。

---

### 4.2 CP 元数据与公共能力（common_cp.py）

DCP 的复杂度集中在「元数据」上：每个 rank 必须知道自己这段 KV 有多长、属于哪些请求，以及如何把局部结果合并。`common_cp.py` 把这些跨后端共享的能力抽成了两个 Mixin 和几个纯函数。

#### 4.2.1 概念说明：DCP group 与 interleave 感知的本地长度

DCP 把 TP 组再按序列维度划分出一个 **DCP group**（即上游 vLLM 的 `dcp_group`）。`DCPImplMixin` 和 `DCPMetadataBuilderMixin` 在初始化时都从 `get_dcp_group()` 取出 `dcp_size`（组内卡数）和 `dcp_rank`（本卡编号）。

对每个请求，给定它的总序列长度 \(L\)，需要算出「在 interleave 布局下，rank \(r\) 实际持有多少 token」。这就是 `get_dcp_local_seq_lens()` 做的事，它是所有 CP 元数据的数学起点。

#### 4.2.2 核心流程：本地长度公式与 LSE 合并

**① 本地长度公式**（\(L\) 总长，\(D\) dcp_size，\(I\) interleave_size）：

\[
\text{base} = \left\lfloor \frac{L}{I \cdot D} \right\rfloor \cdot I,\quad
\text{remainder} = L - \text{base}\cdot D
\]
\[
\text{local}(r) = \text{base} + \min\!\bigl(\max(\text{remainder} - r\cdot I,\ 0),\ I\bigr)
\]

含义：每 \(D\cdot I\) 个 token 为一轮，每轮里每个 rank 拿连续 \(I\) 个；`base` 是「完整轮」贡献，`remainder` 是最后不满一轮的部分，按 rank 顺序依次填满宽度 \(I\)，剩余 rank 拿 0。

**② 局部结果合并**：每个 rank 用本地 KV 算出 \((O_i, l_i)\) 后，`_merge_dcp_attention_output` 先用 all-to-all 把各 rank 的输出重排，再调 CANN 算子 `npu_attention_update` 做在线 softmax 合并（即第 2 节的公式）。GQA、MLA、SFA 三条前向路径都复用这个合并函数。

#### 4.2.3 源码精读：四个公共能力

**① interleave 本地长度计算** `get_dcp_local_seq_lens`：

[vllm_ascend/attention/context_parallel/common_cp.py:11-29](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/context_parallel/common_cp.py#L11-L29) —— 向量化实现上面的本地长度公式，返回形状 `[num_reqs, dcp_size]`（每个请求在**每个** rank 上的长度），各 rank 再用 `[:, self.dcp_rank]` 取自己的那一列。

```python
def get_dcp_local_seq_lens(seq_lens, dcp_size, interleave_size):
    tiled = seq_lens.unsqueeze(-1)
    rank_offsets = torch.arange(dcp_size, dtype=seq_lens.dtype, device=seq_lens.device)
    base = tiled // interleave_size // dcp_size * interleave_size
    remainder = tiled - base * dcp_size
    return base + torch.clamp(remainder - rank_offsets * interleave_size, 0, interleave_size)
```

**② 元数据构建 Mixin** `DCPMetadataBuilderMixin`：负责发现 DCP group，并从公共元数据里取出 DCP 专属字段（`context_parallel_metadata`）。

[vllm_ascend/attention/context_parallel/common_cp.py:32-82](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/context_parallel/common_cp.py#L32-L82) —— 提供 `_require_dcp_metadata`（断言 DCP 元数据已填充）、`_get_dcp_context_lens`（取出 `[num_reqs, dcp_size]` 的本地长度矩阵）、`_get_dcp_rank_context_lens`（取本 rank 列）。GQA/MLA/SFA 的 CP 构建器都继承它，从而共享同一套「取本地长度」逻辑。

**③ 通信 Mixin** `DCPImplMixin`：负责 DCP 集合通信与结果合并。

[vllm_ascend/attention/context_parallel/common_cp.py:85-136](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/context_parallel/common_cp.py#L85-L136) —— 提供 `_dcp_all_gather`（沿某维 all-gather，dcp_size==1 时直通）、`_dcp_all_gather_fragments`（把多个张量拼接后一次 all-gather 再拆回，省通信次数）、`_merge_dcp_attention_output`（合并局部输出与 LSE）。

**④ LSE 合并的两个底层函数**：

- [vllm_ascend/attention/context_parallel/common_cp.py:139-165](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/context_parallel/common_cp.py#L139-L165) `_process_attn_out_lse` —— 把 `attn_output` 与 `softmax_lse` 拼成 `[bs, num_heads, v_head_dim+1]`，转置后用 `dist.all_to_all_single` 在 DCP 组内交换，让每个 rank 拿到「全部 head、本 rank 序列段」的数据。
- [vllm_ascend/attention/context_parallel/common_cp.py:168-195](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/context_parallel/common_cp.py#L168-L195) `_npu_attention_update` —— reshape 后调用 `torch_npu.npu_attention_update(lse_list, out_list, 0)`，由 CANN 算子在 NPU 上完成在线 softmax 合并。

另外还有一个纯 PyTorch 的等价实现 `_update_out_and_lse`，可作为理解数学的参考：

[vllm_ascend/attention/context_parallel/common_cp.py:222-233](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/context_parallel/common_cp.py#L222-L233) —— 这段几乎就是第 2 节公式的直译：

```python
def _update_out_and_lse(out_list, lse_list):
    lse_final = torch.logsumexp(lse_list, dim=0, keepdim=False)
    out_final = torch.sum(torch.exp(lse_list - lse_final) * out_list, dim=0)
    return out_final, lse_final
```

**⑤ 元数据从哪里来**：在 model runner 侧，`dcp_utils.py` 的 `generate_dcp_metadata` 把每个请求的 `context_len`（已计算 token 数）喂给 `get_dcp_local_seq_lens`，得到 `num_computed_tokens_of_dcp`（即每个请求在每个 rank 上的本地长度矩阵），打包成 `AscendDCPMetadata`。

[vllm_ascend/worker/dcp_utils.py:619-627](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/dcp_utils.py#L619-L627) —— 构造 DCP 元数据的核心：

```python
local_seq_lens = self._get_dcp_local_seq_lens(torch.tensor(context_lens))
...
metadata = AscendDCPMetadata(
    num_computed_tokens_of_dcp=local_seq_lens.numpy(),
    query_lens_cpu=query_lens_cpu,
    max_query_len=...,
)
```

#### 4.2.4 代码实践：手算 interleave 切分

**实践目标**：用纸笔（或 Python）验证 `get_dcp_local_seq_lens` 的输出，建立对 interleave 布局的直觉。

**操作步骤**：

1. 取 \(D=2\)（dcp_size）、\(I=1\)（默认 interleave_size）、请求序列长度 \(L=5\)。
2. 代入公式：`base = (5 // 1 // 2) * 1 = 2`；`remainder = 5 - 2*2 = 1`。
   - rank 0：`2 + clamp(1 - 0, 0, 1) = 3`
   - rank 1：`2 + clamp(1 - 1, 0, 1) = 2`
3. 验证：两 rank 之和 \(3+2=5=L\)，正确。含义是 token 序号 `{0,2,4}` 归 rank 0，`{1,3}` 归 rank 1。
4.（可选）写 3 行示例代码调用该函数（无需 NPU，纯 CPU 张量）：

```python
# 示例代码（非项目原有代码），可在任意带 torch 的环境运行
import torch
from vllm_ascend.attention.context_parallel.common_cp import get_dcp_local_seq_lens
print(get_dcp_local_seq_lens(torch.tensor([5]), dcp_size=2, interleave_size=1))
# 预期: tensor([[3, 2]])
```

**需要观察的现象**：返回矩阵每行求和应等于原序列长度；每列对应一个 rank。

**预期结果**：`tensor([[3, 2]])`，与手算一致。

> 说明：上面这 3 行是**示例代码**，不是项目原有文件。若当前环境未安装 `vllm_ascend`，可只用步骤 1–3 的手算部分；调用结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`_dcp_all_gather_fragments` 为什么要先把多个张量 `cat` 起来再 all-gather，最后再 `split` 回去？

> **答案**：把多次 all-gather 合并成一次，减少集合通信的启动次数与同步开销。在昇腾上，连续的 all-gather 之间会有不必要的 stream 依赖，合并后更高效（代码注释也说明了这一点）。

**练习 2**：为什么 LSE 合并用 `logsumexp` 而不是直接 `sum`？

> **答案**：在线 softmax 要求加权平均的权重是 \(\exp(l_i - l)\)，它必须先用 `logsumexp` 算出归一化常数 \(l=\log\sum_i\exp(l_i)\)，否则数值上会溢出/丢精度。直接 `sum` 得不到正确的注意力合并结果。

---

### 4.3 GQA / MLA / SFA-DSA 三条 CP 前向路径

三类后端的 KV 布局完全不同（标准显式 KV、MLA 隐式压缩 KV、SFA 稀疏 + indexer），所以它们的 CP 分片策略也不同。本节对照讲解，并完成指定的实践任务。

#### 4.3.1 概念说明：三种分片策略

- **GQA（标准 MHA/GQA）**：沿 **head 维**切。decode 时把各 rank 的 query heads all-gather 到一起（`num_heads *= dcp_size`），每个 rank 用自己的本地 KV 算出 partial \((O_i, l_i)\)，再合并。prefill/chunked-prefill 时类似，并支持「计算-通信重叠」的多流调度。
- **MLA（DeepSeek 类）**：沿 **序列维**切 KV，但 KV 存在低维隐空间（`kv_lora_rank`）。prefill 时 gather 跨 rank 的上下文 KV、**恢复成请求连续顺序**再算；decode 时 gather query 片段（`q_nope`/`q_pe`）后用本地隐空间 KV 算，再合并。
- **SFA（GLM-5.2 类，含 SWA）**：**复制 indexer、分片大 KV**。LightningIndexer 的缓存在每个 rank 上都有一份（保证选出的 sparse top-k 块与非 CP 一致），但占大头的 SFA KV 仍然分片；indexer 选出的全局 top-k 索引会被「重映射」成本地 KV 索引再做稀疏注意力。
- **DSA-CP（DeepSeek-V4 类）**：在 SFA 基础上再加压缩器（compressor），并**沿 token 维**切分序列（区别于 SFA 的 head/indexer 复制策略），需要 FlashComm1（SP）支持。

#### 4.3.2 核心流程：三类后端 decode/prefill 的通信

| 后端 | decode 通信 | prefill 通信 | 合并方式 |
| --- | --- | --- | --- |
| GQA | all-gather Q heads（沿 head 维） | all-gather Q + all-to-all 合并上下文输出 | `_merge_dcp_attention_output`（CANN `npu_attention_update`） |
| MLA | all-gather Q 片段（`q_nope`/`q_pe`） | all-gather KV 后 `_reorg_kvcache` 恢复请求连续顺序 | 同上 |
| SFA | all-gather Q 片段 + 重映射 sparse 索引 + all-to-all 合并 | 压缩 block table 后 all-gather 引用到的 KV 块 | `_merge_dcp_outputs`（softmax 加权） |
| DSA-CP | 沿 token 切，all-to-all 还原 head | full-gather `o_proj` 权重做全 head 输出投影 | TP all-to-all 还原 |

设计文档对 GQA 与 MLA 的 prefill/decode 流程有图示说明，参见 [docs/source/developer_guide/Design_Documents/context_parallel.md:41-54](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/docs/source/developer_guide/Design_Documents/context_parallel.md#L41-L54)。

#### 4.3.3 源码精读：三条路径的关键方法

**① GQA DCP decode**：`AscendAttentionDCPImpl._forward_decode_dcp`

[vllm_ascend/attention/context_parallel/attention_cp.py:278-300](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/context_parallel/attention_cp.py#L278-L300) —— all-gather query heads，把 head 数放大 `dcp_size` 倍，本地 KV 用 `num_computed_tokens_of_dcp[:, dcp_rank]` 作为 `actual_seq_lengths_kv`：

```python
if self.dcp_size > 1:
    query = self._dcp_all_gather(query, 1)      # 沿 head 维 gather
    num_heads = self.num_heads * self.dcp_size
else:
    num_heads = self.num_heads
```

随后调用 CANN `npu_fused_infer_attention_score` 得到 `(attn_out, attn_lse)`，最后交给公共的 `_merge_dcp_attention_output` 合并（见 [attention_cp.py:389-393](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/context_parallel/attention_cp.py#L389-L393)）。

GQA 的完整 `forward_impl` 把 decode 与 chunked-prefill 拼在一起，并用独立 stream 做「计算-通信重叠」：

[vllm_ascend/attention/context_parallel/attention_cp.py:546-610](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/context_parallel/attention_cp.py#L546-L610) —— 注意注释里画的双流时序：current_stream 跑当前 chunk 的 head/tail 注意力，`cp_chunkedprefill_comm_stream` 跑 Q 的 all-gather 与输出的 all-to-all，两者重叠。

**② MLA DCP decode 与 KV reorg**：

[vllm_ascend/attention/context_parallel/mla_cp.py:283-308](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/context_parallel/mla_cp.py#L283-L308) —— MLA decode：query 片段经 `reorg_decode_q`（即 `_dcp_all_gather_fragments`）gather，`actual_seq_lengths_kv` 用本 rank 的 `cp_seq_len`（本地上下文长度），KV 在 `kv_lora_rank` 隐空间。

prefill（chunked）时 MLA 需要把跨 rank gather 来的 KV「恢复成请求连续顺序」，这正是 `_reorg_kvcache` 的职责，其文档注释给了清晰的例子：

[vllm_ascend/attention/context_parallel/mla_cp.py:452-486](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/context_parallel/mla_cp.py#L452-L486) —— 例：rank0 的 KV 是 `[T0_0,T0_1,T0_2,T0_3,T1_0,...]`，rank1 是 `[T0_4,T0_5,pad,pad,...]`，all-gather 后要重排成请求连续的 `[T0_0..T0_5, T1_0..]`。

**③ SFA DCP：复制 indexer + 索引重映射**

SFA 的关键难题是：indexer 要在全序列上选 sparse top-k 块，但大头的 SFA KV 又想分片省显存。解法是 indexer 缓存复制、SFA KV 分片，再把 indexer 选出的「全局索引」重映射为「本地索引」。

[vllm_ascend/attention/context_parallel/sfa_cp.py:533-567](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/context_parallel/sfa_cp.py#L533-L567) `_remap_sparse_indices` —— 把复制 indexer 视图下的 top-k 索引，按 interleave/dcp_size 规则换算成本地 KV 的索引，并丢弃不属于本 rank 的索引：

```python
local_block_indices = torch.floor(topk_indices_fp32 / interleave_size)
local_owner_base = torch.floor(local_block_indices / self.dcp_size) * self.dcp_size
local_owner = local_block_indices - local_owner_base
local_owner_mask = (topk_indices_fp32 >= 0) & (local_owner == self.dcp_rank)
```

而 builder 侧负责临时构造「复制视图」的 block table 与 slot mapping，让 indexer 以为它看到的是完整序列：

[vllm_ascend/attention/context_parallel/sfa_cp.py:171-205](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/context_parallel/sfa_cp.py#L171-L205) `_build_block_table_replicated_view` —— 由 DCP-local block table 派生出 indexer 用的「复制视图」。

**④ DSA-CP：按 token 切 + o_proj 全权重**

DSA-CP 把序列沿 token 维切到各 rank，需要 SP（FlashComm1）支持，开启门控见 [vllm_ascend/utils.py:1371-1391](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py#L1371-L1391)（`enable_dsa_cp` 要求 `enable_sp()`）。元数据构建的核心是 `_build_local_token_metadata`，它把扁平的 token 流均匀切到各 TP rank，其文档注释里有完整数值示例：

[vllm_ascend/attention/context_parallel/dsa_cp.py:808-833](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/context_parallel/dsa_cp.py#L808-L833) —— 给出 TP=3 时 9 个请求、45 个 token 如何切到 rank 1（`local_start=15, local_end=30`）的例子。

#### 4.3.4 代码实践：`enable_dcp()` 为真时切换到哪个实现？为何 MLA/SWA-MLA 可分别做 CP？

这是本讲指定的实践任务。它是一个**源码阅读型实践**，目标是把「开关 → 分发 → 各自 CP 实现」这条链路彻底走通。

**实践目标**：

1. 说清 `enable_dcp()` 为真时，GQA / MLA / SFA 三类后端的 `get_impl_cls` 分别返回哪个实现类。
2. 解释为什么 MLA 与 SWA-MLA（即 SFA）可以**各自独立**地做 CP，而不会互相干扰。

**操作步骤**：

1. 打开 [vllm_ascend/attention/attention_v1.py:84-89](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/attention_v1.py#L84-L89)：标准 GQA 后端在 `enable_dcp()` 为真时返回 `AscendAttentionDCPImpl`。
2. 打开 [vllm_ascend/attention/mla_v1.py:100-104](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/mla_v1.py#L100-L104)：MLA 后端返回 `AscendMlaDCPImpl`。
3. 打开 [vllm_ascend/attention/sfa_v1.py:141-146](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/sfa_v1.py#L141-L146)：SFA（SWA-MLA）后端返回 `AscendSFADCPImpl`。
4. 再看平台路由 [vllm_ascend/platform.py:803-808](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L803-L808)：`use_mla=True,use_sparse=False` → MLA 后端；`use_mla=True,use_sparse=True` → SFA 后端。**一个模型只会被路由到其中一个后端**。

**参考答案（你要写出自己的版本）**：

> 当 `enable_dcp()` 为真时：
> - 普通 GQA 模型走 `AscendAttentionBackend`，`get_impl_cls` 返回 **`AscendAttentionDCPImpl`**（位于 `attention_cp.py`）；
> - MLA 模型走 `AscendMLABackend`，返回 **`AscendMlaDCPImpl`**（位于 `mla_cp.py`）；
> - SFA（SWA-MLA）模型走 `AscendSFABackend`，门控是 `enable_sfa_dcp_replicated_indexer()`，返回 **`AscendSFADCPImpl`**（位于 `sfa_cp.py`）。
>
> MLA 与 SWA-MLA（SFA）能分别独立做 CP，原因有二：
> 1. **路由独立**：平台 `backend_map` 按 `(use_mla, use_sparse)` 把模型分到不同后端类，每个后端类各自定义自己的 `get_impl_cls`/`get_builder_cls` 与各自的 CP 门控，互不共享代码路径。
> 2. **分片策略不同但都自洽**：MLA-CP 在 `kv_lora_rank` 隐空间里 gather 并恢复请求连续顺序的 KV；SFA-CP 复制小体积的 indexer 缓存、分片大体积的 SFA KV 并重映射 sparse 索引。两者针对各自的 KV 布局设计了匹配的通信与合并方式，因此可以各自独立启用 CP。

**需要观察的现象**：三个分发点的 `if` 条件与 import 路径互不相同，验证了「独立」。

**预期结果**：你能不看资料，画出「模型特征 → 平台选后端 → 后端按开关选 CP 变体」的三级路由图。

> 说明：本实践为源码阅读型，无需 NPU；若要运行验证，需要带 MLA/SFA 的真实模型与多卡 NPU 环境，运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：GQA DCP 在 decode 时为什么要 `num_heads *= dcp_size`？

> **答案**：GQA 按 head 维切分：每个 DCP rank 只持有 `num_heads / dcp_size` 个 head 的本地 KV，但 gather 之后 query 携带了全部 head，于是用 gather 后的「全 head 数」（`num_heads * dcp_size`，即原始总 head 数）去对本地 KV 做注意力，每个 rank 负责「全 head × 本地 KV 段」，最后按 head 合并。

**练习 2**：SFA-CP 为什么不让 indexer 也分片，而要复制它？

> **答案**：indexer 的职责是在完整序列上选出 sparse top-k 块，若它只看本地段，选出的块会和单卡（非 CP）SFA 不一致，破坏语义等价性。而 indexer 缓存相对 SFA KV 小得多，复制它的代价远小于复制全部 SFA KV，所以折中方案是「复制小的 indexer、分片大的 SFA KV，再把全局索引重映射到本地」。

---

## 5. 综合实践

把本讲知识串起来：**画一张完整的「DCP 一次 decode」数据流图，覆盖元数据生成到结果合并**。

要求：

1. 从 `dcp_utils.py:generate_dcp_metadata` 出发，标注 `num_computed_tokens_of_dcp`（形状 `[num_reqs, dcp_size]`）如何由 `get_dcp_local_seq_lens` 算出，又如何被塞进 `AscendDCPMetadata`、再被 `DCPMetadataBuilderMixin._get_dcp_rank_context_lens` 取出本 rank 列。
2. 选定一个后端（建议 GQA），画出：本地 query → `_dcp_all_gather`（head 维）→ `npu_fused_infer_attention_score`（`actual_seq_lengths_kv` = 本 rank 本地长度）→ `(attn_out, attn_lse)` → `_merge_dcp_attention_output`（all-to-all + `npu_attention_update`）→ 最终输出。
3. 在图上用不同颜色标出「计算」与「跨 rank 通信」两类操作，并指出哪些操作在 `cp_chunkedprefill_comm_stream` 上（可重叠）。

完成后，你应该能解释：为什么整条链路结束后，得到的结果与「单卡看完整序列」数学等价（提示：在线 softmax 合并的无损性）。

> 说明：这是一道源码阅读 + 画图任务，无需运行；若想用真实模型验证「无损」，可在 NPU 环境对比 `decode_context_parallel_size=1` 与 `=2` 的输出 logits，**待本地验证**。

## 6. 本讲小结

- **CP 的本质**：在 TP 组内部沿**序列维度**分片 KV cache，消除冗余存储；vllm-ascend 实现的是 **DCP**（Decode Context Parallel）。
- **开关与分发**：`enable_dcp()`（`decode_context_parallel_size > 1`）让每个后端的 `get_impl_cls`/`get_builder_cls` 延迟 import 并返回 CP 变体；CP 是后端的「特化」而非独立后端。
- **公共能力在 `common_cp.py`**：`get_dcp_local_seq_lens`（interleave 本地长度公式）、`DCPMetadataBuilderMixin`（取 DCP 元数据）、`DCPImplMixin`（all-gather + LSE 合并）。合并的数学核心是「在线 softmax」：\(l=\log\sum_i\exp(l_i),\ O=\sum_i\exp(l_i-l)O_i\)。
- **三类后端三种分片策略**：GQA 按 head 切（gather Q）、MLA 在隐空间 gather 并恢复请求连续 KV、SFA 复制 indexer 分片大 KV（重映射 sparse 索引）、DSA-CP 按 token 切（需 SP）。
- **平台路由是关键**：`get_attn_backend_cls` 用 `(use_mla, use_sparse, use_compress)` 选后端，一个模型只走一个后端，因此 MLA 与 SFA（SWA-MLA）能各自独立做 CP。
- **无损性**：所有局部结果经 LSE 合并后，与单卡全序列注意力数学等价。

## 7. 下一步学习建议

- **向「通信」深入**：本讲的 all-gather/all-to-all 都跑在 HCCL 上，建议接着学 u7-l2（NPUCommunicator 与 HCCL），理解 DCP group 在 `parallel_state` 里是如何从 TP 组划分出来的。
- **向「图模式」深入**：DCP 实现里有大量 `update_graph_params` / `_EXTRA_CTX.capturing` 分支（见 `attention_cp.py:174-276`），它们与 ACL Graph 捕获/回放强相关，建议学 u8-l3（ACL Graph）后再回看这些分支。
- **向「PD 分离」延伸**：SFA DCP 的复制 indexer 与 KV 传输连接器有交互（见 `mooncake_connector.py` 里的 `enable_sfa_dcp_replicated_indexer`），可在 u10-l2（PD 分离与 KV 传输）中看到完整图景。
- **动手验证**：在有 NPU 的环境，用 `examples/offline_inference_npu_long_seq.py` 对比不同 `--dcp` 下的显存与 TTFT，并把本讲第 5 节的数据流图与真实 `npu-smi` / 日志对照。
