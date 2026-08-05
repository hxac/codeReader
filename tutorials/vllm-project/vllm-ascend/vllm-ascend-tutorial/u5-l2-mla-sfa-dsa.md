# 讲义：MLA / SFA / DSA 与稀疏注意力

## 1. 本讲目标

本讲承接 u5-l1（`AscendAttentionBackend` 的注册与元数据机制），深入 vllm-ascend 为「长上下文、大 KV 缓存」模型准备的**三类高级注意力后端**。读完本讲你应当能够：

- 说清 **MLA、SFA、DSA** 三类注意力分别解决什么问题、对应哪类模型；
- 理解平台层如何用一个三元组 `(use_mla, use_sparse, use_compress)` 把模型路由到正确的后端；
- 掌握 MLA 的「隐式 KV 压缩 + 权重吸收」原理，以及它在 NPU 上的实现要点；
- 理解 SFA / DSA 的「indexer 稀疏选择」与「分层压缩 + 滑窗」机制；
- 说清 `AscendSFAIndexerCacheSpec` 为什么改为继承 `MLAAttentionSpec`（#12849）；
- **理解为什么 #12852 要把 `record_attention_compute_start()` 前移到 `forward` 主体，使无 indexer 的 SFA 层（如 GLM-5.2）也能打开 prefetch gate**（本讲本次更新重点）；
- 了解 `fa3_v1` 等新后端的接入方式，以及 indexer 缓存占位后端的作用。

> **本次更新（`3829122` → `7201c97a6`）影响本讲的改动**：
> - **#12852（本讲新增重点）**：SFA 把 `record_attention_compute_start()` 从 `indexer_select_post_process` 内部**前移**到 `forward` 主体、`skip_topk` 分支之前——目的是让**无 indexer 的 SFA 层（如 GLM-5.2 复用 top-k 的层）也能打开 prefetch gate**（见 4.3.3「prefetch gate 与无 indexer 层」）。
> - 其余文件（`mla_v1.py`、`dsa_v1.py`、`abstract.py`、`indexer.py`、`platform.py`、`utils.py`、`kv_cache_interface.py`、`attention_v1.py`、`fa3_v1.py`）在本次区间**未改动**，仅刷新永久链接 HEAD 与个别行号。
>
> **前序区间（`646684f → 3829122`）已纳入本讲、仍然有效**的改动：
> - **#12849**：`AscendSFAIndexerCacheSpec` 的父类从 `FullAttentionSpec` 改为 `MLAAttentionSpec`（见 4.3）。
> - **#13484**：`NPUPlatform` 重构，`get_attn_backend_cls` 仍在类内，但 FA3 特判 `_validate_fa3_backend` 已下移为**模块级函数**（见 4.1）。
> - **#13456**：CANN 9.1.0 要求 FIAV2 算子的 K/V 连续，标准 fiaV2 后端补了 `.contiguous()`（见 4.2.3）。
> - **#13026**：SFA 后端新增「稀疏 KV 卸载」分流分支（详见 u10-l6），本讲只点出挂载点。
> - **#13447**：SFA 内的 mxfp 量化 dtype 由兼容垫片内联为 `torch_npu.float8_e8m0fnu`。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个直觉。

### 2.1 为什么标准注意力不够用

标准多头注意力（MHA）对每个 token、每个头都要缓存一份 Key 和 Value。当上下文变长（几十万 token）、模型变大（上百层、几十个头），KV 缓存的显存会爆炸。三类「省 KV」思路由此诞生：

- **MLA（Multi-head Latent Attention，隐式注意力）**：不存完整的 K/V，而是存一个低维「隐向量」，注意力时再展开。代表模型 DeepSeek-V2/V3。
- **稀疏注意力（Sparse）**：不让每个 token 看到全部历史，而是用一个「索引器（indexer）」挑出最相关的若干 KV 块，只对它们做注意力。
- **分层压缩（Compress）**：把历史 KV 按比例（1/4、1/128）压缩成「状态缓存（state cache）」，远处用压缩版、近处用滑窗（SWA）原版。代表模型 DeepSeek-V4。

> 术语提示：本讲里的 SFA（Sparse Flash Attention）与 DSA 都属于「带 indexer 的稀疏/压缩注意力」，区别在于**是否额外做分层压缩**。这一点决定了平台如何选后端。

### 2.2 三类注意力与上游 vLLM 的接口

vllm-ascend 并没有为每一类注意力各写一套独立框架，而是复用上游 vLLM 的「注意力后端（`AttentionBackend`）」抽象：每个后端提供三件套——`get_impl_cls()`（真正算注意力的实现类）、`get_builder_cls()`（构建注意力元数据的构建器）、`get_kv_cache_shape()`（声明 KV 缓存的形状）。这一机制已在 u5-l1 讲过，本讲专注三件套在 MLA/SFA/DSA 中的差异。

### 2.3 一个贯穿全讲的关键事实：KV 缓存形状不同

u5-l1 提到，标准后端 `AscendAttentionBackend` 的 KV 缓存形状带一个前导 `2`：

```
(2, num_blocks, block_size, num_kv_heads, head_size)
```

这个 `2` 用来叠放 Key 和 Value。而 MLA/SFA/DSA 的 `get_kv_cache_shape` 都返回 **4 维、不带前导 2**：

```
(num_blocks, block_size, num_kv_heads, head_size)
```

原因是：这些后端不再用「K/V 各一份」的方式存缓存。MLA 存的是「隐向量」；SFA/DSA 的 KV 缓存是一个**元组**，由多个独立张量拼成（如 `(k_nope, k_pe)` 或更复杂的组合），用元组而非前导维来表达「多块缓存」。这是识别一个后端是不是 MLA 系的最快线索。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vllm_ascend/platform.py](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/platform.py) | 平台钩子 `get_attn_backend_cls`，用三元组分发到 MLA/SFA/DSA 后端；FA3 特判 `_validate_fa3_backend`（#13484 后为模块级函数） |
| [vllm_ascend/attention/mla_v1.py](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/mla_v1.py) | MLA 后端：`AscendMLABackend` + `AscendMLAImpl`（隐式 KV 压缩） |
| [vllm_ascend/attention/sfa_v1.py](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/sfa_v1.py) | SFA 后端：`AscendSFABackend`（行 116）+ `AscendSFAImpl`（行 504）；#12852 把 `record_attention_compute_start` 前移到 `forward`（行 2029） |
| [vllm_ascend/attention/dsa_v1.py](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/dsa_v1.py) | DSA 后端：`AscendDSABackend` + `AscendDSAImpl`（分层压缩 + SWA + indexer） |
| [vllm_ascend/attention/abstract.py](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/abstract.py) | DSA 实现的抽象基类 `DSAAttentionImpl` |
| [vllm_ascend/attention/indexer.py](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/indexer.py) | `AscendSFAIndexerBackend`：SFA 索引缓存的「占位后端」 |
| [vllm_ascend/memcache_comm_fence.py](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/memcache_comm_fence.py) | `AttentionComputeStartGate` 与 `record_attention_compute_start`：layerwise 预取的「注意力起点 gate」（#12852 重点） |
| [vllm_ascend/core/kv_cache_interface.py](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/core/kv_cache_interface.py) | KV 缓存规格：`AscendMLAAttentionSpec` 与 `AscendSFAIndexerCacheSpec`（#12849 改继承 `MLAAttentionSpec`） |
| [vllm_ascend/attention/fa3_v1.py](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/fa3_v1.py) | `AscendFABackend`：基于 flash_attn_npu_v3 的训练-推理一致性后端 |
| [vllm_ascend/utils.py](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/utils.py) | `model_uses_sfa_sparse`、`enable_dsa_cp` 等判定函数 |

## 4. 核心概念与源码讲解

### 4.1 平台三元组分发：三类注意力如何被选中

#### 4.1.1 概念说明

vllm-ascend 不在模型代码里硬编码「我用 MLA 还是 SFA」，而是把判定集中到平台层。`NPUPlatform.get_attn_backend_cls` 是一个**纯函数式路由表**：它根据一个三元组 `(use_mla, use_sparse, use_compress)` 查表，返回后端的「类路径字符串」。三元组每一维都来自模型的 HF 配置（`hf_config`）：

