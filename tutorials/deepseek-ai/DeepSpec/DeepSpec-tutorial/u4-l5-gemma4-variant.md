# 接入第二个模型族：Gemma4 变体与模型无关层

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐条列出 `Gemma4DSparkModel` 与 `Qwen3DSparkModel` 在**注意力结构、归一化、embedding、RoPE、logits 输出**上的差异点，并说出哪些代码是逐行相同的。
2. 解释 `gemma4/config.py` 的 `build_draft_config` 为什么比 qwen3 版本多出「提取 `text_config`」和「21 个必填字段校验」两步。
3. 读懂 `config/dspark/dspark_gemma4_12b.py`，理解 `mask_token_id`、`target_layer_ids`、`chat_template`、`torch_compile` 等字段为什么会随模型族变化。
4. 建立一张「新增模型族要改哪些文件」的清单，为第 7 单元的扩展实战打基础。

本讲是第 4 单元（DSpark 建模）的收官。前面四讲已经讲完了构图三件套（u4-l1）、Qwen3 草稿模型结构（u4-l2）、Markov 头（u4-l3）和损失函数（u4-l4）。本讲换一个视角：**把 Gemma4 当作「第二个用户」来检验 DSpark 的代码分层**——如果一套算法代码是好的，接入第二个模型族时应该只需要重写「与 HF 组件耦合的那一层」，而算法本身一行不改。DeepSpec 正是这样组织的。

## 2. 前置知识

阅读本讲前，你需要回顾以下概念（都在前几讲建立过）：

- **草稿模型与目标模型**（u1-l1）：小草稿模型读目标模型的中间层隐状态，学会「一步猜出目标模型接下来要输出的 token 块」。
- **target cache 与 target_layer_ids**（u2-l4、u2-l5）：训练前用 forward hook 把目标模型若干层（`target_layer_ids` 指定，`-1` 代表 embedding 输出）的隐状态落盘；训练时草稿模型把它们拼接后作为输入特征。
- **DSpark 构图三件套**（u4-l1）：`sample_anchor_positions` 采锚点、`create_noise_embed` 造 mask token 噪声块、`create_dspark_attention_mask` 造「上下文 + 本块内可见」的块稀疏掩码。这三个函数在 `common.py`，**与模型族无关**。
- **双源 K/V 注意力**（u4-l2）：上下文位置的 K/V 来自目标缓存特征（经 `fc` 投影 + `hidden_norm`），草稿位置的 K/V 来自草稿自身残差流，两路共享同一组投影权重，按「上下文在前、草稿在后」拼接。
- **模板方法模式**（u3-l1）：`BaseTrainer` 定死训练骨架，子类只填 `_build_draft_model` 和 `run_batch` 两个钩子。

本讲新引入的术语：

- **模型族（model family）**：指 Qwen3、Gemma4 这类基于 HuggingFace `transformers` 实现的预训练模型家族。每个家族有自己的 config 字段、注意力变体、归一化结构和 tokenizer。
- **模型无关层（model-agnostic layer）**：指 `deepspec/modeling/dspark/` 下的 `common.py`（构图 + 输出容器）、`loss.py`（损失）、`markov_head.py`（Markov 头）——它们只操作抽象张量，不知道底下是 Qwen3 还是 Gemma4。
- **模型相关层（model-specific layer）**：指 `deepspec/modeling/dspark/<family>/` 目录——attention、decoder layer、模型外壳，这些必须复刻对应家族的结构约定。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspec/modeling/dspark/gemma4/modeling.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py) | Gemma4 草稿模型本体：注意力、decoder layer、模型外壳与训练 forward |
| [deepspec/modeling/dspark/gemma4/config.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/config.py) | Gemma4 版 `build_draft_config`：从嵌套 config 提取 text_config 并派生草稿 config |
| [deepspec/modeling/dspark/qwen3/modeling.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py) | 对照组：Qwen3 草稿模型（u4-l2 已精读） |
| [deepspec/modeling/dspark/qwen3/config.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py) | 对照组：Qwen3 版 `build_draft_config` |
| [config/dspark/dspark_gemma4_12b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_gemma4_12b.py) | Gemma4-12B 的训练配置（README 中 released checkpoint 的对应配置） |
| [config/dspark/dspark_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py) | 对照组：Qwen3-4B 的训练配置 |
| [deepspec/trainer/dspark_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py) | 两个 trainer：`Gemma4DSparkTrainer` 只覆写一个钩子 |
| [deepspec/modeling/dspark/common.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py) | 模型无关层：构图函数、`validate_target_layer_ids`、`DSparkForwardOutput` |
| [deepspec/data/parser.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py) | `TEMPLATE_REGISTRY` 里 `"gemma4"` 聊天模板的注册处（`data.chat_template` 的消费端） |

目录结构上，`deepspec/modeling/dspark/` 下只有三个共享文件（`common.py`、`loss.py`、`markov_head.py`）和两个家族子目录（`qwen3/`、`gemma4/`，各含 `__init__.py`、`config.py`、`modeling.py`）。这个「共享算法 + 家族子目录」的布局就是本讲的主角。

## 4. 核心概念与源码讲解

### 4.1 Gemma4DSparkModel 差异：注意力、Norm 与外壳

#### 4.1.1 概念说明

DSpark 草稿模型的「骨架」是固定的：噪声块输入 + 双源 K/V 注意力 + 若干 decoder 层 + 冻结的 embedding/lm_head + 可选的 Markov 头和置信度头。但骨架里每个「零件」长什么样，由目标模型家族决定——草稿模型必须在数值行为上与目标模型族兼容（embedding 是否缩放、注意力缩放系数、RoPE 数学形式、logits 是否 softcap），否则从目标模型中间层「继承」来的特征就对不上号。

因此 DeepSpec 的做法是：**每个家族写一份 modeling.py，零件从 `transformers` 对应家族模块里直接 import 原件**，只有把零件组装成「双源 K/V 注意力」和「DSpark 训练 forward」的胶水代码是新写的。Gemma4 版比 Qwen3 版多出来的复杂度，全部来自 Gemma4 家族本身的结构差异：

