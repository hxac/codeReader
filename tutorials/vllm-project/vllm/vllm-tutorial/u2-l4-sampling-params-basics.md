# SamplingParams 采样参数入门

## 1. 本讲目标

本讲聚焦于「如何控制 vLLM 生成什么样的文本」。学完后你应当能够：

- 说清 `SamplingParams` 在生成流程中的位置：它是连接「用户意图」与「采样器实际行为」的参数容器。
- 掌握 `temperature`、`top_p`、`top_k`、`max_tokens`、`n` 这几个最常用字段的含义与默认值。
- 理解温度如何决定「贪婪采样（greedy）」还是「随机采样（random）」，以及 `__post_init__` 在其中做的修正。
- 能在 `generate` 调用中正确设置这些参数，并预测它们对输出多样性与长度的影响。

本讲承接 u2-l1（离线推理与 `LLM.generate`）：那里我们只笼统地传了一个 `sampling_params`，本讲要把它彻底讲透。

## 2. 前置知识

在阅读本讲前，建议你已经了解：

- **logits（ logits / 逻辑值）**：模型对每个候选 token 给出的未归一化分数，形状是 `[词表大小]`。分数越高代表模型越认为该 token 合理。
- **softmax 与概率分布**：把 logits 转成一组和为 1 的概率。采样器本质上就是「先得到概率分布，再从中抽一个 token」。
- **贪心采样（greedy）**：每步只取概率最大的那个 token，结果完全确定。
- **离线推理入口**：u2-l1 讲过的 `LLM.generate(prompts, sampling_params)`，其中 `sampling_params` 就是本讲的主角。

一个最小的心智模型：每生成一个 token，模型都先吐出一组 logits，采样器根据 `SamplingParams` 把这组 logits 加工成一个概率分布，然后抽签。不同参数就是在改变「加工」这一步。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vllm/sampling_params.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py) | `SamplingParams` 的定义，所有字段、默认值、校验（`_verify_args`）与构造后修正（`__post_init__`）都在这里。本讲的主战场。 |
| [examples/basic/offline_inference/basic.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/examples/basic/offline_inference/basic.py) | 最简离线推理示例，演示 `SamplingParams(temperature=0.8, top_p=0.95)` 的用法。 |
| [vllm/v1/sample/sampler.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/sample/sampler.py) | V1 采样器实现，类文档字符串给出了 logits 加工的精确顺序；本讲用它说明参数「在哪儿生效」。 |

## 4. 核心概念与源码讲解

### 4.1 SamplingParams：承载采样意图的参数容器

#### 4.1.1 概念说明

`SamplingParams` 是一个「参数声明对象」。你不需要自己写采样逻辑，只需要把生成意图填进它的字段，引擎会在合适的时机读这些字段去驱动采样器。它本质上回答一个问题：「这一批请求，应该怎样从 logits 里抽出下一个 token？」

vLLM 的 `SamplingParams` 在设计上**对齐 OpenAI 的 text completion API**（见类文档字符串），所以你若用过 OpenAI 的 `temperature`、`top_p`，概念可以直接迁移。

#### 4.1.2 核心流程

`SamplingParams` 的生命周期可以概括为三步：

1. **构造**：用户传入 `temperature`、`top_p` 等关键字参数。
2. **构造后修正 + 校验**：`__post_init__` 把零散的输入归一化（例如把字符串 `stop` 统一成列表），并对每个字段做范围检查（`_verify_args`）。
3. **采样时消费**：采样器读取这些字段，决定贪婪/随机、截断范围与生成长度。

#### 4.1.3 源码精读

`SamplingParams` 是一个 `msgspec.Struct`（带 `PydanticMsgspecMixin`），这意味着它既能被 msgspec 高效序列化（进程间传递），又能享受 pydantic 的校验能力：

[vllm/sampling_params.py:199-211](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L199-L211) — 类声明，文档字符串写明「遵循 OpenAI text completion API 的采样参数」。

各字段的默认值与含义集中在文件上半部分。下面是最常用的几个：

