# 以 Llama 为例的模型重写实现

## 1. 本讲目标

上一讲（u3-l3）讲清了 LMDeploy PyTorch 后端的「Patch 重写机制」：它在实例化时直接把 HuggingFace 原模型类换成同名不同实现的优化类，主链路是 `build_patched_model → build_model_from_hf_config → _get_model_class`，靠 `MODULE_MAP` 这张「arch 名 → 实现类 qualname 字符串」的纯数据注册表驱动。

本讲要回答的问题是：**当 arch 名命中 `MODULE_MAP` 后，被替换进来的那个类究竟长什么样？它如何在不重新发明轮子的前提下，把 attention / MLP / embedding 重写成支持 Paged Attention、张量并行、量化和 CUDA Graph 的版本？**

我们以最经典、最简洁的 Llama 为样本。学完后你应当能够：

1. 看懂 `lmdeploy/pytorch/models/llama.py` 里 `LlamaForCausalLM` 的整体骨架与生命周期方法。
2. 说清 `LlamaAttention` 与 `LlamaMLP` 两个子模块如何「拼装」而非「手写」——即如何复用 `nn.attention` / `nn.linear` / `nn.norm` / `nn.rotary_embedding` 等优化积木。
3. 理解「打包权重映射」（`packed_modules_mapping` / `stacked_params_mapping`）如何让重写后的模型与 HF 原始权重名对接。
4. 掌握「整模型级重写」的套路，为后续阅读 Qwen、DeepSeek 等更复杂的重写文件，乃至自己接入新模型（u10-l1）打下基础。

> 命名提示：本讲规划大纲里把注意力重写类记作 `LlamaAttentionWrapper`，但源码中的真实类名是 [`LlamaAttention`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L24)。本讲一律以源码真实命名为准。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**为什么要重写？** 原生 HF 的 `LlamaForCausalLM` 是为「训练 / 单条推理」设计的，它有三个推理引擎用不上的硬伤：① attention 每步重新计算 Q·Kᵀ，没有「分块 KV 缓存」（即 Paged Attention 思想），无法做持续批处理；② 线性层是普通 `nn.Linear`，不支持张量并行（TP）切分、也不支持 AWQ/W8A8/FP8 等权重量化；③ 整图结构不支持 CUDA Graph 捕获。所以 LMDeploy 选择「换类」而非「改实例」。

**重写的两条路径（承接 u3-l3）。** 一是**整模型级**：`_get_model_class` 按 arch 名精确命中 `MODULE_MAP`，直接返回本讲的 `LlamaForCausalLM`（[patch.py:165-196](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L165-L196)）；二是**子模块级**：`get_rewrite_cls` 走 full name → class name 降级匹配（[patch.py:96-103](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L96-L103)）。Llama 走的是第一条——整模型被替换，其内部的 attention/MLP 子模块也由本文件直接定义，不再走子模块查表。

**「拼装」而非「手写」。** Llama 的重写文件几乎没有手写算子：attention 的 Paged Attention kernel 藏在 `nn.Attention` 背后的 `backends`，线性层藏在 `nn.linear.build_*` 背后的多套量化实现，归一化藏在 `nn.RMSNorm` 背后。重写文件只负责「把这些积木按 Llama 的拓扑连起来」。这是 LMDeploy 模型重写的核心哲学，也是它能让 40+ 模型共享同一套 nn 模块的关键。

> 关键术语回顾：arch 名（`config.json` 的 `architectures[0]`）、qualname（全限定类名）、Prefill/Decode、GQA（`num_key_value_heads < num_attention_heads`）、tie_word_embeddings（词嵌入与输出头共享权重）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它讲什么 |
| --- | --- | --- |
| `lmdeploy/pytorch/models/llama.py` | Llama 整模型重写实现 | 全篇主角，4 个模块都围绕它 |
| `lmdeploy/pytorch/nn/attention.py` | 通用 `Attention` 模块（Paged Attention 包装器） | 讲清 `LlamaAttention` 复用的积木 |
| `lmdeploy/pytorch/nn/linear/__init__.py` | 线性层工厂：`build_qkv_proj` / `build_o_proj` / `build_gateup_linear` / `build_down_linear` | 讲清线性层如何按量化与 TP 自动选实现 |
| `lmdeploy/pytorch/nn/norm.py` | `RMSNorm`（融合 residual add） | 讲清 decoder layer 的归一化与残差融合 |
| `lmdeploy/pytorch/nn/activation.py` | `SiluAndMul` 激活 | 讲清 MLP 的门控激活 |
| `lmdeploy/pytorch/models/module_map.py` | arch 名注册表 | 确认 `LlamaForCausalLM` 的注册位置 |
| `lmdeploy/pytorch/models/patch.py` | 模型构建与替换主链路 | 衔接 u3-l3，定位「换类」入口 |
| `lmdeploy/pytorch/weight_loader/model_weight_loader.py` | `load_weight` 权重写入工具 | 讲清 `load_weights` 的最后一公里 |

