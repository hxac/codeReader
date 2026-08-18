# u6-l4 DSpark 评估器：块提议、草稿缓存与上下文更新

## 1. 本讲目标

学完本讲，你应该能够：

1. 追踪一次 DSpark 提议的完整路径：从构造 `mask token` 噪声块，到一次草稿前向，再到产出 `draft_probs` 与 `verify_input_ids`。
2. 解释 `forward_dspark_draft_block` 里 `position_ids` 切片公式 `[:, get_seq_length() : start + block_size]` 的含义，以及草稿 KV cache 与目标 KV cache 的「对偶维护」不变式。
3. 说明 `_update` 如何从验证输出中只切出「被接受前缀 + 锚点」的目标隐状态，供下一轮草稿前向使用。
4. 描述置信度头如何在提议阶段提前截断低置信前缀（`_confident_prefix_length`），以及空提议如何退化为一轮纯目标采样。
5. 亲手回答本讲的核心思考题：`past_key_values_draft.crop(start)` 为什么必须发生在取回 `block_hidden` 之后。

本讲是第 6 单元的第 4 篇。u6-l2 讲了投机解码主循环的「骨架」，u6-l3 讲了拒绝采样验证的「数学」，本讲终于打开 DSpark 这个「算法插件」本身——看它如何实现四个钩子中的三个（`init_context` / `propose` / `update`），第四个 `post_verify`（置信度校准记录）留到 u6-l5 精读。

## 2. 前置知识

### 2.1 主循环的钩子合同（承接 u6-l2）

`generate_decoding_sample` 只做算法无关的事：prefill、验证前向、游标推进、统计收集。算法差异全部下沉到四个钩子，钩子之间用两个数据类做合同：

- [deepspec/eval/base_evaluator.py:167-171](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L167-L171)：`DraftProposal` 携带 `draft_token_count`（提议了几个草稿 token）、`verify_input_ids`（喂给目标模型验证的输入，首元素必须是当前锚点 token）、`draft_probs`（草稿侧的提议概率分布，供拒绝采样用）。
- [deepspec/eval/base_evaluator.py:174-183](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L174-L183)：`VerificationResult` 携带 `target_output`（目标模型前向的完整输出，含各层隐状态）、`accepted_draft_tokens`、`next_token` 等。

主循环的两个不变式（本讲反复用到）：

- 游标 `start` 永远指向「最后一个已提交 token」的位置，`output_ids[:, start]` 就是下一轮提议的锚点；
- 轮末 `past_key_values_target.crop(start)` 之后，目标 KV cache 长度恒等于 `start`，对应「除锚点外的全部已提交前缀」。

每轮提交 \(a + 1\) 个 token（\(a\) 个被接受草稿 + 1 个 residual/bonus token），所以新 `start` = 旧 `start` + \(a\) + 1。

### 2.2 DSpark 的训练构图（承接 u4-l1 / u4-l2）

DSpark 的训练方式决定了它的推理方式，三个要点在本讲全部复现：

1. **锚点 + 噪声块**：以某个已确认 token 为锚点，构造 `block_size` 个位置的输入，槽位 0 放锚点 token 本身，槽位 1..B-1 全放 `mask_token_id`（见 [deepspec/modeling/dspark/common.py:264-294](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L264-L294) 的 `create_noise_embed`）。
2. **位置编号**：槽位 \(t\) 的 RoPE 位置 = 锚点位置 \(p\) + \(t\)，它预测的是位置 \(p+t+1\) 上的 token（见 [deepspec/modeling/dspark/common.py:251-261](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L251-L261) 的 `create_position_ids`，以及 u4-l1 的推导）。
3. **双源 K/V**：草稿注意力的上下文位置没有草稿残差流，其 K/V 来自「目标模型隐状态特征」经 `fc` 投影与 `hidden_norm` 后再做 `k_proj/v_proj`；草稿块位置的 K/V 来自草稿自身的残差流；两路拼在一起（见 [deepspec/modeling/dspark/qwen3/modeling.py:103-112](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L103-L112)）。上下文特征由 [deepspec/modeling/dspark/common.py:52-56](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L52-L56) 的 `extract_context_feature` 从 `output_hidden_states` 元组里抽取拼接：`-1` 层取索引 0（embedding 输出），第 \(i\) 层取索引 \(i+1\)。

### 2.3 DynamicCache 的三个原语

本讲的「状态维护」全部建立在 `transformers.DynamicCache` 的三个操作上：

- `update(k, v, layer_idx, ...)`: 把新的 K/V **追加**到缓存末尾，返回「旧缓存 + 新增」拼成的全长 K/V；
- `get_seq_length()`: 返回缓存中当前的 token 数；
- `crop(max_length)`: 把每层 K/V 截断到前 `max_length` 个位置。

### 2.4 拒绝采样对 `draft_probs` 的要求（承接 u6-l3）

验证端按 \(\min(1, p_{\text{target}}/p_{\text{draft}})\) 接受草稿 token。要让输出分布无损，`draft_probs` 必须是**提议时真正采样的那个分布**——采样后又在别处修改 logits 是不行的。这个约束直接决定了 4.2 节的一个关键设计：传给验证端的是 markov 头修正后的 logits，而不是冻结 `lm_head` 的原始 logits。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [deepspec/eval/dspark/evaluator.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py) | `Qwen3DSparkEvaluator`：实现 `_init_context` / `_propose` / `_update` / `_post_verify` 四钩子，并组装 `generate_one_sample` |
| [deepspec/eval/dspark/draft_ops.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py) | 草稿运算层：`forward_dspark_draft_block`（一次前向产出一块）、`build_dspark_proposal`（采样 + 置信度早停）、`DSparkDraftProposal`（扩展数据类） |
| [deepspec/eval/base_evaluator.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py) | u6-l2/u6-l3 已精读的主循环与验证；本讲只引用其钩子调用点与 crop 语句 |
| [deepspec/modeling/dspark/qwen3/modeling.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py) | u4-l2 已精读的草稿模型；本讲用到 `_forward_backbone`、`compute_logits`、`sample_draft_tokens`、`predict_confidence_step` |
| [deepspec/modeling/dspark/common.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py) | `extract_context_feature`（隐状态抽取）与训练侧构图函数（对照用） |
| [deepspec/eval/dspark/confidence_head.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py) | `ConfidenceHeadRecorder`，只在本讲边界处出现，细节归 u6-l5 |

