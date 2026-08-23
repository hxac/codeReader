# OpenPanguV2 MoE 模型结构总览

## 1. 本讲目标

本讲带你从整体上精读 `pangu_v2_moe.py`（约 2432 行），这是 openPangu-2.0-Flash（92B）/Pro（505B）在 vLLM 中的模型实现。学完后你应该能够：

1. 说出模型文件中 `OpenPanguV2MLP`、`OpenPanguV2MOE`、`OpenPanguV2DecoderLayer`、`OpenPanguV2Model`、`OpenPanguV2ForCausalLM` 五层"积木"各自的职责与组合方式。
2. 解释一份 openPangu-2.0 权重的 `config.json` 是如何被 omni-npu 识别与注册的：`AutoConfig`/`ModelRegistry` 注册、`match_hf_configs.json` 匹配、MLA 架构补丁三步各管什么。
3. 梳理 `load_weights` 的四大分支，以及权重在张量并行（TP）与专家并行（EP）下被切分加载的入口。

本讲只看"骨架"：注意力、MoE 算子、mHC 等子模块的内部实现分别在 u3-l2、u3-l3、u3-l4 展开。

## 2. 前置知识

阅读本讲前，建议你已了解 u2-l3 的结论：vLLM 执行器按固定生命周期驱动 `NPUWorker`，其中 `load_model` 阶段会根据模型架构名实例化模型类，随后由 loader 逐个张量调用模型的 `load_weights`。本讲正是这条链路中"模型本体"的部分。

再用三句话补齐概念：

- **MoE（Mixture of Experts，混合专家）**：把 Transformer 每层的 FFN 换成"很多个专家 FFN + 一个路由器"。每个 token 只激活其中少数几个专家（`num_experts_per_tok` 个），从而在参数量巨大的同时控制计算量。openPangu-2.0 在此基础上还有一个**共享专家**（shared expert），对所有 token 恒定生效，与路由专家（routed experts）输出相加。
- **Dense 层与 MoE 层混排**：openPangu-2.0 不是每层都用 MoE——前 `first_k_dense_replace` 层用普通稠密 FFN（Dense MLP），其余层换成 MoE。这就是"分清哪层是 Dense、哪层是 MoE"的来源。
- **TP/EP 切分**：TP（Tensor Parallel，张量并行）把单个矩阵按行/列切到多卡；EP（Expert Parallel，专家并行）把**不同的专家**放到不同的卡。一个 MoE 权重加载时走哪条路，取决于它属于 attention/共享专家（TP）还是路由专家（EP）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py` | 模型本体：五层"积木"与前向、权重加载全部在此（2432 行） |
| `components/omni-npu/src/omni_npu/v1/models/__init__.py` | `register_models()`：向 transformers 的 `AutoConfig` 与 vLLM 的 `ModelRegistry` 注册 openPangu V2 |
| `components/omni-npu/src/omni_npu/model_config/configs/match_hf_configs.json` | "超参指纹库"：用一组超参组合反查模型名（如 `openpangu_v2_92B`），供最佳实践配置系统使用 |
| `components/omni-npu/src/omni_npu/model_config/config_loader/loader.py` | `parse_hf_config()`：执行 match_hf_configs.json 匹配的代码 |
| `components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_moe/patch_model_arch_config_convertor.py` | 模型专属补丁：让 vLLM 的架构转换器认识 `openpangu_v2` 是 DeepSeek 风格 MLA |

## 4. 核心概念与源码讲解

### 4.1 五层积木：从 MLP 到 ForCausalLM（MoE Transformer 结构）

#### 4.1.1 概念说明

一个 vLLM 模型实现通常是一个"洋葱"结构，openPangu V2 也不例外。从内到外五层：

| 层 | 类名 | 职责 |
| --- | --- | --- |
| ① 稠密 FFN | `OpenPanguV2MLP` | 经典 `gate_up_proj → SiLU·Mul → down_proj` 三段式 FFN；既给 Dense 层用，也被复用为 MoE 里的**共享专家** |
| ② MoE FFN | `OpenPanguV2MOE` | 路由器 gate + 共享专家 + 路由专家（`NPUSharedFusedMoE`），按 4 种通信策略分发前向 |
| ③ 解码层 | `OpenPanguV2DecoderLayer` | 一个 Transformer block：注意力 + FFN（Dense 或 MoE 二选一）+ 四个 RMSNorm + 可选 mHC 模块 |
| ④ 模型主体 | `OpenPanguV2Model` | 词表 Embedding + N 层 DecoderLayer 堆叠 + 尾部 norm，处理 PP（流水线并行）切分 |
| ⑤ 推理外壳 | `OpenPanguV2ForCausalLM` | 持有 `OpenPanguV2Model` + `lm_head` + `logits_processor`，实现 `load_weights` 等 vLLM 接口 |

为什么要分五层？因为 vLLM 的各子系统只认特定层次：流水线并行在 ④/⑤ 切（`PPMissingLayer`），投机解码与 MoE 统计在 ⑤ 查询（`num_moe_layers` 等），权重点缀与量化钩子在 ①~③ 生效。分层让每类关注点都有明确的挂载点。

#### 4.1.2 核心流程

一次前向的数据流（自顶向下）：

```text
OpenPanguV2ForCausalLM.forward(input_ids, positions)
  └─ OpenPanguV2Model.forward
       ├─ embed_tokens(input_ids) → hidden_states          # [num_tokens, hidden_size]
       ├─ （可选）MHC 多流扩展、TP padding
       ├─ layers[0].mhc_head(...)                          # MHC 开启时的头部变换
       ├─ for 每层 DecoderLayer.forward:
       │     ├─ self_attn(hidden_states, cos, sin)         # 稀疏注意力（u3-l2 详讲）
       │     ├─ mhc_sandwich_norm_post_pre(...)            # 注意力后 / FFN 前的三明治 norm
       │     ├─ mlp(hidden_states)                          # Dense MLP 或 MOE
       │     └─ mhc_sandwich_norm_post_pre(...)            # FFN 后 / 下一层前的三明治 norm
       └─ 尾层: norm → 返回 hidden_states
  └─ compute_logits → lm_head → logits_processor
