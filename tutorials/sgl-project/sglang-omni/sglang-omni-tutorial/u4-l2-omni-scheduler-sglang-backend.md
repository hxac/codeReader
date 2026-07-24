# OmniScheduler 与 SGLang 后端

## 1. 本讲目标

上一讲（u4-l1）我们打开了调度器黑盒的最简实现 `SimpleScheduler`，它适用于「来一个算一个」的非自回归（non-AR）阶段，比如预处理、编码器、聚合。但 omni/语音/TTS 管线里真正昂贵的是**自回归（AR）阶段**——thinker、talker 这类需要 KV 缓存、需要 prefill/decode 分离、需要 batch 选择的生成引擎。这些恰恰是上游 SGLang 已经打磨得极好的能力。

本讲要回答一个核心问题：**OmniScheduler 是如何「不重新发明轮子」地复用 SGLang 的 AR 调度，同时又不丢掉 omni 自己的传输、请求对象与流式行为的？**

学完后你应当掌握：

- OmniScheduler 与上游 SGLang `Scheduler` 的**组合边界**：哪些方法委托给 SGLang、哪些被 omni 自己改写。
- `create_sglang_infrastructure` 返回的那个**七元组**（worker / tree_cache / KV 池 / prefill / decode / model_config）分别是什么、为什么这样切。
- `build_sglang_server_args` 如何把 omni 的意图翻译成 SGLang 能理解的 `ServerArgs`。
- 在 `run_batch` 与 prefill/decode 主循环中，**哪些工作是 SGLang 干的、哪些仍是 omni 自己干的**。
- 在 async decode 快路径丢弃 stale 请求时，为何 **extend/mixed batch 的 per-token 字段必须由 omni 手动 reslice**，而 SGLang 的 `filter_batch` 做不到（这正是 commit #1027 修复的正确性问题）。

## 2. 前置知识

在进入本讲前，请确保你已经理解以下概念（它们在前序讲义中已建立）：

- **Stage 是 IO 外壳**（u3-l1）：Stage 收控制消息、读写 relay、做 fan-in 聚合，把所有计算推给 `scheduler.inbox`，scheduler 在独立线程算完放回 `scheduler.outbox`。
- **调度器五件套契约**（u4-l1）：`inbox`、`outbox`、`start()`、`stop()`、`abort(request_id)`。`SimpleScheduler` 的主循环就是 `inbox.get → compute_fn(data) → outbox.put`。
- **消息类型**（u4-l1）：进入调度器的 `IncomingMessage`（`new_request` / `stream_chunk` / `stream_done`）与离开的 `OutgoingMessage`（`result` / `stream` / `error`）。
- **AR 阶段与非 AR 阶段的区别**：非 AR 阶段无 KV 缓存，来一个算一个；AR 阶段需要为每个请求维护一段随生成增长的 KV 缓存，并区分 prefill（处理提示词）与 decode（逐 token 生成）。

本讲会反复用到两个 SGLang 术语，先做个通俗解释：

- **prefill**：处理输入提示词的阶段。提示词可能很长（如几千个 token），SGLang 会把它们**分块（chunked prefill）**填入，并与已缓存的公共前缀做**前缀匹配（radix tree）**以复用 KV。
- **decode**：逐个 token 生成新内容的阶段。每生成一个 token，KV 缓存就增长一格；当 KV 池快满时，SGLang 会**retract（回退）**一部分请求，把它们踢回 waiting 队列等下次重新 prefill，腾出显存。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
|------|------|
| [sglang_omni/scheduling/omni_scheduler.py](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py) | `OmniScheduler` 主体——AR 阶段对 Stage 暴露的调度器，用组合（而非继承）复用 SGLang 调度逻辑。 |
| [sglang_omni/scheduling/bootstrap.py](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/bootstrap.py) | `create_sglang_infrastructure`——构建并返回 SGLang 基础设施七元组。 |
| [sglang_omni/scheduling/sglang_backend/server_args_builder.py](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/sglang_backend/server_args_builder.py) | `build_sglang_server_args`——把 omni 的运行时意图组装成上游 `ServerArgs`。 |
| [sglang_omni/scheduling/sglang_backend/prefill.py](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/sglang_backend/prefill.py) | `PrefillManager`——管理 prefill 的 waiting 队列、分块、前缀匹配。 |
| [sglang_omni/scheduling/sglang_backend/decode.py](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/sglang_backend/decode.py) | `DecodeManager`——管理 decode 批次与 KV 满时的 retract。 |
| [sglang_omni/scheduling/sglang_backend/cache.py](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/sglang_backend/cache.py) | `create_tree_cache`——按配置创建 RadixCache 或 ChunkCache。 |
| [sglang_omni/scheduling/engine_factory.py](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/engine_factory.py) | `TtsEngineBuilder`——把 server_args、基础设施、model_runner、adapter 串成一个 `OmniScheduler` 的模板基类。 |
| [sglang_omni/models/qwen3_omni/bootstrap.py](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/models/qwen3_omni/bootstrap.py) | Qwen3-Omni thinker/talker 调度器的真实构造入口，是上述机制的活样本。 |

> 本讲 4.5 还会下钻 `omni_scheduler.py` 里 async decode 快路径的两个方法——`_drop_stale_overrun`（丢弃 stale 请求时按 token 粒度 reslice extend/mixed batch）与 `_free_overrun_step_slots`（释放 overrun 的 KV 槽），并以 [tests/unit_test/pipeline/test_async_decode.py](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/tests/unit_test/pipeline/test_async_decode.py) 的纯 CPU 单测作为可运行验证入口。

---

## 4. 核心概念与源码讲解

### 4.1 OmniScheduler 的组合策略：不继承，用 `__getattr__` 委托

#### 4.1.1 概念说明

最自然的复用方式是「继承」：`class OmniScheduler(SGLangScheduler)`。但 OmniScheduler 偏偏**不继承**，而是用 **unbound method + `__getattr__`** 的组合方式。为什么？

因为继承会把 omni 与 SGLang 上游的 `Scheduler.__init__`、属性初始化顺序**深度耦合**——上游 `__init__` 会建 tokenizer、detokenizer、grammar、metrics 等一堆 omni 用不到的子系统。omni 想要的是：**只借上游的「大脑」（batch 选择、结果处理、内存检查），不借它的「四肢」（tokenizer/detokenizer 管线、对外协议）**。

组合策略的直觉是：

- omni 自己维护所有调度**状态**（`waiting_queue`、`running_batch`、KV 池引用……）。
- 当调用一个 omni 没有定义的方法（比如 `get_next_batch_to_run`）时，Python 触发 `__getattr__`，它去上游 `Scheduler` **类**上找，找到后用 `types.MethodType` 把它**绑定到当前实例**，于是上游方法里的 `self.xxx` 读到的就是 omni 实例上的状态。

这样 omni 既能用到上游完整的调度方法解析顺序（MRO），又完全掌控了状态与生命周期。

对 Stage 而言，OmniScheduler 暴露的公开契约与 SimpleScheduler **完全一致**——这正应了 u3-l1 的「Stage 不因 scheduler 类型而分支」不变量：

> 公开契约：`inbox`、`outbox`、`start()`、`stop()`、`abort(request_id)`。

#### 4.1.2 核心流程

`OmniScheduler` 的方法分为三类：

