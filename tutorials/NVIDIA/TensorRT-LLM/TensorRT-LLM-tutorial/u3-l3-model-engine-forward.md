# ModelEngine 与模型前向

## 1. 本讲目标

上一讲（u3-l2）我们拆开了 PyExecutor 的单步循环，看到每一步都会「调度 → 准备资源 → 前向 → 采样 → 处理响应」。其中「前向」这一步一直被当作黑盒，只用一句 `model forward` 带过。本讲就把这个黑盒打开。

学完本讲，你应该能够：

- 区分 `ModelEngine` 抽象基类与它的具体实现 `PyTorchModelEngine`，知道「接口在抽象类、实现在子类」的设计意图。
- 复述 `PyTorchModelEngine.forward()` 一次前向「需要哪些输入、内部做了什么、产出什么」。
- 解释 `ModelLoader` 如何从一个 HuggingFace（HF）checkpoint 目录，一步步变成一个可以前向的模型对象。
- 理解 `ModelConfig` 在运行时的角色：它把 HF 权重配置（`pretrained_config`）和推理引擎的各种运行时开关打包在一起，成为前向与加载之间的桥梁。

## 2. 前置知识

阅读本讲前，请确保你已经理解：

- **PyExecutor 单步循环**（u3-l2）：知道 `engine.forward(...)` 是单步循环里被调用的一环，它的输入是「调度后的请求 + 资源管理器」。
- **请求流图**（u3-l1）：知道请求身份沿链路演变为 `tllm.Request`，并理解 `ScheduledRequests`、`ResourceManager` 这些概念。
- **Python 调度、C++ 加速**（u2-l3 / u3-l1）：知道 kernel 与 C++ 运行时负责高性能，而编排逻辑在 Python。
- **基础 PyTorch 概念**：`nn.Module.forward()`、`@torch.inference_mode()`、CUDA Graph 的「捕获一次、反复回放」思路。

几个本讲会用到的术语，先做通俗解释：

| 术语 | 通俗解释 |
|------|----------|
| `ModelEngine` | 「模型引擎」抽象基类。它定义了引擎必须长什么样（要能前向、能 warmup），但不规定具体怎么实现。 |
| `PyTorchModelEngine` | `ModelEngine` 在 PyTorch 后端的具体实现，是本讲的主角。 |
| `ModelLoader` | 「模型加载器」。把磁盘上的 checkpoint 变成内存里、GPU 上、带好权重的模型对象。 |
| `ModelConfig` | 「模型运行时配置」。包裹 HF 的 `pretrained_config`，再额外携带量化、并行、后端选择等推理引擎关心的开关。 |
| logits | 模型最后一层输出的「每个词的未归一化分数」，形状一般是 `[num_tokens, vocab_size]`。采样就是从它里面挑 token。 |
| CUDA Graph 回放 | 把一整段 GPU 操作录制成一张「图」，之后每步直接重放，省掉反复「启动 kernel」的 CPU 开销。 |

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [model_engine.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/model_engine.py) | 定义 `ModelEngine` 抽象基类与 `PyTorchModelEngine` 实现，是本讲的核心。 |
| [model_loader.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/model_loader.py) | 定义 `ModelLoader`，负责从 checkpoint 加载、校验配置、装配权重。 |
| [model_config.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py) | 定义 `ModelConfig` 数据类，连接 HF 配置与运行时开关。 |
| [arch_overview.md](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/torch/arch_overview.md) | 官方架构总览，说明 ModelEngine / Decoder / Scheduler 三者的分工。 |

> 说明：本讲引用的行号基于 HEAD `4b7d719975`。`model_engine.py` 是一个超过 6600 行的大文件，我们只精读其中与前向直接相关的片段，不会逐行讲解。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **ModelEngine 抽象基类** —— 引擎的「契约」。
2. **PyTorchModelEngine 与前向流程** —— 契约在 PyTorch 后端的实现，本讲的重点。
3. **ModelLoader 与 ModelConfig** —— 模型如何被加载，以及配置如何在加载与前向之间传递。

这三个模块的关系可以用一句话串起来：

> PyExecutor 创建一个 `PyTorchModelEngine`；引擎在构造时借助 `ModelLoader` 从 checkpoint 加载出带 `ModelConfig` 的模型；之后 PyExecutor 每一步都调用 `engine.forward(...)`，让这个模型对当前批次的请求做一次前向，产出 logits。