```

其中 MoE 层内部的分发逻辑是"两级路由"：

```text
OpenPanguV2MOE.forward(hidden_states)
  ├─ total_len > moe_seq_split_length? ── 是 → 按 token 维切块，逐块走 _forward_single 再拼接
  └─ _forward_single 按 moe_comm_strategy / 硬件分流：
       ├─ ascend950                     → _forward_fused_moe        （融合算子直通）
       ├─ TP>1 且 "allreduce"           → _forward_allgather(use_allreduce=True)
       ├─ "all2allv"                    → _forward_all2allv
       ├─ "dispatch_combine"            → _forward_dispatch_combine
       ├─ "allgather_reducescatter"     → _forward_allgather(use_allreduce=False)
       └─ 其余                          → _forward_fused_moe
```

最朴素的 `_forward_fused_moe` 只有三步：`gate` 算路由 logits → `self.experts`（NPUSharedFusedMoE）一次前向同时产出共享专家与路由专家输出 → 两者相加。通信策略变化时，变的只是"token 和专家分数如何在 EP 组内搬运"，算子本身不变。

#### 4.1.3 源码精读

**① `OpenPanguV2MLP`：三段式稠密 FFN。**

[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L132-L172](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L132-L172) 定义了它的构造：`gate_up_proj` 是 `MergedColumnParallelLinear`（把 gate 和 up 两个矩阵合并成一个大矩阵，TP 时按列切），`down_proj` 是 `RowParallelLinear`（TP 时按行切）；`check_ffn_act_fn` 强制激活函数只能是 `silu`——因为 NPU 融合内核只实现了 SiLU。

[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L174-L196](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L174-L196) 的 forward 里有两个易被忽略的细节：TP>1 且通信策略不是 `allreduce` 时，进入 FFN 前先 `all_gather` 补全序列、算完再 `reduce_scatter` 切回去（序列并行的通信换计算）；`disable_tp=True` 时则完全跳过——这正是共享专家的用法。

**② `OpenPanguV2MOE`：路由 + 共享专家 + 融合路由专家。**

[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L234-L261](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L234-L261) 构造路由侧：`gate` 是 `ReplicatedLinear`（每卡完整一份，不切分，输出维度 = `n_routed_experts`，且用 float32 保路由精度）；`e_score_correction_bias` 是 DeepSeek 式的专家分数修正偏置；`shared_experts` 直接复用 ① 的 `OpenPanguV2MLP`，其中间维度 = `moe_intermediate_size × n_shared_experts`，且 `disable_tp=True`——**共享专家每卡完整复制，不参与 TP 切分**。

[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L275-L307](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L275-L307) 构造 `NPUSharedFusedMoE`（u3-l3 精读）并推导 EP 布局：逻辑专家数 = `n_routed_experts`；开启 EPLB 时物理专家数 = 逻辑专家数 + 冗余专家数 × EP 组大小；`n_local_physical_experts` 是本卡持有的物理专家数，`physical_expert_start/end` 圈出本卡负责的专家编号区间——这三个量是 4.3 节权重按 EP 切分的依据。

[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L318-L387](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L318-L387) 是两级路由的外层：`forward` 负责"超长序列切块"（`moe_seq_split_length` 控制，避免一次 grouped matmul 吃满内存），`_forward_single` 负责按通信策略选择 5 条前向路径之一。配合 [components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L389-L413](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L389-L413) 的 `_forward_fused_moe` 可以看到最简路径：gate 打分 → `self.experts(hidden_states, router_logits)` 一次返回 `(shared_output, final_hidden_states)` → 相加。

**③ `OpenPanguV2DecoderLayer`：组装一个 block。**

[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L1256-L1357](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L1256-L1357) 是本讲最重要的构造函数，注意**子模块的注册顺序**：

1. `attn_mhc_module` / `mlp_mhc_module`（可选 mHC，L1302-1316）——刻意声明在 attention/mlp **之前**，因为 `state_dict()` 按 `_modules` 顺序遍历，checkpoint 里的权重名前缀顺序以此为準（L1295-1301 的注释专门解释了这一点）；
2. `self_attn = NPUPanguSparseAttention(...)`（L1318-1336），吃进 `qk_nope_head_dim`/`kv_lora_rank` 等 MLA 超参；L1283-1288 强制要求这些字段存在，否则直接 `ValueError`——**openPangu V2 必须是 MLA 架构**；
3. `mlp`：L1338-1347 的条件 `layer_idx >= first_k_dense_replace` 且 `n_routed_experts` 存在时构造 `OpenPanguV2MOE`，否则 L1349-1357 构造稠密 `OpenPanguV2MLP`（用 `intermediate_size`，而非 `moe_intermediate_size`）；
4. 四个 RMSNorm：`input_layernorm` → `post_attention_layernorm` → `pre_mlp_layernorm` → `post_mlp_layernorm`（L1364-1380），外加可选的 `block_post_layernorm`（L1383-1390）。两两夹住 attention 和 MLP，故源码称之为"sandwich norm"。

[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L1647-L1787](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L1647-L1787) 的 `forward` 主体只做四件事：处理 mHC 延迟回调（L1664-1722，u3-l4 详讲）→ `self.self_attn(...)` → `mhc_sandwich_norm_post_pre`（注意力后/FFN 前）→ `self.mlp(...)` → 再一次 `mhc_sandwich_norm_post_pre`（FFN 后）。返回五元组 `(hidden_states, residual, h_post, h_res, sk_event)`——比常见模型多出的三个量都是 mHC 多流残差流（u3-l4 承接）。

**④ `OpenPanguV2Model`：堆 N 层 + PP 切分。**

[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L1965-L2016](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L1965-L2016)：`embed_tokens` 用 `NPUVocabParallelEmbedding`，且只在 PP 首卡（或 tie embedding 的末卡）创建，否则是占位 `PPMissingLayer`；`make_layers` 按 `num_hidden_layers` 批量构造 DecoderLayer 并返回本进程负责的 `[start_layer, end_layer)` 区间；末卡才有 `norm`。

[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L2033-L2056](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2033-L2056) 有个精巧设计：给每层预登记 `_tail_refs`——最后一层指向 `(merge_mhc_module, norm)`，其余层指向下一层的 `(attn_mhc_module, input_layernorm)`。这样 DecoderLayer 在 FFN 之后要做尾部 norm 时，不需要知道自己在流水线里的位置，查 `_tail_refs` 即可（forward 中 L1769 的 `nxt_mhc_pre, nxt_layernorm, is_model_tail = self._tail_refs`）。`enable_multi_stream` 打开时还会创建共享的 `side_stream/fetch_stream` 并注入每层。

[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L2061-L2112](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2061-L2112) 的 `forward`：PP 首卡做 embedding，非首卡从 `intermediate_tensors` 恢复 `hidden_states/residual`；MHC 开启时把 hidden_states 复制 `mhc_num_stream` 份（L2084-2086）；cos/sin 从首层 rotary 的缓存表按 positions 索引（L2088-2089，避免每层重算）；非末卡把中间张量打包成 `IntermediateTensors` 传给下一段流水线。

**⑤ `OpenPanguV2ForCausalLM`：vLLM 的对接面。**

[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L2115-L2126](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2115-L2126)：类同时继承 `SupportsPP`、`SupportsLoRA`、`MixtureOfExperts`、`IsHybrid` 等接口标记——vLLM 靠这些 mixin 判断模型能力（能否流水线并行、是不是 MoE、是不是混合架构）。`packed_modules_mapping` 声明"checkpoint 里的两个独立权重合并进一个参数"的映射，是 4.3 节权重加载的关键。

[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L2158-L2230](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2158-L2230)：构造 `lm_head`（`NPUParallelLMHead`，支持本地/DP 两种并行开关）与 `NPULogitsProcessor`；随后 L2210-2230 遍历所有层，挑出 MoE 层的 `experts` 汇总成 `moe_layers` 列表，并从"样例 MoE 层"提取全局 MoE 超参（逻辑/物理/本地专家数等）——外部系统（如 u9 的 EPLB）通过这些属性感知模型拓扑，`update_physical_experts_metadata`（L2258-2273）则支持运行时热更新专家映射。

#### 4.1.4 代码实践：超参落点表 + 实例化顺序统计

**实践目标**：拿一份 openPangu-2.0 的超参描述，在 `pangu_v2_moe.py` 中逐一找到它的"落点"（哪个类、哪一行消费了它），并统计 `OpenPanguV2DecoderLayer.__init__` 中子模块的注册顺序。

**操作步骤**（无 NPU 也能完成，纯源码阅读 + 本地小脚本）：

1. 如果你手上有真实权重的 `config.json`，用它；没有的话，用 `match_hf_configs.json` 里的指纹作为"等价 config"。例如 92B 的超参指纹（见 4.2.3）：`hidden_size=2560`、`num_attention_heads=48`、`vocab_size=151552`、`intermediate_size=9216`、`n_routed_experts=256`、`n_shared_experts=1`、`moe_intermediate_size=1024`。
2. 逐项在 `pangu_v2_moe.py` 中 `grep` 该字段名，记录每个出现点的类与行为。
3. 数一遍 `OpenPanguV2DecoderLayer.__init__`（L1256-1419）里赋值给 `self.xxx` 的子模块顺序。

下面是参考答案模板（**示例代码**，可直接保存为 `trace_config.py` 在本仓库任意位置运行，它只读 JSON 不依赖 torch）：

```python
# 示例代码：从 match_hf_configs.json 提取 openPangu V2 三个规格的超参指纹
import json

