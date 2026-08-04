# AR 解码加速：异步输出实例化（Async Output Materialization）

## 1. 本讲目标

本讲是 AR 模块（U4）的性能优化收口篇。读完后你应当能够：

- 说清楚在未启用本特性时，AR 解码每一步（decode step）在 `sample_tokens()` 关键路径上要额外做哪些 CPU 侧工作，以及它们为什么会拖慢「下一步解码」的启动。
- 解释「异步输出实例化」如何把 `OmniModelRunnerOutput` 的构造（D2H 拷贝、张量切片、payload 拼装、连接器信号回收）移到后台线程，让第 N 步的 payload 构造与第 N+1 步的 GPU 解码重叠。
- 画出 `OmniAsyncGPUModelRunnerOutput` 的输出生命周期，并定位「快照 step-local 状态 → clone 输出张量 → 专用 CUDA stream D2H → 后台 builder 构造 payload → `get_output` join 收尾」在源码中的位置。
- 列出该特性自动启用的全部运行时守卫条件，并解释 Thinker / Talker / Code2Wav 三个 stage 的差异化行为（特别是 Talker 为什么必须先 eager 执行 decode-state postprocess）。
- 说明 Ascend NPU 为什么不走这条异步路径，而是仍由 `NPUARModelRunner` 同步构造输出。

## 2. 前置知识

本讲建立在 u4-l1（AR 模块总览）与 u4-l3（多模态输出处理）之上，复用其中已建立的概念。这里只补三个本讲特有的背景：

- **解码关键路径（critical path）**：AR 解码是「一步一步往前走」的。第 N 步采样新 token 后，调度器必须尽快拿到这些 token id 才能启动第 N+1 步的前向。凡是夹在「采样完成」与「下一步前向启动」之间、且只服务于「输出/下游交付」的工作，都是关键路径上的浪费。
- **D2H / CUDA stream / CUDA event**：把 GPU 张量搬到 CPU 叫 **D2H（Device-to-Host）拷贝**。如果在默认 stream 上同步拷贝，会阻塞 GPU；正确做法是用一条**专用拷贝 stream** 做 `non_blocking` 异步拷贝，并用一个 **CUDA event** 记录「拷贝何时完成」，消费方按需 `synchronize()` 这个 event 即可。本特性大量依赖这个模式。
- **async chunk**：vLLM-Omni 的阶段间增量传输机制（见 `docs/design/feature/async_chunk.md`），它把一个 stage 的输出切成小块流式投递给下一 stage。**异步输出实例化是 async chunk 的下游伴侣**：async chunk 负责「把输出切成块在阶段间流」，本特性负责「这些块的 CPU 构造不要卡住解码」。二者关系见 [文档 Overview 段](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L12-L59)。

> 名词速查：`sample_tokens()` 是 vLLM-Omni AR runner 的两阶段执行后半段（前半段 `execute_model()` 只跑前向、返回 `None` 并把中间产物暂存进 `ExecuteModelState`），负责采样、账本更新、输出构造与隐藏态/多模态交付（详见 u4-l1）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `docs/design/feature/omni_async_output_materialization.md` | 本特性的权威设计文档，含动机、性能、架构、启用条件与各平台兜底。 |
| `vllm_omni/worker/gpu_ar_model_runner.py` | 实现主体：`OmniAsyncGPUModelRunnerOutput` 异步输出类、安全张量快照工具、守卫条件、后台 builder，以及 `sample_tokens()` 中把同步/异步路径分流的那段装配代码。 |
| `vllm_omni/platforms/npu/worker/npu_ar_model_runner.py` | Ascend NPU 的独立 AR runner，说明它为何仍同步构造 `OmniModelRunnerOutput`。 |
| `vllm_omni/deploy/qwen3_omni_moe.yaml` | Qwen3-Omni 的默认部署配置，`async_chunk` 与各 stage 的 `async_scheduling` 在此声明。 |
| `vllm_omni/model_executor/models/qwen3_omni/qwen3_omni.py` | Thinker / Talker 模型侧的 opt-in 标志声明。 |
| `vllm_omni/worker/omni_connector_model_runner_mixin.py` | `should_accumulate_full_payload_output()`，证明本特性路径不会被 full-payload 累积分支同时命中。 |

## 4. 核心概念与源码讲解

### 4.1 为什么需要异步实例化：解码关键路径上的 CPU 开销

#### 4.1.1 概念说明

一个 AR stage 每一步必须**尽快**返回采样得到的 token id——调度器要靠它们启动下一步解码。但 vLLM-Omni 的 AR stage 不只吐 token：它还要交付

- 每请求的隐藏态（`pooler_output`，喂给下游 stage 的语义向量，见 u4-l1）；
- 多模态张量（`multimodal_outputs`，如音频/图像 latent，见 u4-l3）；
- 流式阶段间 wire payload（`inter_stage_outputs`，async chunk 要的「块」）；
- 连接器信号（`get_omni_connector_output()`，OmniConnector 控制面，见 u3-l4）。

构造这些 payload 需要张量 D2H、切片、`flatten_payload`、Python 对象拼装。问题在于：**这些工作里没有一项是「下一步解码」需要的**，但在未启用本特性时，它们全部内联在 `sample_tokens()` 里，必须跑完才能返回、才能启动下一步前向。于是每一步解码都被这段 CPU 工作撑开一个「空隙」。

#### 4.1.2 核心流程

