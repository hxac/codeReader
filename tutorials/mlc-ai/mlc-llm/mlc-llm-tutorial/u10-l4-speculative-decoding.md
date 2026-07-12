# 推测解码动作链

## 1. 本讲目标

本讲是 U10「KV 缓存、前缀缓存、采样与推测解码」单元的收官篇，专讲**推测解码（speculative decoding）**这条加速链在引擎里如何落地为一组可插拔的 Action。

读完本讲，你应当能够：

1. 说清推测解码「起草（draft）→ 校验（verify）→ 接受/拒绝」三段式的直觉与收益来源。
2. 区分 MLC LLM 的三种推测模式——`small_draft`（小模型链式起草）、`eagle`（基于隐藏状态的 EAGLE 起草）、`medusa`——以及它们各自装配出怎样的 Action 链。
3. 精读 `batch_draft` / `eagle_batch_draft` 起草动作与 `batch_verify` / `eagle_batch_verify` 校验动作，理解 `draft_output_tokens` 如何在动作之间流转，以及接受的 token 如何经 `CommitAcceptedTokenTreeNodesToKVCache` 落进分页 KV cache。
4. 理解 `AutoSpecDecode` 如何根据并发批量自动决定「这一步要不要推测」。
5. 认识 `DraftTokenWorkspaceManager` 这个跨动作的 GPU 槽位池为什么是推测解码的物理基础。

## 2. 前置知识

### 2.1 为什么需要推测解码：访存受限下的加速

大模型 decode 阶段每生成一个 token，都要把全部权重从显存读到算核（**memory-bound**）。此时算核的计算单元大量空闲——一次访存只换一个 token，太浪费。

推测解码的直觉是：**用一个很小的「草稿模型」先廉价地猜出接下来 K 个 token，再用大模型一次性把这 K+1 个 token 的前馈打包算完（一个 batch）**。大模型这一次前馈的开销和只算 1 个 token 几乎一样（访存大头是权重，不是激活），却能同时校验 K 个猜测。猜对的直接收下，猜错的从出错处重新采样。于是「一次大模型前馈」从产出 1 个 token 变成产出若干个 token，吞吐显著提升。

代价是：草稿猜得越准收益越大，猜得差则白算；所以草稿模型的质量与「接受率（acceptance rate）」决定一切。

### 2.2 校验为什么是「拒绝采样」

校验不只是「逐字比对」，而是要保证最终分布与单用大模型完全一致。标准做法是**拒绝采样（rejection sampling）**：设草稿模型给出 token \(x\) 的概率为 \(q(x)\)、大模型为 \(p(x)\)，则

\[
\text{接受概率} = \min\!\left(1,\; \frac{p(x)}{q(x)}\right)
\]

若接受，token 入列；若拒绝，则从规整化的「残差分布」重采样：

