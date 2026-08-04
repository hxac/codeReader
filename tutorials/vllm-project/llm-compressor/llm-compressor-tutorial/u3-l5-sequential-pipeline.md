# SequentialPipeline 逐层校准深析

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `SequentialPipeline` 为什么要把模型切成「子图」、为什么每个子图要跑**两遍前向**。
- 解释 `sequential_targets` 是如何被推断出来的（`infer_sequential_targets`），以及 `trace_subgraphs` 如何用 `torch.fx` 把整模型拓扑切分成可执行子图。
- 读懂「校准 pass / 传播 pass」两遍前向各自的职责，以及 `propagate_error`、`AutoRoundModifier` 的特殊处理。
- 理解 `IntermediatesCache` 如何在 batch 与子图之间 offload / onload 中间激活，从而把峰值显存压到一个子图级别。
- 能动手跑一次 GPTQ 量化，从日志里数出子图数量、每个子图的前向次数，并通过 `sequential_offload_device` 观察显存变化。

## 2. 前置知识

本讲假设你已经学过 **u3-l4（校准管线选择与 CalibrationPipeline）**，知道 `sequential` 只是四种校准管线之一（另三种是 `independent` / `basic` / `datafree`），并由 `_infer_pipeline` 在 `any(m.requires_calibration_data)` 为真时被选中。此外需要回忆以下几讲的概念：

- **u2-l2 / u2-l3 的生命周期**：管线在固定时机触发 `CALIBRATION_START` → `SEQUENTIAL_EPOCH_END` → `CALIBRATION_END` 三个事件，每个事件会被广播到 modifier 的 `on_*` 钩子。
- **u3-l2 / u3-l3 的校准 hook**：`QuantizationMixin.start_calibration` 会用 `HooksMixin.register_hook` 给目标模块挂上前向 hook；其中 `calibrate_output_hook` 不仅收集统计，还会调用 `forward_quantize` 对输出做**伪量化**——这是量化误差跨层传播的关键。

两个通俗的直觉：

1. **为什么要逐层校准？** 一次性对全模型量化时，每一层的量化误差会同时产生并相互叠加，校准数据无法「看见」真实的累积误差。逐层校准让每一层都基于「上一层已经量化后」的真实输出来估计自己的激活范围，从而更贴近最终部署时的误差分布。
2. **为什么显存是个大问题？** 校准时需要把所有 batch 的中间激活都留着（下一层要用）。一个 70B 模型、512 个样本、2048 序列长度的中间激活，轻易就是上百 GB。解决办法是把这些激活 offload 到 CPU（或另一张卡），只在用到时 onload 回 GPU。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [pipeline.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py) | `SequentialPipeline.__call__`：切子图、两遍前向、缓存中间激活的总编排 |
| [helpers.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/helpers.py) | `trace_subgraphs` + `Subgraph` + 拓扑划分等子图切分逻辑 |
| [cache.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/cache.py) | `IntermediatesCache`：中间激活的 offload/onload/prefetch 缓存 |
| [utils/pytorch/module.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/pytorch/module.py) | `infer_sequential_targets` / `get_no_split_params`：推断切分层 |
| [args/dataset_arguments.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/dataset_arguments.py) | `sequential_targets` / `sequential_offload_device` / `propagate_error` 等参数定义 |
| [modifiers/quantization/calibration.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py) | `calibrate_output_hook`（误差传播的真正发生点） |

---

## 4. 核心概念与源码讲解

### 4.1 SequentialPipeline 全景：三步流程与 sequential_targets 推断

#### 4.1.1 概念说明

`SequentialPipeline` 是 llm-compressor 里**最常用、也最复杂**的校准管线：所有需要校准数据的算法（GPTQ、W8A8 激活量化、AWQ、SmoothQuant 等）默认都走它（参见 u3-l4 的 `_infer_pipeline`）。

它对自己要做什么，在源码 docstring 里写得非常清楚——这是本讲最重要的一段话，建议逐字读：

[pipeline.py:L60-L81](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L60-L81) —— SequentialPipeline 的三步自述。

把它翻译成三步：

1. **切子图**：按 `sequential_targets` 把模型划分成若干子图（subgraph）。
2. **两遍前向**：数据**依次**流过每个子图；每个子图跑两遍——第一遍触发校准 hook（收集统计），第二遍捕获「量化之后」的激活。
3. **缓存 + offload**：子图之间的中间激活被缓存，并在 batch 之间 offload 到 CPU 以省显存。