文档用两段伪代码精确刻画了「搬走前后」的差别。**搬走前**，关键路径上一串 CPU 工作（[文档 L26-L37](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L26-L37)）：

```text
sample tokens
  -> update decode state
  -> copy hidden and multimodal outputs to CPU   ← 下一步解码不需要
  -> build per-request payloads                  ← 下一步解码不需要
  -> build the streaming wire payload            ← 下一步解码不需要
  -> collect connector signals                   ← 下一步解码不需要
  -> return sampled tokens
  -> launch the next decode step
```

**搬走后**，关键路径上只留「下一步解码必需的工作」（[文档 L39-L55](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L39-L55)）：

```text
sample tokens
  -> update decode state
  -> snapshot output state and start asynchronous D2H copies
  -> register sampled tokens
  -> launch the next decode step

background output path
  -> wait for the payload snapshot
  -> build per-request payloads
  -> build the streaming wire payload
  -> collect connector signals
  -> construct OmniModelRunnerOutput
```

效果就是**时间重叠**：第 N 步的 payload 构造（后台线程）与第 N+1 步的 GPU 解码（主路径）并行。文档用一张图量化了收益——对 Qwen3-Omni 的 Talker，连续两步解码之间的「观察空隙」从约 2.8 ms 降到 41 µs（[文档 L70-L87](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L70-L87)）。

> 一个常被忽略的要点：本特性**不改变模型计算与生成结果**。文档明确说明它只是把「构造」挪了位置，并用同步安全拷贝路径做了输出哈希对照验证（文本/音频哈希一致），见 [文档 L122-L126](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L122-L126) 与性能表 [文档 L102-L120](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L102-L120)（在并发 64 下，吞吐 +22%、平均音频 RTF -25%）。

#### 4.1.3 源码精读

关键路径的「分流点」在 `sample_tokens()` 末尾。runner 先把所有**纯标量/列表的 step-local 元数据**就地深拷一份快照（请求 id、token span、采样结果、logprobs 等），再决定走同步还是异步构造：

- `sample_tokens()` 先采样、做账本更新（`_bookkeeping_sync`），这是无论哪条路径都必须留在关键路径上的工作；
- 随后构造一组「快照变量」并定义一个 **`output_builder` 闭包**——真正「构造 `OmniModelRunnerOutput`」的全部 CPU 工作都被封进了这个闭包里：

[vllm_omni/worker/gpu_ar_model_runner.py:2066-2118](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L2066-L2118)：先把 `query_start_loc`、`scheduler_output`、`req_ids`、`valid_sampled_token_ids`、`logprobs`、`num_nans_in_logits` 全部拷成快照，再定义 `output_builder()`。闭包里调用 `_build_omni_model_runner_output_from_snapshot(...)`，正是「按请求切 hidden、拼 pooler payload、分区 inter/client、构造 wire、回收连接器信号」的所在地（实现见 [vllm_omni/worker/gpu_ar_model_runner.py:1725-1902](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L1725-L1902)，其中末尾 [L1899-L1900](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L1899-L1900) 回收连接器信号 `get_omni_connector_output()`）。

- 最后用一个分支决定闭包何时执行：

[vllm_omni/worker/gpu_ar_model_runner.py:2120-2144](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L2120-L2144)：`not use_async_omni_output` 时**立即**调用 `output_builder()` 同步构造；否则把闭包**作为 builder 传给** `OmniAsyncGPUModelRunnerOutput`，延后到后台线程执行。

读懂这段就抓住了「关键路径瘦身」的全部要义：被搬走的不是某行代码，而是「何时执行 `output_builder`」。

#### 4.1.4 代码实践

**实践目标**：在源码中验证「被搬走的 CPU 工作到底是什么」。

1. 打开 `_build_omni_model_runner_output_from_snapshot`（[L1725-L1902](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L1725-L1902)）。
2. 逐段标注每段 `record_function_or_nullcontext("omni_output_builder:...")` 标记的工作：`process_additional_information`、`build_pooler_payloads`、`build_multimodal_outputs`、`get_omni_connector_output`。
3. 对照文档「搬走前」伪代码（[L26-L37](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L26-L37)），把这些 `omni_output_builder:*` 段一一对应到伪代码的 5 个步骤。

**需要观察的现象**：这些 `omni_output_builder:*` 标记全部出现在 `_build_omni_model_runner_output_from_snapshot` 内部，而**不出现在** `sample_tokens()` 的关键路径上（只在 `use_async_omni_output=False` 时才同步出现在关键路径）。这正是「搬走」的物证。

**预期结果**：你会得到一张「伪代码步骤 ↔ builder 内部代码段」的对照表，证明被异步化的就是这 5 类 CPU 工作。

#### 4.1.5 小练习与答案

**练习 1**：如果某个 AR stage 既不向下游 stage 交付任何 payload（`pooler_output`/`multimodal_outputs` 都为空），异步实例化还有意义吗？

**参考答案**：意义很小。被搬走的正是「构造下游 payload」的工作；若该 step 无下游 payload（`downstream_req_ids` 为空），builder 内部多数段会早退，关键路径上本就没多少 CPU 工作可搬。这也解释了为何本特性聚焦在「需要交付隐藏态/多模态 payload 的 Thinker/Talker」上。

---

### 4.2 输出生命周期：`OmniAsyncGPUModelRunnerOutput` 与后台 builder

#### 4.2.1 概念说明

