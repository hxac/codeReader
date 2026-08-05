# 讲义：MLA / SFA / DSA 与稀疏注意力

## 1. 本讲目标

本讲承接 u5-l1（`AscendAttentionBackend` 的注册与元数据机制），深入 vllm-ascend 为「长上下文、大 KV 缓存」模型准备的**三类高级注意力后端**。读完本讲你应当能够：

- 说清 **MLA、SFA、DSA** 三类注意力分别解决什么问题、对应哪类模型；
- 理解平台层如何用一个三元组 `(use_mla, use_sparse, use_compress)` 把模型路由到正确的后端；
- 掌握 MLA 的「隐式 KV 压缩 + 权重吸收」原理，以及它在 NPU 上的实现要点；
- 理解 SFA / DSA 的「indexer 稀疏选择」与「分层压缩 + 滑窗」机制；
- 了解 `fa3_v1` 等新后端的接入方式，以及 indexer 缓存占位后端的作用。

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
| [vllm_ascend/platform.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py) | 平台钩子 `get_attn_backend_cls`，用三元组分发到 MLA/SFA/DSA 后端 |
| [vllm_ascend/attention/mla_v1.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/mla_v1.py) | MLA 后端：`AscendMLABackend` + `AscendMLAImpl`（隐式 KV 压缩） |
| [vllm_ascend/attention/sfa_v1.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/sfa_v1.py) | SFA 后端：`AscendSFABackend` + `AscendSFAImpl`（indexer + top-k 稀疏） |
| [vllm_ascend/attention/dsa_v1.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/dsa_v1.py) | DSA 后端：`AscendDSABackend` + `AscendDSAImpl`（分层压缩 + SWA + indexer） |
| [vllm_ascend/attention/abstract.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/abstract.py) | DSA 实现的抽象基类 `DSAAttentionImpl` |
| [vllm_ascend/attention/indexer.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/indexer.py) | `AscendSFAIndexerBackend`：SFA 索引缓存的「占位后端」 |
| [vllm_ascend/attention/fa3_v1.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/fa3_v1.py) | `AscendFABackend`：基于 flash_attn_npu_v3 的训练-推理一致性后端 |
| [vllm_ascend/utils.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py) | `model_uses_sfa_sparse`、`enable_dsa_cp` 等判定函数 |

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

路由表与 FA3 特判在 [vllm_ascend/platform.py:795-822](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L795-L822)。注意四元映射：

