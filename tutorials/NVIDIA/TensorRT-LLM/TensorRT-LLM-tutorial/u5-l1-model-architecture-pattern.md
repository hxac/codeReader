# 模型架构范式：Config + ForCausalLM

## 1. 本讲目标

本讲是「模型与注册机制」单元的第一讲，承接 u4-l3 里建立的 `ModelConfig` / `PretrainedConfig` 概念，回答一个最根本的问题：

> 在 PyTorch 后端里，一个具体的模型（比如 Llama）到底是怎么写出来的？它和框架之间遵循怎样的「契约」？

读完本讲，你应该能够：

1. 说出 **`DecoderModelForCausalLM`** 这个基类承担了哪些「所有解码器语言模型都共有」的职责（lm head、logits、权重加载、流水线切分、投机解码挂载点）。
2. 看懂任意一个 `modeling_xxx.py` 的三段式骨架：**embedding → decoder layer 循环 → lm head**，并知道这三段分别落在哪两个基类上。
3. 解释 **`DecoderModel`**（本仓库中充当「Decoder/骨干」角色的类）如何驱动逐层前向，以及它与抽象层 `DecoderLayer` 的关系。
4. 区分「基类提供的能力」与「子类必须覆盖的部分」，从而为 u5-l3「添加一个新模型」打下基础。

> 重要澄清：本仓库 `_torch/` 下**并没有**一个字面叫 `Decoder` 的类。本讲的「Decoder 模块」指的是 [tensorrt_llm/_torch/models/modeling_utils.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py) 里的 **`DecoderModel`**——也就是「embedding + layers + final norm」这一层骨干。这是你阅读源码时最容易踩的命名坑，本讲会反复强调。

## 2. 前置知识

在进入源码前，先用最朴素的语言把几个概念对齐：

- **Transformer 解码器（decoder-only）**：现代大模型（Llama、Qwen、DeepSeek 等）几乎都是 decoder-only 架构。一层解码器大致长这样：输入 `hidden_states` 先过 `input_layernorm`，进 `self_attn`（自注意力），残差相加；再过 `post_attention_layernorm`，进 `mlp`（前馈网络），再残差相加。
- **残差连接（residual）**：`output = x + Sublayer(x)`。把一条贯穿整层的「残差流」单独拎出来传递，可以省一次内存读写，也方便把 norm 和 allreduce 融合在一起。本仓库的层普遍以 `(hidden_states, residual)` 二元组在层与层之间传递。
- **RMSNorm**：LayerNorm 的简化版，不减均值，只用均方根归一化：

  \[
  \mathrm{RMSNorm}(x) = \frac{x}{\sqrt{\dfrac{1}{H}\sum_{i=1}^{H} x_i^2 + \epsilon}} \cdot \gamma
  \]

  本仓库的 `RMSNorm` 支持一种**融合调用**：传入 `residual` 时，它先做 `residual = residual + hidden`，再对新的 residual 做 RMSNorm，一次返回 `(norm_out, residual)`。这正是层里能写出 `post_attention_layernorm(hidden_states, residual)` 的原因。
- **`nn.Module` + 泛型（`Generic`）**：PyTorch 的所有模型都是 `nn.Module` 的子类。本仓库还大量使用 `Generic[TModel, TConfig]`，把「骨干模型类型」和「HF 配置类型」作为类型参数挂到类上，方便 IDE 与类型检查。
- **元类（metaclass）**：Python 里「用来造类的类」。本仓库用元类 `PostInitCaller` 在对象创建后自动调用 `__post_init__` 和 `__pp_init__`，是一处非常关键、也非常容易看漏的设计，本讲 4.1 会专门拆解。
- **ModelConfig**：u4-l3 讲过，`ModelConfig[TConfig]` 是运行时包裹层，内部 `pretrained_config` 才是 HF 的 `LlamaConfig` 之类。本讲会看到 `model_config.pretrained_config.hidden_size` 这种访问方式。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [tensorrt_llm/_torch/models/modeling_utils.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py) | 定义三个最关键的基类：`DecoderModel`（骨干）、`DecoderModelForCausalLM`（语言模型外壳），以及元类 `PostInitCaller` / `PPInitCaller`；还定义模型注册表 `MODEL_CLASS_MAPPING` 与装饰器 `register_auto_model`。 |
| [tensorrt_llm/_torch/modules/decoder_layer.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/modules/decoder_layer.py) | 定义单层的抽象基类 `DecoderLayer`，只规定 `forward` 的签名，并提供一个流水线用的 `skip_forward`。 |
| [tensorrt_llm/_torch/models/modeling_llama.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_llama.py) | Llama 的具体实现：`LlamaModel`、`LlamaDecoderLayer`、`LlamaAttention`、`LlamaForCausalLM`。是我们剖析「子类如何填充骨架」的主样本。 |