## 4. 核心概念与源码讲解

### 4.1 LlamaForCausalLM：整模型重写的骨架与权重对接

#### 4.1.1 概念说明

`LlamaForCausalLM` 是被 `MODULE_MAP` 注册、被 `_get_model_class` 选中的那个「替换类」。它对应 HF 原版 `transformers.models.llama.modeling_llama.LlamaForCausalLM`，但内部从零搭起：持有 `LlamaModel`（主干，含 embedding + N 层 decoder layer + final norm + rotary）、`lm_head`（输出投影）、`config` 与 `ctx_mgr`（步骤上下文管理器）。它还混入了 `CudaGraphMixin`，让整图可被 CUDA Graph 捕获。

它对外暴露的不是普通 `forward → logits`，而是一组「引擎友好」的生命周期方法：`forward` 只返回 hidden_states、`get_logits` 单独算 logits、`prepare_inputs_for_generation` 负责把引擎送来的 `StepContext` 翻译成 forward 入参、`load_weights` 负责把 HF 权重名映射进打包后的参数。

#### 4.1.2 核心流程

一次 decode step 在 `LlamaForCausalLM` 上的执行流程：

1. 引擎调用 `prepare_inputs_for_generation(past_key_values, context=step_ctx)`，从 `StepContext` 取出 `input_ids` / `position_ids` / `attn_metadata`，并注入多模态 vision embedding（若有）。
2. 引擎调用 `forward(**inputs)` → 转发给 `self.model(...)` 得到 `hidden_states`（不在此算 logits）。
3. 引擎拿到 hidden_states 后，按需调用 `get_logits(hidden_states)` 得到采样 logits。
4. 权重加载阶段（一次性）：`load_weights(weights)` 按 `stacked_params_mapping` 把 HF 的 `q_proj/k_proj/v_proj` 合并进 `qkv_proj`、`gate_proj/up_proj` 合并进 `gate_up_proj`。

#### 4.1.3 源码精读

先看注册：`MODULE_MAP` 把 arch 名 `LlamaForCausalLM` 指向本文件的同名类。

