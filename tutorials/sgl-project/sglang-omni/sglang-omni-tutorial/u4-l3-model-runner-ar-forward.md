# ModelRunner 与 AR 前向路径

> 本讲对应大纲 `u4-l3`，承接 `u4-l2`（OmniScheduler 与 SGLang 后端）。
> 上一讲我们看到 OmniScheduler 用「组合」复用了 SGLang 的 batch 选择、内存检查与 KV 管理；本讲下钻到调度器选好 batch 之后真正「算」的那一层——`ModelRunner`。

## 1. 本讲目标

学完本讲，你应当能够：

- 画出 `ModelRunner.execute()` 的完整处理链：`ForwardBatch 构造 → before_* 钩子 → custom/标准 forward → post_* 钩子 → 采样/logit 后处理 → 输出抽取`。
- 说清 `ForwardBatch` 是什么、谁构造它、为什么它是「前向这一步的统一输入」。
- 复述采样管线做了哪些 logit 后处理（重复惩罚、codec 屏蔽、按种子采样），以及为什么这些被收敛在基类里。
- 解释 `ThinkerModelRunner` 在 prefill 的哪一步注入 image/video/audio embedding（以及 deepstack 视觉向量）。
- 解释「反馈式 AR」的概念：写缓冲 → forward → 抽码本/反馈 → 推流，并指出它在仓库里由哪些真实代码承载。

## 2. 前置知识

本讲默认你已掌握前置讲义 `u4-l1`（调度器接口与 SimpleScheduler）和 `u4-l2`（OmniScheduler 与 SGLang 后端）的内容。在此之上，补充三个术语：

- **AR（Autoregressive，自回归）**：模型一次只生成一个 token（或一个码本码字），把它拼回输入再生成下一个。文字 LLM、TTS talker、codec 解码器都是 AR。区别于「无状态来一个算一个」的 encoder/预处理阶段（它们用 SimpleScheduler，见 `u4-l1`）。
- **prefill / decode 两相**：prefill 是「处理整段 prompt」（一次吃一长串 token，对应 SGLang 的 `is_extend()`）；decode 是「逐 token 续写」（每次只前进一个位置）。`ModelRunner` 的每一步要么是 prefill，要么是 decode，钩子也分两套。
- **codebook（码本）与反馈（feedback）**：语音/TTS 模型常把一帧音频编码成多层离散码字（codebook）。talker 每步先 AR 预测第 0 层码，再用一个副头自回归预测剩余层；下一步的输入又依赖本步产出的码——这条「上一步产出喂给下一步输入」的环路就是**反馈回路**。