```python
# 平台钩子：用三元组查表返回后端类路径
@classmethod
def get_attn_backend_cls(cls, selected_backend, attn_selector_config, num_heads=None):
    use_compress = getattr(attn_selector_config, "use_compress", False)
    key = (attn_selector_config.use_mla, attn_selector_config.use_sparse)

    # 特判：训练-推理一致性场景改走 FA3
    if selected_backend == AttentionBackendEnum.FLASH_ATTN and cls._validate_fa3_backend(key, attn_selector_config):
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

三元组的两个维度由辅助函数从 HF 配置推断。`use_sparse` 的判定逻辑很关键——它要求「有 `index_topk` 但没有 `compress_ratios`」，这正是 SFA 与 DSA 的分水岭，见 [vllm_ascend/utils.py:111-119](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py#L111-L119)：

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

而 `use_compress` 在模型运行器初始化时根据 `hf_config` 是否含 `compress_ratios` 一次性确定，见 [vllm_ascend/worker/model_runner_v1.py:276-278](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/model_runner_v1.py#L276-L278)（必须在 `super().__init__()` 之前设置，因为父类初始化分配 KV 张量时会读取它）：

```python
self.use_compress = (
    hf_config is not None and hasattr(hf_config, "compress_ratios")
)
```

FA3 后端的特判 `_validate_fa3_backend` 在 [vllm_ascend/platform.py:824-849](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L824-L849)：它要求「batch invariant（训练-推理一致性）」、`key == (False, False)`（非 MLA 非 SFA）、且 `flash_attn_npu_v3` 可导入并提供 `flash_attn_with_kvcache`。FA3 主要用于和训练侧对齐数值（`fa3_v1.py` 第 3 行 `from flash_attn_npu_v3 import flash_attn_with_kvcache`），性能通常不如默认 FIA 后端，故仅在一致性场景启用。

#### 4.1.4 代码实践

> **实践目标**：把三类注意力的「适用模型 / kv 压缩 / 是否共享 kv」整理成对照表（本讲指定实践任务）。

操作步骤（源码阅读型，无需 NPU）：

1. 打开 [vllm_ascend/platform.py:803-808](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L803-L808)，确认四条映射。
2. 打开 [vllm_ascend/utils.py:111-119](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py#L111-L119)，理解 SFA 的判定门槛。
3. 阅读三个后端的 `get_kv_cache_shape`（4.2–4.4 会给出行号），体会 KV 缓存形状的差异。
4. 完成下表（参考答案见 4.1.5）。

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

后端类声明 KV 缓存为 4 维，且按 `enable_dcp()` 在普通实现与上下文并行实现间分流，见 [vllm_ascend/attention/mla_v1.py:71-109](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/mla_v1.py#L71-L109)：

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

权重吸收发生在查询投影里，见 [vllm_ascend/attention/mla_v1.py:895-907](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/mla_v1.py#L895-L907)：

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

对应的 \(W_{UK}\)、\(W_{UV}\) 是在 `process_weights_after_loading` 里从 `kv_b_proj` 权重**预先拆分并转置**好的（`W_UV`、`W_UK_T`），见 [vllm_ascend/attention/mla_v1.py:909-942](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/mla_v1.py#L909-L942)。这正是「吸收」能在运行期廉价执行的缘故——重排工作在加载后一次性完成。

注意力算完后用 \(W_{UV}\) 投影回输出维度，见 [vllm_ascend/attention/mla_v1.py:871-878](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/mla_v1.py#L871-L878)：

```python
def _v_up_proj(self, x):
    x = x.view(self.num_heads, -1, self.kv_lora_rank)          # (N, B, L)
    x = torch_npu.npu_transpose_batchmatmul(x, self.W_UV, ...) # 隐向量 → V
    x = x.reshape(-1, self.num_heads * self.v_head_dim)
    return x