先看 evaluator 的骨架全貌，建立「谁在什么时候被调用」的框架：

[deepspec/eval/dspark/evaluator.py:162-179](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L162-L179) 把四个钩子原样注入 `generate_decoding_sample`——DSpark 评估器自己**没有**解码循环，它只是给 u6-l2 的通用循环提供插件：

```python
return generate_decoding_sample(
    target_model=self.target_model,
    input_ids=input_ids,
    ...
    init_context=self._init_context,
    propose=self._propose,
    update=self._update,
    post_verify=self._post_verify,
)
```

另外两个入口级细节：`max_proposal_tokens` 属性直接等于草稿模型的 `block_size`（[deepspec/eval/dspark/evaluator.py:40-42](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L40-L42)）；Gemma4 评估器只有一行——换 `draft_model_cls`（[deepspec/eval/dspark/evaluator.py:224-226](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L224-L226)），这正是 u4-l5「模型无关层 + 模型相关层」分层在评估侧的兑现：钩子逻辑完全复用。

还有一个防御性闸门值得注意：[deepspec/eval/dspark/evaluator.py:68-83](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L68-L83) 的 `build_models` 在加载两个模型后调用 `assert_no_final_target_layer`。因为评估侧依赖 `output_hidden_states=True` 取中间层（见 [deepspec/eval/base_evaluator.py:100-112](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L100-L112) 的注释），而 transformers 在末层槽位存的是**过 final norm 的隐状态**，与训练时 target cache 的「raw 层输出」语义不一致——这是 u2-l5 讲过的同一颗雷，评估侧在入口处再排一次。

## 4. 核心概念与源码讲解

### 4.1 forward_dspark_draft_block：一次前向产出一整块草稿

#### 4.1.1 概念说明

Eagle3 式投机解码每轮需要链式跑 7 次单 token 草稿前向（见 u5-l1），而 DSpark 的核心优势是：**一次长度为 \(B\)（block_size）的并行前向，同时产出整块草稿的全部隐藏状态**。`forward_dspark_draft_block` 就是这次前向的封装，它要同时解决三件事：

1. 给 \(B\) 个草稿槽位喂输入（槽 0 = 锚点 token，其余 = mask token）；
2. 给「上下文键」和「草稿键」分别赋予正确的 RoPE 位置；
3. 维护草稿自己的 KV cache——既要复用历史上下文的 K/V，又要在前向结束后把一次性草稿 K/V 裁掉。

为什么草稿还需要自己的 KV cache？因为双源 K/V 设计里，上下文位置的 K/V 是「草稿侧对目标隐状态特征的投影」（经 `fc` → `hidden_norm` → `k_proj/v_proj`），逐层逐 token 计算并不便宜；已提交前缀这部分每轮都一样，理应缓存复用，而不是每轮对整个上下文重算。

#### 4.1.2 核心流程

设当前游标为 `start`（锚点 token 在 `output_ids` 中的位置），块长 \(B\)，上一轮结束后的草稿缓存长度为 \(c\)（第 1 轮 \(c = 0\)，之后 \(c\) = 上一轮的 `start`）。一次 `forward_dspark_draft_block` 的流程：

1. 构造 `draft_input_ids`：形状 \((1, B)\)，先全部填 `mask_token_id`，再把槽 0 覆盖为锚点 token `output_ids[:, start]`。
2. **位置切片**：取 `position_ids[:, c : start + B]`。切片长度 = \(start + B - c\)。由于本轮新提交的 token 恰好占据位置 \(c \dots start-1\)（共 \(a_{prev}+1\) 个），这个切片**正好同时覆盖**：
   - 新提交 token 的位置 \(c \dots start-1\)——给本轮新算的上下文键做 RoPE 用；
   - 草稿块的位置 \(start \dots start+B-1\)——给块的 query 和键做 RoPE 用。
3. 调 `model._forward_backbone`：`noise_embedding = embed_tokens(draft_input_ids)`（query 长度 \(B\)），`target_hidden_states` 是 `context.target_hidden_states`（长度 \(start - c\)，即上一轮 `_update` 存下的「锚点 + 被接受前缀」特征），`use_cache=True`，`is_causal=False`，`attention_mask=None`。
4. 前向内部（见 4.1.3）：`k = cat([k_ctx, k_noise])` 经 `past_key_values.update` 追加进缓存，注意力读到全长 K/V = \([c \text{ 个历史上下文}] + [start-c \text{ 个新上下文}] + [B \text{ 个块内}]\)，长度 \(start + B\)，与位置切片一一对应。
5. 前向返回 `block_hidden`（形状 \((1, B, H)\)）后，执行 `past_key_values_draft.crop(start)`，把缓存截回 `start`，**丢弃块内 \(B\) 个一次性 K/V，保留新追加的 \(start-c\) 个上下文 K/V**。

三条不变式（本讲最重要的一张图）：

- **切片长度恒等式**：位置切片长度 \(= (start - c) + B\) = 本轮上下文特征长度 + 块长。这给了一个自查口诀：切片长度减去 \(B\)，必须正好等于 `context.target_hidden_states` 的序列长度。
- **双缓存对偶**：轮末目标缓存被主循环 `crop(start)`（[deepspec/eval/base_evaluator.py:425](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L425)），草稿缓存被本函数 `crop(start)`——两条缓存长度同步收敛到同一个 `start`，都表示「已提交前缀中除锚点以外的全部位置」。区别只在内容：目标缓存存目标模型自己的 K/V，草稿缓存存草稿侧投影出的上下文 K/V。
- **训练掩码的退化**：训练时用 flex BlockMask 表达「锚点左侧上下文 ∪ 本块内」（u4-l1）；评估时缓存里只有一段上下文（全部 \(< start\)）加一个块，`attention_mask=None, is_causal=False` 的全注意力**恰好就是**同一个掩码——块间隔离由「缓存里永远只有一个块」自动保证，锚点不进上下文缓存、只作块的槽 0，也与训练的 `kv_idx < anchor_pos` 严格一致。训练构图 = 推理构图，在这里落到了字节级。

#### 4.1.3 源码精读