一个关键前提：管线**要求模型可被 `torch.fx` 追踪（traceable）**。docstring 里特别提醒，视觉模型配合视觉数据集常常因为特殊的输入处理而无法追踪，此时会抛 `torch.fx.proxy.TraceError`；解决办法是用 `llmcompressor.transformers.tracing` 把不可追踪的函数包起来。

#### 4.1.2 核心流程

`__call__` 是一个被 `@handle_sequential_oom` 装饰的静态方法（OOM 时会给出改 `sequential_targets` 的友好提示）。它的骨架可以概括为 6 步：

```
1. 设备准备        onload_device=主GPU, offload_device=sequential_offload_device
2. 推断切分层      infer_sequential_targets(model, sequential_targets)
3. 追踪切子图      trace_subgraphs(model, sample_input, targets, ignore, ...)
4. 触发校准开始    LifecycleCallbacks.calibration_start()   # 挂校准 hook
5. 逐子图循环      for subgraph in subgraphs:
                     pass1 触发 hook + sequential_epoch_end (量化权重)
                     pass2 (仅 propagate_error) 关 hook 捕获量化后输出 → 写缓存
6. 触发校准结束    LifecycleCallbacks.calibration_end()     # 卸 hook、freeze
```

其中第 2 步「推断切分层」是理解子图数量的钥匙。`infer_sequential_targets` 的逻辑很简单：用户传了 `sequential_targets` 就用它（字符串包成列表），没传就从 HuggingFace 模型自带的 `_no_split_modules`（通常是 `DecoderLayer`）推断。

