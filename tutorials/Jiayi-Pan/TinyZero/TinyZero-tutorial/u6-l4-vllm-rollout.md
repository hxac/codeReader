# vLLM Rollout 生成

## 1. 本讲目标

在 PPO/GRPO 训练循环里，每一步都要先用当前策略「生成一批回答」，再算奖励、算优势、更新参数。这一步「生成」在 verl 里就叫 **rollout**。本讲只讲一件事：veRL 是如何用 **vLLM** 这个推理引擎来完成 rollout 的。

学完本讲你应该能够：

1. 说清 vLLM 推理引擎与 FSDP 训练权重为何要解耦，二者如何交替工作。
2. 看懂 `_pre_process_inputs` 为什么、以及如何把「左填充的 prompt」还原成「无填充的 token 列表」交给 vLLM。
3. 读懂 `generate_sequences` 如何把 vLLM 的输出「右填充的回答」拼回完整序列，并重建 `attention_mask`（`get_eos_mask`）与 `position_ids`。
4. 区分 `rollout.n > 1` 多次采样（GRPO 用）与 `do_sample=False` 贪心验证两条不同的采样路径。
5. 解释 `free_cache_engine` 与 `enforce_eager` 之间那条硬性约束的含义。

本讲只讲 **vLLM 引擎内部** 的生成与张量重建；权重在训练态（FSDP）和推理态（vLLM）之间如何同步，属于下一讲 [u6-l5 FSDP↔vLLM 权重同步](u6-l5-fsdp-vllm-weight-sync.md) 的内容。

---

## 2. 前置知识

### 2.1 什么是 rollout

强化学习里，rollout 指的是「让策略去和环境交互、产出轨迹」这一步。在 LLM 的 RL 中，「环境」就是语言模型自己续写，「轨迹」就是一段生成的 token 序列。所以这里的 rollout ≈ 用当前模型生成一批回答。本讲的主角 `vLLMRollout` 就是专门干这件事的类。

### 2.2 为什么要用 vLLM

直接用 HuggingFace 的 `model.generate()` 也能生成，但速度慢、显存利用率低。vLLM 用了 **PagedAttention**、**连续批处理（continuous batching）** 等技术，能显著提升吞吐。PPO 每步都要生成几千上万条回答，rollout 往往是训练循环里最耗时的阶段，所以 verl 默认用 vLLM。

> verl 也保留了一个 `HFRollout` 作为慢速后备，配置项 `actor_rollout_ref.rollout.name` 可在 `vllm` 与 `hf` 之间切换，TinyZero 默认是 `vllm`。

### 2.3 左填充 vs 右填充

这是贯穿本讲的关键概念，请务必记住：

| 方向 | 形式 | 用在哪 |
|------|------|--------|
| **左填充（left pad）** | `[pad, pad, 真, 实, token]` | prompt 一侧。因为生成是从序列**右端**追加 token 的，prompt 内容必须贴在最右 |
| **右填充（right pad）** | `[真, 实, token, pad, pad]` | response 一侧。回答长短不一，短的要补齐到固定长度 |

veRL 的数据加载器（见 [u2-l3 RLHFDataset](u2-l3-rlhf-dataset-loading.md)）吐出的 prompt 是**左填充**的。但 vLLM 的输入不希望带这些无意义的左 pad，所以进 vLLM 前要先把左填充去掉；生成完的回答则要**右填充**对齐，再拼回 prompt。

### 2.4 承接的前置认知

- **DataProto**（[u3-l1](u3-l1-dataproto-protocol.md)）：`generate_sequences` 的输入输出都是 `DataProto`，其 `batch` 是 TensorDict 张量列、`meta_info` 是全局元信息。
- **混合引擎**（[u6-l1](u6-l1-hybrid-actor-rollout-ref-worker.md)）：`vLLMRollout` 并不独立存在，它由 `ActorRolloutRefWorker._build_rollout` 创建，和 actor 共享同一份 FSDP 权重。
- **`attention_mask` / `position_ids`**（[u2-l3](u2-l3-rlhf-dataset-loading.md)）：`compute_position_id_with_mask` 用 `clip(cumsum(mask)-1, min=0)` 由 mask 推出位置编号，本讲会用到同样的「位置连续递增」思想。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [verl/workers/rollout/vllm_rollout/vllm_rollout.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/vllm_rollout/vllm_rollout.py) | 本讲主角。定义 `vLLMRollout`，含 `_pre_process_inputs`、`__init__`、`generate_sequences` |
| [verl/workers/rollout/base.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/base.py) | 抽象基类 `BaseRollout`，规定 `generate_sequences(prompts) -> DataProto` 的接口契约 |
| [verl/utils/torch_functional.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py) | 工具函数 `get_eos_mask`、`pad_sequence_to_length`，本讲张量重建的关键 |
| [verl/workers/fsdp_workers.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py) | 调用方：`_build_rollout` 创建 `vLLMRollout`，`generate_sequences` 包裹它并重算 `old_log_probs` |
| [verl/trainer/config/ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml) | `actor_rollout_ref.rollout` 分组，本讲涉及的所有配置项的默认值 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **vLLMRollout 的定位与引擎初始化**（`__init__`）
2. **左填充去除**（`_pre_process_inputs`）
3. **生成主流程与张量重建**（`generate_sequences` + `get_eos_mask` / `pad_sequence_to_length`）
4. **多次采样 n>1 与贪心验证 do_sample=False**