[vllm/sampling_params.py:236-245](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L236-L245) — `temperature`、`top_p`、`top_k` 的声明与一句话说明。注意默认值：`temperature=1.0`、`top_p=1.0`、`top_k=0`（0 表示不限制）。

[vllm/sampling_params.py:262-266](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L262-L266) — `max_tokens` 默认 `16`，`min_tokens` 默认 `0`。

[vllm/sampling_params.py:213-223](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L213-L223) — `n` 字段：一次请求生成多少条候选输出。

`basic.py` 给出了最典型的使用方式——构造一个 `SamplingParams` 对象后整体传给 `generate`：

[examples/basic/offline_inference/basic.py:14](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/examples/basic/offline_inference/basic.py#L14) — `sampling_params = SamplingParams(temperature=0.8, top_p=0.95)`。

[examples/basic/offline_inference/basic.py:23](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/examples/basic/offline_inference/basic.py#L23) — 把它传给 `llm.generate(prompts, sampling_params)`，4 条 prompt 共用同一套采样参数。

#### 4.1.4 代码实践

1. **目标**：感受「字段即意图，默认值即安全档」。
2. **步骤**：在 Python 解释器里（无需启动引擎）直接构造对象并查看其 `__repr__`：

   ```python
   from vllm import SamplingParams
   sp = SamplingParams(temperature=0.8, top_p=0.95)
   print(sp)              # 看哪些字段被设置、哪些仍是默认
   print(sp.max_tokens)   # 16，因为你没传它
   ```
3. **观察现象**：`__repr__` 会列出 `n`、各 penalty、`temperature`、`top_p`、`top_k`、`max_tokens` 等。`max_tokens` 显示为 `16`，验证「未设置即默认」。
4. **预期结果**：`print(sp)` 输出以 `SamplingParams(n=1, ...)` 开头，`top_k=0`、`max_tokens=16`。
5. 是否需要运行环境：此步只需 `import vllm`，可不加载模型；输出结果待本地验证。

#### 4.1.5 小练习与答案

- **练习**：`SamplingParams()`（全用默认）等价于什么样的采样策略？
- **答案**：`temperature=1.0`、`top_p=1.0`、`top_k=0`，即「不缩放、不截断」地从模型原始分布里随机采样，最多生成 16 个 token、1 条候选。

---

### 4.2 temperature：温度与「贪婪 vs 随机」

#### 4.2.1 概念说明

`temperature`（温度）控制概率分布的「尖锐程度」。模型给出 logits \(z_i\)，温度 \(T\) 把它变成 \(z_i / T\)，再 softmax：

\[
p_i = \frac{\exp(z_i / T)}{\sum_j \exp(z_j / T)}
\]

- \(T \to 0\)：分布越来越尖，最终退化为「永远取最大」（贪婪采样）。
- \(T = 1\)：使用模型原始分布。
- \(T > 1\)：分布变平，低概率 token 也更容易被抽到，输出更「放飞」。

在 vLLM 里，**`temperature` 接近 0 会被强制当作贪婪采样**，这是 vLLM 的一个关键约定。

#### 4.2.2 核心流程

vLLM 用一个 `SamplingType` 枚举把温度归为三类，决定采样路径：

1. `temperature < 1e-5` → `GREEDY`（贪婪，确定性）。
2. 否则若设置了 `seed` → `RANDOM_SEED`（可复现的随机）。
3. 否则 → `RANDOM`（随机）。

一旦判定为贪婪，`__post_init__` 会**强制**把 `top_p`、`top_k`、`min_p` 复位（因为截断对 argmax 毫无意义），并要求 `n == 1`。

#### 4.2.3 源码精读

采样类型枚举：

[vllm/sampling_params.py:64-67](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L64-L67) — `SamplingType`：`GREEDY`、`RANDOM`、`RANDOM_SEED`。

构造后修正里判定贪婪并复位截断参数：

[vllm/sampling_params.py:499-504](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L499-L504) — `temperature < _SAMPLING_EPS` 时把 `top_p=1.0`、`top_k=0`、`min_p=0.0`，并调用 `_verify_greedy_sampling()`。

[vllm/sampling_params.py:640-644](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L640-L644) — `_verify_greedy_sampling`：贪婪采样时 `n` 必须为 1。

把温度映射成类型的逻辑：

[vllm/sampling_params.py:717-723](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L717-L723) — `sampling_type` 缓存属性：`temperature < eps` 返回 `GREEDY`，设了 `seed` 返回 `RANDOM_SEED`，否则 `RANDOM`。

采样器侧，温度通过除法施加，最终用 `torch.where` 在「贪婪结果」与「随机结果」之间二选一：

[vllm/v1/sample/sampler.py:227-241](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/sample/sampler.py#L227-L241) — `apply_temperature`（`logits / temp`，且对贪婪请求把 `temp` 当 1.0 防止除零）与 `greedy_sample`（`argmax`）。

[vllm/v1/sample/sampler.py:296-302](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/sample/sampler.py#L296-L302) — 同一批请求里，温度低于阈值的走 `greedy_sampled`，其余走 `random_sampled`。

#### 4.2.4 代码实践

1. **目标**：对比 `temperature=0`（贪婪，确定性）与 `temperature=0.9`（随机，多样）。
2. **步骤**：基于 `basic.py` 写两段调用（示例代码）：

   ```python
   # 示例代码
   from vllm import LLM, SamplingParams
   llm = LLM(model="facebook/opt-125m")
   prompt = "The capital of France is"

   greedy = llm.generate([prompt], SamplingParams(temperature=0, max_tokens=20))
   random = llm.generate([prompt], SamplingParams(temperature=0.9, max_tokens=20))

   print("greedy:", greedy[0].outputs[0].text)
   print("random:", random[0].outputs[0].text)
   ```
3. **观察现象**：把贪婪那段**运行两次**，输出应完全相同；随机那段两次通常不同。
4. **预期结果**：`temperature=0` 输出稳定可复现；`temperature=0.9` 输出有随机性。
5. 是否需要运行环境：需要能加载 `opt-125m` 的环境（CPU/GPU 皆可），运行结果待本地验证。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 vLLM 在 `temperature=0` 时要把 `top_p` 强制设回 `1.0`？
- **答案**：贪婪采样每步只取 argmax，截断（top_p/top_k）改变的是候选集合，对「永远取最大」没有任何影响，复位可避免误导并简化采样路径。
- **练习 2**：设 `temperature=0` 且 `n=3` 会发生什么？
- **答案**：`_verify_greedy_sampling` 抛出 `VLLMValidationError`，因为贪婪采样要求 `n == 1`。

---

### 4.3 top_p / top_k / min_p：截断采样三兄弟

#### 4.3.1 概念说明

随机采样时，原始分布往往有一条「长尾」——很多概率极低、明显不合理的 token。如果直接按原始分布抽签，偶尔会抽到这些烂 token，导致输出崩坏。**截断采样**的核心思想是「先把明显不靠谱的 token 摘掉，再在剩下的里抽」。vLLM 提供三种互补的截断方式：

- **top_k**：只保留概率最高的 `k` 个 token。
- **top_p（nucleus sampling / 核采样）**：把 token 按概率从高到低排序，取**累计概率首次达到 `top_p`** 的那一段。
- **min_p**：保留概率不低于「最大概率 × `min_p`」的 token（一种相对阈值）。

三者可以同时生效，取交集。

#### 4.3.2 核心流程

在采样器的 `sample()` 里，截断发生在施加温度之后、真正抽签之前。`Sampler` 类的文档字符串把整条加工链总结得非常清楚：

1. 施加温度。
2. 施加「对 argmax 不变」的处理器（默认含 min_p）。
3. 施加 top_k 和/或 top_p。
4. 在剩余分布里抽签。

也就是：`logits → ÷温度 → min_p 截断 → top_k/top_p 截断 → 抽样`。

#### 4.3.3 源码精读

字段声明：

[vllm/sampling_params.py:240-249](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L240-L249) — `top_p`（区间 (0, 1]，默认 1.0 即不截断）、`top_k`（0 或 -1 表示不限制）、`min_p`（[0,1]，默认 0.0）。

字段校验（范围）：

[vllm/sampling_params.py:565-581](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L565-L581) — `top_p` 必须 `0 < top_p <= 1`；`top_k` 不能小于 -1（-1 与 0 等价于关闭）；`min_p` 必须 `0 <= min_p <= 1`。

采样器文档字符串给出的加工顺序：

[vllm/v1/sample/sampler.py:41-51](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/sample/sampler.py#L41-L51) — 第 7 步「采样下一个 token」的子步骤：贪婪 → 温度 → argmax 不变处理器（默认 min_p）→ top_k/top_p → 抽样。

实际调用：

[vllm/v1/sample/sampler.py:285-291](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/sample/sampler.py#L285-L291) — `self.topk_topp_sampler(logits, generators, top_k, top_p)` 同时处理 top_k 与 top_p。

#### 4.3.4 代码实践

1. **目标**：体会 `top_k` 对随机性的收敛作用。
2. **步骤**：固定一个较高温度，分别用「无 top_k」与「top_k=1」对比（示例代码）：

   ```python
   # 示例代码
   from vllm import LLM, SamplingParams
   llm = LLM(model="facebook/opt-125m")
   prompt = "Once upon a time"

   a = llm.generate([prompt], SamplingParams(temperature=0.9, max_tokens=20))
   b = llm.generate([prompt], SamplingParams(temperature=0.9, top_k=1, max_tokens=20))
   print("no top_k:", a[0].outputs[0].text)
   print("top_k=1 :", b[0].outputs[0].text)
   ```
3. **观察现象**：`top_k=1` 等价于「只在最高概率 token 里抽」，因为只有一个候选，它退化为贪婪。
4. **预期结果**：`top_k=1` 的输出确定性等同贪婪，且应与 `temperature=0` 的结果一致。
5. 是否需要运行环境：需要能加载模型的环境，结果待本地验证。

#### 4.3.5 小练习与答案

- **练习**：`top_p=0.9` 与 `top_k=50` 同时设置时，最终的候选集合是什么？
- **答案**：先取概率最高的 50 个 token，再在其中按累计概率截到 0.9，最终候选是两者的**交集**——既不超过 50 个，累计概率也不超过 0.9 的那段。

---

### 4.4 max_tokens / min_tokens：控制生成长度

#### 4.4.1 概念说明

截断参数决定「每一步抽哪个 token」，而长度参数决定「一共抽多少步」：

- **max_tokens**：每个输出序列最多生成多少个 token，到数即停（默认 **16**，偏小，实战中常需调大）。
- **min_tokens**：在生成够这么多 token 之前，**不允许**输出 EOS 或 `stop_token_ids`（默认 0，即不约束）。

`min_tokens` 的典型用途是「强制模型把话说完」——例如要求摘要至少 50 个 token，避免模型一开口就结束。

#### 4.4.2 核心流程

1. 每生成一个 token，计数器 +1。
2. 若已生成数 `>= min_tokens`，才允许命中 EOS / stop token 而结束。
3. 若已生成数 `== max_tokens`，强制结束（`finish_reason` 通常为 `length`）。
4. 注意 `max_tokens` 还受模型 `max_model_len` 约束：`prompt 长度 + max_tokens` 不能超过模型最大长度。

#### 4.4.3 源码精读

[vllm/sampling_params.py:262-266](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L262-L266) — `max_tokens`（默认 16）、`min_tokens`（默认 0）字段。

[vllm/sampling_params.py:582-596](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L582-L596) — 校验：`max_tokens >= 1`、`min_tokens >= 0`，且 `min_tokens <= max_tokens`。

`min_tokens` 的强制效果体现在采样器对 logits 的预处理——在未达最小长度时屏蔽 EOS：

[vllm/v1/sample/sampler.py:33-36](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/sample/sampler.py#L33-L36) — 类文档第 5 步：施加「非 argmax 不变」的处理器，其中就包含 min_tokens 处理器（在达到 min_tokens 前把 EOS 的 logit 压成 -inf）。

#### 4.4.4 代码实践

1. **目标**：直观看到 `max_tokens` 截断输出。
2. **步骤**：用贪婪采样分别设 `max_tokens=5` 与 `max_tokens=30`（示例代码）：

   ```python
   # 示例代码
   from vllm import LLM, SamplingParams
   llm = LLM(model="facebook/opt-125m")
   prompt = "The future of AI is"

   short = llm.generate([prompt], SamplingParams(temperature=0, max_tokens=5))
   long  = llm.generate([prompt], SamplingParams(temperature=0, max_tokens=30))
   print("5  tokens:", repr(short[0].outputs[0].text))
   print("30 tokens:", repr(long[0].outputs[0].text))
   print("finish_reason:", long[0].outputs[0].finish_reason)
   ```
3. **观察现象**：`max_tokens=5` 的输出明显更短。
4. **预期结果**：短输出的 token 数约为 5；若长输出是因长度而非 EOS 结束，`finish_reason` 为 `"length"`。
5. 是否需要运行环境：需要模型环境，结果待本地验证。

#### 4.4.5 小练习与答案

- **练习**：为什么 `max_tokens` 默认只有 16，而 `min_tokens` 默认是 0？
- **答案**：16 是一个保守的安全默认，防止新手忘了设上限时模型无限生成；`min_tokens=0` 表示默认不强制最小长度，把「是否要强制说够多」交给用户按场景决定。

---

### 4.5 n：一次生成多条候选

#### 4.5.1 概念说明

`n` 决定**同一个 prompt 生成几条独立的候选输出**。设 `n=3`，`generate` 返回的每个 `RequestOutput` 里会带 3 个 `CompletionOutput`（回忆 u2-l1 的两层结构）。这在「需要多个备选答案再挑选」的场景（如 best-of-n、多样本投票）很有用。

两个关键约束：

- **贪婪采样时 `n` 必须为 1**（因为贪婪每次结果都一样，多生成几条毫无意义）。
- `n` 有一个全局上限，由环境变量 `VLLM_MAX_N_SEQUENCES`（默认 16384）控制。

#### 4.5.2 核心流程

1. 引擎为同一个 prompt 复制 `n` 个序列，它们共享 prompt 的 KV 缓存（前缀复用），但各自独立采样。
2. 每条序列独立生成，各自达到 EOS 或 max_tokens 后结束。
3. 结果聚合：`RequestOutput.outputs` 是长度为 `n` 的 `CompletionOutput` 列表。

#### 4.5.3 源码精读

[vllm/sampling_params.py:213-223](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L213-L223) — `n` 字段及其说明（含 `VLLM_MAX_N_SEQUENCES` 上限提示，以及流式下 `n>1` 需配合 `output_kind=FINAL_ONLY` 的注意）。

[vllm/sampling_params.py:516-528](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L516-L528) — 校验：`n` 必须是 `>= 1` 的整数，且 `<= VLLM_MAX_N_SEQUENCES`。

[vllm/sampling_params.py:640-644](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L640-L644) — 贪婪采样（`temperature≈0`）时 `n` 必须为 1。

#### 4.5.4 代码实践

1. **目标**：用 `n=3` 在随机采样下拿到多条候选。
2. **步骤**（示例代码）：

   ```python
   # 示例代码
   from vllm import LLM, SamplingParams
   llm = LLM(model="facebook/opt-125m")
   prompt = "A creative name for a pet cat is"
   outs = llm.generate([prompt], SamplingParams(temperature=0.9, n=3, max_tokens=12))

   for i, comp in enumerate(outs[0].outputs):
       print(f"candidate {i}:", repr(comp.text))
   ```
3. **观察现象**：`outs[0].outputs` 长度为 3，三条文本互不相同。
4. **预期结果**：打印出 3 条风格各异的候选名。
5. 是否需要运行环境：需要模型环境，结果待本地验证。

#### 4.5.5 小练习与答案

- **练习**：把上面例子的 `temperature` 改成 `0` 会怎样？
- **答案**：触发 `_verify_greedy_sampling`，抛出 `VLLMValidationError`——贪婪采样不允许 `n > 1`。

---

## 5. 综合实践

**任务**：写一个脚本，用**同一个 prompt** 探索四类参数的联合影响，并解释你看到的现象。

建议脚本（示例代码，基于 `basic.py` 改造）：

```python
# 示例代码
from vllm import LLM, SamplingParams

llm = LLM(model="facebook/opt-125m")
prompt = "The most important skill for a software engineer is"

configs = {
    "贪婪/短":   SamplingParams(temperature=0,   max_tokens=10),
    "贪婪/长":   SamplingParams(temperature=0,   max_tokens=40),
    "随机/top_k": SamplingParams(temperature=0.9, top_k=20, max_tokens=20),
    "多样/n=3":  SamplingParams(temperature=0.9, n=3,      max_tokens=20),
}

for name, sp in configs.items():
    result = llm.generate([prompt], sp)[0]
    texts = [o.text for o in result.outputs]
    print(f"[{name}] n={len(texts)} -> {texts}")
```

完成后，请回答：

1. 「贪婪/短」与「贪婪/长」的输出前 10 个 token 是否完全一致？为什么？（提示：贪婪是确定性的。）
2. 「随机/top_k」连续运行两次，结果是否相同？把它换成 `temperature=0` 又会怎样？
3. 「多样/n=3」为什么必须搭配 `temperature > 0`？若把 `max_tokens` 调到 1，`outputs` 里还有 3 条吗？

> 运行说明：以上脚本需要可加载 `facebook/opt-125m` 的 vLLM 环境（CPU 或 GPU）。若当前环境无 GPU/未安装 vLLM，可先做「源码阅读型实践」：对照 [vllm/sampling_params.py:457-513](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L457-L513) 的 `__post_init__`，手动推演每组 `SamplingParams` 构造后哪些字段会被修正，再待本地验证实际输出。

## 6. 本讲小结

- `SamplingParams` 是连接「用户采样意图」与「V1 采样器」的参数容器，字段语义对齐 OpenAI text completion API。
- `temperature` 决定分布尖锐度；接近 0 时 vLLM 在 `__post_init__` 里强制判定为**贪婪采样**，并复位 `top_p/top_k/min_p`、要求 `n==1`。
- `top_p`（核采样）、`top_k`、`min_p` 是三种互补的**截断**手段，在采样链中于「施加温度之后、抽签之前」生效，可同时设置取交集。
- `max_tokens`（默认 16）限定生成长度上限，`min_tokens` 在未达最小长度前屏蔽 EOS，二者共同决定一条序列何时结束。
- `n` 让一个 prompt 产出多条独立候选，但**贪婪采样下只能为 1**，且受 `VLLM_MAX_N_SEQUENCES` 上限约束。
- 真正消费这些字段的是 `vllm/v1/sample/sampler.py` 的 `Sampler`，其类文档字符串给出了 logits 加工的权威顺序。

## 7. 下一步学习建议

- **深入采样实现**：本讲只到「参数如何被声明与校验」。想看 `top_k`/`top_p` 的真实截断内核，可读 `vllm/v1/sample/ops/topk_topp_sampler.py`，后续 u7-l1（采样器与 logits 处理）会系统讲解。
- **采样元数据如何承载多请求参数**：一批请求里每个的 `temperature`/`top_k` 都不同，采样器如何批量处理？见 `vllm/v1/sample/metadata.py`（u7-l1 覆盖）。
- **penalty 家族**：`presence_penalty`、`frequency_penalty`、`repetition_penalty` 也是 `SamplingParams` 字段，本讲未展开，可在 u7-l1 一并学习。
- **进阶字段**：`seed`、`stop`、`logprobs`、`structured_outputs` 等留到对应专题（如 u9-l5 结构化输出）再深入。