辅助文件（不精读，仅引用）：[tensorrt_llm/_torch/models/modeling_speculative.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_speculative.py) 里的 `SpecDecOneEngineForCausalLM` 是 `LlamaForCausalLM` 的直接父类（它本身又继承 `DecoderModelForCausalLM`）。

## 4. 核心概念与源码讲解

### 4.1 DecoderModelForCausalLM 基类的职责

#### 4.1.1 概念说明

一个「用于因果语言建模（Causal LM）」的模型，无论具体是 Llama 还是 Qwen，对外都要做同一件事：吃 `input_ids`，吐 `logits`（词表上的概率分布）。这件事可以拆成两半：

1. **骨干（backbone）**：把 token id 变成 hidden states，再逐层精炼。这一半是模型特有的，每个架构都不一样。
2. **语言模型头（lm head）+ logits**：把最后一层 hidden states 投影回词表大小，再交给采样器。这一半几乎对所有模型都一样——只是个矩阵乘法 + 可选的 gather。

`DecoderModelForCausalLM` 就是那个**「对所有模型都一样」的外壳**。它的设计哲学是：

> 「我自己不知道你长什么样，你把你的骨干塞给我（构造时传入 `model`），我负责给你套上 lm head、算 logits、处理权重加载和量化配置、挂上流水线/投机解码。」

所以子类要做的非常少：基本只需要「构造自己的骨干」并把它交给基类。

#### 4.1.2 核心流程

`DecoderModelForCausalLM` 的一生由元类 `PostInitCaller` 编排，顺序是：

```text
PostInitCaller.__call__                      # 元类：造对象
   │
   ├─ type.__call__ → 运行 __init__           # ① 建 lm_head / logits_processor，
   │                                           │   并在子类构造的骨干里建好所有权重
   │
   ├─ obj.__post_init__()                     # ② 应用逐层量化配置、排除模块、create_weights
   │
   └─ obj.__pp_init__()                       # ③ 流水线并行：切层、删掉不归本 rank 的权重
```

这套顺序解决了一个很现实的问题：**权重创建非常吃显存**。如果先把所有层、所有 rank 的权重都建出来再删，容易 OOM。因此本仓库用 `MetaInitMode`（meta 设备）先「占位」创建，等 `__pp_init__` 确定哪些权重真正需要后，才真正分配显存。源码注释把这件事说得很清楚（见 4.1.3）。

运行期（推理时）的流程就简单多了：

```text
forward(input_ids, attn_metadata, ...)
   │
   ├─ output = self.model(input_ids, ...)          # 调骨干，拿到最后一层 hidden states
   │
   └─ return self.logits_processor.forward(        # lm_head 投影 → logits
          output, self.lm_head, attn_metadata, ...)
```

#### 4.1.3 源码精读

**类定义与泛型。** 注意它继承 `nn.Module`，并用 `Generic[TModel, TConfig]` 把「骨干类型」和「配置类型」参数化；元类是 `PostInitCaller`：

[tensorrt_llm/_torch/models/modeling_utils.py:377-379](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L377-L379) —— 类声明，`Generic[TModel, TConfig]` 把骨干与配置类型绑到类上。

**元类 `PostInitCaller`：自动串联三段初始化。** 这是理解整个模型加载流程的钥匙：

[tensorrt_llm/_torch/models/modeling_utils.py:359-371](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L359-L371) —— 在标准 `type.__call__` 之后，自动调用 `__post_init__()` 和 `__pp_init__()`，注释解释了「先在 meta 上建权重、再删冗余、最后才分配」的省显存策略。