[lmdeploy/pytorch/models/module_map.py:24-26](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py#L24-L26) — 把 arch 名映射到本文件的 qualname 字符串。注意值是字符串（延迟导入），不是类本身，这是 u3-l3 讲过的设计。

再看骨架。`LlamaForCausalLM` 继承自 `nn.Module` 与 `CudaGraphMixin`：

[lmdeploy/pytorch/models/llama.py:289-320](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L289-L320) — 整模型构造：建 `LlamaModel` 主干与 `lm_head`。`lm_head` 用 `build_rowwise_linear` 构建，意味着它按行切分（TP 时沿输入维度切，输出维度完整，最后 all-reduce）。`build_rowwise_linear` 见 [lmdeploy/pytorch/nn/linear/__init__.py:147-181](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/__init__.py#L147-L181)。

forward 与 logits 解耦：

[lmdeploy/pytorch/models/llama.py:322-349](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L322-L349) — `forward` 只返回 hidden_states，`get_logits` 才把 hidden_states 转回 `self.dtype` 后过 `lm_head`。这种拆分让投机解码（u9-l2）等场景能在 draft / verify 间复用 hidden_states，不必每步都算到 logits。

词嵌入与 tie_word_embeddings：

[lmdeploy/pytorch/models/llama.py:341-353](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L341-L353) — 当 `config.tie_word_embeddings=True` 时，`update_weights` 把 `lm_head.weight` 直接绑到 `embed_tokens.weight`，省一份显存。`get_input_embeddings` 透传给主干，供多模态路径注入 vision embedding 使用。

最关键的一段——**打包权重映射**。重写类把 `q_proj/k_proj/v_proj` 合并成单个 `qkv_proj`、把 `gate_proj/up_proj` 合并成单个 `gate_up_proj`，那么 HF 权重里分散的四个名字如何灌进去？靠两张表：

[lmdeploy/pytorch/models/llama.py:292-302](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L292-L302) — 类属性 `packed_modules_mapping` 声明「逻辑打包名 → 原始分片名列表」，这是给权重加载器与 LoRA 注入用的「打包契约」。

[lmdeploy/pytorch/models/llama.py:393-423](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L393-L423) — `load_weights` 里的 `stacked_params_mapping` 把 HF 的 `*.q_proj` / `*.k_proj` / `*.v_proj` 重写到 `*.qkv_proj` 并带上 shard_id（`'q'/'k'/'v'`），再交给 `load_weight(param, loaded_weight, shard_id=...)`。注释里写明这段改编自 vLLM。

[lmdeploy/pytorch/weight_loader/model_weight_loader.py:19-25](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L19-L25) — `load_weight` 的派发逻辑：若参数自带 `weight_loader`（量化线性层 / TP 切分层会挂这个回调），就调它做分片写入；否则走 `default_weight_loader` 做形状校验后 `copy_`。这就是「打包 + TP 切分」的最后一公里。

`prepare_inputs_for_generation` 是引擎与模型的「协议层」：

[lmdeploy/pytorch/models/llama.py:364-391](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L364-L391) — 从 `StepContext` 取 `input_ids/position_ids/attn_metadata`，并在检测到 `vision_embeddings` 时把它们按 `input_embedding_indexing` 写回 `inputs_embeds`。Llama 本身是多模态「兼容位」，这段逻辑让纯文本 Llama 与 VLM 共用同一套引擎调用约定（u9-l1 会展开）。

#### 4.1.4 代码实践

**实践目标**：对照 HF 原版与 lmdeploy 版的 `LlamaForCausalLM`，列出被替换的关键方法与新增的「引擎协议」方法。

**操作步骤**：

1. 打开 HF 的 [modeling_llama.py](https://github.com/huggingface/transformers/blob/main/src/transformers/models/llama/modeling_llama.py)（或本地 `transformers` 安装目录下的同名文件），找到原版 `LlamaForCausalLM`。
2. 对照本讲 [llama.py:289-423](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L289-L423)，填写下表（示例答案见 4.1.5）：

   | 维度 | HF 原版 | lmdeploy 重写版 |
   | ---- | ---- | ---- |
   | forward 返回 | `CausalLMOutputWithPast`（含 logits） | ? |
   | attention 类 | `LlamaAttention`（含 `o_proj` 等） | ? |
   | MLP 类 | `LlamaMLP`（`gate_proj`/`up_proj`/`down_proj` 三个独立线性层） | ? |
   | 权重加载 | 自动 `from_pretrained` | ? |
   | TP / 量化 | 无 | ? |

3. 在 [llama.py:396-403](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L396-L403) 的 `stacked_params_mapping` 里，把每条 `(param_name, shard_name, shard_id)` 三元组抄出来，标注它把「哪几个 HF 分片」合并到了「哪个打包参数」。

**需要观察的现象 / 预期结果**：

- 重写版 `forward` 只返回 hidden_states，`get_logits` 被单独拆出。
- 重写版多出 `prepare_inputs_for_generation` / `load_weights` / `update_weights` / `get_outputs_cudagraph` 等「引擎协议」方法——这些在 HF 原版里都不存在。
- MLP 的三个独立线性层在重写版里被合并为 `gate_up_proj`（融合）+ `down_proj`。

> 若想运行验证（**待本地验证**，需 GPU 与已编译的 lmdeploy）：可写一段脚本，用 `pipeline(...)` 加载一个小 Llama 模型后，打印 `type(pipe.async_engine.engine)...model` 与 `pipe.async_engine.engine.model.packed_modules_mapping`，确认替换确实发生、且打包契约就位。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_logits` 要先把 `hidden_states.to(dtype=self.dtype)`，而不是直接 `self.lm_head(hidden_states)`？

> **参考答案**：主干 forward 内部（尤其 RMSNorm）可能在 fp32 下计算以保证数值精度，导致 hidden_states 的 dtype 与模型权重 dtype（如 bf16）不一致。先转回 `self.dtype` 再过 `lm_head`，既避免 dtype 不匹配报错，又让 matmul 走高效的 bf16/tf32 路径。

**练习 2**：`stacked_params_mapping` 里 `gate_proj` / `up_proj` 的 `shard_id` 分别是 `0` 和 `1`，而 `q_proj` / `k_proj` / `v_proj` 却是字符串 `'q'` / `'k'` / `'v'`。为什么类型不一致？

> **参考答案**：`shard_id` 只是交给下游 `load_weight(param, loaded_weight, shard_id=...)` 的「分片定位符」，类型由打包线性层的 `weight_loader` 约定。MLP 的融合层 `MergedBaseLinear` 用整数下标在 `all_out_features` 列表里定位分片；QKV 融合层 `QKVBaseLinear` 用 `'q'/'k'/'v'` 标签定位。两套标签各自匹配对应线性层 `weight_loader` 的解析逻辑（详见 u5-l2）。

### 4.2 LlamaAttention：注意力重写与 nn 积木复用

#### 4.2.1 概念说明

`LlamaAttention` 是本文件内自定义的子模块（不是子模块级查表替换的产物）。它的职责是：把 hidden_states 投影成 Q/K/V、施加旋转位置编码（RoPE）、跑 Paged Attention、再用 `o_proj` 投影回 hidden 维度。它本身**不含任何 attention 数学实现**，真正的 attention kernel 由 `nn.Attention` 包装器在运行时从 `backends` 取。

注意 GQA 场景：现代 Llama 的 `num_key_value_heads` 小于 `num_attention_heads`，KV 头会被多个 Q 头共享。构造时需把 Q 头数、KV 头数、head_dim 都告诉 `build_qkv_proj`，以便它正确打包 QKV 并做 TP 切分。

#### 4.2.2 核心流程

`LlamaAttention.forward` 的四步：

1. **QKV 投影**：`qkv_proj(hidden_states)` 一次性算出打包的 QKV；再 `split_qkv` 拆成三份。
2. **旋转位置编码**：`apply_rotary_pos_emb(q, k, cos, sin)` 只作用于 Q 和 K（V 不旋转）。
3. **注意力**：`attn_fwd(q, k, v, k_cache, v_cache, attn_metadata, ...)`——这里同时完成「写入分块 KV 缓存」与「计算 attention」两件事，是 Paged Attention 的核心。
4. **输出投影**：`reshape` 还原成 `(batch, seq, num_heads*head_dim)`，再 `o_proj` 回 hidden 维度。

#### 4.2.3 源码精读

构造——四个积木的拼装：

[lmdeploy/pytorch/models/llama.py:24-67](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L24-L67) — `LlamaAttention.__init__`：用 `build_qkv_proj` 建打包 QKV 投影、`ApplyRotaryEmb()` 建旋转编码、`Attention(...)` 建 attention 包装器、`build_o_proj` 建输出投影。`head_dim` 用 `getattr(config, 'head_dim', hidden_size // num_heads)` 兜底（部分模型 head_dim 不等于 hidden_size/num_heads，如 MLA 变体）；`num_replicate_kv_heads` 用于在 TP 下复制 KV 头以保证可整除。

这三个线性层工厂的「自动选型」逻辑在 `nn/linear/__init__.py`：

[lmdeploy/pytorch/nn/linear/__init__.py:258-331](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/__init__.py#L258-L331) — `build_qkv_proj`：先查 `quant_config.get_quant_method(prefix, module_kind='linear')` 得到 `quant_method`，再据它返回 `QKVBaseLinear` / `QKVAwqLinear` / `QKVW8A8Linear` / `QKVBlockedF8Linear`。也就是说，同一个 `LlamaAttention` 在 FP16、AWQ、W8A8、FP8 权重下会自动实例化出不同实现——重写文件完全不需要 `if quant` 分支。

[lmdeploy/pytorch/nn/linear/__init__.py:334-363](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/__init__.py#L334-L363) — `build_o_proj`：`o_proj` 是行并行（row-parallel），沿输入维度切分，TP 下需要 all-reduce，所以委托给 `build_rowwise_linear`。

前向——四步拼装：

[lmdeploy/pytorch/models/llama.py:69-109](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L69-L109) — `forward`：第 78-81 行算并拆 QKV；第 84-91 行施加 RoPE；第 94-104 行跑 attention；第 108 行输出投影。注意第 101-102 行：当 `len(past_key_value) == 4` 时，第 3/4 项被当作 KV cache 的 scale/zero（FP8 KV 量化场景），否则传 `None`。第 103 行 `inplace=True` 表示允许原地写 KV 缓存以省显存。

被复用的 `nn.Attention`：

[lmdeploy/pytorch/nn/attention.py:21-76](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/attention.py#L21-L76) — `Attention.__init__`：先经 `_update_num_heads` 按 TP 世界大小切分 Q/KV 头数（第 46 行），再从 `get_backend()` 取 `OpType.PagedAttention` 的实现构建器并 `.build(...)` 出 `self.impl`。第 75-76 行注册了 `k_scale`/`v_scale` 两个 buffer，给 FP8 KV cache 当固定 scale。

[lmdeploy/pytorch/nn/attention.py:93-136](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/attention.py#L93-L136) — `Attention.forward`：先 `_lazy_init`（alibi 模型延迟建 slope 表），再读 `attn_metadata.quant_policy` 决定是否把固定 scale 塞给 kernel，最后转给 `self.impl.forward`。**真正的 attention 数学（FlashAttention / triton / flash_mla）都藏在 `self.impl` 里，由 `backends` 按设备与配置分发**（u5-l4 专题展开）。

旋转编码只作用于 Q/K：

[lmdeploy/pytorch/models/llama.py:50](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L50) 与 [lmdeploy/pytorch/models/llama.py:84-91](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L84-L91) — `ApplyRotaryEmb` 实例化无参，cos/sin 由主干的 `rotary_emb` 在 `LlamaModel.forward` 里算好后再传入（见 [llama.py:256-258](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L256-L258)）。RoPE 的原理简介：

旋转位置编码把位置 \(m\) 编码为对 query/key 每一对相邻维度的二维旋转：

\[
\begin{pmatrix} x_{2i} \\ x_{2i+1} \end{pmatrix}
\otimes
\begin{pmatrix} \cos(m\theta_i) \\ \cos(m\theta_i) \end{pmatrix}
+
\begin{pmatrix} -x_{2i+1} \\ x_{2i} \end{pmatrix}
\otimes
\begin{pmatrix} \sin(m\theta_i) \\ \sin(m\theta_i) \end{pmatrix},\quad
\theta_i = 10000^{-2i/d}
\]

它让内积只依赖 **相对位置** \(m-n\)，从而天然适合外推与长上下文。`build_rotary_embedding_from_config` 会按 `rope_scaling` 自动选用 Default / Linear / DynamicNTK / Yarn / Llama3 等变体（见 [rotary_embedding.py:226](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/rotary_embedding.py#L226)），`LlamaModel` 只需一句 `build_rotary_embedding_from_config(config)` 就拿到了正确的实现。

#### 4.2.4 代码实践

**实践目标**：定位 `LlamaAttention` 的前向实现，并标注它复用了哪些 nn 积木、各自藏在哪个文件。

**操作步骤**：

1. 读 [llama.py:69-109](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L69-L109) 的 `forward`，对每一行前向计算，在一张表里填出「该行调用的属性 → 该属性的来源（构造时哪一行建的）→ 背后真正的实现文件」。
2. 跳转到 [attention.py:93-136](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/attention.py#L93-L136)，找到 `self.impl.forward` 的调用点，回答：「Llama 重写文件里有没有出现任何 attention 数学公式（softmax、Q·Kᵀ）？真正的数学在哪一层？」
3. 选做：在 [linear/__init__.py:258-269](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/__init__.py#L258-L269) 的 `build_qkv_proj` 签名里，数一数 `LlamaAttention` 构造它时传了哪些参数（对照 [llama.py:36-47](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L36-L47)），并解释 `is_tp=True` 与 `num_replicate_kv_heads` 的作用。

**需要观察的现象 / 预期结果**：

- `LlamaAttention.forward` 全文找不到一个 softmax / matmul 公式——所有数学都在 `self.attn_fwd.impl` 与各 `*proj` 实现里。
- 复用的 nn 积木有四个：`build_qkv_proj`、`ApplyRotaryEmb`、`Attention`、`build_o_proj`，背后分别对应 `nn/linear`、`nn/rotary_embedding`、`nn/attention`（→ `backends`）、`nn/linear`。
- `build_qkv_proj` 收到 `num_q_heads`、`num_kv_heads`、`head_size`、`bias`、`quant_config`、`num_replicate_kv_heads`、`is_tp` 等——这些参数足以让它既做 GQA 打包、又做 TP 切分、又按量化策略选实现。

#### 4.2.5 小练习与答案

**练习 1**：`LlamaAttention` 里没有 `k_proj` / `v_proj` / `q_proj` 三个独立属性，只有一个 `qkv_proj`。这对显存与访存有什么好处？

> **参考答案**：三个投影的输入都是同一个 `hidden_states`，融合成单个 `qkv_proj` 后，hidden_states 只需从显存读一次，即可同时算出 Q/K/V（三次访存合并为一次），显著降低访存带宽压力；同时权重在内存中连续排布，便于后续量化 kernel（AWQ/W8A8/FP8）做融合 GEMM。

**练习 2**：第 101-102 行 `k_scales_zeros=None if len(past_key_value) == 2 else past_key_value[2]` 这个表达式在表达什么？

> **参考答案**：`past_key_value` 的长度在运行时会变化——长度为 2 表示普通 KV cache（只有 key/value 两块）；长度为 4 表示带量化元数据的 KV cache（多出 key 的 scale/zero、value 的 scale/zero）。该三元式按长度自适应地传或是不传 scale/zero 给 attention kernel，让同一段代码兼容 KV 量化开/关两种状态。

### 4.3 LlamaMLP：门控前馈与融合 gate-up

#### 4.3.1 概念说明

Llama 的 MLP 是门控前馈网络（SwiGLU）：对输入 `x`，先同时算「门控」`gate` 与「上投影」`up`，再用 `SiLU(gate) * up` 做逐元素门控，最后 `down` 投影回 hidden 维度。公式：

\[
\text{MLP}(x) = W_{\text{down}} \cdot \big(\text{SiLU}(W_{\text{gate}} x) \odot (W_{\text{up}} x)\big)
\]

其中 \(\text{SiLU}(x) = x \cdot \sigma(x)\)。

与 attention 一样，重写版把 `gate_proj` 与 `up_proj` 融合成单个 `gate_up_proj`——因为两者输入相同、结构相同（都是 hidden → intermediate 的列并行投影），融合后 hidden 只读一次。

#### 4.3.2 核心流程

`LlamaMLP.forward` 三步：

1. `gate_up_proj(x)` 一次性算出 `[gate, up]` 拼接结果（最后一维是 `2 * intermediate_size`）。
2. `act_fn(gate_up)` = `SiluAndMul` 对最后一维对半切，前半过 SiLU 再乘以后半。
3. `down_proj(act)` 投影回 hidden 维度。

#### 4.3.3 源码精读

构造：

[lmdeploy/pytorch/models/llama.py:112-140](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L112-L140) — `LlamaMLP.__init__`：`build_gateup_linear(config.hidden_size, [intermediate_size, intermediate_size], ...)` 建融合的 gate_up（注意第二个参数是「两个输出维度」的列表，对应 gate 与 up 各一份）；`SiluAndMul(inplace=True)` 建门控激活；`build_down_linear` 建行并行的 down。`mlp_bias` 用 `getattr(config, 'mlp_bias', False)` 兜底——老 Llama 无 bias，部分新模型有。

`build_gateup_linear` 走的是融合列并行路径：

[lmdeploy/pytorch/nn/linear/__init__.py:366-397](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/__init__.py#L366-L397) — `build_gateup_linear` 委托给 `build_merged_colwise_linear`，按 `quant_method` 返回 `MergedBaseLinear` / `MergedAwqLinear` / `MergedW8A8Linear` / `MergedBlockedF8Linear`。`layer_type='mlp'` 让它走 MLP 的 TP 拓扑（与 attention 的 TP 拓扑可分离，对应 u3-l2 的 `TPMode.DP_TP`）。

前向：

[lmdeploy/pytorch/models/llama.py:142-146](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L142-L146) — 三行就是三步，极其简洁。`inplace=True` 让 `SiluAndMul` 尽量原地写，省一次显存分配。

被复用的激活：

[lmdeploy/pytorch/nn/activation.py:7-18](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/activation.py#L7-L18) — `SiluAndMul` 同样是「薄包装 + backend 实现」：构造时从 `get_backend()` 取 `OpType.SiluAndMul` 的实现并 `.build(inplace)`，前向直接转给 `self.impl.forward(x)`。这和 `Attention` / `RMSNorm` 是完全一致的设计模式。

> 顺带看一眼 decoder layer 的归一化与残差融合。`LlamaDecoderLayer.forward` 里 `self.input_layernorm(hidden_states, residual)` 这一行同时做「residual add + RMSNorm」：

[lmdeploy/pytorch/models/llama.py:182-210](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L182-L210) — 首层（`residual is None`）只做 norm；后续层调 `input_layernorm(x, residual)` 返回 `(normed, new_residual)`，把残差加法融合进归一化 kernel，减少一次访存。

[lmdeploy/pytorch/nn/norm.py:13-74](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/norm.py#L13-L74) — `RMSNorm.forward(x, residual)` 把 `residual` 透传给 `self.impl.forward(x, self.weight, residual)`；底层 kernel 在残差非空时做融合。RMSNorm 公式为：

\[
\text{RMSNorm}(x) = \frac{x}{\sqrt{\frac{1}{d}\sum_{i=1}^{d} x_i^2 + \varepsilon}} \cdot \gamma
\]

相比 LayerNorm 省去了均值中心化，计算更省、且同样有效，是 Llama 系的标配。

#### 4.3.4 代码实践

**实践目标**：理解 MLP 三步前向如何对应到 nn 积木，并对照 HF 原版确认「融合」带来的结构差异。

**操作步骤**：

1. 读 [llama.py:142-146](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L142-L146)，把三行代码对应到公式里的 \(W_{\text{gate}}\)、\(W_{\text{up}}\)、\(\text{SiLU}\)、\(\odot\)、\(W_{\text{down}}\)。
2. 对照 HF 原版 `LlamaMLP`：原版有三个独立线性层 `gate_proj` / `up_proj` / `down_proj`，前向是 `down_proj(act_fn(gate_proj(x)) * up_proj(x))`；重写版把它压缩成 `down_proj(act_fn(gate_up_proj(x)))`。请回答：融合后 `gate_up_proj(x)` 输出张量最后一维的形状是什么？
3. 在 [activation.py:7-18](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/activation.py#L7-L18) 里确认 `SiluAndMul` 是「薄包装」，真正的 fused kernel 在 `self.impl`（即 `backends` 提供的实现）。

**需要观察的现象 / 预期结果**：

- `gate_up_proj(x)` 最后一维 = `2 * config.intermediate_size`（gate 与 up 各占一半），`SiluAndMul` 对半切后还原回 `intermediate_size`。
- HF 原版 `self.gate_proj` / `self.up_proj` / `self.down_proj` 三个属性；重写版只有 `self.gate_up_proj` / `self.down_proj` 两个。

> **待本地验证**（可选运行）：若已安装 lmdeploy，可打印 `model.model.layers[0].mlp` 的子模块名，应看到 `gate_up_proj` 与 `down_proj`，而不是三个独立投影。

#### 4.3.5 小练习与答案

**练习 1**：`gate_up_proj` 与 `down_proj` 分别是列并行（col-parallel）还是行并行（row-parallel）？为什么？

> **参考答案**：`gate_up_proj` 是列并行（`build_gateup_linear` → `build_merged_colwise_linear`，`colwise=True`），沿输出维度切分，每个 TP rank 算自己那份 intermediate，无需通信；`down_proj` 是行并行（`build_down_linear` → `build_rowwise_linear`，`colwise=False`），沿输入维度切分，每个 rank 算部分和，最后 all-reduce 汇总。这是标准 MLP 的 TP 切分套路：先列后行，只在两者之间做一次 all-reduce。

**练习 2**：如果某模型把激活从 SiLU 换成 GELU（如某些 vision encoder），重写文件要改什么？

> **参考答案**：把 `self.act_fn = SiluAndMul(...)` 换成 `self.act_fn = GeluAndMul(...)`（两者同在 [activation.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/activation.py#L21-L32)），其余拓扑不变。这正是「拼装」哲学的好处——换激活只是换一块积木。

## 5. 综合实践

**任务**：把本讲三个模块串起来，为「一个假设的简化 Llama 变体」画出重写类结构图，并预判它接入 lmdeploy 需要改哪些地方。

设定：假设有一个 `MyLlamaForCausalLM`，与标准 Llama 唯一的不同是——它的 MLP 用 GELU 而非 SiLU，且 attention 额外带一个 `alibi` 偏置（无 RoPE）。

请完成：

1. **结构图**：参考本讲的 4.1–4.3，画出 `MyLlamaForCausalLM` → `LlamaModel`（`embed_tokens` / `layers[N]` / `norm`）→ `LlamaDecoderLayer`（`self_attn` / `mlp` / 两个 `RMSNorm`）的树状结构，在每个节点旁标注「复用了哪个 nn 积木 / 哪个 build 工厂」。
2. **改动清单**：列出为支持它，需要在哪些文件做哪些最小改动。提示：
   - `models/my_llama.py`：复制 `llama.py`，把 `SiluAndMul` 换成 `GeluAndMul`；在 `LlamaAttention` 构造 `Attention` 时传 `alibi=True`（参考 [attention.py:31](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/attention.py#L31)）；去掉 `LlamaModel` 里的 `rotary_emb` 与 attention 里的 `apply_rotary_pos_emb`。
   - `models/module_map.py`：参考 [module_map.py:24-26](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py#L24-L26) 加一行 `MODULE_MAP.update({'MyLlamaForCausalLM': '...my_llama.MyLlamaForCausalLM'})`。
   - `load_weights` 的 `stacked_params_mapping`：GQA + gate_up 打包契约不变，通常无需改动。
3. **自检问题**：回答——你的 `MyLlamaForCausalLM` 是否还需要 `packed_modules_mapping`？为什么？（答案：需要，因为 `qkv_proj` 与 `gate_up_proj` 仍是打包的，LoRA 注入与权重加载都依赖它。）

> 这个综合实践本质上就是 u10-l1「添加新 PyTorch 模型完整流程」的一个最小预演。做完后，你已经有了一份「新模型接入清单」的雏形。

## 6. 本讲小结

- Llama 的整模型重写在 [llama.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py)，由 `MODULE_MAP` 注册、`_get_model_class` 按 arch 名 `LlamaForCausalLM` 选中（[module_map.py:24-26](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py#L24-L26)）。
- 重写的哲学是「拼装而非手写」：`LlamaAttention` / `LlamaMLP` 内部不含任何 attention/激活数学公式，全部复用 `nn.Attention` / `nn.linear.build_*` / `nn.RMSNorm` / `nn.SiluAndMul` / `nn.ApplyRotaryEmb` 等积木，真正的 kernel 藏在 `backends`。
- 线性层按 `quant_config.get_quant_method(...)` 自动选 FP16/AWQ/W8A8/FP8 实现，重写文件无需写 `if quant` 分支（[linear/__init__.py:258-331](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/__init__.py#L258-L331)）。
- 注意力与 MLP 都做了「融合」：QKV 融合为 `qkv_proj`、gate/up 融合为 `gate_up_proj`，以减少访存；`packed_modules_mapping`（类属性）与 `stacked_params_mapping`（`load_weights` 内）共同把 HF 的分片权重名映射进这些打包参数。
- `forward` 与 `get_logits` 解耦、`prepare_inputs_for_generation` 翻译 `StepContext`、`CudaGraphMixin` 提供 CUDA Graph 能力——这些是 HF 原版没有的「引擎协议」方法。
- 读写本文件只需理解「积木 + 拓扑 + 打包契约」三件事，这是阅读所有 `models/*.py` 的通用钥匙。

## 7. 下一步学习建议

- **横向对比**：打开 [lmdeploy/pytorch/models/qwen.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/qwen.py) 或 `deepseek_v2.py`，对比它们与 `llama.py` 的结构差异——你会发现套路几乎一致，只是 attention/MLP 换了变体（如 MLA、MoE）。这能验证你是否真的掌握了本讲的「积木」视角。
- **深入积木**：本讲只用了 `nn.Attention` / `nn.linear` 等积木的「外壳」，下一单元 U5 会拆开它们。建议先读 u5-l1（nn 优化模块）、u5-l2（线性层与量化变体），看清 `Attention.impl` 与各类 `*Linear` 背后的实现。
- **权重加载**：若对「打包 + TP 切分」的写入细节感兴趣，可读 [model_weight_loader.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py) 与 u3-l5（weight_loader）。
- **接入新模型**：本讲的综合实践是 u10-l1「添加新 PyTorch 模型完整流程」的预演，学完 U5 后可直接进入 u10-l1 完成一次真实的新模型接入。