```

decode 主算子在 [vllm_ascend/attention/mla_v1.py:1375-1579](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/mla_v1.py#L1375-L1579)（`_forward_decode`），它根据是否投机解码、是否 `fa_quant`、是否 NZ 布局选用不同的 `input_layout`（如 `BNSD_NBSD`、`TND_NTD`），再调 `npu_fused_infer_attention_score_v2`，并支持 ACL Graph 捕获。prefill 主算子在 [vllm_ascend/attention/mla_v1.py:1226-1296](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/mla_v1.py#L1226-L1296)（`_forward_prefill`），用 TND 布局展开完整 K/V 算注意力。整体 `forward` 把 decode/prefill 结果拼回 `o_proj_input` 再做输出投影，见 [vllm_ascend/attention/mla_v1.py:1701-1776](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/mla_v1.py#L1701-L1776)。

#### 4.2.4 代码实践

> **实践目标**：跟踪一次 MLA decode 的「吸收 → 隐向量注意力 → 重建」数据流。

操作步骤（源码阅读型，无需 NPU）：

1. 从 [vllm_ascend/attention/mla_v1.py:1701](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/mla_v1.py#L1701) 的 `forward` 入手，确认它调用 `_mla_preprocess`（第 1630 行）。
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

> 术语提示：SFA 里 `indexer` 是模型层的对象（含 `wq_b`、`wk_weights_proj`、`k_norm` 等）；`topk_indices` 是 indexer 选出的块号；`skip_topk` 表示某层复用别层的 top-k（IndexCache 机制）。

#### 4.3.2 核心流程

SFA 的 `forward` 分「融合预处理」与「原生预处理」两条路：

1. **选择预处理类型** `PreprocessType`（`NATIVE` / `PROLOG_V3` / `MLAPO`）：根据量化方式、是否 KV consumer、是否 DSA-CP 决定能否走融合算子。见 [vllm_ascend/attention/sfa_v1.py:739-756](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/sfa_v1.py#L739-L756)。
2. **indexer 预处理 `indexer_select_pre_process`**：算出索引键 \(k_{li}\)，做 RoPE，必要时做 C8 量化后写入 indexer 缓存。
3. **主 KV 预处理**：NATIVE 路径用 `exec_kv`（`npu_kv_rmsnorm_rope_cache`）把隐向量 RMSNorm+RoPE+写缓存；融合路径用 `npu_mla_prolog_v3` / `mla_preprocess` 一次完成。
4. **indexer 选择 `indexer_select_post_process`**：算索引查询 \(q_{li}\)，调用 `DeviceOperator.indexer_select_post_process` 得到 top-k 块号（除非 `skip_topk`）。
5. **稀疏注意力 `_execute_sparse_flash_attention_process`**：把 `ql_nope`、`q_pe`、top-k 块号交给 `DeviceOperator.execute_sparse_flash_attention_process`。
6. **`_v_up_proj` + `o_proj`**：还原输出并做输出投影。

SFA 还要处理「主缓存」与「indexer 缓存」的拼装：`_compose_sfa_kv_cache` 把分散分配的主缓存和 indexer 缓存拼成内核期望的元组（C8 量化时布局会变）。

#### 4.3.3 源码精读

SFA 后端与 MLA 后端结构同构，但 `get_impl_cls` 按 `enable_sfa_dcp_replicated_indexer()` 分流到 DCP 实现，见 [vllm_ascend/attention/sfa_v1.py:112-150](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/sfa_v1.py#L112-L150)：

```python
class AscendSFABackend(AttentionBackend):
    @staticmethod
    def get_kv_cache_shape(num_blocks, block_size, num_kv_heads, head_size, cache_type=""):
        return (num_blocks, block_size, num_kv_heads, head_size)   # 同样 4 维、无前导 2

    @staticmethod
    def get_impl_cls():
        if enable_sfa_dcp_replicated_indexer():                    # DCP 时换实现
            from vllm_ascend.attention.context_parallel.sfa_cp import AscendSFADCPImpl
            return AscendSFADCPImpl
        return AscendSFAImpl
```

预处理类型枚举见 [vllm_ascend/attention/sfa_v1.py:75-78](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/sfa_v1.py#L75-L78)：

```python
class PreprocessType(enum.Enum):
    NATIVE = "native"      # 逐步执行，兼容性最好
    PROLOG_V3 = "prolog_v3"  # CANN 融合算子（KV consumer、量化场景）
    MLAPO = "mlapo"        # A3 融合算子（W8A8，≤1024 token）
```

indexer 的核心——选 top-k——在 `indexer_select_post_process`，见 [vllm_ascend/attention/sfa_v1.py:1423-1506](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/sfa_v1.py#L1423-L1506)。它先用 `wq_b` 把 `q_c` 投影成索引查询 `q_li`、做 RoPE，再交给 `DeviceOperator.indexer_select_post_process` 输出块号：

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

稀疏注意力本体在 [vllm_ascend/attention/sfa_v1.py:1532-1544](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/sfa_v1.py#L1532-L1544)，整体 `forward` 的编排（含 PROLOG_V3/MLAPO/NATIVE 三路、DSA-CP、o_proj 处理）见 [vllm_ascend/attention/sfa_v1.py:1785-2048](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/sfa_v1.py#L1785-L2048)。

SFA 还有一个「占位后端」`AscendSFAIndexerBackend`，专门为了让 KV 缓存规划器为 indexer 单独分配一块物理张量（与主 MLA 缓存共享 block id），见 [vllm_ascend/attention/indexer.py:14-55](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/indexer.py#L14-L55)。它不参与真正的前向计算（`build` 返回 `None`），只是「缓存可见性」的载体：

```python
class AscendSFAIndexerBackend(AttentionBackend):
    """Placeholder backend for split SFA indexer cache layers.
    ... The current SFA forward path still consumes metadata from the real
    ``*.attn`` layer ..., so this backend only needs to make the indexer cache
    visible to cache initialization."""
    @staticmethod
    def get_builder_cls():
        return AscendSFAIndexerMetadataBuilder   # build() 直接返回 None