1. **omni 自己定义、完全改写的**（优先级高于 `__getattr__`）：`recv_requests`、`process_input_requests`、`run_batch`、`stream_output`、`send_to_tokenizer`。
2. **omni 定义、对上游做轻量包装的**：`get_new_batch_prefill`（加了一个 prefill 合并闸门）。
3. **完全委托给上游 SGLang 的**（经 `__getattr__` 动态绑定）：`get_next_batch_to_run`、`process_batch_result`、`init_metrics`、`flush_cache` 等一大批。

委托机制的关键代码：

```python
def __getattr__(self, name: str):
    # ...（grammar_queue / grammar_backend 的特例略）
    try:
        attr = getattr(_Upstream, name)          # 在上游 SGLang Scheduler 类上查
    except AttributeError:
        raise AttributeError(...)
    if callable(attr):
        return types.MethodType(attr, self)      # 绑定到当前 omni 实例
    return attr
```

绑定的效果：当 omni 调用 `self.get_next_batch_to_run()` 时，它内部又会调 `self.get_new_batch_prefill()`——这个调用同样经 `__getattr__` 落到 omni **自己改写过的** `get_new_batch_prefill` 上。于是「上游调度框架」与「omni 的定制点」在调用链里自然交织，而无需继承。

#### 4.1.3 源码精读

`OmniScheduler` 的类文档串把这一策略说得很清楚，标注了被改写的方法清单：

[sglang_omni/scheduling/omni_scheduler.py:85-98](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L85-L98) —— 类定义与组合策略说明：上游调度方法经 `__getattr__` 查上游类并绑定到本实例，被改写的方法（`recv_requests` / `process_input_requests` / `run_batch` / `send_to_tokenizer`）直接定义在本类、优先级更高。

[sglang_omni/scheduling/omni_scheduler.py:442-466](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L442-L466) —— `__getattr__` 实现：去 `_Upstream`（即上游 `Scheduler`）上找属性，callable 则用 `types.MethodType(attr, self)` 绑定。

为了让上游方法不爆炸，omni 在 `__init__` 里手动设置了上游 `Scheduler.__init__` 本会设置的**大量兼容字段**——把用不到的子系统设为桩：

[sglang_omni/scheduling/omni_scheduler.py:348-358](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L348-L358) —— 把 `watchdog`、`grammar_manager`（`_NoOpGrammarManager`）等设为空桩，speculative decoding、disaggregation 等特性全部关闭。

三个最典型的「omni 改写以接管输出/传输」的桩也在这里：

[sglang_omni/scheduling/omni_scheduler.py:387](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L387) —— `self.send_to_detokenizer = _NoOpSender()`：上游 `process_batch_result` 完成后默认会把结果发给 detokenizer，omni 用空发送器让它静默，改由自己的 `outbox` 路由。

[sglang_omni/scheduling/omni_scheduler.py:1065-1067](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L1065-L1067) —— `send_to_tokenizer` 改成 no-op：结果走 stage outbox，不送 tokenizer。

> 这三处（`send_to_detokenizer` 桩、`stream_output` 改写、`send_to_tokenizer` no-op）就是 omni「保留自己的传输与输出行为」的具体落点——上游算完，omni 不让上游把结果按 SGLang 的管线吐出去，而是拦下来走自己的 `OutgoingMessage`。

#### 4.1.4 代码实践

**实践目标**：亲手验证「组合而非继承」——确认一个 omni 没定义的方法确实来自上游类，且被绑定到了 omni 实例。

**操作步骤**：

1. 在 `omni_scheduler.py` 中定位被改写的四个方法（`recv_requests`、`process_input_requests`、`run_batch`、`stream_output`）的 `def` 行，确认它们都直接定义在 `OmniScheduler` 类体内。
2. 在同文件搜索 `get_next_batch_to_run`——确认它**没有** `def`，证明它来自 `__getattr__` 委托。
3. 阅读单元测试 `tests/unit_test/serve/test_sglang_bootstrap.py`，理解 omni 如何在不依赖真实 GPU/模型的情况下验证基础设施构建（`monkeypatch` 替换 `create_sglang_infrastructure`）。

**需要观察的现象**：

- `get_next_batch_to_run` 在本文件中**只被调用、从未被定义**，它的定义在上游 `sglang.srt.managers.scheduler.Scheduler`（即文件顶部导入的 `_Upstream`）。
- 被 `monkeypatch` 的测试能在没有 GPU 的 CI 上通过，说明 omni 把「需要 GPU 的基础设施构建」与「调度器逻辑」切得很干净。

**预期结果**：你能用一句话区分「omni 改写的方法」与「omni 委托的方法」的判据——**类体里有 `def` 就是改写，没有就是委托**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `OmniScheduler` 不直接 `class OmniScheduler(SGLangScheduler)`？

**参考答案**：继承会强制走上游 `Scheduler.__init__`，连带初始化 tokenizer/detokenizer/grammar/metrics 等 omni 不需要的子系统，且把属性初始化顺序与上游深度耦合。组合（`__getattr__` 委托）让 omni 只借上游的调度算法（batch 选择、结果处理、内存检查），完全自管状态与生命周期，并把用不到的子系统设为空桩。

**练习 2**：当上游的 `get_next_batch_to_run` 内部调用 `self.get_new_batch_prefill()` 时，实际执行的是上游版本还是 omni 版本？为什么？

**参考答案**：执行的是 **omni 版本**。因为 `get_new_batch_prefill` 直接定义在 `OmniScheduler` 类体里（[omni_scheduler.py:837](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L837)），它先于 `__getattr__` 生效（普通属性查找优先于 `__getattr__`），所以上游框架调用它时命中的是 omni 的改写版。这正是「上游框架」与「omni 定制点」在调用链里交织的方式。

---

### 4.2 `create_sglang_infrastructure`：复用的 SGLang 基础设施元组

#### 4.2.1 概念说明

OmniScheduler 要复用 SGLang 的 AR 调度，就需要 SGLang 那一套「基础设施」已经就位：加载好的模型 worker、KV 缓存池、tree cache、prefill 管理器、decode 管理器。但 omni 不想让每个模型家族都重复写这套样板代码，于是抽出一个**通用引导函数** `create_sglang_infrastructure`，它一次性把这些东西建好，打包成一个**七元组**返回。

这个七元组就是 omni 向 SGLang 「借来的全部家当」：

| 元组位置 | 名字 | 作用 |
|---------|------|------|
| 0 | `model_worker` | 加载了权重的 SGLang `ModelWorker`（含 `model_runner`、`tp_group`、设备信息） |
| 1 | `tree_cache` | RadixCache / ChunkCache，做前缀匹配与 KV 复用 |
| 2 | `req_to_token_pool` | 「请求 → token 序列在 KV 池里的位置」映射池 |
| 3 | `token_to_kv_pool_allocator` | KV 池分配器，分配/释放每个 token 的 KV 槽位 |
| 4 | `prefill_mgr` | `PrefillManager`，管理 prefill 的 waiting 队列与分块 |
| 5 | `decode_mgr` | `DecodeManager`，管理 decode 批次与 retract |
| 6 | `model_config` | 模型配置（vocab size、context length 等） |

注意：这个函数**只建基础设施，不建 `OmniScheduler`**。Scheduler 是模型家族随后用这些零件自己拼出来的——这给 thinker、talker、TTS 引擎留下了各自定制 model_runner 与 adapter 的空间。

#### 4.2.2 核心流程

引导流程内部是严格的依赖顺序：

