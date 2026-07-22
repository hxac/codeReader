# ModelRunner 与前向执行路径

## 1. 本讲目标

在前几讲里，我们把一条请求从 HTTP 接入一直追踪到了调度器（Scheduler），又弄清了 `ScheduleBatch` 这个「批容器」和 `out_cache_loc` 这套两级 KV 内存池。这些讲义里都把「真正算一次前向」当成了一个黑盒：调度器只是把一批请求交给某个 worker，然后等它吐回 logits。

本讲就负责打开这个黑盒。读完本讲，你应当能够：

1. 说清 `ModelRunner` 在「调度器 / Worker」与「PyTorch 模型」之间的**桥梁角色**，理解它持有模型、注意力后端、KV 内存池、CUDA Graph 这些资源的含义。
2. 跟踪一条 `ScheduleBatch` 如何被 `TpModelWorker.forward_batch_generation` 转成 `ForwardBatch`、交给 `ModelRunner.forward`、最终调用 `model.forward(...)`，并标注每一步的输入输出。
3. 看懂 `_forward_raw` 里**三条前向路径**（decode CUDA Graph 回放 / prefill CUDA Graph / eager）的分发条件，以及 `can_run_graph` 这个判断为何是性能关键。
4. 理解运行期配置已迁移到 `get_exec()` / `get_schedule()` / `get_model()` 等**命名空间袋**，运行期改写走 `get_context().override()` 而非直接赋值。

> 本讲在手册里承接 [u3-l2（Req 与 ScheduleBatch）](u3-l2-req-and-schedule-batch.md)、[u4-l2（KV 内存池）](u4-l2-memory-pool.md) 与 [u2-l5（RuntimeContext 与配置命名空间）](u2-l5-runtime-context-config-bags.md)。建议先读完这三讲再进入本讲。

## 2. 前置知识

本讲会用到下面这些概念，先用一句话回顾：

- **`ScheduleBatch`（调度批）**：调度器视角的「这一轮要算的一批请求」，内含请求列表 `reqs`、内存池引用、以及 `out_cache_loc` 等准备好的批次张量。它是 CPU 侧的、面向调度逻辑的数据结构（见 u3-l2）。
- **`ForwardBatch`（前向批）**：模型视角的「一次前向需要的全部张量」，把 `input_ids`、`positions`、`seq_lens`、`out_cache_loc`、`forward_mode` 等打包在一起，喂给模型与注意力后端（见 u5-l2，本讲只用到它的入口）。
- **注意力后端（attention backend）**：可插拔的注意力实现（FlashInfer、FlashAttention、Triton 等），负责真正读写 KV 内存池（见 u5-l3）。本讲把它当成「`ModelRunner` 持有的一个组件」即可。
- **CUDA Graph**：把一次 GPU 计算的整张图提前录制下来，之后每次只要填好输入 buffer 直接 replay，省掉 CPU 端反复 launch kernel 的开销。decode 阶段每次算的 token 数固定、形状可枚举，特别适合用 CUDA Graph。
- **`RuntimeContext` 与配置命名空间袋**：每个进程持有一份只读 `ServerArgs` 快照，它被 `publish(server_args, role)` 拆成 `device`/`model`/`exec`/`schedule`/... 等「命名空间袋」，代码用 `get_exec()` 等访问器读取，运行期改写只能走 `get_context().override()`（见 u2-l5）。
- **张量并行（TP）**：把一个模型的权重切到多张 GPU 上，每张卡只算一部分，靠 all-reduce 等集合通信合并结果。本讲里的 `TpModelWorker` 就是「负责一张卡」的那个 worker。

一个贯穿本讲的直觉是：**调度器关心「算什么、什么时候算」，`ModelRunner` 关心「怎么把张量喂进模型并把 logits 拿出来」**。两者之间的「翻译层」就是 `TpModelWorker.forward_batch_generation`。

## 3. 本讲源码地图

本讲主要涉及两个核心文件，外加一个把前向路径拆细的子目录：

| 文件 | 作用 |
| --- | --- |
| [model_runner.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py) | 定义 `ModelRunner`——持有模型、注意力后端、内存池与 CUDA Graph，提供 `forward` / `_forward_raw`，是模型执行的真正入口。 |
| [tp_worker.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tp_worker.py) | 定义 `TpModelWorker`——每张 GPU 一个的张量并行 worker，负责构造 `ModelRunner`、把 `ScheduleBatch` 翻译成 `ForwardBatch` 并编排采样。 |
| [runner/eager_runner.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/runner/eager_runner.py) | `EagerRunner`——「不走 CUDA Graph」的前向实现，按 `forward_mode` 分派到 `_execute_decode` / `_execute_extend` / `_execute_idle`，最终调用 `model.forward(...)`。 |
| [runtime_context.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py) | 提供 `get_exec()` / `get_schedule()` / `get_model()` 等命名空间访问器与 `publish` / `get_context().override()`。 |

`model_runner.py` 已是一个 1800+ 行的大类（属本仓库「冻结风格大类」之一，修改它需遵守 `large-class-style` 约束）。本讲不会逐行讲它的 `__init__`，只聚焦与「前向执行」直接相关的几条主线。

## 4. 核心概念与源码讲解

### 4.1 ModelRunner 的角色：调度器与模型之间的桥梁

#### 4.1.1 概念说明

一个推理进程里通常有「调度器」和「模型」两套世界：

- 调度器世界用 `ScheduleBatch`、`Req`、`waiting_queue` 这些概念，关心的是「先服务谁、一批装多少、KV 还够不够」。
- 模型世界是纯 PyTorch：给定 `input_ids`、`positions`、注意力元数据，跑一次 `forward`，拿到 logits。

