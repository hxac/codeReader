# 端到端实战：复现一次 sparse decode

## 1. 本讲目标

本讲是整本手册的收官实战篇。前面十几篇讲义我们分别拆解了 FlashMLA 的接口层、派发框架、四类 kernel、FP8 量化、split-KV 与 combine。本讲不再引入新机制，而是**把所有零件串成一条完整链路**：从随机张量出发，跑通一次「FP8 稀疏解码（sparse decode）」，并对照 PyTorch 参考实现校验正确性、用 CUPTI 测量性能。

学完后你应当能够：

1. 说出 sparse decode 端到端数据流的五个阶段：**数据构造 → FP8 量化与 indices → 调用 kernel → 正确性校验 → 性能测量**。
2. 能复用 `tests/` 下现成的 `lib.py` / `quant.py` / `ref.py`，拼出一个最小可运行的端到端脚本骨架。
3. 理解每个阶段对最终正确性或性能的影响：为什么测试要先跑一遍拿答案、为什么量化要「量化再反量化」、为什么 FLOPs 用「 attended token」而访存用「去重后的 retrieved token」。
4. 会手算给定 shape 的理论 TFlops 与 GB/s，并据此判断该配置是 compute-bound 还是 memory-bound。

> 本讲不重新讲解底层 kernel 内部机制（seesaw、crossover、combine 的 rescale 公式等），这些请回顾 u3-l3、u5-l3、u4-l2。本讲只关注**如何用现成代码把它们跑起来并验证**。

---

## 2. 前置知识

本讲假设你已经读过以下讲义（其结论我们直接复用，不再重复推导）：

- **u5-l1（FP8 KV Cache 布局）**：MLA 把每个 576 维 token 拆成 512 维 NoPE（量化）+ 64 维 RoPE（不量化）；V3.2 布局每个 token 占 656 字节。
- **u5-l4（sparse decode 接口）**：`flash_mla_with_kvcache(..., indices=...)` 走 sparse 路径，强制 `is_fp8_kvcache=True`，V3.2 形状 `h_q=128, d_qk=576`。
- **u6-l1（sparse attention 语义）**：decode 用分页物理索引 `indices_in_kvcache`，无效索引置 `-1`；输出 `(out, lse)`，`lse` 是 base-e 的 log-sum-exp。
- **u8-l2（测试体系）**：`ref.py` 是数值黄金参考；`kk.check_is_allclose` 用三重容差判定；decode 路径会「量化再反量化」以剥离 FP8 量化误差。

此外，请确保你理解两个工程术语：

- **Paged KV cache**：KV 不按序列连续存放，而是切成固定大小的 page block，用 `block_table` 记录每个序列的块映射。sparse decode 把分页映射「烘焙」进 `indices`，于是 kernel 不再需要 `block_table`。
- **sched_meta 复用模式**：`FlashMLASchedMeta` 在**首次调用**时被 kernel 填充 tile 调度元数据，后续多步复用——前提是形状及 `cache_seqlens`、`topk_length` 等取值在多步间保持一致。

如果你手边没有 SM90（H800）或 SM100（B200）GPU，本讲的实践部分给出**完整可运行骨架与「预期输出说明」**，你可以在 CPU 上完成张量构造与数学推导，把 GPU 相关步骤标注为「待本地验证」。

---

## 3. 本讲源码地图

本讲串起的五个文件，恰好对应端到端链路的五个角色：

| 文件 | 角色 | 关键符号 |
| --- | --- | --- |
| `tests/lib.py` | **数据工厂**：把形状与开关翻译成带边界条件的张量 | `RawTestParamForDecode`、`generate_testcase_for_decode`、`KVScope`、`run_flash_mla_decode`、`count_flop_and_mem_vol_for_decode` |
| `tests/quant.py` | **量化器**：bf16 KV ↔ FP8 KV、绝对索引→分页索引 | `FP8KVCacheLayout`、`quantize_k_cache`、`dequantize_k_cache`、`abs_indices2indices_in_kvcache` |
| `flash_mla/flash_mla_interface.py` | **Python 入口**：sparse/dense 二分派发、sched_meta 复用 | `flash_mla_with_kvcache`、`get_mla_metadata`、`FlashMLASchedMeta` |
| `tests/ref.py` | **数值黄金参考**：用纯 PyTorch 复现 kernel 输出 | `ref_sparse_attn_decode`、`_merge_two_lse` |
| `tests/test_flash_mla_sparse_decoding.py` | **编排者**：把上述四者串成「构造→调用→校验→测速」 | `gen_testcase`、`test_flash_mla`、`main` |

另外两个工具文件会被间接调用：

- `tests/kernelkit/compare.py` 的 `check_is_allclose`：三重容差正确性判定。
- `tests/kernelkit/bench.py` 的 `bench_kineto` + `get_e2e_time`：基于 CUPTI 的 kernel 计时。

> 整条链路的「总指挥」是 `tests/test_flash_mla_sparse_decoding.py`，README 里也是直接 `python tests/test_flash_mla_sparse_decoding.py` 运行它。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应链路的四个关键阶段（第五个阶段「校验与测速」合并进 4.4）。

### 4.1 数据构造：把形状与开关翻译成张量

#### 4.1.1 概念说明

sparse decode 不能直接吃「一个 `[b, s_k, d]` 的 KV 张量」，它需要一组**协同约束**的张量：分页的 `blocked_k`、块映射 `block_table`、每条序列的真实长度 `cache_seqlens`、稀疏索引 `indices_in_kvcache`，以及可选的 `topk_length`、`attn_sink`、`extra_k_cache` 等。这些张量之间有大量隐式契约（比如 `block_table` 的取值范围、`indices` 的有效性、KV 必须连续有效），手工构造极易出错。