```
ModelWorker(server_args, gpu_id, tp_rank)          # 加载权重、建 tp_group
   │
   ├─ get_memory_pool()                            # 分配 req_to_token_pool + kv 分配器
   │
   ├─ create_tree_cache(...)                       # RadixCache 或 ChunkCache
   │
   ├─ PrefillManager(page_size, chunked_prefill_size, tree_cache, ...)
   │
   └─ DecodeManager(server_args, kv_allocator,
                    on_retract=lambda req: prefill_mgr.add_one_request(req))  # 关键回调
   │
   └─► 返回七元组
```

这里有个精妙的**闭环**：`DecodeManager` 在 KV 池满、需要 retract 一个请求时，会调 `on_retract` 回调，而该回调被设成 `prefill_mgr.add_one_request(req)`——也就是说，**被 decode 踢出去的请求会自动回到 prefill 队列等下一次重新 prefill**。prefill 与 decode 不是两个孤立的阶段，而是被这个回调缝成一个会自我修复的循环。

引导函数还有一个变体 `create_sglang_infrastructure_defer_cuda_graph`。它的作用是：在构建基础设施期间**临时关闭 CUDA graph 捕获**，建完再恢复。原因写在一段很长的注释里：omni 的某些生成阶段在引导时还不能立刻捕获 CUDA graph——speech tokenizer、sampler 缓冲、阶段本地 decode 助手都还没就位，此时捕获会把重放冻结在一个**不完整的 decode 路径**上。

#### 4.2.3 源码精读

