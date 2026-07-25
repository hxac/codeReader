# 添加一个新模型

## 1. 本讲目标

本讲是「模型与注册机制」单元的实践篇。学完 u5-l1（架构范式）和 u5-l2（自动发现与注册）后，你已经知道模型长什么样、又是怎么被查表找到的。本讲把这两讲串成一条**可落地的 onboarding 流程**——从拿到一个 HuggingFace 模型，到它在 TensorRT-LLM 里跑起来，中间要写哪几样东西、按什么顺序写、踩哪些坑。

读完本讲你应当能够：

1. 复述添加一个新模型的**四步流程**：配置 → 模型定义 → 权重加载 → 注册。
2. 区分 **in-tree（仓内）** 与 **out-of-tree（仓外）** 两种注册方式，并知道何时用哪种。
3. 知道如何**复用 HuggingFace 的配置类**，以及如何处理 checkpoint 权重名与本仓模块名之间的差异（融合层、KV 复制、消费式加载）。
4. 独立起草一份最小可用的 `modeling_mymodel.py` 骨架。

> 提醒：本讲只覆盖 PyTorch 后端（`tensorrt_llm/_torch/models/`）。AutoDeploy 复用同一批模型定义，但走图变换路径，留待 u12-l1。

## 2. 前置知识

本讲默认你已经掌握以下来自前置讲义的概念，下面只做一句话回顾，不再展开：

- **三段式架构范式（u5-l1）**：`DecoderModelForCausalLM`（外壳，管 lm head / logits / 权重加载）+ `DecoderModel`（骨干，管 embed→逐层→norm）+ `DecoderLayer`（单层 forward 契约）。残差流是 `(hidden, residual)`。外壳由元类 `PostInitCaller` 自动串联 `__post_init__`（量化/create_weights）与 `__pp_init__`（流水线切层）。
- **自动发现（u5-l2）**：以 HF `config.json` 的 `architectures[0]` 为钥匙，在 `MODEL_CLASS_MAPPING` 里查 `ForCausalLM` 类；查表前 `_resolve_class` 会按多模态 / EAGLE3 / MTP 等情形**改写**架构名。`@register_auto_model` 装饰器在 `import` 时自注册，**import 即注册**。

如果你对上面任何一句感到陌生，请先回到对应讲义。

此外需要知道两个本讲会用到但前置讲义未细讲的术语：

- **checkpoint**：模型训练后保存的权重文件（通常是 `.safetensors`）。它的**张量命名**（如 `model.layers.0.self_attn.q_proj.weight`）由 HF 那一侧决定，未必和我们模块树里的名字一一对应。
- **融合层（fused layer）**：为了省算子、省访存，我们把 HF 里独立的 Q/K/V 三个线性层合成一个 `qkv_proj`，把 gate/up 合成一个 `gate_up_proj`。这会让 checkpoint 名字对不上模块名字，正是「权重加载」要解决的核心问题。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `docs/source/torch/adding_new_model.md` | 官方 onboarding 指南，本讲的主干 |
| `tensorrt_llm/_torch/models/modeling_utils.py` | 三大基类 `DecoderModel` / `DecoderModelForCausalLM` 与注册机制 `MODEL_CLASS_MAPPING` / `register_auto_model` 的定义地 |
| `tensorrt_llm/_torch/models/modeling_auto.py` | `AutoModelForCausalLM._resolve_class` / `from_config`，查表与实例化的入口 |
| `tensorrt_llm/_torch/models/modeling_deepseekv3.py` | 一个复杂的「生产级」参考实现（MoE + MLA + 投机解码），展示自定义 `load_weights` 与多别名注册 |
| `examples/llm-api/out_of_tree_example/modeling_opt.py` | 一个「教科书级」的简化参考实现（OPT 模型），最适合照着学 |
| `tensorrt_llm/_torch/models/checkpoints/base_weight_loader.py` | `ConsumableWeightsDict`：权重字典的「消费式」包装，边加载边释放 |
| `tensorrt_llm/_torch/models/hf_parameter_utils.py` | 两个小工具函数 `get_parameter_device` / `get_parameter_dtype`，模型代码偶尔会用到 |
| `tensorrt_llm/_torch/modules/decoder_layer.py` | `DecoderLayer` 抽象基类与 `skip_forward` |

## 4. 核心概念与源码讲解

本讲的四个最小模块正好对应官方指南的四个步骤。我们用「指南说什么 → 源码里长什么样」的对照方式讲解。

### 4.1 配置（Model Configuration）

#### 4.1.1 概念说明

第一步永远是**配置**：你得先告诉框架「这个模型的形状参数是什么」——hidden_size、层数、头数、词表大小等等。在 TensorRT-LLM 里，这部分**直接复用 HuggingFace 的配置类**，不自己重新发明。

