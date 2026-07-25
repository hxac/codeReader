# 接入一个新模型架构

## 1. 本讲目标

本讲是「特性与扩展」单元里最综合的一篇：把前面 u2（Python 导出前端）、u4（C++ 构建器）学到的知识串起来，回答一个工程问题——**当你拿到一个 EdgeLLM 还没官方支持的新 decoder-only 模型时，从零把它接进来需要改哪些文件、按什么顺序验证。**

读完本讲，你应该能够：

- 判断一个新检查点是否能「零代码」地走默认 `CausalLM` 跑通，还是必须写自定义模型类。
- 知道 `ModelConfig` 如何把 HF 的 `config.json` 摊平成强类型结构，以及哪些字段需要在 `config.py` 里新增。
- 掌握 `register_model` 注册机制与 `AutoModel` 分发回退逻辑，会为新 `model_type` 写一行注册代码。
- 理解「检查点张量名 ↔ 模块名」的契约，以及 `key_remap` 在什么时候必须出场。
- 拿着 `customization-guide.md` 的扩展点表，独立排出一份「修改文件清单 + export→build→inference 三段验证」的接入计划。

## 2. 前置知识

本讲默认你已经读过以下讲义，下面只做最小回顾，不重复展开：

- **u2-l3（默认解码器模型实现）**：EdgeLLM 不 trace HuggingFace 的 FX 图，而是「从检查点权重直接手写 `nn.Module`」，默认实现是 `CausalLM`，组装层次为 `CausalLM → Transformer → DecoderLayer → Attention/MLP`。
- **u2-l4（检查点加载与权重重排）**：`load_weights` 不用 PyTorch 的 `load_state_dict`，而用自写的 `_set_tensor` 直接写 buffer/parameter，靠「子模块名与 safetensors 的 key 一一对齐」来定位权重；找不到的 key 只记 debug 日志不报错。
- **u4-l2（双阶段优化 profile 与模型类型分发）**：构建器靠 `spec_decode_type / engine_role / maxLoraRank` 等判据，决定加载 `model.onnx` 还是 `lora_model.onnx`，并为 prefill / decode 设两套 profile。

三个贯穿全讲的关键直觉：

1. **配置驱动**：整个流水线（建模 → 导出 → 构建 → 运行时）都从一份 `ModelConfig` 派生形状与 I/O，改一个字段往往就能改图。
2. **以权重事实为准**：`has_qk_norm` 这类能力开关靠扫描检查点里的 key 名判定，**不**靠 `model_type` 字符串匹配——这正是新模型常常能「免注册」就被识别的根因。
3. **Python 端优先，C++ 端尽量不动**：`customization-guide.md` 明确建议，只有当导出的 ONNX 契约或模型 I/O 需要新行为时，才去碰 `cpp/`。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| `docs/source/developer_guide/customization/customization-guide.md` | 官方扩展指南 | 8 个扩展点表、`Adding A Text Model` 五步法 |
| `tensorrt_edgellm/models/default/modeling_default.py` | 默认 decoder-only 实现 | `CausalLM` 的通用能力边界、`onnx_export_spec` |
| `tensorrt_edgellm/model.py` | `AutoModel` 工厂 + 注册表 | `register_model`、`_resolve_model_variant`、分发回退 |
| `tensorrt_edgellm/config.py` | 配置解析 | `ModelConfig.from_pretrained`、`_parse_layer_types`、`_detect_has_qk_norm`、`module_quant_type` |
| `tensorrt_edgellm/__init__.py` | 注册调用现场 | 一排 `register_model(...)` 调用 |
| `tensorrt_edgellm/checkpoint/loader.py` | 权重加载 | `load_weights` 的 key 处理与 `key_remap` 契约 |

## 4. 核心概念与源码讲解

### 4.1 默认 CausalLM：什么时候它就够用（modeling_default）

#### 4.1.1 概念说明

很多新 decoder-only 模型（Llama / Qwen2 / Qwen3 等）其实只是「GQA + SwiGLU + RMSNorm + RoPE」这一标准结构的参数变体。EdgeLLM 的默认 [`CausalLM`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L510-L580) 就是这个通用底盘——只要新检查点的张量名能对上它的子模块名，**一行注册都不用写**就能直接导出。

