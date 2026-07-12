# 生成配置 GenerationConfig 详解

## 1. 本讲目标

在上一讲（u2-l1）里，我们已经认识了 `lmdeploy/messages.py` 这个「公共词汇表」，知道 `GenerationConfig` 是一个 `@dataclass`，会在构造时通过 `__post_init__` 校验采样参数，并被引擎层用 `SamplingParam.from_gen_config` 转换兜底。本讲要在这个基础上往下走一层，把 `GenerationConfig` **每一个常用字段**讲透：

1. 理解采样参数（`temperature` / `top_k` / `top_p` / `min_p` / `do_sample`）各自的含义、默认值以及对输出分布的实际影响。
2. 理解重复惩罚（`repetition_penalty`、`repetition_ngram_*`）和停止控制（`stop_words` / `stop_token_ids` / `bad_words` / `min_new_tokens`）的作用方式与作用阶段。
3. 认识高级输出选项：`n`、`logprobs`、`response_format`、自定义 `logits_processors`，并能判断哪些字段 lmdeploy 实际支持、哪些只是 API 兼容占位。
4. 理解 `QuantPolicy` 枚举如何描述 KV cache 量化策略，以及它在两套引擎配置中的合法取值差异。
5. 学会在 `pipeline(...)` 调用时传入 `gen_config`，并能从源码中反查任意参数的默认值与生效阶段。

学完本讲，你应该能「看着 `GenerationConfig` 的源码，准确说出每个参数会让模型输出变得更具确定性还是更多样、以及它是在采样前还是采样后生效」。

## 2. 前置知识

本讲假设你已经读过 u2-l1，知道：

- `GenerationConfig` 是用户面（`lmdeploy/messages.py`）的类型，`SamplingParam` 是引擎面（`lmdeploy/pytorch/messages.py`）的类型；用户传 `GenerationConfig`，引擎把它转成 `SamplingParam` 后再驱动推理。
- 两个 `messages.py` 文件（`lmdeploy/messages.py` 与 `lmdeploy/pytorch/messages.py`）不要混淆。

补充几个本讲会用到的 LLM 采样基础概念，便于从直觉过渡到源码：

- **Logits（未归一化对数概率）**：模型最后一层对词表中每个 token 给出的原始分数。采样是在 logits 上做的。
- **Softmax**：把 logits 归一化成概率分布。
- **采样（sampling）vs 贪婪解码（greedy decoding）**：贪婪解码每步选 logits 最大的那一个 token，结果确定；采样则按概率分布随机抽取，结果随机、更多样。
- **温度（temperature）**：在 softmax 前对 logits 除以一个正数 T，T 越小分布越「尖」（更确定），T 越大分布越「平」（更多样）。
- **Prefill / Decode 两阶段**：Prefill 处理输入 prompt，Decode 逐个生成新 token。本讲讨论的采样参数几乎都作用在 Decode 的每一步（以及 Prefill 后产生第一个 token 的那一步）。

数学上，带温度 T 的下一个 token 概率为：

\[
p_i = \frac{\exp(\text{logit}_i / T)}{\sum_{j} \exp(\text{logit}_j / T)}
\]

## 3. 本讲源码地图

本讲涉及的源码文件很少，但每个都要精读：

| 文件 | 作用 |
| --- | --- |
| `lmdeploy/messages.py` | 用户面公共类型。`GenerationConfig`（采样参数）与 `QuantPolicy`（KV cache 量化策略枚举）都在这里。 |
| `lmdeploy/pytorch/messages.py` | 引擎面类型。`SamplingParam.from_gen_config` 把 `GenerationConfig` 转换成引擎真正消费的采样参数，并在这里做「兜底修正」（如 `temperature==0` 时强制 `top_k=1`）。 |
| `lmdeploy/pytorch/engine/logits_process.py` | PyTorch 引擎的 logits 处理与采样实现。重复惩罚、top_k/top_p/min_p 过滤、温度都在这里逐个实现，能直观看到「作用阶段」。 |
| `lmdeploy/cli/chat.py` | 命令行 `lmdeploy chat` 构建 `GenerationConfig` 的地方，可作为「哪些参数被实际使用」的参考样例。 |

> 说明：TurboMind 后端（C++）的采样逻辑在 `src/turbomind/generation/logits_processor.cc` 中，原理一致，本讲以 PyTorch 后端的 Python 实现为例讲清「阶段」，便于读者直接读源码。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：4.1 讲 `GenerationConfig` 的整体结构与构造校验；4.2 讲采样参数族；4.3 讲重复惩罚与停止控制；4.4 讲高级输出选项；4.5 讲 `QuantPolicy` 枚举。

### 4.1 GenerationConfig 的整体结构与构造校验

#### 4.1.1 概念说明

`GenerationConfig` 是用户向 lmdeploy 表达「我希望模型怎么生成」的载体：要不要采样、采样多激进、生成多长、遇到哪些词停下、要不要返回 logprobs……全部用一组字段来描述。它本身**不参与计算**，只是一个被校验过的「配置对象」，引擎会在真正推理前把它翻译成内部的 `SamplingParam`。

