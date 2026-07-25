# TorchLlmArgs 与配置层级

## 1. 本讲目标

在 u1-l3 中你已经会用 `LLM(model=...)` 跑推理、用 `trtllm-serve` 起服务；在 u2-l2 中你知道了 `import tensorrt_llm` 时会 re-export `LLM`、`SamplingParams`、`TorchLlmArgs` 等公共对象。但这些都回避了一个核心问题：**TensorRT-LLM 的全部运行时行为——并行度、调度策略、KV cache 显存、CUDA graph——到底是怎么表达和传递的？**

答案就是本讲的主角：**一个叫 `TorchLlmArgs` 的巨型 Pydantic 配置对象**。它是「一次配置、全程生效」的中枢。

学完本讲你应当能够：

1. 说清楚为什么 TensorRT-LLM 选 Pydantic（而不是 dataclass 或裸 dict）来做配置，以及它带来的「严格校验、类型强转、字段元数据、判别联合」四项能力。
2. 复述 `BaseLlmArgs → TorchLlmArgs` 的继承关系，并解释 `LlmArgs = TorchLlmArgs` 这个别名为何存在。
3. 在源码中定位四大子配置族——并行（`_ParallelConfig`）、调度（`SchedulerConfig`）、KV cache（`KvCacheConfig`）、CUDA graph（`CudaGraphConfig`）——并知道它们各自的默认值与作用。
4. 遵循 `CODING_GUIDELINES.md` 的 Pydantic 规范，安全地修改或扩展配置类。

---

## 2. 前置知识

在进入源码前，先用三个小例子建立直觉。

### 2.1 什么是 Pydantic

Pydantic 是 Python 的数据校验库。你定义一个继承 `BaseModel` 的类、声明字段类型，它就会在构造时做三件事：

- **类型强转**：传入字符串 `"1"` 给 `int` 字段，它能帮你转成 `1`；
- **校验**：类型不对或违反约束直接抛 `ValidationError`，绝不静默放过；
- **序列化**：自带 `model_dump()` 转 dict、`model_validate()` 从 dict 还原。

本讲涉及的配置类几乎都继承自 `StrictBaseModel`，它在 `BaseModel` 基础上设了 `extra = "forbid"`——**多传一个不认识的字段就报错**。这能第一时间抓住拼写错误（比如把 `tensor_parallel_size` 写成 `tensor_paralel_size`）。

### 2.2 配置对象的两个设计张力

读 `llm_args.py` 时，你会反复看到两种写法的权衡：

- **扁平（flat）**：最常用的旋钮（`tensor_parallel_size`、`max_num_tokens`）直接放在顶层，方便用户写、方便 IDE 补全。
- **嵌套（nested）**：成族的相关参数（KV cache、调度器、CUDA graph）打包成子配置类，避免顶层字段爆炸。

这两种风格在同一个类里**共存**，这是理解 `BaseLlmArgs` 的钥匙。

### 2.3 配置与 C++ 运行时的桥梁

回顾 u2-l3 的口诀「Python 调度、C++ 加速」：很多配置最终要喂给 C++ 运行时（调度器、KV cache 管理器）。本讲会看到配置侧的实现方式——`PybindMirror` 机制：Python 配置类镜像一个 C++ 结构体，并提供 `_to_pybind()` 把自己翻译成 C++ 对象。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tensorrt_llm/llmapi/llm_args.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py) | 配置中枢，6000+ 行，定义 `BaseLlmArgs`、`TorchLlmArgs` 及全部子配置族 |
| [tensorrt_llm/llmapi/utils.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/utils.py) | 定义 `StrictBaseModel`（`extra="forbid"` 的基类） |
| [tensorrt_llm/llmapi/__init__.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/__init__.py) | 聚合门面，把 `TorchLlmArgs` 及子配置族 re-export 为公共 API |
| [tensorrt_llm/llmapi/llm.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py) | `LLM.__init__`：按 `backend` 选择配置类、校验 kwargs、构造 `self.args` |
| [CODING_GUIDELINES.md](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/CODING_GUIDELINES.md) | 「Pydantic Guidelines」一节，改配置类前必读的规范 |
| [examples/configs/curated/qwen3.yaml](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/examples/configs/curated/qwen3.yaml) | 真实部署 YAML，演示扁平参数 + 嵌套子配置如何混用 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 BaseLlmArgs（公共基类）**、**4.2 TorchLlmArgs（PyTorch 特化）**、**4.3 子配置族（并行/调度/KV cache/CUDA graph）**。