这就是 `customization-guide.md` 给出的第一条接入纪律（见 4.4）：**先检查默认 `CausalLM` 能不能加载检查点，能就不写自定义类。** 只有当架构需要特殊行为（混合 Mamba/SSM 层、特殊注意力缩放、MoE、多模态 hidden_states 外送等）时，才在 `models/<family>/` 下新建实现。

#### 4.1.2 核心流程

默认 CausalLM 之所以「通用」，靠的是两层配置驱动：

1. **逐层按 `config.num_hidden_layers` / `config.layer_types` 堆叠**：`Transformer` 用一个列表推导式建出所有 `DecoderLayer`，层数完全来自配置，换层数不用改代码。
2. **每个 Linear 都经 `make_linear` 选精度**：`make_linear` 拿 `module_name` 去查 `module_quant_type`，自动决定用 FP16 / FP8 / AWQ / GPTQ / NVFP4 哪种线性层。换量化方案也不用改建模代码。

也就是说，默认 CausalLM 把「形状」交给 `ModelConfig`、把「精度」交给 `make_linear`，自己只负责组装与前向。

#### 4.1.3 源码精读

先看 `Transformer` 如何按配置堆层，并把内部存为 `model` 属性以匹配检查点的 `model.` 前缀：

[tensorrt_edgellm/models/default/modeling_default.py:429-436](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L429-L436) —— `embed_tokens` / `layers` / `norm` 三个子模块全部由 `config` 决定形状，层数 = `config.num_hidden_layers`。

接着看 `Attention` 如何用与检查点 key 同名的子模块建 QKV/O 投影，并把 `module_name`（如 `layers.0.self_attn.q_proj`）透传给 `make_linear`：

[tensorrt_edgellm/models/default/modeling_default.py:219-251](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L219-L251) —— `module_prefix = f"layers.{layer_idx}.self_attn"` 就是检查点里 `q_proj` 等权重的查找路径，同时也是 `module_quant_type` 查表用的短名。

最后看 `CausalLM` 暴露的 `onnx_export_spec`，它是「建模 ↔ 导出」之间的契约：模型自己产出 dummy 输入、I/O 名单和动态形状描述，导出器只负责调用 `torch.onnx.export`：

[tensorrt_edgellm/models/default/modeling_default.py:559-641](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L559-L641) —— `Na = config.num_hidden_layers`（attention 层数）、`Nd = config.num_deepstack_features`（多模态 deepstack 输入数）都从配置来；KV cache 与 RoPE 全部以图 I/O 形式暴露，prefill 与 decode 共用一张图。

> 一句话：默认 CausalLM 的「通用性」来自「形状与精度都委托给配置」，模型代码本身保持稳定。

#### 4.1.4 代码实践

**实践目标**：判断一个新检查点能否零代码走默认 CausalLM。

**操作步骤**：

1. 打开检查点目录，查看 `model.safetensors.index.json`（或单文件 `model.safetensors`）里的 key 名。
2. 对照默认 CausalLM 期望的子模块名（见文件头注释 [modeling_default.py:31-36](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L31-L36)）：`model.layers.N.self_attn.{q,k,v,o}_proj`、`model.layers.N.mlp.{gate,up,down}_proj`、`model.layers.N.{input,post_attention}_layernorm`、`model.embed_tokens`、`model.norm`、`lm_head`。
3. 跑一次导出做最终验证：

```bash
tensorrt-edgellm-export <checkpoint_dir> <output_dir>
```

**需要观察的现象**：

- 若 key 名全部对得上，导出日志里不应出现大量 `Key not found` 的 debug 行；产物目录会生成 `model.onnx`、`config.json`、`embedding.safetensors`、tokenizer 等 sidecar。
- 若检查点用的是融合权重（如 `qkv_proj`、`gate_up_proj`），loader 有兜底拆分（见 u2-l4 的 `_try_split_fused_tensor`），仍可能成功。

**预期结果**：标准 Llama/Qwen 系检查点能在不写任何 Python 代码的情况下导出成功。若出现大量 key 不匹配，或架构含 Mamba/MoE/特殊注意力，则进入 4.3 的「写自定义类」路径。

#### 4.1.5 小练习与答案