> 对比：骨干类 `DecoderModel` 用的是另一个元类 [PPInitCaller](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L231-L235)（line 231-235），它**不**自动调 `__post_init__`。真正的「三段串联」只在外壳 `DecoderModelForCausalLM` 创建时触发，再由外壳的 `__pp_init__` 反过来调用 `self.model.__pp_init__()`（见下文）。

**构造函数：收下骨干，搭好 lm head。** 关键是 `self.model = model`（你的骨干），以及根据并行拓扑、是否 tie embedding、是否有 LoRA 自定义头等条件，构造 `self.lm_head`：

[tensorrt_llm/_torch/models/modeling_utils.py:381-385](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L381-L385) —— 构造签名：`def __init__(self, model, *, config, hidden_size, vocab_size)`，骨干 `model` 是位置参数，其余为关键字参数。

[tensorrt_llm/_torch/models/modeling_utils.py:448-459](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L448-L459) —— 常见的 TP 分支下 `lm_head` 的构造：按 `COLUMN` 模式做张量并行、可选 gather_output、带上量化配置。`lm_head` 本身是 [LMHead](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/modules/embedding.py)（一个 `Linear` 子类）。

[tensorrt_llm/_torch/models/modeling_utils.py:473-480](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L473-L480) —— **权重共享（tie_word_embeddings）**：若开启，让 `lm_head.weight` 直接复用 `embed_tokens.weight`，这是 Llama 等很多模型的常见设定。

[tensorrt_llm/_torch/models/modeling_utils.py:482-485](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L482-L485) —— 建 `logits_processor`，并初始化 `prologue/epilogue`（流水线用的「前奏/尾声」模块列表，`epilogue = [self.lm_head]`）。

**前向：骨干 + lm head 的两步。** 这是整个外壳最核心的两行：

[tensorrt_llm/_torch/models/modeling_utils.py:740-754](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L740-L754) —— `output = self.model(...)` 调骨干，`return self.logits_processor.forward(output, self.lm_head, ...)` 投影成 logits。**整个前向没有一行模型特有逻辑**——这正是基类能复用的原因。

**量化与权重加载的公共能力。** 基类还承担了「把逐层量化配置刷到每个 Linear/MoE/Attention 上」「按 `exclude_modules` 跳过量化」以及通用的权重加载：

[tensorrt_llm/_torch/models/modeling_utils.py:600-606](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L600-L606) —— `__post_init__`：先刷量化配置，再遍历所有模块调 `create_weights()`。

[tensorrt_llm/_torch/models/modeling_utils.py:756-780](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L756-L780) —— `load_weights`：根据是否传入 `weight_mapper` 走两条加载实现，子类通常直接复用。

**模型默认值（连回 u4-l2）。** 基类提供了一个类方法 `get_model_defaults`，子类可覆盖它来给出「开箱即用」的优化默认值：

[tensorrt_llm/_torch/models/modeling_utils.py:608-634](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L608-L634) —— 默认返回 `{}`；返回的字典会被「深度合并」进用户的 `llm_args`，且**用户显式设置永远优先**。这正是 u4-l2 讲的「框架默认 < 模型默认 < 用户显式」三级合并里的「模型默认」来源。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「外壳 `DecoderModelForCausalLM` 不含任何模型特有逻辑」这件事。

**操作步骤**（源码阅读型）：

1. 打开 [tensorrt_llm/_torch/models/modeling_utils.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py)，定位 `DecoderModelForCausalLM.forward`（约 728 行起）。
2. 在 `forward` 方法体内搜索任何「Llama / Qwen / hidden_size 具体数值」之类的字样。
3. 同样检查 `__post_init__`、`load_weights`、`__pp_init__`，看它们是否对具体模型名做判断。

**需要观察的现象**：

- `forward` 里只会出现 `self.model(...)` 和 `self.logits_processor.forward(...)`，**没有任何模型特有分支**。
- `load_weights` / `__post_init__` 都是「遍历 `named_modules()` 做通用处理」，与具体架构无关。