理解它的两个关键点：

1. **它是 dataclass，字段即默认值**：所以「默认值」可以直接从源码字段定义里读出来，不需要猜。
2. **它在构造时就校验**：`__post_init__` 会在对象创建那一刻检查取值范围，非法值直接 `assert` 失败。这把「参数错误」挡在了推理之前。

#### 4.1.2 核心流程

`GenerationConfig` 的生命周期：

```text
用户构造 GenerationConfig(temperature=0.7, top_p=0.9, ...)
        │
        ▼  dataclass 自动赋值字段
__post_init__：校验 n / top_p / top_k / temperature / min_p 取值范围
        │
        ▼  传入 pipeline(...).stream_infer(gen_config=...)
引擎层 SamplingParam.from_gen_config(gen_config)：做兜底修正（见 4.2）
        │
        ▼
logits_process.py 中的 FusedLogitsProcessor：在每一步 logits 上真正执行
```

#### 4.1.3 源码精读

`GenerationConfig` 是一个 `@dataclass`，类定义与字段默认值在：

[lmdeploy/messages.py:35-36](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L35-L36) —— 类声明，注意它用的是标准库 `@dataclass`（而同文件的 `TurbomindEngineConfig` 用的是 pydantic 的 `@pydantic_dataclass`，二者校验机制不同）。

采样相关字段的默认值在：

[lmdeploy/messages.py:115-123](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L115-L123) —— 这里能看到关键默认值：`n=1`、`max_new_tokens=512`、`do_sample=False`、`top_p=1.0`、`top_k=50`、`min_p=0.0`、`temperature=0.8`、`repetition_penalty=1.0`。

构造时的校验逻辑在 `__post_init__`：

[lmdeploy/messages.py:195-205](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L195-L205) —— 校验 `n` 为正整数、`top_p ∈ [0,1]`、`top_k >= 0`、`temperature ∈ [0,2]`、`min_p ∈ [0,1]`；并对 ngram 重复参数做「小于等于 0 则归零」的处理。

值得注意的细节：

- `temperature` 允许的上限是 **2**（`assert self.temperature <= 2`），不是无穷大。
- `top_k` 允许 **0**（表示「不过滤」或「由其它参数决定」，见 4.2）。
- `do_sample` 这个字段虽然存在，但**PyTorch 引擎并不读取它**（在引擎层 grep 不到 `.do_sample` 的消费点）。真正决定「贪婪 vs 采样」的是 `temperature` 与 `top_k`，详见 4.2。`do_sample` 主要在 CLI（`cli/chat.py`）里被设为 `True` 用于语义占位。

#### 4.1.4 代码实践

实践目标：直接读源码，把 `GenerationConfig` 的默认值表抄出来，并验证校验确实在构造时触发。

操作步骤：

1. 打开 `lmdeploy/messages.py` 第 115–152 行，把每个字段名和默认值列成一张表。
2. 运行下面这段「示例代码」（不需要加载模型，仅验证 dataclass 行为），观察哪些构造会成功、哪些会抛 `AssertionError`：

```python
# 示例代码：仅验证 GenerationConfig 的构造校验，不加载模型
from lmdeploy import GenerationConfig

# 合法构造
g1 = GenerationConfig()                       # 全部使用默认值
g2 = GenerationConfig(temperature=1.5, top_p=0.9, top_k=40)
print('默认 temperature =', g1.temperature)    # 预期 0.8
print('默认 repetition_penalty =', g1.repetition_penalty)  # 预期 1.0

# 非法构造（取消注释后会 AssertionError）
# GenerationConfig(temperature=3.0)   # 超出上限 2
# GenerationConfig(top_p=1.5)         # 超出 [0,1]
# GenerationConfig(n=0)               # n 必须为正整数
```

需要观察的现象 / 预期结果：合法构造打印出默认值；非法构造在创建对象那一刻（而非推理时）就抛出 `AssertionError`。**待本地验证**：不同 lmdeploy 版本字段可能微调，以你本地源码为准。

#### 4.1.5 小练习与答案

**练习 1**：`GenerationConfig(temperature=0.0)` 能否通过 `__post_init__` 的校验？为什么？

**答案**：能通过。`__post_init__` 只要求 `temperature >= 0 and temperature <= 2`，`0.0` 合法。至于「temperature=0 等于贪婪解码」的兜底逻辑不在 dataclass 里，而在引擎层的 `SamplingParam.from_gen_config`（见 4.2.3）。

**练习 2**：`GenerationConfig` 用的是 `@dataclass`，而 `TurbomindEngineConfig` 用的是 `@pydantic_dataclass`。这两者在「构造时校验」上会有什么体验差异？