p = "components/omni-npu/src/omni_npu/model_config/configs/match_hf_configs.json"
data = json.load(open(p))
for name in ("openpangu_v2_35B", "openpangu_v2_92B", "openpangu_v2_505B"):
    print(name, "->", json.dumps(data[name], ensure_ascii=False))
```

预期输出（**待本地验证**，格式为准）：

```text
openpangu_v2_35B -> {"model_type": "openpangu_v2", "hidden_size": 2560, ..., "n_routed_experts": 192, "n_shared_experts": 2, "moe_intermediate_size": 512}
openpangu_v2_92B -> {"model_type": "openpangu_v2", "hidden_size": 2560, ..., "n_routed_experts": 256, "n_shared_experts": 1, "moe_intermediate_size": 1024}
openpangu_v2_505B -> {"model_type": "openpangu_v2", "hidden_size": 5120, ..., "n_routed_experts": 384, "n_shared_experts": 1, "moe_intermediate_size": 1792}
```

**超参落点参考答案**（92B 为例）：

| 超参 | 落点 | 说明 |
| --- | --- | --- |
| `hidden_size=2560` | `OpenPanguV2MLP.__init__` L152/L160、`self_attn` L1321、`RMSNorm` L1364-1380、`embed_tokens` L1985-1990 | 所有投影与 norm 的输入输出维度 |
| `num_attention_heads=48` | `NPUPanguSparseAttention` 构造参数 L1322 | 注意力头数 |
| `vocab_size=151552` | `NPUVocabParallelEmbedding` L1985、`NPUParallelLMHead` L2188、`NPULogitsProcessor` L2200 | 词表两端 |
| `intermediate_size=9216` | Dense 层 `OpenPanguV2MLP` L1351 | 仅 Dense FFN 使用 |
| `moe_intermediate_size=1024` | `OpenPanguV2MOE` L251/L280 | 单个路由专家的中间维度；共享专家中间维 = 1024×1 |
| `n_routed_experts=256` | `gate` 输出维度 L236、`NPUSharedFusedMoE` L277 | 路由专家总数 |
| `n_shared_experts=1` | L251 | 共享专家个数（融合进 `shared_experts` 的中间维度） |
| `hidden_act="silu"` | `check_ffn_act_fn` L232/L171 | 非 silu 直接抛错 |
| `qk_nope_head_dim` 等 MLA 字段 | L1323-1327；缺失则 L1283-1288 抛错 | MLA 必需 |
| `first_k_dense_replace` | L1338-1341 | 决定该层是 Dense 还是 MoE 的分界线 |
| `mhc_num_stream` | L1274/L1276、Model L1979-1980 | >1 才启用 mHC 多流 |
| `routed_scaling_factor` | L212 | 路由输出缩放 |
| `num_experts_per_tok` / `norm_topk_prob` | L278 / L282 | top-k 与是否重归一化 |

**DecoderLayer 子模块实例化顺序参考答案**（对权重文件中的参数名前缀顺序有直接影响）：

1. `attn_mhc_module`（可选）
2. `mlp_mhc_module`（可选）
3. `self_attn`
4. `mlp`（`OpenPanguV2MOE` 或 `OpenPanguV2MLP`）
5. `input_layernorm`
6. `post_attention_layernorm`
7. `pre_mlp_layernorm`
8. `post_mlp_layernorm`
9. `block_post_layernorm`（可选，仅 `layer_idx` 出现在 `block_post_layernorm_idx` 时）

**需要观察的现象**：mHC 模块排在 `self_attn` 之前不是随手写的——L1295-1301 注释说明这是为了让 `state_dict()` 先看到 `attn_mhc_module`/`mlp_mhc_module` 前缀。若你用 `torch.load` 打开真实 safetensors 权重（**待本地验证**，需有权重），参数名顺序应与此一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `OpenPanguV2MOE` 里的 `shared_experts` 构造时传 `disable_tp=True`，而 Dense 层的 MLP 不传？

**答案**：Dense 层 MLP 是模型唯一的 FFN，必须参与 TP 切分才能放下；共享专家在 EP 部署下每个 EP rank 都要能独立算出完整的共享专家输出（通信策略注释里的 "local shared expert"），因此整卡复制、不切分（L252-261），其 forward 中 L179/L190 的两个 `if not self.disable_tp` 分支即为跳过通信的开关。

**练习 2**：一个 505B checkpoint（`n_routed_experts=384`，`n_shared_experts=1`，`moe_intermediate_size=1792`）在 8 卡 EP 下，每张卡的路由专家中间权重相当于多大的 FFN？

**答案**：`n_local_physical_experts = 384 // 8 = 48`（未开 EPLB 冗余时，见 L301），每卡路由专家权重等效 48 个 `1792×2` 中间维的 FFN；另有 1 个完整共享专家（中间维 1792×1，复制不切分）。注意"中间维 = hidden→intermediate 的宽度"，实际门控矩阵还要乘 `hidden_size=5120`。