[utils/pytorch/module.py:L151-L172](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/pytorch/module.py#L151-L172) —— `None` 时回退到 `get_no_split_params(model)`。

[utils/pytorch/module.py:L131-L148](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/pytorch/module.py#L131-L148) —— `get_no_split_params` 读取模型的 `_no_split_modules`。

> 直觉：对一个 32 层的 Llama，`_no_split_modules` 一般是 `LlamaDecoderLayer`，于是 sequential 管线会沿这些 decoder 层把模型切成 ~33 个子图（多出来的 1 个是开头的 embedding/mask 等「头部」，源码注释里有说明）。所以「子图数 ≈ decoder 层数 + 1」是本讲最实用的经验法则。

#### 4.1.3 源码精读

注册与类声明——`@CalibrationPipeline.register("sequential")` 把它登记进 u3-l4 讲过的注册表：

[pipeline.py:L51-L59](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L51-L59) —— 注册名 `"sequential"`，签名 `(model, dataloader, dataset_args)`。

设备准备与 AutoRound 特例：

[pipeline.py:L82-L99](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L82-L99) —— `onload_device` 取自主设备，`offload_device` 来自 `dataset_args.sequential_offload_device`（默认 `"cpu"`）。注意 L92-L94 的特殊处理：若 recipe 里含 `AutoRoundModifier`，则强制 `propagate_error = False`（原因见 4.3）。

切子图并触发校准开始：

[pipeline.py:L103-L113](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L103-L113) —— 用 dataloader 的第一个 batch 作 `sample_input` 来追踪，得到 `subgraphs` 列表；随后 `calibration_start()` 触发 `CALIBRATION_START` 事件（→ `on_calibration_start` → 挂校准 hook）。

入口上下文：

[pipeline.py:L115-L134](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L115-L134) —— 两个上下文 + 缓存初始化。

- `calibration_forward_context(model)`：关梯度、关 KV cache、切 eval、关自定义 kernel、把 `lm_head` 挪到 meta 设备省显存（[helpers.py:L120-L137](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/helpers.py#L120-L137)）。
- `DisableQuantization(model)`：在整段校准前向期间，关闭 compressed-tensors 在 forward 内的**自动量化**，把量化的发生权交给管线显式控制——激活伪量化由校准 hook 负责（pass 1），权重的量化参数在 `sequential_epoch_end` 由 modifier 显式 `observe + update_qparams` 写入（[helpers.py:L79-L88](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/helpers.py#L79-L88)）。
- `IntermediatesCache.from_dataloader(...)`：把整个 dataloader 的所有 batch 一次性 offload 进缓存（4.4 详讲）。

收尾：

[pipeline.py:L181](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L181) —— 所有子图跑完后，`calibration_end()` 触发 `CALIBRATION_END`（→ `on_calibration_end` → 卸 hook、删 observer、freeze 量化）。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是建立「子图数 ≈ decoder 层数 + 1」的直觉。

1. **实践目标**：在不跑 GPU 的情况下，确认 `sequential_targets` 的默认值与子图数量的关系。
2. **操作步骤**：
   - 打开一个模型的 `config.json`，找到 `architectures` 字段，确认其 decoder 层类名（如 `LlamaDecoderLayer`）。
   - 在 Python 里加载一个**极小**的该架构模型（例如 `TinyLlama` 系列），执行：
     ```python
     from llmcompressor.utils.pytorch.module import infer_sequential_targets, get_no_split_params
     from transformers import AutoModelForCausalLM
     m = AutoModelForCausalLM.from_pretrained("<tiny-model-id>")  # 示例代码：请替换为可访问的小模型
     print("no_split:", get_no_split_params(m))
     print("inferred:", infer_sequential_targets(m, None))
     print("num_layers:", m.config.num_hidden_layers)
     ```
3. **需要观察的现象**：`inferred` 的值应等于 `no_split`（即 decoder 层类名）；`num_hidden_layers` 应等于你之后在 GPTQ 日志里看到的「子图数 - 1」。
4. **预期结果**：例如 TinyLlama 有 22 层，则 sequential 管线日志里应出现约 23 个子图的进度条。
5. **待本地验证**：具体子图数需以实际 `trace_subgraphs` 的输出为准（头部子图的存在与否取决于模型结构）。

#### 4.1.5 小练习与答案

**练习 1**：用户没有传 `sequential_targets`，管线会怎样决定切分层？
**答案**：`infer_sequential_targets` 收到 `None`，回退到 `get_no_split_params(model)`，读取 HuggingFace 模型自带的 `_no_split_modules`（一般就是 decoder 层类）。

**练习 2**：为什么 `@handle_sequential_oom` 装饰器建议 OOM 时「换更小的 `sequential_targets`（如 `Linear`）」？
**答案**：子图越大，单子图前向时同时在显存里的激活和权重越多。换成更细粒度的切分层（如 `Linear`）会让每个子图更小、峰值显存更低，代价是子图数变多、整体变慢。

---

### 4.2 子图切分：trace_subgraphs

#### 4.2.1 概念说明

「子图（subgraph）」是 sequential 管线的执行单元。`trace_subgraphs` 要解决一个听起来矛盾的需求：

> 把整模型切成若干段，使得**按顺序执行每一段，等价于执行原始模型**，并且**每个 sequential target 恰好属于一段**。

这里的「sequential target」就是 4.1 推断出的切分层（如 `LlamaDecoderLayer`）。切分靠的是 `torch.fx`：先用 `HFTracer` 追踪模型得到一张完整的计算图（`GraphModule`），再按拓扑序把节点分桶，每个 target 落进独立的桶里，最后把每个桶编译成一个可执行的 `Subgraph`。

`Subgraph` 是个 dataclass，三个字段决定了它的全部行为：

[helpers.py:L33-L47](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/helpers.py#L33-L47) —— `graph`（子图本身）、`input_names`（这个子图需要从缓存读哪些中间值）、`consumed_names`（这些中间值在本子图之后再也没人用了，可以从缓存删掉以省内存）。

#### 4.2.2 核心流程

`trace_subgraphs` 的内部流水线：

```
1. match_named_modules(model, sequential_targets)   # 找到所有 target 模块
2. get_sequential_ancestors(model, targets)         # 只追踪 target 的「祖先」模块
3. SequentialTracer(...).trace(...)                 # torch.fx 追踪 → 完整 GraphModule
4. topological_partition(graph, targets, n)         # 拓扑分桶，每桶最多 n 个 target
5. partition_graph(model, partitions)               # 每桶 → 一个 Subgraph
6. trace_consumed_names(subgraphs)                  # 标记每个输入「最后被谁消费」
```

第 2 步是省时间的关键：`SequentialTracer.is_leaf_module` 规定「**只有 target 的祖先模块才进入追踪，其余一律当叶子节点（不展开）**」。

[helpers.py:L199-L201](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/helpers.py#L199-L201) —— `module not in self.ancestors` 的模块被视为叶子。

这避免了追踪整个模型里无关的分支（例如 `lm_head`，它本就被 `disable_lm_head` 挪到 meta 了）。

第 4 步拓扑分桶用一个经典的「入度（indegree）+ 队列」算法（Kahn 拓扑排序的变体）：

[helpers.py:L252-L336](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/helpers.py#L252-L336) —— `topological_partition`。核心思路：维护每个节点的剩余入度，从入度为 0 的节点出发；每遇到一个 target 节点，就在满足 `targets_per_subgraph` 配额后开一个新桶。`targets_per_subgraph` 由 [dataset_arguments.py:L252-L258](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/dataset_arguments.py#L252-L258) 控制（默认 1），值越大子图越少但越费显存。

第 6 步 `trace_consumed_names` 决定了缓存何时能安全释放：

[helpers.py:L398-L414](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/helpers.py#L398-L414) —— 对每个输入名，**逆序**遍历子图，找到最后一个用到它的子图，把它加入那个子图的 `consumed_names`。这样缓存就能在它最后一次被消费后立刻删除。

#### 4.2.3 源码精读

`Subgraph.forward` 把 `torch.fx` 的 `Graph` 编译成可执行函数：

[helpers.py:L49-L64](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/helpers.py#L49-L64) —— `graph.python_code("self")` 生成 forward 源码，`exec` 进 globals，再取出 `forward` 函数执行；返回值是一个 dict（输出节点名 → 计算值）。这就是 pipeline 里 `subgraph.forward(model, **inputs)` 的真身。

`Subgraph.submodules` 返回子图包含的所有模块（**保持确定性顺序**，这对 DDP 很重要）：

[helpers.py:L66-L81](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/helpers.py#L66-L81) —— 这个列表正是 4.3 里 `sequential_epoch_end(subgraph.submodules(model))` 传给 modifier 的 `modules` 参数。

`trace_subgraphs` 主体与追踪用的各种 patch：

[helpers.py:L84-L168](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/helpers.py#L84-L168) —— 注意追踪期间的一组上下文（L114-L134）：强制 `_attn_implementation="eager"`、关 hook、`autowrap_forwards` 把不可追踪的函数（如各种 mask 函数）包成可追踪形式。L162-L166 的告警说明：当前实现会在开头多产生一个不含 target 的「头部子图」，所以子图数通常是 `len(targets) + 1`。

OOM 友好提示：

[helpers.py:L482-L501](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/helpers.py#L482-L501) —— `handle_sequential_oom` 捕获 `OutOfMemoryError` 并改写错误信息，给出针对稠密模型（用 `Linear`）和 MoE 模型（用 attention + expert 类）的具体建议。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到 `trace_subgraphs` 把一个小模型切成几个 `Subgraph`。
2. **操作步骤**（**示例代码**，需有可访问的小模型与 GPU）：
   ```python
   # 示例代码：仅供阅读理解，实际运行需要真实模型与设备
   import torch
   from transformers import AutoModelForCausalLM, AutoTokenizer
   from llmcompressor.utils.pytorch.module import infer_sequential_targets
   from llmcompressor.pipelines.sequential.helpers import trace_subgraphs

   model = AutoModelForCausalLM.from_pretrained("<tiny-model-id>").to("cuda")
   tok = AutoTokenizer.from_pretrained("<tiny-model-id>")
   inputs = tok("hello", return_tensors="pt").to("cuda")

   targets = infer_sequential_targets(model, None)          # 通常是 ['LlamaDecoderLayer']
   subgraphs = trace_subgraphs(model, dict(inputs), targets, ignore=[], targets_per_subgraph=1)
   print("num_subgraphs:", len(subgraphs))
   for i, sg in enumerate(subgraphs):
       print(i, "input_names:", sg.input_names, "consumed:", sg.consumed_names)
   ```
3. **需要观察的现象**：`num_subgraphs` 应约等于 `num_hidden_layers + 1`；第一个子图的 `input_names` 会包含 `input_ids` 等原始输入名，后续子图的 `input_names` 是上一层产出的中间激活名。
4. **预期结果**：能看到每个子图的输入/消费名，理解「中间激活如何在子图间流转」。
5. **待本地验证**：不同架构（尤其含 MoE / vision）可能追踪失败，需配合 `tracing_ignore` 或 `llmcompressor.transformers.tracing`。

#### 4.2.5 小练习与答案

**练习 1**：`trace_consumed_names` 为什么**逆序**遍历子图？
**答案**：要找的是某个输入「最后一次」被消费的子图，逆序遍历遇到的第一个就是最后消费者，把它标 `consumed` 后即可提前 break，这样缓存能在该中间值不再被需要时立即释放。

**练习 2**：把 `sequential_targets_per_subgraph` 从 1 调到 4，子图数和显存分别怎么变？
**答案**：每 4 个 target 合进一个子图，子图数大致变为原来的 1/4（更快），但单个子图同时驻留的权重和激活更多，峰值显存上升。这是 speed/memory 的权衡旋钮。

---

### 4.3 两遍前向：校准 pass 与传播 pass

#### 4.3.1 概念说明

这是 sequential 管线最核心、也最容易看错的部分。每个子图要跑**两遍**前向，docstring 原文是：

> once to trigger calibration hooks, then a second time in order to capture activations after quantization has occurred through hooks.

两遍的职责完全不同：

| | 第一遍（Calibrating） | 第二遍（Propagating） |
|---|---|---|
| 校准 hook | **开**（收集激活统计） | **关**（`HooksMixin.disable_hooks()`） |
| 是否缓存输出 | 仅当 `propagate_error=False` 时缓存 | 仅当 `propagate_error=True` 时缓存 |
| 关键事件 | 末尾触发 `SEQUENTIAL_EPOCH_END`（量化权重） | 无 |
| 目的 | 收集本子图所有 batch 的统计，并完成权重量化 | 捕获「量化后模块」的输出，喂给下一个子图 |

注意第二遍**只在 `propagate_error=True`（默认）时才跑**。`propagate_error` 的含义见 [dataset_arguments.py:L266-L274](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/dataset_arguments.py#L266-L274)：为 True 时用「量化后的层输出」作为下一层输入（误差跨层累积，更准）；为 False 时用「未量化层输出」。

#### 4.3.2 核心流程

误差传播的**真正发生点**在 `calibrate_output_hook`：

[calibration.py:L173-L184](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L173-L184) —— `module.output_observer(output)` 累积统计，随后 `forward_quantize(...)` 对输出做**伪量化**并返回。这个伪量化后的输出，就是下一层会「看到」的输入——量化误差因此跨层传播。

逐子图循环的伪代码（对应 [pipeline.py:L136-L178](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L136-L178)）：

```
for subgraph_index, subgraph in enumerate(subgraphs):
    with disable_offloading():                       # 两遍期间保持模块在显存，减少搬运
        # ---- pass 1: Calibrating ----
        for batch_idx, inputs in batches:
            current_batch_idx = batch_idx
            outputs = subgraph.forward(model, **inputs)   # hook 开 → observe + forward_quantize
            if not propagate_error and not last_subgraph:
                cache.update(batch_idx, outputs)           # propagate_error=False 在此缓存
                cache.delete(batch_idx, consumed_names)

        LifecycleCallbacks.sequential_epoch_end(subgraph.submodules(model))
            # → on_sequential_epoch_end(modules)
            # → modifier 对这些模块 observe(weight) + update_qparams(weight)
            #   （GPTQ 在此用累积的 Hessian 做逐块量化；RTN 在此就近取整）

        # ---- pass 2: Propagating (仅 propagate_error=True) ----
        if propagate_error:
            with HooksMixin.disable_hooks():              # 关 hook，不再重新观测
                for batch_idx, inputs in batches:
                    output = subgraph.forward(model, **inputs)  # 捕获量化后模块的输出
                    if not last_subgraph:
                        cache.update(batch_idx, output)
                        cache.delete(batch_idx, consumed_names)
```

#### 4.3.3 源码精读

第一遍（校准）+ epoch 末：

[pipeline.py:L143-L160](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L143-L160) —— `with disable_offloading()` 保证两遍期间模块不被搬走（减少内存搬运）；遍历所有 batch 触发 hook；遍历完后 `sequential_epoch_end(subgraph.submodules(model))`。注意 `subgraph.submodules(model)` 把子图里的模块列表作为 `modules` 参数传给事件（u2-l3 讲过 `on_sequential_epoch_end(modules)` 的 `modules` 就是这么来的）。

第二遍（传播）：

[pipeline.py:L162-L178](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L162-L178) —— 源码注释明确写出两件事：「this pass does not trigger modifier hooks」和「only used for capturing outputs of compressed modules」。它把量化后模块的输出写进缓存，成为下一个子图的输入。注意**只有 `subgraph_index < num_subgraphs - 1`（非最后一个子图）才需要缓存**——最后一个子图的输出（通常是 logits）没有下游消费者。

AutoRound 特例再回顾：

[pipeline.py:L89-L94](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L89-L94) —— AutoRound 对每层用**自己的前向**做轻量优化，层与层之间不应传播量化误差，因此强制 `propagate_error=False`。此时只有第一遍（且在第一遍缓存输出），没有第二遍。

#### 4.3.4 代码实践

1. **实践目标**：从日志里数出每个子图跑了几遍前向，并理解 `propagate_error` 的影响。
2. **操作步骤**：参考 `examples/quantization_w8a8_int8/`（一个真实的 GPTQ + SmoothQuant 示例），把模型换成本地能跑的小模型，跑一次 oneshot：
   ```python
   # 示例代码：改编自 examples/quantization_w8a8_int8/README.md
   from llmcompressor import oneshot
   from llmcompressor.modifiers.gptq import GPTQModifier
   oneshot(
       model="<tiny-model-id>",
       dataset="<小数据集>",
       recipe=[GPTQModifier(targets="Linear", scheme="W8A8", ignore=["lm_head"])],
       num_calibration_samples=8,        # 调小以加快
       max_seq_length=512,
       output_dir="/tmp/out-w8a8",
   )
   ```
3. **需要观察的现象**：终端会出现两组 tqdm 进度条交替——`(i/N): Calibrating`（第一遍）和 `(i/N): Propagating`（第二遍），每个子图各一组。`N` 即子图数。
4. **预期结果**：每个子图恰好出现一次 `Calibrating` 和一次 `Propagating`，说明默认 `propagate_error=True` 下每个子图两遍前向。若用 `AutoRoundModifier` 替换，则只看到 `Calibrating`（无 `Propagating`）。
5. **待本地验证**：进度条文案与数量以实际版本日志为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么最后一个子图不需要缓存输出？
**答案**：最后一个子图的输出（logits）在校准阶段没有下游子图会消费它（且 `lm_head` 已被 `disable_lm_head` 挪到 meta 设备）。缓存它既浪费内存又无用处，所以 `if subgraph_index < num_subgraphs - 1` 守卫跳过。

**练习 2**：`calibrate_output_hook` 里 `forward_quantize` 的作用是什么？如果去掉它会怎样？
**答案**：它对输出做伪量化，使下一层收到的是「量化后」的激活，从而让量化误差跨层累积、更贴近部署真实情况。去掉后，层间传播的就是全精度激活，校准统计会偏乐观、最终精度可能下降。

**练习 3**：`propagate_error=False` 时，下一层的输入反映的是「量化后」还是「未量化」的上一层输出？
**答案**：未量化的层输出。因为此时只在第一遍缓存，而第一遍的缓存值虽经 `calibrate_output_hook` 伪量化，但权重尚未在该子图 epoch 末被量化；且 AutoRound 这类算法刻意不让权重量化误差跨层传播。两遍策略正是为了在 `propagate_error=True` 时显式补上「权重量化后的输出」。

---

### 4.4 IntermediatesCache：中间激活的缓存与显存优化

#### 4.4.1 概念说明

如果没有缓存机制，逐层校准会要求把「所有 batch 在所有子图边界上的中间激活」同时留在显存里——这对大模型是不可承受的。`IntermediatesCache` 的设计哲学是：

> 每个中间激活在**写入**缓存时立刻 offload 到 `offload_device`（默认 CPU），在**读取**时再 onload 回 `onload_device`（GPU）。这样 GPU 显存里同一时刻只驻留「当前正在用的那一小批激活」。

[cache.py:L33-L58](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/cache.py#L33-L58) —— 类 docstring 明确：「Values are offloaded to the `offload_device` when stored ... and onloaded to their original device when fetched. If `offload_device` is None, values will not be offloaded at all.」

#### 4.4.2 核心流程

缓存的数据模型是两层嵌套：

```
IntermediatesCache
└── batch_intermediates: list[dict]      # 第 i 个元素 = 第 i 个 batch 的中间值
    └── dict[name -> IntermediateValue]  # name 如 "input_ids"、"layer.0.output"
        └── IntermediateValue(value, device)   # value 可能是 Tensor/嵌套结构
```

核心操作对应 pipeline 里的调用：

- `from_dataloader(dataloader, onload_device, offload_device)`：构造时**一次性**把整个 dataloader 的所有 batch offload 进缓存（[cache.py:L71-L102](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/cache.py#L71-L102)）。这就是 sequential 启动时那个 `Preparing cache` 进度条。
- `fetch(batch_idx, input_names)`：取出一个 batch 的若干输入，**onload** 回 GPU（[cache.py:L104-L122](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/cache.py#L104-L122)）。
- `update(batch_idx, outputs)`：把子图产出的新中间值 offload 后写回缓存（[cache.py:L166-L175](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/cache.py#L166-L175)）。
- `delete(batch_idx, consumed_names)`：删掉再没人用的中间值（[cache.py:L177-L191](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/cache.py#L177-L191)），与 4.2 的 `consumed_names` 配合。

把 pipeline 的 batch 循环和缓存串起来看：

```
activations = IntermediatesCache.from_dataloader(dataloader, onload, offload)  # 全量 offload

for subgraph in subgraphs:
    for batch_idx in range(num_batches):
        inputs = fetch(batch_idx, subgraph.input_names)   # onload 需要的输入
        outputs = subgraph.forward(model, **inputs)        # 前向
        update(batch_idx, outputs)                         # offload 新输出
        delete(batch_idx, subgraph.consumed_names)         # 释放已消费的
```

显存峰值因此被压缩到「一个子图 + 一个 batch 的激活」级别，与模型层数、校准样本数**解耦**。

#### 4.4.3 源码精读

onload / offload 的递归处理（支持 Tensor、list、tuple、dict、dataclass 嵌套）：

[cache.py:L303-L339](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/cache.py#L303-L339) —— `_onload_value` 用 `match` 递归把 Tensor 搬到 `onload_device`，并在「源是 pinned + 目标是加速器」时用 `non_blocking=True` 传输。

[cache.py:L341-L404](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/cache.py#L341-L404) —— `_offload_value` 把 Tensor 搬到 `offload_device`，并用一个 `WeakKeyDictionary` 做共享张量的去重缓存，避免多个引用同一张量的中间值重复占内存。注意 L360 的 `OverrideEqMode`：因为要用 Tensor 当字典键，必须临时把 `==` 重写成基于 `id` 的布尔比较（否则 `a == b` 返回的是张量而非标量，无法做字典查找），见 [cache.py:L407-L435](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/cache.py#L407-L435)。

预取优化（`sequential_prefetch`）：

[cache.py:L244-L295](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/cache.py#L244-L295) —— `iter_prefetch` 用一个单线程 `ThreadPoolExecutor` 在后台预取**下一个** batch 的 onload，与本 batch 的前向重叠；有 CUDA 时用独立 stream + Event 做非阻塞 H2D 拷贝。对应参数 [dataset_arguments.py:L292-L299](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/dataset_arguments.py#L292-L299)（默认 False，开启会让「两个 batch 同时在显存」，用显存换时间）。pipeline 里通过 `_get_batches` 选择 `iter` 还是 `iter_prefetch`：

[pipeline.py:L27-L48](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L27-L48) —— 根据 `sequential_prefetch` 二选一。

`size()` 可用于诊断缓存实际占了多少内存：

[cache.py:L204-L238](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/cache.py#L204-L238) —— 按设备统计字节数，递归处理嵌套并用 `memo` 去重共享张量。

#### 4.4.4 代码实践

1. **实践目标**：体会 `sequential_offload_device` 对显存的影响。
2. **操作步骤**：对同一个模型分别用两种配置跑 GPTQ（接 4.3 的脚本），用 `torch.cuda.max_memory_allocated()` 记录峰值显存：
   ```python
   # 示例代码：仅作思路演示
   import torch
   torch.cuda.reset_peak_memory_stats()
   oneshot(model=..., dataset=..., recipe=...,
           sequential_offload_device="cpu", ...)   # 默认：offload 到 CPU
   print("cpu offload peak (MB):", torch.cuda.max_memory_allocated() / 1e6)
   ```
   若有两张卡，可改 `sequential_offload_device="cuda:1"` 再跑一次对比（这是 [dataset_arguments.py:L244-L251](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/dataset_arguments.py#L244-L251) 推荐的多卡用法）。
3. **需要观察的现象**：`offload_device="cpu"` 时 GPU 峰值显存较低、但耗时长（H2D 拷贝多）；改成 `cuda:1` 时峰值 GPU 显存可能略升（取决于卡 1 容量）但整体更快。
4. **预期结果**：单卡场景下 CPU offload 是默认且最省显存的选择；多卡场景下 `cuda:1` 用第二张卡承担中间激活、加速校准。
5. **待本地验证**：具体显存数字与模型/数据规模强相关，请以本地实测为准。

#### 4.4.5 小练习与答案

**练习 1**：`from_dataloader` 为什么在管线启动时**一次性**遍历整个 dataloader，而不是按需加载？
**答案**：逐层校准要求每个 batch 的原始输入在后续每个子图都被反复 fetch；预先全部 offload 到 CPU，可以让「数据准备」与「GPU 校准」解耦，且 offload 到 CPU 比留在 GPU 省显存。代价是启动时的 `Preparing cache` 阶段需要时间（可用更大 batch_size 或 `dataloader_num_workers>0` 加速，见其 docstring）。

**练习 2**：`OverrideEqMode` 解决了什么问题？
**答案**：要把 `torch.Tensor` 当作字典键，但 `tensor == tensor` 返回的是逐元素 bool 张量，字典查找会报错或行为异常。`OverrideEqMode` 临时把相等判断替换成「比较两个对象的 `id`」，返回标量布尔值，从而让 `WeakKeyDictionary` 正常工作。

---

## 5. 综合实践

把本讲四条主线串起来：**子图切分 → 两遍前向 → 事件触发 → 缓存省显存**。

任务：用 `examples/quantization_w8a8_int8/` 的 GPTQ + SmoothQuant 配方，在一个小模型上完成一次 sequential 校准，并回答以下问题（**待本地验证**）：

1. **子图数**：从进度条的 `(i/N)` 读出 `N`，验证它是否约等于 `num_hidden_layers + 1`。
2. **两遍前向**：确认每个子图是否各有一组 `Calibrating` 和 `Propagating` 进度条（默认 `propagate_error=True`）。
3. **缓存阶段**：是否在正式校准前看到 `Preparing cache` 进度条？它处理了多少个 batch（应等于 `num_calibration_samples / batch_size`）？
4. **显存旋钮**：分别设 `sequential_offload_device="cpu"` 与 `sequential_prefetch=True`，记录峰值显存与耗时，体会「显存 ↔ 速度」的权衡。
5. **（进阶）源码追踪**：在 [pipeline.py:L160](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L160) 的 `sequential_epoch_end` 处加一行 `print(f"epoch end for {len(modules)} modules")`，确认每个子图 epoch 末传给 modifier 的模块数，理解 GPTQ「逐子图累积 Hessian → epoch 末量化权重」的节奏（注意：修改源码仅用于本地学习，勿提交）。

> 如果没有 GPU，可退化为「源码阅读型实践」：对照 4.3.2 的伪代码，在 [pipeline.py:L136-L181](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L136-L181) 逐行标注「这一行属于第几遍、是否缓存、触发什么事件」。

## 6. 本讲小结

- `SequentialPipeline` 按「切子图 → 逐子图两遍前向 → 缓存中间激活」三步工作，是所有需校准数据算法的默认管线。
- 切分层由 `infer_sequential_targets` 推断（默认取 HF 模型的 `_no_split_modules`），`trace_subgraphs` 用 `torch.fx` 追踪 + 拓扑分桶得到子图；经验上**子图数 ≈ decoder 层数 + 1**。
- 每个子图两遍前向：第一遍 `Calibrating` 开 hook 收集统计、末尾 `sequential_epoch_end` 量化权重；第二遍 `Propagating`（仅 `propagate_error=True`）关 hook 捕获量化后输出喂给下一层——误差由此跨层传播。
- `calibrate_output_hook` 里的 `forward_quantize` 是误差传播的真正发生点；`AutoRoundModifier` 因逐层独立优化被强制设为 `propagate_error=False`。
- `IntermediatesCache` 通过「写即 offload、读即 onload」把峰值显存与层数/样本数解耦；`consumed_names` 让中间值在最后一次消费后立即释放；`sequential_offload_device` 与 `sequential_prefetch` 是显存↔速度的两个旋钮。

## 7. 下一步学习建议

- **横向对比管线**：继续读 [u3-l6](u3-l6-independent-basic-datafree-pipelines.md)，对比 `IndependentPipeline`（把每个 modifier 委派给子管线）、`BasicPipeline`（整模型一遍、不传播误差）、`DataFreePipeline`（无数据、只触发回调）与 sequential 的差异。
- **大模型内存策略**：本讲的 `IntermediatesCache` offload 是省显存的一面，另一面是「权重逐层 onloading + 磁盘 offloading」，见 [u6-l2 大模型内存策略](u6-l2-big-model-memory.md)。
- **DDP 分布式**：注意 `Subgraph.submodules` 强调的「确定性顺序」是为 DDP 准备的；多卡下 observer 统计的 all-reduce 见 [u6-l1 DDP 分布式量化](u6-l1-ddp-distributed.md)。
- **MoE 场景**：MoE 模型常因专家不可追踪而需要先线性化，`handle_sequential_oom` 给出的 attention+expert 切分建议是入门，深入见 [u5-l2 MoE 建模与线性化](u5-l2-moe-modeling.md)。