vLLM 上游的 `AsyncGPUModelRunnerOutput` 解决的是另一个问题：在 async scheduling 下，把**采样的 token id** 异步拷到 CPU 并回填给 input batch，让调度器不必等采样同步完成。本特性的 `OmniAsyncGPUModelRunnerOutput` **继承**它，保留这套 token 反馈机制，**再叠一层后台 builder** 来异步构造完整的 `OmniModelRunnerOutput`。用文档的话说：「preserves the existing asynchronous sampled-token feedback while adding a background builder for the complete Omni output」（[文档 L132-L135](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L132-L135)）。

#### 4.2.2 核心流程

文档列出 7 步输出生命周期（[文档 L137-L153](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L137-L153)）。把它与代码对应起来：

1. `sample_tokens()` 采样并完成下一步解码必需的账本；
2. 快照 step-local 元数据（请求 id、token span、scheduler output、query start loc 等）；
3. **clone** CUDA payload 张量（防止 CUDA Graph/模型输出缓冲被下步前向覆写）；
4. 在**专用 CUDA stream** 上把 clone 后的 payload 拷到 pinned CPU 内存，用一个 event 记录「就绪」；
5. `OmniAsyncGPUModelRunnerOutput` 启动**后台线程**，等 payload event 就绪后构造 `OmniModelRunnerOutput`；
6. AR runner 立即把采样 token 的 CPU 副本登记进 input batch 并返回，让 async scheduling 推进；
7. 引擎调用 `get_output()` 时，**join** 后台 builder、传播其异常、再走上游实现完成 token/logprobs 收尾。

ASCII 时序（取自 [文档 L155-L169](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L155-L169)）：

```text
GPU / decode path                    Background output path
forward + sample
   |-- clone output tensors
   |-- enqueue D2H copy -----------> wait for D2H event
   |-- register sampled tokens             |-- slice per request
   |-- return async output                 |-- build multimodal payloads
   |                                       |-- partition streaming payloads
next decode step                           |-- build the wire payload
                                           |-- drain connector signals
                                           |-- build OmniModelRunnerOutput
```

#### 4.2.3 源码精读

`OmniAsyncGPUModelRunnerOutput` 的三个核心方法精确对应生命周期步骤 5–7。

**`__init__`：采样 token 反馈 + 启动后台线程。** 它先做和上游完全一样的「采样 token 异步拷 CPU」（这是驱动下一步解码的张量，语义不能动），再启动后台 builder 线程：

[vllm_omni/worker/gpu_ar_model_runner.py:151-L206](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L151-L206)：注释 [L186-L190](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L186-L190) 强调 token 反馈与上游一致；[L201-L206](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L201-L206) 创建并 `start()` 名为 `omni-async-output-builder` 的 daemon 线程，target 是 `_build_output_in_background`。

**`_build_output_in_background`：后台线程体，捕获异常。**

[vllm_omni/worker/gpu_ar_model_runner.py:215-L221](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L215-L221)：它先把当前线程绑定到正确 CUDA 设备，再调用 builder。关键是 `except BaseException` 把异常存进 `self._background_exception` 而不是抛出——因为后台线程抛异常无法被主线程捕获，必须**延迟到 `get_output` 再抛**。

**`get_output`：完成边界 + 异常边界。**

[vllm_omni/worker/gpu_ar_model_runner.py:223-L239](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L223-L239)：`join()` 后台线程，若有异常则 `raise`；再 `_build_model_runner_output_once()` 兜底（覆盖后台线程尚未跑完 builder 就被 `get_output` 的情形），最后 `super().get_output()` 走上游的 token/logprobs 收尾。文档把这一步定义为「the completion and exception boundary before the materialized output is consumed」（[文档 L196-L198](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L196-L198)）。

> **幂等保护**：`_build_model_runner_output_once()` 用 `self._model_runner_output is not None` 保证 builder 只跑一次，并在跑完后把 `_model_runner_output_builder` 置 `None` 释放闭包引用（[L208-L213](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L208-L213)）。这样无论「后台先跑完」还是「`get_output` 先到」，都不会重复构造。

#### 4.2.4 代码实践

**实践目标**：验证「异常延迟抛出」与「幂等只构造一次」两个不变量。

1. 阅读 `_build_output_in_background`（[L215-L221](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L215-L221)）与 `get_output`（[L223-L239](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L223-L239)）。
2. 追踪一个假想场景：后台 builder 抛 `RuntimeError`。画出异常的传播路径——后台线程存异常 → `get_output` join → `raise background_exception`。
3. 追踪「`get_output` 在后台线程之前被调用」的场景：`join` 会阻塞直到后台跑完，再 `_build_model_runner_output_once()` 时发现已构造、直接返回。

**需要观察的现象**：`get_output` 是消费 materialized 输出之前的唯一同步点；任何 builder 异常都只能从这里冒出来。

**预期结果**：你应当能解释为什么测试套件会专门测「background error propagation」（文档 Related Files 提到 `tests/worker/test_gpu_ar_model_runner.py` 覆盖此用例，[文档 L380-L381](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L380-L381)）。

#### 4.2.5 小练习与答案

**练习 1**：为什么不在后台线程里直接 `raise`，而要存进属性再在 `get_output` 抛？

**参考答案**：Python 后台线程（daemon thread）里抛出的异常不会传播到主线程，只会在线程退出时打印 traceback，主流程会拿到一个**错误的、未构造完整**的输出。把异常存进 `_background_exception`，并在 `get_output`（引擎消费输出的唯一入口）`join` 后重新抛出，才能把错误正确地暴露给调度链、避免「静默返回半成品」。

