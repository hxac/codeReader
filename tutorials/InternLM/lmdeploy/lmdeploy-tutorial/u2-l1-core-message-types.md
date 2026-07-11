# 核心消息与响应类型 messages.py

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `lmdeploy/messages.py` 这个「门面文件」里到底定义了哪些贯穿全项目的类型，以及它们的职责边界。
- 看懂 `GenerationConfig` 的每一个采样字段（`temperature` / `top_p` / `top_k` / `repetition_penalty` 等）的含义、默认值与校验规则，并知道它如何被转换成引擎内部的采样参数。
- 区分「引擎原始产出 `EngineOutput`」与「用户最终拿到的 `Response`」，理解二者之间的转换关系。
- 认识 `EngineEvent` / `EventType` 这套请求生命周期事件机制。
- 认识 `MessageStatus` 序列状态机，并准确指出它其实位于 `lmdeploy/pytorch/messages.py`（引擎面），而非本讲的用户面文件。

> 本讲承接 [u1-l4 pipeline 推理快速上手](u1-l4-pipeline-quickstart.md)：那里我们只把 `Response` 当作一个带五个字段（`text` / `generate_token_len` / `input_token_len` / `finish_reason` / `index`）的「黑盒返回值」来用。本讲把这个黑盒彻底打开。

## 2. 前置知识

### 2.1 dataclass 与 enum

LMDeploy 的消息类型大量使用 Python 标准库的两个工具：

- `@dataclass`：用「字段声明」自动生成 `__init__` / `__repr__`，让我们像填表一样构造对象。例如 `GenerationConfig(temperature=0.7)` 就是给一个 dataclass 的字段赋值。
- `enum.Enum` / `enum.IntEnum`：把一组「固定取值」收拢成有名字的枚举，避免到处写魔法数字或字符串。

### 2.2 三种最基本的采样策略

大模型每一步输出的是一个覆盖整个词表的「分数向量」logits。从 logits 选出下一个 token 的方式，就是采样策略：

- **贪心（greedy）**：直接取分数最大的 token，输出确定。
- **temperature 采样**：先把 logits 除以温度 \(T\) 再做 softmax：

  \[ p_i = \frac{\exp(z_i / T)}{\sum_j \exp(z_j / T)} \]

  \(T\) 越小，分布越尖锐（越倾向高分词，越「确定」）；\(T\) 越大，分布越平（越「随机」）。
- **top-k / top-p（nucleus）**：在采样前先裁剪候选集。top-k 只保留分数最高的 \(k\) 个词；top-p 保留累计概率达到 \(p\) 的最小候选集合：

  \[ V_p = \min\left\{ V : \sum_{i \in V} p_i \geq \text{top\_p} \right\} \]

  这些裁剪后再在剩余候选里归一化采样，避免采到长尾噪声词。

本讲不要求你背公式，只要能把这些名字和「它在控制生成结果哪一方面」对应起来即可。

### 2.3 Prefill / Decode 与会话（session）

承接 u1-l4：一次推理分 Prefill（处理输入 prompt）与 Decode（逐个生成 token）两阶段；多个请求可以共享一个 `session`（会话）以保留多轮历史。本讲提到的 `MessageStatus` 状态机，正是用来描述「一个序列当前处在请求生命周期的哪一步」。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `lmdeploy/messages.py` | **用户面/公共**消息类型，是整个项目的「公共词汇表」 | `GenerationConfig`、`Response`、`EngineEvent`、`ResponseType`、`EngineOutput`、`QuantPolicy` |
| `lmdeploy/pytorch/messages.py` | **PyTorch 引擎面**消息类型 | `MessageStatus`、`SchedulerSequence`、`SamplingParam` |
| `lmdeploy/__init__.py` | 顶层包导出 | 哪些类型是公开 API（`__all__`） |
| `lmdeploy/serve/core/async_engine.py` | serve 层把引擎产出转成用户结果 | `GenOut.to_response()` 转换点 |
| `tests/test_lmdeploy/test_messages.py` | 单元测试 | 如何构造并校验 `GenerationConfig` |