[sglang_omni/scheduling/bootstrap.py:9-19](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/bootstrap.py#L9-L19) —— `create_sglang_infrastructure` 签名：通用引导，按需传入 `tp_rank`、`nccl_port`、`model_arch_override`、`weight_prefix`、`capture_hidden_layers`、`total_gpu_memory_fraction`。

[sglang_omni/scheduling/bootstrap.py:48-74](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/bootstrap.py#L48-L74) —— 建池、建 tree_cache、建 `PrefillManager`、建 `DecodeManager`（`on_retract=lambda req: prefill_mgr.add_one_request(req)` 是 prefill↔decode 闭环的关键一行）。

[sglang_omni/scheduling/bootstrap.py:76-84](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/bootstrap.py#L76-L84) —— 返回的七元组：`(model_worker, tree_cache, req_to_token_pool, token_to_kv_pool_allocator, prefill_mgr, decode_mgr, model_config)`。

[sglang_omni/scheduling/bootstrap.py:87-104](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/bootstrap.py#L87-L104) —— 注释解释为何要 defer CUDA graph：引导时阶段专属 decode 状态尚未就绪，过早捕获会冻结在不完整路径上。

[sglang_omni/scheduling/bootstrap.py:105-123](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/bootstrap.py#L105-L123) —— `create_sglang_infrastructure_defer_cuda_graph`：临时置 `disable_cuda_graph=True`，建完在 `finally` 里恢复，并返回 `(want_cuda_graph, infrastructure)` 告知调用方是否需要在阶段专属 setup 后再 `init_device_graphs()`。

来看一个真实模型如何消费这个七元组——Qwen3-Omni 的 thinker 调度器：

[sglang_omni/models/qwen3_omni/bootstrap.py:41-57](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/models/qwen3_omni/bootstrap.py#L41-L57) —— thinker 解包七元组，并传入 `capture_hidden_layers=[0, 24]`（语音需要捕获中间层 hidden state）、`model_arch_override="Qwen3OmniThinkerForCausalLM"`。

#### 4.2.4 代码实践

**实践目标**：理解「引导函数只建基础设施、不建 scheduler」的边界，以及 defer CUDA graph 的可测性。

**操作步骤**：

1. 阅读 `tests/unit_test/serve/test_sglang_bootstrap.py:11-36`，看它如何用 `monkeypatch` 把 `create_sglang_infrastructure` 换成一个返回固定元组的假函数。
2. 跟踪 `create_sglang_infrastructure_defer_cuda_graph` 在该测试里的调用：断言 `want_cuda_graph is True`、构建期间 `disable_cuda_graph` 被临时设为 `True`、构建后恢复为 `False`。

**需要观察的现象**：

- 测试断言 `seen == [True]`——证明构建期间确实被临时关闭了 CUDA graph。
- 测试断言 `server_args.disable_cuda_graph is False`——证明 `finally` 恢复了用户原始设置。

**预期结果**：你能解释「defer 变体 = 临时关闭 + finally 恢复 + 返回是否需要后续捕获」这三件事，并且明白为什么这个逻辑能在没有 GPU 的 CI 上测（因为基础设施构建本身被 mock 掉了）。

> 若要在本地真正运行该测试，执行：`pytest tests/unit_test/serve/test_sglang_bootstrap.py -v`（需要已安装 sglang 等依赖；若环境不全则标注「待本地验证」）。

#### 4.2.5 小练习与答案

**练习 1**：七元组里的 `decode_mgr` 为什么要拿一个 `on_retract` 回调，且回调指向 `prefill_mgr.add_one_request`？

**参考答案**：decode 阶段当 KV 池快满时需要 retract（回退）部分请求以腾出显存。被回退的请求**不能直接丢弃**——它的部分生成已经作废，需要回到 prefill 队列重新处理（前缀已被 tree cache 缓存，重 prefill 成本可控）。把 `on_retract` 接到 `prefill_mgr.add_one_request` 就让 retract 自动驱动重新 prefill，把 prefill 与 decode 缝成一个自修复循环。

**练习 2**：为什么 Qwen3-Omni thinker 用的是 `create_sglang_infrastructure`（非 defer 版）自己手动 defer，而不是直接用 `create_sglang_infrastructure_defer_cuda_graph`？

**参考答案**：thinker 在 `speech_enabled` 时需要捕获中间层 hidden state（`capture_hidden_layers=[0,24]`）以驱动 talker/语音。它在调用前手动设 `enable_return_hidden_states=True` 且 `disable_cuda_graph=True`（见 [bootstrap.py:37-39](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/models/qwen3_omni/bootstrap.py#L37-L39)），调完再恢复并 `init_device_graphs()`——它需要同时配置 hidden state 捕获，逻辑比纯 defer 更定制化，所以直接用基础函数自行编排。

---

### 4.3 prefill/decode 管理与 KV 缓存：把「选哪个 batch」交给 SGLang

#### 4.3.1 概念说明

这是本讲最关键的一节，直接对应实践任务。`OmniScheduler` 的主循环每一轮都要决定「**下一个跑哪个 batch**」——这件事它**完全交给 SGLang**，具体就是上游方法 `get_next_batch_to_run`。

`get_next_batch_to_run` 内部会做两件事之一：

- **有 waiting 请求且能塞下** → 调 `get_new_batch_prefill()`，得到一个 prefill batch（含分块、前缀匹配）。
- **否则** → 继续 decode 当前 `running_batch`。

而 omni 这边，`PrefillManager` 与 `DecodeManager` 这两个类是 omni **自己写的薄封装**（在 `sglang_backend/` 下），但它们内部大量调用 SGLang 的 `PrefillAdder`、`ScheduleBatch` 等工具来做真正的内存预算与分块决策。所以「选 batch」是 omni 编排 + SGLang 算法的混合体。

KV 缓存则完全由 SGLang 的两个池子承担：

- `req_to_token_pool`：记录每个请求的每个 token 落在 KV 池的哪个槽。
- `token_to_kv_pool_allocator`：分配/释放槽位。
- `tree_cache`：RadixCache 做前缀复用，ChunkCache 则不做前缀匹配。

KV 容量有一个简单的不等式约束，omni 在请求准入时就用它**预拒**过长请求：

\[
\text{required\_tokens} = \text{input\_len} + \text{max\_new\_tokens} \le \text{kv\_capacity}
\]

若不满足，请求在进 waiting 队列前就被拒并 abort，避免跑到一半 OOM。

#### 4.3.2 核心流程

`OmniScheduler` 有**三种事件循环**，在 `start()` 里按配置选一种：

```
start()
 ├─ enable_async_decode? → _event_loop_async_decode()   # 一步预瞄 decode
 ├─ enable_overlap?      → _event_loop_overlap()          # prefill/decode 重叠
 └─ 否则                 → _event_loop_normal()           # 最朴素
```

以最朴素的 `_event_loop_normal` 为例，每轮做：

```
_process_admin_requests()              # 处理 RL 权重热更新等 admin 指令
recv_reqs = recv_requests()            # 排空 inbox（TP 下 rank0 广播给 follower）
recv_reqs += _take_deferred_request_payloads()
process_input_requests(recv_reqs)      # ★ omni 改写：StagePayload → SGLang Req 入队
if engine_paused: sleep; continue      # RL 暂停期空转

batch = get_next_batch_to_run()        # ★★ 委托 SGLang：选 prefill 还是 decode
if batch:
    result = run_batch(batch)          # ★ omni 改写：交给 omni 的 model_runner
    process_batch_result(batch, result)# 委托 SGLang：更新 req 状态、判定 finish、stream_output
else:
    self_check_during_idle(); sleep    # 让出 GIL，不饿死共进程的非 AR 阶段
```

注意 `time.sleep(0.001)` 不是随手写的——它**让出 GIL**，避免单进程模式下 busy 的 AR 循环独占 GIL 把同进程的 audio_encoder 等非 AR 阶段饿死（注释里提到会把音频 QPS 从 >10 拖到 <0.5）。

`run_batch` 是 omni 的核心改写点，它把「上游期待的 `GenerationBatchResult`」与「omni 自己的 model_runner 输出」桥接起来：

```
_run_batch(batch):
    sched_output = _build_sched_output(batch)    # ScheduleBatch → SchedulerOutput(omni)
    mr_output    = model_runner.execute(sched_output)  # ★ omni 自己的 runner 算
    _emit_stream_output(sched_output, mr_output) # 流式 chunk 推 outbox
    return _make_batch_result(batch, mr_output)  # 包装成上游能消费的 GenerationBatchResult
```

#### 4.3.3 源码精读

[sglang_omni/scheduling/omni_scheduler.py:858-886](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L858-L886) —— `run_batch` / `_run_batch`：有自定义 `model_runner` 时走 omni 路径（构造 `SchedulerOutput` → `execute` → 包装 `GenerationBatchResult`），否则回退到上游 `_Upstream.run_batch`。

[sglang_omni/scheduling/omni_scheduler.py:888-897](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L888-L897) —— `_build_sched_output`：把上游 `ScheduleBatch` 里的每个 req 包成 `SchedulerRequest(request_id=req.rid, data=req._omni_data)`，`_omni_data` 就是 omni 自己的请求对象（`SGLangARRequestData`），挂在 SGLang 的 `Req` 上随批次流动。

[sglang_omni/scheduling/omni_scheduler.py:926-939](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L926-L939) —— `_make_batch_result`：把 model_runner 已写入的 `batch.output_ids` 包成上游 `GenerationBatchResult`，让上游 `process_batch_result` 能消费——这就是 omni 输出与上游处理的桥。

[sglang_omni/scheduling/omni_scheduler.py:1655-1685](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L1655-L1685) —— `_event_loop_normal`：完整朴素主循环。注意 `get_next_batch_to_run()`（委托 SGLang 选 batch）与 `run_batch`（omni 改写）与 `process_batch_result`（委托 SGLang）三者交替。

[sglang_omni/scheduling/sglang_backend/prefill.py:49-147](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/sglang_backend/prefill.py#L49-L147) —— `PrefillManager.schedule_next_batch`：维护 prefill waiting 队列，用 SGLang 的 `PrefillAdder` 做分块与前缀匹配预算，产出 `ScheduleBatch.prepare_for_extend()`。

[sglang_omni/scheduling/sglang_backend/decode.py:27-83](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/sglang_backend/decode.py#L27-L83) —— `DecodeManager.schedule_next_batch`：检查 `check_decode_mem()`，KV 满则 `retract_decode`，随后 `prepare_for_decode()`。

[sglang_omni/scheduling/sglang_backend/cache.py:9-33](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/sglang_backend/cache.py#L9-L33) —— `create_tree_cache`：按 `disable_radix_cache` 选 ChunkCache（无前缀匹配）或 RadixCache。

[sglang_omni/scheduling/omni_scheduler.py:802-824](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L802-L824) —— `_request_kv_capacity_error`：用 `input_len + max_new_tokens ≤ max_req_len` 预拒过长请求。

abort 时的 KV 释放也是委托 SGLang：

[sglang_omni/scheduling/omni_scheduler.py:1650-1653](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L1650-L1653) —— `_release_request_kv_cache` 调上游 `release_kv_cache(req, self.tree_cache)`。

#### 4.3.4 代码实践

**实践目标**（对应讲义规格里的核心实践）：在 `omni_scheduler.py` 中定位 `run_batch` 与 prefill/decode 调度，说明它把哪些工作交给 SGLang、哪些仍由 omni 自己负责。

**操作步骤**：

1. 打开 `omni_scheduler.py`，找到 `_event_loop_normal`（约 1655 行）。
2. 在该循环里标注三类调用：
   - **委托 SGLang**：`get_next_batch_to_run()`（选 batch）、`process_batch_result(...)`（更新 req 状态/判完成）。
   - **omni 改写**：`recv_requests()`、`process_input_requests()`、`run_batch()`。
3. 进入 `_run_batch`（约 865 行），标注 omni 自留的工作：`_build_sched_output`（构造 omni 的 `SchedulerOutput`）、`model_runner.execute`（omni 自己的 runner，如 `ThinkerModelRunner`）、`_emit_stream_output`（流式 chunk 推 outbox）。
4. 进入 `sglang_backend/prefill.py` 与 `decode.py`，确认 `PrefillManager`/`DecodeManager` 内部真正做内存预算的是 SGLang 的 `PrefillAdder`、`ScheduleBatch.retract_decode` 等工具。

**需要观察的现象**：你会得到一张清晰的「分工表」。

**预期结果**：能填出下表（答案见下方）。

| 工作 | 谁负责 | 代码位置 |
|------|--------|---------|
| 选下一个 batch（prefill 还是 decode） | **SGLang**（`get_next_batch_to_run`，经 `__getattr__` 委托） | 主循环 1672 行 |
| prefill 分块与前缀匹配 | SGLang 算法（`PrefillAdder`）+ omni 编排（`PrefillManager`） | prefill.py 78-108 |
| KV 满时 retract 请求 | **SGLang**（`ScheduleBatch.retract_decode`）经 omni 的 `DecodeManager` 调用 | decode.py 41-43 |
| 真正跑模型前向 | **omni**（`model_runner.execute`，如 ThinkerModelRunner） | omni_scheduler.py 882 |
| 把结果转成上游可消费的格式 | **omni**（`_make_batch_result`） | omni_scheduler.py 927 |
| 更新 req 状态、判定 finish | **SGLang**（`process_batch_result`） | 主循环 1678 |
| 把完成结果路由到下游 stage | **omni**（`stream_output` 改写 → `outbox`） | omni_scheduler.py 1001 |
| abort 时释放 KV | **SGLang**（`release_kv_cache`）经 omni 调用 | omni_scheduler.py 1653 |

> 一句话总结：**SGLang 管「选 batch、算内存、更新状态、管 KV」；omni 管「跑哪个 runner、请求对象长什么样、结果怎么往外吐」。**

#### 4.3.5 小练习与答案

**练习 1**：`_event_loop_normal` 里的 `time.sleep(0.001)` 仅仅是为了省 CPU 吗？

**参考答案**：不是。它的首要作用是**让出 GIL**，让同进程（colocated）下跑在兄弟线程里的非 AR 阶段（如 audio_encoder）能拿到 Python 执行权。注释指出，若 AR 循环 busy-pin 住 GIL，audio_encoder 的前向（大量小 CUDA kernel 的 Python 派发）会慢约 600 倍，音频 QPS 从 >10 跌到 <0.5。

**练习 2**：为什么 omni 要在请求**进 waiting 队列之前**就用 `_request_kv_capacity_error` 拒掉过长请求，而不是等跑到一半再 OOM？

**参考答案**：跑到一半 OOM 会让请求已占用并污染的 KV 槽难以干净回收，且会打断整个 batch。提前用 `input_len + max_new_tokens ≤ kv_capacity` 校验，能在调度前就以确定性方式 abort 并回传清晰错误（还会提示调高 `--thinker-mem-fraction-static`），代价低、行为可预期。

---

### 4.4 server args：把 omni 的意图翻译成 SGLang 的 `ServerArgs`

#### 4.4.1 概念说明

SGLang 的一切行为（KV 池大小、chunked prefill 大小、最大并发请求数、是否启用 radix cache……）都由一个 `ServerArgs` 对象驱动。omni 不能直接把这些参数散落在各处，而是用一个集中函数 `build_sglang_server_args`，把**所有 AR 引擎共享的默认值**固化下来，再允许模型家族用 `**overrides` 覆写。

这是 omni 与 SGLang 之间**最薄的一层翻译**：omni 的运行时意图（model_path、context_length、显存比例、并发上限）→ SGLang 的 `ServerArgs`。

需要特别理解两点：

- **`mem_fraction_static` 的处理很谨慎**：它可以为 `None`（让 SGLang 自动选），但若给了值，`apply_encoder_mem_reserve` 会从中**减去**外部编码器（如 Qwen 的 audio/image encoder）需要的显存余量，且减完不得低于安全地板 0.1，否则报错。
- **omni 的「请求对象」并不靠 `ServerArgs` 传递**，而是在构造 `OmniScheduler` 时通过 `request_builder`、`result_adapter`、`stream_output_builder` 等**回调**注入。这才是 omni「保留自己的请求对象与流式行为」的真正入口。

#### 4.4.2 核心流程

TTS 引擎的通用模板 `TtsEngineBuilder.build()` 把整个组装过程串起来：

```
build(model_path):
    server_args = build_sglang_server_args(checkpoint_dir, context_length, **overrides)
    customize_server_args(server_args)                          # 模型家族微调
    want_cuda_graph, (七元组) = create_sglang_infrastructure_defer_cuda_graph(server_args, gpu_id, ...)
    setup_model(...); compile_model(...); init_device_graphs()  # 阶段专属 setup
    model_runner   = make_model_runner(model_worker, output_proc)
    request_builder, result_adapter = make_adapters(model)
    scheduler = make_scheduler(七元组 + model_runner + adapters)   # ← 构造 OmniScheduler
    return scheduler
```

`make_scheduler` 里能看到 omni 把所有「自留」回调一次性塞进 `OmniScheduler`：

```python
OmniScheduler(
    tp_worker=model_worker, tree_cache=..., req_to_token_pool=...,
    token_to_kv_pool_allocator=..., server_args=server_args, model_config=...,
    prefill_manager=..., decode_manager=..., model_runner=model_runner,   # omni 的 runner
    request_builder=request_builder, result_adapter=result_adapter,        # omni 的请求/结果适配
    abort_callback=self.make_abort_callback(),
    **self.extra_scheduler_kwargs(),
)
```

#### 4.4.3 源码精读

[sglang_omni/scheduling/sglang_backend/server_args_builder.py:10-37](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/sglang_backend/server_args_builder.py#L10-L37) —— `build_sglang_server_args`：固化共享默认值（`trust_remote_code=True`、`tp_size=1`、`pp_size=1`、`random_seed=123`、`max_running_requests=16`、`max_prefill_tokens=16384`），允许 `**overrides` 覆写；若 `mem_fraction_static` 仍为 `None` 则剔除该键，交给 SGLang 自动决定。

[sglang_omni/scheduling/sglang_backend/server_args_builder.py:40-62](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/sglang_backend/server_args_builder.py#L40-L62) —— `apply_encoder_mem_reserve`：从自动选定的 `mem_fraction_static` 中减去外部编码器余量，若结果 < 0.1 则报错（保护地板）。

[sglang_omni/scheduling/engine_factory.py:52-74](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/engine_factory.py#L52-L74) —— `TtsEngineBuilder.build` 用 `build_sglang_server_args` 造 args，再调 defer 版引导拿七元组。

[sglang_omni/scheduling/engine_factory.py:180-211](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/engine_factory.py#L180-L211) —— `make_scheduler`：把七元组 + `model_runner` + `request_builder` + `result_adapter` + `abort_callback` 一起注入 `OmniScheduler`——这就是「omni 保留自己的请求对象与流式行为」的注入点。

最后看一个具体模型的注入：Qwen3-Omni talker 把 codec 词表、各种 codec token id、说话人映射等全部经 `make_talker_scheduler_adapters` 做成 `request_builder` / `result_adapter` / `stream_chunk_handler` / `stream_done_handler`：

[sglang_omni/models/qwen3_omni/bootstrap.py:187-235](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/models/qwen3_omni/bootstrap.py#L187-L235) —— talker 的 adapter 注入：模型专属的 codec/语音语义全部封装在 adapter 回调里，`OmniScheduler` 本身对 codec 一无所知，从而保持调度器的模型无关性。

#### 4.4.4 代码实践

**实践目标**：验证「server args 只翻译共享默认；模型专属语义走 adapter 回调」这条边界。

**操作步骤**：

1. 阅读 `build_sglang_server_args`，列出它固化的全部共享默认字段。
2. 在 `engine_factory.make_scheduler` 里数一下 `OmniScheduler` 接收的「回调型」参数有几个（`request_builder` / `result_adapter` / `stream_output_builder` / `stream_chunk_handler` / `stream_done_handler` / `abort_callback`）。
3. 对照 Qwen3-Omni talker 的 `make_talker_scheduler_adapters` 调用，确认 codec 词表、`codec_eos_id` 等模型专属信息**只**出现在 adapter 里，**不**出现在 `ServerArgs` 里。

**需要观察的现象**：`ServerArgs` 里没有任何 codec/语音专属字段；这些全在 adapter 回调里。

**预期结果**：你能用一句话说清——**`ServerArgs` 描述「这个 AR 引擎怎么跑」（显存、并发、分块），adapter 回调描述「这个模型的请求/结果怎么编码」（token id、词表、hidden state）。两者正交。**

#### 4.4.5 小练习与答案

**练习 1**：`build_sglang_server_args` 默认 `tp_size=1`、`pp_size=1`，但 omni 明明支持张量并行（u6-l6）。这是否矛盾？

**参考答案**：不矛盾。这里的 `tp_size=1` 是构造 `ServerArgs` 时的初始默认；真实 TP 配置由模型家族在 `customize_server_args` 或后续 override 中改写（真实运行时的 `server_args.tp_size` 会被 placement/topology 注入覆盖）。`OmniScheduler.__init__` 读取的是最终的 `server_args.tp_size`（见 [omni_scheduler.py:180](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L180)），与 builder 的初始默认无关。

**练习 2**：若想让一个 TTS 模型把更多显存留给外部 vocoder/encoder，应该改 `ServerArgs` 的哪个字段、用什么辅助函数？

**参考答案**：调低 `mem_fraction_static`（或保持 `None` 让 SGLang 自选后），用 `apply_encoder_mem_reserve(server_args, encoder_mem_reserve)` 从中减去外部编码器余量。注意减后不得低于安全地板 0.1，否则函数会抛 `ValueError`。

---

### 4.5 drop-stale overrun：async decode 快路径下的 extend/mixed batch 切片

#### 4.5.1 概念说明

4.3 讲的 KV 释放走的是 **abort 路径**（请求被显式中止）。本节讲另一条 KV 释放 + 批次收缩路径——它发生在 **async decode 的「一步预瞄（lookahead）」快路径**里，用来丢弃「已经成为 stale」的请求。这条路径在 commit #1027 刚修正过对 extend/mixed batch 的处理，是 omni「自管 per-token 字段、不依赖 SGLang 的 `filter_batch`」最典型的一处落点。

先建立两个直觉：

- **async decode lookahead**：当 batch 够大且都是 decode 时，OmniScheduler 走 `_event_loop_async_decode`。每一轮先 **LAUNCH** 当前 decode 步（GPU 前向 + 片上采样，不等 GPU），再 **RESOLVE** 上一步的主机侧收集——于是总有「一步在飞」（`_async_pending`）。
- **fast path（低并发 decode / prefill / 空 batch）**：不走 lookahead，而是先把在飞的那一步 flush 掉（`_resolve_pending_async`），再同步跑当前 batch。

**stale overrun 问题**就出在这个 flush 上。当前 `batch` 是在循环顶部 `get_next_batch_to_run()` 时建的——它**早于**这次 flush。而 flush（resolve 上一步）可能让某些 req **完成**（`finished()`）或被 **retract**（KV 已释放、回到 waiting）。这些 req 仍然留在当前 `batch` 里。如果直接拿这个 batch 去 `run_batch` + `process_batch_result`，就会把这些 req **再前向、再终结一遍**，对已经释放的 KV 造成 **double-free**（上游 `pop_committed_kv_cache` 会断言 `"Committed KV cache already freed"`）。这就是 talker 在 async-ON、bs≥2 时的崩溃根因——talker 不标早期（sampler）完成，每个完成都只能在 resolve 半步才检测到。

所以需要在 `run_batch` 之前，把这些 finished/retracted 的 req 从 batch 里**剔除**——这就是 `_drop_stale_overrun` 的职责。

**extend/mixed batch 的特殊难点**：剔除动作对 decode batch 很简单，但对 extend/mixed batch 不简单。区别在于 per-token 张量的布局：

| batch 类型 | 每个 req 拥有的 token 数 | `input_ids` / `out_cache_loc` 布局 |
|-----------|------------------------|--------------------------------------|
| decode | 恰好 1 | 长度 = 请求数 N，**按 row 一一对齐**（row i ↔ token i） |
| extend / mixed | `extend_lens[i]` 个 | 各 req 的 token 段**拼接**成一维，长度 = \(\sum_i \text{extend\_lens}[i]\) |

SGLang 的 `ScheduleBatch.filter_batch(keep_indices)` 只懂 **per-row** 布局——它重建 `reqs` 列表，对按 row 排列的张量做行切片。它**不认识** extend batch 的扁平 per-token 拼接布局。于是对一个 extend batch 调 `filter_batch` 只会改 `reqs`，却把 `input_ids` / `out_cache_loc` 这些 per-token 张量**留在原样**——它们仍带着被丢弃 req 的 token、长度也对不上新的 req 数。下一步前向就会读到**错位的 token 与 KV 槽**，轻则结果错乱、重则越界崩溃。

这正是 omni「保留自己的 per-token 字段维护职责」的体现：SGLang 的 `filter_batch` 不处理这些字段，omni 必须自己按 `extend_lens` 算出 token 偏移并 reslice。

#### 4.5.2 核心流程

fast path 里剔除 stale 的调用序列（伪代码）：

```
# _event_loop_async_decode 的 fast path 分支
if self._async_pending is not None:
    self._resolve_pending_async()           # flush 在飞的步骤（可能 finish/retract 一些 req）
    batch = self._drop_stale_overrun(batch) # ★ 把 stale req 连同其 per-token 字段一并剔除
    self.cur_batch = batch
if batch:
    result = self.run_batch(batch)          # 再同步前向（此时 batch 已不含 stale req）
    ...
```

`_drop_stale_overrun` 内部按 `forward_mode` 分两条路：

```
drop = [r.finished() or r.is_retracted for r in batch.reqs]
keep = [i for i,d in enumerate(drop) if not d]

if forward_mode.is_extend():         # extend/mixed batch：按 token 粒度 reslice
    starts[i] = Σ extend_lens[0..i-1]                         # 每个 req 的 token 段起点
    drop_tokens = ⋃ [starts[i], starts[i]+extend_lens[i])   for dropped i
    keep_tokens = ⋃ 同上                                       for kept i
    _free_overrun_step_slots(out_cache_loc, drop_tokens)      # 释放被丢弃 req 的 KV 槽
    batch.filter_batch(keep_indices=keep)                     # 重建 reqs（只动 row 级）
    batch.input_ids         = input_ids[keep_tokens]          # 手动 reslice per-token 字段
    batch.out_cache_loc     = out_cache_loc[keep_tokens]
    batch.extend_lens       = [extend_lens[i] for i in keep]
    batch.extend_num_tokens = sum(batch.extend_lens)
    batch.prefix_lens       = [prefix_lens[i] for i in keep]
    batch.extend_logprob_start_lens = [ ... for i in keep]
    # extend_input_logprob_token_ids 有自己的布局，单独 reslice（见下）
else:                                # decode batch：每 req 1 token，按 row 切即可
    _free_overrun_step_slots(out_cache_loc, [dropped row indices])
    batch.filter_batch(keep_indices=keep)
    batch.out_cache_loc = out_cache_loc[keep]

if batch.decoding_reqs:              # mixed batch 的 folded-decode req 也要过滤
    batch.decoding_reqs = [r for r in decoding_reqs if id(r) in kept_ids]
```

每个 req 的 token 段起点用前缀和算出：

\[
\text{starts}[i] = \sum_{k=0}^{i-1} \text{extend\_lens}[k], \qquad
\text{req } i \text{ 的 token 落在 } [\text{starts}[i],\ \text{starts}[i]+\text{extend\_lens}[i])
\]

有一个字段布局与众不同，需要**单独的偏移**：`extend_input_logprob_token_ids`。req i 在里面只贡献其后缀 \(\text{extend\_lens}[i] - \text{extend\_logprob\_start\_lens}[i]\) 个 id（只有需要 logprob 的那段），与上面的 token 切片**不对齐**，所以不能用 `keep_tokens`，要按它自己的长度重算起点再切。若剔完之后 `batch.return_logprob` 变 False（剩下的 req 都不要 logprob），则直接置 None。

#### 4.5.3 源码精读

[sglang_omni/scheduling/omni_scheduler.py:1982-1990](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L1982-L1990) —— `_event_loop_async_decode` 的 fast path：flush 在飞步骤后立即调 `batch = self._drop_stale_overrun(batch)`；注释说明「batch 在 drain 之前就建好了，drain 可能 finish/retract 仍留在其中的 req，drop 它们以免二次前向、二次释放 KV」。

[sglang_omni/scheduling/omni_scheduler.py:1829-1913](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L1829-L1913) —— `_drop_stale_overrun` 主体：先算 `drop`（`finished()` 或 `is_retracted`）与 `keep`，再按 `forward_mode.is_extend()` 分两条路。

[sglang_omni/scheduling/omni_scheduler.py:1846-1902](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L1846-L1902) —— extend/mixed 分支：按 `extend_lens` 算 `starts`/`drop_tokens`/`keep_tokens`，`_free_overrun_step_slots(out_cache_loc, drop_tokens)` 释放被丢弃 req 的 KV 槽，`filter_batch(keep)` 只重建 `reqs`，随后**手动 reslice** `input_ids` / `out_cache_loc` / `extend_lens` / `extend_num_tokens` / `prefix_lens` / `extend_logprob_start_lens`。

[sglang_omni/scheduling/omni_scheduler.py:1851-1855](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L1851-L1855) —— 断言 `input_embeds` / `replace_embeds` / `token_type_ids` 必须为 None：它们是「另外的 per-token 字段」，omni 当前不 reslice 它们；万一未来某模型在 extend batch 上填充了它们，当前切片就会漏切，所以宁可断言报错（trip）也不静默错切（misslice）。

[sglang_omni/scheduling/omni_scheduler.py:1884-1902](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L1884-L1902) —— logprob 字段单独 reslice：`extend_input_logprob_token_ids` 按 `extend_lens[i] - extend_logprob_start_lens[i]` 的布局重算 `lp_starts` 再切；若剔完 `return_logprob` 变 False 则置 None。

[sglang_omni/scheduling/omni_scheduler.py:1903-1912](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L1903-L1912) —— decode 分支（每 req 恰好 1 token，per-token 张量与 row 一一对齐）与 `decoding_reqs` 过滤：mixed batch 里 folded-decode 的 req 单独记在 `decoding_reqs`，剔除 req 时按 `id(r)` 同步移除，避免悬挂引用。

[sglang_omni/scheduling/omni_scheduler.py:1807-1827](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L1807-L1827) —— `_free_overrun_step_slots`：释放被丢弃的 KV 槽，**只在 RadixCache + page_size=1 下生效**——ChunkCache/paged 分配器在请求结束时已连同 overrun 槽一起释放，这里再补会 double-free，故用 `page_size != 1 or disable_radix_cache` 做闸门；并断言 drop index 不越界。

[sglang_omni/scheduling/omni_scheduler.py:1753-1792](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L1753-L1792) —— `_resolve_and_process`：lookahead 正路径（非 fast path）里同样的 overrun 处理——resolve 前**快照** finished 状态、把「上一步就已 finish/retract」的 req 计入 `skip_rids`、随后按 keep 收缩。`_drop_stale_overrun` 的注释明说它是这条正路径 drop 的「fast-path 对偶」。

可运行验证：[tests/unit_test/pipeline/test_async_decode.py](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/tests/unit_test/pipeline/test_async_decode.py) 的 `test_drop_stale_overrun_mixed_reslices_per_token`（`lens=[3,1,1]`、`done=[F,T,F]`）断言 `freed==[103]`、`out_cache_loc==[100,101,102,104]`、`input_ids==[0,1,2,4]`、`extend_lens==[3,1]`——精确刻画了「中间 req 被丢后，它那 1 个 token 的槽被释放、其余 token 段被重新拼接」。

#### 4.5.4 代码实践

**实践目标**（对应讲义规格核心实践的后半）：定位 `_drop_stale_overrun` 对 extend/mixed batch 的处理，解释为何 per-token 字段必须手动 reslice 而 `filter_batch` 做不到。

**操作步骤**：

1. 打开 `omni_scheduler.py`，定位 `_drop_stale_overrun`（约 1829 行）。
2. 找到 `if forward_mode is not None and forward_mode.is_extend():` 分支（约 1846 行），对照 4.5.2 的伪代码逐行读：`starts` 前缀和 → `drop_tokens`/`keep_tokens` → `_free_overrun_step_slots` → `filter_batch` → 手动 reslice 各 per-token 字段。
3. 切到 `else`（decode 分支，约 1903 行），对比它为何只需 `out_cache_loc[keep]`、不需要 token 级 reslice。
4. 阅读 `tests/unit_test/pipeline/test_async_decode.py` 里的 `test_drop_stale_overrun_mixed_reslices_per_token` 与 `test_drop_stale_overrun_extend_multitoken_drop`，核对断言里的 `freed` / `out_cache_loc` / `extend_lens` 是否与你手算一致。

**需要观察的现象**：

- extend 分支里，`filter_batch(keep_indices=keep)` **只**用 row 级 `keep` 重建 `reqs`；真正让 per-token 张量重新对齐的是紧随其后的 `input_ids[keep_tokens]` 等手动赋值——这就证明了 `filter_batch` 本身做不到。
- decode 分支不需要 `keep_tokens`，因为每 req 恰好 1 token，row 切片即 token 切片。
- `test_drop_stale_overrun_mixed_reslices_per_token`：丢掉 `lens=[3,1,1]` 的中间 req（`done=[F,T,F]`），被释放的是它那 1 个 token 的槽 `103`，存活 token 段拼成 `[100,101,102,104]`。

**预期结果**：你能用一句话说清判据——**「batch 的 per-token 张量按 row 一对一排列时（decode），`filter_batch` 够用；按 `extend_lens` 拼接成一维时（extend/mixed），`filter_batch` 只动 row、留下错位的 per-token 张量，必须由 omni 按 token 偏移手动 reslice。」**

> 本实践为纯源码阅读型，无需 GPU。若环境齐全可执行：`pytest tests/unit_test/pipeline/test_async_decode.py -k drop_stale -v`（环境不全则标注「待本地验证」）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 decode batch（else 分支）可以直接 `filter_batch(keep) + out_cache_loc[keep]`，而 extend/mixed batch 不行？

**参考答案**：decode batch 每个 req 恰好贡献 1 个 token，per-token 张量（`input_ids`/`out_cache_loc`）长度 = 请求数 N，row i 与 token i 一一对应，所以按 row 切片就等于按 token 切片，`filter_batch` 够用。extend/mixed batch 里 req i 拥有 `extend_lens[i]` 个 token，per-token 张量是各 req token 段拼接而成的一维数组，`filter_batch` 只按 row 重建 `reqs`，会留下仍含被丢弃 req token、且长度对不上的 per-token 张量，下一步前向读到错位 token 与 KV 槽。

**练习 2**：`_free_overrun_step_slots` 为什么用 `page_size != 1 or disable_radix_cache` 做闸门、在这两种情况下直接 return？

**参考答案**：ChunkCache（`disable_radix_cache`）的 `cache_finished_req` 会连同 overrun 槽在内的整段一起释放；paged 分配器（`page_size>1`）按整页释放，overrun 槽所在页已随请求尾部 token 一起释放。这两种情况下若再补释放就会 double-free、让两个请求共享同一槽，故必须跳过。只有 RadixCache + page_size=1 时 overrun 槽才是单独泄漏、需要这里补偿释放。

---

## 5. 综合实践

**任务**：为「OmniScheduler 复用 SGLang」画一张完整的**分工边界图**，并对照真实代码自验。

请完成以下步骤：

1. **画主循环**：以 `_event_loop_normal`（[omni_scheduler.py:1655-1685](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L1655-L1685)）为骨架，画出每一轮的步骤，并用两种颜色标注：① 委托 SGLang 的步骤（`get_next_batch_to_run`、`process_batch_result`）；② omni 自留的步骤（`recv_requests`、`process_input_requests`、`run_batch`）。

2. **标注数据形态转换链**：追踪一次请求的数据如何在两种世界间穿梭——
   - 入向：`StagePayload` →（`request_builder`）→ `SGLangARRequestData`（omni 请求对象）→ 挂到 SGLang `Req._omni_data` → 进入 SGLang `waiting_queue`。
   - 出向：`model_runner.execute` → `ModelRunnerOutput` →（`_make_batch_result`）→ SGLang `GenerationBatchResult` →（`process_batch_result` → `stream_output`）→ omni `result_adapter` → `OutgoingMessage` 进 `outbox`。
   - 参考代码：`_build_sched_output`（888）、`_make_batch_result`（927）、`stream_output`（1001）。

3. **定位 KV 闭环**：在 `bootstrap.py` 找到 `on_retract=lambda req: prefill_mgr.add_one_request(req)`，解释 KV 满时请求如何从 decode 回到 prefill。

4. **自验组合策略**：在 `omni_scheduler.py` 全文搜索，确认 `get_next_batch_to_run`、`process_batch_result`、`init_metrics`、`flush_cache` **没有** `def`（即全部委托），而 `run_batch`、`stream_output`、`send_to_tokenizer`、`recv_requests` **有** `def`（即改写）。

5. **追踪 drop-stale 切片**：打开 `_drop_stale_overrun`（[omni_scheduler.py:1829-1913](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L1829-L1913)），对比 extend 分支与 decode 分支。解释为何 extend/mixed batch 的 `input_ids`/`out_cache_loc` 必须**按 token 偏移**（由 `extend_lens` 前缀和算出）手动 reslice，而 decode 分支只需 `out_cache_loc[keep]`；并用 `test_drop_stale_overrun_mixed_reslices_per_token` 的断言自验。

**验收标准**：你能指着代码对别人说清楚——「SGLang 负责选 batch、管 KV、更新状态；omni 负责跑哪个 runner、请求对象长什么样、结果和流怎么吐给下游。两者靠 `__getattr__` 委托 + adapter 回调 + `_omni_data` 挂载这三条缝拼在一起。」

> 本实践为纯源码阅读型，无需 GPU。若想跑可执行验证，可执行 `pytest tests/unit_test/serve/test_sglang_bootstrap.py -v` 确认 defer CUDA graph 行为；执行 `pytest tests/unit_test/scheduling/test_engine_factory.py -v` 确认引擎工厂能 mock 基础设施后正确组装 scheduler（环境不全则标注「待本地验证」）。

## 6. 本讲小结

- **组合而非继承**：`OmniScheduler` 用 `__getattr__` 把未定义的方法委托给上游 SGLang `Scheduler` 类，并用 `types.MethodType` 绑定到本实例——只借上游的调度算法，不借它的 tokenizer/detokenizer 管线（用 `_NoOpSender`、`_NoOpGrammarManager` 等桩堵住）。
- **公开契约不变**：对 Stage 暴露的 `inbox/outbox/start/stop/abort` 与 `SimpleScheduler` 完全一致，印证「Stage 不因 scheduler 类型而分支」。
- **七元组基础设施**：`create_sglang_infrastructure` 一次性建好 `model_worker / tree_cache / req_to_token_pool / token_to_kv_pool_allocator / prefill_mgr / decode_mgr / model_config`，prefill 与 decode 经 `on_retract` 回调缝成自修复闭环；defer 变体用于把 CUDA graph 捕获推迟到阶段专属 setup 之后。
- **分工边界**：SGLang 管「选 batch（`get_next_batch_to_run`）、算内存与分块、retract、更新 req 状态、管 KV」；omni 管「跑哪个 model_runner、请求对象（`_omni_data`）长什么样、结果与流式 chunk 怎么进 outbox」。
- **server args 是最薄翻译层**：`build_sglang_server_args` 固化共享默认（显存比例、并发、分块），模型专属语义（codec 词表、token id）全部走 `request_builder`/`result_adapter`/`stream_*` 回调注入，两者正交。
- **三种事件循环**：`normal` / `overlap` / `async_decode`，朴素循环用 `time.sleep(0.001)` 让出 GIL 以不饿死共进程的非 AR 阶段。
- **drop-stale overrun 由 omni 自管 per-token 字段**：async decode 快路径在 flush 在飞步骤后，当前 batch 可能仍含已被 finish/retract 的 stale req；剔除它们时，extend/mixed batch 的 `input_ids`/`out_cache_loc` 等是按 `extend_lens` 拼接的 per-token 张量，SGLang 的 `filter_batch` 只懂 per-row、会留下错位，故 omni 必须自行按 token 偏移 reslice（并单独处理 logprob 字段与 `decoding_reqs`）；decode batch 因每 req 恰好 1 token 而无此问题。这是 omni「不依赖 SGLang 处理 per-token 字段」的典型落点。

## 7. 下一步学习建议

- **u4-l3（ModelRunner 与 AR 前向路径）**：本讲把 `model_runner.execute` 当作黑盒——下一讲打开它，看 `ForwardBatch → hooks → forward → 输出处理` 的链路，以及 `ThinkerModelRunner` 在哪一步注入多模态 embedding、`FeedbackStrategy` 如何驱动 talker 的反馈循环。
- **u4-l4（流式调度器与流式 vocoder）**：本讲的 `stream_chunk`/`stream_done` 消息在 AR 阶段只是被缓冲；流式 vocoder 如何按 chunk 累积并在生成完成前就吐音频，是自然延伸。
- **u6-l4（RL 权重热更新与 Admin 控制）**：本讲多次出现 `admin`、`pause`、`update_weights`、`retract`——这些是推理侧 RL 的控制面，建议在掌握本讲后再读 admin 生命周期。
- **延伸阅读**：直接打开上游 `sglang.srt.managers.scheduler.Scheduler`（即本讲的 `_Upstream`），对照 `get_next_batch_to_run` / `process_batch_result` 的真实实现，能让你对「omni 借了什么」有体感。
- **延伸阅读（async decode）**：本讲 4.5 引入了 async decode 的 fast path 与 drop-stale。想完整理解「一步预瞄 + launch/resolve 重叠」可读 `_event_loop_async_decode`（[omni_scheduler.py:1915-2001](https://github.com/sgl-project/sglang-omni/blob/e7979b0719e16290a56f571745af0d4428326f1f/sglang_omni/scheduling/omni_scheduler.py#L1915-L2001)）及其单测 `tests/unit_test/pipeline/test_async_decode.py`；这条路径的两步协议与 stale-batch 回收，是 omni 在 SGLang 之上做的性能与正确性增强。