`tests/lib.py` 的角色就是**数据工厂**：你只给它一组「人类友好」的形状与开关（`b`、`s_q`、`topk`、`block_size`、`is_varlen`、`have_topk_length`……），它帮你生成全部满足契约的张量，并且**故意掺入边界条件**（`-1`、越界值、全无效序列、零长度序列）来逼出 kernel 的边界 bug。

#### 4.1.2 核心流程

```
RawTestParamForDecode(b, h_q, s_q, h_kv, s_k, is_varlen, topk, d_qk, ...)
        │  to_test_param()
        ▼
TestParam  ──► generate_testcase_for_decode(p)  ──►  TestcaseForDecode
                                                        ├─ q          [b, s_q, h_q, d_qk]
                                                        ├─ attn_sink  [h_q] 或 None
                                                        ├─ sm_scale
                                                        ├─ kv_scope   (KVScope)
                                                        └─ extra_kv_scope (KVScope 或 None)
```

`generate_testcase_for_decode` 内部为每个 KV 段调用 `generate_one_k_scope`，产出一个 `KVScope`，它打包了：`cache_seqlens`、`block_table`、`blocked_k`、`abs_indices`、`indices_in_kvcache`、`topk_length`。随后对每个 scope 调 `quant_and_dequant_()` 完成 FP8 量化（见 4.2）。

#### 4.1.3 源码精读

测试用例的「形状与开关」由扁平结构 `RawTestParamForDecode` 描述，`to_test_param()` 把 decode 专属字段塞进 `TestParam.decode`：