提议的入口在 evaluator 一侧，先构造噪声块再调 `forward_dspark_draft_block`：

[deepspec/eval/dspark/evaluator.py:99-132](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L99-L132)：`_propose` 钩子——用 `torch.full` 造 mask token 流、把槽 0 换成锚点 token，随后一次前向 + 一次提议组装（组装细节在 4.2）：

```python
draft_input_ids = torch.full(
    (output_ids.size(0), self.max_proposal_tokens),
    int(model.mask_token_id),
    dtype=torch.long,
    device=output_ids.device,
)
draft_input_ids[:, 0] = output_ids[:, start]      # 锚点放槽 0
block_hidden = forward_dspark_draft_block(
    model, draft_input_ids=draft_input_ids,
    position_ids=position_ids,
    past_key_values_draft=context.past_key_values_draft,
    target_hidden_states=context.target_hidden_states,
    start=start, block_size=self.max_proposal_tokens,
)
```

注意两点：其一，这里的构图与训练侧 `create_noise_embed`（[deepspec/modeling/dspark/common.py:276-293](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L276-L293)）完全同构——mask 流 + 块头放锚点；其二，`_propose` 的形参里有 `stop_token_ids` 但函数体没有用到，DSpark 的提前停止走置信度头（4.2），停止 token 的截断交给验证端。

接着是本模块的主角：

[deepspec/eval/dspark/draft_ops.py:22-45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L22-L45)：`forward_dspark_draft_block`——位置切片、一次前向、缓存裁剪，全函数仅 20 行：

```python
draft_position_ids = position_ids[
    :, past_key_values_draft.get_seq_length() : start + block_size
]
block_hidden = model._forward_backbone(
    target_hidden_states=target_hidden_states,
    noise_embedding=model.embed_tokens(draft_input_ids),
    position_ids=draft_position_ids,
    attention_mask=None,
    past_key_values=past_key_values_draft,
    use_cache=True,
    is_causal=False,
)
past_key_values_draft.crop(start)
return block_hidden
```

- 第 32-34 行的切片下界是 `get_seq_length()`（缓存当前长度 \(c\)），上界是 `start + block_size`——正如 4.1.2 所推，切出的位置序列同时服务新上下文键与块内 query/键。
- 传给 `_forward_backbone` 的 `attention_mask=None` + `is_causal=False`：草稿模型在评估时用 `sdpa`（[deepspec/eval/dspark/evaluator.py:33](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L33) 的 `EVAL_ATTN_IMPLEMENTATION`），训练时的 flex BlockMask 在「单块 + 纯历史上下文」的场景下退化为全注意力。
- 第 44 行 `crop(start)` 紧跟在取回 `block_hidden` 之后，为什么顺序不能颠倒，见综合实践的思考题。

位置切片如何变成正确的 RoPE？关键在草稿模型的两段代码：

[deepspec/modeling/dspark/qwen3/modeling.py:361-386](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L361-L386)：`_forward_backbone` 先把 \(K \times H\) 的上下文特征投影回 \(H\) 并做 norm，再以 `position_ids` 计算 RoPE：

```python
hidden_states = noise_embedding
target_hidden_states = self.hidden_norm(self.fc(target_hidden_states))
position_embeddings = self.rotary_emb(hidden_states, position_ids)
for layer in self.layers:
    hidden_states = layer(hidden_states=hidden_states,
                          target_hidden_states=target_hidden_states, ...)
```

[deepspec/modeling/dspark/qwen3/modeling.py:34-40](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L34-L40)：`apply_rotary_pos_emb` 对 query 只取 cos/sin 的**后缀** \(q\_len\) 个位置，对 key 用全部——query 长度是 \(B\)（只有块），而 cos/sin 长度是 \(start+B-c\)（新上下文 + 块），后缀切片恰好让块内 query 拿到位置 \(start \dots start+B-1\)：

```python
q_len = q.size(-2)
q_embed = (q * cos[..., -q_len:, :]) + (rotate_half(q) * sin[..., -q_len:, :])
k_embed = (k * cos) + (rotate_half(k) * sin)
```

[deepspec/modeling/dspark/qwen3/modeling.py:103-119](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L103-L119)：注意力层里的双源 K/V 与缓存写入——上下文键/值来自投影后的目标特征，块内键/值来自草稿残差流，拼接后**整段**追加进 `past_key_values`：

```python
k_ctx = self.k_proj(target_hidden_states)      # 新提交 token 的上下文键
k_noise = self.k_proj(hidden_states)           # 块内草稿键
k = torch.cat([k_ctx, k_noise], dim=1)
...
if past_key_values is not None:
    k, v = past_key_values.update(k, v, self.layer_idx, cache_kwargs)
```

`update` 返回的是「缓存中历史 \(c\) 个 + 新追加 \(start-c+B\) 个」的全长 K/V，注意力因此能同时看见历史上下文、新上下文与当前块——这就是位置切片与缓存布局严格对齐的原因。

#### 4.1.4 代码实践

**实践目标**：用一组具体数字手推 `forward_dspark_draft_block` 的位置切片与缓存长度变化，验证 4.1.2 的三条不变式。

**操作步骤**（纯纸笔 + 对照源码）：

1. 设定：prompt 长度 \(N = 10\)，`block_size` \(B = 7\)。第 1 轮锚点位置 `start = 10`（prefill 后直采的首 token）。
2. 对照 [deepspec/eval/dspark/draft_ops.py:32-34](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L32-L34)，填写第 1 轮：`get_seq_length()` 是多少？切片是 `position_ids[:, ?: ?]`？切片长度是多少？
3. 假设第 1 轮接受 \(a = 3\) 个草稿 token，主循环按 [deepspec/eval/base_evaluator.py:411-425](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L411-L425) 提交并推进 `start`，再填第 2 轮同一行表格。
4. 假设第 2 轮 \(a = 0\)（一个草稿都没接受，只提交 bonus token），填第 3 轮。

**需要观察的现象 / 预期结果**（答案表，可与 5. 综合实践对照）：