**练习 2**：`OmniAsyncGPUModelRunnerOutput` 继承上游 `AsyncGPUModelRunnerOutput`，二者各自负责什么？

**参考答案**：上游负责「采样 token id 的异步 CPU 拷贝与回填」（驱动下一步解码）；子类额外负责「完整 `OmniModelRunnerOutput` 的后台构造」（隐藏态/多模态/wire payload/连接器信号）。两者共享 `sampled_token_ids_cpu` 与 `async_copy_ready_event` 机制。

---

### 4.3 安全状态快照：clone + 专用 stream 的异步 D2H

#### 4.3.1 概念说明

后台 builder 运行时，**下一步调度已经开始了**——它会改写 runner 状态、重用 CUDA Graph 与模型输出缓冲。所以 builder 不能直接读「live」张量，必须读一份**与下一步前向解耦的快照**。文档把 builder 输入分成两类（[文档 L171-L205](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L171-L205)）：

- **快照态（detached snapshot）**：scheduler token 计数、请求 id 与映射、采样 token/logprobs、query start loc、token span、隐藏态与多模态输出张量、KV-connector/encoder-cache 输出。这些是「值拷贝」或「CUDA 张量 clone」。
- **live runner 态（runner-owned）**：连接器输出状态——`get_omni_connector_output()` 是在 builder 内、构造完 wire payload **之后**才去「活取」的每周期连接器信号。正确性依赖「该 builder 是其输出周期里唯一回收连接器信号的消费者」。

#### 4.3.2 核心流程

CUDA 张量必须 clone，因为输出缓冲可能被下步前向覆写。clone + 异步 D2H 的流水线是：

1. 在**当前 stream** 上 `clone` 所有 CUDA 张量（产生 `cuda_sources` 列表，持有 clone 引用防止被回收）；
2. 切到**专用拷贝 stream**，`wait_stream(source)` 等待 clone 完成；
3. 用 `non_blocking=True` 把 clone 拷进 pinned CPU 内存；
4. 在拷贝 stream 上 `record` 一个 event；
5. builder 等这个 event 就绪后再读 CPU 副本；event 就绪后即可清空 `cuda_sources`（GPU 端 clone 不再被需要）。

被快照的内容清单见 [文档 L175-L184](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L175-L184)。

#### 4.3.3 源码精读

四个工具协同完成「clone + 异步 D2H」。

**`_clone_cuda_tensor_payload`**：递归把 dict/list/tuple 里的 CUDA 张量 `detach().clone()`，并把 clone 追加进 `sources` 持有引用；CPU 张量则同步 clone（已是 host-owned 快照）。注释点明 clone 是为了「保护快照免被 CUDA Graph 输出缓冲复用」：

[vllm_omni/worker/gpu_ar_model_runner.py:65-L84](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L65-L84)。

**`_copy_tensor_payload_to_cpu`**：递归把 CUDA 张量拷进 `pin_memory` 的 CPU 空虚张量，`copy_(non_blocking=True)`：

[vllm_omni/worker/gpu_ar_model_runner.py:87-L100](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L87-L100)。

**`_AsyncCPUPayloadSnapshot`**：持有 `payload`、就绪 `event`、`cuda_sources`。`wait()` 同步 event 并清空 GPU 源引用，且用 `_waited` 幂等：

[vllm_omni/worker/gpu_ar_model_runner.py:103-L121](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L103-L121)。

**`_snapshot_tensor_payload_to_cpu_async`**：把上面三者串成流水线——clone → 切专用 stream → 异步拷 → record event：

[vllm_omni/worker/gpu_ar_model_runner.py:124-L141](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L124-L141)。

> **一个被踩过的坑**：拷贝目的地必须是 **pinned**（锁页）内存，否则 `copy_(non_blocking=True)` 会退化成同步、阻塞 stream（文档注释里记了 Thinker prefill 上约 240 ms/step 的退化）。源码因此从平台助手取 `is_pin_memory_available()`，而不是用已失效的 `self.pin_memory`，见 [L1656-L1666](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L1656-L1666)。

**调度器输出快照**：`_snapshot_scheduler_output_for_async_omni_output` 把 `num_scheduled_tokens`、`scheduled_spec_decode_tokens` 这类会被下一步改写的 dict/list 拷一份，再用 dataclass `replace` 生成不可变副本（[L1559-L1575](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L1559-L1575)）。

**把快照装配进 builder 输入**：`_snapshot_omni_output_tensors_for_async_output` 是分流点。`use_async_omni_output=False` 时直接把 live 张量包进 `_OmniOutputTensorSnapshot` 返回（同步路径仍读 live）；为 `True` 时调用 `_snapshot_tensor_payload_to_cpu_async` 走 clone+D2H：

[vllm_omni/worker/gpu_ar_model_runner.py:1634-L1681](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L1634-L1681)。注意 [L1670-L1674](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L1670-L1674)：如果模型声明「不把 hidden 放进快照」（Talker 即如此），`hidden_states_snapshot` 会退成 `hidden_states[:0]`——即 Talker 跳过了不必要的 hidden D2H。

而 builder 闭包开头会先 `async_payload.wait()` 等 event 就绪（[L2096-L2098](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L2096-L2098)），这正是生命周期步骤 5 的「等 payload event」。

#### 4.3.4 代码实践

**实践目标**：用源码确认「专用 stream + pinned + event」三件套缺一不可。