### 4.1 ModelEngine 抽象基类

#### 4.1.1 概念说明

`ModelEngine` 是一个抽象基类（ABC，Abstract Base Class）。它回答一个问题：「一个模型引擎，至少要能做什么？」

答案是两件最核心的事：

- **能做单步前向**（`forward`）：给定调度好的请求和资源，跑一次模型，产出 logits 等。
- **能告诉调用方自己最多支持多少并发序列**（`get_max_num_sequences`）：调度器需要这个数字来决定能接多少请求。

除此之外，还有一个带默认实现（空操作）的 `warmup`：KV cache 管理器初始化完成后，引擎可以借这个机会做一些「预热」——比如捕获 CUDA Graph、跑 `torch.compile`、做 autotuning。子类可以按需覆盖它。

把这三个方法放在抽象基类里的好处是：PyExecutor 只依赖 `ModelEngine` 这个抽象，不关心底层是 PyTorch 还是别的实现。这为「换后端」留出了空间（例如 AutoDeploy 也是在 `PyExecutor` 这层做适配，详见 u12-l1）。

#### 4.1.2 核心流程

抽象类本身没有「流程」，它只规定契约。子类必须实现：

```text
ModelEngine (ABC)
  ├── forward(scheduled_requests, resource_manager, ...) -> dict   # 必须实现
  ├── get_max_num_sequences() -> int                              # 必须实现
  └── warmup(resource_manager) -> None                            # 可选覆盖，默认空
```

#### 4.1.3 源码精读

`ModelEngine` 的定义非常简洁，集中在一个地方：

`ModelEngine` 抽象基类与三个方法的声明 —— 这是引擎的契约：