| 轮次 | start | propose 前草稿缓存长 \(c\) | 位置切片 | 切片长度 | `target_hidden_states` 长度 | forward 后缓存长 | crop 后缓存长 |
|---|---|---|---|---|---|---|---|
| 1 | 10 | 0 | `[0 : 17)` | 17 | 10（整个 prompt） | 17 | 10 |
| 2 | 14 | 10 | `[10 : 21)` | 11 | 4（锚点 + 3 个接受草稿） | 21 | 14 |
| 3 | 15 | 14 | `[14 : 22)` | 8 | 1（仅新锚点） | 22 | 15 |

自查两点：每一行的「切片长度 − 7」都等于 `target_hidden_states` 长度；每一行 crop 后缓存长都等于该轮的 `start`，且与目标缓存轮末长度（[deepspec/eval/base_evaluator.py:425](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L425) 的 `crop(start)`）完全同步。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `forward_dspark_draft_block` 里位置切片的下界从 `get_seq_length()` 改成 `start`，第 2 轮会发生什么？

**答案**：第 2 轮 `get_seq_length()`=10 而 `start`=14，切片会变成 `[14 : 21)`，长度 7。这样 cos/sin 只覆盖位置 14..20，但 key 的长度是 \(4 + 7 = 11\)（新上下文 + 块），RoPE 广播会直接形状失配报错；即便形状碰巧对上，新上下文键（位置 10..13 的 token）也会被赋予 14..17 的错误位置，注意力距离全部错乱。下界必须是「缓存里已有的位置数」。

**练习 2**：训练时 DSpark 用 `create_dspark_attention_mask` 生成块稀疏掩码，评估时却传 `attention_mask=None`，为什么结果仍然正确？

**答案**：训练掩码的可视范围是「锚点左侧上下文 ∪ 本块内，块间隔离」。评估时：(1) 缓存中的上下文全部位于 `start` 之前，天然都在「锚点左侧」；(2) 每轮前向后 `crop(start)` 把块内 K/V 全部裁掉，缓存里任何时刻最多只有一个块，块间隔离自动成立；(3) 块内全可见与训练一致。所以全注意力与训练掩码在此场景下等价。

**练习 3**：草稿缓存里存的上下文 K/V，与目标缓存里的 K/V 是同一份张量吗？

**答案**：不是。目标缓存存目标模型自身各层的 K/V；草稿缓存存的是草稿侧对「`fc` + `hidden_norm` 后的目标隐状态特征」再做 `k_proj/v_proj` 得到的 K/V。两者长度同步（轮末都等于 `start`），但数值与语义都不同——这正是「草稿以目标中间层为输入特征」这一设计在缓存层面的体现。

### 4.2 build_dspark_proposal 与置信度早停

#### 4.2.1 概念说明

`forward_dspark_draft_block` 只给出块的隐藏状态，`build_dspark_proposal` 负责把它变成一份合法的 `DraftProposal`：算 logits → 采样出 \(B\) 个候选 token → （可选）用置信度头把低置信的尾部截掉 → 打包 `verify_input_ids` 与 `draft_probs`。

两个设计点值得先讲直觉：

1. **采样必须用「实际采样分布」回报验证端**。DSpark 的块内 token 依赖由 markov 头逐 token 修正（u4-l3），因此拒绝采样所需的 \(q(x)\) 必须取自修正后的 logits，而不是冻结 `lm_head` 的原始输出。`sample_draft_tokens` 恰好把「采样出的 token」和「采样实际用的 logits」成对返回。
2. **置信度早停是「主动缩短提议」**。置信度头预测每个槽位「这个 token 会被目标模型接受」的概率（训练目标见 u4-l4）；若某个槽位置信度低于阈值，它及其后继都不可信，直接截断，只把可信前缀交给目标模型验证。截断到 0 就是一轮「放弃投机」：只验证锚点本身，目标模型直采一个 bonus token 兜底，保证循环仍能每轮推进 1 个 token。

#### 4.2.2 核心流程

```
输入: draft_input_ids (1,B)  [锚点 + mask 流]
      block_hidden (1,B,H)   [一次前向的块隐状态]

base_logits = lm_head(block_hidden)               # 冻结头算基础 logits
sampled_tokens, draft_logits = sample_draft_tokens(...)   # markov 链式采样, 返回修正后 logits

k = B                                            # 默认整块提议
if 置信度头存在:
    prev = [锚点, sampled[:-1]]                   # 与训练同构的前驱序列
    conf = sigmoid(置信度头(block_hidden, prev))   # (1,B)
    k = 首个 conf < 阈值 的下标                    # 阈值<=0 时 k=B; 槽0即低于阈值则 k=0
if k == 0: 返回空提议(只含锚点, draft_probs=None)
verify_input_ids = [锚点, sampled[:k]]            # 长度 k+1
draft_probs = softmax(draft_logits[:, :k] / T)    # 与验证端同温度同函数
返回 DSparkDraftProposal(k, verify_input_ids, draft_probs, conf[:, :k])
```

空提议的下游行为（连接 u6-l2/u6-l3）：`draft_token_count=0` 时验证端跳过拒绝采样，`next_token` 由目标分布直采（[deepspec/eval/base_evaluator.py:284-285](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L284-L285)），轮统计里记一次长度 0 的提议。代价是仍要做一次长度为 1 的目标前向（锚点必须进目标 KV cache），收益是避免把注定被拒的 6 个 token 也塞给目标模型做更长的前向。

#### 4.2.3 源码精读