**答案**：`@dataclass` 的校验完全依赖你在 `__post_init__` 里手写的 `assert`，类型注解（如 `int`、`float`）本身不做强制转换；`@pydantic_dataclass` 则会把字段做类型校验与转换，并抛出 pydantic 风格的 `ValidationError`。所以同样传一个非法字符串，两者的报错形式不同。

### 4.2 采样参数族：从贪婪到多样

#### 4.2.1 概念说明

这是 `GenerationConfig` 最常被调整的一组参数。它们的共同目标是：**控制每一步从 logits 到「下一个 token」的映射有多确定**。按作用方式可分三类：

- **温度 `temperature`**：在 softmax 前对 logits 整体缩放，改变分布的「尖锐程度」。
- **截断类 `top_k` / `top_p` / `min_p`**：在采样前先把「不可能」的 token 概率置为 `-inf`，缩小候选集。
- **`do_sample`**：语义开关，但在 PyTorch 引擎里实际不读取（见 4.1.3），真正的「贪婪」由 `temperature==0 → top_k=1` 实现。

直觉上：

| 想要的效果 | 调哪个参数 | 方向 |
| --- | --- | --- |
| 输出更确定、可复现 | `temperature` | 调小（0 = 贪婪） |
| 输出更有创造力、更多样 | `temperature` | 调大（如 0.9~1.2） |
| 砍掉长尾、防止极小概率乱词 | `top_p` | 调小（如 0.8~0.9，核采样） |
| 只在最高概率的 N 个里挑 | `top_k` | 调小（如 20~50） |
| 动态按最高概率比例过滤 | `min_p` | 调大（如 0.05，典型 0.01~0.2） |

#### 4.2.2 核心流程

每一步 Decode 的 logits 会按下面的顺序被处理（截取自 `FusedLogitsProcessor.__call__`）：

```text
原始 logits
  → （引导解码 / 自定义 logits_processors，见 4.4）
  → repetition_penalty（见 4.3）
  → temperature 缩放: scores / T
  → bad_words / stop_words 屏蔽
  → top_k 过滤（保留最高 k 个，其余 -inf）
  → top_p 过滤（累积概率 >= top_p 的核之外的 -inf）
  → min_p 过滤（概率 < min_p * 最大概率 的 -inf）
  → softmax + 采样（贪婪时等价于 argmax）
```

注意顺序很关键：**温度在截断类参数之前**，所以 `top_p` 的「累积概率」是在温度调整后的概率上计算的。这意味着 `temperature` 和 `top_p` 不是独立叠加，而是先后作用。

#### 4.2.3 源码精读

引擎层的兜底转换 `SamplingParam.from_gen_config` 在：

[lmdeploy/pytorch/messages.py:150-151](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L150-L151) ← 注意此链接指向引擎面 `lmdeploy/pytorch/messages.py`（不是 `lmdeploy/messages.py`），这里展示了 `from_gen_config` 的入口。

`temperature==0` 强制贪婪的兜底逻辑：

[lmdeploy/pytorch/messages.py:185-192](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L185-L192) —— 当 `temperature == 0` 时，记一条 warning，把 `temperature` 临时改回 `1.0`、并把 `top_k` 强制设为 `1`。`top_k=1` 即「只在最高概率那一个里挑」，等价于 argmax（贪婪解码）。**这就是 PyTorch 引擎里「贪婪解码」的真正触发点，而不是 `do_sample` 字段。**

下面三个截断过滤函数直观展示了 `top_k` / `top_p` / `min_p` 各自如何改写 logits：