- `use_mla`：模型是否使用隐式注意力（MLA 系）。
- `use_sparse`：模型是否带 indexer 但**没有**分层压缩（即 SFA）。
- `use_compress`：模型是否带 `compress_ratios`（即 DSA 的分层压缩）。

这种设计的意义在于：**新增一个 MLA 类模型时，只要它的 HF 配置能被正确归类，平台会自动选中正确后端，无需改模型代码**。这也是 u5-l1 强调的「平台钩子统一返回类路径字符串以延迟 import」在多后端场景下的体现。

#### 4.1.2 核心流程

选型发生在配置校验之后、首个注意力层构建之前：

1. 模型加载阶段，上游 vLLM 要为每个注意力层选后端，调用平台钩子 `get_attn_backend_cls`。
2. 钩子从 `attn_selector_config` 读 `use_mla`、`use_sparse`，从模型运行器读 `use_compress`。
3. 特判：若用户请求 `FLASH_ATTN` 且处于「训练-推理一致性（batch invariant）」场景，且 `flash_attn_npu_v3` 已安装，则改走 FA3 后端。
4. 否则用三元组查 `backend_map`，返回对应后端类路径。
5. 310P 硬件走另一张更小的表（暂不支持 MLA/SFA）。

```
模型 HF 配置
   │
   ├─ 有 index_topk、无 compress_ratios ──► use_sparse=True  (SFA)
   ├─ 有 compress_ratios             ──────► use_compress=True (DSA)
   └─ use_mla=True 且上面都不满足     ──────► MLA
                          │
                          ▼
        get_attn_backend_cls → backend_map[(use_mla, use_sparse, use_compress)]
                          │
                          ▼
   AscendMLABackend / AscendSFABackend / AscendDSABackend
```

#### 4.1.3 源码精读