**预期结果**：你会确信「换一个模型，外壳一行都不用改」——所有差异都在子类塞进来的骨干 `self.model` 里。

#### 4.1.5 小练习与答案

**练习 1**：`DecoderModelForCausalLM.__init__` 为什么要接收 `hidden_size` 和 `vocab_size`，而不是直接从 `config` 里读？

**参考答案**：因为有些场景下词表大小会被改写——比如 LoRA 自定义 `lm_head` 时，`vocab_size` 来自 LoRA checkpoint 而非原始 `config`（见 [modeling_utils.py:438-441](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L438-L441)）。把这两个值作为显式参数传入，让外壳能灵活应对「实际词表 ≠ 配置词表」的情况。

**练习 2**：`PostInitCaller` 和 `PPInitCaller` 两个元类的区别是什么？为什么骨干类只用后者？

**参考答案**：`PostInitCaller` 会在对象创建后额外调用 `__post_init__`（刷量化、建权重）和 `__pp_init__`（切层）；`PPInitCaller` 不做这些（见 [modeling_utils.py:231-235](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L231-L235) vs [359-371](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L359-L371)）。骨干类单独构造时（比如投机解码里单独建 draft 骨干）不需要这套串联；真正的串联只在外壳创建时触发一次，再由外壳的 `__pp_init__` 调用 `self.model.__pp_init__()` 把切层逻辑传给骨干。

---

### 4.2 DecoderModel：驱动逐层前向的「Decoder」骨干

#### 4.2.1 概念说明

再次强调命名：本仓库里充当「Decoder」角色的是 **`DecoderModel`**。它是 `DecoderModelForCausalLM` 的「里子」——`self.model` 指向的就是它。

`DecoderModel` 定义了「一个 decoder-only 骨干长什么样」的最小契约。看它的类级类型注解就能一目了然：

```python
class DecoderModel(nn.Module, metaclass=PPInitCaller):
    config: ModelConfig
    embed_tokens: Embedding
    layers: nn.ModuleList
    norm: RMSNorm
```

也就是说，**任何 decoder-only 骨干都必须提供这四样东西**：token embedding、一层层的 decoder layer、最终的 norm，外加配置。这套契约极简，但足以表达从 Llama 到 DeepSeek 的所有主流架构——区别只在于「`layers` 里塞的是哪种 `DecoderLayer`」。

#### 4.2.2 核心流程

`DecoderModel.forward` 的骨架是所有 decoder-only 模型的「模板代码」：

```text
forward(input_ids 或 inputs_embeds):
    1. 若给的是 input_ids：hidden = embed_tokens(input_ids)
       若给的是 inputs_embeds：直接用（多模态场景）
    2. for layer in self.layers:
           hidden = layer(position_ids, hidden, attn_metadata, ...)
    3. hidden = self.norm(hidden)        # 最终 RMSNorm
    4. return hidden
```

注意：**骨干只输出 hidden states，不算 logits**。logits 是外壳 `DecoderModelForCausalLM` 的职责。这种「骨干算到 norm 为止，外壳接 lm head」的分层，让同一个骨干能被复用到不同任务（语言模型、draft 模型等）。

> 这一层 forward 不显式传 `residual`（残差在每一层内部自己管理并返回）。而 Llama 的 `LlamaModel` 会覆盖这个 forward，加上显式的 `residual` 流与 `skip_norm` 优化（见 4.3 与综合实践）。

#### 4.2.3 源码精读

**类定义与最小契约。**

[tensorrt_llm/_torch/models/modeling_utils.py:238-250](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L238-L250) —— `DecoderModel` 声明与 `__init__`。注意类级注解 `embed_tokens / layers / norm` 只是「约定子类必须赋值」，基类 `__init__` 只设了 `dtype`、`model_config`、`prologue/epilogue`、`keep_embed_tokens`，**并没有**真正建 embed/layers/norm——这些留给具体子类（如 `LlamaModel`）按各自配置去建。

**逐层前向的模板。**

[tensorrt_llm/_torch/models/modeling_utils.py:252-281](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L252-L281) —— 这是本讲最重要的一段。三步清晰可见：

