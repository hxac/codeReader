# 离线推理：LLM 类与 generate/chat

## 1. 本讲目标

本讲带你「跑通第一次推理」。读完之后你应该能够：

- 用 `LLM(model=...)` 构造一个离线推理引擎，并知道构造背后做了哪些事。
- 用 `llm.generate(prompts, sampling_params)` 对一批 prompt 做批量补全（completion）。
- 用 `llm.chat(messages, ...)` 以「对话」的形式调用模型，并理解它与 `generate` 的关系。
- 读懂返回值 `RequestOutput` / `CompletionOutput` 的结构，从中取出 prompt 与生成文本。

本讲只讲**离线推理 API 的使用与源码脉络**，不深入调度、KV 缓存与采样的内部实现（这些在后续单元展开）。它承接上一讲确立的三个公共对象 `LLM` / `SamplingParams` / `ModelRegistry`，把 `LLM` 这一个对象彻底讲透。

## 2. 前置知识

在进入源码前，先建立三组直觉。

### 2.1 「离线」是什么意思

vLLM 提供两条使用入口（在 [仓库目录结构总览] 中讲过 `entrypoints` 是进入系统的两扇门）：

| 入口 | 类/命令 | 面向场景 | 是否需要起服务 |
| --- | --- | --- | --- |
| 离线推理 | `vllm.LLM` | 写脚本、批处理、实验、评测 | 否，同进程直接返回结果 |
| 在线服务 | `vllm serve` | 长驻 HTTP 服务，供客户端调用 | 是，监听端口 |

本讲的 `vllm.LLM` 属于**离线推理**：你在一个 Python 进程里构造引擎、提交 prompt、直接拿到 `list[RequestOutput]` 结果。它适合一次性算完一批任务，不需要把模型部署成常驻服务。

### 2.2 「批量」与「批处理」的关系

朴素推理往往一条 prompt 跑一次 forward。vLLM 的核心能力是**连续批处理（continuous batching）**：你可以把任意多条 prompt 放进一个 `list` 一次性交给 `generate`，引擎内部会自动把它们组织成动态批次、共享 GPU。所以**最佳实践是「把所有 prompt 收集到一个 list，一次性传入」**，而不是循环里一条条调。

### 2.3 generate 与 chat 的差别

- `generate`：输入是**原始文本 prompt**（或 token id），模型从这段文本开始续写。这是最底层的「补全」接口。
- `chat`：输入是**结构化对话**（`[{"role": "user", "content": "..."}]`）。它先用 **chat template** 把对话渲染成一段文本，再交给 `generate` 跑。也就是说，`chat` 是 `generate` 之上加了一层「对话渲染」。

记住这一点，后面读源码时你会看到 `chat` 几乎全是在做「渲染」，最后还是回到 `generate` 同款的下层流程。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `vllm/entrypoints/llm.py` | 定义 `LLM` 类，包含 `__init__` / `generate` / `chat` 等公开方法。本讲主角。 |
| `vllm/entrypoints/offline_utils.py` | `OfflineInferenceMixin`：`generate`/`chat` 真正依赖的执行辅助方法（渲染、入队、跑引擎循环）。 |
| `vllm/v1/engine/llm_engine.py` | `LLMEngine`：离线路径真正驱动的引擎，提供 `add_request` / `step` / `has_unfinished_requests`。 |
| `vllm/outputs.py` | 定义返回值结构 `RequestOutput` 与 `CompletionOutput`。 |
| `examples/basic/offline_inference/basic.py` | 官方 `generate` 最小示例。 |
| `examples/basic/offline_inference/chat.py` | 官方 `chat` 最小示例。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：`LLM.__init__`（构造）、`LLM.generate`（批量补全）、`LLM.chat`（对话渲染）、`RequestOutput`（结果结构）。

### 4.1 LLM.__init__：构造一个推理引擎

#### 4.1.1 概念说明

构造 `LLM` 就是在你的进程里**搭建一套完整的推理运行时**：加载模型权重、切分 KV 缓存显存、初始化调度器与 worker。`LLM` 把这一切封装起来，让你只需关心 `model=...` 这类高层参数。

由于参数非常多（量化、张量并行、显存利用率、编译配置……），vLLM 的做法是：先把这些参数收集成一个 **`EngineArgs`**（引擎参数包），再用它去构造真正的引擎 **`LLMEngine`**。`LLM` 本身更像是一个「门面（facade）」，把参数转发给 `EngineArgs` / `LLMEngine`。