1. 顺序阅读 `_clone_cuda_tensor_payload` → `_copy_tensor_payload_to_cpu` → `_AsyncCPUPayloadSnapshot` → `_snapshot_tensor_payload_to_cpu_async`（[L65-L141](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L65-L141)）。
2. 为每一步标注：它在哪条 stream 上执行？产出什么？event 记录在哪？
3. 找到 builder 闭包里的 `async_payload.wait()`（[L2096-L2098](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L2096-L2098)），确认 builder 读的是 CPU 副本而非 live 张量。

**需要观察的现象**：clone 在 source stream，D2H 在 copy stream，event 记录在 copy stream；`wait()` 之后 `cuda_sources.clear()` 才释放 GPU clone——这正是 [文档 L186-L187](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L186-L187) 所述「snapshot retains its cloned CUDA sources until the transfer event completes」。

**预期结果**：你能画出 clone/D2H/event/wait 在两条 stream 上的时序，并解释为什么 `pin_memory=False` 会让 `non_blocking` 失效。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `get_omni_connector_output()` 不进快照、而要在 builder 里「活取」？

**参考答案**：连接器输出状态是 **runner-owned 的 live 状态**，且文档明确要求它是「该输出周期里唯一回收连接器信号的消费者」（[文档 L189-L198](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L189-L198)）。它必须在 wire payload 构造**之后**回收，时序上无法预先快照；接收侧状态则由连接器 mixin 的 `_lock` 协调。

**练习 2**：`_snapshot_scheduler_output_for_async_omni_output` 为什么要用 `replace` 生成新 dataclass，而不是就地 `.copy()` 几个字段？

**参考答案**：`SchedulerOutput` 是被下一步调度**直接复用并改写**的对象。只拷贝内部 dict 字段、外壳仍是同一个对象，外壳上别的属性仍可能被改。用 `dataclass.replace` 生成一个外壳副本，保证 builder 拿到的是一个不可被下一步改写的稳定视图；若 `replace` 因类型不兼容失败，则退回原对象（保守降级）。

---

### 4.4 启用条件、守卫与 stage 差异化行为

#### 4.4.1 概念说明

本特性**没有独立的 CLI/YAML 开关**——它由 runner 在每个 stage 上按一组运行时条件**自动判定**。这体现了一个工程原则：异步实例化会改变输出生命周期的时序假设，必须由「模型侧声明其输出生命周期可安全延后」+「运行时条件满足」共同背书，缺一则该 stage 静默回退到同步构造（不阻断服务）。

#### 4.4.2 核心流程

守卫条件见文档表（[文档 L339-L348](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L339-L348)）。它们在 `_should_use_async_omni_output()` 里逐条实现，全部为真才走异步路径：

| 条件 | 实现 |
|---|---|
| AR async scheduling 开启 | `self.use_async_scheduling` |
| `async_chunk` 开启 | `model_config.async_chunk` |
| 模型 stage 用 `use_async_omni_output` 显式 opt-in | `model.use_async_omni_output` |
| Omni prefix cache 关闭 | `self.omni_prefix_cache is None` |
| 关闭投机解码 | `self.speculative_config is None` |
| 关闭 routed-expert 输出 | `not enable_return_routed_experts` |
| 无 postprocess，或 postprocess 显式 eager 执行 | `has_postprocess` ⇒ 需 `eager_omni_postprocess_before_async_output` |

三个 stage 的差异化行为（[文档 L223-L234](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L223-L234)）：

- **Thinker**：快照 hidden + 多模态，后台构造下游 payload。下一步 Thinker 解码只需采样 token 反馈。
- **Talker**：**先 eager 跑轻量 postprocess**（更新下一步需要的 decode state），再快照 codec 输出，并**从下游 payload 里剔除 hidden**（因为 Code2Wav 只吃 codec codes，不吃 Talker hidden）。
- **Code2Wav**：不走本路径——它是 generation stage，不由 `GPUARModelRunner` 执行。

#### 4.4.3 源码精读

**守卫函数**：

[vllm_omni/worker/gpu_ar_model_runner.py:1595-L1619](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L1595-L1619)：逐条对应上表。特别注意 [L1614-L1617](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L1614-L1617)：若模型声明了 `has_postprocess` 但**没有**声明 `eager_omni_postprocess_before_async_output`，则返回 `False`——也就是说「有 postprocess 但不 eager」的 stage 一律回退同步，避免把「下一步解码需要的状态更新」错误地推迟。

**Talker 的 eager postprocess**：`_maybe_run_eager_omni_postprocess_before_async_output` 在快照 D2H **之前**，于关键路径上对 live GPU 张量跑一次 `_process_additional_information_updates`，把 decode-state 更新落实，返回 `True` 标记「postprocess 已提前应用」，让 builder 跳过重复执行：

[vllm_omni/worker/gpu_ar_model_runner.py:1683-L1716](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L1683-L1716)。调用点在 [L2079-L2087](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L2079-L2087)，其返回值 `omni_postprocess_already_applied` 透传给 builder（[L2117](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L2117)），builder 内部据此跳过 `process_additional_information` 段（[L1818-L1830](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L1818-L1830)）。

**模型侧 opt-in（Thinker / Talker）**：