关键判断只有一条：

- 模型已在 HF `transformers` 里？→ 直接 `from transformers import XxxConfig`。
- 不在？→ 照着 HF 的 `configuration_llama.py` 自己写一个 `class MyConfig(PretrainedConfig)`。

这一步看起来平淡，但它决定了后面所有「钥匙」。回顾 u5-l2：自动发现用的是 `pretrained_config.architectures[0]` 去查表，而 `architectures` 字段就写在 HF 配置类 / `config.json` 里。配置类选错了，后面的注册全部白搭。

#### 4.1.2 核心流程

```
HF config.json
    │  (由 transformers 解析)
    ▼
PretrainedConfig 子类实例  ←  architectures 字段 = "MyModelForCausalLM"
    │
    ▼
ModelConfig[MyConfig]      ←  包一层运行时开关（并行/量化/注意力后端，见 u4-l3）
    │
    ▼
AutoModelForCausalLM._resolve_class(config)  →  MODEL_CLASS_MAPPING["MyModelForCausalLM"]
```

`ModelConfig[TConfig]` 是带泛型的运行时包裹层（u4-l3 讲过），它把 HF 配置和并行拓扑、量化、注意力后端等「部署期才确定」的开关打包。配置类本身只是它的类型参数 `TConfig`。

#### 4.1.3 源码精读

官方指南明确给出两条配置路径（已在 transformers 里 vs 不在）：