### 4.1 vLLMRollout 的定位与引擎初始化

#### 4.1.1 概念说明

`vLLMRollout` 解决的核心问题是：**训练态的模型（FSDP 分布式包裹）和推理态的模型（vLLM 引擎）是两套不同的权重表示**，不能直接互相喂数据。

- FSDP 把权重**切片**分布在多卡上，每个 rank 只持有部分参数，反传梯度用。
- vLLM 为了加速推理，会把权重按 **tensor parallel** 重新组织，并维护一套 KV cache、CUDA graph 等推理专用结构。

所以二者的协作方式是「**交替占用、显存让位**」：

1. 训练时，FSDP 权重驻留 GPU，vLLM 权重被 **offload**（卸到 CPU/释放）让出显存。
2. 要生成时，进入 sharding manager，把 FSDP 权重**同步**到 vLLM（详见 u6-l5），vLLM 重建 cache engine、做推理。
3. 推理完，再次 offload vLLM、把显存还给训练。

`vLLMRollout.__init__` 就是构建这个「推理引擎 + 采样参数」的对象，并完成第一次 offload。

#### 4.1.2 核心流程

```
__init__(actor_module, config, tokenizer, model_hf_config):
  1. 断言 enforce_eager / free_cache_engine 约束
  2. 取 tensor_parallel_size，断言不超过 world_size
  3. （Megatron 后端才走）初始化 vllm parallel state
  4. 断言模型上下文长度 >= prompt_length + response_length
  5. 用 verl 第三方封装的 LLM 类创建推理引擎 self.inference_engine
  6. 立刻 offload_model_weights()（降低峰值显存）
  7. 用 config 里的采样字段构造 SamplingParams
  8. 记录 pad_token_id
```

#### 4.1.3 源码精读

类定义与构造签名，注意它继承自 `BaseRollout`：