- 第 266-267 行：`inputs_embeds = self.embed_tokens(input_ids)`（embedding）。
- 第 271-277 行：`for decoder_layer in self.layers: hidden_states = decoder_layer(...)`（逐层）。
- 第 279 行：`hidden_states = self.norm(hidden_states)`（最终 norm）。

> 这 30 行就是「Decoder 驱动逐层前向」的全部真相。后面 Llama 的实现只是在这之上加了残差流和跨层 layernorm 链。

**流水线并行：骨干的 `__pp_init__`。** 当 `mapping.has_pp()` 为真时（多卡流水线），骨干负责把 `layers` 按 rank 切分，并给首/尾层的 `forward` 套上「接收上一阶段 / 发往下一阶段」的通信包装：

[tensorrt_llm/_torch/models/modeling_utils.py:283-346](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L283-L346) —— `__pp_init__`：对不归本 rank 的层调用 `skip_forward`（见 4.3），对首层包 `forward_after_recv`、对尾层包 `forward_before_send`。这条逻辑在 u9（分布式与并行）会展开，本讲只需知道「骨干管切层，外壳管 lm head 的 prologue/epilogue」。

#### 4.2.4 代码实践

**实践目标**：验证「`DecoderModel` 提供模板，子类填充具体 embed/layers/norm」。

**操作步骤**：

1. 在 [modeling_llama.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_llama.py) 中找到 `class LlamaModel(DecoderModel)`（约 1030 行）。
2. 对照基类注解 `embed_tokens / layers / norm`，看 `LlamaModel.__init__` 是否逐一给这三者赋值。

**需要观察的现象**：`LlamaModel.__init__` 里会看到 `self.embed_tokens = Embedding(...)`、`self.layers = nn.ModuleList([LlamaDecoderLayer(...)])`、`self.norm = RMSNorm(...)`，正好填满基类契约。

**预期结果**：基类契约的三个字段被完整赋值；这就是「子类填充骨架」的具体表现。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DecoderModel.forward` 允许 `input_ids` 和 `inputs_embeds` 二选一，且不能同时给？

**参考答案**：见 [modeling_utils.py:261-264](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L261-L264) 的断言。纯文本场景给 `input_ids`（内部做 embedding）；多模态场景（如 Llama4 把图像 embedding 融进去）由上层算好 `inputs_embeds` 直接传入。二者同时给会语义冲突，所以用 `^`（异或）强校验。

**练习 2**：基类 `DecoderModel.__init__` 为什么不直接建 `embed_tokens/layers/norm`？

**参考答案**：因为这些的尺寸和具体类型（用哪种 Attention、用不用 MoE、TP 怎么切）完全取决于具体模型配置。基类只定契约，把「怎么建」的决定权下放给子类，符合「公共骨架 + 模型特有细节」的分工。

---

### 4.3 decoder_layer：`DecoderLayer` 抽象与 `LlamaDecoderLayer`

#### 4.3.1 概念说明

最细的一层是「单个 decoder layer」。本仓库用一个极简的抽象基类 `DecoderLayer` 来定义「一层该长什么样」，它只做两件事：

1. 用 `@abstractmethod` 规定 `forward` 的签名：必须接受 `(position_ids, hidden_states, attn_metadata, residual=..., **kwargs)`，返回 `hidden_states` 或 `(hidden_states, residual)`。
2. 提供一个 `skip_forward`：在流水线并行时，让「不归本 rank 的层」变成 no-op（直接把输入原样返回）。

这个抽象之所以重要，是因为**整个 `__pp_init__` 切层机制都依赖它**——基类需要能对任意一层调用 `skip_forward`，而不管它是 Llama 还是 DeepSeek 的层。`DecoderLayer` 就是那个「公共类型」。

`LlamaDecoderLayer` 是它的具体实现，把「Llama 的一层」具体化为：`input_layernorm → LlamaAttention → post_attention_layernorm(+残差) → GatedMLP → 残差`，并叠加了大量 allreduce 融合优化。

#### 4.3.2 核心流程

一层 Llama decoder 的前向（去掉融合优化的简化版）：

```text
forward(position_ids, hidden_states, attn_metadata, residual):
    if residual is None: residual = hidden_states
    if not skip_input_layernorm:
        hidden = input_layernorm(hidden_states)
    hidden = self_attn(position_ids, hidden, attn_metadata, ...)      # 自注意力
    hidden, residual = post_attention_layernorm(hidden, residual)     # 融合：norm + 残差相加
    hidden = mlp(hidden, ...)                                          # 前馈
    hidden, residual = next_layer_layernorm(hidden, residual)          # 下一层的 norm 提前算（链式）
    return hidden, residual