[lmdeploy/pytorch/engine/logits_process.py:68-75](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/logits_process.py#L68-L75) —— `_filter_topk_sorted_`：在已排序的 scores 上，把排名 `>= top_k` 的位置全部置为 `-inf`。

[lmdeploy/pytorch/engine/logits_process.py:78-85](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/logits_process.py#L78-L85) —— `_filter_topp_sorted_`：先 softmax 求概率，再做累积和，把「累积概率超过 `top_p`」的尾部 token 置 `-inf`（核采样）。注意 `mask[:, 0] = False` 保证至少保留 1 个 token。

[lmdeploy/pytorch/engine/logits_process.py:88-95](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/logits_process.py#L88-L95) —— `_filter_minp_sorted_`：先求最大概率 `top_probs`，把概率小于 `min_p * top_probs` 的 token 全部置 `-inf`。这是一种与 `top_p`「方向相反」的动态过滤。

温度缩放的实现非常简短：

温度对 logits 的缩放本质就是逐元素除以 T（在调用 softmax 之前）。结合 4.2.2 的公式，T<1 使分布更尖锐、T>1 使分布更平。

#### 4.2.4 代码实践

实践目标：用不同 `temperature` / `top_p` 组合对同一 prompt 推理 3 次，观察输出差异；同时从源码确认贪婪触发点。

操作步骤：

1. 阅读上面的 `_filter_topk_sorted_` / `_filter_topp_sorted_` / `_filter_minp_sorted_` 三个函数，预测：当 `top_k=1` 时，`top_p` 是否还有机会改变结果？（提示：候选集只剩 1 个 token。）
2. 用「示例代码」对一个本地可用的对话模型跑三组配置（需要 GPU 与模型权重）：

```python
# 示例代码：对比不同采样配置下的输出差异
from lmdeploy import pipeline, GenerationConfig

pipe = pipeline('Qwen/Qwen2.5-7B-Instruct')   # 换成你本地可用的模型
prompt = '用一句话介绍黑洞。'

configs = {
    'greedy':   GenerationConfig(temperature=0, top_p=1.0, random_seed=0),
    'creative': GenerationConfig(temperature=1.0, top_p=0.8, random_seed=0),
    'wild':     GenerationConfig(temperature=1.3, top_p=0.95, random_seed=None),
}
for name, gen in configs.items():
    resp = pipe(prompt, gen_config=gen)
    print(f'[{name}] {resp.response}')
```

需要观察的现象 / 预期结果：

- `greedy` 多次运行结果**完全一致**（确定性）。
- `creative` 偶尔有差异但整体稳定、语言通顺。
- `wild` 每次差异明显、表达更多样，但可能偶尔出现奇怪措辞。

**待本地验证**：实际输出文本取决于模型与硬件；如果你无法运行，请改为「源码阅读型实践」——只做第 1 步的预测，并对照 `_filter_topk_sorted_` 验证 `top_k=1` 时后续 `top_p` 过滤不会改变结果。

#### 4.2.5 小练习与答案

**练习 1**：为什么说 `top_p=1.0` 等于「不做核采样」？

**答案**：`_filter_topp_sorted_` 里 `mask = cum_scores > topp[:, None]`，当 `topp=1.0` 时，累积概率最多等于 1.0，`cum_scores > 1.0` 几乎处处为 `False`（加上 `mask[:, 0] = False` 的保护），所以没有任何 token 被置 `-inf`，等于不过滤。

**练习 2**：`min_p` 与 `top_p` 一个描述「概率下限比例」、一个描述「概率上限累积」，请用一句话说明二者方向相反。

**答案**：`top_p` 保留「累积概率达到 top_p 的最小候选集」（从高到低累加，砍长尾）；`min_p` 则保留「概率不低于最大概率 × min_p 的所有 token」（设定一条相对阈值，砍掉低于阈值的）。前者从上往下累加，后者设定一条相对阈值。

### 4.3 重复惩罚与停止/最小长度控制

#### 4.3.1 概念说明

这一组参数处理「生成过程中的内容控制」：

- **`repetition_penalty`（重复惩罚）**：降低已经出现过的 token 再次出现的概率。`>1` 抑制重复，`=1` 无作用，`<1` 反而鼓励重复（一般不用）。
- **`repetition_ngram_size` / `repetition_ngram_threshold`（ngram 重复早停）**：当最新的 `size` 个 token 组成的 ngram 重复出现达到 `threshold` 次时，提前停止生成，防止「死循环式」复读。
- **`stop_words` / `stop_token_ids`**：生成到这些词/token 时停止；输出**不包含**停止词本身。
- **`bad_words` / `bad_token_ids`**：这些词/token **永远不会被生成**（在采样前屏蔽）。
- **`ignore_eos`**：是否忽略模型自带的结束符（用于「不限长度」地跑下去）。
- **`min_new_tokens`**：至少生成多少个 token，在此之前即使遇到停止词也不停。

#### 4.3.2 核心流程

重复惩罚的数学定义（lmdeploy 的实现即此公式）：

\[
\text{logit}'_i =
\begin{cases}
\text{logit}_i \cdot p, & \text{logit}_i < 0 \\
\text{logit}_i \,/\, p, & \text{logit}_i \ge 0
\end{cases}
\]

其中 \(p\) 是 `repetition_penalty`，该调整**只作用于「已经生成过的 token」**。注意：惩罚对正负 logit 的处理不对称（一个乘、一个除），这是 HuggingFace `RepetitionPenaltyLogitsProcessor` 的经典实现，lmdeploy 沿用它。

作用阶段（来自 4.2.2 的顺序）：`repetition_penalty` 在**温度缩放之前、logits 刚算出来之后**应用——也就是说它直接改写原始 logits，属于「采样前」的预处理。

#### 4.3.3 源码精读

`GenerationConfig` 的默认值（关键：`repetition_penalty=1.0`）：

[lmdeploy/messages.py:115-123](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L115-L123) —— 可确认 `repetition_penalty: float = 1.0`（默认即「不惩罚」）。

引擎层对非法 `repetition_penalty` 的兜底：

[lmdeploy/pytorch/messages.py:193-196](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L193-L196) —— 当 `repetition_penalty <= 0` 时记 warning 并改回 `1.0`。

重复惩罚的核心实现 `_process_repetition_penalty_`：

[lmdeploy/pytorch/engine/logits_process.py:59-65](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/logits_process.py#L59-L65) —— `torch.gather` 取出已生成 token 对应位置的 logit，按「负 logit 乘 penalty、非负 logit 除 penalty」改写后，用 `scatter_` 写回。这正好对应 4.3.2 的公式。

它在 logits 处理主流程中的调用位置：

[lmdeploy/pytorch/engine/logits_process.py:416-419](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/logits_process.py#L416-L419) —— `FusedLogitsProcessor.__call__` 里，紧跟在自定义 logits processor 之后、温度处理（`_process_temperature_`，见第 436–438 行）之前调用 `_process_repetition_penalty_`。**这就是「重复惩罚作用阶段」的源码证据：采样前的 logits 预处理阶段，每一步 Decode 都执行一次。**

停止词/坏词的「词 → id」转换发生在 `GenerationConfig` 的方法里：

[lmdeploy/messages.py:154-174](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L154-L174) —— `convert_stop_bad_words_to_ids` 把字符串形式的 `stop_words` / `bad_words` 经 tokenizer 转成 id，再合并进 `stop_token_ids` / `bad_token_ids`，并去重。引擎最终消费的是 id 列表。

此外，`update_from_hf_gen_cfg` 会把模型 `generation_config.json` 里的 `eos_token_id` 与 tokenizer 的 eos 合并进 `stop_token_ids`：

[lmdeploy/messages.py:176-193](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L176-L193) —— 这解释了为什么即使你不显式设 `stop_words`，模型也能在合适的结束位置停下：eos 在这里被自动补进停止集合。

#### 4.3.4 代码实践

实践目标：从源码确认 `repetition_penalty` 的默认值与作用阶段（本讲核心实践之一）。

操作步骤：

1. 打开 `lmdeploy/messages.py:122`，确认 `repetition_penalty` 默认值（应为 `1.0`）。
2. 打开 `lmdeploy/pytorch/engine/logits_process.py:416-419`，确认它在 `__call__` 中位于温度处理之前。
3. （可选，需模型）用「示例代码」对比有无重复惩罚时的输出，找一个容易复读的 prompt：

```python
# 示例代码：观察 repetition_penalty 对复读的影响
from lmdeploy import pipeline, GenerationConfig

pipe = pipeline('Qwen/Qwen2.5-7B-Instruct')
prompt = '介绍一下中国的春节，多说一些。'

for rp in [1.0, 1.3]:
    gen = GenerationConfig(temperature=0.7, top_p=0.9,
                           repetition_penalty=rp, max_new_tokens=256)
    resp = pipe(prompt, gen_config=gen)
    print(f'--- repetition_penalty={rp} ---')
    print(resp.response)
```

需要观察的现象 / 预期结果：`repetition_penalty=1.3` 时，明显重复的短语会减少；`1.0`（默认）时输出更「原汁原味」但偶尔复读。**待本地验证**：不同模型对重复惩罚的敏感度差异较大。

源码阅读型结论（不依赖运行即可得出）：

- `repetition_penalty` **默认值 = 1.0**（无惩罚）。
- **作用阶段 = 采样前的 logits 预处理**，在 `FusedLogitsProcessor.__call__` 中、温度缩放之前、每步 Decode 执行一次。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `repetition_penalty` 对「正 logit」用除法、对「负 logit」用乘法，而不是统一用除法？

**答案**：为了保证「惩罚」在正负区间都朝「降低该 token 概率」的方向生效。当 `p>1`：正 logit 除以 `p` 会变小、负 logit 乘以 `p` 会变得更负（绝对值更大），二者都让该 token 的 softmax 概率下降。若统一用除法，负 logit 除以 `p` 反而会变大（更接近 0），那就变成「鼓励」而非「惩罚」了。

**练习 2**：`stop_words` 与 `bad_words` 的区别是什么？

**答案**：`stop_words` 触发「停止生成」（输出不含停止词），是终止条件；`bad_words` 触发「屏蔽」（采样前置 `-inf`，永远不会出现），是候选集约束。一个决定「什么时候停」，一个决定「不能出现什么」。

### 4.4 高级输出选项：n、logprobs、response_format 与自定义 logits_processors

#### 4.4.1 概念说明

除了采样本身，`GenerationConfig` 还有一组「输出形态」参数：

- **`n`**：对同一条输入生成多少条候选回答。文档注明**目前只支持 `n=1`**。
- **`logprobs`**：每个输出位置返回多少个 top 对数概率。配合 `Response.logprobs` 使用。
- **`response_format`**：约束输出格式（JSON schema / 正则），底层走引导解码（guided decoding）。
- **`logits_processors`**：用户自定义的 logits 处理函数列表，签名是 `(input_ids, logits) -> logits`，在每个位置被调用。
- **`output_logits` / `output_last_hidden_state` / `return_ppl`**：是否额外返回原始 logits、最后一层隐状态、输入困惑度（PyTorch 引擎对 `output_last_hidden_state` 给了 warning，表示不支持）。

> 重要澄清：`GenerationConfig` **没有 `best_of` 字段**。如果你从 OpenAI API 或 HuggingFace Transformers 习惯了 `best_of`，在 lmdeploy 里它并不存在；`n` 也只支持 1。不要假设它有。这是「不编造接口」原则下需要特别留意的一点。

#### 4.4.2 核心流程

以 `logprobs` 为例：

```text
每步 Decode 生成 token 时：
  if num_logprobs >= 0:
      记录该位置 top-(num_logprobs) 的 logprob（或 raw logits，取决于 logprobs_mode）
  → 最终随 Response.logprobs 返回（list[dict[token_id, prob]]）
```

`logits_processors` 的流程：

```text
scores（当前 logits）
  → 用户传入的每个 processor 依次改写 scores（基于 input_ids 与 scores）
  → 之后再走 repetition_penalty / temperature / 截断等内置处理
```

注意顺序：**自定义 processor 在 repetition_penalty 之前**（见 4.2.2 与 4.4.3 的源码引用），所以自定义 processor 看到的是「原始 logits」。

#### 4.4.3 源码精读

`n` 只支持 1 的说明：

[lmdeploy/messages.py:40-42](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L40-L42) —— 文档明确 `n` 目前**仅支持 1**。

`logprobs` / `response_format` / `logits_processors` 的字段定义与类型：

[lmdeploy/messages.py:132-134](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L132-L134) —— `logprobs: int = None`（None 表示不返回）、`response_format: dict | None = None`、`logits_processors: list[LogitsProcessor] | None = None`。`LogitsProcessor` 的类型签名定义在同文件：

[lmdeploy/messages.py:29-32](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L29-L32) —— `Callable[[torch.Tensor, torch.Tensor], torch.Tensor]`，即 `(input_ids, logits) -> logits`。

自定义 processor 在主流程里的调用位置：

[lmdeploy/pytorch/engine/logits_process.py:412-414](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/logits_process.py#L412-L414) —— `_apply_custom_logits_processors` 在 `repetition_penalty` 之前被调用，确认了 4.4.2 中「自定义 processor 先于内置惩罚」的顺序。

`logprobs` 默认值在引擎层的兜底（`None → -1` 表示关闭）：

[lmdeploy/pytorch/messages.py:216-218](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L216-L218) —— 当 `logprobs is None` 时记 `-1`，引擎据此跳过记录。

最后，看一个「哪些字段被实际用上」的权威样例——CLI `lmdeploy chat` 如何构建 `GenerationConfig`：

[lmdeploy/cli/chat.py:56-57](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/chat.py#L56-L57) —— `build_gen_config` 以 `GenerationConfig(do_sample=True, max_new_tokens=4096)` 起手，再把命令行参数按属性名覆盖上去。这里 `do_sample=True` 仅作语义占位（如 4.1.3 所述，引擎实际由 temperature/top_k 决定贪婪与否）。

#### 4.4.4 代码实践

实践目标：用一个自定义 `logits_processors` 强制模型永远不输出某个 token，验证「采样前改写 logits」的机制。

操作步骤：

1. 阅读上面 `LogitsProcessor` 的签名与调用顺序，确认它收到的是「原始 logits」。
2. 编写「示例代码」（需模型与 GPU）：

```python
# 示例代码：用 logits_processors 禁止某个 token（演示用，token_id 需按你的 tokenizer 改）
import torch
from lmdeploy import pipeline, GenerationConfig

pipe = pipeline('Qwen/Qwen2.5-7B-Instruct')
tok = pipe.tokenizer
ban_id = tok.encode('的', add_special_tokens=False)[-1]   # 想禁止的 token

def ban_token(input_ids, logits):
    logits[:, ban_id] = float('-inf')   # 把该 token 的 logit 置 -inf
    return logits

gen = GenerationConfig(temperature=0.7, top_p=0.9, logits_processors=[ban_token])
resp = pipe('介绍黑洞。', gen_config=gen)
print(resp.response)
```

需要观察的现象 / 预期结果：输出中应尽量不出现被禁止的 token（受分词影响可能仍有边界情况）。**待本地验证**：tokenizer 与模型不同，行为会有差异；无法运行时改为阅读 `_apply_custom_logits_processors` 的调用点，确认它先于 `repetition_penalty` 执行。

#### 4.4.5 小练习与答案

**练习 1**：想用 `GenerationConfig(n=3)` 一次拿到 3 条候选回答，可行吗？

**答案**：不可行。源码文档（`lmdeploy/messages.py:40-42`）明确 `n` 目前**仅支持 1**。要拿多条候选，需在应用层多次调用（并可用不同 `random_seed`）。

**练习 2**：`logits_processors` 里的自定义函数，与内置 `repetition_penalty` 谁先生效？

**答案**：自定义 processor 先生效。`FusedLogitsProcessor.__call__` 中 `_apply_custom_logits_processors`（第 412–414 行）在 `_process_repetition_penalty_`（第 416–419 行）之前调用。所以自定义 processor 看到的是未经重复惩罚的原始 logits。

### 4.5 QuantPolicy 枚举：KV cache 量化策略

#### 4.5.1 概念说明

`QuantPolicy` 描述的是 **KV cache（键值缓存）的量化策略**，而不是模型权重（weight）的量化。要先把两者分清：

- **权重量化**（`model_format=awq/gptq/fp8` 等）：把模型参数本身压成低精度，节省权重显存、提升计算密度。这是 `TurbomindEngineConfig.model_format` / `PytorchEngineConfig.model_format` 的事。
- **KV cache 量化**（`quant_policy`）：把推理过程中存下的 K、V 张量压成低精度，节省「上下文显存」，允许更大 batch 或更长上下文。这才是 `QuantPolicy` 的事。

`QuantPolicy` 是一个 `IntEnum`，每个取值对应一种 KV cache 量化精度。

#### 4.5.2 核心流程

用户在引擎配置里写一个整数（如 `quant_policy=8`），引擎配置的 `__post_init__` 会把它**转换成 `QuantPolicy` 枚举成员**并校验合法性。注意两套引擎对合法取值的要求不同：

- PyTorch 引擎：支持 `INT4(4)` / `INT8(8)` / `FP8(16)` / `FP8_E5M2(17)`，且**仅 CUDA 与 Ascend 设备**允许 KV 量化。
- TurboMind 引擎：**不支持 FP8**（`FP8` 与 `FP8_E5M2` 会被拒绝）。

#### 4.5.3 源码精读

`QuantPolicy` 枚举定义：

[lmdeploy/messages.py:20-27](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L20-L27) —— 各取值：`NONE=0`、`INT4=4`、`INT8=8`、`FP8=16`（float8_e4m3fn，per-tensor scale）、`FP8_E5M2=17`、`TURBO_QUANT=42`（K=4bit QJL4 + V=2bit MSE，一种混合策略）。因为继承 `IntEnum`，所以 `QuantPolicy(8)` 会解析成 `QuantPolicy.INT8`。

TurboMind 配置对 `quant_policy` 的转换与校验：

[lmdeploy/messages.py:322-329](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L322-L329) —— `TurbomindEngineConfig.__post_init__` 用 `QuantPolicy(self.quant_policy)` 转换（非法整数抛 `ValueError`），并 assert 它**不能是 `FP8` 或 `FP8_E5M2`**。

PyTorch 配置对 `quant_policy` 的转换与设备限制：

[lmdeploy/messages.py:494-512](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L494-L512) —— `PytorchEngineConfig.__post_init__` 同样把整数转成 `QuantPolicy`，并额外 assert：当 `quant_policy > 0` 时，`device_type` 必须是 `cuda` 或 `ascend`（即 KV cache 量化只在这两类设备上可用）。`PytorchEngineConfig.quant_policy` 的默认值是 `QuantPolicy.NONE`（第 453 行）。

#### 4.5.4 代码实践

实践目标：通过构造两套引擎配置，直观观察 `QuantPolicy` 的转换与校验差异。

操作步骤：

1. 阅读 `lmdeploy/messages.py:20-27` 的枚举定义，记住每个整数对应的策略。
2. 运行「示例代码」（不需模型，只验证 dataclass 校验）：

```python
# 示例代码：验证 QuantPolicy 在两套引擎配置中的合法取值
from lmdeploy import TurbomindEngineConfig, PytorchEngineConfig
from lmdeploy.messages import QuantPolicy

# PyTorch：FP8(16) 合法
pc = PytorchEngineConfig(quant_policy=16)
print('PyTorch quant_policy =', pc.quant_policy, '==', QuantPolicy.FP8)  # 预期 True

# TurboMind：FP8 非法（取消注释后 AssertionError）
# TurbomindEngineConfig(quant_policy=16)

# TurboMind：INT8(8) 合法
tc = TurbomindEngineConfig(quant_policy=8)
print('TurboMind quant_policy =', tc.quant_policy, '==', QuantPolicy.INT8)  # 预期 True

# PyTorch：非 cuda/ascend 设备上做 KV 量化非法（取消注释后 AssertionError）
# PytorchEngineConfig(quant_policy=8, device_type='maca')
```

需要观察的现象 / 预期结果：PyTorch 接受 `quant_policy=16`；TurboMind 接受 `8` 但拒绝 `16`；非 cuda/ascend 设备上 `quant_policy>0` 被拒。**待本地验证**：上述行为可直接从 `__post_init__` 源码推出，运行仅用于复核。

#### 4.5.5 小练习与答案

**练习 1**：`QuantPolicy` 量化的是权重还是 KV cache？

**答案**：是 **KV cache**（推理时缓存的 K/V 张量）。权重量化由 `model_format` 控制，二者是不同维度，可以同时开启（如 AWQ 权重 + INT8 KV cache）。

**练习 2**：为什么 `TurbomindEngineConfig(quant_policy=16)` 会失败，而 `PytorchEngineConfig(quant_policy=16)` 可以？

**答案**：TurboMind 的 `__post_init__` 显式 assert `quant_policy not in (FP8, FP8_E5M2)`（`lmdeploy/messages.py:326-329`），即 TurboMind 后端尚未实现 FP8 KV 量化；PyTorch 后端没有这条限制，故接受 `16`。

## 5. 综合实践

把本讲的知识串起来，完成一个「调参对比 + 源码反查」的小任务。

**任务背景**：你拿到一个对话模型，需要为它设计两组生成配置——一组用于「严肃问答」（要稳定、可复现、不复读），一组用于「创意写作」（要多样、有想象力、但不胡言乱语）。

**步骤**：

1. **从源码抄默认值**：打开 `lmdeploy/messages.py:115-123`，把 `temperature / top_p / top_k / min_p / repetition_penalty / max_new_tokens` 的默认值列成表。
2. **设计两组配置**（写出 `GenerationConfig(...)`）：
   - 严肃问答：考虑 `temperature=0`（贪婪，注意它会触发 `top_k=1` 的兜底，见 4.2.3）、`repetition_penalty=1.0`、合适的 `max_new_tokens`。
   - 创意写作：考虑 `temperature≈0.9~1.1`、`top_p≈0.85~0.95`、`repetition_penalty≈1.1`（避免复读但又不过度）。
3. **实现一个自定义 `logits_processors`**，在创意模式下额外禁止一个你不想要的 token（参考 4.4.4）。
4. **跑对比实验**（需模型与 GPU）：对同一个 prompt，用两组配置各跑一次，打印 `Response.text`、`Response.generate_token_len`、`Response.finish_reason`。
5. **源码反查**：实验后，对照 `lmdeploy/pytorch/engine/logits_process.py:371-445` 的 `FusedLogitsProcessor.__call__`，向自己解释一遍：你设的 `repetition_penalty`、`temperature`、`top_p` 分别在这段代码的哪一行生效、按什么顺序生效。

**预期结果**：你能用一张「参数 → 默认值 → 生效阶段（源码行号）」的表，把本讲所有采样参数对号入座。若无法运行模型，则把第 4 步替换为「画出 `__call__` 中各处理步骤的顺序图」作为纯源码阅读型交付。**待本地验证**：实际输出文本与你的设计是否吻合。

## 6. 本讲小结

- `GenerationConfig` 是用户面的 `@dataclass`，字段默认值即源码中可见的赋值（`temperature=0.8`、`top_p=1.0`、`top_k=50`、`repetition_penalty=1.0`、`max_new_tokens=512`），`__post_init__` 在构造时校验取值范围。
- PyTorch 引擎**不读取 `do_sample`**；真正的「贪婪 vs 采样」由 `SamplingParam.from_gen_config` 中的 `temperature==0 → top_k=1` 兜底逻辑决定。
- 截断类参数 `top_k` / `top_p` / `min_p` 分别对应 `_filter_topk_sorted_` / `_filter_topp_sorted_` / `_filter_minp_sorted_`，方向各异（前二者从高到低、`min_p` 设相对下限），且都在温度缩放之后执行。
- `repetition_penalty` 默认值 `1.0`（无惩罚），**作用阶段是采样前的 logits 预处理**（`FusedLogitsProcessor.__call__` 中第 416–419 行，温度处理之前），每步 Decode 执行一次；它对正负 logit 分别用除/乘以保证「惩罚」方向一致。
- 高级选项里，`n` 仅支持 1、**没有 `best_of` 字段**；`logits_processors`（签名 `(input_ids, logits) -> logits`）先于内置惩罚生效；`stop_words` 是停止条件、`bad_words` 是屏蔽约束。
- `QuantPolicy` 描述 **KV cache**（而非权重）量化；TurboMind 拒绝 FP8，PyTorch 接受且要求设备为 cuda/ascend。

## 7. 下一步学习建议

- 下一讲 **u2-l3（引擎配置 TurbomindEngineConfig 与 PytorchEngineConfig）** 会把本讲提到的 `quant_policy`、`cache_max_entry_count`、`tp`/`dp`、`enable_prefix_caching` 等引擎级字段讲透，与本讲是天然的「采样参数 vs 引擎资源参数」配对。
- 想看采样参数最终如何被消费，可顺着 `FusedLogitsProcessor.__call__` 继续往下读 `logits_process.py`（top_k/top_p/min_p 过滤与最终采样），这会自然过渡到 U4「PyTorch 引擎执行与调度」。
- 想了解 `response_format` 背后的引导解码，可在 `lmdeploy/serve/openai/` 与 `logits_process.py` 中搜索 `guided_decoding` / `xgrammar`，为后续 U8「服务部署」做铺垫。
- 建议同步浏览 `tests/test_lmdeploy/test_messages.py`（下一单元 u10-l3 会讲测试体系），看测试如何断言 `GenerationConfig` 的校验行为，以巩固本讲结论。