1. **V 可以等于 K**：Gemma4 有一种「替代注意力」模式（`attention_k_eq_v`），此时根本没有 `v_proj`，Value 直接复用 Key。
2. **四种层内归一化 + 层缩放系数**：Gemma 风格的「三明治 norm」——MLP 前后各有一次 norm，层输出还乘一个 `layer_scalar`。
3. **缩放 embedding**：embedding 输出乘 \(\sqrt{d_{\text{model}}}\)，这是 Gemma 家族的传统。
4. **logit softcap**：lm_head 输出可选做 \(\text{tanh}(x/c)\cdot c\) 截断。

#### 4.1.2 核心流程

一次 Gemma4 草稿模型的训练前向，与 Qwen3 版完全同构（这正是设计意图）：

```text
forward(input_ids, target_hidden_states, loss_mask, target_last_hidden_states)
  ├─ sample_anchor_positions          # common.py，采锚点（模型无关）
  ├─ create_noise_embed               # common.py，造噪声块（模型无关）
  ├─ create_position_ids / create_dspark_attention_mask   # common.py
  ├─ _forward_backbone                # ★ 家族差异点 1：rotary_emb 调用方式
  │    ├─ hidden_norm(fc(target_hidden_states))           # K×H → H，家族差异在 norm 类
  │    └─ for layer in layers: Gemma4DSparkDecoderLayer   # ★ 家族差异点 2：层结构
  │         ├─ input_layernorm → 双源 K/V attention       # ★ 家族差异点 3：注意力
  │         ├─ post_attention_layernorm → +residual
  │         └─ pre_ff_norm → MLP → post_ff_norm → +residual → ×layer_scalar
  ├─ compute_logits                   # ★ 家族差异点 4：可选 softcap
  ├─ markov_head.apply_block_logits   # markov_head.py，模型无关
  └─ return DSparkForwardOutput       # common.py，形状合同，模型无关
```

标 ★ 的四处是家族差异，其余全部逐行复用 Qwen3 版逻辑（`forward()` 本体约 150 行在两个文件中逐行相同）。

#### 4.1.3 源码精读

**(1) 注意力：head_dim、缩放系数与「V=K」模式**

Qwen3 版从 config 读 `head_dim`（缺省时按 `hidden_size // num_attention_heads` 推导），注意力缩放系数取 \(\text{head\_dim}^{-1/2}\)：[deepspec/modeling/dspark/qwen3/modeling.py:L48-L57](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L48-L57)（这段代码读 head_dim 并计算 scaling = head_dim**-0.5）。

Gemma4 版则改用 `global_head_dim`，并把缩放系数硬编码为 1.0，同时根据 `attention_k_eq_v` 决定 KV 头数与是否需要 `v_proj`：[deepspec/modeling/dspark/gemma4/modeling.py:L40-L50](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L40-L50)

```python
self.head_dim = int(config.global_head_dim)
self.use_alternative_attention = bool(config.attention_k_eq_v)
if self.use_alternative_attention:
    self.num_key_value_heads = int(config.num_global_key_value_heads)
else:
    self.num_key_value_heads = int(config.num_key_value_heads)
...
self.scaling = 1.0
```

这段代码说明：Gemma4 的全局注意力层（global attention）有自己的 head 维度和 KV 头数（`global_head_dim` / `num_global_key_value_heads`），与滑动层不同；而 `scaling = 1.0` 与 Gemma 家族「把缩放吸收进别处（如 embedding 缩放与 query 归一化约定）」的实现保持一致——草稿模型必须复刻目标模型的数值行为，不能沿用 Qwen3 的 \(1/\sqrt{d}\)。

最显眼的差异是 `v_proj` 可以不存在：[deepspec/modeling/dspark/gemma4/modeling.py:L63-L69](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L63-L69)（当 `use_alternative_attention` 为真时跳过创建 `v_proj`）。对应地，forward 里 Value 直接取 Key 的投影结果：[deepspec/modeling/dspark/gemma4/modeling.py:L107-L114](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L107-L114)

```python
k_ctx = self.k_proj(target_hidden_states)
k_noise = self.k_proj(hidden_states)
if self.use_alternative_attention:
    v_ctx = k_ctx
    v_noise = k_noise
else:
    v_ctx = self.v_proj(target_hidden_states)
    v_noise = self.v_proj(hidden_states)
```

注意这里保留了 u4-l2 讲过的**双源结构**：上下文 K/V（`*_ctx`，来自目标缓存特征）和草稿 K/V（`*_noise`，来自草稿残差流）用同一组投影权重分别计算，再在 [L115-L126](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L115-L126) 按「上下文在前、草稿在后」拼接成 `[bsz, ctx_len + q_len, ...]`——这一点两个家族完全一致。

Gemma4 版还多一个 **`v_norm`**（无 scale 的 RMSNorm）：[deepspec/modeling/dspark/gemma4/modeling.py:L75-L81](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L75-L81)（对 q、k、v 各建一个 RMSNorm，其中 v 的 `with_scale=False`）。Qwen3 版只有 `q_norm`/`k_norm`（[qwen3/modeling.py:L79-L80](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L79-L80)），Value 不做归一化。

**(2) 注意力后端分发方式不同**

Qwen3 版走 HF 的注意力函数注册表 `ALL_ATTENTION_FUNCTIONS`，按 `config._attn_implementation` 选内核：[deepspec/modeling/dspark/qwen3/modeling.py:L129-L147](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L129-L147)（从注册表取 `attn_fn` 并传入 sliding_window 等参数；flex 路径下还在 [L120-L128](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L120-L128) 手工做 GQA 的 `repeat_interleave`）。

Gemma4 版直接写死二分支——`flex_attention`（带 block_mask）或 `F.scaled_dot_product_attention`：[deepspec/modeling/dspark/gemma4/modeling.py:L143-L165](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L143-L165)，并且 GQA 复制（`_repeat_kv`，见 [L83-L86](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L83-L86)）在两个分支之前无条件执行（[L141-L142](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L141-L142)）。评估时草稿模型走 SDPA 分支，训练走 flex 分支，两个家族殊途同归。

**(3) Decoder 层：从 2 个 norm 到 4 个 norm + 层缩放**

