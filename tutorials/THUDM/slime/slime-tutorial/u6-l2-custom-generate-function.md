# 自定义生成函数（custom-generate）

## 1. 本讲目标

学完本讲后，你应该能够：

- 准确说出 slime「自定义生成函数」接口 `--custom-generate-function-path` 的函数签名、挂载点（在默认 rollout 循环的哪一步被调用）和返回值契约。
- 理解多轮工具调用场景里 `loss_mask` 的标注规则：哪些 token 算「模型生成的、要训练」（`1`），哪些是「环境/工具返回的、不训练」（`0`），以及为什么必须用 SGLang 原始 token 而不是重新分词。
- 读懂 search-r1 这个真实示例，掌握「模型生成动作 → 解析 → 调工具 → 拼观测 → 标 loss_mask」的完整循环，并处理 abort/length/stop 三种终止态。
- 掌握「一次 rollout 拆出多个可训练样本」的 fan-out 写法，以及兄弟样本必须共享同一个 `rollout_id` 的同源约束。

本讲是 U6 定制化系列的第二讲，承接 [u6-l1](u6-l1-customization-overview.md) 建立的「21+ 个 function-path hook」全景，聚焦其中最常用、也最适合接入智能体工作流的那个接口。

## 2. 前置知识

本讲假设你已经掌握以下概念（若不熟悉，请先读对应讲义）：

- **Sample 数据载体**（[u3-l1](u3-l1-sample-data-structure.md)）：slime 用一个 `Sample` dataclass 贯穿 rollout 与 training。你需要记得它的几个关键字段：`prompt`（提示词）、`tokens`（完整 token 序列）、`response`（已生成的回复文本）、`response_length`（回复 token 数）、`loss_mask`（与回复等长的 0/1 数组）、`rollout_log_probs`（行为策略对数概率）、`reward`、`status`。
- **默认 rollout 主循环 generate_rollout**（[u3-l2](u3-l2-default-rollout-flow.md)）：slime 默认的 rollout 函数是一条「同步外壳 + 异步内核」的流水线，内部三层 `generate → generate_and_rm → generate_and_rm_group`。本讲的 `custom_generate` 就是替换最底层那个 `generate`。
- **function-path 与 load_function**（[u6-l1](u6-l1-customization-overview.md)）：所有 `--xxx-path` 参数的值都是 import 路径字符串，运行时由 `load_function` 解析成函数对象。
- **SGLang 的 `/generate` HTTP 接口**：rollout 引擎本质是一个 SGLang 服务，slime 通过 HTTP POST `/generate` 把 `text`（或 `input_ids`）+ `sampling_params` 发给它，拿回 `text`、`meta_info`（含 `finish_reason`、可选的 `output_token_logprobs`）。

一句话定位：**`custom_generate` 是给「单个 prompt」插自定义生成逻辑的工位**，它不碰外层的数据调度、奖励计算、动态过滤——那些仍由 slime 默认的 `generate_rollout` 负责。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| :--- | :--- |
| `slime/rollout/sglang_rollout.py` | 默认 rollout 实现。`generate_and_rm` 是 `custom_generate` 的**调用点**；`GenerateState` 是生成过程的单例状态（提供 tokenizer、信号量等）；内置 `generate` 是被替换的默认实现。 |
| `slime/utils/types.py` | `Sample` dataclass 与 `append_response_tokens` 方法——标注 loss_mask 的真正落点。 |
| `slime/utils/misc.py` | `load_function`——把 import 路径字符串解析成函数对象。 |
| `examples/search-r1/generate_with_search.py` | **本讲主角**：一个真实的多轮检索 custom_generate 实现，以及配套的 `reward_func`。 |
| `docs/en/get_started/customization.md` | 接口契约的权威说明，包含 fan-out 返回 `list[Sample]` 的规范。 |
| `docs/en/get_started/quick_start.md` | 「Multiturn Adaptation」章节，多轮适配的三步法。 |

## 4. 核心概念与源码讲解

### 4.1 custom_generate 接口契约：签名、挂载点与返回值

#### 4.1.1 概念说明

slime 把整条 rollout 流水线（取数据 → 生成 → 算奖励 → 过滤 → 凑批量）写成固定的骨架，只把其中「对单个样本做生成」这一格做成可替换的「工位」。这个工位就是 custom_generate。

为什么需要它？默认的 `generate` 是**单轮、一次性**的：把 prompt 丢给 SGLang，拿回一段回复，结束。但很多任务（RAG 检索、工具调用、沙箱执行、浏览器操作）需要**多轮交互**：模型输出一个动作 → 外部环境（检索引擎/代码解释器/浏览器）返回观测 → 模型基于观测继续输出。这种「对话式」的生成无法用单次 HTTP 调用表达，必须自己写循环。