**练习 1**：默认 `CausalLM` 的层数由谁决定？换一个 32 层的检查点需要改 modeling 代码吗？
**答案**：由 `config.num_hidden_layers` 决定（`Transformer.__init__` 的列表推导式）；换层数不用改代码，配置驱动即可。

**练习 2**：为什么 `Attention` 里每个投影都传 `module_name=f"layers.{layer_idx}.self_attn.q_proj"`？
**答案**：这个短名同时是两件事的钥匙——(a) 与检查点 safetensors 的 key 对齐以加载权重；(b) 供 `module_quant_type` 查表决定该投影最终用什么精度。

---

### 4.2 ModelConfig：把检查点 config.json 摊平为强类型结构（config）

#### 4.2.1 概念说明

HuggingFace 的 `config.json` 字段散落各处、嵌套不一（RoPE 藏在 `rope_scaling` 里、量化藏在 `hf_quant_config.json`、混合层型藏在 `layers_block_type`）。EdgeLLM 的 [`ModelConfig`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L469-L478) 把它们**摊平成一份强类型、扁平的 dataclass**，下游（建模、导出、构建、运行时）只认 `ModelConfig`，不必关心模型族。

接入新模型时，`config.py` 是第二个要动的地方——前提是新模型有默认解析器不认识的字段（例如新的注意力变体参数、新的混合层型）。

#### 4.2.2 核心流程

`ModelConfig.from_pretrained` 的解析管线：

1. `load_checkpoint_config_dicts(model_dir)` → 读成 `(root_dict, llm_dict)`，多模态里藏的 LLM 子配置会被「提升」上来。
2. 逐项解析：架构尺寸、RoPE（多级兜底）、`layer_types`、Mamba/GDN 子配置、MoE、`has_qk_norm`（**扫描权重 key**）、`has_value_norm`、量化。
3. 量化解析 `_parse_quant`：先看外部 `hf_quant_config.json`，再看内嵌 `quantization_config`，产出 `QuantConfig`。
4. 返回一个填满的 `ModelConfig`。

关键设计哲学（承接 u2-l1）：**能力开关以权重事实为准，而非 `model_type` 字符串**。`has_qk_norm` 靠扫描检查点里有没有 `.q_norm.weight` 来判定，所以任何按此约定存 norm 权重的新架构都能被自动识别，不必改代码。

#### 4.2.3 源码精读

工厂入口 `from_pretrained` 的解析与组装（节选关键步骤）：

[tensorrt_edgellm/config.py:828-853](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L828-L853) —— 注意 `has_qk_norm = _detect_has_qk_norm(model_dir)`，以及 `layer_types` / `mamba_cfg` / `gdn_cfg` 都在这一段被解析。

`has_qk_norm` 的「权重事实」探测：

[tensorrt_edgellm/config.py:1436-1443](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L1436-L1443) —— 只要检查点里有任意 `.q_norm.weight` key 就判定为 True，与 `model_type` 无关。

逐层 `layer_types` 解析（含混合模型的模式串与 Qwen3.5 的 `linear_attention`→GDN 映射）：

[tensorrt_edgellm/config.py:1271-1311](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L1271-L1311) —— 这是混合模型（Mamba / GDN / MoE）接入的关键扩展点：新层型要先在这里被归一化成 `LAYER_ATTN / LAYER_MAMBA / LAYER_GDN / LAYER_MLP / LAYER_MOE` 之一。

「某模块最终用什么精度」的唯一真相来源 `module_quant_type`：

[tensorrt_edgellm/config.py:393-419](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L393-L419) —— `make_linear` 和导出校验都消费它；`excluded` 与 `layer_overrides` 的 key 都已被归一化成短名（与建模代码传的 `module_name` 同名空间）。

> 一句话：`config.py` 把 HF 的散乱字段摊平，新模型若带来新字段，就在 `from_pretrained` 与对应 `_parse_*` 里加解析；能复用既有约定（如 `.q_norm.weight`）就尽量零代码识别。

#### 4.2.4 代码实践

**实践目标**：亲手解析一个真实检查点，看 `ModelConfig` 产出了哪些字段。

**操作步骤**：

```python
from tensorrt_edgellm.model import load_model_config

cfg = load_model_config("/path/to/qwen3-checkpoint")
print(cfg.model_type, cfg.num_hidden_layers, cfg.num_attention_heads)
print("layer_types:", cfg.layer_types)
print("has_qk_norm:", cfg.has_qk_norm)
print("quant:", cfg.quant.quant_type, "group_size=", cfg.quant.group_size)
print("excluded:", cfg.quant.excluded[:5])
```