> 一句话定位：`Scheduler` 负责「这一步跑哪些请求、怎么排 batch」；`ModelRunner` 负责「给定排好的 batch，怎么把前向、采样、输出这一整套跑完」。二者通过 `inbox/outbox` 这两个线程安全队列解耦（见 `u3-l1`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`sglang_omni/model_runner/base.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/base.py) | 共享基类 `ModelRunner`。定义 `execute()` 全管线、`ForwardBatch` 构造、采样/logit 后处理、输出抽取，以及一组 phase 感知钩子（`before_*` / `custom_*_forward` / `post_*`）。所有 AR 模型继承它。 |
| [`sglang_omni/model_runner/thinker_model_runner.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/thinker_model_runner.py) | `ThinkerModelRunner`：Qwen3-Omni thinker 阶段的 runner，在 prefill 注入多模态 embedding + deepstack 视觉向量。 |
| [`sglang_omni/models/qwen3_omni/talker_model_runner.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/talker_model_runner.py) | `QwenTalkerModelRunner`：Qwen3-Omni talker 阶段的 runner。本讲用它作为「反馈式 AR」概念的真实落地代码。 |
| [`sglang_omni/model_runner/sglang_model_runner.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/sglang_model_runner.py) | `SGLModelRunner`：从后端 args 启动上游 SGLang `ModelRunner` 的薄包装，并把 omni 自己的模型类注册进 SGLang 的模型注册表。 |
| [`sglang_omni/scheduling/omni_scheduler.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/omni_scheduler.py) | OmniScheduler 的 `run_batch` 在此调用 `model_runner.execute(...)`（同步）或 `execute_launch/execute_resolve`（异步前瞻）。 |
| `docs/design/refactor_rfc.md` | 设计文档，描述了 `ModelRunner` 的钩子契约，以及 `FeedbackARModelRunner`/`FeedbackStrategy` 的**设计意图**。注意：这两个类名是设计稿里的概念，仓库当前并未作为独立类落地（见 4.4 的说明）。 |

---

## 4. 核心概念与源码讲解

### 4.1 ForwardBatch 与共享 execute 管线

#### 4.1.1 概念说明

`ModelRunner` 是所有 AR 阶段的**共享执行骨架**。它解决一个核心问题：不同模型（Qwen3 thinker、talker、各种 TTS）的前向细节千差万别，但「拿到一个 batch → 跑前向 → 采样 → 抽输出」这条主干是相同的。如果每个模型各自实现一遍，CUDA Graph、`torch.compile`、重复惩罚、采样种子这些横切关注点就会到处重复且容易不一致。

所以基类把**不变的主干**固化下来，把**模型相关的差异**收口到一组命名一致的「钩子」上。模型作者只覆盖需要的钩子，其余走默认。这正是设计文档强调的动机：「CUDA Graph 和 `torch.compile` 应当是类级别可共享的，而非逐模型配置——`ModelRunner` 抽象存在的部分意义就是让这成为默认」。

主干里有一个关键数据结构：`ForwardBatch`。它来自上游 SGLang（`sglang.srt.model_executor.forward_batch_info.ForwardBatch`），是「前向这一步所有请求的统一输入包」——包含 `input_ids`、`positions`、`sampling_info`、attention 元数据等。Omni 不重新发明它，而是复用 SGLang 已经算好的 batch，再在需要时往里塞多模态 embedding。

#### 4.1.2 核心流程

`execute()` 是同步全流程，也是理解一切的入口。设计文档给出的链路是：

```
ForwardBatch → before_*() → custom_*_forward() 或标准 forward → post_*() → sample/output
                  ↑ 钩子          ↑ 显式自定义前向(返回 None 则走标准)        ↑ 钩子
```

落到代码里，`execute` 分四段，每段都是独立的可复用子步骤：

1. **构造 ForwardBatch**（`_build_forward_batch`）：从 `scheduler_output` 取出 `schedule_batch`，判断是 prefill 还是 decode，决定 hidden 捕获模式，调 `ForwardBatch.init_new(...)` 得到前向输入包。
2. **prepare + forward**（`_prepare_and_forward`）：先跑 `before_prefill/before_decode`（原地改 batch），再跑 `custom_prefill_forward/custom_decode_forward`（模型可返回自己的前向结果；返回 `None` 则走标准 `tp_worker.forward_batch_generation`）。可选地在 post 之前先采样（`sample_before_post_*`）。
3. **post 钩子**（`post_prefill/post_decode`）：前向之后、输出抽取之前的模型相关处理。
4. **finalize**（`_finalize`）：必要时补采样、`output_processor.process(...)` 抽取每请求输出、推进 `generation_steps`，组装 `ModelRunnerOutput` 返回。

异步前瞻（one-step lookahead）是同一套子步骤在 post-decode 边界处**被切开**的版本：`execute_launch` 跑前两段 + 把采样结果发到 GPU/暂存缓冲并记录 CUDA Event（不等 GPU）；`execute_resolve` 等待 Event、读回结果、跑 `_finalize`。同步与异步两条路字节等价，因为它们共用同一批子步骤。

#### 4.1.3 源码精读

`execute()` 的全貌——四段子步骤一目了然：

[sglang_omni/model_runner/base.py:131-162](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/base.py#L131-L162) —— 同步全管线：build → prepare_and_forward → post_prefill/post_decode → _finalize。

关键片段（节选）：

```python
def execute(self, scheduler_output):
    built = self._build_forward_batch(scheduler_output)          # 1. 构造 ForwardBatch
    if built is None:
        return ModelRunnerOutput(outputs={}, req_ids=[], req_id_to_index={})
    forward_batch, schedule_batch, model_worker_batch, is_prefill = built
    batch_result = self._prepare_and_forward(                     # 2. before + forward(+ 可选先采样)
        forward_batch, schedule_batch, scheduler_output.requests, is_prefill
    )
    if is_prefill:
        self.post_prefill(...)                                     # 3a. post 钩子(prefill)
    else:
        self.post_decode(...)                                      # 3b. post 钩子(decode)
    return self._finalize(...)                                     # 4. 采样/输出抽取
```

`_build_forward_batch` 里「判断 phase + 构造 ForwardBatch」的核心：

[sglang_omni/model_runner/base.py:257-293](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/base.py#L257-L293) —— 决定 prefill/decode、协商 hidden 捕获模式、调 `ForwardBatch.init_new`。

```python
schedule_batch = scheduler_output.batch_data
model_worker_batch = schedule_batch.get_model_worker_batch()
is_prefill = bool(schedule_batch.forward_mode.is_extend())        # extend 即 prefill
# ... 协商 capture_hidden_mode ...
forward_batch = ForwardBatch.init_new(model_worker_batch, self.tp_worker.model_runner)
return forward_batch, schedule_batch, model_worker_batch, is_prefill
```

`_prepare_and_forward` 展示了「钩子优先、自定义前向兜底、否则走标准」的优先级：

[sglang_omni/model_runner/base.py:295-339](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/base.py#L295-L339) —— before 钩子 → custom_*_forward（返回 None 则标准 forward）→ 可选 sample-before-post。

```python
if is_prefill:
    self.before_prefill(forward_batch, schedule_batch, requests)
    batch_result = self.custom_prefill_forward(forward_batch, schedule_batch, requests)
else:
    self.before_decode(...)
    batch_result = self.custom_decode_forward(...)
if batch_result is None:                                           # 自定义前向没接管 → 走标准
    batch_result = self.tp_worker.forward_batch_generation(forward_batch)
```

钩子契约（默认全部空实现，子类按需覆盖）：

[sglang_omni/model_runner/base.py:438-482](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/base.py#L438-L482) —— `before_prefill` / `before_decode` / `custom_prefill_forward` / `custom_decode_forward` / `post_prefill` / `post_decode` 的默认实现。

输出抽取与每请求簿记（注意 `set_output_ids` 参数——它控制是否把本步 token 写回 `schedule_batch.output_ids` 供下一步构造 input_ids）：

[sglang_omni/model_runner/base.py:368-432](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/base.py#L368-L432) —— `_finalize`：补采样、`output_processor.process`、推进 `generation_steps`、组装 `ModelRunnerOutput`。

调用方在 OmniScheduler 的 `run_batch` 里——可见 execute 被调用的真实位置：

[sglang_omni/scheduling/omni_scheduler.py:875-884](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/omni_scheduler.py#L875-L884) —— `self._model_runner.execute(sched_output)` → emit stream → make batch result。

#### 4.1.4 代码实践

**实践目标**：亲手把 `execute` 的四段处理链画出来，并确认调用入口。

**操作步骤**（源码阅读型）：

1. 打开 [`base.py` 的 `execute`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/base.py#L131-L162)，按行号把它的调用依次拆成 4 个方框：`_build_forward_batch` → `_prepare_and_forward` → `post_prefill|post_decode` → `_finalize`。
2. 跟到 [`_prepare_and_forward`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/base.py#L295-L339)，圈出三处分支：`before_*` 钩子、`custom_*_forward`、`if batch_result is None` 后的标准 forward。
3. 打开 [`omni_scheduler.py:875-884`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/omni_scheduler.py#L875-L884)，确认 `execute` 就是被 `run_batch` 调起的。

**需要观察的现象**：`before_*` 与 `custom_*_forward` 是**两个独立关注点**——前者原地改 batch，后者返回前向结果（或 `None`）。设计文档明确指出，早期把二者混在一个 `prepare_forward` 钩子里是「misleading」的，[#558](https://github.com/sgl-project/sglang-omni/pull/558) 把它们拆开了。

**预期结果**：你得到一张类似下面的链路图。

```
scheduler_output
   │  _build_forward_batch          (ForwardBatch.init_new)
   ▼
forward_batch
   │  before_prefill / before_decode   (in-place mutate)
   ▼
   │  custom_prefill_forward / custom_decode_forward   (None ⇒ 标准前向)
   ▼
batch_result
   │  post_prefill / post_decode       (模型相关后处理)
   ▼
   │  _finalize  (采样 / output_processor / generation_steps++)
   ▼
ModelRunnerOutput
```

> 待本地验证：若你能在容器里跑起一个 text-only Qwen3-Omni 服务并发一条请求，可在 `_finalize` 入口加一行 `logger.info`，观察到每个 decode step 都会经过它一次。

#### 4.1.5 小练习与答案

**练习 1**：`before_decode` 和 `custom_decode_forward` 都接收 `(forward_batch, schedule_batch, requests)`，它们的返回值约定有何不同？

> **答案**：`before_decode` 返回 `None`（仅原地修改状态，例如写反馈缓冲）；`custom_decode_forward` 返回一个 batch result（`GenerationBatchResult` 之类），或返回 `None` 表示「我不接管前向，请走标准 `tp_worker.forward_batch_generation`」。

**练习 2**：为什么 `execute()` 的 docstring 说它与「pre-async 实现」字节等价？

> **答案**：因为 `execute` 只是把 `execute_launch` + `execute_resolve` 共享的同一批子步骤（`_build_forward_batch` / `_prepare_and_forward` / `_finalize`）按相同顺序串起来跑完，没有引入新的逻辑；异步只是在 post-decode 边界切开并插入一次 Event。

---

### 4.2 采样与 logit 后处理（sampling）

#### 4.2.1 概念说明

采样是「拿到 logits，决定下一个 token」的环节。Omni 把若干**与具体模型无关**的采样相关逻辑收敛进基类，保证所有 AR 模型行为一致：

- **重复惩罚（repetition penalty）**：对已生成过的 token 按比例压低/抬高 logit，避免复读。
- **codec 屏蔽（suppress_tokens）**：语音 codec 模型需要禁止某些 token id 被采到（例如填充码、终止码）。
- **按种子采样（sampling seed）**：RL rollout 要求可复现，需要按请求给定的种子决定采样结果，且在张量并行（TP）多 rank 间保持一致。
- **rollout logprob**：RL 还需要记录被采样 token 的 logprob。

真正的「采样动作」（`top_p`/`top_k`/`multinomial`）由上游 SGLang 的 sampler 完成（`tp_worker.model_runner.sample`）；Omni 在调用它**之前**做 logit 改写、在它**之后**做结果记录。

#### 4.2.2 核心流程

`_sample_next_token_ids` 是采样的总入口，顺序固定：

```
1. _apply_repetition_penalty    # 改写 next_token_logits
2. _apply_codec_suppress_tokens # 把要屏蔽的 token 置 -inf
3. _install_sampling_seeds      # 把每行种子装到 sampling_info
4. (可选) _enable_sampler_logprobs
5. tp_worker.model_runner.sample(logits, forward_batch)   # 真正采样(上游 SGLang)
6. (可选) _record_rollout_logprobs                        # 记录被采 token 的 logprob
```

关于种子有一个微妙点（见代码注释）：SGLang 的 sampler 是**整 batch 共享**的，所以在一个「部分请求带种子、部分不带」的混合 batch 里，不带种子的行不能各自用 rank 本地随机种子（那会让 TP 各 rank 采到不同结果），而是用一个**由 request_id 派生的兜底种子**——这样所有 rank 对同一行算出相同的兜底种子，从而保持 TP 一致，同时不污染用户公开的请求种子字段。

#### 4.2.3 源码精读

采样总入口与六步顺序：

[sglang_omni/model_runner/base.py:579-613](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/base.py#L579-L613) —— `_sample_next_token_ids`：rep penalty → suppress → seeds → sample → logprob 记录。

```python
def _sample_next_token_ids(self, logits_output, forward_batch, schedule_batch, requests):
    self._apply_repetition_penalty(logits_output, requests)
    self._apply_codec_suppress_tokens(logits_output, requests)
    self._install_sampling_seeds(forward_batch, requests)
    ...
    next_token_ids = self.tp_worker.model_runner.sample(logits_output, forward_batch)  # 上游采样
    ...
    return next_token_ids
```

重复惩罚——向量化实现，对 `(row, token)` 做正负两分段缩放（正 logit 除以惩罚、负 logit 乘以惩罚）：

[sglang_omni/model_runner/base.py:722-753](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/base.py#L722-L753) —— `_apply_repetition_penalty`。

codec 屏蔽——把要禁的 token 直接置 `-inf`：

[sglang_omni/model_runner/base.py:755-784](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/base.py#L755-L784) —— `_apply_codec_suppress_tokens`，先查 `data.suppress_tokens`，回退到 `req._codec_suppress_tokens`。

种子安装——注意兜底种子派生与 TP 一致性约束：

[sglang_omni/model_runner/base.py:615-645](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/base.py#L615-L645) —— `_install_sampling_seeds`：混合 batch 里无种子行用 `derive_sampling_seed("sglang-omni-unseeded-row", request_id)` 派生兜底种子。

#### 4.2.4 代码实践

**实践目标**：理解 codec 屏蔽的两个数据来源，并能复述采样各步的先后顺序。

**操作步骤**（源码阅读型）：

1. 阅读 [`_apply_codec_suppress_tokens`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/base.py#L755-L784)，注意它先读 `data.suppress_tokens`，为空时回退到 `req._codec_suppress_tokens`。
2. 用 `Grep` 在 `sglang_omni/models` 下搜索 `_codec_suppress_tokens`，看哪些 TTS 模型设置了它。
3. 回到 [`_sample_next_token_ids`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/base.py#L579-L613)，把六步顺序抄下来。

**需要观察的现象**：rep penalty 与 suppress 都是在 `sample(...)` **之前**改写 `next_token_logits`；采样动作本身完全交给上游。

**预期结果**：你能回答「如果要禁止 talker 采到 EOS 码，应该改哪个字段」——答案是把该 token id 放进 `data.suppress_tokens`（或模型在 `req._codec_suppress_tokens` 上设置）。

> 待本地验证：suppress/rep penalty 是否生效，可在 `_apply_codec_suppress_tokens` 之后打印被置 `-inf` 的 `(row, token)` 对核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么混合 batch 里，未设种子的请求行不能直接用「rank 本地随机种子」？

> **答案**：因为 SGLang 的 sampler 是 batch 级的，TP 各 rank 若用各自本地随机种子，会对同一行采出不同 token，破坏 TP 一致性。改用由 `request_id` 派生的兜底种子，能让所有 rank 对同一行得到相同种子，从而一致，且不修改用户公开的请求种子字段。

**练习 2**：采样动作（`top_p`/`multinomial`）在 Omni 代码里实现，还是在上游 SGLang？

> **答案**：在上游 SGLang。Omni 只在调用 `tp_worker.model_runner.sample(...)` 之前做 logit 改写（rep penalty / suppress / seeds），并把 sampler 的结果拿来用。这与 `u4-l2` 讲的「Omni 借 SGLang 的大脑」一致。

---

### 4.3 ThinkerModelRunner：多模态 embedding 注入

#### 4.3.1 概念说明

Qwen3-Omni 是多模态 omni 模型：thinker 阶段要处理文本 + 图像 + 视频 + 音频。难点在于——SGLang 的标准前向只认 `input_ids`（整数 token 序列），而图像/音频的输入是**连续 embedding 向量**，无法用普通 token id 表示。Qwen3-Omni 的做法是：在 prompt 里用「占位 token id」（`image_token_id`/`video_token_id`/`audio_token_id`）占位，prefill 时把这些占位位置上的文本 embedding **替换**成真正的多模态 embedding。

`ThinkerModelRunner` 就是干这件事的：它在 **prefill 阶段**接管前向（`custom_prefill_forward`），先把多模态 embedding 注入到 `input_embeds`，再带着 deepstack 视觉向量喂给 thinker 模型。decode 阶段（纯文本续写）则不需要注入，走标准路径。

> 关键时机点（本讲 practice_task 的答案）：多模态 embedding 的注入发生在 **prefill**，具体在 `custom_prefill_forward` 里、标准 forward **之前**，通过 `_inject_multimodal_embeds` 替换占位位置、再经 `_forward_with_omni_embeds` 执行前向。

#### 4.3.2 核心流程

thinker 的 prefill 流程：

```
custom_prefill_forward(forward_batch, schedule_batch, requests)
  │  若不是 extend(prefill) → 返回 None，走标准前向
  ▼
_inject_multimodal_embeds(forward_batch, schedule_batch)
  │  1. embed_tokens(input_ids) 得到文本 input_embeds
  │  2. 逐请求、逐模态(image/video/audio)：定位占位 token，切出对应 chunk 的多模态 embedding，替换到 input_embeds
  │  3. 合并 deepstack 视觉向量与全局 visual mask
  ▼
_forward_with_omni_embeds(forward_batch, input_embeds, ds_embeds, vis_masks)
  │  outer.model(input_embeds=..., input_deepstack_embeds=...)  ← 自定义前向
  │  outer.logits_processor(...) → logits_output
  ▼
GenerationBatchResult(logits_output, can_run_cuda_graph=False)
```

两个细节值得注意（来自源码注释）：

- **chunked prefill 的消耗记账**：长 prompt 会被切成多个 chunk 分批 prefill，所以每个模态要记录「本请求已消耗多少 embedding」（`req._omni_consumed`），下次接着切。只有当 `req.is_chunked == 0`（本 chunk 是最后一段）时才清空 `omni_model_inputs`。
- **hidden 捕获模式返回 NULL**：thinker 流式所需的 hidden state 是通过**本地 forward 钩子**捕获的，不走 SGLang 的 logits-output hidden 通道；若请求 `LAST` 模式反而会和 CUDA Graph replay 冲突。

#### 4.3.3 源码精读

`ThinkerModelRunner` 只在 prefill 接管前向：

[sglang_omni/model_runner/thinker_model_runner.py:78-89](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/thinker_model_runner.py#L78-L89) —— `custom_prefill_forward`：仅 extend 相触发注入。

```python
def custom_prefill_forward(self, forward_batch, schedule_batch, requests):
    if not schedule_batch.forward_mode.is_extend():
        return None                                  # decode 不注入，走标准前向
    omni_result = self._inject_multimodal_embeds(forward_batch, schedule_batch)
    if omni_result is not None and omni_result[0] is not None:
        input_embeds, ds_embeds, vis_masks = omni_result
        return self._forward_with_omni_embeds(forward_batch, input_embeds, ds_embeds, vis_masks)
    return None
```

注入主体——逐请求逐模态替换占位位置（节选关键段）：

[sglang_omni/model_runner/thinker_model_runner.py:115-175](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/thinker_model_runner.py#L115-L175) —— `_inject_multimodal_embeds`：定位占位 token、切 chunk、写回 `input_embeds`、记账 `_omni_consumed`。

```python
input_embeds = self._embed_tokens(embed_input_ids)            # 先得到文本 embedding
for modality, token_id in [("image", image_token_id),
                           ("video", video_token_id),
                           ("audio", audio_token_id)]:
    embeds = omni_inputs.get(f"{modality}_embeds")
    if embeds is None:
        continue
    mask = req_input_ids == match_id                          # 占位 token 位置
    ...
    chunk_embeds = embeds[offset : offset + n_tokens].to(...) # 切本 chunk 对应片段
    input_embeds[torch.where(mask)[0] + start] = chunk_embeds # 替换占位
    consumed[modality] = offset + n_tokens                    # 记账，下次接着切
```

自定义前向——带 deepstack 视觉向量喂模型：

[sglang_omni/model_runner/thinker_model_runner.py:278-325](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/thinker_model_runner.py#L278-L325) —— `_forward_with_omni_embeds`：组装 deepstack 全局张量、调 `outer.model(...)`、过 `logits_processor`。

构造函数——从 thinker hf_config 取三个占位 token id：

[sglang_omni/model_runner/thinker_model_runner.py:25-45](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/thinker_model_runner.py#L25-L45) —— 拿到 `image_token_id` / `video_token_id` / `audio_token_id`。

#### 4.3.4 代码实践

**实践目标**：定位「image/audio embedding 在哪一步注入」，并理解占位 token 机制。

**操作步骤**（源码阅读型）：

1. 从 [`custom_prefill_forward`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/thinker_model_runner.py#L78-L89) 出发，确认注入**只在 prefill（extend 相）**发生。
2. 进 [`_inject_multimodal_embeds`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/thinker_model_runner.py#L115-L175)，在 `for modality, token_id in [...]` 这一行停下，确认它遍历 `image/video/audio` 三类，靠「占位 token id 相等」定位替换位置。
3. 用 `Grep` 在 `sglang_omni/models/qwen3_omni` 下搜 `omni_model_inputs`，看上游哪一步（image_encoder/audio_encoder/mm_aggregate）把 `{modality}_embeds` 塞进 `req.omni_model_inputs`。

**需要观察的现象**：替换发生在标准 forward 之**前**（因为 `custom_prefill_forward` 返回非 None，`_prepare_and_forward` 就不会再调 `tp_worker.forward_batch_generation`）。

**预期结果**：你能写出一句话——「`ThinkerModelRunner` 在 prefill 的 `custom_prefill_forward` 里，于标准前向之前，把 `image/video/audio` 占位 token 处的文本 embedding 替换为真正的多模态 embedding」。

> 待本地验证：在 `_inject_multimodal_embeds` 的替换行后打印 `torch.where(mask)[0]` 的长度，可核对每模态替换的 token 数与上游产出是否一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么注入只在 prefill 做，decode 不做？

> **答案**：多模态输入只出现在 prompt 里（占位 token 集中在 prefill 段）；decode 阶段每步只续写一个文本 token，输入位置上没有多模态占位需要替换，所以 decode 走标准前向即可（`custom_prefill_forward` 对 decode 直接返回 None，且没有 `custom_decode_forward` 覆盖）。

**练习 2**：`_omni_consumed` 这个字典解决什么问题？

> **答案**：长 prompt 被分成多个 chunk 做 chunked-prefill，每个模态的多模态 embedding 是一整段连续向量，需要按 chunk 顺序「消耗」——`_omni_consumed[modality]` 记录本请求该模态已经替换了多少，让下一个 chunk 从 `offset` 接着切，避免重复注入或错位。

---

### 4.4 反馈式 AR：写缓冲 → forward → 抽码本/反馈 → 推流

#### 4.4.1 概念说明

语音 codec 类 AR 模型（Qwen3 talker、Fish TTS、各种 TTS）有一个共同结构：**每一步的前向产出会喂给下一步作为输入**。talker 每步先用 AR 主干预测第 0 层码，再用副头自回归预测剩余码本层，产出「多层码字（codes）」和「下一步要用的反馈向量（feedback embeds）」。这与纯文本 LLM「采完 token 直接拼回 input_ids」不同——反馈是连续向量，且需要按帧流式吐给下游 vocoder。

设计文档把这种「反馈环自包含在单个 ModelRunner 实例内」的执行模式称为 **FeedbackARModelRunner**，并把「写缓冲 / 抽输出 / prefill 前向」三件事抽象成三个回调（或一个 `FeedbackStrategy` 对象）。

> ⚠️ **重要事实核对**：`FeedbackARModelRunner` 和 `FeedbackStrategy` 这两个名字**只出现在设计文档 [`docs/design/refactor_rfc.md`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/design/refactor_rfc.md) 里**（见 407–498 行），是设计意图/拟演进的抽象。在当前 HEAD（`cf61f234`）的**真实代码中并不存在这两个类**，也没有 `models/<name>/callbacks.py` 文件。本仓库把这一模式**直接内联**到各模型的 `ModelRunner` 子类里——靠基类的 `before_decode`（写缓冲）与 `post_decode`（抽输出 + 推流）钩子实现，每个 TTS/talker 模型各写一份。下面用**真实存在**的 `QwenTalkerModelRunner` 讲清这套机制。

#### 4.4.2 核心流程

talker 的反馈循环（一个 decode step）：

```
before_decode:
  prepare_decode_buffers(requests)          # 模型内部准备缓冲
  _write_feedback_buffers(requests)         # 把「上一步的 feedback + 下一个 text embed」写进 model._feedback_buffer
        │
        ▼  (随后 _prepare_and_forward 跑标准/自定义前向；模型 forward 内部读 _feedback_buffer)
forward: AR 主干 → 第0层码 → 副头预测剩余码本层 → 存 model._output_codes / _output_embeds
        │
        ▼
post_decode:
  result.next_token_ids = model._sampled_token_ids[:bs]   # 第0层码作为「token」
  _emit_code_chunks_and_feedback(...):
     for 每个请求:
        code_chunk  = model._output_codes[idx]             # 抽码本(多层码字)
        feedback_row = model._output_embeds[idx]           # 抽反馈(下一步输入)
        outbox.put(OutgoingMessage(type="stream",          # 推流:码本 chunk → code2wav 阶段
                    data=code_chunk, target="code2wav"))
        data.pending_feedback_queue.append(feedback_row)   # 反馈入队,供下一步 before_decode 取用
```

这条环路的「状态」挂在每请求数据上：`pending_feedback_queue`（待消费的反馈向量）和 `pending_text_queue`（待消费的 thinker 文本 embed）。`before_decode` 从这两个队列里 `peek`/`pop` 出来相加，写进模型缓冲；`post_decode` 把新产出的反馈 push 回队列。由此形成闭环。

> 数据结构证据：[`SGLangARRequestData`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/sglang_backend/request_data.py#L13-L34) 里有 `pending_feedback_queue`、`pending_text_queue`、`tts_pad_embed`、`thinker_chunks_done` 等字段，正是这条反馈环的载体。

#### 4.4.3 源码精读

`before_decode`：先校验输入就绪、再准备缓冲、最后写反馈——

[sglang_omni/models/qwen3_omni/talker_model_runner.py:44-64](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/talker_model_runner.py#L44-L64) —— `before_decode` → `prepare_decode_buffers` → `_write_feedback_buffers`。

写反馈缓冲——把「上一步 feedback + 下一个 text embed」相加写进 `model._feedback_buffer` 并置 mask：

[sglang_omni/models/qwen3_omni/talker_model_runner.py:338-364](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/talker_model_runner.py#L338-L364) —— `_write_feedback_buffers`：逐行取 `_take_next_decode_input_embed`、`torch.stack` 后写 `_feedback_buffer[rows]`、`_feedback_mask[rows]=True`。

「feedback + text」的合并逻辑（窥探两个队列，text 缺失时用 pad embed 兜底）：

[sglang_omni/models/qwen3_omni/talker_model_runner.py:434-463](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/talker_model_runner.py#L434-L463) —— `_combine_feedback_with_next_text`。

`post_decode`：抽第 0 层码当 token、抽码本与反馈、推流——

[sglang_omni/models/qwen3_omni/talker_model_runner.py:93-109](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/talker_model_runner.py#L93-L109) —— `post_decode` → `_emit_code_chunks_and_feedback`。

推流与反馈入队的核心循环——

[sglang_omni/models/qwen3_omni/talker_model_runner.py:111-136](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/talker_model_runner.py#L111-L136) —— `_emit_code_chunks_and_feedback`：`code_chunk → outbox(type=stream, target=code2wav)`，`feedback_row → pending_feedback_queue`。

```python
for idx, sched_req in enumerate(requests):
    code_chunk = self.model._output_codes[idx].detach().clone()      # 抽码本
    feedback_row = self.model._output_embeds[idx].detach().clone()   # 抽反馈
    self._outbox.put(OutgoingMessage(
        request_id=req.rid, type="stream",
        data=code_chunk, target=self._code2wav_target,               # 推流给 code2wav
        metadata={"stream": is_streaming},
    ))
    sched_req.data.pending_feedback_queue.append(feedback_row)       # 反馈入队,下一步消费
```

构造函数——`feedback_enabled` 开关与 `code2wav_target`：

[sglang_omni/models/qwen3_omni/talker_model_runner.py:15-29](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/talker_model_runner.py#L15-L29) —— `QwenTalkerModelRunner.__init__`。

补充：`SGLModelRunner` 的真正职责是「启动上游 SGLang ModelRunner + 注册 omni 模型类」，它并不参与前向管线，而是为上面的 runner 提供可被 SGLang 调度的模型对象——

[sglang_omni/model_runner/sglang_model_runner.py:78-118](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/sglang_model_runner.py#L78-L118) —— `_register_omni_model`：把 `Qwen3OmniTalker`/`Qwen3OmniThinkerForCausalLM` 等类注册进 SGLang 的 `ModelRegistry`。

#### 4.4.4 代码实践

**实践目标**：把反馈环的「写缓冲 → forward → 抽码本/反馈 → 推流」四步对到真实方法上。

**操作步骤**（源码阅读型）：

1. 在 [`talker_model_runner.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/talker_model_runner.py) 里定位四个方法：`before_decode`（L44）、`_write_feedback_buffers`（L338）、`post_decode`（L93）、`_emit_code_chunks_and_feedback`（L111）。
2. 在 [`request_data.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/sglang_backend/request_data.py#L13-L34) 里圈出 `pending_feedback_queue` 与 `pending_text_queue`，确认它们是反馈环的载体。
3. 用 `Grep` 在 `sglang_omni/models/qwen3_tts/model_runner.py` 搜 `_write_feedback_buffers`，确认这是**跨模型复用的模式**（Qwen3-TTS 也有同名方法，见 [`qwen3_tts/model_runner.py:56,183`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/model_runner.py#L183)），而非 Qwen3-Omni 独有。

**需要观察的现象**：`post_decode` 既「推流」（往 outbox 放 `type="stream"`）又「回填反馈」（往 `pending_feedback_queue` append）；下一步的 `before_decode` 再从队列取出写进模型缓冲——闭环由此成立。

**预期结果**：你能画出 talker 反馈环的状态图（见下方 5. 综合实践）。

> 待本地验证：在 `_emit_code_chunks_and_feedback` 的 append 行后打印 `len(sched_req.data.pending_feedback_queue)`，观察每个 decode step 它先 +1（post 写入）、下一步 before_decode 又 -1（消费）的交替。

#### 4.4.5 小练习与答案

**练习 1**：为什么 RFC 设想的 `FeedbackStrategy` 抽象在当前仓库里没有作为独立类落地？

> **答案**：因为目前只有少数自包含反馈模型（Qwen3 talker、Fish TTS、Qwen3-TTS 等），把三件事（写缓冲/抽输出/prefill 前向）直接写进各自的 `ModelRunner` 子类够用且更显式。RFC 自己也说：bare-function 形式是「what ships today」，Strategy 对象是「如果有第三个自包含模型加入」时的推荐演进。

**练习 2**：`pending_feedback_queue` 与 `pending_text_queue` 各装什么？为什么 text 缺失时能用 `tts_pad_embed` 兜底？

> **答案**：`pending_feedback_queue` 装上一步 talker 产出的反馈向量；`pending_text_queue` 装上游 thinker 流式送来的文本 embed。当 thinker 还在产出文本、但 talker 已经需要续写时（`thinker_chunks_done` 为真且 `tts_pad_embed` 存在），用 pad embed 兜底，保证 talker 的 decode 输入维度完整、不被空缺卡住。

---

## 5. 综合实践

**任务**：画一张 Qwen3-Omni talker 的「单次 decode step 全链路」时序图，把本讲四个模块串起来。

要求在一张图里同时体现：

1. **execute 主干**（4.1）：`_build_forward_batch → _prepare_and_forward → post_decode → _finalize`。
2. **采样位置**（4.2）：在 `_finalize` / `_prepare_and_forward` 内标注「`_sample_next_token_ids`（rep penalty→suppress→seeds→sample）」。
3. **反馈环**（4.4）：`before_decode` 写 `_feedback_buffer` → forward 读它 → `post_decode` 抽 `_output_codes`/`_output_embeds` → 推流 code2wav + 回填 `pending_feedback_queue`。
4. **与 thinker 的衔接**：标注 thinker 的多模态注入（4.3）发生在**另一个 runner**（`ThinkerModelRunner`）的 prefill，其产出的 hidden state 经 `stream_to` 喂给 talker，落到 talker 的 `pending_text_queue`。

参考画法（文字版时序）：

```
[thinker runner / prefill]                      [talker runner / decode step N]
  custom_prefill_forward                          before_decode
    _inject_multimodal_embeds (image/audio)         _write_feedback_buffers
    _forward_with_omni_embeds                         ← 从 pending_feedback_queue 取上一步反馈
    hidden state ──stream_to──┐                       + pending_text_queue 取 text embed
                               ▼                     → 写 model._feedback_buffer
                        pending_text_queue          _prepare_and_forward (forward 读缓冲)
                                                          │ _sample_next_token_ids
                                                          │   (rep penalty→suppress→seed→sample)
                                                          ▼
                                                       post_decode
                                                         _emit_code_chunks_and_feedback
                                                           ├─ code_chunk ──stream──► code2wav
                                                           └─ feedback_row ──► pending_feedback_queue (供 N+1)
                                                       _finalize → ModelRunnerOutput
```

**验收标准**：你能指着图回答三个问题——(a) 多模态 embedding 在 thinker 的哪一步注入？（prefill 的 `custom_prefill_forward`，标准前向之前）(b) talker 的反馈从哪来、到哪去？（上一步 `post_decode` 写入队列，本步 `before_decode` 取出写缓冲）(c) 采样动作本身在 Omni 还是 SGLang？（SGLang 的 `sample`，Omni 只做前置 logit 改写）。

> 待本地验证：若本地有 GPU 环境，可用 `python -m sglang_omni.profiler`（见 `u6-l3`）跑一次请求，对照 timeline 报告核对 thinker prefill 与 talker decode 的相对时序。

## 6. 本讲小结

- `ModelRunner` 是所有 AR 阶段的**共享执行骨架**：`execute()` 把流程固化为 `build ForwardBatch → before_* → custom/标准 forward → post_* → finalize`，模型差异收口到命名一致的钩子。
- `ForwardBatch` 来自上游 SGLang，是「前向这一步的统一输入包」；Omni 复用而非重造它，仅在需要时往里塞多模态 embedding。
- 采样管线在基类统一：rep penalty → codec suppress → 装种子 → 调上游 `sample` →（可选）记 logprob；无种子行用 request_id 派生兜底种子以保持 TP 一致。
- `ThinkerModelRunner` 在 **prefill 的 `custom_prefill_forward`**、标准前向之前，把 image/video/audio 占位 token 处的 embedding 替换为真正的多模态向量，并带上 deepstack 视觉向量。
- 「反馈式 AR」（设计文档称 `FeedbackARModelRunner`/`FeedbackStrategy`）在当前仓库**内联**到各模型 runner，靠 `before_decode` 写缓冲 + `post_decode` 抽码本/反馈/推流实现闭环；`QwenTalkerModelRunner` 是其真实样本。
- OmniScheduler 在 `run_batch` 里调 `execute`（同步）或 `execute_launch`+`execute_resolve`（异步前瞻）；二者共用同一批子步骤，字节等价。

## 7. 下一步学习建议

- **`u4-l4` 流式调度器与流式 vocoder**：本讲看到 talker 通过 `outbox.put(type="stream")` 把码本 chunk 推给 `code2wav`，下一讲讲下游 `Code2WavScheduler` 如何累积这些 chunk、在生成结束前就吐出音频。
- **`u5-l2` Qwen3-Omni 端到端管线**：把 thinker（4.3）与 talker（4.4）放回整条 stage DAG，理解 `stream_to` 如何把 thinker hidden state 流式喂给 talker。
- **`u6-l3` 请求级 Profiler**：想观察本讲描述的 prefill/decode 与反馈环在真实请求里的耗时分布，用 profiler 的 timeline/stage 报告定位瓶颈。
- 继续阅读：基类的异步前瞻 [`execute_launch`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/base.py#L164-L214)/[`execute_resolve`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/base.py#L216-L255) 及 [`lookahead_eligible`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/base.py#L484-L503)，理解「哪些 batch 能走异步、哪些必须回退同步」。