> ⚠️ **一个容易被忽略的事实**：项目里有两个 `messages.py`。一个是本讲的主角 `lmdeploy/messages.py`（用户面，所有后端共享）；另一个是 `lmdeploy/pytorch/messages.py`（PyTorch 引擎内部专用）。`MessageStatus` 和 `SchedulerSequence` 属于后者。规格里把它们一起讲，是因为它们在概念上同属「消息家族」，但**代码上确实分居两个文件**。本讲会明确标注，避免你在错误的文件里翻找。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：`GenerationConfig`（输入侧参数）、`Response` 与 `EngineOutput`（输出侧结果）、`EngineEvent`（生命周期事件）、`MessageStatus`（序列状态机）。

### 4.1 GenerationConfig：控制一次生成的全部参数

#### 4.1.1 概念说明

`GenerationConfig` 是「一次生成请求的全部控制参数」。无论你用 `pipeline(...)` 离线推理，还是用 `lmdeploy serve api_server` 起服务，最终影响「模型怎么生成」的开关都收敛到这个 dataclass。

它是一个**纯用户面**类型，定义在 `lmdeploy/messages.py`，并通过 `lmdeploy/__init__.py` 导出为公开 API：

[lmdeploy/__init__.py:L4](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/__init__.py#L4) — 从 `.messages` 导入 `GenerationConfig`，并在 `__all__` 中对外暴露，所以你能直接 `from lmdeploy import GenerationConfig`。

注意 `__all__` 里**只**导出了 `GenerationConfig` / `PytorchEngineConfig` / `TurbomindEngineConfig` / `VisionConfig` / `ChatTemplateConfig` / `Pipeline` / `Tokenizer` 等「构造/配置」类；像 `Response`、`EngineEvent`、`EngineOutput` 这些属于「引擎返回/内部」类型，并不在公开导出里——你通常只读取它们，而不主动 `new`。

#### 4.1.2 核心流程

一次生成中，`GenerationConfig` 的生命周期大致是：

```text
用户构造 GenerationConfig(temperature=..., top_p=..., ...)
        │
        ▼
__post_init__ 校验字段合法性（范围、正负、类型断言）
        │
        ▼
（可选）convert_stop_bad_words_to_ids：把 stop_words=['<|im_end|>'] 转成 token id
        │
        ▼
（可选）update_from_hf_gen_cfg：合并模型 generation_config.json 里的 eos_token_id
        │
        ▼
引擎内部：SamplingParam.from_gen_config(gen_config) 转成引擎真正使用的采样结构
```

要点：

1. **构造即校验**：`__post_init__` 会在对象创建时立刻检查 `temperature`、`top_p`、`top_k`、`min_p`、`n` 等是否合法，非法直接 `assert` 报错。
2. **停用词两条路径**：用户可以给字符串（`stop_words=['</s>']`）或 token id（`stop_token_ids=[2]`）；字符串需要借助 tokenizer 转成 id。
3. **`GenerationConfig` 不等于引擎采样参数**：它面向用户，引擎内部还会用 `SamplingParam.from_gen_config()` 再做一次「兜底」（例如 `temperature==0` 时强制退化为贪心）。这一转换在 `lmdeploy/pytorch/messages.py` 的 `SamplingParam` 里，属于引擎面，本讲只点到为止。

#### 4.1.3 源码精读

**类型定义与字段**：

[lmdeploy/messages.py:L35-L205](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L35-L205) — `GenerationConfig` 的完整定义。它用 `@dataclass`（注意不是 pydantic），字段都带默认值，所以 `GenerationConfig()` 就能拿到一份「全默认」配置。

关键字段（节选自 [L115-L152](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L115-L152)）：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `n` | `1` | 每个 prompt 生成几条候选（**目前只支持 1**，见 docstring） |
| `max_new_tokens` | `512` | 最多生成多少个 token |
| `do_sample` | `False` | 是否采样；`False` 即贪心 |
| `top_p` | `1.0` | nucleus 采样阈值，范围 `[0,1]` |
| `top_k` | `50` | 只在最高分的 k 个里采样 |
| `min_p` | `0.0` | 最小相对概率阈值，范围 `[0,1]` |
| `temperature` | `0.8` | 温度，范围 `[0,2]` |
| `repetition_penalty` | `1.0` | 重复惩罚，`>1` 抑制重复 |
| `ignore_eos` | `False` | 是否忽略结束符（用于压测） |
| `random_seed` | `None` | 采样随机种子，可复现 |
| `stop_words` / `stop_token_ids` | `None` | 命中即停止生成（字符串 / id 两种） |
| `bad_words` / `bad_token_ids` | `None` | 永不生成这些词 |
| `min_new_tokens` | `None` | 至少生成多少 token |
| `logprobs` | `None` | 每个位置返回 top-N 对数概率 |
| `response_format` | `None` | 约束输出格式（json_schema / regex_schema） |
| `return_ppl` | `False` | 返回输入 prompt 的困惑度（平均交叉熵） |
| `repetition_ngram_size` / `repetition_ngram_threshold` | `0` | n-gram 重复早停 |

**构造即校验 `__post_init__`**：

[lmdeploy/messages.py:L195-L205](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L195-L205) — 在 dataclass 自动生成的 `__init__` 之后被调用，做范围断言。注意最后一处：当 `repetition_ngram_size` 或 `repetition_ngram_threshold` 之一 `<= 0` 时，会把两者都强制置 `0`（即「关闭 n-gram 早停」），这一点有专门的测试保护（见 4.1.4）。

**停用词转 id**：

[lmdeploy/messages.py:L154-L174](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L154-L174) — `convert_stop_bad_words_to_ids(tokenizer)`：把字符串停用词经 `tokenizer.indexes_containing_token(word)` 查出所有命中 token id，再与用户直接给的 `stop_token_ids` 合并去重。这样引擎内部只关心「id 列表」一种形式。

**合并模型的 generation_config.json**：

[lmdeploy/messages.py:L176-L193](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L176-L193) — `update_from_hf_gen_cfg` 会把 tokenizer 的 `eos_token_id` 与模型自带 `generation_config.json` 里的 `eos_token_id`（可能是 int 或 list）都并入 `stop_token_ids`，确保模型定义的结束符能正确触发停止。

#### 4.1.4 代码实践

**实践目标**：亲手构造一个带采样参数的 `GenerationConfig`，打印其字段，并触发一次校验失败，直观感受 `__post_init__` 的作用。

**操作步骤**（无需 GPU，纯 CPU 即可运行）：

1. 编写下面这段「示例代码」（非项目原有，已标注）：

   ```python
   # 示例代码：构造并打印 GenerationConfig
   from lmdeploy import GenerationConfig

   # 用采样参数构造
   cfg = GenerationConfig(temperature=0.7, top_p=0.9, top_k=40,
                          max_new_tokens=128, repetition_penalty=1.05)
   # 打印几个关键字段
   for k in ['do_sample', 'temperature', 'top_p', 'top_k',
             'repetition_penalty', 'max_new_tokens', 'stop_token_ids']:
       print(f'{k} = {getattr(cfg, k)}')

   # 触发校验失败：top_p 必须在 [0, 1]
   try:
       GenerationConfig(top_p=1.5)
   except AssertionError:
       print('caught: top_p 越界被 __post_init__ 拒绝')
   ```

2. 对照真实测试 `tests/test_lmdeploy/test_messages.py` 阅读两个用例：
   - [tests/test_lmdeploy/test_messages.py:L10-L13](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_messages.py#L10-L13) 验证「负值的 n-gram 参数被钳到 0」。
   - [tests/test_lmdeploy/test_messages.py:L25-L32](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_messages.py#L25-L32) 验证 `convert_stop_bad_words_to_ids` 能把字符串停用词转成 id 列表。

**需要观察的现象**：

- 第一段打印里，`do_sample` 默认仍是 `False`（即便你给了 temperature/top_p，`do_sample` 不会自动变 `True`，是否采样取决于引擎层 `SamplingParam.from_gen_config` 的进一步判定，见 [lmdeploy/pytorch/messages.py:L185-L188](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L185-L188)）。
- `stop_token_ids` 默认是 `None`（你并没有设置停用词）。
- 构造 `GenerationConfig(top_p=1.5)` 会抛出 `AssertionError`。

**预期结果**：脚本能完整打印字段并在异常分支打印「caught: …」。

> 如果本地无法运行（缺依赖/无模型权重），明确写「待本地验证」，不要假装已执行。

#### 4.1.5 小练习与答案

**练习 1**：`GenerationConfig` 的默认 `temperature` 是多少？它的合法范围由哪段代码保证？
**答案**：默认 `0.8`；合法范围 `[0, 2]` 由 `__post_init__` 中的 `assert self.temperature >= 0 and self.temperature <= 2` 保证（[L200](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L200)）。

**练习 2**：用户给了 `stop_words=['<|im_end|>']` 但没给 `stop_token_ids`，引擎最终靠什么判定停止？
**答案**：靠 `convert_stop_bad_words_to_ids(tokenizer)` 把字符串转成 token id，再写入 `stop_token_ids`；引擎内部只读 `stop_token_ids`。

---

### 4.2 Response 与 EngineOutput：引擎产出与用户结果

#### 4.2.1 概念说明

LMDeploy 在「输出侧」有一组配套类型，按离用户的远近排列：

- **`EngineOutput`**（引擎面，离用户最远）：引擎每个迭代步产出的原始结果，带一个「状态枚举」`status: ResponseType`、新生成的 `token_ids`、可选 `logits` / `logprobs`、以及 PD 分离用的 `cache_block_ids`。
- **`Response`**（用户面，离用户最近）：最终交到你手里的结果对象，u1-l4 已经介绍过它的五个核心字段。
- 中间还有一个 serve 层的轻量结构 `GenOut`，负责把 `EngineOutput` 装配成 `Response`。

简单说：**`EngineOutput` 是「机器视角的每步产出」，`Response` 是「人视角的最终答案」。**

#### 4.2.2 核心流程

```text
引擎每一步 forward
      │  产出 EngineOutput(status=ResponseType.SUCCESS/FINISH/..., token_ids=[...])
      ▼
serve 层逐 step 收集（流式时不断 yield）
      │
      ▼
GenOut.to_response(index)  ──►  Response(text, generate_token_len, ...)
      │  （流式时多次产出）
      ▼
Response.extend(other) 把多段 Response 拼成完整结果
```

两个关键点：

1. **状态用枚举表达**：`ResponseType.SUCCESS` 表示「本步正常、还有后续」；`ResponseType.FINISH` 表示「生成结束」；还有 `INPUT_LENGTH_ERROR` / `SESSION_NOT_EXIST` / `CANCEL` 等错误/中断状态。
2. **流式拼接**：流式推理会产出一连串增量 `Response`，用 `Response.extend()` 不断把新片段接到旧片段上，最终合成完整文本。

#### 4.2.3 源码精读

**`ResponseType` 枚举**：

[lmdeploy/messages.py:L520-L533](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L520-L533) — 用 `enum.auto()` 自动编号，覆盖成功 / 完成 / 各类错误 / 取消等所有可能的「响应状态」。引擎在 `engine_instance.py` 里到处用它分支判断（例如 [lmdeploy/pytorch/engine/engine_instance.py:L229](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py#L229) 判断 `SUCCESS`、[L239](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py#L239) 判断 `FINISH/CANCEL`）。

**`Response` 字段**：

[lmdeploy/messages.py:L536-L565](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L536-L565) — 除了 u1-l4 讲过的 `text` / `generate_token_len` / `input_token_len` / `finish_reason` / `index`，还有：

| 字段 | 含义 |
| --- | --- |
| `token_ids` | 输出 token id 列表（默认空 list） |
| `logprobs` | 每个位置的 top 对数概率 |
| `logits` / `last_hidden_state` | 原始 logits / 最后一层隐藏态（需在 gen config 里显式开启） |
| `routed_experts` | MoE 路由专家信息（用于 RL router replay） |
| `cached_tokens` | 本次命中前缀缓存的 token 数 |

注意 `finish_reason` 的类型是 `Literal['stop', 'length'] | None`：要么 `'stop'`（自然停止 / 命中停用词），要么 `'length'`（达到 `max_new_tokens`），要么 `None`（尚未结束）。

**流式拼接 `extend`**：

[lmdeploy/messages.py:L597-L621](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L597-L621) — `extend(other)` 把另一个 `Response` 的内容并到自身：`text` 与 `token_ids`、`logprobs` 做**拼接**；而 `generate_token_len` / `input_token_len` / `finish_reason` / `index` 则**用新值覆盖**（因为这些是「当前步」的累计状态）。这就是 `stream_infer` 多段结果能拼回完整文本的底层机制。

**`EngineOutput` 字段**：

[lmdeploy/messages.py:L684-L707](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L684-L707) — 引擎每步产出。`status: ResponseType` 是它的「主键」之一；`cache_block_ids` 是 PD 分离场景下、prefill 完成后回传给 decode 节点的 KV 块信息；`req_metrics` 携带本请求的计时与事件（见 4.3）；`ce_loss` 在 `return_ppl=True` 时提供输入 prompt 的交叉熵。

**转换点 `GenOut.to_response`**：

[lmdeploy/serve/core/async_engine.py:L59-L75](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L59-L75) — serve 层的 `GenOut.to_response(index)` 把内部产出逐字段填进用户面的 `Response(...)`。这是「引擎面 → 用户面」最直接的衔接点（详细实现属于 U8 服务层，本讲只认准这个转换关系）。

#### 4.2.4 代码实践

**实践目标**：用 `Response.extend()` 模拟流式拼接，直观验证「文本拼接、长度覆盖」的行为。无需模型，纯对象操作。

**操作步骤**（示例代码）：

```python
# 示例代码：模拟流式 Response 的拼接
from lmdeploy.messages import Response

# 假装这是流式推理产出的三段增量结果
chunks = [
    Response(text='你好', generate_token_len=2, input_token_len=5, index=0),
    Response(text='，世界', generate_token_len=4, input_token_len=5, index=0),
    Response(text='！', generate_token_len=5, input_token_len=5,
             finish_reason='stop', index=0),
]

final = chunks[0]
for c in chunks[1:]:
    final.extend(c)

print(final.text)                 # 期望：你好，世界！
print(final.generate_token_len)   # 期望：5（被最后一段覆盖，而非累加成 11）
print(final.finish_reason)        # 期望：stop
```

**需要观察的现象**：`text` 是三段拼接的完整句子；而 `generate_token_len` 不是 `2+4+5=11`，而是最后一段的 `5`——印证 `extend` 对「计数类字段做覆盖」。

**预期结果**：打印 `你好，世界！`、`5`、`stop`。

> 这段只依赖 `lmdeploy.messages.Response`，不需要 GPU 或模型权重，本地装好 lmdeploy 的 Python 依赖即可运行。若环境缺 torch，则「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`Response.finish_reason` 有哪些可能取值？分别意味着什么？
**答案**：`'stop'`（自然结束或命中停用词）、`'length'`（达到 `max_new_tokens` 上限）、`None`（尚未结束，常出现于流式中间帧）。

**练习 2**：为什么 `Response.extend` 对 `text` 做拼接，却对 `generate_token_len` 做覆盖？
**答案**：`text` 是累积内容，需要把新片段接在旧内容后；而 `generate_token_len` 在流式每一帧里已经是「截至当前」的累计 token 数，新帧的值更准确，应直接覆盖（见 [L610-L612](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L610-L612)）。

---

### 4.3 EngineEvent：请求生命周期事件

#### 4.3.1 概念说明

`EngineEvent` 是「带时间戳的请求事件」。一个请求从进队、被调度、到可能被抢占，会经历若干关键节点；LMDeploy 把每个节点记成一个 `EngineEvent`，用于性能分析、可观测性和 metrics 上报。

它由两部分组成：

- `EventType`：事件类型枚举。
- `EngineEvent`：`(type, timestamp)` 二元组。

这套设计**借鉴自 vLLM**（源码注释里明确写了 `modified from vllm`），所以你若熟悉 vLLM 的请求生命周期事件，会感到亲切。

#### 4.3.2 核心流程

一个普通请求的事件序列大致是：

```text
请求入队                被调度执行              （可选）被抢占回等待队列
   │                       │                          │
   ▼                       ▼                          ▼
EventType.QUEUED    EventType.SCHEDULED        EventType.PREEMPTED
(记一个 EngineEvent)  (记一个 EngineEvent)       (记一个 EngineEvent)
   │                       │                          │
   └──────────────► 全部 append 到 SchedulerSequence.engine_events
                              │
                              ▼
                   汇入 RequestMetrics.engine_events，用于 metrics
```

事件被追加到 `SchedulerSequence.engine_events` 列表（见 4.4），并最终通过 `RequestMetrics` 暴露给监控体系（U10 会专门讲 metrics）。

#### 4.3.3 源码精读

**`EventType`**：

[lmdeploy/messages.py:L625-L635](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L625-L635) — 三个取值：

- `QUEUED = 1`：请求被引擎入队。
- `SCHEDULED = 2`：请求首次被调度执行。
- `PREEMPTED = 3`：为给其他请求腾资源，被「赶回」等待队列，将来会重新 prefill（注释标注 *currently ignored for simplicity*，表示该事件目前主要起记录作用）。

**`EngineEvent` 与工厂方法**：

[lmdeploy/messages.py:L639-L655](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L639-L655) — `EngineEvent` 只有 `type` 和 `timestamp` 两个字段；类方法 `new_event(event_type, timestamp=None)` 在 `timestamp` 为空时自动用 `time.time()` 取**墙上时钟时间（wall-clock）**。注释强调：**必须用 `time.time()` 而非单调时钟**，目的是和 C++ 侧（`std::chrono::system_clock`）的时间基准保持一致——这一点在 PD 分离、跨进程传递事件时间戳时非常关键。

**请求级指标容器 `RequestMetrics`**：

[lmdeploy/messages.py:L670-L681](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L670-L681) — 把单个请求的事件列表 `engine_events`、token 生成时间戳 `token_timestamp`、投机解码信息 `spec_info`、命中缓存数 `cached_tokens` 打包，作为 `EngineOutput.req_metrics` 的类型。

#### 4.3.4 代码实践

**实践目标**：构造事件并观察时间戳来源；同时定位「事件被记录到哪里」的真实调用点。

**操作步骤**（示例代码 + 源码阅读）：

1. 示例代码：

   ```python
   # 示例代码：构造 EngineEvent
   from lmdeploy.messages import EngineEvent, EventType

   e = EngineEvent.new_event(EventType.QUEUED)
   print(e.type, e.timestamp)        # 期望：EventType.QUEUED <一个浮点时间戳>
   ```

2. 源码阅读：在 `lmdeploy/pytorch/messages.py` 中定位「记录事件」的调用——`SchedulerSequence.record_event` 在 [lmdeploy/pytorch/messages.py:L1003-L1008](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L1003-L1008) 把 `EngineEvent.new_event(...)` 追加进 `self.engine_events`；而「请求刚入队」时调用 `record_event(EventType.QUEUED)` 的位置在 [lmdeploy/pytorch/messages.py:L395](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L395)（`SchedulerSession.add_sequence` 末尾）。

**需要观察的现象**：`e.timestamp` 是一个 `float`（`time.time()` 的返回值），与 `time.time()` 量级一致。

**预期结果**：打印形如 `EventType.QUEUED 1720000000.123`。

> 若本地无 lmdeploy 运行环境，构造 `EngineEvent.new_event` 仍可纯 CPU 运行（仅依赖标准库 `time`）；但若连 `lmdeploy` 都 import 失败，则「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`EngineEvent.new_event` 为什么坚持用 `time.time()`（墙上时钟）而不是 `time.monotonic()`（单调时钟）？
**答案**：因为事件时间戳要和 C++ 侧（`std::chrono::system_clock`，即墙上时钟）对齐，跨进程（如 PD 分离）传递时才能比较；单调时钟只在单进程内有效，跨进程无意义（见 [L651-L654](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L651-L654) 注释）。

**练习 2**：`EventType.PREEMPTED` 描述什么场景？代码注释对它有什么说明？
**答案**：请求为给别的请求腾资源被赶回等待队列、将来重新 prefill；注释标注 `FIXME, currently ignored for simplicity`，表示目前主要起记录作用、调度上暂未完整利用（[L635](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L635)）。

---

### 4.4 MessageStatus：序列状态机

#### 4.4.1 概念说明

`MessageStatus` 是「一条序列（sequence）当前处于哪个生命周期阶段」的状态枚举。它是 LMDeploy 持续批处理（continuous batching）调度的核心状态机：调度器（scheduler）每一步都依据序列的状态，决定它该被 prefill、decode、还是等待。

> ⚠️ **定位提示**：`MessageStatus` **不在** `lmdeploy/messages.py`，而在 [`lmdeploy/pytorch/messages.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py)。原因是：它纯粹是 **PyTorch 引擎内部**的调度概念，用户面代码几乎不会直接接触。本讲把它纳入，是因为它和 `EngineEvent` 一起描述了「请求从进来到结束」的完整生命周期，概念上属于「消息家族」。

#### 4.4.2 核心流程

普通推理路径的状态流转：

```text
新请求进入
   │  SchedulerSession.add_sequence(...)
   ▼
MessageStatus.WAITING   ──►  调度器选中做 prefill  ──►  MessageStatus.RUNNING
                                                          │  逐 token decode
                                                          ▼
                                              生成结束：MessageStatus.STOPPED
```

其中还有一个 `READY` 中间态用于调度准备。PD 分离（prefill/decode 分离部署，见 U9）额外引入一组 `MIGRATION_*` 状态，用于在 prefill 节点与 decode 节点之间迁移请求。

`SequenceManager` 维护一个 `dict[MessageStatus, SeqMap]`（按状态分桶存放序列），调度器通过「取某个状态桶里的序列」来决定本步处理谁——状态切换就是「把序列从一个桶搬到另一个桶」。

#### 4.4.3 源码精读

**`MessageStatus` 枚举**：

[lmdeploy/pytorch/messages.py:L247-L264](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L247-L264) — 取值如下：

| 状态 | 含义 |
| --- | --- |
| `WAITING` | 在等待队列里，尚未被调度 |
| `READY` | 已就绪，等待进入执行 |
| `STOPPED` | 生成结束，序列终止 |
| `RUNNING` | 正在被执行（decode 中） |
| `TO_BE_MIGRATED` | PD 分离：待迁移（prefill 端） |
| `MIGRATION_WAITING` | 未迁移请求在 prefill/decode 两端的等待态 |
| `MIGRATION_READY` | decode 端「正在迁移」的请求就绪态 |
| `MIGRATION_RUNNING` | 迁移执行中 |
| `MIGRATION_DONE` | 迁移完成 |

前四个（`WAITING/READY/STOPPED/RUNNING`）是普通推理会用到的核心状态；后五个 `MIGRATION_*` 专用于 PD 分离。

**入队时如何设定初态**：

[lmdeploy/pytorch/messages.py:L390-L391](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L390-L391) — 在 `SchedulerSession.add_sequence` 里：普通请求设为 `WAITING`；带 `migration_request` 的 PD 分离请求设为 `MIGRATION_WAITING`。这一行决定了序列的「出生状态」。

**状态分桶管理**：

[lmdeploy/pytorch/messages.py:L279-L333](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L279-L333) — `SequenceManager` 用 `_status_seq_map: dict[MessageStatus, SeqMap]` 把序列按状态分桶；`update_sequence_status(seq, new_status)` 就是「从旧桶删除、加入新桶」（[L322-L333](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L322-L333)）。

**承载状态的 `SchedulerSequence`**：

[lmdeploy/pytorch/messages.py:L704-L752](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L704-L752) — `SchedulerSequence` 是引擎内一条序列的完整载体：`history_cache`（历史 token）、`sampling_param`、`logical_blocks`（逻辑 KV 块）、`engine_events`（事件列表）等都在它身上。它的 `status` 属性（[L879-L881](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L879-L881)）委托给内部 `state.status`，是「状态机当前值」的统一读取入口。调度器的具体调度逻辑（如何依据这些状态选 batch）属于 U4，本讲不展开。

#### 4.4.4 代码实践

**实践目标**：列出 `MessageStatus` 的全部取值（注意它在 `pytorch/messages.py`，不是用户面那个文件）。

**操作步骤**：

1. 运行下面「示例代码」（无需模型、无需 GPU）：

   ```python
   # 示例代码：列出 MessageStatus 全部取值
   # 注意：导入路径是 lmdeploy.pytorch.messages，而不是 lmdeploy.messages
   from lmdeploy.pytorch.messages import MessageStatus

   for s in MessageStatus:
       print(s.name, '=', s.value)
   ```

2. 对照 [lmdeploy/pytorch/messages.py:L247-L264](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L247-L264) 的源码，确认你打印出的名字与源码里的枚举成员一一对应。

**需要观察的现象**：打印出 9 个成员（`WAITING/READY/STOPPED/RUNNING` + 5 个 `MIGRATION_*`），每个 `value` 是 `enum.auto()` 自动分配的整数。

**预期结果**：9 行形如 `WAITING = 1`、`READY = 2` … 的输出。

> 若 `import lmdeploy.pytorch.messages` 失败（例如只装了 TurboMind、缺少 PyTorch 依赖），则改为直接阅读上面那段源码确认取值，并标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`MessageStatus` 定义在哪个文件？为什么不在 `lmdeploy/messages.py`？
**答案**：定义在 `lmdeploy/pytorch/messages.py`。因为它是 PyTorch 引擎内部的调度状态概念，属于引擎面，而 `lmdeploy/messages.py` 是面向用户的公共类型层（[L247-L264](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L247-L264)）。

**练习 2**：新请求被 `add_sequence` 加入后，初始状态是什么？由哪一行决定？
**答案**：普通请求初始为 `WAITING`，带 `migration_request` 的 PD 分离请求为 `MIGRATION_WAITING`；由 [L390-L391](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L390-L391) 决定。

---

## 5. 综合实践

**任务**：把本讲四个最小模块串成一条「请求从输入参数到状态变迁再到产出结果」的认知链。

请完成下面三步，并整理成一份笔记：

1. **输入侧**：构造一个 `GenerationConfig(temperature=0.6, top_p=0.85, top_k=30, max_new_tokens=64, stop_words=['<|im_end|>'])`，调用 `convert_stop_bad_words_to_ids` 的逻辑（若无 tokenizer，则手动给 `stop_token_ids=[151645]`），打印所有非 `None` 字段。

2. **生命周期**：在源码里画一条「请求事件 + 状态」时间线，要求至少包含三个节点，并各给一行源码引用：
   - 请求入队：`EventType.QUEUED` 被记录（[pytorch/messages.py:L395](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L395)） + 初态 `WAITING`（[L390](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L390)）。
   - 被调度执行：`EventType.SCHEDULED`（事件类型定义见 [messages.py:L634](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L634)），状态进入 `RUNNING`。
   - 产出结果：引擎每步产出 `EngineOutput`（[messages.py:L684-L707](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L684-L707)），最终经 `GenOut.to_response`（[async_engine.py:L59-L75](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L59-L75)）变成用户面的 `Response`。

3. **输出侧**：用 4.2.4 的 `Response.extend` 示例，手工模拟「3 段流式输出」，验证最终 `text` 拼接正确、`generate_token_len` 为最后一段的累计值。

**交付物**：一份包含「字段表 + 时间线图 + 拼接运行结果」的简短笔记。若某步无法在本地执行，明确标注「待本地验证」，不要伪造运行结果。

## 6. 本讲小结

- `lmdeploy/messages.py` 是**用户面公共词汇表**：`GenerationConfig`（生成参数）、`Response`（用户结果）、`EngineOutput`（引擎每步产出）、`EngineEvent`/`EventType`（生命周期事件）、`ResponseType`（响应状态枚举）、`QuantPolicy`（KV cache 量化策略）都定义在这里。
- `GenerationConfig` 用 `@dataclass` + `__post_init__` 做「构造即校验」，提供 temperature/top_p/top_k/min_p/repetition_penalty/停用词/logprobs/response_format 等全套采样与输出控制开关。
- **输出侧分两层**：`EngineOutput`（带 `ResponseType` 状态的引擎每步产出）→ serve 层 `GenOut.to_response()` → 用户面的 `Response`；流式时多段 `Response` 经 `extend()` 拼回完整结果（文本拼接、计数覆盖）。
- `EngineEvent = (EventType, timestamp)` 记录请求的 QUEUED/SCHEDULED/PREEMPTED 节点，时间戳必须用墙上时钟 `time.time()` 以便与 C++ 侧对齐。
- `MessageStatus` 是序列状态机（WAITING/READY/RUNNING/STOPPED + 5 个 PD 分离迁移态），但它**位于 `lmdeploy/pytorch/messages.py`**，是 PyTorch 引擎面概念，由 `SequenceManager` 按状态分桶管理。
- 区分「两个 messages.py」是后续阅读源码的关键：用户面 `lmdeploy/messages.py` 与引擎面 `lmdeploy/pytorch/messages.py` 各司其职。

## 7. 下一步学习建议

- 想深入采样参数的每一个取值与运行时行为，继续本单元的 **[u2-l2 生成配置 GenerationConfig 详解](u2-l2-generation-config.md)**，它会更细致地拆解 `n` / `best_of` / `logprobs` / `repetition_penalty` 等。
- 想了解引擎配置（`tp` / `cache_max_entry_count` / `session_len` 等）如何影响显存与并发，进入 **[u2-l3 引擎配置](u2-l3-engine-configs.md)**，那里会对比 `TurbomindEngineConfig` 与 `PytorchEngineConfig`。
- 当你之后学 U4 调度器时，会再次遇到本讲的 `MessageStatus` 与 `SchedulerSequence`——届时可回看本讲 4.4，作为「状态机视角」的铺垫。
- 若对输出侧转换（`EngineOutput` → `Response`）的完整链路感兴趣，U8 服务层（`serve/core/async_engine.py`）会展开 `GenOut.to_response` 的全部细节。
