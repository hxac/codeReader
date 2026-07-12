# 多后端推理引擎

## 1. 本讲目标

本讲承接 [u6-l1 推理引擎抽象与协议]，把视线从「统一抽象」移到「具体后端」。

读完本讲，你应当能够：

1. 说清 `--infer_backend` 这一个字符串是如何被映射到具体引擎类（`TransformersEngine` / `VllmEngine` / `SglangEngine` / `LmdeployEngine`）的，以及每个后端的专属参数从哪里来。
2. 理解四种引擎在「如何构造底层引擎、如何驱动生成、同步还是异步」三个维度上的实现差异。
3. 解释切换后端时，为什么 `template` 与 `RequestConfig` 不用改也能保持一致——也就是「上层不变、下层替换」的关键设计。
4. 认识 `GRPOVllmEngine` 这个为强化学习（GRPO）Rollout 量身定制的 vLLM 子类，以及它和普通推理引擎的区别。

---

## 2. 前置知识

本讲默认你已经学完 u6-l1，熟悉下列概念（下面只做最简提醒）：

- **推理引擎抽象**：`BaseInferEngine` 只声明两个方法——同步批量 `infer()` 与异步单条 `infer_async()`；`InferEngine` 是它们的共享实现基类，负责吃 `template`、批处理、统计指标。
- **输入两件套**：`InferRequest`（承载 messages 与多模态资产，「问什么」）与 `RequestConfig`（承载 `temperature` / `max_tokens` / `stream` 等采样参数，「怎么答」）。
- **统一输出**：`ChatCompletionResponse`，结构对齐 OpenAI Chat Completions。
- **template**：把对话 `messages` 翻译成 token 序列的组件（见 u3-l3）。

此外，初学者可以这样建立直觉：**所谓「后端」，就是真正去跑前向、吐 token 的那台发动机**。transformers 是「通用轿车的原厂发动机」，兼容性最好但慢；vLLM / SGLang / Lmdeploy 是「改装涡轮增压」，吞吐高但有各自适配成本。ms-swift 的价值，就是让你只用换一个 `--infer_backend` 参数，就把发动机换掉，而方向盘（输入 `InferRequest`/`RequestConfig`）和仪表盘（输出 `ChatCompletionResponse`）完全不变。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `swift/infer_engine/base.py` | 纯抽象基类 `BaseInferEngine`，定义 `infer` / `infer_async` 契约。 |
| `swift/infer_engine/infer_engine.py` | 共享实现基类 `InferEngine`：吃 template、批处理、默认 `infer` 实现。 |
| `swift/infer_engine/transformers_engine.py` | `TransformersEngine`：基于原生 transformers 的引擎。 |
| `swift/infer_engine/vllm_engine.py` | `VllmEngine`：基于 vLLM 的高吞吐引擎。 |
| `swift/infer_engine/sglang_engine.py` | `SglangEngine`：基于 SGLang 的引擎。 |
| `swift/infer_engine/lmdeploy_engine.py` | `LmdeployEngine`：基于 LMDeploy 的引擎。 |
| `swift/infer_engine/grpo_vllm_engine.py` | `GRPOVllmEngine`：继承 `VllmEngine`，服务于强化学习 Rollout。 |
| `swift/arguments/infer_args.py` | `InferArguments`：定义 `infer_backend` 参数与各后端的参数组。 |
| `swift/rlhf_trainers/args_mixin.py` | `VllmArguments`：vLLM 专属参数组与 `get_vllm_engine_kwargs()`。 |
| `swift/pipelines/infer/infer.py` | `SwiftInfer`：`swift infer` 管道，含引擎工厂 `get_infer_engine()`。 |
| `swift/pipelines/infer/rollout.py` | `SwiftRolloutDeploy`：`swift rollout` 管道，固定使用 `GRPOVllmEngine` 并做权重同步。 |

---

## 4. 核心概念与源码讲解

### 4.1 推理后端切换机制：infer_backend 与引擎工厂

#### 4.1.1 概念说明

ms-swift 把「换推理发动机」这件事抽象成了一个字符串参数 `infer_backend`，取值为 `transformers` / `vllm` / `sglang` / `lmdeploy` 四者之一。这个参数定义在 `InferArguments` 里，默认是 `transformers`：

```python
# infer_backend 取值受 Literal 约束，'pt' 是兼容 swift3.x 的旧别名
infer_backend: Literal['vllm', 'transformers', 'sglang', 'lmdeploy', 'pt'] = 'transformers'
```