路由表与 FA3 特判在 [vllm_ascend/platform.py:215-242](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/platform.py#L215-L242)。注意 #13484 重构后，FA3 特判 `_validate_fa3_backend` 已从类方法下移为**模块级函数**，故这里直接 `_validate_fa3_backend(...)` 调用，而非 `cls._validate_fa3_backend(...)`：

```python
# 平台钩子：用三元组查表返回后端类路径
@classmethod
def get_attn_backend_cls(cls, selected_backend, attn_selector_config, num_heads: int | None = None):
    use_compress = getattr(attn_selector_config, "use_compress", False)
    key = (attn_selector_config.use_mla, attn_selector_config.use_sparse)

    # 特判：训练-推理一致性场景改走 FA3（_validate_fa3_backend 在 #13484 后是模块级函数）
    if selected_backend == AttentionBackendEnum.FLASH_ATTN and _validate_fa3_backend(key, attn_selector_config):
        return "vllm_ascend.attention.fa3_v1.AscendFABackend"

    backend_map = {
        (True,  False, False): "vllm_ascend.attention.mla_v1.AscendMLABackend",   # MLA
        (False, False, False): "vllm_ascend.attention.attention_v1.AscendAttentionBackend",  # 标准注意力
        (True,  True,  False): "vllm_ascend.attention.sfa_v1.AscendSFABackend",   # SFA（带 indexer，无压缩）
        (True,  False, True):  "vllm_ascend.attention.dsa_v1.AscendDSABackend",   # DSA（带 compress_ratios）
    }
    ...
    return backend_map[(attn_selector_config.use_mla, attn_selector_config.use_sparse, use_compress)]
```

三元组的两个维度由辅助函数从 HF 配置推断。`use_sparse` 的判定逻辑很关键——它要求「有 `index_topk` 但没有 `compress_ratios`」，这正是 SFA 与 DSA 的分水岭，见 [vllm_ascend/utils.py:111-119](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/utils.py#L111-L119)：

```python
def model_uses_sfa_sparse(model_config):
    hf_text_config = getattr(model_config, "hf_text_config", None)
    hf_config = getattr(model_config, "hf_config", None)
    return (
        hf_text_config is not None
        and hasattr(hf_text_config, "index_topk")          # 带 indexer
        and not hasattr(hf_text_config, "compress_ratios") # 但没有分层压缩 → SFA
        and not hasattr(hf_config, "compress_ratios")
    )
```

而 `use_compress` 在模型运行器初始化时根据 `hf_config` 是否含 `compress_ratios` 一次性确定，见 [vllm_ascend/worker/model_runner_v1.py:296-298](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/worker/model_runner_v1.py#L296-L298)（必须在 `super().__init__()` 之前设置，因为父类初始化分配 KV 张量时会读取它）：

```python
self.use_compress = (
    hf_config is not None and hasattr(hf_config, "compress_ratios")
)
```

FA3 后端的特判 `_validate_fa3_backend` 在 #13484 重构后位于 [vllm_ascend/platform.py:1089-1113](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/platform.py#L1089-L1113)（**模块级函数**，不再是 `NPUPlatform` 的方法）：它要求「batch invariant（训练-推理一致性）」、`key == (False, False)`（非 MLA 非 SFA）、且 `flash_attn_npu_v3` 可导入并提供 `flash_attn_with_kvcache`。FA3 主要用于和训练侧对齐数值，性能通常不如默认 FIA 后端，故仅在一致性场景启用。

#### 4.1.4 代码实践

> **实践目标**：把三类注意力的「适用模型 / kv 压缩 / 是否共享 kv」整理成对照表（本讲指定实践任务）。

操作步骤（源码阅读型，无需 NPU）：

1. 打开 [vllm_ascend/platform.py:223-228](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/platform.py#L223-L228)，确认四条映射。
2. 打开 [vllm_ascend/utils.py:111-119](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/utils.py#L111-L119)，理解 SFA 的判定门槛。
3. 阅读三个后端的 `get_kv_cache_shape`（4.2–4.4 会给出行号），体会 KV 缓存形状的差异。
4. 完成综合实践 Task 1 的对照表（参考答案见第 5 节）。

需要观察的现象：三类后端的 `get_kv_cache_shape` 都返回 4 维、无前导 `2`，但语义不同——MLA 的 `head_size` 实际是隐向量维度；SFA 的缓存是「主 KV + indexer k」元组；DSA 的缓存是「SWA + 压缩状态 + indexer」多张量元组。预期读者能从形状一致推导出「它们都是非标准 K/V 存储」。

#### 4.1.5 小练习与答案

**练习 1**：一个模型 HF 配置同时有 `index_topk` 和 `compress_ratios`，会被路由到哪个后端？为什么？

**参考答案**：会被路由到 **DSA**（`AscendDSABackend`）。因为 `model_uses_sfa_sparse` 要求「没有 `compress_ratios`」才返回 True；既然有 `compress_ratios`，`use_sparse=False`、`use_compress=True`，命中 `(True, False, True) → DSA`。SFA 仅适用于「有 indexer 但无分层压缩」的模型。

**练习 2**：为什么 `get_attn_backend_cls` 返回的是字符串类路径而不是直接 import 类？

**参考答案**：延迟 import（lazy import）。MLA/SFA/DSA 后端各自依赖不同的重型模块（如 DSA 依赖 `scipy`、SFA 依赖 indexer 相关算子），直接 import 会拖慢启动并可能引入循环依赖。返回字符串让上游 vLLM 只在真正需要该后端时才 import。

---

### 4.2 MLA 注意力：隐式 KV 压缩（DeepSeek）

#### 4.2.1 概念说明

MLA（Multi-head Latent Attention）的核心思想：**用低维隐向量代替完整的 K/V**。它把每个 token 的 KV 信息压缩成一个维度为 `kv_lora_rank` 的隐向量 \(c_{KV}\)，缓存里只存这个隐向量；需要时再通过两组权重 \(W_{UK}\)、\(W_{UV}\) 展开成完整的 K 和 V。

普通注意力每个头的 K、V 是：

\[
K_h = c_{KV}\, W_{UK,h}^{\mathsf T}, \qquad V_h = c_{KV}\, W_{UV,h}
\]

直接算的话，得先把 \(c_{KV}\) 展开成完整 K/V，缓存反而更大。MLA 的妙处在于**「权重吸收（weight absorption）」**：对 decode（每条序列只有 1 个 query token）这种场景，可以把 \(W_{UK,h}^{\mathsf T}\) 折进查询里：

\[
o_h = \mathrm{Attn}\!\big(q_h,\; c_{KV} W_{UK,h}^{\mathsf T}\big)\, W_{UV,h}
     = \mathrm{Attn}\!\big(q_h W_{UK,h}^{\mathsf T},\; c_{KV}\big)\, W_{UV,h}
\]

折叠后，注意力直接在**隐向量 \(c_{KV}\)** 上进行（维度小、缓存小），算完再用 \(W_{UV,h}\) 投影回正常输出维度。这样 KV 缓存只存 \(c_{KV}\)（外加一段 RoPE 用的 \(k_{pe}\)），显存大幅下降。

MLA 还有一个特点：隐向量在所有头之间**共享**（MQA 风格），所以 `num_kv_heads` 很小。

> 术语提示：`q_lora_rank` 是查询侧的低秩压缩维度；`qk_nope_head_dim`/`qk_rope_head_dim` 分别是查询里「不带 RoPE」和「带 RoPE」的部分；`kv_lora_rank` 就是上面说的隐向量维度。

#### 4.2.2 核心流程

MLA 后端对 prefill（长序列、多 token）和 decode（每序列 1 token）走两条不同路径：

- **公共预处理 `_mla_preprocess`**：做 `fused_qkv_a_proj`（融合的 Q/KV 低秩投影）→ 拆出 `q_c` 和 `kv_no_split` → 可选 all-gather（序列并行）。
- **decode 路径 `mla_preprocess_decode` → `_forward_decode`**：
  1. `_q_proj_and_k_up_proj`：把 \(W_{UK}\) 吸收进查询，得到 `ql_nope`（隐维度查询）；
  2. `exec_kv_decode`：用 `npu_kv_rmsnorm_rope_cache` 对隐向量做 RMSNorm + RoPE 并写入缓存；
  3. 调 `npu_fused_infer_attention_score_v2` 在隐缓存上算注意力；
  4. `_v_up_proj`：用 \(W_{UV}\) 把结果投影回输出维度。
- **prefill 路径 `mla_preprocess_prefill` → `_forward_prefill`**：prefill token 多，吸收不再划算，于是**展开** \(c_{KV}\)（`kv_b_proj` 得到完整 K/V），在完整 K/V 上算注意力；超长上下文则用 `_compute_prefill_context` 做分块（chunked）累加。

预处理还有一条「融合算子」加速通道 `mlapo` / `fa_quant`（见 4.2.3），把投影+归一化+RoPE+缓存写入融合进单个 CANN 算子。

#### 4.2.3 源码精读

后端类声明 KV 缓存为 4 维，且按 `enable_dcp()` 在普通实现与上下文并行实现间分流，见 [vllm_ascend/attention/mla_v1.py:71-109](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/mla_v1.py#L71-L109)：

```python
class AscendMLABackend(AttentionBackend):
    accept_output_buffer: bool = True

    @staticmethod
    def get_name() -> str:
        # v2 runner 有名称断言，这里临时返回 FLASH_ATTN 规避
        return "ASCEND_MLA" if not envs_vllm.VLLM_USE_V2_MODEL_RUNNER else "FLASH_ATTN"

    @staticmethod
    def get_kv_cache_shape(num_blocks, block_size, num_kv_heads, head_size, cache_type=""):
        # 4 维、无前导 2：缓存的是隐向量，不是 K/V 各一份
        return num_blocks, block_size, num_kv_heads, head_size

    @staticmethod
    def get_impl_cls():
        if enable_dcp():                       # 上下文并行（DCP）时换实现
            from vllm_ascend.attention.context_parallel.mla_cp import AscendMlaDCPImpl
            return AscendMlaDCPImpl
        return AscendMLAImpl
```

> 这段代码说明：MLA 后端返回的 KV 形状是 `(num_blocks, block_size, num_kv_heads, head_size)`，其中 `head_size` 实际承载的是隐向量维度（`kv_lora_rank`），而非完整头维度。这就是 MLA 省 KV 的物理来源。

权重吸收发生在查询投影里，见 [vllm_ascend/attention/mla_v1.py:895-907](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/mla_v1.py#L895-L907)：

```python
# _q_proj_and_k_up_proj：把 W_UK 吸收进查询
def _q_proj_and_k_up_proj(self, x):
    q_nope, q_pe = (
        self.q_proj(x)[0]
        .view(-1, self.num_heads, self.qk_head_dim)
        .split([self.qk_nope_head_dim, self.qk_rope_head_dim], dim=-1)
    )
    q_nope = q_nope.transpose(0, 1)            # (N, B, P)
    ql_nope = torch.bmm(q_nope, self.W_UK_T)   # 折进 W_UK → 隐维度查询
    return ql_nope.transpose(0, 1), q_pe
```

对应的 \(W_{UK}\)、\(W_{UV}\) 是在 `process_weights_after_loading` 里从 `kv_b_proj` 权重**预先拆分并转置**好的（`W_UV`、`W_UK_T`），见 [vllm_ascend/attention/mla_v1.py:909-942](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/mla_v1.py#L909-L942)。这正是「吸收」能在运行期廉价执行的缘故——重排工作在加载后一次性完成。

注意力算完后用 \(W_{UV}\) 投影回输出维度，见 [vllm_ascend/attention/mla_v1.py:871-878](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/mla_v1.py#L871-L878)：

```python
def _v_up_proj(self, x):
    x = x.view(self.num_heads, -1, self.kv_lora_rank)          # (N, B, L)
    x = torch_npu.npu_transpose_batchmatmul(x, self.W_UV, ...) # 隐向量 → V
    x = x.reshape(-1, self.num_heads * self.v_head_dim)
    return x
```

decode 主算子在 [vllm_ascend/attention/mla_v1.py:1375-1579](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/mla_v1.py#L1375-L1579)（`_forward_decode`），它根据是否投机解码、是否 `fa_quant`、是否 NZ 布局选用不同的 `input_layout`（如 `BNSD_NBSD`、`TND_NTD`），再调 `npu_fused_infer_attention_score_v2`，并支持 ACL Graph 捕获。prefill 主算子在 [vllm_ascend/attention/mla_v1.py:1226-1296](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/mla_v1.py#L1226-L1296)（`_forward_prefill`），用 TND 布局展开完整 K/V 算注意力。整体 `forward` 把 decode/prefill 结果拼回 `o_proj_input` 再做输出投影，见 [vllm_ascend/attention/mla_v1.py:1701-1776](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/mla_v1.py#L1701-L1776)。

> **#13456 关联说明（FIAV2 连续性约束）**：MLA decode 与标准注意力都最终落到 CANN 的 `npu_fused_infer_attention_score_v2`（FIAV2）算子。CANN 升级到 9.1.0 后，该算子新增「K/V 必须连续」的约束；在 GQA（分组查询注意力）等场景下，K/V 张量经头分组后可能非连续，会触发错误。#13456 的修复点落在标准 fiaV2 后端 `AscendAttentionBackendImpl` 里，对入参显式 `.contiguous()`，见 [vllm_ascend/attention/attention_v1.py:1314-1317](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/attention_v1.py#L1314-L1317)：
>
> ```python
> attn_output, _ = torch_npu.npu_fused_infer_attention_score_v2(
>     query,
>     key.contiguous(),     # #13456: CANN 9.1.0 要求 K/V 连续
>     value.contiguous(),
>     ...
> )
> ```
>
> MLA/SFA 的 decode 路径如果遇到同样的非连续布局，也遵循同一约束（该修复登记在标准后端，是 FIAV2 家族的统一处理）。

#### 4.2.4 代码实践

> **实践目标**：跟踪一次 MLA decode 的「吸收 → 隐向量注意力 → 重建」数据流。

操作步骤（源码阅读型，无需 NPU）：

1. 从 [vllm_ascend/attention/mla_v1.py:1701](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/mla_v1.py#L1701) 的 `forward` 入手，确认它调用 `_mla_preprocess`（第 1630 行）。
2. 进入 `mla_preprocess_decode`（第 1606 行），找到 `_q_proj_and_k_up_proj`（第 895 行）——这里是「吸收」。
3. 跟到 `_forward_decode`（第 1375 行），找到对 `npu_fused_infer_attention_score_v2` 的调用（第 1569、1575 行）——这里是「隐向量注意力」。
4. 注意 `forward` 末尾 `_v_up_proj`（第 871 行）的返回被写进 `o_proj_input`——这里是「重建」。
5. 画出张量维度的变化：`q_c (q_lora_rank)` → `ql_nope (kv_lora_rank)` → 注意力输出 `(num_heads, kv_lora_rank)` → `v (num_heads*v_head_dim)`。

预期结果：能复述「decode 时查询被折进隐维度、注意力在小维度缓存上完成、再用 W_UV 还原」这条链；并理解 prefill 为什么走另一条路（token 多，吸收不划算，故展开完整 K/V）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 decode 适合做权重吸收，而 prefill 通常展开完整 K/V？

**参考答案**：吸收把 \(W_{UK}\) 折进查询，对 decode 而言查询 token 少（每序列 1 个），折叠开销小，且注意力在低维隐空间完成省访存；而 prefill 一次性有大量查询 token，先折叠查询反而要做大矩阵乘，且 prefill 本身是计算密集型，直接展开 K/V 更直接。

**练习 2**：MLA 后端的 KV 缓存形状为什么没有前导 `2`？

**参考答案**：前导 `2` 是标准注意力用来叠放 K、V 的。MLA 缓存的是隐向量 \(c_{KV}\)（`k_nope`）和 RoPE 段 `k_pe`，它们是**两个独立的张量**（组成元组），且 V 根本不缓存（由 \(W_{UV}\) 在线重建），所以不需要那个 `2`。

---

### 4.3 SFA 稀疏注意力：indexer + top-k 选择

#### 4.3.1 概念说明

SFA（Sparse Flash Attention）服务「带 indexer、但无分层压缩」的模型（如 DeepSeek-V3.2）。它的思路是：**用一个轻量索引器算出每个查询最该看的历史 KV 块，只对这些 top-k 块做完整注意力**，从而把注意力的复杂度从「看全部历史」降到「看选中的若干块」。

索引器（indexer）维护一组**独立的低维索引键 \(k_{li}\)**（维度 `head_dim`，如 128），与主 MLA 缓存分开存放。查询侧也有一组对应的索引查询 \(q_{li}\)。先用 \(q_{li} \cdot k_{li}\) 打分取 top-k：

\[
\text{selected} = \mathrm{TopK}\big(q_{li} \cdot k_{li},\; k\big)
\]

再对选中的主 KV 块执行稀疏注意力（`npu_kv_quant_sparse_flash_attention` 一类算子）。这样长上下文下既省算力，又保留对关键信息的精确访问。

> 术语提示：SFA 里 `indexer` 是模型层的对象（含 `wq_b`、`wk_weights_proj`、`k_norm` 等）；`topk_indices` 是 indexer 选出的块号；`skip_topk` 表示某层复用别层的 top-k（IndexCache 机制），这样的层**自己没有 indexer**——这一点在 4.3.3 的 prefetch gate 里很关键。

#### 4.3.2 核心流程

SFA 的 `forward` 分「融合预处理」与「原生预处理」两条路：

1. **选择预处理类型** `PreprocessType`（`NATIVE` / `PROLOG_V3` / `MLAPO`）：根据量化方式、是否 KV consumer、是否 DSA-CP 决定能否走融合算子。见 [vllm_ascend/attention/sfa_v1.py:755](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/sfa_v1.py#L755)（`_resolve_preprocess_type`）与 [vllm_ascend/attention/sfa_v1.py:786](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/sfa_v1.py#L786)（`_get_fused_type_unsupported_reasons` 收集降级原因）。
2. **indexer 预处理 `indexer_select_pre_process`**：算出索引键 \(k_{li}\)，做 RoPE，必要时做 C8 量化后写入 indexer 缓存。
3. **主 KV 预处理**：NATIVE 路径用 `exec_kv`（`npu_kv_rmsnorm_rope_cache`）把隐向量 RMSNorm+RoPE+写缓存；融合路径用 `npu_mla_prolog_v3` / `mla_preprocess` 一次完成。
4. **打开 prefetch gate（#12852 前移点）**：在选 top-k **之前**调 `record_attention_compute_start()`，标记本层注意力即将在计算流上发射（见 4.3.3）。
5. **选 top-k**：`skip_topk=True` 时复用缓存（`_get_indexcache_topk_indices`），否则调 `indexer_select_post_process` 算出块号。
6. **稀疏注意力 `_execute_sparse_flash_attention_process`**：把 `ql_nope`、`q_pe`、top-k 块号交给 `DeviceOperator.execute_sparse_flash_attention_process`。
7. **`_v_up_proj` + `o_proj`**：还原输出并做输出投影。

SFA 还要处理「主缓存」与「indexer 缓存」的拼装：`_compose_sfa_kv_cache` 把分散分配的主缓存和 indexer 缓存拼成内核期望的元组（C8 量化时布局会变）；开启稀疏 KV 卸载时，主缓存会被注册成 6 元组，需要先剥离出 NPU 上的前两个张量（见 4.3.3）。

#### 4.3.3 源码精读

SFA 后端与 MLA 后端结构同构。**#13026 之后，`get_builder_cls` / `get_impl_cls` 在最前面新增了「稀疏 KV 卸载」分流**：当 `sparse_kv_offload_config.enabled` 时，换用专门的 `AscendSFAKVOffloadMetadataBuilder` / `AscendSFAKVOffloadImpl`（该特性的数据面与配置约束详见 u10-l6）。其余仍按 `enable_sfa_dcp_replicated_indexer()` 在普通实现与 DCP 实现间分流，见 [vllm_ascend/attention/sfa_v1.py:116-162](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/sfa_v1.py#L116-L162)：

```python
class AscendSFABackend(AttentionBackend):
    @staticmethod
    def get_builder_cls():
        if get_ascend_config().sparse_kv_offload_config.enabled:        # #13026: 稀疏 KV 卸载（见 u10-l6）
            from vllm_ascend.attention.sfa_kv_offload import AscendSFAKVOffloadMetadataBuilder
            return AscendSFAKVOffloadMetadataBuilder
        if enable_sfa_dcp_replicated_indexer():                          # DCP 时换构建器
            from vllm_ascend.attention.context_parallel.sfa_cp import AscendSFADCPMetadataBuilder
            return AscendSFADCPMetadataBuilder
        return AscendSFAMetadataBuilder

    @staticmethod
    def get_kv_cache_shape(num_blocks, block_size, num_kv_heads, head_size, cache_type=""):
        return (num_blocks, block_size, num_kv_heads, head_size)         # 4 维、无前导 2

    @staticmethod
    def get_impl_cls() -> type["AscendSFAImpl"]:
        if get_ascend_config().sparse_kv_offload_config.enabled:         # #13026: 稀疏 KV 卸载
            from vllm_ascend.attention.sfa_kv_offload import AscendSFAKVOffloadImpl
            return AscendSFAKVOffloadImpl
        if enable_sfa_dcp_replicated_indexer():                          # DCP 时换实现
            from vllm_ascend.attention.context_parallel.sfa_cp import AscendSFADCPImpl
            return AscendSFADCPImpl
        return AscendSFAImpl
```

> 顺带一提 #13447：本文件内 indexer 量化分支原本引用兼容垫片 `FLOAT8_E8M0FNU_DTYPE`（来自 `vllm_ascend/device/mxfp_compat.py`），现已内联为 `torch_npu.float8_e8m0fnu`，见 [vllm_ascend/attention/sfa_v1.py:1471-1473](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/sfa_v1.py#L1471-L1473)。垫片被移除后，量化 dtype 直接引用 CANN 真实类型，少一层间接。

预处理类型枚举见 [vllm_ascend/attention/sfa_v1.py:79-82](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/sfa_v1.py#L79-L82)：

```python
class PreprocessType(enum.Enum):
    NATIVE = "native"      # 逐步执行，兼容性最好
    PROLOG_V3 = "prolog_v3"  # CANN 融合算子（KV consumer、量化场景）
    MLAPO = "mlapo"        # A3 融合算子（W8A8，≤1024 token）
```

indexer 的核心——选 top-k——在 `indexer_select_post_process`，见 [vllm_ascend/attention/sfa_v1.py:1439-1521](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/sfa_v1.py#L1439-L1521)。它先用 `wq_b` 把 `q_c` 投影成索引查询 `q_li`、做 RoPE，再交给 `DeviceOperator.indexer_select_post_process` 输出块号：

```python
def indexer_select_post_process(self, x, q_c, kv_cache, attn_metadata, cos, sin, ...):
    ...
    q_li, _ = self.wq_b(q_c)                 # 索引查询
    q_li = q_li.view(-1, self.n_head, self.head_dim)
    ...                                       # RoPE
    return DeviceOperator.indexer_select_post_process(
        self, q_li, q_li_scale, ..., weights, kv_cache, attn_metadata, ...
    )                                         # → top-k 块号
```

稀疏注意力本体在 [vllm_ascend/attention/sfa_v1.py:1547-1568](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/sfa_v1.py#L1547-L1568)（`_execute_sparse_flash_attention_process`，为支持稀疏 KV 卸载新增了 `block_table=None` 形参），整体 `forward` 的编排（含 PROLOG_V3/MLAPO/NATIVE 三路、prefetch gate、skip_topk 分支、DSA-CP、o_proj 处理）见 [vllm_ascend/attention/sfa_v1.py:1815-2084](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/sfa_v1.py#L1815-L2084)。

**prefetch gate 与无 indexer 层（本次更新 #12852 重点）**

`record_attention_compute_start()` 是 **layerwise 分层缓冲复用**（#12852，详见 u10-l7）的一个同步原语，来自 [vllm_ascend/memcache_comm_fence.py:86-92](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/memcache_comm_fence.py#L86-L92)。要理解为什么要把它「前移」，先看它打开的是哪扇门：

- 在 layerwise prefill 卸载里，多个 transformer 层**分时复用一组有限的物理设备缓冲**。当前层 L 把自己的 KV 存进一个共享缓冲并跑注意力时，MemCache 后台线程要为**下一层 L+1** 预取（H2D 回载）KV 到另一个刚释放的缓冲。
- 为了保证「预取」不和「L 还在读缓冲」冲突，预取任务在提交时拿走**当前层对应的 gate**——`reset_attention_compute_start_gate()` 每层建一个新 gate，见 [vllm_ascend/memcache_comm_fence.py:64-75](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/memcache_comm_fence.py#L64-L75)；随后阻塞在 `AttentionComputeStartGate.wait()` 上，见 [vllm_ascend/memcache_comm_fence.py:53-61](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/memcache_comm_fence.py#L53-L61)。
- 当 L 的注意力**真正要在计算流上发射**时，调用 `record_attention_compute_start()` 记录一个 NPU event 并打开 gate，见 [vllm_ascend/memcache_comm_fence.py:41-51](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/memcache_comm_fence.py#L41-L51)；后台线程被唤醒，才开始提交 H2D。这样「预取」严格晚于「L 开始用缓冲」。

> 一句话：**gate 把「下一层的 H2D 预取」闸在「当前层注意力开始发射」之后**，避免覆盖仍在被读的共享缓冲。

**#12852 之前的 bug**：`record_attention_compute_start()` 原本写在 `indexer_select_post_process` 末尾、紧贴 `return DeviceOperator.indexer_select_post_process(...)`。也就是说，**只有真正跑了 indexer 的层才会打开 gate**。但有些 SFA 层（如 GLM-5.2）设置 `skip_topk=True`、复用别层缓存的 top-k、本身**没有 indexer**——它们在 `forward` 里走的是 `_get_indexcache_topk_indices` 分支，**根本不会进入 `indexer_select_post_process`**，于是这层对应的 gate 永远关闭，后台预取线程一直阻塞到超时，layerwise 复用时序被打破。

**修复**：把 `record_attention_compute_start()` 从 indexer 内部删掉，前移到 `forward` 主体、`skip_topk` 分支**之前**，见 [vllm_ascend/attention/sfa_v1.py:2026-2033](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/sfa_v1.py#L2026-L2033)：

```python
# Open the prefetch gate for every SFA layer. Some GLM-5.2 layers
# reuse cached top-k indices and have no indexer, so recording this
# inside indexer_select_post_process would leave their gate closed.
record_attention_compute_start()

if self.skip_topk:
    topk_indices = self._get_indexcache_topk_indices(topk_num_tokens)   # 无 indexer 层走这里
else:
    ...
    topk_indices = self.indexer_select_post_process(...)                 # 有 indexer 层走这里
```

这样**每一层 SFA**（无论有没有 indexer）都会在注意力发射前打开自己的 gate，layerwise 预取时序对所有层一致。

**缓存拼装与稀疏 KV 卸载的衔接**：`_compose_sfa_kv_cache`（[vllm_ascend/attention/sfa_v1.py:1757](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/sfa_v1.py#L1757)）负责把主缓存与 indexer 缓存拼成内核元组。开启稀疏 KV 卸载时，主 MLA 缓存会被注册成 6 元组 `(k_npu, v_npu, k_cpu, v_cpu, topk_buffer_k, topk_buffer_v)`，而注意力内核只消费前两个 NPU 张量，故需先剥离，见 [vllm_ascend/attention/sfa_v1.py:1790-1794](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/sfa_v1.py#L1790-L1794)：

```python
# Sparse KV offload registers the main MLA cache as a 6-tuple
# (k_npu, v_npu, k_cpu, v_cpu, topk_buffer_k, topk_buffer_v); the
# attention kernels only consume the leading NPU pair.
if len(main_cache) == OFFLOAD_KV_CACHE_TUPLE_LEN:
    main_cache = (main_cache[OFFLOAD_K_CACHE_NPU_INDEX], main_cache[OFFLOAD_V_CACHE_NPU_INDEX])
```

**indexer 的「占位后端」与缓存规格**

SFA 的 indexer 缓存是一个独立的物理张量，需要两件东西配合才能被 KV 缓存规划器正确分配：

1. **占位后端 `AscendSFAIndexerBackend`**（[vllm_ascend/attention/indexer.py:14-55](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/indexer.py#L14-L55)）：它不参与真正的前向（`build()` 直接返回 `None`），只是「缓存可见性」的载体——让规划器为 indexer 单独分配一块物理张量，并与主 MLA 缓存**共享 block id**。
2. **缓存规格 `AscendSFAIndexerCacheSpec`**（[vllm_ascend/core/kv_cache_interface.py:96-103](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/core/kv_cache_interface.py#L96-L103)）：描述这块 indexer 缓存的形状、dtype、scale 布局等。

**#12849 的关键改动**：`AscendSFAIndexerCacheSpec` 的父类从 `FullAttentionSpec` 改为 `MLAAttentionSpec`：

```python
@dataclass(frozen=True, kw_only=True)
class AscendSFAIndexerCacheSpec(MLAAttentionSpec):   # 改动前: FullAttentionSpec
    """KV cache spec for SFA indexer K/scale cache.

    The scheduler should treat this as a full-attention-compatible cache so it
    can share block ids with the MLA cache in the same UniformType group. ...
    """
```

**为什么改继承 `MLAAttentionSpec`**（详见 4.3.5 练习与第 5 节综合实践）：

- **语义对齐**：indexer 的 K 缓存本质是一块「跨头共享、按 block 组织、带 `compress_ratio` 的单一隐式张量」，正是 `MLAAttentionSpec` 建模的对象；而 `FullAttentionSpec` 描述的是标准「K/V 各一份」的注意力，过于宽泛。
- **保留 block-id 共享**：`MLAAttentionSpec` 本身是 `FullAttentionSpec` 的子类，所以 indexer 缓存仍是「full-attention-compatible」，依旧和主 MLA 缓存落入同一个 `UniformType` 组、共享 block id（二者注册时的 `uniform_type_base_spec` 都是 `FullAttentionSpec`，见 [vllm_ascend/core/kv_cache_interface.py:213-223](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/core/kv_cache_interface.py#L213-L223)）。
- **继承 MLA 的内存核算**：`MLAAttentionSpec` 携带 `compress_ratio` 字段，其 `max_memory_usage_bytes` 按 `cdiv(max_model_len, block_size*compress_ratio)` 计页（见 [vllm_ascend/core/kv_cache_interface.py:86-93](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/core/kv_cache_interface.py#L86-L93)）；`FullAttentionSpec` 没有这一字段，无法正确规划这类 MLA 系单张量缓存的显存。

> 顺带一提：#13026 还给主 MLA 规格 `AscendMLAAttentionSpec` 增加了 `store_on_host: bool = False` 字段（[vllm_ascend/core/kv_cache_interface.py:33](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/core/kv_cache_interface.py#L33)）及其 merge 校验，用于标记「主 KV 驻留主机」的稀疏卸载场景——同样详见 u10-l6。

#### 4.3.4 代码实践

> **实践目标**：理解 prefetch gate 前移（#12852）与 indexer 缓存「占位后端 + MLA 系缓存规格」两件套。

操作步骤（源码阅读型）：

1. 打开 [vllm_ascend/memcache_comm_fence.py:27-61](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/memcache_comm_fence.py#L27-L61)，读 `AttentionComputeStartGate` 的类文档字符串与 `record`/`wait`，确认「gate 在注意力发射时打开、MemCache 线程等它」。
2. 打开 [vllm_ascend/attention/sfa_v1.py:2026-2033](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/sfa_v1.py#L2026-L2033)，看 `record_attention_compute_start()` 现在位于 `skip_topk` 分支**之前**；对照 [vllm_ascend/attention/sfa_v1.py:1439-1521](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/sfa_v1.py#L1439-L1521) 确认它已从 `indexer_select_post_process` 中移除。
3. 思考：若 `skip_topk=True` 的层仍走旧路径（gate 在 indexer 内），它的 gate 会怎样？
4. 打开 [vllm_ascend/attention/indexer.py:14-55](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/indexer.py#L14-L55)，确认 `AscendSFAIndexerBackend`「只让缓存可见、不参与前向」。
5. 打开 [vllm_ascend/core/kv_cache_interface.py:96-157](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/core/kv_cache_interface.py#L96-L157)，确认 `AscendSFAIndexerCacheSpec(MLAAttentionSpec)` 的父类、`merge()` 断言与 `real_page_size_bytes`。

预期结果：能解释「gate 前移 = 让无 indexer 层也打开预取闸门，避免 MemCache 预取线程阻塞超时」；以及「占位后端 = 让规划器分配独立物理缓存；MLA 系缓存规格 = 描述其形状并共享 block id；前向仍由真正的 `*.attn` 层驱动」这一设计。运行结果：待本地验证（需要带 indexer 的模型与 NPU 环境）。

#### 4.3.5 小练习与答案

**练习 1**：SFA 的主注意力是在「隐向量空间」还是「完整 K/V 空间」上做的？

**参考答案**：主注意力仍走 MLA 的隐向量空间（`ql_nope` 是吸收了 \(W_{UK}\) 的隐维度查询，缓存是隐向量）。indexer 只负责**挑选要参与注意力的块**（top-k），并不改变注意力本身在隐空间的计算。所以 SFA = 「MLA 隐式压缩」+「稀疏块选择」。

**练习 2**：`PreprocessType` 有三个取值，为什么 SFA 要保留 `NATIVE`？

**参考答案**：`PROLOG_V3`、`MLAPO` 这类融合算子有前提（特定量化、KV consumer、token 数上限、非 DSA-CP 等）。当模型/配置不满足前提（如未量化、或 token 数超过 `MLAPO_MAX_SUPPORTED_TOKENS=1024`），必须回退到逐步执行的 `NATIVE` 路径以保证正确性。`_resolve_preprocess_type` 与 `_get_fused_type_unsupported_reasons` 负责收集不支持原因并降级。

**练习 3**（#12849）：`AscendSFAIndexerCacheSpec` 为什么从 `FullAttentionSpec` 改继承 `MLAAttentionSpec`？改了之后还能和主 MLA 缓存共享 block id 吗？

**参考答案**：改继承是因为 indexer 的 K 缓存语义上就是一块「跨头共享、带 `compress_ratio`、按 block 组织的单一隐式张量」，与 `MLAAttentionSpec` 一致；`FullAttentionSpec` 描述的是标准 K/V 注意力，且缺少 `compress_ratio`，无法正确核算这类缓存的显存（`max_memory_usage_bytes` 依赖 `compress_ratio`）。改继承后**仍能共享 block id**：因为 `MLAAttentionSpec` 本身是 `FullAttentionSpec` 的子类，indexer 缓存依旧「full-attention-compatible」，与主 MLA 缓存（`AscendMLAAttentionSpec`）落入同一 `UniformType` 组（二者 `uniform_type_base_spec` 都是 `FullAttentionSpec`），从而共享 block id。

**练习 4**（本次更新 #12852）：为什么 `record_attention_compute_start()` 不能继续留在 `indexer_select_post_process` 里？把它前移到 `forward`、`skip_topk` 分支之前解决了什么问题？

**参考答案**：留在 indexer 里意味着「只有跑 indexer 的层才打开 prefetch gate」。但 GLM-5.2 等模型的某些 SFA 层设 `skip_topk=True`、复用别层的 top-k、**没有 indexer**，在 `forward` 里走 `_get_indexcache_topk_indices` 分支，根本不进 `indexer_select_post_process`，于是这些层的 gate 永远关闭，layerwise 预取的后台线程（`AttentionComputeStartGate.wait()`）一直阻塞到超时，缓冲复用时序被打破。前移到 `skip_topk` 分支之前，保证**每一层**（不管有没有 indexer）都在注意力发射前打开自己的 gate，layerwise 复用时序对所有层一致。

---

### 4.4 DSA 注意力：分层压缩 + 滑窗 + indexer（DeepSeek-V4）

#### 4.4.1 概念说明

DSA 服务「带 `compress_ratios`」的模型（如 DeepSeek-V4）。它在 SFA 的「indexer 稀疏」之外，又加了一层**分层压缩**：把历史 KV 按比例（`compress_ratio` = 4 或 128）压成一块「状态缓存（state cache）」，远处用压缩版、近处用滑窗（Sliding Window Attention，SWA）原版。三类缓存分工：

- **SWA 缓存**：最近的若干 token，用完整 KV，靠滑窗做精确注意力（`ori_mask_mode=4` 即滑窗）。
- **compressor 状态缓存**：把更早的 KV 压成 1/N 的「状态」，用一个带门控（gate）的小网络维护。
- **indexer 缓存**：决定哪些压缩状态/块参与注意力（top-k）。

`compress_ratio` 取值决定缓存元组里有多少张量：`=1` 只有 SWA；`=4` 有「attn + compressor 状态 + indexer compressor 状态 + indexer k + SWA」；`=128` 有「attn + compressor 状态 + SWA」。DSA 还引入了 Hadamard 旋转（`rotate_activation`）等数值技巧。

> 术语提示：DSA 实现的抽象基类是 `DSAAttentionImpl`（[abstract.py:18](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/abstract.py#L18)），它的 `forward` 签名与 MLA 不同（接受 `kv_c_normed`、`k_pe` 等），所以平台用 `DSAAttentionImpl` 而非 `MLAAttentionImpl` 作为基类。

#### 4.4.2 核心流程

DSA 的 `forward` 先按 `compress_ratio` 解包出多块缓存与多份元数据，再分发到 prefill/decode：

```
forward
  ├─ unpack_dsa_forward_kv_cache → (compress_kv, swa_kv, state, indexer_k, indexer_scale, indexer_full)
  ├─ 按 compress_ratio 拆 attn_metadata（=1/4/128 三种元组长度）
  ├─ _forward_prefill / _forward_decode（各自处理 SWA + compressor + indexer）
  │     ├─ MLA prolog（wq_a→q_norm→wq_b，wkv→kv_norm→RoPE）
  │     ├─ compressor 算子（生成压缩状态，cmp_ratio=4/128）
  │     ├─ indexer_select_qli（选 top-k）
  │     └─ 稀疏注意力算子（cmp_ratio、ori_win_left/right、sas_metadata）
  ├─ partial RoPE
  └─ _forward_o_proj（A5 FP8 / OTP / olora_tp / 普通 四种输出投影）
```

`build_prefill_metadata` 按 `compress_ratio` 调不同的 `metadata_op`（cmp_ratio=1/4/128），生成稀疏注意力元数据 `sas_metadata` 与 lightning indexer 元数据 `qli_metadata`。

#### 4.4.3 源码精读

DSA 后端声明了**多种 kernel block size**（2/4/8/…/128，区别于 MLA/SFA 只支持 128），并提供 `get_scale_shape`（给压缩状态的 scale 缓存），见 [vllm_ascend/attention/dsa_v1.py:191-231](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/dsa_v1.py#L191-L231)：

```python
class AscendDSABackend(AttentionBackend):
    @staticmethod
    def get_kv_cache_shape(num_blocks, block_size, num_kv_heads, head_size):
        return num_blocks, block_size, num_kv_heads, head_size          # 4 维、无前导 2

    @staticmethod
    def get_scale_shape(num_blocks, block_size, scale_size):
        return num_blocks, block_size, scale_size                        # 压缩状态 scale 的形状

    @staticmethod
    def get_supported_kernel_block_sizes() -> list[int]:
        return [2, 4, 8, 16, 32, 64, 128]                                # 多档 block size

    @staticmethod
    def get_impl_cls():
        if enable_dsa_cp():                                              # DSA 上下文并行时换实现
            from vllm_ascend.attention.context_parallel.dsa_cp import AscendDSACPImpl
            return AscendDSACPImpl
        return AscendDSAImpl
```

> `enable_dsa_cp()`（[vllm_ascend/utils.py:1371-1391](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/utils.py#L1371-L1391)）要求模型有 indexer 且显式开启 `additional_config["enable_dsa_cp"]` 并启用 SP（FlashComm），三者全满足才走 DSA-CP 实现。

`forward` 按 `compress_ratio` 解包缓存与元数据，见 [vllm_ascend/attention/dsa_v1.py:1939-1958](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/dsa_v1.py#L1939-L1958)：

```python
(compress_kv_cache, swa_kv_cache, state_cache, indexer_k_cache,
 indexer_scale_cache, indexer_full_cache) = DeviceOperator.unpack_dsa_forward_kv_cache(
    kv_cache, self.compress_ratio)

if self.compress_ratio == 4:
    # sorted keys: [attn, compressor.state_cache, indexer.compressor.state_cache, indexer.k_cache, swa_cache]
    (compressor_attn_metadata, compressor_kv_state_metadata, _,
     indexer_kv_scale_metadata, swa_metadata) = attn_metadata
    ...
elif self.compress_ratio == 128:
    (compressor_attn_metadata, compressor_kv_state_metadata, swa_metadata) = attn_metadata
    ...
else:  # ratio == 1：只有 SWA
    (swa_metadata,) = attn_metadata
```

`build_prefill_metadata` 里按 `compress_ratio` 选 `cmp_ratio`（1/4/128）和掩码模式（滑窗/causal）调 `metadata_op`，见 [vllm_ascend/attention/dsa_v1.py:815-893](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/dsa_v1.py#L815-L893)（`ori_mask_mode=4` 表示滑窗）。稀疏注意力本体在 `_forward_prefill`，`compress_ratio<=1` 时直接对 SWA 缓存算稀疏注意力，见 [vllm_ascend/attention/dsa_v1.py:2049-2070](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/dsa_v1.py#L2049-L2070)：

```python
attn_op = DeviceOperator.get_dsa_sparse_attn_op()
...
return attn_op(
    q,
    ori_kv=swa_kv_cache,                       # 滑窗完整 KV
    ori_block_table=swa_prefill_metadata.block_table,
    cu_seqlens_q=actual_seq_lengths_query,
    seqused_kv=actual_seq_lengths_key,
    sinks=self.attn_sink,
    metadata=common_prefill_metadata.sas_metadata,  # 稀疏元数据
    softmax_scale=self.softmax_scale,
    cmp_ratio=max(self.compress_ratio, 1),
    ori_mask_mode=4,                           # 滑窗
    ori_win_left=ori_win_left, ori_win_right=ori_win_right,
    layout_q="TND", layout_kv="PA_ND",
    **extra_attn_kwargs,
)[0]
```

输出投影 `_forward_o_proj` 支持四种路径（A5 的 FP8 量化 bmm、`oproj_tp_enable` 的 OTP all-to-all/reduce-scatter、`olora_tp_enable`、普通 bmm），见 [vllm_ascend/attention/dsa_v1.py:1652-1759](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/dsa_v1.py#L1652-L1759)，这是 DSA 在张量并行下区别于 MLA 的地方。

#### 4.4.4 代码实践

> **实践目标**：对照 `compress_ratio` 取值，理解 DSA 缓存元组如何「分层」。

操作步骤（源码阅读型）：

1. 读 [vllm_ascend/attention/dsa_v1.py:1939-1958](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/dsa_v1.py#L1939-L1958)，记下 ratio=1/4/128 各自解包的元组长度。
2. 读 [vllm_ascend/attention/dsa_v1.py:815-893](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/dsa_v1.py#L815-L893)，注意三种 ratio 下 `cmp_ratio`、`ori_mask_mode`、`cmp_mask_mode` 的差异。
3. 列一张表：ratio / 缓存元组内容 / 是否有 compressor 状态 / 掩码模式。

预期结果：能说清「ratio=1 纯 SWA；ratio=4 引入 compressor 状态 + indexer；ratio=128 进一步压缩」的层级关系。运行结果：待本地验证（需 DeepSeek-V4 类带 `compress_ratios` 的模型与 NPU）。

#### 4.4.5 小练习与答案

**练习 1**：DSA 与 SFA 都有 indexer，二者根本区别是什么？

**参考答案**：SFA 的 indexer 只做「块选择」，KV 缓存仍是 MLA 隐向量、不做序列长度压缩；DSA 额外有 `compress_ratios`，引入 compressor 把历史 KV 压成「状态缓存」（1/4 或 1/128），并用滑窗保留近期精确 KV。平台层正是用「有没有 `compress_ratios`」来区分二者（`use_compress`）。

**练习 2**：为什么 DSA 后端要支持多种 kernel block size（2~128），而 MLA/SFA 只支持 128？

**参考答案**：DSA 的 SWA 用滑窗、compressor 用高压缩比，块大小需要与滑窗宽度、压缩比对齐（代码里 `block_size` 通过 `kwargs` 传入，DSpark drafting 时甚至用 SWA 缓存自己的 block size）。支持多档 block size 让 KV 缓存规划器能按层选择合适粒度；MLA/SFA 的缓存粒度相对单一，固定 128 即可。

---

## 5. 综合实践

把本讲的知识串起来，完成下面的「三类注意力对照表」与三次推理。

**任务 1：填表（本讲指定实践任务）**

| 维度 | MLA | SFA | DSA |
| --- | --- | --- | --- |
| 适用模型 | DeepSeek-V2/V3、Qwen3-MLA、GLM-4.6 等 latent attention 模型 | 带 `index_topk`、**无** `compress_ratios` 的模型（如 DeepSeek-V3.2） | 带 `compress_ratios` 的模型（如 DeepSeek-V4、MiniMax-M3） |
| 三元组 `(mla, sparse, compress)` | `(True, False, False)` | `(True, True, False)` | `(True, False, True)` |
| KV 压缩 | 隐式压缩为隐向量 `kv_lora_rank`；V 不存，由 \(W_{UV}\) 重建 | MLA 隐式压缩 + indexer 选 top-k 块（不压序列长度） | 分层压缩（compressor ratio 4/128）+ SWA 近窗 + indexer |
| 是否共享 KV | 是（隐向量跨头共享，MQA 风格） | 主 KV 跨头共享；indexer 维护独立索引键 | 多块缓存分层；SWA / 状态 / indexer 各自独立 |
| 后端类 | `AscendMLABackend` | `AscendSFABackend` | `AscendDSABackend` |
| 实现基类 | `MLAAttentionImpl` | `MLAAttentionImpl` | `DSAAttentionImpl`（[abstract.py:18](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/abstract.py#L18)） |
| KV 缓存形状 | `(num_blocks, block_size, num_kv_heads, head_size)` | 同左（+ indexer 缓存占位） | 同左（+ scale 形状 + 多块元组） |

**任务 2：选型推理**。假设你拿到一个新模型，其 `config.json` 片段如下，判断它会走哪个后端，并给出推理链：

```json
{
  "q_lora_rank": 1536,
  "kv_lora_rank": 512,
  "index_topk": 512,
  "compress_ratios": [4, 128]
}
```

推理链（参考答案）：

1. 有 `q_lora_rank`/`kv_lora_rank` → `use_mla=True`。
2. 有 `compress_ratios` → `use_compress=True`，且使 `model_uses_sfa_sparse` 返回 `False`（它要求无 `compress_ratios`）→ `use_sparse=False`。
3. 命中 `backend_map[(True, False, True)]` → **DSA**（`AscendDSABackend`）。
4. 因 `compress_ratios=[4,128]`，前向会按层在 `compress_ratio=4` 与 `=128` 间切换缓存布局。

**任务 3：缓存规格继承推理（#12849）**。回答两个问题：(a) SFA indexer 的 `AscendSFAIndexerCacheSpec` 现在继承自哪个类？(b) 为什么要从旧的 `FullAttentionSpec` 改成它？改了之后还能否与主 MLA 缓存共享 block id？

参考答案：

(a) 继承自 **`MLAAttentionSpec`**（[kv_cache_interface.py:97](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/core/kv_cache_interface.py#L97)）。

(b) 三点原因：① indexer 的 K 缓存语义上是一块「跨头共享、带 `compress_ratio`、按 block 组织的单一隐式张量」，与 `MLAAttentionSpec` 一致，`FullAttentionSpec`（标准 K/V）过于宽泛；② `MLAAttentionSpec` 携带 `compress_ratio`，其显存核算 `max_memory_usage_bytes` 依赖它（[kv_cache_interface.py:86-93](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/core/kv_cache_interface.py#L86-L93)），`FullAttentionSpec` 缺该字段，无法正确规划显存。改继承后**仍能共享 block id**：`MLAAttentionSpec ⊂ FullAttentionSpec`，indexer 缓存依旧「full-attention-compatible」，与主 MLA 缓存落入同一 `UniformType` 组（二者 `uniform_type_base_spec` 都是 `FullAttentionSpec`，[kv_cache_interface.py:213-223](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/core/kv_cache_interface.py#L213-L223)）。

**任务 4：prefetch gate 前移推理（本次更新 #12852）**。结合 [memcache_comm_fence.py:27-61](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/memcache_comm_fence.py#L27-L61) 与 [sfa_v1.py:2026-2033](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/sfa_v1.py#L2026-L2033) 回答：`record_attention_compute_start()` 打开的「prefetch gate」控制的是谁等谁？为什么把它放在 `skip_topk` 分支**之前**，而不是放在 `indexer_select_post_process` 里？

参考答案：

- **谁等谁**：MemCache 后台预取线程（要为下一层 L+1 把 KV 从主机 H2D 回载到刚释放的共享缓冲）阻塞在 `AttentionComputeStartGate.wait()` 上，等的是**当前层 L 的注意力开始在计算流上发射**这件事（由 `record_attention_compute_start()` 记录的 NPU event 标记）。目的是保证「下一层的回载」晚于「当前层开始读缓冲」，避免覆盖仍在被读的共享缓冲。
- **为什么放 `skip_topk` 之前**：放在 `indexer_select_post_process` 里意味着只有「真正跑 indexer 的层」才打开 gate。但 GLM-5.2 等模型的某些 SFA 层 `skip_topk=True`、复用别层 top-k、**没有 indexer**，走 `_get_indexcache_topk_indices` 分支、根本不进 `indexer_select_post_process`，gate 永不打开 → 预取线程阻塞超时。放在 `skip_topk` 分支**之前**，保证**每一层**（无论有没有 indexer）都打开 gate，layerwise 缓冲复用时序对所有层一致。

## 6. 本讲小结

- 平台用三元组 `(use_mla, use_sparse, use_compress)` 在 [platform.py:223-228](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/platform.py#L223-L228) 把模型路由到 MLA/SFA/DSA 后端；`use_sparse` 要求「有 `index_topk` 无 `compress_ratios`」，`use_compress` 看 `compress_ratios`（#13484 后 FA3 特判 `_validate_fa3_backend` 是模块级函数）。
- MLA（`AscendMLABackend`）做隐式 KV 压缩：缓存隐向量而非 K/V，decode 时把 \(W_{UK}\) 吸收进查询、注意力在小维度隐空间完成、再用 \(W_{UV}\) 重建，是省 KV 的物理来源。其 decode 依赖的 FIAV2 算子在 CANN 9.1.0 起要求 K/V 连续（#13456，标准后端已加 `.contiguous()`）。
- 三类后端的 `get_kv_cache_shape` 都返回 4 维、无前导 `2`，区别于标准注意力；多块缓存用**元组**表达。
- SFA（`AscendSFABackend`）在 MLA 隐空间之上加 indexer 选 top-k 块；`indexer.py` 的 `AscendSFAIndexerBackend` 是为 indexer 缓存单独分配物理张量的「占位后端」；**#12849 后其缓存规格 `AscendSFAIndexerCacheSpec` 改继承 `MLAAttentionSpec`**（语义对齐 + 继承 compress_ratio 显存核算 + 仍可共享 block id）。
- **#12852：SFA 把 `record_attention_compute_start()` 从 `indexer_select_post_process` 前移到 `forward` 的 `skip_topk` 分支之前**，使无 indexer 的 SFA 层（如 GLM-5.2）也能打开 layerwise 预取的 prefetch gate（`memcache_comm_fence.py` 的 `AttentionComputeStartGate`），否则 MemCache 预取线程会因 gate 永不打开而阻塞超时。layerwise 缓冲复用全貌见 u10-l7。
- SFA 后端本次还新增「稀疏 KV 卸载」分流（`get_builder_cls`/`get_impl_cls` 顶部、`_compose_sfa_kv_cache` 剥离 6 元组），数据面与配置约束详见 u10-l6；mxfp 量化 dtype 已内联（#13447）。
- DSA（`AscendDSABackend`，基类 `DSAAttentionImpl`）再加一层 compressor 分层压缩 + SWA 滑窗，按 `compress_ratio`(1/4/128) 切换缓存元组布局，输出投影支持 OTP/olora_tp 等多种 TP 路径。
- `AscendFABackend`（`fa3_v1.py`）仅在「训练-推理一致性 + 非 MLA/SFA + 已装 flash_attn_npu_v3」时启用，用于数值对齐而非极致性能。

## 7. 下一步学习建议

- **分层 prefill KV 缓冲复用（u10-l7）**：本讲的 #12852 prefetch gate 正是 layerwise 复用的同步原语。下一阶段 u10-l7 会专讲 `layerwise_cache_layout.py` 的 `LayerwiseCacheLayout`、`pool_worker`/`pool_scheduler` 的「回载再复用、上一层保存完成后才复用」时序，以及 `record_attention_compute_start` 如何在其中闸住预取，建议接着读。
- **稀疏 KV 卸载（u10-l6）**：本讲多次出现 #13026 的 `sparse_kv_offload_config.enabled` 分流与 `store_on_host` 字段。u10-l6 会专讲 `attention/sfa_kv_offload.py` 独立 SFA 后端、`SparseKVOffloadManager` 与 C++ 内核如何把主 KV 卸载到主机、decode 期按 top-k 回载。
- **上下文并行（CP）**：本讲多次出现 `enable_dcp()` / `enable_dsa_cp()` 的分流。下一讲 u5-l3 会专讲 MLA-CP / SFA-CP / DSA-CP 如何把长序列切分到多卡，建议接着读 `vllm_ascend/attention/context_parallel/` 下的 `mla_cp.py`、`sfa_cp.py`、`dsa_cp.py`。
- **融合算子**：若对 `MLAPO` / `PROLOG_V3` / `fa_quant` 这类融合预处理感兴趣，可读 u6（自定义算子三层）与 `vllm_ascend/device/device_op.py` 里 `mla_preprocess_only_decode`、`execute_sparse_flash_attention_process` 的分发。
- **KV 缓存规划**：想理解 indexer「占位后端 + MLA 系缓存规格」如何参与 KV 规划，可读 `vllm_ascend/core/kv_cache_interface.py` 里的 `AscendMLAAttentionSpec` / `AscendSFAIndexerCacheSpec` 与 `register_ascend_kv_cache_specs`（行 213-223）。
- **量化交互**：MLA/SFA 的 `process_weights_after_loading` 与 W8A8/MXFP 深度耦合（#13447 已把 mxfp dtype 内联），可结合 u10-l1（量化方法体系）一起读。