**练习 3**：`OpenPanguV2DecoderLayer.forward` 返回五元组而不是通常的两元组，多出的 `h_post/h_res/sk_event` 是干什么的？

**答案**：它们是 mHC（多头残差流）跨层传递的辅助状态：`h_post/h_res` 是 mhc_pre 变换后的两路残差，`sk_event` 是 side stream 上 sinkhorn 完成事件（或 `_DEFERRED_SINKHORN` 哨兵，L77-81）——用于把下一层的 mHC 预处理与当前层算子重叠。详细机制在 u3-l4 展开。

### 4.2 模型如何被识别与注册（vLLM 模型接口）

#### 4.2.1 概念说明

"模型被识别"其实涉及**三条互相独立的链路**，初学者最容易把它们混为一谈：

1. **架构注册（谁来实例化模型类）**：权重 `config.json` 里的 `architectures: ["OpenPanguV2ForCausalLM"]` 指向一个类名。omni-npu 通过 vLLM 的 `ModelRegistry` 把这个名字登记到自己的实现；同时 transformers 的 `AutoConfig` 需要认识 `model_type: "openpangu_v2"` 才能解析 config。
2. **超参指纹匹配（选哪份最佳实践配置）**：`match_hf_configs.json` 用"超参组合 → 模型名"的反查表，把 92B/505B 等规格区分开，供 u5-l1 的最佳实践配置系统加载对应的算子开关。它**不决定**用哪个模型类。
3. **架构能力补丁（vLLM 如何理解这个架构）**：vLLM 的 `ModelArchConfigConvertor` 判断模型是否 DeepSeek 风格 MLA，但它内置的白名单不认识 `openpangu_v2`，需要运行时补丁补上——这就是本讲第三个源码文件的作用，也是 u2-l4 补丁机制的一个真实样例。