Qwen3 版层结构是标准的「pre-norm」两件套：[deepspec/modeling/dspark/qwen3/modeling.py:L160-L163](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L160-L163)（`input_layernorm` 与 `post_attention_layernorm`），残差流是 `x + attn(norm(x))`、`x + mlp(norm(x))`。

Gemma4 版是「三明治」结构，并且带两个能力断言：[deepspec/modeling/dspark/gemma4/modeling.py:L171-L199](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L171-L199)

```python
assert not bool(config.enable_moe_block), (
    "Gemma4 DSpark prototype does not support Gemma4 MoE blocks yet."
)
assert int(config.hidden_size_per_layer_input) == 0, (
    "Gemma4 DSpark prototype does not support per-layer input gates yet."
)
```

这两行明确告知：当前 Gemma4 DSpark 是原型实现，**不支持 MoE 块和逐层输入门控**——这是「不编造能力」的工程表达，接入带这些特性的 Gemma4 目标时会直接断言失败而不是静默出错。接着是四个 norm 与 `layer_scalar`：[L183-L199](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L183-L199)（`input_layernorm`、`post_attention_layernorm`、`pre_feedforward_layernorm`、`post_feedforward_layernorm` 四个 `Gemma4RMSNorm`，外加一个初始化为 1 的 `layer_scalar` buffer）。层 forward 里的残差与缩放见 [L220-L238](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L220-L238)，结尾 `return hidden_states * self.layer_scalar`。MLP 也换成了需要 `layer_idx` 的 `Gemma4TextMLP(config, layer_idx)`（[L182](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L182)），而 Qwen3 版是 `Qwen3MLP(config)`（[qwen3/modeling.py:L159](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L159)）。

**(4) 模型外壳：缩放 embedding、RoPE 与 logit softcap**

Qwen3 版用普通 `nn.Embedding`、`Qwen3RotaryEmbedding` 和本文件内的 `rotate_half` 式 RoPE：[deepspec/modeling/dspark/qwen3/modeling.py:L227-L239](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L227-L239)（embedding 与 rotary 的构建）及 [L34-L40](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L34-L40)（本地 `apply_rotary_pos_emb`）。

Gemma4 版的对应位置是三处家族适配：[deepspec/modeling/dspark/gemma4/modeling.py:L272-L297](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L272-L297)

```python
self.embed_tokens = Gemma4TextScaledWordEmbedding(
    config.vocab_size,
    config.hidden_size,
    getattr(config, "pad_token_id", None),
    embed_scale=float(config.hidden_size) ** 0.5,
)
...
self.rotary_emb = Gemma4TextRotaryEmbedding(
    config,
    layer_type="full_attention",
)
```

embedding 输出要乘 \( \sqrt{d_{\text{model}}} \)（Gemma 家族传统）；RoPE 用 Gemma4 原生实现（其旋转数学与 Qwen3 的 `rotate_half` 不同，故直接 import `transformers` 的 `apply_rotary_pos_emb`，见 [L17](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L17)），且构造与调用都要传 `layer_type="full_attention"`（`_forward_backbone` 中的调用见 [L432-L436](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L432-L436)），因为 Gemma4 的 RoPE 参数（`rope_parameters`）按层型区分。

最后是 logits 的差异——Gemma4 版的 `compute_logits` 支持可选 softcap：[deepspec/modeling/dspark/gemma4/modeling.py:L339-L348](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L339-L348)

```python
logits = self.lm_head(hidden_states)
softcap = getattr(self.config, "final_logit_softcapping", None)
if softcap is not None:
    ...
    logits = torch.tanh(logits / softcap) * softcap
```

即 \( \text{logits} \leftarrow \tanh(\text{logits}/c)\cdot c \)，把 logits 约束在 \((-c, c)\) 内。Qwen3 版就是纯线性（[qwen3/modeling.py:L289-L290](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L289-L290)）。注意这个 softcap 同时作用于草稿 logits 和 `aligned_target_logits`（两者都经 `compute_logits` 计算，见 forward 中 [L537](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L537) 与 [L554](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L554)），保证 u4-l4 的 L1 蒸馏在同一个数值尺度上比较两个分布。

**(5) 逐行相同的部分：证明「算法在共享层」**

两个文件的 `forward()`（[gemma4/modeling.py:L450-L597](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L450-L597) 对照 [qwen3/modeling.py:L388-L525](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L388-L525)）逐行相同：采锚点、造噪声块、拼 position_ids、建块掩码、gather 标签与目标隐状态、构建 `eval_mask`、拼 `prev_token_ids`、套 Markov 头、调 `log_sampler_stats`、算置信度、返回 `DSparkForwardOutput`。`initialize_embeddings_and_head`、`predict_confidence_step`、`sample_draft_tokens`、`sample_draft_token_step` 等方法也完全一致（对比 [gemma4/modeling.py:L320-L417](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L320-L417) 与 [qwen3/modeling.py:L270-L359](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L270-L359)）。这就是「模型无关层」的直接证据：DSpark 算法逻辑只需要写一遍。

#### 4.1.4 代码实践

**实践目标**：用 `diff` 亲手确认「哪些代码是家族差异、哪些是逐行复用」，产出一份差异清单。

**操作步骤**：

1. 在仓库根目录执行（只读命令，不改动任何文件）：

   ```bash
   diff -u deepspec/modeling/dspark/qwen3/modeling.py \
         deepspec/modeling/dspark/gemma4/modeling.py > /tmp/modeling.diff
   wc -l /tmp/modeling.diff
   ```

2. 打开 `/tmp/modeling.diff`，按 hunk（差异块）浏览，把每个 hunk 归类到下表的一行里。

3. 再统计「完全没出现在 diff 里的方法名」，验证它们是复用代码：

   ```bash
   for fn in initialize_embeddings_and_head predict_confidence_step \
             sample_draft_tokens sample_draft_token_step forward; do
     echo "== $fn =="
     grep -c "def $fn" deepspec/modeling/dspark/qwen3/modeling.py \
                     deepspec/modeling/dspark/gemma4/modeling.py
   done
   ```

**需要观察的现象**：