custom_generate 让你只改这一格，而无需重写整条流水线（那是更重的 `--rollout-function-path`，见 u6-l1 的层级关系）。**经验法则：智能体场景优先用 `custom_generate` + `custom_rm`，只有当默认外循环不够用时才换整个 rollout 函数。**

#### 4.1.2 核心流程

custom_generate 在默认 rollout 中的位置（伪代码）：

```text
generate_and_rm_group(整组 n 个采样)
 └─ 对每个 sample 并发调用 generate_and_rm
     └─ 获取信号量 + dp_rank 上下文
         ├─ 若 args.custom_generate_function_path 不为空
         │     → load_function(路径) 解析出函数对象
         │     → 按签名决定是否透传 evaluation 参数
         │     → await custom_generate_func(args, sample, sampling_params[, evaluation])
         └─ 否则
               → await generate(args, sample, sampling_params)   # 默认单轮生成
     └─ apply_rollout_sample_hooks(...)        # 样本钩子
     └─ 若 group_rm=False 且 sample.reward is None
           → async_rm(args, sample)            # 奖励由框架算，custom_generate 不必管
```

要点：

1. **custom_generate 只负责「生成」**，奖励计算由框架在它返回后统一进行（除非你在里面手动把 `sample.reward` 填好——框架会跳过重复算分）。
2. **它运行在信号量保护下**，且自动获得一个 `dp_rank`（数据并行 rank）上下文，所以你不必自己加并发控制。
3. **它是 `async` 协程**，因为内部要 `await` HTTP 调用。

#### 4.1.3 源码精读

先看权威契约（customization 文档）：

[docs/en/get_started/customization.md:L71-L80](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L71-L80) —— 接口定义。`--custom-generate-function-path` 默认 `None`（用内置 generate），作用是「只覆盖默认 rollout 函数里的生成步骤」，签名是：

```python
async def custom_generate(args, sample: Sample, sampling_params: dict) -> Sample | list[Sample]
```

返回值既可以是单个 `Sample`，也可以是 `list[Sample]`（后者用于 fan-out，见 4.4）。

再看真正的调用点 `generate_and_rm`：

[slime/rollout/sglang_rollout.py:L248-L260](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L248-L260) —— 这是 custom_generate 的挂载点。关键逻辑：

```python
custom_func_path = getattr(sample, "generate_function_path", None) or args.custom_generate_function_path
if custom_func_path is not None:
    custom_generate_func = load_function(custom_func_path)
    # if signature has evaluation, pass evaluation
    if "evaluation" in inspect.signature(custom_generate_func).parameters:
        sample = await custom_generate_func(args, sample, sampling_params, evaluation=evaluation)
    else:
        sample = await custom_generate_func(args, sample, sampling_params)
else:
    sample = await generate(args, sample, sampling_params)
```

两个值得注意的细节：

- **逐样本优先级**：`sample.generate_function_path`（来自 eval 数据集配置）会压过全局的 `args.custom_generate_function_path`。这意味着你可以让不同数据集走不同的生成函数。
- **可选的 `evaluation` 形参**：如果你的函数签名里写了 `evaluation` 参数，框架会自动把「当前是不是评估阶段」透传进来，方便训练/评估走不同分支。

最后看 `load_function` 本身，理解「字符串 → 函数」是怎么发生的：