---

### 4.1 BaseLlmArgs：后端无关的公共配置基类

#### 4.1.1 概念说明

`BaseLlmArgs` 收集**所有后端共享**的运行时参数：模型路径、tokenizer、并行度、批大小上限、KV cache、调度器、投机解码等。它刻意把大量常用参数**摊平到顶层**（源码注释原话是 "expanded here for less hierarchy"，即「为了减少层级而展开」）。

它的定位是「基类」：PyTorch 后端的 `TorchLlmArgs` 继承它并加上 PyTorch 专属字段；AutoDeploy 后端有自己的子类。这样公共参数只定义一次、避免重复。

#### 4.1.2 核心流程

`BaseLlmArgs` 的构造与校验流程：

1. **构造**：用户传 `LLM(model=..., tensor_parallel_size=2, **kwargs)`，`LLM.__init__` 把这些 kwargs 喂给 `BaseLlmArgs`（实为 `TorchLlmArgs`）。
2. **field_validator（字段级）**：在赋值前后对单字段做校验/强转，例如 `dtype`、`gpus_per_node`。
3. **model_validator（after，模型级）**：所有字段赋值完成后跑，做跨字段校验与派生状态初始化。最重要的一个是 `validate_parallel_config`，它把摊平的并行字段**组装**成内部的 `_parallel_config`。
4. **派生属性**：通过 `@property` 暴露只读视图，如 `parallel_config`、`speculative_model`。

这套「扁平字段 → 内部聚合模型 → 只读属性」的三段式，是 `BaseLlmArgs` 最值得学习的模式。

#### 4.1.3 源码精读

**严格的基类**。`StrictBaseModel` 在 `utils.py` 里只做一件事——禁止任意多余字段：