- diff 的差异集中在 import 块、`*Attention.__init__`、`*Attention.forward` 的 V 分支与后端分支、`*DecoderLayer.__init__`/`forward`、模型 `__init__` 的组件构建、`compute_logits`、`_forward_backbone` 的 rotary 调用。
- `forward`、`initialize_embeddings_and_head`、`sample_draft_tokens` 等方法的函数体**几乎不出现在 diff 里**（即使出现也只是因为外层类名/行号不同）。

**预期结果**：你应能得到一张类似下面骨架的差异清单（本讲 4.1.3 已给出完整版，请自己 diff 一遍核对）：

| 差异维度 | Qwen3 | Gemma4 |
| --- | --- | --- |
| 注意力缩放 | \( \text{head\_dim}^{-1/2} \) | 1.0 |
| V 投影 | 恒有 `v_proj` | `attention_k_eq_v` 为真时无 `v_proj`，V=K |
| head_dim / KV 头 | `head_dim` / `num_key_value_heads` | `global_head_dim` / 按模式二选一 |
| V 归一化 | 无 | `v_norm`（无 scale） |
| 层内 norm | 2 个 | 4 个 + `layer_scalar` |
| embedding | `nn.Embedding` | 缩放 embedding（\( \sqrt{d} \)） |
| logits | 纯线性 | 可选 tanh softcap |
| 后端分发 | `ALL_ATTENTION_FUNCTIONS` 注册表 | flex / SDPA 二分支 |
| RoPE | `rotate_half` 本地实现 | Gemma4 原生 + `layer_type` |

#### 4.1.5 小练习与答案

**练习 1**：如果删掉 Gemma4 版的 `v_norm`，直接沿用 Qwen3 的「V 不归一化」，会发生什么级别的错误——权重加载报错，还是数值行为偏差？

**答案**：数值行为偏差（且很可能是隐性的）。`v_norm` 是带参数的 `Gemma4RMSNorm` 模块，草稿模型的注意力层是从零训练的（不加载 Gemma4 目标注意力权重，草稿只冻结复用 embedding/lm_head），所以不会在权重加载时报错；但草稿模型在**评估时**要与目标模型协同做投机解码，其内部数值约定越偏离家族惯例，学到的分布越难对齐目标。更直接的问题是：`required_fields` 与 `_validate_required_text_fields` 校验的是 config 而非模块，模型能构建也能训练，错误只会在最终接受率上暴露——这正是「结构复刻家族约定」重要的原因。

**练习 2**：`Gemma4DSparkModel.__init__` 的 `required_fields` 断言元组（[gemma4/modeling.py:L250-L258](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L250-L258)）比 Qwen3 版（[qwen3/modeling.py:L207-L213](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L207-L213)）多了哪两个字段？为什么？

**答案**：多了 `num_global_key_value_heads` 和 `global_head_dim`。因为 Gemma4 的草稿注意力（全注意力层）需要这两个字段来决定 head 维度与 KV 头数（见 4.1.3 第 (1) 点），而 Qwen3 用通用的 `head_dim`/`num_key_value_heads` 就够了。这个断言的作用是把「config 派生遗漏字段」从运行深处的 `AttributeError` 提前到模型构建入口，报错信息直接点名缺失字段。

**练习 3**：`compute_logits` 的 softcap 为什么必须同时作用于 `aligned_target_logits`？

**答案**：`aligned_target_logits` 是 u4-l4 中 L1 蒸馏的教师分布（目标模型 last hidden 过**共享的冻结 lm_head** 得到）。如果 softcap 只作用于草稿 logits，教师和学生的 logits 处于不同数值尺度，\( L_1 = 2(1-a) \) 与「接受率」之间的等价关系就不再成立，蒸馏目标被系统性扭曲。两个分布都经过同一个 `compute_logits`，才保证 L1 距离度量的是真实的分布差。

### 4.2 gemma4 build_draft_config：从嵌套 config 派生草稿 config

#### 4.2.1 概念说明

u4-l2 讲过 Qwen3 版 `build_draft_config` 的套路：深拷贝目标 config，覆写 `num_hidden_layers`（草稿层数）、写入 `architectures` 派发键、解绑 `tie_word_embeddings`、追加 DSpark 专属字段（`block_size`、`target_layer_ids`、`markov_rank` 等）。

Gemma4 版多出两件 Qwen3 不需要的事：

1. **提取 text_config**：Qwen3-4B 是纯文本模型，顶层 config 就是文本 config；而 Gemma4 是多模态模型，顶层 config 里文本部分嵌在 `text_config` 字段下，草稿 config 必须从 `text_config` 派生，否则会带着视觉塔的字段与维度。
2. **必填字段校验**：Gemma4 的 text config 有大量家族特有字段（`global_head_dim`、`attention_k_eq_v`、`enable_moe_block`、`rope_parameters`……），派生前逐一断言存在，缺一个就立即失败并指明缺哪个。

此外它还多写两个「溯源」字段 `target_model_type` / `target_text_model_type`，把来源模型类型随草稿 config 一起存进 checkpoint。

#### 4.2.2 核心流程

```text
build_draft_config(target_config, model_args)
  ├─ get_gemma4_text_config(target_config)      # ★ Gemma4 特有：断言 model_type 并取 text_config
  │    ├─ assert 顶层 model_type ∈ {gemma4, gemma4_unified}
  │    ├─ assert text_config.model_type ∈ {gemma4_text, gemma4_unified_text}
  │    └─ return copy.deepcopy(text_config)
  ├─ _validate_required_text_fields(...)        # ★ Gemma4 特有：21 个必填字段断言
  ├─ validate_target_layer_ids(...)             # 共享：层号 ∈ {-1} ∪ [0, L-1] 且严格递增
  ├─ 校验 confidence_head_alpha / markov_rank 及其伴随字段   # 与 qwen3 相同
  └─ 覆写 draft_config 字段                     # 与 qwen3 相同 + 两个溯源字段
```

与 Qwen3 版的逐行对照：[qwen3/config.py:L37-L56](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L37-L56) 直接 `copy.deepcopy(target_config)` 后开始覆写，没有任何提取与校验步骤。

#### 4.2.3 源码精读

**(1) 提取并断言 text_config**