\[
p_{\text{res}}(x) = \frac{\max(0,\; p(x)-q(x))}{\displaystyle\sum_{x'}\max(0,\; p(x')-q(x'))}
\]

这套数学在引擎里由采样器的 `BatchVerifyDraftTokensWithProbAfterTopP`（见 u10-l3）实现。关键前提是：**校验时必须同时握有草稿模型的分布 \(q\) 和大模型的分布 \(p\)**——这正是本讲反复出现的 `draft_probs` 与 `DraftTokenWorkspaceManager` 存在的根本原因。

### 2.3 承接前面几讲的术语

- **Action 与 Step 循环**（u9-l2）：每个动作只暴露 `Step(estate)`；`ThreadedEngine` 每步按优先级遍历有序动作表 `actions_`，第一个有活干的动作获胜并执行。本讲的 `AutoSpecDecode` 正是 u9-l2 提到的「动作套动作」的实例。
- **committed_tokens vs draft_output_tokens**（u9-l3）：`committed_tokens` 是已敲定、不可撤销的产出；`draft_output_tokens` 是草稿、待大模型 verify 的临时 token。本讲的核心就是把 draft token「转正」为 committed token。
- **分页 KV cache 的提交接口**（u10-l1）：`CommitAcceptedTokenTreeNodesToKVCache(seq_ids, accepted_leaf_indices)` 把接受的 token 树节点真正写进 KV cache，`PopNFromKVCache` 回退末尾若干页，`ForkSequence` 写时复制共享前缀。
- **编译期附加的辅助函数**（u8-l3）：`AttachSpecDecodeAuxFuncs` 在编译期生成了 `scatter`/`gather` 概率与隐藏状态的 kernel，本讲的 `ScatterDraftProbs`/`GatherDraftProbs`/`GatherHiddenStates` 正是经 FunctionTable 按名字符串调用这些 kernel。
- **FunctionTable 名字符串契约**（u9-l4）：C++ 方法 → FunctionTable 字段 → model lib 函数名，三层同名。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [cpp/serve/engine_actions/batch_draft.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_draft.cc) | `small_draft` 模式的链式起草动作，用独立小模型自回归地猜 K 个 token |
| [cpp/serve/engine_actions/eagle_batch_draft.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/eagle_batch_draft.cc) | `eagle` 模式的起草动作，吃大模型隐藏状态 + token 嵌入，在特征空间续写 |
| [cpp/serve/engine_actions/batch_verify.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc) | `small_draft` 模式的校验动作，拒绝采样裁决、提交接受 token、回退 KV cache |
| [cpp/serve/engine_actions/eagle_batch_verify.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/eagle_batch_verify.cc) | `eagle` 模式的校验动作，额外在尾部产出一个「前瞻」草稿 token |
| [cpp/serve/engine_actions/auto_spec_decode.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/auto_spec_decode.cc) | 自动决策动作：按并发批量查表选 draft 长度，决定走推测还是普通解码 |
| [cpp/serve/engine_actions/action_commons.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc) | `CreateEngineActions`：按 `speculative_mode` 装配不同 Action 链 |
| [cpp/serve/draft_token_workspace_manager.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/draft_token_workspace_manager.h) | 草稿 token 的 GPU 槽位池：`AllocSlots`/`FreeSlots` 带引用计数 |
| [cpp/serve/config.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.h) | `SpeculativeMode` 枚举与 `spec_draft_length`/`spec_tree_width` 字段 |
| [cpp/serve/request_state.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.h) | `draft_output_tokens` 等草稿 token 状态字段与 `AddDraftToken`/`CommitToken` |
| [cpp/serve/model.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h) | `BatchVerify`/`BatchVerifyToLastHidden`/`FuseEmbedHidden`/`ScatterDraftProbs` 等接口 |

## 4. 核心概念与源码讲解

### 4.1 推测解码总览：三种模式与动作链装配

#### 4.1.1 概念说明

MLC LLM 支持四种推测模式，由 [cpp/serve/config.h:211-220](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.h#L211-L220) 的 `SpeculativeMode` 枚举定义：

- `kDisable`：关闭推测解码，走普通 `BatchDecode`。
- `kSmallDraft`：经典推测解码。**两个独立模型**——大模型（`verify_model_id_=0`，亦称 LLM/target）+ 小草稿模型（`draft_model_id_=1`，亦称 SSM）。小模型用与原模型同样的 token 自回归地续写 K 步。
- `kEagle`：EAGLE 风格。草稿模型不是「从头读 token」，而是读**大模型最后一层的隐藏状态（hidden states）**并把它与 token 嵌入融合后续写。因为草稿是在「特征空间」里、且与大模型共享 lm_head，接受率显著高于 small_draft。
- `kMedusa`：Medusa 风格。大模型一次前馈直接吐出多个后续位置的 logits（`GetMultiStepLogits`），无需独立小模型的前向，但需要专门训练的多头。

无论哪种模式，引擎里**始终要求两个 Model 对象**（`models_.size() == 2`，见各动作开头的守卫），其中 `models_[0]` 是校验大模型、`models_[1]` 是草稿模型。与之配套的配置字段见 [cpp/serve/config.h:295-303](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.h#L295-L303)：

- `speculative_mode`：选哪种模式。
- `spec_draft_length`：每轮起草几个 token；`0` 表示交给 `AutoSpecDecode` 自动决定。
- `spec_tree_width`：树形起草的宽度；`1` 是线性链，`>1` 是树（需 `BatchTreeDecode`）。

#### 4.1.2 核心流程：动作链装配

动作表不是手写的，而是 `CreateEngineActions` 按 `speculative_mode` 与 `spec_draft_length` 一次性装配出来的。装配结果就是一个有序 `Array<EngineAction>`，顺序即优先级（见 u9-l2）。

| 模式 / 设置 | 装配出的动作链 |
|-------------|----------------|
| `kDisable` | `NewRequestPrefill` → `BatchJumpForward` → `BatchDecode` |
| `kSmallDraft` + `spec_draft_length>0` | `NewRequestPrefill` → `BatchDraft` → `BatchVerify` |
| `kSmallDraft` + `spec_draft_length==0` | `NewRequestPrefill` → `AutoSpecDecode(BatchDraft+BatchVerify, BatchDecode)` |
| `kEagle` | `EagleNewRequestPrefill` → `EagleBatchDraft` → `EagleBatchVerify` |
| `kMedusa` | `EagleNewRequestPrefill` → `EagleBatchVerify` |

#### 4.1.3 源码精读

`CreateEngineActions` 的分支结构是理解整条链的钥匙，见 [cpp/serve/engine_actions/action_commons.cc:16-137](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L16-L137)。关键片段：

[cpp/serve/engine_actions/action_commons.cc:30-46](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L30-L46)：EAGLE 模式装配三件套——EAGLE 专用 prefill、`EagleBatchDraft`、`EagleBatchVerify`。注意它强校验 `spec_draft_length > 0`，注释明说「自动模式暂不支持 Eagle」。

[cpp/serve/engine_actions/action_commons.cc:61-76](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L61-L76)：固定 draft 长度的 small_draft——`NewRequestPrefill` → `BatchDraft` → `BatchVerify`。

[cpp/serve/engine_actions/action_commons.cc:77-102](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L77-L102)：`spec_draft_length==0` 时走 `AutoSpecDecode`，把 `{BatchDraft, BatchVerify}` 当推测动作集、`{BatchDecode}` 当普通动作集塞进去，由它每步自己挑。

#### 4.1.4 代码实践

**实践目标**：在源码层确认「给定一组配置，引擎会装配出哪条动作链」。

**操作步骤**：

1. 打开 [action_commons.cc:27-103](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L27-L103)。
2. 假设用户配置 `speculative_mode=small_draft`、`spec_draft_length=0`、关闭分离式（`disaggregation=false`），追踪 `CreateEngineActions` 走哪条分支。
3. 写出最终返回的 `actions` 数组里每个元素的名字与顺序。

**预期结果**（待本地验证）：`{ NewRequestPrefill, AutoSpecDecode }`，其中 `AutoSpecDecode` 内部又封装了 `{BatchDraft, BatchVerify}` 与 `{BatchDecode}` 两组。这正是 u9-l2 说的「动作套动作」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 EAGLE 模式要求 `spec_draft_length > 0`，而 small_draft 允许为 `0`？

**参考答案**：small_draft 为 `0` 时可挂 `AutoSpecDecode` 每步动态查表选 draft 长度；而 `AutoSpecDecode` 目前只装配 `BatchDraft`/`BatchVerify`，没有 EAGLE 版本（见 [action_commons.cc:31-32](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L31-L32) 的 ICHECK 与注释），故 EAGLE 必须显式给一个正的固定 draft 长度。

**练习 2**：`kMedusa` 模式装配的链里**没有** `EagleBatchDraft`，草稿从哪来？

**参考答案**：Medusa 不用独立小模型做前向，而是大模型一次前馈经 `GetMultiStepLogits` 直接吐出后续多步的 logits；草稿在 `EagleBatchVerify` 尾部的 medusa 分支（见 4.3.5）里产生。

---

### 4.2 起草动作：从链式 token 起草到隐藏状态起草

#### 4.2.1 概念说明

起草动作（draft action）的职责是：**用廉价的方式为每个 running 请求猜出若干「候选下一步 token」及其概率分布**，写入 `RequestModelState::draft_output_tokens`，留给校验动作裁决。

两种起草路线的差别在于「草稿模型怎么读输入」：

- **`BatchDraft`（small_draft）**：草稿模型是一个**独立的自回归小 LM**。它吃「上一步采样出的 token id」→ 嵌入 → `BatchDecode` → logits → 采样，循环 K 轮。和大模型唯一的耦合是「共享 token 词表与已生成 committed token」。
- **`EagleBatchDraft`（eagle）**：草稿模型吃的是**大模型最后一层的 hidden states**（在 verify 阶段顺手保存下来），把它与「上一步 token 嵌入」用 `FuseEmbedHidden` 融合，再走 `BatchDecodeToLastHidden` 得到新的 hidden，最后经 lm_head 得 logits。因为草稿站在大模型的「肩膀（特征）」上续写，接受率更高。

两者都把每个草稿 token 的概率分布 `ScatterDraftProbs` 进 GPU 槽位池——因为校验阶段做拒绝采样还需要这份分布 \(q(x)\)。

#### 4.2.2 核心流程

**`BatchDraft::Step`（small_draft 链式起草）**，参见 [batch_draft.cc:38-300](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_draft.cc#L38-L300)：

```
Step(estate):
  守卫: models_.size()==2 且 running_queue 非空, 否则返回 {}
  抢占循环: 显存不够时 PreemptLastRunningRequestStateEntry / prefix_cache->TryFreeMemory
  对 model_id in [1, models_.size()):           # 第一个模型(大模型)不参与起草
    for draft_id in [0, spec_draft_length):     # 起草 K 轮
      准备本轮输入 token (draft_id==0 含对齐/补滞后的 prefill)
      TokenEmbed -> BatchDecode (或 BatchTreeDecode 当 spec_tree_width>1)
      logit_processor 更新 logits, ComputeProbsFromLogit
      CommitSequenceExtention (把上一轮 prefill 改动提交, 与 GPU 执行重叠)
      sampler 重归一化 + 采样
      AllocSlots; ScatterDraftProbs (把草稿概率存进槽位池)
      mstates[i]->AddDraftToken(sample, slot, parent_idx)   # 写入 draft_output_tokens
```

**`EagleBatchDraft::Step`（eagle 起草）**，参见 [eagle_batch_draft.cc:38-186](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/eagle_batch_draft.cc#L38-L186)。它与 `BatchDraft` 的关键差异有三：

1. **首个草稿 token 不在本动作产生**——它在上一轮 verify（或 prefill）的尾部「前瞻」步骤里已经写进 `draft_output_tokens`，故循环从 `draft_id = 1` 开始（见 [eagle_batch_draft.cc:109](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/eagle_batch_draft.cc#L109)）。
2. **吃 hidden states**：若 `spec_draft_length > 1`，先用 `GatherHiddenStates` 从槽位池取回上一步的隐藏状态（[eagle_batch_draft.cc:101-107](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/eagle_batch_draft.cc#L101-L107)）。
3. **特征空间续写**：`FuseEmbedHidden(embedding, hidden)` → `BatchDecodeToLastHidden` → `GetLogits`（[eagle_batch_draft.cc:129-139](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/eagle_batch_draft.cc#L129-L139)）。注意 logits 来自「草稿模型自己的头」或「借大模型的头」，由 `CanGetLogits()` 决定。

#### 4.2.3 源码精读

**起草守卫与抢占**——两个动作都一样，体现「显存不够先抢」的通用前奏。见 [batch_draft.cc:38-53](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_draft.cc#L38-L53)：第 40 行 `models_.size() != 2` 直接返回空，说明 BatchDraft 只在双模型时生效；第 46-53 行先尝试释放前缀缓存、再抢占最低优先级 running 请求，直到 `CanDecode` 为真。

**链式起草的核心循环**——[batch_draft.cc:112-293](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_draft.cc#L112-L293)。注意第 127-143 行处理「草稿模型落后于大模型」的边界：刚从普通 decode 切到推测时，草稿模型的 `committed_tokens` 比大模型少，需要先把这些「滞后 token」补 prefill 进草稿模型（必要时分块，见 `PrefillLaggedTokensByChunk`）。

**采样 + 写入草稿**——[batch_draft.cc:266-288](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_draft.cc#L266-L288)，这段是 draft action 的「产出」：

```cpp
draft_token_workspace_manager_->AllocSlots(cum_num_tokens.back(), &draft_token_slots_);
models_[model_id]->ScatterDraftProbs(probs_on_device, draft_token_slots_,
                                     &model_workspaces_[0].draft_probs_storage);
for (...) {
  mstates[i]->AddDraftToken(sample_results[j], draft_token_slots_[j], parent_idx);
}
```

`AddDraftToken` 把采样结果连同「槽位号 `draft_token_slot`」与「父节点 `parent_idx`」一起写进 `draft_output_tokens`（字段定义见 [request_state.h:78-84](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.h#L78-L84)）。`parent_idx` 编码了 token 树结构（树形起草时一个父能挂多个子），校验时据此重建树注意力。

**EAGLE 特征融合**——[eagle_batch_draft.cc:129-139](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/eagle_batch_draft.cc#L129-L139)：

```cpp
ObjectRef fused = models_[model_id]->FuseEmbedHidden(
    embeddings, hidden_states, /*batch_size*/ num_rsentries, /*seq_len*/ 1);
hidden_states = models_[model_id]->BatchDecodeToLastHidden(fused, request_internal_ids);
Tensor logits = models_[model_id]->CanGetLogits()
                    ? models_[model_id]->GetLogits(hidden_states)
                    : models_[0]->GetLogits(hidden_states);   // 借大模型的 lm_head
```

`FuseEmbedHidden`/`BatchDecodeToLastHidden` 是 EAGLE 专用的模型接口（声明见 [model.h:128-130](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h#L128-L130) 与 [model.h:191-192](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h#L191-L192)）。`CanGetLogits()`（[model.h:135](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h#L135)）回答「草稿模型有没有自己的 lm_head」，没有就借大模型的——这就是 EAGLE 与大模型「共享预测头」的实现。

#### 4.2.4 代码实践

**实践目标**：对照两种起草动作，确认 EAGLE 「首个草稿 token 来自 verify 尾部」这一设计。

**操作步骤**：

1. 在 [eagle_batch_draft.cc:109](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/eagle_batch_draft.cc#L109) 确认循环起点是 `draft_id = 1`。
2. 阅读其上方注释「The first draft token has been generated in prefill/verify stage」（[eagle_batch_draft.cc:108](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/eagle_batch_draft.cc#L108)）。
3. 跳到 [eagle_batch_verify.cc:267-332](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/eagle_batch_verify.cc#L267-L332)，找到 `// One step draft for the following steps` 块，确认它在校验完之后又调了一次 `BatchDecodeToLastHidden + GetLogits + 采样`，并把结果 `UpdateRequestStatesWithDraftProposals` 写回。

**需要观察的现象**：EAGLE 的「校验」动作比 small_draft 多做了一步「前瞻起草」，因此下一个 `EagleBatchDraft` 只需再补 `spec_draft_length - 1` 步，省掉了一步草稿前向。

**预期结果**：能用一句话说明「EAGLE 把第 0 个草稿 token 的成本摊进了 verify 步，所以 draft 动作循环从 1 开始」。

#### 4.2.5 小练习与答案

**练习 1**：`BatchDraft` 在 `draft_id==0` 时为什么要做 `PrefillLaggedTokensByChunk`？

**参考答案**：引擎可能刚从普通 `BatchDecode` 模式切到推测模式，此时草稿模型的 `committed_tokens` 落后于大模型。第 0 轮必须先把「滞后」的 committed token 补 prefill 进草稿模型，否则后续草稿与历史脱节。见 [batch_draft.cc:127-143](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_draft.cc#L127-L143)。

**练习 2**：起草时为什么必须 `ScatterDraftProbs`，而不是只存采样出的 token id？

**参考答案**：校验阶段做拒绝采样需要草稿模型的分布 \(q(x)\) 与大模型分布 \(p(x)\) 比对（见 2.2）。光存 token id 丢了概率信息，无法做无偏校验。`ScatterDraftProbs` 把整条概率分布写进跨动作的 GPU 槽位池，供 `BatchVerify` 里 `GatherDraftProbs` 取回。

---

### 4.3 校验动作：拒绝采样与 KV cache 提交

#### 4.3.1 概念说明

校验动作（verify action）是推测解码的「裁判」与「记账员」。它做四件事：

1. **打包校验**：把「最后 1 个 committed token + 全部 draft token」拼成一段序列，送大模型一次性前馈（`BatchVerify`/`BatchVerifyToLastHidden`），并行得到每个位置的 logits，即大模型分布 \(p\)。
2. **拒绝采样裁决**：取出起草时存的草稿分布 \(q\)（`GatherDraftProbs`），与 \(p\) 比对，逐 token 决定接受/拒绝，必要时从残差分布补采一个 token。
3. **接受 token 入账**：把接受的 token `CommitToken` 进 verify 与 draft 两个模型的 `committed_tokens`；把拒绝的从 draft 模型 KV cache 回退（`PopNFromKVCache`）。
4. **落进物理 KV cache**：`CommitAcceptedTokenTreeNodesToKVCache` 把接受的 token 树节点真正写进大模型的分页 KV cache（这是 2.3 提到的 u10-l1 接口）。

#### 4.3.2 核心流程（small_draft 的 BatchVerify）

```
Step(estate):
  rsentries, verify_lengths, total = GetDraftsToVerify(estate)   # 跳过 draft 为空的请求; 抢占到能放下
  构造 all_tokens_to_verify = [最后 committed token] + [全部 draft tokens]
  构造 token_tree_parent_ptr (编码 token 树父子关系)
  draft_probs = GatherDraftProbs(...)        # 取回草稿概率 q
  embeddings = TokenEmbed(all_tokens_to_verify)
  logits = BatchVerify(embeddings, seq_ids, lengths, token_tree_parent_ptr)   # 大模型一次前馈
  probs = ComputeProbsFromLogit(logits)      # 大模型分布 p
  sample_results, last_accepted_node =
      BatchVerifyDraftTokensWithProbAfterTopP(probs, ..., draft_probs)        # 拒绝采样
  for 每个请求:
      for 每个接受 token: CommitToken 到 verify_mstate 与 draft_mstate
      spec_tree_width==1 时: PopNFromKVCache(draft 模型, rollback-1)   # 链式回退
  CommitAcceptedTokenTreeNodesToKVCache(verify 模型, 接受的叶子节点)   # 落物理 KV
  若有「全部接受」的请求: 额外 BatchDecode 一步补 draft 模型 KV
  RemoveAllDraftTokens + FreeSlots; 重置 num_tokens_for_next_decode
```

#### 4.3.3 源码精读

**筛选可校验请求**——[batch_verify.cc:290-333](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc#L290-L333) 的 `GetDraftsToVerify`：跳过 `draft_output_tokens` 为空的请求（第 304 行），按 draft 长度估算所需页数并据此抢占，最后断言 `total_verify_length` 不超过 `max_num_sequence` 与 `prefill_chunk_size` 的较小者。

**拼接待校验序列**——[batch_verify.cc:81-108](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc#L81-L108)。关键三行：

```cpp
// the last committed token + all the draft tokens.
draft_token_slots_.push_back(0);  // 占位: 最后 committed token
all_tokens_to_verify.push_back(draft_mstate->committed_tokens.back().GetTokenId());
token_tree_parent_ptr.push_back(-1);                       // 根, 无父
...
for (j in draft_output_tokens):
    all_tokens_to_verify.push_back(draft_output_tokens[j].GetTokenId());
    draft_token_slots_.push_back(draft_mstate->draft_token_slots[j]);
    token_tree_parent_ptr.push_back(draft_mstate->draft_token_parent_idx[j] + 1);  // +1 给根让位
```

`token_tree_parent_ptr` 与起草时写下的 `draft_token_parent_idx` 一一对应，把整棵 token 树喂给大模型的树注意力（`BatchVerify` 声明见 [model.h:207-208](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h#L207-L208)）。

**大模型前馈 + 拒绝采样**——[batch_verify.cc:119-154](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc#L119-L154)。`BatchVerify` 一次算出所有位置 logits，`BatchVerifyDraftTokensWithProbAfterTopP` 拿大模型 `renormalized_probs` 与取回的 `draft_probs_on_device` 做拒绝采样，返回每请求的接受结果与「最后接受的树节点」。

**接受 token 入账 + 链式回退**——[batch_verify.cc:172-194](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc#L172-L194)：

```cpp
for (SampleResult sample_result : sample_results) {           // 接受的 token
  rsentries[i]->mstates[verify_model_id_]->CommitToken(sample_result);
  rsentries[i]->mstates[draft_model_id_]->CommitToken(sample_result);
}
...
if (engine_config_->spec_tree_width == 1) {                   // 链式: 回退草稿模型 KV
  int rollback_length = max(verify_length - accept_length, 0);
  if (rollback_length > 0)
    models_[draft_model_id_]->PopNFromKVCache(..., rollback_length - 1);
}
```

注意 `CommitToken` 只改 `committed_tokens` 这份「逻辑账本」，**不动物理 KV cache**——物理写入由下一步显式完成。

**落进物理 KV cache**——[batch_verify.cc:214-221](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc#L214-L221)：

```cpp
models_[verify_model_id_]->CommitAcceptedTokenTreeNodesToKVCache(
    verify_model_seq_internal_ids, last_accepted_tree_node_verify_model);
if (engine_config_->spec_tree_width > 1) {
  models_[draft_model_id_]->CommitAcceptedTokenTreeNodesToKVCache(
      draft_model_seq_internal_ids, last_accepted_tree_node_draft_model);
}
```

`CommitAcceptedTokenTreeNodesToKVCache`（声明见 [model.h:269-270](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h#L269-L270)）接收「每序列最后接受的叶子节点索引」，由 KV cache 内部把接受路径上的节点保留、把拒绝的分支剪掉。这就是「接受的 token 如何提交进 KV cache」的最终落点。链式（`spec_tree_width==1`）只对大模型做；树形则两个模型都做。

**全接受补一步**——[batch_verify.cc:223-255](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc#L223-L255)：若草稿被全部接受，草稿模型还多产了 1 个 token 却没进它的 KV cache，需对草稿模型补一次 `BatchDecode` 把这个 token 的 KV 补上，保持两模型同步。

**清场**——[batch_verify.cc:257-264](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc#L257-L264)：`RemoveAllDraftTokens` + `draft_token_workspace_manager_->FreeSlots` 释放本轮草稿占的槽位，并把 `num_tokens_for_next_decode` 重置为 1，为下一轮起草/解码做准备。

#### 4.3.4 代码实践

**实践目标**：追踪「一个被接受的 draft token 是如何从 `draft_output_tokens` 变成大模型 KV cache 里的真实条目」。

**操作步骤**：

1. 从 [batch_verify.cc:96-101](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc#L96-L101) 看 draft token 如何被收进 `all_tokens_to_verify`。
2. 从 [batch_verify.cc:175-178](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc#L175-L178) 看接受后如何 `CommitToken` 进两个 mstate。
3. 从 [batch_verify.cc:214-217](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc#L214-L217) 看如何 `CommitAcceptedTokenTreeNodesToKVCache` 落物理 KV。

**需要观察的现象**：`CommitToken`（逻辑）与 `CommitAcceptedTokenTreeNodesToKVCache`（物理）是**两步分离**的——前者改 `committed_tokens` 向量，后者才真正写分页 KV cache 的页表与数据。

**预期结果**：能画出「draft_output_tokens → CommitToken → committed_tokens → CommitAcceptedTokenTreeNodesToKVCache → 物理页」这条链。

#### 4.3.5 小练习与答案

**练习 1**：链式（`spec_tree_width==1`）校验时，为什么对草稿模型调 `PopNFromKVCache`，而大模型只调 `CommitAcceptedTokenTreeNodesToKVCache`？

**参考答案**：起草时草稿模型把每个 draft token 都自回归地写进了自己的 KV cache（所以 draft 模型 KV 含「未接受」部分）；校验拒绝后要把多余的回退，故 `PopNFromKVCache`。大模型的 `BatchVerify` 用树注意力一次性算所有位置、**不**把拒绝 token 写进 KV，校验后只需 `CommitAcceptedTokenTreeNodesToKVCache` 把接受路径写入即可。见 [batch_verify.cc:184-194](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc#L184-L194) 与 [batch_verify.cc:214-217](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc#L214-L217)。

**练习 2**：`EagleBatchVerify` 的 logits 从哪来？与 `BatchVerify` 有何不同？

**参考答案**：`EagleBatchVerify` 调 `BatchVerifyToLastHidden`（[model.h:225-228](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h#L225-L228)）得到 hidden states，再 `GetLogits`（[eagle_batch_verify.cc:126-128](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/eagle_batch_verify.cc#L126-L128)）；hidden 还要在尾部喂给草稿模型做「前瞻起草」，故必须保留 hidden 而非直接返回 logits。`BatchVerify`（small_draft）则直接返回 logits Tensor（[model.h:207-208](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h#L207-L208)）。

---

### 4.4 AutoSpecDecode：自动选择推测或普通解码

#### 4.4.1 概念说明

推测解码并非「永远更优」。当并发批量很大时，草稿会撑爆 `max_num_sequence` 限制，且大 batch 下 decode 本身算力利用率已高、推测的边际收益变小。`AutoSpecDecode` 就是个「**每步现决策**」的外壳动作：根据当前 running 请求数，查一张固定表决定本轮 draft 长度，draft 长度 `>0` 就跑推测动作集（`BatchDraft`+`BatchVerify`），否则跑普通 `BatchDecode`。

这是 u9-l2 提到的「**动作套动作**」的典型——`AutoSpecDecode` 自己是 `EngineActionObj`，它的 `Step` 内部又去调子动作的 `Step`。

#### 4.4.2 核心流程

```
Step(estate):
  num_running = running 请求数
  spec_draft_length = CalculateDraftLength(num_running)   # 查表
  actions = (spec_draft_length > 0) ? spec_decode_actions_ : batch_decode_actions_
  for action in actions: processed = action->Step(estate)  # 跑子动作链
  estate->spec_draft_length = 0                            # 用完复位
  return processed

CalculateDraftLength(n):
  n<10 -> 4; n<20 -> 3; n<30 -> 2; else -> 0
  effective_batch = n * (draft_length + 1)
  return effective_batch > max_num_sequence ? 0 : draft_length   # 超限则关闭推测
```

#### 4.4.3 源码精读

**决策主流程**——[auto_spec_decode.cc:28-49](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/auto_spec_decode.cc#L28-L49)。第 35 行算 draft 长度，第 40-41 行用它选动作集，第 42-44 行 **`for` 循环把子动作链依次跑完**（推测时先 `BatchDraft` 后 `BatchVerify`），第 47 行把 `estate->spec_draft_length` 复位为 0——这一点很重要：`BatchDraft`/`BatchVerify` 读的是 `estate->spec_draft_length`，必须由 AutoSpecDecode 在调用前写入、调用后清零，避免泄漏到下一轮。

**查表 + 安全闸**——[auto_spec_decode.cc:52-68](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/auto_spec_decode.cc#L52-L68)：

```cpp
if (num_running_rsentries < 10)      draft_length = 4;
else if (num_running_rsentries < 20) draft_length = 3;
else if (num_running_rsentries < 30) draft_length = 2;
else                                  draft_length = 0;
int effective_batch_size = num_running_rsentries * (draft_length + 1);
return effective_batch_size > engine_config_->max_num_sequence ? 0 : draft_length;
```

注释（[auto_spec_decode.cc:54-55](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/auto_spec_decode.cc#L54-L55)）坦承「目前只用固定表，后续会做更强的 draft 长度选择」。最后的 `effective_batch_size > max_num_sequence` 闸门保证推测后总 token 数不超并发上限。

#### 4.4.4 代码实践

**实践目标**：手算几组批量下 `AutoSpecDecode` 会选什么。

**操作步骤**：

1. 假设 `max_num_sequence = 4`（小显存单卡常见默认）。
2. 分别对 `num_running = 1, 5, 25` 代入 [auto_spec_decode.cc:56-67](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/auto_spec_decode.cc#L56-L67) 计算 `spec_draft_length`。
3. 判断每组是走推测还是普通 decode。

**预期结果**（待本地验证，取决于 `max_num_sequence` 实际值）：

| num_running | 表内 draft_length | effective_batch (max_num_sequence=4) | 最终 |
|-------------|-------------------|--------------------------------------|------|
| 1 | 4 | 1×5=5 > 4 | 0（普通 decode） |
| 5 | 4 | 5×5=25 > 4 | 0 |
| 25 | 2 | 25×3=75 > 4 | 0 |

可见 **`max_num_sequence` 很小时，安全闸会让推测几乎总被关闭**——只有当 `max_num_sequence` 足够大（如 32+）时，小批量才真正启用推测。这正是「推测解码需要足够的并发预算」的体现。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `AutoSpecDecode::Step` 末尾要把 `estate->spec_draft_length` 复位为 0？

**参考答案**：`spec_draft_length` 是引擎级状态，`BatchDraft` 据它决定起草几轮（[batch_draft.cc:112](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_draft.cc#L112)）。若不复位，下一轮若走普通 `BatchDecode`，残留值会误导后续判断；每轮由 `AutoSpecDecode` 现算现写、用完清零，保证状态干净。

**练习 2**：`AutoSpecDecode` 与 `BatchDraft`/`BatchVerify` 的调用关系，体现了 u9-l2 的哪个概念？

**参考答案**：动作可嵌套——一个 Action 的 `Step` 内部去调其他 Action 的 `Step`。`AutoSpecDecode` 是外壳，`BatchDraft`+`BatchVerify`（或 `BatchDecode`）是被它驱动的子动作链。

---

### 4.5 DraftTokenWorkspaceManager：草稿 token 的槽位池

#### 4.5.1 概念说明

草稿 token 的「概率分布」和「隐藏状态」体积很大（概率是 vocab 维向量、隐藏是 hidden_size 维向量），且必须**跨动作存活**：起草动作 `ScatterDraftProbs` 写入、校验动作 `GatherDraftProbs` 读出。它们不能是动作栈上的局部变量。

`DraftTokenWorkspaceManager` 就是一个**固定大小的 GPU 槽位池**：预分配 `max_num_tokens` 个槽位，每个槽位能存一份概率分布（必要时还有隐藏状态）。起草时 `AllocSlots(n)` 领 n 个槽位、把概率 `Scatter` 进去；校验结束 `FreeSlots` 归还。带引用计数（`AllocSlots` 有 `initial_ref_count` 重载），支持一个草稿槽被多模型/多分支共享时延迟释放。

#### 4.5.2 核心流程

```
起草 BatchDraft/EagleBatchDraft:
  slots = draft_token_workspace_manager_->AllocSlots(n)     # 领槽
  model->ScatterDraftProbs(probs, slots, &storage)          # 写概率到槽
  mstate->AddDraftToken(sample, slot, parent_idx)           # 槽号记进 draft_token_slots

校验 BatchVerify/EagleBatchVerify:
  draft_probs = model->GatherDraftProbs(storage, slots, &dst)   # 从槽读概率
  ... 拒绝采样 ...
  mstate->RemoveAllDraftTokens(&slots)
  draft_token_workspace_manager_->FreeSlots(slots)          # 还槽
```

#### 4.5.3 源码精读

**槽位池接口**——[draft_token_workspace_manager.h:35-99](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/draft_token_workspace_manager.h#L35-L99)。`AllocSlots` 两个重载（[L62](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/draft_token_workspace_manager.h#L62) 与带 `initial_ref_count` 的 [L70](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/draft_token_workspace_manager.h#L70)），`FreeSlots` 在 [L77](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/draft_token_workspace_manager.h#L77)。私有成员 `free_slots_`（空闲槽集合）、`ref_count_`（每槽引用计数）说明它是「池 + 引用计数」的经典实现。类注释（[L28-34](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/draft_token_workspace_manager.h#L28-L34)）明确：「pool of slots for the draft tokens to store the states」。

**配套的 scatter/gather 模型接口**——声明在 [model.h:353-367](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.h#L353-L367)：`GatherHiddenStates`/`ScatterHiddenStates`（EAGLE 用）、`GatherDraftProbs`/`ScatterDraftProbs`（概率用）。它们都接受 `indices`（即槽位号）与一个 `dst` 指针，把数据在「连续大 buffer」与「按槽位散布」之间搬移。这些函数的 GPU kernel 由 u8-l3 的 `AttachSpecDecodeAuxFuncs` 在编译期生成——再次印证「编译期附加、运行期按名字符串调用」的契约。

**跨动作的存取**——起草写入见 [batch_draft.cc:273-275](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_draft.cc#L273-L275)；校验取回见 [batch_verify.cc:109-111](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc#L109-L111)；归还见 [batch_verify.cc:258-260](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc#L258-L260)。三者通过 `draft_token_slots` 字段（[request_state.h:80](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.h#L80)）把「逻辑草稿 token」与「物理槽位」一一绑定。

#### 4.5.4 代码实践

**实践目标**：理解「为何草稿状态要用槽位池而非局部张量」。

**操作步骤**：

1. 在 [batch_draft.cc:273-275](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_draft.cc#L273-L275) 确认起草把概率 `Scatter` 进 `model_workspaces_[0].draft_probs_storage`，槽号存进 `mstates`。
2. 在 [batch_verify.cc:109-111](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc#L109-L111) 确认校验用同样的槽号 `Gather` 取回。
3. 思考：起草与校验是**两个不同动作的两次 `Step`**，若用局部 `Tensor` 会怎样？

**预期结果**：能说明「局部张量随动作 `Step` 返回即销毁，无法跨动作传递；槽位池是常驻 GPU buffer，靠 `AllocSlots`/`FreeSlots` 显式管理生命周期，是跨动作传递草稿概率/隐藏状态的物理载体」。

#### 4.5.5 小练习与答案

**练习 1**：`AllocSlots` 为什么有「带 `initial_ref_count`」的重载？

**参考答案**：树形推测（`spec_tree_width>1`）或一个草稿 token 被多个模型视图共享时，同一槽位可能被多处引用；引用计数保证只有当所有引用都释放时槽位才真正回收，避免悬挂指针。见 [draft_token_workspace_manager.h:70-71](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/draft_token_workspace_manager.h#L70-L71) 与私有 `ref_count_`。

**练习 2**：若关闭推测解码（`kDisable`），`DraftTokenWorkspaceManager` 还会被创建/使用吗？

**参考答案**：不会被使用。`CreateEngineActions` 在 `kDisable` 分支只装配 `BatchDecode`（[action_commons.cc:113-125](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L113-L125)），不涉及 `BatchDraft`/`BatchVerify`，故草稿槽位池不参与运行；`ActionStepPostProcess` 的 `draft_token_workspace_manager` 参数也是 `Optional`（见 [action_commons.h:66](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.h#L66)）。

## 5. 综合实践：画出一次推测解码的时间线

**实践目标**：把本讲三个最小模块（起草、校验、AutoSpecDecode 决策）串成一条完整时间线，并亲口讲清「接受的 token 如何从草稿变成 KV cache 里的真实条目」。

**设定**：small_draft 模式、`spec_draft_length = 4`、`spec_tree_width = 1`（线性链）、单个 running 请求、`max_num_sequence` 足够大。

**操作步骤**：

1. **决策**：若该请求挂在 `AutoSpecDecode` 下（即 `spec_draft_length` 配置为 0、由自动决策），先按 [auto_spec_decode.cc:56-67](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/auto_spec_decode.cc#L56-L67) 算出 `num_running=1` 时 draft_length=4，确认走推测分支。若直接配 `spec_draft_length=4`，则 `BatchDraft`/`BatchVerify` 已在动作链里，跳过此步。

2. **起草**（[batch_draft.cc:112-293](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_draft.cc#L112-L293)）：草稿模型自回归跑 4 轮，每轮采样 1 个 token，连同概率 `ScatterDraftProbs` 进槽位池，`AddDraftToken` 写入 `draft_output_tokens`。此时 `draft_output_tokens` 有 4 项，对应 4 个槽位。

3. **校验**（[batch_verify.cc:42-272](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc#L42-L272)）：
   - `all_tokens_to_verify` = 1 个最后 committed + 4 个 draft = 5 个 token。
   - 大模型 `BatchVerify` 一次前馈算出 5 个位置的 logits（成本≈1 次 decode）。
   - `GatherDraftProbs` 取回 4 份草稿概率；拒绝采样逐一裁决。
   - 假设前 3 个 draft 被接受、第 4 个被拒并在残差分布补采 1 个 token：则 `accept_length=4`（3 个 draft + 1 个补采）。
   - 4 个接受 token 经 `CommitToken` 进两模型的 `committed_tokens`。
   - 链式回退：`rollback_length = 5 - 4 = 1`，对草稿模型 `PopNFromKVCache(..., 0)`（rollback-1=0，无需回退，因为接受到末尾）。
   - `CommitAcceptedTokenTreeNodesToKVCache` 把接受路径写进大模型物理 KV cache。

4. **画时间线**（建议手绘或用文本）：

   ```
   t0: [BatchDraft]  草稿模型 decode×4  -> draft_output_tokens=[d1,d2,d3,d4], 槽位[s0..s3]存概率
   t1: [BatchVerify] 大模型 BatchVerify(5 tokens) -> logits_p
                     GatherDraftProbs(s0..s3) -> probs_q
                     拒绝采样 -> 接受 d1,d2,d3, 补采 a4
   t2:               CommitToken×4 -> committed_tokens 末尾追加 4 个
   t3:               CommitAcceptedTokenTreeNodesToKVCache -> 大模型 KV cache 写入 4 页
                     (草稿模型 PopN 回退多余, 此例为 0)
   t4:               RemoveAllDraftTokens + FreeSlots([s0..s3]) -> 槽位归还
   ```

5. **回答关键问题**：
   - `draft_output_tokens` 是**临时逻辑草稿**，存于 `RequestModelState`，校验后被 `RemoveAllDraftTokens` 清空。
   - 接受的 token **先**经 `CommitToken` 进入 `committed_tokens`（逻辑账本），**再**经 `CommitAcceptedTokenTreeNodesToKVCache` 进入物理分页 KV cache（真实条目）。两步分离使得「逻辑记账」与「物理写入」可分别管理，也方便链式/树形统一处理。

**预期结果**：产出一张时间线图 + 一段说明，讲清「draft → verify → 接受/拒绝 → CommitToken → CommitAcceptedTokenTreeNodesToKVCache → KV cache」全链路，并指出草稿概率经槽位池跨动作传递、接受的 token 经两步（逻辑/物理）落定。

**待本地验证**：若有 GPU 与一对 LLM+SSM 模型，可启用推测解码并观察引擎日志中 `estate->metrics.spec_decode`（接受率统计）与 `/stats` 里的 `decode_tokens_per_s` 提升。否则以源码追踪为准。

## 6. 本讲小结

- 推测解码把「一次大模型前馈产出 1 token」变成「产出多个 token」，本质是**用草稿模型的廉价前向换大模型访存的空闲算力**；收益由接受率决定，校验用**拒绝采样**保证分布无偏。
- MLC LLM 支持三种模式：`small_draft`（独立小模型链式/树式起草）、`eagle`（吃大模型 hidden states、共享 lm_head、校验尾部前瞻起草）、`medusa`（大模型一次吐多步 logits）；由 `CreateEngineActions` 装配出不同的 `Action` 链。
- **起草动作**（`BatchDraft`/`EagleBatchDraft`）产出 `draft_output_tokens` 并把概率 `ScatterDraftProbs` 进槽位池；EAGLE 的首个草稿 token 在 verify 尾部产生，故 draft 循环从 1 开始。
- **校验动作**（`BatchVerify`/`EagleBatchVerify`）做大模型一次前馈 + 拒绝采样；接受 token 经 `CommitToken`（逻辑）+ `CommitAcceptedTokenTreeNodesToKVCache`（物理）两步落进 KV cache，拒绝部分经 `PopNFromKVCache`（链式）或树剪枝（树形）处理。
- **`AutoSpecDecode`** 是「动作套动作」的外壳，按并发批量查表选 draft 长度，受 `effective_batch_size > max_num_sequence` 安全闸约束；`max_num_sequence` 很小时推测会被关闭。
- **`DraftTokenWorkspaceManager`** 是跨动作的 GPU 槽位池，带引用计数，是草稿概率/隐藏状态在 draft 与 verify 之间传递的物理基础，其 scatter/gather kernel 由编译期 `AttachSpecDecodeAuxFuncs` 附加。

## 7. 下一步学习建议

- **横向对照采样器**：重读 u10-l3 的 `BatchVerifyDraftTokensWithProbAfterTopP`，把本讲的「拒绝采样」与采样器里的具体数学实现一一对应，理解 `draft_probs_on_device` 如何参与裁决。
- **回到编译期**：阅读 u8-l3 的 `attach_spec_decode_aux_funcs.py`，看 `ScatterDraftProbs`/`GatherDraftProbs`/`GatherHiddenStates` 这些 kernel 如何按张量并行度生成、如何按名字符串与本讲的 C++ 调用对齐——闭合「编译期附加 ↔ 运行期调用」契约。
- **展望分离式**：进入 u12-l3，对比 `DisaggRemoteSend`/`DisaggPrepareReceive` 与本讲动作的异同——它们同样实现了 `EngineActionObj::Step`，可插拔地插入 `actions_` 链最前（见 [action_commons.cc:127-135](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L127-L135)），理解 Action 模型如何统一支撑推测解码与分离式推理两种高级特性。
- **动手实验**（若有资源）：配置一个 small_draft 或 eagle 推测模型，对比开启/关闭推测时的 `decode_tokens_per_s`，观察 `estate->metrics.spec_decode` 的接受率随 batch 变化，验证 `AutoSpecDecode` 的查表行为。
