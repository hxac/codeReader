# 讲义 u8-l3：Decoder 与 Sampling

## 1. 本讲目标

本讲承接 u3-l2（PyExecutor 单步循环），拆开单步循环里「模型前向之后、响应回写之前」的那一段——**采样（Sampling）**。模型前向产出的是每个 token 位置上的 logits（一张「词表得分表」），而真正决定「下一个 token 是什么」的，是采样器。

学完本讲，你应当能够：

- 说清「logits → 下一个 token」这一步在 `pyexecutor` 中由谁负责、按什么顺序处理。
- 掌握 `SamplingParams` 的主要参数（`temperature` / `top_k` / `top_p` / `min_tokens` / `bad` / `stop` / `logits_processor` 等）以及「什么时候算贪心、什么时候算采样」的判定规则。
- 理解 `sampler.py` + `sampling_utils.py` 如何把一批请求**按策略分组**、再交给 FlashInfer 内核完成真正的采样。
- 说明 `guided_decoder.py`（JSON / regex / EBNF 语法约束）如何通过「对 logits 做位掩码」来强制输出格式，以及它和采样器、logits processor 三者的协作关系。

---

## 2. 前置知识

在进入源码前，先建立两个直觉。

### 2.1 从 logits 到 token：一次「按权重抽奖」

模型每前进一步，对「下一个位置」会输出一个长度等于词表大小 `V` 的向量 `z = (z_0, z_1, …, z_{V-1})`，称为 **logits**（未归一化的得分）。采样要把它变成一个具体的 token id。最朴素的两种做法：

- **贪心（greedy / argmax）**：直接取得分最大的那一个，`token = argmax(z)`。确定、可复现，但单调。
- **概率采样**：先把 logits 转成概率分布，再按概率「掷骰子」抽一个 token。把 logits 转成概率最常见的是配合温度 `T` 的 softmax：

\[
p_i = \frac{\exp(z_i / T)}{\sum_{j} \exp(z_j / T)}
\]

温度 `T` 越高，分布越平（更随机）；`T → 0` 时退化为贪心。在此基础上，`top_k`（只在概率最高的 k 个里抽）、`top_p`（只在累计概率达到 p 的最小集合「核」里抽，即 nucleus sampling）、`min_p`（按最大概率乘一个比例作为下限）等都是对「抽奖范围」的进一步收窄。

### 2.2 罚分、bias、bad/stop：在抽奖之前「动手术」

采样之前，常常需要对 logits 做几道预处理：

- **embedding bias / logit bias**：手工给某些 token 加一个偏置（鼓励或打压）。
- **bad words / stop words**：把某些 token 的 logit 置成 `-inf`，使其绝不可能被采到（ban）或一旦采到就停止。
- **min_tokens / 最小长度惩罚**：在生成长度未达到下限前，把结束符 EOS 的 logit 置成 `-inf`，防止过早收尾。
- **logits processor**：用户自定义的回调，在采样前对 logits 做任意就地改写。

> 关键心法：**所有这些都是在「采样」之前对 logits 张量动手**。采样器（sampler）看到的，是已经被这些预处理「调教」过的 logits。本讲会反复回到这一点。

### 2.3 它在 PyExecutor 单步循环里的位置

回顾 u3-l2：单步循环主干是 `取请求 → 调度 → prepare_resources → 前向 → 采样 → _handle_responses → update_resources`。本讲聚焦其中的「采样」一环：

```text
模型前向产出 model_outputs["logits"]
        │
        ▼
(可选) logits processor 改写 logits          ← 在 model_engine.py
        │
        ▼
(可选) guided decoding 位掩码 logits          ← 在 guided_decoder.py
        │
        ▼
Sampler.sample_async(...)                     ← 本讲主角
        │
        ▼
new_tokens（采样得到的下一个 token）→ update_requests → 回写响应
```

注意：logits processor 与 guided decoding 都发生在采样**之前**，但它们的代码并不在 `sampler.py` 里——这是 TRT-LLM 一个容易让人迷惑的设计点，本讲 4.2 与 4.3 会讲清边界。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tensorrt_llm/sampling_params.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/sampling_params.py) | 用户面 `SamplingParams` 数据类，以及 `GuidedDecodingParams`、`LogitsProcessor` / `BatchedLogitsProcessor` 抽象基类 |
| [tensorrt_llm/_torch/pyexecutor/sampler/sampler.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/sampler/sampler.py) | 采样器继承体系：`Sampler`（ABC）→ `TorchSampler`（默认）、`TRTLLMSampler`（已弃用）、`EarlyStopSampler` 等 |
| [tensorrt_llm/_torch/pyexecutor/sampler/sampling_utils.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/sampler/sampling_utils.py) | 策略解析 `resolve_sampling_strategy`、FlashInfer 内核封装 `_StrategyImpls`、分组采样 `FlashInferGroupedStrategySampler` |
| [tensorrt_llm/_torch/pyexecutor/guided_decoder.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/guided_decoder.py) | `GuidedDecoder` / `CapturableGuidedDecoder`：用语法匹配器生成位掩码，约束 logits |
| [tensorrt_llm/_torch/pyexecutor/model_engine.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/model_engine.py) | logits processor 的实际执行点 `_execute_logit_post_processors`，以及 guided decoder 的挂载点 |
| [docs/source/features/sampling.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/sampling.md) | 官方功能矩阵与用法示例 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**SamplingParams**（4.1，参数表）、**Sampler**（4.2，采样器与策略解析）、**Guided Decoding**（4.3，语法约束）。