这两套世界不能直接对接：模型不认识 `ScheduleBatch`，调度器也不该关心 PyTorch 细节。`ModelRunner` 就是中间那座桥。它在一个对象里同时持有：

- **模型本体** `self.model`（如 `LlamaForCausalLM`）；
- **注意力后端** `self.attn_backend` / `self.decode_attn_backend`；
- **KV 内存池** `self.req_to_token_pool`、`self.token_to_kv_pool_allocator`（见 u4-l2）；
- **三种前向执行器**：`self.eager_runner`、`self.prefill_cuda_graph_runner`、`self.decode_cuda_graph_runner`；
- **采样器** `self.sampler`。

而 `TpModelWorker`（张量并行 worker）则是「包着 `ModelRunner` 的那一层」：它负责构造 `ModelRunner`、读配置、做 tokenizer 初始化、管理多步预测（MTP）时的一组 runner，并提供统一入口 `forward_batch_generation`。

#### 4.1.2 核心流程

一次前向从调度器到模型，粗略经过四步：

```
Scheduler.run_batch(batch)
   │  （batch 是 ScheduleBatch）
   ▼
TpModelWorker.forward_batch_generation(batch)
   │  ① ForwardBatch.init_new(batch, model_runner, ...)
   │     把 ScheduleBatch 翻译成模型能用的 ForwardBatch
   ▼
ModelRunner.forward(forward_batch)
   │  ② _forward_raw(...) 选三条路径之一
   ▼
eager_runner.execute / decode_cuda_graph_runner.execute / ...
   │  ③ 最终调用 self.model.forward(input_ids, positions, forward_batch)
   ▼
LogitsProcessorOutput（含 next_token_logits）
   │  ④ sampler 采样得到 next_token_ids
   ▼
GenerationBatchResult（回给调度器）
```

本模块先聚焦「谁构造谁」与「入口在哪」，4.2 / 4.3 再分别展开「翻译 + 采样编排」和「三路径分发」。

#### 4.1.3 源码精读

`ModelRunner` 的构造参数清楚地显示了它需要哪些「桥的构件」：模型配置、静态显存比例、GPU id、并行状态 `ps`、`server_args`，以及（draft worker 场景）复用的内存池：

> [model_runner.py:231-244](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L231-L244) —— `ModelRunner.__init__` 的签名。注意 `mem_fraction_static`、`ps`、`server_args` 都是构造时一次性传入的「冻结输入」。

它的前向结果用一个很小的 dataclass 包装，目的是把「logits（或 PP 中间态）」和「这次能不能走 CUDA Graph」一起带回去：

> [model_runner.py:219-225](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L219-L225) —— `ModelRunnerOutput`。`can_run_graph` 是关键：它告诉调度器「这一批是否用了 CUDA Graph 路径」，后续结果处理（要不要把输出拷回 CPU）会据此分支。

`TpModelWorker` 在自己的 `__init__` 里调用 `_init_model_config()` 和 `_init_model_runner()` 来把这座桥搭起来。注意这两处读配置的方式已经从「直接读 `server_args`」迁移到了命名空间袋：