[deepspec/modeling/dspark/gemma4/config.py:L9-L19](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/config.py#L9-L19)

```python
def get_gemma4_text_config(target_config):
    assert target_config.model_type in ("gemma4", "gemma4_unified"), (...)
    text_config = target_config.text_config
    assert text_config.model_type in ("gemma4_text", "gemma4_unified_text"), (...)
    return copy.deepcopy(text_config)
```

这段代码做了三件事：确认顶层确实是 Gemma4 家族（防止拿错目标模型）、取嵌套的 `text_config`、深拷贝出一个可安全覆写的副本（深拷贝保证后续对 `draft_config` 的修改不会污染原始目标 config 对象）。`gemma4_unified` 变体的兼容说明 DeepSpec 预期会面对同一家族的多种 config 形态。

**(2) 21 个必填字段的清单**

[deepspec/modeling/dspark/gemma4/config.py:L22-L49](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/config.py#L22-L49) 定义了 `_validate_required_text_fields`，要求的字段包括 `vocab_size`、`hidden_size`、`num_hidden_layers` 这类通用字段，也有 `num_global_key_value_heads`、`global_head_dim`、`attention_k_eq_v`、`enable_moe_block`、`hidden_size_per_layer_input`、`num_kv_shared_layers`、`rope_parameters`、`use_double_wide_mlp` 这类 Gemma4 特有字段。任何缺失都会以 `target_config.text_config.<field> must be provided.` 的明确信息失败。这份清单实际上是「Gemma4 草稿模型实现依赖了哪些家族字段」的权威索引——想知道 modeling.py 用到了什么，读它最快。

**(3) 与 Qwen3 相同的派生逻辑 + 两个溯源字段**

[deepspec/modeling/dspark/gemma4/config.py:L52-L102](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/config.py#L52-L102) 是完整的 `build_draft_config`。与 Qwen3 版相同的部分：`num_hidden_layers` 覆写为 `num_draft_layers`、`architectures = ["Gemma4DSparkModel"]`（L82，这是评估侧 `EVALUATORS` 分发键的来源，见 [eval.py:L12](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L12)）、`tie_word_embeddings = False`、`_attn_implementation = "flex_attention"`（训练固定用 flex，见常量 [L6](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/config.py#L6)）、写入 `block_size` / `target_layer_ids` / `num_anchors` / `markov_rank` / 置信度开关等 DSpark 字段。

Gemma4 特有的是 [L83-L84](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/config.py#L83-L84)：

```python
draft_config.target_model_type = str(target_config.model_type)
draft_config.target_text_model_type = str(draft_config.model_type)
```

把「这个草稿 config 派生自哪种目标模型」记录在案。这两个字段随 checkpoint 的 `config.json` 一起保存；在当前仓库代码里它们只被写入、没有被别处读取（可用 grep 验证），作用是溯源元数据——拿到一个草稿 checkpoint 就能知道它的目标模型族。

**(4) 共享的层号校验**

两个家族都调用 [common.py:L59-L75](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L59-L75) 的 `validate_target_layer_ids`：每个层号要么是 `-1`（哨兵，表示 embedding 输出），要么落在 \([0, \text{num\_target\_layers}-1]\)，且必须严格递增。这是典型的「校验逻辑放共享层」——规则对任何模型族都一样，就不该写两遍。

#### 4.2.4 代码实践

**实践目标**：验证 `build_draft_config` 对嵌套 config 的处理，并观察「缺字段时报什么错」。

**操作步骤**（以下为示例代码，仅读取仓库、不修改源码；需要已 `pip install -r requirements.txt`）：

1. 写一个临时脚本 `/tmp/test_gemma4_cfg.py`：

   ```python
   # 示例代码：手工构造一个最小 Gemma4 风格嵌套 config，走一遍派生流程
   from types import SimpleNamespace
   from deepspec.modeling.dspark.gemma4.config import build_draft_config

   text_cfg = SimpleNamespace(
       model_type="gemma4_text", vocab_size=262208, hidden_size=3840,
       intermediate_size=15360, num_hidden_layers=48, num_attention_heads=32,
       num_global_key_value_heads=16, global_head_dim=256, attention_bias=False,
       attention_dropout=0.0, attention_k_eq_v=True, enable_moe_block=False,
       head_dim=256, hidden_activation="gelu_pytorch_tanh",
       hidden_size_per_layer_input=0, initializer_range=0.02,
       max_position_embeddings=131072, num_key_value_heads=16,
       num_kv_shared_layers=0, rms_norm_eps=1e-6, rope_parameters={},
       use_double_wide_mlp=False,
   )
   target_cfg = SimpleNamespace(model_type="gemma4", text_config=text_cfg)
   model_args = dict(
       num_draft_layers=5, target_layer_ids=[5, 17, 29, 41, 46],
       block_size=7, mask_token_id=4, num_anchors=512,
       confidence_head_alpha=1.0, confidence_head_with_markov=True,
       markov_rank=256, markov_head_type="vanilla",
   )
   draft = build_draft_config(target_cfg, model_args)
   print(draft.num_hidden_layers, draft.target_layer_ids, draft.architectures)
   print(draft.target_model_type, draft.target_text_model_type)
   ```

   注意：`SimpleNamespace` 不是真正的 `PretrainedConfig`，`num_hidden_layers=48` 等数值是示例值；`build_draft_config` 只做属性访问与覆写，因此这个脚本可以验证派生逻辑本身。**完整跑通仍需以真实 `google/gemma-4-12B-it` config 为准，待本地验证。**

2. 再做两个破坏性小实验（每次只改一处）：
   - 把 `target_cfg.model_type` 改成 `"qwen3"`，重跑，观察报错；
   - 删掉 `text_cfg.attention_k_eq_v` 这一行，重跑，观察报错。

**需要观察的现象**：

- 正常路径打印出 `5 [5, 17, 29, 41, 46] ['Gemma4DSparkModel']` 和 `gemma4 gemma4_text`；
- 实验 1 应在 `get_gemma4_text_config` 的第一个断言处失败，报错信息包含 `expects a Gemma4 or Gemma4 Unified top-level target config`；
- 实验 2 应在 `_validate_required_text_fields` 处失败，报错信息为 `target_config.text_config.attention_k_eq_v must be provided.`。

**预期结果**：所有失败都发生在派生入口、且报错点名具体字段——「快速失败 + 可定位」是这个函数的设计要点。若你的 transformers 版本里 Gemma4 的 config 字段名与示例不同，请以 `_validate_required_text_fields` 的清单和真实目标 config 为准调整。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Qwen3 版 `build_draft_config` 不需要 `get_text_config` 这一步，也不需要必填字段校验？

**答案**：因为 Qwen3-4B 是纯文本模型，顶层 config 即文本 config，`copy.deepcopy(target_config)` 拿到的直接就是全部所需字段；Qwen3 家族的字段集合也与 HF 标准对齐，草稿实现只用到 `head_dim`、`num_key_value_heads` 等常规字段，缺少它们时 HF 自己的模型构建就会先报错。Gemma4 的多模态嵌套结构和大量特有字段，使得「取错层级」和「字段缺失」成为真实风险，才值得加两道显式防线。

**练习 2**：`draft_config.tie_word_embeddings = False` 这行在两个家族里都有，它的作用是什么？

**答案**：解绑「embedding 与 lm_head 共享权重」。u3-l1 讲过，草稿模型的 `embed_tokens` 和 `lm_head` 是从目标模型**分别拷贝并冻结**的（`initialize_embeddings_and_head` 会断言两者形状并分别 `copy_`）。如果保留 `tie_word_embeddings=True`，保存/加载 checkpoint 时 HF 会把两个矩阵当作同一份参数处理，可能只存一份或加载时相互覆盖，破坏「冻结镜像目标模型」的语义。

**练习 3**：如果某天要支持一个「文本 + 视觉」且草稿只用文本部分的 Qwen 变体（例如 Qwen-VL 系），应该参考哪个家族的 config.py 写法？

**答案**：参考 Gemma4 版。它已经解决了「顶层是多模态 config、需要提取嵌套 text_config、并校验家族特有字段」的完整问题：`get_gemma4_text_config` 对应「找到并深拷贝文本子 config」，`_validate_required_text_fields` 对应「把该家族特有依赖列成显式清单」。

### 4.3 dspark_gemma4_12b 配置：随模型族变化的字段

#### 4.3.1 概念说明

`config/` 目录下每份训练配置就是一个 Python 文件（u1-l4 讲过「配置即代码」）。对比 `dspark_gemma4_12b.py` 和 `dspark_qwen3_4b.py` 会发现一个重要事实：**两个家族的超参数几乎全部相同，不同的只有「必须随模型族变」的字段**。这说明 DSpark 算法本身的超参（块大小、锚点数、损失权重、Markov 秩）是跨目标模型迁移的，接入新家族时不需要重新调一遍算法超参——改的只是「接口层」。

必须随模型族变的字段有四类：

1. **目标模型身份**：`target_model_name_or_path`（常量 `GEMMA_4_12B = "google/gemma-4-12B-it"`，见 [deepspec/utils/constant/public.py:L10](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/constant/public.py#L10)）。
2. **词表相关**：`mask_token_id`——噪声块用的 mask token 是目标模型词表里的一个具体 id，天然因家族而异。
3. **目标结构相关**：`target_layer_ids`——层号取决于目标模型的深度与「哪些层的信息量最大」，因模型而异。
4. **模板与类**：`trainer_cls`（以及 `data.chat_template`，它决定 parser 用哪个聊天模板渲染对话）。

#### 4.3.2 核心流程

一份家族配置到「可训练的草稿模型」的链路：

```text
config/dspark/dspark_gemma4_12b.py
  ├─ model.target_model_name_or_path ──► 加载目标 config + 目标权重（CPU）
  ├─ model.* 其余字段打包为 model_args ──► Gemma4DSparkTrainer._build_draft_model
  │        └─ build_draft_config(target_config, model_args) → Gemma4DSparkModel
  ├─ train.trainer_cls = Gemma4DSparkTrainer ──► train.py 直接实例化（配置即代码）
  └─ data.chat_template = "gemma4" ──► 数据阶段 parser 查 TEMPLATE_REGISTRY
```

其中 `Gemma4DSparkTrainer` 的全部实现只有覆写一个钩子：[deepspec/trainer/dspark_trainer.py:L42-L48](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L42-L48)

```python
class Gemma4DSparkTrainer(Qwen3DSparkTrainer):
    def _build_draft_model(self, *, target_config, model_args):
        draft_config = build_gemma4_draft_config(
            target_config=target_config,
            model_args=model_args,
        )
        return Gemma4DSparkModel(draft_config)
```

它继承 `Qwen3DSparkTrainer` 的 `run_batch`（调 `compute_dspark_loss`，u4-l4 的共享损失）。评估侧完全对称：`Gemma4DSparkEvaluator` 只换 `draft_model_cls`（[deepspec/eval/dspark/evaluator.py:L224-L225](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L224-L225)）。**接入一个家族的「代码改动面」就是：一个 modeling.py + 一个 config.py + 一个 trainer 子类 + 一个 evaluator 子类 + 一份训练配置。**

#### 4.3.3 源码精读

**(1) model 字段：只有三处与 Qwen3 不同**

Gemma4 配置：[config/dspark/dspark_gemma4_12b.py:L11-L31](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_gemma4_12b.py#L11-L31)

```python
model = dict(
    target_model_name_or_path=GEMMA_4_12B,
    block_size=7,
    num_draft_layers=5,
    target_layer_ids=[5, 17, 29, 41, 46],
    mask_token_id=4,
    num_anchors=512,
    markov_rank=256,
    markov_head_type="vanilla",
    confidence_head_alpha=1.0,
    confidence_head_with_markov=True,
    loss_decay_gamma=4.0,
    ce_loss_alpha=0.1,
    l1_loss_alpha=0.9,
)
```

对照 Qwen3 版 [config/dspark/dspark_qwen3_4b.py:L10-L30](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L10-L30)，逐字段差异只有三个：

| 字段 | Qwen3-4B | Gemma4-12B | 为什么随家族变 |
| --- | --- | --- | --- |
| `target_model_name_or_path` | `Qwen/Qwen3-4B` | `google/gemma-4-12B-it` | 目标模型身份 |
| `target_layer_ids` | `[1, 9, 17, 25, 33]` | `[5, 17, 29, 41, 46]` | 目标层数与「信息量大的层」不同；Gemma4 最大层号 46，由 `validate_target_layer_ids` 可知其目标层数不少于 47 |
| `mask_token_id` | `151669` | `4` | mask token 是目标词表里的具体 id，两族词表不同（具体对应哪个特殊 token，可待本地用各自 tokenizer 验证） |

其余字段——`block_size=7`、`num_draft_layers=5`、`num_anchors=512`、`markov_rank=256`、`confidence_head_alpha=1.0`、`loss_decay_gamma=4.0`、`ce_loss_alpha=0.1`、`l1_loss_alpha=0.9`——**完全一致**，即第 4 单元前几讲讲的 DSpark 算法超参原封不动地从一个模型族搬到了另一个模型族。

**(2) train 与 data 字段：trainer、torch_compile 与聊天模板**

[config/dspark/dspark_gemma4_12b.py:L33-L46](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_gemma4_12b.py#L33-L46) 与 [L53-L58](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_gemma4_12b.py#L53-L58)：

```python
train = dict(
    trainer_cls=Gemma4DSparkTrainer,
    ...
    torch_compile=False,
)
data = dict(
    target_cache_path=None,
    chat_template="gemma4",
    max_length=4096,
    num_workers=4,
)
```

与 Qwen3 版（[dspark_qwen3_4b.py:L33-L46](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L33-L46)、[L52-L57](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L52-L57)）相比有三处不同：`trainer_cls` 换成 Gemma4 版（训练入口 train.py 直接实例化配置里的这个类，见 u1-l3）；`torch_compile=False`（Qwen3 版为 `True`）——是否开启 `torch.compile` 按家族实测决定，Gemma4 原型配置选择关闭；`chat_template="gemma4"` 决定数据阶段用哪个聊天模板。

**(3) chat_template 的消费端：parser 的注册表**

`data.chat_template` 在生成 target cache 阶段被 `preprocess_record` 消费（u2-l2），它查的是 [deepspec/data/parser.py:L42-L51](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py#L42-L51) 注册的 Gemma4 模板：

```python
TEMPLATE_REGISTRY.register(
    "gemma4",
    ChatTemplate(
        assistant_header="<|turn>model\n",
        user_header="<|turn>user\n",
        system_prompt=None,
        end_of_turn_token="<turn|>\n",
        assistant_loss_prefix="<|channel>thought\n<channel|>",
    ),
)
```

与 `"qwen"` 模板（[parser.py:L32-L40](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py#L32-L40)）相比：Gemma4 没有默认 system_prompt（Gemma 把指令放在对话开头而非 system 轮），轮次分隔符完全不同；最关键的是 `assistant_loss_prefix="<|channel>thought\n<channel|>"`——u2-l2 讲过，Gemma4 渲染时插入空 thought 通道、计算 loss_mask 时跳过这段前缀，实现非思考训练。**所以「接入新模型族」不只是 modeling 的事：tokenizer 的对话格式不同，数据侧也要注册对应模板**，否则 loss_mask 会标错位置，整条监督信号作废。

#### 4.3.4 代码实践

**实践目标**：产出一份两族配置的逐字段差异表，并区分「家族必然差异」与「算法超参（应保持一致）」。

**操作步骤**：

1. 逐字段对比两份配置：

   ```bash
   diff -u config/dspark/dspark_qwen3_4b.py config/dspark/dspark_gemma4_12b.py
   ```

2. 把 diff 结果整理成三栏表格：字段 | Qwen3-4B 值 | Gemma4-12B 值。
3. 对每个不同字段，标注它属于哪一类：目标身份 / 词表 / 目标结构 / 模板 / 工程开关。
4. 用下面的只读命令验证「相同的算法超参」（示例代码）：

   ```bash
   for key in block_size num_draft_layers num_anchors markov_rank \
              loss_decay_gamma ce_loss_alpha l1_loss_alpha; do
     a=$(grep -oP "(?<=$key=)[0-9.]+" config/dspark/dspark_qwen3_4b.py)
     b=$(grep -oP "(?<=$key=)[0-9.]+" config/dspark/dspark_gemma4_12b.py)
     echo "$key: qwen3=$a gemma4=$b"
   done
   ```

**需要观察的现象**：diff 只报告约 5-6 行业务差异（target、target_layer_ids、mask_token_id、trainer_cls、torch_compile、chat_template），算法超参一概相同；步骤 4 应逐行打印出两边完全相等的值。

**预期结果**：得到类似下面的表（部分）：

| 字段 | Qwen3-4B | Gemma4-12B | 类别 |
| --- | --- | --- | --- |
| `target_model_name_or_path` | `Qwen/Qwen3-4B` | `google/gemma-4-12B-it` | 目标身份 |
| `target_layer_ids` | `[1,9,17,25,33]` | `[5,17,29,41,46]` | 目标结构 |
| `mask_token_id` | `151669` | `4` | 词表 |
| `trainer_cls` | `Qwen3DSparkTrainer` | `Gemma4DSparkTrainer` | 类绑定 |
| `torch_compile` | `True` | `False` | 工程开关 |
| `data.chat_template` | `"qwen"` | `"gemma4"` | 模板 |
| 其余 model/train 字段 | 相同 | 相同 | DSpark 算法超参 |

#### 4.3.5 小练习与答案

**练习 1**：把 `dspark_gemma4_12b.py` 的 `chat_template` 误留成 `"qwen"`，训练还能跑通吗？损失会在哪个环节出错？

**答案**：大概率能「跑」但监督信号是错的。`TEMPLATE_REGISTRY` 里两个模板都存在，查表不会报错；错在渲染出的对话文本用的是 Qwen 格式（`<|im_start|>` 等），而 Gemma4 tokenizer 对这些字符串的切分方式完全不同——`GeneralParser` 的正则按 assistant 头匹配字符区间时会匹配失败或匹配到错误区间，`loss_mask` 因此全 0 或错位；训练侧表现为 DSpark 的 `eval_mask` 几乎不含监督位置、损失异常小。这属于「静默错误」，比直接崩溃更危险，也解释了为什么 u2-l2 强调模板冻结注册、逐字段核对。

**练习 2**：Gemma4 配置的 `target_layer_ids=[5, 17, 29, 41, 46]` 与 Qwen3 的 `[1, 9, 17, 25, 33]` 都是 5 个层号、近似均匀分布。如果某目标只有 33 层，能照抄 Gemma4 的层号吗？

**答案**：不能。`validate_target_layer_ids`（[common.py:L59-L75](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L59-L75)）要求每个层号落在 \([0, L-1]\)，33 层目标最大合法层号是 32，层号 41/46 会直接触发断言失败（`out of range {-1} U [0, 32]`）。层号必须按目标实际深度重选；`-1`（embedding 输出）是唯一允许的越界哨兵。

**练习 3**：`torch_compile` 在 Qwen3 配置里是 `True`、Gemma4 里是 `False`。结合 u3-l1 讲过的 `torch_compile` 用途，说说不统一说明了什么？

**答案**：`torch.compile` 是性能开关而非正确性开关（u3-l1：`BaseTrainer.__init__` 里按 config 决定是否编译模型）。两族取值不同说明它按「各家族模型在动态形状下实测的编译收益/稳定性」逐案决定——Gemma4 原型可能存在编译开销高或重编译频繁的问题（其注意力有 `use_alternative_attention` 等分支）。这也提示接入新家族时 `torch_compile` 应作为待实测项，而不是照抄某个旧配置。

## 5. 综合实践

**任务：写一份《接入新模型族改动面清单》（设计文档，不落地代码）。**

以本讲的三组对比为材料，假设要为一个新的目标模型族（例如 Llama 系）接入 DSpark，产出一份包含以下小节的文档：

1. **modeling/<family>/modeling.py 必须重写的部分**：对照 4.1.3 的差异清单，逐项判断 Llama 需要哪种实现——注意力缩放系数是多少？有没有 V=K 模式？层内几个 norm？embedding 是否缩放？RoPE 用哪种实现（interleaved 还是 rotate_half）？logits 有没有 softcap？哪些方法（`forward`、`initialize_embeddings_and_head`、`sample_draft_tokens` 等）可以直接从 qwen3 版复制？
2. **modeling/<family>/config.py 的派生逻辑**：目标 config 是纯文本还是嵌套（决定要不要 `get_text_config`）？`_validate_required_text_fields` 该列哪些字段（列出你在 HF 源码/文档里实际确认过的）？`architectures` 派发键起什么名？
3. **config/dspark/<family>.py 的字段取值**：`target_layer_ids` 怎么选（写出依据：目标层数、均匀取 5 层、避开末层的约束来自 `validate_target_layer_ids` 与 u4-l2 讲的「target_layer_ids 不含末层」）？`mask_token_id` 怎么查（用 `AutoTokenizer.from_pretrained` 找 mask token）？`chat_template` 需要在 `TEMPLATE_REGISTRY` 注册哪些字段？
4. **trainer 与 evaluator**：分别需要写几行（对照 `Gemma4DSparkTrainer` 与 `Gemma4DSparkEvaluator` 各自只有一处覆写）。
5. **风险清单**：至少列出三个「静默出错」风险点（提示：模板与 tokenizer 不匹配、层号越界会在哪一步才爆、torch_compile 是否默认关）。

完成后，用 `diff -u` 再核对一遍 qwen3 与 gemma4 的 modeling.py，确认你清单里「直接复制」的方法确实在 diff 中没有出现。这个清单就是 u7-l1 扩展实战的初稿。

## 6. 本讲小结

- **模型无关层**：`common.py`（构图三件套、`DSparkForwardOutput`、`validate_target_layer_ids`）、`loss.py`、`markov_head.py` 只依赖抽象张量，两个家族共享一份；两个 modeling.py 的 `forward()` 与采样/冻结/置信度方法逐行相同，是这一点的直接证据。
- **模型相关层**：Gemma4 相对 Qwen3 的结构差异全在「HF 组件适配」——`global_head_dim`、缩放系数 1.0、`attention_k_eq_v` 时无 `v_proj`（V=K）、`v_norm`、四个三明治 norm + `layer_scalar`、缩放 embedding（\(\sqrt{d}\)）、Gemma4 原生 RoPE（带 `layer_type`）、logits 可选 tanh softcap。
- **config 派生**：Gemma4 版 `build_draft_config` 比 Qwen3 版多两步——从多模态顶层 config 提取并深拷贝 `text_config`、逐项断言 21 个家族必填字段；另写入 `target_model_type`/`target_text_model_type` 溯源字段（当前仓库内只写不读）。
- **能力边界显式化**：Gemma4 原型用断言声明不支持 MoE 块与 per-layer input gate，缺字段、层号越界都在入口快速失败。
- **配置面差异极小**：两族训练配置只有目标身份、`target_layer_ids`、`mask_token_id`、`trainer_cls`、`torch_compile`、`chat_template` 不同，DSpark 算法超参（block_size=7、num_anchors=512、损失权重等）完全一致，说明算法超参可跨目标迁移。
- **接入改动面**：新家族 = 一个 modeling.py + 一个 config.py + 一份训练配置 + trainer/evaluator 各一个几行的子类 + parser 注册一个聊天模板。

## 7. 下一步学习建议

- **第 5 单元（u5-l1、u5-l2）**：转向 Eagle3 算法——看看「换算法」和本讲「换模型族」是如何正交组合的：Eagle3 也有自己的 `qwen3/`、`gemma4/` 子目录，且它的 gemma4 config 同样有 `get_text_config` 式处理，可检验你是否真的掌握了本讲的分层观。
- **u5-l3**：DFlash 与三算法横向对比，会再次用到本讲的「读配置判断行为」方法。
- **第 6 单元（u6-l4）**：DSpark 评估器如何消费本讲的草稿模型（`forward_dspark_draft_block` 会直接调用 `Gemma4DSparkModel` 的 `_forward_backbone` 与 `sample_draft_tokens`），理解训练构图与推理构图的一致性。
- **u7-l1**：把本讲综合实践的清单落地成完整设计文档，完成「接入新目标模型族」的毕业设计。
- 源码层面建议重读一遍 `deepspec/modeling/dspark/gemma4/modeling.py` 的 `forward()`（[L450-L597](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L450-L597)），边读边问「这一行如果换成另一个家族会变吗」——能准确回答这个问题，说明你已经掌握了 DeepSpec 的分层设计。