**需要观察的现象**：

- `layer_types` 为空列表 `[]` 表示纯 attention 模型（默认 CausalLM 适用）；若含 `mamba` / `gdn` 则是混合模型。
- `has_qk_norm` 的值应与检查点里是否存在 `.q_norm.weight` 一致。
- 量化检查点（带 `hf_quant_config.json`）的 `quant_type` 应为 `fp8` / `nvfp4` / `int4_awq_modelopt` 等。

**预期结果**：能完整打印架构与量化字段；若某个关键字段为默认值而你认为该模型有此特性，说明解析器未覆盖该约定，需要在 `config.py` 增补。**待本地验证**：具体输出取决于所用检查点。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `has_qk_norm` 不写 `if model_type == "qwen3": return True`？
**答案**：因为那是「字符串假设」，新模型一上就失效；扫描 `.q_norm.weight` 是「权重事实」，任何遵守该命名的架构都被正确识别，无需改码。

**练习 2**：一个新混合模型引入了全新的层型 `linear_attention_v2`，需要在哪改？
**答案**：先在 `_parse_layer_types`（config.py:1271）里把它归一化成既有标签（或新增一个 `LAYER_*` 常量），再在建模侧实现对应的层；否则 `layer_types` 会把它误判成默认 attention。

---

### 4.3 注册表与 AutoModel 分发 + 权重加载契约（model + loader）

#### 4.3.1 概念说明

当默认 CausalLM 不够用时（混合架构、特殊注意力缩放、需要外送 hidden_states 等），就要写一个 `nn.Module` 子类，并通过 `register_model` 把它登记进注册表。`AutoModel.from_pretrained` 拿到 `model_type` 后查表，查到就用自定义类，**查不到就回退默认 CausalLM**。

与注册表同等重要的是**权重加载契约**：`load_weights` 靠「检查点 key 名 ↔ 模块名」对齐来灌权重。新模型的检查点若用了不同的命名（例如 EAGLE3 的 `midlayer.`、`qkv_proj.`），就必须提供一个 `key_remap` 把检查点 key 翻译成模块期望的名字。

#### 4.3.2 核心流程

`AutoModel.from_pretrained` 的分发主线：

1. `load_model_config(model_dir)` → 解析配置。
2. `_resolve_model_variant(...)` → 裁决唯一变体（普通 `llm` / `eagle3_draft` / `mtp_*` / `dflash_*` / `gemma4_mtp_*`），含互斥校验。
3. 按变体选 `model_class`：草稿变体硬编码；普通 LLM 走 `_MODEL_REGISTRY.get(model_type, CausalLM)`。
4. `model_class(config)` 建空壳 → `load_weights(...)` 灌权重（可带 `key_remap`）。
5. 后处理（tie weights、reduced vocab、GDN 融合等）。

`load_weights` 的 key 处理：剥前缀（VL 多模态的 `language_model.` 等）→ `key_remap`（draft 模型重命名）→ `_set_tensor`（融合权重由 `_try_split_fused_tensor` 兜底拆分）→ `pre_repack_hook` → 量化 repack。

#### 4.3.3 源码精读

两张注册表与 `register_model`：

[tensorrt_edgellm/model.py:47-48](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L47-L48) —— `_MODEL_REGISTRY`（model_type→类）与 `_ATTENTION_SCALE_DEFAULT_REGISTRY`（model_type→注意力缩放函数）。

[tensorrt_edgellm/model.py:82-98](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L82-L98) —— `register_model` 把类与缩放默认值同时登记，是接入新模型类的唯一公开入口。

分发回退到默认 CausalLM 的关键一行：

[tensorrt_edgellm/model.py:318-322](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L318-L322) —— 普通模型 `model_class = _MODEL_REGISTRY.get(config.model_type, CausalLM)`，查不到就用默认，这就是「不注册也能跑」的实现。

变体裁决（互斥校验 + 优先级）：

[tensorrt_edgellm/model.py:475-533](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L475-L533) —— 新接入的纯 decoder-only 模型通常落在最后的 `return "llm"`，不涉及变体逻辑。