[vllm_omni/model_executor/models/qwen3_omni/qwen3_omni.py:133-L164](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/model_executor/models/qwen3_omni/qwen3_omni.py#L133-L164)：
- Thinker（[L133-L134](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/model_executor/models/qwen3_omni/qwen3_omni.py#L133-L134)）：只设 `use_async_omni_output = True`，无 postprocess、仍含 hidden。
- Talker（[L154-L164](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/model_executor/models/qwen3_omni/qwen3_omni.py#L154-L164)）：设 `has_postprocess = True` + `eager_omni_postprocess_before_async_output = True` + `omni_pooler_payload_include_hidden = False`。注释 [L158-L161](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/model_executor/models/qwen3_omni/qwen3_omni.py#L158-L161) 直白解释了 Talker 的取舍：hidden 要留在 GPU 给下一步 decode 用，Code2Wav 只需 codec codes，所以 hidden 不进下游 payload。

**部署 YAML**：

[vllm_omni/deploy/qwen3_omni_moe.yaml:15](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/deploy/qwen3_omni_moe.yaml#L15)：顶层 `async_chunk: true`（这是 async_chunk 与异步实例化共同的硬前提）。两个 AR stage（Thinker=stage0 [L26-L35](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/deploy/qwen3_omni_moe.yaml#L26-L35)、Talker=stage1 [L37-L50](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/deploy/qwen3_omni_moe.yaml#L37-L50)）都设 `enable_prefix_caching: false`（守卫要求之一），`async_scheduling` 对 AR stage 默认为 true（未显式写）。Code2Wav=stage2 则显式 `async_scheduling: false`（[L60](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/deploy/qwen3_omni_moe.yaml#L60)），且它是 generation stage，本就不过 `GPUARModelRunner`。

**与 full-payload 累积互斥**：文档强调本特性路径不会被 full-payload 累积分支同时命中——因为本特性要求 `async_chunk`，而 `should_accumulate_full_payload_output()` 在 `async_chunk` 开启时恒返回 `False`：

[vllm_omni/worker/omni_connector_model_runner_mixin.py:60-L61](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/omni_connector_model_runner_mixin.py#L60-L61)。文档对应说明见 [L199-L205](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L199-L205)。

> **前缀缓存互斥提示**：Omni 前缀缓存与异步实例化目前不能共存——`_should_use_async_omni_output()` 在 `omni_prefix_cache` 存在时返回 `False`（[L1598-L1599](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L1598-L1599)），原因与文档 [L326-L332](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L326-L332) 一致（前缀缓存 merge/update 的时序尚未在后台路径里同步）。

#### 4.4.4 代码实践

**实践目标**：解释「为什么 Talker 必须 eager postprocess，而 Code2Wav 根本不走本路径」。

1. 读 qwen3_omni.py 的 Talker opt-in（[L154-L164](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/model_executor/models/qwen3_omni/qwen3_omni.py#L154-L164)）。
2. 回答：Talker 的 postprocess（`talker_postprocess`）更新的是什么？为什么它必须在关键路径上、在 D2H 之前完成？（提示：它维护下一步 Talker decode 所需的 `hidden_states.last` 等 decode state。）
3. 读 YAML 的 stage2（[L52-L69](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/deploy/qwen3_omni_moe.yaml#L52-L69)），结合文档 [L229](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L229)，回答：Code2Wav 的执行类型是什么？它由哪个 runner 执行？为什么 `GPUARModelRunner._should_use_async_omni_output()` 对它根本不会被执行到？

**需要观察的现象**：Talker 的 `omni_pooler_payload_include_hidden = False` 让 hidden 不进下游 payload（省一次不必要的 D2H）；Code2Wav 是 generation stage（`async_scheduling: false`，且不归 `GPUARModelRunner` 管），所以异步实例化对它「不适用」而非「被守卫拒绝」。

**预期结果**：你能用一句话区分「Talker：属于 AR、需 eager postprocess、含 codec payload、不含 hidden」与「Code2Wav：属于 generation、不走 `GPUARModelRunner`、用正常 generation 输出路径」。

#### 4.4.5 小练习与答案

**练习 1**：把 `enable_prefix_caching` 从 false 改成 true（在 YAML 里），会怎样？

**参考答案**：`_should_use_async_omni_output()` 因 `self.omni_prefix_cache is not None` 返回 `False`（[L1598-L1599](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L1598-L1599)），该 stage 静默回退到同步输出构造。服务仍可用，只是丢失这部分吞吐优化。文档的 warning（[L320-L324](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L320-L324)）明确这点。

**练习 2**：为什么 `use_async_omni_output`、`eager_omni_postprocess_before_async_output`、`omni_pooler_payload_include_hidden` 是「模型实现契约」而非用户配置项？

**参考答案**：它们声明的是「该模型的输出生命周期是否可安全延后」「postprocess 是否必须 eager」「payload 是否含 hidden」这类**与模型内部时序假设强耦合**的事实，只有模型作者能正确判断。把它暴露成用户开关，会让用户在不懂后果的情况下触发错误时序（如把需要 eager 的 postprocess 推迟），导致结果错误。文档 [L361-L365](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L361-L365) 明确其为契约。

---

### 4.5 NPU 兜底：为什么 Ascend 仍走同步路径

#### 4.5.1 概念说明

vLLM-Omni 对 Ascend NPU 走的是**独立的** `NPUARModelRunner`（继承 vLLM-Ascend 的 `OmniNPUModelRunner`，而非 `GPUARModelRunner`，见 [npu_ar_model_runner.py:109-L110](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/platforms/npu/worker/npu_ar_model_runner.py#L109-L110)）。因为它**不继承 `GPUARModelRunner`**，`_should_use_async_omni_output()` 这套守卫与 `OmniAsyncGPUModelRunnerOutput` 这套后台 builder 对它根本不存在。于是 NPU 上的 Omni payload 实例化**保持同步**。

#### 4.5.2 核心流程

文档 Platform 段（[文档 L60-L68](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L60-L68)）与 Compatibility 段（[文档 L355-L359](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L355-L359)）明确：NPU 的 `sample_tokens()` 先**完整**构造 `OmniModelRunnerOutput`、调用 `get_omni_connector_output()` 回收连接器信号，**之后**才包成异步 wrapper——所以 Omni payload 实例化仍是同步的。它包成的也仅是上游 `AsyncGPUModelRunnerOutput`（只有采样 token 的异步反馈），不是 `OmniAsyncGPUModelRunnerOutput`（无后台 builder）。

#### 4.5.3 源码精读

对照 NPU `sample_tokens()` 的末尾，每一步都印证「同步」：

[vllm_omni/platforms/npu/worker/npu_ar_model_runner.py:1273-L1290](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/platforms/npu/worker/npu_ar_model_runner.py#L1273-L1290)：**直接、就地**构造完整的 `OmniModelRunnerOutput`（含 `multimodal_outputs`/`inter_stage_outputs`/`kv_connector_output`），并在 [L1289-L1290](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/platforms/npu/worker/npu_ar_model_runner.py#L1289-L1290) **同步** `get_omni_connector_output()`——这些工作全在 `sample_tokens()` 关键路径上完成。

[vllm_omni/platforms/npu/worker/npu_ar_model_runner.py:1316-L1328](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/platforms/npu/worker/npu_ar_model_runner.py#L1316-L1328)：仅在 `use_async_scheduling` 时包成 **`AsyncGPUModelRunnerOutput`**（注意类名——不是 `OmniAsync*`），传入的 `model_runner_output=` 是**已经构造好的** `model_runner_output`（[L1317](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/platforms/npu/worker/npu_ar_model_runner.py#L1317)），而不是 `model_runner_output_builder=`。这两点（类名不含 Omni、传值而非传 builder）正是「没有后台 builder、输出已就绪」的铁证。

> 对比 GPU 路径：GPU 在 `use_async_omni_output=True` 时传的是 `model_runner_output_builder=output_builder`（闭包，延后执行，[L2135-L2138](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L2135-L2138)），且类是 `OmniAsyncGPUModelRunnerOutput`。NPU 没有 builder 这个入参位置——因为它的 wrapper 不接受 builder。

#### 4.5.4 代码实践

**实践目标**：从两处代码差异确认「NPU 无后台 builder」。

1. 对比 GPU 与 NPU 的 wrapper 构造：
   - GPU：[gpu_ar_model_runner.py:2126-L2138](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L2126-L2138)（`OmniAsyncGPUModelRunnerOutput`，`model_runner_output_builder=`）。
   - NPU：[npu_ar_model_runner.py:1316-L1323](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/platforms/npu/worker/npu_ar_model_runner.py#L1316-L1323)（`AsyncGPUModelRunnerOutput`，`model_runner_output=`）。
2. 在 NPU 文件里全局搜索 `OmniAsyncGPUModelRunnerOutput`，确认**零命中**（待本地验证：可用 `Grep` 在 `vllm_omni/platforms/npu/` 下搜该类名）。

**需要观察的现象**：NPU runner 既不 import 也不使用 `OmniAsyncGPUModelRunnerOutput`；它的输出在包成异步 wrapper **之前**已 100% 构造完毕。

**预期结果**：你能给出结论——NPU 走的是「上游 async token 反馈 + 同步 Omni payload 构造」的组合，而非 GPU 的「async token 反馈 + 后台 Omni payload 构造」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 NPU 不能简单「复用」`OmniAsyncGPUModelRunnerOutput`？

**参考答案**：该类的后台 builder 依赖 CUDA 语义——专用 `torch.cuda.Stream`、`torch.cuda.Event`、pinned memory 的 `non_blocking` 拷贝。NPU（Ascend）是另一套运行时（用 `torch.npu`、`global_stream`、ACLGraph），其张量生命周期与拷贝语义不同；贸然复用会破坏正确性。因此 NPU 选择「同步构造 + 仅复用上游 token 反馈」的保守兜底，等后续单独适配。

**练习 2**：文档说 XPU「inherits `GPUARModelRunner`」、MUSA「selects `GPUARWorker`」，可能进入异步路径但未验证。这传递了什么工程信号？

**参考答案**：守卫里**没有** CUDA/ROCm-only 的平台硬限制（[文档 L349-L354](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L349-L354)）；只要其他条件满足，XPU/MUSA 会**自动**选中异步路径。但它们没做过正确性/性能验证，属于「能用但自担风险」。这是「验证范围（validation scope）≠ 运行时平台护栏（runtime guard）」的典型区分。

---

## 5. 综合实践

把本讲串起来，完成规格要求的源码追踪任务：**对照文档时序图，在 `gpu_ar_model_runner.py` 中定位 `sample_tokens → clone → D2H → 后台 builder → get_output join` 全链路，并解释 Talker / Code2Wav 的行为差异。**

### 步骤 1：画出两条路径的时序对照

打开文档的 ASCII 时序（[文档 L155-L169](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L155-L169)）。在源码里为每一行找到落点，填入下表（答案已给出，供你逐条核对）：

| 时序行 | 源码落点 |
|---|---|
| `forward + sample` | `sample_tokens()` 内 `_sample`（[L1962-L1963](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L1962-L1963)） |
| `clone output tensors` | `_clone_cuda_tensor_payload`（[L65-L84](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L65-L84)），经 `_snapshot_tensor_payload_to_cpu_async`（[L131](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L131)）触发 |
| `enqueue D2H copy` | `_copy_tensor_payload_to_cpu`（[L87-L100](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L87-L100)）于专用 stream（[L137-L140](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L137-L140)） |
| `register sampled tokens` | `set_async_sampled_token_ids`（[L2148-L2151](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L2148-L2151)） |
| `return async output` | `return async_output`（[L2153](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L2153)） |
| `wait for D2H event` | builder 闭包内 `async_payload.wait()`（[L2096-L2098](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L2096-L2098)） |
| `build OmniModelRunnerOutput` | `_build_omni_model_runner_output_from_snapshot`（[L1725-L1902](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L1725-L1902)） |
| 后台执行 | `_build_output_in_background`（[L215-L221](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L215-L221)） |
| join 收尾 | `get_output`（[L223-L239](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L223-L239)） |

### 步骤 2：解释 Talker 的「先 eager 再推迟」

阅读 Talker opt-in（[qwen3_omni.py:154-L164](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/model_executor/models/qwen3_omni/qwen3_omni.py#L154-L164)）与 `_maybe_run_eager_omni_postprocess_before_async_output`（[gpu_ar_model_runner.py:1683-L1716](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/worker/gpu_ar_model_runner.py#L1683-L1716)）。

**要解释清楚**：Talker 的 `talker_postprocess` 会更新下一步 Talker decode 依赖的 decode state（如 `hidden_states.last`）。这部分**必须 eager**，否则下一步解码读到的是旧状态、结果错误。而「codec payload 的构造」（Code2Wav 要的 codec codes 的 D2H、切片、wire 拼装）下一步解码不需要，可以安全推迟到后台。两者职责不同，所以 Talker 是「eager 跑 postprocess + 推迟 codec payload 构造」的组合，而非全推迟或全 eager。

### 步骤 3：解释 Code2Wav 为什么不走

阅读 YAML stage2（[qwen3_omni_moe.yaml:52-L69](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/deploy/qwen3_omni_moe.yaml#L52-L69)）与文档 [L229](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/docs/design/feature/omni_async_output_materialization.md#L229)。

**要解释清楚**：Code2Wav 是 **generation stage**（输入算完即一步出完，见 u4-l2 的 `OmniGenerationScheduler` 快路径），不由 `GPUARModelRunner` 执行（它走 generation runner），`async_scheduling: false`，且没有「下一步解码」需要尽快返回 token 的压力——它本来就是一步完成。因此本特性对它「不适用」，而非被守卫拒绝。

> 完成后，你应当能用一张图把「GPU 解码路径」与「后台输出路径」两条时间轴、Talker 的 eager postprocess 插入点、以及 Code2Wav 的旁路一次性讲清楚。

## 6. 本讲小结

- 异步输出实例化把 `OmniModelRunnerOutput` 的 CPU 侧构造（D2H、切片、payload 拼装、连接器信号回收）从 AR 解码关键路径移到后台线程，使第 N 步 payload 构造与第 N+1 步 GPU 解码重叠；它不改模型计算与生成结果（哈希对照验证）。
- `OmniAsyncGPUModelRunnerOutput` 继承上游 `AsyncGPUModelRunnerOutput`：保留采样 token 的异步反馈，叠加一个后台 builder 线程；`get_output()` 是完成与异常的唯一边界（join + 重抛后台异常）。
- 安全快照靠「CUDA 张量 clone + 专用 stream 的 pinned 非阻塞 D2H + event」实现，与下一步前向解耦；连接器信号是 runner-owned live 态，在 builder 里构造完 wire 后才活取。
- 启用由 `_should_use_async_omni_output()` 的 7 条运行时守卫自动判定（含 async scheduling、async_chunk、模型 opt-in、无前缀缓存/投机/routed-expert、postprocess 须 eager），任一不满足则静默回退同步。
- Thinker 全量快照、Talker 先 eager postprocess 再推迟 codec payload（且剔除 hidden）、Code2Wav 因是 generation stage 根本不走；Ascend NPU 用独立 `NPUARModelRunner`，仍同步构造输出。

## 7. 下一步学习建议

- **横向对照 async chunk**：阅读 `docs/design/feature/async_chunk.md`，理解「阶段间流式切块（async chunk）」与本讲「块自身的异步构造（async output materialization）」如何叠加，二者构成完整的流式交付加速。
- **回到调度链**：结合 u4-l2 的 `OmniARScheduler` 与 u4-l3 的 `MultimodalOutputProcessor`，跟踪异步实例化产出的 `OmniModelRunnerOutput` 如何被 scheduler 适配成 `OmniEngineCoreOutput` 并跨进程传递。
- **NPU 适配方向**：若关心多硬件，可对照本讲 4.5 节与 `vllm-omni-npu-upgrade` 技能，思考「为 NPU 单独实现后台 builder」需要解决哪些非 CUDA 语义问题。
- **验证手段**：阅读 `tests/worker/test_gpu_ar_model_runner.py`（文档 Related Files 提及），看测试如何覆盖快照、守卫、连接器顺序与后台错误传播这四个不变量。