```

#### 4.3.4 代码实践

> **实践目标**：理解 indexer 缓存为何需要单独的后端「占位」。

操作步骤（源码阅读型）：

1. 打开 [vllm_ascend/attention/indexer.py:14-55](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/indexer.py#L14-L55)，读类文档字符串。
2. 回到 SFA `forward` 的 [vllm_ascend/attention/sfa_v1.py:1799](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/sfa_v1.py#L1799)，看 `_compose_sfa_kv_cache` 如何把主缓存与 `self.indexer.k_cache.kv_cache` 拼成元组。
3. 思考：如果没有这个占位后端，KV 缓存规划器如何知道要为 indexer 分配一块独立张量？

预期结果：能解释「占位后端 = 让规划器分配独立物理缓存 + 与主缓存共享 block id，但前向仍由真正的 `*.attn` 层驱动」这一设计。运行结果：待本地验证（需要带 indexer 的模型与 NPU 环境）。

#### 4.3.5 小练习与答案

**练习 1**：SFA 的主注意力是在「隐向量空间」还是「完整 K/V 空间」上做的？

**参考答案**：主注意力仍走 MLA 的隐向量空间（`ql_nope` 是吸收了 \(W_{UK}\) 的隐维度查询，缓存是隐向量）。indexer 只负责**挑选要参与注意力的块**（top-k），并不改变注意力本身在隐空间的计算。所以 SFA = 「MLA 隐式压缩」+「稀疏块选择」。

**练习 2**：`PreprocessType` 有三个取值，为什么 SFA 要保留 `NATIVE`？

**参考答案**：`PROLOG_V3`、`MLAPO` 这类融合算子有前提（特定量化、KV consumer、token 数上限、非 DSA-CP 等）。当模型/配置不满足前提（如未量化、或 token 数超过 `MLAPO_MAX_SUPPORTED_TOKENS=1024`），必须回退到逐步执行的 `NATIVE` 路径以保证正确性。`_resolve_preprocess_type` 与 `_get_fused_type_unsupported_reasons` 负责收集不支持原因并降级。

---

### 4.4 DSA 注意力：分层压缩 + 滑窗 + indexer（DeepSeek-V4）

#### 4.4.1 概念说明

DSA 服务「带 `compress_ratios`」的模型（如 DeepSeek-V4）。它在 SFA 的「indexer 稀疏」之外，又加了一层**分层压缩**：把历史 KV 按比例（`compress_ratio` = 4 或 128）压成一块「状态缓存（state cache）」，远处用压缩版、近处用滑窗（Sliding Window Attention，SWA）原版。三类缓存分工：

- **SWA 缓存**：最近的若干 token，用完整 KV，靠滑窗做精确注意力（`ori_mask_mode=4` 即滑窗）。
- **compressor 状态缓存**：把更早的 KV 压成 1/N 的「状态」，用一个带门控（gate）的小网络维护。
- **indexer 缓存**：决定哪些压缩状态/块参与注意力（top-k）。

`compress_ratio` 取值决定缓存元组里有多少张量：`=1` 只有 SWA；`=4` 有「attn + compressor 状态 + indexer compressor 状态 + indexer k + SWA」；`=128` 有「attn + compressor 状态 + SWA」。DSA 还引入了 Hadamard 旋转（`rotate_activation`）等数值技巧。

> 术语提示：DSA 实现的抽象基类是 `DSAAttentionImpl`（[abstract.py:18](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/abstract.py#L18)），它的 `forward` 签名与 MLA 不同（接受 `kv_c_normed`、`k_pe` 等），所以平台用 `DSAAttentionImpl` 而非 `MLAAttentionImpl` 作为基类。

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

DSA 后端声明了**多种 kernel block size**（2/4/8/…/128，区别于 MLA/SFA 只支持 128），并提供 `get_scale_shape`（给压缩状态的 scale 缓存），见 [vllm_ascend/attention/dsa_v1.py:191-231](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/dsa_v1.py#L191-L231)：

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

> `enable_dsa_cp()`（[vllm_ascend/utils.py:1371-1391](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py#L1371-L1391)）要求模型有 indexer 且显式开启 `additional_config["enable_dsa_cp"]` 并启用 SP（FlashComm），三者全满足才走 DSA-CP 实现。

`forward` 按 `compress_ratio` 解包缓存与元数据，见 [vllm_ascend/attention/dsa_v1.py:1939-1958](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/dsa_v1.py#L1939-L1958)：

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

`build_prefill_metadata` 里按 `compress_ratio` 选 `cmp_ratio`（1/4/128）和掩码模式（滑窗/causal）调 `metadata_op`，见 [vllm_ascend/attention/dsa_v1.py:815-893](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/dsa_v1.py#L815-L893)（`ori_mask_mode=4` 表示滑窗）。稀疏注意力本体在 `_forward_prefill`，`compress_ratio<=1` 时直接对 SWA 缓存算稀疏注意力，见 [vllm_ascend/attention/dsa_v1.py:2049-2070](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/dsa_v1.py#L2049-L2070)：

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

输出投影 `_forward_o_proj` 支持四种路径（A5 的 FP8 量化 bmm、`oproj_tp_enable` 的 OTP all-to-all/reduce-scatter、`olora_tp_enable`、普通 bmm），见 [vllm_ascend/attention/dsa_v1.py:1652-1759](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/dsa_v1.py#L1652-L1759)，这是 DSA 在张量并行下区别于 MLA 的地方。

#### 4.4.4 代码实践

> **实践目标**：对照 `compress_ratio` 取值，理解 DSA 缓存元组如何「分层」。

操作步骤（源码阅读型）：

1. 读 [vllm_ascend/attention/dsa_v1.py:1939-1958](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/dsa_v1.py#L1939-L1958)，记下 ratio=1/4/128 各自解包的元组长度。
2. 读 [vllm_ascend/attention/dsa_v1.py:815-893](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/dsa_v1.py#L815-L893)，注意三种 ratio 下 `cmp_ratio`、`ori_mask_mode`、`cmp_mask_mode` 的差异。
3. 列一张表：ratio / 缓存元组内容 / 是否有 compressor 状态 / 掩码模式。

预期结果：能说清「ratio=1 纯 SWA；ratio=4 引入 compressor 状态 + indexer；ratio=128 进一步压缩」的层级关系。运行结果：待本地验证（需 DeepSeek-V4 类带 `compress_ratios` 的模型与 NPU）。

#### 4.4.5 小练习与答案

**练习 1**：DSA 与 SFA 都有 indexer，二者根本区别是什么？

**参考答案**：SFA 的 indexer 只做「块选择」，KV 缓存仍是 MLA 隐向量、不做序列长度压缩；DSA 额外有 `compress_ratios`，引入 compressor 把历史 KV 压成「状态缓存」（1/4 或 1/128），并用滑窗保留近期精确 KV。平台层正是用「有没有 `compress_ratios`」来区分二者（`use_compress`）。

**练习 2**：为什么 DSA 后端要支持多种 kernel block size（2~128），而 MLA/SFA 只支持 128？

**参考答案**：DSA 的 SWA 用滑窗、compressor 用高压缩比，块大小需要与滑窗宽度、压缩比对齐（代码里 `block_size` 通过 `kwargs` 传入，DSpark drafting 时甚至用 SWA 缓存自己的 block size）。支持多档 block size 让 KV 缓存规划器能按层选择合适粒度；MLA/SFA 的缓存粒度相对单一，固定 128 即可。

---

## 5. 综合实践

把本讲的知识串起来，完成下面的「三类注意力对照表」与一次端到端的选型推理。

**任务 1：填表（本讲指定实践任务）**

| 维度 | MLA | SFA | DSA |
| --- | --- | --- | --- |
| 适用模型 | DeepSeek-V2/V3、Qwen3-MLA、GLM-4.6 等 latent attention 模型 | 带 `index_topk`、**无** `compress_ratios` 的模型（如 DeepSeek-V3.2） | 带 `compress_ratios` 的模型（如 DeepSeek-V4、MiniMax-M3） |
| 三元组 `(mla, sparse, compress)` | `(True, False, False)` | `(True, True, False)` | `(True, False, True)` |
| KV 压缩 | 隐式压缩为隐向量 `kv_lora_rank`；V 不存，由 \(W_{UV}\) 重建 | MLA 隐式压缩 + indexer 选 top-k 块（不压序列长度） | 分层压缩（compressor ratio 4/128）+ SWA 近窗 + indexer |
| 是否共享 KV | 是（隐向量跨头共享，MQA 风格） | 主 KV 跨头共享；indexer 维护独立索引键 | 多块缓存分层；SWA / 状态 / indexer 各自独立 |
| 后端类 | `AscendMLABackend` | `AscendSFABackend` | `AscendDSABackend` |
| 实现基类 | `MLAAttentionImpl` | `MLAAttentionImpl` | `DSAAttentionImpl`（[abstract.py:18](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/abstract.py#L18)） |
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

## 6. 本讲小结

- 平台用三元组 `(use_mla, use_sparse, use_compress)` 在 [platform.py:803-808](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L803-L808) 把模型路由到 MLA/SFA/DSA 后端；`use_sparse` 要求「有 `index_topk` 无 `compress_ratios`」，`use_compress` 看 `compress_ratios`。
- MLA（`AscendMLABackend`）做隐式 KV 压缩：缓存隐向量而非 K/V，decode 时把 \(W_{UK}\) 吸收进查询、注意力在小维度隐空间完成、再用 \(W_{UV}\) 重建，是省 KV 的物理来源。
- 三类后端的 `get_kv_cache_shape` 都返回 4 维、无前导 `2`，区别于标准注意力；多块缓存用**元组**表达。
- SFA（`AscendSFABackend`）在 MLA 隐空间之上加 indexer 选 top-k 块；`indexer.py` 的 `AscendSFAIndexerBackend` 是为 indexer 缓存单独分配物理张量的「占位后端」。
- DSA（`AscendDSABackend`，基类 `DSAAttentionImpl`）再加一层 compressor 分层压缩 + SWA 滑窗，按 `compress_ratio`(1/4/128) 切换缓存元组布局，输出投影支持 OTP/olora_tp 等多种 TP 路径。
- `AscendFABackend`（`fa3_v1.py`）仅在「训练-推理一致性 + 非 MLA/SFA + 已装 flash_attn_npu_v3」时启用，用于数值对齐而非极致性能。

## 7. 下一步学习建议

- **上下文并行（CP）**：本讲多次出现 `enable_dcp()` / `enable_dsa_cp()` 的分流。下一讲 u5-l3 会专讲 MLA-CP / SFA-CP / DSA-CP 如何把长序列切分到多卡，建议接着读 `vllm_ascend/attention/context_parallel/` 下的 `mla_cp.py`、`sfa_cp.py`、`dsa_cp.py`。
- **融合算子**：若对 `MLAPO` / `PROLOG_V3` / `fa_quant` 这类融合预处理感兴趣，可读 u6（自定义算子三层）与 `vllm_ascend/device/device_op.py` 里 `mla_preprocess_only_decode`、`execute_sparse_flash_attention_process` 的分发。
- **KV 缓存规划**：想理解 indexer「占位后端」如何参与 KV 规划，可读 `vllm_ascend/core/kv_cache_interface.py` 里的 `AscendSFAIndexerCacheSpec` 等缓存 spec。
- **量化交互**：MLA/SFA 的 `process_weights_after_loading` 与 W8A8/MXFP8 深度耦合，可结合 u10-l1（量化方法体系）一起读。