---

### 4.1 SamplingParams：采样的「参数表」

#### 4.1.1 概念说明

`SamplingParams` 是用户最常打交道的采样配置对象。它是一个带 `@dataclass` 的翻译层：

- 对**用户**：提供一个统一的、字段丰富的 Python 数据类（`temperature`、`top_p`、`top_k`、`n`、`best_of`、`stop`、`bad`、`logits_processor`、`guided_decoding`、`logprobs` 等）。
- 对**运行时**：在内部把这些字段拆装成 C++ 绑定的两个对象——`tllme.SamplingConfig`（采样策略）和 `tllme.OutputConfig`（输出附加项），以及 `tllme.Request` 的部分字段。

一句话：`SamplingParams` 是「一套参数，三处生效」的中枢。它的校验比 C++ 运行时更严格（在 LLM API 层就拦掉一些危险组合，例如贪心 + `best_of > 1`）。

#### 4.1.2 核心流程

`SamplingParams` 的生命周期分三步：

1. **构造**：用户传入字段；`__post_init__` 做收尾（`pad_id` 缺省回退到 `end_id`、`best_of` 缺省回退到 `n`、`embedding_bias` 转 tensor），并调 `_validate()`。
2. **校验**：`_validate()` 检查取值范围（`0 <= top_p <= 1`、`top_k >= 0`、`temperature >= 0`、`best_of >= n` 等），并把贪心 + 多返回的组合判为非法（除非设了 `TLLM_ALLOW_N_GREEDY_DECODING=1`）。
3. **翻译**：在请求提交阶段，分别由 `_get_sampling_config()` / `_get_output_config()` / `_get_guided_decoding_params()` 翻译成 C++ 对象。

「贪心 vs 采样」的判定不是分散判断，而是集中在三个静态方法里（这是全仓库判定贪心的**唯一真相源**，TorchSampler 也会复用）：

```text
temperature/top_p/top_k/top_p_decay/use_beam_search
                       │
                       ▼
   params_imply_greedy_decoding(...)   ← 静态方法，单一真相源
                       │
            ┌──────────┴───────────┐
        True│                  False│
            ▼                      ▼
         贪心               按 temperature/top_k/top_p 采样
```

#### 4.1.3 源码精读

`SamplingParams` 是 `@dataclass(slots=True, kw_only=True)`，字段注释极详尽（这里只摘关键三类）：