[tensorrt_llm/llmapi/utils.py:36-45](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/utils.py#L36-L45) —— `extra = "forbid"` 是「拼写错误立即报错」的根源。`BaseLlmArgs` 又显式重申了一次：

[tensorrt_llm/llmapi/llm_args.py:4107-4109](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L4107-L4109) —— `model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")`。`arbitrary_types_allowed=True` 是因为有些字段类型是普通 Python 对象（如 `tokenizer`、`mpi_session`），不是 Pydantic 原生类型。

**必备字段 `model`**。这是唯一没有默认值的字段，必须由用户提供：

[tensorrt_llm/llmapi/llm_args.py:4112-4115](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L4112-L4115) —— 模型路径或 HuggingFace Hub 名。

**摊平的并行度字段**。这是「扁平风格」的典型——`tensor_parallel_size`、`pipeline_parallel_size`、`context_parallel_size` 直接放在顶层：

[tensorrt_llm/llmapi/llm_args.py:4155-4188](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L4155-L4188) —— 用户写 `LLM(..., tensor_parallel_size=2)` 就能设并行度，不必再嵌套一层。

**扁平 → 内部聚合的魔法**。注意这些并行字段是「公开扁平字段」，但下游代码（如 `llm.py`）需要的是一个统一的 `parallel_config` 对象。`validate_parallel_config` 这个 `model_validator(mode="after")` 负责把扁平字段组装进一个**私有属性** `_parallel_config`（`_ParallelConfig` 类型）：

[tensorrt_llm/llmapi/llm_args.py:4499-4522](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L4499-L4522) —— 这里把 `moe_*` 字段的 `None` 归一化为 `-1`（`-1` 表示「交给 Mapping 自动计算」），然后用扁平字段构造 `_ParallelConfig`。

**只读属性暴露**。`_parallel_config` 是 `PrivateAttr`（不会出现在 `model_dump()` 里），对外只通过只读 property 暴露：

[tensorrt_llm/llmapi/llm_args.py:4443-4447](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L4443-L4447) —— `_parallel_config` 是私有属性，`parallel_config` 是只读 property。

**嵌套子配置字段**。与扁平并行字段对照，KV cache 和调度器则用嵌套风格：

[tensorrt_llm/llmapi/llm_args.py:4242-4243](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L4242-L4243) —— `kv_cache_config: KvCacheConfig = Field(default_factory=KvCacheConfig, ...)`。注意用的是 `default_factory` 而非 `default=KvCacheConfig()`——后者是「可变默认值」反模式（CODING_GUIDELINES 明确禁止）。

[tensorrt_llm/llmapi/llm_args.py:4281-4283](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L4281-L4283) —— `scheduler_config: SchedulerConfig = Field(default_factory=SchedulerConfig, ...)`。

**YAML 加载**。配置不仅能从 kwargs 构造，还能直接从 YAML 文件加载（`trtllm-serve --config xxx.yaml` 的基础）：

[tensorrt_llm/llmapi/llm_args.py:4453-4457](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L4453-L4457) —— `from_yaml` 用 `yaml.safe_load` 读成 dict，再 `cls(**config_dict)` 走标准 Pydantic 构造（从而享受全部校验）。

#### 4.1.4 代码实践

**实践目标**：亲手感受 `StrictBaseModel` 的 `extra="forbid"` 与扁平字段。

**操作步骤**：

1. 在装有 TensorRT-LLM 的环境里执行：

   ```python
   from tensorrt_llm.llmapi.llm_args import BaseLlmArgs
   # 1) 正常构造（只给必填的 model）
   args = BaseLlmArgs(model="/path/to/model")
   print(args.tensor_parallel_size)   # 1（默认）
   print(args.parallel_config.world_size)  # 1
   # 2) 故意拼错字段名
   try:
       BaseLlmArgs(model="x", tensor_paralel_size=2)  # 拼错 parallel
   except Exception as e:
       print("被拦截：", type(e).__name__)
   ```

2. 把 `tensor_parallel_size` 改成 `2`、`pipeline_parallel_size` 改成 `2`，再次构造，打印 `args.parallel_config.world_size`。

**需要观察的现象**：第 2 步会因为 `extra="forbid"` 抛 `ValidationError`；改对名字后 `world_size` 应等于 `tp * pp * cp = 2 * 2 * 1 = 4`。

**预期结果**：验证了「拼写错误立刻报错」与「扁平字段被聚合为 `parallel_config`」两点。

**注意**：如果本机没有 GPU 或未安装包，构造 `BaseLlmArgs` 可能因 `dtype` 校验器访问 `torch.cuda` 而失败，此时标注为「待本地验证」，可改为只阅读源码理解。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BaseLlmArgs` 把 `tensor_parallel_size` 做成扁平字段，而把 `kv_cache_config` 做成嵌套子配置？

> **答案**：`tensor_parallel_size` 是单值、超高频、几乎所有用户都会碰的旋钮，扁平化降低心智负担；`kv_cache_config` 是一族强相关的参数（显存比例、块重用、dtype 等），打包成子类可避免顶层字段爆炸，也便于整体传递与默认值管理。

**练习 2**：`validate_parallel_config` 用的是 `@model_validator(mode="after")` 而不是 `mode="before"`，为什么？

> **答案**：它需要读取已赋值的 `self.tensor_parallel_size` 等字段来组装 `_parallel_config`，必须在所有字段赋值完成后运行，所以用 `after`；`before` 时字段尚未解析，无法安全引用。

---

### 4.2 TorchLlmArgs：PyTorch 后端的特化

#### 4.2.1 概念说明

`TorchLlmArgs(BaseLlmArgs)` 在公共基类之上，叠加 **PyTorch 后端专属**的配置：CUDA graph、MoE、注意力后端、torch.compile、overlap scheduler、采样器类型等。由于 PyTorch 是默认后端，全仓库有一个关键别名：

[tensorrt_llm/llmapi/llm_args.py:6006](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L6006) —— `LlmArgs = TorchLlmArgs`。

这就是为什么 u2-l2 里 `__all__` 同时导出了 `LlmArgs` 和 `TorchLlmArgs`——它们是同一个类。AutoDeploy 后端则有自己的 `AutoDeployLlmArgs`（同样继承自 `BaseLlmArgs` 家族），由 `LLM.__init__` 按 `backend` 选择。

#### 4.2.2 核心流程

配置类是如何被选中并构造的？看 `LLM.__init__` 的「后端派发」：

1. 读 `backend` kwarg（`"pytorch"` / `"_autodeploy"`）。
2. 选配置类：pytorch → `TorchLlmArgs`；_autodeploy → `AutoDeployLlmArgs`。
3. **kwargs 白名单校验**：用 `llm_args_cls.model_fields.keys()` 检查每个 kwarg 都是合法字段。
4. 构造 `self.args = llm_args_cls(model=..., tokenizer=..., **kwargs)`。

#### 4.2.3 源码精读

**后端派发与 kwargs 校验**：

[tensorrt_llm/llmapi/llm.py:175-205](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L175-L205) —— 175-193 行按 `backend` 选 `llm_args_cls`；205 行 `self.args = llm_args_cls(...)` 完成构造。

[tensorrt_llm/llmapi/llm.py:196-203](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L196-L203) —— 用 `model_fields.keys()` 做 kwargs 白名单，非法字段直接抛 `ValueError`。这是 `StrictBaseModel` 之外的第二道防线（更早、更友好的报错）。

**PyTorch 专属字段**。`TorchLlmArgs` 最具代表性的是 CUDA graph 与注意力后端：

[tensorrt_llm/llmapi/llm_args.py:4818-4826](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L4818-L4826) —— `cuda_graph_config`，默认开启 decode 模式 CUDA graph（用 `default_factory=CudaGraphConfig`）。

[tensorrt_llm/llmapi/llm_args.py:4905-4912](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L4905-L4912) —— `attn_backend`，默认 `'TRTLLM'`。这里还能看到自定义 `Field` 包装器的痕迹：`status="beta"` 与 `telemetry=TelemetryField.categorical(...)`。

**自定义 Field 包装器**。上面提到的 `status` / `telemetry` 不是 Pydantic 原生参数，而是 TRT-LLM 自己包了一层 `Field`：

[tensorrt_llm/llmapi/llm_args.py:94-117](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L94-L117) —— 这个 `Field` 在原生 Pydantic `Field` 之上加了 `status`（`prototype`/`beta`/`deprecated`，标记字段成熟度）和 `telemetry`（是否纳入遥测、按哪种分类）两类元数据，塞进 `json_schema_extra`。读 `llm_args.py` 时看到 `status="prototype"` 就知道该字段「实验性、可能 breaking change」，不能在生产里依赖。

**docstring 自动生成**。因为字段太多，手写文档不现实，仓库用 `TorchLlmArgs` 的 schema 自动生成 docstring：

[tensorrt_llm/llmapi/llm_args.py:6008-6010](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L6008-L6010) —— `TORCH_LLMARGS_EXPLICIT_DOCSTRING` 由 `generate_api_docs_as_docstring` 从字段 schema 生成，挂到 `LLM` 类上作为参数文档。这就是为什么 `LLM` 的 docstring 能列出上百个参数——它们其实来自 `TorchLlmArgs`。

#### 4.2.4 代码实践

**实践目标**：理解 `LLM` 的「参数文档」其实来自 `TorchLlmArgs` 的 schema。

**操作步骤**：

1. 执行 `python -c "import tensorrt_llm; help(tensorrt_llm.LLM)"` 或在 REPL 里 `LLM?`。
2. 对照 [tensorrt_llm/llmapi/llm_args.py:4809](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L4809) 的 `TorchLlmArgs` 字段，确认 `LLM` docstring 里的 `cuda_graph_config`、`attn_backend`、`moe_config` 等条目来自这里。
3. 试着给 `LLM` 传一个 `attn_backend='FLASHINFER'`（前提是环境支持），观察启动日志里 attention 后端是否切换。

**需要观察的现象**：`LLM` 的 docstring 与 `TorchLlmArgs` 字段一一对应；非法后端值会在构造期报错。

**预期结果**：确认「`LLM(...)` 的全部参数 = `TorchLlmArgs` 的全部字段」这一对应关系。若本机无 GPU，第 3 步标为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`LlmArgs = TorchLlmArgs` 这个别名意味着什么？如果某天 AutoDeploy 成为默认后端，这行要怎么改？

> **答案**：意味着「`LlmArgs`」就是「PyTorch 后端的参数类」，因为 PyTorch 是默认后端。若 AutoDeploy 转正，这行要么改成 `LlmArgs = AutoDeployLlmArgs`，要么改成 `Union` /运行时按 backend 解析的别名；同时所有 `import LlmArgs` 的下游代码都要复核。

**练习 2**：`LLM.__init__` 已经用了 `StrictBaseModel` 的 `extra="forbid"`，为何还要在 196-203 行再做一次 kwargs 白名单校验？

> **答案**：两层防护面向不同时机与报错信息。196-203 行在构造**之前**就把非法 kwarg 拦下，给出 `got invalid argument: xxx` 的清晰提示；而 `extra="forbid"` 是构造时 Pydantic 层的兜底。前者更早、信息更友好，后者是最终保证。

---

### 4.3 子配置族：并行 / 调度 / KV cache / CUDA graph

#### 4.3.1 概念说明

本模块是本讲的重头戏。`BaseLlmArgs` / `TorchLlmArgs` 顶层引用了一大批子配置类，它们才是真正决定推理性能的旋钮。先一张总览表：

| 子配置类 | 在 `BaseLlmArgs/TorchLlmArgs` 中的字段 | 关键作用 | 是否 `PybindMirror` |
|----------|----------------------------------------|----------|---------------------|
| `_ParallelConfig` | `parallel_config`（派生私有） | TP/PP/CP/MoE 并行拓扑 | 否（纯 Python，转 `Mapping`） |
| `SchedulerConfig` | `scheduler_config` | 容量调度策略、等待队列、prefix-aware | 是（镜像 C++ `_SchedulerConfig`） |
| `KvCacheConfig` | `kv_cache_config` | KV cache 显存预算、块重用、dtype | 是（镜像 C++ `_KvCacheConfig`） |
| `CudaGraphConfig`（=`DecodeCudaGraphConfig`） | `cuda_graph_config` | CUDA graph 捕获的 batch size 列表 | 否（纯 PyTorch 概念） |

> 提示：`PybindMirror` 一栏「是」表示该配置最终要传给 C++ 运行时，因此 Python 类要和 C++ 结构体保持字段同步；「否」表示纯 Python 后端使用。

#### 4.3.2 核心流程

**PybindMirror 镜像机制**（贯穿调度与 KV cache 两个子配置）：

1. C++ 侧有一个结构体（如 `_SchedulerConfig`），经 nanobind 暴露。
2. Python 侧定义同名语义的 `StrictBaseModel` 子类，继承 `PybindMirror`，实现 `_to_pybind()` 把自己翻译成 C++ 对象。
3. `@PybindMirror.mirror_pybind_fields(_SchedulerConfig)` 装饰器在**类定义时**断言：C++ 类的每个字段在 Python 类里都存在。一旦 C++ 加字段而 Python 忘了加，**import 时就报错**——强制两端同步。

这是「Python 调度、C++ 加速」在配置层的落地：接口与默认值写在 Python（可读、可文档化、可遥测），高性能实现走 C++。

#### 4.3.3 源码精读

**并行拓扑 `_ParallelConfig`**。注意类名带下划线，表示**内部类**，不对外导出；用户只通过扁平字段和 `parallel_config` property 间接接触它：

[tensorrt_llm/llmapi/llm_args.py:1596-1609](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L1596-L1609) —— 字段：`tp_size`、`pp_size`、`cp_size`、`gpus_per_node`、MoE 三件套（`moe_cluster_size`/`moe_tp_size`/`moe_ep_size`，默认 `-1` 表自动）、`enable_attention_dp`。

[tensorrt_llm/llmapi/llm_args.py:1627-1629](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L1627-L1629) —— `world_size` 的计算：`tp_size * pp_size * cp_size`。

[tensorrt_llm/llmapi/llm_args.py:1648-1664](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L1648-L1664) —— `to_mapping()` 把 `_ParallelConfig` 转成 `Mapping` 对象（`Mapping` 是 u9-l1 的主角，描述每个 rank 的角色）。

**调度器 `SchedulerConfig`**。决定请求如何被接纳进 batch：

[tensorrt_llm/llmapi/llm_args.py:3347-3385](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L3347-L3385) —— 注意三点：① 装饰器 `@PybindMirror.mirror_pybind_fields(_SchedulerConfig)` 强制与 C++ 同步；② 默认容量策略是 `GUARANTEED_NO_EVICT`（保证不驱逐已在跑的请求，安全性优先）；③ `_to_pybind()` 把 Python 配置翻译成 C++ `_SchedulerConfig`。`enable_prefix_aware_scheduling` 默认 `True`，开启后会用 KV 前缀复用估计来辅助接纳决策。

`CapacitySchedulerPolicy` 是个镜像 C++ 的枚举：

[tensorrt_llm/llmapi/llm_args.py:3290-3297](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L3290-L3297) —— `MAX_UTILIZATION` / `GUARANTEED_NO_EVICT` / `STATIC_BATCH` 三选一，u8-l1 会深入讲它们的取舍。

**KV cache `KvCacheConfig`**。这是对显存占用影响最大的子配置：

[tensorrt_llm/llmapi/llm_args.py:3587-3617](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L3587-L3617) —— 核心字段：`enable_block_reuse`（默认 `True`，即前缀缓存/prefix caching）、`max_tokens`（缓存能装多少 token）、`free_gpu_memory_fraction`（默认 `0.9`，即把 90% 可用显分给 KV cache）。`max_tokens` 与 `free_gpu_memory_fraction` 同时给定时，取**较小者**对应的显存。

KV cache 显存预算可用一个简单关系描述（\(b_{\text{kv}}\) 为每 token 的 KV cache 字节数）：

\[
M_{\text{kv}} \;=\; \min\!\big(\;\text{free\_gpu\_memory\_fraction}\times M_{\text{free}},\;\; \text{max\_tokens}\times b_{\text{kv}}\;\big)
\]

而 `max_num_tokens`（在 `BaseLlmArgs` 顶层，默认 8192）则是**调度层**单步最多处理的 token 数预算——它与 KV cache 容量是两个不同概念：前者限制一步算多少，后者限制能存多少。

**CUDA graph `CudaGraphConfig`**。这里有个易混淆点：`CudaGraphConfig` 是个别名：

[tensorrt_llm/llmapi/llm_args.py:452-457](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L452-L457) —— `CudaGraphConfig = DecodeCudaGraphConfig`（为向后兼容）；而 `CudaGraphConfigType` 是按 `mode` 字段做判别的联合类型（decode / encode）。`TorchLlmArgs.cuda_graph_config` 用的就是这个联合类型。

CUDA graph 配置的精髓是「为哪些 batch size 预先捕获图」：

[tensorrt_llm/llmapi/llm_args.py:172-186](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L172-L186) —— `batch_sizes`（显式列表）、`max_batch_size`、`enable_padding`。`model_validator` 会保证 `batch_sizes` 与 `max_batch_size` 自洽：给了列表就取 `max(batch_sizes)`，只给上限就自动生成一串 batch size。CUDA graph 能大幅降低 decode 阶段的 kernel launch 开销，u10-l4 会专门讲。

**判别联合的范例 `SpeculativeConfig`**。最后看一个体现 Pydantic 威力的模式——`speculative_config` 字段能接受十几种不同的投机解码配置，靠 `decoding_type` 字段做判别：

[tensorrt_llm/llmapi/llm_args.py:3492-3510](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L3492-L3510) —— `Annotated[Union[...], Field(discriminator="decoding_type")]`。用户给 `{"decoding_type": "eagle3", ...}` 就解析成 `Eagle3DecodingConfig`，给 `{"decoding_type": "ngram", ...}` 就解析成 `NGramDecodingConfig`，互不干扰。这正是 CODING_GUIDELINES 推荐的多态配置写法。

**聚合门面**。这些子配置类都通过 `llmapi/__init__.py` 对外导出：

[tensorrt_llm/llmapi/__init__.py:9-27](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/__init__.py#L9-L27) —— 从 `llm_args` 批量 re-export `SchedulerConfig`、`KvCacheConfig`、`CudaGraphConfig`、`TorchLlmArgs` 等。所以用户可以 `from tensorrt_llm.llmapi import KvCacheConfig, SchedulerConfig` 直接构造子配置。

#### 4.3.4 代码实践

**实践目标**：读懂一份真实部署 YAML，把扁平参数与嵌套子配置对号入座。

**操作步骤**：阅读 [examples/configs/curated/qwen3.yaml](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/examples/configs/curated/qwen3.yaml)（全文仅 20 行）：

```yaml
max_batch_size: 161            # 扁平字段（BaseLlmArgs 顶层）
max_num_tokens: 1160           # 扁平字段（调度层 token 预算）
moe_expert_parallel_size: 1    # 扁平字段（并行）
cuda_graph_config:             # 嵌套子配置（TorchLlmArgs）
  enable_padding: true
  batch_sizes: [1, 2, 4, 8, 16, 32, 64, 128, 256, 384]
enable_attention_dp: true      # 扁平字段（并行）
kv_cache_config:               # 嵌套子配置（BaseLlmArgs）
  free_gpu_memory_fraction: 0.8
```

1. 把每个键对照 `TorchLlmArgs.model_fields`，确认它是扁平字段还是某个嵌套子配置的字段。
2. 解释：为什么 `free_gpu_memory_fraction` 写成 `0.8` 而不是默认的 `0.9`？（提示：这台机器要把更多显存留给模型权重，所以给 KV cache 少一点。）
3. 解释：`cuda_graph_config.batch_sizes` 列出这些值意味着什么？（提示：只为这些 batch size 预捕获 CUDA graph，其它 batch 走 eager 或 padding 到最近值。）

**需要观察的现象**：YAML 的结构与 `TorchLlmArgs` 的字段层级完全对应——顶层键是扁平字段，缩进的 `cuda_graph_config:` / `kv_cache_config:` 是嵌套子配置。

**预期结果**：你能不查文档，仅凭源码字段名解释这份 YAML 每一行的含义。

**进阶（可选）**：参考 `CODING_GUIDELINES.md` 的 Pydantic 规范，给本讲挑出的三个性能关键项做一张表：

| 配置项 | 所在子配置 | 默认值 | 对性能的影响 |
|--------|-----------|--------|-------------|
| `tensor_parallel_size` | `_ParallelConfig`（扁平） | `1` | 多卡切分权重与计算，降低单步延迟、提高吞吐 |
| `cuda_graph_config` | `CudaGraphConfig` | decode 模式开启 | 消除 decode 阶段 kernel launch 开销 |
| `kv_cache_config.free_gpu_memory_fraction` | `KvCacheConfig` | `0.9` | 决定能并发多少请求/多长上下文，直接关系吞吐 |

**修改配置类的注意事项**（依 [CODING_GUIDELINES.md:531-569](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/CODING_GUIDELINES.md#L531-L569) 的 Pydantic 规范）：

- 一律继承 `StrictBaseModel`，不要用 dataclass 或裸类。
- **不要自定义 `__init__`**，会绕过 Pydantic 校验；校验用 `@field_validator`/`@model_validator`，初始化私有状态用 `model_post_init()`，自定义构造用 classmethod（如 `from_yaml`）。
- 字段都要写 `Field(description=...)`；可变默认值必须用 `default_factory`，不能 `default=[]`。
- 用 `Literal[...]`、`PositiveInt`、`Field(ge=0)` 等表达约束，而非手写校验。
- **改完 LLM args 或嵌套配置，必须跑 `python3 scripts/generate_llm_args_golden_manifest.py` 并提交 `tensorrt_llm/usage/llm_args_golden_manifest.json`；新增字段需 telemetry/privacy CODEOWNER 审批。**
- 若该配置镜像了 C++ 类，记得同步两端字段，否则 `mirror_pybind_fields` 装饰器会在 import 时报错。

#### 4.3.5 小练习与答案

**练习 1**：`max_num_tokens` 和 `kv_cache_config.max_tokens` 都带 "tokens"，它们是一回事吗？

> **答案**：不是。`max_num_tokens`（`BaseLlmArgs` 顶层，默认 8192）是**调度层**单步前向最多处理的 token 数预算，限制一步算多少；`kv_cache_config.max_tokens` 是 **KV cache 容量**上限，限制历史能存多少 token。前者是算力/批大小旋钮，后者是显存旋钮。

**练习 2**：`SchedulerConfig` 加了 `@PybindMirror.mirror_pybind_fields(_SchedulerConfig)`，如果你给 C++ `_SchedulerConfig` 新增一个字段却忘了在 Python 侧加，会发生什么？什么时候发生？

> **答案**：会在 **import `llm_args` 模块时**（即类定义被解析、装饰器执行的那一刻）抛 `ValueError`，提示某字段未在 Python 类中镜像。这是设计上的「同步护栏」，确保 Python 接口与 C++ 实现不会悄悄分叉。

**练习 3**：`speculative_config` 用判别联合接受十几种配置，为什么不用一个带 `type` 字段的"大而全"配置类？

> **答案**：判别联合让每种投机解码方法（Eagle3 / MTP / N-gram / …）有自己独立的字段集合和校验，互不污染；解析时只校验命中的那个子类，类型更精确、错误更早更清晰。若用单个大类，要么字段冗余（多数方法用不到的字段也出现），要么校验逻辑塞满 `if-else`，可维护性差。

---

## 5. 综合实践

**任务**：为一个假想的 8 卡部署，从零写一份 `trtllm-serve --config` 用的 YAML，并解释每一行对应 `TorchLlmArgs` 的哪个字段/子配置。

要求：

1. 设 `tensor_parallel_size: 4`、`pipeline_parallel_size: 2`（共 8 卡）。
2. 给 `kv_cache_config.free_gpu_memory_fraction` 一个非默认值，并说明理由。
3. 给 `cuda_graph_config.batch_sizes` 一组合适的值，说明选择依据。
4. 给 `scheduler_config.capacity_scheduler_policy` 选一个策略并说明（参考 [llm_args.py:3290-3297](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py#L3290-L3297)）。

**参考骨架**（示例代码，非项目原有）：

```yaml
# my-serve.yaml —— 示例配置，对应 TorchLlmArgs 字段
tensor_parallel_size: 4          # _ParallelConfig.tp_size（扁平）
pipeline_parallel_size: 2        # _ParallelConfig.pp_size（扁平）
max_num_tokens: 8192             # 调度层 token 预算（扁平）
kv_cache_config:                 # KvCacheConfig
  free_gpu_memory_fraction: 0.85 # 给权重/激活留更多显存
  enable_block_reuse: true       # 前缀缓存
cuda_graph_config:               # CudaGraphConfig (DecodeCudaGraphConfig)
  enable_padding: true
  batch_sizes: [1, 2, 4, 8, 16, 32, 64, 128, 256]
scheduler_config:                # SchedulerConfig
  capacity_scheduler_policy: GUARANTEED_NO_EVICT
```

**自检**：

- 用 `python -c "import yaml; from tensorrt_llm.llmapi.llm_args import TorchLlmArgs; print(TorchLlmArgs.from_yaml('my-serve.yaml').parallel_config.world_size)"` 验证 `world_size = 4*2*1 = 8`（若缺 model 字段会报错——`from_yaml` 走的是完整 Pydantic 构造，`model` 是必填项；可临时加 `model: /path/to/model`）。
- 检查 YAML 里没有任何「多余字段」——否则 `extra="forbid"` 会报错。

**待本地验证**：上述命令需在装好 TensorRT-LLM 且能访问 GPU 的环境运行；纯阅读场景可只做字段对照。

---

## 6. 本讲小结

- TensorRT-LLM 用**一个巨型 Pydantic 对象** `TorchLlmArgs` 表达全部运行时参数，口诀是「一次配置、全程生效」；`LlmArgs = TorchLlmArgs` 是因为 PyTorch 是默认后端。
- 配置分两层继承：`BaseLlmArgs`（后端无关公共字段）→ `TorchLlmArgs`（PyTorch 专属）；AutoDeploy 有自己的子类，由 `LLM.__init__` 按 `backend` 派发。
- 风格上「扁平与嵌套共存」：高频旋钮（并行度、批大小）摊平到顶层，成族参数（KV cache、调度器、CUDA graph）打包成子配置。
- 「扁平 → 内部聚合」靠 `validate_parallel_config` 这类 `model_validator(mode="after")` 完成，对外只暴露只读 `parallel_config` property。
- 严格性来自 `StrictBaseModel` 的 `extra="forbid"`（拼写错误立刻报错），外加 `LLM.__init__` 的 kwargs 白名单二次校验；自定义 `Field` 包装器还标注了每个字段的 `status`（成熟度）与 `telemetry`。
- `PybindMirror` + `mirror_pybind_fields` 是「Python 接口、C++ 实现」的配置层落地：`SchedulerConfig`/`KvCacheConfig` 镜像 C++ 结构体并强制字段同步；改配置类须遵循 `CODING_GUIDELINES.md` 的 Pydantic 规范并重跑 golden manifest。

---

## 7. 下一步学习建议

- **u4-l2 模型默认值与 llm_utils**：本讲只讲了「用户显式传参」，下一讲讲「模型架构自带的默认优化参数」如何经 `apply_model_defaults_to_llm_args` 深度合并进来，覆盖本讲的默认值。
- **u4-l3 ModelConfig 与 PretrainedConfig**：区分「运行时参数 `TorchLlmArgs`」（本讲）与「模型权重/结构参数 `PretrainedConfig`/`QuantConfig`」。
- **u9-l1 Mapping 与并行策略**：本讲的 `_ParallelConfig.to_mapping()` 产出的 `Mapping` 对象是并行原语的主角，下一阶段深入。
- **u8-l1 调度器与 inflight batching**：本讲把 `SchedulerConfig` 当配置看，u8-l1 把它当运行机制拆。
- 继续阅读源码：[tensorrt_llm/llmapi/llm_args.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm_args.py) 中尚未覆盖的子配置（`MoeConfig`、`TorchCompileConfig`、各 `*DecodingConfig`），它们分别在 u10（MoE/量化/投机解码）中展开。