[swift/arguments/infer_args.py:127-L152](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/infer_args.py#L127-L152) 定义了 `InferArguments`，它通过多继承把 `LmdeployArguments`、`SglangArguments`、`VllmArguments` 三组后端参数连同 `BaseArguments` 扁平拼到一起。这就是为什么你在命令行里既能传 `--vllm_tensor_parallel_size`，也能传 `--sglang_tp_size`、`--lmdeploy_tp`——它们来自不同的 mixin，但最终都被同一个 dataclass 收纳。

关键设计：**`infer_backend` 是「开关」，各后端参数是「插槽」，工厂方法是「接线员」**。当你指定某个后端时，只有对应那一组参数会被真正取用，其余的即便填了也无效。

#### 4.1.2 核心流程

引擎的派发发生在 `swift infer` 管道 `SwiftInfer` 的构造期，由静态方法 `get_infer_engine()` 完成：

1. **读开关**：取出 `args.infer_backend`。
2. **拼公共参数**：`model_id_or_path` / `model_type` / `torch_dtype` / `template` 等所有后端通用的字段。
3. **分支选类 + 取专属参数**：按 `infer_backend` 进入对应 `if` 分支，确定引擎类 `infer_engine_cls`，并用 `args.get_xxx_engine_kwargs()` 把该后端专属参数（如 vLLM 的 `tensor_parallel_size`）打包成字典。
4. **实例化**：`return infer_engine_cls(**kwargs)`。

这是一段典型的「字符串路由 + 懒加载导入」工厂：vLLM/SGLang/Lmdeploy 的 `import` 写在分支内部，是为了保护 u1-l3 讲过的 `_LazyModule` 懒加载——不装 vLLM 的用户在用 transformers 后端时不会被强拉 vLLM。

#### 4.1.3 源码精读

工厂方法的完整逻辑：

[swift/pipelines/infer/infer.py:50-L93](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/infer.py#L50-L93) 是引擎工厂 `get_infer_engine()`。四个分支分别把 `infer_backend` 映射到 `TransformersEngine` / `VllmEngine` / `SglangEngine` / `LmdeployEngine`，非法值抛错。注意 vLLM 分支还会处理分布式种子（`is_dist()` 时给不同数据并行进程不同 seed，并切换 `distributed_executor_backend='external_launcher'`）。

而 `SwiftInfer.__init__` 决定了「transformers 走特例、其余走通用工厂」：

[swift/pipelines/infer/infer.py:24-L40](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/infer.py#L24-L40)。这里有一个值得注意的细节：transformers 后端先用 `prepare_model_template(args)` 把模型权重加载成 `nn.Module` 再喂给引擎；其它后端则只传一个模型路径字符串，由引擎自己决定怎么加载。这正反映了 4.2 节要讲的根本差异。

每个后端的专属参数由各自的 `get_xxx_engine_kwargs()` 打包。以 vLLM 为例：

[swift/rlhf_trainers/args_mixin.py:65-L96](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/args_mixin.py#L65-L96) 的 `get_vllm_engine_kwargs()` 把所有 `vllm_*` 字段收进一个字典，其中还会根据 `adapters` 是否为空自动推断 `enable_lora`。SGLang 与 LMDeploy 的打包方法在 `swift/arguments/infer_args.py` 里，结构完全对称。

此外，`infer_backend` 还有一个「自动升级为异步引擎」的隐藏规则：当任务是编码类（embedding/seq_cls/reranker）且后端是 vLLM 时，vLLM 的同步 `LLMEngine` 不支持 `encode`，必须用 `AsyncLLMEngine`：

[swift/arguments/infer_args.py:216-L237](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/infer_args.py#L216-L237) 的 `_init_vllm_async_engine()` 实现了这条自动推断，并在用户强行设 `false` 时报错。

#### 4.1.4 代码实践

**实践目标**：不跑训练，只验证「同一组参数，换一个字符串后端就换一台发动机」。

操作步骤：

1. 准备一个本地可用的基座模型（例如之前 u1-l5 训练产物或任意小模型）。
2. 分别执行两条命令，观察日志开头打印的引擎类与参数差异：

```bash
swift infer --model <your_model> --infer_backend transformers --max_batch_size 4
swift infer --model <your_model> --infer_backend vllm --vllm_tensor_parallel_size 1
```

3. 在交互里问同一个问题，对比首字延迟与吞吐。

需要观察的现象：

- transformers 模式日志里会出现 `model: <nn.Module 的 repr>`（因为模型已是对象）；vllm 模式日志里会出现 vLLM 自身的初始化输出（如 `gpu_memory_utilization`、权重加载条）。
- 两条命令都打印 `request_config: ...`，结构完全一致——这正是「输入不变」的体现。

预期结果：vLLM 首次加载更慢（要编译 CUDA graph、建 KV cache），但多轮连续提问的吞吐明显高于 transformers。

> 待本地验证：具体延迟数值取决于你的显卡与模型规模；本实践重点是观察日志差异，而非追求绝对数字。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `VllmEngine` 等三个加速引擎的 `import` 写在 `get_infer_engine()` 的分支里，而 `TransformersEngine` 写在文件顶部？

**参考答案**：vLLM/SGLang/Lmdeploy 是重型可选依赖。写在分支内做懒加载，可以保证只装了 transformers 的用户在用默认后端时不会被 `import vllm` 失败阻断；而 transformers 是 ms-swift 的核心硬依赖（见 u1-l2 的 `framework.txt`），必然存在，故可放顶部。

**练习 2**：用户传 `--infer_backend pt` 会发生什么？

**参考答案**：`__post_init__` 会把 `pt` 改写成 `transformers` 并打一条 deprecation warning（[swift/arguments/infer_args.py:203-L207](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/infer_args.py#L203-L207)），因为 `pt` 是 swift3.x 的旧别名，为向后兼容保留。

---

### 4.2 TransformersEngine：原生 transformers 推理引擎

#### 4.2.1 概念说明

`TransformersEngine` 是 ms-swift 的「默认发动机」：它直接复用 HuggingFace transformers 的 `model.generate()` 来做生成。它的特点是：

- **最全的任务支持**：因为它直接拿到模型的 logits，所以分类（`seq_cls`）、打分（`reranker`）、过程奖励（`prm`）、向量（`embedding`）这些非生成任务，只有它能完整支持（见 u6-l1）。
- **显式持有模型对象**：构造时它已经拿到了 `nn.Module`，自身就是「模型容器」。
- **无需编译**：启动快，但单卡吞吐远低于加速引擎。

#### 4.2.2 核心流程

`TransformersEngine` 的实现有一条贯穿全篇的暗线——**它用一个常驻工作线程把同步生成「伪装」成异步**：

1. 构造期：拿到模型对象与 template，初始化一个 `Queue()` 和任务池。
2. 用户调 `infer_async(request)`：把请求塞进 `self._queue`，并 `await queue.get()` 等结果。
3. 后台工作线程 `_infer_worker` 不断 `_fetch_infer_requests()`，把队列里**相同 `request_config` 的请求合并成一个 batch**，交给 `_infer` 执行（哈希 key 见 [transformers_engine.py:136-L157](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/transformers_engine.py#L136-L157)）。
4. `_infer` 内部用 `template.encode` 把请求变成输入张量，按 `task_type` 分流到 `_infer_forward`（非生成任务）或 `_infer_full`（生成任务），结果通过 `asyncio.run_coroutine_threadsafe` 投递回各请求的 queue。

同步入口 `infer()` 则简单得多：分批调 `_infer`，不经过那个工作线程。

#### 4.2.3 源码精读

**构造与模型加载**：

[swift/infer_engine/transformers_engine.py:50-L114](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/transformers_engine.py#L50-L114) 是 `__init__`。关键点有三：① 当 `model` 是字符串时，用 `_get_model_processor`（进而 `get_model_processor`，见 u3-l1）加载模型与 processor，再据此构造 template；当 `model` 已是 `nn.Module` 时要求显式传 template。② 调 `Swift.from_pretrained` 逐个挂载 adapter（LoRA）。③ 设 `self.engine = self.model` 作为占位（注释写了 `# dummy`），让上层对 `engine` 属性的访问在 transformers 后端下不报错。

**核心推理分流**：

[swift/infer_engine/transformers_engine.py:501-L561](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/transformers_engine.py#L501-L561) 的 `_infer` 是总指挥。它先用 `self.template.set_mode('transformers')` 把模板切到 transformers 模式，再做 batch 编码、设备搬运，最后用一个三元判断分流：

```python
infer_func = self._infer_forward if self.template.task_type in {
    'seq_cls', 'prm', 'embedding', 'reranker', 'generative_reranker'
} else self._infer_full
```

也就是说，「是生成任务吗」决定了走 `_infer_full`（调 `template.generate` → `model.generate`）还是 `_infer_forward`（调 `model(**inputs)` 取 logits）。

**异步入口与工作线程**：

[swift/infer_engine/transformers_engine.py:467-L498](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/transformers_engine.py#L467-L498) 的 `infer_async` 把请求连同一个新建的 `asyncio.Queue` 一起入队，然后按是否 `stream` 决定返回「队列生成器」还是「单次 `await queue.get()`」。配套的 `_start_infer_worker` / `_infer_worker`（[transformers_engine.py:132-L179](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/transformers_engine.py#L132-L179)）才是真正跑生成的地方。这套设计让 transformers 后端也能像 vLLM 那样「来一个请求异步处理」，只是底层仍是一次同步 `model.generate`。

#### 4.2.4 代码实践

**实践目标**：用 Python 代码直接构造 `TransformersEngine`，理解它「拿模型对象 + template 就能推理」的契约（呼应 u6-l1 的实践）。

操作步骤（示例代码）：

```python
# 示例代码：直接使用 TransformersEngine 推理
from swift.infer_engine import TransformersEngine, InferRequest, RequestConfig

# 注意：model 既可以是 model_id 字符串，也可以是已加载的 nn.Module
engine = TransformersEngine('Qwen/Qwen3-4B', max_batch_size=1)

req = InferRequest(messages=[{'role': 'user', 'content': '用一句话介绍 ms-swift'}])
cfg = RequestConfig(max_tokens=64, temperature=0.7)
resp = engine.infer([req], cfg)[0]
print(type(resp))                # ChatCompletionResponse
print(resp.choices[0].message.content)
```

需要观察的现象：

- 不传 `template` 也能跑——因为 `__init__` 在 `model` 是字符串时会自行加载 processor 并构造 template（这正是 4.1 节里「transformers 走特例、其余走工厂」的根因）。
- `resp` 的结构与 u6-l1 描述的 `ChatCompletionResponse` 完全一致，`usage` 字段给出 prompt/completion token 数。

预期结果：模型正常输出回答，且 `engine.model` 直接就是 `AutoModelForCausalLM` 的实例。

> 待本地验证：模型需先下载；若显存不足可换更小模型或加 `torch_dtype`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TransformersEngine` 要在 `__init__` 里设 `self.engine = self.model`（注释 `# dummy`）？

**参考答案**：上层代码（如 RL rollout 的权重同步）有时会访问 `engine.engine` 这个统一属性。transformers 后端没有独立的「底层引擎」对象，模型本身就是引擎，所以用 `self.model` 占位以维持接口一致。

**练习 2**：把一次 `infer_async` 的请求和另一次 `infer_async` 的请求放进同一个引擎，且 `request_config` 相同，会发生什么？

**参考答案**：工作线程的 `_fetch_infer_requests` 会按 `request_config` 的哈希把两者合并进同一个 batch（`max_batch_size` 限制内）一起算，从而提高利用率（见 [transformers_engine.py:136-L157](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/transformers_engine.py#L136-L157)）。

---

### 4.3 VllmEngine / SglangEngine / LmdeployEngine：三类推理加速后端

#### 4.3.1 概念说明

这三个引擎是 ms-swift 的「高性能发动机」，分别封装 vLLM、SGLang、LMDeploy 三个第三方推理框架。它们的共同点是：

- **构造期只传模型路径**，由底层框架自己加载、切分权重。
- **吞吐远高于 transformers**：底层做了 PagedAttention/CUDA graph/连续批处理等优化。
- **都能复用同一个 `template` 与 `RequestConfig`**：差异只在内部如何把 `RequestConfig` 翻译成各自原生的采样参数。

它们的差异主要在三个维度：用什么类构造底层引擎、同步还是异步、原生采样参数叫什么。

| 引擎 | 构造底层引擎 | 生成入口 | 原生采样参数类型 | 是否支持同步批量 |
| --- | --- | --- | --- | --- |
| `VllmEngine` | `LLMEngine` / `AsyncLLMEngine`（二选一） | `engine.step()` / `engine.generate()` | `SamplingParams` | 是（同步循环 step） |
| `SglangEngine` | `sgl.Engine(server_args)` | `engine.async_generate()` | `dict`（喂给 sglang `SamplingParams`） | 否（仅异步） |
| `LmdeployEngine` | `lmdeploy.pipeline(...)` | `engine.safe_run(...)` | `LmdeployGenerationConfig` | 否（仅异步） |

#### 4.3.2 核心流程

三类引擎都遵循同一条流水线（在各自的 `infer_async` 里）：

1. `self.template.set_mode('vllm' | 'sglang' | 'lmdeploy')`：把 template 切到对应后端模式。这是「template 随后端自适应」的关键一步——template 会据此决定多模态输入该以什么形态交给底层。
2. `template.encode(...)`：把 `InferRequest` 编码成 `input_ids` 与多模态数据。
3. `_prepare_generation_config(request_config)`：把统一的 `RequestConfig` 翻译成底层原生的采样参数。
4. `_add_stop_words(...)`：把模板的停止词与用户停止词叠加，转成底层能识别的 `stop` / `stop_token_ids`。
5. 调底层引擎生成，再把原生输出包装回 `ChatCompletionResponse`。

它们的 `infer()`（同步批量入口）策略不同：vLLM 有完整的同步实现（直接 `engine.step()` 循环），所以 `VllmEngine.infer` 重写得很重；而 SGLang/LMDeploy 只能异步，于是它们的 `infer()` 直接 `return super().infer(...)`，复用基类 `InferEngine.infer` 的「为每个请求起一个 `infer_async` 协程、再用事件循环 gather」的实现。

#### 4.3.3 源码精读

**VllmEngine——最复杂的引擎**

[swift/infer_engine/vllm_engine.py:121-L241](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/vllm_engine.py#L121-L241) 的 `__init__` 参数极多（`gpu_memory_utilization`、`tensor_parallel_size`、`enable_prefix_caching`、`quantization`…），这些都是从 4.1 节的 `get_vllm_engine_kwargs()` 传进来的。构造时它会做大量兼容性适配：用 `inspect.signature(engine_cls).parameters` 探测当前 vLLM 版本支持哪些参数，不支持的就跳过或断言报错（例如 `enable_lora`、`limit_mm_per_prompt`）。

[swift/infer_engine/vllm_engine.py:255-L260](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/vllm_engine.py#L255-L260) 的 `_prepare_engine` 在多个上下文补丁（`patch_auto_tokenizer`、`_patch_auto_config`、`disable_deepspeed_zero3`）保护下，根据 `use_async_engine` 选 `AsyncLLMEngine` 或 `LLMEngine` 来 `from_engine_args`。这些补丁是 ms-swift 让「自家 tokenizer/config」覆盖 vLLM 默认行为的关键胶水。

参数翻译在 [swift/infer_engine/vllm_engine.py:508-L557](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/vllm_engine.py#L508-L557) 的 `_prepare_generation_config`：它把 `RequestConfig` 的 `temperature`/`top_p`/`max_tokens` 等翻译成 vLLM 的 `SamplingParams`。这里有一段注释专门解释了「OpenAI 风格的 `logprobs: bool` / `top_logprobs: int`」如何映射到「vLLM 风格的 `logprobs: int`」——这是后端协议差异的一个缩影。

同步批量推理在 [swift/infer_engine/vllm_engine.py:806-L886](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/vllm_engine.py#L806-L886)：先把所有请求 `add_request` 进引擎，再 `while self.engine.has_unfinished_requests(): self.engine.step()` 推进，最后按 `request_id` 收集结果。流式分支则包成一个生成器逐步 yield。

**SglangEngine——纯异步、参数最简**

[swift/infer_engine/sglang_engine.py:27-L101](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/sglang_engine.py#L27-L101) 的构造直接 `sgl.Engine(server_args=...)`，ServerArgs 里是 SGLang 的并行度（tp/pp/dp/ep）、显存占比、投机解码等参数。它的采样参数翻译最简洁——直接返回一个普通 `dict`（[sglang_engine.py:166-L177](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/sglang_engine.py#L166-L177)），由 SGLang 自己转成其 `SamplingParams`。

它的 `infer()` 一行就交差：[swift/infer_engine/sglang_engine.py:212-L220](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/sglang_engine.py#L212-L220) 直接 `return super().infer(...)`，复用基类。真正的活全在 `infer_async`（[sglang_engine.py:222-L247](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/sglang_engine.py#L222-L247)），它把编码丢到线程池（`run_in_executor`），再 `engine.async_generate` 生成。

**LmdeployEngine——多模态预处理最特殊**

[swift/infer_engine/lmdeploy_engine.py:38-L90](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/lmdeploy_engine.py#L38-L90) 构造时用 `lmdeploy.pipeline(model_dir, backend_config=...)`，并通过 `autoget_backend_config` 自动在 Turbomind 与 PyTorch 后端间选择。它对多模态模型有一段显式版本约束：[lmdeploy_engine.py:117-L124](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/lmdeploy_engine.py#L117-L124) 用 `require_version('lmdeploy<0.9', ...)` 声明 0.9 起不再维护多模态推理。

LMDeploy 的 `infer_async` 最特别之处在于图像预处理：[lmdeploy_engine.py:294-L332](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/lmdeploy_engine.py#L294-L332) 会根据 `lmdeploy` 版本与 `engine.backend`（turbomind / pytorch）走不同的 `vl_encoder` 预处理分支，再分别调 `prepare_lmdeploy_turbomind_inputs` 或 `prepare_lmdeploy_pytorch_inputs`。这是三个后端里与 template 协作最深的一处。

**为什么 template/RequestConfig 切后端不用改？**

答案是「`set_mode` + 翻译器」两层机制。template 的 `set_mode` 把后端类型记进 `self.mode`：

[swift/template/base.py:1618-L1622](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1618-L1622)，template 据此在 encode 时产出不同形态的多模态输入。同时 template 还能用 `prepare_engine_kwargs()` 向后端注入后端专属配置，例如 Qwen3 reranker 模板在 vLLM 模式下注入 `hf_overrides` 改写 architectures：

[swift/template/templates/qwen.py:137-L147](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/qwen.py#L137-L147)。于是上层 `InferRequest`/`RequestConfig` 保持不变，所有后端差异都被封装在 template 的 mode 分支与各引擎的翻译器里。

#### 4.3.4 代码实践

**实践目标**：对同一模型分别用 transformers 与 vllm 后端推理，对比延迟与输出，并验证「换后端时输入/输出结构不变」。

操作步骤：

1. 用 transformers 后端跑一条推理并计时：

```bash
time swift infer --model <your_model> --infer_backend transformers \
    --val_dataset AI-ModelScope/alpaca-gpt4:en#5 --max_new_tokens 64
```

2. 用 vllm 后端跑同一条（注意首跑会多花时间在编译与建 cache）：

```bash
time swift infer --model <your_model> --infer_backend vllm \
    --val_dataset AI-ModelScope/alpaca-gpt4:en#5 --max_new_tokens 64
```

3. （可选）换 sglang / lmdeploy 重复。

需要观察的现象：

- 两次推理的结果都写入 `result_path`（默认在 checkpoint 目录或 `./result` 下），打开 jsonl 会发现每条记录的结构（`response`、`labels`、`messages`）完全一致——只是生成内容可能因采样略有不同。
- vLLM 的 `time` 真实耗时（尤其第二步之后）通常显著低于 transformers，但首次启动更慢。

预期结果：输出结构跨后端一致；vLLM 吞吐更高。这正是「上层不变、下层替换」的实际收益。

> 待本地验证：vllm/sglang/lmdeploy 需对应安装（见 u1-l2 的 extras 分组）；若仅装了核心依赖，只有 transformers 后端可用。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SglangEngine.infer` 能只写一行 `return super().infer(...)`，而 `VllmEngine.infer` 却写了几十行？

**参考答案**：SGLang 的底层 `sgl.Engine` 只暴露异步生成接口，所以同步批量只能由基类 `InferEngine.infer` 用「起协程 + 事件循环 gather」来模拟；而 vLLM 的同步 `LLMEngine` 提供了 `add_request` + `step()` 的显式推进循环，`VllmEngine` 重写 `infer` 直接驱动这个循环效率更高，因此实现更重。

**练习 2**：三个加速引擎都支持 `seq_cls`/`reranker` 这类非生成任务吗？

**参考答案**：vLLM 支持（通过 `PoolingParams` 与 `task='classify'/'score'`，见 [vllm_engine.py:468-L484](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/vllm_engine.py#L468-L484)，但需要异步引擎）；SGLang 目前主要处理生成与 embedding；LMDeploy 同样以生成为主。覆盖最全的仍是 `TransformersEngine`。

---

### 4.4 GRPOVllmEngine：强化学习 Rollout 专用引擎

#### 4.4.1 概念说明

`GRPOVllmEngine` 是 `VllmEngine` 的子类，但用途完全不同：它**不是给 `swift infer` 用的，而是给强化学习（GRPO 等）训练时的「Rollout（采样）」阶段用的**。在 RL 训练里，模型需要反复用当前策略生成一批回答，再用奖励信号更新参数，然后又用新参数生成下一批——这个「用最新权重快速生成」的过程叫 rollout。

它与普通 `VllmEngine` 的两点关键区别：

1. **输出类型不同**：把普通的 `ChatCompletionResponse` 包裹成 `RolloutOutput`，方便 RL 流水线统一处理。
2. **配合权重同步**：rollout 用的引擎必须能在「训练进程更新参数后」把新权重热加载进 vLLM，而不必重启引擎。这件事不在 `GRPOVllmEngine` 里，而在配套的 `WeightSyncWorkerExtension` 与 `swift rollout` 管道里。

#### 4.4.2 核心流程

`GRPOVllmEngine` 自身很薄：

1. `infer(...)`：先处理 LoRA（若引擎已加载训练侧的 LoRA，自动构造 `LoRARequest`），再 `super().infer(...)` 复用 `VllmEngine` 的推理，最后把每个 `ChatCompletionResponse` 包成 `RolloutOutput`。
2. `async_infer(...)`：异步版，断言 `n==1`，批量起 `infer_async` 协程后同样包成 `RolloutOutput`。
3. `_create_chat_completion_response(...)`：覆写父类，额外带上 `routed_experts`（MoE 路由统计，RL 训练的负载均衡损失会用到）。
4. `_add_adapter(...)`：扩展父类，让它既能接受 ms-swift 的 `AdapterRequest`，也能直接接受 vLLM 的 `LoRARequest`。

而真正的 RL 接线在 `swift rollout` 管道 `SwiftRolloutDeploy`：它固定用 `GRPOVllmEngine`（即使用户传了别的 `infer_backend` 也会被改写成 vllm），并通过一个 FastAPI 服务暴露 `init_communicator` / `update_named_param` / `update_flattened_params` 等端点，让训练进程把新权重广播进 vLLM worker。

#### 4.4.3 源码精读

**GRPOVllmEngine 主体**：

[swift/infer_engine/grpo_vllm_engine.py:24-L56](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/grpo_vllm_engine.py#L24-L56) 的 `infer` 是核心。注意它把结果包成 `RolloutOutput` 的逻辑：

```python
for i, result in enumerate(res):
    if not isinstance(result, RolloutOutput):
        ...
        res[i] = RolloutOutput(response=result)
```

这样无论底层返回什么，RL 流水线拿到的都是统一的 `RolloutOutput`。覆写的 `_create_chat_completion_response`（[grpo_vllm_engine.py:104-L137](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/grpo_vllm_engine.py#L104-L137)）相比父类多了 `routed_experts=getattr(output, 'routed_experts', None)`。

**Rollout 管道如何强制用 vLLM 并接线权重同步**：

[swift/pipelines/infer/rollout.py:799-L831](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/rollout.py#L799-L831) 的 `SwiftRolloutDeploy.get_infer_engine` 开头就声明「rollout 只支持 vLLM」，并注入两个 RL 专属配置：① `logprobs_mode='processed_logprobs'`（注释说明这是为正确的重要性采样做的温度缩放）；② `worker_extension_cls` 指向权重同步扩展，这是「训练进程 ↔ vLLM worker」热同步权重的入口。

权重同步扩展 [swift/pipelines/infer/rollout.py:112-L306](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/rollout.py#L112-L306) 是一个挂在 vLLM worker 上的 `WorkerExtension`，提供 `init_communicator` / `update_named_param` / `update_flattened_params` / `update_weights_from_ipc` 等方法，通过 NCCL/ZMQ/CUDA-IPC 把训练侧新算出的权重灌进 vLLM，免去每轮 rollout 都重启引擎。这部分细节属于 u7（强化学习）单元，本讲只需建立「`GRPOVllmEngine` 是为这种热同步场景定制的」这一认知。

#### 4.4.4 代码实践

**实践目标**：通过源码阅读理解 `GRPOVllmEngine` 与普通 `VllmEngine` 的差异，不真正跑 RL（RL 实践留给 u7）。

操作步骤（源码阅读型实践）：

1. 打开 `swift/infer_engine/grpo_vllm_engine.py`，确认它 `class GRPOVllmEngine(VllmEngine)`。
2. 对比 `GRPOVllmEngine.infer` 与 `VllmEngine.infer`（4.3 节）：前者多了 LoRA 自动注入与 `RolloutOutput` 包裹。
3. 在 `swift/pipelines/infer/rollout.py` 搜索 `GRPOVllmEngine`，确认它只在 `SwiftRolloutDeploy` 里被实例化，`swift infer`（`SwiftInfer`）从不使用它。

需要观察的现象：

- `GRPOVllmEngine` 没有自己的 `__init__`，完全复用 `VllmEngine` 的构造参数。
- `_create_chat_completion_response` 多出的 `routed_experts` 字段，是 RL 中 MoE 负载均衡损失所需。

预期结果：你能用一句话向他人解释「`GRPOVllmEngine` = `VllmEngine` + RolloutOutput 包裹 + MoE 路由统计 + 容忍 LoRARequest 直传」，并知道它只服务于 rollout。

> 说明：本实践为源码阅读型，不涉及命令运行；真实 RL 实践见 u7 单元。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `swift infer` 不直接用 `GRPOVllmEngine`？

**参考答案**：`GRPOVllmEngine` 的输出是 `RolloutOutput` 而非 `ChatCompletionResponse`，且它携带 RL 专属的 `routed_experts` 等字段与权重同步扩展，对普通推理是负担；普通推理用 `VllmEngine` 即可，输出更干净。

**练习 2**：rollout 阶段为什么需要「热同步权重」而不是重启引擎？

**参考答案**：GRPO 每个训练步都要用最新策略重新采样，若每次都重启 vLLM（重新加载权重、重建 KV cache），开销远大于训练本身。热同步通过 NCCL/ZMQ/CUDA-IPC 直接把新权重视图灌进已运行的 vLLM worker，把 rollout 的换参成本压到最低。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「后端选型小调研」：

1. **选模型与数据**：挑一个你本地有的小模型（如 Qwen2.5-0.5B）和一份小型验证集（`alpaca-gpt4:en#20`，`#20` 是采样 20 条，参见 u4-l1 的数据集语法）。
2. **三后端横评**：分别用 `--infer_backend transformers / vllm / sglang`（按你装了哪些）跑 `swift infer`，记录：① 引擎初始化耗时；② 总推理耗时；③ `result_path` 里 jsonl 的字段结构。
3. **验证一致性**：写一个小脚本，读取三份 jsonl，对比每条记录的键集合是否相同（应都含 `response`/`labels`/`messages`）。这验证了 4.3 节「上层不变、下层替换」的结论。
4. **定位差异源**：在源码里找到每个引擎调用 `template.set_mode(...)` 的那一行（vllm/sglang/lmdeploy/transformers 各一处），说明这一行就是「template 随后端自适应」的开关。
5. **(进阶) 认识 GRPOVllmEngine**：用 `grep -rn "GRPOVllmEngine" swift/` 找到它被实例化的唯一位置（`rollout.py`），确认它只服务 RL，从而区分「推理后端」与「rollout 后端」两个概念。

产出：一份一页表格，列出三后端在你的环境下的耗时，并附一句结论——在你的硬件上，多大批次以后 vLLM/SGLang 才比 transformers 划算。

> 待本地验证：耗时与「划算临界点」因硬件/模型而异，本实践重在建立测量方法，而非给出统一数字。

---

## 6. 本讲小结

- **一个开关四个发动机**：`--infer_backend`（`transformers`/`vllm`/`sglang`/`lmdeploy`）经 `SwiftInfer.get_infer_engine()` 工厂派发到对应引擎类，各后端专属参数由 `get_xxx_engine_kwargs()` 打包、懒加载导入以保护未装可选依赖的用户。
- **transformers 走特例**：`TransformersEngine` 直接持有 `nn.Module`，用 `model.generate` 生成，并用一个常驻工作线程把同步生成伪装成异步；它的任务覆盖最全（含 seq_cls/reranker/prm/embedding）。
- **三类加速后端差异在三处**：构造底层引擎的方式（`LLMEngine`/`sgl.Engine`/`lmdeploy.pipeline`）、同步还是异步、原生采样参数类型（`SamplingParams`/`dict`/`LmdeployGenerationConfig`）。
- **切换后端输入输出不变**：靠 `template.set_mode(...)` 让模板随后端自适应，加上各引擎里 `RequestConfig → 原生参数` 的翻译器，把所有后端差异封装在引擎层。
- **GRPOVllmEngine 是 RL 专用**：继承 `VllmEngine`，把输出包成 `RolloutOutput`、带上 `routed_experts`，并配合 `WeightSyncWorkerExtension` 与 `swift rollout` 管道实现权重的热同步，只在强化学习 Rollout 阶段使用。

---

## 7. 下一步学习建议

- **进入强化学习**：本讲的 `GRPOVllmEngine` 是 u7 单元（强化学习与 GRPO）的入口。建议接着学 **u7-l1 RLHF 训练流程**，理解 `SwiftRLHF` 如何在 SFT 之上接入 DPO/KTO/GRPO；再到 **u7-l2 GRPO 算法核心** 看 rollout 产出的样本如何被算优势与奖励。
- **深入 template 与后端的耦合**：若你对多模态推理感兴趣，可重读 u3-l4，并结合本讲 `set_mode` 与 `prepare_engine_kwargs` 的机制，理解同一张图片为何在不同后端下被预处理成不同形态。
- **部署与服务化**：若你的目标是把模型变成 API 服务，下一步直接学 **u8-l2 部署与服务化**，它讲解 `swift deploy` 如何基于 vLLM/SGLang 启动 OpenAI 兼容服务——那里会用到的 `DeployArguments` 会把 `vllm_use_async_engine` 默认设为 `True`，正好呼应本讲 4.1 节的异步引擎推断规则。