[verl/workers/rollout/vllm_rollout/vllm_rollout.py:57-59](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/vllm_rollout/vllm_rollout.py#L57-L59) — `class vLLMRollout(BaseRollout)`，入参 `actor_module` 是 HuggingFace API 风格的模型（FSDP 包裹后的内层模块），`config` 是 `actor_rollout_ref.rollout` 这一段配置。

本讲最重要的一条**约束**：

[verl/workers/rollout/vllm_rollout/vllm_rollout.py:71-72](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/vllm_rollout/vllm_rollout.py#L71-L72) — `assert not (not config.enforce_eager and config.free_cache_engine)`。翻译成人话：**如果你想开启 `free_cache_engine`（每轮生成后释放 KV cache），就必须先开启 `enforce_eager`（关闭 vLLM 的 CUDA graph）**。

原因：CUDA graph 会预先把推理计算图「录制」下来，其中固化了对 KV cache 显存地址的引用；如果之后把 cache engine 释放重建，CUDA graph 里那些地址就失效了，再推理会崩。所以「动态释放/重建 cache」与「静态录制 CUDA graph」二者只能选其一。TinyZero 默认两个都开（`enforce_eager: True`、`free_cache_engine: True`），即选了「牺牲一点速度换显存灵活性」。

接着是上下文长度断言与推理引擎创建：

[verl/workers/rollout/vllm_rollout/vllm_rollout.py:89-100](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/vllm_rollout/vllm_rollout.py#L89-L100) — 断言模型支持的最大位置 `max_position_embeddings` 必须能容纳 `prompt_length + response_length`（否则整条序列放不下）；然后用 verl 自己在 `verl/third_party/vllm` 下打过补丁的 `LLM` 类构造引擎，关键参数有 `tensor_parallel_size`、`gpu_memory_utilization`、`max_model_len`（也设为 prompt+response 之和）、`load_format`。

> 注意这里的 `LLM` 不是直接从 vllm 官方 import 的，而是 `from verl.third_party.vllm import LLM`。verl 给它加了 `sync_model_weights`、`offload_model_weights`、`init_cache_engine`、`free_cache_engine` 等方法，正是这些「补丁方法」支撑了训练↔推理的交替。这是 verl 解耦设计的关键一环。

构造完引擎立刻 offload，给训练让显存：

[verl/workers/rollout/vllm_rollout/vllm_rollout.py:102-103](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/vllm_rollout.py#L102-L103) — 构造结束后立即 `self.inference_engine.offload_model_weights()`，把刚载入的 vLLM 权重卸走，避免它在训练阶段白白占显存。

最后是采样参数的构造，这里有一个**自动透传**的小技巧：

[verl/workers/rollout/vllm_rollout/vllm_rollout.py:105-123](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/vllm_rollout/vllm_rollout.py#L105-L123) — 先放好 `n=1`、`logprobs=1`、`max_tokens=response_length` 三个基底，然后遍历 config 的所有键，**只要该键是 vllm `SamplingParams` 的合法属性，就把它拷进 kwargs**。这意味着你在 yaml 的 `rollout` 段下新增任何 vllm 支持的采样参数（如 `temperature`、`top_p`、`top_k`、`ignore_eos`、`n`），都会自动生效，无需改代码。最终 `self.sampling_params = SamplingParams(**kwargs)`。

#### 4.1.4 代码实践

**实践目标**：搞清 `ppo_trainer.yaml` 里 rollout 段都透传了哪些采样参数。

1. 打开 [ppo_trainer.yaml 的 rollout 段（第 61-84 行）](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L61-L84)。
2. 逐项判断：哪些会进入 `__init__` 的 `kwargs`（即属于 `SamplingParams` 属性，如 `temperature`、`top_p`、`top_k`、`n`、`ignore_eos`），哪些只是引擎/显存参数（如 `enforce_eager`、`gpu_memory_utilization`、`load_format`，它们在 `LLM(...)` 构造时被消费，不进 SamplingParams）。
3. **需要观察的现象**：训练启动时，stdout 会打印一行 `kwargs: {...}`（来自第 120 行的 `print(f"kwargs: {kwargs}")`）。核对你列出的「应进 kwargs 的项」是否都出现在这行里。
4. **预期结果**：`kwargs` 里至少包含 `n`、`logprobs`、`max_tokens`、`temperature`、`top_p`、`top_k`、`ignore_eos` 等键；而 `enforce_eager`、`gpu_memory_utilization`、`load_format`、`free_cache_engine` 不会出现。
5. 本实践为纯源码阅读 + 观察，**待本地验证**（需实际启动一次训练才能看到那行 print）。

#### 4.1.5 小练习与答案

**练习 1**：如果想让 vLLM 用上 CUDA graph 加速（即 `enforce_eager=False`），`free_cache_engine` 能不能继续设为 `True`？为什么？

> **答案**：不能。源码第 71-72 行的断言会直接报错。因为 CUDA graph 录制时固化了 KV cache 的显存地址，而 `free_cache_engine=True` 会在每轮生成后释放并重建 cache，导致地址失效。要关 `enforce_eager` 就必须同时关 `free_cache_engine`。

**练习 2**：为什么构造完 `inference_engine` 后要立刻调用一次 `offload_model_weights()`？

> **答案**：`init_model` 阶段 actor 的 FSDP 权重也要占显存，如果不马上 offload，vLLM 权重会与 FSDP 权重叠加导致峰值显存过高甚至 OOM。offload 后显存只留给训练态，等真正要生成时再通过 sharding manager 同步回来。

---

### 4.2 左填充去除：_pre_process_inputs

#### 4.2.1 概念说明

数据加载器吐出的 `input_ids` 是一个**左填充**的张量（见 u2-l3），形如 `[pad, pad, pad, t1, t2, t3]`，且整个 batch 被堆叠成 `[bs, prompt_length]` 的矩形。

但 vLLM 的 `generate` 接口接受的是 **`List[List[int]]`**——每个样本一个**不带任何填充**的 token id 列表，长度可变。原因有二：

1. vLLM 内部用连续批处理 + PagedAttention，自己会做调度和填充，不希望外部预填；
2. 左填充的 pad token 进了模型会污染注意力（尤其没有正确 attention_mask 时），不如直接剥掉。

所以需要一个函数把左填充剥掉，还原出真实 prompt。这就是 `_pre_process_inputs`。

#### 4.2.2 核心流程

```
_pre_process_inputs(pad_token_id, prompt_token_ids):  # prompt_token_ids 是一条 [prompt_length] 的一维张量
  1. 找到第一个不等于 pad_token_id 的位置 non_pad_index
  2. 从该位置切片到末尾 -> 真实 token
  3. 转成 python list 返回
```

#### 4.2.3 源码精读

[verl/workers/rollout/vllm_rollout/vllm_rollout.py:48-54](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/vllm_rollout/vllm_rollout.py#L48-L54) — 注意源码注释里那句 `# NOTE(sgm): add for verl. We can optimize it by making the dataloader yield List[int] without padding.`——作者自己承认这步是「为了适配带填充的 dataloader 而打的补丁」，理想情况下数据加载器直接吐无填充列表就不需要这步了。

核心两行：

```python
non_pad_index = torch.nonzero(prompt_token_ids != pad_token_id, as_tuple=False)[0][0]
token_ids = prompt_token_ids[non_pad_index:].tolist()
```

- `prompt_token_ids != pad_token_id` 得到一个布尔张量 `[F, F, F, T, T, T]`（左填充）。
- `torch.nonzero(...)[0][0]` 取第一个 `True` 的下标，即真实内容的起点。
- 从起点切片到末尾，就是去填充后的真实 prompt，再 `.tolist()` 转成 python 列表交给 vLLM。

> 边界情况：如果一条 prompt **没有任何 pad**（`non_pad_index == 0`），切片就是整条，逻辑依然成立。这也意味着这个函数对「无填充」输入是安全的。

#### 4.2.4 代码实践

**实践目标**：用一个一维张量模拟左填充 prompt，手工验证 `_pre_process_inputs` 的输出。

1. 准备（**示例代码**，可在任意装了 torch 的环境运行，不依赖 vLLM）：

   ```python
   import torch
   pad_token_id = 0
   # 模拟一条左填充 prompt：3 个 pad + 3 个真实 token
   prompt = torch.tensor([0, 0, 0, 5, 6, 7])
   non_pad_index = torch.nonzero(prompt != pad_token_id, as_tuple=False)[0][0]
   token_ids = prompt[non_pad_index:].tolist()
   print(token_ids)        # 期望 [5, 6, 7]
   print(non_pad_index)    # 期望 3
   ```
2. **需要观察的现象**：`token_ids` 应当恰好是去填充后的真实序列，长度等于真实 token 数。
3. **预期结果**：输出 `[5, 6, 7]`，`non_pad_index` 输出 `3`。
4. 再试一个无填充输入 `torch.tensor([5, 6, 7])`，验证 `non_pad_index == 0`、`token_ids == [5, 6, 7]`。
5. 本实践可直接运行验证（不需要 GPU/训练）。

#### 4.2.5 小练习与答案

**练习 1**：如果把这段代码改成右填充的 prompt（`[5, 6, 7, 0, 0, 0]`）会怎样？

> **答案**：`non_pad_index` 会是 0（第一个 token 就不是 pad），切片得到整条 `[5, 6, 7, 0, 0, 0]`，**尾部的 pad 没被去掉**，会污染生成。这正说明该函数**只对左填充有效**，也是为什么 veRL 全链路坚持 prompt 左填充的原因。

**练习 2**：为什么用 `torch.nonzero(...)[0][0]` 找起点，而不是直接 `int((prompt != pad_token_id).nonzero()[0])`？两者等价吗？

> **答案**：本质都是找第一个非 pad 的下标，`as_tuple=False` 返回形状 `[N, 1]`，所以 `[0][0]` 取第一个元素。写法是防御性的，等价。若整条全是 pad（极端错误输入），`nonzero` 为空会抛 IndexError，相当于一种隐式断言。

---

### 4.3 生成主流程 generate_sequences 与张量重建

这是本讲最核心的模块。`generate_sequences` 把上面两步串起来，并完成最难的部分——把 vLLM 输出拼回完整序列、重建 `attention_mask` 与 `position_ids`。

#### 4.3.1 概念说明

vLLM 的 `generate` 返回的是**变长的回答**：每条回答可能早早遇到 eos 就停了（短），也可能一直生成到 `max_tokens`（长）。但训练需要**定长张量**。

于是 `generate_sequences` 要做三件事：

1. **去左填充 + 调用 vLLM 生成**，拿到 response token ids 和 logprobs。
2. **右填充对齐**：把变长的 response 用 pad 补齐到 `response_length`。
3. **重建 mask 和 position**：因为 prompt 左填充、response 右填充的混合形态很特殊，必须重新算 `attention_mask`（用 `get_eos_mask` 在 eos 处截断）和 `position_ids`（在 prompt 末位之上继续递增）。

最后把 prompt 和 response 拼成完整序列 `seq = [prompt, response]`，连同重建的 mask/position 打包成 `DataProto` 返回。

#### 4.3.2 核心流程

```
generate_sequences(prompts):               # prompts: DataProto，batch 含左填充的 input_ids/attention_mask/position_ids
  1. (可选) 重建 vLLM cache engine
  2. 取出 idx(input_ids)、attention_mask、position_ids、eos_token_id
  3. 对 batch 每条样本调 _pre_process_inputs 去左填充 -> idx_list
  4. 按 do_sample 决定是否覆盖采样参数（贪心 vs 采样）
  5. self.inference_engine.generate(prompt_token_ids=idx_list, sampling_params)
       -> output = (response_token_ids, logprobs)，两者都已被 vLLM 内部右填充对齐到 batch 内最长
  6. 若 response 不足 response_length，用 pad_sequence_to_length 右填充补齐
  7. (n>1 且采样时) 把 prompt 侧张量 repeat_interleave(n)，对齐 n 条回答
  8. seq = cat([idx, response])                # 拼成完整序列
  9. 重建 position_ids：在 prompt 末位之上 + arange(1, response_length+1)
 10. 重建 attention_mask：prompt 段沿用原 mask，response 段用 get_eos_mask
 11. 打包成 TensorDict，(可选) 释放 cache engine，返回 DataProto
```

#### 4.3.3 源码精读

整个方法用 `@torch.no_grad()` 装饰，因为生成阶段不需要梯度：

[verl/workers/rollout/vllm_rollout/vllm_rollout.py:141-160](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/vllm_rollout/vllm_rollout.py#L141-L160) — 开头处理 `free_cache_engine`（若开启则 `init_cache_engine()` 重建 KV cache，因为上一轮结束时释放掉了）；取出 `input_ids`、`attention_mask`、`position_ids` 三个张量以及 `eos_token_id`；然后用一个循环把每条 prompt 去左填充得到 `idx_list`。

接下来调用 vLLM 生成。注意 `prompts=None`，因为我们已经把 prompt 转成了 token id 列表：

[verl/workers/rollout/vllm_rollout/vllm_rollout.py:174-184](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/vllm_rollout/vllm_rollout.py#L174-L184) — `output = self.inference_engine.generate(prompts=None, sampling_params=..., prompt_token_ids=idx_list)`；`output[0]` 是 response token ids、`output[1]` 是对应的 logprobs。这里的 `output` 实际是 verl 补丁版 `LLM._post_process_outputs` 返回的 `(output_token_ids, logprobs)` 二元组——它已用 `pad_sequence` 把 batch 内的回答**右填充对齐到最长**。

> **重要细节**：`log_probs = output[1]` 虽然被取出来了，但在本函数里**并没有被放进返回的 batch**（见第 216 行 `'old_log_probs'` 那行是被注释掉的）。原因是 vLLM 推理的前向数值与 FSDP actor 的前向数值**不完全一致**，而 PPO 的 importance ratio 要求更新起点 ratio≈1，所以调用方（`fsdp_workers.ActorRolloutRefWorker.generate_sequences`）会**用 actor 自己的前向重新算一遍 `old_log_probs`**。这点在 [u6-l1](u6-l1-hybrid-actor-rollout-ref-worker.md) 已详细说明，这里只需记住「vLLM 给的 logprob 不直接用」。

若 response 不足 `response_length`，补齐：

[verl/workers/rollout/vllm_rollout/vllm_rollout.py:186-188](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/vllm_rollout/vllm_rollout.py#L186-L188) — 用 `pad_sequence_to_length` 把 response（和 logprobs）右填充到固定 `response_length`。

[verl/utils/torch_functional.py:209-219](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L209-L219) — `pad_sequence_to_length` 的实现：默认 `left_pad=False` 即右填充，用 `F.pad` 在末尾补 `pad_token_id` 到 `max_seq_len`；若已超长则原样返回。

接下来是张量重建的**重头戏**——`position_ids` 与 `attention_mask`。先看作者留在源码里的示意图，它极其重要：

[verl/workers/rollout/vllm_rollout/vllm_rollout.py:201-208](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/vllm_rollout/vllm_rollout.py#L201-L208) — 注释直接画出了「左填充 prompt + 右填充 response」的拼接结果：

```
# prompt: left pad + response: right pad
# attention_mask: [0,0,0,0,1,1,1,1, | 1,1,1,0,0,0,0,0]
# position_ids:   [0,0,0,0,0,1,2,3, | 4,5,6,7,8,9,10,11]
```

左侧 `|` 以左是 prompt 段（左填充：前 4 位 pad），以右是 response 段（右填充：前 3 位有效、eos 后补 pad）。

**position_ids 重建**（第 197-206 行）：

```python
response_length = response.size(1)
delta_position_id = torch.arange(1, response_length + 1, device=...)   # [1,2,...,response_length]
delta_position_id = delta_position_id.unsqueeze(0).repeat(batch_size, 1)
response_position_ids = position_ids[:, -1:] + delta_position_id       # 在 prompt 末位上继续 +
position_ids = torch.cat([position_ids, response_position_ids], dim=-1)
```

关键在 `position_ids[:, -1:]`：取 prompt 段**最后一个位置编号**（即 prompt 真实内容的最大位置，例如示意图里的 3），再叠加 `[1,2,...,response_length]`，得到 response 段从 `4` 开始的连续编号。这样整条序列的位置编号在 prompt→response 交界处**严格连续递增**，没有跳跃。

**attention_mask 重建**（第 207-208 行）：

```python
response_attention_mask = get_eos_mask(response_id=response, eos_token=eos_token_id, dtype=attention_mask.dtype)
attention_mask = torch.cat((attention_mask, response_attention_mask), dim=-1)
```

response 段的 mask 不是「全 1」，而是用 `get_eos_mask` 在**第一个 eos token 处截断**：eos 及其之前为 1，eos 之后为 0（包括右填充的 pad）。

[verl/utils/torch_functional.py:139-148](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L139-L148) — `get_eos_mask` 的实现。它的核心是一个 `cumsum` 技巧，文档注释给了一个完美示例：

```
eos_token=1
response_id: [0, 0, 2, 42, 3, 5, 1, 0, 0]
eos_mask:     [1, 1, 1, 1,  1, 1, 1, 0, 0]
```

即「保留 eos 及其之前的所有 token，eos 之后（含尾部的 pad）全部置 0」。原理用数学表达：令 \(m_i = \mathbb{1}[r_i = \text{eos}]\)（第 i 位是否为 eos），则

\[
\text{mask}_i = \mathbb{1}\!\left[\sum_{j \le i} m_j - m_i = 0\right]
\]

即「到当前位为止还没出现过 eos（不含当前位是否为 eos）」时为 1。源码里 `(torch.cumsum(eos_mask, dim=1) - eos_mask).bool()` 就是 \(\sum_{j \le i} m_j - m_i\)，再 `logical_not` 取反即得上式。

> **为什么 response mask 要截断到 eos？** 因为回答一旦遇到 eos 就结束了，后面的 token 都是 pad/无意义，不应参与损失和优势计算。把它们的 mask 置 0，下游的 `masked_mean`、`apply_kl_penalty` 就会自动忽略这些位置。

最后把所有张量打包进 TensorDict 返回：

[verl/workers/rollout/vllm_rollout/vllm_rollout.py:211-226](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/vllm_rollout/vllm_rollout.py#L211-L226) — 返回的 batch 同时保留 `prompts`、`responses`、`input_ids`（此时已是 prompt+response 完整序列）、`attention_mask`、`position_ids`；若 `free_cache_engine` 开启则调用 `free_cache_engine()` 释放本轮 KV cache。

#### 4.3.4 代码实践

**实践目标**：亲手画出 `attention_mask` 与 `position_ids` 在「左填充 prompt + 右填充 response」下的拼接示意，验证对源码的理解。

1. **设定一个具体例子**（**示例代码**，可在本地用 torch 运行，无需 GPU/训练）：
   - prompt 段长度 = 8，真实 prompt 只有 4 个 token，左填充 4 个 pad：
     - `input_ids_prompt = [PAD,PAD,PAD,PAD, t1,t2,t3,t4]`
     - `attention_mask_prompt = [0,0,0,0, 1,1,1,1]`
     - `position_ids_prompt = [0,0,0,0, 0,1,2,3]`（真实 token 从位置 0 递增，pad 位为 0）
   - response 段长度 = 5，真实回答 3 个 token 后遇到 eos，再右填充 1 个 pad：
     - `response = [r1, r2, EOS, PAD, PAD]`（设 `eos_token_id = EOS`）
2. **手动套用源码公式**：
   - `delta_position_id = [1,2,3,4,5]`
   - `response_position_ids = position_ids_prompt[-1:] + delta = [3] + [1,2,3,4,5] = [4,5,6,7,8]`
   - 拼接 `position_ids = [0,0,0,0, 0,1,2,3, | 4,5,6,7,8]`
   - `get_eos_mask(response, eos)`：eos 在第 3 位（下标 2），得 `[1,1,1,0,0]`
   - 拼接 `attention_mask = [0,0,0,0, 1,1,1,1, | 1,1,1,0,0]`
3. **需要观察的现象**：position_ids 在 prompt→response 交界处（3→4）连续；attention_mask 的 response 段在 eos 之后变 0。
4. **预期结果**：与上面手算一致。把它写成一张示意图（对齐源码第 203-204 行注释的格式）。
5. 对照源码第 201-208 行注释，确认你画的图与作者画的一致。本实践可直接运行验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `response_position_ids` 要用 `position_ids[:, -1:]`（prompt 末位）作为基数，而不是直接用 `arange(0, response_length)`？

> **答案**：因为 prompt 的真实长度因样本而异（左填充量不同），其末位编号也不同（如示例里是 3）。response 必须从 prompt 的**实际末尾**继续编号，才能保证位置编号在整条序列上连续无跳跃。直接 `arange(0, ...)` 会让位置编号从 0 重新开始，等于把 prompt 和 response 当成两条独立序列，破坏了相对位置信息。

**练习 2**：若一条回答没生成出任何 eos（一直生成到 `max_tokens`），`get_eos_mask` 会返回什么？

> **答案**：`response_id` 中没有等于 eos 的位置，`cumsum` 全为 0，`logical_not` 后全为 1，即整段 response 都有效（mask 全 1）。这是符合预期的：没遇到 eos 说明模型一直有话可说，整段都应计入训练。

**练习 3**：返回的 `log_probs`（`output[1]`）在本函数里被用了吗？为什么？

> **答案**：没被使用。它会被调用方用 FSDP actor 的前向重新算一遍 `old_log_probs`（见 fsdp_workers.py 第 425-436 行），因为 vLLM 与 FSDP 前向数值不完全一致，而 PPO 的 importance ratio 要求 ratio 初始≈1，必须用与训练同一个 actor 的前向来算。

---

### 4.4 多次采样 n>1 与贪心验证 do_sample=False

#### 4.4.1 概念说明

`generate_sequences` 要服务两种截然不同的调用场景：

1. **训练时采样**：每个 prompt 采样 `n` 条回答（GRPO 必需 `n>1`，PPO 通常 `n=1`），用 `temperature>0` 的随机采样保证多样性。
2. **验证时贪心**：评估模型真实能力时，要关闭随机性（`temperature=0`、`top_p=1`、`n=1`），让模型给出确定性「最优」回答，便于跨 step 比较。

这两种场景由两个开关控制：`rollout.n`（采样条数）与 `meta_info['do_sample']`（是否随机采样）。`do_sample=False` 即「贪心验证」。

#### 4.4.2 核心流程

```
do_sample = prompts.meta_info.get('do_sample', True)   # 默认采样
if not do_sample:                                       # 验证/贪心分支
    覆盖 sampling_params: temperature=0, top_p=1, top_k=-1, n=1, best_of=1 ...
with update_sampling_params(**kwargs):                  # 临时改采样参数，用完自动还原
    output = inference_engine.generate(...)
if self.config.n > 1 and do_sample:                     # 仅训练采样时才扩展 batch
    idx / attention_mask / position_ids 各 repeat_interleave(n)
```

#### 4.4.3 源码精读

贪心分支的参数覆盖：

[verl/workers/rollout/vllm_rollout/vllm_rollout.py:162-171](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/vllm_rollout/vllm_rollout.py#L162-L171) — `do_sample=False` 时，强制 `temperature=0`（贪心解码，每步选概率最高的 token）、`top_p=1.0`、`top_k=-1`（关掉 top-k/top-p 截断）、`n=1`（贪心只生成 1 条）、`best_of=1`。这一组覆盖确保输出确定。

> 注意 `do_sample` 来自 `prompts.meta_info`，**不是** yaml 里的 `rollout.do_sample` 配置项。调用方（`RayPPOTrainer._validate`）会在验证时往 `meta_info` 写 `do_sample=False`，训练时则默认 `True`。yaml 里那个 `do_sample: True`（第 82 行）实际是个未被本函数读取的「文档性默认值」。这是源码里容易踩坑的一点。

临时改采样参数用的是 `update_sampling_params` 上下文管理器：

[verl/workers/rollout/vllm_rollout/vllm_rollout.py:125-139](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/vllm_rollout/vllm_rollout.py#L125-L139) — 进入时把要改的属性暂存旧值并 `setattr` 新值，`yield` 之后（退出 `with`）再把旧值写回。这样 `generate_sequences` 里临时改成贪心参数，调用结束后 `self.sampling_params` 自动恢复成训练用的采样参数，**不会污染下一次调用**。

`n>1` 时扩展 prompt 侧张量：

[verl/workers/rollout/vllm_rollout/vllm_rollout.py:190-195](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/vllm_rollout/vllm_rollout.py#L190-L195) — 当 `self.config.n > 1` 且 `do_sample=True`（训练采样）时，vLLM 对每个 prompt 实际产出了 `n` 条回答（因为 `sampling_params.n`），输出张量的第 0 维变成 `bs * n`。为了让 prompt 侧张量与 response 侧对齐，用 `repeat_interleave(self.config.n, dim=0)` 把每个 prompt **连续复制 n 份**。

> **与 GRPO 的连接**：`repeat_interleave`（连续复制）而不是 `repeat`（块复制），正是为了让「同一 prompt 的 n 条回答在 batch 里连续排列」，这与 [u5-l5 GRPO](u5-l5-grpo-algorithm.md) 里「按 uid 分组做组内归一化」、以及 `DataProto.repeat(interleave=True)` 的对齐方式完全一致。`n>1` 是 GRPO 的入口开关。

注意这里有个**容易忽略的细节**：贪心验证分支虽然把 `n` 设成了 1，但 `repeat_interleave` 的条件是 `self.config.n > 1 and do_sample`，贪心时 `do_sample=False`，所以即使配置里 `n>1`，验证时也不会扩展，每个 prompt 只出 1 条回答。

#### 4.4.4 代码实践

**实践目标**：理清 `n>1` 训练采样 vs `do_sample=False` 贪心验证两条路径的张量形状变化。

1. 阅读 [generate_sequences 第 162-195 行](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/vllm_rollout/vllm_rollout.py#L162-L195)。
2. 假设 `batch_size=4`、`rollout.n=8`，填下表（**待本地验证**形状推断）：

   | 场景 | `do_sample` | vLLM 输出 response 第 0 维 | `idx` repeat_interleave 后第 0 维 | 最终 `seq` 第 0 维 |
   |------|-------------|------------------------------|-----------------------------------|--------------------|
   | 训练采样 | True | ?（应 4×8=32） | ?（应 32） | ?（应 32） |
   | 贪心验证 | False | ?（应 4×1=4） | ?（不扩展，仍 4） | ?（应 4） |
3. **需要观察的现象**：训练采样时 batch 膨胀 n 倍；贪心验证时 batch 不变。
4. **预期结果**：见括号内答案。
5. 再回答：若 `rollout.n=1`（PPO 默认），训练采样的 batch 第 0 维是多少？（应仍为 4，不膨胀）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `repeat_interleave` 要放在「`if response不足则 pad`」之后、`seq = cat([idx, response])` 之前？

> **答案**：因为 vLLM 输出的 response 已经是 `bs*n` 行（n>1 时），而 `idx`（prompt）此时还是 `bs` 行，二者第 0 维不一致无法直接 `cat`。必须先对 `idx`、`attention_mask`、`position_ids` 都 `repeat_interleave(n)` 把它们扩成 `bs*n` 行，三者才能与 response 在第 0 维对齐后拼接。

**练习 2**：`update_sampling_params` 为什么必须实现成「退出时还原」的上下文管理器，而不能直接 `self.sampling_params.temperature = 0`？

> **答案**：因为 `self.sampling_params` 是实例长期持有的对象，下一次 `generate_sequences` 调用还会复用它。如果不还原，贪心验证时设的 `temperature=0` 会泄漏到下一次训练采样里，导致训练也变成贪心。用 `with` 保证「临时修改、用完即还原」。

**练习 3**：启用 GRPO 至少要把哪个配置项改大？它在本函数里如何生效？

> **答案**：`actor_rollout_ref.rollout.n`（设为 >1）。它在第 190 行 `if self.config.n > 1 and do_sample` 处生效，使每个 prompt 采样 n 条并 `repeat_interleave` 对齐，供下游 GRPO 做组内归一化。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，当一次「rollout 模拟器」，手工跑通一条样本从输入到输出的全过程，并产出一份张量对齐示意图。

**背景设定**（**示例代码**，可在本地 torch 环境运行，不依赖 vLLM/GPU）：

- `pad_token_id = 0`，`eos_token_id = 2`
- prompt 段长度 6，真实 prompt 3 个 token（`[10, 11, 12]`），左填充 3 个 pad
- response 段长度 4，真实回答 `[20, 21, 2]`（第 3 个是 eos），右填充 1 个 pad

**操作步骤**：

1. 写出 `input_ids`（prompt）、`attention_mask`、`position_ids` 三个 prompt 张量（左填充形态）。
2. 模拟 `_pre_process_inputs`：算出去填充后的 prompt token 列表。
3. 模拟 vLLM 生成 + `pad_sequence_to_length`：写出右填充到 response_length 的 response 张量。
4. 模拟 `get_eos_mask`：写出 response 段的 attention_mask（在 eos 处截断）。
5. 模拟 position_ids 重建：用 `position_ids_prompt[-1:] + arange(1, response_length+1)` 算出 response 段位置编号。
6. 拼成完整 `seq`、`attention_mask`、`position_ids`，画成三行对齐示意图。
7. **进阶**：把 `rollout.n=2`、`do_sample=True` 也加进来，写出 `repeat_interleave(2)` 后 prompt 侧和 response 侧第 0 维的大小。

**参考答案**（第 1-6 步）：

```
input_ids(prompt) = [ 0, 0, 0,10,11,12]
attention_mask   = [ 0, 0, 0, 1, 1, 1]
position_ids     = [ 0, 0, 0, 0, 1, 2]      # 真实 token 从位置 0 起

去填充 prompt    = [10, 11, 12]

response(右填充)  = [20, 21, 2, 0]
get_eos_mask     = [ 1,  1, 1, 0]           # eos 在下标 2，之后置 0
response pos_ids = [2]+[1,2,3,4] = [3,4,5,6]

完整 seq         = [ 0, 0, 0,10,11,12, | 20,21, 2, 0]
attention_mask   = [ 0, 0, 0, 1, 1, 1, |  1, 1, 1, 0]
position_ids     = [ 0, 0, 0, 0, 1, 2, |  3, 4, 5, 6]
```

第 7 步（n=2）：vLLM 对 1 个 prompt 出 2 条 response，response 第 0 维 = 2；prompt 侧 `repeat_interleave(2)` 后第 0 维也 = 2，最终 `seq` 形状 `[2, 10]`。

**需要观察的现象**：示意图中 position_ids 在 prompt→response 交界处（2→3）连续；attention_mask 在 response 的 eos 之后归零；pad 位（prompt 左侧、response 右侧）的 mask 均为 0。

**预期结果**：与参考答案一致。本实践为纯张量手算，可直接用 torch 复现验证。

---

## 6. 本讲小结

- `vLLMRollout` 是 verl 用 vLLM 做生成（rollout）的类，与 FSDP 训练态权重**解耦**：推理时通过 sharding manager 同步权重、重建 cache engine，推理完 offload 让显存。
- `free_cache_engine=True` 必须搭配 `enforce_eager=True`（关 CUDA graph），否则断言失败——因为 CUDA graph 固化了 KV cache 地址，与「动态释放/重建 cache」互斥。
- `_pre_process_inputs` 用 `nonzero` 找第一个非 pad 位置，把**左填充 prompt** 还原成无填充的 `List[int]` 交给 vLLM；该函数只对左填充有效。
- `generate_sequences` 把 vLLM 的变长 response **右填充**对齐到 `response_length`，再用 `get_eos_mask`（eos 处截断）重建 response 的 attention_mask，用「prompt 末位 + arange」重建连续的 position_ids。
- vLLM 返回的 logprobs **不直接使用**，调用方会用 FSDP actor 的前向重算 `old_log_probs`，保证 PPO 的 importance ratio 起点为 1。
- `do_sample=False`（来自 meta_info，验证时）走贪心路径（temperature=0、n=1），靠 `update_sampling_params` 上下文管理器临时改参数、用完还原；`rollout.n>1`（训练采样）走 `repeat_interleave` 扩展 batch，是 GRPO 的入口。

---

## 7. 下一步学习建议

本讲只讲了 vLLM 引擎「怎么生成、怎么重建张量」，但留下了一个大问号：**vLLM 的权重从哪来？** 答案是每次生成前由 sharding manager 从 FSDP 同步过来。这正是下一讲的主题：

- **[u6-l5 FSDP↔vLLM 权重同步](u6-l5-fsdp-vllm-weight-sync.md)**：精读 `FSDPVLLMShardingManager` 的 `__enter__`/`sync_model_weights`/`preprocess_data`/`postprocess_data`，看清训练态↔推理态的权重搬运与 tp 组通信。建议把本讲「为什么 logprob 要重算」「offload 让显存」的认知带过去，二者是一体的。

此外建议回头对照阅读：

- [fsdp_workers.py 的 `generate_sequences`（第 400-446 行）](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L400-L446)，看清本讲的 `vLLMRollout.generate_sequences` 是如何被 sharding manager 包裹、并在之后重算 `old_log_probs` 的——这是把本讲与 u6-l1、u6-l5 串起来的关键调用点。
- [verl/third_party/vllm/vllm_v_0_5_4/llm.py 的 `_post_process_outputs`](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/third_party/vllm/vllm_v_0_5_4/llm.py#L213-L233)，理解 vLLM 输出 `(output_token_ids, logprobs)` 二元组的由来与内部右填充逻辑。
