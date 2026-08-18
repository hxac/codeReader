# Qwen3DSparkModel：复用 HF 组件的草稿模型结构

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 DSpark 注意力层为什么对「上下文位置」和「草稿位置」使用**两套不同的 K/V 来源**，并说清拼接顺序与块掩码 KV 布局的对应关系。
2. 逐字段说明草稿模型的 config 是如何由目标模型 config 深拷贝后派生的（层数、`target_layer_ids`、`block_size`、`architectures`、`tie_word_embeddings` 等）。
3. 拿着默认配置 `dspark_qwen3_4b` 的具体数字，从 `input_ids` 一路追踪到 `DSparkForwardOutput` 的每个张量形状，说出一次训练前向到底产出了多少个监督位置。

本讲是 u4-l1 的下半篇：u4-l1 讲清了「构图三件套」（锚点采样、噪声块、非因果掩码）这三个**纯函数**；本讲把它们装进一个真正的 `nn.Module`——`Qwen3DSparkModel`，看清草稿模型本体长什么样。

## 2. 前置知识

本讲默认你已掌握前几讲的内容，这里只做简短回顾与少量新概念补充：

- **u4-l1 的构图三件套**：`sample_anchor_positions` 从 `loss_mask` 里采样锚点；`create_noise_embed` 生成以锚点 token 开头的 mask token 块；`create_dspark_attention_mask` 产出「锚点左侧上下文 ∪ 本块内」的块稀疏掩码。本讲会直接调用它们，不再重复推导。
- **u2-l4 / u2-l5 的目标缓存**：训练时 `target_hidden_states` 不是现场算的，而是从磁盘缓存读来的——把目标模型 `target_layer_ids` 各层输出沿最后一维拼接成宽度 \( K \times H \) 的特征（\( K \) 是层数，\( H \) 是 `hidden_size`）。写入端在 [scripts/data/prepare_target_cache.py:L122-L125](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L122-L125) 做的正是这个 `torch.cat(..., dim=-1)`。
- **u3-l1 的模板方法**：`BaseTrainer.build_models` 负责「读目标 config → 调子类钩子 `_build_draft_model` → 从目标模型拷贝并冻结 embed/lm_head」，子类 `Qwen3DSparkTrainer` 只填钩子。本讲的 `build_draft_config` 就住在这个钩子里。
- **新概念：残差流（residual stream）**。Transformer 每层的输出 = 输入 + 注意力结果 + MLP 结果，像一条不断被读写的「总线」。关键点：DSpark 草稿模型的残差流**只覆盖草稿（噪声）位置**，上下文位置根本不在残差流里——它们只以 K/V 的形式被「查询」。这是理解双源 K/V 的钥匙。
- **新概念：K/V 投影与 GQA**。注意力中 Query/Key/Value 分别由线性层 `q_proj/k_proj/v_proj` 从隐状态投影而来。分组查询注意力（GQA）让多个 Q 头共享少量 KV 头（Qwen3-4B 是 32 个 Q 头共享 8 个 KV 头），\( \text{num\_key\_value\_groups} = 32/8 = 4 \)。
- **新概念：RoPE 位置编码**。旋转位置编码把「绝对位置」编码进 Q/K 向量的旋转角，注意力得分只依赖相对位置差。DSpark 给草稿槽位赋「锚点位置 + t」的真实位置 id（u4-l1 已讲），本讲会看到这些 id 如何流进 `Qwen3RotaryEmbedding`。
- **新概念：RMSNorm**。Qwen 系用的归一化层，比 LayerNorm 少减均值操作，`transformers` 直接提供了现成的 `Qwen3RMSNorm`，本讲模型原样复用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspec/modeling/dspark/qwen3/config.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L1-L61) | `build_draft_config`：从目标 config 深拷贝并派生草稿 config。本讲 4.1 的主角 |
| [deepspec/modeling/dspark/qwen3/modeling.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L1-L533) | 草稿模型本体：注意力层、decoder 层、模型外壳。本讲 4.2、4.3 的主角 |
| [deepspec/modeling/dspark/common.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L1-L309) | 模型无关的构图纯函数与 `DSparkForwardOutput` 数据类合同（u4-l1 已精读大部分） |
| [config/dspark/dspark_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L1-L68) | 默认训练配置：`model_args` 的真实取值来源 |
| [deepspec/trainer/dspark_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L1-L49) | 训练器子类：`_build_draft_model` 与 `run_batch`，把 batch 喂给 forward |
| [deepspec/trainer/base_trainer.py](https://github.com/deepseek-ai-DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L251-L285) | `build_models`：config 加载与冻结 embed/lm_head 的调用现场（u3-l1 已精读） |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**build_draft_config**、**Qwen3DSparkAttention 双源 K/V**、**_forward_backbone 与 forward**。

### 4.1 build_draft_config：草稿 config 如何从目标 config 派生

#### 4.1.1 概念说明

草稿模型的 config 不是手写的一份独立文件，而是**运行时从目标模型 config 派生**的。原因很实际：

1. **接口必须严格对齐**。草稿模型与目标模型共用词表（`vocab_size`）、隐藏宽度相关的归一化超参（`rms_norm_eps`）、注意力头划分（`num_attention_heads`/`num_key_value_heads`/`head_dim`）等几十个字段。手抄一份必然抄漏、抄错；深拷贝目标 config 再改少数字段，天然保证对齐。
2. **architectures 标签是评估侧的派发键**。回顾 u1-l3：`eval.py` 靠 checkpoint 里 `architectures[0]` 查 `EVALUATORS` 字典（见 [eval.py:L10-L16](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L10-L16)，`"Qwen3DSparkModel"` 正是其中一键）。这个标签就是在这里写进 config、随 checkpoint 落盘的。
3. **「算法开关」集中在 model_args**。markov 头、置信度头是否存在，完全由配置字段（`markov_rank`、`confidence_head_alpha`）是否大于 0 推导——这正是 u5-l3 将讲到的「DFlash = 关掉若干开关的 DSpark」的实现基础。

#### 4.1.2 核心流程

```text
输入: target_config (AutoConfig 从 HF 加载的 Qwen3-4B config)
      model_args   (配置文件里的 model 字段, ConfigNode)

1. 读取 num_draft_layers, 构造 layer_types = ["full_attention"] * num_draft_layers
2. 校验 target_layer_ids: 非空、严格递增、取值 ∈ {-1} ∪ [0, 目标层数-1]
3. 由 confidence_head_alpha > 0 推导 enable_confidence_head
   由 markov_rank > 0      推导需要 markov_head_type
4. draft_config = copy.deepcopy(target_config)   # 全字段继承
5. 覆写少数关键字段:
   architectures      = ["Qwen3DSparkModel"]     # 评估侧派发键
   num_target_layers  = 目标层数 (36)             # 记账, 方便校验
   num_hidden_layers  = num_draft_layers (5)      # 草稿层数!
   block_size / mask_token_id / target_layer_ids / num_anchors
   tie_word_embeddings = False                    # 草稿单独持有两份权重
   layer_types / _attn_implementation = "flex_attention"
6. 返回 draft_config
```

#### 4.1.3 源码精读

先看 model_args 的真实取值——默认配置 [config/dspark/dspark_qwen3_4b.py:L10-L30](https://github.com/deepseek-ai-DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L10-L30) 的 `model` 字典：`block_size=7`、`num_draft_layers=5`、`target_layer_ids=[1, 9, 17, 25, 33]`、`mask_token_id=151669`、`num_anchors=512`、`markov_rank=256`、`confidence_head_alpha=1.0`。这份字典就是 `build_draft_config` 的第二个入参。

再看派生函数本体。第一步是算法开关的校验与推导，[deepspec/modeling/dspark/qwen3/config.py:L13-L35](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L13-L35)：先取出目标层数与草稿层数、构造全 `full_attention` 的 `layer_types`，然后调用 `validate_target_layer_ids` 校验层号，最后从 `confidence_head_alpha`/`markov_rank` 是否大于 0 推导两个头的开关，并断言开关打开时必须提供配套字段。

其中 `validate_target_layer_ids` 是模型无关的通用校验，[deepspec/modeling/dspark/common.py:L59-L75](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L59-L75)：要求层号非空、严格递增，且每个值要么是 `-1`（表示 embedding 输出，u2-l5 讲过的哨兵层号），要么落在 \([0, \text{num\_target\_layers}-1]\) 内。对 Qwen3-4B（36 层）来说合法区间是 \(\{-1\} \cup [0, 35]\)，默认的 `[1, 9, 17, 25, 33]` 均匀采样了 5 层、不含最后一层——不含末层是硬约束，评估侧还有一道同样的闸门（`assert_no_final_target_layer`，见 [deepspec/eval/base_evaluator.py:L100-L107](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L100-L107)）。

第二步是深拷贝加覆写，[deepspec/modeling/dspark/qwen3/config.py:L37-L56](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L37-L56)：`copy.deepcopy(target_config)` 后逐项覆写。几个值得划重点的字段：

- `num_hidden_layers = num_draft_layers`：**同一个字段名，含义被偷换了**。目标 config 里它是 36（草稿从它派生注意力头等超参），覆写后变成 5——所以 `Qwen3DSparkModel.__init__` 里 `range(config.num_hidden_layers)` 只建 5 层。原始层数被另存进 `num_target_layers` 留作校验记账。
- `tie_word_embeddings = False`：Qwen3-4B 目标是词嵌入绑定（tied）的，而草稿模型要**分别**持有 `embed_tokens` 和 `lm_head` 两份参数（u3-l1 讲过：两者都从目标拷贝后冻结），所以必须显式解开绑定。
- `_attn_implementation = "flex_attention"`：常量 `TRAIN_ATTN_IMPLEMENTATION` 定义在 [deepspec/modeling/dspark/qwen3/config.py:L6-L6](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L6-L6)。训练强制走 flex_attention（非因果块掩码只有这条路能编译成块稀疏核，见 u4-l1）。
- `architectures = ["Qwen3DSparkModel"]`：写入派发键，这是 checkpoint 保存后被 `eval.py` 读到的值。

派生结果最终由 `Qwen3DSparkTrainer._build_draft_model` 消费，[deepspec/trainer/dspark_trainer.py:L17-L22](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L17-L22)：两行——建 config、建模型。而 `target_config` 的来源在基类，[deepspec/trainer/base_trainer.py:L254-L264](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L254-L264)：`AutoConfig.from_pretrained("Qwen/Qwen3-4B")` 加载，连同 `model_args` 一起传给钩子。

#### 4.1.4 代码实践

**实践目标**：亲手跑一次 `build_draft_config`，产出草稿 config 与 Qwen3-4B 目标 config 的逐字段差异表。

**操作步骤**（以下为示例代码，保存为仓库外任意路径的独立脚本即可，需 `pip install transformers`，只会下载 config.json，不下权重）：

```python
# 示例代码：diff_draft_config.py
from transformers import AutoConfig
from deepspec.modeling.dspark.qwen3.config import build_draft_config
from deepspec.utils.config import to_config_node

model_args = to_config_node(dict(
    target_model_name_or_path="Qwen/Qwen3-4B",
    block_size=7, num_draft_layers=5,
    target_layer_ids=[1, 9, 17, 25, 33],
    mask_token_id=151669, num_anchors=512,
    markov_rank=256, markov_head_type="vanilla",
    confidence_head_alpha=1.0, confidence_head_with_markov=True,
))
target = AutoConfig.from_pretrained("Qwen/Qwen3-4B")
draft = build_draft_config(target_config=target, model_args=model_args)

def flat(cfg, prefix=""):
    out = {}
    for k, v in cfg.to_dict().items():
        out[k] = v
    return out

t, d = flat(target), flat(draft)
for k in sorted(set(t) | set(d)):
    if t.get(k) != d.get(k):
        print(f"{k:32s} target={t.get(k)!r:28s} draft={d.get(k)!r}")
```

**需要观察的现象**：差异行应远少于相同行——只有 `architectures`、`num_hidden_layers`（36→5）、`tie_word_embeddings`（True→False）、`layer_types`、`_attn_implementation` 以及若干新增键（`block_size`、`target_layer_ids`、`num_anchors`、`mask_token_id`、`markov_*`、`confidence_head_*`、`num_target_layers`）出现在输出里；`vocab_size`、`hidden_size`、`num_attention_heads`、`num_key_value_heads`、`head_dim`、`rms_norm_eps` 等全部保持一致。顺手记录目标 config 的 `hidden_size`、`num_hidden_layers`、头划分数值——4.3 的形状推演要用。

**预期结果**：一张约十几行的差异表；`vocab_size=151936`、`hidden_size=2560` 等继承字段原样保留。若 `transformers` 把新增键也收进 `to_dict()`，新增键会以 `target=None, draft=...` 的形式出现，属正常。

**待本地验证**：不同版本 `transformers` 的 `to_dict()` 对自定义字段的序列化行为略有差异，以实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `target_layer_ids` 写成 `[33, 25, 17, 9, 1]`（倒序），会在哪一行、以什么方式失败？

**答案**：在 [common.py:L71-L74](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L71-L74) 的 `assert previous is None or layer_id > previous` 处触发 AssertionError（"target_layer_ids must be strictly increasing."）。要求升序是因为写入端按此顺序 `torch.cat` 拼接特征、读取端必须按同一顺序解释缓存里的 \( K \times H \) 宽张量，乱序会让同一份缓存对应到完全不同的特征。

**练习 2**：为什么草稿 config 必须把 `tie_word_embeddings` 显式设为 `False`？

**答案**：Qwen3-4B 目标模型的输入嵌入与输出头是绑定共享的；而草稿模型需要**独立**的 `embed_tokens` 与 `lm_head` 两个参数张量（虽然初值都从目标拷贝且冻结，u3-l1）。若不解除绑定，`transformers` 在保存/加载 checkpoint 时会把两者当成同一份权重处理，与 `initialize_embeddings_and_head` 分别断言两个形状、分别 `copy_` 的实现（[modeling.py:L277-L281](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L277-L281)）不一致。

### 4.2 Qwen3DSparkAttention：双源 K/V 与 HF 组件复用

#### 4.2.1 概念说明

回顾 u4-l1 的构图：一次前向的 KV 序列是 `上下文（seq_len 个位置）+ 草稿块（num_anchors × block_size 个位置）`。现在问：**每个 KV 位置上的「原始向量」从哪来？**

- 上下文位置：草稿模型在这些位置上**没有残差流**（模型输入只有噪声 embedding，长度是草稿位数量）。但缓存里存着目标模型多层隐状态拼接的特征——它携带了目标模型的全部知识。于是上下文位置的 K/V 直接从 `fc` 投影后的目标特征算出。
- 草稿位置：这些位置的输入是 mask token / 锚点 token 的 embedding，经过草稿层逐层加工，残差流里就是草稿自己的隐状态 `hidden_states`。

所以 K/V 有两个来源：`k_ctx = k_proj(target_hidden_states)` 与 `k_noise = k_proj(hidden_states)`，再沿序列维拼成 \([\text{ctx}; \text{noise}]\)——**拼接顺序与块掩码的 KV 布局严格对应**（`dspark_mask_mod` 里 `kv_idx < seq_len` 判定上下文，见 [common.py:L86-L96](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L86-L96)）。

这样设计还有一个重要性质：**草稿层不需要对 seq_len 个上下文位置做任何前向计算**。上下文以「现成的目标特征 → 一次 k/v 投影」的廉价方式进入注意力，计算量只跟草稿位数量（默认 \( 512 \times 7 = 3584 \)）走，与 seq_len 无关的部分仅剩投影本身。这正是「以目标中间层为输入特征」这一 DSpark 范式在结构上的落点。

复用 HF 组件的动机则很朴素：`Qwen3MLP`、`Qwen3RMSNorm`、`Qwen3RotaryEmbedding`、`Qwen3PreTrainedModel`、`eager_attention_forward`、`rotate_half` 全部直接 import 自 `transformers.models.qwen3.modeling_qwen3`（[modeling.py:L7-L17](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L7-L17)）——草稿层与目标层共享同一套组件实现，行为（归一化、FFN 结构、RoPE 基）逐比特一致，还免费获得 HF 的注意力实现分发（eager/sdpa/flash/flex）。

#### 4.2.2 核心流程

```text
Qwen3DSparkAttention.forward(hidden_states, target_hidden_states, ...)
  # hidden_states:        [bsz, q_len, H]        草稿位残差流(已过 input_layernorm)
  # target_hidden_states: [bsz, seq_len, H]      fc 投影+norm 后的目标特征
  # q_len = num_anchors * block_size

1. q  = q_proj(hidden_states) → q_norm → 转置成 [bsz, heads, q_len, head_dim]
2. k_ctx, v_ctx   = k/v_proj(target_hidden_states)   # 上下文来源
   k_noise, v_noise = k/v_proj(hidden_states)        # 草稿来源
   k = cat([k_ctx, k_noise], dim=1)                  # [bsz, seq_len+q_len, kv_heads*head_dim]
   v = cat([v_ctx, v_noise], dim=1)
   k = k_norm(k) → 转置
3. RoPE: q 取 cos/sin 的最后 q_len 个位置, k 用全部位置
4. (仅评估) past_key_values.update(k, v, layer_idx)
5. (仅 flex + GQA) repeat_interleave 把 8 个 KV 头扩成 32 个
6. attn_fn = 按 config._attn_implementation 从 ALL_ATTENTION_FUNCTIONS 取出
   attn_output = attn_fn(q, k, v, attention_mask=块掩码, ...)
7. return o_proj(attn_output)
```

#### 4.2.3 源码精读

**双源投影与拼接**是本模块最核心的 10 行，[deepspec/modeling/dspark/qwen3/modeling.py:L99-L114](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L99-L114)：`q` 只从草稿隐状态投影；`k_ctx`/`v_ctx` 与 `k_noise`/`v_noise` 用**同一组** `k_proj`/`v_proj` 权重分别投影两种来源后，`torch.cat([k_ctx, k_noise], dim=1)` 沿序列维拼接，再统一 `view` 成 `[bsz, ctx_len+q_len, num_key_value_heads, head_dim]`、过 `k_norm`、转置。注意 `ctx_len` 直接取自 `target_hidden_states.shape[1]`（[L97-L98](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L97-L98)），两种来源共享投影权重意味着「目标特征空间」与「草稿隐状态空间」被刻意压到同一个 K/V 空间里。

**RoPE 的后缀切片**藏在自带的 `apply_rotary_pos_emb` 里，[modeling.py:L34-L40](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L34-L40)：`cos`/`sin` 是用完整 `full_position_ids`（长度 `seq_len + q_len`）在模型外壳里算好的，而 `q` 只有 `q_len` 个位置——所以 `q_embed` 用 `cos[..., -q_len:, :]` 取**末尾** q_len 段（对应草稿位的位置 id），`k_embed` 用全长。这一行小切片是「Q 短、K 长」布局下 RoPE 正确性的关键，也是本文件没有直接复用 HF 原版函数的原因。

**flex + GQA 的兼容处理**，[modeling.py:L120-L128](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L120-L128)：当注意力实现是 `flex_attention` 且 `num_key_value_groups > 1` 时，用 `repeat_interleave` 把 8 个 KV 头复制扩展成 32 个、`reshape` 成完整 MHA 形状——即 flex 路径下不做 GQA，用显存换兼容性。

**注意力实现分发**，[modeling.py:L129-L147](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L129-L147)：默认 `attn_fn = eager_attention_forward`；非 eager 时从 `ALL_ATTENTION_FUNCTIONS[config._attn_implementation]` 取（flex_attention 就是从这张表拿的）。`is_causal` 逐调用传入并镜像到模块属性上（[L132-L136](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L132-L136) 的注释解释了这是 SDPA 派发内核时会读模块属性的原因）；模块默认 `self.is_causal = False`（[L58-L58](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L58-L58)），因为因果性完全由传入的块掩码表达，模型本身**不是**因果的。

**KV cache 分支**只在评估时走到，[modeling.py:L117-L119](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L117-L119)：训练时 `past_key_values=None`、`use_cache=False`，直接跳过。cache 的维护细节（`crop` 等）留到 u6-l4。

**decoder 层外壳**几乎照抄 Qwen3 的 pre-norm 残差结构，[modeling.py:L154-L198](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L154-L198)：`GradientCheckpointingLayer` 基类（激活重算支持）、`Qwen3MLP`、两个 `Qwen3RMSNorm` 全是 HF 原件；唯一的 DSpark 改动是 `self_attn` 换成了双源版本，`target_hidden_states` 作为额外参数一路透传给注意力。

**双源的前提：`fc` + `hidden_norm`** 定义在模型外壳里，[modeling.py:L240-L245](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L240-L245)：`nn.Linear(len(target_layer_ids) * hidden_size, hidden_size, bias=False)` 把宽度 \( K \times H = 5 \times 2560 = 12800 \) 的拼接特征投回 \( H = 2560 \)，随后 `hidden_norm` 做 RMSNorm。这两个模块在 `_forward_backbone` 里**只算一次**、所有层共享（见 4.3.3），每层注意力拿到的 `target_hidden_states` 是同一份投影后的特征。

#### 4.2.4 代码实践

**实践目标**：不看源码，独立复述双源 K/V 的数据流，并用一个最小等价实现验证形状与拼接语义。

**操作步骤**（以下为示例代码，纯 PyTorch，无需 GPU 与模型权重）：

```python
# 示例代码：mini_dual_kv.py
import torch, torch.nn as nn

bsz, seq_len, q_len, H, kv_heads, head_dim = 1, 12, 8, 32, 2, 16
k_proj = nn.Linear(H, kv_heads * head_dim, bias=False)

hidden   = torch.randn(bsz, q_len, H)          # 草稿位残差流
target_h = torch.randn(bsz, seq_len, H)        # fc+norm 后的目标特征

k = torch.cat([k_proj(target_h), k_proj(hidden)], dim=1)
print(k.shape)   # 期望 [1, 20, 32] = [bsz, seq_len+q_len, kv_heads*head_dim]

# 验证拼接语义: 前 seq_len 行必须等于 k_proj(target_h)
assert torch.equal(k[:, :seq_len], k_proj(target_h))
assert torch.equal(k[:, seq_len:], k_proj(hidden))
print("dual-source concat semantics OK")
```

**需要观察的现象**：第一个打印是 `[1, 20, 32]`；两条断言通过，说明「ctx 在前、draft 在后」的顺序与 `kv_idx < seq_len` 的掩码判定一一对应。

**预期结果**：输出 `dual-source concat semantics OK`。然后回到源码，把 [modeling.py:L99-L114](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L99-L114) 与你的 mini 版逐行对照，标出真实实现多出的三件事：`q_norm/k_norm`、RoPE、GQA 头形变换。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 `torch.cat([k_ctx, k_noise], dim=1)`，只保留 `k_noise`，模型还训练得起来吗？会发生什么？

**答案**：训练不会报错（张量形状自洽），但块掩码的 `KV_LEN = seq_len + num_blocks * block_size`（[common.py:L99-L106](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L99-L106)）与 K 的实际长度不再一致，掩码构造/注意力核会因长度不匹配直接失败；即便改成短掩码，草稿块也将看不到任何上文上下文——而 DSpark 的核心假设（草稿以目标中间层特征为条件模仿目标分布）就依赖 ctx K/V 这一路。

**练习 2**：为什么 `q_norm` 在拼接前对 q 单独做，而 `k_norm` 在两种来源拼接**之后**统一做（[L113](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L113)）？

**答案**：RMSNorm 是按最后一维（每个 head_dim 向量）逐位置独立的归一化，先 `view` 再拼接与先拼接再 `view` 后统一归一化在数学上等价（拼接只动序列维，不动 head_dim 维）；统一在拼接后做一次，是为了把两次 kernel 调用合成一次，写法更短。真正不能合并的是 q 与 k：它们使用不同的 norm 模块实例（`q_norm`/`k_norm` 是两份独立参数）。

### 4.3 _forward_backbone 与 forward：一次训练前向的形状追踪

#### 4.3.1 概念说明

`forward` 是训练时的总入口，它做两件事：**(1) 训练构图**——现场采样锚点、造噪声 embedding、算位置 id、建块掩码（复用 u4-l1 的三件套）；**(2) 一次前向出全部监督信号**——单次 backbone 调用同时产出 \( 512 \times 7 = 3584 \) 个草稿位置的 logits、对应的目标标签 `target_ids`、有效位掩码 `eval_mask`、置信度预测，以及对齐的目标分布 `aligned_target_logits`。

所有输出装进 `DSparkForwardOutput`——它是模型与损失函数（u4-l4 的 `compute_dspark_loss`）之间的**形状合同**，注释里逐字段写明形状，见 [common.py:L11-L40](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L11-L40)。损失侧不碰任何中间量，只消费这个 dataclass，这就是「建模与损失解耦」的边界。

`_forward_backbone` 则是外壳内的「纯 Transformer 部分」：fc 投影目标特征 → 算 RoPE → 逐层过 decoder 层 → 末尾 norm。它同时服务训练与评估（评估时多传 `past_key_values`/`use_cache`），这也是噪声流前向与 KV cache 维护共用一份代码的原因。

#### 4.3.2 核心流程

以默认配置 `dspark_qwen3_4b`（\( K{=}5 \) 层目标特征、\( H{=}2560 \)、`num_anchors`\( {=}512 \)、`block_size`\( {=}7 \)、词表 151936）和一个 `local_batch_size=1` 的 batch 为例（seq_len 记为 \( L \)，collate 后最长 4096）：

| 步骤 | 张量 | 形状 | 说明 |
| --- | --- | --- | --- |
| 输入 | `input_ids` | `[1, L]` | int64，来自缓存 |
| 输入 | `target_hidden_states` | `[1, L, 12800]` | 5 层 × 2560 拼接特征 |
| 输入 | `target_last_hidden_states` | `[1, L, 2560]` | 目标末层隐状态 |
| 输入 | `loss_mask` | `[1, L]` | assistant 区段为 1（u2-l2） |
| 构图 | `anchor_positions` / `block_keep_mask` | `[1, 512]` | 每步随机重采样 |
| 构图 | `noise_embedding` | `[1, 3584, 2560]` | `create_noise_embed` |
| 构图 | `full_position_ids` | `[1, L+3584]` | ctx 用 0..L-1，draft 用锚点+t |
| 构图 | 块掩码 BlockMask | Q=3584, KV=L+3584 | `create_dspark_attention_mask` |
| backbone | `output_hidden` | `[1, 3584, 2560]` | **只有草稿位**，无 ctx 位 |
| backbone | `output_hidden_4d` | `[1, 512, 7, 2560]` | reshape 成块结构 |
| 输出 | `draft_logits` | `[1, 512, 7, 151936]` | lm_head + markov 偏置 |
| 输出 | `target_ids` / `eval_mask` | `[1, 512, 7]` | gather 标签 / 有效前缀 |
| 输出 | `aligned_target_logits` | `[1, 512, 7, 151936]` | 目标分布（蒸馏用） |
| 输出 | `confidence_pred` | `[1, 512, 7]` | 置信度头 |

注意一个体量事实：`draft_logits` 单张量 \( 512 \times 7 \times 151936 \times 2 \) 字节（bf16）≈ **1.09 GB**，`aligned_target_logits` 同量级——一次前向的 logits 内存开销以 GB 计，这也是 `local_batch_size=1` 的现实原因之一。

#### 4.3.3 源码精读

**`_forward_backbone`**，[modeling.py:L361-L386](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L361-L386)：四步。① 残差流以 `noise_embedding` 为初值（[L372](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L372) ——embedding 查表已在 `create_noise_embed` 里做完，这里不再查表）；② `target_hidden_states = self.hidden_norm(self.fc(target_hidden_states))`（[L373](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L373) ——\( 12800 \to 2560 \) 投影加归一化，**循环外只算一次**）；③ 用完整 `position_ids` 算 RoPE 的 cos/sin（[L374](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L374)）；④ 逐层调用 decoder 层并返回末尾 `self.norm` 的输出（[L375-L386](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L375-L386)）。

**`forward` 的构图段**，[modeling.py:L398-L427](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L398-L427)：依次调 `sample_anchor_positions`（每步随机重采样——锚点位置是训练时的数据增广）、`create_noise_embed`、拼接 `full_position_ids`（ctx 段是 `arange(seq_len)` 展开，draft 段来自 `create_position_ids`）、`create_dspark_attention_mask`，随后把四者交给 `_forward_backbone`。这五步就是 u4-l1 的全部内容在此处的「调用现场」。

**标签与目标分布的 gather**，[modeling.py:L429-L465](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L429-L465)：先把 `output_hidden` reshape 成 `[bsz, num_blocks, block_size, H]`；再用 `label_indices = anchor_positions + (1..block_size)` 从 `input_ids` 里 gather 出每个槽位要预测的目标 token（`target_ids`，槽位 t 预测锚点后第 t+1 个 token——u4-l1 的约定）；`safe_label_indices` 对越界与 dummy 块做了钳制与清零。若 `target_last_hidden_states` 给定，则以 `safe_label_indices - 1` 为位置 gather 目标末层隐状态、过同一个 `lm_head` 得到 `aligned_target_logits`——**位置 i-1 的目标隐状态预测 token i**，这正是自回归的错位约定，u5-l2 讲 Eagle3 时会再次出现。

**`eval_mask` 与 markov 输入**，[modeling.py:L466-L493](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L466-L493)：`build_eval_mask` 产出「标签存在 ∪ 在监督区 ∪ 块有效」的前缀掩码；`prev_token_ids = cat([anchor_token, target_ids[:, :, :-1]])` 构造每个槽位的「前一 token」（锚点槽用锚点 token 本身）——这是 markov 头（u4-l3）的输入；`draft_logits` 由 `lm_head(output_hidden)` reshape 而来，若 markov 头存在再叠加其块级偏置。

**收尾**，[modeling.py:L495-L525](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L495-L525)：`log_sampler_stats` 以 `no_grad` 记录锚点采样质量的四个指标（u3-l6 的 `add_metric` 通道）；置信度头按 `confidence_head_with_markov` 决定是否把 markov 的 prev embedding 拼进特征（[L504-L516](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L504-L516)）；最后打包返回 `DSparkForwardOutput`。

**调用现场**在训练器里，[deepspec/trainer/dspark_trainer.py:L25-L39](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L25-L39)：`run_batch` 把 collator 产出的四个 batch 字段原样喂给 `self.model(...)`（即本讲 forward），拿到 `DSparkForwardOutput` 后交给 `compute_dspark_loss`。batch 字段与 forward 形参一一同名，可以对照 u2-l6 的 `CacheCollator` 输出核对。

另外补一句 `__init__` 的合同检查，[modeling.py:L207-L224](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L207-L224)：构造时断言 config 必须含 `target_layer_ids`、`mask_token_id`、`num_anchors`、`enable_confidence_head`、`markov_rank` 五个字段，条件字段（`markov_head_type`、`confidence_head_with_markov`）随开关断言——4.1 的派生逻辑正好保证这些断言必然通过，两处代码互为「合同甲乙双方」。embed/lm_head 的拷贝冻结入口 [modeling.py:L270-L283](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L270-L283) 已在 u3-l1 精读，此处不赘述。

#### 4.3.4 代码实践

**实践目标**：完成规格要求的「纸上形状追踪」——不看答案填出整条形状链，再与源码注释核对。

**操作步骤**：

1. 抄下 4.3.2 的表格，但把「形状」列全部遮住，只留步骤名。
2. 以 \( L = 100 \)（seq_len）、默认 `dspark_qwen3_4b` 其余参数（\( K=5 \)、\( H=2560 \)、512 锚点、block_size 7、词表 151936）手工填写每一行。
3. 打开 `DSparkForwardOutput` 的注释 [common.py:L29-L40](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L29-L40) 与 forward 各 gather 处，核对答案。
4. 进阶（可选，需 GPU 且 `torch.nn.attention.flex_attention` 可用）：用 4.1.4 得到的 `draft` config 建一个**随机初始化**的 `Qwen3DSparkModel`（不需要目标权重），把 `num_anchors` 覆盖为 4、喂 `L=48` 的玩具 `input_ids`/`loss_mask`/`target_hidden_states`/`target_last_hidden_states`，打印每个输出的真实形状。

**需要观察的现象**：纸上表填完后，`noise_embedding` 应为 `[1, 28, 2560]`（\( 4 \times 7 \) 个草稿位）、`draft_logits` 应为 `[1, 4, 7, 151936]`；进阶脚本的真实输出与纸上表逐项一致。

**预期结果**：全部一致；其中 `output_hidden` 的形状 `[1, 28, 2560]` 最能说明「残差流只覆盖草稿位」——它不含 \( L \) 个上下文位置。

**待本地验证**：进阶步骤依赖 flex_attention 的 `create_block_mask` 在你的 torch 版本与设备上可用；若不可用，纸上追踪部分（步骤 1-3）不受影响。

#### 4.3.5 小练习与答案

**练习 1**：`aligned_target_logits` 为什么用 `safe_label_indices - 1` 去 gather，而不是 `safe_label_indices` 本身？

**答案**：自回归约定「位置 i-1 的隐状态经 lm_head 预测 token i」。草稿槽位（锚点后第 t 位）要预测的标签是 `input_ids[anchor+t+1]`（即 `safe_label_indices` 指向的位置），而产生这个预测的目标侧证据是**它前一位**（`anchor+t`）的末层隐状态，所以 gather 位置要减一（[modeling.py:L449-L465](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L449-L465)）。若不减一，目标分布会整体「晚一个 token」，L1 蒸馏就会对错位分布。

**练习 2**：一次 forward 的监督位置数是多少？为什么说锚点采样是「免费的数据增广」？

**答案**：\( 512 \times 7 = 3584 \) 个槽位（其中 dummy 块与越界后缀被 `eval_mask` 扣除）。因为 `sample_anchor_positions` 每次 forward 都用新的随机数重采样锚点（[modeling.py:L398-L403](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L398-L403)），同一条训练样本在不同 step、不同 epoch 会被切成不同的 (锚点, 块) 组合，等价于对监督信号做了持续重组合，而磁盘上的缓存字节一个都没变。

**练习 3**：`compute_logits` 只是 `self.lm_head(hidden_states)`（[modeling.py:L289-L290](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L289-L290)），为什么值得单独包一个方法？

**答案**：因为同一个冻结 `lm_head` 被用于两处语义不同的计算——草稿侧 `draft_logits` 与目标侧 `aligned_target_logits`。包成方法后两处调用点都写 `compute_logits(...)`，明确表达「两边用同一个输出头」这一设计决策（这是 DSpark 蒸馏对齐的前提），也让子类（如 Gemma4 变体）能按需覆写。

## 5. 综合实践

把本讲三个模块串成一张「从 config 到损失输入」的完整张量流图，作为本讲的交付物：

1. **跑通 4.1.4 的差异表脚本**，记录 Qwen3-4B 目标 config 的关键数值（`hidden_size`、`num_hidden_layers`、头划分、`vocab_size`、`tie_word_embeddings`）。
2. **画三层流程图**（纸或 drawio 均可）：
   - 第一层（config 派生）：`AutoConfig("Qwen/Qwen3-4B")` → `build_draft_config` 覆写字段 → `Qwen3DSparkModel.__init__` 各子模块（embed、5 个 decoder 层、fc、hidden_norm、norm、markov_head、confidence_head），每个子模块标注参数形状（如 `fc: Linear(12800→2560)`）。
   - 第二层（forward）：从 `run_batch` 的四个 batch 字段开始，按 4.3.2 表格把每个中间张量的形状标到连线上，双源 K/V 的两路分支在注意力框里汇合。
   - 第三层（合同出口）：`DSparkForwardOutput` 七个字段及其形状，箭头指向 `compute_dspark_loss`（预告 u4-l4）。
3. **自检三问**（图上必须能直接回答）：草稿残差流覆盖哪些位置？上下文信息从哪条路进入注意力？`prev_token_ids` 与 `aligned_target_logits` 分别是给哪个下游模块用的？

## 6. 本讲小结

- 草稿 config 由目标 config **深拷贝 + 覆写**而来：`num_hidden_layers` 36→5、`architectures` 写入评估侧派发键、`tie_word_embeddings` 强制解开、`_attn_implementation` 固定 flex_attention；`target_layer_ids` 必须升序且不含末层。
- 注意力层是**双源 K/V**：上下文位置的 K/V 来自 `fc` 投影（\( K \times H \to H \)）+ `hidden_norm` 后的目标缓存特征，草稿位置来自草稿自身残差流，两路共享同一组 `k_proj/v_proj` 权重、按 `[ctx; draft]` 顺序拼接，与块掩码的 KV 布局严格对齐。
- 草稿层大量**复用 HF 原件**（`Qwen3MLP`/`Qwen3RMSNorm`/`Qwen3RotaryEmbedding`/注意力实现分发表），自写部分只有双源注意力与 RoPE 的 Q 后缀切片（`cos[..., -q_len:, :]`，Q 短 K 长布局的正确性关键）。
- 一次训练 forward 现场采样锚点、单次前向产出 \( 512 \times 7 \) 个监督位置；`output_hidden` 只含草稿位；`draft_logits` 与 `aligned_target_logits` 均为 `[bsz, 512, 7, vocab]`（单张量约 1.09 GB bf16），标签按「位置 i-1 预测 token i」错位对齐。
- 模型与损失通过 `DSparkForwardOutput` 这份形状合同解耦，markov 头与置信度头以可插拔字段（`markov_rank`/`confidence_head_alpha`）挂接。

## 7. 下一步学习建议

- **u4-l3（Markov 头）**：本讲两次遇到 `prev_token_ids`（markov 偏置的输入、置信度特征的拼接项），下一讲精读 `markov_head.py` 的低秩双线性偏置与逐 token 采样。
- **u4-l4（DSpark 损失）**：本讲的 `DSparkForwardOutput` 七个字段正是 `compute_dspark_loss` 的全部输入，带着 4.3 的形状表去读损失会非常顺。
- **u4-l5（Gemma4 变体）**：想检验自己是否真正理解本讲，可以预告性地 diff `dspark/qwen3/` 与 `dspark/gemma4/` 两目录——你会发现本讲的三个最小模块在另一族模型上「哪些共享、哪些重写」。
- 若想看双源 K/V 在推理侧的对应物（KV cache 的 `crop` 维护、块提议复用同一套层），可提前浏览 [deepspec/eval/dspark/draft_ops.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L1-L1)，正式精读在 u6-l4。