key remap 的样本（EAGLE3 draft，展示「检查点名→模块名」翻译）：

[tensorrt_edgellm/model.py:536-558](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L536-L558) —— `midlayer.` → `layers.0.`、跳过 `t2d` / `target_model.`。若你的新检查点命名与模块名不一致，就在这里加一个同类函数并通过 `key_remap` 传入。

权重加载入口与 `key_remap` 契约：

[tensorrt_edgellm/checkpoint/loader.py:59-91](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/loader.py#L59-L91) —— `key_remap` 对每个（剥前缀后的）key 调用，返回新 key（重命名）、原 key（不变）或 `None`（跳过）。

> 一句话：`model.py` 负责「选哪个类 + 重命名哪些 key」，`loader.py` 负责「按名灌权重」；新模型若命名与默认对齐就两处都不用动。

#### 4.3.4 代码实践

**实践目标**：列出当前注册了哪些 model_type→类映射，并写出一行注册新模型的伪代码。

**操作步骤**：

1. 读 `__init__.py` 的注册现场，统计映射：

[tensorrt_edgellm/__init__.py:60-91](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/__init__.py#L60-L91) —— 可以看到 `gemma4` / `nemotron_h` / `qwen3_5_text` / `qwen3_moe` / `qwen3_omni*` 等都各自映射到自定义类，而 `llama` / `qwen2` / `qwen3` **没有出现在表里**（走默认 CausalLM）。

2. 为假设的新模型 `myarch` 写注册（示例代码，非项目原有）：

```python
from .models.myarch.modeling_myarch import MyArchCausalLM
from .model import register_model, standard_attention_scale

register_model("myarch", MyArchCausalLM, standard_attention_scale)
```

**需要观察的现象**：

- 支持 matrix 里 `llama` / `qwen2` / `qwen3` 的 `Handling` 列写着 `-> default CausalLM`，正是因为它们没有 `register_model` 行（见 `supported-models.md`）。
- 注册后，`AutoModel.from_pretrained` 遇到 `model_type == "myarch"` 会用 `MyArchCausalLM`。

**预期结果**：能说清「哪些模型走默认、哪些走自定义类」，并掌握 `register_model` 的三参数（model_type、类、注意力缩放默认函数）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `llama` 不在 `__init__.py` 的注册表里却仍能导出？
**答案**：因为分发是 `_MODEL_REGISTRY.get(model_type, CausalLM)`，查不到就回退默认 `CausalLM`；llama 是标准结构，默认实现即可加载。

**练习 2**：新检查点的投影权重叫 `model.layers.N.self_attn.qkv_proj.weight`（融合），需要写 `key_remap` 吗？
**答案**：通常不需要——loader 有 `_try_split_fused_tensor` 兜底，能把 `qkv_proj` / `gate_up_proj` 自动拆成 `q/k/v_proj` 与 `gate/up_proj`（见 u2-l4）。只有像 EAGLE3 那种结构性命名差异才需要专门的 `key_remap`。

---

### 4.4 端到端接入清单与验证流程（customization 文档）

#### 4.4.1 概念说明

`customization-guide.md` 是 NVIDIA 官方给接入者画的「边界图」。它把扩展点拆成 8 个区域，并用一张表告诉你每个区域动哪个文件、改什么。本模块把它落到一份**可勾选的工程清单**，并配上 export→build→inference 的三段验证。

核心原则有两条：

- **新增模型和功能应瞄准 checkpoint 工作流**（`quantization` 量化 + `tensorrt_edgellm` 导出），而不是去改 C++。
- **C++ 端只在 ONNX 契约或模型 I/O 需要新行为时才动**；优先等 Python 端的 ONNX 与 sidecar 稳定后再碰运行时。

#### 4.4.2 核心流程

官方 `Adding A Text Model` 五步（与 4.1–4.3 完全对应）：

1. 先看默认 `CausalLM` 能不能加载检查点。
2. 不行就在 `models/<family>/` 下加模型实现，**保持参数名与 HF 检查点对齐**。
3. 在 `__init__.py` 注册 `model_type`。
4. 在 `config.py` / `checkpoint/` 加所需的配置提升、key remap、融合权重拆分或量化元数据处理。
5. 用 `tensorrt-edgellm-export` 导出，并验证 `llm_build` + `llm_inference`。

验证三段式（承接 u1-l5）：

- **export**：`tensorrt-edgellm-export <ckpt> <out>` → 产出 `model.onnx` + sidecar。
- **build**：`llm_build --onnxDir <out> --engineDir <eng> ...` → 编译出 `engine`（注意：engine 不可跨 GPU 型号 / TRT 版本迁移）。
- **inference**：`llm_inference --engineDir <eng> ...` → 跑出文本。

#### 4.4.3 源码精读

8 个导出扩展点表（接入时按表查「动哪个文件」）：

[docs/source/developer_guide/customization/customization-guide.md:11-22](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/customization/customization-guide.md#L11-L22) —— 文本模型、注册、检查点解析、权重加载、编码器导出、组件编排、自定义算子、运行时/插件八行。

`Adding A Text Model` 五步法：

[docs/source/developer_guide/customization/customization-guide.md:44-53](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/customization/customization-guide.md#L44-L53) —— 注意第 5 步明确要求验证 `llm_build` + `llm_inference`。

自定义算子的三件套（仅当需要新算子时）：

[docs/source/developer_guide/customization/customization-guide.md:76-85](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/customization/customization-guide.md#L76-L85) —— `ops.py`（torch stub）+ `dynamo_translations.py`（ONNX 翻译）+ `onnx_custom_schemas.py`（schema），且**运行时支持必须先于文档声明存在**。

> 一句话：`customization-guide.md` 是接入的总目录，先用它的表定边界，再用 4.1–4.3 的源码细节填内容。

#### 4.4.4 代码实践

**实践目标**：为一个假设的新 decoder-only 模型排出完整文件清单与验证方式（即本讲 `practice_task`）。

**操作步骤**：见第 5 节「综合实践」的完整清单——它就是本模块的实践产出。

**需要观察的现象 / 预期结果**：清单应覆盖「可能改 / 可能新增」的文件，且每一步都绑定一个 export→build→inference 的验证命令。

#### 4.4.5 小练习与答案

**练习 1**：接入一个新算子需要同时改哪三个 Python 文件？还需要补什么？
**答案**：`ops.py`（stub）、`dynamo_translations.py`（翻译规则）、`onnx_custom_schemas.py`（schema）；并且对应的 C++ 插件/运行时支持必须已存在，否则导出的 ONNX 会在构建期因未知算子失败。

**练习 2**：为什么 `customization-guide` 建议尽量晚地动 `cpp/`？
**答案**：因为 Python 端的 ONNX 契约和 sidecar 还在变时改 C++ 会反复返工；等导出端稳定后再补运行时，能避免无效劳动，也符合「config 驱动、Python 优先」的整体设计。

---

## 5. 综合实践

**任务**：假设要接入一个新的 decoder-only 模型（`model_type = "myarch"`），列出需要修改/新增的文件清单，并说明每一步的验证方式（export→build→inference）。

### 第 0 步：先做「零代码」试探

在动任何代码前，先跑默认路径：

```bash
tensorrt-edgellm-export /path/to/myarch /tmp/myarch_out
```

- 若成功产出 `model.onnx` + sidecar → 直接跳到第 4 步验证，**本任务完成**（标准结构无需改码）。
- 若失败（key 大量不匹配 / 架构特殊）→ 进入第 1 步。

> 判据来自 4.1：默认 `CausalLM` 的子模块名约定见 [modeling_default.py:31-36](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L31-L36)。

### 文件清单（按改动概率从高到低）

| # | 文件 | 改动内容 | 触发条件 |
|---|------|---------|---------|
| 1 | `tensorrt_edgellm/__init__.py` | 加 `register_model("myarch", MyArchCausalLM, scale_fn)` | 写了自定义类（必改） |
| 2 | `tensorrt_edgellm/models/myarch/modeling_myarch.py`（**新增**） | 自定义 `nn.Module`，子模块名对齐检查点 key | 默认 CausalLM 无法表达架构 |
| 3 | `tensorrt_edgellm/config.py` | 在 `from_pretrained` / `_parse_*` 里解析新字段或新层型 | 新模型带了默认解析器不认识的字段 |
| 4 | `tensorrt_edgellm/checkpoint/loader.py` 或 `model.py` 的 `key_remap` | 检查点命名与模块名不一致时翻译 | 命名差异（如 `midlayer.`） |
| 5 | `tensorrt_edgellm/models/ops.py` + `onnx/dynamo_translations.py` + `onnx/onnx_custom_schemas.py` | 新算子 stub + 翻译 + schema | 需要 lowering 到 TRT 插件的新算子 |
| 6 | `cpp/plugins/`、`cpp/runtime/` 等 | 新插件 / 运行时行为 | ONNX 契约或模型 I/O 需要新行为（最后才动） |
| 7 | `docs/.../supported-models.md` | 登记新模型行 | 正式支持时（文档纪律） |
| 8 | `tests/` | 加聚焦的导出测试 | 新增模型族（文档要求） |

### 验证流程（每改一处都要重跑）

1. **export**（验证建模 + 配置 + 权重加载）：
   ```bash
   tensorrt-edgellm-export /path/to/myarch /tmp/myarch_out
   ```
   成功标志：生成 `model.onnx`，日志无大量 `Key not found`。
2. **build**（验证 ONNX 契约 + profile）：
   ```bash
   examples/llm/llm_build --onnxDir /tmp/myarch_out --engineDir /tmp/myarch_eng \
       --maxBatchSize 1 --maxInputLen 1024 --maxKVCacheCapacity 2048
   ```
   成功标志：生成 engine。注意 `maxKVCacheCapacity ≥ maxInputLen`（承接 u4-l3），且 engine 绑定当前 GPU 型号与 TRT 版本，不可跨环境迁移。
3. **inference**（验证端到端正确性）：
   ```bash
   examples/llm/llm_inference --engineDir /tmp/myarch_eng --inputFile input.json
   ```
   成功标志：产出合理文本。

> **若无法在本机运行**（无 GPU / 无检查点）：至少把三条命令按真实参数组装完整、解释每个参数含义，并标注「待本地验证」——不要假装已跑过。

### 自查要点

- 自定义类的构造函数必须只接受一个 `ModelConfig`（见 [model.py:82-98](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L82-L98) 的约定）。
- 若模型需要外送 hidden_states（如 Qwen3-Omni Thinker→Talker），在子类设类属性 `emit_hidden_states = True`（见 [modeling_default.py:522-524](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L522-L524)）。
- 注意力缩放若非常规（如 Gemma4 用恒等缩放），注册时传对应函数（见 `__init__.py` 里 `_identity_attention_scale`）。

## 6. 本讲小结

- **默认 CausalLM 是通用底盘**：标准 decoder-only 结构靠配置驱动层数、靠 `make_linear` 选精度，往往零代码就能导出（4.1）。
- **`ModelConfig` 把 HF 散乱字段摊平**：新模型若带来新字段/新层型，才需要在 `config.py` 增补；能力开关以权重事实为准（4.2）。
- **注册表 + 分发回退**：`register_model` 是接入自定义类的唯一入口，查不到 `model_type` 自动回退默认 `CausalLM`（4.3）。
- **权重加载靠「名对名」契约**：命名对齐则免 `key_remap`，结构性差异才需翻译（4.3）。
- **`customization-guide.md` 是总目录**：8 个扩展点表 + `Adding A Text Model` 五步法 + 「Python 优先、C++ 最后」的边界纪律（4.4）。
- **验证永远三段式**：export → build → inference，每改一处都要重跑；engine 不可跨 GPU/TRT 版本迁移。

## 7. 下一步学习建议

- 若你的新模型是**混合架构**（含 Mamba/SSM/GDN），精读 [`tensorrt_edgellm/models/nemotron_h/modeling_nemotron_h.py`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/nemotron_h/modeling_nemotron_h.py)——它是自定义类如何重写层堆叠（带 `conv_states` / `ssm_states`）的最佳样本。
- 若需要**新算子**，回到 u2-l5（ONNX 导出）与 u8-l1（插件架构），按 `ops.py → dynamo_translations.py → onnx_custom_schemas.py → cpp/plugins` 的链路落地。
- 若要接入的是**多模态/音频/Omni**组件，参考 `customization-guide.md` 的 `Adding A Multimodal Or Action Component` 一节与 u2-l6（导出 CLI 与组件编排）。
- 阅读官方 [supported-models.md](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/getting_started/supported-models.md)，对照「Handling」列理解每个模型族是走默认还是自定义类。