#### 4.2.2 核心流程

```text
vLLM 启动（entry point omni_custom_models，见 u2-l1）
  └─ register_models()
       ├─ AutoConfig.register("openpangu_v2", OpenPanguV2Config)   # 链路①a：config 可解析
       └─ ModelRegistry.register_model("OpenPanguV2ForCausalLM", "…pangu_v2_moe:OpenPanguV2ForCausalLM")
                                                                     # 链路①b：架构名 → 类路径（延迟 import）

加载权重目录
  ├─ 读 config.json → architectures 命中注册表 → 实例化 OpenPanguV2ForCausalLM
  ├─ parse_hf_config(hf_config)                                       # 链路②：超参指纹反查
  │    ├─ 遍历 match_hf_configs.json，全部字段相等才算命中
  │    ├─ 0 个命中 → 用 model_type 当名字；多个命中 → 仅 deepseek_v3/v32 有消歧规则，否则报错
  │    └─ 再从 quantization_config 推导 quant_type（bf16 / w8a8 / …）
  │    → (model_name, quant_type) 交给 u5-l1 的 best_practice_configs.json
  └─ ModelArchConfigConvertor.is_deepseek_mla 被补丁替换              # 链路③
       └─ model_type == "openpangu_v2" 且 kv_lora_rank 非空 → 按 MLA 处理
```

#### 4.2.3 源码精读

**链路①：注册。**