- `RawTestParamForDecode` 字段定义见 [tests/lib.py:46-72](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L46-L72)：注意默认 `d_qk=576`（V3.2）、`d_v=512`，与 README 的 MLA 约束一致。
- 用例清单在 `gen_testcase()` 里枚举：[tests/test_flash_mla_sparse_decoding.py:23-126](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_decoding.py#L23-L126)。它分三类：`correctness_cases`（验正确性）、`corner_cases`（边界）、`performance_cases`（测速）。注意 `performance_cases` 里有「生产形状」与「峰值形状」两组，峰值组是 `RawTestParam(74*2, h_q, 2, 1, 32768, True, topk=16384, d_qk=d_qk)`。

数据工厂主体 `generate_testcase_for_decode` 的关键步骤：

- 构造 Q 并 clamp 到 `[-1, 1]`：[tests/lib.py:232-233](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L232-L233)。
- `generate_one_k_scope` 内部按 `cache_seqlens`（可 varlen、可含零长度）生成 `block_table`（一个乱序的 `arange`），再用 `kk.gen_non_contiguous_randn_tensor` 造分页 KV：[tests/lib.py:242-289](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L242-L289)。
- 故意把「未被索引命中的 KV」置成 `NaN`：[tests/lib.py:281-284](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L281-L284)。这是**正确性陷阱**：如果 kernel 误读了不该读的 token，NaN 会污染输出，校验立刻失败。
- `sm_scale = t.d_qk ** -0.55`：[tests/lib.py:304](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L304)（注意不是教科书式的 `d_qk ** -0.5`，测试自定了一个略小的 scale）。

#### 4.1.4 代码实践

**实践目标**：不动用 GPU，纯用 `lib.py` 在 CPU 上构造一组 sparse decode 张量，检查它们的形状与契约。

**操作步骤**（CPU 也可运行，注意要把 `torch.set_default_device` 去掉或设为 cpu）：

```python
# 示例代码：仅做张量构造，不调用 kernel，CPU 可跑
import sys; sys.path.insert(0, "tests")
import torch
# 若无 CUDA，需让 lib.py 内部的 .cuda() 调用落到 CPU —— 见下方说明
import lib

# 1) 用扁平参数描述一个 V3.2 sparse decode 用例
raw = lib.RawTestParamForDecode(
    b=4, h_q=128, s_q=2, h_kv=1, s_kv=512, is_varlen=True, topk=64, d_qk=576
)
p = raw.to_test_param()

# 2) 生成协同约束的张量组
t = lib.generate_testcase_for_decode(p)

# 3) 检查契约
kv = t.kv_scope
print("q           :", t.q.shape)                 # 期望 [4, 2, 128, 576]
print("blocked_k   :", kv.blocked_k.shape)        # [num_blocks, block_size, 1, 576]
print("block_table :", kv.block_table.shape)      # [b, max_blocks_per_seq]
print("indices     :", kv.indices_in_kvcache.shape)  # [b, s_q, topk]
print("attn_sink   :", None if t.attn_sink is None else t.attn_sink.shape)
```

> ⚠️ 说明：`lib.generate_testcase_for_decode` 内部有 `.cuda()` 调用（如 `cache_seqlens = cache_seqlens_cpu.cuda()`）。在纯 CPU 环境下这一步会报错，属于「待本地验证」部分。完整可运行骨架见 [第 5 节综合实践](#5-综合实践)，那里给出了 CPU 友好的最小版。

**需要观察的现象**：

- `indices_in_kvcache` 的取值范围应在 `[0, num_blocks*block_size)` 之间，且无效位置为 `-1`。
- `block_table` 的每行是 `[0, num_blocks)` 的一个排列。

**预期结果**：形状与上述注释一致；若打印 `indices_in_kvcache` 最小值，应能看到 `-1`（被 `_randperm_batch` 注入的无效候选之一）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `generate_one_k_scope` 要把未被索引命中的 KV 置成 `NaN`，而不是 `0`？

> **参考答案**：置 `0` 时，即使 kernel 误读了不该读的 token，结果也只是「多加了一个 0」，输出仍然正确，**掩盖了 bug**。置 `NaN` 后，任何误读都会让输出出现 `NaN`，校验立即失败，从而把「越界读」这类错误显式暴露出来。

**练习 2**：`_randperm_batch` 的 `paddings` 参数里包含 `-1`、`t.s_kv`、`114514`、`2147483647` 等值（见 [tests/lib.py:131](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L131)）。这些「无效索引候选」分别对应哪类边界？

> **参考答案**：`-1` 是 sparse decode 的标准「无效」标记；`>= s_kv`（如 `t.s_kv`、`114514`）测试「逻辑越界但非 -1」的分支；`2147483647`/`-2147483648` 测试 int32 上下界的极端值，确保 kernel 的索引比较与掩码逻辑对「大正数」也安全。

---

### 4.2 FP8 量化与 indices 编码

#### 4.2.1 概念说明

sparse decode 强制 FP8 KV cache。这带来两件事：

1. **量化**：bf16 的 KV 必须按「FP8 with scale」布局打包成字节流（V3.2 是 656 字节/token）。`tests/quant.py` 的 `quantize_k_cache` 是权威参考实现。
2. **分页索引烘焙**：sparse decode 的 `indices` 不是逻辑下标，而是**分页物理索引** `indices_in_kvcache`，由 `abs_indices2indices_in_kvcache` 把「绝对位置 + block_table」折叠成单一整数，让 kernel 不再需要 `block_table`。

此外，测试体系有一个关键技巧：**量化再反量化（quant-and-dequant）**。直接用原始 bf16 KV 当参考、用 FP8 KV 跑 kernel，两者之间的差值会混入「量化误差」，可能把「正确的 kernel」误判为「错误」。`KVScope.quant_and_dequant_()` 的做法是：先把 KV 量化成 FP8、再反量化回 bf16，**让参考实现也吃这份带量化误差的 KV**，从而把误差从「kernel 正确性」里剥离出去。

#### 4.2.2 核心流程

```
bf16 blocked_k ──quantize_k_cache──► FP8 blocked_k_quantized ──dequantize_k_cache──► bf16 blocked_k'(带量化误差)
                                         │                                              │
                                         └────────────► 喂给 kernel                    └────────► 喂给 ref 参考

abs_indices [b,s_q,topk] ──abs_indices2indices_in_kvcache(block_table, block_size)──► indices_in_kvcache [b,s_q,topk]
```

两种 layout 的字节结构（`get_meta()` 返回 `(d, d_nope, d_rope, tile_size, num_tiles)`）：

- **V32**（d_qk=576）：`(576, 512, 64, 128, 4)`，每 token `512(fp8) + 4×4(scale) + 64×2(rope) = 656` 字节。
- **MODEL1**（d_qk=512）：`(512, 448, 64, 64, 7)`，每 token `448 + 2×64 + 7 + 1 = 584` 字节（含 block 对齐填充）。

#### 4.2.3 源码精读

layout 枚举与元信息：[tests/quant.py:6-15](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L6-L15)。

V32 的 656 字节布局直接体现在 `bytes_per_token` 公式：[tests/quant.py:35-41](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e457e089f888280e0/tests/quant.py#L35-L41)——`result_k_nope_part`（512 字节 fp8）+ `result_k_scale_factor`（16 字节，4 个 fp32）+ `result_k_rope_part`（128 字节，64 个 bf16，原样拷贝不量化）。

tile 级量化核心：每 128 维 NoPE 配一个 scale，按 2 的幂向上取整（UE8M0）保证 fp8 不溢出 448 上限：[tests/quant.py:43-50](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L43-L50)。`_cast_scale_inv_to_ue8m0`（[tests/quant.py:17-18](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L17-L18)）即 `2^ceil(log2(max/448))`。

「量化再反量化」发生在 `KVScope.quant_and_dequant_`：[tests/lib.py:178-193](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L178-L193)——注意它**同时**保存了 `blocked_k_quantized`（喂 kernel）并把 `blocked_k` 替换成反量化结果（喂 ref）。

分页索引烘焙 `abs_indices2indices_in_kvcache`：[tests/quant.py:126-158](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/quant.py#L126-L158)。核心是把逻辑下标 `abs` 折叠为 `block_table[abs//block_size]*block_size + abs%block_size`，并对 `-1` 保留为 `-1`。其文档字符串里给出了等价的 for 循环写法，便于对照理解。

#### 4.2.4 代码实践

**实践目标**：在 CPU 上完成一次「量化 → 反量化」并测量量化误差，验证字节布局与文档一致。这一步**不需要 GPU**，因为 `quant.py` 全部是纯 PyTensor 操作（fp8 量化 PyTorch 在 CPU 也支持）。

**操作步骤**：

```python
# 示例代码：CPU 可运行
import sys; sys.path.insert(0, "tests")
import torch
from quant import FP8KVCacheLayout, quantize_k_cache, dequantize_k_cache

torch.manual_seed(0)
bf16_kv = torch.randn(2, 64, 1, 576, dtype=torch.bfloat16) * 0.1   # [num_blocks, block_size, h_k=1, d]

q_cache = quantize_k_cache(bf16_kv, FP8KVCacheLayout.V32_FP8Sparse)
print("quantized shape :", q_cache.shape)            # [2, 64, 1, 656]
print("bytes per token :", q_cache.shape[-1])        # 656

dq = dequantize_k_cache(q_cache, FP8KVCacheLayout.V32_FP8Sparse)
err = (dq.float() - bf16_kv.float()).abs().max().item()
print("max quant err  :", err)                       # 期望是较小值（与 UE8M0 粒度相关）
```

**需要观察的现象**：

- `q_cache.shape[-1]` 应为 `656`，正好对应 README「First 512 / Next 16 / Last 128」三段。
- 量化误差 `err` 应远小于正确性容差（out 用 `2.01/128≈0.0157`），说明 UE8M0 粒度量化的精度损失可控。

**预期结果**：`bytes per token = 656`；`max quant err` 通常在 `1e-2` 量级（取决于输入幅值）。这正是测试要做「量化再反量化」的原因——单独的量化误差就已接近正确性容差，必须从参考里剥离。

#### 4.2.5 小练习与答案

**练习 1**：V32 的 scale 是 fp32（4 字节 × 4 个 = 16 字节），MODEL1 的 scale 是 `e8m0`（1 字节 × 7 个）。为什么 MODEL1 改用 1 字节的 e8m0？

> **参考答案**：e8m0 即 UE8M0——只存 2 的指数幂（无尾数），用 1 字节就能表达一个量化比例因子。MODEL1 把 tile 粒度从 128 降到 64（`num_tiles` 从 4 升到 7），scale 数量翻倍，改用 1 字节编码可控制 scale 段的存储开销；同时「2 的幂」性质让反量化退化成简单的指数运算，便于硬件实现。

**练习 2**：`abs_indices2indices_in_kvcache` 为什么要在烘焙前先把 `-1` 暂时改成 `0`，烘焙后再改回 `-1`？

> **参考答案**：因为中间用 `index_select` 去 `block_table` 里取块号，传 `-1` 会让 `index_select` 越界报错。所以先把无效位置临时指向合法的 `0` 号块完成计算，最后再用掩码把无效位置还原成 `-1`（kernel 用 `-1` 判无效）。

---

### 4.3 端到端调用：flash_mla_with_kvcache 与 sched_meta 复用

#### 4.3.1 概念说明

有了量化后的 `k_cache` 和 `indices_in_kvcache`，就可以调用 `flash_mla_with_kvcache`。这个函数本身很薄，只做两件事：

1. **首次调用初始化 sched_meta**：把形状快照存进 `FlashMLASchedMeta.config`，kernel 在 GPU 端填充 `tile_scheduler_metadata` 与 `num_splits`；后续调用复用它们，并断言「形状/关键取值未变」。
2. **sparse/dense 二分派发**：有 `indices` 走 `flash_mla_cuda.sparse_decode_fwd`，否则走 `dense_decode_fwd`。sparse 路径强制 `is_fp8_kvcache=True`、`causal=False`。

理解这条链路后，你就能看懂 `lib.run_flash_mla_decode` 是如何把 `TestcaseForDecode` 的字段按位置传给这个函数的——它正是端到端调用的「最后一公里」。

#### 4.3.2 核心流程

```
flash_mla_with_kvcache(q, k_cache, block_table=None, cache_seqlens=None, head_dim_v,
                       sched_meta, num_splits=None, softmax_scale, causal=False,
                       is_fp8_kvcache=True, indices=..., attn_sink=..., extra_k_cache=...,
                       extra_indices_in_kvcache=..., topk_length=..., extra_topk_length=...)
        │
        ├─ 首次：sched_meta.have_initialized=False → 存 config 快照
        │  后续：逐字段断言与 config 一致
        │
        ├─ topk = indices.shape[-1]   (None 则 dense)
        │
        └─ topk is not None ──► flash_mla_cuda.sparse_decode_fwd(...) ──► (out, lse, new_meta, new_num_splits)
                                                                sched_meta 更新并复用
```

返回：`out [b, s_q, h_q, head_dim_v]`（bf16）、`lse [b, h_q, s_q]`（fp32，base-e）。

#### 4.3.3 源码精读

`flash_mla_with_kvcache` 签名与文档：[flash_mla/flash_mla_interface.py:53-103](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L53-L103)。文档里详述了 V3.2 的 656 字节布局与 `indices_in_kvcache` 的编码公式。

`topk` 由 `indices` 末维推导：[flash_mla/flash_mla_interface.py:109-111](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L109-L111)。

**sched_meta 复用模式**的核心是「首次存快照、后续逐字段断言」：

- 首次初始化 `config`：[flash_mla/flash_mla_interface.py:115-135](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L115-L135)。
- 后续断言一致性（`b`/`s_q`/`h_q`/`page_block_size`/`h_k`/`causal`/`is_fp8_kvcache`/`topk`/`extra_*`）：[flash_mla/flash_mla_interface.py:136-149](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L136-L149)。这就是「多步解码时形状必须不变」的强制约束。

**sparse 二分派发**（强制 FP8）：[flash_mla/flash_mla_interface.py:151-160](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L151-L160)——`assert is_fp8_kvcache`，然后调用 `flash_mla_cuda.sparse_decode_fwd`，把 11 个参数按固定顺序透传。

调用结束后回写 sched_meta：[flash_mla/flash_mla_interface.py:171-173](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L171-L173)。

测试侧的「最后一公里」`run_flash_mla_decode`：[tests/lib.py:319-334](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L319-L334)。注意它**按位置**传参（`block_table=None, cache_seqlens=None` 对应 sparse；`is_fp8_kvcache=True`；`indices=t.kv_scope.indices_in_kvcache`），并把 `extra_kv_scope` 的量化结果与索引也接上。

#### 4.3.4 代码实践

**实践目标**：把 4.1、4.2 的产物接上 `flash_mla_with_kvcache`，跑一次 sparse decode。**无 GPU 时**给出骨架并说明预期输出。

**操作步骤（有 GPU，H800/B200）**：

```python
import sys; sys.path.insert(0, "tests")
import torch, flash_mla, lib
from lib import RawTestParamForDecode

dev = "cuda:0"
torch.set_default_dtype(torch.bfloat16); torch.set_default_device(dev)

raw = RawTestParamForDecode(b=4, h_q=128, s_q=2, h_kv=1, s_kv=512,
                            is_varlen=True, topk=64, d_qk=576, num_runs=0)
p = raw.to_test_param()
t = lib.generate_testcase_for_decode(p)          # 内部已完成 quant_and_dequant_

sched_meta, _ = flash_mla.get_mla_metadata()     # 空壳，首次调用时填充
out, lse = lib.run_flash_mla_decode(p, t, sched_meta, None)

print("out:", out.shape, out.dtype)              # [4, 2, 128, 512] bf16
print("lse:", lse.shape, lse.dtype)              # [4, 128, 2]     fp32
```

**需要观察的现象**：

- 首次调用后 `sched_meta.have_initialized` 变为 `True`，`tile_scheduler_metadata` 不再是 `None`。
- 若紧接着用**相同形状**再调一次，复用同一 `sched_meta`，不会触发一致性断言。
- 若把第二次的 `b` 改成别的值再用同一 `sched_meta` 调用，会抛 `AssertionError`（config 不一致）。

**预期结果**：`out` 形状 `[b, s_q, h_q, 512]`、`lse` 形状 `[b, h_q, s_q]`。

> **无 GPU 时**：`lib.generate_testcase_for_decode` 与 `flash_mla.cuda` 都依赖 CUDA，本步骤标注「待本地验证」。完整 CPU 友好骨架见第 5 节。

#### 4.3.5 小练习与答案

**练习 1**：为什么 sparse 路径里 `block_table` 和 `cache_seqlens` 都传 `None`？

> **参考答案**：因为分页映射已经在 `abs_indices2indices_in_kvcache` 里被「烘焙」进 `indices`——`indices_in_kvcache` 直接就是物理地址 `块号*block_size+偏移`，kernel 拿着它就能直接寻址 KV 池，不再需要 `block_table` 做翻译。`cache_seqlens` 在 sparse 下也没有意义（有效 token 集合由 `indices` + `topk_length` 决定，不由序列长度决定）。

**练习 2**：`get_mla_metadata()` 现在是无参空壳（[flash_mla/flash_mla_interface.py:37-50](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L37-L50)），但 README 里旧示例传了一堆参数。为什么改成空壳还能工作？

> **参考答案**：调度元数据现在**全部在 kernel 首次调用时由 GPU 端生成**，Python 端不再需要预先知道任何形状信息。`get_mla_metadata` 只负责返回一个空的 `FlashMLASchedMeta` 容器，真正的填充发生在第一次 `flash_mla_with_kvcache` 内部（由 `sparse_decode_fwd` / `dense_decode_fwd` 返回 `new_tile_scheduler_metadata` 回写）。保留 `*args/**kwargs` 只为兼容旧调用方式。

---

### 4.4 校验与测速：ref 参考 + bench_kineto

#### 4.4.1 概念说明

跑完 kernel 只是拿到「答案」，还要回答两个问题：**对不对**（correctness）和**快不快**（performance）。FlashMLA 的测试用一套统一的编排 `test_flash_mla` 同时回答两者：

- **正确性**：用 `ref.ref_sparse_attn_decode` 算出 PyTorch 黄金参考，再用 `kk.check_is_allclose` 做三重容差比较（abs/rel/cos_diff），并对 `inf`/`nan` 异常值做逐位掩码匹配。
- **性能**：用 `kk.bench_kineto`（基于 PyTorch 的 CUPTI profiler）跑 `num_runs` 次，按 kernel 名拆分计时，再用 `count_flop_and_mem_vol_for_decode` 算出理论 FLOPs 与访存字节，除以端到端耗时得到 TFlops 与 GB/s。

这里有两个容易踩坑的细节：

1. **先跑一遍拿答案，再测速**：测速阶段会反复分配张量，可能复用掉「正确答案」所在显存导致**假阳性**（false positive）。所以测试**先**跑一次拿到 `out_ans/lse_ans` 并保存，**再**进入测速。
2. **FLOPs 用 attended token，访存用 retrieved（去重）token**：sparse attention 里多个索引可能指向同一个 KV token，FLOPs 按每个索引算一次（重复也算），但访存按**去重后**的唯一 token 算一次（命中 L2/重复读不重复计）。这让「算术强度」更贴近真实硬件行为。

#### 4.4.2 核心流程

```
test_flash_mla(p):
  t = generate_testcase_for_decode(p)
  sched_meta = get_mla_metadata()
  ── if check_correctness ──► run_decode() 一次 → (out_ans, lse_ans)   # 先拿答案，避免被测速覆盖
  ── if num_runs > 0 ──► result = bench_kineto(run_decode, num_runs)
        │   按 kernel 名取 splitkv / combine 的逐 kernel 耗时
        │   e2e = get_e2e_time(splitkv, combine)  # 或单 kernel 退化为 splitkv 自身
        │   flops, mem = count_flop_and_mem_vol_for_decode(p, t)
        │   TFlops = flops / e2e_s / 1e12 ;  GB/s = mem / e2e_s / 1e9
  ── if check_correctness ──► out_ref,lse_ref = ref_sparse_attn_decode(p,t)
        check_is_allclose("out", out_ans, out_ref, ...)   # abs 1e-3 / rel 2.01/128 / cos 5e-6
        check_is_allclose("lse", lse_ans, lse_ref, ...)   # abs 1e-6 / rel 8.01/65536
```

#### 4.4.3 源码精读

**先拿答案、再测速**的顺序至关重要，代码里专门注释了原因：[tests/test_flash_mla_sparse_decoding.py:158-164](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_decoding.py#L158-L164)。

测速主调：`result = kk.bench_kineto(run_decode, p.num_runs)`：[tests/test_flash_mla_sparse_decoding.py:173](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_decoding.py#L173)。随后用两个固定 kernel 名去拆分耗时——`flash_fwd_splitkv_mla_fp8_sparse_kernel` 与 `flash_fwd_mla_combine_kernel`：[tests/test_flash_mla_sparse_decoding.py:175-188](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_decoding.py#L175-L188)。

端到端耗时的取法（有 combine 取 span、无 combine 退化为 splitkv 自身耗时）：[tests/test_flash_mla_sparse_decoding.py:194-202](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_decoding.py#L194-L202)。`get_e2e_time` 的定义是「最后一个 kernel 结束 − 第一个 kernel 开始」：[tests/kernelkit/bench.py:77-100](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/kernelkit/bench.py#L77-L100)。

TFlops / GB/s 的换算：[tests/test_flash_mla_sparse_decoding.py:204-218](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_decoding.py#L204-L218)。

理论 FLOPs 与访存字节的计算 `count_flop_and_mem_vol_for_decode`：

- FLOPs：`compute_flop = 2 * p.h_q * num_attended_tokens * (p.d_qk + p.d_v)`（[tests/lib.py:392](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L392)）。这与 u3-l1 的理论公式一致：\(\text{FLOPs}=2\cdot h_q\cdot s_q\cdot(\text{attended})\cdot(d_k+d_v)\)，因 MLA 中 K/V 同源，K 只加载一次但 QK 与 PV 各算一次，故乘 \((d_k+d_v)\)。
- 访存：`mem_vol = Q字节 + 去重K字节 + O字节`（[tests/lib.py:393-398](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L393-L398)），其中 K 用 `num_retrieved_tokens`（去重，[tests/lib.py:378-387](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L378-L387)）。
  > 细节注记：`kv_token_size = 656 if p.d_qk == 576 else 576`（[tests/lib.py:393](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L393)）里 MODEL1 用了 `576`，而 `quant.py` 里 MODEL1 的精确布局是 `584` 字节。这是测试对 HBM 访存的**近似估算**（注释 `# Assume FP8 KV Cache`），并非精确字节——性能核算重在量级，不追求逐字节精确。

正确性校验：`ref.ref_sparse_attn_decode` 产黄金参考，再用三重容差判定：

- 参考实现入口：[tests/test_flash_mla_sparse_decoding.py:225-226](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_decoding.py#L225-L226)。
- `out` 容差 `abs_tol=1e-3, rel_tol=2.01/128, cos_diff_tol=5e-6`：[tests/test_flash_mla_sparse_decoding.py:228](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_decoding.py#L228)。
- `lse` 容差 `abs_tol=1e-6, rel_tol=8.01/65536`：[tests/test_flash_mla_sparse_decoding.py:229](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_decoding.py#L229)。lse 容差更紧，是因为 lse 会被后续 `exp` 指数放大，微小的 lse 误差会放大成大的输出误差。

`check_is_allclose` 的判定逻辑（异常值逐位匹配 + abs/rel 双门 OR + cos_diff 全局兜底）：[tests/kernelkit/compare.py:31-92](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/kernelkit/compare.py#L31-L92)。

`ref_sparse_attn_decode` 的关键处理（lonely query 置 `out=0, lse=+inf`、attn_sink 只缩放 out 不改 lse）：[tests/ref.py:55-103](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L55-L103)。

#### 4.4.4 代码实践：手算 TFlops / GB/s（CPU 可做）

**实践目标**：给一个具体的 V3.2 sparse decode 形状，手算理论 FLOPs、访存字节与算术强度，判断 bound，并与 README 报告的 410 TFlops 对照。

**取测试里的 V3.2 生产形状**（`base_and_bszs` 第一项，b 取 64）：

\[
b=64,\ h_q=128,\ s_q=2,\ \text{topk}=2048,\ d_{qk}=576,\ d_v=512,\ \text{kv\_token\_size}=656
\]

**第 1 步：attended token 数**（无 topk_length，不去重）：

\[
N_\text{attended}=b\cdot s_q\cdot \text{topk}=64\cdot2\cdot2048=262{,}144
\]

**第 2 步：理论 FLOPs**：

\[
\text{FLOPs}=2\cdot h_q\cdot N_\text{attended}\cdot(d_{qk}+d_v)
=2\cdot128\cdot262{,}144\cdot(576+512)\approx 7.30\times10^{10}
\]

**第 3 步：访存字节**（K 项用上界，即不去重，作为保守估计）：

\[
\begin{aligned}
\text{Q字节}&=2\cdot b\cdot s_q\cdot h_q\cdot d_{qk}=2\cdot64\cdot2\cdot128\cdot576\approx 1.89\times10^{7}\\
\text{O字节}&=2\cdot b\cdot s_q\cdot h_q\cdot d_v=2\cdot64\cdot2\cdot128\cdot512\approx 1.68\times10^{7}\\
\text{K字节}&\le N_\text{attended}\cdot656=262{,}144\cdot656\approx 1.72\times10^{8}\\
\text{mem\_vol}&\approx 2.08\times10^{8}\ \text{字节}
\end{aligned}
\]

（实际 K 字节会更小，因为 `num_retrieved_tokens` 去重；这里取上界做保守估计。）

**第 4 步：算术强度与 bound 判定**：

\[
I=\frac{\text{FLOPs}}{\text{mem\_vol}}\approx\frac{7.30\times10^{10}}{2.08\times10^{8}}\approx 351\ \text{FLOP/byte}
\]

回顾 u3-l1：H800 SXM5 降频后峰值算力 \(P\approx865\) TFlops、带宽 \(B=3.35\) TB/s，平衡点 \(I^*=P/B\approx258\) FLOP/byte。此处 \(I\approx351>258\)，**判定为 compute-bound**——与 sparse decode 的设计目标（README 报告 H800 上 410 TFlops）一致。

**第 5 步：反推耗时与吞吐（待本地验证）**。假设在 H800 上实测端到端耗时 \(T\)（由 `bench_kineto` 给出），则：

\[
\text{TFlops}=\frac{\text{FLOPs}}{T\cdot10^{12}},\qquad \text{GB/s}=\frac{\text{mem\_vol}}{T\cdot10^{9}}
\]

README 报告 sparse decode 在 H800 上约 **410 TFlops**，反推 \(T\approx 7.30\times10^{10}/(410\times10^{12})\approx 178\ \mu s\) 量级（不同 batch/形状会有差异）。**实际数值待本地用 `bench_kineto` 验证**，此处仅给量级参考。

**操作步骤（有 GPU）**：把上面这个形状塞进 `RawTestParamForDecode` 并设 `num_runs=30`，运行 `test_flash_mla`，观察它打印的 `Compute/Memory`、`TFlops`、`GB/s`、`us` 四行，与手算对照。

**预期结果**：`Compute/Memory` 约 350 上下（与手算的 \(I\) 吻合）；`TFlops` 在 compute-bound 形状下应接近 README 的 410 量级。

#### 4.4.5 小练习与答案

**练习 1**：为什么测试要先跑一次 `run_decode()` 拿答案，再去跑 `bench_kineto` 测速？

> **参考答案**：`bench_kineto` 会反复执行 `run_decode`，期间 PyTorch 的 caching allocator 会反复分配/复用显存。如果先测速、后拿答案，答案张量可能落在被测速过程复用过的显存块上，读到的是「被覆盖过的旧数据」——校验会**误判通过**（假阳性）。先拿答案并保存在独立变量里，规避了这种覆盖。

**练习 2**：`count_flop_and_mem_vol_for_decode` 里，FLOPs 用 `num_attended_tokens`、访存用 `num_retrieved_tokens`（去重）。如果两者都用 `num_attended_tokens`（不去重），算出的算术强度会偏高还是偏低？为什么测试选择去重？

> **参考答案**：不去重会让 K 的访存字节被高估（重复读同一 token 算了多次），从而**低估**算术强度 \(I=\text{FLOPs}/\text{mem}\)。实际硬件上，重复命中的 KV 多半落在 L2 cache，不会重复走 HBM，所以按去重后的唯一 token 数估算 HBM 访存更贴近真实。测试选择去重，是为了让「GB/s」反映真实 HBM 带宽利用率，而不是被重复读放大。

**练习 3**：`lse` 的相对容差是 `8.01/65536≈1.2e-4`，比 `out` 的 `2.01/128≈0.0157` 紧得多。为什么 lse 要用更紧的容差？

> **参考答案**：lse 之后会被 `exp`（或 `exp2f`）指数放大。\(e^x\) 在 \(x\) 较大处导数也大，lse 的微小误差经指数后会放大成显著的输出误差。所以必须把 lse 卡得很紧（约 bf16 尾数 1 ULP 量级），才能保证后续 softmax 权重与输出 `out` 的精度。

---

## 5. 综合实践

**任务**：组装一个**最小端到端脚本**，串起「构造 bf16 KV → 量化 FP8 → 生成 indices → 调用 kernel → ref 校验 → 打印耗时与 TFlops」。无 GPU 时给出完整可运行骨架与预期输出说明。

下面这份骨架刻意做成「CPU 也能跑的形状构造 + GPU 部分 guarded」两部分，方便你在两种环境下使用：

```python
# 示例代码：最小端到端 sparse decode 骨架
# 用法（有 H800/B200）：python this_script.py
# 无 GPU：可运行到「数据就绪」处，kernel 调用标注为待本地验证
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tests"))

import torch
import kernelkit as kk                       # tests/kernelkit
import flash_mla
import lib, ref
from quant import FP8KVCacheLayout, quantize_k_cache, dequantize_k_cache

HAS_CUDA = torch.cuda.is_available()
dev = "cuda:0" if HAS_CUDA else "cpu"

# ───────────────────────── 1. 数据构造 ─────────────────────────
# 用扁平参数描述一个 V3.2 sparse decode 用例（与测试的 V3.2 生产形状同源）
raw = lib.RawTestParamForDecode(
    b=4, h_q=128, s_q=2, h_kv=1, s_kv=512, is_varlen=True,
    topk=64, d_qk=576, num_runs=30, check_correctness=True,
)

if HAS_CUDA:
    torch.set_default_dtype(torch.bfloat16); torch.set_default_device(dev)
    p = raw.to_test_param()
    t = lib.generate_testcase_for_decode(p)         # 内部已 quant_and_dequant_
else:
    # CPU 路径：手动复刻最小张量组（仅供理解形状，数值仅供走通流程）
    b, s_q, h_q, d_qk, topk, block_size = 4, 2, 128, 576, 64, 64
    num_blocks = 16
    bf16_kv = torch.randn(num_blocks, block_size, 1, d_qk, dtype=torch.bfloat16) * 0.1
    q_cache = quantize_k_cache(bf16_kv, FP8KVCacheLayout.V32_FP8Sparse)
    dq_kv   = dequantize_k_cache(q_cache, FP8KVCacheLayout.V32_FP8Sparse)
    print("[CPU] q_cache bytes/token =", q_cache.shape[-1], "（期望 656）")
    print("[CPU] max quant err       =",
          (dq_kv.float() - bf16_kv.float()).abs().max().item())
    print("[CPU] 数据就绪。后续 kernel 调用与测速待本地验证（需 H800/B200）。")
    sys.exit(0)

# ───────────────────────── 2. 调用 kernel ─────────────────────────
sched_meta, _ = flash_mla.get_mla_metadata()
def run_decode():
    return lib.run_flash_mla_decode(p, t, sched_meta, None)

torch.cuda.synchronize()
out_ans, lse_ans = run_decode()                   # 先拿答案，避免被测速覆盖
torch.cuda.synchronize()

# ───────────────────────── 3. 正确性校验 ─────────────────────────
out_ref, lse_ref = ref.ref_sparse_attn_decode(p, t)
ok_out = kk.check_is_allclose("out", out_ans, out_ref,
                              abs_tol=1e-3, rel_tol=2.01/128, cos_diff_tol=5e-6)
ok_lse = kk.check_is_allclose("lse", lse_ans, lse_ref,
                              abs_tol=1e-6, rel_tol=8.01/65536)
print("correctness:", "PASS" if (ok_out and ok_lse) else "FAIL")

# ───────────────────────── 4. 性能测量 ─────────────────────────
if p.num_runs > 0 and not kk.is_using_profiling_tools():
    res = kk.bench_kineto(run_decode, p.num_runs)
    splitkv = "flash_fwd_splitkv_mla_fp8_sparse_kernel"
    combine = "flash_fwd_mla_combine_kernel"
    e2e_s = res.get_e2e_time(splitkv, combine)     # 端到端：splitkv 起 → combine 止
    fm = lib.count_flop_and_mem_vol_for_decode(p, t)
    tflops = fm.flop / e2e_s / 1e12
    gBps   = fm.mem_vol / e2e_s / 1e9
    print(f"C/M={fm.flop/fm.mem_vol:.0f}  TFlops={tflops:.0f}  GB/s={gBps:.0f}  e2e={e2e_s*1e6:.1f}us")
```

**预期输出说明**：

- **CPU 路径**：打印 `bytes/token = 656`、一个较小的 `max quant err`（通常 `1e-2` 量级），随后退出。
- **GPU 路径**：`correctness: PASS`；性能行打印 `C/M`（与 4.4 手算的 ~351 同量级）、`TFlops`（compute-bound 形状下接近 README 的 410 量级）、`GB/s`、端到端耗时。
  > 实际 TFlops / GB/s / 耗时**待本地验证**——它们取决于具体 GPU、CUDA 版本与形状。本骨架保证「链路正确」，数值以你机器实测为准。

**串联要点回顾**：这份骨架把四个最小模块串成了闭环——`lib` 造数据（4.1）、`quant` 量化与烘焙索引（4.2）、`flash_mla_with_kvcache` 调用并复用 sched_meta（4.3）、`ref` + `bench_kineto` 校验与测速（4.4）。任何一环出错，都会在 `correctness` 或异常数值上立刻暴露。

---

## 6. 本讲小结

- 端到端 sparse decode 链路是 **数据构造 → FP8 量化与 indices → 调用 kernel → 正确性校验 → 性能测量** 五段，分别由 `lib.py`、`quant.py`、`flash_mla_interface.py`、`ref.py`、`test_flash_mla_sparse_decoding.py`(+`kernelkit`) 承担。
- `lib.py` 是数据工厂，把「形状+开关」翻译成协同约束的张量组，并**故意掺入 `-1`/越界/NaN 边界**逼出 kernel bug。
- `quant.py` 提供 656 字节（V32）/ 584 字节（MODEL1）的 FP8 布局；测试用「**量化再反量化**」把 FP8 量化误差从正确性判定里剥离出去。
- `flash_mla_with_kvcache` 是薄壳：首次调用初始化 `sched_meta` 并由 kernel 填充，后续复用并断言形状一致；有 `indices` 走 sparse 路径（强制 FP8）。
- 校验用 `ref.ref_sparse_attn_decode` 黄金参考 + `check_is_allclose` 三重容差；测速用 `bench_kineto` 按 kernel 名拆分，再用 `count_flop_and_mem_vol_for_decode` 算理论 TFlops/GB/s。
- 两个工程细节决定可信度：**先拿答案再测速**（防假阳性）；**FLOPs 用 attended token、访存用去重 retrieved token**（贴近真实 HBM 行为）。手算可得该配置算术强度 ~351 > 平衡点 258，判定 compute-bound，与 README 的 410 TFlops 一致。

---

## 7. 下一步学习建议

本讲是整本手册的收官篇，至此你已具备「从零跑通并验证一次 FlashMLA kernel」的完整能力。后续建议：

1. **动手扩展（承接 u9-l2）**：照着 u9-l2 的「端到端改动清单」，尝试为 sparse decode 新增一个 head_dim 或 feature，复用本讲的端到端骨架做回归校验——这是检验你是否真正理解派发框架的最佳方式。
2. **读两篇 deep-dive 博客**：`docs/20250422-new-kernel-deep-dive.md`（dense decode 的 seesaw 与 compute-bound 分析）与 `docs/20250929-hopper-fp8-sparse-deep-dive.md`（FP8 sparse 的 dequant-bound 与 crossover），把本讲的「为什么这样测」与「为什么这样设计」对上。
3. **深入性能剖析**：把本讲的 `bench_kineto` 换成 `nsys`/`ncu`（注意 `kernelkit` 会检测并避让 profiling 工具），观察 crossover/DSM 如何把反量化瓶颈从 ~50 cycle 砍到 ~25 cycle，把抽象的 TFlops 数字落到具体指令。
4. **横向对比**：用 `benchmark/bench_flash_mla.py` 把 FlashMLA 与 flashinfer / triton / 原生 torch 同形状对比，理解每个实现的优势区间。
5. **回到源码**：以本讲的链路为地图，按 u3–u7 的顺序逐个深入底层 kernel——当你能把「链路里的某一行」与「kernel 里某段 WGMMA/UMMA」对应起来时，就真正读通了 FlashMLA。