```

这里有两个「不那么显然」的设计，值得专门解释：

- **残差流显式传递**：`residual` 不是每层自己存，而是作为参数在层与层之间流动。`post_attention_layernorm(hidden, residual)` 这种「双参数」调用，正是 4.2 里提到的 RMSNorm 融合路径——它内部做 `residual = residual + hidden` 再 norm，省一次 kernel。
- **跨层 layernorm 链（`next_layer_layernorm`）**：本层结尾顺便把「下一层的 input_layernorm」也算掉，并让下一层用 `skip_input_layernorm=True` 跳过。这把「allreduce + 残差 + norm」三件事融合成一次通信，是 TP 场景下的关键性能优化。这条链由 `LlamaForCausalLM.setup_aliases` 在加载后「接线」（见 4.3.3 与综合实践）。

#### 4.3.3 源码精读

**抽象基类 `DecoderLayer`。** 全文很短，但信息密度高：

[tensorrt_llm/_torch/modules/decoder_layer.py:10-34](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/modules/decoder_layer.py#L10-L34) —— `forward` 是抽象方法（line 12-21），规定了一层的统一签名；`skip_forward`（line 23-34）是流水线 no-op 的实现，根据是否传 `residual` 决定返回单项还是二元组。

**Llama 一层的构造：组装子模块。**

[tensorrt_llm/_torch/models/modeling_llama.py:680-727](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_llama.py#L680-L727) —— `LlamaDecoderLayer.__init__` 关键赋值：`self.self_attn = LlamaAttention(...)`（line 680）、`self.mlp = GatedMLP(...)`（line 686）、`self.input_layernorm = RMSNorm(...)`（line 705）、`self.post_attention_layernorm = RMSNorm(...)`（line 717）、`self.all_reduce = AllReduce(...)`（line 723），以及两个「待接线」字段 `self.next_layer_layernorm = None` / `self.next_attn = None`（line 726-727）——它们在构造时是空的，等 `setup_aliases` 来连。

**Llama 一层的前向：残差 + 融合。**

[tensorrt_llm/_torch/models/modeling_llama.py:811-825](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_llama.py#L811-L825) —— 残差初始化与 `input_layernorm`、`self_attn` 调用。注意 `skip_input_layernorm` 标志：若被上一层链式接管，这里就跳过。

[tensorrt_llm/_torch/models/modeling_llama.py:858-862](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_llama.py#L858-L862) —— 非融合分支下 `post_attention_layernorm(hidden_states, residual)` 的「双参数」调用，正是 RMSNorm 融合残差的体现。

[tensorrt_llm/_torch/models/modeling_llama.py:873-878](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_llama.py#L873-L878) —— `mlp` 调用。

[tensorrt_llm/_torch/models/modeling_llama.py:930-940](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_llama.py#L930-L940) —— 收尾：若存在 `next_layer_layernorm`，用它（融合 allreduce）算出下一层输入并返回 `(hidden, residual)`；这正是「跨层 norm 链」的落点。

**接线者 `setup_aliases`。** 这些 `next_layer_layernorm` 是怎么被连起来的？答案在 `LlamaForCausalLM.setup_aliases`：

[tensorrt_llm/_torch/models/modeling_llama.py:1143-1153](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_llama.py#L1143-L1153) —— 遍历每一层：非末层把 `layer.next_layer_layernorm` 指向下一层的 `input_layernorm`，并置下一层 `skip_input_layernorm = True`、设置 `next_attn`；末层则指向 `self.model.norm`（最终 norm）并置 `self.model.skip_norm = True`。这是一次「加载后、推理前」的结构性接线，属于基类 `post_load_weights` 调用的 `setup_aliases` 阶段（见 [modeling_utils.py:671-726](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_utils.py#L671-L726)）。

> 补充：`LlamaForCausalLM` 的真实继承链是 `LlamaForCausalLM → SpecDecOneEngineForCausalLM → DecoderModelForCausalLM`（见 [modeling_speculative.py:1894-1895](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_speculative.py#L1894-L1895)）。`SpecDecOneEngineForCausalLM` 在「外壳」基础上加了投机解码的 draft 模型与 spec_worker（u10-l3 会展开）。本讲关注架构范式，可暂时把它视作「增强版外壳」。

#### 4.3.4 代码实践

**实践目标**：亲手跟踪「跨层 norm 链」是如何被接上的，理解 `skip_input_layernorm` 与 `next_layer_layernorm` 的配合。

**操作步骤**：

1. 打开 [modeling_llama.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_llama.py) 的 `LlamaForCausalLM.setup_aliases`（约 1143 行）。
2. 假设有 2 层（`num_hidden_layers=2`），在纸上画出：第 0 层的 `next_layer_layernorm` 指向谁？第 0 层被设置了什么标志？末层（第 1 层）的 `next_layer_layernorm` 又指向谁？
3. 再回到 `LlamaDecoderLayer.forward`（约 802 行），顺着 `skip_input_layernorm` 与 `next_layer_layernorm` 两个分支，验证「第 0 层结尾算的是第 1 层的 input_layernorm，第 1 层开头会跳过它」。

**需要观察的现象**：

- 对 `idx = 0`（非末层）：`layer.next_layer_layernorm = layers[1].input_layernorm`，`layers[1].skip_input_layernorm = True`。
- 对 `idx = 1`（末层）：`layer.next_layer_layernorm = self.model.norm`，`self.model.skip_norm = True`。

**预期结果**：你会看到「norm 被从下一层开头挪到上一层结尾」的整条链路，这是把 allreduce 通信与 norm 融合的关键。若无法在本地跑模型，此项可作纯阅读理解，标注「待本地验证」其性能收益。

#### 4.3.5 小练习与答案

**练习 1**：`DecoderLayer` 是抽象基类，但 `LlamaDecoderLayer.__init__` 里调用的是 `super().__init__()` 而非某个具体父类。这个 `super()` 指向谁？它做了什么？

**参考答案**：指向 [decoder_layer.py:10](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/modules/decoder_layer.py#L10) 的 `DecoderLayer(nn.Module, ABC)`，`super().__init__()` 初始化 `nn.Module` 基类（建立模块注册、hook 等基础设施）。抽象基类本身不存子模块，子模块（`self_attn`/`mlp`/各 norm）由 `LlamaDecoderLayer` 自己赋值。

**练习 2**：`skip_forward` 为什么需要根据 `residual is ...` 分两种返回？这和流水线并行有什么关系？

**参考答案**：见 [decoder_layer.py:23-34](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/modules/decoder_layer.py#L23-L34)。一个「不归本 rank 的层」要变成 no-op，但调用方期望的返回形状可能带 `residual` 也可能不带（取决于这一层在不在残差链上）。`skip_forward` 按是否传 `residual` 决定返回单项或二元组，保证 no-op 层的返回形状与真实层一致，从而让流水线切分对调用方透明。

## 5. 综合实践

把三个最小模块串起来，完成本讲的主任务：

> **阅读 `modeling_llama.py`，对照 `DecoderModelForCausalLM` 基类，列出 `LlamaForCausalLM` 复用了哪些基类能力、又覆盖了什么；并标注「Decoder」类（即 `DecoderModel` 的具体子类）所在位置。**

建议按以下步骤产出一张「复用 vs 覆盖」对照表：

1. **定位继承链**。确认 `LlamaForCausalLM(SpecDecOneEngineForCausalLM[LlamaModel, LlamaConfig])`（[modeling_llama.py:1134-1135](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_llama.py#L1134-L1135)），以及 `SpecDecOneEngineForCausalLM(DecoderModelForCausalLM[...])`（[modeling_speculative.py:1894-1895](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_speculative.py#L1894-L1895)）。

2. **填「复用」栏**（来自基类、Llama 没重写）：lm head 构造与 tie embedding 逻辑、`logits_processor`、`load_weights`、`__post_init__`/`create_weights`、`__pp_init__` 的外壳部分、`get_model_defaults`、forward 里「骨干 → logits」的两步骨架。

3. **填「覆盖」栏**（Llama 自己写的）：
   - `LlamaForCausalLM.__init__`：只是 `super().__init__(LlamaModel(model_config), model_config)`，把骨干交出去（[modeling_llama.py:1137-1141](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_llama.py#L1137-L1141)）。
   - `LlamaForCausalLM.setup_aliases`：接跨层 norm 链（[modeling_llama.py:1143-1153](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_llama.py#L1143-L1153)）。
   - `LlamaModel`（[modeling_llama.py:1030](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_llama.py#L1030)）：覆盖 `__init__` 建 embed/layers/norm，覆盖 `forward` 加残差流与 `skip_norm`。**这就是「Decoder」类的落点**。
   - `LlamaDecoderLayer`（[modeling_llama.py:659](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_llama.py#L659)）：实现抽象的 `forward`。

4. **标注「Decoder」类位置**：明确写出「`Decoder` = `DecoderModel`，其 Llama 具体子类是 [modeling_llama.py:1030](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_llama.py#L1030) 的 `LlamaModel`」。

5. （可选对照）看一个**更干净**的直接继承例子 [MistralForCausalLM](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_llama.py#L1609-L1627)（modeling_llama.py:1609-1627）：它直接 `DecoderModelForCausalLM[LlamaModel, LlamaConfig]`，没有 SpecDec 中间层，能更清楚地看到「子类只写 `__init__`」的极简模式。

**预期产出**：一张两列表格，左列「复用自基类」，右列「Llama 覆盖/新增」。这张表会让你确信：**写一个新模型，工作量几乎全在 `Model`（骨干）和 `DecoderLayer`（单层）两个类上，外壳几乎零成本。** 这是 u5-l3「添加一个新模型」的直接前提。

## 6. 本讲小结

- **三层分工**：`DecoderModelForCausalLM`（外壳，管 lm head/logits/权重/PP/投机解码）→ `DecoderModel`（骨干，管 embed→逐层→norm 的模板）→ `DecoderLayer`（单层抽象，规定 forward 签名 + 提供 PP 的 `skip_forward`）。
- **命名澄清**：本仓库没有字面叫 `Decoder` 的类，「Decoder」= `DecoderModel`；Llama 的骨干是 `LlamaModel`。
- **元类驱动初始化**：`PostInitCaller` 在对象创建后自动串联 `__post_init__`（量化/create_weights）与 `__pp_init__`（切层），配合 meta 设备省显存——这是理解模型加载的关键。
- **残差流 + RMSNorm 融合**：层与层之间以 `(hidden, residual)` 流动；`RMSNorm(hidden, residual)` 把「残差相加 + norm」合成一次。
- **跨层 norm 链**：`setup_aliases` 把下一层的 `input_layernorm` 接到上一层结尾，配合 `skip_input_layernorm`，实现「allreduce + 残差 + norm」融合。
- **复用多、覆盖少**：子类主要写 `Model.__init__/forward` 与 `DecoderLayer.forward`，外壳能力（含 `get_model_defaults`）直接继承——这正是「Config + ForCausalLM」范式的价值。

## 7. 下一步学习建议

- **u5-l2 自动发现与模型注册**：本讲出现了 `@register_auto_model("LlamaForCausalLM")` 和 `MODEL_CLASS_MAPPING`，下一讲会讲清楚 HF 的 `architectures` 字段如何一路解析到这里的模型类。
- **u5-l3 添加一个新模型**：拿着本讲的「复用 vs 覆盖」表，实践写一个 `modeling_mymodel.py`，你会发现只需照着 `LlamaModel` / `LlamaDecoderLayer` 填两个类。
- **延伸阅读**：对照 [tensorrt_llm/_torch/models/modeling_deepseekv3.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/models/modeling_deepseekv3.py)（MoE 模型）看同一套范式如何承载更复杂的层（MoE + MLA），体会「骨干契约不变、单层变复杂」的可扩展性。