[components/omni-npu/src/omni_npu/v1/models/__init__.py:L7-L22](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/__init__.py#L7-L22) 就是全部：定义一个极简的 `OpenPanguV2Config`（只有 `model_type` 与忽略键，其余字段由 `PretrainedConfig` 基类透传保存），注册进 `AutoConfig`；再用"模块:类"字符串把两个架构名（主模型与 MTP 模型，后者 u3-l5 讲）登记到 `ModelRegistry`。注意 `register_models` 函数体内部才 import，与 u2-l1 讲过的 entry point 延迟加载策略一致。

**链路②：超参指纹匹配。**

[components/omni-npu/src/omni_npu/model_config/configs/match_hf_configs.json:L247-L266](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/match_hf_configs.json#L247-L266) 是 openPangu V2 家族的三个指纹条目。三个规格的 `model_type` 相同（`openpangu_v2`），靠 `hidden_size`/`n_routed_experts` 等数值区分——这解释了为什么匹配字段必须**全部相等**才命中。

[components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L244-L276](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L244-L276) 执行匹配：逐条目逐字段比对 `vars(hf_config)`；命中 0 个时回退用 `model_type`（此时最佳实践配置查不到，只会得到默认值并告警）；命中多个时只有 deepseek 家族有硬编码消歧，其余直接 `RuntimeError`——所以给新模型登记指纹时要保证字段组合的唯一性。同文件 L278-312 还会从 `quantization_config` 推导 `quant_type`（如 `w8a8c16`、`bf16`），与 model_name 一起作为后续查配置的复合键。

**链路③：MLA 架构补丁。**

[components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_moe/patch_model_arch_config_convertor.py:L9-L46](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_moe/patch_model_arch_config_convertor.py#L9-L46)：补丁类 `PanguV2MoeModelArchConfigConvertorPatch` 以 `ModelArchConfigConvertorBase` 为目标，用 `_attr_names_to_apply = ["is_deepseek_mla"]` 声明只替换这一个方法（u2-l4 的四要素：注册名 `PanguV2MoeModelArchConfigConvertorPatch`、目标 `ModelArchConfigConvertorBase`、符号 `is_deepseek_mla`、动机——让 vLLM 把 openPangu V2 当作 DeepSeek 风格 MLA 处理）。替换后的实现把 `"openpangu_v2"` 加入白名单，并以 `kv_lora_rank is not None` 作双保险。若没有这个补丁，vLLM 会把模型当成普通 MHA/GQA 架构，KV Cache 形状推导随之出错——模型文件 L1283-1288 的强制 MLA 检查与它前后呼应。

#### 4.2.4 代码实践：手工执行一次指纹匹配

**实践目标**：理解"指纹必须全部字段相等"这一匹配语义，并验证 92B/505B 指纹互不误命中。

**操作步骤**：

1. 运行下面的脚本（**示例代码**，纯标准库，CPU 可跑）：

```python
# 示例代码：模拟 loader.py parse_hf_config 的匹配循环
import json

base = "components/omni-npu/src/omni_npu/model_config/"
table = json.load(open(base + "configs/match_hf_configs.json"))

def match(hf_config: dict):
    hits = []
    for name, fp in table.items():
        if all(hf_config.get(k, "<missing>") == v for k, v in fp.items()):
            hits.append(name)
    return hits

# 模拟一份 92B 的 config.json（截取指纹涉及的字段）
fake_92b = {
    "model_type": "openpangu_v2", "hidden_size": 2560,
    "num_attention_heads": 48, "vocab_size": 151552,
    "intermediate_size": 9216, "n_routed_experts": 256,
    "n_shared_experts": 1, "moe_intermediate_size": 1024,
}
print("92B 命中:", match(fake_92b))
# 故意改错一个字段，模拟"指纹对不上"
bad = dict(fake_92b, n_routed_experts=255)
print("改错后命中:", match(bad))
```

2. 观察 `match(bad)` 的结果——0 个命中时真实代码走 `model_name = hf_config.model_type`（即 `"openpangu_v2"`），而 best_practice_configs.json 里没有这个名字的条目，于是只加载默认配置并打印 warning。
3. 思考：如果两个条目指纹有包含关系会发生什么？（答案见练习 2。）

**需要观察的现象 / 预期结果**：`92B 命中: ['openpangu_v2_92B']`；`改错后命中: []`（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：`match_hf_configs.json` 与 `ModelRegistry` 各自回答什么问题？

**答案**：`ModelRegistry` 回答"这个架构名对应哪个 Python 类"（决定加载哪份模型代码）；`match_hf_configs.json` 回答"这套超参对应哪个模型规格"（决定加载哪份最佳实践算子配置）。前者以 `config.json` 的 `architectures` 为键，后者以超参数值组合为键。

**练习 2**：若某新模型的指纹字段是另一条目的真子集，`parse_hf_config` 会怎样？

**答案**：会同时命中多个条目。除 deepseek_v3/deepseek_v32 有硬编码消歧外，其余情况直接抛 `RuntimeError`（loader.py L268-274）。因此登记新指纹时应选足区分性字段，保证组合唯一。

**练习 3**：补丁 `_is_deepseek_mla` 里为什么除了查 `model_type` 白名单还要检查 `kv_lora_rank is not None`？

**答案**：防御性设计——同 `model_type` 的 checkpoint 可能缺少 MLA 字段（比如某些变体或配置残缺时）。此时把它当 MLA 处理反而会在后续 KV 形状推导中崩溃，返回 False 让其走非 MLA 路径；与模型侧 L1283-1288 "缺 MLA 字段就抛错"形成双层保险。

### 4.3 权重加载与 TP/EP 切分入口

#### 4.3.1 概念说明

vLLM 加载权重不走 PyTorch 的 `load_state_dict`，而是把 checkpoint 展平成 `(名字, 张量)` 流，逐个喂给模型的 `load_weights`。模型要自己回答四个问题：

1. 这个名字**要不要跳过**（如 RoPE 的 `inv_freq` 是可推导的缓存、MTP 层属于另一个模型）？
2. 它是不是**多个 checkpoint 权重拼成一个参数**（stacked/packed，如 `gate_proj`+`up_proj` → `gate_up_proj`）？
3. 它是不是**专家权重**（名字里带专家编号，需要映射到 fused MoE 的堆叠权重上，并按 EP 挑卡）？
4. 都不是，则是普通权重，直接按参数名加载。

而"TP/EP 切分"并不发生在 `load_weights` 里——真正干活的是每个参数自带的 `weight_loader` 回调：`MergedColumnParallelLinear` 的 loader 知道自己是列切、该取哪一段；`NPUSharedFusedMoE` 的 loader 知道本卡该留下哪些专家。`load_weights` 只负责把每个张量**路由**到正确的 `weight_loader`。这就是"切分入口"的确切含义。

#### 4.3.2 核心流程

```text
for (name, loaded_weight) in weights:                 # 主循环 L2368
  ├─ _skip_weight?                                    # L2300：inv_freq / tie 的 lm_head / MTP 层
  │     └─ get_spec_layer_idx_from_weight_name：层号 ≥ num_hidden_layers 即 MTP 层，跳过
  ├─ _try_load_stacked                                # L2307：gate/up→gate_up_proj、q_a+kv_a→fused_qkv_a_proj
  │     └─ param.weight_loader(param, w, shard_id)    #   shard_id 指明拼在第几块
  ├─ _try_load_expert                                 # L2329：experts.N.gate_proj → w13/w2 堆叠权重
  │     └─ weight_loader(..., expert_id, shard_id)    #   expert_id + EP rank 决定落在哪卡哪行
  ├─ maybe_remap_kv_scale_name / _normalize_weight_name   # kv 量化 scale 重映射；k_layernorm→kv_a_layernorm 兼容
  └─ 普通路径：params_dict[name].weight_loader(param, w) # TP 并行层在此自行切片
```

#### 4.3.3 源码精读

**映射表准备。**

[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L2276-L2295](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2276-L2295) 准备三张映射表：`stacked_params_mapping` 基础两条（gate/up 合并）加上 MLA 的 `fused_qkv_a_proj`（`q_a_proj` + `kv_a_proj_with_mqa`，只有 `q_lora_rank` 存在时 checkpoint 才有这种打包形态，见 L2165-2172）；`expert_params_mapping` 由 `SharedFusedMoE.make_expert_params_mapping` 生成，把 `mlp.experts.N.gate_proj/down_proj/up_proj` 这类带专家编号的名字映射到 fused MoE 的 `w13/w2` 堆叠参数，并带上冗余专家数信息。

**三个分支函数与主循环。**

[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L2300-L2305](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2300-L2305) 的 `_skip_weight` 三条规则：跳过 `rotary_emb.inv_freq`（可由 theta 推导）；tie_word_embeddings 时跳过 `lm_head.weight`（直接共享 embedding，见 L2196-2197）；以及通过 [L2421-L2432](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2421-L2432) 的 `get_spec_layer_idx_from_weight_name` 判定 MTP 层——层号 ≥ `num_hidden_layers` 的权重属于 speculative 模型（u3-l5），主模型不加载，留给 MTP 模型自己加载。

[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L2329-L2359](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2329-L2359) 的 `_try_load_expert` 有个返回三态的小设计：`None` 表示"不是专家权重，请走后续分支"；`""` 表示"看起来是专家权重但没加载成功"（例如属于其他卡的专家），直接静默跳过；字符串表示成功。`weight_loader(..., return_success=True)` 的布尔返回让上层能区分这三种情况。

[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L2361-L2418](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2361-L2418) 的普通路径依次做：bias 不存在则跳过、KV scale 名重映射、PP 缺参跳过、`k_layernorm` → `kv_a_layernorm` 新旧 checkpoint 兼容（L2361-2366）、最后取参数的 `weight_loader`（没有就 `default_weight_loader`）落盘。L2410-2416 是一个补账逻辑：MLA 多流拆分把 `q_b_proj` 的加载器同时填充了 `q_b_nope_proj/q_b_pe_proj` 两个合成子参数，这里把它们也记入 `loaded_params`，避免 vLLM 的"未加载权重"检查误报。

**TP/EP 切分在哪里发生。**

- TP：`OpenPanguV2MLP` 的 `gate_up_proj`（列切）与 `down_proj`（行切）在 [L151-L169](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L151-L169) 构造；`weight_loader` 由 vLLM 的并行线性层基类提供，`load_weights` 只在普通路径/stacked 路径调用它。
- EP：专家权重经 `expert_params_mapping` 进入 `NPUSharedFusedMoE` 的 loader；本卡持有哪个专家区间由 [L295-L307](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L295-L307) 的 `physical_expert_start/end` 决定。loader 的具体实现在 u3-l3 精读。

#### 4.3.4 代码实践：给一个权重名"导航"

**实践目标**：对 4 个代表性权重名，写出它们在 `load_weights` 中走的分支与最终落点，检验你对路由逻辑的掌握。

**操作步骤**：

1. 阅读主循环 L2368-2418 与三个分支函数，填写下表（先自己写，再对答案）。
2. 若容器内有真实 checkpoint，可用 `python -c "from safetensors import safe_open; ..."` 打印若干键名对照（**待本地验证**）。

| 权重名（checkpoint 侧） | 走哪个分支 | 落到哪个参数 | 谁负责切分 |
| --- | --- | --- | --- |
| `model.layers.0.mlp.gate_proj.weight` | `_try_load_stacked` | `model.layers.0.mlp.gate_up_proj.weight` | `MergedColumnParallelLinear.weight_loader`（TP 列切，shard_id=0） |
| `model.layers.3.mlp.experts.17.up_proj.weight` | `_try_load_expert` | `…mlp.experts.w13_weight`（堆叠） | `NPUSharedFusedMoE` 的 loader（EP 按 expert_id 挑卡 + TP 列切） |
| `model.layers.2.self_attn.k_layernorm.weight` | 普通路径（先 `_normalize_weight_name`） | `…self_attn.kv_a_layernorm.weight` | `default_weight_loader` |
| `model.layers.64.mlp.gate.weight`（设 `num_hidden_layers=64`） | `_skip_weight` 跳过 | 无（属 MTP 层，u3-l5 的模型加载） | — |

**需要观察的现象 / 预期结果**：第 1、2 行体现了"同是 FFN 权重，Dense 层走 stacked、MoE 层走 expert"的分野——判断依据正是 4.1.3 中 L1338-1357 的 Dense/MoE 二选一在参数名上的投影（`mlp.gate_proj` vs `mlp.experts.N.gate_proj`）。第 4 行需满足 `num_nextn_predict_layers > 0`，否则 `get_spec_layer_idx_from_weight_name` 返回 None、不跳过（此时该名字会因找不到参数而打印 `Skip loading`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_skip_weight` 要在 stacked/expert 分支**之前**执行？

**答案**：MTP 层的权重名（`model.layers.64.…`）与普通层结构完全相同（同样含 `gate_proj`/`experts.N.…`），若不先跳过，会先命中 stacked/expert 映射、错误地覆盖主模型参数或污染 `loaded_params` 集合。跳过检查是最廉价的短路，放在最前。

**练习 2**：`_try_load_expert` 返回 `""` 与 `None` 分别意味着什么？为什么必须区分？

**答案**：`None` = 名字里没有专家关键字，不是专家权重，主循环要继续走普通路径；`""` = 名字像专家权重但映射后参数不存在（典型：该专家不在本卡的 `physical_expert_start/end` 区间），主循环必须**跳过且不再走普通路径**，否则会拿专家张量去撞 `params_dict` 里的无关参数名。

**练习 3**：若 checkpoint 保存的是 `fused_qkv_a_proj`（已打包）而配置里 `q_lora_rank=None`，会发生什么？

**答案**：`packed_modules_mapping` 只在 `fuse_qkv_a_proj=True`（L2165-2167）时注册 `fused_qkv_a_proj` 条目，`stacked_params_mapping` 中对应映射也不会命中——权重名经普通路径找不到参数，最终打印 `Skip loading …` 并被 vLLM 的未加载检查发现（或静默缺参报错于前向）。这就是"模型结构与 checkpoint 打包形态必须互相声明"的体现。

## 5. 综合实践

**任务：为 openPangu-2.0-Flash（92B）产出一份《模型结构导览报告》。**

把你在本讲学到的三件事串起来，输出一份 Markdown 报告，包含：

1. **超参落点表**：以 `match_hf_configs.json` 的 `openpangu_v2_92B` 指纹为"config.json"，完成 4.1.4 的落点表（可用示例脚本 `trace_config.py` 取数）。
2. **层类型地图**：92B 的 `num_hidden_layers` 与 `first_k_dense_replace` 需从真实权重 `config.json` 获取（仓库指纹表未收录这两个字段，**待确认**）；拿到后画出"第 0 层～第 N-1 层，哪些是 Dense、哪些是 MoE"的条带图，并标注每层的实例化顺序（按 4.1.4 的 9 项模板，注意 mHC 与 block_post_layernorm 是否存在）。
3. **权重导航表**：从权重文件（或依据结构推导）挑 6 个名字——embedding、一个 Dense 层 gate、一个 MoE 层 gate、一个专家权重、一个 `kv_a_layernorm`（或 `k_layernorm`）、一个 MTP 层权重——填写 4.3.4 的三列分支表。
4. **验证**：若你已按 u1-l4 部署过服务，回到 `server_0.log` 中找到加载阶段的权重统计日志，与你推导的层类型地图互相印证（例如 MoE 层数 = `num_hidden_layers - first_k_dense_replace`，正是 L2207 的 `num_moe_layers` 计算式）。全程无 NPU 时第 4 步标注"待本地验证"。

这份报告也是后续三讲的"地图"：u3-l2 沿 `self_attn` 深入，u3-l3 沿 `mlp.experts` 深入，u3-l4 沿 `attn_mhc_module/mlp_mhc_module` 深入。

## 6. 本讲小结

- `pangu_v2_moe.py` 是五层积木：`MLP`（稠密 FFN/共享专家）→ `MOE`（路由 + 共享 + 融合路由专家）→ `DecoderLayer`（注意力 + FFN + 三明治四 norm + 可选 mHC）→ `Model`（N 层堆叠 + PP 切分 + `_tail_refs` 尾部引用）→ `ForCausalLM`（lm_head/logits/MoE 统计/权重加载）。
- Dense 与 MoE 层以 `first_k_dense_replace` 为界在 `DecoderLayer.__init__` 二选一实例化；共享专家复用 `OpenPanguV2MLP` 但 `disable_tp=True`（每卡完整复制）。
- 模型识别是三条独立链路：`AutoConfig`/`ModelRegistry` 注册（决定用哪个类）、`match_hf_configs.json` 超参指纹（决定加载哪份最佳实践配置）、`is_deepseek_mla` 补丁（让 vLLM 把 `openpangu_v2` 当 DeepSeek 风格 MLA）。
- `load_weights` 是四分支路由器（skip → stacked → expert → 普通），TP/EP 切分由各参数自带的 `weight_loader` 完成；专家按 `physical_expert_start/end` 区间落在 EP 各卡。
- 子模块声明顺序（mHC 在 attention 之前）与 checkpoint 参数名顺序直接相关；MTP 层权重（层号 ≥ `num_hidden_layers`）在主模型中被显式跳过。

## 7. 下一步学习建议

本讲只搭了骨架，三个子模块还是黑盒，建议按依赖顺序继续：

1. **u3-l2（Pangu 稀疏注意力）**：进入 `self_attn` 即 `NPUPanguSparseAttention`——Indexer 如何为每层选 topk token、调用哪些 torch_npu 自定义算子。
2. **u3-l3（MoE 层实现与专家并行）**：进入 `mlp.experts` 即 `NPUSharedFusedMoE`，弄清 `w13/w2` 堆叠权重的 loader 如何结合 `expert_id` 与 EP rank 切分，以及 `_forward_allgather/all2allv` 各通信路径的算子序列。
3. **u3-l4（自定义层）**：弄懂 `NPUmHC` 的残差流与本讲 forward 里反复出现的 `h_post/h_res/sk_event`，以及 `mhc_sandwich_norm_post_pre` 的"三明治"norm 语义。
4. **u3-l5（MTP 投机解码）**：本讲跳过的 `model.layers.64+` 权重去了哪里——`OpenPanguV2MTPModel` 与拒绝采样如何协作。