- [adding_new_model.md:25-41](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/adding_new_model.md#L25-L41) 说明：若已在 transformers，复用其配置类；否则自己继承 `PretrainedConfig` 定义。

复用 HF 配置最直观的例子是 out-of-tree 的 OPT 实现，它直接用 `from transformers import OPTConfig` 作为 `ModelConfig` 的类型参数：

- [modeling_opt.py:6-18](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/llm-api/out_of_tree_example/modeling_opt.py#L6-L18) `from transformers import OPTConfig`，后续所有类的签名都是 `ModelConfig[OPTConfig]`。

而查表入口 `_resolve_class` 正是读 `pretrained_config.architectures[0]` 去 `MODEL_CLASS_MAPPING.get(...)`：

- [modeling_auto.py:13-43](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_auto.py#L13-L43) 先取 `architectures[0]`，按 EAGLE3 / MTP 等情形改写，最后 `MODEL_CLASS_MAPPING.get(model_arch)`。这印证了「配置里的 architectures 字段就是自动发现的钥匙」。

> 旁注：`hf_parameter_utils.py` 只是一个**很小的工具文件**，提供 `get_parameter_device` / `get_parameter_dtype` 两个函数（见 [hf_parameter_utils.py:28-33](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/hf_parameter_utils.py#L28-L33)），用于在 transformers v5 不再导出它们时做替代。新模型通常用不到，知道它存在即可，不要误以为它承担了核心配置逻辑。

#### 4.1.4 代码实践

**实践目标**：理解配置类与「钥匙」的对应关系。

1. 打开任意一个 HF 模型的 `config.json`（例如本机已有的 `facebook/opt-125m` 或随便一个），找到 `"architectures"` 字段，记下它的值。
2. 对照 [modeling_opt.py:228](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/llm-api/out_of_tree_example/modeling_opt.py#L228) 的 `@register_auto_model("OPTForCausalLM")`，确认两者的字符串**必须完全一致**。
3. **需要观察的现象**：若把注册名故意写错（例如 `"OPTForCausalLM"` 写成 `"OptForCausalLM"`），运行时会落到 `_resolve_class` 返回 `None` 的分支，抛出 `Unknown architecture for AutoModelForCausalLM`（见 [modeling_auto.py:60-63](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_auto.py#L60-L63)）。
4. **预期结果**：建立「`config.json` 的 `architectures[0]` ⟷ `@register_auto_model(name)` 的 name」这一条强绑定的直觉。

#### 4.1.5 小练习与答案

**练习 1**：如果你的模型不在 HF transformers 里，配置类应该怎么写？
**答案**：新建 `configuration_mymodel.py`，定义 `class MyConfig(PretrainedConfig)`，照着 HF 的 `configuration_llama.py` 组织字段，并确保 `architectures` 字段会带上你将来注册用的名字。

**练习 2**：`ModelConfig[MyConfig]` 里的 `MyConfig` 和 `architectures` 字段分别扮演什么角色？
**答案**：`MyConfig` 描述**模型形状**（hidden_size、层数等，来自 checkpoint）；`architectures[0]` 是**注册钥匙**，决定框架实例化哪个 `ForCausalLM` 类。二者职责不同。

---

### 4.2 模型定义（Model Definition）

#### 4.2.1 概念说明

第二步是**定义模型代码**：把 HF 那份 `modeling_xxx.py` 移植过来，删掉训练相关代码，然后把若干原生 PyTorch 模块替换成本仓的高性能实现。一个典型 decoder 模型需要写**四个类**，正好对应 u5-l1 讲过的三层范式再加一个 attention 子类：

| 类 | 继承自 | 职责 |
|----|--------|------|
| `MyAttention` | `Attention` | 让注意力计算与运行时兼容（KV cache metadata 契约） |
| `MyDecoderLayer` | `DecoderLayer` | 单层 forward（norm → attn → residual → mlp → residual） |
| `MyModel` | `DecoderModel` | 骨干：embed + ModuleList(layers) + norm |
| `MyModelForCausalLM` | `DecoderModelForCausalLM[MyModel, MyConfig]` | 外壳：lm head / logits / 权重加载挂载点 |

核心心法（承接 u5-l1）：**复用多、覆盖少**。基类已经把 lm head、logits 处理、权重加载脚手架、流水线切分都做好了，你只写「这个模型特有的」东西。

#### 4.2.2 核心流程

```
写 MyAttention(Attention)        # 只在 __init__ 里把 config 参数喂给 super().__init__
        │
写 MyDecoderLayer(DecoderLayer)  # 组装 self_attn / norm / mlp，实现 forward
        │
写 MyModel(DecoderModel)         # 组装 embed_tokens + layers ModuleList + norm，实现 forward 逐层循环
        │
写 MyModelForCausalLM(DecoderModelForCausalLM)
        │  super().__init__(MyModel(...), config=..., hidden_size=..., vocab_size=...)
        ▼
元类 PostInitCaller 自动串联 __post_init__ + __pp_init__   # 量化、建权重、切流水线
```

两个移植要点（来自指南）：

1. **`attn_metadata` 必须正确传递**：它由运行时创建并下传，存放了批处理输入与 KV cache 的元数据，模型开发者要确保它一路传到 attention 模块（见 u6-l1）。
2. **输入张量是 packed 模式**：`input_ids` / `position_ids` / `hidden_states` 的第一维是「一个 batch 内所有 token 拍平后的总数」，不是 `[batch, seq]`。这一点和 HF 的 `[batch, seq]` 写法不同，移植时要改。

#### 4.2.3 源码精读

官方指南给出完整骨架（这是本讲最重要的代码模板）：

- [adding_new_model.md:47-105](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/adding_new_model.md#L47-L105) 四个类的最小骨架：`MyAttention` / `MyDecoderLayer` / `MyModel` / `MyModelForCausalLM`，最后一行 `super().__init__(MyModel(model_config), config=..., hidden_size=..., vocab_size=...)` 是外壳初始化的标准姿势。
- [adding_new_model.md:107-118](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/adding_new_model.md#L107-L118) 两个移植要点（attn_metadata 契约、packed 输入）+ 可选替换的高性能模块清单（`Linear` / `Embedding` / `RotaryEmbedding` / `RMSNorm`）。

`DecoderLayer` 基类本身极简，只规定 forward 签名并提供 PP 用的 `skip_forward`：

- [decoder_layer.py:10-34](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/decoder_layer.py#L10-L34) `class DecoderLayer(nn.Module, ABC)`，`forward` 是 `@abstractmethod`，`skip_forward` 在非首/末 PP rank 上把 forward 替换成「原样透传 hidden/residual」。

简化参考实现 OPT，它的 `OPTDecoderLayer.forward` 是理解单层 forward 的最好入口（比 DeepSeekV3 简单得多）：

- [modeling_opt.py:88-130](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/llm-api/out_of_tree_example/modeling_opt.py#L88-L130) 标准 `residual = hidden_states; norm; attn; add; norm; mlp; add` 模式。注意 OPT 的特殊之处是 `do_layer_norm_before` 决定 norm 在前还是在后，这是「模型特有逻辑」的典型例子。
- [modeling_opt.py:133-225](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/llm-api/out_of_tree_example/modeling_opt.py#L133-L225) `OPTModel(DecoderModel)`：组装 `embed_tokens` / `embed_positions` / `layers`，`forward` 里逐层调用。

外壳的标准初始化（OPT 例子，最干净）：

- [modeling_opt.py:228-238](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/llm-api/out_of_tree_example/modeling_opt.py#L228-L238) `@register_auto_model("OPTForCausalLM")` + `class OPTForCausalLM(DecoderModelForCausalLM[OPTModel, OPTConfig])`，`__init__` 只有一句 `super().__init__(OPTModel(model_config), config=model_config, hidden_size=..., vocab_size=...)`。

对照基类 `DecoderModelForCausalLM.__init__` 看这句 `super().__init__` 实际做了什么：

- [modeling_utils.py:377-485](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_utils.py#L377-L485) 基类负责：建 `lm_head`（含 TP / 量化 / tied embeddings 处理）、挂 `logits_processor`、设置 `prologue/epilogue`。这印证了「外壳零成本」——你只要传对四个参数。
- [modeling_utils.py:472-480](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_utils.py#L472-L480) tied embeddings（`tie_word_embeddings=True`）时，`lm_head.weight` 直接复用 `embed_tokens.weight`，无需额外加载——这是「复用基类能力」的一个具体收益。

> **进阶提示**：OPT 例子直接继承 `DecoderModelForCausalLM`，这是指南推荐的最简路径。生产级模型（Llama、DeepSeekV3）继承的是它的子类 `SpecDecOneEngineForCausalLM`（[modeling_speculative.py:1894](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_speculative.py#L1894)），为单引擎投机解码做了扩展。初学添加模型时**不必**继承它，先把基础跑通。

#### 4.2.4 代码实践

**实践目标**：照着 OPT 的简单度，起草一个假想 `MyModel` 的单层 forward。

1. 复制 [modeling_opt.py:88-130](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/llm-api/out_of_tree_example/modeling_opt.py#L88-L130) 的结构，把 `OPTDecoderLayer` 改名为 `MyDecoderLayer`。
2. 假设你的模型是「标准 pre-norm Llama 式」：`input_layernorm → attn → +residual → post_attention_layernorm → mlp → +residual`。把 OPT 里的 `do_layer_norm_before` 分支去掉，写成固定的 pre-norm 顺序。
3. **需要观察的现象**：你的 forward 返回的是单个 `hidden_states`（OPT 风格）还是 `(hidden_states, residual)` 元组（Llama/DeepSeekV3 风格）？对照 [decoder_layer.py:12-21](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/decoder_layer.py#L12-L21)，基类允许两种返回。
4. **预期结果**：得到一个 30 行以内的 `MyDecoderLayer.forward`，能在脑子里走通残差流。
5. 若不确定 pre-norm 与 OPT 的 norm 位置差异如何影响数值，标注「待本地验证」再用真实 checkpoint 比对。

#### 4.2.5 小练习与答案

**练习 1**：为什么指南建议把原生 `nn.Linear` 换成本仓的 `Linear`？
**答案**：本仓 `Linear`（`tensorrt_llm/_torch/modules/linear.py`）内置了张量并行与量化支持，换成它才能免费获得 TP / FP8 / FP4 能力；用原生 `nn.Linear` 则这些特性都不会生效。

**练习 2**：`MyModelForCausalLM.__init__` 里那句 `super().__init__(...)` 必须传哪四个关键参数？
**答案**：`model`（骨干实例）、`config`（`ModelConfig`）、`hidden_size`、`vocab_size`。基类据此建 lm head 与 logits 处理器（见 [modeling_utils.py:381-382](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_utils.py#L381-L382)）。

---

### 4.3 权重加载（Weight Loading）

#### 4.3.1 概念说明

第三步是**权重加载**，也是最容易踩坑的一步。问题本质是：**checkpoint 里的张量名/布局，和你定义的模块树名/布局对不上**。

典型的不匹配有四类：

1. **融合层**：HF 存 `q_proj` / `k_proj` / `v_proj` 三个权重，你模块里是单个 `qkv_proj`。要把三个拼成一个再赋值。
2. **GQA 的 KV 复制**：当 `num_kv_heads < tp_size` 时，每个 TP rank 需要把 KV 权重复制若干份（`duplicate_kv_weight`）。
3. **名字前缀差异**：HF 的 OPT 权重以 `decoder.layers...` 开头，你的模块树是 `model.layers...`，要做前缀替换。
4. **量化重排**：FP4/FP8 权重需要按 block scale 重排或反量化（DeepSeekV3 里大量出现）。

好消息：基类 `DecoderModelForCausalLM` 提供了一个**默认 `load_weights`**（`_load_weights_impl`），能自动处理最常见的融合层（qkv_proj / gate_up_proj）和 KV 复制。**只有默认逻辑搞不定时**，你才需要覆盖 `load_weights`。

#### 4.3.2 核心流程

```
ModelLoader 读 checkpoint → 得到 weights 字典
        │
        ▼
model.load_weights(weights)            # 默认走基类实现，否则走你覆盖的版本
        │
        ├─ 若 names[-1] in {'qkv_proj','gate_up_proj'} → 合并多个源权重，调子模块 load_weights
        ├─ k_proj/v_proj → duplicate_kv_weight（GQA 复制）
        └─ 其余 → filter_weights 后直接 copy_ 到参数
        ▼
post_load_weights() = setup_aliases() + transform_weights() + cache_derived_state()
```

融合 QKV 的形状用公式表达：三个独立的 \([H, H]\) 权重拼接成一个

\[
W_{\text{qkv}} \in \mathbb{R}^{\,3H \times H}, \quad W_{\text{qkv}} = \begin{bmatrix} W_Q \\ W_K \\ W_V \end{bmatrix}
\]

这正是指南里「三个权重拼接成一个」的数学含义（[adding_new_model.md:134-154](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/adding_new_model.md#L134-L154)）。

#### 4.3.3 源码精读

指南说明何时需要自定义 `load_weights`：

- [adding_new_model.md:122-156](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/adding_new_model.md#L122-L156) 基类提供默认 `load_weights`，但若默认不适用就自己实现；并强调**推荐调用模块级的 `load_weights`**（如 `Linear`、`Embedding`），而不是自己手写 copy，因为它们已处理好 TP 与量化。

基类默认实现的核心逻辑（理解它能帮你判断「要不要覆盖」）：

- [modeling_utils.py:756-780](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_utils.py#L756-L780) `DecoderModelForCausalLM.load_weights`：有 `weight_mapper` 走 v2，否则走 `_load_weights_impl`。
- [modeling_utils.py:1069-1072](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_utils.py#L1069-L1072) 默认的 `params_map = {'qkv_proj': ['q_proj','k_proj','v_proj'], 'gate_up_proj': ['gate_proj','up_proj']}`——这就是「默认能自动处理 qkv/gate_up 融合」的真相。
- [modeling_utils.py:104-126](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_utils.py#L104-L126) `duplicate_kv_weight`：GQA 下按 `reps = tp_size // num_kv_heads` 复制 KV 权重。
- [modeling_utils.py:998-1004](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_utils.py#L998-L1004) `filter_weights(prefix, weights)`：按前缀裁出某模块的权重并去掉前缀。

OPT 的自定义 `load_weights` 是「前缀替换 + 融合」的最佳教材：

- [modeling_opt.py:240-298](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/llm-api/out_of_tree_example/modeling_opt.py#L240-L298) 注意三处：① `params_map` 把 `qkv_proj` 映射到 `[q_proj,k_proj,v_proj]`、把 `o_proj` 映射到 `[out_proj]`（OPT 的输出投影叫 `out_proj`）；② 对 `k_proj`/`v_proj` 调 `duplicate_kv_weight`；③ `name.replace('model', weight_prefix, 1)` 处理前缀差异。OPT 还**复用了基类同名的 `filter_weights` / `duplicate_kv_weight`**（从 modeling_utils 导入，见 [modeling_opt.py:11-14](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/llm-api/out_of_tree_example/modeling_opt.py#L11-L14)），印证了「推荐调用现成工具」。

「消费式加载」是本仓降低峰值显存的关键机制。`ConsumableWeightsDict` 包装权重字典，加载完一个模块就 `mark_consumed` 删掉对应键：

- [base_weight_loader.py:11-23](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/checkpoints/base_weight_loader.py#L11-L23) 类注释：边加载边删除权重，降低峰值内存，且线程安全。
- [base_weight_loader.py:73-91](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/checkpoints/base_weight_loader.py#L73-L91) `mark_consumed(prefix)` 删除所有以 `prefix.` 开头的键。自定义 `load_weights` 时应像 DeepSeekV3 那样在各分支末尾调用它（如 [modeling_deepseekv3.py:450-451](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_deepseekv3.py#L450-L451)）。

加载完成后，`ModelLoader` 会跑 `post_load_weights` 钩子链，做跨层 alias、一次性张量变换、派生状态重算：

- [model_loader.py:656-658](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/model_loader.py#L656-L658) `self._call_load_weights(model.load_weights, weights, self.weight_mapper)` 的调用点。
- [model_loader.py:1227-1232](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/model_loader.py#L1227-L1232) `_walk_full_post_load` 遍历模块树调用各自的 `post_load_weights()`。
- [modeling_utils.py:713-726](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_utils.py#L713-L726) 默认 `post_load_weights` = `setup_aliases` + `transform_weights` + `cache_derived_state` 三步。模型可通过覆盖这些钩子挂入跨层逻辑（DeepSeekV3 的 `setup_aliases` 就是一例，见 [modeling_deepseekv3.py:2026-2061](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_deepseekv3.py#L2026-L2061)，它把 `next_layer_layernorm` 指向下一层的 `input_layernorm`，并挂 NVFP4 融合 scale）。

#### 4.3.4 代码实践

**实践目标**：判断一个新模型能否「白嫖」默认 `load_weights`，还是必须自定义。

1. 拿到你目标模型的 HF checkpoint，用 `safetensors` 列出所有权重键名（示例命令，**待本地验证**环境是否有 `safetensors` CLI）：
   ```bash
   # 示例命令，待本地验证
   python -c "from safetensors import safe_open; f=safe_open('model.safetensors','pt'); print('\n'.join(list(f.keys())[:30]))"
   ```
2. 对照你在 4.2 写的模块树名，逐项核对：
   - 是否是 `q_proj`/`k_proj`/`v_proj` 三件套？（是 → 默认 `params_map` 能处理）
   - 是否有非标准名字（如 OPT 的 `out_proj`、DeepSeekV3 的 `kv_a_proj_with_mqa`）？（是 → 需自定义）
   - 前缀是否一致？（`model.layers...` vs `decoder.layers...`）
3. **需要观察的现象**：若全部命中默认 `params_map` 且前缀一致，你**可以不写** `load_weights`，直接继承基类即可。
4. **预期结果**：产出一张「checkpoint 键名 ⟷ 模块名 ⟷ 是否需自定义」的三列表，作为要不要写 `load_weights` 的决策依据。

#### 4.3.5 小练习与答案

**练习 1**：为什么 DeepSeekV3 要把 `load_weights` 委托给一个单独的 `DeepseekV3WeightLoader` 类（[modeling_deepseekv3.py:161-169](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_deepseekv3.py#L161-L169)），而 OPT 直接写在 `ForCausalLM` 里？
**答案**：DeepSeekV3 的 MLA 注意力 + MoE + FP4 量化带来大量特殊的权重重排/反量化/重命名逻辑（见其 `requantize_weight_with_new_scale`、`load_kv_b_proj_and_k_b_proj_trans` 等内嵌函数），代码量大，抽成独立类便于组织与测试；OPT 逻辑简单，写在类内即可。这是工程取舍，不是硬性要求。

**练习 2**：`mark_consumed` 不调用会怎样？
**答案**：功能上不影响正确性（权重仍会加载成功），但会**抬高峰值显存**——所有权重会一直留在字典里直到加载结束。大模型上容易 OOM。DeepSeekV3 在每个分支都显式调用（如 [modeling_deepseekv3.py:450-451](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_deepseekv3.py#L450-L451)）正是为此。

---

### 4.4 注册（Model Registration）

#### 4.4.1 概念说明

第四步是**注册**：让你的 `ForCausalLM` 类进入 `MODEL_CLASS_MAPPING`，这样自动发现（u5-l2）才能查到它。本仓的注册方式极其轻量——**一个装饰器**：

```python
@register_auto_model("MyModelForCausalLM")
class MyModelForCausalLM(DecoderModelForCausalLM[MyModel, MyConfig]):
    ...
```

装饰器在 `import` 这个模块时执行，往字典里塞一条 `{name: cls}`。所以注册的生效条件就是「**这个模块被 import 到**」。

由此自然分出两种注册方式：

| 方式 | 文件放哪 | 如何被 import | 是否改 TRT-LLM 源码 |
|------|----------|---------------|---------------------|
| **in-tree（仓内）** | `tensorrt_llm/_torch/models/modeling_mymodel.py` | 在 `_torch/models/__init__.py` 里加一行 import | 是 |
| **out-of-tree（仓外）** | 你的工作目录 | 在你的脚本里 `import modeling_mymodel` | 否 |

#### 4.4.2 核心流程

```
@register_auto_model(name) 装饰 ForCausalLM 类
        │
        │  模块被 import 时执行装饰器
        ▼
MODEL_CLASS_MAPPING[name] = ForCausalLM 类
        │
        │  用户加载模型
        ▼
AutoModelForCausalLM._resolve_class  →  MODEL_CLASS_MAPPING.get(architectures[0])
        │
        ▼
from_config(config)  →  cls(config)  实例化
```

两种注册方式的差异**只在「如何让模块被 import」这一步**，装饰器本身完全一样。

#### 4.4.3 源码精读

装饰器实现极其简短：

- [modeling_utils.py:814-828](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_utils.py#L814-L828) `MODEL_CLASS_MAPPING = {}` 与 `register_auto_model(name)`：一个返回装饰器的闭包，把 `cls` 塞进字典后原样返回 `cls`。装饰器**不改变类**，只做注册副作用。

**in-tree 方式**——把模型纳入核心发行。指南要求在 `__init__.py` 里加 import：

- [adding_new_model.md:171-182](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/adding_new_model.md#L171-L182) 在 `_torch/models/__init__.py` 加 `from .modeling_mymodel import MyModelForCausalLM` 并加入 `__all__`。
- [__init__.py:17](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/__init__.py#L17) 真实例子：`from .modeling_deepseekv3 import DeepseekV3ForCausalLM`，并在 [__init__.py:69-133](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/__init__.py#L69-L133) 的 `__all__` 里登记。由于 `import tensorrt_llm` 会链式 import 这个 `__init__.py`（u2-l2 讲过），所以**用户什么都不用做**就自动注册了。

**out-of-tree 方式**——不改 TRT-LLM 源码。指南与示例：

- [adding_new_model.md:184-203](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/adding_new_model.md#L184-L203) 把 `modeling_mymodel.py` 放工作目录，脚本里 `import modeling_mymodel` 再用 `LLM(...)`，并给出可运行命令 `python examples/llm-api/out_of_tree_example/main.py`。
- [main.py:1-23](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/llm-api/out_of_tree_example/main.py#L1-L23) 关键的第一行 `import modeling_opt  # noqa`：这一句触发 `@register_auto_model("OPTForCausalLM")` 执行，注册完成后 `LLM(model='facebook/opt-125m')` 才能查到 `OPTForCausalLM`。删掉这一行，示例会报 `Unknown architecture`。

**多别名注册**——一个类服务多个架构名。装饰器可叠加：

- [modeling_deepseekv3.py:1890-1894](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_deepseekv3.py#L1890-L1894) `DeepseekV3ForCausalLM` 同时注册了 `GlmMoeDsaForCausalLM`、`DeepseekV32ForCausalLM`、`DeepseekV3ForCausalLM` 三个名字——这正是 u5-l2 提到的「一个类、多个架构别名」，让同一份实现服务 DeepSeek-V3 / V3.2 / GLM-5 家族。

**配套的模型默认值钩子**——注册后还可以让模型提供「开箱即用」的默认参数（u4-l2 讲过合并优先级）：

- [modeling_utils.py:608-661](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_utils.py#L608-L661) `get_model_defaults`（返回覆盖默认值的字典）与 `get_preferred_transceiver_runtime`（声明分离式服务的 transceiver 偏好）。
- [modeling_deepseekv3.py:1896-1918](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_deepseekv3.py#L1896-L1918) GLM-5 家族通过覆盖 `get_preferred_transceiver_runtime` 选择 Python transceiver 的真实例子——一个被多个架构名共用的类，靠 `pretrained_config.architectures` 区分不同 checkpoint。

#### 4.4.4 代码实践

**实践目标**：亲手跑通 out-of-tree 注册，直观感受「import 即注册」。

1. 进入示例目录：`cd examples/llm-api/out_of_tree_example`（**待本地验证**是否有 GPU 与 `facebook/opt-125m` 权重下载条件）。
2. 运行 `python main.py`，观察正常输出三段生成文本。
3. 把 [main.py:1](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/llm-api/out_of_tree_example/main.py#L1) 的 `import modeling_opt  # noqa` 注释掉再跑。
4. **需要观察的现象**：第 3 步会抛 `Unknown architecture for AutoModelForCausalLM: OPTForCausalLM`（来自 [modeling_auto.py:60-63](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_auto.py#L60-L63)），因为 `OPTForCausalLM` 从未被注册进 `MODEL_CLASS_MAPPING`。
5. **预期结果**：建立「`@register_auto_model` 的副作用依赖模块被 import」这一因果链的肌肉记忆。

> 若无 GPU 或无法下载权重，可把第 2-3 步降级为「源码阅读型实践」：在 `main.py` 的 `import modeling_opt` 处打断点（或在其后 `print(MODEL_CLASS_MAPPING.get("OPTForCausalLM"))`），确认注册是否发生即可，标注「待本地验证」运行结果。

#### 4.4.5 小练习与答案

**练习 1**：in-tree 与 out-of-tree 注册，装饰器写法有区别吗？
**答案**：没有。两者都用 `@register_auto_model(name)`。区别只在「让模块被 import」的方式：in-tree 靠在 `_torch/models/__init__.py` 加 import（用户无感），out-of-tree 靠用户脚本里显式 `import modeling_xxx`。

**练习 2**：为什么 DeepSeekV3 要叠三个 `@register_auto_model`？
**答案**：同一份模型实现（含 MLA + MoE）要服务 DeepSeek-V3、V3.2、GLM-5 家族，这些 checkpoint 的 `architectures` 字段各不相同。叠多个装饰器让一个类在 `MODEL_CLASS_MAPPING` 里注册多个键，避免为每个架构名复制一份代码。

---

## 5. 综合实践

把四步串成一个完整任务：**为假想的 `MyModel` 起草一份最小 out-of-tree 实现**。

**任务背景**：假设 `MyModel` 是一个标准 pre-norm、GQA 的 decoder 模型，已在 HF transformers 里（有 `MyConfig` 和 `architectures: ["MyModelForCausalLM"]`）。

**操作步骤**：

1. **新建工作目录** `mymodel_demo/`，创建 `modeling_mymodel.py` 和 `run.py`。
2. **配置**（对应 4.1）：在文件头 `from transformers import MyConfig`。
3. **模型定义**（对应 4.2）：照 [adding_new_model.md:47-105](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/adding_new_model.md#L47-L105) 的骨架写四个类。单层 forward 用 pre-norm 顺序：`residual=h; h=input_layernorm(h); h=self_attn(...); h=residual+h; residual=h; h=post_attention_layernorm(h); h=mlp(h); h=residual+h; return h`。
4. **权重加载**（对应 4.3）：先**尝试不写** `load_weights`，依赖基类默认的 `qkv_proj`/`gate_up_proj` 融合与 KV 复制。只有当 checkpoint 键名非标准时，再仿照 [modeling_opt.py:240-298](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/llm-api/out_of_tree_example/modeling_opt.py#L240-L298) 写一个。
5. **注册**（对应 4.4）：在 `MyModelForCausalLM` 上加 `@register_auto_model("MyModelForCausalLM")`。
6. **入口脚本** `run.py` 仿照 [main.py:1-23](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/llm-api/out_of_tree_example/main.py#L1-L23)：
   ```python
   import modeling_mymodel  # noqa  ← 触发注册
   from tensorrt_llm import LLM, SamplingParams
   llm = LLM(model="<your-mymodel-checkpoint>")
   print(llm.generate(["Hello, my name is"]))
   ```

**验收清单**：

- [ ] `MyModelForCausalLM` 的 `__init__` 只有一句 `super().__init__(...)`。
- [ ] `MyModel.forward` 是「embed → 逐层 → norm」三段。
- [ ] 装饰器名与 checkpoint 的 `architectures[0]` 完全一致。
- [ ] 能说清自己**为什么写了 / 没写** `load_weights`。
- [ ] 能回答：若把它改成 in-tree 注册，需要在哪两个文件做什么改动？（答：把 `modeling_mymodel.py` 移到 `_torch/models/`，并在 [__init__.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/__init__.py#L17) 加一行 import + 在 `__all__` 登记。）

> 本任务无需真实权重即可完成骨架与注册逻辑的编写；端到端跑通需要 GPU 与对应 checkpoint，标注「待本地验证」。

## 6. 本讲小结

- 添加新模型是**四步流程**：配置 → 模型定义 → 权重加载 → 注册，前两步决定「形状与计算」，后两步决定「权重对得上、能被找到」。
- **配置**直接复用 HF 的 `PretrainedConfig` 子类；`config.json` 的 `architectures[0]` 是贯穿全流程的「钥匙」，必须与注册名严格一致。
- **模型定义**写四个类（`Attention` / `DecoderLayer` / `DecoderModel` / `DecoderModelForCausalLM`），尽量复用基类与高性能模块（`Linear`/`Embedding`/`RMSNorm`），只写模型特有逻辑；注意 `attn_metadata` 契约与 packed 输入。
- **权重加载**优先用基类默认实现（已自动处理 qkv/gate_up 融合与 GQA KV 复制）；不匹配时再覆盖，并尽量调用模块级 `load_weights`、用 `ConsumableWeightsDict.mark_consumed` 降峰值显存。
- **注册**只需一个 `@register_auto_model` 装饰器，副作用是「import 即注册」；由此分出 in-tree（改 `__init__.py`，用户无感）与 out-of-tree（用户脚本里显式 import，不改源码）两种方式，装饰器写法完全相同。
- 装饰器可叠加实现**多别名注册**（如 DeepSeekV3 服务 V3/V3.2/GLM-5），并可通过 `get_model_defaults` / `get_preferred_transceiver_runtime` 提供模型默认值。

## 7. 下一步学习建议

- **想看一个真实模型的完整四步**：精读 [modeling_llama.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_llama.py)（`LlamaDecoderLayer` 在 L659、`LlamaModel` 在 L1030、`@register_auto_model("LlamaForCausalLM")` 在 L1134），它是介于 OPT（极简）与 DeepSeekV3（复杂）之间的最佳「中等复杂度」范本。
- **要加多模态模型**：阅读 `trtllm-model-onboard-multimodal` 技能（VLM 需要 vision encoder + input processor + weight mapper，超出本讲范围），并看 `register_vision_encoder` 装饰器（[modeling_utils.py:831-861](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_utils.py#L831-L861)）。
- **要让模型跑得更快**：进入 u6（注意力后端）与 u10（量化 / MoE / CUDA Graph），理解你在 4.2 替换的 `Linear`/`Attention` 背后的高性能实现。
- **贡献回上游**：若打算走 in-tree 并提 PR，先读 u12-l4（测试 / CI / 贡献流程）与 `CODING_GUIDELINES.md`，按 DCO、PR 标题、版权头规范提交。
