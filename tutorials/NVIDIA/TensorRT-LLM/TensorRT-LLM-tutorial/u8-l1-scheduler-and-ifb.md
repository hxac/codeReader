# 调度器与 inflight batching

> 本讲属于「进阶层 · 调度与运行时（u8）」单元的第 1 讲，承接 [u3-l2 PyExecutor 单步循环](u3-l2-pyexecutor-step-loop.md)。
> 在 u3-l2 里，我们把 PyExecutor 的单步循环当作一台「发动机」，其中有一行 `self.scheduler.schedule_request(...)` 被当作黑盒略过。本讲就拆开这个黑盒。

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一句话说清 **inflight batching（in-flight batching，动态批）** 在每一步里到底「动态」在哪里。
2. 复述 **两步调度** 的职责划分：`CapacityScheduler` 管「显存放不放得下」，`MicroBatchScheduler` 管「本步算多少 token」。
3. 看懂 `SimpleScheduler` 如何把两步串起来，并说出它的输入输出契约 `SchedulerOutput`。
4. 区分三种调度器装配分支：C++ 绑定（默认）、纯 Python（`use_python_scheduler`）、V2 合并调度器（`KVCacheManagerV2`）。
5. 仿照官方文档里的 `GuaranteedNoEvictScheduler` 例子，写一个简化版自定义 `CapacityScheduler` 骨架，并知道在哪一行源码把它接进 PyExecutor。

## 2. 前置知识

本讲默认你已经掌握 u3-l2 的以下概念，这里只做最小回顾：

- **PyExecutor 单步循环（step）**：PyExecutor 在后台线程跑一个长驻事件循环，每一圈（一步）把所有活跃请求共同推进一个 token。这一圈就叫一个 **inflight batching 的「步」**。
- **请求三阶段**：一个请求的一生大致是 `CONTEXT_INIT`（算 prompt，算力密集的 prefill）→ `GENERATION_IN_PROGRESS`（逐 token 生成，带宽密集的 decode）→ `GENERATION_COMPLETE`。本讲会反复用到这些状态。
- **ResourceManager 三段式**：`prepare_resources` → `update_resources` → `free_resources`。调度器决定「谁上场」之后，才轮到 ResourceManager「准备场地」。

此外，你需要知道两个调度器都在回答 **资源** 问题，但量纲不同：

| 资源 | 量纲 | 谁负责 |
|------|------|--------|
| KV cache 显存块（block） | 「块数」 | `CapacityScheduler` |
| 本步前向的 token 总量 | 「token 数」 | `MicroBatchScheduler` |

块不够 → 请求放不下；token 超了 → 一次前向塞不下。两者必须分别把关，这正是「两步」的根因。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [docs/source/torch/scheduler.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/scheduler.md) | 官方调度器说明文档，含两步调度的概念定义与自定义示例（**部分链接已过期，见下文**）。 |
| [tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py) | 调度器主体：抽象基类 `CapacityScheduler`/`MicroBatchScheduler`/`RequestScheduler`、C++ 绑定 `BindCapacityScheduler`/`BindMicroBatchScheduler`、组合器 `SimpleScheduler`、纯 Python 实现 `PyCapacityScheduler`/`PyMicroBatchScheduler`、`SimpleUnifiedScheduler`。 |
| [tensorrt_llm/_torch/pyexecutor/scheduler/scheduler_v2.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler_v2.py) | V2 调度器 `KVCacheV2Scheduler`：把容量准入与 token 预算合并到单一循环里，配合 `KVCacheManagerV2` 的 `resize()` 内联分配显存。 |
| [tensorrt_llm/_torch/pyexecutor/py_executor_creator.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor_creator.py) | 执行器装配入口 `create_py_executor()`：读配置、建模型引擎、建 KV cache，最后调 `create_py_executor_instance()` 真正组装执行器（含调度器）。 |
| [tensorrt_llm/_torch/pyexecutor/_util.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/_util.py) | `create_py_executor_instance()` 的真正实现，**调度器实例化的三选一分支就在这里**（L2458–L2527）。文档把它归在 `py_executor_creator.py`，是历史遗留说法。 |
| [tensorrt_llm/_torch/pyexecutor/py_executor.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py) | 单步循环本体，`_schedule()`（L5271）每步调用一次 `scheduler.schedule_request(...)`。 |
| [tensorrt_llm/llmapi/llm_args.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py) | `CapacitySchedulerPolicy`（L3290）与 `SchedulerConfig`（L3347）——用户侧选策略的旋钮。 |

> ⚠️ **一个重要的版本事实**：官方文档 `scheduler.md` 里的链接 `tensorrt_llm/_torch/pyexecutor/scheduler.py`（单数、直接挂在 `pyexecutor/` 下）已经失效——文件早已迁到 `scheduler/scheduler.py` 子包。文档里展示的独立类 `GuaranteedNoEvictScheduler` 也已被重构为 `PyCapacityScheduler` + `GuaranteedNoEvictPolicy`（策略类）。本讲会同时给出「文档演示的概念版」与「真实在跑的实现版」，帮你把两者对上。