> [tp_worker.py:403-420](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tp_worker.py#L403-L420) —— `_init_model_config`。主模型读 `get_model().model_path` / `get_model().revision`；草稿模型读 `get_spec().speculative_draft_model_path` / `get_spec().speculative_draft_model_revision`。同一份只读 `ServerArgs`，按「是不是草稿」从不同命名空间袋取值。

> [tp_worker.py:422-437](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tp_worker.py#L422-L437) —— `_init_model_runner`。`mem_fraction_static=get_schedule().mem_fraction_static`，注意它取自 `schedule` 命名空间而非裸 `server_args`。

`ModelRunner` 在 `init_cuda_graphs` 里一次性建好三种执行器（`init_attention_backends` 必须在它之前调用，以保证 CUDA Graph 捕获时辅助隐状态采集已就绪）：

> [model_runner.py:829-846](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L829-L846) —— `init_attention_backends`：先配置辅助隐状态采集，再 `build_attention_backends(model_runner=self)`，把得到的 prefill/decode 后端挂到 `self`。

> [model_runner.py:848-855](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L848-L855) —— `init_cuda_graphs`：调用 `capture_cuda_graphs(...)`，把返回的 `eager_runner` / `prefill_runner` / `decode.runner` 三个执行器分别赋给 `self.eager_runner` / `self.prefill_cuda_graph_runner` / `self.decode_cuda_graph_runner`。

抽象基类 `BaseTpWorker` 用一个 `@abstractmethod` 把「前向生成」定成所有 worker 的统一契约：

> [tp_worker.py:69-77](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tp_worker.py#L69-L77) —— `BaseTpWorker.forward_batch_generation` 与 `model_runner` 属性都是抽象方法。调度器只认这个接口，不关心背后是普通 TP worker 还是投机 worker。

#### 4.1.4 代码实践

**实践目标**：建立「调度器 → worker → ModelRunner → 模型」的对象持有关系直觉。

**操作步骤（源码阅读型）**：

1. 在 `tp_worker.py` 中打开 `TpModelWorker.__init__`（[tp_worker.py:277-360](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tp_worker.py#L277-L360)），找出它如何分别调用 `_init_model_config()` 和 `_init_model_runner()`，以及把 runner 存进哪个属性。
2. 在 `model_runner.py` 中搜 `self.eager_runner`、`self.prefill_cuda_graph_runner`、`self.decode_cuda_graph_runner` 三个属性，确认它们都在 `init_cuda_graphs` 里被赋值。
3. 在 `scheduler.py` 中搜索 `forward_batch_generation`，确认调度器是通过 `self.model_worker.forward_batch_generation(batch)` 调进来的。

**需要观察的现象**：你会看到调度器调的是 `model_worker`（一个 `BaseTpWorker`），而不是直接调 `ModelRunner`；`ModelRunner` 被包在 worker 内部，外部世界不直接接触它。

**预期结果**：画出一张「对象持有图」——`Scheduler` 持有 `TpModelWorker`，`TpModelWorker` 持有 `ModelRunner`（可能是一组 `model_runner_list`），`ModelRunner` 持有 `model`、`attn_backend`、三种 runner、内存池。

> 待本地验证：实际运行时可在 `TpModelWorker.__init__` 末尾加一行 `logger.info(f"runner list len={len(self.model_runner_list)}")`，普通模型应为 1，开启多步预测（multi-layer EAGLE）时会大于 1。

#### 4.1.5 小练习与答案

**练习 1**：`ModelRunner` 为什么不直接接收一个 `ScheduleBatch` 来跑前向，而要先有人把它转成 `ForwardBatch`？

**参考答案**：`ScheduleBatch` 是调度逻辑的容器（请求列表、预算账本），含很多模型用不到的字段；`ForwardBatch` 是把「一次前向真正需要的张量」拍平、整理后的载体。把翻译步骤放在 `TpModelWorker` 里，让 `ModelRunner` 只关心「张量进、logits 出」，职责更单一，也便于 draft worker 复用同一套 runner。

**练习 2**：为什么 `init_attention_backends` 必须在 `init_cuda_graphs` 之前调用？

**参考答案**：CUDA Graph 捕获会把整个前向录制成一张固定的图重放。注意力后端和「辅助隐状态采集配置」必须在录制之前就位，否则录到的图会用错误的注意力元数据或漏掉辅助隐状态的采集钩子。`init_attention_backends` 开头的注释明确写了这一点。

---

### 4.2 TpModelWorker.forward_batch_generation：批的组装与采样编排

#### 4.2.1 概念说明

调度器调进来的入口就是 `forward_batch_generation`。它做两件事：

1. **翻译批**：把 `ScheduleBatch` 转成 `ForwardBatch`（必要时）。
2. **编排采样**：拿到 `ModelRunner.forward` 返回的 logits 后，决定要不要立刻采样、要不要把采样推迟到下一轮（overlap 模式下的优化）。

它的返回值是 `GenerationBatchResult`，里面既有 logits，也有采样得到的 `next_token_ids`，以及一些可选的 MoE / indexer 指标。它是「批这一层」的统一回包格式。

#### 4.2.2 核心流程

```
forward_batch_generation(batch=None | ScheduleBatch, forward_batch=None, is_verify=False, ...):
  if batch is not None:
      ① ForwardBatch.init_new(batch, model_runner, ...)        # 翻译批
  ② out = model_runner.forward(forward_batch)                  # 跑前向，得到 logits_output + can_run_graph
  if 是 PP 最后一卡:
      if is_verify:                                            # 投机解码的验证步，跳过采样
          return GenerationBatchResult(...)
      if overlap 且 启用文法约束:
          把采样推迟到下一轮（delay_sample_func）              # 让采样与 GPU 重叠
      else if 不是 prefill-only:
          next_token_ids = model_runner.sample(logits, batch)  # 立刻采样
      return GenerationBatchResult(logits_output, next_token_ids, ...)
  else:                                                        # PP 中间卡
      返回 pp_hidden_states_proxy_tensors（给下一阶段的隐状态）
```

注意两条「分叉」：

- **流水线并行（PP）**：非最后一卡只产出隐状态代理张量（`pp_hidden_states_proxy_tensors`），不采 logits、不采样。
- **投机解码的 verify 步**：只验证草稿 token，不在这层采样（spec_v2 worker 会在验证后自己发 publish）。

#### 4.2.3 源码精读

入口签名透露了它的多种调用形态（既能传 `batch` 让它现造 `ForwardBatch`，也能直接传造好的 `forward_batch`）：

> [tp_worker.py:530-556](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tp_worker.py#L530-L556) —— `forward_batch_generation`。当 `batch is not None` 时调用 `ForwardBatch.init_new(batch, self.model_runner, ...)` 翻译批；这是 `ScheduleBatch → ForwardBatch` 的唯一标准入口。

调用 `ModelRunner.forward` 并组装结果：

> [tp_worker.py:564-576](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tp_worker.py#L564-L576) —— `out = self.model_runner.forward(forward_batch, pp_proxy_tensors=...)`，再把 `out.logits_output` 与 `out.can_run_graph` 装进 `GenerationBatchResult`。注意它读的是 `ModelRunnerOutput` 的两个字段。

overlap + 文法约束时，采样被包成一个闭包 `delay_sample_func` 推迟到下一轮（这是为了把采样开销藏到 GPU 计算背后，呼应 u3-l4 讲的零开销调度）：

> [tp_worker.py:582-595](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tp_worker.py#L582-L595) —— 当 `enable_overlap and not enable_spec` 且批次带文法时，定义 `sample_batch_func` 并赋给 `batch_result.delay_sample_func`，直接返回（采样延后）。

普通路径直接采样；prefill-only 批则造零张量占位（prefill 那一步本就不产出新 token）：

> [tp_worker.py:597-619](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tp_worker.py#L597-L619) —— 非 prefill-only 调 `model_runner.sample(...)` 拿 `next_token_ids`；prefill-only 造一个长度等于序列数的零张量。

PP 中间卡的分支：

> [tp_worker.py:620-630](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tp_worker.py#L620-L630) —— 非 PP 最后一卡时，`out.logits_output` 其实是隐状态代理张量，回包只带 `pp_hidden_states_proxy_tensors`，不采样。

调度器侧的调用点（给你一个「谁在调它」的锚点）：

> [scheduler.py:3381-3383](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/scheduler.py#L3381-L3383) —— `run_batch` 里 `batch_result = self.model_worker.forward_batch_generation(batch, **fwd_kwargs)`。投机解码还会通过 `fwd_kwargs["on_publish"]` 注入一个回调（见 u10）。

#### 4.2.4 代码实践

**实践目标**：理解 `forward_batch_generation` 在三种场景下（普通 decode、prefill-only、overlap+文法）行为不同。

**操作步骤（源码阅读型）**：

1. 打开 [tp_worker.py:530-630](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tp_worker.py#L530-L630)，对照上面的流程图给每一行加注释，标出「翻译批 / 跑前向 / 采样 / 回包」四段。
2. 分别搜索 `is_verify`、`delay_sample_func`、`is_prefill_only` 三个关键词，回答：它们各自让函数在哪一步提前 `return`，返回了什么？

**需要观察的现象**：你会看到「采样」并不是无条件立刻执行的——它可能被推迟、可能被跳过、也可能只是造占位张量。

**预期结果**：写出三行结论——
- `is_verify=True`：不采样，直接返回 logits；
- overlap + 文法：采样延后一轮（`delay_sample_func`）；
- prefill-only：造零占位 `next_token_ids`。

> 待本地验证：若要实测，可参考 [examples/runtime/engine/offline_batch_inference.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/examples/runtime/engine/offline_batch_inference.py) 用 `Engine` 跑一个长 prompt，在 `sample` 入口加日志观察 prefill 阶段确实跳过了真实采样。

#### 4.2.5 小练习与答案

**练习 1**：为什么 overlap + 文法约束时要把采样推迟到下一轮？

**参考答案**：文法约束需要在采样前构造词表掩码（`update_regex_vocab_mask`），这一步在 CPU 上有一定开销。overlap 模式下，本轮 GPU 正在跑前向，把「上一批的采样 + 本批的掩码构造」放到这个空档里执行，就能让采样开销被 GPU 计算掩盖，从而不拖慢吞吐（呼应 u3-l4 的零开销调度思想）。

**练习 2**：PP（流水线并行）的中间卡为什么不在这一层采样？

**参考答案**：PP 把模型的不同层切到不同卡上，中间卡的「输出」还不是最终 logits，而是给下一阶段的隐状态（`pp_hidden_states_proxy_tensors`）。只有 PP 的最后一卡才拿到能算 logits 的隐状态，所以采样只在最后一卡发生。

---

### 4.3 ModelRunner.forward 与 _forward_raw：三种前向路径的分发

#### 4.3.1 概念说明

`ModelRunner.forward` 是对外入口，`_forward_raw` 才是真正选路径的地方。一次前向有「快路」和「慢路」之分：

- **快路（CUDA Graph 回放）**：decode 阶段每步 token 数固定、形状可枚举，可以提前把整张计算图录下来，之后每次只填输入 buffer 直接 replay，省掉 CPU 端逐个 launch kernel 的开销。这是 decode 低延迟的关键。
- **慢路（eager，即时计算）**：prefill 阶段 token 数变化大、形状不固定，难以录图，于是走「正常的 PyTorch 即时前向」。
- **中间路（prefill CUDA Graph / piecewise）**：把 prefill 切成分段图捕获（piecewise cuda graph），在形状合适时也走图。

`_forward_raw` 的核心就是用一个布尔量 `can_run_graph` 决定走快路还是慢路，并在慢路里再细分成 split-prefill / prefill-cuda-graph / eager 三种。

#### 4.3.2 核心流程

`forward` 的流程（先做一层「前后处理」包装，再委托 `_forward_raw`）：

```
forward(forward_batch):
  forward_pass_id += 1
  with (canary_ctx, profile_step_span, expert_distribution_recorder):
      output = _forward_raw(forward_batch, ...)        # 真正的前向
      if enable_elastic_ep:
          output = _maybe_rebalance_after_rank_fault(...)  # 弹性 EP 故障后重平衡
  output.expert_distribution_metrics = recorder_outputs["metrics"]
  # 各种 capturer / manager 的收尾回调
  routed_experts_output / indexer_topk_output / eplb / dumper / msprobe
  if get_exec().moe.elastic_ep_backend is not None:
      maybe_join_ep_ranks()
  return output
```

`_forward_raw` 的分发（重点）：

```
_forward_raw(forward_batch, ...):
  建立 ForwardContext（把 attn_backend 挂进线程局部上下文）
  can_run_graph = (forward_mode 是 cuda_graph) 且 decode_cuda_graph_runner 存在 且 它.can_run_graph(batch)
  if can_run_graph:
      ① decode_cuda_graph_runner.execute(batch)          # 快路：回放录好的 decode 图
      return ModelRunnerOutput(ret, can_run_graph=True)
  _prepare_eager_forward_batch(batch)                    # DP/MLP 同步 padding、attn-tp 归一
  _maybe_execute_deferred_mamba_cow_and_clear(batch)     # mamba 的延迟 COW/clear
  if forward_mode.is_split_prefill():
      ② forward_split_prefill(batch)                     # 层切分 prefill，留在 ModelRunner
  elif 是 extend 且 prefill_cuda_graph_runner 能跑 且 无 CP:
      ③ prefill_cuda_graph_runner.execute(batch)         # 中间路：prefill 分段图
      can_run_graph = True
  else:
      ④ eager_runner.execute(batch)                      # 慢路：decode/extend/idle 即时前向
  return ModelRunnerOutput(ret, can_run_graph)
```

四条出口里，④ `eager_runner.execute` 才是真正调用 `self.model.forward(...)` 的地方（见下方源码精读）。

#### 4.3.3 源码精读

`forward` 的入口与前后处理：

> [model_runner.py:1262-1306](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L1262-L1306) —— `forward`。注意它先建 `step_span_ctx`（性能分析）、canary 上下文（KV 校验）、专家分布记录器，再 `output = self._forward_raw(...)`。

前向结束后的收尾回调，以及一个典型的命名空间袋配置读取：

> [model_runner.py:1321-1356](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L1321-L1356) —— 收尾阶段读 `no_copy_to_cpu = not get_schedule().disable_overlap_schedule`（`schedule` 袋）、`get_exec().moe.elastic_ep_backend`（`exec.moe` 子袋）。这就是本讲强调的「配置访问已迁移到命名空间袋」的具体落点。

`_forward_raw` 的开头：建立 `ForwardContext` 并计算 `can_run_graph`：

> [model_runner.py:1406-1427](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L1406-L1427) —— 若当前线程已有 forward context 就复用，否则用 `forward_context(ForwardContext(attn_backend=...))` 新建；`can_run_graph` 要求 `forward_mode.is_cuda_graph()`、runner 存在、且 `runner.can_run_graph(batch)` 三者同时成立。

快路——decode CUDA Graph 回放，命中即提前返回：

> [model_runner.py:1438-1443](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L1438-L1443) —— `ret = self.decode_cuda_graph_runner.execute(forward_batch, pp_proxy_tensors=...)`，包成 `ModelRunnerOutput(logits_output=ret, can_run_graph=True)` 直接返回，**不再往下走**。

慢路的预处理与三分发：

> [model_runner.py:1445-1499](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L1445-L1499) —— 先 `_prepare_eager_forward_batch`（DP/MLP 同步 padding、attn-tp 归一），再按 `forward_mode` 分派：`is_split_prefill()` → `forward_split_prefill`；满足 prefill 图条件 → `prefill_cuda_graph_runner.execute`；否则 → `self.eager_runner.execute(forward_batch, pp_proxy_tensors=...)`。注释里点明只有 decode 图那条路会提前 return，其余三条都跑「活批」、都需要先 padding。

那 `eager_runner.execute` 内部到底干了什么？它按 `forward_mode` 再分派一次，最终调用模型本体：

> [eager_runner.py:194-204](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/runner/eager_runner.py#L194-L204) —— `EagerRunner.execute`：decode → `_execute_decode`、idle → `_execute_idle`、extend（含 draft_extend_v2）→ `_execute_extend`。

> [eager_runner.py:219-250](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/runner/eager_runner.py#L219-L250) —— `_execute_decode`：必要时 `load_batch`（把活批拷进固定大小的静态 buffer）、`attn_backend.init_forward_metadata(forward_batch)`（生成注意力元数据），最后 `return model_runner.model.forward(input_ids, positions, forward_batch, **kwargs)`。这就是整条链最底层的「模型前向」调用。

> [eager_runner.py:332-337](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/runner/eager_runner.py#L332-L337) —— `_execute_extend` 的普通分支同样是 `model_runner.model.forward(input_ids, positions, forward_batch, **kwargs)`。

把整条链收束成一句话：**`_forward_raw` 选路径，`EagerRunner` 是「真正跑 `model.forward`」的那条路径**。CUDA Graph 路径在内部也是调用同样的模型 forward，只不过是在「捕获阶段」录一次、之后反复 replay。

#### 4.3.4 代码实践

**实践目标**：用 `--cuda-graph` 开关对比 decode 路径，并在源码里定位「replay 前填哪些 buffer」。

**操作步骤（含可运行命令）**：

1. 启动两个服务对比（小模型，单卡即可，待本地验证 GPU 可用）：
   ```bash
   # 路径 A：开启 CUDA Graph（默认）
   sglang serve --model-path <small-model> --cuda-graph-bs 1,2,4,8
   # 路径 B：关闭 CUDA Graph
   sglang serve --model-path <small-model> --disable-cuda-graph
   ```
2. 用 [examples/frontend_language/quick_start/local_example_chat.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/examples/frontend_language/quick_start/local_example_chat.py) 的方式发若干短请求，观察两次的 per-token 延迟差异。
3. 源码侧：在 [model_runner.py:1438-1443](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L1438-L1443) 的 `decode_cuda_graph_runner.execute(...)` 调用处，跳进 `DecodeCudaGraphRunner.execute`（[decode_cuda_graph_runner.py:1210](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/runner/decode_cuda_graph_runner.py#L1210)），定位它在 replay 前如何 `load_batch` 填充静态 buffer。

**需要观察的现象**：开启 CUDA Graph 时 decode 阶段的 CPU 开销显著降低（每 token 延迟更稳更低）；关闭后 decode 走 `eager_runner._execute_decode`，每个 op 都现 launch。

**预期结果**：能说出 `can_run_graph=True` 时走的是 `decode_cuda_graph_runner.execute`（快路），`can_run_graph=False` 时走 `eager_runner.execute`（慢路），两条路最终都把 `forward_batch` 喂给同一个 `model.forward`。

> 待本地验证：实际延迟数字取决于硬件与模型，本讲不预设具体数值。

#### 4.3.5 小练习与答案

**练习 1**：`can_run_graph` 由哪三个条件共同决定？为什么 decode 容易满足而 prefill 不容易？

**参考答案**：三个条件是——`forward_mode.is_cuda_graph()`、`self.decode_cuda_graph_runner` 存在、且 `runner.can_run_graph(forward_batch)` 返回真。decode 阶段每个请求每步只算 1 个 token（或固定的草稿 token 数），批形状可枚举，所以能预先录制若干固定 batch size 的图；prefill 阶段每批 token 总数随 prompt 长度变化，形状不固定，难以穷举录制，故默认走 eager 或分段图。

**练习 2**：`_forward_raw` 里有四个出口（split_prefill / prefill_cuda_graph / decode_cuda_graph / eager），它们的共同终点是什么？

**参考答案**：都是把 `forward_batch`（含 `input_ids`、`positions`、`out_cache_loc`、注意力元数据）喂给同一个模型 `self.model.forward(input_ids, positions, forward_batch, ...)`，差别只在于「eager 现算」还是「replay 录好的图」、以及要不要做切分/分段。

**练习 3**：为什么 decode CUDA Graph 命中时要 `return` 提前退出，而其它三条路径不提前退出？

**参考答案**：decode 图路径在自己的 `execute` 内部已经把输入 padding、buffer 填充、replay 全做完了，结果直接可用；而其它三条路径之前还需要 `_prepare_eager_forward_batch`（DP/MLP padding、attn-tp 归一）等公共预处理，且执行后还可能需要 `post_forward_mlp_sync_batch` 等收尾，所以不能提前 return（见 [model_runner.py:1501-1507](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L1501-L1507)）。

---

### 4.4 运行期配置访问：get_exec() 命名空间袋与 get_context().override()

#### 4.4.1 概念说明

本讲的源码里大量出现 `get_exec().moe.elastic_ep_backend`、`get_schedule().disable_overlap_schedule`、`get_model().model_path` 这样的写法。这是 SGLang 配置体系重构后的标准读法（详见 u2-l5）。要点回顾：

- 每个进程在启动时由 `publish(server_args, role=...)` 把只读 `ServerArgs` 快照成若干**命名空间袋**（`_ConfigBag`），如 `device` / `model` / `exec` / `schedule` / `spec` / `lora` / `parallel` 等。
- 读配置应走对应访问器：`get_exec()`、`get_schedule()`、`get_model()`、`get_spec()`、`get_lora()`、`get_parallel()`、`get_device()`。袋是只读的，且读取可被 `torch.compile` 追踪。
- **运行期改写配置**不能再 `server_args.x = y`（会被 `__setattr__` 守卫拦截），也不能再 `server_args.override(...)`，唯一合法入口是 `get_context().override(source, **fields)`：它只写袋、不碰 `server_args`、全或无校验、并记 provenance。

在 `model_runner.py` 与 `tp_worker.py` 里，你能看到这次重构（RFC 相关的配置迁移）把几乎所有的 `self.server_args.x` / `get_server_args().x` 都替换成了命名空间袋访问，把 `self.server_args.override(...)` 替换成了 `get_context().override(...)`。

#### 4.4.2 核心流程

```
进程启动:
  publish(server_args, role="scheduler")
      → set_server_args + 把字段按 NS(...) 标注拆进各 _ConfigBag

运行期读:
  get_exec().features.enable_tf32_matmul      # exec.features 子袋
  get_exec().moe.elastic_ep_backend           # exec.moe 子袋
  get_schedule().disable_overlap_schedule     # schedule 袋（平铺）
  get_model().model_path / .model_impl        # model 袋
  get_spec().speculative_draft_model_path     # spec 袋
  get_parallel().enable_dp_attention          # parallel 袋

运行期改写（弹性 EP 扩容等场景）:
  get_context().override("elastic_ep.scale", dp_size=target_size)
      → 只写 config 袋，server_args 保持原始 RAW 记录
```

注意 `exec` 袋下还有子结构（`features` / `moe` / `graph` / `comm` / `deterministic`），而 `schedule` / `model` 等袋多为平铺字段。这是「逻辑命名空间」与「文件物理位置」解耦的结果——一个字段归在哪个袋，由它声明处的 `NS('xxx')` 标注决定，与它在 `server_args.py` 里写在哪个方法无关。

#### 4.4.3 源码精读

访问器的定义极其简单——它们都返回某个命名空间袋：

> [runtime_context.py:1032-1057](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L1032-L1057) —— `get_device` / `get_model` / `get_exec` / `get_schedule` / `get_memory` / `get_spec` / `get_lora` 都是 `return _CONTEXT.config_bag("<name>")`。注释明确：在 `publish` 之前调用会抛 `ValueError("... not published")`。

`publish` 的语义——每进程一次，记录角色并装入 server_args：

> [runtime_context.py:1076-1088](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L1076-L1088) —— `publish(server_args, *, role, hf_config=None)`。draft worker 跳过 publish，避免覆盖目标模型的进程级全局态。

`ModelRunner.__init__` 里的典型读法（替代了旧的 `get_server_args().enable_tf32_matmul`）：

> [model_runner.py:310-312](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L310-L312) —— `if get_exec().features.enable_tf32_matmul: torch.set_float32_matmul_precision("high")`。`enable_tf32_matmul` 属于 `exec.features` 子袋。

`forward` 收尾里的两处读法，分别取自 `schedule` 袋与 `exec.moe` 子袋：

> [model_runner.py:1323](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L1323) —— `no_copy_to_cpu = not get_schedule().disable_overlap_schedule`。

> [model_runner.py:1353-1354](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L1353-L1354) —— `if get_exec().moe.elastic_ep_backend is not None: self.maybe_join_ep_ranks()`。

`TpModelWorker` 构造时的读法，主/草稿模型分别从 `model` 与 `spec` 袋取值：

> [tp_worker.py:403-420](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tp_worker.py#L403-L420) —— 见 4.1.3 引用。`mem_fraction_static` 取自 `get_schedule()`（[tp_worker.py:427](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tp_worker.py#L427)）。

**运行期改写的标准写法**——弹性 EP 扩容时更新 `dp_size`，旧代码是 `self.server_args.override(...)`，现在改为 `get_context().override(...)`：

> [model_runner.py:1708-1710](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L1708-L1710) —— `_elastic_scale_up` 收尾：`from sglang.srt.runtime_context import get_context` + `get_context().override("elastic_ep.scale", dp_size=target_size)`。

`update_model_fields`（RL 权重热更新等场景）同样改走 `get_context().override`：

> [model_runner.py:1835-1842](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L1835-L1842) —— 热更新模型后 `get_context().override("model_runner.update_model_fields", model_path=..., load_format=...)`。

一个值得细看的「半迁移」点：`configure_kv_cache_dtype` 在解析出最终 KV cache dtype 后，要把它写回袋让 `get_model().kv_cache_dtype` 的读者看到；但注意它**故意仍直接读 `self.server_args.kv_cache_dtype` 作为解析输入**（因为 `server_args` 保持原始 RAW 记录）：

> [model_runner.py:1102-1126](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L1102-L1126) —— 解析输入读 `self.server_args.kv_cache_dtype`（RAW），解析结果经 `get_context().override(...)` 写进袋；同时新增了 `self.kv_cache_dtype_str` 字段，供注意力后端直接读本 runner 解析出的 dtype（避免 draft runner 读到目标模型的 dtype）。

这条「RAW 作输入、袋作输出」的细节，正是理解 SGLang 配置迁移后「什么该读 `server_args`、什么该读袋」的关键判据。

#### 4.4.4 代码实践

**实践目标**：能在源码里区分「读哪个袋」与「改写走哪个入口」，并能自己给一处旧代码做迁移。

**操作步骤（源码阅读型）**：

1. 在 `model_runner.py` 里 `grep` 出所有 `get_exec()` / `get_schedule()` / `get_model()` / `get_lora()` / `get_parallel()` / `get_spec()` 调用，按袋归类。回答：`enable_lora` 在哪个袋？`enable_dp_attention` 在哪个袋？`mem_fraction_static` 在哪个袋？
2. 用 `git log -p` 查看 [model_runner.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py) 里 `_initialize_elastic_ep_joiner`（[model_runner.py:386-447](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L386-L447)）的近期改动，确认它从 `self.server_args.elastic_ep_backend` 变成了 `get_exec().moe.elastic_ep_backend`、`self.server_args.ep_join_rank_offset` 变成了 `get_parallel().ep_join_rank_offset`。

**需要观察的现象**：你会看到一个清晰的对照——同一个逻辑概念（如 EP 后端）现在统一从 `exec.moe` 子袋读，而并行拓扑类字段（`ep_join_rank_offset`）从 `parallel` 袋读。

**预期结果**：写出一个「字段 → 袋」的小对照表，至少 6 行；并能把任意一处 `self.server_args.foo = bar` 改写为 `get_context().override("<source>", foo=bar)`。

> 待本地验证：可选地，在 `forward` 收尾处临时加 `logger.info(get_schedule().disable_overlap_schedule)`，分别在 `--disable-overlap-schedule` 开 / 关时启动，确认袋的值随 CLI 改变。

#### 4.4.5 小练习与答案

**练习 1**：`configure_kv_cache_dtype` 为什么解析输入读 `self.server_args.kv_cache_dtype`，而解析结果却要写进袋（`get_context().override`）？

**参考答案**：`server_args` 是「原始 RAW 用户意图」的只读留档，作为解析器的输入是稳定的；解析结果（可能被权重配置修正过的最终 dtype）是派生态，必须写进袋才能让 `get_model().kv_cache_dtype` 等读者看到。如果反过来把结果写回 `server_args`，就破坏了「`server_args` 保持 pristine」的不变量。

**练习 2**：为什么 draft worker 的 `kv_cache_dtype` 不读 `get_model()` 袋，而要新增 `self.kv_cache_dtype_str`？

**参考答案**：draft worker 不调用 `publish`，进程级 `get_model()` 袋里装的是**目标模型**的配置。若 draft 直接读袋，会拿到目标的 dtype，错误地驱动 draft 自己的 FP8 cast/descale 路径。所以每个 runner 把自己解析出的 dtype 存在 `self.kv_cache_dtype_str`，注意力后端直接读本 runner 的字段。

**练习 3**：把下面这行旧代码迁移成新写法：`self.server_args.override("elastic_ep.scale_join", dp_size=join_effective_ep_size)`。

**参考答案**：改为
```python
from sglang.srt.runtime_context import get_context
get_context().override("elastic_ep.scale_join", dp_size=join_effective_ep_size)
```
这正是 [model_runner.py:445-447](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L445-L447) 在本次迁移中做的改动。注意 `server_args` 自身不再被运行期改写。

---

## 5. 综合实践

把本讲四个最小模块串起来，做一次「全链路追踪」。

**任务**：给定一个普通的单卡 decode 步骤，从调度器开始，一路追到 `model.forward(...)`，并回答下列问题（建议用一张大图记录）：

1. **调用链**：写出从 `Scheduler.run_batch` 到 `self.model.forward(...)` 的完整方法调用序列（至少 6 个方法）。
2. **数据演变**：标注数据结构在哪一步从 `ScheduleBatch` 变成 `ForwardBatch`，又在哪一步变成 `LogitsProcessorOutput`，最后在哪一步变成 `GenerationBatchResult`。
3. **路径选择**：假设这一步是 decode 且 `batch_size` 在已录制的图尺寸里，`_forward_raw` 会走哪条出口？`can_run_graph` 会被设成什么？为什么能提前 `return`？
4. **张量来源**：`model.forward` 收到的 `input_ids`、`positions` 这两个张量的形状分别是什么？它们在 `ForwardBatch` 里由谁负责填好？（提示：`input_ids.shape[0]` 对 decode 等于 `batch_size`，对 prefill 等于本批 token 总数；联系 u4-l2 的 `out_cache_loc`。）
5. **配置来源**：在这一步里，`disable_overlap_schedule`（决定 `no_copy_to_cpu`）从哪个命名空间袋读？如果要在运行期临时关掉 overlap，应该调用哪个入口？

**参考做法（源码阅读型，不要求运行）**：

- 调用链：`run_batch` → `model_worker.forward_batch_generation`（[scheduler.py:3381](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/scheduler.py#L3381)）→ `ForwardBatch.init_new`（[tp_worker.py:545](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tp_worker.py#L545)）→ `ModelRunner.forward`（[model_runner.py:1262](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L1262)）→ `_forward_raw`（[model_runner.py:1406](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L1406)）→ `decode_cuda_graph_runner.execute`（快路）或 `eager_runner.execute`（慢路）→ `model.forward`（[eager_runner.py:245](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/runner/eager_runner.py#L245)）。
- 数据演变：`ScheduleBatch`（`forward_batch_generation` 入参）→ `ForwardBatch`（`init_new` 之后）→ `LogitsProcessorOutput`（`forward` 返回的 `ModelRunnerOutput.logits_output`）→ `GenerationBatchResult`（采样后回包）。
- 路径选择：走 decode CUDA Graph 快路，`can_run_graph=True`，因为输入 padding / buffer 填充 / replay 都在 `decode_cuda_graph_runner.execute` 内部完成，结果直接可用故提前 return。
- 张量来源：decode 时 `input_ids` 形状 `[batch_size]`、`positions` 形状 `[batch_size]`；它们与 `out_cache_loc`（`[batch_size]`，见 u4-l2）一起由 `ForwardBatch.init_new` / runner 的 `load_batch` 填好。
- 配置来源：`get_schedule().disable_overlap_schedule`（[model_runner.py:1323](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/model_runner.py#L1323)）；运行期临时改写走 `get_context().override(...)`。

> 待本地验证：第 3、4 题的张量形状与图尺寸判定，建议在真实小模型 + 单卡上用 [examples/runtime/engine/offline_batch_inference.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/examples/runtime/engine/offline_batch_inference.py) 跑一段，在 `_execute_decode` 的 `model.forward(...)` 前打印 `forward_batch.input_ids.shape` 实测确认。

## 6. 本讲小结

- `ModelRunner` 是「调度器/Worker」与「PyTorch 模型」之间的桥梁，在一个对象里持有模型、注意力后端、KV 内存池、三种前向执行器与采样器；`TpModelWorker` 包着它，对调度器暴露统一的 `forward_batch_generation`。
- `TpModelWorker.forward_batch_generation` 负责「翻译批 + 编排采样」：把 `ScheduleBatch` 经 `ForwardBatch.init_new` 变成 `ForwardBatch`，调 `ModelRunner.forward`，再按是否 verify / 是否 overlap+文法 / 是否 prefill-only 决定采样时机。
- `ModelRunner.forward` 做前后处理（性能分析、canary、专家分布记录、各种 capturer 收尾），真正的路径选择在 `_forward_raw`。
- `_forward_raw` 有四条出口：decode CUDA Graph 回放（快路，命中即提前 return）、split-prefill、prefill CUDA Graph（分段图）、eager（decode/extend/idle 即时前向）；前三条之外的 `eager_runner.execute` 是「真正调用 `model.forward`」的那条路径。
- `can_run_graph` 是性能关键：它决定 decode 是否走录好的图重放；decode 形状可枚举故易满足，prefill 形状多变故默认走 eager。
- 运行期配置已迁移到命名空间袋：读用 `get_exec()` / `get_schedule()` / `get_model()` / `get_spec()` / `get_lora()` / `get_parallel()` 等，改写一律走 `get_context().override(source, **fields)`；`server_args` 退化为只读 RAW 留档，作解析输入用，不再被运行期写。

## 7. 下一步学习建议

- **[u5-l2（ForwardBatch）](u5-l2-forward-batch.md)**：本讲把 `ForwardBatch` 当成了既成事实，下一讲会逐字段拆开它，讲清 `input_ids` / `seq_lens` / `out_cache_loc` / `extend_start_loc` / `forward_mode` 各自的含义与 prefill/decode 差异。
- **[u5-l3（注意力后端）](u5-l3-attention-backends.md)**：本讲里反复出现的 `attn_backend.init_forward_metadata` 和 `attn_backend` 的来历，下一讲会讲清它的注册与选择机制（FlashInfer / Triton / FA3 等）。
- **[u7-l1（CUDA Graph）](u7-l1-cuda-graph.md)**：本讲只讲了「decode 图命中就 replay」，专家层的捕获、buffer registry 填充、`CudaGraphConfig` 的 phase/backend 等细节留到 CUDA Graph 专讲。
- **动手阅读建议**：对照 [eager_runner.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/model_executor/runner/eager_runner.py) 的 `_execute_decode` / `_execute_extend`，再打开任一模型（如 [llama.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py)）的 `forward`，确认 `model.forward(input_ids, positions, forward_batch)` 的三个参数如何流入 decoder 层——这是 u5-l5 的内容。