#### 4.1.2 核心流程

```
LLM(model=..., tensor_parallel_size=..., ...)
   │
   ├─ 1. 规整少量特殊参数（compilation_config 等可传 int/dict/对象）
   │
   ├─ 2. 把所有参数打包成 EngineArgs(model=..., ...)
   │
   ├─ 3. LLMEngine.from_engine_args(engine_args, usage_context=LLM_CLASS)
   │        ↑ 这里真正加载模型、分配 KV 缓存、起 worker
   │
   └─ 4. 从引擎取出 model_config / renderer / input_processor 等引用，
          供后续 generate / chat 使用
```

> 说明：`usage_context=UsageContext.LLM_CLASS` 是告诉引擎「我现在是被离线 `LLM` 类使用的」，引擎据此做一些针对性优化/统计。`LLMEngine` 是 V1 架构下离线路径使用的引擎（关于「V1 多进程架构」会在 u3-l1 详讲；这里只需知道它就是那个跑模型的引擎）。

#### 4.1.3 源码精读

`LLM` 是一个继承了多个 Mixin 的类，`generate`/`chat` 之外的执行逻辑都来自这些 Mixin：

[vllm/entrypoints/llm.py:67-67](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py#L67-L67) —— `LLM` 的类声明，继承了 `OfflineInferenceMixin`（提供 `_run_completion`/`_run_chat`/`_run_engine` 等执行辅助）。

`__init__` 签名很长，但核心参数就几个。第一关键字参数 `model` 是必填的，其余如 `tensor_parallel_size`、`dtype`、`gpu_memory_utilization`、`quantization` 都有默认值：

[vllm/entrypoints/llm.py:177-222](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py#L177-L222) —— 构造函数签名。注意 `model: str` 之后用 `*,` 强制其余参数全部按关键字传入。

构造函数中段，所有参数被组装成 `EngineArgs`：

[vllm/entrypoints/llm.py:295-335](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py#L295-L335) —— 构造 `EngineArgs`，把 `LLM` 收到的高层参数原样传进去。

随后用 `EngineArgs` 构造引擎，并把引擎暴露的几个对象存到 `self` 上：

[vllm/entrypoints/llm.py:339-354](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py#L339-L354) —— 真正构造 `LLMEngine`，并取出 `model_config`、`renderer`、`chat_template`、`input_processor` 供 `generate`/`chat` 使用。

其中 `self.renderer.warmup(...)` 会在启动时预热对话渲染器；`self.input_processor` 则负责后续的 tokenization（在 u5-l5 详讲）。这里你只需知道：构造结束时，引擎已经就绪，可以接收请求了。

#### 4.1.4 代码实践

1. 实践目标：在不实际下载大模型的前提下，确认 `LLM` 的构造入口与参数透传关系。
2. 操作步骤：
   - 打开 [vllm/entrypoints/llm.py:177](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py#L177)，对照 `__init__` 签名，找出 `tensor_parallel_size`、`gpu_memory_utilization`、`dtype` 三个参数的默认值。
   - 跟到 [vllm/entrypoints/llm.py:295](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py#L295)，确认这些参数都被传入了 `EngineArgs`。
3. 需要观察的现象：你会看到 `LLM` 几乎不在自身做参数处理，而是把全部参数透传给 `EngineArgs`，再交给 `LLMEngine`。
4. 预期结果：能用一句话说出「`LLM` 是 `LLMEngine` 的门面，参数经 `EngineArgs` 中转」。
5. 无法在本环境实际构造（需要 GPU 与模型下载）：**待本地验证**。

#### 4.1.5 小练习与答案

- 练习 1：为什么 `LLM.__init__` 在 `model` 之后用 `*,` 强制关键字参数？
  - 答案：因为后面有几十个参数，且经常会用其中任意几个（如只设 `tensor_parallel_size`）。强制关键字传参可以避免「按位置填错参数」这类隐蔽 bug，也让调用方更易读。
- 练习 2：`LLM(model=...)` 调用后，是 `LLM` 自己加载了模型权重吗？
  - 答案：不是。`LLM` 把参数打包成 `EngineArgs`，由 `LLMEngine.from_engine_args(...)` 负责实际加载模型、分配显存、起 worker；`LLM` 只是持有引擎引用并对外暴露方法。

### 4.2 LLM.generate：批量补全接口

#### 4.2.1 概念说明

`generate` 是最核心的推理方法：你给它一批 prompt 和采样参数，它返回一批 `RequestOutput`。它的关键设计有两点：

1. **自动批处理**：引擎根据显存约束自动把 prompt 组织成动态批次，调用方无需手动切 batch。
2. **`sampling_params` 三种形态**：单个 `SamplingParams`（应用到所有 prompt）、与 prompt 等长的列表（一一对应）、或 `None`（用默认采样参数）。

#### 4.2.2 核心流程

`generate` 本身做的是「校验 + 取默认参数 + 转发」三件事，真正的执行在 Mixin 里：

```
llm.generate(prompts, sampling_params)
   │
   ├─ 校验 runner_type == "generate"（生成式模型才支持）
   │
   ├─ sampling_params is None → 取 get_default_sampling_params()
   │
   └─ _run_completion(...)
         ├─ _add_completion_requests(...)   预处理 prompt + 入队
         │     └─ _add_request() → llm_engine.add_request(...)
         └─ _run_engine(...)                 同步循环跑完所有请求
               └─ while has_unfinished_requests():
                       step_outputs = llm_engine.step()
                       收集 finished 的输出
```

`_run_engine` 是一个**同步 busy loop**：只要还有未完成的请求，就反复调用 `llm_engine.step()`，把每一步产出的、已经 `finished` 的结果收集起来。这正是「离线」接口的特点——调用会阻塞，直到所有 prompt 都生成完毕。

> 关于 `step()`：在 V1 架构里，`LLMEngine.step()` 会从 EngineCore 进程取出本步输出、交给 `output_processor` 处理、再返回 `RequestOutput` 列表。调度与模型执行发生在 EngineCore 进程中（u3-l1 / u5-l1 详讲）。

#### 4.2.3 源码精读

`generate` 的实现很短，核心是参数校验与转发：

[vllm/entrypoints/llm.py:414-477](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py#L414-L477) —— `generate` 定义。注意 [L457-L463](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py#L457-L463) 校验 `runner_type` 必须为 `"generate"`，[L465-L466](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py#L465-L466) 在 `sampling_params` 为 `None` 时取默认值。

`get_default_sampling_params` 会从模型配置里取出与「全局默认」不同的那部分采样参数：

[vllm/entrypoints/llm.py:407-412](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py#L407-L412) —— 取默认采样参数。这样模型自带的推荐采样设置（如某些模型要求特定 temperature）会被自动应用。

真正的执行在 `OfflineInferenceMixin._run_completion` 里：它先 `_add_completion_requests`（预处理并入队），再 `_run_engine`（跑循环）：

[vllm/entrypoints/offline_utils.py:326-349](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/offline_utils.py#L326-L349) —— `_run_completion`：入队 + 跑引擎两步。

单个请求通过 `_add_request` 进入引擎，它把每个请求的 `output_kind` 设为 `FINAL_ONLY`（离线场景只关心最终结果，不要中间流式输出）：

[vllm/entrypoints/offline_utils.py:552-571](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/offline_utils.py#L552-L571) —— `_add_request`：分配 `request_id`，调用 `llm_engine.add_request(...)`。

最后是同步循环 `_run_engine`，反复 `step()` 直到没有未完成请求：

[vllm/entrypoints/offline_utils.py:573-595](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/offline_utils.py#L573-L595) —— `_run_engine` 的核心：`while self.llm_engine.has_unfinished_requests(): step_outputs = self.llm_engine.step()`，并把 `finished` 的输出收集到结果列表（带 tqdm 进度条）。

引擎侧的 `step` 则负责取出并处理 EngineCore 的输出：

[vllm/v1/engine/llm_engine.py:296-334](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/llm_engine.py#L296-L334) —— `LLMEngine.step`：取输出 → 处理 → 终止因 stop string 提前结束的请求 → 记录统计 → 返回 `request_outputs`。

官方最小示例 `basic.py` 就是这套流程的最干净体现：

[examples/basic/offline_inference/basic.py:14-31](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/examples/basic/offline_inference/basic.py#L14-L31) —— 构造 `SamplingParams` → `LLM(model=...)` → `llm.generate(prompts, sampling_params)` → 遍历 `output.outputs[0].text` 打印。

#### 4.2.4 代码实践

1. 实践目标：跑通官方 `generate` 示例，确认返回结构。
2. 操作步骤（需要 GPU 与网络可下载模型）：
   ```bash
   .venv/bin/python examples/basic/offline_inference/basic.py
   ```
   或参照它自己写一段：用一个更小的 `facebook/opt-125m`，对 3 条 prompt 做 `generate`。
3. 需要观察的现象：终端打印 `Prompt:` 与 `Output:` 成对出现；`output.outputs[0].text` 就是单条候选的生成文本。
4. 预期结果：得到一个长度等于 `len(prompts)` 的 `list[RequestOutput]`，顺序与输入一致。
5. 若无 GPU/网络：**待本地验证**；可改为阅读 `basic.py` 并手动追踪 `_run_engine` 循环逻辑。

#### 4.2.5 小练习与答案

- 练习 1：如果我传 `sampling_params` 为一个**列表**，它和 `prompts` 的长度关系是什么？
  - 答案：列表长度必须等于 `prompts` 长度，按位置一一对应（每条 prompt 用各自的采样参数）；若传单个 `SamplingParams`，则应用到全部 prompt。
- 练习 2：为什么离线 `_add_request` 要把 `output_kind` 设为 `FINAL_ONLY`？
  - 答案：离线场景调用方一次拿全部最终结果即可，不需要每个 token 都回调；`FINAL_ONLY` 让引擎只在请求完成时返回一份结果，减少无用的中间输出与开销。

### 4.3 LLM.chat：对话模板渲染后调用 generate

#### 4.3.1 概念说明

`chat` 接受的是**结构化对话**，例如：

```python
conversation = [
    {"role": "system", "content": "You are a helpful assistant"},
    {"role": "user", "content": "Hello"},
]
```

它的工作是把这些消息用模型自带的（或你指定的）**chat template** 渲染成一段纯文本，再走和 `generate` 完全相同的下层流程。所以 `chat` 的额外职责主要是「渲染」。

#### 4.3.2 核心流程

```
llm.chat(messages, sampling_params)
   │
   ├─ 校验 runner_type == "generate"
   ├─ sampling_params is None → 取默认
   └─ _run_chat(...)
         ├─ _add_chat_requests(...)
         │     └─ 用 chat template 把 messages 渲染成文本
         │         → 再 _add_request() 入队
         └─ _run_engine(...)   （与 generate 完全相同的同步循环）
```

注意最后一行：`chat` 和 `generate` **共用同一个 `_run_engine`**。两者唯一的差别在「入队前的渲染」。

#### 4.3.3 源码精读

`chat` 同样是「校验 + 取默认 + 转发」，但多了一批与对话渲染相关的参数（`chat_template`、`add_generation_prompt`、`tools` 等）：

[vllm/entrypoints/llm.py:608-700](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py#L608-L700) —— `chat` 定义。docstring 明确说明：会话会被转成文本 prompt，再调用 `generate` 的底层逻辑（[L625-L629](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py#L625-L629)）。

`_run_chat` 与 `_run_completion` 几乎对称，区别只在 `_add_chat_requests` 负责渲染：

[vllm/entrypoints/offline_utils.py:351-385](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/offline_utils.py#L351-L385) —— `_run_chat`：先 `_add_chat_requests`（含对话渲染），再 `_run_engine`。

官方 `chat` 示例展示了三种用法：单条对话、批量对话（把同一对话重复成列表）、以及自定义 chat template：

[examples/basic/offline_inference/chat.py:60-82](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/examples/basic/offline_inference/chat.py#L60-L82) —— 先对单条 `conversation` 调 `chat`，再把 `conversation` 复制 10 份做批量对话推理，验证 `chat` 同样支持批量。

[examples/basic/offline_inference/chat.py:84-96](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/examples/basic/offline_inference/chat.py#L84-L96) —— 当提供 `--chat-template-path` 时，读取文件内容作为自定义 `chat_template` 传入。

> 提示：`chat` 默认使用模型自带的 chat template。只有当模型没有内置模板，或你想覆盖时，才需要显式传 `chat_template`。

#### 4.3.4 代码实践

1. 实践目标：理解 `chat` 与 `generate` 共享下层、只差一层渲染。
2. 操作步骤（需要 GPU 与可下载模型，如 `meta-llama/Llama-3.2-1B-Instruct`）：
   ```bash
   .venv/bin/python examples/basic/offline_inference/chat.py
   ```
3. 需要观察的现象：单条对话与批量（10 条）对话都能返回；批量时会显示 tqdm 进度条（因为示例显式传了 `use_tqdm=True`）。
4. 预期结果：`outputs` 长度等于传入的对话数量，每条 `output.outputs[0].text` 是模型对最后一条 `user` 消息的回复。
5. 若无 GPU/网络：**待本地验证**；可阅读 `_run_chat` 与 `_run_completion`，对比确认二者仅在「入队前渲染」处不同。

#### 4.3.5 小练习与答案

- 练习 1：`chat` 和 `generate` 的返回类型一样吗？
  - 答案：一样，都是 `list[RequestOutput]`。`chat` 只是在入队前多做了对话渲染，最终走的是同一个 `_run_engine`。
- 练习 2：什么时候必须显式传 `chat_template`？
  - 答案：当模型自身没有内置 chat template（例如基座模型/部分自训练模型），或你想覆盖默认模板时。若模型已带模板，不传也能正常工作。

### 4.4 RequestOutput / CompletionOutput：读懂返回结构

#### 4.4.1 概念说明

`generate` / `chat` 返回 `list[RequestOutput]`，**列表长度 = 你传入的 prompt/对话数量，顺序一一对应**。每个 `RequestOutput` 代表「一条请求的最终结果」，它内部又包含一个 `outputs` 列表，里面每个元素是一个 `CompletionOutput`（对应 `SamplingParams.n` 生成的第 `n` 个候选）。

数据结构是「两层」：

- 外层 `RequestOutput`：请求级，承载 prompt、prompt token id、是否完成等。
- 内层 `CompletionOutput`：候选级，承载这一条候选的生成文本、token id、finish_reason 等。

#### 4.4.2 核心流程（字段速查）

```
RequestOutput（请求级）
├── request_id            请求唯一 id
├── prompt                原始 prompt 文本
├── prompt_token_ids      prompt 的 token id 列表
├── outputs: list[CompletionOutput]   候选列表（默认 1 条）
│     └── CompletionOutput（候选级）
│           ├── index              第几个候选（n 采样时 0..n-1）
│           ├── text               生成文本  ← 最常用
│           ├── token_ids          生成 token id
│           ├── cumulative_logprob 累计对数概率
│           ├── finish_reason      结束原因（如 "stop"/"length"）
│           └── stop_reason        触发停止的具体字符串/token
└── finished              整个请求是否完成
```

取生成文本的典型写法（与 `basic.py` 一致）：`output.outputs[0].text`。

#### 4.4.3 源码精读

`CompletionOutput` 是一个 dataclass，字段含义直接对应上面的速查表：

[vllm/outputs.py:21-48](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/outputs.py#L21-L48) —— `CompletionOutput` 字段定义。`finished()` 方法（[L50-L51](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/outputs.py#L50-L51)）通过 `finish_reason is not None` 判断该候选是否结束。

`RequestOutput` 是普通类（`__init__`），核心字段如下：

[vllm/outputs.py:112-150](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/outputs.py#L112-L150) —— `RequestOutput.__init__` 把字段逐一赋给实例，包括 `prompt`、`prompt_token_ids`、`outputs`、`finished`，以及缓存统计 `num_cached_tokens`（命中前缀缓存的 token 数）等。

类文档字符串清楚列出了每个字段的语义，是理解返回结构的一手资料：

[vllm/outputs.py:85-110](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/outputs.py#L85-L110) —— `RequestOutput` docstring，逐字段说明。

#### 4.4.4 代码实践

1. 实践目标：从返回结构中正确取出多候选生成结果。
2. 操作步骤：在 `basic.py` 基础上，把 `SamplingParams` 改成 `SamplingParams(temperature=0.8, top_p=0.95, n=2)`，然后遍历 `output.outputs`（而不是只取 `[0]`）打印每个候选。
3. 需要观察的现象：每条 prompt 现在会打印 2 段不同的生成文本，对应 `outputs[0]` 和 `outputs[1]`，它们的 `index` 分别是 0 和 1。
4. 预期结果：`len(output.outputs) == 2`，两段文本因 `temperature>0` 而不同。
5. 若无 GPU/网络：**待本地验证**。

#### 4.4.5 小练习与答案

- 练习 1：`output.outputs` 的长度由什么决定？
  - 答案：由该请求 `SamplingParams` 的 `n` 决定（默认 `n=1`，所以 `outputs` 只有一条）。它表示「对同一 prompt 采样几次」。
- 练习 2：`RequestOutput.finished` 和 `CompletionOutput.finished()` 有什么区别？
  - 答案：前者表示整条请求（含所有候选）是否完成；后者表示某一条候选是否结束（`finish_reason is not None`）。当 `n>1` 时，请求完成要求所有候选都结束。

## 5. 综合实践

把本讲的四个模块串起来，完成一个小任务：

> 用 `LLM` 同时完成「补全」与「对话」两种调用，并把结果统一解析。

参考思路（伪代码，需本地 GPU/网络验证）：

```python
# 示例代码：演示 generate 与 chat 的统一结果解析
from vllm import LLM, SamplingParams

llm = LLM(model="facebook/opt-125m")  # 生成式补全模型

# (1) generate：原始文本续写
prompts = ["The capital of France is", "Hello, my name is", "The future of AI is"]
sp = SamplingParams(temperature=0.8, top_p=0.95, max_tokens=16)
outputs = llm.generate(prompts, sp)
for o in outputs:
    print("prompt:", o.prompt)
    for cand in o.outputs:           # 遍历候选（n=1 时只有一条）
        print("  ->", cand.text, "| finish:", cand.finish_reason)

# (2) chat：结构化对话（注意：opt-125m 是基座模型，可能无内置 chat template，
#     此处仅演示调用形式；真正多轮对话建议用带 instruct 后缀的模型）
```

要做的事：

1. 分别调用 `generate` 和 `chat`，体会两者输入形态的差异。
2. 用同一个解析逻辑（遍历 `output.outputs`）打印两者的结果，验证返回结构一致。
3. 把 `max_tokens` 调大/调小，观察 `finish_reason` 在 `"stop"`（遇到停止符）与 `"length"`（达到 max_tokens）之间的变化。
4. 在源码侧：对照 [offline_utils.py:326-385](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/offline_utils.py#L326-L385) 确认 `_run_completion` 与 `_run_chat` 最后都汇聚到 `_run_engine`。

> 若环境无 GPU/网络，则改为纯源码阅读：画出 `generate(prompts) → _run_completion → _add_completion_requests → _add_request → add_request` 与 `_run_engine → step → get_output` 的完整调用链，并标注每一步的文件与行号。

## 6. 本讲小结

- `LLM` 是离线推理入口，是 `LLMEngine` 的门面：构造时把参数打包成 `EngineArgs`，再由 `LLMEngine.from_engine_args(...)` 真正加载模型、分配显存。
- `generate(prompts, sampling_params)` 接收原始文本 prompt，自动批处理；`sampling_params` 可为单个、列表或 `None`（取模型默认）。
- `chat(messages, ...)` 接收结构化对话，先用 chat template 渲染成文本，再走与 `generate` 完全相同的 `_run_engine` 同步循环。
- 离线执行的底层是一个 busy loop：`while has_unfinished_requests(): llm_engine.step()`，并把 `finished` 的结果收集为 `list[RequestOutput]`。
- 返回结构是两层：`RequestOutput`（请求级，含 prompt）内含 `list[CompletionOutput]`（候选级，含 `text` / `token_ids` / `finish_reason`），`outputs` 长度由 `SamplingParams.n` 决定。
- 离线路径把每个请求的 `output_kind` 设为 `FINAL_ONLY`，只在请求完成时返回一份结果。

## 7. 下一步学习建议

- 下一讲 **u2-l2（vllm CLI 与 serve）** 会进入在线服务入口，看看 `vllm serve` 如何拉起常驻服务，并与本讲的离线 `LLM` 形成对照。
- 若想深入采样参数本身（temperature/top_p/top_k/max_tokens/n 的数学含义），可先读 **u2-l4（SamplingParams 入门）**。
- 若想理解 `_run_engine` 里 `step()` 背后的引擎架构（EngineCore 进程、调度、worker），请到单元 3：**u3-l1（V1 多进程架构）** 与 **u5-l1（EngineCore 主循环）**。
- 继续阅读 [vllm/entrypoints/llm.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py) 中的 `beam_search` / `pooling` 等其他 Mixin 方法，了解 `LLM` 还能做哪些事。

[仓库目录结构总览]: u1-l3-repo-structure.md