[slime/utils/misc.py:L39-L47](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/misc.py#L39-L47) —— 用 `rpartition(".")` 切最后一个点，前半段当模块 import，后半段当属性取出来。所以你传的路径必须是「模块能 import 到 + 函数名」，例如 `generate_with_search.generate`。

#### 4.1.4 代码实践

实践目标：不跑训练，纯用 Python 验证「字符串路径 → 函数对象」的解析，并检查签名，建立对契约的直觉。

操作步骤（待本地验证，需要可 import 的 slime 环境）：

1. 在仓库根目录启动 Python，确认能 import。
2. 用 `load_function` 解析一个内置函数，检查它确实是 coroutine function。
3. 用 `inspect` 模拟框架的「是否带 evaluation 形参」判定。

```python
# 示例代码：验证 load_function 与签名判定（不依赖 GPU）
import inspect
from slime.utils.misc import load_function

# 1. 解析默认 rollout 函数（它是普通函数，内部用 run(coro) 驱动协程）
fn = load_function("slime.rollout.sglang_rollout.generate_rollout")
print("generate_rollout:", fn)

# 2. 模拟框架判定：函数是否声明了 evaluation 形参
has_eval = "evaluation" in inspect.signature(fn).parameters
print("has evaluation param?", has_eval)

# 3. 解析 search-r1 的 generate（它是 async def）
gen = load_function("generate_with_search.generate")  # 需先把 examples/search-r1 放进 PYTHONPATH
print("is coroutine function?", inspect.iscoroutinefunction(gen))
```

预期结果：第 2 步对 `generate_rollout` 输出 `False`（它没有 evaluation 形参，靠自己的 `evaluation` 入参处理）；第 3 步对 search-r1 的 `generate` 输出 `True`。

#### 4.1.5 小练习与答案

**练习 1**：如果你想让训练和评估使用不同的生成逻辑（比如评估时不调用真实搜索引擎），有哪两种实现方式？

**参考答案**：① 在函数签名里加 `evaluation` 形参，框架会自动透传，函数内部据此分支；② 在 eval 数据集配置里设 `custom_generate_function_path`，让该数据集走完全不同的函数（逐样本优先级压过全局 `args.custom_generate_function_path`）。

**练习 2**：为什么 custom_generate 里不必自己写 `asyncio.Semaphore` 来限制对 SGLang 的并发？

**参考答案**：因为调用点 `generate_and_rm` 已经把它包在 `state.semaphore` 与 `state.dp_rank_context()` 里（见挂载点源码），并发控制由框架统一负责，函数只管「对一个样本生成」。

---

### 4.2 loss mask 标注：区分可训练 token 与环境 token

#### 4.2.1 概念说明

`loss_mask` 是一个与「回复部分」等长的 0/1 数组，长度等于 `response_length`（注意：不含 prompt 部分，prompt 在训练端单独处理）。它的作用是告诉训练阶段：**哪些 token 该计算策略梯度（1），哪些该被忽略（0）。**

在单轮任务里，整个回复都是模型生成的，所以 `loss_mask` 全是 1。但在多轮工具调用里，回复序列里混杂了两类 token：

- **模型生成的 token**（思考、`<search>query</search>`、`<answer>...</answer>` 等）→ `loss_mask = 1`，参与梯度。
- **环境/工具返回的 token**（检索结果 `<information>...</information>`、API 返回、沙箱输出）→ `loss_mask = 0`，**不参与梯度**。

为什么环境 token 不能训练？因为 RL 优化的是「策略（模型）给出动作的概率」，环境观测不是模型产生的，模型对它「负责」没有意义；如果把工具返回的内容也当成目标去最大化，反而会教模型去模仿检索结果，破坏学习信号。

#### 4.2.2 核心流程

标注 loss_mask 有两种等价写法，关键在「逐段 append」：

```text
对每一轮：
  1. 模型生成一段 → 追加 token，loss_mask 追加 [1]*len(段)   # trainable=True
  2. 工具返回一段 → 追加 token，loss_mask 追加 [0]*len(段)   # trainable=False
最后 sample.loss_mask 应与 response_length 等长。
```

一个**致命的注意点**：当你需要收集对数概率（用于 off-policy 修正 / TIS，见 u6-l5）时，**绝不能对 SGLang 返回的字符串做任何后处理再重新分词**。因为重新分词可能产生与引擎实际采样不同的 token，导致 token 与 logp 对不齐。search-r1 的注释专门强调了这一点（见 4.3.3）。

#### 4.2.3 源码精读

标注的真正落点是 `Sample.append_response_tokens`，它的 `trainable` 参数就是 loss_mask 的开关：

[slime/utils/types.py:L253-L292](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L253-L292) —— 关键约束（节选）：

```python
if tokens and trainable and log_probs is None:
    raise ValueError("trainable response tokens require rollout log probabilities.")
if tokens and not trainable:
    if log_probs is not None:
        raise ValueError("non-trainable response tokens should not pass rollout log probabilities.")
    log_probs = [0.0] * len(tokens)
...
self.loss_mask += [1 if trainable else 0] * len(tokens)
```

这几行暗含了 slime 的两条硬约束：

1. **trainable=True 的 token 必须带 log_probs**——可训练 token 必须有行为策略对数概率（供重要性采样）。
2. **trainable=False 的 token 不许带 log_probs**——框架会自动填 0.0 占位，保证数组长度对齐。

也就是说，`append_response_tokens` 帮你同时维护 `tokens`、`response_length`、`loss_mask`、`rollout_log_probs` 四个数组的一致性。你只要正确传 `trainable`，就不必手动拼四个列表。

再看 search-r1 里两种调用的对照（先看模型 token、再看观测 token）：

[examples/search-r1/generate_with_search.py:L219-L226](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L219-L226) —— 模型生成段，`trainable=True`，带 log_probs：

```python
loss_mask += [1] * len(cur_response_token_ids)
sample.append_response_tokens(
    args,
    tokens=cur_response_token_ids,
    log_probs=cur_response_log_probs if SEARCH_R1_CONFIGS["return_logprob"] else None,
    trainable=True,
    meta_info=output["meta_info"] if "output_token_logprobs" in output["meta_info"] else None,
)
```

[examples/search-r1/generate_with_search.py:L240-L244](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L240-L244) —— 工具观测段，`trainable=False`，不带 log_probs：

```python
obs_tokens_ids = state.tokenizer(next_obs, add_special_tokens=False)["input_ids"]
...
loss_mask += [0] * len(obs_tokens_ids)
sample.append_response_tokens(args, tokens=obs_tokens_ids, trainable=False)
```

注意 search-r1 同时维护了一个「裸的」`loss_mask` 列表和通过 `append_response_tokens` 维护的内部状态——最终在函数末尾用裸列表覆盖 `sample.loss_mask`（见 4.3.3）。两种写法二选一即可，关键是最终 `loss_mask` 与 `response_length` 等长且取值正确。

#### 4.2.4 代码实践

实践目标：在纯 Python 里复现 loss_mask 标注，验证长度与取值。

```python
# 示例代码：手动模拟两轮交互的 loss_mask（不调用真实模型）
from slime.utils.types import Sample

# 假设 prompt 是 3 个 token，模型第一轮生成 4 个，工具返回 5 个，模型第二轮生成 2 个
prompt_ids = [10, 11, 12]
gen1 = [20, 21, 22, 23]     # 模型生成 -> trainable
obs1 = [30, 31, 32, 33, 34] # 工具返回 -> 非 trainable
gen2 = [40, 41]             # 模型生成 -> trainable

s = Sample(prompt="demo", tokens=list(prompt_ids), loss_mask=[])
s.append_response_tokens(tokens=gen1, log_probs=[-0.1]*4, trainable=True)
s.append_response_tokens(tokens=obs1, trainable=False)
s.append_response_tokens(tokens=gen2, log_probs=[-0.2]*2, trainable=True)

assert s.response_length == 4 + 5 + 2 == 11
assert s.loss_mask == [1,1,1,1, 0,0,0,0,0, 1,1]
assert s.rollout_log_probs == [-0.1]*4 + [0.0]*5 + [-0.2]*2
print("OK:", s.loss_mask)
```

需要观察的现象：`loss_mask` 中环境段恰为 0；`rollout_log_probs` 在非训练段被填 0 占位且长度对齐。

预期结果：打印 `[1, 1, 1, 1, 0, 0, 0, 0, 0, 1, 1]`，三个 assert 全部通过。待本地验证（需 slime 可 import；若 `append_response_tokens` 因 args 相关逻辑报错，可只验证裸列表拼装：`[1]*4 + [0]*5 + [1]*2`）。

#### 4.2.5 小练习与答案

**练习 1**：如果误把工具返回的 token 标成 `trainable=True` 但没给 log_probs，会发生什么？

**参考答案**：`append_response_tokens` 会直接抛 `ValueError("trainable response tokens require rollout log probabilities.")`，因为可训练 token 必须有对数概率。这是 slime 防御性的硬约束。

**练习 2**：为什么开 TIS（重要性采样）时，search-r1 要禁用 `postprocess_responses` 对字符串的后处理？

**参考答案**：后处理会改字符串，重新分词可能得到与引擎实际采样不同的 token，导致 token 与 logp 不再一一对应，破坏 TIS 所需的 `rollout_log_probs` 对齐。开 TIS 时只能直接用 SGLang 返回的原始 `output_token_logprobs` 里的 token id 与 logp。

---

### 4.3 search-r1 实战：多轮检索生成的完整实现

#### 4.3.1 概念说明

search-r1 是一个经典的「检索增强 + 工具调用」RL 任务：模型可以输出 `<search>查询词</search>` 触发一次检索，引擎把检索结果包成 `<information>...</information>` 拼回上下文，模型继续推理；最终用 `<answer>答案</answer>` 收尾。slime 的 `examples/search-r1/` 是它的最小复现，也是多轮 custom_generate 的官方范例。

它的核心是 [examples/search-r1/generate_with_search.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py) 里的 `generate` 函数，配套一个 `reward_func`（奖励）。两者通过启动脚本的 `--custom-generate-function-path` 与 `--custom-rm-path` 注入。

#### 4.3.2 核心流程

search-r1 的 `generate` 是一个最多 `max_turns` 轮的循环，每轮做四件事：

```text
初始化：prompt 分词、清空 response/loss_mask/rollout_log_probs、注入 stop 标签
for 每一轮（最多 max_turns）:
    1. POST /generate，发送「prompt + 已累积的 response」
    2. 处理 abort（中途被取消）→ 置 ABORTED 直接返回
    3. 取本轮模型输出：
         - 若收 logp：直接从 output_token_logprobs 取 token_id 与 logp（保证对齐）
         - 否则：可安全后处理字符串再分词
    4. 追加本轮模型 token（trainable=True）
    5. 若 finish_reason == "length"（撞长度上限）→ 跳出
    6. 解析动作 execute_predictions：
         - <search>   → 调检索、拼 <information>...</information>、done=False
         - <answer>   → done=True，跳出
         - 非法动作   → 拼一段提示「我上一步动作无效…」、done=False
    7. 追加观测 token（trainable=False）
    若 done 则跳出
收尾：写回 tokens/response/response_length/loss_mask/rollout_log_probs，
       按 finish_reason 置 status（length→TRUNCATED / abort→ABORTED / stop→COMPLETED）
```

#### 4.3.3 源码精读

逐段精读 `generate` 函数。

**入口与初始化**：

[examples/search-r1/generate_with_search.py:L145-L161](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L145-L161) —— 签名 `async def generate(args, sample, sampling_params) -> Sample`，开头先断言不支持 partial rollout，然后取 `GenerateState` 拿 tokenizer，把 prompt 分词进 `sample.tokens`，并清空各个累积列表。

**关键 trick：注入 stop 标签**：

[examples/search-r1/generate_with_search.py:L164-L177](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L164-L177) —— 把 `</search>`、`</answer>` 加入 `sampling_params["stop"]`。注释解释了原因：不设 stop 时，SGLang 会在闭合标签后继续吐「垃圾 token」（甚至会凭空造出新的 `Question:`）。例子里原本靠 `postprocess_responses` 裁掉这些垃圾，但开 TIS 后裁剪被禁用（要保持 token/logp 对齐），垃圾就会留在轨迹里被训练。设 stop 标签能从源头避免，且 slime 已设 `no_stop_trim=True` 保证闭合标签本身被保留。**这是多轮工具调用里很容易踩的坑。**

**主循环：模型生成段**：

[examples/search-r1/generate_with_search.py:L179-L226](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L179-L226) —— 每轮 POST 请求；abort 检查（`finish_reason.type == "abort"` → 置 `ABORTED` 返回）；按是否收 logp 取 token；追加为 `trainable=True`。注意 abort 判断在生成后、追加前。

**主循环：环境观测段**：

[examples/search-r1/generate_with_search.py:L232-L253](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L232-L253) —— `finish_reason == "length"` 直接跳出；否则 `execute_predictions` 解析动作；把观测分词后以 `trainable=False` 追加；开 logp 时还做了一次长度对齐断言 `len(response_token_ids) == len(rollout_log_probs)`，这是防回归的护栏。

**收尾：状态机映射**：

[examples/search-r1/generate_with_search.py:L256-L274](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L256-L274) —— 把累积结果写回 Sample 字段，并用 Python 3.10 的 `match/case` 把 SGLang 的 `finish_reason.type` 映射到 `Sample.Status`：`length→TRUNCATED`、`abort→ABORTED`、`stop→COMPLETED`。`status` 决定下游（partial rollout、奖励、过滤）如何对待这个样本。

**配套奖励函数**（虽然属于 `--custom-rm-path`，但和 generate 强耦合，顺便看清）：

[examples/search-r1/generate_with_search.py:L277-L293](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L277-L293) —— `async def reward_func(args, sample, **kwargs) -> float`，用 `compute_score_em` 对 `prompt + response` 与 `ground_truth` 算 EM 分，含一个 `format_score` 格式分。它接收的是 generate 填好的完整 `sample`。

最后看启动脚本怎么把两者接上：

[examples/search-r1/run_qwen2.5_3B.sh:L115-L122](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/run_qwen2.5_3B.sh#L115-L122) —— `--custom-generate-function-path generate_with_search.generate` 与 `--custom-rm-path generate_with_search.reward_func`。注意 `RUNTIME_ENV_JSON` 里把脚本目录加进了 `PYTHONPATH`，所以 `generate_with_search` 这个模块名才能被 `load_function` import 到（见 [run_qwen2.5_3B.sh:L128-L133](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/run_qwen2.5_3B.sh#L128-L133)）。

#### 4.3.4 代码实践

实践目标：仿照 search-r1，写一个**最小**的多轮 custom_generate 骨架——模型生成 → 调一个假工具 → 拼回历史 → 正确标 loss_mask，但**不依赖真实检索服务**。

```python
# 示例代码：最小多轮 custom_generate 骨架（假工具，可直接被 load_function 解析）
from slime.rollout.sglang_rollout import GenerateState
from slime.utils.http_utils import post
from slime.utils.types import Sample

MAX_TURNS = 2

async def generate(args, sample: Sample, sampling_params) -> Sample:
    state = GenerateState(args)
    url = f"http://{args.sglang_router_ip}:{args.sglang_router_port}/generate"

    prompt_text = sample.prompt
    prompt_ids = state.tokenizer(prompt_text, add_special_tokens=False)["input_ids"]
    sample.tokens = list(prompt_ids)
    sample.loss_mask = []

    response, response_ids, loss_mask = "", [], []

    # 关键：在工具/答案边界处停止，避免引擎继续吐垃圾 token
    stops = list(dict.fromkeys([*(sampling_params.get("stop") or []), "</tool>", "</answer>"]))
    sampling_params = {**sampling_params, "stop": stops}

    for _ in range(MAX_TURNS):
        output = await post(url, {"text": prompt_text + response, "sampling_params": sampling_params})

        # 1) 处理 abort
        if output["meta_info"]["finish_reason"]["type"] == "abort":
            sample.status = Sample.Status.ABORTED
            return sample

        # 2) 模型生成段 —— trainable=True，必须带 log_probs
        seg_ids = [item[1] for item in output["meta_info"]["output_token_logprobs"]]
        seg_logp = [item[0] for item in output["meta_info"]["output_token_logprobs"]]
        response += output["text"]
        response_ids += seg_ids
        loss_mask += [1] * len(seg_ids)
        sample.append_response_tokens(args, tokens=seg_ids, log_probs=seg_logp, trainable=True,
                                      meta_info=output["meta_info"])

        # 3) 终止判断
        if "<answer>" in output["text"] or output["meta_info"]["finish_reason"]["type"] == "length":
            break

        # 4) 调假工具 + 拼观测 —— trainable=False，不带 log_probs
        obs = "\n<tool_result>" + fake_tool(output["text"]) + "</tool_result>\n"
        obs_ids = state.tokenizer(obs, add_special_tokens=False)["input_ids"]
        response += obs
        response_ids += obs_ids
        loss_mask += [0] * len(obs_ids)
        sample.append_response_tokens(args, tokens=obs_ids, trainable=False)

    # 5) 收尾：写回字段 + 状态映射
    sample.tokens = prompt_ids + response_ids
    sample.response = response
    sample.response_length = len(response_ids)
    sample.loss_mask = loss_mask
    match output["meta_info"]["finish_reason"]["type"]:
        case "length": sample.status = Sample.Status.TRUNCATED
        case "abort":  sample.status = Sample.Status.ABORTED
        case "stop":   sample.status = Sample.Status.COMPLETED
    return sample


def fake_tool(text: str) -> str:
    """一个假工具：把模型输出原样回显，演示用。"""
    return f"echo:{text[:20]}"
```

操作步骤：把上面的骨架存成 `my_generate.py`，在训练脚本里加 `--custom-generate-function-path my_generate.generate`，并确保 `my_generate.py` 所在目录在 `PYTHONPATH` 中。

需要观察的现象：每轮 `loss_mask` 在模型段为 1、在 `<tool_result>` 段为 0；`response_length` 与 `len(loss_mask)` 相等。

预期结果：训练日志里 response 长度统计正常，不出现「引擎在闭合标签后继续生成垃圾」的情况。**完整端到端运行需要 GPU 与 SGLang 集群，待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：如果把注入 stop 标签那段代码删掉，开 TIS 训练时会出现什么问题？

**参考答案**：SGLang 会在 `</search>` / `</answer>` 之后继续生成垃圾 token。因为开 TIS 时 `postprocess_responses` 被禁用（要保持 token/logp 对齐），这些垃圾会留在轨迹里且 `loss_mask=1`，被当成模型输出去训练，同时可能破坏格式校验导致奖励下降。

**练习 2**：search-r1 的 `generate` 在收到 `finish_reason == "length"` 时为什么直接 `break` 而不再调工具？

**参考答案**：`length` 表示已撞到 `max_new_tokens` 上限，回复被截断。此时继续拼观测只会让截断的输出更长且无意义，应直接收尾并置 `TRUNCATED`，让下游按截断样本处理。

---

### 4.4 fan-out：返回多个 Sample 与 rollout_id 同源约束

#### 4.4.1 概念说明

默认情况下，一次 custom_generate 调用返回**一个** `Sample`（一次执行 = 一条训练样本）。但在某些智能体场景里，一次 rollout 执行天然会产出**多个**可训练片段：

- **子智能体（subagent）**：主智能体派生一个子智能体，子智能体的轨迹和主智能体的续写都该被训练。
- **上下文压缩（compaction）**：长轨迹被压缩，压缩前后各成一段独立训练样本。
- **多智能体**：一次执行涉及多个 agent，各自一段。

slime 允许 custom_generate 返回 `list[Sample]`，这就是 fan-out。**核心契约是：同一批次 fan-out 出来的兄弟样本必须共享同一个 `rollout_id`**，这样 slime 在 train-step 切分和 loss 聚合时会把它们当成「同一次 rollout 的多个片段」，而不是错误地计成多次独立 rollout（否则同一次执行的奖励会被重复放大）。

#### 4.4.2 核心流程

fan-out 的标准写法（伪代码）：

```text
rollout_id = sample.rollout_id if sample.rollout_id is not None else sample.index
segments = 把一次执行拆成 K 段
samples = []
for segment in segments:
    s = copy.copy(sample)              # 浅拷贝，保留 prompt/metadata 等公共字段
    s.tokens / s.response / s.loss_mask / s.reward = segment 的对应值
    s.status = COMPLETED
    s.rollout_id = rollout_id          # 关键：所有兄弟共享同一 rollout_id
    samples.append(s)
return samples                          # 返回 list[Sample]
```

如果整条轨迹只有一个总奖励却拆成 K 段，常见做法是把奖励均摊：每段赋 `reward / K`，避免同一次 rollout 的奖励被放大 K 倍。

下游处理在 `generate_and_rm` 里已自动兼容（见 4.1.3 调用点之后的分支）：当返回值是 `list` 时，框架对每个样本分别算奖励并返回 `samples` 列表。

#### 4.4.3 源码精读

fan-out 的契约定义在 customization 文档：

[docs/en/get_started/customization.md:L87-L115](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L87-L115) —— 明确「兄弟样本必须共享同一个 `rollout_id`」，并给出标准模板：浅拷贝原 sample、逐段填字段、统一设 `rollout_id`，以及「单总奖励拆 K 段时按 `reward/K` 均摊」的建议。

`rollout_id` 字段的语义在 `Sample` 定义里有详细注释：

[slime/utils/types.py:L99-L106](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L99-L106) —— 默认 `None`，下游回退到 `index`（默认单样本路径下 `rollout_id == index`）；「把一次执行拆成多个训练样本」的路径应给所有兄弟设同一个 `rollout_id`，使 loss 聚合在一次 rollout 内平均，而非重复计数。

再看框架侧对 `list` 返回值的兼容处理（在调用点之后）：

[slime/rollout/sglang_rollout.py:L268-L278](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L268-L278) —— 当 `isinstance(sample, list)` 时，框架逐个算奖励（`batched_async_rm`）并返回列表；若任一兄弟为 `ABORTED` 则整组直接返回，不再算分。这正是 fan-out 返回值能被默认外循环正确消化的原因。

#### 4.4.4 代码实践

实践目标：写一个把一条假轨迹拆成 K 段、均摊奖励的 generate 骨架，确保所有段 `rollout_id` 相同。

```python
# 示例代码：fan-out 骨架（不调用真实模型，用假轨迹演示拆分与均摊）
import copy
from slime.utils.types import Sample

def make_segment(idx, prompt, rollout_id, reward_per_seg):
    s = copy.copy(Sample(prompt=prompt, tokens=[], loss_mask=[], rollout_id=rollout_id))
    s.tokens = [100 + idx, 101 + idx]            # 假装是第 idx 段的 token
    s.response = f"segment_{idx}"
    s.response_length = 2
    s.loss_mask = [1, 1]
    s.reward = reward_per_seg
    s.status = Sample.Status.COMPLETED
    return s

async def generate(args, sample: Sample, sampling_params) -> list[Sample]:
    K = 3
    total_reward = 0.9
    rollout_id = sample.rollout_id if sample.rollout_id is not None else sample.index
    segments = [make_segment(i, sample.prompt, rollout_id, total_reward / K) for i in range(K)]

    # 契约自检：所有兄弟必须共享同一 rollout_id
    assert len({s.rollout_id for s in segments}) == 1, "兄弟样本 rollout_id 必须相同"
    return segments
```

需要观察的现象：三段样本 `rollout_id` 全相等；每段 reward 都是 `0.3`，总和等于原始 `0.9`，未被放大。

预期结果：`{s.rollout_id for s in segments}` 为单元素集合；`sum(s.reward) == total_reward`。待本地验证（需 slime 可 import；`generate` 是协程，本地可用 `asyncio.run(generate(args_stub, sample, {}))` 驱动）。

#### 4.4.5 小练习与答案

**练习 1**：fan-out 出 K 个兄弟样本却忘了设 `rollout_id`，会发生什么？

**参考答案**：`rollout_id` 回退到各自的 `index`，于是 slime 把 K 段当成 K 次独立 rollout 计数，loss 聚合时不会在一次 rollout 内平均。更糟的是若每段都赋了完整 reward，同一次执行的总奖励被放大了约 K 倍，梯度信号失真。

**练习 2**：为什么用 `copy.copy(sample)`（浅拷贝）而不是 `copy.deepcopy`？

**参考答案**：浅拷贝足以新建一个独立 `Sample` 对象并保留 `prompt`、`metadata` 等不可变/共享字段，然后对每个可变字段（`tokens`、`response`、`loss_mask`、`reward`）重新赋值即可。深拷贝会连带复制 prompt 文本等大对象，开销更高且无必要——只要不直接 mutate 共享引用即可。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个**「带日志的搜索式 custom_generate」**小任务：

1. 以 4.3.4 的骨架为基础，增加一段：在每轮模型生成后，用 `print` 或 slime 的 trace 工具记录「第几轮、生成了几个 token、loss_mask 里有几个 1」。
2. 把假工具 `fake_tool` 改成「当模型输出包含数字时，返回该数字的平方；否则返回 `no_number`」，并确保返回串用 `<tool_result>...</tool_result>` 包裹。
3. 在函数末尾加一段自检断言：`assert len(sample.loss_mask) == sample.response_length`，保证长度对齐。
4. 把该函数用 `--custom-generate-function-path` 接入一个最小训练配置（可复用 `examples/search-r1/run_qwen2.5_3B.sh` 的结构，只换函数路径与数据）。
5. 跑一轮 rollout（`--num-rollout 1`），观察日志里每轮的 token 数与 loss_mask 中 1 的个数是否合理（模型段为 1、工具段为 0）。

完成标志：日志能看到多轮交互、loss_mask 长度断言通过、`sample.status` 在正常结束时为 `COMPLETED`。完整训练需 GPU 集群，**待本地验证**；若没有集群，至少用 4.2.4 / 4.4.4 的纯 Python 自检覆盖 loss_mask 与 fan-out 的正确性。

## 6. 本讲小结

- `--custom-generate-function-path` 是「只换生成工位、不动外循环」的接口，签名 `async def custom_generate(args, sample, sampling_params) -> Sample | list[Sample]`，在 `generate_and_rm` 内被 `load_function` 解析后调用，运行在信号量与 dp_rank 上下文中。
- custom_generate **只管生成**，奖励由框架在返回后统一计算（除非你手动填好 `sample.reward`）；签名可选地声明 `evaluation` 形参以区分训练/评估。
- **loss_mask 是多轮训练的关键**：模型生成 token 标 `trainable=True`（必须带 log_probs），环境/工具 token 标 `trainable=False`（自动填 0 占位）；开 TIS 时绝不能重新分词，必须用 SGLang 原始 token id 与 logp。
- search-r1 示例展示了完整套路：注入 stop 标签防止引擎吐垃圾 → 多轮「生成/解析/调工具/拼观测」→ abort/length/stop 三态映射 `Sample.Status`。
- fan-out 返回 `list[Sample]` 时，**所有兄弟样本必须共享同一个 `rollout_id`**，否则同一次 rollout 会被重复计数、奖励被放大；单总奖励拆 K 段时按 `reward/K` 均摊。
- 接入只需两步：写好函数、在启动脚本加 `--custom-generate-function-path`，并确保模块在 `PYTHONPATH` 中可被 import。

## 7. 下一步学习建议

- **自定义奖励与样本转换**：读 [u6-l3](u6-l3-custom-reward-and-conversion.md)，把本讲的 `reward_func` 与 `--custom-rm-path`、`--custom-convert-samples-to-train-data-path` 打通，理解奖励如何写回 `sample.reward` 并流向优势估计。
- **off-policy 修正（TIS）**：读 [u6-l5](u6-l5-custom-loss-offpolicy.md)，搞清楚本讲反复提到的 `rollout_log_probs` 如何用于重要性采样比，以及 search-r1 开 `--use-tis` 后为何必须保持 token/logp 对齐。
- **更重的替换**：若你的场景连默认外循环都不够用（如全异步、长尾），读 [u7-l4](u7-l4-streaming-async-partial.md) 与 `--rollout-function-path`，理解从「换工位」升级到「换整条流水线」的边界。
- **直接读范例**：通读 `examples/search-r1/generate_with_search.py` 全文与 `examples/multi_agent/`，对照本讲的契约，体会单样本与 fan-out 两种写法在真实工程里的差异。