[sampling_params.py:171-L172](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/sampling_params.py#L171-L172) 定义了 `SamplingParams`。其字段可归为三族：

- **采样策略族**：`top_k` / `top_p` / `top_p_min` / `top_p_reset_ids` / `top_p_decay` / `temperature` / `min_p` / `seed`（[sampling_params.py:290-L307](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/sampling_params.py#L290-L307)），注释里特别说明 `None` = 未指定。
- **多路生成族**：`n` / `best_of` / `use_beam_search`（[sampling_params.py:284-L287](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/sampling_params.py#L284-L287)）。
- **停止/约束族**：`bad` / `bad_token_ids` / `stop` / `stop_token_ids` / `min_tokens` / `logits_processor` / `guided_decoding`（[sampling_params.py:272-L282](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/sampling_params.py#L272-L282)）。

构造后立即校验：[sampling_params.py:367-L411](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/sampling_params.py#L367-L411) 是 `_validate()`，做范围检查，并禁止「贪心 + `best_of > 1`」（除非显式开关）。注意注释点明：LLM API 比 C++ 运行时更严格。

贪心判定的「单一真相源」三个静态方法：[sampling_params.py:461-L498](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/sampling_params.py#L461-L498)。要点（务必记住这条规则）：

- **显式贪心**：`top_k == 1`、`top_p == 0.0`、`temperature == 0` 任一成立 → 贪心，**且优先级最高**（即便设了 top-p decay 也强行贪心）。
- **隐式贪心**：`temperature`、`top_p`、`top_k` 全为 `None` → 贪心；但若 `top_p_decay` 处于激活态（`< 1`），则改为走 top-p 采样，好让「逐 step 衰减的运行时 top_p」能生效。

翻译示例——`_get_sampling_config()` 把字段映射到 `tllme.SamplingConfig`，并处理 `best_of`/`n`/`use_beam_search` 到 C++ `beam_width`/`num_return_sequences` 的不等价映射：[sampling_params.py:602-L632](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/sampling_params.py#L602-L632)。

> 一个容易踩坑的点：`repetition_penalty` / `presence_penalty` / `frequency_penalty` / `min_p` 这些字段确实存在于 `SamplingParams`，并被翻译进 C++ `SamplingConfig`，但 **PyTorch 后端的 `TorchSampler` 在每步采样的策略解析里只消费 `temperature` / `top_p` / `top_k` / `top_p_decay`**（见 4.2.3 的 `resolve_sampling_strategy`）。这几个「罚分 / min_p」字段属于 C++ SamplingConfig 的管线（服务于已弃用的 `TRTLLMSampler` 与历史路径），并非 TorchSampler 当前热路径的一部分。若你需要这些罚分，目前应通过 `logits_processor` 自行实现（见 4.2.3）。

#### 4.1.4 代码实践

**实践目标**：直观感受 `temperature` / `top_p` 对同一提示的影响，并验证贪心判定的可复现性。

**操作步骤**（需 GPU + 已安装 TensorRT-LLM，参见 u1-l2/u1-l3）：

1. 用贪心跑两次同一提示，观察输出是否**逐字一致**。
2. 用高温度 + 收窄 top_p 跑两次，观察输出是否**不同**但更「聚焦」。

```python
# 示例代码：参照 docs/source/features/sampling.md 的用法改写
from tensorrt_llm import LLM, SamplingParams

llm = LLM(model="nvidia/Llama-3.1-8B-Instruct-FP8")

prompt = "Hello, my name is"

# 1) 贪心：temperature/top_p/top_k 全 None → 隐式贪心
greedy = SamplingParams(max_tokens=32)
# 2) 采样：温度 1.0，top_k=8，top_p=0.5
sample = SamplingParams(max_tokens=32, temperature=1.0, top_k=8, top_p=0.5)

out1 = llm.generate([prompt, prompt], [greedy, greedy])
out2 = llm.generate([prompt, prompt], [sample, sample])
```

**需要观察的现象**：
- 贪心两次输出完全相同；采样两次输出不同。
- 采样输出由于 `top_k=8, top_p=0.5` 收窄了抽奖范围，应比纯高温度更连贯。

**预期结果**：贪心可复现；采样不可复现（除非设 `seed`）。如本机无法运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：用户传 `SamplingParams(top_k=1, temperature=2.0, top_p=0.3)`，实际会走贪心还是采样？为什么？
**答案**：贪心。因为「显式贪心」规则里 `top_k == 1` 任一成立即贪心，优先级高于其它参数。

**练习 2**：为什么 `_validate()` 要在 LLM API 层禁止「贪心 + `best_of > 1`」，而 C++ 运行时却允许？
**答案**：贪心每次都取 argmax，多条采样结果会完全相同，`best_of > 1` 毫无意义还浪费算力；LLM API 选择「早失败、给清晰错误」，而 C++ 运行时为保持兼容性允许它（可用 `TLLM_ALLOW_N_GREEDY_DECODING=1` 放开）。

**练习 3**：`top_p_decay` 处于激活态、但 `top_p` 未指定时，会走贪心还是 top-p 采样？
**答案**：top-p 采样。因为 `params_imply_greedy_decoding` 在「全 None 隐式贪心」分支里会检查 `top_p_decay` 是否激活，激活则不走贪心，以便衰减后的运行时 top_p 能真正生效。

---

### 4.2 Sampler：采样在 pyexecutor 中的组织

#### 4.2.1 概念说明

`Sampler` 是 PyExecutor 单步循环里「前向之后、回写之前」的一环。它接收已经算好的 `model_outputs["logits"]`，产出 `new_tokens`（每个请求采到的下一个 token）。仓库里有一条继承体系，**按模型类型 / 后端选择不同的 Sampler**：

- `Sampler`（ABC）：定义 `sample_async` / `update_requests` / `is_generation_model` 等抽象接口。
- `TorchSampler`：**默认**采样器，Python + FlashInfer 内核，功能最全。
- `TRTLLMSampler`：基于 C++ 旧实现，**已弃用**，将在 1.4 版移除。
- `EarlyStopSampler`：用于非生成模型（如 BERT、奖励模型），跳过解码直接收尾。
- `EarlyStopWithMMResult`：用于多模态 encoder-only 模型，直接取 batch 输出。

关键术语：
- **策略（Strategy）**：把一组采样参数归约成的一个可枚举标签，如 `greedy` / `top_k` / `top_p` / `top_k_top_p` / `temperature` / `beam_search`。
- **分组采样（grouped sampling）**：把一批请求里「策略相同」的请求合并成一组，一次 FlashInfer 内核调用处理整组，显著降低异构 batch 的采样开销。

#### 4.2.2 核心流程

`TorchSampler` 的单步采样（`sample_async` → `_process_requests`）主干如下：

```text
sample_async(scheduled_requests, model_outputs)
  │
  ▼
_process_requests:
  1. _select_generated_logits       取出「需要采样的那几个位置」的 logits
  2. _apply_embedding_bias          对 logits 加偏置（logit bias）
  3. _apply_min_length_penalty       未达最小长度 → 把 EOS 的 logit 置 -inf
  4. _apply_bad_words               把 bad words 末 token 的 logit 置 -inf
  5. 分支：
     ├─ 全是贪心且不需要 logprobs → _fast_greedy_sample_kernel（argmax 快路径）
     └─ 否则                    → _sample_batched_by_strategy（按策略分组 + FlashInfer）
  6. (可选) _process_logprobs        计算/收集 logprobs
  7. _unbatch_sampling_results      把结果散回每个请求的输出槽
  │
  ▼
update_requests(state)              把 new_tokens 写回各 LlmRequest，处理停止条件/finish_reason
```

「按策略分组」这条慢路径内部，又分两层路由：

1. **第一层**：`resolve_sampling_strategy`（在 `sampling_utils.py`）把每个请求的 `UtilsSamplingParams` 解析成一个 `Strategy` 元组。
2. **第二层**：`FlashInferGroupedStrategySampler.sample_grouped_strategies` 按 `strategy_grouping_key` 把请求分桶，再为每桶选一个 `_StrategyImpls.*` 实现（如 `TopKTopPWithProbs`）调 FlashInfer 内核。

> 性能要点（来自 sampling.md）：TorchSampler 用 FlashInfer 的采样内核，并尽量走「免排序」实现——除非用户要 logprobs 或投机解码的拒绝采样，否则不计算完整的采样概率，省下大量开销。

#### 4.2.3 源码精读

**Sampler 抽象基类**：[sampler.py:197-L231](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/sampler/sampler.py#L197-L231)。`sample_async` 是抽象方法，返回一个 `SampleState`（携带 `device`/`host` 两套张量与一个 `sampler_event`）。`update_requests` 负责把结果写回请求。`EarlyStopSampler`（[sampler.py:253-L288](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/sampler/sampler.py#L253-L288)）则直接把请求标成 `GENERATION_COMPLETE`，体现「非生成模型无需解码」。

**Sampler 的选择**不在 sampler.py 里，而在 `_util.py` 的装配点：[_util.py:2645-L2675](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/_util.py#L2645-L2675)。判定顺序很说明问题：STAR attention → TorchSampler；有投机解码 → `get_spec_decoder`；mm encoder-only → `EarlyStopWithMMResult`；显式选 `TRTLLMSampler` → 走弃用路径并打 warning；非生成模型 → `EarlyStopSampler`；**否则默认 TorchSampler**。

**主入口 `sample_async`**：[sampler.py:4091-L4209](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/sampler/sampler.py#L4091-L4209)。它被 `@torch.inference_mode()` 与 `@nvtx_range("sample_async")` 装饰；先 `setup_sampler_step`，再 `_process_requests` 拿到 `new_tokens` 与各张量，处理 finish_reasons、logprobs 的 D2H 拷贝，最后组装 `SampleStateTorch` 返回。注意 `sample_async` 本身「尽量异步」——真正的设备同步在 `update_requests` 里通过 `sampler_event.synchronize()` 完成。

**编排核心 `_process_requests`**：[sampler.py:5212-L5384](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/sampler/sampler.py#L5212-L5384)。这段就是 4.2.2 流程图的源码版。两段关键代码：

- 预处理三连——embedding bias、min length penalty、bad words：[sampler.py:5252-L5293](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/sampler/sampler.py#L5252-L5293)。它们都**就地改写 logits**，与 2.2 节心法一致。
- 快/慢路径分流：[sampler.py:5296-L5356](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/sampler/sampler.py#L5296-L5356)。`_can_use_fast_greedy_path` 为真走 argmax，否则走分组采样。

**贪心快路径**：`_can_use_fast_greedy_path` 要求「全部请求是贪心 且 无人要 logprobs」：[sampler.py:2606-L2620](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/sampler/sampler.py#L2606-L2620)。满足时调用 `_fast_greedy_sample_kernel`——它本质就是 `torch.argmax` + 可选 d2t 翻译 + scatter：[sampler.py:4223-L4247](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/sampler/sampler.py#L4223-L4247)。这是吞吐敏感场景（如大量贪心请求）的关键优化。

**min_tokens 惩罚**：`_apply_min_length_penalty` 把「未达最小长度处的 EOS logit」改成 `-inf`：[sampler.py:4741-L4800](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/sampler/sampler.py#L4741-L4800)。注意它用 `index_put_` 做批量置 `-inf`，且仅在确有请求需要时才把张量转 host（延迟转换，避免热路径开销）。

**慢路径：按策略分组采样** `_sample_batched_by_strategy`：[sampler.py:4395-L4654](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/sampler/sampler.py#L4395-L4654)。其核心循环遍历每个分组，对每组调 `FlashInferGroupedStrategySampler.sample_grouped_strategies`，再把结果 `copy_` 进批次输出缓冲；若有人要 processed/raw logprobs 或投机解码需要 target_probs，会额外保留 softmax 张量。

**第一层路由：策略解析** `resolve_sampling_strategy`：[sampling_utils.py:155-L206](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/sampler/sampling_utils.py#L155-L206)。它先复用 `SamplingParams.params_imply_greedy_decoding` 判贪心；否则按「缺省 `temperature=1.0`、`top_p=1.0`、`top_k=vocab_size`」补默认，再据 `need_top_k`/`need_top_p` 组合输出五种策略之一：

```python
# 伪代码（简化自 resolve_sampling_strategy）
if params_imply_greedy_decoding(...):   return ("greedy", None)
temperature = temperature or 1.0
if use_beam_search:                     return ("beam_search", bw_in, bw_out, temperature)
top_p = top_p or 1.0
top_k = top_k or vocab_size
need_top_k = top_k < vocab_size
need_top_p = top_p < 1 or top_p_decay_active(params)
if need_top_p and need_top_k:           return ("top_k_top_p", top_k, top_p, temperature)
if need_top_p:                          return ("top_p", top_p, temperature)
if need_top_k:                          return ("top_k", top_k, temperature)
return ("temperature", temperature)
```

**第二层路由：分组 + FlashInfer 内核** `FlashInferGroupedStrategySampler`：[sampling_utils.py:805-L903](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/sampler/sampling_utils.py#L805-L903)。`strategy_grouping_key`（[sampling_utils.py:811-L824](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/sampler/sampling_utils.py#L811-L824)）把 Strategy 映射成可分桶的 key（`beam_search` 还带上 beam 宽度）；`sample_grouped_strategies`（[sampling_utils.py:839-L903](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/sampler/sampling_utils.py#L839-L903)）按「是否需要返回概率」选 `*WithProbs` 或 `*SampleOnly` 实现类。真正的「除以温度 → softmax → 抽样」在 `_StrategyImpls` 里，例如 `_prepare_probs_with_temperature` 调 `softmax_op(logits, temperature)`：[sampling_utils.py:334-L341](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/sampler/sampling_utils.py#L334-L341)。这一步正是 2.1 节 softmax 公式的落点。

**用户自定义 logits processor 的执行点**不在 sampler.py，而在 `model_engine.py._execute_logit_post_processors`：[model_engine.py:6614-L6659](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/model_engine.py#L6614-L6659)。它在「最后一层 PP rank」上，对 context 请求只处理最后一步、对 generation 请求处理每个 beam，就地改写 `outputs["logits"]`，且**发生在采样之前**。`LogitsProcessor` 的接口契约定义在 [sampling_params.py:108-L137](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/sampling_params.py#L108-L137)。

#### 4.2.4 代码实践

**实践目标**：在 sampler.py / sampling_utils.py 中精确定位「采样算子调用」，并跟踪贪心快路径与分组慢路径的分流。

**操作步骤**（源码阅读型实践，无需 GPU）：

1. 打开 `tensorrt_llm/_torch/pyexecutor/sampler/sampler.py`，定位 `_process_requests`（约 5212 行），画出本讲 4.2.2 的流程图，标注每一步对应的行号。
2. 找到 `_can_use_fast_greedy_path`（约 2606 行），看清它要求「全贪心 + 不要 logprobs」。然后在 `_process_requests` 里找到调用 `_fast_greedy_sample_kernel` 的分支（约 5296 行）。
3. 跟进 `_fast_greedy_sample_kernel`（约 4223 行），确认采样算子是 `torch.argmax`。
4. 切到 `sampling_utils.py`，定位 `FlashInferGroupedStrategySampler.sample_grouped_strategies`（约 839 行），确认慢路径的采样算子是 FlashInfer 的 `sampling_from_probs_op` / `top_k_top_p_sampling_batch`（通过 `_StrategyImpls.*` 间接调用）。

**需要观察的现象**：
- 贪心路径**不经过** FlashInfer，只做 argmax + scatter，所以更快。
- 分组慢路径按 `strategy_grouping_key` 分桶；同桶请求共享一次内核调用。
- logits processor 与 guided decoding 都在采样**之前**、且代码在 `model_engine.py`，不在 `sampler.py`。

**预期结果**：你能用一句话说清「logits 在哪里被 argmax / 在哪里被 softmax+抽样」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_can_use_fast_greedy_path` 要额外要求「不要 logprobs」？走快路径的请求拿不到什么？
**答案**：快路径只做 argmax + scatter，跳过了 `_sample_batched_by_strategy` 里对 softmax 的收集，因此拿不出 processed/raw logprobs。需要 logprobs 的贪心请求只能走慢路径（`GreedyWithProbs` 实现）。

**练习 2**：一个 batch 里同时有贪心请求和 top-p 请求，会触发几次 FlashInfer 采样内核调用？
**答案**：两次。`_sample_batched_by_strategy` 会把它们按 `strategy_grouping_key` 分成「greedy」和「top_p」两个桶，每桶各一次 `sample_grouped_strategies`（贪心桶内部仍是 argmax 类的 `GreedyWithProbs/SampleOnly`）。

**练习 3**：用户想实现「重复惩罚（repetition penalty）」，但在 `SamplingParams.repetition_penalty` 上设值后却没生效，为什么？该怎么办？
**答案**：因为 `TorchSampler` 的策略解析只消费 `temperature`/`top_p`/`top_k`/`top_p_decay`，`repetition_penalty` 属于 C++ SamplingConfig 管线、不在 PyTorch 热路径里。正确做法是通过 `logits_processor` 传一个自定义回调，在采样前对已出现 token 的 logit 做惩罚（除以/乘以惩罚系数）。

---

### 4.3 Guided Decoding：用语法约束 logits

#### 4.3.1 概念说明

**Guided Decoding（引导解码）** 让你能强制模型输出符合某种结构——比如合法 JSON、某个 JSON Schema、一条正则、或一段 EBNF 语法。它的本质**不是一种采样策略，而是在采样之前对 logits 做位掩码（bitmask）**：

- 维护一个**语法匹配器（grammar matcher）**，它知道「按目前生成的前缀，下一个 token 哪些是合法的」。
- 每一步把「合法 token 集合」编码成一张位掩码（每个 bit 对应词表里的一个 token，1 = 合法、0 = 非法）。
- 用这张位掩码把 logits 中「非法 token」改成 `-inf`，于是采样器绝不可能采到它们。

TRT-LLM 支持两种匹配器后端：**XGrammar**（支持 structural_tag）与 **LLGuidance**。`GuidedDecodingParams`（[sampling_params.py:55-L78](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/sampling_params.py#L55-L78)）允许指定 `json` / `regex` / `grammar` / `json_object` / `structural_tag` 五选一（`_validate` 强制只能选一个）。

关键术语：
- **bitmask**：紧凑的合法 token 位图，大小为 `ceil(vocab_size / 32)` 个 int32。
- **grammar matcher**：有状态的语法自动机，每接受一个 token 就推进，并能给出「下一步合法 token 集合」。

#### 4.3.2 核心流程

Guided decoding 的单步执行（`GuidedDecoder.execute`）分四步：

```text
execute(logits)
  │
  1. build()         在 CPU 上推进语法匹配器、填好 bitmask_host
  │     ├─ require_matcher_init    (context 最后一块) → 新建 matcher
  │     └─ require_matcher_advance (generation)       → accept_token + fill_next_token_bitmask
  │
  2. copy_bitmask()  bitmask_host ──异步拷贝──► bitmask (GPU)
  │
  3. apply_bitmask() torch.ops.trtllm.logits_bitmask(logits, bitmask, ...)
  │                   把非法 token 的 logit 改成 -inf（就地）
  │
  4. 返回 failed_requests（语法处理出错的请求）
```

这里有两条「是否要推进 matcher」的判定，构成一个小的状态机（在 `GuidedRequest` 上）：

- `require_matcher_init`：仅当「是 context 阶段 且 是最后一块 chunk」时新建 matcher。
- `require_matcher_advance`：对主模型，仅当「generation 阶段」推进；对 draft 请求（投机解码草稿），在 context 末块或 generation 阶段都推进。

> 与投机解码协作：草稿请求也会经过 guided decoder，若草稿 token 被语法拒绝则提前终止该 draft；目标模型还会在拒绝后做 `rollback_rejected_tokens` 把 matcher 回滚到最后一个被接受的 token。与分离式服务协作：decode 节点的第一步（`is_generation_only_first_iteration`）需要重建 matcher（`init_disagg_gen_requests`）。

为了能在 **CUDA Graph** 捕获下工作，还有子类 `CapturableGuidedDecoder`：把「依赖 host 上 new_tokens 的逻辑」放进 CUDA callback（`@hostfunc`），用一个 `Queue` 在普通 host 代码与 callback 间传数据，从而可被图捕获。

#### 4.3.3 源码精读

**`GuidedDecodingParams`**：[sampling_params.py:55-L78](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/sampling_params.py#L55-L78)，五字段选其一；`_validate` 统计非空字段数，超过 1 即报错。它最终由 `SamplingParams._get_guided_decoding_params` 翻译成 C++ `tllme.GuidedDecodingParams`（带 `GuideType`）：[sampling_params.py:671-L700](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/sampling_params.py#L671-L700)。

**请求快照 `GuidedRequest`**：[guided_decoder.py:22-L83](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/guided_decoder.py#L22-L83)。它把 `LlmRequest` 里与引导解码相关的字段（含状态标志、new_token、draft_tokens）拍成一份「host 生产、device 消费」的快照。两条推进判定的注释和实现见 [guided_decoder.py:42-L60](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/guided_decoder.py#L42-L60)。

**`GuidedDecoder.__init__`**：[guided_decoder.py:144-L203](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/guided_decoder.py#L144-L203)。按后端建 `XGrammarMatcherFactory` 或 `LLGuidanceMatcherFactory`；预分配 GPU/CPU 的 bitmask 与 token_mask；并为每个 seq slot 维护一个 matcher 槽。`bitmask_size = ceil(vocab_size_padded / 32)`：[guided_decoder.py:205-L207](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/guided_decoder.py#L205-L207)。

**核心 `_build`**：[guided_decoder.py:209-L291](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/guided_decoder.py#L209-L291)。这段是状态机的实现：决定 init/advance，调 `matcher.accept_token`（失败时主模型报错、draft 则终止），再 `matcher.fill_next_token_bitmask` 把下一位置的合法位图填进 `bitmask_host`；draft token 还会逐个尝试 accept 以预填多位图。出错则收集进 `failed_requests`。

**位掩码施加 `_apply_bitmask`**：[guided_decoder.py:303-L336](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/guided_decoder.py#L303-L336)。它处理 TP 分片（每个 rank 只施加自己负责的那段词表位图），核心一句：

```python
torch.ops.trtllm.logits_bitmask(
    logits[:num_bitmask_tokens],
    self.bitmask[:num_bitmask_tokens, bitmask_start:bitmask_end],
    token_mask=self.token_mask[:num_bitmask_tokens],
    d2t=d2t,
)  # 就地：把位图为 0 的 token logit 改成 -inf
```

**单步编排 `execute`**：[guided_decoder.py:365-L379](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/guided_decoder.py#L365-L379)。它在一条单独的 CUDA stream 上 `build → copy_bitmask → record event`，再让主流等待 bitmask 就绪后 `apply_bitmask`，实现「CPU 语法计算」与「GPU 前向」的并行。

> 集成点：guided decoder 由 `model_engine.py` 在前向得到 logits 后、采样之前调用（`add_batch` 见 model_engine.py:3827 附近，`execute` 紧随其后）。这正解释了 2.3 节流程图里「guided decoding 位掩码 logits」那一格的位置。

#### 4.3.4 代码实践

**实践目标**：用 JSON Schema 约束输出，验证「非法 token 被 bitmask 挡掉」的效果。

**操作步骤**（需 GPU + trtllm-serve 或 LLM API）：

1. 用 `SamplingParams(guided_decoding=GuidedDecodingParams(json=<schema>))` 跑一段生成。
2. 多次运行，检查输出是否**始终**是合法 JSON、且字段类型符合 schema。

```python
# 示例代码
from tensorrt_llm import LLM, SamplingParams
from tensorrt_llm.sampling_params import GuidedDecodingParams

llm = LLM(model="nvidia/Llama-3.1-8B-Instruct-FP8")

schema = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age":  {"type": "integer", "minimum": 0},
    },
    "required": ["name", "age"],
}
sp = SamplingParams(
    max_tokens=64,
    temperature=0.7,
    guided_decoding=GuidedDecodingParams(json=schema),
)
out = llm.generate(["Give me a person profile as JSON."], sp)
print(out[0].outputs[0].text)
import json; print(json.loads(out[0].outputs[0].text))  # 应总能解析成功
```

**源码阅读补充**（无需 GPU）：在 `guided_decoder.py` 中定位 `_apply_bitmask`（约 303 行），确认「约束输出格式」是通过 `torch.ops.trtllm.logits_bitmask` 把非法 token 的 logit 改成 `-inf`——也就是说，**guided decoding 不改采样策略，只改 logits**，采样器仍按原 `temperature`/`top_p` 在「合法 token 子集」里正常抽样。

**需要观察的现象**：
- 即便 `temperature=0.7`，输出仍严格合法（不会出现畸形 JSON）。
- 在 `_build` 里若 matcher 接受 token 失败，主模型会抛 `ValueError`（见 [guided_decoder.py:253-L255](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/guided_decoder.py#L253-L255)）——说明 bitmask 是「硬约束」。

**预期结果**：输出总能被 `json.loads` 解析且满足 schema。如本机无法运行，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：guided decoding 和 `top_p` 采样能同时用吗？谁先生效？
**答案**：能。guided decoding 先在采样前用 bitmask 把非法 token 的 logit 改成 `-inf`，随后采样器在「剩余合法 token」上照常做 softmax + top_p 抽样。二者作用层级不同。

**练习 2**：为什么 `GuidedDecoder.execute` 要在单独的 CUDA stream 上做 `build` 与 `copy_bitmask`？
**答案**：`build` 是 CPU 上的语法自动机计算（耗时且不占 GPU），单独放一条 stream 并用 event 同步，可以让它与主流上的模型前向重叠，减少引导解码引入的额外延迟。

**练习 3**：`CapturableGuidedDecoder` 相比基类多了什么、为什么需要它？
**答案**：它把依赖「host 上的 new_tokens」的逻辑包进 `@hostfunc`（CUDA callback），并通过 `Queue` 在普通 host 代码与 callback 之间传递请求快照，从而能在 CUDA Graph 捕获/回放下工作（图捕获要求不能有动态的 host 端 Python 调用）。

---

## 5. 综合实践

把三个模块串起来：**给定同一提示，对比四种解码配置的输出差异，并能在源码里解释每一步发生在哪。**

1. 准备 4 组 `SamplingParams`：
   - A：贪心（全 None）
   - B：`temperature=1.0, top_k=8, top_p=0.5`
   - C：贪心 + `logits_processor`（自定义一个把某个 token id 打压的处理器）
   - D：`temperature=0.7` + `guided_decoding=GuidedDecodingParams(json_object=True)`
2. 用 `llm.generate([prompt]*4, [A,B,C,D])` 跑一次。
3. 对每种输出，写一段说明，指出「logits 经历了哪些预处理、最终走的是快路径还是慢路径、采样算子是什么」：
   - A → 快路径（argmax）。
   - B → 慢路径（top_k_top_p 桶，FlashInfer）。
   - C → 慢路径（贪心 + 需要 logits processor 处理，processor 在 `model_engine._execute_logit_post_processors` 生效）。
   - D → 慢路径（贪心/采样均可，但 logits 先被 guided decoder 的 bitmask 修剪）。
4. 在 `sampler.py` 的 `_process_requests` 里，用行号标注这四种配置分别命中哪些步骤、跳过哪些步骤。

> 这个任务把「参数表（4.1）— 采样器分流（4.2）— 约束施加（4.3）」三者连成一条链，帮助你建立「从一行用户配置到一次内核调用」的完整心智模型。

---

## 6. 本讲小结

- 采样是 PyExecutor 单步循环里「前向之后、回写之前」的一环：输入 logits，输出 `new_tokens`。
- `SamplingParams` 是「一套参数、三处生效」的翻译层，贪心判定集中在三个静态方法（`params_imply_greedy_decoding` 等），是全仓库的单一真相源。
- `Sampler` 体系按模型/后端分流，**默认 `TorchSampler`**（Python + FlashInfer）；`TRTLLMSampler` 已弃用；非生成模型用 `EarlyStopSampler`。
- `TorchSampler._process_requests` 的预处理链是：embedding bias → min length penalty → bad words，**全部就地改写 logits**；随后贪心走 argmax 快路径，否则按策略分组走 FlashInfer 慢路径。
- 策略解析分两层：`resolve_sampling_strategy` 把参数归约为 Strategy 元组，`FlashInferGroupedStrategySampler` 按 key 分桶后调 FlashInfer 内核（softmax + 抽样即落在此处）。
- **Guided Decoding 不是采样策略，而是采样前对 logits 做位掩码**（`torch.ops.trtllm.logits_bitmask`），由 `GuidedDecoder` 在独立 stream 上 build/copy/apply；logits processor 同样在采样前、由 `model_engine.py` 执行。

---

## 7. 下一步学习建议

- 想深入「草稿 token 如何与采样器交互」（`process_draft_tokens`、拒绝采样、`_process_draft_tokens_rejection_sampling`），请继续阅读 u10-l3（投机解码），它会展开 `TorchSampler` 里与 spec decoding 相关的方法。
- 想了解「stop 条件、finish_reason 如何在采样后写入请求并影响调度」，可回看 u3-l2 的 `_handle_responses` 与 u8-l2 的请求状态机。
- 想看「logprobs 的两种模式（RAW vs PROCESSED）如何在采样路径里分别收集」，可精读 `_sample_batched_by_strategy` 中对 `need_raw_logprobs` / `need_processed_logprobs` 的处理，以及 `_process_logprobs`。
- 若对 CUDA Graph 下的采样/约束感兴趣，可对照 `CapturableGuidedDecoder` 与 u10-l4（CUDA Graph 与 torch.compile）一起读。