## 4. 核心概念与源码讲解

### 4.1 inflight batching 与两步调度的总体框架

#### 4.1.1 概念说明

**inflight batching（也叫 continuous batching / iteration-level batching）** 的核心思想是：**批的组成不是在请求到来时一次定死，而是每一步（每个解码迭代）都重新决定一次**。

对比传统的 static batching：

- **static batching**：凑齐一个固定 batch，所有请求一起 prefill、一起 decode，等最慢的那个生成完才整批结束。长短请求混在一起，GPU 空等严重。
- **inflight batching**：每一步都重新算账——哪个请求这一步要算？哪个已经生成完了可以踢出 batch？哪个新请求可以插进来？短请求生成完立刻让出名额给新请求，GPU 几乎不空转。

所以「动态」就动态在：**batch 的成员列表在每一步都可能变化**。而「每一步重新算账」这件事，就是调度器（scheduler）的职责。

官方文档 [scheduler.md:3-5](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/scheduler.md#L3-L5) 一句话定义：

> TensorRT LLM PyTorch backend employs inflight batching, a mechanism where batching and scheduling occur dynamically at each LLM step. The scheduler is invoked to determine which requests are scheduled at the current step.

#### 4.1.2 核心流程

TensorRT-LLM 把「每步算账」拆成两个串联的子问题，这就是 **两步调度**：

```text
所有活跃请求 active_requests
        │
        ▼
 ┌──────────────────────────────┐
 │ 第 1 步：CapacityScheduler    │  问题：显存放得下哪些？（KV cache 块预算）
 │  输出 fitting_requests        │      + paused_requests（被暂停的）
 └──────────────────────────────┘
        │ fitting_requests
        ▼
 ┌──────────────────────────────┐
 │ 第 2 步：MicroBatchScheduler  │  问题：放得下的里面，本步前向算多少 token？
 │  输出 context_requests        │      （token 预算、微批切分 chunking）
 │       generation_requests     │
 └──────────────────────────────┘
        │
        ▼
 SchedulerOutput → 交给 ModelEngine 做一次前向
```

`SimpleScheduler` 就是把这两步串起来的容器，它的输出打包成 `SchedulerOutput`：

```python
# scheduler.py:56-66
SchedulerOutput = namedtuple(
    "SchedulerOutput",
    [
        "encoder_requests",                  # 编码器阶段请求（多模态/enc-dec）
        "context_requests",                  # 本步要做 prefill 的请求
        "generation_requests",               # 本步要做 decode 的请求
        "paused_requests",                   # 被暂停的请求（MAX_UTILIZATION 才会有）
        "fitting_disagg_gen_init_requests",  # 分离式服务：刚到的 decode-only 请求
        "num_fitting_requests",              # 容量层放行的总数（统计用）
    ],
)
```

> 注意 `SchedulerOutput` 把 context 和 generation 分开返回，而不是混在一起——因为这两类请求执行的是**不同的注意力 kernel**（context 是双向/全长度，generation 是增量单 token），下游 ModelEngine 要分别处理。

#### 4.1.3 源码精读

`SimpleScheduler` 的实现非常薄，正好把「两步」体现得淋漓尽致：

[SimpleScheduler（scheduler.py:422-448）](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L422-L448) 把 `CapacityScheduler` 与 `MicroBatchScheduler` 组合起来：先调容量调度拿到 `fitting_requests`，再把它们喂给微批调度，最终拼成 `SchedulerOutput`。

它只做两件事的串联：

```python
def schedule_request(self, active_requests, inflight_request_ids) -> SchedulerOutput:
    # 第 1 步：容量准入
    fitting_requests, fitting_disagg_gen_init_requests, paused_requests = (
        self.capacity_scheduler.schedule_request(active_requests)
    )
    # 第 2 步：微批 / token 预算
    encoder_requests, context_requests, generation_requests = (
        self.micro_batch_scheduler.schedule(fitting_requests, inflight_request_ids)
    )
    return SchedulerOutput(...)
```

第二个参数 `inflight_request_ids` 是为 **流水线并行（pipeline parallelism, PP）** 预留的：在 C++ 运行时里，跨 PP stage 的 micro-batch 会重叠执行，需要知道哪些请求「已经在飞」。官方文档 [scheduler.md:16-17](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/scheduler.md#L16-L17) 明确指出：**PyTorch 流程不支持 PP，所以这个集合恒为空集**。本讲你可以先把它当空集忽略。

而真正「每步调用一次」的地方在 PyExecutor 主循环里：

[_schedule()（py_executor.py:5271-5313）](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L5271-L5313) 是单步循环里调用调度器的入口。核心就一行（L5277）：

```python
scheduler_output = self.scheduler.schedule_request(
    self.active_requests, self.inflight_req_ids)
```

拿到 `SchedulerOutput` 后，它再把结果整理成 `ScheduledRequests` 对象返回，供主循环的「准备资源 → 前向 → 采样」后续阶段使用。

#### 4.1.4 代码实践

**实践目标**：在日志里亲眼看一次「每步调度」发生，确认调度器确实每步都被调用。

**操作步骤**：

1. 用模块级日志把调度器所在的模块调到 debug，参考 `AGENTS.md` 里的 `TLLM_LOG_LEVEL_BY_MODULE`（例如 `TLLM_LOG_LEVEL_BY_MODULE="debug:_torch"`）。
2. 用 `trtllm-serve` 或 LLM API 跑一个小模型，同时发若干条长短不一的请求。
3. 在 [scheduler.py:1792-1795](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L1792-L1795) 的日志（`[Summary] Capacity scheduler allows N requests, pauses M requests`）以及 [py_executor.py:2599-2602](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2599-L2602) 的 `scheduled ... context requests and ... generation requests` 上观察。

**需要观察的现象**：

- 每个解码迭代都打印一行调度摘要（证明「每步调用」）。
- 随着短请求先完成，`generation_requests` 数量动态下降，新请求作为 `context_requests` 插进来（证明「批成员每步变化」）。

**预期结果**：你能看到 batch 成员列表随时间起伏，而不是固定一批从头跑到尾。

> 待本地验证：精确的日志文案与行号请以你本机版本为准；若无 GPU/模型，可只读 `py_executor.py` 的 `_executor_loop` 系列，确认 `_schedule()` 出现在每个循环体的固定位置即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能把 `CapacityScheduler` 和 `MicroBatchScheduler` 合并成一个调度器？
**参考答案**：两者把关的资源量纲不同——前者是 KV cache 的「块数」（显存容量），后者是「token 数」（单步前向算力预算）。一个请求可能「块放得下」但「token 超了单步预算」（例如一条超长 prompt），也可能反过来。分开两步才能各自独立地做准入与切分决策，尤其是 microbatch 的 **chunking（上下文分块）** 只在 token 维度才有意义。

**练习 2**：`SchedulerOutput` 为什么要把 `context_requests` 和 `generation_requests` 分两个列表返回，而不是合成一个？
**参考答案**：因为这两类请求在下游执行的是不同的注意力 kernel——context 请求要做全长度（可能分块）的 prefill 注意力，generation 请求只处理增量单 token 的 decode 注意力。`ScheduledRequests` 类还据此提供了 `can_run_cuda_graph`、`is_generation_only` 等便捷判断（[scheduler.py:163-191](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L163-L191)），下游据此选择不同的执行路径。

---

### 4.2 CapacityScheduler：显存容量准入

#### 4.2.1 概念说明

`CapacityScheduler` 回答一个纯粹的问题：**给定当前空闲的 KV cache 块，哪些活跃请求这一步可以被放行（fit）？**

它的输入是全部活跃请求，输出是三个列表：

- `fitting_requests`：放行的请求（显存够）。
- `paused_requests`：被暂停的请求（仅 `MAX_UTILIZATION` 策略会产生，见下文）。
- `fitting_disagg_gen_init_requests`：分离式服务场景下的 decode-only 初始化请求（本讲先忽略）。

它**不关心** token 预算，也**不做**微批切分——那是下一步 `MicroBatchScheduler` 的事。它只盯着「块」。

`CapacityScheduler` 是个抽象基类（ABC），只规定了一个抽象方法 `schedule_request`：

[CapacityScheduler ABC（scheduler.py:312-322）](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L312-L322)，签名注释里点明它要和 C++ 头文件 `capacityScheduler.h` 对齐。

#### 4.2.2 核心流程

容量调度有 **三种策略**，由用户配置 `CapacitySchedulerPolicy` 选择（[llm_args.py:3290-3297](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L3290-L3297)）：

| 策略 | 行为 | 会暂停请求吗 |
|------|------|------------|
| `GUARANTEED_NO_EVICT`（默认） | 为每个放行请求**预留**足够跑到完成的块，绝不驱逐 | 否 |
| `MAX_UTILIZATION` | 最大化利用率，**允许暂停（驱逐）已开始的请求**给新请求腾位 | 是 |
| `STATIC_BATCH` | 只在没有活跃请求时才调度（凑静态批） | 否 |

`GUARANTEED_NO_EVICT` 的核心直觉是 **「先保证已经在跑的能跑完，再收新的」**：

1. **优先放行 generation**：已经在 decode 的请求，先预留它跑到 `max_seq_len` 所需的块（用 `get_remaining_blocks_to_completion` 估算）。
2. **剩余块再收 context**：用 `get_needed_resource_to_completion(request)` 算新 prompt 需要多少块，若 `needed_blocks ≤ available_blocks` 才放行，并扣减 `available_blocks`。
3. **一旦放不下就停**：保证「放行的都能完成」，不破坏承诺。

设总块数为 \(B\)，generation 请求预留后剩余块数为：

\[
B_{\text{avail}} = B - \sum_{r \in \text{gen}} \mathrm{needed}(r)
\]

新 context 请求 \(c\) 被放行当且仅当 \(\mathrm{needed}(c) \le B_{\text{avail}}\)，放行后更新 \(B_{\text{avail}} \leftarrow B_{\text{avail}} - \mathrm{needed}(c)\)。

`MAX_UTILIZATION` 更激进：当新请求放不下时，它会**回头暂停一个已开始的请求**（释放其块）来腾位，从而把显存利用率榨满，代价是牺牲一点公平性（被暂停的请求要等下一轮恢复）。

#### 4.2.3 源码精读

真实在跑的实现有三套，分别对应不同装配分支：

**(a) C++ 绑定版 `BindCapacityScheduler`（默认）**

[BindCapacityScheduler（scheduler.py:325-371）](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L325-L371)：Python 只是个薄壳，真正的容量调度逻辑在 C++ `tb_internal.algorithms.CapacityScheduler` 里。构造时把策略、是否有 KV cache manager、`no_schedule_until_state`（默认 `CONTEXT_INIT`，enc-dec 模型会放宽到 `ENCODER_INIT`）等参数传进去；`schedule_request` 直接把请求和各 manager 转发给 C++：

```python
def schedule_request(self, active_requests):
    return self.impl(
        active_requests,
        self.kv_cache_manager,
        self.peft_cache_manager,
        self.cross_kv_cache_manager,
    )
```

这正是「Python 调度、C++ 加速」的体现——接口在 Python，高性能实现在 C++。

**(b) 纯 Python 版 `PyCapacityScheduler`（`use_python_scheduler=True` 时）**

[PyCapacityScheduler（scheduler.py:1514-1811）](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L1514-L1811) 是 C++ 实现的逐行 Python 镜像（类注释明确「Aligned 1:1 with C++ logic」）。它用 **策略模式** 把三种策略拆成三个 policy 类：`MaxRequestsPolicy`、`GuaranteedNoEvictPolicy`、`MaxUtilizationPolicy`，构造时按配置选一个（[`_create_policy`, scheduler.py:1571-1582](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L1571-L1582)）。

[GuaranteedNoEvictPolicy.schedule（scheduler.py:1044-1203）](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L1044-L1203) 就是文档里那个 `GuaranteedNoEvictScheduler` 的「真身」。它比文档示例复杂得多——额外处理了多窗口大小（VSWA）、前缀复用跳过优化、PEFT/LoRA 页预算、enc-dec 双池——但骨架完全一致：第一遍放行 generation 并扣减预留块，第二遍用剩余块尝试收 context。核心入口：

[PyCapacityScheduler.schedule_request（scheduler.py:1774-1797）](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L1774-L1797) 委托给选中的 policy，再把结果分类成 `fitting_requests` 与 `fitting_disagg_gen_init_requests`。

**(c) V2 合并调度器（见 4.3 节）**

`KVCacheManagerV2` 不用单独的 CapacityScheduler，而是把容量准入和 token 预算合到一个循环里——但容量判断的那段逻辑（`try_allocate_generation` / `resize_context` 失败即不放行）精神是一样的。

> **把文档和源码对上**：文档 [scheduler.md:38-89](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/scheduler.md#L38-L89) 给的那个独立 `GuaranteedNoEvictScheduler` 类，是 **C++ 绑定出现之前的纯 Python 旧实现**。现在它已演化为 `PyCapacityScheduler` + `GuaranteedNoEvictPolicy`。两者算法同源，文档示例用来教学「最小可读」版本完全合适。

#### 4.2.4 代码实践

**实践目标**：通过配置切换策略，对比 `GUARANTEED_NO_EVICT` 与 `MAX_UTILIZATION` 在压力下的行为差异。

**操作步骤**：

1. 在 `SchedulerConfig` 里改 `capacity_scheduler_policy`（[llm_args.py:3349-3351](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L3349-L3351)），例如通过 YAML config：
   ```yaml
   scheduler_config:
     capacity_scheduler_policy: MAX_UTILIZATION
   ```
2. 把 KV cache 池调小（`kv_cache_config.free_gpu_memory_fraction` 调低）制造显存压力。
3. 并发发送一批长短差异大的请求。

**需要观察的现象**：

- `GUARANTEED_NO_EVICT`：日志里 `paused_requests` 几乎为 0；新请求可能排队较久才进 batch。
- `MAX_UTILIZATION`：日志里出现 `request ID ... -> pause` / `-> start`（[scheduler.py:1318-1322](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L1318-L1322)），显存利用率更高，但某些请求的端到端延迟波动更大。

**预期结果**：两种策略下吞吐与延迟曲线不同——`MAX_UTILIZATION` 通常吞吐更高，尾部延迟更大。这是典型的「利用率 vs 公平性」取舍。

> 待本地验证：精确数值依赖模型与硬件，请在本机实测。

#### 4.2.5 小练习与答案

**练习 1**：`MAX_UTILIZATION` 会暂停「已开始的请求」。请问它挑选暂停受害者时，为什么不挑 `ENCODER_INIT` 或首个 context chunk 的请求？
**参考答案**：因为这类请求还没分配任何 self-pool 的 KV 块，暂停它们释放不出显存，没有意义。源码里 `is_started_request` 明确要求 `(context_init 且 非首个 chunk) 或 generation_in_progress`（[scheduler.py:1247-1254](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L1247-L1254)），V2 版同样如此（[scheduler_v2.py:979-986](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler_v2.py#L979-L986)）。

**练习 2**：默认策略为什么是 `GUARANTEED_NO_EVICT` 而不是吞吐更高的 `MAX_UTILIZATION`？
**参考答案**：保证不驱逐意味着任何被放行的请求都一定能在不被打断的情况下跑完，行为可预期、延迟稳定，适合大多数服务场景。`MAX_UTILIZATION` 虽吞吐更高，但暂停/恢复会引入延迟抖动，需要运维明确接受这一取舍后才会启用。

---

### 4.3 MicroBatchScheduler：token 预算与上下文分块

#### 4.3.1 概念说明

`CapacityScheduler` 决定「显存放得下谁」，但「放得下」不等于「这一步前向算得完」。一条超长 prompt 可能块够、但 token 数远超单步算力预算 `max_num_tokens`。`MicroBatchScheduler` 解决的就是这第二个问题：**在放行的请求里，本步前向实际算多少 token？**

它的两个关键能力：

1. **token 预算控制**：累计本步要算的 token，一旦超过 `max_num_tokens` 就停止纳入新请求。
2. **上下文分块（context chunking / chunked prefill）**：当一条 prompt 的 token 超过剩余预算时，**不整条算**，而是切成若干块（chunk）跨多步算完。这让一条超长请求能与短请求在同一个 batch 里并存，避免被一条长 prompt 独占 GPU。

文档 [scheduler.md:15-19](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/scheduler.md#L15-L19) 总结了它的输入输出：输入是 `fitting_requests` 与 `inflight_request_ids`，输出是 `context_requests` 与 `generation_requests`。没被选进这两个列表的请求，本步不参与前向。

#### 4.3.2 核心流程

MicroBatch 的算账方式按请求类型不同（见 [PyMicroBatchPolicy 的三段分支，scheduler.py:568-674](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L568-L674)）：

| 请求类型 | 单步 token 计法 |
|----------|----------------|
| encoder 请求 | `encoder_output_len` |
| context 请求（不分块） | `base_tokens + draft_tokens`（可减去可复用的前缀 token） |
| context 请求（分块） | 切成 `chunk_size`，仅算本块 + 末块的 draft |
| generation 请求 | `beam_width + draft_tokens` |

预算不等式：

\[
\sum_{r \in \text{batch}} \mathrm{tokens}(r) \le \text{max\_num\_tokens}
\]

每纳入一个请求就把它的 token 计入 `batch_num_tokens`，若超预算则 `break`（停止纳入）。对 generation 请求还有一个 **beam width 一致性检查**：同一 batch 内所有 generation 请求的 beam width 必须相同，否则跳过（[scheduler.py:658-667](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L658-L667)）。

分块策略由 `ContextChunkingPolicy` 选择（[llm_args.py:3300-3308](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L3300-L3308)）：

- `FIRST_COME_FIRST_SERVED`（默认）：先到先得，每个请求贪心占满剩余预算。
- `EQUAL_PROGRESS`：齐头并进，每轮给每个待分块请求均加一个 `unit_size`。
- `FORCE_CHUNK`：按快照边界强制切（给 Mamba2/线性注意力这类无前缀复用的状态缓存用）。

#### 4.3.3 源码精读

同样有两套实现：

**(a) C++ 绑定版 `BindMicroBatchScheduler`（默认）**

[BindMicroBatchScheduler（scheduler.py:389-419）](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L389-L419) 把 `ctx_chunk_config` 转成 C++ `ContextChunkingConfig`，调用 C++ `MicroBatchScheduler`，再把返回的请求按 encoder/context 拆开。

**(b) 纯 Python 版 `PyMicroBatchScheduler`**

[PyMicroBatchScheduler.schedule（scheduler.py:526-728）](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L526-L728) 是一份相当长但结构清晰的实现，主循环遍历 `fitting_requests`，按请求类型分发到三段（A encoder / B context / C generation），累计 `batch_num_tokens` 并在超预算时 `break`。分块由 `_set_ctx_requests_chunk_size`（[scheduler.py:758-783](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L758-L783)）按策略分发到 `_chunk_fcfs` / `_chunk_equal_progress` / `_chunk_forced` 三个方法。

**(c) V2 版——容量与微批合一**

[KVCacheV2Scheduler（scheduler_v2.py:136-248）](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler_v2.py#L136-L248) 是 V2 的核心创新：**不再分两步**，而是在 `_schedule_loop`（[scheduler_v2.py:252-455](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler_v2.py#L252-L455)）里用一个 `BudgetTracker`（[scheduler_v2.py:43-133](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler_v2.py#L43-L133)）同时盯 token 预算和请求数预算，并在循环内直接调 `kv_cache_manager.resize_context()` / `try_allocate_generation()` 内联分配显存。

V2 还做了两个重要的两阶段处理：

1. **先 generation，后 context**：把 context 请求推迟到第二阶段（`pending_ctx`），确保 generation 的 PEFT 预算先算清，避免 adapter 驱逐冲突（[scheduler_v2.py:298-423](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler_v2.py#L298-L423)）。
2. **死锁检测**：若有 generation 候选却一个都没排上、也没驱逐任何请求，就抛 `RuntimeError`——否则调度器会永远空转（[scheduler_v2.py:430-446](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler_v2.py#L430-L446)）。这条错误信息很实用，提示你调大 `host_cache_size` 或 `max_tokens`。

#### 4.3.4 代码实践

**实践目标**：开启 chunked prefill，观察一条超长 prompt 被切成多块、与短请求同 batch 前进。

**操作步骤**：

1. 在 `llm_args` 里设 `enable_chunked_prefill=True`，并设一个较小的 `max_num_tokens`（例如 2048）。
2. 发送一条远超 `max_num_tokens` 的长 prompt（例如 8192 token），同时并发几条短请求。
3. 把日志调到 debug，关注 [scheduler.py:707-711](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L707-L711) 的 `context request scheduled: ID ..., chunk size N`。

**需要观察的现象**：长请求会连续多步出现在 `context_requests` 里，每步 `chunk size` 递增（累计向 prompt 末尾推进），同时短请求作为 generation 与之共存。

**预期结果**：长请求不再独占 GPU、短请求不用排队等长 prompt 算完；总吞吐提升。

> 待本地验证：chunk 行为依赖 `chunk_unit_size`（默认等于 `tokens_per_block`，[py_executor_creator.py:760-775](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor_creator.py#L760-L775)）。

#### 4.3.5 小练习与答案

**练习 1**：generation 请求每步只算 1 个 token（无投机解码时），为什么还要做「beam width 一致性检查」？
**参考答案**：beam search 时一个请求每步的 token 数等于当前 beam width，且会随迭代动态变化（1→2→…→beamWidth）。若把不同 beam width 的请求塞进同一 batch，张量形状无法对齐，因此必须保证 batch 内 generation 请求的 beam width 一致（[scheduler.py:658-667](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L658-L667)）。

**练习 2**：V2 调度器为什么要把 context 请求推迟到 generation 之后处理？
**参考答案**：因为 generation 请求可能持有 PEFT/LoRA adapter 且不能被中途驱逐。如果先收 context、把 PEFT 预算花光，随后 generation 想加载 adapter 时就会驱逐失败导致崩溃。先 generation 后 context 能保证 generation 的 adapter 预算先落定（见 [scheduler_v2.py:300-311](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler_v2.py#L300-L311) 的 `pre_claim_peft` 注释）。

---

### 4.4 SimpleScheduler 组合与自定义调度

#### 4.4.1 概念说明

前三节我们分别看了 capacity 与 microbatch。`SimpleScheduler`（[scheduler.py:422-453](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L422-L453)）就是把两者串起来的默认组合器。

但 TensorRT-LLM 的调度是 **高度可定制** 的。官方文档 [scheduler.md:24-32](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/scheduler.md#L24-L32) 给出两条定制路径：

1. **替换某一步**：继承 `CapacityScheduler` 或 `MicroBatchScheduler`，实现自己的 `schedule_request` / `schedule`，再用 `SimpleScheduler` 包起来。
2. **完全自定义**：如果两步架构不适合你，直接继承顶层抽象 `RequestScheduler`（[scheduler.py:218-238](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L218-L238)），一次性实现 `schedule_request`。

`RequestScheduler` 是所有调度器的顶层抽象，规定两个方法：

- `schedule_request(active_requests, inflight_request_ids) -> SchedulerOutput`：每步调度。
- `can_schedule(requests) -> bool`：PP 重试循环里的干跑检查（PyTorch 流程恒可视为 True/保守启发式）。

`SimpleScheduler`、`SimpleUnifiedScheduler`、`KVCacheV2Scheduler` 都实现了这个抽象。

#### 4.4.2 核心流程

定制并接入自定义调度器的完整链路：

```text
1. 写自己的调度器类（继承 CapacityScheduler / MicroBatchScheduler / RequestScheduler）
            │
            ▼
2. 在 _util.py 的 create_py_executor_instance() 里替换实例化
   （文档说 py_executor_creator.py，实际在 _util.py:2458-2527）
            │
            ▼
3. create_py_executor()（py_executor_creator.py:336）调用上面那个函数
            │
            ▼
4. PyExecutor 拿到 scheduler，每步调 scheduler.schedule_request(...)
```

实例化的 **三选一分支** 在 [_util.py:2458-2527](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/_util.py#L2458-L2527)：

| 条件 | 选用 |
|------|------|
| KV cache manager 是 `KVCacheManagerV2` | `KVCacheV2Scheduler`（合并单循环） |
| `scheduler_config.use_python_scheduler == True` | `SimpleUnifiedScheduler`（纯 Python 两步） |
| 否则（默认） | `BindCapacityScheduler` + `BindMicroBatchScheduler` → `SimpleScheduler`（C++ 绑定两步） |

注意 `scheduler_capacity` 这个细节：默认分支用的是 `max_batch_size * pp_size`（[scheduler.py 注释 / _util.py:2434](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/_util.py#L2434)），因为 capacity 调度要能容纳跨 PP stage 的请求；而 microbatch 用的是 `max_batch_size`（单次前向的上限）。

#### 4.4.3 源码精读

**默认装配分支**（你接自定义调度器最常改的地方）：

```python
# _util.py:2505-2527 （简化）
capacity_scheduler = BindCapacityScheduler(
    scheduler_capacity,
    kv_cache_manager.impl,
    peft_cache_manager.impl,
    scheduler_config.capacity_scheduler_policy,   # ← 换成你的类
    cross_kv_cache_manager.impl if ... else None,
    two_step_lookahead=mapping.has_pp(),
    no_schedule_until_state=no_schedule_until_state,
    enable_prefix_aware_scheduling=enable_prefix_aware_scheduling,
)
mb_scheduler = BindMicroBatchScheduler(max_batch_size, max_num_tokens, ctx_chunk_config)
scheduler = SimpleScheduler(capacity_scheduler, mb_scheduler)
```

要把 `BindCapacityScheduler` 换成你自己的类，**只需保证你的类实现了 `CapacityScheduler` 的抽象方法 `schedule_request`**，返回 `(fitting_requests, fitting_disagg_gen_init_requests, paused_requests)` 三元组即可。`SimpleScheduler` 不关心具体是哪个子类。

文档示例 [`GuaranteedNoEvictScheduler`, scheduler.md:38-89](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/scheduler.md#L38-L89) 就是这种「替换某一步」的范式：它继承 `CapacityScheduler`，自己实现 `schedule_request`，核心逻辑是文档那段——先为 generation 预留块、再用剩余块收 context。注释里特意提醒：

> Resource estimation should align with resource allocation and deallocation in `kv_cache_manager`.

意思是：你在调度器里「估算」需要的块数，必须和 KV cache manager 真正分配/释放的口径一致，否则会「调度说放得下、实际分配却失败」。这一点在 [u7-l1 分页 KV Cache](u7-l1-paged-kv-cache-manager.md) 讲过——`get_needed_resource_to_completion` / `get_max_resource_count` 这些接口正是为了让调度器和 manager 用同一把尺子。

`SimpleScheduler` 还有一个 `can_schedule` 干跑方法（[scheduler.py:450-453](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L450-L453)），它只跑容量调度看是否「全部都能 fit」，用于 PP 重试循环（[py_executor.py:2482-2510 `_pp_retry_until_can_schedule`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2482-L2510)）。自定义调度器也应实现它（V2 版目前返回保守的 `True`，见 [scheduler_v2.py:1076-1085](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler_v2.py#L1076-L1085)）。

#### 4.4.4 代码实践

**实践目标**：仿照文档示例，写一个简化版自定义 `CapacityScheduler` 骨架，并说明接入点。

**操作步骤**：

1. 新建一个文件（例如 `my_scheduler.py`，**注意不要改源码**，这只是练习产物，放你自己的目录），写入下面的示例代码：

```python
# 示例代码：简化版 GuaranteedNoEvict 风格 CapacityScheduler（仅供学习）
from tensorrt_llm._torch.pyexecutor.scheduler.scheduler import CapacityScheduler
from tensorrt_llm._torch.pyexecutor.llm_request import LlmRequestState


class MyNoEvictScheduler(CapacityScheduler):
    """只放行「块够跑到完成」的请求，保证不驱逐。"""

    def __init__(self, max_num_requests: int, kv_cache_manager):
        super().__init__()
        self.max_num_requests = max_num_requests
        self.kv_cache_manager = kv_cache_manager

    def schedule_request(self, active_requests):
        # 返回 (fitting_requests, fitting_disagg_gen_init_requests, paused_requests)
        scheduled = []
        max_blocks = self.kv_cache_manager.get_max_resource_count()
        reserved = 0
        for req in active_requests:
            if len(scheduled) >= self.max_num_requests or reserved >= max_blocks:
                break
            # 只看已经在跑的 generation（CONTEXT_INIT 的新请求留作练习扩展）
            if req.state in (LlmRequestState.GENERATION_IN_PROGRESS,
                             LlmRequestState.GENERATION_TO_COMPLETE):
                need = self.kv_cache_manager.get_needed_resource_to_completion(req)
                scheduled.append(req)
                reserved += need
        return scheduled, [], []
```

2. 在 [_util.py:2505](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/_util.py#L2505) 处，把 `BindCapacityScheduler(...)` 替换为 `MyNoEvictScheduler(scheduler_capacity, kv_cache_manager.impl)`（这一步需要你 fork 仓库改源码，仅限本地实验，不要提交）。

**需要观察的现象**：服务能正常启动并生成；日志里 generation 请求被放行，而（在你尚未实现 context 分支时）新 prompt 暂时进不了 batch。

**预期结果**：你理解了「自定义调度器 = 实现抽象方法 + 在装配点替换实例」这套机制。

> 待本地验证：本骨架只处理 generation，不能单独跑通完整服务；请把它当作理解接入点的脚手架，逐步补齐 context 分支后再实测。

#### 4.4.5 小练习与答案

**练习 1**：如果你的自定义调度器只想改变「收哪些请求」的逻辑，但完全不想碰 token 预算与分块，应该继承哪个类？
**参考答案**：继承 `CapacityScheduler`（[scheduler.py:312-322](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L312-L322)），只实现 `schedule_request`；microbatch 继续用默认的 `BindMicroBatchScheduler`，最后用 `SimpleScheduler` 把两者组合。这是改动最小、风险最低的定制方式。

**练习 2**：文档说接入点在 `py_executor_creator.py`，本讲却说在 `_util.py`，矛盾吗？
**参考答案**：不矛盾，是版本演进。`create_py_executor()`（在 `py_executor_creator.py:336`）是面向用户的装配入口，但它把「真正组装执行器」的活委托给了 `_util.py` 的 `create_py_executor_instance()`，调度器的三选一实例化（L2458-2527）就发生在后者里。文档写于更早版本，当时调度器实例化还在 `py_executor_creator.py` 内。理解时记住「入口在 creator，调度器实例化在 _util」即可。

---

## 5. 综合实践

**任务**：把本讲三个最小模块串起来，画一张「一条请求从到达到第一个 token 输出」的调度视角时序图，并标注每一步由哪个调度器负责。

要求：

1. 画出请求在以下角色之间的流转：`waiting_queue` → `active_requests` → `CapacityScheduler`（块准入）→ `MicroBatchScheduler`（token 预算/分块）→ `ResourceManager.prepare_resources` → `ModelEngine.forward` → `Sampler`。
2. 在图上标出三处「可能被挡住」的关卡：
   - 块不够（CapacityScheduler 不放行）→ 留在 `active_requests` 等下一轮。
   - token 超预算（MicroBatchScheduler 不纳入或切块）→ 本步不算或只算一块。
   - 分块未到末块（chunk）→ 本步不采样，要等多步把 prompt 算完。
3. 对照源码验证你的图：调度发生在 [py_executor.py:5277](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L5277)，资源准备在 `prepare_resources`，前向在 `_forward_step`。
4. 进阶：尝试在图上区分默认（C++ 绑定 `SimpleScheduler`）与 V2（`KVCacheV2Scheduler` 合并循环）两条路径的差异——V2 把容量与 token 预算合在一个 `_schedule_loop` 里，且内联做了 `resize_context` 分配。

**预期产出**：一张清晰的时序图（手绘或工具均可）+ 一段说明，解释「为什么短请求在 inflight batching 下能比 static batching 快得多拿到首 token」。

## 6. 本讲小结

- **inflight batching 的「动态」= 每步重新调度**：每个解码迭代都重新决定 batch 成员，短请求完成即让位，GPU 不空转。
- **两步调度各管一种资源**：`CapacityScheduler` 管 KV cache 块（显存容量），`MicroBatchScheduler` 管 token（单步算力预算），量纲不同所以必须分开。
- **`SimpleScheduler` 是默认组合器**：先 capacity 后 microbatch，输出 `SchedulerOutput`（context/generation 分开，因下游用不同注意力 kernel）。
- **三种策略**：`GUARANTEED_NO_EVICT`（默认，预留块不驱逐）、`MAX_UTILIZATION`（可暂停请求换利用率）、`STATIC_BATCH`。
- **三套实现 + 三选一装配**：默认 C++ 绑定（`BindCapacityScheduler`/`BindMicroBatchScheduler`）、纯 Python（`use_python_scheduler=True`）、V2 合并（`KVCacheManagerV2`）；实例化分支在 `_util.py:2458-2527`，入口在 `py_executor_creator.py:336`。
- **自定义调度器 = 实现抽象方法 + 在装配点替换**：文档示例 `GuaranteedNoEvictScheduler` 是教学版，真身是 `PyCapacityScheduler` + `GuaranteedNoEvictPolicy`；估算口径必须与 `kv_cache_manager` 的分配/释放一致。

## 7. 下一步学习建议

- **向下深入请求状态机**：本讲反复提到 `CONTEXT_INIT`/`GENERATION_IN_PROGRESS` 等状态，下一讲 [u8-l2 请求生命周期与状态机](u8-l2-request-lifecycle-state-machine.md) 会完整拆解 `LlmRequest` 的状态迁移、`waiting_queue` 与 `seq_slot_manager`。
- **向左回顾 KV cache**：CapacityScheduler 的所有「块」判断都依赖 `kv_cache_manager` 的容量接口，建议重读 [u7-l1 分页 KV Cache 与 KVCacheManager](u7-l1-paged-kv-cache-manager.md) 的 `get_needed_resource_to_completion` / `get_max_resource_count` 部分。
- **向右衔接采样与解码**：调度器决定「谁上场」之后，紧接着是采样；见 [u8-l3 Decoder 与 Sampling](u8-l3-decoder-and-sampling.md)。
- **源码延伸阅读**：若想看 V2 调度器的驱逐与挂起（suspend/resume）细节，精读 [scheduler_v2.py 的 `_try_evict_for_gen` 与 `_suspend_request`（scheduler_v2.py:977-1052）](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler_v2.py#L977-L1052)，这是理解分离式服务与多级缓存（GPU/Host/Disk）下调度行为的关键。