[model_engine.py:95-117](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/model_engine.py#L95-L117)

其中 `forward` 是抽象方法，签名里列出了它的核心入参：`scheduled_requests`（调度后的请求）、`resource_manager`（资源管理器）、`new_tensors_device`（采样相关的新张量）等。注意 `forward` 本身是抽象的——真正干活的逻辑在子类里。

`warmup` 是一个有默认实现的具体方法（默认 `return`，即什么都不做）。注释明确说明它「在 KV cache manager 初始化之后」被调用，用于「实例化 CUDA graphs、运行 torch.compile 等」：

[model_engine.py:111-117](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/model_engine.py#L111-L117)

官方架构文档对它的定位也很直白——「持有语言模型，并高效地支持单步前向」：

[arch_overview.md:33-37](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/torch/arch_overview.md#L33-L37)

#### 4.1.4 代码实践

**实践目标**：通过源码阅读，确认 `ModelEngine` 抽象了哪些能力，并理解「抽象方法 vs 带默认实现的方法」的区别。

**操作步骤**：

1. 打开 `model_engine.py`，定位到第 95 行的 `class ModelEngine(ABC)`。
2. 列出标注了 `@abstractmethod` 的方法（`get_max_num_sequences`、`forward`）。
3. 找到没有 `@abstractmethod` 装饰的 `warmup` 方法，阅读其 docstring。

**需要观察的现象**：

- `forward` 和 `get_max_num_sequences` 的函数体都是 `raise NotImplementedError`——它们只声明签名，把实现责任留给子类。
- `warmup` 有真正的函数体（`return`），子类即使不覆盖它也不会报错。

**预期结果**：你能口头复述「引擎必须实现两件事（前向 + 最大并发数），可选实现一件（warmup）」。

#### 4.1.5 小练习与答案

**练习 1**：如果有一个新的推理后端想接入 PyExecutor，它最少需要实现 `ModelEngine` 的哪几个方法？

**参考答案**：必须实现 `forward` 和 `get_max_num_sequences`（两者都是 `@abstractmethod`）。`warmup` 可选，不覆盖时是空操作。

**练习 2**：为什么 `warmup` 不设成抽象方法？

**参考答案**：因为不是所有后端都需要预热（比如一个最简单的 eager 后端可能不需要捕获 CUDA Graph）。把它设成带默认空实现的具体方法，让「需要预热的后端覆盖它、不需要的后端忽略它」，降低了接入门槛。

### 4.2 PyTorchModelEngine 与前向流程

#### 4.2.1 概念说明

`PyTorchModelEngine` 是 `ModelEngine` 在 PyTorch 后端的实现，也是默认后端真正「跑模型」的地方。它身上挂着两顶帽子：

- **在构造时**：借助 `ModelLoader` 把模型加载进显存（详见 4.3）。
- **在推理时**：实现 `forward()`，对当前批次的请求执行一次高效前向。

`forward()` 的「高效」体现在两条路径的配合上：

1. **CUDA Graph 回放路径**：当批次的形状命中了预先捕获好的图时，直接 `replay`，省掉大量 CPU 侧的 kernel 启动开销。
2. **Eager（即时执行）路径**：当形状没命中图（比如需要首次捕获，或形状不在已捕获集合里）时，走普通的 PyTorch 即时执行 `_forward_step`。

无论走哪条路径，最终都汇聚到同一个核心动作：调用模型本身的 `self.model.forward(**inputs)`，产出 logits。

#### 4.2.2 核心流程

一次 `forward` 的主干可以概括为下面这张图（省略投机解码等可选分支）：

```text
engine.forward(scheduled_requests, resource_manager, ...)
   │
   ├─ 从 resource_manager 取 kv_cache_manager（含 draft 的）
   ├─ _set_up_attn_metadata(kv_cache_manager)        # 构造注意力元数据
   ├─ [可选] _set_up_spec_metadata(...)              # 投机解码元数据
   │
   ├─ cuda_graph_runner.pad_batch(...) -> padded_requests   # 把 batch 补齐到可图形状
   ├─ maybe_get_cuda_graph(...) -> key                     # 查当前形状有没有现成的图
   ├─ _prepare_inputs(...) -> inputs dict                  # 把请求变成模型能吃的张量
   │
   ├─ if 命中图（key is not None）:
   │      cuda_graph_runner.replay(key, inputs)             # 回放
   ├─ else:
   │      _forward_step(inputs)                             # eager 执行
   │           ├─ _preprocess_inputs(inputs)
   │           ├─ model_forward(**inputs)  →  self.model.forward(...)
   │           └─ 按 gather_ids 收集 logits
   │
   └─ return outputs  # 一个 dict，核心键是 'logits'
```

输入与产出一句话总结：

- **输入**：`ScheduledRequests`（调度后的请求集合）+ `ResourceManager`（资源管理器，含 KV cache）。`forward` 内部会把它们「翻译」成一个 `inputs` 字典（包含 `input_ids`、`position_ids`、`attn_metadata` 等）。
- **产出**：一个字典 `outputs`，最重要的键是 `'logits'`（形状约 `[num_tokens, vocab_size]`），交给后续的采样/解码使用。

#### 4.2.3 源码精读

**(1) `forward` 的签名与装饰器**。它用 `@torch.inference_mode()` 包住（推理阶段不需要梯度），并通过 `with_model_extra_attrs` 给模型注入「额外属性」：

[model_engine.py:6008-6017](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/model_engine.py#L6008-L6017)

**(2) 取资源 + 构造注意力元数据**。开头先从 `resource_manager` 取 KV cache 管理器，再调用 `_set_up_attn_metadata` 把它和当前请求绑成注意力元数据（注意力后端靠它知道每条请求的 KV 在缓存里的位置）：

[model_engine.py:6018-6024](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/model_engine.py#L6018-L6024)

**(3) 两条路径的分流**。当存在 KV cache 管理器时，`forward` 会尝试用 CUDA Graph：先用 `pad_batch` 把当前批次补齐，再查 `maybe_get_cuda_graph` 拿到一个 `key`。`key is not None` 表示有现成图可回放，否则走 eager：

[model_engine.py:6100-6167](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/model_engine.py#L6100-L6167)

注意 `6152` 行的 `_prepare_inputs` 才是真正把请求「翻译」成张量的地方，产出的 `inputs` 字典随后既给 eager 路径，也给图回放路径用。

**(4) Eager 路径与图回放路径都通向 `_forward_step`**。当 `can_run_graph` 为假时，直接调用 `_forward_step`；为真且需要捕获时，把 `_forward_step` 作为「待捕获函数」交给 `cuda_graph_runner.capture`，之后用 `replay` 反复执行：

[model_engine.py:6161-6209](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/model_engine.py#L6161-L6209)

**(5) `_forward_step` 的三步**。先 `_preprocess_inputs` 修正一些张量（例如重叠调度下更新 position_ids / kv_lens），再调用 `model_forward` 拿到 logits，最后按投机解码的 `gather_ids` 收集 logits：

[model_engine.py:6241-6277](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/model_engine.py#L6241-L6277)

**(6) `model_forward` —— 真正调用模型**。它把注意力/投机元数据写进模型的 `extra_attrs`，然后调用 `self.model.forward(**kwargs)`。这一行就是「Python 编排」与「具体模型计算」的分界线，之后的逐层前向（embedding → decoder layers → lm head）都发生在 `self.model` 内部：

[model_engine.py:6222-6239](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/model_engine.py#L6222-L6239)

> 补充：`forward` 还会返回一个 `outputs` 字典。从 `_forward_step` 可以看到，当模型返回单个张量时会被包成 `{'logits': logits}`，所以无论模型内部返回什么，对外的产出都是一个含 `'logits'` 键的字典。

#### 4.2.4 代码实践

**实践目标**：阅读 `forward` 的签名与 `_forward_step` 的实现要点，写出一段说明「一次前向需要哪些输入、产出什么」。

**操作步骤**：

1. 打开 `model_engine.py:6010` 的 `forward` 方法，抄下它的全部参数。
2. 打开 `model_engine.py:6242` 的 `_forward_step`，观察它如何调用 `model_forward`。
3. 打开 `model_engine.py:6222` 的 `model_forward`，确认最终调用的是 `self.model.forward(**kwargs)`。

**需要观察的现象**：

- `forward` 的「显式输入」是 `scheduled_requests`、`resource_manager` 以及几个采样/投机解码相关的张量；它并不直接接收 `input_ids`，而是内部 `_prepare_inputs` 把请求翻译成张量字典。
- `_forward_step` 的输入是 `inputs: Dict[str, Any]`，里面才是模型真正要的东西。
- 输出统一是字典，`'logits'` 是核心键。

**预期结果**：你能写出类似下面这段说明（请用自己的话改写）：

> 一次前向的「外部输入」是调度后的请求集合与资源管理器；引擎内部先构造注意力元数据，再把请求翻译成 `inputs` 字典（含 `input_ids`、`position_ids`、`attn_metadata` 等）。随后要么回放 CUDA Graph、要么走 eager `_forward_step`，二者最终都调用 `self.model.forward(**inputs)`。产出一个字典，核心是 `'logits'` 张量，供后续采样使用。

**待本地验证**：若你想确认实际运行时的 `inputs` 到底有哪些键，可在本机能跑的模型上给 `model_forward`（`model_engine.py:6222`）临时加一行 `logger.info(list(kwargs.keys()))`，跑一次推理观察打印。注意：临时调试日志不应提交。

#### 4.2.5 小练习与答案

**练习 1**：`forward` 为什么不直接接收 `input_ids`，而要接收 `scheduled_requests`？

**参考答案**：因为引擎需要在「请求」这一层做很多事：判断每条请求是 prefill 还是 decode、决定 KV cache 的块映射、为投机解码准备 draft token 等。`scheduled_requests` 携带了这些上下文，引擎才能在 `_prepare_inputs` 里把它翻译成形状正确的张量字典。直接传 `input_ids` 会丢失这些信息。

**练习 2**：eager 路径和 CUDA Graph 回放路径，最终调用模型的代码是同一处吗？

**参考答案**：是。图捕获时用的「待捕获函数」就是 `_forward_step`（见 `6172-6177` 行的 `capture_forward_fn`），它内部调 `model_forward` → `self.model.forward`。所以两条路径殊途同归，都汇聚到对模型 `forward` 的同一次调用，区别只在「是否把这次调用录制成了可回放的图」。

**练习 3**：`get_max_num_sequences` 返回什么？它的值怎么算？

**参考答案**：返回引擎支持的最大并发序列数，PyExecutor 用它推算 `max_num_active_requests`。计算式是 `pp_size * batch_size`（流水线并行度乘以单段批大小）：

[model_engine.py:2750-2755](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/model_engine.py#L2750-L2755)

### 4.3 ModelLoader 与 ModelConfig

#### 4.3.1 概念说明

前面看到 `forward` 依赖 `self.model`。那 `self.model` 是怎么来的？答案在 `PyTorchModelEngine.__init__`：它创建一个 `ModelLoader`，调用 `model_loader.load(...)` 拿到模型。

`ModelLoader` 是「模型加载器」，职责是把磁盘上的 checkpoint 目录变成一个「GPU 上、带权重、可前向」的模型对象。它的核心方法 `load` 做四件事：

1. **加载并校验配置**：读 HF 的 `config.json`，组装出 `ModelConfig`。
2. **按配置建模型骨架**：用 `AutoModelForCausalLM.from_config(config)` 实例化模型（先用 meta 张量占位，再落到 CUDA）。
3. **装填权重**：从 checkpoint 读权重，映射进模型（不同 `load_format` 走不同分支：`AUTO`/`GMS`/`DUMMY`/`VISION_ONLY`）。
4. **post-load 收尾**：跑权重变换、别名设置等模块级钩子。

而 `ModelConfig` 是「模型运行时配置」。它**包裹** HF 的 `pretrained_config`（描述模型权重本身的结构，如层数、隐层维度），再额外携带推理引擎关心的运行时开关：量化配置、并行映射（`mapping`）、注意力后端（`attn_backend`）、MoE 后端（`moe_backend`）等。可以把 `ModelConfig` 理解成「HF 权重配置 + 引擎运行时开关」的组合包。

一个关键设计：`ModelConfig` 在 `from_pretrained` 创建完成后会被「冻结」（`_frozen = True`），之后大部分字段不可修改，防止运行期被意外篡改。

#### 4.3.2 核心流程

加载链路（从 PyExecutor 视角看）：

```text
PyTorchModelEngine.__init__(model_path, llm_args, mapping, ...)
   │
   ├── 构造 checkpoint_loader（_construct_checkpoint_loader）
   ├── 创建 ModelLoader(llm_args, mapping, ...)
   └── model, moe_lb = model_loader.load(checkpoint_dir, checkpoint_loader)
         │
         ├── 1. _load_and_validate_config(...)
         │       └── checkpoint_loader.load_config(...)
         │             └── ModelConfig.from_pretrained(checkpoint_dir, ...)  → ModelConfig（冻结）
         ├── 2. AutoModelForCausalLM.from_config(config)  → DecoderModelForCausalLM（骨架，meta → cuda）
         ├── 3. 按 load_format 装填权重：
         │       AUTO   → checkpoint_loader.load_weights(...) → model.load_weights(...)
         │       DUMMY  → initialize_dummy_weights(model)
         │       GMS    → 共享显存池零拷贝
         │       VISION_ONLY → 复用已加载的视觉权重
         └── 4. post-load 钩子：_walk_full_post_load → 各模块 post_load_weights()
   │
   └── self.model = model   # 之后 forward 用到的就是它
```

之后在运行期，引擎通过 `self.model.model_config` 访问 `ModelConfig`，例如取 `pretrained_config` 或 `quant_config`。这样「加载期产出的配置」就和「前向期的开关」连起来了。

#### 4.3.3 源码精读

**(1) 引擎在构造时调用 `ModelLoader.load`**。下面这段展示了引擎如何创建加载器、调用 `load`，并把返回的模型挂到 `self.model` 上：

[model_engine.py:410-416](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/model_engine.py#L410-L416)

可以看到 `load` 返回两个东西：模型本体和（可能的）MoE 负载均衡器。

**(2) `ModelLoader.load` 的主流程**。`_load_and_validate_config` 产出配置；随后在 `timing` 与 `maybe_create_moe_load_balancer` 上下文里，先用 `AutoModelForCausalLM.from_config(config_copy)` 建模型骨架（优先走 meta 初始化 `MetaInitMode`，失败则回退普通初始化）：

[model_loader.py:491-509](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/model_loader.py#L491-L509)

> meta 初始化的含义：先用「没有真实数据、只记形状」的 meta 张量把整棵模块树搭起来，再统一分配显存、装权重。这能避免「先在 CPU 建大权重再搬运到 GPU」的浪费。

**(3) 权重装填（以 `AUTO` 格式为例）**。读权重、拿到 weight_mapper、调用 `model.load_weights`，最后还要处理「需要单独加载 draft 模型权重」的投机解码情形：

[model_loader.py:616-674](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/model_loader.py#L616-L674)

如果只是想用随机权重测试模型结构而不需要真实 checkpoint，则走 `DUMMY` 分支：

[model_loader.py:927-933](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/model_loader.py#L927-L933)

**(4) post-load 收尾**。所有格式装完权重后，统一跑 post-load 钩子（`_walk_full_post_load` 会遍历模块，调用各自的 `post_load_weights`，用来做融合 QKV、量化缩放等一次性变换）：

[model_loader.py:945-996](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/model_loader.py#L945-L996)

**(5) `ModelConfig` 的结构**。它是一个带泛型参数 `Generic[TConfig]` 的 dataclass，核心字段是 `pretrained_config`（HF 配置）、`mapping`（并行）、`quant_config`（量化），外加一长串运行时开关（`attn_backend`、`moe_backend`、`use_cuda_graph` 等）：

[model_config.py:131-211](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py#L131-L211)

**(6) 冻结机制**。`__setattr__` 在 `_frozen=True` 时拒绝修改大多数字段（只放行 `extra_attrs`、`pretrained_config`、`quant_config` 等少数）：

[model_config.py:213-228](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py#L213-L228)

**(7) `from_pretrained` —— 配置的工厂方法**。它读 HF 配置、解析量化配置（modelopt / hf / dtypes 多种格式）、解析 MoE 后端，最后构造并冻结 `ModelConfig`：

[model_config.py:821-843](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py#L821-L843)（方法签名与文件锁开头）

结尾处把 `_frozen` 置为 `True` 再返回：

[model_config.py:1197-1202](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/model_config.py#L1197-L1202)

**(8) 运行期如何用 `ModelConfig`**。前向相关代码里，引擎通过 `self.model.model_config` 取配置，例如读 KV cache 元素大小：

[model_engine.py:825-836](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/model_engine.py#L825-L836)

这里 `self.model.model_config.quant_config` 就是加载期写进 `ModelConfig` 的量化配置——这就是「加载与前向通过 ModelConfig 传递信息」的实证。

#### 4.3.4 代码实践

**实践目标**：在 `model_loader.py` 中追踪 `ModelLoader` 如何从 HF checkpoint 得到可前向的模型，理清「配置 → 骨架 → 权重 → 收尾」四步。

**操作步骤**：

1. 打开 `model_loader.py:476` 的 `ModelLoader.load` 方法。
2. 依次定位：`_load_and_validate_config`（`1307` 行）、`AutoModelForCausalLM.from_config`（`501` 行）、`AUTO` 权重分支（`616` 行）、post-load 钩子（`945` 行）。
3. 打开 `model_config.py:821` 的 `ModelConfig.from_pretrained`，浏览它如何根据 `hf_quant_config.json` / `config.json` 里的量化字段决定 `quant_algo`。

**需要观察的现象**：

- `load` 把配置加载、模型实例化、权重装填、post-load 钩子按固定顺序串起来，每一步都依赖上一步的产物。
- `from_pretrained` 末尾的 `model_config._frozen = True` 说明返回的配置是只读的。
- 不同 `load_format`（`AUTO`/`GMS`/`DUMMY`/`VISION_ONLY`）对应完全不同的权重来源分支，但都汇入同一套 post-load 收尾。

**预期结果**：你能画出一张「`load` 内部四步流水线」的草图，并指出 `ModelConfig` 在第一步被创建、在第 2 步被 `from_config` 消费、在运行期被 `self.model.model_config` 读取。

**待本地验证**：`from_pretrained` 会根据 checkpoint 实际携带的量化文件（`hf_quant_config.json` / `dtypes.json` / 内联 `quantization_config`）走不同分支。若要确认某个具体模型走了哪条分支，可在本机加载该模型时观察日志（代码里有大量 `logger.info`），这是最可靠的确认方式。

#### 4.3.5 小练习与答案

**练习 1**：`ModelConfig` 和 HF 的 `pretrained_config` 是什么关系？为什么不直接用 `pretrained_config`？

**参考答案**：`ModelConfig` 包含一个 `pretrained_config` 字段，但额外携带了推理引擎才关心的运行时开关（`mapping`、`quant_config`、`attn_backend`、`moe_backend`、`use_cuda_graph` 等）。`pretrained_config` 只描述「权重长什么样」，而引擎还需要知道「用哪种注意力后端、哪种 MoE 后端、是否开 CUDA Graph」等，这些不属于 HF 权重本身，所以需要再包一层。

**练习 2**：`ModelConfig` 创建后还能随意改字段吗？为什么这样设计？

**参考答案**：不能。`from_pretrained` 末尾会把 `_frozen` 置 `True`，之后 `__setattr__` 会拒绝修改大多数字段（只放行极少数）。这样设计是为了防止运行期被意外篡改——配置一旦定下来，前向、KV cache 大小、量化路径都依赖它稳定不变。

**练习 3**：`load_format=DUMMY` 有什么用？

**参考答案**：用随机小权重填充模型（`initialize_dummy_weights`），不读真实 checkpoint。它适合在只想验证模型结构、调度链路、CUDA Graph 捕获逻辑而不需要真实推理结果时使用（例如部分单测）。真实部署应使用 `AUTO`。

## 5. 综合实践

**任务**：把本讲三个模块串起来，画出一张「从 checkpoint 到一次前向 logits」的完整时序图，并配文字说明。

具体步骤：

1. **加载段**：以 `PyTorchModelEngine.__init__` 为起点，画出它如何 `ModelLoader.load` → `ModelConfig.from_pretrained` → `AutoModelForCausalLM.from_config` → 装权重 → post-load → 得到 `self.model`。标注每一步发生在「引擎构造期」。
2. **配置传递**：在图上单独标出 `ModelConfig`，用箭头说明它「在加载期被创建 → 被建模型时消费 → 在运行期被 `self.model.model_config` 读取」。
3. **前向段**：以 `engine.forward(scheduled_requests, resource_manager)` 为起点，画出 `_set_up_attn_metadata` → `_prepare_inputs` →（CUDA Graph 回放 或 eager `_forward_step`）→ `model_forward` → `self.model.forward(**inputs)` → `{'logits': ...}`。标注「eager 与图回放最终汇聚到同一次 `model.forward`」。
4. **衔接检查**：在图上标出 `self.model` 这个变量，确认它既是加载段的产物，又是前向段的依赖——这正是两个模块的连接点。

**验收标准**：

- 图上能清楚区分「构造期（一次性）」与「每步前向（反复执行）」两段。
- 能指出 `ModelEngine`（抽象契约）、`PyTorchModelEngine`（实现）、`ModelLoader`（加载）、`ModelConfig`（配置）各自的位置。
- 能口头解释为什么「换后端只需要实现 `ModelEngine` 的抽象方法，而加载逻辑可以复用」。

**待本地验证**：若想给图加上真实的张量形状，可在本机能跑的模型上，临时在 `_prepare_inputs` 返回处打印 `inputs` 各键的形状、在 `_forward_step` 返回处打印 `outputs['logits']` 的形状（仅本地调试，勿提交）。这能把图里的抽象箭头落成具体数字。

## 6. 本讲小结

- `ModelEngine` 是引擎的抽象契约：必须实现 `forward` 与 `get_max_num_sequences`，可选覆盖 `warmup`。PyExecutor 只依赖这个抽象，与具体后端解耦。
- `PyTorchModelEngine.forward()` 一次前向的输入是 `ScheduledRequests` + `ResourceManager`，内部先构造注意力元数据、再把请求翻译成张量字典，最后产出含 `'logits'` 的字典。
- 前向有两条路径：CUDA Graph 回放（命中已捕获形状时省 CPU 开销）与 eager `_forward_step`；二者最终都汇聚到同一次 `self.model.forward(**inputs)`。
- `ModelLoader.load` 把 checkpoint 变成可前向的模型，分四步：加载并校验配置（产出 `ModelConfig`）→ 建模型骨架 → 装填权重 → post-load 收尾。
- `ModelConfig` 是「HF 权重配置 + 引擎运行时开关」的组合包，创建后即冻结；它连接了加载期（`from_pretrained`）与前向期（`self.model.model_config`）。
- 本讲把 u3-l2 单步循环里被当作黑盒的「前向」彻底打开，下一层的黑盒是 `self.model.forward` 内部的逐层前向（embedding / decoder layers / lm head），那是 u5（模型与注册机制）的内容。

## 7. 下一步学习建议

- **横向（调度与采样）**：本讲只讲了「前向」。如果你想看前向产出的 logits 如何变成 token，继续读 u8-l3（Decoder 与 Sampling）。
- **纵向（模型内部）**：若想钻进 `self.model.forward` 内部，看逐层前向与 `DecoderModelForCausalLM` 范式，进入 u5-l1（模型架构范式：Config + ForCausalLM）。
- **性能（前向加速）**：本讲多次提到 CUDA Graph 与 `torch.compile`，它们的捕获/编译细节在 u10-l4（CUDA Graph 与 torch.compile / piecewise）展开。
- **推荐阅读顺序**：如果只想把「请求 → token」整条链路看完，建议 u3-l3（本讲）→ u8-l3（采样）→ u5-l1（模型内部）；如果更关心性能，则 u3-l3 → u10-l4。