[deepspec/eval/dspark/draft_ops.py:96-113](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L96-L113)：`build_dspark_proposal` 的前半段——冻结 `lm_head` 算基础 logits，再交给模型采样。`sample_draft_tokens`（[deepspec/modeling/dspark/qwen3/modeling.py:309-333](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L309-L333)）在有 markov 头时走 `sample_block_tokens` 链式采样（[deepspec/modeling/dspark/markov_head.py:55-90](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/markov_head.py#L55-L90)：每一步「修正 logits → 采样 → 把采到的 token 变成下一步的前驱」），并返回**采样实际使用的** corrected logits；无 markov 头（DFlash）则退化为对 base_logits 的独立采样：

```python
proposal_hidden_states = block_hidden[:, :block_size, :]
base_draft_logits = model.compute_logits(proposal_hidden_states)
sampled_tokens, draft_logits = model.sample_draft_tokens(
    base_draft_logits,
    first_prev_token_ids=draft_input_ids[:, 0],
    temperature=temperature,
    hidden_states=proposal_hidden_states,
)
```

[deepspec/eval/dspark/draft_ops.py:115-134](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L115-L134)：置信度预测与早停。置信度头存在时先预测 `(1, B)` 的 logits，再决定可信前缀长度；长度为 0 时返回空提议（`_empty_dspark_proposal`，[deepspec/eval/dspark/draft_ops.py:48-54](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L48-L54)，其 `verify_input_ids` 只含锚点、`draft_probs=None`）：

```python
proposal_draft_tokens = int(block_size)
if model.confidence_head is not None:
    confidence_logits = _predict_confidence_logits(...)
    if confidence_logits is None:
        return _empty_dspark_proposal(draft_input_ids)
    proposal_draft_tokens = _confident_prefix_length(
        confidence_logits, block_size=block_size,
        threshold=float(confidence_threshold),
    )
if proposal_draft_tokens == 0:
    return _empty_dspark_proposal(draft_input_ids)
```

（中间那个 `confidence_logits is None` 分支是防御式检查：`predict_confidence_step` 只有在头不存在时才返回 `None`（[deepspec/modeling/dspark/qwen3/modeling.py:297-298](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L297-L298)），而外层已经确认头存在，正常路径走不到这里。）

[deepspec/eval/dspark/draft_ops.py:82-93](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L82-L93)：`_confident_prefix_length`——阈值非正则整块提议；否则找**第一个**低于阈值的槽位下标，前缀到此为止。注意 `below_threshold[0]` 取的是 batch 维 0（评估恒为 bsz=1，函数入口也有断言，见 [deepspec/eval/dspark/draft_ops.py:105](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L105)）：

```python
if threshold <= 0.0:
    return int(block_size)
below_threshold = confidence_logits.sigmoid() < threshold
if not bool(below_threshold[0].any().item()):
    return int(block_size)
return int(torch.nonzero(below_threshold[0], as_tuple=False)[0].item())
```

[deepspec/eval/dspark/draft_ops.py:57-79](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L57-L79)：`_predict_confidence_logits`——前驱序列拼法是「锚点 + 已采样 token 右移一位」，与训练侧 `prev_token_ids = [anchor_token, target_ids[:, :, :-1]]`（[deepspec/modeling/dspark/qwen3/modeling.py:478-481](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L478-L481)）同构，只是把教师强制的真值换成了草稿自己采出的 token：

```python
prev_token_ids = torch.cat(
    [draft_input_ids[:, :1], sampled_tokens[:, :-1]],
    dim=1,
)
```

[deepspec/eval/dspark/draft_ops.py:136-153](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L136-L153)：组装返回值。`verify_input_ids` = 锚点 + 可信前缀的采样 token（长度 \(k+1\)）；`draft_probs` 用与验证端**完全相同**的函数与温度归一（验证端见 [deepspec/eval/base_evaluator.py:229](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L229)，同为 `logits_to_probs(logits, temperature)`，函数定义在 [deepspec/utils/sampling.py:6-11](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/sampling.py#L6-L11)）——这是 u6-l3 强调的「同温度、同函数」无损条件在提议端的落点；置信度 logits 也同步截到 \(k\) 长，供 `_post_verify` 的校准记录消费：

```python
verify_input_ids = torch.cat(
    [draft_input_ids[:, :1], sampled_tokens[:, :proposal_draft_tokens]],
    dim=1,
)
draft_probs = logits_to_probs(draft_logits[:, :proposal_draft_tokens, :], temperature)
```

最后，[deepspec/eval/dspark/draft_ops.py:17-19](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L17-L19) 的 `DSparkDraftProposal` 在基类合同上只加一个字段 `confidence_logits`——扩展而不破坏合同，主循环对此一无所知，只有 DSpark 自己的 `_post_verify` 会用（它先断言类型，见 [deepspec/eval/dspark/evaluator.py:149-160](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L149-L160)，把 proposal 与 verification 成对交给 `ConfidenceHeadRecorder.observe`；记录器仅在置信度头存在且 `confidence_threshold == 0.0` 时创建——阈值非 0 时接受长度被截断策略污染，校准统计会带偏，细节归 u6-l5）。

#### 4.2.4 代码实践

**实践目标**：沿源码追踪 `draft_probs` 的诞生链，并解释「为什么不能用 base_draft_logits 算 draft_probs」。

**操作步骤**：

1. 从 [deepspec/eval/dspark/draft_ops.py:107](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L107) 的 `compute_logits`（实现即 `lm_head(hidden)`，[deepspec/modeling/dspark/qwen3/modeling.py:289-290](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L289-L290)）出发，依次打开 `sample_draft_tokens` → `sample_block_tokens`，确认「返回的第二个值是每一步实际用于采样的 step_logits」。
2. 再看 [deepspec/eval/dspark/draft_ops.py:140-143](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L140-L143)：`draft_probs` 用的正是这个返回值。
3. 对照 [deepspec/eval/base_evaluator.py:244-257](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L244-L257)：验证端用 `draft_probs` 在「已提议 token」上取概率算接受比。

**需要观察的现象 / 预期结果**：链路为 `block_hidden → lm_head → base_logits → markov 逐步修正 → 采样 + corrected_logits → logits_to_probs → draft_probs`。若改用 base_logits，则拒绝采样里的 \(q(x)\) 不再是真实提议分布：对 DSpark（markov 修正显著）接受概率计算系统性偏差，输出分布不再与目标一致——无损性被破坏；对 DFlash（无 markov 头）两者本来相同，所以 DFlash 不受影响。

#### 4.2.5 小练习与答案

**练习 1**：`confidence_threshold = 0.6`，某轮 7 个槽位的置信度（sigmoid 后）为 `[0.9, 0.8, 0.55, 0.95, 0.9, 0.8, 0.7]`，提议几个 token？

**答案**：2 个。`below_threshold = [F,F,T,F,F,F,F]`，第一个 True 的下标是 2，`_confident_prefix_length` 返回 2——注意返回值语义是「截断位置」，随后 `proposal_draft_tokens` 用它作为保留长度，`verify_input_ids = [锚点, sampled[:2]]`，即提议 2 个草稿 token、验证长度 3。若你数成了「保留到下标 2 共 3 个」，重看 [deepspec/eval/dspark/draft_ops.py:136-139](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L136-L139) 的切片 `[:, :proposal_draft_tokens]`：下标 2 的槽位（0.55）本身被丢弃。

**练习 2**：为什么空提议的 `draft_probs` 可以是 `None`？主循环会不会因此崩掉？

**答案**：验证端只有在 `draft_token_count > 0` 时才访问 `draft_probs`（[deepspec/eval/base_evaluator.py:241-242](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L241-L242) 有断言守门），空提议直接走 `accepted_draft_tokens = 0` 分支，`next_token` 由目标分布直采。所以 `None` 是合法值，且空提议仍推动 `start` 前进 1（bonus token）。

**练习 3**：置信度早停截掉的是「提议长度」，它会改变输出文本的分布吗？

**答案**：不会改变每轮已提交 token 的正确性——被截断的部分根本不参与验证；提交的 token 仍经由「接受 + residual/bonus 采样」保证分布与目标一致（u6-l3 的无损性证明不依赖提议如何生成）。它改变的是**速度**：更短的验证前向、更少的无效计算，代价是可能放弃本来会被接受的部分草稿，使平均接受长度下降。这正是「草稿质量只决定速度、不决定正确性」的一个具体体现。

### 4.3 _init_context / _update：上下文特征与双缓存状态维护

#### 4.3.1 概念说明

主循环把跨轮状态全部封在 `context` 这个 `SimpleNamespace` 里（u6-l2 的设计），DSpark 给它装两个字段：

- `past_key_values_draft`：草稿 KV cache，生命周期贯穿整个样本的解码；
- `target_hidden_states`：**最近一段**未入缓存 token 的目标隐状态特征——它是一个「滚动窗口」，每轮被 `_update` 整体替换。

第二个字段是理解 `_update` 的关键。4.1 节说过：草稿缓存长度在轮末等于当轮 `start`，而下一轮的位置切片需要从「缓存长度」补到「新 start」，这段缺口的 K/V 必须现算，原料就是 `target_hidden_states`。`_update` 的职责是从验证输出里**精确切出这段缺口**：

- 验证前向跑的是 `verify_input_ids`（锚点 + 全部草稿 token），其 `hidden_states` 覆盖位置 `start .. start+k`；
- 本轮新提交（且尚未进入草稿缓存）的是位置 `start .. start+a`（锚点 + 被接受草稿），共 \(a+1\) 个；
- 所以切片 `[:, :a+1]` 一次到位，既不重复也不遗漏。next_token（新锚点）不在其中——它的隐状态还不存在，要等下一轮验证前向。

#### 4.3.2 核心流程

一个样本的完整状态生命周期：

```
prefill(目标模型, output_hidden_states=True)
  └─ _init_context:
       past_key_values_draft = DynamicCache()          # 空
       target_hidden_states = extract(prefill.hidden_states)  # 覆盖位置 0..N-1, 即整个 prompt

循环每一轮:
  _propose:  位置切片 [c : start+B] ← c = 草稿缓存长
             forward 时把 target_hidden_states (长 start-c) 的 K/V 追加进草稿缓存
             crop(start) → 草稿缓存覆盖位置 0..start-1
  verify:    目标前向(锚点+草稿), 输出 hidden_states 覆盖 start..start+k
             主循环 crop(start+a+1) → 目标缓存覆盖位置 0..start+a
  _update:   target_hidden_states ← extract(verify输出)[:, :a+1]
             # 覆盖位置 start..start+a = 恰好补上「草稿缓存的缺口」
  start ← start+a+1; 下一轮的 c 正好等于本轮 start → 缺口闭合
```

闭环自检：第 \(r+1\) 轮 `_propose` 时 `get_seq_length()` = 第 \(r\) 轮的 `start`，而 `target_hidden_states` 覆盖「第 \(r\) 轮 start 到第 \(r+1\) 轮 start − 1」——缓存已有部分与滚动窗口**严丝合缝地拼接**成完整上下文，无重叠、无缝隙。首轮是特例：缓存为空，窗口覆盖整个 prompt（`N` 个），一次补齐。

#### 4.3.3 源码精读

[deepspec/eval/dspark/evaluator.py:85-97](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L85-L97)：`_init_context`——prefill 之后被调用一次（调用点在 [deepspec/eval/base_evaluator.py:378-383](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L378-L383)），把 prefill 的 `output_hidden_states` 抽成 \(K \times H\) 特征。钩子签名上主循环还会传 `output_ids`、`position_ids`、`num_input_tokens`，这里用 `**kwargs` 吞掉不需要的部分：

```python
return SimpleNamespace(
    past_key_values_draft=DynamicCache(),
    target_hidden_states=extract_context_feature(
        initial_output.hidden_states,
        self.draft_model.target_layer_ids,
    ),
)
```

抽取函数 [deepspec/modeling/dspark/common.py:52-56](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L52-L56)：`-1` 层（embedding 输出）映射到元组索引 0，第 \(i\) 个 decoder 层映射到 \(i+1\)，选中的层沿最后一维拼接——与 u2-l5 生成 target cache 时的取法一字不差，训练与评估吃的是同一种特征：

```python
return torch.cat(
    [hidden_states[0 if layer_id == -1 else layer_id + 1] for layer_id in layer_ids],
    dim=-1,
)
```

[deepspec/eval/dspark/evaluator.py:134-147](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L134-L147)：`_update`——对同一份验证输出做同样的抽取，然后**只切前 \(a+1\) 个位置**并整体替换滚动窗口。注意它只改 `context.target_hidden_states`，完全不碰草稿缓存（缓存的拼接发生在下一轮 `_propose` 的 forward 里）：

```python
verified_target_hidden = extract_context_feature(
    verification.target_output.hidden_states,
    self.draft_model.target_layer_ids,
)
context.target_hidden_states = verified_target_hidden[
    :,
    : verification.accepted_draft_tokens + 1,
    :,
]
```

为什么切片长度是 `accepted_draft_tokens + 1` 而不是 `+2`（把 next_token 也算上）？因为 `verification.target_output.hidden_states` 是**验证前向**的输出，只覆盖 `verify_input_ids` 里的 token（锚点 + 草稿），next_token 是验证后从目标分布采样的，其隐状态此刻并不存在。对照主循环的提交语句 [deepspec/eval/base_evaluator.py:411-425](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L411-L425)：`verify_input_ids[:, :a+1]` 写入 `output_ids[start : start+a+1]`，`next_token` 写在 `start+a+1`——`_update` 保留的正是前者。next_token 作为下一轮锚点，走的是「块槽 0 的输入 embedding」这条路，而不是上下文特征这条路。

两个边界情形：\(a = 0\) 时窗口长度为 1（只有锚点），下一轮只补 1 个上下文 K/V；`terminated_by_stop_token` 时主循环在 [deepspec/eval/base_evaluator.py:415-419](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L415-L419) 直接 break，不调用 `update`——状态就此作废，无需维护。

#### 4.3.4 代码实践

**实践目标**：画出「一条 token 从生成到进入草稿上下文」的完整状态旅程，检验对双缓存分工的理解。

**操作步骤**（纸笔绘图，对照源码）：

1. 画两条水平时间轴，分别标注「目标 KV cache 覆盖范围」「草稿 KV cache + 滚动窗口覆盖范围」，横轴是序列位置 0..21（沿用 4.1.4 的三轮例子：\(N=10\)，三轮的 \(a\) 分别为 3、0、任意）。
2. 在每个关键节点（prefill 后、每轮 `_propose` 的 forward 后与 crop 后、每轮主循环 `crop(start)` 后、每轮 `_update` 后）更新两条轴的覆盖范围。
3. 给第 2 轮验证时接受的第 1 个草稿 token（位置 11）做全程标注：它先作为 sampled token 被 `_propose` 产出 → 在验证前向中获得目标隐状态 → 被接受后由主循环写入 `output_ids` 并保留在目标缓存 → 被 `_update` 切进滚动窗口 → 在第 2 轮 `_propose` 的 forward 中由 `fc/hidden_norm/k_proj` 变成草稿缓存的第 11 号 K/V。

**需要观察的现象 / 预期结果**：任一时刻「草稿缓存 ∪ 滚动窗口」恰好等于「目标缓存」，且都等于位置 0..start-1（锚点除外）；同一条 token 在两条缓存中的 K/V 数值不同（4.1.5 练习 3），但位置编号严格一致。**待本地验证**：若想运行验证而非纸笔，可写一个只依赖 `torch` + `transformers` 的小脚本，用 `DynamicCache` 配假 K/V 模拟三轮 update/crop 并打印 `get_seq_length()`（参考 5. 综合实践第 4 步）。

#### 4.3.5 小练习与答案

**练习 1**：`_init_context` 为什么不把首 token（prefill 后直采的那个）的隐状态也放进窗口？

**答案**：首 token 的位置是 \(N\)，而窗口初始覆盖 0..\(N-1\)（prompt）。首 token 没有经过任何目标前向（它是从 prefill logits 采样的，prefill 的 `hidden_states` 只覆盖 prompt 本身），所以拿不到它的隐状态。它正是第 1 轮的锚点，走「`draft_input_ids[:, 0]` 输入 embedding」路径进入草稿；它的隐状态在第 1 轮验证前向之后才由 `_update` 进入窗口——与后续每轮的 next_token 完全对称。

**练习 2**：`_update` 里如果误写成 `[:, :accepted_draft_tokens + 2]`，会在哪里、以什么形式出错？

**答案**：不会立刻越界报错（验证输出的隐状态长度是 \(k+1\)，只要 \(a+2 \le k+1\) 切片就合法），错误会潜伏到下一轮 `_propose`：窗口长度变成 \(a+2\)，而位置切片长度 \(= (start_{new} - c) + B = (a+1) + B\)，于是 cos/sin 比新增 key 短 1 个位置，RoPE 相乘形状失配报错；即便绕过（比如恰好 \(a = k\)），窗口里也会混入一个**不存在**的 token 的隐状态（把草稿 token \(d_a\) 的隐状态错当 next_token 的），注意力内容被污染。这体现了该协议「长度必须精确闭合」的脆弱性与 `a+1` 这个数字的必然性。

**练习 3**：为什么 `_update` 不顺带把窗口里的隐状态直接写入草稿缓存（比如调用一次 `update`），而要留到下一轮 forward 里做？

**答案**：草稿缓存是**逐层**的 K/V，由每层注意力的 `k_proj/v_proj` 在 forward 内部写入（[deepspec/modeling/dspark/qwen3/modeling.py:117-119](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L117-L119)），评估器拿不到也不应操作层内细节；且写入时必须与「位置切片 + RoPE + 与块内 K/V 拼接」同步完成。`_update` 只搬运原料（目标隐状态特征），加工统一发生在下一轮 `forward_dspark_draft_block`——职责分离让 `_update` 保持三行核心逻辑。

## 5. 综合实践

**实践目标**：完成规格任务——对照 `_propose` 默写一次块提议的完整伪代码（含 `draft_input_ids` 构造、位置切片、cache crop），并回答核心思考题：`past_key_values_draft.crop(start)` 为什么必须发生在取回 `block_hidden` 之后。

### 步骤 1：默写块提议伪代码

对照 [deepspec/eval/dspark/evaluator.py:99-132](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L99-L132) 与 [deepspec/eval/dspark/draft_ops.py:22-153](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L22-L153)，先自己写，再对照下面的参考（伪代码，非项目原码）：

```
def propose(context, output_ids, position_ids, start):
    # 1. 构造噪声块: B 个 mask token, 槽 0 换成锚点 output_ids[:, start]
    draft_input_ids = full((1, B), mask_token_id)
    draft_input_ids[:, 0] = output_ids[:, start]

    # 2. 位置切片: 从草稿缓存当前长度到 start+B
    c = context.past_key_values_draft.get_seq_length()
    draft_position_ids = position_ids[:, c : start + B]     # 长 (start-c)+B

    # 3. 一次草稿前向
    #    query = 块的 B 个 embedding; 上下文键 = fc+hidden_norm(target_hidden_states) 再 k/v_proj
    #    全长 K/V = 缓存(0..c-1) + 新上下文(c..start-1) + 块(start..start+B-1)
    #    q 的 RoPE 取 cos/sin 的后 B 个位置 → 块内位置 start..start+B-1
    block_hidden = draft._forward_backbone(
        noise_embedding=embed(draft_input_ids),
        target_hidden_states=context.target_hidden_states,   # 长 start-c
        position_ids=draft_position_ids, past_key_values=cache, use_cache=True)

    # 4. 取回输出后立刻裁剪: 丢弃块内 B 个一次性 K/V, 保留新上下文 K/V
    context.past_key_values_draft.crop(start)                # 缓存长度 → start

    # 5. 采样与置信度早停
    base_logits = lm_head(block_hidden)
    sampled, corrected = sample_block_tokens(base_logits, first_prev=anchor)
    k = B 或 置信度首个低于阈值的下标 (可能为 0)
    return DSparkDraftProposal(
        draft_token_count=k,
        verify_input_ids=[anchor, sampled[:k]],
        draft_probs=softmax(corrected[:, :k] / T),
        confidence_logits=conf[:, :k])
```

### 步骤 2：手推双缓存对位表

完成 4.1.4 的三轮表格，并额外补一列「目标 cache 长度（轮末）」。预期：轮 1 末两条缓存都是 14，轮 2 末都是 15——长度永远同步，内容各司其职。

### 步骤 3：回答 crop 顺序思考题

参考答案要点（四层递进）：

1. **计算依赖**：块内 \(B\) 个位置的 K/V 是在这次 forward 里才追加进缓存的，注意力计算必须读到它们才能产出 `block_hidden`；裁剪只能发生在注意力完成之后。
2. **数据独立性**：`block_hidden` 是独立张量，不引用缓存内部存储，所以「先取回、后裁剪」不会破坏已得结果——这使该顺序安全。
3. **裁剪的动机**：块内 K/V 是基于 mask token 噪声残差流算出的一次性产物。下一轮这些位置的真实 token 已知（接受前缀 + 新锚点），其上下文 K/V 必须由 `_update` 存下的真实目标隐状态重新计算；废弃 K/V 若不裁掉会永久污染缓存。
4. **裁到 `start` 的必然性**：只有裁到 `start`，缓存长度才与「已提交前缀（锚点除外）」对齐。若不裁（缓存长度停在 `start+B`），下一轮位置切片下界 `get_seq_length()` 会从 `start+B` 起跳——RoPE 位置凭空多出 \(B\)，且新上下文 K/V 会接在废弃块 K/V 之后，位置与内容双重错位，与目标 cache 的同步也被打破。序列化地看：`crop(start)` 与主循环的 `past_key_values_target.crop(start)`（[deepspec/eval/base_evaluator.py:425](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L425)）是同一条不变式在两条缓存上的两次执行。

### 步骤 4（可选，待本地验证）：用 DynamicCache 复现裁剪语义

以下为**示例代码**（非项目代码），只验证 `update/crop/get_seq_length` 的行为与「不裁剪会导致下一轮切片起点错位」：

```python
import torch
from transformers import DynamicCache

B, start = 7, 14
cache = DynamicCache()

def kv(n):  # 形状 [1, num_kv_heads, n, head_dim] 的假 K/V
    return torch.randn(1, 8, n, 64), torch.randn(1, 8, n, 64)

# 第 1 轮: 缓存已有 10 个(模拟), 本轮追加 新上下文4 + 块7
k, v = kv(4 + B); cache.update(k, v, layer_idx=0)
print(cache.get_seq_length())        # 预期 21 = start + B
cache.crop(start)
print(cache.get_seq_length())        # 预期 14 = start
# 若注释掉上一行 crop, 下一轮切片下界会变成 21 而不是 14:
print("next slice would start at", cache.get_seq_length())
```

**需要观察的现象 / 预期结果**：打印依次为 `21`、`14`、`next slice would start at 14`；注释掉 `crop` 后最后一行变为 21，直接演示错位。本脚本依赖本地安装的 `torch` 与 `transformers`，行为**待本地验证**。

## 6. 本讲小结

- DSpark 的 `_propose` 用「锚点 + mask token 块」做**一次**长度 \(B\) 的草稿前向（`forward_dspark_draft_block`），同时产出整块隐藏状态——这是它相对链式逐 token 提议（Eagle3）的速度来源。
- 位置切片 `[:, get_seq_length() : start+block_size]` 一箭双雕：既给待补的新上下文键赋 RoPE 位置，又给块内 query/键赋位置；切片长度减 \(B\) 恒等于 `target_hidden_states` 长度。
- 草稿缓存与目标缓存**对偶维护**：每轮各自 `crop(start)`，长度同步等于 `start`，内容分别是草稿侧投影的上下文 K/V 与目标模型自身的 K/V；训练时的块稀疏掩码在评估时退化为「缓存里只有一个块」的全注意力，训练构图与推理构图严格一致。
- `build_dspark_proposal` 从修正后的 logits 采样并以其构造 `draft_probs`（拒绝采样无损性的前提），置信度头按阈值截断低置信前缀，截到 0 则退化为「只验证锚点 + 目标直采」的空转轮。
- `_update` 把验证输出的隐状态切成「锚点 + 被接受前缀」共 \(a+1\) 个，作为滚动窗口整体替换，恰好补上草稿缓存的缺口；next_token 的隐状态此刻尚不存在，它以下一轮锚点的身份走输入 embedding 路径。

## 7. 下一步学习建议

下一讲 **u6-l5 置信度头评估**正好接住本讲留下的线头：`_post_verify` 交给 `ConfidenceHeadRecorder.observe` 的那对 `(proposal, verification)` 如何变成 ECE / AUROC / Brier 与可靠性图，以及为什么 `confidence_threshold` 非 0 时要关闭校准记录（本讲 [evaluator.py:44-48](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L44-L48) 已埋下这个条件）。之后 **u6-l6 Eagle3 评估器**值得带着对比清单去读：它的 `extend_draft_cache` 预填充与链式提议如何解决本讲的同一组问题（上下文特征、缓存对齐、锚点交接），两套实现的差异能反向加深对 DSpark 设计的理解。若想动手，可在小 checkpoint 上跑 `scripts/eval/eval.sh` 时调整 `--confidence-threshold`，观察本讲的早停逻辑对 `accept_len` 与 `verify_rate` 的实际影响（可结合 u6-l1 的指标恒等式预判方向，结果待本地验证）。
